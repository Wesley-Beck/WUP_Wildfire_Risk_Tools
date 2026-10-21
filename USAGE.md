# AORC Wildfire Risk Analysis System — Usage Guide

A complete toolkit for computing hourly fire danger indices from NOAA's
Analysis of Record for Calibration (AORC) dataset, focused on the
Western Upper Peninsula of Michigan.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Quick Start](#quick-start)
4. [CLI Reference](#cli-reference)
5. [Output File Guide](#output-file-guide)
6. [Fire Danger Index Descriptions](#fire-danger-index-descriptions)
7. [Fuel Model Selection](#fuel-model-selection)
8. [Fuel Moisture Methods](#fuel-moisture-methods)
9. [Adding NDVI Data for FPI](#adding-ndvi-data-for-fpi)
10. [Grid Explorer (aorc-tools serve)](#grid-explorer-aorc-tools-serve)
11. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Python** >= 3.9
- **Internet access** for AORC S3 data (bucket is public, no AWS credentials needed)
- **Study area shapefile** — a boundary polygon in `.shp`, `.gpkg`, or `.geojson` format
  - The Western Upper Peninsula shapefile is included at `data/shapefiles/Western_Upper_Peninsula.shp`
  - Includes full Isle Royale coverage (Keweenaw County)
- **RAM**: ~2 GB recommended for ~15,000 grid points
- **Disk**: ~500 MB per year of extracted data (all intermediates)

---

## Installation

```bash
# Clone the repository
git clone https://github.com/wesley-beck/r2i2_winter_resiliance_tools_and_website.git
cd r2i2_winter_resiliance_tools_and_website

# Install as editable package (creates the aorc-tools command)
pip install -e .
```

This installs all dependencies (xarray, zarr, s3fs, geopandas, shapely, etc.)
and registers the `aorc-tools` CLI command.

Verify installation:
```bash
aorc-tools status
```

---

## Quick Start

Three commands take you from shapefile to fire danger data:

### Step 1: Generate point index

This identifies which AORC grid points fall within your study area boundary.

```bash
aorc-tools points \
  --shapefile data/shapefiles/Western_Upper_Peninsula.shp \
  --output ./data/output
```

Output: `data/output/points_index.csv` — a table of ~15,000 land grid points
with their coordinates and AORC array indices.

### Step 2: Extract data and compute indices

This fetches hourly AORC weather data from S3 and computes all fire danger
indices for every point, every hour.

```bash
aorc-tools extract \
  --points ./data/output/points_index.csv \
  --year 2020 \
  --output ./data/output
```

This will take several hours for a full year (8,760 hours x ~15,000 points).
You can extract a single month to test:

```bash
aorc-tools extract \
  --points ./data/output/points_index.csv \
  --year 2020 \
  --start-month 7 --end-month 7 \
  --output ./data/output
```

### Step 3: Browse results

Open the website to view results on a map:

```bash
cd website
python -m http.server 8000
# Then open http://localhost:8000 in your browser
```

Or load any CSV directly in Python, R, or Excel:

```python
import pandas as pd
fwi = pd.read_csv("data/output/2020/07/cfwi/2020_07_FWI.csv", index_col=0)
# Rows = point_id, Columns = "YYYY-MM-DD HH:MM"
print(fwi.describe())
```

---

## CLI Reference

### `aorc-tools points`

Generate AORC grid point index from a study area boundary.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--shapefile` | Yes | — | Path to boundary file (.shp, .gpkg, .geojson) |
| `--output` | Yes | — | Output directory for `points_index.csv` |
| `--filter-water` / `--no-filter-water` | No | `--filter-water` | Exclude large water bodies (Lake Superior, etc.) via OpenStreetMap |
| `--water-min-area` | No | `10.0` | Minimum water body area (km^2) to exclude |

**Output**: `{output}/points_index.csv` with columns:
- `point_id` — unique integer identifier
- `latitude` — WGS84 latitude
- `longitude` — WGS84 longitude
- `lat_idx` — index into AORC latitude array (for fast data access)
- `lon_idx` — index into AORC longitude array

### `aorc-tools extract`

Extract hourly AORC data and compute fire danger indices for all points.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--points` | Yes | — | Path to `points_index.csv` from the `points` command |
| `--year` | Yes | — | Year to extract (e.g., 2020) |
| `--output` | No | `./data/output` | Base output directory |
| `--start-month` | No | `1` | First month to extract (1-12) |
| `--end-month` | No | `12` | Last month to extract (1-12) |
| `--fuel-model` | No | `G` | NFDRS fuel model code (A-Z, see [Fuel Model Selection](#fuel-model-selection)) |
| `--fuel-moisture` | No | `emc` | Fuel moisture method: `emc` (equilibrium) or `nelson` (timelag carry-forward) |
| `--latitude` | No | `46.5` | Representative latitude for FWI sunrise/sunset calculations |

### `aorc-tools status`

Check AORC S3 connectivity and report grid dimensions. No arguments.

### `aorc-tools serve`

Start the grid explorer (JSON API + `website/explorer.html`).

| Flag | Default | Description |
|------|---------|-------------|
| `--source` | `synthetic` | `synthetic` = demo weather, no network; `aorc` = NOAA AORC v1.1 on S3 |
| `--points` | `data/output/points_index.csv` | Point index |
| `--output` | `data/output` | Where the batch record (`data_{year}_{month}.db`) lives |
| `--static-dir` | `data/static` | FPI NDVI inputs (see [Adding NDVI Data](#adding-ndvi-data-for-fpi)) |
| `--climatology` | `data/climatology/percentiles.json` | Percentile reference built by `scripts/build_percentile_climatology.py` |
| `--cache-dir` / `--cache-max-gb` | `data/cache/bricks` / `8` | On-disk cache of fetched AORC bricks (LRU) |
| `--max-cells` / `--max-hours` | `12000` / `1488` | On-demand compute limits per request |
| `--host` / `--port` | `127.0.0.1` / `8765` | Bind address |

---

## Output File Guide

Each extraction produces monthly CSV files organized in subdirectories.
**Every intermediate calculation step is saved as a separate CSV** so
researchers can inspect and verify any part of the pipeline.

### Directory Structure

```
data/output/{year}/{month}/
  raw_aorc/                          # Raw AORC values in native units
    {year}_{month}_TMP_2maboveground.csv     # Temperature (K)
    {year}_{month}_SPFH_2maboveground.csv    # Specific humidity (kg/kg)
    {year}_{month}_PRES_surface.csv           # Pressure (Pa)
    {year}_{month}_UGRD_10maboveground.csv   # U-wind (m/s)
    {year}_{month}_VGRD_10maboveground.csv   # V-wind (m/s)
    {year}_{month}_APCP_surface.csv           # Precipitation (mm)
    {year}_{month}_DSWRF_surface.csv          # Shortwave radiation (W/m^2)
    {year}_{month}_DLWRF_surface.csv          # Longwave radiation (W/m^2)

  converted/                         # Unit-converted weather variables
    {year}_{month}_temperature_c.csv          # Temperature (Celsius)
    {year}_{month}_temperature_f.csv          # Temperature (Fahrenheit)
    {year}_{month}_relative_humidity.csv      # Relative humidity (%)
    {year}_{month}_wind_speed_ms.csv          # Wind speed (m/s)
    {year}_{month}_wind_speed_kph.csv         # Wind speed (km/h)
    {year}_{month}_wind_speed_mph.csv         # Wind speed (mph)
    {year}_{month}_precipitation_mm.csv       # Precipitation (mm)

  cfwi/                              # Canadian Fire Weather Index system
    {year}_{month}_FFMC.csv                   # Fine Fuel Moisture Code (0-101)
    {year}_{month}_DMC.csv                    # Duff Moisture Code
    {year}_{month}_DC.csv                     # Drought Code
    {year}_{month}_ISI.csv                    # Initial Spread Index
    {year}_{month}_BUI.csv                    # Buildup Index
    {year}_{month}_FWI.csv                    # Fire Weather Index (final)

  nfdrs/                             # National Fire Danger Rating System
    {year}_{month}_FM1.csv                    # 1-hour fuel moisture (%)
    {year}_{month}_FM10.csv                   # 10-hour fuel moisture (%)
    {year}_{month}_FM100.csv                  # 100-hour fuel moisture (%)
    {year}_{month}_FM1000.csv                 # 1000-hour fuel moisture (%)
    {year}_{month}_SC.csv                     # Spread Component (ft/min)
    {year}_{month}_ERC.csv                    # Energy Release Component (BTU/ft^2)
    {year}_{month}_BI.csv                     # Burning Index

  fpi/                               # Fire Potential Index (requires NDVI)
    {year}_{month}_RG.csv                     # Relative Greenness (0-1)
    {year}_{month}_EMC.csv                    # Equilibrium Moisture Content (%)
    {year}_{month}_FM10_fpi.csv               # 10-hr fuel moisture, FPI method (%)
    {year}_{month}_FPI.csv                    # Fire Potential Index (0-100)

  state/                             # Carry-forward state for continuity
    fwi_state_{year}_{month}.json             # FFMC, DMC, DC per point
    nfdrs_state_{year}_{month}.json           # Fuel moisture per point

  metadata_{year}_{month}.json       # Full calculation parameters
```

### CSV Format

- **Rows**: Indexed by `point_id` (matches `points_index.csv`)
- **Columns**: Datetime strings in format `YYYY-MM-DD HH:MM` (hourly)
- **Values**: Floating-point numbers for the variable at that point and time

Example (FWI for July 2020):
```
point_id, 2020-07-01 00:00, 2020-07-01 01:00, ..., 2020-07-31 23:00
0,        2.34,              2.18,              ..., 5.67
1,        2.41,              2.25,              ..., 5.82
...
```

---

## Fire Danger Index Descriptions

### Canadian Fire Weather Index (CFWI) System

The FWI system tracks moisture content in three fuel layers and combines
them into fire behavior indicators. Higher values = greater fire danger.

| Component | What It Measures | Range | Key Inputs |
|-----------|-----------------|-------|------------|
| **FFMC** | Surface litter dryness (1-2 cm deep) | 0-101 | Temp, RH, wind, rain |
| **DMC** | Organic layer dryness (5-10 cm deep) | 0+ | Temp, RH, rain |
| **DC** | Deep drought severity (10-20 cm deep) | 0+ | Temp, rain |
| **ISI** | Expected fire spread rate | 0+ | Wind speed + FFMC |
| **BUI** | Fuel available for combustion | 0+ | DMC + DC |
| **FWI** | Overall fire intensity | 0+ | ISI + BUI |

**How to interpret FWI values** (approximate ranges for northern forests):

| FWI Range | Fire Danger Level |
|-----------|------------------|
| 0-5 | Low |
| 5-10 | Moderate |
| 10-20 | High |
| 20-30 | Very High |
| 30+ | Extreme |

### NFDRS Indices (ERC, SC, BI)

The National Fire Danger Rating System uses detailed fuel models to
estimate fire behavior. Different fuel models produce different value ranges.

| Index | What It Measures | Units | Interpretation |
|-------|-----------------|-------|----------------|
| **ERC** | Total energy available per unit area if the fire front passes | BTU/ft^2 | Higher = more intense fire. Climate-driven (no wind). |
| **SC** | Rate of forward fire spread | ft/min | Higher = faster spread. Wind-dependent. |
| **BI** | Combined measure of fire difficulty to suppress | dimensionless | BI = 3.01 x (SC x ERC)^0.46. Higher = harder to control. |

ERC is particularly useful for tracking **seasonal trends** — it rises
slowly through dry periods and drops sharply with sustained rain.

### Fire Potential Index (FPI)

FPI combines satellite vegetation data (NDVI) with weather to assess
fire potential. It accounts for both fuel condition (green vs. cured)
and fire weather.

| Value Range | Meaning |
|-------------|---------|
| 0-25 | Low potential |
| 25-50 | Moderate potential |
| 50-75 | High potential |
| 75-100 | Very high potential |

FPI requires NDVI satellite data. Without NDVI, FPI outputs will be NaN.
See [Adding NDVI Data for FPI](#adding-ndvi-data-for-fpi).

---

## Fuel Model Selection

The NFDRS fuel model determines the fuel loading parameters used for
ERC, SC, and BI calculations. The system supports all 22 standard
NFDRS fuel models (A through Z, excluding M).

### Default: Fuel Model G

Fuel model **G** ("Dense conifer with heavy dead fuel") is the default
for the Western Upper Peninsula, which is predominantly forested with
mixed conifer/hardwood stands and significant dead fuel accumulation.

### Other Fuel Models

Use `--fuel-model X` to select a different model. Common choices:

| Model | Description | When to Use |
|-------|-------------|-------------|
| **A** | Western annual grass | Open grassland |
| **C** | Open pine with grass | Sparse pine woodland |
| **D** | Southern rough | Shrubby areas |
| **F** | Intermediate brush | Moderate shrub |
| **G** | Dense conifer, heavy dead fuel | **WUP forests (default)** |
| **H** | Short needle conifer, light dead fuel | Young pine stands |
| **R** | Hardwood litter, closed canopy | Deciduous forest |
| **T** | Sagebrush/grass | Not typical for WUP |
| **U** | Western long needle pine | Not typical for WUP |

For spatially-varying fuel models (different models for different points),
future versions will support a fuel model map from LANDFIRE.

---

## Fuel Moisture Methods

The system supports two approaches for estimating dead fuel moisture:

### EMC Method (Default: `--fuel-moisture emc`)

Uses the **Equilibrium Moisture Content** approximation (Simard 1968):
- Computes EMC from current temperature and relative humidity
- 1-hr fuel moisture = EMC
- 10-hr fuel moisture = EMC x 1.28
- 100-hr and 1000-hr derived with additional lag factors
- **No carry-forward state** — each hour is independent

Best for: Quick analyses, when you don't need to track multi-day drying trends.

### Nelson Method (`--fuel-moisture nelson`)

Uses the **Nelson dead fuel moisture model** with timelag calculations:
- 1-hr moisture responds within hours (fast drying/wetting)
- 10-hr moisture has ~10 hour response time
- 100-hr moisture has ~100 hour response time (~4 days)
- 1000-hr moisture has ~1000 hour response time (~6 weeks)
- **Carry-forward state** — previous hour's moisture affects current hour

Best for: Multi-month analyses where cumulative drying matters (e.g.,
drought tracking). The 1000-hr fuel moisture is especially important
for capturing seasonal drought effects on ERC.

---

## Adding NDVI Data for FPI

The Fire Potential Index (FPI) requires satellite vegetation data that
is not included in the AORC dataset. Without NDVI data, FPI will output
NaN values.

### What You Need

1. **NASA Earthdata account** (free): https://urs.earthdata.nasa.gov/
2. **MODIS NDVI data** (MOD13A2): 1 km resolution, 16-day composites
   - Or **VIIRS NDVI** (VNP13A1): 500 m resolution, 16-day composites
3. **Static FPI inputs** (computed once from multi-year NDVI archive):
   - NDVI minimum per pixel (historical minimum)
   - NDVI maximum per pixel (historical maximum)
   - Live fuel moisture map (LLfm)
   - Dead fuel moisture map (DLfm)
   - Moisture of extinction map (MXd)

### File Format

Place static data in `data/static/`:
```
data/static/
  FPI_Input_Static.nc    # NetCDF with ND_min, ND_max, LLfm, DLfm, MXd
```

Place time-varying NDVI data organized by year:
```
data/static/ndvi/
  FPI_Input_2020.nc      # NetCDF with ND0 (daily NDVI), Tmax, RH_min
```

The system will auto-detect these files when running FPI calculations.

---

## WUP Grid Explorer

A map-based tool for computing and viewing the four indices for any AORC cell, any hours, with every
calculation step visible. Results are saved as you go, so you can close it and pick up later.

### Starting it

| How | Command | Notes |
|---|---|---|
| **One file, no Python** | double-click `WUP_Explorer.exe` | Built by the *Build WUP Explorer* GitHub Action (Actions tab → Run workflow → download `WUP_Explorer-Windows`). macOS/Linux builds come from the same workflow. |
| **Double-click, from this folder** | `WUP_Explorer.bat` (Windows) · `WUP_Explorer.command` (macOS) · `./wup_explorer.sh` (Linux) | The first run makes a private `.venv` and installs `requirements-explorer.txt`. Later runs start in seconds. |
| **Python** | `python -m aorc_tools.explorer` (or `aorc-tools serve`, `wup-explorer`) | Options: `--source auto\|aorc\|synthetic`, `--workspace DIR`, `--port 8765`, `--no-browser` |

The browser opens at http://127.0.0.1:8765/.
- **Data source:** with `--source auto` (the default), the explorer uses the real NOAA AORC archive on S3
  if it can reach it, otherwise clearly labelled synthetic demo weather. The source badge switches it.
- **Running twice:** starting it again while it is already running just opens the browser.
- **Saved data:** everything goes to `WUP_Explorer_data/` next to the program (results, jobs, saved
  views, exports).

### Vocabulary

| Term | Meaning |
|---|---|
| **cell** | one AORC grid cell, 1/120° (~930 m N–S × ~640 m E–W at 47° N), identified by `point_id` |
| **brick** | one Zarr chunk: 144 h × 128 × 256 cells. The unit S3 serves; 5 bricks cover the WUP. |
| **packet** | one cell at one hour, the smallest unit of calculation |
| **job** | "compute these bricks over this period and save every packet"; resumable |

### What it computes (default configuration `hourly-default`)

- **FWI, hourly (FWI2025):** identical to NRCan's cffdrs-ng reference code (`docs/validation`).
  - DMC/DC dry only while the sun is up at that cell. Daylight comes from the sun's position at the packet's UTC instant.
  - FFMC has the 0.5 mm canopy interception.
- **FWI, classic daily:** Van Wagner (1987), identical to xclim.
  - Updated once per cell at 12:00 local standard time from noon weather and the 24 h rain.
  - This is the form CWFIS, Copernicus/GEFF and the Argonne study report.
- **ERC, BI:** NFDRS-88 equations (default fuel model G) on hourly timelag dead-fuel moisture.
  - The equations reproduce NFDRS4. The dead-fuel moisture model is simpler than NFDRS 2016's Nelson
    model, so these are reported as percentiles (`docs/validation`).
- **FPI:** Burgan (1998).
  - MODIS NDVI, 2000 onward.
  - Hourly 10-h fuel moisture × daily greenness.
- **Resolution ladder:** each packet uses its own cell-hour value when it exists. Otherwise, in order: the
  same cell ±3 h (not for rain), the 3×3 mean, 7×7, the brick mean, then "unavailable". The tier is saved
  with every packet.
- **Time basis:**
  - AORC stamps are UTC.
  - Gogebic and Iron cells use Central time (UTC−6 standard), the others Eastern (UTC−5).
  - Daily views use each cell's local standard day.
  - Daylight saving only changes what the clock shows.
  - See the *Time & daylight* tab.

Other configurations are one click away in the Calculation panel:
- the legacy hourly port and legacy UTC daylight test, to reproduce the old batch record;
- EMC fuel moisture;
- daily-Burgan FPI;
- another fuel model.

Results are stored per configuration.

### Using it

1. **Select area.**
   - **Cell** (shift-click adds), **Box**, **Brick**, **County**, or **Entire WUP**.
   - **Import** a GeoJSON polygon, a saved selection or view, or a snapshot.
2. **Time window.** Start (UTC), the number of hourly packets, and the spin-up for live runs.
3. **Show** reads saved results if they cover the window, otherwise computes them now. **Explore live**
   always computes and adds comparison overlays. **Calculate & save** queues a background job for the
   selection's bricks.
4. **Index tabs (FWI, FPI, ERC, BI).**
   - Hourly, daily-max or daily-at-obs-hour charts, raw or percentile values.
   - Overlays: classic daily FWI, other daylight basis, other FWI port, other moisture model.
   - Under each chart, every step for the focused cell at the selected packet, with its formula, source
     and effective resolution for that year.
5. **Fires.** See below.
6. **Coverage** shows what percentage of all packets is saved, by index, year × month, brick and tier.
   **Jobs** runs and resumes background calculations. **Save & share** holds links, saved views,
   snapshots, CSV/NetCDF/GeoJSON exports and the workspace file list.

### Known-fire checks (Fires tab)

The tab answers "do the numbers make sense where real fires started?"

1. Pick a fire (table, or a yellow dot on the map) and press **Check this fire**. You get FWI (hourly and
   daily), FPI, ERC and BI at the fire's cells (3×3 around the point of origin, or the perimeter) on the
   discovery day: at the discovery hour when it is known, as the day's maximum, and as the maximum over
   the 3 days before.
2. **Calculate this season** queues one job (that brick, April–October of the fire's year). After it, the
   check also ranks the discovery day among all days of that season at those cells: 80th, 90th and 97th
   percentiles are flagged.
3. **Reference checks:** values recorded with a fire (for example the earlier real-AORC Pinery numbers)
   are compared automatically.
4. **Import fires…** takes CSV, Excel or GeoJSON from NIFC (WFIGS/InFORM, including shapefile-truncated
   names such as `FireDiscov`, `IncidentSi`, `InitialLat`), MTBS or Michigan DNR. Records outside the WUP
   are skipped. For fires without coordinates, use **Set location** and click the map.
5. **Check all & save report** writes `wup_fire_checks_*.html/.csv/.json` to `WUP_Explorer_data/exports`.

Without the browser, the same run is one command. It computes every fire's season, skips anything
already saved, checks every fire, and writes the reports:

```bat
Run_Fire_Checks.bat                         :: or: WUP_Explorer.exe --fire-checks
WUP_Explorer.exe --fire-checks --fires pinery-2009,summit-lake-2024
```

These are case checks, not a skill score: one fire on one day says little on its own. A skill test needs
every day with and without fires, which is the companion paper.

### Results files

- One NetCDF4 (CF-1.8) file per 144 h time chunk covering the whole WUP rectangle:
  `WUP_Explorer_data/results/<source>/<config>/<year>/WUP_FDI_<year>_k<chunk>_<date>.nc`.
- Variables: `FWI`, `FWI_daily`, `FPI`, `ERC`, `BI` as int16 with scale factors, zlib-compressed;
  `wx_tier` and `ndvi_tier`; and the chain state in the group `chain_state`.
- About 2 bytes per packet for all indices together.
- Open any file in Panoply, QGIS or `xarray.open_dataset`.
- *Coverage › Export year* merges a year into one hourly file plus one daily file (daily maxima over the
  local standard day, and `FWI_daily_noon`).

### Validation and comparison with other tools

See `docs/validation/README.md` (reproducible with `scripts/compare_*.py`) and
`docs/existing_tools_comparison.md`. Both are also summarised in the explorer's *Data flow* tab.

---

## Troubleshooting

### S3 Connection Fails

```
Error: Could not connect to AORC S3 bucket
```

- The AORC bucket (`noaa-nws-aorc-v1-1-1km`) is public and requires no
  AWS credentials, but does require internet access.
- Check your network connection and any proxy/firewall settings.
- Try: `aorc-tools status` to test connectivity.
- The system tries 5 different S3 access methods automatically.

### Out of Memory

If extraction runs out of RAM with many points:

1. Extract fewer months at a time: `--start-month 1 --end-month 3`
2. Use a smaller study area (fewer points)
3. The system processes data hour-by-hour so peak memory usage is
   proportional to point count, not time range.

### NaN Values in Output

- **FWI components**: Check that the carry-forward state from previous
  months is available in the `state/` directory. If starting fresh in
  the middle of a year, the first few days use default startup values
  (FFMC=85, DMC=6, DC=15).
- **FPI**: NaN is expected if NDVI data is not available.
- **ERC/BI**: Check fuel moisture values — if all moisture values are
  zero or negative, check the raw AORC temperature and humidity CSVs.

### Water Filtering Fails

If `--filter-water` fails due to network issues (OSM download), use
`--no-filter-water` to skip water body exclusion. Lake Superior points
will be included but can be filtered later in post-processing.

### CSV Files Are Too Large

Monthly CSVs can be 50-200 MB each (15K points x 744 hours). If this
is too large for Excel:
- Load in Python with `pandas.read_csv()`
- Load specific columns: `pd.read_csv(file, usecols=['2020-07-15 12:00'])`
- Use the website map interface instead of opening CSVs directly

---

## Data Sources and References

### AORC Dataset
- Fall et al. 2023: "The Office of Water Prediction's Analysis of Record
  for Calibration, version 1.1" — JAWRA
- S3 Bucket: `s3://noaa-nws-aorc-v1-1-1km` (public, no credentials)
- Resolution: 30 arc-seconds (~800 m), hourly, 1979-present

### Fire Danger Indices
- **CFWI**: Van Wagner & Pickett 1985; hourly method from Van Wagner 1977;
  cffdrs-ng (NRCan CFFDRS2025)
- **ERC/BI**: Bradshaw et al. 1984 (USFS INT-169); NFDRS4-TechDoc;
  Argonne National Laboratory (Yu/Feng 2023)
- **FPI**: Burgan et al. 1998; Argonne National Laboratory (Yu/Feng 2023)

### Validation
All fire danger index calculations are cross-validated against:
- Argonne National Laboratory code (Yu & Feng, doi:10.1029/2023EF003823)
- cffdrs-ng Python implementation (NRCan)
- firelab/NFDRS4-TechDoc (USFS Fire Lab)
