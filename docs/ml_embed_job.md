# Daily text-embedding job on AWS Batch (draft, nothing deployed)

Status (2026-10-29): drafted and tested locally only. No AWS resource, image, Hugging Face repo or
S3 object was created or changed. Files: `infra/embed/{Dockerfile.embed,Dockerfile.embed.dockerignore,requirements.txt,run_embed.py}`,
`infra/embed-batch.yaml`, `infra/iam/embed-job-role-policy.json`, `tests/test_embed_infra.py`.

## What it does

`run_embed.py` takes a lock, then for each spec in `EMBED_SPECS` (default `gemma2-text,harrier-title`, text
specs only) runs the framework's `load_catalogue` -> `run_embed` against the S3 store and writes a
summary. Only rows whose `(id, supermarket, content_key)` has no `ok` row for the spec are embedded
(new products and changed `title_cleaned`). It writes through `ml_common` unchanged: one
`part-*.parquet` per `CHUNK_ROWS` (1024) rows, never rewriting a part.

```
s3://ie-supermarket-data/ml/embeddings/<spec_id>/{manifest.json,part-*.parquet}   framework, append-only
s3://ie-supermarket-data/ml/_state/LOCK                                           this job
s3://ie-supermarket-data/ml/_run_summary/<YYYY-MM-DD>_<spec>.json                this job
```

