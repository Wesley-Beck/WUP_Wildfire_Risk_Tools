"""
FPI vegetation inputs and per-cell fuel models, each with its own ladder.

NDVI (per cell, per day; held constant across the 24 hours of the day):

    tier 0  MOD13Q1   250 m, 16-day composite, interpolated to daily, aggregated to the cell
    tier 1  MOD13A1   500 m, 16-day       (used where tier 0 is missing / QA-screened out)
    tier 2  MOD13A2   1 km, 16-day
    tier 3  NOAA CDR AVHRR/VIIRS NDVI, 0.05 deg (~5 km), 16-day max composite  -- OPT-IN ONLY
    tier 4  per-cell day-of-year NDVI climatology (2000–2026)                  -- OPT-IN ONLY
            (tiers 3-4 are outside the locked "FPI = MODIS era 2000–2026" decision, CLAUDE.md Option 1)
    tier 9  none      -> FPI is NaN (always the case before 2000-02-18 unless opt-in tiers are enabled)

File layout (USAGE.md):
    data/static/FPI_Input_Static.nc                       ND_min, ND_max, LLfm, DLfm, MXd (lat, lon)
    data/static/ndvi/<PRODUCT>/FPI_Input_<year>.nc        ND0 (time, lat, lon), daily  (PRODUCT: MOD13Q1, MOD13A1,
                                                           MOD13A2, AVHRR_CDR -- written by explorer.ndvi_ingest)
    data/static/ndvi/FPI_Input_<year>.nc                  legacy single-product layout -> tier 0
    data/static/ndvi/ndvi_doy_climatology.nc              ND_clim (doy, lat, lon), tier 4 (built by ndvi_ingest)

Fuel model (per cell):
    tier 0  data/static/nfdrs_fuel_model.csv  (point_id, fuel_model)  e.g. derived from LANDFIRE
    tier 1  the configured default model (G) everywhere
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.config import NFDRS_FUEL_MODELS

MODIS_START = pd.Timestamp("2000-02-18")
NDVI_PRODUCTS = [("MOD13Q1", 250), ("MOD13A1", 500), ("MOD13A2", 1000)]
NDVI_TIERS = {0: "MOD13Q1 250 m", 1: "MOD13A1 500 m", 2: "MOD13A2 1 km", 3: "AVHRR CDR ~5 km (opt-in)",
              4: "day-of-year climatology (opt-in)", 9: "none"}


def fuel_loadings(models: np.ndarray) -> dict:
    """Burgan live/dead loadings and moisture of extinction per cell from its NFDRS model."""
    llfm = np.array([NFDRS_FUEL_MODELS[m]["LWOOD"] + NFDRS_FUEL_MODELS[m]["LHERB"] for m in models], dtype=float)
    dlfm = np.array([sum(NFDRS_FUEL_MODELS[m][k] for k in ("L1", "L10", "L100", "L1000")) for m in models], dtype=float)
    mxd = np.array([float(NFDRS_FUEL_MODELS[m]["MXD"]) for m in models])
    return {"llfm": llfm, "dlfm": dlfm, "mxd": mxd}


class FuelMap:
    def __init__(self, static_dir="data/static", default="G"):
        self.default = default
        self.path = Path(static_dir) / "nfdrs_fuel_model.csv"
        self.map = {}
        if self.path.exists():
            df = pd.read_csv(self.path)
            self.map = {int(p): str(m).upper() for p, m in zip(df.point_id, df.fuel_model) if str(m).upper() in NFDRS_FUEL_MODELS}

    @property
    def available(self):
        return bool(self.map)

    def models(self, point_ids, default=None):
        d = default or self.default
        m = np.array([self.map.get(int(p), d) for p in point_ids], dtype=object)
        tier = np.array([0 if int(p) in self.map else 1 for p in point_ids], dtype=np.uint8)
        return m, tier

    def describe(self):
        return {"available": self.available, "file": str(self.path), "n_cells": len(self.map),
                "tiers": {0: "per-cell map (nfdrs_fuel_model.csv)", 1: f"default model everywhere"}}


class NDVIProvider:
    """Real NDVI from NetCDF files (see module docstring)."""

    name = "files"

    def __init__(self, static_dir="data/static", allow_climatology=False):
        self.dir = Path(static_dir)
        self.allow_climatology = allow_climatology
        self.static_path = self.dir / "FPI_Input_Static.nc"

    @property
    def available(self):
        return self.static_path.exists()

    def describe(self):
        prods = {p: sorted(x.stem.split("_")[-1] for x in (self.dir / "ndvi" / p).glob("FPI_Input_*.nc"))
                 for p in [q for q, _ in NDVI_PRODUCTS] + ["AVHRR_CDR"] if (self.dir / "ndvi" / p).exists()}
        legacy = sorted(x.stem.split("_")[-1] for x in (self.dir / "ndvi").glob("FPI_Input_*.nc")) if (self.dir / "ndvi").exists() else []
        return {"kind": "files", "available": self.available, "static": str(self.static_path), "products": prods,
                "legacy_years": legacy, "climatology": (self.dir / "ndvi" / "ndvi_doy_climatology.nc").exists(),
                "allow_climatology": self.allow_climatology, "tiers": NDVI_TIERS}

    def _pick(self, da, lats, lons):
        import xarray as xr
        return da.sel(latitude=xr.DataArray(lats, dims="cell"), longitude=xr.DataArray(lons, dims="cell"), method="nearest")

    def get(self, grid, point_ids, times: pd.DatetimeIndex):
        if not self.available:
            return None
        import xarray as xr
        rows = grid.rows_for(point_ids)
        lats, lons = grid.points.latitude.values[rows], grid.points.longitude.values[rows]
        st = xr.open_dataset(self.static_path)
        out = {k: self._pick(st[v], lats, lons).values.astype(float)
               for k, v in [("nd_min", "ND_min"), ("nd_max", "ND_max"), ("llfm", "LLfm"), ("dlfm", "DLfm"), ("mxd", "MXd")]}
        days = times.normalize()
        udays = days.unique()
        nd_day = np.full((len(udays), len(rows)), np.nan)
        tier_day = np.full((len(udays), len(rows)), 9, dtype=np.uint8)
        candidates = [(i, self.dir / "ndvi" / p) for i, (p, _) in enumerate(NDVI_PRODUCTS)] + [(0, self.dir / "ndvi")]
        if self.allow_climatology:                   # opt-in: AVHRR CDR (also before MODIS)
            candidates.append((3, self.dir / "ndvi" / "AVHRR_CDR"))
        for year in sorted(set(udays.year)):
            ysel = udays.year == year
            for tier, folder in candidates:
                f = folder / f"FPI_Input_{year}.nc"
                if not f.exists():
                    continue
                ds = xr.open_dataset(f)
                vals = self._pick(ds["ND0"].sel(time=udays[ysel], method="nearest"), lats, lons).values
                blk = nd_day[ysel]
                era_ok = (udays[ysel] >= MODIS_START) if tier < 3 else np.ones(int(ysel.sum()), dtype=bool)
                take = np.isnan(blk) & np.isfinite(vals) & era_ok[:, None]
                blk[take] = vals[take]
                nd_day[ysel] = blk
                tb = tier_day[ysel]; tb[take] = tier; tier_day[ysel] = tb
        clim = self.dir / "ndvi" / "ndvi_doy_climatology.nc"
        if self.allow_climatology and clim.exists() and np.isnan(nd_day).any():
            ds = xr.open_dataset(clim)
            vals = self._pick(ds["ND_clim"].sel(doy=xr.DataArray(udays.dayofyear.values, dims="t")), lats, lons).values
            take = np.isnan(nd_day) & np.isfinite(vals)
            nd_day[take] = vals[take]
            tier_day[take] = 4
        pos = udays.get_indexer(days)
        out["nd0"] = nd_day[pos]
        out["tier"] = tier_day[pos]
        return out


class SyntheticNDVI:
    """Demo NDVI with the same ladder: a seasonal curve, cloudy composites that
    drop to 500 m / 1 km, nothing before MODIS unless climatology is allowed."""

    name = "synthetic"

    def __init__(self, allow_climatology=False):
        self.allow_climatology = allow_climatology

    @property
    def available(self):
        return True

    def describe(self):
        return {"kind": "synthetic", "available": True, "allow_climatology": self.allow_climatology, "tiers": NDVI_TIERS,
                "note": "Synthetic NDVI (demo only)."}

    def get(self, grid, point_ids, times):
        rows = grid.rows_for(point_ids)
        lat, lon = grid.points.latitude.values[rows], grid.points.longitude.values[rows]
        tex = 0.03 * np.sin(lon * 37) * np.cos(lat * 41)
        nd_min, nd_max = 0.18 + tex, 0.86 + tex
        doy = times.dayofyear.values.astype(float)[:, None]
        green = np.clip(np.sin(np.pi * (doy - 110) / 190), 0, 1)
        nd0 = nd_min[None] + (nd_max - nd_min)[None] * (0.12 + 0.88 * green)
        # 16-day composite id -> deterministic "cloudy" composites by brick-ish region
        comp = ((times.dayofyear.values.astype(np.int64) - 1) // 16 + times.year.values.astype(np.int64) * 23)[:, None]
        region = (np.floor(lat * 4) * 7 + np.floor(lon * 3)).astype(np.int64)[None]
        h = ((comp * 2654435761 + region * 40503) % 1000003) % 100
        tier = np.where(h < 6, 1, np.where(h < 8, 2, 0)).astype(np.uint8)
        pre = (times < MODIS_START)[:, None] & np.ones_like(tier, dtype=bool)
        if self.allow_climatology:
            tier = np.where(pre, 4, tier).astype(np.uint8)
        else:
            nd0 = np.where(pre, np.nan, nd0)
            tier = np.where(pre, 9, tier).astype(np.uint8)
        return {"nd0": nd0, "tier": tier, "nd_min": nd_min, "nd_max": nd_max}
