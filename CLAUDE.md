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

`dbt/` (project `dbt_ie_supermarket_analytics`, profile of the same name) transforms the raw scraped data using **dbt-duckdb** — DuckDB reads the gzipped JSONL directly from S3 via `httpfs`, no warehouse load step. Run commands with `uv run dbt <command> --project-dir dbt` (requires the `dbt` group; a local `profiles.yml` with AWS credentials configured for the `dbt_ie_supermarket_analytics` profile is not checked in).

Layers, one model per retailer (`aldi`, `tesco`, `dunnes`, `supervalu`) at each layer:
- **sources** (`models/staging/raw_s3.yml`) — `external_source` points at `s3://ie-supermarket-data/raw/{retailer}/{run_date}/*.jsonl.gz`; `run_date` defaults to today and can be overridden with `--vars '{run_date: YYYY-MM-DD}'` to build off a specific scrape.
- **staging** (`models/staging/stg_*.sql`) — one row per raw JSON record, flattened/renamed to a common column set (id, title, brand, price, promotion fields, category hierarchy, etc). Materialized as views.
- **intermediate** (`models/intermediate/int_*.sql`) — per-retailer cleaning (title/brand normalization, unit-price normalization to kg/l, allergen/dietary-flag derivation). `int_all_supermarket_products` unions all retailers and is materialized `external`, writing a Parquet file back to `s3://ie-supermarket-data/processed/`. `int_product_title_embeddings` reads precomputed title embeddings back from S3 (path keyed by the `embedding_model` var) rather than computing them in dbt.
- **snapshots** (`snapshots/scd_product_prices.sql`) — SCD (`check` strategy) over price/quantity/promotion columns across all four staging models, giving price history over time.

Generic tests (`unique`/`not_null` on each retailer's id column) live in `models/staging/schema.yml`.

## Dockerfiles

- `Dockerfile.ids` — lightweight, `uv sync --group ids`, used for aldi ID scraping
- `Dockerfile.scraper` — full scraper, `uv sync --group scraper`, includes patchright + Chrome, used for all API scripts and tesco ID scraping
