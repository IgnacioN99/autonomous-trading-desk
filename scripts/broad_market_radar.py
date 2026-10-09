#!/usr/bin/env python3
"""
broad_market_radar.py - Broad Market and Multi-Conviction Screener.
Concurrently scans top Binance Futures contracts (5m, 15m or 1h), ranking opportunities
by confidence tier: Tier S (Maximum), Tier A+ (High), Tier A (Strong), and Delta hedges.

Read-only: uses public Binance Futures market data and never places orders.

Levels are measured from the trigger (the effective entry); `trigger_distance_pct` is signed (> 0: the trigger is
beyond the price in the trade direction). The enrichment never lifts a row to Tier S without institutional volume
or a >= 60% wick (`tier_s_eligible`). Rows above the risk_pct ceiling are dropped with a stderr count.

CLI:
    python3 scripts/broad_market_radar.py [--json] [--top N] [--interval 15m|5m|1h]
                                          [--universe N] [--env prod|testnet]
Exit codes: 0 ok, 1 data/API error, 2 bad usage.
"""

import urllib.error
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
from utils import rate_limit_guard
from utils import squeeze_filter as sqf

SUPPORTED_INTERVALS = ("5m", "15m", "1h")
DEFAULT_INTERVAL = "15m"
DEFAULT_UNIVERSE = 80
# Stop distance from the trigger, in % (issue #84): below the floor the SL is widened to 1.5%; above the ceiling the
# row is flagged and dropped from the qualified list (TP2 = 4R would be out of intraday reach).
MIN_RISK_PCT = 1.4
MAX_RISK_PCT = 5.0

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

def _get_json(url, timeout):
    """Public market-data GET through the process-wide rate-limit guard (utils/rate_limit_guard.py): an active ban
    raises RateLimitedError before the request; HTTP 429/418 trip it (enabled guard only)."""
    rate_limit_guard.raise_if_banned()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        rate_limit_guard.on_http_error(e)
        raise

def fetch_klines(symbol, interval=DEFAULT_INTERVAL, limit=55):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return _get_json(url, 6)

def tier_code(confidence):
    """Stable machine-readable tier code (the `tier` field is a human label)."""
    if confidence >= 80:
        return "S"
    if confidence >= 65:
        return "A+"
    if confidence >= 55:
        return "A"
    return "B+"

