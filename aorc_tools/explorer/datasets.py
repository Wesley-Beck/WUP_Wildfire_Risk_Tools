"""
Climate datasets other than AORC as explorer sources: regional climate model output
(GLARM / RegCM4, WRF), reanalyses (ERA5), CF/CORDEX-style files -- local files
(offline) or URLs (OPeNDAP, HTTPS, s3://, Zarr) (online).

A dataset is described by a small JSON config kept in the workspace
(``datasets/<slug>.json``), e.g. for one GLARM projection:

    {"name": "GLARM IPSL RCP8.5 2080-2099", "preset": "regcm",
     "paths": ["D:/glarm/ipsl_rcp85/*SRF*.nc"],
     "scenario": "RCP8.5", "gcm": "IPSL-CM5A-MR", "notes": "..."}

It then behaves like AORC everywhere in the tool: same seven variables per packet
(K, kg/kg, Pa, m/s, mm per hour, W/m2), same bricks and 144-h chunks, the same
resolution ladder, same chain, results stored separately under its own source name
(``ds-<slug>``), so historical (AORC) and future (dataset) results never mix.

What the adapter does:
  * variables  -- preset name lists (overridable per dataset), unit conversion from the
                  ``units`` attribute; humidity from q, mixing ratio, RH or dew point; precipitation
                  from rates, per-step amounts or run-accumulated totals (WRF)
  * space      -- regular lat/lon axes: bilinear; 2-D (curvilinear) lat/lon: inverse-distance of
                  the 4 nearest model points. A 18 km model gives an 18 km field sampled at 800 m
                  cells (recorded in the notes; it adds no detail).
  * time       -- sub-daily steps interpolated linearly to hourly (states); precipitation spread
                  evenly over the hours of its accumulation period; non-standard calendars
                  (noleap / 360-day) mapped to real dates, impossible dates dropped.
Bias correction against the AORC baseline is deliberately NOT done here (see
docs/future_glarm_plan.md): raw model fields first, correction as an explicit, reported step.
"""

from __future__ import annotations

import glob
import json
import logging
import math
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.grid import CHUNK_TIME, Brick, Grid
from aorc_tools.explorer.sources import VARIABLES, BaseSource
from aorc_tools.explorer.workspace import now_iso

logger = logging.getLogger(__name__)

# target variable -> candidate source variable names (first found wins)
PRESETS = {
    "regcm": {   # GLARM / RegCM4 SRF output, CORDEX names
        "TMP_2maboveground": ["tas", "t2m", "tas_2m"],
        "SPFH_2maboveground": ["qas", "huss", "hurs"],
        "PRES_surface": ["ps", "psurf", "psl"],
        "UGRD_10maboveground": ["uas", "u10"],
        "VGRD_10maboveground": ["vas", "v10"],
        "_wind_speed": ["sfcWind", "wind"],
        "APCP_surface": ["pr", "prcp", "tp"],
        "DSWRF_surface": ["rsds", "rsns", "swdown"],
    },
    "cf": {      # CF / CMIP / CORDEX standard names
        "TMP_2maboveground": ["tas"],
        "SPFH_2maboveground": ["huss", "hurs"],
        "PRES_surface": ["ps", "psl"],
        "UGRD_10maboveground": ["uas"],
        "VGRD_10maboveground": ["vas"],
        "_wind_speed": ["sfcWind"],
        "APCP_surface": ["pr"],
        "DSWRF_surface": ["rsds"],
    },
    "wrf": {
        "TMP_2maboveground": ["T2"],
        "SPFH_2maboveground": ["Q2"],                    # water-vapour MIXING RATIO
        "PRES_surface": ["PSFC"],
        "UGRD_10maboveground": ["U10"],
        "VGRD_10maboveground": ["V10"],
        "APCP_surface": ["RAINNC+RAINC", "RAINNC"],      # accumulated since model start
        "DSWRF_surface": ["SWDOWN"],
    },
    "era5": {
        "TMP_2maboveground": ["t2m", "2t"],
        "SPFH_2maboveground": ["d2m", "2d"],             # dew point -> q
        "PRES_surface": ["sp"],
        "UGRD_10maboveground": ["u10", "10u"],
        "VGRD_10maboveground": ["v10", "10v"],
        "APCP_surface": ["tp"],                          # m per hour
        "DSWRF_surface": ["ssrd"],                       # J m-2 per hour
    },
    "aorc": {v: [v] for v in VARIABLES},
}
PRESET_HINTS = {
    "regcm": "GLARM (RegCM4 + FVCOM, Xue et al. 2022) and other RegCM/CORDEX output: tas, qas/huss, ps, uas/vas, pr, rsds",
    "cf": "CF/CMIP/CORDEX names: tas, huss/hurs, ps, uas/vas or sfcWind, pr, rsds",
    "wrf": "WRF wrfout: T2, Q2 (mixing ratio), PSFC, U10/V10 (rotated with COSALPHA/SINALPHA if present), RAINNC+RAINC (accumulated), SWDOWN",
    "era5": "ERA5 single levels: t2m, d2m, sp, u10, v10, tp, ssrd",
    "aorc": "AORC variable names (round-trip tests, AORC-like exports)",
}
TIME_NAMES = ["time", "Time", "XTIME", "valid_time"]
LAT_NAMES = ["lat", "latitude", "XLAT", "xlat", "nav_lat"]
LON_NAMES = ["lon", "longitude", "XLONG", "xlon", "nav_lon"]


