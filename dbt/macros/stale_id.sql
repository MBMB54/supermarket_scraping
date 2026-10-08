{#- Errors that mean the product id no longer exists (the ID feed lags the price scrape). Dropped in
    staging so only live products remain; any other error stays visible to the data-quality tests.
    error may be inferred as JSON, so compare as text without JSON quotes. -#}
{% macro is_stale_id_error(error) %}
trim(CAST({{ error }} AS VARCHAR), '"') IN ('Not found in any store', 'HTTP 404')
{% endmacro %}
