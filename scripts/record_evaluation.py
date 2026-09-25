#!/usr/bin/env python3
"""
record_evaluation.py - Atomic Registration of Subagent Evaluation Dossier.
Atomic persistence of pre-execution evaluation authorization tokens.

Saves the verdict emitted by the 'isolated_market_evaluator' subagent into logs/evaluations/latest_dossier.json.
This file serves as a mechanical authorization token that the pre_trade_guard.py hook
requires prior to permitting any order execution on Binance.

Usage:
  python3 scripts/record_evaluation.py --symbols TIAUSDT,SAGAUSDT --directions LONG,SHORT --evaluator isolated_market_evaluator --summary "Delta-neutral basket approved"
  python3 scripts/record_evaluation.py --json-file path/to/dossier.json
"""

import os
import sys
import json
import time
import datetime
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.atomic_writer import atomic_write_json, atomic_append_jsonl

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(BASE_DIR, "logs", "evaluations")
DOSSIER_FILE = os.path.join(EVAL_DIR, "latest_dossier.json")
EVAL_HISTORY_FILE = os.path.join(EVAL_DIR, "evaluations_history.jsonl")

TTL_SECONDS = 1200 # 20 minutes maximum validity window before expiration

def record_evaluation_dossier(
    approved_candidates: list,
    evaluator_agent: str = "isolated_market_evaluator",
    conversation_id: str = None,
    summary: str = "",
    status: str = "APPROVED",
    raw_payload: dict = None
) -> dict:
    os.makedirs(EVAL_DIR, exist_ok=True)
    now_ts = int(time.time())
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    dossier = {
        "timestamp_utc": now_utc,
        "timestamp_ts": now_ts,
        "valid_until_ts": now_ts + TTL_SECONDS,
        "evaluator_agent": evaluator_agent,
        "conversation_id": conversation_id or os.environ.get("CONVERSATION_ID", "clean_room_context"),
        "status": status.upper(),
        "approved_symbols": [c.get("symbol", "").upper() for c in approved_candidates if isinstance(c, dict)],
        "approved_candidates": approved_candidates,
        "summary": summary.strip(),
        "raw_payload": raw_payload or {}
    }

    # Atomic write to latest_dossier.json
    atomic_write_json(DOSSIER_FILE, dossier)

    # Append-only persistent audit history
    history_record = {
        "timestamp_utc": now_utc,
        "evaluator_agent": evaluator_agent,
        "status": status.upper(),
        "approved_symbols": dossier["approved_symbols"],
        "summary": summary.strip()
    }
    atomic_append_jsonl(EVAL_HISTORY_FILE, history_record)

    print(f"✅ EVALUATION DOSSIER RECORDED: {len(dossier['approved_symbols'])} asset(s) approved.")
    print(f"   Symbols: {', '.join(dossier['approved_symbols'])} | Valid until: {datetime.datetime.fromtimestamp(dossier['valid_until_ts'], datetime.timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"   Location: {DOSSIER_FILE}")
    return dossier

def main():
    parser = argparse.ArgumentParser(description="Subagent Evaluation Dossier Recorder")
    parser.add_argument("--symbols", type=str, help="Comma-separated list of approved symbols (e.g. TIAUSDT,SAGAUSDT)")
    parser.add_argument("--directions", type=str, help="Corresponding directions (e.g. LONG,SHORT)")
    parser.add_argument("--evaluator", type=str, default="isolated_market_evaluator", help="Evaluator subagent name")
    parser.add_argument("--summary", type=str, default="Quantitative evaluation approved", help="Summary or thesis")
    parser.add_argument("--status", type=str, default="APPROVED", choices=["APPROVED", "REJECTED", "NEUTRAL"], help="Verdict")
    parser.add_argument("--json-file", type=str, help="Load complete dossier from a JSON file")
    args = parser.parse_args()

    if args.json_file and os.path.exists(args.json_file):
        with open(args.json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        candidates = data.get("approved_candidates", [])
        if not candidates and "top_candidates" in data:
            candidates = data["top_candidates"]
        record_evaluation_dossier(
            approved_candidates=candidates,
            evaluator_agent=data.get("evaluator_agent", args.evaluator),
            summary=data.get("summary", args.summary),
            status=data.get("status", args.status),
            raw_payload=data
        )
    elif args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        dirs = [d.strip().upper() for d in (args.directions or "").split(",") if d.strip()]
        candidates = []
        for i, sym in enumerate(syms):
            direction = dirs[i] if i < len(dirs) else "LONG"
            candidates.append({"symbol": sym, "direction": direction})
        record_evaluation_dossier(
            approved_candidates=candidates,
            evaluator_agent=args.evaluator,
            summary=args.summary,
            status=args.status
        )
    else:
        # If no arguments provided, try reading JSON from stdin
        if not sys.stdin.isatty():
            try:
                import re
                raw_input = sys.stdin.read()
                match = re.search(r"<dossier_json>([\s\S]*?)</dossier_json>", raw_input)
                json_str = match.group(1).strip() if match else raw_input.strip()
                data = json.loads(json_str)
                candidates = data.get("approved_candidates", data.get("top_candidates", []))
                record_evaluation_dossier(
                    approved_candidates=candidates,
                    evaluator_agent=data.get("evaluator_agent", args.evaluator),
                    summary=data.get("summary", args.summary),
                    status=data.get("status", args.status),
                    raw_payload=data
                )
                return
            except Exception as e:
                print(f"Error parsing JSON from stdin: {e}", file=sys.stderr)
        parser.print_help()

if __name__ == "__main__":
    main()
