#!/usr/bin/env python3
"""
sync_session_state.py - Deterministic Session and Portfolio State Synchronizer.
Acts as the Single Source of Truth for clean-room AI agent instances,
eliminating context bloat, information loss, and hallucinations.

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

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
AUDIT_LOG = os.path.join(LOGS_DIR, "trades_audit.jsonl")

def get_start_of_day_utc() -> int:
    """Returns timestamp in ms for the start of the current UTC day (00:00:00 UTC)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)

def load_audit_metadata() -> Dict[str, dict]:
    """Loads latest metadata from trades_audit.jsonl keyed by symbol."""
    meta = {}
    if os.path.exists(AUDIT_LOG):
        try:
            with open(AUDIT_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            sym = record.get("symbol")
                            if sym:
                                meta[sym] = record
                        except Exception:
                            continue
        except Exception:
            pass
    return meta

def sync_session_state(target_env: str = "testnet") -> dict:
    """
    Synchronizes directly against the Binance Futures ledger (Mainnet/Testnet)
    and generates the structured session state.
    """
    os.makedirs(LOGS_DIR, exist_ok=True)
    audit_meta = load_audit_metadata()
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_ts = int(time.time())

    # 1. Macro BTC
    btc_ticker = eft.send_signed_request("GET", "/fapi/v1/ticker/price", {"symbol": "BTCUSDT"}, target_env=target_env)
    btc_price = float(btc_ticker.get("price", 0.0)) if isinstance(btc_ticker, dict) else 0.0

    # 2. Active Ledger Positions
    pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    active_positions = []
    long_notional = 0.0
    short_notional = 0.0

    if isinstance(pos_res, list):
        for p in pos_res:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                sym = p["symbol"]
                direction = "LONG" if amt > 0 else "SHORT"
                entry_p = float(p.get("entryPrice", 0))
                mark_p = float(p.get("markPrice", 0))
                unrealized_pnl = float(p.get("unRealizedProfit", 0))
                leverage = int(p.get("leverage", 3))
                notional = abs(amt * mark_p)
                margin = notional / leverage if leverage > 0 else 0.0
                roe_pct = (unrealized_pnl / margin * 100) if margin > 0 else 0.0

                if direction == "LONG":
                    long_notional += notional
                else:
                    short_notional += notional

                meta_trade = audit_meta.get(sym, {})
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
                    "entry_time_ts": meta_trade.get("timestamp"),
                    "entry_time_utc": datetime.datetime.fromtimestamp(meta_trade.get("timestamp", now_ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if meta_trade.get("timestamp") else "Unknown",
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
    total_active_notional = long_notional + short_notional
    net_notional_delta = long_notional - short_notional
    delta_ratio = (net_notional_delta / total_active_notional) if total_active_notional > 0 else 0.0

    if delta_ratio > 0.35:
        portfolio_delta_bias = "LONG_HEAVY"
        delta_advice = "🚨 BULLISH IMBALANCE: Additional Longs prohibited. Short hedge or risk neutralization required prior to new exposure."
    elif delta_ratio < -0.35:
        portfolio_delta_bias = "SHORT_HEAVY"
        delta_advice = "🚨 BEARISH IMBALANCE: Additional Shorts prohibited. Long support leg or risk neutralization required."
    else:
        portfolio_delta_bias = "DELTA_BALANCED"
        delta_advice = "⚖️ DELTA-NEUTRAL EQUILIBRIUM: Balanced portfolio with bounded directional exposure (Δ ≈ 0)."

    # Package consolidated state
    state = {
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
        }
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

    return "\n".join(lines)

if __name__ == "__main__":
    env = sys.argv[1] if len(sys.argv) > 1 else "testnet"
    state = sync_session_state(target_env=env)
    print(format_markdown_summary(state))
