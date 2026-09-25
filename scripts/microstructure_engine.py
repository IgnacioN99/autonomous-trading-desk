#!/usr/bin/env python3
"""
microstructure_engine.py - Motor de Microestructura de Mercado y Flujo de Órdenes (Order Flow) para Binance Futures.
Implementa:
1. Cumulative Volume Delta (CVD) acumulado sobre 30 períodos (7.5 horas en 15m).
2. Detección de Absorción de Liquidez Institucional (Divergencia Precio vs CVD y muros pasivos en libro).
3. Matriz de Régimen de Mercado basada en Z-Score de Open Interest (OI) y precio (NotebookLM 4-Quadrant Matrix).
4. Tape Reading dinámico en tiempo real vía aggTrades con umbral de percentil 95% para órdenes institucionales.
"""

import os
import sys
import json
import time
import math
import urllib.request
import numpy as np

BASE_FAPI = "https://fapi.binance.com"

def fetch_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def get_symbol_microstructure(symbol, period="15m", history_limit=30):
    """
    Consulta métricas cuantitativas reales de flujo de órdenes sobre una ventana de 30 períodos:
    1. Taker Buy/Sell Ratio & CVD Acumulado (Taker agresivo comprador vs vendedor)
    2. Open Interest Histórico con Z-Score (Dinero fresco institucional vs liquidaciones forzadas)
    3. Tasa de Financiación (Funding Rate) y prima de mercado
    4. Acción del precio y mechas de absorción
    """
    try:
        # 1. Taker Buy/Sell Volume Ratio (Ventana de 30 velas)
        url_taker = f"{BASE_FAPI}/futures/data/takerlongshortRatio?symbol={symbol}&period={period}&limit={history_limit}"
        taker_data = fetch_json(url_taker)

        # 2. Open Interest Histórico (Ventana de 30 velas)
        url_oi = f"{BASE_FAPI}/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={history_limit}"
        oi_data = fetch_json(url_oi)

        # 3. Premium Index (Funding Rate actual)
        url_prem = f"{BASE_FAPI}/fapi/v1/premiumIndex?symbol={symbol}"
        prem_data = fetch_json(url_prem)

        # 4. Klines (Ventana correspondiente para correlacionar precio con OI y CVD)
        url_klines = f"{BASE_FAPI}/fapi/v1/klines?symbol={symbol}&interval={period}&limit={history_limit}"
        klines = fetch_json(url_klines)

        if not taker_data or not oi_data or not klines or len(klines) < 5:
            return None

        # --- ANÁLISIS DE CUMULATIVE VOLUME DELTA (CVD) ---
        deltas = np.array([float(t.get("buyVol", 0)) - float(t.get("sellVol", 0)) for t in taker_data])
        cvd_cumulative = np.cumsum(deltas)
        cvd_current_delta = float(deltas[-1])
        cvd_window_net = float(cvd_cumulative[-1] - cvd_cumulative[0])

        latest_taker = taker_data[-1]
        t_ratio = float(latest_taker.get("buySellRatio", 1.0))
        buy_vol = float(latest_taker.get("buyVol", 0))
        sell_vol = float(latest_taker.get("sellVol", 0))

        # --- ANÁLISIS ESTADÍSTICO DE OPEN INTEREST (OI) ---
        oi_series = np.array([float(x.get("sumOpenInterest", 0)) for x in oi_data])
        oi_current = float(oi_series[-1])
        oi_prev = float(oi_series[-2])
        oi_delta = oi_current - oi_prev
        oi_change_pct = (oi_delta / oi_prev * 100) if oi_prev > 0 else 0.0

        # Calcular Z-Score del cambio de OI respecto a la ventana histórica de 30 períodos
        oi_pct_changes = np.diff(oi_series) / oi_series[:-1] * 100
        oi_std = float(np.std(oi_pct_changes)) if len(oi_pct_changes) > 2 else 1.0
        oi_mean = float(np.mean(oi_pct_changes)) if len(oi_pct_changes) > 2 else 0.0
        oi_z_score = float((oi_change_pct - oi_mean) / oi_std) if oi_std > 0 else 0.0

        oi_val_usd = float(oi_data[-1].get("sumOpenInterestValue", 0))

        # --- MÉTRICAS DE FINANCIACIÓN ---
        funding_rate = float(prem_data.get("lastFundingRate", 0))
        funding_rate_pct = funding_rate * 100 # en %
        annualized_funding = funding_rate * 3 * 365 * 100 # APR en %

        # --- MÉTRICAS DE PRECIO Y VELAS ---
        c_open = float(klines[-1][1])
        c_high = float(klines[-1][2])
        c_low = float(klines[-1][3])
        c_close = float(klines[-1][4])
        p_change_pct = ((c_close - c_open) / c_open) * 100

        # Rango y Mechas de Absorción
        candle_range = c_high - c_low if (c_high - c_low) > 0 else 1e-8
        body_top = max(c_open, c_close)
        body_bottom = min(c_open, c_close)
        lower_wick_pct = ((body_bottom - c_low) / candle_range) * 100
        upper_wick_pct = ((c_high - body_top) / candle_range) * 100

        # --- CLASIFICACIÓN DE RÉGIMEN CUANTITATIVO (Z-Score de OI >= 1.25σ o Delta Significativo) ---
        # Filtro robusto: Se requiere un cambio estadísticamente anómalo en OI (|Z| >= 1.25 o |ΔOI| >= 0.40%)
        is_oi_inflow = (oi_z_score >= 1.25 or oi_change_pct >= 0.40)
        is_oi_outflow = (oi_z_score <= -1.25 or oi_change_pct <= -0.40)

        if p_change_pct > 0.15 and is_oi_inflow:
            regime = "LONG_BUILDUP"
            regime_desc = f"Convicción Alcista (Entrada institucional de capital: Z={oi_z_score:+.2f}σ, ΔOI={oi_change_pct:+.2f}%)"
        elif p_change_pct > 0.15 and is_oi_outflow:
            regime = "SHORT_SQUEEZE"
            regime_desc = f"Short Squeeze (Subida mecánica por liquidación forzada de cortos: Z={oi_z_score:+.2f}σ, ΔOI={oi_change_pct:+.2f}%)"
        elif p_change_pct < -0.15 and is_oi_inflow:
            regime = "SHORT_BUILDUP"
            regime_desc = f"Convicción Bajista (Entrada neta de capital vendedor agresivo: Z={oi_z_score:+.2f}σ, ΔOI={oi_change_pct:+.2f}%)"
        elif p_change_pct < -0.15 and is_oi_outflow:
            regime = "LONG_UNWINDING"
            regime_desc = f"Capitulación / Long Unwinding (Liquidación masiva de compradores: Z={oi_z_score:+.2f}σ, ΔOI={oi_change_pct:+.2f}%)"
        else:
            regime = "NEUTRAL_CONSOLIDATION"
            regime_desc = f"Consolidación de Rango / Balance de Subasta (Z_OI={oi_z_score:+.2f}σ)"

        # --- DETECCIÓN DE ABSORCIÓN DE LIQUIDEZ (CVD Divergence & Limit Walls) ---
        absorption = "NONE"
        absorption_desc = "Sin absorción anómala detectada"

        # ABSORCIÓN COMPRADORA (Bullish Absorption):
        # Taker agresivo vendiendo fuerte (Taker Ratio <= 0.85 o CVD negativo en la vela),
        # pero el precio NO cae o rechaza fuertemente (mecha inferior >= 40% o p_change >= -0.15%).
        # Conclusión: Órdenes límite pasivas absorbiendo todo el flujo vendedor en soporte.
        if (t_ratio <= 0.85 or cvd_current_delta < 0) and (lower_wick_pct >= 40.0 or p_change_pct >= -0.15):
            absorption = "BULLISH_ABSORPTION"
            absorption_desc = f"Absorción Compradora Activa (Taker Ratio {t_ratio:.2f} absorbido por pared en bid, mecha {lower_wick_pct:.0f}%)"

        # ABSORCIÓN VENDEDORA (Bearish Absorption):
        # Taker agresivo comprando fuerte (Taker Ratio >= 1.25 o CVD positivo en la vela),
        # pero el precio no logra subir o deja mecha superior (mecha superior >= 40% o p_change <= 0.15%).
        # Conclusión: Bloques institucionales pasivos descargando en el ask contra el retail.
        elif (t_ratio >= 1.25 or cvd_current_delta > 0) and (upper_wick_pct >= 40.0 or p_change_pct <= 0.15):
            absorption = "BEARISH_ABSORPTION"
            absorption_desc = f"Absorción Vendedora Activa (Taker Ratio {t_ratio:.2f} absorbido por pared en ask, mecha {upper_wick_pct:.0f}%)"

        # --- DESEQUILIBRIO DEL FLUJO DE ÓRDENES (OIB) & VWAP DEVIATION (NotebookLM) ---
        total_taker_vol = buy_vol + sell_vol
        oib_ratio = float((buy_vol - sell_vol) / total_taker_vol) if total_taker_vol > 0 else 0.0

        typical_prices = np.array([(float(k[2]) + float(k[3]) + float(k[4])) / 3.0 for k in klines])
        vols = np.array([float(k[5]) for k in klines])
        sum_vols = float(np.sum(vols))
        vwap = float(np.sum(typical_prices * vols) / sum_vols) if sum_vols > 0 else c_close
        vwap_deviation_pct = float(((c_close - vwap) / vwap) * 100.0) if vwap > 0 else 0.0

        # Detección de Cascada de Liquidación (Galton-Watson lambda_hat)
        cascade_diag = detect_liquidation_cascade(symbol, oi_z_score, oi_change_pct, p_change_pct, oib_ratio)

        return {
            "symbol": symbol,
            "taker_ratio": round(t_ratio, 3),
            "oib_ratio": round(oib_ratio, 3),
            "buy_vol": buy_vol,
            "sell_vol": sell_vol,
            "cvd_current_delta": round(cvd_current_delta, 1),
            "cvd_window_net": round(cvd_window_net, 1),
            "oi_current": oi_current,
            "oi_delta": oi_delta,
            "oi_change_pct": round(oi_change_pct, 2),
            "oi_z_score": round(oi_z_score, 2),
            "oi_usd": oi_val_usd,
            "funding_rate_pct": round(funding_rate_pct, 4),
            "annualized_funding_apr": round(annualized_funding, 1),
            "price_change_pct": round(p_change_pct, 2),
            "vwap": round(vwap, 4),
            "vwap_deviation_pct": round(vwap_deviation_pct, 2),
            "lower_wick_pct": round(lower_wick_pct, 1),
            "upper_wick_pct": round(upper_wick_pct, 1),
            "regime": regime,
            "regime_desc": regime_desc,
            "absorption": absorption,
            "absorption_desc": absorption_desc,
            "cascade_risk": cascade_diag["cascade_phase"],
            "branching_ratio_est": cascade_diag["branching_ratio_est"],
            "cascade_warning": cascade_diag["warning"]
        }
    except Exception as e:
        return None

