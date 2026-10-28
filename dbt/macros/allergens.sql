{#- Allergen names for one status from a JSON map such as {"Milk": "Contains", "Egg": "May Contain"}.
    Cast to JSON inside so Fusion static analysis resolves json_keys. -#}
{% macro allergens_from_json_map(col, status) %}
list_filter(
    json_keys(CAST({{ col }} AS JSON)),
    lambda k: json_extract_string(CAST({{ col }} AS JSON), '$.' || k) {{ "= 'Contains'" if status == 'contains' else "LIKE '%May Contain%'" }}
)::VARCHAR[]
{% endmacro %}


{#- Allergen names from a list of {name, values} structs (Tesco allergenInfo). -#}
{% macro allergens_from_structs(col, status) %}
flatten(
    list_transform(
        list_filter({{ col }}, lambda x: {{ "x.name = 'Contains'" if status == 'contains' else "x.name ILIKE '%may contain%'" }}),
        lambda x: x.values
    )
)::VARCHAR[]
{% endmacro %}
