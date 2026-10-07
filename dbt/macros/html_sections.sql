{#- Dunnes embeds ingredients / allergy / lifestyle info in the description HTML as
    "<b>Heading</b><br/>body<br/><br/>". -#}
{% macro html_section(html, heading) %}
regexp_extract({{ html }}, '<b>{{ heading }}</b><br/>(.*?)(?:<br/><br/>|$)', 1)
{% endmacro %}


{% macro html_section_all(html, heading) %}
regexp_extract_all({{ html }}, '<b>{{ heading }}</b><br/>(.*?)(?:<br/><br/>|$)', 1)
{% endmacro %}


{#- Ingredients section HTML -> single clean string; NULL if empty. -#}
{% macro html_ingredients_text(section) %}
NULLIF(
    trim(
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace({{ section }}, '<br/>', ', ', 'g'),
                        '<[^>]*>', '', 'g'),
                    '(?i)^ingredients:\s*', ''),
                '\s+', ' ', 'g'),
            '([a-z])([A-Z])', '\1, \2', 'g')
    ),
    ''
)
{% endmacro %}
