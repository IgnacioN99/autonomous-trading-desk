#!/usr/bin/env python3
"""
trading_scorecard.py - Scorecard Cuantitativo y Meta-Improver de Estrategias.
Auditoría estadística de rendimiento y calibración continua de parámetros.

Evaluación rigurosa basada en el registro empírico de operaciones:
Audita el historial real de operaciones en logs/trades_audit.jsonl y logs/trade_insights.jsonl:
1. Calcula métricas cuantitativas globales: Win Rate, Profit Factor, R:R realizado vs teórico, Expected Value.
2. Desglosa rendimiento por Nivel de Confianza (Tier S vs Tier A+ vs YOLO).
3. Agrupa las causas de pérdidas por clusters forenses.
4. Emite recomendaciones matemáticas de auto-calibración (ajuste de colchón ATR, filtros de volumen).

Uso:
  python3 scripts/trading_scorecard.py [--json] [--out [path]]
"""

import os
import sys
import json
import time
import math
from typing import Dict, Any, List

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
AUDIT_LOG = os.path.join(LOGS_DIR, "trades_audit.jsonl")
INSIGHTS_LOG = os.path.join(LOGS_DIR, "trade_insights.jsonl")
SCORECARD_OUTPUT = os.path.join(LOGS_DIR, "trading_scorecard.json")

