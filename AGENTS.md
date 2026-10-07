# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, etc.) working in this repository.


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

`dbt/` (project `dbt_ie_supermarket_analytics`, profile of the same name) transforms the raw scraped data using **dbt-duckdb** — DuckDB reads the gzipped JSONL directly from S3 via `httpfs`, no warehouse load step. The project runs on **dbt Fusion 2.0.6** (binary `~/.local/bin/dbt`, alias `dbtf`). With the repo venv active, plain `dbt` resolves to the dbt-core shim in `.venv/bin/dbt`, which crashes on Python 3.14 — use `dbtf` or the full path. See `dbt/README.md`.

**Running**: no `--static-analysis off` is needed; the project sets no `static_analysis` config and the default works (`--static-analysis strict` still fails on `list_filter(json_keys(...))`). Pass `--vars '{run_date: YYYY-MM-DD}'` — it selects the raw partition and sets `scraped_date`. The var `parquet_output_prefix` (default `s3://ie-supermarket-data/processed`) is where `int_all_supermarket_products` writes its Parquet; **override it with a local dir when testing** or the build overwrites the production file that `ml/embeddings.py` reads.

Layers, one model per retailer (`aldi`, `tesco`, `dunnes`, `supervalu`) at each layer:
- **sources** (`models/staging/raw_s3.yml`) — `external_source` points at `s3://ie-supermarket-data/raw/{retailer}/{run_date}/*.jsonl.gz`; `run_date` defaults to today and can be overridden with `--vars '{run_date: YYYY-MM-DD}'` to build off a specific scrape.
- **staging** (`models/staging/stg_*.sql`) — one row per raw JSON record, flattened/renamed to a common column set (id, title, brand, price, promotion fields, category hierarchy, etc). Materialized as views. Exception: `stg_supervalu` and `stg_dunnes` drop rows with `error = 'Not found in any store'` (stale product IDs from the slower ID-refresh cycle — always `data IS NULL`); genuine fetch failures are deliberately left in so `assert_scrape_error_rate` can catch them.
- **intermediate** (`models/intermediate/int_*.sql`) — all four models emit an identical schema, in the same order (SuperValu is the reference — see its final `SELECT`), so they're safely unionable; a retailer missing a given real-world field emits an explicitly-typed `NULL` for it instead of omitting the column. Dietary-flag columns beyond the base seven (`is_dairy_free`, `is_sugar_free`, `has_no_added_sugar`, `is_diabetic_friendly`) are currently SuperValu-only (derived from its category tree / `attributes.dietary`); the other three emit typed `NULL`. Shared logic lives in two macros: `normalize_text(col)` (lowercase/strip-accents/punctuation cleanup, used for `title`/`brand`) and `clean_title(col)` (applied on top, strips pack-size/weight/duration quantifiers like `500g`/`8 pack`/`2 x 400g` for the `title_cleaned` column — ported from `remove_quantities_batch` in `notebooks/embeddings.ipynb`, improves title-embedding retrieval). Beyond normalization: unit-price normalization to kg/l, and allergen/dietary-flag derivation, preferring explicit raw boolean/structured fields (e.g. SuperValu `attributes.vegan`, Tesco `details.allergenInfo`) over free-text parsing where the raw data supports it. `int_all_supermarket_products` unions `id, supermarket, title, title_cleaned` across all four and is materialized `external`, writing a Parquet file back to `s3://ie-supermarket-data/processed/`. `int_product_title_embeddings` reads precomputed title embeddings back from S3 (path keyed by the `embedding_model` var) rather than computing them in dbt.
- **marts** — `fct_product_listings` (four retailers unioned), `fct_product_price_daily` (incremental, `delete+insert` on `supermarket, id, scraped_date`, never full-refresh) and `fct_price_history` (versions with `valid_from`/`valid_to`/`is_current`, derived from the daily table so backfills in any order are correct). The old snapshot is gone.

Generic tests (`unique`/`not_null` on each retailer's `id` column, plus `not_null` on `supermarket`) live in `models/staging/schema.yml` and `models/intermediate/schema.yml`.

## Dockerfiles

- `Dockerfile.ids` — lightweight, `uv sync --group ids`, used for aldi ID scraping
- `Dockerfile.scraper` — full scraper, `uv sync --group scraper`, includes patchright + Chrome, used for all API scripts and tesco ID scraping

## Conventions

- Do not add new boolean attribute columns for minor product characteristics (decaf, craft, protein, ...). Only genuine dietary restrictions affecting a large share of the catalogue get a flag; the current set is complete. Everything else is inferred from the product title at search time.
- Project skills/workflows live in `.agents/skills/` (`check-jobs`, `submit-jobs`); `.claude/skills` is a symlink to it.

## Shared scraper module and review

`scraper_common/` holds the code shared by the scraper scripts: `Storage` (S3 or `OUTPUT_DIR` local), `RunConfig`/`chunk_slice`, `request_with_retry`, `read_ids`/`run_ids_job` (guarded `ids/latest` overwrite), `run_scrape` (batching, checkpoints, data-quality gate, `_run_summary_chunk{N}.json`) and `Storefront` (SuperValu/Dunnes). Scripts add the repo root to `sys.path`; both Dockerfiles copy the package. Gate details: `docs/scraper_data_quality.md`. Tests: `uv run --group test pytest`.

Before finishing a change to scrapers/infra, apply `docs/REVIEW.md` (or invoke the `code-reviewer` subagent in `.claude/agents/`). Alert infra drafts live in `infra/` (not applied automatically).
