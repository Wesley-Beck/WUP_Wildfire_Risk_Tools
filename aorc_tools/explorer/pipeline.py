"""
Documentation of the chain (rendered by the page) and JSON payload builders.

Nothing here computes science: see chain.py. This module describes every step,
the resolution of every input (with its fallback ladder), how each FDI is used
by the tool, and serialises chain results for the browser.
"""

from __future__ import annotations

import warnings

import numpy as np

from aorc_tools.explorer.chain import FDIS, ChainResult
from aorc_tools.explorer.ladder import WX_TIERS
from aorc_tools.explorer.ndvi import NDVI_TIERS

AORC_DOC = "AORC v1.1 methods (NOAA OWP, aorc_v1_1_methods.pdf)"

# Effective source resolution by era (per the AORC v1.1 methods document).
RESOLUTION_MATRIX = [
    {"input": "Air temperature (2 m)", "id": "TMP_2maboveground", "grid": "800 m / 1 h (instantaneous at the top of the hour)",
     "source": "1979–2015: NLDAS-2 12 km, adjusted with PRISM/NCEI climatology, Livneh daily max/min (7 km) and a 0.65 °C/100 m lapse rate · 2016–: URMA 2.5 km",
     "effective": "~800 m climatological pattern; day-to-day variation 12 km (→2015) / 2.5 km (2016→)",
     "ladder": "cell-hour → time-interp ≤3 h → 3×3 → 7×7 → brick mean", "used_by": ["FWI", "ERC", "BI", "FPI"]},
    {"input": "Specific humidity (2 m)", "id": "SPFH_2maboveground", "grid": "800 m / 1 h",
     "source": "1979–2015: NLDAS-2 12 km, terrain-adjusted (RH held constant through the height change) · 2016–: URMA 2.5 km (the document says 2016 in one section, 2018 in another)",
     "effective": "12 km (→2015) / 2.5 km (2016→)", "ladder": "cell-hour → time-interp ≤3 h → 3×3 → 7×7 → brick mean",
     "used_by": ["FWI", "ERC", "BI", "FPI"]},
    {"input": "Surface pressure", "id": "PRES_surface", "grid": "800 m / 1 h",
     "source": "1979–2015: NLDAS-2 12 km, hydrostatic terrain adjustment · 2016–: URMA 2.5 km",
     "effective": "12 km (→2015) / 2.5 km (2016→); only enters via RH", "ladder": "cell-hour → time-interp ≤3 h → 3×3 → 7×7 → brick mean",
     "used_by": ["FWI", "ERC", "BI", "FPI"]},
    {"input": "10 m wind (U, V)", "id": "UGRD_10maboveground", "grid": "800 m / 1 h",
     "source": "NLDAS-2 12 km, bilinear, not downscaled (the document's table lists NLDAS-2 throughout; its text says URMA from 2016)",
     "effective": "12 km", "ladder": "cell-hour → time-interp ≤3 h → 3×3 → 7×7 → brick mean", "used_by": ["FWI", "BI"]},
    {"input": "Precipitation (1 h)", "id": "APCP_surface", "grid": "800 m / 1 h (accumulation over the hour ENDING at the stamp)",
     "source": "Daily totals: NLDAS-2 12 km (1979–2001), Stage IV 4 km (2002–), Livneh/PRISM bias & pattern corrections; hourly timing from radar/satellite mosaics (MDR 47 km 1979–94, Stage II/NOWrad 4 km 1996–2002, CMORPH 8 km 1998–, Stage IV 4 km 2002–, GDAS 27 km fallback)",
     "effective": "~12 km (→2001) / ~4 km (2002→), with an 800 m PRISM climatological pattern",
     "ladder": "cell-hour → 3×3 → 7×7 → brick mean (accumulations are never time-interpolated)", "used_by": ["FWI", "ERC", "BI"]},
    {"input": "Downward shortwave", "id": "DSWRF_surface", "grid": "800 m / 1 h", "source": "NLDAS-2 12 km, not downscaled",
     "effective": "12 km", "ladder": "carried for reference; not used by these four indices", "used_by": []},
    {"input": "Sun position (daylight)", "id": "daylight_fixed", "grid": "per cell, per packet",
     "source": "NOAA solar-position equations at the packet's UTC instant, using the cell's own latitude and longitude",
     "effective": "exact (within ~2 min of astral)", "ladder": "—", "used_by": ["FWI"]},
    {"input": "NDVI (greenness)", "id": "NDVI", "grid": "cell / daily, held across the day's 24 h",
     "source": "MODIS MOD13Q1 250 m 16-day composite, aggregated to the cell and interpolated to daily", "effective": "250 m / 16-day",
     "ladder": "MOD13Q1 250 m → MOD13A1 500 m → MOD13A2 1 km → (opt-in, outside the locked decision) AVHRR CDR ~5 km → day-of-year climatology → none. Nothing before 2000-02-18 unless opted in.",
     "used_by": ["FPI"]},
    {"input": "Fuel model", "id": "fuel_model", "grid": "per cell", "source": "data/static/nfdrs_fuel_model.csv (e.g. from LANDFIRE)",
     "effective": "cell when mapped", "ladder": "per-cell map → project default (G) everywhere", "used_by": ["ERC", "BI", "FPI"]},
    {"input": "Live fuel moisture", "id": "MC_HERB", "grid": "constant", "source": "herbaceous 120 %, woody 100 % (no live-fuel model)",
     "effective": "none (constant)", "ladder": "—", "used_by": ["ERC", "BI"]},
]

