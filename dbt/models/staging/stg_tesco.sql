{{ config(materialized='view') }}

WITH source AS (
    SELECT * FROM {{ source('external_source', 'tesco') }}
)

SELECT
    tpnc AS id,
    'tesco' AS supermarket,
    data.title AS title,
    data.brandName AS brand,
    CAST(data.details.packSize[1].value AS FLOAT) AS quantity,
    LOWER(data.details.packSize[1].units) AS unit,
    data.price.unitPrice AS unit_price,
    LOWER(data.price.unitOfMeasure) AS unit_of_measure,
    data.price.actual AS price,
    data.promotions[1].metaData.seo.afterDiscountPrice AS promotion_price,
    data.promotions[1].price.beforeDiscount AS price_before_discount,
    data.promotions[1].price.afterDiscount AS price_after_discount,
    TRY_CAST(data.promotions[1].startDate AS DATE) AS promotion_start_date,
    TRY_CAST(data.promotions[1].endDate AS DATE) AS promotion_end_date,
    data.promotions[1].description AS promotion_description,
    data.promotions[1].attributes[1] AS promotion_type,
    data.promotions[1].qualities AS promotion_qualities,
    data.superDepartmentName AS category_1,
    data.departmentName AS category_2,
    data.aisleName AS category_3,
    data.shelfName AS category_4,
    data.description AS item_description,
    data.details.ingredients AS ingredients,
    data.foodIcons AS dietary_flags,
    data.media.defaultImage.url AS image_url,
    CASE WHEN data.title IS NULL AND data.promotions[1].metaData.seo.afterDiscountPrice IS NOT NULL THEN
        FALSE ELSE TRUE END AS is_product_available,
    -- price_cut = straight per-unit discount (incl. clearance); multibuy = 3-for-2 / 2-for-€5 / bogof / meal deals
    COALESCE(list_contains(data.promotions[1].qualities, 'price_cut'), FALSE) AS is_discount,
    COALESCE(list_contains(data.promotions[1].qualities, 'multibuy'), FALSE) AS is_promotion,
    CURRENT_DATE AS scraped_date
FROM source