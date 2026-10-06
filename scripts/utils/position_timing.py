#!/usr/bin/env python3
"""
position_timing.py - Shared holding-time and dead-alpha logic (issue #92).

One implementation used by the doctor's drift watchdog (scripts/trading_drift_watchdog.py), the ledger sync
(scripts/sync_session_state.py) and the position guardian (scripts/loops/position_guardian_loop.py), so they can
never disagree about how long a position has been open or whether its alpha is dead.

Entry time of the CURRENT position (resolve_entry_time), in order:
  1. Binance fills (GET /fapi/v1/userTrades?symbol=S&limit=1000): walking fills newest -> oldest from the current
     positionAmt, the first fill before which the position was flat or on the opposite side is the opening fill
     (handles add-ons, partial reduces and flips). Falls through when the window does not reach that fill (the
     endpoint only returns the last 7 days / 1000 fills), in hedge mode (positionSide != BOTH), on any API error
     dict (the MCP gateway does not map userTrades) or exception.
  2. logs/trades_audit.jsonl: latest entry record (event records skipped, total_qty required) for the symbol AND
     the same direction AND target_env, accepted only when it matches the live position (entry_price within 0.5%
     of positionRisk entryPrice AND total_qty >= |positionAmt|); a stale record of an earlier trade gives UNKNOWN.
     For resting entries (STOP_MARKET / LIMIT) that record is written when the fill is protected, so it is an upper
     bound on the fill time: the holding time is understated. A trades_audit-sourced verdict is report-only:
     autonomous closes require userTrades (is_autonomous_close_allowed).
  3. UNKNOWN (entry_ts None). Never "now" and never positionRisk updateTime (it moves on every fill/reduce/funding
     update, so it does not mark the opening of the position).

Dead alpha (assess_dead_alpha): held >= max_hours (default 4h) AND stagnant (mark within 1.2% of entry AND
|ROE| < 15%). An unknown holding time is verdict UNKNOWN: never dead alpha, never reported as 0.0h.
"""

import json
import os
import time
from decimal import Decimal, InvalidOperation

SOURCE_USER_TRADES = "userTrades"
SOURCE_TRADES_AUDIT = "trades_audit"
SOURCE_UNKNOWN = "UNKNOWN"

VERDICT_DEAD_ALPHA = "DEAD_ALPHA"
VERDICT_HEALTHY = "HEALTHY"
VERDICT_UNKNOWN = "UNKNOWN"

DEFAULT_MAX_HOURS = 4.0
STAGNANT_PRICE_DIFF_PCT = 1.2
STAGNANT_ROE_PCT = 15.0
USER_TRADES_LIMIT = 1000
AUDIT_ENTRY_PRICE_TOLERANCE_PCT = 0.5


def _dec(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _sign(d):
    return (d > 0) - (d < 0)


def entry_time_from_fills(fills, position_amt):
    """Opening time (int seconds) of the current position reconstructed from userTrades fills, or None.

    Walk newest -> oldest with pos_after starting at the current positionAmt and pos_before = pos_after - signed
    qty (BUY +, SELL -). The first fill whose pos_before is flat or on the opposite side opened the current
    position. None when the walk cannot be reconciled (window too short, hedge mode, malformed fill)."""
    amt = _dec(position_amt)
    if amt is None or amt == 0 or not isinstance(fills, list) or not fills:
        return None
    side = _sign(amt)
    rows = []
    for f in fills:
        if not isinstance(f, dict):
            return None
        if str(f.get("positionSide") or "BOTH").upper() != "BOTH":
            return None  # hedge mode: one-way reconstruction does not apply
        qty = _dec(f.get("qty"))
        t = f.get("time")
        fside = str(f.get("side", "")).upper()
        if qty is None or qty <= 0 or fside not in ("BUY", "SELL") or not isinstance(t, (int, float)) or isinstance(t, bool):
            return None
        rows.append((int(t), _dec(f.get("id")) or Decimal(0), qty if fside == "BUY" else -qty))
    rows.sort(key=lambda r: (r[0], r[1]))
    pos_after = amt
    for t, _, signed in reversed(rows):
        pos_before = pos_after - signed
        if pos_before == 0 or _sign(pos_before) != side:
            return t // 1000
        pos_after = pos_before
    return None


def latest_audit_entry_record(audit_path, symbol, direction, target_env=None):
    """Latest entry record in trades_audit.jsonl for symbol + direction (+ target_env when the record has one).
    Same semantics as execute_futures_trade.latest_trade_audit_record (event records skipped, total_qty required),
    plus the direction and environment filters."""
    if not audit_path or not os.path.exists(audit_path):
        return None
    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return _latest_from_records(records, symbol, direction, target_env)


def audit_record_matches_position(rec, entry_price, position_amt):
    """True when an audit record plausibly describes the CURRENT position (round 2 of issue #92): its entry_price is
    within AUDIT_ENTRY_PRICE_TOLERANCE_PCT of the positionRisk entryPrice AND its total_qty >= |positionAmt| (a TP1
    partial reduce keeps it valid). Otherwise it is a stale record of an earlier trade (manual reopen, MCP mode,
    late-recorded position) and must not date the current position."""
    try:
        live_entry = float(entry_price)
        rec_entry = float(rec.get("entry_price"))
        rec_qty = float(rec.get("total_qty"))
        amt = abs(float(position_amt))
    except (TypeError, ValueError, AttributeError):
        return False
    if live_entry <= 0 or rec_entry <= 0 or amt <= 0:
        return False
    if abs(rec_entry - live_entry) / live_entry * 100 > AUDIT_ENTRY_PRICE_TOLERANCE_PCT:
        return False
    return rec_qty >= amt - 1e-9


def resolve_entry_time(symbol, direction, position_amt, target_env, *, entry_price=None, fetch=None,
                       audit_path=None, audit_records=None):
    """Returns (entry_ts | None, source) for the current position, source in {userTrades, trades_audit, UNKNOWN}.

    fetch: callable(method, endpoint, params, target_env=...) like execute_futures_trade.send_signed_request.
    entry_price: positionRisk entryPrice; the latest matching audit record is accepted only when
    audit_record_matches_position (without entry_price the audit fallback is never used).
    audit_path: path to trades_audit.jsonl. audit_records: optional pre-parsed list (newest last) used instead of
    reading audit_path. Never returns now or updateTime.

    Snapshot race: positionRisk and userTrades are separate reads; a fill landing between them leaves the walk
    unreconciled (or reconciled against the new fill), and an unreconciled walk falls through like a short window."""
    if fetch is not None:
        try:
            fills = fetch("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": USER_TRADES_LIMIT},
                          target_env=target_env)
        except Exception:
            fills = None
        ts = entry_time_from_fills(fills, position_amt) if isinstance(fills, list) else None
        if ts:
            return ts, SOURCE_USER_TRADES
    rec = None
    if audit_records is not None:
        rec = _latest_from_records(audit_records, symbol, direction, target_env)
    elif audit_path:
        rec = latest_audit_entry_record(audit_path, symbol, direction, target_env)
    if rec and audit_record_matches_position(rec, entry_price, position_amt):
        try:
            ts = int(float(rec.get("timestamp")))
        except (TypeError, ValueError):
            ts = 0
        if ts > 0:
            return ts, SOURCE_TRADES_AUDIT
    return None, SOURCE_UNKNOWN