FDI_CARDS = [
    {"id": "FWI", "name": "Fire Weather Index", "system": "Canadian FFDRS — hourly FWI2025 (cffdrs-ng), plus classic daily FWI", "time": "hourly (+ daily at 12:00 LST)",
     "space": "cell; wind/humidity effectively 12 km (→2015)",
     "how": "Hourly FFMC; DMC and DC dry only while the sun is up at that cell; ISI → BUI → FWI every hour. The classic daily FWI (noon LST, 24 h rain) runs alongside for comparison with daily products.",
     "status": "hourly chain reproduces NRCan's official cffdrs-ng reference exactly; daily chain reproduces xclim exactly (docs/validation)", "report": "native value and percentile"},
    {"id": "FPI", "name": "Fire Potential Index", "system": "Burgan et al. 1998 (Argonne port)", "time": "hourly weather × daily greenness",
     "space": "cell (NDVI 250 m)", "how": "Relative greenness from NDVI sets the live/dead split; the hourly 10-h timelag fuel moisture sets dead-fuel dryness.",
     "status": "MODIS era only (2000–2026); the hourly moisture basis is this tool's adaptation (daily Burgan form available)", "report": "0–100 and percentile"},
    {"id": "ERC", "name": "Energy Release Component", "system": "NFDRS (Argonne port), fuel model G", "time": "hourly",
     "space": "cell", "how": "Hourly 1/10/100/1000-h timelag dead-fuel moisture and constant live moisture → load-weighted reaction intensity.",
     "status": "derived/simplified — reported as a percentile, not cited as canonical NFDRS", "report": "percentile"},
    {"id": "BI", "name": "Burning Index", "system": "NFDRS (Argonne port)", "time": "hourly", "space": "cell (wind 12 km)",
     "how": "3.01·(SC·ERC)^0.46 with the hourly spread component.", "status": "derived/simplified — reported as a percentile", "report": "percentile"},
]

KNOWN_ISSUES = [
    {"id": "daylight", "status": "FIXED (default)",
     "text": "The legacy hourly DMC/DC daylight test compared the UTC hour of each packet with sunrise/sunset in local SOLAR hours at one "
             "latitude (46.5 °N). In the WUP that credits drying from about 23:00 to 13:00 local solar time and misses the afternoon: "
             "about half of all hours are misclassified. Default now: the sun's position at each cell at the packet's UTC instant. The "
             "legacy test stays available as a comparator and for reproducing old outputs."},
    {"id": "coefficients", "status": "RESOLVED IN THE EXPLORER (confirm for the batch record)",
     "text": "The legacy hourly port used the DAILY-system DMC/DC drying coefficients and reset its rain total every dry hour with no FFMC "
             "canopy interception. On NRCan's own reference weather (PRF2007, 2,623 h) it gives mean DMC 0.4 vs 17.5 and mean DC 1,529 vs "
             "136, and FWI in the right class only 72 % of hours. The cffdrs-ng mode (hourly coefficients + 0.5 mm FFMC interception + "
             "canopy total reset after 5 dry hours) reproduces the official output exactly, so the explorer now defaults to it. The legacy "
             "port stays selectable to reproduce the existing 1979–2002 batch record, which was computed with it."},
    {"id": "aorc-resolution", "status": "CAVEAT",
     "text": "The AORC grid is ~800 m, but day-to-day information in humidity, pressure, wind and shortwave comes from 12 km NLDAS-2 "
             "(→2015) or 2.5 km URMA (2016→); precipitation from ~12 km (→2001) or ~4 km Stage IV (2002→). The 800 m detail is a "
             "climatological (PRISM) and terrain pattern. See the Resolution tab."},
    {"id": "derived", "status": "CAVEAT",
     "text": "ERC/BI use one NFDRS fuel model (unless a per-cell map is supplied) and constant live-fuel moisture; they are derived "
             "indices reported as percentiles."},
    {"id": "futures", "status": "PLANNED (after the record verifies)",
     "text": "Climate-model datasets (GLARM/RegCM, CMIP/CORDEX, WRF, ERA5) already run through the same four FDIs (Data tab), stored "
             "apart from the AORC record. Not yet built: bias correction against AORC 2000–2019 (delta change / quantile delta "
             "mapping), so FDIs from raw model weather carry the model's biases. The public GLARM-Proj1 files hold only daily lake "
             "temperature, ice and 2 m air temperature; the FDIs need the RegCM surface fields. See docs/future_glarm_plan.md."},
]

