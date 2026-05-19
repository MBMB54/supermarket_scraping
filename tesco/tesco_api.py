import asyncio
import datetime
import gzip
import json
import logging
import os
import random
import uuid

import aiohttp
import boto3
import polars as pl
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
handle = "tesco_api"
logger = logging.getLogger(handle)

with open("graphql_query.txt") as file:
    TESCO_GRAPHQL_QUERY = file.read().rstrip()

BUCKET = "ie-supermarket-data"
CONCURRENT_REQUESTS = 5
DELAY_BETWEEN_BATCHES = 2
API_KEY = "TvOSZJHlEk0pjniDGQFAc9Q59WGAR4dA"
USER_AGENT_STRINGS = [
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
]

TESCO_IDS = (
    pl.read_parquet(
        f"s3://{BUCKET}/raw/tesco/ids/latest/tesco_product_ids.parquet",
        storage_options={"aws_region": "eu-west-1"},
    )
    .get_column("id")
    .cast(pl.String)
    .unique()
    .to_list()
)
logger.info(f"Loaded {len(TESCO_IDS)} tesco product IDs from S3")


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
                "skipReviews": True,
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


def load_checkpoint(chunk_id: int, folder_date: str) -> set[str]:
    s3 = boto3.client("s3")
    key = f"raw/tesco/{folder_date}/checkpoints/chunk_{chunk_id}.json"
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
    key = f"raw/tesco/{folder_date}/checkpoints/chunk_{chunk_id}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps({"processed_ids": processed_ids}),
        ContentType="application/json",
    )


def delete_checkpoint(chunk_id: int, folder_date: str) -> None:
    s3 = boto3.client("s3")
    key = f"raw/tesco/{folder_date}/checkpoints/chunk_{chunk_id}.json"
    try:
        s3.delete_object(Bucket=BUCKET, Key=key)
    except Exception:
        pass


async def fetch_product(
    session: aiohttp.ClientSession, tpnc: str, semaphore: asyncio.Semaphore
) -> dict:
    async with semaphore:
        await asyncio.sleep(0.5)
        try:
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
                return {"tpnc": tpnc, "data": None, "error": f"HTTP {response.status}"}
        except Exception as e:
            return {"tpnc": tpnc, "data": None, "error": str(e)}


async def fetch_all_products(
    product_ids: list[str], chunk_id: int, folder_date: str, timestamp: str
) -> tuple[int, int]:
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    s3 = boto3.client("s3")
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_product(session, tpnc, semaphore) for tpnc in product_ids]
        total_successes = total_failures = 0
        processed_ids: list[str] = []
        batch_size = 100
        for batch_num, i in enumerate(range(0, len(tasks), batch_size)):
            batch = tasks[i : i + batch_size]
            batch_results = await asyncio.gather(*batch)

            batch_filename = f"tesco_raw_{timestamp}_chunk{chunk_id}_batch{batch_num:04d}.jsonl.gz"
            tmp_path = f"/tmp/{batch_filename}"
            with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
                for result in batch_results:
                    f.write(json.dumps(result) + "\n")
            s3.upload_file(tmp_path, BUCKET, f"raw/tesco/{folder_date}/{batch_filename}")

            total_successes += sum(1 for r in batch_results if r["data"])
            total_failures += sum(1 for r in batch_results if r["error"])
            processed_ids.extend(r["tpnc"] for r in batch_results)
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

chunk_size = len(TESCO_IDS) // total_chunks
start = chunk_id * chunk_size
end = start + chunk_size if chunk_id < total_chunks - 1 else len(TESCO_IDS)
product_ids = TESCO_IDS[start:end]

already_processed = load_checkpoint(chunk_id, folder_date)
if already_processed:
    logger.info(f"Resuming from checkpoint: {len(already_processed)} already processed")
    product_ids = [tpnc for tpnc in product_ids if tpnc not in already_processed]

logger.info(f"Fetching {len(product_ids)} products (chunk {start}:{end})")

successes, failures = asyncio.run(fetch_all_products(product_ids, chunk_id, folder_date, timestamp))

logger.info(f"Success: {successes}, Failed: {failures}")
delete_checkpoint(chunk_id, folder_date)
logger.info(f"Completed chunk {chunk_id}")
