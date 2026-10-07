{#- First promotion on a storefront gateway product (promotions is a JSON array string). -#}
{% macro first_promotion_field(promotions, key) %}
json_extract_string({{ promotions }}, '$[0].{{ key }}')
{% endmacro %}


{#- Multibuy-style deals. is_discount (wasPrice present) can be TRUE for the same product. -#}
{% macro is_multibuy_promotion(promotions) %}
CASE
    WHEN {{ promotions }} IS NULL OR json_array_length({{ promotions }}) = 0 THEN FALSE
    WHEN {{ first_promotion_field(promotions, 'promotionType') }}
         IN ('BulkPromotion', 'BundlePromotion', 'Custom') THEN TRUE
    ELSE FALSE
END
{% endmacro %}
