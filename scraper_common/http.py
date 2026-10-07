import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class Response:
    """Client-agnostic result. status 0 means a transport error (timeout, reset, ...)."""

    status: int
    data: Any = None
    error: str | None = None
    retry_after: float | None = None

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status in RETRYABLE_STATUS


def backoff_delay(attempt: int, base: float, cap: float, retry_after: float | None) -> float:
    delay = min(cap, base * 2**attempt)
    if retry_after:
        delay = max(delay, min(retry_after, cap))
    return delay + random.uniform(0, base)


async def request_with_retry(
    send: Callable[[], Awaitable[Response]],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Response:
    """Call `send` until it returns a non-retryable response or attempts are exhausted."""
    for attempt in range(attempts):
        response = await send()
        if not response.retryable or attempt == attempts - 1:
            return response
        await asyncio.sleep(backoff_delay(attempt, base_delay, max_delay, response.retry_after))
    raise AssertionError("unreachable")


def retry_until[T](
    fn: Callable[[], T], *, attempts: int = 3, wait: float = 10.0, ok: Callable[[T], bool] = bool
) -> T:
    """Sync retry for ID discovery: re-run `fn` until `ok(result)`; return the last result."""
    for attempt in range(attempts):
        result = fn()
        if ok(result) or attempt == attempts - 1:
            return result
        time.sleep(wait * 2**attempt)
    raise AssertionError("unreachable")
