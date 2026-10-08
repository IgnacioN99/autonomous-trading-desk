#!/usr/bin/env python3
"""
dynamic_exit_manager.py - Quantitative Dynamic Exit and Structural Trailing Stop Manager.
Replaces flat break-even with microstructural levels (15m Swing High/Low + Chandelier ATR),
preserving positive right-tail convexity and managing Alpha Decay (stalled momentum timeouts).

Activation gate (Issue #95): the planned Stop Loss is left untouched until the trade has earned a trail:
+1.0R of planned risk (TRAIL_ACTIVATION_R) or +2.0x ATR_15m (TRAIL_ACTIVATION_ATR) of favourable excursion
since entry on CLOSED 15m candles, or a TP1 fill. Once active, the Chandelier stop is anchored to the extreme
since entry (not the forming candle). Before +2.0x ATR_15m MFE or a TP1 fill an activated trail may tighten up
to one tick short of entry, never into the True Net BE dead zone (Issue #106); this gives up profit protection on
a fast reversal through entry (accepted to keep the stop out of the fee dead zone). YOLO positions are never
trailed before TP1; a YOLO position without a matching trades_audit record is never trailed (its TP1 fill is
unknown). TP1/TP2 are never re-based.

Profit lock (Issue #183): once True Net BE is allowed (verified TP1 fill or +2.0x ATR_15m) and R is known, the stop
is at least the R step reached by the MFE (profile exit_management; default 1R -> True Net BE, 2R -> +1R,
3R -> +2R, +1R per further full R). On a verified TP1 fill the lock MFE also uses the closed 1m bars of the forming
15m candle, the mark price and the TP1 price, so the lock moves in the same cycle. The 0.5x ATR floor still wins.

TP1 trust (Issue #163): a TP1 fill counts only when the matched trades_audit record is verified against the
Binance fills (userTrades open time). With "reference_unverified" (fills unavailable, e.g. MCP mode, where the
gateway does not serve userTrades, so every position) TP1 is unknown: no TP1 activation, True Net BE only via
+2.0x ATR_15m, and a YOLO position is never trailed. The planned SL and entry time still come from the record.

Stop updates are PLACE-THEN-CANCEL (execute_futures_trade.replace_protective_stop): the new stop is
verified on /fapi/v1/openAlgoOrders before the old one is cancelled, stops only ever tighten, and any
update whose resulting protection cannot be verified reports success=False. The current stops are re-read right
before a write (Issue #167: a --once run and the loop cannot replace each other's fresher stop).

Usage:
  python3 scripts/dynamic_exit_manager.py [--env prod|testnet] [--symbol BTCUSDT] [--dry-run] [--json]
The background loop scripts/loops/position_guardian_loop.py runs this logic on a schedule.
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
import user_profile
from utils.env_resolver import resolve_env
from utils import position_timing as pt
from utils import trade_excursion

BASE_FAPI = "https://fapi.binance.com"

# Trailing activation gate (Issue #95): the planned SL is kept until price has moved TRAIL_ACTIVATION_R x the
# initial risk, or TRAIL_ACTIVATION_ATR x ATR_15m, in favour since entry on CLOSED 15m bars, or TP1 has filled.
TRAIL_ACTIVATION_R = 1.0
TRAIL_ACTIVATION_ATR = 2.0
REFERENCE_ENTRY_TOLERANCE = 0.005  # trades_audit entry_price must be within 0.5% of the live entryPrice
REFERENCE_OPEN_SLACK_SECONDS = 300  # a trades_audit record older than the userTrades open time - 300s is stale

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


INTRABAR_KLINES_LIMIT = 16  # 1m bars of the forming 15m candle (weight 1), read only after a verified TP1 fill
_EPS = 1e-9


def _profit_lock_step(mfe_r, steps, extend_last_step):
    """(step_mfe_r, lock_r) of the highest step with mfe_r <= MFE_R (beyond the last step, +1.0R of lock per further
    full 1.0R of MFE when extend_last_step), or None below the first step."""
    hit = None
    for s in steps:
        if float(s["mfe_r"]) <= mfe_r + _EPS:
            hit = (float(s["mfe_r"]), float(s["lock_r"]))
    if hit is None:
        return None
    last = steps[-1]
    if extend_last_step and hit[0] == float(last["mfe_r"]):
        extra = int((mfe_r - hit[0]) + _EPS)
        if extra > 0:
            hit = (hit[0] + extra, hit[1] + extra)
    return hit


def _lock_mfe(is_long, entry_price, closed_mfe, *, tp1_filled, exit_management, intrabar_extreme, tp1_price, mark):
    """(mfe, source) used by the profit lock: the closed-15m MFE, raised on a verified TP1 fill (lock_on_tp1) by the
    intrabar 1m extreme, the audit tp1_price and the mark price. Values on the loss side or unparsable are ignored."""
    best, source = max(closed_mfe, 0.0), "closed_15m"
    if tp1_filled is not True or not exit_management.get("lock_on_tp1"):
        return best, source
    for name, value in (("intrabar_1m", intrabar_extreme), ("tp1_price", tp1_price), ("mark", mark)):
        try:
            price = float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            continue
        if not price > 0:
            continue
        exc = (price - entry_price) if is_long else (entry_price - price)
        if exc > best:
            best, source = exc, name
    return best, source


def calculate_structural_stop(symbol, direction, entry_price, current_sl_price=0.0, target_env=None, *,
                              planned_sl=None, entry_ts=None, tp1_filled=None, mark_price=None,
                              reference_source=None, intrabar_extreme=None, tp1_price=None, exit_management=None):
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
        or after TP1 fill. Before that (an r_multiple activation) the candidate is capped one tick short of entry
        (LONG <= entry - tick, SHORT >= entry + tick), never in the fee dead zone (Issue #106); a current stop
        already at or past entry (e.g. after --move-breakeven) is kept.
      - PROFIT LOCK (Issue #183, profile exit_management, see user_profile.get_exit_management): only once True Net
        BE is allowed and R exists, the candidate is tightened to the highest step with mfe_r <= MFE/R (default
        1R -> True Net BE, 2R -> +1R, 3R -> +2R, then +1R per further full R). The lock MFE is the closed-15m MFE;
        on a verified TP1 fill (tp1_filled True, lock_on_tp1) also intrabar_extreme (closed 1m bars of the
        forming 15m candle, read by the caller), tp1_price and mark_price. Result "profit_lock": None or
        {mfe_r, mfe_source, step_mfe_r, lock_r, lock_price, capped_by_price_floor, binding}. Before BE is allowed
        the #106 cap above is unchanged. exit_management None loads the profile.
      - Never loosens against current_sl_price; never closer than 0.5x ATR to price (mark_price if given, else
        the last closed close); this floor may cap the profit lock (capped_by_price_floor).
    Take-profit orders are never re-based: TP1/TP2 keep their original levels.
    """
    target_env = resolve_env(target_env)
    filters = eft.get_symbol_filters(symbol, target_env=target_env)
    if not filters:
        return None

    k15m = get_klines_data(symbol, interval="15m", limit=99)  # limit < 100: request weight 1 (Issue #108)
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
            profit_lock=None,
        )

    if extreme is None:  # TP1 filled before any closed bar since entry: anchor on the last closed bar
        extreme = highs[-1] if is_long else lows[-1]
    be_allowed = tp1_filled is True or (atr_15m > 0 and mfe >= 2.0 * atr_15m)
    tick = filters["tickSize"]

    # Profit lock (Issue #183): only once True Net BE is allowed and R exists.
    profit_lock = None
    if be_allowed and initial_risk is not None and exit_management is None:
        exit_management = user_profile.get_exit_management()
    if be_allowed and initial_risk is not None and exit_management.get("profit_lock_enabled"):
        lock_mfe, lock_source = _lock_mfe(is_long, entry_price, mfe, tp1_filled=tp1_filled,
                                          exit_management=exit_management, intrabar_extreme=intrabar_extreme,
                                          tp1_price=tp1_price, mark=mark)
        mfe_r = lock_mfe / initial_risk
        step = _profit_lock_step(mfe_r, exit_management.get("profit_lock_steps") or [],
                                 bool(exit_management.get("extend_last_step")))
        if step is not None:
            step_mfe_r, lock_r = step
            if lock_r <= 0:
                lock_price = entry_price * ((1 + eft.TRUE_NET_BE_FEE_BUFFER) if is_long
                                            else (1 - eft.TRUE_NET_BE_FEE_BUFFER))
            else:
                lock_price = (entry_price + lock_r * initial_risk) if is_long else (entry_price - lock_r * initial_risk)
            profit_lock = {"mfe_r": round(mfe_r, 4), "mfe_source": lock_source, "step_mfe_r": step_mfe_r,
                           "lock_r": lock_r, "lock_price": lock_price, "capped_by_price_floor": False,
                           "binding": False}
    swing_lows, swing_highs = find_recent_swings(highs, lows, window=2)

    if is_long:
        recent_swing_low = swing_lows[-1] if swing_lows else min(lows[-6:])
        chandelier_stop = extreme - (1.8 * atr_15m)
        structural_level = recent_swing_low - (0.3 * atr_15m)
        candidate_stop = max(structural_level, chandelier_stop)

        # Right-tail preservation: True Net Break-Even only after >= 2.0x ATR expansion since entry or TP1 fill;
        # before that the stop stays at least one tick below entry (never in the fee dead zone, Issue #106).
        if be_allowed:
            candidate_stop = max(candidate_stop, entry_price * 1.002)
        else:
            candidate_stop = min(candidate_stop, eft.round_price(Decimal(str(entry_price)) - Decimal(str(tick)),
                                                                 tick, filters["precision_price"]))

        if profit_lock:  # Issue #183: the tighter of the trail and the R step lock
            candidate_stop = max(candidate_stop, profit_lock["lock_price"])

        # Do not allow stop loss to regress lower (unidirectional ratchet)
        if current_sl_price > 0 and candidate_stop <= current_sl_price:
            candidate_stop = current_sl_price

        # Keep the stop at least 0.5x ATR below price (safety wins over the profit lock)
        candidate_stop = min(candidate_stop, cur_p - (0.5 * atr_15m))
        if profit_lock:
            profit_lock["capped_by_price_floor"] = candidate_stop < profit_lock["lock_price"]
    else:
        recent_swing_high = swing_highs[-1] if swing_highs else max(highs[-6:])
        chandelier_stop = extreme + (1.8 * atr_15m)
        structural_level = recent_swing_high + (0.3 * atr_15m)
        candidate_stop = min(structural_level, chandelier_stop)

        if be_allowed:
            candidate_stop = min(candidate_stop, entry_price * 0.998)
        else:  # one tick above entry, rounded like every stop here (round_price), so still strictly above it
            candidate_stop = max(candidate_stop, eft.round_price(Decimal(str(entry_price)) + Decimal(str(tick)),
                                                                 tick, filters["precision_price"]))

        if profit_lock:
            candidate_stop = min(candidate_stop, profit_lock["lock_price"])

        if current_sl_price > 0 and candidate_stop >= current_sl_price:
            candidate_stop = current_sl_price

        candidate_stop = max(candidate_stop, cur_p + (0.5 * atr_15m))
        if profit_lock:
            profit_lock["capped_by_price_floor"] = candidate_stop > profit_lock["lock_price"]

    if profit_lock:  # the lock set the stop (not a tighter trail / current stop, not the price floor)
        profit_lock["binding"] = candidate_stop == profit_lock["lock_price"]
    rounded_stop = eft.round_price(candidate_stop, filters["tickSize"], filters["precision_price"])

    return dict(
        common,
        new_structural_sl=rounded_stop,
        chandelier_anchor=extreme,
        is_profit_locked=(rounded_stop > entry_price) if is_long else (rounded_stop < entry_price),
        locked_roe_pct=round(((rounded_stop - entry_price) / entry_price * 100) if is_long else ((entry_price - rounded_stop) / entry_price * 100), 2),
        should_update=(rounded_stop > current_sl_price) if is_long else (rounded_stop < current_sl_price and current_sl_price > 0),
        reason="activated",
        profit_lock=profit_lock,
    )


