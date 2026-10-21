"""
Flask API + page host for the WUP Grid Explorer.

Start it with ``python -m aorc_tools.explorer`` (or ``aorc-tools serve``, or the
WUP_Explorer launcher). Everything it writes goes into one workspace folder.

Main endpoints (JSON unless noted)
    GET  /                       the app (static/index.html)
    GET  /api/status             source, workspace, settings, coverage headline, jobs
    GET  /api/grid | /api/points.csv | /api/geo/<name> | /api/catalog | /api/presets
    POST /api/select             selection -> cells, bricks, packets, fetch size, stored coverage
    POST /api/series             chart series for cells x window   (mode: auto | store | live | batch)
    POST /api/trace              every step for one cell x window (+ comparators)
    GET  /api/timebase           sunrise/sunset & hour-by-hour clocks for one cell and date
    GET  /api/field              one index for all 28 k cells at one hour (store or batch record)
    GET  /api/coverage           percentage calculated, by FDI / year / month / brick / tier
    *    /api/jobs[...]          background calculation jobs
    *    /api/selections, /api/sessions, /api/exports   saving and sharing
    POST /api/export/netcdf      cells x window as NetCDF (also saved in the workspace)
    POST /api/climatology/build  percentile reference from stored data
    *    /api/fires[...]          known-fire catalog, import, checks, season jobs, reports
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from aorc_tools import __version__ as PKG_VERSION
from aorc_tools.config import NFDRS_FUEL_MODELS
from aorc_tools.explorer import timebase
from aorc_tools.explorer.chain import FDIS, PRESETS, ChainConfig, run_chain
from aorc_tools.explorer.climatology import Climatology
from aorc_tools.explorer.extracted import ExtractedStore
from aorc_tools.explorer import fires as firemod
from aorc_tools.explorer.inputs import InputStore
from aorc_tools.explorer.datasets import PRESET_HINTS, DatasetRegistry, DatasetSource
from aorc_tools.explorer.tasks import TaskRunner
from aorc_tools.explorer.geo import load_geojson
from aorc_tools.explorer.grid import Brick, Grid, load_grid
from aorc_tools.explorer.jobs import JobRunner
from aorc_tools.explorer.ndvi import FuelMap, NDVIProvider, SyntheticNDVI
from aorc_tools.explorer.pipeline import catalog_payload, series_payload, trace_payload
from aorc_tools.explorer.sources import AORCSource, OfflineMissing, SyntheticSource
from aorc_tools.explorer.store import NC_LOCK, PACK, SAVED, STORED, ResultsStore
from aorc_tools.explorer.workspace import Workspace

logger = logging.getLogger(__name__)
LIVE_MAX_PACKETS = 2_500_000      # cells x hours per live calculation (~750 B each in trace mode)
STATIC = Path(__file__).resolve().parent / "static"
SOURCES = {"synthetic": "Synthetic demo weather", "aorc-s3": "NOAA AORC v1.1 on S3"}


def _t(s) -> datetime:
    s = str(s).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable time {s!r} (use YYYY-MM-DDTHH)")


class Context:
    """Live objects shared by the API and the job runner (the source can be switched at runtime)."""

    def __init__(self, ws: Workspace, grid: Grid, static_dir: Path, active_source: str, loader=None):
        self.ws, self.grid, self.static_dir = ws, grid, Path(static_dir)
        self._sources = {}
        self._lock = threading.Lock()
        self.active = active_source
        self.loader = loader
        self.fuel_map = FuelMap(static_dir)
        self._clims = {}
        self.reachable = {}
        self.inputs = InputStore(ws, grid)
        self.offline = bool(ws.settings.get("offline", False))
        self.datasets = DatasetRegistry(ws)

    def sources_available(self):
        out = dict(SOURCES)
        for d in self.datasets.list():
            out[f"ds-{d['slug']}"] = d["name"] + (f" ({d['scenario']})" if d.get("scenario") else "")
        return out

    def source_for(self, name):
        name = "aorc-s3" if name in ("aorc", "s3") else name
        with self._lock:
            if name not in self._sources:
                if name == "synthetic":
                    self._sources[name] = SyntheticSource()
                elif name.startswith("ds-"):
                    cfg = self.datasets.get(name[3:])
                    if cfg is None:
                        raise ValueError(f"unknown dataset {name}")
                    self._sources[name] = DatasetSource(cfg)
                elif name == "aorc-s3":
                    self._sources[name] = AORCSource(cache_dir=self.ws.brick_cache_dir,
                                                     max_gb=float(self.ws.settings.get("cache_max_gb", 8.0)),
                                                     loader=self.loader, inputs=self.inputs,
                                                     keep_inputs=bool(self.ws.settings.get("keep_inputs", True)),
                                                     offline=self.offline)
                else:
                    raise ValueError(f"unknown source {name}")
            return self._sources[name]

    @property
    def source(self):
        return self.source_for(self.active)

    def set_offline(self, offline: bool):
        self.offline = bool(offline)
        with self._lock:
            if "aorc-s3" in self._sources:
                self._sources["aorc-s3"].offline = self.offline
        self.ws.save_settings(offline=self.offline)

    def download_source(self):
        """An online AORC source writing into the input store (for bulk downloads, whatever the active mode)."""
        if not hasattr(self, "_dl"):
            self._dl = AORCSource(cache_dir=self.ws.brick_cache_dir, loader=self.loader, inputs=self.inputs,
                                  keep_inputs=True, offline=False)
        return self._dl

    def ndvi_for(self, cfg: ChainConfig, source_name=None):
        if (source_name or self.active) == "synthetic":
            return SyntheticNDVI(allow_climatology=cfg.allow_ndvi_climatology)
        p = NDVIProvider(self.static_dir, allow_climatology=cfg.allow_ndvi_climatology)
        return p if p.available else None

    def ndvi_signature(self, cfg: ChainConfig, source_name, year) -> str:
        p = self.ndvi_for(cfg, source_name)
        if p is None:
            p = NDVIProvider(self.static_dir, allow_climatology=cfg.allow_ndvi_climatology)
        return p.signature(year) if hasattr(p, "signature") else str(getattr(p, "name", "synthetic"))

    def clim_path(self, source, cfg_id):
        return self.ws.root / "climatology" / f"{source}_{cfg_id}.json"

    def climatology(self, source, cfg_id):
        p = self.clim_path(source, cfg_id)
        key = (source, cfg_id, p.stat().st_mtime if p.exists() else 0)
        if key not in self._clims:
            self._clims[key] = Climatology(p)
        return self._clims[key]


def _human_bytes(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}"
        n /= 1024


def _probe_chunk(src):
    """(year, k) of the first 144-h chunk a source has data for."""
    d0, _ = src.domain({})
    ch = Grid.time_chunks(d0, d0, src.origin_of_year)[0]
    return ch["year"], ch["k"]


def create_app(ws: Workspace, grid: Grid, ctx: Context, *, extracted: ExtractedStore | None = None,
               max_cells: int = 12000, max_hours: int = 24 * 92, per_cell_limit: int = 1024,
               start_jobs: bool = True) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
    store = ResultsStore(ws, grid)
    jobs = JobRunner(ws, store, grid, ctx)
    extracted = extracted or ExtractedStore("data/output")
    fire_catalog = firemod.FireCatalog(ws, grid)
    tasks = TaskRunner(ws, grid, ctx)
    app.extensions["wup"] = {"ws": ws, "grid": grid, "ctx": ctx, "store": store, "jobs": jobs, "fires": fire_catalog,
                             "tasks": tasks}
    live_lock = threading.Semaphore(1)                  # one live calculation at a time (memory)
    if start_jobs:
        jobs.start()
        tasks.start()

    @app.errorhandler(OfflineMissing)
    def offline_missing(e):
        return jsonify({"error": str(e), "offline_missing": True}), 409

    def _local_origin(origin):
        from urllib.parse import urlparse
        try:
            return urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1")
        except Exception:                               # noqa: BLE001
            return False

    @app.before_request
    def same_machine_only():
        """The API changes files on this computer: refuse requests that a web page from another site makes
        through the browser (browsers always send Origin on those)."""
        origin = request.headers.get("Origin")
        if origin and origin != "null" and not _local_origin(origin):
            return jsonify({"error": "requests from other web sites are not accepted"}), 403
        return None

    @app.after_request
    def cors(resp):
        origin = request.headers.get("Origin")
        if origin and _local_origin(origin):            # the UI on another local port (?api=...) may call in
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(ValueError)
    @app.errorhandler(KeyError)
    @app.errorhandler(TypeError)
    def bad_request(e):
        logger.info("bad request %s: %s: %s", request.path, type(e).__name__, e)
        msg = f"missing or invalid value: {e}" if isinstance(e, KeyError) else str(e)
        return jsonify({"error": msg}), 400

    @app.errorhandler(Exception)
    def internal(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        logger.exception("unexpected error in %s", request.path)
        return jsonify({"error": f"{type(e).__name__}: {e}", "path": request.path}), 500

    def err(msg, code=400, **kw):
        return jsonify({"error": msg, **kw}), code

    def body():
        return request.get_json(force=True, silent=True) or {}

    def cfg_from(b) -> ChainConfig:
        c = b.get("config")
        if isinstance(c, str) and c in PRESETS:
            return PRESETS[c]
        return ChainConfig.from_dict(c) if c else PRESETS["hourly-default"]

    def cfg_from_args():
        if "config" in request.args:
            return cfg_from({"config": json.loads(request.args["config"])})
        return PRESETS["hourly-default"]

    def selection_ids(spec) -> list[int]:
        if spec is None:
            return []
        if isinstance(spec, list):
            return [int(p) for p in spec]
        if not isinstance(spec, dict):
            raise ValueError("selection must be a list of cell ids or a selection object")
        mode = spec.get("mode", "cells")
        if mode == "cells":
            return [int(p) for p in spec.get("point_ids", [])]
        if mode == "bbox":
            b = spec["bbox"]
            return grid.cells_in_bbox(float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        if mode == "brick":
            return grid.cells_in_brick(int(spec["ci"]), int(spec["cj"]))
        if mode == "bricks":
            out = []
            for k in spec["bricks"]:
                ci, cj = map(int, k.split("_"))
                out += grid.cells_in_brick(ci, cj)
            return out
        if mode == "county":
            names = spec.get("counties") or [spec.get("county")]
            return grid.points.loc[grid.points.county.isin(names), "point_id"].astype(int).tolist()
        if mode == "all":
            return grid.points.point_id.astype(int).tolist()
        raise ValueError(f"unknown selection mode {mode!r}")

    def bricks_of(pids):
        if not len(pids):
            return []
        sub = grid.points.iloc[grid.rows_for(pids)]
        return sorted({f"{int(a)}_{int(b)}" for a, b in zip(sub.ci, sub.cj)})

    def window(b):
        start = _t(b["start"])
        hours = int(b.get("hours", 24))
        if hours < 1 or hours > max_hours:
            raise ValueError(f"hours must be 1..{max_hours}")
        return start, start + timedelta(hours=hours - 1), hours

    # ------------------------------------------------------------------ app shell
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/api/status")
    def status():
        cfg = PRESETS["hourly-default"]
        src = ctx.source
        cov = store.coverage(src.name, cfg.id, *src.domain(ws.settings))
        clim = ctx.climatology(src.name, cfg.id)
        ndvi = ctx.ndvi_for(cfg)
        return jsonify({
            "version": PKG_VERSION, "workspace": str(ws.root), "settings": ws.settings,
            "source": {**src.status(), "active": ctx.active, "available": ctx.sources_available(),
                       "reachable": ctx.reachable.get(ctx.active)},
            "synthetic": src.name == "synthetic",
            "default_config": {**cfg.as_dict(), "id": cfg.id, "label": cfg.label()},
            "presets": {k: {**v.as_dict(), "id": v.id, "label": v.label()} for k, v in PRESETS.items()},
            "grid": {k: v for k, v in grid.summary().items() if k != "bricks"},
            "coverage": {k: cov[k] for k in ("percent_computed", "packets_computed", "per_fdi", "domain", "n_chunks")},
            "jobs": {"active": [j for j in jobs.list() if j["status"] in ("running", "queued", "paused")][:5],
                     "current": jobs.current},
            "climatology": clim.payload(),
            "ndvi": ndvi.describe() if ndvi is not None else {"available": False,
                                                                "note": "no NDVI files in " + str(ctx.static_dir)},
            "fuel_map": ctx.fuel_map.describe(),
            "extracted": extracted.rescan().payload(),
            "limits": {"max_cells": max_cells, "max_hours": max_hours, "per_cell_limit": per_cell_limit},
            "fuel_models": {k: v["name"] for k, v in NFDRS_FUEL_MODELS.items()},
            "zones": timebase.zone_table(),
        })

    SETTING_RULES = {      # name: (type, min, max)
        "workers": (int, 1, 16), "download_workers": (int, 1, 8), "job_spinup_hours": (int, 0, 24 * 90),
        "cache_max_gb": (float, 0.1, 1000.0), "min_free_gb": (float, 0.5, 1000.0), "job_auto_retries": (int, 0, 100),
        "auto_resume": (bool, None, None), "keep_inputs": (bool, None, None), "domain_end": (str, None, None)}

    @app.post("/api/settings")
    def settings():
        b = body()
        clean = {}
        for k, (typ, lo, hi) in SETTING_RULES.items():
            if k not in b:
                continue
            v = b[k]
            try:
                if typ is bool:
                    if not isinstance(v, bool):
                        raise ValueError("true or false")
                elif typ is str:
                    v = _t(v).strftime("%Y-%m-%dT%H")
                else:
                    v = typ(v)
                    if not (lo <= v <= hi):
                        raise ValueError(f"between {lo} and {hi}")
            except (TypeError, ValueError) as e:
                return err(f"setting {k}: {e}")
            clean[k] = v
        return jsonify(ws.save_settings(**clean))

    @app.post("/api/source")
    def set_source():
        b = body()
        name = b.get("name", "synthetic")
        name = "aorc-s3" if name in ("aorc", "s3", "aorc-local") else name
        avail = ctx.sources_available()
        if name not in avail:
            return err(f"unknown source {name}")
        if name == "aorc-s3" and "offline" in b:
            ctx.set_offline(bool(b["offline"]))
        try:
            src = ctx.source_for(name)
            if name == "aorc-s3" and not ctx.offline:
                src.origin_of_year(_t(ws.settings["domain_end"]).year)   # opens the store: proves S3 is reachable
                ctx.reachable[name] = True
        except Exception as e:
            ctx.reachable[name] = False
            return err(f"cannot reach {avail[name]}: {e}", 502)
        ctx.active = name
        return jsonify({"active": name})

    @app.get("/api/grid")
    def grid_summary():
        return jsonify(grid.summary())

    @app.get("/api/points.csv")
    def points_csv():
        cols = ["point_id", "latitude", "longitude", "lat_idx", "lon_idx", "ci", "cj", "county", "zone", "std_offset_h"]
        return app.response_class(grid.points[cols].to_csv(index=False), mimetype="text/csv")

    @app.get("/api/geo/<name>")
    def geo(name):
        if name not in ("wup_counties", "wup_outline"):
            abort(404)
        return jsonify(load_geojson(name))

    @app.get("/api/catalog")
    def catalog():
        return jsonify(catalog_payload())

    @app.get("/api/presets")
    def presets():
        return jsonify({k: {**v.as_dict(), "id": v.id, "label": v.label()} for k, v in PRESETS.items()})

    # ------------------------------------------------------------------ selection estimate
    @app.post("/api/select")
    def select():
        b = body()
        try:
            pids = selection_ids(b.get("selection"))
            start, end, hours = window(b) if "start" in b else (None, None, 0)
            cfg = cfg_from(b)
        except (KeyError, ValueError) as e:
            return err(str(e))
        bricks = bricks_of(pids)
        out = {"n_cells": len(pids), "bricks": bricks, "within_limits": 0 < len(pids) <= max_cells,
               "counties": grid.points.iloc[grid.rows_for(pids)].county.value_counts().to_dict() if pids else {},
               "packets": len(pids) * hours}
        if start is not None and pids:
            spin = int(b.get("spinup_hours", 0))
            fetch_chunks = jobs.chunks_for(ctx.active, start - timedelta(hours=spin), end)
            win_chunks = jobs.chunks_for(ctx.active, start, end)
            cells = {x["key"]: x for x in grid.bricks()}
            rect = sum((cells[k]["lat_idx_range"][1] - cells[k]["lat_idx_range"][0]) *
                       (cells[k]["lon_idx_range"][1] - cells[k]["lon_idx_range"][0]) for k in bricks)
            out["n_time_chunks"] = len(fetch_chunks)
            out["raw_fetch_mb"] = round(rect * 144 * 7 * 4 * len(fetch_chunks) / 1048576, 1)
            stored = sum(store.has_chunk(ctx.active, cfg.id, c["year"], c["k"], k) for k in bricks for c in win_chunks)
            total = len(bricks) * len(win_chunks)
            out["stored_fraction"] = round(stored / total, 4) if total else 0
            out["config_id"] = cfg.id
        out["point_ids"] = pids if len(pids) <= 30000 else []
        return jsonify(out)

    # ------------------------------------------------------------------ series / trace
    def live_result(pids, start, end, spin, cfg, mode="trace"):
        src = ctx.source
        with live_lock:
            raw = src.fetch(grid, pids, start - timedelta(hours=spin), end, gap_fill=cfg.gap_fill)
            res = run_chain(raw, grid=grid, cfg=cfg, spinup_hours=spin, mode=mode, ndvi=ctx.ndvi_for(cfg),
                            fuel_map=ctx.fuel_map, climatology=ctx.climatology(src.name, cfg.id))
        return raw, res

    @app.post("/api/series")
    def series():
        b = body()
        try:
            pids = selection_ids(b.get("selection", b.get("point_ids")))
            start, end, hours = window(b)
            spin = min(max(int(b.get("spinup_hours", 168)), 0), 24 * 90)
            fdis = [f for f in b.get("fdis", FDIS) if f in FDIS] or FDIS
            cfg = cfg_from(b)
            mode = b.get("mode", "auto")
        except (KeyError, ValueError) as e:
            return err(str(e))
        if not pids:
            return err("empty selection")
        t0 = time.perf_counter()
        src = ctx.source
        cov = {"store": 0.0}
        if mode in ("auto", "store"):
            times, steps, frac, missing = store.series(src.name, cfg.id, pids, start, end)
            cov["store"] = round(frac, 4)
            if frac >= 0.9999 or (mode == "store" and frac > 0):
                clim = ctx.climatology(src.name, cfg.id)
                if clim.available:
                    for k in FDIS:
                        if clim.has(k):
                            steps[f"{k}_pct"] = clim.percentile(k, steps[k])
                p = series_payload(steps, times, pids, fdis=fdis, source="store", per_cell_limit=per_cell_limit,
                                   notes=[f"Read from the results store (config {cfg.id}): continuous stored runs. "
                                          "Comparator overlays need 'Explore live'.",
                                          *([f"{len(missing)} chunk(s) not computed yet: NaN"] if missing else [])],
                                   params={**cfg.as_dict(), "config_id": cfg.id},
                                   extra={"coverage": cov, "synthetic": src.name == "synthetic", "data_source": src.name})
                p["elapsed_s"] = round(time.perf_counter() - t0, 3)
                return jsonify(p)
            if mode == "store":
                return err("nothing stored for this window and configuration", 404, coverage=cov)
        if mode == "batch":
            return series_from_batch(pids, start, end, fdis)
        if len(pids) > max_cells:
            return err(f"{len(pids)} cells exceeds the live limit ({max_cells}). Calculate & save it as a job, "
                       f"or view the stored field.", 413, n_cells=len(pids), max_cells=max_cells)
        if len(pids) * (hours + spin) > LIVE_MAX_PACKETS:
            return err(f"{len(pids):,} cells x {hours + spin:,} hours (with spin-up) is too large to calculate live "
                       f"(limit {LIVE_MAX_PACKETS:,} cell-hours, about 2 GB of memory). Use Calculate & save, a shorter "
                       "window or fewer cells.", 413, n_cells=len(pids), hours=hours + spin)
        raw, res = live_result(pids, start, end, spin, cfg)
        m = res.analysis_mask
        steps = {k: np.asarray(v)[m] for k, v in res.steps.items()}
        p = series_payload(steps, res.times[m], pids, fdis=fdis, source="live", per_cell_limit=per_cell_limit,
                           alts=res.alts, notes=res.notes, params={**cfg.as_dict(), "config_id": cfg.id},
                           extra={"coverage": cov, "synthetic": src.name == "synthetic", "data_source": src.name,
                                  "raw": {"bricks": raw.bricks, "time_chunks": raw.time_chunks, "stats": raw.stats,
                                          "tier_counts": raw.tier_counts}, "spinup_hours": spin})
        p["elapsed_s"] = round(time.perf_counter() - t0, 3)
        return jsonify(p)

    def series_from_batch(pids, start, end, fdis):
        cov = extracted.coverage(start, end)
        if not cov["complete"]:
            return err("the batch record (data/output) does not cover this window", 404, coverage=cov)
        steps, times = {}, None
        for v in ["FWI", "ERC", "BI"]:
            t, a = extracted.series(v, pids, start, end)
            if len(t):
                steps[v], times = a, t
        if times is None:
            return err("no batch values for this window", 404)
        cp = extracted.metadata(start.year, start.month).get("calculation_params", {})
        steps["wx_tier"] = np.zeros_like(steps["FWI"], dtype=np.uint8)
        return jsonify(series_payload(steps, times, pids, fdis=[f for f in fdis if f != "FPI"], source="batch",
                                      notes=[f"Batch record {', '.join(cov['have'])}: daylight basis "
                                             f"{cp.get('daylight', 'legacy-utc (record made before the fix)')}, "
                                             f"fuel moisture {cp.get('fuel_moisture_method', '?')}"],
                                      params=cp, extra={"coverage": cov}))

    @app.post("/api/trace")
    def trace():
        b = body()
        try:
            pid = int(b["point_id"])
            start, end, hours = window(b)
            spin = int(b.get("spinup_hours", 168))
            cfg = cfg_from(b)
        except (KeyError, ValueError) as e:
            return err(str(e))
        src = ctx.source
        # If the store holds the chunk containing `start`, re-run from that chunk's stored start state so
        # the trace reproduces the stored values exactly; otherwise cold-start with spin-up.
        brick = bricks_of([pid])[0]
        row = next((r for r in store._rows_overlapping(src.name, cfg.id, start, start) if r["brick"] == brick), None)
        t0 = time.perf_counter()
        if row is not None and b.get("prefer", "auto") != "live":
            c_start = pd.Timestamp(row["t_start"]).to_pydatetime()
            state = store.state_at(src.name, cfg.id, row["year"], row["k"], brick, "start", [pid])
            raw = src.fetch(grid, [pid], c_start, end, gap_fill=cfg.gap_fill)
            res = run_chain(raw, grid=grid, cfg=cfg, state=state,
                            spinup_hours=int((start - c_start).total_seconds() // 3600), mode="trace",
                            ndvi=ctx.ndvi_for(cfg), fuel_map=ctx.fuel_map, climatology=ctx.climatology(src.name, cfg.id))
            origin = "store-state"
        else:
            raw, res = live_result([pid], start, end, spin, cfg)
            origin = "cold-start"
        p = trace_payload(res, 0, include_spinup=bool(b.get("include_spinup", False)))
        r = grid.points.iloc[grid.rows_for([pid])[0]]
        p.update({"origin": origin, "elapsed_s": round(time.perf_counter() - t0, 3), "synthetic": src.name == "synthetic",
                  "cell": {"point_id": pid, "lat": float(r.latitude), "lon": float(r.longitude), "county": r.county,
                           "zone": r.zone, "tz": r.tz, "std_offset_h": int(r.std_offset_h), "lat_idx": int(r.lat_idx),
                           "lon_idx": int(r.lon_idx), "brick": brick},
                  "raw_tier_counts": raw.tier_counts})
        return jsonify(p)

    @app.get("/api/timebase")
    def timebase_view():
        try:
            pid = int(request.args["pid"])
            date = _t(request.args.get("date", "2009-05-20"))
            days = max(1, min(366, int(request.args.get("days", 1))))
        except (KeyError, ValueError) as e:
            return err(str(e))
        r = grid.points.iloc[grid.rows_for([pid])[0]]
        rep = timebase.day_report(float(r.latitude), float(r.longitude), r.county, date)
        if days > 1:
            mis = 0
            for d in range(days):
                mis += timebase.day_report(float(r.latitude), float(r.longitude), r.county,
                                           date + timedelta(days=d))["misclassified_hours"]
            rep["window"] = {"days": days, "misclassified_hours": mis, "hours": 24 * days}
        return jsonify(rep)

    # ------------------------------------------------------------------ whole-area field
    @app.get("/api/field")
    def field():
        var = request.args.get("var", "FWI")
        mode = request.args.get("mode", "store")
        try:
            when = _t(request.args.get("time", ""))
        except ValueError as e:
            return err(str(e))
        cfg = cfg_from_args()
        src = ctx.source
        if mode == "store":
            vals, n = store.field(src.name, cfg.id, var, when)
        else:
            got = extracted.field(var, when)
            if got is None:
                return err(f"no batch record for {var} at {when:%Y-%m-%dT%H}", 404)
            pid_order, v = got
            vals = np.full(grid.n_cells, np.nan, np.float32)
            vals[grid.rows_for(pid_order)] = v
            n = int(np.isfinite(v).sum())
        finite = vals[np.isfinite(vals)]
        out = {"var": var, "time": when.strftime("%Y-%m-%dT%H:00Z"), "n": int(n), "mode": mode, "config_id": cfg.id,
               "values": [None if not np.isfinite(x) else round(float(x), 2) for x in vals], "percentile_values": None}
        if len(finite):
            out.update({"p2": float(np.percentile(finite, 2)), "median": float(np.median(finite)),
                        "p98": float(np.percentile(finite, 98))})
        clim = ctx.climatology(src.name, cfg.id)
        if mode == "store" and clim.available and clim.has(var):
            pv = clim.percentile(var, vals)
            out["percentile_values"] = [None if not np.isfinite(x) else round(float(x), 1) for x in pv]
        return jsonify(out)

    @app.get("/api/field/times")
    def field_times():
        try:
            y, m = int(request.args["year"]), int(request.args["month"])
        except (KeyError, ValueError):
            return err("year and month required")
        if request.args.get("mode", "store") == "batch":
            if not extracted.has(y, m):
                return jsonify({"times": []})
            return jsonify({"times": [t.replace(" ", "T") for t in extracted.timestamps("FWI", y, m)]})
        cfg = cfg_from_args()
        return jsonify({"times": [t.strftime("%Y-%m-%dT%H:%M") for t in store.stored_times(ctx.active, cfg.id, y, m)]})

    # ------------------------------------------------------------------ coverage
    @app.get("/api/coverage")
    def coverage():
        cfg = cfg_from_args()
        cid = request.args.get("config_id", cfg.id)
        c = store.coverage(ctx.active, cid, *ctx.source.domain(ws.settings),
                           allow_ndvi_climatology=cfg.allow_ndvi_climatology)
        c["configs"] = store.configs(ctx.active)
        c["batch_months"] = extracted.rescan().months()
        return jsonify(c)

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def jobs_list():
        return jsonify({"jobs": jobs.list(), "current": jobs.current, "workers": ws.settings.get("workers", 2)})

    @app.post("/api/jobs")
    def jobs_create():
        b = body()
        try:
            cfg = cfg_from(b)
            bricks = list(b["bricks"]) if "bricks" in b else bricks_of(selection_ids(b.get("selection")))
            if not bricks:
                raise ValueError("no bricks selected")
            jid = jobs.create(b.get("name"), ctx.active, cfg, bricks, _t(b["start"]), _t(b["end"]), b.get("spinup_hours"))
        except (KeyError, ValueError) as e:
            return err(str(e))
        return jsonify(jobs.get(jid))

    @app.post("/api/jobs/<int:jid>/<action>")
    def jobs_action(jid, action):
        if action not in ("pause", "resume", "cancel"):
            abort(404)
        getattr(jobs, action)(jid)
        return jsonify(jobs.get(jid) or {})

    @app.delete("/api/jobs/<int:jid>")
    def jobs_delete(jid):
        jobs.delete(jid)
        return jsonify({"deleted": jid})

    # ------------------------------------------------------------------ saving & sharing
    @app.get("/api/selections")
    def sel_list():
        return jsonify(ws.list_selections())

    @app.post("/api/selections")
    def sel_save():
        b = body()
        payload = b.get("selection") or {}
        try:
            if payload.get("mode") != "cells":
                payload = {**payload, "point_ids": selection_ids(payload)}
        except (KeyError, ValueError) as e:
            return err(str(e))
        return jsonify({"id": ws.save_selection(b.get("name", "selection"), payload, b.get("notes", ""))})

    @app.get("/api/selections/<int:sid>")
    def sel_get(sid):
        s = ws.get_selection(sid)
        return jsonify(s) if s else err("not found", 404)

    @app.delete("/api/selections/<int:sid>")
    def sel_del(sid):
        ws.delete_selection(sid)
        return jsonify({"deleted": sid})

    @app.get("/api/sessions")
    def ses_list():
        return jsonify(ws.list_sessions())

    @app.post("/api/sessions")
    def ses_save():
        b = body()
        return jsonify({"id": ws.save_session(b.get("name", "session"), b.get("state", {}))})

    @app.get("/api/sessions/<int:sid>")
    def ses_get(sid):
        s = ws.get_session(sid)
        return jsonify(s) if s else err("not found", 404)

    @app.delete("/api/sessions/<int:sid>")
    def ses_del(sid):
        ws.delete_session(sid)
        return jsonify({"deleted": sid})

    @app.get("/api/exports")
    def exp_list():
        return jsonify(ws.list_exports())

    @app.post("/api/exports")
    def exp_save():
        b = body()
        data = base64.b64decode(b["base64"]) if "base64" in b else str(b.get("content", "")).encode("utf-8")
        eid, path = ws.save_export(b.get("name", "export.txt"), data, b.get("kind", "file"), b.get("note", ""))
        return jsonify({"id": eid, "name": path.name, "path": str(path), "bytes": len(data)})

    @app.get("/api/exports/<int:eid>")
    def exp_get(eid):
        p = ws.export_path(eid)
        if p is None or not p.exists():
            abort(404)
        return send_file(p, as_attachment=request.args.get("inline") != "1", download_name=p.name)

    @app.post("/api/export/netcdf")
    def export_netcdf():
        import xarray as xr
        b = body()
        try:
            pids = selection_ids(b.get("selection", b.get("point_ids")))
            start, end, hours = window(b)
            cfg = cfg_from(b)
            spin = int(b.get("spinup_hours", 168))
        except (KeyError, ValueError) as e:
            return err(str(e))
        if not pids:
            return err("empty selection")
        src = ctx.source
        times, steps, frac, _ = store.series(src.name, cfg.id, pids, start, end)
        origin = "store"
        if frac < 0.9999:
            if len(pids) > max_cells:
                return err("not stored and too large to compute live", 413)
            raw, res = live_result(pids, start, end, spin, cfg, mode="store")
            m = res.analysis_mask
            steps = {k: np.asarray(v)[m] for k, v in res.steps.items()}
            times, origin = res.times[m], "live"
        sub = grid.points.iloc[grid.rows_for(pids)]
        ds = xr.Dataset({v: (("time", "cell"), np.asarray(steps[v], dtype=np.float32), {"long_name": PACK[v][1], "units": PACK[v][2]})
                         for v in SAVED if v in steps},
                        coords={"time": pd.DatetimeIndex(times).values,
                                "point_id": ("cell", np.asarray(pids, dtype=np.int32)),
                                "latitude": ("cell", sub.latitude.values), "longitude": ("cell", sub.longitude.values),
                                "lat_idx": ("cell", sub.lat_idx.values.astype(np.int32)),
                                "lon_idx": ("cell", sub.lon_idx.values.astype(np.int32)),
                                "std_offset_h": ("cell", sub.std_offset_h.values.astype(np.int16))})
        ds["wx_tier"] = (("time", "cell"), np.asarray(steps["wx_tier"], dtype=np.int16))
        ds["ndvi_tier"] = (("time", "cell"), np.asarray(steps["ndvi_tier"], dtype=np.int16))
        ds.attrs.update({"title": "WUP fire danger indices (WUP Grid Explorer)", "source": src.name,
                         "synthetic": int(src.name == "synthetic"), "config": json.dumps(cfg.as_dict()),
                         "config_id": cfg.id, "origin": origin,
                         "time_basis": "UTC (AORC stamps); std_offset_h gives each cell's local standard time",
                         "created": datetime.now().isoformat(timespec="seconds")})
        enc = {v: {"zlib": True, "complevel": 6, "shuffle": True, "dtype": "int16", "scale_factor": PACK[v][0],
                   "_FillValue": -32768} for v in SAVED if v in steps}
        enc.update({v: {"zlib": True, "complevel": 6} for v in ("wx_tier", "ndvi_tier")})
        name = f"wup_fdi_{start:%Y%m%dT%H}_{hours}h_{len(pids)}cells_{cfg.id}.nc"
        path = ws.root / "exports" / f"{datetime.now():%Y%m%d-%H%M%S}_{name}"
        with NC_LOCK:
            ds.to_netcdf(path, engine="netcdf4", encoding=enc)
        ws.register_export(path, "netcdf", f"{len(pids)} cells x {hours} h ({origin})")
        return send_file(path, as_attachment=True, download_name=path.name, mimetype="application/x-netcdf")

    @app.post("/api/export/year")
    def export_year():
        b = body()
        try:
            cfg = cfg_from(b)
            year = int(b["year"])
        except (KeyError, ValueError) as e:
            return err(str(e))
        try:
            out = store.export_year(ctx.active, cfg.id, year, ws.root / "exports")
        except ValueError as e:
            return err(str(e), 404)
        out["hourly_id"] = ws.register_export(out["hourly"], "netcdf-year", f"hourly {year}")
        out["daily_id"] = ws.register_export(out["daily"], "netcdf-daily", f"daily max {year}")
        return jsonify(out)

    @app.get("/api/results/files")
    def results_files():
        cfg = cfg_from_args()
        year = request.args.get("year")
        files = store.files(ctx.active, cfg.id, int(year) if year else None)
        return jsonify({"dir": str(ws.results_dir / ctx.active / cfg.id), "files": files,
                        "bytes": sum(f["bytes"] for f in files)})

    @app.post("/api/climatology/build")
    def clim_build():
        b = body()
        cfg = cfg_from(b)
        out = store.build_climatology(ctx.active, cfg.id, ctx.clim_path(ctx.active, cfg.id), stride=int(b.get("stride", 4)))
        return jsonify({**{k: v for k, v in out.items() if k != "vars"}, "vars": {k: v["n"] for k, v in out["vars"].items()}})

    # ------------------------------------------------------------------ data: inputs, downloads, tasks
    def data_inventory():
        st = ws.settings
        return {"aorc": ctx.inputs.inventory(st.get("domain_start"), st.get("domain_end")),
                "offline": ctx.offline, "keep_inputs": bool(st.get("keep_inputs", True)),
                "ndvi": NDVIProvider(ctx.static_dir).describe(),
                "static_dir": str(ctx.static_dir),
                "tasks": tasks.list(20)}

    @app.get("/api/data")
    def data_get():
        return jsonify(data_inventory())

    @app.post("/api/data/aorc")
    def data_aorc():
        b = body()
        try:
            start, end = _t(b["start"]), _t(b["end"])
            if end < start:
                raise ValueError("end before start")
            bricks = list(b.get("bricks") or [x["key"] for x in grid.bricks()])
            tid = tasks.create("aorc-download", b.get("name") or f"AORC {start:%Y-%m-%d} → {end:%Y-%m-%d} ({len(bricks)} bricks)",
                               {"start": start.isoformat(), "end": end.isoformat(), "bricks": bricks})
        except (KeyError, ValueError) as e:
            return err(str(e))
        return jsonify(tasks.get(tid))

    @app.post("/api/data/ndvi")
    def data_ndvi():
        b = body()
        try:
            backend = b.get("backend", "planetary")
            product = b.get("product", "MOD13Q1")
            yrs = b.get("years") or [2000, 2026]
            years = list(range(int(yrs[0]), int(yrs[-1]) + 1)) if len(yrs) == 2 else [int(y) for y in yrs]
            if backend not in ("planetary", "appeears", "cdr", "local"):
                raise ValueError("backend must be planetary, appeears, cdr or local")
            if backend == "local" and not b.get("folder"):
                raise ValueError("local backend needs a folder")
            params = {"backend": backend, "product": product, "years": years, "folder": b.get("folder")}
            tid = tasks.create("ndvi-ingest", b.get("name") or f"NDVI {product if backend != 'cdr' else 'AVHRR CDR'} "
                                                                f"{years[0]}–{years[-1]} via {backend}", params)
            if backend == "appeears" and b.get("username"):
                tasks.secrets[tid] = {"username": b.get("username"), "password": b.get("password")}
        except (KeyError, ValueError) as e:
            return err(str(e))
        return jsonify(tasks.get(tid))

    @app.get("/api/datasets")
    def datasets_list():
        out = []
        for d in ctx.datasets.list():
            item = {**d, "source": f"ds-{d['slug']}"}
            try:
                item["status"] = ctx.source_for(item["source"]).status()
            except Exception as e:
                item["status"] = {"error": str(e)}
            out.append(item)
        return jsonify({"datasets": out, "presets": PRESET_HINTS})

    @app.post("/api/datasets")
    def datasets_save():
        b = body()
        try:
            cfg = ctx.datasets.save(b)
            name = f"ds-{cfg['slug']}"
            with ctx._lock:
                ctx._sources.pop(name, None)
            st = ctx.source_for(name).status()            # probe: opens the files, finds variables and dates
            if st.get("error"):
                raise ValueError(st["error"])
            probe = ctx.source_for(name)._raw_brick_chunk(grid, *_probe_chunk(ctx.source_for(name)),
                                                                Brick(*map(int, max(grid.bricks(), key=lambda x: x["n_cells"])["key"].split("_")))) \
                if b.get("probe", True) else None
        except Exception as e:
            return err(f"dataset not usable: {e}")
        meta = probe[2] if probe else {}
        found = meta.get("variables", {})
        missing = [v for v in ("TMP_2maboveground", "SPFH_2maboveground", "PRES_surface", "APCP_surface") if v not in found]
        if "UGRD_10maboveground" not in found and "_wind_speed" not in found:
            missing.append("10 m wind")
        return jsonify({"dataset": cfg, "source": name, "status": st, "variables_found": found, "missing": missing,
                        "model_resolution_km": meta.get("model_resolution_km")})

    def _dataset_localize_task(tid, params, progress):
        slug = params["slug"]
        src = ctx.source_for(f"ds-{slug}")
        files = src.localize(ctx.datasets.local_dir(slug), grid.bbox, progress=progress)
        ctx.datasets.set_local(slug, files)
        with ctx._lock:
            ctx._sources.pop(f"ds-{slug}", None)               # reopen from the local copy
        st = ctx.source_for(f"ds-{slug}").status()
        return {"files": len(files), "bytes": int(sum(Path(f).stat().st_size for f in files)),
                "dir": str(ctx.datasets.local_dir(slug)), "start": st.get("start"), "end": st.get("end")}

    tasks.register("dataset-localize", _dataset_localize_task)

    @app.post("/api/datasets/<slug>/localize")
    def datasets_localize(slug):
        if ctx.datasets.get(slug) is None:
            return err("unknown dataset", 404)
        tid = tasks.create("dataset-localize", f"Offline copy of {ctx.datasets.get(slug)['name']}", {"slug": slug})
        if body().get("wait"):
            tasks.run_now(tid)
        return jsonify(tasks.get(tid))

    @app.delete("/api/datasets/<slug>")
    def datasets_delete(slug):
        ctx.datasets.delete(slug)
        with ctx._lock:
            ctx._sources.pop(f"ds-{slug}", None)
        if ctx.active == f"ds-{slug}":
            ctx.active = "synthetic"
        return jsonify({"deleted": slug})

    # ------------------------------------------------------------------ verification of the saved record
    def _verify_task(tid, params, progress):
        from aorc_tools.explorer import verify as verifymod
        src = ctx.source_for(params["source"])
        cfg = ChainConfig.from_dict(params["config"])
        rep_ = verifymod.verify_record(store, grid, src, cfg, ws.settings, mode=params.get("mode", "quick"),
                                       progress=progress, years=params.get("years"), bricks=params.get("bricks"))
        paths = verifymod.save_report(ws, rep_)
        eid = ws.register_export(paths["html"], "verify-report", f"{rep_['overall']} {src.name} {cfg.id} {rep_['mode']}")
        ws.register_export(paths["json"], "verify-json", f"{rep_['overall']} {src.name} {cfg.id} {rep_['mode']}")
        return {"overall": rep_["overall"], "checks": {k: v["status"] for k, v in rep_["checks"].items()},
                "completeness_percent": rep_["checks"]["completeness"]["percent"], "files_checked": rep_["files_checked"],
                "problems": rep_["problems"][:20], "report_export_id": eid, **paths}

    tasks.register("verify", _verify_task)

    def _repair_task(tid, params, progress):
        from aorc_tools.explorer import verify as verifymod
        src = ctx.source_for(params["source"])
        cfg = ChainConfig.from_dict(params["config"])
        rep_ = verifymod.verify_record(store, grid, src, cfg, ws.settings, mode="full", progress=progress,
                                       bricks=params.get("bricks"))
        out = verifymod.repair(store, rep_)
        return {**out, "unreadable": rep_["checks"]["readable_files"]["unreadable"], "verify_overall": rep_["overall"]}

    tasks.register("repair", _repair_task)

    @app.post("/api/data/repair")
    def data_repair():
        b = body()
        try:
            cfg = cfg_from(b)
            source = b.get("source") or ctx.active
            ctx.source_for(source)
        except (KeyError, ValueError) as e:
            return err(str(e))
        tid = tasks.create("repair", f"Repair {source} {cfg.id}", {"source": source, "config": cfg.as_dict(),
                                                                    "bricks": b.get("bricks")})
        if b.get("wait"):
            tasks.run_now(tid)
        return jsonify(tasks.get(tid))

    @app.post("/api/data/verify")
    def data_verify():
        b = body()
        try:
            cfg = cfg_from(b)
            source = b.get("source") or ctx.active
            ctx.source_for(source)
            mode = b.get("mode", "quick")
            if mode not in ("quick", "full"):
                raise ValueError("mode must be quick or full")
            yrs = b.get("years")
            years = (list(range(int(yrs[0]), int(yrs[-1]) + 1)) if len(yrs) == 2 else [int(y) for y in yrs]) if yrs else None
            tid = tasks.create("verify", b.get("name") or f"Verify {source} {cfg.id} ({mode})",
                               {"source": source, "config": cfg.as_dict(), "mode": mode, "years": years,
                                "bricks": b.get("bricks")})
        except (KeyError, ValueError) as e:
            return err(str(e))
        if b.get("wait"):
            tasks.run_now(tid)
        return jsonify(tasks.get(tid))

    # ------------------------------------------------------------------ research bundle
    def _bundle_task(tid, params, progress):
        from aorc_tools.explorer import bundle as bundlemod
        out = Path(params["path"])
        r = bundlemod.make(ws, ctx, store, grid, out, parts=params.get("parts"), results_filter=params.get("results"),
                           progress=progress)
        r["export_id"] = ws.register_export(out, "bundle", f"research bundle {_human_bytes(r['bytes'])}")
        return r

    def _bundle_check_task(tid, params, progress):
        from aorc_tools.explorer import bundle as bundlemod
        r = bundlemod.check(Path(params["path"]), progress=progress)
        return {k: v for k, v in r.items() if k != "provenance"} | {"errors": r["bad"] + r["missing"]}

    tasks.register("bundle", _bundle_task)
    tasks.register("bundle-check", _bundle_check_task)

    @app.get("/api/bundle/plan")
    def bundle_plan():
        from aorc_tools.explorer import bundle as bundlemod
        parts = [p for p in request.args.get("parts", "").split(",") if p] or None
        pl = bundlemod.plan(ws, ctx, parts)
        return jsonify({"parts": pl["parts"], "total_bytes": pl["total_bytes"], "labels": bundlemod.PARTS,
                        "default_path": str(ws.root.parent / f"WUP_FDI_bundle_{datetime.now():%Y%m%d}.zip"),
                        "configs": store.configs()})

    @app.post("/api/bundle")
    def bundle_make():
        from aorc_tools.explorer import bundle as bundlemod
        b = body()
        parts = [p for p in (b.get("parts") or list(bundlemod.PARTS)) if p in bundlemod.PARTS]
        path = Path(b.get("path") or ws.root.parent / f"WUP_FDI_bundle_{datetime.now():%Y%m%d}.zip").expanduser()
        if path.suffix.lower() != ".zip":
            path = path.with_suffix(".zip")
        try:
            path.resolve().relative_to(ws.root.resolve())
            if path.resolve().parent != (ws.root / "bundles").resolve():
                return err("put the bundle outside the workspace folder (or in its bundles/ folder)")
        except ValueError:
            pass
        tid = tasks.create("bundle", b.get("name") or f"Research bundle → {path.name}",
                           {"path": str(path), "parts": parts, "results": b.get("results")})
        if b.get("wait"):
            tasks.run_now(tid)
        return jsonify(tasks.get(tid))

    @app.post("/api/bundle/check")
    def bundle_check():
        b = body()
        if not b.get("path") or not Path(b["path"]).exists():
            return err("no such file")
        tid = tasks.create("bundle-check", f"Check {Path(b['path']).name}", {"path": b["path"]})
        if b.get("wait"):
            tasks.run_now(tid)
        return jsonify(tasks.get(tid))

    @app.post("/api/data/offline")
    def data_offline():
        ctx.set_offline(bool(body().get("offline", True)))
        return jsonify({"offline": ctx.offline})

    @app.get("/api/tasks")
    def tasks_list():
        return jsonify({"tasks": tasks.list(50)})

    @app.post("/api/tasks/<int:tid>/<action>")
    def tasks_action(tid, action):
        if action not in ("cancel", "resume"):
            return err("unknown action")
        getattr(tasks, action)(tid)
        return jsonify(tasks.get(tid) or {})

    @app.delete("/api/tasks/<int:tid>")
    def tasks_delete(tid):
        try:
            tasks.delete(tid)
        except ValueError as e:
            return err(str(e))
        return jsonify({"deleted": tid})

    # ------------------------------------------------------------------ known fires
    def fire_live(pids, start, end, spin, cfg):
        return live_result(pids, pd.Timestamp(start).to_pydatetime(), pd.Timestamp(end).to_pydatetime(), spin, cfg, mode="store")[1]

    def run_fire_check(fire, cfg, b):
        rep_ = firemod.check_fire(fire, grid=grid, store=store, source_name=ctx.active, cfg=cfg, live=fire_live,
                                  days_before=int(b.get("days_before", 14)), days_after=int(b.get("days_after", 3)),
                                  radius=b.get("radius"), spinup_hours=int(ws.settings.get("job_spinup_hours", 720)),
                                  domain_end=ctx.source.domain(ws.settings)[1])
        if rep_.get("status") == "ok":
            d = ws.root / "fires" / "checks"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{fire['properties']['id']}_{ctx.active}_{cfg.id}.json").write_text(json.dumps(rep_, indent=1, default=str))
        return rep_

    @app.get("/api/fires")
    def fires_list():
        feats = fire_catalog.all()
        de = pd.Timestamp(ws.settings.get("domain_end"))
        for f in feats:
            t = firemod.discovery_utc(f)
            f["properties"]["in_record"] = bool(t is not None and pd.Timestamp(t) <= de and t.year >= 1979)
            chk = ws.root / "fires" / "checks" / f"{f['properties']['id']}_{ctx.active}_{cfg_from_args().id}.json"
            f["properties"]["checked"] = chk.exists()
        return jsonify({"type": "FeatureCollection", "features": feats, "domain_end": str(de)})

    @app.post("/api/fires/import")
    def fires_import():
        f = request.files.get("file")
        if f is None:
            return err("no file")
        try:
            out = fire_catalog.import_file(f.filename or "fires.csv", f.read())
        except Exception as e:
            return err(f"could not read {f.filename}: {e}")
        return jsonify(out)

    @app.post("/api/fires/<fid>/location")
    def fires_location(fid):
        b = body()
        try:
            return jsonify(fire_catalog.set_location(fid, float(b["lat"]), float(b["lon"])))
        except (KeyError, ValueError) as e:
            return err(str(e))

    @app.delete("/api/fires/<fid>")
    def fires_delete(fid):
        try:
            fire_catalog.delete(fid)
        except KeyError as e:
            return err(str(e), 404)
        return jsonify({"deleted": fid})

    @app.post("/api/fires/<fid>/check")
    def fires_check(fid):
        b = body()
        fire = fire_catalog.get(fid)
        if fire is None:
            return err("unknown fire", 404)
        t0 = time.perf_counter()
        r = run_fire_check(fire, cfg_from(b), b)
        r["elapsed_s"] = round(time.perf_counter() - t0, 2)
        return jsonify(r)

    @app.post("/api/fires/<fid>/season-job")
    def fires_season_job(fid):
        b = body()
        fire = fire_catalog.get(fid)
        if fire is None:
            return err("unknown fire", 404)
        try:
            spec = firemod.season_job_spec(grid, fire, b.get("radius"), ws.settings.get("domain_end"))
            jid = jobs.create(spec["name"], ctx.active, cfg_from(b), spec["bricks"], spec["start"], spec["end"])
        except ValueError as e:
            return err(str(e))
        return jsonify(jobs.get(jid))

    @app.post("/api/fires/season-jobs")
    def fires_season_jobs():
        b = body()
        cfg = cfg_from(b)
        made, skipped = [], []
        want = set(b.get("ids") or [])
        de = pd.Timestamp(ws.settings.get("domain_end"))
        by_key = {}
        for f in fire_catalog.all():
            p_ = f["properties"]
            t = firemod.discovery_utc(f)
            if want and p_["id"] not in want:
                continue
            reason = ("no location" if not p_.get("in_grid") else "no date" if t is None
                      else "after the AORC data in this workspace" if pd.Timestamp(t) > de else "before 1979" if t.year < 1979 else None)
            if reason:
                skipped.append(f"{p_['id']} ({reason})")
                continue
            spec = firemod.season_job_spec(grid, f, None, ws.settings.get("domain_end"))
            k = (tuple(spec["bricks"]), spec["start"].year)
            by_key.setdefault(k, (spec, []))[1].append(p_["name"])
        for (bricks, yr), (spec, names) in by_key.items():
            try:
                jid = jobs.create(f"Fire season {yr} · {', '.join(names)}", ctx.active, cfg, list(bricks), spec["start"], spec["end"])
                made.append(jobs.get(jid))
            except ValueError as e:
                skipped.append(f"{yr}: {e}")
        return jsonify({"jobs": made, "skipped": skipped})

    @app.post("/api/fires/report")
    def fires_report():
        b = body()
        cfg = cfg_from(b)
        want = set(b.get("ids") or [])
        reps = []
        for f in fire_catalog.all():
            if want and f["properties"]["id"] not in want:
                continue
            try:
                reps.append(run_fire_check(f, cfg, b))
            except Exception as e:                     # one bad record must not sink the report
                logger.exception("fire check failed")
                reps.append({"fire": {"id": f["properties"]["id"], "name": f["properties"].get("name")}, "status": "error", "message": str(e)})
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        rows = firemod.summary_rows(reps)
        csv_id, _ = ws.save_export(f"wup_fire_checks_{ctx.active}_{cfg.id}.csv", pd.DataFrame(rows).to_csv(index=False).encode(), "fire-checks-csv",
                                f"{len(reps)} fires")
        html_id, _ = ws.save_export(f"wup_fire_checks_{ctx.active}_{cfg.id}.html", firemod.html_report(reps).encode(), "fire-checks-html",
                                 f"{len(reps)} fires")
        json_id, _ = ws.save_export(f"wup_fire_checks_{ctx.active}_{cfg.id}.json", json.dumps(reps, indent=1, default=str).encode(),
                                 "fire-checks-json", f"{len(reps)} fires")
        ok = [r for r in reps if r.get("status") == "ok"]
        return jsonify({"reports": reps, "csv_id": csv_id, "html_id": html_id, "json_id": json_id, "stamp": stamp,
                        "summary": {"checked": len(ok), "elevated": sum(r.get("verdict", {}).get("kind") == "elevated" for r in ok),
                                    "mixed": sum(r.get("verdict", {}).get("kind") == "mixed" for r in ok),
                                    "not_elevated": sum(r.get("verdict", {}).get("kind") == "not-elevated" for r in ok),
                                    "unranked": sum(r.get("verdict", {}).get("kind") == "unranked" for r in ok)}})

    @app.post("/api/shutdown")
    def shutdown():
        if request.remote_addr not in ("127.0.0.1", "::1", None):
            abort(403)
        jobs.stop()
        tasks.stop()

        def bye():
            for fn in app.extensions["wup"].get("on_exit", []):
                try:
                    fn()
                except Exception:                     # noqa: BLE001
                    pass
            os._exit(0)
        threading.Timer(0.5, bye).start()
        return jsonify({"bye": True})

    return app


def default_points_csv():
    here = Path(__file__).resolve()
    for p in (Path("data/output/points_index.csv"), here.parent / "static" / "points_index.csv",
              here.parents[2] / "data" / "output" / "points_index.csv"):
        if p.exists():
            return p
    raise FileNotFoundError("points_index.csv not found (data/output/points_index.csv)")


def build(workspace, points_csv=None, source="synthetic", static_dir="data/static", output_base="data/output",
          loader=None, start_jobs=True, **kw):
    ws = Workspace(workspace)
    grid = load_grid(points_csv or default_points_csv())
    ctx = Context(ws, grid, Path(static_dir), "aorc-s3" if source in ("aorc", "s3", "aorc-s3") else "synthetic",
                  loader=loader)
    return create_app(ws, grid, ctx, extracted=ExtractedStore(output_base), start_jobs=start_jobs, **kw)
