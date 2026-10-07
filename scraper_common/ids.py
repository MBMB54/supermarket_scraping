import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime

import polars as pl

from scraper_common.storage import Storage

logger = logging.getLogger(__name__)

MIN_SHRINK_RATIO = 0.5
MAX_STALE_DAYS = int(os.environ.get("MAX_STALE_DAYS", "14"))


class IdsRejected(Exception):
    pass


def latest_key(retailer: str) -> str:
    return f"raw/{retailer}/ids/latest/{retailer}_product_ids.parquet"


def read_ids(storage: Storage, retailer: str, column: str = "product_id") -> list[str]:
    """Sorted unique IDs from ids/latest (or the local file named by IDS_FILE)."""
    local = os.environ.get("IDS_FILE")
    if local:
        df = pl.read_parquet(local)
    else:
        remote = Storage(None, storage.bucket)
        uri, opts = remote.parquet_uri_and_options(latest_key(retailer))
        df = pl.read_parquet(uri, storage_options=opts)
    ids = df.get_column(column).cast(pl.String).unique().sort().to_list()
    if not ids:
        raise ValueError(f"{retailer}: ids/latest is empty")
    logger.info(f"Loaded {len(ids)} {retailer} product IDs")
    return ids


def _previous_count(storage: Storage, retailer: str) -> int | None:
    if storage.last_modified(latest_key(retailer)) is None:
        return None
    uri, opts = storage.parquet_uri_and_options(latest_key(retailer))
    return pl.scan_parquet(uri, storage_options=opts).select(pl.len()).collect().item()


def publish_ids(
    storage: Storage, retailer: str, df: pl.DataFrame, now: datetime, id_column: str = "product_id"
) -> dict:
    """Write the dated snapshot, then overwrite ids/latest unless the result looks broken.

    Refuses (IdsRejected) an empty result or one smaller than MIN_SHRINK_RATIO of the current
    latest, since a blocked or partially-rendered sitemap would otherwise silently shrink the
    scrape. FORCE_IDS=1 bypasses the shrink check.
    """
    df = df.unique(subset=id_column, keep="first", maintain_order=True)
    df = df.with_columns(pl.lit(now).alias("scraped_at"), pl.lit(retailer).alias("retailer"))
    if df.height == 0:
        raise IdsRejected("0 IDs scraped")
    previous = _previous_count(storage, retailer)
    if previous and df.height < previous * MIN_SHRINK_RATIO and os.environ.get("FORCE_IDS") != "1":
        raise IdsRejected(f"{df.height} IDs vs {previous} in latest (<{MIN_SHRINK_RATIO:.0%})")

    dated = (
        f"raw/{retailer}/ids/date={now:%Y-%m-%d}/{retailer}_product_ids_{now:%Y%m%d_%H%M%S}.parquet"
    )
    for key in (dated, latest_key(retailer)):
        uri, opts = storage.parquet_uri_and_options(key)
        if storage.output_dir:
            (storage.output_dir / key).parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(uri, compression="snappy", storage_options=opts or None)
        logger.info(f"Wrote {df.height} {retailer} product IDs to {uri}")
    return {"count": df.height, "previous_count": previous, "path": dated}


def run_ids_job(
    retailer: str,
    scrape: Callable[[], pl.DataFrame],
    storage: Storage,
    *,
    min_age_hours: float,
    id_column: str = "product_id",
) -> int:
    """Run an ID-discovery job: skip if latest is fresh, else scrape and publish.

    A rejected result keeps the existing ids/latest and exits 0 so the dependent API jobs still
    run on the previous IDs; the failure is recorded in raw/{retailer}/ids/_run_summary_{date}.json
    (status "rejected") for the alert check. Once ids/latest is older than MAX_STALE_DAYS the job
    exits 1 instead, so a persistently blocked sitemap cannot go unnoticed.
    """
    now = datetime.now(tz=UTC)
    force = os.environ.get("FORCE_REFRESH") == "1"
    modified = storage.last_modified(latest_key(retailer))
    if not force and modified and (now - modified).total_seconds() < min_age_hours * 3600:
        logger.info(f"{retailer} IDs fresher than {min_age_hours}h; skipping")
        return 0

    summary: dict = {"retailer": retailer, "stage": "ids", "run_at": now.isoformat()}
    try:
        summary |= {"status": "ok", **publish_ids(storage, retailer, scrape(), now, id_column)}
    except IdsRejected as e:
        logger.error(f"{retailer} IDs rejected ({e}); leaving ids/latest unchanged")
        summary |= {"status": "rejected", "reason": str(e)}
    logger.info(json.dumps({"event": "ids_summary", **summary}))
    storage.put_json(f"raw/{retailer}/ids/_run_summary_{now:%Y-%m-%d}.json", summary)
    stale_days = (now - modified).days if modified else None
    if summary["status"] == "rejected" and stale_days is not None and stale_days > MAX_STALE_DAYS:
        logger.error(f"{retailer} ids/latest is {stale_days} days old; failing the job")
        return 1
    return 0
