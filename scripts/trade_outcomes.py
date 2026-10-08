#!/usr/bin/env python3
"""
trade_outcomes.py - Per-trade exits, realized R and MFE / MAE reconstructed from Binance fills (issue #182).

Read-only: it never places, changes or cancels orders. The only signed request is GET /fapi/v1/userTrades (through
execute_futures_trade.send_signed_request, the executor's client path); MFE / MAE come from public 1m klines
(utils/trade_excursion.fetch_klines_range).

Inputs:
  - Entries: non-event records of logs/trades_audit.jsonl with entry_price, sl_price and total_qty, for --env (a
    record without target_env matches), at or after --since, optionally one --symbol.
  - Fills: userTrades per symbol, startTime / endTime windows of at most 7 days from the earliest entry - 90 min to
    now, limit 1000; a window returning 1000 fills is split in halves until complete (independent of the reply's
    order; deduplicated by fill id), at most MAX_SPLIT_DEPTH (12) halvings and MAX_REQUESTS_PER_SYMBOL (200)
    requests: beyond that the symbol's rows get "truncated": true and the result a warning. A non-list reply (MCP
    mode: the gateway does not serve userTrades; auth or API error) marks that symbol's trades "fills_unavailable".
  - Trailed stops: successful, non-dry-run trail_stop actions (new_sl) of logs/guardian_actions.jsonl. Other stop
    moves (--move-breakeven, the night cutoff ratchet) are not logged as trail_stop: their fills label as
    BREAKEVEN (near the True Net BE level) or MANUAL_OR_OTHER.
  - Tick sizes: one public, unsigned GET /fapi/v1/exchangeInfo per run (exit_policy_sim.fetch_exchange_info); when
    it fails the stop tolerance is the percent one only and the entry qty match uses the relative tolerance only.

Entry fills (entry_match), in order: (a) "order_id": the fills of entry_order_id; (b) "side_qty_window" (the record
has an entry_order_id that no fill carries, e.g. a STOP_MARKET entry whose audit stores the algoId while userTrades
reports the child orderId): the entry-side, zero-realizedPnl fills grouped by orderId whose group qty matches
total_qty (1e-6 relative, or one stepSize) and whose first fill lies in [max(previous same-symbol audit timestamp,
timestamp - 90 min), timestamp + 60 s], the group closest in time to the audit timestamp, never an order already
matched to another record; (c) else status "no_entry_fill" (no legs, filled_qty null, excluded from the summary;
such a record consumes no fill and still bounds earlier same-direction trades at its timestamp - 120 s); (d) a
record without entry_order_id keeps the legacy entry time, audit timestamp - 120 s ("legacy"). Matched entry orders
are never exit legs of another trade. Closing fills (side opposite to the entry, same positionSide or BOTH, at or
after the entry fill, before the next audit entry of the same symbol + direction) are consumed in time order until
filled_qty is closed (1e-6 relative; a fill shared by two trades is split); otherwise the trade stays "open".
filled_qty = the matched entry fills' qty when found, else the audit total_qty (a partly filled, then cancelled
LIMIT entry closes on what filled). entry_vwap = VWAP of the matched entry fills (null without them); the R basis is
entry_vwap when present, else the audit entry_price. Leg reasons: orderId == tp1_order_id -> TP1, == tp2_order_id
-> TP2; else the nearest of these levels within its tolerance: sl_price and each trail_stop new_sl placed between
entry and the fill (max(0.3% of the price, 2 x tickSize)) -> SL / TRAILED_STOP, True Net BE entry x (1 + 0.002)
(LONG; 1 - 0.002 SHORT; +/- 0.15 % of the price) -> BREAKEVEN; else MANUAL_OR_OTHER (stop fills are market child
orders without the algo id, so stops are matched by price).
  realized_r_gross = sum(qty_i x signed(price_i - entry)) / (risk x filled_qty), risk = |entry - sl| (SL on the loss
  side, else null); realized_r_net = sum(realizedPnl - commission) over the closing legs and the matched entry fills
  / (risk x filled_qty), null unless every commission is in USDT (entry_commission_included says whether the entry
  fills were identified). mfe_r / mae_r / mfe_ts from 1m klines after the fill minute whose bar closed by the exit
  (no print after the exit) plus the leg prices; giveback_r = mfe_r - realized_r_gross. entry_ts, exit_ts, mfe_ts and
  leg times are in ms.
summarize_closed_today (used by sync_session_state.py): the same matching on the day's fills already fetched (no
request, no klines): per-trade closed / wins / losses / scratches (|R| < 0.05) and R sums; an entry before the day
counts with partial_history (today's legs, audit entry).

Output: logs/trade_outcomes.jsonl (rewritten atomically each run, one JSON line per trade, each stamped with the
run's "env" and "since" so offline readers such as trading_scorecard.py can filter and date it; --output must
resolve inside logs/, else exit 2) and with --json {"ok", "env", "since", "trades", "closed", "summary", "warnings",
"output"}. Exit code 0 when at least one trade was resolved (or there is no trade in range), 1 when no symbol's
fills were readable, 2 on a bad --output.

Usage:
  python3 scripts/trade_outcomes.py [--since YYYY-MM-DD] [--symbol S] [--env prod|testnet] [--json] [--no-klines]
      [--output PATH]
"""

