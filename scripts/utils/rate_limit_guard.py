#!/usr/bin/env python3
"""
rate_limit_guard.py - Process-wide Binance market-data rate-limit guard (issue #91.1).

Binance answers HTTP 429 when the IP request-weight limit is broken and HTTP 418 (IP auto-ban, 2 minutes to
3 days) when 429s are ignored. Every scan market-data fetch (microstructure_engine, broad_market_radar,
quant_risk_engine pairs, funding_arbitrage, market_regime, broad_yolo_scanner) calls raise_if_banned() before
its request and on_http_error() on an HTTPError, so after one 429/418 no further market-data request leaves the
process until the ban expires.

- Opt-in: the guard only acts after enable() (or inside scan_session()). Only scan entry points enable it
  (screening_pipeline, broad_market_radar, broad_yolo_scanner, quant_risk_engine, funding_arbitrage and
  market_regime CLIs). The executor, the position guardian, dynamic_exit_manager and the night cutoff loop never
  do, so risk-reducing paths are never blocked. While disabled, behaviour is exactly as before (HTTPError
  propagates unchanged).
- trip(status, retry_after) sets banned_until = now + Retry-After (header in seconds; default 60 s for 429,
  120 s for 418 when it is missing or unparsable). trip() never writes files.
- Persistence: persist() writes STATE_FILE (logs/market_data_rate_limit.json: banned_until epoch seconds,
  status, updated_ts) from main-thread code at the end of a run; enable() restores a future banned_until (an
  expired one is ignored), so the next run does not call Binance until the ban expires. An operator may delete
  the file to clear a stale ban.

Stdlib + utils only.
"""

import contextlib
import os
import sys
import threading
import time
from typing import Optional

from utils.atomic_writer import atomic_write_json, read_json_safe  # callers put scripts/ on sys.path
from utils import file_lock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_FILE = os.path.join(BASE_DIR, "logs", "market_data_rate_limit.json")  # module-level so tests can redirect it

RATE_LIMIT_HTTP_CODES = (429, 418)  # 429 = limit broken (back off), 418 = IP auto-banned after ignoring 429s
DEFAULT_RETRY_AFTER_S = {429: 60.0, 418: 120.0}
MAX_RETRY_AFTER_S = 3 * 24 * 3600.0  # Binance's longest 418 ban
UNAVAILABLE_PREFIX = "UNAVAILABLE: "


class RateLimitedError(RuntimeError):
    """Binance answered HTTP 429 (rate limit) or 418 (IP auto-ban), or a recorded ban is still active: no further
    market-data request is issued."""

    def __init__(self, message: str = "Binance rate limit", status: Optional[int] = None):
        super().__init__(message)
        self.status = status


_lock = threading.Lock()
_enabled = False
_banned_until = 0.0
_status = None
_dirty = False  # a trip() not yet persisted


def is_enabled() -> bool:
    return _enabled


def enable(now: Optional[float] = None) -> bool:
    """Turns the guard on for this process and restores a still-active persisted ban. Returns the previous state."""
    global _enabled
    with _lock:
        previous = _enabled
        _enabled = True
    load_persisted(now)
    return previous


def disable() -> None:
    """Turns the guard off (in-memory ban kept, so a later enable() in the same process still honours it)."""
    global _enabled
    with _lock:
        _enabled = False


def load_persisted(now: Optional[float] = None) -> None:
    """Restores banned_until from STATE_FILE when it lies in the future (expired or malformed records are
    ignored). Read-only."""
    global _banned_until, _status
    now = time.time() if now is None else float(now)
    try:
        data = read_json_safe(STATE_FILE, default=None)
        until = float(data.get("banned_until"))
        status = int(data.get("status")) if data.get("status") is not None else None
    except Exception:
        return
    if until != until or until <= now:  # NaN or expired
        return
    until = min(until, now + MAX_RETRY_AFTER_S)
    with _lock:
        if until > _banned_until:
            _banned_until, _status = until, status


