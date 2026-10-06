#!/usr/bin/env python3
"""
dynamic_exit_manager.py - Quantitative Dynamic Exit and Structural Trailing Stop Manager.
Replaces flat break-even with microstructural levels (15m Swing High/Low + Chandelier ATR),
preserving positive right-tail convexity and managing Alpha Decay (stalled momentum timeouts).

Activation gate (Issue #95): the planned Stop Loss is left untouched until the trade has earned a trail:
+1.0R of planned risk (TRAIL_ACTIVATION_R) or +2.0x ATR_15m (TRAIL_ACTIVATION_ATR) of favourable excursion
since entry on CLOSED 15m candles, or a TP1 fill. Once active, the Chandelier stop is anchored to the extreme
since entry (not the forming candle). YOLO positions are never trailed before TP1. TP1/TP2 are never re-based.

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

# Trailing activation gate (Issue #95): the planned SL is kept until price has moved TRAIL_ACTIVATION_R x the
# initial risk, or TRAIL_ACTIVATION_ATR x ATR_15m, in favour since entry on CLOSED 15m bars, or TP1 has filled.
TRAIL_ACTIVATION_R = 1.0
TRAIL_ACTIVATION_ATR = 2.0
REFERENCE_ENTRY_TOLERANCE = 0.005  # trades_audit entry_price must be within 0.5% of the live entryPrice

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

def _bars_since_entry(candles, entry_ts):
    """Closed candles whose open time is at or after entry_ts (the fill candle itself is excluded: conservative).
    entry_ts None -> no bars since entry. entry_ts older than the window -> every closed candle in the window."""
    if entry_ts is None:
        return []
    try:
        entry_ms = float(entry_ts) * 1000.0
    except (TypeError, ValueError):
        return []
    return [k for k in candles if float(k[0]) >= entry_ms]


def calculate_structural_stop(symbol, direction, entry_price, current_sl_price=0.0, target_env=None, *,
                              planned_sl=None, entry_ts=None, tp1_filled=None, mark_price=None,
                              reference_source=None):
    """
    Calculates the dynamic Stop Loss preserving convexity (positive skewness), on CLOSED 15m candles only
    (the forming candle is dropped, so intrabar noise can neither activate nor anchor the trail).

    ACTIVATION GATE (Issue #95): the planned SL is kept untouched until ONE of these holds (checked in order):
      - "tp1_filled":    TP1 has filled (tp1_filled is True);
      - "r_multiple":    MFE since entry >= TRAIL_ACTIVATION_R (1.0) x initial risk R = |entry - planned_sl|;
      - "atr_expansion": MFE since entry >= TRAIL_ACTIVATION_ATR (2.0) x ATR_15m.
    R exists only when planned_sl is on the loss side of entry; ATR_15m <= 0 means never activated.
    MFE (max favourable excursion) is measured on closed 15m candles opened at/after entry_ts (the fill candle is
    excluded). planned_sl defaults to current_sl_price (reference_source "current_stop"; callers pass the
    resolved source, see resolve_trade_reference); entry_ts None means no bars since entry (not activated
    unless TP1 filled). If not activated: should_update False, reason "trail_not_activated".

    ANCHOR once activated:
      - Chandelier: extreme since entry (LONG highest high / SHORT lowest low of closed bars since entry; the last
        closed bar when TP1 filled with no bars yet) -/+ 1.8x ATR_15m.
      - Structural: last 15m swing low - 0.3x ATR (LONG) / swing high + 0.3x ATR (SHORT).
      - LONG candidate = max(structural, chandelier); SHORT = min(...).
      - RIGHT-TAIL PRESERVATION: True Net Break-Even (entry +/-0.2%) only after MFE >= 2.0x ATR_15m since entry
        or after TP1 fill.
      - Never loosens against current_sl_price; never closer than 0.5x ATR to price (mark_price if given, else
        the last closed close).
    Take-profit orders are never re-based: TP1/TP2 keep their original levels.
    """
    target_env = resolve_env(target_env)
    filters = eft.get_symbol_filters(symbol, target_env=target_env)
    if not filters:
        return None

    k15m = get_klines_data(symbol, interval="15m", limit=100)
    if not k15m:
        return None
    closed = k15m[:-1]  # drop the forming candle
    if len(closed) < 15:
        return None

    highs = [float(k[2]) for k in closed]
    lows = [float(k[3]) for k in closed]
    closes = [float(k[4]) for k in closed]
    last_close = closes[-1]
    try:
        mark = float(mark_price) if mark_price else 0.0
    except (TypeError, ValueError):
        mark = 0.0
    cur_p = mark if mark > 0 else last_close

    atr_15m = calculate_atr(highs, lows, closes, period=14)
    is_long = direction.upper() == "LONG"
    entry_price = float(entry_price)
    current_sl_price = float(current_sl_price or 0.0)

    ref_sl = planned_sl if planned_sl is not None else current_sl_price
    try:
        ref_sl = float(ref_sl or 0.0)
    except (TypeError, ValueError):
        ref_sl = 0.0
    # R exists only for a reference stop on the LOSS side of entry (a stop already at break-even or in profit,
    # e.g. the current_stop fallback after an earlier trail, carries no initial risk: no r_multiple activation).
    loss_side = ref_sl > 0 and ((ref_sl < entry_price) if is_long else (ref_sl > entry_price))
    initial_risk = (entry_price - ref_sl) if is_long else (ref_sl - entry_price)
    if not loss_side or initial_risk <= 0:
        initial_risk = None

    since = _bars_since_entry(closed, entry_ts)
    if since:
        extreme = max(float(k[2]) for k in since) if is_long else min(float(k[3]) for k in since)
        mfe = (extreme - entry_price) if is_long else (entry_price - extreme)
    else:
        extreme = None
        mfe = 0.0

    # No usable ATR -> no activation at all (the chandelier and every buffer need ATR > 0).
    activation_reason = None
    if atr_15m > 0:
        if tp1_filled is True:
            activation_reason = "tp1_filled"
        elif since and initial_risk is not None and mfe >= TRAIL_ACTIVATION_R * initial_risk:
            activation_reason = "r_multiple"
        elif since and mfe >= TRAIL_ACTIVATION_ATR * atr_15m:
            activation_reason = "atr_expansion"

    common = {
        "symbol": symbol,
        "direction": direction.upper(),
        "current_price": cur_p,
        "entry_price": entry_price,
        "current_sl": current_sl_price,
        "atr_15m": atr_15m,
        "activation_reason": activation_reason,
        "reference_source": reference_source or (None if planned_sl is not None else "current_stop"),
        "planned_sl": ref_sl or None,
        "initial_risk": initial_risk,
        "mfe": mfe,
        "bars_since_entry": len(since),
    }

    if activation_reason is None:
        kept = current_sl_price if current_sl_price > 0 else (ref_sl or 0.0)
        return dict(
            common,
            new_structural_sl=kept,
            is_profit_locked=bool(kept) and ((kept > entry_price) if is_long else (kept < entry_price)),
            locked_roe_pct=round(((kept - entry_price) / entry_price * 100) if is_long else ((entry_price - kept) / entry_price * 100), 2) if kept else 0.0,
            should_update=False,
            reason="trail_not_activated",
        )

    if extreme is None:  # TP1 filled before any closed bar since entry: anchor on the last closed bar
        extreme = highs[-1] if is_long else lows[-1]
    be_allowed = tp1_filled is True or (atr_15m > 0 and mfe >= 2.0 * atr_15m)
    swing_lows, swing_highs = find_recent_swings(highs, lows, window=2)

    if is_long:
        recent_swing_low = swing_lows[-1] if swing_lows else min(lows[-6:])
        chandelier_stop = extreme - (1.8 * atr_15m)
        structural_level = recent_swing_low - (0.3 * atr_15m)
        candidate_stop = max(structural_level, chandelier_stop)

        # Right-tail preservation: True Net Break-Even only after >= 2.0x ATR expansion since entry or TP1 fill
        if be_allowed:
            candidate_stop = max(candidate_stop, entry_price * 1.002)

        # Do not allow stop loss to regress lower (unidirectional ratchet)
        if current_sl_price > 0 and candidate_stop <= current_sl_price:
            candidate_stop = current_sl_price

        # Keep the stop at least 0.5x ATR below price
        candidate_stop = min(candidate_stop, cur_p - (0.5 * atr_15m))
    else:
        recent_swing_high = swing_highs[-1] if swing_highs else max(highs[-6:])
        chandelier_stop = extreme + (1.8 * atr_15m)
        structural_level = recent_swing_high + (0.3 * atr_15m)
        candidate_stop = min(structural_level, chandelier_stop)

        if be_allowed:
            candidate_stop = min(candidate_stop, entry_price * 0.998)

        if current_sl_price > 0 and candidate_stop >= current_sl_price:
            candidate_stop = current_sl_price

        candidate_stop = max(candidate_stop, cur_p + (0.5 * atr_15m))

    rounded_stop = eft.round_price(candidate_stop, filters["tickSize"], filters["precision_price"])

    return dict(
        common,
        new_structural_sl=rounded_stop,
        chandelier_anchor=extreme,
        is_profit_locked=(rounded_stop > entry_price) if is_long else (rounded_stop < entry_price),
        locked_roe_pct=round(((rounded_stop - entry_price) / entry_price * 100) if is_long else ((entry_price - rounded_stop) / entry_price * 100), 2),
        should_update=(rounded_stop > current_sl_price) if is_long else (rounded_stop < current_sl_price and current_sl_price > 0),
        reason="activated",
    )


def resolve_trade_reference(symbol, position, direction, current_sl, target_env):
    """
    Reference (planned) SL and entry time used by the trailing activation gate.
    Primary: the latest logs/trades_audit.jsonl entry record for the symbol, accepted only when its direction
    matches, its target_env (if present) matches and its entry_price is within 0.5% of the position's entryPrice.
    Fallback: the current verified stop as planned SL and positionRisk updateTime as entry time.
    Returns (planned_sl, entry_ts, reference_source).
    """
    entry_p = float(position.get("entryPrice") or 0)

    def _update_ts():
        try:
            ut = int(float(position.get("updateTime") or 0))
        except (TypeError, ValueError):
            ut = 0
        return ut / 1000.0 if ut > 0 else None

    try:
        rec = eft.latest_trade_audit_record(symbol)
    except Exception:
        rec = None
    if isinstance(rec, dict) and entry_p > 0:
        try:
            rec_entry = float(rec.get("entry_price") or 0)
            rec_sl = float(rec.get("sl_price") or 0)
        except (TypeError, ValueError):
            rec_entry = rec_sl = 0.0
        rec_env = rec.get("target_env")
        if (str(rec.get("direction", "")).upper() == direction
                and (not rec_env or rec_env == target_env)
                and rec_entry > 0 and rec_sl > 0
                and abs(rec_entry - entry_p) / entry_p <= REFERENCE_ENTRY_TOLERANCE):
            try:
                entry_ts = float(rec["timestamp"]) if rec.get("timestamp") else None
            except (TypeError, ValueError):
                entry_ts = None
            if entry_ts is None:
                entry_ts = _update_ts()
            return rec_sl, entry_ts, "trade_audit"
    return (current_sl if current_sl > 0 else None), _update_ts(), "current_stop"


def update_position_to_structural_stop(symbol, target_env=None, dry_run=False, position=None):
    """
    Ratchets the Stop Loss of an open position to its structural level, strictly if it tightens risk.

    Guarantees:
      - PLACE-THEN-CANCEL: the new stop is placed and verified on /fapi/v1/openAlgoOrders before the old stop
        is cancelled; if it cannot be verified the old stop is kept and success is False.
      - Never loosens: a stop is only replaced by one strictly closer to price in the favourable direction.
      - success=True only when the position ends with a verified stop (existing or new).
      - dry_run=True computes the decision but never sends a write request.
      - YOLO positions are never trailed before TP1 fills (reason "yolo_before_tp1", no write).
      - Activation gate (Issue #95): the planned SL (latest matching trades_audit record, else the current stop)
        is kept until +1.0R or +2.0x ATR_15m since entry on closed 15m bars, or TP1 fill (reason
        "trail_not_activated", no write). Results carry activation_reason and reference_source.
      - Take-profit orders are never touched or re-based.
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

    # Trade reference first: TP1 state is only trusted when the audit record belongs to THIS position (a stale
    # record from a previous trade must never report tp1_filled, skip the YOLO gate or allow the BE ratchet).
    planned_sl, entry_ts, reference_source = resolve_trade_reference(symbol, position, direction, current_sl, target_env)
    tp1_filled, _ = eft.detect_tp1_filled(symbol, abs(amt))
    tp1_ok = tp1_filled if reference_source == "trade_audit" else None

    # YOLO: never trail before TP1 fills (right-tail preservation), whichever path calls this function.
    yolo, _ = eft.detect_yolo_position(symbol, leverage=position.get("leverage"))
    if yolo and tp1_ok is not True:
        return keep("yolo_before_tp1", "YOLO position: trailing deferred until TP1 fills (right-tail preservation).")

    calc = calculate_structural_stop(symbol, direction, entry_p, current_sl_price=current_sl, target_env=target_env,
                                     planned_sl=planned_sl, entry_ts=entry_ts, tp1_filled=tp1_ok,
                                     mark_price=mark_p or None, reference_source=reference_source)
    if not calc:
        return keep("structural_stop_unavailable", f"Could not calculate structural stop for {symbol}.")
    # Every later path (kept, tightened, dry run, unverified) reports why the trail is (not) active.
    base.update(activation_reason=calc.get("activation_reason"), reference_source=reference_source)

    if calc.get("reason") == "trail_not_activated":
        return keep("trail_not_activated",
                    f"Trailing not activated for {symbol}: planned SL kept (reference {planned_sl or 'none'} from "
                    f"{reference_source}; needs +{TRAIL_ACTIVATION_R:g}R or +{TRAIL_ACTIVATION_ATR:g}x ATR_15m "
                    f"on closed 15m bars since entry, or TP1 fill).")

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
    levels in profit if favorable market expansion has occurred. Each position goes through
    update_position_to_structural_stop, so the YOLO-before-TP1 and activation gates apply here too.
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
