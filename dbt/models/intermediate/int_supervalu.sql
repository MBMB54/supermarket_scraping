{{ config(materialized='table') }}

WITH stg AS (
    SELECT * FROM {{ ref('stg_supervalu') }}
),

normalised AS (
    SELECT
        *,
        {{ normalize_text('title') }} AS title_norm,
        {{ unit_qty_normalised('unit', 'quantity') }} AS unit_qty_normalised
    FROM stg
)

SELECT
    id::VARCHAR AS id,
    supermarket::VARCHAR AS supermarket,
    title_norm::VARCHAR AS title,
    {{ clean_title('title') }}::VARCHAR AS title_cleaned,
    {{ normalize_text('brand') }}::VARCHAR AS brand,
    is_own_brand::BOOLEAN AS is_own_brand,
    item_description::VARCHAR AS item_description,
    {{ normalize_category('category_1') }}::VARCHAR AS category_1,
    {{ normalize_category('category_2') }}::VARCHAR AS category_2,
    {{ normalize_category('category_3') }}::VARCHAR AS category_3,
    {{ normalize_category('category_4') }}::VARCHAR AS category_4,
    is_discount::BOOLEAN AS is_discount,
    is_promotion::BOOLEAN AS is_promotion,
    price::DOUBLE AS price,
    was_price::DOUBLE AS was_price,
    quantity::DOUBLE AS selling_size,
    unit::VARCHAR AS unit,
    unit_qty_normalised::DOUBLE AS unit_qty_normalised,
    {{ unit_normalised('unit') }}::VARCHAR AS unit_normalised,
    unit_price::DOUBLE AS unit_price,
    {{ price_per_unit_normalised('price', 'unit_qty_normalised') }}::DOUBLE AS price_per_unit_normalised,
    promotion_description::VARCHAR AS promotion_description,
    promotion_type::VARCHAR AS promotion_type,
    CAST(NULL AS VARCHAR[]) AS promotion_qualities,
    promotion_start_date::DATE AS promotion_start_date,
    promotion_end_date::DATE AS promotion_end_date,
    discount_start_date::DATE AS discount_start_date,
    discount_end_date::DATE AS discount_end_date,
    ingredients::VARCHAR AS ingredients,
    {{ allergens_from_json_map('allergy_advice', 'contains') }} AS contains_allergens,
    {{ allergens_from_json_map('allergy_advice', 'may_contain') }} AS may_contain_allergens,
    dietary_flags::VARCHAR[] AS dietary_flags,
    -- attributes.vegan / .vegetarian and the lifestyle tags disagree often; OR them to avoid false negatives
    (coalesce(is_vegan, false) OR {{ has_flag('dietary_flags', 'Suitable for Vegans') }})::BOOLEAN AS is_vegan,
    (coalesce(is_vegetarian, false) OR {{ has_flag('dietary_flags', 'Suitable for Vegetarians') }})::BOOLEAN AS is_vegetarian,
    -- Four independent sources for the same claim (attribute, lifestyle, dietary, coeliac tag)
    (
        coalesce(is_gluten_free, false)
        OR {{ has_flag('dietary_flags', 'Gluten free') }}
        OR {{ has_flag('dietary_flags', 'Suitable for Coeliacs') }}
        OR {{ has_flag('dietary_ext', 'Gluten Free') }}
    )::BOOLEAN AS is_gluten_free,
    -- Category-path membership is OR'd in so dissolving organic buckets in the taxonomy
    -- consolidation cannot lose organic-ness (lifestyle tag misses ~12%)
    (
        {{ has_flag('dietary_flags', 'Organic') }}
        OR coalesce(category_2 ILIKE '%organic%', false)
        OR coalesce(category_3 ILIKE '%organic%', false)
        OR coalesce(category_4 ILIKE '%organic%', false)
    )::BOOLEAN AS is_organic,
    -- Skimmed Milk is deliberately not a low-fat category: it survives as its own product type
    (
        {{ has_flag('dietary_flags', 'Low Fat') }}
        OR {{ low_fat_title_claim('title_norm') }}
        OR {{ low_fat_nutrition_panel('fat_per_100g') }}
        OR coalesce(category_3 IN ('Low Fat & Fat Free', 'Low Fat Milk'), false)
    )::BOOLEAN AS is_low_fat,
    list_contains(dietary_flags, 'Kosher')::BOOLEAN AS is_kosher,
    list_contains(dietary_flags, 'Halal')::BOOLEAN AS is_halal,
    -- Dairy-free / sugar-free / diabetic attributes are shelved as categories that the taxonomy
    -- consolidation dissolves, so they are preserved here first. SuperValu-only.
    (
        coalesce(category_2 IN ('Lactose & Dairy Free', 'Dairy & Lactose Free'), false)
        OR coalesce(category_3 = 'Dairy Free', false)
        OR {{ has_flag('dietary_flags', 'Suitable for Sufferers of Lactose Intolerance') }}
    )::BOOLEAN AS is_dairy_free,
    (coalesce(category_3 = 'Sugar Free', false) OR title_norm LIKE '%sugar free%')::BOOLEAN AS is_sugar_free,
    -- A "no added sugar" claim, distinct from is_sugar_free
    (
        {{ has_flag('dietary_flags', 'No Added Sugar') }}
        OR {{ has_flag('dietary_flags', 'No Sugar') }}
        OR {{ has_flag('dietary_flags', 'Low in sugar') }}
    )::BOOLEAN AS has_no_added_sugar,
    {{ has_flag('dietary_ext', 'Suitable for Diabetics') }}::BOOLEAN AS is_diabetic_friendly,
    image_url::VARCHAR AS image_url,
    is_product_available::BOOLEAN AS is_product_available,
    scraped_date::DATE AS scraped_date
FROM normalised
