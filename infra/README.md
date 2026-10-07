# Scraper alerting (drafted, not applied)

`batch-alerts.yaml` creates:

- SNS topic `supermarket-scraper-alerts` with an email subscription (confirm the email it sends).
- EventBridge rule: any AWS Batch job in the queue reaching `FAILED` publishes to the topic. The
  scrapers exit non-zero when the data-quality gate fails (see `docs/scraper_data_quality.md`), so
  an all-NULL Tesco run now shows up as a FAILED job.
- Lambda `supermarket_summary_check`, run daily (default 14:00 UTC), that reads
  `raw/{retailer}/{today}/_run_summary_chunk{N}.json` and `raw/{retailer}/ids/_run_summary_{today}.json`
  and emails if a summary is missing, has `status != ok`, or an ID refresh was `rejected`. This
  catches the two cases Batch cannot: a chunk that never ran, and ID jobs that deliberately exit 0
  (so dependent scrapers still run on the previous IDs).

## Apply

```bash
export AWS_REGION=eu-west-1
aws cloudformation deploy \
  --template-file infra/batch-alerts.yaml \
  --stack-name supermarket-scraper-alerts \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides AlertEmail=you@example.com TotalChunks=5
```

`TotalChunks` must match `TOTAL_CHUNKS` in `lambda/handler.py` (default 5). Adjust `CheckSchedule`
to run after the slowest retailer normally finishes (Tesco can take ~3h+). Then click the
confirmation link in the subscription email.

## Test

```bash
aws sns publish --topic-arn $(aws cloudformation describe-stacks --stack-name supermarket-scraper-alerts \
  --query 'Stacks[0].Outputs[0].OutputValue' --output text) --subject test --message "alert path ok"
aws lambda invoke --function-name supermarket_summary_check /dev/stdout
```

## Remove

```bash
aws cloudformation delete-stack --stack-name supermarket-scraper-alerts
```

## Also worth doing (not in the template)

- The Batch job definition `retryStrategy` only retries Spot interruptions, so a gate failure is not
  retried; that is intended (the checkpoint is kept for a manual re-run).
- The local AWS CLI profile is the account root user; create a scoped IAM user/role for day-to-day use.
