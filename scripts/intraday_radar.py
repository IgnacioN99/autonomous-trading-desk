#!/usr/bin/env python3
"""
intraday_radar.py - High-Confluence Intraday Scanner for Binance Futures.
Specialized in 15m and 5m timeframes for bounded capital day trading and rotation.
"""

import urllib.request
import json
import time
import math
import sys
import argparse

def get_top_crypto_pairs(limit=35):
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"Error querying 24hr tickers: {e}", file=sys.stderr)
        return []

    # Query exchangeInfo to strictly filter pure crypto contracts (underlyingType == 'COIN')
    info_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    info_req = urllib.request.Request(info_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(info_req, timeout=10) as resp:
            info_data = json.loads(resp.read().decode())
        valid_crypto_symbols = set(
            s["symbol"] for s in info_data.get("symbols", [])
            if s.get("underlyingType") == "COIN"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        )
    except Exception:
        valid_crypto_symbols = set()

    valid = []
    for item in data:
        sym = item["symbol"]
        if valid_crypto_symbols:
            if sym in valid_crypto_symbols:
                valid.append(item)
        else:
            if sym.endswith("USDT") and not sym.startswith(("USDC", "XAU", "XAG", "EUR")):
                valid.append(item)

    # Sort by descending USDT quote volume
    valid.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
    return [x["symbol"] for x in valid[:limit]]

def get_klines(symbol, interval="15m", limit=50):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            return data
    except Exception:
        return []

def calculate_ema(series, period):
    if len(series) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = [sum(series[:period]) / period]
    for price in series[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

def calculate_rsi(closes, period=14):
    if len(closes) <= period:
        return 50.0
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(delta))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = abs(min(delta, 0.0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return (max(highs) - min(lows)) / 2 if highs and lows else 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def analyze_symbol(symbol, interval="15m"):
    klines = get_klines(symbol, interval=interval, limit=55)
    if len(klines) < 30:
        return None

    # Parse klines: [time, open, high, low, close, volume, ...]
    opens = [float(k[1]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    current_price = closes[-1]
    candle_open = opens[-1]
    candle_high = highs[-1]
    candle_low = lows[-1]
    candle_vol = volumes[-1]

    # Candle range and wicks
    total_range = candle_high - candle_low
    if total_range <= 0:
        return None

    body_top = max(candle_open, current_price)
    body_bottom = min(candle_open, current_price)
    lower_wick = body_bottom - candle_low
    upper_wick = candle_high - body_top

    lower_wick_ratio = (lower_wick / total_range) * 100
    upper_wick_ratio = (upper_wick / total_range) * 100

    # RSI
    rsi_15m = calculate_rsi(closes, period=14)

    # EMA 20
    emas = calculate_ema(closes, period=20)
    ema20 = emas[-1] if emas else current_price

    # Relative volume vs 20-candle average
    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else candle_vol
    vol_ratio = (candle_vol / avg_vol) if avg_vol > 0 else 1.0

    # Distance to EMA 20 in %
    dist_to_ema20_pct = ((ema20 - current_price) / current_price) * 100

    # Recent high/low across last 50 15m candles (~12.5 hours)
    recent_high = max(highs)
    recent_low = min(lows)

    score_long = 0
    score_short = 0
    long_reasons = []
    short_reasons = []

    # Criterion 1: Extreme RSI (Mean Reversion)
    if rsi_15m < 28:
        score_long += 35
        long_reasons.append(f"RSI 15m extreme oversold ({rsi_15m:.1f})")
    elif rsi_15m < 35:
        score_long += 20
        long_reasons.append(f"RSI 15m oversold ({rsi_15m:.1f})")
    elif rsi_15m > 72:
        score_short += 35
        short_reasons.append(f"RSI 15m extreme overbought ({rsi_15m:.1f})")
    elif rsi_15m > 65:
        score_short += 20
        short_reasons.append(f"RSI 15m overbought ({rsi_15m:.1f})")

    # Criterion 2: Institutional absorption wicks
    if lower_wick_ratio >= 40:
        score_long += 30
        long_reasons.append(f"Buyer absorption wick ({lower_wick_ratio:.0f}% of candle)")
    elif lower_wick_ratio >= 25:
        score_long += 15
        long_reasons.append(f"Support rejection ({lower_wick_ratio:.0f}% lower wick)")

    if upper_wick_ratio >= 40:
        score_short += 30
        short_reasons.append(f"Seller absorption wick ({upper_wick_ratio:.0f}% of candle)")
    elif upper_wick_ratio >= 25:
        score_short += 15
        short_reasons.append(f"Resistance rejection ({upper_wick_ratio:.0f}% upper wick)")

    # Criterion 3: Volume Climax
    if vol_ratio >= 1.8:
        score_long += 20 if score_long > score_short else 0
        score_short += 20 if score_short > score_long else 0
        reason_txt = f"Volume climax {vol_ratio:.1f}x average"
        if score_long >= score_short:
            long_reasons.append(reason_txt)
        else:
            short_reasons.append(reason_txt)
    elif vol_ratio >= 1.3:
        score_long += 10 if score_long > score_short else 0
        score_short += 10 if score_short > score_long else 0

    # Criterion 4: Sweep of recent session highs/lows
    if current_price <= recent_low * 1.008:
        score_long += 15
        long_reasons.append("Session low liquidity sweep zone")
    if current_price >= recent_high * 0.992:
        score_short += 15
        short_reasons.append("Session high liquidity sweep zone")

    atr = calculate_atr(highs, lows, closes, period=14)

    # Determine direction
    if score_long >= 45 and score_long > score_short:
        direction = "LONG"
        confluence_score = min(score_long, 98)
        reasons = long_reasons
        entry = current_price
        trigger_entry = candle_high * 1.0005 # Trigger: break above absorption candle high
        sl = candle_low - (1.3 * atr) # ATR anti-sweep buffer
        risk_pct = ((entry - sl) / entry) * 100
        # Ensure minimum 1.0% technical buffer against micro-noise
        if risk_pct < 1.0:
            sl = entry * 0.988
            risk_pct = 1.2

        tp1 = max(ema20, entry * (1 + risk_pct * 1.8 / 100)) # Minimum 1.8R to EMA 20
        tp2 = entry * (1 + risk_pct * 4.0 / 100) # 4.0R structural
        rr = (tp2 - entry) / (entry - sl) if (entry - sl) > 0 else 4.0
    elif score_short >= 45 and score_short > score_long:
        direction = "SHORT"
        confluence_score = min(score_short, 98)
        reasons = short_reasons
        entry = current_price
        trigger_entry = candle_low * 0.9995 # Trigger: break below absorption candle low
        sl = candle_high + (1.3 * atr) # ATR anti-sweep buffer
        risk_pct = ((sl - entry) / entry) * 100
        if risk_pct < 1.0:
            sl = entry * 1.012
            risk_pct = 1.2
        tp1 = min(ema20, entry * (1 - risk_pct * 1.8 / 100))
        tp2 = entry * (1 - risk_pct * 4.0 / 100)
        rr = (entry - tp2) / (sl - entry) if (sl - entry) > 0 else 4.0
    else:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "score": confluence_score,
        "price": current_price,
        "trigger": trigger_entry,
        "rsi_15m": round(rsi_15m, 1),
        "vol_ratio": round(vol_ratio, 1),
        "ema20": ema20,
        "atr": atr,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": round(rr, 2),
        "risk_pct": round(risk_pct, 2),
        "reasons": reasons
    }

def scan_market(top_n=5, interval="15m"):
    symbols = get_top_crypto_pairs(limit=65)
    candidates = []
    for s in symbols:
        try:
            res = analyze_symbol(s, interval=interval)
            if res and res["rr"] >= 1.8:
                candidates.append(res)
        except Exception:
            continue
        time.sleep(0.04) # gentle rate limit

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Futures Intraday Scanner")
    parser.add_argument("--interval", type=str, default="15m", choices=["5m", "15m", "1h"], help="Scanning timeframe")
    parser.add_argument("--top", type=int, default=5, help="Number of opportunities to return")
    parser.add_argument("--format", type=str, default="table", choices=["table", "json"], help="Output format")
    args = parser.parse_args()

    results = scan_market(top_n=args.top, interval=args.interval)

    if args.format == "json":
        print(json.dumps(results, indent=2))
    else:
        print(f"\n⚡ BINANCE FUTURES INTRADAY RADAR (Timeframe: {args.interval}) ⚡")
        print("=" * 80)
        if not results:
            print("No opportunities with sufficient confluence found at this moment.")
        for i, item in enumerate(results, 1):
            tier = "Tier S (🔥 Maximum)" if item["score"] >= 75 else "Tier A (Strong)"
            roe_est = round(item["risk_pct"] * item["rr"] * 3, 1) # at 3x
            trigger_str = f"{item['trigger']:.4f}" if item.get("trigger") else f"{item['entry']:.4f}"
            print(f"#{i} | {item['symbol']} - {item['direction']} | Confluence: {item['score']}% ({tier})")
            print(f"   • Next-Candle Trigger: {trigger_str} | Market Price: {item['price']}")
            print(f"   • Stop Loss (1.3x ATR Buffer): {item['sl']:.4f} (-{item['risk_pct']}%)")
            print(f"   • TP1 (EMA 20 / BE): {item['tp1']:.4f} | TP2 (Structural): {item['tp2']:.4f}")
            print(f"   • R:R Ratio: {item['rr']}:1 | Estimated ROE (3x): +{roe_est}%")
            print(f"   • Confluence Factors: {', '.join(item['reasons'])}")
            print("-" * 80)
