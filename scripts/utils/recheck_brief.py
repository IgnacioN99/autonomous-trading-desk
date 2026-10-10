#!/usr/bin/env python3
"""
recheck_brief.py - Inputs of `prime_evaluator_brief.py --recheck SYMBOL:DIRECTION` (issue #267). Stdlib only.

When the user confirms a candidate after its dossier expired, the re-check builds a normal brief (fresh ground
truth, macro BTC, risk profile, lessons) with only that candidate's LIVE setup, recomputed by the screening code
(`screening_pipeline.py --recheck`, the same 60 s subprocess as a full scan) and never copied from the old brief.
- The old plan comes only from a dossier record re-verified against the evaluator transcript
  (dossier_provenance.rebuild_verified_record; its expiry is not checked): an APPROVED record approving that
  symbol and direction for the same environment. Issue #270: the brief CLI has no session, so exactly one such
  record (by sha256) among latest_dossier.json and the per-session files must exist. Anything else (none, two
  sessions' plans), a YOLO candidate (needs a full scan) or a dossier that is itself a re-check (carries
  `recheck_of`, or its evaluations_history.jsonl row does, issue #279: no chained re-checks) refuses the re-check
  (RecheckError: no brief is written).
- The brief carries `recheck` {symbol, direction, setup_status found|no_setup|unavailable, cause} and
  `recheck_of` (sha256 and the verified old plan). The YOLO slot is not scanned (empty, never counted as a YOLO
  scan failure). record_evaluation.py links the new dossier to `recheck_of` and prints the bounds verdict
  (utils/recheck_bounds.py).
"""

import json
import os
import subprocess
import sys
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


class RecheckError(Exception):
    """The re-check cannot start (bad argument, no verified approval to re-check, YOLO candidate)."""


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
                        session: Optional[str] = None) -> Dict[str, Any]:
    """`recheck_of` snapshot of the confirmed candidate (issue #270). With a known `session`: that session's own
    dossier file (else latest_dossier.json). Without one (the brief CLI): every dossier file (latest_dossier.json and
    the per-session files) is checked and exactly one distinct verified plan (by sha256) must qualify; two or more
    refuse (ambiguous), none raises latest_dossier.json's own error. Issue #279: an unreadable evaluation history
    refuses (the chain guard cannot read it)."""
    history = _history_lines(base_dir)
    if session:
        return _plan_from_file(dp.resolve_dossier_path(base_dir, session=session), symbol, direction, target_env,
                               history)
    latest = dp.default_dossier_path(base_dir)
    plans, latest_error = {}, None
    for path in dp.dossier_paths(base_dir):
        if _is_recheck_of(path, symbol, direction, history):  # that plan was already re-checked: no chained re-checks
            raise RecheckError("the latest dossier is already a re-check: ask the user again or run a full scan")
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
    raise latest_error or RecheckError("no readable evaluation dossier to re-check: run a full scan")


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


def _history_is_recheck(history: List[str], sha256: Any) -> bool:
    """Issue #279: True when a history row of this sha256 carries `recheck_of` (it sits outside the hash, so
    deleting it from the dossier file by hand cannot reopen a chain). A row that mentions the sha256 but does not
    parse raises RecheckError (the record is not provably an original)."""
    if not isinstance(sha256, str) or not sha256:
        return False
    for line in history:
        if sha256 not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise RecheckError("the evaluation history row of the latest dossier is unreadable: cannot tell whether "
                               "it is already a re-check, ask the user again or run a full scan")
        if isinstance(row, dict) and row.get("sha256") == sha256 and "recheck_of" in row:
            return True
    return False


def _approves(record: dict, symbol: str, direction: str) -> bool:
    return any(isinstance(c, dict) and str(c.get("symbol") or "").upper() == symbol
               and str(c.get("direction") or "").upper() == direction for c in record.get("approved_candidates") or [])