def _resolve_trade_reference_full(symbol, position, direction, current_sl, target_env, *, fetch=None):
    """resolve_trade_reference plus the matched audit record (or None) and the warnings list. See
    resolve_trade_reference for the rules. fetch: send_signed_request-like callable for the userTrades read (default
    eft.send_signed_request, looked up at call time); userTrades is only read once a candidate record matched."""
    return _resolve_trade_reference_with_candidate(symbol, position, direction, current_sl, target_env,
                                                   fetch=fetch)[:5]


def _resolve_trade_reference_with_candidate(symbol, position, direction, current_sl, target_env, *, fetch=None):
    """_resolve_trade_reference_full plus the candidate record: the newest same symbol / direction / env entry record,
    whether it matched the position or not (None when the audit file is missing or unreadable, or when userTrades
    proves the record older than the position, i.e. an earlier trade). Issue #172: the
    YOLO gate reads its is_yolo when no record matched."""
    entry_p = float(position.get("entryPrice") or 0)
    warnings = []
    rec = None

    def _update_ts():
        try:
            ut = int(float(position.get("updateTime") or 0))
        except (TypeError, ValueError):
            ut = 0
        return ut / 1000.0 if ut > 0 else None

    def _fallback():
        return (current_sl if current_sl > 0 else None), _update_ts(), "current_stop", None, warnings, rec

    # Tail-bounded read (issue #94 reader); unreadable / corrupt files are reported, never silent (Issue #107.3).
    path = os.path.join(eft._workspace_dir(), "logs", "trades_audit.jsonl")
    records = []
    if os.path.exists(path):
        stats = {}
        try:
            records = eft.read_audit_tail(path, stats=stats)
        except OSError:
            warnings.append("audit_unreadable")
            return _fallback()
        if stats.get("malformed_lines"):
            warnings.append(f"audit_corrupt_lines:{stats['malformed_lines']}")

    # Latest entry record for symbol + direction + env (env aliases normalised like position_timing), then it
    # must describe the CURRENT position: entry within tolerance and total_qty >= |positionAmt|.
    rec = pt._latest_from_records(records, symbol, direction, target_env)
    if not isinstance(rec, dict) or entry_p <= 0:
        return _fallback()
    try:
        rec_entry = float(rec.get("entry_price") or 0)
        rec_sl = float(rec.get("sl_price") or 0)
    except (TypeError, ValueError):
        rec_entry = rec_sl = 0.0
    if (rec_entry <= 0 or rec_sl <= 0
            or abs(rec_entry - entry_p) / entry_p > REFERENCE_ENTRY_TOLERANCE
            or not pt.audit_record_matches_position(rec, entry_p, position.get("positionAmt"))):
        return _fallback()
    try:
        entry_ts = float(rec["timestamp"]) if rec.get("timestamp") else None
    except (TypeError, ValueError):
        entry_ts = None

    # Stale-record check against the position's real open time (Binance fills only, no audit fallback).
    open_ts, source = pt.resolve_entry_time(symbol, direction, position.get("positionAmt"), target_env,
                                            entry_price=entry_p, fetch=fetch or eft.send_signed_request)
    if source == pt.SOURCE_USER_TRADES and open_ts:
        if entry_ts is None or entry_ts < float(open_ts) - REFERENCE_OPEN_SLACK_SECONDS:
            return _fallback()[:5] + (None,)  # proven to belong to an earlier trade: not a YOLO candidate either
    else:
        warnings.append("reference_unverified")
    if entry_ts is None:
        entry_ts = _update_ts()
    return rec_sl, entry_ts, "trade_audit", rec, warnings, rec


