{{ config(materialized='view') }}

SELECT
    data.sku as id,
    data.name AS title,
    data.brandName AS brand,
    regexp_replace(data.sellingSize,'[a-zA-Z]+','') AS quantity,
    regexp_extract(data.sellingSize,'[a-zA-Z]+') AS unit,
    regexp_replace(data.price.amountRelevantDisplay,'€','') AS price,
    --data.promotions.startDate AS promotion_start_date,
    --data.promotions.endDate AS promotion_end_date,
    data.price.comparisonDisplay AS unit_price,
    data.categories[1].name AS department,
    data.categories[2].name AS sub_department,
    data.description AS description,
    CURRENT_DATE AS scraped_date
FROM {{ source('external_source', 'aldi') }}