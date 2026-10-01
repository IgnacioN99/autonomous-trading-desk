#!/usr/bin/env python3
"""
pre_trade_guard.py - PreToolUse Hook for the agentic runtime.
Deterministic pre-execution safety harness and programmatic risk verification.

Hardened against fail-open behaviors and spoofing vulnerabilities (Issue #9):
1. DEFAULT-DENY ON UNRECOGNIZED/EMPTY INPUT:
   Empty payload, invalid JSON, or malformed payload shape immediately return 'deny' with code 2.
2. SINGLE CHOKE POINT ENFORCEMENT:
   Opening trades are permitted ONLY via 'deploy_futures_trade' or 'execute_futures_trade.py'.
   Direct calls to Binance MCP write tools (futures_usds.newOrder, spot.newOrder, etc.) are DENIED
   unless explicitly flagged as risk-reducing (reduceOnly=true, closePosition=true, cancelOrder).
3. STRUCTURED RISK-REDUCING ACTION PARSING:
   Requires structured arguments (--close-position, --auto-heal, --audit-orphans, reduceOnly=true).
   Never matches generic substrings like 'close' across the command line.
4. FAIL-CLOSED SESSION STATE & STALENESS CHECK:
   Verifies that logs/session_state.json exists, is valid (is_valid=True), and is NOT stale (<= 300s).
   Stale or invalid state in PROD immediately blocks order execution.
5. EVALUATION DOSSIER ANTI-SPOOFING:
   Never trusts valid_until_ts blindly. Effective expiry = min(valid_until_ts, timestamp_ts + 1200).
   Missing, negative, or future timestamp_ts treats dossier as expired and blocks execution.
6. STRICT DELTA DIRECTION PARSING:
   Direction must strictly parse to LONG or SHORT. If both or neither match, fails closed (deny).
7. ENVIRONMENT RESOLUTION:
   Uses centralized env_resolver to resolve and enforce PROD invariants.

Target latency: < 15ms.
"""

import os
import sys
import json
import time
import re
from typing import Dict, Any, Tuple, Optional

# Ensure scripts directory is on sys.path for utils
base_script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_script_dir not in sys.path:
    sys.path.insert(0, base_script_dir)

try:
    from utils.env_resolver import resolve_env, is_prod_environment, find_workspace_root
except ImportError:
    try:
        from scripts.utils.env_resolver import resolve_env, is_prod_environment, find_workspace_root
    except ImportError:
        def find_workspace_root() -> str:
            p = os.path.abspath(__file__)
            while p and p != os.path.dirname(p):
                p = os.path.dirname(p)
                if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "logs")):
                    return p
            return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        def resolve_env(explicit_env=None, base_dir=None) -> str:
            val = (explicit_env or os.environ.get("BINANCE_API_ENV") or "prod").strip().lower()
            return "prod" if val in ["prod", "production", "mainnet"] else "testnet"

        def is_prod_environment(explicit_env=None, base_dir=None) -> bool:
            return resolve_env(explicit_env, base_dir=base_dir) == "prod"


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
    """
    Verifies whether an action reduces or eliminates risk (NEVER blocked).
    Structured arguments ONLY:
    - MCP: reduceOnly=true, closePosition=true, or risk-reducing tools (cancelOrder, close_position_market, etc.)
    - CLI: --close-position, --auto-heal, --audit-orphans, --reduce-only (unless false), or explicit risk-reduction scripts.
    Never relies on generic substring 'close' across the command line.
    """
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
        reducing_tools = [
            "cancelorder", "cancelalgoorder", "cancelallopenorders",
            "deleteorder", "closeposition", "close_position_market",
            "move_to_breakeven", "move_sl_to_breakeven", "audit_orphan_positions"
        ]
        if any(kw in mcp_tool for kw in reducing_tools):
            return True

    # 2. Check structured command line flags and dedicated scripts
    if cmd_or_name:
        cmd_str = cmd_or_name

        # Explicit structured flags
        if re.search(r"--close[-_]position\b", cmd_str, re.IGNORECASE):
            return True

        if re.search(r"--auto[-_]heal\b", cmd_str, re.IGNORECASE):
            return True

        if re.search(r"--audit[-_]orphans\b", cmd_str, re.IGNORECASE):
            return True

        if re.search(r"(?:^|\s)(?:--help|-h)\b", cmd_str, re.IGNORECASE):
            return True

        # reduce-only flag check (must not be followed by false)
        if re.search(r"--reduce[-_]only\b", cmd_str, re.IGNORECASE):
            if not re.search(r"--reduce[-_]only(?:\s+|=)false\b", cmd_str, re.IGNORECASE):
                return True

        # Dedicated risk-reducing script invocations
        if re.search(r"\b(night_cutoff_loop|audit_orphan_positions|close_position_market|close_position)\.py\b", cmd_str):
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

    if cmd:
        m = re.search(r"--symbol(?:\s+|=)(['\"]?)([A-Za-z0-9_]+)\1", cmd)
        if m:
            return m.group(2).upper().strip()
        m2 = re.search(r"['\"]([A-Z0-9]+USDT)['\"]", cmd)
        if m2:
            return m2.group(1).upper().strip()
    return ""


