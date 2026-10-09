#!/usr/bin/env python3
"""
recheck_brief.py - Inputs of `prime_evaluator_brief.py --recheck SYMBOL:DIRECTION` (issue #267). Stdlib only.

When the user confirms a candidate after its dossier expired, the re-check builds a normal brief (fresh ground
truth, macro BTC, risk profile, lessons) with only that candidate's LIVE setup, recomputed by the screening code
(`screening_pipeline.py --recheck`, the same 60 s subprocess as a full scan) and never copied from the old brief.
- The old plan comes only from logs/evaluations/latest_dossier.json re-verified against the evaluator transcript
  (dossier_provenance.rebuild_verified_record; its expiry is not checked): an APPROVED record approving that
  symbol and direction for the same environment. Anything else, a YOLO candidate (needs a full scan) or a latest
  dossier that is itself a re-check (carries `recheck_of`: no chained re-checks) refuses the re-check
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
from typing import Any, Dict, Optional, Tuple

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


def load_confirmed_plan(symbol: str, direction: str, target_env: str, base_dir: str) -> Dict[str, Any]:
    """`recheck_of` snapshot of the confirmed candidate, taken only from the provenance-verified rebuilt record of
    latest_dossier.json (expiry not checked). Raises RecheckError when the record is missing, already a re-check
    (it carries `recheck_of` / `recheck_bounds`), unverifiable, not APPROVED, for another environment, does not approve symbol + direction, or the candidate is YOLO."""
    path = dp.default_dossier_path(base_dir)
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
    prov = rebuilt.get("provenance") if isinstance(rebuilt.get("provenance"), dict) else {}
    return dict({"sha256": prov.get("sha256"), "symbol": symbol, "direction": direction},
                **{k: cand.get(k) for k in SNAPSHOT_KEYS},
                evaluated_ts=rebuilt.get("timestamp_ts"), valid_until_ts=rebuilt.get("valid_until_ts"))


def fetch_recheck_payload(symbol: str, direction: str, target_env: str, run_id: str, base_dir: str) -> dict:
    """The live setup from `screening_pipeline.py --recheck SYMBOL:DIRECTION --json` (a subprocess, like the
    full scan). {} on any failure."""
    script = os.path.join(base_dir, "scripts", "screening_pipeline.py")
    env = dict(os.environ, **{RUN_ID_ENV: run_id})
    try:
        res = subprocess.run([sys.executable, script, "--json", "--env", target_env, "--recheck",
                              f"{symbol}:{direction}"], capture_output=True, text=True,
                             timeout=PIPELINE_TIMEOUT_S, env=env)
        if res.returncode == 0 and res.stdout.strip():
            payload = json.loads(res.stdout.strip())
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {}


def recheck_inputs(payload: Any, symbol: str, direction: str, run_id: str) -> Tuple[dict, dict]:
    """(screening payload for the brief, `recheck` block). The screening keeps at most the one candidate of
    symbol + direction, and only when the pipeline found it. A failed, foreign or inconsistent payload is
    `unavailable` with no candidate (fail closed)."""
    block = {"symbol": symbol, "direction": direction, "setup_status": "unavailable", "cause": None}
    rc = payload.get("recheck") if isinstance(payload, dict) else None
    if not isinstance(rc, dict):
        block["cause"] = "screening pipeline failed"
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
