"""
Background calculation jobs: "compute these bricks over this period and keep
the results", resumable across restarts.

A job is (source, configuration, set of bricks, period). Work is done brick by
brick in time order, one 144-hour chunk at a time, so the moisture codes carry
forward continuously:

* if the previous chunk of the same brick is already stored, the chain starts
  from its end state ("continuous");
* otherwise it cold-starts ``spinup_hours`` earlier and discards the spin-up
  ("spinup:720h"), and every later chunk in the job is continuous.

Finished chunks are written atomically and recorded in the catalog, so a job
that is paused, cancelled, or interrupted (window closed, power cut) simply
skips what exists when it runs again. Several bricks run in parallel.
"""

from __future__ import annotations

import json
import logging
import threading
import time as _time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from aorc_tools.explorer.chain import ChainConfig, run_chain
from aorc_tools.explorer.grid import Brick, Grid
from aorc_tools.explorer.store import ResultsStore
from aorc_tools.explorer.workspace import Workspace, now_iso

logger = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, ws: Workspace, store: ResultsStore, grid: Grid, ctx):
        """``ctx`` supplies the live source/ndvi/fuel_map (they can be switched at runtime):
        ctx.source_for(name) -> BaseSource, ctx.ndvi_for(cfg, source) -> provider, ctx.fuel_map."""
        self.ws, self.store, self.grid, self.ctx = ws, store, grid, ctx
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._flags: dict[int, str] = {}           # job_id -> "pause" | "cancel"
        self._thread = None
        self.current: dict = {}
        self._rate = []                            # recent seconds per packet

    # ------------------------------------------------------------------ CRUD
    def chunks_for(self, source, start, end, domain_end=None):
        src = self.ctx.source_for(source)
        chs = self.grid.time_chunks(start, end, src.origin_of_year)
        if domain_end is not None:
            de = pd.Timestamp(domain_end).to_pydatetime()
            chs = [dict(c, end=min(c["end"], de)) for c in chs if c["start"] <= de]
        return chs

    def create(self, name, source, cfg: ChainConfig, bricks, start, end, spinup_hours=None):
        cfg.validate()
        de = pd.Timestamp(self.ws.settings["domain_end"]).to_pydatetime()
        dsd = pd.Timestamp(self.ws.settings["domain_start"]).to_pydatetime()
        start, end = max(pd.Timestamp(start).to_pydatetime(), dsd), min(pd.Timestamp(end).to_pydatetime(), de)
        if end < start:
            raise ValueError("period is outside the data domain")
        chunks = self.chunks_for(source, start, end, de)
        bricks = sorted(set(bricks))
        cells = {b["key"]: b["n_cells"] for b in self.grid.bricks()}
        unknown = [b for b in bricks if b not in cells]
        if unknown:
            raise ValueError(f"unknown bricks {unknown}")
        hours = sum(int((c["end"] - c["start"]).total_seconds() // 3600) + 1 for c in chunks)
        packets = sum(cells[b] for b in bricks) * hours
        sp = int(self.ws.settings.get("job_spinup_hours", 720) if spinup_hours is None else spinup_hours)
        self.store.register_config(source, cfg)
        with self.ws.conn() as c:
            cur = c.execute("""INSERT INTO jobs (name, created, updated, source, config_id, config, bricks, start, "end",
                               spinup_hours, status, tasks_total, tasks_done, packets_total, packets_done, error, seconds, note)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (name or f"{', '.join(bricks)} {start:%Y-%m-%d}→{end:%Y-%m-%d}", now_iso(), now_iso(), source, cfg.id,
                             json.dumps(cfg.as_dict()), json.dumps(bricks), chunks[0]["start"].isoformat(),
                             chunks[-1]["end"].isoformat(), sp, "queued", len(chunks) * len(bricks), 0, packets, 0, None, 0.0,
                             f"period rounded out to whole 144-h chunks"))
            jid = cur.lastrowid
        self._wake.set()
        return jid

    def list(self):
        with self.ws.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()]
        for r in rows:
            r["bricks"] = json.loads(r["bricks"]); r["config"] = json.loads(r["config"])
            r["percent"] = round(100 * (r["packets_done"] or 0) / r["packets_total"], 2) if r["packets_total"] else 0
            if r["status"] == "running" and self._rate:
                rate = float(np.median(self._rate[-20:]))
                r["eta_s"] = round(rate * max(0, r["packets_total"] - r["packets_done"]) / max(1, self._workers()), 1)
        return rows

    def get(self, jid):
        return next((j for j in self.list() if j["id"] == int(jid)), None)

    def _set(self, jid, **kw):
        kw["updated"] = now_iso()
        cols = ", ".join(f'"{k}"=?' for k in kw)
        with self.ws.conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*kw.values(), int(jid)))

    def pause(self, jid):
        self._flags[int(jid)] = "pause"
        j = self.get(jid)
        if j and j["status"] == "queued":
            self._set(jid, status="paused")

    def resume(self, jid):
        self._flags.pop(int(jid), None)
        self._set(jid, status="queued", error=None)
        self._wake.set()

    def cancel(self, jid):
        self._flags[int(jid)] = "cancel"
        j = self.get(jid)
        if j and j["status"] in ("queued", "paused"):
            self._set(jid, status="cancelled")

    def delete(self, jid):
        self.cancel(jid)
        with self.ws.conn() as c:
            c.execute("DELETE FROM jobs WHERE id=? AND status NOT IN ('running')", (int(jid),))

    # ------------------------------------------------------------------ scheduler
    def start(self):
        with self.ws.conn() as c:   # anything that was running when the app last closed resumes
            c.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            if not self.ws.settings.get("auto_resume", True):
                c.execute("UPDATE jobs SET status='paused' WHERE status='queued'")
        self._thread = threading.Thread(target=self._loop, name="wup-jobs", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set(); self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _workers(self):
        return max(1, int(self.ws.settings.get("workers", 2)))

    def _loop(self):
        while not self._stop.is_set():
            with self.ws.conn() as c:
                r = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if r is None:
                self._wake.wait(timeout=2.0); self._wake.clear()
                continue
            self._run_job(int(r["id"]))

    def run_now(self, jid):
        """Synchronous run (tests / scripts)."""
        self._run_job(int(jid))
        return self.get(jid)

    def _run_job(self, jid):
        """Time-chunk-major: for each 144-h chunk (in time order) compute every brick of the job in
        parallel, then write ONE results file for that chunk (atomic). Moisture state carries from
        each brick's previous chunk."""
        job = self.get(jid)
        if job is None or job["status"] != "queued":
            return
        self._flags.pop(jid, None)
        self._set(jid, status="running")
        cfg = ChainConfig.from_dict(job["config"])
        source = job["source"]
        t0 = _time.perf_counter()
        start, end = pd.Timestamp(job["start"]).to_pydatetime(), pd.Timestamp(job["end"]).to_pydatetime()
        chunks = self.chunks_for(source, start, end, self.ws.settings["domain_end"])
        bricks = job["bricks"]
        pids = {b: np.array(self.grid.cells_in_brick(*map(int, b.split("_")))) for b in bricks}
        hours = lambda ch: int((ch["end"] - ch["start"]).total_seconds() // 3600) + 1  # noqa: E731

        def done_counts():
            n_t = n_p = 0
            for ch in chunks:
                for b in bricks:
                    if self.store.has_chunk(source, cfg.id, ch["year"], ch["k"], b, need_end=ch["end"]):
                        n_t += 1
                        n_p += len(pids[b]) * hours(ch)
            return n_t, n_p

        n_t, n_p = done_counts()
        self._set(jid, tasks_done=n_t, packets_done=n_p)
        errors = []
        with ThreadPoolExecutor(max_workers=min(self._workers(), len(bricks)), thread_name_prefix="wup-brick") as ex:
            for ch in chunks:
                if self._flags.get(jid) or self._stop.is_set():
                    break
                todo = [b for b in bricks if not self.store.has_chunk(source, cfg.id, ch["year"], ch["k"], b, need_end=ch["end"])]
                if not todo:
                    continue
                tc = _time.perf_counter()
                futures = {b: ex.submit(self.compute_brick_chunk, source, cfg, b, pids[b], ch, job["spinup_hours"]) for b in todo}
                parts = []
                for b, fu in futures.items():
                    try:
                        parts.append(fu.result())
                    except Exception as e:
                        logger.error("job %s brick %s chunk %s/k%s failed: %s", jid, b, ch["year"], ch["k"], e)
                        traceback.print_exc()
                        errors.append(f"{b} {ch['year']}/k{ch['k']}: {e}")
                if parts:
                    try:
                        self.store.write_chunk(source, cfg, ch["year"], ch["k"], ch["start"], parts, jid)
                    except Exception as e:
                        traceback.print_exc()
                        errors.append(f"write {ch['year']}/k{ch['k']}: {e}")
                if errors:
                    break
                dt = _time.perf_counter() - tc
                n_new = sum(len(p["point_ids"]) * len(p["times"]) for p in parts)
                self._rate.append(dt / max(1, n_new))
                self._rate = self._rate[-30:]
                n_t += len(parts)
                n_p += n_new
                self.current = {"job": jid, "chunk": f"{ch['year']}/k{ch['k']:03d}", "t": ch["start"].isoformat(),
                                "bricks": [p["brick"] for p in parts]}
                self._set(jid, tasks_done=n_t, packets_done=n_p, seconds=round(_time.perf_counter() - t0, 1))
        self.current = {}
        flag = self._flags.pop(jid, None)
        if errors:
            self._set(jid, status="error", error="; ".join(errors[:3]))
        elif flag == "pause":
            self._set(jid, status="paused")
        elif flag == "cancel":
            self._set(jid, status="cancelled")
        elif self._stop.is_set():
            self._set(jid, status="queued")
        else:
            n_t, n_p = done_counts()
            ok = n_t >= job["tasks_total"]
            self._set(jid, status="done" if ok else "error", tasks_done=n_t, packets_done=n_p,
                      seconds=round(_time.perf_counter() - t0, 1), error=None if ok else "some chunks missing after run")

    # ------------------------------------------------------------------ one brick, one chunk
    def prev_chunk_key(self, source, ch):
        src = self.ctx.source_for(source)
        t_prev = ch["start"] - timedelta(hours=1)
        pc = self.grid.time_chunks(t_prev, t_prev, src.origin_of_year)[0]
        return pc["year"], pc["k"]

    def compute_brick_chunk(self, source, cfg, bkey, pids, ch, spinup_hours):
        src = self.ctx.source_for(source)
        py, pk = self.prev_chunk_key(source, ch)
        prev_ok = ch["start"] > pd.Timestamp(self.ws.settings["domain_start"]).to_pydatetime() and \
            self.store.has_chunk(source, cfg.id, py, pk, bkey, need_end=ch["start"] - timedelta(hours=1))
        state = self.store.state_at(source, cfg.id, py, pk, bkey, "end", pids) if prev_ok else None
        dsd = pd.Timestamp(self.ws.settings["domain_start"]).to_pydatetime()
        if state is not None:
            spin, continuity, w_start = 0, "continuous", ch["start"]
        else:
            w_start = max(dsd, ch["start"] - timedelta(hours=int(spinup_hours)))
            spin = int((ch["start"] - w_start).total_seconds() // 3600)
            continuity = f"spinup:{spin}h" if spin else "cold"
        tc = _time.perf_counter()
        raw = src.fetch(self.grid, pids, w_start, ch["end"], gap_fill=cfg.gap_fill)
        res = run_chain(raw, grid=self.grid, cfg=cfg, state=state, spinup_hours=spin, mode="store",
                        ndvi=self.ctx.ndvi_for(cfg, source), fuel_map=self.ctx.fuel_map)
        m = res.analysis_mask
        return {"brick": bkey, "times": res.times[m], "point_ids": pids,
                "steps": {k: np.asarray(v)[m] for k, v in res.steps.items()},
                "start_state": res.start_state, "end_state": res.end_state, "continuity": continuity,
                "seconds": _time.perf_counter() - tc}
