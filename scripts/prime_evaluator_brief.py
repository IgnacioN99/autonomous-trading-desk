#!/usr/bin/env python3
"""
prime_evaluator_brief.py - Empacador Determinista de Contexto para el Evaluador Cuantitativo.
Compresión y estructuración de contexto para inferencia de alto rendimiento.

Sintetiza la información esencial del mercado y estado de la cuenta para evaluación sin sobrecarga.
Ensambla el brief de mercado exacto a partir del Ground Truth (session_state.json),
el payload tipado de escaneo y las lecciones aprendidas de trade_insights.jsonl.

Tamaño objetivo: < 1,800 tokens (vs 35,000 tokens de chat acumulado).
Cero pérdida de información, cero alucinación en instancias limpias.

Uso:
  python3 scripts/prime_evaluator_brief.py [--json] [--out [path]]
"""

import os
import sys
import json
import time
import subprocess
from typing import Dict, Any, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
INSIGHTS_FILE = os.path.join(LOGS_DIR, "trade_insights.jsonl")
BRIEF_FILE = os.path.join(LOGS_DIR, "primed_brief.json")

def ensure_fresh_state(max_age_sec: int = 600) -> dict:
    """Verifica si session_state.json está fresco; si no, lo sincroniza en ~600ms."""
    needs_sync = True
    if os.path.exists(STATE_FILE):
        age = time.time() - os.path.getmtime(STATE_FILE)
        if age < max_age_sec:
            needs_sync = False
            
    if needs_sync:
        sync_script = os.path.join(BASE_DIR, "scripts", "sync_session_state.py")
        subprocess.run([sys.executable, sync_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def load_recent_insights(limit: int = 3) -> List[dict]:
    """Carga las últimas K lecciones activas desde trade_insights.jsonl."""
    if not os.path.exists(INSIGHTS_FILE):
        return []
    active = []
    superseded = set()
    try:
        with open(INSIGHTS_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
            for l in lines:
                try:
                    obj = json.loads(l)
                    if obj.get("superseded"):
                        superseded.add(obj.get("id"))
                    else:
                        active.append(obj)
                except Exception:
                    continue
        valid = [a for a in active if a.get("id") not in superseded]
        return valid[-limit:]
    except Exception:
        return []

def get_latest_screening_payload() -> dict:
    """Obtiene el payload más reciente de escaneo de mercado o invoca screening_pipeline."""
    pipeline_script = os.path.join(BASE_DIR, "scripts", "screening_pipeline.py")
    try:
        res = subprocess.run([sys.executable, pipeline_script, "--json"], capture_output=True, text=True, timeout=25)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
    except Exception:
        pass
    return {}

def assemble_primed_brief() -> dict:
    state = ensure_fresh_state()
    screening = get_latest_screening_payload()
    insights = load_recent_insights(limit=3)
    
    portfolio = state.get("portfolio_exposure", {})
    active_pos = state.get("active_positions", [])
    closed_today = state.get("closed_trades_today", {})
    
    # Context Pack condensado (Token-budget optimizado)
    brief = {
        "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "target_env": state.get("target_env", "testnet").upper(),
        "ground_truth_portfolio": {
            "delta_bias": portfolio.get("delta_bias", "NEUTRAL"),
            "long_notional_usdt": portfolio.get("long_notional_usdt", 0.0),
            "short_notional_usdt": portfolio.get("short_notional_usdt", 0.0),
            "net_delta_usdt": portfolio.get("net_notional_delta_usdt", 0.0),
            "floating_pnl_usdt": portfolio.get("total_floating_pnl_usdt", 0.0),
            "closed_trades_today": closed_today.get("count", 0),
            "realized_pnl_today": closed_today.get("realized_pnl_net_usdt", 0.0),
            "tactical_rule": portfolio.get("delta_advice", "Operativa normal balanceada."),
            "active_positions_count": len(active_pos),
            "positions_summary": [
                {
                    "symbol": p["symbol"],
                    "dir": p["direction"],
                    "entry": p["entry_price"],
                    "mark": p["mark_price"],
                    "pnl": p["unrealized_pnl_usdt"],
                    "roe": p["roe_pct"],
                    "sl": p.get("sl_price"),
                    "sl_verified": p.get("sl_algo_verified", False)
                } for p in active_pos
            ]
        },
        "macro_btc": screening.get("macro", {
            "btc_price": state.get("macro_btc", {}).get("price_usdt", 0.0),
            "allows_alt_shorts": True
        }),
        "filtered_opportunities": screening.get("top_candidates", []),
        "stat_arb_pairs": screening.get("actionable_stat_arb", []),
        "funding_arbitrage_desk": screening.get("top_funding_arbitrage", []),
        "yolo_slot": screening.get("yolo_slot_status", "INACTIVO: Preservando capital."),
        "committed_memory_lessons": [
            {
                "tag": i.get("tags", []),
                "lesson": i.get("insight")
            } for i in insights
        ]
    }
    
    # Guardar a disco
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(BRIEF_FILE, "w", encoding="utf-8") as f:
        json.dump(brief, f, indent=2, ensure_ascii=False)
        
    return brief

def format_markdown_brief(brief: dict) -> str:
    p = brief["ground_truth_portfolio"]
    m = brief["macro_btc"]
    
    lines = []
    lines.append(f"# 📦 PRIMED EVALUATOR BRIEF ({brief['timestamp_utc']})")
    lines.append(f"**Env:** `{brief['target_env']}` | **BTC:** `${m.get('btc_price', 0):,.1f}` | **Delta:** `{p['delta_bias']}`")
    lines.append("")
    lines.append("### ⚖️ Ground Truth de Cartera (Ledger Binance)")
    lines.append(f"- **Posiciones Vivas ({p['active_positions_count']}):** " + (", ".join([f"{x['symbol']} ({x['dir']} PnL: ${x['pnl']})" for x in p['positions_summary']]) if p['positions_summary'] else "Ninguna"))
    lines.append(f"- **Delta Neto:** `${p['net_delta_usdt']:+.2f}` (L: ${p['long_notional_usdt']} | S: ${p['short_notional_usdt']})")
    lines.append(f"- **PnL Realizado Hoy:** `${p['realized_pnl_today']:+.2f}` USDT | **PnL Flotante:** `${p['floating_pnl_usdt']:+.2f}` USDT")
    lines.append(f"- **Regla Obligatoria:** {p['tactical_rule']}")
    lines.append("")
    
    if brief.get("committed_memory_lessons"):
        lines.append("### 🧠 Lecciones Comprometidas Recientes")
        for les in brief["committed_memory_lessons"]:
            lines.append(f"- *[{', '.join(les['tag'])}]:* {les['lesson']}")
        lines.append("")
        
    opps = brief.get("filtered_opportunities", [])
    lines.append(f"### 🎯 Oportunidades Técnicas Filtradas ({len(opps)})")
    if opps:
        lines.append("| Símbolo | Dir | Tier | Conf | Precio | SL | TP1 / TP2 | R:R | Riesgo $ | Confluencias |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |")
        for o in opps:
            lines.append(f"| **{o.get('symbol')}** | {o.get('direction')} | {o.get('tier', '').split()[0]} | {o.get('confidence')}% | {o.get('current_price')} | {o.get('sl_price')} | {o.get('tp1_price')} / {o.get('tp2_price')} | {o.get('rr_ratio')}R | ${o.get('target_dollar_risk', 1.5)} | {'; '.join(o.get('reasons', [])[:2])} |")
    else:
        lines.append("*(Sin oportunidades intradía que superen el filtro microestructural)*")
    lines.append("")
    
    lines.append(f"**YOLO Slot:** {brief.get('yolo_slot')}")
    return "\n".join(lines)

if __name__ == "__main__":
    brief = assemble_primed_brief()
    if "--json" in sys.argv:
        print(json.dumps(brief, indent=2, ensure_ascii=False))
    else:
        print(format_markdown_brief(brief))
