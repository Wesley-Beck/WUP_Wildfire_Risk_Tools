"""
End-to-end pipeline test with timing benchmarks.

Tests: SQLite storage, parallel S3 prefetch, binary web file generation,
CSV export, and carry-forward state — with detailed timing breakdown.

Usage:
    python tests/test_pipeline_benchmark.py
"""

import json
import logging
import sqlite3
import struct
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def timed(label):
    """Context manager that prints elapsed time."""
    class Timer:
        def __init__(self):
            self.elapsed = 0
        def __enter__(self):
            self.start = time.perf_counter()
            return self
        def __exit__(self, *args):
            self.elapsed = time.perf_counter() - self.start
            logger.info("  [TIMING] %s: %.3fs", label, self.elapsed)
    return Timer()


def test_sqlite_storage():
    """Test SQLiteStorage: write, flush, read, web files, CSV export."""
    from aorc_tools.storage import SQLiteStorage

    logger.info("=" * 60)
    logger.info("TEST 1: SQLite Storage Backend")
    logger.info("=" * 60)

    n_points = 1000  # Use 1K points for fast testing (prod = 28K)
    n_hours = 48     # 2 days

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create mock points
        points_df = pd.DataFrame({
            "point_id": range(n_points),
            "latitude": np.linspace(46.0, 47.5, n_points),
            "longitude": np.linspace(-90.0, -88.0, n_points),
        })

        # --- Write test ---
        with timed("SQLite open + store points") as t_open:
            db = SQLiteStorage(tmpdir, 2020, 7)
            db.open()
            db.store_points(points_df)

        with timed(f"Accumulate {n_hours} hours × 3 variables") as t_acc:
            for h in range(n_hours):
                ts = datetime(2020, 7, 1 + h // 24, h % 24)
                db.add("FWI", ts, np.random.rand(n_points).astype(np.float32) * 50)
                db.add("ERC", ts, np.random.rand(n_points).astype(np.float32) * 100)
                db.add("temperature_c", ts, np.random.rand(n_points).astype(np.float32) * 30 + 5)

        with timed("Flush to SQLite") as t_flush:
            db.flush()

        # Check DB size
        db_path = Path(tmpdir) / "2020" / "07" / "data_2020_07.db"
        db_size = db_path.stat().st_size / 1024
        logger.info("  DB size: %.1f KB (%d points × %d hours × 3 vars)", db_size, n_points, n_hours)

        # --- Read test ---
        with timed("Read single hour") as t_read:
            vals = db.get_hour("FWI", "2020-07-01 12:00")
        assert vals is not None, "get_hour returned None"
        assert len(vals) == n_points, f"Expected {n_points} values, got {len(vals)}"
        logger.info("  Read %d values for FWI at 2020-07-01 12:00", len(vals))

        with timed("Get timestamps") as t_ts:
            timestamps = db.get_timestamps("FWI")
        assert len(timestamps) == n_hours, f"Expected {n_hours} timestamps, got {len(timestamps)}"

        with timed("Get variables") as t_vars:
            variables = db.get_variables()
        assert set(variables) == {"FWI", "ERC", "temperature_c"}, f"Unexpected variables: {variables}"

        # --- Binary web file generation ---
        with timed("Generate binary web files") as t_web:
            db.generate_web_files()

        web_dir = Path(tmpdir) / "2020" / "07" / "web"
        assert web_dir.exists(), "Web directory not created"
        bin_files = list(web_dir.glob("*.bin"))
        assert len(bin_files) == 3, f"Expected 3 .bin files, got {len(bin_files)}"

        # Verify binary file format
        fwi_bin = web_dir / "FWI.bin"
        with open(fwi_bin, "rb") as f:
            np_read, nh_read = struct.unpack("<II", f.read(8))
            ts_len = struct.unpack("<I", f.read(4))[0]
            ts_block = f.read(ts_len).decode("utf-8")
            ts_list = ts_block.split("\n")
            # Read first hour of data
            data = np.frombuffer(f.read(np_read * 4), dtype=np.float32)

        assert np_read == n_points, f"Binary header n_points mismatch: {np_read} vs {n_points}"
        assert nh_read == n_hours, f"Binary header n_hours mismatch: {nh_read} vs {n_hours}"
        assert len(ts_list) == n_hours, f"Binary timestamps count mismatch: {len(ts_list)} vs {n_hours}"
        assert len(data) == n_points, f"Binary data length mismatch: {len(data)} vs {n_points}"
        logger.info("  Binary format verified: %d points × %d hours", np_read, nh_read)

        # Check variables.json manifest
        manifest = web_dir / "variables.json"
        assert manifest.exists(), "variables.json not created"
        with open(manifest) as f:
            var_list = json.load(f)
        assert len(var_list) == 3

        bin_size = sum(f.stat().st_size for f in bin_files) / 1024
        logger.info("  Total .bin size: %.1f KB", bin_size)

        # --- CSV export test ---
        with timed("Export single variable CSV") as t_csv1:
            csv_path = db.export_csv("FWI")
        assert csv_path.exists(), "CSV not created"
        csv_df = pd.read_csv(csv_path, index_col=0)
        assert csv_df.shape == (n_points, n_hours), f"CSV shape mismatch: {csv_df.shape}"

        with timed("Export all CSVs") as t_csv_all:
            all_paths = db.export_all_csv()
        assert len(all_paths) == 3

        csv_size = sum(p.stat().st_size for p in all_paths) / 1024
        logger.info("  Total CSV size: %.1f KB", csv_size)
        logger.info("  Size ratio (DB/CSV): %.2f×", db_size / csv_size)

        db.close()

    logger.info("  TEST 1 PASSED ✓")
    return {
        "open": t_open.elapsed,
        "accumulate": t_acc.elapsed,
        "flush": t_flush.elapsed,
        "read_hour": t_read.elapsed,
        "web_gen": t_web.elapsed,
        "csv_export": t_csv1.elapsed,
        "csv_all": t_csv_all.elapsed,
        "db_size_kb": db_size,
        "csv_size_kb": csv_size,
        "bin_size_kb": bin_size,
    }


def test_extraction_pipeline_timing():
    """Test the full extraction pipeline with AORC S3 access + timing."""
    logger.info("=" * 60)
    logger.info("TEST 2: Full Extraction Pipeline (1 day, S3)")
    logger.info("=" * 60)

    from aorc_tools.aorc_access import AORCDataLoader
    from aorc_tools.point_filter import load_point_index

    points_file = Path("data/output/points_index.csv")
    if not points_file.exists():
        logger.warning("  SKIPPED: points_index.csv not found at %s", points_file)
        return None

    points_df = load_point_index(str(points_file))
    n_points = len(points_df)
    logger.info("  Loaded %d points", n_points)

    lat_indices = points_df["lat_idx"].values
    lon_indices = points_df["lon_idx"].values
    lat_bounds = (points_df["latitude"].min() - 0.05, points_df["latitude"].max() + 0.05)
    lon_bounds = (points_df["longitude"].min() - 0.05, points_df["longitude"].max() + 0.05)

    loader = AORCDataLoader()

    # --- Benchmark: single day fetch with spatial subsetting ---
    with timed("S3 dataset open (first access)") as t_s3_open:
        loader._open_dataset(2020)

    with timed("Day fetch: bbox subset, 24h batch (day 1)") as t_day1:
        data1 = loader.get_daily_point_values(
            2020, 7, 1, lat_indices, lon_indices, lat_bounds, lon_bounds
        )

    assert "hours" in data1, "No 'hours' key in result"
    assert len(data1["hours"]) == 24, f"Expected 24 hours, got {len(data1['hours'])}"
    assert "TMP_2maboveground" in data1, "No temperature data"
    logger.info("  Got %d hours × %d points for %d variables",
                len(data1["hours"]), data1["TMP_2maboveground"].shape[1],
                sum(1 for k in data1 if k not in ("hours", "timestamps")))

    # --- Benchmark: second day (should be faster, dataset already open) ---
    with timed("Day fetch: bbox subset, 24h batch (day 2, cached DS)") as t_day2:
        data2 = loader.get_daily_point_values(
            2020, 7, 2, lat_indices, lon_indices, lat_bounds, lon_bounds
        )

    # --- Benchmark: parallel prefetch of 2 days ---
    from concurrent.futures import ThreadPoolExecutor
    from aorc_tools.extract import _prefetch_day

    with timed("Parallel prefetch: 2 days simultaneously") as t_parallel:
        executor = ThreadPoolExecutor(max_workers=2)
        f1 = executor.submit(_prefetch_day, loader, 2020, 7, 3,
                             lat_indices, lon_indices, lat_bounds, lon_bounds)
        f2 = executor.submit(_prefetch_day, loader, 2020, 7, 4,
                             lat_indices, lon_indices, lat_bounds, lon_bounds)
        d3 = f1.result(timeout=120)
        d4 = f2.result(timeout=120)
        executor.shutdown(wait=False)

    logger.info("  Parallel 2-day fetch: %.3fs (vs sequential ~%.3fs)",
                t_parallel.elapsed, t_day1.elapsed + t_day2.elapsed)

    # --- Benchmark: fire index computation ---
    from aorc_tools.climate_convert import (
        kelvin_to_celsius, celsius_to_fahrenheit, wind_components_to_speed,
        ms_to_kph, ms_to_mph, specific_to_relative_humidity,
    )
    from aorc_tools.fire_indices.fwi import HourlyFWI
    from aorc_tools.fire_indices.nfdrs import compute_erc_bi
    from aorc_tools.fire_indices.fuel_moisture import emc_fuel_moisture

    fwi = HourlyFWI(n_points, latitude=46.5)

    with timed(f"Fire index computation: 24h × {n_points} points") as t_compute:
        for h_idx in range(24):
            hour_val = data1["hours"][h_idx]
            current = datetime(2020, 7, 1, hour_val)
            doy = current.timetuple().tm_yday

            temp_k = data1.get("TMP_2maboveground", np.full((24, n_points), np.nan))[h_idx]
            spfh = data1.get("SPFH_2maboveground", np.full((24, n_points), np.nan))[h_idx]
            pres_pa = data1.get("PRES_surface", np.full((24, n_points), np.nan))[h_idx]
            ugrd = data1.get("UGRD_10maboveground", np.full((24, n_points), np.nan))[h_idx]
            vgrd = data1.get("VGRD_10maboveground", np.full((24, n_points), np.nan))[h_idx]
            precip = data1.get("APCP_surface", np.full((24, n_points), np.nan))[h_idx]

            temp_c = kelvin_to_celsius(temp_k)
            temp_f = celsius_to_fahrenheit(temp_c)
            rh = specific_to_relative_humidity(spfh, temp_k, pres_pa)
            ws_ms = wind_components_to_speed(ugrd, vgrd)
            ws_kph = ms_to_kph(ws_ms)
            ws_mph = ms_to_mph(ws_ms)

            fwi_result = fwi.update(temp_c, rh, ws_kph, precip, doy, current.hour)

            fm = emc_fuel_moisture(temp_f, rh)
            nfdrs_result = compute_erc_bi(
                fm["fm1"], fm["fm10"], fm["fm100"], fm["fm1000"],
                np.full(n_points, 120.0), np.full(n_points, 100.0),
                ws_mph, fuel_model="G",
            )

    per_hour = t_compute.elapsed / 24
    logger.info("  Per-hour compute: %.4fs (%d pts)", per_hour, n_points)
    logger.info("  Projected full month (744h): %.1fs (%.1f min)",
                per_hour * 744, per_hour * 744 / 60)

    # --- Benchmark: full day SQLite write + web gen ---
    with tempfile.TemporaryDirectory() as tmpdir:
        from aorc_tools.storage import SQLiteStorage

        db = SQLiteStorage(tmpdir, 2020, 7)
        db.open()
        db.store_points(points_df)

        with timed(f"SQLite accumulate 24h × {n_points} pts × 20 vars") as t_acc:
            for h_idx in range(24):
                ts = datetime(2020, 7, 1, data1["hours"][h_idx])
                # Simulate all variable categories
                for var in ["FWI", "FFMC", "DMC", "DC", "ISI", "BUI",
                            "ERC", "SC", "BI", "FM1", "FM10", "FM100", "FM1000",
                            "temperature_c", "temperature_f", "relative_humidity",
                            "wind_speed_kph", "wind_speed_mph", "precipitation_mm",
                            "wind_speed_ms"]:
                    db.add(var, ts, np.random.rand(n_points).astype(np.float32))

        with timed("SQLite flush (24h × 20 vars)") as t_flush:
            db.flush()

        with timed("Generate web .bin files (20 vars)") as t_web:
            db.generate_web_files()

        db_path = Path(tmpdir) / "2020" / "07" / "data_2020_07.db"
        db_size_mb = db_path.stat().st_size / 1048576
        web_dir = Path(tmpdir) / "2020" / "07" / "web"
        bin_size_mb = sum(f.stat().st_size for f in web_dir.glob("*.bin")) / 1048576

        logger.info("  DB size: %.2f MB, Web .bin total: %.2f MB", db_size_mb, bin_size_mb)

        db.close()

    logger.info("  TEST 2 PASSED ✓")

    return {
        "n_points": n_points,
        "s3_open": t_s3_open.elapsed,
        "day_fetch_1": t_day1.elapsed,
        "day_fetch_2": t_day2.elapsed,
        "parallel_2day": t_parallel.elapsed,
        "compute_24h": t_compute.elapsed,
        "compute_per_hour": per_hour,
        "sqlite_acc_24h": t_acc.elapsed,
        "sqlite_flush_24h": t_flush.elapsed,
        "web_gen_24h": t_web.elapsed,
    }


def test_carry_forward_state():
    """Test that FWI carry-forward state works across months."""
    logger.info("=" * 60)
    logger.info("TEST 3: Carry-Forward State")
    logger.info("=" * 60)

    from aorc_tools.fire_indices.fwi import HourlyFWI

    n_points = 100

    # Simulate month 1
    fwi1 = HourlyFWI(n_points, latitude=46.5)
    for h in range(24):
        fwi1.update(
            np.full(n_points, 25.0),  # temp_c
            np.full(n_points, 40.0),  # rh
            np.full(n_points, 15.0),  # wind_kph
            np.full(n_points, 0.0),   # precip
            182, h,                   # doy, hour
        )
    state1 = fwi1.get_state()

    # Simulate month 2 with carry-forward
    fwi2 = HourlyFWI(n_points, latitude=46.5, initial_state=state1)
    result2 = fwi2.update(
        np.full(n_points, 25.0), np.full(n_points, 40.0),
        np.full(n_points, 15.0), np.full(n_points, 0.0),
        183, 0,
    )

    # Simulate month 2 WITHOUT carry-forward (default startup)
    fwi3 = HourlyFWI(n_points, latitude=46.5)
    result3 = fwi3.update(
        np.full(n_points, 25.0), np.full(n_points, 40.0),
        np.full(n_points, 15.0), np.full(n_points, 0.0),
        183, 0,
    )

    # Results should differ (carry-forward vs default)
    diff_ffmc = np.abs(result2["FFMC"] - result3["FFMC"]).mean()
    logger.info("  Mean FFMC difference (carry-forward vs default): %.4f", diff_ffmc)
    assert diff_ffmc > 0.01, "Carry-forward state had no effect on FFMC"

    # State should be JSON-serializable
    state_json = json.dumps(state1)
    state_back = json.loads(state_json)
    fwi4 = HourlyFWI(n_points, latitude=46.5, initial_state=state_back)
    result4 = fwi4.update(
        np.full(n_points, 25.0), np.full(n_points, 40.0),
        np.full(n_points, 15.0), np.full(n_points, 0.0),
        183, 0,
    )
    assert np.allclose(result2["FFMC"], result4["FFMC"]), "JSON round-trip broke state"

    logger.info("  TEST 3 PASSED ✓")


def print_summary(storage_results, pipeline_results):
    """Print a summary of all benchmark results."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("BENCHMARK SUMMARY")
    logger.info("=" * 60)

    if storage_results:
        logger.info("")
        logger.info("Storage (1K points, 48 hours, 3 variables):")
        logger.info("  Accumulate:     %.3fs", storage_results["accumulate"])
        logger.info("  Flush to DB:    %.3fs", storage_results["flush"])
        logger.info("  Read 1 hour:    %.4fs", storage_results["read_hour"])
        logger.info("  Gen web files:  %.3fs", storage_results["web_gen"])
        logger.info("  Export 1 CSV:   %.3fs", storage_results["csv_export"])
        logger.info("  DB size:        %.1f KB", storage_results["db_size_kb"])
        logger.info("  CSV size:       %.1f KB", storage_results["csv_size_kb"])
        logger.info("  .bin size:      %.1f KB", storage_results["bin_size_kb"])

    if pipeline_results:
        n = pipeline_results["n_points"]
        logger.info("")
        logger.info("Pipeline (%d points):", n)
        logger.info("  S3 dataset open:    %.3fs", pipeline_results["s3_open"])
        logger.info("  Day fetch (1st):    %.3fs", pipeline_results["day_fetch_1"])
        logger.info("  Day fetch (2nd):    %.3fs", pipeline_results["day_fetch_2"])
        logger.info("  Parallel 2-day:     %.3fs (%.1f%% of sequential)",
                     pipeline_results["parallel_2day"],
                     100 * pipeline_results["parallel_2day"] /
                     (pipeline_results["day_fetch_1"] + pipeline_results["day_fetch_2"]))
        logger.info("  Compute 24h:        %.3fs (%.4fs/hour)",
                     pipeline_results["compute_24h"], pipeline_results["compute_per_hour"])
        logger.info("  SQLite acc 24h:     %.3fs", pipeline_results["sqlite_acc_24h"])
        logger.info("  SQLite flush 24h:   %.3fs", pipeline_results["sqlite_flush_24h"])
        logger.info("  Web gen 24h:        %.3fs", pipeline_results["web_gen_24h"])

        # Projected full month
        day_fetch = pipeline_results["day_fetch_2"]  # Use cached DS time
        compute = pipeline_results["compute_24h"]
        flush = pipeline_results["sqlite_flush_24h"]
        web = pipeline_results["web_gen_24h"]

        # With prefetch: fetch overlaps with compute, so bottleneck = max(fetch, compute)
        day_sequential = day_fetch + compute + flush / 31
        day_prefetch = max(day_fetch, compute) + flush / 31
        month_sequential = day_sequential * 31 + web
        month_prefetch = day_prefetch * 31 + web

        logger.info("")
        logger.info("PROJECTED FULL MONTH (31 days):")
        logger.info("  Without prefetch:  %.1fs (%.1f min)", month_sequential, month_sequential / 60)
        logger.info("  With prefetch:     %.1fs (%.1f min)", month_prefetch, month_prefetch / 60)
        logger.info("  Prefetch speedup:  %.1f%%", 100 * (1 - month_prefetch / month_sequential))

        # Bottleneck analysis
        logger.info("")
        logger.info("BOTTLENECK ANALYSIS:")
        total = day_fetch + compute + flush / 31
        logger.info("  S3 fetch:    %.1f%% (%.3fs/day)", 100 * day_fetch / total, day_fetch)
        logger.info("  Compute:     %.1f%% (%.3fs/day)", 100 * compute / total, compute)
        logger.info("  DB write:    %.1f%% (%.3fs/day)", 100 * (flush / 31) / total, flush / 31)

        if day_fetch > compute:
            logger.info("  → S3 I/O BOUND: fetch takes %.1f× longer than compute", day_fetch / compute)
            logger.info("  → Further optimization: increase prefetch depth or use async I/O")
        else:
            logger.info("  → COMPUTE BOUND: compute takes %.1f× longer than fetch", compute / day_fetch)
            logger.info("  → Further optimization: vectorize fire index math or use numba")


if __name__ == "__main__":
    logger.info("AORC Wildfire Risk Pipeline — Benchmark Suite")
    logger.info("=" * 60)

    # Test 1: SQLite storage (always runs, no S3 needed)
    storage_results = test_sqlite_storage()

    # Test 2: Full pipeline with S3 (requires points_index.csv + internet)
    pipeline_results = None
    try:
        pipeline_results = test_extraction_pipeline_timing()
    except Exception as e:
        logger.error("Pipeline test failed: %s", e)
        import traceback
        traceback.print_exc()

    # Test 3: Carry-forward state
    test_carry_forward_state()

    # Summary
    print_summary(storage_results, pipeline_results)
