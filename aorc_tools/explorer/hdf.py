"""
One process-wide lock for every use of the HDF5 library.

The netCDF4 wheels on PyPI ship an HDF5 that is not thread-safe: two threads inside it at the same time can
crash the whole process with a segmentation fault (no Python traceback, the run just disappears). The explorer
reads and writes NetCDF from several threads (job workers, data tasks, the web server), so every netCDF4 call and
every xarray read of a NetCDF file goes through ``HDF_LOCK``:

    with HDF_LOCK:
        with netCDF4.Dataset(path) as ds: ...

    ds = open_dataset(path)     # xarray: every lazy read of this dataset takes HDF_LOCK too

``tests/test_robustness.py`` reproduces the crash without the lock and checks the explorer's own paths with it.
Zarr (AORC on S3) and GeoTIFF reads do not touch HDF5 and need no lock.
"""

from __future__ import annotations

import threading

HDF_LOCK = threading.RLock()


def open_dataset(path, **kw):
    """xarray.open_dataset that serialises all HDF5 access through HDF_LOCK (opening and every later read)."""
    import xarray as xr
    kw.setdefault("lock", HDF_LOCK)
    with HDF_LOCK:
        return xr.open_dataset(path, **kw)


def to_netcdf(ds, path, **kw):
    """Dataset.to_netcdf under HDF_LOCK."""
    with HDF_LOCK:
        if hasattr(ds, "load"):
            ds = ds.load()
        return ds.to_netcdf(path, **kw)
