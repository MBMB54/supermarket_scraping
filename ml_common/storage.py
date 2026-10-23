import io
import posixpath
from datetime import UTC, datetime
from uuid import uuid4

import polars as pl

from scraper_common.config import REGION


class Storage:
    """One interface over a local dir, `s3://bucket/prefix` or `hf://buckets/user/bucket/prefix`.

    A plain path is a local directory (tests, laptop runs). fsspec and the remote backends are
    imported on first use, so constructing a Storage needs no network or optional dependency.
    """

    def __init__(self, root: str):
        self.root = root.rstrip("/")
        self._resolved = None

    @property
    def fs(self):
        return self._resolve()[0]

    def _resolve(self):
        if self._resolved is None:
            import fsspec

            options = (
                {"client_kwargs": {"region_name": REGION}} if self.root.startswith("s3://") else {}
            )
            self._resolved = fsspec.core.url_to_fs(self.root, **options)
        return self._resolved

    def _path(self, key: str) -> str:
        return posixpath.join(self._resolve()[1], key)

    def uri(self, key: str) -> str:
        return f"{self.root}/{key}"

    def exists(self, key: str) -> bool:
        return self.fs.exists(self._path(key))

    def list(self, prefix: str) -> list[str]:
        path = self._path(prefix)
        if not self.fs.exists(path):
            return []
        base = posixpath.join(self._resolve()[1], "")
        return sorted(p.removeprefix(base) for p in self.fs.find(path))

    def read_bytes(self, key: str) -> bytes:
        return self.fs.cat_file(self._path(key))

    def write_bytes(self, key: str, body: bytes) -> None:
        path = self._path(key)
        self.fs.makedirs(posixpath.dirname(path), exist_ok=True)
        self.fs.pipe_file(path, body)

    def read_parquet(self, key: str, columns: list[str] | None = None) -> pl.DataFrame:
        with self.fs.open(self._path(key), "rb") as f:
            return pl.read_parquet(f, columns=columns)

    def write_parquet(self, df: pl.DataFrame, key: str) -> None:
        buf = io.BytesIO()
        df.write_parquet(buf)
        self.write_bytes(key, buf.getvalue())

    def append_parquet(self, df: pl.DataFrame, prefix: str) -> str:
        """Write `df` as a new `<prefix>/part-<utc>-<rand>.parquet`; existing parts are never touched."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        key = f"{prefix}/part-{stamp}-{uuid4().hex[:8]}.parquet"
        self.write_parquet(df, key)
        return key