import argparse
import datetime
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
from utils.env_resolver import resolve_env
from utils import position_timing as pt
from utils import trade_excursion

USER_TRADES = "/fapi/v1/userTrades"
FILLS_LIMIT = 1000
WINDOW_MS = 7 * 24 * 3600 * 1000
ENTRY_MATCH_LOOKBACK_MS = 5400 * 1000  # side + qty entry match: first fill at most 90 min before the audit write
ENTRY_MATCH_AHEAD_MS = 60 * 1000  # ... and at most 60 s after it
ENTRY_LOOKBACK_MS = ENTRY_MATCH_LOOKBACK_MS  # fills are fetched from the earliest entry minus this
ENTRY_FILL_SLACK_MS = 120 * 1000
PRICE_TOLERANCE = 0.003
TICK_TOLERANCE = 2  # stop / trail tolerance is at least this many ticks
BREAKEVEN_BAND = 0.002  # True Net BE level: entry x (1 + 0.002) on the favourable side
BREAKEVEN_MATCH_BAND = 0.0015  # a fill within +/- 0.15 % of the price of that level is BREAKEVEN
SCRATCH_R = 0.05  # closed-today summary: |R| below this is a scratch
MAX_SPLIT_DEPTH = 12  # userTrades window halvings per 7-day window
MAX_REQUESTS_PER_SYMBOL = 200
QTY_TOLERANCE = 1e-6
KLINES_LIMIT = 1500
KLINES_TIMEOUT_SECONDS = 6  # offline CLI: longer than the guardian's 2 s
DEFAULT_SINCE_DAYS = 7
REASONS = ("TP1", "TP2", "SL", "TRAILED_STOP", "BREAKEVEN", "MANUAL_OR_OTHER")


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _logs_dir():
    return os.path.join(eft._workspace_dir(), "logs")


def _read_jsonl(path):
    """JSON-object lines of path ([] when missing); malformed lines are skipped."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _write_jsonl_atomic(path, rows):
    """Rewrites path with one JSON line per row: temp file in the same directory, fsync, os.replace."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{os.path.basename(path)}.tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _is_entry_record(rec, env):
    """True for a non-event audit record of env (or without target_env) with entry_price, sl_price, total_qty,
    timestamp, a LONG / SHORT direction and a symbol."""
    if not isinstance(rec, dict) or rec.get("event"):
        return False
    entry, sl, qty = _num(rec.get("entry_price")), _num(rec.get("sl_price")), _num(rec.get("total_qty"))
    if not entry or entry <= 0 or sl is None or sl <= 0 or not qty or qty <= 0 or not _num(rec.get("timestamp")):
        return False
    if str(rec.get("direction") or "").upper() not in ("LONG", "SHORT"):
        return False
    rec_env = pt.norm_env(rec.get("target_env"))
    if rec_env and rec_env != env:
        return False
    return bool(str(rec.get("symbol") or "").strip())


def load_entries(env, since_ts, symbol=None):
    """Entry records of logs/trades_audit.jsonl for env since since_ts (seconds), oldest first."""
    out = []
    for rec in _read_jsonl(os.path.join(_logs_dir(), "trades_audit.jsonl")):
        if not _is_entry_record(rec, env):
            continue
        sym = str(rec.get("symbol") or "").upper()
        if _num(rec.get("timestamp")) < since_ts or (symbol and sym != symbol):
            continue
        out.append(rec)
    out.sort(key=lambda r: _num(r.get("timestamp"), 0))
    return out


def load_filters(env):
    """{SYMBOL: {"tickSize", "stepSize", ...}} from one public, unsigned exchangeInfo read; {} when it fails (percent
    tolerances only)."""
    try:
        import exit_policy_sim
        return exit_policy_sim.parse_filters(exit_policy_sim.fetch_exchange_info(env))
    except Exception:
        return {}


