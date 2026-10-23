import polars as pl

from ml_common.storage import Storage

PREFIX = "image_map"
PRODUCT = ["supermarket", "id", "image_url"]
OK = "ok"
TRANSIENT = "transient"
PERMANENT = "permanent"

SCHEMA = {
    "supermarket": pl.String,
    "id": pl.String,
    "image_url": pl.String,
    "image_sha256": pl.String,
    "content_type": pl.String,
    "bytes": pl.Int64,
    "width": pl.Int32,
    "height": pl.Int32,
    "status": pl.String,
    "error": pl.String,
    "attempts": pl.Int32,
    "fetched_at": pl.Datetime("us", "UTC"),
}


def read_map(storage: Storage) -> pl.DataFrame:
    """Every map row ever written: one per fetch attempt, newest last per product and URL."""
    parts = [storage.read_parquet(k) for k in storage.list(PREFIX) if k.endswith(".parquet")]
    return pl.concat([pl.DataFrame(schema=SCHEMA), *parts])


def append_rows(storage: Storage, rows: pl.DataFrame) -> str:
    return storage.append_parquet(rows.select(list(SCHEMA)), PREFIX)


def latest(image_map: pl.DataFrame) -> pl.DataFrame:
    """Newest row per (supermarket, id, image_url)."""
    return image_map.sort("fetched_at").unique(subset=PRODUCT, keep="last", maintain_order=True)


def pending(
    catalogue: pl.DataFrame, image_map: pl.DataFrame, *, max_attempts: int, retry_failed: bool
) -> pl.DataFrame:
    """Catalogue rows to fetch, with their previous attempt count, interleaved across retailers.

    A (supermarket, id, image_url) with no row is new, so a changed URL is refetched. A transient
    failure is retried while its attempts are under `max_attempts`; permanent failures and
    exhausted transient ones wait for `retry_failed`.
    """
    last = latest(image_map).select(*PRODUCT, "status", "attempts")
    todo = (
        catalogue.drop_nulls("image_url")
        .filter(pl.col("image_url") != "")
        .select(PRODUCT)
        .join(last, on=PRODUCT, how="left")
    )
    wanted = (
        pl.col("status").is_null()
        | ((pl.col("status") == TRANSIENT) & (pl.col("attempts") < max_attempts))
        | (pl.lit(retry_failed) & (pl.col("status") != OK))
    )
    return (
        todo.filter(wanted)
        .with_columns(pl.col("attempts").fill_null(0))
        .with_columns(pl.int_range(pl.len()).over("supermarket").alias("_rank"))
        .sort("_rank", "supermarket")
        .drop("_rank", "status")
    )


def attach_image_sha256(catalogue: pl.DataFrame, image_map: pl.DataFrame) -> pl.DataFrame:
    """Add `image_sha256` from the newest ok row for the product's current image URL.

    Matching on the URL as well as (supermarket, id) means a product whose URL changed has no hash
    until its new image is fetched, so its old embedding is superseded rather than reused.
    """
    ok = latest(image_map.filter(pl.col("status") == OK)).select(*PRODUCT, "image_sha256")
    return catalogue.join(ok, on=PRODUCT, how="left")
