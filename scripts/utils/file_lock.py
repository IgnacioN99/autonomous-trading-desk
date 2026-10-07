#!/usr/bin/env python3
"""
file_lock.py - Best-effort cross-process lock on a sidecar `<path>.lock` file (issue #91.5).

Used around read-modify-write of small observability files (logs/yolo_scan_health.json,
logs/market_data_rate_limit.json). POSIX: fcntl.flock; Windows: msvcrt.locking. The wait is bounded
(LOCK_WAIT_S): after it the caller proceeds WITHOUT the lock and one line goes to stderr, so a stuck
lock can lose a counter increment but never blocks a scan. Failing to open the lock file (e.g. an
unwritable logs/ dir) also proceeds without the lock; the caller's own write then reports the error.
Stdlib only.
"""

import contextlib
import os
import sys
import time

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None

LOCK_WAIT_S = 2.0
_POLL_S = 0.02


def _try_lock(fh) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return True  # no locking primitive: proceed unlocked


def _unlock(fh) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextlib.contextmanager
def locked(path: str, wait_s: float = None):
    """Holds an exclusive lock on `path + ".lock"` for the block. Yields True when the lock is held, False when
    the block runs unlocked (timeout or lock file unavailable). Never raises because of the lock itself."""
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s
    fh = None
    held = False
    try:
        lock_path = os.path.abspath(path) + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "a+")
    except Exception:
        fh = None
    if fh is not None:
        deadline = time.monotonic() + wait_s
        while True:
            if _try_lock(fh):
                held = True
                break
            if time.monotonic() >= deadline:
                try:
                    sys.stderr.write(f"file_lock: {os.path.basename(path)} still locked after {wait_s:.1f}s; "
                                     "proceeding without the lock\n")
                except Exception:
                    pass
                break
            time.sleep(_POLL_S)
    try:
        yield held
    finally:
        if fh is not None:
            if held:
                _unlock(fh)
            try:
                fh.close()
            except Exception:
                pass
