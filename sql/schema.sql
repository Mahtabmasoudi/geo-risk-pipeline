-- Star schema for the risk warehouse.
-- One row per pipeline run in pipeline_runs, one row per region in
-- dim_region, one row per (region, run) in fact_regional_risk.
--
-- This is intentionally the same shape you'd use on Redshift - dim_region
-- as a slowly-changing dimension, fact_regional_risk as an append-only
-- fact table keyed by run so you can track how risk scores drift over time
-- as the raster/model inputs change. See docs/architecture.md for notes on
-- what changes if this were actually deployed on Redshift.

CREATE SEQUENCE IF NOT EXISTS seq_region_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_fact_id START 1;

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          VARCHAR PRIMARY KEY,
    run_timestamp   TIMESTAMP NOT NULL,
    vector_source   VARCHAR,
    raster_source   VARCHAR,
    region_count    INTEGER,
    raster_crs      VARCHAR,
    raster_width    INTEGER,
    raster_height   INTEGER,
    status          VARCHAR,       -- 'success' | 'failed'
    notes           VARCHAR
);

CREATE TABLE IF NOT EXISTS dim_region (
    region_id       INTEGER PRIMARY KEY DEFAULT nextval('seq_region_id'),
    region_name     VARCHAR NOT NULL UNIQUE,
    region_type     VARCHAR NOT NULL DEFAULT 'state',
    population_density DOUBLE,   -- from the source vector attributes
    source          VARCHAR
);

CREATE TABLE IF NOT EXISTS fact_regional_risk (
    fact_id         INTEGER PRIMARY KEY DEFAULT nextval('seq_fact_id'),
    region_id       INTEGER NOT NULL REFERENCES dim_region(region_id),
    run_id          VARCHAR NOT NULL REFERENCES pipeline_runs(run_id),
    hazard_mean     DOUBLE,
    hazard_min      DOUBLE,
    hazard_max      DOUBLE,
    hazard_stddev   DOUBLE,
    pixel_count     INTEGER,
    risk_tier       VARCHAR    -- 'Low' | 'Medium' | 'High' | 'Severe'
);
