# Multimodal embedding framework on Hugging Face: research (2026-10-23)

Read-only research. Nothing was created, uploaded or spent. Items I could not verify are in section 4.

## 1. Recommendation

**Storage.** Use one private **HF bucket** (`brianbarry97/ie-supermarket-embeddings`) as the working store for derived artefacts, and keep S3 as the system of record for scraped and dbt data.
- Images: one content-addressed file per image, `images/<sha256[:2]>/<sha256>.webp`. Re-runs skip files already present, and a dead URL never forces a re-download of 10 GB.
- Embeddings: one Parquet file per (model, task spec) with columns `id, content_hash, vector`. Dimensions are cheap to truncate when the model supports Matryoshka.
- Keep the existing dataset repo `irish_supermarket_data` as a frozen, versioned snapshot only.
- Buckets are mutable and unversioned with Xet chunk dedup, so a re-uploaded but unchanged file costs nothing. They are billed per TB, and egress is free.

**Automation.** Use a **scheduled HF Job** (`hf jobs scheduled uv run "0 5 * * *" ...`), not a webhook.
- The job reads a product manifest, finds ids or content hashes with no vector for each registered spec, embeds only those, and writes the result back.
- Webhooks are capped at 1,000 triggers per 24h, and a triggered run gets neither the job's volumes nor its secrets, so the script must be fetched by URL.
- The same script runs on the laptop with `uv run`. A 200-1000 item daily delta is cheap either way.

**Models.**
- Primary multimodal: **Qwen/Qwen3-VL-Embedding-2B** (Apache-2.0, already in use, 2048 dims, Matryoshka 64-2048, instruction-aware, sentence-transformers).
- Cheap text-only title matching: keep **microsoft/harrier-oss-v1-270m** (MIT, 268M).
- Challenger to benchmark: **jinaai/jina-embeddings-v5-omni-small** (1.7B, 1024 dims, Matryoshka). It is non-commercial (CC-BY-NC-4.0); see decision 1.
- Do not hard-code any of these. Make the framework spec-driven (section 2.4).

**Vector search.** No separate vector DB. Brute-force search in DuckDB is fast enough at this scale (measured below).

**Decisions you must make**
1. **Is the app commercial?** If yes, jina v5 is out, because every jina v5 and jina-clip-v2 repo is CC-BY-NC. If it is non-commercial or personal, jina omni becomes the primary candidate (reason below).
2. **Which store holds the working vectors?** HF bucket, or stay in S3 and let HF Jobs read S3 with a scoped key. I recommend the bucket for images and vectors, and S3 only for the dbt output.
3. **Upgrade DuckDB to 2.0.0?** It is on PyPI, and it is what gives you native `hf://buckets/` reads and the `huggingface` extension. It is a storage-format bump (see 2.2), so try it on a copy of `supermarket_data.db` and check dbt Fusion first. Skipping it is fine, because the S3-gateway and Python paths work on 1.5.3.
4. **Build a small in-domain eval** (about 100 queries such as "red packaging", "blue tin", with judged hits) before committing. Public scores (MMEB-V2, MIEB) are dominated by documents and VQA, not packshots.
5. **Query-time encoder.** Run the 2B model on the Mac (text-only is acceptable), or use the cheap-query option for the jina family (below).

## 2. Findings

### 2.1 HF Buckets (docs: huggingface.co/docs/hub/storage-buckets, storage-buckets-access, storage-buckets-s3)
- **Semantics.** The docs call them "S3-like object storage on Xet, non-versioned and mutable". Overwrites and deletes are immediate and permanent, and there are no PRs or dataset viewer. Chunk-level dedup applies, and `hf buckets cp` can copy Xet files server-side from a repo or bucket in the same region.
- **My bucket.** `hf buckets info brianbarry97/jobs-artifacts` returned private, 26 files, 272 KB. Jobs auto-create and use it for `-v ./localdir:/data` volumes.
- **CLI.** `hf buckets create/cp/sync/ls/rm/info/move`, plus `hf sync`. `sync` supports `--delete`, `--dry-run`, `--include/--exclude` and `--plan/--apply`.
- **Region.** `hf buckets create --region [us|eu]` says "Requires Team plan or above". Unless you are on Team, you get the default region. The `hf auth whoami` output does not show your plan, so I could not tell.
- **Reads and writes.**
  - Any HF tool can use `hf://buckets/user/bucket/path`: `HfFileSystem` (fsspec), pandas, polars, `datasets`.
  - `hf-mount` (`brew install hf-mount`, NFS/FUSE) mounts a bucket lazily, so only the bytes read hit the network.
  - Jobs and Spaces take `-v hf://buckets/user/b:/mnt[:ro]`.
  - An **S3-compatible gateway** at `https://s3.hf.co/<namespace>` takes HF-derived keys (`HFAK...`), `URL_STYLE path`, and region `us-east-1`. DuckDB 1.5.3 can read and write through an S3 secret, and boto3 and rclone work. Limits: no versioning, ACLs or object metadata, and no cross-namespace copy.
  - Webhook or live-follow events for bucket changes: yes (`repo.content` with an `updatedFiles` list). The 1,000 triggers per 24h cap is the catch.
