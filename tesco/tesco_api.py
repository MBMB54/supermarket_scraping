# Call Tesco API
import asyncio
import gzip
import json
import logging
import os
import random
import uuid
from datetime import datetime

import aiohttp
import boto3

logging.basicConfig(level=logging.NOTSET)
handle = "tesco_api"
logger = logging.getLogger(handle)

with open("graphql_query.txt") as file:
    TESCO_GRAPHQL_QUERY = file.read().rstrip()
with open("ie_tesco_ids.csv") as f:
    next(f)  # Skip header
    TESCO_IDS = f.read().splitlines()
CONCURRENT_REQUESTS = 5
DELAY_BETWEEN_BATCHES = 2
API_KEY = "TvOSZJHlEk0pjniDGQFAc9Q59WGAR4dA"
USER_AGENT_STRINGS = [
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
]


def get_headers():
    trace_id = str(uuid.uuid4())
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "language": "en-IE",
        "origin": "https://www.tesco.ie",
        "referer": "https://www.tesco.ie/",
        "region": "IE",
        "user-agent": random.choice(USER_AGENT_STRINGS),
        "x-apikey": API_KEY,
        "traceid": f"{trace_id}:{uuid.uuid4()}",
        "trkid": trace_id,
    }


def build_payload(tpnc: str) -> list:
    return [
        {
            "operationName": "GetProduct",
            "variables": {
                "includeVariations": True,
                "includeFulfilment": True,
                "markRecentlyViewed": False,
                "includeMatchingProducts": True,
                "tpnc": tpnc,
                "skipReviews": True,  # Skip reviews to speed up response
                "offset": 0,
                "count": 10,
                "sellersType": "ALL",
                "sellerTypeForVariations": "TOP",
                "productReviewsNodeMaxTimeout": 380,
            },
            "extensions": {"mfeName": "mfe-pdp"},
            "query": TESCO_GRAPHQL_QUERY,
        }
    ]


async def fetch_product(
    session: aiohttp.ClientSession, tpnc: str, semaphore: asyncio.Semaphore
) -> dict:
    async with semaphore:
        try:
            await asyncio.sleep(0.5)
            async with session.post(
                "https://xapi.tesco.com/", headers=get_headers(), json=build_payload(tpnc)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return {
                        "tpnc": tpnc,
                        "data": data[0].get("data", {}).get("product"),
                        "error": None,
                    }
                else:
                    return {"tpnc": tpnc, "data": None, "error": f"HTTP {response.status}"}
        except Exception as e:
            return {"tpnc": tpnc, "data": None, "error": str(e)}


async def fetch_all_products(product_ids: list[str]) -> list[dict]:
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_product(session, tpnc, semaphore) for tpnc in product_ids]
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
chunk_size = len(TESCO_IDS) // total_chunks
logger.info(f"Chunk size : {chunk_size}")
start = chunk_id * chunk_size
end = start + chunk_size if chunk_id < total_chunks - 1 else len(TESCO_IDS)
logger.info(f"Calling API for product id {start} to {end}")
product_ids = TESCO_IDS[start:end]

results = asyncio.run(fetch_all_products(product_ids))

# Filter successes and failures
successes = [r for r in results if r["data"]]
failures = [r for r in results if r["error"]]

logger.info(f"Success: {len(successes)}, Failed: {len(failures)}")

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
folder_date = datetime.now().strftime("%Y-%m-%d")
filename = f"tesco_raw_{timestamp}_chunk{chunk_id}.jsonl.gz"
aws_tmp_location = f"/tmp/{filename}"
s3_raw_upload_location = f"raw/tesco/{folder_date}/{filename}"
with gzip.open(aws_tmp_location, "wt", encoding="utf-8") as f:
    for result in results:
        f.write(json.dumps(result) + "\n")

s3 = boto3.client("s3")

s3.upload_file(aws_tmp_location, "ie-supermarket-data", s3_raw_upload_location)


logger.info(f"Saved {len(results)} products to {filename}")
