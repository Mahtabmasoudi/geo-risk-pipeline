"""
Data quality gate that runs between ingest and transform.

Each check returns a QualityResult. run_all() runs every check and raises
DataQualityError if any of them fail, so a broken run stops before it loads
bad numbers into the warehouse instead of silently producing a wrong report.
"""

from dataclasses import dataclass

import geopandas as gpd
import rasterio

from src.pipeline_utils import get_logger, load_config

logger = get_logger("quality_checks")


class DataQualityError(Exception):
    pass


@dataclass
class QualityResult:
    check_name: str
    passed: bool
    detail: str


def check_geometry_validity(regions: gpd.GeoDataFrame) -> QualityResult:
    invalid = regions[~regions.geometry.is_valid | regions.geometry.isna()]
    passed = len(invalid) == 0
    if passed:
        detail = "all geometries valid"
    else:
        bad_names = invalid["name"].tolist() if "name" in invalid else invalid.index.tolist()
        detail = f"{len(invalid)} invalid/null geometries: {bad_names}"
    return QualityResult("geometry_validity", passed, detail)


def check_crs_match(regions: gpd.GeoDataFrame, raster_path) -> QualityResult:
    with rasterio.open(raster_path) as src:
        raster_crs = src.crs
    vector_crs = regions.crs

    # We don't require an exact match - transform.py reprojects if needed -
    # but we do want both CRSs to be defined at all.
    passed = vector_crs is not None and raster_crs is not None
    detail = f"vector CRS={vector_crs}, raster CRS={raster_crs}"
    return QualityResult("crs_defined", passed, detail)


def check_region_name_uniqueness(regions: gpd.GeoDataFrame, name_field: str) -> QualityResult:
    names = regions[name_field]
    duplicates = names[names.duplicated()].tolist()
    passed = len(duplicates) == 0
    detail = "all region names unique" if passed else f"duplicate names: {duplicates}"
    return QualityResult("region_name_uniqueness", passed, detail)


def check_hazard_value_range(hazard_stats: list[dict], expected_range: list[float]) -> QualityResult:
    low, high = expected_range
    out_of_range = [
        row for row in hazard_stats if row["hazard_mean"] is not None and not (low <= row["hazard_mean"] <= high)
    ]
    passed = len(out_of_range) == 0
    detail = (
        f"all region means within [{low}, {high}]"
        if passed
        else f"{len(out_of_range)} regions out of range: {[r['region_name'] for r in out_of_range]}"
    )
    return QualityResult("hazard_value_range", passed, detail)


def check_no_missing_hazard_data(hazard_stats: list[dict]) -> QualityResult:
    """Advisory, not blocking: a region can legitimately fall outside the
    raster's extent (e.g. Alaska/Hawaii/Puerto Rico are outside the CONUS
    bbox used by the demo raster). We still want this surfaced clearly -
    silently reporting a 0 or null risk score for those regions would be
    worse than flagging them as no-coverage. run_post_transform_checks
    treats this one as non-fatal; report.py excludes these regions from
    the ranked output rather than showing a misleading score.
    """
    missing = [row for row in hazard_stats if row["pixel_count"] == 0]
    passed = len(missing) == 0
    detail = (
        "every region overlapped at least one raster pixel"
        if passed
        else f"{len(missing)} region(s) outside raster coverage, excluded from ranking: "
        f"{[r['region_name'] for r in missing]}"
    )
    return QualityResult("hazard_coverage", passed, detail)


def run_pre_transform_checks(regions: gpd.GeoDataFrame, raster_path) -> list[QualityResult]:
    config = load_config()
    name_field = config["sources"]["region_name_field"]

    results = [
        check_geometry_validity(regions),
        check_crs_match(regions, raster_path),
        check_region_name_uniqueness(regions, name_field),
    ]
    _log_and_raise_if_failed(results)
    return results


def run_post_transform_checks(hazard_stats: list[dict]) -> list[QualityResult]:
    config = load_config()
    expected_range = config["quality"]["hazard_value_range"]

    blocking = [check_hazard_value_range(hazard_stats, expected_range)]
    advisory = [check_no_missing_hazard_data(hazard_stats)]

    _log_results(advisory + blocking)
    _raise_if_failed(blocking)
    return advisory + blocking


def _log_results(results: list[QualityResult]) -> None:
    for r in results:
        level = logger.info if r.passed else logger.warning
        level(f"[{'PASS' if r.passed else 'FLAGGED'}] {r.check_name}: {r.detail}")


def _log_and_raise_if_failed(results: list[QualityResult]) -> None:
    """Used for checks where any failure should stop the run (pre-transform)."""
    for r in results:
        level = logger.info if r.passed else logger.error
        level(f"[{'PASS' if r.passed else 'FAIL'}] {r.check_name}: {r.detail}")
    _raise_if_failed(results)


def _raise_if_failed(results: list[QualityResult]) -> None:
    failed = [r for r in results if not r.passed]
    if failed:
        names = ", ".join(r.check_name for r in failed)
        raise DataQualityError(f"Data quality checks failed: {names}")
