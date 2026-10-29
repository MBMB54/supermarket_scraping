{{ config(materialized='table') }}
{{ embedding_view('gemma2_text') }}
