{#- The raw partition date being processed (--vars '{run_date: YYYY-MM-DD}'). Defaults to today in
    UTC, which is what the scrapers use for their S3 folder names. run_started_at is UTC and exists in
    dbt-core and Fusion (modules.datetime.timezone is Fusion-only). Sources use the same expression
    inline because dbt-core renders source config without macros. Scheduled runs always pass it. -#}
{% macro run_date() %}
{{ var('run_date', run_started_at.strftime('%Y-%m-%d')) }}
{% endmacro %}

{% macro run_date_sql() %}
CAST('{{ run_date() | trim }}' AS DATE)
{% endmacro %}
