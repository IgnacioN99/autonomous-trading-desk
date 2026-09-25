#!/usr/bin/env python3
"""
dynamic_exit_manager.py - Gestor Cuantitativo de Salidas Dinámicas y Trailing Stops Estructurales.
Reemplaza el Break-Even plano por niveles microestructurales (Swing High/Low de 5m + Chandelier ATR),
preservando la convexidad de la cola derecha y gestionando el Alpha Decay (timeout por estancamiento).
"""

import os
import sys
import json
import time
import urllib.request
from decimal import Decimal

# Asegurar path local
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft

BASE_FAPI = "https://fapi.binance.com"

def fetch_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def get_klines_data(symbol, interval="5m", limit=30):
    url = f"{BASE_FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return fetch_json(url)

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return (max(highs) - min(lows)) / 2 if highs and lows else 0.0
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    return sum(trs[-period:]) / period

def find_recent_swings(highs, lows, window=2):
    """
    Encuentra puntos de giro estructurales (fractales de soporte y resistencia).
    """
    swing_lows = []
    swing_highs = []
    n = len(highs)
    for i in range(window, n - window):
        # Mínimo menor que sus velas vecinas
        if all(lows[i] <= lows[i - j] for j in range(1, window + 1)) and all(lows[i] <= lows[i + j] for j in range(1, window + 1)):
            swing_lows.append(lows[i])
        # Máximo mayor que sus velas vecinas
        if all(highs[i] >= highs[i - j] for j in range(1, window + 1)) and all(highs[i] >= highs[i + j] for j in range(1, window + 1)):
            swing_highs.append(highs[i])
    return swing_lows, swing_highs

def calculate_structural_stop(symbol, direction, entry_price, current_sl_price=0.0, target_env="testnet"):
    """
    Calcula el precio de Stop Loss dinámico óptimo preservando la convexidad (positive skewness):
    - Utiliza velas de 15m (en lugar de 5m) para filtrar el micro-ruido del spread y libros de órdenes.
    - Ancla el stop detrás de Swing Lows/Highs estructurales con colchón Chandelier ATR (1.8x ATR_15m).
    - PRESERVACIÓN DE COLA DERECHA (Anti-Truncamiento de Ganancias):
      NO mueve prematuramente el Stop Loss a Break-Even ante micro-retrocesos.
      El stop solo se ratchetea a True Net Break-Even (+0.2% fee buffer) si el precio ha avanzado
      al menos +2.0x ATR_15m a favor (expansión de tendencia confirmada) o si el Swing estructural
      mismo ya se ha consolidado en terreno de ganancia.
    """
    filters = eft.get_symbol_filters(symbol, target_env=target_env)
    if not filters:
        return None

    k15m = get_klines_data(symbol, interval="15m", limit=35)
    if not k15m or len(k15m) < 15:
        return None

    opens = [float(k[1]) for k in k15m]
    highs = [float(k[2]) for k in k15m]
    lows = [float(k[3]) for k in k15m]
    closes = [float(k[4]) for k in k15m]
    cur_p = closes[-1]

    atr_15m = calculate_atr(highs, lows, closes, period=14)
    swing_lows, swing_highs = find_recent_swings(highs, lows, window=2)

    is_long = direction.upper() == "LONG"

    if is_long:
        # Para LONG: Buscar swing low estructural reciente
        recent_swing_low = swing_lows[-1] if swing_lows else min(lows[-6:])
        chandelier_stop = cur_p - (1.8 * atr_15m)
        structural_level = recent_swing_low - (0.3 * atr_15m)
        candidate_stop = max(structural_level, chandelier_stop)

        # Preservación de cola derecha: Solo mover a True Net Break-Even si la expansión es >= 2.0x ATR
        fee_buffer_stop = entry_price * 1.002
        if cur_p >= entry_price + (2.0 * atr_15m):
            candidate_stop = max(candidate_stop, fee_buffer_stop)

        # No permitir retroceder el Stop Loss si ya estaba más arriba (ratchet unidireccional)
        if current_sl_price > 0 and candidate_stop <= current_sl_price:
            candidate_stop = current_sl_price

        # No colocar el stop por encima del precio actual
        candidate_stop = min(candidate_stop, cur_p - (0.5 * atr_15m))

    else:
        # Para SHORT: Buscar swing high estructural reciente
        recent_swing_high = swing_highs[-1] if swing_highs else max(highs[-6:])
        chandelier_stop = cur_p + (1.8 * atr_15m)
        structural_level = recent_swing_high + (0.3 * atr_15m)
        candidate_stop = min(structural_level, chandelier_stop)

        # Preservación de cola derecha: Solo mover a True Net Break-Even si la caída es >= 2.0x ATR
        fee_buffer_stop = entry_price * 0.998
        if cur_p <= entry_price - (2.0 * atr_15m):
            candidate_stop = min(candidate_stop, fee_buffer_stop)

        # No permitir retroceder el Stop Loss si ya estaba más abajo
        if current_sl_price > 0 and candidate_stop >= current_sl_price:
            candidate_stop = current_sl_price

        # No colocar el stop por debajo del precio actual
        candidate_stop = max(candidate_stop, cur_p + (0.5 * atr_15m))

    rounded_stop = eft.round_price(candidate_stop, filters["tickSize"], filters["precision_price"])

    return {
        "symbol": symbol,
        "direction": direction.upper(),
        "current_price": cur_p,
        "entry_price": entry_price,
        "current_sl": current_sl_price,
        "new_structural_sl": rounded_stop,
        "atr_15m": atr_15m,
        "is_profit_locked": (rounded_stop > entry_price) if is_long else (rounded_stop < entry_price),
        "locked_roe_pct": round(((rounded_stop - entry_price) / entry_price * 100) if is_long else ((entry_price - rounded_stop) / entry_price * 100), 2),
        "should_update": (rounded_stop > current_sl_price) if is_long else (rounded_stop < current_sl_price and current_sl_price > 0)
    }

