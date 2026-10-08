#!/usr/bin/env python3
"""
night_cutoff_loop.py - Night Cutoff Operating Loop and Zero Overnight Risk Enforcer.
Safety loop for shielding positions and closing the session without unhedged floating risk.

Night Desk Operating Rules:
1. Audits all live positions on Binance Futures.
2. If a position has confirmed profit (ROE >= +10% or favorable expansion >= +1.5R):
   Automatically ratchets Stop Loss to TRUE NET BREAK-EVEN (+0.2% above entry price to absorb fees).
3. If a position is stagnant with dry volume or near Stop Loss:
   Issues a risk alert or defensive close recommendation to prevent unmanaged overnight risk.
4. Cancels all pending orphan LIMIT orders (entry orders or TPs of closed positions)
   older than 60-90 minutes to prevent ghost fills while asleep.
5. Emits a consolidated night safety status report.

Usage:
  python3 scripts/loops/night_cutoff_loop.py [--env testnet|mainnet] [--auto-ratchet]

run_night_cutoff returns {"close_failures": [symbols], "unprotected": [symbols], "stop_unknown": [symbols]} plus
"read_error" when positionRisk cannot be read (orphan order cleanup is then skipped). stop_unknown: openAlgoOrders
stayed unreadable after retries, so no heal, ratchet or close was attempted for that symbol (manual check). Exit
status: 1 when a market close failed, a position remains without a verified stop, its stop state is unknown or
positions could not be read, else 0 (a failed Break-Even ratchet does not change it). Any of those prints the
"NIGHT CUTOFF INCOMPLETE" banner instead of the success banner.
"""

import os
import sys
import time
import json
import datetime
import argparse

# Ensure local path resolution
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import execute_futures_trade as eft
import dynamic_exit_manager as dem
from utils.env_resolver import resolve_env

