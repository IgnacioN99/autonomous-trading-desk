#!/usr/bin/env python3
"""
trade_outcomes.py - Per-trade exits, realized R and MFE / MAE reconstructed from Binance fills (issue #182).

Read-only: it never places, changes or cancels orders. The only signed request is GET /fapi/v1/userTrades (through
execute_futures_trade.send_signed_request, the executor's client path); MFE / MAE come from public 1m klines
(utils/trade_excursion.fetch_klines_range).

Inputs:
  - Entries: non-event records of logs/trades_audit.jsonl with entry_price, sl_price and total_qty, for --env (a
    record without target_env matches), at or after --since, optionally one --symbol.
  - Fills: userTrades per symbol, startTime / endTime windows of at most 7 days from the earliest entry - 5 min to
    now, limit 1000; a window returning 1000 fills is split in halves until complete (independent of the reply's
    order; deduplicated by fill id). A non-list reply (MCP
    mode: the gateway does not serve userTrades; auth or API error) marks that symbol's trades "fills_unavailable".
  - Trailed stops: successful, non-dry-run trail_stop actions (new_sl) of logs/guardian_actions.jsonl.

Per trade: the entry fill time is the earliest fill of entry_order_id (else the audit timestamp - 120 s). Closing
fills (side opposite to the entry, same positionSide or BOTH, at or after the entry fill, before the next audit
entry of the same symbol + direction, never another record's entry_order_id fill) are consumed in time order until
filled_qty is closed (1e-6 relative; a fill shared by two trades is split); otherwise the trade stays "open".
filled_qty = the entry_order_id fills' qty when found, else the audit total_qty (a partly filled, then cancelled
LIMIT entry closes on what filled). Leg reasons, in order: orderId == tp1_order_id -> TP1,
== tp2_order_id -> TP2; price within 0.3% of sl_price -> SL; within 0.3% of a trail_stop new_sl placed between entry
and the fill -> TRAILED_STOP; within entry x (1 +/- 0.002) -> BREAKEVEN; else MANUAL_OR_OTHER (stop fills are
market child orders without the algo id, so stops are matched by price).
  realized_r_gross = sum(qty_i x signed(price_i - entry)) / (risk x filled_qty), risk = |entry - sl| (SL on the loss
  side, else null); realized_r_net = sum(realizedPnl - commission) over the closing legs and the matched entry fills
  / (risk x filled_qty), null unless every commission is in USDT (entry_commission_included says whether the entry
  fills were identified). mfe_r / mae_r / mfe_ts from 1m klines after the fill minute up to the exit minute plus the
  leg prices; giveback_r = mfe_r - realized_r_gross. entry_ts, exit_ts, mfe_ts and leg times are in ms.

Output: logs/trade_outcomes.jsonl (rewritten atomically each run, one JSON line per trade, each stamped with the
run's "env" and "since" so offline readers such as trading_scorecard.py can filter and date it) and with --json
{"ok", "env", "since", "trades", "closed", "summary", "output"}. Exit code 0 when at least one trade was resolved (or
there is no trade in range), 1 when no symbol's fills were readable.

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
ENTRY_LOOKBACK_MS = 5 * 60 * 1000
ENTRY_FILL_SLACK_MS = 120 * 1000
PRICE_TOLERANCE = 0.003
BREAKEVEN_BAND = 0.002
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


def load_entries(env, since_ts, symbol=None):
    """Entry records of logs/trades_audit.jsonl for env since since_ts (seconds), oldest first."""
    out = []
    for rec in _read_jsonl(os.path.join(_logs_dir(), "trades_audit.jsonl")):
        if rec.get("event"):
            continue
        entry, sl, qty = _num(rec.get("entry_price")), _num(rec.get("sl_price")), _num(rec.get("total_qty"))
        ts = _num(rec.get("timestamp"))
        direction = str(rec.get("direction") or "").upper()
        if not entry or entry <= 0 or sl is None or sl <= 0 or not qty or qty <= 0 or not ts:
            continue
        if direction not in ("LONG", "SHORT"):
            continue
        rec_env = pt.norm_env(rec.get("target_env"))
        if rec_env and rec_env != env:
            continue
        sym = str(rec.get("symbol") or "").upper()
        if not sym or ts < since_ts or (symbol and sym != symbol):
            continue
        out.append(rec)
    out.sort(key=lambda r: _num(r.get("timestamp"), 0))
    return out


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


def _fetch_window(symbol, start_ms, end_ms, env, seen):
    """Adds the userTrades of [start_ms, end_ms] to seen (by id); None or the error text. A full window (FILLS_LIMIT
    rows) is split in half and both halves fetched (down to 1 ms), so paging never depends on the reply's order."""
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
        mid = (int(start_ms) + int(end_ms)) // 2
        return _fetch_window(symbol, start_ms, mid, env, seen) or _fetch_window(symbol, mid + 1, end_ms, env, seen)
    return None


