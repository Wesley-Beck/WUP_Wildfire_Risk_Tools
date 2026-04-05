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
@click.option("--local-data", default=None,
              help="Path to local AORC mirror (use 'aorc-tools download' to create)")
def extract(points_file, year, output, start_month, end_month,
            fuel_model, fuel_moisture, latitude, output_format, skip_raw,
            local_data):
    """Extract hourly AORC data and compute fire danger indices."""
    from aorc_tools.point_filter import load_point_index
    from aorc_tools.extract import extract_year

    logger.info("Loading points from %s", points_file)
    points_df = load_point_index(points_file)
    logger.info("Loaded %d points", len(points_df))

    # Set up local mirror if path provided
    mirror = None
    if local_data:
        from aorc_tools.local_mirror import LocalAORCMirror
        mirror = LocalAORCMirror(local_data, points_df)
        status = mirror.status()
        logger.info("Local mirror: %d months cached (%.1f GB)",
                     status["months_downloaded"], status["total_size_gb"])

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
        mirror=mirror,
        callback=progress,
    )
    click.echo(f"Extraction complete for {year}")


@main.command()
@click.option("--points", "points_file", required=True, help="Path to points_index.csv")
@click.option("--mirror-dir", required=True, help="Local directory to store AORC data")
@click.option("--start-year", required=True, type=int, help="Start year (e.g., 1979)")
@click.option("--end-year", required=True, type=int, help="End year (e.g., 2024)")
@click.option("--start-month", default=1, type=int, help="Start month in start year")
@click.option("--end-month", default=12, type=int, help="End month in end year")
def download(points_file, mirror_dir, start_year, end_year, start_month, end_month):
    """Download AORC data to local disk for offline computation.

    Downloads only the grid points in your study area (~28K points for WUP),
    not the full CONUS grid. Approximately 3 GB per year compressed.

    Once downloaded, use --local-data with the extract command to compute
    fire indices from local data instead of S3 (eliminates network bottleneck).
    """
    from aorc_tools.point_filter import load_point_index
    from aorc_tools.local_mirror import LocalAORCMirror

    points_df = load_point_index(points_file)
    click.echo(f"Loaded {len(points_df)} points")

    mirror = LocalAORCMirror(mirror_dir, points_df)
    status = mirror.status()
    click.echo(f"Mirror: {status['months_downloaded']} months cached "
               f"({status['total_size_gb']} GB) in {mirror_dir}")

    def progress(year, month, msg):
        click.echo(f"  {year}-{month:02d}: {msg}")

    count = mirror.download_range(
        start_year, end_year,
        start_month=start_month, end_month=end_month,
        callback=progress,
    )
    click.echo(f"Downloaded {count} new months")

    status = mirror.status()
    click.echo(f"Mirror total: {status['months_downloaded']} months, "
               f"{status['total_size_gb']} GB")


@main.command()
@click.option("--points", "points_file", required=True, help="Path to points_index.csv")
@click.option("--mirror-dir", required=True, help="Local AORC mirror directory")
def sync(points_file, mirror_dir):
    """Check for new AORC data and download any missing months.

    Compares the local mirror against what's available on S3 and downloads
    any months that haven't been cached yet.
    """
    from aorc_tools.point_filter import load_point_index
    from aorc_tools.local_mirror import LocalAORCMirror

    points_df = load_point_index(points_file)
    mirror = LocalAORCMirror(mirror_dir, points_df)

    click.echo("Checking for new AORC data on S3...")
    missing = mirror.check_for_updates()

    if not missing:
        click.echo("Local mirror is up to date!")
        return

    click.echo(f"Found {len(missing)} months to download:")
    for year, month in missing[:10]:
        click.echo(f"  {year}-{month:02d}")
    if len(missing) > 10:
        click.echo(f"  ... and {len(missing) - 10} more")

    def progress(year, month, msg):
        click.echo(f"  {year}-{month:02d}: {msg}")

    count = mirror.sync(callback=progress)
    click.echo(f"Synced {count} new months")


