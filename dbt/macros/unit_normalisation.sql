{#- Pack size normalised to kg / l / m / each, so prices can be compared across pack sizes. -#}

{#- Pack count stated in a title, or NULL. Deliberately conservative: only "N pack", "N pk"/"Npk",
    "pack of N" and "N x" (e.g. "4 x 500ml"). Expects a normalize_text()'d title. -#}
{% macro title_pack_count(title) %}
COALESCE(
    TRY_CAST(NULLIF(regexp_extract({{ title }}, '(?:^|\s)(\d{1,3})\s?(?:pack|pk)(?:\s|$)', 1), '') AS INTEGER),
    TRY_CAST(NULLIF(regexp_extract({{ title }}, '(?:^|\s)pack of (\d{1,3})(?:\s|$)', 1), '') AS INTEGER),
    TRY_CAST(NULLIF(regexp_extract({{ title }}, '(?:^|\s)(\d{1,3})\s?x(?:\s|\d|$)', 1), '') AS INTEGER)
)
{% endmacro %}


{#- Pack size converted to kg / l / m.
    Counting units (unit NULL, or listed in count_units) use the retailer's item count as the
    quantity, so price_per_unit_normalised is a per-item price (sheets, tablets, wipes included).
    Accuracy over coverage: if the title states a pack count that differs from that quantity, the
    quantity is not trusted and the result is 1.0 (price per pack). Any other unit (m2 area, voltage,
    bare numbers) is 1.0. Pass title (normalised) to enable the title check. -#}
{% macro unit_qty_normalised(unit, qty, title=none, count_units=['sht', 'sngl', 'prs']) %}
CASE
    WHEN {{ unit }} IN ('g', 'mg', 'ml') THEN {{ qty }} / 1000.0
    WHEN {{ unit }} = 'cl' THEN {{ qty }} / 100.0
    WHEN {{ unit }} IN ('kg', 'l', 'm', 'litre', 'litres') THEN {{ qty }}
    WHEN {{ unit }} IS NULL OR {{ unit }} IN ({{ count_units | map('tojson') | join(', ') | replace('"', "'") }}) THEN
        {% if title is not none %}
        CASE WHEN {{ title_pack_count(title) }} IS NOT NULL AND {{ title_pack_count(title) }} <> {{ qty }} THEN 1.0
             ELSE COALESCE({{ qty }}, 1.0) END
        {% else %}
        COALESCE({{ qty }}, 1.0)
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


{#- Per-item prices can be tiny (EUR 0.0123 per sheet), so countable ('each') units keep 4 decimals;
    weight, volume and length keep 2. -#}
{% macro price_per_unit_normalised(price, qty_normalised, unit_normalised) %}
ROUND({{ price }} / NULLIF({{ qty_normalised }}, 0), CASE WHEN {{ unit_normalised }} = 'each' THEN 4 ELSE 2 END)
{% endmacro %}
