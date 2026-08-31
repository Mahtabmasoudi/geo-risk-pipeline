"""
Orchestrator: runs the full pipeline end to end.

    ingest -> quality checks (pre) -> transform -> quality checks (post) -> load -> report

Each stage is a plain function call here rather than a DAG framework, but
the stages map directly onto an Airflow DAG or a Step Functions state
machine - see docs/architecture.md for how each function here would become
a task/state if this were deployed on AWS.

Usage:
    python pipeline.py
"""

import sys
import time

from src.ingest import load_hazard_raster_meta, load_regions
from src.pipeline_utils import get_logger, load_config, project_path
from src.quality_checks import (
    DataQualityError,
    run_post_transform_checks,
    run_pre_transform_checks,
)
from src.report import generate as generate_report
from src.transform import compute_regional_hazard_stats
from src.warehouse import load as load_warehouse

logger = get_logger("pipeline")


def run() -> int:
    start = time.time()
    config = load_config()
    raster_path = project_path(config["sources"]["hazard_raster"])

    logger.info("=== Stage 1/5: ingest ===")
    regions = load_regions()
    raster_meta = load_hazard_raster_meta()

    logger.info("=== Stage 2/5: quality checks (pre-transform) ===")
    try:
        run_pre_transform_checks(regions, raster_path)
    except DataQualityError as e:
        logger.error(f"Aborting run: {e}")
        return 1

    logger.info("=== Stage 3/5: transform (zonal statistics) ===")
    hazard_stats = compute_regional_hazard_stats(regions, str(raster_path))

    logger.info("=== Stage 4/5: quality checks (post-transform) + load ===")
    try:
        run_post_transform_checks(hazard_stats)
    except DataQualityError as e:
        logger.error(f"Aborting run: {e}")
        return 1

    run_meta = {
        "vector_source": config["sources"]["regions_vector"],
        "raster_source": config["sources"]["hazard_raster"],
        "region_count": len(regions),
        "raster_crs": raster_meta["crs"],
        "raster_width": raster_meta["width"],
        "raster_height": raster_meta["height"],
        "status": "success",
        "notes": None,
    }
    run_id = load_warehouse(regions, hazard_stats, run_meta)

    logger.info("=== Stage 5/5: report ===")
    result = generate_report(run_id)

    elapsed = time.time() - start
    logger.info(f"Pipeline run {run_id} complete in {elapsed:.1f}s. {result['row_count']} regions reported.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