STAGES = [
    {"id": "intake", "label": "1 · AORC intake", "note": "7 variables per packet, through the resolution ladder (S3, or the local input store offline; climate-model datasets are regridded to the same cells)."},
    {"id": "convert", "label": "2 · Unit conversion", "note": "Deterministic, stateless, per packet."},
    {"id": "time", "label": "3 · Time & sun", "note": "Per-cell sun position at the packet's UTC instant; county time zone for local-time views."},
    {"id": "cffwis", "label": "4a · CFFWIS (hourly + classic daily)", "note": "Carry-forward FFMC/DMC/DC moisture state; DMC/DC dry only in daylight. Classic daily FWI updated once per cell at 12:00 LST."},
    {"id": "nfdrs", "label": "4b · NFDRS", "note": "Hourly timelag dead-fuel moisture (default) or EMC multipliers; live fuels constant."},
    {"id": "fpi", "label": "4c · FPI (Burgan)", "note": "NDVI ladder; MODIS era only unless the climatology tier is opted in."},
    {"id": "post", "label": "5 · Post-pass", "note": "Percentiles vs the reference climatology built from computed data."},
]

STEP_CATALOG: list[dict] = [
    {"id": "TMP_2maboveground", "stage": "intake", "label": "2 m air temperature", "units": "K", "formula": "AORC variable (instantaneous, top of hour)", "ref": AORC_DOC, "res": "12 km→2015 / 2.5 km 2016→ (PRISM pattern)", "res_eras": [[2015, "12 km + PRISM"], [9999, "2.5 km + PRISM"]], "feeds": ["FWI", "ERC", "BI", "FPI"]},
    {"id": "SPFH_2maboveground", "stage": "intake", "label": "2 m specific humidity", "units": "kg/kg", "formula": "AORC variable", "ref": AORC_DOC, "res": "12 km→2015 / 2.5 km 2016→", "res_eras": [[2015, "12 km"], [9999, "2.5 km"]], "feeds": ["FWI", "ERC", "BI", "FPI"]},
    {"id": "PRES_surface", "stage": "intake", "label": "Surface pressure", "units": "Pa", "formula": "AORC variable", "ref": AORC_DOC, "res": "12 km→2015 / 2.5 km 2016→", "res_eras": [[2015, "12 km"], [9999, "2.5 km"]], "feeds": ["FWI", "ERC", "BI", "FPI"]},
    {"id": "UGRD_10maboveground", "stage": "intake", "label": "10 m wind, U", "units": "m/s", "formula": "AORC variable", "ref": AORC_DOC, "res": "12 km", "feeds": ["FWI", "BI"]},
    {"id": "VGRD_10maboveground", "stage": "intake", "label": "10 m wind, V", "units": "m/s", "formula": "AORC variable", "ref": AORC_DOC, "res": "12 km", "feeds": ["FWI", "BI"]},
    {"id": "APCP_surface", "stage": "intake", "label": "Precipitation (hour ending at stamp)", "units": "mm", "formula": "AORC variable", "ref": AORC_DOC, "res": "~12 km→2001 / ~4 km 2002→", "res_eras": [[2001, "~12 km"], [9999, "~4 km"]], "feeds": ["FWI", "ERC", "BI"]},
    {"id": "DSWRF_surface", "stage": "intake", "label": "Downward shortwave", "units": "W/m²", "formula": "AORC variable (reference only)", "ref": AORC_DOC, "res": "12 km", "feeds": []},
    {"id": "wx_tier", "stage": "intake", "label": "Weather resolution tier (worst input)", "units": "tier", "formula": "0 native · 1 time-interp · 2 3×3 · 3 7×7 · 4 brick · 9 none", "ref": "explorer.ladder", "feeds": ["FWI", "ERC", "BI", "FPI"]},
    {"id": "temperature_c", "stage": "convert", "label": "Temperature", "units": "°C", "formula": "T − 273.15", "ref": "climate_convert.kelvin_to_celsius", "inputs": ["TMP_2maboveground"], "feeds": ["FWI"]},
    {"id": "temperature_f", "stage": "convert", "label": "Temperature", "units": "°F", "formula": "T°C·9/5 + 32", "ref": "climate_convert.celsius_to_fahrenheit", "inputs": ["temperature_c"], "feeds": ["ERC", "BI", "FPI"]},
    {"id": "relative_humidity", "stage": "convert", "label": "Relative humidity", "units": "%", "formula": "100·e/es (Magnus)", "ref": "climate_convert.specific_to_relative_humidity", "inputs": ["SPFH_2maboveground", "TMP_2maboveground", "PRES_surface"], "feeds": ["FWI", "ERC", "BI", "FPI"]},
    {"id": "wind_speed_kph", "stage": "convert", "label": "Wind speed", "units": "km/h", "formula": "√(U²+V²)·3.6", "ref": "climate_convert", "inputs": ["UGRD_10maboveground", "VGRD_10maboveground"], "feeds": ["FWI"]},
    {"id": "wind_speed_mph", "stage": "convert", "label": "Wind speed", "units": "mph", "formula": "√(U²+V²)·2.23694", "ref": "climate_convert", "inputs": ["UGRD_10maboveground", "VGRD_10maboveground"], "feeds": ["BI"]},
    {"id": "precipitation_mm", "stage": "convert", "label": "Precipitation", "units": "mm/h", "formula": "APCP", "ref": "extract.py", "inputs": ["APCP_surface"], "feeds": ["FWI", "ERC", "BI"]},
    {"id": "lst_hour", "stage": "time", "label": "Local standard hour (cell's county)", "units": "h", "formula": "UTC + (−6 CT | −5 ET), no DST", "ref": "timebase", "feeds": ["FWI"]},
    {"id": "solar_elevation", "stage": "time", "label": "Solar elevation at the packet", "units": "°", "formula": "NOAA solar position, cell lat/lon, UTC instant", "ref": "timebase.solar_zenith", "feeds": ["FWI"]},
    {"id": "sunrise_lst", "stage": "time", "label": "Sunrise (LST)", "units": "h", "formula": "720 − 4(lon + ha) − eqtime, zenith 90.833°", "ref": "timebase.sun_times_utc (as cffdrs-ng get_sunlight)", "feeds": ["FWI"]},
    {"id": "sunset_lst", "stage": "time", "label": "Sunset (LST)", "units": "h", "formula": "720 − 4(lon − ha) − eqtime", "ref": "timebase.sun_times_utc", "feeds": ["FWI"]},
    {"id": "daylight_fixed", "stage": "time", "label": "Daylight — fixed (sun above horizon)", "units": "0/1", "formula": "zenith < 90.833°", "ref": "timebase.daylight_solar", "feeds": ["FWI"]},
    {"id": "daylight_legacy", "stage": "time", "label": "Daylight — legacy (UTC hour vs solar times)", "units": "0/1", "formula": "sunrise ≤ UTC hour ≤ sunset at 46.5 °N", "ref": "timebase.daylight_legacy", "feeds": ["FWI"]},
    {"id": "daylight_used", "stage": "time", "label": "Daylight used by this chain", "units": "0/1", "formula": "per configuration", "ref": "chain", "feeds": ["FWI"]},
    {"id": "mc_ffmc", "stage": "cffwis", "label": "Fine-fuel moisture content", "units": "%", "formula": "hourly FFMC moisture update (Van Wagner 1977)", "ref": "fwi.hourly_ffmc_vectorized", "inputs": ["temperature_c", "relative_humidity", "wind_speed_kph", "precipitation_mm"], "feeds": ["FWI"], "state": True},
    {"id": "FFMC", "stage": "cffwis", "label": "Fine Fuel Moisture Code", "units": "", "formula": "59.5·(250 − mc)/(147.27 + mc)", "ref": "fwi.mc_to_ffmc", "inputs": ["mc_ffmc"], "feeds": ["FWI"]},
    {"id": "precip_cumulative", "stage": "cffwis", "label": "Canopy rain total", "units": "mm", "formula": "cffdrs-ng: Σ rain, reset after 5 dry hours · legacy: reset every dry hour", "ref": "fwi.HourlyFWI (NG_FWI rain_since_intercept_reset)", "feeds": ["FWI"], "state": True},
    {"id": "canopy_drying", "stage": "cffwis", "label": "Dry hours since rain", "units": "h", "formula": "+1 per dry hour; at 5 the canopy total resets", "ref": "fwi.HourlyFWI", "feeds": ["FWI"], "state": True},
    {"id": "mc_dmc", "stage": "cffwis", "label": "Duff moisture content", "units": "%", "formula": "rain once Σ > 1.5 mm (0.92·P − 1.27); drying 2.22e-4·T·(100 − RH) per daylight hour", "ref": "fwi.hourly_dmc_vectorized", "inputs": ["temperature_c", "relative_humidity", "precip_cumulative", "daylight_used"], "feeds": ["FWI"], "state": True},
    {"id": "DMC", "stage": "cffwis", "label": "Duff Moisture Code", "units": "", "formula": "43.43·ln(280/(mc − 20))", "ref": "fwi.mc_to_dmc", "inputs": ["mc_dmc"], "feeds": ["FWI"]},
    {"id": "mc_dc", "stage": "cffwis", "label": "Deep-layer moisture content", "units": "%", "formula": "rain once Σ > 2.8 mm (0.83·P − 1.27); drying 0.015·T + 3/16 per daylight hour", "ref": "fwi.hourly_dc_vectorized", "inputs": ["temperature_c", "precip_cumulative", "daylight_used"], "feeds": ["FWI"], "state": True},
    {"id": "DC", "stage": "cffwis", "label": "Drought Code", "units": "", "formula": "400·ln(400/mc)", "ref": "fwi.mc_to_dc", "inputs": ["mc_dc"], "feeds": ["FWI"]},
    {"id": "ISI", "stage": "cffwis", "label": "Initial Spread Index", "units": "", "formula": "0.208·f(W)·f(F)", "ref": "fwi.initial_spread_index", "inputs": ["wind_speed_kph", "FFMC"], "feeds": ["FWI"]},
    {"id": "BUI", "stage": "cffwis", "label": "Buildup Index", "units": "", "formula": "0.8·DC·DMC/(DMC + 0.4·DC)", "ref": "fwi.buildup_index", "inputs": ["DMC", "DC"], "feeds": ["FWI"]},
    {"id": "FWI", "stage": "cffwis", "label": "Fire Weather Index", "units": "", "formula": "B = 0.1·ISI·f(D); FWI = exp(2.72·(0.434·ln B)^0.647)", "ref": "Van Wagner & Pickett 1985 (fwi.fire_weather_index)", "inputs": ["ISI", "BUI"], "feeds": ["FWI"], "fdi": True},
    {"id": "precip_24h_noon", "stage": "cffwis", "label": "Daily: rain, 24 h to 12:00 LST", "units": "mm", "formula": "Σ hourly APCP since the previous noon (noon packets only)", "ref": "chain.DailyState", "inputs": ["precipitation_mm"], "feeds": ["FWI"]},
    {"id": "FFMC_daily", "stage": "cffwis", "label": "Daily FFMC (classic)", "units": "", "formula": "Van Wagner 1987, noon T/RH/wind, 0.5 mm interception", "ref": "fwi_daily.daily_ffmc (== xclim)", "inputs": ["temperature_c", "relative_humidity", "wind_speed_kph", "precip_24h_noon"], "feeds": ["FWI"]},
    {"id": "DMC_daily", "stage": "cffwis", "label": "Daily DMC (classic)", "units": "", "formula": "1.894(T+1.1)(100−RH)·Le·1e−4; Le by month", "ref": "fwi_daily.daily_dmc", "inputs": ["temperature_c", "relative_humidity", "precip_24h_noon"], "feeds": ["FWI"]},
    {"id": "DC_daily", "stage": "cffwis", "label": "Daily DC (classic)", "units": "", "formula": "(0.36(T+2.8) + Lf)/2; Lf by month", "ref": "fwi_daily.daily_dc", "inputs": ["temperature_c", "precip_24h_noon"], "feeds": ["FWI"]},
    {"id": "ISI_daily", "stage": "cffwis", "label": "Daily ISI (classic)", "units": "", "formula": "0.208·exp(0.05039·W)·f(F)", "ref": "fwi_daily.daily_isi", "inputs": ["FFMC_daily", "wind_speed_kph"], "feeds": ["FWI"]},
    {"id": "BUI_daily", "stage": "cffwis", "label": "Daily BUI (classic)", "units": "", "formula": "0.8·DC·DMC/(DMC + 0.4·DC)", "ref": "fwi_daily.daily_bui", "inputs": ["DMC_daily", "DC_daily"], "feeds": ["FWI"]},
    {"id": "FWI_daily", "stage": "cffwis", "label": "Classic daily FWI (12:00 LST, held to next noon)", "units": "", "formula": "same FWI equation on the daily codes", "ref": "fwi_daily (Van Wagner 1987; == xclim cffwis_indices)", "inputs": ["ISI_daily", "BUI_daily"], "feeds": ["FWI"]},
    {"id": "EMC", "stage": "nfdrs", "label": "Equilibrium moisture content (hourly)", "units": "%", "formula": "Simard 1968", "ref": "climate_convert.equilibrium_moisture_content", "inputs": ["temperature_f", "relative_humidity"], "feeds": ["ERC", "BI", "FPI"]},
    {"id": "FM1", "stage": "nfdrs", "label": "1-h dead fuel moisture", "units": "%", "formula": "timelag: approach EMC with τ = 1 h | emc: EMC", "ref": "fuel_moisture", "inputs": ["EMC"], "feeds": ["ERC", "BI"], "state": True},
    {"id": "FM10", "stage": "nfdrs", "label": "10-h dead fuel moisture", "units": "%", "formula": "timelag τ = 10 h | emc: 1.28·EMC", "ref": "fuel_moisture", "inputs": ["EMC"], "feeds": ["ERC", "BI", "FPI"], "state": True},
    {"id": "FM100", "stage": "nfdrs", "label": "100-h dead fuel moisture", "units": "%", "formula": "timelag τ = 100 h | emc: 1.5·EMC", "ref": "fuel_moisture", "inputs": ["EMC"], "feeds": ["ERC", "BI"], "state": True},
    {"id": "FM1000", "stage": "nfdrs", "label": "1000-h dead fuel moisture", "units": "%", "formula": "timelag τ = 1000 h | emc: 1.8·EMC", "ref": "fuel_moisture", "inputs": ["EMC"], "feeds": ["ERC", "BI"], "state": True},
    {"id": "MC_HERB", "stage": "nfdrs", "label": "Live herbaceous moisture", "units": "%", "formula": "120 (constant)", "ref": "extract.py", "feeds": ["ERC", "BI"]},
    {"id": "MC_WOOD", "stage": "nfdrs", "label": "Live woody moisture", "units": "%", "formula": "100 (constant)", "ref": "extract.py", "feeds": ["ERC", "BI"]},
    {"id": "fuel_model_tier", "stage": "nfdrs", "label": "Fuel-model tier", "units": "tier", "formula": "0 per-cell map · 1 default model", "ref": "explorer.ndvi.FuelMap", "feeds": ["ERC", "BI", "FPI"]},
    {"id": "SC", "stage": "nfdrs", "label": "Spread Component", "units": "ft/min", "formula": "Rothermel I_R·ξ·(1 + φs + φw)/heat sink", "ref": "nfdrs.compute_erc_bi", "inputs": ["FM1", "FM10", "FM100", "MC_HERB", "MC_WOOD", "wind_speed_mph"], "feeds": ["BI"]},
    {"id": "ERC", "stage": "nfdrs", "label": "Energy Release Component", "units": "BTU/ft²", "formula": "round(0.04·I_Re·τ)", "ref": "nfdrs.compute_erc_bi (derived; percentile)", "inputs": ["FM1", "FM10", "FM100", "FM1000", "MC_HERB", "MC_WOOD"], "feeds": ["ERC", "BI"], "fdi": True},
    {"id": "BI", "stage": "nfdrs", "label": "Burning Index", "units": "", "formula": "3.01·(SC·ERC)^0.46", "ref": "nfdrs.compute_erc_bi (derived; percentile)", "inputs": ["SC", "ERC"], "feeds": ["BI"], "fdi": True},
    {"id": "ndvi_tier", "stage": "fpi", "label": "NDVI resolution tier", "units": "tier", "formula": "0 250 m · 1 500 m · 2 1 km · 3 AVHRR 5 km · 4 climatology · 9 none", "ref": "explorer.ndvi", "feeds": ["FPI"]},
    {"id": "NDVI", "stage": "fpi", "label": "NDVI (daily, held over the day)", "units": "", "formula": "MODIS 16-day composite → daily", "ref": "Burgan et al. 1998", "feeds": ["FPI"]},
    {"id": "ND_MIN", "stage": "fpi", "label": "NDVI minimum (cell)", "units": "", "formula": "static envelope", "ref": "FPI_Input_Static.nc", "feeds": ["FPI"]},
    {"id": "ND_MAX", "stage": "fpi", "label": "NDVI maximum (cell)", "units": "", "formula": "static envelope", "ref": "FPI_Input_Static.nc", "feeds": ["FPI"]},
    {"id": "RG", "stage": "fpi", "label": "Relative greenness", "units": "0–1", "formula": "(ND − ND_min)/(ND_max − ND_min)", "ref": "fpi.compute_fpi", "inputs": ["NDVI", "ND_MIN", "ND_MAX"], "feeds": ["FPI"]},
    {"id": "TMAX24_F", "stage": "fpi", "label": "Trailing 24 h max temperature", "units": "°F", "formula": "max over 24 packets (daily-Burgan basis)", "ref": "fpi.compute_fpi_from_hourly", "inputs": ["temperature_f"], "feeds": ["FPI"]},
    {"id": "RHMIN24", "stage": "fpi", "label": "Trailing 24 h min RH", "units": "%", "formula": "min over 24 packets (daily-Burgan basis)", "ref": "fpi.compute_fpi_from_hourly", "inputs": ["relative_humidity"], "feeds": ["FPI"]},
    {"id": "EMC_FPI", "stage": "fpi", "label": "EMC from daily extremes", "units": "%", "formula": "Simard 1968 on (Tmax, RHmin)", "ref": "fpi.compute_fpi", "inputs": ["TMAX24_F", "RHMIN24"], "feeds": ["FPI"]},
    {"id": "FM10_FPI", "stage": "fpi", "label": "10-h fuel moisture used by FPI", "units": "%", "formula": "hourly: FM10 timelag · daily-burgan: 1.28·EMC_FPI", "ref": "fpi.compute_fpi(fm10=…)", "inputs": ["FM10", "EMC_FPI"], "feeds": ["FPI"]},
    {"id": "FPI", "stage": "fpi", "label": "Fire Potential Index", "units": "0–100", "formula": "100 − 100·(RG·L_f + min(FM10/MXd, 1)·D_f), extinction-adjusted", "ref": "Burgan et al. 1998 (fpi.compute_fpi)", "inputs": ["RG", "FM10_FPI"], "feeds": ["FPI"], "fdi": True},
    {"id": "FWI_pct", "stage": "post", "label": "FWI percentile", "units": "%", "formula": "empirical percentile (pooled-regional)", "ref": "CLAUDE.md", "inputs": ["FWI"], "feeds": ["FWI"]},
    {"id": "ERC_pct", "stage": "post", "label": "ERC percentile", "units": "%", "formula": "how ERC is reported", "ref": "CLAUDE.md", "inputs": ["ERC"], "feeds": ["ERC"]},
    {"id": "BI_pct", "stage": "post", "label": "BI percentile", "units": "%", "formula": "how BI is reported", "ref": "CLAUDE.md", "inputs": ["BI"], "feeds": ["BI"]},
    {"id": "FPI_pct", "stage": "post", "label": "FPI percentile", "units": "%", "formula": "2000–2026 reference", "ref": "CLAUDE.md", "inputs": ["FPI"], "feeds": ["FPI"]},
]
CATALOG_BY_ID = {s["id"]: s for s in STEP_CATALOG}


