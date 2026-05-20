import datetime
import logging
import re

import boto3
import polars as pl
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("supervalu_api")

BUCKET = "ie-supermarket-data"
RETAILER = "supervalu"


def scrape_supervalu_product_ids() -> list[dict]:
    xml_text = requests.get("https://shop.supervalu.ie/sitemap.xml", timeout=30).text
    results = re.findall(r"/product/[^<]+-id-(\d+)", xml_text)

    # results = []
    # for url in urls:
    #     match = re.search(r"\d{18}", url)
    #     if match:
    #         results.append({"product_id": match.group(), "url": url})

    return results


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

    latest_uri = f"s3://{BUCKET}/raw/{RETAILER}/ids/latest/supervalu_product_ids.parquet"
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
        records = scrape_supervalu_product_ids()
        write_to_parquet_and_upload(records)
