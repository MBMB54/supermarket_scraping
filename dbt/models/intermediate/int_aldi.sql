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
    {{ normalize_text('title') }} AS title,
    {{ clean_title('title') }} AS title_cleaned,
    {{ normalize_text('brand') }} AS brand,
    description AS item_description,
    category_1,
    category_2,
    is_sale AS is_discount,
    CAST(regexp_replace(was_price_display, '[^\d.]', '', 'g') AS DOUBLE) AS was_price,
    regexp_replace(price_display, '[^\d.]', '', 'g')::DOUBLE AS price,
    unit_qty AS selling_size,
    unit_raw AS unit,
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
    -- Aldi's raw price.perUnit is populated on <1% of rows (9/4311 sampled) and, where
    -- present, holds a pack-weight quantity (e.g. 0.5 "kg") rather than a euro amount -
    -- it is not a usable per-unit price signal, so unit_price is NULL for Aldi.
    CAST(NULL AS DOUBLE) AS unit_price,
    -- ingredients: raw field carries literal "N/A"/"na"/"n/a" placeholders (incl. one
    -- non-breaking-space padded variant) instead of true NULLs for ~10% of populated rows;
    -- normalize all such placeholders to real NULL.
    CASE
        WHEN ingredients IS NULL THEN NULL
        WHEN regexp_replace(upper(ingredients), '[^A-Z]', '', 'g') = 'NA' THEN NULL
        ELSE trim(ingredients)
    END AS ingredients,
    list_transform(
        list_filter(nutritional_claims, claim -> lower(claim.label) = 'contains'),
        claim -> claim.value
    ) AS contains_allergens,
    list_transform(
        list_filter(nutritional_claims, claim -> lower(claim.label) = 'may contain'),
        claim -> claim.value
    ) AS may_contain_allergens,
    -- image_url_raw is a Scene7-style templated URL, e.g.
    -- ".../scaleWidth/{width}/<uuid>/{slug}" - substitute a fixed display width and drop
    -- the optional {slug} segment; verified both substitutions resolve to a live image (HTTP 200).
    CASE
        WHEN image_url_raw IS NULL THEN NULL
        ELSE replace(replace(image_url_raw, '{width}', '600'), '{slug}', '')
    END AS image_url,
    is_product_available,
    scraped_date
FROM stg
WHERE is_product_available IS TRUE
)

SELECT
    id,
    supermarket,
    title,
    title_cleaned,
    brand,
    CAST(NULL AS BOOLEAN) AS is_own_brand,
    item_description,
    {{ normalize_category('category_1') }} AS category_1,
    {{ normalize_category('category_2') }} AS category_2,
    CAST(NULL AS VARCHAR) AS category_3,
    CAST(NULL AS VARCHAR) AS category_4,
    is_discount,
    CAST(NULL AS BOOLEAN) AS is_promotion,
    price,
    was_price,
    selling_size,
    unit,
    unit_qty_normalised,
    unit_normalised,
    unit_price,
    ROUND(price / NULLIF(unit_qty_normalised, 0), 2) AS price_per_unit_normalised,
    CAST(NULL AS VARCHAR) AS promotion_description,
    CAST(NULL AS VARCHAR) AS promotion_type,
    CAST(NULL AS VARCHAR[]) AS promotion_qualities,
    CAST(NULL AS DATE) AS promotion_start_date,
    CAST(NULL AS DATE) AS promotion_end_date,
    -- discount_start_date: staging's sale_date (raw onSaleDate) pairs with display strings
    -- like "In Store Thu 30 Jul" / "While Stock Lasts" and correlates with notForSale=true -
    -- it represents when the product becomes available/restocked in store, not when a
    -- discount begins. No discount-start signal exists in Aldi's raw data, so this is NULL.
    CAST(NULL AS DATE) AS discount_start_date,
    CAST(NULL AS DATE) AS discount_end_date,
    ingredients,
    contains_allergens,
    may_contain_allergens,
    CAST(NULL AS VARCHAR[]) AS dietary_flags,
    CAST(NULL AS BOOLEAN) AS is_vegan,
    CAST(NULL AS BOOLEAN) AS is_vegetarian,
    CAST(NULL AS BOOLEAN) AS is_gluten_free,
    CAST(NULL AS BOOLEAN) AS is_organic,
    CAST(NULL AS BOOLEAN) AS is_low_fat,
    CAST(NULL AS BOOLEAN) AS is_kosher,
    CAST(NULL AS BOOLEAN) AS is_halal,
    image_url,
    is_product_available,
    scraped_date
FROM extracted