- **DuckDB and buckets.** Per the docs, DuckDB **2.x** `httpfs` reads `hf://buckets/` natively and read-only (`CREATE SECRET hf (TYPE huggingface, PROVIDER credential_chain)` for private). **1.x does not**.
  - I confirmed this on 1.5.3: "DuckDB only supports querying datasets or spaces".
  - On 2.0.0, an unauthenticated read of a private bucket path returns HTTP 401. That confirms the path is recognised, but I did not complete an authenticated read.
- **Pricing.** hf.co/storage lists $18/TB/month private ($12 public), with egress and CDN included. Storage limits page: private allowance is 100 GB (free) or 1 TB (PRO), and the same limits apply to buckets.
  - Your repo currently uses 10.36 GB (`used_storage` on the dataset repo), so storage is about $0.19/month at pay-as-you-go rates, and probably $0 inside the allowance.
- **Dataset repo vs bucket vs S3.**
  - Dataset repo: git-backed, versioned, has a Parquet viewer. Recommended under 100k files, under 10k per folder, and about 100 files per commit. History accumulates, so deleting a file does not free space unless you squash.
  - 100k individual images fit a repo only in sharded folders and with slow commits. They fit a bucket well.
  - Images inside Parquet (your current layout) are good for training and streaming. They are bad for incremental updates, because adding one image rewrites a shard. Parquet content-defined chunking (`optimize-parquet.py` in `uv-scripts`) reduces that cost but does not remove it.
  - S3: best if dbt and scrapers stay there. Jobs need credentials, and egress to HF compute costs on the AWS side.

### 2.2 DuckDB, the `huggingface` extension, and vector search
- **What it is.** A *community* extension by dentiny: `INSTALL huggingface FROM community; LOAD huggingface;`. Functions: `hf_files`, `hf_scan` (projection and filter pushdown), `hf_profile`, `hf_dataset_estimate`, plus `cache_httpfs_*` cache controls. It uses the DuckDB secret manager for auth. Lance datasets are not supported.
- **It is read-only exploration of dataset repos, not bucket I/O, not vector search, and not a way to write.**
- **Version contradiction (verified).** The extension page claims DuckDB 1.4.4 to 2.0.0. In practice the `osx_arm64` binary returns HTTP 404 for v1.4.4, v1.5.0, v1.5.1, v1.5.2 and v1.5.3, and exists only for v2.0.0.
- **Test.** On 1.5.3, `INSTALL huggingface FROM community` fails with a 404. On 2.0.0 (via `uv run --with duckdb==2.0.0`), `hf_files('stanfordnlp/imdb')` returned the Parquet shards, `hf_scan` counted 100,000 rows, and `hf_profile` reported about 83 MB. Note that the argument has no `datasets/` prefix.
- **Long-standing path.** `read_parquet('hf://datasets/stanfordnlp/imdb/...')` through httpfs works on both 1.5.3 and 2.0.0 (25,000 rows returned), and uses range requests for Parquet.
- **Vector search, measured.** 100,000 x 1024 float32, `array_cosine_distance` + `ORDER BY ... LIMIT 10`: 0.09-0.10 s per query on both versions (M-series Mac). The same query in numpy, brute force on a normalised matrix: 6-11 ms.
  - At 50-100k items, no ANN index is required. If scale grows 10x, use `vss` (HNSW). Its persistence is still marked experimental, which I did not re-check (see section 4).
