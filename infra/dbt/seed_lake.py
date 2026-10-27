"""One-off: copy the price history from the local dbt DuckDB file into a fresh DuckLake catalog.

`fct_product_price_daily` is incremental and only exists in the local `supermarket_data.db`; the
raw days cannot be replayed (several Tesco days fail the gate), so the first Batch run must start
from these rows. Refuses to run if a catalog already exists in the state store.

    python infra/dbt/seed_lake.py --local-db supermarket_data.db
Env: OUTPUT_DIR (state store on local disk) and LAKE_DATA (Parquet location) for a dry run.
"""

import argparse
import sys
from pathlib import Path

from common import connect
from lake import CATALOG_KEY, DATA_PATH, LockHeld, acquire, push, release

from scraper_common.storage import Storage

TABLE = "fct_product_price_daily"


def seed(storage: Storage, local_db: Path, catalog: Path, data_path: str = DATA_PATH) -> int:
    acquire(storage, "seed")
    try:
        if storage.get_bytes(CATALOG_KEY) is not None:
            raise SystemExit(f"{CATALOG_KEY} already exists; refusing to overwrite the lake")
        catalog.unlink(missing_ok=True)
        con = connect()
        con.execute("INSTALL ducklake; LOAD ducklake")
        con.execute(f"ATTACH '{local_db}' AS local (READ_ONLY)")
        con.execute(f"ATTACH 'ducklake:{catalog}' AS lake (DATA_PATH '{data_path}')")
        con.execute(
            f"CREATE TABLE lake.main.{TABLE} AS "
            f"SELECT * FROM local.main.{TABLE} ORDER BY supermarket, id, scraped_date"
        )
        rows = con.execute(f"SELECT count(*) FROM lake.main.{TABLE}").fetchone()[0]
        expected = con.execute(f"SELECT count(*) FROM local.main.{TABLE}").fetchone()[0]
        if rows != expected:
            raise RuntimeError(f"copied {rows} of {expected} rows")
        con.close()
        push(storage, catalog, "seed")
        return rows
    finally:
        release(storage)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local-db", type=Path, required=True)
    ap.add_argument("--catalog", type=Path, default=Path("/tmp/seed_catalog.ducklake"))
    args = ap.parse_args()
    try:
        print(f"seeded {seed(Storage.from_env(), args.local_db, args.catalog)} rows")
    except LockHeld as e:
        sys.exit(str(e))
