"""
Local input store: a compact, offline copy of the AORC data the WUP needs.

Every AORC brick the explorer downloads is written here (unless switched off), and
a bulk "download period" task can fill it ahead of time. With the store in place
the explorer runs without the network, and the store can travel in a research
bundle.

Layout (one file per 144-h AORC time chunk, all bricks of the WUP rectangle):

    WUP_Explorer_data/inputs/aorc/<year>/AORC_WUP_<year>_k<kkk>.nc

* NetCDF4 / CF-1.8, the six chain variables + DSWRF as int16 with scale/offset,
  zlib level 6 + shuffle, chunks (time 144, 64, 64). Quantization steps are far
  below AORC's own precision (0.01 K, 1e-6 kg/kg, 1 Pa, 0.01 m/s, 0.01 mm, 0.1 W/m2).
* Only study cells plus a 3-cell ring around them are kept (the resolution
  ladder's widest neighbourhood is 7x7), so the gap-filling of every study cell is
  identical to working from S3; everything else is a fill value that compresses away.
* A SQLite table (``input_chunks``) records which (year, chunk, brick) are present.
"""

from __future__ import annotations

import json
import os
import threading
import time as _time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.workspace import now_iso

DATASET = "aorc-v1.1"
FILL = np.int16(-32768)
COMP = dict(compression="zlib", complevel=6, shuffle=True)
RING = 3
# variable: (scale, offset, units, long_name)
PACK = {
    "TMP_2maboveground": (0.01, 273.15, "K", "2 m air temperature"),
    "SPFH_2maboveground": (1e-6, 0.016, "kg kg-1", "2 m specific humidity"),
    "PRES_surface": (1.0, 85000.0, "Pa", "surface pressure"),
    "UGRD_10maboveground": (0.01, 0.0, "m s-1", "10 m eastward wind"),
    "VGRD_10maboveground": (0.01, 0.0, "m s-1", "10 m northward wind"),
    "APCP_surface": (0.01, 327.67, "kg m-2", "precipitation in the hour ending at the stamp"),
    "DSWRF_surface": (0.1, 3276.7, "W m-2", "downward shortwave radiation"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS input_chunks (
    dataset TEXT, year INTEGER, k INTEGER, brick TEXT, n_hours INTEGER, t_start TEXT, t_end TEXT,
    path TEXT, created TEXT, meta TEXT, PRIMARY KEY (dataset, year, k, brick));
"""
from aorc_tools.explorer.hdf import HDF_LOCK as LOCK  # noqa: E402  (one lock for all HDF5 use)


def pack(v, a):
    s, o = PACK[v][0], PACK[v][1]
    q = np.round((np.asarray(a, dtype=np.float64) - o) / s)
    return np.where(np.isfinite(q), np.clip(q, -32767, 32767), FILL).astype(np.int16)


def unpack(v, q):
    s, o = PACK[v][0], PACK[v][1]
    q = np.asarray(q)
    return np.where(q == FILL, np.nan, q.astype(np.float64) * s + o).astype(np.float32)


def _dilate(mask, r):
    out = mask.copy()
    ny, nx = mask.shape
    ys, xs = np.where(mask)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            y, x = ys + dy, xs + dx
            ok = (y >= 0) & (y < ny) & (x >= 0) & (x < nx)
            out[y[ok], x[ok]] = True
    return out


class InputStore:
    def __init__(self, ws, grid, dataset=DATASET):
        self.ws, self.grid, self.dataset = ws, grid, dataset
        self.root = ws.root / "inputs" / "aorc"
        self.root.mkdir(parents=True, exist_ok=True)
        with ws.conn() as c:
            c.executescript(SCHEMA)
        self.la0, la1 = grid.lat_idx_range
        self.lo0, lo1 = grid.lon_idx_range
        self.ny, self.nx = la1 - self.la0, lo1 - self.lo0
        y = grid.points.lat_idx.values - self.la0
        x = grid.points.lon_idx.values - self.lo0
        study = np.zeros((self.ny, self.nx), dtype=bool)
        study[y, x] = True
        self.keep = _dilate(study, RING)
        self.lat = grid.lat0 + (self.la0 + np.arange(self.ny)) * grid.step
        self.lon = grid.lon0 + (self.lo0 + np.arange(self.nx)) * grid.step

    # ------------------------------------------------------------------ catalog
    def path_for(self, year, k):
        return self.root / str(year) / f"AORC_WUP_{year}_k{int(k):03d}.nc"

    def has(self, year, k, brick_key) -> bool:
        with self.ws.conn() as c:
            r = c.execute("SELECT path FROM input_chunks WHERE dataset=? AND year=? AND k=? AND brick=?",
                          (self.dataset, int(year), int(k), brick_key)).fetchone()
        return bool(r) and Path(r["path"]).exists()

    def quarantine(self, year, k, reason):
        """Set a damaged chunk file aside and forget every brick in it (they are fetched again when needed)."""
        path = self.path_for(year, k)
        with LOCK:
            self.ws.quarantine(path, "inputs", reason)
            with self.ws.conn() as c:
                c.execute("DELETE FROM input_chunks WHERE dataset=? AND year=? AND k=?", (self.dataset, int(year), int(k)))

    def inventory(self, domain_start=None, domain_end=None) -> dict:
        with self.ws.conn() as c:
            rows = c.execute("SELECT year, COUNT(*) AS n, SUM(n_hours) AS hours FROM input_chunks WHERE dataset=? "
                             "GROUP BY year ORDER BY year", (self.dataset,)).fetchall()
        files = list(self.root.rglob("*.nc"))
        nbytes = sum(f.stat().st_size for f in files)
        n_bricks = len(self.grid.bricks())
        per_year = {int(r["year"]): {"brick_chunks": int(r["n"]), "hours": int(r["hours"] or 0)} for r in rows}
        total_bc = sum(v["brick_chunks"] for v in per_year.values())
        # whole-domain brick-chunk count, exactly as jobs and downloads cut it (year stores; 1979 starts Feb 1)
        ds = pd.Timestamp(domain_start or "1979-02-01T00").to_pydatetime()
        de = pd.Timestamp(domain_end or "2026-09-30T23").to_pydatetime()
        origin = lambda y: datetime(1979, 2, 1) if y == 1979 else datetime(y, 1, 1)  # noqa: E731
        expected = {}
        for ch in self.grid.time_chunks(ds, de, origin) if de >= ds else []:
            expected[ch["year"]] = expected.get(ch["year"], 0) + n_bricks
        for y, n in expected.items():
            per_year.setdefault(y, {"brick_chunks": 0, "hours": 0})["expected"] = n
        domain_bc = sum(expected.values())
        packets_per_bc = len(self.grid.points) / max(n_bricks, 1) * 144
        return {"dataset": self.dataset, "dir": str(self.root), "files": len(files), "bytes": nbytes,
                "brick_chunks": total_bc, "domain_brick_chunks": domain_bc,
                "percent": round(100.0 * total_bc / domain_bc, 4) if domain_bc else 0.0,
                "bytes_per_value": round(nbytes / max(1.0, total_bc * packets_per_bc * len(PACK)), 3),
                "full_record_estimate_bytes": int(nbytes / total_bc * domain_bc) if total_bc else None,
                "per_year": per_year}

    # ------------------------------------------------------------------ write / read
    def write_brick_chunk(self, year, k, brick_key, arrays: dict, times: pd.DatetimeIndex, meta: dict):
        """arrays: var -> (n_t, ny_b, nx_b) for the brick's clip rectangle given by meta lat/lon_idx_range."""
        import netCDF4
        la0, la1 = meta["lat_idx_range"]
        lo0, lo1 = meta["lon_idx_range"]
        ys, xs = slice(la0 - self.la0, la1 - self.la0), slice(lo0 - self.lo0, lo1 - self.lo0)
        keep = self.keep[ys, xs]
        path = self.path_for(year, k)
        path.parent.mkdir(parents=True, exist_ok=True)
        times = pd.DatetimeIndex(times)
        nt = len(times)
        with LOCK:
            data, bricks = None, {}
            if path.exists():
                try:
                    with netCDF4.Dataset(path) as ds:
                        old_t = pd.to_datetime(np.asarray(ds["time"][:], dtype=np.int64), unit="h", origin=pd.Timestamp("1979-01-01"))
                        if len(old_t) != nt or not (old_t == times).all():
                            raise ValueError(f"input chunk {path.name} has different hours")
                        data = {}
                        for v in PACK:
                            ds[v].set_auto_maskandscale(False)
                            data[v] = np.asarray(ds[v][:])
                        bricks = json.loads(getattr(ds, "bricks_json", "{}"))
                except Exception as e:                  # damaged: keep it aside and start this chunk file afresh
                    self.quarantine(year, k, f"unreadable while merging brick {brick_key}: {e}")
                    data, bricks = None, {}
            if data is None:
                data = {v: np.full((nt, self.ny, self.nx), FILL, dtype=np.int16) for v in PACK}
            for v in PACK:
                if v in arrays:
                    blk = pack(v, arrays[v])
                    cur = data[v][:, ys, xs]
                    data[v][:, ys, xs] = np.where(keep[None], blk, cur)
            bricks[brick_key] = {k_: meta.get(k_) for k_ in ("store", "fetched_at", "lat_idx_range", "lon_idx_range")}
            tmp = path.with_name(path.stem + f".{threading.get_ident()}.tmp")
            with netCDF4.Dataset(tmp, "w", format="NETCDF4") as ds:
                ds.createDimension("time", nt)
                ds.createDimension("latitude", self.ny)
                ds.createDimension("longitude", self.nx)
                t = ds.createVariable("time", "i4", ("time",))
                t.units, t.calendar, t.standard_name = "hours since 1979-01-01 00:00:00", "standard", "time"
                t[:] = ((times - pd.Timestamp("1979-01-01")) / pd.Timedelta("1h")).astype(np.int64)
                la = ds.createVariable("latitude", "f8", ("latitude",)); la.units = "degrees_north"; la[:] = self.lat
                lo = ds.createVariable("longitude", "f8", ("longitude",)); lo.units = "degrees_east"; lo[:] = self.lon
                for v, (s, o, units, ln) in PACK.items():
                    var = ds.createVariable(v, "i2", ("time", "latitude", "longitude"), fill_value=FILL,
                                            chunksizes=(nt, min(64, self.ny), min(64, self.nx)), **COMP)
                    var.scale_factor, var.add_offset = np.float32(s), np.float32(o)
                    var.units, var.long_name = units, ln
                    var.set_auto_maskandscale(False)
                    var[:] = data[v]
                ds.Conventions = "CF-1.8"
                ds.title = "NOAA AORC v1.1, Western Upper Peninsula subset (study cells + 3-cell ring)"
                ds.source = "s3://noaa-nws-aorc-v1-1-1km (NOAA Office of Water Prediction)"
                ds.dataset = self.dataset
                ds.year, ds.time_chunk = int(year), int(k)
                ds.bricks_json = json.dumps(bricks)
                ds.packing = ("int16 with scale_factor/add_offset; steps: 0.01 K, 1e-6 kg/kg, 1 Pa, 0.01 m/s, "
                              "0.01 mm, 0.1 W/m2. Cells farther than 3 cells from the study area are fill values.")
                ds.history = f"{datetime.now().isoformat(timespec='seconds')} written by WUP Grid Explorer"
            from aorc_tools.explorer.runtime import fsync_file
            from aorc_tools.explorer.store import _replace
            fsync_file(tmp)
            try:
                _replace(tmp, path)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
            with self.ws.conn() as c:
                c.execute("INSERT OR REPLACE INTO input_chunks (dataset, year, k, brick, n_hours, t_start, t_end, path, created, meta) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (self.dataset, int(year), int(k), brick_key, nt, times[0].isoformat(), times[-1].isoformat(),
                           str(path), now_iso(), json.dumps(bricks[brick_key])))
        return path

    def read_brick_chunk(self, year, k, brick_key, lat_idx_range, lon_idx_range):
        """-> (arrays var->(n_t, ny_b, nx_b) float32, times, meta) or None."""
        import netCDF4
        path = self.path_for(year, k)
        if not self.has(year, k, brick_key):
            return None
        la0, la1 = lat_idx_range
        lo0, lo1 = lon_idx_range
        ys, xs = slice(la0 - self.la0, la1 - self.la0), slice(lo0 - self.lo0, lo1 - self.lo0)
        with LOCK:
            with netCDF4.Dataset(path) as ds:
                times = pd.to_datetime(np.asarray(ds["time"][:], dtype=np.int64), unit="h", origin=pd.Timestamp("1979-01-01"))
                arrays = {}
                for v in PACK:
                    ds[v].set_auto_maskandscale(False)
                    arrays[v] = unpack(v, ds[v][:, ys, xs])
                bricks = json.loads(getattr(ds, "bricks_json", "{}"))
        meta = {"lat_idx_range": [la0, la1], "lon_idx_range": [lo0, lo1], "local_store": str(path),
                **{k_: v for k_, v in (bricks.get(brick_key) or {}).items() if k_ in ("store", "fetched_at")}}
        return arrays, times, meta
