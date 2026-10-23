# ml/ audit

Read-only review; style baseline is `scraper_common/` and `docs/REVIEW.md`.

## What exists today

Catalogue flow: dbt `int_all_supermarket_products` writes `processed/all_supermarket_products.parquet` with only `id, supermarket, title, title_cleaned` (dbt/models/intermediate/int_all_supermarket_products.sql:6). `ml/embeddings.py` embeds `title_cleaned` and writes `processed/embeddings/{model-slug}/product_title_embeddings.parquet`. dbt `int_product_title_embeddings` reads that file back. There are two separate, unconnected image steps: `export_data_to_hf.py` reads `image_url` from the local `supermarket_data.db` (not from S3, because the processed parquet has no `image_url`), and uploads to a private HF dataset. `download_images_bytes_hf.py` and `image_embeddings_hf.py` then run as HF Jobs.

## Findings, most severe first

1. **Changed titles never re-embed.** `embeddings.py:77-79` anti-joins on `(id, supermarket)` only. If a title or `title_cleaned` changes (or the `clean_title` macro is improved), the stale vector stays forever. Nothing records which text or prompt produced a vector. The fix is a content key (hash of the exact embedded text) plus a spec id.
2. **No model revision pin, and model identity is thin.** `embeddings.py:21,86` uses a floating model id with `trust_remote_code=True`, so a new upstream commit silently changes vectors. Old and new vectors are mixed in one file with the same `embedding_model` string. The prompt (:23) and normalisation are not part of the path or any column. `image_embeddings_hf.py:33` and `agents.py:26` also hardcode model ids.
3. **Query and document embeddings are asymmetric.** Documents are embedded with the long "Instruct: ... Query:" prompt (`embeddings.py:88-94`), but `agents.py:26,265` encodes queries with no prompt. Either this is deliberate (then say so) or it hurts retrieval. The two files can drift because the model id and prompt are copied in each.
4. **Stale FTS index in agents.py.** `hybrid_search` calls `fts_main_int_supervalu.match_bm25` (agents.py:287). The index is only created in notebooks (agents.ipynb cell with `create_fts_index`, duckdb.ipynb). dbt rebuilds `int_supervalu` as a table, which drops the index schema. Nothing recreates it, so after any dbt run the agent breaks or searches outdated data. The agent opens the DB `read_only=True` (agents.py:44), so it cannot rebuild it either. Needs a post-build step (dbt `post-hook` or a `ml_common` refresh command).
5. **Unsafe SQL and hardcoded paths in agents.py.** `search_term` is interpolated into SQL with an f-string (:287), so an apostrophe breaks the query or injects SQL. Use parameters. The DB path is absolute `/Users/brianbarry/...` (:44). `FLOAT[640]` is hardcoded in agents.py:270 and in the dbt model, which blocks changing model. Model load, DB connection, the S3 secret and extension installs all run at import time (:26-48), so importing the module has side effects.
6. **Image download redoes everything and swallows errors.** `download_images_bytes_hf.py:85-116` re-downloads all products every run into ~10 GB of byte shards, with no skip of already-fetched images, no URL or content hash, and no record of failures. `except Exception` (:67) only prints and keeps going; failed images are stored as null and are indistinguishable from "no image" on the next run. Shards are uploaded in one `upload_folder` at the end, so a crash after hours loses everything.
7. **Image embedding is all-in-memory and full-rebuild.** `image_embeddings_hf.py:44-85` loads every shard, decodes every image, re-embeds everything and writes one file; nothing is incremental. Images that fail to open crash the whole run (:65, no handling) while null bytes are skipped without a trace. It also has no `__main__` guard and runs at import, as does the downloader. `HF_TOKEN` is read via `os.environ[...]` with a duplicate `storage_options` dict in each script.
8. **embeddings.py error handling.** `read_parquet_from_s3` (:45-51) decides "file missing" by substring matching on error text including "404", which can hide real failures. The 'catalogue missing' case logs an error and returns exit code 0 (:61-65), so a scheduler thinks it succeeded. dbt cares about `title_embedding::FLOAT[640]`, so a model with another dimension breaks dbt silently at build time.
9. **Duplicated config/IO/auth/logging.** Bucket, region, S3 credentials dict, HF dataset id, token handling, batch sizes and `print`/`tqdm`/`logging` are re-implemented in `embeddings.py`, `export_data_to_hf.py`, `download_images_bytes_hf.py`, `image_embeddings_hf.py`, `agents.py` and both classifier scripts. `scraper_common` already has `BUCKET`, `REGION`, `setup_logging` and `Storage`, which `ml/` ignores. The DB path and `CREATE SECRET` also appear in export_data_to_hf.py:23-26, agents.py:44-45, classify_orphan_products.py:45.