def detect_liquidation_cascade(symbol, oi_z_score, oi_change_pct, p_change_pct, oib_ratio):
    """
    Detecta cascadas de liquidación basadas en el modelo Galton-Watson del NotebookLM:
    - Subcriticidad basal: lambda_hat ~= 0.03
    - Pre-onset: lambda_hat ~= 0.097
    - Pico de nucleación: lambda_hat ~= 0.195
    - Características empíricas: purga rápida de OI (>= 10% o Z_OI <= -2.5), desequilibrio agresivo OIB <= -0.50.
    """
    is_oi_flush = (oi_change_pct <= -10.0 or oi_z_score <= -2.5)
    is_price_dump = (p_change_pct <= -2.0)
    is_aggressive_sell = (oib_ratio <= -0.50)

    if is_oi_flush and is_price_dump and is_aggressive_sell:
        return {
            "is_cascade": True,
            "cascade_phase": "NUCLEATION_PEAK",
            "branching_ratio_est": 0.195,
            "warning": "🚨 ALERTA CASCADA DE LIQUIDACIÓN ACTIVA: Purga masiva de OI y precio. Prohibido abrir Longs; expandir stops vía ATR*."
        }
    elif is_oi_flush or (oi_z_score <= -2.0 and p_change_pct <= -1.5):
        return {
            "is_cascade": False,
            "cascade_phase": "PRE_ONSET",
            "branching_ratio_est": 0.097,
            "warning": "⚠️ RIESGO ELEVADO DE CASCADA: Desapalancamiento forzado acelerándose."
        }
    return {
        "is_cascade": False,
        "cascade_phase": "BASELINE",
        "branching_ratio_est": 0.031,
        "warning": None
    }

