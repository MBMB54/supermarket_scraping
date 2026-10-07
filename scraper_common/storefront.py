"""Shared client for the Musgrave-style storefront gateway used by SuperValu and Dunnes."""

import logging
import random
import uuid
from dataclasses import dataclass, field

import aiohttp
import requests

from scraper_common.http import Response, request_with_retry

logger = logging.getLogger(__name__)

NOT_FOUND = "Not found in any store"
MAX_CONSECUTIVE_STORE_ERRORS = 3
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)


@dataclass(frozen=True)
class Storefront:
    gateway: str  # e.g. https://storefrontgateway.supervalu.ie
    site: str  # e.g. https://shop.supervalu.ie
    user_agents: list[str]
    store_listing_user_agent: str
    extra_headers: dict[str, str] = field(default_factory=dict)
    stores_per_chunk: int = 60

    def store_listing_headers(self) -> dict:
        return {
            "User-Agent": self.store_listing_user_agent,
            "Accept": "application/json, text/plain, */*",
            "x-site-host": self.site,
            "x-site-location": "HeadersBuilderInterceptor",
            "x-correlation-id": str(uuid.uuid4()),
            "x-shopping-mode": "11111111-1111-1111-1111-111111111111",
            "x-customer-session-id": f"{self.site}|{uuid.uuid4()}",
            "Referer": f"{self.site}/",
        }

    def product_headers(self) -> dict:
        return {
            "User-Agent": random.choice(self.user_agents),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Origin": self.site,
            "Referer": f"{self.site}/",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            **self.extra_headers,
        }

    def fetch_store_ids(self) -> list:
        response = requests.get(
            f"{self.gateway}/api/stores", headers=self.store_listing_headers(), timeout=30
        )
        response.raise_for_status()
        ids = [store["retailerStoreId"] for store in response.json()["items"]]
        if not ids:
            raise RuntimeError(f"{self.gateway} returned no stores")
        logger.info(f"Loaded {len(ids)} store IDs")
        return ids

    def stores_for_chunk(self, store_ids: list, chunk_id: int) -> list:
        """Each chunk tries a different shuffled subset of stores for better coverage."""
        shuffled = store_ids[:]
        random.Random(chunk_id).shuffle(shuffled)
        return shuffled[: self.stores_per_chunk]

    async def _get(self, session: aiohttp.ClientSession, url: str) -> Response:
        try:
            async with session.get(url, headers=self.product_headers()) as resp:
                if resp.status == 200:
                    return Response(200, data=await resp.json(content_type=None))
                retry_after = resp.headers.get("Retry-After", "")
                return Response(
                    resp.status,
                    error=f"HTTP {resp.status}",
                    retry_after=float(retry_after) if retry_after.isdigit() else None,
                )
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            return Response(0, error=f"{type(e).__name__}: {e}")

    def make_fetch_one(self, session: aiohttp.ClientSession, store_ids: list):
        """Return fetch_one(product_id): first store that has the product wins.

        Transient failures are retried; a product is only reported as NOT_FOUND when store
        misses outnumber errors, otherwise the record carries the last error so the data-quality
        gate sees the outage instead of a pile of "stale" IDs.
        """

        async def fetch_one(product_id: str) -> dict:
            misses = errors = consecutive_errors = 0
            last_error = None
            for store_id in store_ids:
                url = f"{self.gateway}/api/stores/{store_id}/products/{product_id}"
                resp = await request_with_retry(lambda url=url: self._get(session, url))
                if resp.status == 200 and isinstance(resp.data, dict) and resp.data.get("name"):
                    return {
                        "product_id": product_id,
                        "store_id": store_id,
                        "data": resp.data,
                        "error": None,
                    }
                if resp.status in (200, 404):
                    misses += 1
                    consecutive_errors = 0
                else:
                    errors += 1
                    consecutive_errors += 1
                    last_error = resp.error
                    if consecutive_errors >= MAX_CONSECUTIVE_STORE_ERRORS:
                        break
            error = last_error if errors and errors >= misses else NOT_FOUND
            return {"product_id": product_id, "store_id": None, "data": None, "error": error}

        return fetch_one
