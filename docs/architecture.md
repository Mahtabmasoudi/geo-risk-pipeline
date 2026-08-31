# Architecture

## What's actually running here

Everything in this repo runs locally with no cloud dependency:

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

## How this maps onto AWS

The role this repo is modeled after leans on Redshift, S3, Glue, EMR, Step
Functions, and EventBridge. None of that is wired up here (no AWS account
in the loop while building this), but each local piece has a fairly direct
cloud equivalent:

| Local piece | AWS equivalent | Notes |
|---|---|---|
| `data/raw/*.geojson`, `*.tif` | S3 raw zone (`s3://.../raw/`) | Partition by ingestion date; raw files land here untouched. |
| `data/processed/`, `outputs/` | S3 processed/curated zone | Output of the transform stage, ready for the warehouse or BI tools. |
| `src/ingest.py` | Lambda (small/scheduled) or a Glue Python shell job | Current volume is tiny; a Lambda triggered by EventBridge on a schedule would cover it. |
| `src/transform.py` (zonal stats) | AWS Glue (PySpark) or an EMR step | At real scale (hundreds of large rasters x thousands of polygons) this needs to be distributed - GeoPandas/rasterio don't parallelize across a cluster on their own. A Spark job using a library like `apache-sedona` for the raster/vector join is the natural upgrade path. |
| `src/quality_checks.py` | Glue Data Quality rules, or a Deequ job, with results pushed to CloudWatch | Same blocking-vs-advisory split: some checks should fail the pipeline, some should just alert. |
| DuckDB (`warehouse/risk_warehouse.duckdb`) | Redshift | Same star schema (`sql/schema.sql`) - `dim_region` / `fact_regional_risk` / `pipeline_runs` translate to Redshift DDL close to as-is (mainly swapping `nextval(sequence)` for an `IDENTITY` column). |
| `pipeline.py` (sequential function calls) | Step Functions state machine | Each stage becomes a state; failures branch to a notification/alerting state instead of a Python exception. |
| Manual `python pipeline.py` | EventBridge scheduled rule | Triggers the Step Functions execution on a schedule (or in response to an S3 `ObjectCreated` event when new raw data lands). |
| `src/cloud_sync.py` | boto3 S3 upload, gated by IAM role | Present in the repo but off by default - see that file's docstring for what it takes to turn on. |
| `src/report.py` (static CSV/PNG) | QuickSight dashboard reading from Redshift, or a scheduled job writing to a reporting materialized view | Static files are fine for a demo; a real dashboard should query the warehouse directly so it's always current. |

## Known limitations / what I'd fix next

- **Raster is synthetic.** `src/generate_hazard_raster.py` documents exactly how it's built. Swapping in a real hazard raster (WorldClim, NOAA NClimGrid, FEMA's National Risk Index) is just a matter of pointing `config.sources.hazard_raster` at a different single-band GeoTIFF - no other code changes.
- **Zonal stats run region-by-region in a Python loop.** Fine for 52 states; wouldn't scale to, say, county-level data nationwide or a large batch of rasters. That's the point where I'd move to a distributed approach (Spark/Sedona) rather than optimizing the Python loop further.
- **No incremental loading.** Every run reloads the full region set and inserts a full new set of fact rows. For a real deployment I'd want change-data-capture on the vector source and a way to only reprocess the polygons that actually changed.
- **AK/HI/PR fall outside the demo raster's CONUS bounding box**, so they show up in the warehouse with `pixel_count = 0` / `hazard_mean = NULL` rather than a fabricated score. `report.py` excludes them from the ranked output. A real raster with full US coverage would remove this gap entirely.
