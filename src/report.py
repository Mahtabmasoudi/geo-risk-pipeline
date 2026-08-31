"""
Reporting stage: query the warehouse for the most recent run and produce
a denormalized summary CSV plus a choropleth PNG map.

Kept deliberately simple (matplotlib, no web framework) since the point is
the data pipeline, not a dashboard app - a real deployment would point a BI
tool (QuickSight, etc.) at the warehouse instead of generating static files.
"""

import geopandas as gpd
import matplotlib.pyplot as plt

from src.pipeline_utils import get_logger, load_config, project_path
from src.warehouse import get_connection

logger = get_logger("report")


def get_latest_run_id(con) -> str:
    row = con.execute("SELECT run_id FROM pipeline_runs ORDER BY run_timestamp DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("No pipeline runs found in the warehouse. Run pipeline.py first.")
    return row[0]


def build_summary_dataframe(con, run_id: str):
    """Denormalized region + risk table for a given run, ordered highest risk first."""
    return con.execute(
        """
        SELECT
            d.region_name,
            d.population_density,
            f.hazard_mean,
            f.hazard_min,
            f.hazard_max,
            f.hazard_stddev,
            f.pixel_count,
            f.risk_tier
        FROM fact_regional_risk f
        JOIN dim_region d ON f.region_id = d.region_id
        WHERE f.run_id = ?
        ORDER BY f.hazard_mean DESC NULLS LAST
        """,
        [run_id],
    ).fetchdf()


def write_summary_csv(df, out_path) -> None:
    df.to_csv(out_path, index=False)
    logger.info(f"Wrote summary CSV to {out_path} ({len(df)} rows)")


def write_choropleth_map(df, out_path) -> None:
    """Join the risk scores back onto the region geometries and plot them.

    Regions with no raster coverage (pixel_count == 0, hazard_mean is null)
    are plotted in gray with a hatch pattern so they read as "no data"
    rather than being silently dropped or shown as zero risk.
    """
    config = load_config()
    name_field = config["sources"]["region_name_field"]
    regions_path = project_path(config["sources"]["regions_vector"])

    geo = gpd.read_file(regions_path)
    merged = geo.merge(df, left_on=name_field, right_on="region_name", how="left")

    # the demo raster only covers CONUS - drop AK/HI/PR from the plot extent
    # so the map doesn't zoom out to fit them with no data to show
    conus = merged.cx[-130:-65, 24:50]

    fig, ax = plt.subplots(1, 1, figsize=(12, 7))
    has_data = conus[conus["hazard_mean"].notna()]
    no_data = conus[conus["hazard_mean"].isna()]

    has_data.plot(
        column="hazard_mean",
        cmap="YlOrRd",
        linewidth=0.5,
        edgecolor="white",
        legend=True,
        legend_kwds={"label": "Hazard index (0-100)", "shrink": 0.6},
        ax=ax,
    )
    if len(no_data) > 0:
        no_data.plot(ax=ax, facecolor="lightgray", edgecolor="white", hatch="///", label="No raster coverage")

    ax.set_title("Regional Climate Hazard Risk (demo data)", fontsize=13)
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    logger.info(f"Wrote choropleth map to {out_path}")


def generate(run_id: str | None = None) -> dict:
    config = load_config()
    con = get_connection()
    try:
        run_id = run_id or get_latest_run_id(con)
        df = build_summary_dataframe(con, run_id)
    finally:
        con.close()

    csv_path = project_path(config["outputs"]["summary_csv"])
    map_path = project_path(config["outputs"]["map_png"])
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    write_summary_csv(df, csv_path)
    write_choropleth_map(df, map_path)

    top5 = df[df["hazard_mean"].notna()].head(5)
    logger.info("Top 5 highest-risk regions:")
    for _, row in top5.iterrows():
        logger.info(f"  {row['region_name']:20s} mean={row['hazard_mean']:.1f}  tier={row['risk_tier']}")

    return {"run_id": run_id, "csv_path": str(csv_path), "map_path": str(map_path), "row_count": len(df)}


if __name__ == "__main__":
    generate()