## Dead or unused code

- `ml/generate_llm_responses.py` (657 lines): vendored copy of the public HF `uv-scripts/vllm` template (its own comment, :185, still says `generate-responses.py`). Added once in commit 8d62d12 and never touched; no file in the repo imports or documents it (grep clean). Delete it; the template can be fetched from HF when needed.
- `ml/orphan_classifications.jsonl` and `review_console.html` are data/UI for the active taxonomy project: leave them.
- Notebooks hold library logic: `hybrid_search` (duplicated in agents.ipynb and agents.py), FTS index creation and the DuckDB bootstrap. Move these into the package; the rest is exploration.

## Lint, types and dependencies

- `uv run ruff check ml --statistics`: 2 errors, both in the dead file (`F401` unused `Optional`, auto-fixable; `B007` unused loop var). `ruff format --check`: only `download_images_bytes_hf.py` would change. So `ml/` is nearly clean, and CI's `--extend-exclude ml` (.github/workflows/ci.yml:18) can be removed after deleting `generate_llm_responses.py` and running `ruff format`.
- `classify_orphan_products.py` has `except Exception: # noqa: BLE001` handlers (:121, 288, 342), a dead `from datetime import date  # noqa: F401`, and print-based logging. `apply_review_decisions.py` is clean. Light cleanup only.
- Dependency groups do not match what scripts import. `uv run --group ml` is not enough for: `agents.py` (needs `--group agents`, plus `duckdb`, which is only in `dbt`/`notebook`, and `sentence-transformers` from `ml`), `export_data_to_hf.py` (needs `duckdb` from `dbt`, docstring says `--group dbt --group ml`), and the two classifier scripts (duckdb, pydantic-ai, logfire). `embeddings.py` and the HF job scripts rely on PEP 723 headers whose pins differ from `pyproject.toml` (sentence-transformers `>=5.2.2` vs `>=6.0.0`). `ml` group bundles unrelated heavy packages (spacy, mlflow, mlx-lm, gliner2) that a scheduled embedding job would install for nothing.
- No tests cover ml/.

## What dbt expects (must not change until the last step)

- Path: `{embeddings_input_prefix}/embeddings/{embedding_model}/product_title_embeddings.parquet`; defaults `s3://ie-supermarket-data/processed` and `microsoft-harrier-oss-v1-270m` (dbt/dbt_project.yml:27).
- Columns read: `id` (string), `supermarket` (string), `title` (string), `title_embedding` (list of float, cast to `FLOAT[640]`), `embedding_model` (string), `embedded_at` (timestamp).
- Extra columns in the file (currently `title_cleaned`) are ignored. The input catalogue path `processed/all_supermarket_products.parquet` with `id, supermarket, title, title_cleaned` is also a contract (`int_all_supermarket_products`).
- `agents.py` reads `int_product_title_embeddings` (`id, supermarket, title_embedding`), `int_supervalu` (`id, title, category_1..4`, more in other tools) and `fts_main_int_supervalu`.

## Target layout