def _open_any(path):
    """Local NetCDF/GRIB-free files, Zarr stores, OPeNDAP endpoints, or plain HTTPS / s3:// NetCDF objects."""
    import xarray as xr
    remote = bool(re.match(r"^[a-z0-9+]+://", path))
    if path.rstrip("/").endswith(".zarr"):
        so = {"anon": True} if path.startswith("s3://") else None
        return xr.open_zarr(path, decode_times=False, storage_options=so)
    if not remote or re.search(r"dodsC|opendap|/dap[0-9]?/|thredds", path, re.I):
        return xr.open_dataset(path, decode_times=False)           # local file or OPeNDAP (netCDF4 engine)
    try:                                                          # netCDF-C byte-range reads over HTTPS
        if path.startswith("http"):
            return xr.open_dataset(path + ("" if "#mode=bytes" in path else "#mode=bytes"), decode_times=False)
    except Exception:
        pass
    import fsspec                                                 # s3:// or HTTPS object through fsspec + h5netcdf
    fs_kw = {"anon": True} if path.startswith("s3://") else {}
    fobj = fsspec.open(path, "rb", **fs_kw).open()
    return xr.open_dataset(fobj, engine="h5netcdf", decode_times=False)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:48] or "dataset"


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------

def _units(da):
    return str(da.attrs.get("units", "")).strip().lower().replace("**", "").replace("^", "")


def to_kelvin(a, units):
    return a + 273.15 if units in ("c", "degc", "deg_c", "celsius", "°c") else a


def to_pa(a, units):
    if units in ("hpa", "mb", "mbar"):
        return a * 100.0
    if units == "kpa":
        return a * 1000.0
    return a


def q_from(kind, a, units, t_k, p_pa):
    """specific humidity (kg/kg) from q, mixing ratio, RH (%) or dew point."""
    if kind == "mixing_ratio":
        return a / (1.0 + a)
    if kind == "rh":
        rh = a * 100.0 if np.nanmax(a) <= 1.5 else a
        tc = t_k - 273.15
        e = rh / 100.0 * 611.2 * np.exp(17.67 * tc / (tc + 243.5))
        return 0.622 * e / (p_pa - 0.378 * e)
    if kind == "dewpoint":
        td = to_kelvin(a, units) - 273.15
        e = 611.2 * np.exp(17.67 * td / (td + 243.5))
        return 0.622 * e / (p_pa - 0.378 * e)
    return a / 1000.0 if units in ("g/kg", "g kg-1") else a


