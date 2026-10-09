{#- Slowly-changing price history derived from the complete set of daily facts, so it is correct
    whatever order days were loaded in. A new version starts on the first scrape date where
    price, was_price or unit_price differs from the previous scrape date. valid_to is the date the
    next version starts (exclusive); NULL for the latest version (is_current).

    last_seen_date is the product's latest scrape date (the same on every version row). is_active
    compares it with the latest scrape date loaded for the SAME retailer, so one retailer's missed
    run does not mark the others inactive. active_grace_days (default 0) tolerates that many missed
    days: a failed fetch leaves no daily row, so a product can vanish for a day and come back.
    Live prices: WHERE is_current AND is_active. -#}
{% set grace = var('active_grace_days', 0) %}

WITH daily AS (
    SELECT
        *,
        LAG(price) OVER w AS prev_price,
        LAG(was_price) OVER w AS prev_was_price,
        LAG(unit_price) OVER w AS prev_unit_price,
        LAG(scraped_date) OVER w AS prev_date
    FROM {{ ref('fct_product_price_daily') }}
    WINDOW w AS (PARTITION BY supermarket, id ORDER BY scraped_date)
),

versions AS (
    SELECT *
    FROM daily
    WHERE prev_date IS NULL
       OR price IS DISTINCT FROM prev_price
       OR was_price IS DISTINCT FROM prev_was_price
       OR unit_price IS DISTINCT FROM prev_unit_price
),

last_seen AS (
    SELECT supermarket, id, MAX(scraped_date) AS last_seen_date
    FROM {{ ref('fct_product_price_daily') }}
    GROUP BY supermarket, id
),

latest_load AS (
    SELECT supermarket, MAX(scraped_date) AS latest_scraped_date
    FROM {{ ref('fct_product_price_daily') }}
    GROUP BY supermarket
)

SELECT
    v.supermarket::VARCHAR AS supermarket,
    v.id::VARCHAR AS id,
    v.title::VARCHAR AS title,
    v.brand::VARCHAR AS brand,
    v.price::DOUBLE AS price,
    v.was_price::DOUBLE AS was_price,
    v.unit_price::DOUBLE AS unit_price,
    v.is_discount::BOOLEAN AS is_discount,
    v.is_promotion::BOOLEAN AS is_promotion,
    v.scraped_date::DATE AS valid_from,
    LEAD(v.scraped_date) OVER (PARTITION BY v.supermarket, v.id ORDER BY v.scraped_date)::DATE AS valid_to,
    (LEAD(v.scraped_date) OVER (PARTITION BY v.supermarket, v.id ORDER BY v.scraped_date) IS NULL)::BOOLEAN AS is_current,
    s.last_seen_date::DATE AS last_seen_date,
    (s.last_seen_date >= l.latest_scraped_date - INTERVAL {{ grace }} DAY)::BOOLEAN AS is_active
FROM versions AS v
INNER JOIN last_seen AS s USING (supermarket, id)
INNER JOIN latest_load AS l USING (supermarket)
