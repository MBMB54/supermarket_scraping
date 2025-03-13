#!/bin/bash
categories=("frozen-303714" "best-of-fresh-294566" "food-cupboard-drinks-bakery-294572")

for category in "${categories[@]}"; do
  aws batch submit-job \
    --job-name "ocado-${category}" \
    --job-queue "getting-started-fargate-job-queue" \
    --job-definition "batch_scraper_job-defintion:7" \
    --container-overrides "command=[\"python\",\"marks.py\",\"--category\",\"${category}\"]" \
    --retry-strategy "attempts=3" \
    --timeout "attemptDurationSeconds=600"
done
