-- fct_product_listings must contain exactly the rows of the retailer models built this run
-- (var retailers; all four on a normal day).
WITH expected AS (
    SELECT
{%- for retailer in var('retailers') %}
        {{ '+ ' if not loop.first }}(SELECT count(*) FROM {{ ref('int_' ~ retailer) }})
{%- endfor %} AS n
),

actual AS (
    SELECT count(*) AS n FROM {{ ref('fct_product_listings') }}
)

SELECT expected.n AS expected_rows, actual.n AS actual_rows
FROM expected, actual
WHERE expected.n <> actual.n
