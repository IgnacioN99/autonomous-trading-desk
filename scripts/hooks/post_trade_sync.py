#!/usr/bin/env python3
"""
post_trade_sync.py - PostToolUse Hook for deterministic portfolio synchronization.
Reactive local state update following each exchange execution.

If an executed command mutated trading positions (open, close, or break-even ratchet),
this hook runs `sync_session_state.py` to guarantee that
Ground Truth (session_state.json) remains permanently fresh.
If an opening order was placed, it immediately runs an orphan audit with auto-heal.

Contract:
  Input (stdin): JSON with step metadata.
  Output (stdout): {}
"""

import os
import sys
import json
import subprocess

def find_workspace_root() -> str:
    p = os.path.abspath(__file__)
    while p and p != os.path.dirname(p):
        p = os.path.dirname(p)
        if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "logs")):
            return p
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def is_opening_mcp_order(mcp_args: dict) -> bool:
    reduce_only = mcp_args.get("reduceOnly", mcp_args.get("reduce_only"))
    close_pos = mcp_args.get("closePosition", mcp_args.get("close_position"))
    if reduce_only is True or str(reduce_only).lower() == "true":
        return False
    if close_pos is True or str(close_pos).lower() == "true":
        return False
    return True

def handle_post_trade_sync(payload: dict) -> dict:
    """
    Inspects toolCall payload and executes necessary sync / audit operations.
    Returns status dict for observability and testing.
    """
    tool_call = payload.get("toolCall", {})
    tool_name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    command_line = args.get("CommandLine", "")

    order_placed = False
    is_opening = False

    if tool_name == "call_mcp_tool":
        server_name = args.get("ServerName", "")
        mcp_tool_name = args.get("ToolName", "")
        raw_args = args.get("Arguments", {})
        if isinstance(raw_args, str):
            try:
                mcp_args = json.loads(raw_args)
            except Exception:
                mcp_args = {}
        elif isinstance(raw_args, dict):
            mcp_args = raw_args
        else:
            mcp_args = {}

        if server_name == "binance" and mcp_tool_name in ["futures_usds.newOrder", "margin.marginAccountNewOrder"]:
            order_placed = True
            is_opening = is_opening_mcp_order(mcp_args)
        elif server_name == "crypto_radar":
            order_placed = True
            if mcp_tool_name in ["deploy_futures_trade", "place_order"]:
                is_opening = True

    elif tool_name == "run_command":
        trading_keywords = [
            "execute_futures_trade",
            "close_position_market",
            "move_sl_to_breakeven",
            "deploy_futures_trade",
            "night_cutoff_loop"
        ]
        if any(kw in command_line for kw in trading_keywords):
            order_placed = True
            if "execute_futures_trade.py" in command_line and "--close" not in command_line and "close_position_market" not in command_line:
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
    target_env = os.environ.get("BINANCE_API_ENV", "testnet").lower()

    # 1. Trigger session state sync
    sync_script = os.path.join(base_dir, "scripts", "sync_session_state.py")
    if os.path.exists(sync_script):
        try:
            subprocess.run([sys.executable, sync_script, target_env], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            result["synced"] = True
        except Exception as e:
            sys.stderr.write(f"[POST-TRADE-SYNC ERROR] Sync failed: {e}\n")

    # 2. Run orphan audit with auto-heal if opening order
    if is_opening:
        try:
            scripts_dir = os.path.join(base_dir, "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import execute_futures_trade as eft
            eft.audit_orphan_positions(target_env=target_env, auto_heal=True)
            result["audit_healed"] = True
        except Exception as e:
            sys.stderr.write(f"[POST-TRADE-SYNC AUDIT ERROR] Orphan audit failed: {e}\n")

    return result

def main():
    try:
        raw_input = sys.stdin.read()
        if raw_input.strip():
            payload = json.loads(raw_input)
            handle_post_trade_sync(payload)
    except Exception:
        pass

    # PostToolUse contract expects an empty JSON object on stdout
    print(json.dumps({}))

if __name__ == "__main__":
    main()
