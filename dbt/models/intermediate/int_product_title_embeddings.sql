{{ config(materialized='table') }}

{% set model_slug = var("embedding_model", "microsoft-harrier-oss-v1-270m") %}

-- Embeddings are computed offline (ml/embeddings.py) and read back from S3, not computed in dbt
SELECT
    id,
    supermarket,
    title,
    title_embedding::FLOAT[640] AS title_embedding,
    embedding_model,
    embedded_at
FROM read_parquet('{{ var("embeddings_input_prefix") }}/embeddings/{{ model_slug }}/product_title_embeddings.parquet')
