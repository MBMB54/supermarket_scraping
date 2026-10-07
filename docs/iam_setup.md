# IAM setup (least privilege) - draft, nothing applied

Policies are in `infra/iam/`. They were drafted from a read-only inspection on 2026-10-07; no IAM
resources were created or changed. Account `557690579730`, region `eu-west-1`.

## What exists today

| Identity | Reality | Problem |
|---|---|---|
| Root user | has 1 access key (`AccountAccessKeysPresent=1`), MFA enabled; the local CLI uses it (`sts get-caller-identity` returns `:root`) | root keys can do anything; likely also the keys in GitHub secrets |
| IAM users | none | - |
| OIDC providers | none | GitHub deploys use long-lived keys |
| `aws_batch_2` | used as both `jobRoleArn` and `executionRoleArn` of both Batch job definitions. Attached: `AmazonEC2ContainerRegistryFullAccess`, `SecretsManagerReadWrite`, `AmazonS3FullAccess`, `CloudWatchFullAccessV2` | far too broad; no code in the repo uses Secrets Manager |
| `aldi_scraper_lambda-role-9m23mie2` | Lambda `supermarket_batch_scheduler` role. Attached: `AWSLambdaBasicExecutionRole`, `AWSBatchFullAccess`, `AmazonS3FullAccess` | the function only calls `batch:SubmitJob`; S3 is unused |
| ECR | repo `docker-images` | - |
| EventBridge | rule `supermarket-scraper-daily` (`cron(0 8 * * ? *)`) | - |

## Target identities

1. **GitHub Actions deploy role `supermarket-github-deploy`** (OIDC, no stored keys).
   Trust: `github-deploy-trust.json` (limited to repo `MBMB54/supermarket_scraping`, environment
   `production`, which `aws.yml` already uses). Permissions: `github-deploy-policy.json` (ECR push to
   `docker-images`, register Batch job definitions, PassRole for the two Batch roles, update the one
   Lambda).
2. **Batch job role `supermarket-batch-job-role`**: trust `batch-job-role-trust.json`, policy
   `batch-job-role-policy.json` (read/write/delete under `raw/*` only; the scrapers read ID parquet,
   write batches, checkpoints, summaries and Tesco debug screenshots, all under `raw/`).
   **Batch execution role `supermarket-batch-execution-role`**: same trust, managed policy
   `AmazonECSTaskExecutionRolePolicy` (ECR pull + CloudWatch logs). Splitting these two replaces `aws_batch_2`.
3. **Lambda role**: keep the existing role but replace `AWSBatchFullAccess` and `AmazonS3FullAccess`
   with `lambda-scheduler-policy.json` (SubmitJob on the queue and the two job definitions) and keep
   `AWSLambdaBasicExecutionRole`.
4. **Human**: preferably IAM Identity Center (SSO) with an admin permission set for you, plus the
   `human-operator-policy.json` for a day-to-day operator permission set (data access, Batch,
   logs, Lambda invoke, and the alert stack in `infra/`). If you stay with IAM users instead: one user,
   MFA, no root keys.

## Apply order (when permission is given)

```bash
# 1. OIDC provider (once)
aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
# 2. Deploy role
aws iam create-role --role-name supermarket-github-deploy --assume-role-policy-document file://infra/iam/github-deploy-trust.json
aws iam put-role-policy --role-name supermarket-github-deploy --policy-name deploy --policy-document file://infra/iam/github-deploy-policy.json
# 3. Batch roles
aws iam create-role --role-name supermarket-batch-job-role --assume-role-policy-document file://infra/iam/batch-job-role-trust.json
aws iam put-role-policy --role-name supermarket-batch-job-role --policy-name raw-bucket --policy-document file://infra/iam/batch-job-role-policy.json
aws iam create-role --role-name supermarket-batch-execution-role --assume-role-policy-document file://infra/iam/batch-job-role-trust.json
aws iam attach-role-policy --role-name supermarket-batch-execution-role --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
# 4. Lambda: put-role-policy lambda-scheduler-policy.json, then detach AWSBatchFullAccess + AmazonS3FullAccess
```

Then point the Batch job definitions at the new roles. The workflow's `jq` step copies the latest
revision, so add `| .containerProperties.jobRoleArn = "<job role arn>" | .containerProperties.executionRoleArn = "<exec role arn>"`
to both `jq` programs in `.github/workflows/aws.yml` (or register one revision by hand). Verify a
scraper job and the ID jobs succeed before detaching the old broad policies from `aws_batch_2`.

## What changes in the repo / locally

- **GitHub secrets**: delete `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (environment `production`); optionally add a variable `AWS_DEPLOY_ROLE_ARN`.
- **`.github/workflows/aws.yml`**: add under the existing `permissions:` block `id-token: write`; in the `Configure AWS credentials` step (lines 30-33) replace the two `aws-*-key` inputs with `role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}` (keep `aws-region`); add the two `jobRoleArn`/`executionRoleArn` jq assignments above. The `docker push`, `register-job-definition` and `update-function-code` steps are unchanged. `ci.yml` needs no AWS access.
- **Local `aws configure`**: stop using root keys. With SSO: `aws configure sso` (profile e.g. `supermarket-operator`, region `eu-west-1`, output `json`), then `export AWS_PROFILE=supermarket-operator`. Without SSO: create the IAM user, `aws configure --profile supermarket-operator`, then deactivate and delete the root access key (`aws iam delete-access-key` as root or via console). Note: `AWS_DEFAULT_OUTPUT` / config `output=` must be a valid type (`json`); an invalid value made some CLI calls fail with "Unknown output type" earlier.
- **Local scraper runs** only need read access to `raw/*/ids/latest/` when `OUTPUT_DIR` is set.

## Caveats

- The `iam:PassRole` condition and Batch `RegisterJobDefinition` (which has no resource-level scoping) mean the deploy role can register arbitrary job definitions using the two named roles only.
- `human-operator-policy.json` is broad for CloudFormation (`cloudformation:*`, `sns:*`, `events:*` on `*`); tighten it once the alert stack is deployed.
- Policies are untested against live IAM (no policy simulator run).
