import gzip
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra" / "dbt"))

import compact_raw  # noqa: E402
import lake  # noqa: E402
import landing  # noqa: E402
import precheck  # noqa: E402
import seed_lake  # noqa: E402
from common import connect  # noqa: E402

from scraper_common.storage import Storage  # noqa: E402

PREFIX = "raw/demo/2026-01-01/"


def write_batches(storage: Storage, prefix: str, sizes: list[int]) -> int:
    n = 0
    for i, size in enumerate(sizes):
        records = [
            {"product_id": f"{i}-{j}", "data": {"price": j}, "error": None} for j in range(size)
        ]
        storage.put_jsonl_gz(f"{prefix}demo_raw_chunk0_batch{i:04d}.jsonl.gz", records)
        n += size
    storage.put_json(f"{prefix}_run_summary_chunk0.json", {"status": "ok"})
    storage.put_json(f"{prefix}checkpoints/chunk_0.json", {"done": n})
    return n


def test_put_if_absent_is_a_lock(tmp_path):
    s = Storage(tmp_path)
    assert s.put_if_absent("dbt/LOCK", b"a") is True
    assert s.put_if_absent("dbt/LOCK", b"b") is False
    assert s.get_bytes("dbt/LOCK") == b"a"


def test_list_keys_and_delete_many(tmp_path):
    s = Storage(tmp_path)
    write_batches(s, PREFIX, [2, 3])
    keys = dict(s.list_keys(PREFIX))
    assert f"{PREFIX}checkpoints/chunk_0.json" in keys and len(keys) == 4
    assert s.list_keys("raw/other/") == []
    s.delete_many([k for k in keys if k.endswith(".jsonl.gz")])
    assert [k for k, _ in s.list_keys(PREFIX) if k.endswith(".jsonl.gz")] == []


def test_compact_merges_and_keeps_summaries(tmp_path):
    s = Storage(tmp_path)
    total = write_batches(s, PREFIX, [2, 3, 4])
    assert compact_raw.compact_partition(s, PREFIX).startswith("compacted")
    keys = [k for k, _ in s.list_keys(PREFIX)]
    assert f"{PREFIX}compacted.jsonl.gz" in keys
    assert not [k for k in keys if "batch" in k]
    assert (
        f"{PREFIX}_run_summary_chunk0.json" in keys and f"{PREFIX}checkpoints/chunk_0.json" in keys
    )
    assert compact_raw.count_rows(s.get_bytes(f"{PREFIX}compacted.jsonl.gz")) == total
    # dbt/landing read it through the same glob as the originals
    rows = connect().execute(
        f"SELECT count(*) FROM read_json('{tmp_path}/{PREFIX}*.jsonl.gz', union_by_name=true)"
    )
    assert rows.fetchone()[0] == total
    assert compact_raw.compact_partition(s, PREFIX) == "nothing to do"


def test_compact_never_remerges_an_existing_compacted_object(tmp_path):
    s = Storage(tmp_path)
    total = write_batches(s, PREFIX, [2, 3])
    batches = [k for k, _ in s.list_keys(PREFIX) if k.endswith(".jsonl.gz")]
    s.put_bytes(f"{PREFIX}compacted.jsonl.gz", b"".join(s.get_bytes(k) for k in sorted(batches)))
    # crashed after upload, before delete: finish the delete, rows stay exact
    assert compact_raw.compact_partition(s, PREFIX).startswith("finished deletion")
    assert compact_raw.count_rows(s.get_bytes(f"{PREFIX}compacted.jsonl.gz")) == total


def test_compact_refuses_to_delete_when_counts_disagree(tmp_path):
    s = Storage(tmp_path)
    write_batches(s, PREFIX, [2, 3])
    s.put_bytes(f"{PREFIX}compacted.jsonl.gz", gzip.compress(b'{"a": 1}\n'))
    with pytest.raises(RuntimeError, match="rows vs"):
        compact_raw.compact_partition(s, PREFIX)
    assert len([k for k, _ in s.list_keys(PREFIX) if "batch" in k]) == 2


