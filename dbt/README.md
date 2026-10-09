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

## Raw source reads

Sources use `read_json(..., union_by_name=true)` (`models/staging/raw_s3.yml`). Plain auto-detection samples one schema for the whole glob, so a new tag key in `data.attributes` on a later file (e.g. `back To School`) fails the read with `has unknown key`; `union_by_name` infers per file and merges. Full-day read times (dunnes/supervalu/aldi/tesco): about 2.6 / 6.4 / 0.5 / 2.6 s, versus 1.2 / 1.5 / 0.5 / 2.3 s before. Stale ids (`Not found in any store`, `HTTP 404`) are dropped in staging via `is_stale_id_error`.

## Unit prices

`unit_qty_normalised` turns pack sizes into kg / l / m. For counted items (blank unit, Tesco `SHT`, Aldi each/pack) it is the item count, giving a per-item `price_per_unit_normalised` (4 dp). If the title states a different pack count (N pack, Npk, pack of N, N x), or the unit is not a count (m2), it is 1.0, i.e. price per pack.

## Price history

`fct_product_price_daily` is incremental and never full-refreshed; it holds one row per product per scrape date. `fct_price_history` derives price versions from it, so load order does not matter.

- `is_current`: latest version of the product. `is_active`: the product was seen on its retailer's latest loaded day (`active_grace_days` var, default 0, tolerates missed days). `last_seen_date` is on every version row.
- Current prices of live products: `SELECT * FROM fct_price_history WHERE is_current AND is_active`.

Backfill (replays raw partitions oldest first, no tests or gates; only retailers with a partition and some data on that day are built):

```bash
python dbt/scripts/backfill.py --start 2026-02-01 --end 2026-10-08 \
    --profiles-dir <dir with profiles.yml> --db <the duckdb file in that profile> \
    --parquet-prefix <local dir> --dbt ~/.local/bin/dbt
dbtf build --project-dir dbt --select fct_price_history assert_price_history_consistent
```

Per-day load stats are written to table `backfill_stats` (rows, null titles, null prices): days with mostly NULL prices (e.g. Tesco 429 days) contribute only their priced rows to the history.

See `docs/dbt_orchestration_plan.md` for scheduled runs.
