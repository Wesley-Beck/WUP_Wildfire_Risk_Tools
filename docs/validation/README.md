# Validation against official reference implementations

Run on 2026-10-20 in a cloud workspace. Each comparison feeds identical weather to this
repo's code and to an official implementation, then compares the outputs. Everything here
can be re-run with the scripts named below.

## 1. Hourly FWI vs NRCan cffdrs-ng (FWI2025): exact once fixed

`scripts/compare_with_cffdrs_ng.py --cffdrs-ng <clone>` uses cffdrs-ng `ed143d7` (main,
2026-08-13), its reference weather `PRF2007_hourly_wx.csv` (Petawawa Research Forest,
2,623 h) and its official output `PRF2007_standard_hourly_FWI.csv`. All runs start from
FFMC 85 / DMC 6 / DC 15.

| Run | DMC mean (ref 17.45) | DC mean (ref 136.1) | FWI mean (ref 3.84) | FWI max abs diff | FWI r | FWI in the same class* |
|---|---|---|---|---|---|---|
| official `hFWI()` re-run | 17.45 | 136.1 | 3.84 | 0 | 1.000 | 100 % |
| **legacy port** (the existing batch record) | **0.36** | **1,528.6** | **0.72** | 18.95 | 0.62 | 72.4 % |
| legacy port + cffdrs-ng DMC/DC coefficients only | 19.01 | 154.2 | 3.97 | 5.83 | 0.997 | 96.5 % |
| **cffdrs-ng mode** (new default) | 17.45 | 136.1 | 3.84 | **< 0.0001** | 1.000 | 100 % |
| cffdrs-ng mode + per-cell solar daylight (as run on AORC) | 17.47 | 136.2 | 3.84 | 0.05 | 1.000 | 100 % |

\*Classes: < 5, 5–10, 10–20, 20–30, ≥ 30.

What the legacy port gets wrong:
1. It applies the daily-system DMC/DC drying coefficients every daylight hour. DMC barely dries
   and collapses toward 0, and DC runs away past 3,000.
2. It resets its running rain total every dry hour. cffdrs-ng keeps a canopy total and resets
   it only after 5 dry hours.
3. It has no 0.5 mm FFMC canopy interception.

Fixing (1) alone still leaves FWI up to 5.8 off. Fixing all three reproduces the reference
exactly. Moving from cffdrs-ng's LST sunrise/sunset to the explorer's per-cell sun position
changes 2 of 2,623 hours and FWI by at most 0.05.

Confidence: well supported (exact agreement with the official output on the official data).

## 2. Classic daily FWI vs xclim: exact

`scripts/compare_daily_fwi_xclim.py` runs `aorc_tools.fire_indices.fwi_daily` and xclim 0.62.0
`cffwis_indices` on cffdrs-ng's `PRF2007_daily_wx.csv` (109 days, noon observations, no season
mask, start-up 85/6/15). FFMC, DMC, DC, ISI, BUI and FWI agree to better than 1e-5 on every day.
This is the form CWFIS, the Copernicus/GEFF ERA5 reanalysis, xclim and the Argonne study report.
The explorer computes it next to the hourly chain (step `FWI_daily`), once per cell at 12:00 LST.

## 3. Derived ERC/BI vs USFS Fire Lab NFDRS4 (NFDRS 2016): equations exact, moisture is the gap

`scripts/compare_with_nfdrs4.py --nfdrs4-csv all_G.csv` uses NFDRS4 `84223b9` (2026-10-16), built
from source. It runs on NFDRS4's sample RAWS record (241513, 2001–2017, 142,342 h, 46.8° N) with
NFDRS4's custom fuel model "C", which is the NFDRS-88 fuel model G parameter set
(`nfdrs4_init_fuel_model_G.txt`). Daily values are taken at the 13:00 obs hour, Apr–Oct.

| Our run | ERC Spearman | ERC mean abs Δ percentile | ERC top-10 % days caught | BI Spearman | BI mean abs Δ percentile |
|---|---|---|---|---|---|
| ERC/BI equations fed NFDRS4's own fuel moistures | **1.000** | 0.5 | 96.6 % | 0.997 | 1.2 |
| NFDRS4 dead moistures + our constant live (120/100 %) | 1.000 | 0.65 | 100 % | 0.995 | 1.5 |
| our dead moistures + NFDRS4 live | 0.902 | 10.1 | 63.3 % | 0.867 | 9.7 |
| **explorer default** (timelag dead + constant live) | 0.900 | 10.1 | 62.5 % | 0.865 | 9.7 |
| legacy EMC multipliers | 0.774 | 14.5 | 46.5 % | 0.824 | 11.8 |

Reading:
- The ERC/BI equations are right.
- Live-fuel moisture hardly matters for fuel model G.
- The whole gap comes from the dead-fuel moisture model. Our simple timelag relaxation, where all
  four classes chase the same EMC, does not match NFDRS4's Nelson (2000) stick model. Ranks still
  correlate (ρ ≈ 0.90), but individual days land about 10 percentile points off, and only about
  6 in 10 of NFDRS4's top-decile days are top-decile in ours.

Confidence: well supported for this station and fuel model. It is plausible, but not tested, that
the same holds on AORC in the WUP.

Option to close the gap: use Nelson dead-fuel moisture.
- NFDRS4 itself is public domain.
- Its `deadfuelmoisture.cpp` (Bevins 2005) is GPL-2.0-or-later.
- Porting it into this repo, or linking it into the executable, would bring GPL terms with it. This
  repo currently has no license. That is a project decision. Calling the unmodified NFDRS4 program as
  a separate tool is the low-friction alternative.

## Files

| File | What |
|---|---|
| `cffdrs_ng_PRF2007_comparison.{json,csv}` | statistics per run and variable |
| `cffdrs_ng_PRF2007_series.csv` | hour-by-hour FWI/DMC/DC, reference vs each run |
| `daily_fwi_vs_xclim_PRF2007.{json,csv}` | daily codes, ours vs xclim |
| `nfdrs4_comparison.json` | ERC/BI/dead-fuel-moisture agreement per run and basis |
| `nfdrs4_init_fuel_model_G.txt` | the NFDRS4 station configuration used (fuel model C = NFDRS-88 G) |