def _add_cap_component(components, final_score):
    """Books the cap/floor adjustment so that sum(components) == final_score (issue #202). The 95 and 74 caps
    are negative points under `cap`; the enrichment's floor of 20 is positive points under `floor`."""
    delta = int(final_score) - sum(components.values())
    if delta < 0:
        components["cap"] = components.get("cap", 0) + delta
    elif delta > 0:
        components["floor"] = components.get("floor", 0) + delta

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
    candle_vol = volumes[-2]  # closed wick candle: the forming candle's volume is partial (issue #83)

    # Absorption wicks: BOTH sides from the same, last CLOSED candle (klines[-1] is still forming, so its wicks
    # are not final). Never mix sides across candles: lower + upper must stay <= 100% of one range (issue #20).
    wick_kline = klines[-2]
    # The stop must sit beyond the wick that justifies the trade, not inside it: anchor on the more extreme of
    # the forming candle and the wick candle (issue #20).
    wick_high = float(wick_kline[2])
    wick_low = float(wick_kline[3])
    # Zero-range guard on the candle the wicks come from, not on the forming one (issue #133)
    if wick_high - wick_low <= 0:
        return None
    wick_candle_open_time = int(wick_kline[0])
    effective_lower_wick, effective_upper_wick = me.candle_wick_pcts(wick_kline)

    rsi_15m = calculate_rsi(closes, period=14)
    emas = calculate_ema(closes, period=20)
    ema20 = emas[-1] if emas else current_price

    avg_vol = sum(volumes[-22:-2]) / 20 if len(volumes) >= 22 else candle_vol
    vol_ratio = (candle_vol / avg_vol) if avg_vol > 0 else 1.0

    recent_high = max(highs[-40:])
    recent_low = min(lows[-40:])

    atr = calculate_atr(highs, lows, closes, period=14)

    score_long = 0
    score_short = 0
    long_reasons = []
    short_reasons = []
    # Structured points per factor (issue #202): sum(score_components) == confidence, caps included
    long_points = {}
    short_points = {}

    # 1. RSI Scoring
    if rsi_15m < 28:
        score_long += 35
        long_points["rsi"] = 35
        long_reasons.append(f"RSI {interval} extreme oversold ({rsi_15m:.1f})")
    elif rsi_15m < 35:
        score_long += 25
        long_points["rsi"] = 25
        long_reasons.append(f"RSI {interval} oversold ({rsi_15m:.1f})")
    elif rsi_15m > 74:
        score_short += 35
        short_points["rsi"] = 35
        short_reasons.append(f"RSI {interval} extreme overbought ({rsi_15m:.1f})")
    elif rsi_15m > 66:
        score_short += 25
        short_points["rsi"] = 25
        short_reasons.append(f"RSI {interval} overbought ({rsi_15m:.1f})")

    # 2. Institutional Absorption Wicks
    if effective_lower_wick >= 50:
        score_long += 35
        long_points["wick"] = 35
        long_reasons.append(f"Massive buyer absorption wick ({effective_lower_wick:.0f}%)")
    elif effective_lower_wick >= 30:
        score_long += 20
        long_points["wick"] = 20
        long_reasons.append(f"Support rejection ({effective_lower_wick:.0f}% lower wick)")

    if effective_upper_wick >= 50:
        score_short += 35
        short_points["wick"] = 35
        short_reasons.append(f"Massive seller absorption wick ({effective_upper_wick:.0f}%)")
    elif effective_upper_wick >= 30:
        score_short += 20
        short_points["wick"] = 20
        short_reasons.append(f"Resistance rejection ({effective_upper_wick:.0f}% upper wick)")

    # 3. Volume Climax
    if vol_ratio >= 1.8:
        pts = 20
        r_txt = f"Volume climax {vol_ratio:.1f}x average"
        if score_long >= score_short:
            score_long += pts
            long_reasons.append(r_txt)
            long_points["volume"] = pts
        else:
            score_short += pts
            short_reasons.append(r_txt)
            short_points["volume"] = pts
    elif vol_ratio >= 1.3:
        pts = 10
        r_txt = f"Volume expansion {vol_ratio:.1f}x average"
        if score_long >= score_short:
            score_long += pts
            long_reasons.append(r_txt)
            long_points["volume"] = pts
        else:
            score_short += pts
            short_reasons.append(r_txt)
            short_points["volume"] = pts

    # 4. Liquidity Sweeps
    if current_price <= recent_low * 1.012:
        score_long += 15
        long_points["sweep"] = 15
        long_reasons.append("Liquidity sweep at local lows")
    if current_price >= recent_high * 0.988:
        score_short += 15
        short_points["sweep"] = 15
        short_reasons.append("Liquidity sweep at local highs")

    # 5. Elastic Distance to EMA 20 (Mean Reversion)
    dist_to_ema = ((ema20 - current_price) / current_price) * 100
    if dist_to_ema > 1.8:
        score_long += 10
        long_points["ema_stretch"] = 10
        long_reasons.append(f"Discount vs EMA 20 ({dist_to_ema:+.1f}%)")
    elif dist_to_ema < -1.8:
        score_short += 10
        short_points["ema_stretch"] = 10
        short_reasons.append(f"Overextension vs EMA 20 ({dist_to_ema:+.1f}%)")

    # Determine Direction. Every level is measured from the effective entry, the breakout trigger the order enters
    # at (issue #86); `price` stays informational.
    if score_long >= 45 and score_long > score_short:
        direction = "LONG"
        confidence = min(score_long, 95)
        reasons = long_reasons
        score_components = dict(long_points)
        trigger = candle_high * 1.0005
        entry = trigger
        sl = min(candle_low, wick_low) - (1.3 * atr)
        risk_pct = ((entry - sl) / entry) * 100
        if risk_pct < MIN_RISK_PCT:
            sl = entry * 0.985
            risk_pct = 1.5
        tp1 = max(ema20, entry * (1 + risk_pct * 1.8 / 100))
        tp2 = entry * (1 + risk_pct * 4.0 / 100)
        rr = (tp2 - entry) / (entry - sl) if (entry - sl) > 0 else 4.0
    elif score_short >= 45 and score_short > score_long:
        direction = "SHORT"
        confidence = min(score_short, 95)
        reasons = short_reasons
        score_components = dict(short_points)
        trigger = candle_low * 0.9995
        entry = trigger
        sl = max(candle_high, wick_high) + (1.3 * atr)
        risk_pct = ((sl - entry) / entry) * 100
        if risk_pct < MIN_RISK_PCT:
            sl = entry * 1.015
            risk_pct = 1.5
        tp1 = min(ema20, entry * (1 - risk_pct * 1.8 / 100))
        tp2 = entry * (1 - risk_pct * 4.0 / 100)
        rr = (entry - tp2) / (sl - entry) if (sl - entry) > 0 else 4.0
    else:
        return None

    # No radar friction filter (issue #141): with the risk floor and TP1 >= 1.8R, TP1 is always >= 2.52% from the
    # trigger, so a 0.50% check could never reject. The binding checks are executor Gate 3 and evaluator K3.

    # SCORE QUALITY FILTER:
    # For Tier S (heuristic score >= 80, not a probability), climax volume
    # (vol_ratio >= 1.4x) or massive absorption wick (>= 60%) is MANDATORY.
    # Without institutional volume or wick footprint, cannot qualify as Tier S by RSI alone.
    has_volume_or_wick_climax = (vol_ratio >= 1.4) or (effective_lower_wick >= 60 if direction == "LONG" else effective_upper_wick >= 60)
    if not has_volume_or_wick_climax:
        confidence = min(confidence, 74)  # issue #165: ineligible rows stay inside the A+ band (no 75-79 gap)
    _add_cap_component(score_components, confidence)

    # Assign Tier
    if confidence >= 80:
        tier = "Tier S (🔥 Top Score, heuristic)"
    elif confidence >= 65:
        tier = "Tier A+ (High Score)"
    elif confidence >= 55:
        tier = "Tier A (Strong Confluence)"
    else:
        tier = "Tier B+ (Moderate Opportunity)"

    row = {
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "tier": tier,
        "tier_code": tier_code(confidence),
        "interval": interval,
        "price": current_price,
        "trigger": trigger,
        # Signed (issue #140): > 0 means the trigger is beyond the price in the trade direction. The trigger comes
        # from the forming high/low and high >= close >= low, so it is never crossed at scan time; crossing happens
        # at execution time, where the executor gates use the current price.
        "trigger_distance_pct": round((trigger - current_price if direction == "LONG" else current_price - trigger)
                                      / current_price * 100, 2),
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
        "wick_candle_open_time": wick_candle_open_time,  # open time (ms) of the closed candle both wicks come from
        # Exact (unrounded) volume-or-wick rule, re-applied after the microstructure enrichment (issue #134)
        "tier_s_eligible": bool(has_volume_or_wick_climax),
        # Exact (unrounded) climax part of the macro rule for altcoin shorts (issue #206), read by the pipeline gate
        "alt_short_climax_ok": bool(vol_ratio >= sqf.ALT_SHORT_CLIMAX_VOL),
        "score_components": score_components,
        "reasons": reasons
    }
    # Issue #84: a stop this far from the trigger puts TP2 (4R) out of intraday reach. The row is flagged here and
    # dropped by scan_all_liquid_pairs after the microstructure enrichment.
    if risk_pct > MAX_RISK_PCT:
        row["risk_pct_over_ceiling"] = True
        row["disqualify_reason"] = f"risk_pct {risk_pct:.2f}% > {MAX_RISK_PCT}% intraday ceiling"
    return row

