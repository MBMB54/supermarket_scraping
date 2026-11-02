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


def delete_data_files(paths: list[str], data_path: str) -> int:
    """Delete DuckLake data files ourselves; returns how many were requested.

    Refuses anything outside `data_path`, since these paths come from the catalog and an unexpected
    one (a corrupted row, a different lake) must never reach a delete call.
    """
    root = data_path if data_path.endswith("/") else data_path + "/"
    outside = [p for p in paths if not p.startswith(root)]
    if outside:
        raise RuntimeError(f"refusing to delete outside {root}: {outside[:3]}")
    if root.startswith("s3://"):
        bucket, _, _ = root[len("s3://") :].partition("/")
        Storage(bucket=bucket).delete_many([p[len(f"s3://{bucket}/") :] for p in paths])
    else:
        for p in paths:
            Path(p).unlink(missing_ok=True)
    return len(paths)


def maintain(
    catalog: Path, data_path: str = DATA_PATH, retention_days: int = SNAPSHOT_RETENTION_DAYS
) -> int:
    """Expire old snapshots, merge the one-small-file-per-day Parquet, delete unreferenced files.

    Returns the number of data files deleted.
    """
    con = connect()
    con.execute("INSTALL ducklake; LOAD ducklake")
    con.execute(f"ATTACH 'ducklake:{catalog}' AS lake (DATA_PATH '{data_path}')")
    for sql in (
        f"CALL ducklake_expire_snapshots('lake', older_than => now() - INTERVAL '{retention_days} days')",
        "CALL ducklake_merge_adjacent_files('lake')",
    ):
        con.execute(sql)
    # DuckLake's own cleanup crashes on S3 with DuckDB 2.0.0 (INTERNAL Error: Information loss on
    # integer cast, inside httpfs S3FileSystem::RemoveFiles; reproduced on a scratch lake, fine on 1.5.6,
    # still present in 2.0.1 dev builds). So ask what it WOULD delete (dry_run) and delete those objects
    # ourselves. older_than = 1 day keeps files a concurrent reader could still be opening. The catalog's
    # scheduled-for-deletion rows are not pruned, so already-deleted paths are listed (and re-deleted,
    # harmlessly) each week; switch back to the real calls once DuckDB fixes RemoveFiles.
    doomed = set()
    for fn in ("ducklake_cleanup_old_files", "ducklake_delete_orphaned_files"):
        listed = con.execute(
            f"CALL {fn}('lake', older_than => now() - INTERVAL '1 day', dry_run => true)"
        ).fetchall()
        doomed.update(row[0] for row in listed)
    con.close()
    return delete_data_files(sorted(doomed), data_path)
