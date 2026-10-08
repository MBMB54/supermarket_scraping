WITH stg AS (
    SELECT * FROM {{ ref('stg_dunnes') }}
),

-- Unnest to one row per (product, allergen) and keep the strictest status per allergen
-- (Contains > May Contain > Free From): map_from_entries errors on duplicate keys, and the
-- advice text can list an allergen more than once.
allergy_pairs AS (
    SELECT
        id,
        unnest(regexp_extract_all(allergy_section, '([A-Za-z][A-Za-z ]*?) - (Contains|May Contain|Free From)', 1)) AS allergen_name,
        unnest(regexp_extract_all(allergy_section, '([A-Za-z][A-Za-z ]*?) - (Contains|May Contain|Free From)', 2)) AS allergen_status
    FROM (
        SELECT id, array_to_string({{ html_section_all('item_description', 'Allergy Advice') }}, '|') AS allergy_section
        FROM stg
    )
    WHERE allergy_section != ''
),

allergy_dedup AS (
    SELECT DISTINCT ON (id, allergen_name)
        id,
        allergen_name,
        allergen_status
    FROM allergy_pairs
    ORDER BY
        id,
        allergen_name,
        CASE allergen_status WHEN 'Contains' THEN 1 WHEN 'May Contain' THEN 2 ELSE 3 END
),

allergy_json AS (
    SELECT
        id,
        to_json(map_from_entries(list_zip(list(allergen_name), list(allergen_status)))) AS allergy_advice
    FROM allergy_dedup
    GROUP BY id
),

parsed AS (
    SELECT
        stg.*,
        allergy_json.allergy_advice,
        {{ html_ingredients_text(html_section('stg.item_description', 'Ingredients')) }} AS ingredients,
        -- Lifestyle tags (Kosher, Halal, ...) are the only source for kosher/halal
        NULLIF(string_split({{ html_section('stg.item_description', 'Lifestyle') }}, '<br/>'), ['']) AS dietary_flags
    FROM stg
    LEFT JOIN allergy_json USING (id)
),

normalised AS (
    SELECT
        *,
        {{ normalize_text('title') }} AS title_norm,
        {{ unit_qty_normalised('unit', 'quantity', 'title_norm') }} AS unit_qty_normalised,
        {{ unit_normalised('unit') }} AS unit_normalised
    FROM parsed
)

SELECT
    id::VARCHAR AS id,
    supermarket::VARCHAR AS supermarket,
    title_norm::VARCHAR AS title,
    {{ clean_title('title') }} AS title_cleaned,
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
    unit_normalised::VARCHAR AS unit_normalised,
    unit_price::DOUBLE AS unit_price,
    {{ price_per_unit_normalised('price', 'unit_qty_normalised', 'unit_normalised') }}::DOUBLE AS price_per_unit_normalised,
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
    -- Boolean attributes agree with the structured and Lifestyle tags for vegan, vegetarian and organic
    is_vegan::BOOLEAN AS is_vegan,
    is_vegetarian::BOOLEAN AS is_vegetarian,
    -- "Suitable for Coeliacs" is a second vocabulary for the same claim
    (
        coalesce(is_gluten_free, false)
        OR {{ has_flag('dietary_flags', 'Gluten free') }}
        OR {{ has_flag('dietary_flags', 'Suitable for Coeliacs') }}
    )::BOOLEAN AS is_gluten_free,
    is_organic::BOOLEAN AS is_organic,
    -- Widened beyond the retailer's own tag to match int_supervalu's definition
    (
        coalesce(is_low_fat, false)
        OR {{ low_fat_title_claim('title_norm') }}
        OR {{ low_fat_nutrition_panel('fat_per_100g') }}
    )::BOOLEAN AS is_low_fat,
    list_contains(dietary_flags, 'Kosher')::BOOLEAN AS is_kosher,
    list_contains(dietary_flags, 'Halal')::BOOLEAN AS is_halal,
    CAST(NULL AS BOOLEAN) AS is_dairy_free,
    CAST(NULL AS BOOLEAN) AS is_sugar_free,
    CAST(NULL AS BOOLEAN) AS has_no_added_sugar,
    CAST(NULL AS BOOLEAN) AS is_diabetic_friendly,
    image_url::VARCHAR AS image_url,
    is_product_available::BOOLEAN AS is_product_available,
    scraped_date::DATE AS scraped_date
FROM normalised
