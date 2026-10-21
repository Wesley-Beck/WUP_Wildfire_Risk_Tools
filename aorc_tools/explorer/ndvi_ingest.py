"""
NDVI ingest for FPI: download, quality-screen, regrid to the 800 m AORC cells,
interpolate the 16-day composites to daily, and write the files ``ndvi.NDVIProvider``
reads (see that module for the layout and the NDVI resolution ladder).

Backends (all end in the same regridding step):

* ``planetary``  MODIS MOD13Q1 (250 m) / MOD13A1 (500 m), Collection 6.1, from the Microsoft
                 Planetary Computer STAC API (cloud-optimised GeoTIFFs, no login).
* ``appeears``   MODIS MOD13Q1/A1/A2 from NASA AppEEARS (area request -> NetCDF), needs a
                 free NASA Earthdata login (EARTHDATA_USERNAME / EARTHDATA_PASSWORD, never stored).
* ``cdr``        NOAA Climate Data Record AVHRR/VIIRS NDVI v5 (0.05 deg, daily, 1981->) from the
                 public AWS bucket noaa-cdr-ndvi-pds, built into 16-day maximum-value composites.
                 Opt-in tier for pre-2000 FPI (outside the locked MODIS-era decision).
* ``local``      a folder of NDVI GeoTIFF/NetCDF files you already have (date in the file name).

Regridding: pixels finer than the cell (250/500 m) are averaged into the cell whose
footprint holds their centre (QA-screened pixels only); coarser pixels (1 km, 5 km)
are sampled at the cell centre. MODIS QA: pixel reliability 0 (good) and 1 (marginal)
are kept, 2 (snow/ice) and 3 (cloudy) are dropped.

Output (int16, scale 1e-4, zlib):
    <static>/ndvi/<PRODUCT>/FPI_Input_<year>.nc   ND0(time=daily, latitude, longitude) + ND_composite
    <static>/FPI_Input_Static.nc                  ND_min, ND_max (per cell, from the archive), LLfm, DLfm, MXd
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.workspace import now_iso

logger = logging.getLogger(__name__)
SIN_R = 6371007.181                       # MODIS sinusoidal sphere radius (m)
FILL = np.int16(-32768)
SCALE = 1e-4
COMP = dict(compression="zlib", complevel=6, shuffle=True)
PRODUCT_DIR = {"MOD13Q1": "MOD13Q1", "MOD13A1": "MOD13A1", "MOD13A2": "MOD13A2", "CDR": "AVHRR_CDR", "LOCAL": "MOD13Q1"}
PIXEL_M = {"MOD13Q1": 231.656, "MOD13A1": 463.313, "MOD13A2": 926.625, "CDR": 5550.0}
MAX_GAP_DAYS = 48                          # bridge at most two missing composites (48 d between mid-dates)


def composite_starts(year):
    """MODIS 16-day composite start dates of a year (DOY 1, 17, ..., 353)."""
    return [pd.Timestamp(year, 1, 1) + pd.Timedelta(days=d) for d in range(0, 365, 16)]


# ---------------------------------------------------------------------------
# regridding to the AORC cells
# ---------------------------------------------------------------------------

class CellGrid:
    """The explorer grid seen as a lookup table: (lat, lon) -> study-cell row, and the WUP rectangle."""

    def __init__(self, grid):
        self.grid = grid
        self.la0, la1 = grid.lat_idx_range
        self.lo0, lo1 = grid.lon_idx_range
        self.ny, self.nx = la1 - self.la0, lo1 - self.lo0
        self.n = len(grid.points)
        self.y = (grid.points.lat_idx.values - self.la0).astype(np.int64)
        self.x = (grid.points.lon_idx.values - self.lo0).astype(np.int64)
        self.rowmap = np.full((self.ny, self.nx), -1, dtype=np.int64)
        self.rowmap[self.y, self.x] = np.arange(self.n)
        self.lat = grid.lat0 + (self.la0 + np.arange(self.ny)) * grid.step
        self.lon = grid.lon0 + (self.lo0 + np.arange(self.nx)) * grid.step
        self.cell_lat = grid.points.latitude.values.astype(float)
        self.cell_lon = grid.points.longitude.values.astype(float)
        b = grid.bbox
        self.bbox = (b[0] - 0.05, b[1] - 0.05, b[2] + 0.05, b[3] + 0.05)

    def rows_of(self, lat, lon):
        iy = np.rint((np.asarray(lat) - self.grid.lat0) / self.grid.step).astype(np.int64) - self.la0
        ix = np.rint((np.asarray(lon) - self.grid.lon0) / self.grid.step).astype(np.int64) - self.lo0
        ok = (iy >= 0) & (iy < self.ny) & (ix >= 0) & (ix < self.nx)
        r = np.full(iy.shape, -1, dtype=np.int64)
        r[ok] = self.rowmap[iy[ok], ix[ok]]
        return r

    def bin(self, lat, lon, val):
        """Average fine pixels into cells -> (sum, count) per study cell."""
        lat, lon, val = (np.asarray(a, dtype=np.float64).ravel() for a in (lat, lon, val))
        r = self.rows_of(lat, lon)
        ok = (r >= 0) & np.isfinite(val)
        s = np.bincount(r[ok], weights=val[ok], minlength=self.n)
        c = np.bincount(r[ok], minlength=self.n).astype(np.float64)
        return s, c

    def sample(self, field, lat1d, lon1d):
        """Nearest coarse pixel at each cell centre (field (nlat, nlon) on 1-D lat/lon axes)."""
        lat1d, lon1d = np.asarray(lat1d, float), np.asarray(lon1d, float)
        iy = np.abs(lat1d[None, :] - self.cell_lat[:, None]).argmin(axis=1)
        ix = np.abs(lon1d[None, :] - self.cell_lon[:, None]).argmin(axis=1)
        return np.asarray(field, dtype=np.float64)[iy, ix]


def sinusoidal_to_latlon(x, y):
    lat = np.degrees(y / SIN_R)
    with np.errstate(divide="ignore", invalid="ignore"):
        lon = np.degrees(x / (SIN_R * np.cos(np.radians(lat))))
    return lat, lon


def latlon_to_sinusoidal(lat, lon):
    return SIN_R * np.radians(lon) * np.cos(np.radians(lat)), SIN_R * np.radians(lat)


def modis_qa_ndvi(ndvi_raw, reliability):
    """MOD13 NDVI int16 (scale 1e-4, fill -3000) + pixel reliability -> float NDVI with bad pixels NaN."""
    nd = np.asarray(ndvi_raw, dtype=np.float64)
    ok = (nd >= -2000) & (nd <= 10000)
    if reliability is not None:
        rel = np.asarray(reliability)
        ok &= (rel == 0) | (rel == 1)
    return np.where(ok, nd * SCALE, np.nan)


# ---------------------------------------------------------------------------
# accumulation of composites and daily interpolation
# ---------------------------------------------------------------------------

class YearAccumulator:
    def __init__(self, cells: CellGrid, year: int, product: str):
        self.cells, self.year, self.product = cells, year, product
        self.sum: dict = {}
        self.cnt: dict = {}
        self.sources: list = []

    def add_fine(self, start, lat, lon, ndvi, source=""):
        start = pd.Timestamp(start).normalize()
        s, c = self.cells.bin(lat, lon, ndvi)
        if start not in self.sum:
            self.sum[start], self.cnt[start] = np.zeros(self.cells.n), np.zeros(self.cells.n)
        self.sum[start] += s
        self.cnt[start] += c
        if source:
            self.sources.append(source)

    def add_coarse(self, start, field, lat1d, lon1d, source=""):
        start = pd.Timestamp(start).normalize()
        v = self.cells.sample(field, lat1d, lon1d)
        ok = np.isfinite(v)
        if start not in self.sum:
            self.sum[start], self.cnt[start] = np.zeros(self.cells.n), np.zeros(self.cells.n)
        self.sum[start][ok] += v[ok]
        self.cnt[start][ok] += 1
        if source:
            self.sources.append(source)

    def composites(self):
        starts = sorted(self.sum)
        with np.errstate(invalid="ignore", divide="ignore"):
            vals = np.array([np.where(self.cnt[s] > 0, self.sum[s] / self.cnt[s], np.nan) for s in starts])
        return starts, vals                            # (n_comp, n_cells)

    def daily(self, starts=None, vals=None):
        """Linear interpolation between composite mid-dates (start + 8 d), per cell, no more than MAX_GAP_DAYS."""
        if starts is None:
            starts, vals = self.composites()
        days = pd.date_range(f"{self.year}-01-01", f"{self.year}-12-31", freq="D")
        out = np.full((len(days), self.cells.n), np.nan, dtype=np.float32)
        if not starts:
            return days, out
        mid = np.array([(pd.Timestamp(s) + pd.Timedelta(days=8) - days[0]).days for s in starts], dtype=float)
        dd = np.arange(len(days), dtype=float)
        for c in range(self.cells.n):
            col = vals[:, c]
            ok = np.isfinite(col)
            if ok.sum() == 0:
                continue
            xm, ym = mid[ok], col[ok]
            if ok.sum() == 1:
                near = np.abs(dd - xm[0]) <= 8
                out[near, c] = ym[0]
                continue
            v = np.interp(dd, xm, ym, left=np.nan, right=np.nan)
            # gaps: distance to the nearest valid composite mid-date on each side
            j = np.searchsorted(xm, dd)
            lo = np.where(j > 0, xm[np.maximum(j - 1, 0)], -1e9)
            hi = np.where(j < len(xm), xm[np.minimum(j, len(xm) - 1)], 1e9)
            v[(hi - lo) > MAX_GAP_DAYS] = np.nan
            at = np.minimum(j, len(xm) - 1)                 # a composite's own mid-date is always its value
            hit = xm[at] == dd
            v[hit] = ym[at[hit]]
            edge = (dd < xm[0]) & (xm[0] - dd <= 8)
            v[edge] = ym[0]
            edge = (dd > xm[-1]) & (dd - xm[-1] <= 8)
            v[edge] = ym[-1]
            out[:, c] = v
        return days, out


def write_year(static_dir: Path, acc: YearAccumulator, backend: str) -> Path:
    import netCDF4
    cells = acc.cells
    starts, vals = acc.composites()
    days, daily = acc.daily(starts, vals)
    d = Path(static_dir) / "ndvi" / PRODUCT_DIR[acc.product]
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"FPI_Input_{acc.year}.nc"
    tmp = path.with_suffix(".tmp")

    def grid3(a):
        g = np.full((a.shape[0], cells.ny, cells.nx), FILL, dtype=np.int16)
        q = np.where(np.isfinite(a), np.clip(np.round(a / SCALE), -32767, 32767), FILL).astype(np.int16)
        g[:, cells.y, cells.x] = q
        return g

    with netCDF4.Dataset(tmp, "w", format="NETCDF4") as ds:
        ds.createDimension("time", len(days))
        ds.createDimension("composite", len(starts))
        ds.createDimension("latitude", cells.ny)
        ds.createDimension("longitude", cells.nx)
        t = ds.createVariable("time", "i4", ("time",)); t.units = f"days since {acc.year}-01-01"; t.calendar = "standard"
        t[:] = np.arange(len(days))
        ct = ds.createVariable("composite", "i4", ("composite",)); ct.units = f"days since {acc.year}-01-01"
        ct.long_name = "16-day composite start date"
        ct[:] = [(pd.Timestamp(s) - pd.Timestamp(acc.year, 1, 1)).days for s in starts]
        la = ds.createVariable("latitude", "f8", ("latitude",)); la.units = "degrees_north"; la[:] = cells.lat
        lo = ds.createVariable("longitude", "f8", ("longitude",)); lo.units = "degrees_east"; lo[:] = cells.lon
        for name, arr, dims, ln, chunk in (("ND0", daily, "time", "NDVI, daily (linear between 16-day composite mid-dates)", 31),
                                           ("ND_composite", vals, "composite", "NDVI, 16-day composite averaged into the cell", 23)):
            v = ds.createVariable(name, "i2", (dims, "latitude", "longitude"), fill_value=FILL,
                                  chunksizes=(min(chunk, max(arr.shape[0], 1)), min(64, cells.ny), min(64, cells.nx)), **COMP)
            v.scale_factor, v.add_offset = np.float32(SCALE), np.float32(0)
            v.long_name, v.units = ln, "1"
            v.set_auto_maskandscale(False)
            v[:] = grid3(np.asarray(arr))
        ds.product, ds.backend, ds.year = acc.product, backend, int(acc.year)
        ds.qa = "MODIS pixel reliability 0/1 kept, 2/3 dropped" if acc.product.startswith("MOD") else "CDR QA cloud/shadow flags dropped"
        ds.regridding = ("pixels finer than the cell averaged into the cell holding their centre; coarser pixels sampled "
                         "at the cell centre (AORC 1/120 deg grid)")
        ds.sources_json = json.dumps(sorted(set(acc.sources))[:400])
        ds.history = f"{datetime.now().isoformat(timespec='seconds')} written by WUP Grid Explorer NDVI ingest"
    os.replace(tmp, path)
    return path


def build_static(static_dir: Path, cells: CellGrid, fuel_models, products=("MOD13Q1", "MOD13A1", "MOD13A2"),
                 robust: bool = False) -> dict:
    """ND_min / ND_max per cell from every ingested composite (first product that has data for the cell),
    plus Burgan loadings from the fuel model per cell -> FPI_Input_Static.nc."""
    import netCDF4
    from aorc_tools.explorer.ndvi import fuel_loadings
    static_dir = Path(static_dir)
    lo = np.full(cells.n, np.nan)
    hi = np.full(cells.n, np.nan)
    used, years = {}, set()
    for p in products:
        files = sorted((static_dir / "ndvi" / PRODUCT_DIR[p]).glob("FPI_Input_*.nc"))
        if not files:
            continue
        vals = []
        for f in files:
            with netCDF4.Dataset(f) as ds:
                v = ds["ND_composite"]; v.set_auto_maskandscale(False)
                a = np.asarray(v[:])[:, cells.y, cells.x].astype(np.float64)
                vals.append(np.where(a == FILL, np.nan, a * SCALE))
                years.add(int(ds.year))
        allv = np.concatenate(vals, axis=0)
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                pl = np.nanpercentile(allv, 1, axis=0) if robust else np.nanmin(allv, axis=0)
                ph = np.nanpercentile(allv, 99, axis=0) if robust else np.nanmax(allv, axis=0)
        take = ~np.isfinite(lo) & np.isfinite(pl)
        lo[take], hi[take] = pl[take], ph[take]
        used[p] = int(take.sum())
    load = fuel_loadings(np.asarray(fuel_models, dtype=object))
    path = static_dir / "FPI_Input_Static.nc"
    static_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with netCDF4.Dataset(tmp, "w", format="NETCDF4") as ds:
        ds.createDimension("latitude", cells.ny)
        ds.createDimension("longitude", cells.nx)
        la = ds.createVariable("latitude", "f8", ("latitude",)); la.units = "degrees_north"; la[:] = cells.lat
        lo_ = ds.createVariable("longitude", "f8", ("longitude",)); lo_.units = "degrees_east"; lo_[:] = cells.lon
        for name, arr, ln in (("ND_min", lo, "minimum NDVI of the archive"), ("ND_max", hi, "maximum NDVI of the archive"),
                              ("LLfm", load["llfm"], "live fuel load (Burgan, from the cell's NFDRS model)"),
                              ("DLfm", load["dlfm"], "dead fuel load"), ("MXd", load["mxd"], "dead-fuel moisture of extinction")):
            g = np.full((cells.ny, cells.nx), np.nan, dtype=np.float32)
            g[cells.y, cells.x] = arr
            v = ds.createVariable(name, "f4", ("latitude", "longitude"), fill_value=np.float32(np.nan), **COMP)
            v.long_name = ln
            v[:] = g
        ds.envelope = "1st/99th percentile" if robust else "minimum/maximum"
        ds.years = json.dumps(sorted(years))
        ds.history = f"{datetime.now().isoformat(timespec='seconds')} written by WUP Grid Explorer NDVI ingest"
    os.replace(tmp, path)
    clim = build_doy_climatology(static_dir, cells, products)
    return {"path": str(path), "cells_by_product": used, "years": sorted(years),
            "cells_without_envelope": int((~np.isfinite(lo)).sum()), "doy_climatology": clim}


def build_doy_climatology(static_dir: Path, cells: CellGrid, products=("MOD13Q1", "MOD13A1", "MOD13A2")):
    """Mean daily NDVI per day of year over every ingested MODIS year -> ndvi/ndvi_doy_climatology.nc.
    Used only when non-MODIS NDVI is opted in: before 2000 and for future scenarios (the Argonne study
    held satellite NDVI fixed for its future FPI for the same reason)."""
    import netCDF4
    static_dir = Path(static_dir)
    s = np.zeros((366, cells.n))
    c = np.zeros((366, cells.n))
    years = set()
    for p in products:
        for f in sorted((static_dir / "ndvi" / PRODUCT_DIR[p]).glob("FPI_Input_*.nc")):
            with netCDF4.Dataset(f) as ds:
                v = ds["ND0"]; v.set_auto_maskandscale(False)
                a = np.asarray(v[:])[:, cells.y, cells.x].astype(np.float64)
                a = np.where(a == FILL, np.nan, a * SCALE)
                yr = int(ds.year)
            doy = np.arange(a.shape[0])
            ok = np.isfinite(a)
            np.add.at(s, doy, np.where(ok, a, 0.0))
            np.add.at(c, doy, ok.astype(float))
            years.add(yr)
        if years:
            break
    if not years:
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        m = np.where(c > 0, s / c, np.nan)
    m[365] = np.where(np.isfinite(m[365]), m[365], m[364])          # day 366 from day 365 when no leap years
    out = static_dir / "ndvi" / "ndvi_doy_climatology.nc"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with netCDF4.Dataset(tmp, "w", format="NETCDF4") as ds:
        ds.createDimension("doy", 366)
        ds.createDimension("latitude", cells.ny)
        ds.createDimension("longitude", cells.nx)
        d = ds.createVariable("doy", "i2", ("doy",)); d[:] = np.arange(1, 367)
        la = ds.createVariable("latitude", "f8", ("latitude",)); la.units = "degrees_north"; la[:] = cells.lat
        lo = ds.createVariable("longitude", "f8", ("longitude",)); lo.units = "degrees_east"; lo[:] = cells.lon
        g = np.full((366, cells.ny, cells.nx), FILL, dtype=np.int16)
        g[:, cells.y, cells.x] = np.where(np.isfinite(m), np.round(m / SCALE), FILL).astype(np.int16)
        v = ds.createVariable("ND_clim", "i2", ("doy", "latitude", "longitude"), fill_value=FILL,
                              chunksizes=(31, min(64, cells.ny), min(64, cells.nx)), **COMP)
        v.scale_factor, v.add_offset = np.float32(SCALE), np.float32(0)
        v.set_auto_maskandscale(False)
        v[:] = g
        v.long_name = "mean daily NDVI for the day of year"
        ds.years = json.dumps(sorted(years))
    os.replace(tmp, out)
    return {"path": str(out), "years": sorted(years)}


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

def _read_window(href, bbox_latlon, sinusoidal=True):
    """Read the part of a raster covering the WUP; -> (array, lat, lon) of pixel centres."""
    import rasterio
    from rasterio.windows import from_bounds
    lon0, lat0, lon1, lat1 = bbox_latlon
    with rasterio.open(href) as src:
        if sinusoidal:
            lats = np.array([lat0, lat0, lat1, lat1, (lat0 + lat1) / 2, (lat0 + lat1) / 2])
            lons = np.array([lon0, lon1, lon0, lon1, lon0, lon1])
            xs, ys = latlon_to_sinusoidal(lats, lons)
        else:
            from rasterio.warp import transform as wtransform
            xs, ys = wtransform("EPSG:4326", src.crs, [lon0, lon1, lon0, lon1], [lat0, lat0, lat1, lat1])
        b = src.bounds
        xmin, xmax = max(min(xs), b.left), min(max(xs), b.right)
        ymin, ymax = max(min(ys), b.bottom), min(max(ys), b.top)
        if xmin >= xmax or ymin >= ymax:
            return None
        win = from_bounds(xmin, ymin, xmax, ymax, src.transform).round_offsets().round_lengths()
        a = src.read(1, window=win)
        tr = src.window_transform(win)
        jj, ii = np.meshgrid(np.arange(a.shape[1]) + 0.5, np.arange(a.shape[0]) + 0.5)
        x = tr.c + jj * tr.a + ii * tr.b
        y = tr.f + jj * tr.d + ii * tr.e
        if sinusoidal:
            lat, lon = sinusoidal_to_latlon(x, y)
        else:
            from rasterio.warp import transform as wtransform
            lo_, la_ = wtransform(src.crs, "EPSG:4326", x.ravel().tolist(), y.ravel().tolist())
            lat, lon = np.asarray(la_).reshape(x.shape), np.asarray(lo_).reshape(x.shape)
        return a, lat, lon


class PlanetaryComputerMODIS:
    """MODIS 16-day NDVI COGs via the Planetary Computer STAC API (anonymous; URLs are signed on the fly)."""

    STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
    COLLECTION = {"MOD13Q1": "modis-13Q1-061", "MOD13A1": "modis-13A1-061"}

    def __init__(self, product="MOD13Q1", items_fn=None):
        if product not in self.COLLECTION:
            raise ValueError(f"Planetary Computer serves {list(self.COLLECTION)}; use AppEEARS for {product}")
        self.product = product
        self._items_fn = items_fn

    def items(self, year, bbox):
        if self._items_fn is not None:
            return self._items_fn(year, bbox)
        import planetary_computer
        import pystac_client
        cat = pystac_client.Client.open(self.STAC, modifier=planetary_computer.sign_inplace)
        search = cat.search(collections=[self.COLLECTION[self.product]], bbox=list(bbox),
                            datetime=f"{year}-01-01/{year}-12-31")
        return [it for it in search.items()
                if str(it.properties.get("platform", "terra")).lower() == "terra" or it.id.upper().startswith("MOD")]

    @staticmethod
    def _asset(item, *must):
        for k, a in item.assets.items():
            if all(m.lower() in k.lower() for m in must):
                return a.href
        return None

    def ingest_year(self, acc: YearAccumulator, progress=None):
        items = self.items(acc.year, acc.cells.bbox)
        if not items:
            raise RuntimeError(f"no {self.product} items for {acc.year} over the WUP")
        for i, it in enumerate(items):
            nd_href = self._asset(it, "16_days_ndvi")
            rel_href = self._asset(it, "pixel_reliability")
            if nd_href is None:
                continue
            t = pd.Timestamp(it.properties.get("start_datetime") or it.datetime)
            start = t.tz_convert(None) if t.tzinfo else t
            got = _read_window(nd_href, acc.cells.bbox, sinusoidal=True)
            if got is None:
                continue
            nd, lat, lon = got
            rel = _read_window(rel_href, acc.cells.bbox, sinusoidal=True)[0] if rel_href else None
            acc.add_fine(start, lat, lon, modis_qa_ndvi(nd, rel), source=getattr(it, "id", ""))
            if progress:
                progress(i + 1, len(items), f"{acc.year} {getattr(it, 'id', '')}")


class AppEEARSMODIS:
    """NASA AppEEARS area request for MODIS NDVI + pixel reliability over the WUP (NetCDF, geographic)."""

    API = "https://appeears.earthdatacloud.nasa.gov/api"
    LAYERS = {"MOD13Q1": ("MOD13Q1.061", "_250m_16_days_NDVI", "_250m_16_days_pixel_reliability"),
              "MOD13A1": ("MOD13A1.061", "_500m_16_days_NDVI", "_500m_16_days_pixel_reliability"),
              "MOD13A2": ("MOD13A2.061", "_1_km_16_days_NDVI", "_1_km_16_days_pixel_reliability")}

    def __init__(self, product="MOD13Q1", username=None, password=None, download_dir=None, session=None):
        self.product = product
        self.username = username or os.environ.get("EARTHDATA_USERNAME")
        self.password = password or os.environ.get("EARTHDATA_PASSWORD")
        self.download_dir = Path(download_dir or ".")
        self._session = session

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _token(self):
        if not (self.username and self.password):
            raise RuntimeError("AppEEARS needs a NASA Earthdata login: set EARTHDATA_USERNAME and EARTHDATA_PASSWORD "
                               "(free account at urs.earthdata.nasa.gov)")
        r = self.session.post(f"{self.API}/login", auth=(self.username, self.password), timeout=60)
        r.raise_for_status()
        return r.json()["token"]

    def request_year(self, year, bbox, poll_s=30, timeout_s=6 * 3600):
        prod, nd, rel = self.LAYERS[self.product]
        lon0, lat0, lon1, lat1 = bbox
        geo = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}}]}
        task = {"task_type": "area", "task_name": f"WUP_{self.product}_{year}",
                "params": {"dates": [{"startDate": f"01-01-{year}", "endDate": f"12-31-{year}"}],
                           "layers": [{"product": prod, "layer": nd}, {"product": prod, "layer": rel}],
                           "geo": geo, "output": {"format": {"type": "netcdf4"}, "projection": "geographic"}}}
        hdr = {"Authorization": f"Bearer {self._token()}"}
        r = self.session.post(f"{self.API}/task", json=task, headers=hdr, timeout=60)
        r.raise_for_status()
        tid = r.json()["task_id"]
        t0 = _time.time()
        while True:
            s = self.session.get(f"{self.API}/task/{tid}", headers=hdr, timeout=60).json()
            if s.get("status") == "done":
                break
            if s.get("status") in ("error", "expired"):
                raise RuntimeError(f"AppEEARS task {tid} {s.get('status')}")
            if _time.time() - t0 > timeout_s:
                raise TimeoutError(f"AppEEARS task {tid} still {s.get('status')} after {timeout_s} s")
            _time.sleep(poll_s)
        files = self.session.get(f"{self.API}/bundle/{tid}", headers=hdr, timeout=60).json()["files"]
        out = []
        self.download_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if not f["file_name"].endswith(".nc"):
                continue
            dst = self.download_dir / Path(f["file_name"]).name
            with self.session.get(f"{self.API}/bundle/{tid}/{f['file_id']}", headers=hdr, stream=True, timeout=600) as resp:
                resp.raise_for_status()
                with open(dst, "wb") as fh:
                    for chunk in resp.iter_content(1 << 20):
                        fh.write(chunk)
            out.append(dst)
        return out

    def ingest_netcdf(self, acc: YearAccumulator, path):
        """Process one AppEEARS NetCDF (variables named after the layers, dims time/lat/lon)."""
        import xarray as xr
        _, nd, rel = self.LAYERS[self.product]
        with xr.open_dataset(path, mask_and_scale=False, decode_times=True) as ds:
            ndv = next(v for v in ds.data_vars if v.endswith(nd.lstrip("_")) or v == nd)
            relv = next((v for v in ds.data_vars if v.endswith(rel.lstrip("_")) or v == rel), None)
            lat, lon = ds["lat"].values, ds["lon"].values
            LON, LAT = np.meshgrid(lon, lat)
            for ti, t in enumerate(pd.DatetimeIndex(ds["time"].values)):
                a = ds[ndv].isel(time=ti).values
                r = ds[relv].isel(time=ti).values if relv else None
                vals = modis_qa_ndvi(a, r)
                if self.product == "MOD13A2":
                    acc.add_coarse(t, vals, lat, lon, source=Path(path).name)
                else:
                    acc.add_fine(t, LAT, LON, vals, source=Path(path).name)

    def ingest_year(self, acc: YearAccumulator, progress=None, files=None):
        files = files or self.request_year(acc.year, acc.cells.bbox)
        for i, f in enumerate(files):
            self.ingest_netcdf(acc, f)
            if progress:
                progress(i + 1, len(files), Path(f).name)


class NOAACDR:
    """NOAA CDR AVHRR/VIIRS NDVI v5 (0.05 deg daily) -> 16-day maximum-value composites aligned with MODIS."""

    BUCKET = "noaa-cdr-ndvi-pds"

    def __init__(self, root=None, fs=None):
        self.root = root or f"s3://{self.BUCKET}/data"
        self._fs = fs

    @property
    def fs(self):
        if self._fs is None:
            import fsspec
            self._fs = fsspec.filesystem("s3", anon=True) if str(self.root).startswith("s3://") else fsspec.filesystem("file")
        return self._fs

    def files(self, year):
        base = f"{self.root}/{year}"
        try:
            names = self.fs.ls(base)
        except FileNotFoundError:
            return {}
        out = {}
        for n in names:
            m = re.search(r"_(\d{8})_", Path(n).name)
            if m and n.endswith(".nc"):
                out[pd.Timestamp(m.group(1))] = n if "://" in n or not str(self.root).startswith("s3://") else f"s3://{n}"
        return out

    def ingest_year(self, acc: YearAccumulator, progress=None):
        import h5netcdf
        files = self.files(acc.year)
        if not files:
            raise RuntimeError(f"no CDR NDVI files for {acc.year}")
        lon0, lat0, lon1, lat1 = acc.cells.bbox
        starts = composite_starts(acc.year)
        best = {s: None for s in starts}
        lat_ax = lon_ax = None
        for i, (day, path) in enumerate(sorted(files.items())):
            s = max([c for c in starts if c <= day], default=None)
            if s is None:
                continue
            with self.fs.open(path, "rb") as fh, h5netcdf.File(fh, "r") as ds:
                lat, lon = np.asarray(ds["latitude"][:]), np.asarray(ds["longitude"][:])
                iy = np.where((lat >= lat0) & (lat <= lat1))[0]
                ix = np.where((lon >= lon0) & (lon <= lon1))[0]
                if not len(iy) or not len(ix):
                    continue
                nd = ds["NDVI"]
                a = np.asarray(nd[0, iy.min():iy.max() + 1, ix.min():ix.max() + 1] if nd.ndim == 3 else
                               nd[iy.min():iy.max() + 1, ix.min():ix.max() + 1]).astype(np.float64)
                sf = float(nd.attrs.get("scale_factor", 1e-4)); fv = nd.attrs.get("_FillValue", -9999)
                a = np.where(a == fv, np.nan, a * sf)
                if "QA" in ds.variables:
                    qa = np.asarray(ds["QA"][0, iy.min():iy.max() + 1, ix.min():ix.max() + 1] if ds["QA"].ndim == 3 else
                                    ds["QA"][iy.min():iy.max() + 1, ix.min():ix.max() + 1]).astype(np.int64)
                    cloudy = (qa & 0b10) > 0                        # bit 1: cloudy
                    shadow = (qa & 0b100) > 0                       # bit 2: cloud shadow
                    a = np.where(cloudy | shadow, np.nan, a)
                a = np.where((a < -0.2) | (a > 1.0), np.nan, a)
                lat_ax, lon_ax = lat[iy.min():iy.max() + 1], lon[ix.min():ix.max() + 1]
            best[s] = a if best[s] is None else np.fmax(best[s], a)
            if progress:
                progress(i + 1, len(files), str(day.date()))
        for s, a in best.items():
            if a is not None:
                acc.add_coarse(s, a, lat_ax, lon_ax, source=f"CDR {s.date()}")


class LocalRasters:
    """NDVI rasters you already have: GeoTIFF (any CRS) or NetCDF, one composite per file, date in the name
    (MODIS 'A2009145', '2009145', '20090525' or '2009-05-25'). Values > 1.5 are taken as scaled by 1e-4."""

    DATE_PATTERNS = [(r"A(\d{4})(\d{3})", "doy"), (r"(\d{4})-(\d{2})-(\d{2})", "ymd"), (r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", "ymd"),
                     (r"(?<!\d)(\d{4})(\d{3})(?!\d)", "doy")]

    def __init__(self, folder, product="MOD13Q1"):
        self.folder = Path(folder)
        self.product = product

    @classmethod
    def date_of(cls, name):
        for pat, kind in cls.DATE_PATTERNS:
            m = re.search(pat, name)
            if m:
                g = list(map(int, m.groups()))
                try:
                    return pd.Timestamp(g[0], 1, 1) + pd.Timedelta(days=g[1] - 1) if kind == "doy" else pd.Timestamp(*g)
                except ValueError:
                    continue
        return None

    def files(self, year):
        out = []
        for f in sorted(self.folder.rglob("*")):
            if f.suffix.lower() in (".tif", ".tiff", ".nc") and "reliab" not in f.name.lower() and "qa" not in f.name.lower():
                d = self.date_of(f.name)
                if d is not None and d.year == year:
                    out.append((d, f))
        return out

    def ingest_year(self, acc: YearAccumulator, progress=None):
        fl = self.files(acc.year)
        if not fl:
            raise RuntimeError(f"no NDVI rasters for {acc.year} in {self.folder}")
        for i, (d, f) in enumerate(fl):
            if f.suffix.lower() == ".nc":
                import xarray as xr
                with xr.open_dataset(f) as ds:
                    v = next(n for n in ds.data_vars if "ndvi" in n.lower())
                    a = np.squeeze(ds[v].values).astype(np.float64)
                    lat = ds[[c for c in ds.coords if c.lower().startswith("lat")][0]].values
                    lon = ds[[c for c in ds.coords if c.lower().startswith("lon")][0]].values
                    LON, LAT = np.meshgrid(lon, lat)
            else:
                import rasterio
                with rasterio.open(f) as src:
                    sinus = "sinusoidal" in (src.crs.to_wkt().lower() if src.crs else "")
                got = _read_window(str(f), acc.cells.bbox, sinusoidal=sinus)
                if got is None:
                    continue
                a, LAT, LON = got
                a = a.astype(np.float64)
            if np.nanmax(np.abs(a)) > 1.5:
                a = np.where((a >= -2000) & (a <= 10000), a * SCALE, np.nan)
            pix_deg = float(np.nanmedian(np.abs(np.diff(LAT[:, 0])))) if LAT.shape[0] > 1 else 0.01
            if pix_deg > 0.012:                       # coarser than ~1.3 km -> sample
                acc.add_coarse(d, a, LAT[:, 0], LON[0, :], source=f.name)
            else:
                acc.add_fine(d, LAT, LON, a, source=f.name)
            if progress:
                progress(i + 1, len(fl), f.name)


def make_backend(name, product="MOD13Q1", **kw):
    if name == "planetary":
        return PlanetaryComputerMODIS(product, items_fn=kw.get("items_fn"))
    if name == "appeears":
        return AppEEARSMODIS(product, kw.get("username"), kw.get("password"), kw.get("download_dir"))
    if name == "cdr":
        return NOAACDR(kw.get("root"))
    if name == "local":
        return LocalRasters(kw["folder"], product)
    raise ValueError(f"unknown NDVI backend {name}")


def run_ingest(grid, static_dir, backend_name, product, years, fuel_models, progress=None, build_envelope=True, **kw):
    """Ingest years with one backend, then rebuild the static envelope. Returns a summary."""
    cells = CellGrid(grid)
    prod = "CDR" if backend_name == "cdr" else product
    be = make_backend(backend_name, product, **kw)
    written, errors = [], []
    n = len(years)
    for yi, year in enumerate(years):
        acc = YearAccumulator(cells, int(year), prod)
        try:
            be.ingest_year(acc, progress=(lambda d, t, note, yi=yi: progress(yi + d / max(t, 1), n, note)) if progress else None)
            starts, vals = acc.composites()
            p = write_year(static_dir, acc, backend_name)
            frac = float(np.isfinite(vals).mean()) if len(starts) else 0.0
            written.append({"year": int(year), "path": str(p), "composites": len(starts), "cell_coverage": round(frac, 4)})
        except Exception as e:
            logger.exception("NDVI %s %s failed", backend_name, year)
            errors.append(f"{year}: {type(e).__name__}: {e}"[:300])
        if progress:
            progress(yi + 1, n, f"{year} done")
    env = build_static(static_dir, cells, fuel_models) if build_envelope and written else None
    return {"backend": backend_name, "product": prod, "written": written, "errors": errors, "envelope": env,
            "finished": now_iso()}
