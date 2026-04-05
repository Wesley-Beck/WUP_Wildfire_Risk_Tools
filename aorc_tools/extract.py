"""
Hourly data extraction pipeline.

Orchestrates: AORC data fetch → unit conversion → fire index calculation
→ SQLite storage + binary web files + optional CSV export.

Optimized with:
- Parallel S3 day-downloads (prefetch next day while processing current)
- SQLite storage (3× smaller than CSV, instant indexed queries)
- Binary web files (.bin) for fast browser loading (no CSV parsing)
- Optional CSV export with configurable precision
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.aorc_access import AORCDataLoader
from aorc_tools.climate_convert import (
    kelvin_to_celsius,
    celsius_to_fahrenheit,
    wind_components_to_speed,
    ms_to_kph,
    ms_to_mph,
    specific_to_relative_humidity,
)
from aorc_tools.fire_indices.fwi import HourlyFWI
from aorc_tools.fire_indices.nfdrs import compute_erc_bi
from aorc_tools.fire_indices.fuel_moisture import emc_fuel_moisture, NelsonFuelMoisture
from aorc_tools.metadata import generate_metadata, save_metadata
from aorc_tools.storage import SQLiteStorage

logger = logging.getLogger(__name__)


class CSVAccumulator:
    """Accumulates hourly point data and writes monthly CSV files."""

    def __init__(self, n_points, point_ids):
        self.n_points = n_points
        self.point_ids = point_ids
        self.data = {}

    def add(self, name, timestamp, values):
        if name not in self.data:
            self.data[name] = []
        self.data[name].append((timestamp, np.asarray(values)))

    def save(self, output_dir, prefix=""):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, records in self.data.items():
            if not records:
                continue
            timestamps = [r[0] for r in records]
            columns = [t.strftime("%Y-%m-%d %H:%M") for t in timestamps]
            data_matrix = np.column_stack([r[1] for r in records])
            df = pd.DataFrame(data_matrix, index=self.point_ids, columns=columns)
            df.index.name = "point_id"
            df.to_csv(output_dir / f"{prefix}{name}.csv")
        logger.info("Saved %d CSV files to %s", len(self.data), output_dir)

    def clear(self):
        self.data.clear()


def _prefetch_day(loader, year, month, day, lat_indices, lon_indices,
                  lat_bounds, lon_bounds):
    """Download one day's AORC data (runs in background thread)."""
    return loader.get_daily_point_values(
        year, month, day, lat_indices, lon_indices, lat_bounds, lon_bounds,
    )


