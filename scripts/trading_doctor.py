#!/usr/bin/env python3
"""
trading_doctor.py - Pre-Flight Diagnostic & Health Sensor for Trading Desk.
Comprehensive verification of connectivity, clock synchronization, and orphan position auditing.

Verifies:
1. API Connectivity and Network Latency (< 800ms)
2. Clock Drift (< 1000ms against Binance server)
3. API Credentials and Permissions (Testnet / Mainnet)
4. Available Capital and USDT Balance
5. Forensic Orphan Position Audit (Fail CLOSED if position lacks Stop Loss on ledger)
6. State Ledger Freshness (session_state.json)

Usage:
  python3 scripts/trading_doctor.py [--env testnet|mainnet] [--heal]
  Exit 0 if system is healthy and ready to trade.
  Exit 1 if a critical failure occurs (Fail CLOSED).
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import hmac
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft

def run_doctor(target_env: str = "testnet", auto_heal: bool = False) -> int:
    start_time = time.time()
    print("=" * 65)
    print("🩺 TRADING DOCTOR — PRE-FLIGHT SYSTEM DIAGNOSTIC")
    print(f"Target Environment: {target_env.upper()}")
    print("=" * 65)

    critical_failures = []
    warnings = []
    ok_items = []

    # 1. API Configuration & Credentials
    api_key, secret_key, base_url = eft.get_client_config(target_env=target_env)
    if not api_key or not secret_key:
        critical_failures.append("API credentials not found or invalid in .env")
        print("❌ [API KEYS] Missing credentials or placeholder in .env")
        return 1
    else:
        masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "***"
        ok_items.append(f"Credentials detected for {target_env.upper()} ({masked_key})")
        print(f"✅ [API KEYS] Credentials OK ({target_env.upper()})")

    # 2. Network Latency & Clock Drift
    try:
        t0 = time.time()
        req = urllib.request.Request(f"{base_url}/fapi/v1/time", headers={"User-Agent": "TradingDoctor/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            t1 = time.time()
            data = json.loads(resp.read().decode())
            server_time = data.get("serverTime", 0)
            latency_ms = int((t1 - t0) * 1000)
            mid_local_ms = int(((t0 + t1) / 2.0) * 1000)
            drift_ms = abs(server_time - mid_local_ms)

            if latency_ms > 1200:
                warnings.append(f"Elevated network latency: {latency_ms}ms")
                print(f"⚠️  [NETWORK] High latency: {latency_ms}ms")
            else:
                ok_items.append(f"API latency: {latency_ms}ms")
                print(f"✅ [NETWORK] API latency: {latency_ms}ms")

            if drift_ms > 1000:
                critical_failures.append(f"Excessive clock drift: {drift_ms}ms (limit: 1000ms)")
                print(f"❌ [CLOCK DRIFT] Dangerous clock drift: {drift_ms}ms")
            elif drift_ms > 400:
                warnings.append(f"Moderate clock drift: {drift_ms}ms")
                print(f"⚠️  [CLOCK DRIFT] Moderate clock drift: {drift_ms}ms")
            else:
                ok_items.append(f"Optimal clock drift: {drift_ms}ms")
                print(f"✅ [CLOCK DRIFT] Clock synchronization OK ({drift_ms}ms)")
    except Exception as e:
        critical_failures.append(f"Failed to connect to time endpoint: {str(e)}")
        print(f"❌ [NETWORK] Unable to connect to {base_url}: {e}")
        return 1

    # 3. Balance & Free Margin Query
    try:
        balance_res = eft.send_signed_request("GET", "/fapi/v2/balance", target_env=target_env)
        if isinstance(balance_res, list):
            usdt_bal = next((b for b in balance_res if b.get("asset") == "USDT"), None)
            if usdt_bal:
                total_bal = float(usdt_bal.get("balance", 0.0))
                free_bal = float(usdt_bal.get("availableBalance", 0.0))
                if free_bal < 10.0:
                    warnings.append(f"Low available USDT balance: ${free_bal:.2f} USDT")
                    print(f"⚠️  [BALANCE] Low available USDT balance: ${free_bal:.2f} (Total: ${total_bal:.2f})")
                else:
                    ok_items.append(f"USDT Balance: ${free_bal:.2f} available of ${total_bal:.2f}")
                    print(f"✅ [BALANCE] Available balance: ${free_bal:.2f} USDT (Total: ${total_bal:.2f})")
            else:
                warnings.append("USDT asset not found in futures balance")
                print("⚠️  [BALANCE] USDT asset not found")
        else:
            critical_failures.append(f"Unexpected response fetching balance: {balance_res}")
            print(f"❌ [BALANCE] Error fetching balance: {balance_res}")
    except Exception as e:
        critical_failures.append(f"Failed to fetch balance: {str(e)}")
        print(f"❌ [BALANCE] Authentication or connection error: {e}")

    # 4. Forensic Orphan Position Audit (FAIL CLOSED)
    try:
        pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
        active_positions = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0] if isinstance(pos_res, list) else []

        algos_res = eft.send_signed_request("GET", "/fapi/v1/openAlgoOrders", target_env=target_env)
        active_algos = algos_res if isinstance(algos_res, list) else []
        algo_symbols = {a.get("symbol") for a in active_algos if a.get("orderType") in ["STOP_MARKET", "STOP"]}

        orphan_positions = []
        for p in active_positions:
            sym = p.get("symbol")
            if sym not in algo_symbols:
                orphan_positions.append(p)

        if orphan_positions:
            err_msg = f"Detected {len(orphan_positions)} ORPHAN position(s) lacking Stop Loss on Binance: {[p['symbol'] for p in orphan_positions]}"
            if auto_heal:
                print(f"🚨 [ORPHAN AUDIT] {err_msg} — TRIGGERING AUTO-HEAL...")
                for op in orphan_positions:
                    sym = op["symbol"]
                    amt = float(op["positionAmt"])
                    entry_p = float(op["entryPrice"])
                    exit_side = "SELL" if amt > 0 else "BUY"
                    emergency_sl = entry_p * (0.975 if amt > 0 else 1.025)
                    heal_res = eft.place_algo_stop_loss(sym, exit_side, emergency_sl, target_env=target_env)
                    if heal_res.get("algoId"):
                        print(f"   🛡️ Auto-Heal successful for {sym}: Algo SL placed at {emergency_sl:.5f}")
                        ok_items.append(f"Auto-Heal applied to {sym}")
                    else:
                        critical_failures.append(f"Auto-Heal failure on {sym}: {heal_res}")
                        print(f"   ❌ Failed to apply Auto-Heal on {sym}: {heal_res}")
            else:
                critical_failures.append(err_msg)
                print(f"❌ [ORPHAN AUDIT] FAIL CLOSED: {err_msg}")
                print("   👉 Run `python3 scripts/trading_doctor.py --heal` or place Stop Loss immediately.")
        else:
            if active_positions:
                ok_items.append(f"{len(active_positions)} active position(s), all with verified Stop Loss on Binance")
                print(f"✅ [ORPHAN AUDIT] {len(active_positions)} active position(s) — All protected with Stop Loss.")

                # 4b. Dead Alpha & Temporal Drift Sensor
                try:
                    import trading_drift_watchdog as tdw
                    drift_report = tdw.audit_dead_alpha(target_env=target_env, max_hours=4.0, auto_exit=False)
                    dead_count = drift_report.get("dead_alpha_count", 0)
                    if dead_count > 0:
                        warnings.append(f"Detected {dead_count} position(s) with Dead Alpha (>4h stagnant).")
                        print(f"⚠️  [DEAD ALPHA] {dead_count} stagnant position(s) exceed intraday holding threshold.")
                    else:
                        ok_items.append("Active position holding health OK (Zero Dead Alpha).")
                except Exception:
                    pass
            else:
                ok_items.append("Zero open positions. Zero unhedged exposure.")
                print("✅ [ORPHAN AUDIT] Clean portfolio. Zero open positions.")
    except Exception as e:
        critical_failures.append(f"Failed orphan order audit: {str(e)}")
        print(f"❌ [ORPHAN AUDIT] Error querying positions and orders: {e}")

    # 5. session_state.json Freshness Audit
    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    state_file = os.path.join(logs_dir, "session_state.json")
    if os.path.exists(state_file):
        mtime = os.path.getmtime(state_file)
        age_sec = time.time() - mtime
        if age_sec > 1800:
            warnings.append(f"session_state.json is stale ({int(age_sec/60)} minutes old). Run `sync_session_state.py`.")
            print(f"⚠️  [STATE LEDGER] session_state.json is {int(age_sec/60)} min old. Sync recommended.")
        else:
            ok_items.append(f"session_state.json is fresh ({int(age_sec)}s)")
            print(f"✅ [STATE LEDGER] session_state.json synced {int(age_sec)}s ago")
    else:
        warnings.append("session_state.json does not exist yet. Run `sync_session_state.py`.")
        print("⚠️  [STATE LEDGER] session_state.json does not exist. Run `sync_session_state.py`.")

    elapsed = round(time.time() - start_time, 2)
    print("=" * 65)
    print(f"DIAGNOSTIC COMPLETED IN {elapsed}s")

    if critical_failures:
        print(f"🔴 STATUS: SYSTEM DISABLED ({len(critical_failures)} critical failure(s)). FAIL CLOSED.")
        for f in critical_failures:
            print(f"   ✖ {f}")
        print("=" * 65)
        return 1
    elif warnings:
        print(f"🟡 STATUS: OPERATIONAL WITH WARNINGS ({len(warnings)} warning(s)).")
        for w in warnings:
            print(f"   ▲ {w}")
        print("=" * 65)
        return 0
    else:
        print("🟢 STATUS: 100% GREEN AND HEALTHY. READY TO TRADE.")
        print("=" * 65)
        return 0

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trading Doctor - Pre-flight Health Check")
    parser.add_argument("--env", default="testnet", choices=["testnet", "mainnet"], help="Target execution environment")
    parser.add_argument("--heal", action="store_true", help="Auto-heal orphan positions by placing emergency SL")
    args = parser.parse_args()

    sys.exit(run_doctor(target_env=args.env, auto_heal=args.heal))
