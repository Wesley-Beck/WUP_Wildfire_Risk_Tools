"""
WUP Grid Explorer — one command (or one double-click) to run everything.

    python -m aorc_tools.explorer                 # opens the browser at http://127.0.0.1:8765/
    python -m aorc_tools.explorer --source synthetic --workspace D:\\fire\\ws
    python -m aorc_tools.explorer --fire-checks       # no browser: compute every known fire's season,
                                                      # check each fire, write CSV/HTML/JSON reports

The workspace folder keeps every result, job, saved selection, saved view and
export, so closing and reopening simply continues where you left off. When run
as the packaged WUP_Explorer.exe the workspace defaults to a WUP_Explorer_data
folder next to the .exe.
"""

from __future__ import annotations

import argparse
import json
import logging
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
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    base = base_dir()
    ws_dir = Path(args.workspace) if args.workspace else base / "WUP_Explorer_data"
    url = f"http://{args.host}:{args.port}/"

    if not args.fire_checks and not port_free(args.host, args.port):
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
    source = args.source
    if source == "auto":
        remembered = ws.settings.get("source")
        print("Checking the NOAA AORC archive on S3 ...", flush=True)
        if s3_reachable():
            source = "aorc"
        else:
            source = "synthetic"
            print("  AORC is not reachable from this computer right now - starting with SYNTHETIC demo weather.\n"
                  "  (Switch to the real archive from the source badge once you are online.)")
        if remembered == "synthetic" and source == "aorc":
            source = "aorc"
    ws.save_settings(source=source)

    static_dir = Path(args.static_dir) if args.static_dir else base / "data" / "static"
    output = Path(args.output) if args.output else base / "data" / "output"
    if args.fire_checks:
        return run_fire_checks(ws_dir, source, static_dir, output, args)
    app = build(ws_dir, points_csv=args.points, source=source, static_dir=static_dir, output_base=output,
                max_cells=args.max_cells)

    print("\n" + "=" * 64)
    print(" WUP Grid Explorer")
    print(f"   open:      {url}")
    print(f"   workspace: {ws.root}")
    print(f"   data:      {'NOAA AORC v1.1 (S3)' if source == 'aorc' else 'SYNTHETIC demo weather (not real data)'}")
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
