---
name: submit-jobs
description: Submit AWS Batch scraping jobs for one or more retailers (tesco, aldi, supervalu, dunnes). Handles job definition lookup, chunk count, and dependency wiring.
disable-model-invocation: true
---

Submit AWS Batch scraping jobs. Usage: `/submit-jobs [retailer...]` — omit retailer to submit all active retailers.

## What to do

1. Check `lambda/handler.py` for the current set of active retailers, job definitions, and TOTAL_CHUNKS.
2. For each retailer requested (or all active ones):
   - If the retailer has an IDs job (aldi, supervalu, dunnes do; tesco does not), submit it first: aldi uses `ie_supermarket_ids_job_definition`, supervalu and dunnes use `ie_supermarket_batch_job_definition` with command `python {retailer}_ids.py`.
   - Submit `TOTAL_CHUNKS` API scraper jobs using `ie_supermarket_batch_job_definition`, passing `CHUNK_ID` and `TOTAL_CHUNKS` as container overrides.
   - If the API jobs depend on an IDs job, wire `dependsOn` using the IDs job ID.
3. Run the submission via the AWS CLI (`aws batch submit-job ...`) or remind the user they can trigger the Lambda directly.

## Key constants (verify against lambda/handler.py before running)

- Job queue: `getting-started-fargate-job-queue`
- Scraper job definition: `ie_supermarket_batch_job_definition`
- IDs job definition: `ie_supermarket_ids_job_definition`
- TOTAL_CHUNKS: check handler.py (currently 5, env default)
- Region: `eu-west-1`

## Shortcut

The Lambda at the schedule trigger submits all active retailers automatically. To invoke it manually:
```bash
aws lambda invoke --function-name supermarket_batch_scheduler --region eu-west-1 /dev/stdout
```
The function is `supermarket_batch_scheduler` (see `.github/workflows/aws.yml`).

## After submitting

Run `/check-jobs` to monitor progress.
