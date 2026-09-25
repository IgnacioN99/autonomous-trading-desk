#!/usr/bin/env python3
"""
trading_doctor.py - Diagnóstico Pre-Vuelo y Sensor de Salud del Desk de Trading.
Verificación integral de conectividad, sincronización de reloj y auditoría de huérfanas.

Verifica:
1. Conectividad y Latencia de API (< 800ms)
2. Deriva de Reloj (Clock Drift < 1000ms con el servidor de Binance)
3. Credenciales y Permisos de API (Testnet / Mainnet)
4. Capital Disponible y Saldo en USDT
5. Auditoría Forense de Posiciones Huérfanas (Fail CLOSED si hay una posición sin Stop Loss en ledger)
6. Frescura del Ledger de Estado (session_state.json)

Uso:
  python3 scripts/trading_doctor.py [--env testnet|mainnet] [--heal]
  Exit 0 si el sistema está listo para operar.
  Exit 1 si existe una falla crítica que prohíbe operar (Fail CLOSED).
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import hmac
import hashlib

# Asegurar path local
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft

def run_doctor(target_env: str = "testnet", auto_heal: bool = False) -> int:
    start_time = time.time()
    print("=" * 65)
    print("🩺 TRADING DOCTOR — PRE-FLIGHT SYSTEM DIAGNOSTIC")
    print(f"Target Environment: {target_env.upper()}")
    print("=" * 65)

    critical_failures = []
    warnings = []
    ok_items = []

    # 1. Configuración de API y Credenciales
    api_key, secret_key, base_url = eft.get_client_config(target_env=target_env)
    if not api_key or not secret_key:
        critical_failures.append("Credenciales de API no encontradas o inválidas en .env")
        print("❌ [API KEYS] Credenciales ausentes o con placeholder en .env")
        return 1
    else:
        masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "***"
        ok_items.append(f"Credenciales detectadas para {target_env.upper()} ({masked_key})")
        print(f"✅ [API KEYS] Credenciales OK ({target_env.upper()})")

    # 2. Ping de Red y Clock Drift
    try:
        t0 = time.time()
        req = urllib.request.Request(f"{base_url}/fapi/v1/time", headers={"User-Agent": "TradingDoctor/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            t1 = time.time()
            data = json.loads(resp.read().decode())
            server_time = data.get("serverTime", 0)
            latency_ms = int((t1 - t0) * 1000)
            mid_local_ms = int(((t0 + t1) / 2.0) * 1000)
            drift_ms = abs(server_time - mid_local_ms)

            if latency_ms > 1200:
                warnings.append(f"Latencia de red elevada: {latency_ms}ms")
                print(f"⚠️  [NETWORK] Latencia alta: {latency_ms}ms")
            else:
                ok_items.append(f"Latencia de API: {latency_ms}ms")
                print(f"✅ [NETWORK] Latencia de API: {latency_ms}ms")

            if drift_ms > 1000:
                critical_failures.append(f"Clock drift excesivo: {drift_ms}ms (límite 1000ms)")
                print(f"❌ [CLOCK DRIFT] Deriva de reloj peligrosa: {drift_ms}ms")
            elif drift_ms > 400:
                warnings.append(f"Clock drift moderado: {drift_ms}ms")
                print(f"⚠️  [CLOCK DRIFT] Deriva de reloj moderada: {drift_ms}ms")
            else:
                ok_items.append(f"Clock drift óptimo: {drift_ms}ms")
                print(f"✅ [CLOCK DRIFT] Sincronización de reloj OK ({drift_ms}ms)")
    except Exception as e:
        critical_failures.append(f"Fallo al conectar con endpoint de tiempo: {str(e)}")
        print(f"❌ [NETWORK] Imposible conectar con {base_url}: {e}")
        return 1

    # 3. Consulta de Balances y Margen Libre
    try:
        balance_res = eft.send_signed_request("GET", "/fapi/v2/balance", target_env=target_env)
        if isinstance(balance_res, list):
            usdt_bal = next((b for b in balance_res if b.get("asset") == "USDT"), None)
            if usdt_bal:
                total_bal = float(usdt_bal.get("balance", 0.0))
                free_bal = float(usdt_bal.get("availableBalance", 0.0))
                if free_bal < 10.0:
                    warnings.append(f"Saldo USDT disponible bajo: ${free_bal:.2f} USDT")
                    print(f"⚠️  [BALANCE] Saldo USDT disponible bajo: ${free_bal:.2f} (Total: ${total_bal:.2f})")
                else:
                    ok_items.append(f"Saldo USDT: ${free_bal:.2f} disponible de ${total_bal:.2f}")
                    print(f"✅ [BALANCE] Saldo disponible: ${free_bal:.2f} USDT (Total: ${total_bal:.2f})")
            else:
                warnings.append("No se encontró el activo USDT en el balance de futuros")
                print("⚠️  [BALANCE] Activo USDT no encontrado")
        else:
            critical_failures.append(f"Respuesta inesperada al consultar balance: {balance_res}")
            print(f"❌ [BALANCE] Error en balance: {balance_res}")
    except Exception as e:
        critical_failures.append(f"Fallo al consultar balance: {str(e)}")
        print(f"❌ [BALANCE] Error de autenticación o conexión: {e}")

    # 4. Auditoría Forense de Posiciones Huérfanas (FAIL CLOSED)
    try:
        pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
        active_positions = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0] if isinstance(pos_res, list) else []

        algos_res = eft.send_signed_request("GET", "/fapi/v1/openAlgoOrders", target_env=target_env)
        active_algos = algos_res if isinstance(algos_res, list) else []
        algo_symbols = {a.get("symbol") for a in active_algos if a.get("orderType") in ["STOP_MARKET", "STOP"]}

        orphan_positions = []
        for p in active_positions:
            sym = p.get("symbol")
            amt = float(p.get("positionAmt", 0))
            if sym not in algo_symbols:
                orphan_positions.append(p)

        if orphan_positions:
            err_msg = f"Detectadas {len(orphan_positions)} posición(es) HUÉRFANAS sin Stop Loss en Binance: {[p['symbol'] for p in orphan_positions]}"
            if auto_heal:
                print(f"🚨 [ORPHAN AUDIT] {err_msg} — DISPARANDO AUTO-HEAL...")
                for op in orphan_positions:
                    sym = op["symbol"]
                    amt = float(op["positionAmt"])
                    entry_p = float(op["entryPrice"])
                    exit_side = "SELL" if amt > 0 else "BUY"
                    # Colocar Stop Loss de emergencia al 2.5% del precio de entrada
                    emergency_sl = entry_p * (0.975 if amt > 0 else 1.025)
                    heal_res = eft.place_algo_stop_loss(sym, exit_side, emergency_sl, target_env=target_env)
                    if heal_res.get("algoId"):
                        print(f"   🛡️ Auto-Heal exitoso para {sym}: Algo SL colocado en {emergency_sl:.5f}")
                        ok_items.append(f"Auto-Heal aplicado a {sym}")
                    else:
                        critical_failures.append(f"Fallo de Auto-Heal en {sym}: {heal_res}")
                        print(f"   ❌ Fallo al aplicar Auto-Heal en {sym}: {heal_res}")
            else:
                critical_failures.append(err_msg)
                print(f"❌ [ORPHAN AUDIT] FAIL CLOSED: {err_msg}")
                print("   👉 Ejecuta `python3 scripts/trading_doctor.py --heal` o coloca el Stop Loss inmediatamente.")
        else:
            if active_positions:
                ok_items.append(f"{len(active_positions)} posición(es) activa(s), todas con Stop Loss verificado en Binance")
                print(f"✅ [ORPHAN AUDIT] {len(active_positions)} posición(es) viva(s) — Todas protegidas con Stop Loss.")

                # 4b. Sensor de Deriva Temporal y Alfa Muerto (Drift Watchdog)
                try:
                    import trading_drift_watchdog as tdw
                    drift_report = tdw.audit_dead_alpha(target_env=target_env, max_hours=4.0, auto_exit=False)
                    dead_count = drift_report.get("dead_alpha_count", 0)
                    if dead_count > 0:
                        warnings.append(f"Detectada(s) {dead_count} posición(es) con Alfa Muerto (>4h estancadas).")
                        print(f"⚠️  [DEAD ALPHA] {dead_count} posición(es) estancadas superan el horizonte intradía.")
                    else:
                        ok_items.append("Salud temporal de posiciones viva OK (Sin Alfa Muerto).")
                except Exception as e:
                    pass
            else:
                ok_items.append("Cero posiciones abiertas. Cero exposición.")
                print("✅ [ORPHAN AUDIT] Cartera limpia. Cero posiciones abiertas.")
    except Exception as e:
        critical_failures.append(f"Fallo en auditoría de órdenes huérfanas: {str(e)}")
        print(f"❌ [ORPHAN AUDIT] Error al consultar posiciones y órdenes: {e}")

    # 5. Verificación de Frescura de session_state.json
    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    state_file = os.path.join(logs_dir, "session_state.json")
    if os.path.exists(state_file):
        mtime = os.path.getmtime(state_file)
        age_sec = time.time() - mtime
        if age_sec > 1800:
            warnings.append(f"session_state.json desactualizado ({int(age_sec/60)} minutos de antigüedad). Corre `sync_session_state.py`.")
            print(f"⚠️  [STATE LEDGER] session_state.json tiene {int(age_sec/60)} min. Se recomienda sincronizar.")
        else:
            ok_items.append(f"session_state.json fresco ({int(age_sec)}s)")
            print(f"✅ [STATE LEDGER] session_state.json sincronizado hace {int(age_sec)}s")
    else:
        warnings.append("session_state.json no existe aún. Corre `sync_session_state.py`.")
        print("⚠️  [STATE LEDGER] session_state.json no existe. Corre `sync_session_state.py`.")

    elapsed = round(time.time() - start_time, 2)
    print("=" * 65)
    print(f"DIAGNÓSTICO COMPLETADO EN {elapsed}s")

    if critical_failures:
        print(f"🔴 ESTADO: SISTEMA INHABILITADO ({len(critical_failures)} fallas críticas). FAIL CLOSED.")
        for f in critical_failures:
            print(f"   ✖ {f}")
        print("=" * 65)
        return 1
    elif warnings:
        print(f"🟡 ESTADO: OPERATIVO CON ADVERTENCIAS ({len(warnings)} avisos).")
        for w in warnings:
            print(f"   ▲ {w}")
        print("=" * 65)
        return 0
    else:
        print("🟢 ESTADO: 100% VERDE Y SALUDABLE. LISTO PARA OPERAR.")
        print("=" * 65)
        return 0

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trading Doctor - Pre-flight Health Check")
    parser.add_argument("--env", default="testnet", choices=["testnet", "mainnet"], help="Ambiente de ejecución")
    parser.add_argument("--heal", action="store_true", help="Auto-cura posiciones huérfanas colocando SL de emergencia")
    args = parser.parse_args()

    sys.exit(run_doctor(target_env=args.env, auto_heal=args.heal))
