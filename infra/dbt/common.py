"""Settings and the DuckDB connection shared by the dbt Batch job's steps."""

import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: scraper_common

from scraper_common.config import BUCKET, REGION  # noqa: E402

RETAILERS = ("tesco", "aldi", "dunnes", "supervalu")
RAW_PREFIX = os.environ.get("RAW_PREFIX", f"s3://{BUCKET}/raw")
LANDING_PREFIX = os.environ.get("LANDING_PREFIX", f"s3://{BUCKET}/landing")
MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "3GB")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs")
    con.execute(f"CREATE SECRET (TYPE s3, PROVIDER credential_chain, REGION '{REGION}')")
    # In a container DuckDB sizes its limit from the host's RAM, which can exceed the Fargate task's
    # (it reported 6 GiB on a 4 GB task), so state the limit explicitly.
    con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
    return con
