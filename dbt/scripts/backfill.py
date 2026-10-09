"""Replay historical raw partitions into the daily price facts, oldest first.

For each run_date in [--start, --end] that has a raw partition, runs
    dbt run --select +fct_product_price_daily --vars '{run_date, retailers: [...only those present]}'
which skips tests and quality gates on purpose (historical days include known bad scrapes; the
daily fact already drops rows without a price). Per-day load stats go to table backfill_stats in
the target DuckDB so bad days can be reviewed. Afterwards run once:
    dbt build --select fct_price_history assert_price_history_consistent
Order does not matter for correctness (delete+insert by date); chronological is just tidy.

    python dbt/scripts/backfill.py --start 2026-02-01 --end 2026-10-08 \\
        --profiles-dir <dir with profiles.yml> --parquet-prefix <local dir>
Writes only to the DuckDB in the profile and the local prefix: point the profile at a scratch DB.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import date, timedelta

import boto3
import duckdb

RETAILERS = ["tesco", "dunnes", "supervalu", "aldi"]
BUCKET = "ie-supermarket-data"


def partitions(s3, retailer: str) -> set[str]:
    out, token = set(), None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": f"raw/{retailer}/", "Delimiter": "/"}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        out |= {p["Prefix"].split("/")[2] for p in r.get("CommonPrefixes", [])}
        token = r.get("NextContinuationToken")
        if not token:
            return out


def has_payload(con, retailer: str, ds: str) -> bool:
    """False for days where every record failed (data NULL everywhere): the typed models cannot
    even bind such a day, and it would contribute no prices anyway."""
    n = con.execute(
        "SELECT count(*) FILTER (WHERE data IS NOT NULL AND CAST(data AS VARCHAR) <> 'null') "
        f"FROM read_json('s3://{BUCKET}/raw/{retailer}/{ds}/*.jsonl.gz', union_by_name=true)"
    ).fetchone()[0]
    return n > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--profiles-dir", required=True)
    ap.add_argument("--db", required=True, help="DuckDB file used by the profile (for stats)")
    ap.add_argument("--parquet-prefix", required=True, help="local dir; never production S3")
    ap.add_argument("--project-dir", default="dbt")
    ap.add_argument("--dbt", default="dbt")
    a = ap.parse_args()
    if a.parquet_prefix.startswith("s3://"):
        sys.exit("refusing an S3 parquet prefix for a backfill")

    probe = duckdb.connect()
    for q in (
        "INSTALL aws",
        "LOAD aws",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET s3_region='eu-west-1'",
        "CREATE SECRET (TYPE s3, PROVIDER credential_chain)",
    ):
        probe.execute(q)
    s3 = boto3.client("s3")
    have = {r: partitions(s3, r) for r in RETAILERS}
    d, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    failed = []
    while d <= end:
        ds = d.isoformat()
        present = [r for r in RETAILERS if ds in have[r]]
        d += timedelta(days=1)
        empty = [r for r in present if not has_payload(probe, r, ds)]
        present = [r for r in present if r not in empty]
        if empty:
            print(f"{ds} skipped, no payload at all: {empty}", flush=True)
        if not present:
            continue
        vars_ = json.dumps(
            {"run_date": ds, "retailers": present, "parquet_output_prefix": a.parquet_prefix}
        )
        t0 = time.time()
        rc = subprocess.run(
            [
                a.dbt,
                "run",
                "--project-dir",
                a.project_dir,
                "--profiles-dir",
                a.profiles_dir,
                "--select",
                "+fct_product_price_daily",
                "--vars",
                vars_,
            ],
            capture_output=True,
            text=True,
        )
        secs = round(time.time() - t0, 1)
        if rc.returncode:
            failed.append(ds)
            print(f"{ds} FAILED ({secs}s): {rc.stdout[-600:]}", flush=True)
            continue
        con = duckdb.connect(a.db)
        con.execute(
            "CREATE TABLE IF NOT EXISTS backfill_stats (run_date DATE, supermarket VARCHAR, "
            "rows_loaded BIGINT, null_title BIGINT, null_price BIGINT, seconds DOUBLE)"
        )
        con.execute("DELETE FROM backfill_stats WHERE run_date = ?", [ds])
        con.execute(
            "INSERT INTO backfill_stats SELECT CAST(? AS DATE), supermarket, count(*), "
            "count(*) FILTER (WHERE title IS NULL), count(*) FILTER (WHERE price IS NULL), ? "
            "FROM fct_product_listings GROUP BY 1, 2",
            [ds, secs],
        )
        con.close()
        print(f"{ds} ok {present} {secs}s", flush=True)
    print("failed dates:", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
