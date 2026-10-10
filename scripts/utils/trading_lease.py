#!/usr/bin/env python3
"""
trading_lease.py - Single trading writer for PROD opening orders (issue #280).

logs/trading_lease.json = {session_id, runtime ("claude" | "agy"), acquired_at, heartbeat_at, env} (epoch seconds).
One agent session at a time may open PROD positions: the PreToolUse hook (scripts/hooks/pre_trade_guard.py) claims
the lease at an allowed opening and refreshes it on the holder's next ones; the PostToolUse hook
(scripts/hooks/post_trade_sync.py) refreshes it after the holder's brief / record commands and commits a takeover the
user approved (`python3 scripts/trading_lease.py --take`, force-asked by the PreToolUse hook). Those two hooks are the
file's only writers. A lease whose heartbeat is older than LEASE_STALE_SECONDS holds nothing: the next opener claims
it. Risk-reducing paths and TESTNET never read it.

Fail closed: an unreadable or malformed file is an error (the PROD callers deny) and every write holds the sidecar
lock (utils.file_lock) for at most LOCK_WAIT_S; a lock not acquired raises LeaseError and nothing is written.
Pure functions with an explicit base_dir; stdlib only.
"""

import contextlib
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

try:
    from utils.atomic_writer import atomic_write_json
    from utils.file_lock import locked
except ImportError:  # pragma: no cover - imported as scripts.utils.trading_lease
    from scripts.utils.atomic_writer import atomic_write_json
    from scripts.utils.file_lock import locked

LEASE_REL_PATH = ("logs", "trading_lease.json")
# Stale after this long without a heartbeat (dossier TTL 1200 s plus a user-confirmation wait); not a profile key
LEASE_STALE_SECONDS = 1800
# Shorter than file_lock's 2 s: the PreToolUse hook has a 10 s budget
LOCK_WAIT_S = 1.5
SESSION_ABBREV_CHARS = 8
TAKE_COMMAND = "python3 scripts/trading_lease.py --take"
STATUS_COMMAND = "python3 scripts/trading_lease.py --status"


class LeaseError(RuntimeError):
    """The lease file is unreadable or malformed, or its lock was not acquired (PROD openings deny)."""


def lease_path(base_dir: str) -> str:
    return os.path.join(base_dir, *LEASE_REL_PATH)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate(record: Any) -> Dict[str, Any]:
    """The record when it is a lease (session_id a non-empty string, acquired_at / heartbeat_at numbers); raises
    LeaseError otherwise. runtime and env are informational and never make a record malformed."""
    if not isinstance(record, dict):
        raise LeaseError("trading lease is not a JSON object")
    if not isinstance(record.get("session_id"), str) or not record["session_id"].strip():
        raise LeaseError("trading lease has no session_id")
    for key in ("acquired_at", "heartbeat_at"):
        if not _number(record.get(key)):
            raise LeaseError(f"trading lease has no numeric {key}")
    return record


def load(base_dir: str) -> Optional[Dict[str, Any]]:
    """The current lease, None when there is none; raises LeaseError when the file is unreadable or malformed."""
    path = lease_path(base_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError) as e:
        # strerror: an OSError's text without the file path
        raise LeaseError(f"trading lease unreadable ({type(e).__name__}: {getattr(e, 'strerror', None) or e})")
    return validate(record)


def is_stale(record: Dict[str, Any], now: float, ttl: int = LEASE_STALE_SECONDS) -> bool:
    return now - float(record["heartbeat_at"]) > ttl


def abbreviate(session: Any) -> str:
    text = str(session or "")
    return text[:SESSION_ABBREV_CHARS] + ("..." if len(text) > SESSION_ABBREV_CHARS else "")


def describe(record: Dict[str, Any], now: float) -> str:
    """'session 5e55105e... (claude), last heartbeat 42s ago' (never the lease file name)."""
    age = max(0, int(now - float(record["heartbeat_at"])))
    return (f"session {abbreviate(record.get('session_id'))} ({record.get('runtime') or 'unknown runtime'}), "
            f"last heartbeat {age}s ago")


def denial_reason(result: Dict[str, Any], now: float) -> str:
    """Text of a denied check_opening / claim_or_refresh result."""
    holder = result.get("holder")
    if result.get("kind") == "error":
        return (f"{result.get('error')}; PROD openings fail closed until it is repaired. Check it with "
                f"`{STATUS_COMMAND}`; after the user approves, `{TAKE_COMMAND}` replaces it with this session.")
    held = describe(holder, now) if isinstance(holder, dict) else "another session"
    if result.get("kind") == "unknown_caller":
        return (f"the trading lease is held by {held} and this call carries no session id, so it cannot be "
                "matched to the holder.")
    return (f"the trading lease is held by {held}: one session at a time may open PROD positions (it goes stale "
            f"after {LEASE_STALE_SECONDS // 60} min without a heartbeat). Check it with `{STATUS_COMMAND}`; to take "
            f"it over, ask the user and run `{TAKE_COMMAND}` (it needs the user's explicit approval). Risk-reducing "
            "commands never need it.")


