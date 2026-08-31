# Architecture

## What's actually running here

The core pipeline runs entirely locally with no cloud dependency:

```
data/raw/regions.geojson  ---\
                               >--  transform.py  --> DuckDB warehouse --> report.py --> outputs/
data/raw/hazard_surface.tif --/         (zonal stats)   (star schema)      (CSV + PNG)
```

`pipeline.py` runs the stages in order and stops the run if a blocking data
quality check fails (see `src/quality_checks.py`). Every run is recorded in
the `pipeline_runs` table with a run id, timestamp, and the source files it
used, so `fact_regional_risk` rows can always be traced back to how they
were produced.

## AWS extensions: what's real vs. what's documented

Being precise about this distinction matters more than the table looking
complete, so here's the honest status of every piece, not just what it maps
to conceptually.

### Actually implemented and deployed against real AWS resources

| Piece | What it does | Where |
|---|---|---|
| **S3** | Pipeline outputs (CSV, PNG, warehouse file) uploaded to a real bucket | `src/cloud_sync.py` |
| **IAM (user)** | A scoped IAM user with an inline policy limited to `PutObject`/`GetObject`/`ListBucket` on exactly one bucket - not `AmazonS3FullAccess` | console-created, policy JSON in this doc's appendix |
| **DynamoDB** | A `region_name`-keyed table synced from the warehouse's latest run, for fast single-region lookups (the warehouse is built for analytical queries, not point lookups - this is a serving-layer cache in front of it) | `src/dynamo_sync.py` |
| **Lambda** | A small data-service function: given a region name, returns its risk data from DynamoDB | `aws_lambda/region_risk_lookup/lambda_function.py` |
| **IAM (role)** | The Lambda's execution role - short-lived credentials the Lambda *service* assumes, scoped to `dynamodb:GetItem` on one table. This is a different half of "IAM roles and permissions" than the IAM user above: a **role** is assumed by a service, a **user** holds long-lived credentials for a person/script. Both appear in this project on purpose. | console-created, policy JSON in this doc's appendix |
| **EventBridge** | A scheduled rule invoking the Lambda periodically | console-created |
| **Step Functions** | A state machine that calls the Lambda, then branches (Choice state) on whether the returned `risk_tier` is High/Severe | `statemachine/region_risk_workflow.asl.json` |

DynamoDB, Lambda, and Step Functions each have a real forever-free tier at
the scale this project runs at (not a 12-month trial) - the entire
DynamoDB + Lambda + EventBridge + Step Functions extension costs $0/month
to keep running.

### Code written, not deployed

| Piece | What exists | Why it wasn't deployed |
|---|---|---|
| **Redshift** | A full loader (`src/redshift_loader.py`) using the documented `redshift_connector` API and Redshift's real `COPY FROM S3` bulk-load pattern (not row-by-row INSERT, which is slow on Redshift's MPP architecture). Redshift-specific DDL in `sql/schema_redshift.sql` - `IDENTITY` columns instead of DuckDB's sequences, explicit `DISTKEY`/`SORTKEY` choices. | No forever-free tier; a cluster or Serverless workgroup costs money per hour from the moment it exists until it's deleted. Provisioning one just to prove the loader works, then remembering to tear it down, wasn't worth the cost/risk for a demo repo. |
| **Glue** | A PySpark job (`glue_jobs/transform_zonal_stats_glue.py`) that distributes the same zonal-stats logic from `src/transform.py` across a cluster - broadcasts the (small) raster to every worker, partitions the region polygons. The core raster/vector overlay logic in it was tested locally against the real project data and produces identical numbers to `src/transform.py` (see the module docstring for that verification). What's *not* verified is the Glue job harness itself (`GlueContext`, `getResolvedOptions`, the Glue Data Catalog integration) - those only run inside an actual Glue job. | Glue has no free tier for job execution - billed per DPU-hour with a 1-minute minimum from job start, so even a quick test costs something. |
| **EMR** | Not written as separate code - `transform_zonal_stats_glue.py`'s core Spark logic would run on EMR with only the job-bootstrap code changed (Glue's `GlueContext`/`getResolvedOptions` swapped for a plain `SparkSession`). | Same reasoning as Glue, plus EMR needs its own EC2 fleet underneath, which is a bigger cost surface than Glue's managed DPUs. |

