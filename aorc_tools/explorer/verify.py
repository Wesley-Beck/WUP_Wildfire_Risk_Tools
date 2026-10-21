"""
Verify the saved record before anyone builds on it (CLAUDE.md pipeline step 5).

For one source + configuration, over the source's whole domain (AORC: 1979-02-01 -> domain end):

  completeness   every (year, 144-h chunk, brick) is saved, with every hour and every study cell
  continuity     moisture codes carry across chunks: each chunk's start state equals the previous chunk's
                 end state in the files, and only the very first chunk of the record is a cold start
  values         physical ranges; ERC/BI not clipped at 100; no hour where one brick-wide field is uniform;
                 FWI_daily changes only at 12:00 LST; FPI empty before MODIS (2000-02-18) unless non-MODIS
                 NDVI was opted in; FWI NaN only where the weather tier is 9
  tiers          share of the record from each weather tier

``mode="quick"``: catalog checks for every chunk + file checks on the first file of each year.
``mode="full"``: file checks on every file. The report has PASS / WARN / FAIL per check and per year.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.store import FILL_I2, FILL_U1, NC_LOCK, PACK, SAVED, T_EPOCH
from aorc_tools.explorer.workspace import now_iso

RANGES = {"FWI": (0.0, 250.0), "FWI_daily": (0.0, 250.0), "FPI": (0.0, 100.0), "ERC": (0.0, 400.0), "BI": (0.0, 1000.0)}
MODIS_START = pd.Timestamp("2000-02-18")
CONT_KEYS = ("mc_ffmc", "mc_dmc", "mc_dc", "d_dc", "fm1000")
STATE_TOL = 1e-4
UNIFORM_MIN_FWI = 1.0     # below this, FWI packed to 0.1 can legitimately be one value everywhere (winter)


def _status(ok, warn=False):
    return "PASS" if ok else ("WARN" if warn else "FAIL")


def _origin_fn(src):
    from aorc_tools.explorer.sources import AORCSource
    if isinstance(src, AORCSource):                       # no network needed for the layout of the record
        return lambda y: datetime(1979, 2, 1) if y == 1979 else datetime(y, 1, 1)
    return src.origin_of_year


def _file_checks(store, grid, path, cfg, acc):
    """Value checks on one chunk file; adds counts into ``acc``. Returns the file's chain state (study cells), or
    None when the file cannot be read (recorded in ``acc['unreadable']``)."""
    try:
        return _file_checks_inner(store, grid, path, cfg, acc)
    except Exception as e:                                  # damaged file: report it, keep checking the rest
        acc["unreadable"].append({"path": str(path), "error": f"{type(e).__name__}: {e}"[:300]})
        return None


def _file_checks_inner(store, grid, path, cfg, acc):
    import netCDF4
    from aorc_tools.explorer.chain import STATE_KEYS
    with NC_LOCK:
        with netCDF4.Dataset(path) as ds:
            t = pd.to_datetime(np.asarray(ds["time"][:], dtype=np.int64), unit="h", origin=T_EPOCH)
            data = {}
            for v in SAVED:
                if v in ds.variables:
                    ds[v].set_auto_maskandscale(False)
                    data[v] = np.asarray(ds[v][:])[:, store.y, store.x]
            ds["wx_tier"].set_auto_maskandscale(False)
            wx = np.asarray(ds["wx_tier"][:])[:, store.y, store.x]
            g = ds.groups.get("chain_state")
            state = {}
            if g is not None:
                for w in ("start", "end"):
                    for k in STATE_KEYS:
                        if f"{w}_{k}" in g.variables:
                            g[f"{w}_{k}"].set_auto_maskandscale(False)
                            state[f"{w}_{k}"] = np.asarray(g[f"{w}_{k}"][:])[store.y, store.x]
    acc["files"] += 1
    computed = wx != FILL_U1
    off = grid.points.std_offset_h.values
    for v, q in data.items():
        lo, hi = RANGES[v]
        a = np.where(q == FILL_I2, np.nan, q.astype(np.float64) * PACK[v][0])
        fin = np.isfinite(a)
        acc["out_of_range"][v] += int(np.sum(fin & ((a < lo - 1e-6) | (a > hi + 1e-6))))
        if v in ("ERC", "BI") and fin.any():
            at100 = np.mean(a[fin] == 100.0)
            if np.nanmax(a) == 100.0 and at100 > 0.01:
                acc["clipped"] += int(np.sum(a == 100.0))
        if v == "FWI":
            for h in range(a.shape[0]):
                row = a[h][fin[h] & computed[h]]
                if row.size > 1000 and np.std(row) == 0 and np.mean(row) >= UNIFORM_MIN_FWI:
                    acc["uniform_hours"] += 1
            acc["nan_not_tier9"] += int(np.sum(computed & ~fin & (wx != 9)))
        if v == "FWI_daily" and a.shape[0] > 1:
            changed = np.abs(np.diff(np.nan_to_num(a, nan=-1.0), axis=0)) > 0
            lst_hour = (t.hour.values[1:, None] + off[None]) % 24
            acc["daily_off_noon"] += int(np.sum(changed & (lst_hour != 12) & computed[1:] & computed[:-1]))
        if v == "FPI" and not cfg.allow_ndvi_climatology:
            acc["fpi_pre_modis"] += int(np.sum((t < MODIS_START)[:, None] & fin))
    return state


def _is_next(prev_key, key):
    if prev_key[0] == key[0]:
        return key[1] == prev_key[1] + 1
    return key[0] == prev_key[0] + 1 and key[1] == 0      # first chunk of a year follows the last of the previous


def verify_record(store, grid, src, cfg, settings, mode="quick", progress=None, years=None, bricks=None) -> dict:
    source = src.name
    d0, d1 = (pd.Timestamp(x) for x in src.domain(settings))
    origin = _origin_fn(src)
    bricks = {b["key"]: int(b["n_cells"]) for b in grid.bricks() if not bricks or b["key"] in bricks}
    with store.ws.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM chunks WHERE source=? AND config_id=?", (source, cfg.id)).fetchall()]
    have = {(r["year"], r["k"], r["brick"]): r for r in rows}
    all_years = list(range(d0.year, d1.year + 1))
    years = [int(y) for y in (years or all_years) if int(y) in all_years]
    rep = {"source": source, "config_id": cfg.id, "config": cfg.as_dict(), "mode": mode, "created": now_iso(),
           "domain": [d0.isoformat(), d1.isoformat()], "bricks": sorted(bricks), "years": {}, "checks": {}, "problems": []}
    acc = {"files": 0, "unreadable": [], "out_of_range": {v: 0 for v in SAVED}, "clipped": 0, "uniform_hours": 0,
           "daily_off_noon": 0, "fpi_pre_modis": 0, "nan_not_tier9": 0}
    expected_total = present_total = short_total = cells_short = 0
    cov = {"packets": 0, "FWI": 0, "ERC": 0, "BI": 0, "fpi_season_packets": 0, "FPI_season": 0}
    cont = {"continuous": 0, "cold_start_at_domain": 0, "spinup_after_gap": 0}
    pairs = mismatch = 0
    tiers = {}
    prev_end = prev_key = None
    for yi, y in enumerate(years):
        y0, y1 = max(pd.Timestamp(origin(y)), d0), min(pd.Timestamp(y, 12, 31, 23), d1)
        if y1 < y0:
            continue
        chunks = grid.time_chunks(y0.to_pydatetime(), y1.to_pydatetime(), origin)
        n_missing, missing, short = 0, [], 0
        files = []
        for ch in chunks:
            need_h = int((min(pd.Timestamp(ch["end"]), d1) - max(pd.Timestamp(ch["start"]), d0)) / pd.Timedelta("1h")) + 1
            paths = set()
            for b, n_cells in bricks.items():
                expected_total += 1
                r = have.get((ch["year"], ch["k"], b))
                if r is None or not r.get("path") or not Path(r["path"]).exists():
                    n_missing += 1
                    if len(missing) < 40:
                        missing.append(f"k{ch['k']:03d} {b}")
                    continue
                present_total += 1
                paths.add(r["path"])
                if int(r["n_hours"]) < need_h:
                    short += 1
                    short_total += 1
                    rep["problems"].append(f"{y} k{ch['k']:03d} {b}: {r['n_hours']} of {need_h} hours")
                if int(r["n_cells"]) < n_cells:
                    cells_short += 1
                    rep["problems"].append(f"{y} k{ch['k']:03d} {b}: {r['n_cells']} of {n_cells} cells")
                cc = str(r["continuity"] or "")
                if cc == "continuous":
                    cont["continuous"] += 1
                elif pd.Timestamp(r["t_start"]) <= d0:
                    cont["cold_start_at_domain"] += 1
                else:
                    cont["spinup_after_gap"] += 1
                    rep["problems"].append(f"{y} k{ch['k']:03d} {b}: {cc} (computed before the previous chunk existed)")
                for t_, n_ in json.loads(r["wx_tiers"] or "{}").items():
                    tiers[t_] = tiers.get(t_, 0) + int(n_)
                npk = int(r["n_hours"]) * int(r["n_cells"])
                cov["packets"] += npk
                cov["FWI"] += int(r["valid_fwi"] or 0)
                cov["ERC"] += int(r["valid_erc"] or 0)
                cov["BI"] += int(r["valid_bi"] or 0)
                ts_ = pd.Timestamp(r["t_start"])
                if ts_ >= MODIS_START and 4 <= ts_.month <= 10:              # fire season, MODIS era
                    cov["fpi_season_packets"] += npk
                    cov["FPI_season"] += int(r["valid_fpi"] or 0)
            files.append(((ch["year"], ch["k"]), sorted(paths)[0] if paths else None))
        todo = files if mode == "full" else files[:1]
        for key, p in todo:
            if p is None:
                prev_end = prev_key = None
                continue
            st = _file_checks(store, grid, p, cfg, acc)
            if st is None:
                prev_end = prev_key = None
                continue
            if prev_end is not None and _is_next(prev_key, key):
                for k in CONT_KEYS:
                    a, b = prev_end.get(f"end_{k}"), st.get(f"start_{k}")
                    if a is None or b is None:
                        continue
                    both = np.isfinite(a) & np.isfinite(b)
                    if both.any():
                        pairs += 1
                        dmax = float(np.max(np.abs(a[both].astype(np.float64) - b[both])))
                        if dmax > STATE_TOL * max(1.0, float(np.max(np.abs(a[both])))):
                            mismatch += 1
                            rep["problems"].append(f"{key[0]} k{key[1]:03d}: start {k} differs from previous end (max {dmax:.4g})")
            prev_end, prev_key = st, key
        if mode != "full":
            prev_end = prev_key = None
        n_exp = len(chunks) * len(bricks)
        rep["years"][y] = {"expected": n_exp, "present": n_exp - n_missing, "n_missing": n_missing, "missing": missing,
                           "short_chunks": short, "status": _status(n_missing == 0 and short == 0)}
        if progress:
            progress(yi + 1, len(years), f"{y}: {n_exp - n_missing}/{n_exp} brick-chunks")
    tot = sum(tiers.values()) or 1
    rep["checks"] = {
        "completeness": {"status": _status(present_total == expected_total and short_total == 0 and cells_short == 0),
                         "present": present_total, "expected": expected_total,
                         "percent": round(100.0 * present_total / max(expected_total, 1), 3),
                         "short_hours": short_total, "short_cells": cells_short},
        "continuity": {"status": _status(cont["spinup_after_gap"] == 0 and mismatch == 0), **cont,
                       "state_pairs_checked": pairs, "state_mismatches": mismatch},
        "ranges": {"status": _status(sum(acc["out_of_range"].values()) == 0), "out_of_range_packets": acc["out_of_range"]},
        "erc_bi_not_clipped": {"status": _status(acc["clipped"] == 0), "packets_at_100": acc["clipped"]},
        "uniform_fields": {"status": _status(acc["uniform_hours"] == 0, warn=True), "hours": acc["uniform_hours"]},
        "fwi_daily_noon_only": {"status": _status(acc["daily_off_noon"] == 0), "changes_off_noon": acc["daily_off_noon"]},
        "fpi_modis_era": {"status": _status(acc["fpi_pre_modis"] == 0), "values_before_2000_02_18": acc["fpi_pre_modis"]},
        "nan_only_tier9": {"status": _status(acc["nan_not_tier9"] == 0, warn=True), "packets": acc["nan_not_tier9"]},
        "weather_tiers": {"status": "INFO", "percent": {k: round(100.0 * v / tot, 4) for k, v in sorted(tiers.items())}},
    }
    pc = {k: round(100.0 * cov[k] / max(cov["packets"], 1), 4) for k in ("FWI", "ERC", "BI")}
    worst = min(pc.values()) if cov["packets"] else 100.0
    rep["checks"]["index_coverage"] = {"status": "PASS" if worst >= 99.9 else ("WARN" if worst >= 99.0 else "FAIL"),
                                       "percent_valid": pc, "packets": cov["packets"]}
    if cov["fpi_season_packets"]:
        fp = round(100.0 * cov["FPI_season"] / cov["fpi_season_packets"], 3)
        rep["checks"]["fpi_season_coverage"] = {
            "status": "PASS" if fp >= 95.0 else "WARN", "percent_valid_apr_oct_modis_era": fp,
            "note": "FPI needs NDVI; winter gaps under snow are expected, so only April-October is checked"}
    rep["checks"]["readable_files"] = {"status": _status(not acc["unreadable"]), "unreadable": len(acc["unreadable"]),
                                       "files": [u["path"] for u in acc["unreadable"][:50]]}
    for u in acc["unreadable"][:50]:
        rep["problems"].append(f"unreadable file {Path(u['path']).name}: {u['error']}")
    missing_files = [r["path"] for r in rows if r.get("path") and not Path(r["path"]).exists()]
    rep["checks"]["catalog_files_exist"] = {"status": _status(not missing_files), "rows_without_file": len(missing_files)}
    rep["unreadable_files"] = [u["path"] for u in acc["unreadable"]]
    rep["files_checked"] = acc["files"]
    st = [c["status"] for c in rep["checks"].values()]
    rep["overall"] = "FAIL" if "FAIL" in st else ("WARN" if "WARN" in st else "PASS")
    rep["problems"] = rep["problems"][:500]
    return rep


def save_report(ws, rep) -> dict:
    """Write JSON + HTML next to the other exports; return their paths."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"verify_{rep['source']}_{rep['config_id']}_{rep['mode']}_{stamp}"
    out = {}
    for ext, text in (("json", json.dumps(rep, indent=1, default=str)), ("html", html_report(rep))):
        p = ws.root / "exports" / f"{base}.{ext}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        out[ext] = str(p)
    return out


