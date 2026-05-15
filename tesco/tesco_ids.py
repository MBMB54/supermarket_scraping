import asyncio
import datetime
import json
import logging
import math
import random
import re
from dataclasses import asdict, dataclass, field

import boto3
import polars as pl
from botocore.exceptions import ClientError
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tesco_ids")

BUCKET = "ie-supermarket-data"
RETAILER = "tesco"
CATEGORIES = [
    "fresh-food",
    "bakery",
    "frozen-food",
    "treats-and-snacks",
    "food-cupboard",
    "drinks",
    "baby-and-toddler",
    "health-and-beauty",
    "pets",
    "household",
    "home-and-living",
]
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


@dataclass
class ScrapeProgress:
    completed_categories: list = field(default_factory=list)
    current_category: str | None = None
    current_page: int = 1
    current_category_hrefs: int = 0
    hrefs: list = field(default_factory=list)

    def get_start_page(self, category: str) -> int:
        if self.current_category == category:
            expected_hrefs = self.current_page * 48
            if self.current_category_hrefs >= expected_hrefs:
                return self.current_page + 1
            return self.current_page
        return 1

    def start_new_category(self, category: str):
        self.current_category = category
        self.current_page = 1
        self.current_category_hrefs = 0

    def add_hrefs(self, new_hrefs: list, page_num: int):
        self.hrefs.extend(new_hrefs)
        self.current_page = page_num
        self.current_category_hrefs += len(new_hrefs)

    def complete_category(self):
        if self.current_category:
            self.completed_categories.append(self.current_category)
        self.current_category = None
        self.current_page = 1
        self.current_category_hrefs = 0


def _progress_key(folder_date: str) -> str:
    return f"raw/{RETAILER}/ids/date={folder_date}/progress.json"


def load_progress(folder_date: str) -> ScrapeProgress:
    s3 = boto3.client("s3")
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=_progress_key(folder_date))
        data = json.loads(obj["Body"].read())
        return ScrapeProgress(
            completed_categories=data.get("completed_categories", []),
            current_category=data.get("current_category"),
            current_page=data.get("current_page", 1),
            current_category_hrefs=data.get("current_category_hrefs", 0),
            hrefs=data.get("hrefs", []),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return ScrapeProgress()
        raise


def save_progress(progress: ScrapeProgress, folder_date: str) -> None:
    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=BUCKET,
        Key=_progress_key(folder_date),
        Body=json.dumps(asdict(progress)),
        ContentType="application/json",
    )


def delete_progress(folder_date: str) -> None:
    s3 = boto3.client("s3")
    try:
        s3.delete_object(Bucket=BUCKET, Key=_progress_key(folder_date))
    except Exception:
        pass


def _already_ran_today(folder_date: str) -> bool:
    s3 = boto3.client("s3")
    resp = s3.list_objects_v2(
        Bucket=BUCKET,
        Prefix=f"raw/{RETAILER}/ids/date={folder_date}/tesco_product_ids",
    )
    return resp.get("KeyCount", 0) > 0


def upload_ids(hrefs: list, folder_date: str, timestamp: str) -> str:
    ids = list(
        {re.search(r"/products/(\d{9})", h).group(1) for h in hrefs if re.search(r"/products/(\d{9})", h)}
    )
    now = datetime.datetime.now(tz=datetime.UTC)
    s3_uri = f"s3://{BUCKET}/raw/{RETAILER}/ids/date={folder_date}/tesco_product_ids_{timestamp}.parquet"

    df = pl.DataFrame({"id": ids}).with_columns(
        pl.lit(now).alias("scraped_at"),
        pl.lit(RETAILER).alias("retailer"),
    )
    df.write_parquet(s3_uri, compression="snappy", storage_options={"aws_region": "eu-west-1"})

    logger.info(f"Uploaded {len(ids)} product IDs to {s3_uri}")
    return s3_uri


async def extract_page_hrefs(page) -> list:
    await page.locator(".WL_DZkV_Rvg0WJi").last.wait_for()
    return await page.locator(".WL_DZkV_Rvg0WJi a").evaluate_all(
        "els => [...new Set(els.map(el => el.href).filter(h => h.includes('/products/')))]"
    )


async def scrape_categories(folder_date: str) -> list:
    progress = load_progress(folder_date)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1920, "height": 1080},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined })"
        )
        page = await context.new_page()

        for category in CATEGORIES:
            if category in progress.completed_categories:
                logger.info(f"Skipping completed: {category}")
                continue

            if progress.current_category != category:
                progress.start_new_category(category)

            start_page = progress.get_start_page(category)
            logger.info(f"Scraping category: {category} from page {start_page}")

            await page.goto(
                f"https://www.tesco.ie/groceries/en-IE/shop/{category}/all?sortBy=relevance&page={start_page}&count=48#top",
                timeout=0,
                wait_until="load" if start_page == 1 else "domcontentloaded",
            )

            if start_page == 1:
                try:
                    await page.get_by_text("Accept all").click(timeout=5000)
                    await asyncio.sleep(1)
                except Exception:
                    pass

            pagination_string = await page.get_by_test_id("pagination-result-count").text_content()
            total_products = int(re.findall(r"\d+", pagination_string.replace(",", ""))[-1])
            max_pages = math.ceil(total_products / 48)

            hrefs = await extract_page_hrefs(page)
            progress.add_hrefs(hrefs, start_page)
            save_progress(progress, folder_date)
            logger.info(f"{category} - Page {start_page}/{max_pages} - Total hrefs: {len(progress.hrefs)}")

            for page_num in range(start_page + 1, max_pages + 1):
                await asyncio.sleep(random.uniform(2, 5))
                await page.goto(
                    f"https://www.tesco.ie/groceries/en-IE/shop/{category}/all?sortBy=relevance&page={page_num}&count=48#top",
                    timeout=0,
                    wait_until="domcontentloaded",
                )
                hrefs = await extract_page_hrefs(page)
                progress.add_hrefs(hrefs, page_num)
                save_progress(progress, folder_date)
                logger.info(f"{category} - Page {page_num}/{max_pages} - Total hrefs: {len(progress.hrefs)}")

            progress.complete_category()
            save_progress(progress, folder_date)
            await asyncio.sleep(random.uniform(5, 10))

        await browser.close()

    return progress.hrefs


if __name__ == "__main__":
    now = datetime.datetime.now(tz=datetime.UTC)
    folder_date = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y%m%d_%H%M%S")

    if _already_ran_today(folder_date):
        logger.info("IDs already scraped today — skipping")
    else:
        hrefs = asyncio.run(scrape_categories(folder_date))
        upload_ids(hrefs, folder_date, timestamp)
        delete_progress(folder_date)
