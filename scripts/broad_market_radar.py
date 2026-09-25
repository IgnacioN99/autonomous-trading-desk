#!/usr/bin/env python3
"""
broad_market_radar.py - Escáner de Mercado Amplio y Convicción Multi-Nivel.
Analiza concurrentemente el top de Binance Futuros (15m y 1h), clasificando oportunidades
por nivel de confianza: Tier S (Máxima), Tier A+ (Alta), Tier A (Fuerte), y Coberturas Delta.
"""

import urllib.request
import json
import time
import math
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import microstructure_engine as me

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

def analyze_single_symbol(symbol):
    url_15m = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=15m&limit=55"
    req_15m = urllib.request.Request(url_15m, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req_15m, timeout=6) as resp:
            klines_15m = json.loads(resp.read().decode())
    except Exception:
        return None

    if len(klines_15m) < 35:
        return None

    opens = [float(k[1]) for k in klines_15m]
    highs = [float(k[2]) for k in klines_15m]
    lows = [float(k[3]) for k in klines_15m]
    closes = [float(k[4]) for k in klines_15m]
    volumes = [float(k[5]) for k in klines_15m]

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

    # Vela anterior para confirmar mechas de absorción recientes
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
        long_reasons.append(f"RSI 15m sobreventa extrema ({rsi_15m:.1f})")
    elif rsi_15m < 35:
        score_long += 25
        long_reasons.append(f"RSI 15m sobreventa ({rsi_15m:.1f})")
    elif rsi_15m > 74:
        score_short += 35
        short_reasons.append(f"RSI 15m sobrecompra extrema ({rsi_15m:.1f})")
    elif rsi_15m > 66:
        score_short += 25
        short_reasons.append(f"RSI 15m sobrecompra ({rsi_15m:.1f})")

    # 2. Mechas de Absorción Institucional
    if effective_lower_wick >= 50:
        score_long += 35
        long_reasons.append(f"Mecha absorción compradora brutal ({effective_lower_wick:.0f}%)")
    elif effective_lower_wick >= 30:
        score_long += 20
        long_reasons.append(f"Rechazo en mínimos ({effective_lower_wick:.0f}% mecha)")

    if effective_upper_wick >= 50:
        score_short += 35
        short_reasons.append(f"Mecha absorción vendedora ({effective_upper_wick:.0f}%)")
    elif effective_upper_wick >= 30:
        score_short += 20
        short_reasons.append(f"Rechazo en máximos ({effective_upper_wick:.0f}% mecha)")

    # 3. Volumen Clímax
    if vol_ratio >= 1.8:
        pts = 20
        r_txt = f"Volumen clímax {vol_ratio:.1f}x media"
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

    # 4. Barrido de Mínimos / Máximos (Liquidity Sweeps)
    if current_price <= recent_low * 1.012:
        score_long += 15
        long_reasons.append("Barrido de liquidez en mínimos locales")
    if current_price >= recent_high * 0.988:
        score_short += 15
        short_reasons.append("Barrido de liquidez en máximos locales")

    # 5. Distancia elástica a EMA 20 (Mean Reversion)
    dist_to_ema = ((ema20 - current_price) / current_price) * 100
    if dist_to_ema > 1.8:
        score_long += 10
        long_reasons.append(f"Descuento frente a EMA 20 ({dist_to_ema:+.1f}%)")
    elif dist_to_ema < -1.8:
        score_short += 10
        short_reasons.append(f"Extensión alcista vs EMA 20 ({dist_to_ema:+.1f}%)")

    # Determinar dirección
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

    # FILTRO INSTITUCIONAL DE FRICCIÓN FINANCIERA:
    # Costo taker roundtrip (0.10%) + spread conservador (0.03%) = 0.13%
    # Si la distancia a TP1 es menor al triple de la fricción (< 0.45%), se descarta
    expected_gain_tp1_pct = abs(tp1 - entry) / entry * 100
    if expected_gain_tp1_pct < 0.50:
        return None

    # REGLA RIGUROSA DE CONVICCIÓN:
    # Para calificar como Tier S (Convicción Máxima >= 80%), es OBLIGATORIO que haya
    # volumen clímax real (vol_ratio >= 1.4x) o mecha brutal (>= 60%).
    # Sin volumen o mecha institucional, no puede ser Tier S por simple RSI.
    has_volume_or_wick_climax = (vol_ratio >= 1.4) or (effective_lower_wick >= 60 if direction == "LONG" else effective_upper_wick >= 60)
    if confidence >= 80 and not has_volume_or_wick_climax:
        confidence = 74 # Degradado a Tier A+ por falta de volumen institucional

    # Asignar Tier
    if confidence >= 80:
        tier = "Tier S (🔥 Máxima Convicción Institucional)"
    elif confidence >= 65:
        tier = "Tier A+ (Alta Convicción)"
    elif confidence >= 55:
        tier = "Tier A (Fuerte Confluencia)"
    else:
        tier = "Tier B+ (Oportunidad Moderada)"

    return {
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "tier": tier,
        "price": current_price,
        "trigger": trigger,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": round(rr, 2),
        "risk_pct": round(risk_pct, 2),
        "rsi_15m": round(rsi_15m, 1),
        "vol_ratio": round(vol_ratio, 1),
        "lower_wick": round(effective_lower_wick, 1),
        "upper_wick": round(effective_upper_wick, 1),
        "reasons": reasons
    }

