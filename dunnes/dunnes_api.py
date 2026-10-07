import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import aiohttp

from scraper_common.config import RunConfig, setup_logging
from scraper_common.ids import read_ids
from scraper_common.runner import run_scrape
from scraper_common.storage import Storage
from scraper_common.storefront import REQUEST_TIMEOUT, Storefront

RETAILER = "dunnes"
STOREFRONT = Storefront(
    gateway="https://storefrontgateway.dunnesstoresgrocery.com",
    site="https://www.dunnesstoresgrocery.com",
    user_agents=[
        "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
        "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    ],
    store_listing_user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
)


async def scrape(cfg: RunConfig, storage: Storage) -> int:
    ids = read_ids(storage, RETAILER)
    stores = STOREFRONT.stores_for_chunk(STOREFRONT.fetch_store_ids(), cfg.chunk_id)
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        return await run_scrape(cfg, storage, ids, STOREFRONT.make_fetch_one(session, stores))


def main() -> int:
    setup_logging(RETAILER)
    cfg = RunConfig.from_env(RETAILER, max_not_found_rate=0.85)
    return asyncio.run(scrape(cfg, Storage(cfg.output_dir)))


if __name__ == "__main__":
    sys.exit(main())
