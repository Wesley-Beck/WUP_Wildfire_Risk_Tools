"""
``WUP_Explorer --self-test``: check this computer before a long run, and leave a report to send back.

  environment   Python, the packaged/explorer packages and their versions, HDF5 build
  machine       memory, CPU count, free disk at the workspace, write permission, cloud-sync folders, path length
  network       NOAA AORC on S3 (opens a year and reads one real brick-chunk), Planetary Computer STAC,
                NASA AppEEARS, NOAA CDR bucket
  end-to-end    in a throw-away workspace: compute two 6-day chunks (synthetic weather), verify, make and check a
                small bundle, and hammer NetCDF from several threads (the HDF5 lock)
  real AORC     when S3 answered: the four indices for one real brick-chunk (2009, Baraga) with value checks

Writes exports/selftest_<time>.txt and .json in the workspace. Exit code 0 = no FAIL.
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

PACKAGES = [("numpy", True), ("pandas", True), ("xarray", True), ("zarr", True), ("s3fs", True), ("fsspec", True),
            ("netCDF4", True), ("cftime", True), ("flask", True), ("shapely", True), ("tzdata", True), ("openpyxl", True),
            ("rasterio", False), ("pystac_client", False), ("planetary_computer", False), ("h5netcdf", False),
            ("h5py", False), ("requests", False)]


class Report:
    def __init__(self):
        self.rows = []

    def add(self, section, name, status, detail="", **extra):
        self.rows.append({"section": section, "check": name, "status": status, "detail": str(detail), **extra})
        mark = {"PASS": "ok  ", "WARN": "WARN", "FAIL": "FAIL", "INFO": "    ", "SKIP": "skip"}[status]
        print(f"  [{mark}] {section:<11} {name:<34} {detail}", flush=True)

    def worst(self):
        st = [r["status"] for r in self.rows]
        return "FAIL" if "FAIL" in st else ("WARN" if "WARN" in st else "PASS")


def _timed(fn, timeout):
    out = {}

    def run():
        t0 = time.time()
        try:
            out["value"] = fn()
        except Exception as e:                               # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
        out["seconds"] = time.time() - t0
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return {"error": f"no answer within {timeout} s", "seconds": timeout}
    return out


def check_environment(R: Report):
    R.add("environment", "python", "PASS" if sys.version_info >= (3, 10) else "FAIL",
          f"{platform.python_version()} ({'packaged exe' if getattr(sys, 'frozen', False) else sys.executable})")
    R.add("environment", "platform", "INFO", f"{platform.platform()}")
    for name, required in PACKAGES:
        try:
            m = importlib.import_module(name)
            v = getattr(m, "__version__", "?")
            if name == "netCDF4":
                v = f"{v} (HDF5 {getattr(m, '__hdf5libversion__', '?')}, netCDF-C {getattr(m, '__netcdf4libversion__', '?')})"
            R.add("environment", name, "PASS", v)
        except Exception as e:                               # noqa: BLE001
            R.add("environment", name, "FAIL" if required else "WARN",
                  f"not importable ({type(e).__name__}){'' if required else ' - needed only for NDVI ingest'}")


def check_machine(R: Report, ws_dir: Path):
    from aorc_tools.explorer.runtime import disk_free_gb, ram_gb, sync_folder
    ram = ram_gb()
    R.add("machine", "memory", "PASS" if (ram or 0) >= 12 else ("WARN" if (ram or 0) >= 6 else "FAIL"),
          f"{ram:.1f} GB (about 2 GB per calculation worker)" if ram else "unknown")
    R.add("machine", "cpu cores", "INFO", os.cpu_count())
    free = disk_free_gb(ws_dir)
    R.add("machine", "free disk at the workspace", "PASS" if free >= 200 else ("WARN" if free >= 20 else "FAIL"),
          f"{free:.0f} GB free (full record: ~60-130 GB AORC inputs + ~30 GB results + bundle)")
    try:
        ws_dir.mkdir(parents=True, exist_ok=True)
        probe = ws_dir / f".write_probe_{os.getpid()}"
        probe.write_bytes(b"x" * 1024)
        os.replace(probe, probe.with_suffix(".2"))
        probe.with_suffix(".2").unlink()
        R.add("machine", "workspace writable", "PASS", str(ws_dir))
    except Exception as e:                                   # noqa: BLE001
        R.add("machine", "workspace writable", "FAIL", f"{ws_dir}: {e}")
    sf = sync_folder(ws_dir)
    R.add("machine", "cloud-sync folder", "WARN" if sf else "PASS",
          f"inside {sf}: sync can lock files mid-write; prefer a local folder" if sf else "no")
    n = len(str(ws_dir))
    if os.name == "nt":
        R.add("machine", "path length", "PASS" if n <= 150 else "WARN",
              f"{n} characters (Windows limit for full file paths is 260)")
    from aorc_tools.explorer.runtime import WorkspaceLock, WorkspaceBusy
    try:
        WorkspaceLock(ws_dir).acquire(mode="self-test").release()
        R.add("machine", "workspace not in use", "PASS", "no other explorer holds it")
    except WorkspaceBusy as e:
        R.add("machine", "workspace not in use", "INFO", str(e))
    R.add("machine", "local time", "INFO", f"{datetime.now().isoformat(timespec='seconds')} "
                                              f"({time.tzname[0]}/{time.tzname[1]})")


def check_network(R: Report, timeout=60):
    from aorc_tools.aorc_access import AORCDataLoader
    res = {}

    def aorc():
        ds = AORCDataLoader()._open_dataset(2009)
        v = ds["TMP_2maboveground"].isel(time=slice(3168, 3169), latitude=slice(1800, 1810), longitude=slice(1100, 1110))
        return float(v.values.mean())
    r = _timed(aorc, timeout)
    res["aorc"] = "error" not in r
    R.add("network", "NOAA AORC v1.1 on S3", "PASS" if res["aorc"] else "FAIL",
          f"opened 2009.zarr and read a sample in {r['seconds']:.1f} s" if res["aorc"] else r["error"][:200])

    def stac():
        import pystac_client
        cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1")
        items = list(cat.search(collections=["modis-13Q1-061"], bbox=[-89.5, 46.5, -89.0, 47.0],
                                datetime="2009-05-01/2009-05-31", max_items=1).items())
        return len(items)
    r = _timed(stac, timeout)
    R.add("network", "Microsoft Planetary Computer (MODIS)", "PASS" if "error" not in r and r.get("value") else "WARN",
          f"STAC search answered in {r['seconds']:.1f} s" if "error" not in r else r["error"][:200])

    def appeears():
        import requests
        resp = requests.get("https://appeears.earthdatacloud.nasa.gov/api/product", timeout=timeout)
        resp.raise_for_status()
        return len(resp.json())
    r = _timed(appeears, timeout)
    R.add("network", "NASA AppEEARS", "PASS" if "error" not in r else "WARN",
          f"answered in {r['seconds']:.1f} s" if "error" not in r else r["error"][:200])

    def cdr():
        import fsspec
        return len(fsspec.filesystem("s3", anon=True).ls("noaa-cdr-ndvi-pds/data/2009")[:3])
    r = _timed(cdr, timeout)
    R.add("network", "NOAA CDR NDVI bucket", "PASS" if "error" not in r else "WARN",
          f"listed in {r['seconds']:.1f} s" if "error" not in r else r["error"][:200])
    return res


def check_end_to_end(R: Report, real_aorc=False):
    from aorc_tools.explorer import bundle as bundlemod
    from aorc_tools.explorer import verify as verifymod
    from aorc_tools.explorer.chain import ChainConfig
    from aorc_tools.explorer.server import build
    from aorc_tools.explorer.workspace import Workspace
    tmp = Path(tempfile.mkdtemp(prefix="wup_selftest_"))
    try:
        ws = tmp / "ws"
        Workspace(ws).save_settings(domain_start="2009-05-01T00", domain_end="2009-05-12T23")
        app = build(ws, source="synthetic", start_jobs=False)
        E = app.extensions["wup"]
        t0 = time.time()
        jid = E["jobs"].create("selftest", "synthetic", ChainConfig(), ["25_18", "26_19"], "2009-05-01T00", "2009-05-12T23")
        j = E["jobs"].run_now(jid)
        R.add("end-to-end", "calculate 2 bricks x 2 chunks", "PASS" if j["status"] == "done" else "FAIL",
              f"{j['status']} in {time.time() - t0:.1f} s ({j['packets_done']:,} packets)")
        rep = verifymod.verify_record(E["store"], E["grid"], E["ctx"].source_for("synthetic"), ChainConfig(), E["ws"].settings,
                                      mode="full", bricks=["25_18", "26_19"])
        odd = ", ".join(f"{k} {v['status']}" for k, v in rep["checks"].items() if v["status"] not in ("PASS", "INFO"))
        R.add("end-to-end", "verify", "PASS" if rep["overall"] in ("PASS", "WARN") else "FAIL",
              rep["overall"] + (f": {odd}" if odd else ""))
        z = tmp / "b.zip"
        bundlemod.make(E["ws"], E["ctx"], E["store"], E["grid"], z, parts=["results", "catalog"])
        chk = bundlemod.check(z)
        R.add("end-to-end", "bundle + checksum", "PASS" if chk["status"] == "PASS" else "FAIL",
              f"{chk['ok']} files, {z.stat().st_size / 1e6:.1f} MB")
        # several threads on NetCDF at once (the HDF5 lock)
        from aorc_tools.explorer import hdf
        import numpy as np
        import xarray as xr
        nc = tmp / "x.nc"
        hdf.to_netcdf(xr.Dataset({"a": (("t", "y"), np.random.rand(100, 100))}), nc)
        stop, errors = threading.Event(), []
        c = app.test_client()
        pid = int(E["grid"].cells_in_brick(25, 18)[0])

        def reader():
            while not stop.is_set():
                try:
                    with hdf.open_dataset(nc) as ds:
                        float(ds["a"].values.sum())
                    c.post("/api/series", json={"point_ids": [pid], "start": "2009-05-02T00", "hours": 24, "mode": "store"})
                except Exception as e:                       # noqa: BLE001
                    errors.append(repr(e))
        ts = [threading.Thread(target=reader) for _ in range(4)]
        [t.start() for t in ts]
        time.sleep(5)
        stop.set()
        [t.join() for t in ts]
        R.add("end-to-end", "NetCDF from 4 threads (5 s)", "PASS" if not errors else "FAIL",
              "no errors" if not errors else errors[0][:200])
        if real_aorc:
            check_real_aorc(R, tmp / "real")
        else:
            R.add("real AORC", "one brick-chunk, 4 indices", "SKIP", "S3 not reachable from this computer")
    except Exception as e:                                   # noqa: BLE001
        import traceback
        R.add("end-to-end", "unexpected error", "FAIL", f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-2000:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_real_aorc(R: Report, ws: Path):
    from aorc_tools.explorer.server import build
    from aorc_tools.explorer.workspace import Workspace
    import numpy as np
    Workspace(ws).save_settings(domain_start="2009-05-13T00", domain_end="2009-05-18T23")
    app = build(ws, source="aorc", start_jobs=False)
    E = app.extensions["wup"]
    t0 = time.time()
    pid = int(E["grid"].cells_in_brick(25, 18)[100])
    r = app.test_client().post("/api/series", json={"point_ids": [pid], "start": "2009-05-13T00", "hours": 144,
                                                    "spinup_hours": 0, "mode": "live"})
    if r.status_code != 200:
        R.add("real AORC", "one brick-chunk, 4 indices", "FAIL", (r.get_json() or {}).get("error", r.status_code))
        return
    s = {x["id"]: np.array(x["values"][0], dtype=float) for x in r.get_json()["steps"]}
    tc = s.get("temperature_c", s.get("TMP_2maboveground", np.array([np.nan])) - 273.15)
    ok = (np.isfinite(s["FWI"]).mean() > 0.95 and -30 < np.nanmin(tc) and np.nanmax(tc) < 40
          and np.nanmax(s["FWI"]) < 150 and np.nanmin(s["ERC"]) >= 0)
    R.add("real AORC", "one brick-chunk, 4 indices", "PASS" if ok else "WARN",
          f"{time.time() - t0:.1f} s; T {np.nanmin(tc):.1f}..{np.nanmax(tc):.1f} C, FWI max {np.nanmax(s['FWI']):.1f}, "
          f"ERC max {np.nanmax(s['ERC']):.0f}")


def run_self_test(base, ws_dir, args) -> int:
    ws_dir = Path(ws_dir)
    R = Report()
    print(f"\nWUP Explorer self-test · workspace {ws_dir}\n")
    check_environment(R)
    check_machine(R, ws_dir)
    net = check_network(R, timeout=float(os.environ.get("WUP_SELFTEST_TIMEOUT", 60)))
    check_end_to_end(R, real_aorc=net.get("aorc", False))
    out_dir = ws_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    j = out_dir / f"selftest_{stamp}.json"
    t = out_dir / f"selftest_{stamp}.txt"
    j.write_text(json.dumps({"overall": R.worst(), "created": datetime.now().isoformat(timespec="seconds"),
                             "rows": R.rows}, indent=1), encoding="utf-8")
    t.write_text(f"WUP Explorer self-test {datetime.now().isoformat(timespec='seconds')} - overall {R.worst()}\n\n"
                 + "\n".join(f"[{r['status']}] {r['section']} / {r['check']}: {r['detail']}" for r in R.rows) + "\n",
                 encoding="utf-8")
    print(f"\nOverall: {R.worst()}.  Report: {t}\n(send that file, or the .json next to it, if anything is WARN or FAIL)\n")
    return 0 if R.worst() != "FAIL" else 1
