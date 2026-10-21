"""
AORC grid geometry: cells, bricks (Zarr chunks) and packets.

Everything here is derived at runtime from ``points_index.csv`` (which was
itself generated from the real AORC coordinate arrays), never from a
hard-coded origin. The one assumption is the AORC v1.1 grid step of
1/120 degree (30 arc-seconds), which is a documented property of the dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from aorc_tools.config import AORC_RESOLUTION

# Zarr chunk layout of the NOAA AORC v1.1 yearly stores (verified against the
# real store in the Pinery case-study work: chunks are 144 time x 128 lat x
# 256 lon for every variable).
CHUNK_TIME = 144
CHUNK_LAT = 128
CHUNK_LON = 256

STEP = AORC_RESOLUTION  # 1/120 degree


@dataclass(frozen=True)
class Brick:
    """One spatial Zarr chunk (ci = lat chunk index, cj = lon chunk index)."""

    ci: int
    cj: int

    @property
    def lat_idx_range(self) -> tuple[int, int]:
        return self.ci * CHUNK_LAT, (self.ci + 1) * CHUNK_LAT

    @property
    def lon_idx_range(self) -> tuple[int, int]:
        return self.cj * CHUNK_LON, (self.cj + 1) * CHUNK_LON

    @property
    def key(self) -> str:
        return f"{self.ci}_{self.cj}"


class Grid:
    """The AORC cell grid restricted to the study-area point index."""

    def __init__(self, points_df: pd.DataFrame):
        required = {"point_id", "latitude", "longitude", "lat_idx", "lon_idx"}
        missing = required - set(points_df.columns)
        if missing:
            raise ValueError(f"points_index is missing columns: {sorted(missing)}")

        self.step = STEP
        self.points = points_df.reset_index(drop=True).copy()
        self.points["point_id"] = self.points["point_id"].astype(int)
        self.points["lat_idx"] = self.points["lat_idx"].astype(int)
        self.points["lon_idx"] = self.points["lon_idx"].astype(int)

        # Resolve the grid origin from the data itself (median removes the
        # ~1e-6 degree floating noise present in the AORC coordinate arrays).
        self.lat0 = float(np.median(self.points["latitude"] - self.points["lat_idx"] * STEP))
        self.lon0 = float(np.median(self.points["longitude"] - self.points["lon_idx"] * STEP))

        resid_lat = np.abs(self.points["latitude"] - (self.lat0 + self.points["lat_idx"] * STEP)).max()
        resid_lon = np.abs(self.points["longitude"] - (self.lon0 + self.points["lon_idx"] * STEP)).max()
        if resid_lat > STEP / 10 or resid_lon > STEP / 10:
            raise ValueError(
                "points_index coordinates are not on a 1/120-degree grid "
                f"(max residual lat {resid_lat:.2e}, lon {resid_lon:.2e})"
            )

        self._pid_to_row = pd.Series(self.points.index.values, index=self.points["point_id"].values)
        self.points["ci"] = self.points["lat_idx"] // CHUNK_LAT
        self.points["cj"] = self.points["lon_idx"] // CHUNK_LON
        self.points["li"] = self.points["lat_idx"] % CHUNK_LAT
        self.points["lj"] = self.points["lon_idx"] % CHUNK_LON

        # county + time zone per cell (used for local-time display and LST daily views)
        try:
            from aorc_tools.explorer.geo import cell_attributes
            attrs = cell_attributes(self.points)
            for k, v in attrs.items():
                self.points[k] = v
        except Exception as e:  # geography is a convenience; never block the grid
            import logging
            logging.getLogger(__name__).warning("county assignment unavailable (%s); defaulting to Eastern", e)
            self.points["county"] = "unknown"
            self.points["tz"] = "America/Detroit"
            self.points["std_offset_h"] = -5
            self.points["zone"] = "ET"

        self.bbox = (
            float(self.points["longitude"].min() - STEP / 2),
            float(self.points["latitude"].min() - STEP / 2),
            float(self.points["longitude"].max() + STEP / 2),
            float(self.points["latitude"].max() + STEP / 2),
        )
        self.lat_idx_range = (int(self.points["lat_idx"].min()), int(self.points["lat_idx"].max()) + 1)
        self.lon_idx_range = (int(self.points["lon_idx"].min()), int(self.points["lon_idx"].max()) + 1)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    @property
    def n_cells(self) -> int:
        return len(self.points)

    def rows_for(self, point_ids) -> np.ndarray:
        """Row positions (into ``self.points``) for a list of point ids."""
        pids = np.asarray(list(point_ids), dtype=int)
        unknown = pids[~np.isin(pids, self._pid_to_row.index.values)]
        if len(unknown):
            raise KeyError(f"unknown point_id(s): {unknown[:5].tolist()}{'...' if len(unknown) > 5 else ''}")
        return self._pid_to_row.loc[pids].values

    def cell_bounds(self, point_id: int) -> tuple[float, float, float, float]:
        r = self.points.iloc[self.rows_for([point_id])[0]]
        return (r.longitude - STEP / 2, r.latitude - STEP / 2, r.longitude + STEP / 2, r.latitude + STEP / 2)

    def cells_in_bbox(self, lon_min, lat_min, lon_max, lat_max) -> list[int]:
        p = self.points
        m = (p.longitude >= lon_min) & (p.longitude <= lon_max) & (p.latitude >= lat_min) & (p.latitude <= lat_max)
        return p.loc[m, "point_id"].tolist()

    def cells_in_brick(self, ci: int, cj: int) -> list[int]:
        p = self.points
        return p.loc[(p.ci == ci) & (p.cj == cj), "point_id"].tolist()

    def bricks(self) -> list[dict]:
        """Every brick that intersects the study area, with its clipped extent."""
        out = []
        for (ci, cj), g in self.points.groupby(["ci", "cj"]):
            b = Brick(int(ci), int(cj))
            la0, la1 = b.lat_idx_range
            lo0, lo1 = b.lon_idx_range
            out.append({
                "ci": b.ci, "cj": b.cj, "key": b.key,
                "n_cells": int(len(g)),
                # full chunk extent in degrees (cell edges)
                "full_bbox": [self.lon0 + lo0 * STEP - STEP / 2, self.lat0 + la0 * STEP - STEP / 2,
                              self.lon0 + lo1 * STEP - STEP / 2, self.lat0 + la1 * STEP - STEP / 2],
                # extent of the study-area cells inside the chunk
                "clip_bbox": [float(g.longitude.min() - STEP / 2), float(g.latitude.min() - STEP / 2),
                              float(g.longitude.max() + STEP / 2), float(g.latitude.max() + STEP / 2)],
                "lat_idx_range": [int(g.lat_idx.min()), int(g.lat_idx.max()) + 1],
                "lon_idx_range": [int(g.lon_idx.min()), int(g.lon_idx.max()) + 1],
            })
        return out

    def brick_of(self, point_id: int) -> Brick:
        r = self.points.iloc[self.rows_for([point_id])[0]]
        return Brick(int(r.ci), int(r.cj))

    # ------------------------------------------------------------------
    # Time chunks
    # ------------------------------------------------------------------

    @staticmethod
    def time_chunks(start: datetime, end: datetime, origin_of_year=None) -> list[dict]:
        """Which 144-hour Zarr time chunks a [start, end] window touches.

        ``origin_of_year(year)`` returns the first timestamp of that year's
        store (Jan 1 00:00 for every year except 1979, whose store begins
        1979-02-01). Each year is a separate store, so chunk indices restart
        at the year boundary.
        """
        if origin_of_year is None:
            origin_of_year = lambda y: datetime(y, 1, 1)  # noqa: E731
        out = []
        t = start
        while t <= end:
            t0 = origin_of_year(t.year)
            k = int((t - t0).total_seconds() // 3600) // CHUNK_TIME
            c_start = t0 + timedelta(hours=k * CHUNK_TIME)
            c_end = c_start + timedelta(hours=CHUNK_TIME - 1)
            year_end = datetime(t.year, 12, 31, 23)
            if c_end > year_end:
                c_end = year_end
            out.append({"year": t.year, "k": k, "start": c_start, "end": c_end})
            t = c_end + timedelta(hours=1)
        return out

    @staticmethod
    def packet_id(point_id: int, when: datetime) -> str:
        return f"{when.strftime('%Y-%m-%dT%HZ')}/p{point_id}"

    # ------------------------------------------------------------------
    # Summary for the client
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        return {
            "step_deg": STEP,
            "lat0": self.lat0,
            "lon0": self.lon0,
            "n_cells": self.n_cells,
            "bbox": list(self.bbox),
            "lat_idx_range": list(self.lat_idx_range),
            "lon_idx_range": list(self.lon_idx_range),
            "chunk": {"time": CHUNK_TIME, "lat": CHUNK_LAT, "lon": CHUNK_LON},
            "bricks": self.bricks(),
            "counties": self.points.groupby("county").size().astype(int).to_dict(),
            "zones": self.points.groupby("zone").size().astype(int).to_dict(),
            "approx_cell_size_m": {
                "north_south": round(111_320 * STEP, 0),
                "east_west_at_mean_lat": round(111_320 * STEP * float(np.cos(np.radians(self.points.latitude.mean()))), 0),
            },
        }


def load_grid(points_csv) -> Grid:
    return Grid(pd.read_csv(points_csv))
