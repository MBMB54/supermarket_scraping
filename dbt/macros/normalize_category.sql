{% macro normalize_category(col) %}
    nullif(regexp_replace(trim({{ col }}), '\s+', ' ', 'g'), '')
{% endmacro %}
