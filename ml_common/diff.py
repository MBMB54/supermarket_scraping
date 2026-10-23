import polars as pl

from ml_common.keys import input_key
from ml_common.specs import EmbeddingSpec

KEY = ["id", "supermarket", "content_key"]


def with_content_keys(spec: EmbeddingSpec, catalogue: pl.DataFrame) -> pl.DataFrame:
    """Add `content_key`; rows without the input the spec needs cannot be embedded and are dropped."""
    required = [c for c in (spec.text_field, spec.image_field) if c]
    rows = catalogue.drop_nulls(required)
    if spec.text_field:
        rows = rows.filter(pl.col(spec.text_field) != "")
    texts = rows[spec.text_field].to_list() if spec.text_field else [None] * len(rows)
    images = rows[spec.image_field].to_list() if spec.image_field else [None] * len(rows)
    keys = [input_key(spec.modality, t, i) for t, i in zip(texts, images, strict=True)]
    return rows.with_columns(pl.Series("content_key", keys, dtype=pl.String))


def pending(spec: EmbeddingSpec, catalogue: pl.DataFrame, done: pl.DataFrame) -> pl.DataFrame:
    """Catalogue rows whose (id, supermarket, content_key) has no successful row for this spec."""
    return with_content_keys(spec, catalogue).join(done.select(KEY), on=KEY, how="anti")
