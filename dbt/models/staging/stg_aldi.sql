{{ config(materialized='view') }}

WITH source AS (
    SELECT * FROM {{ source('external_source', 'aldi') }}
)

SELECT
    data.sku as id,
    'aldi' AS supermarket,
    data.name AS title,
    data.brandName AS brand,
    data.sellingSize AS selling_size,
    data.price.amountRelevantDisplay AS price_display,
    data.price.wasPriceDisplay AS was_price_display,
    data.price.comparisonDisplay AS price_comparison_display,
    data.OnSaleDateDisplay AS sale_date_display,
    data.OnSaleDate AS sale_date,
    data.categories[1].name AS category_1,
    data.categories[2].name AS category_2,
    NULL AS category_3,
    NULL AS category_4,
    data.description AS description,
    CASE WHEN data.notForSaleReason == 'This product is currently not available.' THEN 
        FALSE ELSE TRUE END AS is_product_available,
    CASE WHEN data.price.wasPriceDisplay IS NOT NULL THEN TRUE ELSE FALSE END AS is_sale,
    CURRENT_DATE AS scraped_date
FROM source