def update_position_to_structural_stop(symbol, target_env="testnet"):
    """
    Audita la posición de un par y actualiza su Stop Loss al nivel estructural óptimo
    únicamente si mejora el riesgo (ratchet unidireccional protector).
    """
    pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, target_env=target_env)
    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0] if isinstance(pos_res, list) else []
    if not active:
        return {"success": False, "error": f"No hay posición activa en {symbol}"}

    pos = active[0]
    amt = float(pos["positionAmt"])
    entry_p = float(pos["entryPrice"])
    direction = "LONG" if amt > 0 else "SHORT"
    exit_side = "SELL" if amt > 0 else "BUY"

    # Consultar SL actual
    open_algos = eft.send_signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, target_env=target_env)
    current_sl = 0.0
    old_algo_id = None
    if isinstance(open_algos, list):
        for ao in open_algos:
            if ao.get("orderType") in ["STOP_MARKET", "STOP"] and ao.get("side") == exit_side:
                current_sl = float(ao.get("triggerPrice", 0))
                old_algo_id = ao.get("algoId")
                break

    calc = calculate_structural_stop(symbol, direction, entry_p, current_sl_price=current_sl, target_env=target_env)
    if not calc:
        return {"success": False, "error": f"No se pudo calcular el stop estructural para {symbol}"}

    new_sl = calc["new_structural_sl"]

    # Si el nuevo stop no mejora el riesgo actual, mantener el stop vigente
    if not calc["should_update"] and current_sl > 0:
        return {
            "success": True,
            "updated": False,
            "symbol": symbol,
            "message": f"Stop actual ({current_sl}) ya es óptimo o más estricto que el nivel estructural ({new_sl}). Se mantiene sin cambios.",
            "current_sl": current_sl,
            "structural_sl": new_sl
        }

    # Cancelar SL previo y colocar nuevo SL con verificación y rollback
    if old_algo_id:
        eft.send_signed_request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": old_algo_id}, target_env=target_env)

    new_order = eft.place_algo_stop_loss(symbol, exit_side, new_sl, target_env=target_env)
    verified, _ = eft.verify_algo_stop_loss(symbol, exit_side, new_sl, target_env=target_env)

    # Rollback de seguridad si falló la colocación del nuevo SL
    if not verified and current_sl > 0:
        rollback = eft.place_algo_stop_loss(symbol, exit_side, current_sl, target_env=target_env)
        return {
            "success": False,
            "error": f"Fallo al actualizar a stop estructural ({new_order}). Rollback ejecutado: SL previo restaurado en {current_sl}."
        }

    return {
        "success": True,
        "updated": True,
        "symbol": symbol,
        "direction": direction,
        "previous_sl": current_sl,
        "new_sl": new_sl,
        "entry_price": entry_p,
        "is_profit_locked": calc["is_profit_locked"],
        "locked_roe_pct": calc["locked_roe_pct"],
        "message": f"🛡️ Stop Loss actualizado a nivel estructural: {new_sl} ({'+' if calc['is_profit_locked'] else ''}{calc['locked_roe_pct']}% ROE protegido)."
    }