# Checked against official implementations (docs/validation, scripts/compare_*.py)
VALIDATION = [
    {"what": "Hourly FWI (cffdrs-ng mode)", "against": "NRCan cffdrs-ng NG_FWI.py @ ed143d7, official PRF2007 output (2,623 h)",
     "result": "exact: FFMC/DMC/DC/ISI/BUI/FWI max |difference| < 0.0001; with per-cell sun-position daylight FWI ≤ 0.05", "verdict": "match"},
    {"what": "Hourly FWI (legacy port)", "against": "same reference",
     "result": "DMC mean 0.36 vs 17.45; DC mean 1,529 vs 136; FWI r = 0.62, same danger class 72 % of hours", "verdict": "does not match"},
    {"what": "Classic daily FWI (FWI_daily)", "against": "xclim 0.62.0 cffwis_indices, PRF2007 daily (109 days)",
     "result": "exact: all six codes < 1e-5", "verdict": "match"},
    {"what": "ERC/BI equations (fuel model G)", "against": "USFS NFDRS4 @ 84223b9 fed its own fuel moistures, RAWS 241513 2001–2017",
     "result": "ERC ρ = 1.000, BI ρ = 0.997; mean |Δ percentile| 0.5 / 1.2", "verdict": "match"},
    {"what": "ERC/BI as computed here (timelag dead-fuel moisture)", "against": "NFDRS4 (Nelson dead + GSI live moisture), same station",
     "result": "fire-season ρ 0.90 (ERC) / 0.87 (BI); mean |Δ percentile| ≈ 10; 62 % of NFDRS4 top-decile days also top-decile here",
     "verdict": "tracks, not equal"},
]

