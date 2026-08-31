# Data dictionary

## Sources

| Dataset | Path | Type | Origin |
|---|---|---|---|
| Region boundaries | `data/raw/regions.geojson` | Vector (GeoJSON, EPSG:4326) | Real US state boundaries + population density, pulled from [PublicaMundi/MappingAPI](https://github.com/PublicaMundi/MappingAPI) (a public GeoJSON mirror commonly used for choropleth demos). Re-fetch with `python -m src.ingest --fetch-regions`. |
| Hazard surface | `data/raw/hazard_surface.tif` | Raster (single-band GeoTIFF, float32, EPSG:4326) | Synthetically generated - see `src/generate_hazard_raster.py` for the exact method. Stands in for a real climate hazard raster (e.g. WorldClim, NOAA NClimGrid, FEMA National Risk Index). |

## Warehouse tables (`warehouse/risk_warehouse.duckdb`)

### `dim_region`

One row per region. Upserted, not versioned - if a region's name or
attributes need to change over time, this table would need slowly-changing-
dimension handling, which isn't implemented here.

| Column | Type | Description |
|---|---|---|
| `region_id` | INTEGER, PK | Surrogate key, assigned on first insert. |
| `region_name` | VARCHAR | Region name from the source vector's `name` property (e.g. "Texas"). Unique. |
| `region_type` | VARCHAR | Always `state` in this dataset; kept as a column so counties/other levels could be added later. |
| `population_density` | DOUBLE | People per unit area, from the source vector's `density` property. Units as provided by the source (not independently verified). |
| `source` | VARCHAR | Literal tag identifying where the row came from (`us-states-geojson`). |

### `fact_regional_risk`

Append-only: one row per `(region, pipeline run)`. Never updated in place,
so risk scores can be tracked over time as the raster/model inputs change.

| Column | Type | Description |
|---|---|---|
| `fact_id` | INTEGER, PK | Surrogate key. |
| `region_id` | INTEGER, FK -> `dim_region.region_id` | Which region this row scores. |
| `run_id` | VARCHAR, FK -> `pipeline_runs.run_id` | Which pipeline run produced this row. |
| `hazard_mean` | DOUBLE | Mean hazard index (0-100) across all raster pixels inside the region's boundary. `NULL` if the region has no overlapping raster pixels. |
| `hazard_min` / `hazard_max` | DOUBLE | Min/max pixel value within the region. |
| `hazard_stddev` | DOUBLE | Standard deviation of pixel values within the region - a rough signal of how spatially uniform the hazard is inside that region. |
| `pixel_count` | INTEGER | Number of valid (non-nodata) raster pixels that fell inside the region. `0` means no overlap with the raster extent. |
| `risk_tier` | VARCHAR | `Low` / `Medium` / `High` / `Severe`, derived from `hazard_mean` using the thresholds in `config/pipeline_config.yaml` (`risk_tiers`). `NULL` if `hazard_mean` is `NULL`. |

### `pipeline_runs`

One row per execution of `pipeline.py`. This is the metadata/lineage
record - it's what lets you answer "which raster and vector files produced
this report" after the fact.

| Column | Type | Description |
|---|---|---|
| `run_id` | VARCHAR, PK | UUID generated per run. |
| `run_timestamp` | TIMESTAMP | UTC time the run completed the load stage. |
| `vector_source` / `raster_source` | VARCHAR | Config-relative paths to the input files used for this run. |
| `region_count` | INTEGER | Number of regions processed. |
| `raster_crs`, `raster_width`, `raster_height` | VARCHAR / INTEGER | Metadata read directly off the raster file at ingest time. |
| `status` | VARCHAR | `success` or `failed`. |
| `notes` | VARCHAR | Free text, currently unused by the pipeline itself but there for manual annotation. |

## Risk tier thresholds

Defined in `config/pipeline_config.yaml` under `risk_tiers`, currently:

- **Low**: hazard_mean <= 25
- **Medium**: 25 < hazard_mean <= 50
- **High**: 50 < hazard_mean <= 75
- **Severe**: hazard_mean > 75

These are arbitrary round numbers for the demo, not derived from any real
risk model - swap in real thresholds once a real hazard raster is in use.

## DynamoDB table: `geo-risk-pipeline-region-risk`

A serving-layer cache, not a second copy of the warehouse. Synced from the
warehouse's *latest run only* by `src/dynamo_sync.py` - it does not carry
history the way `fact_regional_risk` does. If you need "what was Texas's
risk score three runs ago," that's a warehouse query, not a DynamoDB one.

| Attribute | Type | Description |
|---|---|---|
| `region_name` | String (partition key) | Matches `dim_region.region_name`. |
| `hazard_mean` | Number | Same value as `fact_regional_risk.hazard_mean` for the latest run. Omitted entirely (not written as null) for regions with no raster coverage, since DynamoDB has no natural null-vs-missing distinction worth relying on here. |
| `risk_tier` | String | `Low`/`Medium`/`High`/`Severe`, or `"Unknown"` if the region has no coverage. |
| `pixel_count` | Number | Same as the warehouse column. |
| `run_id` | String | Which pipeline run this snapshot came from - lets you tell how stale the cached value is. |
| `updated_at` | String (ISO 8601) | When `dynamo_sync.py` wrote this item, not when the pipeline run itself happened. |

Read by `aws_lambda/region_risk_lookup/lambda_function.py` via a single
`get_item` call keyed on `region_name` - the whole reason this table exists
is to make that a single-digit-millisecond lookup instead of a warehouse
query.

