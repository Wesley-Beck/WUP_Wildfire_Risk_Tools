"""
Results store: compact, standard files that any NetCDF tool can open.

Layout (inside the workspace)::

    results/<source>/<config_id>/<year>/WUP_FDI_<year>_k<kkk>_<YYYYMMDD>.nc

One file per 144-hour time chunk (the AORC chunk length), covering the whole
study area on its native regular lat/lon grid (277 x 324 AORC cells, of which
28,408 are study cells). Each file is CF-1.8 NetCDF-4 with

    FWI, FPI, ERC, BI (time, latitude, longitude)   int16 packed: FWI 0.1, FPI/ERC/BI 1 unit
    wx_tier, ndvi_tier (time, latitude, longitude)  ubyte: which rung of the resolution ladder
    point_id, county, std_offset_h (latitude, longitude)
    group chain_state/: start_* and end_* moisture state (float32) so any chunk can be
                        re-run exactly and the next chunk continues seamlessly

compressed with zlib (deflate level 6 + byte shuffle, chunks 24 h x 64 x 64): about
1.5 bytes per packet for all four indices, ~10x smaller than float32. Readers:

    xarray     xr.open_dataset(one_file) or xr.open_mfdataset("results/<source>/<config>/2009/*.nc") (dask)
    Panoply    open any file -> FWI -> map, step through hours
    QGIS/GDAL  add as raster layer (NetCDF), choose the variable, use the temporal controller

Files are written to a temp name and renamed, so a crash never corrupts saved
results. Every netCDF/HDF5 call in the process goes through one lock (HDF5 is
not thread-safe). The SQLite catalog in workspace.db records which brick of
which chunk is done, so coverage never has to open a file.
"""

from __future__ import annotations

import json
import os
import threading
import time as _time
import warnings
from datetime import datetime
from pathlib import Path

import logging

import numpy as np
import pandas as pd

from aorc_tools.explorer.chain import STATE_KEYS
from aorc_tools.explorer.grid import Grid
from aorc_tools.explorer.ndvi import MODIS_START
from aorc_tools.explorer.workspace import Workspace, now_iso

logger = logging.getLogger(__name__)
STORED = ["FWI", "FPI", "ERC", "BI"]            # the four indices (coverage, percentiles)
SAVED = STORED + ["FWI_daily"]                  # everything written per packet
PACK = {  # variable: (scale, long_name, units, comment)
    "FWI": (0.1, "Canadian Fire Weather Index (hourly)", "1", "Hourly CFFWIS chain (cffdrs-ng / FWI2025 unless config_json says legacy); native value."),
    "FWI_daily": (0.1, "Canadian Fire Weather Index, classic daily (12:00 LST)", "1",
                  "Van Wagner 1987 daily chain updated once per cell at 12:00 local standard time with the 24 h rain to noon; "
                  "the value is held until the next noon. Same equations as xclim cffwis_indices / CWFIS / GEFF."),
    "FPI": (1.0, "Fire Potential Index (Burgan 1998)", "1", "0-100. MODIS era only (2000-02-18 on) unless climatological NDVI was enabled."),
    "ERC": (1.0, "Energy Release Component (NFDRS, derived)", "BTU ft-2", "Derived/simplified NFDRS index; report as a percentile."),
    "BI": (1.0, "Burning Index (NFDRS, derived)", "1", "Derived/simplified NFDRS index; report as a percentile."),
}
FILL_I2 = np.int16(-32768)
FILL_U1 = np.uint8(255)
from aorc_tools.explorer.hdf import HDF_LOCK as NC_LOCK  # noqa: E402  (one lock for all HDF5 use)
from aorc_tools.explorer.runtime import fsync_file, replace_file  # noqa: E402

DAILY_FORMAT = 4        # 2: daily minima; 3: hour of the minimum; 4: part-days left out, Dec 31 completed, tie-midpoint
                        # hours, hourly FWI at noon. Older daily files are rebuilt
MIN_DAY_HOURS = 20      # a local day needs this many saved hours to get a daily maximum / minimum
TIME_UNITS = "hours since 1979-01-01 00:00:00"
T_EPOCH = pd.Timestamp("1979-01-01")
COUNTIES = ["Baraga", "Gogebic", "Houghton", "Iron", "Keweenaw", "Ontonagon"]
COMP = dict(compression="zlib", complevel=6, shuffle=True)


def _ts(s) -> pd.Timestamp:
    return pd.Timestamp(str(s).replace("Z", ""))


def _replace(tmp: Path, dst: Path):
    replace_file(tmp, dst)


def _month_hours(a, b) -> dict:
    """{(year, month): number of whole hours of [a, b] in that month} without building an hourly index."""
    a, b = pd.Timestamp(a), pd.Timestamp(b)
    out = {}
    cur = a
    while cur <= b:
        nxt = (cur + pd.offsets.MonthBegin(1)).normalize()
        stop = min(b, nxt - pd.Timedelta("1h"))
        out[(cur.year, cur.month)] = int((stop - cur) / pd.Timedelta("1h")) + 1
        cur = nxt
    return out


def _nc_times(ds):
    return T_EPOCH + pd.to_timedelta(np.asarray(ds["time"][:], dtype=np.int64), unit="h")


