#!/usr/bin/env python3
"""
dynamic_exit_manager.py - Quantitative Dynamic Exit and Structural Trailing Stop Manager.
Replaces flat break-even with microstructural levels (15m Swing High/Low + Chandelier ATR),
preserving positive right-tail convexity and managing Alpha Decay (stalled momentum timeouts).

Stop updates are PLACE-THEN-CANCEL (execute_futures_trade.replace_protective_stop): the new stop is
verified on /fapi/v1/openAlgoOrders before the old one is cancelled, stops only ever tighten, and any
update whose resulting protection cannot be verified reports success=False.

Usage:
  python3 scripts/dynamic_exit_manager.py [--env prod|testnet] [--symbol BTCUSDT] [--dry-run] [--json]
The background loop scripts/loops/position_guardian_loop.py runs this logic on a schedule.
"""

import os
import sys
import json
import time
import urllib.request

# Ensure local path resolution
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
from utils.env_resolver import resolve_env

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

def calculate_structural_stop(symbol, direction, entry_price, current_sl_price=0.0, target_env=None):
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
    target_env = resolve_env(target_env)
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

def update_position_to_structural_stop(symbol, target_env=None, dry_run=False, position=None):
    """
    Ratchets the Stop Loss of an open position to its structural level, strictly if it tightens risk.

    Guarantees:
      - PLACE-THEN-CANCEL: the new stop is placed and verified on /fapi/v1/openAlgoOrders before the old stop
        is cancelled; if it cannot be verified the old stop is kept and success is False.
      - Never loosens: a stop is only replaced by one strictly closer to price in the favourable direction.
      - success=True only when the position ends with a verified stop (existing or new).
      - dry_run=True computes the decision but never sends a write request.
    `position` (a positionRisk row) may be passed to avoid re-querying.
    """
    target_env = resolve_env(target_env)
    symbol = str(symbol).upper()
    base = {"symbol": symbol, "env": target_env, "updated": False, "dry_run": bool(dry_run)}

    if position is None:
        pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, target_env=target_env)
        if not isinstance(pos_res, list):
            return dict(base, success=False, reason="position_query_failed", error=f"Position query failed for {symbol}: {pos_res}")
        active = [p for p in pos_res if float(p.get("positionAmt", 0) or 0) != 0]
        if not active:
            return dict(base, success=False, reason="no_position", error=f"No active position for {symbol}")
        position = active[0]

    amt = float(position["positionAmt"])
    entry_p = float(position["entryPrice"])
    mark_p = float(position.get("markPrice") or 0)
    is_long = amt > 0
    direction = "LONG" if is_long else "SHORT"
    exit_side = "SELL" if is_long else "BUY"
    qty = str(position["positionAmt"]).strip().lstrip("-")
    base.update(direction=direction, entry_price=entry_p)

    old_stops, err = eft.get_open_stop_orders(symbol, exit_side, target_env=target_env)
    if err:
        return dict(base, success=False, reason="orders_query_failed",
                    error=f"Cannot read current stops for {symbol} ({err}); nothing changed.")
    current = eft.tightest_stop(old_stops, is_long)
    current_sl = eft._trigger_price(current) if current else 0.0
    protected = current_sl > 0
    base.update(previous_sl=current_sl, current_sl=current_sl)

    def keep(reason, message):
        # No write: success mirrors whether an existing verified stop protects the position.
        out = dict(base, success=protected, reason=reason, message=message)
        if not protected:
            out["error"] = f"{message} Position has NO verified stop."
        return out

    calc = calculate_structural_stop(symbol, direction, entry_p, current_sl_price=current_sl, target_env=target_env)
    if not calc:
        return keep("structural_stop_unavailable", f"Could not calculate structural stop for {symbol}.")

    new_sl = calc["new_structural_sl"]
    base["structural_sl"] = new_sl
    ref_price = mark_p or calc.get("current_price") or 0
    if ref_price and ((is_long and new_sl >= ref_price) or (not is_long and new_sl <= ref_price)):
        return keep("stop_would_trigger_immediately",
                    f"Structural level {new_sl} is not on the protective side of price {ref_price}; stop unchanged.")
    if not eft.is_tighter_stop(new_sl, current_sl, is_long):
        return keep("not_tighter",
                    f"Active stop ({current_sl}) is already optimal or tighter than structural level ({new_sl}). Preserved unchanged.")

    if dry_run:
        out = keep("dry_run", f"DRY RUN: would tighten {symbol} stop from {current_sl or 'none'} to {new_sl}.")
        out["planned_sl"] = new_sl
        return out

    filters = eft.get_symbol_filters(symbol, target_env=target_env) or {}
    rep = eft.replace_protective_stop(symbol, exit_side, new_sl, qty, old_stops, target_env=target_env,
                                      tick_size=filters.get("tickSize"))
    if not rep["success"]:
        msg = (f"New structural stop {new_sl} for {symbol} could not be verified ({rep.get('placement')}). "
               + (f"Previous stop {current_sl} kept; nothing cancelled." if protected else "Position has NO verified stop."))
        return dict(base, success=False, reason="new_stop_unverified", error=msg, message=msg)

    warnings = [f"Old stop {ce['algo_id']} could not be cancelled ({ce['error']})." for ce in rep["cancel_errors"]]
    return dict(
        base,
        success=True,
        updated=True,
        reason="tightened",
        new_sl=new_sl,
        new_stop=rep["new_stop"],
        cancelled_old_stop_ids=rep["cancelled_old_stop_ids"],
        warnings=warnings,
        is_profit_locked=calc["is_profit_locked"],
        locked_roe_pct=calc["locked_roe_pct"],
        message=f"🛡️ Stop Loss updated to structural level: {new_sl} ({'+' if calc['is_profit_locked'] else ''}{calc['locked_roe_pct']}% ROE protected).",
    )

