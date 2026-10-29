{{ config(materialized='view') }}

-- The vectors of the successful rows of every spec, keyed like stg_embedding_parts
-- (spec_id, supermarket, id, vector_index, embedded_at).
SELECT
    spec_id::VARCHAR AS spec_id,
    supermarket::VARCHAR AS supermarket,
    id::VARCHAR AS id,
    coalesce(vector_index, 0)::INTEGER AS vector_index,
    embedded_at::TIMESTAMP AS embedded_at,
    embedding::FLOAT[] AS embedding
FROM ({{ read_embedding_parts() }})
WHERE status = 'ok'
