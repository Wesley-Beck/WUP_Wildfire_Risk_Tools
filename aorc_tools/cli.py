"""
Command-line interface for the AORC Wildfire Risk Analysis System.

Usage:
    python -m aorc_tools.cli points --shapefile path/to/WUP.shp --output ./data/output
    python -m aorc_tools.cli extract --points ./data/output/points_index.csv --year 2020
    python -m aorc_tools.cli status
"""

import logging
import sys

import click

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@click.group()
def main():
    """AORC Wildfire Risk Analysis Tools for the Western Upper Peninsula."""
    pass


@main.command()
@click.option("--shapefile", required=True, help="Path to study area shapefile (.shp/.gpkg/.geojson)")
@click.option("--output", required=True, help="Output directory for point index CSV")
@click.option("--filter-water/--no-filter-water", default=True, help="Exclude large water bodies")
@click.option("--water-min-area", default=10.0, help="Min water body area in km² to exclude")
def points(shapefile, output, filter_water, water_min_area):
    """Generate AORC grid point index from a study area shapefile."""
    from pathlib import Path

    from aorc_tools.aorc_access import AORCDataLoader
    from aorc_tools.point_filter import (
        load_boundary, generate_point_index, save_point_index,
    )

    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load boundary
    logger.info("Loading boundary from %s", shapefile)
    boundary = load_boundary(shapefile)

    # Get AORC coordinates
    logger.info("Fetching AORC coordinates...")
    loader = AORCDataLoader()
    lats, lons = loader.get_coordinates()

    # Generate points
    logger.info("Generating point index...")
    points_df = generate_point_index(boundary, lats, lons)
    logger.info("Generated %d points within boundary", len(points_df))

    # Optionally filter water
    if filter_water:
        from aorc_tools.water_filter import get_large_water_bodies, exclude_water_points
        logger.info("Downloading water body data...")
        water_geom = get_large_water_bodies(boundary, min_area_km2=water_min_area)
        points_df = exclude_water_points(points_df, water_geom)

    # Save
    output_file = output_dir / "points_index.csv"
    save_point_index(points_df, output_file)
    click.echo(f"Saved {len(points_df)} points to {output_file}")


@main.command()
@click.option("--points", "points_file", required=True, help="Path to points_index.csv")
@click.option("--year", required=True, type=int, help="Year to extract")
@click.option("--output", default="./data/output", help="Base output directory")
@click.option("--start-month", default=1, type=int, help="Start month (1-12)")
@click.option("--end-month", default=12, type=int, help="End month (1-12)")
@click.option("--fuel-model", default="G", help="NFDRS fuel model code (A-Z)")
@click.option("--fuel-moisture", default="emc", type=click.Choice(["emc", "nelson"]),
              help="Fuel moisture method")
@click.option("--latitude", default=46.5, type=float, help="Representative latitude for FWI")
@click.option("--format", "output_format", default="both",
              type=click.Choice(["sqlite", "csv", "both"]),
              help="Output format: sqlite (compact + web files), csv, or both")
@click.option("--skip-raw/--no-skip-raw", default=False,
              help="Skip raw AORC output (saves ~30% time/space)")
def extract(points_file, year, output, start_month, end_month,
            fuel_model, fuel_moisture, latitude, output_format, skip_raw):
    """Extract hourly AORC data and compute fire danger indices."""
    from aorc_tools.point_filter import load_point_index
    from aorc_tools.extract import extract_year

    logger.info("Loading points from %s", points_file)
    points_df = load_point_index(points_file)
    logger.info("Loaded %d points", len(points_df))

    def progress(pct, msg):
        click.echo(f"  [{pct:5.1f}%] {msg}")

    logger.info("Extracting %d-%02d through %d-%02d", year, start_month, year, end_month)
    extract_year(
        year, points_df, output,
        start_month=start_month, end_month=end_month,
        fuel_model=fuel_model,
        fuel_moisture_method=fuel_moisture,
        latitude=latitude,
        output_format=output_format,
        skip_raw=skip_raw,
        callback=progress,
    )
    click.echo(f"Extraction complete for {year}")


@main.command()
def status():
    """Check AORC S3 connection and system status."""
    click.echo("Checking AORC S3 access...")

    try:
        from aorc_tools.aorc_access import AORCDataLoader
        loader = AORCDataLoader()
        lats, lons = loader.get_coordinates()
        click.echo(f"  AORC grid: {len(lats)} lats × {len(lons)} lons")
        click.echo(f"  Lat range: {lats.min():.4f} to {lats.max():.4f}")
        click.echo(f"  Lon range: {lons.min():.4f} to {lons.max():.4f}")
        click.echo("  Status: OK")
    except Exception as e:
        click.echo(f"  Status: FAILED — {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
