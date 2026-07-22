# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "sentence-transformers[image]",
#     "polars",
#     "huggingface-hub",
#     "pillow",
#     "qwen-vl-utils",
# ]
# ///
"""
Embed Food Cupboard product images using Qwen3-VL-Embedding-2B.

Reads:  hf://datasets/brianbarry97/supermarket-image-embeddings/food_cupboard_images.parquet
Writes: hf://datasets/brianbarry97/supermarket-image-embeddings/food_cupboard_embeddings.parquet

Smoke test (200 rows):
    uvx --from "huggingface_hub" hf jobs uv run ml/image_embed_hf_job.py --flavor a100-large --env SUBSET=200 --secrets HF_TOKEN=$(cat ~/.cache/huggingface/token)

Full run:
    uvx --from "huggingface_hub" hf jobs uv run ml/image_embed_hf_job.py --flavor a100-large --secrets HF_TOKEN=$(cat ~/.cache/huggingface/token)
"""

import io
import os

import polars as pl
from huggingface_hub import HfFileSystem
from PIL import Image
from sentence_transformers import SentenceTransformer

DATASET_ID = "brianbarry97/supermarket-image-embeddings"
MODEL_ID = "Qwen/Qwen3-VL-Embedding-2B"
BATCH_SIZE = 128
SUBSET = int(os.environ.get("SUBSET", "0"))

token = os.environ["HF_TOKEN"]
storage_options = {"token": token}

print(f"Loading {MODEL_ID}...")
model = SentenceTransformer(MODEL_ID)

print("Reading input parquet...")
df = pl.read_parquet(
    f"hf://datasets/{DATASET_ID}/food_cupboard_images.parquet",
    storage_options=storage_options,
)

if SUBSET:
    df = df.head(SUBSET)
    print(f"Subset mode: using first {SUBSET} rows")

print(f"Loaded {len(df)} products — {df['retailer'].value_counts().sort('retailer')}")

image_bytes_col = df["image_bytes"].to_list()
n = len(image_bytes_col)
embedding_col: list[list[float] | None] = [None] * n

print(f"Embedding {n} images in batches of {BATCH_SIZE}...")
for start in range(0, n, BATCH_SIZE):
    chunk = image_bytes_col[start : start + BATCH_SIZE]
    images, indices = [], []
    for i, raw in enumerate(chunk):
        if raw is not None:
            images.append(Image.open(io.BytesIO(bytes(raw))).convert("RGB"))
            indices.append(start + i)

    if not images:
        continue

    vecs = model.encode(images, batch_size=BATCH_SIZE, normalize_embeddings=True)
    for idx, vec in zip(indices, vecs.tolist(), strict=True):
        embedding_col[idx] = vec

    if (start // BATCH_SIZE) % 10 == 0:
        print(f"  {min(start + BATCH_SIZE, n)}/{n}")

print("Done.")

result_df = df.drop("image_bytes").with_columns(pl.Series("embedding", embedding_col))

out_path = f"datasets/{DATASET_ID}/food_cupboard_embeddings{'_subset' if SUBSET else ''}.parquet"
print(f"Writing to hf://{out_path} ...")
with HfFileSystem(token=token).open(out_path, "wb") as f:
    result_df.write_parquet(f)

print(f"Saved to hf://{out_path}")
