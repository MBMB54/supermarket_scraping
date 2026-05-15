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

    # Aldi IDs: lightweight image (requests + polars, no browser)
    ids_aldi_response = batch.submit_job(
        jobName="aldi-ids",
        jobQueue=JOB_QUEUE,
        jobDefinition=IDS_JOB_DEFINITION,
        containerOverrides={"command": ["python", "aldi_ids.py"]},
    )
    ids_job_ids = {"aldi": ids_aldi_response["jobId"], "supervalu": ids_aldi_response["jobId"]}
    submitted.append({"stage": "ids", "retailer": "aldi", "jobId": ids_aldi_response["jobId"]})
    logger.info(f"Submitted aldi ids job: {ids_aldi_response['jobId']}")

    # Tesco IDs: runs in the scraper image which already has Playwright
    ids_tesco_response = batch.submit_job(
        jobName="tesco-ids",
        jobQueue=JOB_QUEUE,
        jobDefinition=SCRAPER_JOB_DEFINITION,
        containerOverrides={"command": ["python", "tesco_ids.py"]},
    )
    ids_job_ids["tesco"] = ids_tesco_response["jobId"]
    submitted.append({"stage": "ids", "retailer": "tesco", "jobId": ids_tesco_response["jobId"]})
    logger.info(f"Submitted tesco ids job: {ids_tesco_response['jobId']}")

    # supervalu has no dedicated IDs job — its chunks run after aldi-ids as a proxy
    ids_job_ids["supervalu"] = ids_job_ids["aldi"]

    # Submit scraper chunks; each waits for its retailer's IDs job
    for retailer in RETAILERS:
        for chunk_id in range(TOTAL_CHUNKS):
            response = batch.submit_job(
                jobName=f"{retailer}-scraper-chunk{chunk_id}",
                jobQueue=JOB_QUEUE,
                jobDefinition=SCRAPER_JOB_DEFINITION,
                dependsOn=[{"jobId": ids_job_ids[retailer], "type": "SEQUENTIAL"}],
                containerOverrides={
                    "command": ["python", f"{retailer}_api.py"],
                    "environment": [
                        {"name": "CHUNK_ID", "value": str(chunk_id)},
                        {"name": "TOTAL_CHUNKS", "value": str(TOTAL_CHUNKS)},
                    ],
                },
            )
            submitted.append(
                {"stage": "scraper", "retailer": retailer, "chunkId": chunk_id, "jobId": response["jobId"]}
            )
            logger.info(f"Submitted {retailer} chunk {chunk_id}/{TOTAL_CHUNKS}: {response['jobId']}")

    return {"statusCode": 200, "jobs": submitted}
