"""
Power lines and fire danger: where do the lines run through the places, seasons and years with the highest fire
danger, and is that danger increasing?

Transmission lines first; any line layer (distribution lines from a utility, a proposed route) works the same way.

"Risk" follows the project's working definition: the SEASONALITY and INTENSITY of fire danger, and how they change.
For every index (hourly FWI as its daily maximum, classic daily FWI at 12:00 LST, ERC, BI, FPI) and every year:

* intensity:
  * the annual (global) maximum of the daily maxima and its date;
  * the spring and fall (local) maxima and the summer (local) and season (global) minima of the 15-day smoothed
    series;
  * the number of days above the 90th / 95th / 99th percentile of all study cells' daily maxima (March-November,
    pooled over the reference years: one regional scale, so places can be compared).
* seasonality:
  * the date of each peak and trough;
  * the first and last day above the 90th percentile and the span between them (the high-danger season);
  * the mean seasonal cycle;
  * the local hour at which the index peaks on high-danger days.
* recovery: the daily MINIMUM of each hourly index (how far danger falls overnight): its season mean and the days
  whose minimum stays above the region's 90th percentile of daily minima (nights that do not recover);
* change: the Theil-Sen slope per decade and the Mann-Kendall test of each yearly metric. Per-cell significance is
  controlled for the false discovery rate (Benjamini-Hochberg, as recommended by Wilks 2016 for maps).

Lines are mapped to the ~800 m AORC cells they cross (km of line in each cell). A line's or the corridor's value is
the length-weighted mean over its cells; the region's value is the mean over all study cells, so the two can be
compared year by year.

These are fire-WEATHER danger measures. They say where and when conditions favour fire if an ignition occurs. They
are not ignition probability (that needs line age, conductor type, vegetation contact and outage records) and not
consequence (fuels, homes, egress). They are one input to a risk model, not the model.
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.hdf import HDF_LOCK
from aorc_tools.explorer.runtime import replace_file
from aorc_tools.explorer.workspace import now_iso

logger = logging.getLogger(__name__)

BUNDLED_DIR = Path(__file__).resolve().parent / "static" / "geo" / "lines"
INDICES = ["FWI", "FWI_daily", "ERC", "BI", "FPI"]
LABEL = {"FWI": "FWI (hourly, daily max)", "FWI_daily": "FWI (classic daily, 12:00 LST)", "ERC": "ERC", "BI": "BI",
         "FPI": "FPI"}
PCTS = (90, 95, 99)
SEASON = ((3, 1), (11, 30))          # March 1 - November 30, local standard days
SPRING = ((3, 1), (6, 30))
SUMMER = ((6, 1), (8, 31))
FALL = ((8, 1), (11, 30))
SMOOTH_DAYS = 15
MIN_SEASON_FRACTION = 0.9            # a year counts for a cell when 90% of its Mar-Nov days are saved
MIN_TREND_YEARS = 10
FDR_Q = 0.10
SAMPLE_KM = 0.1                      # lines are sampled every 100 m to find the cells they cross
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")

# yearly metrics (per cell, per index); "doy" metrics are days since Jan 1 (0 = Jan 1)
METRICS = {
    "annual_max": "annual maximum of the daily maxima",
    "annual_max_doy": "date of the annual maximum (day of year)",
    "season_mean": "mean daily maximum, March-November",
    "days_p90": "days above the regional 90th percentile",
    "days_p95": "days above the regional 95th percentile",
    "days_p99": "days above the regional 99th percentile",
    "first_p90_doy": "first day above the 90th percentile (day of year)",
    "last_p90_doy": "last day above the 90th percentile (day of year)",
    "span_p90": "days from the first to the last day above the 90th percentile",
    "spring_peak": "spring (Mar-Jun) local maximum of the 15-day smoothed series",
    "spring_peak_doy": "date of the spring peak (day of year)",
    "summer_trough": "summer (Jun-Aug) local minimum of the smoothed series",
    "summer_trough_doy": "date of the summer trough (day of year)",
    "fall_peak": "fall (Aug-Nov) local maximum of the smoothed series",
    "fall_peak_doy": "date of the fall peak (day of year)",
    "season_min": "season (Mar-Nov) minimum of the smoothed series",
    "peak_hour_lst": "median local standard hour of the daily maximum on days above the 90th percentile",
    "min_season_mean": "mean daily MINIMUM, March-November (overnight recovery; higher = poorer recovery)",
    "days_min_p90": "days whose daily minimum is above the regional 90th percentile of daily minima (nights that do not recover)",
}
TREND_METRICS = ["days_p90", "days_p95", "annual_max", "season_mean", "spring_peak", "fall_peak", "first_p90_doy",
                 "last_p90_doy", "span_p90", "min_season_mean", "days_min_p90"]


# =============================================================================================== line layers

def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _voltage_kv(props: dict):
    """kV from HIFLD VOLTAGE (kV; -999999 = unknown) or OSM voltage (volts, may list several: '138000;69000')."""
    for k in ("voltage_kv", "VOLTAGE", "voltage", "Voltage", "VOLT_KV", "KV", "kv"):
        if k not in props or props[k] in (None, ""):
            continue
        v = props[k]
        vals = [_num(x) for x in re.split(r"[;,/ ]+", str(v)) if x.strip()] if isinstance(v, str) else [_num(v)]
        vals = [x for x in vals if x is not None and x > 0]
        if not vals:
            continue
        x = max(vals)
        return round(x / 1000.0, 1) if (k in ("voltage", "Voltage") and x >= 1000) else round(x, 1)
    return None


def _pick(props, *keys):
    for k in keys:
        v = props.get(k)
        if v not in (None, "", "NOT AVAILABLE", "UNKNOWN", -999999):
            return v
    return None


def normalize_feature(f: dict, i: int, source_tag: str) -> dict | None:
    g = f.get("geometry") or {}
    if g.get("type") not in ("LineString", "MultiLineString"):
        return None
    p = dict(f.get("properties") or {})
    loc = str(_pick(p, "location", "TYPE", "type") or "").lower()
    kind = str(_pick(p, "power") or "").lower()
    out = {
        "id": str(_pick(p, "id", "ID", "@id", "osm_id") or f"{source_tag}-{i:05d}"),
        "name": _pick(p, "name", "NAME", "LINE_NAME"),
        "voltage_kv": _voltage_kv(p),
        "volt_class": _pick(p, "volt_class", "VOLT_CLASS"),
        "owner": _pick(p, "owner", "OWNER", "operator", "OPERATOR"),
        "status": _pick(p, "status", "STATUS"),
        "underground": bool("underground" in loc or kind == "cable"),
        "kind": kind or ("transmission" if _voltage_kv(p) and _voltage_kv(p) >= 69 else None),
        "sub_1": _pick(p, "sub_1", "SUB_1"),
        "sub_2": _pick(p, "sub_2", "SUB_2"),
        "county": _pick(p, "county", "COUNTY"),
        "source_date": _pick(p, "source_date", "SOURCEDATE", "VAL_DATE", "timestamp"),
    }
    out = {k: v for k, v in out.items() if v is not None}
    for k, v in p.items():                                   # cross-check results stay top-level
        if (k.startswith("alt_") or "_best" in k) and v is not None:
            out[k] = v
    keep = {k: v for k, v in p.items() if isinstance(v, (str, int, float, bool)) and k not in out and len(str(v)) < 200}
    if keep:
        out["attrs"] = keep
    return {"type": "Feature", "geometry": g, "properties": out}


def _lines_of(geom):
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    return list(geom["coordinates"])


def line_length_km(geom) -> float:
    tot = 0.0
    for ln in _lines_of(geom):
        a = np.asarray(ln, dtype=float)[:, :2]
        if len(a) < 2:
            continue
        kx = 111.2 * np.cos(np.radians(a[:, 1].mean()))
        tot += float(np.hypot(np.diff(a[:, 0]) * kx, np.diff(a[:, 1]) * 111.2).sum())
    return tot


def _read_shapefile_zip(data: bytes) -> list[dict]:
    """Features (WGS84) from a zipped shapefile: pyshp reads it, rasterio's GDAL reprojects using the .prj."""
    import shapefile                                                   # pyshp
    z = zipfile.ZipFile(io.BytesIO(data))
    shp = [n for n in z.namelist() if n.lower().endswith(".shp") and not n.startswith("__MACOSX")]
    if not shp:
        raise ValueError("no .shp file in the zip")
    base = shp[0][:-4]
    get = lambda ext: z.read(next(n for n in z.namelist() if n.lower() == (base + ext).lower()))  # noqa: E731
    r = shapefile.Reader(shp=io.BytesIO(get(".shp")), dbf=io.BytesIO(get(".dbf")), shx=io.BytesIO(get(".shx")))
    try:
        prj = get(".prj").decode("utf-8", "ignore")
    except StopIteration:
        prj = None
    fields = [f[0] for f in r.fields[1:]]
    feats = []
    for sr in r.iterShapeRecords():
        geo = sr.shape.__geo_interface__
        props = dict(zip(fields, sr.record))
        feats.append({"type": "Feature", "geometry": geo, "properties": props})
    if prj and "GEOGCS" not in prj.split("PROJCS")[0][:10] and prj.strip().startswith("PROJCS"):
        from rasterio.crs import CRS
        from rasterio.warp import transform_geom
        src = CRS.from_wkt(prj)
        for f in feats:
            f["geometry"] = transform_geom(src, "EPSG:4326", f["geometry"])
    return feats


