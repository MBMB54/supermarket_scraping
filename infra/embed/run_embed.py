"""Batch entrypoint: embed the catalogue rows each text spec is missing, append them to the S3 store.

Runs after the dbt job (EventBridge rule on its SUCCEEDED state change, infra/embed-batch.yaml), so
it is idempotent and cheap when nothing changed: `run_embed` diffs the catalogue against the store
and only calls the embedder for pending rows, and the model is loaded on that first call, so a
no-op run never imports torch or downloads a model.

Env: EMBEDDINGS_STORE (required, `s3://bucket/ml`; unset would silently write to the container's
disk), CATALOGUE_URI (default the dbt-written processed parquet; shadow week:
s3://.../processed-shadow/all_supermarket_products.parquet), EMBED_SPECS (comma list of text specs,
default `gemma2-text,harrier-title`), CHUNK_ROWS (rows per appended part, default 1024),
EMBED_MAX_FAILURE_RATE (default 0.05) with EMBED_MIN_FAILURES (default 3): a spec fails when both
are exceeded, TORCH_THREADS (default CPU count; Fargate may report the host's CPUs, so the job
definition sets it), EMBED_LIMIT (smoke-test cap on rows per spec), RUN_DATE (default today UTC),
SNS_TOPIC_ARN (optional), LOCK_MAX_AGE_MINUTES (default 30; the lock is refreshed before every
chunk, so only a killed job's lock goes stale).

State under the store root: `_state/LOCK` and `_run_summary/<date>_<spec>.json`. A held lock means
another run is embedding; this run exits 0 quietly (the holder's catalogue read may predate the
latest dbt build, so the next trigger picks those rows up). Exits 1 when a spec failed.
Draft, not deployed.
"""

import gc
import logging
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: ml_common, scraper_common

from ml_common.catalogue import load_catalogue  # noqa: E402
from ml_common.config import MLConfig, setup_logging  # noqa: E402
from ml_common.pipeline import run_embed  # noqa: E402
from ml_common.specs import EmbeddingSpec, get_spec  # noqa: E402
from ml_common.storage import Storage as StoreStorage  # noqa: E402
from scraper_common.storage import Storage  # noqa: E402

logger = logging.getLogger("embed")

DEFAULT_SPECS = "gemma2-text,harrier-title"


def alert(subject: str, body: str) -> None:
    print(subject, body, sep="\n")
    if topic := os.environ.get("SNS_TOPIC_ARN"):
        boto3.client("sns").publish(TopicArn=topic, Subject=subject[:99], Message=body[:250_000])


def state_storage(store_uri: str) -> tuple[Storage, str]:
    """Lock and summary objects sit beside the tables, through the scrapers' Storage (S3 or a dir)."""
    if store_uri.startswith("s3://"):
        parsed = urlparse(store_uri)
        return Storage(bucket=parsed.netloc), parsed.path.strip("/")
    return Storage(Path(store_uri)), ""


def key(prefix: str, name: str) -> str:
    return f"{prefix}/{name}" if prefix else name


def peak_rss_mb() -> int:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(
        peak / (2**20 if sys.platform == "darwin" else 1024)
    )  # bytes on macOS, KiB on Linux


class StoreLock:
    """S3 conditional-write lock. `touch` refreshes it so staleness means "no heartbeat", not "old"."""

    def __init__(self, storage: Storage, lock_key: str, holder: str, max_age_minutes: float):
        self.storage, self.key, self.holder = storage, lock_key, holder.encode()
        self.max_age_minutes = max_age_minutes

    def acquire(self) -> bool:
        if self.storage.put_if_absent(self.key, self.holder):
            return True
        modified = self.storage.last_modified(self.key)
        if modified is None:  # released between the two calls
            return self.storage.put_if_absent(self.key, self.holder)
        if (datetime.now(UTC) - modified).total_seconds() / 60 < self.max_age_minutes:
            return False
        logger.warning("breaking a stale lock held by %r", self.storage.get_bytes(self.key))
        self.storage.delete(self.key)
        return self.storage.put_if_absent(self.key, self.holder)

    def touch(self) -> None:
        self.storage.put_bytes(self.key, self.holder)

    def release(self) -> None:
        if self.storage.get_bytes(self.key) == self.holder:  # never delete a lock someone took over
            self.storage.delete(self.key)


def load_embedder(spec: EmbeddingSpec):
    """The CPU sentence-transformers embedder with the model loaded (imports torch)."""
    import torch

    from ml_common.embedders import SentenceTransformerEmbedder

    torch.set_num_threads(int(os.environ.get("TORCH_THREADS") or os.cpu_count() or 1))
    embedder = SentenceTransformerEmbedder(spec, device="cpu")
    embedder.model  # noqa: B018  load now so the load time is measured apart from encoding
    return embedder


