-- For deep discounts (>= 10% off) on kg/l items, a unit price that matches the old regular price
-- instead of the effective price is stale. Warns when more than 5% of such rows are stale for a
-- retailer (Tesco's raw figure was; the staging fix rescales it). Needs fct_product_listings.
{{ config(severity = 'warn') }}

WITH discounted AS (
    SELECT
        supermarket,
        unit_price * unit_qty_normalised / price AS ratio_effective,
        unit_price * unit_qty_normalised / was_price AS ratio_regular
    FROM {{ ref('fct_product_listings') }}
    WHERE was_price > price * 1.1
      AND price > 0
      AND unit_price IS NOT NULL
      AND unit_normalised IN ('kg', 'l')
      AND unit_qty_normalised > 0
)

SELECT
    supermarket,
    count(*) AS discounted_rows,
    count(*) FILTER (
        WHERE abs(ratio_regular - 1) < 0.03 AND abs(ratio_effective - 1) > 0.05
    ) AS stale_unit_price_rows
FROM discounted
GROUP BY supermarket
HAVING count(*) FILTER (
    WHERE abs(ratio_regular - 1) < 0.03 AND abs(ratio_effective - 1) > 0.05
) > 0.05 * count(*)
