#!/usr/bin/env python3
"""
recheck_brief.py - Inputs of `prime_evaluator_brief.py --recheck SYMBOL:DIRECTION` (issue #267). Stdlib only.

When the user confirms a candidate after its dossier expired, the re-check builds a normal brief (fresh ground
truth, macro BTC, risk profile, lessons) with only that candidate's LIVE setup, recomputed by the screening code
(`screening_pipeline.py --recheck`, the same 60 s subprocess as a full scan) and never copied from the old brief.
- The old plan comes only from a dossier record re-verified against the evaluator transcript
  (dossier_provenance.rebuild_verified_record; its expiry is not checked): an APPROVED record approving that
  symbol and direction for the same environment. Issue #270: the brief CLI has no session, so exactly one such
  record (by sha256) among latest_dossier.json and the per-session files must exist (issue #284: under Claude Code,
  CLAUDE_CODE_SESSION_ID selects the caller's own dossier_<session>.json when it exists). Anything else (none, two
  sessions' plans), a YOLO candidate (needs a full scan) or a dossier that is itself a re-check (carries
  `recheck_of`, or its evaluations_history.jsonl row does, issue #279: no chained re-checks) refuses the re-check
  (RecheckError: no brief is written).
- Issue #298: a newer scan of the same session must not silently cancel an earlier approval. When the session's own
  dossier does not approve the candidate (not APPROVED, the candidate is missing, or it is a re-check of ANOTHER
  candidate per its history row), the session's evaluations_history.jsonl rows are searched, newest first: each
  row's evaluator transcript is re-extracted and the record rebuilt and provenance-verified
  (rebuild_verified_record); its sha256 must equal the row's and its parent session the caller's. A row that cannot
  be re-verified is skipped and counted, never trusted. The window is the profile's recheck_max_age_seconds from
  the original evaluation (an older original fails the age bound anyway). The newest approving scan wins (the
  others are listed). One re-check per CANDIDATE of an original dossier: a history row with `recheck_of` = the
  original's sha256 and `recheck_symbol` / `recheck_direction` = the requested candidate refuses; a row without
  those fields (written before them) refuses every candidate of that original. A re-check dossier is never
  re-checkable itself. Only with CLAUDE_CODE_SESSION_ID; another session's scans are never read for approvals.
- The brief carries `recheck` {symbol, direction, setup_status found|no_setup|unavailable, cause} and
  `recheck_of` (sha256 and the verified old plan). The YOLO slot is not scanned (empty, never counted as a YOLO
  scan failure). record_evaluation.py links the new dossier to `recheck_of` and prints the bounds verdict
  (utils/recheck_bounds.py).
"""

import datetime
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

