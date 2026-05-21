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
import requests
import uuid
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logger = logging.getLogger("supervalu_api")

BUCKET = "ie-supermarket-data"
CONCURRENT_REQUESTS = 5
DELAY_BETWEEN_BATCHES = 2
USER_AGENT_STRINGS = [
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
]

SUPERVALU_IDS = (
    pl.read_parquet(
        f"s3://{BUCKET}/raw/supervalu/ids/latest/supervalu_product_ids.parquet",
        storage_options={"aws_region": "eu-west-1"},
    )
    .get_column("product_id")
    .unique()
    .sort()
    .to_list()
)
logger.info(f"Loaded {len(SUPERVALU_IDS)} supervalu product IDs from S3")


def fetch_store_ids() -> list[int]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "x-site-host": "https://shop.supervalu.ie",
        "x-site-location": "HeadersBuilderInterceptor",
        "x-correlation-id": str(uuid.uuid4()),
        "x-shopping-mode": "11111111-1111-1111-1111-111111111111",
        "x-customer-session-id": f"https://shop.supervalu.ie|{uuid.uuid4()}",
        "Referer": "https://shop.supervalu.ie/",
    }
    response = requests.get("https://storefrontgateway.supervalu.ie/api/stores", headers=headers, timeout=30)
    response.raise_for_status()
    return [store["retailerStoreId"] for store in response.json()["items"]]


STORES_PER_CHUNK = 60

_all_store_ids = fetch_store_ids()
logger.info(f"Loaded {len(_all_store_ids)} supervalu store IDs")


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENT_STRINGS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Origin": "https://shop.supervalu.ie",
        "Referer": "https://shop.supervalu.ie/",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def load_checkpoint(chunk_id: int, folder_date: str) -> set[str]:
    s3 = boto3.client("s3")
    key = f"raw/supervalu/{folder_date}/checkpoints/chunk_{chunk_id}.json"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        data = json.loads(obj["Body"].read())
        return set(data["processed_ids"])
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return set()
        raise


def save_checkpoint(chunk_id: int, folder_date: str, processed_ids: list[str]) -> None:
    s3 = boto3.client("s3")
    key = f"raw/supervalu/{folder_date}/checkpoints/chunk_{chunk_id}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps({"processed_ids": processed_ids}),
        ContentType="application/json",
    )


def delete_checkpoint(chunk_id: int, folder_date: str) -> None:
    s3 = boto3.client("s3")
    key = f"raw/supervalu/{folder_date}/checkpoints/chunk_{chunk_id}.json"
    try:
        s3.delete_object(Bucket=BUCKET, Key=key)
    except Exception:
        pass


async def fetch_product(
    session: aiohttp.ClientSession, product_id: str, semaphore: asyncio.Semaphore
) -> dict:
    async with semaphore:
        await asyncio.sleep(0.5)
        for store_id in STORE_IDS:
            try:
                async with session.get(
                    f"https://storefrontgateway.supervalu.ie/api/stores/{store_id}/products/{product_id}",
                    headers=get_headers(),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("name"):
                            return {
                                "product_id": product_id,
                                "store_id": store_id,
                                "data": data,
                                "error": None,
                            }
            except Exception:
                continue
        return {
            "product_id": product_id,
            "store_id": None,
            "data": None,
            "error": "Not found in any store",
        }


async def fetch_all_products(
    product_ids: list[str], chunk_id: int, folder_date: str, timestamp: str
) -> tuple[int, int]:
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    s3 = boto3.client("s3")
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_product(session, pid, semaphore) for pid in product_ids]
        total_successes = total_failures = 0
        processed_ids: list[str] = []
        batch_size = 100
        for batch_num, i in enumerate(range(0, len(tasks), batch_size)):
            batch = tasks[i : i + batch_size]
            batch_results = await asyncio.gather(*batch)

            batch_filename = f"supervalu_raw_{timestamp}_chunk{chunk_id}_batch{batch_num:04d}.jsonl.gz"
            tmp_path = f"/tmp/{batch_filename}"
            with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
                for result in batch_results:
                    f.write(json.dumps(result) + "\n")
            s3.upload_file(tmp_path, BUCKET, f"raw/supervalu/{folder_date}/{batch_filename}")

            total_successes += sum(1 for r in batch_results if r["data"])
            total_failures += sum(1 for r in batch_results if r["error"])
            processed_ids.extend(r["product_id"] for r in batch_results)
            save_checkpoint(chunk_id, folder_date, processed_ids)
            logger.info(f"Processed {min(i + batch_size, len(tasks))}/{len(tasks)} products")
            await asyncio.sleep(DELAY_BETWEEN_BATCHES)
        return total_successes, total_failures


chunk_id = int(os.environ["CHUNK_ID"])
total_chunks = int(os.environ["TOTAL_CHUNKS"])
logger.info(f"Scraping chunk {chunk_id}/{total_chunks}")

# Each chunk tries a different shuffled subset of stores for better coverage
_rng = random.Random(chunk_id)
_shuffled = _all_store_ids[:]
_rng.shuffle(_shuffled)
STORE_IDS = _shuffled[:STORES_PER_CHUNK]
logger.info(f"Using {len(STORE_IDS)} stores for this chunk")

now = datetime.datetime.now(tz=datetime.UTC)
folder_date = now.strftime("%Y-%m-%d")
timestamp = now.strftime("%Y%m%d_%H%M%S")

chunk_size = len(SUPERVALU_IDS) // total_chunks
start = chunk_id * chunk_size
end = start + chunk_size if chunk_id < total_chunks - 1 else len(SUPERVALU_IDS)
product_ids = SUPERVALU_IDS[start:end]

already_processed = load_checkpoint(chunk_id, folder_date)
if already_processed:
    logger.info(f"Resuming from checkpoint: {len(already_processed)} already processed")
    product_ids = [pid for pid in product_ids if pid not in already_processed]

logger.info(f"Fetching {len(product_ids)} products (chunk {start}:{end})")

successes, failures = asyncio.run(fetch_all_products(product_ids, chunk_id, folder_date, timestamp))

logger.info(f"Success: {successes}, Failed: {failures}")
delete_checkpoint(chunk_id, folder_date)
logger.info(f"Completed chunk {chunk_id}")
