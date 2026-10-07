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
import urllib.error
import urllib.request
import numpy as np

from utils import rate_limit_guard  # scripts/ is on sys.path (run as a script or imported by a desk script)

BASE_FAPI = "https://fapi.binance.com"

def fetch_json(url, timeout=6):
    """Public market-data GET. With the process-wide rate-limit guard enabled (scan entry points only), an active
    ban raises RateLimitedError before the request and HTTP 429/418 trip it (utils/rate_limit_guard.py)."""
    rate_limit_guard.raise_if_banned()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        rate_limit_guard.on_http_error(e)
        raise

WICK_SUM_EPSILON = 1e-6

def _wick_pcts(o, h, l, c, candle_range):
    body_top = max(o, c)
    body_bottom = min(o, c)
    return ((body_bottom - l) / candle_range) * 100, ((h - body_top) / candle_range) * 100

def candle_wick_pcts(kline):
    """(lower_wick_pct, upper_wick_pct) of ONE Binance kline [openTime, open, high, low, close, ...], each as a %
    of that candle's high-low range. Both sides always come from the same candle (issue #20).
    Zero range (high == low) -> (0.0, 0.0). Invariant: 0 <= each side and lower + upper <= 100; malformed data
    that breaks it (open/close outside [low, high]) is not emitted as-is: the body is clamped into [low, high]."""
    o, h, l, c = (float(kline[i]) for i in (1, 2, 3, 4))
    candle_range = h - l
    if candle_range <= 0:
        return 0.0, 0.0
    lower, upper = _wick_pcts(o, h, l, c, candle_range)
    if lower + upper > 100.0 + WICK_SUM_EPSILON or lower < -WICK_SUM_EPSILON or upper < -WICK_SUM_EPSILON:
        o, c = min(max(o, l), h), min(max(c, l), h)
        lower, upper = _wick_pcts(o, h, l, c, candle_range)
    return lower, upper

def select_wick_kline(klines, wick_candle_open_time=None):
    """Kline whose wicks are reported: the one opening at `wick_candle_open_time` (ms) when given, else the last
    CLOSED candle (klines[-2]; klines[-1] is still forming). Returns (kline, mismatch): mismatch is True when an
    open time was requested but no kline matches it (fallback to the last closed candle)."""
    if wick_candle_open_time is not None:
        for k in klines:
            try:
                if int(k[0]) == int(wick_candle_open_time):
                    return k, False
            except (TypeError, ValueError):
                continue
        return klines[-2], True
    return klines[-2], False

def select_taker_index(taker_data, candle_open_time):
    """Index of the takerlongshortRatio row for the candle opening at `candle_open_time` (ms): Binance stamps
    each closed period with its kline open time. None when no row matches (missing/malformed timestamp, lag)."""
    for i in range(len(taker_data) - 1, -1, -1):
        try:
            if int(taker_data[i].get("timestamp")) == int(candle_open_time):
                return i
        except (TypeError, ValueError, AttributeError):
            continue
    return None

