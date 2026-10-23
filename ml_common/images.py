import asyncio
import io
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import aiohttp
import fsspec
import polars as pl
import pyarrow.parquet as pq
from PIL import Image

from ml_common import image_map
from ml_common.config import MLConfig
from ml_common.keys import content_key, image_key
from ml_common.storage import Storage
from scraper_common.http import Response, request_with_retry
from scraper_common.quality import RunStats, gate_failures

logger = logging.getLogger(__name__)

MIN_BYTES = 200
MAX_BYTES = 25_000_000
MIN_SIDE = 16
SHARD_COLUMNS = ["id", "supermarket", "image_url", "image_bytes"]


@dataclass(frozen=True)
class ImageInfo:
    content_type: str
    width: int
    height: int


def inspect_image(data: bytes) -> ImageInfo:
    """Sniff the format from the bytes and reject anything that is not a plausible product image."""
    if not MIN_BYTES <= len(data) <= MAX_BYTES:
        raise ValueError(f"{len(data)} bytes is outside {MIN_BYTES}..{MAX_BYTES}")
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt, (width, height) = img.format, img.size
            img.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError) as e:
        raise ValueError(f"not a decodable image: {type(e).__name__}: {e}") from e
    if min(width, height) < MIN_SIDE:
        raise ValueError(f"image is only {width}x{height}")
    return ImageInfo(Image.MIME.get(fmt, f"image/{fmt.lower()}"), width, height)


def store_blob(storage: Storage, data: bytes) -> tuple[str, bool]:
    """Write `data` under its hash unless already stored; returns (sha256, already_existed)."""
    sha = content_key(data)
    key = image_key(sha)
    existed = storage.exists(key)
    if not existed:
        storage.write_bytes(key, data)
    return sha, existed


def _ok_fields(data: bytes, info: ImageInfo, sha: str) -> dict:
    return {
        "image_sha256": sha,
        "content_type": info.content_type,
        "bytes": len(data),
        "width": info.width,
        "height": info.height,
        "status": image_map.OK,
        "error": None,
    }


def _failed_fields(status: str, error: str) -> dict:
    return {
        "image_sha256": None,
        "content_type": None,
        "bytes": None,
        "width": None,
        "height": None,
        "status": status,
        "error": error,
    }


def _ingest(storage: Storage, data: bytes) -> tuple[dict, bool]:
    """Validate and store one payload; an invalid image is a permanent failure of that URL."""
    try:
        info = inspect_image(data)
    except ValueError as e:
        return _failed_fields(image_map.PERMANENT, str(e)), False
    sha, existed = store_blob(storage, data)
    return _ok_fields(data, info, sha), existed


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    value = response.headers.get("Retry-After", "")
    return float(value) if value.isdigit() else None


class _Downloader:
    """Fetches URLs with a per-host concurrency cap and a delay before every request."""

    def __init__(self, session: aiohttp.ClientSession, cfg: MLConfig):
        self.session = session
        self.delay = cfg.image_delay
        self.slots = defaultdict(lambda: asyncio.Semaphore(cfg.image_concurrency))

    async def _get(self, url: str) -> Response:
        async with self.slots[urlsplit(url).hostname]:
            await asyncio.sleep(self.delay)
            try:
                async with self.session.get(url) as resp:
                    if resp.status == 200:
                        return Response(200, data=await resp.read())
                    return Response(
                        resp.status, error=f"HTTP {resp.status}", retry_after=_retry_after(resp)
                    )
            except (aiohttp.ClientError, TimeoutError) as e:
                return Response(0, error=f"{type(e).__name__}: {e}")

    async def fetch(self, storage: Storage, url: str) -> tuple[dict, bool]:
        """Result columns for one URL and whether its blob already existed."""
        resp = await request_with_retry(lambda: self._get(url))
        if resp.status == 200:
            return await asyncio.to_thread(_ingest, storage, resp.data)
        status = image_map.TRANSIENT if resp.retryable else image_map.PERMANENT
        return _failed_fields(status, resp.error), False


@dataclass
class FetchSummary:
    pending: int
    stats: RunStats
    bytes_total: int
    blobs_reused: int
    duration_s: float
    by_supermarket: dict[str, dict[str, int]]
    failures: list[str]

    def to_dict(self) -> dict:
        return {
            "event": "image_fetch_summary",
            "pending": self.pending,
            **self.stats.to_dict(),
            "bytes_total": self.bytes_total,
            "blobs_reused": self.blobs_reused,
            "duration_s": round(self.duration_s, 1),
            "by_supermarket": self.by_supermarket,
            "status": "failed" if self.failures else "ok",
            "failures": self.failures,
        }


def _records(batch: pl.DataFrame, results: dict, fetched_at: datetime) -> pl.DataFrame:
    rows = [
        {
            **row,
            "attempts": row["attempts"] + 1,
            "fetched_at": fetched_at,
            **results[row["image_url"]],
        }
        for row in batch.to_dicts()
    ]
    return pl.DataFrame(rows, schema=image_map.SCHEMA)


