{#- Slowly-changing price history derived from the complete set of daily facts, so it is correct
    whatever order days were loaded in. A new version starts on the first scrape date where
    price, was_price or unit_price differs from the previous scrape date. valid_to is the date the
    next version starts (exclusive); NULL for the current version. -#}
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
)

SELECT
    supermarket,
    id,
    title,
    brand,
    price,
    was_price,
    unit_price,
    is_discount,
    is_promotion,
    scraped_date AS valid_from,
    LEAD(scraped_date) OVER (PARTITION BY supermarket, id ORDER BY scraped_date) AS valid_to,
    LEAD(scraped_date) OVER (PARTITION BY supermarket, id ORDER BY scraped_date) IS NULL AS is_current
FROM versions
