-- Hard gate matching docs/scraper_data_quality.md: fails the build when a retailer's raw
-- partition breaches the scraper's own limits (error rate 20%, empty payload 5%, not-found 60%,
-- Dunnes 85%). Keep these numbers in sync with the scraper's MAX_*_RATE defaults.
WITH raw AS (
    {%- for retailer in var('retailers') %}
    SELECT '{{ retailer }}'{{ ' AS retailer' if loop.first }}, CAST(error AS VARCHAR){{ ' AS error' if loop.first }}, data IS NULL{{ ' AS no_data' if loop.first }} FROM {{ source('external_source', retailer) }}
    {{ 'UNION ALL' if not loop.last }}
    {%- endfor %}
),

agg AS (
    SELECT
        retailer,
        count(*) AS total_records,
        100.0 * count(*) FILTER (
            WHERE error IS NOT NULL AND NOT {{ is_stale_id_error('error') }}
        ) / count(*) AS error_pct,
        100.0 * count(*) FILTER (WHERE error IS NULL AND no_data) / count(*) AS empty_pct,
        100.0 * count(*) FILTER (WHERE {{ is_stale_id_error('error') }}) / count(*) AS not_found_pct
    FROM raw
    GROUP BY retailer
)

SELECT *
FROM agg
WHERE error_pct > 20
   OR empty_pct > 5
   OR not_found_pct > CASE retailer WHEN 'dunnes' THEN 85 ELSE 60 END
