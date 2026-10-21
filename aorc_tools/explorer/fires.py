"""
Known-fire checks: do the computed indices make sense where and when real fires started?

This is a case-based plausibility check, not skill validation (that needs every
day with and without fires -- the companion paper). For one fire it answers:

* what were FWI (hourly FWI2025 and classic daily), FPI, ERC and BI at the
  fire's cells on the discovery day (and the hour of discovery when known)?
* how unusual was that day at THOSE cells -- its percentile among all days of
  the same fire season (Apr-Oct, local standard days) in the saved results?
* do the numbers agree with independent reference values recorded for the fire
  (e.g. your earlier real-AORC Pinery runs)?

Fires come from a seed catalog (static/geo/wup_fires.geojson) plus anything
imported into the workspace (CSV / XLSX / GeoJSON from NIFC, MTBS, Michigan DNR;
common column names are recognised automatically).
"""

from __future__ import annotations

import io
import json
import math
import re
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.workspace import now_iso

SEED = Path(__file__).resolve().parent / "static" / "geo" / "wup_fires.geojson"
CHECK_VARS = ["FWI", "FWI_daily", "FPI", "ERC", "BI"]
LABEL = {"FWI": "FWI (hourly, FWI2025)", "FWI_daily": "FWI (classic daily, 12:00 LST)", "FPI": "FPI", "ERC": "ERC", "BI": "BI"}
FWI_CLASSES = [(0, "Low"), (5, "Moderate"), (10, "High"), (20, "Very high"), (30, "Extreme")]
RANK_CLASSES = [(0, "below median"), (50, "above median"), (80, "high (80th+)"), (90, "very high (90th+)"), (97, "extreme (97th+)")]
SEASON = (4, 10)                       # April..October, local standard months
RADIUS_BY_PRECISION = {"point of origin": 1, "perimeter centroid": 1, "approximate": 3, "unknown": 3}
MAX_CELLS = 400

