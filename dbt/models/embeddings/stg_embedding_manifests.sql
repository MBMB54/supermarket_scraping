{{ config(materialized='view') }}

-- One row per spec from ml/embeddings/<spec_id>/manifest.json (written once by ml_common when the
-- spec's first part is appended). Fields are read through to_json so a manifest written by an older
-- or newer ml_common (extra or missing spec keys) does not break the read.
WITH manifests AS (
    SELECT to_json(spec) AS spec
    FROM read_json('{{ var("embeddings_store_prefix") }}/embeddings/*/manifest.json', union_by_name=true)
)

SELECT
    json_extract_string(spec, '$.spec_id') AS spec_id,
    json_extract_string(spec, '$.name') AS name,
    json_extract_string(spec, '$.model_id') AS model_id,
    json_extract_string(spec, '$.revision') AS model_revision,
    json_extract_string(spec, '$.modality') AS modality,
    json_extract_string(spec, '$.prefix') AS prefix,
    try_cast(json_extract_string(spec, '$.image_tokens') AS INTEGER) AS image_tokens
FROM manifests
