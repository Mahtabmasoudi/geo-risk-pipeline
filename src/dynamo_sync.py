"""
Optional stage: sync the latest risk score per region into DynamoDB.

Why this exists, not just "because the job posting mentions non-relational
stores": the DuckDB/Redshift warehouse is built for analytical queries -
joins, aggregations, historical trends across runs. It's a bad fit for "an
app needs this one region's current risk score in under 10ms." That's
exactly the kind of access pattern DynamoDB is built for: a single-digit-ms
key-value lookup by region_name, no joins, no query planning.

So the pattern here is standard in real systems: DuckDB/Redshift as the
system of record for analysis, DynamoDB as a fast serving layer synced from
it. dynamo_region_risk_lookup (the Lambda in aws_lambda/) reads from this
table, not from the warehouse directly.

Off by default (config.dynamodb.enabled: false), same pattern as
cloud_sync.py. Requires the same local AWS credentials already set up via
`aws configure`, plus a dynamodb:PutItem/BatchWriteItem permission on the
IAM user - see docs/architecture.md for the updated policy JSON.
"""

from datetime import datetime, timezone
from decimal import Decimal

from src.pipeline_utils import get_logger, load_config
from src.warehouse import get_connection

logger = get_logger("dynamo_sync")


def _to_dynamo_number(value):
    """DynamoDB's Python SDK requires Decimal, not float, for number types -
    passing a raw float raises a TypeError. None passes through unchanged
    so we can omit missing values below rather than writing NaN.
    """
    if value is None:
        return None
    return Decimal(str(value))


def _fetch_latest_risk_rows(con) -> list[dict]:
    return con.execute(
        """
        SELECT
            d.region_name,
            f.hazard_mean,
            f.risk_tier,
            f.pixel_count,
            f.run_id
        FROM fact_regional_risk f
        JOIN dim_region d ON f.region_id = d.region_id
        WHERE f.run_id = (SELECT run_id FROM pipeline_runs ORDER BY run_timestamp DESC LIMIT 1)
        """
    ).fetchall()


def sync_latest_risk_to_dynamodb() -> int:
    config = load_config()
    dynamo_config = config["dynamodb"]

    if not dynamo_config.get("enabled"):
        logger.info(
            "dynamodb.enabled is false in config - skipping DynamoDB sync. See this file's docstring."
        )
        return 0

    import boto3  # imported lazily, same reasoning as cloud_sync.py

    table_name = dynamo_config["table_name"]
    table = boto3.resource("dynamodb").Table(table_name)

    con = get_connection()
    try:
        rows = _fetch_latest_risk_rows(con)
    finally:
        con.close()

    now = datetime.now(timezone.utc).isoformat()
    written = 0

    # batch_writer buffers and sends in batches of 25 (DynamoDB's own limit)
    # instead of one PutItem round-trip per region - the difference between
    # 52 API calls and ~3 for this dataset.
    with table.batch_writer() as batch:
        for region_name, hazard_mean, risk_tier, pixel_count, run_id in rows:
            item = {
                "region_name": region_name,
                "hazard_mean": _to_dynamo_number(hazard_mean),
                "risk_tier": risk_tier or "Unknown",
                "pixel_count": _to_dynamo_number(pixel_count),
                "run_id": run_id,
                "updated_at": now,
            }
            # DynamoDB rejects attributes with a None value outright -
            # strip them rather than writing a broken item for AK/HI/PR
            item = {k: v for k, v in item.items() if v is not None}
            batch.put_item(Item=item)
            written += 1

    logger.info(f"Synced {written} region risk records to DynamoDB table '{table_name}'")
    return written


if __name__ == "__main__":
    sync_latest_risk_to_dynamodb()
