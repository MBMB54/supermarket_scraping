WITH source AS (
    SELECT * FROM {{ source('external_source', 'dunnes') }}
    -- Same stale-ID pattern and rationale as stg_supervalu (same storefront backend).
    WHERE error IS DISTINCT FROM 'Not found in any store'
)

SELECT
    product_id AS id,
    'dunnes' AS supermarket,
    data.name AS title,
    data.brand AS brand,
    data.unitsOfSize.size AS quantity,
    NULLIF(LOWER(data.unitsOfSize.abbreviation), '') AS unit,
    {{ parse_price('data.unitPrice') }} AS unit_price,
    {{ parse_price('data.wasUnitPrice') }} AS was_unit_price,
    LOWER(data.unitOfMeasure.abbreviation) AS unit_of_measure,
    -- price is already the effective (discounted) price; was_price is the original
    {{ parse_price('data.price') }} AS price,
    {{ parse_price('data.wasPrice') }} AS was_price,
    {{ utc_to_local_date(first_promotion_field('data.promotions', 'startDateUtc')) }} AS promotion_start_date,
    {{ utc_to_local_date(first_promotion_field('data.promotions', 'endDateUtc')) }} AS promotion_end_date,
    {{ parse_dd_mm_yyyy('data.tprInfo.effectiveFrom') }} AS discount_start_date,
    {{ parse_dd_mm_yyyy('data.tprInfo.effectiveUntil') }} AS discount_end_date,
    {{ first_promotion_field('data.promotions', 'name') }} AS promotion_description,
    {{ first_promotion_field('data.promotions', 'promotionType') }} AS promotion_type,
    data.categories[2].category AS category_1,
    data.categories[3].category AS category_2,
    data.categories[4].category AS category_3,
    CAST(NULL AS VARCHAR) AS category_4,
    -- Ingredients, allergy advice and lifestyle tags only exist as HTML in this field
    data.description AS item_description,
    data.attributes['gluten free'] AS is_gluten_free,
    data.attributes.vegetarian AS is_vegetarian,
    data.attributes.vegan AS is_vegan,
    data.attributes.organic AS is_organic,
    data.attributes.lowfat AS is_low_fat,
    data.attributes.other AS other_dietary,
    data.nutritionProfiles['per 100g']['Total Fat'].size AS fat_per_100g,
    data.primaryImage.zoom AS image_url,
    data.available AS is_product_available,
    data.wasPrice IS NOT NULL AS is_discount,
    {{ is_multibuy_promotion('data.promotions') }} AS is_promotion,
    data.attributes.OwnBrand AS is_own_brand,
    {{ run_date_sql() }} AS scraped_date
FROM source
WHERE data.name IS NOT NULL