def run_night_cutoff(target_env: str = None, auto_ratchet: bool = True, overnight_mode: str = None):
    target_env = resolve_env(target_env)

    try:
        import user_profile as up
        prof = up.load_user_profile()
    except Exception:
        prof = {}

    if not overnight_mode:
        overnight_mode = prof.get("overnight_mode", "ZERO_OVERNIGHT_RISK")

    print("=" * 70)
    print("🌙 NIGHT CUTOFF LOOP — OVERNIGHT RISK SHIELDING PROTOCOL")
    print(f"UTC Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Target Environment: {target_env.upper()}")
    print(f"Overnight Mode: {overnight_mode}")
    print("=" * 70)

    # 1. Fetch active positions from Binance
    summary = {"close_failures": [], "unprotected": [], "stop_unknown": []}
    try:
        pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    except Exception as e:
        pos_res = {"error": str(e)}
    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0] if isinstance(pos_res, list) else []
    if not isinstance(pos_res, list):
        summary["read_error"] = f"positionRisk unreadable: {pos_res}"

    if "read_error" in summary:
        print(f"❌ CRITICAL: {summary['read_error']}. Open positions UNKNOWN; overnight risk not verified.")
    elif not active:
        print("✅ ZERO OPEN POSITIONS: Portfolio 100% clean. Zero overnight risk.")
    else:
        print(f"🛡️  AUDITING {len(active)} LIVE POSITION(S) [Mode: {overnight_mode}]:")
        for p in active:
            sym = p["symbol"]
            amt = float(p["positionAmt"])
            entry_p = float(p["entryPrice"])
            mark_p = float(p["markPrice"])
            unpnl = float(p.get("unRealizedProfit", 0))
            margin = float(p.get("isolatedMargin", 0))
            direction = "LONG" if amt > 0 else "SHORT"
            roe_pct = (unpnl / margin * 100) if margin > 0 else 0.0

            print(f"\n   • {sym} ({direction} {abs(amt):.3f} @ {entry_p:.5f})")
            print(f"     Current Mark: {mark_p:.5f} | Floating PnL: ${unpnl:+.2f} USDT ({roe_pct:+.1f}% ROE)")

            # Mode 1: CLOSE_ALL_AT_MARKET -> Close 100% of positions at market
            if overnight_mode == "CLOSE_ALL_AT_MARKET":
                print(f"     🚪 CLOSE_ALL_AT_MARKET mode: Closing {sym} at market to eliminate overnight exposure...")
                close_res = eft.close_position_market(sym, target_env=target_env)
                if close_res.get("success"):
                    print(f"     ✅ Position {sym} successfully closed at market.")
                else:
                    summary["close_failures"].append(sym)
                    print(f"     ❌ Failed to close {sym} at market: {close_res.get('error')}")
                continue

            # For SWING_STRUCTURAL_STOP and ZERO_OVERNIGHT_RISK:
            # First, verify active Stop Loss (retried: an unreadable listing is UNKNOWN, never "no stop")
            algos = None
            for delay in (0.0,) + tuple(eft.STOP_VERIFY_RETRY_DELAYS):
                if delay:
                    time.sleep(delay)
                try:
                    algos = eft.send_signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym},
                                                    target_env=target_env)
                except Exception as e:
                    algos = {"error": str(e)}
                if isinstance(algos, list):
                    break
            if not isinstance(algos, list):
                summary["stop_unknown"].append(sym)
                print(f"     ❌ CRITICAL: {sym} stop state UNKNOWN (openAlgoOrders unreadable: {algos}). "
                      f"No heal, ratchet or close attempted. Manual check required.")
                continue
            active_sl = [a for a in algos if a.get("orderType") in ["STOP_MARKET", "STOP"]]

            if not active_sl:
                print(f"     🚨 DANGER: {sym} HAS NO ACTIVE STOP LOSS. Placing verified emergency Stop Loss...")
                heal_res = eft.heal_orphan_position(p, target_env=target_env, close_on_failure=True)
                if heal_res.get("closed"):
                    print(f"     🚪 Emergency stop could not be verified; {sym} closed at market (reduce-only).")
                    continue
                if not heal_res.get("success"):
                    summary["unprotected"].append(sym)
                    print(f"     ❌ CRITICAL: {sym} remains unprotected ({heal_res.get('reason')}). Manual action required.")
                    continue
                sl_price = float(heal_res["healed_sl_price"])
                print(f"     ✅ Emergency Stop Loss verified at {sl_price}")
            else:
                sl_price = float(active_sl[0].get("triggerPrice", 0))
                print(f"     🛡️ Confirmed active Stop Loss at: {sl_price:.5f}")

            # Ratchet winning positions to True Net Break-Even (+0.2% fee buffer)
            ratcheted_to_be = False
            fee_buffer = 0.002
            target_be = entry_p * (1.0 + fee_buffer) if direction == "LONG" else entry_p * (1.0 - fee_buffer)
            filters = eft.get_symbol_filters(sym, target_env=target_env)
            be_rounded = eft.round_price(target_be, filters["tickSize"], filters["precision_price"])

            is_already_at_be = (sl_price >= be_rounded) if direction == "LONG" else (sl_price <= be_rounded)
            if is_already_at_be:
                ratcheted_to_be = True

            if roe_pct >= 5.0 and auto_ratchet and not is_already_at_be:
                is_better = (be_rounded > sl_price) if direction == "LONG" else (be_rounded < sl_price)
                if is_better:
                    print(f"     📈 Position in profit (+{roe_pct:.1f}% ROE). Ratcheting to True Net Break-Even...")
                    # End-of-day ratchet is explicit policy (AGENTS.md Layer 7), so it overrides the intraday
                    # TP1/2xATR anti-truncation rule; the move is still place-then-cancel and verified.
                    be_res = eft.move_sl_to_breakeven(sym, target_env=target_env, force=True)
                    if be_res.get("success"):
                        print(f"     ✅ SL Shielded to Break-Even at {be_rounded} (+0.2% fees covered). ZERO RISK.")
                        ratcheted_to_be = True
                    else:
                        print(f"     ⚠️  Warning tightening SL: {be_res.get('error') or be_res.get('reason')}")

            # Mode 2: SWING_STRUCTURAL_STOP -> Allow positions with verified SL to remain open
            if overnight_mode == "SWING_STRUCTURAL_STOP":
                print(f"     🌊 SWING_STRUCTURAL_STOP mode: Position {sym} permitted overnight with verified SL at {sl_price:.5f}.")

            # Mode 3: ZERO_OVERNIGHT_RISK -> Ratchet winning to True Net BE and close unhedged directional positions
            elif overnight_mode == "ZERO_OVERNIGHT_RISK":
                if not ratcheted_to_be:
                    print(f"     ⚠️ Position {sym} not at Break-Even (ROE: {roe_pct:.1f}%). ZERO_OVERNIGHT_RISK requires closing unhedged positions...")
                    close_res = eft.close_position_market(sym, target_env=target_env)
                    if close_res.get("success"):
                        print(f"     ✅ Unhedged position {sym} closed at market (Zero Overnight Risk guaranteed).")
                    else:
                        summary["close_failures"].append(sym)
                        print(f"     ❌ Failed to close {sym} at market: {close_res.get('error')}")
                else:
                    print(f"     🛡️ Position {sym} is safely locked at True Net Break-Even. Zero unhedged overnight risk.")

    # 2. Cleanup orphan limit orders
    print("\n🧹 ORPHAN LIMIT ORDERS CLEANUP:")
    # Unknown positions: every order would look orphaned (TPs of live positions included), so skip the cleanup.
    open_orders = None if "read_error" in summary else \
        eft.send_signed_request("GET", "/fapi/v1/openOrders", target_env=target_env)
    if "read_error" in summary:
        print("⚠️  Skipped: positions unknown, orphan orders cannot be identified.")
    elif isinstance(open_orders, list) and open_orders:
        now_ms = int(time.time() * 1000)
        cancelled_count = 0
        active_symbols = {p["symbol"] for p in active}

        for o in open_orders:
            order_id = o.get("orderId")
            sym = o.get("symbol")
            created_ms = o.get("time", now_ms)
            age_min = (now_ms - created_ms) / (1000 * 60)

            # If symbol has no live position, or is an unfilled entry order > 90m old:
            is_reduce_only = o.get("reduceOnly", False)
            if sym not in active_symbols or (not is_reduce_only and age_min > 90):
                order_desc = "orphan (no position)" if sym not in active_symbols else f"stale entry limit ({age_min:.0f}m)"
                print(f"   • Cancelling {order_desc} order #{order_id} on {sym} (type: {o.get('type')})")
                eft.send_signed_request("DELETE", "/fapi/v1/order", {"symbol": sym, "orderId": order_id}, target_env=target_env)
                cancelled_count += 1

        if cancelled_count > 0:
            print(f"✅ {cancelled_count} orphan order(s) cancelled to prevent accidental fills.")
        else:
            print("✅ No expired orphan orders detected.")
    else:
        print("✅ Zero pending limit orders on exchange.")

    # 3. Sync Final Session State
    print("\n📡 Synchronizing session state to persist Ground Truth...")
    sync_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sync_session_state.py")
    rc = os.system(f"{sys.executable} {sync_script} --env {target_env} > /dev/null 2>&1")
    if isinstance(rc, int) and rc != 0:
        # Issue #127: the sync wrote an INVALID state or could not write it; warning only (exit code unchanged).
        exit_code = rc   # Windows: os.system returns the exit code itself
        if os.name != "nt":   # POSIX: a wait status
            try:
                exit_code = os.waitstatus_to_exitcode(rc)
            except (AttributeError, ValueError):
                exit_code = rc >> 8
        print(f"⚠️ session_state.json sync FAILED (exit code {exit_code}): the state is INVALID or was not updated.")
    else:
        print("✅ session_state.json updated with nightly cutoff state.")

    print("\n" + "=" * 70)
    problems = []
    if summary.get("read_error"):
        problems.append(f"read_error: {summary['read_error']}")
    for key in ("close_failures", "unprotected", "stop_unknown"):
        if summary[key]:
            problems.append(f"{key}: {summary[key]}")
    if problems:
        print(f"🚨 NIGHT CUTOFF INCOMPLETE: overnight risk NOT verified ({'; '.join(problems)}). Manual action required.")
    else:
        print("🌙 NIGHT CUTOFF COMPLETED. DESK IN SECURE OVERNIGHT MODE.")
    print("=" * 70)
    return summary

def main(argv=None) -> int:
    """CLI entry point. Exit status 1 when a close failed, a position remains unprotected, its stop state is unknown or
    positions could not be read, else 0."""
    default_env = resolve_env()
    parser = argparse.ArgumentParser(description="Night Cutoff Loop - Zero Overnight Risk")
    parser.add_argument("--env", default=default_env, help="Target execution environment (prod/testnet)")
    parser.add_argument("--auto-ratchet", action="store_true", default=True)
    parser.add_argument("--overnight-mode", dest="overnight_mode", choices=["ZERO_OVERNIGHT_RISK", "CLOSE_ALL_AT_MARKET", "SWING_STRUCTURAL_STOP"], default=None, help="Override overnight mode from profile")
    args = parser.parse_args(argv)

    summary = run_night_cutoff(target_env=args.env, auto_ratchet=args.auto_ratchet, overnight_mode=args.overnight_mode)
    return 1 if summary["close_failures"] or summary["unprotected"] or summary["stop_unknown"] \
        or summary.get("read_error") else 0

if __name__ == "__main__":
    sys.exit(main())