def resolve_trade_reference(symbol, position, direction, current_sl, target_env):
    """
    Reference (planned) SL and entry time used by the trailing activation gate.
    Primary: the latest logs/trades_audit.jsonl entry record (last AUDIT_TAIL_BYTES only) for the symbol AND the
    same direction AND target_env (aliases normalised; a record without target_env matches), accepted only when
    its sl_price is set, its entry_price is within 0.5% of the position's entryPrice and its total_qty >=
    |positionAmt| (position_timing.audit_record_matches_position). Issue #107: when Binance fills (userTrades) give
    the position's open time, a record older than open time - REFERENCE_OPEN_SLACK_SECONDS belongs to an earlier
    trade and is rejected; when fills are unavailable (MCP, API error) the record is kept with the warning
    "reference_unverified". An unreadable audit file gives "audit_unreadable", skipped malformed lines
    "audit_corrupt_lines:<n>" (warnings only, never a blocking error).
    Fallback: the current verified stop as planned SL and positionRisk updateTime as entry time.
    Returns (planned_sl, entry_ts, reference_source).
    """
    return _resolve_trade_reference_full(symbol, position, direction, current_sl, target_env)[:3]


def _position_is_flat(symbol, target_env):
    """True only when a fresh GET /fapi/v2/positionRisk for `symbol` reads, has at least one row for the symbol, and
    every such row carries a parsable positionAmt of zero. A read error, an empty list, a row without a symbol, no row
    for the symbol, a missing or non-numeric positionAmt or any other unexpected payload gives False (the caller
    re-protects)."""
    try:
        rows = eft.send_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, target_env=target_env)
    except Exception:
        return False
    if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("symbol") for r in rows):
        return False
    mine = [r for r in rows if str(r.get("symbol") or "").upper() == symbol]
    if not mine:
        return False
    for r in mine:
        try:
            if float(r["positionAmt"]) != 0:
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


