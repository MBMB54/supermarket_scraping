"""
Export product metadata from duckdb (id, title, image_url, supermarket) to an HF private dataset.
Metadata only — no image bytes. Run download_images_bytes_hf.py next (as an HF Job) to fetch
image bytes for the image_url column with datacenter-to-HF bandwidth instead of local bandwidth.

Run locally:
    uv run --group dbt --group ml python ml/export_data_to_hf.py

Uploads to: brianbarry97/irish_supermarket_data (private dataset)
  all_supermarket_data.parquet — id, supermarket, title, title_cleaned, image_url
"""

import logging
import os

import duckdb
from huggingface_hub import HfApi, HfFileSystem

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("export_data_to_hf")

DATASET_ID = "brianbarry97/irish_supermarket_data"
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "supermarket_data.db")

con = duckdb.connect(DB_PATH, read_only=True)
con.sql("CREATE OR REPLACE SECRET secret (TYPE s3, PROVIDER credential_chain);")

df = con.sql("""
    SELECT id, supermarket, title, title_cleaned, image_url FROM int_tesco     WHERE title IS NOT NULL
    UNION ALL
    SELECT id, supermarket, title, title_cleaned, image_url FROM int_dunnes    WHERE title IS NOT NULL
    UNION ALL
    SELECT id, supermarket, title, title_cleaned, image_url FROM int_supervalu WHERE title IS NOT NULL
    UNION ALL
    SELECT id, supermarket, title, title_cleaned, image_url FROM int_aldi      WHERE title IS NOT NULL
""").pl()
logger.info(f"Fetched {len(df)} products")

HfApi().create_repo(DATASET_ID, repo_type="dataset", private=True, exist_ok=True)

dest = f"datasets/{DATASET_ID}/all_supermarket_data.parquet"
logger.info(f"Writing to hf://{dest} ...")
with HfFileSystem().open(dest, "wb") as f:
    df.write_parquet(f)

logger.info(f"Done — hf://{dest}")
