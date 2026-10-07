import gzip
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from scraper_common.config import BUCKET, REGION

logger = logging.getLogger(__name__)


class Storage:
    """S3 access for raw output; writes to `output_dir` on local disk instead when it is set."""

    def __init__(self, output_dir: Path | None = None, bucket: str = BUCKET):
        self.output_dir = output_dir
        self.bucket = bucket
        self._client = None

    @classmethod
    def from_env(cls) -> Storage:
        output_dir = os.environ.get("OUTPUT_DIR")
        return cls(Path(output_dir) if output_dir else None)

    @property
    def s3(self):
        if self._client is None:
            self._client = boto3.client("s3", region_name=REGION)
        return self._client

    def uri(self, key: str) -> str:
        return str(self.output_dir / key) if self.output_dir else f"s3://{self.bucket}/{key}"

    def put_bytes(self, key: str, body: bytes, content_type: str | None = None) -> None:
        if self.output_dir:
            path = self.output_dir / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            return
        extra = {"ContentType": content_type} if content_type else {}
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, **extra)

    def get_bytes(self, key: str) -> bytes | None:
        if self.output_dir:
            path = self.output_dir / key
            return path.read_bytes() if path.exists() else None
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def delete(self, key: str) -> None:
        if self.output_dir:
            (self.output_dir / key).unlink(missing_ok=True)
        else:
            self.s3.delete_object(Bucket=self.bucket, Key=key)

    def put_json(self, key: str, obj: dict) -> None:
        self.put_bytes(key, json.dumps(obj).encode(), "application/json")

    def get_json(self, key: str) -> dict | None:
        body = self.get_bytes(key)
        return json.loads(body) if body is not None else None

    def put_jsonl_gz(self, key: str, records: list[dict]) -> None:
        lines = "".join(json.dumps(r) + "\n" for r in records)
        self.put_bytes(key, gzip.compress(lines.encode("utf-8")))

    def last_modified(self, key: str) -> datetime | None:
        if self.output_dir:
            path = self.output_dir / key
            return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC) if path.exists() else None
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)["LastModified"]
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def parquet_uri_and_options(self, key: str) -> tuple[str, dict]:
        return self.uri(key), {} if self.output_dir else {"aws_region": REGION}
