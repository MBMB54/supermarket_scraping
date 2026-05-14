import asyncio
import datetime
import gzip
import json
import logging
import os
import random

import aiohttp
import boto3
import polars as pl

logging.basicConfig(level=logging.NOTSET)
handle = "aldi_api"
logger = logging.getLogger(handle)

BUCKET = "ie-supermarket-data"
CONCURRENT_REQUESTS = 5
DELAY_BETWEEN_BATCHES = 2
USER_AGENT_STRINGS = [
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
]

today = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%d")
ALDI_IDS = (
    pl.read_parquet(
        f"s3://{BUCKET}/raw/aldi/ids/date={today}/*.parquet",
        storage_options={"aws_region": "eu-west-1"},
    )
    .get_column("product_id")
    .to_list()
)
logger.info(f"Loaded {len(ALDI_IDS)} aldi product IDs from S3")


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENT_STRINGS),
        "Accept": "*/*",
        "Accept-Language": "en-IE",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Origin": "https://www.aldi.ie",
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
        async with session.get(
            f"https://api.aldi.ie/v2/products/{product_id}",
            headers=get_headers(),
        ) as response:
            if response.status == 200:
                data = await response.json()
                # Check if product actually exists in response
                if data.get("data"):
                    return {
                        "product_id": product_id,
                        "data": data.get("data"),
                        "error": None,
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


chunk_id = int(os.environ["CHUNK_ID"])
total_chunks = int(os.environ["TOTAL_CHUNKS"])
logger.info(f"Scraping chunk {chunk_id}/{total_chunks}")
# Each task scrapes its portion
chunk_size = len(ALDI_IDS) // total_chunks
logger.info(f"Chunk size : {chunk_size}")
start = chunk_id * chunk_size
end = start + chunk_size if chunk_id < total_chunks - 1 else len(ALDI_IDS)
logger.info(f"Calling API for product id {start} to {end}")
product_ids = ALDI_IDS[start:end]

results = asyncio.run(fetch_all_products(product_ids))

# Filter successes and failures
successes = [r for r in results if r["data"]]
failures = [r for r in results if r["error"]]

logger.info(f"Success: {len(successes)}, Failed: {len(failures)}")

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
folder_date = datetime.now().strftime("%Y-%m-%d")
filename = f"aldi_raw_{timestamp}_chunk{chunk_id}.jsonl.gz"
aws_tmp_location = f"/tmp/{filename}"
s3_raw_upload_location = f"raw/aldi/{folder_date}/{filename}"
with gzip.open(aws_tmp_location, "wt", encoding="utf-8") as f:
    for result in results:
        f.write(json.dumps(result) + "\n")

s3 = boto3.client("s3")

s3.upload_file(aws_tmp_location, "ie-supermarket-data", s3_raw_upload_location)


logger.info(f"Saved {len(results)} products to {filename}")