def _decide(record: Optional[Dict[str, Any]], session: Optional[str], now: float) -> Dict[str, Any]:
    """allow / deny for an opening by `session` given the current (valid) lease."""
    if record is None:
        return {"allow": True, "kind": "none", "holder": None}
    if not session:  # a lease (stale or not) with a caller that cannot be matched to it: fail closed
        return {"allow": False, "kind": "unknown_caller", "holder": record}
    if is_stale(record, now):
        return {"allow": True, "kind": "stale", "holder": record}
    if record["session_id"] == session:
        return {"allow": True, "kind": "held", "holder": record}
    return {"allow": False, "kind": "other", "holder": record}


def check_opening(base_dir: str, session: Optional[str], now: float) -> Dict[str, Any]:
    """Read-only check of an opening by `session` (None: caller unknown). Returns {allow, kind, holder[, error]}:
    kind "none" (no lease), "stale", "held" (the caller holds it), "other" / "unknown_caller" (denied; an unknown
    caller is denied by any lease, a stale one included), "error" (unreadable / malformed: denied)."""
    try:
        record = load(base_dir)
    except LeaseError as e:
        return {"allow": False, "kind": "error", "holder": None, "error": str(e)}
    return _decide(record, session, now)


def _write(base_dir: str, record: Dict[str, Any]) -> None:
    try:
        atomic_write_json(lease_path(base_dir), record)
    except Exception as e:
        raise LeaseError(f"trading lease not written ({type(e).__name__})")


@contextlib.contextmanager
def _locked_update(base_dir: str):
    """Holds the lease lock for the block; raises LeaseError when it was not acquired within LOCK_WAIT_S (file_lock
    itself would proceed unlocked)."""
    with locked(lease_path(base_dir), wait_s=LOCK_WAIT_S) as held:
        if not held:
            raise LeaseError(f"trading lease lock not acquired within {LOCK_WAIT_S:g}s; nothing written")
        yield


def claim_or_refresh(base_dir: str, session: Optional[str], runtime: Optional[str], now: float,
                     env: str = "prod") -> Dict[str, Any]:
    """Atomic check-and-set at an allowed PROD opening: under the lock, re-reads the lease and claims it (none or
    stale) or refreshes heartbeat_at (held by `session`); another fresh holder or an unknown caller with a lease
    is denied and nothing is written. An unknown caller (session None) never claims. Idempotent for one session.
    Returns the check_opening dict ("error" on an unreadable lease, a lock not acquired or a failed write)."""
    if not session:
        return check_opening(base_dir, None, now)
    try:
        with _locked_update(base_dir):
            result = _decide(load(base_dir), session, now)
            if not result["allow"]:
                return result
            holder = result["holder"]
            if result["kind"] == "held":
                record = dict(holder, heartbeat_at=int(now))
            else:
                record = {"session_id": session, "runtime": runtime or "unknown", "acquired_at": int(now),
                          "heartbeat_at": int(now), "env": env}
            _write(base_dir, record)
            return dict(result, holder=record)
    except LeaseError as e:
        return {"allow": False, "kind": "error", "holder": None, "error": str(e)}


def refresh_if_holder(base_dir: str, session: Optional[str], now: float) -> bool:
    """Refreshes heartbeat_at when `session` holds the lease (stale or not); never acquires. True when written.
    Raises LeaseError on an unreadable lease, a lock not acquired or a failed write."""
    if not session:
        return False
    with _locked_update(base_dir):
        record = load(base_dir)
        if record is None or record["session_id"] != session:
            return False
        _write(base_dir, dict(record, heartbeat_at=int(now)))
        return True


def take(base_dir: str, session: str, runtime: Optional[str], now: float, env: str = "prod") -> Dict[str, Any]:
    """Takeover the user approved: the lease now belongs to `session` (whatever it held before, a malformed file
    included). Raises LeaseError on a lock not acquired or a failed write."""
    if not session:
        raise LeaseError("takeover without a session id")
    with _locked_update(base_dir):
        record = {"session_id": session, "runtime": runtime or "unknown", "acquired_at": int(now),
                  "heartbeat_at": int(now), "env": env}
        _write(base_dir, record)
        return record


def status(base_dir: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(lease or None, error or None) for read-only reporting (doctor, brief, --status)."""
    try:
        return load(base_dir), None
    except LeaseError as e:
        return None, str(e)


def status_line(base_dir: str, now: Optional[float] = None) -> str:
    """One informational line for the doctor and the brief (never names the lease file)."""
    now = time.time() if now is None else now
    record, error = status(base_dir)
    if error:
        return f"Trading lease unreadable ({error}): PROD openings are denied until it is repaired or taken over."
    if record is None:
        return "Trading lease free: the next PROD opening claims it."
    state = "STALE, the next PROD opening claims it" if is_stale(record, now) else "active"
    return f"Trading lease held by {describe(record, now)} ({state})."
