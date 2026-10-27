"""Batch entrypoint: precheck the scrapes, land them as Parquet, run dbt on DuckLake, alert.

Env: RUN_DATE (YYYY-MM-DD) or SCHEDULED_TIME (ISO, from EventBridge Scheduler's
<aws.scheduler.scheduled-time>; its UTC date is used) -- one is required. A run is the final slot
of the day (and alerts when the scrapes are still not ready) when FINAL_ATTEMPT=1 or its
SCHEDULED_TIME hour is >= FINAL_HOUR_UTC (default 14); SNS_TOPIC_ARN (optional);
MAINTAIN=1|0|auto (auto = Sundays); ALLOW_FRESH_LAKE=1 (permit a first run with no catalog); OUTPUT_DIR (keep state on local disk, for tests);
STATE_PREFIX, LAKE_DATA, PROCESSED_PREFIX (together, for a shadow run; see lake.py).

Retailers are published independently. Each slot builds every retailer whose scrape is complete
and passed its gate, and leaves the others out (a "degraded" day) instead of blocking them; a later
slot adds a retailer that finished late. Rebuilding a day is idempotent, so a slot that finds nothing
new exits 0 quietly, and only the final slot reports retailers that never arrived. It exits non-zero
only when nothing could be built or a build fails, so Batch marks the job FAILED.
Draft, not deployed.
"""

import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import boto3
from common import LANDING_PREFIX, RETAILERS
from lake import (
    CATALOG_KEY,
    DATA_PATH,
    LockHeld,
    acquire,
    built_key,
    maintain,
    pull,
    push,
    release,
)
from landing import land_all
from precheck import NOT_READY, READY, classify

from scraper_common.config import BUCKET
from scraper_common.storage import Storage

ROOT = Path(__file__).resolve().parents[2]
PROJECT_DIR = ROOT / "dbt"
LOCAL_CATALOG = Path(os.environ.get("LAKE_CATALOG", "/tmp/catalog.ducklake"))
PROCESSED_PREFIX = os.environ.get("PROCESSED_PREFIX", f"s3://{BUCKET}/processed")
TOPIC = os.environ.get("SNS_TOPIC_ARN")


def alert(subject: str, body: str) -> None:
    print(subject, body, sep="\n")
    if TOPIC:
        boto3.client("sns").publish(TopicArn=TOPIC, Subject=subject[:99], Message=body[:250_000])


def resolve_run_date() -> str:
    if "RUN_DATE" in os.environ:
        return date.fromisoformat(os.environ["RUN_DATE"]).isoformat()
    return datetime.fromisoformat(os.environ["SCHEDULED_TIME"]).date().isoformat()


def is_final_slot() -> bool:
    if os.environ.get("FINAL_ATTEMPT") == "1":
        return True
    scheduled = os.environ.get("SCHEDULED_TIME")
    final_hour = int(os.environ.get("FINAL_HOUR_UTC", "14"))
    return scheduled is not None and datetime.fromisoformat(scheduled).hour >= final_hour


# Per-source row floors from models/staging/raw_s3.yml. On a degraded day the fct_product_listings
# floor (30000 for all four) becomes the sum for the retailers present.
MIN_LISTING_ROWS = {"tesco": 5000, "dunnes": 3000, "supervalu": 5000, "aldi": 1500}


def run_dbt(run_date: str, ready: list[str]) -> int:
    degraded = set(ready) != set(RETAILERS)
    dbt_vars = {
        "run_date": run_date,
        "use_landing": True,
        "landing_prefix": LANDING_PREFIX,
        "parquet_output_prefix": PROCESSED_PREFIX,
        "retailers": ready,
    }
    if degraded:
        dbt_vars["listing_min_rows"] = sum(MIN_LISTING_ROWS[r] for r in ready)
    # Unit tests run in CI/locally under Fusion; dbt-core handles three of their fixtures differently.
    # Absent retailers' sources and everything downstream of them are skipped. The processed/
    # catalogue export is skipped too on a degraded day: it would overwrite the full file ml/ reads
    # with a subset.
    exclude = ["test_type:unit"]
    exclude += [f"source:external_source.{r}+" for r in RETAILERS if r not in ready]
    exclude += ["int_all_supermarket_products"] if degraded else []
    cmd = [
        os.environ.get("DBT_BIN", "dbt"),
        "build",
        "--project-dir",
        str(PROJECT_DIR),
        "--target",
        "batch",
        "--exclude",
        *exclude,
        "--vars",
        json.dumps(dbt_vars),
    ]
    print("+", " ".join(cmd))
    return subprocess.call(
        cmd, env={**os.environ, "LAKE_CATALOG": str(LOCAL_CATALOG), "LAKE_DATA": DATA_PATH}
    )


