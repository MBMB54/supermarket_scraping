import asyncio
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import aiohttp

from scraper_common.config import RunConfig, setup_logging
from scraper_common.ids import read_ids
from scraper_common.runner import run_scrape
from scraper_common.storage import Storage

logger = setup_logging("tesco_api")

TESCO_GRAPHQL_QUERY = (Path(__file__).parent / "graphql_query.txt").read_text().rstrip()

RETAILER = "tesco"
# Tesco/Akamai rate-limits xapi.tesco.com (see docs/tesco-429-incident.md): the measured knee is
# ~2.5 req/s. Budget is an aggregate across ALL chunks; each container takes an equal share.
TOTAL_REQ_PER_SEC = float(os.environ.get("TESCO_TOTAL_REQ_PER_SEC", "2.0"))
CONCURRENT_REQUESTS = 2  # in-flight cap only; pacing is done by RequestPacer
MAX_ATTEMPTS = int(os.environ.get("TESCO_MAX_ATTEMPTS", "8"))
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)
API_KEY = "TvOSZJHlEk0pjniDGQFAc9Q59WGAR4dA"
USER_AGENT_STRINGS = [
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
]


class RequestPacer:
    """Even request pacing (token-bucket with burst 1) plus a shared penalty on throttling."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        if slot > now:
            await asyncio.sleep(slot - now)

    async def penalise(self, seconds: float) -> None:
        """Push every future slot back so all workers cool off when Tesco throttles."""
        async with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


class Throttled(Exception):
    def __init__(self, label: str, retry_after: float | None = None):
        super().__init__(label)
        self.label = label
        self.retry_after = retry_after


class Unrecoverable(Exception):
    pass


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


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


def parse_body(body) -> dict | None:
    """Return the product dict, or raise Throttled / Unrecoverable for rejections.

    Tesco sometimes rejects at the GraphQL layer with HTTP 200 and an `errors` body
    (e.g. 'Too many requests') and no `data` key - that must never look like an empty product.
    """
    item = body[0] if isinstance(body, list) and body else body
    if not isinstance(item, dict):
        raise Throttled("GraphQL malformed response")
    errors = item.get("errors")
    if errors or "data" not in item or item["data"] is None:
        text = json.dumps(errors)[:300] if errors else "missing data"
        if "not-found" in text.lower() or '"status": 404' in text:
            raise Unrecoverable(f"GraphQL error: {text}")
        throttled = "too many" in text.lower() or "429" in text
        raise Throttled("GraphQL 429" if throttled else f"GraphQL error: {text}")
    product = item["data"].get("product")
    if not product:
        raise Unrecoverable("No product returned")
    return product


async def _attempt(session: aiohttp.ClientSession, tpnc: str) -> dict:
    async with session.post(
        "https://xapi.tesco.com/", headers=get_headers(), json=build_payload(tpnc)
    ) as response:
        if response.status == 200:
            return parse_body(await response.json(content_type=None))
        if response.status == 429 or response.status >= 500:
            raise Throttled(f"HTTP {response.status}", _retry_after(response))
        raise Unrecoverable(f"HTTP {response.status}")


async def fetch_product(
    session: aiohttp.ClientSession,
    tpnc: str,
    pacer: RequestPacer,
    stats: dict | None = None,
) -> tuple[dict, bool]:
    """Fetch one product. Returns (record, retryable_failure).

    Throttle/transient failures are retried with Retry-After + exponential backoff + jitter.
    If still failing, the record carries an error (never null/null) and retryable_failure is
    True so the caller does not checkpoint it.
    """
    stats = stats if stats is not None else {}
    last_error = "unknown"
    for attempt in range(MAX_ATTEMPTS):
        await pacer.wait()
        stats["requests"] = stats.get("requests", 0) + 1
        try:
            product = await _attempt(session, tpnc)
            return {"tpnc": tpnc, "data": product, "error": None}, False
        except Unrecoverable as e:
            return {"tpnc": tpnc, "data": None, "error": str(e)}, False
        except Throttled as e:
            last_error = e.label
            retry_after = e.retry_after
        except (aiohttp.ClientError, TimeoutError) as e:
            last_error = f"{type(e).__name__}: {e}"[:200]
            retry_after = None
        stats["throttled"] = stats.get("throttled", 0) + 1
        if attempt == MAX_ATTEMPTS - 1:
            break
        backoff = min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt)
        delay = max(retry_after or 0, backoff) + random.uniform(0, backoff / 2)
        await pacer.penalise(delay)
        stats["retries"] = stats.get("retries", 0) + 1
        await asyncio.sleep(delay)
    return {"tpnc": tpnc, "data": None, "error": last_error}, True


async def scrape(cfg: RunConfig, storage: Storage) -> int:
    ids = read_ids(storage, RETAILER, column="id")
    pacer = RequestPacer(TOTAL_REQ_PER_SEC / cfg.total_chunks)
    logger.info(f"Pacing at {TOTAL_REQ_PER_SEC / cfg.total_chunks:.3f} req/s for this chunk")
    stats: dict = {}
    retryable: set[str] = set()
    connector = aiohttp.TCPConnector(limit=CONCURRENT_REQUESTS, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector, timeout=TIMEOUT) as session:

        async def fetch_one(tpnc: str) -> dict:
            record, soft = await fetch_product(session, tpnc, pacer, stats)
            if soft:
                retryable.add(tpnc)
            return record

        code = await run_scrape(
            cfg,
            storage,
            ids,
            fetch_one,
            id_key="tpnc",
            is_retryable=lambda record: record["tpnc"] in retryable,
            concurrency=CONCURRENT_REQUESTS,
            request_delay=0,
            batch_delay=0,
            fail_on_retryable=True,
        )
    logger.info(f"Request stats: {stats}")
    return code


def main() -> int:
    cfg = RunConfig.from_env(RETAILER)
    return asyncio.run(scrape(cfg, Storage(cfg.output_dir)))


if __name__ == "__main__":
    sys.exit(main())
