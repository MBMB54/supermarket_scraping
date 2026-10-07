{#- One row per retailer product per scrape: all four int_* models share one schema (enforced by
    their contracts), so a positional UNION ALL is safe. -#}
{% set retailers = ['tesco', 'dunnes', 'supervalu', 'aldi'] %}

{% for retailer in retailers %}
SELECT * FROM {{ ref('int_' ~ retailer) }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
