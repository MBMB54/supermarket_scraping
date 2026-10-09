-- Structural checks on fct_price_history: exactly one current version per product, versions do not
-- overlap, last_seen_date is the same across a product's versions, and is_active matches the
-- retailer's latest loaded day (recomputed from the daily facts).
{% set grace = var('active_grace_days', 0) %}

WITH per_key AS (
    SELECT
        supermarket,
        id,
        count(*) FILTER (WHERE is_current) AS current_rows,
        count(DISTINCT last_seen_date) AS last_seen_values,
        count(DISTINCT is_active) AS active_values,
        count(*) FILTER (WHERE valid_to IS NOT NULL AND valid_from >= valid_to) AS bad_ranges,
        count(*) FILTER (WHERE is_current <> (valid_to IS NULL)) AS bad_current_flag,
        max(last_seen_date) AS last_seen_date,
        bool_or(is_active) AS is_active
    FROM {{ ref('fct_price_history') }}
    GROUP BY supermarket, id
),

latest AS (
    SELECT supermarket, max(scraped_date) AS latest_scraped_date
    FROM {{ ref('fct_product_price_daily') }}
    GROUP BY supermarket
)

SELECT k.*
FROM per_key AS k
INNER JOIN latest USING (supermarket)
WHERE current_rows <> 1
   OR last_seen_values <> 1
   OR active_values <> 1
   OR bad_ranges > 0
   OR bad_current_flag > 0
   OR is_active <> (k.last_seen_date >= latest.latest_scraped_date - INTERVAL {{ grace }} DAY)
