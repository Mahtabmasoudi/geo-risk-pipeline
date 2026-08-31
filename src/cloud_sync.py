"""
Optional stage: push pipeline outputs to S3.

Off by default (config.aws.enabled: false). This whole repo runs fine
without AWS - it's here to show what wiring the pipeline up to real cloud
storage looks like, and because the target role calls for AWS experience
(S3/Glue/EMR/Redshift/Step Functions/EventBridge).

To actually use this:
  1. Have your own AWS account and an S3 bucket.
  2. Run `aws configure` on your own machine to set your credentials.
     Boto3 picks them up automatically from there - nothing goes in this
     repo, this config file, or any chat. Never paste AWS keys into a
     prompt to an AI assistant or commit them to git.
  3. Set aws.enabled: true and aws.bucket in config/pipeline_config.yaml.
  4. Run `python -m src.cloud_sync`.

I didn't run this module myself while building the repo - my sandbox's
network is locked down to a handful of package registries and can't reach
AWS, so there was nothing to test against. The code follows the standard
boto3 upload_file pattern; if `aws.enabled` stays false you'll never hit it.
"""

from src.pipeline_utils import get_logger, load_config, project_path

logger = get_logger("cloud_sync")


def upload_outputs_to_s3() -> None:
    config = load_config()
    aws_config = config["aws"]

    if not aws_config.get("enabled"):
        logger.info(
            "aws.enabled is false in config - skipping S3 upload. See this file's docstring to turn it on."
        )
        return

    import boto3  # imported lazily so boto3 isn't required unless this path is used

    bucket = aws_config["bucket"]
    prefix = aws_config.get("prefix", "")
    if not bucket:
        raise ValueError("config.aws.bucket is empty - set it before enabling aws.enabled")

    s3 = boto3.client("s3")

    files_to_upload = {
        project_path(config["outputs"]["summary_csv"]): f"{prefix}/regional_risk_summary.csv",
        project_path(config["outputs"]["map_png"]): f"{prefix}/risk_map.png",
        project_path(config["warehouse"]["path"]): f"{prefix}/risk_warehouse.duckdb",
    }

    for local_path, s3_key in files_to_upload.items():
        logger.info(f"Uploading {local_path} -> s3://{bucket}/{s3_key}")
        s3.upload_file(str(local_path), bucket, s3_key)

    logger.info(f"Upload complete: s3://{bucket}/{prefix}/")


if __name__ == "__main__":
    upload_outputs_to_s3()
