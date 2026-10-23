{{ config(
    materialized='external',
    location=var('parquet_output_prefix') ~ '/all_supermarket_products.parquet'
) }}

SELECT id, supermarket, title, title_cleaned, image_url
FROM {{ ref('fct_product_listings') }}
WHERE title IS NOT NULL
