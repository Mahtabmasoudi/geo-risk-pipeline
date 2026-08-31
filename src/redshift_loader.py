"""
Optional stage: load the warehouse tables into a real Redshift cluster
(or Redshift Serverless workgroup).

I did not run this against a live cluster while building this project.
Reasons, plainly:
  - Redshift has no forever-free tier the way DynamoDB/Lambda do, and no
    reliable free-trial terms I could confirm at the time of writing -
    provisioning one means a real, ongoing per-hour charge until it's
    torn down.
  - My own sandbox's network is locked to a handful of package registries
    and can't reach AWS at all, so there was nothing to test against even
    if a cluster existed.

The code below follows the documented redshift_connector API and Redshift's
documented COPY syntax. It's written to the same standard as the rest of
this project, but "written correctly" and "verified against a live
cluster" are different claims - be upfront about that distinction if this
comes up.

Loading strategy: export each warehouse table to CSV, upload those CSVs to
S3 (reusing cloud_sync.py's upload_file call), then issue a Redshift COPY
command per table. This is the standard way to bulk-load Redshift - row-by-
row INSERT statements are slow on Redshift because of its columnar, MPP
architecture (every INSERT round-trips through the leader node instead of
loading in parallel across compute nodes the way COPY does).

Setup this expects, none of it stored in this repo:
  - A Redshift cluster or Serverless workgroup, reachable from wherever
    this script runs.
  - An IAM role attached to that cluster with s3:GetObject on the bucket
    used below (a different role than the IAM user used for cloud_sync.py -
    this one is assumed by the Redshift service itself, not a person).
  - config.redshift.* filled in (host, port, database, user, iam_role_arn).
  - The REDSHIFT_PASSWORD environment variable set locally. Never put a
    database password in pipeline_config.yaml - that file is meant to be
    committed to git.
"""

import csv
import os

from src.pipeline_utils import get_logger, load_config, project_path
from src.warehouse import get_connection

logger = get_logger("redshift_loader")

TABLES = ["dim_region", "fact_regional_risk", "pipeline_runs"]


def _export_table_to_csv(con, table_name: str, out_dir) -> str:
    """Dump a warehouse table to CSV so it can be uploaded to S3 for COPY."""
    rows = con.execute(f"SELECT * FROM {table_name}").fetchall()
    columns = [desc[0] for desc in con.description]

    out_path = out_dir / f"{table_name}.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)

    logger.info(f"Exported {len(rows)} rows from {table_name} to {out_path}")
    return str(out_path)


def _upload_csvs_to_s3(csv_paths: list[str], bucket: str, prefix: str) -> dict[str, str]:
    import boto3

    s3 = boto3.client("s3")
    s3_keys = {}

    for path in csv_paths:
        filename = os.path.basename(path)
        key = f"{prefix}/redshift_load/{filename}"
        s3.upload_file(path, bucket, key)
        s3_keys[filename.replace(".csv", "")] = key
        logger.info(f"Uploaded {path} -> s3://{bucket}/{key}")

    return s3_keys


def _run_copy_commands(redshift_conn, bucket: str, s3_keys: dict[str, str], iam_role_arn: str) -> None:
    cursor = redshift_conn.cursor()

    # Load order matters here: dim_region and pipeline_runs first since
    # fact_regional_risk has foreign keys pointing at both. Redshift won't
    # reject the load if this order is wrong (FKs are query hints only,
    # not enforced - see the comment in schema_redshift.sql), but the data
    # would be logically broken even though the COPY "succeeds".
    load_order = ["dim_region", "pipeline_runs", "fact_regional_risk"]

    for table_name in load_order:
        s3_path = f"s3://{bucket}/{s3_keys[table_name]}"
        copy_sql = f"""
            COPY {table_name}
            FROM '{s3_path}'
            IAM_ROLE '{iam_role_arn}'
            CSV
            IGNOREHEADER 1;
        """
        logger.info(f"Running COPY for {table_name} from {s3_path}")
        cursor.execute(copy_sql)

    redshift_conn.commit()


def load_to_redshift() -> None:
    config = load_config()
    rs_config = config["redshift"]

    if not rs_config.get("enabled"):
        logger.info("redshift.enabled is false in config - skipping Redshift load. See this file's docstring.")
        return

    password = os.environ.get("REDSHIFT_PASSWORD")
    if not password:
        raise ValueError(
            "REDSHIFT_PASSWORD environment variable is not set. "
            "Set it locally before enabling redshift.enabled - never put it in the config file."
        )

    import redshift_connector  # imported lazily, same reasoning as cloud_sync.py

    # 1. export warehouse tables to local CSV
    export_dir = project_path("data/processed")
    export_dir.mkdir(parents=True, exist_ok=True)
    con = get_connection()
    try:
        csv_paths = [_export_table_to_csv(con, table, export_dir) for table in TABLES]
    finally:
        con.close()

    # 2. upload those CSVs to S3 so Redshift's COPY command can read them
    aws_config = config["aws"]
    s3_keys = _upload_csvs_to_s3(csv_paths, aws_config["bucket"], aws_config["prefix"])

    # 3. connect to Redshift and run schema DDL + COPY commands
    redshift_conn = redshift_connector.connect(
        host=rs_config["host"],
        port=rs_config["port"],
        database=rs_config["database"],
        user=rs_config["user"],
        password=password,
    )
    try:
        schema_sql = project_path("sql/schema_redshift.sql").read_text()
        cursor = redshift_conn.cursor()
        cursor.execute(schema_sql)
        redshift_conn.commit()

        _run_copy_commands(redshift_conn, aws_config["bucket"], s3_keys, rs_config["iam_role_arn"])
    finally:
        redshift_conn.close()

    logger.info("Redshift load complete")


if __name__ == "__main__":
    load_to_redshift()
