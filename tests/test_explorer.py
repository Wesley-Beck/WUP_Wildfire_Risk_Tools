"""
Tests for the WUP Grid Explorer and the index implementations it relies on.

Fast and offline: synthetic AORC-shaped weather, plus small reference fixtures
from the official NRCan cffdrs-ng output (hourly FWI2025) and xclim (classic
daily FWI) in tests/data.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aorc_tools.explorer import timebase
from aorc_tools.explorer.chain import PRESETS, STATE_KEYS, ChainConfig
from aorc_tools.fire_indices.fwi import HourlyFWI
from aorc_tools.fire_indices.fwi_daily import DailyFWI

DATA = Path(__file__).parent / "data"
PINERY = (46.7578, -88.4028)


# ----------------------------------------------------------------------------- reference implementations

def test_hourly_fwi_reproduces_official_cffdrs_ng():
    """cffdrs-ng mode == NRCan NG_FWI.py on its own reference weather (PRF2007)."""
    df = pd.read_csv(DATA / "prf2007_hourly_reference_head.csv", comment="#")
    hr, sr, ss = df.hr.values, df.sunrise.values, df.sunset.values
    dl = ((sr <= hr) & (hr <= ss)) | ((hr < 6) & (sr <= hr + 24) & (hr + 24 <= ss))
    f = HourlyFWI(1, coefficients="cffdrs-ng")
    got = {k: [] for k in ("FFMC", "DMC", "DC", "ISI", "BUI", "FWI")}
    for i, r in enumerate(df.itertuples(index=False)):
        o = f.update(np.array([r.temp]), np.array([r.rh]), np.array([r.ws]), np.array([r.prec]), 0, int(r.hr),
                     daylight=np.array([dl[i]]))
        for k in got:
            got[k].append(float(o[k][0]))
    for k in got:
        assert np.max(np.abs(np.array(got[k]) - df[k.lower()].values)) < 2e-3, k


def test_legacy_port_is_not_cffdrs_ng():
    """The legacy port (daily coefficients, no interception) departs from the reference -- kept only to reproduce old outputs."""
    df = pd.read_csv(DATA / "prf2007_hourly_reference_head.csv", comment="#")
    hr, sr, ss = df.hr.values, df.sunrise.values, df.sunset.values
    dl = ((sr <= hr) & (hr <= ss))
    f = HourlyFWI(1, coefficients="legacy")
    dmc = []
    for i, r in enumerate(df.itertuples(index=False)):
        dmc.append(float(f.update(np.array([r.temp]), np.array([r.rh]), np.array([r.ws]), np.array([r.prec]), 0, int(r.hr),
                                  daylight=np.array([dl[i]]))["DMC"][0]))
    assert np.mean(np.abs(np.array(dmc) - df.dmc.values)) > 2.0


def test_daily_fwi_matches_xclim():
    df = pd.read_csv(DATA / "prf2007_daily_xclim_reference.csv", comment="#")
    d = DailyFWI(1)
    for r in df.itertuples(index=False):
        o = d.update(np.array([r.temp]), np.array([r.rh]), np.array([r.ws]), np.array([r.prec]), np.array([r.mon]))
        for k in ("FFMC", "DMC", "DC", "ISI", "BUI", "FWI"):
            assert abs(float(o[k][0]) - getattr(r, f"{k}_xclim")) < 1e-3, (k, r.yr, r.mon, r.day)


def test_hourly_state_round_trip_includes_canopy():
    f = HourlyFWI(3, coefficients="cffdrs-ng")
    for h in range(10):
        f.update(np.full(3, 20.0), np.full(3, 40.0), np.full(3, 10.0), np.full(3, 0.3 if h < 3 else 0.0), 140, h,
                 daylight=np.ones(3, bool))
    st = f.get_state()
    assert "canopy_drying" in st
    g = HourlyFWI(3, coefficients="cffdrs-ng", initial_state=st)
    a = f.update(np.full(3, 22.0), np.full(3, 35.0), np.full(3, 12.0), np.zeros(3), 140, 11, daylight=np.ones(3, bool))
    b = g.update(np.full(3, 22.0), np.full(3, 35.0), np.full(3, 12.0), np.zeros(3), 140, 11, daylight=np.ones(3, bool))
    assert np.allclose(a["FWI"], b["FWI"])


# ----------------------------------------------------------------------------- time basis

def test_solar_times_baraga():
    st = timebase.sun_times_utc(np.array([PINERY[0]]), np.array([PINERY[1]]), pd.Timestamp("2009-05-20"))
    sunrise = float(st["sunrise"][0])
    assert 10.0 < sunrise < 10.5                     # ~10:14 UTC = 06:14 EDT
    rep = timebase.day_report(PINERY[0], PINERY[1], "Baraga", "2009-05-20")
    assert rep["misclassified_hours"] >= 10          # the legacy UTC-vs-solar test is wrong about half the day
    assert rep["clock_abbr"] == "EDT"


def test_local_clock_and_lst():
    t = datetime(2009, 5, 20, 17)
    assert timebase.lst_hour(t, -5) == 12
    loc, abbr, off, dst = timebase.local_clock(t, "America/Detroit")
    assert (loc.hour, abbr, off, dst) == (13, "EDT", -4.0, True)
    loc, abbr, off, dst = timebase.local_clock(t, "America/Menominee")
    assert (loc.hour, abbr) == (12, "CDT")


# ----------------------------------------------------------------------------- app fixture (synthetic)

@pytest.fixture(scope="module")
def app(tmp_path_factory):
    from aorc_tools.explorer.server import build
    ws = tmp_path_factory.mktemp("ws")
    a = build(ws, source="synthetic", start_jobs=False)
    return a


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


def pinery_cell(app):
    pts = app.extensions["wup"]["grid"].points
    i = ((pts.latitude - PINERY[0]) ** 2 + (pts.longitude - PINERY[1]) ** 2).values.argmin()
    return int(pts.point_id.values[i]), f"{pts.ci.values[i]}_{pts.cj.values[i]}"


def test_default_config_is_verified_cffdrs_ng():
    assert ChainConfig().fwi_coefficients == "cffdrs-ng"
    assert PRESETS["legacy-batch"].fwi_coefficients == "legacy"
    assert {"canopy_drying", "d_ffmc", "d_fwi"} <= set(STATE_KEYS)


def test_live_series_has_hourly_and_daily_fwi(app, client):
    pid, _ = pinery_cell(app)
    r = client.post("/api/series", json={"point_ids": [pid], "start": "2009-05-13T00", "hours": 72, "spinup_hours": 168,
                                         "mode": "live"}).get_json()
    ids = [s["id"] for s in r["steps"]]
    for k in ("FWI", "FWI_daily", "FPI", "ERC", "BI", "wx_tier"):
        assert k in ids
    daily = np.array(next(s for s in r["steps"] if s["id"] == "FWI_daily")["values"][0], dtype=float)
    times = pd.DatetimeIndex(r["time"])
    changes = times[1:][np.abs(np.diff(daily)) > 0]
    assert len(changes) and all(t.hour == 17 for t in changes)     # updates at 12:00 LST (UTC-5) only


def test_job_store_matches_live_and_trace(app, client):
    pid, brick = pinery_cell(app)
    jobs = app.extensions["wup"]["jobs"]
    j = client.post("/api/jobs", json={"bricks": [brick], "start": "2009-05-13T00", "end": "2009-05-18T23"}).get_json()
    jobs.run_now(j["id"])
    assert jobs.get(j["id"])["status"] == "done"
    s = client.post("/api/series", json={"point_ids": [pid], "start": "2009-05-13T00", "hours": 144, "mode": "store"}).get_json()
    assert s["source"] == "store"
    live = client.post("/api/series", json={"point_ids": [pid], "start": "2009-05-13T00", "hours": 144, "spinup_hours": 720,
                                            "mode": "live"}).get_json()
    for v, tol in (("FWI", 0.051), ("FWI_daily", 0.051), ("ERC", 0.51), ("BI", 0.51)):
        a = np.array(next(x for x in s["steps"] if x["id"] == v)["values"][0], dtype=float)
        b = np.array(next(x for x in live["steps"] if x["id"] == v)["values"][0], dtype=float)
        assert np.nanmax(np.abs(a - b)) <= tol, v
    tr = client.post("/api/trace", json={"point_id": pid, "start": "2009-05-15T00", "hours": 24}).get_json()
    assert tr["origin"] == "store-state"
    a = np.array(tr["steps"]["FWI"], dtype=float)
    b = np.array(next(x for x in s["steps"] if x["id"] == "FWI")["values"][0], dtype=float)[48:72]
    assert np.nanmax(np.abs(a - b)) <= 0.051
    files = app.extensions["wup"]["store"].files("synthetic", ChainConfig().id, 2009)
    import netCDF4
    with netCDF4.Dataset(files[0]["path"]) as ds:
        assert {"FWI", "FWI_daily", "FPI", "ERC", "BI", "wx_tier"} <= set(ds.variables)
        assert {"end_canopy_drying", "end_d_dc"} <= set(ds.groups["chain_state"].variables)


def test_fire_catalog_and_check(app, client):
    fc = client.get("/api/fires").get_json()
    byid = {f["properties"]["id"]: f["properties"] for f in fc["features"]}
    assert byid["pinery-2009"]["in_grid"] and byid["summit-lake-2024"]["in_grid"]
    assert byid["craig-lake-2026"]["lat"] is None
    r = client.post("/api/fires/pinery-2009/check", json={"days_before": 5, "days_after": 1}).get_json()
    assert r["status"] == "ok" and r["synthetic"]
    assert r["window"]["discovery_local_day"] == "2009-05-20"
    assert {"FWI", "FWI_daily", "ERC", "BI"} <= set(r["indices"])
    assert len(r["reference_checks"]) == 3
    rs = r["reference_series"]                         # the earlier real-AORC Pinery run, compared series by series
    q = {x["quantity"]: x for x in rs["series"]}
    assert {"FWI_daily", "FWI", "FFMC", "ISI", "BUI"} <= set(q)
    assert len(q["FWI"]["ref"]) == 576 and q["FWI"]["ref"][7 * 24 + 18] == 11.74      # 2009-05-20 18Z
    assert q["FWI"]["stats"]["n"] > 300 and q["FWI_daily"]["stats"]["n"] >= 10
    assert q["FWI"]["cell_distance_km"] < 0.6 and q["FWI_daily"]["cell_distance_km"] < 0.6
    from aorc_tools.explorer.fires import load_reference_series
    with pytest.raises(ValueError):
        load_reference_series("../wup_fires.geojson/../../../server.py")
    r = client.post("/api/fires/craig-lake-2026/check", json={}).get_json()
    assert r["status"] == "no-location"


def test_fire_import_and_location(app, client):
    csv = ("IncidentNa,FireDiscov,IncidentSi,InitialLat,InitialLon,POOCounty,FireCause\n"
           "Test Ridge,2012-05-02 18:30:00,40,47.05,-88.55,Houghton,Human\n"
           "Elsewhere,2012-05-02,40,44.0,-85.0,Wexford,Human\n")
    r = client.post("/api/fires/import", data={"file": (io.BytesIO(csv.encode()), "fires.csv")},
                    content_type="multipart/form-data").get_json()
    assert r["added"] == 1 and r["outside_study_area"] == 1
    r = client.post("/api/fires/craig-lake-2026/location", json={"lat": 46.66, "lon": -88.18}).get_json()
    assert r["properties"]["in_grid"] and r["properties"]["location_precision"] == "approximate"


def test_season_job_spec(app):
    from aorc_tools.explorer import fires as F
    fire = app.extensions["wup"]["fires"].get("pinery-2009")
    spec = F.season_job_spec(app.extensions["wup"]["grid"], fire)
    assert spec["start"] == datetime(2009, 4, 1, 5) and spec["end"] == datetime(2009, 11, 1, 4)
    assert len(spec["bricks"]) == 1


def test_parse_when_variants():
    from aorc_tools.explorer.fires import parse_when
    assert parse_when(1729286229000) == ("2024-10-18T21:17:09Z", False)
    assert parse_when("2009-05-20") == ("2009-05-20", True)
    assert parse_when("20090520") == ("2009-05-20", True)
    assert parse_when("2009-05-20", "1430") == ("2009-05-20T14:30:00Z", False)


def test_compact_results_and_export_year(app, client):
    cov = client.get("/api/coverage").get_json()
    assert cov["files"]["bytes_per_packet"] < 3.0          # int16 + zlib
    r = client.post("/api/export/year", json={"year": 2009}).get_json()
    import netCDF4
    with netCDF4.Dataset(r["daily"]) as ds:
        assert "FWI_daily_noon" in ds.variables and "ERC_daily_max" in ds.variables
