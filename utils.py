import glob
import scipy
import numpy as np
import pandas as pd
import scanpy as sc
from anndata import AnnData, read_h5ad
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from pyscenic.aucell import aucell, derive_auc_threshold
from pyscenic.genesig import GeneSignature


def preprocess_adata(adata):
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    return adata


def load_adata(path, adata_is_given=False, sparse_is_given=False):
    if adata_is_given:
        adata = read_h5ad(f'{path}adata.h5ad')
    elif sparse_is_given:
        cl = pd.read_csv(f'{path}cell_labels.csv')
        genes = pd.read_csv(f'{path}genes_symbol.csv', header=None, names=['gene_symbol'])
        genes.index = genes['gene_symbol'].values
        sparse = scipy.sparse.load_npz(f'{path}matrix_sparse.npz')
        adata = AnnData(sparse, var=genes, obs=cl)
    else:
        cl = pd.read_csv(f'{path}cell_labels.csv')
        genes = pd.read_csv(f'{path}genes_symbol.csv', header=None, names=['gene_symbol'])
        genes.index = genes['gene_symbol'].values
        dense = pd.read_csv(f'{path}counts.csv', index_col=0)
        adata = AnnData(dense.reset_index(drop=True), obs=cl.reset_index(drop=True))
        adata.var = genes
    adata.var_names_make_unique()
    return adata


def gene_selector(
    adata,
    obs_name,
    label_upreg,
    label_downreg=None,
    lfc_threshold=3,
    pval_threshold=0.1,
    DE_method='t-test_overestim_var',
    sort_by='logfoldchanges',
    sort_ascending=False
):
    if label_downreg is None:
        adata = adata.copy()
        adata.obs['1_vs_all'] = [label_upreg if label == label_upreg
                                 else 'Other' for label in adata.obs[obs_name]]
        obs_name = '1_vs_all'
        label_downreg = 'Other'

    if label_downreg is not None:
        unique_labels = np.unique(adata.obs[obs_name])
        if label_upreg not in unique_labels or label_downreg not in unique_labels:
            return None

    adata = sc.tl.rank_genes_groups(
        adata,
        groupby=obs_name,
        groups=[label_upreg, label_downreg],
        key_added=f'{DE_method}_results',
        n_genes=15000,
        copy=True,
        method=DE_method
    )

    DE_results_df = pd.DataFrame()
    DE_results_df['gene_symbol'] = adata.uns[f'{DE_method}_results']['names'][label_upreg]
    DE_results_df['logfoldchanges'] = adata.uns[f'{DE_method}_results']['logfoldchanges'][label_upreg]
    DE_results_df['p'] = adata.uns[f'{DE_method}_results']['pvals'][label_upreg]
    DE_results_df['padj'] = adata.uns[f'{DE_method}_results']['pvals_adj'][label_upreg]
    DE_results_df['scores'] = adata.uns[f'{DE_method}_results']['scores'][label_upreg]
    DE_results_df = DE_results_df.loc[((DE_results_df['padj'] < pval_threshold)
                                       & (DE_results_df['logfoldchanges'] > lfc_threshold))]
    DE_results_df.sort_values(by=sort_by, ascending=sort_ascending, inplace=True)
    # gene_list = list(DE_results_df['gene_symbol'].values)
    return DE_results_df


def gene_list_integrator(
    list_of_DE_results_df,
    integration_fun,
    integrate_by='logfoldchanges',
    sort_ascending=False,
    top_x=100
):
    dfs = [df.copy() for df in list_of_DE_results_df]
    if len(dfs) == 0:
        raise('Error: Neither input dataset contains either upregulated or downregulated labels.')
    for i, df in enumerate(dfs):
        dfs[i].set_index(df['gene_symbol'], inplace=True)
        dfs[i] = df[integrate_by]
        dfs[i].name = f'{integrate_by}{i}'
    DE_results_df = integration_fun(dfs)

    for i in range(len(dfs)):
        DE_results_df[f'{integrate_by}{i}'] /= DE_results_df[f'{integrate_by}{i}'].max()
    DE_results_df['weighted_avg'] = (
        DE_results_df[[
            f'{integrate_by}{i}' for i in range(len(dfs))
        ]].mean(axis=1)
    )
    DE_results_df.sort_values(by='weighted_avg', ascending=sort_ascending, inplace=True)
    DE_results_df[integrate_by] = DE_results_df['weighted_avg']
    DE_results_df['gene_symbol'] = DE_results_df.index.values

    gene_list = list(DE_results_df.index.values)
    gene_list = gene_list[:int(top_x)] if len(gene_list) >= top_x else gene_list
    return gene_list, DE_results_df


def intersection_fun(x): return pd.concat(x, axis=1, join='inner')
def union_fun(x): return pd.concat(x, axis=1, join='outer')


def create_gmt(gene_list_dict):
    gmt = pd.DataFrame(
        [val for val in gene_list_dict.values()], 
        index=[key for key in gene_list_dict.keys()]
        )
    gmt.insert(0, "00", "ikarus")
    gmt.to_csv("out/signatures.gmt", header=None)


