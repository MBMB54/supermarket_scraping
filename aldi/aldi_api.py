import asyncio
import random
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from scraper_common.config import RunConfig, setup_logging
from scraper_common.http import Response, request_with_retry
from scraper_common.ids import read_ids
from scraper_common.runner import run_scrape
from scraper_common.storage import Storage

RETAILER = "aldi"
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]


def get_headers() -> dict:
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


async def _get(session: AsyncSession, product_id: str) -> Response:
    try:
        resp = await session.get(
            f"https://api.aldi.ie/v2/products/{product_id}", headers=get_headers(), timeout=30
        )
    except RequestException as e:
        return Response(0, error=f"{type(e).__name__}: {e}")
    if resp.status_code == 200:
        try:
            return Response(200, data=resp.json().get("data"))
        except ValueError as e:
            return Response(0, error=f"invalid JSON: {e}")
    retry_after = resp.headers.get("Retry-After", "")
    return Response(
        resp.status_code,
        error=f"HTTP {resp.status_code}",
        retry_after=float(retry_after) if retry_after.isdigit() else None,
    )


def make_fetch_one(session: AsyncSession):
    async def fetch_one(product_id: str) -> dict:
        resp = await request_with_retry(lambda: _get(session, product_id))
        return {"product_id": product_id, "data": resp.data, "error": resp.error}

    return fetch_one


async def scrape(cfg: RunConfig, storage: Storage) -> int:
    ids = read_ids(storage, RETAILER)
    async with AsyncSession(impersonate="chrome120") as session:
        return await run_scrape(cfg, storage, ids, make_fetch_one(session))


def main() -> int:
    setup_logging(RETAILER)
    cfg = RunConfig.from_env(RETAILER)
    return asyncio.run(scrape(cfg, Storage(cfg.output_dir)))


if __name__ == "__main__":
    sys.exit(main())