def load_trail_stops(env):
    """{SYMBOL: [(ts_s, new_sl)]} of successful, non-dry-run trail_stop actions for env."""
    out = {}
    for rec in _read_jsonl(os.path.join(_logs_dir(), "guardian_actions.jsonl")):
        if rec.get("type") != "trail_stop" or rec.get("success") is not True or rec.get("dry_run"):
            continue
        if pt.norm_env(rec.get("env")) not in (None, env):
            continue
        new_sl = _num((rec.get("detail") or {}).get("new_sl"))
        ts = _num(rec.get("timestamp"))
        if new_sl and ts:
            out.setdefault(str(rec.get("symbol") or "").upper(), []).append((ts, new_sl))
    return out


def _fetch_window(symbol, start_ms, end_ms, env, seen, budget, depth=0):
    """Adds the userTrades of [start_ms, end_ms] to seen (by id); None or the error text. A full window (FILLS_LIMIT
    rows) is split in half and both halves fetched (down to 1 ms), so paging never depends on the reply's order;
    beyond MAX_SPLIT_DEPTH halvings or MAX_REQUESTS_PER_SYMBOL requests (budget["requests"]) it stops and sets
    budget["truncated"]."""
    if budget["requests"] >= MAX_REQUESTS_PER_SYMBOL:
        budget["truncated"] = True
        return None
    budget["requests"] += 1
    params = {"symbol": symbol, "startTime": int(start_ms), "endTime": int(end_ms), "limit": FILLS_LIMIT}
    try:
        res = eft.send_signed_request("GET", USER_TRADES, params, target_env=env)
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:200]
    if not isinstance(res, list):
        return str(res)[:200]
    for f in res:
        if isinstance(f, dict):
            seen[str(f.get("id"))] = f
    if len(res) >= FILLS_LIMIT and end_ms > start_ms:
        if depth >= MAX_SPLIT_DEPTH:
            budget["truncated"] = True
            return None
        mid = (int(start_ms) + int(end_ms)) // 2
        return (_fetch_window(symbol, start_ms, mid, env, seen, budget, depth + 1)
                or _fetch_window(symbol, mid + 1, end_ms, env, seen, budget, depth + 1))
    return None


def fetch_fills(symbol, start_ms, end_ms, env, budget=None):
    """(fills sorted by time, None) or (None, error text): userTrades in windows of at most 7 days; a window that
    returns FILLS_LIMIT fills is split in halves until each part is complete (or 1 ms wide), within the request cap;
    deduplicated by id. budget (optional dict) receives "requests" and "truncated"."""
    seen = {}
    budget = {} if budget is None else budget
    budget.setdefault("requests", 0)
    budget.setdefault("truncated", False)
    window_start = int(start_ms)
    while window_start <= end_ms:
        window_end = min(window_start + WINDOW_MS - 1, int(end_ms))
        err = _fetch_window(symbol, window_start, window_end, env, seen, budget)
        if err is not None:
            return None, err
        window_start = window_end + 1
    fills = sorted(seen.values(), key=lambda f: (int(_num(f.get("time"), 0)), int(_num(f.get("id"), 0))))
    return fills, None


def _entry_side(direction):
    return "BUY" if direction == "LONG" else "SELL"


def _fill_ms(f):
    return int(_num(f.get("time"), 0))


