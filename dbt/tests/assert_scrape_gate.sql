-- Hard gate matching docs/scraper_data_quality.md: fails the build when a retailer's raw
-- partition breaches the scraper's own limits (error rate 20%, empty payload 5%, not-found 60%,
-- Dunnes 85%). Keep these numbers in sync with the scraper's MAX_*_RATE defaults.
WITH raw AS (
    SELECT 'aldi' AS retailer, CAST(error AS VARCHAR) AS error, data IS NULL AS no_data FROM {{ source('external_source', 'aldi') }}
    UNION ALL
    SELECT 'tesco', CAST(error AS VARCHAR), data IS NULL FROM {{ source('external_source', 'tesco') }}
    UNION ALL
    SELECT 'dunnes', CAST(error AS VARCHAR), data IS NULL FROM {{ source('external_source', 'dunnes') }}
    UNION ALL
    SELECT 'supervalu', CAST(error AS VARCHAR), data IS NULL FROM {{ source('external_source', 'supervalu') }}
),

agg AS (
    SELECT
        retailer,
        count(*) AS total_records,
        100.0 * count(*) FILTER (
            WHERE error IS NOT NULL AND error NOT IN ('Not found in any store', 'HTTP 404')
        ) / count(*) AS error_pct,
        100.0 * count(*) FILTER (WHERE error IS NULL AND no_data) / count(*) AS empty_pct,
        100.0 * count(*) FILTER (WHERE error IN ('Not found in any store', 'HTTP 404')) / count(*) AS not_found_pct
    FROM raw
    GROUP BY retailer
)

SELECT *
FROM agg
WHERE error_pct > 20
   OR empty_pct > 5
   OR not_found_pct > CASE retailer WHEN 'dunnes' THEN 85 ELSE 60 END
