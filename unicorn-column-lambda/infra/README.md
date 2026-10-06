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
1. **Artifact bucket**: any existing S3 bucket in the deploy region, e.g. `packed-column-sync-artifacts-<account>`.
2. **From Lilly:** VPC + private subnets whose NAT egress IP is on the Benchling tenant allowlist,
   and - if required - KMS key ARN, permissions boundary ARN, mandatory tags.
3. **Fill `params-<env>.json`** - replace every `<...>` value. Empty `""` = feature off
   (`VpcId` + `SubnetIds` both empty = Lambda outside a VPC, which the Lilly IP allowlist will block).

## Deploy (console)
1. Build both zips into `dist/` (from `unicorn-column-lambda/`):
   ```bash
   python build.py
   ```
2. **S3 console** -> artifact bucket -> upload `dist/packed-column-sync.zip` and
   `dist/packed-column-sync-layer.zip` under the prefix `packed-column-sync/`
   (keys must match `CodeS3Key` / `LayerS3Key`).
3. **CloudFormation console** -> Create stack -> *With new resources* -> *Upload a template file* ->
   `infra/template.yaml`.
4. Stack name `packed-column-sync-dev`; fill the parameters from `params-dev.json`.
5. Add tags (e.g. `Application=PackedColumnSync`, `Environment=dev`), tick
   **"I acknowledge that AWS CloudFormation might create IAM resources with custom names"**.
6. Review, then Submit. Keep the change set / events as change-control evidence.

Same with the CLI (after step 2):
```bash
aws cloudformation deploy --template-file infra/template.yaml --stack-name packed-column-sync-dev --parameter-overrides file://infra/params-dev.json --capabilities CAPABILITY_NAMED_IAM --no-execute-changeset --tags Application=PackedColumnSync Environment=dev
```
Review the change set, then run it again without `--no-execute-changeset`.

## After the first deploy
1. **Set the Benchling client secret** (the stack creates it with a placeholder; the Lambda refuses to
   run until it is replaced). Put `{"client_secret": "<value>"}` in a local file, then:
   ```bash
   aws secretsmanager put-secret-value --secret-id packed-column-sync/dev/benchling --secret-string file://benchling-secret.json
   ```
   Delete the file afterwards. Stack updates never overwrite this value.
   Console alternative: Secrets Manager -> `packed-column-sync/dev/benchling` -> *Retrieve secret value*
   -> *Edit* -> Plaintext `{"client_secret":"<value>"}` -> Save.
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
- **Code change:** `python build.py --code`, upload the zip under a **new key** (e.g.
  `packed-column-sync/packed-column-sync-v2.zip`), then *Update stack* -> *Use current template* and set
  `CodeS3Key` to the new key. Re-using the same key does NOT update the Lambda.
- **Dependency change:** `python build.py`, new key for the layer zip, update `LayerS3Key` the same way.
- **Parameter change:** *Update stack* -> *Use current template* -> change the value.

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
