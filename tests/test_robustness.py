"""
Robustness: what happens to a long unattended run when things go wrong.

  * network failures (S3 reads retried; a job that still fails retries itself later and resumes continuously)
  * a chunk computed earlier with a cold spin-up (e.g. a fire-season job) is recomputed when the record before it is
    filled in, so the finished record is continuous
  * damaged files (truncated by a power cut or a bad copy) are set aside and recomputed / refetched
  * one process per workspace (OS file lock), leftover half-written files cleaned up, disk-full stop
  * many threads touching NetCDF at once (one HDF5 lock) do not crash
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aorc_tools.explorer.chain import ChainConfig
from aorc_tools.explorer.workspace import Workspace

SMALL, OTHER = "25_18", "26_19"


@pytest.fixture(scope="module")
def grid():
    from aorc_tools.explorer.grid import load_grid
    from aorc_tools.explorer.server import default_points_csv
    return load_grid(default_points_csv())


def _build(ws, **kw):
    from aorc_tools.explorer.server import build
    return build(ws, start_jobs=False, **kw)


def _verify(app, source, bricks):
    from aorc_tools.explorer import verify
    E = app.extensions["wup"]
    return verify.verify_record(E["store"], E["grid"], E["ctx"].source_for(source), ChainConfig(), E["ws"].settings,
                                mode="full", bricks=list(bricks))


def _series(app, pid, start, hours):
    r = app.test_client().post("/api/series", json={"point_ids": [pid], "start": start, "hours": hours, "mode": "store"}).get_json()
    assert r["source"] == "store"
    return {s["id"]: np.array(s["values"][0], dtype=float) for s in r["steps"]}


# ----------------------------------------------------------------------------- network failures

@pytest.fixture()
def fake_s3(tmp_path, grid):
    from tests.fake_aorc import FakeLoader, build_store
    build_store(tmp_path / "zarr", grid, 2009, chunks=[20, 21, 22, 23], bricks=[SMALL])
    return FakeLoader(tmp_path / "zarr")


def test_s3_read_retries_then_succeeds(tmp_path, grid, fake_s3, monkeypatch):
    from aorc_tools.explorer.grid import Brick
    from aorc_tools.explorer.sources import AORCSource
    src = AORCSource(cache_dir=tmp_path / "c", loader=fake_s3)
    waits = []
    src._sleep = waits.append
    real = AORCSource._s3_read_once
    fails = {"n": 2}

    def flaky(self, *a):
        if fails["n"]:
            fails["n"] -= 1
            raise ConnectionResetError("simulated reset")
        return real(self, *a)
    monkeypatch.setattr(AORCSource, "_s3_read_once", flaky)
    arrays, times, meta, hit = src._raw_brick_chunk(grid, 2009, 21, Brick(25, 18))
    assert len(times) == 144 and np.isfinite(arrays["TMP_2maboveground"]).any()
    assert waits == [5, 20]                                           # two retries, growing waits


def test_job_survives_network_outage_and_stays_continuous(tmp_path, grid, fake_s3, monkeypatch):
    from aorc_tools.explorer.sources import AORCSource
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-24T23")
    app = _build(ws, source="aorc", loader=fake_s3)
    E = app.extensions["wup"]
    src = E["ctx"].source_for("aorc-s3")
    src._sleep = lambda s: None
    real = AORCSource._s3_read_once
    outage = {"calls": 0}

    def flaky(self, grid_, year, k, *a):
        outage["calls"] += 1
        if k == 22 and outage["calls"] < 12:                          # chunk 22 unreachable for a while
            raise TimeoutError("simulated S3 timeout")
        return real(self, grid_, year, k, *a)
    monkeypatch.setattr(AORCSource, "_s3_read_once", flaky)
    jid = E["jobs"].create("j", "aorc-s3", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-24T23")
    E["jobs"].run_now(jid)
    j = E["jobs"].get(jid)
    assert j["status"] == "retrying" and j["attempts"] == 1 and j["retry_at"]
    sleeps = []
    j = E["jobs"].run_until_settled(jid, sleep=sleeps.append)
    assert j["status"] == "done" and len(sleeps) >= 1
    rep = _verify(app, "aorc-s3", [SMALL])
    assert rep["checks"]["continuity"]["status"] == "PASS" and rep["checks"]["completeness"]["status"] == "PASS"


def test_offline_missing_is_not_retried(tmp_path, grid):
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-12T23", offline=True)
    app = _build(ws, source="aorc", loader=None)
    E = app.extensions["wup"]
    jid = E["jobs"].create("j", "aorc-s3", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-12T23")
    E["jobs"].run_now(jid)
    j = E["jobs"].get(jid)
    assert j["status"] == "error" and "offline" in j["error"].lower()


# ----------------------------------------------------------------------------- continuity repair

def test_season_job_first_then_full_record_is_continuous(tmp_path, grid):
    """A fire-season job (cold spin-up mid-record) followed by the full record: the season's chunks are
    recomputed from the record before them, and the result equals one straight continuous run."""
    a, b = tmp_path / "a", tmp_path / "b"
    for w in (a, b):
        Workspace(w).save_settings(domain_start="2009-04-01T00", domain_end="2009-06-10T23")
    app_a, app_b = _build(a, source="synthetic"), _build(b, source="synthetic")
    Ea, Eb = app_a.extensions["wup"], app_b.extensions["wup"]
    jid = Ea["jobs"].create("season", "synthetic", ChainConfig(), [SMALL], "2009-05-13T00", "2009-06-10T23")
    Ea["jobs"].run_now(jid)
    rep = _verify(app_a, "synthetic", [SMALL])
    assert rep["checks"]["completeness"]["status"] == "FAIL"           # April-May 12 not there yet
    jid = Ea["jobs"].create("record", "synthetic", ChainConfig(), [SMALL], "2009-04-01T00", "2009-06-10T23")
    Ea["jobs"].run_now(jid)
    rep = _verify(app_a, "synthetic", [SMALL])
    assert rep["overall"] in ("PASS", "WARN"), rep["checks"]
    assert rep["checks"]["continuity"]["status"] == "PASS" and rep["checks"]["continuity"]["spinup_after_gap"] == 0
    jid = Eb["jobs"].create("straight", "synthetic", ChainConfig(), [SMALL], "2009-04-01T00", "2009-06-10T23")
    Eb["jobs"].run_now(jid)
    pid = int(grid.cells_in_brick(25, 18)[11])
    sa, sb = _series(app_a, pid, "2009-05-13T00", 600), _series(app_b, pid, "2009-05-13T00", 600)
    for v in ("FWI", "FWI_daily", "ERC", "BI"):
        assert np.nanmax(np.abs(sa[v] - sb[v])) < 1e-6, v


# ----------------------------------------------------------------------------- damaged files

def test_truncated_results_file_is_found_set_aside_and_recomputed(tmp_path, grid):
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-30T23")
    app = _build(ws, source="synthetic")
    E = app.extensions["wup"]
    jid = E["jobs"].create("r", "synthetic", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-30T23")
    E["jobs"].run_now(jid)
    files = sorted(f["path"] for f in E["store"].files("synthetic", ChainConfig().id, 2009))
    victim = Path(files[2])
    data = victim.read_bytes()
    victim.write_bytes(data[: len(data) // 2])
    rep = _verify(app, "synthetic", [SMALL])                           # reports, does not crash
    assert rep["checks"]["readable_files"]["status"] == "FAIL" and rep["overall"] == "FAIL"
    r = app.test_client().post("/api/data/repair", json={"wait": True, "bricks": [SMALL]}).get_json()
    t = E["tasks"].get(r["id"])
    assert t["status"] == "done" and t["result"]["quarantined_files"] == 1
    assert (ws / "quarantine" / "log.jsonl").exists() and any((ws / "quarantine" / "results").iterdir())
    E["jobs"].resume(jid)
    E["jobs"].run_now(jid)
    rep = _verify(app, "synthetic", [SMALL])
    assert rep["overall"] in ("PASS", "WARN"), rep["checks"]


def test_merge_into_damaged_results_file_sets_it_aside(tmp_path, grid):
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-12T23")
    app = _build(ws, source="synthetic")
    E = app.extensions["wup"]
    jid = E["jobs"].create("r", "synthetic", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-12T23")
    E["jobs"].run_now(jid)
    f = Path(sorted(x["path"] for x in E["store"].files("synthetic", ChainConfig().id, 2009))[0])
    f.write_bytes(f.read_bytes()[:4000])
    jid = E["jobs"].create("o", "synthetic", ChainConfig(), [OTHER], "2009-05-01T00", "2009-05-12T23")
    E["jobs"].run_now(jid)                                             # writes OTHER into the same chunk files
    assert E["jobs"].get(jid)["status"] == "done"
    assert any((ws / "quarantine" / "results").iterdir())
    jid = E["jobs"].create("r2", "synthetic", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-12T23")
    E["jobs"].run_now(jid)                                             # SMALL's lost chunk is recomputed
    rep = _verify(app, "synthetic", [SMALL, OTHER])
    assert rep["overall"] in ("PASS", "WARN"), rep["checks"]


def test_truncated_input_chunk_is_refetched(tmp_path, grid, fake_s3):
    from aorc_tools.explorer.grid import Brick
    ws = tmp_path / "ws"
    app = _build(ws, source="aorc", loader=fake_s3)
    E = app.extensions["wup"]
    src = E["ctx"].source_for("aorc-s3")
    a1, *_ = src._raw_brick_chunk(grid, 2009, 21, Brick(25, 18))
    p = E["ctx"].inputs.path_for(2009, 21)
    p.write_bytes(p.read_bytes()[:3000])
    src2 = E["ctx"].download_source()
    a2, _, _, hit = src2._raw_brick_chunk(grid, 2009, 21, Brick(25, 18))
    assert not hit                                                     # fetched again from "S3"
    np.testing.assert_allclose(a2["TMP_2maboveground"], a1["TMP_2maboveground"], equal_nan=True)
    log = [json.loads(x) for x in (ws / "quarantine" / "log.jsonl").read_text().splitlines()]
    assert log[-1]["kind"] == "inputs"


def test_unreadable_settings_file_is_kept_and_defaults_used(tmp_path):
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(workers=3)
    (ws / "settings.json").write_text("{ not json")
    w = Workspace(ws)
    assert w.settings["workers"] == 2 and list(ws.glob("settings.json.bad-*"))


# ----------------------------------------------------------------------------- process-level safety

def test_one_process_per_workspace(tmp_path):
    from aorc_tools.explorer.runtime import WorkspaceBusy, WorkspaceLock
    a = WorkspaceLock(tmp_path).acquire(mode="browser", url="http://127.0.0.1:8765/")
    with pytest.raises(WorkspaceBusy) as e:
        WorkspaceLock(tmp_path).acquire(mode="headless")
    assert e.value.owner["url"] == "http://127.0.0.1:8765/" and "8765" in str(e.value)
    a.release()
    WorkspaceLock(tmp_path).acquire(mode="headless").release()          # free again


def test_leftover_tmp_files_are_cleaned(tmp_path):
    ws = Workspace(tmp_path / "ws")
    d = ws.root / "results" / "synthetic" / "x" / "2009"
    d.mkdir(parents=True)
    (d / "WUP_FDI_2009_k022_20090513.140213.tmp").write_bytes(b"x")
    (d / "keep.nc").write_bytes(b"x")
    (ws.root / "inputs" / "aorc" / "2009").mkdir(parents=True)
    (ws.root / "inputs" / "aorc" / "2009" / "AORC_WUP_2009_k022.99.tmp").write_bytes(b"x")
    assert ws.cleanup_tmp() == 2 and (d / "keep.nc").exists()


def test_disk_full_stops_job_without_retries(tmp_path, monkeypatch):
    from aorc_tools.explorer import jobs as jobsmod
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-12T23")
    app = _build(ws, source="synthetic")
    E = app.extensions["wup"]
    monkeypatch.setattr(jobsmod, "disk_free_gb", lambda p: 1.0)
    jid = E["jobs"].create("d", "synthetic", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-12T23")
    E["jobs"].run_now(jid)
    j = E["jobs"].get(jid)
    assert j["status"] == "error" and "disk nearly full" in j["error"]


def test_concurrent_netcdf_use_does_not_crash(tmp_path, grid):
    """Job-style writes, input-store reads/writes and xarray NDVI reads from several threads at once."""
    import subprocess
    import sys
    script = tmp_path / "hammer.py"
    script.write_text(f"""
