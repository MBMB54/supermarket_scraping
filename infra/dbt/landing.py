"""Landing layer: one retailer-day of raw gzipped JSONL -> one zstd Parquet file.

The read is exactly the one dbt used on the raw files (`read_json(..., union_by_name=true)`), so
the typed columns, and therefore the staging models, are unchanged; the Parquet is just a cheaper
file to re-read (about 0.65x the size, no JSON parsing). It is a derived cache: rerunning
overwrites the partition, and the raw files stay the source of truth.

    python infra/dbt/landing.py --run-date 2026-08-05 [--retailers tesco aldi]
"""

import argparse
from pathlib import Path

from common import LANDING_PREFIX, RAW_PREFIX, RETAILERS, connect


def land(con, retailer: str, run_date: str, raw_prefix: str, landing_prefix: str) -> int:
    """Write the retailer's partition for `run_date`; returns its row count."""
    src = f"{raw_prefix}/{retailer}/{run_date}/*.jsonl.gz"
    dst = f"{landing_prefix}/{retailer}/scraped_date={run_date}/data.parquet"
    if not dst.startswith("s3://"):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY (SELECT * FROM read_json('{src}', union_by_name=true)) "
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
