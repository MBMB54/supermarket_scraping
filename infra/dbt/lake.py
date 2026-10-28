"""State for the dbt Batch job: the DuckLake catalog file, the lock around it, and maintenance.

DuckLake keeps table data as Parquet under DATA_PATH and everything else (schemas, file lists,
snapshots) in a small catalog database. We use a DuckDB-file catalog, which cannot be written in
place on S3, so a run is: lock -> pull catalog -> dbt build (writes new Parquet, edits the local
catalog) -> push catalog. Pushing the catalog is the commit: a failed run never pushes, so the Parquet
it wrote is unreferenced and `ducklake_delete_orphaned_files` removes it later.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

from common import connect

from scraper_common.config import BUCKET
from scraper_common.storage import Storage

# STATE_PREFIX/LAKE_DATA/PROCESSED_PREFIX are overridden together for the shadow run so it cannot
# touch production state.
STATE_PREFIX = os.environ.get("STATE_PREFIX", "dbt/state")
CATALOG_KEY = f"{STATE_PREFIX}/catalog.ducklake"
LOCK_KEY = f"{STATE_PREFIX}/LOCK"
DATA_PATH = os.environ.get("LAKE_DATA", f"s3://{BUCKET}/ducklake/")
# A run takes ~2 minutes; the compute environment is Fargate Spot, so an interrupted run leaves its lock
# behind and the next scheduler slot (an hour later) must be able to take it over.
LOCK_MAX_AGE_HOURS = float(os.environ.get("LOCK_MAX_AGE_HOURS", "0.5"))
SNAPSHOT_RETENTION_DAYS = int(os.environ.get("SNAPSHOT_RETENTION_DAYS", "7"))


class LockHeld(RuntimeError):
    pass


def built_key(run_date: str, retailer: str) -> str:
    """Marker written (inside the lock) once a retailer-day has been built and pushed."""
    return f"{STATE_PREFIX}/built/{run_date}/{retailer}"


def acquire(storage: Storage, run_date: str) -> None:
    """Take the state lock; a lock older than LOCK_MAX_AGE_HOURS is from a killed job and is broken."""
    if storage.put_if_absent(LOCK_KEY, run_date.encode()):
        return
    held_since = storage.last_modified(LOCK_KEY)
    age_h = (datetime.now(UTC) - held_since).total_seconds() / 3600 if held_since else None
    if age_h is not None and age_h < LOCK_MAX_AGE_HOURS:
        raise LockHeld(f"state locked by run {storage.get_bytes(LOCK_KEY)!r} for {age_h:.1f}h")
    storage.delete(LOCK_KEY)
    if not storage.put_if_absent(LOCK_KEY, run_date.encode()):
        raise LockHeld("lost the race for a stale lock")


def release(storage: Storage) -> None:
    storage.delete(LOCK_KEY)


def pull(storage: Storage, local: Path) -> bool:
    """Download the catalog to `local`; False (fresh lake) when none exists yet."""
    body = storage.get_bytes(CATALOG_KEY)
    if body is None:
        return False
    local.write_bytes(body)
    return True


def push(storage: Storage, local: Path, run_date: str) -> None:
    body = local.read_bytes()
    storage.put_bytes(CATALOG_KEY, body)
    # Small dated copy for rollback, valid for about the snapshot retention window (older ones point at
    # Parquet that maintenance has deleted); a lifecycle rule expires dbt/state/archive/.
    storage.put_bytes(f"{STATE_PREFIX}/archive/{run_date}/catalog.ducklake", body)


def maintain(
    catalog: Path, data_path: str = DATA_PATH, retention_days: int = SNAPSHOT_RETENTION_DAYS
):
    """Expire old snapshots, merge the one-file-per-day Parquet, delete files nothing references."""
    con = connect()
    con.execute("INSTALL ducklake; LOAD ducklake")
    con.execute(f"ATTACH 'ducklake:{catalog}' AS lake (DATA_PATH '{data_path}')")
    # older_than = 1 day on the deletes keeps files a concurrent reader could still be opening.
    for sql in (
        f"CALL ducklake_expire_snapshots('lake', older_than => now() - INTERVAL '{retention_days} days')",
        "CALL ducklake_merge_adjacent_files('lake')",
        "CALL ducklake_cleanup_old_files('lake', older_than => now() - INTERVAL '1 day')",
        "CALL ducklake_delete_orphaned_files('lake', older_than => now() - INTERVAL '1 day')",
    ):
        con.execute(sql)
    con.close()
