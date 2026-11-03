{{ config(tags=["search"]) }}
-- dim_product_search must hold exactly the keys of fct_product_listings: same row count, no key
-- missing and no key extra. Returns one row per discrepancy kind (empty = pass).
WITH l AS (SELECT supermarket, id FROM {{ ref('fct_product_listings') }}),
s AS (SELECT supermarket, id FROM {{ ref('dim_product_search') }})
SELECT 'row_count' AS problem, (SELECT count(*) FROM l) AS n_listings, (SELECT count(*) FROM s) AS n_search
WHERE (SELECT count(*) FROM l) <> (SELECT count(*) FROM s)
UNION ALL
SELECT 'missing_in_search', count(*), NULL FROM (SELECT * FROM l EXCEPT SELECT * FROM s) HAVING count(*) > 0
UNION ALL
SELECT 'extra_in_search', count(*), NULL FROM (SELECT * FROM s EXCEPT SELECT * FROM l) HAVING count(*) > 0
