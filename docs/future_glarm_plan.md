# Future wildfire risk with GLARM: plan (groundwork only)

**Status (2026-10-21).** The tool can already read climate-model output, regrid it to the 800 m cells, run the same
four FDIs, store the results apart from the historical record, and verify them (`aorc_tools/explorer/datasets.py`,
Data tab, tested on synthetic RegCM- and WRF-shaped files). Everything else below waits for the gate.

**Gate.** Futures start only when the historical record is finished and trusted:
- `--verify full` PASSes for the whole AORC domain (1979-02-01 → domain end), every brick;
- the known-fire checks look sensible;
- the batch-record decisions in `CLAUDE.md` are closed (cffdrs-ng FWI; ERC/BI as percentiles).

Labels used here: **[well supported]** = stated in the cited source or verified in code; **[plausible]** = reasoned
but not yet checked against data; **[decision]** = Wes's call.

---

## 1. GLARM: what it is and what it provides

**The model** [well supported]
- Source: Xue et al. 2022, *GMD* 15, 4425–4446.
- GLARM = RegCM4 atmosphere (18 km) two-way coupled with the FVCOM 3-D lake and ice model (1–4 km lake grid).
- Forcing: three CMIP5 GCMs (IPSL-CM5A-MR, MPI-ESM-MR, GISS-E2-H) under RCP4.5 and RCP8.5.
- Periods: early century 2000–2019 (the baseline), mid century 2030–2049, late century 2080–2099.

**GLARM-Proj1** [well supported]
- A continuous 1981–2099 projection, released December 2024 on Digital Commons @ Michigan Tech.
- The public release holds daily 3-D lake temperature, lake ice cover and 2 m air temperature.

**The gap that matters for this project** [well supported]
- The public files do **not** include humidity, wind, precipitation or radiation, and the FDIs need all of them.
- So the FDI work needs RegCM's near-surface atmospheric output ("SRF" fields) from Dr. Xue's group (§7, Q1).

**Why 2000–2019 is convenient** [well supported]
- GLARM's baseline is exactly 2000–2019, and AORC covers it hourly at 800 m.
- So model-vs-observation comparison and bias correction have a clean 20-year overlap.

## 2. What the four FDIs need from GLARM

| AORC input (tool name) | RegCM / CORDEX name | Used by | Notes |
|---|---|---|---|
| 2 m temperature (`TMP_2maboveground`) | `tas` | all four | |
| 2 m specific humidity (`SPFH_2maboveground`) | `qas` / `huss` (or `hurs`) | all four | RH is derived from q, T and p |
| surface pressure (`PRES_surface`) | `ps` | RH conversion | |
| 10 m wind (`UGRD/VGRD_10maboveground`) | `uas`, `vas` (or `sfcWind`) | FWI (ISI), BI | |
| precipitation (`APCP_surface`) | `pr` (flux) | all four | |
| shortwave radiation (`DSWRF_surface`) | `rsds` | not used by the indices | traced only |

**Time step**
- Hourly or 3-hourly runs straight through the hourly chain. The tool interpolates states linearly to hourly and
  spreads precipitation over its accumulation period [well supported, tested].
- Daily-only output cannot run the hourly chain. The tool now flags any dataset coarser than 6-hourly
  (status `warning`). For daily data, see option D in §4.
