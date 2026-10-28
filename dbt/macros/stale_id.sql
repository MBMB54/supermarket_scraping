{#- Errors that mean the product id no longer exists (the ID feed lags the price scrape). Dropped in
    staging so only live products remain; any other error stays visible to the data-quality tests.
    error may be inferred as JSON, so compare as text without JSON quotes. Tesco's not-found error is
    a long GraphQL message (`GraphQL error: [{"message": "product-not-found", ...}]`, truncated), so it
    is matched by substring. Keep in sync with NOT_FOUND_ERRORS / NOT_FOUND_MARKERS in
    scraper_common/quality.py. Returns NULL for a NULL error; callers coalesce or test IS NOT NULL. -#}
{% macro is_stale_id_error(error) %}
(
    trim(CAST({{ error }} AS VARCHAR), '"') IN ('Not found in any store', 'HTTP 404', 'No product returned')
    OR CAST({{ error }} AS VARCHAR) LIKE '%product-not-found%'
)
{% endmacro %}
