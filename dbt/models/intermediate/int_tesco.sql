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
    certification_flags,
    nutritional_claims,
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
    -- Unlike SuperValu, Tesco's dietary_flags (foodIcons) is NOT under-reporting here: it's a
    -- clean exact subset of the broader on-page icon badges (certification_flags) for every
    -- shared tag (e.g. 'Suitable for Vegans' 833/833, 'Gluten free' 342/342, 'Halal' 89/89,
    -- 'Kosher' 103/103 — identical counts either way), with disagreement counts of just 7/2/8/9
    -- products respectively — noise, not a real gap. Left as single-source.
    list_contains(dietary_flags, 'Suitable for Vegans')      AS is_vegan,
    list_contains(dietary_flags, 'Suitable for Vegetarians') AS is_vegetarian,
    list_contains(dietary_flags, 'Gluten free')              AS is_gluten_free,
    -- Organic IS a real gap: certification_flags (the full icon badge list) carries specific
    -- organic-certification-body marks (EU Organic, Soil Association Organic, Irish Organic
    -- Association, etc.) that aren't folded into dietary_flags' generic 'Organic' tag — 33
    -- products (128 -> 161, +26%) are organic-certified but only visible via the badge text.
    (
        coalesce(list_contains(dietary_flags, 'Organic'), false)
        OR coalesce(len(list_filter(certification_flags, x -> x ILIKE '%organic%')) > 0, false)
    )::BOOLEAN AS is_organic,
    -- Three independent signals OR'd together, mirroring SuperValu's is_low_fat: the foodIcons
    -- 'Low Fat' tag, explicit "low fat"/"fat free"/"0 percent fat" wording in the (already
    -- normalized) title, and Tesco's free-text regulatory nutritionalClaims (e.g. 'Low Fat',
    -- 'Low in fat', 'Fat free' — deliberately excludes 'low in saturated fat' claims, which are
    -- a distinct regulatory claim about saturates, not total fat). 97 -> ~142 true (+46%).
    -- NOT using a nutrition-panel fat-grams threshold here, unlike SuperValu: Tesco's
    -- nutritionInfo is a free-text table (20+ 'per 100g'/'per 100ml' header variants, only
    -- ~10% of products have a parseable row) rather than SuperValu's typed per-100g map, and a
    -- naive <=3g/100g threshold flags plenty of naturally-low-fat items that aren't marketed as
    -- diet products (meringues, chutney, deli ham, soup) — a much noisier signal than the
    -- SuperValu case, so left out; see session notes if this should be reconsidered.
    (
        coalesce(list_contains(dietary_flags, 'Low Fat'), false)
        OR title LIKE '%low fat%'
        OR title LIKE '%fat free%'
        OR title LIKE '%0 percent fat%'
        OR coalesce(
            len(list_filter(
                nutritional_claims,
                x -> lower(x) LIKE '%low fat%' OR lower(x) LIKE '%fat free%'
                     OR lower(x) LIKE '%low in fat%'
            )) > 0,
            false
        )
    )::BOOLEAN AS is_low_fat,
    list_contains(dietary_flags, 'Kosher')                   AS is_kosher,
    list_contains(dietary_flags, 'Halal')                    AS is_halal,
    image_url::VARCHAR AS image_url,
    is_product_available::BOOLEAN AS is_product_available,
    scraped_date::DATE AS scraped_date
FROM priced
