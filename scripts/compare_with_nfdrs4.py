"""
Compare this repo's DERIVED ERC/BI (aorc_tools.fire_indices.nfdrs.compute_erc_bi,
as used by the explorer) with the official USFS Fire Lab NFDRS4 (NFDRS 2016)
reference implementation, using NFDRS4's own sample RAWS record
(data/241513_2001_2017.fw21: 17 years of hourly weather, 46.8 N).

NFDRS 2016 has no fuel model G, but NFDRS4's sample custom fuel model "C" is the
NFDRS-88 fuel model G parameter set, so both sides use the same fuel bed.

    # 1) build NFDRS4 (see its README), run it with fuelModel = "C" and
    #    outputInterval = "0" to write the hourly allOutputsFile
    # 2) python scripts/compare_with_nfdrs4.py --nfdrs4-csv all_G.csv [--out docs/validation]

Runs compared against NFDRS4 (all fuel model G, hourly, same weather):
  explorer      our timelag dead-fuel moisture + constant live moisture (120/100 %)
  legacy-emc    EMC multipliers (1.0/1.28/1.5/1.8) + constant live moisture
  formula-only  our ERC/BI equations fed with NFDRS4's own dead AND live moistures
Daily values = the 13:00 local observation (NFDRS obsHour). Because ERC/BI are
reported as percentiles, the headline metric is rank agreement.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aorc_tools.fire_indices.fuel_moisture import NelsonFuelMoisture, emc_fuel_moisture  # noqa: E402
from aorc_tools.fire_indices.nfdrs import compute_erc_bi  # noqa: E402


def load(path):
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    t = df["DateTime"].str.slice(0, 15)
    df["local"] = pd.to_datetime(t, format="%Y%m%dT%H%M%S")
    return df.dropna(subset=["Temperature(F)", "RelativeHumidity(%)", "WindSpeed(mph)", "Precipitation(in)"]).reset_index(drop=True)


def ours(df, mode):
    n = len(df)
    tf, rh = df["Temperature(F)"].values, df["RelativeHumidity(%)"].values
    ws, pmm = df["WindSpeed(mph)"].values, df["Precipitation(in)"].values * 25.4
    out = {k: np.empty(n) for k in ("ERC", "BI", "fm1", "fm10", "fm100", "fm1000")}
    if mode == "formula-only":
        r = compute_erc_bi(df["1HourDFM(%)"].values, df["10HourDFM(%)"].values, df["100HourDFM(%)"].values,
                           df["1000HourDFM(%)"].values, df["HerbLFM(%)"].values, df["WoodyLFM(%)"].values, ws, fuel_model="G")
        return {"ERC": np.asarray(r["ERC"], float), "BI": np.asarray(r["BI"], float)}
    nel = NelsonFuelMoisture(1)
    for i in range(n):
        if mode == "explorer":
            fm = nel.update(np.array([tf[i]]), np.array([rh[i]]), np.array([pmm[i]]))
        else:
            fm = emc_fuel_moisture(np.array([tf[i]]), np.array([rh[i]]))
        for k in ("fm1", "fm10", "fm100", "fm1000"):
            out[k][i] = float(np.asarray(fm[k])[0])
    r = compute_erc_bi(out["fm1"], out["fm10"], out["fm100"], out["fm1000"], np.full(n, 120.0), np.full(n, 100.0), ws, fuel_model="G")
    out["ERC"], out["BI"] = np.asarray(r["ERC"], float), np.asarray(r["BI"], float)
    return out


def agree(ref, got):
    ok = np.isfinite(ref) & np.isfinite(got)
    a, b = pd.Series(ref[ok]), pd.Series(got[ok])
    pa, pb = a.rank(pct=True) * 100, b.rank(pct=True) * 100
    top_a, top_b = pa >= 90, pb >= 90
    return {"n": int(ok.sum()), "pearson_r": float(a.corr(b)), "spearman_rho": float(a.corr(b, method="spearman")),
            "mean_abs_percentile_diff": float((pa - pb).abs().mean()),
            "top10pct_hit_rate": float((top_a & top_b).sum() / max(1, top_a.sum())),
            "ref_mean": float(a.mean()), "got_mean": float(b.mean())}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--nfdrs4-csv", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "validation")
    a = ap.parse_args(argv)
    df = load(a.nfdrs4_csv)
    obs = (df["local"].dt.hour == 13).values
    season = df["local"].dt.month.between(4, 10).values
    runs = {m: ours(df, m) for m in ("explorer", "legacy-emc", "formula-only")}
    res = {}
    for m, o in runs.items():
        res[m] = {}
        for v in ("ERC", "BI"):
            ref = df[v].values.astype(float)
            res[m][v] = {"hourly_all": agree(ref, o[v]), "daily_13LST_all": agree(ref[obs], o[v][obs]),
                         "daily_13LST_AprOct": agree(ref[obs & season], o[v][obs & season])}
        if "fm1000" in o:
            res[m]["dead_fm_vs_nfdrs4"] = {k: agree(df[c].values.astype(float), o[k]) for k, c in
                                          (("fm1", "1HourDFM(%)"), ("fm10", "10HourDFM(%)"), ("fm100", "100HourDFM(%)"), ("fm1000", "1000HourDFM(%)"))}
    live = {"HerbLFM_mean": float(df["HerbLFM(%)"].mean()), "WoodyLFM_mean": float(df["WoodyLFM(%)"].mean()),
            "HerbLFM_AprOct_13LST_range": [float(df["HerbLFM(%)"][obs & season].min()), float(df["HerbLFM(%)"][obs & season].max())]}
    a.out.mkdir(parents=True, exist_ok=True)
    meta = {"station": "RAWS 241513 (NFDRS4 sample), 2001-2017 hourly, 46.82 N", "fuel_model": "NFDRS-88 G (NFDRS4 custom C)",
            "hours": int(len(df)), "nfdrs4_live_moisture": live, "results": res}
    (a.out / "nfdrs4_comparison.json").write_text(json.dumps(meta, indent=1))
    print(f"NFDRS4 sample RAWS · {len(df)} hours · fuel model G")
    print(f"{'run':13s} {'var':4s} {'basis':20s} {'pearson':>8s} {'spearman':>9s} {'|dPct|':>7s} {'top10%hit':>9s} {'ref mean':>9s} {'ours':>8s}")
    for m in res:
        for v in ("ERC", "BI"):
            for basis, s in res[m][v].items():
                print(f"{m:13s} {v:4s} {basis:20s} {s['pearson_r']:8.3f} {s['spearman_rho']:9.3f} {s['mean_abs_percentile_diff']:7.2f} "
                      f"{100 * s['top10pct_hit_rate']:8.1f}% {s['ref_mean']:9.2f} {s['got_mean']:8.2f}")
        if "dead_fm_vs_nfdrs4" in res[m]:
            print("   dead FM r:", {k: round(x["pearson_r"], 3) for k, x in res[m]["dead_fm_vs_nfdrs4"].items()},
                  "means ref/ours:", {k: (round(x["ref_mean"], 1), round(x["got_mean"], 1)) for k, x in res[m]["dead_fm_vs_nfdrs4"].items()})
    print("NFDRS4 live moisture:", live)
    return meta


if __name__ == "__main__":
    main()
