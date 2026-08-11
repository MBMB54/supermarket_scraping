# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "polars",
#     "huggingface-hub",
#     "aiohttp",
#     "tqdm",
# ]
# ///
"""
Download product images in-datacenter and write to HF dataset.
Stores raw JPEG bytes — no re-encoding overhead.

Processes in batches, writing each batch to its own local parquet shard before
uploading — peak memory is bounded by one batch's worth of image bytes (~1GB),
not the full ~12GB dataset, since building one big in-memory column would hold
two copies of everything at once (OOMs on cpu-upgrade's 32GB).

Reads:  hf://datasets/brianbarry97/irish_supermarket_data/all_supermarket_data.parquet
        (produced by export_data_to_hf.py)
Writes: hf://datasets/brianbarry97/irish_supermarket_data/images/part-*.parquet

Run on cpu-upgrade (~$0.03/hr) — CDN-to-HF is all datacenter bandwidth:
    uvx --from "huggingface_hub" hf jobs uv run ml/download_images_bytes_hf.py \
        --flavor cpu-upgrade \
        --secrets HF_TOKEN=$(cat ~/.cache/huggingface/token)
"""

import asyncio
import os
import tempfile

import aiohttp
import polars as pl
from huggingface_hub import HfApi
from tqdm import tqdm

DATASET_ID = "brianbarry97/irish_supermarket_data"
DOWNLOAD_WORKERS = 128
MAX_PER_HOST = 32
MAX_RETRIES = 3
BATCH_SIZE = 4000
token = os.environ["HF_TOKEN"]
storage_options = {"token": token}


async def fetch_all(urls: list[str | None]) -> list[bytes | None]:
    results: list[bytes | None] = [None] * len(urls)
    connector = aiohttp.TCPConnector(limit=DOWNLOAD_WORKERS, limit_per_host=MAX_PER_HOST)
    # Bounds how many requests are in flight at once so a task's timeout clock only
    # starts once it actually gets a connection slot — without this, asyncio.gather
    # creates all tasks upfront and thousands queue behind the connector limit long
    # enough to blow past `timeout` before ever reaching the network.
    semaphore = asyncio.Semaphore(DOWNLOAD_WORKERS)
    pbar = tqdm(total=len(urls), unit="img", leave=False)

    async def fetch(session: aiohttp.ClientSession, idx: int, url: str | None) -> None:
        if url is None:
            return
        async with semaphore:
            for attempt in range(MAX_RETRIES):
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        r.raise_for_status()
                        results[idx] = await r.read()
                        return
                except Exception as e:
                    if attempt == MAX_RETRIES - 1:
                        print(f"  Warning: failed {url}: {type(e).__name__} {e}")
                    else:
                        await asyncio.sleep(2**attempt)

    async def fetch_with_progress(session: aiohttp.ClientSession, idx: int, url: str | None) -> None:
        await fetch(session, idx, url)
        pbar.update(1)

    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[fetch_with_progress(session, i, url) for i, url in enumerate(urls)])

    pbar.close()
    return results


print("Reading input metadata...")
df = pl.read_parquet(
    f"hf://datasets/{DATASET_ID}/all_supermarket_data.parquet",
    storage_options=storage_options,
)
print(f"Loaded {len(df)} products — {df['supermarket'].value_counts().sort('supermarket')}")

n_batches = (len(df) + BATCH_SIZE - 1) // BATCH_SIZE
n_failed_total = 0

with tempfile.TemporaryDirectory() as tmpdir:
    for batch_idx in tqdm(range(n_batches), unit="batch", desc="Downloading"):
        start = batch_idx * BATCH_SIZE
        batch_df = df[start : start + BATCH_SIZE]

        image_bytes = asyncio.run(fetch_all(batch_df["image_url"].to_list()))
        n_failed_total += sum(1 for b in image_bytes if b is None)

        batch_df = batch_df.with_columns(pl.Series("image_bytes", image_bytes, dtype=pl.Binary))
        batch_df.write_parquet(os.path.join(tmpdir, f"part-{batch_idx:05d}.parquet"))
        del image_bytes, batch_df

    if n_failed_total:
        print(f"  {n_failed_total} images missing/failed to download — stored as null")

    dest = f"datasets/{DATASET_ID}/images/"
    print(f"Uploading {n_batches} shards to hf://{dest} ...")
    HfApi(token=token).upload_folder(
        folder_path=tmpdir,
        path_in_repo="images",
        repo_id=DATASET_ID,
        repo_type="dataset",
    )

print(f"Done — hf://{dest}")
