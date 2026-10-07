{#- Append-only daily price facts: one row per retailer product per scrape date (run_date).
    delete+insert on the key makes any run idempotent and order-independent, so reruns and
    out-of-order backfills just rewrite that day's rows. Never --full-refresh (history lives only
    here): full_refresh is disabled. Rows with no price are failed fetches, not price changes. -#}
{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key=['supermarket', 'id', 'scraped_date'],
    full_refresh=false
) }}

SELECT
    supermarket,
    id,
    scraped_date,
    title,
    brand,
    price,
    was_price,
    unit_price,
    is_discount,
    is_promotion
FROM {{ ref('fct_product_listings') }}
WHERE price IS NOT NULL
