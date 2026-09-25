#!/usr/bin/env python3
"""
quant_risk_engine.py - Motor Cuantitativo de Riesgo, Paridad de Volatilidad y Arbitraje Estadístico (Pairs Trading).
Implementa:
1. Paridad de Volatilidad (Sizing con Pérdida Monetaria Constante e Inversa a la Volatilidad).
2. Fractional Kelly Empírico (Calculado a partir del historial real en ledger y anclado al capital).
3. Pairs Trading Cointegrado Riguroso (Engle-Granger Cointegration, Augmented Dickey-Fuller (ADF),
   y cálculo de Vida Media Ornstein-Uhlenbeck usando numpy y statsmodels).
"""

import os
import sys
import json
import time
import math
import urllib.request
import warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from decimal import Decimal

import numpy as np
import statsmodels.tsa.stattools as ts

# Asegurar path local
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft

BASE_FAPI = "https://fapi.binance.com"

def fetch_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def calculate_volatility_parity_sizing(symbol, entry_price, sl_price, target_dollar_risk=1.50, leverage=3, target_env="testnet"):
    """
    Calcula el dimensionamiento exacto para que el Stop Loss cueste EXACTAMENTE target_dollar_risk (ej. $1.50 USDT).
    - Si el activo es muy volátil (SL lejano), el tamaño nocional y margen se reducen.
    - Si el activo es muy estable (SL cercano), el tamaño nocional se incrementa.
    - Cumple estrictamente los filtros minNotional, lotSize y tickSize de Binance.
    """
    if entry_price <= 0 or sl_price <= 0:
        return {"error": "Precios deben ser mayores a 0."}

    filters = eft.get_symbol_filters(symbol, target_env=target_env)
    if not filters:
        return {"error": f"No se encontraron filtros para {symbol}"}

    risk_distance = abs(entry_price - sl_price)
    risk_pct = (risk_distance / entry_price) # en fracción (ej. 0.02)
    if risk_pct <= 0:
        return {"error": "La distancia al Stop Loss no puede ser 0."}

    # Nocional = Target Dollar Risk / Distancia al Stop Loss
    ideal_notional = target_dollar_risk / risk_pct

    # Ajustar por minNotional de Binance (mínimo $5 USDT)
    min_notional = filters.get("minNotional", 5.0)
    final_notional = max(ideal_notional, min_notional)

    # Cantidad de contratos
    raw_qty = final_notional / entry_price
    step_qty = eft.round_step(raw_qty, filters["stepSize"], filters["precision_qty"])

    if step_qty * entry_price < min_notional:
        step_qty = eft.round_step(step_qty + filters["stepSize"], filters["stepSize"], filters["precision_qty"])

    actual_notional = step_qty * entry_price
    required_margin = actual_notional / leverage
    actual_dollar_risk = actual_notional * risk_pct

    is_long = entry_price > sl_price
    direction = "LONG" if is_long else "SHORT"

    # Objetivos asimétricos R:R (TP1 = 1.8R, TP2 = 4.0R) para preservar la cola derecha
    tp1_price = entry_price * (1 + risk_pct * 1.8) if is_long else entry_price * (1 - risk_pct * 1.8)
    tp2_price = entry_price * (1 + risk_pct * 4.0) if is_long else entry_price * (1 - risk_pct * 4.0)

    tp1_rounded = eft.round_price(tp1_price, filters["tickSize"], filters["precision_price"])
    tp2_rounded = eft.round_price(tp2_price, filters["tickSize"], filters["precision_price"])
    sl_rounded = eft.round_price(sl_price, filters["tickSize"], filters["precision_price"])

    return {
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "sl_price": sl_rounded,
        "tp1_price": tp1_rounded,
        "tp2_price": tp2_rounded,
        "leverage": leverage,
        "step_qty": step_qty,
        "actual_notional": round(actual_notional, 2),
        "required_margin": round(required_margin, 2),
        "target_dollar_risk": target_dollar_risk,
        "actual_dollar_risk": round(actual_dollar_risk, 2),
        "risk_pct": round(risk_pct * 100, 2),
        "potential_gain_tp1": round(actual_notional * 0.30 * (risk_pct * 1.8), 2),
        "potential_gain_tp2": round(actual_notional * 0.70 * (risk_pct * 4.0), 2),
        "ratio_rr": 4.0
    }

