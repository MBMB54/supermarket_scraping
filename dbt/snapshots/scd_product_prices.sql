{% snapshot scd_product_prices %}

{{ config(
    target_schema='main',
    unique_key="id::varchar",
    strategy='check',
    check_cols=['price', 'was_price', 'unit_price'],
) }}

SELECT
    supermarket AS retailer,
    id,
    title,
    brand,
    price,
    was_price,
    unit_price,
    is_discount,
    is_promotion,
    scraped_date
FROM {{ ref('int_tesco') }}

UNION ALL

SELECT
    supermarket AS retailer,
    id,
    title,
    brand,
    price,
    was_price,
    unit_price,
    is_discount,
    is_promotion,
    scraped_date
FROM {{ ref('int_aldi') }}

UNION ALL

SELECT
    supermarket AS retailer,
    id,
    title,
    brand,
    price,
    was_price,
    unit_price,
    is_discount,
    is_promotion,
    scraped_date
FROM {{ ref('int_dunnes') }}

UNION ALL

SELECT
    supermarket AS retailer,
    id,
    title,
    brand,
    price,
    was_price,
    unit_price,
    is_discount,
    is_promotion,
    scraped_date
FROM {{ ref('int_supervalu') }}

{% endsnapshot %}
