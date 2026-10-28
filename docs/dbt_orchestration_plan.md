# Running dbt on a schedule

Status (2026-10-27): **deployed in shadow mode**. Stack `supermarket-dbt-batch` exists with the shadow
prefixes (`dbt/state-shadow`, `ducklake-shadow/`, `processed-shadow/`); both schedules are ENABLED (the
dbt one writes only to the shadow prefixes). Four shadow days were built on real AWS Batch from the image
built by `aws.yml`, and the shadow lake matches the local database exactly (including 2026-10-22, rebuilt
from compacted raw). The lifecycle rules are applied and the raw backfill compaction is done (all dates up
to 2026-10-25). Still pending: the production lake (`dbt/state/`, `ducklake/`) and the cutover; see "Rollout".

## Recommendation

1. **Where**: an AWS Batch Fargate job (`infra/dbt/Dockerfile.dbt`, 2 vCPU / 4 GB, same queue and ECR as the scrapers). GitHub Actions only runs CI (parse + helper tests, no data).
2. **Trigger**: EventBridge Scheduler, four slots a day (11:30-14:30 UTC). **Retailers are published independently**: each slot builds every retailer whose scrape is complete and passed its gate and leaves the rest out (a degraded day, alerted as `dbt OK (DEGRADED, n missing)`), instead of blocking all four on one. A later slot rebuilds the day with a retailer that finished late (idempotent). A slot with nothing new exits 0 quietly; the 14:30 slot reports retailers that never arrived. It exits non-zero only if no retailer could be built or the build fails. Phase 2 (orchestrator submits the job) is unchanged.
3. **run_date**: derived from the scheduler's scheduled time (or `RUN_DATE`) and passed to dbt explicitly. dbt itself still defaults to today's UTC date when `run_date` is omitted (via `run_started_at`, which works in dbt-core and Fusion), but the Batch job never relies on that.
4. **Layers**:
   - `raw/` gzipped JSONL, written by the scrapers, never changed; the source of truth.
   - `landing/{retailer}/scraped_date={date}/data.parquet`: one zstd Parquet per retailer-day, produced by `infra/dbt/landing.py` from the same `read_json(..., union_by_name=true)` dbt used, so staging models are unchanged.
   - DuckLake (`ducklake/` Parquet + a small catalog file) holds the marts, i.e. the price history. Staging/intermediate are views in an in-memory DuckDB, rebuilt each run.
   - `processed/all_supermarket_products.parquet` is still written by `int_all_supermarket_products` for `ml/embeddings.py`.
5. **Publish** (built markers are per retailer-day: `built/{date}/{retailer}`): pull the catalog file (~4 MB), `dbt build`, and push it only after a green build; an S3 conditional-write lock (with a 30 minute stale-lock takeover, since runs take about 2 minutes and Fargate Spot can kill one) surrounds the run. Failed runs leave only unreferenced Parquet, removed by weekly maintenance. A run with **no catalog refuses to start** (`ALLOW_FRESH_LAKE=1` overrides): in production a missing catalog means "not seeded" or "state lost", and building from nothing would fork the history away from `seed_lake.py`'s copy.
6. **Raw compaction**: a separate daily job merges each retailer-day's ~140 batch files into one object (row counts verified), so a Glacier Instant Retrieval lifecycle rule is worth having. The scheduled job only touches dates 2-7 days old (`--max-age-days 7`); the 155-day backfill is a deliberate manual run. MotherDuck and Quack are deferred (below).
7. **Alerting**: Batch FAILED events (existing rule) plus the entrypoint's own OK/FAILED/NOT RUN message to the same SNS topic; the OK message lists dbt test warnings.

## What was verified (local, 2026-10-26)

