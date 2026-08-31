"""
region-risk-lookup Lambda function.

A small data service: given a region name, returns its latest hazard score
and risk tier from DynamoDB. This is the "Develop data services and APIs
that support both internal and external system integrations" piece of the
job posting - a tiny, real example of that pattern, not the whole thing.

Deploy this by pasting the file contents directly into the Lambda
console's inline code editor - it only imports boto3 and the standard
library, both already present in every AWS Lambda Python runtime, so there
is nothing to package or upload.

Expected event shapes (handles both so it works whether invoked directly
for testing, or later put behind API Gateway):
    Direct test invoke:   {"region_name": "Texas"}
    API Gateway proxy:    {"queryStringParameters": {"region_name": "Texas"}}

Environment variable required:
    TABLE_NAME - the DynamoDB table name (e.g. geo-risk-pipeline-region-risk)
"""

import json
import os
from decimal import Decimal

import boto3

TABLE_NAME = os.environ.get("TABLE_NAME", "geo-risk-pipeline-region-risk")

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(TABLE_NAME)


class _DecimalEncoder(json.JSONEncoder):
    """DynamoDB returns numbers as Decimal, which json.dumps doesn't handle
    by default - convert to int/float for the response body.
    """

    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _extract_region_name(event: dict) -> str | None:
    if "region_name" in event:
        return event["region_name"]
    query_params = event.get("queryStringParameters") or {}
    return query_params.get("region_name")


def lambda_handler(event, context):
    region_name = _extract_region_name(event)

    if not region_name:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "region_name is required"}),
        }

    response = _table.get_item(Key={"region_name": region_name})
    item = response.get("Item")

    if item is None:
        return {
            "statusCode": 404,
            "body": json.dumps({"error": f"no risk data found for region '{region_name}'"}),
        }

    return {
        "statusCode": 200,
        "body": json.dumps(item, cls=_DecimalEncoder),
    }
