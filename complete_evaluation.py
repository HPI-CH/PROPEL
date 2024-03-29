import argparse
import os
import json
import time
import random
from datetime import datetime

import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split

from src.data import get_data_from_name
from src.preprocess import get_preprocessed_data
from src.models import get_classification_model_grid
from src.evaluate import evaluate_single_model
from src.utils.metrics import all_classification_metrics_list
from src.utils.plot import boxplot, plot_summary_roc_pr, plot_summary_roc, plot_summary_prc


def main(args):
    # Setup output directory
    seed = args.seed
    np.random.seed(seed)
    random.seed(seed)
    if args.out_dir is None:
        feature_set_string = '' if args.feature_set is None else f'_{"_".join(args.feature_set)}'
        args.out_dir = f'results_{args.dataset}{feature_set_string}_{str(datetime.now().strftime("%Y-%m-%dT%H-%M-%S"))}_seed_{seed}'
    else:
        feature_set_string = '' if args.feature_set is None else f'_{"_".join(args.feature_set)}'
        args.out_dir = f'{args.out_dir}/results_{args.dataset}{feature_set_string}_{str(datetime.now().strftime("%Y-%m-%dT%H-%M-%S"))}_seed_{seed}'

    os.makedirs(f'{args.out_dir}', exist_ok=True)
    os.makedirs(f'{args.out_dir}/data_frames', exist_ok=True)

    # Get DataInformation object for the specified task
    data = get_data_from_name(args.dataset)

    # Parse data
    data.parse(drop_columns=args.drop_features, feature_set=args.feature_set, drop_missing_value=args.drop_missing_value,
               out_dir=args.out_dir, exploration=args.data_exploration, external_validation=args.external_testset)

    # Preprocess data
    X, Y = get_preprocessed_data(data,
                                 fs_operations=args.feature_selectors,
                                 missing_threshold=args.missing_threshold,
                                 correlation_threshold=args.correlation_threshold,
                                 imputer=args.imputer,
                                 normaliser=args.normaliser,
                                 verbose=True,
                                 validation=False)

    # Preprocess external validation data
    if args.external_testset:
        X_val, Y_val = get_preprocessed_data(data,
                                             fs_operations=args.feature_selectors,
                                             missing_threshold=args.missing_threshold,
                                             correlation_threshold=args.correlation_threshold,
                                             imputer=args.imputer,
                                             normaliser=args.normaliser,
                                             verbose=True, validation=True)
        print(f'Dropping columns in val data since they are missing in train data: {X_val.columns.difference(X.columns)}')
        # Get rid of extra columns introduced by values in validation dataset
        X_val = X_val.drop(set(X_val.columns.difference(X.columns)), axis=1)
        assert len(X.columns.difference(X_val.columns)) == 0, f'Error: Train data includes columns {X.columns.difference(X_val.columns)} that are missing in val data'
    all_metrics_list = all_classification_metrics_list

    all_test_metric_dfs = {metric: pd.DataFrame(dtype=np.float64) for metric in all_metrics_list if metric != 'confusion_matrix'}

    with open(f'{args.out_dir}/best_parameters.txt', 'a+') as f:
        f.write(f'\n========== New Trial at {time.strftime("%d.%m.%Y %H:%M:%S")} ==========\n')
        f.write(str(vars(args)))
        f.write('\n')

    for k, label_col in enumerate(Y.columns):
        print(f'Predicting {label_col}')
        with open(f'{args.out_dir}/best_parameters.txt', 'a+') as f:
            f.write(f'=====\n{label_col}\n=====')

        # Set endpoint for iteration
        y = Y[label_col]

        # If we do not have an external validation dataset, we split the original dataset
        if not args.external_testset:
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=args.test_fraction,
                                                                random_state=seed, shuffle=True, stratify=y)
        else:
            # Set endpoint for iteration
            y_val = Y_val[label_col]

            # Set train and test
            X_train = X
            y_train = y
            X_test = X_val
            y_test = y_val

        all_model_metrics = {}

        # model grid
        model_grid = get_classification_model_grid('balanced' if args.balancing_option == 'class_weight' else None,
                                                   seed=args.seed)
        for j, (model, param_grid) in enumerate(model_grid):
            val_metrics, test_metrics, curves = evaluate_single_model(model, param_grid,
                                                                      X_train, y_train, X_test, y_test,
                                                                      cv_splits=args.cv_splits,
                                                                      select_features=args.select_features,
                                                                      shap_value_eval=args.shap_eval,
                                                                      out_dir=args.out_dir,
                                                                      sample_balancing=args.balancing_option,
                                                                      seed=seed)
            all_model_metrics[str(model.__class__.__name__)] = (val_metrics, test_metrics, curves)

        # ===== Save aggregate plots across models =====
        # Generate Boxplots for Metrics
        json_metric_data = {}
        for metric_name in all_model_metrics[str(model.__class__.__name__)][0].keys():
            if metric_name == 'confusion_matrix':
                json_metric_data[metric_name] = {model_name: ([cv_cm.tolist() for cv_cm in val_metrics[metric_name]], test_metrics[metric_name].tolist())
                                                 for model_name, (val_metrics, test_metrics, _) in all_model_metrics.items()}
                continue
            metric_data = {model_name: (val_metrics[metric_name], test_metrics[metric_name])
                           for model_name, (val_metrics, test_metrics, _) in all_model_metrics.items()}
            json_metric_data[metric_name] = metric_data
            boxplot(args.out_dir, metric_data, metric_name, label_col, ymin=(-1 if metric_name == 'mcc' else 0))
        json.dump(json_metric_data, open(f'{args.out_dir}/{label_col}/all_model_metrics.json', 'w'), indent=4)

        # Plot roc pr for all models
        plot_summary_roc(all_model_metrics, args.out_dir, label_col, dataset_partition='val', legend=True, value_in_legend=False)
        plot_summary_roc(all_model_metrics, args.out_dir, label_col, dataset_partition='test', legend=True, value_in_legend=False)
        plot_summary_prc(all_model_metrics, args.out_dir, label_col, y, dataset_partition='val', legend=True, value_in_legend=False)
        plot_summary_prc(all_model_metrics, args.out_dir, label_col, y, dataset_partition='test', legend=True, value_in_legend=False)
        plot_summary_roc_pr(all_model_metrics, args.out_dir, label_col, y)

        # save results in DF
        for model_name, test_data in {model_name: entry[1] for model_name, entry in all_model_metrics.items()}.items():
            for metric, value in test_data.items():
                if metric == 'confusion_matrix':
                    continue
                all_test_metric_dfs[metric].loc[model_name, label_col.replace(' ', '_')] = value

        for metric, df in all_test_metric_dfs.items():
            df.to_csv(f'{args.out_dir}/data_frames/{metric}.csv')


