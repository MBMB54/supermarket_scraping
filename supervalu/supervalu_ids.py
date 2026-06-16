import asyncio
import datetime
import logging
import os
import re

import aiohttp
import boto3
import polars as pl
import requests
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("supervalu_ids")

BUCKET = "ie-supermarket-data"
RETAILER = "supervalu"
REFRESH_DAYS = 7
STORE_ID = 364
CONCURRENT_REQUESTS = 5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PRODUCT_ID_RE = re.compile(r'/product/[^"]+?-id-(\d+)')
NEXT_PAGE_RE = re.compile(r'<link rel="next" href="([^"]+)"')


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=10, max=60),
    retry=retry_if_result(lambda x: not x),
    retry_error_callback=lambda s: [],
)
def get_category_paths() -> list[str]:
    xml_text = requests.get("https://shop.supervalu.ie/sitemap.xml", timeout=30).text
    all_category_urls = re.findall(r"<loc>(https://shop\.supervalu\.ie/categories/[^<]+)</loc>", xml_text)
    depth2 = [
        u.replace("https://shop.supervalu.ie/categories/", "")
        for u in all_category_urls
        if len(u.replace("https://shop.supervalu.ie/categories/", "").split("/")) == 2
    ]
    logger.info(f"Found {len(depth2)} subcategories in sitemap")
    return depth2


async def fetch_category_ids(session: aiohttp.ClientSession, category_path: str) -> list[str]:
    ids = []
    url = f"https://shop.supervalu.ie/sm/delivery/rsid/{STORE_ID}/categories/{category_path}"

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
                    f"https://shop.supervalu.ie/sm/delivery/rsid/{STORE_ID}/categories/",
                )
                if next_match and page_ids
                else None
            )
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            break

    return ids


async def scrape_all_categories(category_paths: list[str]) -> list[str]:
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    connector = aiohttp.TCPConnector(limit=CONCURRENT_REQUESTS, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(connect=10, sock_read=60)

    async def bounded_fetch(session: aiohttp.ClientSession, path: str) -> list[str]:
        async with semaphore:
            return await fetch_category_ids(session, path)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        results = await asyncio.gather(
            *[bounded_fetch(session, path) for path in category_paths]
        )
    all_ids = list({id_ for ids in results for id_ in ids})
    logger.info(f"Found {len(all_ids)} unique product IDs across {len(category_paths)} categories")
    return all_ids


def write_to_parquet_and_upload(records: list[str]) -> str:
    now = datetime.datetime.now(tz=datetime.UTC)
    folder_date = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    filename = f"{RETAILER}_product_ids_{timestamp}.parquet"
    s3_uri = f"s3://{BUCKET}/raw/{RETAILER}/ids/date={folder_date}/{filename}"

    df = pl.DataFrame({"product_id": records}).with_columns(
        pl.lit(now).alias("scraped_at"),
        pl.lit(RETAILER).alias("retailer"),
    )

    df.write_parquet(s3_uri, compression="snappy", storage_options={"aws_region": "eu-west-1"})
    logger.info(f"Uploaded {len(records)} product IDs to {s3_uri}")

    latest_uri = f"s3://{BUCKET}/raw/{RETAILER}/ids/latest/{RETAILER}_product_ids.parquet"
    df.write_parquet(latest_uri, compression="snappy", storage_options={"aws_region": "eu-west-1"})
    logger.info(f"Updated latest IDs at {latest_uri}")

    return s3_uri


def _recently_scraped() -> bool:
    s3 = boto3.client("s3")
    try:
        obj = s3.head_object(Bucket=BUCKET, Key=f"raw/{RETAILER}/ids/latest/{RETAILER}_product_ids.parquet")
        age = datetime.datetime.now(tz=datetime.UTC) - obj["LastModified"]
        return age.days < REFRESH_DAYS
    except s3.exceptions.ClientError:
        return False


if __name__ == "__main__":
    force = os.environ.get("FORCE_REFRESH") == "1"
    if not force and _recently_scraped():
        logger.info(f"IDs scraped within last {REFRESH_DAYS} days — skipping")
    else:
        category_paths = get_category_paths()
        if not category_paths:
            logger.error("No categories found — sitemap likely blocked. Leaving latest/ unchanged.")
        else:
            records = asyncio.run(scrape_all_categories(category_paths))
            if not records:
                logger.error("0 IDs scraped. Leaving latest/ unchanged.")
            else:
                write_to_parquet_and_upload(records)