def _read_with_pyogrio(name: str, data: bytes) -> list[dict]:
    """GeoPackage / File Geodatabase (zipped .gdb folder) / anything GDAL reads, when pyogrio is installed."""
    try:
        import pyogrio
    except ImportError:
        raise ValueError("This format needs the optional package pyogrio (pip install pyogrio). "
                         "Or export the layer as GeoJSON or a zipped shapefile (ArcGIS: Features To JSON).") from None
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / Path(name).name
        p.write_bytes(data)
        src = p
        if name.lower().endswith(".zip"):
            zipfile.ZipFile(p).extractall(td)
            gdbs = [d for d in Path(td).rglob("*.gdb") if d.is_dir()]
            gpk = list(Path(td).rglob("*.gpkg"))
            src = gdbs[0] if gdbs else (gpk[0] if gpk else p)
        layers = [ly for ly, gt in pyogrio.list_layers(src) if "line" in str(gt).lower()]
        if not layers:
            raise ValueError("no line layer found")
        js = pyogrio.read_dataframe(src, layer=layers[0]).to_crs(4326).to_json(drop_id=True)
    return json.loads(js)["features"]


class LineLayers:
    """Line layers: bundled ones (read-only) plus imported / downloaded ones in <workspace>/lines/layers."""

    def __init__(self, ws, grid):
        self.ws, self.grid = ws, grid
        self.dir = ws.root / "lines" / "layers"
        self.dir.mkdir(parents=True, exist_ok=True)
        pts = grid.points
        self._bbox = (float(pts.longitude.min()) - 0.05, float(pts.latitude.min()) - 0.05,
                      float(pts.longitude.max()) + 0.05, float(pts.latitude.max()) + 0.05)

    @staticmethod
    def check_id(lid: str) -> str:
        if not isinstance(lid, str) or not SLUG.match(lid):
            raise ValueError(f"invalid layer id {lid!r}")
        return lid

    def _paths(self, lid):
        lid = self.check_id(lid)
        for d, origin in ((self.dir, "workspace"), (BUNDLED_DIR, "bundled")):
            g = d / f"{lid}.geojson"
            if g.exists():
                return g, d / f"{lid}.json", origin
        return None, None, None

    def list(self) -> list[dict]:
        out, seen = [], set()
        for d, origin in ((self.dir, "workspace"), (BUNDLED_DIR, "bundled")):
            if not d.exists():
                continue
            for m in sorted(d.glob("*.json")):
                if m.stem in seen or not (d / f"{m.stem}.geojson").exists():
                    continue
                try:
                    meta = json.loads(m.read_text(encoding="utf-8"))
                except Exception:                                      # noqa: BLE001
                    continue
                seen.add(m.stem)
                out.append({**meta, "id": m.stem, "origin": origin})
        return out

    def meta(self, lid) -> dict:
        g, m, origin = self._paths(lid)
        if g is None:
            raise KeyError(f"no line layer {lid}")
        meta = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
        return {**meta, "id": lid, "origin": origin}

    def get(self, lid) -> dict:
        g, _, _ = self._paths(lid)
        if g is None:
            raise KeyError(f"no line layer {lid}")
        return json.loads(g.read_text(encoding="utf-8"))

    def save(self, lid, features: list[dict], meta: dict, source_tag="line") -> dict:
        """Normalise, keep what touches the study area, write <lid>.geojson + <lid>.json atomically."""
        self.check_id(lid)
        from shapely.geometry import box, shape, mapping
        area = box(*self._bbox)
        feats = []
        for i, f in enumerate(features):
            nf = normalize_feature(f, i, source_tag)
            if nf is None:
                continue
            geom = shape(nf["geometry"])
            if not geom.intersects(area):
                continue
            clipped = geom.intersection(area)
            if clipped.is_empty or clipped.geom_type not in ("LineString", "MultiLineString"):
                continue
            nf["geometry"] = mapping(clipped)
            nf["geometry"] = json.loads(json.dumps(nf["geometry"]))          # tuples -> lists
            nf["properties"]["length_km"] = round(line_length_km(nf["geometry"]), 3)
            feats.append(nf)
        if not feats:
            raise ValueError("no line features inside the study area")
        ids = [f["properties"]["id"] for f in feats]
        if len(set(ids)) != len(ids):                                  # make ids unique (county splits etc.)
            seen = {}
            for f in feats:
                k = f["properties"]["id"]
                seen[k] = seen.get(k, 0) + 1
                if seen[k] > 1:
                    f["properties"]["id"] = f"{k}#{seen[k]}"
        km = sum(f["properties"]["length_km"] for f in feats)
        kv = [f["properties"].get("voltage_kv") for f in feats]
        meta = {**meta, "n_features": len(feats), "km": round(km, 1), "saved": now_iso(),
                "with_voltage": sum(1 for x in kv if x), "with_owner": sum(1 for f in feats if f["properties"].get("owner")),
                "voltages_kv": sorted({x for x in kv if x}), "owners": sorted({str(f["properties"]["owner"]) for f in feats
                                                                            if f["properties"].get("owner")})[:40]}
        for path, payload in ((self.dir / f"{lid}.geojson", {"type": "FeatureCollection", "name": lid, "features": feats}),
                              (self.dir / f"{lid}.json", meta)):
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            replace_file(tmp, path)
        return {**meta, "id": lid, "origin": "workspace"}

    def import_file(self, filename: str, data: bytes, name=None) -> dict:
        low = filename.lower()
        if low.endswith((".geojson", ".json")):
            js = json.loads(data.decode("utf-8-sig"))
            feats = js.get("features", []) if js.get("type") == "FeatureCollection" else [js]
            crs = (js.get("crs") or {}).get("properties", {}).get("name", "")
            if crs and not re.search(r"4326|CRS84", crs):
                raise ValueError(f"GeoJSON must be in longitude/latitude (WGS84); this one says {crs}")
        elif low.endswith(".zip"):
            z = zipfile.ZipFile(io.BytesIO(data))
            names = [n.lower() for n in z.namelist()]
            if any(n.endswith(".shp") for n in names) and not any(".gdb/" in n for n in names):
                feats = _read_shapefile_zip(data)
            else:
                feats = _read_with_pyogrio(filename, data)
        elif low.endswith(".gpkg"):
            feats = _read_with_pyogrio(filename, data)
        else:
            raise ValueError("Import GeoJSON, a zipped shapefile, or (with pyogrio) a GeoPackage / zipped .gdb")
        stem = re.sub(r"[^a-z0-9_-]+", "-", Path(filename).stem.lower()).strip("-")[:60] or "lines"
        lid = f"import-{stem}"
        return self.save(lid, feats, {"name": name or Path(filename).name, "source": f"imported file {filename}",
                                      "imported": now_iso()}, source_tag=stem)

    def delete(self, lid):
        g, m, origin = self._paths(lid)
        if origin != "workspace":
            raise ValueError("bundled layers cannot be deleted")
        g.unlink(missing_ok=True)
        m.unlink(missing_ok=True)


