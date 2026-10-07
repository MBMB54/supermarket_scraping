WITH stg AS (
    SELECT * FROM {{ ref('stg_tesco') }}
),

normalised AS (
    SELECT
        *,
        {{ normalize_text('title') }} AS title_norm,
        {{ unit_qty_normalised('unit', 'quantity') }} AS unit_qty_normalised,
        -- price is the current effective price: only a price_cut discount lowers it. Multibuy
        -- promos (3-for-2, ...) leave the unit price unchanged.
        CASE WHEN is_discount AND promotion_price IS NOT NULL
             THEN promotion_price ELSE price END AS effective_price,
        CASE WHEN is_discount AND promotion_price IS NOT NULL
             THEN price ELSE NULL END AS original_price
    FROM stg
)

SELECT
    id::VARCHAR AS id,
    supermarket::VARCHAR AS supermarket,
    title_norm::VARCHAR AS title,
    {{ clean_title('title') }} AS title_cleaned,
    {{ normalize_text('brand') }}::VARCHAR AS brand,
    CAST(NULL AS BOOLEAN) AS is_own_brand,
    item_description::VARCHAR AS item_description,
    {{ normalize_category('category_1') }}::VARCHAR AS category_1,
    {{ normalize_category('category_2') }}::VARCHAR AS category_2,
    {{ normalize_category('category_3') }}::VARCHAR AS category_3,
    {{ normalize_category('category_4') }}::VARCHAR AS category_4,
    is_discount::BOOLEAN AS is_discount,
    is_promotion::BOOLEAN AS is_promotion,
    effective_price::DOUBLE AS price,
    original_price::DOUBLE AS was_price,
    quantity::DOUBLE AS selling_size,
    unit::VARCHAR AS unit,
    unit_qty_normalised::DOUBLE AS unit_qty_normalised,
    {{ unit_normalised('unit') }}::VARCHAR AS unit_normalised,
    unit_price::DOUBLE AS unit_price,
    {{ price_per_unit_normalised('effective_price', 'unit_qty_normalised') }}::DOUBLE AS price_per_unit_normalised,
    promotion_description::VARCHAR AS promotion_description,
    promotion_type::VARCHAR AS promotion_type,
    promotion_qualities::VARCHAR[] AS promotion_qualities,
    promotion_start_date::DATE AS promotion_start_date,
    promotion_end_date::DATE AS promotion_end_date,
    CAST(NULL AS DATE) AS discount_start_date,
    CAST(NULL AS DATE) AS discount_end_date,
    array_to_string(
        list_filter(
            list_transform(ingredients, x -> trim(regexp_replace(x, '<[^>]+>', '', 'g'))),
            x -> length(x) > 0
        ),
        ', '
    )::VARCHAR AS ingredients,
    {{ allergens_from_structs('allergen_info', 'contains') }} AS contains_allergens,
    {{ allergens_from_structs('allergen_info', 'may_contain') }} AS may_contain_allergens,
    dietary_flags::VARCHAR[] AS dietary_flags,
    -- foodIcons is a clean subset of the icon badges for these tags, so a single source is enough
    list_contains(dietary_flags, 'Suitable for Vegans') AS is_vegan,
    list_contains(dietary_flags, 'Suitable for Vegetarians') AS is_vegetarian,
    list_contains(dietary_flags, 'Gluten free') AS is_gluten_free,
    -- Organic certification marks only appear in the badge captions, not in foodIcons
    (
        {{ has_flag('dietary_flags', 'Organic') }}
        OR coalesce(len(list_filter(certification_flags, x -> x ILIKE '%organic%')) > 0, false)
    )::BOOLEAN AS is_organic,
    -- No nutrition-panel threshold here (unlike SuperValu): Tesco's panel is free text and a
    -- 3g/100g cut-off flags plenty of items not marketed as low fat. "low in saturated fat"
    -- claims are excluded on purpose.
    (
        {{ has_flag('dietary_flags', 'Low Fat') }}
        OR title_norm LIKE '%low fat%'
        OR {{ low_fat_title_claim('title_norm') }}
        OR coalesce(
            len(list_filter(
                nutritional_claims,
                x -> lower(x) LIKE '%low fat%' OR lower(x) LIKE '%fat free%' OR lower(x) LIKE '%low in fat%'
            )) > 0,
            false
        )
    )::BOOLEAN AS is_low_fat,
    list_contains(dietary_flags, 'Kosher') AS is_kosher,
    list_contains(dietary_flags, 'Halal') AS is_halal,
    CAST(NULL AS BOOLEAN) AS is_dairy_free,
    CAST(NULL AS BOOLEAN) AS is_sugar_free,
    CAST(NULL AS BOOLEAN) AS has_no_added_sugar,
    CAST(NULL AS BOOLEAN) AS is_diabetic_friendly,
    image_url::VARCHAR AS image_url,
    is_product_available::BOOLEAN AS is_product_available,
    scraped_date::DATE AS scraped_date
FROM normalised