def precip_mm_per_step(a, units, step_h):
    """Convert a precipitation field to mm accumulated over each source step."""
    u = units.replace(" ", "")
    if u in ("kgm-2s-1", "kg/m2/s", "mm/s", "mms-1", "kgm-2/s"):
        return a * 3600.0 * step_h
    if u in ("mm/h", "mmh-1", "mm/hr"):
        return a * step_h
    if u in ("mm/d", "mm/day", "mmd-1", "kgm-2d-1"):
        return a * step_h / 24.0
    if u == "m":
        return a * 1000.0
    return a                                              # mm (or kg m-2) per step


# ---------------------------------------------------------------------------
# spatial weights
# ---------------------------------------------------------------------------

class Regridder:
    """Weights from a model grid to target points: bilinear on 1-D axes, IDW-4 on 2-D lat/lon."""

    def __init__(self, lat, lon, t_lat, t_lon):
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        lon = np.where(lon > 180, lon - 360, lon)
        t_lat, t_lon = np.asarray(t_lat, float), np.asarray(t_lon, float)
        self.shape = None
        if lat.ndim == 1 and lon.ndim == 1:
            self.kind = "bilinear"
            self.ny, self.nx = len(lat), len(lon)
            ia, fa = self._axis(lat, t_lat)
            ib, fb = self._axis(lon, t_lon)
            idx = [(ia, ib), (ia + 1, ib), (ia, ib + 1), (ia + 1, ib + 1)]
            w = [(1 - fa) * (1 - fb), fa * (1 - fb), (1 - fa) * fb, fa * fb]
            self.flat = np.stack([np.clip(i, 0, self.ny - 1) * self.nx + np.clip(j, 0, self.nx - 1) for i, j in idx], 1)
            self.w = np.stack(w, 1)
            valid = (ia >= 0) & (ib >= 0) & (ia < self.ny - 1) & (ib < self.nx - 1)
            self.w[~valid] = np.nan
            self.res_km = float(np.nanmedian(np.abs(np.diff(lat)))) * 111.2
        else:
            self.kind = "idw4"
            LAT, LON = (lat, lon) if lat.ndim == 2 else np.meshgrid(lat, lon, indexing="ij")
            self.ny, self.nx = LAT.shape
            sv = self._xyz(LAT.ravel(), LON.ravel())
            tv = self._xyz(t_lat, t_lon)
            k = 4
            self.flat = np.zeros((len(t_lat), k), dtype=np.int64)
            d = np.zeros((len(t_lat), k))
            for s in range(0, len(t_lat), 4096):
                dd = 2 - 2 * (tv[s:s + 4096] @ sv.T)                     # squared chord distance
                part = np.argpartition(dd, k, axis=1)[:, :k]
                self.flat[s:s + 4096] = part
                d[s:s + 4096] = np.take_along_axis(dd, part, axis=1)
            dist = np.sqrt(np.maximum(d, 0)) * 6371.0
            wt = 1.0 / np.maximum(dist, 1e-3) ** 2
            self.w = wt / wt.sum(axis=1, keepdims=True)
            step = np.sqrt(np.maximum(2 - 2 * np.sum(sv[1:] * sv[:-1], axis=1), 0)) * 6371.0
            self.res_km = float(np.median(step[step > 0])) if (step > 0).any() else float("nan")

    @staticmethod
    def _xyz(lat, lon):
        la, lo = np.radians(lat), np.radians(lon)
        return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], -1)

    @staticmethod
    def _axis(ax, t):
        asc = ax[-1] > ax[0]
        a = ax if asc else ax[::-1]
        i = np.searchsorted(a, t) - 1
        i = np.clip(i, -1, len(a) - 1)
        lo_ = a[np.clip(i, 0, len(a) - 1)]
        hi_ = a[np.clip(i + 1, 0, len(a) - 1)]
        f = np.where(hi_ > lo_, (t - lo_) / np.where(hi_ > lo_, hi_ - lo_, 1), 0.0)
        bad = (t < a[0]) | (t > a[-1])
        i = np.where(bad, -1, i)
        if not asc:                                   # map back to the descending axis
            n = len(a)
            i2 = n - 2 - i
            return np.where(bad, -1, i2), 1 - f
        return i, f

    def apply(self, field2d):
        """field2d (ny, nx) or (nt, ny, nx) -> (n_targets,) or (nt, n_targets)"""
        f = np.asarray(field2d, dtype=np.float64)
        flat = f.reshape(f.shape[:-2] + (-1,))
        vals = flat[..., self.flat]                      # (..., n, 4)
        w = self.w
        if self.kind == "bilinear":
            ok = np.isfinite(vals) & np.isfinite(w)
            num = np.nansum(np.where(ok, vals * w, 0), axis=-1)
            den = np.sum(np.where(ok, w, 0), axis=-1)
            return np.where(den > 0.5, num / np.where(den > 0, den, 1), np.nan)
        ok = np.isfinite(vals)
        num = np.sum(np.where(ok, vals * w, 0), axis=-1)
        den = np.sum(np.where(ok, w, 0), axis=-1)
        return np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)


