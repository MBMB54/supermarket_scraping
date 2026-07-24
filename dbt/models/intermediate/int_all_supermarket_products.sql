{{ config(
    materialized='external',
    location='s3://ie-supermarket-data/processed/all_supermarket_products.parquet'
) }}

SELECT id, supermarket, title, title_cleaned FROM {{ ref('int_tesco') }}     WHERE title IS NOT NULL
UNION ALL
SELECT id, supermarket, title, title_cleaned FROM {{ ref('int_dunnes') }}    WHERE title IS NOT NULL
UNION ALL
SELECT id, supermarket, title, title_cleaned FROM {{ ref('int_supervalu') }} WHERE title IS NOT NULL
UNION ALL
SELECT id, supermarket, title, title_cleaned FROM {{ ref('int_aldi') }}      WHERE title IS NOT NULL