def match_entry(rec, fills, prev_ts=None, taken=(), step=None, window_start_ms=None):
    """{"entry_ms", "fills", "match"} of an audit record (rules (a)-(d) of the module docstring). prev_ts: timestamp
    (s) of the previous same-symbol audit record (lower bound of the side + qty window); taken: orderIds (str) that
    belong to other records; step: the symbol's stepSize (qty tolerance) when known. window_start_ms: start of the
    fetched fills when they may not reach back to this entry (closed-today summary): an unmatched record whose match
    window starts before it is "partial_history" (entry_ms = window_start_ms), not "no_entry_fill"."""
    oid = rec.get("entry_order_id")
    direction = str(rec.get("direction")).upper()
    side = _entry_side(direction)
    ts_ms = int(_num(rec.get("timestamp"), 0) * 1000)
    legacy_ms = ts_ms - ENTRY_FILL_SLACK_MS
    fills = fills or []
    if oid is None or str(oid) == "":
        if window_start_ms is not None and legacy_ms < window_start_ms:
            return {"entry_ms": int(window_start_ms), "fills": [], "match": "partial_history"}
        return {"entry_ms": legacy_ms, "fills": [], "match": "legacy"}
    matched = [f for f in fills if str(f.get("orderId")) == str(oid) and f.get("side") == side]
    if matched:
        return {"entry_ms": min(_fill_ms(f) for f in matched), "fills": matched, "match": "order_id"}
    lo = ts_ms - ENTRY_MATCH_LOOKBACK_MS
    if prev_ts is not None:
        lo = max(lo, int(_num(prev_ts, 0) * 1000))
    hi = ts_ms + ENTRY_MATCH_AHEAD_MS
    total = _num(rec.get("total_qty"), 0.0)
    tol = max(total * QTY_TOLERANCE, _num(step, 0.0) or 0.0) * (1 + 1e-9)
    groups = {}
    for f in fills:
        if f.get("side") != side or str(f.get("positionSide") or "BOTH").upper() not in ("BOTH", direction):
            continue
        if _num(f.get("realizedPnl"), 0.0) != 0:
            continue  # a closing fill of an earlier position, never an opening one
        o = str(f.get("orderId"))
        if o not in taken:
            groups.setdefault(o, []).append(f)
    best = None
    for g in groups.values():
        first = min(_fill_ms(f) for f in g)
        if not lo <= first <= hi or abs(sum(_num(f.get("qty"), 0.0) for f in g) - total) > tol:
            continue
        key = (abs(first - ts_ms), first)
        if best is None or key < best[0]:
            best = (key, first, g)
    if best:
        return {"entry_ms": best[1], "fills": best[2], "match": "side_qty_window"}
    if window_start_ms is not None and lo < window_start_ms:
        return {"entry_ms": int(window_start_ms), "fills": [], "match": "partial_history"}
    return {"entry_ms": legacy_ms, "fills": [], "match": "no_entry_fill"}


def match_entries(recs, fills, step=None, window_start_ms=None):
    """(matches, foreign) for one symbol's records (oldest first): matches[i] = match_entry of recs[i] (each record's
    side + qty match excludes the orders of every audit entry_order_id and of earlier matches), foreign[i] = orderIds
    (str) of the other records' entries (entry_order_id and matched orders), never exit legs of recs[i]."""
    taken = {str(r.get("entry_order_id")) for r in recs if r.get("entry_order_id") not in (None, "")}
    matches = []
    for i, r in enumerate(recs):
        prev_ts = _num(recs[i - 1].get("timestamp")) if i > 0 else None
        m = match_entry(r, fills, prev_ts=prev_ts, taken=taken, step=step, window_start_ms=window_start_ms)
        taken |= {str(f.get("orderId")) for f in m["fills"]}
        matches.append(m)
    foreign = []
    for r, m in zip(recs, matches):
        own = {str(f.get("orderId")) for f in m["fills"]} | {str(r.get("entry_order_id"))}
        foreign.append(taken - own)
    return matches, foreign


def entry_fill_ms(rec, fills):
    """(time ms, entry fills) of match_entry(rec, fills) (no other record known)."""
    m = match_entry(rec, fills)
    return m["entry_ms"], m["fills"]


def leg_reason(fill, rec, trail_stops, entry_ms, tick=None, entry=None):
    """TP1 / TP2 by order id, else the nearest stop level in tolerance (SL, TRAILED_STOP: max(0.3 % of the price,
    2 x tick); BREAKEVEN: True Net BE level of entry (default the audit entry_price) +/- 0.15 % of the price), else
    MANUAL_OR_OTHER."""
    oid = str(fill.get("orderId"))
    if rec.get("tp1_order_id") is not None and oid == str(rec.get("tp1_order_id")):
        return "TP1"
    if rec.get("tp2_order_id") is not None and oid == str(rec.get("tp2_order_id")):
        return "TP2"
    price = _num(fill.get("price"), 0.0)
    stop_tol = max(PRICE_TOLERANCE * price, TICK_TOLERANCE * (_num(tick, 0.0) or 0.0)) * (1 + 1e-9)
    candidates = []  # (distance, rank, reason): the nearest wins, ties in this order
    sl = _num(rec.get("sl_price"))
    if sl and sl > 0 and abs(price - sl) <= stop_tol:
        candidates.append((abs(price - sl), 0, "SL"))
    fill_s = _num(fill.get("time"), 0) / 1000.0
    for ts, new_sl in trail_stops:
        if entry_ms / 1000.0 <= ts <= fill_s and new_sl and new_sl > 0 and abs(price - new_sl) <= stop_tol:
            candidates.append((abs(price - new_sl), 1, "TRAILED_STOP"))
    entry = _num(rec.get("entry_price")) if entry is None else entry
    if entry:
        is_long = str(rec.get("direction") or "").upper() != "SHORT"
        be = entry * ((1 + BREAKEVEN_BAND) if is_long else (1 - BREAKEVEN_BAND))
        if abs(price - be) <= BREAKEVEN_MATCH_BAND * price * (1 + 1e-9):
            candidates.append((abs(price - be), 2, "BREAKEVEN"))
    return min(candidates)[2] if candidates else "MANUAL_OR_OTHER"


