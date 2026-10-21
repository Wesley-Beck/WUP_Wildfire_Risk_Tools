"""
WUP Grid Explorer — one command (or one double-click) to run everything.

    python -m aorc_tools.explorer                 # opens the browser at http://127.0.0.1:8765/
    python -m aorc_tools.explorer --source synthetic --workspace D:\\fire\\ws
    python -m aorc_tools.explorer --fire-checks       # no browser: compute every known fire's season,
                                                      # check each fire, write CSV/HTML/JSON reports
    python -m aorc_tools.explorer --full-record       # no browser: NDVI (if missing) -> compute the whole
                                                      # record (resumable) -> verify -> report
    python -m aorc_tools.explorer --download-aorc 1979-02-01 2026-09-30   # fill the local AORC store
    python -m aorc_tools.explorer --verify full       # check the saved record, write an HTML/JSON report
    python -m aorc_tools.explorer --bundle D:\\WUP_bundle.zip   # one zip with app + data + results + manifest
    python -m aorc_tools.explorer --offline           # AORC only from the local store (no internet)

The workspace folder keeps every result, job, saved selection, saved view and
export, so closing and reopening simply continues where you left off. When run
as the packaged WUP_Explorer.exe the workspace defaults to a WUP_Explorer_data
folder next to the .exe.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def port_free(host, port) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) != 0


def already_running(host, port) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=2) as r:
            return r.status == 200 and b"workspace" in r.read(4000)
    except Exception:
        return False


def s3_reachable(timeout=20.0) -> bool:
    """Can we open this year's AORC store? (runs in a thread so a hung network can't block start-up)."""
    ok = {"v": False}

    def probe():
        try:
            from aorc_tools.aorc_access import AORCDataLoader
            from datetime import datetime
            AORCDataLoader()._open_dataset(datetime.now().year - 1)
            ok["v"] = True
        except Exception:
            ok["v"] = False

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout)
    return ok["v"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="WUP_Explorer", description="WUP Grid Explorer: hourly fire-danger indices for the Western Upper Peninsula")
    ap.add_argument("--workspace", default=None, help="folder for results, jobs and saved views (default: WUP_Explorer_data next to the program)")
    ap.add_argument("--source", choices=["auto", "aorc", "synthetic"], default="auto",
                    help="auto (default): the real AORC archive if reachable, otherwise demo weather")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--points", default=None, help="points_index.csv (default: bundled copy)")
    ap.add_argument("--static-dir", default=None, help="NDVI / fuel-model inputs (default: data/static next to the program)")
    ap.add_argument("--output", default=None, help="legacy batch record folder (default: data/output)")
    ap.add_argument("--max-cells", type=int, default=12000)
    ap.add_argument("--fire-checks", action="store_true",
                    help="headless: calculate each known fire's season (Apr-Oct, its brick), check every fire, write reports, exit")
    ap.add_argument("--fires", default="", help="with --fire-checks: comma-separated fire ids (default: all in the catalog)")
    ap.add_argument("--no-season", action="store_true", help="with --fire-checks: do not compute seasons (rank only what is saved)")
    ap.add_argument("--config", default="hourly-default", help="preset name (hourly-default, legacy-batch) or a JSON configuration")
    g = ap.add_argument_group("data and the full record (headless; each step skips what is already done)")
    g.add_argument("--offline", action="store_true", help="read AORC only from the local input store (WUP_Explorer_data/inputs/aorc)")
    g.add_argument("--download-aorc", nargs=2, metavar=("START", "END"), help="download AORC for all bricks into the local store")
    g.add_argument("--ingest-ndvi", nargs="?", const="2000-%d" % (time.localtime().tm_year), metavar="YEARS",
                   help="build FPI's NDVI inputs (default 2000-now): YEARS like 2000-2026")
    g.add_argument("--ndvi-backend", default="planetary", choices=["planetary", "appeears", "cdr", "local"])
    g.add_argument("--ndvi-product", default="MOD13Q1", choices=["MOD13Q1", "MOD13A1", "MOD13A2"])
    g.add_argument("--ndvi-folder", default=None, help="with --ndvi-backend local: folder of NDVI GeoTIFF/NetCDF files")
    g.add_argument("--compute", nargs=2, metavar=("START", "END"), help="compute all bricks for a period (resumable)")
    g.add_argument("--verify", nargs="?", const="quick", choices=["quick", "full"], help="verify the saved record")
    g.add_argument("--full-record", action="store_true",
                   help="NDVI if missing -> compute the whole domain -> full verification (each step resumes)")
    g.add_argument("--bundle", default=None, metavar="ZIP", help="write a research bundle zip (app, inputs, NDVI, results, manifest)")
    g.add_argument("--bundle-parts", default="app,results,inputs_aorc,inputs_datasets,ndvi,catalog",
                   help="comma-separated: app, results, inputs_aorc, inputs_datasets, ndvi, catalog")
    args = ap.parse_args(argv)
    headless = any([args.fire_checks, args.download_aorc, args.ingest_ndvi, args.compute, args.verify, args.full_record,
                    args.bundle])

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    base = base_dir()
    ws_dir = Path(args.workspace) if args.workspace else default_workspace(base)
    url = f"http://{args.host}:{args.port}/"

    if not headless and not port_free(args.host, args.port):
        if already_running(args.host, args.port):
            print(f"WUP Grid Explorer is already running at {url} - opening it.")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        for p in range(args.port + 1, args.port + 50):
            if port_free(args.host, p):
                args.port, url = p, f"http://{args.host}:{p}/"
                break

    from aorc_tools.explorer.server import build
    from aorc_tools.explorer.workspace import Workspace

    ws = Workspace(ws_dir)
    if ws.relocated:
        print(f"Workspace moved here - {ws.relocated} catalog paths now point to {ws.root}")
    source = args.source
    offline = bool(args.offline)
    local_aorc = any((ws.root / "inputs" / "aorc").rglob("*.nc")) if (ws.root / "inputs" / "aorc").exists() else False
    if source == "auto":
        if offline:
            source = "aorc"
        else:
            print("Checking the NOAA AORC archive on S3 ...", flush=True)
            if s3_reachable():
                source = "aorc"
            elif local_aorc:
                source, offline = "aorc", True
                print("  AORC on S3 is not reachable - using the LOCAL AORC store (offline mode).")
            else:
                source = "synthetic"
                print("  AORC is not reachable from this computer right now - starting with SYNTHETIC demo weather.\n"
                      "  (Switch to the real archive from the source badge once you are online.)")
    elif source == "aorc" and local_aorc and not offline and not s3_reachable(8.0):
        offline = True
        print("  AORC on S3 is not reachable - using the LOCAL AORC store (offline mode).")
    ws.save_settings(source=source, offline=offline)

    static_dir = Path(args.static_dir) if args.static_dir else default_static_dir(base, ws.root)
    output = Path(args.output) if args.output else base / "data" / "output"
    if args.fire_checks:
        return run_fire_checks(ws_dir, source, static_dir, output, args)
    if headless:
        return run_headless(ws_dir, source, static_dir, output, args)
    app = build(ws_dir, points_csv=args.points, source=source, static_dir=static_dir, output_base=output,
                max_cells=args.max_cells)

    print("\n" + "=" * 64)
    print(" WUP Grid Explorer")
    print(f"   open:      {url}")
    print(f"   workspace: {ws.root}")
    print(f"   data:      {('NOAA AORC v1.1 (local store, offline)' if offline else 'NOAA AORC v1.1 (S3)') if source == 'aorc' else 'SYNTHETIC demo weather (not real data)'}")
    print(f"   NDVI/fuel: {static_dir}")
    print("   stop:      close this window or press Ctrl+C (jobs continue next time)")
    print("=" * 64 + "\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions["wup"]["jobs"].stop()
    return 0


def default_workspace(base: Path) -> Path:
    """WUP_Explorer_data next to the program; in an unzipped research bundle the launchers live in app/ and the
    workspace one level up, so look there too."""
    for c in (base / "WUP_Explorer_data", base.parent / "WUP_Explorer_data"):
        if (c / "workspace.db").exists():
            return c
    return base / "WUP_Explorer_data"


def default_static_dir(base: Path, ws_root: Path) -> Path:
    """NDVI + fuel-model inputs: the folder that has FPI_Input_Static.nc, preferring data/static next to the program
    (older layout) and otherwise the workspace's inputs/static (where NDVI ingest writes and bundles carry it)."""
    cands = [base / "data" / "static", ws_root / "inputs" / "static"]
    for c in cands:
        if (c / "FPI_Input_Static.nc").exists():
            return c
    for c in cands:
        if c.exists() and any(c.iterdir()):
            return c
    return ws_root / "inputs" / "static"


