# ML embedding framework (`ml_common/`)

Implements steps 1-3 of `docs/ml_audit.md`, plus the CLI and the image store (`fetch`/`seed`). Nothing here writes to S3, Hugging Face or the DB unless you point `--store` at them.

## Store layout

One store root (`EMBEDDINGS_STORE` / `--store`): a local directory, `s3://bucket/prefix`, or `hf://buckets/user/bucket/prefix` (a private HF Bucket, via `HfFileSystem`).

```
embeddings/<spec_id>/manifest.json            written once: the spec fields and columns
embeddings/<spec_id>/part-<utc>-<rand>.parquet  append-only, one per chunk of CHUNK_ROWS
images/<sha256[:2]>/<sha256>                  content-addressed ORIGINAL image bytes, no extension
image_map/part-<utc>-<rand>.parquet           append-only product -> image-hash links, one row per fetch attempt
```

Images are stored exactly as the retailer served them (no re-encoding); the key is the sha256 of those bytes, so identical images across products or retailers are stored once. The format is sniffed from the bytes (PIL), which is why there is no extension; the map records `content_type, bytes, width, height`. The older `.webp` convention is gone.

## Image store (`ml_common/images.py`, `image_map.py`, `cli/images.py`)

`image_map` columns: `supermarket, id, image_url, image_sha256, content_type, bytes, width, height, status, error, attempts, fetched_at`. The map is append-only; the newest row per `(supermarket, id, image_url)` is current.

- `status`: `ok`; `transient` (timeout, reset, 429/5xx after in-run retries); `permanent` (4xx such as 404, or a payload that is not a decodable image, under 200 bytes, over 25 MB or under 16 px).
- What `fetch` downloads: catalogue rows whose `(supermarket, id, image_url)` has no map row (so a changed URL is refetched and gets a new hash), plus `transient` rows with `attempts < IMAGE_MAX_ATTEMPTS` (default 5, one attempt per run). `permanent` rows and capped `transient` rows wait for `--retry-failed`. Identical URLs are fetched once per run.
- Politeness: at most `IMAGE_CONCURRENCY` (4) requests per host, `IMAGE_DELAY` (0.25 s) before each, `IMAGE_TIMEOUT` (30 s); retries use `scraper_common.http.request_with_retry` (exponential backoff, honours `Retry-After` digits); descriptive `User-Agent` (`IMAGE_USER_AGENT`). Rows are interleaved across retailers so `--limit N` samples all four and hosts are hit in parallel.
- Resume: a batch of `CHUNK_ROWS` is fetched, its blobs written, then its map part appended. Killing the run loses at most the batch in flight; a rerun re-diffs against the map and skips blobs that already exist.
- Quality gate: the run reuses `scraper_common.quality` (`RunStats`, `gate_failures`), logs one `image_fetch_summary` JSON line (counts, `bytes_total`, `blobs_reused`, per-retailer status counts, duration) and exits 1 when the error rate exceeds `IMAGE_MAX_ERROR_RATE` (0.2) or the 404 rate exceeds `IMAGE_MAX_NOT_FOUND_RATE` (0.1). Zero pending rows is a pass.
- Validation is `PIL.Image.verify()`, which does not decode pixels, so a truncated JPEG can pass; the embedder records such an image as an `error` row.
- `seed` imports the legacy HF dataset shards (`id, supermarket, image_url, image_bytes`) by content hash with `fetched_at` = the shard's last-modified time and `attempts = 0`, so the ~10 GB need not be re-downloaded from retailers. It reads shard by shard in `CHUNK_ROWS` batches (bounded memory), skips rows with null bytes/URL or an invalid image, skips triples already mapped, and is safe to rerun. A seeded row whose URL has since changed is simply refetched by `fetch`.
- Catalogue join: `catalogue.load_catalogue` adds `image_sha256` for image specs from the newest `ok` map row matching the product's *current* `image_url`. A product whose URL changed has a null hash until refetched (so it is skipped, not embedded from a stale image); once fetched, a different hash gives a new `content_key`, so exactly that row re-embeds and `table.latest_ok` supersedes the old vector. If the new URL serves the same bytes, nothing re-embeds.

```bash
uv sync --group ml-embed
uv run python -m ml_common.cli.images seed --store ./embedding_store            # optional, legacy shards from HF
uv run python -m ml_common.cli.images fetch --store ./embedding_store --catalogue cat.parquet --limit 100
uv run python -m ml_common.cli.images fetch --store ./embedding_store --catalogue cat.parquet --supermarkets tesco aldi --retry-failed
```