def audit_and_trail_all_positions(target_env="testnet"):
    """
    Audita todas las posiciones activas de la cuenta y ratchetea sus Stop Loss a niveles
    estructurales en profit si el precio ya se ha desplazado favorablemente.
    """
    pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    if not isinstance(pos_res, list):
        return {"error": f"Error consultando posiciones: {pos_res}"}

    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0]
    if not active:
        return {"total_active": 0, "message": "No hay posiciones activas abiertas."}

    results = []
    for p in active:
        sym = p["symbol"]
        res = update_position_to_structural_stop(sym, target_env=target_env)
        results.append(res)

    return {
        "total_active": len(active),
        "results": results
    }

def check_dead_alpha_timeout(symbol, target_env="testnet", max_idle_minutes=90):
    """
    Evalúa si la posición sufre de 'Alfa Muerto' (estancamiento prolongado sin expansión de volumen ni precio).
    Si un setup intradía no avanza en 90 min y su rango es menor al 0.35%, la tesis se considera expirada.
    """
    k15m = get_klines_data(symbol, interval="15m", limit=10)
    if not k15m or len(k15m) < 6:
        return {"status": "UNKNOWN", "reason": "Datos insuficientes"}

    closes = [float(k[4]) for k in k15m[-6:]]
    volumes = [float(k[5]) for k in k15m[-6:]]
    highs = [float(k[2]) for k in k15m[-6:]]
    lows = [float(k[3]) for k in k15m[-6:]]

    max_p = max(highs)
    min_p = min(lows)
    range_pct = ((max_p - min_p) / min_p) * 100
    avg_recent_vol = sum(volumes) / len(volumes)

    # Si en las últimas 6 velas de 15m (90 min) el precio osciló menos de un 0.40%
    if range_pct < 0.40:
        return {
            "status": "DEAD_ALPHA_STALLED",
            "range_pct": round(range_pct, 2),
            "idle_candles": 6,
            "recommendation": "CERRAR_O_PROTEGER",
            "message": f"⚠️ ALFA AGOTADO ({symbol}): Rango de 90m extremadamente comprimido ({range_pct:.2f}%). La tesis intradiaria ha perdido impulso."
        }

    return {
        "status": "HEALTHY_MOMENTUM",
        "range_pct": round(range_pct, 2),
        "recommendation": "MANTENER",
        "message": f"Posición {symbol} en curso con volatilidad activa ({range_pct:.2f}% de rango en 90m)."
    }

if __name__ == "__main__":
    print("🔬 AUDITORÍA DE SALIDAS DINÁMICAS & TRAILING ESTRUCTURAL 🔬\n")
    audit = audit_and_trail_all_positions(target_env="testnet")
    for r in audit.get("results", []):
        print(f"• {r.get('symbol')}: {r.get('message')}")
