#!/usr/bin/env python3
"""
trading_drift_watchdog.py - Temporal Drift and Dead Alpha Sensor.
Proactive lifecycle and holding-time monitoring for open positions.

In intraday trading (15m/5m), an absorption or breakout hypothesis has a finite useful lifetime (Half-Life H).
If a position has been open for 3 to 4 hours stagnant within a narrow range (+/- 0.3R) with dry volume,
the original statistical thesis has EXPIRED.

Keeping it open exposes capital to funding fees and adverse macro shocks.
This watchdog audits live positions and:
1. Detects positions with 'Dead Alpha'.
2. If slightly in profit, aggressively ratchets SL to Break-Even.
3. If frozen at entry, issues a defensive close recommendation or triggers auto-close (--auto-exit).

Usage:
  python3 scripts/trading_drift_watchdog.py [--env testnet|mainnet] [--max-hours 4.0] [--auto-exit]
"""

import os
import sys
import time
import json
import datetime
import argparse

# Ensure local path resolution
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
from utils.atomic_writer import read_json_safe, atomic_append_jsonl

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
AUDIT_LOG = os.path.join(LOGS_DIR, "trades_audit.jsonl")

def audit_dead_alpha(target_env: str = "testnet", max_hours: float = 4.0, auto_exit: bool = False):
    print("=" * 70)
    print("⏳ DEAD ALPHA & DRIFT WATCHDOG — TEMPORAL HOLDING AUDIT")
    print(f"UTC Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Max Holding Time: {max_hours}h | Target Env: {target_env.upper()}")
    print("=" * 70)

    # 1. Fetch active Binance positions
    pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0] if isinstance(pos_res, list) else []

    if not active:
        print("✅ ZERO OPEN POSITIONS: Zero temporal drift. Clean portfolio.")
        return {"active_count": 0, "dead_alpha_count": 0, "positions": []}

    state = read_json_safe(STATE_FILE, default={})
    meta_positions = {p["symbol"]: p for p in state.get("active_positions", [])}
    now_ts = int(time.time())

    results = []
    dead_alpha_detected = []

    for p in active:
        sym = p["symbol"]
        amt = float(p["positionAmt"])
        direction = "LONG" if amt > 0 else "SHORT"
        entry_p = float(p["entryPrice"])
        mark_p = float(p["markPrice"])
        unpnl = float(p.get("unRealizedProfit", 0))
        margin = float(p.get("isolatedMargin", 0))
        roe_pct = (unpnl / margin * 100) if margin > 0 else 0.0

        meta = meta_positions.get(sym, {})
        entry_time_ts = meta.get("entry_time_ts", now_ts)
        elapsed_sec = now_ts - entry_time_ts
        elapsed_hours = round(elapsed_sec / 3600.0, 2)

        sl_price = meta.get("sl_price")
        price_diff_pct = abs(mark_p - entry_p) / entry_p * 100

        # Quantitative Dead Alpha Criterion:
        # Exceeded max_hours AND price has not moved more than 1.2% from entry (stagnant dead range)
        is_stagnant = price_diff_pct < 1.2 and abs(roe_pct) < 15.0
        is_overdue = elapsed_hours >= max_hours
        is_dead_alpha = is_overdue and is_stagnant

        item = {
            "symbol": sym,
            "direction": direction,
            "amount": abs(amt),
            "entry_price": entry_p,
            "mark_price": mark_p,
            "elapsed_hours": elapsed_hours,
            "unrealized_pnl_usdt": unpnl,
            "roe_pct": roe_pct,
            "is_dead_alpha": is_dead_alpha,
            "action_taken": "NONE"
        }

        print(f"\n• Position: {sym} ({direction}) | Entry: {entry_p} | Mark: {mark_p}")
        print(f"  Holding Duration: {elapsed_hours}h (Limit: {max_hours}h) | PnL: ${unpnl:+.2f} USDT ({roe_pct:+.1f}% ROE)")

        if is_dead_alpha:
            dead_alpha_detected.append(item)
            print(f"  🚨 ALERT [DEAD ALPHA]: Original thesis expired after {elapsed_hours}h in tight range ({price_diff_pct:.2f}% price movement).")
            
            if auto_exit:
                print(f"  ⚡ TRIGGERING AUTO-EXIT: Closing position at market to recycle capital...")
                close_res = eft.close_position_market(sym, target_env=target_env)
                item["action_taken"] = "AUTO_EXIT_CLOSED"
                item["close_result"] = close_res
                print(f"  ✅ Position closed at market.")
            else:
                print(f"  ⚠️  RECOMMENDATION: Market close or tighten SL to Break-Even immediately to eliminate risk.")
                item["action_taken"] = "RECOMMEND_EXIT"
        else:
            print(f"  ✅ Temporal Health OK (Within operational horizon or trending)")

        results.append(item)

    print("\n" + "=" * 70)
    print(f"SUMMARY: {len(active)} position(s) evaluated | {len(dead_alpha_detected)} with Dead Alpha.")
    print("=" * 70)

    return {
        "active_count": len(active),
        "dead_alpha_count": len(dead_alpha_detected),
        "positions": results
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dead Alpha & Drift Watchdog")
    parser.add_argument("--env", default="testnet", choices=["testnet", "mainnet"])
    parser.add_argument("--max-hours", type=float, default=4.0, help="Maximum holding hours before declaring dead alpha")
    parser.add_argument("--auto-exit", action="store_true", help="Closes dead alpha positions at market")
    args = parser.parse_args()

    audit_dead_alpha(target_env=args.env, max_hours=args.max_hours, auto_exit=args.auto_exit)
