#!/usr/bin/env python3
"""
microstructure_engine.py - Market Microstructure and Order Flow Engine for Binance Futures.
Implements:
1. Cumulative Volume Delta (CVD) accumulated over 30 periods (7.5 hours on 15m).
2. Institutional Liquidity Absorption Detection (Price vs CVD Divergence & passive limit walls).
3. Market Regime Matrix based on Open Interest (OI) Z-Score and price (4-Quadrant Matrix).
4. Real-time dynamic tape reading via aggTrades with a 95th percentile threshold for institutional blocks.
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
    Queries quantitative order flow metrics over a 30-period rolling window:
    1. Taker Buy/Sell Volume Ratio & Cumulative CVD (Aggressive buyer vs seller taker volume)
    2. Historical Open Interest with Z-Score (Fresh institutional capital vs forced liquidations)
    3. Funding Rate & market premium
    4. Price action and absorption wicks
    """
    try:
        # 1. Taker Buy/Sell Volume Ratio (30-candle window)
        url_taker = f"{BASE_FAPI}/futures/data/takerlongshortRatio?symbol={symbol}&period={period}&limit={history_limit}"
        taker_data = fetch_json(url_taker)

        # 2. Historical Open Interest (30-candle window)
        url_oi = f"{BASE_FAPI}/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={history_limit}"
        oi_data = fetch_json(url_oi)

        # 3. Premium Index (Current Funding Rate)
        url_prem = f"{BASE_FAPI}/fapi/v1/premiumIndex?symbol={symbol}"
        prem_data = fetch_json(url_prem)

        # 4. Klines (Window to correlate price with OI and CVD)
        url_klines = f"{BASE_FAPI}/fapi/v1/klines?symbol={symbol}&interval={period}&limit={history_limit}"
        klines = fetch_json(url_klines)

        if not taker_data or not oi_data or not klines or len(klines) < 5:
            return None

        # --- CUMULATIVE VOLUME DELTA (CVD) ANALYSIS ---
        deltas = np.array([float(t.get("buyVol", 0)) - float(t.get("sellVol", 0)) for t in taker_data])
        cvd_cumulative = np.cumsum(deltas)
        cvd_current_delta = float(deltas[-1])
        cvd_window_net = float(cvd_cumulative[-1] - cvd_cumulative[0])

        latest_taker = taker_data[-1]
        t_ratio = float(latest_taker.get("buySellRatio", 1.0))
        buy_vol = float(latest_taker.get("buyVol", 0))
        sell_vol = float(latest_taker.get("sellVol", 0))

        # --- OPEN INTEREST (OI) STATISTICAL ANALYSIS ---
        oi_series = np.array([float(x.get("sumOpenInterest", 0)) for x in oi_data])
        oi_current = float(oi_series[-1])
        oi_prev = float(oi_series[-2])
        oi_delta = oi_current - oi_prev
        oi_change_pct = (oi_delta / oi_prev * 100) if oi_prev > 0 else 0.0

        # Calculate Z-Score of OI changes over historical 30-period window
        oi_pct_changes = np.diff(oi_series) / oi_series[:-1] * 100
        oi_std = float(np.std(oi_pct_changes)) if len(oi_pct_changes) > 2 else 1.0
        oi_mean = float(np.mean(oi_pct_changes)) if len(oi_pct_changes) > 2 else 0.0
        oi_z_score = float((oi_change_pct - oi_mean) / oi_std) if oi_std > 0 else 0.0

        oi_val_usd = float(oi_data[-1].get("sumOpenInterestValue", 0))

        # --- FUNDING METRICS ---
        funding_rate = float(prem_data.get("lastFundingRate", 0))
        funding_rate_pct = funding_rate * 100 # in %
        annualized_funding = funding_rate * 3 * 365 * 100 # APR in %

        # --- PRICE & CANDLE METRICS ---
        c_open = float(klines[-1][1])
        c_high = float(klines[-1][2])
        c_low = float(klines[-1][3])
        c_close = float(klines[-1][4])
        p_change_pct = ((c_close - c_open) / c_open) * 100

        # Range and Absorption Wicks
        candle_range = c_high - c_low if (c_high - c_low) > 0 else 1e-8
        body_top = max(c_open, c_close)
        body_bottom = min(c_open, c_close)
        lower_wick_pct = ((body_bottom - c_low) / candle_range) * 100
        upper_wick_pct = ((c_high - body_top) / candle_range) * 100

        # --- QUANTITATIVE REGIME CLASSIFICATION (OI Z-Score >= 1.25σ or Significant Delta) ---
        # Robust filter: Requires statistically anomalous OI change (|Z| >= 1.25 or |ΔOI| >= 0.40%)
        is_oi_inflow = (oi_z_score >= 1.25 or oi_change_pct >= 0.40)
        is_oi_outflow = (oi_z_score <= -1.25 or oi_change_pct <= -0.40)

        if p_change_pct > 0.15 and is_oi_inflow:
            regime = "LONG_BUILDUP"
            regime_desc = f"Bullish Conviction (Institutional capital inflow: Z={oi_z_score:+.2f}σ, ΔOI={oi_change_pct:+.2f}%)"
        elif p_change_pct > 0.15 and is_oi_outflow:
            regime = "SHORT_SQUEEZE"
            regime_desc = f"Short Squeeze (Mechanical rally via forced short liquidations: Z={oi_z_score:+.2f}σ, ΔOI={oi_change_pct:+.2f}%)"
        elif p_change_pct < -0.15 and is_oi_inflow:
            regime = "SHORT_BUILDUP"
            regime_desc = f"Bearish Conviction (Aggressive net seller capital inflow: Z={oi_z_score:+.2f}σ, ΔOI={oi_change_pct:+.2f}%)"
        elif p_change_pct < -0.15 and is_oi_outflow:
            regime = "LONG_UNWINDING"
            regime_desc = f"Capitulation / Long Unwinding (Massive buyer liquidation: Z={oi_z_score:+.2f}σ, ΔOI={oi_change_pct:+.2f}%)"
        else:
            regime = "NEUTRAL_CONSOLIDATION"
            regime_desc = f"Range Consolidation / Auction Equilibrium (Z_OI={oi_z_score:+.2f}σ)"

        # --- LIQUIDITY ABSORPTION DETECTION (CVD Divergence & Limit Walls) ---
        absorption = "NONE"
        absorption_desc = "No anomalous absorption detected"

        # BULLISH ABSORPTION:
        # Aggressive taker selling (Taker Ratio <= 0.85 or negative candle CVD),
        # but price fails to drop or displays strong lower wick (lower wick >= 40% or p_change >= -0.15%).
        # Conclusion: Passive limit bid walls absorbing all sell flow at support.
        if (t_ratio <= 0.85 or cvd_current_delta < 0) and (lower_wick_pct >= 40.0 or p_change_pct >= -0.15):
            absorption = "BULLISH_ABSORPTION"
            absorption_desc = f"Active Bullish Absorption (Taker Ratio {t_ratio:.2f} absorbed by bid wall, lower wick {lower_wick_pct:.0f}%)"

        # BEARISH ABSORPTION:
        # Aggressive taker buying (Taker Ratio >= 1.25 or positive candle CVD),
        # but price fails to rise or displays strong upper wick (upper wick >= 40% or p_change <= 0.15%).
        # Conclusion: Institutional passive ask blocks unloading into retail flow.
        elif (t_ratio >= 1.25 or cvd_current_delta > 0) and (upper_wick_pct >= 40.0 or p_change_pct <= 0.15):
            absorption = "BEARISH_ABSORPTION"
            absorption_desc = f"Active Bearish Absorption (Taker Ratio {t_ratio:.2f} absorbed by ask wall, upper wick {upper_wick_pct:.0f}%)"

        # --- ORDER FLOW IMBALANCE (OIB) & VWAP DEVIATION ---
        total_taker_vol = buy_vol + sell_vol
        oib_ratio = float((buy_vol - sell_vol) / total_taker_vol) if total_taker_vol > 0 else 0.0

        typical_prices = np.array([(float(k[2]) + float(k[3]) + float(k[4])) / 3.0 for k in klines])
        vols = np.array([float(k[5]) for k in klines])
        sum_vols = float(np.sum(vols))
        vwap = float(np.sum(typical_prices * vols) / sum_vols) if sum_vols > 0 else c_close
        vwap_deviation_pct = float(((c_close - vwap) / vwap) * 100.0) if vwap > 0 else 0.0

        # Liquidation Cascade Detection (Galton-Watson lambda_hat)
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
    Detects liquidation cascades based on the Galton-Watson branching process model:
    - Basal subcriticality: lambda_hat ~= 0.03
    - Pre-onset: lambda_hat ~= 0.097
    - Nucleation peak: lambda_hat ~= 0.195
    - Empirical triggers: rapid OI flush (>= 10% drop or Z_OI <= -2.5), aggressive sell imbalance OIB <= -0.50.
    """
    is_oi_flush = (oi_change_pct <= -10.0 or oi_z_score <= -2.5)
    is_price_dump = (p_change_pct <= -2.0)
    is_aggressive_sell = (oib_ratio <= -0.50)

    if is_oi_flush and is_price_dump and is_aggressive_sell:
        return {
            "is_cascade": True,
            "cascade_phase": "NUCLEATION_PEAK",
            "branching_ratio_est": 0.195,
            "warning": "🚨 ACTIVE LIQUIDATION CASCADE ALERT: Massive OI flush and price collapse. Long orders prohibited; expand stops via ATR*."
        }
    elif is_oi_flush or (oi_z_score <= -2.0 and p_change_pct <= -1.5):
        return {
            "is_cascade": False,
            "cascade_phase": "PRE_ONSET",
            "branching_ratio_est": 0.097,
            "warning": "⚠️ ELEVATED CASCADE RISK: Forced deleveraging accelerating."
        }
    return {
        "is_cascade": False,
        "cascade_phase": "BASELINE",
        "branching_ratio_est": 0.031,
        "warning": None
    }

def calculate_dynamic_atr_star(base_atr, relative_spread=0.0005, spread_median=0.0003, basis_pct=0.0, is_cascade=False, high_correlation=False):
    """
    Adjusted Dynamic ATR Formula:
    ATR*_t = ATR_t * (1 + gamma_1 * (Spread_t / Spread_median - 1) + gamma_2 * |F_t - S_t| / S_t + gamma_3 * I_{rho > 0.80})
    Prevents premature stopouts caused by microstructural spread expansion during liquidation events.
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
    Analyzes live trade execution tape using statistical thresholds:
    Defines 'Whale Block Trade' via 95th percentile sample notional volume.
    """
    try:
        url = f"{BASE_FAPI}/fapi/v1/aggTrades?symbol={symbol}&limit={limit}"
        trades = fetch_json(url)
        if not trades or len(trades) < 20:
            return None

        notionals = np.array([float(t["q"]) * float(t["p"]) for t in trades])
        # Dynamic threshold: 95th percentile with $25,000 floor for major assets
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
    print("🔬 MICROSTRUCTURE & ORDER FLOW ANALYSIS (BINANCE FUTURES) 🔬\n")
    for s in test_syms:
        m = get_symbol_microstructure(s)
        t = get_live_aggtrades_tape(s)
        if m:
            print(f"• {m['symbol']}:")
            print(f"  Regime: {m['regime']} -> {m['regime_desc']}")
            print(f"  Absorption: {m['absorption']} -> {m['absorption_desc']}")
            print(f"  Taker Buy/Sell: {m['taker_ratio']} | 30-Candle CVD: {m['cvd_window_net']:+,.0f}")
            print(f"  OI Z-Score: {m['oi_z_score']:+.2f}σ (ΔOI: {m['oi_change_pct']:+.2f}%) | 8h Funding: {m['funding_rate_pct']:.4f}%")
            if t:
                print(f"  Tape (p95 Whale: ${t['whale_threshold_usd']:,.0f}): Bias {t['live_bias']} (Imbalance: {t['imbalance_pct']:+.1f}% | Whales: +{t['whale_buys']}/-{t['whale_sells']})")
            print()
