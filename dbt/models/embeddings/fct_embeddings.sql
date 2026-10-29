{{ config(materialized='table') }}

-- Current embeddings, long format: one row per (spec, product, vector); selection logic in
-- int_embedding_current_keys. Vector sizes differ per spec, hence the variable-length FLOAT[]; use
-- the per-spec emb_* relations for fixed-size ARRAYs.
SELECT
    k.spec_id,
    k.supermarket,
    k.id,
    k.vector_index,
    v.embedding,
    k.content_key,
    k.embedded_at
FROM {{ ref('int_embedding_current_keys') }} AS k
INNER JOIN {{ ref('stg_embedding_vectors') }} AS v
    ON v.spec_id = k.spec_id
    AND v.supermarket = k.supermarket
    AND v.id = k.id
    AND v.vector_index = k.vector_index
    AND v.embedded_at = k.embedded_at