def _bar(done, total, note=""):
    pct = 100.0 * done / total if total else 100.0
    print(f"\r    {pct:6.2f} %  {str(note)[:70]:<70}", end="", flush=True)


def run_headless(ws_dir, source, static_dir, output, args) -> int:
    """Data and full-record steps without the browser. Every step is resumable: run the same command again."""
    from aorc_tools.explorer.chain import PRESETS, ChainConfig
    from aorc_tools.explorer.server import build

    cfg = PRESETS[args.config] if args.config in PRESETS else ChainConfig.from_dict(json.loads(args.config))
    app = build(ws_dir, points_csv=args.points, source=source, static_dir=static_dir, output_base=output, start_jobs=False)
    E, c = app.extensions["wup"], app.test_client()
    ws, tasks, jobs, grid = E["ws"], E["tasks"], E["jobs"], E["grid"]
    src_name = "aorc-s3" if source == "aorc" else source
    d0, d1 = E["ctx"].source_for(src_name).domain(ws.settings)
    rc = 0

    def run_task(resp):
        t = resp.get_json() or {}
        if "id" not in t:
            print("  error:", t["error"])
            return None
        tid = t["id"]
        print(f"  task #{tid}: {t['name']}", flush=True)
        if tasks.get(tid)["status"] == "queued":
            tasks.run_now(tid)
        t = tasks.get(tid)
        print(f"\n    {t['status']}" + (f" - {t['error']}" if t.get("error") else ""), flush=True)
        return t

    print(f"\nWUP Grid Explorer (headless) · source {src_name}{' (offline)' if E['ctx'].offline else ''} · configuration "
          f"{cfg.id} ({args.config}) · workspace {ws.root}")
    if source == "synthetic":
        print("  NOTE: synthetic demo weather - not real data. Run where the AORC archive (or a local store) is reachable.")
    orig_progress_set = tasks._set

    def _set(tid, **kw):                                  # mirror task progress on the console
        orig_progress_set(tid, **kw)
        if "done" in kw and "total" in kw:
            _bar(kw["done"], kw["total"], kw.get("note", ""))
    tasks._set = _set

    if args.download_aorc:
        print("\n[1] AORC download into the local store")
        t = run_task(c.post("/api/data/aorc", json={"start": args.download_aorc[0], "end": args.download_aorc[1]}))
        if t and t.get("result"):
            r = t["result"]
            print(f"    downloaded {r['downloaded']}, already present {r['already_present']}, errors {r['n_errors']}; "
                  f"store {r['store_bytes'] / 1e9:.2f} GB ({r['store_percent']} % of the domain)")
    if args.ingest_ndvi or (args.full_record and not (Path(static_dir) / "FPI_Input_Static.nc").exists()):
        yrs = args.ingest_ndvi or f"2000-{min(d1.year, time.localtime().tm_year)}"
        a, _, b = yrs.partition("-")
        print(f"\n[2] NDVI ingest ({args.ndvi_product} via {args.ndvi_backend}, {a}-{b or a}) into {static_dir}")
        body = {"backend": args.ndvi_backend, "product": args.ndvi_product, "years": [int(a), int(b or a)], "folder": args.ndvi_folder}
        if args.ndvi_backend == "appeears":
            import getpass
            body["username"] = os.environ.get("EARTHDATA_USERNAME") or input("    Earthdata username: ")
            body["password"] = os.environ.get("EARTHDATA_PASSWORD") or getpass.getpass("    Earthdata password: ")
        run_task(c.post("/api/data/ndvi", json=body))
    period = args.compute or ([d0.isoformat(), d1.isoformat()] if args.full_record else None)
    if period:
        print(f"\n[3] Compute {period[0]} -> {period[1]}, all bricks (saved chunks are skipped)")
        bricks = [b["key"] for b in grid.bricks()]
        jid = jobs.create(f"record {period[0][:10]}→{period[1][:10]}", src_name, cfg, bricks, period[0], period[1])
        t0 = time.time()
        stop = threading.Event()

        def watch():
            while not stop.wait(15):
                j_ = jobs.get(jid) or {}
                el = time.time() - t0
                d_, n_ = j_.get("tasks_done", 0), max(j_.get("tasks_total", 1), 1)
                eta = el / d_ * (n_ - d_) if d_ else float("nan")
                _bar(d_, n_, f"{d_}/{n_} brick-chunks · {el / 3600:.1f} h elapsed · ~{eta / 3600:.1f} h left")
        threading.Thread(target=watch, daemon=True).start()
        try:
            jobs.run_now(jid)
        finally:
            stop.set()
        j = jobs.get(jid)
        print()
        print(f"    job #{jid}: {j['status']} - {j['tasks_done']}/{j['tasks_total']} brick-chunks in {time.time() - t0:.0f} s"
              + (f" - {j['error']}" if j.get("error") else ""))
        if j["status"] != "done":
            rc = 1
    if args.verify or args.full_record:
        mode = args.verify or "full"
        print(f"\n[4] Verify the saved record ({mode})")
        t = run_task(c.post("/api/data/verify", json={"config": cfg.as_dict(), "source": src_name, "mode": mode}))
        if t and t.get("result"):
            r = t["result"]
            print(f"    OVERALL {r['overall']} · completeness {r['completeness_percent']} % · {r['files_checked']} files opened")
            for k, v in r["checks"].items():
                print(f"      {k:<22} {v}")
            for p in r["problems"][:10]:
                print("      -", p)
            print(f"    report: {r['html']}")
            rc = rc or (0 if r["overall"] != "FAIL" else 2)
    if args.bundle:
        print(f"\n[5] Research bundle -> {args.bundle}")
        t = run_task(c.post("/api/bundle", json={"path": args.bundle, "parts": args.bundle_parts.split(",")}))
        if t and t.get("result"):
            r = t["result"]
            print(f"    {r['files']} files, {r['bytes'] / 1e9:.2f} GB zip ({r['uncompressed_bytes'] / 1e9:.2f} GB of files) -> {r['path']}")
    print()
    return rc


