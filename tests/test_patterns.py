"""
Patterns tab: season windows (staffing / funding dates), groupings of high-danger cells, daily vs hourly FWI.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aorc_tools.explorer import patterns as P
from aorc_tools.explorer.chain import ChainConfig
from aorc_tools.explorer.workspace import Workspace


@pytest.fixture(scope="module")
def grid():
    from aorc_tools.explorer.grid import load_grid
    from aorc_tools.explorer.server import default_points_csv
    return load_grid(default_points_csv())


def test_season_windows_find_spring_and_fall_runs():
    year = 2001
    d = np.arange(365, dtype=float)
    z = 2 + 10 * np.exp(-0.5 * ((d - 120) / 10) ** 2) + 6 * np.exp(-0.5 * ((d - 280) / 8) ** 2)   # Apr 30 / Oct 7
    Z = np.vstack([z, z * 0.3]).T.astype(np.float32)
    w = P.season_windows(Z, year, 6.0)
    above = np.where(z >= 6)[0]
    spring = above[above < 196]
    fall = above[above >= 196]
    assert w["spring_start"][0] == spring.min() and w["spring_end"][0] == spring.max()
    assert w["fall_start"][0] == fall.min() and w["fall_end"][0] == fall.max() and w["fall_days"][0] == len(fall)
    assert np.isnan(w["spring_start"][1]) and w["spring_days"][1] == 0            # never high: no window, 0 days
    w4 = P.season_windows(np.vstack([np.insert(z, 59, z[58])] * 2).T.astype(np.float32), 2004, 6.0)   # leap year
    assert w4["spring_start"][0] == w["spring_start"][0]


def test_groupings_count_connected_high_areas(grid, tmp_path):
    from aorc_tools.explorer.server import build
    app = build(tmp_path / "ws", source="synthetic", start_jobs=False)
    pa = app.extensions["wup"]["patterns"]
    store = app.extensions["wup"]["store"]
    X = np.zeros((365, grid.n_cells), dtype=np.float32)
    iy, ix = store.y, store.x
    c1, c2 = grid.rows_for(grid.cells_in_brick(25, 18))[500], grid.rows_for(grid.cells_in_brick(24, 19))[3000]
    blob1 = (np.abs(iy - iy[c1]) <= 2) & (np.abs(ix - ix[c1]) <= 2)
    blob2 = (np.abs(iy - iy[c2]) <= 4) & (np.abs(ix - ix[c2]) <= 4)
    assert blob1.sum() >= 4 and blob2.sum() >= 20
    day = 150
    X[day, blob1 | blob2] = 50.0
    daily, sizes, big, high = pa._clusters(X, 2001, 40.0)
    row = next(r for r in daily if r["doy"] == day)
    area = P.cell_area_km2(grid)
    assert row["n_groups"] == 2
    assert row["max_km2"] == pytest.approx(max(area[blob1].sum(), area[blob2].sum()), rel=1e-3)
    assert high[blob1 | blob2].sum() == (blob1 | blob2).sum() and high[~(blob1 | blob2)].sum() == 0
    assert sum(r["n_groups"] for r in daily) == 2


def test_patterns_on_a_synthetic_record(grid, tmp_path, monkeypatch):
    from aorc_tools.explorer import lines as L
    from aorc_tools.explorer.server import build
    for mod in (L, P):
        monkeypatch.setattr(mod, "MIN_SEASON_FRACTION", 0.05)
    monkeypatch.setattr(L, "MIN_TREND_YEARS", 3)
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2007-04-01T00", domain_end="2009-06-30T23")
    app = build(ws, source="synthetic", start_jobs=False)
    E = app.extensions["wup"]
    cfg = ChainConfig()
    for y in (2007, 2008, 2009):
        jid = E["jobs"].create(f"y{y}", "synthetic", cfg, ["25_18", "25_19"], f"{y}-04-25T00", f"{y}-05-24T23")
        assert E["jobs"].run_now(jid)["status"] == "done"
    c = app.test_client()
    r = c.post("/api/patterns/run", json={"wait": True, "indices": ["FWI", "ERC"], "season_pct": 70}).get_json()
    t = E["tasks"].get(r["id"])
    assert t["status"] == "done", t
    aid = t["result"]["id"]
    s = c.get(f"/api/patterns/{aid}").get_json()
    fwi = s["indices"]["FWI"]
    assert fwi["thresholds"]["season"] <= fwi["thresholds"]["grouping"]
    assert "WUP mainland" in fwi["areas"] and "Isle Royale" in fwi["areas"] and any(k.startswith("brick ") for k in fwi["areas"])
    br = fwi["areas"]["brick 25_18"]
    assert br["years"] and "spring" in next(iter(br["years"].values()))
    assert sum(fwi["groupings"]["size_histogram_km2"].values()) > 0
    dvh = s["daily_vs_hourly"]
    assert -1 <= dvh["mainland"]["rank_r"] <= 1 and dvh["thresholds"]["hourly_max_p90"] > 0
    assert sum(dvh["peak_hour_histogram"].values()) > 0
    rows = c.get(f"/api/patterns/{aid}/table/season_windows?index=FWI&area=brick%2025_18").get_json()["rows"]
    assert rows and {r["window"] for r in rows} <= {"spring", "fall"}
    f = c.get(f"/api/patterns/{aid}/cells?key=FWI_compare.kappa").get_json()
    assert len(f["values"]) == grid.n_cells and any(v is not None for v in f["values"])
    for name in ("season_windows.csv", "groupings_yearly.csv", "groupings_daily.csv", "cells.csv", "summary.json"):
        assert c.get(f"/api/patterns/{aid}/file/{name}").status_code == 200
    assert c.get(f"/api/patterns/{aid}/file/..%2Fx").status_code in (400, 404)


# ----------------------------------------------------------------------------- driving variables

def test_shapley_shares_sum_to_r2_and_find_the_driver():
    from aorc_tools.explorer.drivers import dominant_driver, shapley_r2
    rng = np.random.default_rng(0)
    X = rng.normal(size=(4000, 3))
    y = 3 * X[:, 0] + 1 * X[:, 1] + rng.normal(size=4000)
    sh, r2 = shapley_r2(X, y)
    assert sh.sum() == pytest.approx(r2, abs=1e-9) and sh[0] > sh[1] > sh[2] >= -1e-3
    Z = np.array([[2.0, 0.5, np.nan], [0.1, -1.0, 3.0], [np.nan, np.nan, np.nan]])
    assert dominant_driver(Z).tolist() == [0, 2, -1]


def test_driver_analysis_on_synthetic_weather(grid, tmp_path):
    from aorc_tools.explorer.drivers import DriverAnalysis
    from aorc_tools.explorer.server import build
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-01-01T00", domain_end="2009-11-30T23")
    app = build(ws, source="synthetic", start_jobs=False)
    E = app.extensions["wup"]
    da = DriverAnalysis(E["ws"], E["grid"], E["store"], E["ctx"])
    da.sample = lambda n_per_brick=12, seed=7: {k: v for k, v in DriverAnalysis.sample(da, 4).items() if k in ("25_18", "24_19")}
    r = da.run("synthetic", ChainConfig(), years=[2009], n_per_brick=4)
    s = da.summary(r["id"])
    fwi = s["results"]["FWI"]["25_18"]["all"]
    assert set(fwi["shares"]) == {"temperature", "humidity", "wind", "rain_72h", "fuel_dryness_BUI"}
    assert sum(x for x in fwi["shares"].values() if x is not None) == pytest.approx(fwi["r2"], abs=1e-3)
    assert sum(x for x in fwi["dominant_on_high_days"].values()) == pytest.approx(1.0, abs=5e-3)   # rounded shares
    assert "fwi_split" in fwi and {"spring", "summer", "fall"} & set(s["results"]["FWI"]["25_18"])
    assert "wind" not in s["results"]["ERC"]["25_18"]["all"]["shares"]
    assert da.file(r["id"], "drivers.csv").exists()


def test_jobs_save_driver_diagnostics_that_match_the_record(grid, tmp_path):
    from aorc_tools.explorer.drivers import DriverAnalysis, diagnostics_path, diagnostics_sample
    from aorc_tools.explorer.server import build
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-04-01T00", domain_end="2009-11-30T23")
    app = build(ws, source="synthetic", start_jobs=False)
    E = app.extensions["wup"]
    cfg = ChainConfig()
    jid = E["jobs"].create("d", "synthetic", cfg, ["25_18"], "2009-04-01T00", "2009-05-12T23")
    assert E["jobs"].run_now(jid)["status"] == "done"
    sample = diagnostics_sample(grid)["25_18"]
    f = diagnostics_path(ws, "synthetic", cfg.id, 2009, 21, "25_18")
    assert f.exists()
    with np.load(f) as z:
        assert z["point_ids"].tolist() == sample and len(z["hours"]) == 144
        fwi_diag = z["FWI"].astype(float)
        t0 = pd.Timestamp("1970-01-01") + pd.Timedelta(hours=int(z["hours"][0]))
    times, steps, frac, _ = E["store"].series("synthetic", cfg.id, sample, t0, t0 + pd.Timedelta(hours=143))
    assert frac == 1.0
    np.testing.assert_allclose(fwi_diag, steps["FWI"], atol=0.05 + 0.002 * np.nanmax(steps["FWI"]))   # float16 vs int16 packing
    da = DriverAnalysis(E["ws"], E["grid"], E["store"], E["ctx"])
    r = da.run("synthetic", cfg, years=[2009], mode="diagnostics")
    s = da.summary(r["id"])
    assert s["from"]["diagnostics_brick_years"] == 1 and s["from"]["recomputed_brick_years"] == 0
    assert "25_18" in s["results"]["FWI"]
