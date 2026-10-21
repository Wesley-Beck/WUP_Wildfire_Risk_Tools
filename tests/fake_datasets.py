"""
Synthetic climate-model files for offline tests of explorer.datasets:

* ``aorc_like``  -- regular 1/120 deg lat/lon over the WUP, hourly, AORC variable names
                    (values from SyntheticSource; a round trip must reproduce them exactly)
* ``regcm_like`` -- GLARM/RegCM4-style SRF file: ~18 km rotated 2-D lat/lon grid, 3-hourly,
                    CORDEX names (tas, qas, ps, uas, vas, pr flux, rsds), 'noleap' calendar
* ``wrf_like``   -- wrfout-style: XLAT/XLONG(Time, y, x), Times char array, T2, Q2 (mixing
                    ratio), PSFC, U10/V10, RAINNC accumulated, SWDOWN, hourly
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.grid import Brick


def smooth_fields(lat, lon, hours_since_start, doy):
    h = np.asarray(hours_since_start, float)[:, None]
    tas = 285 + 6 * np.sin(2 * np.pi * (h - 15) / 24) - 1.2 * (lat - 47)[None] + 0.3 * np.sin(np.radians(lon) * 30)[None]
    rh = np.clip(65 - 20 * np.sin(2 * np.pi * (h - 15) / 24) + 2 * (lat - 47)[None], 15, 100)
    ps = 98000 - 40 * (lat - 46)[None] + 0 * h
    tc = tas - 273.15
    e = rh / 100 * 611.2 * np.exp(17.67 * tc / (tc + 243.5))
    q = 0.622 * e / (ps - 0.378 * e)
    u = 3 + 2 * np.sin(2 * np.pi * h / 37) + 0 * lat[None]
    v = 1 + 0 * h + 0.5 * np.cos(np.radians(lon) * 20)[None]
    rate = np.where((h % 48) < 6, 1.0e-3 * (1 + 0.2 * np.sin(np.radians(lat) * 40))[None], 0.0)   # kg m-2 s-1 bursts
    sw = np.maximum(np.sin(2 * np.pi * (h - 12) / 24), 0) * 700 + 0 * lat[None]
    return tas, q, ps, u, v, rate, sw


def aorc_like(path: Path, grid, start, hours, bricks=("25_19",)):
    import xarray as xr
    from aorc_tools.explorer.sources import SyntheticSource, VARIABLES
    syn = SyntheticSource(inject_gaps=False)
    la0, la1 = grid.lat_idx_range
    lo0, lo1 = grid.lon_idx_range
    lat = grid.lat0 + np.arange(la0, la1) * grid.step
    lon = grid.lon0 + np.arange(lo0, lo1) * grid.step
    times = pd.date_range(start, periods=hours, freq="h")
    data = {v: np.full((hours, len(lat), len(lon)), np.nan, np.float32) for v in VARIABLES}
    chunks = grid.time_chunks(times[0].to_pydatetime(), times[-1].to_pydatetime(), syn.origin_of_year)
    for bk in bricks:
        ci, cj = map(int, bk.split("_"))
        for ch in chunks:
            arrays, ct, meta, _ = syn._raw_brick_chunk(grid, ch["year"], ch["k"], Brick(ci, cj))
            (a0, a1), (b0, b1) = meta["lat_idx_range"], meta["lon_idx_range"]
            ti = times.get_indexer(ct)
            ok = ti >= 0
            for v in VARIABLES:
                data[v][ti[ok], a0 - la0:a1 - la0, b0 - lo0:b1 - lo0] = arrays[v][ok]
    ds = xr.Dataset({v: (("time", "latitude", "longitude"), data[v]) for v in VARIABLES},
                    coords={"time": times, "latitude": lat, "longitude": lon})
    ds.to_netcdf(path)
    return path


def regcm_like(path: Path, start="2085-05-01", days=20, res_km=18.0, calendar="noleap"):
    import cftime
    import netCDF4
    ny, nx = 26, 30
    # rotated, slightly curvilinear grid around the WUP
    jj, ii = np.meshgrid(np.arange(nx), np.arange(ny))
    lat = 45.6 + ii * res_km / 111.2 + 0.02 * jj
    lon = -91.2 + jj * res_km / (111.2 * np.cos(np.radians(lat))) - 0.03 * ii
    t0 = pd.Timestamp(start)
    nt = days * 8
    hrs = np.arange(nt) * 3.0
    tas, q, ps, u, v, rate, sw = smooth_fields(lat.ravel(), lon.ravel(), hrs, 0)
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("time", nt); ds.createDimension("iy", ny); ds.createDimension("jx", nx)
        t = ds.createVariable("time", "f8", ("time",))
        t.units = f"hours since {t0:%Y-%m-%d %H:%M:%S}"; t.calendar = calendar
        t[:] = hrs
        la = ds.createVariable("xlat", "f4", ("iy", "jx")); la.units = "degrees_north"; la[:] = lat
        lo = ds.createVariable("xlon", "f4", ("iy", "jx")); lo.units = "degrees_east"; lo[:] = lon
        for name, arr, units in (("tas", tas, "K"), ("qas", q, "kg kg-1"), ("ps", ps, "Pa"), ("uas", u, "m s-1"),
                                 ("vas", v, "m s-1"), ("pr", rate, "kg m-2 s-1"), ("rsds", sw, "W m-2")):
            x = ds.createVariable(name, "f4", ("time", "iy", "jx")); x.units = units
            x[:] = np.broadcast_to(arr, (nt, lat.size)).reshape(nt, ny, nx)
        ds.model = "synthetic RegCM4-like SRF (test)"
    return path, (lat, lon), hrs, rate


def wrf_like(path: Path, start="2009-05-13 00:00", hours=72, res_km=4.0):
    import netCDF4
    ny, nx = 60, 70
    jj, ii = np.meshgrid(np.arange(nx), np.arange(ny))
    lat = 45.9 + ii * res_km / 111.2
    lon = -90.6 + jj * res_km / (111.2 * np.cos(np.radians(lat)))
    hrs = np.arange(hours, dtype=float)
    tas, q, ps, u, v, rate, sw = smooth_fields(lat.ravel(), lon.ravel(), hrs, 0)
    w = q / (1 - q)                                                  # mixing ratio
    acc = np.cumsum(rate * 3600.0, axis=0)                            # mm since start (hour ending at stamp)
    times = pd.date_range(start, periods=hours, freq="h")
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("Time", None); ds.createDimension("DateStrLen", 19)
        ds.createDimension("south_north", ny); ds.createDimension("west_east", nx)
        tv = ds.createVariable("Times", "S1", ("Time", "DateStrLen"))
        tv[:] = np.array([list(t.strftime("%Y-%m-%d_%H:%M:%S")) for t in times], dtype="S1")
        for name, arr in (("XLAT", lat), ("XLONG", lon)):
            x = ds.createVariable(name, "f4", ("Time", "south_north", "west_east")); x[:] = np.broadcast_to(arr, (hours, ny, nx))
        for name, arr, units in (("T2", tas, "K"), ("Q2", w, "kg kg-1"), ("PSFC", ps, "Pa"), ("U10", u, "m s-1"),
                                 ("V10", v, "m s-1"), ("RAINNC", acc, "mm"), ("SWDOWN", sw, "W m-2")):
            x = ds.createVariable(name, "f4", ("Time", "south_north", "west_east")); x.units = units
            x[:] = np.broadcast_to(arr, (hours, lat.size)).reshape(hours, ny, nx)
    return path, (lat, lon), hrs, rate
