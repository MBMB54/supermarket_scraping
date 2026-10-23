# ML embedding framework (`ml_common/`)

Implements steps 1-3 of `docs/ml_audit.md`, plus the CLI. Nothing here writes to S3, Hugging Face or the DB unless you point `--store` at them.

## Store layout

One store root (`EMBEDDINGS_STORE` / `--store`): a local directory, `s3://bucket/prefix`, or `hf://buckets/user/bucket/prefix` (a private HF Bucket, via `HfFileSystem`).

```
embeddings/<spec_id>/manifest.json            written once: the spec fields and columns
embeddings/<spec_id>/part-<utc>-<rand>.parquet  append-only, one per chunk of CHUNK_ROWS
images/<sha256[:2]>/<sha256>.webp             content-addressed image bytes
```

S3 stays the source of truth for scraped and dbt data. The store is the working area for derived artefacts.

## Spec, content key, idempotency

- `EmbeddingSpec` hashes `model_id, revision, modality, text_field, image_field, prefix, normalize, dim, dtype, image_tokens` into `spec_id`. Name, batch size, query prefix, device-loading options and the dbt layout flag are operational and do not change the id. A different id is a different table, so models and settings never mix.
- `prefix` is the literal prompt text (resolved from the model's own prompt table when the spec is written), never a prompt name, so an upstream remap cannot silently change vectors. Revisions are pinned commit shas (`make_spec` resolves the current one through `HfApi`; the registry holds literals so importing never touches the network).
- `content_key`: text -> sha256(text); image -> sha256 of the stored image bytes (`image_sha256`); image_text -> sha256(image_sha + NUL + text). A change to either input changes the key.
- Diff: catalogue rows (with a non-empty input) anti-joined, in polars, on `(id, supermarket, content_key)` against `ok` rows of that spec. Re-running embeds 0. A changed title embeds exactly that row; the old row stays in the table, so readers take `table.latest_ok` (newest `ok` per `(id, supermarket)`).
- Failures: a row that cannot be decoded or has no image bytes is written with `status='error'`, a null vector and the reason. Errors do not count as done, so the next run retries them; an old error row beside a newer `ok` row is expected. There is no attempt counter yet.

## Table schemas

Clean schema (new specs): `id, supermarket, content_key, spec_id, model_id, model_revision, modality, embedding (list<float32>), embedded_at, status, error`.

`harrier-title` (`dbt_compatible`): `id, supermarket, title, title_cleaned, title_embedding, embedding_model, embedded_at, content_key, spec_id, model_revision, status, error`, i.e. a superset of what `int_product_title_embeddings` reads. Error rows have a null `title_embedding`; filter `status = 'ok'` before pointing dbt at it.

## Running

```bash
uv sync --group ml-embed
uv run python -m ml_common.cli.embed --spec gemma2-text --catalogue cat.parquet --store ./embedding_store
```

The catalogue needs `id, supermarket` plus the spec's fields (`title_cleaned`, and `image_sha256` for image specs). `ml_common/cli/embed.py` is argparse only; `pipeline.run_embed` does diff -> embed -> append. Model choice is the registry (`specs.py`); to add one, call `make_spec(name, model_id, modality=..., text_field=..., prefix=...)` once to get the pinned revision and add the literal entry to `REGISTRY`. No other code changes. `SentenceTransformerEmbedder` covers any sentence-transformers model; another library means another class satisfying the `Embedder` protocol.

As an HF Job: Jobs run outside the repo, so `ml_common` (and `scraper_common`, which it imports for `setup_logging`/`BUCKET`) must be installed from a wheel or git checkout, then `python -m ml_common.cli.embed ... --store hf://buckets/<user>/<bucket>` (mount or `HF_TOKEN` secret). A PEP 723 header cannot import the package by itself, so none is provided.

## Not done yet (deliberately)

- HF bucket creation, uploading anything to HF, the scheduled job.
- Pushing the dbt catalogue to HF after each build; `image_url`/`image_sha256` in `int_all_supermarket_products`; the image downloader that fills `images/`.
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
