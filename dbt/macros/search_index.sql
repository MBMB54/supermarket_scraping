{#- BM25 full-text index for dim_product_search, rebuilt after every build of the table.

    DuckDB's fts extension has no incremental update (create_fts_index rebuilds the whole index into
    schema fts_main_<table>) and dbt's table materialization recreates the table, so the index is
    recreated in a post-hook. It runs only on the targets in var fts_targets (local DuckDB files):
    the scheduled `batch` target uses an in-memory main database, so an index there would be
    discarded, and DuckLake cannot hold indexes. Indexed fields: title_cleaned, brand and the four
    category levels; ingredients and descriptions are deliberately left out (long text swamps the
    title signal, see dbt/README.md). Also (re)creates the table macro bm25_search(query).

    Table macros bind lazily, so bm25_search exists even before the first index build. -#}
{% macro build_search_index(relation) %}
{%- if execute and target.name in var('fts_targets') -%}
INSTALL fts;
LOAD fts;
PRAGMA create_fts_index(
    '{{ relation.identifier }}', 'product_key',
    'title_cleaned', 'brand', 'category_1', 'category_2', 'category_3', 'category_4',
    overwrite = 1
);
CREATE OR REPLACE MACRO bm25_search(query_text) AS TABLE
    SELECT product_key, score
    FROM (
        SELECT
            product_key,
            fts_main_{{ relation.identifier }}.match_bm25(product_key, query_text) AS score
        FROM {{ relation }}
    )
    WHERE score IS NOT NULL;
{%- else -%}
SELECT 1
{%- endif -%}
{% endmacro %}