EXISTING_TOOLS = [
    {"name": "gridMET", "indices": "ERC, BI, FM100, FM1000", "grid": "~4 km", "time": "daily", "period": "1979→", "weather": "NLDAS-2 + PRISM",
     "compare": "daily ERC/BI → this tool's 'daily @ 13:00 LST' view; the Argonne historical benchmark"},
    {"name": "Copernicus CEMS fire danger (GEFF/ERA5)", "indices": "FWI system, ERC, BI, KBDI, …", "grid": "0.25°", "time": "daily (12:00 local)",
     "period": "1940→", "weather": "ERA5", "compare": "FWI_daily (classic daily FWI)"},
    {"name": "NFDRS 2016 / NFDRS4 (WIMS, FireFamilyPlus)", "indices": "ERC, BI, SC, IC, KBDI", "grid": "RAWS stations", "time": "hourly, obs 13:00",
     "period": "—", "weather": "stations", "compare": "ERC/BI equations match; dead-fuel moisture differs (see validation)"},
    {"name": "NRCan FWI2025 (cffdrs-ng)", "indices": "hourly FWI system + grass", "grid": "any", "time": "hourly", "period": "—", "weather": "any",
     "compare": "the hourly FWI here (exact)"},
    {"name": "ERA5-FWI-SN (Benoit et al. 2026)", "indices": "daily FWI at solar noon", "grid": "~31 km", "time": "daily", "period": "1950→ (Canada)",
     "weather": "ERA5 hourly", "compare": "same solar-position idea as this tool's daylight fix"},
    {"name": "USGS/USFS Fire Potential Index", "indices": "FPI", "grid": "satellite grid", "time": "daily + 7-day forecast", "period": "operational",
     "weather": "NWS NDFD", "compare": "same Burgan structure as FPI here"},
    {"name": "Yu et al. 2023 (Argonne)", "indices": "FPI, FWI, ERC, BI", "grid": "gridMET ~4 km (historical); WRF 12 km (future)",
     "time": "daily", "period": "1984–2019", "weather": "gridMET; WRF", "compare": "the project's precedent; MTBS-based evaluation"},
]


