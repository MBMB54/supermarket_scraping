{% macro utc_to_local_date(col) %}
{#
  Casting straight to TIMESTAMPTZ only shifts correctly when the source is a string
  with an explicit UTC marker (e.g. SuperValu/Dunnes' json_extract_string output).
  Tesco's equivalent field is inferred as a native, timezone-naive TIMESTAMP at the
  external-table level, which silently drops the 'Z' — casting that straight to
  TIMESTAMPTZ reinterprets it as session-local time instead of UTC, a no-op here
  since the session is already Europe/Dublin. Casting to plain TIMESTAMP first
  normalizes both shapes to the same naive wall-clock value, which is then
  explicitly anchored as UTC before converting to local time.
#}
CAST((TRY_CAST({{ col }} AS TIMESTAMP) AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Dublin' AS DATE)
{% endmacro %}
