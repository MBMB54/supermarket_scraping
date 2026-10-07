"""One-off: rebuild Dunnes ids/latest from the most recent complete raw scrape.

Keeps every ID whose record has data or failed transiently; drops only "Not found in any store"
IDs (saved to raw/dunnes/ids/excluded/ so they are recoverable). Dry run by default.

    uv run python scripts/dunnes_ids_from_raw.py [--date YYYY-MM-DD] [--write]
"""

import argparse
import gzip
import io
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import polars as pl

from scraper_common.config import setup_logging
from scraper_common.ids import MIN_SHRINK_RATIO, publish_ids, read_ids
from scraper_common.storage import Storage

RETAILER = "dunnes"
NOT_FOUND = "Not found in any store"


def list_keys(storage: Storage, prefix: str) -> list[str]:
    pages = storage.s3.get_paginator("list_objects_v2").paginate(
        Bucket=storage.bucket, Prefix=prefix
    )
    return [o["Key"] for page in pages for o in page.get("Contents", [])]


def dates_newest_first(storage: Storage) -> list[str]:
    resp = storage.s3.list_objects_v2(
        Bucket=storage.bucket, Prefix=f"raw/{RETAILER}/", Delimiter="/"
    )
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return sorted((n for n in names if re.fullmatch(r"\d{4}-\d{2}-\d{2}", n)), reverse=True)


def complete_chunks(keys: list[str], total_chunks: int) -> bool:
    found = {int(m.group(1)) for k in keys if (m := re.search(r"_chunk(\d+)_batch", k))}
    return found == set(range(total_chunks))


def classify(storage: Storage, keys: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"ok": set(), "not_found": set(), "transient": set()}
    for key in keys:
        for line in gzip.decompress(storage.get_bytes(key)).decode().splitlines():
            r = json.loads(line)
            if r.get("data"):
                out["ok"].add(r["product_id"])
            elif r.get("error") == NOT_FOUND:
                out["not_found"].add(r["product_id"])
            else:
                out["transient"].add(r["product_id"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--total-chunks", type=int, default=5)
    parser.add_argument("--write", action="store_true", help="write snapshot and ids/latest")
    args = parser.parse_args()
    log = setup_logging("dunnes_ids_from_raw")
    storage = Storage()

    date = args.date
    for candidate in [date] if date else dates_newest_first(storage):
        keys = [
            k for k in list_keys(storage, f"raw/{RETAILER}/{candidate}/") if k.endswith(".jsonl.gz")
        ]
        if complete_chunks(keys, args.total_chunks):
            date = candidate
            break
        log.info(f"{candidate}: incomplete ({len(keys)} files), trying earlier date")
    else:
        raise SystemExit("no complete raw scrape found")

    current = set(read_ids(storage, RETAILER))
    result = classify(storage, keys)
    keep = result["ok"] | result["transient"]
    scraped = keep | result["not_found"]
    print(f"raw date {date}: {len(keys)} files, {len(scraped)} distinct IDs")
    print(
        f"  ok={len(result['ok'])} transient/error={len(result['transient'])} "
        f"not_found={len(result['not_found'])}"
    )
    print(
        f"current ids/latest: {len(current)}; IDs in current not in scrape: {len(current - scraped)}"
    )
    print(
        f"result to keep: {len(keep)} ({len(keep) / len(current):.1%} of current, guard {MIN_SHRINK_RATIO:.0%})"
    )
    print(f"new vs current: +{len(keep - current)} -{len(current - keep)}")
    if not args.write:
        print("dry run; pass --write to publish")
        return 0

    old_snapshots = list_keys(storage, f"raw/{RETAILER}/ids/date=")
    if not any(k.endswith(".parquet") for k in old_snapshots):
        raise SystemExit("no dated snapshot of the old list exists; refusing to overwrite")
    now = datetime.now(tz=UTC)
    excluded = pl.DataFrame(
        {"product_id": sorted(result["not_found"])}, schema={"product_id": pl.String}
    )
    buf = io.BytesIO()
    excluded.write_parquet(buf)
    key = f"raw/{RETAILER}/ids/excluded/date={now:%Y-%m-%d}/dunnes_excluded_ids_{now:%Y%m%d_%H%M%S}.parquet"
    storage.put_bytes(key, buf.getvalue())
    log.info(f"saved {excluded.height} excluded IDs to {storage.uri(key)}")
    df = pl.DataFrame({"product_id": sorted(keep)}, schema={"product_id": pl.String})
    print(publish_ids(storage, RETAILER, df, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
