"""
The explorer's workspace: one folder that holds everything the tool produces,
so a session can be closed and picked up again (or copied to another machine).

    <workspace>/
        workspace.db        SQLite: computed-chunk catalog, jobs, saved selections, sessions, exports
        settings.json       domain end, source, worker count, auto-resume, ...
        cache/bricks/       raw AORC brick cache (LRU, size-capped)
        results/<source>/<config_id>/<year>/<brick>/k###.npz   stored indices + chunk start/end state
        climatology/        percentile reference files
        exports/            snapshots, NetCDF, CSV written from the page
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    source TEXT, config_id TEXT, year INTEGER, k INTEGER, brick TEXT,
    t_start TEXT, t_end TEXT, n_hours INTEGER, n_cells INTEGER,
    valid_fwi INTEGER, valid_fpi INTEGER, valid_erc INTEGER, valid_bi INTEGER,
    wx_tiers TEXT, ndvi_tiers TEXT, continuity TEXT, seconds REAL, created TEXT, job_id INTEGER, path TEXT,
    PRIMARY KEY (source, config_id, year, k, brick));
CREATE INDEX IF NOT EXISTS chunks_time ON chunks (source, config_id, t_start, t_end);
CREATE TABLE IF NOT EXISTS configs (
    source TEXT, config_id TEXT, config TEXT, label TEXT, created TEXT, PRIMARY KEY (source, config_id));
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created TEXT, updated TEXT, source TEXT, config_id TEXT,
    config TEXT, bricks TEXT, start TEXT, "end" TEXT, spinup_hours INTEGER, status TEXT,
    tasks_total INTEGER, tasks_done INTEGER, packets_total INTEGER, packets_done INTEGER,
    error TEXT, seconds REAL, note TEXT);
CREATE TABLE IF NOT EXISTS selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, created TEXT, updated TEXT, kind TEXT,
    n_cells INTEGER, payload TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, created TEXT, updated TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT, path TEXT, bytes INTEGER, created TEXT, note TEXT);
"""

DEFAULT_SETTINGS = {
    "domain_start": "1979-02-01T00",
    "domain_end": "2026-09-30T23",
    "workers": 2,
    "auto_resume": True,
    "job_spinup_hours": 720,
    "cache_max_gb": 8.0,
    "source": None,
}


