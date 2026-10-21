"""
Read-only access to the batch extractor's canonical outputs
(``data/output/{year}/{month}/data_{year}_{month}.db``, written by
``aorc_tools.storage.SQLiteStorage``).

Two uses in the explorer:

* whole-study-area *fields* at one hour (28 k values = one 112 kB blob) for
  the "entire WUP" map mode -- far cheaper than recomputing anything;
* cell *series* straight from the stored intermediates, so a cell's trace can
  be shown from the canonical record where it exists (and the on-demand
  recomputation can be checked against it).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

TS_FMT = "%Y-%m-%d %H:%M"


class ExtractedStore:
    def __init__(self, output_base="data/output"):
        self.base = Path(output_base)
        self._months: dict[tuple[int, int], Path] = {}
        self._vars: dict[tuple[int, int], list[str]] = {}
        self._pid_order: dict[tuple[int, int], np.ndarray] = {}
        self.rescan()

    # ------------------------------------------------------------------
    def rescan(self):
        self._months.clear()
        pat = re.compile(r"data_(\d{4})_(\d{2})\.db$")
        if self.base.exists():
            for p in self.base.glob("*/*/data_*.db"):
                m = pat.search(p.name)
                if m:
                    self._months[(int(m.group(1)), int(m.group(2)))] = p
        return self

    def months(self) -> list[str]:
        return [f"{y}-{m:02d}" for (y, m) in sorted(self._months)]

    def has(self, year: int, month: int) -> bool:
        return (year, month) in self._months

    def variables(self, year: int, month: int) -> list[str]:
        key = (year, month)
        if key not in self._vars:
            with sqlite3.connect(self._months[key]) as c:
                self._vars[key] = [r[0] for r in c.execute("SELECT DISTINCT variable FROM data ORDER BY variable")]
        return self._vars[key]

    def point_order(self, year: int, month: int) -> np.ndarray:
        key = (year, month)
        if key not in self._pid_order:
            with sqlite3.connect(self._months[key]) as c:
                self._pid_order[key] = np.array([r[0] for r in c.execute("SELECT point_id FROM points ORDER BY point_id")])
        return self._pid_order[key]

    def metadata(self, year: int, month: int) -> dict:
        p = self._months[(year, month)].parent / f"metadata_{year}_{month:02d}.json"
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}

    # ------------------------------------------------------------------
    def field(self, var: str, when: datetime):
        """All stored values of ``var`` at one hour -> (point_ids, float32 values)."""
        key = (when.year, when.month)
        if key not in self._months:
            return None
        with sqlite3.connect(self._months[key]) as c:
            row = c.execute("SELECT values_blob FROM data WHERE variable=? AND timestamp=?",
                            (var, when.strftime(TS_FMT))).fetchone()
        if row is None:
            return None
        return self.point_order(*key), np.frombuffer(row[0], dtype=np.float32).copy()

    def timestamps(self, var: str, year: int, month: int) -> list[str]:
        with sqlite3.connect(self._months[(year, month)]) as c:
            return [r[0] for r in c.execute("SELECT timestamp FROM data WHERE variable=? ORDER BY timestamp", (var,))]

    def series(self, var: str, point_ids, start: datetime, end: datetime):
        """Stored ``var`` for the given cells over [start, end] -> (DatetimeIndex, (n_hours, n_cells))."""
        pids = np.asarray(point_ids, dtype=int)
        months = sorted({(t.year, t.month) for t in pd.date_range(start, end, freq="MS").union([start, end])})
        frames, times = [], []
        for key in months:
            if key not in self._months:
                continue
            order = self.point_order(*key)
            cols = np.searchsorted(order, pids)
            ok = (cols < len(order)) & (order[np.minimum(cols, len(order) - 1)] == pids)
            with sqlite3.connect(self._months[key]) as c:
                rows = c.execute(
                    "SELECT timestamp, values_blob FROM data WHERE variable=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
                    (var, start.strftime(TS_FMT), end.strftime(TS_FMT))).fetchall()
            for ts, blob in rows:
                a = np.frombuffer(blob, dtype=np.float32)
                vals = np.full(len(pids), np.nan, dtype=np.float32)
                vals[ok] = a[cols[ok]]
                frames.append(vals)
                times.append(ts)
        if not frames:
            return pd.DatetimeIndex([]), np.empty((0, len(pids)), dtype=np.float32)
        return pd.to_datetime(times, format=TS_FMT), np.vstack(frames)

    def coverage(self, start: datetime, end: datetime) -> dict:
        need = sorted({(t.year, t.month) for t in pd.date_range(start, end, freq="MS").union([start, end])})
        have = [k for k in need if k in self._months]
        return {"needed": [f"{y}-{m:02d}" for y, m in need], "have": [f"{y}-{m:02d}" for y, m in have],
                "complete": len(have) == len(need)}

    def payload(self) -> dict:
        return {"base": str(self.base), "months": self.months(), "n_months": len(self._months)}