def _is_recheck_of(path: str, symbol: str, direction: str, history: List[str]) -> bool:
    """True when the file holds a re-check dossier of symbol + direction: its stored `recheck_of` or, when that was
    removed, the history row of its sha256 (issue #279)."""
    try:
        record = dp.load_dossier(path)
    except Exception:
        return False
    old = record.get("recheck_of")
    if isinstance(old, dict):
        return str(old.get("symbol") or "").upper() == symbol and str(old.get("direction") or "").upper() == direction
    prov = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    return _approves(record, symbol, direction) and _history_is_recheck(history, prov.get("sha256"))


def _plan_from_file(path: str, symbol: str, direction: str, target_env: str,
                    history: Optional[List[str]] = None) -> Dict[str, Any]:
    """`recheck_of` snapshot of the confirmed candidate, taken only from the provenance-verified rebuilt record of
    one dossier file (expiry not checked). Raises RecheckError when the record is missing, already a re-check
    (it carries `recheck_of` / `recheck_bounds`, or its history row in `history` carries `recheck_of`),
    unverifiable, not APPROVED, for another environment, does not approve symbol + direction, or the candidate
    is YOLO."""
    try:
        record = dp.load_dossier(path)
    except Exception as e:
        raise RecheckError(f"no readable evaluation dossier to re-check ({type(e).__name__}): run a full scan")
    # Drift and age are always measured against the ORIGINAL confirmed plan, never a previous re-check
    if "recheck_of" in record or "recheck_bounds" in record:
        raise RecheckError("the latest dossier is already a re-check: ask the user again or run a full scan")
    ok, reason, rebuilt = dp.rebuild_verified_record(record)
    if not ok or not isinstance(rebuilt, dict):
        raise RecheckError(f"the latest dossier is not provenance-verified ({reason}): run a full scan")
    prov = rebuilt.get("provenance") if isinstance(rebuilt.get("provenance"), dict) else {}
    if _history_is_recheck(history or [], prov.get("sha256")):
        raise RecheckError("the latest dossier is already a re-check: ask the user again or run a full scan")
    if rebuilt.get("status") != "APPROVED":
        raise RecheckError(f"the latest dossier is {rebuilt.get('status')}, not APPROVED: nothing to re-check")
    dossier_env = _norm_env((rebuilt.get("raw_payload") or {}).get("target_env") or record.get("target_env"))
    if dossier_env != _norm_env(target_env):
        raise RecheckError(f"the latest dossier was evaluated for {dossier_env.upper() or 'an unknown environment'}, "
                           f"not {str(target_env).upper()}")
    cand = next((c for c in rebuilt.get("approved_candidates") or [] if isinstance(c, dict)
                 and str(c.get("symbol") or "").upper() == symbol
                 and str(c.get("direction") or "").upper() == direction), None)
    if cand is None:
        raise RecheckError(f"the latest dossier does not approve {symbol} {direction}: run a full scan")
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
    "blocks": {"recheck", "recheck_of"}}. Raises RecheckError before any market data is read."""
    symbol, direction = parse_recheck_spec(spec)
    recheck_of = load_confirmed_plan(symbol, direction, target_env, base_dir)
    if run_id is None:
        import uuid
        run_id = uuid.uuid4().hex
    payload = fetch_recheck_payload(symbol, direction, target_env, run_id, base_dir)
    screening, block = recheck_inputs(payload, symbol, direction, run_id)
    return {"screening": screening, "yolo_slot": dict(RECHECK_YOLO_SLOT),
            "blocks": {"recheck": block, "recheck_of": recheck_of}}


def recheck_summary(brief: dict) -> str:
    """One Markdown line for the parent: the re-check result and the confirmed plan it re-checks."""
    rc, old = brief.get("recheck") or {}, brief.get("recheck_of") or {}
    return (f"**Re-check:** {rc.get('symbol')} {rc.get('direction')} -> `{rc.get('setup_status')}`"
            + (f" ({rc['cause']})" if rc.get("cause") else "")
            + f" | re-checks dossier sha256 {str(old.get('sha256'))[:16]}… (Tier {old.get('tier')}, entry "
              f"{old.get('entry')}, SL {old.get('stop_loss')}, TP2 {old.get('tp2')})")
