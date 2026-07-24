{{ config(materialized='view') }}

WITH source AS (
    SELECT * FROM {{ source('external_source', 'supervalu') }}
)

SELECT
    product_id AS id,
    'supervalu' AS supermarket,
    data.name AS title,
    data.brand AS brand,
    data.unitsOfSize.size AS quantity,
    NULLIF(LOWER(data.unitsOfSize.abbreviation), '') AS unit,
    TRY_CAST(regexp_replace(data.unitPrice, '[^0-9.]', '', 'g') AS FLOAT) AS unit_price,
    TRY_CAST(regexp_replace(data.wasUnitPrice, '[^0-9.]', '', 'g') AS FLOAT) AS was_unit_price,
    LOWER(data.unitOfMeasure.abbreviation) AS unit_of_measure,
    -- SuperValu API returns the effective (already-discounted) price; was_price is original when discounted
    TRY_CAST(regexp_replace(data.price, '[^0-9.]', '', 'g') AS FLOAT) AS price,
    TRY_CAST(regexp_replace(data.wasPrice, '[^0-9.]', '', 'g') AS FLOAT) AS was_price,
    TRY_CAST(json_extract_string(data.promotions, '$[0].startDateUtc') AS DATE) AS promotion_start_date,
    TRY_CAST(json_extract_string(data.promotions, '$[0].endDateUtc') AS DATE) AS promotion_end_date,
    -- tprInfo covers standalone TPR price cuts; dates arrive in DD/MM/YYYY format
    CAST(TRY_STRPTIME(data.tprInfo.effectiveFrom, '%d/%m/%Y') AS DATE) AS discount_start_date,
    CAST(TRY_STRPTIME(data.tprInfo.effectiveUntil, '%d/%m/%Y') AS DATE) AS discount_end_date,
    json_extract_string(data.promotions, '$[0].name') AS promotion_description,
    json_extract_string(data.promotions, '$[0].promotionType') AS promotion_type,
    data.categories[2].category AS category_1,
    data.categories[3].category AS category_2,
    data.categories[4].category AS category_3,
    data.categories[5].category AS category_4,
    data.description AS item_description,
    data.ingredients AS ingredients,
    data.attributes._allergy_advice AS allergy_advice,
    data.attributes.lifestyle AS dietary_flags,
    data.attributes.vegan AS is_vegan,
    data.attributes.vegetarian AS is_vegetarian,
    data.attributes['gluten free'] AS is_gluten_free,
    data.primaryImage.zoom AS image_url,
    data.available AS is_product_available,
    -- is_discount: wasPrice populated whenever a per-unit price cut applies (TPR or ProductPromotion)
    data.wasPrice IS NOT NULL AS is_discount,
    -- is_promotion: multibuy deals (BulkPromotion, BundlePromotion); can be TRUE alongside is_discount
    CASE
        WHEN json_array_length(data.promotions) = 0 OR data.promotions IS NULL THEN FALSE
        WHEN json_extract_string(data.promotions, '$[0].promotionType') IN ('BulkPromotion', 'BundlePromotion', 'Custom') THEN TRUE
        ELSE FALSE
    END AS is_promotion,
    data.attributes['own brand'] AS is_own_brand,
    CURRENT_DATE AS scraped_date
FROM source
