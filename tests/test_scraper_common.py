import asyncio
import gzip
import json
from unittest.mock import MagicMock

import polars as pl
import pytest
from botocore.exceptions import ClientError

from scraper_common.config import RunConfig, chunk_slice
from scraper_common.http import Response, request_with_retry, retry_until
from scraper_common.ids import IdsRejected, publish_ids
from scraper_common.quality import RunStats, gate_failures
from scraper_common.runner import checkpoint_key, run_scrape, summary_key
from scraper_common.storage import Storage

GATE = {"max_error_rate": 0.2, "max_empty_rate": 0.05, "max_not_found_rate": 0.6}


def make_cfg(tmp_path=None, **overrides) -> RunConfig:
    base = dict(
        retailer="demo",
        chunk_id=0,
        total_chunks=1,
        run_date="2026-01-02",
        timestamp="20260102_030405",
        output_dir=tmp_path,
        max_ids=None,
        batch_size=2,
        max_error_rate=0.2,
        max_empty_rate=0.05,
        max_not_found_rate=0.6,
    )
    return RunConfig(**{**base, **overrides})


def test_chunk_slice_covers_everything_without_overlap():
    items = list(range(23))
    parts = [chunk_slice(items, i, 5) for i in range(5)]
    assert sum(parts, []) == items
    assert max(map(len, parts)) - min(map(len, parts)) <= 1


def test_from_env_rejects_bad_chunk(monkeypatch):
    monkeypatch.setenv("CHUNK_ID", "5")
    monkeypatch.setenv("TOTAL_CHUNKS", "5")
    with pytest.raises(ValueError):
        RunConfig.from_env("demo")


def test_stats_classification():
    s = RunStats()
    s.add({"data": {"a": 1}, "error": None})
    s.add({"data": None, "error": None})
    s.add({"data": {}, "error": None})
    s.add({"data": None, "error": "HTTP 429"})
    s.add({"data": None, "error": "Not found in any store"})
    s.add({"data": None, "error": "HTTP 503"}, retryable=True)
    assert (s.ok, s.empty, s.error, s.not_found, s.retryable, s.total) == (1, 2, 1, 1, 1, 6)
    assert RunStats.from_dict(s.to_dict()).to_dict() == s.to_dict()


def test_gate_zero_rows_and_all_null():
    assert gate_failures(RunStats(), **GATE) == ["zero rows produced"]
    assert gate_failures(RunStats(resumed_skipped=10), **GATE) == []
    nulls = RunStats()
    for _ in range(10):
        nulls.add({"data": None, "error": None})
    assert any("empty/null" in r for r in gate_failures(nulls, **GATE))


def test_gate_passes_healthy_run():
    s = RunStats()
    for _ in range(95):
        s.add({"data": {"x": 1}, "error": None})
    for _ in range(5):
        s.add({"data": None, "error": "Not found in any store"})
    assert gate_failures(s, **GATE) == []


def test_request_with_retry_retries_then_gives_up(monkeypatch):
    monkeypatch.setattr("scraper_common.http.backoff_delay", lambda *a: 0)
    calls = []

    async def send():
        calls.append(1)
        return Response(429, error="HTTP 429")

    result = asyncio.run(request_with_retry(send, attempts=3))
    assert result.status == 429 and len(calls) == 3


def test_request_with_retry_does_not_retry_404():
    calls = []

    async def send():
        calls.append(1)
        return Response(404, error="HTTP 404")

    asyncio.run(request_with_retry(send))
    assert len(calls) == 1


def test_retry_until_returns_last_result(monkeypatch):
    monkeypatch.setattr("scraper_common.http.time.sleep", lambda _: None)
    assert retry_until(lambda: [], attempts=2) == []


def test_put_jsonl_gz_round_trip_s3_mock():
    storage = Storage()
    storage._client = MagicMock()
    storage.put_jsonl_gz("raw/x/a.jsonl.gz", [{"a": 1}, {"b": "é"}])
    kwargs = storage._client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == storage.bucket and kwargs["Key"] == "raw/x/a.jsonl.gz"
    lines = gzip.decompress(kwargs["Body"]).decode().splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": "é"}]


def test_get_json_missing_key_returns_none():
    storage = Storage()
    storage._client = MagicMock()
    storage._client.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "Get")
    assert storage.get_json("k") is None


def test_run_scrape_writes_batches_summary_and_clears_checkpoint(tmp_path):
    cfg = make_cfg(tmp_path)

    async def fetch_one(pid):
        return {"product_id": pid, "data": {"id": pid}, "error": None}

    code = asyncio.run(
        run_scrape(cfg, Storage(tmp_path), list("abcde"), fetch_one, request_delay=0, batch_delay=0)
    )
    assert code == 0
    files = sorted((tmp_path / cfg.day_prefix).glob("*.jsonl.gz"))
    assert [f.name for f in files] == [
        f"demo_raw_20260102_030405_chunk0_batch000{i}.jsonl.gz" for i in range(3)
    ]
    rows = [json.loads(line) for f in files for line in gzip.open(f, "rt")]
    assert [r["product_id"] for r in rows] == list("abcde")
    summary = json.loads((tmp_path / summary_key(cfg)).read_text())
    assert summary["ok"] == 5 and summary["status"] == "ok"
    assert not (tmp_path / checkpoint_key(cfg)).exists()
    assert not list((tmp_path / cfg.day_prefix).glob("_run_summary*.jsonl.gz"))


