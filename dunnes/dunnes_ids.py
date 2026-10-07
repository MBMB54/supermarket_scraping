import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import polars as pl
from curl_cffi.requests import Session

from scraper_common.config import setup_logging
from scraper_common.http import retry_until
from scraper_common.ids import run_ids_job
from scraper_common.storage import Storage

RETAILER = "dunnes"
REFRESH_HOURS = 7 * 24
PRODUCT_ID_RE = re.compile(r"/product/[^<]+-id-(\d+)")


def scrape_dunnes_product_ids() -> list[str]:
    with Session(impersonate="chrome136") as s:
        xml_text = s.get("https://www.dunnesstoresgrocery.com/sitemap.xml", timeout=30).text
    return PRODUCT_ID_RE.findall(xml_text)


def scrape() -> pl.DataFrame:
    ids = retry_until(scrape_dunnes_product_ids, wait=10)
    return pl.DataFrame({"product_id": ids}, schema={"product_id": pl.String})


def main() -> int:
    setup_logging(RETAILER)
    return run_ids_job(RETAILER, scrape, Storage.from_env(), min_age_hours=REFRESH_HOURS)


if __name__ == "__main__":
    sys.exit(main())
