{{ config(materialized='view') }}

WITH stg AS (
    SELECT *,
    TRY_CAST(
            REGEXP_EXTRACT(COALESCE(selling_size, price_comparison_display,title), '(\d+\.?\d*)') AS DOUBLE
        ) AS unit_qty,
    LOWER(
            REGEXP_EXTRACT(COALESCE(selling_size, price_comparison_display,title), '(\d+\.?\d*)\s*([a-zA-Z]+)', 2)
        ) AS unit_raw
    FROM {{ ref('stg_aldi') }}
),

extracted AS (
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
    is_sale,
    COALESCE(regexp_replace(was_price_display, '[^\d.]', '', 'g')) AS was_price,
    regexp_replace(price_display, '[^\d.]', '', 'g')::FLOAT AS current_price,
    selling_size,
    price_comparison_display,
    unit_raw,
    unit_qty,
    CASE 
            WHEN unit_qty IS NULL    THEN 1.0
            WHEN unit_raw = 'g'      THEN unit_qty / 1000
            WHEN unit_raw = 'kg'     THEN unit_qty
            WHEN unit_raw = 'ml'     THEN unit_qty / 1000
            WHEN unit_raw = 'l'      THEN unit_qty
            WHEN unit_raw = 'cl'     THEN unit_qty / 100
            ELSE unit_qty
        END AS unit_qty_normalised,
    CASE unit_raw
            WHEN 'g'  THEN 'kg'
            WHEN 'kg' THEN 'kg'
            WHEN 'ml' THEN 'l'
            WHEN 'l'  THEN 'l'
            WHEN 'litres' THEN 'l'
            WHEN 'litre' THEN 'l'
            WHEN 'cl' THEN 'l'
            ELSE 'each'
        END AS unit_normalised,
    CASE
    WHEN selling_size IS NOT NULL THEN 'selling_size'
    WHEN price_comparison_display IS NOT NULL THEN 'price_comparison display'
    WHEN REGEXP_EXTRACT(title, '\d+\.?\d*\s*[a-zA-Z]+') IS NOT NULL THEN 'title'
    ELSE 'each' END AS size_source
FROM stg
WHERE is_product_available IS TRUE
)

SELECT *,  ROUND(current_price / NULLIF(unit_qty_normalised, 0),2) AS price_per_unit_normalised
FROM extracted