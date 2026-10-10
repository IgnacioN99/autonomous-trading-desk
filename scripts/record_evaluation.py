#!/usr/bin/env python3
"""
record_evaluation.py - Atomic registration of the evaluator subagent dossier.

Persists the verdict emitted by the 'isolated_market_evaluator' subagent into
logs/evaluations/dossier_<session>.json (its parent session, issue #270) and, as the newest scan overall,
logs/evaluations/latest_dossier.json. Those files are the authorization token required by the pre_trade_guard.py
hook (PROD: the calling session's own file) and execute_futures_trade.py before any order is dispatched.

Canonical flow (PROD and TESTNET):
  1. python3 scripts/prime_evaluator_brief.py
  2. agy:         invoke_subagent(TypeName="isolated_market_evaluator")  -> returns its conversationId
     Claude Code: Agent tool, subagent_type "isolated_market_evaluator"   -> reports its agentId
  3. agy:         python3 scripts/record_evaluation.py --from-subagent <conversationId>
     Claude Code: python3 scripts/record_evaluation.py --from-claude-subagent <agentId>
     (--from-subagent also accepts a Claude Code agentId; the id format is auto-detected)

The recorder reads the <dossier_json> block the subagent itself emitted in its transcript and
stores a provenance stamp (source, transcript path, step, sha256) that every consumer re-verifies.
Claude Code transcripts must carry agentType "isolated_market_evaluator" in their meta.json.
Truncated agy rows are resolved from transcript_full.jsonl (paired by step_index; dossier_provenance.py).
The message carrying the block must hold a '## Precondition Checklist' consistent with it (C4.2 = status,
K1-K4 and C3.1 checked and K4 APPROVED/DOWNGRADED for every approved candidate;
dossier_provenance.check_precondition_checklist): an inconsistent APPROVED dossier is refused in PROD (exit 2)
and recorded with a warning in TESTNET; REJECTED/NEUTRAL dossiers are always recorded, with a warning.
The PROD trade gate re-runs the same check on the transcript (dossier_provenance.rebuild_verified_record).
Dossiers typed by hand are NOT accepted in PROD.
Issue #298: the summary prints the brief's generated_at, the evaluation time and the validity as UTC date-times, and
per approved candidate an advisory `Delta (est.)` line plus the approved set's ratio (utils/delta_fit.py, cached book
and brief risk_profile only; UNKNOWN when unverifiable; nothing stored, no exchange call; Gate 1 decides).

Legacy manual paths (TESTNET only, stored as schema_version 1 / source "manual_testnet"):
  python3 scripts/record_evaluation.py --env testnet --symbols TIAUSDT,SAGAUSDT --directions LONG,SHORT
  python3 scripts/record_evaluation.py --env testnet --json-file path/to/dossier.json
  echo '<dossier_json>{...}</dossier_json>' | python3 scripts/record_evaluation.py --env testnet
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from typing import Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from utils.atomic_writer import atomic_write_json, atomic_append_jsonl  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402

# Workspace root resolved relative to this script (scripts/ -> repo root). Tests may patch BASE_DIR.
BASE_DIR = os.path.dirname(SCRIPTS_DIR)

TTL_SECONDS = dp.TTL_SECONDS  # 20 minutes, counted from the moment the evaluator emitted the dossier
LEGACY_SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


class RecordRefused(Exception):
    """Raised when policy forbids recording the dossier (exit code 2)."""


def _paths(base_dir: Optional[str] = None):
    base = base_dir or BASE_DIR
    eval_dir = os.path.join(base, "logs", "evaluations")
    return base, os.path.join(eval_dir, "latest_dossier.json"), os.path.join(eval_dir, "evaluations_history.jsonl")


def _resolve_env(explicit_env: Optional[str] = None, base_dir: Optional[str] = None) -> str:
    from utils.env_resolver import resolve_env
    return resolve_env(explicit_env, base_dir=base_dir or BASE_DIR)


def _fmt_utc(ts: int, fmt: str = "%Y-%m-%d %H:%M:%S UTC") -> str:
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime(fmt)


def _rel(path: str, base: str) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


def _register_shadow() -> None:
    """Enrolls unapproved/disqualified candidates into counterfactual shadow tracking (best effort)."""
    try:
        import shadow_tracker
        stats = {"deduped_window": 0}
        shadow_count = shadow_tracker.register_from_evaluation(stats)
        if shadow_count > 0 or stats["deduped_window"]:
            print(f"👻 SHADOW TRACKER: {shadow_count} candidate(s) enrolled into counterfactual efficacy auditing "
                  f"(deduped_window {stats['deduped_window']}).")
    except Exception:
        pass


RADAR_SNAPSHOT_WINDOW_S = 900  # the brief's radar scores must be at most 15 min older than the dossier


def build_radar_snapshots(record: dict, base_dir: Optional[str] = None) -> Optional[dict]:
    """Join of each approved candidate with the radar row that prime_evaluator_brief.py wrote to
    logs/primed_brief_scores.json (issue #202). Keyed "SYMBOL|DIRECTION"; each value is {radar_snapshot,
    radar_snapshot_reason} with reason missing / unreadable / stale / no_match when the row is null. It is stored
    outside the provenance sha256 (which hashes only the evaluator's <dossier_json>). Readers: the executor's audit
    record, and the calibrated Tier S gate (utils.score_calibration.radar_snapshot_matches, in the executor and the
    guard), which asks the user unless the snapshot confidence equals the dossier score and the record's sha256 is
    the one the gate validated."""
    cands = [c for c in record.get("approved_candidates") or [] if isinstance(c, dict)]
    if str(record.get("status", "")).upper() != "APPROVED" or not cands:
        return None
    path = os.path.join(base_dir or BASE_DIR, "logs", "primed_brief_scores.json")
    reason, rows = None, []
    if not os.path.exists(path):
        reason = "missing"
    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            generated_at = int(data["generated_at_ts"])
            rows = [r for r in data["rows"] if isinstance(r, dict)]
            sidecar_env = str(data.get("env") or "").lower()
        except Exception:
            reason = "unreadable"
        else:
            ts = int(record.get("timestamp_ts") or 0)
            if not ts - RADAR_SNAPSHOT_WINDOW_S <= generated_at <= ts:
                reason = "stale"
            elif sidecar_env and record.get("target_env") and sidecar_env != str(record["target_env"]).lower():
                rows = []  # scores of another environment never match
    out = {}
    for c in cands:
        symbol, direction = str(c.get("symbol") or "").upper(), str(c.get("direction") or "").upper()
        row = None if reason else next((r for r in rows if str(r.get("symbol") or "").upper() == symbol
                                        and str(r.get("direction") or "").upper() == direction), None)
        out[f"{symbol}|{direction}"] = {"radar_snapshot": row,
                                        "radar_snapshot_reason": reason or (None if row else "no_match")}
    return out


def attach_recheck(record: dict, base_dir: Optional[str] = None, now_ts: Optional[int] = None) -> Optional[str]:
    """Issue #267: when the brief (logs/primed_brief.json) is a `--recheck` brief and this dossier was evaluated on
    it, stores the brief's `recheck_of` (old dossier sha256 and its verified plan) and the deterministic bounds
    verdict (utils/recheck_bounds.py, profile bounds from user_profile.get_recheck_bounds) as `recheck_of` /
    `recheck_bounds`, outside the provenance sha256 (like radar_snapshots). Linked only when the dossier's
    `brief_generated_at_ts` equals the brief's `generated_at_ts` (issue #279: a dossier without that field is never
    linked). Never blocks recording; nothing is stored without a link. Returns a warning (the caller prints
    `Re-check: NOT LINKED (no verdict)`) when the brief is missing or unparseable, or is a re-check brief this
    dossier is not linked to; None for a normal brief or a linked dossier (issue #298: a normal brief that replaced
    this dossier's own brief is reported by brief_replaced_warning)."""
    path = os.path.join(base_dir or BASE_DIR, "logs", "primed_brief.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            brief = json.load(f)
        if not isinstance(brief, dict):
            raise ValueError("not a JSON object")
    except Exception as e:
        return (f"logs/primed_brief.json is missing or unreadable ({type(e).__name__}): no re-check verdict, ask "
                "the user again before executing a re-checked plan")
    recheck_of = brief.get("recheck_of")
    if not isinstance(recheck_of, dict) and brief.get("recheck") is None:
        return None
    raw = (record.get("raw_payload") or {}).get("brief_generated_at_ts")
    try:
        linked = (isinstance(recheck_of, dict) and raw is not None
                  and int(raw) == int(brief.get("generated_at_ts")))
    except (TypeError, ValueError):
        linked = False
    if linked and str(brief.get("target_env") or "").lower() not in ("", str(record.get("target_env") or "").lower()):
        linked = False
    if not linked:
        return ("the brief is a --recheck brief but this dossier was not evaluated on it (brief_generated_at_ts "
                "missing or different): not linked to the confirmed plan, ask the user again before executing")
    record["recheck_of"] = dict(recheck_of)
    try:
        from utils import recheck_bounds as rb
        import user_profile as up
        try:
            profile = up.load_user_profile(base_dir or BASE_DIR)
        except Exception:
            profile = {}
        bounds = up.get_recheck_bounds(profile)
        verdict = rb.evaluate_recheck_bounds(recheck_of, record, int(now_ts if now_ts is not None else time.time()),
                                             bounds["recheck_max_drift_r"], bounds["recheck_max_age_seconds"])
        record["recheck_bounds"] = dict(verdict, bounds=bounds)
    except Exception as e:  # never blocks recording; an unknown verdict asks the user again
        record["recheck_bounds"] = {"within_bounds": False, "checks": [],
                                    "reasons": [f"bounds check failed ({type(e).__name__})"]}
    return None


def brief_replaced_warning(record: dict, base_dir: Optional[str] = None) -> Optional[str]:
    """Issue #298: a warning when logs/primed_brief.json is a normal brief whose generated_at_ts differs from this
    dossier's brief_generated_at_ts (both present): another brief replaced the one the dossier was evaluated on before
    it was recorded. The dossier itself does not say whether that brief was a --recheck brief, so the warning names
    the consequence for a re-check (no verdict) without claiming it is one. None otherwise (also when the brief is
    missing, unreadable or a re-check brief: attach_recheck reports those). Never raises."""
    try:
        with open(os.path.join(base_dir or BASE_DIR, "logs", "primed_brief.json"), "r", encoding="utf-8") as f:
            brief = json.load(f)
        if not isinstance(brief, dict) or isinstance(brief.get("recheck_of"), dict) or brief.get("recheck") is not None:
            return None
        raw = (record.get("raw_payload") or {}).get("brief_generated_at_ts")
        if raw is None or brief.get("generated_at_ts") is None or int(raw) == int(brief["generated_at_ts"]):
            return None
    except Exception:
        return None
    return (f"logs/primed_brief.json (generated_at_ts {brief.get('generated_at_ts')}) is not the brief this dossier "
            f"was evaluated on (brief_generated_at_ts {raw}): another brief replaced it before recording. If this "
            "dossier answers a --recheck, it has no re-check verdict: ask the user again before executing")


SESSION_DOSSIER_PRUNE_AFTER_S = 6 * 3600  # per-session files untouched for longer are deleted (best effort)


def _prune_session_dossiers(base: str, keep: str, now_ts: Optional[float] = None) -> None:
    """Deletes per-session dossier files (dp.SESSION_DOSSIER_PREFIX) older than SESSION_DOSSIER_PRUNE_AFTER_S by
    mtime, except `keep`. Never touches latest_dossier.json or the history; never raises."""
    cutoff = (now_ts if now_ts is not None else time.time()) - SESSION_DOSSIER_PRUNE_AFTER_S
    for path in dp.dossier_paths(base)[1:]:
        try:
            if os.path.abspath(path) != os.path.abspath(keep) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            continue


def _superseded_approvals(session_file: Optional[str], record: dict, now_ts: int) -> list:
    """Issue #298: approvals of the session's previous record (read before it is overwritten) that `record` does not
    approve and that have not expired: [{symbol, direction, tier, valid_until_ts}]. Informational only (the
    previous record is not verified here); [] when there is none or it is unreadable. Never raises."""
    try:
        if not session_file or not os.path.isfile(session_file):
            return []
        previous = dp.load_dossier(session_file)
        valid_until = int(previous.get("valid_until_ts") or 0)
        if str(previous.get("status") or "").upper() != "APPROVED" or valid_until <= now_ts:
            return []
        kept = {(str(c.get("symbol") or "").upper(), str(c.get("direction") or "").upper())
                for c in record.get("approved_candidates") or [] if isinstance(c, dict)}
        out = []
        for c in previous.get("approved_candidates") or []:
            if not isinstance(c, dict):
                continue
            key = (str(c.get("symbol") or "").upper(), str(c.get("direction") or "").upper())
            if key[0] and key not in kept:
                out.append({"symbol": key[0], "direction": key[1], "tier": c.get("tier"),
                            "valid_until_ts": valid_until})
        return out
    except Exception:
        return []


def _recheck_candidate_fields(recheck_of) -> dict:
    """Issue #298: {recheck_symbol, recheck_direction} of a re-check's `recheck_of` snapshot; {} when it is not a
    re-check or either value is missing (the row then counts as an old row: every candidate of the original is
    refused)."""
    if not isinstance(recheck_of, dict):
        return {}
    sym, dirn = str(recheck_of.get("symbol") or "").strip().upper(), str(recheck_of.get("direction") or "").strip().upper()
    return {"recheck_symbol": sym, "recheck_direction": dirn} if sym and dirn else {}


def _persist(record: dict, base_dir: Optional[str] = None, shadow: bool = True,
             superseded: Optional[list] = None) -> str:
    """Issue #270: writes the record to its session's file (dossier_<parent_conversation_id>.json, when the
    session is known) and, as a full copy, to latest_dossier.json (the newest scan overall). Returns the latter.
    Issue #298: `superseded` (a list, when given) receives _superseded_approvals of the session's previous record;
    the history row then carries `superseded_symbols` ("SYMBOL:DIRECTION", only when non-empty)."""
    base, dossier_file, history_file = _paths(base_dir)
    try:
        snapshots = build_radar_snapshots(record, base)
    except Exception:  # audit metadata only: never blocks recording the verdict
        snapshots = None
    if snapshots is not None:
        record["radar_snapshots"] = snapshots
    session_file = dp.session_dossier_path(base, record.get("parent_conversation_id"))
    dropped = _superseded_approvals(session_file, record, int(record.get("recorded_at_ts") or time.time()))
    if superseded is not None:
        superseded.extend(dropped)
    if session_file:
        atomic_write_json(session_file, record)
    atomic_write_json(dossier_file, record)
    if session_file:
        _prune_session_dossiers(base, keep=session_file)
    prov = record.get("provenance") or {}
    atomic_append_jsonl(history_file, {
        "timestamp_utc": record.get("timestamp_utc"),
        "recorded_at_utc": _fmt_utc(record.get("recorded_at_ts") or time.time()),
        "target_env": record.get("target_env"),
        "schema_version": record.get("schema_version"),
        "evaluator_agent": record.get("evaluator_agent"),
        "conversation_id": record.get("conversation_id"),
        "parent_conversation_id": record.get("parent_conversation_id"),
        "status": record.get("status"),
        "approved_symbols": record.get("approved_symbols", []),
        "provenance_source": prov.get("source"),
        "sha256": prov.get("sha256"),
        "summary": record.get("summary", ""),
        # Issue #267: only on a re-check dossier
        **({"recheck_of": (record.get("recheck_of") or {}).get("sha256"),
            "recheck_within_bounds": bool((record.get("recheck_bounds") or {}).get("within_bounds"))}
           if isinstance(record.get("recheck_of"), dict) else {}),
        # Issue #298: the candidate this re-check consumed, from the verified plan of the brief (never the evaluator's
        # answer, so a NEUTRAL / REJECTED re-check still names it); the per-candidate chain guard of recheck_brief
        **_recheck_candidate_fields(record.get("recheck_of")),
        # Issue #298: optional, only when the previous scan of the session had unexpired approvals this one dropped
        **({"superseded_symbols": [f"{s['symbol']}:{s['direction']}" for s in dropped]} if dropped else {}),
    })
    if shadow:
        _register_shadow()
    return dossier_file


def _recheck_deadline(record: dict) -> Optional[int]:
    """Issue #279: the latest time the age bound still holds (the original plan's evaluated_ts +
    recheck_max_age_seconds), or None when it cannot be read (the verdict is then already out of bounds)."""
    bounds = (record.get("recheck_bounds") or {}).get("bounds") or {}
    try:
        return int((record.get("recheck_of") or {})["evaluated_ts"]) + int(bounds["recheck_max_age_seconds"])
    except (KeyError, TypeError, ValueError):
        return None


def _delta_preview(record: dict, base: str, now_ts: int, cands: list) -> tuple:
    """Issue #298 item 4: (per-candidate line lists, summary line) of the advisory post-trade delta preview
    (utils/delta_fit.py) from the cached book (logs/session_state.json, logs/pending_entries.json) and the brief the
    dossier was evaluated on. Reads files only: no network, no signed request, no write. Never raises: any failure
    is UNKNOWN."""
    try:
        from utils import delta_fit as df
        logs = os.path.join(base, "logs")
        env = str(record.get("target_env") or "").lower()
        if env == "testnet":  # the executor skips Gate 1 in TESTNET
            return ([[f"Delta (est.): {df.TESTNET_NA}"] for _ in cands],
                    f"Delta (est.) of the approved set: {df.TESTNET_NA}")

        def _load(name):
            try:
                with open(os.path.join(logs, name), "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None

        records, reg_err = df.load_registry_records(logs, env)
        book, reason = df.book_from_state(_load("session_state.json"), records, reg_err, env, now_ts)
        brief, raw = _load("primed_brief.json"), (record.get("raw_payload") or {}).get("brief_generated_at_ts")
        rows = []
        for c in cands:
            c = c if isinstance(c, dict) else {}
            notional, why = df.estimate_notional(c, brief, raw)
            rows.append({"symbol": c.get("symbol"), "direction": c.get("direction"), "notional_estimate": notional,
                         "reason": why})
        result = df.evaluate(book, reason, rows)
        return [df.format_candidate(r, env) for r in result["candidates"]], df.format_summary(result, len(rows))
    except Exception as e:
        unknown = f"Delta (est.): UNKNOWN (preview failed: {type(e).__name__})"
        return [[unknown] for _ in cands], f"Delta (est.) of the approved set: UNKNOWN (preview failed: {type(e).__name__})"


def _print_summary(record: dict, dossier_file: str, base: str, now_ts: int,
                   recheck_not_linked: bool = False, superseded: Optional[list] = None,
                   brief_replaced: bool = False) -> None:
    status = record.get("status")
    cands = record.get("approved_candidates") or []
    valid_until = int(record.get("valid_until_ts") or 0)
    remaining = max(0, valid_until - now_ts)
    print(f"✅ EVALUATION DOSSIER RECORDED | env: {str(record.get('target_env', '')).upper()} | status: {status}")
    print(f"   Evaluator: {record.get('evaluator_agent')} | conversation: {record.get('conversation_id')}")
    try:  # issue #298 item 7: the brief the dossier was evaluated on
        brief_at = _fmt_utc((record.get("raw_payload") or {})["brief_generated_at_ts"])
    except Exception:
        brief_at = "unknown"
    print(f"   Brief generated at: {brief_at}")
    print(f"   Evaluated at: {record.get('timestamp_utc')} | Valid until: {_fmt_utc(valid_until)} "
          f"({remaining // 60}m {remaining % 60:02d}s left)")
    if status == "APPROVED" and cands:
        print(f"   Approved ({len(cands)}):")
        delta_lines, delta_summary = _delta_preview(record, base, now_ts, cands)
        for i, c in enumerate(cands):
            lines = delta_lines[i] if i < len(delta_lines) else ["Delta (est.): UNKNOWN (preview failed)"]
            direction = c.get("direction") or "UNKNOWN DIRECTION (will be rejected in PROD)"
            confirm = c.get("requires_user_confirmation")
            if confirm is None:
                flag = "requires_user_confirmation not set -> ask the user before executing"
            elif confirm:
                flag = "REQUIRES USER CONFIRMATION in chat before executing"
            else:
                flag = "fast-track (no confirmation required)"
            extras = []
            for key in ("tier", "leverage", "entry", "stop_loss", "tp1", "tp2"):
                if c.get(key) is not None:
                    extras.append(f"{key}={c.get(key)}")
            if c.get("is_yolo"):
                extras.append("YOLO")
            print(f"     - {c['symbol']} {direction} | {flag}" + (f" | {' '.join(extras)}" if extras else ""))
            for line in lines:  # issue #298 item 4: advisory, read before asking the user
                print(f"       {line}")
        print(f"   {delta_summary}")
    else:
        print("   No trade authorized by this dossier.")
    if record.get("summary"):
        print(f"   Summary: {record['summary']}")
    if superseded:  # issue #298: a newer scan never silently cancels the session's pending approvals
        print("   Superseded approvals of the previous scan of this session (re-check with "
              "`prime_evaluator_brief.py --recheck SYMBOL:DIRECTION` while still inside the window):")
        for s in superseded:
            print(f"     - {s.get('symbol')} {s.get('direction')} | tier={s.get('tier')} | valid until "
                  f"{_fmt_utc(s.get('valid_until_ts') or 0, '%H:%M:%S UTC')}")
    old = record.get("recheck_of")
    if isinstance(old, dict):  # issue #267
        from utils import recheck_bounds as rb
        lines = rb.format_recheck_verdict(record.get("recheck_bounds") or {})
        print(f"   Re-check of dossier sha256 {str(old.get('sha256'))[:16]}… ({old.get('symbol')} {old.get('direction')}"
              f" Tier {old.get('tier')}, entry {old.get('entry')}, SL {old.get('stop_loss')}, TP2 {old.get('tp2')}): "
              f"{lines[0]}")
        for line in lines[1:]:
            print(f"     {line}")
        age_until = _recheck_deadline(record)
        deadline = min(valid_until, age_until) if age_until is not None else valid_until
        left = max(0, deadline - now_ts)
        age_text = _fmt_utc(age_until, '%H:%M:%S UTC') if age_until is not None else "unknown"
        print(f"   Deadline: valid until {_fmt_utc(valid_until, '%H:%M:%S UTC')}, age bound until {age_text}: "
              f"execute before {_fmt_utc(deadline, '%H:%M:%S UTC')} ({left // 60}m {left % 60:02d}s left)")
    elif recheck_not_linked:  # issue #279: never a silent missing verdict
        print("   Re-check: NOT LINKED (no verdict)")
    elif brief_replaced:  # issue #298: not a re-check verdict line, the dossier may be a normal scan
        print("   Brief replaced before recording: if this dossier answers a --recheck, there is no re-check verdict")
    prov = record.get("provenance") or {}
    if prov.get("source") in dp.SUBAGENT_SOURCES:
        runtime = "Claude Code" if prov.get("source") == dp.CLAUDE_SOURCE else "agy"
        print(f"   Provenance: {runtime} subagent transcript step {prov.get('step_index')} "
              f"sha256 {str(prov.get('sha256'))[:16]}…")
    else:
        print(f"   Provenance: {prov.get('source', 'none')} (schema v{record.get('schema_version')}, not accepted in PROD)")
    print(f"   Location: {_rel(dossier_file, base)}")


def record_from_subagent(
    conversation_id: str,
    target_env: Optional[str] = None,
    base_dir: Optional[str] = None,
    now_ts: Optional[int] = None,
    shadow: bool = True,
    verbose: bool = True,
) -> dict:
    """Extracts the dossier emitted by the evaluator subagent from its transcript and records it.
    Accepts an agy conversationId or a Claude Code agentId (auto-detected).
    Raises dp.ProvenanceError (extraction failed) or RecordRefused (policy)."""
    if dp.is_claude_agent_id(conversation_id):
        return record_from_claude_subagent(conversation_id, target_env, base_dir, now_ts, shadow, verbose)
    transcript = dp.find_subagent_transcript(conversation_id)
    extracted = dp.extract_dossier_from_transcript(transcript)
    return _record_extracted(extracted, target_env, base_dir, now_ts, shadow, verbose)


def record_from_claude_subagent(
    agent_id: str,
    target_env: Optional[str] = None,
    base_dir: Optional[str] = None,
    now_ts: Optional[int] = None,
    shadow: bool = True,
    verbose: bool = True,
) -> dict:
    """Records the dossier emitted by a Claude Code 'isolated_market_evaluator' subagent (agentId).
    The transcript's meta.json must report agentType 'isolated_market_evaluator'."""
    transcript = dp.find_claude_subagent_transcript(agent_id)
    extracted = dp.extract_dossier_from_claude_transcript(transcript, dp.EVALUATOR_NAME)
    return _record_extracted(extracted, target_env, base_dir, now_ts, shadow, verbose)


def _record_extracted(
    extracted: dict,
    target_env: Optional[str],
    base_dir: Optional[str],
    now_ts: Optional[int],
    shadow: bool,
    verbose: bool,
) -> dict:
    base = base_dir or BASE_DIR
    env = _resolve_env(target_env, base)
    now_ts = int(now_ts if now_ts is not None else time.time())
    record = dp.build_record_from_extraction(extracted, recorded_at_ts=now_ts)

    parent_id = extracted.get("parent_conversation_id")
    if not parent_id or parent_id == extracted.get("conversation_id"):
        raise RecordRefused(
            "The transcript is not a subagent conversation (no parent sender found). Pass the id of the "
            "'isolated_market_evaluator' subagent (agy conversationId from invoke_subagent, or Claude Code "
            "agentId from the Agent tool), not the main agent's own conversation."
        )

    evaluated_at = int(record.get("timestamp_ts") or 0)
    if evaluated_at <= 0:
        raise RecordRefused("Could not determine when the evaluator emitted the dossier (missing transcript timestamp).")
    if evaluated_at > now_ts + dp.CLOCK_DRIFT_TOLERANCE_S:
        raise RecordRefused(f"Dossier timestamp {evaluated_at} is in the future (now {now_ts}). Check the system clock.")
    if now_ts > evaluated_at + TTL_SECONDS:
        age_min = (now_ts - evaluated_at) / 60.0
        raise RecordRefused(
            f"Dossier is already expired: emitted at {_fmt_utc(evaluated_at)} ({age_min:.1f} min ago, "
            f"TTL {TTL_SECONDS // 60} min). Re-run prime_evaluator_brief.py and the evaluator subagent."
        )

    dossier_env = str((extracted.get("dossier") or {}).get("target_env") or "").strip().lower()
    if dossier_env:
        try:
            dossier_env = _resolve_env(dossier_env, base)
        except ValueError:
            raise RecordRefused(f"Dossier target_env '{dossier_env}' is not a valid environment.")
        if dossier_env != env:
            raise RecordRefused(
                f"Dossier was evaluated for {dossier_env.upper()} but this recording targets {env.upper()}. "
                "Re-run the brief and evaluator for the correct environment (or pass --env)."
            )

    problems = dp.check_precondition_checklist(extracted.get("final_text") or "", extracted.get("dossier") or {})
    if problems:
        detail = "; ".join(problems)
        if record.get("status") == "APPROVED" and env == "prod":
            raise RecordRefused(
                f"The evaluator's Precondition Checklist does not match its dossier: {detail}. "
                "Re-run the evaluator subagent."
            )
        # TESTNET APPROVED, or REJECTED/NEUTRAL in any env: record with a warning. A refused REJECTED/NEUTRAL
        # record would leave an older APPROVED latest_dossier.json live (and skip shadow enrollment); it
        # authorizes nothing, so recording it is the fail-closed choice.
        print(f"⚠️ CHECKLIST WARNING ({env.upper()}, status {record.get('status')}): {detail}", file=sys.stderr)

    record["target_env"] = env
    recheck_warning = attach_recheck(record, base, now_ts)
    if recheck_warning:
        print(f"⚠️ RE-CHECK NOT LINKED: {recheck_warning}", file=sys.stderr)
    replaced_warning = None if recheck_warning else brief_replaced_warning(record, base)
    if replaced_warning:
        print(f"⚠️ BRIEF REPLACED: {replaced_warning}", file=sys.stderr)
    superseded: list = []
    dossier_file = _persist(record, base, shadow=shadow, superseded=superseded)
    if verbose:
        _print_summary(record, dossier_file, base, now_ts, recheck_not_linked=bool(recheck_warning),
                       superseded=superseded, brief_replaced=bool(replaced_warning))
    return record


def record_evaluation_dossier(
    approved_candidates: list,
    evaluator_agent: str = "isolated_market_evaluator",
    conversation_id: str = None,
    summary: str = "",
    status: str = "APPROVED",
    raw_payload: dict = None,
    target_env: Optional[str] = None,
    base_dir: Optional[str] = None,
    shadow: bool = True,
) -> dict:
    """Legacy manual recording (TESTNET only). Refused in PROD with RecordRefused: PROD dossiers
    must come from the evaluator subagent transcript (--from-subagent)."""
    base = base_dir or BASE_DIR
    env = _resolve_env(target_env, base)
    if env == "prod":
        raise RecordRefused(
            "Manual dossiers are not accepted in PROD. Invoke the 'isolated_market_evaluator' subagent and run "
            "`python3 scripts/record_evaluation.py --from-subagent <conversationId>` (agy) or "
            "`--from-claude-subagent <agentId>` (Claude Code)."
        )

    now_ts = int(time.time())
    status_norm, pending = dp.normalize_status(status)
    candidates = dp.normalize_candidates({"approved_candidates": approved_candidates or []}, pending)
    if status_norm != "APPROVED":
        candidates = []
    record = {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "timestamp_utc": _fmt_utc(now_ts),
        "timestamp_ts": now_ts,
        "valid_until_ts": now_ts + TTL_SECONDS,
        "recorded_at_ts": now_ts,
        "target_env": env,
        "evaluator_agent": evaluator_agent,
        "conversation_id": conversation_id or os.environ.get("CONVERSATION_ID", "manual_testnet"),
        "status": status_norm,
        "approved_symbols": [c["symbol"] for c in candidates],
        "approved_candidates": candidates,
        "summary": str(summary or "").strip(),
        "provenance": {"source": "manual_testnet"},
        "raw_payload": raw_payload or {},
    }
    superseded: list = []
    dossier_file = _persist(record, base, shadow=shadow, superseded=superseded)
    _print_summary(record, dossier_file, base, now_ts, superseded=superseded)
    return record


def _legacy_from_payload(data: dict, args, env: str) -> dict:
    candidates = data.get("approved_candidates") or data.get("top_candidates") or []
    return record_evaluation_dossier(
        approved_candidates=candidates,
        evaluator_agent=data.get("evaluator_agent", args.evaluator),
        summary=data.get("summary", args.summary),
        status=data.get("status", args.status),
        raw_payload=data,
        target_env=env,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluator subagent dossier recorder")
    parser.add_argument("--from-subagent", metavar="CONVERSATION_ID",
                        help="Record the <dossier_json> emitted by the evaluator subagent (agy conversationId or unique "
                             "prefix; a Claude Code agentId is auto-detected)")
    parser.add_argument("--from-claude-subagent", metavar="AGENT_ID",
                        help="Record the <dossier_json> emitted by the Claude Code 'isolated_market_evaluator' "
                             "subagent (agentId reported by the Agent tool)")
    parser.add_argument("--env", default=None, help="Target environment (prod|testnet). Defaults to the project resolver.")
    # Legacy manual paths (TESTNET only)
    parser.add_argument("--symbols", type=str, help="[TESTNET only] Comma-separated approved symbols")
    parser.add_argument("--directions", type=str, help="[TESTNET only] Directions matching --symbols (LONG,SHORT)")
    parser.add_argument("--evaluator", type=str, default=dp.EVALUATOR_NAME, help="[TESTNET only] Evaluator name")
    parser.add_argument("--summary", type=str, default="Manual TESTNET evaluation", help="[TESTNET only] Summary")
    parser.add_argument("--status", type=str, default="APPROVED", choices=list(dp.VALID_STATUSES), help="[TESTNET only] Verdict")
    parser.add_argument("--json-file", type=str, help="[TESTNET only] Load a dossier from a JSON file")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        env = _resolve_env(args.env)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        if args.from_subagent and args.from_claude_subagent:
            print("❌ Use either --from-subagent or --from-claude-subagent, not both.", file=sys.stderr)
            return EXIT_REFUSED
        if args.from_claude_subagent:
            record_from_claude_subagent(args.from_claude_subagent, target_env=env)
            return EXIT_OK
        if args.from_subagent:
            record_from_subagent(args.from_subagent, target_env=env)
            return EXIT_OK

        stdin_piped = not args.symbols and not args.json_file and not sys.stdin.isatty()
        if not (args.symbols or args.json_file or stdin_piped):
            parser.print_help()
            return EXIT_ERROR

        if env == "prod":
            raise RecordRefused(
                "Manual dossier recording (--symbols / --json-file / stdin) is disabled in PROD. "
                "Invoke the 'isolated_market_evaluator' subagent (agy invoke_subagent / Claude Code Agent tool) and "
                "record its verdict with `python3 scripts/record_evaluation.py --from-subagent <conversationId>` "
                "or `--from-claude-subagent <agentId>`."
            )

        if args.json_file:
            if not os.path.exists(args.json_file):
                print(f"❌ JSON file not found: {args.json_file}", file=sys.stderr)
                return EXIT_ERROR
            with open(args.json_file, "r", encoding="utf-8") as f:
                _legacy_from_payload(json.load(f), args, env)
        elif args.symbols:
            syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            dirs = [d.strip().upper() for d in (args.directions or "").split(",") if d.strip()]
            candidates = [{"symbol": sym, "direction": dirs[i] if i < len(dirs) else "LONG"} for i, sym in enumerate(syms)]
            record_evaluation_dossier(
                approved_candidates=candidates,
                evaluator_agent=args.evaluator,
                summary=args.summary,
                status=args.status,
                target_env=env,
            )
        else:
            raw_input = sys.stdin.read()
            match = re.search(r"<dossier_json>([\s\S]*?)</dossier_json>", raw_input)
            data = json.loads(match.group(1).strip() if match else raw_input.strip())
            if not isinstance(data, dict):
                raise ValueError("stdin dossier is not a JSON object")
            _legacy_from_payload(data, args, env)
        return EXIT_OK
    except RecordRefused as e:
        print(f"⛔ REFUSED: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except dp.ProvenanceError as e:
        print(f"❌ PROVENANCE ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    except (ValueError, json.JSONDecodeError) as e:
        print(f"❌ Invalid dossier input: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