def fetch_fills(symbol, start_ms, end_ms, env):
    """(fills sorted by time, None) or (None, error text): userTrades in windows of at most 7 days; a window that
    returns FILLS_LIMIT fills is split in halves until each part is complete (or 1 ms wide); deduplicated by id."""
    seen = {}
    window_start = int(start_ms)
    while window_start <= end_ms:
        window_end = min(window_start + WINDOW_MS - 1, int(end_ms))
        err = _fetch_window(symbol, window_start, window_end, env, seen)
        if err is not None:
            return None, err
        window_start = window_end + 1
    fills = sorted(seen.values(), key=lambda f: (int(_num(f.get("time"), 0)), int(_num(f.get("id"), 0))))
    return fills, None


def _entry_side(direction):
    return "BUY" if direction == "LONG" else "SELL"


def entry_fill_ms(rec, fills):
    """(time ms, entry fills): earliest fill of entry_order_id, else audit timestamp - 120 s with no entry fills."""
    oid = rec.get("entry_order_id")
    side = _entry_side(str(rec.get("direction")).upper())
    matched = [f for f in fills or [] if oid is not None and str(f.get("orderId")) == str(oid) and f.get("side") == side]
    if matched:
        return min(int(_num(f.get("time"), 0)) for f in matched), matched
    return int(_num(rec.get("timestamp"), 0) * 1000) - ENTRY_FILL_SLACK_MS, []


def _near(price, level, tol):
    return level is not None and level > 0 and abs(price - level) <= tol * price


def leg_reason(fill, rec, trail_stops, entry_ms):
    oid = str(fill.get("orderId"))
    if rec.get("tp1_order_id") is not None and oid == str(rec.get("tp1_order_id")):
        return "TP1"
    if rec.get("tp2_order_id") is not None and oid == str(rec.get("tp2_order_id")):
        return "TP2"
    price = _num(fill.get("price"), 0.0)
    if _near(price, _num(rec.get("sl_price")), PRICE_TOLERANCE):
        return "SL"
    fill_s = _num(fill.get("time"), 0) / 1000.0
    for ts, new_sl in trail_stops:
        if entry_ms / 1000.0 <= ts <= fill_s and _near(price, new_sl, PRICE_TOLERANCE):
            return "TRAILED_STOP"
    entry = _num(rec.get("entry_price"))
    if entry and abs(price - entry) <= entry * BREAKEVEN_BAND * (1 + 1e-9):
        return "BREAKEVEN"
    return "MANUAL_OR_OTHER"


def _initial_risk(direction, entry, sl):
    if not entry or not sl or sl <= 0:
        return None
    on_loss_side = sl < entry if direction == "LONG" else sl > entry
    return abs(entry - sl) if on_loss_side else None


def kline_excursion(symbol, direction, entry, risk, entry_ms, exit_ms, legs, env):
    """(mfe_r, mae_r, mfe_ts): public 1m bars opened after the fill minute and before exit_ms, plus the leg prices."""
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
            if open_ms < start or open_ms >= exit_ms:
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


SCORE_FIELDS = ("score", "score_tier", "score_components", "dossier_tier", "dossier_score", "dossier_sha256")


def score_fields(rec):
    """Score metadata of the audit entry (issue #202), None when the record predates it."""
    return {k: rec.get(k) for k in SCORE_FIELDS}