| Check | Result |
|---|---|
| dbt-core 1.10 + dbt-duckdb 1.10 + DuckDB 1.5.6, DuckLake 1.0, `batch` target, full build | green, 118 tests pass, 3 warns (pre-existing) |
| `fct_product_price_daily` `delete+insert` on DuckLake; build 08-05, 08-06, rerun 08-05 | no duplicates, counts unchanged; rows identical to the local DB (0 differing both ways) |
| Seed: copy 6.07M history rows from `supermarket_data.db` | 1.7 s, 8.8 MB Parquet + 3.9 MB catalog (the 618 MB DuckDB file is ~10 MB as Parquet) |
| Seeded lake + real build of 2026-10-25 | history (`fct_product_price_daily`, `fct_price_history` versions) identical to the local DB for 2026-02-01..10-23; new day added; forced rerun idempotent |
| Landing vs raw as the source | marts identical (0 rows differ both ways); build 21 s vs 2 m 47 s |
| dbt-level gate failure (2026-10-05, Tesco null rows, with the scraper summaries faked ok) | dbt fails, catalog byte-identical, lock released, no built marker |
| Nothing ready | non-final slots exit 0 quietly, the final slot exits 1 with an alert |
| Degraded day: 2026-10-25 with Tesco not finished | builds aldi/dunnes/supervalu (99 tests green), Tesco absent, `processed/` export skipped, only those three marked built; next slot with nothing new is quiet |
| Then Tesco arrives | full rebuild of the day (118 tests green), export written; lake identical to a single full build (0 differing rows in price_daily, listings, price_history; no duplicates) |
| Compaction of one real day (4 retailers) | 540 objects -> 4, row counts identical (20,025 / 4,560 / 10,594 / 17,262), same glob still reads them; `--dry-run`, rerun and the min-age guard behave |
| Real S3, no `OUTPUT_DIR`, all prefixes under `smoketest/` (deleted afterwards): refusal with no catalog, S3 conditional-write lock refusing a second holder, seed, real precheck on the real run summaries, build, rerun | all as designed, 39 s end to end. The stale-lock takeover on S3 (`HeadObject` age) was only tested on local disk |
| Fusion 2.0.9 `parse` with no `--vars` (IDE/language server) | works; `ls` shows local marts still resolve to the `supermarket_data` database |
| `dbt compile` without `run_date` (dbt-core and Fusion) | resolves to today's UTC date; an explicit var overrides |
| Fusion 2.0.9 on the changed project (local `dev` target) | parses; all 8 unit tests pass |

## Things that changed in the dbt project

- The `run_date` default is now `run_started_at.strftime('%Y-%m-%d')` instead of `modules.datetime.timezone`, which dbt-core lacks. dbt-core renders source config without macros, so `raw_s3.yml` repeats the expression inline.
- Source location switches on `use_landing` (default false: raw, as before, so local runs and `scripts/backfill.py` are unchanged; the Batch job sets true).
- Marts get `+database: lake` only when `target.name == 'batch'`.
- The `primary_key` constraint on `fct_product_listings` was dropped: DuckLake cannot enforce it. The `unique_combination` test covers it.
- The Batch job passes `--exclude test_type:unit`: two unit tests error and one differs on fixture typing under dbt-core (they pass under Fusion). Run them locally with Fusion. Worth fixing later.
- The image runs DuckDB 2.0 (`duckdb>=2.0.0,<3`, upgraded 2026-10-28; see "DuckDB 2.0" below). The models were migrated to `lambda x: ...`, which works on 1.5 and 2.0.
- `scraper_common/{config,storage}.py` gained `from __future__ import annotations` (the dbt image is Python 3.12; dbt-core does not run on 3.14) and `Storage` gained `put_if_absent`, `list_keys`, `delete_many`.

## Cost (eu-west-1 list prices from the AWS Pricing API, measured sizes)

Measured: raw is 6.1 GiB in 79,086 objects, growing 51 MiB/day (560 objects/day); 88% of objects are under 128 KB. Landing Parquet is 0.65x the raw size. Fargate 2 vCPU / 4 GB is about 6 minutes a day, about 0.30 USD a month. 12-month cumulative, including compute:

| Scenario | 12 months | Month 12 | 36 months |
|---|---|---|---|
| Original plan (state file, 30 archives, raw Standard) | 16.67 | 1.58 | 64.83 |
| Landing + DuckLake, raw Standard | 10.94 | 1.22 | 57.28 |
| + Glacier IR with AWS defaults | 10.81 | 1.16 | 53.77 |
| + Glacier IR forced onto every raw object | 13.05 | 1.19 | 51.16 |
| + compact raw first, then Glacier IR after 30 days | 7.73 | 0.80 | 35.42 |
| same, landing to Glacier IR after 90 days | 6.22 | 0.56 | 22.59 |

Glacier IR only pays once the raw files are compacted: small objects are billed as 128 KB and each transition request costs 0.00002 USD. **The bucket has versioning enabled and no noncurrent-version expiry**, which the table above did not model: it already holds ~3.3 GB of noncurrent raw versions (the scrapers rewrite each growing `checkpoints/chunk_N.json` after every batch, about 20 MB a day, ~0.6 GB a month, never expiring), 0.4 GB in `processed/`, and compaction deletes would also leave the originals behind as noncurrent versions. `infra/dbt/s3_lifecycle.json` therefore expires noncurrent versions (14 days; 3 days for `processed/`, which the dbt job overwrites daily) and cleans delete markers; without it compaction saves nothing and cost grows. The 14 days double as the recovery window for a compaction mistake. Applying it permanently deletes the existing noncurrent versions, including 195 batch files deleted on 2026-05-19 and old `ids/latest` versions. Not included: scraper PUTs (unchanged), internet egress, and a Postgres catalog (not needed at this scale; a DuckDB catalog file under the lock is enough).

## Rollout (in this order)