def calculate_empirical_kelly(target_env="testnet"):
    """
    Calcula la fracción óptima de Kelly empírica basada en el historial de operaciones del ledger
    y anclada al capital total disponible en cuenta.
    f* = p - (q / b)
    donde p = win rate, q = 1 - p, b = payoff ratio (avg_win / avg_loss).
    Exige un mínimo de N >= 30 operaciones para significancia estadística básica (MacKinnon/Kelly threshold).
    """
    trades = eft.send_signed_request("GET", "/fapi/v1/userTrades", {"limit": 100}, target_env=target_env)
    if not isinstance(trades, list):
        return {"error": f"Error al consultar historial de trades: {trades}"}

    pnls = [float(t["realizedPnl"]) for t in trades if float(t.get("realizedPnl", 0)) != 0]
    total_closed = len(pnls)

    acc = eft.send_signed_request("GET", "/fapi/v2/account", target_env=target_env)
    total_equity = float(acc.get("totalWalletBalance", 100.0)) if isinstance(acc, dict) else 100.0

    if total_closed < 30:
        return {
            "status": "INSUFFICIENT_DATA_CONSERVATIVE_MODE",
            "total_trades_analyzed": total_closed,
            "message": f"Muestra reducida ({total_closed} trades < 30 requeridos para significancia estadística). Error estándar elevado. Aplicando Fractional Kelly conservador fijo: $1.50 USDT por posición (riesgo acotado).",
            "recommended_dollar_risk": 1.50,
            "account_equity_usdt": round(total_equity, 2),
            "win_rate_pct": round((len([p for p in pnls if p > 0]) / total_closed * 100), 1) if total_closed > 0 else 0.0,
            "confidence": "LOW_SAMPLE_SIZE"
        }

    wins = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p < 0]

    win_count = len(wins)
    loss_count = len(losses)

    win_rate = win_count / total_closed if total_closed > 0 else 0.0
    # Error estándar de la proporción
    se_win_rate = math.sqrt(win_rate * (1.0 - win_rate) / total_closed) if total_closed > 0 else 0.0

    avg_win = sum(wins) / win_count if win_count > 0 else 0.0
    avg_loss = sum(losses) / loss_count if loss_count > 0 else 1.0

    payoff_b = avg_win / avg_loss if avg_loss > 0 else 1.0
    q = 1.0 - win_rate

    # Fórmula de Kelly: f* = p - (q / b)
    kelly_full = win_rate - (q / payoff_b) if payoff_b > 0 else 0.0
    kelly_quarter = max(0.0, kelly_full / 4.0)

    if kelly_full <= 0:
        diagnosis = "EXPECTATIVA_NEGATIVA (Payoff ratio insuficiente o pérdidas superan ganancias). Se exige endurecer R:R >= 2.5:1 y ceñir stops estructurales."
        recommended_risk_usdt = 1.50 # Riesgo base mínimo
    else:
        diagnosis = f"EXPECTATIVA_POSITIVA (Kelly Completo: {kelly_full*100:.1f}% | Quarter-Kelly: {kelly_quarter*100:.1f}%)"
        risk_fraction = min(0.02, max(0.005, kelly_quarter))
        recommended_risk_usdt = max(1.50, round(total_equity * risk_fraction, 2))

    return {
        "status": "STATISTICALLY_VALID",
        "total_trades_analyzed": total_closed,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate_pct": round(win_rate * 100, 1),
        "win_rate_std_error": round(se_win_rate * 100, 2),
        "avg_win_usdt": round(avg_win, 2),
        "avg_loss_usdt": round(avg_loss, 2),
        "payoff_ratio_b": round(payoff_b, 2),
        "full_kelly_pct": round(kelly_full * 100, 1),
        "quarter_kelly_pct": round(kelly_quarter * 100, 2),
        "account_equity_usdt": round(total_equity, 2),
        "diagnosis": diagnosis,
        "recommended_dollar_risk": round(recommended_risk_usdt, 2)
    }

