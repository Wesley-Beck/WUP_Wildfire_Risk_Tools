"""
Background data tasks (separate from calculation jobs): downloads, NDVI ingest,
verification, research bundles. One task runs at a time; each reports progress
(done / total), can be cancelled, and is resumed after a restart if it was
running (downloads skip what is already on disk, so resuming is cheap).
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from aorc_tools.explorer.grid import Brick
from aorc_tools.explorer.workspace import now_iso

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, name TEXT, params TEXT, status TEXT,
    created TEXT, updated TEXT, total INTEGER, done INTEGER, note TEXT, error TEXT, result TEXT);
"""


class Cancelled(Exception):
    pass


class TaskRunner:
    def __init__(self, ws, grid, ctx):
        self.ws, self.grid, self.ctx = ws, grid, ctx
        with ws.conn() as c:
            c.executescript(SCHEMA)
            c.execute("UPDATE tasks SET status='queued' WHERE status='running'")
        self.kinds = {"aorc-download": self._aorc_download, "ndvi-ingest": self._ndvi_ingest}
        self.secrets: dict = {}                    # task id -> credentials, memory only (never written to disk)
        self._cancel: set = set()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def register(self, kind, fn):
        """fn(task_id, params, progress(done, total, note)) -> result dict (``errors`` list -> done-with-errors)"""
        self.kinds[kind] = fn

    # ------------------------------------------------------------------ CRUD
    def create(self, kind, name, params: dict) -> int:
        if kind not in self.kinds:
            raise ValueError(f"unknown task kind {kind}")
        with self.ws.conn() as c:
            cur = c.execute("INSERT INTO tasks (kind, name, params, status, created, updated, total, done, note) "
                            "VALUES (?,?,?,?,?,?,0,0,'')",
                            (kind, name, json.dumps(params, default=str), "queued", now_iso(), now_iso()))
            tid = cur.lastrowid
        self._wake.set()
        return tid

    def _row(self, r):
        d = dict(r)
        d["params"] = json.loads(d["params"] or "{}")
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        d["percent"] = round(100.0 * d["done"] / d["total"], 2) if d["total"] else (100.0 if d["status"] == "done" else 0.0)
        return d

    def list(self, limit=50):
        with self.ws.conn() as c:
            return [self._row(r) for r in c.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def get(self, tid):
        with self.ws.conn() as c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return self._row(r) if r else None

    def _set(self, tid, **kw):
        kw["updated"] = now_iso()
        with self.ws.conn() as c:
            c.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), tid))

    def cancel(self, tid):
        t = self.get(tid)
        if t and t["status"] == "running":
            self._cancel.add(tid)
        elif t and t["status"] == "queued":
            self._set(tid, status="cancelled")

    def resume(self, tid):
        t = self.get(tid)
        if t and t["status"] in ("cancelled", "error", "done-with-errors"):
            self._set(tid, status="queued", error=None)
            self._wake.set()

    def delete(self, tid):
        t = self.get(tid)
        if t and t["status"] == "running":
            raise ValueError("cancel the task first")
        with self.ws.conn() as c:
            c.execute("DELETE FROM tasks WHERE id=?", (tid,))

    # ------------------------------------------------------------------ running
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="data-tasks")
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            with self.ws.conn() as c:
                r = c.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if r is None:
                self._wake.wait(5)
                self._wake.clear()
                continue
            self.run_now(r["id"])

    def run_now(self, tid):
        t = self.get(tid)
        if t is None:
            return None
        self._set(tid, status="running", error=None)

        def progress(done, total, note=""):
            if tid in self._cancel:
                raise Cancelled()
            self._set(tid, done=int(done), total=int(total), note=str(note)[:500])

        from aorc_tools.explorer.runtime import keep_awake
        try:
            with keep_awake:                             # Windows: no sleep while a data task runs
                result = self.kinds[t["kind"]](t["id"], t["params"], progress)
            errs = (result or {}).get("errors") or []
            self._set(tid, status="done-with-errors" if errs else "done", result=json.dumps(result, default=str))
        except Cancelled:
            self._cancel.discard(tid)
            self._set(tid, status="cancelled")
        except Exception as e:
            logger.exception("task %s failed", tid)
            self._set(tid, status="error", error=f"{type(e).__name__}: {e}"[:2000],
                      note=traceback.format_exc()[-500:])
        return self.get(tid)

    # ------------------------------------------------------------------ AORC download
    def _aorc_download(self, tid, params, progress):
        """Fetch every (year, chunk, brick) of a period into the local input store (skips what is there)."""
        src = self.ctx.download_source()
        start, end = pd.Timestamp(params["start"]).to_pydatetime(), pd.Timestamp(params["end"]).to_pydatetime()
        bricks = params.get("bricks") or [b["key"] for b in self.grid.bricks()]
        chunks = self.grid.time_chunks(start, end, src.origin_of_year)
        items = [(c["year"], c["k"], b) for c in chunks for b in bricks]
        todo = [it for it in items if not src.inputs.has(*it)]
        total, done, errors = len(items), len(items) - len(todo), []
        progress(done, total, f"{len(todo)} brick-chunks to download")
        workers = int(self.ws.settings.get("download_workers", 3))

        def one(it):
            y, k, b = it
            ci, cj = map(int, b.split("_"))
            src._raw_brick_chunk(self.grid, y, k, Brick(ci, cj))
            return it

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(one, it): it for it in todo}
            for f in as_completed(futs):
                it = futs[f]
                try:
                    f.result()
                except Cancelled:
                    raise
                except Exception as e:                       # keep going; report at the end
                    errors.append(f"{it[0]} k{it[1]:03d} {it[2]}: {type(e).__name__}: {e}"[:300])
                done += 1
                try:
                    progress(done, total, f"{it[0]} chunk {it[1]} brick {it[2]}")
                except Cancelled:
                    for g in futs:
                        g.cancel()
                    raise
        inv = src.inputs.inventory(self.ws.settings.get("domain_start"), self.ws.settings.get("domain_end"))
        return {"downloaded": len(todo) - len(errors), "already_present": total - len(todo), "errors": errors[:50],
                "n_errors": len(errors), "store_bytes": inv["bytes"], "store_percent": inv["percent"]}

    # ------------------------------------------------------------------ NDVI ingest
    def _ndvi_ingest(self, tid, params, progress):
        from aorc_tools.explorer import ndvi_ingest as NI
        years = [int(y) for y in params["years"]]
        backend, product = params.get("backend", "planetary"), params.get("product", "MOD13Q1")
        kw = {"force": bool(params.get("force"))}
        if backend == "local":
            kw["folder"] = params["folder"]
        if backend == "appeears":
            cred = self.secrets.pop(tid, {})
            kw.update(username=cred.get("username"), password=cred.get("password"),
                      download_dir=self.ws.root / "inputs" / "downloads" / "appeears")
        models, _ = self.ctx.fuel_map.models(self.grid.points.point_id.values, params.get("fuel_model", "G"))
        res = NI.run_ingest(self.grid, self.ctx.static_dir, backend, product, years, models,
                            progress=lambda d, t, note: progress(int(d * 100), t * 100, note), **kw)
        return res
