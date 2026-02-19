import asyncio
import gzip
import json
import logging
import random
from datetime import datetime

import aiohttp
import boto3

logging.basicConfig(level=logging.NOTSET)
handle = "supervalu_api"
logger = logging.getLogger(handle)

CONCURRENT_REQUESTS = 5
DELAY_BETWEEN_BATCHES = 2
USER_AGENT_STRINGS = [
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
]
with open("/Users/brianbarry/code/supermarket_scraping/supervalu/supervalu_ids.csv") as f:
    next(f)  # Skip header
    SUPERVALU_IDS = f.read().splitlines()


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENT_STRINGS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://shop.supervalu.ie",
        "Referer": "https://shop.supervalu.ie/",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


async def fetch_product(
    session: aiohttp.ClientSession, product_id: str, semaphore: asyncio.Semaphore
) -> dict:
    async with semaphore:
        await asyncio.sleep(0.5)

        for store_id in [5550, 309, 267, 350]:
            try:
                async with session.get(
                    f"https://storefrontgateway.supervalu.ie/api/stores/{store_id}/products/{product_id}",
                    headers=get_headers(),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        # Check if product actually exists in response
                        if data.get("name"):  # Adjust based on actual response
                            return {
                                "product_id": product_id,
                                "store_id": store_id,
                                "data": data,
                                "error": None,
                            }
                    # If 404 or empty, try next store
            except Exception:
                continue  # Try next store on error

        # All stores failed
        return {
            "product_id": product_id,
            "store_id": None,
            "data": None,
            "error": "Not found in any store",
        }


async def fetch_all_products(product_ids: list[str]) -> list[dict]:
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_product(session, product_id, semaphore) for product_id in product_ids]
        results = []
        # Process in batches for progress tracking
        batch_size = 100
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i : i + batch_size]
            batch_results = await asyncio.gather(*batch)
            results.extend(batch_results)
            # Progress update
            logger.info(f"Processed {min(i + batch_size, len(tasks))}/{len(tasks)} products")
            # Small delay between batches so requests don't get blocked
            await asyncio.sleep(DELAY_BETWEEN_BATCHES)
        return results


chunk_id = 1  # int(os.environ["CHUNK_ID"])
total_chunks = 1  # int(os.environ["TOTAL_CHUNKS"])
logger.info(f"Scraping chunk {chunk_id}/{total_chunks}")
# Each task scrapes its portion
chunk_size = len(SUPERVALU_IDS) // total_chunks
logger.info(f"Chunk size : {chunk_size}")
start = chunk_id * chunk_size
end = start + chunk_size if chunk_id < total_chunks - 1 else len(SUPERVALU_IDS)
logger.info(f"Calling API for product id {start} to {end}")
product_ids = SUPERVALU_IDS[0:501]

results = asyncio.run(fetch_all_products(product_ids))

# Filter successes and failures
successes = [r for r in results if r["data"]]
failures = [r for r in results if r["error"]]

logger.info(f"Success: {len(successes)}, Failed: {len(failures)}")

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
folder_date = datetime.now().strftime("%Y-%m-%d")
filename = f"supervalu_raw_{timestamp}_chunk{chunk_id}.jsonl.gz"
aws_tmp_location = f"/tmp/{filename}"
s3_raw_upload_location = f"raw/supervalu/{folder_date}/{filename}"
with gzip.open(aws_tmp_location, "wt", encoding="utf-8") as f:
    for result in results:
        f.write(json.dumps(result) + "\n")

s3 = boto3.client("s3")

s3.upload_file(aws_tmp_location, "ie-supermarket-data", s3_raw_upload_location)


logger.info(f"Saved {len(results)} products to {filename}")