def calculate_dynamic_atr_star(base_atr, relative_spread=0.0005, spread_median=0.0003, basis_pct=0.0, is_cascade=False, high_correlation=False):
    """
    Fórmula de ATR Dinámico Ajustado (NotebookLM - Arbitraje de Tasas y Modelado de Volatilidad):
    ATR*_t = ATR_t * (1 + gamma_1 * (Spread_t / Spread_median - 1) + gamma_2 * |F_t - S_t| / S_t + gamma_3 * I_{rho > 0.80})
    Evita la expulsión prematura por ruido microestructural y expansión de spread en eventos de liquidación.
    """
    gamma1 = 0.25
    gamma2 = 0.50
    gamma3 = 0.35

    spread_factor = max(1.0, relative_spread / spread_median) if spread_median > 0 else 1.0
    basis_factor = abs(basis_pct) / 100.0
    cascade_factor = 1.0 if (is_cascade or high_correlation) else 0.0

    multiplier = 1.0 + (gamma1 * (spread_factor - 1.0)) + (gamma2 * basis_factor) + (gamma3 * cascade_factor)
    return round(base_atr * multiplier, 6)

def get_live_aggtrades_tape(symbol, limit=300):
    """
    Analiza la cinta de ejecuciones en tiempo real con umbrales estadísticos:
    Define 'Block Trade' (Ballena) mediante el percentil 95 de volumen nocional de la muestra.
    """
    try:
        url = f"{BASE_FAPI}/fapi/v1/aggTrades?symbol={symbol}&limit={limit}"
        trades = fetch_json(url)
        if not trades or len(trades) < 20:
            return None

        notionals = np.array([float(t["q"]) * float(t["p"]) for t in trades])
        # Umbral dinámico: Percentil 95 con piso de $25,000 para activos mayores
        whale_threshold = max(25000.0, float(np.percentile(notionals, 95)))

        buy_qty = 0.0
        sell_qty = 0.0
        whale_buys = 0
        whale_sells = 0

        for t in trades:
            q = float(t["q"])
            p = float(t["p"])
            notional = q * p
            is_buyer_maker = t["m"] # True = aggressive sell, False = aggressive buy

            if is_buyer_maker:
                sell_qty += q
                if notional >= whale_threshold:
                    whale_sells += 1
            else:
                buy_qty += q
                if notional >= whale_threshold:
                    whale_buys += 1

        total_qty = buy_qty + sell_qty if (buy_qty + sell_qty) > 0 else 1.0
        live_ratio = buy_qty / sell_qty if sell_qty > 0 else 2.0
        imbalance_pct = ((buy_qty - sell_qty) / total_qty) * 100

        return {
            "symbol": symbol,
            "trades_analyzed": len(trades),
            "whale_threshold_usd": round(whale_threshold, 0),
            "live_taker_ratio": round(live_ratio, 3),
            "imbalance_pct": round(imbalance_pct, 1),
            "whale_buys": whale_buys,
            "whale_sells": whale_sells,
            "live_bias": "BULLISH_PRESSURE" if imbalance_pct > 15 else ("BEARISH_PRESSURE" if imbalance_pct < -15 else "BALANCED")
        }
    except Exception:
        return None

if __name__ == "__main__":
    test_syms = ["BTCUSDT", "SOLUSDT", "ETHUSDT", "1000BONKUSDT"]
    print("🔬 ANÁLISIS DE MICROESTRUCTURA & ORDER FLOW (BINANCE FUTURES) 🔬\n")
    for s in test_syms:
        m = get_symbol_microstructure(s)
        t = get_live_aggtrades_tape(s)
        if m:
            print(f"• {m['symbol']}:")
            print(f"  Régimen: {m['regime']} -> {m['regime_desc']}")
            print(f"  Absorción: {m['absorption']} -> {m['absorption_desc']}")
            print(f"  Taker Buy/Sell: {m['taker_ratio']} | CVD 30-velas: {m['cvd_window_net']:+,.0f}")
            print(f"  OI Z-Score: {m['oi_z_score']:+.2f}σ (ΔOI: {m['oi_change_pct']:+.2f}%) | Funding 8h: {m['funding_rate_pct']:.4f}%")
            if t:
                print(f"  Tape (p95 Ballena: ${t['whale_threshold_usd']:,.0f}): Presión {t['live_bias']} (Imbalance: {t['imbalance_pct']:+.1f}% | Ballenas: +{t['whale_buys']}/-{t['whale_sells']})")
            print()