def is_autonomous_close_allowed(entry_time_source):
    """Autonomous dead-alpha closes (guardian --close-dead-alpha, watchdog --auto-exit) only when the holding time
    comes from Binance fills; a trades_audit-sourced verdict is report-only."""
    return entry_time_source == SOURCE_USER_TRADES


def _norm_env(env):
    """'prod' / 'testnet' for any env alias (mainnet, production, ...); None when empty; lower-cased when unknown."""
    if not env:
        return None
    try:
        from utils.env_resolver import resolve_env  # callers put scripts/ on sys.path
        return resolve_env(str(env))
    except Exception:
        return str(env).strip().lower()


def _latest_from_records(records, symbol, direction, target_env):
    symbol, direction = str(symbol).upper(), str(direction).upper()
    env = _norm_env(target_env)
    for rec in reversed(list(records or [])):
        if not isinstance(rec, dict) or rec.get("event") or "total_qty" not in rec:
            continue
        if str(rec.get("symbol", "")).upper() != symbol or str(rec.get("direction", "")).upper() != direction:
            continue
        rec_env = _norm_env(rec.get("target_env"))
        if env and rec_env and rec_env != env:
            continue
        return rec
    return None


def holding_hours(entry_ts, now_ts=None):
    """Hours since entry_ts (2 decimals) or None when the entry time is unknown."""
    if not entry_ts:
        return None
    now_ts = int(time.time()) if now_ts is None else now_ts
    return round(max(0, now_ts - int(entry_ts)) / 3600.0, 2)


def position_roe_pct(row):
    """ROE % of a positionRisk row: unRealizedProfit / margin, margin = isolatedMargin when > 0, else
    |positionAmt| * markPrice / leverage. 0.0 when no margin can be derived."""
    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    unpnl = f(row.get("unRealizedProfit"))
    margin = f(row.get("isolatedMargin"))
    if margin <= 0:
        lev = f(row.get("leverage"))
        notional = abs(f(row.get("positionAmt"))) * f(row.get("markPrice"))
        margin = notional / lev if lev > 0 else 0.0
    return (unpnl / margin * 100) if margin > 0 else 0.0


def assess_dead_alpha(*, elapsed_hours, entry_price, mark_price, roe_pct, max_hours=DEFAULT_MAX_HOURS):
    """Dead-alpha verdict shared by the watchdog and the guardian: overdue (elapsed >= max_hours) AND stagnant
    (price within 1.2% of entry AND |ROE| < 15%). elapsed None (unknown entry time) -> UNKNOWN, never dead alpha."""
    try:
        entry_price = float(entry_price)
        mark_price = float(mark_price)
    except (TypeError, ValueError):
        entry_price = mark_price = 0.0
    price_diff_pct = abs(mark_price - entry_price) / entry_price * 100 if entry_price > 0 else None
    roe_pct = float(roe_pct or 0.0)
    out = {"elapsed_hours": elapsed_hours, "max_hours": max_hours, "price_diff_pct": price_diff_pct,
           "roe_pct": roe_pct, "is_overdue": None, "is_stagnant": None, "is_dead_alpha": False,
           "verdict": VERDICT_UNKNOWN}
    if price_diff_pct is not None:
        out["is_stagnant"] = price_diff_pct < STAGNANT_PRICE_DIFF_PCT and abs(roe_pct) < STAGNANT_ROE_PCT
    if elapsed_hours is None or price_diff_pct is None:
        return out
    out["is_overdue"] = elapsed_hours >= max_hours
    out["is_dead_alpha"] = bool(out["is_overdue"] and out["is_stagnant"])
    out["verdict"] = VERDICT_DEAD_ALPHA if out["is_dead_alpha"] else VERDICT_HEALTHY
    return out
