"""
AORC S3/ZARR data access layer.

Ported from the existing aorc_data_loader.py with the following changes:
- get_hourly_data() returns RAW AORC values (no unit conversions)
- Removed convert_aorc_to_fire_weather() (conversions now in climate_convert.py)
- Added coordinate caching to ~/.aorc_cache/
- Kept the proven multi-method S3 fallback approach

The 5 fallback methods for S3 access are preserved exactly from the
original code since they handle various AWS access configurations.
"""

import os
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Cache directory for AORC coordinates
CACHE_DIR = Path.home() / ".aorc_cache"


class AORCDataLoader:
    """Load hourly AORC data from the NOAA S3 bucket.

    Uses ZARR format via multiple fallback S3 access methods.
    Returns raw AORC values with no unit conversions applied.

    Ported from existing aorc_data_loader.py — same bucket, same
    fallback methods, proven to work with the NOAA AORC archive.
    """

    BUCKET = "noaa-nws-aorc-v1-1-1km"

    # AORC variable names to extract
    VARIABLES = [
        "TMP_2maboveground",
        "SPFH_2maboveground",
        "PRES_surface",
        "UGRD_10maboveground",
        "VGRD_10maboveground",
        "APCP_surface",
        "DSWRF_surface",
        "DLWRF_surface",
    ]

    def __init__(self):
        self._ds_cache = {}  # year → xarray Dataset
        self._coords = None  # cached lat/lon arrays

    # ------------------------------------------------------------------
    # S3 Dataset Access (5 fallback methods from existing code)
    # ------------------------------------------------------------------

    def _open_dataset(self, year):
        """Open AORC ZARR dataset for a given year, trying 5 methods.

        Ported directly from existing aorc_data_loader.py — these
        fallback methods handle different AWS SDK configurations and
        are proven to work in production.
        """
        if year in self._ds_cache:
            return self._ds_cache[year]

        zarr_path = f"{self.BUCKET}/{year}.zarr"
        ds = None
        errors = []

        # Method 1: fsspec mapper (most common)
        try:
            import fsspec
            mapper = fsspec.get_mapper(
                f"s3://{zarr_path}",
                anon=True,
                default_fill_cache=False,
            )
            import xarray as xr
            ds = xr.open_zarr(mapper, consolidated=True)
            logger.info("AORC %d opened via fsspec mapper", year)
        except Exception as e:
            errors.append(f"Method 1 (fsspec): {e}")

        # Method 2: S3Map
        if ds is None:
            try:
                import s3fs
                import xarray as xr
                s3 = s3fs.S3FileSystem(anon=True)
                store = s3fs.S3Map(root=f"s3://{zarr_path}", s3=s3)
                ds = xr.open_zarr(store, consolidated=True)
                logger.info("AORC %d opened via S3Map", year)
            except Exception as e:
                errors.append(f"Method 2 (S3Map): {e}")

        # Method 3: Direct URL
        if ds is None:
            try:
                import xarray as xr
                url = f"https://{self.BUCKET}.s3.amazonaws.com/{year}.zarr"
                ds = xr.open_zarr(url, consolidated=True)
                logger.info("AORC %d opened via direct URL", year)
            except Exception as e:
                errors.append(f"Method 3 (URL): {e}")

        # Method 4: open_dataset with engine='zarr'
        if ds is None:
            try:
                import xarray as xr
                ds = xr.open_dataset(
                    f"s3://{zarr_path}",
                    engine="zarr",
                    backend_kwargs={"storage_options": {"anon": True}},
                )
                logger.info("AORC %d opened via open_dataset", year)
            except Exception as e:
                errors.append(f"Method 4 (open_dataset): {e}")

        # Method 5: Non-consolidated zarr
        if ds is None:
            try:
                import fsspec
                import xarray as xr
                mapper = fsspec.get_mapper(
                    f"s3://{zarr_path}",
                    anon=True,
                )
                ds = xr.open_zarr(mapper, consolidated=False)
                logger.info("AORC %d opened via non-consolidated zarr", year)
            except Exception as e:
                errors.append(f"Method 5 (non-consolidated): {e}")

        if ds is None:
            raise ConnectionError(
                f"Failed to open AORC dataset for {year}. "
                f"All 5 methods failed:\n" + "\n".join(errors)
            )

        self._ds_cache[year] = ds
        return ds

    # ------------------------------------------------------------------
    # Coordinate Access
    # ------------------------------------------------------------------

    def get_coordinates(self, year=2020):
        """Get AORC latitude and longitude 1D arrays.

        Caches to ~/.aorc_cache/coordinates.npz for fast subsequent access.

        Args:
            year: Any valid AORC year (just used to access the dataset
                  for coordinate extraction).

        Returns:
            (lats, lons): 1D numpy arrays of latitude and longitude values.
        """
        if self._coords is not None:
            return self._coords

        # Try cache first
        cache_file = CACHE_DIR / "coordinates.npz"
        if cache_file.exists():
            data = np.load(cache_file)
            self._coords = (data["lats"], data["lons"])
            logger.info("Loaded coordinates from cache (%d lats, %d lons)",
                        len(self._coords[0]), len(self._coords[1]))
            return self._coords

        # Fetch from AORC
        ds = self._open_dataset(year)
        lats = ds.latitude.values
        lons = ds.longitude.values

        # Cache locally
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.savez(cache_file, lats=lats, lons=lons)
        logger.info("Cached AORC coordinates (%d lats, %d lons)", len(lats), len(lons))

        self._coords = (lats, lons)
        return self._coords

    def get_coordinate_indices(self, target_lats, target_lons, year=2020):
        """Find the nearest AORC grid indices for given lat/lon points.

        Args:
            target_lats: Array of target latitudes.
            target_lons: Array of target longitudes.
            year: AORC year for coordinate lookup.

        Returns:
            (lat_indices, lon_indices): Arrays of integer indices into the
            AORC grid. Use with .isel(latitude=lat_idx, longitude=lon_idx).
        """
        lats, lons = self.get_coordinates(year)
        target_lats = np.asarray(target_lats)
        target_lons = np.asarray(target_lons)

        lat_indices = np.abs(lats[:, None] - target_lats[None, :]).argmin(axis=0)
        lon_indices = np.abs(lons[:, None] - target_lons[None, :]).argmin(axis=0)

        return lat_indices, lon_indices

    # ------------------------------------------------------------------
    # Spatial Subsetting Helpers
    # ------------------------------------------------------------------

    def _get_bbox_indices(self, lat_bounds, lon_bounds, year=2020):
        """Get AORC grid index slices for a lat/lon bounding box.

        Returns:
            (lat_slice, lon_slice, lat_offset, lon_offset): The slice objects
            for subsetting and the starting index offsets for remapping.
        """
        lats, lons = self.get_coordinates(year)

        # Find index range for latitude (AORC lats are ascending)
        lat_mask = (lats >= lat_bounds[0]) & (lats <= lat_bounds[1])
        lat_idxs = np.where(lat_mask)[0]
        lat_start, lat_end = lat_idxs[0], lat_idxs[-1] + 1

        # Find index range for longitude (AORC lons may be negative)
        lon_mask = (lons >= lon_bounds[0]) & (lons <= lon_bounds[1])
        lon_idxs = np.where(lon_mask)[0]
        lon_start, lon_end = lon_idxs[0], lon_idxs[-1] + 1

        return (
            slice(lat_start, lat_end),
            slice(lon_start, lon_end),
            lat_start,
            lon_start,
        )

    def remap_indices_to_bbox(self, lat_indices, lon_indices, lat_offset, lon_offset):
        """Remap full-grid point indices to a spatial subset.

        Args:
            lat_indices: Point lat indices into the full AORC grid.
            lon_indices: Point lon indices into the full AORC grid.
            lat_offset: Starting lat index of the bbox subset.
            lon_offset: Starting lon index of the bbox subset.

        Returns:
            (local_lat_idx, local_lon_idx): Indices into the bbox subset arrays.
        """
        return lat_indices - lat_offset, lon_indices - lon_offset

    # ------------------------------------------------------------------
    # Batch Data Extraction (optimized)
    # ------------------------------------------------------------------

    def get_multiday_point_values(self, year, month, start_day, end_day,
                                  lat_indices, lon_indices, lat_bounds, lon_bounds):
        """Extract multiple days of AORC data in a single S3 read.

        AORC ZARR chunks span 144 hours (6 days) on the time axis. Fetching
        multiple days at once amortizes S3 overhead across chunk boundaries,
        giving ~5× less redundant data transfer than single-day fetches.

        Args:
            year, month: Time period.
            start_day, end_day: Day range (inclusive).
            lat_indices: Array of latitude indices (full-grid).
            lon_indices: Array of longitude indices (full-grid).
            lat_bounds: (lat_min, lat_max) tuple.
            lon_bounds: (lon_min, lon_max) tuple.

        Returns:
            dict: day_number → daily_data dict (same format as get_daily_point_values).
                  Each daily_data has "hours", "timestamps", and variable arrays.
        """
        import pandas as pd

        ds = self._open_dataset(year)

        lat_sl, lon_sl, lat_off, lon_off = self._get_bbox_indices(
            lat_bounds, lon_bounds, year
        )
        local_lat, local_lon = self.remap_indices_to_bbox(
            lat_indices, lon_indices, lat_off, lon_off
        )

        # Load the full multi-day time range at once
        start_ts = pd.Timestamp(year, month, start_day, 0)
        end_ts = pd.Timestamp(year, month, end_day, 23)

        subset = ds.isel(latitude=lat_sl, longitude=lon_sl)
        multi = subset.sel(time=slice(start_ts, end_ts))

        actual_times = pd.DatetimeIndex(multi.time.values)

        # Load all variable data at once (single S3 read per variable)
        var_data = {}
        for var in self.VARIABLES:
            if var in multi:
                var_data[var] = multi[var].values[:, local_lat, local_lon]

        # Split by day
        result = {}
        for day in range(start_day, end_day + 1):
            day_mask = actual_times.day == day
            day_times = actual_times[day_mask]

            if len(day_times) == 0:
                continue

            daily = {
                "hours": [t.hour for t in day_times],
                "timestamps": day_times,
            }
            for var, data in var_data.items():
                daily[var] = data[day_mask]

            result[day] = daily

        logger.debug("Loaded %d days (%d-%02d-%02d to %02d): %d total hours",
                     len(result), year, month, start_day, end_day,
                     sum(len(d["hours"]) for d in result.values()))

        return result

    def get_daily_point_values(self, year, month, day, lat_indices, lon_indices,
                               lat_bounds, lon_bounds):
        """Extract a full day (24h) of AORC data at point locations.

        Uses spatial subsetting to load only the bounding box region,
        reducing data volume by ~350× compared to loading full CONUS.
        Loads all 24 hours at once for the spatial subset.

        Args:
            year, month, day: Date components.
            lat_indices: Array of latitude indices (full-grid).
            lon_indices: Array of longitude indices (full-grid).
            lat_bounds: (lat_min, lat_max) tuple.
            lon_bounds: (lon_min, lon_max) tuple.

        Returns:
            dict: Variable name → 2D numpy array (24 hours × n_points),
                  raw AORC units. Hour index 0 = 00:00 UTC.
                  Also includes "hours" key → list of actual hour values.
        """
        import pandas as pd

        ds = self._open_dataset(year)

        # Spatial subset using isel (integer indexing, avoids coord lookup overhead)
        lat_sl, lon_sl, lat_off, lon_off = self._get_bbox_indices(
            lat_bounds, lon_bounds, year
        )
        local_lat, local_lon = self.remap_indices_to_bbox(
            lat_indices, lon_indices, lat_off, lon_off
        )

        # Time range for this day
        start_ts = pd.Timestamp(year, month, day, 0)
        end_ts = pd.Timestamp(year, month, day, 23)

        # Select spatial bbox and time range from the dataset
        subset = ds.isel(latitude=lat_sl, longitude=lon_sl)
        daily = subset.sel(time=slice(start_ts, end_ts))

        # Get actual timestamps present
        actual_times = pd.DatetimeIndex(daily.time.values)
        n_hours = len(actual_times)
        hours = [t.hour for t in actual_times]

        result = {"hours": hours, "timestamps": actual_times}

        for var in self.VARIABLES:
            if var not in daily:
                logger.warning("Variable %s not found for %s-%02d-%02d", var, year, month, day)
                continue

            # Load the full day's bbox data at once: shape (n_hours, n_lat, n_lon)
            data = daily[var].values

            # Extract point values for all hours at once
            # data[:, local_lat, local_lon] → shape (n_hours, n_points)
            result[var] = data[:, local_lat, local_lon]

        return result

    # ------------------------------------------------------------------
    # Single-Hour Access (kept for backward compatibility / status checks)
    # ------------------------------------------------------------------

    def get_hourly_point_values(self, year, month, day, hour, lat_indices, lon_indices,
                                lat_bounds=None, lon_bounds=None):
        """Extract one hour of AORC data at specific grid point indices.

        If lat_bounds/lon_bounds are provided, uses spatial subsetting
        for much faster extraction (~350× less data than full CONUS).

        Args:
            year, month, day, hour: Timestamp components.
            lat_indices: Array of latitude indices (from get_coordinate_indices).
            lon_indices: Array of longitude indices.
            lat_bounds: Optional (lat_min, lat_max) for spatial subsetting.
            lon_bounds: Optional (lon_min, lon_max) for spatial subsetting.

        Returns:
            dict: Variable name → 1D numpy array (one value per point), raw units.
        """
        import pandas as pd

        ds = self._open_dataset(year)
        timestamp = pd.Timestamp(year, month, day, hour)

        if lat_bounds is not None and lon_bounds is not None:
            # Optimized path: spatial subsetting first
            lat_sl, lon_sl, lat_off, lon_off = self._get_bbox_indices(
                lat_bounds, lon_bounds, year
            )
            local_lat, local_lon = self.remap_indices_to_bbox(
                lat_indices, lon_indices, lat_off, lon_off
            )
            subset = ds.isel(latitude=lat_sl, longitude=lon_sl)
            hourly = subset.sel(time=timestamp, method="nearest")

            result = {}
            for var in self.VARIABLES:
                if var in hourly:
                    data = hourly[var].values
                    result[var] = data[local_lat, local_lon]
            return result
        else:
            # Legacy path: loads full CONUS (slow)
            hourly = ds.sel(time=timestamp, method="nearest")
            result = {}
            for var in self.VARIABLES:
                if var in hourly:
                    data = hourly[var].values
                    result[var] = data[lat_indices, lon_indices]
            return result