Summary fields: `status ok|failed`, `failures`, `spec_id`, `catalogue_uri`, `catalogue_rows`, `pending`,
`embedded`, `failed`, `model_loaded`, `model_load_seconds`, `duration_seconds`, `peak_rss_mb`, `job_id`,
`runs_today`, `embedded_today` (a second run the same day adds to, not replaces, the day's total).

Exit codes: 0 = ok, nothing to do, or another run holds the lock; 1 = a spec failed. A spec fails when
`failed >= EMBED_MIN_FAILURES (3)` and `failed / attempted > EMBED_MAX_FAILURE_RATE (0.05)`, or it raised
(hub unreachable, catalogue missing: `load_catalogue` raises on a missing file/column). One spec's
failure does not stop the other. On failure: SNS `embed FAILED for <date>` (and the existing
`supermarket-batch-job-failed` rule also emails, as it does for dbt). On success it emails only when
rows were embedded; a no-op run is silent.

## Trigger flow

```
dbt-daily job (def ie_supermarket_dbt_job_definition) SUCCEEDED
  -> EventBridge rule supermarket-embed-after-dbt (queue + jobName dbt-daily + jobDefinition prefix)
  -> batch:SubmitJob embed-daily (def ie_supermarket_embed_job_definition, newest ACTIVE revision)
```

The rule matches the revisioned ARN by `prefix` (`...:job-definition/ie_supermarket_dbt_job_definition:`).
The embed job's own SUCCEEDED event does not match (different name and definition), so there is no loop.
The target passes no event data: the job uses today's UTC date and reads the catalogue as it is.
The rule deploys `DISABLED`.

(a) **Quiet dbt slots.** dbt exits 0 on every non-failing slot (nothing ready, already built, degraded),
so expect up to 4 events a day. The embed job handles that: `run_embed` diffs first and only calls the
embedder for pending rows; the model loads on that first call. Measured on a fully embedded store: 0
pending, no `torch` import, RSS 95 MB, model never loaded (`model_loaded: false`; enforced in
`test_a_run_with_nothing_new_never_loads_a_model`). Cost of a no-op: container start + reading the
`(id, supermarket, content_key)` columns of every part (7.1 s for gemma2-text's 49 parts from the Mac;
in-region should be faster).

(b) **Concurrency.** `_state/LOCK` is an S3 conditional create (`IfNoneMatch: *`, as the dbt job's lock).
The holder rewrites it before every chunk, so a stale lock means "no heartbeat for 30 minutes"
(`LOCK_MAX_AGE_MINUTES`), not "old"; a run that finds a live lock exits 0 quietly (the existing FAILED
rule would double-email an alert). Release only deletes a lock still carrying this job's id. Trade-off:
if run A read the catalogue before a later dbt build rewrote it and run B skipped on the lock, those
rows wait for the next event or day (at most ~1 day, and only while a long run is going, i.e. the first
harrier-title run). Batch job-name dedupe was not used; Batch allows duplicate names.

(c) **Degraded days.** `processed/` is only rewritten when all four retailers built, so a degraded
day leaves the catalogue unchanged and the embed job finds nothing new. Fine.

**Shadow week.** The shadow dbt schedule also produces SUCCEEDED events, and shadow writes
`processed-shadow/`. Deploy with `TriggerState=DISABLED` and run by hand with
`CatalogueUri=s3://ie-supermarket-data/processed-shadow/all_supermarket_products.parquet` (the role
can read both prefixes), or enable the rule with that parameter. After cutover, redeploy with the
default `processed/`.

## Measurements

Mac, CPU only (`device="cpu"`, `torch.set_num_threads(4)`, `CUDA_VISIBLE_DEVICES=`), 1,000 random real
`title_cleaned` values (mean 36 chars), the framework's `SentenceTransformerEmbedder`. The machine was
shared with other work (load average 4-5 of 10 cores) during part of the run, so each spec has an idle
and a contended figure.

| | gemma2-text (768-d fp32) | harrier-title (640-d) |
|---|---|---|
| model on disk | 1.4 GB | 544 MB |
| model load, files cached | 8.6 s (12.7 s contended) | 11.1 s (first load incl. torch import; 2.9 s second in one process) |
| 1,000 titles, idle | 8.9 s = 113/s | 13.6 s = 74/s |
| 1,000 titles, contended | 20-27 s = 37-50/s | 35 s = 29/s |
| peak RSS | 2.0 GB (1.8-2.3) | 1.95 GB |
| download, unauthenticated, this link | 15 s for 1.4 GB | 9 s for 544 MB |

Both specs in one process (the entrypoint, with `gc.collect()` between specs) stayed near 2 GB, not 4 GB.
Re-running the same 1,000 rows embedded 0 in 0.01 s. Fresh-venv first imports of torch took ~47 s on
the Mac (uncompiled `.pyc`); the image compiles them at build time.

A Fargate vCPU is a hyperthread of an x86 core, so 4 vCPU is roughly 2 cores, and no x86 or Fargate run
was possible from here (no Docker). Assuming 3-5x slower than the idle Mac (about the contended figures):
gemma2-text 15-40 rows/s, harrier-title 10-25 rows/s.

| work | estimate on 4 vCPU Fargate |
|---|---|
| daily increment (~90 new or changed titles; 559 rows differ between `processed/` of 10-23 and `processed-shadow/` of 10-29, 6 days) | download 30-90 s + load 10-30 s + embed 5-15 s per spec: about 3-5 min per run |
| quiet slot | 1-2 min (image pull + diff), model not loaded |
| first harrier-title run (no table exists: 50,042 rows pending) | 35-85 min |
| full 50k re-embed of gemma2-text | 20-55 min |

Sizing: peak RSS ~2 GB, and 8 GB is Fargate's minimum for 4 vCPU, so the job definition is 4 vCPU / 8192 MB
(2 vCPU / 4 GB would hold the memory but double the first-run time). Timeout 10,800 s covers the slow
estimate of the first harrier run with margin. `TORCH_THREADS=OMP_NUM_THREADS=4` is set because a
container can report the host's CPU count. **These Fargate numbers are extrapolations; the first manual
run (deploy step 4) gives the real ones in `_run_summary`.**

### Costs (eu-west-1 list prices from memory, verify; queue uses Fargate Spot)

4 vCPU + 8 GB is about $0.20/h on demand and about $0.06/h on Spot. No-op slot ~2 min: <$0.01. Increment
run ~5 min: ~$0.02. Four events a day, mostly no-ops: ~$0.05-0.10/day, **~$1-3/month**. First harrier run
~$0.1-0.3 once. ECR: ~1 GB image, ~$0.10/month per retained tag (the repo has 139 images and no lifecycle policy).

## Decision: download models at start, do not bake them

Both repos are public and ungated (checked 2026-10-29 through the Hub API: `gated: false, private: false`),
and the revisions are pinned in `specs.py`, so `HF_TOKEN` is not needed. Download: 2 GB in ~25 s on this link, 1-2 min expected on Fargate, only on runs with pending rows.

Baking would add ~2 GB of incompressible weights to every image. `aws.yml` builds with no layer cache on
every push and the model layer's digest would change each build, so each push stores another ~2 GB in ECR
(no lifecycle policy), and Fargate pulls the whole image (~3 GB) for every run, including the 3 of 4
daily runs that do nothing. The unbaked image is ~1 GB. Cost of not baking: a Hub outage on a day
with new rows fails the job (alert; next event retries); the Hub can rate-limit unauthenticated downloads
from shared Fargate IPs (set `HF_TOKEN` as a Batch secret if it happens); `harrier-title` runs
`trust_remote_code` from the pinned revision. If those matter, the fallback is a one-time mirror to
`s3://.../ml/models/` and a `snapshot_download` from there; not built.

## Patches to existing files (for you to apply)

1. `.github/workflows/aws.yml`: a `build-embed-image` job (tag `embed-<sha>`), built only when
   `infra/embed`, `ml_common` or `scraper_common` changed since the previous push (the dbt job rebuilds
   on every push; this image is ~1 GB), registering a new revision of the job definition once the
   stack exists. `git apply --check -p1` passes against the current file.
   If the deploy credentials are not root, they also need `iam:PassRole` on
   `arn:aws:iam::557690579730:role/supermarket-embed-job-role` (`infra/iam/github-deploy-policy.json`'s
   `PassBatchRoles`); not patched because that draft file's ARNs are garbled.

```diff
--- a/.github/workflows/aws.yml
+++ b/.github/workflows/aws.yml
@@ -148,3 +148,69 @@
           ' job-def.json > new-job-def.json
           aws batch register-job-definition --cli-input-json file://new-job-def.json
         done
+
+  build-embed-image:
+    name: Build and push embed image
+    runs-on: ubuntu-latest
+    environment: production
+    steps:
+    - name: Checkout
+      uses: actions/checkout@v4
+      with:
+        fetch-depth: 0
+
+    # The image is ~1 GB with no layer cache, so build it only when its inputs changed.
+    - name: Did the embed image inputs change?
+      id: changed
+      run: |
+        BASE="${{ github.event.before }}"
+        if [ -z "$BASE" ] || ! git cat-file -e "$BASE^{commit}" 2>/dev/null \
+           || ! git diff --quiet "$BASE" "$GITHUB_SHA" -- infra/embed ml_common scraper_common; then
+          echo "changed=true" >> $GITHUB_OUTPUT
+        else
+          echo "changed=false" >> $GITHUB_OUTPUT
+        fi
+
+    - name: Configure AWS credentials
+      if: steps.changed.outputs.changed == 'true'
+      uses: aws-actions/configure-aws-credentials@v4
+      with:
+        aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
+        aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
+        aws-region: ${{ env.AWS_REGION }}
+
+    - name: Login to Amazon ECR
+      if: steps.changed.outputs.changed == 'true'
+      id: login-ecr
+      uses: aws-actions/amazon-ecr-login@v2
+
+    - name: Build and push embed image
+      if: steps.changed.outputs.changed == 'true'
+      id: build-embed
+      env:
+        ECR_REGISTRY: ${{ steps.login-ecr.outputs.registry }}
+        IMAGE_TAG: embed-${{ github.sha }}
+      run: |
+        # BuildKit picks up infra/embed/Dockerfile.embed.dockerignore
+        docker build -f infra/embed/Dockerfile.embed -t $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG .
+        docker push $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG
+        echo "image=$ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG" >> $GITHUB_OUTPUT
+
+    - name: Update Batch job definition — embed image (once infra/embed-batch.yaml is deployed)
+      if: steps.changed.outputs.changed == 'true'
+      env:
+        EMBED_IMAGE: ${{ steps.build-embed.outputs.image }}
+      run: |
+        name=ie_supermarket_embed_job_definition
+        aws batch describe-job-definitions --job-definition-name $name --status ACTIVE \
+          --query 'max_by(jobDefinitions, &revision)' > job-def.json
+        if [ "$(cat job-def.json)" = "null" ] || [ ! -s job-def.json ]; then
+          echo "$name not deployed yet; skipping"
+          exit 0
+        fi
+        # .tags: CloudFormation adds aws:* tags, which RegisterJobDefinition rejects
+        jq --arg IMAGE_URI "$EMBED_IMAGE" '
+          .containerProperties.image = $IMAGE_URI |
+          del(.jobDefinitionArn, .revision, .status, .containerOrchestrationType, .tags)
+        ' job-def.json > new-job-def.json
+        aws batch register-job-definition --cli-input-json file://new-job-def.json
```

2. Optional, `docs/dbt_orchestration_plan.md` "Not written: ... the event-driven trigger" and
   `infra/README.md`: add a pointer to this document. `infra/dbt/run_dbt.py` and `infra/dbt-batch.yaml`
   need no change for this job. If the dbt build should later read the embeddings (the `tag:embeddings`
   models that `run_dbt.py` excludes), `dbt-job-role-policy.json` needs `s3:GetObject` and list on
   `ml/*`; note the embed job runs after dbt, so dbt would see the previous run's vectors.

## Deploy steps, in order (nothing done yet)

Run from the repo root with `AWS_REGION=eu-west-1`.

1. **Review and apply the patch above, push to `local_implementation`.** Risk: low. CI builds and pushes
   `docker-images:embed-<sha>` (the first build of this Dockerfile has never been run: no Docker here;
   expect to fix a Dockerfile issue, e.g. a missing system library, on the first CI run). The job-definition step
   skips until the stack exists. Alternatively build locally with `--platform linux/amd64` and push by hand.
2. **Deploy the stack with the trigger off.**
   `aws cloudformation deploy --template-file infra/embed-batch.yaml --stack-name supermarket-embed-batch --capabilities CAPABILITY_NAMED_IAM --parameter-overrides Image=<ECR URI> JobQueueArn=arn:aws:batch:eu-west-1:557690579730:job-queue/getting-started-fargate-job-queue ExecutionRoleArn=arn:aws:iam::557690579730:role/aws_batch_2 AlertTopicArn=arn:aws:sns:eu-west-1:557690579730:supermarket-scraper-alerts`.
   Risk: low; creates a role, a job definition, a disabled rule. `validate-template` passes.
3. **Run `gemma2-text` alone against the real store** (`EMBED_SPECS=gemma2-text`, default catalogue or
   the shadow one). The store already holds all 50,042 rows, so it should embed only the ~559 differing
   rows (shadow catalogue) or 0. Check `_run_summary` and that no existing part changed. Risk: low; append-only
   writes. If it re-embeds ~50k, the spec id changed: stop (the unit test guards this).
4. **First `harrier-title` run** (the full 50,042 rows, ~35-85 min, ~$0.1-0.3). Record the real
   load/throughput/RSS from its summary and adjust the vCPU/timeout if needed. Risk: medium for cost/time only.
   Decision needed first (below).
5. **Enable the trigger** (`TriggerState=ENABLED`, re-deploy). Watch the next dbt SUCCEEDED: a Batch job
   `embed-daily` should appear within a minute. Risk: low; worst case a failed job and an alert.
   Do this only after the dbt cutover, or point `CatalogueUri` at `processed-shadow/` during the shadow week.

Rollback: disable the rule; delete the stack; `ml/` tables are append-only and can simply be left.

## Risks and follow-ups

- Fargate throughput and the image build are unmeasured/untested (above).
- The store grows one small part per spec per day with new rows; `read_done` reads every part (7 s at 49
  parts from the Mac), so after a year (~400 parts) the diff step costs about a minute. The framework
  has no compaction yet.
- A reclaimed Spot task fails the job; the heartbeat lock goes stale after 30 min and the diff resumes from
  the parts already written (re-submit manually or wait for the next event).
- `ListBucket` is deliberately not prefix-conditioned: s3fs's `makedirs` probes the bucket root, which a
  `s3:prefix` condition would deny and then fall into `create_bucket`. Verified with
  `iam simulate-custom-policy` (ml/ read-write allowed; processed/ write, raw/ read and ml/embeddings delete denied).
- Persistent per-row errors below the failure threshold are retried daily and stay quiet.
- The legacy `processed/embeddings/<slug>/product_title_embeddings.parquet` that dbt and `ml/agents.py`
  read is still produced only by `ml/embeddings.py`; this job does not replace it.