def html_report(rep) -> str:
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
    color = {"PASS": "#1d7a4f", "WARN": "#a8661a", "FAIL": "#b8452d", "INFO": "#555"}
    rows = "".join(f"<tr><td>{esc(k)}</td><td style='color:{color.get(v['status'], '#333')};font-weight:700'>{esc(v['status'])}</td>"
                   f"<td><code>{esc(json.dumps({a: b for a, b in v.items() if a != 'status'}))}</code></td></tr>"
                   for k, v in rep["checks"].items())
    yrs = "".join(f"<tr><td>{y}</td><td style='color:{color[v['status']]};font-weight:700'>{v['status']}</td>"
                  f"<td>{v['present']}/{v['expected']}</td><td>{esc(', '.join(v['missing'][:8]))}{' …' if v['n_missing'] > 8 else ''}</td></tr>"
                  for y, v in rep["years"].items())
    probs = "".join(f"<li>{esc(p)}</li>" for p in rep["problems"][:200])
    return ("<!doctype html><html><head><meta charset='utf-8'><title>WUP record verification</title><style>"
            "body{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1100px;color:#222;background:#fff}"
            "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;"
            "vertical-align:top}code{font-size:12px;white-space:pre-wrap}</style></head><body>"
            f"<h1>Record verification: <span style='color:{color[rep['overall']]}'>{rep['overall']}</span></h1>"
            f"<p>source <b>{esc(rep['source'])}</b> · configuration <b>{esc(rep['config_id'])}</b> · {esc(rep['mode'])} mode · "
            f"domain {esc(rep['domain'][0])} → {esc(rep['domain'][1])} · {rep['files_checked']} files opened · {esc(rep['created'])}</p>"
            "<h2>Checks</h2><table><tr><th>Check</th><th>Status</th><th>Details</th></tr>" + rows + "</table>"
            "<h2>Years</h2><table><tr><th>Year</th><th>Status</th><th>Brick-chunks</th><th>Missing (first)</th></tr>" + yrs + "</table>"
            + (f"<h2>Problems</h2><ul>{probs}</ul>" if probs else "") + "</body></html>")


def repair(store, rep) -> dict:
    """Act on a verification report: quarantine unreadable files and drop catalog rows whose file is gone, so the
    next calculation job recomputes exactly those chunks (and, through the continuity check, any later chunk that no
    longer continues from them)."""
    quarantined, dropped = 0, 0
    for path in rep.get("unreadable_files", []):
        dropped += store.quarantine_file(path, "verification: unreadable")
        quarantined += 1
    with store.ws.conn() as c:
        rows = c.execute("SELECT rowid, path FROM chunks WHERE source=? AND config_id=?",
                         (rep["source"], rep["config_id"])).fetchall()
        gone = [r["rowid"] for r in rows if not r["path"] or not Path(r["path"]).exists()]
        for rid in gone:
            c.execute("DELETE FROM chunks WHERE rowid=?", (rid,))
    return {"quarantined_files": quarantined, "catalog_rows_dropped": dropped + len(gone),
            "next": "run the calculation again (Calculate & save / --full-record); it recomputes what is missing"}
