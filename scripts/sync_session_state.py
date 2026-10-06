#!/usr/bin/env python3
"""
sync_session_state.py - Deterministic Session and Portfolio State Synchronizer.
Writes a ledger cache for clean-room AI agent instances, eliminating context bloat, information loss,
and hallucinations. It is a cache, not an authority: the executor's PROD gates re-read the exchange and
apply the stricter of the cache and the live view (issue #101).

Zero LLM Tokens / Latency ~600ms.
Generates 'logs/session_state.json' and outputs a typed executive summary for cold-start priming.
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
from utils.portfolio_exposure import compute_exposure, LONG_HEAVY, SHORT_HEAVY
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

def load_audit_metadata(target_env: str = None) -> Dict[str, dict]:
    """Loads latest metadata from trades_audit.jsonl keyed by symbol, strictly filtered by target_env."""
    meta = {}
    # Env aliases normalised on both sides ("mainnet" == "prod"), same as utils/position_timing (issue #92).
    norm_env = pt._norm_env(target_env)
    if os.path.exists(AUDIT_LOG):
        try:
            with open(AUDIT_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            rec_env = pt._norm_env(record.get("target_env"))
                            if norm_env and rec_env and rec_env != norm_env:
                                continue
                            sym = record.get("symbol")
                            if sym:
                                meta[sym] = record
                        except Exception:
                            continue
        except Exception:
            pass
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
            "win_rate_pct": 0.0,
            "gross_realized_pnl_usdt": 0.0,
            "commissions_usdt": 0.0,
            "net_realized_pnl_usdt": 0.0
        }
    }
    try:
        from utils.atomic_writer import atomic_write_json
        atomic_write_json(STATE_FILE, error_state)
    except Exception:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(error_state, f, indent=2, ensure_ascii=False)
    return error_state

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
    audit_meta = load_audit_metadata(target_env=target_env)
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_ts = int(time.time())

    # 1. Macro BTC
    btc_ticker = eft.send_signed_request("GET", "/fapi/v1/ticker/price", {"symbol": "BTCUSDT"}, target_env=target_env)
    btc_price = float(btc_ticker.get("price", 0.0)) if isinstance(btc_ticker, dict) else 0.0

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
        entry_ts, entry_source = pt.resolve_entry_time(sym, direction, p.get("positionAmt"), target_env,
                                                       entry_price=entry_p, fetch=eft.send_signed_request,
                                                       audit_path=AUDIT_LOG)
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

    # 3. Active Algo Orders (Stop Loss) on Binance
    algos_res = eft.send_signed_request("GET", "/fapi/v1/openAlgoOrders", target_env=target_env)
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

    # 4. Open Limit Orders (TP1, TP2)
    open_orders_res = eft.send_signed_request("GET", "/fapi/v1/openOrders", target_env=target_env)
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

    # 5. Today's Trades & Realized PnL
    start_ms = get_start_of_day_utc()
    trades_res = eft.send_signed_request("GET", "/fapi/v1/userTrades", {"startTime": start_ms, "limit": 100}, target_env=target_env)
    today_realized_pnl = 0.0
    today_commissions = 0.0
    closed_trades_count = 0
    wins_count = 0
    losses_count = 0

    if isinstance(trades_res, list):
        for t in trades_res:
            pnl = float(t.get("realizedPnl", 0))
            comm = float(t.get("commission", 0))
            today_commissions += comm
            if pnl != 0:
                today_realized_pnl += pnl
                closed_trades_count += 1
                if pnl > 0:
                    wins_count += 1
                else:
                    losses_count += 1

    net_realized_today = today_realized_pnl - today_commissions
    win_rate_today = (wins_count / closed_trades_count * 100) if closed_trades_count > 0 else 0.0

    # 6. Portfolio Delta Exposure Calculation
    # (utils/portfolio_exposure.compute_exposure: delta_ratio = (long - short) / (long + short), +/-0.35 thresholds)
    net_notional_delta = exposure["net_notional"]
    portfolio_delta_bias = exposure["delta_bias"]

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
        "macro_btc": {
            "price_usdt": btc_price
        },
        "portfolio_exposure": {
            "total_active_positions": len(active_positions),
            "long_notional_usdt": round(long_notional, 2),
            "short_notional_usdt": round(short_notional, 2),
            "net_notional_delta_usdt": round(net_notional_delta, 2),
            "delta_bias": portfolio_delta_bias,
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
            "win_rate_pct": round(win_rate_today, 1),
            "gross_realized_pnl_usdt": round(today_realized_pnl, 4),
            "commissions_usdt": round(today_commissions, 4),
            "net_realized_pnl_usdt": round(net_realized_today, 4)
        },
        "shadow_desk_summary": shadow_summary
    }

    # Save to atomic file with kernel-level replace
    try:
        from utils.atomic_writer import atomic_write_json
        atomic_write_json(STATE_FILE, state)
    except Exception:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    return state

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

    lines = [
        f"# 📡 SESSION & PORTFOLIO STATE ({state['last_updated_utc']})",
        f"**BTC:** ${btc['price_usdt']:,.2f} USDT | **Env:** {state['target_env'].upper()}",
        "",
        "### 📊 Today's Operating Balance",
        f"* **Closed Trades Today:** {closed['closed_trades_count']} (Wins: {closed['wins']} | Losses: {closed['losses']} | Win Rate: {closed['win_rate_pct']}%)",
        f"* **Net Realized PnL Today:** **{'+' if closed['net_realized_pnl_usdt'] >= 0 else ''}{closed['net_realized_pnl_usdt']:.4f} USDT** (Commissions: -${closed['commissions_usdt']:.4f})",
        f"* **Total Floating PnL:** **{'+' if exp['total_floating_pnl_usdt'] >= 0 else ''}{exp['total_floating_pnl_usdt']:.4f} USDT**",
        "",
        f"### ⚖️ Portfolio Exposure & Delta: `{exp['delta_bias']}`",
        f"* **Long Notional:** ${exp['long_notional_usdt']:.2f} | **Short Notional:** ${exp['short_notional_usdt']:.2f} | **Net Delta:** ${exp['net_notional_delta_usdt']:+.2f}",
        f"* **Tactical Rule:** {exp['delta_advice']}",
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

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Deterministic Session State Synchronizer")
    parser.add_argument("env_pos", nargs="?", default=None, help="Target execution environment (positional)")
    parser.add_argument("--env", default=None, help="Target execution environment (--env)")
    args = parser.parse_args()

    cfg = eft.load_env()
    default_env = (os.environ.get("BINANCE_API_ENV") or cfg.get("BINANCE_API_ENV", "prod")).lower()
    # Normalised ("mainnet"/"production" -> "prod"), so the ledger's target_env compares equal across callers (#92).
    target_env = resolve_env(args.env or args.env_pos or default_env)
    state = sync_session_state(target_env=target_env)
    print(format_markdown_summary(state))
