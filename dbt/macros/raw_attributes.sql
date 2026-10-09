{#- Read one key of a free-form attributes object. The raw object's keys differ by product, day and
    era: a struct column breaks the whole query when a key is missing from the data being read, so
    go through JSON, where an absent key is just NULL. type is a DuckDB type (e.g. BOOLEAN,
    VARCHAR[]); VARCHAR returns the string unquoted (keeps JSON-encoded strings as text). -#}
{% macro raw_attr(attrs, key, type) %}
{%- if type == 'VARCHAR' -%}
json_extract_string(to_json({{ attrs }}), '$."{{ key }}"')
{%- elif type == 'JSON' -%}
json_extract(to_json({{ attrs }}), '$."{{ key }}"')
{%- else -%}
CAST(json_extract(to_json({{ attrs }}), '$."{{ key }}"') AS {{ type }})
{%- endif -%}
{% endmacro %}
