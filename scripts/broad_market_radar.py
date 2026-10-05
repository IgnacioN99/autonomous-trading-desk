#!/usr/bin/env python3
"""
broad_market_radar.py - Broad Market and Multi-Conviction Screener.
Concurrently scans top Binance Futures contracts (5m, 15m or 1h), ranking opportunities
by confidence tier: Tier S (Maximum), Tier A+ (High), Tier A (Strong), and Delta hedges.

Read-only: uses public Binance Futures market data and never places orders.

CLI:
    python3 scripts/broad_market_radar.py [--json] [--top N] [--interval 15m|5m|1h]
                                          [--universe N] [--env prod|testnet]
Exit codes: 0 ok, 1 data/API error, 2 bad usage.
"""

import urllib.request
import json
import time
import math
import sys
import os
import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import microstructure_engine as me

SUPPORTED_INTERVALS = ("5m", "15m", "1h")
DEFAULT_INTERVAL = "15m"
DEFAULT_UNIVERSE = 80

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

def fetch_klines(symbol, interval=DEFAULT_INTERVAL, limit=55):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        return json.loads(resp.read().decode())

def tier_code(confidence):
    """Stable machine-readable tier code (the `tier` field is a human label)."""
    if confidence >= 80:
        return "S"
    if confidence >= 65:
        return "A+"
    if confidence >= 55:
        return "A"
    return "B+"

