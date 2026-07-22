{{ config(materialized='table') }}

{% set model_slug = var("embedding_model", "microsoft-harrier-oss-v1-270m") %}

SELECT
    id,
    supermarket,
    title,
    title_embedding,
    embedding_model,
    embedded_at
FROM read_parquet('s3://ie-supermarket-data/processed/embeddings/{{ model_slug }}/product_title_embeddings.parquet')
