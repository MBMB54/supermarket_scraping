import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "eu-west-1")
JOB_QUEUE = os.environ.get("BATCH_JOB_QUEUE", "getting-started-fargate-job-queue")
SCRAPER_JOB_DEFINITION = os.environ.get("SCRAPER_JOB_DEFINITION", "ocado_scraper_job_definition")
TOTAL_CHUNKS = int(os.environ.get("TOTAL_CHUNKS", "5"))

RETAILERS = ["aldi", "tesco", "supervalu"]


def lambda_handler(event, context):
    batch = boto3.client("batch", region_name=REGION)

    submitted = []
    for retailer in RETAILERS:
        for chunk_id in range(TOTAL_CHUNKS):
            response = batch.submit_job(
                jobName=f"{retailer}-scraper-chunk{chunk_id}",
                jobQueue=JOB_QUEUE,
                jobDefinition=SCRAPER_JOB_DEFINITION,
                containerOverrides={
                    "command": ["python", f"{retailer}_api.py"],
                    "environment": [
                        {"name": "CHUNK_ID", "value": str(chunk_id)},
                        {"name": "TOTAL_CHUNKS", "value": str(TOTAL_CHUNKS)},
                    ],
                },
            )
            job_id = response["jobId"]
            submitted.append({"retailer": retailer, "chunkId": chunk_id, "jobId": job_id})
            logger.info(f"Submitted {retailer} chunk {chunk_id}/{TOTAL_CHUNKS}: {job_id}")

    return {"statusCode": 200, "jobs": submitted}
