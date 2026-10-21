"""
Raw-data sources for the explorer.

A source answers one question: "give me the raw AORC variables for these cells
over this hourly window". Two implementations:

* ``AORCSource``      -- the real NOAA AORC v1.1 Zarr stores on S3, fetched one
                         *brick* (144 h x 128 lat x 256 lon chunk, clipped to the
                         study area) at a time and cached on disk, so repeated
                         clicks inside the same brick/time chunk never hit S3 twice.
* ``SyntheticSource`` -- deterministic, physically plausible fake weather for
                         demos and tests, including a few injected gaps so the
                         resolution ladder can be seen working. Clearly labelled.

Every brick/time-chunk passes through ``explorer.ladder.fill_brick`` (unless
gap filling is switched off), so each packet carries a weather tier: 0 = its own
value, 1 = time-interpolated, 2/3 = 3x3 / 7x7 neighbourhood, 4 = brick mean,
9 = unavailable.

Both return a ``RawWindow`` of shape (n_hours, n_cells) per variable, in the
raw AORC units the rest of the pipeline expects (K, kg/kg, Pa, m/s, mm, W/m2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time as _time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.grid import CHUNK_TIME, Brick, Grid
from aorc_tools.explorer.ladder import fill_brick

logger = logging.getLogger(__name__)

# The seven AORC variables the index chain consumes. DLWRF is in the archive
# but no index uses it, so the explorer does not fetch it.
VARIABLES = [
    "TMP_2maboveground",
    "SPFH_2maboveground",
    "PRES_surface",
    "UGRD_10maboveground",
    "VGRD_10maboveground",
    "APCP_surface",
    "DSWRF_surface",
]
CHAIN_VARS = VARIABLES[:6]   # DSWRF is carried for reference only


@dataclass
class RawWindow:
    times: pd.DatetimeIndex                 # hourly, UTC (naive)
    point_ids: np.ndarray                   # (n_cells,)
    data: dict                              # var -> float32 (n_hours, n_cells)
    source: str                             # "aorc-s3" | "synthetic"
    tiers: np.ndarray | None = None         # uint8 (n_hours, n_cells): worst weather tier
    tier_counts: dict = field(default_factory=dict)   # var -> {tier: packets}
    bricks: list = field(default_factory=list)
    time_chunks: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def n_hours(self) -> int:
        return len(self.times)

    @property
    def n_cells(self) -> int:
        return len(self.point_ids)


# ---------------------------------------------------------------------------
# Brick cache (real source)
# ---------------------------------------------------------------------------

class BrickCache:
    """LRU on-disk cache of clipped brick/time-chunk arrays (.npz)."""

    def __init__(self, cache_dir, max_gb: float = 8.0):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_gb * 1024 ** 3)

    def path(self, year: int, k: int, brick: Brick) -> Path:
        return self.dir / str(year) / f"k{k:03d}_{brick.key}.npz"

    def get(self, year, k, brick):
        p = self.path(year, k, brick)
        if not p.exists():
            return None
        try:
            with np.load(p, allow_pickle=False) as z:
                out = {v: z[v] for v in z.files if v in VARIABLES}
                times = pd.to_datetime(z["times_ns"].astype("int64"))
                meta = json.loads(str(z["meta"]))
            p.touch()
            return out, times, meta
        except Exception as e:
            logger.warning("cache read failed for %s (%s); refetching", p.name, e)
            p.unlink(missing_ok=True)
            return None

    def put(self, year, k, brick, arrays: dict, times: pd.DatetimeIndex, meta: dict):
        p = self.path(year, k, brick)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.stem + f".{threading.get_ident()}.tmp.npz")
        np.savez_compressed(tmp, times_ns=times.values.astype("datetime64[ns]").astype("int64"),
                            meta=np.array(json.dumps(meta)), **{v: a.astype(np.float32) for v, a in arrays.items()})
        tmp.replace(p)
        self._evict()

    def _evict(self):
        files = sorted(self.dir.rglob("*.npz"), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        while total > self.max_bytes and files:
            f = files.pop(0)
            total -= f.stat().st_size
            f.unlink(missing_ok=True)

    def usage(self) -> dict:
        files = list(self.dir.rglob("*.npz"))
        return {"files": len(files), "bytes": sum(f.stat().st_size for f in files),
                "max_bytes": self.max_bytes, "dir": str(self.dir)}


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class BaseSource:
    name = "base"
    description = ""
    synthetic = False

    def __init__(self):
        self._filled: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def origin_of_year(self, year: int) -> datetime:
        return datetime(1979, 2, 1) if year == 1979 else datetime(year, 1, 1)

    def status(self) -> dict:
        return {"name": self.name, "description": self.description, "synthetic": self.synthetic}

    def _raw_brick_chunk(self, grid: Grid, year: int, k: int, brick: Brick):
        """-> (arrays var->(n_t, ny, nx), times, meta{lat_idx_range, lon_idx_range}, cache_hit)"""
        raise NotImplementedError

    def brick_chunk(self, grid: Grid, year: int, k: int, brick: Brick, gap_fill: bool = True):
        """Study cells of one brick for one time chunk, after the resolution ladder.

        Returns dict(times, point_ids, data var->(n_t, n_cells), tiers (n_t, n_cells),
        tier_counts, hit, meta)."""
        key = (year, k, brick.key, bool(gap_fill))
        with self._lock:
            if key in self._filled:
                self._filled.move_to_end(key)
                out = dict(self._filled[key]); out["hit"] = True
                return out
        arrays, times, meta, hit = self._raw_brick_chunk(grid, year, k, brick)
        sub = grid.points[(grid.points.ci == brick.ci) & (grid.points.cj == brick.cj)]
        la0, lo0 = meta["lat_idx_range"][0], meta["lon_idx_range"][0]
        ny = meta["lat_idx_range"][1] - la0
        nx = meta["lon_idx_range"][1] - lo0
        li, lj = sub.lat_idx.values - la0, sub.lon_idx.values - lo0
        study = np.zeros((ny, nx), dtype=bool)
        study[li, lj] = True
        if gap_fill:
            arrays, worst, counts = fill_brick(arrays, study, CHAIN_VARS)
            tiers = worst[:, li, lj]
        else:
            tiers = np.where(np.any([~np.isfinite(arrays[v][:, li, lj]) for v in CHAIN_VARS if v in arrays], axis=0),
                             9, 0).astype(np.uint8)
            counts = {v: {0: int(np.isfinite(arrays[v][:, li, lj]).sum()),
                          9: int((~np.isfinite(arrays[v][:, li, lj])).sum())} for v in CHAIN_VARS if v in arrays}
        out = {"times": times, "point_ids": sub.point_id.values.astype(int),
               "data": {v: np.ascontiguousarray(a[:, li, lj]) for v, a in arrays.items()},
               "tiers": tiers.astype(np.uint8), "tier_counts": counts, "meta": meta}
        with self._lock:
            self._filled[key] = out
            while len(self._filled) > 6:
                self._filled.popitem(last=False)
        out = dict(out); out["hit"] = hit
        return out

    def fetch(self, grid: Grid, point_ids, start: datetime, end: datetime, gap_fill: bool = True) -> RawWindow:
        t_start = _time.perf_counter()
        pids = np.asarray(point_ids, dtype=int)
        rows = grid.rows_for(pids)
        sub = grid.points.iloc[rows]
        times = pd.date_range(start, end, freq="h")
        chunks = grid.time_chunks(start, end, self.origin_of_year)
        data = {v: np.full((len(times), len(pids)), np.nan, dtype=np.float32) for v in VARIABLES}
        tiers = np.full((len(times), len(pids)), 9, dtype=np.uint8)
        counts: dict = {}
        request_pos = {int(p): i for i, p in enumerate(pids)}
        bricks_touched, hits, misses, n_read = [], 0, 0, 0
        for (ci, cj), g in sub.groupby(["ci", "cj"]):
            brick = Brick(int(ci), int(cj))
            bricks_touched.append(brick.key)
            out_cols = np.array([request_pos[int(p)] for p in g.point_id.values])
            for ch in chunks:
                bc = self.brick_chunk(grid, ch["year"], ch["k"], brick, gap_fill)
                hits += int(bc["hit"]); misses += int(not bc["hit"])
                col = pd.Index(bc["point_ids"]).get_indexer(g.point_id.values)
                ctimes = bc["times"]
                t_mask = (ctimes >= times[0]) & (ctimes <= times[-1])
                if not t_mask.any():
                    continue
                dst = times.get_indexer(ctimes[t_mask])
                for v in VARIABLES:
                    if v in bc["data"]:
                        data[v][dst[:, None], out_cols[None, :]] = bc["data"][v][t_mask][:, col]
                tiers[dst[:, None], out_cols[None, :]] = bc["tiers"][t_mask][:, col]
                n_read += len(g) * int(t_mask.sum())
        for t in np.unique(tiers):
            counts[int(t)] = int((tiers == t).sum())
        return RawWindow(times=times, point_ids=pids, data=data, source=self.name, tiers=tiers, tier_counts=counts,
                         bricks=sorted(set(bricks_touched)),
                         time_chunks=[{"year": c["year"], "k": c["k"], "start": c["start"].isoformat(),
                                       "end": c["end"].isoformat()} for c in chunks],
                         stats={"cache_hits": hits, "cache_misses": misses, "packets": int(n_read),
                                "seconds": round(_time.perf_counter() - t_start, 3)})


# ---------------------------------------------------------------------------
# Synthetic (demo / tests)
# ---------------------------------------------------------------------------

class SyntheticSource(BaseSource):
    """Deterministic fake weather: diurnal + seasonal cycles, smooth spatial
    gradients, multi-hour rain events coherent across neighbouring cells, and
    a few injected gaps (isolated masked packets, a masked hour slice, a
    multi-hour cluster outage) shaped like the April-2026 AORC masking advisory.

    The numbers are NOT real. Every API response carries ``synthetic: true``."""

    name = "synthetic"
    synthetic = True
    description = ("Synthetic AORC-shaped weather (demo only - not real data), with injected gaps to exercise "
                   "the resolution ladder. Same chunk/brick geometry and units as the real archive.")

    def __init__(self, seed: int = 20260520, inject_gaps: bool = True):
        super().__init__()
        self.seed = seed
        self.inject_gaps = inject_gaps

    def _rng(self, *parts):
        h = hashlib.sha256(("|".join(map(str, parts)) + f"|{self.seed}").encode()).digest()
        return np.random.default_rng(int.from_bytes(h[:8], "little"))

    def _raw_brick_chunk(self, grid: Grid, year: int, k: int, brick: Brick):
        from aorc_tools.explorer.timebase import solar_elevation

        t0 = self.origin_of_year(year) + timedelta(hours=k * CHUNK_TIME)
        times = pd.date_range(t0, periods=CHUNK_TIME, freq="h")
        times = times[times.year == year]
        n_t = len(times)
        sub = grid.points[(grid.points.ci == brick.ci) & (grid.points.cj == brick.cj)]
        la0, la1 = int(sub.lat_idx.min()), int(sub.lat_idx.max()) + 1
        lo0, lo1 = int(sub.lon_idx.min()), int(sub.lon_idx.max()) + 1
        n_lat, n_lon = la1 - la0, lo1 - lo0
        lat = grid.lat0 + np.arange(la0, la1) * grid.step
        lon = grid.lon0 + np.arange(lo0, lo1) * grid.step
        LAT, LON = np.meshgrid(lat, lon, indexing="ij")

        rng = self._rng(year, k)
        hour = times.hour.values.astype(float)
        doy = times.dayofyear.values.astype(float)
        season = -np.cos(2 * np.pi * (doy - 15) / 365.25)
        diurnal = np.sin(2 * np.pi * (hour - 15) / 24)               # max ~21Z = mid-afternoon local
        synoptic = np.cumsum(rng.normal(0, 0.35, n_t)); synoptic -= synoptic.mean()
        t_reg = 277.0 + 13.0 * season + 5.0 * diurnal + synoptic
        rh_reg = np.clip(72 - 18 * diurnal - 2.5 * synoptic + rng.normal(0, 3, n_t), 12, 100)
        ws_reg = np.clip(3.0 + 1.5 * np.sin(2 * np.pi * (hour - 14) / 24) + np.cumsum(rng.normal(0, 0.25, n_t)) * 0.3, 0.3, 18)
        wd_reg = np.cumsum(rng.normal(0, 0.15, n_t)) + rng.uniform(0, 2 * np.pi)
        rain_reg = np.zeros(n_t)
        for _ in range(rng.poisson(2.2)):
            s = rng.integers(0, n_t); L = rng.integers(2, 10)
            rain_reg[s:s + L] += rng.gamma(1.2, 1.1, size=min(L, n_t - s))

        lat_c = LAT - LAT.mean()
        t_sp = -1.6 * lat_c + 0.4 * np.sin(LON * 7)
        rh_sp = 3.0 * lat_c + 1.5 * np.cos(LON * 9 + LAT * 4)
        rain_sp = 1.0 + 0.25 * np.sin(LON * 11) * np.cos(LAT * 13)
        cell_noise = self._rng(brick.key).normal(0, 0.6, (n_lat, n_lon))

        T = t_reg[:, None, None] + t_sp[None] + cell_noise[None]
        RH = np.clip(rh_reg[:, None, None] + rh_sp[None] - 0.8 * cell_noise[None], 5, 100)
        WS = np.clip(ws_reg[:, None, None] * (1 + 0.15 * np.sin(LON * 5)[None]), 0.2, 25)
        WD = wd_reg[:, None, None] + 0.1 * cell_noise[None]
        P = np.maximum(rain_reg[:, None, None] * rain_sp[None], 0.0)
        PRES = 98500.0 - 30.0 * (LAT - 46.0)[None] + 150.0 * synoptic[:, None, None]
        t_c = T - 273.15
        es = 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))
        e = RH / 100.0 * es
        w = 0.622 * e / np.maximum(PRES / 100.0 - e, 1.0)
        Q = w / (1 + w)
        clat, clon = float(LAT.mean()), float(LON.mean())
        elev = np.array([float(solar_elevation(clat, clon, t)) for t in times])
        SW = (np.maximum(np.sin(np.radians(elev)), 0) * 950.0)[:, None, None] * (1 - 0.6 * (P > 0))

        arrays = {
            "TMP_2maboveground": T.astype(np.float32),
            "SPFH_2maboveground": Q.astype(np.float32),
            "PRES_surface": PRES.astype(np.float32),
            "UGRD_10maboveground": (WS * np.cos(WD)).astype(np.float32),
            "VGRD_10maboveground": (WS * np.sin(WD)).astype(np.float32),
            "APCP_surface": P.astype(np.float32),
            "DSWRF_surface": np.broadcast_to(SW, (n_t, n_lat, n_lon)).astype(np.float32),
        }
        if self.inject_gaps:
            g = self._rng("gaps", year, k, brick.key)
            names = list(arrays)[:6]
            # (a) isolated masked packets, ~0.03 % per variable
            for v in names:
                m = g.random(arrays[v].shape) < 3e-4
                arrays[v][m] = np.nan
            # (b) one masked hour slice of one variable (1 chunk in 6)
            if g.random() < 1 / 6:
                arrays[names[g.integers(0, 6)]][g.integers(1, n_t - 1)] = np.nan
            # (c) a 5-hour outage over a small cluster of cells (1 chunk in 8)
            if g.random() < 1 / 8 and n_t > 8:
                y, x = g.integers(0, n_lat), g.integers(0, n_lon)
                t = g.integers(1, n_t - 6)
                for v in ("TMP_2maboveground", "SPFH_2maboveground"):
                    arrays[v][t:t + 5, max(0, y - 2):y + 3, max(0, x - 2):x + 3] = np.nan
        meta = {"lat_idx_range": [la0, la1], "lon_idx_range": [lo0, lo1], "synthetic": True}
        return arrays, times, meta, False


# ---------------------------------------------------------------------------
# Real AORC on S3
# ---------------------------------------------------------------------------

class AORCSource(BaseSource):
    """NOAA AORC v1.1 on S3, read brick-by-brick through the existing
    ``AORCDataLoader`` (same five-method fallback), with a disk cache."""

    name = "aorc-s3"
    description = ("NOAA AORC v1.1 Zarr stores (s3://noaa-nws-aorc-v1-1-1km/{year}.zarr), "
                   "fetched one 144 h x 128 x 256 chunk at a time, clipped to the study area, cached on disk.")

    def __init__(self, cache_dir="data/cache/bricks", max_gb=8.0, loader=None):
        super().__init__()
        from aorc_tools.aorc_access import AORCDataLoader
        self.loader = loader or AORCDataLoader()
        self.cache = BrickCache(cache_dir, max_gb)
        self._origin: dict = {}

    def origin_of_year(self, year):
        if year not in self._origin:
            ds = self.loader._open_dataset(year)
            self._origin[year] = pd.Timestamp(ds.time.values[0]).to_pydatetime()
        return self._origin[year]

    def last_time(self, year):
        ds = self.loader._open_dataset(year)
        return pd.Timestamp(ds.time.values[-1]).to_pydatetime()

    def status(self):
        s = super().status()
        s["cache"] = self.cache.usage()
        return s

    def _raw_brick_chunk(self, grid: Grid, year: int, k: int, brick: Brick):
        cached = self.cache.get(year, k, brick)
        if cached is not None:
            arrays, times, meta = cached
            return arrays, times, meta, True
        ds = self.loader._open_dataset(year)
        sub = grid.points[(grid.points.ci == brick.ci) & (grid.points.cj == brick.cj)]
        la0, la1 = int(sub.lat_idx.min()), int(sub.lat_idx.max()) + 1
        lo0, lo1 = int(sub.lon_idx.min()), int(sub.lon_idx.max()) + 1
        t_idx0 = k * CHUNK_TIME
        t_idx1 = min(t_idx0 + CHUNK_TIME, ds.sizes["time"])
        lat_live = float(ds.latitude.values[la0]); lon_live = float(ds.longitude.values[lo0])
        lat_exp = grid.lat0 + la0 * grid.step; lon_exp = grid.lon0 + lo0 * grid.step
        if abs(lat_live - lat_exp) > 1e-4 or abs(lon_live - lon_exp) > 1e-4:
            raise RuntimeError(
                f"AORC coordinate mismatch: store lat/lon at index ({la0},{lo0}) = ({lat_live:.5f},{lon_live:.5f}) "
                f"but points_index implies ({lat_exp:.5f},{lon_exp:.5f}). Regenerate points_index.csv.")
        t0 = _time.perf_counter()
        sel = ds.isel(time=slice(t_idx0, t_idx1), latitude=slice(la0, la1), longitude=slice(lo0, lo1))
        arrays = {v: np.asarray(sel[v].values, dtype=np.float32) for v in VARIABLES if v in sel}
        times = pd.DatetimeIndex(sel.time.values)
        meta = {"lat_idx_range": [la0, la1], "lon_idx_range": [lo0, lo1],
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "seconds": round(_time.perf_counter() - t0, 2), "store": f"s3://{self.loader.BUCKET}/{year}.zarr"}
        logger.info("fetched %d/k%03d brick %s (%d x %d x %d) in %.1fs",
                    year, k, brick.key, len(times), la1 - la0, lo1 - lo0, meta["seconds"])
        self.cache.put(year, k, brick, arrays, times, meta)
        return arrays, times, meta, False


def make_source(kind: str, **kw) -> BaseSource:
    if kind == "synthetic":
        return SyntheticSource(**{k: v for k, v in kw.items() if k in ("seed", "inject_gaps")})
    if kind in ("aorc", "aorc-s3", "s3"):
        return AORCSource(**{k: v for k, v in kw.items() if k in ("cache_dir", "max_gb", "loader")})
    raise ValueError(f"unknown source kind: {kind}")
