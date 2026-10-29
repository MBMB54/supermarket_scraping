{{ config(tags=['embeddings']) }}
-- Every current vector must have the length recorded in dim_embedding_specs (so a spec never mixes
-- sizes), and each emb_* relation's dim in the embedding_specs var must match the data. Returns the
-- offending specs.
WITH bad_vectors AS (
    SELECT e.spec_id, count(*) AS n_bad
    FROM {{ ref('fct_embeddings') }} AS e
    JOIN {{ ref('dim_embedding_specs') }} AS d USING (spec_id)
    WHERE len(e.embedding) <> d.dim
    GROUP BY e.spec_id
),

configured AS (
    SELECT * FROM (VALUES
{%- for key, cfg in var('embedding_specs').items() %}
        {{ ',' if not loop.first }}('{{ key }}', '{{ cfg["spec_id"] }}', {{ cfg["dim"] }})
{%- endfor %}
    ) AS t(key, spec_id, configured_dim)
)

SELECT spec_id, 'vector length differs from dim' AS problem, n_bad AS n
FROM bad_vectors
UNION ALL
SELECT c.spec_id, 'embedding_specs var dim ' || c.configured_dim || ' != data dim ' || coalesce(d.dim::VARCHAR, 'NULL (spec missing)'), 1
FROM configured AS c
LEFT JOIN {{ ref('dim_embedding_specs') }} AS d USING (spec_id)
WHERE d.dim IS DISTINCT FROM c.configured_dim