# ---------------------------------------------------------------------------
# the source
# ---------------------------------------------------------------------------

def _decode_times(tvals):
    out = []
    for t in np.asarray(tvals).ravel():
        if isinstance(t, (np.datetime64, pd.Timestamp, datetime)):
            out.append(pd.Timestamp(t))
            continue
        if isinstance(t, (bytes, np.bytes_)):            # WRF Times "2009-05-13_00:00:00"
            t = t.decode()
        if isinstance(t, str):
            out.append(pd.Timestamp(t.replace("_", " ")))
            continue
        try:                                             # cftime objects (noleap, 360_day, ...)
            out.append(pd.Timestamp(t.year, t.month, t.day, t.hour, t.minute))
        except (ValueError, AttributeError):
            out.append(pd.NaT)                           # e.g. Feb 30 in a 360-day calendar
    return pd.DatetimeIndex(out)


class DatasetSource(BaseSource):
    synthetic = False

    def __init__(self, config: dict, cache_files: int = 6):
        super().__init__()
        self.cfg = dict(config)
        self.slug = slugify(self.cfg.get("slug") or self.cfg["name"])
        self.name = f"ds-{self.slug}"
        self.preset = self.cfg.get("preset", "cf")
        self.varmap = {**PRESETS[self.preset], **(self.cfg.get("variables") or {})}
        self.description = (f"{self.cfg['name']} ({self.preset} preset"
                            + (f", {self.cfg['scenario']}" if self.cfg.get("scenario") else "")
                            + (f", {self.cfg['gcm']}" if self.cfg.get("gcm") else "") + ")")
        self.precip_period = self.cfg.get("precip_period", "ending")      # accumulation interval ends at the stamp
        self._index = None                      # [(path, times)]
        self._open: OrderedDict = OrderedDict()
        self._cache_files = cache_files
        self._regrid: dict = {}
        self._lock2 = threading.Lock()

    # ---- files and time index
    def sources(self):
        out = []
        for p in self.cfg.get("paths") or []:
            if re.match(r"^[a-z0-9+]+://", p):
                out.append(p)
            else:
                out += sorted(glob.glob(str(Path(p).expanduser())))
        return out

    def _open_ds(self, path):
        import xarray as xr
        with self._lock2:
            if path in self._open:
                self._open.move_to_end(path)
                return self._open[path]
        ds = _open_any(path)
        with self._lock2:
            self._open[path] = ds
            while len(self._open) > self._cache_files:
                self._open.popitem(last=False)[1].close()
        return ds

    def _times_of(self, ds):
        import xarray as xr
        name = next((n for n in TIME_NAMES if n in ds.variables), None)
        if name is None and "Times" in ds.variables:                 # WRF
            return _decode_times(ds["Times"].values.astype(str) if ds["Times"].dtype.kind == "S" else
                                 ["".join(c.decode() for c in row) for row in ds["Times"].values])
        if name is None:
            raise ValueError("no time coordinate found")
        try:                                             # xarray >= 2025: decoder object
            coder = xr.coders.CFDatetimeCoder(use_cftime=True)
            tv = xr.decode_cf(ds[[name]], decode_times=coder)[name].values
        except (AttributeError, TypeError):
            tv = xr.decode_cf(ds[[name]], use_cftime=True)[name].values
        return _decode_times(tv)

    def index(self):
        if self._index is None:
            idx = []
            for p in self.sources():
                try:
                    t = self._times_of(self._open_ds(p))
                    idx.append((p, t))
                except Exception as e:
                    logger.warning("dataset %s: cannot index %s (%s)", self.name, p, e)
            if not idx:
                raise RuntimeError(f"dataset {self.cfg['name']}: no readable files in {self.cfg.get('paths')}")
            self._index = idx
        return self._index

    def domain(self, settings=None):
        allt = [t for _, ts in self.index() for t in ts if not pd.isna(t)]
        return min(allt).to_pydatetime(), max(allt).to_pydatetime()

    def origin_of_year(self, year):
        return datetime(year, 1, 1)

    def step_hours(self):
        t = pd.DatetimeIndex(sorted({x for _, ts in self.index() for x in ts if not pd.isna(x)}))
        d = np.diff(t.values).astype("timedelta64[m]").astype(float) / 60.0
        return float(np.median(d)) if len(d) else 1.0

    def status(self):
        s = super().status()
        try:
            d0, d1 = self.domain()
            step = self.step_hours()
            s.update({"files": len(self.index()), "start": d0.isoformat(), "end": d1.isoformat(),
                      "step_hours": step, "preset": self.preset, "config": self.cfg})
            if step > 6:
                s["warning"] = (f"{step:g}-hourly data: the diurnal cycle is not resolved, so hourly FWI/ERC/BI (and the "
                                "12:00 LST daily FWI) from it would be unreliable. Daily model output needs a diurnal "
                                "disaggregation step first (docs/future_glarm_plan.md).")
        except Exception as e:
            s.update({"error": str(e), "config": self.cfg})
        return s

    # ---- variable access
    def _var(self, ds, target):
        for cand in self.varmap.get(target, []):
            if "+" in cand:
                parts = cand.split("+")
                if all(p in ds.variables for p in parts):
                    return cand, sum(ds[p] for p in parts)
            elif cand in ds.variables:
                return cand, ds[cand]
        return None, None

    def _latlon(self, ds):
        lat = next((ds[n] for n in LAT_NAMES if n in ds.variables or n in ds.coords), None)
        lon = next((ds[n] for n in LON_NAMES if n in ds.variables or n in ds.coords), None)
        if lat is None or lon is None:
            raise ValueError("no latitude/longitude found")
        la, lo = np.asarray(lat.values), np.asarray(lon.values)
        if la.ndim == 3:                                  # WRF XLAT(Time, y, x)
            la, lo = la[0], lo[0]
        return la, lo

    def _regridder(self, path, ds, t_lat, t_lon, key):
        rk = (path, key)
        if rk not in self._regrid:
            la, lo = self._latlon(ds)
            self._regrid[rk] = Regridder(la, lo, t_lat, t_lon)
        return self._regrid[rk]

    def localize(self, dest: Path, bbox, margin_deg=0.6, progress=None) -> list:
        """Offline copy: the WUP window (bbox + margin) of every file, only the variables the FDIs use, as
        compressed NetCDF in ``dest``. Times, calendars and units are copied unchanged, so the copy reads exactly
        like the original."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        lon0, lat0, lon1, lat1 = bbox
        lon0, lat0, lon1, lat1 = lon0 - margin_deg, lat0 - margin_deg, lon1 + margin_deg, lat1 + margin_deg
        wanted = {p for v in self.varmap.values() for c in v for p in c.split("+")} | {"COSALPHA", "SINALPHA", "Times"}
        files = self.sources()
        out = []
        for i, path in enumerate(files):
            ds = self._open_ds(path)
            lat = next((ds[n] for n in LAT_NAMES if n in ds.variables or n in ds.coords), None)
            lon = next((ds[n] for n in LON_NAMES if n in ds.variables or n in ds.coords), None)
            if lat is None or lon is None:
                raise ValueError(f"{path}: no latitude/longitude")
            la, lo = (np.asarray(x.values) for x in (lat, lon))
            if la.ndim == 3:
                la, lo = la[0], lo[0]
            lo = np.where(lo > 180, lo - 360, lo)
            if la.ndim == 1:
                iy = np.where((la >= lat0) & (la <= lat1))[0]
                ix = np.where((lo >= lon0) & (lo <= lon1))[0]
                isel = {lat.dims[-1]: slice(iy.min(), iy.max() + 1), lon.dims[-1]: slice(ix.min(), ix.max() + 1)}
            else:
                inside = (la >= lat0) & (la <= lat1) & (lo >= lon0) & (lo <= lon1)
                yy, xx = np.where(inside)
                if not len(yy):
                    raise ValueError(f"{path}: no grid points over the WUP")
                ydim, xdim = lat.dims[-2], lat.dims[-1]
                isel = {ydim: slice(yy.min(), yy.max() + 1), xdim: slice(xx.min(), xx.max() + 1)}
            keep = [v for v in ds.data_vars if v in wanted or v in (lat.name, lon.name)]
            sub = ds[keep].isel({k: v for k, v in isel.items() if k in ds.dims})
            enc = {v: {"zlib": True, "complevel": 5} for v in sub.data_vars if sub[v].dtype.kind in "fiu"}
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(path.split("?")[0].rstrip("/")).name) or f"part{i:04d}"
            if not name.endswith(".nc"):
                name = name.replace(".zarr", "") + ".nc"
            dst = dest / name
            tmp = dst.with_suffix(".tmp")
            sub.load().to_netcdf(tmp, encoding=enc)
            os.replace(tmp, dst)
            out.append(str(dst))
            if progress:
                progress(i + 1, len(files), name)
        return out

    def _raw_brick_chunk(self, grid: Grid, year: int, k: int, brick: Brick):
        t0 = self.origin_of_year(year) + timedelta(hours=k * CHUNK_TIME)
        hours = pd.date_range(t0, periods=CHUNK_TIME, freq="h")
        hours = hours[hours.year == year]
        sub = grid.points[(grid.points.ci == brick.ci) & (grid.points.cj == brick.cj)]
        la0, la1 = int(sub.lat_idx.min()), int(sub.lat_idx.max()) + 1
        lo0, lo1 = int(sub.lon_idx.min()), int(sub.lon_idx.max()) + 1
        ny, nx = la1 - la0, lo1 - lo0
        t_lat = np.repeat(grid.lat0 + np.arange(la0, la1) * grid.step, nx)
        t_lon = np.tile(grid.lon0 + np.arange(lo0, lo1) * grid.step, ny)
        step = self.step_hours()
        lo_t, hi_t = hours[0] - pd.Timedelta(hours=step), hours[-1] + pd.Timedelta(hours=step)
        steps = {v: [] for v in VARIABLES}
        stamps = []
        units_seen = {}
        for path, ts in self.index():
            sel = np.where((ts >= lo_t) & (ts <= hi_t))[0]
            if not len(sel):
                continue
            ds = self._open_ds(path)
            tdim = next((d for d in ("time", "Time", "valid_time") if d in ds.dims), None)
            rg = self._regridder(path, ds, t_lat, t_lon, brick.key)
            fields = {}
            for target in list(VARIABLES) + ["_wind_speed"]:
                name, da = self._var(ds, target)
                if da is None:
                    continue
                units_seen[target] = (name, _units(da) if hasattr(da, "attrs") else "")
                a = np.asarray(da.isel({tdim: sel}).values if tdim else da.values[sel], dtype=np.float64)
                fields[target] = rg.apply(a)            # (n_sel, n_targets)
            # WRF wind rotation to earth-relative
            if self.preset == "wrf" and "COSALPHA" in ds.variables and "UGRD_10maboveground" in fields:
                ca, sa = (rg.apply(np.asarray(ds[n].values)[0] if ds[n].ndim == 3 else ds[n].values) for n in ("COSALPHA", "SINALPHA"))
                u, v = fields["UGRD_10maboveground"], fields["VGRD_10maboveground"]
                fields["UGRD_10maboveground"], fields["VGRD_10maboveground"] = u * ca - v * sa, v * ca + u * sa
            for i, s_ in enumerate(sel):
                stamps.append(ts[s_])
                for v in VARIABLES:
                    steps[v].append(fields[v][i] if v in fields else None)
                steps.setdefault("_wind_speed", []).append(fields["_wind_speed"][i] if "_wind_speed" in fields else None)
        n_t = len(hours)
        arrays = {v: np.full((n_t, ny * nx), np.nan, dtype=np.float64) for v in VARIABLES}
        if stamps:
            order = np.argsort(np.asarray(stamps, dtype="datetime64[ns]"))
            st = pd.DatetimeIndex(np.asarray(stamps, dtype="datetime64[ns]")[order])
            keep = ~st.duplicated()
            st = st[keep]
            pick = lambda lst: [lst[j] for j, kk in zip(order, keep) if kk]   # noqa: E731
            sx = np.asarray((st - hours[0]) / pd.Timedelta("1h"), dtype=float)
            hx = np.arange(n_t, dtype=float)
            raw = {}
            for v in list(VARIABLES) + ["_wind_speed"]:
                vals = pick(steps.get(v, []))
                if not vals or any(x is None for x in vals):
                    continue
                raw[v] = np.asarray(vals)                                  # (n_steps, n_targets)
            # precipitation: per-step amounts spread over their hours (before interpolating states)
            if "APCP_surface" in raw:
                name, units = units_seen["APCP_surface"]
                stp = float(np.median(np.diff(sx))) if len(sx) > 1 else step
                pr = raw.pop("APCP_surface")
                if self.preset == "wrf":                                   # run-accumulated -> per step
                    pr = np.diff(np.vstack([pr[:1], pr]), axis=0)
                    pr = np.maximum(pr, 0)
                else:
                    pr = precip_mm_per_step(pr, units, stp)
                per_h = pr / max(stp, 1.0)
                for j, x in enumerate(sx):
                    a0, a1 = (x - stp, x) if self.precip_period == "ending" else ((x, x + stp) if self.precip_period == "starting"
                                                                                   else (x - stp / 2, x + stp / 2))
                    hs = np.where((hx > a0 - 1e-9) & (hx <= a1 + 1e-9))[0] if self.precip_period == "ending" else \
                        np.where((hx >= a0 - 1e-9) & (hx < a1 - 1e-9))[0]
                    arrays["APCP_surface"][hs] = per_h[j][None]
            for v, a in raw.items():
                if v == "_wind_speed":
                    continue
                # vectorised linear interpolation in time
                j = np.searchsorted(sx, hx) - 1
                j = np.clip(j, 0, len(sx) - 2) if len(sx) > 1 else np.zeros_like(hx, dtype=int)
                if len(sx) > 1:
                    f = ((hx - sx[j]) / np.where(sx[j + 1] > sx[j], sx[j + 1] - sx[j], 1))[:, None]
                    out = a[j] * (1 - f) + a[j + 1] * f
                    bad = (hx < sx[0]) | (hx > sx[-1]) | ((sx[j + 1] - sx[j]) > max(2 * step, 6.5))
                    out[bad] = np.nan
                else:
                    out = np.where(np.abs(hx - sx[0])[:, None] < 0.5, a[0][None], np.nan)
                arrays[v] = out
            # units and derived quantities
            if "TMP_2maboveground" in raw:
                arrays["TMP_2maboveground"] = to_kelvin(arrays["TMP_2maboveground"], units_seen["TMP_2maboveground"][1])
            if "PRES_surface" in raw:
                arrays["PRES_surface"] = to_pa(arrays["PRES_surface"], units_seen["PRES_surface"][1])
            if "SPFH_2maboveground" in raw:
                name, units = units_seen["SPFH_2maboveground"]
                kind = ("mixing_ratio" if self.preset == "wrf" else "rh" if name in ("hurs", "rh", "rh2m")
                        else "dewpoint" if name in ("d2m", "2d") else "q")
                arrays["SPFH_2maboveground"] = q_from(kind, arrays["SPFH_2maboveground"], units,
                                                      arrays["TMP_2maboveground"], arrays["PRES_surface"])
            if "UGRD_10maboveground" not in raw and "_wind_speed" in raw:
                # speed only: carry it as u (direction does not enter the indices)
                j = np.clip(np.searchsorted(sx, hx) - 1, 0, max(len(sx) - 2, 0))
                ws = raw["_wind_speed"]
                f = ((hx - sx[j]) / np.where(sx[j + 1] > sx[j], sx[j + 1] - sx[j], 1))[:, None] if len(sx) > 1 else 0
                arrays["UGRD_10maboveground"] = ws[j] * (1 - f) + ws[np.minimum(j + 1, len(sx) - 1)] * f
                arrays["VGRD_10maboveground"] = np.zeros_like(arrays["UGRD_10maboveground"])
            if "DSWRF_surface" in raw and self.preset == "era5":
                arrays["DSWRF_surface"] = arrays["DSWRF_surface"] / 3600.0
        arrays = {v: a.reshape(n_t, ny, nx).astype(np.float32) for v, a in arrays.items()}
        rg_any = next(iter(self._regrid.values()), None)
        meta = {"lat_idx_range": [la0, la1], "lon_idx_range": [lo0, lo1], "dataset": self.cfg["name"],
                "model_resolution_km": round(rg_any.res_km, 1) if rg_any else None, "step_hours": step,
                "variables": {k_: v_[0] for k_, v_ in units_seen.items()}}
        return arrays, hours, meta, False


# ---------------------------------------------------------------------------
# registry (workspace/datasets/*.json)
# ---------------------------------------------------------------------------

WS_TOKEN = "${WORKSPACE}"


class DatasetRegistry:
    """Dataset definitions in workspace/datasets/*.json. Paths inside the workspace are stored as
    ``${WORKSPACE}/...`` so a copied or bundled workspace keeps working."""

    def __init__(self, ws):
        self.ws = ws
        self.dir = ws.root / "datasets"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _expand(self, cfg):
        cfg = dict(cfg)
        cfg["paths"] = [p.replace(WS_TOKEN, str(self.ws.root)) for p in cfg.get("paths") or []]
        return cfg

    def list(self):
        out = []
        for f in sorted(self.dir.glob("*.json")):
            try:
                out.append(self._expand(json.loads(f.read_text())))
            except Exception:
                continue
        return out

    def get(self, slug):
        f = self.dir / f"{slug}.json"
        return self._expand(json.loads(f.read_text())) if f.exists() else None

    def local_dir(self, slug) -> Path:
        return self.ws.root / "inputs" / "datasets" / slug

    def set_local(self, slug, files):
        """Point a dataset at its offline copy (keeping the original locations for reference)."""
        f = self.dir / f"{slug}.json"
        cfg = json.loads(f.read_text())
        if not cfg.get("remote_paths"):
            cfg["remote_paths"] = cfg["paths"]
        cfg["paths"] = [f"{WS_TOKEN}/inputs/datasets/{slug}/*.nc"]
        cfg["local_copy"] = {"files": len(files), "made": now_iso()}
        f.write_text(json.dumps(cfg, indent=2))
        return self._expand(cfg)

    def save(self, cfg: dict):
        cfg = dict(cfg)
        if not cfg.get("name"):
            raise ValueError("a dataset needs a name")
        if cfg.get("preset", "cf") not in PRESETS:
            raise ValueError(f"preset must be one of {list(PRESETS)}")
        if not cfg.get("paths"):
            raise ValueError("give one or more file paths, glob patterns or URLs")
        if isinstance(cfg["paths"], str):
            cfg["paths"] = [p.strip() for p in cfg["paths"].splitlines() if p.strip()]
        cfg["slug"] = slugify(cfg.get("slug") or cfg["name"])
        root = str(self.ws.root)
        cfg["paths"] = [WS_TOKEN + p[len(root):] if p.startswith(root) else p for p in cfg["paths"]]
        (self.dir / f"{cfg['slug']}.json").write_text(json.dumps(cfg, indent=2))
        return self._expand(cfg)

    def delete(self, slug):
        (self.dir / f"{slug}.json").unlink(missing_ok=True)
