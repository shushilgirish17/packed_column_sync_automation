# infra - CloudFormation for Packed Column Sync

`template.yaml` creates the whole AWS side for one environment (dev / qa / prod):

| Resource | Name | Purpose |
|---|---|---|
| Secrets Manager secret | `packed-column-sync/<env>/benchling` | Benchling App `client_secret` (url + client_id are env vars) |
| S3 bucket | `packed-column-sync-<env>-<account>` | Job packages `unicorn-sync/incoming/<stem>.zip` + `.job.json` (retained on stack delete) |
| SQS FIFO queue | `packed-column-sync-<env>.fifo` | One message per column, one at a time |
| Lambda + layer | `packed-column-sync-<env>` | Webhook, sync, async self-invoke, completion reports |
| Function URL | (output `WebhookUrl`) | Benchling webhook endpoint; every request is signature-verified |
| Log group | `/aws/lambda/packed-column-sync-<env>` | Execution + audit log (retained on stack delete) |
| IAM role | `packed-column-sync-<env>-lambda` | Least privilege on this stack's resources only |
| IAM managed policy | `packed-column-sync-<env>-unicorn-consumer` | For the on-prem UNICORN host |

## Before the first deploy (once per account)
1. **Artifact bucket** for the packaged code, e.g. `packed-column-sync-artifacts-<account>`.
2. **From Lilly:** VPC + private subnets whose NAT egress IP is on the Benchling tenant allowlist,
   and - if required - KMS key ARN, permissions boundary ARN, mandatory tags.
3. **Fill `params-<env>.json`** - replace every `<...>` value. Empty `""` = feature off.

## Deploy
Run from `unicorn-column-lambda/`. Add `--profile <profile> --region <region>` to every `aws` command.

1. Build the layer and code zips into `dist/`:
   ```bash
   python build.py
   ```
2. Lint:
   ```bash
   cfn-lint infra/template.yaml
   ```
   Two `W3002` warnings (local code paths) are expected - step 3 resolves them.
3. Upload the zips and write `infra/packaged.yaml` with their S3 locations:
   ```bash
   aws cloudformation package --template-file infra/template.yaml --s3-bucket packed-column-sync-artifacts-<account> --output-template-file infra/packaged.yaml
   ```
4. Create a change set and **review it before executing** (keep it as change-control evidence):
   ```bash
   aws cloudformation deploy --template-file infra/packaged.yaml --stack-name packed-column-sync-dev --parameter-overrides file://infra/params-dev.json --capabilities CAPABILITY_NAMED_IAM --no-execute-changeset --tags Application=PackedColumnSync Environment=dev
   ```
   Then run the same command without `--no-execute-changeset`.

## After the first deploy
1. **Set the Benchling client secret** (the stack creates it with a placeholder; the Lambda refuses to
   run until it is replaced). Put `{"client_secret": "<value>"}` in a local file, then:
   ```bash
   aws secretsmanager put-secret-value --secret-id packed-column-sync/dev/benchling --secret-string file://benchling-secret.json
   ```
   Delete the file afterwards. Stack updates never overwrite this value.
2. **Benchling App** -> set the webhook URL to the `WebhookUrl` output. `AppDefinitionId` must match the app.
3. **UNICORN host:** attach `ConsumerPolicyArn` to its AWS identity (role or Identity Center permission
   set, by `ConsumerPolicyName`), then in the root `.env` set `SQS_QUEUE_URL` (= `QueueUrl`),
   `LAMBDA_FUNCTION_NAME` (= `FunctionName`), `AWS_PROFILE`, `AWS_REGION`.
4. **Smoke test:** one Active column from the canvas -> Queued -> picked up by `sqs_pull.py` /
   `watch_service.ps1` -> canvas shows Synced; audit line in CloudWatch.

Read all outputs with:
```bash
aws cloudformation describe-stacks --stack-name packed-column-sync-dev --query "Stacks[0].Outputs"
```

## Updates
Code change: `python build.py --code`, then steps 3-4. Dependency change: `python build.py`, then 3-4.
Parameter change: edit the params file, then step 4.

## Notes
- **KMS:** a customer key must allow `logs.<region>.amazonaws.com` (log group) and the UNICORN host
  identity (`kms:Decrypt`) in its key policy.
- **Visibility timeout** only has to cover the S3 download: `sqs_pull.py` deletes the message as soon
  as the job is in the watch folder, before UNICORN runs.
- **Deleting the stack** keeps the bucket and log group (audit evidence). The secret enters a 7-30 day
  recovery window, so re-creating the same env immediately needs
  `aws secretsmanager delete-secret --force-delete-without-recovery` or a different `NamePrefix`.
- **Webhook endpoint** is a public Function URL secured by Benchling's signature check. If Lilly
  requires API Gateway + WAF instead, replace `WebhookUrl` / its two permissions.
