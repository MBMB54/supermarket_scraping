# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, etc.) working in this repository.


## Package Manager

This project uses `uv`. Always run scripts via `uv run python <script>`.

Dependency groups:
- `uv sync` — core only (boto3, polars, requests)
- `uv sync --group scraper` — adds aiohttp, playwright, patchright, curl-cffi
- `uv sync --group ids` — lightweight ID scripts (no browser)
- `uv sync --group notebook --group ml` — analysis and ML
- `uv sync --group ml-embed` — slim embedding framework (`ml_common`): sentence-transformers, transformers, pillow, s3fs
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

`dbt/` (project `dbt_ie_supermarket_analytics`, profile of the same name) transforms the raw scraped data using **dbt-duckdb** — DuckDB reads the gzipped JSONL directly from S3 via `httpfs`, no warehouse load step. The project runs on **dbt Fusion 2.0.9** (binary `~/.local/bin/dbt`, alias `dbtf`; embeds DuckDB v1.5.5, upgrade with `dbt system update`, previous binary kept in `~/supermarket_backups/dbt-bin/`). With the repo venv active, plain `dbt` resolves to the dbt-core shim in `.venv/bin/dbt`, which crashes on Python 3.14 — use `dbtf` or the full path. See `dbt/README.md`.

**Running**: no `--static-analysis off` is needed; the project sets no `static_analysis` config and the default works (`--static-analysis strict` still fails on `list_filter(json_keys(...))`). Pass `--vars '{run_date: YYYY-MM-DD}'` — it selects the raw partition and sets `scraped_date`. The var `parquet_output_prefix` (default `s3://ie-supermarket-data/processed`) is where `int_all_supermarket_products` writes its Parquet; **override it with a local dir when testing** or the build overwrites the production file that `ml/embeddings.py` reads.