def analyze_single_symbol(symbol, interval=DEFAULT_INTERVAL):
    try:
        klines = fetch_klines(symbol, interval=interval, limit=55)
    except Exception:
        return None

    if not isinstance(klines, list) or len(klines) < 35:
        return None

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

    total_range = candle_high - candle_low
    if total_range <= 0:
        return None

    body_top = max(candle_open, current_price)
    body_bottom = min(candle_open, current_price)
    lower_wick = body_bottom - candle_low
    upper_wick = candle_high - body_top

    lower_wick_ratio = (lower_wick / total_range) * 100
    upper_wick_ratio = (upper_wick / total_range) * 100

    # Previous candle to confirm recent absorption wicks
    prev_open = opens[-2]
    prev_close = closes[-2]
    prev_high = highs[-2]
    prev_low = lows[-2]
    prev_range = prev_high - prev_low
    prev_lower_wick_ratio = 0
    prev_upper_wick_ratio = 0
    if prev_range > 0:
        prev_body_top = max(prev_open, prev_close)
        prev_body_bottom = min(prev_open, prev_close)
        prev_lower_wick_ratio = ((prev_body_bottom - prev_low) / prev_range) * 100
        prev_upper_wick_ratio = ((prev_high - prev_body_top) / prev_range) * 100

    effective_lower_wick = max(lower_wick_ratio, prev_lower_wick_ratio)
    effective_upper_wick = max(upper_wick_ratio, prev_upper_wick_ratio)

    rsi_15m = calculate_rsi(closes, period=14)
    emas = calculate_ema(closes, period=20)
    ema20 = emas[-1] if emas else current_price

    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else candle_vol
    vol_ratio = (candle_vol / avg_vol) if avg_vol > 0 else 1.0

    recent_high = max(highs[-40:])
    recent_low = min(lows[-40:])

    atr = calculate_atr(highs, lows, closes, period=14)

    score_long = 0
    score_short = 0
    long_reasons = []
    short_reasons = []

    # 1. RSI Scoring
    if rsi_15m < 28:
        score_long += 35
        long_reasons.append(f"RSI {interval} extreme oversold ({rsi_15m:.1f})")
    elif rsi_15m < 35:
        score_long += 25
        long_reasons.append(f"RSI {interval} oversold ({rsi_15m:.1f})")
    elif rsi_15m > 74:
        score_short += 35
        short_reasons.append(f"RSI {interval} extreme overbought ({rsi_15m:.1f})")
    elif rsi_15m > 66:
        score_short += 25
        short_reasons.append(f"RSI {interval} overbought ({rsi_15m:.1f})")

    # 2. Institutional Absorption Wicks
    if effective_lower_wick >= 50:
        score_long += 35
        long_reasons.append(f"Massive buyer absorption wick ({effective_lower_wick:.0f}%)")
    elif effective_lower_wick >= 30:
        score_long += 20
        long_reasons.append(f"Support rejection ({effective_lower_wick:.0f}% lower wick)")

    if effective_upper_wick >= 50:
        score_short += 35
        short_reasons.append(f"Massive seller absorption wick ({effective_upper_wick:.0f}%)")
    elif effective_upper_wick >= 30:
        score_short += 20
        short_reasons.append(f"Resistance rejection ({effective_upper_wick:.0f}% upper wick)")

    # 3. Volume Climax
    if vol_ratio >= 1.8:
        pts = 20
        r_txt = f"Volume climax {vol_ratio:.1f}x average"
        if score_long >= score_short:
            score_long += pts
            long_reasons.append(r_txt)
        else:
            score_short += pts
            short_reasons.append(r_txt)
    elif vol_ratio >= 1.3:
        pts = 10
        if score_long >= score_short:
            score_long += pts
        else:
            score_short += pts

    # 4. Liquidity Sweeps
    if current_price <= recent_low * 1.012:
        score_long += 15
        long_reasons.append("Liquidity sweep at local lows")
    if current_price >= recent_high * 0.988:
        score_short += 15
        short_reasons.append("Liquidity sweep at local highs")

    # 5. Elastic Distance to EMA 20 (Mean Reversion)
    dist_to_ema = ((ema20 - current_price) / current_price) * 100
    if dist_to_ema > 1.8:
        score_long += 10
        long_reasons.append(f"Discount vs EMA 20 ({dist_to_ema:+.1f}%)")
    elif dist_to_ema < -1.8:
        score_short += 10
        short_reasons.append(f"Overextension vs EMA 20 ({dist_to_ema:+.1f}%)")

    # Determine Direction
    if score_long >= 45 and score_long > score_short:
        direction = "LONG"
        confidence = min(score_long, 95)
        reasons = long_reasons
        entry = current_price
        trigger = candle_high * 1.0005
        sl = candle_low - (1.3 * atr)
        risk_pct = ((entry - sl) / entry) * 100
        if risk_pct < 1.4:
            sl = entry * 0.985
            risk_pct = 1.5
        tp1 = max(ema20, entry * (1 + risk_pct * 1.8 / 100))
        tp2 = entry * (1 + risk_pct * 4.0 / 100)
        rr = (tp2 - entry) / (entry - sl) if (entry - sl) > 0 else 4.0
    elif score_short >= 45 and score_short > score_long:
        direction = "SHORT"
        confidence = min(score_short, 95)
        reasons = short_reasons
        entry = current_price
        trigger = candle_low * 0.9995
        sl = candle_high + (1.3 * atr)
        risk_pct = ((sl - entry) / entry) * 100
        if risk_pct < 1.4:
            sl = entry * 1.015
            risk_pct = 1.5
        tp1 = min(ema20, entry * (1 - risk_pct * 1.8 / 100))
        tp2 = entry * (1 - risk_pct * 4.0 / 100)
        rr = (entry - tp2) / (sl - entry) if (sl - entry) > 0 else 4.0
    else:
        return None

    # FINANCIAL FRICTION FILTER:
    # Roundtrip taker fee (0.10%) + conservative spread (0.03%) = 0.13%
    # If distance to TP1 is less than 3.5x friction (< 0.45%), disqualify
    expected_gain_tp1_pct = abs(tp1 - entry) / entry * 100
    if expected_gain_tp1_pct < 0.50:
        return None

    # CONVICTION QUALITY FILTER:
    # For Tier S (Maximum Institutional Conviction >= 80%), climax volume
    # (vol_ratio >= 1.4x) or massive absorption wick (>= 60%) is MANDATORY.
    # Without institutional volume or wick footprint, cannot qualify as Tier S by RSI alone.
    has_volume_or_wick_climax = (vol_ratio >= 1.4) or (effective_lower_wick >= 60 if direction == "LONG" else effective_upper_wick >= 60)
    if confidence >= 80 and not has_volume_or_wick_climax:
        confidence = 74 # Downgraded to Tier A+ due to lack of institutional volume

    # Assign Tier
    if confidence >= 80:
        tier = "Tier S (🔥 Maximum Institutional Conviction)"
    elif confidence >= 65:
        tier = "Tier A+ (High Conviction)"
    elif confidence >= 55:
        tier = "Tier A (Strong Confluence)"
    else:
        tier = "Tier B+ (Moderate Opportunity)"

    return {
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "tier": tier,
        "tier_code": tier_code(confidence),
        "interval": interval,
        "price": current_price,
        "trigger": trigger,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": round(rr, 2),
        "risk_pct": round(risk_pct, 2),
        "rsi": round(rsi_15m, 1),
        "rsi_15m": round(rsi_15m, 1),  # legacy key, holds the RSI of `interval`
        "vol_ratio": round(vol_ratio, 1),
        "lower_wick": round(effective_lower_wick, 1),
        "upper_wick": round(effective_upper_wick, 1),
        "reasons": reasons
    }

