#!/usr/bin/env python3
"""
Build the percentile reference distribution (CLAUDE.md "post-pass") from the
batch record in data/output.

    python scripts/build_percentile_climatology.py --output data/output \
        --vars FWI ERC BI --out data/climatology/percentiles.json

Method (stated in the manifest so the paper can cite it):
    * pooled-regional: every cell contributes to one distribution
    * whole-season: all hours of every extracted month (no day-of-year window)
    * reference period: whatever months exist in data/output (listed)

For FPI (2000-2026 only) run it separately with --vars FPI --since 2000 once
FPI is in the batch record, so its reference pool is the MODIS-era overlap.

Memory: one month of one variable is 744 x 28 k float32 = 83 MB; we stream
month by month and keep a stride-sampled pool (default every 3rd hour), which
is plenty for 101 quantiles.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="data/output")
    ap.add_argument("--vars", nargs="+", default=["FWI", "ERC", "BI"])
    ap.add_argument("--out", default="data/climatology/percentiles.json")
    ap.add_argument("--since", type=int, default=None, help="first year to include")
    ap.add_argument("--until", type=int, default=None, help="last year to include")
    ap.add_argument("--hour-stride", type=int, default=3)
    args = ap.parse_args()

    dbs = sorted(Path(args.output).glob("*/*/data_*.db"))
    pools = {v: [] for v in args.vars}
    months = []
    for db in dbs:
        y, m = int(db.name[5:9]), int(db.name[10:12])
        if (args.since and y < args.since) or (args.until and y > args.until):
            continue
        months.append(f"{y}-{m:02d}")
        with sqlite3.connect(db) as c:
            for v in args.vars:
                rows = c.execute("SELECT values_blob FROM data WHERE variable=? ORDER BY timestamp", (v,)).fetchall()
                for blob in rows[:: args.hour_stride]:
                    a = np.frombuffer(blob[0], dtype=np.float32)
                    pools[v].append(a[np.isfinite(a)])
        print(f"  {y}-{m:02d}: " + ", ".join(f"{v}={sum(len(x) for x in pools[v]):,}" for v in args.vars))

    out = {"method": "pooled-regional, whole-season (empirical, all extracted hours, stride %d)" % args.hour_stride,
           "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "source_months": months, "vars": {}}
    for v in args.vars:
        if not pools[v]:
            print(f"  {v}: no data")
            continue
        pool = np.concatenate(pools[v])
        q = np.percentile(pool, np.arange(101)).tolist()
        out["vars"][v] = {"q": q, "n": int(len(pool)), "min": float(pool.min()), "max": float(pool.max())}
        print(f"  {v}: n={len(pool):,} p50={q[50]:.2f} p90={q[90]:.2f} p99={q[99]:.2f}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out))
    print("wrote", args.out)


if __name__ == "__main__":
    main()
