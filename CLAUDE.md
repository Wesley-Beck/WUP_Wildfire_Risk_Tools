# CLAUDE.md — WUP Wildfire Risk Indices (NAPS 2026)

Source of truth for any Claude Code session (local, EC2, HPC, or web). Keep updated as
decisions change. Web-chat Claude = design/review; Claude Code = execution; this file + the
scripts/briefs/source in this repo are the shared context. New session? Read this, then
`docs/orchestration_brief.md`.

## Goal & deadline
Compute four fire-danger indices (FPI, FWI, ERC, BI) for Michigan's Western Upper Peninsula
(six WUPPDR counties: Baraga, Gogebic, Houghton, Iron, Keweenaw, Ontonagon + Isle Royale) at
800 m / hourly from NOAA AORC v1.1. Coverage: FWI/ERC/BI = full 1979–2026; FPI = 2000–2026
(MODIS-era NDVI, Option 1 — see below). NAPS 2026 manuscript (IEEE PES, 6 pp) is a
METHODS + DATA paper: methodology, motivation, and the computed dataset reported as regional
analysis, with all stipulations. Index validation vs observed fires and GLARM projection are
COMPANION papers. Deadline: July 1, 2026 (no resubmission).

## Repo contents
- `CLAUDE.md` (this file) · `README.md`
- `docs/orchestration_brief.md` — 5-step plan (verify → fix → resolve → compute → re-verify)
- `docs/manuscript_draft_v4.md` — current draft
- `scripts/verify_index_implementations.py` · `scripts/verify_computed_data.py` · `scripts/batch_compute_remaining_years.py`
- `src/` — program1 (extraction) + wildfire_indices module
- `aorc_tools/explorer/` — WUP Grid Explorer (`python -m aorc_tools.explorer`, `WUP_Explorer.bat`, one-file
  `WUP_Explorer.exe` from `.github/workflows/build-explorer.yml` + `packaging/wup_explorer.spec`): map selection,
  hourly chain with per-packet resolution tiers, NetCDF4 results store (int16+zlib, ~2 B/packet), resumable jobs,
  coverage %, save/share, known-fire checks (`fires.py`, Fires tab, `--fire-checks`). Tests: `tests/test_explorer.py`.
  Data side (2026-10-21): AORC input store + offline mode (`inputs.py`, `inputs/aorc`), NDVI ingest (`ndvi_ingest.py`:
  Planetary Computer / AppEEARS / NOAA CDR / local; QA 0/1; cell averaging; daily interp; envelope), climate-model
  adapter (`datasets.py`: regcm=GLARM, cf, wrf, era5), full-record verifier (`verify.py`, `--verify`), research bundle
  (`bundle.py`, `--bundle`; zip + SHA-256 manifest; workspace relocates itself), headless `--full-record`,
  Data tab. Tests: `tests/test_data_pipeline.py`.
  Robustness (2026-10-21, for day-long unattended runs on Wes's Windows 10 / 16 GB machine; USAGE.md "Long unattended
  runs"):
  - Concurrency and process safety:
    - ONE process-wide HDF5 lock (`hdf.py`; the netCDF4 wheel's HDF5 is not thread-safe → segfaults).
    - Workspace OS lock + logs/faulthandler (`runtime.py`).
  - Retries:
    - S3 read retries; job auto-retry with status `retrying`.
    - Permanent errors stop: OfflineMissing, layout, NoDataYet, disk.
    - Scheduler never dies; `run_until_settled` for headless runs.
    - Unattended runs never fall back to synthetic weather (exit 4).
  - Files:
    - Atomic tmp+fsync+replace with Windows retries.
    - tmp cleanup at start.
    - Quarantine + `--repair`.
  - Continuity: `chunk_state` = missing / stale / ok, via `prev_rowid` and a state comparison. NDVI-signature changes
    → recompute.
  - Limits and checks:
    - AORC data-end clamp.
    - Memory caps: workers ≤ RAM/6; live cap 2.5M cell-hours.
    - Exact histogram climatology.
    - Coverage cache.
  - Server: Origin/CORS check and JSON errors.
  - `--self-test` (`selftest.py`, `Run_Self_Test.bat`).
  - Tests: `tests/test_robustness.py`.
  Lines (2026-10-21; `lines.py`, `lines_ingest.py`, Lines tab, `--lines`, `--lines-download`; USAGE.md "Lines tab";
  `docs/lines_data_sources.md`):
  - Question: fire-weather danger along transmission lines, corridor vs region.
    - Wes's definition of risk = seasonality + intensity (local/global maxima and minima) and their increase.
    - Distribution lines: later, only if overlap is found (no public data). The advocacy write-up waits on those
      results.
  - Metrics per cell and year, built from daily maxima/minima files (`store.daily_year`, cached in
    `<ws>/daily`, rebuilt when a year's chunks change):
    - annual max;
    - spring/fall peaks and summer trough (15-day smoothed);
    - days above the regional P90/95/99 (Mar–Nov, pooled, exact histogram);
    - first/last P90 day and span;
    - peak hour;
    - nights that do not recover (daily min above the P90 of minima).
  - Trends: Theil–Sen + Mann–Kendall; Benjamini–Hochberg FDR for cell maps.
  - Line data:
    - Bundled layer = Wes's HIFLD clip (geometry only; the attributes were lost in the county join).
    - HIFLD Open shut down 2025-08-26. The final release (data 2024-09-30) is downloadable from the Esri archive
      (`hifld`) or the NETL mirror. OSM via Overpass (`osm`). `cross_attributes` fills owner/voltage gaps
      (WUP 69 kV owners are mostly NOT AVAILABLE in HIFLD).
  - Fire checks also compare whole series with the earlier real-AORC Pinery demo
    (`static/geo/reference/pinery-2009-earlier-run.json`).
  - Tests: `tests/test_lines.py`.
- `docs/future_glarm_plan.md` — futures with GLARM (GATED on a full-record PASS). Key facts: public GLARM-Proj1 has
  only daily lake temp/ice/T2m → need RegCM SRF fields from Xue's group; Yu et al. 2023 give NO weights (sensitivity,
  skill, 5.2 projections only) → weights must be built (W1 equal/agreement, W2 Yu Fig. 3, W3 WUP's own skill).
- `docs/validation/` + `scripts/compare_*.py` — this code vs official cffdrs-ng, xclim, NFDRS4 (see below).
  `docs/existing_tools_comparison.md` — gridMET, CEMS/GEFF, NFDRS4, FWI2025, USGS FPI, Argonne.
- `data/points/` — AORC points CSV + shapefiles (committed; small)
- `data/output/`, `data/cache/`, `data/raw/` — NOT in git (see .gitignore); local only

## Index implementation decisions (current)
- **FWI = xclim** (`xclim.indices.fire.cffwis_indices`, `drought_code`, `fire_season`). Validated
  (<3% vs Field et al. 2015); handles overwintering. Cite Wang et al. 2015; Cantin et al. 2014; xclim.
  Replace hand-rolled FWI; validate sample points both ways. Report native FWI AND its percentile.
- **FPI = recompute CORRECTLY (Burgan), MODIS-era 2000–2026 [OPTION 1 — LOCKED].** FWI/ERC/BI
  cover full 1979–2026; FPI covers 2000–2026 only, because validated 250–500 m satellite NDVI
  begins with MODIS (Terra) in 2000 (pre-2000 is only ~5 km AVHRR, which would break the 800 m
  premise). State this coverage difference explicitly in the paper — it is a feature, not a gap.
  Implementation: (a) NDVI = MODIS MOD13Q1 (16-day, 250 m; finer than 800 m so aggregate up),
  QA-screened; (b) build the per-pixel relative-greenness envelope `RG=(ND-ND_min)/(ND_max-ND_min)`
  from the 2000–2026 growing-season MODIS archive; (c) LANDFIRE fuel model (recent, applied across
  years) -> moisture of extinction + loadings (replaces create_default placeholders); (d) regrid
  MODIS + LANDFIRE onto the 800 m AORC grid (rioxarray/GDAL warp); (e) interpolate 16-day NDVI to
  daily, hold across hours — greenness term daily, EMC (T/RH) term hourly. Cite Burgan, Klaver &
  Klaver 1998. Real-NDVI FPI for the full record is NOT attempted here.
- **ERC / BI = DERIVED (simplified), reported as PERCENTILES, honestly labeled** (NOT cited as NFDRS).
  Stop clipping to 0-100 (they are open-ended). Confirmed by Wesley.

## FWI hourly chain (RESOLVED in the explorer 2026-10-20 — confirm for the batch record)
Checked against NRCan's official cffdrs-ng output (`NG_FWI.py` @ ed143d7, PRF2007 reference, 2,623 h;
`scripts/compare_with_cffdrs_ng.py`, `docs/validation/`):
- The legacy hourly port in `fwi.py` (used by `extract.py` for the existing 1979–2002 record) is wrong in three ways:
  - daily-system DMC/DC drying coefficients applied hourly;
  - rain total reset every dry hour;
  - no 0.5 mm FFMC interception.
- Effect: mean DMC 0.36 vs 17.45, mean DC 1,529 vs 136, FWI r = 0.62.
- `HourlyFWI(coefficients="cffdrs-ng")` now also implements cffdrs-ng's rain handling (canopy total reset after 5 dry
  hours, prec_effective for FFMC/DMC/DC). It reproduces the reference EXACTLY (< 1e-4). The explorer default is
  cffdrs-ng.
- Per-cell sun-position daylight (explorer) vs cffdrs-ng LST sunrise/sunset: FWI ≤ 0.05 difference.
- `extract.py` still defaults to legacy (`--fwi-coefficients`). DECIDE: recompute the batch record with cffdrs-ng
  (recommended; its FWI is not usable as is) and pin the commit.

Classic DAILY FWI is now also computed in the explorer (`fire_indices/fwi_daily.py`, step `FWI_daily`, 12:00 LST per
cell, 24 h rain to noon). It equals xclim `cffwis_indices` exactly (`scripts/compare_daily_fwi_xclim.py`), i.e. the
CWFIS/GEFF/Argonne-comparable form. Report both: hourly FWI2025 (novel) and classic daily (comparator).

## ERC/BI vs NFDRS 2016 (checked 2026-10-20, `scripts/compare_with_nfdrs4.py`)
Setup: NFDRS4 @ 84223b9, sample RAWS 2001–2017, fuel model G (NFDRS4 custom "C").
- Our ERC/BI EQUATIONS reproduce NFDRS4 when fed its moistures (ρ = 1.000 / 0.997).
- The explorer's timelag dead-fuel moisture gives fire-season ρ 0.90 (ERC) / 0.87 (BI) and ~10 percentile points
  mean difference; 62 % of NFDRS4 top-decile days are also top-decile here.
- Live moisture barely matters for G.
- So "derived, reported as percentiles" stands. To close the gap: Nelson dead-fuel moisture. NFDRS4's
  `deadfuelmoisture.cpp` is GPL-2.0+ (NFDRS4 itself public domain) and this repo has no license → licensing decision
  before porting/linking; calling the unmodified NFDRS4 CLI is the alternative.

## Known-fire checks (explorer Fires tab / `--fire-checks`)
- Seed catalog `aorc_tools/explorer/static/geo/wup_fires.geojson`: Pinery 2009 (DNR/NIFC; carries the earlier real-run
  reference values), Summit Lake 2024 (WFIGS, 328 ac, natural), three small WFIGS fires, three Oct-2026 fires without
  published coordinates.
- Import NIFC/MTBS/DNR files for more.
- What a check reports: discovery-day values at 3×3 cells, plus the percentile rank among the same Apr–Oct season's
  days (needs that season saved: one job).
- These are case checks, not skill (companion paper).
- This cloud workspace cannot reach S3 (proxy 403), so real values come from Wes's machine: `Run_Fire_Checks.bat`.

## Yu et al. 2023 (Argonne) — check before citing
Per the paper text:
- Historical indices used gridMET ~4 km (12 km was the WRF future run).
- Fire-size correlations were weak (ρ < 0.32 at grid/daily) but positive and significant; regional values only in
  Fig. 3.
- The draft's "computed at 12 km" and "reportedly negative" need checking against Fig. 3.

## Percentile methodology (for ERC/BI; optional for FWI/FPI cross-comparison)
Raw per-year index values are the canonical durable outputs; percentiles are a cheap post-pass
computed ONCE after the full raw record exists (re-run if 2026 updates arrive).
For each cell, replace each daily value by its empirical percentile in the chosen reference
distribution. Three choices to STATE in the paper:
1. Reference pool: pooled-regional (preserves spatial differences) vs per-cell (normalizes spatial
   bias but erases "where is worst"). Default = pooled-regional for the spatial figures.
2. Reference window: whole-season (keeps seasonal cycle; for climatology figure) vs day-of-year
   ±15 d (gives anomalies; for outlier panel). Use both for their respective figures.
3. Reference period: full 1979-2026 (spans AORC source-stream changes -> disclose) vs a homogeneous
   sub-period. State which. NOTE: ERC/BI percentiles use 1979-2026; FPI percentiles (if reported) use
   2000-2026 — different N. Any inter-index agreement comparison that includes FPI MUST be restricted
   to the 2000-2026 overlap window so it is apples-to-apples.
Percentiles make open-ended/derived indices fairly REPORTABLE and put all four on a common 0-100
scale for inter-index comparison. They are NOT validation and do NOT make a simplified index correct.

## AORC stipulations (must appear in the paper)
- Effective resolution differs by variable: precip & temperature true ~800 m, well-validated (incl.
  Great Lakes, Hong et al. 2022; temp within ~2 °C). 10 m wind & shortwave are NLDAS-2 (~12 km)
  bilinearly interpolated; humidity NLDAS-2-derived. Refinement over Argonne (12 km) genuine for
  precip/temp, partial for wind/humidity.
- Not temporally homogeneous: precip source streams change by era; non-precip methodology breaks ~end-2015.
- Apr 2, 2026 AWS advisory: ~0.04% rows incorrectly masked; Zarr regenerated. Use corrected data; record version.

## Pipeline (see orchestration brief)
1. Verify existing 1979-2002 (laptop OK; data is local). Report ground truth FIRST.
2. Fix FWI -> xclim.
3. Resolve FPI (Burgan, MODIS NDVI 2000-2026 per Option 1) + ERC/BI (derived + percentile).
   SEQUENCING: FWI/ERC/BI are ready now — run their full 1979-2026 record first; build the FPI
   NDVI/LANDFIRE pipeline in parallel and slot FPI (2000-2026) in last, so the NDVI work doesn't
   block the bulk of the dataset before the deadline.
4. Compute 1979-2026 year-by-year with the finalized implementation (recompute 1979-2002 if its path
   differs). Atomic writes; per-year QA; cache raw downloads separately from outputs.
5. Re-verify full record (every file PASS; no uniform fields; ERC/BI not clipped; FWI continuity
   across year boundaries; full coverage).

## Run safely, year-by-year (this is I/O-bound, not FLOPS-bound)
batch_compute_remaining_years.py: skips done years (resume), chains FWI state, logs, checks disk.
Add: atomic CSV writes (write .tmp then os.replace), per-year verify call, raw-download cache so a
recompute doesn't re-pull S3. Canonical outputs = raw per-year index values; everything else derived.
Compute next to the data: EC2 in the AORC bucket region (NOAA Open Data ~us-east-1 — confirm), or
MTU HPC (check node S3 access), or Claude Code web (verify S3 network access; 30 GB disk -> year-by-year).
Fetch only needed variables/hours (solar-noon + 24-h precip sum for daily indices).

## Citations to verify (essentials)
Fall et al. 2023 (AORC, JAWRA, 10.1111/1752-1688.13143); Van Wagner 1987 / Van Wagner & Pickett 1985 (FWI);
Wang et al. 2015 + Cantin et al. 2014 + xclim (FWI impl); Burgan, Klaver & Klaver 1998 (FPI — once real NDVI);
Heinsch et al. 2009 + NWCG PMS 437 (ERC/BI percentiles, Lake States BUI); Deeming et al. 1977 / Cohen &
Deeming 1985 (NFDRS — only if canonical, not used for derived ERC/BI); Alexander & de Groot 1988 (FWI jack
pine); Hong et al. 2022 (AORC Great Lakes); Eidenshink et al. 2007 (MTBS); Lin et al. 2024 (geographic bias);
Argonne CONUS multi-index study (confirm; poss. Earth's Future 2023, 10.1029/2023EF003823); GLARM reference.