# =============================================================================================== lines -> cells

class CellIndex:
    """Nearest study cell of a lon/lat on the regular AORC grid (no search tree needed)."""

    def __init__(self, grid):
        p = grid.points
        self.grid = grid
        self.la0, self.lo0 = int(p.lat_idx.min()), int(p.lon_idx.min())
        ny, nx = int(p.lat_idx.max()) - self.la0 + 1, int(p.lon_idx.max()) - self.lo0 + 1
        self.lut = np.full((ny, nx), -1, dtype=np.int64)
        self.lut[p.lat_idx.values - self.la0, p.lon_idx.values - self.lo0] = np.arange(len(p))

    def rows(self, lon, lat) -> np.ndarray:
        """Row in grid.points for each point (-1 = not a study cell: Lake Superior, outside the counties)."""
        g = self.grid
        iy = np.rint((np.asarray(lat) - g.lat0) / g.step).astype(np.int64) - self.la0
        ix = np.rint((np.asarray(lon) - g.lon0) / g.step).astype(np.int64) - self.lo0
        ok = (iy >= 0) & (iy < self.lut.shape[0]) & (ix >= 0) & (ix < self.lut.shape[1])
        out = np.full(iy.shape, -1, dtype=np.int64)
        out[ok] = self.lut[iy[ok], ix[ok]]
        return out


def sample_line(geom, step_km=SAMPLE_KM):
    """Points every ``step_km`` along the line (segment midpoints), each standing for ``step_km`` of line."""
    lons, lats, wts = [], [], []
    for ln in _lines_of(geom):
        a = np.asarray(ln, dtype=float)[:, :2]
        for (x0, y0), (x1, y1) in zip(a[:-1], a[1:]):
            kx = 111.2 * math.cos(math.radians((y0 + y1) / 2))
            seg = math.hypot((x1 - x0) * kx, (y1 - y0) * 111.2)
            if seg <= 0:
                continue
            n = max(1, int(math.ceil(seg / step_km)))
            t = (np.arange(n) + 0.5) / n
            lons.append(x0 + (x1 - x0) * t)
            lats.append(y0 + (y1 - y0) * t)
            wts.append(np.full(n, seg / n))
    if not lons:
        return np.array([]), np.array([]), np.array([])
    return np.concatenate(lons), np.concatenate(lats), np.concatenate(wts)


def features_to_cells(fc: dict, grid, index: CellIndex | None = None) -> dict:
    """{feature_id: {"rows": int array, "km": float array, "km_total": .., "km_outside": ..}}."""
    index = index or CellIndex(grid)
    out = {}
    for f in fc["features"]:
        lon, lat, w = sample_line(f["geometry"])
        rows = index.rows(lon, lat) if len(lon) else np.array([], dtype=np.int64)
        inside = rows >= 0
        if inside.any():
            u, inv = np.unique(rows[inside], return_inverse=True)
            km = np.bincount(inv, weights=w[inside])
        else:
            u, km = np.array([], dtype=np.int64), np.array([])
        out[f["properties"]["id"]] = {"rows": u, "km": km, "km_total": float(w.sum()),
                                      "km_outside": float(w[~inside].sum()) if len(w) else 0.0}
    return out


