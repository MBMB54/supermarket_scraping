import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import polars as pl

from ml_common import table
from ml_common.diff import pending
from ml_common.embedders import Embedder, EmbedInput
from ml_common.keys import image_key
from ml_common.specs import EmbeddingSpec
from ml_common.storage import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSummary:
    pending: int
    embedded: int
    failed: int


def _inputs(spec: EmbeddingSpec, rows: pl.DataFrame, storage: Storage) -> list[EmbedInput]:
    texts = rows[spec.text_field].to_list() if spec.text_field else [None] * len(rows)
    shas = rows[spec.image_field].to_list() if spec.image_field else [None] * len(rows)
    inputs = []
    for text, sha in zip(texts, shas, strict=True):
        try:
            image = storage.read_bytes(image_key(sha)) if sha else None
        except FileNotFoundError:
            image = None
        inputs.append(EmbedInput(text=text, image=image))
    return inputs


def run_embed(
    spec: EmbeddingSpec,
    catalogue: pl.DataFrame,
    storage: Storage,
    embedder: Embedder,
    *,
    limit: int | None = None,
    chunk_rows: int = 1024,
) -> RunSummary:
    """Embed rows missing from the spec's table and append one part file per chunk."""
    todo = pending(spec, catalogue, table.read_done(storage, spec))
    total = len(todo)
    todo = todo.head(limit) if limit is not None else todo
    logger.info("%s: %d pending of %d catalogue rows", spec.name, len(todo), len(catalogue))
    embedded = failed = 0
    for start in range(0, len(todo), chunk_rows):
        rows = todo.slice(start, chunk_rows)
        results = embedder.encode(_inputs(spec, rows, storage))
        part = table.build_rows(spec, rows, results, datetime.now(UTC))
        table.append_part(storage, spec, part)
        bad = sum(r.error is not None for r in results)
        embedded, failed = embedded + len(results) - bad, failed + bad
        logger.info("%s: %d/%d done (%d failed)", spec.name, embedded + failed, len(todo), failed)
    return RunSummary(pending=total, embedded=embedded, failed=failed)
