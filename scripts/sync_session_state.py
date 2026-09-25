#!/usr/bin/env python3
"""
sync_session_state.py - Sincronizador Determinista de Estado de Sesión y Cartera.
Actúa como la Fuente Única de la Verdad (Single Source of Truth) para instancias limpias
de agentes de IA, eliminando la sobrecarga de contexto, la pérdida de información y las alucinaciones.

Zero Tokens LLM / Latencia ~600ms.
Genera 'logs/session_state.json' y emite un resumen ejecutivo tipado para el Cold-Start.
"""

import os
import sys
import json
import time
import datetime
from typing import Dict, List, Any

# Asegurar path local
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
AUDIT_LOG = os.path.join(LOGS_DIR, "trades_audit.jsonl")

def get_start_of_day_utc() -> int:
    """Devuelve el timestamp en ms del inicio del día actual (00:00:00 UTC)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)

def load_audit_metadata() -> Dict[str, dict]:
    """Carga los metadatos más recientes de trades_audit.jsonl por símbolo."""
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
    Sincroniza directamente contra el ledger de Binance Futures Mainnet/Testnet
    y genera el estado estructurado de la sesión.
    """
    os.makedirs(LOGS_DIR, exist_ok=True)
    audit_meta = load_audit_metadata()
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_ts = int(time.time())

    # 1. Macro BTC
    btc_ticker = eft.send_signed_request("GET", "/fapi/v1/ticker/price", {"symbol": "BTCUSDT"}, target_env=target_env)
    btc_price = float(btc_ticker.get("price", 0.0)) if isinstance(btc_ticker, dict) else 0.0

    # 2. Posiciones Activas en Ledger
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
                    "entry_time_utc": datetime.datetime.fromtimestamp(meta_trade.get("timestamp", now_ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if meta_trade.get("timestamp") else "Desconocido",
                    "sl_price": meta_trade.get("sl_price"),
                    "sl_algo_id": meta_trade.get("sl_algo_id"),
                    "tp1_price": meta_trade.get("tp1_price"),
                    "tp2_price": meta_trade.get("tp2_price")
                })

    # 3. Órdenes Algo (Stop Loss) activas en Binance
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

    # Verificar si alguna posición activa carece de Stop Loss y actualizar con el precio vivo del ledger
    algo_map = {a["symbol"]: a for a in active_sl_orders}
    for pos in active_positions:
        live_algo = algo_map.get(pos["symbol"])
        if live_algo:
            pos["sl_price"] = live_algo["trigger_price"]
            pos["sl_algo_id"] = live_algo["algo_id"]
            pos["sl_algo_verified"] = True
        else:
            pos["sl_algo_verified"] = False

    # 4. Órdenes Límite Abiertas (TP1, TP2)
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

    # 5. Trades de Hoy y PnL Realizado
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

    # 6. Cálculo de Exposición Delta de Cartera
    total_active_notional = long_notional + short_notional
    net_notional_delta = long_notional - short_notional
    delta_ratio = (net_notional_delta / total_active_notional) if total_active_notional > 0 else 0.0

    if delta_ratio > 0.35:
        portfolio_delta_bias = "LONG_HEAVY"
        delta_advice = "🚨 DESBALANCE ALCISTA: Prohibido abrir más Longs. Se exige abrir cobertura Short o neutralizar antes de nuevo riesgo."
    elif delta_ratio < -0.35:
        portfolio_delta_bias = "SHORT_HEAVY"
        delta_advice = "🚨 DESBALANCE BAJISTA: Prohibido abrir más Shorts. Se exige abrir pata Long de soporte o neutralizar."
    else:
        portfolio_delta_bias = "DELTA_BALANCED"
        delta_advice = "⚖️ EQUILIBRIO DELTA-NEUTRAL: Cartera balanceada con exposición direccional acotada (Δ ≈ 0)."

    # Empaquetar estado consolidado
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

    # Guardar en archivo atómico con kernel-level replace
    try:
        from utils.atomic_writer import atomic_write_json
        atomic_write_json(STATE_FILE, state)
    except Exception:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    return state

def format_markdown_summary(state: dict) -> str:
    """Genera un reporte compacto en Markdown para consumo directo de cualquier agente."""
    exp = state["portfolio_exposure"]
    closed = state["closed_today_summary"]
    btc = state["macro_btc"]

    lines = [
        f"# 📡 ESTADO DE SESIÓN & CARTERA ({state['last_updated_utc']})",
        f"**BTC:** ${btc['price_usdt']:,.2f} USDT | **Env:** {state['target_env'].upper()}",
        "",
        "### 📊 Balance Operativo de Hoy",
        f"* **Trades Cerrados Hoy:** {closed['closed_trades_count']} (Ganados: {closed['wins']} | Perdidos: {closed['losses']} | Win Rate: {closed['win_rate_pct']}%)",
        f"* **PnL Realizado Neto Hoy:** **{'+' if closed['net_realized_pnl_usdt'] >= 0 else ''}{closed['net_realized_pnl_usdt']:.4f} USDT** (Comisiones: -${closed['commissions_usdt']:.4f})",
        f"* **PnL Flotante Total:** **{'+' if exp['total_floating_pnl_usdt'] >= 0 else ''}{exp['total_floating_pnl_usdt']:.4f} USDT**",
        "",
        f"### ⚖️ Exposición & Delta de Cartera: `{exp['delta_bias']}`",
        f"* **Nocional Long:** ${exp['long_notional_usdt']:.2f} | **Nocional Short:** ${exp['short_notional_usdt']:.2f} | **Delta Neto:** ${exp['net_notional_delta_usdt']:+.2f}",
        f"* **Regla Táctica:** {exp['delta_advice']}",
        "",
        f"### 🛡️ Posiciones Activas ({exp['total_active_positions']})"
    ]

    if not state["active_positions"]:
        lines.append("* *Ninguna posición abierta. Cartera en reposo plano.*")
    else:
        lines.append("| Par | Dir | Entrada | Mark | PnL (USDT) | ROE % | Margen | SL Algo | TP1 / TP2 |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for p in state["active_positions"]:
            sl_icon = "✅" if p.get("sl_algo_verified") else "🚨 HUÉRFANA"
            tp_str = f"{p.get('tp1_price', 'N/A')} / {p.get('tp2_price', 'N/A')}"
            lines.append(f"| **{p['symbol']}** | {p['direction']} {p['leverage']}x | {p['entry_price']} | {p['mark_price']} | {p['unrealized_pnl_usdt']:+.2f} | {p['roe_pct']:+.1f}% | ${p['margin_usdt']:.2f} | {sl_icon} {p.get('sl_price', 'N/A')} | {tp_str} |")

    return "\n".join(lines)

if __name__ == "__main__":
    env = sys.argv[1] if len(sys.argv) > 1 else "testnet"
    state = sync_session_state(target_env=env)
    print(format_markdown_summary(state))