- **2.0 upgrade risk.** Unverified: I did not test whether a 2.0-written `supermarket_data.db` opens in 1.5.3, or what dbt Fusion's DuckDB version does. Try it on a copy.

### 2.3 HF Jobs
- **Scheduling.** `hf jobs scheduled uv run "<cron>" script.py` (or `@daily`, `@hourly`), with `ps/inspect/suspend/resume/reschedule/trigger/delete`. `--attempts n` retries a failed run. Scheduled jobs can use the same flags as `hf jobs uv run`: `--flavor`, `-e`, `-s`, `-v`, `--timeout`. I did not find a concurrency flag in the docs.
- **Webhooks.** `hf webhooks create --watch bucket:user/name --job-id <id> --secrets HF_TOKEN`. It fires on bucket file changes and on dataset commits, at most 1,000 times per 24h. The run gets the webhook's secret, not the job's. Write outputs to a *different* bucket, or the output triggers it again.
- **Volumes.** `-v hf://buckets/user/b:/data` is read-write by default, with `:ro` available. Dataset and model repos mount read-only. Files are fetched lazily.
- **Timeouts and logs.** The default timeout is 30 minutes. Set `--timeout 2h` or similar. Logs: `hf jobs logs <id>`. A job's local files vanish at exit, so write to a bucket volume.
- **Billing.** Per minute while Starting or Running. Available if the account has a positive credit balance (PRO credits count).
- **Prices from `hf jobs hardware`:** cpu-basic $0.01/h, cpu-upgrade $0.03, t4-small $0.40, t4-medium $0.60, l4x1 $0.80, a10g-small $1.00, a10g-large $1.50, l40sx1 $1.80, a100-large $2.50, h200 $5.00.
- **Existing runs.** `hf jobs ps -a` shows `download_images_bytes_hf.py` (2m to 13m, several ERRORs) and `generate_llm_responses.py`. There is no completed run of `image_embeddings_hf.py` in the list, so I have no measured embedding throughput.
- **S3 credentials in a Job.** Cleanest options, best first:
  1. Avoid S3 entirely. A local step, or a small dbt post-hook, `hf sync`s the manifest to the bucket. The Job needs only `HF_TOKEN`.
  2. A dedicated IAM user with read-only access to `processed/*`, passed as `-s AWS_ACCESS_KEY_ID -s AWS_SECRET_ACCESS_KEY` (Jobs secrets are encrypted server-side). `docs/iam_setup.md` notes there are currently no IAM users and the root key is in use, so do not pass the root key.
  3. Use an HF fine-grained token with the bucket scope only, since the S3 gateway derives its keys from it.

### 2.4 Models (Oct 2026). Verified via `hf models info`, model cards and the jina paper (arXiv 2605.08384)

| Model | Params | Dims | Modalities | MRL | Licence | Notes |
|---|---|---|---|---|---|---|
| Qwen3-VL-Embedding-2B | 2.1B | 2048 | text, image, video | yes, 64-2048 | Apache-2.0 | card reports MMEB-V2 results (8B scores higher; I did not transcribe an overall figure); instruction-aware |
| Qwen3-VL-Embedding-8B | 8B | 4096 | same | yes | Apache-2.0 | 8B is heavy on a Mac |
| jina-embeddings-v5-omni-small | 1.63B | 1024 | text, image, video, audio | 32-1024 | **CC-BY-NC-4.0** | Text tower is the same as jina-v5-text-small (596M). Paper claims the open-weight frontier on MIEB-Lite/MMEB-V/MAEB |
| jina-embeddings-v5-omni-nano | 0.99B | ? | same | yes | **CC-BY-NC-4.0** | Smaller alternative |
| SigLIP2 so400m (patch14-384) | 1.1B | 1152 | text, image only | no | Apache-2.0 | Classic CLIP-style dual encoder with small text tower (~ 400M), so query encoding is cheap. Short text only |
| jina-clip-v2 | 0.87B | 1024 | text, image | yes | **CC-BY-NC-4.0** | Older |
| nomic-embed-multimodal-3b | 3B | ? | text+image | ? | tag empty, **unverified** | Qwen2.5-VL-3B base |
| ColQwen2.5 v0.2 / ColQwen3 4B | 3-4B | multi-vector | text, image | no | MIT / Apache-2.0 | Late interaction stores ~hundreds of 128-d vectors per image, so the index is ~100x larger. Built for document pages, overkill for packshots |
| harrier-oss-v1-270m | 268M | 640? | text | ? | MIT | Incumbent for titles. 0.6B and 27B siblings exist |

