{% snapshot scd_product_prices %}

{{ config(
    target_schema='main',
    unique_key="id::varchar",
    strategy='check',
    check_cols=['price', 'quantity','promotion_price', 'unit_price'],
) }}

SELECT
    'tesco'     AS retailer,
    id,
    title,
    brand,
    price,
    unit_price,
    promotion_price,
    scraped_date
FROM {{ ref('stg_tesco') }}

UNION ALL

SELECT
    'aldi'      AS retailer,
    id,
    title,
    brand,
    price,
    unit_price,
    NULL::double AS promotion_price,
    scraped_date
FROM {{ ref('stg_aldi') }}

UNION ALL

SELECT
    'dunnes'    AS retailer,
    id,
    title,
    brand,
    price,
    unit_price,
    promotion_price,
    scraped_date
FROM {{ ref('stg_dunnes') }}

UNION ALL

SELECT
    'supervalu' AS retailer,
    id,
    title,
    brand,
    price,
    unit_price,
    promotion_price,
    scraped_date
FROM {{ ref('stg_supervalu') }}

{% endsnapshot %}
