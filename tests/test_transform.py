"""
Unit tests for the zonal statistics logic in src/transform.py.

Rather than relying on the real (randomly generated) demo raster, these
build a small known raster in memory - a 10x10 grid where every pixel is
exactly 42 - so the expected zonal stats are known ahead of time and the
test isn't just re-checking whatever the generator happened to produce.
"""

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import box

from src.transform import _zonal_stats_for_geometry, classify_risk_tier


def _write_constant_raster(path, value: float, nodata: float = -9999.0):
    width, height = 10, 10
    transform = from_bounds(0, 0, 10, 10, width, height)
    data = np.full((height, width), value, dtype="float32")

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def test_zonal_stats_on_constant_raster(tmp_path):
    raster_path = tmp_path / "constant.tif"
    _write_constant_raster(raster_path, value=42.0)

    # a 4x4 box fully inside the 10x10 raster
    geometry = box(2, 2, 6, 6)

    with rasterio.open(raster_path) as dataset:
        stats = _zonal_stats_for_geometry(dataset, geometry)

    assert stats["pixel_count"] > 0
    assert stats["hazard_mean"] == 42.0
    assert stats["hazard_min"] == 42.0
    assert stats["hazard_max"] == 42.0
    assert stats["hazard_stddev"] == 0.0


def test_zonal_stats_outside_raster_extent(tmp_path):
    raster_path = tmp_path / "constant.tif"
    _write_constant_raster(raster_path, value=42.0)

    # a box entirely outside the raster's 0-10, 0-10 extent
    geometry = box(100, 100, 110, 110)

    with rasterio.open(raster_path) as dataset:
        stats = _zonal_stats_for_geometry(dataset, geometry)

    assert stats["pixel_count"] == 0
    assert stats["hazard_mean"] is None


def test_classify_risk_tier_boundaries():
    tier_config = {"low_max": 25, "medium_max": 50, "high_max": 75}

    assert classify_risk_tier(0, tier_config) == "Low"
    assert classify_risk_tier(25, tier_config) == "Low"
    assert classify_risk_tier(25.1, tier_config) == "Medium"
    assert classify_risk_tier(50, tier_config) == "Medium"
    assert classify_risk_tier(75, tier_config) == "High"
    assert classify_risk_tier(75.1, tier_config) == "Severe"
    assert classify_risk_tier(100, tier_config) == "Severe"


def test_classify_risk_tier_handles_missing_data():
    tier_config = {"low_max": 25, "medium_max": 50, "high_max": 75}
    assert classify_risk_tier(None, tier_config) is None
