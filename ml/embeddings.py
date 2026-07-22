# /// script
# dependencies = [
#   "sentence-transformers>=5.2.2",
#   "polars",
# ]
# ///

import logging
import os
import re
from datetime import UTC, datetime

import polars as pl
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("embeddings")

# Order matters: multiplier form ("8 x 20g") must be matched before the plain
# unit pattern, or it'd leave a dangling "8 x". Requiring a unit word directly
# after the number (not just any number) avoids stripping meaningful numbers
# like infant-formula stage ("first infant milk 1") or brand numerals ("v8").
#
# Count-style patterns (pack/piece/box) require N >= 2: singular "1 piece"/
# "1 pack" is often the *only* qualifier on thin listings (e.g. magazine
# titles like "chat 1 piece"), and stripping it leaves a bare common word
# that collides with unrelated short titles in embedding space. Weight/volume
# units strip regardless of N since "1 kg" carries as little product-identity
# signal as "800 g" — the number itself is never the informative part there.
_MULTIPLIER_PATTERN = re.compile(
    r"\b\d+\s*x\s*\d+(\.\d+)?\s*(g|kg|ml|l|cl)\b", re.IGNORECASE
)
_PACK_PATTERN = re.compile(r"\b(?!1\s*(pack|packs|pk)\b)\d+\s*(pack|packs|pk)\b", re.IGNORECASE)
_PIECE_PATTERN = re.compile(r"\b(?!1\s*(piece|pieces)\b)\d+\s*(piece|pieces)\b", re.IGNORECASE)
_BOX_PATTERN = re.compile(r"\b(?!1\s*(box|boxes)\b)\d+\s*(box|boxes)\b", re.IGNORECASE)
_UNIT_PATTERN = re.compile(r"\b\d+(\.\d+)?\s*(g|kg|ml|l|cl)\b", re.IGNORECASE)


def strip_quantity(title: str) -> str:
    """Remove pack-size/weight/volume phrases so they don't dominate the embedding."""
    text = title
    for pattern in (_MULTIPLIER_PATTERN, _PACK_PATTERN, _PIECE_PATTERN, _BOX_PATTERN, _UNIT_PATTERN):
        text = pattern.sub(" ", text)
    stripped = re.sub(r"\s+", " ", text).strip()
    return stripped or title

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
    k: v for k, v in {
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY"),
        "aws_region": os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"),
    }.items() if v is not None
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
        logger.error("all_supermarket_products.parquet not found — run dbt build --select int_all_supermarket_products first")
        return

    logger.info(f"Loaded {len(all_products):,} products")

    logger.info("Reading existing embeddings from S3")
    existing = read_parquet_from_s3(EMBEDDINGS_KEY)
    if existing is None:
        existing = pl.DataFrame(schema={"id": pl.Utf8, "supermarket": pl.Utf8})
        logger.info("No existing embeddings — first run")
    else:
        logger.info(f"Found {len(existing):,} existing embeddings")

    new_products = all_products.join(existing.select(["id", "supermarket"]), on=["id", "supermarket"], how="anti")

    if new_products.is_empty():
        logger.info(f"0 new products to embed with {MODEL_ID} — skipping")
        return

    logger.info(f"Embedding {len(new_products):,} new products with {MODEL_ID}")
    model = SentenceTransformer(MODEL_ID)
    titles_for_embedding = [strip_quantity(t) for t in new_products["title"].to_list()]
    embeddings = model.encode(
        titles_for_embedding,
        prompt=EMBEDDING_PROMPT,
        normalize_embeddings=True,
        show_progress_bar=True,
        batch_size=BATCH_SIZE,
    )

    new_rows = new_products.with_columns([
        pl.Series("title_embedding", embeddings.tolist()),
        pl.lit(MODEL_ID).alias("embedding_model"),
        pl.lit(datetime.now(UTC)).alias("embedded_at"),
    ])

    updated = pl.concat([existing, new_rows], how="diagonal_relaxed") if not existing.is_empty() else new_rows
    write_parquet_to_s3(updated, EMBEDDINGS_KEY)
    logger.info(f"Written {len(updated):,} total embeddings to s3://{BUCKET}/{EMBEDDINGS_KEY}")


if __name__ == "__main__":
    main()
