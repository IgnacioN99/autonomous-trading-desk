#!/usr/bin/env python3
"""
intraday_radar.py - Escáner Intradiario de Alta Confluencia para Binance Futuros.
Especializado en timeframe de 15m y 5m para cuentas de capital acotado y rotación en el mismo día.
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
        print(f"Error consultando tickers 24h: {e}", file=sys.stderr)
        return []

    # Obtener exchangeInfo para filtrar estrictamente criptos puras (underlyingType == 'COIN')
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

    # Ordenar por volumen en USDT descendente
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

    # Candle range y mechas
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

    # Volumen relativo frente a la media de 20 velas
    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else candle_vol
    vol_ratio = (candle_vol / avg_vol) if avg_vol > 0 else 1.0

    # Distancia a EMA 20 en %
    dist_to_ema20_pct = ((ema20 - current_price) / current_price) * 100

    # 24h High/Low de las últimas 50 velas 15m (~12.5 horas)
    recent_high = max(highs)
    recent_low = min(lows)

    score_long = 0
    score_short = 0
    long_reasons = []
    short_reasons = []

    # Criterio 1: RSI Extremo (Mean Reversion)
    if rsi_15m < 28:
        score_long += 35
        long_reasons.append(f"RSI 15m sobreventa extrema ({rsi_15m:.1f})")
    elif rsi_15m < 35:
        score_long += 20
        long_reasons.append(f"RSI 15m sobreventa ({rsi_15m:.1f})")
    elif rsi_15m > 72:
        score_short += 35
        short_reasons.append(f"RSI 15m sobrecompra extrema ({rsi_15m:.1f})")
    elif rsi_15m > 65:
        score_short += 20
        short_reasons.append(f"RSI 15m sobrecompra ({rsi_15m:.1f})")

    # Criterio 2: Mechas de absorción institucional (NotebookLM principle)
    if lower_wick_ratio >= 40:
        score_long += 30
        long_reasons.append(f"Mecha absorción compradora ({lower_wick_ratio:.0f}% de la vela)")
    elif lower_wick_ratio >= 25:
        score_long += 15
        long_reasons.append(f"Rechazo en mínimos ({lower_wick_ratio:.0f}% mecha)")

    if upper_wick_ratio >= 40:
        score_short += 30
        short_reasons.append(f"Mecha absorción vendedora ({upper_wick_ratio:.0f}% de la vela)")
    elif upper_wick_ratio >= 25:
        score_short += 15
        short_reasons.append(f"Rechazo en máximos ({upper_wick_ratio:.0f}% mecha)")

    # Criterio 3: Volumen clímax
    if vol_ratio >= 1.8:
        score_long += 20 if score_long > score_short else 0
        score_short += 20 if score_short > score_long else 0
        reason_txt = f"Volumen clímax {vol_ratio:.1f}x la media"
        if score_long >= score_short:
            long_reasons.append(reason_txt)
        else:
            short_reasons.append(reason_txt)
    elif vol_ratio >= 1.3:
        score_long += 10 if score_long > score_short else 0
        score_short += 10 if score_short > score_long else 0

    # Criterio 4: Barrido de mínimos/máximos recientes
    if current_price <= recent_low * 1.008:
        score_long += 15
        long_reasons.append("Zona de barrido de mínimos de sesión")
    if current_price >= recent_high * 0.992:
        score_short += 15
        short_reasons.append("Zona de barrido de máximos de sesión")

    atr = calculate_atr(highs, lows, closes, period=14)

    # Decidir dirección
    if score_long >= 45 and score_long > score_short:
        direction = "LONG"
        confluence_score = min(score_long, 98)
        reasons = long_reasons
        entry = current_price
        trigger_entry = candle_high * 1.0005 # Gatillo: superar el máximo de la vela de absorción
        sl = candle_low - (1.3 * atr) # Colchón técnico ATR anti-barrido
        risk_pct = ((entry - sl) / entry) * 100
        # Asegurar mínimo de 1.0% de holgura técnica frente a ruido
        if risk_pct < 1.0:
            sl = entry * 0.988
            risk_pct = 1.2

        tp1 = max(ema20, entry * (1 + risk_pct * 1.8 / 100)) # Mínimo 1.8R a EMA 20
        tp2 = entry * (1 + risk_pct * 4.0 / 100) # 4.0R estructural
        rr = (tp2 - entry) / (entry - sl) if (entry - sl) > 0 else 4.0
    elif score_short >= 45 and score_short > score_long:
        direction = "SHORT"
        confluence_score = min(score_short, 98)
        reasons = short_reasons
        entry = current_price
        trigger_entry = candle_low * 0.9995 # Gatillo: perforar el mínimo de la vela de absorción
        sl = candle_high + (1.3 * atr) # Colchón técnico ATR anti-barrido
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
        time.sleep(0.04) # rate limit suave

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Escáner Intradiario de Futuros")
    parser.add_argument("--interval", type=str, default="15m", choices=["5m", "15m", "1h"], help="Timeframe de escaneo")
    parser.add_argument("--top", type=int, default=5, help="Número de oportunidades a devolver")
    parser.add_argument("--format", type=str, default="table", choices=["table", "json"], help="Formato de salida")
    args = parser.parse_args()

    results = scan_market(top_n=args.top, interval=args.interval)

    if args.format == "json":
        print(json.dumps(results, indent=2))
    else:
        print(f"\n⚡ RADAR INTRADIARIO BINANCE FUTUROS (Timeframe: {args.interval}) ⚡")
        print("=" * 80)
        if not results:
            print("No se encontraron oportunidades con confluencia suficiente en este momento.")
        for i, item in enumerate(results, 1):
            tier = "Tier S (🔥 Máxima)" if item["score"] >= 75 else "Tier A (Fuerte)"
            roe_est = round(item["risk_pct"] * item["rr"] * 3, 1) # a 3x
            trigger_str = f"{item['trigger']:.4f}" if item.get("trigger") else f"{item['entry']:.4f}"
            print(f"#{i} | {item['symbol']} - {item['direction']} | Confluencia: {item['score']}% ({tier})")
            print(f"   • Gatillo Confirmación (Next-Candle): {trigger_str} | Precio Mercado: {item['price']}")
            print(f"   • Stop Loss (Buffer 1.3x ATR): {item['sl']:.4f} (-{item['risk_pct']}%)")
            print(f"   • TP1 (EMA 20 / BE): {item['tp1']:.4f} | TP2 (Estructural): {item['tp2']:.4f}")
            print(f"   • Ratio R:R: {item['rr']}:1 | ROE Estimado (3x): +{roe_est}%")
            print(f"   • Factores de Confluencia: {', '.join(item['reasons'])}")
            print("-" * 80)
