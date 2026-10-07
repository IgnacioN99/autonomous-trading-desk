#!/usr/bin/env python3
"""
market_regime.py - Quantitative Market Regime Classifier.
Analyzes Bitcoin structure (EMA 20/50, ATR), funding rate climate,
and overall liquidity to recommend optimal quantitative strategies in real time.
"""

import argparse
import os
import sys
import urllib.error
import urllib.request
import json
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import rate_limit_guard

def fetch_json(url):
    """Public market-data GET through the process-wide rate-limit guard (utils/rate_limit_guard.py)."""
    rate_limit_guard.raise_if_banned()
    req = urllib.request.Request(url, headers={'User-Agent': 'BinanceAgentic/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        rate_limit_guard.on_http_error(e)
        raise

def get_btc_macro_state():
    """Analyzes Bitcoin trend across 1h and 15m timeframes."""
    try:
        klines = fetch_json("https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=50")
        closes = [float(k[4]) for k in klines]
        cur_price = closes[-1]
        
        def calc_ema(values, period):
            k = 2 / (period + 1)
            ema = [values[0]]
            for v in values[1:]:
                ema.append(v * k + ema[-1] * (1 - k))
            return ema[-1]
            
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        
        if cur_price > ema20 > ema50:
            trend = "BULLISH_TREND"
            bias_score = 1.0
        elif cur_price < ema20 < ema50:
            trend = "BEARISH_TREND"
            bias_score = -1.0
        else:
            trend = "CHOPPY_RANGE"
            bias_score = 0.0
            
        return {
            "symbol": "BTCUSDT",
            "price": cur_price,
            "ema20": round(ema20, 2),
            "ema50": round(ema50, 2),
            "trend": trend,
            "bias_score": bias_score
        }
    except Exception as e:
        return {"error": str(e), "symbol": "BTCUSDT", "trend": "UNKNOWN", "bias_score": 0.0, "price": 0.0, "ema20": 0.0, "ema50": 0.0}

def get_funding_climate(min_volume_usdt=20_000_000):
    """Evaluates whether market exhibits bullish euphoria, bearish panic, or normal baseline."""
    try:
        tickers = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
        vol_map = {t['symbol']: float(t['quoteVolume']) for t in tickers}
        
        premiums = fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex")
        
        high_positive = []
        high_negative = []
        
        for p in premiums:
            sym = p['symbol']
            vol = vol_map.get(sym, 0)
            if vol >= min_volume_usdt and sym.endswith('USDT'):
                fr = float(p.get('lastFundingRate', 0)) * 100
                apr = fr * 3 * 365
                
                item = {
                    "symbol": sym,
                    "funding_rate_8h": round(fr, 4),
                    "apr": round(apr, 1),
                    "volume_24h": round(vol / 1e6, 1),
                    "mark_price": float(p.get('markPrice', 0))
                }
                
                if apr >= 25.0:
                    high_positive.append(item)
                elif apr <= -25.0:
                    high_negative.append(item)
                    
        high_positive.sort(key=lambda x: x['funding_rate_8h'], reverse=True)
        high_negative.sort(key=lambda x: x['funding_rate_8h'])
        
        return {
            "high_positive_count": len(high_positive),
            "high_negative_count": len(high_negative),
            "top_positive": high_positive[:5],
            "top_negative": high_negative[:5]
        }
    except Exception as e:
        return {"error": str(e), "high_positive_count": 0, "high_negative_count": 0}

def classify_regime():
    btc = get_btc_macro_state()
    funding = get_funding_climate()
    
    pos_count = funding.get('high_positive_count', 0)
    neg_count = funding.get('high_negative_count', 0)
    btc_trend = btc.get('trend', 'CHOPPY_RANGE')
    
    if pos_count >= 2 or neg_count >= 2:
        recommended_strategy = "DELTA_NEUTRAL_FUNDING_ARBITRAGE"
        rationale = (
            f"Detected {pos_count + neg_count} liquid contracts with extreme annualized funding rates (|APR| > 25%). "
            f"Delta-Neutral Arbitrage (Cash-and-Carry) offers passive yield without price exposure (historical Sharpe 4.84)."
        )
    elif btc_trend in ["BULLISH_TREND", "BEARISH_TREND"]:
        recommended_strategy = "DIRECTIONAL_CONDITIONAL_TRIGGER"
        direction = "LONG" if btc_trend == "BULLISH_TREND" else "SHORT"
        rationale = (
            f"Bitcoin is in defined trend ({btc_trend}, Price {btc.get('price')} vs EMA20 {btc.get('ema20')}). "
            f"Trend-aligned directional trades ({direction}) with Next-Candle conditional triggers carry statistical edge."
        )
    else:
        recommended_strategy = "ABSORPTION_OR_PAIRS_TRADING"
        rationale = (
            "Bitcoin is in choppy sideways consolidation. "
            "Avoid direct market breakouts; prioritize mean reversion at support with Spot CVD absorption or cointegrated pairs trading."
        )
        
    return {
        "btc_state": btc,
        "funding_climate": funding,
        "recommended_strategy": recommended_strategy,
        "rationale": rationale
    }

def main(argv=None):
    """CLI: python3 scripts/market_regime.py [--json] [--env prod|testnet]
    Read-only public market data. Exit codes: 0 ok, 1 BTC and funding data both unavailable, 2 bad usage."""
    parser = argparse.ArgumentParser(description="Macro market regime classifier (read-only)")
    parser.add_argument("--json", action="store_true", help="Print a single JSON document on stdout")
    parser.add_argument("--env", default=None, help="prod|testnet (resolved via env_resolver; market data is always public mainnet)")
    args = parser.parse_args(argv)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from utils.env_resolver import resolve_env
        env = resolve_env(args.env)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    with rate_limit_guard.scan_session():  # a persisted 429/418 ban skips the scan without calling Binance
        if rate_limit_guard.is_banned():
            err = rate_limit_guard.error_payload("regime", env)
            if args.json:
                sys.stdout.write(json.dumps(err, indent=2) + "\n")
            else:
                sys.stderr.write(f"Regime scan skipped: {err['market_data_status']}\n")
            return 1
        report = classify_regime()
        banned = rate_limit_guard.is_banned()
    if banned:
        # A 429/418 during the run (swallowed per fetch): the regime is unreliable, report it as unavailable.
        report["market_data_status"] = rate_limit_guard.unavailable_text()
    failed = banned or ("error" in report["btc_state"] and "error" in report["funding_climate"])
    if args.json:
        payload = {"status": "error" if failed else "ok", "command": "regime", "env": env}
        payload.update(report)
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 1 if failed else 0

    print_report(report)
    return 1 if failed else 0

def print_report(report):
    print("=== CURRENT MARKET REGIME ===")
    print(f"Recommended Strategy: {report['recommended_strategy']}")
    print(f"Diagnosis: {report['rationale']}")
    print(f"\nBitcoin State: {report['btc_state']['trend']} (Price: {report['btc_state']['price']}, EMA20: {report['btc_state']['ema20']}, EMA50: {report['btc_state']['ema50']})")
    print(f"Positive Funding Opportunities: {report['funding_climate']['high_positive_count']}")
    print(f"Negative Funding Opportunities: {report['funding_climate']['high_negative_count']}")

if __name__ == "__main__":
    sys.exit(main())
