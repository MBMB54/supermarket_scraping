{{ config(materialized='table') }}

-- One row per embedding spec: manifest metadata plus statistics measured from the data. dim is the
-- length of the stored vectors (the manifest's dim is NULL when no Matryoshka truncation was set);
-- n_items / n_vectors count current embeddings (fct_embeddings), embedded_from / embedded_to span
-- all successful embeddings including superseded ones.
WITH current_stats AS (
    SELECT
        spec_id,
        max(len(embedding)) AS dim,
        count(DISTINCT (supermarket, id)) AS n_items,
        count(*) AS n_vectors
    FROM {{ ref('fct_embeddings') }}
    GROUP BY spec_id
),

history AS (
    SELECT
        spec_id,
        min(embedded_at) AS embedded_from,
        max(embedded_at) AS embedded_to
    FROM {{ ref('stg_embedding_parts') }}
    WHERE status = 'ok'
    GROUP BY spec_id
)

SELECT
    m.spec_id,
    m.name,
    m.model_id,
    m.model_revision,
    m.modality,
    m.prefix,
    s.dim::INTEGER AS dim,
    m.image_tokens,
    s.n_items::BIGINT AS n_items,
    s.n_vectors::BIGINT AS n_vectors,
    h.embedded_from,
    h.embedded_to
FROM {{ ref('stg_embedding_manifests') }} AS m
LEFT JOIN current_stats AS s USING (spec_id)
LEFT JOIN history AS h USING (spec_id)
