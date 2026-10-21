# R2I2 Winter Resilient Electric Power Systems — Wildfire Risk Tools

Wildfire risk analysis tools for the **Western Upper Peninsula of Michigan**
(Houghton, Keweenaw, Ontonagon, Gogebic, Iron, Baraga counties + Isle Royale),
built as part of the R2I2 Winter Resilient Electric Power Systems project.

## What This Does

Extracts hourly weather data from NOAA's Analysis of Record for Calibration
(AORC) dataset at ~800m resolution and computes four fire danger indices
matching Argonne National Laboratory's methodology:

| Index | System | What It Measures |
|-------|--------|-----------------|
| **FWI** | Canadian Fire Weather Index | Overall fire intensity (weather-driven) |
| **ERC** | NFDRS Energy Release Component | Energy available per unit area |
| **BI** | NFDRS Burning Index | Difficulty of fire suppression |
| **FPI** | Fire Potential Index (Burgan 1998) | Vegetation + weather fire potential |

Every intermediate calculation step is saved as a separate CSV file for
full transparency and researcher access.

## Quick Start

```bash
pip install -e .

# Generate point index from study area shapefile
aorc-tools points --shapefile data/shapefiles/Western_Upper_Peninsula.shp --output ./data/output

# Extract a month of data
aorc-tools extract --points ./data/output/points_index.csv --year 2020 --start-month 7 --end-month 7

# View results on a map (legacy Leaflet viewer of the batch CSV/bin files)
cd website && python -m http.server 8000

# WUP Grid Explorer: map, hourly FWI2025 + daily FWI, FPI, ERC, BI for any cell and hours,
# every step traced, results saved, known-fire checks. Double-click WUP_Explorer.bat
# (or WUP_Explorer.exe from the "Build WUP Explorer" GitHub Action), or:
python -m aorc_tools.explorer                 # real AORC if reachable, local store offline, otherwise demo weather
python -m aorc_tools.explorer --fire-checks   # headless: compute fire seasons, check known fires, write reports
python -m aorc_tools.explorer --full-record   # headless: NDVI if missing -> whole record (resumable) -> full verification
python -m aorc_tools.explorer --bundle D:/WUP_FDI_bundle.zip   # one zip: program + inputs + NDVI + results + manifest
```

The explorer's **Data** tab covers the AORC input store (offline use), NDVI ingest (Planetary Computer,
AppEEARS, NOAA CDR, local rasters), climate-model datasets (GLARM/RegCM, CMIP/CORDEX, WRF, ERA5), record
verification and research bundles. Future-climate plan: `docs/future_glarm_plan.md`.

See **[USAGE.md](USAGE.md)** for the complete user guide.

## Project Structure

```
aorc_tools/              # Python package — fire index calculations
  fire_indices/          # FWI, ERC/BI, FPI implementations
  explorer/              # WUP Grid Explorer: chain, results store, jobs, fire checks, Flask API + static app
                         #   inputs.py (AORC input store), ndvi_ingest.py, datasets.py (climate models),
                         #   verify.py (record verifier), bundle.py (research bundle), tasks.py (data tasks)
  cli.py                 # Command-line interface
  extract.py             # Hourly extraction pipeline
  config.py              # AORC constants, NFDRS fuel models (A-Z)
website/                 # Map interfaces
  explorer.html          # pointer to the explorer (now served by python -m aorc_tools.explorer)
  index.html             # Legacy Leaflet viewer of batch outputs
  js/, css/              # Legacy viewer code
scripts/
  build_percentile_climatology.py   # percentile reference (CLAUDE.md post-pass)
  compare_with_cffdrs_ng.py         # hourly FWI vs NRCan cffdrs-ng official output
  compare_daily_fwi_xclim.py        # classic daily FWI vs xclim
  compare_with_nfdrs4.py            # ERC/BI vs USFS NFDRS4
docs/validation/         # results of those comparisons
docs/existing_tools_comparison.md   # gridMET, CEMS/GEFF, NFDRS4, FWI2025, USGS FPI, Argonne
docs/future_glarm_plan.md           # GLARM futures, bias correction, Argonne-informed weighting (after the gate)
packaging/               # PyInstaller spec for the one-file WUP_Explorer executable
WUP_Explorer.bat / .command, wup_explorer.sh, Run_Fire_Checks.bat, Run_Full_Record.bat   # double-click launchers
data/
  shapefiles/            # Study area boundary (includes Isle Royale)
  output/                # Extracted CSV results (git-ignored)
tests/                   # Validation against Argonne & cffdrs-ng
```

## Data Sources

- **AORC v1.1**: NOAA S3 bucket `noaa-nws-aorc-v1-1-1km` (public, ~800m hourly, 1979-present)
- **Fire indices**: Validated against Argonne National Laboratory code (Yu & Feng 2023)
- **Fuel models**: NFDRS fuel model G default (dense conifer), all 22 models supported
- **NDVI** (for FPI): NASA MODIS/VIIRS (optional, see USAGE.md)

## References

- Fall et al. 2023: AORC v1.1 methods (JAWRA)
- Yu & Feng 2023: Characterizing compound wildfire and heat wave events (AGU Earth's Future)
- Van Wagner & Pickett 1985: Canadian FWI System
- Bradshaw et al. 1984: NFDRS fuel models (USFS INT-169)
- Burgan et al. 1998: Fire Potential Index
- Yu, Feng, Wang & Wright 2023: Performance of fire danger indices and their utility in predicting future wildfire
  danger over the CONUS (Earth's Future 11, e2023EF003823)
- Xue et al. 2022: GLARM climate projections over the Great Lakes region (GMD 15, 4425-4446)