def warnings(run_results: Path) -> list[str]:
    results = json.loads(run_results.read_text())["results"]
    return [r["unique_id"] for r in results if r["status"] == "warn"]


def should_maintain(run_date: str) -> bool:
    mode = os.environ.get("MAINTAIN", "auto")
    return mode == "1" or (mode == "auto" and date.fromisoformat(run_date).weekday() == 6)


def plan(status: dict, built: list[str], final: bool) -> str:
    """wait | fail | done | build, from per-retailer precheck status and what is already built."""
    ready = [r for r in RETAILERS if status[r][0] == READY]
    if not ready:
        pending = any(kind == NOT_READY for kind, _ in status.values())
        return "wait" if pending and not final else "fail"
    return "done" if set(ready) <= set(built) else "build"


def describe(status: dict) -> list[str]:
    return [f"{r}: {detail}" for r, (kind, detail) in status.items() if kind != READY]


def main() -> int:
    run_date = resolve_run_date()
    storage = Storage.from_env()
    status = classify(storage, run_date, RETAILERS)
    ready = [r for r in RETAILERS if status[r][0] == READY]
    built = [r for r in RETAILERS if storage.get_bytes(built_key(run_date, r)) is not None]
    excluded = describe(status)
    final = is_final_slot()

    match plan(status, built, final):
        case "wait":
            print("nothing ready yet, a later slot will retry:", *excluded, sep="\n")
            return 0
        case "fail":
            alert(f"dbt NOT RUN for {run_date}: no retailer ready", "\n".join(excluded))
            return 1
        case "done":
            if final and excluded:
                alert(
                    f"dbt DEGRADED for {run_date}: {len(excluded)} retailer(s) missing",
                    "\n".join(excluded),
                )
            else:
                print(f"{run_date}: {ready} already built")
            return 0

    try:
        acquire(storage, run_date)
    except LockHeld as e:
        alert(f"dbt NOT RUN for {run_date}", str(e))
        return 1
    try:
        if not pull(storage, LOCAL_CATALOG) and os.environ.get("ALLOW_FRESH_LAKE") != "1":
            # Missing in production means "not seeded yet" or "state lost", never "start over":
            # building from nothing would fork the price history away from seed_lake.py's copy.
            alert(
                f"dbt NOT RUN for {run_date}: no catalog",
                f"{CATALOG_KEY} does not exist. Run infra/dbt/seed_lake.py first, or set "
                "ALLOW_FRESH_LAKE=1 for a deliberately empty lake.",
            )
            return 1
        print("landed rows:", land_all(run_date, ready))
        if rc := run_dbt(run_date, ready):
            alert(f"dbt FAILED for {run_date}", "See the Batch job logs. Catalog was not updated.")
            return rc
        if should_maintain(run_date):
            maintain(LOCAL_CATALOG)
        push(storage, LOCAL_CATALOG, run_date)  # the commit: only after a green build
        for retailer in ready:
            storage.put_bytes(built_key(run_date, retailer), b"")
        warned = warnings(
            Path(os.environ.get("DBT_TARGET_PATH", PROJECT_DIR / "target")) / "run_results.json"
        )
        lines = [f"Built: {', '.join(ready)}"]
        if excluded:
            retry = "later slots will retry" if not final else "no more slots today"
            lines += [f"Excluded ({retry}):", *excluded]
        lines += ["Warnings:", *warned] if warned else ["No warnings."]
        subject = f"dbt OK for {run_date}"
        if excluded:
            subject = f"dbt OK (DEGRADED, {len(excluded)} missing) for {run_date}"
        alert(subject, "\n".join(lines))
        return 0
    finally:
        release(storage)


if __name__ == "__main__":
    sys.exit(main())
