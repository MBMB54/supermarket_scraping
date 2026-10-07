{#- Detects scrapes that return records with an id but no payload (e.g. Tesco 429 responses
    parsed as empty rows). A row is "empty" when id is present and every column in
    value_columns is NULL. Fails when empty rows exceed max_pct of all rows. -#}
{% test null_row_rate(model, id_column, value_columns, max_pct) %}

WITH counts AS (
    SELECT
        count(*) AS total_rows,
        count(*) FILTER (
            WHERE {{ id_column }} IS NOT NULL
            {% for col in value_columns %} AND {{ col }} IS NULL {% endfor %}
        ) AS empty_rows
    FROM {{ model }}
)

SELECT
    total_rows,
    empty_rows,
    round(100.0 * empty_rows / total_rows, 2) AS empty_pct
FROM counts
WHERE total_rows > 0
  AND 100.0 * empty_rows / total_rows > {{ max_pct }}

{% endtest %}
