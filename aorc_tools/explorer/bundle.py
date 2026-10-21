"""
Research bundle: one zip that lets anyone reproduce, inspect, or extend the WUP fire-danger record.

    WUP_FDI_bundle_<date>/
        README.txt                  how to run it, what is inside, how to check it
        MANIFEST.json               every file with bytes + SHA-256, and the provenance of the record
        WUP_Explorer.exe            (when made from the packaged program on Windows)
        app/                        source snapshot: aorc_tools/, launchers, requirements, docs, scripts
        WUP_Explorer_data/          the workspace: catalog (workspace.db), settings, results/ (four FDIs),
                                    inputs/aorc (local AORC store, optional), inputs/static (NDVI + fuel models),
                                    inputs/datasets (offline climate-model copies), fires/, datasets/
                                    (definitions), climatology/, exports/ (reports)

Unzip anywhere and double-click WUP_Explorer.exe (Windows) or app/WUP_Explorer.bat / .command / wup_explorer.sh;
the workspace re-points its catalog to the new folder on first start (``Workspace._relocate``), and the
explorer starts offline from the bundled AORC store when the internet is not available.

NetCDF files are already zlib-compressed inside, so they are stored as-is (ZIP_STORED); text is deflated.
Zip64 is used automatically above 4 GB. ``check_bundle(zip)`` re-hashes every member against the manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from aorc_tools.explorer.workspace import now_iso

STORED_EXT = {".nc", ".nc4", ".zip", ".gz", ".zst", ".png", ".jpg", ".tif", ".tiff", ".hdf", ".h5", ".exe"}
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "cache", "bundles", "downloads", "node_modules"}
SKIP_SUFFIX = (".tmp", ".pyc", ".lock", "-wal", "-shm")
APP_TOP = ["README.md", "USAGE.md", "CLAUDE.md", "setup.py", "requirements.txt", "requirements-explorer.txt",
           "WUP_Explorer.bat", "WUP_Explorer.command", "wup_explorer.sh", "Run_Fire_Checks.bat", "Run_Full_Record.bat",
           "Run_Self_Test.bat", "requirements-tested.txt"]
APP_DIRS = ["aorc_tools", "docs", "packaging", "scripts", "tests"]
PARTS = {
    "app": "Program (source snapshot, launchers, docs; plus WUP_Explorer.exe when packaged)",
    "results": "Computed record: FWI (hourly + daily), FPI, ERC, BI NetCDF files",
    "inputs_aorc": "Local AORC input store (lets anyone recompute offline)",
    "ndvi": "NDVI composites, relative-greenness envelope and fuel models (FPI inputs)",
    "inputs_datasets": "Offline copies of climate-model datasets (WUP window), e.g. GLARM",
    "catalog": "Workspace catalog, settings, jobs, fire catalog and checks, dataset definitions, reports",
}
CITATIONS = [
    "Fall, G. et al. (2023) The Office of Water Prediction's Analysis of Record for Calibration, version 1.1. JAWRA. doi:10.1111/1752-1688.13143",
    "Van Wagner, C.E. (1987) Development and structure of the Canadian Forest Fire Weather Index System. Forestry Tech. Rep. 35.",
    "Natural Resources Canada, cffdrs-ng (FWI2025 hourly system), https://github.com/nrcan-cfs-fire/cffdrs-ng",
    "Burgan, R.E., Klaver, R.W., Klaver, J.M. (1998) Fuel models and fire potential from satellite and surface observations. IJWF 8(3).",
    "Bradshaw, L.S. et al. (1984) The 1978 National Fire-Danger Rating System: technical documentation. GTR INT-169.",
    "Didan, K. (2021) MODIS/Terra Vegetation Indices 16-Day L3 Global 250m SIN Grid V061 (MOD13Q1). NASA LP DAAC.",
    "Yu, G. et al. (2023) Performance of fire danger indices and their utility in predicting future wildfire danger over the "
    "conterminous United States. Earth's Future 11, e2023EF003823.",
]


def _skip(p: Path) -> bool:
    return any(part in SKIP_DIRS for part in p.parts) or p.name.endswith(SKIP_SUFFIX)


def _walk(root: Path):
    if not root.exists():
        return
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if not _skip(p):
                yield p


def app_root() -> Path | None:
    """Folder holding the source snapshot: the repository when run from source, or the copy packed into the exe."""
    if getattr(sys, "frozen", False):
        p = Path(getattr(sys, "_MEIPASS", "")) / "app_source"
        return p if p.exists() else None
    here = Path(__file__).resolve().parents[2]
    return here if (here / "aorc_tools").exists() else None


def git_commit(root: Path | None) -> str | None:
    if root is None:
        return None
    if (root / "APP_VERSION.txt").exists():
        return (root / "APP_VERSION.txt").read_text().strip() or None
    head = root / ".git" / "HEAD"
    try:
        h = head.read_text().strip()
        if h.startswith("ref:"):
            ref = root / ".git" / h.split(" ", 1)[1]
            if ref.exists():
                return ref.read_text().strip()
            packed = root / ".git" / "packed-refs"
            for line in packed.read_text().splitlines() if packed.exists() else []:
                if line.endswith(h.split(" ", 1)[1]):
                    return line.split()[0]
            return None
        return h
    except OSError:
        return None


def plan(ws, ctx, parts=None, results_filter=None) -> dict:
    """What would go into a bundle: (arcname, path) per part, with sizes. ``results_filter``: list of
    ``source/config_id`` to include (default: everything saved)."""
    parts = set(parts or PARTS)
    files: dict[str, list[tuple[str, Path]]] = {k: [] for k in PARTS}
    W = "WUP_Explorer_data"
    if "app" in parts:
        if getattr(sys, "frozen", False) and Path(sys.executable).exists():
            files["app"].append((Path(sys.executable).name, Path(sys.executable)))
        root = app_root()
        if root is not None:
            for name in APP_TOP:
                if (root / name).exists():
                    files["app"].append((f"app/{name}", root / name))
            for d in APP_DIRS:
                for p in _walk(root / d):
                    files["app"].append((f"app/{p.relative_to(root).as_posix()}", p))
    if "results" in parts:
        for p in _walk(ws.results_dir):
            rel = p.relative_to(ws.results_dir)
            if results_filter and len(rel.parts) >= 2 and f"{rel.parts[0]}/{rel.parts[1]}" not in results_filter:
                continue
            files["results"].append((f"{W}/results/{rel.as_posix()}", p))
    if "inputs_aorc" in parts:
        base = ws.root / "inputs" / "aorc"
        for p in _walk(base):
            files["inputs_aorc"].append((f"{W}/inputs/aorc/{p.relative_to(base).as_posix()}", p))
    if "inputs_datasets" in parts:
        base = ws.root / "inputs" / "datasets"
        for p in _walk(base):
            files["inputs_datasets"].append((f"{W}/inputs/datasets/{p.relative_to(base).as_posix()}", p))
    if "ndvi" in parts:
        sd = Path(ctx.static_dir)
        for p in _walk(sd):
            files["ndvi"].append((f"{W}/inputs/static/{p.relative_to(sd).as_posix()}", p))
    if "catalog" in parts:
        for name in ("settings.json",):
            if (ws.root / name).exists():
                files["catalog"].append((f"{W}/{name}", ws.root / name))
        for d in ("fires", "datasets", "climatology", "exports"):
            for p in _walk(ws.root / d):
                files["catalog"].append((f"{W}/{p.relative_to(ws.root).as_posix()}", p))
    sizes = {k: {"files": len(v), "bytes": int(sum(p.stat().st_size for _, p in v)), "label": PARTS[k]}
             for k, v in files.items() if k in parts}
    if "catalog" in parts:
        sizes["catalog"]["bytes"] += ws.db_path.stat().st_size if ws.db_path.exists() else 0
        sizes["catalog"]["files"] += 1
    return {"parts": sizes, "total_bytes": int(sum(s["bytes"] for s in sizes.values())), "files": files}


def provenance(ws, ctx, store, grid) -> dict:
    root = app_root()
    st = ws.settings
    configs = store.configs()
    out = {"created": now_iso(), "made_on": {"python": sys.version.split()[0], "platform": platform.platform(),
                                             "frozen_exe": bool(getattr(sys, "frozen", False))},
           "app_commit": git_commit(root), "domain": [st.get("domain_start"), st.get("domain_end")],
           "grid": {k: v for k, v in grid.summary().items() if k in ("n_cells", "step_deg", "counties", "zones", "bbox")},
           "configs": [{"source": c["source"], "config_id": c["config_id"], "label": c["label"], "config": c["config"],
                        "chunks": c["chunks"], "packets": c["packets"]} for c in configs],
           "aorc_input_store": ctx.inputs.inventory(st.get("domain_start"), st.get("domain_end")),
           "datasets": ctx.datasets.list(), "citations": CITATIONS}
    try:
        from aorc_tools.explorer.chain import PRESETS
        ndvi = ctx.ndvi_for(PRESETS["hourly-default"], "aorc-s3")
        out["ndvi"] = ndvi.describe() if ndvi is not None else {"available": False}
    except Exception as e:                                          # pragma: no cover - descriptive only
        out["ndvi"] = {"error": str(e)}
    vdir = ws.root / "exports"
    reps = sorted(vdir.glob("verify_*.json"), key=lambda p: p.stat().st_mtime) if vdir.exists() else []
    latest = {}
    for p in reps:
        try:
            r = json.loads(p.read_text())
            latest[f"{r['source']}/{r['config_id']}"] = {"overall": r["overall"], "mode": r["mode"], "created": r["created"],
                                                          "completeness_percent": r["checks"]["completeness"]["percent"],
                                                          "file": p.name}
        except Exception:
            continue
    out["verification"] = latest
    return out


README = """WUP fire-danger research bundle
==============================