def update_position_to_structural_stop(symbol, target_env=None, dry_run=False, position=None, *, fetch=None):
    """
    Ratchets the Stop Loss of an open position to its structural level, strictly if it tightens risk.

    Guarantees:
      - PLACE-THEN-CANCEL: the new stop is placed and verified on /fapi/v1/openAlgoOrders before the old stop
        is cancelled; if it cannot be verified the old stop is kept and success is False.
      - Re-read before replace (Issue #167): right before a write the stops are queried again; a read error keeps
        everything (reason "stops_requery_failed", plus a "stops_requery_failed:<error>" warning, issue #172), a
        fresher stop already at or beyond the new level gives "not_tighter", else the fresh stops are the ones
        replaced. Issue #172: an empty re-read re-queries positionRisk for the symbol (weight 5, only on this path);
        a flat position gives success=True, reason "position_closed" and no write; a read error or a still-open
        position is re-protected.
      - Never loosens: a stop is only replaced by one strictly closer to price in the favourable direction.
      - success=True only when the position ends with a verified stop (existing or new).
      - dry_run=True computes the decision but never sends a write request.
      - YOLO positions are never trailed before TP1 fills (reason "yolo_before_tp1", no write, with yolo_source).
        TP1 is read from the matched trade reference only, so a YOLO position without a matching record is not
        trailed; YOLO detection never falls back to the newest raw audit record (audit_fallback=False). Issue #172:
        with no matched record, the newest same symbol / direction / env record's is_yolo still marks the position
        YOLO (yolo_source "trade_audit_unmatched"; a false positive only keeps the planned SL).
      - TP1 counts only for a verified reference (Issue #163): with "reference_unverified" (no userTrades open time;
        MCP mode: every position) tp1_filled is None, so no TP1 activation, no BE via TP1, no YOLO trail.
      - is_yolo, yolo_source, tp1_filled (the trusted value): every result from the stop query on.
      - warnings (every result after the stop query; the early returns position_query_failed / no_position /
        orders_query_failed carry none): trade-reference notices from resolve_trade_reference
        ("reference_unverified", "audit_unreadable", "audit_corrupt_lines:<n>"), plus old-stop cancel errors when
        tightened.
      - Activation gate (Issue #95): the planned SL (latest matching trades_audit record, else the current stop)
        is kept until +1.0R or +2.0x ATR_15m since entry on closed 15m bars, or TP1 fill (reason
        "trail_not_activated", no write). Results carry activation_reason and reference_source. Before +2.0x
        ATR_15m MFE or a TP1 fill an activated trail stays one tick short of entry: it gives up profit protection
        on a fast reversal through entry (accepted to keep the stop out of the fee dead zone).
      - Profit lock (Issue #183, profile exit_management): once True Net BE is allowed the stop is tightened to the
        R step reached by the MFE (calculate_structural_stop). On a verified TP1 fill only (tp1_filled True,
        lock_on_tp1) one 1m klines read (trade_excursion.fetch_klines_range, limit 16, weight 1) of the forming
        15m candle, plus the mark price and the record's tp1_price, raise the lock MFE in the same call; a failed
        read adds "intrabar_unavailable" and never blocks the trail. MCP / reference_unverified: tp1_filled is
        None, so no TP1 lock and no 1m read; closed-15m steps still apply after +2.0x ATR_15m when R exists.
        Results from the calc on carry "profit_lock" (dict or None); exit_management profile warnings join
        "warnings". The stop still changes only through replace_protective_stop and the #167 re-read.
      - Take-profit orders are never touched or re-based.
    `position` (a positionRisk row) may be passed to avoid re-querying. `fetch` (send_signed_request-like) is used
    for the userTrades read only (the guardian shares one read per symbol per cycle); no userTrades call is made
    without a candidate audit record matching the position's entry price and size.
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
    base.update(previous_sl=current_sl, current_sl=current_sl)

    def keep(reason, message):
        # No write: success mirrors whether a verified stop (the latest read) protects the position.
        protected_now = base["current_sl"] > 0
        out = dict(base, success=protected_now, reason=reason, message=message)
        if not protected_now:
            out["error"] = f"{message} Position has NO verified stop."
        return out

    # Trade reference first: TP1 state is only trusted when the audit record belongs to THIS position (a stale
    # record from a previous trade must never report tp1_filled, skip the YOLO gate or allow the BE ratchet).
    planned_sl, entry_ts, reference_source, ref_rec, ref_warnings, candidate = _resolve_trade_reference_with_candidate(
        symbol, position, direction, current_sl, target_env, fetch=fetch)
    base["warnings"] = list(ref_warnings)
    # TP1 and YOLO read the SAME matched record (Issue #107); without one, TP1 is unknown. Issue #163: an
    # unverified record (no userTrades open time) may be a stale same-direction trade, so its TP1 is unknown too.
    tp1_filled, _ = eft.detect_tp1_filled(symbol, abs(amt), record=ref_rec) if ref_rec else (None, None)
    tp1_ok = (tp1_filled if (reference_source == "trade_audit" and "reference_unverified" not in ref_warnings)
              else None)

    # YOLO: never trail before TP1 fills (right-tail preservation), whichever path calls this function.
    yolo, yolo_source = eft.detect_yolo_position(symbol, leverage=position.get("leverage"), record=ref_rec,
                                                 audit_fallback=False)
    if not yolo and ref_rec is None and isinstance(candidate, dict) and eft._truthy(candidate.get("is_yolo")):
        # Issue #172: no matched record (slipped fill, qty mismatch) but the newest same symbol / direction / env
        # record is YOLO. A false positive only keeps the planned SL.
        yolo, yolo_source = True, "trade_audit_unmatched"
    base.update(is_yolo=bool(yolo), yolo_source=yolo_source, tp1_filled=tp1_ok)
    if yolo and tp1_ok is not True:
        return keep("yolo_before_tp1", "YOLO position: trailing deferred until TP1 fills (right-tail preservation).")

    # Issue #183: profit-lock settings; on a verified TP1 fill only, one 1m klines read (weight 1) of the forming 15m
    # candle gives the intrabar MFE (the closed 15m bars cover the rest). A failed read never blocks the trail.
    exit_mgmt = user_profile.get_exit_management()
    base["warnings"].extend(exit_mgmt["warnings"])
    intrabar_extreme = None
    tp1_price = ref_rec.get("tp1_price") if (tp1_ok is True and isinstance(ref_rec, dict)) else None
    if tp1_ok is True and exit_mgmt["profit_lock_enabled"] and exit_mgmt["lock_on_tp1"] and entry_ts is not None:
        now_ms = int(time.time() * 1000)
        start_ms = max(trade_excursion.first_post_entry_bar_ms(entry_ts), now_ms // (15 * 60_000) * (15 * 60_000))
        try:
            bars = trade_excursion.fetch_klines_range(symbol, "1m", start_ms, limit=INTRABAR_KLINES_LIMIT,
                                                      target_env=target_env)
            closed_1m = [k for k in bars if float(k[0]) + trade_excursion.BAR_MS <= now_ms]  # drop the forming bar
            if closed_1m:
                intrabar_extreme = (max(float(k[2]) for k in closed_1m) if is_long
                                    else min(float(k[3]) for k in closed_1m))
        except Exception:
            base["warnings"].append("intrabar_unavailable")

    calc = calculate_structural_stop(symbol, direction, entry_p, current_sl_price=current_sl, target_env=target_env,
                                     planned_sl=planned_sl, entry_ts=entry_ts, tp1_filled=tp1_ok,
                                     mark_price=mark_p or None, reference_source=reference_source,
                                     intrabar_extreme=intrabar_extreme, tp1_price=tp1_price,
                                     exit_management=exit_mgmt)
    if not calc:
        return keep("structural_stop_unavailable", f"Could not calculate structural stop for {symbol}.")
    # Every later path (kept, tightened, dry run, unverified) reports why the trail is (not) active.
    base.update(activation_reason=calc.get("activation_reason"), reference_source=reference_source,
                profit_lock=calc.get("profit_lock"))
    lock = calc.get("profit_lock")
    lock_note = ""
    if isinstance(lock, dict) and lock.get("binding"):
        level = "True Net BE" if not lock["lock_r"] else f"+{lock['lock_r']:g}R"
        lock_note = f" (profit lock {level} at MFE {lock['mfe_r']:.2f}R)"

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
        out = keep("dry_run", f"DRY RUN: would tighten {symbol} stop from {current_sl or 'none'} to {new_sl}{lock_note}.")
        out["planned_sl"] = new_sl
        return out

    # Issue #167: re-read the stops right before writing, so an overlapping --once run or loop that already
    # tightened (or replaced) the stop is neither loosened nor left with a duplicate.
    fresh, err = eft.get_open_stop_orders(symbol, exit_side, target_env=target_env)
    if err:
        # Issue #172: success stays True (the first read's stop still protects) but the failure is visible.
        base["warnings"].append(f"stops_requery_failed:{err}")
        return keep("stops_requery_failed",
                    f"Cannot re-read current stops for {symbol} before replacing ({err}); nothing changed.")
    if not fresh and _position_is_flat(symbol, target_env):
        # Issue #172: the stop vanished because it triggered; placing a new one on a flat position would fail and
        # read as "unprotected".
        return dict(base, success=True, reason="position_closed", current_sl=0.0,
                    message=f"{symbol} position closed (its stop triggered) before the trailing write; nothing placed.")
    fresh_sl = eft._trigger_price(eft.tightest_stop(fresh, is_long)) if fresh else 0.0
    base["current_sl"] = fresh_sl
    if not eft.is_tighter_stop(new_sl, fresh_sl, is_long):
        return keep("not_tighter",
                    f"Active stop ({fresh_sl}) is already optimal or tighter than structural level ({new_sl}). Preserved unchanged.")

    filters = eft.get_symbol_filters(symbol, target_env=target_env) or {}
    rep = eft.replace_protective_stop(symbol, exit_side, new_sl, qty, fresh, target_env=target_env,
                                      tick_size=filters.get("tickSize"))
    if not rep["success"]:
        msg = (f"New structural stop {new_sl} for {symbol} could not be verified ({rep.get('placement')}). "
               + (f"Previous stop {fresh_sl} kept; nothing cancelled." if fresh_sl > 0 else "Position has NO verified stop."))
        return dict(base, success=False, reason="new_stop_unverified", error=msg, message=msg)

    warnings = base["warnings"] + [f"Old stop {ce['algo_id']} could not be cancelled ({ce['error']})."
                                   for ce in rep["cancel_errors"]]
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
        message=f"🛡️ Stop Loss updated to structural level: {new_sl} ({'+' if calc['is_profit_locked'] else ''}{calc['locked_roe_pct']}% ROE protected){lock_note}.",
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
        sym = args.symbol.upper()
        try:
            res = update_position_to_structural_stop(sym, target_env=target_env, dry_run=args.dry_run)
        except Exception as e:  # same structured failure as audit_and_trail_all_positions (Issue #108)
            res = {"symbol": sym, "success": False, "updated": False, "reason": "exception", "error": str(e), "message": f"Error: {e}"}
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
