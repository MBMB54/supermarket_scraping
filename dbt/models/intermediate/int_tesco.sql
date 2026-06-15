{{ config(materialized='view') }}

WITH cleaned AS (
SELECT
    id,
    regexp_replace(
    regexp_replace(
        regexp_replace(
            regexp_replace(
                strip_accents(lower(title)),
            '%', ' percent', 'g'),
        '&', 'and', 'g'),
    '-', ' ', 'g'),
    '[^a-z0-9\s]', '', 'g') AS title,
    regexp_replace(
    regexp_replace(
        regexp_replace(
            regexp_replace(
                strip_accents(lower(brand)),
            '%', ' percent', 'g'),
        '&', 'and', 'g'),
    '-', ' ', 'g'),
    '[^a-z0-9\s]', '', 'g') AS brand,
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
    -- keep the raw HTML so allergens can be parsed from <strong> tags below
    ingredients AS ingredients_raw,
    dietary_flags,
    image_url
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
    id,
    title,
    brand,
    category_1,
    category_2,
    category_3,
    category_4,
    is_discount,
    is_promotion,
    price,
    was_price,
    promotion_start_date,
    promotion_end_date,
    promotion_description,
    promotion_type,
    promotion_qualities,
    unit_price,
    selling_size,
    unit,
    unit_qty_normalised,
    unit_normalised,
    -- cleaned ingredients: HTML stripped + trimmed per element, empties removed
    list_filter(
        list_transform(ingredients_raw, x -> trim(regexp_replace(x, '<[^>]+>', '', 'g'))),
        x -> length(x) > 0
    ) AS ingredients,
    -- derived allergens: Tesco bolds allergens with <strong> tags inside ingredients
    list_distinct(
        list_transform(
            regexp_extract_all(array_to_string(ingredients_raw, ' '), '<strong>([^<]+)</strong>', 1),
            x -> trim(lower(x))
        )
    ) AS allergens,
    dietary_flags,
    list_contains(dietary_flags, 'Suitable for Vegans')      AS is_vegan,
    list_contains(dietary_flags, 'Suitable for Vegetarians') AS is_vegetarian,
    list_contains(dietary_flags, 'Gluten free')              AS is_gluten_free,
    list_contains(dietary_flags, 'Organic')                  AS is_organic,
    list_contains(dietary_flags, 'Kosher')                   AS is_kosher,
    list_contains(dietary_flags, 'Halal')                    AS is_halal,
    image_url,
    ROUND(price / NULLIF(unit_qty_normalised, 0), 2) AS price_per_unit_normalised
FROM priced
