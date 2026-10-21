"""
Tests for the data side of the WUP Grid Explorer: the AORC input store and offline mode, NDVI ingest
(Planetary Computer / AppEEARS / local-raster paths), the climate-dataset adapter (GLARM-like RegCM, WRF),
the full-record verifier, the research bundle, and workspace relocation.

All offline: a fake AORC Zarr store (tests/fake_aorc.py), synthetic MODIS GeoTIFFs on the real sinusoidal
projection (tests/fake_ndvi.py) and synthetic model output (tests/fake_datasets.py).
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aorc_tools.explorer.chain import ChainConfig
from aorc_tools.explorer.workspace import Workspace, rebase_path

SMALL = "25_18"                       # smallest brick (1,078 cells)
K22 = ("2009-05-13T00", "2009-05-18T23")   # exactly AORC time chunk k=22 of 2009


@pytest.fixture(scope="module")
def grid():
    from aorc_tools.explorer.grid import load_grid
    from aorc_tools.explorer.server import default_points_csv
    return load_grid(default_points_csv())


def _build(ws, **kw):
    from aorc_tools.explorer.server import build
    return build(ws, start_jobs=False, **kw)


def _run_job(app, source, bricks, start, end, cfg=None):
    E = app.extensions["wup"]
    jid = E["jobs"].create("t", source, cfg or ChainConfig(), bricks, start, end)
    E["jobs"].run_now(jid)
    j = E["jobs"].get(jid)
    assert j["status"] == "done", j
    return j


# ----------------------------------------------------------------------------- workspace relocation

def test_rebase_path_across_systems(tmp_path):
    assert rebase_path(r"C:\Users\wes\WUP_Explorer_data\results\a\b.nc", r"C:\Users\wes\WUP_Explorer_data", tmp_path) == \
        str(tmp_path / "results" / "a" / "b.nc")
    assert rebase_path("/x/ws/results/a.nc", "/x/ws", tmp_path) == str(tmp_path / "results" / "a.nc")
    assert rebase_path("/elsewhere/a.nc", "/x/ws", tmp_path) == "/elsewhere/a.nc"


def test_workspace_relocates_catalog(tmp_path):
    a = tmp_path / "a"
    ws = Workspace(a)
    with ws.conn() as c:
        c.execute("INSERT INTO exports (name, kind, path, bytes, created, note) VALUES ('x', 'f', ?, 1, '', '')",
                  (str(a / "exports" / "x.txt"),))
    shutil.copytree(a, tmp_path / "b")
    ws2 = Workspace(tmp_path / "b")
    assert ws2.relocated == 1
    with ws2.conn() as c:
        assert c.execute("SELECT path FROM exports").fetchone()[0] == str((tmp_path / "b").resolve() / "exports" / "x.txt")
    assert Workspace(tmp_path / "b").relocated == 0


# ----------------------------------------------------------------------------- AORC input store + offline

@pytest.fixture(scope="module")
def aorc_ws(tmp_path_factory, grid):
    """A workspace that computed one AORC chunk (fake S3 store) for one brick, keeping the inputs."""
    from tests.fake_aorc import FakeLoader, build_store
    root = tmp_path_factory.mktemp("aorc")
    build_store(root / "zarr", grid, 2009, chunks=[21, 22], bricks=[SMALL])
    loader = FakeLoader(root / "zarr")
    ws = root / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-07T00", domain_end=K22[1])
    app = _build(ws, source="aorc", loader=loader)
    _run_job(app, "aorc-s3", [SMALL], "2009-05-07T00", K22[1])
    return {"root": root, "ws": ws, "app": app, "loader": loader}


def test_inputs_kept_while_computing(aorc_ws):
    E = aorc_ws["app"].extensions["wup"]
    inv = E["ctx"].inputs.inventory("2009-05-07T00", K22[1])
    assert inv["brick_chunks"] == 2 and inv["per_year"][2009]["expected"] == 10
    assert inv["bytes_per_value"] < 1.5                                   # int16 + zlib
    assert E["ctx"].inputs.has(2009, 22, SMALL)


def test_offline_equals_online(aorc_ws, grid):
    from aorc_tools.explorer.inputs import PACK
    from aorc_tools.explorer.sources import AORCSource, OfflineMissing
    E = aorc_ws["app"].extensions["wup"]
    pids = grid.cells_in_brick(25, 18)[:40]
    on = AORCSource(cache_dir=aorc_ws["root"] / "c1", loader=aorc_ws["loader"], inputs=None, keep_inputs=False)
    off = AORCSource(cache_dir=aorc_ws["root"] / "c2", loader=None, inputs=E["ctx"].inputs, offline=True)
    t0, t1 = pd.Timestamp(K22[0]).to_pydatetime(), pd.Timestamp(K22[1]).to_pydatetime()
    a, b = on.fetch(grid, pids, t0, t1), off.fetch(grid, pids, t0, t1)
    for v in a.data:
        x, y = np.asarray(a.data[v], float), np.asarray(b.data[v], float)
        assert np.nanmax(np.abs(x - y)) <= 0.51 * PACK[v][0] + 1e-3 * PACK[v][0], v    # within half an int16 packing step
    with pytest.raises(OfflineMissing):
        off.fetch(grid, pids, pd.Timestamp("2009-06-01").to_pydatetime(), pd.Timestamp("2009-06-02").to_pydatetime())


def test_download_task_skips_present(aorc_ws):
    E = aorc_ws["app"].extensions["wup"]
    c = aorc_ws["app"].test_client()
    t = c.post("/api/data/aorc", json={"start": K22[0], "end": K22[1], "bricks": [SMALL]}).get_json()
    E["tasks"].run_now(t["id"])
    r = E["tasks"].get(t["id"])["result"]
    assert r["already_present"] == 1 and r["downloaded"] == 0


# ----------------------------------------------------------------------------- verifier

@pytest.fixture(scope="module")
def syn_ws(tmp_path_factory):
    ws = tmp_path_factory.mktemp("syn")
    Workspace(ws).save_settings(domain_start="2009-12-29T00", domain_end="2010-01-08T23")   # crosses a year boundary
    app = _build(ws, source="synthetic")
    _run_job(app, "synthetic", [SMALL], "2009-12-29T00", "2010-01-08T23")
    return app


def _verify(app, mode="full", bricks=(SMALL,)):
    from aorc_tools.explorer import verify
    E = app.extensions["wup"]
    return verify.verify_record(E["store"], E["grid"], E["ctx"].source_for("synthetic"), ChainConfig(), E["ws"].settings,
                                mode=mode, bricks=list(bricks))


def test_verify_passes_complete_record(syn_ws):
    rep = _verify(syn_ws)
    assert rep["overall"] == "PASS", rep["checks"]
    assert rep["checks"]["continuity"]["state_pairs_checked"] >= 4             # incl. 2009 k60 -> 2010 k0
    assert rep["checks"]["continuity"]["cold_start_at_domain"] == 1
    assert rep["checks"]["completeness"]["percent"] == 100.0
    whole = _verify(syn_ws, mode="quick", bricks=())
    assert whole["checks"]["completeness"]["status"] == "FAIL"                   # other bricks were never computed


def test_verify_catches_broken_continuity_and_missing_files(syn_ws, tmp_path):
    import netCDF4
    E = syn_ws.extensions["wup"]
    files = sorted(f["path"] for y in (2009, 2010) for f in E["store"].files("synthetic", ChainConfig().id, y))
    keep = {p: Path(p).read_bytes() for p in files}
    try:
        with netCDF4.Dataset(files[1], "a") as ds:                              # corrupt one start state
            v = ds.groups["chain_state"]["start_mc_dc"]
            v.set_auto_maskandscale(False)
            a = np.asarray(v[:])
            a[np.isfinite(a)] += 5.0
            v[:] = a
        rep = _verify(syn_ws)
        assert rep["checks"]["continuity"]["status"] == "FAIL" and rep["checks"]["continuity"]["state_mismatches"] >= 1
        Path(files[-1]).unlink()
        rep = _verify(syn_ws)
        assert rep["checks"]["completeness"]["status"] == "FAIL" and rep["years"][2010]["n_missing"] == 1
    finally:
        for p, b in keep.items():
            Path(p).write_bytes(b)
    assert _verify(syn_ws)["overall"] == "PASS"


def test_verify_task_and_report(syn_ws):
    E = syn_ws.extensions["wup"]
    r = syn_ws.test_client().post("/api/data/verify", json={"mode": "quick", "bricks": [SMALL], "wait": True}).get_json()
    t = E["tasks"].get(r["id"])
    assert t["status"] == "done" and t["result"]["overall"] == "PASS"
    html = Path(t["result"]["html"]).read_text()
    assert "Record verification" in html and "PASS" in html
    assert json.loads(Path(t["result"]["json"]).read_text())["bricks"] == [SMALL]


# ----------------------------------------------------------------------------- research bundle

def test_bundle_roundtrip_and_offline_rerun(aorc_ws, tmp_path):
    """Bundle a workspace with AORC inputs + results, unzip elsewhere, and recompute offline from the bundle."""
    from aorc_tools.explorer import bundle
    E = aorc_ws["app"].extensions["wup"]
    out = tmp_path / "b" / "wup_bundle.zip"
    r = bundle.make(E["ws"], E["ctx"], E["store"], E["grid"], out, parts=["results", "inputs_aorc", "catalog"])
    assert r["files"] > 3 and out.exists()
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        man = json.loads(z.read(next(n for n in names if n.endswith("MANIFEST.json"))))
        nc = next(i for i in z.infolist() if i.filename.endswith(".nc"))
        assert nc.compress_type == zipfile.ZIP_STORED                         # NetCDF is already compressed
    assert any(f["path"].startswith("WUP_Explorer_data/inputs/aorc/") for f in man["files"])
    assert man["provenance"]["configs"][0]["config_id"] == ChainConfig().id
    assert bundle.check(out)["status"] == "PASS"

    root = bundle.extract(out, tmp_path / "x")
    ws2 = root / "WUP_Explorer_data"
    Workspace(ws2).save_settings(offline=True)
    app2 = _build(ws2, source="aorc", loader=None)
    assert app2.extensions["wup"]["ws"].relocated == 0                            # relocated by the save_settings open above
    c = app2.test_client()
    pid = int(E["grid"].cells_in_brick(25, 18)[7])
    s = c.post("/api/series", json={"point_ids": [pid], "start": K22[0], "hours": 144, "mode": "store"}).get_json()
    assert s["source"] == "store"
    live = c.post("/api/series", json={"point_ids": [pid], "start": K22[0], "hours": 144, "spinup_hours": 144,
                                       "mode": "live"}).get_json()
    for v, tol in (("FWI", 0.051), ("ERC", 0.51), ("BI", 0.51)):
        a = np.array(next(x for x in s["steps"] if x["id"] == v)["values"][0], dtype=float)
        b = np.array(next(x for x in live["steps"] if x["id"] == v)["values"][0], dtype=float)
        assert np.nanmax(np.abs(a - b)) <= tol, v

    # tampering is detected
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(bad, "w") as zout:
        victim = next(i.filename for i in zin.infolist() if i.filename.endswith(".nc") and "/results/" in i.filename)
        for i in zin.infolist():
            data = zin.read(i.filename)
            zout.writestr(i, data[:-10] + b"0123456789" if i.filename == victim else data)
    chk = bundle.check(bad)
    assert chk["status"] == "FAIL" and len(chk["bad"]) == 1


def test_bundle_plan_and_endpoint(syn_ws, tmp_path):
    c = syn_ws.test_client()
    pl = c.get("/api/bundle/plan").get_json()
    assert set(pl["labels"]) == {"app", "results", "inputs_aorc", "inputs_datasets", "ndvi", "catalog", "lines", "daily"}
    assert "daily" not in pl["default_parts"] and "lines" in pl["default_parts"]
    assert pl["parts"]["results"]["bytes"] > 0 and pl["parts"]["app"]["files"] > 20
    inside = c.post("/api/bundle", json={"path": str(syn_ws.extensions["wup"]["ws"].root / "x.zip")})
    assert inside.status_code == 400                                                  # never inside the workspace itself
    r = c.post("/api/bundle", json={"path": str(tmp_path / "s.zip"), "parts": ["app", "results", "catalog"], "wait": True}).get_json()
    t = syn_ws.extensions["wup"]["tasks"].get(r["id"])
    assert t["status"] == "done", t
    with zipfile.ZipFile(tmp_path / "s.zip") as z:
        n = z.namelist()
    assert any(x.endswith("app/aorc_tools/explorer/verify.py") for x in n)
    assert any(x.endswith("README.txt") for x in n) and any(x.endswith("WUP_Explorer_data/workspace.db") for x in n)


# ----------------------------------------------------------------------------- NDVI ingest

@pytest.fixture(scope="module")
def ndvi_tifs(tmp_path_factory, grid):
    pytest.importorskip("rasterio")
    from aorc_tools.explorer import ndvi_ingest as NI
    from tests.fake_ndvi import make_items
    folder = tmp_path_factory.mktemp("tifs")
    starts = NI.composite_starts(2009)[8:12]                    # Apr 23 - Jun 10
    return folder, make_items(folder, NI.CellGrid(grid).bbox, starts), starts


def test_ndvi_ingest_planetary_path(ndvi_tifs, grid, tmp_path):
    import xarray as xr
    from aorc_tools.explorer import ndvi_ingest as NI
    from tests.fake_ndvi import cloudy, ndvi_field
    folder, items, starts = ndvi_tifs
    models = np.array(["G"] * len(grid.points))
    res = NI.run_ingest(grid, tmp_path, "planetary", "MOD13Q1", [2009], models, items_fn=lambda y, b: items)
    assert not res["errors"] and res["written"][0]["composites"] == 4
    lat, lon = grid.points.latitude.values, grid.points.longitude.values
    with xr.open_dataset(res["written"][0]["path"]) as ds:
        comp = ds["ND_composite"].values[:, grid.points.lat_idx.values - grid.lat_idx_range[0],
                                          grid.points.lon_idx.values - grid.lon_idx_range[0]]
    for i, s in enumerate(starts):
        mid = s + pd.Timedelta(days=8)
        truth = ndvi_field(lat, lon, mid)
        ok = np.isfinite(comp[i])
        cl = cloudy(lat, lon, mid)
        assert ok[~cl].mean() > 0.995                                              # every clear cell has a value
        assert cl[~ok].mean() > 0.98                                               # the gaps are the cloudy patch (QA 3)
        assert np.nanmax(np.abs(comp[i][ok] - truth[ok])) < 0.004                  # 231 m pixels averaged into 800 m cells
    assert (tmp_path / "FPI_Input_Static.nc").exists() and res["envelope"]


def test_ndvi_ingest_local_rasters_equals_planetary(ndvi_tifs, grid, tmp_path):
    import xarray as xr
    from aorc_tools.explorer import ndvi_ingest as NI
    folder, items, _ = ndvi_tifs
    models = np.array(["G"] * len(grid.points))
    a = NI.run_ingest(grid, tmp_path / "a", "planetary", "MOD13Q1", [2009], models, items_fn=lambda y, b: items)
    b = NI.run_ingest(grid, tmp_path / "b", "local", "MOD13Q1", [2009], models, folder=folder)
    assert not b["errors"]
    with xr.open_dataset(a["written"][0]["path"]) as x, xr.open_dataset(b["written"][0]["path"]) as y:
        xa, ya = x["ND_composite"].values, y["ND_composite"].values
    both = np.isfinite(xa) & np.isfinite(ya)
    assert np.isfinite(ya).sum() >= np.isfinite(xa).sum()     # no reliability band locally: cloudy pixels are kept
    assert both.sum() == np.isfinite(xa).sum()
    assert np.mean(np.abs(xa[both] - ya[both]) < 0.002) > 0.97   # identical except cells partly under the cloud mask


def test_ndvi_ingest_appeears_netcdf(grid, tmp_path):
    """The AppEEARS area-request NetCDF layout (time/lat/lon, layer-named variables)."""
    import xarray as xr
    from aorc_tools.explorer import ndvi_ingest as NI
    from tests.fake_ndvi import ndvi_field
    cells = NI.CellGrid(grid)
    lon0, lat0, lon1, lat1 = cells.bbox
    lat = np.arange(lat1, lat0, -1 / 480.0)                      # ~231 m geographic pixels
    lon = np.arange(lon0, lon1, 1 / 480.0)
    LON, LAT = np.meshgrid(lon, lat)
    starts = NI.composite_starts(2009)[9:11]
    nd = np.stack([np.round(ndvi_field(LAT, LON, s + pd.Timedelta(days=8)) / 1e-4) for s in starts]).astype(np.int16)
    rel = np.zeros_like(nd)
    rel[:, :40, :] = 3                                           # a cloudy strip along the north edge
    ds = xr.Dataset({"_250m_16_days_NDVI": (("time", "lat", "lon"), nd), "_250m_16_days_pixel_reliability": (("time", "lat", "lon"), rel)},
                    coords={"time": pd.DatetimeIndex(starts), "lat": lat, "lon": lon})
    path = tmp_path / "MOD13Q1.061_aid0001.nc"
    ds.to_netcdf(path)
    acc = NI.YearAccumulator(cells, 2009, "MOD13Q1")
    NI.AppEEARSMODIS("MOD13Q1").ingest_year(acc, files=[path])
    st, vals = acc.composites()
    assert len(st) == 2
    truth = ndvi_field(cells.cell_lat, cells.cell_lon, starts[0] + pd.Timedelta(days=8))
    ok = np.isfinite(vals[0])
    assert ok.mean() > 0.95 and np.nanmax(np.abs(vals[0][ok] - truth[ok])) < 0.004


def test_ndvi_daily_interpolation_and_gaps(grid):
    from aorc_tools.explorer import ndvi_ingest as NI
    cells = NI.CellGrid(grid)
    acc = NI.YearAccumulator(cells, 2009, "MOD13Q1")
    st = NI.composite_starts(2009)
    vals = np.full((9, cells.n), np.nan)
    vals[0], vals[1], vals[4], vals[8] = 0.3, 0.5, 0.8, 0.4  # 2 composites missing (48 d), then 3 missing (64 d)
    days, daily = acc.daily(st[:9], vals)
    mid = [(s + pd.Timedelta(days=8)).dayofyear - 1 for s in st[:9]]
    assert abs(daily[mid[0], 0] - 0.3) < 1e-6 and abs(daily[(mid[0] + mid[1]) // 2, 0] - 0.4) < 1e-6
    assert abs(daily[mid[2], 0] - 0.6) < 1e-6                 # bridged
    assert abs(daily[mid[4], 0] - 0.8) < 1e-6 and np.isnan(daily[mid[6], 0])   # not bridged
    assert abs(daily[mid[0] - 8, 0] - 0.3) < 1e-6 and np.isnan(daily[mid[8] + 9, 0])


# ----------------------------------------------------------------------------- climate datasets (GLARM-like)

def test_glarm_like_dataset_job_and_verify(tmp_path, grid):
    from aorc_tools.explorer import verify
    from tests.fake_datasets import regcm_like
    regcm_like(tmp_path / "SRF.2085.nc", start="2085-05-01", days=20)
    app = _build(tmp_path / "ws", source="synthetic")
    c = app.test_client()
    r = c.post("/api/datasets", json={"name": "GLARM-like RCP8.5", "preset": "regcm", "scenario": "RCP8.5",
                                      "paths": str(tmp_path / "SRF.*.nc")}).get_json()
    assert r["missing"] == [] and 15 < r["model_resolution_km"] < 22
    src = r["source"]
    E = app.extensions["wup"]
    d0, d1 = E["ctx"].source_for(src).domain(E["ws"].settings)
    assert pd.Timestamp(d0) == pd.Timestamp("2085-05-01") and pd.Timestamp(d1).day == 20
    _run_job(app, src, [SMALL], d0, d1)
    rep = verify.verify_record(E["store"], E["grid"], E["ctx"].source_for(src), ChainConfig(), E["ws"].settings,
                               mode="full", bricks=[SMALL])
    # an 18 km model regridded to 800 m can make a brick-wide FWI field uniform at the 0.1 packing step: a WARN, not a FAIL
    # (FPI is not computed for futures unless the NDVI climatology is opted in, so its season coverage only warns)
    assert all(v["status"] in ("PASS", "INFO") for k, v in rep["checks"].items()
               if k not in ("uniform_fields", "fpi_season_coverage")), rep["checks"]
    assert rep["checks"]["index_coverage"]["percent_valid"]["FWI"] == 100.0
    pid = int(grid.cells_in_brick(25, 18)[3])
    assert c.post("/api/source", json={"name": src}).status_code == 200
    s = c.post("/api/series", json={"point_ids": [pid], "start": "2085-05-05T00", "hours": 48, "mode": "store"}).get_json()
    fwi = np.array(next(x for x in s["steps"] if x["id"] == "FWI")["values"][0], dtype=float)
    assert s["source"] == "store" and np.isfinite(fwi).all() and fwi.max() > 0
    assert E["store"].files("synthetic", ChainConfig().id, 2085) == []          # futures stored apart from the record

    # offline copy of the WUP window, read back identically
    slug = src[3:]
    t = c.post(f"/api/datasets/{slug}/localize", json={"wait": True}).get_json()
    t = E["tasks"].get(t["id"])
    assert t["status"] == "done" and t["result"]["files"] == 1, t
    raw_def = json.loads((E["ws"].root / "datasets" / f"{slug}.json").read_text())
    assert raw_def["paths"][0].startswith("${WORKSPACE}/inputs/datasets/") and raw_def["remote_paths"]
    assert Path(t["result"]["dir"]).is_relative_to(E["ws"].root)
    live = c.post("/api/series", json={"point_ids": [pid], "start": "2085-05-05T00", "hours": 48, "spinup_hours": 96,
                                       "mode": "live"}).get_json()
    fwi_local = np.array(next(x for x in live["steps"] if x["id"] == "FWI")["values"][0], dtype=float)
    assert np.nanmax(np.abs(fwi_local - fwi)) <= 0.051                           # same weather after the copy
