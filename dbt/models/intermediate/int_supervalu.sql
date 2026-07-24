{{ config(materialized='view') }}

WITH cleaned AS (
SELECT
    id,
    supermarket,
    {{ normalize_text('title') }} AS title,
    {{ clean_title('title') }} AS title_cleaned,
    {{ normalize_text('brand') }} AS brand,
    category_1,
    category_2,
    category_3,
    category_4,
    is_discount,
    is_promotion,
    -- SuperValu API already returns the effective price in price; was_price is original when discounted
    price,
    was_price,
    promotion_start_date,
    promotion_end_date,
    discount_start_date,
    discount_end_date,
    promotion_description,
    promotion_type,
    unit_price,
    quantity AS selling_size,
    unit,
    CASE
            WHEN unit = 'g'      THEN quantity / 1000.0
            WHEN unit = 'kg'     THEN quantity
            WHEN unit = 'mg'     THEN quantity / 1000.0
            WHEN unit = 'ml'     THEN quantity / 1000.0
            WHEN unit = 'l'      THEN quantity
            WHEN unit = 'm'      THEN quantity
            WHEN unit IN ('litre', 'litres') THEN quantity
            WHEN unit = 'cl'     THEN quantity / 100.0
            ELSE 1.0
        END AS unit_qty_normalised,
    CASE unit
            WHEN 'g'  THEN 'kg'
            WHEN 'kg' THEN 'kg'
            WHEN 'ml' THEN 'l'
            WHEN 'mg' THEN 'l'
            WHEN 'l'  THEN 'l'
            WHEN 'litres' THEN 'l'
            WHEN 'litre' THEN 'l'
            WHEN 'cl' THEN 'l'
            WHEN 'm'  THEN 'm'
            ELSE 'each'
        END AS unit_normalised,
    ingredients,
    allergy_advice,
    dietary_flags,
    is_vegan,
    is_vegetarian,
    is_gluten_free,
    image_url,
    is_product_available,
    is_own_brand,
    item_description,
    scraped_date
FROM {{ ref('stg_supervalu') }}
)

SELECT
    id::VARCHAR AS id,
    supermarket::VARCHAR AS supermarket,
    title::VARCHAR AS title,
    title_cleaned::VARCHAR AS title_cleaned,
    brand::VARCHAR AS brand,
    is_own_brand::BOOLEAN AS is_own_brand,
    item_description::VARCHAR AS item_description,
    category_1::VARCHAR AS category_1,
    category_2::VARCHAR AS category_2,
    category_3::VARCHAR AS category_3,
    category_4::VARCHAR AS category_4,
    is_discount::BOOLEAN AS is_discount,
    is_promotion::BOOLEAN AS is_promotion,
    price::DOUBLE AS price,
    was_price::DOUBLE AS was_price,
    selling_size::DOUBLE AS selling_size,
    unit::VARCHAR AS unit,
    unit_qty_normalised::DOUBLE AS unit_qty_normalised,
    unit_normalised::VARCHAR AS unit_normalised,
    unit_price::DOUBLE AS unit_price,
    ROUND(price::DOUBLE / NULLIF(unit_qty_normalised::DOUBLE, 0), 2) AS price_per_unit_normalised,
    promotion_description::VARCHAR AS promotion_description,
    promotion_type::VARCHAR AS promotion_type,
    CAST(NULL AS VARCHAR[]) AS promotion_qualities,
    promotion_start_date::DATE AS promotion_start_date,
    promotion_end_date::DATE AS promotion_end_date,
    discount_start_date::DATE AS discount_start_date,
    discount_end_date::DATE AS discount_end_date,
    ingredients::VARCHAR AS ingredients,
    list_filter(
        json_keys(allergy_advice),
        k -> json_extract_string(allergy_advice, '$.' || k) = 'Contains'
    )::VARCHAR[] AS contains_allergens,
    list_filter(
        json_keys(allergy_advice),
        k -> json_extract_string(allergy_advice, '$.' || k) LIKE '%May Contain%'
    )::VARCHAR[] AS may_contain_allergens,
    dietary_flags::VARCHAR[] AS dietary_flags,
    is_vegan::BOOLEAN AS is_vegan,
    is_vegetarian::BOOLEAN AS is_vegetarian,
    is_gluten_free::BOOLEAN AS is_gluten_free,
    list_contains(dietary_flags, 'Organic')::BOOLEAN AS is_organic,
    CAST(NULL AS BOOLEAN) AS is_low_fat,
    list_contains(dietary_flags, 'Kosher')::BOOLEAN AS is_kosher,
    list_contains(dietary_flags, 'Halal')::BOOLEAN AS is_halal,
    image_url::VARCHAR AS image_url,
    is_product_available::BOOLEAN AS is_product_available,
    scraped_date::DATE AS scraped_date
FROM cleaned
