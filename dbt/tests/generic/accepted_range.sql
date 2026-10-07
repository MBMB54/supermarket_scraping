{#- Fails for rows where a numeric column is outside [min_value, max_value]. NULLs are ignored
    (use not_null for those). Either bound may be omitted. -#}
{% test accepted_range(model, column_name, min_value=none, max_value=none) %}

SELECT *
FROM {{ model }}
WHERE {{ column_name }} IS NOT NULL
  AND (
      false
      {% if min_value is not none %} OR {{ column_name }} < {{ min_value }} {% endif %}
      {% if max_value is not none %} OR {{ column_name }} > {{ max_value }} {% endif %}
  )

{% endtest %}
