{{ config(materialized='view') }}

WITH source AS (
    SELECT * FROM {{ source('external_source', 'dunnes') }}
),

-- Dunnes runs on the same storefrontgateway backend as SuperValu, but unlike SuperValu its
-- data.attributes struct has no _allergy_advice key at all (confirmed empty across a full day's
-- scrapes). Allergen info is instead embedded in the description HTML under an "Allergy
-- Advice" section, e.g. "Milk - Contains<br/>Wheat - May Contain<br/>", same as how ingredients
-- is already extracted from HTML below since data.ingredients is always null for Dunnes.
with_allergy_section AS (
    SELECT
        *,
        array_to_string(
            regexp_extract_all(data.description, '<b>Allergy Advice</b><br/>(.*?)(?:<br/><br/>|$)', 1),
            '|'
        ) AS allergy_advice_section
    FROM source
),

-- Unnest to one row per (product, allergen) pair, then keep the highest-severity status per
-- allergen (Contains > May Contain > Free From) before rebuilding the map: map_from_entries
-- hard-errors on duplicate keys, and a product's Allergy Advice text can otherwise repeat the
-- same allergen (e.g. once under "For allergens, see ingredients in bold" plus once under the
-- structured list, or listing it twice with different statuses).
allergy_pairs AS (
    SELECT
        product_id,
        unnest(regexp_extract_all(allergy_advice_section, '([A-Za-z][A-Za-z ]*?) - (Contains|May Contain|Free From)', 1)) AS allergen_name,
        unnest(regexp_extract_all(allergy_advice_section, '([A-Za-z][A-Za-z ]*?) - (Contains|May Contain|Free From)', 2)) AS allergen_status
    FROM with_allergy_section
    WHERE allergy_advice_section != ''
),

allergy_dedup AS (
    SELECT DISTINCT ON (product_id, allergen_name)
        product_id,
        allergen_name,
        allergen_status
    FROM allergy_pairs
    ORDER BY
        product_id,
        allergen_name,
        CASE allergen_status WHEN 'Contains' THEN 1 WHEN 'May Contain' THEN 2 ELSE 3 END
),

allergy_json AS (
    SELECT
        product_id,
        to_json(map_from_entries(list_zip(list(allergen_name), list(allergen_status)))) AS allergy_advice
    FROM allergy_dedup
    GROUP BY product_id
)

SELECT
    product_id AS id,
    'dunnes' AS supermarket,
    data.name AS title,
    data.brand AS brand,
    data.unitsOfSize.size AS quantity,
    NULLIF(LOWER(data.unitsOfSize.abbreviation), '') AS unit,
    TRY_CAST(regexp_replace(data.unitPrice, '[^0-9.]', '', 'g') AS FLOAT) AS unit_price,
    TRY_CAST(regexp_replace(data.wasUnitPrice, '[^0-9.]', '', 'g') AS FLOAT) AS was_unit_price,
    LOWER(data.unitOfMeasure.abbreviation) AS unit_of_measure,
    -- SuperValu API returns the effective (already-discounted) price; was_price is original when discounted
    TRY_CAST(regexp_replace(data.price, '[^0-9.]', '', 'g') AS FLOAT) AS price,
    TRY_CAST(regexp_replace(data.wasPrice, '[^0-9.]', '', 'g') AS FLOAT) AS was_price,
    TRY_CAST(json_extract_string(data.promotions, '$[0].startDateUtc') AS DATE) AS promotion_start_date,
    TRY_CAST(json_extract_string(data.promotions, '$[0].endDateUtc') AS DATE) AS promotion_end_date,
    -- tprInfo covers standalone TPR price cuts; dates arrive in DD/MM/YYYY format
    CAST(TRY_STRPTIME(data.tprInfo.effectiveFrom, '%d/%m/%Y') AS DATE) AS discount_start_date,
    CAST(TRY_STRPTIME(data.tprInfo.effectiveUntil, '%d/%m/%Y') AS DATE) AS discount_end_date,
    json_extract_string(data.promotions, '$[0].name') AS promotion_description,
    json_extract_string(data.promotions, '$[0].promotionType') AS promotion_type,
    data.categories[2].category AS category_1,
    data.categories[3].category AS category_2,
    data.categories[4].category AS category_3,
    NULL::string AS category_4,
    data.description AS item_description,
    -- data.ingredients is always null; extract from the description HTML instead
    NULLIF(
        trim(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(
                                regexp_extract(data.description, '<b>Ingredients</b><br/>(.*?)(?:<br/><br/>|$)', 1),
                                '<br/>',
                                ', ',
                                'g'
                            ),
                            '<[^>]*>',
                            '',
                            'g'
                        ),
                        '(?i)^ingredients:\s*',
                        ''
                    ),
                    '\s+',
                    ' ',
                    'g'
                ),
                '([a-z])([A-Z])',
                '\1, \2',
                'g'
            )
        ),
        ''
    ) AS ingredients,
    data.attributes['gluten free'] AS is_gluten_free,
    data.attributes.vegetarian AS is_vegetarian,
    data.attributes.vegan AS is_vegan,
    data.attributes.organic AS is_organic,
    data.attributes.lowfat AS is_low_fat,
    data.attributes.other AS other_dietary,
    allergy_json.allergy_advice AS allergy_advice,
    data.primaryImage.zoom AS image_url,
    data.available AS is_product_available,
    -- is_discount: wasPrice populated whenever a per-unit price cut applies (TPR or ProductPromotion)
    data.wasPrice IS NOT NULL AS is_discount,
    -- is_promotion: multibuy deals (BulkPromotion, BundlePromotion); can be TRUE alongside is_discount
    CASE
        WHEN json_array_length(data.promotions) = 0 OR data.promotions IS NULL THEN FALSE
        WHEN json_extract_string(data.promotions, '$[0].promotionType') IN ('BulkPromotion', 'BundlePromotion', 'Custom') THEN TRUE
        ELSE FALSE
    END AS is_promotion,
    data.attributes.OwnBrand AS is_own_brand,
    CURRENT_DATE AS scraped_date
FROM with_allergy_section
LEFT JOIN allergy_json USING (product_id)
WHERE data.name IS NOT NULL
