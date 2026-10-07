{#- Fails when the relation has fewer than min_rows rows. Also catches an empty or missing raw
    partition when applied to a source or staging model. -#}
{% test min_row_count(model, min_rows) %}

SELECT count(*) AS row_count
FROM {{ model }}
HAVING count(*) < {{ min_rows }}

{% endtest %}
