# Review checklist for scraper / infra changes

Apply to every PR touching `aldi/ dunnes/ supervalu/ tesco/ scraper_common/ lambda/ Dockerfile.* .github/ infra/`.

1. **DRY** - S3 I/O, retries, chunking, ID read/write, logging and the quality gate live in
   `scraper_common/`. New scripts import them; no copy-pasted checkpoint/upload/header code.
2. **Comments** - only the non-obvious "why". No narration of what the code does, no stale TODOs.
3. **Defensive code** - no `except Exception: pass/continue`, no bare swallow-and-log; catch the
   narrowest exception, and only where there is a defined recovery. No None-checks on values the
   code itself just produced. Let unexpected errors crash so Batch marks the job FAILED.
4. **Data-quality gate** - every scrape path goes through `run_scrape` (or reproduces its
   zero-rows / error / empty-payload gate, non-zero exit, `_run_summary_chunk{N}.json`). ID jobs go
   through `run_ids_job` (never overwrite `ids/latest` with empty or sharply shrunken results).
5. **Schema compatibility** - raw record keys/types and S3 paths unchanged, or the dbt owner is told
   (`docs/scraper_data_quality.md`). Non-data files must not end in `.jsonl.gz`.
6. **Idempotency / resume** - re-running a chunk is safe; dates are UTC; `CHUNK_ID`/`TOTAL_CHUNKS`
   validated; slices cover all IDs exactly once.
7. **Secrets** - no keys, tokens or cookies in the repo, logs or Docker layers (note: any
   committed site API key should be treated as public and rotated if it grants anything private).
8. **Infra** - Dockerfiles copy every new module; `uv sync --frozen` still works; Lambda/Batch
   changes keep the job-definition names; infra changes are drafted under `infra/`, not applied.
9. **Verification** - `uv run ruff check .`, `uv run ruff format --check`, `uv run pytest`, and a local
   smoke run (`OUTPUT_DIR=... MAX_IDS=20`) for scraper changes. New shared logic gets a unit test.
