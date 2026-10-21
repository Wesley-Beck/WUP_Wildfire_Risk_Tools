"""
Check aorc_tools.fire_indices.fwi_daily (classic daily FWI used by the explorer
as the comparator to existing daily products) against xclim's
``cffwis_indices`` on cffdrs-ng's daily reference weather (PRF2007_daily_wx.csv:
noon temperature/RH/wind and 24 h rain).

    pip install xclim            # verification only; not a runtime dependency
    python scripts/compare_daily_fwi_xclim.py --cffdrs-ng path/to/cffdrs-ng [--out docs/validation]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def load_fwi_daily():
    spec = importlib.util.spec_from_file_location("fwi_daily", ROOT / "aorc_tools" / "fire_indices" / "fwi_daily.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cffdrs-ng", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "validation")
    a = ap.parse_args(argv)
    import xarray as xr
    import xclim
    from xclim.indices.fire import cffwis_indices

    wx = pd.read_csv(a.cffdrs_ng / "data" / "PRF2007_daily_wx.csv")
    time = pd.to_datetime(dict(year=wx.yr, month=wx.mon, day=wx.day))
    lat = float(wx.lat.iloc[0])

    # ---- ours
    fd = load_fwi_daily()
    st = fd.DailyFWI(1)
    ours = []
    for r in wx.itertuples(index=False):
        o = st.update(np.array([r.temp]), np.array([r.rh]), np.array([r.ws]), np.array([r.prec]), np.array([r.mon]))
        ours.append({k: float(v[0]) for k, v in o.items()})
    ours = pd.DataFrame(ours)

    # ---- xclim (continuous, no season mask, no overwintering, standard start-up values)
    da = lambda v, u: xr.DataArray(wx[v].values.astype(float), dims=("time",), coords={"time": time}, attrs={"units": u})  # noqa: E731
    out = cffwis_indices(tas=da("temp", "degC"), pr=da("prec", "mm/d"), sfcWind=da("ws", "km/h"), hurs=da("rh", "%"),
                         lat=xr.DataArray(lat, attrs={"units": ""}),
                         ffmc0=None, dmc0=None, dc0=None)   # None -> start-up values 85 / 6 / 15
    names = ["DC", "DMC", "FFMC", "ISI", "BUI", "FWI"]
    ref = pd.DataFrame({n: np.asarray(v.values, dtype=float) for n, v in zip(names, out)})

    res = {}
    print(f"xclim {xclim.__version__} · PRF2007 daily · {len(wx)} days")
    print(f"{'var':5s} {'max|d|':>9s} {'mean|d|':>9s} {'ref mean':>9s} {'ours mean':>9s}")
    for k in ["FFMC", "DMC", "DC", "ISI", "BUI", "FWI"]:
        d = ours[k].values - ref[k].values
        ok = np.isfinite(d)
        res[k] = {"max_abs": float(np.max(np.abs(d[ok]))), "mean_abs": float(np.mean(np.abs(d[ok]))),
                  "ref_mean": float(np.nanmean(ref[k])), "ours_mean": float(np.nanmean(ours[k])), "n": int(ok.sum())}
        print(f"{k:5s} {res[k]['max_abs']:9.5f} {res[k]['mean_abs']:9.5f} {res[k]['ref_mean']:9.3f} {res[k]['ours_mean']:9.3f}")
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "daily_fwi_vs_xclim_PRF2007.json").write_text(json.dumps({"xclim": xclim.__version__, "days": len(wx), "results": res}, indent=1))
    pd.concat([wx[["yr", "mon", "day"]], ours.add_suffix("_ours"), ref.add_suffix("_xclim")], axis=1).to_csv(
        a.out / "daily_fwi_vs_xclim_PRF2007.csv", index=False)
    return res


if __name__ == "__main__":
    main()
