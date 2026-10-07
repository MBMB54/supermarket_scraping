{#- UTC timestamp (string or naive TIMESTAMP) -> Europe/Dublin calendar date.
    Casting to naive TIMESTAMP first makes string and native-timestamp sources behave the same;
    a direct TIMESTAMPTZ cast would reinterpret Tesco's naive values as local time. -#}
{% macro utc_to_local_date(col) %}
CAST((TRY_CAST({{ col }} AS TIMESTAMP) AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Dublin' AS DATE)
{% endmacro %}
