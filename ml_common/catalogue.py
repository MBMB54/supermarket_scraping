import posixpath

import polars as pl

from ml_common.specs import EmbeddingSpec
from ml_common.storage import Storage


def load_catalogue(uri: str, spec: EmbeddingSpec) -> pl.DataFrame:
    """Read the columns `spec` needs from a catalogue parquet (local path or s3:// URI).

    A missing file or column raises, so a scheduled run fails instead of silently embedding nothing.
    Image specs need an `image_sha256` column: the sha256 of the bytes stored under
    `images/<aa>/<sha>.webp` in the store.
    """
    root, name = posixpath.split(uri)
    return Storage(root or ".").read_parquet(name, columns=spec.catalogue_columns)