RESULTS_README = """WUP Grid Explorer - saved results
================================

results/<source>/<config_id>/<year>/WUP_FDI_<year>_k<NNN>_<YYYYMMDD>.nc

  One NetCDF-4 file per 144-hour chunk (the AORC download unit), covering the whole
  Western Upper Peninsula on the native AORC grid (277 x 324 cells, 1/120 degree).
  Variables (time, latitude, longitude):
      FWI   Fire Weather Index (hourly, stored to 0.1)
      FPI   Fire Potential Index (0-100, stored to 1)
      ERC   Energy Release Component (derived NFDRS, stored to 1; report as percentile)
      BI    Burning Index (derived NFDRS, stored to 1; report as percentile)
      wx_tier / ndvi_tier   which rung of the resolution ladder each value used
  plus point_id, county and std_offset_h maps, and a chain_state group (internal).
  Values are int16 with scale_factor and zlib compression: readers decode them to
  floating point automatically; NaN = not computed or outside the study area.
  config.json in each <config_id> folder says exactly how the indices were computed.
  source "synthetic" = demo weather, NOT real data.

Open them
  Python   import xarray as xr
           ds = xr.open_mfdataset("results/aorc-s3/<config_id>/2009/*.nc")   # needs: pip install dask
           # or one exported year file: xr.open_dataset("exports/...WUP_FDI_hourly_2009_....nc")
           ds.FWI.sel(time="2009-05-20T20").plot()                 # map
           ds.FWI.sel(latitude=46.68, longitude=-88.45, method="nearest").plot()   # series
  Panoply  File > Open > any .nc file > FWI > Create Plot (step through time)
  QGIS     Layer > Add Raster Layer > the .nc file > choose FWI; use the Temporal Controller

One file per year: in the explorer, Coverage tab > "Export year" (or the API
/api/export/year) writes WUP_FDI_hourly_<year>_... .nc and a much smaller
WUP_FDI_daily_<year>_... .nc (daily maximum of each index on each cell's local
standard day, and the hour it happened) into exports/.
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_name(s: str, default="item") -> str:
    s = re.sub(r"[^A-Za-z0-9._ -]+", "_", (s or "").strip())[:80].strip()
    return s or default


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        for d in ("cache/bricks", "results", "climatology", "exports"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "workspace.db"
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(chunks)").fetchall()}
            if "path" not in cols:                       # workspaces created by an earlier build
                c.execute("ALTER TABLE chunks ADD COLUMN path TEXT")
        self.settings = self._load_settings()
        readme = self.root / "results" / "README.txt"
        if not readme.exists():
            readme.write_text(RESULTS_README)

    # ------------------------------------------------------------------
    def conn(self):
        c = sqlite3.connect(self.db_path, timeout=60, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _load_settings(self):
        p = self.root / "settings.json"
        s = dict(DEFAULT_SETTINGS)
        if p.exists():
            try:
                s.update(json.loads(p.read_text()))
            except Exception:
                pass
        return s

    def save_settings(self, **kw):
        with self._lock:
            self.settings.update({k: v for k, v in kw.items() if k in DEFAULT_SETTINGS or k in self.settings})
            tmp = self.root / "settings.json.tmp"
            tmp.write_text(json.dumps(self.settings, indent=2))
            tmp.replace(self.root / "settings.json")
        return self.settings

    @property
    def brick_cache_dir(self):
        return self.root / "cache" / "bricks"

    @property
    def results_dir(self):
        return self.root / "results"

    # ------------------------------------------------------------------ selections
    def list_selections(self):
        with self.conn() as c:
            rows = c.execute("SELECT id, name, created, updated, kind, n_cells, notes FROM selections ORDER BY updated DESC").fetchall()
        return [dict(r) for r in rows]

    def get_selection(self, sid):
        with self.conn() as c:
            r = c.execute("SELECT * FROM selections WHERE id=?", (int(sid),)).fetchone()
        if not r:
            return None
        d = dict(r); d["payload"] = json.loads(d["payload"])
        return d

    def save_selection(self, name, payload: dict, notes=""):
        name = safe_name(name, "selection")
        n = len(payload.get("point_ids", [])) if isinstance(payload, dict) else 0
        t = now_iso()
        with self.conn() as c:
            c.execute("""INSERT INTO selections (name, created, updated, kind, n_cells, payload, notes) VALUES (?,?,?,?,?,?,?)
                         ON CONFLICT(name) DO UPDATE SET updated=excluded.updated, kind=excluded.kind,
                         n_cells=excluded.n_cells, payload=excluded.payload, notes=excluded.notes""",
                      (name, t, t, payload.get("mode", "cells"), n, json.dumps(payload), notes))
            r = c.execute("SELECT id FROM selections WHERE name=?", (name,)).fetchone()
        return int(r["id"])

    def delete_selection(self, sid):
        with self.conn() as c:
            c.execute("DELETE FROM selections WHERE id=?", (int(sid),))

    # ------------------------------------------------------------------ sessions
    def list_sessions(self):
        with self.conn() as c:
            rows = c.execute("SELECT id, name, created, updated FROM sessions ORDER BY updated DESC").fetchall()
        return [dict(r) for r in rows]

    def get_session(self, sid):
        with self.conn() as c:
            r = c.execute("SELECT * FROM sessions WHERE id=?", (int(sid),)).fetchone()
        if not r:
            return None
        d = dict(r); d["state"] = json.loads(d["state"])
        return d

    def save_session(self, name, state: dict):
        name = safe_name(name, "session")
        t = now_iso()
        with self.conn() as c:
            c.execute("""INSERT INTO sessions (name, created, updated, state) VALUES (?,?,?,?)
                         ON CONFLICT(name) DO UPDATE SET updated=excluded.updated, state=excluded.state""",
                      (name, t, t, json.dumps(state)))
            r = c.execute("SELECT id FROM sessions WHERE name=?", (name,)).fetchone()
        return int(r["id"])

    def delete_session(self, sid):
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE id=?", (int(sid),))

    # ------------------------------------------------------------------ exports
    def save_export(self, name, data: bytes, kind="file", note=""):
        name = safe_name(name, "export")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.root / "exports" / f"{stamp}_{name}"
        path.write_bytes(data)
        with self.conn() as c:
            cur = c.execute("INSERT INTO exports (name, kind, path, bytes, created, note) VALUES (?,?,?,?,?,?)",
                            (path.name, kind, str(path), len(data), now_iso(), note))
            eid = cur.lastrowid
        return eid, path

    def register_export(self, path, kind="file", note=""):
        path = Path(path)
        with self.conn() as c:
            c.execute("DELETE FROM exports WHERE path=?", (str(path),))
            cur = c.execute("INSERT INTO exports (name, kind, path, bytes, created, note) VALUES (?,?,?,?,?,?)",
                            (path.name, kind, str(path), path.stat().st_size, now_iso(), note))
            return cur.lastrowid

    def list_exports(self):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM exports ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows if Path(r["path"]).exists()]

    def export_path(self, eid):
        with self.conn() as c:
            r = c.execute("SELECT path FROM exports WHERE id=?", (int(eid),)).fetchone()
        return Path(r["path"]) if r else None
