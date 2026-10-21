# Power-line data for the WUP: what exists (checked 2026-10-21)

These are the sources the explorer's **Lines** tab can use (*Download current lines…*, `--lines-download`). Facts
marked *verified* were fetched from the source on 2026-10-21. Facts marked *unverified* are beliefs that need
checking.

## What happened to HIFLD

- **HIFLD Open was discontinued on 2025-08-26.** Its website and portal went offline on 2025-09-16. *Verified:*
  NAPSG Foundation notice; Data Lumos archive record.
- Some layers moved to HIFLD Secure. Secure needs Login.gov, a GII account, a data use agreement and a federal
  sponsor. Whether transmission lines are among them is *unverified*.
- **HIFLD Next** (hifld.publicenvirodata.org, Public Environmental Data Partners) re-publishes the archived layers as
  bulk files. It launched in March 2026.

## Transmission lines: sources, best first

### 1. HIFLD, final release — explorer source `hifld` (default)

Esri-hosted archive of the last HIFLD release.

- Service: `https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/US_Electric_Power_Transmission_Lines/FeatureServer/0`
- Release: data currency **2024-09-30**, 94,619 lines nationwide (69–765 kV). maxRecordCount 2000.
- Fields: ID, TYPE, STATUS, OWNER, VOLTAGE, VOLT_CLASS, INFERRED, SUB_1, SUB_2, SOURCE, SOURCEDATE, VAL_METHOD,
  VAL_DATE.
  - `-999999` marks a missing number and `NOT AVAILABLE` a missing text value. The explorer treats both as unknown.
- **152 lines intersect the WUP box** (*verified*). The bbox includes Marquette County and northern Wisconsin; the
  explorer clips to the study area.
  - Voltage: 69 kV (69 lines), 138 kV (36), unknown (31), 115 kV (14), 345 kV (2).
  - Owner: NOT AVAILABLE (61), Northern States Power Co – Wisconsin (50), American Transmission Co (32), City of
    Marquette (5), Northern States Power Co (3), Upper Peninsula Power Co (1).
- **Owner is weak in the WUP** (*verified* from the attributes). Every 69 kV line in Keweenaw, Houghton and Baraga
  has OWNER = NOT AVAILABLE. Their substations include Atlantic, Keweenaw, Osceola, Portage, MTU, Elevation St,
  Baraga Vlg and JH Warden.
- The same release, two other ways:
  - **HIFLD Next bulk files** (GeoParquet 44 MB, GPKG, SHP, GeoJSON):
    `https://storage.googleapis.com/hifld-next-datasets-prod/hifld/transmission-lines-1/transmission-lines-1/v1.0.0/`
    The explorer imports the GeoJSON or GPKG (GPKG needs pyogrio).
  - **Data Lumos / ICPSR archive.** Cite it as: Oak Ridge National Laboratory and U.S. Department of Homeland
    Security. *HIFLD OPEN Transmission Lines.* Ann Arbor, MI: ICPSR, 2025-11-24.
    https://doi.org/10.3886/E240591V1
- Licence: U.S. Government work (data.gov). The Esri item itself lists the Esri Master License Agreement.

### 2. NETL Energy Transition Atlas copy — explorer source `netl` (backup)

- Service: `https://arcgis.netl.doe.gov/server/rest/services/Hosted/EAT_Online_2025_Update_WFL1/FeatureServer/12`
- Same 152 lines in the box (*verified*). Fewer fields: no SUB_1, SUB_2 or INFERRED.

### 3. OpenStreetMap — explorer source `osm` (cross-check)

- The explorer downloads `power=line` and `power=cable` ways through the Overpass API. It tries overpass-api.de
  first, then mirrors.
- Licence: ODbL. Credit "© OpenStreetMap contributors"; in print add openstreetmap.org/copyright. Derived databases
  are share-alike.
- Statewide, 95% of Michigan's `power=line` ways carry `voltage` and 62% carry `operator` (Geofabrik taginfo,
  2026-10-20, *verified*).
- WUP box (a third-party OpenInfraMap copy, 2026-10-20; *verified* but not authoritative):
  - 532 segments are ≥ 69 kV, and 51% of them have no operator.
  - The operators named are American Transmission Company, Xcel Energy and UPPCO.
  - A few voltages are obviously wrong (0, 2 kV).
- No source assesses OSM completeness for the Upper Peninsula. Use OSM to fill and check HIFLD gaps, not on its own.
- The Overpass endpoints could not be reached from this cloud workspace. They were not tested here, so the
  self-test checks them on your machine.

### 4. Old HIFLD Open service — do not use

- `services1.arcgis.com/Hp6G80Pky0om7QvQ/.../Electric_Power_Transmission_Lines/FeatureServer/0`
- Last edit 2023-09-05. It has 52 lines in the WUP box and **no 69 kV lines**.
- The NIFC EGP "Transmission Lines (HIFLD)" item still points here.

### Not available as GIS

- **ATC** (owns most UP transmission; *unverified* that it owns most WUP lines) publishes only PDF maps:
  - Files: 2025 Ten-Year Assessment, Zone 2 *Existing Facilities* and *Projects*
    (`atc10yearplan.com/wp-content/uploads/2025/11/TYA-Zone2_Existing_Facilities.pdf`).
  - Use them to check owners and voltages by eye.
  - Zone 2 covers Houghton, Keweenaw, Baraga, Iron, "Gogebic East" and "Ontonagon East".
  - It lists a provisional *Western UP Reliability Project* (2030).
- **EIA Energy Atlas**: no transmission layer of its own; the EIA FAQ points to HIFLD.
- **MPSC / State of Michigan GIS**: none. The MPSC *Electric Utility Service Areas* layer can help assign owners:
  `https://services3.arcgis.com/943LBv9FP414WfDO/arcgis/rest/services/ELECTRIC_UTILITY_SERVICE_AREA_MI_WFL1/FeatureServer`
- **MISO, PNNL, NREL, Global Energy Monitor**: none for this area. Utility line data is often treated as CEII
  (critical energy infrastructure information) (*unverified*).
- **Distribution lines**: no public layer.

### A six-county copy already at MTU

- MTU's portal hosts a public six-county clip of HIFLD with all attributes:
  `https://portal1-geo.sabu.mtu.edu/server/rest/services/Hosted/WUPElectricPowerTransmissionLines/FeatureServer/62`
- Details: 53 lines, Albers EPSG:6350, max VAL_DATE 2021-02-02.
- It may be your own or a colleague's. Import it (export it as GeoJSON in WGS84), or download `hifld` here; both
  come from the same release lineage.
- The explorer's bundled layer comes from Wes's ArcGIS project (TransmissionLines.aprx, 2024-11-04). It has 75
  county-split pieces, ~804 km, and geometry only, because the county spatial join emptied the HIFLD attribute
  columns.

## Recommended workflow

1. Lines tab › *Download current lines…* › **HIFLD final release** (or `WUP_Explorer.exe --lines-download hifld`).
2. *Download current lines…* › **OpenStreetMap**.
3. Select the HIFLD layer › *Fill gaps from* the OSM layer › **Cross-check → new layer**.
   - Owner and voltage are filled only where HIFLD has none and at least half of the line lies within 150 m of an
     OSM line.
   - Voltage disagreements are flagged.
4. Check the filled owners against ATC's Zone 2 map.
5. *Analyse lines* on the cross-checked layer. `--full-record` also analyses every layer at the end.
