# /// script
# dependencies = [
#   "sentence-transformers>=5.2.2",
#   "polars",
# ]
# ///

import logging
import os
from datetime import UTC, datetime

import polars as pl
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("embeddings")


BUCKET = "ie-supermarket-data"
PRODUCTS_KEY = "processed/all_supermarket_products.parquet"
MODEL_ID = os.environ.get("EMBEDDING_MODEL", "microsoft/harrier-oss-v1-270m")
BATCH_SIZE = 128
EMBEDDING_PROMPT = (
    "Instruct: Retrieve semantically similar supermarket grocery product titles "
    "for product matching across retailers. Match on product identity — brand, "
    "type, and variety — and ignore differences in pack size, quantity, or weight\n"
    "Query: "
)

# s3 key uses the model slug so each model gets its own isolated parquet
_model_slug = MODEL_ID.replace("/", "-")
EMBEDDINGS_KEY = f"processed/embeddings/{_model_slug}/product_title_embeddings.parquet"

S3_STORAGE_OPTIONS = {
    k: v
    for k, v in {
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY"),
        "aws_region": os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"),
    }.items()
    if v is not None
}


def read_parquet_from_s3(key: str) -> pl.DataFrame | None:
    try:
        return pl.read_parquet(f"s3://{BUCKET}/{key}", storage_options=S3_STORAGE_OPTIONS)
    except Exception as e:
        if "No such file" in str(e) or "NoSuchKey" in str(e) or "404" in str(e):
            return None
        raise


def write_parquet_to_s3(df: pl.DataFrame, key: str) -> None:
    df.write_parquet(f"s3://{BUCKET}/{key}", storage_options=S3_STORAGE_OPTIONS)


def main() -> None:
    logger.info("Reading all_supermarket_products from S3")
    all_products = read_parquet_from_s3(PRODUCTS_KEY)
    if all_products is None or all_products.is_empty():
        logger.error(
            "all_supermarket_products.parquet not found — run dbt build --select int_all_supermarket_products first"
        )
        return

    logger.info(f"Loaded {len(all_products):,} products")

    logger.info("Reading existing embeddings from S3")
    existing = read_parquet_from_s3(EMBEDDINGS_KEY)
    if existing is None:
        existing = pl.DataFrame(schema={"id": pl.Utf8, "supermarket": pl.Utf8})
        logger.info("No existing embeddings — first run")
    else:
        logger.info(f"Found {len(existing):,} existing embeddings")

    new_products = all_products.join(
        existing.select(["id", "supermarket"]), on=["id", "supermarket"], how="anti"
    )

    if new_products.is_empty():
        logger.info(f"0 new products to embed with {MODEL_ID} — skipping")
        return

    logger.info(f"Embedding {len(new_products):,} new products with {MODEL_ID}")
    model = SentenceTransformer(MODEL_ID, trust_remote_code=True)
    titles_for_embedding = new_products["title_cleaned"].to_list()
    embeddings = model.encode(
        titles_for_embedding,
        prompt=EMBEDDING_PROMPT,
        normalize_embeddings=True,
        show_progress_bar=True,
        batch_size=BATCH_SIZE,
    )

    new_rows = new_products.with_columns(
        [
            pl.Series("title_embedding", embeddings.tolist()),
            pl.lit(MODEL_ID).alias("embedding_model"),
            pl.lit(datetime.now(UTC)).alias("embedded_at"),
        ]
    )

    updated = (
        pl.concat([existing, new_rows], how="diagonal_relaxed")
        if not existing.is_empty()
        else new_rows
    )
    write_parquet_to_s3(updated, EMBEDDINGS_KEY)
    logger.info(f"Written {len(updated):,} total embeddings to s3://{BUCKET}/{EMBEDDINGS_KEY}")


if __name__ == "__main__":
    main()
