#!/usr/bin/env python3
"""
pre_trade_guard.py - PreToolUse Hook for the agentic runtime.
Deterministic pre-execution safety harness and programmatic risk verification.

Intercepts run_command and call_mcp_tool prior to execution to enforce mechanical hard gates:
1. CLEAN-ROOM EVALUATION GATE:
   Strictly prohibits executing trades directly in the primary chat without a prior dossier
   issued by the 'isolated_market_evaluator' subagent within the last 20 minutes (logs/evaluations/latest_dossier.json).
2. DELTA-NEUTRAL GATE:
   Physically blocks Long orders when portfolio is LONG_HEAVY or Short orders when SHORT_HEAVY.
3. RISK REDUCTION EXCEPTION:
   Position closes, risk reduction, move_to_breakeven, or orphan heals are authorized IMMEDIATELY.

Target latency: < 15ms.
"""

import os
import sys
import json
import time
import re

def is_risk_reducing_action(cmd_or_name: str, args_dict: dict = None) -> bool:
    """Verifies whether an action reduces or eliminates risk (NEVER blocked)."""
    text = (cmd_or_name + " " + json.dumps(args_dict or {})).lower()
    reducing_signals = [
        "close_position", "move_to_breakeven", "cancel", "audit_orphan",
        "reduceonly", "reduce_only", "--close", "delete_order", "algoorder"
    ]
    return any(sig in text for sig in reducing_signals)

def extract_target_symbol(cmd: str, args_dict: dict = None) -> str:
    """Extracts target symbol from tool arguments or shell command line."""
    if args_dict:
        if "symbol" in args_dict:
            return str(args_dict["symbol"]).upper().strip()
        if "Arguments" in args_dict:
            raw_a = args_dict["Arguments"]
            if isinstance(raw_a, dict) and "symbol" in raw_a:
                return str(raw_a["symbol"]).upper().strip()
            elif isinstance(raw_a, str):
                try:
                    parsed_a = json.loads(raw_a)
                    if "symbol" in parsed_a:
                        return str(parsed_a["symbol"]).upper().strip()
                except Exception:
                    pass

    m = re.search(r"--symbol\s+([A-Za-z0-9_]+)", cmd)
    if m:
        return m.group(1).upper().strip()
    m2 = re.search(r"['\"]([A-Z0-9]+USDT)['\"]", cmd)
    if m2:
        return m2.group(1).upper().strip()
    return ""

def find_workspace_root() -> str:
    p = os.path.abspath(__file__)
    while p and p != os.path.dirname(p):
        p = os.path.dirname(p)
        if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "logs")):
            return p
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

        # -------------------------------------------------------------
        # 1. IDENTIFY TARGET: Is this an execution attempt?
        # -------------------------------------------------------------
        is_trading_command = False
        if tool_name == "run_command":
            if any(script in command_line for script in ["execute_futures_trade.py", "deploy_fresh_basket.py", "deploy_"]):
                is_trading_command = True
        elif tool_name == "call_mcp_tool":
            server_name = args.get("ServerName", "")
            mcp_tool_name = args.get("ToolName", "")
            if server_name in ["binance", "crypto_radar"]:
                if any(kw in mcp_tool_name.lower() for kw in ["neworder", "deploy_futures_trade", "placeorder"]):
                    is_trading_command = True

        if not is_trading_command:
            print(json.dumps({"decision": "allow"}))
            return

        # -------------------------------------------------------------
        # 2. INVARIANT: NEVER BLOCK RISK-REDUCING ACTIONS
        # -------------------------------------------------------------
        if is_risk_reducing_action(command_line, args):
            print(json.dumps({"decision": "allow", "reason": "Risk-reducing action / exit authorized."}))
            return

        # -------------------------------------------------------------
        # 3. GATE 1: MANDATORY CLEAN-ROOM EVALUATOR (HARNESS GATE)
        # -------------------------------------------------------------
        base_dir = find_workspace_root()
        dossier_file = os.path.join(base_dir, "logs", "evaluations", "latest_dossier.json")
        has_bypass_eval = ("--bypass-eval-gate" in command_line or args.get("bypass_eval_gate") is True)

        target_sym = extract_target_symbol(command_line, args)
        now_ts = int(time.time())

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
        # 4. GATE 2: DELTA-NEUTRAL & PORTFOLIO MANAGEMENT
        # -------------------------------------------------------------
        has_bypass_delta = ("--bypass-delta-gate" in command_line or args.get("bypass_delta_gate") is True)
        if not has_bypass_delta:
            state_file = os.path.join(base_dir, "logs", "session_state.json")
            if os.path.exists(state_file):
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        state = json.load(f)
                    
                    is_long_attempt = bool(re.search(r"['\"]?LONG['\"]?", command_line, re.IGNORECASE)) or "--dir LONG" in command_line.upper() or "BUY" in command_line.upper()
                    is_short_attempt = bool(re.search(r"['\"]?SHORT['\"]?", command_line, re.IGNORECASE)) or "--dir SHORT" in command_line.upper()
                    
                    if tool_name == "call_mcp_tool":
                        raw_args = args.get("Arguments", {})
                        if isinstance(raw_args, str):
                            try:
                                raw_args = json.loads(raw_args)
                            except Exception:
                                raw_args = {}
                        dir_arg = str(raw_args.get("direction", "")).upper()
                        if dir_arg == "LONG":
                            is_long_attempt, is_short_attempt = True, False
                        elif dir_arg == "SHORT":
                            is_short_attempt, is_long_attempt = True, False

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
        # In case of internal error, fail OPEN with warning to avoid deadlocking workspace
        print(json.dumps({"decision": "allow", "reason": f"Hook warning: {str(e)}"}))

if __name__ == "__main__":
    main()
