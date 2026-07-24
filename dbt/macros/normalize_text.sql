{% macro normalize_text(col) %}
regexp_replace(
    regexp_replace(
        regexp_replace(
            regexp_replace(
                strip_accents(lower({{ col }})),
            '%', ' percent', 'g'),
        '&', 'and', 'g'),
    '-', ' ', 'g'),
'[^a-z0-9\s]', '', 'g')
{% endmacro %}
