#!/usr/bin/env python3
"""
dynamic_exit_manager.py - Quantitative Dynamic Exit and Structural Trailing Stop Manager.
Replaces flat break-even with microstructural levels (15m Swing High/Low + Chandelier ATR),
preserving positive right-tail convexity and managing Alpha Decay (stalled momentum timeouts).
"""

import os
import sys
import json
import time
import urllib.request
from decimal import Decimal

# Ensure local path resolution
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
    Finds structural swing pivot points (support and resistance fractals).
    """
    swing_lows = []
    swing_highs = []
    n = len(highs)
    for i in range(window, n - window):
        # Pivot low lower than its surrounding neighbor bars
        if all(lows[i] <= lows[i - j] for j in range(1, window + 1)) and all(lows[i] <= lows[i + j] for j in range(1, window + 1)):
            swing_lows.append(lows[i])
        # Pivot high higher than its surrounding neighbor bars
        if all(highs[i] >= highs[i - j] for j in range(1, window + 1)) and all(highs[i] >= highs[i + j] for j in range(1, window + 1)):
            swing_highs.append(highs[i])
    return swing_lows, swing_highs

def calculate_structural_stop(symbol, direction, entry_price, current_sl_price=0.0, target_env="testnet"):
    """
    Calculates optimal dynamic Stop Loss preserving convexity (positive skewness):
    - Uses 15m candles (instead of 5m) to filter order book and spread micro-noise.
    - Anchors stop behind structural Swing Lows/Highs with Chandelier ATR buffer (1.8x ATR_15m).
    - RIGHT-TAIL PRESERVATION (Zero Early Truncation):
      Does NOT prematurely drag Stop Loss to Break-Even on minor pullbacks.
      The stop only ratchets to True Net Break-Even (+0.2% fee buffer) once price has advanced
      at least +2.0x ATR_15m favorably (confirmed trend expansion) or when the structural
      swing pivot itself has firmly consolidated into profitable territory.
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
        # For LONG: Locate recent structural swing low
        recent_swing_low = swing_lows[-1] if swing_lows else min(lows[-6:])
        chandelier_stop = cur_p - (1.8 * atr_15m)
        structural_level = recent_swing_low - (0.3 * atr_15m)
        candidate_stop = max(structural_level, chandelier_stop)

        # Right-tail preservation: Only ratchet to True Net Break-Even once expansion >= 2.0x ATR
        fee_buffer_stop = entry_price * 1.002
        if cur_p >= entry_price + (2.0 * atr_15m):
            candidate_stop = max(candidate_stop, fee_buffer_stop)

        # Do not allow stop loss to regress lower (unidirectional ratchet)
        if current_sl_price > 0 and candidate_stop <= current_sl_price:
            candidate_stop = current_sl_price

        # Do not place stop above current mark price
        candidate_stop = min(candidate_stop, cur_p - (0.5 * atr_15m))

    else:
        # For SHORT: Locate recent structural swing high
        recent_swing_high = swing_highs[-1] if swing_highs else max(highs[-6:])
        chandelier_stop = cur_p + (1.8 * atr_15m)
        structural_level = recent_swing_high + (0.3 * atr_15m)
        candidate_stop = min(structural_level, chandelier_stop)

        # Right-tail preservation: Only ratchet to True Net Break-Even once decline >= 2.0x ATR
        fee_buffer_stop = entry_price * 0.998
        if cur_p <= entry_price - (2.0 * atr_15m):
            candidate_stop = min(candidate_stop, fee_buffer_stop)

        # Do not allow stop loss to regress higher
        if current_sl_price > 0 and candidate_stop >= current_sl_price:
            candidate_stop = current_sl_price

        # Do not place stop below current mark price
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
    Audits a position and ratchets its Stop Loss to the optimal structural level
    strictly if it improves risk (protective unidirectional ratchet).
    """
    pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, target_env=target_env)
    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0] if isinstance(pos_res, list) else []
    if not active:
        return {"success": False, "error": f"No active position for {symbol}"}

    pos = active[0]
    amt = float(pos["positionAmt"])
    entry_p = float(pos["entryPrice"])
    direction = "LONG" if amt > 0 else "SHORT"
    exit_side = "SELL" if amt > 0 else "BUY"

    # Query current active SL
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
        return {"success": False, "error": f"Could not calculate structural stop for {symbol}"}

    new_sl = calc["new_structural_sl"]

    # If the new stop does not tighten current risk, preserve active stop
    if not calc["should_update"] and current_sl > 0:
        return {
            "success": True,
            "updated": False,
            "symbol": symbol,
            "message": f"Active stop ({current_sl}) is already optimal or tighter than structural level ({new_sl}). Preserved unchanged.",
            "current_sl": current_sl,
            "structural_sl": new_sl
        }

    # Cancel previous SL and place new SL with verification and rollback
    if old_algo_id:
        eft.send_signed_request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": old_algo_id}, target_env=target_env)

    new_order = eft.place_algo_stop_loss(symbol, exit_side, new_sl, target_env=target_env)
    verified, _ = eft.verify_algo_stop_loss(symbol, exit_side, new_sl, target_env=target_env)

    # Safety rollback if placement of new SL fails
    if not verified and current_sl > 0:
        rollback = eft.place_algo_stop_loss(symbol, exit_side, current_sl, target_env=target_env)
        return {
            "success": False,
            "error": f"Failed to update to structural stop ({new_order}). Rollback executed: previous SL restored at {current_sl}."
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
        "message": f"🛡️ Stop Loss updated to structural level: {new_sl} ({'+' if calc['is_profit_locked'] else ''}{calc['locked_roe_pct']}% ROE protected)."
    }

def audit_and_trail_all_positions(target_env="testnet"):
    """
    Audits all open account positions and ratchets their Stop Loss to structural
    levels in profit if favorable market expansion has occurred.
    """
    pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    if not isinstance(pos_res, list):
        return {"error": f"Error querying positions: {pos_res}"}

    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0]
    if not active:
        return {"total_active": 0, "message": "No active open positions."}

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
    Evaluates whether the position is suffering from 'Dead Alpha' (prolonged stagnation without price or volume expansion).
    If an intraday setup fails to expand in 90 min and range compression is under 0.40%, the thesis is deemed expired.
    """
    k15m = get_klines_data(symbol, interval="15m", limit=10)
    if not k15m or len(k15m) < 6:
        return {"status": "UNKNOWN", "reason": "Insufficient data"}

    closes = [float(k[4]) for k in k15m[-6:]]
    volumes = [float(k[5]) for k in k15m[-6:]]
    highs = [float(k[2]) for k in k15m[-6:]]
    lows = [float(k[3]) for k in k15m[-6:]]

    max_p = max(highs)
    min_p = min(lows)
    range_pct = ((max_p - min_p) / min_p) * 100
    avg_recent_vol = sum(volumes) / len(volumes)

    # If price fluctuated under 0.40% across the last 6 15m candles (90 min)
    if range_pct < 0.40:
        return {
            "status": "DEAD_ALPHA_STALLED",
            "range_pct": round(range_pct, 2),
            "idle_candles": 6,
            "recommendation": "CLOSE_OR_PROTECT",
            "message": f"⚠️ DEAD ALPHA ({symbol}): 90m range extremely compressed ({range_pct:.2f}%). Intraday thesis has lost momentum."
        }

    return {
        "status": "HEALTHY_MOMENTUM",
        "range_pct": round(range_pct, 2),
        "recommendation": "HOLD",
        "message": f"Position {symbol} tracking normally with active volatility ({range_pct:.2f}% 90m range)."
    }

if __name__ == "__main__":
    print("🔬 DYNAMIC EXITS & STRUCTURAL TRAILING AUDIT 🔬\n")
    audit = audit_and_trail_all_positions(target_env="testnet")
    for r in audit.get("results", []):
        print(f"• {r.get('symbol')}: {r.get('message')}")