`--store` goes before the subcommand. The catalogue needs `id, supermarket, image_url` (dbt `int_all_supermarket_products` now carries `image_url`).

`ml/download_images_bytes_hf.py` (images inside HF dataset parquet shards, re-downloads everything every run) is superseded by `fetch` and `seed`; it is kept only until the store is seeded.

S3 stays the source of truth for scraped and dbt data. The store is the working area for derived artefacts.

## Spec, content key, idempotency

- `EmbeddingSpec` hashes `model_id, revision, modality, text_field, image_field, prefix, normalize, dim, dtype, image_tokens` into `spec_id`. Name, batch size, query prefix, device-loading options and the dbt layout flag are operational and do not change the id. A different id is a different table, so models and settings never mix.
- `prefix` is the literal prompt text (resolved from the model's own prompt table when the spec is written), never a prompt name, so an upstream remap cannot silently change vectors. Revisions are pinned commit shas (`make_spec` resolves the current one through `HfApi`; the registry holds literals so importing never touches the network).
- Registry names: image specs come in the default token budget (no suffix, the model's 280) and `-t<N>` variants that set `image_tokens=N`, built in `specs.py` from `IMAGE_TOKEN_VARIANTS = (140,)`: `gemma2-image`, `gemma2-image-text`, `gemma2-image-t140`, `gemma2-image-text-t140`. Each has its own `spec_id` and table. Add another budget by extending that tuple.
- `content_key`: text -> sha256(text); image -> sha256 of the stored image bytes (`image_sha256`); image_text -> sha256(image_sha + NUL + text). A change to either input changes the key.
- Diff: catalogue rows (with a non-empty input) anti-joined, in polars, on `(id, supermarket, content_key)` against `ok` rows of that spec. Re-running embeds 0. A changed title embeds exactly that row; the old row stays in the table, so readers take `table.latest_ok` (newest `ok` per `(id, supermarket)`).
- Failures: a row that cannot be decoded or has no image bytes is written with `status='error'`, a null vector and the reason. Errors do not count as done, so the next run retries them; an old error row beside a newer `ok` row is expected. There is no attempt counter yet.

## Table schemas

Clean schema (new specs): `id, supermarket, content_key, spec_id, model_id, model_revision, modality, embedding (list<float32>), embedded_at, status, error`.

All specs, including `harrier-title`, use this one schema; dbt models select from it per spec. Error rows have a null `embedding`; filter `status = 'ok'` before reading.

## Running

```bash
uv sync --group ml-embed
uv run python -m ml_common.cli.embed --spec gemma2-text --catalogue cat.parquet --store ./embedding_store
```

The catalogue needs `id, supermarket` plus the spec's fields (`title_cleaned`; image specs read `image_url` and get `image_sha256` from the store's image map, so run `fetch` first). `ml_common/cli/embed.py` is argparse only; `pipeline.run_embed` does diff -> embed -> append. Model choice is the registry (`specs.py`); to add one, call `make_spec(name, model_id, modality=..., text_field=..., prefix=...)` once to get the pinned revision and add the literal entry to `REGISTRY`. No other code changes. `SentenceTransformerEmbedder` covers any sentence-transformers model; another library means another class satisfying the `Embedder` protocol.

As an HF Job: Jobs run outside the repo, so `ml_common` (and `scraper_common`, which it imports for logging, retries and the quality gate) must be installed from a wheel or git checkout. Both `cli.images` (fetch/seed) and `cli.embed` take `--store hf://buckets/<user>/<bucket>` and `--catalogue hf://buckets/<user>/<bucket>/catalogue/latest.parquet` and need only an `HF_TOKEN` secret; nothing in the code is job-specific, and in a datacenter the same politeness limits just finish faster because transfer is quicker. Sketch (flavors are suggestions):

```bash
# fetch (and seed): CPU only, ~1 h at 4 req/host, so --timeout 4h
hf jobs run --flavor cpu-upgrade --timeout 4h --secrets HF_TOKEN \
  python:3.14 bash -c "git clone <repo> r && cd r && pip install uv && uv sync --group ml-embed \
  && uv run python -m ml_common.cli.images --store hf://buckets/brianbarry97/<bucket> fetch \
     --catalogue hf://buckets/brianbarry97/<bucket>/catalogue/latest.parquet"
# embed: GPU, one spec per job
... --flavor a10g-large --timeout 3h ... python -m ml_common.cli.embed --spec gemma2-image-text-t140 --store ... --catalogue ...
```

A PEP 723 header cannot import the package by itself, so none is provided.

## Not done yet (deliberately)

- HF bucket creation, uploading anything to HF, the scheduled job.
- Pushing the dbt catalogue to HF after each build (dbt now emits `image_url`; `image_sha256` is joined from the image map at embed time, not in dbt).
- Running `fetch` against the full catalogue and `seed` against all legacy shards (only small test runs so far: 200 seed rows, 200 fetched images).
- Hardening: blob writes are not atomic on a local disk (a kill mid-write could leave a truncated blob that `exists` then trusts); map compaction.
- Porting `ml/embeddings.py` (writes the file dbt reads) and `ml/agents.py` (search, FTS, query encoder) onto the package (audit steps 4-7).
- An attempt counter for repeatedly failing items, and compaction of old/stale rows.
- Quality evaluation: the probe results in the report are an eyeball check only; build an in-domain eval before choosing a model.

## embeddinggemma-2 on a 16 GB M-series Mac (probe, 200 Tesco packshots, 2026-10-23)

Versions: sentence-transformers 6.2.0, transformers 5.20.2 (card needs >=6.1 / >=5.18), torch 2.12.0. No `trust_remote_code`. Pin `google/embeddinggemma-2@914f7f89`.

- Load 2-5 s warm (3 GB download once). Text-only model: ~1.2 GB MPS; with the vision tower ~2.4 GB idle, 6.7-8.5 GB peak while encoding images.
- Single text query (bf16, MPS): p50 24 ms, p95 40 ms. 200 titles in 1.8 s (~110/s).
- Images are ~2365 px squares; the vision tower runs at the default 280-token budget: ~0.6-0.7 img/s on MPS and on CPU alike (bf16 or fp32), so 50k images is ~20 h on the Mac. Budget 140 -> ~1.9 img/s, 70 -> ~4 img/s (`image_tokens` spec field; mean cos vs 280 about 0.92-0.94 image-only, 0.98 image+text). Use a GPU Job for the full catalogue.
- Image batch 32 aborts on MPS (tries to allocate a 9.7 GB buffer); the image specs use `batch_size=4`.
- bf16 on MPS is not free: text vectors differ from fp32 by cos 0.97 mean (min 0.92), norms land at 0.998-1.004, and truncation checks show the same noise. fp32 text is also faster on MPS (0.33 s vs 0.56 s per 50). Never fp16 (NaN, per the card; non-finite outputs are recorded as errors).
- `prompt_name` is not used: the literal prefix goes through `prompt=` for text, and is prepended into the `text` of the interleaved dict (`"title: none | text: {title} <|image|>"`). Only text takes a prefix; image-only inputs get none.
- Selective loading works through `config_kwargs={"audio_config": None}` (and `vision_config` for text).
- Matryoshka: `truncate_dim` + `normalize_embeddings` re-normalises as the card says.

## HF architecture (planned; nothing is created yet)

One private HF bucket `brianbarry97/<name>` holds the whole working set:

```
catalogue/latest.parquet            current products, pushed from dbt after each build
catalogue/date=YYYY-MM-DD.parquet   dated snapshots of the same file
images/<aa>/<sha256>                original image bytes, content-addressed
image_map/part-*.parquet            (supermarket, id, image_url) -> image_sha256, status, attempts
embeddings/<spec_id>/part-*.parquet one table per spec, keyed by content_key
```

- The catalogue is the list of products (with `image_url`, `title_cleaned`); it is overwritten with the latest dbt output so jobs always diff against what exists now, and the dated copies allow replay.
- `image_map` links products to image hashes. It is separate from `images/` because many products share one blob and because a product's URL can change; it records failures and attempts so retries are bounded.
- Each embedding spec gets its own table so models and settings never mix. Rows carry `(id, supermarket, content_key)`: join to the catalogue on `(supermarket, id)` for the product, and to the image map via `image_sha256 = content_key` (image specs) for the picture. A changed title or image gives a new `content_key`, so only that row is recomputed; readers take `latest_ok`.
- Buckets are mutable and Xet-deduplicated with free egress (`docs/ml_hf_research.md`); S3 stays the system of record for scraped and dbt data. Fetch, seed and embed are the same CLIs locally and in Jobs.
