# Layers to compare with the fire-danger record, the mine-waste question, and daily vs hourly FWI

Compiled 2026-10-21 from a literature and data search. **[V]** means the source was fetched and checked. **[B]**
means believed but not confirmed. The order of work is fixed: first make the tool robust, then compute the
1979–2026 record, then compare layers.

## Vegetation and fuels (finest and most consistent first)

| Layer | What | Resolution / time | Access | Notes |
|---|---|---|---|---|
| **Annual NLCD** (USGS/MRLC) | land cover, change, impervious | 30 m [B]; Collection 1.1 1985–2024 [V]; C1.2 to 2025 announced ~2026-06-30 [V] | COGs on AWS S3 (us-west-2), mrlc.gov [V] | Method-consistent annual series. Forest classes are coarse. |
| **LANDFIRE LF2024, "NC" GeoArea** (the WUP is in NC [V]) | EVT/EVC/EVH; FBFM13/40, CBD, CBH, CC, CH; a **CFFDRS fuel-type layer**; disturbance | 30 m [B]; LF2024 NC released Oct 2025 [V]; LF2025 NC scheduled Nov 2026, adds HLS seasonal fuels [V] | landfire.gov | The CFFDRS fuel types suit FWI/FBP work. Versions before 2022 are not a consistent series [B]. |
| **USFS LCMS v2025-11** | fast/slow loss, gain, land cover, land use | 30 m [B]; 1985–2025 annual [V] | data.fs.usda.gov raster gateway; Earth Engine [V] | Best annual disturbance record (harvest, blowdown, insects). |
| **HLS v2.0 + HLS-VI** (NASA) | NDVI, EVI, **NDMI**, NBR | 30 m, 2–3 day [V]; VI forward from Feb 2025, back-processing to 2013/2015 planned [V] | AppEEARS (since 2025-12-09) [V], CMR-STAC [B] | Green-up, leaf-off, canopy moisture. Cloud and snow gaps [B]. |
| **Snow-off date** | SNODAS (1 km daily, 2003–) [V]; **UA SWE** (4 km daily, 1981-10 to 2023-09) [V]; MODIS MOD10A1 (500 m, 2000–) [B] | — | NSIDC | Likely the main control on spring fire-season onset. UA SWE covers most of the AORC era. MODIS ends late 2026 / early 2027 [V]: plan for VIIRS. |
| USFS TreeMap 2016/2020/2022/2023 [V] | forest type, canopy cover, dead trees/acre, dead carbon | 30 m | doi 10.2737/RDS-2026-0038 (2023) [V] | Dead-fuel proxies. 2024 edition expected fall 2026 [V]. |
| USFS BIGMAP 2018 [V] | biomass by species (327) | 30 m | raster gateway | Jack/red pine, aspen, spruce–fir fractions. |
| MODIS MCD12Q2 v061 [V] | green-up / senescence / dormancy dates | 500 m, 2001–2024 | LP DAAC | Phenology dates. |
| Soil moisture | SMAP L4 9 km 2015– [B]; NSIDC-0779 1 km 2015-04 to 2024-06 [V]; NLDAS-2 Noah ~12 km 1979– [B] | — | — | NLDAS-2 matches AORC's wind/humidity source. |
| Disturbance | USFS IDS aerial survey, Region 9 [B], 2021–2025 + all years [V]; Hansen GFC v1.13 to 2025 [V]; LANDFIRE Dist [V] | — | — | IDS polygons are sketch-mapped [B]. |
| Validation | NEON UNDE PhenoCam, Gogebic County (46.23 N, 89.54 W), 2016– [V] | — | phenocam.nau.edu | Ground truth for green-up and leaf-off. |
| Live fuel moisture | no validated satellite LFMC product for the Lake States; Globe-LFMC 2.0 (Yebra et al. 2024, doi 10.1038/s41597-024-03159-6) [V] | — | — | Use HLS NDMI or the NFDRS GSI as a proxy [B]. |
| Michigan | IFMAP/GAP UP land cover (1997–2001, 30 m) [V]; MDNR forest inventory (state forest stands only) [V] | — | — | Historical snapshot; partial coverage. |

## Mine waste and metals

### Layers

- **EPA NPL Superfund boundaries** (CC0) [V]. Torch Lake operable units:
  - OU1: Lake Linden, Hubbell/Tamarack City, Mason Sands.
  - OU3: Calumet Lake, Boston Pond, Michigan Smelter, Dollar Bay, Point Mills, Scales Creek.
  - The remedy is a soil cap plus vegetation (EPA ESD, Oct 2024) [V].