def _initial_risk(direction, entry, sl):
    if not entry or not sl or sl <= 0:
        return None
    on_loss_side = sl < entry if direction == "LONG" else sl > entry
    return abs(entry - sl) if on_loss_side else None


def kline_excursion(symbol, direction, entry, risk, entry_ms, exit_ms, legs, env):
    """(mfe_r, mae_r, mfe_ts): public 1m bars opened after the fill minute and closed by exit_ms (the exit minute's bar,
    which may hold prints after the exit, is left out), plus the leg prices."""
    peak = trough = entry
    mfe_ts = None
    is_short = direction == "SHORT"

    def fold(fav, adv, ts):
        nonlocal peak, trough, mfe_ts
        if (fav < peak) if is_short else (fav > peak):
            peak, mfe_ts = fav, ts
        if (adv > trough) if is_short else (adv < trough):
            trough = adv

    start = trade_excursion.first_post_entry_bar_ms(entry_ms / 1000.0)
    while start < exit_ms:
        rows = trade_excursion.fetch_klines_range(symbol, "1m", start, KLINES_LIMIT, env,
                                                  timeout=KLINES_TIMEOUT_SECONDS)
        last_open = None
        for k in rows:
            open_ms = int(k[0])
            last_open = open_ms
            close_ms = int(k[6]) if len(k) > 6 else open_ms + trade_excursion.BAR_MS - 1
            if open_ms < start or open_ms >= exit_ms or close_ms > exit_ms:
                continue
            high, low = float(k[2]), float(k[3])
            fold(low if is_short else high, high if is_short else low, open_ms)
        if not rows or last_open is None or len(rows) < KLINES_LIMIT:
            break
        start = max(start, last_open) + trade_excursion.BAR_MS
    for leg in legs:
        fold(leg["price"], leg["price"], leg["time"])
    high, low = (trough, peak) if is_short else (peak, trough)
    mfe_r, mae_r = trade_excursion.excursion_r(direction, entry, risk, high, low)
    return mfe_r, mae_r, mfe_ts


