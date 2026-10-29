import json
from dataclasses import asdict
from datetime import datetime

import polars as pl

from ml_common.diff import KEY
from ml_common.embedders import EmbedResult
from ml_common.specs import EmbeddingSpec
from ml_common.storage import Storage

OK = "ok"
ERROR = "error"
_EMBEDDING = pl.List(pl.Float32)


def part_keys(storage: Storage, spec: EmbeddingSpec) -> list[str]:
    return [k for k in storage.list(spec.table_prefix) if k.endswith(".parquet")]


def read_done(storage: Storage, spec: EmbeddingSpec) -> pl.DataFrame:
    """Keys with a successful embedding; error rows are not done, so they are retried."""
    empty = pl.DataFrame(schema={c: pl.String for c in KEY})
    parts = [
        storage.read_parquet(k, columns=[*KEY, "status"]).filter(pl.col("status") == OK)
        for k in part_keys(storage, spec)
    ]
    return pl.concat([empty, *[p.select(KEY) for p in parts]])


def read_table(storage: Storage, spec: EmbeddingSpec) -> pl.DataFrame:
    """All rows of a spec; parts written before `vector_index` existed read as vector 0."""
    table = pl.concat(
        [storage.read_parquet(k) for k in part_keys(storage, spec)], how="diagonal_relaxed"
    )
    if "vector_index" not in table.columns:
        return table.with_columns(pl.lit(0, dtype=pl.Int32).alias("vector_index"))
    return table.with_columns(pl.col("vector_index").fill_null(0))


def latest_ok(table: pl.DataFrame) -> pl.DataFrame:
    """Current vectors per (id, supermarket): every vector of the newest successful embedding.

    A multi-vector item is written in one chunk, so its vectors share one `embedded_at`; older
    embeddings of the same item (a changed title or image) are dropped whole.
    """
    ok = table.filter(pl.col("status") == OK)
    newest = pl.col("embedded_at") == pl.col("embedded_at").max().over(["id", "supermarket"])
    return ok.filter(newest)


def build_rows(
    spec: EmbeddingSpec, rows: pl.DataFrame, results: list[EmbedResult], embedded_at: datetime
) -> pl.DataFrame:
    vectors = pl.Series("embedding", [r.vector for r in results], dtype=_EMBEDDING)
    meta = [
        pl.Series("status", [ERROR if r.error else OK for r in results], dtype=pl.String),
        pl.Series("error", [r.error for r in results], dtype=pl.String),
        pl.lit(embedded_at).alias("embedded_at"),
        pl.lit(spec.spec_id).alias("spec_id"),
        pl.lit(spec.revision).alias("model_revision"),
    ]
    return rows.with_columns(
        vectors,
        *meta,
        pl.lit(spec.model_id).alias("model_id"),
        pl.lit(spec.modality).alias("modality"),
        pl.lit(0, dtype=pl.Int32).alias("vector_index"),
    ).select(
        "id",
        "supermarket",
        "vector_index",
        "content_key",
        "spec_id",
        "model_id",
        "model_revision",
        "modality",
        "embedding",
        "embedded_at",
        "status",
        "error",
    )


def append_part(storage: Storage, spec: EmbeddingSpec, table: pl.DataFrame) -> str:
    """Write a new part file; existing parts are never touched."""
    manifest = f"{spec.table_prefix}/manifest.json"
    if not storage.exists(manifest):
        body = {"spec": asdict(spec) | {"spec_id": spec.spec_id}, "columns": table.columns}
        storage.write_bytes(manifest, json.dumps(body, indent=2).encode())
    return storage.append_parquet(table, spec.table_prefix)
