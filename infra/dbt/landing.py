"""Landing layer: one retailer-day of raw gzipped JSONL -> one zstd Parquet file.

The read is equivalent to the one dbt used on the raw files (`read_json(..., union_by_name=true)`), so
the typed columns, and therefore the staging models, are unchanged; the Parquet is just a cheaper
file to re-read (about 0.65x the size, no JSON parsing). It is a derived cache: rerunning
overwrites the partition, and the raw files stay the source of truth.

    python infra/dbt/landing.py --run-date 2026-08-05 [--retailers tesco aldi]
"""

import argparse
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import LANDING_PREFIX, RAW_PREFIX, RETAILERS, connect

from scraper_common.storage import Storage


def merge_batches(raw_prefix: str, retailer: str, run_date: str, out: Path) -> int:
    """Concatenate the retailer-day's `*.jsonl.gz` batches into one gzip file; returns the count.

    A concatenation of gzip members is a valid gzip stream and reads identically (it is what
    compact_raw.py relies on). It is done here because DuckDB 2.0's `read_json(..., union_by_name=true)`
    over a day of ~200 batch files reserved more than 6 GiB (32 MiB per file) and failed the 4 GB
    Fargate task on 2026-10-29, whatever memory_limit/threads/maximum_object_size were set; one
    file needs a few hundred MiB.
    """
    if raw_prefix.startswith("s3://"):
        bucket, _, base = raw_prefix[len("s3://") :].partition("/")
        storage = Storage(bucket=bucket)
        listed = storage.list_keys(f"{base.rstrip('/')}/{retailer}/{run_date}/")
        names, read = sorted(k for k, _ in listed if k.endswith(".jsonl.gz")), storage.get_bytes
    else:
        names = sorted(str(p) for p in (Path(raw_prefix) / retailer / run_date).glob("*.jsonl.gz"))
        read = lambda name: Path(name).read_bytes()  # noqa: E731
    if not names:
        raise FileNotFoundError(f"no raw batches under {raw_prefix}/{retailer}/{run_date}/")
    with ThreadPoolExecutor(8) as pool, out.open("wb") as f:
        for body in pool.map(read, names):
            f.write(body)
    return len(names)


def land(con, retailer: str, run_date: str, raw_prefix: str, landing_prefix: str) -> int:
    """Write the retailer's partition for `run_date`; returns its row count."""
    dst = f"{landing_prefix}/{retailer}/scraped_date={run_date}/data.parquet"
    if not dst.startswith("s3://"):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        merged = Path(tmp) / "day.jsonl.gz"
        merge_batches(raw_prefix, retailer, run_date, merged)
        # sample_size=-1: one file no longer gets union_by_name's per-file schema inference, so
        # scan every record, otherwise a key that first appears late in the day would fail the read.
        con.execute(
            f"COPY (SELECT * FROM read_json('{merged}', sample_size=-1)) "
            f"TO '{dst}' (FORMAT parquet, COMPRESSION zstd)"
        )
    return con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]


def land_all(
    run_date: str, retailers=RETAILERS, raw_prefix=RAW_PREFIX, landing_prefix=LANDING_PREFIX
):
    con = connect()
    return {r: land(con, r, run_date, raw_prefix, landing_prefix) for r in retailers}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-date", required=True)
    ap.add_argument("--retailers", nargs="+", default=list(RETAILERS))
    args = ap.parse_args()
    for retailer, rows in land_all(args.run_date, args.retailers).items():
        print(f"{retailer} {args.run_date}: {rows} rows")
