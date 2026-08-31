"""
AWS Glue job: distributed version of src/transform.py's zonal statistics.

Not deployed or run against a real Glue job - no Glue job resource exists
for this, and Glue has no free tier for job execution (billed per DPU-hour
from the moment a job starts, with a 1-minute minimum). Writing this
against Glue's real job structure and testing it locally against a live
job are different things; this is the former only.

Why this exists at all, given src/transform.py already does zonal stats:
that script loops over regions in plain Python, which is fine for 52
states and one small raster. It would not be fine for, say, county-level
data nationwide (~3,100 polygons) times a batch of dozens of raster tiles -
that's the point where a single Python process becomes the bottleneck and
you want the work spread across a cluster. This script is what that looks
like.

Design: the raster is broadcast (sent once to every worker) rather than
distributed, while the region polygons are the thing that gets
partitioned across the cluster. That only works because the hazard raster
here is small (a few hundred KB) - if the input were instead many large
raster tiles, you'd flip this around and partition the rasters, or reach
for a proper distributed spatial library like Apache Sedona instead of
this broadcast approach. For the state/county scale this project is built
around, broadcasting the raster is simpler and avoids adding a Sedona
dependency to the job.

Expected job parameters (set via --arg-name when creating the Glue job,
or in the console's "Job parameters" section):
    --RASTER_S3_PATH    s3://bucket/prefix/hazard_surface.tif
    --REGIONS_S3_PATH   s3://bucket/prefix/regions.geojson
    --OUTPUT_S3_PATH    s3://bucket/prefix/zonal_stats_output/
    --additional-python-modules  geopandas,rasterio,shapely  (Glue doesn't
        ship these by default - they need to be listed as a job parameter
        so Glue installs them into the job's Python environment on startup)
"""

import sys

import boto3
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import Row
from pyspark.sql.types import DoubleType, IntegerType, StringType, StructField, StructType

args = getResolvedOptions(sys.argv, ["JOB_NAME", "RASTER_S3_PATH", "REGIONS_S3_PATH", "OUTPUT_S3_PATH"])

sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(args["JOB_NAME"], args)


def _download_s3_object_to_bytes(s3_path: str) -> bytes:
    bucket, key = s3_path.replace("s3://", "").split("/", 1)
    obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


def _zonal_stats_for_partition(raster_bytes_bc):
    """Returns a function suitable for mapPartitions: opens the broadcast
    raster once per partition (not once per row - rasterio.MemoryFile has
    real open/close overhead you don't want to pay per polygon) and yields
    one Row per region in that partition.
    """

    def process_partition(region_rows):
        import numpy as np
        from rasterio.io import MemoryFile
        from rasterio.mask import mask
        from shapely import wkt

        with MemoryFile(raster_bytes_bc.value) as memfile, memfile.open() as raster_dataset:
            for row in region_rows:
                geometry = wkt.loads(row.geometry_wkt)
                try:
                    out_image, _ = mask(raster_dataset, [geometry], crop=True, nodata=raster_dataset.nodata)
                    valid_pixels = out_image[0][out_image[0] != raster_dataset.nodata]
                except ValueError:
                    valid_pixels = np.array([])

                if valid_pixels.size == 0:
                    yield Row(
                        region_name=row.region_name,
                        hazard_mean=None,
                        hazard_min=None,
                        hazard_max=None,
                        hazard_stddev=None,
                        pixel_count=0,
                    )
                else:
                    yield Row(
                        region_name=row.region_name,
                        hazard_mean=float(np.mean(valid_pixels)),
                        hazard_min=float(np.min(valid_pixels)),
                        hazard_max=float(np.max(valid_pixels)),
                        hazard_stddev=float(np.std(valid_pixels)),
                        pixel_count=int(valid_pixels.size),
                    )

    return process_partition


def main():
    # geopandas isn't a Spark-native reader, so the GeoJSON is read once on
    # the driver and turned into a small Spark DataFrame (region_name,
    # geometry_wkt) that gets distributed to workers from there. This is
    # fine because the *number of regions* is what needs to scale here
    # (thousands of counties), not the size of any single geometry.
    import geopandas as gpd

    regions_bytes = _download_s3_object_to_bytes(args["REGIONS_S3_PATH"])
    with open("/tmp/regions.geojson", "wb") as f:
        f.write(regions_bytes)
    regions_gdf = gpd.read_file("/tmp/regions.geojson")

    region_rows = [
        Row(region_name=row["name"], geometry_wkt=row.geometry.wkt) for _, row in regions_gdf.iterrows()
    ]
    regions_df = spark.createDataFrame(region_rows)

    raster_bytes = _download_s3_object_to_bytes(args["RASTER_S3_PATH"])
    raster_bytes_bc = sc.broadcast(raster_bytes)

    output_schema = StructType(
        [
            StructField("region_name", StringType()),
            StructField("hazard_mean", DoubleType()),
            StructField("hazard_min", DoubleType()),
            StructField("hazard_max", DoubleType()),
            StructField("hazard_stddev", DoubleType()),
            StructField("pixel_count", IntegerType()),
        ]
    )

    result_rdd = regions_df.rdd.mapPartitions(_zonal_stats_for_partition(raster_bytes_bc))
    result_df = spark.createDataFrame(result_rdd, schema=output_schema)

    result_df.write.mode("overwrite").parquet(args["OUTPUT_S3_PATH"])

    job.commit()


if __name__ == "__main__":
    main()
