#!/usr/bin/env python3
"""
pre_trade_guard.py - PreToolUse Hook for the agentic runtime.
Deterministic pre-execution safety harness and programmatic risk verification.

Intercepts run_command and call_mcp_tool prior to execution to enforce mechanical hard gates:
1. RISK REDUCTION EXCEPTION (INVARIANT):
   Position closes, risk reduction, move_to_breakeven, reduceOnly/closePosition, or orphan heals are authorized IMMEDIATELY.
2. PRODUCTION INVARIANT:
   In PROD (env == "prod" or BINANCE_API_ENV == "PROD"), gate bypasses are strictly FORBIDDEN.
3. CLEAN-ROOM EVALUATION GATE:
   Strictly prohibits opening trades without a valid APPROVED dossier (< 20 min TTL)
   issued by the 'isolated_market_evaluator' subagent containing the symbol in approved_symbols.
4. LEVERAGE GATE:
   Blocks changeInitialLeverage > 3x unless authorized in the dossier as a YOLO moonshot.
5. DELTA-NEUTRAL GATE:
   Blocks BUY (Long) if portfolio is LONG_HEAVY; blocks SELL (Short) if portfolio is SHORT_HEAVY.
   Permits hedging orders (e.g. SELL when LONG_HEAVY).
6. FAIL-CLOSED:
   Any unhandled exception or parsing error immediately emits {"decision": "deny"}.

Target latency: < 15ms.
"""

import os
import sys
import json
import time
import re
from typing import Dict, Any, Tuple, Optional

def parse_mcp_arguments(args_dict: dict) -> dict:
    """Extracts dictionary of arguments from tool call args regardless of serialization."""
    if not args_dict:
        return {}
    if "Arguments" in args_dict:
        raw = args_dict["Arguments"]
        if isinstance(raw, dict):
            return raw
        elif isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
    return args_dict

def is_risk_reducing_action(cmd_or_name: str, args_dict: dict = None) -> bool:
    """Verifies whether an action reduces or eliminates risk (NEVER blocked)."""
    # 1. Check structured MCP arguments
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)

        reduce_only = mcp_args.get("reduceOnly", mcp_args.get("reduce_only"))
        if reduce_only is True or str(reduce_only).lower() == "true":
            return True

        close_pos = mcp_args.get("closePosition", mcp_args.get("close_position"))
        if close_pos is True or str(close_pos).lower() == "true":
            return True

        mcp_tool = str(args_dict.get("ToolName", "")).lower()
        if any(kw in mcp_tool for kw in ["cancel", "deleteorder", "closeposition", "algoorder"]):
            return True

    # 2. Check command line string
    cmd_lower = cmd_or_name.lower()
    reducing_signals = [
        "close_position_market", "move_sl_to_breakeven", "move_to_breakeven",
        "audit_orphan_positions", "night_cutoff_loop", "cancel_order", "delete_order"
    ]
    if any(sig in cmd_lower for sig in reducing_signals):
        return True

    if "--close" in cmd_lower:
        return True

    if "--reduce-only" in cmd_lower or "--reduce_only" in cmd_lower:
        if not re.search(r"--reduce[-_]only\s+false", cmd_lower):
            return True

    return False

def extract_target_symbol(cmd: str, args_dict: dict = None) -> str:
    """Extracts target symbol from tool arguments or shell command line."""
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        if "symbol" in mcp_args:
            return str(mcp_args["symbol"]).upper().strip()
        if "symbol" in args_dict:
            return str(args_dict["symbol"]).upper().strip()

    m = re.search(r"--symbol\s+([A-Za-z0-9_]+)", cmd)
    if m:
        return m.group(1).upper().strip()
    m2 = re.search(r"['\"]([A-Z0-9]+USDT)['\"]", cmd)
    if m2:
        return m2.group(1).upper().strip()
    return ""