@main.command(name="run-all")
@click.option("--points", "points_file", required=True, help="Path to points_index.csv")
@click.option("--mirror-dir", required=True, help="Local AORC mirror directory")
@click.option("--output", default="./data/output", help="Base output directory")
@click.option("--start-year", default=1979, type=int, help="Start year (default: 1979)")
@click.option("--end-year", default=None, type=int, help="End year (default: current year)")
@click.option("--fuel-model", default="G", help="NFDRS fuel model code")
@click.option("--format", "output_format", default="sqlite",
              type=click.Choice(["sqlite", "csv", "both"]),
              help="Output format (default: sqlite)")
@click.option("--skip-raw/--no-skip-raw", default=True,
              help="Skip raw AORC output (default: yes, saves space)")
@click.option("--download-first/--no-download-first", default=True,
              help="Download missing months before extraction")
def run_all(points_file, mirror_dir, output, start_year, end_year,
            fuel_model, output_format, skip_raw, download_first):
    """Download AORC data and compute fire indices for the entire timeline.

    This is the all-in-one command. It:
    1. Downloads any missing months from S3 to the local mirror
    2. Extracts fire indices year-by-year from local data
    3. Carries forward FWI/NFDRS state across months for continuity

    Example: Process the full AORC archive (1979-present):
        aorc-tools run-all --points data/output/points_index.csv --mirror-dir ./data/aorc_local

    Example: Process just fire seasons (May-Oct) for recent years:
        aorc-tools run-all --points data/output/points_index.csv --mirror-dir ./data/aorc_local \\
            --start-year 2015 --end-year 2024
    """
    import time
    from datetime import datetime as dt
    from aorc_tools.point_filter import load_point_index
    from aorc_tools.local_mirror import LocalAORCMirror
    from aorc_tools.extract import extract_year

    if end_year is None:
        end_year = dt.utcnow().year

    points_df = load_point_index(points_file)
    click.echo(f"Loaded {len(points_df)} points")

    mirror = LocalAORCMirror(mirror_dir, points_df)
    status_info = mirror.status()
    click.echo(f"Local mirror: {status_info['months_downloaded']} months cached "
               f"({status_info['total_size_gb']} GB)")

    total_years = end_year - start_year + 1
    total_months = total_years * 12

    # Step 1: Download missing months
    if download_first:
        click.echo(f"\n=== Step 1: Downloading AORC data ({start_year}-{end_year}) ===")
        def dl_progress(year, month, msg):
            click.echo(f"  {year}-{month:02d}: {msg}")

        downloaded = mirror.download_range(start_year, end_year, callback=dl_progress)
        click.echo(f"Downloaded {downloaded} new months")
    else:
        click.echo("\nSkipping download (--no-download-first)")

    # Step 2: Extract fire indices year by year
    click.echo(f"\n=== Step 2: Computing fire indices ({start_year}-{end_year}) ===")
    t_total = time.perf_counter()
    state = {"fwi_state": None, "fm_state": None}

    for yr_idx, year in enumerate(range(start_year, end_year + 1)):
        click.echo(f"\n--- Year {year} ({yr_idx + 1}/{total_years}) ---")
        t_year = time.perf_counter()

        def progress(pct, msg):
            click.echo(f"  [{pct:5.1f}%] {msg}")

        state = extract_year(
            year, points_df, output,
            fuel_model=fuel_model,
            output_format=output_format,
            skip_raw=skip_raw,
            mirror=mirror,
            fwi_state=state["fwi_state"],
            fm_state=state["fm_state"],
            callback=progress,
        )

        elapsed = time.perf_counter() - t_year
        click.echo(f"  Year {year} complete in {elapsed:.0f}s")

    total_time = time.perf_counter() - t_total
    click.echo(f"\n=== All done! {total_years} years processed in {total_time:.0f}s ===")
    click.echo(f"Output: {output}")


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