def resolve_trade(rec, fills, consumed, trail_stops, next_start_ms, env, klines=True, foreign_entry_ids=(),
                  match=None, tick=None):
    """Outcome dict of one audit entry. consumed: {fill id: qty already assigned to earlier trades} (updated).
    match: the record's match_entry result (default: match_entry(rec, fills)). The qty to close is the filled qty: the
    sum of the matched entry fills when found, else the audit total_qty; the R basis is their VWAP (entry_vwap), else
    the audit entry_price. Fills of another audit record's entry (foreign_entry_ids: entry_order_id or matched
    orderIds) are never exit legs. A "no_entry_fill" record gets no legs and consumes nothing. tick: the symbol's
    tickSize (stop tolerance) when known."""
    direction = str(rec.get("direction")).upper()
    symbol = str(rec.get("symbol")).upper()
    entry = _num(rec.get("entry_price"))
    sl = _num(rec.get("sl_price"))
    total_qty = _num(rec.get("total_qty"))
    match = match or match_entry(rec, fills)
    entry_ms, entry_fills = match["entry_ms"], match["fills"]
    entry_qty = sum(_num(f.get("qty"), 0.0) for f in entry_fills)
    entry_vwap = (sum(_num(f.get("price"), 0.0) * _num(f.get("qty"), 0.0) for f in entry_fills) / entry_qty
                  if entry_qty > 0 else None)
    basis = entry_vwap or entry
    risk = _initial_risk(direction, basis, sl)
    filled_qty = entry_qty or total_qty
    close_side = "SELL" if direction == "LONG" else "BUY"
    foreign = {str(i) for i in foreign_entry_ids if i is not None}
    out = {"symbol": symbol, "direction": direction, "entry_ts": entry_ms, "entry_price": entry,
           "entry_vwap": round(entry_vwap, 10) if entry_vwap else None, "entry_match": match["match"], "sl_price": sl,
           "initial_risk": round(risk, 10) if risk else None, "total_qty": total_qty, "filled_qty": filled_qty,
           "tp1_price": _num(rec.get("tp1_price")), "tp2_price": _num(rec.get("tp2_price")),
           "is_yolo": bool(rec.get("is_yolo"))}
    if match["match"] == "no_entry_fill":
        out.update(status="no_entry_fill", filled_qty=None, legs=[], exit_ts=None, realized_r_gross=None,
                   realized_r_net=None, entry_commission_included=False, tp1_filled=False, exit_reason=None)
        return out
    if match["match"] == "partial_history":
        out["partial_history"] = True

    remaining = filled_qty
    legs = []
    for f in fills:
        if remaining <= filled_qty * QTY_TOLERANCE:
            break
        t = int(_num(f.get("time"), 0))
        if f.get("side") != close_side or t < entry_ms or (next_start_ms is not None and t >= next_start_ms):
            continue
        if str(f.get("orderId")) in foreign:
            continue  # another trade's entry fill (e.g. an opposite-direction entry in one-way mode)
        if str(f.get("positionSide") or "BOTH").upper() not in ("BOTH", direction):
            continue
        fid = str(f.get("id"))
        qty = _num(f.get("qty"), 0.0)
        free = qty - consumed.get(fid, 0.0)
        if free <= qty * QTY_TOLERANCE:
            continue
        used = min(free, remaining)
        consumed[fid] = consumed.get(fid, 0.0) + used
        remaining -= used
        share = used / qty if qty else 0.0
        legs.append({"reason": leg_reason(f, rec, trail_stops, entry_ms, tick=tick, entry=basis), "qty": used,
                     "price": _num(f.get("price"), 0.0), "time": t, "order_id": f.get("orderId"),
                     "realized_pnl": _num(f.get("realizedPnl"), 0.0) * share,
                     "commission": _num(f.get("commission"), 0.0) * share,
                     "commission_asset": f.get("commissionAsset")})

    closed = remaining <= filled_qty * QTY_TOLERANCE
    sign = 1.0 if direction == "LONG" else -1.0
    gross = net = None
    if risk and legs:
        gross = round(sum(l["qty"] * sign * (l["price"] - basis) for l in legs) / (risk * filled_qty), 4)
        assets = {str(l["commission_asset"] or "").upper() for l in legs}
        assets |= {str(f.get("commissionAsset") or "").upper() for f in entry_fills}
        if assets == {"USDT"}:
            pnl = sum(l["realized_pnl"] - l["commission"] for l in legs)
            pnl -= sum(_num(f.get("commission"), 0.0) for f in entry_fills)
            net = round(pnl / (risk * filled_qty), 4)
    out.update(status="closed" if closed else "open", legs=legs, exit_ts=legs[-1]["time"] if closed else None,
               realized_r_gross=gross, realized_r_net=net, entry_commission_included=bool(entry_fills),
               tp1_filled=any(l["reason"] == "TP1" for l in legs),
               exit_reason=legs[-1]["reason"] if closed else None)
    if klines and closed and risk:
        try:
            mfe_r, mae_r, mfe_ts = kline_excursion(symbol, direction, basis, risk, entry_ms, out["exit_ts"], legs, env)
            out.update(mfe_r=mfe_r, mae_r=mae_r, mfe_ts=mfe_ts,
                       giveback_r=round(mfe_r - gross, 4) if mfe_r is not None and gross is not None else None)
        except Exception as e:
            out.update(mfe_r=None, mae_r=None, mfe_ts=None, giveback_r=None, klines_error=f"{type(e).__name__}: {e}")
    return out


def summarize(trades):
    closed = [t for t in trades if t.get("status") == "closed"]

    def mean(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 4) if values else None

    by_reason = {}
    for t in closed:
        by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
    captured = [t for t in closed if (t.get("mfe_r") or 0) > 0 and t.get("realized_r_gross") is not None]
    mfe_sum = sum(t["mfe_r"] for t in captured)
    return {
        "by_exit_reason": by_reason,
        "tp1_then_breakeven": sum(1 for t in closed if t["tp1_filled"] and t["exit_reason"] == "BREAKEVEN"),
        "tp2_hits": sum(1 for t in closed if any(l["reason"] == "TP2" for l in t["legs"])),
        "full_sl": sum(1 for t in closed if t["legs"] and all(l["reason"] == "SL" for l in t["legs"])),
        "mean_realized_r_gross": mean(t.get("realized_r_gross") for t in closed),
        "mean_realized_r_net": mean(t.get("realized_r_net") for t in closed),
        "mean_mfe_r": mean(t.get("mfe_r") for t in closed),
        "capture_ratio": round(sum(t["realized_r_gross"] for t in captured) / mfe_sum, 4) if mfe_sum > 0 else None,
        "mean_giveback_r": mean(t.get("giveback_r") for t in closed if (t.get("mfe_r") or 0) >= 1),
    }


