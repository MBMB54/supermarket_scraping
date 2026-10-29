import json
import os
import re
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra" / "embed"))

import run_embed  # noqa: E402

from ml_common import table  # noqa: E402
from ml_common.embedders import EmbedResult  # noqa: E402
from ml_common.specs import get_spec  # noqa: E402
from ml_common.storage import Storage as StoreStorage  # noqa: E402
from scraper_common.storage import Storage  # noqa: E402

DAY = "2026-01-01"


class FakeEmbedder:
    def __init__(self, fail=lambda text: False):
        self.fail, self.encoded = fail, []

    def encode(self, inputs):
        self.encoded += [i.text for i in inputs]
        return [
            EmbedResult(None, "boom") if self.fail(i.text) else EmbedResult([1.0, 0.0])
            for i in inputs
        ]


class Factory:
    """Stands in for load_embedder: counts model loads, hands out one fake per spec."""

    def __init__(self, **kwargs):
        self.kwargs, self.loads, self.embedders = kwargs, [], {}

    def __call__(self, spec):
        self.loads.append(spec.name)
        self.embedders[spec.name] = FakeEmbedder(**self.kwargs)
        return self.embedders[spec.name]


def write_catalogue(path, titles):
    pl.DataFrame(
        {
            "id": [str(i) for i in range(len(titles))],
            "supermarket": ["tesco"] * len(titles),
            "title": titles,
            "title_cleaned": titles,
            "image_url": [None] * len(titles),
        }
    ).write_parquet(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = tmp_path / "ml"
    catalogue = tmp_path / "all_supermarket_products.parquet"
    write_catalogue(catalogue, ["milk", "bread", "eggs"])
    monkeypatch.setenv("EMBEDDINGS_STORE", str(store))
    monkeypatch.setenv("CATALOGUE_URI", str(catalogue))
    monkeypatch.setenv("RUN_DATE", DAY)
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    monkeypatch.delenv("EMBED_LIMIT", raising=False)
    alerts = []
    monkeypatch.setattr(run_embed, "alert", lambda subject, body: alerts.append((subject, body)))
    return store, catalogue, alerts


def summary(store, spec, day=DAY):
    return json.loads((store / "_run_summary" / f"{day}_{spec}.json").read_text())


def test_embeds_new_rows_for_both_specs_and_alerts_once(env):
    store, _, alerts = env
    factory = Factory()
    assert run_embed.main(factory) == 0
    assert factory.loads == ["gemma2-text", "harrier-title"]
    for name in ("gemma2-text", "harrier-title"):
        done = table.read_done(StoreStorage(str(store)), get_spec(name))
        assert len(done) == 3
        s = summary(store, name)
        assert s["status"] == "ok" and s["embedded"] == 3 and s["model_loaded"] is True
        assert s["spec_id"] == get_spec(name).spec_id and s["peak_rss_mb"] > 0
    assert [a[0] for a in alerts] == [f"embed OK for {DAY}"]
    assert not (store / "_state" / "LOCK").exists()


def test_a_run_with_nothing_new_never_loads_a_model(env):
    store, catalogue, alerts = env
    assert run_embed.main(Factory()) == 0
    alerts.clear()
    factory = Factory()
    assert run_embed.main(factory) == 0  # the dbt job's quiet slots trigger exactly this
    assert factory.loads == []
    assert alerts == []
    s = summary(store, "gemma2-text")
    assert s["pending"] == 0 and s["embedded"] == 0 and s["model_loaded"] is False
    assert s["runs_today"] == 2 and s["embedded_today"] == 3  # the first run's work is kept


def test_only_new_and_changed_titles_embed(env):
    store, catalogue, _ = env
    run_embed.main(Factory())
    write_catalogue(
        catalogue, ["milk", "bread", "free range eggs", "butter"]
    )  # id 2 changed, 3 new
    factory = Factory()
    assert run_embed.main(factory) == 0
    assert factory.embedders["gemma2-text"].encoded == ["free range eggs", "butter"]
    assert summary(store, "harrier-title")["embedded_today"] == 5


def test_a_held_lock_exits_quietly_without_work(env):
    store, _, alerts = env
    Storage(store).put_bytes("_state/LOCK", b"other-job")
    factory = Factory()
    assert run_embed.main(factory) == 0
    assert factory.loads == [] and alerts == []
    assert not (store / "_run_summary").exists()
    assert (store / "_state" / "LOCK").read_bytes() == b"other-job"  # not released by us


def test_a_stale_lock_is_taken_over_and_released(env):
    store, _, _ = env
    s = Storage(store)
    lock = run_embed.StoreLock(s, "_state/LOCK", "dead-job", max_age_minutes=30)
    assert lock.acquire()
    old = (datetime.now(UTC) - timedelta(minutes=45)).timestamp()
    os.utime(store / "_state" / "LOCK", (old, old))
    mine = run_embed.StoreLock(s, "_state/LOCK", "new-job", max_age_minutes=30)
    assert mine.acquire()
    lock.release()  # the dead job cannot delete a lock it no longer holds
    assert (store / "_state" / "LOCK").exists()
    mine.release()
    assert not (store / "_state" / "LOCK").exists()


def test_the_lock_is_refreshed_before_each_encode(env, monkeypatch):
    touches = []
    monkeypatch.setattr(run_embed.StoreLock, "touch", lambda self: touches.append(1))
    monkeypatch.setenv("CHUNK_ROWS", "1")
    run_embed.main(Factory())
    assert len(touches) == 6  # 3 chunks x 2 specs


def test_failures_over_the_threshold_fail_the_run_but_not_the_other_spec(env, monkeypatch):
    store, _, alerts = env
    monkeypatch.setenv("EMBED_MIN_FAILURES", "2")
    factory = Factory(fail=lambda text: text != "milk")
    assert run_embed.main(factory) == 1
    assert factory.loads == ["gemma2-text", "harrier-title"]
    s = summary(store, "gemma2-text")
    assert s["status"] == "failed" and s["failed"] == 2 and "rows failed" in s["failures"][0]
    assert alerts[0][0] == f"embed FAILED for {DAY}"
    assert not (store / "_state" / "LOCK").exists()


def test_failure_gate_needs_both_a_minimum_count_and_a_rate():
    assert run_embed.gate_failures(attempted=1000, failed=2, max_rate=0.05, min_failures=3) == []
    assert run_embed.gate_failures(attempted=1000, failed=40, max_rate=0.05, min_failures=3) == []
    assert run_embed.gate_failures(attempted=1000, failed=60, max_rate=0.05, min_failures=3)


def test_one_spec_crashing_still_runs_the_other_and_exits_1(env):
    store, _, alerts = env

    def make(spec):
        if spec.name == "gemma2-text":
            raise OSError("hub unreachable")
        return FakeEmbedder()

    assert run_embed.main(make) == 1
    assert summary(store, "gemma2-text")["failures"] == ["OSError: hub unreachable"]
    assert summary(store, "harrier-title")["embedded"] == 3
    assert "hub unreachable" in alerts[0][1]
    assert not (store / "_state" / "LOCK").exists()


def test_refuses_image_specs_and_a_missing_store(env, monkeypatch):
    monkeypatch.setenv("EMBED_SPECS", "gemma2-image")
    with pytest.raises(ValueError, match="only text specs"):
        run_embed.main(Factory())
    monkeypatch.delenv("EMBEDDINGS_STORE")
    with pytest.raises(KeyError):
        run_embed.main(Factory())


def test_state_lives_beside_the_tables_under_the_store_prefix():
    storage, prefix = run_embed.state_storage("s3://some-bucket/ml")
    assert storage.bucket == "some-bucket" and prefix == "ml"
    assert run_embed.key(prefix, "_state/LOCK") == "ml/_state/LOCK"
    assert run_embed.key("", "_state/LOCK") == "_state/LOCK"


def test_default_spec_ids_match_the_store_in_s3():
    # Changing a spec's hashed fields starts a new table and re-embeds the whole catalogue (~50k
    # rows); the first id is the table already copied to s3://.../ml, update both deliberately.
    assert get_spec("gemma2-text").spec_id == "268dad232ccc02bc"
    assert get_spec("harrier-title").spec_id == "25173d59826a669a"


# --- CloudFormation, IAM policy, image ---------------------------------------------------------


class Loader(yaml.SafeLoader):
    pass


Loader.add_multi_constructor("!", lambda loader, suffix, node: {suffix: _node(loader, node)})


def _node(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


@pytest.fixture(scope="module")
def template():
    return yaml.load((ROOT / "infra" / "embed-batch.yaml").read_text(), Loader)


@pytest.fixture(scope="module")
def policy():
    return json.loads((ROOT / "infra" / "iam" / "embed-job-role-policy.json").read_text())


def test_trigger_matches_only_a_succeeded_dbt_daily_job(template):
    rule = template["Resources"]["EmbedAfterDbtRule"]["Properties"]
    detail = rule["EventPattern"]["detail"]
    assert detail["status"] == ["SUCCEEDED"] and detail["jobName"] == [{"Ref": "DbtJobName"}]
    assert template["Parameters"]["DbtJobName"]["Default"] == "dbt-daily"
    assert template["Parameters"]["DbtJobDefinitionName"]["Default"] == (
        "ie_supermarket_dbt_job_definition"
    )
    assert "prefix" in detail["jobDefinition"][0]  # events carry the revisioned ARN
    assert rule["State"] == {"Ref": "TriggerState"}
    assert template["Parameters"]["TriggerState"]["Default"] == "DISABLED"
    target = rule["Targets"][0]
    assert target["Arn"] == {"Ref": "JobQueueArn"}
    assert target["BatchParameters"]["JobDefinition"] == "ie_supermarket_embed_job_definition"


def test_job_definition_is_sized_from_the_measurement(template):
    props = template["Resources"]["EmbedJobDefinition"]["Properties"]
    container = props["ContainerProperties"]
    sizes = {r["Type"]: r["Value"] for r in container["ResourceRequirements"]}
    assert sizes == {
        "VCPU": "4",
        "MEMORY": "8192",
    }  # peak RSS ~2 GB; 8 GB is Fargate's 4-vCPU floor
    env = {e["Name"]: e["Value"] for e in container["Environment"]}
    assert env["TORCH_THREADS"] == "4" and "EMBEDDINGS_STORE" in env and "CATALOGUE_URI" in env
    assert props["PlatformCapabilities"] == ["FARGATE"]
    assert container["Command"] == ["python", "infra/embed/run_embed.py"]


def test_template_and_standalone_policy_agree(template, policy):
    statements = template["Resources"]["EmbedJobRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    assert {
        s["Action"] if isinstance(s["Action"], str) else tuple(s["Action"]) for s in statements
    } == {
        s["Action"] if isinstance(s["Action"], str) else tuple(s["Action"])
        for s in policy["Statement"]
    }


def test_policy_is_least_privilege(policy):
    by_sid = {s["Sid"]: s for s in policy["Statement"]}
    for s in policy["Statement"]:
        actions = [s["Action"]] if isinstance(s["Action"], str) else s["Action"]
        assert not any("*" in a for a in actions)
    assert by_sid["ReadWriteEmbeddingStore"]["Resource"] == "arn:aws:s3:::ie-supermarket-data/ml/*"
    assert by_sid["DeleteOnlyTheLock"]["Resource"] == "arn:aws:s3:::ie-supermarket-data/ml/_state/*"
    writes = {"s3:PutObject", "s3:DeleteObject"}
    for sid, s in by_sid.items():
        resources = [s["Resource"]] if isinstance(s["Resource"], str) else s["Resource"]
        if writes & set([s["Action"]] if isinstance(s["Action"], str) else s["Action"]):
            assert all("/ml/" in r for r in resources), sid
    readable = by_sid["ReadCatalogue"]["Resource"]
    assert all("/processed" in r for r in readable) and not any("raw" in r for r in readable)


def test_requirements_pin_what_uv_lock_resolves():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"]}
    pins = {}
    for line in (ROOT / "infra" / "embed" / "requirements.txt").read_text().splitlines():
        if m := re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==(\S+)$", line):
            pins[m[1].lower()] = m[2]
    assert {"torch", "sentence-transformers", "transformers", "polars", "s3fs", "boto3"} <= set(
        pins
    )
    assert {n: v for n, v in pins.items() if locked[n] != v} == {}


def test_dockerfile_uses_cpu_torch_and_ships_the_packages():
    text = (ROOT / "infra" / "embed" / "Dockerfile.embed").read_text()
    assert text.startswith("# Build from the repo root") and "FROM python:3.14-slim" in text
    assert "download.pytorch.org/whl/cpu" in text and "torch==2.12.0+cpu" in text
    for path in ("scraper_common/", "ml_common/", "infra/embed/run_embed.py"):
        assert f"COPY {path}" in text
    ignore = (ROOT / "infra" / "embed" / "Dockerfile.embed.dockerignore").read_text()
    assert "!ml_common/" in ignore and "!scraper_common/" in ignore and "!infra/embed/" in ignore
