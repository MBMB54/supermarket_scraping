{% test unique_combination(model, columns) %}

SELECT {{ columns | join(', ') }}, count(*) AS n
FROM {{ model }}
GROUP BY {{ columns | join(', ') }}
HAVING count(*) > 1

{% endtest %}
