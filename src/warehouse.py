"""
Load stage: write the transformed data into the DuckDB warehouse using the
star schema defined in sql/schema.sql.

DuckDB stands in for Redshift here - same star schema, same SQL, just
running embedded instead of as a managed cluster. dim_region is upserted
(one row per region, kept stable across runs); fact_regional_risk is
append-only so every pipeline run adds a new snapshot instead of overwriting
history.
"""

import uuid
from datetime import datetime, timezone

import duckdb
import geopandas as gpd

from src.pipeline_utils import get_logger, load_config, project_path

logger = get_logger("warehouse")


def get_connection() -> duckdb.DuckDBPyConnection:
    config = load_config()
    db_path = project_path(config["warehouse"]["path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    schema_path = project_path("sql/schema.sql")
    con.execute(schema_path.read_text())


def upsert_dim_region(con: duckdb.DuckDBPyConnection, regions: gpd.GeoDataFrame, name_field: str) -> None:
    """Insert any region not already present. Existing regions are left alone -
    boundaries and names don't change often enough to warrant SCD tracking here.
    """
    density_field = "density" if "density" in regions.columns else None

    for _, region in regions.iterrows():
        region_name = region[name_field]
        density = float(region[density_field]) if density_field and region[density_field] is not None else None

        exists = con.execute(
            "SELECT 1 FROM dim_region WHERE region_name = ?", [region_name]
        ).fetchone()

        if not exists:
            con.execute(
                """
                INSERT INTO dim_region (region_id, region_name, region_type, population_density, source)
                VALUES (nextval('seq_region_id'), ?, 'state', ?, 'us-states-geojson')
                """,
                [region_name, density],
            )

    count = con.execute("SELECT count(*) FROM dim_region").fetchone()[0]
    logger.info(f"dim_region now has {count} rows")


def record_pipeline_run(con: duckdb.DuckDBPyConnection, run_id: str, run_meta: dict) -> None:
    con.execute(
        """
        INSERT INTO pipeline_runs
            (run_id, run_timestamp, vector_source, raster_source, region_count,
             raster_crs, raster_width, raster_height, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            datetime.now(timezone.utc),
            run_meta.get("vector_source"),
            run_meta.get("raster_source"),
            run_meta.get("region_count"),
            run_meta.get("raster_crs"),
            run_meta.get("raster_width"),
            run_meta.get("raster_height"),
            run_meta.get("status", "success"),
            run_meta.get("notes"),
        ],
    )


def insert_fact_rows(con: duckdb.DuckDBPyConnection, run_id: str, hazard_stats: list[dict]) -> None:
    for row in hazard_stats:
        region_row = con.execute(
            "SELECT region_id FROM dim_region WHERE region_name = ?", [row["region_name"]]
        ).fetchone()
        if region_row is None:
            logger.warning(f"Skipping fact row for unknown region: {row['region_name']}")
            continue
        region_id = region_row[0]

        con.execute(
            """
            INSERT INTO fact_regional_risk
                (fact_id, region_id, run_id, hazard_mean, hazard_min, hazard_max,
                 hazard_stddev, pixel_count, risk_tier)
            VALUES (nextval('seq_fact_id'), ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                region_id,
                run_id,
                row["hazard_mean"],
                row["hazard_min"],
                row["hazard_max"],
                row["hazard_stddev"],
                row["pixel_count"],
                row["risk_tier"],
            ],
        )

    logger.info(f"Inserted {len(hazard_stats)} fact rows for run {run_id}")


def load(regions: gpd.GeoDataFrame, hazard_stats: list[dict], run_meta: dict) -> str:
    """Full load stage: schema init, dim upsert, fact insert, run record.
    Returns the run_id so the caller (pipeline.py) can reference it in the report.
    """
    config = load_config()
    name_field = config["sources"]["region_name_field"]
    run_id = str(uuid.uuid4())

    con = get_connection()
    try:
        init_schema(con)
        upsert_dim_region(con, regions, name_field)
        record_pipeline_run(con, run_id, run_meta)
        insert_fact_rows(con, run_id, hazard_stats)
    finally:
        con.close()

    return run_id
