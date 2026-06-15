{{ config(materialized='view') }}

WITH cleaned AS (
SELECT *
FROM {{ ref('stg_supervalu') }}
)

SELECT *
FROM cleaned 