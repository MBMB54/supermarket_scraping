-- Early-warning monitor on the raw scrape (warn only). Thresholds mirror the scraper gate in
-- docs/scraper_data_quality.md; this fires at a quarter of the gate's error limit so a trend is
-- visible before the Batch job itself fails. Scans one raw partition per retailer in var retailers: run on the daily build.
{{ config(severity = 'warn') }}

WITH raw AS (
    -- error can be inferred as JSON for some retailers; cast so string comparisons work
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
        ) / nullif(count(*), 0) AS error_pct,
        100.0 * count(*) FILTER (WHERE error IS NULL AND no_data) / nullif(count(*), 0) AS empty_pct,
        100.0 * count(*) FILTER (WHERE {{ is_stale_id_error('error') }}) / nullif(count(*), 0) AS not_found_pct
    FROM raw
    GROUP BY retailer
)

-- not_found_pct is dropped in staging, so it is only visible here: Tesco's and Aldi's ID lists are small
-- and refreshed often, so more than 5% stale means the ID feed has lagged (or the API is returning
-- false 404s). SuperValu and Dunnes legitimately run far higher (see the hard gate's limits).
SELECT * FROM agg
WHERE error_pct >= 5.0
   OR empty_pct >= 1.0
   OR (retailer IN ('tesco', 'aldi') AND not_found_pct >= 5.0)
