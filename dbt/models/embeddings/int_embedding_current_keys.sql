{{ config(materialized='view') }}

-- Which embeddings are current. Mirrors ml_common.table.latest_ok: among a key's successful rows
-- keep every vector of the newest embedded_at (a multi-vector item is written in one chunk, so its
-- vectors share embedded_at; older embeddings of a changed title or image are dropped whole). An
-- error row never hides the last good vector because only ok rows are ranked.
SELECT
    spec_id,
    supermarket,
    id,
    vector_index,
    content_key,
    embedded_at
FROM {{ ref('stg_embedding_parts') }}
WHERE status = 'ok'
QUALIFY embedded_at = max(embedded_at) OVER (PARTITION BY spec_id, supermarket, id)