def is_prod_environment(cmd: str, args_dict: dict = None) -> bool:
    """Detects if the runtime environment is PROD / Mainnet."""
    env_vars = [
        os.environ.get("BINANCE_API_ENV", ""),
        os.environ.get("ENV", ""),
        os.environ.get("TARGET_ENV", "")
    ]
    for ev in env_vars:
        if ev.strip().lower() in ["prod", "production", "mainnet"]:
            return True

    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        t_env = str(args_dict.get("target_env", args_dict.get("env", mcp_args.get("target_env", "")))).strip().lower()
        if t_env in ["prod", "production", "mainnet"]:
            return True

    if cmd and re.search(r"--env\s+(prod|mainnet)", cmd, re.IGNORECASE):
        return True

    return False

def find_workspace_root() -> str:
    p = os.path.abspath(__file__)
    while p and p != os.path.dirname(p):
        p = os.path.dirname(p)
        if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "logs")):
            return p
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def check_leverage_gate(symbol: str, requested_leverage: int, base_dir: str) -> Tuple[bool, str]:
    """
    Validates leverage changes against standard limits (<= 3x) or approved YOLO status.
    """
    if requested_leverage <= 3:
        return True, "Standard leverage (<= 3x) authorized."

    dossier_file = os.path.join(base_dir, "logs", "evaluations", "latest_dossier.json")
    if not os.path.exists(dossier_file):
        return False, f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): Requested leverage ({requested_leverage}x > 3x) exceeds standard ceiling and no evaluation dossier exists."

    try:
        with open(dossier_file, "r", encoding="utf-8") as f:
            dossier_data = json.load(f)
    except Exception as e:
        return False, f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): Failed to read evaluation dossier ({e})."

    now_ts = int(time.time())
    valid_until = dossier_data.get("valid_until_ts", 0)
    status = dossier_data.get("status", "").upper()

    if now_ts > valid_until or status != "APPROVED":
        return False, f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): Evaluation dossier expired or status is '{status}'."

    approved_symbols = dossier_data.get("approved_symbols", [])
    if symbol and symbol not in approved_symbols:
        return False, f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): Asset '{symbol}' is not approved in evaluation dossier."

    # Verify YOLO authorization
    is_yolo_authorized = False
    for cand in dossier_data.get("approved_candidates", []):
        if cand.get("symbol", "").upper() == symbol:
            cand_yolo = cand.get("is_yolo")
            tier = str(cand.get("tier", "")).lower()
            strategy = str(cand.get("strategy", "")).lower()
            cand_lev = cand.get("leverage", 3)
            try:
                cand_lev = int(cand_lev)
            except Exception:
                cand_lev = 3

            if cand_yolo is True or str(cand_yolo).lower() == "true":
                is_yolo_authorized = True
            elif "yolo" in tier or "yolo" in strategy:
                is_yolo_authorized = True
            elif cand_lev >= requested_leverage:
                is_yolo_authorized = True
            break

    if symbol in dossier_data.get("yolo_approved_symbols", []):
        is_yolo_authorized = True

    if is_yolo_authorized:
        return True, f"YOLO moonshot leverage ({requested_leverage}x) authorized for '{symbol}' in evaluation dossier."
    else:
        return False, (
            f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): "
            f"Requested leverage ({requested_leverage}x > 3x) for '{symbol}' is not authorized as a YOLO moonshot in the evaluation dossier."
        )

