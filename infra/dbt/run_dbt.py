"""Batch entrypoint: pull the DuckDB state file, run dbt for one run_date, publish, alert.

Env: RUN_DATE (YYYY-MM-DD, required), BUCKET (default ie-supermarket-data), SNS_TOPIC_ARN (optional).
Draft, not deployed. Exit code is non-zero on any dbt failure so Batch marks the job FAILED.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BUCKET = os.environ.get("BUCKET", "ie-supermarket-data")
RUN_DATE = os.environ["RUN_DATE"]
TOPIC = os.environ.get("SNS_TOPIC_ARN")
DB_KEY = "dbt/state/supermarket_data.duckdb"
LOCK_KEY = "dbt/state/LOCK"
LOCAL_DB = Path(os.environ.get("DBT_DUCKDB_PATH", "/tmp/supermarket_data.duckdb"))
OUT = f"s3://{BUCKET}/processed"

s3 = boto3.client("s3")
sns = boto3.client("sns") if TOPIC else None


def alert(subject: str, body: str) -> None:
    print(subject, body, sep="\n")
    if sns:
        sns.publish(TopicArn=TOPIC, Subject=subject[:99], Message=body[:250_000])


def take_lock() -> None:
    try:  # S3 conditional write: fails if another run holds the lock
        s3.put_object(Bucket=BUCKET, Key=LOCK_KEY, Body=RUN_DATE.encode(), IfNoneMatch="*")
    except ClientError as e:
        raise SystemExit(f"state locked by another run: {e}") from e


def dbt(*args: str) -> int:
    cmd = [
        "dbt",
        *args,
        "--project-dir",
        "dbt",
        "--vars",
        json.dumps({"run_date": RUN_DATE, "parquet_output_prefix": f"{OUT}"}),
    ]
    print("+", " ".join(cmd))
    return subprocess.call(cmd)


def main() -> int:
    take_lock()
    try:
        try:
            s3.download_file(BUCKET, DB_KEY, str(LOCAL_DB))
        except ClientError:
            print("no existing state file: starting a fresh database")
        rc = dbt("seed") or dbt("build")
        if rc:
            alert(
                f"dbt FAILED for {RUN_DATE}", "See the Batch job logs. State file was not updated."
            )
            return rc
        # Publish only after a green build, so a bad run never overwrites good state
        s3.upload_file(str(LOCAL_DB), BUCKET, DB_KEY)
        s3.copy_object(
            Bucket=BUCKET,
            Key=f"dbt/archive/{RUN_DATE}/supermarket_data.duckdb",
            CopySource={"Bucket": BUCKET, "Key": DB_KEY},
        )
        alert(f"dbt OK for {RUN_DATE}", f"Published s3://{BUCKET}/{DB_KEY}")
        return 0
    finally:
        s3.delete_object(Bucket=BUCKET, Key=LOCK_KEY)


if __name__ == "__main__":
    sys.exit(main())
