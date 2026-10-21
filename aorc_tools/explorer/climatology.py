"""
Percentile reference distributions (the CLAUDE.md "post-pass").

File: ``data/climatology/percentiles.json``::

    {
      "method": "pooled-regional, whole-season",
      "built_at": "...", "source_months": ["2020-07", ...],
      "vars": {"FWI": {"q": [101 values], "n": 123456}, "ERC": {...}, ...}
    }

``q[i]`` is the i-th percentile of all packets in the reference pool. A value
is mapped to its percentile by linear interpolation between the q's.
Build it with ``scripts/build_percentile_climatology.py`` once the batch
record exists; it is *reporting* scaffolding, not validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


class Climatology:
    def __init__(self, path="data/climatology/percentiles.json"):
        self.path = Path(path)
        self.available = self.path.exists()
        self.meta = {}
        self._q = {}
        if self.available:
            d = json.loads(self.path.read_text())
            self.meta = {k: v for k, v in d.items() if k != "vars"}
            self._q = {k: np.asarray(v["q"], dtype=float) for k, v in d.get("vars", {}).items()}
            self.available = bool(self._q)

    def has(self, var: str) -> bool:
        return var in self._q

    def percentile(self, var: str, values: np.ndarray) -> np.ndarray:
        q = self._q[var]
        v = np.asarray(values, dtype=float)
        out = np.full(v.shape, np.nan)
        ok = np.isfinite(v)
        out[ok] = np.interp(v[ok], q, np.arange(len(q)) * (100.0 / (len(q) - 1)))
        return out

    def describe(self) -> str:
        m = self.meta
        months = m.get("source_months", [])
        span = f"{months[0]}..{months[-1]} ({len(months)} months)" if months else "unknown reference"
        return f"{m.get('method', 'pooled')} reference, {span}; vars {sorted(self._q)}"

    def payload(self) -> dict:
        return {"available": self.available, "meta": self.meta, "vars": sorted(self._q)}