async def _fetch_all(
    storage: Storage, todo: pl.DataFrame, cfg: MLConfig
) -> tuple[pl.DataFrame, int]:
    """Fetch `todo` batch by batch; returns the rows written and how many blobs already existed."""
    written = [pl.DataFrame(schema=image_map.SCHEMA)]
    blobs_reused = 0
    results: dict[str, dict] = {}
    timeout = aiohttp.ClientTimeout(total=cfg.image_timeout, connect=10)
    headers = {"User-Agent": cfg.image_user_agent}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        downloader = _Downloader(session, cfg)
        for start in range(0, len(todo), cfg.chunk_rows):
            batch = todo.slice(start, cfg.chunk_rows)
            urls = [u for u in batch["image_url"].unique().to_list() if u not in results]
            fetched = await asyncio.gather(*(downloader.fetch(storage, u) for u in urls))
            results |= {u: fields for u, (fields, _) in zip(urls, fetched, strict=True)}
            blobs_reused += sum(existed for _, existed in fetched)
            records = _records(batch, results, datetime.now(UTC))
            image_map.append_rows(storage, records)
            written.append(records)
            done = sum(len(w) for w in written)
            logger.info("images: %d/%d rows done", done, len(todo))
    return pl.concat(written), blobs_reused


def fetch_images(
    storage: Storage,
    catalogue: pl.DataFrame,
    cfg: MLConfig,
    *,
    limit: int | None = None,
    supermarkets: list[str] | None = None,
    retry_failed: bool = False,
) -> FetchSummary:
    """Download images for catalogue rows with no usable map row; one map part per batch.

    A batch is the unit of resume: blobs are written as they arrive and the map part after the
    batch, so an interrupted run redoes at most one batch (its blobs are skipped by hash).
    Identical URLs are fetched once per run; identical bytes from different URLs share a blob.
    """
    t0 = time.monotonic()
    if supermarkets:
        catalogue = catalogue.filter(pl.col("supermarket").is_in(supermarkets))
    todo = image_map.pending(
        catalogue,
        image_map.read_map(storage),
        max_attempts=cfg.image_max_attempts,
        retry_failed=retry_failed,
    )
    candidates = len(catalogue.drop_nulls("image_url"))
    todo = todo.head(limit) if limit is not None else todo
    logger.info("images: %d to fetch of %d catalogue rows with a URL", len(todo), candidates)
    written, blobs_reused = asyncio.run(_fetch_all(storage, todo, cfg))

    stats = RunStats(resumed_skipped=candidates - len(todo))
    for r in written.iter_rows(named=True):
        stats.add(
            {"error": r["error"], "data": r["image_sha256"]},
            retryable=r["status"] == image_map.TRANSIENT,
        )
    failures = gate_failures(
        stats,
        max_error_rate=cfg.image_max_error_rate,
        max_empty_rate=1.0,
        max_not_found_rate=cfg.image_max_not_found_rate,
    )
    by_supermarket: dict[str, dict[str, int]] = defaultdict(dict)
    for sm, status, n in written.group_by("supermarket", "status").len().iter_rows():
        by_supermarket[sm][status] = n
    summary = FetchSummary(
        len(todo),
        stats,
        int(written["bytes"].sum() or 0),
        blobs_reused,
        time.monotonic() - t0,
        dict(by_supermarket),
        failures,
    )
    logger.info(json.dumps(summary.to_dict()))
    return summary


@dataclass(frozen=True)
class SeedSummary:
    rows_seen: int
    imported: int
    already_mapped: int
    skipped: int
    blobs_reused: int


def _seed_batch(
    storage: Storage, batch: pl.DataFrame, fetched_at: datetime
) -> tuple[list, int, int]:
    records, skipped, reused = [], 0, 0
    for row in batch.iter_rows(named=True):
        data = row["image_bytes"]
        try:
            info = inspect_image(data)
        except ValueError:
            skipped += 1
            continue
        sha, existed = store_blob(storage, data)
        reused += existed
        records.append(
            {
                "supermarket": row["supermarket"],
                "id": row["id"],
                "image_url": row["image_url"],
                **_ok_fields(data, info, sha),
                "attempts": 0,
                "fetched_at": fetched_at,
            }
        )
    return records, skipped, reused


def seed_from_shards(
    storage: Storage, pattern: str, *, batch_rows: int, max_rows: int | None = None
) -> SeedSummary:
    """Import legacy `id, supermarket, image_url, image_bytes` parquet shards into the store.

    Reads each shard in batches (bounded memory), skips rows with no bytes or URL, no valid image,
    or already mapped to the same URL, and stamps `fetched_at` with the shard's modification time.
    Re-running continues where an interrupted import stopped.
    """
    fs, path = fsspec.core.url_to_fs(pattern)
    seen = imported = already = skipped = reused = 0
    known = image_map.latest(image_map.read_map(storage).filter(pl.col("status") == image_map.OK))
    for shard in sorted(fs.glob(path)):
        fetched_at = fs.modified(shard).astimezone(UTC)
        with fs.open(shard, "rb") as f:
            batches = pq.ParquetFile(f).iter_batches(batch_size=batch_rows, columns=SHARD_COLUMNS)
            for arrow in batches:
                batch = pl.from_arrow(arrow).drop_nulls(["image_bytes", "image_url"])
                seen += arrow.num_rows
                fresh = batch.join(known, on=image_map.PRODUCT, how="anti")
                already += len(batch) - len(fresh)
                records, bad, dup = _seed_batch(storage, fresh, fetched_at)
                skipped += arrow.num_rows - len(batch) + bad
                reused += dup
                imported += len(records)
                if records:
                    image_map.append_rows(storage, pl.DataFrame(records, schema=image_map.SCHEMA))
                logger.info("seed %s: %d rows seen, %d imported", shard, seen, imported)
                if max_rows is not None and seen >= max_rows:
                    return SeedSummary(seen, imported, already, skipped, reused)
    return SeedSummary(seen, imported, already, skipped, reused)
