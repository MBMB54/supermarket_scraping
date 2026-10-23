import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

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
    return pl.concat([storage.read_parquet(k) for k in part_keys(storage, spec)])


def latest_ok(table: pl.DataFrame) -> pl.DataFrame:
    """Current vector per (id, supermarket): the newest successful row, ignoring stale ones."""
    return (
        table.filter(pl.col("status") == OK)
        .sort("embedded_at")
        .unique(subset=["id", "supermarket"], keep="last", maintain_order=True)
    )


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
    if spec.dbt_compatible:
        return rows.with_columns(vectors.alias("title_embedding"), *meta).select(
            "id",
            "supermarket",
            "title",
            spec.text_field,
            "title_embedding",
            pl.lit(spec.model_id).alias("embedding_model"),
            "embedded_at",
            "content_key",
            "spec_id",
            "model_revision",
            "status",
            "error",
        )
    return rows.with_columns(
        vectors,
        *meta,
        pl.lit(spec.model_id).alias("model_id"),
        pl.lit(spec.modality).alias("modality"),
    ).select(
        "id",
        "supermarket",
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
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    key = f"{spec.table_prefix}/part-{stamp}-{uuid4().hex[:8]}.parquet"
    storage.write_parquet(table, key)
    return key