def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            print(json.dumps({"decision": "allow"}))
            return

        payload = json.loads(raw_input)
        tool_call = payload.get("toolCall", {})
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args", {})
        command_line = args.get("CommandLine", "")

        mcp_args = parse_mcp_arguments(args)

        # -------------------------------------------------------------
        # 1. IDENTIFY TARGET ACTION TYPE
        # -------------------------------------------------------------
        is_trading_command = False
        is_leverage_command = False

        if tool_name == "run_command":
            subcmds = re.split(r'(&&|;|\|\||\|)', command_line)
            for subcmd in subcmds:
                subcmd_clean = subcmd.strip()
                if not subcmd_clean or subcmd_clean in ["&&", ";", "||", "|"]:
                    continue
                if any(subcmd_clean.startswith(p) for p in ["git ", "gh ", "grep ", "cat ", "ls ", "find ", "diff ", "python3 -m py_compile ", "python3 -m unittest", "pytest", "cp ", "rm ", "mkdir ", "chmod "]):
                    continue
                if any(script in subcmd_clean for script in ["execute_futures_trade.py", "deploy_fresh_basket.py", "deploy_"]):
                    is_trading_command = True
                    break

        elif tool_name == "call_mcp_tool":
            server_name = args.get("ServerName", "")
            mcp_tool_name = args.get("ToolName", "")
            if server_name == "binance":
                if mcp_tool_name in ["futures_usds.newOrder", "margin.marginAccountNewOrder"]:
                    is_trading_command = True
                elif mcp_tool_name == "futures_usds.changeInitialLeverage":
                    is_leverage_command = True
            elif server_name == "crypto_radar":
                if any(kw in mcp_tool_name.lower() for kw in ["deploy_futures_trade", "placeorder"]):
                    is_trading_command = True

        if not is_trading_command and not is_leverage_command:
            print(json.dumps({"decision": "allow"}))
            return

        base_dir = find_workspace_root()

        # -------------------------------------------------------------
        # 2. INVARIANT: NEVER BLOCK RISK-REDUCING ACTIONS
        # -------------------------------------------------------------
        if is_risk_reducing_action(command_line, args):
            print(json.dumps({"decision": "allow", "reason": "Risk-reducing action / exit authorized."}))
            return

        target_sym = extract_target_symbol(command_line, args)
        now_ts = int(time.time())
        is_prod = is_prod_environment(command_line, args)

        # -------------------------------------------------------------
        # 3. LEVERAGE GATE (for futures_usds.changeInitialLeverage)
        # -------------------------------------------------------------
        if is_leverage_command:
            raw_lev = mcp_args.get("leverage")
            try:
                requested_lev = int(float(raw_lev))
            except Exception:
                print(json.dumps({
                    "decision": "deny",
                    "reason": f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): Invalid leverage value ({raw_lev})."
                }))
                return

            allowed, reason = check_leverage_gate(target_sym, requested_lev, base_dir)
            if allowed:
                print(json.dumps({"decision": "allow", "reason": reason}))
            else:
                print(json.dumps({"decision": "deny", "reason": reason}))
            return

        # -------------------------------------------------------------
        # 4. PRODUCTION INVARIANT: NO GATE BYPASSES IN PROD
        # -------------------------------------------------------------
        has_bypass_eval = ("--bypass-eval-gate" in command_line or args.get("bypass_eval_gate") is True or mcp_args.get("bypass_eval_gate") is True)
        has_bypass_delta = ("--bypass-delta-gate" in command_line or args.get("bypass_delta_gate") is True or mcp_args.get("bypass_delta_gate") is True)

        if is_prod and (has_bypass_eval or has_bypass_delta):
            deny_msg = (
                "🚨 PROD INVARIANT VIOLATION: Gate bypasses (--bypass-eval-gate, --bypass-delta-gate) "
                "are strictly FORBIDDEN in PROD environment."
            )
            print(json.dumps({"decision": "deny", "reason": deny_msg}))
            return

        if is_prod:
            has_bypass_eval = False
            has_bypass_delta = False

        # -------------------------------------------------------------
        # 5. GATE 1: MANDATORY CLEAN-ROOM EVALUATOR (HARNESS GATE)
        # -------------------------------------------------------------
        dossier_file = os.path.join(base_dir, "logs", "evaluations", "latest_dossier.json")

        if not has_bypass_eval:
            is_valid_dossier = False
            dossier_data = None
            if os.path.exists(dossier_file):
                try:
                    with open(dossier_file, "r", encoding="utf-8") as f:
                        dossier_data = json.load(f)
                    valid_until = dossier_data.get("valid_until_ts", 0)
                    status = dossier_data.get("status", "").upper()
                    if now_ts <= valid_until and status == "APPROVED":
                        is_valid_dossier = True
                except Exception:
                    is_valid_dossier = False

            if not is_valid_dossier:
                deny_msg = (
                    "🚨 ACTION BLOCKED BY PRE-TOOL-USE HOOK (Hard Gate - Clean-Room Evaluator Required):\n"
                    "Executing orders directly in primary chat without prior clean-room evaluation is STRICTLY PROHIBITED.\n"
                    "The primary agent MUST NOT make inline trade decisions or bypass the multi-agent harness.\n\n"
                    "👉 REQUIRED ACTION:\n"
                    "1. Invoke subagent 'isolated_market_evaluator' via invoke_subagent, providing the deterministic brief from 'python3 scripts/prime_evaluator_brief.py'.\n"
                    "2. The evaluator subagent must emit the Master Dossier and persist it via 'python3 scripts/record_evaluation.py'.\n"
                    "3. Only with a fresh 'APPROVED' dossier (< 20 min) may execution proceed."
                )
                print(json.dumps({"decision": "deny", "reason": deny_msg}))
                return

            approved_symbols = dossier_data.get("approved_symbols", []) if dossier_data else []
            if target_sym and approved_symbols and target_sym not in approved_symbols:
                deny_msg = (
                    f"🚨 ACTION BLOCKED BY PRE-TOOL-USE HOOK:\n"
                    f"Asset '{target_sym}' was NOT approved in the evaluator dossier ({dossier_data.get('evaluator_agent')}).\n"
                    f"Approved assets: {', '.join(approved_symbols)}.\n"
                    "By quantitative discipline, trading assets outside the validated dossier is prohibited."
                )
                print(json.dumps({"decision": "deny", "reason": deny_msg}))
                return

        # -------------------------------------------------------------
        # 6. GATE 2: DELTA-NEUTRAL & PORTFOLIO MANAGEMENT
        # -------------------------------------------------------------
        if not has_bypass_delta:
            state_file = os.path.join(base_dir, "logs", "session_state.json")
            if os.path.exists(state_file):
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        state = json.load(f)

                    is_long_attempt = False
                    is_short_attempt = False

                    if tool_name == "call_mcp_tool":
                        side_arg = str(mcp_args.get("side", "")).upper().strip()
                        dir_arg = str(mcp_args.get("direction", "")).upper().strip()
                        if side_arg == "BUY" or dir_arg == "LONG":
                            is_long_attempt, is_short_attempt = True, False
                        elif side_arg == "SELL" or dir_arg == "SHORT":
                            is_short_attempt, is_long_attempt = True, False
                    else:
                        is_long_attempt = bool(re.search(r"['\"]?LONG['\"]?", command_line, re.IGNORECASE)) or "--dir LONG" in command_line.upper() or "BUY" in command_line.upper()
                        is_short_attempt = bool(re.search(r"['\"]?SHORT['\"]?", command_line, re.IGNORECASE)) or "--dir SHORT" in command_line.upper()

                    portfolio = state.get("portfolio_exposure", {})
                    delta_bias = portfolio.get("delta_bias", "NEUTRAL")

                    if delta_bias == "LONG_HEAVY" and is_long_attempt and not is_short_attempt:
                        reason_msg = (
                            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Delta-Neutral Hard Gate): "
                            f"Portfolio is bullishly unbalanced (Delta: +${portfolio.get('net_notional_delta_usdt', 0):.2f} USDT / LONG_HEAVY). "
                            "Opening additional Longs without Short hedging is strictly prohibited."
                        )
                        print(json.dumps({"decision": "deny", "reason": reason_msg}))
                        return

                    elif delta_bias == "SHORT_HEAVY" and is_short_attempt and not is_long_attempt:
                        reason_msg = (
                            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Delta-Neutral Hard Gate): "
                            f"Portfolio is bearishly unbalanced (Delta: -${abs(portfolio.get('net_notional_delta_usdt', 0)):.2f} USDT / SHORT_HEAVY). "
                            "Opening additional Shorts without Long hedging is strictly prohibited."
                        )
                        print(json.dumps({"decision": "deny", "reason": reason_msg}))
                        return
                except Exception:
                    pass

        # All gates passed successfully
        print(json.dumps({"decision": "allow", "reason": "Mechanical hard gates and subagent validation PASSED successfully."}))

    except Exception as e:
        # FAIL-CLOSED: Any internal hook error blocks execution — never silently allow
        sys.stderr.write(f"[PRE-TRADE-GUARD INTERNAL ERROR] {str(e)}\n")
        print(json.dumps({"decision": "deny",
                          "reason": f"🚨 FAIL-CLOSED: Pre-trade guard internal error ({str(e)}). Cannot verify safety — order blocked."}))

if __name__ == "__main__":
    main()