Hourly fire-danger indices for Michigan's Western Upper Peninsula (Baraga, Gogebic, Houghton, Iron,
Keweenaw, Ontonagon + Isle Royale) on the NOAA AORC v1.1 800 m grid:
  FWI  Canadian Fire Weather Index: hourly FWI2025 (cffdrs-ng) and classic daily (12:00 LST)
  FPI  Fire Potential Index (Burgan et al. 1998), MODIS-era NDVI (2000 onward)
  ERC  Energy Release Component (derived NFDRS; report as percentiles)
  BI   Burning Index (derived NFDRS; report as percentiles)

Made {created} - {total} in {n} files.  Contents:
{parts}

RUN IT
  Windows:   double-click WUP_Explorer.exe (if present), or app\\WUP_Explorer.bat (installs Python packages once).
  macOS:     app/WUP_Explorer.command        Linux: app/wup_explorer.sh
  The explorer opens in your browser on the bundled workspace (WUP_Explorer_data). With no internet it runs
  from the bundled AORC store (offline mode). Data tab -> Verify re-checks the whole record.
  Headless: python -m aorc_tools.explorer --workspace WUP_Explorer_data --verify full

READ THE RESULTS WITHOUT THE APP
  WUP_Explorer_data/results/<source>/<config_id>/<year>/WUP_FDI_<year>_k<NNN>_<date>.nc
  NetCDF-4, one file per 144-hour chunk, (time, latitude, longitude); int16 with scale_factor, so
  xarray.open_dataset / netCDF4 / R ncdf4 / Panoply / QGIS decode the values automatically.
  config.json in each <config_id> folder records exactly how the indices were computed.