def build_outcomes(env, since_ts, symbol=None, klines=True, now_ms=None):
    """(trades, readable_symbols, unavailable_symbols)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    entries = load_entries(env, since_ts, symbol)
    trail = load_trail_stops(env)
    by_symbol = {}
    for rec in entries:
        by_symbol.setdefault(str(rec["symbol"]).upper(), []).append(rec)
    filters = load_filters(env) if by_symbol else {}
    trades, readable, unavailable = [], 0, 0
    for sym, recs in by_symbol.items():
        start = int(min(_num(r["timestamp"]) for r in recs) * 1000) - ENTRY_LOOKBACK_MS
        budget = {}
        fills, err = fetch_fills(sym, start, now_ms, env, budget=budget)
        if fills is None:
            unavailable += 1
            for r in recs:
                trades.append({"symbol": sym, "direction": str(r.get("direction")).upper(),
                               "entry_ts": int(_num(r.get("timestamp")) * 1000), "entry_price": _num(r.get("entry_price")),
                               "sl_price": _num(r.get("sl_price")), "total_qty": _num(r.get("total_qty")),
                               "is_yolo": bool(r.get("is_yolo")), "status": "fills_unavailable", "error": err})
            continue
        readable += 1
        sym_filters = filters.get(sym) or {}
        matches, foreign = match_entries(recs, fills, step=sym_filters.get("stepSize"))
        starts = [m["entry_ms"] for m in matches]
        consumed = {}
        for i, r in enumerate(recs):
            nxt = next((starts[j] for j in range(i + 1, len(recs))
                        if str(recs[j].get("direction")).upper() == str(r.get("direction")).upper()), None)
            trades.append(resolve_trade(r, fills, consumed, trail.get(sym, []), nxt, env, klines=klines,
                                        foreign_entry_ids=foreign[i], match=matches[i],
                                        tick=sym_filters.get("tickSize")))
            if budget.get("truncated"):
                trades[-1]["truncated"] = True
    trades.sort(key=lambda t: (t.get("entry_ts") or 0, t["symbol"]))
    return trades, readable, unavailable


def _trade_r(t):
    """R basis of a resolved trade: realized_r_net, else realized_r_gross (None without either)."""
    return t.get("realized_r_net") if t.get("realized_r_net") is not None else t.get("realized_r_gross")


def summarize_closed_today(entries, fills, day_start_ms, env, open_positions=None):
    """Per-trade closed-today figures from the day's userTrades already fetched (fills since day_start_ms, any
    symbol) and the audit records (entries: raw trades_audit records, filtered here to env's entry records): the
    resolve_trade matching and consumption with klines disabled and no request. A trade counts when it is closed and
    its final exit leg is at or after day_start_ms. An entry whose fills precede the window (partial_history) is
    resolved from today's legs and the audit entry and counts when its legs close the audit qty, or when it has a
    leg today and open_positions (set of (SYMBOL, DIRECTION) open now) is given and holds no such position.
    Returns {"trades_closed", "wins", "losses", "scratches" (|R| < SCRATCH_R), "realized_r_net_sum",
    "realized_r_gross_sum", "fills_closed" (fills with a non-zero realizedPnl), "partial_history" (count)}. R per
    trade: realized_r_net, else realized_r_gross; a trade without R is classed by its legs' realizedPnl - commission."""
    fills = [f for f in fills or [] if isinstance(f, dict)]
    by_symbol_fills = {}
    for f in sorted(fills, key=lambda f: (_fill_ms(f), int(_num(f.get("id"), 0)))):
        by_symbol_fills.setdefault(str(f.get("symbol") or "").upper(), []).append(f)
    by_symbol = {}
    for rec in entries or []:
        if _is_entry_record(rec, env):
            by_symbol.setdefault(str(rec["symbol"]).upper(), []).append(rec)
    out = {"trades_closed": 0, "wins": 0, "losses": 0, "scratches": 0, "realized_r_net_sum": 0.0,
           "realized_r_gross_sum": 0.0, "fills_closed": sum(1 for f in fills if _num(f.get("realizedPnl"), 0.0) != 0),
           "partial_history": 0}
    for sym, sym_fills in by_symbol_fills.items():
        recs = sorted(by_symbol.get(sym, []), key=lambda r: _num(r.get("timestamp"), 0))
        if not recs:
            continue
        matches, foreign = match_entries(recs, sym_fills, window_start_ms=day_start_ms)
        starts = [m["entry_ms"] for m in matches]
        consumed = {}
        for i, r in enumerate(recs):
            nxt = next((starts[j] for j in range(i + 1, len(recs))
                        if str(recs[j].get("direction")).upper() == str(r.get("direction")).upper()), None)
            t = resolve_trade(r, sym_fills, consumed, [], nxt, env, klines=False, foreign_entry_ids=foreign[i],
                              match=matches[i])
            partial = bool(t.get("partial_history"))
            closed = t.get("status") == "closed"
            if partial and not closed and t.get("legs") and open_positions is not None:
                closed = (sym, t["direction"]) not in open_positions
            if not closed or not t.get("legs") or t["legs"][-1]["time"] < day_start_ms:
                continue
            out["trades_closed"] += 1
            out["partial_history"] += int(partial)
            r_value = _trade_r(t)
            if r_value is None:
                pnl = sum(l["realized_pnl"] - l["commission"] for l in t["legs"])
                kind = "wins" if pnl > 0 else "losses" if pnl < 0 else "scratches"
            else:
                kind = "scratches" if abs(r_value) < SCRATCH_R else "wins" if r_value > 0 else "losses"
            out[kind] += 1
            out["realized_r_net_sum"] += t.get("realized_r_net") or 0.0
            out["realized_r_gross_sum"] += t.get("realized_r_gross") or 0.0
    out["realized_r_net_sum"] = round(out["realized_r_net_sum"], 4)
    out["realized_r_gross_sum"] = round(out["realized_r_gross_sum"], 4)
    return out


def _inside_logs(path):
    """True when path is a file path inside the workspace logs/ directory, lexically and after resolving symlinks."""
    logs = os.path.abspath(_logs_dir())
    target = os.path.abspath(path)
    real_logs, real_target = os.path.realpath(logs), os.path.realpath(target)
    try:
        lexical = os.path.commonpath([logs, target]) == logs and target != logs
        resolved = os.path.commonpath([real_logs, real_target]) == real_logs and real_target != real_logs
    except ValueError:
        return False
    return lexical and resolved


def _since_ts(value):
    try:
        d = datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r} (expected YYYY-MM-DD)")
    return value, int(d.replace(tzinfo=datetime.timezone.utc).timestamp())


