#!/usr/bin/env python3
"""
sync_session_state.py - Deterministic Session and Portfolio State Synchronizer.
Writes a ledger cache for clean-room AI agent instances, eliminating context bloat, information loss,
and hallucinations. It is a cache, not an authority: the executor's PROD gates re-read the exchange and
apply the stricter of the cache and the live view (issue #101).

Zero LLM Tokens / Latency ~600ms.
Generates 'logs/session_state.json' and outputs a typed executive summary for cold-start priming.
Issue #48: portfolio_exposure also carries resting_entries [{"symbol", "dir", "kind"}] (same-env
logs/pending_entries.json records whose entry order still rests on openAlgoOrders / openOrders and whose symbol has
no open position), resting_margin_usdt (sum of their margin_usdt) and delta_bias_incl_resting (delta_bias with each
resting entry as a leg of its direction; "UNKNOWN" when the registry or an order listing cannot be read).
delta_bias itself is unchanged. Issue #173: audit_read_error (logs/trades_audit.jsonl exists but is unreadable:
the exception text, else None) and audit_corrupt_lines (skipped non-JSON-object lines, else 0); the doctor warns on
either. Issue #160: portfolio_exposure.resting_mismatches lists same-env records with no live order match and no
open position (not counted; the doctor warns), and the order listings are read before positionRisk so an entry
filling between the reads is double counted rather than missed. The state is only ever written atomically
(issue #127): a failed write leaves the
previous file. Issue #208: closed_today_summary counts trades, not fills (trade_outcomes.summarize_closed_today on
the day's userTrades, pages of 1000 up to 10 pages, "truncated" when incomplete, and the audit records): closed_trades_count / wins / losses / scratches per trade,
win_rate_pct = wins / closed_trades_count (scratches count in the denominator), realized_r_net (sum of per-trade R), partial_history (trades entered before today), fills_closed (fills with a
realized PnL); the USDT figures stay sums over the fills. CLI exit code 1 when the state written is INVALID or the write failed, else 0.
"""

import os
import sys
import json
import time
import datetime
from typing import Dict, List, Any

# Ensure local path resolution
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
from utils.portfolio_exposure import compute_exposure, book_exposure, LONG_HEAVY, SHORT_HEAVY
from utils import position_timing as pt
from utils.env_resolver import resolve_env

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
AUDIT_LOG = os.path.join(LOGS_DIR, "trades_audit.jsonl")

