"""
A stand-in for the NOAA AORC v1.1 S3 Zarr store, for offline tests.

Builds a local Zarr year store with AORC's real layout (time x latitude x longitude,
chunks 144 x 128 x 256, global CONUS index space) but materialises only the chunks a
test touches, filled from the explorer's SyntheticSource so values look like weather.
``FakeLoader`` has the one method AORCSource uses (``_open_dataset(year)``).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.grid import CHUNK_TIME, Brick
from aorc_tools.explorer.sources import VARIABLES, SyntheticSource

NLAT, NLON = 3400, 5100


def build_store(root: Path, grid, year: int, chunks, bricks, seed=7) -> Path:
    import zarr
    store = Path(root) / f"{year}.zarr"
    t0 = pd.Timestamp(1979, 2, 1) if year == 1979 else pd.Timestamp(year, 1, 1)
    times = pd.date_range(t0, pd.Timestamp(year, 12, 31, 23), freq="h")
    g = zarr.open_group(store, mode="a")
    if "time" not in g:
        ta = g.create_array("time", data=((times - pd.Timestamp("1970-01-01")) // pd.Timedelta("1h")).values.astype("int64"),
                            dimension_names=("time",))
        ta.attrs.update({"units": "hours since 1970-01-01 00:00:00", "calendar": "proleptic_gregorian"})
        g.create_array("latitude", data=(grid.lat0 + np.arange(NLAT) * grid.step), dimension_names=("latitude",))
        g.create_array("longitude", data=(grid.lon0 + np.arange(NLON) * grid.step), dimension_names=("longitude",))
        for v in VARIABLES:
            g.create_array(v, shape=(len(times), NLAT, NLON), chunks=(CHUNK_TIME, 128, 256), dtype="float32",
                           fill_value=np.nan, dimension_names=("time", "latitude", "longitude"))
    syn = SyntheticSource(seed=seed, inject_gaps=False)
    for k in chunks:
        for bk in bricks:
            ci, cj = map(int, bk.split("_"))
            arrays, ctimes, meta, _ = syn._raw_brick_chunk(grid, year, k, Brick(ci, cj))
            (la0, la1), (lo0, lo1) = meta["lat_idx_range"], meta["lon_idx_range"]
            t_a = k * CHUNK_TIME
            for v in VARIABLES:
                g[v][t_a:t_a + len(ctimes), la0:la1, lo0:lo1] = arrays[v]
    zarr.consolidate_metadata(store)
    return store


class FakeLoader:
    BUCKET = "fake-aorc"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.opened = 0

    def _open_dataset(self, year):
        import xarray as xr
        self.opened += 1
        return xr.open_zarr(self.root / f"{year}.zarr", consolidated=True)