def catalog_payload() -> dict:
    return {"stages": STAGES, "steps": STEP_CATALOG, "fdis": FDIS, "resolution": RESOLUTION_MATRIX,
            "wx_tiers": {str(k): v for k, v in WX_TIERS.items()}, "ndvi_tiers": {str(k): v for k, v in NDVI_TIERS.items()},
            "fdi_cards": FDI_CARDS, "known_issues": KNOWN_ISSUES, "validation": VALIDATION, "existing_tools": EXISTING_TOOLS}


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _clean(a, nd=3):
    a = np.asarray(a, dtype=float)
    r = np.round(a, nd)
    if r.ndim == 1:
        return [None if not np.isfinite(x) else float(x) for x in r]
    return [[None if not np.isfinite(x) else float(x) for x in row] for row in r]


def _tier_counts(a):
    a = np.asarray(a)
    return {str(int(t)): int((a == t).sum()) for t in np.unique(a)}


def chart_steps(fdis):
    ids = []
    for f in fdis:
        ids += [f, f"{f}_pct"]
    if "FWI" in fdis:
        ids += ["FWI_daily", "FWI@daylight", "FWI@coef"]
    if "ERC" in fdis:
        ids += ["ERC@fm"]
    if "BI" in fdis:
        ids += ["BI@fm"]
    if "FPI" in fdis:
        ids += ["FPI@fpi"]
    return ids + ["wx_tier", "ndvi_tier"]