def enrich_candidate_microstructure(cand, funding_intervals=None):
    """Order-flow enrichment of one radar row. `funding_intervals` ({symbol: hours} from fetch_funding_intervals)
    normalizes funding to 8h for the squeeze thresholds (issue #206); a symbol not in it (or None) uses 8h."""
    sym = cand['symbol']
    # Same wick candle as the scan, so the ORDER FLOW text and the wick fields describe one candle (issue #20)
    micro = me.get_symbol_microstructure(sym, period=cand.get('interval', DEFAULT_INTERVAL),
                                         wick_candle_open_time=cand.get('wick_candle_open_time'))
    interval_h = sqf.funding_interval_h(sym, funding_intervals)
    cand['funding_interval_h'] = interval_h
    if not micro:
        # Issue #206: a SHORT without micro data cannot be checked for squeeze risk, so it is capped (fail closed)
        cand['funding_rate_pct'] = None
        cand['funding_rate_8h_pct'] = None
        if cand.get('direction') == 'SHORT':
            comps = dict(cand.get('score_components') or {"base": int(cand['confidence'])})
            cand['confidence'] = _apply_squeeze_cap(cand, comps, int(cand['confidence']),
                                                    sqf.short_squeeze_reasons(micro))
            cand['score_components'] = comps
            _set_tier(cand)
        return cand
    cand['micro'] = micro
    cand['funding_rate_pct'] = micro.get('funding_rate_pct')  # raw, per funding interval
    funding_8h = sqf.normalize_funding_8h(micro.get('funding_rate_pct'), interval_h)
    cand['funding_rate_8h_pct'] = round(funding_8h, 4) if funding_8h is not None else None
    # Squeeze / crowding thresholds are per 8h: the helpers read the normalized value (the micro dict is unchanged)
    squeeze_micro = dict(micro, funding_rate_8h_pct=funding_8h)
    score = cand['confidence']
    direction = cand['direction']
    reasons = cand['reasons']
    # Rows built without components (legacy callers) start from one `base` entry, so the sum rule still holds
    comps = dict(cand.get('score_components') or {"base": int(score)})

    def add(key, pts):
        comps[key] = comps.get(key, 0) + pts
        return pts

    regime = micro['regime']
    absorption = micro['absorption']
    t_ratio = micro['taker_ratio']
    oi_pct = micro['oi_change_pct']
    funding_rate = micro['funding_rate_pct']
    # Absorption is only scored when wick and taker data describe the wick candle (issue #83); a mismatch can
    # only remove the bonus, never add score.
    # A missing flag counts as unmatched (fail closed, issue #135).
    absorption_scored = not (micro.get('wick_candle_mismatch') or micro.get('taker_candle_matched') is not True)
    if not absorption_scored:
        reasons.append("🔬 ORDER FLOW: absorption not scored (wick/taker candle mismatch)")
    # Each value is labelled with the candle it comes from (issue #135): the price change is the forming candle's,
    # OI is the latest row, the taker ratio is the closed wick candle's.
    flow_txt = (f"ΔP(forming)={micro.get('price_change_pct', 0.0):+.2f}%, OI(latest)={oi_pct:+.2f}%, "
                f"Taker(closed)={t_ratio:.2f}")

    if direction == 'SHORT':
        # Strongly penalize if market is in active Long Build-Up (aggressive buying + rising OI)
        if regime == 'LONG_BUILDUP':
            score += add("flow_penalty", -30)
            reasons.append(f"⚠️ MACRO PENALTY: Active Long Build-up ({flow_txt})")
        elif absorption == 'BEARISH_ABSORPTION' and absorption_scored:
            score += add("absorption", 15)
            reasons.append(f"🔬 ORDER FLOW: {micro['absorption_desc']}")
        elif regime == 'SHORT_SQUEEZE':
            score += add("flow_regime", 10)
            reasons.append(f"🔬 ORDER FLOW: {micro['regime_desc']}")
        # Penalize if funding rate is deeply negative (crowded short; raw per-interval rate, unchanged by #206)
        if funding_rate < -0.015:
            score += add("funding", -15)
            reasons.append(f"⚠️ CROWDED: Negative funding ({funding_rate:.4f}%)")

    elif direction == 'LONG':
        # Strongly penalize if market is in active Short Build-Up (aggressive selling + rising OI)
        if regime == 'SHORT_BUILDUP':
            score += add("flow_penalty", -30)
            reasons.append(f"⚠️ MACRO PENALTY: Active Short Build-up ({flow_txt})")
        elif absorption == 'BULLISH_ABSORPTION' and absorption_scored:
            score += add("absorption", 15)
            reasons.append(f"🔬 ORDER FLOW: {micro['absorption_desc']}")
        elif regime == 'LONG_BUILDUP':
            score += add("flow_regime", 10)
            reasons.append(f"🔬 ORDER FLOW: {micro['regime_desc']}")
        # Penalize if funding rate is excessively positive (crowded long)
        if funding_rate > 0.035:
            score += add("funding", -15)
            reasons.append(f"⚠️ CROWDED: Excessive positive funding ({funding_rate:.4f}%)")

    # Tier S still needs institutional volume or a >= 60% wick after the enrichment (issue #134): same cap as
    # analyze_single_symbol, unconditional since #165 (75-79 never stays on an ineligible row). A missing flag is
    # not eligible (fail closed).
    if cand.get('tier_s_eligible') is not True:
        score = min(score, 74)
    cand['absorption_scored'] = absorption_scored
    cand['confidence'] = max(20, min(95, score))
    _add_cap_component(comps, cand['confidence'])
    if direction == 'SHORT':
        cand['confidence'] = _apply_squeeze_cap(cand, comps, cand['confidence'],
                                                sqf.short_squeeze_reasons(squeeze_micro))
    elif direction == 'LONG':
        crowding = sqf.long_crowding_reasons(squeeze_micro)
        cand['long_crowding_risk'] = bool(crowding)
        cand['long_crowding_reasons'] = crowding
    cand['score_components'] = comps
    _set_tier(cand)
    return cand

