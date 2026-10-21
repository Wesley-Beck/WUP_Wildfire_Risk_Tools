"""
Synthetic MODIS NDVI composites for offline tests of explorer.ndvi_ingest:
GeoTIFFs on the real MODIS sinusoidal projection (231.656 m pixels) over the WUP,
an NDVI field with a known formula, a pixel-reliability band with a cloudy patch,
and STAC-like item objects pointing at the files (the Planetary Computer code path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.ndvi_ingest import SIN_R, latlon_to_sinusoidal, sinusoidal_to_latlon

SINU = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
PIX = 231.656358263889


def ndvi_field(lat, lon, day: pd.Timestamp):
    g = np.clip(np.sin(np.pi * (day.dayofyear - 100) / 200), 0, 1)
    return 0.25 + 0.55 * g + 0.04 * np.sin(np.radians(lon) * 40) + 0.03 * np.cos(np.radians(lat) * 50)


def cloudy(lat, lon, day):
    # a cloudy patch that moves with the composite, ~10 % of the area
    c_lat = 46.3 + (day.dayofyear % 64) / 64 * 1.6
    return (np.abs(lat - c_lat) < 0.12) & (lon > -89.6) & (lon < -88.6)


def write_composite(folder: Path, bbox, start: pd.Timestamp, step=PIX):
    import rasterio
    from rasterio.transform import from_origin
    lon0, lat0, lon1, lat1 = bbox
    xs, ys = latlon_to_sinusoidal(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]))
    x0, x1, y0, y1 = min(xs) - step, max(xs) + step, min(ys) - step, max(ys) + step
    nx, ny = int((x1 - x0) / step) + 1, int((y1 - y0) / step) + 1
    tr = from_origin(x0, y1, step, step)
    jj, ii = np.meshgrid(np.arange(nx) + 0.5, np.arange(ny) + 0.5)
    lat, lon = sinusoidal_to_latlon(x0 + jj * step, y1 - ii * step)
    mid = start + pd.Timedelta(days=8)
    nd = np.round(ndvi_field(lat, lon, mid) / 1e-4).astype(np.int16)
    rel = np.where(cloudy(lat, lon, mid), 3, 0).astype(np.int16)
    tag = f"A{start.year}{start.dayofyear:03d}"
    paths = {}
    for name, arr in (("250m_16_days_NDVI", nd), ("250m_16_days_pixel_reliability", rel)):
        p = folder / f"MOD13Q1.{tag}.h11v04.061.{name}.tif"
        with rasterio.open(p, "w", driver="GTiff", width=nx, height=ny, count=1, dtype="int16", crs=SINU, transform=tr,
                           compress="deflate", tiled=True) as dst:
            dst.write(arr, 1)
        paths[name] = p
    return paths


@dataclass
class Asset:
    href: str


@dataclass
class Item:
    id: str
    datetime: pd.Timestamp
    properties: dict = field(default_factory=dict)
    assets: dict = field(default_factory=dict)


def make_items(folder: Path, bbox, starts):
    folder.mkdir(parents=True, exist_ok=True)
    items = []
    for s in starts:
        p = write_composite(folder, bbox, s)
        items.append(Item(id=f"MOD13Q1.A{s.year}{s.dayofyear:03d}.h11v04.061", datetime=s,
                          properties={"start_datetime": s.isoformat() + "Z", "platform": "terra"},
                          assets={k: Asset(str(v)) for k, v in p.items()}))
    return items
