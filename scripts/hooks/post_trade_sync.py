#!/usr/bin/env python3
"""
post_trade_sync.py - Hook PostToolUse para sincronización determinista de cartera.
Actualización reactiva del estado local tras cada ejecución en el exchange.

Si un comando ejecutó una operación de trading (apertura, cierre o break-even),
este hook ejecuta en segundo plano `sync_session_state.py` para asegurar que
el Ground Truth (session_state.json) esté permanentemente fresco.

Contract:
  Input (stdin): JSON con metadata del paso.
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

            # Solo sincronizar si el comando ejecutado alteró posiciones de trading
            if any(kw in command_line for kw in trading_keywords):
                base_dir = find_workspace_root()
                sync_script = os.path.join(base_dir, "scripts", "sync_session_state.py")
                # Ejecutar de forma no bloqueante o con timeout rápido
                subprocess.run([sys.executable, sync_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)

    except Exception:
        pass

    # El contrato de PostToolUse siempre espera un objeto JSON vacío en stdout
    print(json.dumps({}))

if __name__ == "__main__":
    main()