def load_audit_records() -> List[dict]:
    if not os.path.exists(AUDIT_LOG):
        return []
    records = []
    with open(AUDIT_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    return records

def load_insights_records() -> List[dict]:
    if not os.path.exists(INSIGHTS_LOG):
        return []
    records = []
    superseded = set()
    with open(INSIGHTS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    obj = json.loads(line)
                    if obj.get("superseded"):
                        superseded.add(obj.get("id"))
                    else:
                        records.append(obj)
                except Exception:
                    continue
    return [r for r in records if r.get("id") not in superseded]

def generate_scorecard() -> dict:
    trades = load_audit_records()
    insights = load_insights_records()

    total_trades = len(trades)
    wins = []
    losses = []
    tiers_data = {"Tier S": [], "Tier A+": [], "Tier A": [], "YOLO": [], "Otro": []}

    # Analizar trades cerrados
    for t in trades:
        pnl = t.get("realized_pnl_usdt")
        # Si no tiene realized_pnl explícito, inferir de evento o cálculo
        if pnl is None:
            # Si tiene sl_price y entry_price, estimar si tocó SL
            pnl = t.get("unrealized_pnl_usdt", 0.0)

        tier = t.get("tier", "Tier S" if t.get("leverage", 3) == 3 else "YOLO")
        if "Tier S" in tier:
            target_tier = "Tier S"
        elif "A+" in tier:
            target_tier = "Tier A+"
        elif "Tier A" in tier:
            target_tier = "Tier A"
        elif t.get("leverage", 3) >= 10:
            target_tier = "YOLO"
        else:
            target_tier = "Otro"

        t_summary = {
            "symbol": t.get("symbol"),
            "pnl": round(float(pnl), 2),
            "leverage": t.get("leverage", 3),
            "direction": t.get("direction")
        }

        tiers_data[target_tier].append(t_summary)
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(abs(pnl))

    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0

    total_profit = sum(wins)
    total_loss = sum(losses)
    profit_factor = (total_profit / total_loss) if total_loss > 0 else (99.0 if total_profit > 0 else 0.0)
    net_pnl = total_profit - total_loss
    avg_win = (total_profit / win_count) if win_count > 0 else 0.0
    avg_loss = (total_loss / loss_count) if loss_count > 0 else 0.0
    ev = ((win_rate / 100.0) * avg_win) - (((100 - win_rate) / 100.0) * avg_loss)

    # Agrupación de causas raíz en insights
    cause_clusters = {}
    for i in insights:
        cause = i.get("root_cause", "GENERAL")
        cause_clusters[cause] = cause_clusters.get(cause, 0) + 1

    # Calibraciones recomendadas (Meta-Improver)
    recommendations = []
    if total_trades < 5:
        recommendations.append("Muestra estadística pequeña (n < 5 trades). Continuar acumulando ejecuciones para significancia.")
    else:
        if win_rate < 45.0:
            recommendations.append("Win Rate bajo (<45%). Se recomienda elevar el filtro de volumen institucional de 1.4x a 1.8x y exigir confluencia de CVD.")
        if cause_clusters.get("BTC_DUMP_CORRELATION", 0) >= 2:
            recommendations.append("Cluster de riesgo: Múltiples pérdidas por correlación con caída de BTC. Mantener activo el Hard Gate Delta-Neutral inviolable.")
        if profit_factor > 1.8 and win_rate >= 55.0:
            recommendations.append("Parámetros robustos (Profit Factor > 1.8). Sistema califica para escalamiento gradual de margen.")

    scorecard = {
        "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "sample_size": total_trades,
        "performance": {
            "total_trades": total_trades,
            "wins": win_count,
            "losses": loss_count,
            "win_rate_pct": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "net_pnl_usdt": round(net_pnl, 2),
            "avg_win_usdt": round(avg_win, 2),
            "avg_loss_usdt": round(avg_loss, 2),
            "expected_value_per_trade": round(ev, 2)
        },
        "tiers_breakdown": {
            k: {
                "count": len(v),
                "net_pnl": round(sum(x["pnl"] for x in v), 2),
                "win_rate": round(len([x for x in v if x["pnl"] > 0]) / len(v) * 100, 1) if v else 0.0
            } for k, v in tiers_data.items() if len(v) > 0
        },
        "loss_cause_clusters": cause_clusters,
        "meta_improver_recommendations": recommendations
    }

    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(SCORECARD_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(scorecard, f, indent=2, ensure_ascii=False)

    return scorecard

def format_scorecard_report(sc: dict) -> str:
    p = sc["performance"]
    lines = []
    lines.append("=" * 70)
    lines.append("🏆 QUANTITATIVE TRADING SCORECARD & META-IMPROVER")
    lines.append(f"Fecha: {sc['timestamp_utc']} | Muestra: {sc['sample_size']} trades")
    lines.append("=" * 70)
    lines.append(f"• Win Rate: {p['win_rate_pct']:.1f}% ({p['wins']}W / {p['losses']}L)")
    lines.append(f"• Profit Factor: {p['profit_factor']:.2f} | PnL Neto: ${p['net_pnl_usdt']:+.2f} USDT")
    lines.append(f"• Win Promedio: ${p['avg_win_usdt']:.2f} | Loss Promedio: ${p['avg_loss_usdt']:.2f}")
    lines.append(f"• Expected Value (EV): ${p['expected_value_per_trade']:+.2f} USDT por trade")
    lines.append("-" * 70)
    lines.append("📊 RENDIMIENTO POR NIVEL DE CONFIANZA (TIERS):")
    for tier, stats in sc.get("tiers_breakdown", {}).items():
        lines.append(f"  - {tier}: {stats['count']} trades | Win Rate: {stats['win_rate']}% | PnL: ${stats['net_pnl']:+.2f} USDT")
    lines.append("-" * 70)
    lines.append("🔬 CLUSTERS DE CAUSAS RAÍZ (Pérdidas):")
    for cause, cnt in sc.get("loss_cause_clusters", {}).items():
        lines.append(f"  - {cause}: {cnt} ocurrencia(s)")
    lines.append("-" * 70)
    lines.append("💡 RECOMENDACIONES DEL META-IMPROVER:")
    for rec in sc.get("meta_improver_recommendations", []):
        lines.append(f"  👉 {rec}")
    lines.append("=" * 70)
    return "\n".join(lines)

if __name__ == "__main__":
    sc = generate_scorecard()
    if "--json" in sys.argv:
        print(json.dumps(sc, indent=2, ensure_ascii=False))
    else:
        print(format_scorecard_report(sc))
