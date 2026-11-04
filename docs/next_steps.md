# Next steps (written 2026-11-04)

For a fresh session. Everything below was verified against the repo and AWS on 2026-11-04; re-check anything
that may have moved (`git log`, `aws cloudformation describe-stacks`, `max(scraped_date)` in the local DB).
Companion docs: `docs/agent_migration_plan.md` (move `ml/agents.py` onto the new tables),
`docs/ml_embed_job.md` (drafted AWS text-embedding job), `docs/dbt_orchestration_plan.md` (the live dbt
pipeline), `docs/ml_framework.md` (embedding framework and reference notes), `docs/infrastructure_guide.md`.

## 1. What is live, drafted, retired

| Thing | State |
|---|---|
| Scrapers + alerts | Live. Lambda `supermarket_batch_scheduler` 08:00 UTC; stack `supermarket-scraper-alerts` (SNS to the owner). Tesco chunks run at 0.25 vCPU / 1 GB. |
| dbt pipeline | Live on the production prefixes since 2026-11-02: stack `supermarket-dbt-batch` (schedule `supermarket-dbt-daily`, `cron(30 11-14 * * ? *)`, a built-check at 15:00, raw compaction at 15:30). Marts in a DuckLake catalog (`s3://ie-supermarket-data/dbt/state/catalog.ducklake`, data `ducklake/`). It excludes `tag:embeddings` and `tag:search`. |
| Local DB `supermarket_data.db` | Last built through **2026-10-29**. Has `fct_embeddings`, `emb_gemma2_text`, `dim_embedding_specs`; **no `dim_product_search`** yet. Not fed by the Batch job. Backups in `~/supermarket_backups/` (10-09, 10-20, 10-29). |
| Embedding store (text) | `s3://ie-supermarket-data/ml/embeddings/<spec_id>/` : `gemma2-text` `268dad232ccc02bc` (50,042 rows), `harrier-title` `25173d59826a669a` (49,976 rows). Read by dbt (`models/embeddings/`). |
| HF bucket | Private `brianbarry97/supermarket-embeddings`: `catalogue/` (pushed by hand on 2026-10-23, now stale), `images/` + `image_map/` (49,952 images mapped), `embeddings/` for `gemma2-text`, `gemma2-image-t140` `badcc88197ce60b4`, `gemma2-image-text-t140` `c0ddd74e3236c671`. dbt cannot read the image specs. Image embedding is manual by decision. |
| Search table | `dim_product_search` + BM25 post-hook + `bm25_search()` macro: committed, built and tested on a scratch DB only. Local targets only (`var fts_targets`). |
| AWS text-embed job | Drafted and validated (`infra/embed-batch.yaml`, `infra/embed/`, `infra/iam/embed-job-role-policy.json`), **not deployed**. |
| Retired (2026-11-04) | `ml/embeddings.py`, dbt model `int_product_title_embeddings` and var `embeddings_input_prefix`, `ml/export_data_to_hf.py`, `ml/download_images_bytes_hf.py`, `ml/image_embeddings_hf.py`. `ml/agents.py` still queries the old local table until it is migrated. |

`parquet_output_prefix` (dbt var) is **not** part of the retired path. It is where `int_all_supermarket_products`
writes `processed/all_supermarket_products.parquet`, the catalogue read by `ml_common` (`CATALOGUE_URI` default),
the embed job (`CatalogueUri`), the HF catalogue push, `run_dbt.py`'s degraded-day skip, the stack parameter
`ProcessedPrefix` and `dbt/scripts/backfill.py --parquet-prefix`. Renaming it (e.g. to a catalogue-export name)
touches production stack parameters, so it is an optional later cleanup (decision D5).

## 2. Fresh backfill (the owner has noticed data issues; list them here before starting)

Data issues found: _to be filled in by the owner._

Constraints that shape the sequence:
- The local DB is the **source of the seed** for the production lake, and the lake appends a day at a time
  from there. If model logic changes the history, the lake must be re-seeded from the rebuilt DB.
