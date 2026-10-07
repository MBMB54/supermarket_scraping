# Running dbt on a schedule

## Recommendation

1. **Where**: an AWS Batch Fargate job from `infra/dbt/Dockerfile.dbt` (same queue, ECR and IAM model as the scrapers). GitHub Actions runs CI only (parse + unit tests, no data).
2. **Trigger**: phase 1 is an EventBridge Scheduler cron *with a precheck*; phase 2 is event-driven from the scrape orchestrator. Never a bare cron.
3. **run_date**: the orchestrator computes it once and passes `RUN_DATE` to both the scrapers and dbt (the scraper doc already supports `RUN_DATE`); dbt never uses `today()` in production.
4. **Publish**: the canonical state is a `.duckdb` file at `s3://ie-supermarket-data/dbt/state/` (pulled before, pushed after a green build, S3 conditional-write lock) plus Parquet in `processed/`. Local analysis attaches or copies the file. MotherDuck is deferred.
5. **Alerting**: Batch FAILED events and the entrypoint's own OK/FAILED message go to one SNS topic. dbt test errors fail the job; warns are listed in the OK message.

## Why

**Trigger.** Scrapes are chunked Batch jobs across four retailers, so "Batch completed" is many events, and Tesco has been late or broken for weeks. A fixed cron alone either runs on partial data or wastes hours of slack. Phase 1: schedule at the usual finish time plus margin (scrapes start about 08:00 UTC), and have the entrypoint first check that each retailer has `_run_summary_chunk*.json` files with `status: ok` for `RUN_DATE` (see `docs/scraper_data_quality.md`); if not, exit with a distinct code and let Batch retry in 60 minutes (max 4 attempts), then alert. Phase 2: when the Lambda orchestrator knows all chunks finished, it submits the dbt job (or a Step Functions state machine: scrape -> gate -> dbt -> notify). That removes the guesswork and is worth building once the orchestrator is stable.

**Data-quality gate.** The dbt build itself is the gate. `assert_scrape_gate` (the scraper's thresholds: error 20%, empty 5%, not-found 60% / Dunnes 85%) and `null_row_rate` on staging fail the build before anything is published, and the state file is only uploaded after a green build. Verified: on 2026-10-05 the Tesco null-row test and `assert_scrape_gate` both fail (about 70% of Tesco rows are empty); on 2026-08-05 the full build is green.

**Where.** Batch gives: no laptop dependency, IAM role instead of keys, same ECR/deploy path as scrapers, about 3 minutes of runtime for 2 vCPU. Local launchd depends on the laptop being on and awake. GitHub Actions needs an OIDC role plus a 500 MB state file round-trip out of AWS every run, and gives no advantage over Batch.

**Publishing.** Price history (`fct_product_price_daily`) is kept only inside the DuckDB file, so an ephemeral container must restore and re-save that file; hence pull -> build -> push with a lock object (`If-None-Match: *`) so two runs can never interleave, S3 versioning enabled, and a dated copy in `dbt/archive/`. Parquet alone cannot hold the incremental history. MotherDuck would remove the file shuffling but adds a vendor, a token and a second place to pay for a 500 MB database; revisit if more than one person queries concurrently.

## Price history (resolved)

The `check` snapshot was dropped: its `valid_from` is wall-clock time, its key was `id` alone, and it cannot be backfilled out of order. It is replaced by:

- `fct_product_price_daily`: incremental, `delete+insert` on `(supermarket, id, scraped_date)`, `full_refresh: false`. `scraped_date` is now the raw `run_date`, so reruns and backfills in any order are idempotent. Rows with no price (failed fetches) are excluded so they never register as price changes.
- `fct_price_history`: price versions (`valid_from`, `valid_to`, `is_current`) derived from the full daily table, hence correct whatever the load order.

Trade-off: history now lives in the daily table, which must be preserved in the state file (and is rebuilt by replaying run_dates from the raw partitions). The old `scd_product_prices` table in `supermarket_data.db` is left untouched; drop it when satisfied. A product absent for some days keeps its last version open.

## Cost

Fargate 2 vCPU / 4 GB for about 5 minutes per day is roughly 1 USD a month. State file transfer within the region is free; S3 storage for the state file plus 30 daily archives is about 15 GB, under 1 USD. SNS is free at this volume. GitHub Actions CI is within the free tier.

## Secrets

None stored. The Batch job role needs `s3:GetObject/PutObject/DeleteObject` on `dbt/*` and `processed/*`, `s3:GetObject` on `raw/*`, `sns:Publish` on the topic. DuckDB uses the `credential_chain` secret. Only MotherDuck would need a token (Secrets Manager).

## Phased rollout

1. **Local parity (now)**: run `dbt build --vars '{run_date: ...}'` by hand; image and entrypoint drafted in `infra/dbt/`. Verify the project under dbt-core + dbt-duckdb (the image does not use Fusion because its licence in containers is unverified), then build and run the image once manually.
2. **Shadow**: scheduled job with precheck writing to a separate `dbt/state-shadow/` key, SNS to email, for a week; compare with the local database.
3. **Cutover**: point consumers at the S3 state file / Parquet, enable the lock, archive and S3 versioning.
4. **Event-driven**: orchestrator submits the job; optional MotherDuck.

## Draft files (not deployed)

- `infra/dbt/Dockerfile.dbt`, `requirements.txt`, `profiles.yml`, `run_dbt.py`: Batch image and entrypoint (lock, pull, `dbt seed && dbt build`, publish, SNS).
- `infra/dbt/github_workflow_dbt_ci.yml`: CI workflow to copy into `.github/workflows/`.
- Not yet written: the precheck against run summaries, the Batch job definition, the EventBridge rules and the SNS topic.
