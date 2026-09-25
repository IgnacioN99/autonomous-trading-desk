#!/usr/bin/env python3
"""
trade_telemetry.py - Quantitative Trading Telemetry: 'Shocks' vs. 'Normal Variance' Classification.
Forensic loss attribution and model variance classification.

In quantitative finance, evaluating a model assuming all losses stem from the same
mathematical distribution invalidates the Kelly Criterion. If a -$1.50 loss occurs because
BTC suffered an unexpected -$4,000 flash crash in 15m from a regulatory shock, it is NOT an inherent technical strategy failure.

This module classifies each exit and event into:
- NORMAL_VARIANCE: Loss or gain within normal stochastic asset price behavior.
- MACRO_SHOCK: Extraordinary exogenous event (BTC liquidation cascade, CME listing announcement, exploit).
- EXECUTION_BOUNCE: Friction or execution rejection failure (slippage gap, minNotional).

Usage:
  python3 scripts/trade_telemetry.py record-exit --symbol TRXUSDT --pnl -1.05 --exit-type SL --class NORMAL_VARIANCE --note "Hit SL due to local market weakness"
  python3 scripts/trade_telemetry.py record-shock --type BTC_LIQUIDATION_CASCADE --note "BTC dropped from $86.2k to $83.8k in 45m"
  python3 scripts/trade_telemetry.py summary
"""

import os
import sys
import time
import json
import datetime
import argparse
from typing import Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.atomic_writer import atomic_append_jsonl

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
TELEMETRY_FILE = os.path.join(LOGS_DIR, "trade_telemetry.jsonl")

def record_trade_exit(
    symbol: str,
    direction: str,
    pnl_usdt: float,
    exit_type: str,
    classification: str = "NORMAL_VARIANCE",
    strategy: str = "microstructure_wick_reversion",
    note: str = ""
) -> dict:
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    event = {
        "event_type": "TRADE_EXIT",
        "timestamp_utc": now_utc,
        "timestamp_ts": int(time.time()),
        "symbol": symbol.upper(),
        "direction": direction.upper(),
        "pnl_usdt": round(float(pnl_usdt), 4),
        "exit_type": exit_type.upper(),  # TP1, TP2, SL, DEAD_ALPHA, MANUAL
        "classification": classification.upper(),  # NORMAL_VARIANCE, MACRO_SHOCK, EXECUTION_BOUNCE
        "strategy": strategy,
        "note": note.strip()
    }
    atomic_append_jsonl(TELEMETRY_FILE, event)
    print(f"📡 Telemetry recorded: {symbol} [{exit_type}] -> PnL: ${pnl_usdt:+.2f} ({classification})")
    return event

def record_macro_shock(shock_type: str, impacted_symbols: List[str] = None, note: str = "") -> dict:
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    event = {
        "event_type": "MACRO_SHOCK",
        "timestamp_utc": now_utc,
        "timestamp_ts": int(time.time()),
        "shock_type": shock_type.upper(),
        "impacted_symbols": [s.upper() for s in (impacted_symbols or [])],
        "note": note.strip()
    }
    atomic_append_jsonl(TELEMETRY_FILE, event)
    print(f"🚨 Macro Shock recorded: {shock_type} — {note}")
    return event

def generate_telemetry_summary():
    if not os.path.exists(TELEMETRY_FILE):
        print("ℹ️  No telemetry records found yet.")
        return

    exits = []
    shocks = []
    with open(TELEMETRY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    obj = json.loads(line)
                    if obj.get("event_type") == "TRADE_EXIT":
                        exits.append(obj)
                    elif obj.get("event_type") == "MACRO_SHOCK":
                        shocks.append(obj)
                except Exception:
                    continue

    normal_exits = [e for e in exits if e.get("classification") == "NORMAL_VARIANCE"]
    shock_exits = [e for e in exits if e.get("classification") == "MACRO_SHOCK"]

    print("\n" + "=" * 70)
    print("📊 QUANTITATIVE TELEMETRY REPORT (Shocks vs Normal Variance)")
    print("=" * 70)
    print(f"Total Logged Trades: {len(exits)}")
    print(f"• Normal Variance: {len(normal_exits)} trades | Cumulative PnL: ${sum(e['pnl_usdt'] for e in normal_exits):+.2f} USDT")
    print(f"• Exogenous Macro Shocks: {len(shock_exits)} trades | Impacted PnL: ${sum(e['pnl_usdt'] for e in shock_exits):+.2f} USDT")
    print(f"• Systemic Macro Shocks: {len(shocks)} events")

    if normal_exits:
        normal_wins = [e for e in normal_exits if e["pnl_usdt"] > 0]
        normal_wr = (len(normal_wins) / len(normal_exits)) * 100
        print(f"🏆 Pure Win Rate (Isolated from Shocks): {normal_wr:.1f}%")

    print("=" * 70 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trade Telemetry System")
    subparsers = parser.add_subparsers(dest="cmd")

    # exit
    p_exit = subparsers.add_parser("record-exit")
    p_exit.add_argument("--symbol", required=True)
    p_exit.add_argument("--dir", default="LONG")
    p_exit.add_argument("--pnl", type=float, required=True)
    p_exit.add_argument("--exit-type", default="SL", choices=["TP1", "TP2", "SL", "DEAD_ALPHA", "MANUAL"])
    p_exit.add_argument("--class", dest="classification", default="NORMAL_VARIANCE", choices=["NORMAL_VARIANCE", "MACRO_SHOCK", "EXECUTION_BOUNCE"])
    p_exit.add_argument("--strategy", default="microstructure_wick_reversion")
    p_exit.add_argument("--note", default="")

    # shock
    p_shock = subparsers.add_parser("record-shock")
    p_shock.add_argument("--type", required=True)
    p_shock.add_argument("--symbols", default="")
    p_shock.add_argument("--note", default="")

    # summary
    subparsers.add_parser("summary")

    args = parser.parse_args()

    if args.cmd == "record-exit":
        record_trade_exit(args.symbol, args.dir, args.pnl, args.exit_type, args.classification, args.strategy, args.note)
    elif args.cmd == "record-shock":
        symbols_list = [s.strip() for s in args.symbols.split(",") if s.strip()]
        record_macro_shock(args.type, symbols_list, args.note)
    else:
        generate_telemetry_summary()