def audit_and_trail_all_positions(target_env=None, dry_run=False):
    """
    Audits all open account positions and ratchets their Stop Loss to structural
    levels in profit if favorable market expansion has occurred.
    """
    target_env = resolve_env(target_env)
    pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    if not isinstance(pos_res, list):
        return {"error": f"Error querying positions: {pos_res}"}

    active = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0]
    if not active:
        return {"total_active": 0, "message": "No active open positions."}

    results = []
    for p in active:
        sym = p["symbol"]
        try:
            res = update_position_to_structural_stop(sym, target_env=target_env, dry_run=dry_run, position=p)
        except Exception as e:
            res = {"symbol": sym, "success": False, "updated": False, "reason": "exception", "error": str(e), "message": f"Error: {e}"}
        results.append(res)

    return {
        "total_active": len(active),
        "results": results
    }

def check_dead_alpha_timeout(symbol, target_env=None, max_idle_minutes=90):
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

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Structural trailing stop audit (place-then-cancel, never loosens)")
    parser.add_argument("--env", choices=["prod", "testnet"], default=None, help="Target environment; defaults to utils.env_resolver.resolve_env()")
    parser.add_argument("--symbol", default=None, help="Only trail this symbol")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Compute decisions without sending writes")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON")
    args = parser.parse_args(argv)

    target_env = resolve_env(args.env)
    if args.symbol:
        res = update_position_to_structural_stop(args.symbol.upper(), target_env=target_env, dry_run=args.dry_run)
        audit = {"total_active": 1, "results": [res]}
    else:
        audit = audit_and_trail_all_positions(target_env=target_env, dry_run=args.dry_run)

    if args.json_output:
        print(json.dumps(audit, indent=2))
    else:
        print(f"🔬 DYNAMIC EXITS & STRUCTURAL TRAILING AUDIT ({target_env.upper()}) 🔬\n")
        if audit.get("error"):
            print(f"Error: {audit['error']}")
        for r in audit.get("results", []):
            print(f"• {r.get('symbol')}: {r.get('message') or r.get('error')}")
    failed = bool(audit.get("error")) or any(not r.get("success") for r in audit.get("results", []))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
