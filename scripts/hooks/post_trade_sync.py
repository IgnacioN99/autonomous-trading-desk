#!/usr/bin/env python3
"""
post_trade_sync.py - PostToolUse Hook for deterministic portfolio synchronization.
Reactive local state update following each exchange execution.

If an executed command mutated trading positions (open, close, or break-even ratchet),
this hook runs `sync_session_state.py` to guarantee that
Ground Truth (session_state.json) remains permanently fresh.
If an opening order was placed, it immediately runs an orphan audit with auto-heal.

Recognized tool calls (Antigravity and Claude Code payloads):
  * run_command / Bash invoking the sanctioned trading scripts.
  * call_mcp_tool, mcp_tool, mcp_<server>_<tool>, mcp__<server>__<tool>:
      - crypto_radar trading/position tools (scans, newsletters and sizing helpers are ignored);
      - Binance write tools (anything outside the read-only allowlist), including calls wrapped
        in the gateway meta-tool 'tool_execute', placeMultipleOrders and newAlgoOrder.

Contract:
  Input (stdin): JSON with step metadata.
  Output (stdout): {} — always, with exit code 0. Total runtime is bounded well below the
  30s hook timeout (sync <= SYNC_TIMEOUT_S, orphan audit <= AUDIT_TIMEOUT_S).
"""

import os
import re
import sys
import json
import threading
import subprocess

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

SYNC_TIMEOUT_S = 10
AUDIT_TIMEOUT_S = 12

RADAR_OPENING_TOOLS = {"deploy_futures_trade", "place_order"}
RADAR_POSITION_TOOLS = RADAR_OPENING_TOOLS | {
    "move_to_breakeven", "move_sl_to_breakeven", "close_position_market", "close_position",
    "update_trailing_stop_structural", "audit_and_trail_all_positions",
}
BINANCE_OPENING_OPS = {"neworder", "newalgoorder", "placemultipleorders", "marginaccountneworder",
                       "modifyorder", "modifymultipleorders"}

TRADING_SCRIPT_KEYWORDS = [
    "execute_futures_trade",
    "close_position_market",
    "move_sl_to_breakeven",
    "deploy_futures_trade",
    "night_cutoff_loop",
    "dynamic_exit_manager",
]

try:
    import pre_trade_guard as _guard
except Exception:  # pragma: no cover - fall back to a conservative local classifier
    _guard = None


def find_workspace_root() -> str:
    p = os.path.abspath(__file__)
    while p and p != os.path.dirname(p):
        p = os.path.dirname(p)
        if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "logs")):
            return p
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _decode_value(value):
    for _ in range(3):
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped or stripped[0] not in "\"{[":
            return value
        try:
            value = json.loads(stripped)
        except ValueError:
            return value
    return value


def _decode_dict(value) -> dict:
    value = _decode_value(value)
    return value if isinstance(value, dict) else {}


def _is_true(value) -> bool:
    value = _decode_value(value)
    return value is True or str(value).strip().lower() == "true"


def is_opening_mcp_order(mcp_args: dict) -> bool:
    if _is_true(mcp_args.get("reduceOnly", mcp_args.get("reduce_only"))):
        return False
    if _is_true(mcp_args.get("closePosition", mcp_args.get("close_position"))):
        return False
    batch = _decode_value(mcp_args.get("batchOrders", mcp_args.get("batch_orders")))
    if isinstance(batch, list) and batch:
        return any(isinstance(o, dict) and is_opening_mcp_order(o) for o in batch)
    return True


def _normalize(payload: dict) -> dict:
    """Returns {kind, command, server, tool, args} for agy or Claude Code payloads."""
    if _guard is not None:
        try:
            call = _guard.normalize_tool_call(payload)
            return {"kind": call["kind"], "command": call["command"], "server": call["server"],
                    "tool": call["mcp_tool"], "args": call["mcp_args"] or {}}
        except Exception:
            pass

    out = {"kind": "other", "command": "", "server": "", "tool": "", "args": {}}
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else None
    if tool_call is not None:
        name = tool_call.get("name", "")
        args = tool_call.get("args", {}) if isinstance(tool_call.get("args"), dict) else {}
        if name == "run_command":
            out.update(kind="run_command", command=str(_decode_value(args.get("CommandLine", "")) or ""))
        elif name in ("call_mcp_tool", "mcp_tool"):
            out.update(kind="mcp", server=str(_decode_value(args.get("ServerName", "")) or ""),
                       tool=str(_decode_value(args.get("ToolName", "")) or ""),
                       args=_decode_dict(args.get("Arguments", {})))
        elif name.startswith("mcp_"):
            rest = name[4:]
            server, _, tool = rest.partition("_")
            for known in ("crypto_radar", "binance"):
                if rest.startswith(known + "_"):
                    server, tool = known, rest[len(known) + 1:]
            out.update(kind="mcp", server=server, tool=tool, args=_decode_dict(args.get("Arguments", args)))
        return out

    name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) if isinstance(payload.get("tool_input"), dict) else {}
    if name == "Bash":
        out.update(kind="run_command", command=str(tool_input.get("command", "")))
    elif isinstance(name, str) and name.startswith("mcp__"):
        parts = name.split("__")
        out.update(kind="mcp", server=parts[1] if len(parts) > 1 else "",
                   tool="__".join(parts[2:]), args=tool_input)
    return out