def resolve_trade(rec, fills, consumed, trail_stops, next_start_ms, env, klines=True, foreign_entry_ids=()):
    """Outcome dict of one audit entry. consumed: {fill id: qty already assigned to earlier trades} (updated).
    The qty to close (and the R basis) is the filled qty: the sum of the entry_order_id fills when found, else the
    audit total_qty. Fills of another audit record's entry_order_id (foreign_entry_ids) are never exit legs."""
    direction = str(rec.get("direction")).upper()
    symbol = str(rec.get("symbol")).upper()
    entry = _num(rec.get("entry_price"))
    sl = _num(rec.get("sl_price"))
    total_qty = _num(rec.get("total_qty"))
    risk = _initial_risk(direction, entry, sl)
    entry_ms, entry_fills = entry_fill_ms(rec, fills)
    filled_qty = sum(_num(f.get("qty"), 0.0) for f in entry_fills) or total_qty
    close_side = "SELL" if direction == "LONG" else "BUY"
    foreign = {str(i) for i in foreign_entry_ids if i is not None}
    out = {"symbol": symbol, "direction": direction, "entry_ts": entry_ms, "entry_price": entry, "sl_price": sl,
           "initial_risk": round(risk, 10) if risk else None, "total_qty": total_qty, "filled_qty": filled_qty,
           "tp1_price": _num(rec.get("tp1_price")), "tp2_price": _num(rec.get("tp2_price")),
           "is_yolo": bool(rec.get("is_yolo")), **score_fields(rec)}

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
        legs.append({"reason": leg_reason(f, rec, trail_stops, entry_ms), "qty": used,
                     "price": _num(f.get("price"), 0.0), "time": t, "order_id": f.get("orderId"),
                     "realized_pnl": _num(f.get("realizedPnl"), 0.0) * share,
                     "commission": _num(f.get("commission"), 0.0) * share,
                     "commission_asset": f.get("commissionAsset")})

    closed = remaining <= filled_qty * QTY_TOLERANCE
    sign = 1.0 if direction == "LONG" else -1.0
    gross = net = None
    if risk and legs:
        gross = round(sum(l["qty"] * sign * (l["price"] - entry) for l in legs) / (risk * filled_qty), 4)
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
            mfe_r, mae_r, mfe_ts = kline_excursion(symbol, direction, entry, risk, entry_ms, out["exit_ts"], legs, env)
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
    trades, readable, unavailable = [], 0, 0
    for sym, recs in by_symbol.items():
        start = int(min(_num(r["timestamp"]) for r in recs) * 1000) - ENTRY_LOOKBACK_MS
        fills, err = fetch_fills(sym, start, now_ms, env)
        if fills is None:
            unavailable += 1
            for r in recs:
                trades.append({"symbol": sym, "direction": str(r.get("direction")).upper(),
                               "entry_ts": int(_num(r.get("timestamp")) * 1000), "entry_price": _num(r.get("entry_price")),
                               "sl_price": _num(r.get("sl_price")), "total_qty": _num(r.get("total_qty")),
                               "is_yolo": bool(r.get("is_yolo")), **score_fields(r),
                               "status": "fills_unavailable", "error": err})
            continue
        readable += 1
        starts = [entry_fill_ms(r, fills)[0] for r in recs]
        consumed = {}
        for i, r in enumerate(recs):
            nxt = next((starts[j] for j in range(i + 1, len(recs))
                        if str(recs[j].get("direction")).upper() == str(r.get("direction")).upper()), None)
            foreign = [o.get("entry_order_id") for j, o in enumerate(recs) if j != i]
            trades.append(resolve_trade(r, fills, consumed, trail.get(sym, []), nxt, env, klines=klines,
                                        foreign_entry_ids=foreign))
    trades.sort(key=lambda t: (t.get("entry_ts") or 0, t["symbol"]))
    return trades, readable, unavailable


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
    parser.add_argument("--output", default=None, help="Output JSONL (default logs/trade_outcomes.jsonl)")
    args = parser.parse_args(argv)
    try:
        env = resolve_env(args.env)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"Invalid environment: {e}"}))
        return 1
    since_str, since_ts = args.since
    output = args.output or os.path.join(_logs_dir(), "trade_outcomes.jsonl")
    trades, readable, unavailable = build_outcomes(env, since_ts, (args.symbol or "").upper() or None,
                                                   klines=not args.no_klines)
    for t in trades:
        t["env"], t["since"] = env, since_str
    _write_jsonl_atomic(output, trades)
    ok = not trades or readable > 0
    closed = [t for t in trades if t.get("status") == "closed"]
    result = {"ok": ok, "env": env, "since": since_str, "trades": len(trades), "closed": len(closed),
              "summary": summarize(trades), "output": output}
    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        print(f"trade_outcomes {env} since {since_str}: {len(trades)} trade(s), {len(closed)} closed"
              f"{f', {unavailable} symbol(s) without fills' if unavailable else ''} -> {output}")
        for t in trades:
            print(f"  - {t['symbol']} {t['direction']} {t['status']} exit={t.get('exit_reason')} "
                  f"R_gross={t.get('realized_r_gross')} R_net={t.get('realized_r_net')} MFE_R={t.get('mfe_r')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
