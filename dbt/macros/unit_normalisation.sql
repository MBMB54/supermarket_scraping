{#- Pack size normalised to kg / l / m / each, so prices can be compared across pack sizes. -#}

{#- Pack size converted to kg / l / m. Units that are not a weight, volume or length give 1.0 (price
    per pack). Pass count_units to treat those units as genuine item counts, so the number becomes
    the quantity and price_per_unit_normalised is per item (Aldi only: its "6 pack" is parsed from
    text). The structured counts of Tesco/SuperValu/Dunnes are deliberately not used; see
    docs/dbt_orchestration_plan.md. -#}
{% macro unit_qty_normalised(unit, qty, count_units=[]) %}
CASE
    WHEN {{ unit }} IN ('g', 'mg', 'ml') THEN {{ qty }} / 1000.0
    WHEN {{ unit }} = 'cl' THEN {{ qty }} / 100.0
    WHEN {{ unit }} IN ('kg', 'l', 'm', 'litre', 'litres') THEN {{ qty }}
    {% if count_units %}
    WHEN {{ unit }} IN ({{ count_units | map('tojson') | join(', ') | replace('"', "'") }}) THEN COALESCE({{ qty }}, 1.0)
    {% endif %}
    ELSE 1.0
END
{% endmacro %}


{% macro unit_normalised(unit) %}
CASE {{ unit }}
    WHEN 'g' THEN 'kg'
    WHEN 'kg' THEN 'kg'
    WHEN 'mg' THEN 'l'
    WHEN 'ml' THEN 'l'
    WHEN 'cl' THEN 'l'
    WHEN 'l' THEN 'l'
    WHEN 'litre' THEN 'l'
    WHEN 'litres' THEN 'l'
    WHEN 'm' THEN 'm'
    ELSE 'each'
END
{% endmacro %}


{% macro price_per_unit_normalised(price, qty_normalised) %}
ROUND({{ price }} / NULLIF({{ qty_normalised }}, 0), 2)
{% endmacro %}
