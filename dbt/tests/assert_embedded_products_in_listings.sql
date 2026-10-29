{{ config(severity='warn', tags=['embeddings']) }}
-- Products with a current embedding that are not in this run's fct_product_listings: stale ids the
-- scrapers no longer return (the embeddings store keeps every product ever embedded), or a
-- retailer missing from a degraded run. Warn only; consumers should inner join to the listings.
SELECT
    e.spec_id,
    e.supermarket,
    count(DISTINCT e.id) AS n_missing
FROM {{ ref('fct_embeddings') }} AS e
LEFT JOIN {{ ref('fct_product_listings') }} AS l
    ON l.supermarket = e.supermarket AND l.id = e.id
WHERE l.id IS NULL
  AND e.supermarket IN (
{%- for r in var('retailers') %}'{{ r }}'{{ ', ' if not loop.last }}{% endfor %})
GROUP BY e.spec_id, e.supermarket