def test_compact_dry_run_changes_nothing(tmp_path):
    s = Storage(tmp_path)
    write_batches(s, PREFIX, [2, 3])
    before = s.list_keys(PREFIX)
    assert compact_raw.compact_partition(s, PREFIX, dry_run=True).startswith("would merge 2")
    assert s.list_keys(PREFIX) == before


def test_compact_only_old_dates_and_skips_ids_dirs(tmp_path):
    s = Storage(tmp_path)
    write_batches(s, "raw/demo/2026-01-01/", [2])
    write_batches(s, "raw/demo/2026-01-09/", [2])
    s.put_bytes("raw/demo/ids/latest/demo_product_ids.parquet", b"x")
    out = compact_raw.compact(s, ["demo"], date(2026, 1, 8))
    assert list(out) == ["raw/demo/2026-01-01/"]
    assert any("batch" in k for k, _ in s.list_keys("raw/demo/2026-01-09/"))
    assert s.get_bytes("raw/demo/ids/latest/demo_product_ids.parquet") == b"x"


def test_landing_writes_one_parquet_partition_per_retailer_day(tmp_path):
    s = Storage(tmp_path / "raw_root")
    total = write_batches(s, "raw/demo/2026-01-01/", [2, 3])
    # an attribute key that appears in only one file must not break the read (union_by_name)
    s.put_bytes(
        "raw/demo/2026-01-01/extra.jsonl.gz",
        gzip.compress((json.dumps({"product_id": "x", "data": {"newKey": 1}}) + "\n").encode()),
    )
    con = connect()
    rows = landing.land(
        con, "demo", "2026-01-01", str(tmp_path / "raw_root" / "raw"), str(tmp_path / "landing")
    )
    assert rows == total + 1
    parquet = tmp_path / "landing" / "demo" / "scraped_date=2026-01-01" / "data.parquet"
    assert parquet.exists()


def test_landing_fails_loudly_when_the_partition_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="no raw batches"):
        landing.land(connect(), "demo", "2026-01-01", str(tmp_path / "raw"), str(tmp_path / "out"))


def make_summary(storage, retailer, day, chunk, total, status="ok"):
    storage.put_json(
        f"raw/{retailer}/{day}/_run_summary_chunk{chunk}.json",
        {"chunk_id": chunk, "total_chunks": total, "status": status, "failures": []},
    )


def test_precheck_classifies_each_retailer_independently(tmp_path):
    s = Storage(tmp_path)
    for chunk in range(2):
        make_summary(s, "a", "2026-01-01", chunk, 2)
    make_summary(s, "b", "2026-01-01", 0, 2)
    make_summary(s, "c", "2026-01-01", 0, 1, status="failed")
    got = precheck.classify(s, "2026-01-01", ["a", "b", "c", "d"])
    assert got["a"] == (precheck.READY, "ok")
    assert got["b"] == (precheck.NOT_READY, "chunks [1] not finished")
    assert got["c"][0] == precheck.FAILED and "scrape gate failed" in got["c"][1]
    assert got["d"] == (precheck.NOT_READY, "no chunk summaries yet")


def test_lock_blocks_a_second_run_but_breaks_a_stale_one(tmp_path, monkeypatch):
    s = Storage(tmp_path)
    lake.acquire(s, "2026-01-01")
    with pytest.raises(lake.LockHeld, match="locked by run"):
        lake.acquire(s, "2026-01-02")
    monkeypatch.setattr(lake, "LOCK_MAX_AGE_HOURS", 0)
    lake.acquire(s, "2026-01-02")  # stale: taken over
    assert s.get_bytes(lake.LOCK_KEY) == b"2026-01-02"
    lake.release(s)
    assert s.get_bytes(lake.LOCK_KEY) is None


