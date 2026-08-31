"""
Builds a demo climate hazard raster (GeoTIFF) covering the continental US.

Real teams doing this kind of work pull hazard rasters from sources like
NOAA NClimGrid, WorldClim, or FEMA's National Risk Index. Those either need
an account, a large download, or licensing that doesn't make sense for a
throwaway demo repo, so this script generates a stand-in raster instead:
a smooth, spatially-correlated "hazard index" from 0-100, with a mild
south-to-north gradient layered in so it looks like a plausible heat/drought
risk surface rather than pure noise.

Swapping this out for a real raster later just means pointing
sources.hazard_raster in the config at a different .tif - nothing downstream
(transform.py, quality checks, warehouse schema) needs to change, as long as
the new raster is single-band and roughly on the same value scale.

Run directly to (re)generate the demo file:
    python src/generate_hazard_raster.py
"""

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from scipy.ndimage import gaussian_filter

from src.pipeline_utils import load_config, project_path


def build_hazard_array(width: int, height: int, seed: int) -> np.ndarray:
    """Generate a smooth 0-100 hazard surface.

    Method: start from random noise, blur it heavily so neighboring pixels
    are correlated (real climate surfaces don't jump around pixel to pixel),
    then blend in a north-south gradient so the southern part of the grid
    trends hotter/higher-risk, which is at least directionally realistic
    for a heat-risk-style index over the continental US.
    """
    rng = np.random.default_rng(seed)

    noise = rng.normal(loc=0.0, scale=1.0, size=(height, width))
    smoothed = gaussian_filter(noise, sigma=(height / 12, width / 12))

    # row 0 = north edge of the bbox, row (height-1) = south edge -
    # gradient increases southward so the surface trends hotter/higher-risk
    # toward the southern part of the grid
    lat_gradient = np.linspace(-0.35, 0.35, height).reshape(height, 1)
    surface = smoothed + lat_gradient

    # normalize to 0-100
    surface -= surface.min()
    surface /= surface.max()
    surface *= 100.0

    return surface.astype("float32")


def write_hazard_raster(out_path: str, bbox: list, width: int, height: int, seed: int) -> None:
    west, south, east, north = bbox
    transform = from_bounds(west, south, east, north, width, height)
    data = build_hazard_array(width, height, seed)

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": -9999.0,
    }

    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(data, 1)
        dst.update_tags(
            layer_name="synthetic_hazard_index",
            description="Demo climate hazard surface (0-100). Synthetically generated, "
            "see src/generate_hazard_raster.py for method.",
            units="index_0_100",
        )


def main():
    config = load_config()
    raster_cfg = config["raster_generation"]
    out_path = project_path(config["sources"]["hazard_raster"])

    write_hazard_raster(
        out_path=str(out_path),
        bbox=raster_cfg["bbox"],
        width=raster_cfg["width"],
        height=raster_cfg["height"],
        seed=raster_cfg["seed"],
    )
    print(f"Wrote demo hazard raster to {out_path}")


if __name__ == "__main__":
    main()
