WITH source AS (
    SELECT * FROM {{ source('external_source', 'aldi') }}
    -- Dead skus come back as HTTP 404 with no data
    WHERE NOT coalesce({{ is_stale_id_error('error') }}, false)
)

SELECT
    data.sku AS id,
    'aldi' AS supermarket,
    data.name AS title,
    data.brandName AS brand,
    data.sellingSize AS selling_size,
    data.price.amountRelevantDisplay AS price_display,
    data.price.wasPriceDisplay AS was_price_display,
    data.price.comparisonDisplay AS price_comparison_display,
    data.OnSaleDateDisplay AS sale_date_display,
    data.OnSaleDate AS sale_date,
    data.categories[1].name AS category_1,
    data.categories[2].name AS category_2,
    CAST(NULL AS VARCHAR) AS category_3,
    CAST(NULL AS VARCHAR) AS category_4,
    data.description AS description,
    data.ingredients AS ingredients,
    data.nutritionalClaims AS nutritional_claims,
    data.assets[1].url AS image_url_raw,
    data.notForSaleReason IS DISTINCT FROM 'This product is currently not available.' AS is_product_available,
    data.price.wasPriceDisplay IS NOT NULL AS is_sale,
    {{ run_date_sql() }} AS scraped_date
FROM source