def test_pull_push_round_trip_and_dated_archive(tmp_path):
    s = Storage(tmp_path / "store")
    local = tmp_path / "cat.ducklake"
    assert lake.pull(s, local) is False
    local.write_bytes(b"catalog-v1")
    lake.push(s, local, "2026-01-01")
    local.unlink()
    assert lake.pull(s, local) is True and local.read_bytes() == b"catalog-v1"
    assert s.get_bytes(f"{lake.STATE_PREFIX}/archive/2026-01-01/catalog.ducklake") == b"catalog-v1"


def test_seed_copies_history_and_refuses_to_overwrite(tmp_path):
    import duckdb

    db = tmp_path / "local.db"
    c = duckdb.connect(str(db))
    c.execute(
        "CREATE TABLE fct_product_price_daily AS SELECT 'tesco' supermarket, 'p1' id, "
        "DATE '2026-01-01' scraped_date, 1.5 price"
    )
    c.close()
    store = Storage(tmp_path / "store")
    data = str(tmp_path / "lake_data") + "/"
    assert seed_lake.seed(store, db, tmp_path / "seed.ducklake", data) == 1
    assert store.get_bytes(lake.LOCK_KEY) is None  # released
    with pytest.raises(SystemExit, match="refusing"):
        seed_lake.seed(store, db, tmp_path / "seed2.ducklake", data)
    # the pushed catalog opens elsewhere and has the rows
    pulled = tmp_path / "pulled.ducklake"
    lake.pull(store, pulled)
    con = connect()
    con.execute("INSTALL ducklake; LOAD ducklake")
    con.execute(f"ATTACH 'ducklake:{pulled}' AS lake (DATA_PATH '{data}')")
    assert con.execute("SELECT price FROM lake.main.fct_product_price_daily").fetchone() == (1.5,)


def test_maintain_expires_and_cleans_up(tmp_path):
    cat, data = tmp_path / "m.ducklake", str(tmp_path / "md") + "/"
    con = connect()
    con.execute("INSTALL ducklake; LOAD ducklake")
    con.execute(f"ATTACH 'ducklake:{cat}' AS lake (DATA_PATH '{data}')")
    con.execute("CREATE TABLE lake.t AS SELECT 1 a")
    con.execute("INSERT INTO lake.t VALUES (2)")
    con.close()
    lake.maintain(cat, data, retention_days=0)
    con = connect()
    con.execute("INSTALL ducklake; LOAD ducklake")
    con.execute(f"ATTACH 'ducklake:{cat}' AS lake (DATA_PATH '{data}')")
    assert con.execute("SELECT count(*) FROM lake.t").fetchone() == (2,)


def test_compact_only_date(tmp_path):
    s = Storage(tmp_path)
    write_batches(s, "raw/demo/2026-01-01/", [2])
    write_batches(s, "raw/demo/2026-01-02/", [2])
    out = compact_raw.compact(s, ["demo"], date(2026, 1, 8), only_date="2026-01-02")
    assert list(out) == ["raw/demo/2026-01-02/"]
    assert any("batch" in k for k, _ in s.list_keys("raw/demo/2026-01-01/"))


def test_final_slot_from_scheduled_hour(monkeypatch):
    import run_dbt

    monkeypatch.delenv("FINAL_ATTEMPT", raising=False)
    monkeypatch.setenv("SCHEDULED_TIME", "2026-01-01T13:30:00Z")
    assert run_dbt.is_final_slot() is False
    monkeypatch.setenv("SCHEDULED_TIME", "2026-01-01T14:30:00Z")
    assert run_dbt.is_final_slot() is True
    monkeypatch.delenv("SCHEDULED_TIME")
    assert run_dbt.is_final_slot() is False
    monkeypatch.setenv("FINAL_ATTEMPT", "1")
    assert run_dbt.is_final_slot() is True
    assert (
        run_dbt.resolve_run_date.__name__
    )  # imported without side effects (needs RUN_DATE only when called)