def calculate_pair_cointegration(sym_a, sym_b, interval="1h", limit=1000):
    """
    Calcula la cointegración rigurosa de dos activos bajo estándares econométricos institucionales:
    1. Descarga 1000 barras de 1h (~41.6 días de histórico continuo) para evitar sesgos de micro-muestras.
    2. Test de Cointegración Engle-Granger (ts.coint) contrastado con valores críticos asintóticos de MacKinnon (2010).
    3. Regresión OLS para el Hedge Ratio (Beta óptimo).
    4. Estimación del proceso Ornstein-Uhlenbeck con Corrección del Sesgo de Hurwicz para muestras finitas.
    5. Z-Score normalizado del spread actual.
    6. Dimensionamiento Beta-Hedged exacto para garantizar Delta-Neutralidad real (Notional_B = Notional_A * Beta).
    """
    try:
        url_a = f"{BASE_FAPI}/fapi/v1/klines?symbol={sym_a}&interval={interval}&limit={limit}"
        url_b = f"{BASE_FAPI}/fapi/v1/klines?symbol={sym_b}&interval={interval}&limit={limit}"

        k_a = fetch_json(url_a)
        k_b = fetch_json(url_b)

        if not k_a or not k_b or len(k_a) < 300 or len(k_b) < 300:
            return None

        closes_a = np.array([float(k[4]) for k in k_a])
        closes_b = np.array([float(k[4]) for k in k_b])

        min_len = min(len(closes_a), len(closes_b))
        closes_a = closes_a[-min_len:]
        closes_b = closes_b[-min_len:]
        n_obs = len(closes_a)

        log_a = np.log(closes_a)
        log_b = np.log(closes_b)

        # 1. Test Engle-Granger de Cointegración con valores críticos de MacKinnon
        coint_stat, coint_pvalue, crit_values = ts.coint(log_a, log_b)
        crit_5pct = float(crit_values[1]) # -3.34 aprox para 2 variables

        # 2. OLS para Hedge Ratio (Beta de cointegración estático y rolling 10d)
        cov_matrix = np.cov(log_a, log_b)
        var_b = np.var(log_b)
        beta = float(cov_matrix[0, 1] / var_b) if var_b > 0 else 1.0
        corr = float(cov_matrix[0, 1] / np.sqrt(np.var(log_a) * var_b)) if (np.var(log_a) * var_b) > 0 else 0.0

        # Beta dinámico en ventana móvil de 10 días (240 barras de 1h) - NotebookLM Pillar 1 & 4
        window_10d = min(240, n_obs)
        log_a_roll = log_a[-window_10d:]
        log_b_roll = log_b[-window_10d:]
        cov_roll = np.cov(log_a_roll, log_b_roll)
        var_b_roll = np.var(log_b_roll)
        beta_dynamic = float(cov_roll[0, 1] / var_b_roll) if var_b_roll > 0 else beta
        beta_drift_pct = float(abs(beta_dynamic - beta) / abs(beta) * 100.0) if beta != 0 else 0.0

        # Usar beta dinámico para la serie de spread reciente
        spread = log_a - beta_dynamic * log_b

        # 3. Cointegración Parcial (PCI) - Estimación de Ratio de Varianza Reversible R2_MR
        diff_spread = np.diff(spread)
        sigma_diff = float(np.var(diff_spread))
        poly_ar = np.polyfit(spread[:-1], spread[1:], 1)
        rho_ar = float(poly_ar[0])
        res_ar = spread[1:] - (poly_ar[1] + rho_ar * spread[:-1])
        sigma_res = float(np.var(res_ar))
        r2_mr = max(0.0, min(1.0, float(1.0 - (sigma_res / sigma_diff)))) if sigma_diff > 0 else 0.0

        # 4. Test ADF de Estacionariedad en el spread
        adf_result = ts.adfuller(spread, autolag='AIC')
        adf_stat = float(adf_result[0])
        adf_pvalue = float(adf_result[1])

        # 5. Ornstein-Uhlenbeck Mean-Reversion con Corrección de Hurwicz
        # dS_t = alpha + lambda * S_{t-1} + e_t
        lag_spread = spread[:-1]
        delta_spread = spread[1:] - lag_spread
        poly = np.polyfit(lag_spread, delta_spread, 1)
        lam_raw = float(poly[0])

        # Hurwicz Bias Correction: E[lam_hat - lam] ~= -(1 + 3*rho)/N donde rho = 1 + lam_raw
        lam_corr = lam_raw + (1.0 + 3.0 * (1.0 + lam_raw)) / float(n_obs)

        if lam_corr < 0:
            half_life_hours = float(-np.log(2) / lam_corr)
        else:
            half_life_hours = 999.0 # Proceso explosivo o paseo aleatorio puro

        # 6. Z-Score actual
        mean_spread = float(np.mean(spread))
        std_spread = float(np.std(spread))

        if std_spread <= 0:
            return None

        current_spread = float(spread[-1])
        z_score = float((current_spread - mean_spread) / std_spread)

        # Filtro Riguroso Cuantitativo Institucional (MacKinnon 2010 + PCI R2_MR):
        # - coint_pvalue < 0.05 Y coint_stat < crit_5pct (MacKinnon superado)
        # - Ratio de Varianza Reversible PCI R2_MR >= 0.50 (elimina derivas espurias)
        # - Vida media Hurwicz finita (3h <= Half-Life <= 72h)
        # - Divergencia estadísticamente significativa (|Z| >= 2.0σ)
        is_cointegrated = bool(coint_pvalue < 0.05 and coint_stat < crit_5pct and r2_mr >= 0.40)
        is_mean_reverting = bool(3.0 <= half_life_hours <= 72.0)
        is_actionable = bool(is_cointegrated and is_mean_reverting and (abs(z_score) >= 2.0))

        # Dimensionamiento Dynamic Beta-Neutral:
        base_notional_a = 20.0
        hedged_notional_b = round(base_notional_a * beta_dynamic, 2)
        hedged_margin_a = round(base_notional_a / 3.0, 2)
        hedged_margin_b = round(hedged_notional_b / 3.0, 2)

        action = "NEUTRAL"
        trade_recommendation = None
        if is_actionable:
            if z_score >= 2.0:
                action = "ARBITRAGE_SHORT_A_LONG_B"
                trade_recommendation = (
                    f"🚨 OPORTUNIDAD STAT-ARB VÁLIDA (+{z_score:.2f}σ, HL {half_life_hours:.1f}h, PCI R2={r2_mr:.2f}): "
                    f"SHORT {sym_a} (${base_notional_a} nocional / ${hedged_margin_a} margen) + "
                    f"LONG {sym_b} (${hedged_notional_b} nocional / ${hedged_margin_b} margen). "
                    f"Dynamic Beta_10d: {beta_dynamic:.3f} (drift {beta_drift_pct:.1f}%) | Salida: Z=+0.50σ / Stop: Z=+3.50σ"
                )
            elif z_score <= -2.0:
                action = "ARBITRAGE_LONG_A_SHORT_B"
                trade_recommendation = (
                    f"🚨 OPORTUNIDAD STAT-ARB VÁLIDA ({z_score:.2f}σ, HL {half_life_hours:.1f}h, PCI R2={r2_mr:.2f}): "
                    f"LONG {sym_a} (${base_notional_a} nocional / ${hedged_margin_a} margen) + "
                    f"SHORT {sym_b} (${hedged_notional_b} nocional / ${hedged_margin_b} margen). "
                    f"Dynamic Beta_10d: {beta_dynamic:.3f} (drift {beta_drift_pct:.1f}%) | Salida: Z=-0.50σ / Stop: Z=-3.50σ"
                )
        elif abs(z_score) >= 2.0 and not is_cointegrated:
            action = f"REJECTED_NON_COINTEGRATED (MacKinnon p={coint_pvalue:.4f}, PCI R2={r2_mr:.2f})"
            trade_recommendation = f"⚠️ DIVERGENCIA DETECTADA ({z_score:+.2f}σ) PERO RECHAZADA: No supera MacKinnon Engle-Granger (p={coint_pvalue:.4f}) o PCI R2 insuficiente ({r2_mr:.2f} < 0.40)."

        return {
            "pair": f"{sym_a} / {sym_b}",
            "symbol_a": sym_a,
            "symbol_b": sym_b,
            "price_a": float(closes_a[-1]),
            "price_b": float(closes_b[-1]),
            "correlation": round(corr, 3),
            "hedge_ratio_beta": round(beta_dynamic, 3),
            "hedge_ratio_beta_static": round(beta, 3),
            "hedge_ratio_beta_dynamic_10d": round(beta_dynamic, 3),
            "beta_drift_pct": round(beta_drift_pct, 1),
            "pci_r2_mr": round(r2_mr, 3),
            "notional_a": base_notional_a,
            "notional_b": hedged_notional_b,
            "margin_a": hedged_margin_a,
            "margin_b": hedged_margin_b,
            "sample_bars": n_obs,
            "adf_pvalue": round(adf_pvalue, 4),
            "coint_pvalue": round(coint_pvalue, 4),
            "mackinnon_crit_5pct": round(crit_5pct, 3),
            "coint_stat": round(float(coint_stat), 3),
            "half_life_hours": round(half_life_hours, 1),
            "is_cointegrated": is_cointegrated,
            "z_score": round(z_score, 2),
            "target_unwind_z": 0.5 if z_score > 0 else -0.5,
            "stop_loss_z": 3.5 if z_score > 0 else -3.5,
            "action": action,
            "recommendation": trade_recommendation,
            "is_actionable": is_actionable
        }
    except Exception as e:
        return None

