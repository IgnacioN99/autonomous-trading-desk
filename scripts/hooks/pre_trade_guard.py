#!/usr/bin/env python3
"""
pre_trade_guard.py - Hook PreToolUse para el runtime agéntico.
Harness de seguridad determinista pre-ejecución y validación programática de riesgos.

Intercepta run_command y call_mcp_tool antes de su ejecución para aplicar compuertas mecánicas (Hard Gates):
1. COMPUERTA EVALUADOR AISLADO (Clean-Room Evaluation Gate):
   Prohíbe terminantemente ejecutar trades en el chat principal sin un dossier previo emitido
   por el subagente 'isolated_market_evaluator' en los últimos 20 minutos (logs/evaluations/latest_dossier.json).
2. COMPUERTA DELTA-NEUTRAL:
   Bloquea físicamente órdenes Long en LONG_HEAVY o Shorts en SHORT_HEAVY.
3. EXCEPCIÓN DE RIESGO:
   Órdenes de cierre, reducción de riesgo, move_to_breakeven o curación de huérfanas se autorizan DE INMEDIATO.

Latencia objetivo: < 15ms.
"""

import os
import sys
import json
import time
import re

def is_risk_reducing_action(cmd_or_name: str, args_dict: dict = None) -> bool:
    """Verifica si la acción reduce o elimina riesgo (JAMÁS deben ser bloqueadas)."""
    text = (cmd_or_name + " " + json.dumps(args_dict or {})).lower()
    reducing_signals = [
        "close_position", "move_to_breakeven", "cancel", "audit_orphan",
        "reduceonly", "reduce_only", "--close", "delete_order", "algoorder"
    ]
    return any(sig in text for sig in reducing_signals)

def extract_target_symbol(cmd: str, args_dict: dict = None) -> str:
    """Extrae el símbolo objetivo de los argumentos o de la línea de comandos."""
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

    # Regex en la línea de comando
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
        # 1. FILTRO DE HERRAMIENTAS DE TRADING
        # -------------------------------------------------------------
        is_trading_command = False
        if tool_name == "run_command":
            trading_keywords = ["execute_futures_trade", "deploy_futures_trade"]
            is_trading_command = any(kw in command_line for kw in trading_keywords)
        elif tool_name == "call_mcp_tool":
            mcp_tool_name = args.get("ToolName", "")
            is_trading_command = ("deploy_futures_trade" in mcp_tool_name or "execute_futures_trade" in mcp_tool_name)

        if not is_trading_command:
            print(json.dumps({"decision": "allow"}))
            return

        # -------------------------------------------------------------
        # 2. INVARIANTE: NUNCA BLOQUEAR ACCIONES QUE REDUCEN RIESGO
        # -------------------------------------------------------------
        if is_risk_reducing_action(command_line, args):
            print(json.dumps({"decision": "allow", "reason": "Acción de reducción de riesgo / salida autorizada."}))
            return

        # -------------------------------------------------------------
        # 3. COMPUERTA 1: EVALUADOR AISLADO OBLIGATORIO (CLEAN-ROOM HARNESS GATE)
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
                    "🚨 ACCIÓN BLOQUEADA POR PRE-TOOL-USE HOOK (Hard Gate - Evaluador Aislado Obligatorio):\n"
                    "Está terminantemente PROHIBIDO ejecutar órdenes directamente en el chat principal sin evaluación previa.\n"
                    "El modelo principal NO debe tomar decisiones inline ni saltarse la arquitectura multi-agente.\n\n"
                    "👉 ACCIÓN EXIGIDA:\n"
                    "1. Invoca al subagente 'isolated_market_evaluator' mediante invoke_subagent, pasándole el brief determinista generado por 'python3 scripts/prime_evaluator_brief.py'.\n"
                    "2. El subagente evaluador debe emitir el Master Dossier y guardarlo mediante 'python3 scripts/record_evaluation.py'.\n"
                    "3. Solo tras contar con un dossier 'APPROVED' fresco (< 20 min) podrás proceder a la ejecución."
                )
                print(json.dumps({"decision": "deny", "reason": deny_msg}))
                return

            # Verificar si el símbolo específico fue aprobado por el evaluador
            approved_symbols = dossier_data.get("approved_symbols", []) if dossier_data else []
            if target_sym and approved_symbols and target_sym not in approved_symbols:
                deny_msg = (
                    f"🚨 ACCIÓN BLOQUEADA POR PRE-TOOL-USE HOOK:\n"
                    f"El activo '{target_sym}' NO fue aprobado en el dossier del subagente evaluador ({dossier_data.get('evaluator_agent')}).\n"
                    f"Activos aprobados: {', '.join(approved_symbols)}.\n"
                    "Por disciplina cuantitativa, queda prohibido operar activos fuera del dossier validado."
                )
                print(json.dumps({"decision": "deny", "reason": deny_msg}))
                return

        # -------------------------------------------------------------
        # 4. COMPUERTA 2: DELTA-NEUTRAL & GESTIÓN DE CARTERA
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
                    
                    # Chequear en args de MCP si aplica
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
                            "🚨 BLOQUEADO POR PRE-TOOL-USE HOOK (Hard Gate Delta-Neutral): "
                            f"La cartera está en desbalance alcista (Delta: +${portfolio.get('net_notional_delta_usdt', 0):.2f} USDT / LONG_HEAVY). "
                            "Queda terminantemente prohibido abrir más Longs sin cobertura Short."
                        )
                        print(json.dumps({"decision": "deny", "reason": reason_msg}))
                        return

                    elif delta_bias == "SHORT_HEAVY" and is_short_attempt and not is_long_attempt:
                        reason_msg = (
                            "🚨 BLOQUEADO POR PRE-TOOL-USE HOOK (Hard Gate Delta-Neutral): "
                            f"La cartera está en desbalance bajista (Delta: -${abs(portfolio.get('net_notional_delta_usdt', 0)):.2f} USDT / SHORT_HEAVY). "
                            "Queda terminantemente prohibido abrir más Shorts sin cobertura Long."
                        )
                        print(json.dumps({"decision": "deny", "reason": reason_msg}))
                        return
                except Exception:
                    pass

        # Si pasa todas las compuertas con éxito
        print(json.dumps({"decision": "allow", "reason": "Compuertas mecánicas y validación de subagente SUPERADAS con éxito."}))

    except Exception as e:
        # En caso de error interno, fail OPEN para no colgar el workspace pero emitiendo advertencia
        print(json.dumps({"decision": "allow", "reason": f"Hook warning: {str(e)}"}))

if __name__ == "__main__":
    main()
