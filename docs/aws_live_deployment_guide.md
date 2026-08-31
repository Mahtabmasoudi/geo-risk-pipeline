# Deploying the DynamoDB / Lambda / EventBridge / Step Functions extension

This covers the pieces from `docs/architecture.md` that are safe to
actually deploy - each has a real forever-free tier at this project's
scale. It does not cover Redshift or Glue; see `docs/architecture.md` for
why those were left as code-only.

Prerequisite: the S3 + IAM user setup from the main README already done
(a bucket exists, `aws configure` is set up locally with a scoped IAM user).

## 1. Create the DynamoDB table

Console -> DynamoDB -> Create table.
- Table name: `geo-risk-pipeline-region-risk`
- Partition key: `region_name` (String)
- Everything else: defaults (on-demand capacity mode is fine and is what
  the always-free tier is built around)

## 2. Add DynamoDB permissions to the IAM user

The IAM user created for S3 doesn't have DynamoDB access yet. Go to that
user in IAM -> Permissions -> Add permissions -> Create inline policy ->
JSON, and add a second policy using the IAM-user JSON in
`docs/architecture.md`'s appendix (swap in your real bucket name).

## 3. Sync data into the table

Locally:
```
# in config/pipeline_config.yaml, set dynamodb.enabled: true
python -m src.dynamo_sync
```
Should log `Synced 52 region risk records to DynamoDB table '...'`. Spot
check in the console: DynamoDB -> Tables -> your table -> Explore table
items.

## 4. Create the Lambda function

Console -> Lambda -> Create function -> Author from scratch.
- Name: `region-risk-lookup`
- Runtime: Python 3.12 (or whatever the latest available Python is)
- Execution role: create a new role, then after creation attach the
  Lambda-role JSON from `docs/architecture.md`'s appendix as an inline
  policy on that role (same flow as adding a policy to a user, but this
  time from the role's page in IAM instead of a user's page)

Once created, paste the entire contents of
`aws_lambda/region_risk_lookup/lambda_function.py` into the console's
inline code editor (it has no dependencies beyond boto3 and the standard
library, so there's nothing to package or upload). Click Deploy.

Then: Configuration tab -> Environment variables -> Add environment
variable -> key `TABLE_NAME`, value `geo-risk-pipeline-region-risk`.

Test it: the "Test" tab lets you invoke with a sample event. Use:
```json
{"region_name": "Texas"}
```
Expect a 200 response with Texas's risk data in the body.

## 5. Schedule it with EventBridge

Console -> EventBridge -> Rules -> Create rule.
- Name: `region-risk-lookup-heartbeat`
- Rule type: Schedule
- Schedule pattern: rate-based, e.g. `1 day`
- Target: Lambda function -> `region-risk-lookup`
- Optionally configure a fixed JSON input under "Additional settings" ->
  Constant, e.g. `{"region_name": "Texas"}`, so the scheduled invocation
  has something valid to look up

This is intentionally a simple heartbeat/health-check pattern - invoking
the function on a schedule so you can see in CloudWatch Logs that it's
still working, the same way a production system would schedule routine
checks or refreshes.

## 6. Build the Step Functions state machine

Console -> Step Functions -> Create state machine -> choose the code
editor (not the drag-and-drop designer, since we already have the
definition written).

Paste in `statemachine/region_risk_workflow.asl.json`, then replace
`REPLACE_WITH_LAMBDA_ARN` with the actual ARN of `region-risk-lookup`
(found at the top of the Lambda function's console page). Name the state
machine `region-risk-workflow` and create it.

Test it: Start execution, with input:
```json
{"region_name": "Florida"}
```
Florida should route to the `FlagForReview` branch (its risk tier is
Severe). Try a low-risk state like Washington to see the
`NoActionNeeded` branch instead.

## Teardown

None of this costs anything to leave running at this scale, but if you
want to remove it later: delete the Step Functions state machine, the
EventBridge rule, the Lambda function, the DynamoDB table, and finally the
IAM role/user - roughly the reverse of creation order, since some of these
reference each other.
