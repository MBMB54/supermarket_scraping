import asyncio
import datetime
import gzip
import json
import logging
import os
import random

import boto3
from curl_cffi.requests import AsyncSession
import polars as pl
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.NOTSET)
handle = "aldi_api"
logger = logging.getLogger(handle)

BUCKET = "ie-supermarket-data"
CONCURRENT_REQUESTS = 5
DELAY_BETWEEN_BATCHES = 2
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

today = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d")
ALDI_IDS = (
    pl.read_parquet(
        f"s3://{BUCKET}/raw/aldi/ids/date={today}/*.parquet",
        storage_options={"aws_region": "eu-west-1"},
    )
    .get_column("product_id")
    .unique()
    .to_list()
)
logger.info(f"Loaded {len(ALDI_IDS)} aldi product IDs from S3")


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-IE",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Origin": "https://www.aldi.ie",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def load_checkpoint(chunk_id: int, folder_date: str) -> set[str]:
    s3 = boto3.client("s3")
    key = f"raw/aldi/{folder_date}/checkpoints/chunk_{chunk_id}.json"
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
    key = f"raw/aldi/{folder_date}/checkpoints/chunk_{chunk_id}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps({"processed_ids": processed_ids}),
        ContentType="application/json",
    )


def delete_checkpoint(chunk_id: int, folder_date: str) -> None:
    s3 = boto3.client("s3")
    key = f"raw/aldi/{folder_date}/checkpoints/chunk_{chunk_id}.json"
    try:
        s3.delete_object(Bucket=BUCKET, Key=key)
    except Exception:
        pass


async def fetch_product(
    session: AsyncSession, product_id: str, semaphore: asyncio.Semaphore
) -> dict:
    async with semaphore:
        await asyncio.sleep(0.5)
        try:
            response = await session.get(
                f"https://api.aldi.ie/v2/products/{product_id}",
                headers=get_headers(),
            )
            if response.status_code == 200:
                data = response.json()
                return {
                    "product_id": product_id,
                    "data": data.get("data"),
                    "error": None,
                }
            return {"product_id": product_id, "data": None, "error": f"HTTP {response.status_code}"}
        except Exception as e:
            return {"product_id": product_id, "data": None, "error": str(e)}


async def fetch_all_products(
    product_ids: list[str], chunk_id: int, folder_date: str, timestamp: str
) -> tuple[int, int]:
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    s3 = boto3.client("s3")
    async with AsyncSession(impersonate="chrome120") as session:
        tasks = [fetch_product(session, product_id, semaphore) for product_id in product_ids]
        total_successes = total_failures = 0
        processed_ids: list[str] = []
        batch_size = 100
        for batch_num, i in enumerate(range(0, len(tasks), batch_size)):
            batch = tasks[i : i + batch_size]
            batch_results = await asyncio.gather(*batch)

            batch_filename = f"aldi_raw_{timestamp}_chunk{chunk_id}_batch{batch_num:04d}.jsonl.gz"
            tmp_path = f"/tmp/{batch_filename}"
            with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
                for result in batch_results:
                    f.write(json.dumps(result) + "\n")
            s3.upload_file(tmp_path, BUCKET, f"raw/aldi/{folder_date}/{batch_filename}")

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

now = datetime.datetime.now(tz=datetime.UTC)
folder_date = now.strftime("%Y-%m-%d")
timestamp = now.strftime("%Y%m%d_%H%M%S")

chunk_size = len(ALDI_IDS) // total_chunks
start = chunk_id * chunk_size
end = start + chunk_size if chunk_id < total_chunks - 1 else len(ALDI_IDS)
product_ids = ALDI_IDS[start:end]

already_processed = load_checkpoint(chunk_id, folder_date)
if already_processed:
    logger.info(f"Resuming from checkpoint: {len(already_processed)} already processed")
    product_ids = [pid for pid in product_ids if pid not in already_processed]

logger.info(f"Fetching {len(product_ids)} products (chunk {start}:{end})")

successes, failures = asyncio.run(fetch_all_products(product_ids, chunk_id, folder_date, timestamp))

logger.info(f"Success: {successes}, Failed: {failures}")
delete_checkpoint(chunk_id, folder_date)
logger.info(f"Completed chunk {chunk_id}")
