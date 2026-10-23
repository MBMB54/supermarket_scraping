import os
from dataclasses import dataclass

from scraper_common.config import BUCKET, REGION, setup_logging

USER_AGENT = (
    "supermarket-image-store/1.0 (personal price-comparison research; polite, <=4 concurrent)"
)

__all__ = ["BUCKET", "REGION", "MLConfig", "setup_logging"]


@dataclass(frozen=True)
class MLConfig:
    """Settings read from the environment.

    Env: EMBEDDINGS_STORE (local dir, s3://... or hf://buckets/user/bucket; default ./embedding_store),
    CATALOGUE_URI (default the dbt-written processed parquet), CHUNK_ROWS (rows per appended part
    file and per image-fetch/seed batch).

    Image fetching: IMAGE_CONCURRENCY (simultaneous requests per host), IMAGE_DELAY (seconds slept
    before each request), IMAGE_TIMEOUT (total seconds per request), IMAGE_MAX_ATTEMPTS (runs that
    may retry a transient failure before it needs --retry-failed), IMAGE_USER_AGENT,
    IMAGE_MAX_ERROR_RATE / IMAGE_MAX_NOT_FOUND_RATE (quality gate, as in scraper_common).
    """

    store: str
    catalogue: str
    chunk_rows: int
    image_concurrency: int
    image_delay: float
    image_timeout: float
    image_max_attempts: int
    image_user_agent: str
    image_max_error_rate: float
    image_max_not_found_rate: float

    @classmethod
    def from_env(cls) -> MLConfig:
        return cls(
            store=os.environ.get("EMBEDDINGS_STORE", "embedding_store"),
            catalogue=os.environ.get(
                "CATALOGUE_URI", f"s3://{BUCKET}/processed/all_supermarket_products.parquet"
            ),
            chunk_rows=int(os.environ.get("CHUNK_ROWS", "1024")),
            image_concurrency=int(os.environ.get("IMAGE_CONCURRENCY", "4")),
            image_delay=float(os.environ.get("IMAGE_DELAY", "0.25")),
            image_timeout=float(os.environ.get("IMAGE_TIMEOUT", "30")),
            image_max_attempts=int(os.environ.get("IMAGE_MAX_ATTEMPTS", "5")),
            image_user_agent=os.environ.get("IMAGE_USER_AGENT", USER_AGENT),
            image_max_error_rate=float(os.environ.get("IMAGE_MAX_ERROR_RATE", "0.2")),
            image_max_not_found_rate=float(os.environ.get("IMAGE_MAX_NOT_FOUND_RATE", "0.1")),
        )