def test_run_scrape_fails_on_all_null_and_keeps_checkpoint(tmp_path):
    cfg = make_cfg(tmp_path)

    async def fetch_one(pid):
        return {"product_id": pid, "data": None, "error": None}

    code = asyncio.run(
        run_scrape(cfg, Storage(tmp_path), list("abcd"), fetch_one, request_delay=0, batch_delay=0)
    )
    assert code == 1
    summary = json.loads((tmp_path / summary_key(cfg)).read_text())
    assert summary["status"] == "failed" and summary["empty"] == 4
    assert (tmp_path / checkpoint_key(cfg)).exists()


def test_run_scrape_resumes_from_checkpoint(tmp_path):
    cfg = make_cfg(tmp_path)
    storage = Storage(tmp_path)
    storage.put_json(
        checkpoint_key(cfg),
        {"processed_ids": ["a", "b"], "stats": RunStats(total=2, ok=2).to_dict()},
    )
    seen = []

    async def fetch_one(pid):
        seen.append(pid)
        return {"product_id": pid, "data": {"id": pid}, "error": None}

    code = asyncio.run(
        run_scrape(cfg, storage, list("abcd"), fetch_one, request_delay=0, batch_delay=0)
    )
    assert code == 0 and seen == ["c", "d"]
    assert json.loads((tmp_path / summary_key(cfg)).read_text())["total"] == 4


def test_publish_ids_rejects_empty_and_shrunk(tmp_path):
    from datetime import UTC, datetime

    storage = Storage(tmp_path)
    now = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(IdsRejected):
        publish_ids(
            storage, "demo", pl.DataFrame({"product_id": []}, schema={"product_id": pl.String}), now
        )
    publish_ids(storage, "demo", pl.DataFrame({"product_id": [str(i) for i in range(10)]}), now)
    with pytest.raises(IdsRejected):
        publish_ids(storage, "demo", pl.DataFrame({"product_id": ["1", "2"]}), now)
    latest = tmp_path / "raw/demo/ids/latest/demo_product_ids.parquet"
    assert pl.read_parquet(latest).height == 10


def _stats(ok=0, tooMany=0, silent=0, not_found=0):
    s = RunStats()
    for _ in range(ok):
        s.add({"tpnc": "1", "data": {"id": 1}, "error": None})
    for _ in range(tooMany):
        s.add({"tpnc": "1", "data": None, "error": "HTTP 429"})
    for _ in range(silent):
        s.add({"tpnc": "1", "data": None, "error": None})
    for _ in range(not_found):
        s.add({"tpnc": "1", "data": None, "error": "No product returned"})
    return s


def test_gate_fails_tesco_429_incident_shape():
    # docs/tesco-429-incident.md, 2026-09-01: 19,952 rows, 5,941 ok, 11,145 429, 2,866 silent
    failures = gate_failures(_stats(ok=5941, tooMany=11145, silent=2866), **GATE)
    assert any("error rate" in f for f in failures)
    assert any("empty/null" in f for f in failures)


def test_gate_fails_tesco_all_null():
    assert gate_failures(_stats(silent=500), **GATE)


def test_gate_passes_tesco_healthy_shape():
    # 2026-08-08: 19,952 rows, 19,951 ok, 1 silent
    assert gate_failures(_stats(ok=19951, silent=1), **GATE) == []
    assert gate_failures(_stats(ok=19900, not_found=52), **GATE) == []


def test_fail_on_retryable_keeps_checkpoint(tmp_path):
    cfg = make_cfg(tmp_path)
    soft = {"c"}

    async def fetch_one(pid):
        if pid in soft:
            return {"tpnc": pid, "data": None, "error": "HTTP 429"}
        return {"tpnc": pid, "data": {"id": pid}, "error": None}

    code = asyncio.run(
        run_scrape(
            cfg,
            Storage(tmp_path),
            list("abcdefghijklmnopqrst"),
            fetch_one,
            id_key="tpnc",
            is_retryable=lambda r: r["tpnc"] in soft,
            request_delay=0,
            batch_delay=0,
            fail_on_retryable=True,
        )
    )
    assert code == 1
    processed = json.loads((tmp_path / checkpoint_key(cfg)).read_text())["processed_ids"]
    assert "c" not in processed and len(processed) == 19


def test_run_ids_job_fails_when_latest_is_stale(tmp_path, monkeypatch):
    import os
    import time

    from scraper_common import ids
    from scraper_common.ids import latest_key, run_ids_job

    monkeypatch.setattr(ids, "MAX_STALE_DAYS", 14)

    storage = Storage(tmp_path)
    latest = tmp_path / latest_key("demo")
    latest.parent.mkdir(parents=True)
    pl.DataFrame({"product_id": ["1"]}).write_parquet(latest)
    old = time.time() - 30 * 86400
    os.utime(latest, (old, old))
    empty = lambda: pl.DataFrame({"product_id": []}, schema={"product_id": pl.String})  # noqa: E731
    assert run_ids_job("demo", empty, storage, min_age_hours=1) == 1
    os.utime(latest, None)
    assert run_ids_job("demo", empty, storage, min_age_hours=0) == 0