def cell_scorer(
    adata,
    name
):
    gs = GeneSignature.from_gmt("out/signatures.gmt", field_separator=',', gene_separator=',')
    df = adata.to_df()
    percentiles = derive_auc_threshold(df)
    scores = aucell(
        exp_mtx=df,
        signatures=gs, 
        auc_threshold=percentiles[0.01],
        seed=2, 
        normalize=True
        )
    scores.to_csv(f"out/{name}/AUCell_norm_scores.csv", index=False)


def cell_annotator(
    connectivities_dict,
    results_dict,
    names_list,
    obs_names_list,
    training_names_list,
    test_name,
    input_features,
    certainty_threshold,
    n_iter
):
    # first training, pre label propagation
    # Tumor vs Normal classification
    X_features = input_features
    X_dict = {}
    y_dict = {}
    Model = LogisticRegression()
    # Model = RandomForestClassifier()
    for name, obs_name in zip(names_list, obs_names_list):
        X_dict[name] = results_dict[name].loc[:, X_features]
        y_dict[name] = results_dict[name].loc[:, obs_name]
    
    y_train = pd.concat(
        [y_dict[name] for name in training_names_list], 
        axis=0, ignore_index=True)
    X_train = pd.concat(
        [X_dict[name] for name in training_names_list], 
        axis=0, ignore_index=True)
    X_test = X_dict[test_name]
    
    _ = Model.fit(X_train, y_train)
    y_pred_lr = Model.predict(X_test)
    y_pred_proba_lr = Model.predict_proba(X_test)

    results_dict[test_name]['LR_proba_Normal'] = y_pred_proba_lr[:, 0]
    results_dict[test_name]['LR_proba_Tumor'] = y_pred_proba_lr[:, 1]
    results_dict[test_name]['LR_tier_0_prediction'] = y_pred_lr

    results_dict[test_name] = label_propagation(
        results_dict[test_name], 
        connectivities_dict[test_name],
        n_iter, 
        certainty_threshold
        )

    return results_dict[test_name]


def label_propagation(results, connectivities, n_iter, certainty_threshold):
    proba_tier_0 = results.loc[
        :, ['LR_proba_Normal', 'LR_proba_Tumor']
        ].copy()
    proba_tier_0.columns = ['Normal', 'Tumor']

    #certain?
    absdif = abs(results['Normal'] - results['Tumor'])
    results['certain'] = False
    results.loc[
        absdif > absdif.quantile(q=certainty_threshold),
        'certain'
        ] = True

    for i in range(n_iter):
        certainty_threshold_pct = certainty_threshold * np.linspace(1, 0, n_iter)[i]
        results[f'certain{i}'] = False
        results.loc[
            absdif > absdif.quantile(q=certainty_threshold_pct),
            f'certain{i}'
            ] = True 
        proba_tier_0.loc[results[f'certain{i}'] == False] = 0.000001
                 
        lp_step_mtx = np.dot(connectivities, proba_tier_0.values)
        lp_step_mtx = np.divide(lp_step_mtx, lp_step_mtx.sum(axis=1))
        proba_tier_0.loc[:, :] = lp_step_mtx

        current = proba_tier_0.idxmax(axis=1)
        if not i < 5:
            if ((current != pre).sum() / current.size) < 0.001:
                break
        if i == n_iter - 1:
            print(f'Warning: Label propagation did not converge ({((current != pre).sum() / current.size):.4f} >= 0.001) within {n_iter} iterations!')
        pre = current

    # get prediction and arrange output file
    results[
        'LR_with_label_propagation_tier_0_prediction'
        ] = proba_tier_0.idxmax(axis=1)
    results[
        'LR_with_label_propagation_proba_Normal'
        ] = proba_tier_0['Normal']
    results[
        'LR_with_label_propagation_proba_Tumor'
        ] = proba_tier_0['Tumor']
    
    return results


def load_scores(
    name,
    adata
):
    if "tier_0_hallmark_corrected" in adata.obs.columns:
        adata.obs["tier_0_raw"] = adata.obs["tier_0"]
        adata.obs["tier_0"] = adata.obs["tier_0_hallmark_corrected"]
    scores = pd.read_csv(f"out/{name}/AUCell_norm_scores.csv", index_col=False)
    result_df = pd.concat(
        [adata.obs.reset_index(drop=True), scores], 
        axis=1
    )

    return result_df


def calculate_connectivities(
    adata,
    n_neighbors,
    use_highly_variable
):
    sc.pp.highly_variable_genes(adata)
    sc.tl.pca(adata, use_highly_variable=use_highly_variable)
    sc.pp.neighbors(adata, n_neighbors=n_neighbors, method='umap')
    connectivities = adata.obsp['connectivities'].todense()
    return connectivities


def load_connectivities(
    name
):
    connectivities = scipy.sparse.load_npz(
        f'out/{name}/connectivities_sparse.npz'
        ).todense()
    return connectivities