import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from scraper_common.config import RunConfig, chunk_slice
from scraper_common.quality import RunStats, gate_failures, rate
from scraper_common.storage import Storage

logger = logging.getLogger(__name__)

FetchOne = Callable[[str], Awaitable[dict]]


def checkpoint_key(cfg: RunConfig) -> str:
    return f"{cfg.day_prefix}/checkpoints/chunk_{cfg.chunk_id}.json"


def summary_key(cfg: RunConfig) -> str:
    return f"{cfg.day_prefix}/_run_summary_chunk{cfg.chunk_id}.json"


async def run_scrape(
    cfg: RunConfig,
    storage: Storage,
    all_ids: list[str],
    fetch_one: FetchOne,
    *,
    id_key: str = "product_id",
    is_retryable: Callable[[dict], bool] | None = None,
    concurrency: int = 5,
    request_delay: float = 0.5,
    batch_delay: float = 2.0,
    fail_on_retryable: bool = False,
) -> int:
    """Scrape this chunk's slice of `all_ids` and return the process exit code.

    `fetch_one(id)` must return the raw record (it should not raise for ordinary HTTP failures;
    put them in `error`). Records are written as gzipped JSONL batches under raw/{retailer}/{date}/,
    with a checkpoint so a retried job resumes. A record flagged by `is_retryable` is written but
    left out of the checkpoint, so a retry fetches it again (`fail_on_retryable` makes any such record
    fail the run so the retry actually happens). After the last batch the data-quality
    gate runs, a summary is logged and written to _run_summary_chunk{N}.json, and a non-zero code is
    returned (checkpoint kept) if the gate fails.
    """
    started = datetime.now(tz=UTC)
    t0 = time.monotonic()
    chunk_ids = chunk_slice(all_ids, cfg.chunk_id, cfg.total_chunks)
    if cfg.max_ids:
        chunk_ids = chunk_ids[: cfg.max_ids]
    planned = len(chunk_ids)

    checkpoint = storage.get_json(checkpoint_key(cfg)) or {}
    processed: list[str] = list(checkpoint.get("processed_ids", []))
    stats = RunStats.from_dict(checkpoint.get("stats", {}))
    done = set(processed)
    todo = [i for i in chunk_ids if i not in done]
    stats.resumed_skipped = planned - len(todo)
    logger.info(
        f"Chunk {cfg.chunk_id}/{cfg.total_chunks}: {len(todo)} to fetch of {planned} "
        f"({stats.resumed_skipped} already checkpointed)"
    )

    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(pid: str) -> dict:
        async with semaphore:
            if request_delay:
                await asyncio.sleep(request_delay)
            return await fetch_one(pid)

    for batch_num, i in enumerate(range(0, len(todo), cfg.batch_size)):
        batch = todo[i : i + cfg.batch_size]
        records = await asyncio.gather(*(bounded(pid) for pid in batch))
        name = (
            f"{cfg.retailer}_raw_{cfg.timestamp}_chunk{cfg.chunk_id}_batch{batch_num:04d}.jsonl.gz"
        )
        await asyncio.to_thread(storage.put_jsonl_gz, f"{cfg.day_prefix}/{name}", records)
        for record in records:
            retry = bool(is_retryable and is_retryable(record))
            stats.add(record, retryable=retry)
            if not retry:
                processed.append(record[id_key])
        await asyncio.to_thread(
            storage.put_json,
            checkpoint_key(cfg),
            {"processed_ids": processed, "stats": stats.to_dict()},
        )
        logger.info(f"Processed {min(i + cfg.batch_size, len(todo))}/{len(todo)} products")
        if i + cfg.batch_size < len(todo):
            await asyncio.sleep(batch_delay)

    failures = gate_failures(
        stats,
        max_error_rate=cfg.max_error_rate,
        max_empty_rate=cfg.max_empty_rate,
        max_not_found_rate=cfg.max_not_found_rate,
        fail_on_retryable=fail_on_retryable,
    )
    summary = {
        "retailer": cfg.retailer,
        "stage": "scrape",
        "run_date": cfg.run_date,
        "chunk_id": cfg.chunk_id,
        "total_chunks": cfg.total_chunks,
        "started_at": started.isoformat(),
        "duration_s": round(time.monotonic() - t0, 1),
        "planned_ids": planned,
        **stats.to_dict(),
        "error_rate": round(rate(stats.error + stats.retryable, stats.total), 4),
        "empty_rate": round(rate(stats.empty, stats.total), 4),
        "not_found_rate": round(rate(stats.not_found, stats.total), 4),
        "status": "failed" if failures else "ok",
        "failures": failures,
    }
    logger.info(json.dumps({"event": "scrape_summary", **summary}))
    storage.put_json(summary_key(cfg), summary)
    if failures:
        logger.error(f"Data-quality gate failed: {'; '.join(failures)}")
        return 1
    storage.delete(checkpoint_key(cfg))
    return 0