A small package `ml_common/` in the style of `scraper_common/` (plain modules, env-driven frozen config, `setup_logging`, no import-time side effects):

```
ml_common/
  config.py       env config (bucket, region, HF repo ids, DB path via env, tokens); reuses scraper_common.config.setup_logging/BUCKET
  storage.py      one interface: read_parquet/write_parquet/exists for s3://, hf://, local dir (extends scraper_common.Storage idea; OUTPUT_DIR-style local mode for tests)
  catalogue.py    load the processed catalogue (id, supermarket, title, title_cleaned, image_url) from S3 parquet
  keys.py         content_key(text|bytes) -> sha256 hex; spec_id(spec)
  specs.py        EmbeddingSpec(model_id, revision, modality, input_field, prompt, normalize, dim) frozen dataclass; spec_id = hash of all fields; registry of the two current specs
  diff.py         incremental diff: rows whose (id, supermarket, content_key, spec_id) are missing from the existing store
  embedders.py    Embedder protocol: encode(list[inputs]) -> ndarray; SentenceTransformerEmbedder (text and image); lazy model load
  search.py       hybrid_search + ensure_fts_index(con) moved out of agents.py/notebooks, parameterised SQL
  cli/
    embed_titles.py, export_images.py, download_images.py, embed_images.py    thin argparse entry points
tests/test_ml_*.py   keys, specs, diff, storage round trip (local), no network
```

Output files gain `content_key`, `spec_id`, `model_revision` columns beside the dbt-required ones, so dbt keeps working unchanged. Image work is keyed by `(id, supermarket, image_url hash)`, stores one small shard per run (append only, never rewritten), records failures in a status column with retry counts, and embeds in streamed batches, writing per-run output files.

## Script fate

- Thin wrappers over the package: `embeddings.py`, `export_data_to_hf.py`, `download_images_bytes_hf.py`, `image_embeddings_hf.py` (keep HF Jobs PEP 723 headers, add `ml_common` via the repo, or publish as a wheel since Jobs run outside the repo).
- `agents.py`: keep file, replace the DB path, encoder, FTS and search with `ml_common` imports. Not before the user commits their pending edits.
- Delete: `generate_llm_responses.py`.
- Untouched: `classify_orphan_products.py`, `apply_review_decisions.py`, `review_console.html`, `orphan_classifications.jsonl`.
- Notebooks: leave.

## Migration order (each step leaves dbt and agents.py working)

1. Delete `generate_llm_responses.py`; `ruff format ml`; remove `--extend-exclude ml` from CI. No behaviour change.
2. Fix dependency groups: add `duckdb` to `agents`, make a slim `ml-embed` group (sentence-transformers, polars, huggingface-hub, pillow); keep `ml` as is.
3. Add `ml_common` with config, storage, keys, specs and diff plus unit tests, without changing any script.
4. Port `embeddings.py` to it, writing to the same path and the same dbt columns. Add `content_key`, `spec_id`, `model_revision` as extra columns; first run re-embeds rows with missing keys (one-off backfill: compute keys for existing rows without re-embedding if the title matches the text embedded; otherwise re-embed the changed ones only). Pin the revision. Return a non-zero exit when the catalogue is missing. Verify with dbt `int_product_title_embeddings` into a local `embeddings_input_prefix`.
5. Add `image_url` to `int_all_supermarket_products` (dbt owner decision; additive, so embeddings.py keeps working) and make `export_data_to_hf.py` read S3, not the local DB.
6. Rework the image downloader (skip known hashes, append shards, failure status, upload per batch) then the image embedder (streamed, incremental, per-run files).
7. Move `hybrid_search`, FTS creation and the encoder into `ml_common.search`; make the agent call `ensure_fts_index` (needs a writable connection or a post-hook after dbt) and parameterise the SQL. Settle the query-prompt question with a quick retrieval check first.
8. Optional: pass dimension via a dbt var when a second model appears.
