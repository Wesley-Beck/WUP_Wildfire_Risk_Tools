"""
Which weather variable drives each index, brick by brick (Patterns tab › Driving variables, ``--drivers``).

For a fixed random sample of cells in every brick, the chain is re-run March-November of each year in trace mode.
It starts from the saved chain state where the record exists, so it continues the record exactly; otherwise it
cold-starts with 30 days of spin-up. On every local standard day, the drivers are read at the hour of each index's
daily maximum (12:00 LST for the classic daily FWI):

* FWI / FWI_daily — temperature, relative humidity, wind speed, rain in the previous 72 h, fuel dryness (BUI);
  plus the FWI system's own split: fine-fuel / wind spread (ISI) vs deep-fuel dryness (BUI).
* ERC — temperature, RH, rain (72 h), 1000-h fuel moisture (ERC has no wind term).
* BI — temperature, RH, wind, rain (72 h), 1000-h fuel moisture.
* FPI — temperature, RH, rain (72 h), relative greenness (vegetation).

Two answers per brick, index and season part (all / spring Mar-Jun / summer Jul-Aug / fall Sep-Nov):

1. **Share of the variability explained** by each driver: the Shapley (LMG) decomposition of the R2 of a linear
   regression of the index on the standardised drivers. The shares sum to the R2.
2. **Dominant driver on high-danger days** (index at or above the brick's 90th percentile): for each such day, the
   driver with the most extreme anomaly in the dangerous direction (warmer, drier, windier, less rain, drier fuel,
   less green), against that brick and calendar month's climatology. Reported as the share of high days each driver
   dominates.

These are statistical attributions over the sample, not a physical sensitivity analysis. Drivers are correlated
(hot days are dry days); the Shapley shares divide the shared part fairly but do not separate it.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import shutil
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from aorc_tools.explorer.lines import ISLE_ROYALE_LAT, _json_default, _r3, _swap_dir
from aorc_tools.explorer.workspace import now_iso

logger = logging.getLogger(__name__)

DRIVERS = {
    "FWI": ["temperature", "humidity", "wind", "rain_72h", "fuel_dryness_BUI"],
    "FWI_daily": ["temperature", "humidity", "wind", "rain_72h", "fuel_dryness_BUI"],
    "ERC": ["temperature", "humidity", "rain_72h", "fuel_moisture_1000h"],
    "BI": ["temperature", "humidity", "wind", "rain_72h", "fuel_moisture_1000h"],
    "FPI": ["temperature", "humidity", "rain_72h", "greenness"],
}
DANGER_SIGN = {"temperature": 1, "humidity": -1, "wind": 1, "rain_72h": -1, "fuel_dryness_BUI": 1,
               "fuel_moisture_1000h": -1, "greenness": -1, "ISI": 1, "BUI": 1}
LABELS = {"temperature": "temperature", "humidity": "relative humidity", "wind": "wind speed", "rain_72h": "rain (72 h)",
          "fuel_dryness_BUI": "fuel dryness (BUI)", "fuel_moisture_1000h": "1000-h fuel moisture", "greenness": "greenness",
          "ISI": "spread: wind + fine fuels (ISI)", "BUI": "build-up: deep-fuel dryness (BUI)"}
PARTS = {"all": (3, 11), "spring": (3, 6), "summer": (7, 8), "fall": (9, 11)}


def shapley_r2(X, y):
    """LMG / Shapley decomposition of the OLS R2 over the columns of X (standardised inside). Returns (shares, R2)."""
    ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
    X, y = X[ok], y[ok]
    p = X.shape[1]
    if len(y) < 10 * (p + 1) or np.std(y) == 0:
        return np.full(p, np.nan), np.nan
    sd = X.std(axis=0)
    keep = sd > 0
    Z = np.zeros_like(X)
    Z[:, keep] = (X[:, keep] - X[:, keep].mean(axis=0)) / sd[keep]
    yz = (y - y.mean()) / y.std()
    r2 = {}
    for k in range(p + 1):
        for S in itertools.combinations(range(p), k):
            if not S:
                r2[S] = 0.0
                continue
            A = Z[:, S]
            coef, *_ = np.linalg.lstsq(A, yz, rcond=None)
            res = yz - A @ coef
            r2[S] = max(0.0, 1.0 - float(res @ res) / len(yz))
    from math import factorial
    shares = np.zeros(p)
    for j in range(p):
        others = [i for i in range(p) if i != j]
        for k in range(p):
            w = factorial(k) * factorial(p - k - 1) / factorial(p)
            for S in itertools.combinations(others, k):
                shares[j] += w * (r2[tuple(sorted(S + (j,)))] - r2[S])
    return shares, r2[tuple(range(p))]


def dominant_driver(Z_oriented):
    """Index of the most dangerous standardised anomaly per row (NaN-safe); -1 when a row has none."""
    Zf = np.where(np.isfinite(Z_oriented), Z_oriented, -np.inf)
    idx = Zf.argmax(axis=1)
    return np.where(np.isfinite(Z_oriented).any(axis=1), idx, -1)


class DriverAnalysis:
    def __init__(self, ws, grid, store, ctx):
        self.ws, self.grid, self.store, self.ctx = ws, grid, store, ctx
        self.root = ws.root / "patterns" / "drivers"

    def sample(self, n_per_brick=12, seed=7):
        pts = self.grid.points
        out = {}
        rng = np.random.default_rng(seed)
        for (ci, cj), g in pts.groupby(["ci", "cj"]):
            g = g[g.latitude < ISLE_ROYALE_LAT] if (g.latitude < ISLE_ROYALE_LAT).any() else g
            k = min(n_per_brick, len(g))
            out[f"{ci}_{cj}"] = sorted(int(x) for x in rng.choice(g.point_id.values, size=k, replace=False))
        return out

    def _year_days(self, source, cfg, bkey, pids, year, src, chain_kw):
        """Trace run for the sample cells, Mar-Nov of ``year``; per local day, the drivers at each index's daily max."""
        from aorc_tools.explorer.chain import (kelvin_to_celsius, ms_to_kph, run_chain, specific_to_relative_humidity as rh_fn,
                                               wind_components_to_speed)
        d0, d1 = src.domain(self.ws.settings)
        start = max(d0, datetime(year, 3, 1) - timedelta(hours=6))
        end = min(d1, datetime(year, 12, 1, 6))
        if end <= start:
            return None
        ch0 = self.grid.time_chunks(start, start, src.origin_of_year)[0]
        t_prev = ch0["start"] - timedelta(hours=1)
        pc = self.grid.time_chunks(t_prev, t_prev, src.origin_of_year)[0]
        state = None
        if self.store.has_chunk(source, cfg.id, pc["year"], pc["k"], bkey, need_end=t_prev):
            state = self.store.state_at(source, cfg.id, pc["year"], pc["k"], bkey, "end", pids)
        if state is not None:
            w_start, spin = ch0["start"], 0
        else:
            w_start = max(d0, ch0["start"] - timedelta(hours=720))
            spin = int((ch0["start"] - w_start).total_seconds() // 3600)
        raw = src.fetch(self.grid, pids, w_start, end, gap_fill=cfg.gap_fill)
        res = run_chain(raw, grid=self.grid, cfg=cfg, state=state, spinup_hours=spin, mode="trace", **chain_kw)
        m = res.analysis_mask
        S = {k: np.asarray(v)[m] for k, v in res.steps.items()}
        times = pd.DatetimeIndex(res.times[m])
        tk = S["TMP_2maboveground"].astype(np.float64)
        T = kelvin_to_celsius(tk)
        RH = rh_fn(S["SPFH_2maboveground"].astype(np.float64), tk, S["PRES_surface"].astype(np.float64))
        WS = ms_to_kph(wind_components_to_speed(S["UGRD_10maboveground"].astype(np.float64), S["VGRD_10maboveground"].astype(np.float64)))
        P = np.nan_to_num(S["APCP_surface"].astype(np.float64))
        cs = np.cumsum(np.vstack([np.zeros((1, P.shape[1])), P]), axis=0)
        i = np.arange(len(times))
        R72 = cs[i + 1] - cs[np.maximum(0, i - 71)]
        hourly = {"temperature": T, "humidity": RH, "wind": WS, "rain_72h": R72,
                  "fuel_dryness_BUI": S.get("BUI"), "fuel_moisture_1000h": S.get("FM1000"), "greenness": S.get("RG"),
                  "ISI": S.get("ISI"), "BUI": S.get("BUI")}
        rows = self.grid.rows_for(pids)
        off = self.grid.points.std_offset_h.values[rows].astype(int)
        hrs = ((times - pd.Timestamp(year, 1, 1)) / pd.Timedelta("1h")).astype(np.int64).values
        out = []
        for c in range(len(pids)):
            lh = hrs + off[c]
            day, hod = lh // 24, lh % 24
            df = pd.DataFrame({"day": day, "hod": hod})
            for v in ("FWI", "ERC", "BI", "FPI", "FWI_daily"):
                if v not in S:
                    continue
                df[v] = S[v][:, c]
            for k, a in hourly.items():
                if a is not None:
                    df[k] = np.asarray(a)[:, c]
            df = df[(df.day >= 59) & (df.day <= 334 + (1 if pd.Timestamp(year, 12, 31).dayofyear == 366 else 0))]
            for v in ("FWI", "ERC", "BI", "FPI", "FWI_daily"):
                if v not in df:
                    continue
                sub = df[np.isfinite(df[v])]
                if sub.empty:
                    continue
                if v == "FWI_daily":
                    pick = sub[sub.hod == 12]
                else:
                    good = sub.groupby("day").filter(lambda g: len(g) >= 20)
                    if good.empty:
                        continue
                    pick = good.loc[good.groupby("day")[v].idxmax()]
                cols = DRIVERS[v] + (["ISI", "BUI"] if v.startswith("FWI") else [])
                rec = pick[["day", v] + [k for k in cols if k in pick]].copy()
                rec.insert(0, "index", v)
                rec.insert(1, "cell", int(pids[c]))
                rec.insert(2, "year", year)
                rec = rec.rename(columns={v: "value"})
                out.append(rec)
        return pd.concat(out, ignore_index=True) if out else None

    def run(self, source, cfg, years=None, n_per_brick=12, progress=None, chain_kw=None) -> dict:
        progress = progress or (lambda done, total, note="": None)
        src = self.ctx.source_for(source)
        chain_kw = chain_kw if chain_kw is not None else {"ndvi": self.ctx.ndvi_for(cfg, source), "fuel_map": self.ctx.fuel_map}
        d0, d1 = src.domain(self.ws.settings)
        years = years or list(range(d0.year, d1.year + 1))
        samp = self.sample(n_per_brick)
        steps = len(samp) * len(years) + 2
        done = 0
        frames = []
        for bkey, pids in samp.items():
            for y in years:
                progress(done, steps, f"drivers: brick {bkey}, {y}")
                done += 1
                try:
                    f = self._year_days(source, cfg, bkey, pids, y, src, chain_kw)
                except Exception as e:                                     # noqa: BLE001 - a missing year is not fatal
                    logger.warning("drivers: brick %s %s skipped (%s)", bkey, y, e)
                    continue
                if f is not None:
                    f.insert(0, "brick", bkey)
                    frames.append(f)
        if not frames:
            raise ValueError("no driver data: is any of the record computable for these years?")
        D = pd.concat(frames, ignore_index=True)
        D["month"] = (pd.to_datetime(D["year"].astype(str) + "-01-01") + pd.to_timedelta(D["day"], unit="D")).dt.month
        progress(done, steps, "attribution")
        summary = {"id": f"{source}__{cfg.id}", "source": source, "config_id": cfg.id, "created": now_iso(),
                   "years": [int(min(years)), int(max(years))], "cells_per_brick": n_per_brick,
                   "samples": {k: v for k, v in samp.items()}, "labels": LABELS, "drivers": DRIVERS,
                   "method": ("Shapley (LMG) shares of the R2 of a linear regression of the index on its standardised drivers; "
                              "dominant driver on high days = most dangerous standardised anomaly vs the brick-month "
                              "climatology, on days at or above the brick's 90th percentile"),
                   "results": {}}
        rows = []
        for (v, bkey), g in D.groupby(["index", "brick"]):
            cols = [k for k in DRIVERS[v] if k in g and g[k].notna().any()]
            res_b = {}
            for part, (m0, m1) in PARTS.items():
                gp = g[(g.month >= m0) & (g.month <= m1)]
                if len(gp) < 50:
                    continue
                X = gp[cols].to_numpy(dtype=float)
                y = gp["value"].to_numpy(dtype=float)
                sh, r2 = shapley_r2(X, y)
                # dominant driver on high days, anomalies vs brick-month climatology
                mu = gp.groupby("month")[cols].transform("mean")
                sd = gp.groupby("month")[cols].transform("std").replace(0, np.nan)
                Z = ((gp[cols] - mu) / sd).to_numpy(dtype=float) * np.array([DANGER_SIGN[c] for c in cols])
                hi = y >= np.nanpercentile(y, 90)
                dom = dominant_driver(Z[hi])
                share_dom = {cols[j]: _r3(np.mean(dom == j)) for j in range(len(cols))}
                entry = {"n_days": int(len(gp)), "r2": _r3(r2), "shares": {c: _r3(s_) for c, s_ in zip(cols, sh)},
                         "dominant_on_high_days": share_dom}
                if v.startswith("FWI") and {"ISI", "BUI"} <= set(gp.columns):
                    sh2, r22 = shapley_r2(gp[["ISI", "BUI"]].to_numpy(dtype=float), np.log1p(np.clip(y, 0, None)))
                    entry["fwi_split"] = {"ISI": _r3(sh2[0]), "BUI": _r3(sh2[1]), "r2": _r3(r22)}
                res_b[part] = entry
                rows.append({"index": v, "brick": bkey, "part": part, "n_days": len(gp), "r2": entry["r2"],
                             **{f"share.{c}": entry["shares"][c] for c in cols},
                             **{f"dominant.{c}": share_dom[c] for c in cols},
                             **({f"fwi_split.{k}": x for k, x in entry.get("fwi_split", {}).items()})})
            summary["results"].setdefault(v, {})[bkey] = res_b
        aid = summary["id"]
        out_dir, tmp_dir = self.root / aid, self.root / (aid + ".tmp")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True)
        pd.DataFrame(rows).to_csv(tmp_dir / "drivers.csv", index=False)
        D.to_csv(tmp_dir / "driver_days.csv.gz", index=False, compression="gzip")
        (tmp_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=_json_default), encoding="utf-8")
        _swap_dir(tmp_dir, out_dir)
        progress(steps, steps, "done")
        return {"id": aid, "dir": str(out_dir), "rows": len(D), "bricks": len(samp)}

    def list(self):
        out = []
        if self.root.exists():
            for d in sorted(self.root.iterdir()):
                if d.is_dir() and (d / "summary.json").exists() and not d.name.endswith((".tmp", ".old")):
                    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
                    out.append({"id": d.name, "source": s["source"], "created": s["created"], "years": s["years"]})
        return out

    def summary(self, aid):
        if not re.match(r"^[A-Za-z0-9_.-]+$", aid or "") or ".." in aid:
            raise ValueError("invalid id")
        p = self.root / aid / "summary.json"
        if not p.exists():
            raise KeyError(f"no driver analysis {aid}")
        return json.loads(p.read_text(encoding="utf-8"))

    def file(self, aid, name):
        if name not in ("summary.json", "drivers.csv", "driver_days.csv.gz"):
            raise ValueError("unknown file")
        self.summary(aid)
        return self.root / aid / name