def enrich_candidate_microstructure(cand):
    sym = cand['symbol']
    micro = me.get_symbol_microstructure(sym, period=cand.get('interval', DEFAULT_INTERVAL))
    if not micro:
        return cand
    cand['micro'] = micro
    score = cand['confidence']
    direction = cand['direction']
    reasons = cand['reasons']

    regime = micro['regime']
    absorption = micro['absorption']
    t_ratio = micro['taker_ratio']
    oi_pct = micro['oi_change_pct']
    funding_rate = micro['funding_rate_pct']

    if direction == 'SHORT':
        # Strongly penalize if market is in active Long Build-Up (aggressive buying + rising OI)
        if regime == 'LONG_BUILDUP':
            score -= 30
            reasons.append(f"⚠️ MACRO PENALTY: Active Long Build-up (Taker={t_ratio:.2f}, OI={oi_pct:+.2f}%)")
        elif absorption == 'BEARISH_ABSORPTION':
            score += 15
            reasons.append(f"🔬 ORDER FLOW: {micro['absorption_desc']}")
        elif regime == 'SHORT_SQUEEZE':
            score += 10
            reasons.append(f"🔬 ORDER FLOW: {micro['regime_desc']}")
        # Penalize if funding rate is deeply negative (crowded short)
        if funding_rate < -0.015:
            score -= 15
            reasons.append(f"⚠️ CROWDED: Negative funding ({funding_rate:.4f}%)")

    elif direction == 'LONG':
        # Strongly penalize if market is in active Short Build-Up (aggressive selling + rising OI)
        if regime == 'SHORT_BUILDUP':
            score -= 30
            reasons.append(f"⚠️ MACRO PENALTY: Active Short Build-up (Taker={t_ratio:.2f}, OI={oi_pct:+.2f}%)")
        elif absorption == 'BULLISH_ABSORPTION':
            score += 15
            reasons.append(f"🔬 ORDER FLOW: {micro['absorption_desc']}")
        elif regime == 'LONG_BUILDUP':
            score += 10
            reasons.append(f"🔬 ORDER FLOW: {micro['regime_desc']}")
        # Penalize if funding rate is excessively positive (crowded long)
        if funding_rate > 0.035:
            score -= 15
            reasons.append(f"⚠️ CROWDED: Excessive positive funding ({funding_rate:.4f}%)")

    cand['confidence'] = max(20, min(95, score))
    if cand['confidence'] >= 80:
        cand['tier'] = "Tier S (🔥 Maximum Microstructural Conviction)"
    elif cand['confidence'] >= 65:
        cand['tier'] = "Tier A+ (High Confirmed Conviction)"
    elif cand['confidence'] >= 55:
        cand['tier'] = "Tier A (Strong Confluence / Hedge)"
    else:
        cand['tier'] = "Disqualified (<55%)"
    cand['tier_code'] = tier_code(cand['confidence']) if cand['confidence'] >= 55 else "DISQUALIFIED"
    return cand

