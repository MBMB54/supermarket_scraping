{{ config(materialized='view') }}

WITH cleaned AS (
SELECT
    id,
    supermarket,
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
    is_vegan,
    is_vegetarian,
    is_gluten_free,
    is_organic,
    is_low_fat,
    other_dietary,
    image_url,
    is_product_available,
    is_own_brand,
    item_description,
    scraped_date
FROM {{ ref('stg_dunnes') }}
)

SELECT
    id,
    supermarket,
    title,
    brand,
    is_own_brand,
    item_description,
    category_1,
    category_2,
    category_3,
    category_4,
    is_discount,
    is_promotion,
    price,
    was_price,
    selling_size,
    unit,
    unit_qty_normalised,
    unit_normalised,
    unit_price,
    ROUND(price / NULLIF(unit_qty_normalised, 0), 2) AS price_per_unit_normalised,
    promotion_description,
    promotion_type,
    promotion_start_date,
    promotion_end_date,
    discount_start_date,
    discount_end_date,
    ingredients,
    is_vegan,
    is_vegetarian,
    is_gluten_free,
    is_organic,
    is_low_fat,
    image_url,
    is_product_available,
    scraped_date
    
FROM cleaned
