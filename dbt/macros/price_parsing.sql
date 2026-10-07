{#- Display strings such as '€1.99' or '1.99 each' -> DOUBLE; NULL when nothing numeric is left. -#}
{% macro parse_price(col) %}
TRY_CAST(regexp_replace({{ col }}, '[^0-9.]', '', 'g') AS DOUBLE)
{% endmacro %}


{#- Storefront gateway TPR dates arrive as DD/MM/YYYY strings. -#}
{% macro parse_dd_mm_yyyy(col) %}
CAST(TRY_STRPTIME({{ col }}, '%d/%m/%Y') AS DATE)
{% endmacro %}
