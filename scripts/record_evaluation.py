#!/usr/bin/env python3
"""
record_evaluation.py - Atomic registration of the evaluator subagent dossier.

Persists the verdict emitted by the 'isolated_market_evaluator' subagent into
logs/evaluations/latest_dossier.json. That file is the authorization token required by
the pre_trade_guard.py hook and execute_futures_trade.py before any order is dispatched.

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
Dossiers typed by hand are NOT accepted in PROD.

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
        shadow_count = shadow_tracker.register_from_evaluation()
        if shadow_count > 0:
            print(f"👻 SHADOW TRACKER: {shadow_count} candidate(s) enrolled into counterfactual efficacy auditing.")
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


def _persist(record: dict, base_dir: Optional[str] = None, shadow: bool = True) -> str:
    base, dossier_file, history_file = _paths(base_dir)
    try:
        snapshots = build_radar_snapshots(record, base)
    except Exception:  # audit metadata only: never blocks recording the verdict
        snapshots = None
    if snapshots is not None:
        record["radar_snapshots"] = snapshots
    atomic_write_json(dossier_file, record)
    prov = record.get("provenance") or {}
    atomic_append_jsonl(history_file, {
        "timestamp_utc": record.get("timestamp_utc"),
        "recorded_at_utc": _fmt_utc(record.get("recorded_at_ts") or time.time()),
        "target_env": record.get("target_env"),
        "schema_version": record.get("schema_version"),
        "evaluator_agent": record.get("evaluator_agent"),
        "conversation_id": record.get("conversation_id"),
        "status": record.get("status"),
        "approved_symbols": record.get("approved_symbols", []),
        "provenance_source": prov.get("source"),
        "sha256": prov.get("sha256"),
        "summary": record.get("summary", ""),
    })
    if shadow:
        _register_shadow()
    return dossier_file


def _print_summary(record: dict, dossier_file: str, base: str, now_ts: int) -> None:
    status = record.get("status")
    cands = record.get("approved_candidates") or []
    valid_until = int(record.get("valid_until_ts") or 0)
    remaining = max(0, valid_until - now_ts)
    print(f"✅ EVALUATION DOSSIER RECORDED | env: {str(record.get('target_env', '')).upper()} | status: {status}")
    print(f"   Evaluator: {record.get('evaluator_agent')} | conversation: {record.get('conversation_id')}")
    print(f"   Evaluated at: {record.get('timestamp_utc')} | Valid until: {_fmt_utc(valid_until, '%H:%M:%S UTC')} "
          f"({remaining // 60}m {remaining % 60:02d}s left)")
    if status == "APPROVED" and cands:
        print(f"   Approved ({len(cands)}):")
        for c in cands:
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
    else:
        print("   No trade authorized by this dossier.")
    if record.get("summary"):
        print(f"   Summary: {record['summary']}")
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

    record["target_env"] = env
    dossier_file = _persist(record, base, shadow=shadow)
    if verbose:
        _print_summary(record, dossier_file, base, now_ts)
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
    dossier_file = _persist(record, base, shadow=shadow)
    _print_summary(record, dossier_file, base, now_ts)
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
