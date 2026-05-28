{{ config(materialized='table') }}

SELECT
    tpnc AS id,
    data.title AS title,
    data.brandName AS brand,
    data.details.packSize[1].value AS quantity,
    data.details.packSize[1].units AS unit,
    data.price.unitPrice AS unit_price,
    data.price.unitOfMeasure AS unit_of_measure,
    data.price.actual AS price,
    data.promotions[1].startDate AS promotion_start_date,
    data.promotions[1].endDate AS promotion_end_date,
    data.promotions[1].metaData.seo.afterDiscountPrice AS promotion_price,
    data.superDepartmentName AS department_1,
    data.departmentName AS department_2,
    data.aisleName AS department_3,
    data.shelfName AS department_4,
    data.description AS item_description,
    CURRENT_DATE AS scraped_date
FROM {{ source('external_source', 'tesco') }}