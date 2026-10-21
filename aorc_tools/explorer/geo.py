"""
Static geography shipped with the explorer (``static/geo``):

* ``wup_counties.geojson`` -- the six study counties with their time zone
  (US Census 2010 cartographic boundaries, 1:20 M; Gogebic and Iron on Central
  Time, the other four on Eastern Time).
* ``wup_outline.geojson``  -- the study-area outline from the repo shapefile,
  used as the offline basemap.

Each AORC cell is assigned a county by point-in-polygon on its centre; the
~4 % of coastal cells that the generalised county outlines miss are given the
nearest county. County -> time zone comes from ``timebase.COUNTY_TZ``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np

from aorc_tools.explorer.timebase import COUNTY_TZ

GEO_DIR = Path(__file__).resolve().parent / "static" / "geo"


@lru_cache(maxsize=4)
def load_geojson(name: str) -> dict:
    return json.loads((GEO_DIR / f"{name}.geojson").read_text())


def assign_counties(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    from shapely import contains_xy
    from shapely.geometry import Point, shape

    gj = load_geojson("wup_counties")
    polys = [(f["properties"]["name"], shape(f["geometry"])) for f in gj["features"]]
    county = np.full(len(lat), "", dtype=object)
    for name, g in polys:
        m = contains_xy(g, lon, lat)
        county[m & (county == "")] = name
    for i in np.where(county == "")[0]:
        p = Point(float(lon[i]), float(lat[i]))
        county[i] = min(polys, key=lambda t: t[1].distance(p))[0]
    return county


def cell_attributes(points) -> dict:
    """county, IANA zone, standard offset (h) and CT/ET label per cell."""
    county = assign_counties(points["latitude"].values, points["longitude"].values)
    tz = np.array([COUNTY_TZ[c][0] for c in county], dtype=object)
    off = np.array([COUNTY_TZ[c][1] for c in county], dtype=np.int8)
    zone = np.array([COUNTY_TZ[c][2] for c in county], dtype=object)
    return {"county": county, "tz": tz, "std_offset_h": off, "zone": zone}