def cross_attributes(layers: LineLayers, target_id: str, other_id: str, max_m: float = 150.0) -> dict:
    """Fill gaps in one layer's owner / voltage from another layer that follows the same routes (e.g. HIFLD lines
    checked against OpenStreetMap). Each target line is sampled every 100 m; a sample matches the nearest other-layer
    line within ``max_m``. The matched lines' owner and voltage are voted on, weighted by length. The result is a new
    layer with ``alt_owner``, ``alt_voltage_kv``, ``alt_match_share`` (share of the line's length matched), and
    ``owner_best`` / ``voltage_best_kv`` (the target's own value when it has one)."""
    from shapely import STRtree, points
    from shapely.geometry import LineString, MultiLineString
    tgt, oth = layers.get(target_id), layers.get(other_id)
    tmeta, ometa = layers.meta(target_id), layers.meta(other_id)
    lat0 = 46.7
    kx, ky = 111.2 * math.cos(math.radians(lat0)), 111.2

    def km_geom(g):
        ls = [LineString([(x * kx, y * ky) for x, y, *_ in ln]) for ln in _lines_of(g) if len(ln) >= 2]
        return ls[0] if len(ls) == 1 else MultiLineString(ls)
    ofeat = [f for f in oth["features"]]
    tree = STRtree([km_geom(f["geometry"]) for f in ofeat])
    out, n_filled_owner, n_filled_kv, n_disagree = [], 0, 0, 0
    for f in tgt["features"]:
        p = dict(f["properties"])
        lon, lat, w = sample_line(f["geometry"])
        if len(lon):
            pts = points(np.c_[lon * kx, lat * ky])
            ii, jj = tree.query_nearest(pts, max_distance=max_m / 1000.0, all_matches=False)
            share = float(w[ii].sum() / w.sum()) if len(ii) else 0.0
            votes_o, votes_v = {}, {}
            for i, j in zip(ii, jj):
                op = ofeat[j]["properties"]
                if op.get("owner"):
                    votes_o[op["owner"]] = votes_o.get(op["owner"], 0.0) + w[i]
                if op.get("voltage_kv"):
                    votes_v[op["voltage_kv"]] = votes_v.get(op["voltage_kv"], 0.0) + w[i]
            alt_o = max(votes_o, key=votes_o.get) if votes_o else None
            alt_v = max(votes_v, key=votes_v.get) if votes_v else None
        else:
            share, alt_o, alt_v = 0.0, None, None
        p.update(alt_layer=other_id, alt_match_share=round(share, 3))
        if alt_o:
            p["alt_owner"] = alt_o
        if alt_v:
            p["alt_voltage_kv"] = alt_v
        if p.get("owner"):
            p["owner_best"] = p["owner"]
        elif alt_o and share >= 0.5:
            p["owner_best"] = alt_o
            n_filled_owner += 1
        if p.get("voltage_kv"):
            p["voltage_best_kv"] = p["voltage_kv"]
            if alt_v and share >= 0.5 and abs(float(alt_v) - float(p["voltage_kv"])) > 1:
                p["alt_voltage_disagrees"] = True
                n_disagree += 1
        elif alt_v and share >= 0.5:
            p["voltage_best_kv"] = alt_v
            n_filled_kv += 1
        out.append({"type": "Feature", "geometry": f["geometry"], "properties": p})
    lid = re.sub(r"[^a-z0-9_-]+", "-", f"{target_id}-x-{other_id}".lower())[:80]
    meta = {"name": f"{tmeta.get('name', target_id)} + cross-check with {ometa.get('name', other_id)}",
            "kind": tmeta.get("kind"), "source": f"{tmeta.get('source', '')} | gaps filled from: {ometa.get('source', '')}",
            "attribution": "; ".join(x for x in (tmeta.get("attribution"), ometa.get("attribution")) if x),
            "license": "; ".join(x for x in (tmeta.get("license"), ometa.get("license")) if x),
            "cross_check": {"target": target_id, "other": other_id, "max_m": max_m, "owner_filled": n_filled_owner,
                            "voltage_filled": n_filled_kv, "voltage_disagrees": n_disagree,
                            "rule": "owner/voltage taken from the other layer only where the line has none and >= 50% of "
                                    "its length lies within max_m of the other layer's lines"}}
    return layers.save(lid, out, meta, source_tag="x")


# =============================================================================================== statistics

def theil_sen(years: np.ndarray, Y: np.ndarray, block=4000):
    """Per-column Theil-Sen slope (per year), Mann-Kendall S, n, two-sided p (normal approximation, no tie or
    autocorrelation correction). ``Y`` is (n_years, n_cols) with NaN for missing years."""
    years = np.asarray(years, dtype=float)
    ny, nc = Y.shape
    ii, jj = np.triu_indices(ny, k=1)
    dt = (years[jj] - years[ii])[:, None]
    slope = np.full(nc, np.nan)
    S = np.zeros(nc)
    n = np.isfinite(Y).sum(axis=0)
    for c0 in range(0, nc, block):
        y = Y[:, c0:c0 + block].astype(np.float64)
        d = y[jj] - y[ii]
        with np.errstate(invalid="ignore"), _quiet():
            slope[c0:c0 + block] = np.nanmedian(d / dt, axis=0) if np.isfinite(d).any() else np.nan
        S[c0:c0 + block] = np.nansum(np.sign(d), axis=0)
    var = n * (n - 1) * (2 * n + 5) / 18.0
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(S > 0, S - 1, np.where(S < 0, S + 1, 0)) / np.sqrt(var)
    p = np.array([math.erfc(abs(x) / math.sqrt(2)) if np.isfinite(x) else np.nan for x in z])
    bad = n < MIN_TREND_YEARS
    slope[bad], p[bad] = np.nan, np.nan
    return slope, S, n, p


def fdr_mask(p: np.ndarray, q=FDR_Q) -> np.ndarray:
    """Benjamini-Hochberg: True where the trend is significant with the false discovery rate controlled at q."""
    ok = np.isfinite(p)
    out = np.zeros(p.shape, dtype=bool)
    if not ok.any():
        return out
    ps = p[ok]
    order = np.argsort(ps)
    m = len(ps)
    crit = q * (np.arange(1, m + 1) / m)
    passed = ps[order] <= crit
    if passed.any():
        kmax = np.max(np.where(passed)[0])
        sig = np.zeros(m, dtype=bool)
        sig[order[:kmax + 1]] = True
        out[np.where(ok)[0]] = sig
    return out


def _window(year, a, b):
    d0 = pd.Timestamp(year, 1, 1)
    return (pd.Timestamp(year, *a) - d0).days, (pd.Timestamp(year, *b) - d0).days + 1


def _smooth(X, w=SMOOTH_DAYS, min_frac=0.5):
    """Centered running mean along axis 0, ignoring NaN (needs ``min_frac`` of the window present)."""
    fin = np.isfinite(X)
    c = np.cumsum(np.vstack([np.zeros((1, X.shape[1])), np.where(fin, X, 0.0)]), axis=0)
    k = np.cumsum(np.vstack([np.zeros((1, X.shape[1])), fin.astype(np.float64)]), axis=0)
    h = w // 2
    n = X.shape[0]
    lo = np.clip(np.arange(n) - h, 0, n)
    hi = np.clip(np.arange(n) + h + 1, 0, n)
    s, cnt = c[hi] - c[lo], k[hi] - k[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt >= max(1, min_frac * w), s / cnt, np.nan)