def parse_retry_after(raw, status: int) -> float:
    """Seconds to wait from a Retry-After header value; the status default when missing or unparsable."""
    default = DEFAULT_RETRY_AFTER_S.get(int(status), DEFAULT_RETRY_AFTER_S[429])
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        secs = float(str(raw).strip())
    except (TypeError, ValueError, UnicodeDecodeError):
        return default
    if secs != secs or secs <= 0:
        return default
    return min(secs, MAX_RETRY_AFTER_S)


def retry_after_header(err) -> Optional[str]:
    """Retry-After value of an HTTPError (None when absent; tolerates hdrs=None)."""
    getter = getattr(getattr(err, "headers", None), "get", None)
    if not callable(getter):
        return None
    try:
        return getter("Retry-After")
    except Exception:
        return None


def trip(status: int, retry_after=None, now: Optional[float] = None) -> None:
    """Records a 429/418 (in memory only; no-op while the guard is disabled)."""
    global _banned_until, _status, _dirty
    if not _enabled:
        return
    now = time.time() if now is None else float(now)
    until = now + parse_retry_after(retry_after, status)
    with _lock:
        if until > _banned_until:
            _banned_until = until
        _status = int(status)
        _dirty = True


def banned_until() -> float:
    return _banned_until


def is_banned(now: Optional[float] = None) -> bool:
    now = time.time() if now is None else float(now)
    return _enabled and now < _banned_until


def unavailable_text() -> str:
    """Fixed desk text for the scan outputs while a ban is active (no server-controlled content)."""
    status = f" (HTTP {_status})" if _status else ""
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_banned_until)) if _banned_until > 0 else "unknown"
    return f"{UNAVAILABLE_PREFIX}Binance rate limit{status}, retry after {until}"


def raise_if_banned(now: Optional[float] = None) -> None:
    """Raises RateLimitedError while an enabled guard holds an active ban (before any request is issued)."""
    if is_banned(now):
        raise RateLimitedError(unavailable_text()[len(UNAVAILABLE_PREFIX):], status=_status)


def on_http_error(err) -> None:
    """Call from a fetch's `except HTTPError`: on 429/418 with the guard enabled, trips it (honouring Retry-After)
    and raises RateLimitedError. Otherwise returns and the caller re-raises the original error."""
    code = getattr(err, "code", None)
    if not _enabled or code not in RATE_LIMIT_HTTP_CODES:
        return
    trip(code, retry_after_header(err))
    raise RateLimitedError(f"Binance rate limit (HTTP {code})", status=code) from None


def persist() -> None:
    """Writes the ban recorded by trip() to STATE_FILE. Main thread only, at the end of a run; fail-open."""
    global _dirty
    if not _dirty:
        return
    try:
        with file_lock.locked(STATE_FILE):
            with _lock:
                record = {"banned_until": _banned_until, "status": _status, "updated_ts": time.time()}
            try:
                previous = float((read_json_safe(STATE_FILE, default=None) or {}).get("banned_until"))
            except Exception:
                previous = 0.0
            if not (previous == previous and previous > record["banned_until"]):  # never shorten a longer ban
                atomic_write_json(STATE_FILE, record)
        _dirty = False
    except Exception as e:
        try:
            sys.stderr.write(f"rate_limit_guard: could not persist the rate-limit ban ({type(e).__name__})\n")
        except Exception:
            pass


def error_payload(command: str, env: str, **extra) -> dict:
    """JSON error document of a scan CLI while a ban is active (fixed text, no request issued)."""
    payload = {"status": "error", "command": command, "env": env,
               "error": f"RateLimitedError: {unavailable_text()[len(UNAVAILABLE_PREFIX):]}",
               "market_data_status": unavailable_text()}
    payload.update(extra)
    return payload


@contextlib.contextmanager
def scan_session():
    """Scan entry point scope: enable() (restoring a persisted ban), then persist() and restore the previous
    enabled state on exit."""
    previous = enable()
    try:
        yield
    finally:
        persist()
        if not previous:
            disable()


def reset_for_tests() -> None:
    global _enabled, _banned_until, _status, _dirty
    with _lock:
        _enabled = False
        _banned_until = 0.0
        _status = None
        _dirty = False