def extract_env_argument(cmd: str, args_dict: dict = None) -> Optional[str]:
    """Extracts explicit environment parameter from tool args or command line."""
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        for key in ["target_env", "env", "TARGET_ENV", "BINANCE_API_ENV"]:
            if key in args_dict and args_dict[key]:
                return str(args_dict[key]).strip()
            if key in mcp_args and mcp_args[key]:
                return str(mcp_args[key]).strip()

    if cmd:
        m = re.search(r"--env(?:\s+|=)(['\"]?)([A-Za-z0-9_-]+)\1", cmd, re.IGNORECASE)
        if m:
            return m.group(2).strip()

    return None


def parse_trade_direction(cmd: str, args_dict: dict = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Strictly parses trade direction ('LONG' or 'SHORT').
    Returns (direction, error_reason).
    If both or neither match, returns (None, error_reason) for fail-closed rejection.
    """
    found_dirs = set()

    # 1. Check structured MCP arguments
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        dir_val = str(mcp_args.get("direction", "")).upper().strip()
        side_val = str(mcp_args.get("side", "")).upper().strip()

        if dir_val in ["LONG", "BUY"]:
            found_dirs.add("LONG")
        elif dir_val in ["SHORT", "SELL"]:
            found_dirs.add("SHORT")
        elif dir_val:
            return None, f"Invalid direction value '{dir_val}' in tool arguments."

        if side_val in ["BUY", "LONG"]:
            found_dirs.add("LONG")
        elif side_val in ["SELL", "SHORT"]:
            found_dirs.add("SHORT")
        elif side_val:
            return None, f"Invalid side value '{side_val}' in tool arguments."

        if len(found_dirs) == 1:
            return next(iter(found_dirs)), None
        elif len(found_dirs) > 1:
            return None, "Conflicting trade directions detected in tool arguments (both LONG and SHORT)."

    # 2. Check structured command line flags: --dir, --direction, --side
    if cmd:
        flags_found = set()
        for m in re.finditer(r"--(?:dir(?:ection)?|side)(?:\s+|=)(['\"]?)(LONG|SHORT|BUY|SELL)\1\b", cmd, re.IGNORECASE):
            val = m.group(2).upper()
            if val in ["LONG", "BUY"]:
                flags_found.add("LONG")
            elif val in ["SHORT", "SELL"]:
                flags_found.add("SHORT")

        if not flags_found:
            # Check for quoted standalone words 'LONG' or 'SHORT' (e.g. positional script args)
            quoted_long = bool(re.search(r"['\"](LONG|BUY)['\"]", cmd, re.IGNORECASE))
            quoted_short = bool(re.search(r"['\"](SHORT|SELL)['\"]", cmd, re.IGNORECASE))
            if quoted_long:
                flags_found.add("LONG")
            if quoted_short:
                flags_found.add("SHORT")

        if len(flags_found) == 1:
            return next(iter(flags_found)), None
        elif len(flags_found) > 1:
            return None, "Conflicting trade directions detected in command line (both LONG and SHORT matched)."

    return None, "Unable to determine trade direction strictly (neither LONG nor SHORT found)."


def validate_dossier(dossier_data: dict, now_ts: int) -> Tuple[bool, str, int]:
    """
    Validates evaluation dossier with Finding 7 anti-spoofing checks:
    - timestamp_ts must be present, positive, and <= now_ts + 5 (small clock drift buffer).
    - effective expiry = min(valid_until_ts, timestamp_ts + 1200) (max 20 minutes TTL).
    - now_ts <= expiry and status == 'APPROVED'.
    Returns (is_valid, reason, effective_expiry).
    """
    if not isinstance(dossier_data, dict):
        return False, "Evaluation dossier is not a valid JSON dictionary.", 0

    timestamp_ts = dossier_data.get("timestamp_ts")
    if timestamp_ts is None:
        return False, "Evaluation dossier timestamp_ts is missing.", 0
    try:
        timestamp_ts = int(timestamp_ts)
    except Exception:
        return False, f"Evaluation dossier timestamp_ts '{timestamp_ts}' is invalid.", 0

    if timestamp_ts <= 0:
        return False, "Evaluation dossier timestamp_ts must be positive.", 0

    if timestamp_ts > now_ts + 5:
        return False, f"Evaluation dossier timestamp_ts ({timestamp_ts}) is in the future (now: {now_ts}).", 0

    valid_until = dossier_data.get("valid_until_ts", 0)
    try:
        valid_until = int(valid_until)
    except Exception:
        valid_until = 0

    effective_expiry = min(valid_until, timestamp_ts + 1200)
    status = str(dossier_data.get("status", "")).upper()

    if status != "APPROVED":
        return False, f"Evaluation dossier status is '{status}', expected 'APPROVED'.", effective_expiry

    if now_ts > effective_expiry:
        return False, f"Evaluation dossier has expired (effective expiry: {effective_expiry}, now: {now_ts}).", effective_expiry

    return True, "Dossier valid and approved.", effective_expiry


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
    is_valid, reason, _ = validate_dossier(dossier_data, now_ts)
    if not is_valid:
        return False, f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): {reason}"

    approved_symbols = [s.upper() for s in dossier_data.get("approved_symbols", [])]
    if symbol and symbol.upper() not in approved_symbols:
        return False, f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): Asset '{symbol}' is not approved in evaluation dossier."

    # Verify YOLO authorization
    is_yolo_authorized = False
    for cand in dossier_data.get("approved_candidates", []):
        if str(cand.get("symbol", "")).upper() == symbol.upper():
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

    if symbol.upper() in [s.upper() for s in dossier_data.get("yolo_approved_symbols", [])]:
        is_yolo_authorized = True

    if is_yolo_authorized:
        return True, f"YOLO moonshot leverage ({requested_leverage}x) authorized for '{symbol}' in evaluation dossier."
    else:
        return False, (
            f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): "
            f"Requested leverage ({requested_leverage}x > 3x) for '{symbol}' is not authorized as a YOLO moonshot in the evaluation dossier."
        )


def emit_decision(decision: str, reason: str = "", code: int = None) -> int:
    """Emits decision payload to stdout and returns the exit code."""
    res = {"decision": decision}
    if code is not None:
        res["code"] = code
    elif decision == "deny":
        res["code"] = 2
    if reason:
        res["reason"] = reason
    print(json.dumps(res))
    return res.get("code", 0) if decision == "deny" else 0


def main() -> int:
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            return emit_decision("deny", reason="🚨 FAIL-CLOSED: Empty payload received by pre-trade guard.", code=2)

        try:
            payload = json.loads(raw_input)
        except Exception as e:
            return emit_decision("deny", reason=f"🚨 FAIL-CLOSED: Invalid JSON payload ({str(e)}).", code=2)

        if not isinstance(payload, dict) or "toolCall" not in payload or not isinstance(payload.get("toolCall"), dict):
            return emit_decision("deny", reason="🚨 FAIL-CLOSED: Unknown or malformed payload shape (missing toolCall object).", code=2)

        tool_call = payload.get("toolCall", {})
        tool_name = tool_call.get("name", "")
        if not tool_name or not isinstance(tool_name, str):
            return emit_decision("deny", reason="🚨 FAIL-CLOSED: Missing or invalid tool name in toolCall.", code=2)

        args = tool_call.get("args", {})
        if not isinstance(args, dict):
            args = {}
        command_line = args.get("CommandLine", "")
        mcp_args = parse_mcp_arguments(args)

        # -------------------------------------------------------------
        # 1. RISK REDUCTION INVARIANT: NEVER BLOCK RISK-REDUCING ACTIONS
        # -------------------------------------------------------------
        if is_risk_reducing_action(command_line, args):
            return emit_decision("allow", reason="Risk-reducing action / exit authorized.")

        # -------------------------------------------------------------
        # 2. IDENTIFY TARGET ACTION TYPE & ENFORCE SINGLE CHOKE POINT
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
            tool_lower = mcp_tool_name.lower()

            if server_name == "binance":
                if "changeinitialleverage" in tool_lower:
                    is_leverage_command = True
                elif any(kw in tool_lower for kw in ["neworder", "algoorder", "placeorder", "createorder", "newalgoorder"]) or mcp_tool_name in ["futures_usds.newOrder", "futures_coin.newOrder", "spot.newOrder", "margin.marginAccountNewOrder"]:
                    # Direct Binance MCP write tool that can open positions!
                    # Finding 1: Deny direct calls to Binance MCP write tools that can open positions.
                    # Opening orders must be routed exclusively via deploy_futures_trade or execute_futures_trade.py.
                    return emit_decision(
                        "deny",
                        reason=(
                            f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Direct call to Binance MCP write tool '{mcp_tool_name}' "
                            "is strictly forbidden. Opening trades must be routed exclusively through the approved choke point: "
                            "'crypto_radar:deploy_futures_trade' or 'scripts/execute_futures_trade.py' to enforce atomic Stop Loss placement."
                        ),
                        code=2
                    )
            elif server_name == "crypto_radar":
                if mcp_tool_name in ["deploy_futures_trade", "place_order"]:
                    is_trading_command = True

        elif any(tool_name.endswith(sfx) for sfx in [".newOrder", "_newOrder", "newOrder"]):
            # Eagerly loaded Binance MCP order tool called directly
            return emit_decision(
                "deny",
                reason=(
                    f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Direct call to Binance order tool '{tool_name}' "
                    "is strictly forbidden. Opening trades must be routed exclusively through 'crypto_radar:deploy_futures_trade' "
                    "or 'scripts/execute_futures_trade.py'."
                ),
                code=2
            )

        if not is_trading_command and not is_leverage_command:
            return emit_decision("allow")

        base_dir = find_workspace_root()
        target_sym = extract_target_symbol(command_line, args)
        now_ts = int(time.time())

        # Determine Environment safely via env_resolver
        explicit_env = extract_env_argument(command_line, args)
        try:
            is_prod = is_prod_environment(explicit_env, base_dir=base_dir)
        except ValueError as ve:
            return emit_decision("deny", reason=f"🚨 FAIL-CLOSED (Environment Resolution): {str(ve)}", code=2)

        # -------------------------------------------------------------
        # 3. LEVERAGE GATE
        # -------------------------------------------------------------
        if is_leverage_command:
            raw_lev = mcp_args.get("leverage")
            try:
                requested_lev = int(float(raw_lev))
            except Exception:
                return emit_decision(
                    "deny",
                    reason=f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): Invalid leverage value ({raw_lev}).",
                    code=2
                )

            allowed, reason = check_leverage_gate(target_sym, requested_lev, base_dir)
            if allowed:
                return emit_decision("allow", reason=reason)
            else:
                return emit_decision("deny", reason=reason, code=2)

        # -------------------------------------------------------------
        # 4. PRODUCTION INVARIANT: NO GATE BYPASSES IN PROD
        # -------------------------------------------------------------
        has_bypass_eval = ("--bypass-eval-gate" in command_line or args.get("bypass_eval_gate") is True or mcp_args.get("bypass_eval_gate") is True)
        has_bypass_delta = ("--bypass-delta-gate" in command_line or args.get("bypass_delta_gate") is True or mcp_args.get("bypass_delta_gate") is True)

        if is_prod and (has_bypass_eval or has_bypass_delta):
            return emit_decision(
                "deny",
                reason=(
                    "🚨 PROD INVARIANT VIOLATION: Gate bypasses (--bypass-eval-gate, --bypass-delta-gate) "
                    "are strictly FORBIDDEN in PROD environment."
                ),
                code=2
            )

        if is_prod:
            has_bypass_eval = False
            has_bypass_delta = False

        # -------------------------------------------------------------
        # 5. GATE 1: MANDATORY CLEAN-ROOM EVALUATOR (HARNESS GATE)
        # -------------------------------------------------------------
        dossier_file = os.path.join(base_dir, "logs", "evaluations", "latest_dossier.json")

        if not has_bypass_eval:
            if not os.path.exists(dossier_file):
                return emit_decision(
                    "deny",
                    reason=(
                        "🚨 ACTION BLOCKED BY PRE-TOOL-USE HOOK (Clean-Room Evaluator Required):\n"
                        "No evaluation dossier exists at logs/evaluations/latest_dossier.json.\n"
                        "Executing orders directly in primary chat without prior clean-room evaluation is STRICTLY PROHIBITED.\n"
                        "👉 Invoke subagent 'isolated_market_evaluator' via invoke_subagent."
                    ),
                    code=2
                )

            try:
                with open(dossier_file, "r", encoding="utf-8") as f:
                    dossier_data = json.load(f)
            except Exception as e:
                return emit_decision(
                    "deny",
                    reason=f"🚨 FAIL-CLOSED: Failed to read evaluation dossier ({str(e)}).",
                    code=2
                )

            is_valid_dossier, dossier_reason, _ = validate_dossier(dossier_data, now_ts)
            if not is_valid_dossier:
                return emit_decision(
                    "deny",
                    reason=(
                        f"🚨 ACTION BLOCKED BY PRE-TOOL-USE HOOK (Clean-Room Evaluator Required):\n"
                        f"{dossier_reason}\n"
                        "Only with a fresh, valid 'APPROVED' dossier (< 20 min) may execution proceed."
                    ),
                    code=2
                )

            if not target_sym:
                return emit_decision(
                    "deny",
                    reason="🚨 FAIL-CLOSED: No fue posible extraer determinísticamente el símbolo objetivo de la orden.",
                    code=2
                )

            approved_symbols = [s.upper() for s in dossier_data.get("approved_symbols", [])]
            if target_sym not in approved_symbols:
                return emit_decision(
                    "deny",
                    reason=(
                        f"🚨 ACTION BLOCKED BY PRE-TOOL-USE HOOK:\n"
                        f"Asset '{target_sym}' was NOT approved in the evaluator dossier ({dossier_data.get('evaluator_agent', 'isolated_market_evaluator')}).\n"
                        f"Approved assets: {', '.join(approved_symbols) if approved_symbols else 'NONE'}."
                    ),
                    code=2
                )

        # -------------------------------------------------------------
        # 6. GATE 2: DELTA-NEUTRAL & SESSION STATE AUDIT
        # -------------------------------------------------------------
        if not has_bypass_delta:
            state_file = os.path.join(base_dir, "logs", "session_state.json")
            if not os.path.exists(state_file):
                return emit_decision(
                    "deny",
                    reason="🚨 FAIL-CLOSED: session_state.json no existe. Imposible auditar delta de la cartera antes de ejecutar orden.",
                    code=2
                )

            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception as e:
                return emit_decision(
                    "deny",
                    reason=f"🚨 FAIL-CLOSED: Error crítico al leer session_state.json ({str(e)}). Orden bloqueada.",
                    code=2
                )

            if not isinstance(state, dict):
                return emit_decision(
                    "deny",
                    reason="🚨 FAIL-CLOSED: session_state.json no es un objeto JSON válido. Orden bloqueada.",
                    code=2
                )

            # Finding 6: session_state validity check
            if state.get("is_valid") is False or "error" in state:
                return emit_decision(
                    "deny",
                    reason=f"🚨 FAIL-CLOSED: session_state.json está marcado como INVÁLIDO ({state.get('error', 'Error en sincronización con Binance')}). Orden bloqueada.",
                    code=2
                )

            # In PROD, is_valid must be explicitly True
            if is_prod and state.get("is_valid") is not True:
                return emit_decision(
                    "deny",
                    reason="🚨 FAIL-CLOSED: session_state.json no contiene 'is_valid': true requerido para operar en PROD. Orden bloqueada.",
                    code=2
                )

            # Finding 6: Staleness check (max 300s = 5m)
            last_updated_ts = state.get("last_updated_ts", 0)
            try:
                last_updated_ts = int(last_updated_ts)
            except Exception:
                last_updated_ts = 0

            age_seconds = now_ts - last_updated_ts if last_updated_ts > 0 else (now_ts - int(os.path.getmtime(state_file)))
            if is_prod and (last_updated_ts <= 0 or age_seconds > 300):
                return emit_decision(
                    "deny",
                    reason=f"🚨 FAIL-CLOSED: session_state.json está OBSOLETO ({age_seconds}s > 300s límite en PROD). Ejecute 'python3 scripts/sync_session_state.py' antes de operar.",
                    code=2
                )
            elif age_seconds > 300:
                return emit_decision(
                    "deny",
                    reason=f"🚨 FAIL-CLOSED: session_state.json está OBSOLETO ({age_seconds}s > 300s). Re-sincronice el estado de sesión.",
                    code=2
                )

            # Finding 1: Strict Direction Parsing
            trade_dir, dir_err = parse_trade_direction(command_line, args)
            if not trade_dir:
                return emit_decision(
                    "deny",
                    reason=f"🚨 FAIL-CLOSED (Direction Gate): {dir_err} Orden bloqueada.",
                    code=2
                )

            portfolio = state.get("portfolio_exposure", {})
            delta_bias = portfolio.get("delta_bias", "NEUTRAL")

            if delta_bias == "LONG_HEAVY" and trade_dir == "LONG":
                return emit_decision(
                    "deny",
                    reason=(
                        "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Delta-Neutral Hard Gate): "
                        f"Portfolio is bullishly unbalanced (Delta: +${portfolio.get('net_notional_delta_usdt', 0):.2f} USDT / LONG_HEAVY). "
                        "Opening additional Longs without Short hedging is strictly prohibited."
                    ),
                    code=2
                )
            elif delta_bias == "SHORT_HEAVY" and trade_dir == "SHORT":
                return emit_decision(
                    "deny",
                    reason=(
                        "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Delta-Neutral Hard Gate): "
                        f"Portfolio is bearishly unbalanced (Delta: -${abs(portfolio.get('net_notional_delta_usdt', 0)):.2f} USDT / SHORT_HEAVY). "
                        "Opening additional Shorts without Long hedging is strictly prohibited."
                    ),
                    code=2
                )

        # All gates passed successfully
        return emit_decision("allow", reason="Mechanical hard gates and subagent validation PASSED successfully.")

    except Exception as e:
        sys.stderr.write(f"[PRE-TRADE-GUARD INTERNAL ERROR] {str(e)}\n")
        return emit_decision(
            "deny",
            reason=f"🚨 FAIL-CLOSED: Pre-trade guard internal error ({str(e)}). Cannot verify safety — order blocked.",
            code=2
        )


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code if exit_code is not None else 0)
