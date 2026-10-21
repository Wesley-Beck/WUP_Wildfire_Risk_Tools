"""
Process-level safety for long unattended runs:

* ``WorkspaceLock``   one explorer process per workspace. Two processes writing the same chunk files can lose
                      bricks silently, so the second one is refused and told who holds the workspace. The lock is
                      an OS file lock, released by the OS when the holder exits or crashes (no stale locks).
* ``start_logging``   a rotating log file (logs/explorer.log) and a crash trace file (logs/faults.log, Python
                      faulthandler) in the workspace, so a run that dies overnight leaves a record.
* ``ram_gb`` / ``disk_free_gb`` / ``sync_folder``   machine checks used by the job runner and the self-test.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import os
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
_FAULT_FILES = []          # keep faulthandler's file objects alive


class WorkspaceBusy(RuntimeError):
    def __init__(self, owner: dict):
        self.owner = owner or {}
        o = self.owner
        what = o.get("mode", "explorer")
        where = f" at {o['url']}" if o.get("url") else ""
        super().__init__(f"This workspace is in use by another WUP Explorer ({what}{where}, process {o.get('pid', '?')} on "
                         f"{o.get('host', '?')}, started {o.get('started', '?')}). Close it first, or use a different "
                         f"--workspace.")


class WorkspaceLock:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".explorer.lock"
        self.owner_path = self.root / ".explorer.owner.json"
        self._fh = None

    def owner(self) -> dict:
        try:
            return json.loads(self.owner_path.read_text())
        except Exception:
            return {}

    def acquire(self, mode="explorer", url=None) -> "WorkspaceLock":
        fh = open(self.lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise WorkspaceBusy(self.owner())
        self._fh = fh
        info = {"pid": os.getpid(), "host": socket.gethostname(), "mode": mode, "url": url,
                "started": datetime.now().isoformat(timespec="seconds"), "argv": sys.argv[1:]}
        tmp = self.owner_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info, indent=1))
        os.replace(tmp, self.owner_path)
        return self

    def update(self, **kw):
        info = {**self.owner(), **kw}
        tmp = self.owner_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info, indent=1))
        os.replace(tmp, self.owner_path)

    def release(self):
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
        try:
            self.owner_path.unlink()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.release()


def start_logging(ws_root, level=logging.INFO):
    """Console + rotating file log in <workspace>/logs, and native-crash traces in logs/faults.log."""
    logs = Path(ws_root) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(logs / "explorer.log", maxBytes=5 * 1024 * 1024, backupCount=5,
                                                  encoding="utf-8", delay=True)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(sh)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    for noisy in ("botocore", "s3fs", "fsspec", "aiobotocore", "urllib3", "rasterio", "numcodecs"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        f = open(logs / "faults.log", "a", encoding="utf-8")
        f.write(f"\n--- process {os.getpid()} started {datetime.now().isoformat(timespec='seconds')} ---\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
        _FAULT_FILES.append(f)
    except Exception as e:                                 # pragma: no cover
        logger.warning("faulthandler not enabled: %s", e)

    def _excepthook(exc_type, exc, tb):
        logging.getLogger("uncaught").critical("uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _excepthook
    try:
        import threading

        def _thook(args):
            logging.getLogger("uncaught").critical("uncaught exception in thread %s", args.thread.name if args.thread else "?",
                                                   exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.excepthook = _thook
    except Exception:                                      # pragma: no cover
        pass
    return logs


def ram_gb() -> float | None:
    """Total physical memory in GB (None when unknown)."""
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 1024 ** 3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except Exception:
        return None


def disk_free_gb(path) -> float:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1024 ** 3


SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "box sync", "my drive")


def sync_folder(path) -> str | None:
    """Name of a cloud-sync service whose folder contains ``path`` (they lock and rewrite files mid-run)."""
    low = str(Path(path).resolve()).lower()
    for m in SYNC_MARKERS:
        if m in low:
            return m
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        v = os.environ.get(env)
        if v and low.startswith(str(Path(v).resolve()).lower()):
            return "onedrive"
    return None


def retry(fn, waits=(5, 30, 120), what="operation", sleep=None, on_retry=None, permanent=()):
    """Call ``fn()``; on an exception wait and try again (len(waits) retries). ``on_retry(attempt, exc)`` runs before
    each retry (e.g. to re-sign a URL). Exceptions in ``permanent`` are raised at once."""
    import time as _t
    sleep = sleep or _t.sleep
    last = None
    for attempt, wait in enumerate((0,) + tuple(waits)):
        if wait:
            logger.warning("%s failed (%s: %s); retry %d of %d in %ss", what, type(last).__name__, last, attempt,
                           len(waits), wait)
            sleep(wait)
            if on_retry:
                on_retry(attempt, last)
        try:
            return fn()
        except permanent:
            raise
        except Exception as e:                              # noqa: BLE001
            last = e
    raise last


class _KeepAwake:
    """While a job or data task runs, ask Windows not to go to sleep (the screen may still turn off).
    Reference-counted across threads. SetThreadExecutionState is per thread, so one helper thread owns the
    request and switches it on/off. A no-op on other systems."""

    def __init__(self):
        import threading
        self._n = 0
        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._thread = None

    def _set(self, on: bool):
        if os.name != "nt":
            return
        try:
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        except Exception:                                   # noqa: BLE001
            pass

    def _worker(self):
        state = False
        while True:
            self._changed.wait()
            self._changed.clear()
            with self._lock:
                want = self._n > 0
            if want != state:
                self._set(want)
                state = want

    def _poke(self):
        import threading
        if os.name != "nt":
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="keep-awake", daemon=True)
            self._thread.start()
        self._changed.set()

    def __enter__(self):
        with self._lock:
            self._n += 1
        self._poke()
        return self

    def __exit__(self, *a):
        with self._lock:
            self._n = max(0, self._n - 1)
        self._poke()


keep_awake = _KeepAwake()


def fsync_file(path):
    """Force a just-written file to disk before it is renamed into place (a power cut then leaves either the old
    file or the complete new one, never a renamed file with missing data)."""
    try:
        fd = os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def replace_file(tmp, dst, tries=40, wait=0.25):
    """os.replace that waits while Windows refuses because another handle (virus scanner, indexer, a reader in this
    process) has the destination open; raises after ~10 s."""
    import time as _t
    for _ in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            _t.sleep(wait)
    os.replace(tmp, dst)
