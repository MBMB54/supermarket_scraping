"""Merge each retailer-day's raw gzipped JSONL batches into one object, then delete the batches.

Why: a day is ~560 objects, 88% under 128 KB, which S3 Glacier Instant Retrieval bills as 128 KB and
charges a transition request for. One `compacted.jsonl.gz` per retailer-day (a concatenation of gzip
members, which is a valid gzip stream and reads identically through `read_json('*.jsonl.gz')`)
removes both costs. Summaries and checkpoints are left alone.

Safety, since this deletes the source of truth (the bucket is versioned, so deletes stay
recoverable only until the lifecycle rule expires noncurrent versions, 14 days):
  * only dates at least MIN_AGE_DAYS behind RUN_DATE (scrapers may still be resuming a recent one);
  * the merged object's row count must equal the sum over the originals, before and after upload;
  * a partition that already has `compacted.jsonl.gz` is never re-merged (that would double count):
    it is re-verified against the leftover originals and those are deleted;
  * `--dry-run` reports what would happen.

    python infra/dbt/compact_raw.py --run-date 2026-10-26 [--retailers tesco] [--only-date D]
        [--max-age-days N] [--dry-run]
Env: OUTPUT_DIR (operate on a local directory instead of S3).
"""

import argparse
import gzip
import re
from collections import defaultdict
from datetime import date, timedelta

from common import RETAILERS

from scraper_common.storage import Storage

COMPACTED = "compacted.jsonl.gz"
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def count_rows(gz_bytes: bytes) -> int:
    data = gzip.decompress(gz_bytes)
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def floor_ok(day: str, floor: date | None) -> bool:
    return floor is None or date.fromisoformat(day) >= floor


def compact_partition(storage: Storage, prefix: str, dry_run: bool = False) -> str:
    """Compact `prefix` (raw/{retailer}/{date}/); returns a short status for the log."""
    files = [k for k, _ in storage.list_keys(prefix) if "/" not in k[len(prefix) :]]
    batches = [k for k in files if k.endswith(".jsonl.gz") and not k.endswith("/" + COMPACTED)]
    target = prefix + COMPACTED
    if not batches:
        return "nothing to do"
    expected = sum(count_rows(storage.get_bytes(k)) for k in batches)
    if dry_run:
        return f"would merge {len(batches)} objects, {expected} rows"
    if target in files:
        status = "finished deletion"
    else:
        storage.put_bytes(target, b"".join(storage.get_bytes(k) for k in sorted(batches)))
        status = "compacted"
    merged = count_rows(storage.get_bytes(target))
    if merged != expected:
        raise RuntimeError(f"{target}: {merged} rows vs {expected} in {len(batches)} originals")
    storage.delete_many(batches)
    return f"{status}: {len(batches)} objects -> 1, {merged} rows"


def compact(
    storage: Storage,
    retailers,
    cutoff: date,
    dry_run: bool = False,
    only_date: str | None = None,
    floor: date | None = None,
) -> dict[str, str]:
    """Compact every retailer-day with floor <= date <= cutoff (just `only_date` if given)."""
    results = {}
    for retailer in retailers:
        base = f"raw/{retailer}/"
        days = defaultdict(int)
        for key, _ in storage.list_keys(base):
            day = key[len(base) :].split("/", 1)[0]
            if (
                DATE_DIR.match(day)
                and floor_ok(day, floor)
                and date.fromisoformat(day) <= cutoff
                and only_date in (None, day)
            ):
                days[day] += 1
        for day in sorted(days):
            prefix = f"{base}{day}/"
            results[prefix] = compact_partition(storage, prefix, dry_run)
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--run-date", required=True, help="YYYY-MM-DD; dates newer than the cutoff are kept"
    )
    ap.add_argument("--min-age-days", type=int, default=2)
    ap.add_argument("--retailers", nargs="+", default=list(RETAILERS))
    ap.add_argument(
        "--only-date", help="compact just this YYYY-MM-DD (try one day before a backfill)"
    )
    ap.add_argument(
        "--max-age-days",
        type=int,
        help="also skip dates older than this; the scheduled job sets it so it can never run the "
        "historical backfill, which is a deliberate manual run without it",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cutoff = date.fromisoformat(args.run_date) - timedelta(days=args.min_age_days)
    run_date = date.fromisoformat(args.run_date)
    floor = run_date - timedelta(days=args.max_age_days) if args.max_age_days else None
    results = compact(
        Storage.from_env(), args.retailers, cutoff, args.dry_run, args.only_date, floor
    )
    for prefix, status in results.items():
        print(prefix, status)