# column aliases (NIFC WFIGS/InFORM incl. shapefile-truncated names, MTBS, FPA-FOD, generic)
ALIASES = {
    "name": ["incidentname", "incidentna", "incident", "fire_name", "firename", "incid_name", "name"],
    "discovered": ["firediscoverydatetime", "firediscov", "firediscoverydate", "discovery_date", "discoverydate", "ig_date",
                   "startdate", "start_date", "datetime", "discovered", "date", "alarm_date", "fire_date"],
    "discovery_time": ["discovery_time", "disc_time", "time"],
    "acres": ["incidentsize", "incidentsi", "fire_size", "firesize", "burnbndac", "gis_acres", "acres", "size_acres", "dailyacres"],
    "lat": ["initiallatitude", "initiallat", "latitude", "lat", "burnbndlat", "y", "poo_lat", "poolatitude"],
    "lon": ["initiallongitude", "initiallon", "longitude", "lon", "long", "lng", "burnbndlon", "x", "poo_lon", "poolongitude"],
    "county": ["poocounty", "county", "fips_name", "county_name"],
    "cause": ["firecause", "nwcg_general_cause", "nwcg_cause_classification", "stat_cause_descr", "cause"],
    "type": ["incidenttypecategory", "incident_type", "inctyp_abbr", "type", "feature_ca"],
    "source_ref": ["irwinid", "unqe_fire_id", "event_id", "fod_id", "uniquefireidentifier", "fire_id", "id"],
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def parse_when(v, time_v=None):
    """-> (ISO UTC string, date_only). Accepts epoch ms (NIFC), ISO strings, Excel dates, YYYYMMDD."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)) or str(v).strip() in ("", "nan", "NaT", "None"):
        return None, True
    if isinstance(v, (int, float, np.integer, np.floating)) and float(v) > 1e11:          # epoch milliseconds
        t = datetime.fromtimestamp(float(v) / 1000, timezone.utc)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ"), False
    s = str(v).strip()
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    try:
        t = pd.Timestamp(s)
    except Exception:
        return None, True
    if time_v is not None and str(time_v).strip() not in ("", "nan", "None"):
        hhmm = re.sub(r"\D", "", str(time_v)).zfill(4)[:4]
        t = t.normalize() + pd.Timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[2:]))
        date_only = False
    else:
        date_only = (t.hour == 0 and t.minute == 0 and t.second == 0 and (len(s) <= 10 or "T00:00:00" in s or " 00:00:00" in s))
    if t.tzinfo is None:
        if date_only:
            return t.strftime("%Y-%m-%d"), True
        t = t.tz_localize("UTC")          # naive times in NIFC/WFIGS exports are UTC
    return t.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), False


def discovery_utc(fire) -> datetime | None:
    d = fire["properties"].get("discovered")
    if not d:
        return None
    t = pd.Timestamp(d)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t.to_pydatetime()


def _cls(value, table):
    if value is None or not math.isfinite(value):
        return None
    out = table[0][1]
    for lo, name in table:
        if value >= lo:
            out = name
    return out


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------

class FireCatalog:
    def __init__(self, ws, grid):
        self.ws, self.grid = ws, grid
        self.user_path = ws.root / "fires" / "user_fires.geojson"
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        self._lat = grid.points.latitude.values.astype(float)
        self._lon = grid.points.longitude.values.astype(float)

    # -- storage
    def _load(self, path):
        if not Path(path).exists():
            return []
        try:
            return json.loads(Path(path).read_text(encoding="utf-8")).get("features", [])
        except Exception:
            return []

    def _save_user(self, feats):
        tmp = self.user_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, indent=1), encoding="utf-8")
        from aorc_tools.explorer.runtime import replace_file
        replace_file(tmp, self.user_path)

    def all(self) -> list[dict]:
        user = self._load(self.user_path)
        overrides = {f["properties"]["override_of"]: f for f in user if f["properties"].get("override_of")}
        out = []
        for f in self._load(SEED):
            pid = f["properties"]["id"]
            if pid in overrides:
                o = overrides[pid]
                f = {"type": "Feature", "geometry": o.get("geometry") or f.get("geometry"),
                     "properties": {**f["properties"], **{k: v for k, v in o["properties"].items() if k not in ("id", "override_of")}}}
            f["properties"]["origin"] = "seed"
            out.append(f)
        for f in user:
            if not f["properties"].get("override_of"):
                f["properties"]["origin"] = "imported"
                out.append(f)
        return [self._annotate(f) for f in out]

    def get(self, fid):
        return next((f for f in self.all() if f["properties"]["id"] == fid), None)

    def _annotate(self, f):
        p = f["properties"]
        lat, lon = self.point_of(f)
        p["lat"], p["lon"] = lat, lon
        if lat is None:
            p.update(in_grid=False, cell=None, distance_km=None)
            return f
        d = np.hypot((self._lat - lat) * 111.2, (self._lon - lon) * 111.2 * math.cos(math.radians(lat)))
        i = int(d.argmin())
        p["cell"] = int(self.grid.points.point_id.values[i])
        p["distance_km"] = round(float(d[i]), 3)
        p["in_grid"] = bool(d[i] <= 1.2)
        p["cell_county"] = str(self.grid.points.county.values[i])
        p["zone"] = str(self.grid.points.zone.values[i])
        p["std_offset_h"] = int(self.grid.points.std_offset_h.values[i])
        return f

    @staticmethod
    def point_of(f):
        g = f.get("geometry")
        if not g:
            return None, None
        if g["type"] == "Point":
            lon, lat = g["coordinates"][:2]
            return float(lat), float(lon)
        try:
            from shapely.geometry import shape
            c = shape(g).representative_point()
            return float(c.y), float(c.x)
        except Exception:
            return None, None

    # -- edits
    def set_location(self, fid, lat, lon):
        user = self._load(self.user_path)
        seed_ids = {f["properties"]["id"] for f in self._load(SEED)}
        geom = {"type": "Point", "coordinates": [float(lon), float(lat)]}
        for f in user:
            if f["properties"]["id"] == fid or f["properties"].get("override_of") == fid:
                f["geometry"] = geom
                f["properties"]["location_precision"] = "approximate"
                f["properties"]["location_set"] = now_iso()
                self._save_user(user)
                return self.get(fid)
        if fid in seed_ids:
            user.append({"type": "Feature", "geometry": geom,
                         "properties": {"id": f"{fid}--loc", "override_of": fid, "location_precision": "approximate",
                                        "location_set": now_iso()}})
            self._save_user(user)
            return self.get(fid)
        raise KeyError(fid)

    def delete(self, fid):
        user = self._load(self.user_path)
        keep = [f for f in user if f["properties"]["id"] != fid and f["properties"].get("override_of") != fid]
        if len(keep) == len(user):
            raise KeyError(f"{fid} is not an imported fire (seed fires cannot be removed)")
        self._save_user(keep)

    def import_file(self, filename: str, data: bytes, default_source=None) -> dict:
        name = filename.lower()
        feats = []
        if name.endswith((".geojson", ".json")):
            gj = json.loads(data.decode("utf-8-sig"))
            for f in gj.get("features", []):
                feats.append(self._from_row({k.lower(): v for k, v in (f.get("properties") or {}).items()}, f.get("geometry")))
        else:
            if name.endswith((".xlsx", ".xlsm", ".xls")):
                df = pd.read_excel(io.BytesIO(data))
            else:
                df = pd.read_csv(io.BytesIO(data), encoding_errors="replace")
            for _, r in df.iterrows():
                feats.append(self._from_row({str(k).strip().lower(): v for k, v in r.items()}, None))
        src = default_source or f"imported from {filename}"
        user = self._load(self.user_path)
        existing = {(f["properties"].get("name"), f["properties"].get("discovered")) for f in user}
        added = skipped = outside = 0
        for f in feats:
            if f is None:
                skipped += 1
                continue
            f["properties"].setdefault("source", src)
            key = (f["properties"]["name"], f["properties"]["discovered"])
            if key in existing:
                skipped += 1
                continue
            lat, lon = self.point_of(f)
            if lat is not None:
                d = np.hypot((self._lat - lat) * 111.2, (self._lon - lon) * 111.2 * math.cos(math.radians(lat))).min()
                if d > 1.2:
                    outside += 1
                    continue
            f["properties"]["id"] = re.sub(r"[^a-z0-9]+", "-", f"{f['properties']['name']}-{(f['properties']['discovered'] or 'undated')[:10]}".lower()).strip("-")
            existing.add(key)
            user.append(f)
            added += 1
        self._save_user(user)
        return {"added": added, "skipped": skipped, "outside_study_area": outside, "read": len(feats)}

    def _from_row(self, r: dict, geom):
        def pick(field):
            for a in ALIASES[field]:
                if a in r and r[a] is not None and str(r[a]).strip() not in ("", "nan", "None"):
                    return r[a]
            return None
        when, date_only = parse_when(pick("discovered"), pick("discovery_time"))
        lat, lon = _num(pick("lat")), _num(pick("lon"))
        if geom is None and lat is not None and lon is not None:
            geom = {"type": "Point", "coordinates": [lon, lat]}
        if geom is None and when is None:
            return None
        nm = pick("name")
        typ = str(pick("type") or "WF")
        props = {"name": str(nm).strip().title() if nm is not None else "Unnamed fire",
                 "discovered": when, "date_only": date_only, "acres": _num(pick("acres")),
                 "county": str(pick("county") or ""), "cause": str(pick("cause") or ""),
                 "type": "RX" if typ.upper().startswith(("RX", "PRESCRIBED")) else ("WF" if typ.upper().startswith(("WF", "WILD")) else typ),
                 "source_ref": str(pick("source_ref") or ""),
                 "location_precision": ("perimeter centroid" if geom and geom["type"] != "Point" else ("point of origin" if geom else "unknown"))}
        return {"type": "Feature", "geometry": geom, "properties": props}


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------

def fire_cells(grid, fire, radius=None) -> tuple[list[int], int]:
    p = fire["properties"]
    g = fire.get("geometry")
    pts = grid.points
    if g and g["type"] in ("Polygon", "MultiPolygon"):
        try:
            from shapely import contains_xy
            from shapely.geometry import shape
            inside = contains_xy(shape(g), pts.longitude.values, pts.latitude.values)
            ids = pts.point_id.values[inside].astype(int).tolist()
            if ids:
                return ids[:MAX_CELLS], 0
        except Exception:
            pass
    if p.get("cell") is None:
        return [], 0
    r = RADIUS_BY_PRECISION.get(p.get("location_precision", "point of origin"), 1) if radius is None else int(radius)
    row = pts.loc[pts.point_id == p["cell"]].iloc[0]
    m = (np.abs(pts.lat_idx.values - row.lat_idx) <= r) & (np.abs(pts.lon_idx.values - row.lon_idx) <= r)
    return pts.point_id.values[m].astype(int).tolist(), r


def _daily(times, arr, off_h, var):
    """(n_t, n_c) hourly -> per local-standard-day (dates, (n_d, n_c)) daily max; FWI_daily -> value at 12 LST."""
    lst = pd.DatetimeIndex(times) + pd.Timedelta(hours=off_h)
    days = lst.normalize()
    ud = days.unique()
    out = np.full((len(ud), arr.shape[1]), np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for i, d in enumerate(ud):
            m = days == d
            if var == "FWI_daily":
                m = m & (lst.hour == 12)
                if m.any():
                    out[i] = arr[np.where(m)[0][0]]
            else:
                out[i] = np.nanmax(arr[m], axis=0)
    return ud, out


def _pct_rank(values, ref):
    ref = ref[np.isfinite(ref)]
    if not len(ref) or values is None or not np.isfinite(values):
        return None
    return float(100.0 * (np.sum(ref < values) + 0.5 * np.sum(ref == values)) / len(ref))


def check_fire(fire, *, grid, store, source_name, cfg, live, days_before=14, days_after=3, radius=None,
               spinup_hours=720, domain_end=None) -> dict:
    """``live(pids, start, end, spin, cfg)`` -> ChainResult (store mode). Returns a JSON-able report."""
    p = fire["properties"]
    rep = {"fire": {k: p.get(k) for k in ("id", "name", "county", "discovered", "date_only", "acres", "type", "cause",
                                          "location_precision", "source", "notes", "lat", "lon", "cell", "distance_km", "zone")},
           "source": source_name, "config_id": cfg.id, "config": cfg.as_dict(), "synthetic": source_name == "synthetic",
           "created": now_iso(), "notes": []}
    t_disc = discovery_utc(fire)
    if t_disc is None:
        return {**rep, "status": "no-date", "message": "This record has no discovery date."}
    if not p.get("in_grid"):
        return {**rep, "status": "no-location",
                "message": "Set the fire's location (Fires tab › Set location) — it has no point inside the study grid."}
    pids, r = fire_cells(grid, fire, radius)
    rows = grid.rows_for(pids)
    off = int(grid.points.std_offset_h.values[rows][0])
    if p.get("date_only"):                     # a date-only record names the LOCAL day of discovery
        disc_day = pd.Timestamp(str(p["discovered"])[:10])
        t_disc = (disc_day + pd.Timedelta(hours=12 - off)).to_pydatetime()       # local noon, for display only
    else:
        disc_day = pd.Timestamp((t_disc + timedelta(hours=off)).date())
    start = (disc_day - pd.Timedelta(days=days_before)).to_pydatetime() - timedelta(hours=off)
    end = (disc_day + pd.Timedelta(days=days_after + 1)).to_pydatetime() - timedelta(hours=off) - timedelta(hours=1)
    if domain_end is not None and pd.Timestamp(start) > pd.Timestamp(domain_end):
        return {**rep, "status": "beyond-data", "message": f"The discovery date is after the AORC data available to this workspace (ends {domain_end})."}
    if domain_end is not None:
        end = min(pd.Timestamp(end), pd.Timestamp(domain_end)).to_pydatetime()
    rep["cells"] = {"point_ids": pids, "n": len(pids), "radius_cells": r,
                    "basis": "cells inside the perimeter" if r == 0 else f"{2 * r + 1}×{2 * r + 1} cells around the point ({(2 * r + 1) * 0.8:.1f} km box)"}
    rep["window"] = {"start": start.isoformat(), "end": pd.Timestamp(end).isoformat(), "days_before": days_before, "days_after": days_after,
                     "discovery_utc": t_disc.isoformat() + "Z", "discovery_local_day": str(disc_day.date()), "lst_offset_h": off}

    # ---- window values: saved results if complete, otherwise computed now
    times, steps, frac, _ = store.series(source_name, cfg.id, pids, start, end)
    origin = "saved results"
    if frac < 0.9999:
        res = live(pids, start, end, spinup_hours, cfg)
        m = res.analysis_mask
        times = res.times[m]
        steps = {k: np.asarray(v)[m] for k, v in res.steps.items()}
        origin = f"computed now ({spinup_hours} h spin-up)"
        rep["notes"].append("The window was not fully saved, so it was computed now with a cold start and spin-up; "
                            "saved results continue the moisture codes from earlier in the season.")
    rep["window"]["origin"] = origin
    times = pd.DatetimeIndex(times)
    origin_col = pids.index(p["cell"]) if p.get("cell") in pids else 0

    # ---- season reference (saved results only)
    yr = disc_day.year
    s_start = datetime(yr, SEASON[0], 1) - timedelta(hours=off)
    s_end = datetime(yr, SEASON[1] + 1, 1) - timedelta(hours=off) - timedelta(hours=1)
    if domain_end is not None:
        s_end = min(pd.Timestamp(s_end), pd.Timestamp(domain_end)).to_pydatetime()
    st_times, st_steps, st_frac, _ = store.series(source_name, cfg.id, pids, s_start, s_end)
    rep["season"] = {"start_local": f"{yr}-{SEASON[0]:02d}-01", "end_local": f"{yr}-{SEASON[1]:02d}-31",
                     "saved_fraction": round(float(st_frac), 4)}
    if st_frac < 0.5:
        rep["notes"].append(f"Only {100 * st_frac:.0f}% of the {yr} fire season is saved for these cells, so percentile ranks "
                            "are not available yet — use 'Calculate this fire season' (one job) and re-run the check.")

    out = {}
    for v in CHECK_VARS:
        if v not in steps:
            continue
        a = np.asarray(steps[v], dtype=float)
        days, dmax = _daily(times, a, off, v)
        di = np.where(days == disc_day)[0]
        e = {"label": LABEL[v], "daily": {"days": [str(d.date()) for d in days],
                                          "median": [None if not np.isfinite(x) else round(float(x), 2) for x in np.nanmedian(dmax, axis=1)],
                                          "max": [None if not np.isfinite(x) else round(float(x), 2) for x in np.nanmax(np.where(np.isfinite(dmax), dmax, -np.inf), axis=1)],
                                          "origin": [None if not np.isfinite(x) else round(float(x), 2) for x in dmax[:, origin_col]]}}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            if len(di):
                drow = dmax[di[0]]
                e["disc_day"] = {"median": _r(np.nanmedian(drow)), "max": _r(np.nanmax(drow)), "origin": _r(drow[origin_col])}
                prior = dmax[max(0, di[0] - 3):di[0]]
                e["prior3_max"] = _r(np.nanmax(prior)) if prior.size and np.isfinite(prior).any() else None
            if not p.get("date_only"):
                hi = times.get_indexer([pd.Timestamp(t_disc).floor("h")])[0]
                if hi >= 0:
                    e["at_discovery"] = {"median": _r(np.nanmedian(a[hi])), "max": _r(np.nanmax(a[hi])), "origin": _r(a[hi, origin_col]),
                                         "hour_utc": str(times[hi])}
        # rank vs the saved season (per cell, then median across cells)
        if st_frac >= 0.5 and v in st_steps and "disc_day" in e:
            sd, smax = _daily(pd.DatetimeIndex(st_times), np.asarray(st_steps[v], dtype=float), off, v)
            all_ranks = [_pct_rank(dmax[di[0], c], smax[:, c]) for c in range(len(pids))]
            ranks = [x for x in all_ranks if x is not None]
            if ranks:
                e["season_rank"] = {"median": round(float(np.median(ranks)), 1), "origin": _r(all_ranks[origin_col], 1),
                                    "min": round(float(np.min(ranks)), 1), "max": round(float(np.max(ranks)), 1),
                                    "n_days": int(np.isfinite(smax).any(axis=1).sum())}
                e["rank_class"] = _cls(e["season_rank"]["median"], RANK_CLASSES)
        if v in ("FWI", "FWI_daily") and "disc_day" in e:
            e["fwi_class"] = _cls(e["disc_day"]["median"], FWI_CLASSES)
        out[v] = e
    rep["indices"] = out

    # ---- reference values recorded with the fire
    refs = []
    for ref in p.get("reference", []) or []:
        q = ref.get("quantity")
        if q not in steps:
            continue
        a = np.asarray(steps[q], dtype=float)[:, origin_col]
        got, at = None, None
        if "time" in ref:
            i = times.get_indexer([pd.Timestamp(ref["time"]).tz_localize(None) if pd.Timestamp(ref["time"]).tzinfo else pd.Timestamp(ref["time"])])[0]
            got, at = (_r(a[i]), str(times[i])) if i >= 0 else (None, None)
        elif "peak_day" in ref:
            d0 = pd.Timestamp(ref["peak_day"])
            m = (times >= d0) & (times < d0 + pd.Timedelta(days=1))          # UTC day, as the reference was stated
            if m.any() and np.isfinite(a[m]).any():
                j = int(np.nanargmax(np.where(m, a, -np.inf)))
                got, at = _r(a[j]), str(times[j])
        elif "day" in ref:
            d0 = pd.Timestamp(ref["day"])
            dd, dm = _daily(times, a[:, None], off, q)
            k = np.where(dd == d0)[0]
            got, at = (_r(dm[k[0], 0]), str(d0.date())) if len(k) else (None, None)
        if got is None:
            continue
        diff = got - ref["value"]
        refs.append({**ref, "ours": got, "at": at, "diff": round(diff, 2),
                     "agrees": bool(abs(diff) <= ref.get("tolerance", 3.0))})
    rep["reference_checks"] = refs

    # ---- verdict
    ranked = {v: e["season_rank"]["median"] for v, e in out.items() if "season_rank" in e}
    if ranked:
        hi = [v for v, x in ranked.items() if x >= 80]
        rep["verdict"] = {"kind": "elevated" if len(hi) >= 2 else ("mixed" if hi else "not-elevated"),
                          "text": (f"{len(hi)} of {len(ranked)} indices put the discovery day at or above the 80th percentile of the "
                                   f"{yr} season at these cells" + (f" ({', '.join(hi)})." if hi else ".")),
                          "ranks": ranked}
        if not hi:
            rep["verdict"]["text"] += (" Not elevated: worth checking the ignition cause (human ignitions also happen on ordinary "
                                       "days), the reported date, and the location.")
    else:
        fw = out.get("FWI_daily", {}).get("disc_day", {}).get("median")
        rep["verdict"] = {"kind": "unranked", "text": "Season not saved yet, so no percentile ranks. "
                          + (f"Classic daily FWI on the discovery day: {fw} ({_cls(fw, FWI_CLASSES)})." if fw is not None else "")}
    if rep["synthetic"]:
        rep["notes"].insert(0, "SYNTHETIC demo weather — these numbers say nothing about the real fire.")
    rep["status"] = "ok"
    return rep


def _r(x, nd=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def season_job_spec(grid, fire, radius=None, domain_end=None):
    """Bricks and period for 'calculate this fire season' (local Apr 1 .. Oct 31 of the discovery year)."""
    pids, _ = fire_cells(grid, fire, radius)
    if not pids:
        raise ValueError("fire has no location in the study grid")
    t = discovery_utc(fire)
    if t is None:
        raise ValueError("fire has no discovery date")
    off = int(fire["properties"].get("std_offset_h", -5))
    start = datetime(t.year, SEASON[0], 1) - timedelta(hours=off)
    end = datetime(t.year, SEASON[1] + 1, 1) - timedelta(hours=off) - timedelta(hours=1)
    if domain_end is not None:
        end = min(pd.Timestamp(end), pd.Timestamp(domain_end)).to_pydatetime()
    bricks = sorted({f"{grid.points.ci.values[i]}_{grid.points.cj.values[i]}" for i in grid.rows_for(pids)})
    return {"bricks": bricks, "start": start, "end": end, "name": f"Fire season · {fire['properties']['name']} {t.year}"}


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def summary_rows(reports) -> list[dict]:
    rows = []
    for r in reports:
        f = r.get("fire", {})
        base = {"fire_id": f.get("id"), "name": f.get("name"), "county": f.get("county"), "discovered": f.get("discovered"),
                "acres": f.get("acres"), "cause": f.get("cause"), "status": r.get("status"), "source": r.get("source"),
                "config_id": r.get("config_id"), "cells": r.get("cells", {}).get("n"), "window_origin": r.get("window", {}).get("origin"),
                "season_saved": r.get("season", {}).get("saved_fraction"), "verdict": r.get("verdict", {}).get("kind")}
        if r.get("status") != "ok":
            rows.append({**base, "index": "", "message": r.get("message", "")})
            continue
        for v, e in r["indices"].items():
            rows.append({**base, "index": v,
                         "at_discovery_median": (e.get("at_discovery") or {}).get("median"),
                         "disc_day_max_median": (e.get("disc_day") or {}).get("median"),
                         "disc_day_max_origin": (e.get("disc_day") or {}).get("origin"),
                         "prior3_max": e.get("prior3_max"),
                         "season_rank_median": (e.get("season_rank") or {}).get("median"),
                         "season_rank_origin": (e.get("season_rank") or {}).get("origin"),
                         "season_days": (e.get("season_rank") or {}).get("n_days"),
                         "rank_class": e.get("rank_class"), "fwi_class": e.get("fwi_class")})
    return rows


def html_report(reports, title="WUP known-fire checks") -> str:
    esc = lambda s: (str(s) if s is not None else "—").replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>{esc(title)}</title><style>body{{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1100px;color:#1d1f21}}"
             "table{border-collapse:collapse;width:100%;margin:8px 0 18px}td,th{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left}"
             "th{background:#f3f3f1}.warn{background:#fff3d6;border-left:4px solid #c98500;padding:8px 10px;margin:8px 0}"
             ".ok{color:#1d7a4f}.bad{color:#b8452d}h2{margin-top:28px}small{color:#666}</style></head><body>",
             f"<h1>{esc(title)}</h1><p><small>Generated {esc(now_iso())} by the WUP Grid Explorer. Ranks are the discovery day's "
             "percentile among all days of the same fire season (Apr–Oct, local standard days) at the fire's cells, from saved results. "
             "Case-based plausibility check, not skill validation.</small></p>"]
    if any(r.get("synthetic") for r in reports):
        parts.append("<div class='warn'><b>Synthetic demo weather</b> was used for at least one check — those numbers are not real.</div>")
    parts.append("<h2>Summary</h2><table><tr><th>Fire</th><th>Discovered</th><th>Acres</th><th>Cause</th>"
                 "<th>Classic daily FWI</th><th>FWI2025 day max</th><th>FPI</th><th>ERC</th><th>BI</th><th>Verdict</th></tr>")
    for r in reports:
        f = r.get("fire", {})
        if r.get("status") != "ok":
            parts.append(f"<tr><td>{esc(f.get('name'))}</td><td>{esc(f.get('discovered'))}</td><td>{esc(f.get('acres'))}</td>"
                         f"<td>{esc(f.get('cause'))}</td><td colspan='6'>{esc(r.get('message'))}</td></tr>")
            continue

        def cell(v):
            e = r["indices"].get(v, {})
            val = (e.get("disc_day") or {}).get("median")
            rk = (e.get("season_rank") or {}).get("median")
            tail = "" if rk is None else " <small>({:.0f}th pct)</small>".format(rk)
            return f"<td>{esc(val)}{tail}</td>"
        parts.append(f"<tr><td>{esc(f.get('name'))}</td><td>{esc(f.get('discovered'))}</td><td>{esc(f.get('acres'))}</td><td>{esc(f.get('cause'))}</td>"
                     + "".join(cell(v) for v in ("FWI_daily", "FWI", "FPI", "ERC", "BI")) + f"<td>{esc(r.get('verdict', {}).get('kind'))}</td></tr>")
    parts.append("</table>")
    for r in reports:
        f = r.get("fire", {})
        parts.append(f"<h2>{esc(f.get('name'))} · {esc(f.get('discovered'))}</h2>")
        if r.get("status") != "ok":
            parts.append(f"<p>{esc(r.get('message'))}</p>")
            continue
        parts.append(f"<p>{esc(f.get('county'))} County · {esc(f.get('acres'))} ac · cause {esc(f.get('cause') or 'n/a')} · "
                     f"{esc(r['cells']['basis'])} · window {esc(r['window']['origin'])} · source {esc(r['source'])} · config {esc(r['config_id'])}</p>"
                     f"<p><b>{esc(r.get('verdict', {}).get('text'))}</b></p>")
        for n in r.get("notes", []):
            parts.append(f"<div class='warn'>{esc(n)}</div>")
        parts.append("<table><tr><th>Index</th><th>At discovery (median of cells)</th><th>Discovery-day max</th><th>Origin cell</th>"
                     "<th>Max, 3 days before</th><th>Season rank (median, range)</th><th>Class</th></tr>")
        for v, e in r["indices"].items():
            sr = e.get("season_rank")
            srt = "—" if not sr else "{:.0f} ({:.0f}–{:.0f}) of {} days".format(sr["median"], sr["min"], sr["max"], sr["n_days"])
            at = (e.get("at_discovery") or {}).get("median")
            dd = e.get("disc_day") or {}
            parts.append(f"<tr><td>{esc(e['label'])}</td><td>{esc(at)}</td><td>{esc(dd.get('median'))}</td><td>{esc(dd.get('origin'))}</td>"
                         f"<td>{esc(e.get('prior3_max'))}</td><td>{srt}</td><td>{esc(e.get('rank_class') or e.get('fwi_class'))}</td></tr>")
        parts.append("</table>")
        if r.get("reference_checks"):
            parts.append("<table><tr><th>Reference</th><th>Recorded</th><th>This tool</th><th>Difference</th><th>Agrees (±tol)</th><th>Basis</th></tr>")
            for c in r["reference_checks"]:
                parts.append(f"<tr><td>{esc(c['label'])}</td><td>{esc(c['value'])}</td><td>{esc(c['ours'])} <small>{esc(c['at'])}</small></td>"
                             f"<td>{esc(c['diff'])}</td><td class='{('ok' if c['agrees'] else 'bad')}'>{('yes' if c['agrees'] else 'no')} (±{c.get('tolerance', 3)})</td>"
                             f"<td><small>{esc(c.get('basis'))}</small></td></tr>")
            parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)
