import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "eu-west-1")
JOB_QUEUE = os.environ.get("BATCH_JOB_QUEUE", "getting-started-fargate-job-queue")
IDS_JOB_DEFINITION = os.environ.get("IDS_JOB_DEFINITION", "ie_supermarket_ids_job_definition")
SCRAPER_JOB_DEFINITION = os.environ.get("SCRAPER_JOB_DEFINITION", "ie_supermarket_batch_job_definition")
TOTAL_CHUNKS = int(os.environ.get("TOTAL_CHUNKS", "5"))

RETAILERS = ["aldi", "tesco", "supervalu"]


def lambda_handler(event, context):
    batch = boto3.client("batch", region_name=REGION)

    submitted = []

    # Submit ids job first — scraper jobs depend on it completing successfully
    ids_response = batch.submit_job(
        jobName="aldi-ids",
        jobQueue=JOB_QUEUE,
        jobDefinition=IDS_JOB_DEFINITION,
    )
    ids_job_id = ids_response["jobId"]
    submitted.append({"stage": "ids", "retailer": "aldi", "jobId": ids_job_id})
    logger.info(f"Submitted aldi ids job: {ids_job_id}")

    # Submit scraper chunks, each depending on the ids job succeeding
    for retailer in RETAILERS:
        for chunk_id in range(TOTAL_CHUNKS):
            response = batch.submit_job(
                jobName=f"{retailer}-scraper-chunk{chunk_id}",
                jobQueue=JOB_QUEUE,
                jobDefinition=SCRAPER_JOB_DEFINITION,
                dependsOn=[{"jobId": ids_job_id, "type": "SEQUENTIAL"}],
                containerOverrides={
                    "command": ["python", f"{retailer}_api.py"],
                    "environment": [
                        {"name": "CHUNK_ID", "value": str(chunk_id)},
                        {"name": "TOTAL_CHUNKS", "value": str(TOTAL_CHUNKS)},
                    ],
                },
            )
            job_id = response["jobId"]
            submitted.append({"stage": "scraper", "retailer": retailer, "chunkId": chunk_id, "jobId": job_id})
            logger.info(f"Submitted {retailer} chunk {chunk_id}/{TOTAL_CHUNKS}: {job_id}")

    return {"statusCode": 200, "jobs": submitted}
