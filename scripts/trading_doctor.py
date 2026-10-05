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

import re
import shlex
import shutil
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
from utils.env_resolver import resolve_env, is_prod_environment, find_workspace_root

HOOK_SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl")
HOOK_HEARTBEAT_MAX_AGE_S = 24 * 3600
HOOK_SELFTEST_ENV = "TRADING_HOOK_SELFTEST"
HOOK_HEARTBEAT_OVERRIDE_ENV = "PRE_TRADE_GUARD_HEARTBEAT_FILE"  # honoured by scripts/hooks/pre_trade_guard.py

# Synthetic direct Binance MCP order: the PreToolUse guard must ALWAYS deny it (choke-point enforcement).
SYNTHETIC_NEW_ORDER_PAYLOAD = {
    "conversationId": "trading-doctor-selftest",
    "stepIdx": 0,
    "modelName": "trading-doctor",
    "toolCall": {
        "name": "call_mcp_tool",
        "args": {
            "ServerName": "binance",
            "ToolName": "futures_usds.newOrder",
            "Arguments": {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"},
        },
    },
}


def _matcher_matches(matcher, tool_name: str) -> bool:
    """agy matcher semantics: '' or '*' match all tools, otherwise a regex over the tool name."""
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(str(matcher), tool_name) is not None
    except re.error:
        return False


def _hook_script_paths(command: str, hooks_dir: str) -> list:
    """Script files referenced by a hook command, resolved like agy does (cwd = directory containing hooks.json)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    paths = []
    for tok in tokens[1:]:
        if tok.startswith("-"):
            continue
        if tok.endswith(HOOK_SCRIPT_SUFFIXES) or "/" in tok:
            expanded = os.path.expanduser(tok)
            paths.append(expanded if os.path.isabs(expanded) else os.path.normpath(os.path.join(hooks_dir, expanded)))
    return paths


def _last_json_object(text: str):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def read_hook_heartbeat(base_dir: str, now_ts: float = None) -> dict:
    """Reads logs/hook_heartbeat.json written by the PreToolUse guard on every live invocation."""
    path = os.path.join(base_dir, "logs", "hook_heartbeat.json")
    if not os.path.exists(path):
        return {"present": False, "path": path}
    try:
        with open(path, "r", encoding="utf-8") as f:
            hb = json.load(f)
        last = float(hb.get("last_seen_ts", 0))
    except Exception as e:
        return {"present": True, "path": path, "error": f"unreadable heartbeat ({e})"}
    now_ts = time.time() if now_ts is None else now_ts
    return {
        "present": True,
        "path": path,
        "age_s": max(0, int(now_ts - last)) if last > 0 else None,
        "hook": hb.get("hook"),
        "mode": hb.get("mode"),
        "tool": hb.get("tool"),
        "decision": hb.get("decision"),
        "last_seen_utc": hb.get("last_seen_utc"),
    }


def check_pretool_hook(base_dir: str = None, run_selftest: bool = True, timeout_cap_s: int = 30) -> dict:
    """
    Real activation check of the agy PreToolUse safety guard (replaces the old string match):
      1. Parses .agents/hooks.json and collects enabled PreToolUse handlers whose matcher covers call_mcp_tool.
      2. Resolves every hook command's script path relative to .agents/ (agy runs hooks with cwd = hooks.json dir)
         and verifies it exists; verifies the interpreter is on PATH.
      3. POSIX: executes each such PreToolUse command via `sh -c` (cwd .agents) with a synthetic Binance
         futures_usds.newOrder payload and requires exit 0 + decision "deny". Windows: skipped with a warning.
      4. Reports logs/hook_heartbeat.json age if present (read BEFORE the self-test).
    Returns {"ok": bool, "critical": [...], "warnings": [...], "info": [...], "heartbeat": {...}}.
    """
    base_dir = base_dir or find_workspace_root()
    hooks_dir = os.path.join(base_dir, ".agents")
    hooks_path = os.path.join(hooks_dir, "hooks.json")
    report = {"ok": False, "critical": [], "warnings": [], "info": [], "heartbeat": read_hook_heartbeat(base_dir)}

    hb = report["heartbeat"]
    if not hb.get("present"):
        report["warnings"].append("No logs/hook_heartbeat.json yet: the guard has not fired in a live agent session.")
    elif hb.get("error"):
        report["warnings"].append(f"Hook heartbeat {hb['error']}.")
    elif hb.get("age_s") is None or hb["age_s"] > HOOK_HEARTBEAT_MAX_AGE_S:
        report["warnings"].append(f"Hook heartbeat is stale (age: {hb.get('age_s')}s, last: {hb.get('last_seen_utc')}).")
    else:
        report["info"].append(
            f"Hook heartbeat {hb['age_s']}s ago (hook={hb.get('hook')}, mode={hb.get('mode')}, tool={hb.get('tool')}, decision={hb.get('decision')})."
        )

    if not os.path.exists(hooks_path):
        report["critical"].append(".agents/hooks.json not found: no PreToolUse safety guard is configured.")
        return report
    try:
        with open(hooks_path, "r", encoding="utf-8") as f:
            hooks_cfg = json.load(f)
        if not isinstance(hooks_cfg, dict):
            raise ValueError("top-level value is not an object")
    except Exception as e:
        report["critical"].append(f".agents/hooks.json is not valid JSON ({e}).")
        return report

    guard_handlers = []
    for hook_name, spec in hooks_cfg.items():
        if not isinstance(spec, dict) or spec.get("enabled", True) is False:
            continue
        for event, groups in spec.items():
            if event not in ("PreToolUse", "PostToolUse", "PreInvocation", "PostInvocation", "Stop") or not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                handlers = group.get("hooks") if isinstance(group.get("hooks"), list) else [group]
                for handler in handlers:
                    if not isinstance(handler, dict) or not handler.get("command"):
                        continue
                    cmd = str(handler["command"])
                    is_guard = event == "PreToolUse" and _matcher_matches(group.get("matcher"), "call_mcp_tool")
                    bucket = report["critical"] if is_guard else report["warnings"]
                    try:
                        interp = shlex.split(cmd)[0]
                    except (ValueError, IndexError):
                        interp = cmd.split()[0] if cmd.split() else ""
                    if os.path.isabs(interp):
                        report["warnings"].append(f"[{hook_name}/{event}] uses a machine-specific interpreter path '{interp}'.")
                        if not os.path.exists(interp):
                            bucket.append(f"[{hook_name}/{event}] interpreter not found: {interp}")
                    elif os.name != "nt" and interp and not shutil.which(interp) and not interp.startswith("."):
                        bucket.append(f"[{hook_name}/{event}] interpreter '{interp}' not found on PATH.")
                    scripts = _hook_script_paths(cmd, hooks_dir)
                    for sp in scripts:
                        if not os.path.exists(sp):
                            bucket.append(f"[{hook_name}/{event}] script not found (resolved from .agents/): {sp}")
                    if is_guard:
                        try:
                            timeout_s = int(handler.get("timeout", 30))
                        except (TypeError, ValueError):
                            timeout_s = 30
                        guard_handlers.append((hook_name, cmd, max(1, min(timeout_s, timeout_cap_s))))

    if not guard_handlers:
        report["critical"].append("No enabled PreToolUse hook in .agents/hooks.json matches 'call_mcp_tool'.")
        return report

    if not run_selftest:
        report["ok"] = not report["critical"]
        return report

    if os.name == "nt":
        report["warnings"].append("PreToolUse self-test skipped on Windows (agy runs hooks via `cmd /c`); run the doctor under WSL/Linux/macOS to execute it.")
        report["ok"] = not report["critical"]
        return report

    env = dict(os.environ)
    env[HOOK_SELFTEST_ENV] = "1"
    payload = dict(SYNTHETIC_NEW_ORDER_PAYLOAD)
    payload["workspacePaths"] = [base_dir]
    denied_by_any = False
    for hook_name, cmd, timeout_s in guard_handlers:
        try:
            # The self-test must not refresh the live heartbeat: redirect it to a throwaway file.
            with tempfile.TemporaryDirectory() as hb_dir:
                env[HOOK_HEARTBEAT_OVERRIDE_ENV] = os.path.join(hb_dir, "hook_heartbeat.json")
                proc = subprocess.run(
                    ["sh", "-c", cmd], cwd=hooks_dir, input=json.dumps(payload),
                    capture_output=True, text=True, timeout=timeout_s, env=env,
                )
        except subprocess.TimeoutExpired:
            report["critical"].append(f"[{hook_name}] PreToolUse self-test timed out after {timeout_s}s.")
            continue
        except Exception as e:
            report["critical"].append(f"[{hook_name}] PreToolUse self-test could not run ({e}).")
            continue
        decision_obj = _last_json_object(proc.stdout)
        decision = (decision_obj or {}).get("decision")
        if proc.returncode != 0:
            report["critical"].append(
                f"[{hook_name}] PreToolUse guard exited {proc.returncode} (expected 0; agy needs exit 0 + JSON decision). "
                f"stderr: {(proc.stderr or '').strip()[-300:]}"
            )
        elif decision != "deny":
            report["critical"].append(
                f"[{hook_name}] PreToolUse guard did not deny a direct futures_usds.newOrder (decision={decision!r})."
            )
        else:
            denied_by_any = True
            report["info"].append(f"[{hook_name}] PreToolUse self-test: direct futures_usds.newOrder denied (exit 0).")

    report["ok"] = denied_by_any and not report["critical"]
    return report

def run_doctor(target_env: str = None, auto_heal: bool = False) -> int:
    target_env = resolve_env(target_env)
    start_time = time.time()
    print("=" * 65)
    print("🩺 TRADING DOCTOR — PRE-FLIGHT SYSTEM DIAGNOSTIC")
    print(f"Target Environment: {target_env.upper()}")
    print("=" * 65)

    critical_failures = []
    warnings = []
    ok_items = []

    # 1. API Configuration & Credentials
    cfg = eft.load_env(target_env=target_env)
    api_key, secret_key, base_url = eft.get_client_config(target_env=target_env)
    if not api_key or not secret_key:
        critical_failures.append(f"API credentials not found or invalid in environment config for {target_env.upper()}")
        print(f"❌ [API KEYS] Missing credentials or placeholder in environment config for {target_env.upper()}")
        return 1
    else:
        masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "***"
        ok_items.append(f"Credentials detected for {target_env.upper()} ({masked_key})")
        print(f"✅ [API KEYS] Credentials OK ({target_env.upper()})")

    # Safety Flag verification for PROD
    if is_prod_environment(target_env):
        is_armed = str(cfg.get("LIVE_TRADING_ARMED", "")).strip().lower() == "true"
        if not is_armed:
            critical_failures.append("LIVE_TRADING_ARMED=true is required for PROD/MAINNET live trading.")
            print("❌ [SAFETY FLAG] LIVE_TRADING_ARMED is not 'true'. Live execution disarmed.")
        else:
            ok_items.append("LIVE_TRADING_ARMED=true verified for PROD.")
            print("✅ [SAFETY FLAG] LIVE_TRADING_ARMED=true verified (PROD ARMED)")
    else:
        print("ℹ️  [SAFETY FLAG] Sandbox mode (Testnet). LIVE_TRADING_ARMED flag not required.")

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

            max_drift_ms = 2500 if (target_env == "testnet" or api_key == "MCP_OAUTH_ACTIVE") else 1000
            if drift_ms > max_drift_ms:
                critical_failures.append(f"Excessive clock drift: {drift_ms}ms (limit: {max_drift_ms}ms)")
                print(f"❌ [CLOCK DRIFT] Dangerous clock drift: {drift_ms}ms (limit: {max_drift_ms}ms)")
            elif drift_ms > 400:
                warnings.append(f"Moderate clock drift: {drift_ms}ms (compensated dynamically by execution harness)")
                print(f"⚠️  [CLOCK DRIFT] Moderate clock drift: {drift_ms}ms (compensated dynamically)")
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

    # 3b. User Profile Calibration Check (FAIL CLOSED)
    try:
        import user_profile as up
        profile = up.load_user_profile()
        risk_pct = profile.get("risk_pct_equity", 0.005) * 100
        is_completed = profile.get("profile_completed", False)
        if not is_completed:
            critical_failures.append("User profile has not completed onboarding. Run 'python3 scripts/user_profile.py --setup' before trading.")
            print("🚨 [USER PROFILE] User profile has not completed onboarding. Run 'python3 scripts/user_profile.py --setup' before trading.")
        else:
            ok_items.append(f"User Profile calibrated (Risk: {risk_pct:.2f}% equity, Mode: {profile.get('operating_mode')})")
            print(f"✅ [USER PROFILE] Calibrated: {risk_pct:.2f}% risk per trade ({profile.get('operating_mode')})")
    except Exception as e:
        critical_failures.append(f"User profile error: {e}")
        print(f"🚨 [USER PROFILE] Could not load profile: {e}")

    # 3c. PreToolUse Safety Hook Activation Check (FAIL CLOSED): parse, resolve and EXECUTE the guard
    base_dir = find_workspace_root()
    try:
        hook_report = check_pretool_hook(base_dir)
    except Exception as e:
        hook_report = {"ok": False, "critical": [f"PreToolUse hook check crashed ({e})."], "warnings": [], "info": []}
    for msg in hook_report.get("info", []):
        print(f"ℹ️  [SAFETY HOOKS] {msg}")
    for msg in hook_report.get("warnings", []):
        warnings.append(f"Safety hooks: {msg}")
        print(f"⚠️  [SAFETY HOOKS] {msg}")
    if hook_report.get("ok"):
        ok_items.append("PreToolUse safety guard verified (.agents/hooks.json)")
        print("✅ [SAFETY HOOKS] PreToolUse execution guard configured and verified.")
    else:
        for msg in hook_report.get("critical", []) or ["PreToolUse guard could not be verified."]:
            critical_failures.append(f"Safety hooks: {msg} Live order execution is strictly prohibited.")
            print(f"🚨 [SAFETY HOOKS] {msg} Live order execution is strictly prohibited.")

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
                    # Verified emergency stop (place + confirm on openAlgoOrders); never opens exposure
                    heal_res = eft.heal_orphan_position(op, target_env=target_env, close_on_failure=False)
                    if heal_res.get("success"):
                        print(f"   🛡️ Auto-Heal successful for {sym}: verified Algo SL at {heal_res.get('healed_sl_price')}")
                        ok_items.append(f"Auto-Heal applied to {sym}")
                    else:
                        critical_failures.append(f"Auto-Heal failure on {sym}: {heal_res.get('reason')}")
                        print(f"   ❌ Failed to apply Auto-Heal on {sym}: {heal_res.get('reason')}")
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

    # 6. Shadow Desk Counterfactual Audit
    try:
        import shadow_tracker
        shadow_metrics = shadow_tracker.calculate_efficacy_metrics()
        active_shadows = shadow_metrics.get("active_shadow_trades", 0)
        resolved_shadows = shadow_metrics.get("total_resolved", 0)
        fer = shadow_metrics.get("filter_efficacy_ratio_pct", 0.0)
        saved = shadow_metrics.get("capital_saved_usdt", 0.0)
        print(f"👻 [SHADOW DESK] {active_shadows} unexecuted candidate(s) under counterfactual monitoring | Resolved: {resolved_shadows} (FER: {fer}% | Saved: +${saved} USDT)")
    except Exception:
        pass

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
    default_env = resolve_env()
    parser = argparse.ArgumentParser(description="Trading Doctor - Pre-flight Health Check")
    parser.add_argument("--env", default=default_env, help="Target execution environment (prod/testnet)")
    parser.add_argument("--heal", action="store_true", help="Auto-heal orphan positions by placing emergency SL")
    args = parser.parse_args()

    sys.exit(run_doctor(target_env=args.env, auto_heal=args.heal))
