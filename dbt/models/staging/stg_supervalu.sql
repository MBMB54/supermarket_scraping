{{ config(materialized='view') }}

WITH source AS (
    SELECT * FROM {{ source('external_source', 'supervalu') }}
)

SELECT
    product_id as id,
    data.name AS title,
    data.brand AS brand,
    data['attributes']['own brand'] AS is_own_brand,
    data.promotions,
    CASE WHEN data.promotions IS NOT NULL THEN TRUE ELSE FALSE END AS is_promotion,
    data.price AS price,
    data.unitPrice AS unit_price,
    data.wasPrice AS was_price,
    data.wasUnitPrice AS was_unit_price,
    data.unitsOfSize.size AS quantity,
    data.unitsOfSize.abbreviation AS unit,
    TRY_CAST(data.tprInfo.effectiveFromDate AS DATE) AS promotion_start_date,
    TRY_CAST(data.tprInfo.effectiveUntilDate AS DATE) AS promotion_end_date,
    CASE WHEN data.wasPrice IS NOT NULL THEN data.price ELSE NULL END AS promotion_price,
    data.categories[2].category AS category_1,
    data.categories[3].category AS category_2,
    data.categories[4].category AS category_3,
    data.categories[4].category AS category_4,
    data.ingredients AS ingredients_raw,
    data.primaryImage.zoom AS image_url,
    data.attributes.lifestyle AS dietary_flags,
    data.attributes._allergy_advice AS allergens,
    data.description AS description,
    CURRENT_DATE AS scraped_date
FROM source