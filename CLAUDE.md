# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Package Manager

This project uses `uv`. Always run scripts via `uv run python <script>`.

Dependency groups:
- `uv sync` — core only (boto3, polars, requests)
- `uv sync --group scraper` — adds aiohttp, playwright, patchright, curl-cffi
- `uv sync --group ids` — lightweight ID scripts (no browser)
- `uv sync --group notebook --group ml` — analysis and ML
- `uv sync --group dev` — ruff, pre-commit

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

## Dockerfiles

- `Dockerfile.ids` — lightweight, `uv sync --group ids`, used for aldi ID scraping
- `Dockerfile.scraper` — full scraper, `uv sync --group scraper`, includes patchright + Chrome, used for all API scripts and tesco ID scraping
