{#- NULL-safe membership test on a retailer tag list. -#}
{% macro has_flag(flags, tag) %}
coalesce(list_contains({{ flags }}, '{{ tag }}'), false)
{% endmacro %}


{#- Title wording that claims no fat. Title must already be normalize_text()'d. -#}
{% macro low_fat_title_claim(title) %}
({{ title }} LIKE '%fat free%' OR {{ title }} LIKE '%0 percent fat%')
{% endmacro %}


{#- EU "low fat" threshold: at most 3 g per 100 g. -#}
{% macro low_fat_nutrition_panel(fat_per_100g) %}
({{ fat_per_100g }} IS NOT NULL AND {{ fat_per_100g }} <= 3.0)
{% endmacro %}