def _apply_squeeze_cap(cand, comps, confidence, reasons):
    """Issue #206: a SHORT with squeeze risk is flagged and capped at Tier A (score 64), booked as `squeeze_cap` so
    that sum(components) == confidence. A score already at or below 64 only gets the flag. Returns the score."""
    cand['squeeze_risk'] = bool(reasons)
    cand['squeeze_reasons'] = list(reasons)
    if not reasons:
        return confidence
    if confidence > sqf.SQUEEZE_SCORE_CAP:
        comps['squeeze_cap'] = comps.get('squeeze_cap', 0) + sqf.SQUEEZE_SCORE_CAP - confidence
        confidence = sqf.SQUEEZE_SCORE_CAP
    # First line (the brief table shows the SQZ marker instead and skips it)
    cand.setdefault('reasons', []).insert(0, f"{sqf.SQUEEZE_REASON_PREFIX} {', '.join(reasons)} → capped at Tier A")
    return confidence

def _set_tier(cand):
    """Tier label and code from the final enrichment confidence."""
    if cand['confidence'] >= 80:
        cand['tier'] = "Tier S (🔥 Top Score, order flow confirmed)"
    elif cand['confidence'] >= 65:
        cand['tier'] = "Tier A+ (High Confirmed Score)"
    elif cand['confidence'] >= 55:
        cand['tier'] = "Tier A (Strong Confluence / Hedge)"
    else:
        cand['tier'] = "Disqualified (score < 55)"
    cand['tier_code'] = tier_code(cand['confidence']) if cand['confidence'] >= 55 else "DISQUALIFIED"
    return cand