def series_payload(steps: dict, times, point_ids, *, fdis, source, per_cell_limit=1024, alts=None, notes=None,
                   params=None, extra=None) -> dict:
    """Chart payload: per-cell values for the FDIs (+ comparators) when the selection is
    small enough, and mean/min/max over cells always."""
    n_c = len(point_ids)
    out = []
    for sid in chart_steps(fdis):
        if sid not in steps:
            continue
        a = np.asarray(steps[sid], dtype=float)
        e = {"id": sid}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            e["mean"] = _clean(np.nanmean(a, axis=1))
            e["min"] = _clean(np.nanmin(a, axis=1))
            e["max"] = _clean(np.nanmax(a, axis=1))
        if sid.endswith("_tier"):
            e["counts"] = _tier_counts(a)
            if n_c <= per_cell_limit:
                e["values"] = [[int(x) for x in row] for row in np.asarray(steps[sid]).T]
        elif n_c <= per_cell_limit:
            e["values"] = _clean(a.T)
        out.append(e)
    payload = {"source": source, "point_ids": [int(p) for p in point_ids],
               "time": [t.strftime("%Y-%m-%dT%H:00Z") for t in times], "fdis": fdis, "steps": out,
               "alts": {k: v for k, v in (alts or {}).items() if k in steps}, "notes": notes or [], "params": params or {}}
    if extra:
        payload.update(extra)
    return payload


def trace_payload(res: ChainResult, idx: int, *, include_spinup=False) -> dict:
    """Every step for ONE cell (column ``idx`` of the result)."""
    mask = np.ones(len(res.times), bool) if include_spinup else res.analysis_mask
    steps = {}
    for sid, a in res.steps.items():
        a = np.asarray(a)
        col = a[mask][:, idx] if a.ndim == 2 else a[mask]
        steps[sid] = _clean(col, 4)
    return {"point_id": int(res.point_ids[idx]), "time": [t.strftime("%Y-%m-%dT%H:00Z") for t in res.times[mask]],
            "steps": steps, "alts": res.alts, "notes": res.notes, "params": {**res.cfg.as_dict(), "config_id": res.cfg.id},
            "spinup_hours": res.spinup_hours, "meta": res.meta}
