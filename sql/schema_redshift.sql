-- Redshift version of sql/schema.sql.
--
-- Not identical to the DuckDB version - Redshift doesn't support
-- CREATE SEQUENCE / nextval() the way DuckDB and Postgres do. The
-- Redshift-idiomatic equivalent is an IDENTITY column, which is what's
-- used below. Everything else (table/column names, types, relationships)
-- matches sql/schema.sql exactly, so src/warehouse.py's queries would work
-- against either with no changes beyond the connection itself.
--
-- Also note: Redshift enforces primary/foreign keys as query-planning
-- hints only - it does not actually reject a bad insert that violates
-- them. That's a real, easy-to-miss difference from DuckDB/Postgres if
-- you're used to those enforcing constraints at write time.

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          VARCHAR(64) PRIMARY KEY,
    run_timestamp   TIMESTAMP NOT NULL,
    vector_source   VARCHAR(256),
    raster_source   VARCHAR(256),
    region_count    INTEGER,
    raster_crs      VARCHAR(32),
    raster_width    INTEGER,
    raster_height   INTEGER,
    status          VARCHAR(16),
    notes           VARCHAR(512)
);

CREATE TABLE IF NOT EXISTS dim_region (
    region_id       INTEGER IDENTITY(1, 1) PRIMARY KEY,
    region_name     VARCHAR(128) NOT NULL,
    region_type     VARCHAR(32) NOT NULL DEFAULT 'state',
    population_density DOUBLE PRECISION,
    source          VARCHAR(64)
)
DISTSTYLE ALL;  -- dim_region is small and joined constantly - copy it to every node rather than distributing it

CREATE TABLE IF NOT EXISTS fact_regional_risk (
    fact_id         INTEGER IDENTITY(1, 1) PRIMARY KEY,
    region_id       INTEGER NOT NULL REFERENCES dim_region(region_id),
    run_id          VARCHAR(64) NOT NULL REFERENCES pipeline_runs(run_id),
    hazard_mean     DOUBLE PRECISION,
    hazard_min      DOUBLE PRECISION,
    hazard_max      DOUBLE PRECISION,
    hazard_stddev   DOUBLE PRECISION,
    pixel_count     INTEGER,
    risk_tier       VARCHAR(16)
)
DISTSTYLE KEY
DISTKEY (region_id)   -- co-locate each region's fact rows with its dim_region row for fast joins
SORTKEY (run_id);     -- most queries filter to the latest run - sorting by run_id lets Redshift skip blocks for older runs
