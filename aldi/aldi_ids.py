import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import polars as pl
import requests

from scraper_common.config import setup_logging
from scraper_common.http import retry_until
from scraper_common.ids import run_ids_job
from scraper_common.storage import Storage

RETAILER = "aldi"
PRODUCT_URL_RE = re.compile(r"<loc>(https://www\.aldi\.ie/product/[^<]+)</loc>")
PRODUCT_ID_RE = re.compile(r"\d{18}")


def scrape_aldi_product_ids() -> list[dict]:
    xml_text = requests.get("https://www.aldi.ie/sitemap_products.xml", timeout=30).text
    results = []
    for url in PRODUCT_URL_RE.findall(xml_text):
        match = PRODUCT_ID_RE.search(url)
        if match:
            results.append({"product_id": match.group(), "url": url})
    return results


def scrape() -> pl.DataFrame:
    records = retry_until(scrape_aldi_product_ids, wait=10)
    return pl.DataFrame(records, schema={"product_id": pl.String, "url": pl.String})


def main() -> int:
    setup_logging(RETAILER)
    return run_ids_job(RETAILER, scrape, Storage.from_env(), min_age_hours=20)


if __name__ == "__main__":
    sys.exit(main())
