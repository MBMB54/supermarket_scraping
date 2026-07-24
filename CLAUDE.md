# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Package Manager

This project uses `uv`. Always run scripts via `uv run python <script>`.

Dependency groups:
- `uv sync` — core only (boto3, polars, requests)
- `uv sync --group scraper` — adds aiohttp, playwright, patchright, curl-cffi
- `uv sync --group ids` — lightweight ID scripts (no browser)
- `uv sync --group notebook --group ml` — analysis and ML
- `uv sync --group dbt` — dbt-core, dbt-duckdb
- `uv sync --group dev` — ruff, pre-commit
- `uv sync --group test` — pytest

## Linting & Formatting

```bash
uv run ruff check --fix .   # lint with auto-fix
uv run ruff format .        # format
```

Line length is 100. Notebooks are excluded from pre-commit hooks.

## CI/CD

Pushing to `local_implementation` automatically builds both Docker images, registers new AWS Batch job definitions, and deploys the Lambda. To push without triggering this, add `[skip ci]` anywhere in the commit message.

## Scraper Architecture

Two-stage pipeline per retailer:
1. **ID script** (`*_ids.py`) — discovers product IDs, writes to `s3://ie-supermarket-data/raw/{retailer}/ids/date={YYYY-MM-DD}/` and overwrites `raw/{retailer}/ids/latest/{retailer}_product_ids.parquet`
2. **API script** (`*_api.py`) — reads IDs from `latest/`, splits across `TOTAL_CHUNKS` by `CHUNK_ID`, writes gzipped JSONL batches to `raw/{retailer}/{YYYY-MM-DD}/`

To test an API script locally on a small subset:
```bash
CHUNK_ID=0 TOTAL_CHUNKS=200 uv run python tesco/tesco_api.py
```

## AWS

**S3 bucket**: `ie-supermarket-data` (eu-west-1)

**Batch job definitions**:
- `ie_supermarket_batch_job_definition` — scraper image (playwright, all API scripts)
- `ie_supermarket_ids_job_definition` — lightweight IDs image (aldi only)

**Job queue**: `getting-started-fargate-job-queue`

Tesco, aldi, supervalu, and dunnes are all active in the Lambda orchestrator.

## dbt

`dbt/` (project `dbt_ie_supermarket_analytics`, profile of the same name) transforms the raw scraped data using **dbt-duckdb** — DuckDB reads the gzipped JSONL directly from S3 via `httpfs`, no warehouse load step. The project runs on **dbt Fusion** (`dbt` on PATH, currently `2.0.0-preview.202`); `uv sync --group dbt` (dbt-core/dbt-duckdb) is also available as a fallback but its `dbt-common`/`mashumaro` pin doesn't import under this repo's `==3.14.*` Python — use a scratch venv on Python ≤3.13 if you need it.

**Fusion + `--static-analysis`**: `dbt_project.yml` sets `+static_analysis: strict` project-wide. As of `2.0.0-preview.202`, Fusion's strict static-analysis pass has two false-positive bugs that block `dbt run`/`dbt build` (though `dbt show`/`dbt compile` are unaffected, since they skip that pass): it can't resolve the literal S3 glob path in the staging models' `source()` (`TableNotFound`/`does not exist in schema ...` on a staging model), and it can't resolve `list_filter(json_keys(...), k -> ...)` (used for allergen derivation in `int_supervalu`/`int_dunnes`; `FunctionResolutionFailed: list_filter ... actual: (varchar, blob)`). Both are pure static-analysis bugs — the underlying DuckDB execution is correct in both cases (verified directly). Workaround: run with `--static-analysis off`, e.g. `dbt build --project-dir dbt --static-analysis off`. Don't change the `dbt_project.yml` default without checking whether anything relies on the lineage/schema output `strict` produces.

Layers, one model per retailer (`aldi`, `tesco`, `dunnes`, `supervalu`) at each layer:
- **sources** (`models/staging/raw_s3.yml`) — `external_source` points at `s3://ie-supermarket-data/raw/{retailer}/{run_date}/*.jsonl.gz`; `run_date` defaults to today and can be overridden with `--vars '{run_date: YYYY-MM-DD}'` to build off a specific scrape.
- **staging** (`models/staging/stg_*.sql`) — one row per raw JSON record, flattened/renamed to a common column set (id, title, brand, price, promotion fields, category hierarchy, etc). Materialized as views.
- **intermediate** (`models/intermediate/int_*.sql`) — all four models emit an identical 42-column schema, in the same order (SuperValu is the reference — see its final `SELECT`), so they're safely unionable; a retailer missing a given real-world field emits an explicitly-typed `NULL` for it instead of omitting the column. Shared logic lives in two macros: `normalize_text(col)` (lowercase/strip-accents/punctuation cleanup, used for `title`/`brand`) and `clean_title(col)` (applied on top, strips pack-size/weight/duration quantifiers like `500g`/`8 pack`/`2 x 400g` for the `title_cleaned` column — ported from `remove_quantities_batch` in `notebooks/embeddings.ipynb`, improves title-embedding retrieval). Beyond normalization: unit-price normalization to kg/l, and allergen/dietary-flag derivation, preferring explicit raw boolean/structured fields (e.g. SuperValu `attributes.vegan`, Tesco `details.allergenInfo`) over free-text parsing where the raw data supports it. `int_all_supermarket_products` unions `id, supermarket, title, title_cleaned` across all four and is materialized `external`, writing a Parquet file back to `s3://ie-supermarket-data/processed/`. `int_product_title_embeddings` reads precomputed title embeddings back from S3 (path keyed by the `embedding_model` var) rather than computing them in dbt.
- **snapshots** (`snapshots/scd_product_prices.sql`) — SCD (`check` strategy) over `price`/`was_price`/`unit_price` across all four `int_*` models (not staging — those lack a uniform schema), giving price history over time.

Generic tests (`unique`/`not_null` on each retailer's `id` column, plus `not_null` on `supermarket`) live in `models/staging/schema.yml` and `models/intermediate/schema.yml`.

## Dockerfiles

- `Dockerfile.ids` — lightweight, `uv sync --group ids`, used for aldi ID scraping
- `Dockerfile.scraper` — full scraper, `uv sync --group scraper`, includes patchright + Chrome, used for all API scripts and tesco ID scraping
