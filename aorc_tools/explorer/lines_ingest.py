"""
Download current power-line layers for the study area (runs on the user's machine; needs internet).

* OpenStreetMap through the Overpass API: power=line / cable / minor_line ways with their voltage, operator, name,
  ref, circuits and cables tags (ODbL; "© OpenStreetMap contributors"). Mirrors are tried in turn.
* ArcGIS REST feature services (HIFLD / EIA U.S. Energy Atlas transmission lines): the study-area envelope is
  queried page by page as GeoJSON.

Each download becomes a workspace line layer (lines.LineLayers.save) with its source, date and attribution recorded.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
USER_AGENT = "WUP-Explorer/2 (wildfire research; Michigan Technological University)"

# Verified 2026-10-21 (docs/lines_data_sources.md). HIFLD Open shut down on 2025-08-26; the final HIFLD release
# (data currency 2024-09-30, 94,619 lines) lives on as an Esri-hosted archive and as HIFLD Next bulk files.
SOURCES = {
    "hifld": {"label": "HIFLD transmission lines, final release (data to 2024-09-30)", "kind": "arcgis",
              "url": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/US_Electric_Power_Transmission_Lines/FeatureServer/0",
              "description": "The last HIFLD release (ORNL/DHS; 69-765 kV; OWNER, VOLTAGE, VOLT_CLASS, SUB_1/SUB_2, STATUS) from "
                             "Esri's archived copy. Owner is 'NOT AVAILABLE' for many WUP 69 kV lines: cross-check with OpenStreetMap.",
              "attribution": "HIFLD Electric Power Transmission Lines (ORNL / DHS), final release 2024-09-30; archived copy hosted by "
                             "Esri Federal User Community",
              "license": "U.S. Government work (https://www.usa.gov/government-works)"},
    "netl": {"label": "NETL Energy Transition Atlas copy of HIFLD (backup)", "kind": "arcgis",
             "url": "https://arcgis.netl.doe.gov/server/rest/services/Hosted/EAT_Online_2025_Update_WFL1/FeatureServer/12",
             "description": "DOE NETL's mirror of the same HIFLD lines (fewer fields: no SUB_1/SUB_2). Use if the Esri archive is down.",
             "attribution": "HIFLD Electric Power Transmission Lines via DOE NETL Energy Transition Atlas (2025 update)",
             "license": "U.S. Government work (https://www.usa.gov/government-works)"},
    "osm": {"label": "OpenStreetMap (Overpass)", "kind": "osm",
            "description": "power=line and power=cable (transmission and sub-transmission) with voltage and operator tags; "
                           "edited continuously by mappers. "
                           "© OpenStreetMap contributors (ODbL).",
            "attribution": "© OpenStreetMap contributors (ODbL 1.0)", "license": "ODbL 1.0"},
}


def _session(session=None):
    if session is not None:
        return session
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def overpass_query(bbox, kinds=("line", "cable"), timeout=180) -> str:
    w, s, e, n = bbox
    rx = "|".join(kinds)
    return (f'[out:json][timeout:{timeout}];(way["power"~"^({rx})$"]({s:.5f},{w:.5f},{n:.5f},{e:.5f}););out tags geom;')


def overpass_to_features(js: dict) -> list[dict]:
    feats = []
    for el in js.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        coords = [[p["lon"], p["lat"]] for p in el["geometry"] if p]
        if len(coords) < 2:
            continue
        tags = dict(el.get("tags") or {})
        tags["@id"] = f"way/{el['id']}"
        feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": tags})
    return feats


def fetch_osm(bbox, session=None, progress=None, waits=(10, 30, 90), kinds=("line", "cable")) -> tuple[list, dict]:
    sess = _session(session)
    q = overpass_query(bbox, kinds)
    errors = []
    for attempt, wait in enumerate((0,) + tuple(waits)):
        if wait:
            time.sleep(wait)
        for url in OVERPASS_ENDPOINTS:
            if progress:
                progress(attempt, len(waits) + 1, f"asking {url.split('/')[2]}")
            try:
                r = sess.post(url, data={"data": q}, timeout=240)
                if r.status_code in (429, 504, 502, 503):
                    errors.append(f"{url}: HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                js = r.json()
                feats = overpass_to_features(js)
                stamp = (js.get("osm3s") or {}).get("timestamp_osm_base")
                return feats, {"endpoint": url, "osm_timestamp": stamp, "query": q}
            except Exception as e:                                       # noqa: BLE001 - try the next mirror
                errors.append(f"{url}: {type(e).__name__}: {e}")
    raise ConnectionError("OpenStreetMap (Overpass) not reachable: " + "; ".join(errors[-3:]))


def fetch_arcgis(url, bbox, session=None, progress=None, page=1000, where="1=1", out_fields="*") -> tuple[list, dict]:
    """All features of an ArcGIS FeatureServer/MapServer layer intersecting ``bbox`` (lon/lat), as GeoJSON."""
    sess = _session(session)
    w, s, e, n = bbox
    base = url.rstrip("/")
    feats, offset = [], 0
    info = {}
    try:
        r = sess.get(base, params={"f": "json"}, timeout=60)
        r.raise_for_status()
        js = r.json()
        info = {"name": js.get("name"), "max_record_count": js.get("maxRecordCount"),
                "last_edit": (js.get("editingInfo") or {}).get("lastEditDate")}
        page = min(page, int(js.get("maxRecordCount") or page))
    except Exception as ex:                                              # noqa: BLE001 - metadata is optional
        logger.info("layer info not available (%s)", ex)
    while True:
        params = {"where": where, "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                  "spatialRel": "esriSpatialRelIntersects", "outFields": out_fields, "outSR": 4326, "f": "geojson",
                  "resultOffset": offset, "resultRecordCount": page, "returnGeometry": "true"}
        last = None
        for wait in (0, 5, 20, 60):
            if wait:
                time.sleep(wait)
            try:
                r = sess.get(base + "/query", params=params, timeout=120)
                r.raise_for_status()
                js = r.json()
                if "error" in js:
                    raise RuntimeError(js["error"].get("message", str(js["error"])))
                break
            except Exception as ex:                                      # noqa: BLE001
                last = ex
        else:
            raise ConnectionError(f"{base}: {type(last).__name__}: {last}")
        got = js.get("features", [])
        feats += got
        if progress:
            progress(len(feats), len(feats) + (page if got else 0), f"{len(feats)} features")
        more = js.get("exceededTransferLimit") or (js.get("properties") or {}).get("exceededTransferLimit")
        if not got or (not more and len(got) < page):
            break
        offset += len(got)
    return feats, {"endpoint": base, **info}


def download(layers, source_id, session=None, progress=None) -> dict:
    """Fetch ``source_id`` for the study area and save it as workspace layer ``<source_id>-<date>``."""
    if source_id not in SOURCES:
        raise ValueError(f"unknown line source {source_id}; choose from {sorted(SOURCES)}")
    src = SOURCES[source_id]
    bbox = layers._bbox
    if src["kind"] == "osm":
        feats, meta = fetch_osm(bbox, session=session, progress=progress)
    else:
        feats, meta = fetch_arcgis(src["url"], bbox, session=session, progress=progress)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    lid = f"{source_id}-{day}"
    return layers.save(lid, feats, {"name": f"{src['label']} ({day[:4]}-{day[4:6]}-{day[6:]})", "kind": "transmission",
                                    "source": src["description"], "attribution": src["attribution"], "license": src["license"],
                                    "downloaded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "fetch": {k: v for k, v in meta.items() if k != "query"},
                                    "query": meta.get("query")}, source_tag=source_id)


def sources_payload():
    return [{"id": k, "label": v["label"], "description": v["description"]} for k, v in SOURCES.items()]
