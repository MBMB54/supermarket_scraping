# Scraper data-quality gate and run summaries

Applies to aldi, dunnes, supervalu (via `scraper_common/`); Tesco should adopt it (see bottom).

## Where things are written (unchanged raw schema)

- Data: `s3://ie-supermarket-data/raw/{retailer}/{YYYY-MM-DD}/{retailer}_raw_{ts}_chunk{N}_batch{NNNN}.jsonl.gz`
  with records `{product_id, data, error}` (aldi) or `{product_id, store_id, data, error}`
  (supervalu, dunnes). Identical to before.
- Summary per chunk: `raw/{retailer}/{date}/_run_summary_chunk{N}.json` (does not match the dbt
  `*.jsonl.gz` glob). Also logged to stdout as one JSON line (`"event": "scrape_summary"`).
- Checkpoint: `raw/{retailer}/{date}/checkpoints/chunk_{N}.json` now also holds `stats`.
- ID refresh summary: `raw/{retailer}/ids/_run_summary_{date}.json` (`status` ok | rejected).

Summary fields: `total, ok, not_found, error, empty, retryable, resumed_skipped, error_samples,
error_rate, empty_rate, not_found_rate, status (ok|failed), failures[], planned_ids, duration_s`.

## Gate (process exits 1 => Batch job FAILED; checkpoint kept)

| Check | Default | Env override |
|---|---|---|
| zero rows produced | fail | - |
| error rate (error + retryable) | 20% | `MAX_ERROR_RATE` |
| empty/null payload rate (`data` null/empty and `error` null) | 5% | `MAX_EMPTY_RATE` |
| not-found rate (`Not found in any store`, `HTTP 404`) | 60% (dunnes 85%) | `MAX_NOT_FOUND_RATE` |

## Fields dbt should expect

- `data` may be NULL with `error` NULL (empty bucket) for aldi when the API returns 200 without a
  `data` key; treat as a failed fetch.
- `error` values: `Not found in any store` (stale ID, supervalu/dunnes; only emitted when store
  misses outnumber transient errors), `HTTP <status>`, `<ExceptionType>: <msg>` for transport errors.
  Previously transient failures in supervalu/dunnes were also labelled `Not found in any store`;
  now they carry the real error, so `assert_scrape_error_rate` sees outages.
- A failing run still writes its rows to S3; they remain in the dbt source glob.
- Dunnes had 43% `Not found in any store` in a sampled 2026-10-05 batch, hence its higher threshold (85%, leaving headroom; `stores_per_chunk` stays at 60). Rationale: many items are stocked in only a few of the 103 stores (an ID checked against all stores was found in only 3), plus Dunnes `ids/latest` was last refreshed 2026-07-05 (the sitemap fetch is blocked by Cloudflare), so many IDs are discontinued. The threshold is a backstop for outages, not a coverage target.

## Local runs

`OUTPUT_DIR=/some/dir CHUNK_ID=0 TOTAL_CHUNKS=200 MAX_IDS=20 uv run python aldi/aldi_api.py`
writes everything (batches, checkpoint, summary) under `OUTPUT_DIR` instead of S3. IDs are still read
from S3 unless `IDS_FILE` points at a local parquet. `RUN_DATE=YYYY-MM-DD` overrides the date folder
(useful so a job straddling UTC midnight writes one folder).

## Tesco adoption (done)

`tesco/tesco_api.py` and `tesco/tesco_ids.py` now use `scraper_common`. The pacer, Retry-After/backoff
and GraphQL-body classification are unchanged; soft (still-throttled) records are written but left out
of the checkpoint and fail the run (`fail_on_retryable=True`). `TESCO_OUTPUT_DIR` became `OUTPUT_DIR`.
ID jobs: `ids/latest` is only overwritten after the empty/shrink guard; `TEST_MODE` now requires `OUTPUT_DIR`
(it previously overwrote `ids/latest`). The stale-latest exit 1 (`MAX_STALE_DAYS`, rejected ID job and `ids/latest` older than N days) is **disabled by default** (0); set e.g. `MAX_STALE_DAYS=14` to enable it.

## Tesco chunk compute (measured 2026-10-08)

Tesco chunks are pacing-bound (`TESCO_TOTAL_REQ_PER_SEC=2.0` split over 5 chunks = 0.4 req/s each), not CPU bound.
Fargate test, `CHUNK_ID=1 TOTAL_CHUNKS=5 MAX_IDS=300 OUTPUT_DIR=/tmp/out` (nothing written to S3 raw):

| Resources | req/s | ok | 429s | peak RSS | CPU time / wall |
|---|---|---|---|---|---|
| daily run, 2 vCPU / 8 GB (chunk 1, 4,005 ids) | 0.400 | 100% | 0 | n/a | n/a |
| 0.5 vCPU / 2 GB | 0.401 | 300/300 | 0 | 113 MB | 2.7 s / 752 s |
| 0.25 vCPU / 0.5 GB | 0.401 | 300/300 | 0 | 112 MB | 3.3 s / 759 s |

`lambda/handler.py` now overrides Tesco chunk jobs to 0.25 vCPU / 1 GB (`TESCO_RESOURCE_REQUIREMENTS`); other retailers keep the job
definition's 2 vCPU / 8 GB. The 1 GB (rather than 0.5 GB) leaves headroom for a full 4,000-id chunk's checkpoint and retry bursts.
Revert by emptying that constant's use in `submit_scraper_chunks`.
