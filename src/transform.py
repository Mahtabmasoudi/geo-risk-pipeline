"""
Core transform: overlay the hazard raster on each region polygon and
compute zonal statistics (mean/min/max/stddev of the hazard index inside
each region's boundary), then bucket each region into a risk tier.

This is written by hand with rasterio.mask rather than pulling in a zonal
stats library, mainly so the overlay logic is visible and auditable rather
than hidden behind a helper function.
"""

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.mask import mask

from src.pipeline_utils import get_logger, load_config

logger = get_logger("transform")


def _zonal_stats_for_geometry(raster_dataset, geometry) -> dict:
    """Mask the raster to a single polygon and summarize the pixels inside it."""
    empty_result = {
        "hazard_mean": None,
        "hazard_min": None,
        "hazard_max": None,
        "hazard_stddev": None,
        "pixel_count": 0,
    }

    try:
        out_image, _ = mask(raster_dataset, [geometry], crop=True, nodata=raster_dataset.nodata)
    except ValueError:
        # geometry doesn't overlap the raster extent at all
        return empty_result

    band = out_image[0]
    valid_pixels = band[band != raster_dataset.nodata]

    if valid_pixels.size == 0:
        return empty_result

    return {
        "hazard_mean": float(np.mean(valid_pixels)),
        "hazard_min": float(np.min(valid_pixels)),
        "hazard_max": float(np.max(valid_pixels)),
        "hazard_stddev": float(np.std(valid_pixels)),
        "pixel_count": int(valid_pixels.size),
    }


def classify_risk_tier(hazard_mean: float | None, tier_config: dict) -> str | None:
    if hazard_mean is None:
        return None
    if hazard_mean <= tier_config["low_max"]:
        return "Low"
    if hazard_mean <= tier_config["medium_max"]:
        return "Medium"
    if hazard_mean <= tier_config["high_max"]:
        return "High"
    return "Severe"


def compute_regional_hazard_stats(regions: gpd.GeoDataFrame, raster_path: str) -> list[dict]:
    """Return one dict per region with zonal hazard stats and a risk tier."""
    config = load_config()
    name_field = config["sources"]["region_name_field"]
    tier_config = config["risk_tiers"]

    with rasterio.open(raster_path) as raster_dataset:
        # Reproject the vector layer to match the raster CRS if they differ -
        # mask() requires both to be in the same CRS.
        if regions.crs != raster_dataset.crs:
            logger.info(f"Reprojecting regions from {regions.crs} to {raster_dataset.crs}")
            regions = regions.to_crs(raster_dataset.crs)

        results = []
        for _, region in regions.iterrows():
            stats = _zonal_stats_for_geometry(raster_dataset, region.geometry)
            stats["region_name"] = region[name_field]
            stats["risk_tier"] = classify_risk_tier(stats["hazard_mean"], tier_config)
            results.append(stats)

    logger.info(f"Computed zonal hazard stats for {len(results)} regions")
    return results


if __name__ == "__main__":
    from src.ingest import load_regions
    from src.pipeline_utils import project_path

    config = load_config()
    regions = load_regions()
    raster_path = project_path(config["sources"]["hazard_raster"])
    stats = compute_regional_hazard_stats(regions, str(raster_path))

    for row in sorted(stats, key=lambda r: r["hazard_mean"] or 0, reverse=True)[:5]:
        print(f"{row['region_name']:20s} mean={row['hazard_mean']:.1f}  tier={row['risk_tier']}")
