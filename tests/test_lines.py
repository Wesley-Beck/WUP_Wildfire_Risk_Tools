"""
Line exposure to fire danger: line layers, lines -> cells, the seasonality / intensity / trend metrics, and the whole
analysis on a small synthetic record.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aorc_tools.explorer import lines as L
from aorc_tools.explorer.chain import ChainConfig
from aorc_tools.explorer.workspace import Workspace


@pytest.fixture(scope="module")
def grid():
    from aorc_tools.explorer.grid import load_grid
    from aorc_tools.explorer.server import default_points_csv
    return load_grid(default_points_csv())


# ----------------------------------------------------------------------------- statistics

def test_theil_sen_and_mann_kendall():
    yrs = np.arange(1990, 2002, dtype=float)
    Y = np.vstack([2.0 * (yrs - 1990) + 1, np.full(12, 3.0), -(yrs - 1990)]).T
    Y[3, 0] = np.nan                                                    # a missing year
    sl, S, n, p = L.theil_sen(yrs, Y)
    assert sl[0] == pytest.approx(2.0) and sl[1] == 0 and sl[2] == pytest.approx(-1.0)
    assert n.tolist() == [11, 12, 12] and S[2] == -66
    z = 65 / math.sqrt(12 * 11 * 29 / 18)                              # S=-66, continuity correction
    assert p[2] == pytest.approx(math.erfc(z / math.sqrt(2)), rel=1e-9) and p[1] == pytest.approx(1.0)
    rng = np.random.default_rng(1)
    y = rng.normal(size=(30, 1)) + 0.05 * np.arange(30)[:, None]
    ref = np.median([(y[j, 0] - y[i, 0]) / (j - i) for i in range(30) for j in range(i + 1, 30)])
    assert L.theil_sen(np.arange(30), y)[0][0] == pytest.approx(ref)
    sl, *_ = L.theil_sen(np.arange(5.0), np.arange(5.0)[:, None])       # too few years for a trend
    assert np.isnan(sl[0])


def test_fdr_controls_false_discoveries():
    p = np.array([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, np.nan])
    m = L.fdr_mask(p, q=0.05)
    assert m.tolist() == [True, True, False, False, False, False, False, False, False]
    assert not L.fdr_mask(np.full(5, np.nan)).any()


def test_year_metrics_maxima_and_minima_at_every_timescale():
    year = 2001
    d = np.arange(365)
    base = 2 + 18 * np.exp(-0.5 * ((d - 125) / 12) ** 2) + 10 * np.exp(-0.5 * ((d - 280) / 15) ** 2)   # May 5 / Oct 7
    X = np.vstack([base, base * 0.5, base]).T.astype(np.float32)
    X[150:330, 2] = np.nan                                              # third cell: most of the season missing
    Xmin = (X * 0.3).astype(np.float32)
    Xmin[200, 0] = -1.0                                                 # the year's lowest minimum: Jul 20
    H = np.full(X.shape, 15, dtype=np.int8)
    Hmin = np.full(X.shape, 5, dtype=np.int8)
    thr = {90: 12.0, 95: 16.0, 99: 19.0}
    m, valid = L.year_metrics(X, H, year, thr, Xmin=Xmin, thr_min=5.0, Hmin=Hmin)
    assert valid.tolist() == [True, True, False]
    # seasonal local extremes
    assert m["spring_peak_doy"][0] == pytest.approx(125, abs=1) and m["fall_peak_doy"][0] == pytest.approx(280, abs=1)
    assert 140 <= m["summer_trough_doy"][0] <= 243 and m["summer_trough"][0] < m["fall_peak"][0] < m["spring_peak"][0]
    assert m["spring_peak_of_min"][0] > m["summer_trough_of_min"][0]
    # season and year (global) extremes
    assert m["season_max"][0] == pytest.approx(base[59:334].max(), rel=1e-5) and m["season_max_doy"][0] == 125
    assert m["year_max"][0] == pytest.approx(base.max(), rel=1e-5)
    assert m["year_min"][0] == -1.0 and m["year_min_doy"][0] == 200 and m["season_min"][0] == -1.0
    assert np.isnan(m["year_max"][2]) and np.isnan(m["season_max"][2])  # too little saved: no metrics
    # months
    assert m["m05_max"][0] == pytest.approx(base[120:151].max(), rel=1e-5)
    assert m["m07_min"][0] == -1.0 and m["m01_min"][0] == pytest.approx(Xmin[0:31, 0].min(), rel=1e-5)
    assert np.isfinite(m["m01_max"][2]) and np.isnan(m["m07_max"][2])   # cell 3: January saved, July not
    # thresholds, timing, hour of day, recovery
    above = base[59:334] >= 12
    assert m["days_p90"][0] == above.sum() and m["days_p90"][1] == 0
    assert m["first_p90_doy"][0] == 59 + np.argmax(above) and m["span_p90"][0] > 150           # spring through fall
    assert m["peak_hour_lst"][0] == 15 and np.isnan(m["peak_hour_lst"][1]) and m["trough_hour_lst"][1] == 5
    assert m["days_min_p90"][0] == (Xmin[59:334, 0] >= 5).sum()
    assert m["min_season_mean"][0] == pytest.approx(Xmin[59:334, 0].mean(), rel=1e-5)
    assert m["diurnal_range_mean"][0] == pytest.approx((X[59:334, 0] - Xmin[59:334, 0]).mean(), rel=1e-5)
    # a once-a-day index (classic daily FWI): its one value is both max and min; no hours, no range
    m1, _ = L.year_metrics(X, None, year, thr)
    assert m1["year_min"][0] == pytest.approx(X[:, 0].min()) and np.isnan(m1["diurnal_range_mean"][0])
    assert np.isnan(m1["peak_hour_lst"][0]) and np.isnan(m1["days_min_p90"][0])


def test_hist_percentiles_match_numpy():
    rng = np.random.default_rng(3)
    vals = np.round(rng.gamma(2, 5, 20000) / 0.1).astype(np.int64)      # packed with scale 0.1
    counts = np.bincount(vals + 32768, minlength=65536)
    got = L._hist_percentiles(counts, 0.1, (90, 95, 99))
    for p in (90, 95, 99):
        assert got[p] == pytest.approx(np.percentile(vals * 0.1, p), abs=1e-9)


# ----------------------------------------------------------------------------- layers and cells

def test_bundled_layer_maps_to_cells(grid, tmp_path):
    layers = L.LineLayers(Workspace(tmp_path / "ws"), grid)
    meta = {x["id"]: x for x in layers.list()}
    assert "wup_transmission_hifld_2024" in meta and meta["wup_transmission_hifld_2024"]["origin"] == "bundled"
    fc = layers.get("wup_transmission_hifld_2024")
    cells = L.features_to_cells(fc, grid)
    km_in = sum(c["km"].sum() for c in cells.values())
    assert len(fc["features"]) == 75 and 790 < km_in < 805
    rows = np.unique(np.concatenate([c["rows"] for c in cells.values()]))
    assert 900 < len(rows) < 1400
    with pytest.raises(ValueError):
        layers.delete("wup_transmission_hifld_2024")
    with pytest.raises(ValueError):
        layers.get("../settings")


def test_cell_index_is_exact_on_cell_centres(grid):
    idx = L.CellIndex(grid)
    p = grid.points.sample(500, random_state=0)
    rows = idx.rows(p.longitude.values + 0.003, p.latitude.values - 0.003)   # inside the same 1/120 degree cell
    assert (grid.points.point_id.values[rows] == p.point_id.values).all()
    assert idx.rows(np.array([-88.0]), np.array([47.9]))[0] == -1          # Lake Superior


def _osm_like():
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-88.60, 46.95], [-88.40, 46.90], [-88.20, 46.85]]},
         "properties": {"@id": "way/1", "power": "line", "voltage": "138000;69000", "operator": "American Transmission Company", "name": "X"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-88.50, 46.70], [-88.45, 46.72]]},
         "properties": {"@id": "way/2", "power": "cable", "location": "underground", "voltage": "69000"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-84.0, 42.0], [-84.1, 42.1]]},
         "properties": {"@id": "way/3", "power": "line", "voltage": "345000"}},                        # outside: dropped
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-88.5, 46.8]}, "properties": {"power": "tower"}}]}


def test_import_geojson_normalises_osm_and_hifld_fields(grid, tmp_path):
    layers = L.LineLayers(Workspace(tmp_path / "ws"), grid)
    m = layers.import_file("osm lines.geojson", json.dumps(_osm_like()).encode())
    assert m["id"] == "import-osm-lines" and m["n_features"] == 2 and m["voltages_kv"] == [69.0, 138.0]
    fc = layers.get(m["id"])
    p = {f["properties"]["id"]: f["properties"] for f in fc["features"]}
    assert p["way/1"]["owner"] == "American Transmission Company" and p["way/1"]["voltage_kv"] == 138.0
    assert p["way/2"]["underground"] is True
    hifld = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": _osm_like()["features"][0]["geometry"],
             "properties": {"ID": "140123", "VOLTAGE": 138, "VOLT_CLASS": "100-161", "OWNER": "AMERICAN TRANSMISSION CO LLC",
                            "SUB_1": "A", "SUB_2": "B", "STATUS": "IN SERVICE"}},
             {"type": "Feature", "geometry": _osm_like()["features"][1]["geometry"],
              "properties": {"ID": "140124", "VOLTAGE": -999999, "OWNER": "NOT AVAILABLE"}}]}
    m = layers.import_file("hifld.geojson", json.dumps(hifld).encode())
    p = {f["properties"]["id"]: f["properties"] for f in layers.get(m["id"])["features"]}
    assert p["140123"]["voltage_kv"] == 138 and p["140123"]["sub_1"] == "A" and "voltage_kv" not in p["140124"]
    assert "owner" not in p["140124"]
    layers.delete(m["id"])
    assert m["id"] not in {x["id"] for x in layers.list()}


def test_import_zipped_shapefile_reprojects(grid, tmp_path):
    shapefile = pytest.importorskip("shapefile")
    from rasterio.crs import CRS
    from rasterio.warp import transform
    xs, ys = transform("EPSG:4326", "EPSG:3078", [-88.6, -88.2], [46.95, 46.85])
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYLINE)
    w.field("VOLTAGE", "N", decimal=0)
    w.field("OWNER", "C")
    w.line([list(zip(xs, ys))])
    w.record(138, "UPPCO")
    w.close()
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        for ext, b in ((".shp", shp), (".shx", shx), (".dbf", dbf)):
            zf.writestr("lines" + ext, b.getvalue())
        zf.writestr("lines.prj", CRS.from_epsg(3078).to_wkt())
    layers = L.LineLayers(Workspace(tmp_path / "ws"), grid)
    m = layers.import_file("my_lines.zip", z.getvalue())
    f = layers.get(m["id"])["features"][0]
    c = np.asarray(f["geometry"]["coordinates"])
    assert c[0] == pytest.approx([-88.6, 46.95], abs=1e-5) and f["properties"]["voltage_kv"] == 138
    assert f["properties"]["length_km"] == pytest.approx(32.3, abs=0.6)


# ----------------------------------------------------------------------------- the whole analysis

def test_line_analysis_on_a_synthetic_record(grid, tmp_path, monkeypatch):
    """Three short 'years' of synthetic weather for two bricks; the analysis runs end to end, writes its products and
    compares the corridor with the region. (Real seasons need the full record; here the season rule is relaxed.)"""
    from aorc_tools.explorer.server import build
    monkeypatch.setattr(L, "MIN_SEASON_FRACTION", 0.05)
    monkeypatch.setattr(L, "MIN_TREND_YEARS", 3)
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2007-04-01T00", domain_end="2009-06-30T23")
    app = build(ws, source="synthetic", start_jobs=False)
    E = app.extensions["wup"]
    cfg = ChainConfig()
    for y in (2007, 2008, 2009):
        jid = E["jobs"].create(f"y{y}", "synthetic", cfg, ["25_18", "25_19"], f"{y}-05-01T00", f"{y}-05-24T23")
        assert E["jobs"].run_now(jid)["status"] == "done"
    c = app.test_client()
    lay = c.get("/api/lines/layers").get_json()
    assert any(x["id"] == "wup_transmission_hifld_2024" for x in lay["layers"])
    r = c.post("/api/lines/analyse", json={"layer": "wup_transmission_hifld_2024", "wait": True, "indices": ["FWI", "ERC"]}).get_json()
    t = E["tasks"].get(r["id"])
    assert t["status"] == "done", t
    aid = t["result"]["id"]
    s = c.get(f"/api/lines/analyses/{aid}").get_json()
    assert s["years"] == [2007, 2008, 2009] and set(s["indices"]) == {"FWI", "ERC"}
    fwi = s["indices"]["FWI"]
    assert fwi["thresholds"]["p90"] <= fwi["thresholds"]["p95"] <= fwi["thresholds"]["p99"]
    assert fwi["trends"]["days_p90"]["corridor"]["n_years"] == 3
    assert fwi["threshold_min_p90"] is not None and fwi["corridor"]["min_season_mean"] is not None
    assert {"corridor_max", "corridor_min", "region_max", "region_min"} <= set(fwi["record"])
    assert fwi["corridor"]["m05_max"] is not None and fwi["corridor"]["m05_min"] is not None
    assert "corridor_min" in fwi["cycle"] and len(fwi["trends"]) == len(L.METRICS)
    cyc = c.get(f"/api/lines/analyses/{aid}/cycle?index=FWI").get_json()["rows"]
    assert cyc and all(r["corridor_min"] <= r["corridor"] for r in cyc if r["corridor"] is not None and r["corridor_min"] is not None)
    rec = c.get(f"/api/lines/analyses/{aid}/cells?key=FWI.record_max").get_json()
    assert sum(x is not None for x in rec["values"]) > 0
    assert "days_min_p90" in fwi["trends"] and "FWI_daily" not in s["indices"] or s["indices"].get("FWI_daily") is None
    assert s["corridor"]["cells"] > 0 and s["features"][0]["rank"] == 1
    ranked = [f for f in s["features"] if f.get("FWI.days_p95") is not None]
    assert ranked and all(a["FWI.days_p95"] >= b["FWI.days_p95"] for a, b in zip(ranked, ranked[1:]))
    f = c.get(f"/api/lines/analyses/{aid}/cells?key=FWI.days_p90_mean").get_json()
    assert len(f["point_ids"]) == grid.n_cells and f["lo"] <= f["hi"]
    y = c.get(f"/api/lines/analyses/{aid}/yearly?index=FWI&metric=days_p90").get_json()
    assert [r["year"] for r in y["rows"]] == [2007, 2008, 2009]
    for name in ("lines.csv", "cells_FWI.csv", "cells_ERC.csv", "yearly_corridor_vs_region.csv", "seasonal_cycle.csv", "lines_with_metrics.geojson", "README.txt"):
        rr = c.get(f"/api/lines/analyses/{aid}/file/{name}")
        assert rr.status_code == 200 and len(rr.data) > 50, name
    assert c.get(f"/api/lines/analyses/{aid}/file/..%2Fsettings.json").status_code in (400, 404)
    # daily files are reused, and rebuilt only when a year's chunks change
    p = E["store"].daily_path("synthetic", cfg.id, 2008)
    m0 = p.stat().st_mtime_ns
    assert E["store"].daily_year("synthetic", cfg.id, 2008) == p and p.stat().st_mtime_ns == m0
    jid = E["jobs"].create("more", "synthetic", cfg, ["25_18"], "2008-05-25T00", "2008-06-05T23")
    E["jobs"].run_now(jid)
    E["store"].daily_year("synthetic", cfg.id, 2008)
    assert p.stat().st_mtime_ns != m0


# ----------------------------------------------------------------------------- downloads (fake network)

class _Resp:
    def __init__(self, js=None, status=200):
        self._js, self.status_code = js, status

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise IOError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, posts=(), gets=()):
        self.posts, self.gets, self.calls = list(posts), list(gets), []

    def post(self, url, data=None, timeout=None):
        self.calls.append(("post", url, data))
        return self.posts.pop(0)

    def get(self, url, params=None, timeout=None):
        self.calls.append(("get", url, params))
        return self.gets.pop(0)


def test_osm_download_fails_over_and_saves_a_layer(grid, tmp_path):
    from aorc_tools.explorer import lines_ingest as I
    osm = {"osm3s": {"timestamp_osm_base": "2026-10-20T12:00:00Z"}, "elements": [
        {"type": "way", "id": 11, "tags": {"power": "line", "voltage": "138000", "operator": "ATC"},
         "geometry": [{"lat": 46.95, "lon": -88.6}, {"lat": 46.90, "lon": -88.4}]},
        {"type": "way", "id": 12, "tags": {"power": "line"}, "geometry": [{"lat": 46.95, "lon": -88.6}]}]}   # one point: dropped
    sess = _Session(posts=[_Resp(status=504), _Resp(osm)])
    layers = L.LineLayers(Workspace(tmp_path / "ws"), grid)
    m = I.download(layers, "osm", session=sess)
    assert m["n_features"] == 1 and m["with_voltage"] == 1 and m["fetch"]["osm_timestamp"] == "2026-10-20T12:00:00Z"
    assert "OpenStreetMap" in m["attribution"] and m["id"].startswith("osm-")
    q = sess.calls[1][2]["data"]
    assert '"power"~"^(line|cable)$"' in q and "out tags geom" in q
    assert layers.get(m["id"])["features"][0]["properties"]["id"] == "way/11"


def test_arcgis_download_pages_through_results():
    from aorc_tools.explorer import lines_ingest as I
    f = lambda i: {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-88.6, 46.9], [-88.5, 46.9 + i / 1000]]},  # noqa: E731
                   "properties": {"ID": str(i), "VOLTAGE": 69}}
    sess = _Session(gets=[_Resp({"name": "Transmission_Lines", "maxRecordCount": 2}),
                          _Resp({"type": "FeatureCollection", "features": [f(1), f(2)], "properties": {"exceededTransferLimit": True}}),
                          _Resp({"type": "FeatureCollection", "features": [f(3)]})])
    feats, meta = I.fetch_arcgis("https://example.test/arcgis/rest/services/X/FeatureServer/0", (-90.5, 45.9, -87.3, 47.6), session=sess)
    assert [x["properties"]["ID"] for x in feats] == ["1", "2", "3"] and meta["max_record_count"] == 2
    assert sess.calls[2][2]["resultOffset"] == 2 and sess.calls[1][2]["f"] == "geojson" and sess.calls[1][2]["outSR"] == 4326


def test_cross_check_fills_owner_and_voltage_from_a_parallel_layer(grid, tmp_path):
    layers = L.LineLayers(Workspace(tmp_path / "ws"), grid)
    a = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-88.60, 46.95], [-88.20, 46.85]]},
         "properties": {"ID": "h1", "VOLTAGE": 69, "OWNER": "NOT AVAILABLE"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-88.60, 46.60], [-88.30, 46.60]]},
         "properties": {"ID": "h2", "VOLTAGE": 138, "OWNER": "AMERICAN TRANSMISSION CO"}}]}
    b = {"type": "FeatureCollection", "features": [                       # ~50 m north of h1; h2 has no partner
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-88.60, 46.9505], [-88.20, 46.8505]]},
         "properties": {"@id": "way/9", "power": "line", "voltage": "69000", "operator": "Upper Peninsula Power Company"}}]}
    la = layers.import_file("hifld.geojson", json.dumps(a).encode())["id"]
    lb = layers.import_file("osm.geojson", json.dumps(b).encode())["id"]
    m = L.cross_attributes(layers, la, lb)
    p = {f["properties"]["id"]: f["properties"] for f in layers.get(m["id"])["features"]}
    assert p["h1"]["alt_match_share"] > 0.95 and p["h1"]["owner_best"] == "Upper Peninsula Power Company"
    assert p["h1"]["voltage_best_kv"] == 69 and "alt_voltage_disagrees" not in p["h1"]
    assert p["h2"]["alt_match_share"] == 0 and p["h2"]["owner_best"] == "AMERICAN TRANSMISSION CO"
    assert m["cross_check"]["owner_filled"] == 1


# ----------------------------------------------------------------------------- review fixes (2026-10-21)

def test_smoothing_needs_both_sides_and_local_extremes_stay_inside_their_window():
    year = 2001
    d = np.arange(365)
    x = (2 + 20 * np.exp(-0.5 * ((d - 62) / 4) ** 2)).astype(np.float32)        # a sharp peak on Mar 4
    full = np.vstack([x, x]).T
    part = full.copy()
    part[:59] = np.nan                                                          # Jan-Feb not saved
    thr = {90: 50.0, 95: 60.0, 99: 70.0}
    a, _ = L.year_metrics(full, None, year, thr)
    b, _ = L.year_metrics(part, None, year, thr)
    assert b["spring_peak_doy"][0] != 59                                       # not pinned to the window edge
    assert b["spring_peak"][0] <= a["spring_peak"][0] * 1.1                     # nor inflated by a one-sided mean
    mono = np.vstack([np.linspace(1, 30, 365)] * 2).T.astype(np.float32)       # rising all year: no summer trough
    m, _ = L.year_metrics(mono, None, year, thr)
    assert np.isnan(m["summer_trough"][0]) and np.isnan(m["spring_peak"][0])


def test_dates_use_a_365_day_calendar_in_leap_years():
    for year in (2003, 2004):
        nd = 366 if year == 2004 else 365
        d = np.arange(nd)
        jul15 = (pd.Timestamp(year, 7, 15) - pd.Timestamp(year, 1, 1)).days
        x = np.vstack([5 + 10 * np.exp(-0.5 * ((d - jul15) / 3) ** 2)] * 2).T.astype(np.float32)
        m, _ = L.year_metrics(x, None, year, {90: 50.0, 95: 60.0, 99: 70.0})
        assert m["season_max_doy"][0] == 195 and L._fmt_doy(m["season_max_doy"][0]) == "Jul 15"


def test_mann_kendall_ties_and_serial_correlation():
    from scipy import stats
    rng = np.random.default_rng(5)
    Y = rng.integers(0, 4, size=(30, 40)).astype(float)
    Y[rng.random(Y.shape) < 0.1] = np.nan
    tt = L._tie_term(Y)
    for c in range(Y.shape[1]):
        _, cnt = np.unique(Y[:, c][np.isfinite(Y[:, c])], return_counts=True)
        assert tt[c] == pytest.approx(sum(t * (t - 1) * (2 * t + 5) for t in cnt))
    y = np.round(rng.integers(0, 3, 40) + 0.03 * np.arange(40)).astype(float)
    p = L.theil_sen(np.arange(40.0), y[:, None])[3][0]
    assert p == pytest.approx(stats.kendalltau(np.arange(40), y).pvalue, rel=0.15)      # tie-corrected variance
    counts = rng.poisson(np.linspace(0.05, 1.2, 48))                              # sparse rising counts
    sl, S, n, p = L.theil_sen(np.arange(1979.0, 2027.0), counts[:, None].astype(float))
    assert S[0] > 0 and p[0] < 0.05                                               # direction from S, not the slope
    e = rng.normal(size=48)
    assert L.mk_hamed_rao(np.arange(48), e) == pytest.approx(L.theil_sen(np.arange(48.0), e[:, None])[3][0], rel=0.3)
    ar = np.zeros(48)
    for i in range(1, 48):
        ar[i] = 0.85 * ar[i - 1] + rng.normal()
    assert L.mk_hamed_rao(np.arange(48), ar) >= L.theil_sen(np.arange(48.0), ar[:, None])[3][0]


def test_cross_check_keeps_underground_and_raw_attributes(grid, tmp_path):
    layers = L.LineLayers(Workspace(tmp_path / "ws"), grid)
    a = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-88.60, 46.95], [-88.20, 46.85]]},
         "properties": {"ID": "u1", "VOLTAGE": 69, "TYPE": "UNDERGROUND", "OWNER": "NOT AVAILABLE"}}]}
    b = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-88.60, 46.9505], [-88.20, 46.8505]]},
         "properties": {"@id": "way/1", "power": "cable", "location": "underground", "voltage": "69000"}}]}
    la = layers.import_file("a.geojson", json.dumps(a).encode())["id"]
    lb = layers.import_file("b.geojson", json.dumps(b).encode())["id"]
    m = L.cross_attributes(layers, la, lb)
    p = layers.get(m["id"])["features"][0]["properties"]
    assert p["underground"] is True and "owner_best" not in p                    # OSM has no operator: nothing filled
    assert p["attrs"].get("TYPE") == "UNDERGROUND"


def test_daily_file_drops_part_days_and_completes_dec_31(grid, tmp_path):
    import netCDF4
    from aorc_tools.explorer.server import build
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2008-12-14T00", domain_end="2009-01-06T23")
    app = build(ws, source="synthetic", start_jobs=False)
    E = app.extensions["wup"]
    cfg = ChainConfig()
    jid = E["jobs"].create("x", "synthetic", cfg, ["25_18"], "2008-12-20T00", "2009-01-06T23")
    assert E["jobs"].run_now(jid)["status"] == "done"
    path = E["store"].daily_year("synthetic", cfg.id, 2008)
    rows = E["grid"].rows_for(E["grid"].cells_in_brick(25, 18))
    with netCDF4.Dataset(path) as ds:
        mx = ds["FWI_daily_max"][:].filled(np.nan)[:, E["store"].y[rows], E["store"].x[rows]]
        noon = ds["FWI_noon"][:].filled(np.nan)[:, E["store"].y[rows], E["store"].x[rows]]
        hmin = ds["FWI_hour_of_min"][:][:, E["store"].y[rows], E["store"].x[rows]]
        assert int(ds.daily_format) == 4
    dec19, dec20, dec31 = 353, 354, 365
    assert np.isnan(mx[dec19]).all()                     # starts 00 UTC Dec 20 = evening of Dec 19 locally: a part-day
    assert np.isfinite(mx[dec20]).all() and np.isfinite(mx[dec31]).all()   # Dec 31 completed from 2009's first chunk
    assert np.isfinite(noon[dec31]).all() and (np.asarray(hmin[dec31]) >= 0).all()
