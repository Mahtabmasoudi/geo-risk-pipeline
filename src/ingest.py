"""
Ingestion stage: read the raw vector and raster sources off disk and hand
back clean, typed objects for the rest of the pipeline.

This is deliberately the only place that touches file I/O for the raw
inputs, so if the source ever moves (local file -> S3 -> a live API), only
this module changes.

Note on the vector source: data/raw/regions.geojson is real US state
boundary + population density data pulled from a public GeoJSON mirror
(see docs/data_dictionary.md for the source URL and license note). It's
checked into the repo so the pipeline runs offline; re-fetching it is
optional and handled by fetch_regions() below.
"""

import geopandas as gpd
import rasterio
import requests

from src.pipeline_utils import get_logger, load_config, project_path

logger = get_logger("ingest")

REGIONS_SOURCE_URL = (
    "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json"
)


def fetch_regions(dest_path=None, timeout: int = 15) -> None:
    """Re-download the vector source from its public URL.

    Not called automatically by the pipeline - the repo ships with a cached
    copy so `python pipeline.py` works with no network access. Call this
    directly if you want to refresh the source data.
    """
    config = load_config()
    dest_path = dest_path or project_path(config["sources"]["regions_vector"])

    logger.info(f"Fetching regions from {REGIONS_SOURCE_URL}")
    response = requests.get(REGIONS_SOURCE_URL, timeout=timeout)
    response.raise_for_status()

    dest_path.write_bytes(response.content)
    logger.info(f"Saved {len(response.content):,} bytes to {dest_path}")


def _repair_invalid_geometries(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Fix self-intersecting polygons rather than dropping those rows.

    This dataset in particular has a few real-world offenders: Alaska's
    polygon crosses the antimeridian (a classic source of self-intersection
    in lon/lat data), and Maryland/Virginia's Chesapeake Bay shoreline is
    detailed enough to produce ring self-intersections. shapely's
    make_valid() resolves these by rebuilding a valid geometry that covers
    the same area, instead of us silently losing those regions downstream.
    """
    invalid_mask = ~gdf.geometry.is_valid
    if invalid_mask.any():
        bad_names = gdf.loc[invalid_mask, "name"].tolist() if "name" in gdf else gdf.index[invalid_mask].tolist()
        logger.warning(f"Repairing {invalid_mask.sum()} invalid geometries: {bad_names}")
        gdf.loc[invalid_mask, "geometry"] = gdf.loc[invalid_mask, "geometry"].make_valid()
    return gdf


def load_regions() -> gpd.GeoDataFrame:
    """Load the region boundaries as a GeoDataFrame."""
    config = load_config()
    path = project_path(config["sources"]["regions_vector"])

    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python -m src.ingest --fetch-regions` "
            "or place a regions.geojson file at that path."
        )

    gdf = gpd.read_file(path)
    gdf = _repair_invalid_geometries(gdf)
    logger.info(f"Loaded {len(gdf)} region features from {path.name} (CRS: {gdf.crs})")
    return gdf


def load_hazard_raster_meta() -> dict:
    """Return metadata about the hazard raster without loading the full array.

    transform.py opens the raster itself when it actually needs pixel data;
    this is just for logging / the pipeline_runs metadata record.
    """
    config = load_config()
    path = project_path(config["sources"]["hazard_raster"])

    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python -m src.generate_hazard_raster` first, "
            "or point config.sources.hazard_raster at a real raster."
        )

    with rasterio.open(path) as src:
        meta = {
            "path": str(path),
            "crs": str(src.crs),
            "width": src.width,
            "height": src.height,
            "resolution": src.res,
            "bounds": tuple(src.bounds),
            "dtype": src.dtypes[0],
        }
    logger.info(
        f"Hazard raster {path.name}: {meta['width']}x{meta['height']} px, "
        f"CRS {meta['crs']}, resolution {meta['resolution']}"
    )
    return meta


if __name__ == "__main__":
    import sys

    if "--fetch-regions" in sys.argv:
        fetch_regions()
    else:
        load_regions()
        load_hazard_raster_meta()