FUNDING_INFO_URL = 'https://fapi.binance.com/fapi/v1/fundingInfo'

def fetch_funding_intervals():
    """({symbol: fundingIntervalHours}, warning) from the public /fapi/v1/fundingInfo, fetched once per scan (issue
    #206). It lists only symbols with adjusted funding parameters; the rest use Binance's 8h default. Any failure
    except an active rate-limit ban returns ({}, warning): every symbol is then read as 8h and the scan continues
    (a 429/418 still trips the guard in _get_json, so the run reports UNAVAILABLE as usual)."""
    try:
        return sqf.parse_funding_intervals(_get_json(FUNDING_INFO_URL, 8)), None
    except rate_limit_guard.RateLimitedError:
        raise
    except Exception as e:
        return {}, f"fundingInfo unavailable ({type(e).__name__}): funding read as 8h for every symbol"

def scan_all_liquid_pairs(top_n=DEFAULT_UNIVERSE, interval=DEFAULT_INTERVAL, funding_status=None):
    """Scans the `top_n` most liquid USDT-M perpetuals on `interval` candles.
    Raises on exchangeInfo / ticker failures so callers can fail loudly instead of reporting 'no setups'.
    `funding_status` (optional dict) receives {"warning": ...} when fundingInfo failed (issue #206)."""
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"Unsupported interval '{interval}'. Must be one of: {', '.join(SUPPORTED_INTERVALS)}")
    # Fetch liquid symbols
    info = _get_json('https://fapi.binance.com/fapi/v1/exchangeInfo', 8)

    valid_symbols = [
        s['symbol'] for s in info['symbols']
        if s.get('underlyingType') == 'COIN'
        and s.get('contractType') == 'PERPETUAL'
        and s.get('quoteAsset') == 'USDT'
        and s.get('status') == 'TRADING'
        and not any(x in s['symbol'] for x in ['USDC', 'EUR', 'XAU', 'XAG', 'PAXG', 'BUSD'])
    ]

    tickers = _get_json('https://fapi.binance.com/fapi/v1/ticker/24hr', 8)

    ticker_map = {t['symbol']: float(t['quoteVolume']) for t in tickers if t['symbol'] in valid_symbols}
    sorted_symbols = sorted(ticker_map.keys(), key=lambda s: ticker_map[s], reverse=True)[:top_n]

    raw_results = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        future_to_symbol = {executor.submit(analyze_single_symbol, sym, interval): sym for sym in sorted_symbols}
        for future in as_completed(future_to_symbol):
            res = future.result()
            if res:
                raw_results.append(res)

    funding_intervals, funding_warning = fetch_funding_intervals() if raw_results else ({}, None)
    if funding_warning:
        print(f"Radar: {funding_warning}.", file=sys.stderr)
        if isinstance(funding_status, dict):
            funding_status["warning"] = funding_warning

    # Filter and enrich candidate microstructure concurrently
    with ThreadPoolExecutor(max_workers=8) as ex:
        enriched_results = list(ex.map(lambda c: enrich_candidate_microstructure(c, funding_intervals), raw_results))

    # Filter qualified candidates only (>= 55% confidence, risk_pct within the intraday ceiling, issue #84)
    qualified = [c for c in enriched_results if c['confidence'] >= 55 and not c.get('risk_pct_over_ceiling')]
    over_ceiling = [c['symbol'] for c in enriched_results if c.get('risk_pct_over_ceiling')]
    if over_ceiling:  # issue #141: tell "no setups" apart from "setups dropped by the ceiling"
        print(f"Radar: dropped {len(over_ceiling)} row(s) with risk_pct above the {MAX_RISK_PCT}% intraday ceiling "
              f"({', '.join(sorted(over_ceiling))}).", file=sys.stderr)
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
        print(f"• {c['tier']} | {c['symbol']} ({c['direction']}) -> score {c['confidence']} (heuristic, not a probability)")
        print(f"  Price: {c['price']} | Trigger (entry): {c['trigger']:.4f} | SL: {c['sl']:.4f} (-{c['risk_pct']}%) | "
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
    err = None
    # Process-wide market-data rate-limit guard: a persisted ban skips the scan without calling Binance; a 429/418
    # during the scan (even one swallowed by a per-symbol fetch) fails the run and is persisted on exit.
    with rate_limit_guard.scan_session():
        if rate_limit_guard.is_banned():
            err = rate_limit_guard.error_payload("scan", env, interval=args.interval)
        else:
            try:
                # Keep stdout pure JSON: anything printed by library code goes to stderr.
                with contextlib.redirect_stdout(sys.stderr if args.json else real_stdout):
                    candidates = scan_all_liquid_pairs(top_n=args.universe, interval=args.interval)
                if rate_limit_guard.is_banned():
                    raise rate_limit_guard.RateLimitedError("Binance rate limit during the scan")
            except Exception as e:
                err = {"status": "error", "command": "scan", "env": env, "interval": args.interval,
                       "error": f"{type(e).__name__}: {e}"}
                if isinstance(e, rate_limit_guard.RateLimitedError):
                    err["market_data_status"] = rate_limit_guard.unavailable_text()
    if err is not None:
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