class ResultsStore:
    def __init__(self, ws: Workspace, grid: Grid):
        self.ws, self.grid = ws, grid
        self.brick_cells = {b["key"]: b["n_cells"] for b in grid.bricks()}
        self.la0, la1 = grid.lat_idx_range
        self.lo0, lo1 = grid.lon_idx_range
        self.ny, self.nx = la1 - self.la0, lo1 - self.lo0
        self.y = (grid.points.lat_idx.values - self.la0).astype(np.int32)    # per grid row
        self.x = (grid.points.lon_idx.values - self.lo0).astype(np.int32)
        self.lat = grid.lat0 + (self.la0 + np.arange(self.ny)) * grid.step
        self.lon = grid.lon0 + (self.lo0 + np.arange(self.nx)) * grid.step
        pid = np.full((self.ny, self.nx), -1, dtype=np.int32)
        pid[self.y, self.x] = grid.points.point_id.values
        cty = np.full((self.ny, self.nx), -1, dtype=np.int8)
        cty[self.y, self.x] = [COUNTIES.index(c) if c in COUNTIES else -1 for c in grid.points.county.values]
        off = np.zeros((self.ny, self.nx), dtype=np.int8)
        off[self.y, self.x] = grid.points.std_offset_h.values
        self._static = {"point_id": pid, "county": cty, "std_offset_h": off}

    # ------------------------------------------------------------------ catalog
    def chunk_dir(self, source, cfg_id, year):
        return self.ws.results_dir / source / cfg_id / str(year)

    def path_for(self, source, cfg_id, year, k, chunk_start):
        return self.chunk_dir(source, cfg_id, year) / f"WUP_FDI_{year}_k{int(k):03d}_{pd.Timestamp(chunk_start):%Y%m%d}.nc"

    def register_config(self, source, cfg):
        with self.ws.conn() as c:
            c.execute("INSERT OR IGNORE INTO configs (source, config_id, config, label, created) VALUES (?,?,?,?,?)",
                      (source, cfg.id, json.dumps(cfg.as_dict()), cfg.label(), now_iso()))
        d = self.ws.results_dir / source / cfg.id
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps({"config_id": cfg.id, "source": source, "label": cfg.label(),
                                                   "config": cfg.as_dict()}, indent=2))

    def configs(self, source=None):
        q = ("SELECT c.source, c.config_id, c.config, c.label, COUNT(k.k) AS chunks, SUM(k.n_hours*k.n_cells) AS packets "
             "FROM configs c LEFT JOIN chunks k ON k.source=c.source AND k.config_id=c.config_id")
        args = ()
        if source:
            q += " WHERE c.source=?"
            args = (source,)
        q += " GROUP BY c.source, c.config_id ORDER BY packets DESC"
        with self.ws.conn() as c:
            rows = c.execute(q, args).fetchall()
        return [{**dict(r), "config": json.loads(r["config"]), "packets": int(r["packets"] or 0)} for r in rows]

    def chunk_row(self, source, cfg_id, year, k, brick):
        with self.ws.conn() as c:
            r = c.execute("SELECT rowid AS _rowid, * FROM chunks WHERE source=? AND config_id=? AND year=? AND k=? AND brick=?",
                          (source, cfg_id, int(year), int(k), brick)).fetchone()
        return dict(r) if r else None

    def mark_continuous(self, row, prev_rowid):
        """Record that a chunk's start state was checked against its predecessor's end state."""
        with self.ws.conn() as c:
            c.execute("UPDATE chunks SET continuity='continuous', prev_rowid=? WHERE rowid=?", (prev_rowid, row["_rowid"]))

    def has_chunk(self, source, cfg_id, year, k, brick, need_end=None):
        r = self.chunk_row(source, cfg_id, year, k, brick)
        if r is None or not r.get("path") or not Path(r["path"]).exists():
            return False
        return need_end is None or _ts(r["t_end"]) >= pd.Timestamp(need_end)

    def _rows_overlapping(self, source, cfg_id, start, end):
        with self.ws.conn() as c:
            rows = c.execute("SELECT * FROM chunks WHERE source=? AND config_id=? AND t_end>=? AND t_start<=?",
                             (source, cfg_id, pd.Timestamp(start).isoformat(), pd.Timestamp(end).isoformat())).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _read_for_merge(path, times, data, tiers, state):
        """Copy everything saved in ``path`` into the new arrays (in place); returns its per-brick metadata."""
        import netCDF4
        with netCDF4.Dataset(path) as ds:
            dst = times.get_indexer(_nc_times(ds))
            ok = dst >= 0
            new = {}
            for v in list(data) + list(tiers):
                if v not in ds.variables:                  # file written before this variable existed
                    continue
                ds[v].set_auto_maskandscale(False)
                new[v] = np.asarray(ds[v][:])
            g = ds.groups.get("chain_state")
            st = {}
            if g is not None:
                for key in state:
                    if key in g.variables:
                        g[key].set_auto_maskandscale(False)
                        st[key] = np.asarray(g[key][:], dtype=np.float32)
            meta = json.loads(getattr(ds, "bricks_json", "{}"))
        for v, a in new.items():                           # only after the whole file read cleanly
            (data if v in data else tiers)[v][dst[ok]] = a[ok]
        state.update(st)
        return meta

    @staticmethod
    def _readable(path) -> bool:
        import netCDF4
        try:
            with NC_LOCK, netCDF4.Dataset(path) as ds:
                for v in SAVED + ["wx_tier"]:
                    if v in ds.variables:
                        ds[v].set_auto_maskandscale(False)
                        ds[v][-1]                               # touch the last chunk of each variable
                return "time" in ds.variables
        except Exception:
            return False

    def quarantine_file(self, path, reason) -> int:
        """Move a damaged results file aside and drop its catalog rows (those bricks are then computed again)."""
        path = Path(path)
        with NC_LOCK:
            self.ws.quarantine(path, "results", reason)
            with self.ws.conn() as c:
                n = c.execute("DELETE FROM chunks WHERE path=?", (str(path),)).rowcount
        return n

    # ------------------------------------------------------------------ writing
    def write_chunk(self, source, cfg, year, k, chunk_start, parts, job_id=None):
        """Write (or merge into) the file for one time chunk.

        parts: list of dicts {brick, times, point_ids, steps{FWI,FPI,ERC,BI,wx_tier,ndvi_tier},
                              start_state, end_state, continuity, seconds}"""
        import netCDF4

        if not parts:
            return None
        times = pd.DatetimeIndex(parts[0]["times"])
        for p in parts:
            if not pd.DatetimeIndex(p["times"]).equals(times):
                raise ValueError("all bricks written to one chunk file must share the same hours")
        path = self.path_for(source, cfg.id, year, k, chunk_start)
        path.parent.mkdir(parents=True, exist_ok=True)
        nt = len(times)
        data = {v: np.full((nt, self.ny, self.nx), FILL_I2, dtype=np.int16) for v in SAVED}
        tiers = {v: np.full((nt, self.ny, self.nx), FILL_U1, dtype=np.uint8) for v in ("wx_tier", "ndvi_tier")}
        state = {f"{w}_{key}": np.full((self.ny, self.nx), np.nan, dtype=np.float32) for w in ("start", "end") for key in STATE_KEYS}
        bricks_meta = {}
        with NC_LOCK:
            if path.exists():                                   # merge with bricks saved earlier
                merged = None
                for attempt in range(3):                        # a virus scanner may hold the file for a moment
                    try:
                        merged = self._read_for_merge(path, times, data, tiers, state)
                        break
                    except PermissionError:
                        if attempt == 2:
                            raise                               # never overwrite bricks we could not read
                        _time.sleep(1.0 + attempt)
                    except Exception as e:                      # damaged (power cut, bad copy): set aside
                        self.quarantine_file(path, f"unreadable while merging new bricks: {type(e).__name__}: {e}")
                        data = {v: np.full((nt, self.ny, self.nx), FILL_I2, dtype=np.int16) for v in SAVED}
                        tiers = {v: np.full((nt, self.ny, self.nx), FILL_U1, dtype=np.uint8) for v in ("wx_tier", "ndvi_tier")}
                        state = {f"{w}_{key}": np.full((self.ny, self.nx), np.nan, dtype=np.float32)
                                 for w in ("start", "end") for key in STATE_KEYS}
                        break
                if merged is not None:
                    bricks_meta = merged
            for p in parts:
                rows = self.grid.rows_for(p["point_ids"])
                yy, xx = self.y[rows], self.x[rows]
                for v in SAVED:
                    if v not in p["steps"]:
                        continue
                    a = np.asarray(p["steps"][v], dtype=np.float64) / PACK[v][0]
                    data[v][:, yy, xx] = np.where(np.isfinite(a), np.clip(np.round(a), -32767, 32767), FILL_I2).astype(np.int16)
                for v in tiers:
                    tiers[v][:, yy, xx] = np.asarray(p["steps"][v], dtype=np.uint8)
                for key in STATE_KEYS:
                    state[f"end_{key}"][yy, xx] = np.asarray(p["end_state"][key], dtype=np.float32)
                    state[f"start_{key}"][yy, xx] = np.asarray(p["start_state"][key], dtype=np.float32)
                bricks_meta[p["brick"]] = {"continuity": p["continuity"], "t_start": times[0].isoformat(),
                                           "t_end": times[-1].isoformat(), "computed": now_iso()}
            tmp = path.with_name(path.stem + f".{threading.get_ident()}.tmp")
            self._write_file(tmp, source, cfg, times, data, tiers, state, bricks_meta, year, k)
            fsync_file(tmp)
            _replace(tmp, path)
        cnt = lambda a: json.dumps({str(int(t)): int((a == t).sum()) for t in np.unique(a)})  # noqa: E731
        with self.ws.conn() as c:
            for p in parts:
                valid = {v: int(np.isfinite(np.asarray(p["steps"][v], dtype=float)).sum()) for v in STORED}
                c.execute("""INSERT OR REPLACE INTO chunks (source, config_id, year, k, brick, t_start, t_end, n_hours, n_cells,
                             valid_fwi, valid_fpi, valid_erc, valid_bi, wx_tiers, ndvi_tiers, continuity, seconds, created, job_id, path,
                             prev_rowid, ndvi_sig)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (source, cfg.id, int(year), int(k), p["brick"], times[0].isoformat(), times[-1].isoformat(), nt,
                           len(p["point_ids"]), valid["FWI"], valid["FPI"], valid["ERC"], valid["BI"],
                           cnt(np.asarray(p["steps"]["wx_tier"])), cnt(np.asarray(p["steps"]["ndvi_tier"])),
                           p["continuity"], float(p.get("seconds", 0.0)), now_iso(), job_id, str(path), p.get("prev_rowid"),
                           p.get("ndvi_sig")))
        return path

    def _grid_vars(self, ds, nt):
        ds.createDimension("time", nt)
        ds.createDimension("latitude", self.ny)
        ds.createDimension("longitude", self.nx)
        t = ds.createVariable("time", "i4", ("time",))
        t.units, t.calendar, t.standard_name, t.axis = TIME_UNITS, "standard", "time", "T"
        t.comment = "UTC. Temperature/humidity are instantaneous at the stamp; precipitation is the hour ending at it."
        la = ds.createVariable("latitude", "f8", ("latitude",))
        la.units, la.standard_name, la.axis = "degrees_north", "latitude", "Y"
        la[:] = self.lat
        lo = ds.createVariable("longitude", "f8", ("longitude",))
        lo.units, lo.standard_name, lo.axis = "degrees_east", "longitude", "X"
        lo[:] = self.lon
        crs = ds.createVariable("crs", "i4")
        crs.grid_mapping_name = "latitude_longitude"
        crs.semi_major_axis, crs.inverse_flattening = 6378137.0, 298.257223563
        return t

    def _packed_var(self, ds, v, nt, chunk_t=24):
        scale, ln, units, comment = PACK[v]
        var = ds.createVariable(v, "i2", ("time", "latitude", "longitude"), fill_value=FILL_I2,
                                chunksizes=(min(chunk_t, max(nt, 1)), min(64, self.ny), min(64, self.nx)), **COMP)
        var.scale_factor, var.add_offset = np.float32(scale), np.float32(0.0)
        var.long_name, var.units, var.comment, var.grid_mapping = ln, units, comment, "crs"
        var.set_auto_maskandscale(False)
        return var

    def _tier_var(self, ds, v, nt, chunk_t=24):
        ln = {"wx_tier": "weather input resolution tier (worst input): 0 native cell-hour, 1 time-interpolated <=3 h, "
                         "2 3x3 neighbourhood, 3 7x7 neighbourhood, 4 brick mean, 9 unavailable",
              "ndvi_tier": "NDVI resolution tier: 0 MOD13Q1 250 m, 1 MOD13A1 500 m, 2 MOD13A2 1 km, 3 AVHRR CDR ~5 km (opt-in), 4 day-of-year climatology (opt-in), 9 none"}[v]
        var = ds.createVariable(v, "u1", ("time", "latitude", "longitude"), fill_value=FILL_U1,
                                chunksizes=(min(chunk_t, max(nt, 1)), min(64, self.ny), min(64, self.nx)), **COMP)
        var.long_name, var.grid_mapping = ln, "crs"
        var.set_auto_maskandscale(False)
        return var

    def _static_vars(self, ds):
        pid = ds.createVariable("point_id", "i4", ("latitude", "longitude"), fill_value=np.int32(-1), **COMP)
        pid.long_name = "points_index.csv point_id of the study cell (-1 = outside the study area)"
        pid[:] = self._static["point_id"]
        cty = ds.createVariable("county", "i1", ("latitude", "longitude"), fill_value=np.int8(-1), **COMP)
        cty.long_name = "county"
        cty.flag_values = np.arange(len(COUNTIES), dtype=np.int8)
        cty.flag_meanings = " ".join(COUNTIES)
        cty[:] = self._static["county"]
        off = ds.createVariable("std_offset_h", "i1", ("latitude", "longitude"), **COMP)
        off.long_name = "local standard time offset from UTC (h): -6 Central (Gogebic, Iron), -5 Eastern"
        off[:] = self._static["std_offset_h"]

    def _global_attrs(self, ds, source, cfg):
        ds.Conventions = "CF-1.8"
        ds.title = "Hourly fire danger indices, Western Upper Peninsula of Michigan (AORC 800 m grid)"
        ds.source = ("SYNTHETIC demo weather - NOT REAL DATA" if source == "synthetic" else
                     "NOAA AORC v1.1 (s3://noaa-nws-aorc-v1-1-1km), computed with aorc_tools.explorer")
        ds.synthetic = int(source == "synthetic")
        ds.config_id = cfg.id
        ds.config_json = json.dumps(cfg.as_dict())
        ds.config_label = cfg.label()
        ds.packing = "FWI stored to 0.1, FPI/ERC/BI to 1 unit (int16 with scale_factor); NaN = not computed or outside the study area"
        ds.time_basis = "UTC; std_offset_h gives each cell's local standard time; DMC/DC daylight from per-cell sun position"
        ds.references = ("Van Wagner & Pickett 1985; cffdrs-ng (NRCan); Bradshaw et al. 1984; Burgan et al. 1998; "
                         "Fall et al. 2023 (AORC v1.1)")

    def _write_file(self, path, source, cfg, times, data, tiers, state, bricks_meta, year, k):
        import netCDF4

        nt = len(times)
        with netCDF4.Dataset(path, "w", format="NETCDF4") as ds:
            t = self._grid_vars(ds, nt)
            t[:] = ((times - T_EPOCH) / pd.Timedelta("1h")).astype(np.int64)
            for v in SAVED:
                self._packed_var(ds, v, nt)[:] = data[v]
            for v in tiers:
                self._tier_var(ds, v, nt)[:] = tiers[v]
            self._static_vars(ds)
            g = ds.createGroup("chain_state")
            g.description = ("Moisture-code / fuel-moisture state at the first (start_*) and last (end_*) hour of this chunk "
                             "for each computed cell. Used to continue the next chunk and to re-run any packet exactly.")
            g.createDimension("latitude", self.ny)
            g.createDimension("longitude", self.nx)
            for key, a in state.items():
                sv = g.createVariable(key, "f4", ("latitude", "longitude"), fill_value=np.float32(np.nan), **COMP)
                sv.set_auto_maskandscale(False)
                sv[:] = a
            self._global_attrs(ds, source, cfg)
            ds.year, ds.time_chunk = int(year), int(k)
            ds.bricks_json = json.dumps(bricks_meta)
            ds.history = f"{datetime.now().isoformat(timespec='seconds')} written by WUP Grid Explorer"

    # ------------------------------------------------------------------ reading
    def _read(self, path, names, group=None, window=None):
        """Raw arrays (packed ints / ubytes / float32 state) from one file, under the HDF5 lock.
        ``window=(y0, y1, x0, x1)`` reads only that lat/lon sub-rectangle (HDF5 decompresses only
        the chunks it touches), which makes small selections fast."""
        import netCDF4
        with NC_LOCK:
            with netCDF4.Dataset(path) as ds:
                src = ds.groups[group] if group else ds
                out = {}
                for n in names:
                    if n in src.variables:
                        v = src[n]
                        v.set_auto_maskandscale(False)
                        if window is not None and v.ndim == 3:
                            y0, y1, x0, x1 = window
                            out[n] = np.asarray(v[:, y0:y1, x0:x1])
                        else:
                            out[n] = np.asarray(v[:])
                if group is None:
                    out["_times"] = _nc_times(ds)
        return out

    @staticmethod
    def unpack(name, raw):
        raw = np.asarray(raw)
        if name in PACK:
            return np.where(raw == FILL_I2, np.nan, raw.astype(np.float32) * np.float32(PACK[name][0]))
        return raw

    def state_at(self, source, cfg_id, year, k, brick, which="end", point_ids=None):
        r = self.chunk_row(source, cfg_id, year, k, brick)
        if r is None or not Path(r["path"]).exists():
            return None
        raw = self._read(r["path"], [f"{which}_{key}" for key in STATE_KEYS], group="chain_state")
        if point_ids is None:
            point_ids = self.grid.cells_in_brick(*map(int, brick.split("_")))
        rows = self.grid.rows_for(point_ids)
        return {key: raw[f"{which}_{key}"][self.y[rows], self.x[rows]].astype(np.float64) for key in STATE_KEYS}

    def series(self, source, cfg_id, point_ids, start, end):
        """Stored FDIs + tiers for cells x [start, end] -> (times, steps (n_t, n_c), fraction stored, missing cells)."""
        pids = np.asarray(point_ids, dtype=int)
        times = pd.date_range(start, end, freq="h")
        out = {v: np.full((len(times), len(pids)), np.nan, dtype=np.float32) for v in SAVED}
        out["wx_tier"] = np.full((len(times), len(pids)), 9, dtype=np.uint8)
        out["ndvi_tier"] = np.full((len(times), len(pids)), 9, dtype=np.uint8)
        have = np.zeros((len(times), len(pids)), dtype=bool)
        rows = self.grid.rows_for(pids)
        yy, xx = self.y[rows], self.x[rows]
        sub = self.grid.points.iloc[rows]
        brick_of = np.array([f"{a}_{b}" for a, b in zip(sub.ci.values, sub.cj.values)])
        by_path: dict = {}
        for r in self._rows_overlapping(source, cfg_id, times[0], times[-1]):
            by_path.setdefault(r["path"], []).append(r["brick"])
        for path, bricks in by_path.items():
            if not Path(path).exists():
                continue
            cols = np.where(np.isin(brick_of, bricks))[0]
            if not len(cols):
                continue
            y0, y1, x0, x1 = int(yy[cols].min()), int(yy[cols].max()) + 1, int(xx[cols].min()), int(xx[cols].max()) + 1
            raw = self._read(path, SAVED + ["wx_tier", "ndvi_tier"], window=(y0, y1, x0, x1))
            ft = raw["_times"]
            tm = np.asarray((ft >= times[0]) & (ft <= times[-1]))
            if not tm.any():
                continue
            dst = times.get_indexer(ft[tm])
            for v in SAVED + ["wx_tier", "ndvi_tier"]:
                if v not in raw:
                    continue
                out[v][dst[:, None], cols[None, :]] = self.unpack(v, raw[v][tm][:, yy[cols] - y0, xx[cols] - x0])
            have[dst[:, None], cols[None, :]] = True
        return times, out, float(have.mean()) if have.size else 0.0, int((~have).any(axis=0).sum())

    def field(self, source, cfg_id, var, t):
        """One variable for every grid cell at hour ``t`` (NaN where not computed)."""
        import netCDF4
        t = pd.Timestamp(t)
        vals = np.full(self.grid.n_cells, np.nan, dtype=np.float32)
        n = 0
        for path in {r["path"] for r in self._rows_overlapping(source, cfg_id, t, t)}:
            if not Path(path).exists():
                continue
            with NC_LOCK:
                with netCDF4.Dataset(path) as ds:
                    i = _nc_times(ds).get_indexer([t])[0]
                    if i < 0:
                        continue
                    v = ds[var]
                    v.set_auto_maskandscale(False)
                    frame = np.asarray(v[i, :, :])
            g = self.unpack(var, frame[self.y, self.x]).astype(np.float32)
            if var in ("wx_tier", "ndvi_tier"):
                g = np.where(g == FILL_U1, np.nan, g)
            ok = np.isfinite(g)
            vals[ok] = g[ok]
            n += int(ok.sum())
        return vals, n

    def stored_times(self, source, cfg_id, year, month):
        start = pd.Timestamp(year, month, 1)
        end = start + pd.offsets.MonthEnd(0) + pd.Timedelta(hours=23)
        hours = set()
        for r in self._rows_overlapping(source, cfg_id, start, end):
            hours.update(pd.date_range(max(_ts(r["t_start"]), start), min(_ts(r["t_end"]), end), freq="h"))
        return sorted(hours)

    def files(self, source, cfg_id, year=None):
        q = "SELECT DISTINCT path, year, k, MIN(t_start) AS t_start, MAX(t_end) AS t_end FROM chunks WHERE source=? AND config_id=?"
        args = [source, cfg_id]
        if year is not None:
            q += " AND year=?"
            args.append(int(year))
        with self.ws.conn() as c:
            rows = [dict(r) for r in c.execute(q + " GROUP BY path ORDER BY t_start", args).fetchall()]
        out = []
        for r in rows:
            if r["path"] and Path(r["path"]).exists():
                r["bytes"] = Path(r["path"]).stat().st_size
                out.append(r)
        return out

    # ------------------------------------------------------------------ coverage
    def coverage(self, source, cfg_id, domain_start, domain_end, allow_ndvi_climatology=False):
        """Coverage summary, cached until the catalog for this source/configuration changes."""
        with self.ws.conn() as c:
            sig = tuple(c.execute("SELECT COUNT(*), MAX(rowid), SUM(continuity='continuous'), SUM(valid_fpi) FROM chunks "
                                  "WHERE source=? AND config_id=?", (source, cfg_id)).fetchone())
        key = (source, cfg_id, str(domain_start), str(domain_end), bool(allow_ndvi_climatology), sig)
        cache = self.__dict__.setdefault("_cov_cache", {})
        if key in cache:
            return {**cache[key], "seconds": 0.0, "cached": True}
        out = self._coverage(source, cfg_id, domain_start, domain_end, allow_ndvi_climatology)
        cache.clear()
        cache[key] = out
        return out

    def _coverage(self, source, cfg_id, domain_start, domain_end, allow_ndvi_climatology=False):
        t0 = _time.perf_counter()
        ds, de = _ts(domain_start), _ts(domain_end)
        with self.ws.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM chunks WHERE source=? AND config_id=?", (source, cfg_id)).fetchall()]
        n_cells = self.grid.n_cells
        dom_hours = int((de - ds) / pd.Timedelta("1h")) + 1
        fpi_start = ds if allow_ndvi_climatology else max(ds, MODIS_START)
        fpi_hours = max(0, int((de - fpi_start) / pd.Timedelta("1h")) + 1)
        totals = {"FWI": n_cells * dom_hours, "ERC": n_cells * dom_hours, "BI": n_cells * dom_hours, "FPI": n_cells * fpi_hours}
        computed = computed_fpi = 0
        valid = {v: 0 for v in STORED}
        per_brick = {k: 0 for k in self.brick_cells}
        years = list(range(ds.year, de.year + 1))
        ym = {y: np.zeros(12) for y in years}
        wx, nd, cont = {}, {}, {}
        for r in rows:
            a, b = max(_ts(r["t_start"]), ds), min(_ts(r["t_end"]), de)
            if b < a:
                continue
            hrs = int((b - a) / pd.Timedelta("1h")) + 1
            computed += hrs * r["n_cells"]
            fa = max(a, fpi_start)
            if b >= fa:
                computed_fpi += (int((b - fa) / pd.Timedelta("1h")) + 1) * r["n_cells"]
            per_brick[r["brick"]] = per_brick.get(r["brick"], 0) + hrs * r["n_cells"]
            frac = hrs / max(1, r["n_hours"])
            for v, col in (("FWI", "valid_fwi"), ("FPI", "valid_fpi"), ("ERC", "valid_erc"), ("BI", "valid_bi")):
                valid[v] += int(round((r[col] or 0) * frac))
            for (y, m), n in _month_hours(a, b).items():
                if y in ym:
                    ym[y][m - 1] += n * r["n_cells"]
            for src, dst in ((r["wx_tiers"], wx), (r["ndvi_tiers"], nd)):
                for t, n in json.loads(src or "{}").items():
                    dst[t] = dst.get(t, 0) + int(n * frac)
            key = "continuous" if r["continuity"] == "continuous" else ("spin-up" if str(r["continuity"]).startswith("spinup") else "cold start")
            cont[key] = cont.get(key, 0) + 1
        dom = {y: np.zeros(12) for y in years}
        for (y, m), n in _month_hours(ds, de).items():
            dom[y][m - 1] = n * n_cells
        month_pct = {str(y): [round(100 * float(ym[y][m]) / dom[y][m], 3) if dom[y][m] else None for m in range(12)] for y in years}
        year_pct = {str(y): round(100 * float(ym[y].sum()) / float(dom[y].sum()), 3) if dom[y].sum() else None for y in years}
        brick_pct = {k: round(100 * per_brick.get(k, 0) / (self.brick_cells[k] * dom_hours), 3) for k in self.brick_cells}
        pct = {v: round(100 * (computed if v != "FPI" else computed_fpi) / totals[v], 4) if totals[v] else 0.0 for v in STORED}
        files = self.files(source, cfg_id)
        disk = sum(f["bytes"] for f in files)
        return {
            "source": source, "config_id": cfg_id,
            "domain": {"start": ds.isoformat(), "end": de.isoformat(), "hours": dom_hours, "cells": n_cells,
                       "packets": n_cells * dom_hours, "fpi_start": fpi_start.isoformat()},
            "packets_computed": int(computed), "percent_computed": round(100 * computed / (n_cells * dom_hours), 4),
            "per_fdi": {v: {"total": int(totals[v]), "valid": int(valid[v]),
                            "percent_valid": round(100 * valid[v] / totals[v], 4) if totals[v] else 0.0,
                            "percent_computed": pct[v]} for v in STORED},
            "per_year": year_pct, "per_month": month_pct, "per_brick": brick_pct,
            "wx_tiers": wx, "ndvi_tiers": nd, "continuity": cont, "n_chunks": len(rows),
            "files": {"n": len(files), "bytes": disk, "bytes_per_packet": round(disk / computed, 3) if computed else None,
                      "dir": str(self.ws.results_dir / source / cfg_id),
                      "full_record_estimate_bytes": int(disk / computed * n_cells * dom_hours) if computed else None},
            "seconds": round(_time.perf_counter() - t0, 3),
        }

    # ------------------------------------------------------------------ percentiles from stored data
    def build_climatology(self, source, cfg_id, out_path, stride=4, since=None):
        """Percentiles of every stored value (pooled over cells and hours). The values are packed int16, so a
        65,536-bin histogram per index gives them exactly, in constant memory, however long the record is."""
        counts = {v: np.zeros(65536, dtype=np.int64) for v in STORED}
        months = set()
        for f in self.files(source, cfg_id):
            if since and int(f["year"]) < since:
                continue
            try:
                raw = self._read(f["path"], STORED)
            except Exception as e:                         # damaged file: leave it out, say so in the log
                logger.warning("climatology: skipping unreadable %s (%s)", f["path"], e)
                continue
            for v in STORED:
                if v not in raw:
                    continue
                a = np.asarray(raw[v][::stride]).ravel()
                a = a[a != FILL_I2].astype(np.int64) + 32768
                if a.size:
                    counts[v] += np.bincount(a, minlength=65536)
            for t in raw["_times"][::24]:
                months.add(f"{t.year}-{t.month:02d}")
        out = {"method": f"pooled-regional, whole-season, every {stride}th stored hour", "built_at": now_iso(),
               "source": source, "config_id": cfg_id, "source_months": sorted(months), "vars": {}}
        for v in STORED:
            n = int(counts[v].sum())
            if not n:
                continue
            cum = np.cumsum(counts[v])
            values = (np.arange(65536) - 32768) * PACK[v][0]
            q = []
            for p in range(101):                            # same convention as np.percentile (linear)
                pos = p / 100.0 * (n - 1)
                lo_i, hi_i = int(np.floor(pos)), int(np.ceil(pos))
                vlo = values[np.searchsorted(cum, lo_i + 1)]
                vhi = values[np.searchsorted(cum, hi_i + 1)]
                q.append(float(vlo + (vhi - vlo) * (pos - lo_i)))
            out["vars"][v] = {"q": q, "n": n}
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + ".tmp")
        tmp.write_text(json.dumps(out))
        replace_file(tmp, out_path)
        return out

    # ------------------------------------------------------------------ daily maxima (local standard days)
    def year_signature(self, source, cfg_id, year, k=None) -> str:
        """Changes whenever a chunk of that year (or only chunk ``k``) is added, recomputed or marked continuous."""
        q = "SELECT COUNT(*), MAX(rowid), SUM(continuity='continuous'), MAX(t_end) FROM chunks WHERE source=? AND config_id=? AND year=?"
        args = [source, cfg_id, int(year)]
        if k is not None:
            q += " AND k=?"
            args.append(int(k))
        with self.ws.conn() as c:
            r = c.execute(q, args).fetchone()
        return "|".join(str(x) for x in tuple(r))

    def daily_path(self, source, cfg_id, year) -> Path:
        return self.ws.root / "daily" / source / cfg_id / f"WUP_FDI_daily_{year}_{source}_{cfg_id}.nc"

    def daily_year(self, source, cfg_id, year, force=False) -> Path | None:
        """WUP_FDI_daily_<year>_….nc in <workspace>/daily (daily maxima only, no hourly merge). Rebuilt only when that
        year's saved chunks changed. None when nothing is saved for the year."""
        import netCDF4
        from aorc_tools.explorer.chain import ChainConfig
        files = self.files(source, cfg_id, year)
        if not files:
            return None
        nxt = [f for f in self.files(source, cfg_id, year + 1) if int(f.get("k", 99)) == 0]   # completes Dec 31
        path = self.daily_path(source, cfg_id, year)
        sig = self.year_signature(source, cfg_id, year) + "|" + self.year_signature(source, cfg_id, year + 1, k=0)
        if path.exists() and not force:
            try:
                with NC_LOCK, netCDF4.Dataset(path) as ds:
                    if getattr(ds, "catalog_sig", None) == sig and int(getattr(ds, "daily_format", 1)) == DAILY_FORMAT:
                        return path
            except Exception as e:                                   # damaged: rebuild it
                logger.warning("daily file %s unreadable (%s); rebuilding", path, e)
        with NC_LOCK:
            with netCDF4.Dataset(files[0]["path"]) as src0:
                cfg = ChainConfig.from_dict(json.loads(src0.config_json))
        nd, dmax, dhr = self._daily_arrays(files + nxt, year)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_daily(path, year, nd, dmax, dhr, source, cfg, catalog_sig=sig)
        return path

    def _daily_arrays(self, files, year):
        """Each index's maximum and minimum over each cell's local standard day (FWI_daily: its 12:00 LST value), the
        local hours of the maximum and the minimum (the middle of tied hours), and hourly FWI at 12:00 LST.
        Days with fewer than MIN_DAY_HOURS saved hours (gaps in the record, the end of the data) are left out (NaN)
        rather than summarised from part of the day. Pass the next year's first chunk file(s) too, so Dec 31 (which
        ends at 05-06 UTC on Jan 1) is complete."""
        off = self.grid.points.std_offset_h.values.astype(int)
        d0 = pd.Timestamp(year, 1, 1)
        nd = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
        n_c = self.grid.n_cells
        dmax = {v: np.full((nd, n_c), np.nan, dtype=np.float32) for v in SAVED}
        dhr = {v: np.full((nd, n_c), -1, dtype=np.int8) for v in SAVED}
        dmin = {v: np.full((nd, n_c), np.nan, dtype=np.float32) for v in SAVED if v != "FWI_daily"}
        dhr_min = {v: np.full((nd, n_c), -1, dtype=np.int8) for v in dmin}
        nh = {v: np.zeros((nd, n_c), dtype=np.int8) for v in dmin}             # saved hours per local day
        noon = np.full((nd, n_c), np.nan, dtype=np.float32)                     # hourly FWI at 12:00 LST
        seen = set()
        for f in files:
            if f["path"] in seen:
                continue
            seen.add(f["path"])
            raw = self._read(f["path"], SAVED)
            hrs = ((raw["_times"] - d0) / pd.Timedelta("1h")).astype(np.int64).values
            for v in SAVED:
                if v not in raw:
                    continue
                a = self.unpack(v, raw[v][:, self.y, self.x])                    # (n_t, n_cells)
                for o in np.unique(off):
                    cm = np.where(off == o)[0]
                    lh = hrs + o
                    day, hod = lh // 24, lh % 24
                    if v in ("FWI_daily", "FWI"):                               # values at 12:00 LST
                        sel = np.where((hod == 12) & (day >= 0) & (day < nd))[0]
                        if len(sel):
                            tgt = dmax[v] if v == "FWI_daily" else noon
                            tgt[np.ix_(day[sel], cm)] = a[np.ix_(sel, cm)]
                            if v == "FWI_daily":
                                dhr[v][np.ix_(day[sel], cm)] = 12
                        if v == "FWI_daily":
                            continue
                    for di in np.unique(day):
                        if di < 0 or di >= nd:
                            continue
                        tm = np.where(day == di)[0]
                        blk = a[np.ix_(tm, cm)]
                        fin = np.isfinite(blk)
                        if not fin.any():
                            continue
                        nh[v][di, cm] += fin.sum(axis=0).astype(np.int8)
                        h = hod[tm].astype(float)[:, None]
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", RuntimeWarning)
                            mx = np.nanmax(blk, axis=0)
                            mn = np.nanmin(blk, axis=0)
                            hmx = np.rint(np.nansum(np.where(fin & (blk == mx), h, np.nan), axis=0)
                                          / np.maximum(1, (fin & (blk == mx)).sum(axis=0)))
                            hmn = np.rint(np.nansum(np.where(fin & (blk == mn), h, np.nan), axis=0)
                                          / np.maximum(1, (fin & (blk == mn)).sum(axis=0)))
                        cur = dmax[v][di, cm]
                        better = np.isfinite(mx) & (~np.isfinite(cur) | (mx > cur))
                        if better.any():
                            dmax[v][di, cm[better]] = mx[better]
                            dhr[v][di, cm[better]] = hmx[better].astype(np.int8)
                        curm = dmin[v][di, cm]
                        lower = np.isfinite(mn) & (~np.isfinite(curm) | (mn < curm))
                        if lower.any():
                            dmin[v][di, cm[lower]] = mn[lower]
                            dhr_min[v][di, cm[lower]] = hmn[lower].astype(np.int8)
        for v in dmin:                                          # part-days are not days
            short = nh[v] < MIN_DAY_HOURS
            dmax[v][short] = np.nan
            dmin[v][short] = np.nan
            dhr[v][short] = -1
            dhr_min[v][short] = -1
        dmax["_min"] = dmin                                     # carried along with the maxima
        dmax["_noon"] = noon
        dhr["_min"] = dhr_min
        return nd, dmax, dhr

    def _write_daily(self, daily, year, nd, dmax, dhr, source, cfg, catalog_sig=None):
        import netCDF4
        tmp = daily.with_name(daily.stem + ".tmp")
        with NC_LOCK:
            out = netCDF4.Dataset(tmp, "w", format="NETCDF4")
        try:
            with NC_LOCK:
                out.createDimension("time", nd)
                out.createDimension("latitude", self.ny)
                out.createDimension("longitude", self.nx)
                t = out.createVariable("time", "i4", ("time",))
                t.units, t.calendar, t.axis = f"days since {year}-01-01", "standard", "T"
                t.long_name = "local standard date (each cell's own time zone)"
                t[:] = np.arange(nd)
                la = out.createVariable("latitude", "f8", ("latitude",)); la.units, la.standard_name = "degrees_north", "latitude"; la[:] = self.lat
                lo = out.createVariable("longitude", "f8", ("longitude",)); lo.units, lo.standard_name = "degrees_east", "longitude"; lo[:] = self.lon
            dmin = dmax.get("_min", {})
            if "_noon" in dmax:
                s_, ln_, units_, _ = PACK["FWI"]
                g = np.full((nd, self.ny, self.nx), FILL_I2, dtype=np.int16)
                g[:, self.y, self.x] = np.where(np.isfinite(dmax["_noon"]), np.round(dmax["_noon"] / s_), FILL_I2).astype(np.int16)
                with NC_LOCK:
                    var = out.createVariable("FWI_noon", "i2", ("time", "latitude", "longitude"), fill_value=FILL_I2,
                                             chunksizes=(31, 64, 64), **COMP)
                    var.scale_factor, var.add_offset = np.float32(s_), np.float32(0)
                    var.long_name, var.units = f"{ln_} at 12:00 LST (hourly system, for comparison with FWI_daily_noon)", units_
                    var.set_auto_maskandscale(False)
                    var[:] = g
                del g
            for v in SAVED:                                          # one variable per lock hold (see export_year)
                s, ln, units, _ = PACK[v]
                if v in dmin:
                    g = np.full((nd, self.ny, self.nx), FILL_I2, dtype=np.int16)
                    g[:, self.y, self.x] = np.where(np.isfinite(dmin[v]), np.round(dmin[v] / s), FILL_I2).astype(np.int16)
                    with NC_LOCK:
                        var = out.createVariable(f"{v}_daily_min", "i2", ("time", "latitude", "longitude"), fill_value=FILL_I2,
                                                 chunksizes=(31, 64, 64), **COMP)
                        var.scale_factor, var.add_offset = np.float32(s), np.float32(0)
                        var.long_name, var.units = f"daily minimum of {ln} over the local standard day (overnight recovery)", units
                        var.set_auto_maskandscale(False)
                        var[:] = g
                    del g
                    gh = np.full((nd, self.ny, self.nx), -1, dtype=np.int8)
                    gh[:, self.y, self.x] = dhr["_min"][v]
                    with NC_LOCK:
                        hv = out.createVariable(f"{v}_hour_of_min", "i1", ("time", "latitude", "longitude"), fill_value=np.int8(-1),
                                                chunksizes=(31, 64, 64), **COMP)
                        hv.long_name = f"local standard hour (0-23) when {v} was lowest"
                        hv.set_auto_maskandscale(False)
                        hv[:] = gh
                    del gh
                vname = "FWI_daily_noon" if v == "FWI_daily" else f"{v}_daily_max"
                g = np.full((nd, self.ny, self.nx), FILL_I2, dtype=np.int16)
                g[:, self.y, self.x] = np.where(np.isfinite(dmax[v]), np.round(dmax[v] / s), FILL_I2).astype(np.int16)
                with NC_LOCK:
                    var = out.createVariable(vname, "i2", ("time", "latitude", "longitude"), fill_value=FILL_I2,
                                             chunksizes=(31, 64, 64), **COMP)
                    var.scale_factor, var.add_offset = np.float32(s), np.float32(0)
                    var.long_name, var.units = (f"{ln} (value at the 12:00 LST observation)" if v == "FWI_daily"
                                                else f"daily maximum of {ln} over the local standard day"), units
                    var.set_auto_maskandscale(False)
                    var[:] = g
                del g
                if v == "FWI_daily":
                    continue
                gh = np.full((nd, self.ny, self.nx), -1, dtype=np.int8)
                gh[:, self.y, self.x] = dhr[v]
                with NC_LOCK:
                    hv = out.createVariable(f"{v}_hour_of_max", "i1", ("time", "latitude", "longitude"), fill_value=np.int8(-1),
                                            chunksizes=(31, 64, 64), **COMP)
                    hv.long_name = f"local standard hour (0-23) when {v} peaked"
                    hv.set_auto_maskandscale(False)
                    hv[:] = gh
                del gh
            with NC_LOCK:
                self._static_vars(out)
                self._global_attrs(out, source, cfg)
                out.title = "Daily maximum fire danger indices (local standard day), Western Upper Peninsula of Michigan"
                out.history = f"{datetime.now().isoformat(timespec='seconds')} daily maxima from hourly chunk files"
                out.daily_format = DAILY_FORMAT
                if catalog_sig is not None:
                    out.catalog_sig = catalog_sig
        except BaseException:
            with NC_LOCK:
                try:
                    out.close()
                except Exception:                                    # noqa: BLE001
                    pass
            tmp.unlink(missing_ok=True)
            raise
        with NC_LOCK:
            out.close()
        fsync_file(tmp)
        _replace(tmp, daily)

    # ------------------------------------------------------------------ one-file-per-year exports
    def export_year(self, source, cfg_id, year, out_dir: Path):
        """Merge a year's chunk files into WUP_FDI_hourly_<year>_….nc and build WUP_FDI_daily_<year>_….nc
        (each index's daily maximum over each cell's local standard day, plus the local hour it occurred)."""
        import netCDF4
        from aorc_tools.explorer.chain import ChainConfig

        files = self.files(source, cfg_id, year)
        if not files:
            raise ValueError(f"nothing saved for {year}")
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = f"{year}_{source}_{cfg_id}"
        hourly = out_dir / f"WUP_FDI_hourly_{tag}.nc"
        daily = out_dir / f"WUP_FDI_daily_{tag}.nc"
        # The HDF5 lock is taken per (file, variable) step, not for the whole merge, so a running job's workers
        # are held up for a second at a time rather than for the minutes a whole-year merge takes.
        with NC_LOCK:
            with netCDF4.Dataset(files[0]["path"]) as src0:
                cfg = ChainConfig.from_dict(json.loads(src0.config_json))
            n_total = 0
            for f in files:
                with netCDF4.Dataset(f["path"]) as src:
                    n_total += src.dimensions["time"].size
        tmp = hourly.with_name(hourly.stem + ".tmp")
        with NC_LOCK:
            out = netCDF4.Dataset(tmp, "w", format="NETCDF4")
        try:
            with NC_LOCK:
                t = self._grid_vars(out, n_total)
                vs = {v: self._packed_var(out, v, n_total) for v in SAVED}
                vs.update({v: self._tier_var(out, v, n_total) for v in ("wx_tier", "ndvi_tier")})
                self._static_vars(out)
                self._global_attrs(out, source, cfg)
                out.year = int(year)
                out.history = f"{datetime.now().isoformat(timespec='seconds')} merged from {len(files)} chunk files"
            n = 0
            for f in files:
                with NC_LOCK:
                    src = netCDF4.Dataset(f["path"])
                try:
                    with NC_LOCK:
                        m = src.dimensions["time"].size
                        t[n:n + m] = src["time"][:]
                        names = [v for v in vs if v in src.variables]
                    for v in names:
                        with NC_LOCK:
                            src[v].set_auto_maskandscale(False)
                            vs[v][n:n + m, :, :] = src[v][:]
                finally:
                    with NC_LOCK:
                        src.close()
                n += m
        except BaseException:
            with NC_LOCK:
                try:
                    out.close()
                except Exception:                                    # noqa: BLE001
                    pass
            tmp.unlink(missing_ok=True)
            raise
        with NC_LOCK:
            out.close()
        fsync_file(tmp)
        _replace(tmp, hourly)

        nxt = [f for f in self.files(source, cfg_id, year + 1) if int(f.get("k", 99)) == 0]
        nd, dmax, dhr = self._daily_arrays(files + nxt, year)
        self._write_daily(daily, year, nd, dmax, dhr, source, cfg)
        return {"hourly": str(hourly), "daily": str(daily), "hourly_bytes": hourly.stat().st_size,
                "daily_bytes": daily.stat().st_size, "chunk_files": len(files)}
