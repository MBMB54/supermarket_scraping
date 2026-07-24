{{ config(materialized='view') }}

WITH cleaned AS (
SELECT
    id,
    supermarket,
    {{ normalize_text('title') }} AS title,
    {{ normalize_text('brand') }} AS brand,
    item_description,
    category_1,
    category_2,
    category_3,
    category_4,
    is_discount,
    is_promotion,
    price AS regular_price,
    promotion_price AS promotional_price,
    promotion_start_date,
    promotion_end_date,
    promotion_description,
    promotion_type,
    promotion_qualities,
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
    -- keep the raw HTML for ingredients text (still stripped of tags in the final select)
    ingredients AS ingredients_raw,
    -- structured allergen data: list of {name, values} structs, e.g. {"name": "Contains", "values": ["Milk"]}
    allergen_info,
    dietary_flags,
    image_url,
    is_product_available,
    scraped_date
FROM {{ ref('stg_tesco') }}
),

priced AS (
SELECT
    *,
    -- price = current effective single-unit price, discounted when there is a price_cut.
    -- multibuy promos (3-for-2 etc.) don't change the unit price, so price stays regular.
    CASE WHEN is_discount AND promotional_price IS NOT NULL
         THEN promotional_price ELSE regular_price END AS price,
    -- was_price = original price, only populated when a discount applies
    CASE WHEN is_discount AND promotional_price IS NOT NULL
         THEN regular_price ELSE NULL END AS was_price
FROM cleaned
)

SELECT
    id::VARCHAR AS id,
    supermarket::VARCHAR AS supermarket,
    title::VARCHAR AS title,
    {{ clean_title('title') }} AS title_cleaned,
    brand::VARCHAR AS brand,
    CAST(NULL AS BOOLEAN) AS is_own_brand,
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
    ROUND(price / NULLIF(unit_qty_normalised, 0), 2)::DOUBLE AS price_per_unit_normalised,
    promotion_description::VARCHAR AS promotion_description,
    promotion_type::VARCHAR AS promotion_type,
    promotion_qualities::VARCHAR[] AS promotion_qualities,
    promotion_start_date::DATE AS promotion_start_date,
    promotion_end_date::DATE AS promotion_end_date,
    CAST(NULL AS DATE) AS discount_start_date,
    CAST(NULL AS DATE) AS discount_end_date,
    -- cleaned ingredients: HTML stripped + trimmed per element, empties removed, joined into one string
    array_to_string(
        list_filter(
            list_transform(ingredients_raw, x -> trim(regexp_replace(x, '<[^>]+>', '', 'g'))),
            x -> length(x) > 0
        ),
        ', '
    )::VARCHAR AS ingredients,
    -- structured allergen data: flatten values from entries named 'Contains'
    flatten(
        list_transform(
            list_filter(allergen_info, x -> x.name = 'Contains'),
            x -> x.values
        )
    )::VARCHAR[] AS contains_allergens,
    -- structured allergen data: flatten values from entries named 'May Contain'
    flatten(
        list_transform(
            list_filter(allergen_info, x -> x.name ILIKE '%may contain%'),
            x -> x.values
        )
    )::VARCHAR[] AS may_contain_allergens,
    dietary_flags::VARCHAR[] AS dietary_flags,
    list_contains(dietary_flags, 'Suitable for Vegans')      AS is_vegan,
    list_contains(dietary_flags, 'Suitable for Vegetarians') AS is_vegetarian,
    list_contains(dietary_flags, 'Gluten free')              AS is_gluten_free,
    list_contains(dietary_flags, 'Organic')                  AS is_organic,
    list_contains(dietary_flags, 'Low Fat')                  AS is_low_fat,
    list_contains(dietary_flags, 'Kosher')                   AS is_kosher,
    list_contains(dietary_flags, 'Halal')                    AS is_halal,
    image_url::VARCHAR AS image_url,
    is_product_available::BOOLEAN AS is_product_available,
    scraped_date::DATE AS scraped_date
FROM priced
