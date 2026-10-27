# Infrastructure and operations guide

**Last verified: 2026-10-27, git commit `078b031` (branch `local_implementation`; the dbt-stack code was last changed in `bdc1d61`).** Code and config were read from the working tree; AWS state was queried read-only (S3, Batch, EventBridge, Scheduler, Lambda, CloudFormation, IAM, ECR, SNS) on the same day. A second engineer was deploying the dbt pipeline while this was written, so section 5.8 describes a moving target: re-check it with the commands given there before relying on it.

This guide is meant to be enough, on its own, for someone who has never seen the project to understand it and run it day to day. Anything I could not confirm is marked **unverified**.

## Table of contents

1. [What the project is, and the end-to-end picture](#1-what-the-project-is-and-the-end-to-end-picture)
2. [Repository map](#2-repository-map)
3. [Setup for a new person](#3-setup-for-a-new-person)
4. [The scraping pipeline in detail](#4-the-scraping-pipeline-in-detail)
5. [AWS infrastructure inventory](#5-aws-infrastructure-inventory)
6. [Where the data is stored](#6-where-the-data-is-stored)
7. [The dbt transformation layer](#7-the-dbt-transformation-layer)
8. [The daily timeline and the dbt Batch job](#8-the-daily-timeline-and-the-dbt-batch-job)
9. [Tests and quality gates](#9-tests-and-quality-gates)
10. [Operations runbook](#10-operations-runbook)
11. [Cost and security](#11-cost-and-security)
12. [Glossary and where to look next](#12-glossary-and-where-to-look-next)

---

## 1. What the project is, and the end-to-end picture

The project collects the public prices of grocery products from four Irish supermarkets (Tesco, Aldi, SuperValu, Dunnes Stores) every day, keeps the raw responses, and turns them into tables that record each product's price history. Downstream code uses those tables for analysis, product search and machine-learning experiments (embeddings of product titles and images, a SuperValu category taxonomy). The AWS account is `557690579730`, the region is `eu-west-1` (Ireland), and all data lives in one S3 bucket, `ie-supermarket-data`.

**End-to-end, in one paragraph.** Every day at 08:00 UTC an EventBridge rule wakes a small Lambda function. The Lambda submits AWS Batch jobs: for each retailer, an optional job that refreshes the list of product IDs, then five "chunk" jobs that each fetch one fifth of the IDs from the retailer's website or API. Each chunk writes gzipped JSON-lines files ("raw" data) plus a small summary file into S3 and exits non-zero if its own data-quality gate fails. Tesco takes about 2h50m; the others finish within about 1h15m. A scheduled Lambda at 14:00 UTC emails if any summary is missing or bad. A separate dbt job (built, being rolled out, **not yet running on a schedule**) will, per retailer, check that the scrape finished and passed, convert the raw files to Parquet, run dbt (a SQL transformation tool) with DuckDB (an embedded analytical database) to produce staging, intermediate and "mart" tables, and store the marts, which hold the price history, in DuckLake (Parquet files in S3 plus a small catalog file). Today the same dbt project is still run by hand on a laptop against a local DuckDB file, `supermarket_data.db`, and that file is what notebooks and `ml/` scripts read.

```
                         DAILY FLOW (all times UTC; 'live' = running today, 'rollout' = built, not scheduled)

  EventBridge rule                      Lambda                       AWS Batch (Fargate containers, queue
  supermarket-scraper-daily  ---------> supermarket_batch_scheduler  getting-started-fargate-job-queue)
  cron(0 8 * * ? *)  [live]             (lambda/handler.py)
                                              | submit_job x ~23
                                              v
        +---------------------------------------------------------------------------------------+
        | aldi-ids -> aldi chunk 0..4        supervalu-ids -> supervalu chunk 0..4              |
        | tesco chunk 0..4 (no ids job)      dunnes chunk 0..4 (no ids job)                     |
        +----------------------------------+----------------------------------------------------+
                                           | each chunk: HTTP fetch of ~1/5 of the product IDs
                                           v
  S3 ie-supermarket-data
    raw/{retailer}/ids/latest/{retailer}_product_ids.parquet  <-- read by chunks (ID lists)
    raw/{retailer}/{date}/{retailer}_raw_..._chunkN_batchNNNN.jsonl.gz   (the source of truth)
    raw/{retailer}/{date}/_run_summary_chunkN.json                       (gate result per chunk)
           |                                           |
           | 14:00  summary-check Lambda [live]        | 11:30, 12:30, 13:30, 14:30  Scheduler [shadow: ENABLED]
           |  -> SNS email if a summary is missing/bad |  -> Batch job ie_supermarket_dbt_job_definition
           v                                           v
     SNS topic supermarket-scraper-alerts    precheck per retailer -> landing Parquet -> dbt build -> push catalog
     (email; also gets Batch FAILED events)        |
                                                   v
                      landing/{retailer}/scraped_date={date}/data.parquet        (derived cache)
                      ducklake/ + dbt/state/catalog.ducklake                     (marts = price history)
                      processed/all_supermarket_products.parquet                 (slim catalogue export)
                                                   |
        local today:  dbtf build on laptop  ----->  supermarket_data.db  (DuckDB file, 648 MB)
                                                   |
                                                   v
                       consumers: ml/ (embeddings, agents, HF export), notebooks/, ad-hoc SQL
```

**Live versus rollout, plainly.** The scraping half is live and has run daily for months (raw folders exist from 2026-02-01 for Tesco). The dbt Batch pipeline's CloudFormation stack `supermarket-dbt-batch` was created on 2026-10-27 with "shadow" storage prefixes (so it cannot touch production dbt state or the `processed/` file `ml/` reads). Three shadow days have been built on Batch and the dbt schedule is enabled in shadow mode; production cutover, raw compaction and the S3 lifecycle rules are still pending (section 5.8).

---

## 2. Repository map

Root: `/Users/brianbarry/supermarket_scraping` (GitHub repo `MBMB54/supermarket_scraping`, per [docs/iam_setup.md](iam_setup.md)).

| Path | What it is |
|---|---|
| [aldi/](../aldi/) | `aldi_ids.py` (reads Aldi's product sitemap), `aldi_api.py` (fetches `api.aldi.ie/v2/products/{id}`), `ie_aldi_ids.csv` (old ID list, not used by the scripts). |
| [tesco/](../tesco/) | `tesco_ids.py` (browser crawl of category pages, runs **locally**, not on AWS), `tesco_api.py` (GraphQL calls to `xapi.tesco.com`), `graphql_query.txt` (the query sent), `ie_tesco_ids.csv` (old list). |
| [supervalu/](../supervalu/) | `supervalu_ids.py` (sitemap plus category crawl over 6 stores), `supervalu_api.py` (storefront gateway). |
| [dunnes/](../dunnes/) | `dunnes_ids.py` (sitemap, currently blocked by Cloudflare, not scheduled), `dunnes_api.py` (storefront gateway). |
| [scraper_common/](../scraper_common/) | Code shared by all scrapers: `config.py` (`RunConfig`, `chunk_slice`), `storage.py` (`Storage`), `http.py` (`request_with_retry`), `ids.py` (read and publish ID lists), `runner.py` (`run_scrape`), `quality.py` (gate), `storefront.py` (SuperValu/Dunnes client). Section 4.4. |
| [lambda/](../lambda/) | `handler.py`, the function `supermarket_batch_scheduler` that submits the daily Batch jobs. |
| [dbt/](../dbt/) | The dbt project `dbt_ie_supermarket_analytics`: `models/` (staging, intermediate, marts), `tests/`, `macros/`, `seeds/` (SuperValu taxonomy CSVs), `scripts/backfill.py`, `README.md`. Section 7. |
| [infra/](../infra/) | AWS definitions: `batch-alerts.yaml` (alert stack, deployed), `dbt-batch.yaml` (dbt/compaction stack, deployed 2026-10-27 in shadow mode), `dbt/` (the dbt Batch job's Python entrypoints, Dockerfile, profiles, S3 lifecycle JSON), `iam/` (draft least-privilege policies, mostly not applied), `README.md`. |
| [ml/](../ml/) | Experiments run by hand: `embeddings.py` (title embeddings to `processed/embeddings/`), `agents.py` (a Pydantic AI search agent over the DB), `classify_orphan_products.py` and `apply_review_decisions.py` (SuperValu taxonomy work), `export_data_to_hf.py`, `download_images_bytes_hf.py` (superseded), `image_embeddings_hf.py`, `review_console.html`, `orphan_classifications.jsonl`. |
| [ml_common/](../ml_common/) | A model-agnostic embedding framework: `specs.py` (frozen `EmbeddingSpec`), `diff.py` (embed only new/changed rows), `storage.py` (local, `s3://` or `hf://buckets/...`), `images.py`/`image_map.py` (incremental image downloader), `cli/` (`embed`, `images`). Design in [ml_framework.md](ml_framework.md). |
| [docs/](.) | Design notes and investigations; index in section 12. |
| [tests/](../tests/) | pytest suites (section 9.1). |
| [scripts/](../scripts/) | `run_tesco_ids_local.sh` + `launchd/com.supermarket.tesco-ids.plist` (weekly local Tesco ID refresh on a Mac), `dunnes_ids_from_raw.py` (rebuild Dunnes IDs from past raw data). |
| [notebooks/](../notebooks/) | Jupyter exploration (`tesco.ipynb`, `aldi.ipynb`, `embeddings.ipynb`, ...). Excluded from linting and pre-commit. Read the local DuckDB. |
| [Dockerfile.scraper](../Dockerfile.scraper) | Image `scraper-<sha>`: Python 3.14, `uv sync --group scraper`, patchright + Chrome, copies `scraper_common/` and every `*_api.py`/`*_ids.py` except `aldi_ids.py`. Default command `python tesco_api.py`. |
| [Dockerfile.ids](../Dockerfile.ids) | Image `ids-<sha>`: lightweight, `--group ids` (curl-cffi), contains only `aldi_ids.py`. |
| [infra/dbt/Dockerfile.dbt](../infra/dbt/Dockerfile.dbt) | Image `dbt-<sha>`: Python 3.12, dbt-core + dbt-duckdb, the `dbt/` project and `infra/dbt/*.py`. |
| [pyproject.toml](../pyproject.toml), `uv.lock` | Dependencies in groups (section 3.1); `requires-python = "==3.14.*"`; ruff and pytest settings. |
| [AGENTS.md](../AGENTS.md) / [CLAUDE.md](../CLAUDE.md) | Guidance for AI coding agents working in the repo. `CLAUDE.md` only imports `AGENTS.md`. |
| `.github/workflows/` | `ci.yml` (lint + tests) and `aws.yml` (build/deploy on push to `local_implementation`). |
| `.agents/skills/` (`.claude/skills` symlinks to it) | Agent skills `check-jobs` and `submit-jobs`. `.claude/agents/code-reviewer.md` is a reviewer sub-agent that applies [REVIEW.md](REVIEW.md). |
| `supermarket_data.db` | Local DuckDB file, 648 MB, git-ignored (`*.db`). The only place today where the marts and embeddings tables live. Section 6.2. |
| `lk/`, `logs/`, `tesco_sample_output.txt` | `lk/` is an empty directory; `logs/` holds local dbt logs; the text file is a sample Tesco response. Not part of the pipeline. |
| [README.md](../README.md) | **Stale**: describes a UK/Ocado/Athena/QuickSight design that does not exist. Ignore it; use this guide. |

---

## 3. Setup for a new person

### 3.1 Python environment (uv)

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.14. Always run scripts as `uv run python <script>`.

```bash
git clone git@github.com:MBMB54/supermarket_scraping.git && cd supermarket_scraping
uv sync --group dev --group test          # linting + tests: enough for most work
```

Dependency groups (from [pyproject.toml](../pyproject.toml)); the base install is `requests, boto3, polars, python-dotenv, fsspec, tenacity`:

| Group | Adds | Use |
|---|---|---|
| `dev` | ruff, pre-commit | linting/formatting |
| `test` | pytest, aiohttp, pillow, pyarrow | `pytest` |
| `scraper` | aiohttp, playwright, patchright, curl-cffi, lxml, brotli | run any `*_api.py`, `tesco_ids.py` |
| `ids` | curl-cffi | run `aldi_ids.py` |
| `dbt` | dbt-core, dbt-duckdb, duckdb | dbt-core route (see 3.5 caveats) |
| `notebook`, `ml`, `agents` | duckdb, pandas, spaCy, transformers, mlflow, pydantic-ai, ... | notebooks and ML |
| `ml-embed` | sentence-transformers, transformers, pillow, s3fs, pyarrow, aiohttp | `ml_common` embedding framework |

Combine groups: `uv sync --group scraper --group test`.

### 3.2 AWS credentials and region

All code assumes region `eu-west-1` and bucket `ie-supermarket-data` (overridable with `AWS_REGION` and `S3_BUCKET`). Code uses boto3's default credential chain, so any of `aws configure`, `AWS_PROFILE`, or SSO works.

```bash
aws configure                     # or: aws configure sso
aws configure set region eu-west-1
aws configure set output json     # important, see below
aws sts get-caller-identity       # confirms who you are
```

Notes:
- `AWS_DEFAULT_OUTPUT` / `output=` must be a valid type. In this environment the default output type is broken (an invalid value gives "Unknown output type"), so always pass `--output json` or set `output=json`.
- On the maintainer's machine the CLI is configured with the account root user's keys. Do not copy that: see section 11.2.
- Minimum permissions for local scraper tests with `OUTPUT_DIR`: read of `raw/*/ids/latest/*`. For dbt on raw data: read of `raw/*`. For anything that writes to S3, you need write access to the prefix involved.
- Some shell setups (zsh) do not word-split variables, so `R="--region eu-west-1 --output json"; aws ... $R` fails. Use `export AWS_DEFAULT_REGION=eu-west-1 AWS_DEFAULT_OUTPUT=json` instead, as the commands below assume.

### 3.3 Linting, formatting and tests

```bash
uv run ruff check --fix .         # lint with auto-fix (line length 100)
uv run ruff format .              # format
uv run pytest -q                  # all tests (no network, no AWS needed)
uv run pytest tests/test_scraper_common.py -q
```

`pre-commit` is configured via the `dev` group (hooks exclude notebooks). Before finishing a change to scrapers or infra, apply the checklist in [REVIEW.md](REVIEW.md) (no duplicated S3/retry/chunk code outside `scraper_common/`, no swallowed exceptions, no secrets, Dockerfiles copy `scraper_common/`, ...).

### 3.4 Run one scraper chunk locally on a tiny subset

Setting `OUTPUT_DIR` makes `Storage` write everything (batches, checkpoint, summary) to a local directory instead of S3. IDs are still read from S3 unless `IDS_FILE` points to a local Parquet file.

```bash
uv sync --group scraper
OUTPUT_DIR=/tmp/out CHUNK_ID=0 TOTAL_CHUNKS=200 MAX_IDS=20 \
  uv run python aldi/aldi_api.py
ls -R /tmp/out
cat /tmp/out/raw/aldi/$(date -u +%F)/_run_summary_chunk0.json
echo $?                           # 0 = gate passed, 1 = gate failed
```

Same for the others: `tesco/tesco_api.py`, `supervalu/supervalu_api.py`, `dunnes/dunnes_api.py`. Useful variables: `CHUNK_ID`, `TOTAL_CHUNKS` (required), `MAX_IDS` (cap per chunk), `RUN_DATE` (force the date folder), `IDS_FILE` (local ID Parquet), `MAX_ERROR_RATE` / `MAX_EMPTY_RATE` / `MAX_NOT_FOUND_RATE`. Tesco also reads `TESCO_TOTAL_REQ_PER_SEC` (default 2.0) and `TESCO_MAX_ATTEMPTS` (default 8). Be polite: use small `MAX_IDS`. Running the ID scripts with `OUTPUT_DIR` set is the safe way to test them; without it they overwrite `ids/latest` in production (after the shrink guard).

### 3.5 Run dbt locally

There are two ways to run the same project, and the caveats matter.

**A. dbt Fusion (what the maintainer uses locally).** dbt Fusion is dbt Labs' newer single-binary engine. Version 2.0.9 is installed at `~/.local/bin/dbt` (alias `dbtf`) and embeds DuckDB 1.5.5. With the repo's virtualenv active, plain `dbt` resolves to `.venv/bin/dbt`, the dbt-core shim, which **crashes on Python 3.14** (a `mashumaro ... UnserializableField` traceback). So call Fusion explicitly.

You also need a profile named `dbt_ie_supermarket_analytics` in `~/.dbt/profiles.yml` (this file is outside the repo). The maintainer's has targets `dev` and `prod`, both pointing at the local file. A working template:

```yaml
dbt_ie_supermarket_analytics:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: /ABSOLUTE/PATH/TO/supermarket_scraping/supermarket_data.db
      threads: 1
      extensions: [httpfs, parquet, aws]
      secrets:
        - type: s3
          provider: credential_chain
      settings:
        s3_region: eu-west-1
```

```bash
dbtf seed  --project-dir dbt                                  # load the SuperValu taxonomy CSVs
dbtf build --project-dir dbt --vars '{run_date: 2026-10-26, parquet_output_prefix: /tmp/processed}'
dbtf test  --project-dir dbt --select "test_type:unit"        # unit tests (Fusion only, see 9.3)
```

Always pass `run_date` (it selects the raw partition `raw/{retailer}/{run_date}/` and stamps `scraped_date`) and **override `parquet_output_prefix` with a local directory when experimenting**: otherwise `int_all_supermarket_products` overwrites the production file `s3://ie-supermarket-data/processed/all_supermarket_products.parquet` that `ml/embeddings.py` reads. Do not pass `--static-analysis strict` (it fails on `list_filter(json_keys(...))`, a Fusion bug); the default works.

**B. dbt-core in a Python 3.12 virtualenv (what the Batch job uses).** Needed for the `batch` target (DuckLake). The dbt-core stack does not run on Python 3.14.

```bash
python3.12 -m venv /tmp/dbt312 && /tmp/dbt312/bin/pip install -r infra/dbt/requirements.txt
# requirements: dbt-core~=1.10, dbt-duckdb~=1.10, duckdb>=1.5.2,<2, boto3>=1.36
/tmp/dbt312/bin/dbt parse --project-dir dbt --profiles-dir infra/dbt --vars '{run_date: 2026-01-01}'
```

`duckdb<2` is pinned because DuckDB 2.0 rejects the `x -> ...` lambda syntax used in the models. The local `dev`/`prod` targets use dbt-duckdb too, but untested combinations (dbt-core on the 3.14 virtualenv) are **unverified**; use Fusion for local work.

---

## 4. The scraping pipeline in detail

### 4.1 Two stages per retailer

1. **ID script** (`*_ids.py`): finds the list of product IDs that currently exist on the retailer's site and writes it to S3 as Parquet. The scheduled Lambda only runs this stage for Aldi (daily) and SuperValu (daily job, but it skips itself unless the list is 7 days old).
2. **API script** (`*_api.py`): reads the ID list from `raw/{retailer}/ids/latest/`, fetches each product's details, and writes the responses (raw JSON) in batches of 100 records to `raw/{retailer}/{date}/`.

**Chunking.** The API stage is split into `TOTAL_CHUNKS = 5` parallel jobs. Chunk `CHUNK_ID` takes a contiguous slice of the sorted, de-duplicated ID list (`items[n*c//T : n*(c+1)//T]`, so chunk sizes differ by at most one). Chunks are independent: one can fail and be re-run without the others. `TOTAL_CHUNKS` must agree in three places: the Lambda's environment (default 5, [lambda/handler.py](../lambda/handler.py)), the summary-check Lambda (`TOTAL_CHUNKS=5`, verified on the deployed function) and the alert stack parameter `TotalChunks`. The dbt precheck reads `total_chunks` from the summary files themselves.

### 4.2 Per-retailer behaviour and quirks

| | Aldi | Tesco | SuperValu | Dunnes |
|---|---|---|---|---|
| ID source | `aldi.ie/sitemap_products.xml` (18-digit IDs in product URLs) | Category crawl of `tesco.ie` with a real browser (patchright/Chrome); product IDs are 9-digit `tpnc` | `shop.supervalu.ie/sitemap.xml` product IDs, plus category-page crawl of 6 stores (store 364 plus 5550, 1708, 260, 1261, 276) | `dunnesstoresgrocery.com/sitemap.xml` (but see below) |
| ID job on AWS? | Yes, daily, own image (`ie_supermarket_ids_job_definition`), skips if latest is under 20 h old | **No**: tesco.ie blocks AWS IPs; run weekly on the maintainer's Mac via launchd ([tesco_ids_local.md](tesco_ids_local.md)) | Yes, daily job on the scraper image; self-skips unless latest is over 7 days old | **No**: sitemap behind a Cloudflare challenge; `ids/latest` was rebuilt from raw data with `scripts/dunnes_ids_from_raw.py` (last 2026-10-07) |
| API | `GET api.aldi.ie/v2/products/{id}` through `curl_cffi` impersonating Chrome 120 | `POST xapi.tesco.com` GraphQL `GetProduct` with an API key embedded in the script | Storefront gateway `GET /api/stores/{store}/products/{id}` | Same storefront gateway software at `storefrontgateway.dunnesstoresgrocery.com` |
| Raw record | `{product_id, data, error}` | `{tpnc, data, error}` (key is `tpnc`) | `{product_id, store_id, data, error}` | `{product_id, store_id, data, error}` |
| Rate control | concurrency 5, 0.5 s delay, 4 attempts with backoff | **Strict pacing**: 2.0 requests per second total, shared equally by the 5 chunks (0.4 req/s each), up to 8 attempts per ID with Retry-After and backoff | concurrency 5, 0.5 s delay | concurrency 5, 0.5 s delay |
| Per-chunk size today | ~920 IDs, ~2.5 min | ~4,000 IDs, ~2h47m (pacing bound) | ~3,450 IDs, 12-15 min | ~2,100 IDs, ~5-6 min |
| Gate: max not-found | 60% | 60% | 60% | 85% |
| Failure mode seen | none recent | 429 throttling (below) | stale IDs | stale IDs, many stores lack an item, Cloudflare |

Details worth knowing:

- **Storefront retailers (SuperValu, Dunnes).** Each chunk lists all stores (`/api/stores`), shuffles them with `random.Random(chunk_id)` (so each chunk always uses the same subset of 60 stores) and tries stores in order until one returns the product. A product is recorded as `Not found in any store` only if store misses outnumber transient errors; otherwise the record carries the real error (e.g. `HTTP 503`), so an outage looks like an outage and not like 'stale IDs'. After 3 consecutive store errors it stops trying. Because Dunnes stocks many items in only a few of its 103 stores and its ID list is old, about 1-2% of Dunnes IDs are not found in a normal run, but the gate allows 85% (it is a backstop for outages, not a coverage target; background in [scraper_data_quality.md](scraper_data_quality.md) and [dunnes_ids_investigation.md](dunnes_ids_investigation.md)).
- **Stale IDs.** The ID lists lag the sites, so some IDs no longer exist. Those return `Not found in any store` (storefronts), `HTTP 404`, or `No product returned` (Tesco). The dbt staging models for SuperValu/Dunnes drop those rows; Aldi's and Tesco's stale rows are kept or dropped according to the staging SQL (the shared macro `is_stale_id_error` in [dbt/macros/stale_id.sql](../dbt/macros/stale_id.sql) lists `Not found in any store` and `HTTP 404`).
- **Tesco 429 incident.** From 2026-08-09 Tesco put Akamai rate controls in front of its API; about 70% of the daily scrape was lost as `HTTP 429` or as silent empty responses. The fix in the code today is the pacer, Retry-After/backoff handling, classification of GraphQL error bodies sent with HTTP 200, and keeping still-throttled records out of the checkpoint so a re-run retries them. 2026-10-27's Tesco run shows `ok = planned` for chunk 0, so the fix works. [tesco-429-incident.md](tesco-429-incident.md) still says "open, unfixed as of 2026-09-02"; that header is out of date (it is useful history, with the per-day error table in `tesco-429-error-rate-history.tsv`). The Lambda gives Tesco jobs a 4-hour attempt timeout and only 0.25 vCPU / 1 GB (they wait on the pacer, so they use little CPU).
- **Aldi** returns HTTP 200 with no `data` key for some IDs; that is counted as an *empty* record and checked against the 5% empty limit.
- **Dunnes ID refresh** is frozen. If new Dunnes products appear, they are not scraped. The workaround script rebuilds the list from a recent complete raw scrape and keeps only IDs that returned data or failed transiently.
- **Tesco ID refresh** (local, weekly, Sundays 06:00 Mac time): the S3 listing shows `ids/date=2026-10-11/progress.json` and `date=2026-10-18/progress.json` (crawl checkpoints) but `ids/latest` was last modified 2026-10-07, so the two most recent weekly crawls appear not to have published a new list. Why is **unverified**; check the log in `~/Library/Logs/supermarket/` on that Mac.

### 4.3 What a chunk does, step by step

[`run_scrape`](../scraper_common/runner.py) does this:

1. Slice the ID list for this chunk; apply `MAX_IDS` if set.
2. Read the checkpoint `raw/{retailer}/{date}/checkpoints/chunk_{N}.json` (a list of processed IDs plus running counts). IDs already in it are skipped. This makes a re-run of a **failed** chunk resume.
3. For each batch of 100 IDs: fetch concurrently, write `{retailer}_raw_{YYYYmmdd_HHMMSS}_chunk{N}_batch{NNNN}.jsonl.gz`, update the counts, rewrite the checkpoint, optionally sleep (`batch_delay`).
4. After the last batch, run the **data-quality gate** (section 9.2), write `_run_summary_chunk{N}.json`, and log the summary as one JSON line (`"event": "scrape_summary"`).
5. Gate failed: exit code **1**, checkpoint **kept**. Gate passed: delete the checkpoint, exit code **0**.

Records are written even when they are errors or empty, because the dbt layer has its own tests that need to see them. A failing run's rows stay in S3 and stay inside the dbt source glob.

**Re-run hazard (from reading the code, not tested).** Batch file names contain the job start time, and staging models do not de-duplicate. If you re-run a chunk after it *succeeded* (checkpoint deleted), or after a gate failure in which some records were written but left out of the checkpoint (Tesco "retryable" records), the same ID will appear twice in that day's raw folder. The `unique` test on the staging `id` columns would then fail the build. Section 10.4 gives the safe procedure.

### 4.4 The shared module `scraper_common/`

| Piece | File | What it does |
|---|---|---|
| `RunConfig`, `chunk_slice`, `setup_logging` | [config.py](../scraper_common/config.py) | Reads `CHUNK_ID`, `TOTAL_CHUNKS`, `RUN_DATE` (default today UTC), `OUTPUT_DIR`, `MAX_IDS`, gate thresholds. `BUCKET`/`REGION` constants. `cfg.day_prefix` is `raw/{retailer}/{run_date}`. |
| `Storage` | [storage.py](../scraper_common/storage.py) | One interface for S3 or a local directory (when `OUTPUT_DIR` is set): `put_bytes/get_bytes/delete`, `put_json/get_json`, `put_jsonl_gz`, `last_modified`, `parquet_uri_and_options`, plus `put_if_absent` (conditional write used as a lock), `list_keys`, `delete_many` (added for the dbt jobs). |
| `request_with_retry`, `Response`, `retry_until` | [http.py](../scraper_common/http.py) | Retries statuses 0 (transport error), 429, 500, 502, 503, 504 up to 4 attempts with exponential backoff (base 1 s, cap 30 s, honours Retry-After, plus jitter). 404 is not retried. `retry_until` is the synchronous version for ID discovery. |
| `read_ids`, `publish_ids`, `run_ids_job` | [ids.py](../scraper_common/ids.py) | Read `ids/latest` (sorted, unique, as strings). Publishing writes the dated snapshot and then overwrites `ids/latest`, **unless** the new list is empty or smaller than 50% of the current one (`IdsRejected`; `FORCE_IDS=1` bypasses the shrink check). A rejected refresh **exits 0** (so dependent chunk jobs still run on the old list) and writes `raw/{retailer}/ids/_run_summary_{date}.json` with `status: "rejected"`. `FORCE_REFRESH=1` ignores the freshness skip. `MAX_STALE_DAYS` (default 0 = off) would make a rejected refresh exit 1 when `latest` is older than N days. |
| `run_scrape`, `RunStats`, `gate_failures` | [runner.py](../scraper_common/runner.py), [quality.py](../scraper_common/quality.py) | Batching, checkpoints, counting, gate, summary. |
| `Storefront` | [storefront.py](../scraper_common/storefront.py) | The SuperValu/Dunnes client described above. |

**Exit codes.** Scrape chunk: 0 pass, 1 gate failed; an uncaught exception (e.g. `ids/latest` unreadable) also ends the process non-zero. Batch marks any non-zero exit as `FAILED`, which triggers the failure rule (section 5.6). ID job: 0 unless `MAX_STALE_DAYS` applies. The Batch retry policy on the scraper job definition retries only Spot interruptions ("Your Spot Task was interrupted*", 2 attempts), so a gate failure is **not** retried automatically.

### 4.5 Failure behaviour in one table

| Situation | What the system does |
|---|---|
| Transient HTTP errors | Retried inside the request (up to 4 attempts; Tesco 8). |
| Still failing after retries | Record gets an `error`; counted in the gate. Tesco marks it "retryable" (written, not checkpointed). |
| Gate fails | Summary says `failed` with reasons; exit 1; Batch job FAILED; SNS email "Batch job ... FAILED"; checkpoint kept; the dbt precheck later classes that retailer as FAILED (not retried). |
| A chunk never runs | No summary file; no Batch failure. Only the 14:00 summary-check Lambda (and the dbt precheck) notice. |
| ID refresh rejected | Old `ids/latest` stays; job exits 0; the summary-check Lambda emails. |
| Container killed (Spot) | Batch retries the job; the checkpoint resumes it. |

---

## 5. AWS infrastructure inventory

Account `557690579730`, region `eu-west-1`. Everything below was read from AWS on 2026-10-27 unless marked otherwise.

### 5.1 How to re-check any of this

```bash
export AWS_DEFAULT_REGION=eu-west-1 AWS_DEFAULT_OUTPUT=json
aws sts get-caller-identity
aws s3 ls s3://ie-supermarket-data/ --output json
aws batch describe-job-queues --output json
aws batch describe-job-definitions --status ACTIVE --output json
aws events list-rules --output json
aws scheduler list-schedules --output json
aws lambda list-functions --output json
aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE --output json
```

### 5.2 S3 bucket `ie-supermarket-data`

- **Versioning: ENABLED** (`aws s3api get-bucket-versioning`). Overwriting or deleting an object keeps the old content as a hidden *noncurrent version* that still costs money. This matters because the scrapers rewrite checkpoint files after every batch and delete them at the end, and `ids/latest` is overwritten daily.
- **Lifecycle configuration: none** (`NoSuchLifecycleConfiguration`). Nothing expires, nothing moves to cheaper storage. Drafts exist in [infra/dbt/](../infra/dbt/) (section 5.7).
- Top-level prefixes today: `raw/`, `processed/`, `warehouse/`, `smoketest/`, `dbt/`, `ducklake-shadow/`. Full table with sizes in section 6.1.

### 5.3 ECR

One repository, `docker-images`, holding all images, distinguished by tag: `scraper-<git sha>`, `ids-<git sha>`, `dbt-<git sha>`, plus older untagged-style hash tags used by the legacy `ocado_scraper_job_defintion`.

### 5.4 AWS Batch

**What Batch and Fargate are.** AWS Batch is a managed job queue: you submit a job (a container image plus a command), and Batch finds somewhere to run it. Fargate is AWS's serverless container runner: no servers to manage; you choose vCPU and memory per job and pay per second.

| Item | Value |
|---|---|
| Job queue | `getting-started-fargate-job-queue` (ENABLED). The compute environment behind it was not inspected (**unverified** details). |
| `ie_supermarket_batch_job_definition` | The scraper job. Latest revision 41. Image `docker-images:scraper-eba60c6...`. 2 vCPU / 8 GB, Fargate, attempt timeout 12000 s, retry 2 attempts only on Spot interruption. Default command `python tesco_api.py`; the Lambda overrides the command and environment per job. Roles: `aws_batch_2` for both job and execution. |
| `ie_supermarket_ids_job_definition` | Aldi ID job. Revision 36. Image `docker-images:ids-eba60c6...`. 0.25 vCPU / 512 MB, timeout 600 s. Command `python aldi_ids.py`. |
| `ie_supermarket_dbt_job_definition` | Revision 1, created 2026-10-27 by the dbt stack. Image `docker-images:dbt-bdc1d61...`. 2 vCPU / 4 GB, timeout 3600 s, no Batch retry. Command `python infra/dbt/run_dbt.py`. Role `supermarket-dbt-job-role`. |
| `ie_supermarket_compact_job_definition` | Revision 1. Same image. 0.5 vCPU / 2 GB. Runs `compact_raw.py --run-date <today> --max-age-days 7`. Role `aws_batch_2`. |
| `ie_supermarket_tesco429_smoketest` | Leftover from the 429 investigation (1 revision, 5400 s timeout). Not used by anything scheduled. |
| `ocado_scraper_job_defintion` | Legacy (spelling as in AWS), 15 active revisions, from the earliest Ocado/Tesco scraper. Not used by the Lambda. Safe to deregister once confirmed unused (not done). |

`aws.yml` registers a **new revision** of the two scraper job definitions (copying the previous one with only the image changed) on each push to `local_implementation`. The Lambda refers to job definitions by name, so it always gets the newest ACTIVE revision.

### 5.5 Scheduling: EventBridge rule and the Lambda

**EventBridge** is AWS's event bus and scheduler. A *rule* with a cron expression fires on a schedule and invokes a *target*.

| Rule | State | Schedule (UTC) | Target |
|---|---|---|---|
| `supermarket-scraper-daily` | ENABLED | `cron(0 8 * * ? *)` | Lambda `supermarket_batch_scheduler` |
| `supermarket-summary-check` | ENABLED | `cron(0 14 * * ? *)` | Lambda `supermarket_summary_check` |
| `supermarket-batch-job-failed` | ENABLED | event pattern: Batch Job State Change, status FAILED, queue `getting-started-fargate-job-queue` | SNS topic `supermarket-scraper-alerts` |

**`supermarket_batch_scheduler`** (Python 3.13, 128 MB, 60 s timeout; code in [lambda/handler.py](../lambda/handler.py); role `aldi_scraper_lambda-role-9m23mie2`; no environment variables set, so defaults apply). On each invocation it submits, in this order:

1. `aldi-ids` (ids job definition), then Aldi chunks 0-4 that `dependsOn` it (type SEQUENTIAL; chunks start after the ID job succeeds).
2. Tesco chunks 0-4 with no ID job, overridden to 0.25 vCPU / 1 GB and a 14,400 s attempt timeout.
3. `supervalu-ids` (on the scraper job definition, command `python supervalu_ids.py`), then SuperValu chunks 0-4 depending on it.
4. Dunnes chunks 0-4, no ID job (not scheduled; see the comment in the handler).

That is 1 + 5 + 5 + 1 + 5 + 5 = 22 jobs. Every job gets `CHUNK_ID`/`TOTAL_CHUNKS` as environment overrides. It is deployed from CI (section 5.9). Trigger it by hand with `aws lambda invoke --function-name supermarket_batch_scheduler --output json /dev/stdout` (this submits ~22 real jobs; do not do it casually).

**`aldi_scraper_lambda`** is an older container-image Lambda (3008 MB, 600 s, last modified 2026-02-12). Nothing in the repo references it and no rule targets it (**unverified** that nothing else does).

### 5.6 Alerting stack (CloudFormation `supermarket-scraper-alerts`, CREATE_COMPLETE)

Defined in [infra/batch-alerts.yaml](../infra/batch-alerts.yaml); despite the "drafted" wording in older notes it **is deployed**. It creates:

- SNS topic **`supermarket-scraper-alerts`** (SNS = Simple Notification Service: a publish/subscribe topic; here it emails the subscriber).
- The `supermarket-batch-job-failed` rule above, which emails `Batch job <name> (<id>) FAILED: <reason>`.
- Lambda **`supermarket_summary_check`** (Python 3.13, 128 MB, 120 s) with schedule rule `supermarket-summary-check` at 14:00 UTC. It reads `raw/{retailer}/{today}/_run_summary_chunk{0..4}.json` and `raw/{retailer}/ids/_run_summary_{today}.json` for `tesco,aldi,supervalu,dunnes` and publishes one email, subject `Scraper data-quality problems <date>`, listing: a missing summary, a summary with `status != ok` and its failures, or an ID refresh that was not `ok`. It catches the two things Batch cannot: a chunk that never ran, and ID jobs that deliberately exit 0.
- The IAM role `supermarket-scraper-alerts-SummaryCheckRole-...` (read `raw/*`, list bucket, publish to the topic).

Who receives the email is whatever address was given as `AlertEmail` at deploy time (not shown here). Subscription confirmation state: **unverified**.

### 5.7 IAM roles that exist

| Role | Used by | Notes |
|---|---|---|
| `aws_batch_2` | Job and execution role of every scraper job definition, and the compaction job | Has `AmazonS3FullAccess`, `AmazonEC2ContainerRegistryFullAccess`, `SecretsManagerReadWrite`, `CloudWatchFullAccessV2` (per [iam_setup.md](iam_setup.md), 2026-10-07; not re-read). Far broader than needed. |
| `aws_batch` | Older, unused by current job definitions (**unverified**) | |
| `aldi_scraper_lambda-role-9m23mie2` | Both `supermarket_batch_scheduler` and `aldi_scraper_lambda` | Basic execution + `AWSBatchFullAccess` + `AmazonS3FullAccess` per iam_setup.md. |
| `supermarket-scraper-alerts-SummaryCheckRole-...` | Summary-check Lambda | Narrow, created by the alert stack. |
| `supermarket-dbt-job-role` | dbt Batch job | Created 2026-10-27 by the dbt stack: read `raw/*`; read/write/delete `landing/*`, `ducklake/*`, `dbt/*`, `processed/*` (and the two shadow prefixes); `sns:Publish`. Cannot delete raw data. |
| `supermarket-dbt-batch-SchedulerRole-...` | EventBridge Scheduler | Allowed `batch:SubmitJob` on the queue and the two dbt job definitions. |
| Service-linked roles (`AWSServiceRoleForBatch`, ECS, ...) | AWS itself | Ignore. |

**Drafted but not applied.** [infra/iam/](../infra/iam/) holds least-privilege policies (`batch-job-role-*`, `github-deploy-*`, `lambda-scheduler-policy.json`, `human-operator-policy.json`) from [iam_setup.md](iam_setup.md). The roles `supermarket-batch-job-role`, `supermarket-batch-execution-role` and `supermarket-github-deploy` do not exist in the account (confirmed by `aws iam list-roles`); GitHub still deploys with long-lived access keys stored as repository secrets. [infra/dbt/s3_lifecycle.json](../infra/dbt/s3_lifecycle.json) and [s3_lifecycle_housekeeping.json](../infra/dbt/s3_lifecycle_housekeeping.json) are also **not applied** (section 5.2).

### 5.8 The dbt pipeline: what is live and what is not

State as updated late on 2026-10-27 (the rollout is in progress; re-run the checks):

| Piece | Status | Evidence |
|---|---|---|
| Code (`infra/dbt/*.py`, `dbt/` changes, tests) | Committed and pushed (`8c3686c`, `d29a445`, `27fa673`, `bdc1d61`, `780ecb7`, `078b031`) | `git log` |
| Image `docker-images:dbt-<sha>` | Built and pushed by the `build-dbt-image` job in `aws.yml` on every push | `aws ecr describe-images --repository-name docker-images` |
| CloudFormation stack `supermarket-dbt-batch` | **Exists** (deployed 2026-10-27), in shadow mode | `aws cloudformation describe-stacks --stack-name supermarket-dbt-batch` |
| Stack parameters | `StatePrefix=dbt/state-shadow`, `LakeData=s3://ie-supermarket-data/ducklake-shadow/`, `ProcessedPrefix=s3://ie-supermarket-data/processed-shadow`, `ExecutionRoleArn` and `ScraperJobRoleArn` = `aws_batch_2` | same command |
| Job definitions `ie_supermarket_dbt_job_definition`, `ie_supermarket_compact_job_definition` | Registered; revision 2 uses the newest image (revision 1 was the first image, whose tests failed on missing fixtures) | `aws batch describe-job-definitions` |
| Schedule `supermarket-dbt-daily` (`cron(30 11-14 * * ? *)`) | **ENABLED**, writes only to the shadow prefixes | `aws scheduler get-schedule --name supermarket-dbt-daily` |
| Schedule `supermarket-raw-compaction-daily` (`cron(30 15 * * ? *)`) | **ENABLED** (dates 2-7 days old only; enabled after the backfill below) | `aws scheduler get-schedule --name supermarket-raw-compaction-daily` |
| Shadow catalog and runs | Seeded from the local database; three days (2026-10-24, 25, 26) built by manual `submit-job` runs, 12 `dbt/state-shadow/built/...` markers; lake matches the local database up to 2026-10-23 with no duplicates | `aws s3 ls s3://ie-supermarket-data/dbt/state-shadow/ --recursive` |
| Production state (`dbt/state/`, `ducklake/`) | **Do not exist yet** (cutover pending); `landing/` exists (shared, written by the shadow runs) | `aws s3 ls` |
| `processed/all_supermarket_products.parquet` | Still last written by a local run; the shadow job writes `processed-shadow/` instead | `aws s3 ls` |
| S3 lifecycle rules | **Applied** (`infra/dbt/s3_lifecycle.json`: raw and landing to Glacier IR, noncurrent versions expire after 14 days (3 for `processed/`), delete markers and multipart uploads cleaned, catalog archive expires after 8 days) | `aws s3api get-bucket-lifecycle-configuration` |
| Raw compaction | **Done** for every day up to 2026-10-25 (four Batch jobs, one per retailer, all exited 0): each `raw/{retailer}/{date}/` now holds the `_run_summary_chunk*.json` files plus one `compacted.jsonl.gz` (about 540 objects became about 6 per retailer-day). The originals are noncurrent versions, recoverable until the 14-day rule removes them (about 2026-11-10). A compacted day (2026-10-22) rebuilt through the shadow job matched the local database exactly | `aws s3 ls s3://ie-supermarket-data/raw/tesco/2026-10-22/` |

The first manual run of the first image failed at one dbt test because the fixture files had never been committed (the root `.gitignore` excludes `*.jsonl`); `780ecb7` fixed that. The plan's **rollout order** (shadow week, then cutover after `seed_lake.py`; the compaction backfill and lifecycle rules are done) is in [dbt_orchestration_plan.md](dbt_orchestration_plan.md).

The Scheduler `Input` format is now verified: the 14:30 UTC slot on 2026-10-27 fired by itself, passed `SCHEDULED_TIME`, and the job built all four retailers for that day (`dbt OK`). The 11:30, 12:30 and 13:30 slots did not exist yet that day (the schedule was enabled at about 14:20).

### 5.9 CI/CD

- [`.github/workflows/ci.yml`](../.github/workflows/ci.yml): on pull requests and on pushes to any branch **except** `local_implementation`: `uv sync --frozen --group dev --group test`, `ruff check . --extend-exclude notebooks`, `ruff format --check` on `scraper_common ml_common ml tests aldi dunnes supervalu tesco lambda`, then `pytest -q`.
- [`.github/workflows/aws.yml`](../.github/workflows/aws.yml): on push to `local_implementation` (environment `production`, concurrency group `deploy`): build and push the ids and scraper images to ECR (tags `ids-<sha>`, `scraper-<sha>`), register new revisions of the two scraper job definitions, zip and deploy `lambda/` to `supermarket_batch_scheduler`; a second job `build-dbt-image` builds `dbt-<sha>` and, if the dbt job definitions exist, registers new revisions for them. **It does not run tests or lint**, and `ci.yml` skips this branch, so code pushed to `local_implementation` ships to AWS untested by CI. Run `uv run pytest -q` yourself first.
- To push to `local_implementation` **without** deploying, put `[skip ci]` in the commit message. (Note that GitHub also then skips `ci.yml`.)
- Credentials are repository secrets `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in environment `production` (long-lived keys).
- [infra/dbt/github_workflow_dbt_ci.yml](../infra/dbt/github_workflow_dbt_ci.yml) is a draft (not under `.github/workflows/`): dbt parse + `tests/test_dbt_infra.py` on Python 3.12.

---

## 6. Where the data is stored

### 6.1 S3 prefixes (bucket `ie-supermarket-data`)

Sizes are current-version bytes from `aws s3 ls --recursive --summarize` on 2026-10-27; noncurrent versions are not included. The plan doc ([dbt_orchestration_plan.md](dbt_orchestration_plan.md)) reports ~3.3 GB of noncurrent raw versions (unverified by me).

| Prefix / path | Contents | Written by | Read by | Retention | Size today |
|---|---|---|---|---|---|
| `raw/{retailer}/{YYYY-MM-DD}/{retailer}_raw_{YYYYmmdd_HHMMSS}_chunk{N}_batch{NNNN}.jsonl.gz` | Gzipped **JSONL** (one JSON object per line) of raw API responses, 100 records per file, ~140 files per retailer-day (Tesco 200). Record shapes in 4.2. This is the **source of truth**. | scrapers | dbt (source `external_source`), landing job, `compact_raw.py`, ad-hoc analysis | Forever (no lifecycle) | aldi 7,266 objs / 131 MB; tesco 30,997 / 3.08 GB; supervalu 20,871 / 2.21 GB; dunnes 20,513 / 1.20 GB |
| `raw/{retailer}/{date}/_run_summary_chunk{N}.json` | Per-chunk result: counts, rates, `status` ok/failed, `failures`, `planned_ids`, `duration_s`, `error_samples`. Does not match the `*.jsonl.gz` glob so dbt ignores it. | `run_scrape` | summary-check Lambda, dbt precheck, humans | Forever | ~400-500 bytes each |
| `raw/{retailer}/{date}/checkpoints/chunk_{N}.json` | Resume state: `processed_ids` and `stats`. Rewritten after every batch, **deleted on success**; so it normally only exists for failed or running chunks. | `run_scrape` | `run_scrape` | Deleted on success (leaves noncurrent versions) | small, grows with IDs |
| `raw/{retailer}/{date}/compacted.jsonl.gz` | Future: all of a day's batches concatenated by `compact_raw.py`. Not present yet. | `compact_raw.py` | dbt (same glob) | | |
| `raw/{retailer}/ids/latest/{retailer}_product_ids.parquet` | Current ID list (Parquet, snappy). Aldi: `product_id, url, scraped_at, retailer`; SuperValu/Dunnes: `product_id, scraped_at, retailer`; Tesco: `id, ...` | ID scripts (`publish_ids`) | API scripts | Overwritten (old versions noncurrent) | 53-133 KB |
| `raw/{retailer}/ids/date={YYYY-MM-DD}/{retailer}_product_ids_{ts}.parquet` | Dated snapshot of each published list | ID scripts | humans, `dunnes_ids_from_raw.py` | Forever | |
| `raw/{retailer}/ids/_run_summary_{date}.json` | ID refresh result (`status` ok/rejected, `count`, `previous_count`, `reason`) | `run_ids_job` | summary-check Lambda | Forever | ~220 bytes |
| `raw/tesco/ids/date={date}/progress.json` | Resumable state of the local Tesco category crawl | `tesco_ids.py` | `tesco_ids.py` | | |
| `raw/tesco/debug/` | Error screenshots (`*.png`) from the Tesco ID crawl | `tesco_ids.py` | humans | Forever | |
| `raw/dunnes/ids/excluded/` | IDs dropped when `ids/latest` was rebuilt from raw | `dunnes_ids_from_raw.py` | humans | | |
| `landing/{retailer}/scraped_date={date}/data.parquet` | **Future.** One zstd Parquet per retailer-day, the same table dbt would get by reading raw JSON (about 0.65x its size). A derived cache; rerunning overwrites it. | `infra/dbt/landing.py` (inside the dbt job) | dbt when `use_landing=true` | Rebuildable from raw | does not exist yet |
| `ducklake/` (production) / `ducklake-shadow/` (shadow) | **DuckLake** data: Parquet files of the marts under `main/<table>/`. Never write there by hand. | dbt Batch job | DuckLake readers | Cleaned by weekly maintenance | shadow only so far |
| `dbt/state/catalog.ducklake` (production) / `dbt/state-shadow/catalog.ducklake` | The DuckLake **catalog**: a small (~4 MB) DuckDB file listing tables, files and snapshots. Pushing it is the "commit". | dbt job, `seed_lake.py` | dbt job | Overwritten each run | shadow only |
| `dbt/state*/archive/{date}/catalog.ducklake` | A dated copy of the catalog for rollback | dbt job | humans | 8 days by the draft lifecycle rule | shadow `archive/seed/` |
| `dbt/state*/LOCK` | Mutual-exclusion lock object (content = run date). Stale after 3 h. | dbt job | dbt job | transient | |
| `dbt/state*/built/{date}/{retailer}` | Empty marker: that retailer-day was built and committed | dbt job | dbt job | Forever | |
| `processed/all_supermarket_products.parquet` | Slim catalogue (`id, supermarket, title, title_cleaned, image_url`, rows with a title). Overwritten by each full dbt build. | dbt model `int_all_supermarket_products` | `ml/embeddings.py`, `ml_common` (`CATALOGUE_URI` default) | Overwritten | 4.9 MB |
| `processed/embeddings/{model-slug}/product_title_embeddings.parquet` | Title vectors for three models (`microsoft-harrier-oss-v1-270m`, `jinaai-jina-embeddings-v5-text-small`, `sentence-transformers-all-MiniLM-L6-v2`) | `ml/embeddings.py` | dbt `int_product_title_embeddings`, `ml/agents.py` | Overwritten | 53-88 MB each |
| `processed-shadow/` | Shadow-week replacement for `processed/` | dbt Batch job (shadow) | humans | Delete after the shadow week | not yet present |
| `warehouse/tesco_raw.db` | A 319 MB DuckDB file from February 2026, last modified 2026-02-12. Purpose not documented in the repo; treat as legacy (**unverified**). | (old manual run) | nothing found | Forever | 319 MB |
| `smoketest/tesco429/...` | Output of the Tesco 429 experiments (88 objects, 10 MB). Safe to delete when the incident is closed. | experiments | humans | Forever | 10 MB |

Raw date folders start 2026-02-01 (Tesco), 2026-02-05 (SuperValu earliest listed), 2026-02-27 (Aldi), 2026-05-21 (Dunnes); there are gaps for some retailers (the listing shows non-contiguous early dates).

### 6.2 Local files

| File | What | Notes |
|---|---|---|
| `supermarket_data.db` | DuckDB database used by `dbtf` `dev`/`prod` targets. Tables: `fct_product_price_daily` (6.07 M rows, 2026-02-01 to 2026-10-23: Tesco 2.19 M, SuperValu 1.72 M, Dunnes 1.55 M, Aldi 0.61 M), `fct_price_history`, `fct_product_listings`, `int_supervalu`, `int_product_title_embeddings`, `backfill_stats`, the three SuperValu seeds; `stg_*` and other `int_*` are views. | The **only complete copy of price history** until the Batch pipeline is cut over; `fct_product_price_daily` is the one table that cannot be rebuilt by replaying raw days (several Tesco days failed their gate). Last loaded day: 2026-10-23. Back it up before big runs: `cp supermarket_data.db ~/supermarket_backups/supermarket_data_$(date +%Y%m%d).db`. Backups from 2026-10-09 and 2026-10-20 exist in `~/supermarket_backups/`. |
| `~/.dbt/profiles.yml` | dbt profile for local runs | outside the repo |
| `~/Library/Logs/supermarket/` | Tesco ID refresh logs | Mac only |
| `dbt/target/`, `dbt/logs/`, `logs/` | dbt artifacts and logs | git-ignored |

---

## 7. The dbt transformation layer

**What dbt is.** dbt ("data build tool") turns a folder of SQL `SELECT` statements into tables and views, working out the order from `ref()` links, and runs tests against the results. **DuckDB** is an embedded analytical database (no server) that can read gzipped JSONL and Parquet directly from S3 through its `httpfs` extension, so there is no separate "load data into a warehouse" step. **Parquet** is a compressed columnar file format. **DuckLake** is a DuckDB extension that stores a table as Parquet files in object storage plus a small catalog database (here one file) that tracks which files belong to which snapshot of which table.

Project: [dbt/dbt_project.yml](../dbt/dbt_project.yml), profile name `dbt_ie_supermarket_analytics`. Models are in four layers:

| Layer | Models | Materialization | Meaning |
|---|---|---|---|
| **Sources** | `external_source.{aldi,tesco,dunnes,supervalu}` ([raw_s3.yml](../dbt/models/staging/raw_s3.yml)) | n/a | The day's raw files: `read_json('s3://.../raw/{retailer}/{run_date}/*.jsonl.gz', union_by_name=true)`, or, when `use_landing=true`, `read_parquet('{landing_prefix}/{retailer}/scraped_date={run_date}/*.parquet')`. `union_by_name` infers the schema per file and merges, because free-form `attributes` keys drift between files. |
| **Staging** | `stg_aldi`, `stg_tesco`, `stg_dunnes`, `stg_supervalu` | view | One row per raw record, renamed and typed. SuperValu/Dunnes drop `Not found in any store`; genuine fetch failures stay so tests can see them. Tesco keeps empty records. |
| **Intermediate** | `int_aldi`, `int_tesco`, `int_dunnes`, `int_supervalu` | views (`int_supervalu` is a table) | Business logic on an identical **46-column schema** in the same order so they can be `UNION ALL`ed. Normalised text (`title`, `brand`, `title_cleaned`), parsed prices and pack sizes, unit price normalised to per kg / litre / metre / each, promotion fields, allergens and dietary flags. A retailer lacking a field emits an explicitly typed NULL. |
| | `int_all_supermarket_products` | `external` (writes Parquet) | Slim `id, supermarket, title, title_cleaned, image_url` export of `fct_product_listings` to `{parquet_output_prefix}/all_supermarket_products.parquet`. The name predates the marts layer; it is an output, not an intermediate. |
| | `int_product_title_embeddings` | table | Reads title embeddings back from `{embeddings_input_prefix}/embeddings/{embedding_model}/product_title_embeddings.parquet` (computed by `ml/embeddings.py`, not in dbt). Not part of the Batch job's useful output. |
| **Marts** | `fct_product_listings` | table, contract enforced | All four retailers unioned: one row per product per run. Keeps one row per `(supermarket, id)`, preferring a priced row. |
| | `fct_product_price_daily` | incremental, `delete+insert` | One row per `(supermarket, id, scraped_date)` with price fields; rows with NULL price are excluded. |
| | `fct_price_history` | table, contract enforced | Price versions derived from the daily table. |
| **Seeds** | `supervalu_taxonomy`, `supervalu_category_map`, `supervalu_product_overrides` | tables | The unified SuperValu category tree and mappings (see [taxonomy-rationale/](taxonomy-rationale/README.md)). |

Macros ([dbt/macros/](../dbt/macros/)): `normalize_text`, `clean_title` (strips pack-size words such as `500g`, `8 pack`, `2 x 400g` for `title_cleaned`), unit normalisation, price parsing, promotions, allergens, dietary flags, `html_sections` (Dunnes description HTML), `active_grace_days`, `run_date`, `stale_id`, `utc_to_local_date`, `normalize_category`, `raw_attributes`.

### 7.1 Price-history design

- **`fct_product_price_daily`** is *append-only by day*. It is configured `incremental` with `incremental_strategy='delete+insert'` on the unique key `[supermarket, id, scraped_date]` and `full_refresh=false`. Each run deletes any rows with the same keys and inserts the new ones, so re-running a day, or loading days out of order, is safe and gives the same result. It must **never be full-refreshed**: it is the only copy of history (blocked in the model config).
- **`fct_price_history`** is *derived* from the daily table every run (so it can never drift from it). A new **version** row starts on the first scrape date where `price`, `was_price` or `unit_price` differs from the previous scrape date. `valid_from` is that date; `valid_to` is the date the next version starts (exclusive), NULL on the latest version (`is_current`). `last_seen_date` is the product's latest scrape date. `is_active` is true if `last_seen_date` is within the retailer's **grace days** of that retailer's latest loaded day: `active_grace_days` in `dbt_project.yml`: tesco 2, aldi 2, dunnes 8, supervalu 8 (measured from the backfill; the reasoning is in [dbt/README.md](../dbt/README.md)). Using each retailer's own latest day means one retailer's missed run does not mark the others inactive.
- Live prices: `SELECT * FROM fct_price_history WHERE is_current AND is_active`.
- The older dbt snapshot `scd_product_prices` is gone.

### 7.2 Variables

| Var | Default | Meaning |
|---|---|---|
| `run_date` | today (UTC, from `run_started_at`) | Raw partition to read and the `scraped_date` stamped on rows. Always pass it for scheduled, backfill and rerun builds (a rerun after midnight UTC would otherwise read the wrong day). |
| `use_landing` | false | Read `landing/` Parquet instead of raw JSONL. The Batch job sets true. |
| `landing_prefix` | `s3://ie-supermarket-data/landing` | Where landing Parquet is. |
| `retailers` | `[tesco, dunnes, supervalu, aldi]` | Retailers built this run. Cross-retailer models and tests loop over it. A **degraded day** narrows it. |
| `listing_min_rows` | 30000 | Floor for `fct_product_listings`; lowered on a degraded day to the sum of the present retailers' minimums (tesco 5000, dunnes 3000, supervalu 5000, aldi 1500). |
| `parquet_output_prefix` | `s3://ie-supermarket-data/processed` | Where `int_all_supermarket_products` writes. **Override for local tests.** |
| `embeddings_input_prefix` | same | Where embeddings are read from. |
| `embedding_model` | `microsoft-harrier-oss-v1-270m` | Embedding table slug. |
| `active_grace_days` | see above | Per-retailer grace. |
| `fixtures_dir` | `dbt/tests/fixtures` | Used by one singular test. |

A **degraded day** is a day built with fewer than four retailers because one's scrape did not finish or failed its gate. Excluded retailers' sources, models and tests are skipped via `--exclude source:external_source.<retailer>+`, and the catalogue export `int_all_supermarket_products` is **not** built on a degraded day (it would overwrite the full `processed/` file `ml/` reads with a subset).

### 7.3 Targets

| Target | Where | Marts live in | Used by |
|---|---|---|---|
| `dev` / `prod` | `~/.dbt/profiles.yml`, both → `supermarket_data.db` | the local DuckDB file | Hand runs on a laptop with Fusion |
| `batch` | [infra/dbt/profiles.yml](../infra/dbt/profiles.yml) | In-memory main database for staging/intermediate, with a DuckLake catalog attached as `lake` (catalog file `LAKE_CATALOG`, default `/tmp/catalog.ducklake`; data `LAKE_DATA`, default `s3://ie-supermarket-data/ducklake/`). Marts get `+database: lake` only when `target.name == 'batch'`. | The AWS Batch job (dbt-core on Python 3.12) |

DuckLake cannot enforce primary keys, so `fct_product_listings` has no primary-key constraint; its `unique_combination` test covers it.

### 7.4 Running and backfilling

Local daily load (after the scrapes have finished):

```bash
cp supermarket_data.db ~/supermarket_backups/supermarket_data_$(date +%Y%m%d).db   # safety copy
dbtf build --project-dir dbt --vars '{run_date: 2026-10-27}'
```

Local **backfill** of historical days, oldest first, without tests or gates (historical days include known bad scrapes), only retailers that have a raw partition with data on that day: [dbt/scripts/backfill.py](../dbt/scripts/backfill.py).

```bash
uv run python dbt/scripts/backfill.py --start 2026-02-01 --end 2026-10-08 \
    --profiles-dir ~/.dbt --db supermarket_data.db \
    --parquet-prefix /tmp/processed --dbt ~/.local/bin/dbt --resume
dbtf build --project-dir dbt --select fct_price_history assert_price_history_consistent
```

`--resume` skips dates already recorded in the `backfill_stats` table; `--timeout` (default 420 s) bounds each dbt attempt; `--parquet-prefix` must be a local directory, never production S3. After a backfill, rebuild `fct_price_history` and run the consistency test.

Batch backfill (once the pipeline is live): submit the dbt job with `RUN_DATE=<day>` (section 10.5). It needs that day's `_run_summary_chunk*.json` files; those exist only for recent days, so a long historical gap must be filled locally.

### 7.5 Seeding the lake (one-off)

`fct_product_price_daily` exists only in the local DB, so the lake must start from a copy of it: [infra/dbt/seed_lake.py](../infra/dbt/seed_lake.py) copies that table into a fresh DuckLake catalog and pushes the catalog, refusing to run if a catalog already exists. The same environment variables (`STATE_PREFIX`, `LAKE_DATA`) choose shadow versus production. The dbt job refuses to start with no catalog (`ALLOW_FRESH_LAKE=1` overrides) so history is never silently forked.

---

## 8. The daily timeline and the dbt Batch job

### 8.1 A normal day (UTC; times from 2026-10-27's Batch jobs)

| Time | What happens | Status |
|---|---|---|
| 06:00 Sunday (Mac local time) | Weekly Tesco ID refresh via launchd (may not have published recently, see 4.2) | live, manual machine |
| 08:00 | Rule `supermarket-scraper-daily` fires the Lambda, which submits 22 Batch jobs | live |
| 08:01-08:02 | `aldi-ids` and `supervalu-ids` run (about 1 min). Tesco and Dunnes chunks start. | live |
| 08:08-08:18 | Aldi chunks (each 2-3 min; start after `aldi-ids`) | live |
| 08:01-10:49 | Tesco chunks, all five in parallel, about 2h47m each | live |
| 08:16-08:59 | SuperValu chunks (12-15 min each; starts staggered as capacity frees) | live |
| 08:01-09:07 | Dunnes chunks (5-6 min each; some start later, ~08:53-09:00, probably waiting for capacity; **unverified**) | live |
| 10:49 | Last Tesco summary file is written; day's raw data complete | live |
| 11:30, 12:30, 13:30, 14:30 | EventBridge Scheduler slots submit `ie_supermarket_dbt_job_definition` (14:30 is the "final" slot) | built; **dbt slots ENABLED (shadow)** |
| 14:00 | `supermarket_summary_check` Lambda emails if anything is missing or failed | live |
| 15:30 | Raw compaction job (dates 2-7 days old only) | **ENABLED** |
| Any time | Any Batch job reaching FAILED emails via SNS | live |

Today, steps from 11:30 onward are done by hand (section 10.2).

### 8.2 What the dbt Batch job does ([infra/dbt/run_dbt.py](../infra/dbt/run_dbt.py))

Input: `SCHEDULED_TIME` (from the scheduler; the UTC date becomes `run_date`) or `RUN_DATE`. The job is the *final slot* if `FINAL_ATTEMPT=1` or the scheduled hour is >= `FINAL_HOUR_UTC` (default 14).

1. **Precheck per retailer** ([precheck.py](../infra/dbt/precheck.py)): read that retailer's `_run_summary_chunk*.json` files for the date.
   - none yet → `NOT_READY` ("no chunk summaries yet");
   - any with `status != ok` → `FAILED` (waiting will not fix it);
   - some chunk numbers missing (out of `total_chunks`) → `NOT_READY`;
   - all present and ok → `READY`.
2. **Built markers**: which retailers already have `dbt/state/built/{date}/{retailer}`.
3. **Decide** ([`plan`](../infra/dbt/run_dbt.py)):

| Ready retailers | Any still NOT_READY? | Final slot? | Already built? | Action | Exit code | Message |
|---|---|---|---|---|---|---|
| none | yes | no | n/a | wait; a later slot retries | 0 | none (log only) |
| none | no (all FAILED) or final slot | any | n/a | stop | **1** | `dbt NOT RUN for <date>: no retailer ready` + reasons |
| some | any | any | all ready ones already built | nothing to build | 0 | final slot and some excluded: `dbt DEGRADED for <date>: n retailer(s) missing`; else log only |
| some | any | any | at least one ready one not built | **build** (below) | 0 or non-zero | see below |

4. **Build**: take the **lock** (an S3 object `LOCK`, created with a conditional write so two jobs cannot both win; a lock older than 3 hours is treated as left by a killed job and taken over; if held: alert `dbt NOT RUN for <date>` and exit 1). Then:
   1. Pull `catalog.ducklake`. Missing → alert `dbt NOT RUN for <date>: no catalog`, exit 1 (unless `ALLOW_FRESH_LAKE=1`).
   2. **Landing**: convert each ready retailer's raw day to `landing/.../data.parquet` ([landing.py](../infra/dbt/landing.py)).
   3. **dbt build** with target `batch`, `--vars` as in 7.2, `--exclude test_type:unit` plus the absent retailers' sources, plus `int_all_supermarket_products` when degraded. Failure → alert `dbt FAILED for <date>` ("Catalog was not updated"), exit with dbt's code.
   4. **Weekly maintenance** if Sunday (or `MAINTAIN=1`): expire snapshots older than 7 days, merge small Parquet files, clean old and orphaned files ([lake.py](../infra/dbt/lake.py)).
   5. **Push** the catalog (this is the commit; it also writes a dated archive copy) and write a `built` marker per retailer.
   6. **Alert**: `dbt OK for <date>` (body lists built retailers, excluded ones and whether later slots will retry, and any dbt test **warnings**), or `dbt OK (DEGRADED, n missing) for <date>`.
   7. Always release the lock.
5. A failed build never pushes the catalog, so the Parquet it wrote is unreferenced and is removed by maintenance. Re-running a day is idempotent; a later slot that finds a late retailer rebuilds the whole day with it included.

Notes: a dbt test failure inside the build is **fail-closed**: it blocks the whole build, healthy retailers included (the plan lists per-retailer retry as a next step, not implemented). The job calls the SNS topic via `SNS_TOPIC_ARN`; with it unset it only prints.

### 8.3 Compaction job ([compact_raw.py](../infra/dbt/compact_raw.py))

Merges each retailer-day's ~140 small batch files into one `compacted.jsonl.gz` (valid because concatenated gzip members read as one stream), verifying row counts before and after, then deletes the originals. It skips dates newer than `run_date - 2 days`, refuses to merge a day that already has a `compacted.jsonl.gz`, and leaves summaries and checkpoints alone. The scheduled version only touches dates 2-7 days old; the historical backfill is a deliberate manual run (section 10.8). It matters because S3 Glacier Instant Retrieval (a cheaper, slower storage class) bills small objects as 128 KB.

---

## 9. Tests and quality gates

There are three independent layers: Python unit tests (9.1), the scraper's runtime gate (9.2) and the dbt tests (9.3).

### 9.1 Python tests (pytest)

Run all: `uv run pytest -q` (config: `testpaths = ["tests"]`, `pythonpath = ["."]`). None need AWS or the network. CI runs them via [ci.yml](../.github/workflows/ci.yml) on pull requests and non-deploy branches (not on pushes to `local_implementation`).

| File | Tests | What it covers |
|---|---|---|
| [tests/test_scraper_common.py](../tests/test_scraper_common.py) | 19 | `chunk_slice` covers all items without overlap; `RunConfig` rejects a bad `CHUNK_ID`; stat classification; the gate (zero rows, all-null, healthy, the Tesco 429 and all-null shapes); `request_with_retry` retries then gives up and does not retry a 404; `retry_until`; `Storage` JSONL.gz round trip with a mocked S3 and missing-key behaviour; `run_scrape` writes batches, summary, clears the checkpoint; fails on all-null and keeps the checkpoint; resumes from a checkpoint; `fail_on_retryable` keeps the checkpoint; `publish_ids` rejects empty and shrunk lists; `run_ids_job` stale-latest behaviour. |
| [tests/test_lambda_handler.py](../tests/test_lambda_handler.py) | 3 | Tesco chunks get reduced resources and the long timeout; other retailers keep the job definition's resources; the Dunnes ID job is not scheduled. |
| [tests/test_dbt_infra.py](../tests/test_dbt_infra.py) | 25 | The dbt Batch helpers on local directories: `put_if_absent` lock; `list_keys`/`delete_many`; compaction merges, keeps summaries, never re-merges, refuses on count mismatch, dry-run, date windows (`only_date`, floor); landing writes one Parquet per retailer-day and fails loudly if the raw partition is missing; precheck classifies each retailer independently; lock blocks a second run but breaks a stale one; catalog pull/push round trip and archive; seed copies history and refuses to overwrite; maintenance; final-slot detection from the scheduled hour; the entrypoint refuses to start a lake from nothing, builds/pushes/marks and is idempotent, does not push when dbt fails, waits quietly until the final slot, publishes ready retailers then adds a late one, reports a failed retailer once; `plan` decisions; the dbt command skips absent retailers and the catalogue export. |
| [tests/test_ml_common.py](../tests/test_ml_common.py) | 26 | The embedding framework with a fake embedder: content keys, spec ids (stable, change with any vector-affecting field), incremental embedding (rerun embeds nothing, one new/changed row embeds one), failures recorded and retried, append-only parts, schemas, remote write/read retries. |
| [tests/test_ml_images.py](../tests/test_ml_images.py) | 15 | The image store: incremental fetch, content-addressed de-duplication, corrupt payloads, retry caps and Retry-After, resume, URL change re-fetch, seeding from legacy shards, format sniffing, the CLI failure-rate exit code. |

(Counts are of `def test_` functions; parametrised tests run more cases.)

### 9.2 The scraper's runtime data-quality gate

Applies to all four retailers through `scraper_common` (Tesco adopted it; it keeps its own pacer and sets `fail_on_retryable=True`). Each fetched record is put in exactly one bucket ([quality.py](../scraper_common/quality.py)):

| Bucket | Meaning |
|---|---|
| `ok` | non-empty `data` |
| `not_found` | `error` in {`Not found in any store`, `HTTP 404`, `No product returned`} (a stale ID; expected) |
| `error` | any other error text |
| `empty` | no error but null/empty `data` (silent rate-limiting) |
| `retryable` | transient failure after all retries; written but not checkpointed; counted as an error |

The chunk **fails** (exit 1) if any of these hold:

| Check | Default threshold | Override |
|---|---|---|
| zero rows produced (unless everything was resumed from the checkpoint) | fail | none |
| error rate = (error + retryable) / total | > 20% | `MAX_ERROR_RATE` |
| empty rate = empty / total | > 5% | `MAX_EMPTY_RATE` |
| not-found rate | > 60% (Dunnes 85%) | `MAX_NOT_FOUND_RATE` |
| Tesco only: any record still `retryable` | fail | n/a |

What the outputs look like: `_run_summary_chunk{N}.json` has `status` (`ok`/`failed`), `failures[]` (text reasons), counts, rates, `planned_ids`, `duration_s`, and up to five `error_samples`. ID jobs have their own guard (`IdsRejected` when the list is empty or under 50% of the current one) which is visible only as `status: rejected` in `ids/_run_summary_{date}.json` and does not fail the job.

### 9.3 dbt tests

dbt has four kinds of test here. A test *fails* (blocks the build, exit non-zero) when severity is `error`, the default; with `severity: warn` it is reported but does not block (the Batch OK email lists warnings). Run all tests with the build: `dbtf build ...`, or tests only with `dbtf test ...`.

**Generic tests** (reusable, parameterised; [dbt/tests/generic/](../dbt/tests/generic/)):

| Test | Passes when | Parameters |
|---|---|---|
| `min_row_count` | the relation has at least `min_rows` rows (also catches an empty or short partition) | `min_rows` |
| `null_row_rate` | rows with an `id_column` but NULL in all `value_columns` are at most `max_pct` of all rows (detects "record with no payload", e.g. Tesco 429s) | `id_column, value_columns, max_pct` |
| `accepted_range` | non-NULL values lie within `[min_value, max_value]` (either bound optional) | `min_value, max_value` |
| `unique_combination` | no combination of the listed columns repeats | `columns` |

Built-in dbt tests also used: `unique`, `not_null`, `accepted_values`, `relationships`.

**Source tests** ([raw_s3.yml](../dbt/models/staging/raw_s3.yml)), all `error`: `min_row_count` on each raw source: aldi 1,500; tesco 5,000; dunnes 3,000; supervalu 5,000.

**Staging tests** ([staging/schema.yml](../dbt/models/staging/schema.yml)):

| Model | Test | Severity |
|---|---|---|
| `stg_tesco`, `stg_aldi`, `stg_supervalu` | `min_row_count` (5,000 / 1,500 / 5,000) | error |
| `stg_dunnes` | `min_row_count` 3,000 | error |
| `stg_tesco`, `stg_supervalu` | `null_row_rate` on `[title, price]` > 5% (`*_null_rows_error`) | error |
| | same, > 1% (`*_null_rows_warn`) | **warn** |
| `stg_aldi` | `null_row_rate` on `[title, price_display]` > 5% / > 1% | error / **warn** |
| all four | `id`: `unique`, `not_null`; `supermarket`: `not_null` | error |

**Intermediate tests** ([intermediate/schema.yml](../dbt/models/intermediate/schema.yml)), same pattern for each of `int_tesco`, `int_aldi`, `int_dunnes`, `int_supervalu`:

| Test | Severity |
|---|---|
| `min_row_count` (5,000 / 1,500 / 3,000 / 5,000) | error |
| `id`: `not_null`, `unique` | error |
| `supermarket`: `not_null`, `accepted_values` {tesco, aldi, dunnes, supervalu} | error |
| `title`: `not_null` | **warn** |
| `price`, `was_price`: `accepted_range` 0 to 1000 | error |
| `unit_qty_normalised`: `accepted_range` min 0 | error |
| `unit_normalised`: `accepted_values` {kg, l, m, each} | **warn** |
| `scraped_date`: `not_null` | error |

(I read the full block for `int_tesco` and the same key lines for the others; the column set varies slightly per retailer. `int_all_supermarket_products` and `int_product_title_embeddings` have no tests.)

**Mart tests** ([marts/schema.yml](../dbt/models/marts/schema.yml)):

| Model | Test | Severity |
|---|---|---|
| `fct_product_listings` | contract enforced (column names/types must match); `min_row_count` = var `listing_min_rows` (30,000); `unique_combination` (`supermarket, id`); `id`, `supermarket`, `scraped_date` `not_null`; `supermarket` `accepted_values`; `price`, `was_price` range 0-1000; `unit_qty_normalised` ≥ 0 | error |
| | `title` `not_null`; `unit_normalised` `accepted_values` | **warn** |
| `fct_product_price_daily` | `unique_combination` (`supermarket, id, scraped_date`); `supermarket, id, scraped_date, price` `not_null` | error |
| `fct_price_history` | contract enforced; `unique_combination` (`supermarket, id, valid_from`); `supermarket, id, price, valid_from, is_current, last_seen_date, is_active` `not_null` | error |

**Seed tests** ([seeds/schema.yml](../dbt/seeds/schema.yml)): `supervalu_taxonomy.taxonomy_1` not_null; `supervalu_product_overrides`: `id` not_null + unique, `id` `relationships` to `int_supervalu.id` (**warn**), `confidence` between 0 and 1, `reviewed_at` not_null (error). The seeds are included in `dbt build`.

**Singular tests** (hand-written SQL in [dbt/tests/](../dbt/tests/); a returned row = failure):

| Test | Checks | Severity |
|---|---|---|
| `assert_scrape_gate` | The dbt-level backstop of the scraper gate, computed on the raw source for every retailer in `var('retailers')`: error rate > 20%, empty rate > 5%, or not-found rate > 60% (Dunnes 85%). Keep numbers in sync with the scraper defaults. | error |
| `assert_scrape_error_rate` | Early warning at a quarter of the gate: error rate ≥ 5% or empty rate ≥ 1% per retailer. | **warn** |
| `assert_fct_matches_int_row_counts` | `fct_product_listings` row count equals the sum of the `int_` models for the retailers built this run. | error |
| `assert_price_history_consistent` | Per product: exactly one `is_current` version; versions do not overlap or invert; `last_seen_date` and `is_active` are the same across versions and match a recomputation from the daily table with the grace days. | error |
| `assert_unit_price_matches_effective_price` | For deep discounts (≥ 10% off) on kg/l items, warns if over 5% of rows carry a unit price that equals the old regular price rather than the effective price (Tesco's raw figure did). | **warn** |
| `assert_source_survives_new_attribute_keys` | Two fixture files with different free-form `attributes` keys load with every row and key intact (guards the `union_by_name` read shape). Reads `dbt/tests/fixtures/attributes_drift/` relative to the repo root. | error |

**Unit tests** (dbt unit tests mock the inputs of one model and check its output; 8 in total):

| File | Test | Checks |
|---|---|---|
| [intermediate/unit_tests.yml](../dbt/models/intermediate/unit_tests.yml) | `int_aldi_parses_size_price_and_placeholders` | Aldi pack size/unit from text, display prices parsed, "N/A" ingredients nulled, unavailable products dropped |
| | `int_tesco_price_cut_vs_multibuy_and_units` | only `price_cut` promotions replace price/was_price; units normalise to kg/l; low-fat and organic flags derived from several signals |
| | `int_supervalu_dietary_flags_and_allergens` | dietary flags OR together attribute, tag and category signals; allergens read from the JSON map by status |
| | `int_dunnes_parses_description_html` | ingredients, allergen status (strictest wins on duplicates) and lifestyle tags from description HTML |
| | `int_tesco_count_units_per_item_price` | counted units give per-item prices (4 dp); title pack count that disagrees falls back to 1.0 |
| | `int_supervalu_count_units_per_item_price` | blank-unit counts become per-item prices; m2 is never a count |
| | `int_tesco_unit_price_follows_price_cut` | a price cut rescales the regular-price unit price to the effective price; multibuy leaves it |
| [marts/unit_tests.yml](../dbt/models/marts/unit_tests.yml) | `fct_price_history_marks_disappeared_products_inactive` | grace-day logic per retailer (tesco 2, dunnes 8), each retailer judged on its own latest day, `last_seen_date` on every version row |

**Where each runs.**

| Context | Tests that run |
|---|---|
| Local `dbtf build` (Fusion) | All of the above, including unit tests (all 8 pass under Fusion 2.0.9 per the plan doc; not re-run for this guide) |
| `dbtf test --select "test_type:unit"` | Unit tests only |
| Batch job (`dbt build --target batch --exclude test_type:unit ...`) | Everything **except** unit tests, and minus the nodes downstream of absent retailers' sources. Why: under dbt-core two unit tests error and one differs on fixture typing (they pass under Fusion); this is flagged in the plan as worth fixing. So the Batch job gives no unit-test protection; run them locally with Fusion before changing a model. |
| `backfill.py` | None, deliberately (`dbt run` only) |
| CI (`ci.yml`) | No dbt at all, only the Python tests. The draft `github_workflow_dbt_ci.yml` would add `dbt parse` and `test_dbt_infra.py`. |

The local plan-doc run on 2026-10-26 reported 118 tests passing and 3 pre-existing warnings.

### 9.4 What each failure means and what to do

| Failure | Meaning | Action |
|---|---|---|
| Batch job FAILED email for a chunk | Scraper gate failed or the process crashed | Read the summary (`_run_summary_chunkN.json` → `failures`) and the CloudWatch log; see 10.3 |
| `_run_summary` missing at 14:00 | Chunk never ran or is still running | Check Batch for that retailer; resubmit (10.4) |
| ID refresh `rejected` | New list was empty or under 50% of the old one; old list kept | Check the site/sitemap; use `FORCE_IDS=1` only if the shrink is genuine |
| `min_row_count` fails on a source | Raw partition empty or tiny | The scrape failed; check the summaries; do not build that retailer |
| `null_row_rate` error (>5%) | Many records with an id but no title/price: throttling or API change | Look at `error_samples`; rescrape if transient |
| `null_row_rate` warn (>1%) | Same, milder | Watch the trend |
| `unique` on a staging `id` | Duplicate ids in the day's raw folder: a re-run wrote the same ID twice, or the ID list has duplicates | Remove the superseded batch files for that chunk (10.4) |
| `assert_scrape_gate` | The raw partition breaches scraper limits (a chunk was force-ignored, or thresholds drifted apart) | Same as the scraper gate failing |
| `assert_scrape_error_rate` warn | Error ≥ 5% or empty ≥ 1% | Early warning only |
| `accepted_range` price | A price outside 0-1000 or negative: parsing bug or bad data | Inspect the offending rows in `stg_`/`int_` |
| `unique_combination` on `fct_product_price_daily` | Duplicate key after delete+insert: should not happen; indicates a bug | Stop; do not full-refresh; investigate |
| `assert_fct_matches_int_row_counts` | A mart union dropped or duplicated rows | Investigate the union / `retailers` var |
| `assert_price_history_consistent` | History logic or grace-days mismatch | Rebuild `fct_price_history`; check `active_grace_days` |
| `dbt FAILED` email | Build or a test failed; **catalog not updated**, no built marker | Read the Batch log; fix; resubmit with `RUN_DATE` |
| Contract error on a mart | A model's columns/types differ from `schema.yml` | Update the model or the contract together |

---

## 10. Operations runbook

All commands assume `export AWS_DEFAULT_REGION=eu-west-1 AWS_DEFAULT_OUTPUT=json` and `D=$(date -u +%F)`.

### 10.1 Check a day's health

1. **Did every job finish?**
   ```bash
   for s in RUNNING PENDING RUNNABLE FAILED SUCCEEDED; do
     echo -n "$s: "; aws batch list-jobs --job-queue getting-started-fargate-job-queue \
       --job-status $s --output json | python3 -c "import json,sys; print(len(json.load(sys.stdin)['jobSummaryList']))"
   done
   ```
   (The list is not date-filtered; failed and succeeded jobs are retained by Batch for a limited time. The `/check-jobs` skill in `.agents/skills/check-jobs/` does this plus record counts.)
2. **Summaries**: all 20 files should exist and say `ok`.
   ```bash
   for r in tesco aldi supervalu dunnes; do
     for n in 0 1 2 3 4; do
       aws s3 cp s3://ie-supermarket-data/raw/$r/$D/_run_summary_chunk$n.json - 2>/dev/null \
        | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['retailer'],d['chunk_id'],d['status'],d['planned_ids'],d['ok'],d['not_found'],d['error'],d['empty'],d['failures'])" \
        || echo "$r chunk $n: MISSING"
     done
   done
   ```
3. **ID refreshes**: `aws s3 cp s3://ie-supermarket-data/raw/aldi/ids/_run_summary_$D.json -` (and supervalu). Check the age of the lists: `aws s3 ls s3://ie-supermarket-data/raw/dunnes/ids/latest/` (Dunnes and Tesco are expected to be old; Tesco weekly at best).
4. **Expected row counts** (2026-10-27): per retailer-day, total planned IDs are about Tesco 20,000, SuperValu 17,000, Dunnes 10,600, Aldi 4,600 (5 × the per-chunk figures in 4.2). A big drop is suspicious even if the gate passed.
5. **Email**: absence of an email is not proof of health (the only nightly email is the 14:00 check, and it only sends when something is wrong).

### 10.2 Build the day's data (today, by hand)

```bash
dbtf build --project-dir dbt --vars "{run_date: $D}"
```
Do this after Tesco finishes (about 10:50 UTC). Back up the DB first on important days. Once the Batch pipeline is cut over this is automatic; see 10.5 for the manual trigger.

### 10.3 Reading alerts

| Email subject or text | Source | Meaning |
|---|---|---|
| `Batch job <name> (<id>) FAILED: <reason>` | Batch failure rule | A scraper, ID job or dbt/compaction job exited non-zero or timed out. `<reason>` is e.g. `Essential container in task exited`. |
| `Scraper data-quality problems <date>` | 14:00 summary check | Lines like `tesco chunk 2: no summary for <date>`, `chunk N: [reasons]`, `aldi IDs: rejected <reason>`. |
| `dbt OK for <date>` | dbt job | **OK**: all four retailers built; body lists warnings (names of tests that warned) or "No warnings". |
| `dbt OK (DEGRADED, n missing) for <date>` | dbt job | **DEGRADED**: built without n retailers; the body says whether later slots will retry or it is the last slot. |
| `dbt DEGRADED for <date>: n retailer(s) missing` | dbt job at the final slot | Nothing new to build; these retailers never arrived or failed their gate. Backfill later (10.5). |
| `dbt NOT RUN for <date>: no retailer ready` | dbt job | **NOT RUN**: no retailer passed the precheck at the final slot (or all failed). Exit 1 → also a Batch FAILED email. |
| `dbt NOT RUN for <date>` (body: `state locked by run ...`) | dbt job | Another run holds the lock. See 10.6. |
| `dbt NOT RUN for <date>: no catalog` | dbt job | Catalog missing: lake not seeded or state lost. See 10.6. |
| `dbt FAILED for <date>` | dbt job | **FAILED**: dbt build or test failed; the catalog was not updated. Read the Batch log. |

(The OK/DEGRADED/NOT RUN/FAILED emails exist in code only until the dbt schedule is enabled.)

Logs: Batch jobs log to CloudWatch Logs group `/aws/batch/job` (the default; **unverified** for this account). Find a job's log stream with `aws batch describe-jobs --jobs <id> --output json` (`container.logStreamName`) and read it with `aws logs get-log-events --log-group-name /aws/batch/job --log-stream-name <stream> --output json`.

### 10.4 Re-run a scrape day

Preconditions: understand the hazard in 4.3 (duplicates).

- **A chunk failed its gate or crashed, and you want to retry it.** The checkpoint is still there, so a re-run resumes. But records written during the failed run that were *not* checkpointed (errors, Tesco retryable) will be fetched again and appear twice. Safe procedure: list the day's files for that chunk, delete the batch files `..._chunk{N}_batch*.jsonl.gz` of the **failed** attempt only if you also delete the checkpoint (so the chunk restarts from scratch), then resubmit; or accept duplicates and de-duplicate afterwards. Simplest correct option: delete both that chunk's batch files and its checkpoint, then resubmit.
  ```bash
  aws s3 ls s3://ie-supermarket-data/raw/tesco/$D/ | grep chunk2_
  aws s3 rm s3://ie-supermarket-data/raw/tesco/$D/checkpoints/chunk_2.json
  # delete that chunk's batch files one by one (review the list first), then:
  aws batch submit-job --job-name tesco-scraper-chunk2 --job-queue getting-started-fargate-job-queue \
    --job-definition ie_supermarket_batch_job_definition \
    --container-overrides '{"command":["python","tesco_api.py"],"environment":[{"name":"CHUNK_ID","value":"2"},{"name":"TOTAL_CHUNKS","value":"5"}],"resourceRequirements":[{"type":"VCPU","value":"0.25"},{"type":"MEMORY","value":"1024"}]}' \
    --timeout attemptDurationSeconds=14400
  ```
  (deleting versioned objects only creates delete markers, so it is recoverable until you expire noncurrent versions.) For a retry on a different date than today, add `{"name":"RUN_DATE","value":"YYYY-MM-DD"}` to the environment.
- **A chunk never ran.** Resubmit as above; there is nothing to clean up. The `/submit-jobs` skill ([.agents/skills/submit-jobs/SKILL.md](../.agents/skills/submit-jobs/SKILL.md)) automates submission.
- **Re-run everything.** Invoke the Lambda by hand (`aws lambda invoke --function-name supermarket_batch_scheduler --output json /dev/stdout`). Only do this if no raw data exists for today yet; otherwise you create duplicates.
- **A job running past midnight UTC** would split its output across two date folders; pass `RUN_DATE` to keep one.

### 10.5 Re-run or backfill a day in dbt

- **Local**: `dbtf build --project-dir dbt --vars "{run_date: YYYY-MM-DD}"`. Idempotent for the daily table. For many days use `dbt/scripts/backfill.py` (7.4).
- **Batch (after cutover)**: submit the dbt job for a given day. It re-checks the summaries for that date and builds every ready retailer:
  ```bash
  aws batch submit-job --job-name dbt-manual --job-queue getting-started-fargate-job-queue \
    --job-definition ie_supermarket_dbt_job_definition \
    --container-overrides '{"environment":[{"name":"RUN_DATE","value":"2026-10-26"},{"name":"FINAL_ATTEMPT","value":"1"}]}'
  ```
  `FINAL_ATTEMPT=1` makes it report instead of waiting. A day that is already fully built exits 0 quietly (delete `dbt/state/built/<date>/<retailer>` to force a rebuild of a retailer). Add `{"name":"MAINTAIN","value":"1"}` to force maintenance. **Until the cutover (section 5.8), the stack is in shadow mode (`dbt/state-shadow`), so a manual run only updates the shadow lake.**
- **Order does not matter** for the price tables (delete+insert by date), so late backfills are safe.

### 10.6 dbt job problems

| Symptom | Likely cause | Fix |
|---|---|---|
| `NOT RUN ... state locked by run '...' for X.Xh` | A previous job is still running, or was killed before releasing the lock | If under 3 h, wait. After 3 h the next run takes the lock over by itself. To clear it by hand once you are sure nothing runs: `aws s3 rm s3://ie-supermarket-data/dbt/state/LOCK` (shadow: `dbt/state-shadow/LOCK`). |
| `NOT RUN ... no catalog` | Lake not seeded, or `dbt/state/catalog.ducklake` deleted | Seed with `infra/dbt/seed_lake.py` (7.5) or restore a catalog from `dbt/state/archive/<date>/` (valid ~7 days). Never set `ALLOW_FRESH_LAKE=1` on production unless you mean to start an empty history. |
| `dbt FAILED`, log shows a failed test | Data problem (9.4) | Fix/rescrape, resubmit with `RUN_DATE`. |
| Job exits at once, "nothing ready yet" in log | Scrapes not finished (non-final slot) | Normal; a later slot retries. |
| Job fails reading raw or landing | Missing partition, credentials, or role permissions | Check the dbt job role's policy (5.7) and that the date folder exists. |
| Batch job stuck in RUNNABLE | No Fargate capacity or networking | Check the compute environment (not inspected here). |

### 10.7 Common scraper failures

| Failure | How you see it | Cause and fix |
|---|---|---|
| **Tesco 429s / silent empties** | Tesco chunk FAILED; `error_samples` shows `HTTP 429` or `GraphQL 429`; empty rate high | Akamai rate limiting. Keep the aggregate rate at or below 2.0 req/s (`TESCO_TOTAL_REQ_PER_SEC`); do not raise `TOTAL_CHUNKS` without dividing the rate; re-run the failed chunk off-peak (10.4). Background in [tesco-429-incident.md](tesco-429-incident.md). |
| **Stale IDs** | High `not_found` rate (SuperValu/Dunnes: "Not found in any store"; Tesco: "No product returned"; Aldi/Tesco `HTTP 404`) | The ID list is old. Refresh it (SuperValu/Aldi: `FORCE_REFRESH=1` ID job; Tesco: local script; Dunnes: `scripts/dunnes_ids_from_raw.py`). Gate trips at 60% (Dunnes 85%). |
| **Dunnes Cloudflare** | `dunnes_ids.py` returns nothing / 403 "Just a moment..."; gateway answers 429 with the challenge page | The ID sitemap is challenged; the job is not scheduled. After many requests from one IP the gateway itself can throttle. Use the derive-from-raw script; do not try to bypass the challenge (see [dunnes_ids_investigation.md](dunnes_ids_investigation.md)). |
| **ID list shrank** | `ids/_run_summary_<date>.json` `status: rejected` | The sitemap changed or was blocked. Investigate; `FORCE_IDS=1` only if the smaller list is real. |
| **Tesco ID refresh not publishing** | `ids/latest` older than a week | Run `scripts/run_tesco_ids_local.sh` on a residential connection; read its log. |
| **Duplicate IDs in raw** | `unique` test fails on `stg_*.id` | See 10.4. |
| **Job timed out** | Batch FAILED with timeout reason | Scraper job definition limit is 12,000 s (Tesco 14,400 s via the Lambda). Reduce chunk size by raising `TOTAL_CHUNKS` (keeping Tesco's total rate fixed). |
| **Lambda submit errors** | No jobs for the day; Lambda errors in CloudWatch | Check `supermarket_batch_scheduler` logs; job definition names; queue state. |

### 10.8 Add a new retailer

1. Create `newretailer/newretailer_api.py` (and `_ids.py` if IDs can be discovered), following [aldi/aldi_api.py](../aldi/aldi_api.py): `RunConfig.from_env`, `read_ids`, a `fetch_one` that returns `{product_id, data, error}`, and `run_scrape`. Put shared logic into `scraper_common/`, not the script. Choose the gate thresholds via `RunConfig.from_env(..., max_not_found_rate=...)`.
2. Add `COPY` lines to [Dockerfile.scraper](../Dockerfile.scraper) (and [Dockerfile.ids](../Dockerfile.ids) if the ID script is lightweight).
3. Seed the first ID list into `raw/newretailer/ids/latest/newretailer_product_ids.parquet` (run the ID script locally with `FORCE_REFRESH=1`, or write a Parquet with a `product_id` string column).
4. Add the retailer in [lambda/handler.py](../lambda/handler.py) (`submit_scraper_chunks`, optionally an ID job) and a test in [tests/test_lambda_handler.py](../tests/test_lambda_handler.py).
5. Add `newretailer` to the summary-check `Retailers` parameter ([infra/batch-alerts.yaml](../infra/batch-alerts.yaml)) and redeploy that stack.
6. dbt: add the source in `raw_s3.yml` (with a `min_row_count`), `stg_newretailer.sql`, `int_newretailer.sql` on the **same 46-column schema in the same order** as the others, tests in both `schema.yml` files, add it to `var('retailers')`, to `assert_scrape_gate`'s thresholds if special, to the `accepted_values` lists, to `RETAILERS`/`MIN_LISTING_ROWS` in `infra/dbt/` (`common.py`, `run_dbt.py`), and `active_grace_days`. Update `fct_product_listings` (it loops over `var('retailers')`).
7. Run `uv run pytest -q`, `dbtf build` for a test day, then push (this deploys the scraper, so consider `[skip ci]` until ready).

### 10.9 Backfill raw compaction and lifecycle (rollout steps, manual)

Only follow after reading the plan ([dbt_orchestration_plan.md](dbt_orchestration_plan.md), "Rollout"):
1. Apply [s3_lifecycle_housekeeping.json](../infra/dbt/s3_lifecycle_housekeeping.json) first (noncurrent-version expiry 14 days, `processed/` 3 days, delete-marker clean-up, multipart abort, catalog archive expiry). **This permanently deletes existing noncurrent versions**, including batch files deleted on 2026-05-19 and old `ids/latest` versions. Note that `put-bucket-lifecycle-configuration` replaces the whole configuration.
2. Dry-run and test compaction on one old day: `python infra/dbt/compact_raw.py --run-date <today> --only-date <old day> --dry-run`, then for real on that one day, check it, then the full historical backfill (without `--max-age-days`).
3. Only after that apply [s3_lifecycle.json](../infra/dbt/s3_lifecycle.json), which adds the Glacier Instant Retrieval transitions (raw after 30 days, landing after 90 days, objects over 128 KB only), and enable the compaction schedule.

### 10.10 Rolling back

| What | How |
|---|---|
| A bad scraper/Lambda deploy | Revert the commit on `local_implementation` and push (redeploys). Or re-register a previous job-definition revision: `aws batch describe-job-definitions --job-definition-name ie_supermarket_batch_job_definition --status ACTIVE --output json`, then register a copy pointing at the old image tag (every build is tagged `scraper-<sha>` in ECR). |
| Bad dbt build on Batch | The catalog is only replaced after a green build, so a failed build changes nothing. After a bad *green* build: copy `dbt/state/archive/<earlier date>/catalog.ducklake` back to `dbt/state/catalog.ducklake` (valid for about 7 days because maintenance deletes older Parquet), and remove the `built/` markers for the bad days. |
| Bad local build | Restore `supermarket_data.db` from `~/supermarket_backups/`. |
| Raw data deleted or overwritten | The bucket is versioned: `aws s3api list-object-versions --bucket ie-supermarket-data --prefix <key>` and copy the old version back. Not possible after noncurrent versions expire (14 days once the lifecycle is applied). |
| `ids/latest` damaged | Copy back an older version, or a dated snapshot from `ids/date=.../`. |
| The whole dbt Batch pipeline | Disable the schedules (already disabled): `aws scheduler update-schedule` needs the full schedule definition, so prefer re-deploying the stack with `DbtScheduleState=DISABLED`. Deleting the stack removes the job definitions and roles but not data in S3. |

---

## 11. Cost and security

### 11.1 Cost

Measured numbers are from [dbt_orchestration_plan.md](dbt_orchestration_plan.md) (eu-west-1 list prices; I did not recompute them):

- Raw data was 6.1 GiB in 79,086 objects on 2026-10-26, growing about 51 MiB (560 objects) a day; 88% of objects are under 128 KB. Today's listing shows ~79,650 objects and 6.6 GB of current versions.
- The dbt job: 2 vCPU / 4 GB Fargate for about 6 minutes a day ≈ **0.30 USD a month**. Scraper compute (Tesco's five 0.25 vCPU containers for ~2h47m, the rest 2 vCPU / 8 GB for minutes) was not priced in the docs; **unverified**.
- Storage over 12 months, including compute (plan doc, USD): original plan 16.67; landing + DuckLake with raw in Standard storage 10.94; plus compaction then Glacier IR after 30 days 7.73; plus landing to Glacier IR after 90 days 6.22. Glacier IR only pays off once raw files are compacted, because it bills small objects as 128 KB and charges per transition.
- **The versioning cost nobody modelled**: with versioning on and no noncurrent expiry, every overwritten checkpoint and every deleted object keeps costing money (about 3.3 GB noncurrent raw versions in the plan doc, growing ~0.6 GB a month), and compaction would leave the originals behind as noncurrent versions, saving nothing. That is the reason for the lifecycle files.
- Not included: scraper PUT requests, internet egress, any Postgres catalog (not needed at this scale).
- The dbt and compaction job definitions use public IPs (`AssignPublicIp: ENABLED`), so they add no NAT gateway charge; the scraper job definitions' networking was not inspected (**unverified**).

### 11.2 Security notes

- **No secrets in the repo** by design (`.env`, `.secrets`, `*.db` are git-ignored). Exception to be aware of: Tesco's public web API key `x-apikey` is hard-coded in `tesco/tesco_api.py`; it is the key the tesco.ie website itself sends, not an account credential. AWS credentials come from the environment/role at runtime.
- **The AWS CLI on the maintainer's machine is often used as the root user** (`sts get-caller-identity` returned `:root` in October 2026 per [iam_setup.md](iam_setup.md); root has one access key and MFA). Root credentials bypass all IAM controls. Create a scoped IAM user or SSO permission set ([iam_setup.md](iam_setup.md) has the draft policies) and delete the root access key. The same long-lived keys are probably in the GitHub `production` environment secrets.
- Batch jobs run as `aws_batch_2`, which has full S3, ECR, Secrets Manager and CloudWatch access; the scrapers only need `raw/*`. The new `supermarket-dbt-job-role` is narrowly scoped and cannot delete raw data. The compaction job, however, uses `aws_batch_2` because it must delete.
- Bucket versioning is the main protection against accidental deletion (recovery window = however long noncurrent versions are kept).
- GitHub deploys use access keys, not OIDC; the OIDC deploy role in `infra/iam/` is drafted, not applied.
- Scraping etiquette: the Tesco rate (2 req/s total) was chosen below a measured throttle threshold; Dunnes challenge pages are not to be bypassed.
- SNS alerts go to one email address; anyone with the topic ARN and publish rights can send alerts.

---

## 12. Glossary and where to look next

### 12.1 Glossary

| Term | Meaning here |
|---|---|
| **AWS Batch** | Managed job queue that runs containers on demand. |
| **Fargate** | Serverless container runtime used by Batch; you pick vCPU/memory per job. |
| **Job definition / job / queue** | The template (image, resources, command), one submitted run of it, and the line jobs wait in. |
| **ECR** | AWS's Docker image registry (repo `docker-images`). |
| **EventBridge** | AWS scheduler and event router (cron rules, event patterns). **EventBridge Scheduler** is its newer one-off/recurring scheduler used by the dbt stack. |
| **Lambda** | Short-lived serverless function. |
| **SNS** | Notification topic; here it emails. |
| **CloudFormation / stack** | AWS's infrastructure-as-code; a stack is one deployed template. |
| **S3 / prefix / object / versioning** | Object storage; a "folder" is just a key prefix; versioning keeps old copies of overwritten or deleted objects. |
| **Lifecycle rule** | An S3 rule that expires or moves objects automatically (e.g. delete noncurrent versions after 14 days, move to Glacier IR). |
| **Noncurrent version / delete marker** | A hidden old copy of an object / the placeholder left when a versioned object is deleted. |
| **Glacier Instant Retrieval (IR)** | Cheaper S3 storage class with millisecond access but per-object overhead (128 KB minimum billed). |
| **JSONL / `.jsonl.gz`** | JSON Lines: one JSON record per line; gzipped. The raw format. |
| **Parquet** | Compressed columnar file format used for IDs, landing, DuckLake and exports. |
| **DuckDB** | Embedded analytical database that can query S3 files directly. |
| **dbt / dbt-core / dbt Fusion** | SQL transformation tool; dbt-core is the Python version, Fusion the newer Rust binary. |
| **DuckLake** | DuckDB extension: tables as Parquet files plus a catalog database (here one file). |
| **Catalog** | DuckLake's metadata file `catalog.ducklake`. |
| **Landing** | The Parquet copy of one retailer-day of raw data, used as the dbt source in Batch. |
| **Mart** | Final, analysis-ready tables (`fct_*`). |
| **Staging / intermediate** | The two preparation layers before the marts. |
| **Incremental model / delete+insert** | A table updated by deleting then re-inserting only the keys being loaded. |
| **SCD / version** | Slowly changing dimension: one row per period during which a value was constant. |
| **Contract (dbt)** | An enforced list of column names and types for a model. |
| **Unit test (dbt)** | A test with mocked inputs and expected outputs for one model. |
| **Chunk** | One of the `TOTAL_CHUNKS` slices of a retailer's ID list, scraped by one job. |
| **Checkpoint** | Per-chunk resume file of processed IDs. |
| **Run summary** | Per-chunk JSON with counts and gate result. |
| **Gate** | The pass/fail check at the end of a chunk. |
| **Stale ID** | An ID in our list that the retailer no longer serves. |
| **Degraded day** | A day built without one or more retailers. |
| **Shadow week** | A trial period where the Batch pipeline writes to `*-shadow` prefixes while production is untouched. |
| **Cutover** | Switching the production schedule on after seeding `dbt/state/`. |
| **Embedding** | A numeric vector representing a title or image, for similarity search (`ml/`, `ml_common/`). |
| **uv** | The Python package/environment manager used here. |
| **launchd** | macOS scheduler used for the weekly local Tesco ID refresh. |
| **Akamai / Cloudflare** | Bot-protection/CDN services in front of Tesco and Dunnes. |
| **tpnc** | Tesco's product identifier used as the Tesco ID. |

### 12.2 Where to look next

| Document | Read it for |
|---|---|
| [AGENTS.md](../AGENTS.md) | Short working rules for coding agents and the source of several conventions here |
| [dbt/README.md](../dbt/README.md) | Running dbt, vars, unit prices, price history and grace-day reasoning, backfill commands |
| [dbt_orchestration_plan.md](dbt_orchestration_plan.md) | Design, verification results, costs, rollout order and operating notes for the dbt Batch pipeline (see 5.8 for the live state) |
| [scraper_data_quality.md](scraper_data_quality.md) | The gate, summary fields, local runs, Tesco chunk sizing |
| [tesco-429-incident.md](tesco-429-incident.md) and [tesco-429-error-rate-history.tsv](tesco-429-error-rate-history.tsv) | Tesco rate-limiting investigation and per-day error data |
| [tesco_ids_local.md](tesco_ids_local.md) | Weekly local Tesco ID refresh via launchd |
| [dunnes_ids_investigation.md](dunnes_ids_investigation.md) | Why Dunnes IDs are frozen and the alternatives |
| [iam_setup.md](iam_setup.md) | Current IAM reality and the least-privilege plan |
| [infra/README.md](../infra/README.md) | Alert stack apply/test/remove commands |
| [REVIEW.md](REVIEW.md) | Review checklist for scraper/infra changes |
| [ml_framework.md](ml_framework.md), [ml_audit.md](ml_audit.md), [ml_hf_research.md](ml_hf_research.md) | Embedding framework design, audit of `ml/`, Hugging Face storage research |
| [duckdb2_test.md](duckdb2_test.md) | Why DuckDB 2.0 is not adopted yet |
| [taxonomy-rationale/](taxonomy-rationale/README.md) | SuperValu taxonomy design and per-department rationale |
| [.agents/skills/check-jobs/SKILL.md](../.agents/skills/check-jobs/SKILL.md), [submit-jobs](../.agents/skills/submit-jobs/SKILL.md) | Agent-run job status and submission recipes |