- `infra/dbt/seed_lake.py` **refuses to run if `dbt/state/catalog.ducklake` exists**, so a reseed means
  removing the production catalog and data first (a destructive, owner-run step; the bucket is versioned,
  noncurrent versions expire after 14 days).
- Raw days are compacted into one `compacted.jsonl.gz` per retailer-day; the source glob (`*.jsonl.gz`)
  matches it, so replays still read them. Raw older than the lifecycle threshold sits in Glacier Instant
  Retrieval (reads work, small retrieval fee).
- `dbt/scripts/backfill.py` replays raw days with gates and tests off (models only). A full replay from
  2026-02-01 was done end to end on 2026-10-21 and takes about 100 minutes of awake, plugged-in laptop
  (the Mac sleeps on battery and stalls S3 reads; use `caffeinate -dimsu`).
- Tesco days 2026-08-09 .. 2026-10-07 mostly lost prices to Akamai rate limiting. Replay drops NULL-price
  rows by design, so those days carry only the priced rows.
- Embeddings are keyed by `content_key` (hash of the embedded text/image): after a rebuild only rows whose
  `title_cleaned` / image changed get re-embedded.

Recommended sequence (each step reversible until step 6):
1. Pause the dbt schedule: redeploy `supermarket-dbt-batch` with `DbtScheduleState=DISABLED` and every other
   parameter unchanged (current values: `aws cloudformation describe-stacks --stack-name supermarket-dbt-batch`;
   the existing deploy recipe is in `docs/dbt_orchestration_plan.md`, Rollout). Leave `CompactScheduleState` as is.
2. Back up: `cp supermarket_data.db ~/supermarket_backups/supermarket_data_$(date +%Y%m%d).db`; copy the
   catalog (`aws s3 cp s3://ie-supermarket-data/dbt/state/catalog.ducklake ~/supermarket_backups/`).
3. Fix the models (dbt/), run `dbt parse` and the unit tests (`dbtf test --select test_type:unit`).
4. Rebuild into a **new** DuckDB file instead of dropping tables: copy `~/.dbt/profiles.yml` to a scratch
   dir, point `path` at e.g. `supermarket_data_rebuilt.db`, then
   `uv run python dbt/scripts/backfill.py --start 2026-02-01 --end <last complete day> --profiles-dir <scratch dir> --db supermarket_data_rebuilt.db --parquet-prefix /tmp/processed --dbt ~/.local/bin/dbt`
   followed by `~/.local/bin/dbt build --project-dir dbt --profiles-dir <scratch dir> --vars '{run_date: <last day>, parquet_output_prefix: /tmp/processed}'`
   (a full build also produces `fct_embeddings`, `dim_product_search` and its BM25 index).
5. Compare the new file with the old one (row counts per retailer per day, `EXCEPT ALL` on unchanged
   columns, sampled products); decide it is correct.
6. Re-seed production (destructive, owner-run): remove `dbt/state/catalog.ducklake` and the `ducklake/`
   data prefix (and `dbt/state/archive/`, `dbt/state/built/` markers), then
   `python infra/dbt/seed_lake.py --local-db supermarket_data_rebuilt.db` with the production `STATE`/`LAKE`
   environment (see the module docstring and `docs/dbt_orchestration_plan.md`).
7. Replay any days after the local DB's last day through Batch with `RUN_DATE=<day>`, then re-enable the
   schedule (`DbtScheduleState=ENABLED`).
8. Swap the rebuilt file into place (keep the old one), refresh the HF catalogue copy (D1/D2), then re-run
   the embedding specs; only changed rows re-embed.

## 3. Decisions (owner), with the plan for each

**D1. How the agent gets a fresh DB with the search index.** DuckLake cannot hold the BM25 index and the Batch
job does not feed the local file. Options: (a) keep a local refresh: `backfill.py --resume` for the missing
days plus a `dbtf build` (close notebook connections first; fine as a stop-gap); (b) **recommended**: a final
step in the scheduled job builds a small derived `search.duckdb` (search table + BM25 index + embeddings +
`fct_price_history`) in the container and uploads it to S3; the agent attaches it read-only over S3 (verify
`ATTACH 's3://…/search.duckdb' (READ_ONLY)` keeps the FTS schema usable on DuckDB 2.0 before building
anything, and give the job role write access to the artifact prefix); (c) point the agent at an attached copy
of the lake for marts and keep only the index local (most complex).

