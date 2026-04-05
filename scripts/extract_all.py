#!/usr/bin/env python3
"""
Full-timeline AORC extraction script with resume support.

Extracts fire danger indices for the entire AORC timeline (1979-2024),
one month at a time, with automatic carry-forward of FWI state between
months. Skips months that have already been extracted.

Usage:
    python scripts/extract_all.py
    python scripts/extract_all.py --start-year 2000 --end-year 2024
    python scripts/extract_all.py --start-year 2020 --end-year 2020 --start-month 6 --end-month 9
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aorc_tools.extract import extract_month
from aorc_tools.aorc_access import AORCDataLoader
from aorc_tools.point_filter import load_point_index

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def month_is_extracted(output_base, year, month):
    """Check if a month has already been extracted by looking for output CSVs."""
    month_dir = Path(output_base) / str(year) / f"{month:02d}"
    # Check for at least the cfwi directory with FWI.csv
    fwi_csv = month_dir / "cfwi" / f"{year}_{month:02d}_FWI.csv"
    return fwi_csv.exists()


def load_carry_forward_state(output_base, year, month):
    """Load FWI carry-forward state from the previous month."""
    state_file = (
        Path(output_base) / str(year) / f"{month:02d}" / "state"
        / f"fwi_state_{year}_{month:02d}.json"
    )
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return None


def find_previous_state(output_base, year, month):
    """Find the most recent carry-forward state before the given year/month."""
    # Check previous month
    if month > 1:
        prev_year, prev_month = year, month - 1
    else:
        prev_year, prev_month = year - 1, 12

    state = load_carry_forward_state(output_base, prev_year, prev_month)
    if state:
        logger.info("Loaded carry-forward state from %d-%02d", prev_year, prev_month)
        return state

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Extract AORC fire danger indices for the full timeline."
    )
    parser.add_argument("--start-year", type=int, default=1979,
                        help="First year to extract (default: 1979)")
    parser.add_argument("--end-year", type=int, default=2024,
                        help="Last year to extract (default: 2024)")
    parser.add_argument("--start-month", type=int, default=1,
                        help="Start month within each year (default: 1)")
    parser.add_argument("--end-month", type=int, default=12,
                        help="End month within each year (default: 12)")
    parser.add_argument("--points", default="data/output/points_index.csv",
                        help="Path to points_index.csv")
    parser.add_argument("--output", default="data/output",
                        help="Base output directory")
    parser.add_argument("--fuel-model", default="G",
                        help="NFDRS fuel model code (default: G)")
    parser.add_argument("--fuel-moisture", default="emc",
                        choices=["emc", "nelson"],
                        help="Fuel moisture method (default: emc)")
    parser.add_argument("--latitude", type=float, default=46.5,
                        help="Representative latitude for FWI (default: 46.5)")
    parser.add_argument("--format", default="both",
                        choices=["sqlite", "csv", "both"],
                        help="Output format (default: both)")
    parser.add_argument("--skip-raw", action="store_true",
                        help="Skip raw AORC output (saves ~30%% time/space)")
    parser.add_argument("--force", action="store_true",
                        help="Re-extract months even if output already exists")
    args = parser.parse_args()

    # Load points
    print(f"Loading points from {args.points}...")
    points_df = load_point_index(args.points)
    print(f"Loaded {len(points_df)} points")

    # Build list of (year, month) to extract
    months_to_do = []
    months_skipped = 0
    for year in range(args.start_year, args.end_year + 1):
        sm = args.start_month if year == args.start_year else 1
        em = args.end_month if year == args.end_year else 12
        for month in range(sm, em + 1):
            if not args.force and month_is_extracted(args.output, year, month):
                months_skipped += 1
            else:
                months_to_do.append((year, month))

    total = len(months_to_do)
    if months_skipped > 0:
        print(f"Skipping {months_skipped} already-extracted months")
    if total == 0:
        print("All months already extracted. Use --force to re-extract.")
        return

    print(f"Extracting {total} months ({months_to_do[0][0]}-{months_to_do[0][1]:02d} "
          f"through {months_to_do[-1][0]}-{months_to_do[-1][1]:02d})")
    print(f"Estimated time: ~{total * 15} minutes ({total * 15 / 60:.1f} hours)")
    print()

    # Create shared loader (reuses S3 connections)
    loader = AORCDataLoader()

    # Track timing
    start_time = time.time()
    fwi_state = None

    for idx, (year, month) in enumerate(months_to_do):
        # Try to load carry-forward state if we don't have it
        if fwi_state is None:
            fwi_state = find_previous_state(args.output, year, month)

        elapsed = time.time() - start_time
        if idx > 0:
            avg_per_month = elapsed / idx
            remaining = avg_per_month * (total - idx)
            eta_str = f"ETA: {remaining / 60:.0f} min"
        else:
            eta_str = ""

        print(f"[{idx + 1}/{total}] Extracting {year}-{month:02d}... {eta_str}")

        def progress(pct, msg):
            print(f"  [{pct:5.1f}%] {msg}")

        try:
            result = extract_month(
                year, month, points_df, args.output,
                loader=loader,
                fuel_model=args.fuel_model,
                fuel_moisture_method=args.fuel_moisture,
                fwi_state=fwi_state,
                latitude=args.latitude,
                output_format=args.format,
                skip_raw=args.skip_raw,
                callback=progress,
            )
            fwi_state = result["fwi_state"]
            print(f"  Completed {year}-{month:02d}")
        except Exception as e:
            logger.error("FAILED %d-%02d: %s", year, month, e)
            print(f"  FAILED: {e}")
            # Reset state on failure so next month starts fresh
            fwi_state = None
            continue

    total_time = time.time() - start_time
    print()
    print(f"Extraction complete!")
    print(f"Total time: {total_time / 60:.1f} minutes ({total_time / 3600:.1f} hours)")
    print(f"Output: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
