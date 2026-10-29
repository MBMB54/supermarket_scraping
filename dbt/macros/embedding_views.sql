{#- Body of a per-spec embedding relation (models/embeddings/emb_*.sql): the current vectors of one
    spec from fct_embeddings with the embedding cast to a fixed-size FLOAT[dim] ARRAY, which
    array_cosine_similarity / array_distance and DuckDB's vss HNSW index require (a variable-length
    list does not work with them). `key` names an entry of the embedding_specs var in
    dbt_project.yml: {spec_id, dim}. Dim is explicit on purpose (no run-time introspection, so
    parse/compile stay static); the assert_embedding_dims test fails if it disagrees with the data. -#}
{% macro embedding_view(key) %}
{%- set cfg = var('embedding_specs')[key] -%}
SELECT
    supermarket,
    id,
    vector_index,
    embedding::FLOAT[{{ cfg['dim'] }}] AS embedding,
    content_key,
    embedded_at
FROM {{ ref('fct_embeddings') }}
WHERE spec_id = '{{ cfg["spec_id"] }}'
{% endmacro %}

{#- All rows of all specs in the embedding store, with a vector_index column guaranteed to exist.
    Older parts predate vector_index (those rows are vector 0, like ml_common.table.read_table);
    union_by_name alone cannot add a column that no part has, so the empty UNION ALL BY NAME arm
    supplies it. Used by stg_embedding_parts and stg_embedding_vectors. -#}
{% macro read_embedding_parts() %}
SELECT * FROM read_parquet('{{ var("embeddings_store_prefix") }}/embeddings/*/part-*.parquet', union_by_name=true)
UNION ALL BY NAME
SELECT NULL::INTEGER AS vector_index WHERE false
{% endmacro %}