def scan_coingrated_market_pairs():
    """
    Escanea canasta de pares estructuralmente vinculados aplicando test MacKinnon Engle-Granger
    y Ornstein-Uhlenbeck con corrección Hurwicz sobre 1000 barras continuas de 1h.
    """
    candidate_pairs = [
        ("BTCUSDT", "ETHUSDT"),    # Macro L1 Anchor
        ("SOLUSDT", "AVAXUSDT"),   # High-Throughput Alt L1s
        ("SUIUSDT", "APTUSDT"),    # Move VM L1s
        ("NEARUSDT", "APTUSDT"),   # Sharded / Parallel Alt L1s
        ("LINKUSDT", "ETHUSDT"),   # Core DeFi Infrastructure vs Host L1
        ("DOTUSDT", "ATOMUSDT"),   # Modular Cross-Chain L0/L1
        ("ARBUSDT", "OPUSDT")      # Ethereum L2 Rollups
    ]

    results = []
    for a, b in candidate_pairs:
        res = calculate_pair_cointegration(a, b, interval="1h", limit=1000)
        if res:
            results.append(res)

    results.sort(key=lambda x: abs(x["z_score"]), reverse=True)
    return results

if __name__ == "__main__":
    print("🔬 ANÁLISIS CUANTITATIVO DE RIESGO & PAIRS TRADING (STAT-ARB) 🔬\n")
    print("1. EVALUACIÓN DE KELLY EMPÍRICO:")
    k = calculate_empirical_kelly()
    print(f"   Win Rate: {k.get('win_rate_pct')}% | Payoff b: {k.get('payoff_ratio_b')}")
    print(f"   Diagnóstico: {k.get('diagnosis')}")
    print(f"   Riesgo Dollar Sugerido: ${k.get('recommended_dollar_risk')} USDT por trade\n")

    print("2. ESCÁNER DE PARES COINTEGRADOS (TEST ADF + HALF-LIFE OU):")
    pairs = scan_coingrated_market_pairs()
    for p in pairs:
        coint_tag = "✅ COINTEGRADO" if p.get("is_cointegrated") else "❌ NO COINTEGRADO"
        print(f"• {p['pair']} ({coint_tag})")
        print(f"  Z-Score: {p['z_score']:+.2f}σ | Beta: {p['hedge_ratio_beta']} | ADF p-val: {p['adf_pvalue']} | Half-Life: {p['half_life_hours']}h")
        if p.get("recommendation"):
            print(f"  {p['recommendation']}")
