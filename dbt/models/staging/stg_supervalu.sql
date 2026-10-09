WITH source AS (
    SELECT * FROM {{ source('external_source', 'supervalu') }}
    -- Dead product IDs (ID feed lags the price scrape) come back with data = NULL and exactly this
    -- error. Filter the specific string, not `error IS NOT NULL`, so real fetch failures stay
    -- visible to assert_scrape_error_rate and the null-row tests.
    WHERE NOT coalesce({{ is_stale_id_error('error') }}, false)
)

SELECT
    product_id AS id,
    'supervalu' AS supermarket,
    data.name AS title,
    data.brand AS brand,
    TRY_CAST(data.unitsOfSize.size AS DOUBLE) AS quantity,
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
    -- categories[1] is always the "Grocery" root. Ids/codes are kept for taxonomy drift checks.
    data.categories[2].category AS category_1,
    data.categories[3].category AS category_2,
    data.categories[4].category AS category_3,
    data.categories[5].category AS category_4,
    data.categories[2].categoryId AS category_id_1,
    data.categories[3].categoryId AS category_id_2,
    data.categories[4].categoryId AS category_id_3,
    data.categories[5].categoryId AS category_id_4,
    data.categories[2].retailerId AS category_code_1,
    data.categories[3].retailerId AS category_code_2,
    data.categories[4].retailerId AS category_code_3,
    data.categories[5].retailerId AS category_code_4,
    data.defaultCategory AS default_category,
    data.description AS item_description,
    data.ingredients AS ingredients,
    {{ raw_attr('data.attributes', '_allergy_advice', 'VARCHAR') }} AS allergy_advice,
    {{ raw_attr('data.attributes', 'lifestyle', 'VARCHAR[]') }} AS dietary_flags,
    -- Independent second dietary vocabulary that disagrees with lifestyle; OR'd in int_supervalu
    {{ raw_attr('data.attributes', 'dietary', 'VARCHAR[]') }} AS dietary_ext,
    {{ raw_attr('data.attributes', 'vegan', 'BOOLEAN') }} AS is_vegan,
    {{ raw_attr('data.attributes', 'vegetarian', 'BOOLEAN') }} AS is_vegetarian,
    {{ raw_attr('data.attributes', 'gluten free', 'BOOLEAN') }} AS is_gluten_free,
    -- _storage_type is a JSON string such as '{"Type":"Chilled"}'
    NULLIF(json_extract_string(json_extract_string(to_json(data.attributes), '$."_storage_type"'), '$.Type'), '') AS storage_state,
    data.nutritionProfiles['per 100g']['Total Fat'].size AS fat_per_100g,
    data.primaryImage.zoom AS image_url,
    data.available AS is_product_available,
    data.wasPrice IS NOT NULL AS is_discount,
    {{ is_multibuy_promotion('data.promotions') }} AS is_promotion,
    {{ raw_attr('data.attributes', 'own brand', 'BOOLEAN') }} AS is_own_brand,
    {{ run_date_sql() }} AS scraped_date
FROM source