- RegCM commonly writes SRF fields 3-hourly [plausible — confirm with Dr. Xue's group].

**Data volume** [plausible]
- Only a WUP subset with a margin is needed: about 30 × 30 model cells at 18 km.
- Seven variables, 3-hourly, for 3 GCMs × 2 RCPs × 3 periods: a few GB at most.

## 3. What the tool already does (tested)

**Dataset adapter** (`datasets.py`)
- Presets: `regcm` (GLARM), `cf`, `wrf`, `era5`, `aorc`.
- Reads 1-D or curvilinear 2-D lat/lon and cftime calendars (`noleap`, `360_day`).
- Regridding: bilinear for regular grids; inverse-distance (4 neighbours) for curvilinear model grids.
- Converts units, handles accumulated or flux precipitation, and rotates WRF winds.

**Storage and checks**
- Results live under their own source name (`ds-<slug>`), never mixed with the AORC record.
- The verifier, fire checks and exports all work per source.
- Round trip: an AORC-shaped file through the adapter reproduces the AORC values exactly.

**NDVI for future FPI**
- Tier 4 (day-of-year MODIS climatology, opt-in with "allow non-MODIS NDVI") gives future FPI a fixed greenness
  cycle (§5).

## 4. Bias correction (to build after the gate) [decision]

GLARM's 18 km weather differs from AORC's 800 m weather; raw model FDIs carry those biases. Options:

**A. Delta change on AORC** (recommended primary) [plausible]
- Method:
  - For each GCM × RCP × period, compute per cell and calendar month the change from GLARM 2000–2019.
  - Temperature: additive change. Humidity, wind and precipitation: multiplicative change.
  - Apply the changes to the AORC 2000–2019 hourly series, then run the unchanged pipeline.
- Pros:
  - Keeps AORC's 800 m detail and hourly structure.
  - Future FDIs are directly comparable with the historical record.
- Cons:
  - Keeps today's weather sequences, so changes in dry-spell length (which drive DMC/DC) are only partly
    represented.

**B. Quantile delta mapping** (Cannon et al. 2015) [plausible]
- Method: correct the GLARM series per cell, month and quantile against AORC 2000–2019, preserving the model's
  projected change in each quantile.
- Pros: keeps the model's own sequences, including drought persistence.
- Cons:
  - Spatial detail below 18 km comes only from the correction.
  - More moving parts, and precipitation needs wet-hour frequency adjustment.

**C. Raw model**
- Use only to quantify how much the correction matters.

**D. Daily-only contingency** (if no sub-daily output exists)
- Disaggregate each day with the cell's mean AORC 2000–2019 diurnal cycle for that month (temperature, humidity,
  wind), and place daily precipitation by AORC's hourly wet-hour climatology.
- Or compute only the daily forms: classic daily FWI, daily ERC/BI, daily-Burgan FPI. The tool has these.

**Suggestion**
- Run A as the main result and B as a sensitivity test. The A–B difference is a structural uncertainty to report.

**Prerequisite either way**
- Run GLARM 2000–2019 through the tool and compare its FDIs with the AORC FDIs for the same years: distributions,
  seasonal cycle, 95th percentiles.
- This is cheap with the adapter and tells you how much correction is needed.

## 5. NDVI and FPI in the future

**What Yu et al. did** [well supported]
- They held NDVI fixed at 1995–2004 satellite values for present-day and future FPI.
- They note this "might reduce the sensitivity" of projected fire potential.

**The equivalent here**
- MODIS day-of-year climatology (2000–2019), tier 4.
- Future FPI then changes only through dead-fuel moisture (temperature, humidity). Say so in the paper.

**A greenness-trend scenario is possible but speculative**
- Example: a longer growing season shifts the NDVI cycle.
- Keep it as a sensitivity test only.

## 6. Argonne (Yu et al. 2023): what it gives and what it does not

Paper: Yu, Feng, Wang & Wright (2023), *Earth's Future* 11, e2023EF003823. All points below are
[well supported] (paper text and tables).

**Setup**
- Historical: four FDIs, 1984–2019, at ~4 km from gridMET; fires from MTBS (13,353 wildfires).
- Future: WRF at 12 km, bias-corrected CCSM4, RCP8.5, 1995–2004 vs 2085–2094.

**Projected change for ecoregion 5.2 (Mixed Wood Shield)**
- Most of the WUP falls in Level II ecoregion 5.2 (Level III 50, Northern Lakes and Forests).
- Change from present-day to 2085–2094, in % (Table 2):

  | Index | Mean | σ | 95th percentile |
  |---|---|---|---|
  | FWI | +42 | +52 | +26 |
  | FPI | +6 | +3 | +1 |
  | ERC | +8 | +4 | +2 |
  | BI | +4 | +4 | +3 |

- The north-eastern regions show their largest increases in fall.
- Historical 5.2 trends (Table 1): FWI and FPI means rise, ERC mean falls significantly, and fire size shows an
  insignificant decrease.

**Which inputs drive each index** (sensitivity, Fig. 2)
- RHmin is the first or second most dominant input for all four FDIs.
- Daily precipitation dominates ERC and BI.
- Wind matters mainly above the 95th percentile.
- Eastern CONUS (which includes the WUP) is most sensitive to:
  - NDVI for FPI;
  - Tmax for FWI;
  - daily precipitation for ERC/BI.

**How well the indices track fire** (Fig. 3)
- Correlations with fire size are weak at grid-daily scale (ρ < 0.32), stronger when aggregated, and all
  significant.
- BI is slightly best at all scales.
- Regional values appear only in Fig. 3 (panels e–h).

**What it does not give: weights**
- There is no weighted or combined index.
- The only combination is the average number of days above each index's 95th percentile across the four FDIs.

So "weights informed by Argonne" has to be constructed. Options [decision]:

