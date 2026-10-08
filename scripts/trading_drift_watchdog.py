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

Holding time and the dead-alpha verdict come from utils/position_timing.py, shared with the ledger sync and the
position guardian (issue #92): the entry time of each LIVE position is resolved from Binance fills (userTrades),
then the trades audit log, else UNKNOWN. An unknown entry time is reported as such (elapsed_hours None plus a
warning) and is never dead alpha; it is never "now" (0.0h) and never positionRisk updateTime.
Dead alpha = held >= max_hours AND mark within 1.2% of entry AND |ROE| < 15%.
--auto-exit closes only per utils/position_timing.dead_alpha_close_decision (same rule as the guardian's
--close-dead-alpha): dead alpha AND holding time from Binance fills AND the 15m range stall
(dynamic_exit_manager.check_dead_alpha_timeout); otherwise RECOMMEND_EXIT with auto_exit_skipped (printed as "stall
check unavailable" when the stall status is UNKNOWN, else "Coiling/active range"). The watchdog never
files issue reports itself (close_position_market files the P0 of a not-flat close).
An unreadable positionRisk returns {"read_error": str, "active_count": None, ...}: open positions UNKNOWN, never
"zero open positions". A userTrades error is reported per position as entry_time_error (+ entry_time_rate_limited).

Usage:
  python3 scripts/trading_drift_watchdog.py [--env testnet|mainnet] [--max-hours 4.0] [--auto-exit]

Exit status: 1 when positionRisk could not be read (read_error) or any auto-exit close failed (AUTO_EXIT_FAILED; the
message shows stop_source/stop_protected), else 0.
"""

import os
import sys
import time
import datetime
import argparse

# Ensure local path resolution
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
from utils.env_resolver import resolve_env
from utils import position_timing as pt


def _audit_log_path():
    return os.path.join(eft._workspace_dir(), "logs", "trades_audit.jsonl")


def audit_dead_alpha(target_env: str = None, max_hours: float = 4.0, auto_exit: bool = False):
    target_env = resolve_env(target_env)
    print("=" * 70)
    print("⏳ DEAD ALPHA & DRIFT WATCHDOG — TEMPORAL HOLDING AUDIT")
    print(f"UTC Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Max Holding Time: {max_hours}h | Target Env: {target_env.upper()}")
    print("=" * 70)

    # 1. Fetch active Binance positions
    try:
        pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
        detail = None if isinstance(pos_res, list) else str(pos_res)
    except Exception as e:
        pos_res, detail = None, f"{type(e).__name__}: {e}"
    if detail is not None:
        read_error = f"positionRisk unreadable: {detail}"
        print(f"❌ POSITION READ FAILED: {read_error}. Open positions UNKNOWN; temporal audit not performed.")
        return {"read_error": read_error, "active_count": None, "dead_alpha_count": 0, "unknown_holding_count": 0,
                "unknown_holding_symbols": [], "positions": []}
    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0]

    if not active:
        print("✅ ZERO OPEN POSITIONS: Zero temporal drift. Clean portfolio.")
        return {"active_count": 0, "dead_alpha_count": 0, "unknown_holding_count": 0, "unknown_holding_symbols": [],
                "positions": []}

    now_ts = int(time.time())
    audit_path = _audit_log_path()

    results = []
    dead_alpha_detected = []
    unknown_holding = []

    for p in active:
        sym = p["symbol"]
        amt = float(p["positionAmt"])
        direction = "LONG" if amt > 0 else "SHORT"
        entry_p = float(p["entryPrice"])
        mark_p = float(p["markPrice"])
        unpnl = float(p.get("unRealizedProfit", 0))
        roe_pct = pt.position_roe_pct(p)

        entry_time_ts, entry_source, entry_diag = pt.resolve_entry_time_detailed(
            sym, direction, p["positionAmt"], target_env, entry_price=entry_p, fetch=eft.send_signed_request,
            audit_path=audit_path)
        elapsed_hours = pt.holding_hours(entry_time_ts, now_ts)
        verdict = pt.assess_dead_alpha(elapsed_hours=elapsed_hours, entry_price=entry_p, mark_price=mark_p,
                                       roe_pct=roe_pct, max_hours=max_hours)
        is_dead_alpha = verdict["is_dead_alpha"]
        price_diff_pct = verdict["price_diff_pct"]

        item = {
            "symbol": sym,
            "direction": direction,
            "amount": abs(amt),
            "entry_price": entry_p,
            "mark_price": mark_p,
            "elapsed_hours": elapsed_hours,
            "entry_time_ts": entry_time_ts,
            "entry_time_source": entry_source,
            "holding_verdict": verdict["verdict"],
            "unrealized_pnl_usdt": unpnl,
            "roe_pct": roe_pct,
            "is_dead_alpha": is_dead_alpha,
            "action_taken": "NONE"
        }
        if entry_diag["user_trades_error"]:
            item["entry_time_error"] = entry_diag["user_trades_error"]
            if entry_diag["rate_limited"]:
                item["entry_time_rate_limited"] = True

        print(f"\n• Position: {sym} ({direction}) | Entry: {entry_p} | Mark: {mark_p}")
        if elapsed_hours is None:
            item["warning"] = (f"Holding time UNKNOWN for {sym}: no reconcilable Binance fill and no matching "
                               f"trades_audit record. Temporal audit not possible for this position. Review "
                               f"manually: python3 scripts/execute_futures_trade.py --positions --json.")
            unknown_holding.append(sym)
            print(f"  Holding Duration: UNKNOWN (Limit: {max_hours}h) | PnL: ${unpnl:+.2f} USDT ({roe_pct:+.1f}% ROE)")
            print(f"  ⚠️  WARNING: {item['warning']}")
        else:
            print(f"  Holding Duration: {elapsed_hours}h (Limit: {max_hours}h, source: {entry_source}) | "
                  f"PnL: ${unpnl:+.2f} USDT ({roe_pct:+.1f}% ROE)")

        if is_dead_alpha:
            dead_alpha_detected.append(item)
            print(f"  🚨 ALERT [DEAD ALPHA]: Original thesis expired after {elapsed_hours}h in tight range ({price_diff_pct:.2f}% price movement).")
            
            if auto_exit and not pt.is_autonomous_close_allowed(entry_source):
                # Holding time not from Binance fills (trades_audit): report only, never an autonomous close.
                print(f"  ⚠️  AUTO-EXIT SKIPPED: holding time from {entry_source}, not Binance fills. Review manually.")
                item["action_taken"] = "RECOMMEND_EXIT"
                item["auto_exit_skipped"] = f"entry time source {entry_source}: report only"
            elif auto_exit:
                # Same close rule as the guardian (issue #138): the 15m range stall is required too.
                try:
                    import dynamic_exit_manager as dem
                    status = (dem.check_dead_alpha_timeout(sym, target_env=target_env) or {}).get("status") or "UNKNOWN"
                except Exception:
                    status = "UNKNOWN"
                item["stall_status"] = status
                allowed, reason = pt.dead_alpha_close_decision(verdict["verdict"], entry_source, status)
                if not allowed:
                    item["action_taken"] = "RECOMMEND_EXIT"
                    item["auto_exit_skipped"] = reason
                    context = "stall check unavailable" if status == "UNKNOWN" else "Coiling/active range"
                    print(f"  ⚠️  AUTO-EXIT SKIPPED: {reason}. {context}; review manually.")
                else:
                    print(f"  ⚡ TRIGGERING AUTO-EXIT: Closing position at market to recycle capital...")
                    close_res = eft.close_position_market(sym, target_env=target_env)
                    item["close_result"] = close_res
                    if close_res.get("success"):
                        item["action_taken"] = "AUTO_EXIT_CLOSED"
                        print(f"  ✅ Position closed at market.")
                    else:
                        item["action_taken"] = "AUTO_EXIT_FAILED"
                        protected = close_res.get("stop_protected")
                        print(f"  ❌ AUTO-EXIT FAILED: {close_res.get('error') or 'close not confirmed (no error text)'} | "
                              f"stop_source={close_res.get('stop_source')} stop_protected={protected}"
                              f"{'' if protected is True else ' (treat as UNPROTECTED)'}")
            else:
                print(f"  ⚠️  RECOMMENDATION: Market close or tighten SL to Break-Even immediately to eliminate risk.")
                item["action_taken"] = "RECOMMEND_EXIT"
        elif elapsed_hours is not None:
            print(f"  ✅ Temporal Health OK (Within operational horizon or trending)")

        results.append(item)

    print("\n" + "=" * 70)
    print(f"SUMMARY: {len(active)} position(s) evaluated | {len(dead_alpha_detected)} with Dead Alpha"
          f" | {len(unknown_holding)} with UNKNOWN holding time.")
    print("=" * 70)

    return {
        "active_count": len(active),
        "dead_alpha_count": len(dead_alpha_detected),
        "unknown_holding_count": len(unknown_holding),
        "unknown_holding_symbols": unknown_holding,
        "positions": results
    }

def main(argv=None) -> int:
    """CLI entry point. Exit status 1 when positionRisk could not be read (read_error) or any auto-exit close failed
    (AUTO_EXIT_FAILED), else 0."""
    default_env = resolve_env()
    parser = argparse.ArgumentParser(description="Dead Alpha & Drift Watchdog")
    parser.add_argument("--env", default=default_env, help="Target execution environment (prod/testnet)")
    parser.add_argument("--max-hours", type=float, default=4.0, help="Maximum holding hours before declaring dead alpha")
    parser.add_argument("--auto-exit", action="store_true", help="Closes dead alpha positions at market")
    args = parser.parse_args(argv)

    report = audit_dead_alpha(target_env=args.env, max_hours=args.max_hours, auto_exit=args.auto_exit)
    failed = [p for p in report.get("positions", []) if p.get("action_taken") == "AUTO_EXIT_FAILED"]
    return 1 if failed or report.get("read_error") else 0


if __name__ == "__main__":
    sys.exit(main())
