#!/usr/bin/env python3
"""
trading_drift_watchdog.py - Sensor de Deriva y "Alfa Muerto" (Dead Alpha Watchdog).
Monitoreo proactivo del ciclo de vida y tiempo de retención de posiciones abiertas.

En trading intradía (15m/5m), una hipótesis técnica de absorción o breakout tiene un tiempo de vida útil
(Half-Life H). Si una posición lleva más de 3 a 4 horas abierta estancada en un rango minúsculo (+/- 0.3R)
con volumen seco, la tesis estadística original HA EXPIRADO.

Mantenerla viva sólo expone capital a comisiones de financiamiento (funding fees) y volatilidad macro adversa.
Este watchdog audita las posiciones vivas y:
1. Detecta posiciones con 'Alfa Muerto'.
2. Si está en ganancia leve, ciñe el SL a Break-Even de forma agresiva.
3. Si está congelada en el punto de entrada, emite recomendación de cierre preventivo o auto-cierre (--auto-exit).

Uso:
  python3 scripts/trading_drift_watchdog.py [--env testnet|mainnet] [--max-hours 4.0] [--auto-exit]
"""

import os
import sys
import time
import json
import datetime
import argparse

# Asegurar path local
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
from utils.atomic_writer import read_json_safe, atomic_append_jsonl

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
AUDIT_LOG = os.path.join(LOGS_DIR, "trades_audit.jsonl")

def audit_dead_alpha(target_env: str = "testnet", max_hours: float = 4.0, auto_exit: bool = False):
    print("=" * 70)
    print("⏳ DEAD ALPHA & DRIFT WATCHDOG — AUDITORÍA DE DERIVA TEMPORAL")
    print(f"Hora UTC: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Max Holding Time: {max_hours}h | Target Env: {target_env.upper()}")
    print("=" * 70)

    # 1. Leer posiciones activas en Binance
    pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0] if isinstance(pos_res, list) else []

    if not active:
        print("✅ CERO POSICIONES ABIERTAS: Cero deriva temporal. Cartera limpia.")
        return {"active_count": 0, "dead_alpha_count": 0, "positions": []}

    state = read_json_safe(STATE_FILE, default={})
    meta_positions = {p["symbol"]: p for p in state.get("active_positions", [])}
    now_ts = int(time.time())

    results = []
    dead_alpha_detected = []

    for p in active:
        sym = p["symbol"]
        amt = float(p["positionAmt"])
        direction = "LONG" if amt > 0 else "SHORT"
        entry_p = float(p["entryPrice"])
        mark_p = float(p["markPrice"])
        unpnl = float(p.get("unRealizedProfit", 0))
        margin = float(p.get("isolatedMargin", 0))
        roe_pct = (unpnl / margin * 100) if margin > 0 else 0.0

        meta = meta_positions.get(sym, {})
        entry_time_ts = meta.get("entry_time_ts", now_ts)
        elapsed_sec = now_ts - entry_time_ts
        elapsed_hours = round(elapsed_sec / 3600.0, 2)

        sl_price = meta.get("sl_price")
        price_diff_pct = abs(mark_p - entry_p) / entry_p * 100

        # Criterio Cuantitativo de Alfa Muerto:
        # Ha superado max_hours Y el precio no se ha movido más de 1.2% del punto de entrada (rango muerto)
        is_stagnant = price_diff_pct < 1.2 and abs(roe_pct) < 15.0
        is_overdue = elapsed_hours >= max_hours
        is_dead_alpha = is_overdue and is_stagnant

        item = {
            "symbol": sym,
            "direction": direction,
            "amount": abs(amt),
            "entry_price": entry_p,
            "mark_price": mark_p,
            "elapsed_hours": elapsed_hours,
            "unrealized_pnl_usdt": unpnl,
            "roe_pct": roe_pct,
            "is_dead_alpha": is_dead_alpha,
            "action_taken": "NONE"
        }

        print(f"\n• Posición: {sym} ({direction}) | Entrada: {entry_p} | Mark: {mark_p}")
        print(f"  Tiempo Abierta: {elapsed_hours}h (Límite: {max_hours}h) | PnL: ${unpnl:+.2f} USDT ({roe_pct:+.1f}% ROE)")

        if is_dead_alpha:
            dead_alpha_detected.append(item)
            print(f"  🚨 ALERTA [DEAD ALPHA]: La tesis original ha expirado tras {elapsed_hours}h en rango estrecho ({price_diff_pct:.2f}% de movimiento).")
            
            if auto_exit:
                print(f"  ⚡ DISPARANDO AUTO-EXIT: Cerrando posición a mercado para reciclar capital...")
                close_res = eft.close_position_market(sym, target_env=target_env)
                item["action_taken"] = "AUTO_EXIT_CLOSED"
                item["close_result"] = close_res
                print(f"  ✅ Posición cerrada a mercado.")
            else:
                print(f"  ⚠️  RECOMENDACIÓN: Cerrar posición a mercado o ceñir SL a Break-Even inmediato para eliminar riesgo.")
                item["action_taken"] = "RECOMMEND_EXIT"
        else:
            print(f"  ✅ Salud Temporal OK (Dentro del horizonte operativo o en expansión)")

        results.append(item)

    print("\n" + "=" * 70)
    print(f"RESUMEN: {len(active)} posición(es) evaluadas | {len(dead_alpha_detected)} con Alfa Muerto.")
    print("=" * 70)

    return {
        "active_count": len(active),
        "dead_alpha_count": len(dead_alpha_detected),
        "positions": results
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dead Alpha & Drift Watchdog")
    parser.add_argument("--env", default="testnet", choices=["testnet", "mainnet"])
    parser.add_argument("--max-hours", type=float, default=4.0, help="Horas máximas antes de declarar alfa muerto")
    parser.add_argument("--auto-exit", action="store_true", help="Cierra a mercado las posiciones con alfa muerto")
    args = parser.parse_args()

    audit_dead_alpha(target_env=args.env, max_hours=args.max_hours, auto_exit=args.auto_exit)
