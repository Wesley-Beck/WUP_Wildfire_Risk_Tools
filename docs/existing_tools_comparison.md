# How the WUP Grid Explorer compares with existing fire-danger tools

Compiled 2026-10-20. "Checked" means verified this session against the source linked below.
Lines marked *(not verified here)* are background knowledge that still needs checking before
they go into a paper.

## The products

| Product | Indices | Grid | Time step | Period | Weather source | Relation to this tool |
|---|---|---|---|---|---|---|
| **WUP Grid Explorer** (this repo) | FWI hourly (FWI2025), classic daily FWI, FPI, ERC, BI | 800 m AORC cells. Effective weather is 2.5–12 km, precipitation ~4–12 km | hourly; daily FWI at 12:00 LST | 1979–2026; FPI 2000→ | NOAA AORC v1.1 | — |
| **NRCan cffdrs-ng / FWI2025** | hourly FWI system + grassland codes | point / any | hourly | — | any | Our hourly FWI reproduces its official output exactly (validation §1). FWI2025 is being released in stages; the NRCan page was issued 2025-02-21. *Checked.* |
| **xclim `cffwis_indices`** | classic daily FWI system | any | daily | — | any | Our `FWI_daily` reproduces it exactly (validation §2). *Checked.* |
| **NFDRS 2016 / NFDRS4** (USFS Fire Lab), used through WIMS/FireFamilyPlus | ERC, BI, SC, IC, KBDI; Nelson dead-fuel moisture; GSI live-fuel moisture | RAWS stations | hourly, daily obs at 13:00 | — | stations | Our ERC/BI equations reproduce it; our dead-fuel moisture model does not (validation §3). *Checked.* |
| **gridMET** (U. Idaho, Abatzoglou 2013) | ERC, BI, FM100, FM1000 (+ weather) | ~4 km (1/24°) | daily | 1979 → present | NLDAS-2 + PRISM | The CONUS gridded ERC/BI benchmark; Argonne used it for the historical record. The Earth Engine catalog page does not state the fuel model *(commonly reported as fuel model G; not verified here)*. *Checked:* grid, cadence, period, bands. |
| **Copernicus CEMS fire danger indices, historical** (ECMWF) | FWI system, ERC, BI, SC, IC, KBDI, Drought Factor, Danger rating, … (15 indices) | 0.25° (ERA5 native ~31 km) | daily snapshot at 12:00 local | 1940 → present | ERA5 | Global daily FWI/ERC/BI. Compare with our `FWI_daily`, not the hourly FWI. *Checked.* |
| **ERA5-FWI-SN** (Benoit et al. 2026, ESSD) | daily FWI system | ~31 km | daily, inputs at **solar noon** from hourly ERA5 | 1950 → present | ERA5 | Canada only. Its solar-noon basis matches the explorer's per-cell sun-position daylight fix. *Checked.* |
| **USGS / USFS Fire Potential Index** | FPI (Burgan) | moderate-resolution satellite + forecast grid | daily, plus 7-day forecasts | operational | NWS NDFD forecasts | An operational forecast, not a historical record. Uses the same Burgan structure as our FPI. *Checked (fact sheet).* |
| **Yu et al. 2023, Argonne** (Earth's Future) | FPI, FWI, ERC, BI | historical: gridMET ~4 km; future: WRF 12 km | daily | 1984–2019 historical; 1995–2004 vs 2085–2094 | gridMET; WRF (CCSM4, RCP 8.5) | The project's precedent. ERC/BI used NFDRS 2016 fuel models (Jolly 2018). FPI used AVHRR NDVI at 0.05° with 10-h fuel moisture. Evaluated against 13,353 MTBS wildfires. *Checked.* |

## What this means for the project

1. **FWI is now directly comparable to the standard products.** The hourly chain equals the official
   FWI2025 code, and the daily chain equals xclim and therefore CWFIS/GEFF-style daily FWI. Both are
   computed for every packet, so the paper can report both. The legacy hourly port behind the existing
   1979–2002 batch record does not match either one (validation §1), so that record's FWI should be
   recomputed. *Well supported.*
2. **ERC/BI percentiles roughly track NFDRS 2016** (ρ ≈ 0.90 in fire season at one RAWS station) but are
   not interchangeable with it. Per-day percentiles differ by about 10 points, and the gap is entirely
   the dead-fuel moisture model. This backs the "derived, reported as percentiles" decision. Closing the
   gap means adopting the Nelson stick model, which raises the GPL question in validation §3.
   *Well supported for that station; plausible for the WUP.*
3. **Resolution claims need care.** AORC delivers 800 m cells. Before 2016, though, its humidity,
   pressure and wind are NLDAS-2 at ~12 km, the same parent data as gridMET's humidity and wind. Its
   precipitation is ~12 km before 2002 and ~4 km (Stage IV) after. Against gridMET, the real spatial gain
   is in precipitation after 2002 and the PRISM-shaped temperature pattern, not in wind or humidity. The
   real temporal gain is hourly versus daily. *Well supported (AORC methods document; gridMET catalog).*
4. **Two statements in the earlier manuscript draft are worth re-checking against Yu et al. 2023.**
   - **"Computed at 12 km":** the historical evaluation used gridMET at ~4 km. 12 km was the WRF future
     run.
   - **"Reportedly negative" correspondence in the northern Lake States:** the paper's text reports weak
     (ρ < 0.32 at the grid/daily scale) but positive and significant correlations. Regional values appear
     only in its Figure 3.
   
   Check Figure 3 before keeping "negative". *Plausible discrepancy; needs verification.*
5. **Known-fire checks are case studies, not skill scores.** Yu et al. scored indices against all MTBS
   fires. In the WUP, MTBS holds only a couple of events, so the explorer's per-fire percentile ranks are
   the realistic first step. A proper skill test needs every day with and without fires (FPA-FOD or
   Michigan DNR records) and belongs in the companion paper.

## Sources

- cffdrs-ng: https://github.com/nrcan-cfs-fire/cffdrs-ng · NRCan CFFDRS next generation: https://natural-resources.canada.ca/forest-forestry/wildland-fires/canadian-forest-fire-danger-rating-system-generation
- NFDRS4: https://github.com/firelab/NFDRS4
- xclim: https://xclim.readthedocs.io
- gridMET (Earth Engine catalog): https://developers.google.com/earth-engine/datasets/catalog/IDAHO_EPSCOR_GRIDMET
- CEMS fire danger indices historical data: https://ewds.climate.copernicus.eu/datasets/cems-fire-historical-v1 · user guide: https://confluence.ecmwf.int/spaces/CEMS/pages/340779714
- Benoit et al. 2026, ERA5-FWI-SN: https://essd.copernicus.org/articles/18/5995/2026/
- USGS FPI fact sheet: https://pubs.usgs.gov/fs/2012/3121/fs20123121.pdf
- Yu et al. 2023, Earth's Future: https://nwfsc.forestry.oregonstate.edu/sites/default/files/publications/Earth%20s%20Future%20-%202023%20-%20Yu%20-%20Performance%20of%20Fire%20Danger%20Indices%20and%20Their%20Utility%20in%20Predicting%20Future%20Wildfire%20Danger%20Over.pdf