def main(argv=None):
    default_since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DEFAULT_SINCE_DAYS)
                     ).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="Read-only per-trade exits, realized R and MFE/MAE from Binance fills")
    parser.add_argument("--since", type=_since_ts, default=_since_ts(default_since), help="YYYY-MM-DD (UTC), default 7 days ago")
    parser.add_argument("--symbol", default=None, help="Only this symbol")
    parser.add_argument("--env", choices=["prod", "testnet"], default=None, help="Defaults to utils.env_resolver.resolve_env()")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print the summary as JSON")
    parser.add_argument("--no-klines", action="store_true", dest="no_klines", help="Skip MFE/MAE from public 1m klines")
    parser.add_argument("--output", default=None, help="Output JSONL inside logs/ (default logs/trade_outcomes.jsonl)")
    args = parser.parse_args(argv)
    try:
        env = resolve_env(args.env)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"Invalid environment: {e}"}))
        return 1
    since_str, since_ts = args.since
    output = args.output or os.path.join(_logs_dir(), "trade_outcomes.jsonl")
    if not _inside_logs(output):
        print(f"trade_outcomes: --output must be a file inside {_logs_dir()} (got {output!r})", file=sys.stderr)
        return 2
    trades, readable, unavailable = build_outcomes(env, since_ts, (args.symbol or "").upper() or None,
                                                   klines=not args.no_klines)
    for t in trades:
        t["env"], t["since"] = env, since_str
    _write_jsonl_atomic(output, trades)
    ok = not trades or readable > 0
    closed = [t for t in trades if t.get("status") == "closed"]
    truncated = sorted({t["symbol"] for t in trades if t.get("truncated")})
    warnings = [f"userTrades request cap reached for {', '.join(truncated)}: their fills may be incomplete "
                f"(rows flagged truncated)"] if truncated else []
    result = {"ok": ok, "env": env, "since": since_str, "trades": len(trades), "closed": len(closed),
              "summary": summarize(trades), "warnings": warnings, "output": output}
    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        print(f"trade_outcomes {env} since {since_str}: {len(trades)} trade(s), {len(closed)} closed"
              f"{f', {unavailable} symbol(s) without fills' if unavailable else ''} -> {output}")
        for t in trades:
            print(f"  - {t['symbol']} {t['direction']} {t['status']} exit={t.get('exit_reason')} "
                  f"R_gross={t.get('realized_r_gross')} R_net={t.get('realized_r_net')} MFE_R={t.get('mfe_r')}")
        for w in warnings:
            print(f"  note: {w}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
