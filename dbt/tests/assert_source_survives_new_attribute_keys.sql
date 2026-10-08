-- Sanity check for the raw-source read settings (union_by_name=true in models/staging/raw_s3.yml):
-- files whose free-form data.attributes object has different keys must load with every row and key
-- intact. Caveat: the original failure ("has unknown key 'back To School'") only reproduces on
-- full-size days (2026-10-08 dunnes/supervalu), not on this two-row fixture, so this guards the
-- query shape, not the sampling behaviour. Fixture path is relative to where dbt runs (repo root);
-- override with --vars '{fixtures_dir: ...}'.
WITH loaded AS (
    SELECT
        product_id,
        data.attributes.vegan AS vegan,
        data.attributes.IrishMade.value AS irish_made,
        data.attributes['back To School'].value AS back_to_school
    FROM read_json('{{ var("fixtures_dir", "dbt/tests/fixtures") }}/attributes_drift/*.jsonl', union_by_name=true)
)

SELECT 'wrong row count' AS problem FROM (SELECT count(*) AS n FROM loaded) WHERE n <> 2
UNION ALL
SELECT 'attribute lost' FROM loaded
WHERE (product_id = '1' AND (vegan IS NOT TRUE OR irish_made IS NOT FALSE))
   OR (product_id = '2' AND (vegan IS NOT FALSE OR back_to_school IS NOT TRUE))
