{#- SQL expression: grace days for a retailer column, from the active_grace_days dict var. -#}
{% macro active_grace_days(supermarket_col) %}
{%- set cfg = var('active_grace_days', {'default': 2}) -%}
CASE {{ supermarket_col }}
{%- for retailer, days in cfg.items() if retailer != 'default' %}
    WHEN '{{ retailer }}' THEN {{ days }}
{%- endfor %}
    ELSE {{ cfg.get('default', 0) }}
END
{% endmacro %}