def run_fire_checks(ws_dir, source, static_dir, output, args) -> int:
    """Headless known-fire run: season jobs (skipping anything already saved), then a check of every fire."""
    from aorc_tools.explorer.chain import PRESETS, ChainConfig
    from aorc_tools.explorer.server import build

    cfg = PRESETS[args.config] if args.config in PRESETS else ChainConfig.from_dict(json.loads(args.config))
    app = build(ws_dir, points_csv=args.points, source=source, static_dir=static_dir, output_base=output, start_jobs=False)
    E, c = app.extensions["wup"], app.test_client()
    ids = [x.strip() for x in args.fires.split(",") if x.strip()]
    print(f"\nKnown-fire checks · source {source} · configuration {cfg.id} ({args.config})")
    if source == "synthetic":
        print("  NOTE: synthetic demo weather - the numbers will not mean anything for the real fires.")
    if not args.no_season:
        r = c.post("/api/fires/season-jobs", json={"config": cfg.as_dict(), "ids": ids}).get_json()
        print(f"  {len(r['jobs'])} season job(s) to run (already-saved chunks are skipped); not runnable: {', '.join(map(str, r['skipped'])) or 'none'}")
        for j in r["jobs"]:
            t0 = time.time()
            print(f"  - job #{j['id']} {j['name']}: {j['tasks_total']} brick-chunks, {j['packets_total']:,} packets ...", flush=True)
            E["jobs"].run_now(j["id"])
            jj = E["jobs"].get(j["id"])
            print(f"    {jj['status']} in {time.time() - t0:.0f} s" + (f" - {jj['error']}" if jj.get("error") else ""), flush=True)
    r = c.post("/api/fires/report", json={"config": cfg.as_dict(), "ids": ids}).get_json()
    s = r["summary"]
    print(f"\n  {s['checked']} fire(s) checked: {s['elevated']} elevated, {s['mixed']} mixed, {s['not_elevated']} not elevated, "
          f"{s['unranked']} without a saved season")
    for rep in r["reports"]:
        f = rep.get("fire", {})
        v = rep.get("verdict", {}).get("text") or rep.get("message", "")
        print(f"   · {f.get('name')} ({str(f.get('discovered'))[:10]}): {v}")
    ex = E["ws"].root / "exports"
    print(f"\n  Reports (HTML, CSV, JSON) are in {ex} - send the three wup_fire_checks_* files to check them.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