def test_compact_floor_bounds_the_window(tmp_path):
    s = Storage(tmp_path)
    for day in ("2026-01-01", "2026-01-05"):
        write_batches(s, f"raw/demo/{day}/", [2])
    out = compact_raw.compact(s, ["demo"], date(2026, 1, 8), floor=date(2026, 1, 4))
    assert list(out) == ["raw/demo/2026-01-05/"]
    assert any("batch" in k for k, _ in s.list_keys("raw/demo/2026-01-01/"))


def run_main(
    monkeypatch, tmp_path, ready=("tesco", "aldi", "dunnes", "supervalu"), allow_fresh=True
):
    """Run main() with dbt/landing faked; `ready` retailers get ok summaries for 2026-01-01."""
    import run_dbt

    store = Storage(tmp_path / "store")
    for retailer in ready:
        make_summary(store, retailer, "2026-01-01", 0, 1)
    catalog = tmp_path / "local.ducklake"
    calls = []

    def fake_dbt(run_date, retailers):
        calls.append(list(retailers))
        catalog.write_bytes(b"built")
        return 0

    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("RUN_DATE", "2026-01-01")
    monkeypatch.setenv("MAINTAIN", "0")
    monkeypatch.delenv("FINAL_ATTEMPT", raising=False)
    monkeypatch.delenv("SCHEDULED_TIME", raising=False)
    monkeypatch.setattr(run_dbt, "LOCAL_CATALOG", catalog)
    monkeypatch.setattr(run_dbt, "run_dbt", fake_dbt)
    monkeypatch.setattr(run_dbt, "land_all", lambda run_date, retailers: {})
    monkeypatch.setattr(run_dbt, "warnings", lambda path: [])
    alerts = []
    monkeypatch.setattr(run_dbt, "alert", lambda subject, body: alerts.append((subject, body)))
    if allow_fresh:
        monkeypatch.setenv("ALLOW_FRESH_LAKE", "1")
    return run_dbt, store, calls, alerts


def test_main_refuses_to_start_a_lake_from_nothing(monkeypatch, tmp_path):
    run_dbt, store, calls, alerts = run_main(monkeypatch, tmp_path, allow_fresh=False)
    assert run_dbt.main() == 1
    assert calls == []
    assert "no catalog" in alerts[0][0]
    assert store.get_bytes(lake.LOCK_KEY) is None  # lock released
    assert store.get_bytes(lake.CATALOG_KEY) is None


def test_main_builds_pushes_marks_and_is_idempotent(monkeypatch, tmp_path):
    run_dbt, store, calls, alerts = run_main(monkeypatch, tmp_path)
    assert run_dbt.main() == 0
    assert calls == [["tesco", "aldi", "dunnes", "supervalu"]]
    assert store.get_bytes(lake.CATALOG_KEY) == b"built"
    for retailer in run_dbt.RETAILERS:
        assert store.get_bytes(lake.built_key("2026-01-01", retailer)) == b""
    assert store.get_bytes(lake.LOCK_KEY) is None
    assert alerts[0][0] == "dbt OK for 2026-01-01"
    assert run_dbt.main() == 0 and len(calls) == 1  # nothing new: dbt not rerun


def test_main_does_not_push_when_dbt_fails(monkeypatch, tmp_path):
    run_dbt, store, calls, alerts = run_main(monkeypatch, tmp_path)
    monkeypatch.setattr(run_dbt, "run_dbt", lambda run_date, retailers: 1)
    assert run_dbt.main() == 1
    assert store.get_bytes(lake.CATALOG_KEY) is None
    assert store.get_bytes(lake.built_key("2026-01-01", "tesco")) is None
    assert store.get_bytes(lake.LOCK_KEY) is None


def test_main_waits_quietly_until_the_final_slot_when_nothing_is_ready(monkeypatch, tmp_path):
    run_dbt, store, calls, alerts = run_main(monkeypatch, tmp_path, ready=())
    assert run_dbt.main() == 0 and calls == [] and alerts == []
    monkeypatch.setenv("FINAL_ATTEMPT", "1")
    assert run_dbt.main() == 1 and calls == [] and "no retailer ready" in alerts[0][0]


