{{ config(
    materialized='table',
    tags=['search'],
    post_hook="{{ build_search_index(this) }}",
) }}
{#- One row per (supermarket, id): the product's text, dietary and display fields in one place, for
    keyword (BM25) + filter search. No embeddings: join emb_<spec> / fct_embeddings on
    (supermarket, id) at query time. The BM25 index is built by the post-hook, on local targets only
    (macro build_search_index). Columns are listed explicitly so a new fct_product_listings column
    does not silently change the search schema. -#}
SELECT
    supermarket || ':' || id AS product_key,
    supermarket,
    id,
    title,
    title_cleaned,
    brand,
    is_own_brand,
    category_1,
    category_2,
    category_3,
    category_4,
    item_description,
    ingredients,
    contains_allergens,
    may_contain_allergens,
    dietary_flags,
    is_vegan,
    is_vegetarian,
    is_gluten_free,
    is_organic,
    is_low_fat,
    is_kosher,
    is_halal,
    is_dairy_free,
    is_sugar_free,
    has_no_added_sugar,
    is_diabetic_friendly,
    price,
    was_price,
    is_discount,
    is_promotion,
    unit_price,
    price_per_unit_normalised,
    unit_normalised,
    image_url,
    is_product_available,
    scraped_date
FROM {{ ref('fct_product_listings') }}
