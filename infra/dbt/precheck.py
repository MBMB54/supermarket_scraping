"""Decide, per retailer, whether its scrape for a run_date has finished and passed its gate.

Each chunk writes `raw/{retailer}/{date}/_run_summary_chunk{N}.json` (see docs/scraper_data_quality.md)
carrying `chunk_id`, `total_chunks`, `status` and `failures`.
"""

from scraper_common.storage import Storage

READY = "ready"
NOT_READY = "not_ready"  # chunks still missing; a later slot may find it ready
FAILED = "failed"  # a chunk failed the scraper's own gate; waiting will not fix it


def classify(storage: Storage, run_date: str, retailers) -> dict[str, tuple[str, str]]:
    """{retailer: (READY | NOT_READY | FAILED, human-readable detail)}."""
    out = {}
    for retailer in retailers:
        prefix = f"raw/{retailer}/{run_date}/_run_summary_chunk"
        summaries = [storage.get_json(key) for key, _ in storage.list_keys(prefix)]
        if not summaries:
            out[retailer] = (NOT_READY, "no chunk summaries yet")
            continue
        bad = [f"chunk {s['chunk_id']}: {s['failures']}" for s in summaries if s["status"] != "ok"]
        if bad:
            out[retailer] = (FAILED, "scrape gate failed, " + "; ".join(bad))
            continue
        missing = set(range(summaries[0]["total_chunks"])) - {s["chunk_id"] for s in summaries}
        if missing:
            out[retailer] = (NOT_READY, f"chunks {sorted(missing)} not finished")
        else:
            out[retailer] = (READY, "ok")
    return out
