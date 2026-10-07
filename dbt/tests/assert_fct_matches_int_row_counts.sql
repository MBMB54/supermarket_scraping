-- fct_product_listings must contain exactly the rows of the four retailer models.
WITH expected AS (
    SELECT
        (SELECT count(*) FROM {{ ref('int_tesco') }})
        + (SELECT count(*) FROM {{ ref('int_dunnes') }})
        + (SELECT count(*) FROM {{ ref('int_supervalu') }})
        + (SELECT count(*) FROM {{ ref('int_aldi') }}) AS n
),

actual AS (
    SELECT count(*) AS n FROM {{ ref('fct_product_listings') }}
)

SELECT expected.n AS expected_rows, actual.n AS actual_rows
FROM expected, actual
WHERE expected.n <> actual.n