def get_start_of_day_utc() -> int:
    """Returns timestamp in ms for the start of the current UTC day (00:00:00 UTC)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)

DAY_FILLS_LIMIT = 1000
DAY_FILLS_MAX_PAGES = 10


def fetch_day_fills(start_ms: int, target_env: str):
    """(fills, truncated) of GET /fapi/v1/userTrades since start_ms (issue #208): pages of DAY_FILLS_LIMIT, each next
    page from the last fill's time (inclusive, deduplicated by (symbol, id): ids are per symbol), until a page holds fewer than DAY_FILLS_LIMIT
    rows; at most DAY_FILLS_MAX_PAGES pages. truncated: the cap was hit, a later page failed or brought no new fill
    (the day's figures may then be incomplete). A failed first read returns its non-list reply (fills unreadable)."""
    seen, params = {}, {"startTime": int(start_ms), "limit": DAY_FILLS_LIMIT}
    for page in range(DAY_FILLS_MAX_PAGES):
        res = eft.send_signed_request("GET", "/fapi/v1/userTrades", dict(params), target_env=target_env)
        if not isinstance(res, list):
            return (res, False) if page == 0 else (_sorted_fills(seen), True)
        new = 0
        for f in res:
            key = (str(f.get("symbol") or "").upper(), str(f.get("id"))) if isinstance(f, dict) else None
            if key and key not in seen:  # fill ids are per symbol: (symbol, id) identifies a fill
                seen[key] = f
                new += 1
        if len(res) < DAY_FILLS_LIMIT:
            return _sorted_fills(seen), False
        if not new:
            return _sorted_fills(seen), True  # a full page of one millisecond: cannot advance
        params["startTime"] = max(params["startTime"],
                                  max(int(_as_float(f.get("time"))) for f in res if isinstance(f, dict)))
    return _sorted_fills(seen), True


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _sorted_fills(seen: dict) -> List[dict]:
    """Fills by (time, symbol, id); time and id numeric (a non-numeric value sorts as 0)."""
    return sorted(seen.values(), key=lambda f: (_as_float(f.get("time")), str(f.get("symbol") or "").upper(),
                                                _as_float(f.get("id"))))


def _read_audit_records():
    """(records, read_error, corrupt_lines) for trades_audit.jsonl (AUDIT_LOG), read once per sync. records: parsed
    dict lines ([] when missing or unreadable). read_error: "<ExceptionType>: <text>" when the file exists but cannot
    be read, else None. corrupt_lines: non-empty lines that are not JSON objects (skipped). Issue #173: both go into
    the ledger as audit_read_error / audit_corrupt_lines."""
    records, corrupt = [], 0
    if not os.path.exists(AUDIT_LOG):
        return records, None, 0
    try:
        with open(AUDIT_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    corrupt += 1
                    continue
                if isinstance(record, dict):
                    records.append(record)
                else:
                    corrupt += 1
    except Exception as e:
        return [], f"{type(e).__name__}: {e}", 0
    return records, None, corrupt


def load_audit_metadata(target_env: str = None, records: List[dict] = None) -> Dict[str, dict]:
    """Loads latest metadata from trades_audit.jsonl (or the pre-read records) keyed by symbol, strictly filtered by
    target_env."""
    meta = {}
    # Env aliases normalised on both sides ("mainnet" == "prod"), same as utils/position_timing (issue #92).
    norm_env = pt.norm_env(target_env)
    for record in (_read_audit_records()[0] if records is None else records):
        try:
            rec_env = pt.norm_env(record.get("target_env"))
            if norm_env and rec_env and rec_env != norm_env:
                continue
            sym = record.get("symbol")
            if sym:
                meta[sym] = record
        except Exception:
            continue
    return meta

def write_error_state(err_msg: str, now_ts: int, now_utc: str, target_env: str, btc_price: float) -> dict:
    """Writes and returns the fail-closed state (is_valid False, delta_bias UNKNOWN, zeroed figures, error text) used
    when the ledger positions cannot be read or classified: never a 0-position DELTA_BALANCED state (Finding 6)."""
    error_state = {
        "is_valid": False,
        "error": err_msg,
        "last_updated_ts": now_ts,
        "last_updated_utc": now_utc,
        "target_env": target_env,
        "macro_btc": {
            "price_usdt": btc_price
        },
        "portfolio_exposure": {
            "total_active_positions": 0,
            "long_notional_usdt": 0.0,
            "short_notional_usdt": 0.0,
            "net_notional_delta_usdt": 0.0,
            "delta_bias": "UNKNOWN",
            "delta_bias_incl_resting": "UNKNOWN",
            "resting_entries": [],
            "resting_margin_usdt": 0.0,
            "resting_mismatches": [],
            "delta_advice": f"🚨 LEDGER SYNC FAILED: {err_msg}",
            "total_floating_pnl_usdt": 0.0
        },
        "active_positions": [],
        "active_sl_algo_orders": [],
        "active_tp_limit_orders": [],
        "closed_today_summary": {
            "closed_trades_count": 0,
            "wins": 0,
            "losses": 0,
            "scratches": 0,
            "win_rate_pct": 0.0,
            "realized_r_net": 0.0,
            "partial_history": 0,
            "fills_closed": 0,
            "truncated": False,
            "counted_by": "trades",
            "gross_realized_pnl_usdt": 0.0,
            "commissions_usdt": 0.0,
            "net_realized_pnl_usdt": 0.0
        }
    }
    return _write_state(error_state)


def _write_state(state: dict) -> dict:
    """Writes the state atomically (temp file + os.replace). Issue #127: there is no plain open(..., "w") fallback;
    a failed write prints the error, leaves the previous file and returns the state with "state_write_error" (the CLI
    then exits 1). Readers judge the previous file by its own last_updated_ts, so it cannot look fresh."""
    try:
        from utils.atomic_writer import atomic_write_json
        atomic_write_json(STATE_FILE, state)
    except Exception as e:
        print(f"sync_session_state: session_state.json NOT written ({type(e).__name__}: {e}); the previous file is "
              "kept", file=sys.stderr)
        return dict(state, state_write_error=f"{type(e).__name__}: {e}")
    return state


def _load_registry_records(target_env: str):
    """Same-env records of logs/pending_entries.json (LOGS_DIR). Returns (records, error): a missing file is no
    records; an unreadable or malformed one is an error."""
    path = os.path.join(LOGS_DIR, "pending_entries.json")
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [], f"pending entries registry unreadable ({e})"
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return [], "pending entries registry malformed"
    return [r for r in data["entries"].values() if isinstance(r, dict) and r.get("target_env") == target_env], None


def resting_entry_exposure(target_env: str, algos_res, open_orders_res, exposure: dict) -> dict:
    """Issue #48: resting entries of the registry that still rest on the exchange. A record counts only when its
    entry order (by entry_id: algoId for STOP_MARKET, orderId for LIMIT) is among the opening orders of the listings
    already fetched (eft.live_resting_opening_orders: not closePosition / reduceOnly, no final algoStatus) and its
    symbol has no open position (a filled record waiting for its protect cycle is not counted twice). Each one is a
    leg of its direction at trigger_or_limit_price x total_qty. Returns {"resting_entries", "resting_margin_usdt",
    "delta_bias_incl_resting", "resting_mismatches"}; "UNKNOWN" when the registry or a listing cannot be read.
    Issue #160: a record with no live order match and no open position on its symbol is not counted but listed in
    resting_mismatches [{"symbol", "entry_id", "side"}] (a stale record or an unexpected id format; the doctor warns)."""
    out = {"resting_entries": [], "resting_margin_usdt": 0.0, "delta_bias_incl_resting": "UNKNOWN",
           "resting_mismatches": []}
    if not isinstance(algos_res, list) or not isinstance(open_orders_res, list):
        return out
    records, err = _load_registry_records(target_env)
    if err:
        return out
    live = eft.live_resting_opening_orders({"open_algo_orders": algos_res, "open_orders": open_orders_res})
    live_ids = {(str(o.get("symbol") or "").upper(), kind, str(eft._order_id(o))) for _src, kind, o in live}
    open_symbols = set(exposure["symbols"])
    long_n, short_n, margin = exposure["long_notional"], exposure["short_notional"], 0.0
    for rec in records:
        sym = str(rec.get("symbol") or "").upper()
        kind = "STOP_MARKET" if str(rec.get("kind") or "").upper() == "STOP_MARKET" else "LIMIT"
        is_long = str(rec.get("direction") or "").upper() == "LONG"
        if sym in open_symbols:
            continue
        if (sym, kind, str(rec.get("entry_id"))) not in live_ids:
            out["resting_mismatches"].append({"symbol": sym, "entry_id": str(rec.get("entry_id")),
                                              "side": "LONG" if is_long else "SHORT"})
            continue
        try:
            notional = abs(float(rec.get("trigger_or_limit_price")) * float(rec.get("total_qty")))
            margin += float(rec.get("margin_usdt") or 0.0)
        except (TypeError, ValueError):
            return {"resting_entries": [], "resting_margin_usdt": 0.0, "delta_bias_incl_resting": "UNKNOWN",
                    "resting_mismatches": []}
        if is_long:
            long_n += notional
        else:
            short_n += notional
        out["resting_entries"].append({"symbol": sym, "dir": "LONG" if is_long else "SHORT", "kind": kind})
    out["resting_margin_usdt"] = round(margin, 2)
    out["delta_bias_incl_resting"] = book_exposure(long_n, short_n)["delta_bias"]
    return out

def sync_session_state(target_env: str = None) -> dict:
    """
    Synchronizes directly against the Binance Futures ledger (Mainnet/Testnet)
    and generates the structured session state.
    """
    if target_env is None:
        cfg = eft.load_env()
        target_env = (os.environ.get("BINANCE_API_ENV") or cfg.get("BINANCE_API_ENV", "prod")).lower()
    target_env = resolve_env(target_env)  # "mainnet" -> "prod": one spelling in the ledger (issue #92)
    os.makedirs(LOGS_DIR, exist_ok=True)
    records, audit_read_error, audit_corrupt_lines = _read_audit_records()   # one read per sync (issue #138)
    audit_meta = load_audit_metadata(target_env, records=records)
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_ts = int(time.time())

    # 1. Macro BTC
    btc_ticker = eft.send_signed_request("GET", "/fapi/v1/ticker/price", {"symbol": "BTCUSDT"}, target_env=target_env)
    btc_price = float(btc_ticker.get("price", 0.0)) if isinstance(btc_ticker, dict) else 0.0

    # Issue #160: the order listings are read BEFORE positionRisk (same order as the executor's
    # fetch_live_gate_snapshot): an entry that fills between the reads then shows as both resting and filled (double
    # counted, the safe side) instead of vanishing from both. A raised read is a failed listing (non-list: resting
    # exposure UNKNOWN), so a failing positionRisk read still writes the fail-closed state below.
    listings = []
    for endpoint in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
        try:
            listings.append(eft.send_signed_request("GET", endpoint, target_env=target_env))
        except Exception as e:
            listings.append({"error": f"{type(e).__name__}: {e}"})
    algos_res, open_orders_res = listings

    # 2. Active Ledger Positions
    try:
        pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    except Exception as e:
        pos_res = {"error": str(e)}

    # Finding 6: If positionRisk API call returns an error dict, exception, or non-list,
    # DO NOT write session_state.json with 0 positions and DELTA_BALANCED.
    if not isinstance(pos_res, list) or (isinstance(pos_res, dict) and ("code" in pos_res or "error" in pos_res or "msg" in pos_res)):
        return write_error_state(f"Failed to fetch positionRisk from ledger: {pos_res}", now_ts, now_utc,
                                 target_env, btc_price)

    active_positions = []
    # Portfolio delta classification shared with the executor's PROD gates (utils/portfolio_exposure.py, issue #101).
    # A malformed row (issue #117) is a failed sync: same fail-closed state as a failed positionRisk read.
    try:
        exposure = compute_exposure(pos_res)
    except ValueError as e:
        return write_error_state(f"Malformed positionRisk data from ledger: {e}", now_ts, now_utc, target_env,
                                 btc_price)
    long_notional = exposure["long_notional"]
    short_notional = exposure["short_notional"]

    for live_pos in exposure["active_positions"]:
        p = live_pos["row"]
        amt = live_pos["qty"]
        sym = p["symbol"]
        direction = live_pos["side"]
        entry_p = float(p.get("entryPrice", 0))
        mark_p = float(p.get("markPrice", 0))
        unrealized_pnl = float(p.get("unRealizedProfit", 0))
        leverage = int(p.get("leverage", 3))
        notional = live_pos["notional"]
        margin = notional / leverage if leverage > 0 else 0.0
        roe_pct = (unrealized_pnl / margin * 100) if margin > 0 else 0.0

        meta_trade = audit_meta.get(sym, {})
        # Entry time of the CURRENT position (issue #92): Binance fills, then a matching trades_audit record, else
        # UNKNOWN (null). Never positionRisk updateTime and never "now" (that reported 0.0h holding).
        entry_ts, entry_source, entry_diag = pt.resolve_entry_time_detailed(
            sym, direction, p.get("positionAmt"), target_env, entry_price=entry_p, fetch=eft.send_signed_request,
            audit_records=records)
        active_positions.append({
            "symbol": sym,
            "direction": direction,
            "qty": amt,
            "entry_price": entry_p,
            "mark_price": mark_p,
            "unrealized_pnl_usdt": round(unrealized_pnl, 4),
            "roe_pct": round(roe_pct, 2),
            "leverage": leverage,
            "notional_usdt": round(notional, 2),
            "margin_usdt": round(margin, 2),
            "entry_order_id": meta_trade.get("entry_order_id"),
            "entry_time_ts": entry_ts,
            "entry_time_utc": (datetime.datetime.fromtimestamp(entry_ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                               if entry_ts else "UNKNOWN"),
            "entry_time_source": entry_source,
            "sl_price": meta_trade.get("sl_price"),
            "sl_algo_id": meta_trade.get("sl_algo_id"),
            "tp1_price": meta_trade.get("tp1_price"),
            "tp2_price": meta_trade.get("tp2_price")
        })
        if entry_diag["user_trades_error"]:
            active_positions[-1]["entry_time_error"] = entry_diag["user_trades_error"]
            if entry_diag["rate_limited"]:
                active_positions[-1]["entry_time_rate_limited"] = True

    # 3. Active Algo Orders (Stop Loss) on Binance (algos_res read before positionRisk, issue #160)
    active_sl_orders = []
    if isinstance(algos_res, list):
        for a in algos_res:
            active_sl_orders.append({
                "algo_id": a.get("algoId"),
                "symbol": a.get("symbol"),
                "side": a.get("side"),
                "trigger_price": float(a.get("triggerPrice", 0)),
                "order_type": a.get("orderType"),
                "close_position": a.get("closePosition", False)
            })

    # Verify active positions with active SL orders
    algo_map = {a["symbol"]: a for a in active_sl_orders}
    for pos in active_positions:
        live_algo = algo_map.get(pos["symbol"])
        if live_algo:
            pos["sl_price"] = live_algo["trigger_price"]
            pos["sl_algo_id"] = live_algo["algo_id"]
            pos["sl_algo_verified"] = True
        else:
            pos["sl_algo_verified"] = False

    # 4. Open Limit Orders (TP1, TP2) (open_orders_res read before positionRisk, issue #160)
    active_tp_orders = []
    if isinstance(open_orders_res, list):
        for o in open_orders_res:
            active_tp_orders.append({
                "order_id": o.get("orderId"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "price": float(o.get("price", 0)),
                "qty": float(o.get("origQty", 0)),
                "reduce_only": o.get("reduceOnly", False),
                "type": o.get("type")
            })

    # 5. Today's Trades & Realized PnL. USDT sums per fill; trade counts per trade (issue #208): the day's fills are
    #    matched to the audit entries by trade_outcomes.summarize_closed_today (no extra request), so a TP1 partial
    #    plus its runner is one trade, not two wins; fills_closed keeps the per-fill count.
    start_ms = get_start_of_day_utc()
    trades_res, day_truncated = fetch_day_fills(start_ms, target_env)
    today_realized_pnl = 0.0
    today_commissions = 0.0
    fills_closed = 0

    if isinstance(trades_res, list):
        for t in trades_res:
            pnl = float(t.get("realizedPnl", 0))
            comm = float(t.get("commission", 0))
            today_commissions += comm
            if pnl != 0:
                today_realized_pnl += pnl
                fills_closed += 1

    day = {"trades_closed": 0, "wins": 0, "losses": 0, "scratches": 0, "realized_r_net_sum": 0.0,
           "partial_history": 0}
    day_error = None
    if isinstance(trades_res, list):
        try:
            import trade_outcomes
            day = trade_outcomes.summarize_closed_today(
                records, trades_res, start_ms, target_env,
                open_positions={(p["symbol"].upper(), p["direction"]) for p in active_positions})
        except Exception as e:
            day_error = f"{type(e).__name__}: {e}"[:200]
            # PR #212 review: never report "0 closed trades" after real exits; fall back to the legacy per-fill
            # counts (counted_by "fills") so the brief still sees activity, with the error alongside.
            wins_f = sum(1 for t in trades_res if float(t.get("realizedPnl", 0)) > 0)
            losses_f = sum(1 for t in trades_res if float(t.get("realizedPnl", 0)) < 0)
            day = {"trades_closed": wins_f + losses_f, "wins": wins_f, "losses": losses_f, "scratches": 0,
                   "realized_r_net_sum": None, "partial_history": 0}
    closed_trades_count = day["trades_closed"]
    wins_count = day["wins"]
    losses_count = day["losses"]

    net_realized_today = today_realized_pnl - today_commissions
    win_rate_today = (wins_count / closed_trades_count * 100) if closed_trades_count > 0 else 0.0

    # 6. Portfolio Delta Exposure Calculation
    # (utils/portfolio_exposure.compute_exposure: delta_ratio = (long - short) / (long + short), +/-0.35 thresholds)
    net_notional_delta = exposure["net_notional"]
    portfolio_delta_bias = exposure["delta_bias"]
    resting = resting_entry_exposure(target_env, algos_res, open_orders_res, exposure)

    if portfolio_delta_bias == LONG_HEAVY:
        delta_advice = "🚨 BULLISH IMBALANCE: Additional Longs prohibited. Short hedge or risk neutralization required prior to new exposure."
    elif portfolio_delta_bias == SHORT_HEAVY:
        delta_advice = "🚨 BEARISH IMBALANCE: Additional Shorts prohibited. Long support leg or risk neutralization required."
    else:
        delta_advice = "⚖️ DELTA-NEUTRAL EQUILIBRIUM: Balanced portfolio with bounded directional exposure (Δ ≈ 0)."

    # 7. Shadow Desk Telemetry & Counterfactual Metrics
    shadow_summary = {
        "active_shadow_trades": 0,
        "total_resolved": 0,
        "true_negatives": 0,
        "false_negatives": 0,
        "filter_efficacy_ratio_pct": 0.0,
        "capital_saved_usdt": 0.0,
        "missed_alpha_usdt": 0.0,
        "net_filter_edge_usdt": 0.0
    }
    try:
        import shadow_tracker
        shadow_metrics = shadow_tracker.calculate_efficacy_metrics()
        if shadow_metrics:
            shadow_summary = {
                "active_shadow_trades": shadow_metrics.get("active_shadow_trades", 0),
                "total_resolved": shadow_metrics.get("total_resolved", 0),
                "true_negatives": shadow_metrics.get("true_negatives", 0),
                "false_negatives": shadow_metrics.get("false_negatives", 0),
                "timeouts": shadow_metrics.get("timeouts", 0),
                "filter_efficacy_ratio_pct": shadow_metrics.get("filter_efficacy_ratio_pct", 0.0),
                "capital_saved_usdt": shadow_metrics.get("capital_saved_usdt", 0.0),
                "missed_alpha_usdt": shadow_metrics.get("missed_alpha_usdt", 0.0),
                "net_filter_edge_usdt": shadow_metrics.get("net_filter_edge_usdt", 0.0),
                "intraday_fer_pct": shadow_metrics.get("intraday_fer_pct", 0.0),
                "intraday_net_edge_usdt": shadow_metrics.get("intraday_net_edge_usdt", 0.0),
                "rolling_fer_pct": shadow_metrics.get("rolling_fer_pct", 0.0),
                "rolling_net_edge_usdt": shadow_metrics.get("rolling_net_edge_usdt", 0.0)
            }
    except Exception:
        pass

    # Package consolidated state
    state = {
        "is_valid": True,
        "last_updated_ts": now_ts,
        "last_updated_utc": now_utc,
        "target_env": target_env,
        "audit_read_error": audit_read_error,
        "audit_corrupt_lines": audit_corrupt_lines,
        "macro_btc": {
            "price_usdt": btc_price
        },
        "portfolio_exposure": {
            "total_active_positions": len(active_positions),
            "long_notional_usdt": round(long_notional, 2),
            "short_notional_usdt": round(short_notional, 2),
            "net_notional_delta_usdt": round(net_notional_delta, 2),
            "delta_bias": portfolio_delta_bias,
            "delta_bias_incl_resting": resting["delta_bias_incl_resting"],
            "resting_entries": resting["resting_entries"],
            "resting_margin_usdt": resting["resting_margin_usdt"],
            "resting_mismatches": resting["resting_mismatches"],
            "delta_advice": delta_advice,
            "total_floating_pnl_usdt": round(sum(p["unrealized_pnl_usdt"] for p in active_positions), 4)
        },
        "active_positions": active_positions,
        "active_sl_algo_orders": active_sl_orders,
        "active_tp_limit_orders": active_tp_orders,
        "closed_today_summary": {
            "closed_trades_count": closed_trades_count,
            "wins": wins_count,
            "losses": losses_count,
            "scratches": day["scratches"],
            "win_rate_pct": round(win_rate_today, 1),
            "realized_r_net": day["realized_r_net_sum"],
            "partial_history": day["partial_history"],
            "fills_closed": fills_closed,
            "truncated": day_truncated,
            "gross_realized_pnl_usdt": round(today_realized_pnl, 4),
            "commissions_usdt": round(today_commissions, 4),
            "net_realized_pnl_usdt": round(net_realized_today, 4)
        },
        "shadow_desk_summary": shadow_summary
    }
    state["closed_today_summary"]["counted_by"] = "fills" if day_error else "trades"
    if day_error:
        state["closed_today_summary"]["trade_summary_error"] = day_error

    # Save to atomic file with kernel-level replace (no non-atomic fallback, issue #127)
    return _write_state(state)

def _fmt_r(value):
    """Signed R for the markdown summary; "n/a" when the per-trade summary failed (counted_by "fills")."""
    return "n/a" if value is None else f"{value:+.2f}R"


def format_markdown_summary(state: dict) -> str:
    """Generates a compact Markdown report for direct consumption by any agent."""
    if not state.get("is_valid", True):
        return (
            f"# 🚨 SESSION & PORTFOLIO STATE SYNC ERROR ({state.get('last_updated_utc', 'N/A')})\n\n"
            f"**Status:** `INVALID` | **Env:** {str(state.get('target_env', 'UNKNOWN')).upper()}\n"
            f"**Error:** {state.get('error', 'Ledger synchronization failed')}\n\n"
            f"⚠️ Trading gates are FAIL-CLOSED until a valid session state is synchronized."
        )

    exp = state["portfolio_exposure"]
    closed = state["closed_today_summary"]
    btc = state["macro_btc"]
    partial_note = (f" | Entered before today: {closed['partial_history']}" if closed.get("partial_history") else "")
    if closed.get("truncated"):
        partial_note += " | ⚠️ day fills truncated (figures may be incomplete)"

    lines = [
        f"# 📡 SESSION & PORTFOLIO STATE ({state['last_updated_utc']})",
        f"**BTC:** ${btc['price_usdt']:,.2f} USDT | **Env:** {state['target_env'].upper()}",
        "",
        "### 📊 Today's Operating Balance",
        f"* **Closed Trades Today:** {closed['closed_trades_count']} (Wins: {closed['wins']} | Losses: {closed['losses']} | Scratches: {closed.get('scratches', 0)} | Win Rate: {closed['win_rate_pct']}%)"
        f" | **Realized R (net):** {_fmt_r(closed.get('realized_r_net', 0.0))} | Closing fills: {closed.get('fills_closed', 0)}"
        f"{partial_note}",
        f"* **Net Realized PnL Today:** **{'+' if closed['net_realized_pnl_usdt'] >= 0 else ''}{closed['net_realized_pnl_usdt']:.4f} USDT** (Commissions: -${closed['commissions_usdt']:.4f})",
        f"* **Total Floating PnL:** **{'+' if exp['total_floating_pnl_usdt'] >= 0 else ''}{exp['total_floating_pnl_usdt']:.4f} USDT**",
        "",
        f"### ⚖️ Portfolio Exposure & Delta: `{exp['delta_bias']}`",
        f"* **Long Notional:** ${exp['long_notional_usdt']:.2f} | **Short Notional:** ${exp['short_notional_usdt']:.2f} | **Net Delta:** ${exp['net_notional_delta_usdt']:+.2f}",
        f"* **Tactical Rule:** {exp['delta_advice']}",
        f"* **Incl. resting entries:** `{exp.get('delta_bias_incl_resting', 'UNKNOWN')}` "
        f"({len(exp.get('resting_entries') or [])} resting, margin ${exp.get('resting_margin_usdt', 0.0):.2f})",
        "",
        f"### 🛡️ Active Positions ({exp['total_active_positions']})"
    ]

    if not state["active_positions"]:
        lines.append("* *No open positions. Portfolio in flat rest.*")
    else:
        lines.append("| Pair | Dir | Entry | Mark | PnL (USDT) | ROE % | Margin | Algo SL | TP1 / TP2 |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for p in state["active_positions"]:
            sl_icon = "✅" if p.get("sl_algo_verified") else "🚨 ORPHAN"
            tp_str = f"{p.get('tp1_price', 'N/A')} / {p.get('tp2_price', 'N/A')}"
            lines.append(f"| **{p['symbol']}** | {p['direction']} {p['leverage']}x | {p['entry_price']} | {p['mark_price']} | {p['unrealized_pnl_usdt']:+.2f} | {p['roe_pct']:+.1f}% | ${p['margin_usdt']:.2f} | {sl_icon} {p.get('sl_price', 'N/A')} | {tp_str} |")

    sh = state.get("shadow_desk_summary")
    if sh and sh.get("total_resolved", 0) > 0:
        lines.append("")
        lines.append(f"### 👻 Shadow Desk Counterfactuals (Clean Intraday FER: {sh.get('intraday_fer_pct', 0.0)}% | Global: {sh['filter_efficacy_ratio_pct']}%)")
        lines.append(f"* **Resolved Audits:** {sh['total_resolved']} (✅ Dodged Losses / TN: {sh['true_negatives']} | ⚠️ Missed Alpha / FN: {sh['false_negatives']} | ⏳ Timeouts: {sh.get('timeouts', 0)})")
        lines.append(f"* **Intraday Clean Edge (<=4h):** **{'+' if sh.get('intraday_net_edge_usdt', 0) >= 0 else ''}${sh.get('intraday_net_edge_usdt', 0):.2f} USDT** (Rolling FER: {sh.get('rolling_fer_pct', 0.0)}%)")
        lines.append(f"* **Global Capital Saved:** **+${sh['capital_saved_usdt']:.2f} USDT** | **Missed Alpha:** -${sh['missed_alpha_usdt']:.2f} USDT (Monitoring {sh.get('active_shadow_trades', 0)} setups)")

    return "\n".join(lines)

def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Deterministic Session State Synchronizer")
    parser.add_argument("env_pos", nargs="?", default=None, help="Target execution environment (positional)")
    parser.add_argument("--env", default=None, help="Target execution environment (--env)")
    args = parser.parse_args(argv)

    cfg = eft.load_env()
    default_env = (os.environ.get("BINANCE_API_ENV") or cfg.get("BINANCE_API_ENV", "prod")).lower()
    # Normalised ("mainnet"/"production" -> "prod"), so the ledger's target_env compares equal across callers (#92).
    target_env = resolve_env(args.env or args.env_pos or default_env)
    state = sync_session_state(target_env=target_env)
    print(format_markdown_summary(state))
    # Issue #127: a written INVALID state (or a failed write) is a failed sync for the callers' return code.
    return 1 if state.get("is_valid") is not True or state.get("state_write_error") else 0


if __name__ == "__main__":
    sys.exit(main())
