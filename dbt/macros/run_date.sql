{#- The raw partition date being processed (--vars '{run_date: YYYY-MM-DD}'). Defaults to today in
    UTC, which is what the scrapers use for their S3 folder names. -#}
{% macro run_date() %}
{{ var('run_date', modules.datetime.datetime.now(modules.datetime.timezone.utc).date().isoformat()) }}
{% endmacro %}

{% macro run_date_sql() %}
CAST('{{ run_date() | trim }}' AS DATE)
{% endmacro %}
