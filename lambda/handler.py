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


def submit_ids_job(batch, retailer: str, script: str, job_definition: str) -> dict:
    response = batch.submit_job(
        jobName=f"{retailer}-ids",
        jobQueue=JOB_QUEUE,
        jobDefinition=job_definition,
        containerOverrides={"command": ["python", script]},
    )
    job_id = response["jobId"]
    logger.info(f"Submitted {retailer} ids job: {job_id}")
    return {"stage": "ids", "retailer": retailer, "jobId": job_id}


def submit_scraper_chunks(batch, retailer: str, api_script: str, ids_job_id: str | None = None) -> list[dict]:
    kwargs = {}
    if ids_job_id:
        kwargs["dependsOn"] = [{"jobId": ids_job_id, "type": "SEQUENTIAL"}]

    jobs = []
    for chunk_id in range(TOTAL_CHUNKS):
        response = batch.submit_job(
            jobName=f"{retailer}-scraper-chunk{chunk_id}",
            jobQueue=JOB_QUEUE,
            jobDefinition=SCRAPER_JOB_DEFINITION,
            containerOverrides={
                "command": ["python", api_script],
                "environment": [
                    {"name": "CHUNK_ID", "value": str(chunk_id)},
                    {"name": "TOTAL_CHUNKS", "value": str(TOTAL_CHUNKS)},
                ],
            },
            **kwargs,
        )
        logger.info(f"Submitted {retailer} chunk {chunk_id}/{TOTAL_CHUNKS}: {response['jobId']}")
        jobs.append({"stage": "scraper", "retailer": retailer, "chunkId": chunk_id, "jobId": response["jobId"]})
    return jobs


def lambda_handler(event, context):
    batch = boto3.client("batch", region_name=REGION)
    submitted = []

    aldi_ids = submit_ids_job(batch, "aldi", "aldi_ids.py", IDS_JOB_DEFINITION)
    submitted.append(aldi_ids)
    submitted.extend(submit_scraper_chunks(batch, "aldi", "aldi_api.py", aldi_ids["jobId"]))

    submitted.extend(submit_scraper_chunks(batch, "tesco", "tesco_api.py"))

    supervalu_ids = submit_ids_job(batch, "supervalu", "supervalu_ids.py", SCRAPER_JOB_DEFINITION)
    submitted.append(supervalu_ids)
    submitted.extend(submit_scraper_chunks(batch, "supervalu", "supervalu_api.py", supervalu_ids["jobId"]))

    dunnes_ids = submit_ids_job(batch, "dunnes", "dunnes_ids.py", SCRAPER_JOB_DEFINITION)
    submitted.append(dunnes_ids)
    submitted.extend(submit_scraper_chunks(batch, "dunnes", "dunnes_api.py", dunnes_ids["jobId"]))

    return {"statusCode": 200, "jobs": submitted}
