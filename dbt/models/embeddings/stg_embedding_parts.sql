{{ config(materialized='view') }}

-- Bookkeeping columns of every row of every embedding spec in the ml_common store
-- (ml/embeddings/<spec_id>/part-*.parquet), including superseded embeddings and error rows. Parts
-- are append-only. The vector itself is in stg_embedding_vectors, kept apart so the selection logic
-- (int_embedding_current_keys) has only primitive columns and can be unit tested.
SELECT
    id::VARCHAR AS id,
    supermarket::VARCHAR AS supermarket,
    coalesce(vector_index, 0)::INTEGER AS vector_index,
    content_key::VARCHAR AS content_key,
    spec_id::VARCHAR AS spec_id,
    model_id::VARCHAR AS model_id,
    model_revision::VARCHAR AS model_revision,
    modality::VARCHAR AS modality,
    embedded_at::TIMESTAMP AS embedded_at,
    status::VARCHAR AS status,
    error::VARCHAR AS error
FROM ({{ read_embedding_parts() }})
