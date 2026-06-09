import datetime
import logging
import re

import boto3
import polars as pl
from curl_cffi.requests import Session
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dunnes_ids")

BUCKET = "ie-supermarket-data"
RETAILER = "dunnes"


def _on_retry_exhausted(retry_state):
    logger.error(f"0 IDs scraped after {retry_state.attempt_number} attempts — sitemap likely blocked. Leaving latest/ unchanged.")
    return []


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=10, max=60),
    retry=retry_if_result(lambda x: not x),
    retry_error_callback=_on_retry_exhausted,
)
def scrape_dunnes_product_ids() -> list[str]:
    with Session(impersonate="chrome120") as s:
        xml_text = s.get("https://www.dunnesstoresgrocery.com/sitemap.xml", timeout=30).text
    return re.findall(r"/product/[^<]+-id-(\d+)", xml_text)


def write_to_parquet_and_upload(records: list[dict]) -> str:
    now = datetime.datetime.now(tz=datetime.UTC)
    folder_date = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    filename = f"{RETAILER}_product_ids_{timestamp}.parquet"
    s3_uri = f"s3://{BUCKET}/raw/{RETAILER}/ids/date={folder_date}/{filename}"

    df = pl.DataFrame({"product_id": records}).with_columns(
        pl.lit(now).alias("scraped_at"),
        pl.lit(RETAILER).alias("retailer"),
    )

    df.write_parquet(
        s3_uri,
        compression="snappy",
        storage_options={
            "aws_region": "eu-west-1",
        },
    )

    logger.info(f"Uploaded {len(records)} product IDs to {s3_uri}")

    latest_uri = f"s3://{BUCKET}/raw/{RETAILER}/ids/latest/dunnes_product_ids.parquet"
    df.write_parquet(latest_uri, compression="snappy", storage_options={"aws_region": "eu-west-1"})
    logger.info(f"Updated latest IDs at {latest_uri}")

    return s3_uri


def _already_ran_today() -> bool:
    s3 = boto3.client("s3")
    today = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d")
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"raw/{RETAILER}/ids/date={today}/")
    return resp.get("KeyCount", 0) > 0


if __name__ == "__main__":
    if _already_ran_today():
        logger.info("IDs already scraped today — skipping")
    else:
        records = scrape_dunnes_product_ids()
        if not records:
            logger.error("0 IDs scraped — sitemap likely blocked. Leaving latest/ unchanged.")
        else:
            write_to_parquet_and_upload(records)