**D2. Deploy the AWS text-embedding job** (`docs/ml_embed_job.md` has the full design, measurements and
deploy order). Steps: apply the documented `.github/workflows/aws.yml` patch and push (CI builds
`embed-<sha>`); deploy `supermarket-embed-batch` with `TriggerState=DISABLED`; run `gemma2-text` alone (should
embed only the rows added since 2026-10-26); run `harrier-title` (its table now exists, so the first run is
incremental, not 50k rows); enable the trigger. Open parameters: failure gate (3 rows and 5%), 30-minute
stale-lock window, models downloaded at start vs an S3 mirror. If the dbt scheduled build should include the
embeddings models later, the dbt job role needs read access to `ml/*` and `tag:embeddings` must come out of
the exclusion in `infra/dbt/run_dbt.py` (and `tests/test_dbt_infra.py`).

**D3. Fast-forward `main`.** `main` is behind `local_implementation` by several commits (`git rev-list --count
origin/main..origin/local_implementation`); `git push origin local_implementation:main` is a clean
fast-forward (`main` is an ancestor). Push without `[skip ci]` only when you want CI to rebuild the images.

**D4. Small dbt choices.** Remove `prod` from `fts_targets` (it points at the same file as `dev`); compute
`dim_embedding_specs.embedded_from/embedded_to` from current embeddings only (they span superseded ones today);
add a unit test for `dim_product_search`; register the `bm25_search` and `build_search_index` macros in
`dbt/macros/schema.yml`.

**D5 (optional). Rename the catalogue export** (`int_all_supermarket_products`, `parquet_output_prefix`,
stack parameter `ProcessedPrefix`) to names that describe a catalogue export. Needs a stack parameter change
and a coordinated deploy; not worth doing during the backfill.

**D6 (optional). dbt CI.** `infra/dbt/github_workflow_dbt_ci.yml` is a draft (parse + `tests/test_dbt_infra.py`
on pull requests) that was never installed under `.github/workflows/`.

## 4. Watch list and manual cleanup

- **Sunday 2026-11-08**: first weekly DuckLake maintenance on the production lake, using the workaround that
  deletes expired data files itself (commit `c6dfe44`; DuckDB 2.0.0's S3 delete crashes). Check the four
  slots that day and the alert mail.
- **Dunnes IDs are frozen** at the 10,594 derived from the last raw scrape (`scripts/dunnes_ids_from_raw.py`);
  the sitemap is blocked from AWS and the laptop. New products are not discovered. See
  `docs/dunnes_ids_investigation.md` (a third, small search-endpoint probe was proposed, not run).
- **IAM is parked** (`docs/iam_setup.md`). The Tesco weekly launchd job (`scripts/launchd/`) uses the default AWS
  profile; set `AWS_PROFILE` in the wrapper script if the default changes.
- **Legacy objects.** Done 2026-11-04: the three S3 files `processed/embeddings/*/product_title_embeddings.parquet`
  were deleted (the bucket is versioned; the `processed/` noncurrent-expiry rule keeps them recoverable for
  about 3 days), and the local DB lost the stray FTS schema `fts_main_tesco_table` and the empty `flock_config`
  schema (a verified backup `~/supermarket_backups/supermarket_data_20261104.db` was taken first). Still
  present because `ml/agents.py` queries them until it is migrated: table `int_product_title_embeddings`
  (index `hnsw_idx`) and schema `fts_main_int_supervalu`. Drop them after the migration is verified; the exact
  statements, including loading `vss` first, are in `docs/agent_migration_plan.md` section 10.
- A `notebooks/embeddings.ipynb` working copy is deliberately left uncommitted.