def _arg_ext(Z, lo, hi, fn):
    """(value, day index) of the max/min of Z[lo:hi] per column; NaN where the window is empty."""
    sub = Z[lo:hi]
    fin = np.isfinite(sub)
    anyf = fin.any(axis=0)
    fill = -np.inf if fn == "max" else np.inf
    filled = np.where(fin, sub, fill)
    idx = filled.argmax(axis=0) if fn == "max" else filled.argmin(axis=0)
    val = filled[idx, np.arange(sub.shape[1])]
    return np.where(anyf, val, np.nan), np.where(anyf, idx + lo, np.nan)


def year_metrics(X, H, year, thr: dict, Xmin=None, thr_min=None) -> tuple[dict, np.ndarray]:
    """Yearly metrics for every column of X (daily maxima, (n_days, n_cells), NaN = not saved). H = local hour of the
    maximum (int8, -1 = n/a) or None. Returns (metrics, valid) where valid marks cells with >= 90% of Mar-Nov saved."""
    s0, s1 = _window(year, *SEASON)
    Xs = X[s0:s1]
    valid = np.isfinite(Xs).mean(axis=0) >= MIN_SEASON_FRACTION
    m = {}
    m["annual_max"], d = _arg_ext(X, s0, s1, "max")
    m["annual_max_doy"] = d
    with np.errstate(invalid="ignore"), _quiet():
        m["season_mean"] = np.nanmean(Xs, axis=0) if Xs.size else np.nan
    for p in PCTS:
        with np.errstate(invalid="ignore"):
            m[f"days_p{p}"] = (Xs >= thr[p]).sum(axis=0).astype(float)
    with np.errstate(invalid="ignore"):
        high = Xs >= thr[90]
    anyh = high.any(axis=0)
    first = high.argmax(axis=0)
    last = Xs.shape[0] - 1 - high[::-1].argmax(axis=0)
    m["first_p90_doy"] = np.where(anyh, first + s0, np.nan)
    m["last_p90_doy"] = np.where(anyh, last + s0, np.nan)
    m["span_p90"] = np.where(anyh, last - first + 1, 0.0)
    Z = _smooth(X)
    m["spring_peak"], m["spring_peak_doy"] = _arg_ext(Z, *_window(year, *SPRING), "max")
    m["summer_trough"], m["summer_trough_doy"] = _arg_ext(Z, *_window(year, *SUMMER), "min")
    m["fall_peak"], m["fall_peak_doy"] = _arg_ext(Z, *_window(year, *FALL), "max")
    m["season_min"], _ = _arg_ext(Z, s0, s1, "min")
    if H is not None:
        Hs = H[s0:s1].astype(float)
        Hs[(Hs < 0) | ~high] = np.nan
        with np.errstate(invalid="ignore"), _quiet():
            m["peak_hour_lst"] = np.nanmedian(Hs, axis=0)
    else:
        m["peak_hour_lst"] = np.full(X.shape[1], np.nan)
    if Xmin is not None and thr_min is not None:
        Ms = Xmin[s0:s1]
        with np.errstate(invalid="ignore"), _quiet():
            m["min_season_mean"] = np.nanmean(Ms, axis=0)
            m["days_min_p90"] = (Ms >= thr_min).sum(axis=0).astype(float)
    else:
        m["min_season_mean"] = np.full(X.shape[1], np.nan)
        m["days_min_p90"] = np.full(X.shape[1], np.nan)
    for k in m:
        m[k] = np.where(valid, m[k], np.nan).astype(np.float32)
    return m, valid


class _quiet:
    def __enter__(self):
        import warnings
        self._w = warnings.catch_warnings()
        self._w.__enter__()
        warnings.simplefilter("ignore", RuntimeWarning)

    def __exit__(self, *a):
        self._w.__exit__(*a)


def _hist_percentiles(counts, scale, pcts):
    n = int(counts.sum())
    if not n:
        return {p: None for p in pcts}
    cum = np.cumsum(counts)
    values = (np.arange(len(counts)) - 32768) * scale
    out = {}
    for p in pcts:                                                   # same convention as np.percentile (linear)
        pos = p / 100.0 * (n - 1)
        lo_i, hi_i = int(np.floor(pos)), int(np.ceil(pos))
        vlo = values[np.searchsorted(cum, lo_i + 1)]
        vhi = values[np.searchsorted(cum, hi_i + 1)]
        out[p] = float(vlo + (vhi - vlo) * (pos - lo_i))
    return out


def _doy_365(year, n_days):
    """Index of each day of the year on a 365-day calendar (Feb 29 -> -1)."""
    idx = np.arange(n_days)
    if n_days == 366:
        idx = np.where(idx < 59, idx, idx - 1)
        idx[59] = -1
    return idx


def _fmt_doy(d, year=2001):
    if d is None or not np.isfinite(d):
        return None
    return (pd.Timestamp(year, 1, 1) + pd.Timedelta(days=int(round(float(d))))).strftime("%b %d")


# =============================================================================================== analysis

