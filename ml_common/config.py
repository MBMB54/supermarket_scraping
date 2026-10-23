import os
from dataclasses import dataclass

from scraper_common.config import BUCKET, REGION, setup_logging

__all__ = ["BUCKET", "REGION", "MLConfig", "setup_logging"]


@dataclass(frozen=True)
class MLConfig:
    """Settings read from the environment.

    Env: EMBEDDINGS_STORE (local dir, s3://... or hf://buckets/user/bucket; default ./embedding_store),
    CATALOGUE_URI (default the dbt-written processed parquet), CHUNK_ROWS (rows per appended part
    file and per bulk image fetch).
    """

    store: str
    catalogue: str
    chunk_rows: int

    @classmethod
    def from_env(cls) -> MLConfig:
        return cls(
            store=os.environ.get("EMBEDDINGS_STORE", "embedding_store"),
            catalogue=os.environ.get(
                "CATALOGUE_URI", f"s3://{BUCKET}/processed/all_supermarket_products.parquet"
            ),
            chunk_rows=int(os.environ.get("CHUNK_ROWS", "1024")),
        )