def extract_month(year, month, points_df, output_base, loader=None,
                  fuel_model="G", fuel_moisture_method="emc",
                  fwi_state=None, fm_state=None, latitude=46.5,
                  callback=None, output_format="both",
                  csv_precision=None, skip_raw=False):
    """Extract one month of hourly AORC data and compute all indices.

    Uses parallel S3 prefetching: downloads the next day while processing
    the current one, roughly doubling throughput.

    Args:
        year, month: Time period.
        points_df: DataFrame with point_id, latitude, longitude, lat_idx, lon_idx.
        output_base: Base output directory path.
        loader: AORCDataLoader instance (created if None).
        fuel_model: NFDRS fuel model code (default "G").
        fuel_moisture_method: "emc" or "nelson".
        fwi_state, fm_state: Carry-forward state dicts.
        latitude: Representative latitude for FWI sunrise/sunset.
        callback: Optional function(progress_pct, message).
        output_format: "sqlite" | "csv" | "both" (default "both").
        csv_precision: Decimal places for CSV export (None = full precision).
        skip_raw: If True, skip saving raw AORC variables (saves ~30% time/space).

    Returns:
        dict with "fwi_state" and "fm_state" for carry-forward.
    """
    if loader is None:
        loader = AORCDataLoader()

    n_points = len(points_df)
    point_ids = points_df["point_id"].values
    lat_indices = points_df["lat_idx"].values
    lon_indices = points_df["lon_idx"].values

    lat_bounds = (
        points_df["latitude"].min() - 0.05,
        points_df["latitude"].max() + 0.05,
    )
    lon_bounds = (
        points_df["longitude"].min() - 0.05,
        points_df["longitude"].max() + 0.05,
    )

    month_dir = Path(output_base) / str(year) / f"{month:02d}"
    use_sqlite = output_format in ("sqlite", "both")
    use_csv = output_format in ("csv", "both")

    # Initialize SQLite storage
    db = None
    if use_sqlite:
        db = SQLiteStorage(output_base, year, month)
        db.open()
        db.store_points(points_df)

    # Initialize CSV accumulators
    raw_acc = CSVAccumulator(n_points, point_ids) if use_csv and not skip_raw else None
    conv_acc = CSVAccumulator(n_points, point_ids) if use_csv else None
    cfwi_acc = CSVAccumulator(n_points, point_ids) if use_csv else None
    nfdrs_acc = CSVAccumulator(n_points, point_ids) if use_csv else None

    # Initialize fire index models
    fwi = HourlyFWI(n_points, latitude=latitude, initial_state=fwi_state)
    fm_model = NelsonFuelMoisture(n_points, initial_fm=fm_state) if fuel_moisture_method == "nelson" else None

    import calendar
    n_days = calendar.monthrange(year, month)[1]
    total_hours = n_days * 24
    hours_processed = 0

    # Parallel prefetch: download next day while processing current day
    executor = ThreadPoolExecutor(max_workers=2)

    prefetch_future = executor.submit(
        _prefetch_day, loader, year, month, 1,
        lat_indices, lon_indices, lat_bounds, lon_bounds,
    )

    for day in range(1, n_days + 1):
        if callback:
            pct = 100.0 * hours_processed / total_hours
            callback(pct, f"Processing {year}-{month:02d}-{day:02d}...")

        try:
            daily_data = prefetch_future.result(timeout=120)
        except Exception as e:
            logger.error("Failed to fetch AORC day %s-%02d-%02d: %s", year, month, day, e)
            hours_processed += 24
            if day < n_days:
                prefetch_future = executor.submit(
                    _prefetch_day, loader, year, month, day + 1,
                    lat_indices, lon_indices, lat_bounds, lon_bounds,
                )
            continue

        # Prefetch NEXT day while processing this one
        if day < n_days:
            prefetch_future = executor.submit(
                _prefetch_day, loader, year, month, day + 1,
                lat_indices, lon_indices, lat_bounds, lon_bounds,
            )

        actual_hours = daily_data["hours"]
        actual_timestamps = daily_data["timestamps"]

        for h_idx, (hour_val, ts) in enumerate(zip(actual_hours, actual_timestamps)):
            current = datetime(year, month, day, hour_val)
            doy = current.timetuple().tm_yday

            raw = {}
            for var in loader.VARIABLES:
                if var in daily_data:
                    raw[var] = daily_data[var][h_idx]

            # Raw AORC
            if not skip_raw:
                if db:
                    for var, values in raw.items():
                        db.add(var, current, values)
                if raw_acc:
                    for var, values in raw.items():
                        raw_acc.add(var, current, values)

            # Unit conversions
            temp_k = raw.get("TMP_2maboveground", np.full(n_points, np.nan))
            spfh = raw.get("SPFH_2maboveground", np.full(n_points, np.nan))
            pres_pa = raw.get("PRES_surface", np.full(n_points, np.nan))
            ugrd = raw.get("UGRD_10maboveground", np.full(n_points, np.nan))
            vgrd = raw.get("VGRD_10maboveground", np.full(n_points, np.nan))
            precip_mm = raw.get("APCP_surface", np.full(n_points, np.nan))

            temp_c = kelvin_to_celsius(temp_k)
            temp_f = celsius_to_fahrenheit(temp_c)
            rh = specific_to_relative_humidity(spfh, temp_k, pres_pa)
            ws_ms = wind_components_to_speed(ugrd, vgrd)
            ws_kph = ms_to_kph(ws_ms)
            ws_mph = ms_to_mph(ws_ms)

            converted = {
                "temperature_c": temp_c, "temperature_f": temp_f,
                "relative_humidity": rh, "wind_speed_ms": ws_ms,
                "wind_speed_kph": ws_kph, "wind_speed_mph": ws_mph,
                "precipitation_mm": precip_mm,
            }
            if db:
                for key, values in converted.items():
                    db.add(key, current, values)
            if conv_acc:
                for key, values in converted.items():
                    conv_acc.add(key, current, values)

            # Canadian FWI
            fwi_result = fwi.update(temp_c, rh, ws_kph, precip_mm, doy, current.hour)
            if db:
                for key, values in fwi_result.items():
                    db.add(key, current, values)
            if cfwi_acc:
                for key, values in fwi_result.items():
                    cfwi_acc.add(key, current, values)

            # NFDRS
            if fuel_moisture_method == "nelson" and fm_model is not None:
                fm = fm_model.update(temp_f, rh, precip_mm)
            else:
                fm = emc_fuel_moisture(temp_f, rh)

            all_nfdrs = {"FM1": fm["fm1"], "FM10": fm["fm10"],
                         "FM100": fm["fm100"], "FM1000": fm["fm1000"]}
            nfdrs_result = compute_erc_bi(
                fm["fm1"], fm["fm10"], fm["fm100"], fm["fm1000"],
                np.full(n_points, 120.0), np.full(n_points, 100.0),
                ws_mph, fuel_model=fuel_model,
            )
            all_nfdrs.update(nfdrs_result)

            if db:
                for key, values in all_nfdrs.items():
                    db.add(key, current, values)
            if nfdrs_acc:
                for key, values in all_nfdrs.items():
                    nfdrs_acc.add(key, current, values)

            hours_processed += 1

    executor.shutdown(wait=False)

    # Flush SQLite and generate web files
    if db:
        db.flush()
        db.generate_web_files()
        db.close()

    # Save CSVs
    if use_csv:
        prefix = f"{year}_{month:02d}_"
        if raw_acc:
            raw_acc.save(month_dir / "raw_aorc", prefix)
        if conv_acc:
            conv_acc.save(month_dir / "converted", prefix)
        if cfwi_acc:
            cfwi_acc.save(month_dir / "cfwi", prefix)
        if nfdrs_acc:
            nfdrs_acc.save(month_dir / "nfdrs", prefix)

    # Save carry-forward state
    state_dir = month_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    fwi_state_out = fwi.get_state()
    with open(state_dir / f"fwi_state_{year}_{month:02d}.json", "w") as f:
        json.dump(fwi_state_out, f)

    fm_state_out = fm_model.get_state() if fm_model else None
    if fm_state_out:
        with open(state_dir / f"nfdrs_state_{year}_{month:02d}.json", "w") as f:
            json.dump({k: v.tolist() if hasattr(v, "tolist") else v
                       for k, v in fm_state_out.items()}, f)

    metadata = generate_metadata(
        year, month, fuel_model,
        calculation_params={
            "fuel_moisture_method": fuel_moisture_method,
            "latitude": latitude,
            "n_points": n_points,
            "output_format": output_format,
        },
    )
    save_metadata(metadata, month_dir / f"metadata_{year}_{month:02d}.json")

    if callback:
        callback(100.0, f"Completed {year}-{month:02d}")

    return {"fwi_state": fwi_state_out, "fm_state": fm_state_out}


def extract_year(year, points_df, output_base, start_month=1, end_month=12,
                 **kwargs):
    """Extract a full year of data, month by month with state carry-forward."""
    state = {"fwi_state": kwargs.pop("fwi_state", None),
             "fm_state": kwargs.pop("fm_state", None)}

    for month in range(start_month, end_month + 1):
        logger.info("Extracting %d-%02d", year, month)
        state = extract_month(
            year, month, points_df, output_base,
            fwi_state=state["fwi_state"],
            fm_state=state["fm_state"],
            **kwargs,
        )

    return state
