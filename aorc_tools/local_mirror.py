"""
Local AORC data mirror — downloads and caches AORC point data locally.

Downloads only the exact grid points needed (28K land points in WUP bbox),
not the full CONUS grid or even the full bbox. This is ~300× less data
than the full AORC ZARR archive.

Storage format: one .npz file per month containing:
    - timestamps: array of datetime strings
    - {variable_name}: float32 array (n_hours × n_points) for each AORC var

Directory structure:
    {mirror_dir}/
        YYYY/
            MM/
                aorc_YYYY_MM.npz     (~200-300 MB compressed per month)
        manifest.json                (tracks what's been downloaded)

Usage:
    mirror = LocalAORCMirror("./data/aorc_local", points_df)
    mirror.download_month(2020, 7)       # Download one month
    mirror.download_range(1979, 2024)    # Download full timeline
    data = mirror.load_month(2020, 7)    # Load from local disk (instant)
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class LocalAORCMirror:
    """Download and cache AORC point data locally for fast offline access."""

    def __init__(self, mirror_dir, points_df, loader=None):
        """Initialize the local mirror.

        Args:
            mirror_dir: Path to local storage directory.
            points_df: DataFrame with point_id, latitude, longitude, lat_idx, lon_idx.
            loader: AORCDataLoader instance (created if None).
        """
        self.mirror_dir = Path(mirror_dir)
        self.mirror_dir.mkdir(parents=True, exist_ok=True)
        self.points_df = points_df
        self.n_points = len(points_df)
        self.lat_indices = points_df["lat_idx"].values
        self.lon_indices = points_df["lon_idx"].values
        self.lat_bounds = (
            points_df["latitude"].min() - 0.05,
            points_df["latitude"].max() + 0.05,
        )
        self.lon_bounds = (
            points_df["longitude"].min() - 0.05,
            points_df["longitude"].max() + 0.05,
        )
        self._loader = loader
        self._manifest = self._load_manifest()

    @property
    def loader(self):
        """Lazy-load the AORC S3 loader."""
        if self._loader is None:
            from aorc_tools.aorc_access import AORCDataLoader
            self._loader = AORCDataLoader()
        return self._loader

    # ------------------------------------------------------------------
    # Manifest tracking
    # ------------------------------------------------------------------

    def _manifest_path(self):
        return self.mirror_dir / "manifest.json"

    def _load_manifest(self):
        path = self._manifest_path()
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return {"downloaded": {}, "n_points": 0, "last_sync": None}

    def _save_manifest(self):
        self._manifest["n_points"] = self.n_points
        self._manifest["last_sync"] = datetime.utcnow().isoformat() + "Z"
        with open(self._manifest_path(), "w") as f:
            json.dump(self._manifest, f, indent=2)

    def is_downloaded(self, year, month):
        """Check if a month's data has been downloaded."""
        key = f"{year}-{month:02d}"
        return key in self._manifest.get("downloaded", {})

    def _month_path(self, year, month):
        return self.mirror_dir / str(year) / f"{month:02d}" / f"aorc_{year}_{month:02d}.npz"

    # ------------------------------------------------------------------
    # Download from S3
    # ------------------------------------------------------------------

    def download_month(self, year, month, force=False):
        """Download one month of AORC data for all points.

        Uses the optimized multi-day batch fetch (6-day chunks aligned
        to ZARR boundaries) for maximum S3 throughput.

        Args:
            year, month: Time period.
            force: Re-download even if already cached.

        Returns:
            Path to the saved .npz file.
        """
        import calendar
        from concurrent.futures import ThreadPoolExecutor

        key = f"{year}-{month:02d}"
        npz_path = self._month_path(year, month)

        if not force and npz_path.exists() and self.is_downloaded(year, month):
            logger.info("Already downloaded: %s", key)
            return npz_path

        npz_path.parent.mkdir(parents=True, exist_ok=True)
        n_days = calendar.monthrange(year, month)[1]

        logger.info("Downloading AORC %s (%d days, %d points)...",
                     key, n_days, self.n_points)

        # Build 6-day chunk schedule
        CHUNK_DAYS = 6
        chunks = []
        d = 1
        while d <= n_days:
            end = min(d + CHUNK_DAYS - 1, n_days)
            chunks.append((d, end))
            d = end + 1

        # Download all chunks with parallel prefetch
        all_timestamps = []
        var_data = {}  # variable → list of (n_hours, n_points) arrays

        executor = ThreadPoolExecutor(max_workers=2)

        # Prefetch first chunk
        future = executor.submit(
            self.loader.get_multiday_point_values,
            year, month, chunks[0][0], chunks[0][1],
            self.lat_indices, self.lon_indices,
            self.lat_bounds, self.lon_bounds,
        )

        t_start = time.perf_counter()
        for i, (start_day, end_day) in enumerate(chunks):
            chunk_data = future.result(timeout=300)

            # Prefetch next chunk
            if i + 1 < len(chunks):
                future = executor.submit(
                    self.loader.get_multiday_point_values,
                    year, month, chunks[i + 1][0], chunks[i + 1][1],
                    self.lat_indices, self.lon_indices,
                    self.lat_bounds, self.lon_bounds,
                )

            # Collect data from each day in the chunk
            for day in range(start_day, end_day + 1):
                if day not in chunk_data:
                    continue
                daily = chunk_data[day]
                for ts in daily["timestamps"]:
                    all_timestamps.append(ts.strftime("%Y-%m-%d %H:%M"))

                for var_name in self.loader.VARIABLES:
                    if var_name in daily:
                        if var_name not in var_data:
                            var_data[var_name] = []
                        var_data[var_name].append(daily[var_name])

        executor.shutdown(wait=False)
        t_download = time.perf_counter() - t_start

        # Concatenate all hours for each variable
        save_dict = {"timestamps": np.array(all_timestamps)}
        for var_name, arrays in var_data.items():
            save_dict[var_name] = np.concatenate(arrays, axis=0).astype(np.float32)

        # Save compressed
        t_save = time.perf_counter()
        np.savez_compressed(npz_path, **save_dict)
        t_save = time.perf_counter() - t_save

        size_mb = npz_path.stat().st_size / 1048576
        n_hours = len(all_timestamps)

        logger.info("Saved %s: %.1f MB (%d hours × %d points × %d vars) "
                     "[download: %.1fs, save: %.1fs]",
                     npz_path.name, size_mb, n_hours, self.n_points,
                     len(var_data), t_download, t_save)

        # Update manifest
        self._manifest.setdefault("downloaded", {})[key] = {
            "path": str(npz_path),
            "size_mb": round(size_mb, 1),
            "n_hours": n_hours,
            "n_vars": len(var_data),
            "downloaded_at": datetime.utcnow().isoformat() + "Z",
        }
        self._save_manifest()

        return npz_path

    def download_range(self, start_year, end_year, start_month=1, end_month=12,
                       callback=None):
        """Download a range of months.

        Args:
            start_year, end_year: Year range (inclusive).
            start_month, end_month: Month range within each year.
            callback: Optional function(year, month, status_msg).

        Returns:
            Number of months downloaded.
        """
        count = 0
        total = 0
        for year in range(start_year, end_year + 1):
            sm = start_month if year == start_year else 1
            em = end_month if year == end_year else 12
            for month in range(sm, em + 1):
                total += 1

        done = 0
        for year in range(start_year, end_year + 1):
            sm = start_month if year == start_year else 1
            em = end_month if year == end_year else 12
            for month in range(sm, em + 1):
                done += 1
                if self.is_downloaded(year, month):
                    if callback:
                        callback(year, month, f"[{done}/{total}] Already cached")
                    continue

                if callback:
                    callback(year, month, f"[{done}/{total}] Downloading...")

                try:
                    self.download_month(year, month)
                    count += 1
                except Exception as e:
                    logger.error("Failed to download %d-%02d: %s", year, month, e)
                    if callback:
                        callback(year, month, f"[{done}/{total}] FAILED: {e}")

        return count

    # ------------------------------------------------------------------
    # Load from local disk
    # ------------------------------------------------------------------

    def load_month(self, year, month):
        """Load a month's AORC data from local disk.

        Returns the same format as AORCDataLoader.get_multiday_point_values()
        but reads from disk instead of S3 (~100× faster).

        Returns:
            dict: day_number → {
                "hours": [0,1,...,23],
                "timestamps": DatetimeIndex,
                {variable}: float32 array (n_hours_in_day, n_points),
            }
        """
        import calendar
        import pandas as pd

        npz_path = self._month_path(year, month)
        if not npz_path.exists():
            raise FileNotFoundError(
                f"No local data for {year}-{month:02d}. "
                f"Run download_month({year}, {month}) first."
            )

        t0 = time.perf_counter()
        data = np.load(npz_path, allow_pickle=True)
        t_load = time.perf_counter() - t0

        timestamps = data["timestamps"]
        n_hours = len(timestamps)

        # Parse timestamps to group by day
        dt_index = pd.DatetimeIndex(timestamps)
        variables = [k for k in data.files if k != "timestamps"]

        result = {}
        n_days = calendar.monthrange(year, month)[1]

        for day in range(1, n_days + 1):
            day_mask = dt_index.day == day
            if not day_mask.any():
                continue

            day_times = dt_index[day_mask]
            daily = {
                "hours": [t.hour for t in day_times],
                "timestamps": day_times,
            }
            for var in variables:
                daily[var] = data[var][day_mask]

            result[day] = daily

        logger.debug("Loaded %s from disk in %.3fs (%d hours, %d vars)",
                      npz_path.name, t_load, n_hours, len(variables))

        return result

    def load_day(self, year, month, day):
        """Load a single day from local disk.

        Returns:
            dict with "hours", "timestamps", and variable arrays.
        """
        month_data = self.load_month(year, month)
        if day not in month_data:
            raise ValueError(f"No data for day {day} in {year}-{month:02d}")
        return month_data[day]

    # ------------------------------------------------------------------
    # Sync / Update Checker
    # ------------------------------------------------------------------

    def check_for_updates(self):
        """Check AORC S3 for months not yet downloaded.

        Checks the latest available year in S3 and reports any months
        that are available but not in the local mirror.

        Returns:
            list of (year, month) tuples that are available but not downloaded.
        """
        missing = []

        # Check what years are available in S3 by trying to open datasets
        # Start from the latest and work backward until we find what we have
        current_year = datetime.utcnow().year
        for year in range(current_year, 1978, -1):
            try:
                self.loader._open_dataset(year)
            except Exception:
                continue

            for month in range(1, 13):
                if not self.is_downloaded(year, month):
                    # Verify this month actually has data
                    import calendar
                    import pandas as pd
                    try:
                        ds = self.loader._open_dataset(year)
                        ts = pd.Timestamp(year, month, 1)
                        if ts <= pd.Timestamp(ds.time.values[-1]):
                            missing.append((year, month))
                    except Exception:
                        pass

            # Stop if we've found all months for years we already have
            all_months_present = all(
                self.is_downloaded(year, m) for m in range(1, 13)
            )
            if all_months_present:
                break

        return sorted(missing)

    def sync(self, callback=None):
        """Download any new months not yet in the local mirror.

        Returns:
            Number of new months downloaded.
        """
        missing = self.check_for_updates()
        if not missing:
            logger.info("Local mirror is up to date")
            return 0

        logger.info("Found %d months to download", len(missing))
        count = 0
        for i, (year, month) in enumerate(missing):
            if callback:
                callback(year, month, f"[{i+1}/{len(missing)}] Syncing...")
            try:
                self.download_month(year, month)
                count += 1
            except Exception as e:
                logger.error("Failed to sync %d-%02d: %s", year, month, e)

        return count

    # ------------------------------------------------------------------
    # Status / Info
    # ------------------------------------------------------------------

    def status(self):
        """Return a summary of the local mirror state."""
        downloaded = self._manifest.get("downloaded", {})
        total_mb = sum(d.get("size_mb", 0) for d in downloaded.values())
        years = set()
        for key in downloaded:
            years.add(int(key.split("-")[0]))

        return {
            "mirror_dir": str(self.mirror_dir),
            "months_downloaded": len(downloaded),
            "years_covered": sorted(years) if years else [],
            "total_size_gb": round(total_mb / 1024, 2),
            "n_points": self.n_points,
            "last_sync": self._manifest.get("last_sync"),
        }
