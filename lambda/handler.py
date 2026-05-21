import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "eu-west-1")
JOB_QUEUE = os.environ.get("BATCH_JOB_QUEUE", "getting-started-fargate-job-queue")
IDS_JOB_DEFINITION = os.environ.get("IDS_JOB_DEFINITION", "ie_supermarket_ids_job_definition")
SCRAPER_JOB_DEFINITION = os.environ.get(
    "SCRAPER_JOB_DEFINITION", "ie_supermarket_batch_job_definition"
)
TOTAL_CHUNKS = int(os.environ.get("TOTAL_CHUNKS", "5"))


def lambda_handler(event, context):
    batch = boto3.client("batch", region_name=REGION)
    submitted = []

    # Aldi IDs: lightweight image (requests + polars, no browser)
    ids_aldi_response = batch.submit_job(
        jobName="aldi-ids",
        jobQueue=JOB_QUEUE,
        jobDefinition=IDS_JOB_DEFINITION,
        containerOverrides={"command": ["python", "aldi_ids.py"]},
    )
    aldi_ids_job_id = ids_aldi_response["jobId"]
    submitted.append({"stage": "ids", "retailer": "aldi", "jobId": aldi_ids_job_id})
    logger.info(f"Submitted aldi ids job: {aldi_ids_job_id}")

    # Aldi scraper chunks: depend on aldi-ids completing
    for chunk_id in range(TOTAL_CHUNKS):
        response = batch.submit_job(
            jobName=f"aldi-scraper-chunk{chunk_id}",
            jobQueue=JOB_QUEUE,
            jobDefinition=SCRAPER_JOB_DEFINITION,
            dependsOn=[{"jobId": aldi_ids_job_id, "type": "SEQUENTIAL"}],
            containerOverrides={
                "command": ["python", "aldi_api.py"],
                "environment": [
                    {"name": "CHUNK_ID", "value": str(chunk_id)},
                    {"name": "TOTAL_CHUNKS", "value": str(TOTAL_CHUNKS)},
                ],
            },
        )
        submitted.append({"stage": "scraper", "retailer": "aldi", "chunkId": chunk_id, "jobId": response["jobId"]})
        logger.info(f"Submitted aldi chunk {chunk_id}/{TOTAL_CHUNKS}: {response['jobId']}")

    # Tesco scraper chunks: IDs are scraped separately, no dependency
    for chunk_id in range(TOTAL_CHUNKS):
        response = batch.submit_job(
            jobName=f"tesco-scraper-chunk{chunk_id}",
            jobQueue=JOB_QUEUE,
            jobDefinition=SCRAPER_JOB_DEFINITION,
            containerOverrides={
                "command": ["python", "tesco_api.py"],
                "environment": [
                    {"name": "CHUNK_ID", "value": str(chunk_id)},
                    {"name": "TOTAL_CHUNKS", "value": str(TOTAL_CHUNKS)},
                ],
            },
        )
        submitted.append({"stage": "scraper", "retailer": "tesco", "chunkId": chunk_id, "jobId": response["jobId"]})
        logger.info(f"Submitted tesco chunk {chunk_id}/{TOTAL_CHUNKS}: {response['jobId']}")

    # Supervalu scraper chunks: IDs are scraped separately, no dependency
    for chunk_id in range(TOTAL_CHUNKS):
        response = batch.submit_job(
            jobName=f"supervalu-scraper-chunk{chunk_id}",
            jobQueue=JOB_QUEUE,
            jobDefinition=SCRAPER_JOB_DEFINITION,
            containerOverrides={
                "command": ["python", "supervalu_api.py"],
                "environment": [
                    {"name": "CHUNK_ID", "value": str(chunk_id)},
                    {"name": "TOTAL_CHUNKS", "value": str(TOTAL_CHUNKS)},
                ],
            },
        )
        submitted.append({"stage": "scraper", "retailer": "supervalu", "chunkId": chunk_id, "jobId": response["jobId"]})
        logger.info(f"Submitted supervalu chunk {chunk_id}/{TOTAL_CHUNKS}: {response['jobId']}")

    return {"statusCode": 200, "jobs": submitted}