- **USGS Prospect- and Mine-Related Features**, v10 (2023), doi 10.5066/F78W3CHG [V]. Tailings, dumps, pits and
  mines, including Michigan. The best wall-to-wall footprint layer. Completeness on the iron ranges is unconfirmed
  [B].
- **Michigan EGLE Part 201 sites** [V]. Points with contaminant lists.
- **Stamp-sand mapping**: Kerfoot et al. 2014 (*ISPRS IJGI* 3:66, doi 10.3390/ijgi3010066) [V] and Kerfoot et al.
  2020 (*JGLR* 46:1423) [V]. USGS Buffalo Reef data release, doi 10.5066/P9NJY125 [V]. On land, combine the USGS
  tailings polygons with the EPA boundaries and digitise on Michigan's complete QL2 lidar [V].

### The hypothesis and what supports each step

The proposed chain: mine waste → metals taken up by vegetation → more conductive or more flammable vegetation →
more ignitions (for example vegetation contact with power lines) or more extreme fire danger.

| Step | Status |
|---|---|
| Plants on Keweenaw stamp sands take up copper | **Established, but thin evidence.** Camelina on Hubbell/Tamarack stamp sand reached 263 µg/g Cu in leaves (Sidhu, Sarkar & Datta 2020, *Appl. Sci.* 10:1499) [V]; normal foliar Cu is ~5–20 mg/kg [B]. Cu is mostly in bound forms (Sidhu et al. 2016, *EMA* 188:176) [V]. Bare stamp sands did not sustain plant growth [V]. |
| Trace metals change flammability | **Not shown.** Mineral content changes pyrolysis and char combustion at percent levels of K, Ca, Mg and P (Fuentes et al. 2008) [V], not at hundreds of mg/kg Cu. No study found on foliar Cu and ignitability. |
| Metal stress adds dead fuel | **Plausible, indirect.** Litter builds up near smelters (Palmerton; Beyer 1988) [V]. Fires on dead material helped make the Sudbury barrens (Winterhalder 1985) [V]. |
| Metal-rich vegetation conducts more (line-contact faults) | **Speculative: no study found.** Documented drivers are branch diameter, voltage gradient, contact location, species, seasonal drying and wind (EPRI; Victoria VCIT 2015) [V]. |
| Metal-rich ground attracts lightning | **Not supported.** Strike density follows terrain and convection. Ground conductivity changes waveforms, not where lightning strikes (Cummins 2014) [V]. No soil effect in southern Brazil [V]. |
| Mine sites have more fires | **No study found.** Mine sites sit with towns, roads and railroads, so a mine layer would mostly proxy human ignitions. |
| Fire mobilises metals into water | **Established** (Abraham, Dowling & Florentine 2017, *STOTEN* 599:1740) [V]. Relevant around Torch Lake and Portage Lake after fires. |

**Use in this project, for now:**

- Use the mine-waste layers as a **fuel-break mask** (bare stamp sands).
- Use them as a **human-activity proxy**.
- Use them as a **post-fire water-quality hazard** modifier.

Testing the metals-to-flammability or metals-to-conductivity link needs new field measurements on capped sites
versus controls: foliar Cu, live fuel moisture, ignitability and conductivity. That would be a study in its own
right.

## Other layers that may correlate

1. **FPA-FOD 7th edition** (1992–2024, 2.66 M records, doi 10.2737/RDS-2013-0009.7) [V].
   - The primary occurrence layer: discovery date, NWCG cause (railroad and power line [B]), size.
   - Also ask Michigan DNR for ignition records with times. The DNR map has no download [V].
2. **MTBS v12** (1984–2024; east threshold > 500 ac) [V]. Few WUP fires qualify [B].
3. **Lightning**:
   - NCEI SWDI **nldn-tiles**: free, daily cloud-to-ground counts on a 0.1° grid [V].
   - NLDN raw data cost money or are restricted to NOAA [V].
   - GOES GLM (8–14 km) [V].
