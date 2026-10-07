# dbt_ie_supermarket_analytics

Transforms the scraped retailer data in S3 with dbt + DuckDB. Layers:

- `staging/` `stg_<retailer>`: rename and cast raw records, one row per record (views).
- `intermediate/` `int_<retailer>`: business logic on a shared 46-column contract (views; SuperValu is a table).
- `marts/`: `fct_product_listings` (all retailers, one row per product per run), `fct_product_price_daily` (append-only daily prices, incremental) and `fct_price_history` (price versions derived from the daily facts).
- `int_all_supermarket_products` writes Parquet for `ml/embeddings.py`; `int_product_title_embeddings` reads the embeddings back.
- `seeds/`: SuperValu taxonomy. `macros/`: shared logic (units, price parsing, promotions, allergens, dietary flags, text cleaning, run_date).

## Running dbt (Fusion)

This project runs on **dbt Fusion** (`2.0.6`, binary at `~/.local/bin/dbt`). With the repo venv active,
`dbt` resolves to `.venv/bin/dbt` (the dbt-core shim), which crashes on Python 3.14 with a
`mashumaro ... UnserializableField` traceback. Use the Fusion binary explicitly:

```bash
alias dbtf=~/.local/bin/dbt        # already in ~/.zshrc; or call ~/.local/bin/dbt directly
dbtf build --project-dir dbt --vars '{run_date: 2026-08-05}'
dbtf test  --project-dir dbt --select "test_type:unit"
# keep production S3 untouched (writes Parquet locally instead):
dbtf build --project-dir dbt --vars '{run_date: 2026-08-05, parquet_output_prefix: /tmp/processed}'
```

`--static-analysis off` is not needed: the project sets no `static_analysis` config and the default
works. `--static-analysis strict` still fails on `list_filter(json_keys(...))` (Fusion bug).

## Vars

- `run_date` (YYYY-MM-DD): raw partition read (`raw/{retailer}/{run_date}/`) and the `scraped_date` stamped on rows. Defaults to today (UTC). Pass it explicitly for any scheduled or backfill run.
- `parquet_output_prefix`: where dbt writes Parquet (default `s3://ie-supermarket-data/processed`); override to test.
- `embeddings_input_prefix`: where dbt reads embeddings (default same prefix).

## Price history

`fct_product_price_daily` is incremental and never full-refreshed; to backfill replay run_dates in any order, e.g. `dbtf run --select +fct_product_price_daily --vars '{run_date: 2026-08-05}'`, then `dbtf run --select fct_price_history`.

See `docs/dbt_orchestration_plan.md` for scheduled runs.
