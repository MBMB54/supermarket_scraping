import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import aiohttp
import polars as pl
import requests

from scraper_common.config import setup_logging
from scraper_common.http import retry_until
from scraper_common.ids import run_ids_job
from scraper_common.storage import Storage

logger = logging.getLogger("supervalu_ids")

RETAILER = "supervalu"
REFRESH_HOURS = 7 * 24
STORE_ID = 364
# Assortment is store-specific — a single store's category pages miss products
# excluded from its local listing (e.g. regulated OTC meds) or with narrow regional
# distribution (e.g. fresh/local items). 5550 is SuperValu's only "Corporate"-type
# store ("SuperValu Online") and carries a visibly broader catalogue than any single
# branch; the rest are regular branches picked for geographic spread.
EXTRA_STORE_IDS = [5550, 1708, 260, 1261, 276]
CONCURRENT_REQUESTS = 5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PRODUCT_ID_RE = re.compile(r'/product/[^"]+?-id-(\d+)')
NEXT_PAGE_RE = re.compile(r'<link rel="next" href="([^"]+)"')
SITEMAP_PRODUCT_ID_RE = re.compile(r"/product/[^<]+-id-(\d+)")


def fetch_sitemap() -> str:
    return retry_until(
        lambda: requests.get("https://shop.supervalu.ie/sitemap.xml", timeout=30).text, wait=10
    )


def get_category_paths(xml_text: str) -> list[str]:
    all_category_urls = re.findall(
        r"<loc>(https://shop\.supervalu\.ie/categories/[^<]+)</loc>", xml_text
    )
    depth2 = [
        u.replace("https://shop.supervalu.ie/categories/", "")
        for u in all_category_urls
        if len(u.replace("https://shop.supervalu.ie/categories/", "").split("/")) == 2
    ]
    logger.info(f"Found {len(depth2)} subcategories in sitemap")
    return depth2


def get_sitemap_product_ids(xml_text: str) -> list[str]:
    ids = SITEMAP_PRODUCT_ID_RE.findall(xml_text)
    logger.info(f"Found {len(ids)} product IDs listed directly in sitemap")
    return ids


async def fetch_category_ids(
    session: aiohttp.ClientSession, category_path: str, store_id: int
) -> list[str]:
    ids = []
    url = f"https://shop.supervalu.ie/sm/delivery/rsid/{store_id}/categories/{category_path}"

    while url:
        try:
            async with session.get(url, headers=HEADERS) as response:
                if response.status != 200:
                    logger.warning(f"HTTP {response.status} for {url}")
                    break
                html = await response.text()

            page_ids = PRODUCT_ID_RE.findall(html)
            ids.extend(page_ids)

            next_match = NEXT_PAGE_RE.search(html)
            url = (
                next_match.group(1).replace(
                    "https://shop.supervalu.ie/categories/",
                    f"https://shop.supervalu.ie/sm/delivery/rsid/{store_id}/categories/",
                )
                if next_match and page_ids
                else None
            )
        except (aiohttp.ClientError, TimeoutError) as e:
            logger.warning(f"Error fetching {url}: {e}")
            break

    return ids


async def scrape_all_categories(category_paths: list[str], store_id: int) -> list[str]:
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    connector = aiohttp.TCPConnector(limit=CONCURRENT_REQUESTS, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(connect=10, sock_read=60)

    async def bounded_fetch(session: aiohttp.ClientSession, path: str) -> list[str]:
        async with semaphore:
            return await fetch_category_ids(session, path, store_id)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        results = await asyncio.gather(*[bounded_fetch(session, path) for path in category_paths])
    all_ids = list({id_ for ids in results for id_ in ids})
    logger.info(
        f"Store {store_id}: found {len(all_ids)} unique product IDs across "
        f"{len(category_paths)} categories"
    )
    return all_ids


def scrape() -> pl.DataFrame:
    xml_text = fetch_sitemap()
    category_paths = get_category_paths(xml_text)
    sitemap_ids = get_sitemap_product_ids(xml_text)
    category_ids: set[str] = set()
    if category_paths:
        for store_id in [STORE_ID, *EXTRA_STORE_IDS]:
            category_ids |= set(asyncio.run(scrape_all_categories(category_paths, store_id)))
    records = sorted(category_ids | set(sitemap_ids))
    logger.info(
        f"Combined {len(category_ids)} category-crawl IDs across {1 + len(EXTRA_STORE_IDS)} "
        f"stores with {len(sitemap_ids)} sitemap IDs into {len(records)} unique IDs"
    )
    return pl.DataFrame({"product_id": records}, schema={"product_id": pl.String})


def main() -> int:
    setup_logging(RETAILER)
    return run_ids_job(RETAILER, scrape, Storage.from_env(), min_age_hours=REFRESH_HOURS)


if __name__ == "__main__":
    sys.exit(main())
