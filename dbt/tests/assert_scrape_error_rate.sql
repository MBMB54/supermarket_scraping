-- Early-warning monitor on the raw scrape (warn only). Thresholds mirror the scraper gate in
-- docs/scraper_data_quality.md; this fires at a quarter of the gate's error limit so a trend is
-- visible before the Batch job itself fails. Scans four raw partitions: run on the daily build.
{{ config(severity = 'warn') }}

WITH raw AS (
    -- error can be inferred as JSON for some retailers; cast so string comparisons work
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
        ) / nullif(count(*), 0) AS error_pct,
        100.0 * count(*) FILTER (WHERE error IS NULL AND no_data) / nullif(count(*), 0) AS empty_pct
    FROM raw
    GROUP BY retailer
)

SELECT * FROM agg WHERE error_pct >= 5.0 OR empty_pct >= 1.0
