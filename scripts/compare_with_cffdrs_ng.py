"""
Compare this repo's hourly FWI (aorc_tools.fire_indices.fwi.HourlyFWI) with the
official NRCan cffdrs-ng implementation (FWI/Python/NG_FWI.py) on cffdrs-ng's
own reference weather: Petawawa Research Forest 2007 (PRF2007_hourly_wx.csv),
whose official output ships as PRF2007_standard_hourly_FWI.csv.

    git clone --depth 1 https://github.com/nrcan-cfs-fire/cffdrs-ng
    python scripts/compare_with_cffdrs_ng.py --cffdrs-ng path/to/cffdrs-ng [--out docs/validation]

Runs (all start from FFMC 85 / DMC 6 / DC 15 like the reference):
  official      NG_FWI.hFWI() re-run here (checks the shipped reference file)
  legacy        our port as the batch record used it (daily-system DMC/DC
                coefficients, rain total reset every dry hour, no FFMC canopy
                interception)
  coef-only     legacy port with only the DMC/DC coefficients swapped
  cffdrs-ng     our port in cffdrs-ng mode (coefficients + rain interception
                + 5-dry-hour canopy reset)  -- should reproduce the reference

Daylight is taken from the reference's own sunrise/sunset columns for all runs
(so only the fire-weather equations are compared), and once more from this
repo's per-cell solar-position test (timebase.daylight_solar) to show that
the time basis we use for AORC matches cffdrs-ng's LST sunrise/sunset test.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aorc_tools.fire_indices.fwi import HourlyFWI  # noqa: E402
from aorc_tools.explorer.timebase import daylight_solar  # noqa: E402

VARS = ["ffmc", "dmc", "dc", "isi", "bui", "fwi"]
FWI_CLASSES = [0, 5, 10, 20, 30, np.inf]          # CWFIS-style low / moderate / high / very high / extreme


def run_ours(wx: pd.DataFrame, daylight: np.ndarray, coefficients: str, rain: str | None = None) -> pd.DataFrame:
    f = HourlyFWI(1, latitude=float(wx["lat"].iloc[0]), coefficients=coefficients, rain=rain)
    rows = []
    for i, r in enumerate(wx.itertuples(index=False)):
        o = f.update(np.array([r.temp]), np.array([r.rh]), np.array([r.ws]), np.array([r.prec]),
                     0, int(r.hr), daylight=np.array([daylight[i]]))
        rows.append({k.lower(): float(v[0]) for k, v in o.items()})
    return pd.DataFrame(rows)


def stats(ref: pd.Series, got: pd.Series) -> dict:
    d = (got.values - ref.values)
    return {"max_abs": float(np.max(np.abs(d))), "mean_abs": float(np.mean(np.abs(d))),
            "rmse": float(np.sqrt(np.mean(d ** 2))), "bias": float(np.mean(d)),
            "r": float(np.corrcoef(ref.values, got.values)[0, 1]) if np.std(got.values) > 0 else float("nan"),
            "ref_mean": float(ref.mean()), "got_mean": float(got.mean())}


def fwi_class(v):
    return np.digitize(v, FWI_CLASSES[1:-1])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cffdrs-ng", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "validation")
    a = ap.parse_args(argv)
    ng = a.cffdrs_ng.resolve()
    sys.path.insert(0, str(ng / "FWI" / "Python"))
    import NG_FWI  # type: ignore

    wx = pd.read_csv(ng / "data" / "PRF2007_hourly_wx.csv")
    ref = pd.read_csv(ng / "data" / "PRF2007_standard_hourly_FWI.csv")
    assert len(wx) == len(ref)
    commit = "unknown"
    try:
        import subprocess
        commit = subprocess.check_output(["git", "-C", str(ng), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        pass

    # --- official re-run (sanity: does NG_FWI reproduce its own reference file?)
    off = NG_FWI.hFWI(wx.copy(), silent=True, round_out=None)
    off = off.reset_index(drop=True)

    # --- daylight from the reference's own sunrise/sunset (LST hours) ...
    hr, sr, ss = ref["hr"].values, ref["sunrise"].values, ref["sunset"].values
    dl_ref = ((sr <= hr) & (hr <= ss)) | ((hr < 6) & (sr <= hr + 24) & (hr + 24 <= ss))
    # ... and from our per-cell solar position at the packet's UTC instant
    lat, lon, tz = float(wx["lat"].iloc[0]), float(wx["long"].iloc[0]), float(wx["timezone"].iloc[0])
    t_utc = [datetime(int(r.yr), int(r.mon), int(r.day), int(r.hr), tzinfo=timezone.utc) - timedelta(hours=tz)
             for r in wx.itertuples(index=False)]
    dl_sol = np.array([bool(np.asarray(daylight_solar(np.array([lat]), np.array([lon]), t))[0]) for t in t_utc])

    runs = {
        "official (re-run)": off,
        "legacy": run_ours(wx, dl_ref, "legacy", rain="legacy"),
        "coef-only": run_ours(wx, dl_ref, "cffdrs-ng", rain="legacy"),
        "cffdrs-ng": run_ours(wx, dl_ref, "cffdrs-ng", rain="cffdrs-ng"),
        "cffdrs-ng + solar daylight": run_ours(wx, dl_sol, "cffdrs-ng", rain="cffdrs-ng"),
        "legacy + solar daylight": run_ours(wx, dl_sol, "legacy", rain="legacy"),
    }
    table, rows = {}, []
    for name, df in runs.items():
        table[name] = {v: stats(ref[v], df[v]) for v in VARS}
        cls_agree = float(np.mean(fwi_class(ref["fwi"].values) == fwi_class(df["fwi"].values)))
        table[name]["fwi_class_agreement"] = cls_agree
        for v in VARS:
            s = table[name][v]
            rows.append({"run": name, "variable": v.upper(), **{k: round(x, 4) for k, x in s.items()}})
        rows.append({"run": name, "variable": "FWI class agreement", "mean_abs": round(cls_agree, 4)})
    daylight_mismatch = int(np.sum(dl_ref != dl_sol))

    a.out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(a.out / "cffdrs_ng_PRF2007_comparison.csv", index=False)
    series = pd.DataFrame({"yr": ref.yr, "mon": ref.mon, "day": ref.day, "hr": ref.hr,
                           "fwi_reference": ref.fwi, **{f"fwi_{k.split(' ')[0]}{'_solar' if 'solar' in k else ''}": d["fwi"].values
                                                        for k, d in runs.items() if k != 'official (re-run)'},
                           "dmc_reference": ref.dmc, "dmc_legacy": runs["legacy"]["dmc"].values, "dmc_cffdrs-ng": runs["cffdrs-ng"]["dmc"].values,
                           "dc_reference": ref.dc, "dc_legacy": runs["legacy"]["dc"].values, "dc_cffdrs-ng": runs["cffdrs-ng"]["dc"].values})
    series.to_csv(a.out / "cffdrs_ng_PRF2007_series.csv", index=False)
    meta = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "cffdrs_ng_commit": commit,
            "station": "Petawawa Research Forest (PRF) 2007, 45.996 N 77.427 W, UTC-4 local hours",
            "n_hours": int(len(wx)), "daylight_hours_differing_solar_vs_reference": daylight_mismatch, "results": table}
    (a.out / "cffdrs_ng_PRF2007_comparison.json").write_text(json.dumps(meta, indent=1))

    print(f"cffdrs-ng commit {commit[:10]} · PRF2007 · {len(wx)} hours · daylight differs (solar vs reference) in {daylight_mismatch} h")
    print(f"{'run':28s} {'var':4s} {'max|d|':>9s} {'mean|d|':>9s} {'bias':>9s} {'r':>7s}   ref_mean  got_mean")
    for name in runs:
        for v in VARS:
            s = table[name][v]
            print(f"{name:28s} {v.upper():4s} {s['max_abs']:9.4f} {s['mean_abs']:9.4f} {s['bias']:9.4f} {s['r']:7.4f}   {s['ref_mean']:8.3f}  {s['got_mean']:8.3f}")
        print(f"{name:28s} FWI class agreement {100 * table[name]['fwi_class_agreement']:.1f}%")
    return meta


if __name__ == "__main__":
    main()