UTILS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(UTILS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from utils import dossier_provenance as dp  # noqa: E402  (stdlib only)
from utils.yolo_scan_health import RUN_ID_ENV  # noqa: E402  (stdlib only)

SETUP_STATUSES = ("found", "no_setup", "unavailable")
RECHECK_YOLO_SLOT = {"status": "UNAVAILABLE", "candidates": [],
                     "summary": "UNAVAILABLE: not scanned by a single-candidate re-check. YOLO slot kept empty."}
PIPELINE_TIMEOUT_S = 60
CAUSE_MAX_CHARS = 200
SNAPSHOT_KEYS = ("tier", "score", "entry", "stop_loss", "tp1", "tp2", "leverage")
HISTORY_FILE = "evaluations_history.jsonl"  # next to latest_dossier.json (record_evaluation._paths)
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"  # issue #284: selects the caller's own dossier file (Claude Code)


class RecheckError(Exception):
    """The re-check cannot start (bad argument, no verified approval to re-check, YOLO candidate)."""


class _NoApproval(RecheckError):
    """Issue #298: the dossier approves nothing for symbol + direction (not APPROVED, or the candidate is missing);
    with a session the window search may still find an earlier approval. `head` is the message without its tail."""

    def __init__(self, head: str, tail: str = "run a full scan"):
        super().__init__(f"{head}: {tail}")
        self.head = head


class _WrongEnv(RecheckError):
    """The verified dossier was evaluated for another environment."""


def parse_recheck_spec(text: Any) -> Tuple[str, str]:
    """"ETHFIUSDT:LONG" -> ("ETHFIUSDT", "LONG"). Any other form raises RecheckError."""
    parts = str(text or "").strip().upper().split(":")
    if len(parts) != 2 or not parts[0].isalnum() or parts[1] not in ("LONG", "SHORT"):
        raise RecheckError(f"--recheck expects SYMBOL:DIRECTION (e.g. ETHFIUSDT:LONG), got '{text}'")
    return parts[0], parts[1]


def _norm_env(value: Any) -> str:
    env = str(value or "").strip().lower()
    return "prod" if env in ("production", "mainnet") else env


def load_confirmed_plan(symbol: str, direction: str, target_env: str, base_dir: str,
                        session: Optional[str] = None, notes: Optional[List[str]] = None) -> Dict[str, Any]:
    """`recheck_of` snapshot of the confirmed candidate (issue #270). With a known `session`: that session's own
    dossier file (else latest_dossier.json). Without one (the brief CLI): the dossier_<session>.json named by
    CLAUDE_CODE_SESSION_ID when it exists (issue #284: its own plan or error; the chain guard reads only that file),
    else every dossier file (latest_dossier.json and the per-session files) is checked and exactly one distinct
    verified plan (by sha256) must qualify; two or more refuse (ambiguous), none raises latest_dossier.json's own
    error. Issue #279: an unreadable evaluation history refuses (the chain guard cannot read it). Issue #298: when the
    session's own file does not approve the candidate, the session's earlier scans within recheck_max_age_seconds are
    searched (_plan_from_session_scans); a candidate already re-checked from its original refuses
    (_refuse_if_rechecked). `notes` receives the lines the CLI
    prints (which earlier scan is re-checked, the other scans approving it)."""
    history = _history_lines(base_dir)
    # Issue #284: the caller's own dossier_<session>.json (CLAUDE_CODE_SESSION_ID, sanitised; a selector only, never a
    # security boundary: the plan is provenance-verified) decides alone when it exists, so another session's
    # re-check never blocks it. No variable or no such file: every file, as before.
    sid = session if session else os.environ.get(SESSION_ENV)
    own = dp.session_dossier_path(base_dir, sid)
    if own and os.path.isfile(own):
        try:
            plan = _plan_from_file(own, symbol, direction, target_env, history)
        except _NoApproval as e:
            return _plan_from_session_scans(sid, e, symbol, direction, target_env, base_dir, history, notes)
        _refuse_if_rechecked(history, plan.get("sha256"), sid, symbol, direction)
        return plan
    latest = dp.default_dossier_path(base_dir)
    if session:
        return _plan_from_file(latest, symbol, direction, target_env, history)
    plans, latest_error = {}, None
    for path in dp.dossier_paths(base_dir):
        rechecked = _is_recheck_of(path, symbol, direction, history)
        if rechecked is not None:  # that plan was already re-checked: no chained re-checks
            raise _chain_refusal(rechecked, symbol, direction)
        try:
            plan = _plan_from_file(path, symbol, direction, target_env, history)
        except RecheckError as e:
            if path == latest:
                latest_error = e
            continue
        plans.setdefault(plan.get("sha256"), plan)
    if len(plans) == 1:
        return next(iter(plans.values()))
    if len(plans) > 1:
        raise RecheckError(f"{len(plans)} verified dossiers of different sessions approve {symbol} {direction}: "
                           "ambiguous, run a full scan")
    if isinstance(latest_error, _NoApproval) and not str(os.environ.get(SESSION_ENV) or "").strip():
        raise RecheckError(f"{latest_error}\n  (earlier scans of this session are searched only with {SESSION_ENV} "
                           "set, under Claude Code)")
    raise latest_error or RecheckError("no readable evaluation dossier to re-check: run a full scan")


def _session_label(session: Any) -> str:
    """The session id sanitised like its dossier file name (issue #284), 'unknown' when absent."""
    label = (dp.SESSION_ID_UNSAFE_RE.sub("_", session.strip())[:dp.SESSION_ID_MAX_CHARS]
             if isinstance(session, str) else "")
    return label or "unknown"


def _recheck_max_age(base_dir: str) -> int:
    """recheck_max_age_seconds of the profile (user_profile.get_recheck_bounds; an unreadable profile = defaults)."""
    import user_profile as up
    try:
        profile = up.load_user_profile(base_dir)
    except Exception:
        profile = {}
    return int(up.get_recheck_bounds(profile)["recheck_max_age_seconds"])


def _utc_ts(text: Any) -> Optional[int]:
    """Epoch of a history row's timestamp_utc ('%Y-%m-%d %H:%M:%S UTC'), None when it does not parse."""
    try:
        return int(datetime.datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S UTC")
                   .replace(tzinfo=datetime.timezone.utc).timestamp())
    except ValueError:
        return None


def _rebuild_from_row(row: dict, session: str) -> Optional[dict]:
    """Issue #298: the provenance-verified record of one history row, rebuilt from its evaluator transcript (found by
    the row's conversation id and provenance source), or None when it cannot be re-verified: transcript missing or
    unreadable, its last <dossier_json> block hashing to another sha256 than the row's, another parent session, or
    rebuild_verified_record failing (checklist included). The row itself is never trusted."""
    try:
        source, conv = row.get("provenance_source"), row.get("conversation_id")
        if source == dp.CLAUDE_SOURCE:
            extracted = dp.extract_dossier_from_claude_transcript(dp.find_claude_subagent_transcript(conv),
                                                                  dp.EVALUATOR_NAME)
        elif source == dp.AGY_SOURCE:
            extracted = dp.extract_dossier_from_transcript(dp.find_subagent_transcript(conv))
        else:
            return None
        if extracted.get("sha256") != row.get("sha256") or extracted.get("parent_conversation_id") != session:
            return None
        ok, _, rebuilt = dp.rebuild_verified_record(dp.build_record_from_extraction(extracted))
    except Exception:
        return None
    prov = rebuilt.get("provenance") if ok and isinstance(rebuilt, dict) else None
    return rebuilt if isinstance(prov, dict) and prov.get("sha256") == row.get("sha256") else None


def _plan_from_session_scans(session: str, no_approval: "_NoApproval", symbol: str, direction: str, target_env: str,
                             base_dir: str, history: List[str], notes: Optional[List[str]]) -> Dict[str, Any]:
    """Issue #298: `recheck_of` snapshot from the newest earlier scan of `session` that approves symbol + direction,
    when the session's own dossier file does not (`no_approval`). Only history rows of this session that are not
    re-checks are read, newest first, and only within recheck_max_age_seconds of the original evaluation (measured
    from the rebuilt record's timestamp_ts; an older original fails the age bound of utils/recheck_bounds.py anyway).
    Each candidate row is re-verified from its transcript (_rebuild_from_row); one that cannot be is skipped and
    counted. The newest approving scan wins; a YOLO candidate or a chained re-check there refuses; the other approving
    scans are listed in `notes`. A candidate already re-checked from that original refuses (_refuse_if_rechecked)."""
    sid, label = session.strip(), _session_label(session)
    max_age, now = _recheck_max_age(base_dir), int(time.time())
    checked, unverifiable, found = 0, 0, []
    for line in reversed(history):  # appended in recording order: newest first
        if sid not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            unverifiable += 1
            continue
        if not isinstance(row, dict) or row.get("parent_conversation_id") != sid or "recheck_of" in row:
            continue
        row_ts = _utc_ts(row.get("timestamp_utc"))
        if row_ts is not None and now - row_ts > max_age:
            continue
        checked += 1
        symbols = row.get("approved_symbols") if isinstance(row.get("approved_symbols"), list) else []
        if str(row.get("status") or "").upper() != "APPROVED" or symbol not in (str(s).upper() for s in symbols):
            continue
        rebuilt = _rebuild_from_row(row, sid)
        if rebuilt is None:
            unverifiable += 1
            continue
        ts = int(rebuilt.get("timestamp_ts") or 0)
        if now - ts > max_age:
            continue
        try:
            found.append((ts, _plan_from_rebuilt(rebuilt, row.get("target_env"), symbol, direction, target_env,
                                                 history)))
        except (_NoApproval, _WrongEnv):
            continue
        except RecheckError as e:  # the newest approval decides: a YOLO candidate or a chained re-check refuses
            found.append((ts, e))
    if not found:
        raise RecheckError(f"{no_approval.head}; no approval of {symbol} {direction} in the last {max_age // 60} min "
                           f"of session {label}: {checked} scans checked, {unverifiable} unverifiable, none approves "
                           "it: run a full scan")
    found.sort(key=lambda item: item[0], reverse=True)  # stable: equal times keep the newest row first
    ts, plan = found[0]
    if isinstance(plan, RecheckError):
        raise plan
    _refuse_if_rechecked(history, plan.get("sha256"), session, symbol, direction)
    if notes is not None:
        notes.append(f"Re-check of an earlier scan of session {label}: dossier sha256 {str(plan.get('sha256'))[:16]}… "
                     f"evaluated {datetime.datetime.fromtimestamp(ts, datetime.timezone.utc):%H:%M:%S UTC} "
                     f"({no_approval.head}).")
        others = [str(p.get("sha256"))[:16] + "…" for _, p in found[1:] if isinstance(p, dict)]
        if others:
            notes.append(f"{symbol} {direction} also approved in: {', '.join(others)} (older, not used).")
    return plan


def _row_candidate(row: Any) -> Optional[Tuple[str, str]]:
    """Issue #298: (symbol, direction) a re-check history row consumed (`recheck_symbol` / `recheck_direction`,
    record_evaluation._persist), or None for a row written before them (or with unusable values)."""
    if not isinstance(row, dict):
        return None
    sym, dirn = row.get("recheck_symbol"), row.get("recheck_direction")
    if not isinstance(sym, str) or not isinstance(dirn, str) or not sym.strip() or not dirn.strip():
        return None
    return sym.strip().upper(), dirn.strip().upper()


def _refuse_if_rechecked(history: List[str], sha256: Any, session: Any, symbol: str, direction: str) -> None:
    """Issue #298: one re-check per CANDIDATE of an original dossier. Refuses when a history row carries `recheck_of`
    equal to this sha256 and names symbol + direction as its re-checked candidate; a row with `recheck_of` but without
    the candidate fields (written before them) refuses every candidate of that original, and a row naming the sha256
    that does not parse refuses too (cannot tell, fail closed). A re-check of another candidate does not refuse."""
    if not isinstance(sha256, str) or not sha256:
        return
    label = _session_label(session)
    for line in history:
        if sha256 not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise RecheckError("an evaluation history row naming the dossier to re-check is unreadable: cannot tell "
                               "whether it was already re-checked, ask the user again or run a full scan")
        if not isinstance(row, dict) or row.get("recheck_of") != sha256:
            continue
        cand = _row_candidate(row)
        if cand is None:
            raise RecheckError(f"the dossier sha256 {sha256[:16]}… of session {label} was already re-checked by a "
                               "history row that does not name its candidate (written before issue #298): every "
                               f"candidate of it is refused, {symbol} {direction} included: ask the user again or "
                               "run a full scan")
        if cand == (symbol, direction):
            raise RecheckError(f"{symbol} {direction} of the dossier sha256 {sha256[:16]}… of session {label} was "
                               "already re-checked (one re-check per candidate of an original dossier): ask the user "
                               "again or run a full scan")


def _history_lines(base_dir: str) -> List[str]:
    """Lines of logs/evaluations/evaluations_history.jsonl (record_evaluation._persist appends one row per recorded
    dossier; a re-check row carries `recheck_of`). A missing file is empty; an unreadable one raises RecheckError."""
    path = os.path.join(os.path.dirname(dp.default_dossier_path(base_dir)), HISTORY_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()
    except (OSError, ValueError) as e:
        raise RecheckError(f"the evaluation history is unreadable ({type(e).__name__}): cannot tell whether the "
                           "latest dossier is already a re-check, ask the user again or run a full scan")


def _history_recheck_row(history: List[str], sha256: Any) -> Optional[dict]:
    """Issue #279: the history row of this sha256 when it carries `recheck_of` (it sits outside the hash, so
    deleting it from the dossier file by hand cannot reopen a chain), else None. A row that mentions the sha256 but
    does not parse raises RecheckError (the record is not provably an original)."""
    if not isinstance(sha256, str) or not sha256:
        return None
    for line in history:
        if sha256 not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise RecheckError("the evaluation history row of the latest dossier is unreadable: cannot tell whether "
                               "it is already a re-check, ask the user again or run a full scan")
        if isinstance(row, dict) and row.get("sha256") == sha256 and "recheck_of" in row:
            return row
    return None


def _history_is_recheck(history: List[str], sha256: Any) -> bool:
    return _history_recheck_row(history, sha256) is not None


def _approves(record: dict, symbol: str, direction: str) -> bool:
    return any(isinstance(c, dict) and str(c.get("symbol") or "").upper() == symbol
               and str(c.get("direction") or "").upper() == direction for c in record.get("approved_candidates") or [])


def _is_recheck_of(path: str, symbol: str, direction: str, history: List[str]) -> Optional[dict]:
    """The file's record when it holds a re-check dossier of symbol + direction (its stored `recheck_of` or, when
    that was removed, the history row of its sha256, issue #279), else None."""
    try:
        record = dp.load_dossier(path)
    except Exception:
        return None
    old = record.get("recheck_of")
    if isinstance(old, dict):
        same = (str(old.get("symbol") or "").upper() == symbol
                and str(old.get("direction") or "").upper() == direction)
        return record if same else None
    prov = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    return record if _approves(record, symbol, direction) and _history_is_recheck(history, prov.get("sha256")) else None


def _chain_refusal(record: Any, symbol: str, direction: str) -> RecheckError:
    """Issue #284: the chain refusal names the session (sanitised like its file name) of the dossier that is already
    a re-check; issue #298: and the requested candidate."""
    session = record.get("parent_conversation_id") if isinstance(record, dict) else None
    return RecheckError(f"the dossier of session {_session_label(session)} is already a re-check and cannot be "
                        f"re-checked for {symbol} {direction}: ask the user again or run a full scan")


def _recheck_dossier_error(record: dict, symbol: str, direction: str, history: Optional[List[str]]) -> RecheckError:
    """Error for a dossier that is itself a re-check: it is never re-checkable itself. Issue #298: when its history row
    names the candidate it re-checked (`recheck_symbol` / `recheck_direction`), that candidate is not the requested
    one and the stored `recheck_of` (when present) agrees, it is a _NoApproval (the session's earlier scans may still
    approve the requested candidate, each under the per-candidate guard); otherwise the chain refusal (an old row, a
    missing row or a conflict refuses, fail closed)."""
    prov = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    row_cand = _row_candidate(_history_recheck_row(history or [], prov.get("sha256")))
    old = record.get("recheck_of")
    stored = (_row_candidate({"recheck_symbol": old.get("symbol"), "recheck_direction": old.get("direction")})
              if isinstance(old, dict) else None)
    if row_cand is not None and row_cand != (symbol, direction) and ("recheck_of" not in record or stored == row_cand):
        return _NoApproval(f"the latest dossier is a re-check of {row_cand[0]} {row_cand[1]} and does not approve "
                           f"{symbol} {direction}")
    return _chain_refusal(record, symbol, direction)


def _plan_from_file(path: str, symbol: str, direction: str, target_env: str,
                    history: Optional[List[str]] = None) -> Dict[str, Any]:
    """`recheck_of` snapshot of the confirmed candidate, taken only from the provenance-verified rebuilt record of
    one dossier file (expiry not checked). Raises RecheckError when the record is missing, already a re-check
    (it carries `recheck_of` / `recheck_bounds`, or its history row in `history` carries `recheck_of`),
    unverifiable, not APPROVED, for another environment, does not approve symbol + direction, or the candidate
    is YOLO. Issue #298: a record that is not APPROVED, a missing candidate and a re-check of another candidate
    (_recheck_dossier_error) raise _NoApproval (the session's earlier scans may still approve it)."""
    try:
        record = dp.load_dossier(path)
    except Exception as e:
        raise RecheckError(f"no readable evaluation dossier to re-check ({type(e).__name__}): run a full scan")
    # Drift and age are always measured against the ORIGINAL confirmed plan, never a previous re-check
    if "recheck_of" in record or "recheck_bounds" in record:
        raise _recheck_dossier_error(record, symbol, direction, history)
    ok, reason, rebuilt = dp.rebuild_verified_record(record)
    if not ok or not isinstance(rebuilt, dict):
        raise RecheckError(f"the latest dossier is not provenance-verified ({reason}): run a full scan")
    return _plan_from_rebuilt(rebuilt, record.get("target_env"), symbol, direction, target_env, history)


def _plan_from_rebuilt(rebuilt: dict, stored_env: Any, symbol: str, direction: str, target_env: str,
                       history: Optional[List[str]] = None) -> Dict[str, Any]:
    """The checks of _plan_from_file after provenance verification, on a rebuilt record (`stored_env`: the stored
    target_env, used when the evaluator's payload has none)."""
    prov = rebuilt.get("provenance") if isinstance(rebuilt.get("provenance"), dict) else {}
    if _history_is_recheck(history or [], prov.get("sha256")):
        raise _recheck_dossier_error(rebuilt, symbol, direction, history)
    if rebuilt.get("status") != "APPROVED":
        raise _NoApproval(f"the latest dossier is {rebuilt.get('status')}, not APPROVED", "nothing to re-check")
    dossier_env = _norm_env((rebuilt.get("raw_payload") or {}).get("target_env") or stored_env)
    if dossier_env != _norm_env(target_env):
        raise _WrongEnv(f"the latest dossier was evaluated for {dossier_env.upper() or 'an unknown environment'}, "
                        f"not {str(target_env).upper()}")
    cand = next((c for c in rebuilt.get("approved_candidates") or [] if isinstance(c, dict)
                 and str(c.get("symbol") or "").upper() == symbol
                 and str(c.get("direction") or "").upper() == direction), None)
    if cand is None:
        raise _NoApproval(f"the latest dossier does not approve {symbol} {direction}")
    if cand.get("is_yolo") is True or str(cand.get("is_yolo")).strip().lower() in ("true", "1", "yes"):
        raise RecheckError(f"{symbol} is a YOLO (memecoin slot) candidate: --recheck does not support it, "
                           "it needs a full scan")
    return dict({"sha256": prov.get("sha256"), "symbol": symbol, "direction": direction},
                **{k: cand.get(k) for k in SNAPSHOT_KEYS},
                evaluated_ts=rebuilt.get("timestamp_ts"), valid_until_ts=rebuilt.get("valid_until_ts"))


def fetch_recheck_payload(symbol: str, direction: str, target_env: str, run_id: str, base_dir: str) -> dict:
    """The live setup from `screening_pipeline.py --recheck SYMBOL:DIRECTION --json` (a subprocess, like the
    full scan). On any failure only {"recheck_failure": <class>} (issue #279: the exception class, `exit <code>`,
    `empty output` or `not a JSON object`), which recheck_inputs turns into an `unavailable` cause."""
    script = os.path.join(base_dir, "scripts", "screening_pipeline.py")
    env = dict(os.environ, **{RUN_ID_ENV: run_id})
    try:
        res = subprocess.run([sys.executable, script, "--json", "--env", target_env, "--recheck",
                              f"{symbol}:{direction}"], capture_output=True, text=True,
                             timeout=PIPELINE_TIMEOUT_S, env=env)
        if res.returncode != 0:
            return {"recheck_failure": f"exit {res.returncode}"}
        if not res.stdout.strip():
            return {"recheck_failure": "empty output"}
        payload = json.loads(res.stdout.strip())
        return payload if isinstance(payload, dict) else {"recheck_failure": "not a JSON object"}
    except Exception as e:
        return {"recheck_failure": type(e).__name__}


def recheck_inputs(payload: Any, symbol: str, direction: str, run_id: str) -> Tuple[dict, dict]:
    """(screening payload for the brief, `recheck` block). The screening keeps at most the one candidate of
    symbol + direction, and only when the pipeline found it. A failed, foreign or inconsistent payload is
    `unavailable` with no candidate (fail closed)."""
    block = {"symbol": symbol, "direction": direction, "setup_status": "unavailable", "cause": None}
    rc = payload.get("recheck") if isinstance(payload, dict) else None
    if not isinstance(rc, dict):
        failure = payload.get("recheck_failure") if isinstance(payload, dict) else None
        block["cause"] = "screening pipeline failed" + (f" ({str(failure)[:CAUSE_MAX_CHARS // 2]})" if failure else "")
        return {}, block
    if payload.get("run_id") != run_id:
        block["cause"] = "screening payload run id mismatch"
        return {}, block
    if str(rc.get("symbol") or "").upper() != symbol or str(rc.get("direction") or "").upper() != direction:
        block["cause"] = "screening payload is for another symbol or direction"
        return {}, block
    status = rc.get("setup_status") if rc.get("setup_status") in SETUP_STATUSES else "unavailable"
    cause = str(rc.get("cause"))[:CAUSE_MAX_CHARS] if rc.get("cause") else None
    cands = [c for c in payload.get("top_candidates") or [] if isinstance(c, dict)
             and str(c.get("symbol") or "").upper() == symbol and str(c.get("direction") or "").upper() == direction]
    if str(payload.get("market_data_status") or "").startswith("UNAVAILABLE"):
        status, cause = "unavailable", cause or str(payload["market_data_status"])[:CAUSE_MAX_CHARS]
    if status == "found" and not cands:
        status, cause = "unavailable", "screening reported a setup without its candidate row"
    block.update(setup_status=status, cause=cause)
    screening = dict(payload, top_candidates=cands[:1] if status == "found" else [])
    screening.pop("recheck", None)
    return screening, block


def prepare_recheck(spec: Any, target_env: str, base_dir: str, run_id: Optional[str] = None) -> dict:
    """The `recheck` argument of prime_evaluator_brief.assemble_primed_brief: {"screening", "yolo_slot",
    "blocks": {"recheck", "recheck_of"}, "notes"} (`notes`: lines for the CLI only, never in the brief, issue #298).
    Raises RecheckError before any market data is read."""
    symbol, direction = parse_recheck_spec(spec)
    notes: List[str] = []
    recheck_of = load_confirmed_plan(symbol, direction, target_env, base_dir, notes=notes)
    if run_id is None:
        import uuid
        run_id = uuid.uuid4().hex
    payload = fetch_recheck_payload(symbol, direction, target_env, run_id, base_dir)
    screening, block = recheck_inputs(payload, symbol, direction, run_id)
    return {"screening": screening, "yolo_slot": dict(RECHECK_YOLO_SLOT),
            "blocks": {"recheck": block, "recheck_of": recheck_of}, "notes": notes}


def recheck_summary(brief: dict) -> str:
    """One Markdown line for the parent: the re-check result and the confirmed plan it re-checks (issue #298: with
    the plan's evaluated-at and valid-until times in UTC, `unknown` when absent)."""
    rc, old = brief.get("recheck") or {}, brief.get("recheck_of") or {}

    def _utc(ts):
        try:
            return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (TypeError, ValueError, OverflowError, OSError):
            return "unknown"

    return (f"**Re-check:** {rc.get('symbol')} {rc.get('direction')} -> `{rc.get('setup_status')}`"
            + (f" ({rc['cause']})" if rc.get("cause") else "")
            + f" | re-checks dossier sha256 {str(old.get('sha256'))[:16]}… (Tier {old.get('tier')}, entry "
              f"{old.get('entry')}, SL {old.get('stop_loss')}, TP2 {old.get('tp2')}; evaluated at "
              f"{_utc(old.get('evaluated_ts'))}, valid until {_utc(old.get('valid_until_ts'))})")