def scan_all_liquid_pairs(top_n=DEFAULT_UNIVERSE, interval=DEFAULT_INTERVAL):
    """Scans the `top_n` most liquid USDT-M perpetuals on `interval` candles.
    Raises on exchangeInfo / ticker failures so callers can fail loudly instead of reporting 'no setups'."""
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"Unsupported interval '{interval}'. Must be one of: {', '.join(SUPPORTED_INTERVALS)}")
    # Fetch liquid symbols
    info_req = urllib.request.Request('https://fapi.binance.com/fapi/v1/exchangeInfo', headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(info_req, timeout=8) as r:
        info = json.loads(r.read().decode())

    valid_symbols = [
        s['symbol'] for s in info['symbols']
        if s.get('underlyingType') == 'COIN'
        and s.get('contractType') == 'PERPETUAL'
        and s.get('quoteAsset') == 'USDT'
        and s.get('status') == 'TRADING'
        and not any(x in s['symbol'] for x in ['USDC', 'EUR', 'XAU', 'XAG', 'PAXG', 'BUSD'])
    ]

    ticker_req = urllib.request.Request('https://fapi.binance.com/fapi/v1/ticker/24hr', headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(ticker_req, timeout=8) as r:
        tickers = json.loads(r.read().decode())

    ticker_map = {t['symbol']: float(t['quoteVolume']) for t in tickers if t['symbol'] in valid_symbols}
    sorted_symbols = sorted(ticker_map.keys(), key=lambda s: ticker_map[s], reverse=True)[:top_n]

    raw_results = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        future_to_symbol = {executor.submit(analyze_single_symbol, sym, interval): sym for sym in sorted_symbols}
        for future in as_completed(future_to_symbol):
            res = future.result()
            if res:
                raw_results.append(res)

    # Filter and enrich candidate microstructure concurrently
    with ThreadPoolExecutor(max_workers=8) as ex:
        enriched_results = list(ex.map(enrich_candidate_microstructure, raw_results))

    # Filter qualified candidates only (>= 55% confidence)
    qualified = [c for c in enriched_results if c['confidence'] >= 55]
    qualified.sort(key=lambda x: x['confidence'], reverse=True)
    return qualified

def _json_default(obj):
    """Serializes numpy scalars and any other non-JSON type deterministically."""
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(obj)

def emit_json(payload, stream=None):
    stream = stream or sys.stdout
    stream.write(json.dumps(payload, indent=2, default=_json_default) + "\n")
    stream.flush()

def _standard_leverage():
    """Standard leverage from the user profile (used only for the informational ROE estimate)."""
    try:
        import user_profile as up
        prof = up.load_user_profile()
        return max(1, min(int(prof.get("leverage_standard", 3)), up.get_leverage_ceiling(prof)))
    except Exception:
        return 3

def build_scan_payload(candidates, env, interval, universe, top, latency_ms):
    leverage = _standard_leverage()
    selected = candidates[:top] if top else candidates
    out = []
    for c in selected:
        item = dict(c)
        item["micro"] = c.get("micro")
        item["roe_est_pct"] = round(c["risk_pct"] * c["rr"] * leverage, 1)
        out.append(item)
    return {
        "status": "ok",
        "command": "scan",
        "env": env,
        "interval": interval,
        "universe_size": universe,
        "leverage_standard": leverage,
        "qualified_count": len(candidates),
        "count": len(out),
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "latency_ms": latency_ms,
        "candidates": out,
    }

def print_text_report(payload):
    print(f"Total qualified candidates with microstructure ({payload['interval']}, env={payload['env']}): "
          f"{payload['qualified_count']}\n")
    for c in payload["candidates"]:
        m = c.get('micro') or {}
        print(f"• {c['tier']} | {c['symbol']} ({c['direction']}) -> {c['confidence']}%")
        print(f"  Entry: {c['price']} | Trigger: {c['trigger']:.4f} | SL: {c['sl']:.4f} (-{c['risk_pct']}%) | "
              f"TP1: {c['tp1']:.4f} | TP2: {c['tp2']:.4f} | R:R {c['rr']}:1 | "
              f"ROE est ({payload['leverage_standard']}x): +{c['roe_est_pct']}%")
        print(f"  Flow: Taker={m.get('taker_ratio', 1.0):.2f} | OI={m.get('oi_change_pct', 0.0):+.2f}% | "
              f"Regime={m.get('regime', 'N/A')} | Funding={m.get('funding_rate_pct', 0.0):.4f}%")
        print(f"  Factors: {', '.join(c['reasons'])}\n")

def main(argv=None):
    parser = argparse.ArgumentParser(description="Broad Binance Futures market radar (read-only, never places orders)")
    parser.add_argument("--json", action="store_true", help="Print a single JSON document on stdout (diagnostics go to stderr)")
    parser.add_argument("--top", type=int, default=0, help="Max candidates to return (default: all qualified)")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL, choices=SUPPORTED_INTERVALS, help="Candle interval (default 15m)")
    parser.add_argument("--universe", type=int, default=DEFAULT_UNIVERSE, help="Number of most liquid pairs to scan (default 80)")
    parser.add_argument("--env", default=None, help="prod|testnet (resolved via env_resolver; market data is always public mainnet)")
    args = parser.parse_args(argv)

    if args.top < 0 or args.universe < 1:
        parser.print_usage(sys.stderr)
        sys.stderr.write("error: --top must be >= 0 and --universe >= 1\n")
        return 2
    try:
        from utils.env_resolver import resolve_env
        env = resolve_env(args.env)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    real_stdout = sys.stdout
    t0 = time.time()
    try:
        # Keep stdout pure JSON: anything printed by library code goes to stderr.
        with contextlib.redirect_stdout(sys.stderr if args.json else real_stdout):
            candidates = scan_all_liquid_pairs(top_n=args.universe, interval=args.interval)
    except Exception as e:
        err = {"status": "error", "command": "scan", "env": env, "interval": args.interval,
               "error": f"{type(e).__name__}: {e}"}
        if args.json:
            emit_json(err, real_stdout)
        else:
            sys.stderr.write(f"Radar scan failed: {err['error']}\n")
        return 1

    payload = build_scan_payload(candidates, env, args.interval, args.universe, args.top,
                                 int((time.time() - t0) * 1000))
    if args.json:
        emit_json(payload, real_stdout)
    else:
        print_text_report(payload)
    return 0

if __name__ == "__main__":
    sys.exit(main())
