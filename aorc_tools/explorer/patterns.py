"""
Patterns in the saved record, region-wide (Patterns tab, ``--patterns``).

* **Season windows** — when the spring and the fall high-danger windows open and close each year, and how long they
  last; per cell, then summarised for the mainland WUP, each county, Isle Royale and each brick. Meant for planning
  seasonal staffing and funding dates: the summary gives, per area, the historical envelope (10th percentile of start
  dates to 90th percentile of end dates) for the whole record and for the last 20 years, and the trend of each date.
  A window is the run of days whose 15-day smoothed daily maximum is at or above the regional ``season_pct``
  percentile; spring = Mar 1 - Jul 15, fall = Jul 16 - Nov 30 (local standard days, 365-day calendar).
* **Groupings** — on every day, the connected areas (8-neighbour) of cells whose daily maximum is at or above the
  regional ``cluster_pct`` percentile: how many there are, how large (km2), the largest, and how that changes.
* **Daily vs hourly FWI** — the classic daily FWI (12:00 LST observation) against the hourly system (FWI2025's
  daily maximum, and its value at 12:00 LST): correlation, rank agreement, agreement on high-danger days (each
  system's own 90th percentile), the afternoon peak the noon value misses, the hour of the peak, by cell, month and
  year. Cells where the daily value tracks the hourly one well are marked "daily adequate".

Every threshold is a percentile of all mainland cells' daily values, March-November, pooled over the reference
years (one regional scale). Isle Royale is reported as its own area.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.lines import (ISLE_ROYALE_LAT, MIN_SEASON_FRACTION, _doy_365, _fmt_doy, _hist_percentiles,
                                       _json_default, _quiet, _r3, _smooth, _swap_dir, _window, cell_area_km2,
                                       mk_hamed_rao, read_daily_vars, theil_sen)
from aorc_tools.explorer.workspace import now_iso

logger = logging.getLogger(__name__)

INDICES = ["FWI", "FWI_daily", "ERC", "BI", "FPI"]
SPRING_W = ((3, 1), (7, 15))
FALL_W = ((7, 16), (11, 30))
SIZE_BINS = [0, 5, 25, 100, 500, 2000, np.inf]          # km2
SIZE_LABELS = ["<5", "5-25", "25-100", "100-500", "500-2000", ">2000"]
LARGE_KM2 = 100.0
RECENT_YEARS = 20


def _vname(v):
    return "FWI_daily_noon" if v == "FWI_daily" else f"{v}_daily_max"


def _unpack(raw, scale):
    from aorc_tools.explorer.store import FILL_I2
    return np.where(raw == FILL_I2, np.nan, raw.astype(np.float32) * np.float32(scale))


def _q(a, q):
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    return float(np.percentile(a, q)) if a.size else np.nan


def _trend(years, y):
    sl, S, n, p = theil_sen(np.asarray(years, float), np.asarray(y, float)[:, None])
    phr = mk_hamed_rao(years, y)
    return {"per_decade": None if not np.isfinite(sl[0]) else round(float(sl[0] * 10), 3),
            "tau": None if n[0] < 2 else round(float(S[0] / (n[0] * (n[0] - 1) / 2)), 3),
            "p": None if not np.isfinite(p[0]) else round(float(p[0]), 4), "p_hr": None if phr is None else round(phr, 4),
            "n_years": int(n[0])}


def season_windows(Z, year, T):
    """Per cell: (start, end, days) of the spring and fall windows where the smoothed series Z >= T (day of year on a
    365-day calendar; NaN start/end and 0 days when there is no such day)."""
    out = {}
    for name, (a, b) in (("spring", SPRING_W), ("fall", FALL_W)):
        lo, hi = _window(year, a, b)
        sub = Z[lo:hi]
        fin = np.isfinite(sub)
        hit = np.where(fin, sub >= np.float32(T), False)
        anyh = hit.any(axis=0)
        first = hit.argmax(axis=0) + lo
        last = hi - 1 - hit[::-1].argmax(axis=0)
        days = hit.sum(axis=0).astype(float)
        if Z.shape[0] == 366:
            first = np.where(first >= 59, first - 1, first)
            last = np.where(last >= 59, last - 1, last)
        cover = fin.mean(axis=0) >= MIN_SEASON_FRACTION
        out[f"{name}_start"] = np.where(anyh & cover, first, np.nan)
        out[f"{name}_end"] = np.where(anyh & cover, last, np.nan)
        out[f"{name}_days"] = np.where(cover, days, np.nan)
    return out


class PatternAnalysis:
    def __init__(self, ws, grid, store):
        self.ws, self.grid, self.store = ws, grid, store
        self.root = ws.root / "patterns"
        pts = grid.points
        self.area_km2 = cell_area_km2(grid)
        self.mainland = pts.latitude.values < ISLE_ROYALE_LAT
        areas = {"WUP mainland": np.where(self.mainland)[0]}
        county = np.where(self.mainland, pts.county.values.astype(str), "Isle Royale")
        for c in sorted(set(county)):
            areas[c if c == "Isle Royale" else f"{c} County"] = np.where(county == c)[0]
        for (ci, cj), g in pts.groupby(["ci", "cj"]):
            areas[f"brick {ci}_{cj}"] = grid.rows_for(g.point_id.values)
        self.areas = areas
        self.ny, self.nx = store.ny, store.nx

    def analysis_id(self, source, cfg_id):
        return f"{source}__{cfg_id}"

    def years_saved(self, source, cfg_id):
        with self.ws.conn() as c:
            return [int(r[0]) for r in c.execute("SELECT DISTINCT year FROM chunks WHERE source=? AND config_id=? ORDER BY year",
                                                 (source, cfg_id)).fetchall()]

    # ------------------------------------------------------------------------------------------------ run
    def run(self, source, cfg_id, indices=None, years=None, ref_years=None, season_pct=80, cluster_pct=95,
            progress=None) -> dict:
        from aorc_tools.explorer.store import FILL_I2, PACK
        progress = progress or (lambda done, total, note="": None)
        indices = [v for v in (indices or INDICES) if v in INDICES]
        saved = self.years_saved(source, cfg_id)
        years = [y for y in (years or saved) if y in saved]
        if not years:
            raise ValueError("nothing saved for this source and configuration yet: compute the record first")
        ref_years = [y for y in (ref_years or years) if y in years]
        if not ref_years:
            raise ValueError("none of the reference years is saved")
        if not (50 <= float(season_pct) <= 99 and 50 <= float(cluster_pct) <= 99.9):
            raise ValueError("percentiles must be 50-99")
        steps = len(years) * (1 + 2 * len(indices)) + (len(years) * 2 if "FWI" in indices else 0) + 2
        done = 0
        paths = {}
        for y in years:
            progress(done, steps, f"daily maxima {y}")
            paths[y] = self.store.daily_year(source, cfg_id, y)
            done += 1
        aid = self.analysis_id(source, cfg_id)
        out_dir, tmp_dir = self.root / aid, self.root / (aid + ".tmp")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True)
        summary = {"id": aid, "source": source, "config_id": cfg_id, "created": now_iso(), "years": years,
                   "reference_years": [min(ref_years), max(ref_years)], "season_pct": float(season_pct),
                   "cluster_pct": float(cluster_pct), "areas": {k: int(len(v)) for k, v in self.areas.items()},
                   "definitions": {
                       "season_window": f"days whose 15-day smoothed daily maximum is >= the regional {season_pct:g}th "
                                        "percentile; spring Mar 1-Jul 15, fall Jul 16-Nov 30; per cell, then the median "
                                        "(and 10th-90th percentile) over an area's cells",
                       "envelope": "10th percentile of window starts to 90th percentile of window ends over all the "
                                   "area's cell-years (whole record, and the last 20 years)",
                       "grouping": f"8-connected cells with a daily maximum >= the regional {cluster_pct:g}th percentile",
                       "daily_vs_hourly": "classic daily FWI (12:00 LST) vs the hourly system's daily maximum and its "
                                          "12:00 LST value; high days = each system's own regional 90th percentile; "
                                          "'daily adequate' = rank correlation >= 0.9 and kappa >= 0.6"},
                   "indices": {}}
        season_rows, cluster_rows, cluster_daily = [], [], []
        cell_maps = {}
        mainland_idx = np.where(self.mainland)[0]
        for v in indices:
            scale = PACK[v][0]
            name = _vname(v)
            # thresholds (exact, mainland cells, Mar-Nov)
            counts = np.zeros(65536, dtype=np.int64)
            for y in ref_years:
                if paths.get(y) is None:
                    continue
                raw = read_daily_vars(self.store, paths[y], [name]).get(name)
                if raw is None:
                    continue
                s0, s1 = _window(y, (3, 1), (11, 30))
                a = raw[s0:s1][:, mainland_idx].ravel()
                a = a[a != FILL_I2].astype(np.int64) + 32768
                if a.size:
                    counts += np.bincount(a, minlength=65536)
            thr = _hist_percentiles(counts, scale, (float(season_pct), float(cluster_pct)))
            T_s, T_c = thr[float(season_pct)], thr[float(cluster_pct)]
            if T_s is None:
                summary["indices"][v] = {"skipped": "no saved values"}
                done += 2 * len(years)
                continue
            # ---- season windows + groupings, one pass over the years
            ny_ = len(years)
            W = {k: np.full((ny_, self.grid.n_cells), np.nan, dtype=np.float32)
                 for k in ("spring_start", "spring_end", "spring_days", "fall_start", "fall_end", "fall_days")}
            big_days = np.zeros(self.grid.n_cells)
            high_days = np.zeros(self.grid.n_cells)
            size_hist = np.zeros(len(SIZE_LABELS), dtype=np.int64)
            yrs_ok = []
            for yi, y in enumerate(years):
                progress(done, steps, f"{v}: season windows and groupings {y}")
                done += 2
                if paths.get(y) is None:
                    continue
                raw = read_daily_vars(self.store, paths[y], [name]).get(name)
                if raw is None:
                    continue
                X = _unpack(raw, scale)
                del raw
                Z = _smooth(X)
                win = season_windows(Z, y, T_s)
                for k in W:
                    W[k][yi] = win[k]
                yrs_ok.append(y)
                cr = self._clusters(X, y, T_c)
                if cr is None:
                    continue
                daily, sizes, big, high = cr
                big_days += big
                high_days += high
                size_hist += np.histogram(sizes, bins=SIZE_BINS)[0]
                for d in daily:
                    cluster_daily.append({"index": v, **d})
                dd = pd.DataFrame(daily) if daily else pd.DataFrame(columns=["n_groups", "mean_km2", "max_km2", "high_km2"])
                on = dd[dd["n_groups"] > 0] if len(dd) else dd
                cluster_rows.append({"index": v, "year": y, "days_with_groups": int(len(on)),
                                     "mean_groups_per_day": _r3(on["n_groups"].mean()) if len(on) else 0.0,
                                     "mean_group_km2": _r3(on["mean_km2"].mean()) if len(on) else None,
                                     "mean_largest_km2": _r3(on["max_km2"].mean()) if len(on) else None,
                                     "largest_km2": _r3(on["max_km2"].max()) if len(on) else None,
                                     "high_area_km2_days": _r3(on["high_km2"].sum()) if len(on) else 0.0,
                                     "days_with_large_group": int((on["max_km2"] >= LARGE_KM2).sum()) if len(on) else 0})
            if not yrs_ok:
                summary["indices"][v] = {"skipped": "no year saved"}
                continue
            ix = {"thresholds": {"season": _r3(T_s), "grouping": _r3(T_c)}, "areas": {}}
            yarr = np.array(years)
            recent = yarr >= max(yrs_ok) - (RECENT_YEARS - 1)
            for area, rows in self.areas.items():
                a_out = {"cells": int(len(rows)), "years": {}}
                series = {k: [] for k in ("spring_start", "spring_end", "spring_days", "fall_start", "fall_end", "fall_days")}
                for yi, y in enumerate(years):
                    rec = {}
                    for w in ("spring", "fall"):
                        st, en, dy = W[f"{w}_start"][yi, rows], W[f"{w}_end"][yi, rows], W[f"{w}_days"][yi, rows]
                        has = np.isfinite(dy)
                        if has.mean() < 0.5:                     # most of the area not saved this year
                            for k in ("start", "end", "days"):
                                series[f"{w}_{k}"].append(np.nan)
                            continue
                        share = float((dy[has] > 0).mean())
                        rec[w] = {"share_with_window": round(share, 3),
                                  "start": _r3(_q(st, 50)), "start_p10": _r3(_q(st, 10)), "start_p90": _r3(_q(st, 90)),
                                  "end": _r3(_q(en, 50)), "end_p10": _r3(_q(en, 10)), "end_p90": _r3(_q(en, 90)),
                                  "days": _r3(np.nanmean(dy))}
                        series[f"{w}_start"].append(_q(st, 50))
                        series[f"{w}_end"].append(_q(en, 50))
                        series[f"{w}_days"].append(float(np.nanmean(dy)))
                        season_rows.append({"index": v, "area": area, "year": y, "window": w, **rec[w],
                                            "start_date": _fmt_doy(rec[w]["start"]), "end_date": _fmt_doy(rec[w]["end"])})
                    if rec:
                        a_out["years"][str(y)] = rec
                a_out["trends"] = {k: _trend(years, np.array(vv)) for k, vv in series.items()}
                env = {}
                for w in ("spring", "fall"):
                    for lab, sel in (("all_years", np.ones(len(years), bool)), ("last_20_years", recent)):
                        st = W[f"{w}_start"][sel][:, rows]
                        en = W[f"{w}_end"][sel][:, rows]
                        if np.isfinite(st).any():
                            env.setdefault(w, {})[lab] = {"from": _r3(_q(st, 10)), "to": _r3(_q(en, 90)),
                                                          "from_date": _fmt_doy(_q(st, 10)), "to_date": _fmt_doy(_q(en, 90)),
                                                          "median_start": _fmt_doy(_q(st, 50)), "median_end": _fmt_doy(_q(en, 50))}
                a_out["envelope"] = env
                ix["areas"][area] = a_out
            # cell maps: median window dates, mean days, trends; groupings
            cm = {}
            with np.errstate(invalid="ignore"), _quiet():
                for k in W:
                    cm[f"{k}_median" if not k.endswith("_days") else f"{k}_mean"] = (
                        np.nanmedian(W[k], axis=0) if not k.endswith("_days") else np.nanmean(W[k], axis=0))
                for k in ("spring_start", "spring_end", "fall_start", "fall_end", "spring_days", "fall_days"):
                    sl, S, n, p = theil_sen(yarr.astype(float), W[k])
                    cm[f"{k}_trend"] = sl * 10
                    cm[f"{k}_tau"] = np.where(n >= 10, S / np.maximum(1, n * (n - 1) / 2.0), np.nan)
                n_years = max(1, len(yrs_ok))
                cm["large_group_days_per_year"] = big_days / n_years
                cm["share_of_high_days_in_large_group"] = np.where(high_days > 0, big_days / np.maximum(high_days, 1), np.nan)
            cell_maps[v] = cm
            cy = [r for r in cluster_rows if r["index"] == v]
            ix["groupings"] = {
                "size_histogram_km2": dict(zip(SIZE_LABELS, map(int, size_hist))),
                "mean_group_km2": _r3(np.mean([r["mean_group_km2"] for r in cy if r["mean_group_km2"] is not None]))
                if any(r["mean_group_km2"] is not None for r in cy) else None,
                "trends": {k: _trend([r["year"] for r in cy], np.array([np.nan if r[k] is None else r[k] for r in cy], float))
                           for k in ("days_with_groups", "mean_group_km2", "mean_largest_km2", "largest_km2",
                                     "days_with_large_group")} if len(cy) >= 2 else {}}
            summary["indices"][v] = ix
        # ---- daily vs hourly FWI
        if "FWI" in indices:
            cmp_out = self._daily_vs_hourly(paths, years, ref_years, mainland_idx, progress, done, steps)
            done += 2 * len(years)
            if cmp_out is not None:
                summary["daily_vs_hourly"], cell_maps["FWI_compare"] = cmp_out
        # ---- write
        progress(done, steps, "writing results")
        pd.DataFrame(season_rows).to_csv(tmp_dir / "season_windows.csv", index=False)
        pd.DataFrame(cluster_rows).to_csv(tmp_dir / "groupings_yearly.csv", index=False)
        pd.DataFrame(cluster_daily).to_csv(tmp_dir / "groupings_daily.csv", index=False)
        pts = self.grid.points
        flat = {f"{v}.{k}": np.asarray(a_, dtype=np.float32) for v, cm in cell_maps.items() for k, a_ in cm.items()}
        np.savez_compressed(tmp_dir / "cells.npz", point_id=pts.point_id.values, **flat)
        cols = {"point_id": pts.point_id.values, "latitude": pts.latitude.values.round(5), "longitude": pts.longitude.values.round(5),
                "county": pts.county.values}
        cols.update({k: np.where(np.isfinite(a_), np.round(a_, 4), np.nan) for k, a_ in flat.items()})
        pd.DataFrame(cols).to_csv(tmp_dir / "cells.csv", index=False)
        (tmp_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=_json_default), encoding="utf-8")
        _swap_dir(tmp_dir, out_dir)
        progress(steps, steps, "done")
        return {"id": aid, "dir": str(out_dir), "years": len(years), "indices": list(summary["indices"])}

    # ------------------------------------------------------------------------------------------------ groupings
    def _clusters(self, X, year, T):
        """Daily connected high-danger areas. Returns (per-day rows, all group sizes, per-cell days in a large group,
        per-cell high days) or None without scipy."""
        try:
            from scipy import ndimage
        except ImportError:                                                # pragma: no cover
            logger.warning("scipy not installed: groupings skipped")
            return None
        s0, s1 = _window(year, (3, 1), (11, 30))
        st = self.store
        area = self.area_km2
        grid_area = np.zeros((self.ny, self.nx))
        grid_area[st.y, st.x] = area
        rows_of = np.full((self.ny, self.nx), -1, dtype=np.int64)
        rows_of[st.y, st.x] = np.arange(self.grid.n_cells)
        struct = np.ones((3, 3), dtype=bool)
        daily, sizes = [], []
        big = np.zeros(self.grid.n_cells)
        high = np.zeros(self.grid.n_cells)
        d365 = _doy_365(year, X.shape[0])
        T32 = np.float32(T)
        for di in range(s0, s1):
            x = X[di]
            fin = np.isfinite(x)
            if not fin.any():
                continue
            cov = round(float(fin.mean()), 3)                         # share of the study cells saved that day
            hi = fin & (x >= T32)
            g = np.zeros((self.ny, self.nx), dtype=bool)
            g[st.y[hi], st.x[hi]] = True
            lab, n = ndimage.label(g, structure=struct)
            if n:
                sz = np.bincount(lab.ravel(), weights=grid_area.ravel(), minlength=n + 1)[1:]
                sizes.append(sz)
                high[hi] += 1
                cell_lab = lab[st.y, st.x]
                big_lab = np.where(sz >= LARGE_KM2)[0] + 1
                if len(big_lab):
                    big[np.isin(cell_lab, big_lab)] += 1
                daily.append({"year": year, "doy": int(d365[di]) if d365[di] >= 0 else 58, "n_groups": int(n),
                              "mean_km2": round(float(sz.mean()), 2), "max_km2": round(float(sz.max()), 2),
                              "high_km2": round(float(sz.sum()), 2), "saved_share": cov})
            else:
                daily.append({"year": year, "doy": int(d365[di]) if d365[di] >= 0 else 58, "n_groups": 0,
                              "mean_km2": None, "max_km2": 0.0, "high_km2": 0.0, "saved_share": cov})
        return daily, (np.concatenate(sizes) if sizes else np.array([])), big, high

    # ------------------------------------------------------------------------------------------------ daily vs hourly
    def _daily_vs_hourly(self, paths, years, ref_years, mainland_idx, progress, done, steps):
        from aorc_tools.explorer.store import FILL_I2, PACK
        names = ["FWI_daily_noon", "FWI_daily_max", "FWI_noon", "FWI_hour_of_max"]
        scale_d, scale_h = PACK["FWI_daily"][0], PACK["FWI"][0]
        # regional CDFs of each system (mainland, Mar-Nov, reference years) -> thresholds and percentile ranks
        cnt = {k: np.zeros(65536, dtype=np.int64) for k in ("d", "h")}
        for y in ref_years:
            if paths.get(y) is None:
                continue
            r = read_daily_vars(self.store, paths[y], names[:2])
            if len(r) < 2:
                continue
            s0, s1 = _window(y, (3, 1), (11, 30))
            for key, nm in (("d", "FWI_daily_noon"), ("h", "FWI_daily_max")):
                a = r[nm][s0:s1][:, mainland_idx].ravel()
                a = a[a != FILL_I2].astype(np.int64) + 32768
                cnt[key] += np.bincount(a, minlength=65536)
        if not cnt["d"].sum() or not cnt["h"].sum():
            return None
        cdf = {k: np.cumsum(c) / c.sum() for k, c in cnt.items()}
        thr_d = _hist_percentiles(cnt["d"], scale_d, (90,))[90]
        thr_h = _hist_percentiles(cnt["h"], scale_h, (90,))[90]
        nc = self.grid.n_cells
        acc = {k: np.zeros(nc) for k in ("n", "sx", "sy", "sxx", "syy", "sxy", "rx", "ry", "rxx", "ryy", "rxy",
                                         "both", "h_only", "d_only", "neither", "excess", "bias")}
        month = {m: {k: 0.0 for k in ("both", "h_only", "d_only", "neither", "excess", "n")} for m in range(1, 13)}
        hour_hist = np.zeros(24, dtype=np.int64)
        yearly = []
        for y in years:
            progress(done, steps, f"daily vs hourly FWI {y}")
            done += 2
            if paths.get(y) is None:
                continue
            r = read_daily_vars(self.store, paths[y], names)
            if "FWI_daily_noon" not in r or "FWI_daily_max" not in r:
                continue
            s0, s1 = _window(y, (3, 1), (11, 30))
            rd, rh = r["FWI_daily_noon"][s0:s1], r["FWI_daily_max"][s0:s1]
            ok = (rd != FILL_I2) & (rh != FILL_I2)
            x = np.where(ok, rd.astype(np.float64) * scale_d, 0.0)
            yv = np.where(ok, rh.astype(np.float64) * scale_h, 0.0)
            rx = np.where(ok, cdf["d"][np.clip(rd.astype(np.int64) + 32768, 0, 65535)], 0.0)
            ry = np.where(ok, cdf["h"][np.clip(rh.astype(np.int64) + 32768, 0, 65535)], 0.0)
            hd = ok & (x >= np.float32(thr_d))
            hh = ok & (yv >= np.float32(thr_h))
            acc["n"] += ok.sum(0)
            acc["sx"] += x.sum(0); acc["sy"] += yv.sum(0); acc["sxx"] += (x * x).sum(0); acc["syy"] += (yv * yv).sum(0)
            acc["sxy"] += (x * yv).sum(0)
            acc["rx"] += rx.sum(0); acc["ry"] += ry.sum(0); acc["rxx"] += (rx * rx).sum(0); acc["ryy"] += (ry * ry).sum(0)
            acc["rxy"] += (rx * ry).sum(0)
            acc["both"] += (hd & hh).sum(0); acc["h_only"] += (hh & ~hd).sum(0); acc["d_only"] += (hd & ~hh).sum(0)
            acc["neither"] += (ok & ~hd & ~hh).sum(0)
            acc["bias"] += np.where(ok, yv - x, 0).sum(0)
            if "FWI_noon" in r:
                rn = r["FWI_noon"][s0:s1]
                okn = ok & (rn != FILL_I2)
                acc["excess"] += np.where(okn, yv - rn.astype(np.float64) * scale_h, 0).sum(0)
            if "FWI_hour_of_max" in r:
                hm = r["FWI_hour_of_max"][s0:s1]
                hour_hist += np.bincount(hm[hh & (hm >= 0)].astype(np.int64), minlength=24)[:24]
            dates = pd.date_range(pd.Timestamp(y, 1, 1) + pd.Timedelta(days=s0), periods=s1 - s0)
            for mth in range(3, 12):
                sel = (dates.month == mth)
                if not sel.any():
                    continue
                M = month[mth]
                okm = ok[sel][:, mainland_idx]
                M["n"] += okm.sum()
                M["both"] += (hd & hh)[sel][:, mainland_idx].sum(); M["h_only"] += (hh & ~hd)[sel][:, mainland_idx].sum()
                M["d_only"] += (hd & ~hh)[sel][:, mainland_idx].sum(); M["neither"] += (okm & ~hd[sel][:, mainland_idx] & ~hh[sel][:, mainland_idx]).sum()
                if "FWI_noon" in r:
                    rn = r["FWI_noon"][s0:s1][sel][:, mainland_idx]
                    okn = okm & (rn != FILL_I2)
                    M["excess"] += np.where(okn, yv[sel][:, mainland_idx] - rn.astype(np.float64) * scale_h, 0).sum()
            frac = ok[:, mainland_idx].mean() if ok.size else 0
            if frac >= MIN_SEASON_FRACTION:
                yearly.append({"year": y, "days_high_daily": round(float(hd[:, mainland_idx].sum(0).mean()), 3),
                               "days_high_hourly": round(float(hh[:, mainland_idx].sum(0).mean()), 3)})
        n = acc["n"]
        with np.errstate(invalid="ignore", divide="ignore"):
            def corr(sx, sy, sxx, syy, sxy):
                cov = sxy / n - (sx / n) * (sy / n)
                return cov / np.sqrt((sxx / n - (sx / n) ** 2) * (syy / n - (sy / n) ** 2))
            r_val = corr(acc["sx"], acc["sy"], acc["sxx"], acc["syy"], acc["sxy"])
            r_rank = corr(acc["rx"], acc["ry"], acc["rxx"], acc["ryy"], acc["rxy"])
            a_, b_, c_, d_ = acc["both"], acc["h_only"], acc["d_only"], acc["neither"]
            po = (a_ + d_) / n
            pe = ((a_ + b_) * (a_ + c_) + (c_ + d_) * (b_ + d_)) / (n * n)
            kappa = (po - pe) / (1 - pe)
            hit = a_ / (a_ + b_)
            far = c_ / (a_ + c_)
            bias = acc["bias"] / n
            excess = acc["excess"] / n
        adequate = (r_rank >= 0.9) & (kappa >= 0.6)
        maps = {"r": r_val, "rank_r": r_rank, "kappa": kappa, "hit_rate": hit, "false_alarm_ratio": far,
                "mean_hourly_minus_daily": bias, "afternoon_excess": excess, "daily_adequate": adequate.astype(float)}
        maps = {k: np.where(n >= 30, v_, np.nan) for k, v_ in maps.items()}
        mainland = self.mainland
        by_area = {}
        for area, rows in self.areas.items():
            with np.errstate(invalid="ignore"), _quiet():
                by_area[area] = {k: _r3(np.nanmedian(maps[k][rows])) for k in ("rank_r", "kappa", "hit_rate", "afternoon_excess")}
                by_area[area]["share_daily_adequate"] = _r3(np.nanmean(maps["daily_adequate"][rows]))
        months = {}
        for mth, M in month.items():
            if M["n"]:
                a_, b_, c_, d_ = M["both"], M["h_only"], M["d_only"], M["neither"]
                tot = a_ + b_ + c_ + d_
                po = (a_ + d_) / tot
                pe = ((a_ + b_) * (a_ + c_) + (c_ + d_) * (b_ + d_)) / (tot * tot)
                months[str(mth)] = {"kappa": _r3((po - pe) / (1 - pe)) if pe < 1 else None,
                                    "hit_rate": _r3(a_ / (a_ + b_)) if a_ + b_ else None,
                                    "afternoon_excess": _r3(M["excess"] / M["n"]),
                                    "share_high_hourly": _r3((a_ + b_) / tot), "share_high_daily": _r3((a_ + c_) / tot)}
        yd = pd.DataFrame(yearly)
        trends = {}
        if len(yd) >= 2:
            trends = {"days_high_daily": _trend(yd.year.values, yd.days_high_daily.values),
                      "days_high_hourly": _trend(yd.year.values, yd.days_high_hourly.values),
                      "difference": _trend(yd.year.values, (yd.days_high_hourly - yd.days_high_daily).values)}
        with np.errstate(invalid="ignore"), _quiet():
            out = {"thresholds": {"daily_p90": _r3(thr_d), "hourly_max_p90": _r3(thr_h)},
                   "mainland": {k: _r3(np.nanmedian(maps[k][mainland])) for k in maps if k != "daily_adequate"},
                   "share_daily_adequate": _r3(np.nanmean(maps["daily_adequate"][mainland])),
                   "by_area": by_area, "months": months,
                   "peak_hour_histogram": {str(h): int(c) for h, c in enumerate(hour_hist)},
                   "yearly": yearly, "trends": trends,
                   "notes": ["Thresholds are each system's own regional 90th percentile (absolute values do not transfer).",
                             "Afternoon excess = hourly daily maximum minus the hourly value at 12:00 LST.",
                             "Computing the classic daily FWI costs about 1/24 of the hourly system."]}
        return out, maps

    # ------------------------------------------------------------------------------------------------ results
    def list(self):
        out = []
        if self.root.exists():
            for d in sorted(self.root.iterdir()):
                if d.is_dir() and (d / "summary.json").exists() and not d.name.endswith((".tmp", ".old")):
                    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
                    out.append({"id": d.name, "source": s["source"], "config_id": s["config_id"], "created": s["created"],
                                "years": [min(s["years"]), max(s["years"])]})
        return out

    def _dir(self, aid):
        if not isinstance(aid, str) or not re.match(r"^[A-Za-z0-9_.-]+$", aid) or ".." in aid:
            raise ValueError("invalid analysis id")
        d = self.root / aid
        if not (d / "summary.json").exists():
            raise KeyError(f"no pattern analysis {aid}")
        return d

    def summary(self, aid):
        return json.loads((self._dir(aid) / "summary.json").read_text(encoding="utf-8"))

    def cell_field(self, aid, key):
        with np.load(self._dir(aid) / "cells.npz") as z:
            if key not in z.files:
                raise KeyError(f"no cell field {key}")
            return z["point_id"].copy(), z[key].copy()

    def table(self, aid, name):
        if name not in ("season_windows", "groupings_yearly", "groupings_daily"):
            raise ValueError("unknown table")
        return pd.read_csv(self._dir(aid) / f"{name}.csv")

    def file(self, aid, name):
        if name not in ("summary.json", "season_windows.csv", "groupings_yearly.csv", "groupings_daily.csv", "cells.csv"):
            raise ValueError("unknown file")
        return self._dir(aid) / name