- **Primary: Qwen3-VL-Embedding-2B.** It is permissive, already wired in `ml/image_embeddings_hf.py` (batch 128, `a100-large` in its docstring), and text, image and mixed inputs share a space.
- **Query-time cost.** Encoding a short text query through a 2B decoder works on MPS or CPU but is the heaviest part of the app. I did not measure it, because the model is not cached locally (see section 4).
- **Cheaper query encoders.**
  - SigLIP2 has a separate small text tower.
  - jina omni's selective loading (`modality="text"`) leaves just the text tower (~600M), and its text space is *identical* to jina-v5-text-small, which makes cross-modal queries cheap.
  - For Qwen, truncating to 512 dims cuts index size and compute.
- **Honest caveat.** None of these numbers is about grocery packshots. "Red packaging" depends on colour and layout, which general models may embed weakly. Run the eval in decision 4 before locking in.
- **Making the framework model-agnostic.** A registry of **specs**: `{name, hf_id, modality, dim, truncate_to, query_prefix, doc_prefix, batch_size, normalize}`. A table `embeddings/<spec>/part-*.parquet` holds `(id, content_hash, vector)`. The job computes the set difference between manifest and existing keys per spec. Adding a model means adding one spec entry. Query-time code loads the spec and calls `encode_query`.

### 2.5 Other HF features
- **Xet** gives chunk dedup for repos and buckets. **Parquet CDC** reduces re-upload cost when only part of a file changes.
- **Inference Endpoints** (dedicated) bill per minute, scale to zero after 15 idle minutes. Examples: T4 $0.5/h, L4 $0.8/h, A10G $1.0/h, L40S $1.8/h. A scale-to-zero embedding endpoint is plausible for query-time encoding if the Mac is too slow. The cold start is long, so I would not use it for an interactive app unless always-on.
- **Spaces** can mount buckets read-only for a search demo.
- **HF MCP server** was not authorised in this session, so I could not use it.

## 3. Rough monthly cost (estimates, not measured)

| Item | Assumption | Cost |
|---|---|---|
| Storage: 10 GB images + about 5 GB vectors (50k x 2048 fp32 = 410 MB per spec, fp16 half) | 15 GB private | $0 inside 100 GB; $0.27 at $18/TB |
| One-off full embed, 50k images + 50k titles, Qwen3-VL-2B, a10g-large | guess 10-20 img/s, so 45-85 min | ~$1.1-2.1 (a100-large ~$2-3, faster). Use `--timeout 3h` |
| Daily incremental, 200-1000 new items | a10g-small, ~10 min including model load | ~$0.17/day, ~$5/month. $0 on the Mac |
| Titles only, harrier-270m | cpu-upgrade, 5 min | < $0.01 |
| Query-time endpoint (optional) | L4 always on | ~$580/month. Scale-to-zero: pennies idle, cold start |
| Total, recommended setup | | roughly $5-8/month once the initial embed is done |

## 4. Unverified or uncertain
- **Your plan.** `hf auth whoami` does not show it. I could not tell whether you have PRO, and the region and quota statements depend on it.
- **Embedding throughput and exact cost.** No measured run of `image_embeddings_hf.py` exists in `hf jobs ps`. All GPU-hour figures are estimates.
- **Mac query latency** not measured, because I did not download the model.
- **Benchmark numbers.** The jina paper and Qwen card scores were not re-run. MMEB-V2 and MIEB do not represent grocery packshots. The `hf datasets leaderboard` search for MMEB/MIEB was not run.
- **Authenticated reads.** I did not complete an authenticated `hf://buckets/` read from DuckDB 2.0.0 or an S3-gateway test.
- **DuckDB vss HNSW persistence.** The docs page returned only a redirect, so I am relying on prior knowledge (experimental flag).
- **Licences.** nomic-embed-multimodal-3b has no licence tag. Check its card.
- **Scheduled-job concurrency / overlap** behaviour is not documented anywhere I found.
- **DuckDB 2.0 file compatibility** with 1.5.3 and dbt Fusion: untested.