class LineAnalysis:
    """Runs over the saved record: daily files per year (built from the chunk files when missing or outdated), regional
    thresholds, yearly metrics per cell, trends, then corridor / per-line / region summaries written to
    <workspace>/lines/analyses/<id>/."""

    def __init__(self, ws, grid, store, layers: LineLayers):
        self.ws, self.grid, self.store, self.layers = ws, grid, store, layers
        self.root = ws.root / "lines" / "analyses"

    def analysis_id(self, layer_id, source, cfg_id):
        return f"{layer_id}__{source}__{cfg_id}"

    def years_saved(self, source, cfg_id):
        with self.ws.conn() as c:
            return [int(r[0]) for r in c.execute("SELECT DISTINCT year FROM chunks WHERE source=? AND config_id=? ORDER BY year",
                                                 (source, cfg_id)).fetchall()]

    def _read_daily(self, path, v):
        import netCDF4
        from aorc_tools.explorer.store import PACK
        name = "FWI_daily_noon" if v == "FWI_daily" else f"{v}_daily_max"
        with HDF_LOCK, netCDF4.Dataset(path) as ds:
            if name not in ds.variables:
                return None, None
            var = ds[name]
            var.set_auto_maskandscale(False)
            raw = np.asarray(var[:])[:, self.store.y, self.store.x]
            hname = f"{v}_hour_of_max"
            H = mn = None
            if hname in ds.variables:
                hv = ds[hname]
                hv.set_auto_maskandscale(False)
                H = np.asarray(hv[:])[:, self.store.y, self.store.x]
            if f"{v}_daily_min" in ds.variables:
                mv = ds[f"{v}_daily_min"]
                mv.set_auto_maskandscale(False)
                mn = np.asarray(mv[:])[:, self.store.y, self.store.x]
        return raw, H, mn

    def run(self, layer_id, source, cfg_id, indices=None, years=None, ref_years=None, progress=None) -> dict:
        from aorc_tools.explorer.store import FILL_I2, PACK
        progress = progress or (lambda done, total, note="": None)
        indices = [v for v in (indices or INDICES) if v in INDICES]
        fc = self.layers.get(layer_id)
        lmeta = self.layers.meta(layer_id)
        saved = self.years_saved(source, cfg_id)
        years = [y for y in (years or saved) if y in saved]
        if not years:
            raise ValueError("nothing saved for this source and configuration yet: compute the record first")
        ref_years = [y for y in (ref_years or years) if y in years]
        steps = len(years) + len(indices) * (len(ref_years) + len(years)) + 2
        done = 0
        # 1. daily files (incremental)
        paths = {}
        for y in years:
            progress(done, steps, f"daily maxima {y}")
            paths[y] = self.store.daily_year(source, cfg_id, y)
            done += 1
        # 2. lines -> cells
        cells = features_to_cells(fc, self.grid)
        n_cells = self.grid.n_cells
        w_corr = np.zeros(n_cells)
        for fid, cinfo in cells.items():
            np.add.at(w_corr, cinfo["rows"], cinfo["km"])
        corr = np.where(w_corr > 0)[0]
        if not len(corr):
            raise ValueError("none of the lines crosses a study cell")
        aid = self.analysis_id(layer_id, source, cfg_id)
        out_dir = self.root / aid
        tmp_dir = self.root / (aid + ".tmp")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True)
        fids = list(cells)
        summary = {
            "id": aid, "layer": lmeta, "source": source, "config_id": cfg_id, "created": now_iso(),
            "years": years, "reference_years": [min(ref_years), max(ref_years)], "indices": {},
            "definitions": {"season": "March 1 - November 30 (local standard days)", "metrics": METRICS,
                            "thresholds": "percentiles of all study cells' daily maxima, Mar-Nov, pooled over the reference years",
                            "smoothing_days": SMOOTH_DAYS, "valid_year": f">= {int(MIN_SEASON_FRACTION * 100)}% of Mar-Nov days saved",
                            "trend": f"Theil-Sen slope per decade; Mann-Kendall p (two-sided); >= {MIN_TREND_YEARS} valid years; "
                                     f"cell maps: Benjamini-Hochberg FDR q = {FDR_Q}"},
            "corridor": {"cells": int(len(corr)), "km_in_cells": round(float(w_corr.sum()), 1),
                         "km_outside_cells": round(sum(c["km_outside"] for c in cells.values()), 1),
                         "share_of_region_cells": round(len(corr) / n_cells, 4)},
            "caveats": [
                "Fire-weather danger along the lines, not ignition probability or consequence.",
                "Thresholds are regional (one scale for every place); a line in a drier part of the WUP crosses them more often.",
                "AORC is not temporally homogeneous (source streams change by era; non-precipitation fields change ~2015): "
                "check trends for steps at those dates before reading them as climate change.",
                "Per-cell p-values ignore autocorrelation between years; the corridor and per-line series are the primary result.",
                "FPI exists only from 2000 (MODIS NDVI), so its trends cover fewer years.",
            ],
        }
        props = {f["properties"]["id"]: f["properties"] for f in fc["features"]}
        feat_rows = {fid: {"id": fid, **{k: v for k, v in props[fid].items() if k != "attrs"},
                           "cells": int(len(cells[fid]["rows"])), "km_in_cells": round(float(cells[fid]["km"].sum()), 3)} for fid in fids}
        yearly_rows, cycle_rows = [], []
        cell_out = {}
        for v in indices:
            scale = PACK[v][0]
            # 3. regional thresholds (exact, from a histogram of the packed values)
            counts = np.zeros(65536, dtype=np.int64)
            counts_min = np.zeros(65536, dtype=np.int64)
            for y in ref_years:
                progress(done, steps, f"{v}: thresholds {y}")
                done += 1
                if paths.get(y) is None:
                    continue
                raw, _, mn = self._read_daily(paths[y], v)
                if raw is None:
                    continue
                s0, s1 = _window(y, *SEASON)
                for arr, cnt in ((raw, counts), (mn, counts_min)):
                    if arr is None:
                        continue
                    a = arr[s0:s1].ravel()
                    a = a[a != FILL_I2].astype(np.int64) + 32768
                    if a.size:
                        cnt += np.bincount(a, minlength=65536)
            thr = _hist_percentiles(counts, scale, PCTS)
            thr_min = _hist_percentiles(counts_min, scale, (90,))[90]
            if thr[90] is None:
                summary["indices"][v] = {"skipped": "no saved values"}
                done += len(years)
                continue
            # 4. yearly metrics per cell
            M = {k: np.full((len(years), n_cells), np.nan, dtype=np.float32) for k in METRICS}
            cyc_c = np.zeros(365); cyc_r = np.zeros(365); cyc_n = np.zeros(365)
            n_valid = np.zeros(len(years), dtype=int)
            for yi, y in enumerate(years):
                progress(done, steps, f"{v}: metrics {y}")
                done += 1
                if paths.get(y) is None:
                    continue
                raw, H, mn = self._read_daily(paths[y], v)
                if raw is None:
                    continue
                X = np.where(raw == FILL_I2, np.nan, raw.astype(np.float32) * np.float32(scale))
                Xmin = None if mn is None else np.where(mn == FILL_I2, np.nan, mn.astype(np.float32) * np.float32(scale))
                del raw, mn
                m, valid = year_metrics(X, H, y, thr, Xmin, thr_min)
                del Xmin
                n_valid[yi] = int(valid.sum())
                for k in METRICS:
                    M[k][yi] = m[k]
                if valid[corr].any() and valid.any():
                    d365 = _doy_365(y, X.shape[0])
                    keep = d365 >= 0
                    wc = w_corr[corr] * valid[corr]
                    with np.errstate(invalid="ignore"), _quiet():
                        xc = np.nansum(X[:, corr] * wc, axis=1) / np.maximum(1e-9, np.sum(np.isfinite(X[:, corr]) * wc, axis=1))
                        xr = np.nanmean(np.where(valid, X, np.nan), axis=1)
                    ok = keep & np.isfinite(xc) & np.isfinite(xr)
                    cyc_c[d365[ok]] += xc[ok]; cyc_r[d365[ok]] += xr[ok]; cyc_n[d365[ok]] += 1
                del X, H
            vy = [y for y, n in zip(years, n_valid) if n > 0]
            if not vy:
                summary["indices"][v] = {"skipped": "no year has 90% of March-November saved", "thresholds": thr}
                continue
            yarr = np.array(years, dtype=float)
            # corridor / region / per-line yearly series
            agg = {}
            for k in METRICS:
                A = M[k]
                fin = np.isfinite(A)
                with np.errstate(invalid="ignore"), _quiet():
                    region = np.nanmean(A, axis=1)
                    wc = np.where(fin[:, corr], w_corr[corr], 0.0)
                    corridor = np.where(wc.sum(axis=1) > 0, np.nansum(np.where(fin[:, corr], A[:, corr], 0) * wc, axis=1)
                                        / np.maximum(1e-9, wc.sum(axis=1)), np.nan)
                agg[k] = (corridor, region)
                for yi, y in enumerate(years):
                    if np.isfinite(corridor[yi]) or np.isfinite(region[yi]):
                        yearly_rows.append({"index": v, "year": y, "metric": k,
                                            "corridor": None if not np.isfinite(corridor[yi]) else round(float(corridor[yi]), 3),
                                            "region": None if not np.isfinite(region[yi]) else round(float(region[yi]), 3)})
            trends = {}
            for k in TREND_METRICS:
                c_, r_ = agg[k]
                Y = np.vstack([c_, r_, c_ - r_]).T
                sl, S, n, p = theil_sen(yarr, Y)
                trends[k] = {name: {"per_decade": None if not np.isfinite(sl[j]) else round(float(sl[j] * 10), 4),
                                    "p": None if not np.isfinite(p[j]) else round(float(p[j]), 4), "n_years": int(n[j])}
                             for j, name in enumerate(("corridor", "region", "corridor_minus_region"))}
            # mean seasonal cycle
            with np.errstate(invalid="ignore", divide="ignore"):
                cc = _smooth((cyc_c / cyc_n)[:, None])[:, 0]
                cr = _smooth((cyc_r / cyc_n)[:, None])[:, 0]
            for d in range(365):
                if cyc_n[d]:
                    cycle_rows.append({"index": v, "doy": d, "date": _fmt_doy(d), "corridor": round(float(cc[d]), 3),
                                       "region": round(float(cr[d]), 3), "years": int(cyc_n[d])})

            def cyc_points(z):
                pts = {}
                for name, (a, b), fn in (("spring_peak", SPRING, "max"), ("summer_trough", SUMMER, "min"), ("fall_peak", FALL, "max")):
                    lo, hi = _window(2001, a, b)
                    val, idx = _arg_ext(z[:, None], lo, hi, fn)
                    pts[name] = {"value": None if not np.isfinite(val[0]) else round(float(val[0]), 2), "date": _fmt_doy(idx[0])}
                lo, hi = _window(2001, *SEASON)
                val, idx = _arg_ext(z[:, None], lo, hi, "max")
                pts["global_max"] = {"value": None if not np.isfinite(val[0]) else round(float(val[0]), 2), "date": _fmt_doy(idx[0])}
                val, idx = _arg_ext(z[:, None], lo, hi, "min")
                pts["season_min"] = {"value": None if not np.isfinite(val[0]) else round(float(val[0]), 2), "date": _fmt_doy(idx[0])}
                return pts

            def mean_of(arr):
                with np.errstate(invalid="ignore"), _quiet():
                    x = np.nanmean(arr)
                return None if not np.isfinite(x) else round(float(x), 3)

            recent = np.array(years) >= max(vy) - 9
            early = np.array(years) <= min(vy) + 9
            summary["indices"][v] = {
                "label": LABEL[v], "thresholds": {f"p{p}": round(thr[p], 3) for p in PCTS},
                "threshold_min_p90": None if thr_min is None else round(thr_min, 3),
                "valid_years": vy, "cells_valid_by_year": dict(zip(map(str, years), map(int, n_valid))),
                "corridor": {k: mean_of(agg[k][0]) for k in METRICS},
                "region": {k: mean_of(agg[k][1]) for k in METRICS},
                "corridor_first_10y": {k: mean_of(agg[k][0][early]) for k in TREND_METRICS},
                "corridor_last_10y": {k: mean_of(agg[k][0][recent]) for k in TREND_METRICS},
                "region_first_10y": {k: mean_of(agg[k][1][early]) for k in TREND_METRICS},
                "region_last_10y": {k: mean_of(agg[k][1][recent]) for k in TREND_METRICS},
                "years_corridor_above_region": {k: int(np.nansum(agg[k][0] > agg[k][1])) for k in ("days_p90", "days_p95", "annual_max")},
                "cycle": {"corridor": cyc_points(cc), "region": cyc_points(cr)},
                "trends": trends,
            }
            # per-cell maps (all study cells): climatology + trends, with FDR significance
            cm = {}
            with np.errstate(invalid="ignore"), _quiet():
                for k in ("days_p90", "days_p95", "days_p99", "annual_max", "season_mean", "spring_peak", "fall_peak", "span_p90",
                          "min_season_mean", "days_min_p90"):
                    cm[f"{k}_mean"] = np.nanmean(M[k], axis=0)
                for k in ("spring_peak_doy", "fall_peak_doy", "summer_trough_doy", "first_p90_doy", "last_p90_doy", "peak_hour_lst"):
                    cm[f"{k}_median"] = np.nanmedian(M[k], axis=0)
            for k in ("days_p90", "days_p95", "annual_max", "season_mean", "first_p90_doy", "span_p90", "days_min_p90"):
                sl, S, n, p = theil_sen(yarr, M[k])
                cm[f"{k}_trend"] = sl * 10
                cm[f"{k}_p"] = p
                cm[f"{k}_sig"] = fdr_mask(p).astype(np.float32)
                summary["indices"][v].setdefault("cell_trends", {})[k] = {
                    "cells_tested": int(np.isfinite(p).sum()),
                    "cells_increasing_sig": int(((sl > 0) & (cm[f"{k}_sig"] > 0)).sum()),
                    "cells_decreasing_sig": int(((sl < 0) & (cm[f"{k}_sig"] > 0)).sum()),
                    "corridor_cells_increasing_sig": int(((sl[corr] > 0) & (cm[f"{k}_sig"][corr] > 0)).sum()),
                    "corridor_cells_decreasing_sig": int(((sl[corr] < 0) & (cm[f"{k}_sig"][corr] > 0)).sum())}
            cell_out[v] = {k: a.astype(np.float32) for k, a in cm.items()}
            # per line
            for fid in fids:
                rows, km = cells[fid]["rows"], cells[fid]["km"]
                r = feat_rows[fid]
                if not len(rows):
                    continue
                for k in ("days_p90", "days_p95", "days_p99", "annual_max", "season_mean", "spring_peak", "fall_peak", "first_p90_doy",
                          "last_p90_doy", "span_p90", "peak_hour_lst", "min_season_mean", "days_min_p90"):
                    A = M[k][:, rows]
                    fin = np.isfinite(A)
                    wk = np.where(fin, km, 0.0)
                    with np.errstate(invalid="ignore", divide="ignore"):
                        series = np.where(wk.sum(axis=1) > 0, np.nansum(np.where(fin, A, 0) * wk, axis=1) / wk.sum(axis=1), np.nan)
                    r[f"{v}.{k}"] = mean_of(series)
                    if k in ("days_p90", "days_p95", "annual_max", "first_p90_doy", "span_p90", "days_min_p90"):
                        sl, S, n, p = theil_sen(yarr, series[:, None])
                        r[f"{v}.{k}.trend_per_decade"] = None if not np.isfinite(sl[0]) else round(float(sl[0] * 10), 4)
                        r[f"{v}.{k}.p"] = None if not np.isfinite(p[0]) else round(float(p[0]), 4)
                    if k in ("days_p90", "days_p95"):
                        r[f"{v}.{k}.last_10y"] = mean_of(series[recent])
                with np.errstate(invalid="ignore"), _quiet():
                    r[f"{v}.max_cell_days_p95"] = None if not np.isfinite(np.nanmax(cm["days_p95_mean"][rows])) else \
                        round(float(np.nanmax(cm["days_p95_mean"][rows])), 3)
            del M
        # 5. write products
        progress(done, steps, "writing results")
        frame = pd.DataFrame(list(feat_rows.values()))
        main = next((v for v in ("FWI", "FWI_daily", "ERC", "BI", "FPI") if f"{v}.days_p95" in frame), None)
        if main:
            frame = frame.sort_values(f"{main}.days_p95", ascending=False, na_position="last")
            frame.insert(1, "rank", np.arange(1, len(frame) + 1))
        summary["ranking_by"] = f"{main}.days_p95" if main else None
        summary["features"] = json.loads(frame.to_json(orient="records"))
        frame.to_csv(tmp_dir / "lines.csv", index=False)
        pd.DataFrame(yearly_rows).to_csv(tmp_dir / "yearly_corridor_vs_region.csv", index=False)
        pd.DataFrame(cycle_rows).to_csv(tmp_dir / "seasonal_cycle.csv", index=False)
        pts = self.grid.points
        cols = {"point_id": pts.point_id.values, "latitude": pts.latitude.values.round(5),
                "longitude": pts.longitude.values.round(5), "county": pts.county.values, "line_km": w_corr.round(4)}
        for v, cm in cell_out.items():
            for k, a in cm.items():
                cols[f"{v}.{k}"] = np.where(np.isfinite(a), np.round(a, 4), np.nan)
        pd.DataFrame(cols).to_csv(tmp_dir / "cells.csv", index=False)
        np.savez_compressed(tmp_dir / "cells.npz", point_id=pts.point_id.values, line_km=w_corr,
                            **{f"{v}.{k}": a for v, cm in cell_out.items() for k, a in cm.items()})
        lines_fc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": f["geometry"],
             "properties": {**{k: v for k, v in f["properties"].items() if k != "attrs"},
                            **{k: v for k, v in feat_rows[f["properties"]["id"]].items() if "." in k or k == "rank"}}}
            for f in fc["features"]]}
        ranks = {x["id"]: x.get("rank") for x in summary["features"]}
        for f in lines_fc["features"]:
            f["properties"]["rank"] = ranks.get(f["properties"]["id"])
        (tmp_dir / "lines_with_metrics.geojson").write_text(json.dumps(lines_fc, separators=(",", ":")), encoding="utf-8")
        (tmp_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=_json_default), encoding="utf-8")
        (tmp_dir / "README.txt").write_text(_readme(summary), encoding="utf-8")
        _swap_dir(tmp_dir, out_dir)
        progress(steps, steps, "done")
        return {"id": aid, "dir": str(out_dir), "features": len(fids), "corridor_cells": int(len(corr)), "years": len(years)}

    # ---------------------------------------------------------------- reading results
    def list(self):
        out = []
        if not self.root.exists():
            return out
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and (d / "summary.json").exists() and not d.name.endswith((".tmp", ".old")):
                s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
                out.append({"id": d.name, "layer": s["layer"].get("id"), "layer_name": s["layer"].get("name"), "source": s["source"],
                            "config_id": s["config_id"], "created": s["created"], "years": [min(s["years"]), max(s["years"])]})
        return out

    def _dir(self, aid):
        if not isinstance(aid, str) or not re.match(r"^[a-z0-9_#-]+$", aid, re.I) or ".." in aid:
            raise ValueError("invalid analysis id")
        d = self.root / aid
        if not (d / "summary.json").exists():
            raise KeyError(f"no analysis {aid}")
        return d

    def summary(self, aid):
        return json.loads((self._dir(aid) / "summary.json").read_text(encoding="utf-8"))

    def cell_field(self, aid, key):
        with np.load(self._dir(aid) / "cells.npz") as z:              # closed at once (Windows renames)
            if key not in z.files:
                raise KeyError(f"no cell field {key}")
            return z["point_id"].copy(), z[key].copy(), z["line_km"].copy()

    def file(self, aid, name):
        if name not in ("summary.json", "lines.csv", "cells.csv", "yearly_corridor_vs_region.csv", "seasonal_cycle.csv",
                        "lines_with_metrics.geojson", "README.txt"):
            raise ValueError("unknown file")
        return self._dir(aid) / name

    def yearly(self, aid):
        return pd.read_csv(self._dir(aid) / "yearly_corridor_vs_region.csv")

    def cycle(self, aid):
        return pd.read_csv(self._dir(aid) / "seasonal_cycle.csv")


