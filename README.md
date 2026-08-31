# geo-risk-pipeline

A small end-to-end data engineering pipeline that ingests geospatial data
(vector region boundaries + a raster hazard surface), computes zonal risk
statistics per region, loads the results into a warehouse using a star
schema, and produces a summary report and a map.

![risk map](outputs/risk_map.png)

## What it does

```
data/raw/regions.geojson  ---\
                               >-- transform.py --> DuckDB warehouse --> report.py --> outputs/
data/raw/hazard_surface.tif -/        (zonal stats)   (star schema)      (CSV + PNG)
```

1. **Ingest** - load US state boundaries (vector) and a hazard raster, repair
   any invalid geometries found along the way.
2. **Quality checks** - validate geometries, confirm the CRS is defined,
   check for duplicate region names before doing any real work.
3. **Transform** - for each region, overlay the hazard raster and compute
   zonal statistics (mean/min/max/stddev of the hazard index inside that
   region's boundary), then classify each region into a risk tier.
4. **Quality checks (again)** - make sure computed values are in the
   expected range, flag any regions with no raster coverage.
5. **Load** - upsert region dimension data and insert a new fact row per
   region for this run into a DuckDB warehouse (star schema).
6. **Report** - query the warehouse for the latest run and write out a
   summary CSV and a choropleth map.

Run the whole thing with:

```bash
python pipeline.py
```

## Setup

```bash
python -m venv venv
source venv/bin/activate     # venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Then:

```bash
# (optional) regenerate the demo hazard raster
python -m src.generate_hazard_raster

# run the full pipeline
python pipeline.py

# run the tests
pytest tests/ -v

# lint
ruff check .
```

Outputs land in `outputs/regional_risk_summary.csv` and
`outputs/risk_map.png`. The warehouse itself is at
`warehouse/risk_warehouse.duckdb` - poke around it directly if you want:

```bash
python3 -c "
import duckdb
con = duckdb.connect('warehouse/risk_warehouse.duckdb')
print(con.execute('''
    SELECT d.region_name, f.hazard_mean, f.risk_tier
    FROM fact_regional_risk f JOIN dim_region d ON f.region_id = d.region_id
    ORDER BY f.hazard_mean DESC LIMIT 10
''').fetchdf())
"
```

## Project structure

```
config/pipeline_config.yaml   all tunable settings - paths, thresholds, feature flags
data/raw/                     input files (real vector boundaries, generated raster)
src/
  ingest.py                   load + validate raw inputs
  generate_hazard_raster.py   builds the demo hazard raster
  quality_checks.py           data quality gate (blocking + advisory checks)
  transform.py                raster/vector zonal statistics
  warehouse.py                DuckDB load logic (star schema)
  report.py                   summary CSV + choropleth map
  cloud_sync.py                optional S3 upload, off by default
sql/schema.sql                warehouse DDL
pipeline.py                   orchestrates all of the above
tests/                        pytest unit tests
docs/
  architecture.md             how this maps onto an AWS deployment
  data_dictionary.md          column-level docs for every warehouse table
.github/workflows/ci.yml      lint + test + full pipeline run on every push
```

## On the data

- **Region boundaries are real.** US state polygons + population density,
  pulled from a public GeoJSON source (see `docs/data_dictionary.md` for
  the exact URL). While loading them I ran into a genuinely real data
  quality issue: Alaska's polygon crosses the antimeridian and a couple of
  East Coast states have self-intersecting coastline geometry, both of
  which `is_valid` flags as invalid. `src/ingest.py` repairs these with
  `shapely.make_valid()` instead of silently dropping the rows.
- **The hazard raster is synthetic.** Real climate/hazard rasters (WorldClim,
  NOAA NClimGrid, FEMA's National Risk Index) generally require an account,
  a large download, or licensing that doesn't make sense for a demo repo.
  `src/generate_hazard_raster.py` documents exactly how the substitute
  raster is built (smoothed noise + a north-south gradient) so it's not a
  black box. Swapping in a real raster is a one-line config change - see
  that file's docstring.

## Extending to AWS

The pipeline runs entirely locally (DuckDB instead of Redshift, local files
instead of S3). `docs/architecture.md` has a full table mapping each piece
to its AWS equivalent. `src/cloud_sync.py` has a working (but
by-default-disabled) S3 upload using boto3 - turning it on requires your
own AWS account and credentials configured locally via `aws configure`.

## Tech stack

Python, GeoPandas, rasterio, shapely, DuckDB, matplotlib, pytest, ruff,
GitHub Actions.

## Known limitations

See the bottom of `docs/architecture.md` - short version: the raster is
synthetic, zonal stats run in a plain Python loop (fine at 52 regions,
wouldn't scale to county-level data without moving to Spark), and there's
no incremental/CDC loading yet.
