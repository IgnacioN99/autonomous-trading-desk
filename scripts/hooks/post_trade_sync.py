#!/usr/bin/env python3
"""
post_trade_sync.py - PostToolUse Hook for deterministic portfolio synchronization.
Reactive local state update following each exchange execution.

If an executed command mutated trading positions (open, close, or break-even ratchet),
this hook asynchronously runs `sync_session_state.py` to guarantee that
Ground Truth (session_state.json) remains permanently fresh.

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

def main():
    try:
        raw_input = sys.stdin.read()
        if raw_input.strip():
            payload = json.loads(raw_input)
            tool_call = payload.get("toolCall", {})
            args = tool_call.get("args", {})
            command_line = args.get("CommandLine", "")

            trading_keywords = [
                "execute_futures_trade",
                "close_position_market",
                "move_sl_to_breakeven",
                "deploy_futures_trade",
                "night_cutoff_loop"
            ]

            # Trigger sync only if command altered trading positions
            if any(kw in command_line for kw in trading_keywords):
                base_dir = find_workspace_root()
                sync_script = os.path.join(base_dir, "scripts", "sync_session_state.py")
                subprocess.run([sys.executable, sync_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)

    except Exception:
        pass

    # PostToolUse contract expects an empty JSON object on stdout
    print(json.dumps({}))

if __name__ == "__main__":
    main()