def get_symbol_microstructure(symbol, period="15m", history_limit=30, wick_candle_open_time=None):
    """
    Queries quantitative order flow metrics over a 30-period rolling window:
    1. Taker Buy/Sell Volume Ratio & Cumulative CVD (Aggressive buyer vs seller taker volume)
    2. Historical Open Interest with Z-Score (Fresh institutional capital vs forced liquidations)
    3. Funding Rate & market premium
    4. Price action and absorption wicks

    Wicks (`lower_wick_pct`/`upper_wick_pct` and the % in `absorption_desc`) come from ONE candle: the kline
    opening at `wick_candle_open_time` (ms) when given (callers pass the candle they scored, so a candle boundary
    between their fetch and this one does not shift it), else the last closed candle. If the requested candle is
    not in this fetch, the last closed candle is used and `wick_candle_mismatch` is True.

    Taker ratio, buy/sell volume, OIB and `cvd_current_delta` come from the taker row whose `timestamp` equals that
    wick candle's open time (Binance stamps each closed period with its kline open time), so the absorption flag
    and its text describe one candle (issue #83). No matching row -> `taker_candle_matched` False, the latest row
    is reported for information only and `absorption` is "NONE". The absorption flag needs both the taker
    condition and a >= 40% wick on that candle; `price_change_pct` (latest period) feeds only regime and cascade.
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

        # Absorption Wicks: both sides from one candle (the caller's candle, else the last closed one)
        wick_kline, wick_candle_mismatch = select_wick_kline(klines, wick_candle_open_time)
        wick_open_time = int(wick_kline[0])
        lower_wick_pct, upper_wick_pct = candle_wick_pcts(wick_kline)

        # --- CUMULATIVE VOLUME DELTA (CVD) ANALYSIS ---
        deltas = np.array([float(t.get("buyVol", 0)) - float(t.get("sellVol", 0)) for t in taker_data])
        cvd_cumulative = np.cumsum(deltas)
        cvd_window_net = float(cvd_cumulative[-1] - cvd_cumulative[0])

        # Taker row of the wick candle (matched by open time), else the latest row for information only
        taker_idx = select_taker_index(taker_data, wick_open_time)
        taker_candle_matched = taker_idx is not None
        if not taker_candle_matched:
            taker_idx = len(taker_data) - 1
        cvd_current_delta = float(deltas[taker_idx])
        wick_taker = taker_data[taker_idx]
        t_ratio = float(wick_taker.get("buySellRatio", 1.0))
        buy_vol = float(wick_taker.get("buyVol", 0))
        sell_vol = float(wick_taker.get("sellVol", 0))

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

        candle_utc = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(wick_open_time / 1000))

        # Only scored on one candle: the wick candle and its own taker row. Unmatched taker data -> "NONE".
        # BULLISH ABSORPTION:
        # Aggressive taker selling (Taker Ratio <= 0.85 or negative candle CVD) AND a strong lower wick (>= 40%).
        # Conclusion: Passive limit bid walls absorbing all sell flow at support.
        if not taker_candle_matched:
            absorption_desc = "Absorption not evaluated (no taker data for the wick candle)"
        elif (t_ratio <= 0.85 or cvd_current_delta < 0) and lower_wick_pct >= 40.0:
            absorption = "BULLISH_ABSORPTION"
            absorption_desc = (f"Active Bullish Absorption on the {candle_utc} candle (Taker Ratio {t_ratio:.2f}, "
                               f"CVD delta {cvd_current_delta:+,.0f} absorbed by bid wall, lower wick {lower_wick_pct:.0f}%)")

        # BEARISH ABSORPTION:
        # Aggressive taker buying (Taker Ratio >= 1.25 or positive candle CVD) AND a strong upper wick (>= 40%).
        # Conclusion: Institutional passive ask blocks unloading into retail flow.
        elif (t_ratio >= 1.25 or cvd_current_delta > 0) and upper_wick_pct >= 40.0:
            absorption = "BEARISH_ABSORPTION"
            absorption_desc = (f"Active Bearish Absorption on the {candle_utc} candle (Taker Ratio {t_ratio:.2f}, "
                               f"CVD delta {cvd_current_delta:+,.0f} absorbed by ask wall, upper wick {upper_wick_pct:.0f}%)")

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
            "wick_candle_open_time": wick_open_time,
            "wick_candle_mismatch": wick_candle_mismatch,
            "taker_candle_matched": taker_candle_matched,
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

DEFAULT_SYMBOLS = ["BTCUSDT", "SOLUSDT", "ETHUSDT", "1000BONKUSDT"]

def main(argv=None):
    """CLI: python3 scripts/microstructure_engine.py [--symbols A,B] [--interval 15m] [--json] [--env prod|testnet]
    Read-only public market data. Exit codes: 0 ok, 1 no data for any symbol, 2 bad usage."""
    import argparse
    import contextlib
    parser = argparse.ArgumentParser(description="Order flow / microstructure snapshot (read-only)")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated futures symbols")
    parser.add_argument("--interval", default="15m", choices=["5m", "15m", "1h"], help="Period for taker/OI/klines windows")
    parser.add_argument("--json", action="store_true", help="Print a single JSON document on stdout")
    parser.add_argument("--env", default=None, help="prod|testnet (resolved via env_resolver; market data is always public mainnet)")
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        sys.stderr.write("error: --symbols must list at least one symbol\n")
        return 2
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from utils.env_resolver import resolve_env
        env = resolve_env(args.env)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    real_stdout = sys.stdout
    results = []
    with contextlib.redirect_stdout(sys.stderr if args.json else real_stdout):
        for s in symbols:
            results.append({"symbol": s, "micro": get_symbol_microstructure(s, period=args.interval),
                            "tape": get_live_aggtrades_tape(s)})
    ok = any(r["micro"] for r in results)

    if args.json:
        payload = {"status": "ok" if ok else "error", "command": "microstructure", "env": env,
                   "interval": args.interval, "symbols": results}
        if not ok:
            payload["error"] = "No microstructure data could be fetched for any symbol."
        real_stdout.write(json.dumps(payload, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o)) + "\n")
        return 0 if ok else 1

    print("🔬 MICROSTRUCTURE & ORDER FLOW ANALYSIS (BINANCE FUTURES) 🔬\n")
    for r in results:
        m, t = r["micro"], r["tape"]
        if m:
            print(f"• {m['symbol']}:")
            print(f"  Regime: {m['regime']} -> {m['regime_desc']}")
            print(f"  Absorption: {m['absorption']} -> {m['absorption_desc']}")
            print(f"  Taker Buy/Sell: {m['taker_ratio']} | 30-Candle CVD: {m['cvd_window_net']:+,.0f}")
            print(f"  OI Z-Score: {m['oi_z_score']:+.2f}σ (ΔOI: {m['oi_change_pct']:+.2f}%) | 8h Funding: {m['funding_rate_pct']:.4f}%")
            if t:
                print(f"  Tape (p95 Whale: ${t['whale_threshold_usd']:,.0f}): Bias {t['live_bias']} (Imbalance: {t['imbalance_pct']:+.1f}% | Whales: +{t['whale_buys']}/-{t['whale_sells']})")
            print()
        else:
            print(f"• {r['symbol']}: no microstructure data\n")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