Layers, one model per retailer (`aldi`, `tesco`, `dunnes`, `supervalu`) at each layer:
- **sources** (`models/staging/raw_s3.yml`) — `external_source` points at `s3://ie-supermarket-data/raw/{retailer}/{run_date}/*.jsonl.gz`; `run_date` (`--vars '{run_date: YYYY-MM-DD}'`, default today UTC) selects the partition. With `use_landing: true` (the scheduled Batch job) the source instead reads the Parquet copy `s3://ie-supermarket-data/landing/{retailer}/scraped_date={run_date}/data.parquet` written by `infra/dbt/landing.py`.
- **staging** (`models/staging/stg_*.sql`) — one row per raw JSON record, flattened/renamed to a common column set (id, title, brand, price, promotion fields, category hierarchy, etc). Materialized as views. Exception: every staging model drops stale-ID rows via the `is_stale_id_error` macro (`Not found in any store`, `HTTP 404`, `No product returned`, or an error containing `product-not-found` for Tesco's GraphQL 404; these are always `data IS NULL`, the ID feed lags the sites), keep it in sync with `NOT_FOUND_ERRORS`/`NOT_FOUND_MARKERS` in `scraper_common/quality.py`. Genuine fetch failures are deliberately left in so `assert_scrape_error_rate` can catch them.
- **intermediate** (`models/intermediate/int_*.sql`) — all four models emit an identical schema, in the same order (SuperValu is the reference — see its final `SELECT`), so they're safely unionable; a retailer missing a given real-world field emits an explicitly-typed `NULL` for it instead of omitting the column. Dietary-flag columns beyond the base seven (`is_dairy_free`, `is_sugar_free`, `has_no_added_sugar`, `is_diabetic_friendly`) are currently SuperValu-only (derived from its category tree / `attributes.dietary`); the other three emit typed `NULL`. Shared logic lives in two macros: `normalize_text(col)` (lowercase/strip-accents/punctuation cleanup, used for `title`/`brand`) and `clean_title(col)` (applied on top, strips pack-size/weight/duration quantifiers like `500g`/`8 pack`/`2 x 400g` for the `title_cleaned` column — ported from `remove_quantities_batch` in `notebooks/embeddings.ipynb`, improves title-embedding retrieval). Beyond normalization: unit-price normalization to kg/l, and allergen/dietary-flag derivation, preferring explicit raw boolean/structured fields (e.g. SuperValu `attributes.vegan`, Tesco `details.allergenInfo`) over free-text parsing where the raw data supports it. `int_all_supermarket_products` selects `id, supermarket, title, title_cleaned, image_url` from `fct_product_listings` and is materialized `external`, writing a Parquet file back to `s3://ie-supermarket-data/processed/`. `int_product_title_embeddings` reads precomputed title embeddings back from S3 (path keyed by the `embedding_model` var) rather than computing them in dbt.
- **marts** — `fct_product_listings` (four retailers unioned), `fct_product_price_daily` (incremental, `delete+insert` on `supermarket, id, scraped_date`, never full-refresh) and `fct_price_history` (versions with `valid_from`/`valid_to`/`is_current`, derived from the daily table so backfills in any order are correct). The old snapshot is gone.

**Scheduled build (deployed in shadow mode 2026-10-27; production cutover pending)**: `infra/dbt/` holds the Batch image and entrypoints; plan, measurements and rollout order are in `docs/dbt_orchestration_plan.md`. `run_dbt.py` classifies each retailer from the scrapers' `_run_summary` files and builds only those that are complete and passed (a degraded day skips the rest and the `processed/` export, a later slot adds late ones), `landing.py` writes one Parquet file per retailer-day (`landing/`), dbt-core builds target `batch` with the marts in a DuckLake catalog (`ducklake/` + a catalog file under `dbt/state/`, pushed only after a green build), and `compact_raw.py` merges each old retailer-day's raw batches into one `compacted.jsonl.gz` (it deletes the originals; run it with `--dry-run`/`--only-date` first and the bucket is versioned: deletes are recoverable until the lifecycle rule expires noncurrent versions). The Batch job excludes `test_type:unit` and runs DuckDB 2.0 (`duckdb>=2.0.0`; its DuckLake storage format is one-way, so every lake reader needs 2.0, see `docs/dbt_orchestration_plan.md`). Tests: `uv run --group test pytest tests/test_dbt_infra.py`.

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

## Embedding framework (`ml_common/`)

Model-agnostic, incremental embeddings for the product catalogue. A frozen `EmbeddingSpec` (`specs.py`: pinned model revision, modality `text`/`image`/`image_text`, literal prompt prefix, normalisation, Matryoshka `dim`, dtype) defines a vector; `spec_id` is a hash of everything that changes the vectors and names its table. `diff.py` embeds only rows whose `(id, supermarket, content_key)` has no `ok` row for that spec, so changed titles/images re-embed and unchanged ones never do. `Storage` is one interface over a local dir, `s3://` or `hf://buckets/...`; `SentenceTransformerEmbedder` loads lazily and returns an error result (never a silent skip) for items it cannot decode. Registry: `harrier-title` (incumbent title model), `gemma2-text`, `gemma2-image`, `gemma2-image-text` and their `-t140` (image_tokens=140) variants. The image store (`images.py`, `image_map.py`, `cli/images.py`) downloads original image bytes incrementally into a content-addressed layout; `ml/download_images_bytes_hf.py` is superseded. Full design: `docs/ml_framework.md`. Tests: `uv run --group test pytest tests/test_ml_common.py` (fake embedder, no network).

```bash
uv sync --group ml-embed
uv run python -m ml_common.cli.embed --spec harrier-title --catalogue cat.parquet --store ./embedding_store --limit 50
```

```bash
uv run python -m ml_common.cli.images --store ./embedding_store fetch --catalogue cat.parquet --limit 100   # incremental; exits 1 over the failure-rate gate
uv run python -m ml_common.cli.images --store ./embedding_store seed                                       # import legacy HF image shards
```

Env: `EMBEDDINGS_STORE` (default `./embedding_store`), `CATALOGUE_URI` (default the dbt-written `processed/all_supermarket_products.parquet`), `CHUNK_ROWS`, `IMAGE_CONCURRENCY`/`IMAGE_DELAY`/`IMAGE_TIMEOUT`/`IMAGE_MAX_ATTEMPTS`/`IMAGE_USER_AGENT`/`IMAGE_MAX_ERROR_RATE`/`IMAGE_MAX_NOT_FOUND_RATE`. Store layout: `embeddings/<spec_id>/{manifest.json,part-*.parquet}`, `images/<sha256[:2]>/<sha256>` (original bytes, no extension) and `image_map/part-*.parquet` (all append-only, never rewritten). Image specs read `image_url` from the catalogue and join `image_sha256` from the image map, so run `fetch` first. Tests need `--group test` (aiohttp, pillow, pyarrow). The existing `processed/embeddings/<slug>/product_title_embeddings.parquet` that dbt and `ml/agents.py` read is still produced only by `ml/embeddings.py`.