class LazyEmbedder:
    """Builds the real embedder on the first `encode`, which `run_embed` only calls for pending rows."""

    def __init__(self, make, heartbeat):
        self._make, self._heartbeat = make, heartbeat
        self._embedder = None
        self.load_seconds = 0.0

    @property
    def loaded(self) -> bool:
        return self._embedder is not None

    def encode(self, inputs):
        self._heartbeat()
        if self._embedder is None:
            start = time.perf_counter()
            self._embedder = self._make()
            self.load_seconds = time.perf_counter() - start
            logger.info("model loaded in %.1fs", self.load_seconds)
        return self._embedder.encode(inputs)


def gate_failures(attempted: int, failed: int, max_rate: float, min_failures: int) -> list[str]:
    if failed >= min_failures and failed / attempted > max_rate:
        return [f"{failed}/{attempted} rows failed to embed (limit {max_rate:.0%})"]
    return []


def embed_spec(spec, cfg, store, lock, make_embedder, run_date, limit) -> dict:
    started = time.perf_counter()
    catalogue = load_catalogue(cfg.catalogue, spec, store)
    lazy = LazyEmbedder(lambda: make_embedder(spec), lock.touch)
    result = run_embed(spec, catalogue, store, lazy, limit=limit, chunk_rows=cfg.chunk_rows)
    failures = gate_failures(
        result.embedded + result.failed,
        result.failed,
        float(os.environ.get("EMBED_MAX_FAILURE_RATE", "0.05")),
        int(os.environ.get("EMBED_MIN_FAILURES", "3")),
    )
    return {
        "spec": spec.name,
        "spec_id": spec.spec_id,
        "run_date": run_date,
        "status": "failed" if failures else "ok",
        "failures": failures,
        "catalogue_uri": cfg.catalogue,
        "catalogue_rows": len(catalogue),
        "pending": result.pending,
        "embedded": result.embedded,
        "failed": result.failed,
        "model_loaded": lazy.loaded,
        "model_load_seconds": round(lazy.load_seconds, 1),
        "duration_seconds": round(time.perf_counter() - started, 1),
        "peak_rss_mb": peak_rss_mb(),
        "job_id": os.environ.get("AWS_BATCH_JOB_ID"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def write_summary(state: Storage, prefix: str, summary: dict) -> None:
    """One object per spec and day; later runs the same day add to `embedded_today`."""
    summary_key = key(prefix, f"_run_summary/{summary['run_date']}_{summary['spec']}.json")
    earlier = state.get_json(summary_key) or {}
    summary["runs_today"] = earlier.get("runs_today", 0) + 1
    summary["embedded_today"] = earlier.get("embedded_today", 0) + summary.get("embedded", 0)
    state.put_json(summary_key, summary)


def main(make_embedder=load_embedder) -> int:
    setup_logging("embed")
    cfg = MLConfig.from_env()
    store_uri = os.environ["EMBEDDINGS_STORE"]
    run_date = os.environ.get("RUN_DATE") or datetime.now(UTC).date().isoformat()
    limit = int(os.environ["EMBED_LIMIT"]) if os.environ.get("EMBED_LIMIT") else None
    specs = [get_spec(n.strip()) for n in os.environ.get("EMBED_SPECS", DEFAULT_SPECS).split(",")]
    if bad := [s.name for s in specs if s.modality != "text"]:
        raise ValueError(
            f"only text specs run here (images are fetched and embedded by hand): {bad}"
        )

    state, prefix = state_storage(store_uri)
    lock = StoreLock(
        state,
        key(prefix, "_state/LOCK"),
        os.environ.get("AWS_BATCH_JOB_ID", "local"),
        float(os.environ.get("LOCK_MAX_AGE_MINUTES", "30")),
    )
    if not lock.acquire():
        print(
            f"another embed run holds {state.uri(lock.key)}; exiting, the next trigger catches up"
        )
        return 0

    store = StoreStorage(store_uri)
    summaries, errors = [], []
    try:
        for spec in specs:
            try:
                summary = embed_spec(spec, cfg, store, lock, make_embedder, run_date, limit)
            except (
                Exception
            ) as e:  # one spec's failure must not stop the other; reported and exit 1
                traceback.print_exc()
                summary = {
                    "spec": spec.name,
                    "spec_id": spec.spec_id,
                    "run_date": run_date,
                    "status": "failed",
                    "failures": [f"{type(e).__name__}: {e}"[:500]],
                    "job_id": os.environ.get("AWS_BATCH_JOB_ID"),
                }
            write_summary(state, prefix, summary)
            summaries.append(summary)
            errors += [f"{summary['spec']}: {f}" for f in summary["failures"]]
            gc.collect()  # drop the previous model before the next one loads
    finally:
        lock.release()

    lines = [
        f"{s['spec']}: {s['status']}, pending {s.get('pending', '?')}, embedded "
        f"{s.get('embedded', 0)}, failed {s.get('failed', 0)}"
        for s in summaries
    ]
    if errors:
        alert(f"embed FAILED for {run_date}", "\n".join([*errors, "", *lines]))
        return 1
    if any(s["embedded"] for s in summaries):  # a no-op run (most slots) stays quiet
        alert(f"embed OK for {run_date}", "\n".join(lines))
    else:
        print(f"{run_date}: nothing to embed", *lines, sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
