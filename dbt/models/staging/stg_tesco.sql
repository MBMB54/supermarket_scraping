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
    {{ utc_to_local_date('data.promotions[1].startDate') }} AS promotion_start_date,
    {{ utc_to_local_date('data.promotions[1].endDate') }} AS promotion_end_date,
    data.promotions[1].description AS promotion_description,
    data.promotions[1].attributes[1] AS promotion_type,
    data.promotions[1].qualities AS promotion_qualities,
    data.superDepartmentName AS category_1,
    data.departmentName AS category_2,
    data.aisleName AS category_3,
    data.shelfName AS category_4,
    data.description AS item_description,
    data.details.ingredients AS ingredients,
    data.details.allergenInfo AS allergen_info,
    data.foodIcons AS dietary_flags,
    -- Superset of foodIcons: also carries organic certification marks that foodIcons misses
    list_transform(data.icons, x -> x.caption) AS certification_flags,
    data.details.nutritionalClaims AS nutritional_claims,
    data.media.defaultImage.url AS image_url,
    NOT (data.title IS NULL AND data.promotions[1].metaData.seo.afterDiscountPrice IS NOT NULL)
        AS is_product_available,
    -- price_cut: straight per-unit discount; multibuy: 3-for-2, 2-for-EUR5, meal deals
    COALESCE(list_contains(data.promotions[1].qualities, 'price_cut'), FALSE) AS is_discount,
    COALESCE(list_contains(data.promotions[1].qualities, 'multibuy'), FALSE) AS is_promotion,
    {{ run_date_sql() }} AS scraped_date
FROM source