4. **WUI**: SILVIS 1990–2020 v4 [V].
5. **Railroads**: FRA rail network (BTS NTAD, updated quarterly) [V].
6. **Drought**: gridMET SPI/SPEI/EDDI/PDSI, 4 km, 5-day, 1980– [V].
7. **Topography**: 3DEP QL2 lidar, 100% of Michigan [V].
8. **Soils**: gSSURGO drainage and texture, e.g. droughty outwash like the Baraga Plains [B].
9. **Peatlands**: NWI + Histosols [B].
10. **Lake influence**: distance to Lake Superior and onshore fetch.
    - AORC wind and humidity (~12 km) cannot resolve lake breezes.
    - HRRR (3 km, 2014–) [B] or NLDAS-3 1 km forcing [V] could.
    - No WUP study quantifies lake-breeze effects on fire.

## Daily vs hourly FWI

What FWI2025 is [V]:

- NRCan's FWI2025 needs hourly weather but "remains a daily, landscape-level planning tool".
- Report: GLC-X-42, 2025, hdl 1845/347500.
- Code: cffdrs-ng, MPL-2.0.
- Do not feed it to FBP yet (notice of Feb 2026).

**Reasons for hourly:**

1. **Overnight recovery.** The diurnal FFMC runs 91.5 → 72.7 overnight (Lawson), versus 86.4–90.6 for Van Wagner's
   hourly FFMC (Anderson 2009) [V].
2. **Peak timing.** FWI2025 reports a peak-burn hour (the maximum of ISI) [V].
3. **Rain timing.** Rain is intercepted and accumulated hour by hour [V]. Under the daily system, rain after noon
   counts the next day [B].
4. **Fixed observation hour.** Choosing solar noon versus clock noon shifts mean FWI by up to 3.4 (Benoit et al.
   2026, *ESSD* 18:5995) [V]. At Hobart only about half of daily FFDI peaks fall at the 15:00 observation (*JAMC*
   50:1617, 2011) [V].
5. **Extremes are sampling-sensitive.** Daily-mean inputs bias FWI low (Herrera et al. 2013, *Clim. Change*
   118:827) [V]. Daily proxies overstate trends in extreme days by 5–10% (EGU25-6292) [V].
6. **Night-time burning is increasing.** Luo et al. 2024, *Nature* 627:321 [V]; Balch et al. 2022, *Nature*
   602:442 [V].
7. **Grass codes.** GFMC, GSI and GFWI need hourly solar radiation [V]. They suit spring pre-green-up fires [B].
8. **Consistency.** AORC is natively hourly, and NFDRS2016 is hourly too (Nelson model) [V].

**Reasons daily may be enough:**

1. **Slow codes.** DMC and DC (lags of ~15 and ~53 days) gain little from hourly inputs [V].
2. **Archives are daily.** CWFIS and GEFF/ERA5 are noon-daily, 0.25°, 1979– (Vitolo et al. 2020, *Sci Data* 7:216)
   [V]. Thresholds do not transfer between the systems.
3. **Hourly FFMC can overpredict fire growth.**
   - Bias: 1.78–2.05 for Van Wagner versus 1.11–1.33 for Lawson (Anderson 2009) [V].
   - Area burned is overpredicted (Podur & Wotton 2011, *IJWF* 20:497) [V].
4. **It needs local calibration** (Zhang et al. 2017; Wotton & Beverly 2007) [V].
5. **Cost and inputs.** Climate projections are mostly daily, and hourly runs over the grid are ~24× the compute.

**Plan in the explorer:**

- Keep both systems, and compare them with percentiles, not absolute thresholds.
- The Patterns tab compares the two per cell: agreement on high-danger days, the size of the afternoon peak that
  daily misses, and seasonal and spatial patterns. This shows where daily FWI could stand in for hourly.

## First steps once the record exists

1. Clip FPA-FOD 7 to the WUP (occurrence), and request DNR records with ignition times.
2. Build a snow-off date per AORC cell: UA SWE 1982–2023, then SNODAS 2004–. Test it against the spring season
   start from the Patterns tab.
3. Aggregate Annual NLCD C1.2 and LCMS v2025-11 class and loss fractions to the 800 m cells.
4. Add LANDFIRE LF2024 NC (CFFDRS fuel type, FBFM40, canopy). Switch to LF2025 when NC is released.
5. Add static covariates: gSSURGO drainage and texture, distance to Lake Superior, rail, roads, WUI.
6. Get SWDI nldn-tiles to pair with lightning-caused fires in FPA-FOD.
7. Use the mine-waste polygons (USGS v10 + EPA NPL) as a fuel-break and human-proxy mask.
