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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from aorc_tools.explorer.chain import ChainConfig, run_chain
from aorc_tools.explorer.grid import Brick, Grid
from aorc_tools.explorer.ndvi import MODIS_START
from aorc_tools.explorer.runtime import disk_free_gb, keep_awake, ram_gb
from aorc_tools.explorer.store import ResultsStore
from aorc_tools.explorer.workspace import Workspace, now_iso

logger = logging.getLogger(__name__)

JOB_RETRY_WAITS = (60, 300, 900, 1800, 3600)   # s before automatic retry 1, 2, 3, ... (the last value repeats)
STATE_CHECK_KEYS = ("mc_ffmc", "mc_dmc", "mc_dc", "d_ffmc", "d_dmc", "d_dc", "fm1", "fm10", "fm100", "fm1000")
STATE_TOL = 1e-4


class JobStop(Exception):
    """A condition that should stop the job without automatic retries (disk full, offline data missing...)."""


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
        self._ndvi_sigs: dict = {}

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
        dsd, de = self.ctx.source_for(source).domain(self.ws.settings)
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
        if j and j["status"] in ("queued", "retrying"):
            self._set(jid, status="paused")

    def resume(self, jid):
        self._flags.pop(int(jid), None)
        self._set(jid, status="queued", error=None, attempts=0, retry_at=None)
        self._wake.set()

    def cancel(self, jid):
        self._flags[int(jid)] = "cancel"
        j = self.get(jid)
        if j and j["status"] in ("queued", "paused", "retrying", "error"):
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
        n = max(1, int(_num(self.ws.settings.get("workers", 2), 2)))
        ram = ram_gb()
        if ram:                                          # up to ~3 GB per worker (largest brick, 30-day spin-up)
            n = min(n, max(1, int(ram // 6)))
        return n

    def _promote_retries(self):
        now = datetime.now().isoformat(timespec="seconds")
        with self.ws.conn() as c:
            c.execute("UPDATE jobs SET status='queued' WHERE status='retrying' AND (retry_at IS NULL OR retry_at<=?)", (now,))

    def run_until_settled(self, jid, sleep=_time.sleep, on_wait=None):
        """Synchronous run that also waits out automatic retries (headless runs)."""
        while True:
            self._run_job(int(jid))
            j = self.get(jid)
            if j is None or j["status"] != "retrying":
                return j
            wait = max(0.0, (pd.Timestamp(j["retry_at"]) - pd.Timestamp(datetime.now())).total_seconds())
            if on_wait:
                on_wait(j, wait)
            sleep(wait)
            if self._stop.is_set():
                return self.get(jid)
            self._set(jid, status="queued")

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._promote_retries()
                with self.ws.conn() as c:
                    r = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if r is None:
                    self._wake.wait(timeout=2.0); self._wake.clear()
                    continue
                self._run_job(int(r["id"]))
            except Exception:                            # keep the scheduler alive whatever happens
                logger.exception("job scheduler error; continuing")
                self._stop.wait(5.0)

    def run_now(self, jid):
        """Synchronous run (tests / scripts)."""
        self._run_job(int(jid))
        return self.get(jid)

    def _run_job(self, jid):
        with keep_awake:                                 # Windows: no sleep while a job runs
            try:
                return self._run_job_inner(jid)
            except Exception as e:                       # anything outside the per-brick handling: never leave a
                logger.exception("job %s stopped by an unexpected error", jid)   # job "running" forever
                self.current = {}
                job = self.get(jid)
                if job is not None:
                    self._settle_failure(jid, job, [f"{type(e).__name__}: {e}"], _is_permanent(e),
                                         self._flags.pop(jid, None))

    def _settle_failure(self, jid, job, errors, permanent, flag):
        """After a failed run: schedule an automatic retry (transient problems) or stop with the error."""
        attempts = int(job.get("attempts") or 0) + 1
        max_auto = int(_num(self.ws.settings.get("job_auto_retries", 8), 8))
        if flag == "cancel":
            self._set(jid, status="cancelled", error="; ".join(errors[:3]))
        elif not permanent and attempts <= max_auto and flag != "pause":
            wait = JOB_RETRY_WAITS[min(attempts, len(JOB_RETRY_WAITS)) - 1]
            at = (datetime.now() + timedelta(seconds=wait)).isoformat(timespec="seconds")
            logger.warning("job %s failed (%s); automatic retry %d of %d at %s", jid, errors[0], attempts, max_auto, at)
            self._set(jid, status="retrying", attempts=attempts, retry_at=at,
                      error=f"retry {attempts}/{max_auto} at {at[11:16]} after: " + "; ".join(errors[:3]))
        else:
            self._set(jid, status="paused" if flag == "pause" else "error", attempts=attempts,
                      error="; ".join(errors[:3]))

    def _run_job_inner(self, jid):
        """Time-chunk-major: for each 144-h chunk (in time order) compute every brick of the job in
        parallel, then write ONE results file for that chunk (atomic). Moisture state carries from
        each brick's previous chunk."""
        job = self.get(jid)
        if job is None or job["status"] != "queued":
            return
        self._flags.pop(jid, None)
        self._ndvi_sigs = {}                             # NDVI fingerprints, read once per job run
        self._set(jid, status="running")
        cfg = ChainConfig.from_dict(job["config"])
        source = job["source"]
        src0 = self.ctx.source_for(source)
        if hasattr(src0, "refresh_data_end") and not getattr(src0, "offline", False):
            src0.refresh_data_end(self.ws.settings)      # never plan hours NOAA has not published
        t0 = _time.perf_counter()
        start, end = pd.Timestamp(job["start"]).to_pydatetime(), pd.Timestamp(job["end"]).to_pydatetime()
        chunks = self.chunks_for(source, start, end, self.ctx.source_for(source).domain(self.ws.settings)[1])
        bricks = job["bricks"]
        pids = {b: np.array(self.grid.cells_in_brick(*map(int, b.split("_")))) for b in bricks}
        hours = lambda ch: int((ch["end"] - ch["start"]).total_seconds() // 3600) + 1  # noqa: E731
        totals = {"tasks_total": len(chunks) * len(bricks),                 # the domain end may have moved
                  "packets_total": sum(len(pids[b]) for b in bricks) * sum(hours(c) for c in chunks)}
        if totals["tasks_total"] != job["tasks_total"] or totals["packets_total"] != job["packets_total"]:
            self._set(jid, **totals)
            job.update(totals)

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
        errors, permanent = [], False
        dsd = self.ctx.source_for(source).domain(self.ws.settings)[0]
        min_free = float(self.ws.settings.get("min_free_gb", 5.0))
        with ThreadPoolExecutor(max_workers=min(self._workers(), len(bricks)), thread_name_prefix="wup-brick") as ex:
            for ch in chunks:
                if self._flags.get(jid) or self._stop.is_set():
                    break
                free = disk_free_gb(self.ws.results_dir)
                if free < min_free:
                    errors.append(f"disk nearly full: {free:.1f} GB free on the workspace drive (needs {min_free:g} GB). "
                                  "Free some space, then resume the job.")
                    permanent = True
                    break
                todo = [b for b in bricks if self.chunk_state(source, cfg, ch, b, dsd) != "ok"]
                if not todo:
                    continue
                tc = _time.perf_counter()
                futures = {b: ex.submit(self.compute_brick_chunk, source, cfg, b, pids[b], ch, job["spinup_hours"]) for b in todo}
                parts = []
                for b, fu in futures.items():
                    try:
                        parts.append(fu.result())
                    except Exception as e:
                        logger.error("job %s brick %s chunk %s/k%s failed: %s", jid, b, ch["year"], ch["k"], e, exc_info=True)
                        errors.append(f"{b} {ch['year']}/k{ch['k']}: {type(e).__name__}: {e}")
                        permanent = permanent or _is_permanent(e)
                if parts:
                    try:
                        self.store.write_chunk(source, cfg, ch["year"], ch["k"], ch["start"], parts, jid)
                    except Exception as e:
                        logger.error("job %s: writing %s/k%s failed: %s", jid, ch["year"], ch["k"], e, exc_info=True)
                        errors.append(f"write {ch['year']}/k{ch['k']}: {type(e).__name__}: {e}")
                        permanent = permanent or _is_permanent(e)
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
                self._set(jid, tasks_done=min(n_t, job["tasks_total"]), packets_done=min(n_p, job["packets_total"]),
                          seconds=round(_time.perf_counter() - t0, 1))
        self.current = {}
        flag = self._flags.pop(jid, None)
        if errors and flag != "cancel":
            self._settle_failure(jid, job, errors, permanent, flag)
        elif flag == "pause":
            self._set(jid, status="paused")
        elif flag == "cancel":
            self._set(jid, status="cancelled")
        elif self._stop.is_set():
            self._set(jid, status="queued")
        else:
            n_t, n_p = done_counts()
            ok = n_t >= job["tasks_total"]
            self._set(jid, status="done" if ok else "error", tasks_done=n_t, packets_done=n_p, attempts=0, retry_at=None,
                      seconds=round(_time.perf_counter() - t0, 1), error=None if ok else "some chunks missing after run")

    # ------------------------------------------------------------------ is a saved chunk usable as it is?
    def chunk_state(self, source, cfg, ch, brick, domain_start) -> str:
        """'missing' (compute), 'stale' (recompute: its start state does not continue from the chunk before it, e.g.
        it was computed with a cold spin-up before the earlier record existed) or 'ok'."""
        r = self.store.chunk_row(source, cfg.id, ch["year"], ch["k"], brick)
        if r is None or not r.get("path") or not Path(r["path"]).exists():
            return "missing"
        if pd.Timestamp(r["t_end"]) < pd.Timestamp(ch["end"]):
            return "missing"
        if r.get("ndvi_sig") is not None and hasattr(self.ctx, "ndvi_signature") and \
                (pd.Timestamp(ch["end"]) >= MODIS_START or cfg.allow_ndvi_climatology):
            key = (cfg.id, source, ch["year"])
            if key not in self._ndvi_sigs:
                self._ndvi_sigs[key] = self.ctx.ndvi_signature(cfg, source, ch["year"])
            if self._ndvi_sigs[key] != r["ndvi_sig"]:
                logger.info("chunk %s/k%03d %s: NDVI inputs changed since it was computed; recomputing",
                            ch["year"], ch["k"], brick)
                return "stale"
        if pd.Timestamp(ch["start"]) <= pd.Timestamp(domain_start):
            return "ok"
        py, pk = self.prev_chunk_key(source, ch)
        p = self.store.chunk_row(source, cfg.id, py, pk, brick)
        if p is None or not p.get("path") or not Path(p["path"]).exists():
            return "ok"                                  # nothing to continue from (yet)
        if r.get("continuity") == "continuous":
            if r.get("prev_rowid") is not None:
                if r["prev_rowid"] == p["_rowid"]:
                    return "ok"                          # computed from (or checked against) the current predecessor
            elif r["_rowid"] > p["_rowid"]:
                return "ok"                              # older catalog rows: written after the predecessor
        try:
            a = self.store.state_at(source, cfg.id, py, pk, brick, "end")
            b = self.store.state_at(source, cfg.id, ch["year"], ch["k"], brick, "start")
        except Exception as e:                           # unreadable file: recompute this chunk
            logger.warning("chunk %s/k%s %s: state unreadable (%s); recomputing", ch["year"], ch["k"], brick, e)
            return "stale"
        if a is None or b is None:
            return "stale"
        for key in STATE_CHECK_KEYS:
            x, y = a.get(key), b.get(key)
            if x is None or y is None:
                continue
            both = np.isfinite(x) & np.isfinite(y)
            if both.any() and np.any(np.abs(x[both] - y[both]) > STATE_TOL * np.maximum(1.0, np.abs(x[both]))):
                logger.info("chunk %s/k%03d %s does not continue from the chunk before it (%s); recomputing",
                            ch["year"], ch["k"], brick, key)
                return "stale"
        self.store.mark_continuous(r, p["_rowid"])
        return "ok"

    # ------------------------------------------------------------------ one brick, one chunk
    def prev_chunk_key(self, source, ch):
        src = self.ctx.source_for(source)
        t_prev = ch["start"] - timedelta(hours=1)
        pc = self.grid.time_chunks(t_prev, t_prev, src.origin_of_year)[0]
        return pc["year"], pc["k"]

    def compute_brick_chunk(self, source, cfg, bkey, pids, ch, spinup_hours):
        src = self.ctx.source_for(source)
        py, pk = self.prev_chunk_key(source, ch)
        dsd = src.domain(self.ws.settings)[0]
        prev_ok = ch["start"] > dsd and \
            self.store.has_chunk(source, cfg.id, py, pk, bkey, need_end=ch["start"] - timedelta(hours=1))
        prow = self.store.chunk_row(source, cfg.id, py, pk, bkey) if prev_ok else None
        state = self.store.state_at(source, cfg.id, py, pk, bkey, "end", pids) if prev_ok else None
        if state is not None:
            spin, continuity, w_start = 0, "continuous", ch["start"]
        else:
            w_start = max(dsd, ch["start"] - timedelta(hours=int(spinup_hours)))
            spin = max(0, int((ch["start"] - w_start).total_seconds() // 3600))   # 0 when the domain starts inside this chunk
            continuity = f"spinup:{spin}h" if spin else "cold"
        tc = _time.perf_counter()
        raw = src.fetch(self.grid, pids, w_start, ch["end"], gap_fill=cfg.gap_fill)
        res = run_chain(raw, grid=self.grid, cfg=cfg, state=state, spinup_hours=spin, mode="store",
                        ndvi=self.ctx.ndvi_for(cfg, source), fuel_map=self.ctx.fuel_map)
        m = res.analysis_mask
        wx = np.asarray(res.steps["wx_tier"])[m]
        if wx.size and (wx >= 9).all():
            from aorc_tools.explorer.sources import NoDataYet
            raise NoDataYet(f"no weather at all for brick {bkey}, {ch['start']:%Y-%m-%d %H}:00 onward: nothing saved")
        if self.ws.settings.get("diagnostics", True):
            try:                                      # driver diagnostics for a fixed cell sample: never fails a job
                self.save_diagnostics(source, cfg, bkey, pids, ch, raw, state, spin)
            except Exception as e:                    # noqa: BLE001
                logger.warning("driver diagnostics for %s %s/k%s not saved: %s", bkey, ch["year"], ch["k"], e)
        return {"brick": bkey, "times": res.times[m], "point_ids": pids,
                "steps": {k: np.asarray(v)[m] for k, v in res.steps.items()},
                "start_state": res.start_state, "end_state": res.end_state, "continuity": continuity,
                "prev_rowid": prow["_rowid"] if (prow and state is not None) else None,
                "ndvi_sig": self.ctx.ndvi_signature(cfg, source, ch["year"]) if hasattr(self.ctx, "ndvi_signature") else None,
                "seconds": _time.perf_counter() - tc}


    def save_diagnostics(self, source, cfg, bkey, pids, ch, raw, state, spin):
        """Hourly drivers (weather, BUI, ISI, 1000-h moisture, greenness) and indices for the brick's fixed diagnostics
        sample, from the same weather and start state as the saved chunk: the Patterns tab's driving-variable analysis
        then needs no second pass over the weather. float16, compressed (~1 GB for the whole record)."""
        from aorc_tools.explorer.drivers import DIAG_KEYS, diagnostics_path, diagnostics_sample
        from aorc_tools.explorer.sources import RawWindow
        want = diagnostics_sample(self.grid).get(bkey, [])
        pos = {int(p_): i for i, p_ in enumerate(pids)}
        idx = np.array([pos[p_] for p_ in want if p_ in pos], dtype=int)
        if not len(idx):
            return
        sub = RawWindow(times=raw.times, point_ids=np.asarray(pids)[idx], data={k: v[:, idx] for k, v in raw.data.items()},
                        source=raw.source, tiers=None if raw.tiers is None else raw.tiers[:, idx])
        st = None if state is None else {k: np.asarray(v)[idx] for k, v in state.items()}
        res = run_chain(sub, grid=self.grid, cfg=cfg, state=st, spinup_hours=spin, mode="trace",
                        ndvi=self.ctx.ndvi_for(cfg, source), fuel_map=self.ctx.fuel_map)
        from aorc_tools.explorer.drivers import derived_weather
        m = res.analysis_mask
        steps = {k: np.asarray(v)[m] for k, v in res.steps.items() if k in DIAG_KEYS or k in sub.data}
        steps.update(derived_weather(steps))
        arrays = {k: np.clip(steps[k], -6e4, 6e4).astype(np.float16) for k in DIAG_KEYS if k in steps}
        path = diagnostics_path(self.ws.root, source, cfg.id, ch["year"], ch["k"], bkey)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.stem + ".tmp.npz")
        hours = ((pd.DatetimeIndex(res.times[m]) - pd.Timestamp("1970-01-01")) // pd.Timedelta("1h")).values.astype(np.int64)
        np.savez_compressed(tmp, hours=hours, point_ids=np.asarray(res.point_ids, dtype=np.int64), **arrays)
        from aorc_tools.explorer.runtime import replace_file
        replace_file(tmp, path)


def _is_permanent(e) -> bool:
    """Errors that a retry will not fix."""
    from aorc_tools.explorer.sources import AORCLayoutError, NoDataYet, OfflineMissing
    if isinstance(e, (OfflineMissing, AORCLayoutError, NoDataYet, JobStop, MemoryError)):
        return True
    return isinstance(e, OSError) and getattr(e, "errno", None) == 28          # ENOSPC: no space left on device


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
