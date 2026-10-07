---
name: check-jobs
description: Check the status of AWS Batch scraping jobs — counts by status (SUCCEEDED/FAILED/RUNNING/PENDING) and total products scraped from S3.
disable-model-invocation: true
---

Check the status of AWS Batch scraping jobs. Usage: `/check-jobs [retailer]` — omit retailer to check all.

## What to do

1. **Job status** — run for each active retailer:
   ```bash
   aws batch list-jobs --job-queue getting-started-fargate-job-queue \
     --job-status RUNNING --region eu-west-1 --query 'jobSummaryList[*].{name:jobName,status:status}' --output table
   ```
   Repeat with `--job-status SUCCEEDED`, `FAILED`, `PENDING`. Summarise counts per status.

2. **Products scraped** — count unique successful records in S3 for today's date:
   ```bash
   # List today's raw files
   aws s3 ls s3://ie-supermarket-data/raw/{retailer}/$(date -u +%Y-%m-%d)/ --region eu-west-1
   ```
   Then use a short Python snippet to count records:
   ```bash
   uv run python - <<'EOF'
   import polars as pl, boto3, gzip, json
   from datetime import datetime, UTC
   
   retailer = "tesco"  # change as needed
   date = datetime.now(UTC).strftime("%Y-%m-%d")
   bucket = "ie-supermarket-data"
   s3 = boto3.client("s3", region_name="eu-west-1")
   
   resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"raw/{retailer}/{date}/")
   keys = [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".jsonl.gz")]
   
   total = success = fail = 0
   seen_ids = set()
   for key in keys:
       obj = s3.get_object(Bucket=bucket, Key=key)
       with gzip.open(obj["Body"], "rt") as f:
           for line in f:
               r = json.loads(line)
               total += 1
               if r.get("data"):
                   seen_ids.add(r.get("product_id") or r.get("tpnc"))
                   success += 1
               else:
                   fail += 1
   print(f"Total records: {total}")
   print(f"Successful (data not null): {success}")
   print(f"Failed: {fail}")
   print(f"Unique product IDs scraped: {len(seen_ids)}")
   EOF
   ```

3. **Data-quality summaries** — each chunk writes `raw/{retailer}/{date}/_run_summary_chunk{N}.json` (`status`, `failures`, counts). Check for failures or missing chunks:
   ```bash
   for r in tesco aldi supervalu dunnes; do
     aws s3 cp --recursive --exclude '*' --include '_run_summary_chunk*.json' \
       s3://ie-supermarket-data/raw/$r/$(date -u +%Y-%m-%d)/ /tmp/summaries/$r/ --region eu-west-1 --quiet
     grep -L '"status": "ok"' /tmp/summaries/$r/*.json 2>/dev/null
   done
   ```
   (Tesco only has summaries once it adopts `scraper_common`.)

4. Report: jobs by status, products scraped, any FAILED job names, and chunks whose summary status is not `ok`.