### Not touched at all

**Kinesis, Firehose** - this project's data isn't a stream (state boundaries
and a hazard raster don't arrive as an event feed), so there was no natural
place to use them without bolting on a use case that doesn't fit the rest
of the pipeline. If a real streaming source existed here - e.g. live sensor
readings feeding into risk scores - Kinesis/Firehose would sit between that
source and S3/Redshift.

## How the rest of the local stack maps onto AWS

| Local piece | AWS equivalent | Notes |
|---|---|---|
| `data/raw/*.geojson`, `*.tif` | S3 raw zone (`s3://.../raw/`) | Partition by ingestion date; raw files land here untouched. |
| `src/ingest.py` | Lambda (small/scheduled) or a Glue Python shell job | Current volume is tiny; a Lambda triggered by EventBridge on a schedule would cover it. |
| `src/transform.py` (zonal stats) | AWS Glue (PySpark) or an EMR step | See `glue_jobs/transform_zonal_stats_glue.py` above - this is the distributed version of this exact logic. |
| `src/quality_checks.py` | Glue Data Quality rules, or a Deequ job, with results pushed to CloudWatch | Same blocking-vs-advisory split as the local version: some checks should fail the pipeline, some should just alert. |
| DuckDB (`warehouse/risk_warehouse.duckdb`) | Redshift | Same star schema - `sql/schema_redshift.sql` is the Redshift-dialect version. |
| `pipeline.py` (sequential function calls) | Step Functions state machine | `statemachine/region_risk_workflow.asl.json` is a small real example of this pattern, not the full pipeline. |
| Manual `python pipeline.py` | EventBridge scheduled rule | Now real - see the table above. |
| `src/report.py` (static CSV/PNG) | QuickSight dashboard reading from Redshift, or a scheduled job writing to a reporting materialized view | Static files are fine for a demo; a real dashboard should query the warehouse directly so it's always current. |

## Known limitations / what I'd fix next

- **Raster is synthetic.** `src/generate_hazard_raster.py` documents exactly how it's built. Swapping in a real hazard raster (WorldClim, NOAA NClimGrid, FEMA's National Risk Index) is just a matter of pointing `config.sources.hazard_raster` at a different single-band GeoTIFF - no other code changes.
- **Local zonal stats run region-by-region in a Python loop.** Fine for 52 states; wouldn't scale to county-level data nationwide without the Glue/Spark approach above.
- **No incremental loading.** Every run reloads the full region set and inserts a full new set of fact rows. For a real deployment I'd want change-data-capture on the vector source and a way to only reprocess the polygons that actually changed.
- **AK/HI/PR fall outside the demo raster's CONUS bounding box**, so they show up in the warehouse with `pixel_count = 0` / `hazard_mean = NULL` rather than a fabricated score. `report.py` excludes them from the ranked output. A real raster with full US coverage would remove this gap entirely.
- **Redshift and Glue are unverified against live infrastructure**, as described above. If this project needed to prove those pieces work rather than just describe how they'd work, that would be the next thing to do, with the cost/teardown discipline that implies.

## Appendix: IAM policy JSON

**IAM user policy** (local dev - `aws configure` credentials used by `cloud_sync.py` and `dynamo_sync.py`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::YOUR-BUCKET-NAME",
        "arn:aws:s3:::YOUR-BUCKET-NAME/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:BatchWriteItem"],
      "Resource": "arn:aws:dynamodb:*:*:table/geo-risk-pipeline-region-risk"
    }
  ]
}
```

**Lambda execution role policy** (assumed by the Lambda service, not a person):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "dynamodb:GetItem",
      "Resource": "arn:aws:dynamodb:*:*:table/geo-risk-pipeline-region-risk"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

The logs permissions in the Lambda role aren't optional extras - without
them the function still runs, but nothing it logs reaches CloudWatch,
which makes debugging a deployed function far harder than it needs to be.
