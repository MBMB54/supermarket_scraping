import posixpath

import polars as pl

from ml_common import image_map
from ml_common.specs import EmbeddingSpec
from ml_common.storage import Storage


def _read(uri: str, columns: list[str]) -> pl.DataFrame:
    root, name = posixpath.split(uri)
    return Storage(root or ".").read_parquet(name, columns=columns)


def load_catalogue(uri: str, spec: EmbeddingSpec, store: Storage) -> pl.DataFrame:
    """Read the columns `spec` needs from a catalogue parquet (local path or s3:// URI).

    A missing file or column raises, so a scheduled run fails instead of silently embedding nothing.
    Image specs read `image_url` and join `image_sha256` from the store's image map; products
    without a fetched image for their current URL get a null hash and are skipped until fetched.
    """
    if not spec.image_field:
        return _read(uri, spec.catalogue_columns)
    columns = [c for c in spec.catalogue_columns if c != spec.image_field]
    rows = _read(uri, [*columns, "image_url"])
    return image_map.attach_image_sha256(rows, image_map.read_map(store)).drop("image_url")


def load_image_catalogue(uri: str) -> pl.DataFrame:
    """Products with an image URL, for the fetcher."""
    return _read(uri, [*image_map.PRODUCT]).drop_nulls("image_url")
