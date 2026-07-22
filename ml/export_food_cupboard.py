"""
Export Food Cupboard products from int_tesco and int_supervalu to an HF private dataset.
Downloads all product images in parallel and stores lossless PNG bytes in the parquet so
future embedding experiments don't need to re-hit CDN URLs.

Run locally:
    uv run --group dbt --group ml python ml/export_food_cupboard.py

Uploads to: brianbarry97/supermarket-image-embeddings (private dataset)
  food_cupboard_images.parquet  — id, title, image_url, retailer, image_bytes (PNG)
"""

import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import duckdb
import polars as pl
import requests
from huggingface_hub import HfApi, HfFileSystem
from PIL import Image
from tqdm import tqdm

DATASET_ID = "brianbarry97/supermarket-image-embeddings"
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "supermarket_data.db")
DOWNLOAD_WORKERS = 32


def fetch_image_bytes(args: tuple[int, str]) -> tuple[int, bytes | None]:
    idx, url = args
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return idx, buf.getvalue()
    except Exception as e:
        print(f"  Warning: failed {url}: {e}")
        return idx, None


con = duckdb.connect(DB_PATH, read_only=True)
con.sql("CREATE OR REPLACE SECRET secret (TYPE s3, PROVIDER credential_chain);")

df_tesco = con.sql("""
    SELECT id, title, image_url, 'tesco' AS retailer
    FROM int_tesco
    WHERE category_1 = 'Food Cupboard'
""").pl()

df_sv = con.sql("""
    SELECT id, title, image_url, 'supervalu' AS retailer
    FROM int_supervalu
    WHERE category_1 = 'Food Cupboard'
""").pl()

df = pl.concat([df_tesco, df_sv])
print(f"Fetched {len(df)} products — tesco: {len(df_tesco)}, supervalu: {len(df_sv)}")

image_bytes: list[bytes | None] = [None] * len(df)
tasks = list(enumerate(df["image_url"].to_list()))

print(f"Downloading {len(tasks)} images with {DOWNLOAD_WORKERS} workers...")
with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
    futures = {executor.submit(fetch_image_bytes, t): t[0] for t in tasks}
    for future in tqdm(as_completed(futures), total=len(tasks), unit="img"):
        idx, data = future.result()
        image_bytes[idx] = data

n_failed = sum(1 for b in image_bytes if b is None)
if n_failed:
    print(f"  {n_failed} images failed to download — stored as null")

df = df.with_columns(pl.Series("image_bytes", image_bytes, dtype=pl.Binary))

HfApi().create_repo(DATASET_ID, repo_type="dataset", private=True, exist_ok=True)

dest = f"datasets/{DATASET_ID}/food_cupboard_images.parquet"
print(f"Writing to hf://{dest} ...")
with HfFileSystem().open(dest, "wb") as f:
    df.write_parquet(f)

print(f"Done — hf://{dest}")
