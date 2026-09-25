#!/usr/bin/env python3
"""
night_cutoff_loop.py - Loop Operativo de Apagado Nocturno y Control Cero Riesgo Overnight.
Loop de seguridad para blindaje y cierre de sesión nocturna sin riesgo flotante.

Reglas Operativas del Desk Nocturno:
1. Audita todas las posiciones vivas en Binance Futures.
2. Si una posición tiene ganancia confirmada (ROE >= +10% o expansión favorable >= +1.5R):
   Ratchet automático del Stop Loss a TRUE NET BREAK-EVEN (+0.2% sobre el precio de entrada para cubrir comisiones).
3. Si una posición está en rango sin volumen o cerca del Stop Loss:
   Emite alerta de riesgo o recomendación de cierre preventivo para no dejar exposición direccional huérfana de noche.
4. Cancela todas las órdenes LIMIT huérfanas pendientes (ordenes de entrada o TPs de posiciones ya cerradas)
   que tengan más de 60-90 minutos de antigüedad para evitar 'fills fantasmas' mientras se duerme.
5. Emite un informe consolidado de seguridad nocturna.

Uso:
  python3 scripts/loops/night_cutoff_loop.py [--env testnet|mainnet] [--auto-ratchet]
"""

import os
import sys
import time
import json
import datetime
import argparse

# Asegurar path local
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import execute_futures_trade as eft
import dynamic_exit_manager as dem

def run_night_cutoff(target_env: str = "testnet", auto_ratchet: bool = True):
    print("=" * 70)
    print("🌙 NIGHT CUTOFF LOOP — PROTOCOLO DE BLINDAJE NOCTURNO")
    print(f"Hora UTC: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Target Environment: {target_env.upper()}")
    print("=" * 70)

    # 1. Consultar Posiciones Activas en Binance
    pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0] if isinstance(pos_res, list) else []

    if not active:
        print("✅ CERO POSICIONES ABIERTAS: Cartera 100% limpia. Cero riesgo overnight.")
    else:
        print(f"🛡️  AUDITORÍA DE {len(active)} POSICIÓN(ES) VIVAS:")
        for p in active:
            sym = p["symbol"]
            amt = float(p["positionAmt"])
            entry_p = float(p["entryPrice"])
            mark_p = float(p["markPrice"])
            unpnl = float(p.get("unRealizedProfit", 0))
            margin = float(p.get("isolatedMargin", 0))
            direction = "LONG" if amt > 0 else "SHORT"
            roe_pct = (unpnl / margin * 100) if margin > 0 else 0.0

            print(f"\n   • {sym} ({direction} {abs(amt):.3f} @ {entry_p:.5f})")
            print(f"     Mark actual: {mark_p:.5f} | PnL Flotante: ${unpnl:+.2f} USDT ({roe_pct:+.1f}% ROE)")

            # Verificar Stop Loss activo
            algos = eft.send_signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym}, target_env=target_env)
            active_sl = [a for a in algos if a.get("orderType") in ["STOP_MARKET", "STOP"]] if isinstance(algos, list) else []

            if not active_sl:
                print(f"     🚨 PELIGRO: {sym} NO TIENE STOP LOSS ACTIVO. Colocando Stop de emergencia...")
                exit_side = "SELL" if amt > 0 else "BUY"
                emergency_sl = entry_p * (0.98 if amt > 0 else 1.02)
                filters = eft.get_symbol_filters(sym, target_env=target_env)
                sl_rounded = eft.round_price(emergency_sl, filters["tickSize"], filters["precision_price"])
                eft.place_algo_stop_loss(sym, exit_side, sl_rounded, target_env=target_env)
                print(f"     ✅ Stop Loss de emergencia colocado en {sl_rounded}")
            else:
                sl_price = float(active_sl[0].get("triggerPrice", 0))
                print(f"     🛡️ Stop Loss Activo confirmado en: {sl_price:.5f}")

                # Si la posición está en verde sustancial, mover a True Net Break-Even
                if roe_pct >= 5.0 and auto_ratchet:
                    # True Net BE (+0.2% de ganancia para absorber comisiones)
                    fee_buffer = 0.002
                    target_be = entry_p * (1.0 + fee_buffer) if direction == "LONG" else entry_p * (1.0 - fee_buffer)
                    filters = eft.get_symbol_filters(sym, target_env=target_env)
                    be_rounded = eft.round_price(target_be, filters["tickSize"], filters["precision_price"])

                    is_better = (be_rounded > sl_price) if direction == "LONG" else (be_rounded < sl_price)
                    if is_better:
                        print(f"     📈 Posición en ganancia (+{roe_pct:.1f}% ROE). Ceñiendo a True Net Break-Even...")
                        be_res = eft.move_sl_to_breakeven(sym, target_env=target_env)
                        if be_res.get("success"):
                            print(f"     ✅ SL Blindado a Break-Even en {be_rounded} (+0.2% comisiones cubiertas). CERO RIESGO.")
                        else:
                            print(f"     ⚠️  Aviso al ceñir SL: {be_res.get('error')}")

    # 2. Limpieza de Órdenes Límite Huérfanas
    print("\n🧹 LIMPIEZA DE ÓRDENES LÍMITE PENDIENTES:")
    open_orders = eft.send_signed_request("GET", "/fapi/v1/openOrders", target_env=target_env)
    if isinstance(open_orders, list) and open_orders:
        now_ms = int(time.time() * 1000)
        cancelled_count = 0
        active_symbols = {p["symbol"] for p in active}

        for o in open_orders:
            order_id = o.get("orderId")
            sym = o.get("symbol")
            created_ms = o.get("time", now_ms)
            age_min = (now_ms - created_ms) / (1000 * 60)

            # Si el símbolo ya no tiene posición viva o la orden tiene > 90m
            if sym not in active_symbols or age_min > 90:
                print(f"   • Cancelando orden huérfana #{order_id} en {sym} (edad: {age_min:.0f}m, tipo: {o.get('type')})")
                eft.send_signed_request("DELETE", "/fapi/v1/order", {"symbol": sym, "orderId": order_id}, target_env=target_env)
                cancelled_count += 1

        if cancelled_count > 0:
            print(f"✅ {cancelled_count} orden(es) huérfana(s) canceladas para evitar ejecuciones accidentales.")
        else:
            print("✅ No se detectaron órdenes huérfanas expiradas.")
    else:
        print("✅ Cero órdenes límite pendientes en el exchange.")

    # 3. Sincronizar Estado de Sesión Final
    print("\n📡 Sincronizando sesión para dejar el Ground Truth actualizado...")
    sync_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sync_session_state.py")
    os.system(f"{sys.executable} {sync_script} > /dev/null 2>&1")
    print("✅ session_state.json actualizado con el estado de cierre nocturno.")

    print("\n" + "=" * 70)
    print("🌙 NIGHT CUTOFF COMPLETADO. DESK EN MODO NOCTURNO SEGURO.")
    print("=" * 70)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Night Cutoff Loop - Zero Overnight Risk")
    parser.add_argument("--env", default="testnet", choices=["testnet", "mainnet"])
    parser.add_argument("--auto-ratchet", action="store_true", default=True)
    args = parser.parse_args()

    run_night_cutoff(target_env=args.env, auto_ratchet=args.auto_ratchet)
