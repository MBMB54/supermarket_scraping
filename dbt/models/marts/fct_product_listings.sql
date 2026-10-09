{#- One row per retailer product per scrape: all four int_* models share one schema (enforced by
    their contracts), so a positional UNION ALL is safe. -#}
{#- var retailers lets a historical backfill build only the retailers that have a raw partition
    for that date (reading a missing partition is an error). -#}
{% set retailers = var('retailers', ['tesco', 'dunnes', 'supervalu', 'aldi']) %}

WITH unioned AS (
{% for retailer in retailers %}
    SELECT * FROM {{ ref('int_' ~ retailer) }}
    {% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
)

-- Some early raw days contain the same id several times (re-scraped); keep one row, preferring
-- one that has a price. A no-op on current data, where ids are unique.
SELECT * FROM unioned
QUALIFY ROW_NUMBER() OVER (PARTITION BY supermarket, id ORDER BY price IS NULL, price) = 1