def get_parser():
    parser = argparse.ArgumentParser('Evaluate classical ML models on post-operative complications dataset.\n' +
                                     'Test metrics correspond to the results of a classification threshold optimised ' +
                                     'based on the optimal F1-score.')

    parser.add_argument('dataset', type=str, choices=['cass_retro', 'esophagus', 'complications', 'stomach'],
                        help='the dataset to process')
    parser.add_argument('--feature_set', '-f', nargs='*', type=str, choices=['pre', 'intra', 'post', 'dyn'],
                        help='if given, processes only features from all provided feature sets')
    parser.add_argument('--external_testset', '-e', action='store_true',
                        help='if specified, external validation dataset will be used as test data')
    parser.add_argument('--imputer', '-i', choices=['iterative', 'knn', 'mean'], nargs='?', const='knn', default=None,
                        help='Which imputer to use for missing values')
    parser.add_argument('--normaliser', '-n', choices=['standard', 'minmax'], nargs='?', const='standard', default=None,
                        help='Which normaliser to use to scale numerical values')
    parser.add_argument('--feature_selectors', '-fs', choices=['missing', 'single_unique', 'collinear'], nargs='*', default=['missing', 'single_unique', 'collinear'],
                        help='Which feature selection functions to use. Do not specify for all, use flag without args for none.')
    parser.add_argument('--out_dir', '-o', type=str,
                        help='output directory')
    parser.add_argument('--no_features_dropped', '-nfd', action='store_false', dest='drop_features',
                        help='deactivates dropping predefined features in dataframe')
    parser.add_argument('--no_feature_selection', '-nfs', action='store_false', dest='select_features',
                        help='deactivates feature selection in pipeline')
    parser.add_argument('--cv_splits', '-cv', type=int, default=10,
                        help='number of cross_validation splits; 1 denotes LOO-CV')
    parser.add_argument('--shap_eval', '-sh', type=bool, default=False,
                        help='if true, shap values will be evaluated. Disabled by default, since it increases runtime a lot.')
    parser.add_argument('--test_fraction', '-t', type=float, default=0.2,
                        help='size of the test set in fraction of total samples')
    parser.add_argument('--balancing_option', '-b', type=str, default='class_weight',
                        choices=['class_weight', 'random_oversampling', 'SMOTE', 'ADASYN', 'none'],
                        help='technique to deal with imbalanced data')
    parser.add_argument('--drop_missing_value', '-dr', type=float, default=0,
                        help='Drop rows with x% of columns having missing values')
    parser.add_argument('--missing_threshold', '-mt', type=float, default=0.5,
                        help='Threshold for dropping columns with missing values')
    parser.add_argument('--correlation_threshold', '-ct', type=float, default=0.95,
                        help='Threshold for dropping columns with high correlation')
    parser.add_argument('--data_exploration', '-ex', action='store_true',
                        help='if true, an html file will be generated showing statistics of the parsed dataset')
    parser.add_argument('--seed', '-s', type=int, default=42,
                        help='If true, a seed will be set for reproducibility')

    return parser


if __name__ == '__main__':
    arg_parser = get_parser()
    args = arg_parser.parse_args()
    print(args)
    main(args)