| Option | Weights from | Pros | Cons |
|---|---|---|---|
| W1 | Equal weights on percentiles, plus a count of how many FDIs exceed their 95th percentile (Yu-style) | Transparent, comparable with Yu | Ignores skill differences |
| W2 | Yu Fig. 3 regional-monthly ρ for ecoregion 5.2 | Directly "Argonne-informed" | Has to be read off a figure; CONUS-scale method, 4 km data |
| W3 (suggested) | This record's own skill in the WUP: each FDI percentile vs WUP fires (WFIGS/DNR/MTBS), e.g. AUC of fire days vs non-fire days, or ρ with log fire size at county-month; `w_i ∝ max(0, skill_i − skill_random)`, bootstrap CIs | Local, defensible, a contribution of the PhD | Small fire sample in the WUP; needs the full record first |

**Sensitivity-informed interpretation (independent of weights)**
- Repeat Yu's Fig. 2 analysis for the WUP: perturb each input and record each FDI's response.
- The tool's trace already exposes every intermediate.
- This tells which climate variable drives each FDI here. Paired with GLARM's projected change in those variables,
  it explains why each FDI changes (e.g. Tmax ↑ and RHmin ↓ → FWI rises most).

**Caveat** [plausible]
- Skill-based weights assume the index–fire relation of 1979–2026 holds in the future (stationarity). State it.

## 7. Questions for Wes / Dr. Xue

1. **Atmospheric output.** Can the group share RegCM4 SRF near-surface fields for the WUP subset with a margin?
   - Fields: `tas`, `qas`/`huss` (or `hurs`), `ps`, `uas`/`vas` (or `sfcWind`), `pr`, `rsds`.
   - Time step: hourly or 3-hourly.
   - Runs: 3 GCMs × RCP4.5/8.5, for 2000–2019, 2030–2049 and 2080–2099 (or GLARM-Proj1's continuous 1981–2099).
   - Also: file format, calendar, grid file (`xlat`/`xlon`).
2. **If only daily output exists:** which daily variables (Tmax, Tmin, RH or q, precipitation, mean/max wind)?
   That decides between option D and daily-only indices.
3. **Combined index or separate?** A weighted combined index (W1/W2/W3), or the four FDIs kept separate with
   Argonne-informed interpretation? Both are possible; the weights need the full record first.
4. **Bias correction:** delta change (A) as primary with QDM (B) as sensitivity, or the reverse?

## 8. Work items after the gate

| # | Item | Where |
|---|---|---|
| 1 | Register GLARM files as datasets (Data tab → Add dataset, preset `regcm`); check variables, time step, calendar | exists |
| 2 | GLARM 2000–2019 vs AORC 2000–2019: FDI distributions, seasonal cycle, 95th percentiles, per cell | new comparison script on top of `store.series` |
| 3 | Delta-change and QDM corrections as dataset transforms (a `bias_correction` block in the dataset JSON, monthly per-cell factors stored in the workspace) | `datasets.py` |
| 4 | Future runs per GCM × RCP × period; verification per source | jobs + `verify.py` (exists) |
| 5 | Change products: Δmean, Δ95th, days above the historical 95th, fire-season length, ensemble mean and spread, agreement count | new `futures.py` + a Futures tab |
| 6 | WUP skill per FDI (needs the full record and fire catalog) → W3 weights with CIs | `fires.py` + new script |
| 7 | WUP sensitivity analysis (Yu Fig. 2 analogue) | uses the chain's trace |

## Sources
- Xue, P., Ye, X., Pal, J. S., Chu, P. Y., Kayastha, M. B., Huang, C. (2022). Climate projections over the Great
  Lakes Region: using two-way coupling of a regional climate model with a 3-D lake model. *GMD* 15, 4425–4446.
  https://doi.org/10.5194/gmd-15-4425-2022
- GLARM-Proj1 dataset on Digital Commons @ Michigan Tech (Dec 2024):
  https://blogs.mtu.edu/library/2024/12/05/glarm-proj1-dataset-on-digital-commons-michigan-tech/ ;
  ASLO 2025 abstract:
  https://aslo.secure-platform.com/2025/solicitations/16/sessiongallery/1937/application/9393
- Yu, G., Feng, Y., Wang, J., Wright, D. B. (2023). Performance of fire danger indices and their utility in
  predicting future wildfire danger over the conterminous United States. *Earth's Future* 11, e2023EF003823.
  https://doi.org/10.1029/2023EF003823
- Cannon, A. J., Sobie, S. R., Murdock, T. Q. (2015). Bias correction of GCM precipitation by quantile mapping:
  how well do methods preserve changes in quantiles and extremes? *J. Climate* 28, 6938–6959.
