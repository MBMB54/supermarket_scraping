{{ config(materialized='table') }}

SELECT
    data.sku as id,
    data.name AS title,
    data.brand AS brand,
    data.unitsOfSize.size AS quantity,
    data.unitsOfSize.abbreviation AS unit,
    COALESCE(data.wasPrice, data.price) AS price,
    data.promotions.startDate AS promotion_start_date,
    data.promotions.endDate AS promotion_end_date,
    data.unitPrice AS unit_price,
    data.unitsOfSize.size AS unit_of_measure,
    CASE WHEN data.wasPrice IS NOT NULL THEN data.price ELSE NULL END AS promotion_price,
    data.categories[2].category AS department,
    data.categories[3].category AS sub_department,
    data.categories[4].category AS aisle,
    data.categories[4].category AS shelf,
    data.description AS description,
    CURRENT_DATE AS scraped_date
FROM {{ source('external_source', 'supervalu') }}