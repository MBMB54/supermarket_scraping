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
Download Food Cupboard product images in-datacenter and write to HF dataset.
Stores raw JPEG bytes — no re-encoding overhead.

Reads:  hf://datasets/brianbarry97/supermarket-image-embeddings/food_cupboard_input.parquet
Writes: hf://datasets/brianbarry97/supermarket-image-embeddings/food_cupboard_images.parquet

Run on cpu-upgrade (~$0.03/hr) — CDN-to-HF is all datacenter bandwidth:
    uvx --from "huggingface_hub" hf jobs uv run ml/fetch_images_hf_job.py \
        --flavor cpu-upgrade \
        --secrets HF_TOKEN=$(cat ~/.cache/huggingface/token)
"""

import asyncio
import os

import aiohttp
import polars as pl
from huggingface_hub import HfFileSystem
from tqdm import tqdm

DATASET_ID = "brianbarry97/supermarket-image-embeddings"
DOWNLOAD_WORKERS = 128
MAX_PER_HOST = 32
MAX_RETRIES = 3
token = os.environ["HF_TOKEN"]
storage_options = {"token": token}


async def fetch_all(urls: list[str]) -> list[bytes | None]:
    results: list[bytes | None] = [None] * len(urls)
    connector = aiohttp.TCPConnector(limit=DOWNLOAD_WORKERS, limit_per_host=MAX_PER_HOST)
    pbar = tqdm(total=len(urls), unit="img")

    async def fetch(session: aiohttp.ClientSession, idx: int, url: str) -> None:
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
        pbar.update(1)

    async def fetch_with_progress(session: aiohttp.ClientSession, idx: int, url: str) -> None:
        await fetch(session, idx, url)
        pbar.update(1)

    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[fetch_with_progress(session, i, url) for i, url in enumerate(urls)])

    pbar.close()
    return results


print("Reading input metadata...")
df = pl.read_parquet(
    f"hf://datasets/{DATASET_ID}/food_cupboard_input.parquet",
    storage_options=storage_options,
)
print(f"Loaded {len(df)} products — {df['retailer'].value_counts().sort('retailer')}")

urls = df["image_url"].to_list()
print(f"Downloading {len(urls)} images ({DOWNLOAD_WORKERS} concurrent)...")
image_bytes = asyncio.run(fetch_all(urls))

n_failed = sum(1 for b in image_bytes if b is None)
if n_failed:
    print(f"  {n_failed} images failed to download — stored as null")

df = df.with_columns(pl.Series("image_bytes", image_bytes, dtype=pl.Binary))

dest = f"datasets/{DATASET_ID}/food_cupboard_images.parquet"
print(f"Writing to hf://{dest} ...")
with HfFileSystem(token=token).open(dest, "wb") as f:
    df.write_parquet(f)

print(f"Done — hf://{dest}")
