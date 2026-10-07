import datetime
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

BUCKET = os.environ.get("S3_BUCKET", "ie-supermarket-data")
REGION = os.environ.get("AWS_REGION", "eu-west-1")


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy in ("boto3", "botocore", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name)


@dataclass(frozen=True)
class RunConfig:
    """Per-run settings read from the environment.

    Env: CHUNK_ID, TOTAL_CHUNKS (required); RUN_DATE (YYYY-MM-DD, default today UTC);
    OUTPUT_DIR (write to this local dir instead of S3); MAX_IDS (smoke-test cap);
    MAX_ERROR_RATE / MAX_EMPTY_RATE / MAX_NOT_FOUND_RATE (data-quality gate thresholds).
    """

    retailer: str
    chunk_id: int
    total_chunks: int
    run_date: str
    timestamp: str
    output_dir: Path | None
    max_ids: int | None
    batch_size: int
    max_error_rate: float
    max_empty_rate: float
    max_not_found_rate: float

    @classmethod
    def from_env(
        cls,
        retailer: str,
        *,
        max_error_rate: float = 0.2,
        max_empty_rate: float = 0.05,
        max_not_found_rate: float = 0.6,
        batch_size: int = 100,
    ) -> RunConfig:
        chunk_id = int(os.environ["CHUNK_ID"])
        total_chunks = int(os.environ["TOTAL_CHUNKS"])
        if not 0 <= chunk_id < total_chunks:
            raise ValueError(f"CHUNK_ID={chunk_id} outside 0..{total_chunks - 1}")
        now = datetime.datetime.now(tz=datetime.UTC)
        output_dir = os.environ.get("OUTPUT_DIR")
        max_ids = os.environ.get("MAX_IDS")
        return cls(
            retailer=retailer,
            chunk_id=chunk_id,
            total_chunks=total_chunks,
            run_date=os.environ.get("RUN_DATE") or now.strftime("%Y-%m-%d"),
            timestamp=now.strftime("%Y%m%d_%H%M%S"),
            output_dir=Path(output_dir) if output_dir else None,
            max_ids=int(max_ids) if max_ids else None,
            batch_size=batch_size,
            max_error_rate=float(os.environ.get("MAX_ERROR_RATE", max_error_rate)),
            max_empty_rate=float(os.environ.get("MAX_EMPTY_RATE", max_empty_rate)),
            max_not_found_rate=float(os.environ.get("MAX_NOT_FOUND_RATE", max_not_found_rate)),
        )

    @property
    def day_prefix(self) -> str:
        return f"raw/{self.retailer}/{self.run_date}"


def chunk_slice[T](items: list[T], chunk_id: int, total_chunks: int) -> list[T]:
    """Contiguous, balanced slice; chunks differ in size by at most one item."""
    n = len(items)
    return items[n * chunk_id // total_chunks : n * (chunk_id + 1) // total_chunks]