import sys, threading, numpy as np, pandas as pd
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from pathlib import Path
from aorc_tools.explorer.server import build
from aorc_tools.explorer.chain import ChainConfig
from aorc_tools.explorer.workspace import Workspace
from aorc_tools.explorer import hdf
import xarray as xr
ws = Path({str(tmp_path / 'ws')!r})
Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-30T23", workers=3)
app = build(ws, source="synthetic", start_jobs=False)
E = app.extensions["wup"]
nc = Path({str(tmp_path / 'x.nc')!r})
xr.Dataset({{"a": (("t", "y"), np.random.rand(200, 300))}}).to_netcdf(nc)
stop = threading.Event()
def reader():
    while not stop.is_set():
        with hdf.open_dataset(nc) as ds:
            float(ds["a"].values.sum())
def series():
    c = app.test_client()
    while not stop.is_set():
        c.post("/api/series", json={{"point_ids": [int(E["grid"].cells_in_brick(25, 18)[0])], "start": "2009-05-02T00",
                                     "hours": 48, "mode": "store"}})
ts = [threading.Thread(target=reader) for _ in range(3)] + [threading.Thread(target=series) for _ in range(2)]
[t.start() for t in ts]
jid = E["jobs"].create("h", "synthetic", ChainConfig(), ["25_18", "26_19", "24_18"], "2009-05-01T00", "2009-05-30T23")
E["jobs"].run_now(jid)
stop.set(); [t.join() for t in ts]
print("OK", E["jobs"].get(jid)["status"])
""")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stderr[-3000:]
    assert "OK done" in r.stdout


# ----------------------------------------------------------------------------- jobs: data end, NDVI changes, scheduler

def test_job_stops_at_the_last_published_aorc_hour(tmp_path, grid, fake_s3, monkeypatch):
    """NOAA has published through May 18: the job plans only the hours that exist and finishes 'done'."""
    from datetime import datetime
    from aorc_tools.explorer.sources import AORCSource
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-24T23")
    monkeypatch.setattr(AORCSource, "last_time", lambda self, year: datetime(2009, 5, 18, 23))
    app = _build(ws, source="aorc", loader=fake_s3)
    E = app.extensions["wup"]
    jid = E["jobs"].create("j", "aorc-s3", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-24T23")
    j = E["jobs"].run_now(jid)
    assert j["status"] == "done", j
    assert j["tasks_total"] == 3                                        # chunks 20-22 (May 1-18); chunk 23 not yet
    assert E["ctx"].source_for("aorc-s3").domain(E["ws"].settings)[1] == datetime(2009, 5, 18, 23)


def test_hours_without_any_weather_stop_the_job_without_retries(tmp_path, grid, fake_s3, monkeypatch):
    """The store has hours past the published data (all missing): nothing is saved for them, no retry loop."""
    from aorc_tools.explorer.sources import AORCSource

    def unknown(self, year):
        raise OSError("no listing")
    monkeypatch.setattr(AORCSource, "last_time", unknown)
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-30T23")
    app = _build(ws, source="aorc", loader=fake_s3)
    E = app.extensions["wup"]
    E["ctx"].source_for("aorc-s3")._sleep = lambda s: None
    jid = E["jobs"].create("j", "aorc-s3", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-30T23")
    j = E["jobs"].run_now(jid)
    assert j["status"] == "error" and "no weather" in j["error"] and not j["retry_at"]
    assert j["tasks_done"] == 4                                         # chunks 20-23 were saved before it


def test_changed_ndvi_inputs_make_saved_chunks_recompute(tmp_path, grid, monkeypatch):
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-12T23")
    app = _build(ws, source="synthetic")
    E = app.extensions["wup"]
    cfg = ChainConfig()
    jid = E["jobs"].create("a", "synthetic", cfg, [SMALL], "2009-05-01T00", "2009-05-12T23")
    E["jobs"].run_now(jid)
    rows = [E["store"].chunk_row("synthetic", cfg.id, 2009, k, SMALL) for k in (20, 21)]
    assert all(r["ndvi_sig"] is not None for r in rows)
    jid = E["jobs"].create("b", "synthetic", cfg, [SMALL], "2009-05-01T00", "2009-05-12T23")
    E["jobs"].run_now(jid)                                              # nothing changed: nothing recomputed
    assert [E["store"].chunk_row("synthetic", cfg.id, 2009, k, SMALL)["_rowid"] for k in (20, 21)] == [r["_rowid"] for r in rows]
    monkeypatch.setattr(type(E["ctx"]), "ndvi_signature", lambda self, c, s, y: "new-ndvi-files")
    jid = E["jobs"].create("c", "synthetic", cfg, [SMALL], "2009-05-01T00", "2009-05-12T23")
    j = E["jobs"].run_now(jid)
    assert j["status"] == "done"
    new = [E["store"].chunk_row("synthetic", cfg.id, 2009, k, SMALL) for k in (20, 21)]
    assert all(r["ndvi_sig"] == "new-ndvi-files" for r in new)
    assert all(n["_rowid"] != o["_rowid"] for n, o in zip(new, rows))
    rep = _verify(app, "synthetic", [SMALL])
    assert rep["checks"]["continuity"]["status"] == "PASS"


def test_unexpected_error_in_a_job_is_retried_not_left_running(tmp_path, grid, monkeypatch):
    from aorc_tools.explorer.jobs import JobRunner as JobManager
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-12T23")
    app = _build(ws, source="synthetic")
    E = app.extensions["wup"]
    real = JobManager.chunks_for
    boom = {"n": 1}

    def flaky(self, *a, **k):
        if boom["n"]:
            boom["n"] -= 1
            raise RuntimeError("something nobody planned for")
        return real(self, *a, **k)
    jid = E["jobs"].create("j", "synthetic", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-12T23")
    monkeypatch.setattr(JobManager, "chunks_for", flaky)
    j = E["jobs"].run_now(jid)
    assert j["status"] == "retrying" and "nobody planned" in j["error"]
    j = E["jobs"].run_until_settled(jid, sleep=lambda s: None)
    assert j["status"] == "done"


def test_scheduler_thread_survives_its_own_errors(tmp_path, grid, monkeypatch):
    import time
    from aorc_tools.explorer.jobs import JobRunner as JobManager
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-06T23")
    app = _build(ws, source="synthetic")
    E = app.extensions["wup"]
    real = JobManager._promote_retries
    boom = {"n": 1}

    def flaky(self):
        if boom["n"]:
            boom["n"] -= 1
            raise OSError("database is locked")
        return real(self)
    monkeypatch.setattr(JobManager, "_promote_retries", flaky)
    monkeypatch.setattr(E["jobs"]._stop, "wait", lambda timeout=None: False)   # no 5 s pause after the error
    jid = E["jobs"].create("j", "synthetic", ChainConfig(), [SMALL], "2009-05-01T00", "2009-05-06T23")
    E["jobs"].start()
    try:
        t0 = time.time()
        while E["jobs"].get(jid)["status"] != "done" and time.time() - t0 < 120:
            time.sleep(0.2)
        assert E["jobs"].get(jid)["status"] == "done" and boom["n"] == 0
        assert E["jobs"]._thread.is_alive()
    finally:
        monkeypatch.undo()
        E["jobs"].stop()


def test_unattended_run_without_aorc_stops_instead_of_using_demo_weather(tmp_path, monkeypatch, capsys):
    import aorc_tools.explorer.__main__ as m
    monkeypatch.setattr(m, "s3_reachable", lambda timeout=20.0: False)
    monkeypatch.setattr(m, "S3_PROBE_WAIT", 0)
    ws = tmp_path / "ws"
    rc = m.main(["--full-record", "--workspace", str(ws), "--no-browser", "--no-ndvi"])
    assert rc == 4
    assert "cannot be reached" in capsys.readouterr().err
    w = Workspace(ws)
    assert w.settings.get("source") != "synthetic"
    with w.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    from aorc_tools.explorer.runtime import WorkspaceLock
    WorkspaceLock(ws).acquire().release()                               # lock released on the way out


# ----------------------------------------------------------------------------- server input checks

@pytest.fixture()
def client(tmp_path):
    app = _build(tmp_path / "ws", source="synthetic")
    return app.test_client()


def test_requests_from_other_web_sites_are_refused(client):
    r = client.post("/api/settings", json={"workers": 2}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/settings", json={"workers": 2}, headers={"Origin": "http://127.0.0.1:9000"})
    assert r.status_code == 200 and r.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:9000"
    r = client.get("/api/status", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "Access-Control-Allow-Origin" not in client.get("/api/status").headers


def test_settings_are_validated(client):
    for bad in ({"workers": "lots"}, {"workers": 0}, {"min_free_gb": -1}, {"auto_resume": "yes"},
                {"domain_end": "not a date"}):
        r = client.post("/api/settings", json=bad)
        assert r.status_code == 400 and "setting" in r.get_json()["error"], bad
    r = client.post("/api/settings", json={"workers": "3", "domain_end": "2026-08-31T23", "evil": 1})
    s = r.get_json()
    assert r.status_code == 200 and s["workers"] == 3 and s["domain_end"] == "2026-08-31T23" and "evil" not in s


def test_dataset_names_cannot_escape_the_workspace(client):
    for slug in ("..%2F..%2Fsettings", "UPPER", "a" * 100, "-x"):
        r = client.delete(f"/api/datasets/{slug}")
        assert r.status_code in (400, 404), (slug, r.status_code)
    r = client.post("/api/datasets/..%2Fjobs/localize", json={})
    assert r.status_code in (400, 404)


def test_bad_requests_get_json_errors_not_html(client):
    r = client.post("/api/series", json={"point_ids": "x"})
    assert r.status_code == 400 and "error" in r.get_json()
    r = client.post("/api/series", data="{not json", content_type="application/json")
    assert r.status_code == 400 and "error" in r.get_json()


def test_live_calculation_size_is_capped(client, grid):
    pids = []
    for b in grid.bricks():
        pids += [int(p) for p in grid.cells_in_brick(*map(int, b["key"].split("_")))]
        if len(pids) >= 2500:
            break
    assert 2500 <= len(pids) <= 12000
    r = client.post("/api/series", json={"point_ids": pids, "start": "2009-05-01T00", "hours": 1400, "mode": "live",
                                         "spinup_hours": 0})
    assert r.status_code == 413 and "too large" in r.get_json()["error"]


# ----------------------------------------------------------------------------- store: coverage cache, climatology

def test_coverage_is_cached_until_the_catalog_changes(tmp_path, grid):
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-12T23")
    app = _build(ws, source="synthetic")
    E = app.extensions["wup"]
    cfg = ChainConfig()
    c1 = E["store"].coverage("synthetic", cfg.id, "2009-05-01T00", "2009-05-12T23")
    c2 = E["store"].coverage("synthetic", cfg.id, "2009-05-01T00", "2009-05-12T23")
    assert c2.get("cached") and not c1.get("cached")
    jid = E["jobs"].create("a", "synthetic", cfg, [SMALL], "2009-05-01T00", "2009-05-12T23")
    E["jobs"].run_now(jid)
    c3 = E["store"].coverage("synthetic", cfg.id, "2009-05-01T00", "2009-05-12T23")
    assert not c3.get("cached")
    assert c3["packets_computed"] > c1["packets_computed"] == 0


def test_climatology_percentiles_are_exact(tmp_path, grid):
    from aorc_tools.explorer.store import FILL_I2, PACK, STORED
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-12T23")
    app = _build(ws, source="synthetic")
    E = app.extensions["wup"]
    cfg = ChainConfig()
    jid = E["jobs"].create("a", "synthetic", cfg, [SMALL, OTHER], "2009-05-01T00", "2009-05-12T23")
    E["jobs"].run_now(jid)
    out = E["store"].build_climatology("synthetic", cfg.id, tmp_path / "clim.json", stride=1)
    pooled = {v: [] for v in STORED}
    for f in E["store"].files("synthetic", cfg.id):
        raw = E["store"]._read(f["path"], STORED)
        for v in STORED:
            if v in raw:
                a = np.asarray(raw[v]).ravel()
                pooled[v].append(a[a != FILL_I2].astype(float) * PACK[v][0])
    for v, q in out["vars"].items():
        ref = np.percentile(np.concatenate(pooled[v]), np.arange(101))
        np.testing.assert_allclose(q["q"], ref, atol=1e-6 * max(1.0, np.abs(ref).max()), err_msg=v)


def test_year_export_while_a_job_runs(tmp_path, grid):
    """Exporting a year takes the HDF5 lock in small steps, so it can run beside a job (and neither breaks)."""
    ws = tmp_path / "ws"
    Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-06-10T23", workers=2)
    app = _build(ws, source="synthetic")
    E = app.extensions["wup"]
    cfg = ChainConfig()
    jid = E["jobs"].create("a", "synthetic", cfg, [SMALL], "2009-05-01T00", "2009-05-24T23")
    E["jobs"].run_now(jid)
    jid = E["jobs"].create("b", "synthetic", cfg, [OTHER], "2009-05-01T00", "2009-06-10T23")
    th = threading.Thread(target=E["jobs"].run_now, args=(jid,))
    th.start()
    out = E["store"].export_year("synthetic", cfg.id, 2009, tmp_path / "exports")
    th.join()
    assert E["jobs"].get(jid)["status"] == "done"
    import netCDF4
    for k in ("hourly", "daily"):
        with netCDF4.Dataset(out[k]) as ds:
            assert ds.dimensions["time"].size > 0
    assert not list((tmp_path / "exports").glob("*.tmp"))