def test_main_publishes_ready_retailers_then_adds_the_late_one(monkeypatch, tmp_path):
    run_dbt, store, calls, alerts = run_main(monkeypatch, tmp_path, ready=("aldi", "dunnes"))
    assert run_dbt.main() == 0
    assert calls == [["aldi", "dunnes"]]  # tesco/supervalu missing: not blocked
    assert "DEGRADED" in alerts[0][0] and "tesco: no chunk summaries yet" in alerts[0][1]
    assert store.get_bytes(lake.built_key("2026-01-01", "tesco")) is None
    assert run_dbt.main() == 0 and len(calls) == 1  # nothing new, not final: quiet
    for retailer in ("tesco", "supervalu"):  # late scrapes finish
        make_summary(store, retailer, "2026-01-01", 0, 1)
    assert run_dbt.main() == 0
    assert calls[1] == ["tesco", "aldi", "dunnes", "supervalu"]  # rebuilt with the full day
    assert alerts[-1][0] == "dbt OK for 2026-01-01"


def test_main_reports_a_failed_retailer_once_at_the_final_slot(monkeypatch, tmp_path):
    run_dbt, store, calls, alerts = run_main(
        monkeypatch, tmp_path, ready=("aldi", "dunnes", "supervalu")
    )
    make_summary(store, "tesco", "2026-01-01", 0, 1, status="failed")
    assert run_dbt.main() == 0 and calls == [["aldi", "dunnes", "supervalu"]]
    assert "scrape gate failed" in alerts[0][1]
    monkeypatch.setenv("FINAL_ATTEMPT", "1")
    assert run_dbt.main() == 0 and len(calls) == 1  # no rebuild
    assert "dbt DEGRADED" in alerts[-1][0]


def test_plan_decisions():
    import run_dbt

    ok, wait, bad = (precheck.READY, "ok"), (precheck.NOT_READY, "x"), (precheck.FAILED, "x")
    names = run_dbt.RETAILERS
    all_ok = dict.fromkeys(names, ok)
    assert run_dbt.plan(all_ok, [], False) == "build"
    assert run_dbt.plan(all_ok, list(names), False) == "done"
    assert run_dbt.plan(dict.fromkeys(names, wait), [], False) == "wait"
    assert run_dbt.plan(dict.fromkeys(names, wait), [], True) == "fail"
    assert run_dbt.plan(dict.fromkeys(names, bad), [], False) == "fail"  # waiting cannot help
    assert run_dbt.plan({**all_ok, "tesco": bad}, [], False) == "build"
    assert run_dbt.plan({**all_ok, "tesco": bad}, ["aldi", "dunnes", "supervalu"], False) == "done"


def test_dbt_command_skips_absent_retailers_and_the_catalogue_export(monkeypatch):
    import run_dbt

    seen = {}
    monkeypatch.setattr(run_dbt.subprocess, "call", lambda cmd, env: seen.update(cmd=cmd) or 0)
    run_dbt.run_dbt("2026-01-01", ["aldi", "dunnes"])
    cmd = seen["cmd"]
    exclude = cmd[cmd.index("--exclude") + 1 : cmd.index("--vars")]
    assert (
        "source:external_source.tesco+" in exclude and "source:external_source.aldi+" not in exclude
    )
    assert "int_all_supermarket_products" in exclude
    dbt_vars = json.loads(cmd[cmd.index("--vars") + 1])
    assert dbt_vars["retailers"] == ["aldi", "dunnes"] and dbt_vars["listing_min_rows"] == 4500
    run_dbt.run_dbt("2026-01-01", list(run_dbt.RETAILERS))
    cmd = seen["cmd"]
    assert "int_all_supermarket_products" not in cmd and "listing_min_rows" not in cmd[-1]
