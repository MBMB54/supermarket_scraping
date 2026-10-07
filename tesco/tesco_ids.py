import asyncio
import logging
import math
import os
import random
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import polars as pl
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from patchright.async_api import async_playwright

from scraper_common.config import setup_logging
from scraper_common.ids import run_ids_job
from scraper_common.storage import Storage

logger = logging.getLogger("tesco_ids")

RETAILER = "tesco"
PRODUCT_ID_RE = re.compile(r"/products/(\d{9})")
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


def load_progress(storage: Storage, folder_date: str) -> ScrapeProgress:
    data = storage.get_json(_progress_key(folder_date))
    return ScrapeProgress(**data) if data else ScrapeProgress()


def save_progress(storage: Storage, progress: ScrapeProgress, folder_date: str) -> None:
    storage.put_json(_progress_key(folder_date), asdict(progress))


def hrefs_to_df(hrefs: list) -> pl.DataFrame:
    ids = {m.group(1) for h in hrefs if (m := PRODUCT_ID_RE.search(h))}
    return pl.DataFrame({"id": sorted(ids)}, schema={"id": pl.String})


async def extract_page_hrefs(page, storage: Storage) -> list:
    try:
        await page.locator("a[href*='/products/']").first.wait_for()
    except PlaywrightTimeoutError:
        logger.error(f"Timed out waiting for product links. Title: '{await page.title()}'")
        key = f"raw/{RETAILER}/debug/extract_page_hrefs_error_{datetime.now(tz=UTC):%Y%m%d_%H%M%S}.png"
        storage.put_bytes(key, await page.screenshot(), "image/png")
        logger.error(f"Screenshot: {storage.uri(key)}")
        raise
    return await page.evaluate(
        "() => [...new Set([...document.querySelectorAll('a[href*=\"/products/\"]')].map(el => el.href))]"
    )


async def scrape_categories(
    storage: Storage,
    folder_date: str,
    categories: list = CATEGORIES,
    page_limit: int | None = None,
) -> list:
    progress = load_progress(storage, folder_date)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        cookies_accepted = False

        for category in categories:
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
                wait_until="load",
            )

            if not cookies_accepted:
                try:
                    await page.get_by_text("Accept all").click(timeout=5000)
                    await asyncio.sleep(1)
                    cookies_accepted = True
                except PlaywrightTimeoutError:
                    logger.info("No cookie banner")

            hrefs = await extract_page_hrefs(page, storage)
            pagination_string = await page.get_by_test_id("pagination-result-count").text_content()
            total_products = int(re.findall(r"\d+", pagination_string.replace(",", ""))[-1])
            max_pages = math.ceil(total_products / 48)
            progress.add_hrefs(hrefs, start_page)
            save_progress(storage, progress, folder_date)
            logger.info(
                f"{category} - Page {start_page}/{max_pages} - Total hrefs: {len(progress.hrefs)}"
            )

            for page_num in range(
                start_page + 1, (min(max_pages, page_limit) if page_limit else max_pages) + 1
            ):
                await asyncio.sleep(random.uniform(2, 5))
                await page.goto(
                    f"https://www.tesco.ie/groceries/en-IE/shop/{category}/all?sortBy=relevance&page={page_num}&count=48#top",
                    timeout=0,
                    wait_until="load",
                )
                hrefs = await extract_page_hrefs(page, storage)
                progress.add_hrefs(hrefs, page_num)
                save_progress(storage, progress, folder_date)
                logger.info(
                    f"{category} - Page {page_num}/{max_pages} - Total hrefs: {len(progress.hrefs)}"
                )

            progress.complete_category()
            save_progress(storage, progress, folder_date)
            await asyncio.sleep(random.uniform(5, 10))

        await browser.close()

    return progress.hrefs


def scrape(storage: Storage) -> pl.DataFrame:
    folder_date = f"{datetime.now(tz=UTC):%Y-%m-%d}"
    if os.environ.get("TEST_MODE"):
        hrefs = asyncio.run(
            scrape_categories(storage, f"test-{folder_date}", ["fresh-food"], page_limit=5)
        )
        storage.delete(_progress_key(f"test-{folder_date}"))
    else:
        hrefs = asyncio.run(scrape_categories(storage, folder_date))
        storage.delete(_progress_key(folder_date))
    return hrefs_to_df(hrefs)


def main() -> int:
    setup_logging("tesco_ids")
    storage = Storage.from_env()
    if os.environ.get("TEST_MODE") and not storage.output_dir:
        raise SystemExit("TEST_MODE requires OUTPUT_DIR so ids/latest is not overwritten")
    return run_ids_job(RETAILER, lambda: scrape(storage), storage, min_age_hours=20, id_column="id")


if __name__ == "__main__":
    sys.exit(main())
