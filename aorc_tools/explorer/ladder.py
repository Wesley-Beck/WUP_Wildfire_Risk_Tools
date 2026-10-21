"""
Resolution ladder: always use the most specific value available, and when it is
missing fall back one step at a time -- recording which step every packet used.

Weather inputs (per AORC variable, per packet = cell x hour):

    tier 0  native        the cell's own value at that hour (AORC 800 m grid, hourly)
    tier 1  time          same cell, linear interpolation across a gap of <= 3 h
                          (not used for precipitation: accumulations are not interpolated)
    tier 2  3x3           mean of valid neighbouring grid cells at the same hour (~2.4 km)
    tier 3  7x7           mean of valid cells in a 7x7 window, same hour (~6 km)
    tier 4  brick         mean of every valid study cell in the brick, same hour
    tier 9  unavailable   nothing usable -> the indices for that packet are NaN

Neighbour statistics only ever use ORIGINAL valid values (never values that
were themselves filled), so fills cannot propagate. A packet's weather tier is
the worst tier among the variables the chain uses.

NDVI (FPI only) and fuel model have their own ladders, see ``ndvi.py``.
"""

from __future__ import annotations

import numpy as np

TIER_NATIVE, TIER_TIME, TIER_3X3, TIER_7X7, TIER_BRICK, TIER_NONE = 0, 1, 2, 3, 4, 9
WX_TIERS = {
    0: {"label": "native cell-hour", "space": "cell (~0.8 km)", "time": "1 h"},
    1: {"label": "same cell, time-interpolated", "space": "cell", "time": "≤3 h gap, linear"},
    2: {"label": "3×3 neighbourhood mean", "space": "~2.4 km", "time": "1 h"},
    3: {"label": "7×7 neighbourhood mean", "space": "~6 km", "time": "1 h"},
    4: {"label": "brick-wide mean", "space": "brick (~100–200 km)", "time": "1 h"},
    9: {"label": "unavailable", "space": "—", "time": "—"},
}
NO_TIME_INTERP = {"APCP_surface"}
MAX_GAP_H = 3


def _neigh_mean(frame: np.ndarray, valid: np.ndarray, r: int) -> np.ndarray:
    """Mean of valid values in a (2r+1)^2 window around every cell of a 2-D frame."""
    v = np.where(valid, frame, 0.0)
    c = valid.astype(np.float64)
    pad = ((r, r), (r, r))
    vp, cp = np.pad(v, pad), np.pad(c, pad)
    # summed-area tables
    S = np.pad(vp.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    C = np.pad(cp.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    ny, nx = frame.shape
    k = 2 * r + 1
    tot = S[k:k + ny, k:k + nx] - S[:ny, k:k + nx] - S[k:k + ny, :nx] + S[:ny, :nx]
    cnt = C[k:k + ny, k:k + nx] - C[:ny, k:k + nx] - C[k:k + ny, :nx] + C[:ny, :nx]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, tot / np.maximum(cnt, 1), np.nan)


def fill_variable(arr: np.ndarray, study: np.ndarray, var: str):
    """Fill missing study packets in one variable's (n_t, ny, nx) brick array.

    Returns (filled array, tier array uint8 (n_t, ny, nx))."""
    out = arr.astype(np.float32, copy=True)
    tier = np.zeros(arr.shape, dtype=np.uint8)
    orig_valid = np.isfinite(arr)
    miss = (~orig_valid) & study[None]
    if not miss.any():
        return out, tier

    # tier 1: time interpolation at the same cell
    if var not in NO_TIME_INTERP:
        ti, yi, xi = np.where(miss)
        for y, x in set(zip(yi.tolist(), xi.tolist())):
            series = arr[:, y, x]
            ok = np.isfinite(series)
            if ok.sum() < 2:
                continue
            idx = np.arange(len(series))
            bad = np.where(~ok)[0]
            # gap length for each missing hour
            starts = np.r_[0, np.where(np.diff(bad) != 1)[0] + 1]
            ends = np.r_[starts[1:], len(bad)]
            for s, e in zip(starts, ends):
                run = bad[s:e]
                lo, hi = run[0] - 1, run[-1] + 1
                if len(run) <= MAX_GAP_H and lo >= 0 and hi < len(series) and ok[lo] and ok[hi]:
                    out[run, y, x] = np.interp(run, [lo, hi], [series[lo], series[hi]])
                    tier[run, y, x] = TIER_TIME
        miss = miss & ~np.isfinite(out)

    # tiers 2-4: spatial, per affected hour, from ORIGINAL valid values only
    for t in np.unique(np.where(miss)[0]):
        m = miss[t]
        frame, valid = arr[t], orig_valid[t]
        for r, code in ((1, TIER_3X3), (3, TIER_7X7)):
            if not m.any():
                break
            nm = _neigh_mean(frame, valid, r)
            take = m & np.isfinite(nm)
            out[t][take] = nm[take]
            tier[t][take] = code
            m = m & ~take
        if m.any():
            pool = frame[valid & study]
            if pool.size:
                out[t][m] = float(pool.mean())
                tier[t][m] = TIER_BRICK
                m[:] = False
        if m.any():
            tier[t][m] = TIER_NONE
    return out, tier


def fill_brick(arrays: dict, study: np.ndarray, variables=None):
    """Apply the ladder to every variable of one brick/time-chunk.

    Returns (filled arrays, worst tier per packet (n_t, ny, nx) uint8,
    per-variable tier counts over study packets)."""
    variables = variables or list(arrays)
    filled, worst, counts = {}, None, {}
    for v in variables:
        if v not in arrays:
            continue
        f, t = fill_variable(arrays[v], study, v)
        filled[v] = f
        worst = t if worst is None else np.maximum(worst, t)
        tt = t[:, study]
        counts[v] = {int(k): int((tt == k).sum()) for k in (0, 1, 2, 3, 4, 9) if (tt == k).any()}
    for v in arrays:
        filled.setdefault(v, arrays[v])
    if worst is None:
        worst = np.zeros(next(iter(arrays.values())).shape, dtype=np.uint8)
    return filled, worst, counts
