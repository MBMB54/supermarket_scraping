{{ config(tags=['search']) }}
-- On local targets (var fts_targets) the BM25 index must exist and cover exactly the rows of
-- dim_product_search: an index left over from an earlier build, or none, means search results are
-- stale. The index is rebuilt by dim_product_search's post-hook. Passes trivially elsewhere (the
-- scheduled `batch` target builds no index).
{% if target.name in var('fts_targets') %}
-- The fts extension must be loaded for the schema to resolve; the post-hook loads it per session.
SELECT 'index missing or stale' AS problem, n_index, n_table
FROM (
    SELECT
        (SELECT count(*) FROM {{ ref('dim_product_search').database }}.fts_main_dim_product_search.docs) AS n_index,
        (SELECT count(*) FROM {{ ref('dim_product_search') }}) AS n_table
)
WHERE n_index <> n_table
{% else %}
SELECT 'unused on this target' AS problem WHERE false
{% endif %}