def enrich_candidate_microstructure(cand):
    sym = cand['symbol']
    micro = me.get_symbol_microstructure(sym)
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
        # Penalizar fuertemente si el mercado está en Long Build-Up (compras agresivas + OI subiendo)
        if regime == 'LONG_BUILDUP':
            score -= 30
            reasons.append(f"⚠️ PENALIZACIÓN MACRO: Long Build-up activo (Taker={t_ratio:.2f}, OI={oi_pct:+.2f}%)")
        elif absorption == 'BEARISH_ABSORPTION':
            score += 15
            reasons.append(f"🔬 ORDER FLOW: {micro['absorption_desc']}")
        elif regime == 'SHORT_SQUEEZE':
            score += 10
            reasons.append(f"🔬 ORDER FLOW: {micro['regime_desc']}")
        # Penalizar si el funding rate es negativo extremo (crowded short)
        if funding_rate < -0.015:
            score -= 15
            reasons.append(f"⚠️ CROWDED: Funding negativo ({funding_rate:.4f}%)")

    elif direction == 'LONG':
        # Penalizar fuertemente si el mercado está en Short Build-Up (ventas agresivas + OI subiendo)
        if regime == 'SHORT_BUILDUP':
            score -= 30
            reasons.append(f"⚠️ PENALIZACIÓN MACRO: Short Build-up activo (Taker={t_ratio:.2f}, OI={oi_pct:+.2f}%)")
        elif absorption == 'BULLISH_ABSORPTION':
            score += 15
            reasons.append(f"🔬 ORDER FLOW: {micro['absorption_desc']}")
        elif regime == 'LONG_BUILDUP':
            score += 10
            reasons.append(f"🔬 ORDER FLOW: {micro['regime_desc']}")
        # Penalizar si el funding rate es excesivamente positivo (crowded long)
        if funding_rate > 0.035:
            score -= 15
            reasons.append(f"⚠️ CROWDED: Funding excesivo ({funding_rate:.4f}%)")

    cand['confidence'] = max(20, min(95, score))
    if cand['confidence'] >= 80:
        cand['tier'] = "Tier S (🔥 Convicción Microestructural Máxima)"
    elif cand['confidence'] >= 65:
        cand['tier'] = "Tier A+ (Alta Convicción Confirmada)"
    elif cand['confidence'] >= 55:
        cand['tier'] = "Tier A (Fuerte Confluencia / Cobertura)"
    else:
        cand['tier'] = "Descalificado (<55%)"
    return cand

def scan_all_liquid_pairs(top_n=80):
    # Obtener símbolos líquidos
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
        future_to_symbol = {executor.submit(analyze_single_symbol, sym): sym for sym in sorted_symbols}
        for future in as_completed(future_to_symbol):
            res = future.result()
            if res:
                raw_results.append(res)

    # Filtrado y enriquecimiento microestructural concurrente
    with ThreadPoolExecutor(max_workers=8) as ex:
        enriched_results = list(ex.map(enrich_candidate_microstructure, raw_results))

    # Filtrar solo candidatos calificados (>= 55% de confianza)
    qualified = [c for c in enriched_results if c['confidence'] >= 55]
    qualified.sort(key=lambda x: x['confidence'], reverse=True)
    return qualified

if __name__ == "__main__":
    candidates = scan_all_liquid_pairs(80)
    print(f"Total candidatos calificados con microestructura: {len(candidates)}\n")
    for c in candidates:
        m = c.get('micro', {})
        print(f"• {c['tier']} | {c['symbol']} ({c['direction']}) -> {c['confidence']}%")
        print(f"  Entrada: {c['price']} | SL: {c['sl']:.4f} (-{c['risk_pct']}%) | TP1: {c['tp1']:.4f} | TP2: {c['tp2']:.4f}")
        print(f"  Flujo: Taker={m.get('taker_ratio', 1.0):.2f} | OI={m.get('oi_change_pct', 0.0):+.2f}% | Régimen={m.get('regime', 'N/A')}")
        print(f"  Factores: {', '.join(c['reasons'])}\n")