def _swap_dir(tmp_dir: Path, out_dir: Path, tries=40, wait=0.25):
    """Put a finished results folder in place of the old one. On Windows a rename fails while another handle (a
    download in progress, a virus scanner) has a file open, so retry for ~10 s."""
    import time as _t
    old = out_dir.with_name(out_dir.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    for i in range(tries):
        try:
            if out_dir.exists():
                out_dir.rename(old)
            tmp_dir.rename(out_dir)
            break
        except PermissionError:
            if i == tries - 1:
                raise
            _t.sleep(wait)
    shutil.rmtree(old, ignore_errors=True)


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _readme(s) -> str:
    lines = [f"Line exposure to fire danger: {s['layer'].get('name')} ({s['layer'].get('id')})",
             f"Record: {s['source']} / {s['config_id']}, years {min(s['years'])}-{max(s['years'])}, created {s['created']}",
             f"Corridor: {s['corridor']['cells']} cells, {s['corridor']['km_in_cells']} km of line inside study cells", "",
             "Files:",
             "  lines.csv                       one row per line feature (ranked), metrics per index: <index>.<metric>",
             "  yearly_corridor_vs_region.csv   each year's metric for the corridor (km-weighted) and the region (all cells)",
             "  seasonal_cycle.csv              mean seasonal cycle of the daily maxima, corridor vs region",
             "  cells.csv                       every study cell: line km, climatology and trend of each metric",
             "  lines_with_metrics.geojson      the lines with their metrics (QGIS/ArcGIS)",
             "  summary.json                    everything above in one file, with definitions and thresholds", "",
             "Definitions:"]
    for k, v in s["definitions"].items():
        if k != "metrics":
            lines.append(f"  {k}: {v}")
    lines.append("  metrics:")
    lines += [f"    {k}: {v}" for k, v in s["definitions"]["metrics"].items()]
    lines += ["", "Caveats:"] + [f"  - {c}" for c in s["caveats"]]
    return "\n".join(lines) + "\n"