CHECK IT
  MANIFEST.json lists every file with its SHA-256 and the record's provenance (configurations, domain,
  input store coverage, NDVI products, the latest verification result, citations).
  python -m aorc_tools.explorer.bundle check <this zip>   re-hashes every file against the manifest.

LARGE ZIP ON WINDOWS
  If the built-in "Extract all" refuses a very large bundle, use 7-Zip (https://www.7-zip.org).
"""


def _human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024


def make(ws, ctx, store, grid, out_path: Path, parts=None, results_filter=None, progress=None) -> dict:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pl = plan(ws, ctx, parts, results_filter)
    stamp = datetime.now().strftime("%Y%m%d")
    top = f"WUP_FDI_bundle_{stamp}"
    items = [(a, p) for k in pl["files"] for a, p in pl["files"][k]]
    total = pl["total_bytes"]
    done = 0
    manifest_files = []
    tmp = out_path.with_name(out_path.name + ".tmp")
    from aorc_tools.explorer.runtime import disk_free_gb, fsync_file, replace_file
    need = total * 1.02 / 1024 ** 3 + 0.5
    if disk_free_gb(out_path.parent) < need:
        raise RuntimeError(f"not enough free space for the bundle at {out_path.parent}: needs about {need:.1f} GB, "
                           f"{disk_free_gb(out_path.parent):.1f} GB free")
    try:
        with tempfile.TemporaryDirectory() as td:
            db_copy = Path(td) / "workspace.db"
            if parts is None or "catalog" in parts:
                src_db = sqlite3.connect(ws.db_path)
                dst_db = sqlite3.connect(db_copy)
                with dst_db:
                    src_db.backup(dst_db)                     # consistent snapshot even while jobs write
                src_db.close()
                dst_db.close()
                items.append(("WUP_Explorer_data/workspace.db", db_copy))
            with zipfile.ZipFile(tmp, "w", allowZip64=True) as z:
                skipped = []
                for arc, p in items:
                    comp = zipfile.ZIP_STORED if p.suffix.lower() in STORED_EXT else zipfile.ZIP_DEFLATED
                    h = hashlib.sha256()
                    try:
                        st = p.stat()
                    except FileNotFoundError:               # removed meanwhile (e.g. a job replaced it): leave out
                        skipped.append(arc)
                        continue
                    zi = zipfile.ZipInfo(f"{top}/{arc}", date_time=datetime.fromtimestamp(st.st_mtime).timetuple()[:6])
                    zi.compress_type = comp
                    zi.external_attr = (0o755 if p.suffix in (".sh", ".command", ".exe") else 0o644) << 16
                    size = st.st_size
                    with open(p, "rb") as f, z.open(zi, "w", force_zip64=size > 2 ** 31) as w:
                        while True:
                            b = f.read(4 * 1024 * 1024)
                            if not b:
                                break
                            h.update(b)
                            w.write(b)
                    manifest_files.append({"path": arc, "bytes": size, "sha256": h.hexdigest()})
                    done += size
                    if progress:
                        progress(done, max(total, 1), arc)
                prov = provenance(ws, ctx, store, grid)
                manifest = {"bundle": top, "format": 1, "provenance": prov, "skipped_while_bundling": skipped,
                            "parts": {k: {kk: vv for kk, vv in v.items()} for k, v in pl["parts"].items()},
                            "files": manifest_files}
                z.writestr(f"{top}/MANIFEST.json", json.dumps(manifest, indent=1, default=str), compress_type=zipfile.ZIP_DEFLATED)
                parts_txt = "\n".join(f"  - {v['label']}: {_human(v['bytes'])} ({v['files']} files)" for v in pl["parts"].values())
                z.writestr(f"{top}/README.txt", README.format(created=prov["created"], total=_human(sum(f["bytes"] for f in manifest_files)),
                                                             n=len(manifest_files), parts=parts_txt), compress_type=zipfile.ZIP_DEFLATED)
                if any(a.startswith("app/") for a, _ in items):
                    z.writestr(f"{top}/app/APP_VERSION.txt", (prov.get("app_commit") or "unknown") + "\n")
        fsync_file(tmp)
        replace_file(tmp, out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)             # never leave a many-GB half bundle behind
        raise
    return {"path": str(out_path), "bytes": out_path.stat().st_size, "files": len(manifest_files),
            "uncompressed_bytes": int(sum(f["bytes"] for f in manifest_files)), "parts": pl["parts"], "top": top}


def check(zip_path: Path, progress=None) -> dict:
    """Re-hash every member against MANIFEST.json."""
    bad, missing, ok = [], [], 0
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        man_name = next(n for n in names if n.endswith("/MANIFEST.json"))
        top = man_name.rsplit("/", 1)[0]
        man = json.loads(z.read(man_name))
        total = sum(f["bytes"] for f in man["files"]) or 1
        done = 0
        have = set(names)
        for f in man["files"]:
            n = f"{top}/{f['path']}"
            if n not in have:
                missing.append(f["path"])
                continue
            h = hashlib.sha256()
            with z.open(n) as r:
                while True:
                    b = r.read(4 * 1024 * 1024)
                    if not b:
                        break
                    h.update(b)
            if h.hexdigest() != f["sha256"]:
                bad.append(f["path"])
            else:
                ok += 1
            done += f["bytes"]
            if progress:
                progress(done, total, f["path"])
    return {"ok": ok, "bad": bad, "missing": missing, "status": "PASS" if not bad and not missing else "FAIL",
            "provenance": man.get("provenance", {})}


def extract(zip_path: Path, dest: Path) -> Path:
    """Unzip a bundle; returns the bundle folder (its workspace relocates itself on first open)."""
    dest = Path(dest)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
        top = next(n for n in z.namelist() if n.endswith("/MANIFEST.json")).rsplit("/", 1)[0]
    root = dest / top
    for p in list(root.rglob("*.sh")) + list(root.rglob("*.command")):
        p.chmod(0o755)
    return root


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m aorc_tools.explorer.bundle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="re-hash every file in a bundle against its manifest")
    c.add_argument("zip")
    e = sub.add_parser("extract", help="unzip a bundle")
    e.add_argument("zip")
    e.add_argument("dest")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        r = check(Path(a.zip))
        print(f"{r['status']}: {r['ok']} files verified, {len(r['bad'])} changed, {len(r['missing'])} missing")
        for p in (r["bad"] + r["missing"])[:50]:
            print("  ", p)
        return 0 if r["status"] == "PASS" else 1
    if a.cmd == "extract":
        print(extract(Path(a.zip), Path(a.dest)))
        return 0


if __name__ == "__main__":
    sys.exit(_main())