def classify_binance_call(tool: str, mcp_args: dict):
    """Returns (order_placed, is_opening) for a Binance MCP tool call."""
    if _guard is not None:
        verdict, _reason, info = _guard.evaluate_binance_tool(tool, mcp_args)
        if verdict == "read_only":
            return False, False
        inner_tool = info.get("tool", tool)
        inner_args = info.get("args") or {}
        op = (info.get("operation") or str(inner_tool).rsplit(".", 1)[-1]).lower()
        if verdict == "risk_reducing" or verdict == "leverage":
            return True, False
        return True, op in BINANCE_OPENING_OPS and is_opening_mcp_order(inner_args)

    # Conservative fallback when the guard module cannot be imported
    name = tool
    args = mcp_args
    if name == "tool_execute":
        name = str(_decode_value(args.get("toolName", "")) or "")
        args = _decode_dict(args.get("arguments", {}))
    op = name.rsplit(".", 1)[-1].lower()
    if op in BINANCE_OPENING_OPS:
        return True, is_opening_mcp_order(args)
    if "cancel" in op or op.startswith("delete") or op in ("changeinitialleverage", "changemargintype"):
        return True, False
    return False, False


def handle_post_trade_sync(payload: dict) -> dict:
    """
    Inspects toolCall payload and executes necessary sync / audit operations.
    Returns status dict for observability and testing.
    """
    call = _normalize(payload if isinstance(payload, dict) else {})
    command_line = call["command"] or ""
    mcp_args = call["args"] if isinstance(call["args"], dict) else {}

    order_placed = False
    is_opening = False

    if call["kind"] == "mcp":
        server = (call["server"] or "").lower().replace("-", "_")
        tool = call["tool"] or ""
        is_binance = "binance" in server or (_guard is not None and _guard.is_binance_call("", tool))
        if tool == "tool_execute" and _guard is not None:
            inner = str(_decode_value(mcp_args.get("toolName", "")) or "")
            is_binance = is_binance or _guard.is_binance_call("", inner)

        if is_binance:
            order_placed, is_opening = classify_binance_call(tool, mcp_args)
        elif server == "crypto_radar" or tool in RADAR_POSITION_TOOLS:
            if tool in RADAR_POSITION_TOOLS:
                order_placed = True
                is_opening = tool in RADAR_OPENING_TOOLS
            elif tool == "audit_orphan_positions" and _is_true(mcp_args.get("auto_heal")):
                order_placed = True

    elif call["kind"] == "run_command":
        if any(kw in command_line for kw in TRADING_SCRIPT_KEYWORDS) or re.search(r"\bdeploy_[A-Za-z0-9_]+\.py\b", command_line):
            order_placed = True
            if (("execute_futures_trade" in command_line or re.search(r"\bdeploy_[A-Za-z0-9_]+\.py\b", command_line))
                    and not re.search(r"--(?:close[-_]position|audit[-_]orphans|auto[-_]heal|help)\b", command_line)
                    and "close_position_market" not in command_line):
                is_opening = True

    result = {
        "order_placed": order_placed,
        "is_opening": is_opening,
        "synced": False,
        "audit_healed": False
    }

    if not order_placed:
        return result

    base_dir = find_workspace_root()
    scripts_dir = os.path.join(base_dir, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    target_env = None
    if isinstance(mcp_args, dict):
        target_env = mcp_args.get("target_env") or mcp_args.get("env")
    if not target_env and command_line:
        m = re.search(r"--env(?:\s+|=)(['\"]?)([A-Za-z0-9_-]+)\1", command_line, re.IGNORECASE)
        if m:
            target_env = m.group(2)
    if not target_env:
        try:
            from utils.env_resolver import resolve_env
            target_env = resolve_env(base_dir=base_dir)
        except Exception:
            target_env = os.environ.get("BINANCE_API_ENV", "prod").lower()
    target_env = str(target_env).lower()

    # 1. Trigger session state sync (bounded)
    sync_script = os.path.join(base_dir, "scripts", "sync_session_state.py")
    if os.path.exists(sync_script):
        try:
            subprocess.run([sys.executable, sync_script, "--env", target_env], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=SYNC_TIMEOUT_S)
            result["synced"] = True
        except Exception as e:
            sys.stderr.write(f"[POST-TRADE-SYNC ERROR] Sync failed: {e}\n")

    # 2. Run orphan audit with auto-heal if opening order (bounded, daemon thread)
    if is_opening:
        outcome = {}

        def _audit():
            try:
                import execute_futures_trade as eft
                eft.audit_orphan_positions(target_env=target_env, auto_heal=True)
                outcome["ok"] = True
            except Exception as e:  # pragma: no cover - logged only
                outcome["error"] = str(e)

        worker = threading.Thread(target=_audit, name="post-trade-orphan-audit", daemon=True)
        worker.start()
        worker.join(AUDIT_TIMEOUT_S)
        if outcome.get("ok"):
            result["audit_healed"] = True
        elif worker.is_alive():
            sys.stderr.write(f"[POST-TRADE-SYNC AUDIT ERROR] Orphan audit exceeded {AUDIT_TIMEOUT_S}s budget.\n")
        else:
            sys.stderr.write(f"[POST-TRADE-SYNC AUDIT ERROR] Orphan audit failed: {outcome.get('error')}\n")

    return result


def main():
    try:
        raw_input = sys.stdin.read()
        if raw_input.strip():
            payload = json.loads(raw_input)
            if isinstance(payload, dict):
                handle_post_trade_sync(payload)
    except Exception as e:
        sys.stderr.write(f"[POST-TRADE-SYNC ERROR] {e}\n")

    # PostToolUse contract expects an empty JSON object on stdout
    print(json.dumps({}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    main()
    # Exit immediately even if a bounded audit thread is still running.
    os._exit(0)
