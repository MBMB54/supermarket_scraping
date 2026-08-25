{{ config(materialized='view') }}

WITH cleaned AS (
SELECT
    id,
    supermarket,
    {{ normalize_text('title') }} AS title,
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
    is_organic,
    is_low_fat,
    fat_per_100g,
    image_url,
    is_product_available,
    is_own_brand,
    item_description,
    scraped_date
FROM {{ ref('stg_dunnes') }}
)

SELECT
    id::VARCHAR AS id,
    supermarket::VARCHAR AS supermarket,
    title::VARCHAR AS title,
    {{ clean_title('title') }} AS title_cleaned,
    brand::VARCHAR AS brand,
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
    selling_size::DOUBLE AS selling_size,
    unit::VARCHAR AS unit,
    unit_qty_normalised::DOUBLE AS unit_qty_normalised,
    unit_normalised::VARCHAR AS unit_normalised,
    unit_price::DOUBLE AS unit_price,
    ROUND(price / NULLIF(unit_qty_normalised, 0), 2)::DOUBLE AS price_per_unit_normalised,
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
    ) AS contains_allergens,
    list_filter(
        json_keys(allergy_advice),
        k -> json_extract_string(allergy_advice, '$.' || k) LIKE '%May Contain%'
    ) AS may_contain_allergens,
    dietary_flags::VARCHAR[] AS dietary_flags,
    -- Verified against raw data: data.attributes.vegan/.vegetarian/.organic agree exactly
    -- (zero disagreements) with both the structured attributes.other/.dietary arrays AND the
    -- free-text Lifestyle section parsed into dietary_flags above, so unlike SuperValu these
    -- don't need OR-ing with a second source -- the boolean alone is complete.
    is_vegan::BOOLEAN AS is_vegan,
    is_vegetarian::BOOLEAN AS is_vegetarian,
    -- Unlike vegan/vegetarian/organic, is_gluten_free does have a real gap: 62 products carry
    -- the Lifestyle tag "Suitable for Coeliacs" -- a second vocabulary for the same claim --
    -- while attributes['gluten free'] is false (961 -> 1,023 true after OR'ing both signals).
    (
        coalesce(is_gluten_free, false)
        OR coalesce(list_contains(dietary_flags, 'Gluten free'), false)
        OR coalesce(list_contains(dietary_flags, 'Suitable for Coeliacs'), false)
    )::BOOLEAN AS is_gluten_free,
    is_organic::BOOLEAN AS is_organic,
    -- Not a data-quality bug: data.attributes.lowfat agrees perfectly with the retailer's own
    -- "Low Fat"/"LowFat" tags (structured array and Lifestyle text alike) -- the retailer's
    -- own claim data is complete and internally consistent, 269/13,861 true either way. This
    -- is a deliberate semantic widening to match int_supervalu's is_low_fat definition (retailer
    -- claim OR nutrition panel qualifying under the EU <=3g/100g threshold OR "fat free"/"0%
    -- fat" title wording), so is_low_fat stays comparable across the unioned schema rather than
    -- meaning "retailer-labelled low fat" for Dunnes and something broader for SuperValu.
    -- Raises true count 269 -> 1,927 (title wording alone contributes 10 rows beyond nutrition).
    (
        coalesce(is_low_fat, false)
        OR title LIKE '%fat free%'
        OR title LIKE '%0 percent fat%'
        OR (fat_per_100g IS NOT NULL AND fat_per_100g <= 3.0)
    )::BOOLEAN AS is_low_fat,
    -- Kosher/Halal have no structured attribute or tag array anywhere in the raw payload --
    -- the ONLY signal is the free-text Lifestyle section (232 Kosher / 157 Halal out of
    -- 13,861 products), so these are read from dietary_flags rather than a boolean attribute.
    list_contains(dietary_flags, 'Kosher')::BOOLEAN AS is_kosher,
    list_contains(dietary_flags, 'Halal')::BOOLEAN AS is_halal,
    image_url::VARCHAR AS image_url,
    is_product_available::BOOLEAN AS is_product_available,
    scraped_date::DATE AS scraped_date

FROM cleaned