1. **Review and merge** the code (commits to `local_implementation` need `[skip ci]`; `Dockerfile.dbt` is not part of the CI image build). Build the image with `--platform linux/amd64` on Apple Silicon (the job definitions are x86_64); it has never been built.
2. **Build and push the image; deploy** `infra/dbt-batch.yaml`. The image is built and pushed by the `build-dbt-image` job in `.github/workflows/aws.yml` on every push to `local_implementation` (tag `dbt-<sha>`); once the stack exists the same job registers a new revision of both job definitions with the new image, and the schedules reference the definitions by name so they always use the newest revision. Parameters for this account: `Image` = the pushed `docker-images:dbt-<sha>` URI, `JobQueueArn` = `arn:aws:batch:eu-west-1:557690579730:job-queue/getting-started-fargate-job-queue`, `ExecutionRoleArn` and `ScraperJobRoleArn` = `arn:aws:iam::557690579730:role/aws_batch_2` (the role the scraper job definitions already use; it has `AmazonS3FullAccess`, so compaction can delete), `AlertTopicArn` = `arn:aws:sns:eu-west-1:557690579730:supermarket-scraper-alerts` (the `supermarket-scraper-alerts` stack is already deployed). Both schedules deploy `DISABLED` (`DbtScheduleState`, `CompactScheduleState`); nothing runs until you enable them.
3. **Shadow week**: deploy (or override) with `StatePrefix=dbt/state-shadow`, `LakeData=s3://ie-supermarket-data/ducklake-shadow/`, `ProcessedPrefix=s3://ie-supermarket-data/processed-shadow` (never a path under `ducklake/`: its orphan cleanup would delete it). Seed the shadow catalog first with `seed_lake.py` and the same env, enable only the dbt schedule, and compare with the local database. Delete the shadow prefixes afterwards (`landing/` is shared and idempotent).
4. **Cutover**: bring `supermarket_data.db` up to date (last loaded day was 2026-10-23), run `seed_lake.py` once against production state, then set `ScheduleState=ENABLED` for the dbt schedule (a run before seeding now refuses). Days missed in between are built by submitting the job with `RUN_DATE=<day>` (the precheck needs that day's `_run_summary` files, which exist only for recent days). **Local consumers are not fed by the Batch job**: `ml/agents.py`, `ml/classify_orphan_products.py`, `ml/export_data_to_hf.py` and the notebooks read tables in `supermarket_data.db` (including `int_supervalu` and the embeddings, which the lake does not persist). Either keep running local `dbtf` builds for them, or re-point them to an attached copy of the catalog for the marts.
5. **Compaction** (manual, by hand): `compact_raw.py --run-date <today> --only-date <old day> --dry-run`, then that one real day, check it, and only then the full backfill of the ~155 historical days without `--max-age-days` (it deletes the original batch objects; the bucket **is versioned**, so they stay recoverable as noncurrent versions until the lifecycle rule expires them after 14 days, and the storage saving only appears then). Only after that enable the compaction schedule. Side effect: `scripts/dunnes_ids_from_raw.py --date <old day>` recognises complete scrapes by chunk file names and will not accept a compacted day (its default newest-first scan is unaffected); `dbt/scripts/backfill.py` only lists date folders and globs `*.jsonl.gz`, so it is unaffected.
6. **Lifecycle**: `infra/dbt/s3_lifecycle_housekeeping.json` (noncurrent-version expiry, delete-marker cleanup, multipart abort, catalog archive expiry) can be applied at any time and is worth doing first; `infra/dbt/s3_lifecycle.json` adds the Glacier IR transitions and goes on only **after** the historical compaction finishes; otherwise the 128 KB+ originals transition first and compaction then pays retrieval and early-delete charges. The bucket has no lifecycle configuration today; `put-bucket-lifecycle-configuration` replaces the whole configuration.
7. **Event-driven trigger** from the orchestrator, later; MotherDuck or a Postgres catalog only if a second writer or reader appears.

## IAM

New role `supermarket-dbt-job-role` (`infra/iam/dbt-job-role-policy.json`): read `raw/*`; read/write/delete `landing/*`, `ducklake/*`, `dbt/*`, `processed/*`; list those prefixes; `sns:Publish`. It cannot delete raw data. The compaction job reuses `supermarket-batch-job-role` (raw read/write/delete). Scheduler needs `batch:SubmitJob` on the queue and both job definitions.

## DuckDB 2.0

Tested 2026-10-28 (dbt-core 1.10, dbt-duckdb 1.10, DuckDB 2.0.0, DuckLake and all other extensions available):
the pipeline builds on 2.0.0 and on 1.5.6 with identical results (0 differing rows in `fct_product_price_daily`, `fct_product_listings`, `fct_price_history`; 118 tests green on both; Fusion's 8 unit tests pass).

- **Lambda syntax** (done): the 12 single-arrow lambdas (`int_aldi`, `int_tesco`, `allergens.sql`, `stg_tesco`) are now `lambda x: ...`. No temporary setting needed. (`SET lambda_syntax='enable_single_arrow'` also works but is removed in DuckDB 2.1.)
- **Not the only blocker, storage format**: 2.0 writes storage version 69, and when it modified an existing 1.5.6-created DuckLake catalog the result could no longer be opened by 1.5.6 ("cannot initialize database with storage version 7 - which is lower than what the database itself uses (69)"). `storage_compatibility_version` works for ordinary DuckDB files but did not make a *DuckLake* catalog readable by 1.5.x. So the lake upgrade is one-way: every reader (notebooks on the project env's 1.5.3, Fusion's embedded 1.5.5 if it ever attaches the lake) must be on 2.0 first, or the Batch image stays on 1.5.x.
- **Python client**: 2.0 removed relation-level `fetchone()`/`fetchmany()`; `ml/agents.py` now uses `rel.limit(n).fetchall()` (valid on both). The pipeline code only uses connection-level `execute().fetchone()`.
- **`icu`** moved out of core; added to the image's pre-installed extensions.
- Checked and not affected: regex `~` semantics, `//`/`%` by zero, untyped `NULL` columns, `X''`/`B''` literals, `MERGE`, S3 URL parameters (none used).
- Upgrade done 2026-10-28, readers first: the project env is locked to DuckDB 2.0.0 (`pyproject.toml` floor raised to `>=2.0.0`; `ml/agents.py` hybrid BM25 + cosine search verified on a copy of the local database), then the Batch image lifted its pin to `duckdb>=2.0.0,<3`. A plain DuckDB file written by 2.0 stays readable by 1.5.6 (tested on a copy of `supermarket_data.db`), so the local Fusion workflow is unaffected; only DuckLake catalogs are one-way. Python 2.0 note: array columns (e.g. `FLOAT[640]`) come back as tuples, not lists, so `.tolist()` before interpolating into SQL.

## Operating notes

- AWS Batch retries do not wait, so retries come from the scheduler slots, not a Batch retry strategy.
- Verified 2026-10-27: the Scheduler universal-target `Input` for `batch:submitJob` works (the 14:30 UTC slot fired and built the day). Still only tested on local disk: the stale-lock age is computed from S3's `LastModified` against the local clock (a negative age was observed from clock skew; harmless).
- **Degraded days**: a retailer is excluded if its scrape summaries are missing/incomplete (retried by later slots) or a chunk failed the scraper's own gate (not retried; reported at the final slot). Excluded retailers' sources, models and tests are skipped via `--exclude source:external_source.<retailer>+`, the cross-retailer tests loop over the `retailers` var, and the `fct_product_listings` row floor drops to the sum of the present retailers' source minimums. On a degraded day `int_all_supermarket_products` is **not** built, because it would overwrite the full `processed/all_supermarket_products.parquet` that `ml/` reads with a subset; the catalogue stays one day stale until a full day builds. A retailer that never arrives is backfilled later by running the job with `RUN_DATE=<day>` once its summaries pass.
- **Still fail-closed**: a dbt test or model failure inside the build (the dbt-level gate is a backstop for what the scraper summaries miss) still blocks the whole build, including healthy retailers. Retrying without the offending retailer (by mapping failed nodes in `run_results.json` to retailers) is the obvious next step but is not implemented.
- Weekly (Sundays, or `MAINTAIN=1`) the job expires snapshots older than 7 days, merges the one-small-file-per-day Parquet, and deletes unreferenced files. Superseded versions of the rewritten marts (`fct_product_listings`, `fct_price_history`) are the main storage there.
- A catalog copy per day is kept in `{STATE_PREFIX}/archive/` (8-day expiry, matching the 7-day snapshot retention: an older catalog would reference Parquet that maintenance already deleted, so catalog rollback works within about a week).
- Consumers: attach a pulled copy of `dbt/state/catalog.ducklake` with the same `DATA_PATH`, or read `processed/` Parquet as before.
- `fts_main_int_supervalu`, `supermarket_data.db` extras and the notebooks' use of the local file are untouched; the Batch job does not build the FTS index.
- Deferred: MotherDuck (managed catalog and storage; needs pricing and bring-your-own-bucket confirmed) and Quack (client-server protocol; DuckDB 2.0 is out but the DuckLake-catalog integration was beta; only useful for an always-on multi-writer server).

## Files

- `infra/dbt/`: `Dockerfile.dbt` (+ `.dockerignore`), `requirements.txt`, `profiles.yml` (`batch` target), `common.py`, `landing.py`, `precheck.py`, `lake.py`, `seed_lake.py`, `compact_raw.py`, `run_dbt.py`, `s3_lifecycle.json`, `github_workflow_dbt_ci.yml`.
- `infra/dbt-batch.yaml` (job definitions, role, schedules; validated with `validate-template` only), `infra/iam/dbt-job-role-policy.json`.
- Tests: `tests/test_dbt_infra.py` (local directories only).
- Not written: the SNS-to-email subscription wiring beyond `batch-alerts.yaml`, and the event-driven trigger.
