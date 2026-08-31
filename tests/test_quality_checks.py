import geopandas as gpd
from shapely.geometry import Polygon

from src.quality_checks import (
    check_geometry_validity,
    check_hazard_value_range,
    check_no_missing_hazard_data,
    check_region_name_uniqueness,
)


def _make_regions(names, geometries, crs="EPSG:4326"):
    return gpd.GeoDataFrame({"name": names, "geometry": geometries}, crs=crs)


def test_geometry_validity_passes_for_simple_squares():
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    regions = _make_regions(["A", "B"], [square, square])

    result = check_geometry_validity(regions)

    assert result.passed


def test_geometry_validity_fails_for_self_intersecting_polygon():
    # bowtie shape: self-intersects at (0.5, 0.5)
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
    regions = _make_regions(["A"], [bowtie])

    result = check_geometry_validity(regions)

    assert not result.passed


def test_region_name_uniqueness_flags_duplicates():
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    regions = _make_regions(["Texas", "Texas"], [square, square])

    result = check_region_name_uniqueness(regions, "name")

    assert not result.passed
    assert "Texas" in result.detail


def test_hazard_value_range_flags_out_of_range_mean():
    stats = [
        {"region_name": "A", "hazard_mean": 50.0},
        {"region_name": "B", "hazard_mean": 150.0},  # out of the 0-100 range
    ]

    result = check_hazard_value_range(stats, [0, 100])

    assert not result.passed
    assert "B" in result.detail


def test_hazard_coverage_flags_zero_pixel_regions_but_reports_which():
    stats = [
        {"region_name": "Texas", "pixel_count": 500},
        {"region_name": "Alaska", "pixel_count": 0},
    ]

    result = check_no_missing_hazard_data(stats)

    assert not result.passed
    assert "Alaska" in result.detail
    assert "Texas" not in result.detail
