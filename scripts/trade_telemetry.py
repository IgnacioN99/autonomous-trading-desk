#!/usr/bin/env python3
"""
trade_telemetry.py - Telemetría Cuantitativa de Trading: Clasificación de 'Shocks' vs. 'Varianza Normal'.
Clasificación forense de atribución de pérdidas y varianza del modelo.

En finanzas cuantitativas, evaluar un modelo asumiendo que todas las pérdidas provienen de la misma
distribución matemática destruye el Criterio de Kelly. Si una pérdida de -$1.50 ocurre porque BTC
sufrió un flash crash de -$4,000 en 15m por un shock regulatorio, NO es un fallo de la estrategia técnica.

Este módulo clasifica cada cierre y evento en:
- NORMAL_VARIANCE: Pérdida o ganancia dentro del comportamiento estocástico normal del activo.
- MACRO_SHOCK: Evento exógeno extraordinario (liquidaciones en cascada de BTC, listing de CME, exploit).
- EXECUTION_BOUNCE: Fallo de fricción o rechazo de ejecución (slippage gap, minNotional).

Uso:
  python3 scripts/trade_telemetry.py record-exit --symbol TRXUSDT --pnl -1.05 --exit-type SL --class NORMAL_VARIANCE --note "Tocado SL por debilidad local"
  python3 scripts/trade_telemetry.py record-shock --type BTC_LIQUIDATION_CASCADE --note "BTC cayó de $86.2k a $83.8k en 45m"
  python3 scripts/trade_telemetry.py summary
"""

import os
import sys
import time
import json
import datetime
import argparse
from typing import Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.atomic_writer import atomic_append_jsonl

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
TELEMETRY_FILE = os.path.join(LOGS_DIR, "trade_telemetry.jsonl")

def record_trade_exit(
    symbol: str,
    direction: str,
    pnl_usdt: float,
    exit_type: str,
    classification: str = "NORMAL_VARIANCE",
    strategy: str = "microstructure_wick_reversion",
    note: str = ""
) -> dict:
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    event = {
        "event_type": "TRADE_EXIT",
        "timestamp_utc": now_utc,
        "timestamp_ts": int(time.time()),
        "symbol": symbol.upper(),
        "direction": direction.upper(),
        "pnl_usdt": round(float(pnl_usdt), 4),
        "exit_type": exit_type.upper(),  # TP1, TP2, SL, DEAD_ALPHA, MANUAL
        "classification": classification.upper(),  # NORMAL_VARIANCE, MACRO_SHOCK, EXECUTION_BOUNCE
        "strategy": strategy,
        "note": note.strip()
    }
    atomic_append_jsonl(TELEMETRY_FILE, event)
    print(f"📡 Telemetría registrada: {symbol} [{exit_type}] -> PnL: ${pnl_usdt:+.2f} ({classification})")
    return event

def record_macro_shock(shock_type: str, impacted_symbols: List[str] = None, note: str = "") -> dict:
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    event = {
        "event_type": "MACRO_SHOCK",
        "timestamp_utc": now_utc,
        "timestamp_ts": int(time.time()),
        "shock_type": shock_type.upper(),
        "impacted_symbols": [s.upper() for s in (impacted_symbols or [])],
        "note": note.strip()
    }
    atomic_append_jsonl(TELEMETRY_FILE, event)
    print(f"🚨 Macro Shock registrado: {shock_type} — {note}")
    return event

def generate_telemetry_summary():
    if not os.path.exists(TELEMETRY_FILE):
        print("ℹ️  No hay registros de telemetría aún.")
        return

    exits = []
    shocks = []
    with open(TELEMETRY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    obj = json.loads(line)
                    if obj.get("event_type") == "TRADE_EXIT":
                        exits.append(obj)
                    elif obj.get("event_type") == "MACRO_SHOCK":
                        shocks.append(obj)
                except Exception:
                    continue

    normal_exits = [e for e in exits if e.get("classification") == "NORMAL_VARIANCE"]
    shock_exits = [e for e in exits if e.get("classification") == "MACRO_SHOCK"]

    print("\n" + "=" * 70)
    print("📊 REPORTE DE TELEMETRÍA CUANTITATIVA (Shocks vs Varianza)")
    print("=" * 70)
    print(f"Total Operaciones Registradas: {len(exits)}")
    print(f"• Varianza Normal: {len(normal_exits)} trades | PnL acumulado: ${sum(e['pnl_usdt'] for e in normal_exits):+.2f} USDT")
    print(f"• Macro Shocks Exógenos: {len(shock_exits)} trades | PnL afectado: ${sum(e['pnl_usdt'] for e in shock_exits):+.2f} USDT")
    print(f"• Macro Shocks del Sistema: {len(shocks)} eventos")

    if normal_exits:
        normal_wins = [e for e in normal_exits if e["pnl_usdt"] > 0]
        normal_wr = (len(normal_wins) / len(normal_exits)) * 100
        print(f"🏆 Win Rate Puro (Aislado de Shocks): {normal_wr:.1f}%")

    print("=" * 70 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trade Telemetry System")
    subparsers = parser.add_subparsers(dest="cmd")

    # exit
    p_exit = subparsers.add_parser("record-exit")
    p_exit.add_argument("--symbol", required=True)
    p_exit.add_argument("--dir", default="LONG")
    p_exit.add_argument("--pnl", type=float, required=True)
    p_exit.add_argument("--exit-type", default="SL", choices=["TP1", "TP2", "SL", "DEAD_ALPHA", "MANUAL"])
    p_exit.add_argument("--class", dest="classification", default="NORMAL_VARIANCE", choices=["NORMAL_VARIANCE", "MACRO_SHOCK", "EXECUTION_BOUNCE"])
    p_exit.add_argument("--strategy", default="microstructure_wick_reversion")
    p_exit.add_argument("--note", default="")

    # shock
    p_shock = subparsers.add_parser("record-shock")
    p_shock.add_argument("--type", required=True)
    p_shock.add_argument("--symbols", default="")
    p_shock.add_argument("--note", default="")

    # summary
    subparsers.add_parser("summary")

    args = parser.parse_args()

    if args.cmd == "record-exit":
        record_trade_exit(args.symbol, args.dir, args.pnl, args.exit_type, args.classification, args.strategy, args.note)
    elif args.cmd == "record-shock":
        symbols_list = [s.strip() for s in args.symbols.split(",") if s.strip()]
        record_macro_shock(args.type, symbols_list, args.note)
    else:
        generate_telemetry_summary()
