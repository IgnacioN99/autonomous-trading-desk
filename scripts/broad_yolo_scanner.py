#!/usr/bin/env python3
"""
broad_yolo_scanner.py - High-Throughput Quantitative YOLO Moonshot Scanner.
Audits 80+ memecoins and hyper-volatile perpetual contracts on Binance Futures.
Enforces Nassim Taleb Barbell Convexity:
- Climax Volume >= 2.0x MA OR Absorption Wick >= 50%
- Asymmetric Convex Sizing (10x-15x leverage, $10 isolated margin)
- TP1 (+75% ROE) and TP2 (+150% ROE)
"""

import sys
import os
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np

BASE_FAPI = "https://fapi.binance.com"

MEME_KEYWORDS = [
    'PEPE', 'DOGE', 'SHIB', 'BONK', 'WIF', 'FLOKI', 'MEME', 'BOME', 'NEIRO', 
    'MOODENG', 'POPCAT', 'GOAT', 'ACT', 'PNUT', 'BRETT', 'MEW', 'CAT', 
    'TURBO', 'BABYDOGE', '1000', 'MOG', 'SLERF', 'SUNDOG', 'MYRO', 'CHEEMS',
    'HIPPO', 'LUCE', 'CHILLGUY', 'FARTCOIN', 'AI16Z', 'GRIFFAIN', 'SPX',
    'TOSHI', 'DEGEN', 'TRUMP', 'MELANIA', 'VIRTUAL', 'SWARMS', 'PENGU', 'PUMP',
    'PONKE', 'ORDI', 'BAN', 'COOKIE', 'MAJOR', 'AIXBT', 'BIO', 'PONS', 'DOGS',
    'NOT', 'HMSTR', 'CATI', 'COW', 'CETUS', 'THE', 'VINE', 'BERA', 'IP', 'JEFF',
    'KAIA', 'SANTOS', 'CHILL', 'TST', 'PIPIN', 'ZEREBRO', 'ARC', 'MON', 'SONIC',
    'XPLUS', 'MARS', 'VELVET', 'RAY', 'DRIFT', 'JUP'
]

EXCLUDE_PATTERNS = ['USDC', 'FDUSD', 'EUR', 'BUSD']

def get_yolo_universe():
    try:
        url = f"{BASE_FAPI}/fapi/v1/ticker/24hr"
        req = urllib.request.Request(url, headers={"User-Agent": "BinanceAgentic/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        
        candidates = set()
        for d in data:
            sym = d.get('symbol', '')
            if not sym.endswith('USDT') or any(ex in sym for ex in EXCLUDE_PATTERNS):
                continue
            
            # Check meme keywords
            is_meme = any(kw in sym for kw in MEME_KEYWORDS)
            
            # Check extreme volatility mover (expanded pool: >= 4.0% change, >= $5M quote volume)
            chg = abs(float(d.get('priceChangePercent', 0)))
            vol = float(d.get('quoteVolume', 0))
            is_mover = (chg >= 4.0 and vol >= 5_000_000)
            
            if is_meme or is_mover:
                candidates.add(sym)
                
        return sorted(list(candidates))
    except Exception as e:
        sys.stderr.write(f"Error fetching ticker list: {e}\n")
        return [
            "1000PEPEUSDT", "DOGEUSDT", "WIFUSDT", "1000BONKUSDT", "1000SHIBUSDT",
            "FLOKIUSDT", "POPCATUSDT", "NEIROUSDT", "PENGUUSDT", "BOMEUSDT", "MOODENGUSDT",
            "BRETTUSDT", "FARTCOINUSDT", "GOATUSDT", "PNUTUSDT", "ACTUSDT", "MEWUSDT"
        ]

def audit_symbol(symbol, interval="15m"):
    url = f"{BASE_FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=40"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BinanceAgentic/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            klines = json.loads(resp.read().decode())
        
        if len(klines) < 25:
            return None
        
        closes = [float(x[4]) for x in klines]
        highs = [float(x[2]) for x in klines]
        lows = [float(x[3]) for x in klines]
        opens = [float(x[1]) for x in klines]
        vols = [float(x[5]) for x in klines]
        
        cur_p = closes[-1]
        c_open, c_high, c_low, c_vol = opens[-1], highs[-1], lows[-1], vols[-1]
        
        total_range = c_high - c_low if (c_high - c_low) > 0 else 1e-8
        lower_wick = (min(c_open, cur_p) - c_low) / total_range * 100
        upper_wick = (c_high - max(c_open, cur_p)) / total_range * 100
        
        # Volume acceleration vs 10-period moving average
        avg_v = sum(vols[-11:-1]) / 10 if len(vols) >= 11 else c_vol
        vol_ratio = c_vol / avg_v if avg_v > 0 else 1.0
        
        # 14-period RSI
        diffs = np.diff(closes)
        gains = np.where(diffs > 0, diffs, 0)
        losses = np.where(diffs < 0, -diffs, 0)
        avg_gain = np.mean(gains[-14:]) if len(gains) >= 14 else 1e-8
        avg_loss = np.mean(losses[-14:]) if len(losses) >= 14 else 1e-8
        rs = avg_gain / (avg_loss + 1e-8)
        rsi = 100 - (100 / (1 + rs))
        
        # ATR calculation
        trs = [
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            for i in range(1, len(closes))
        ]
        atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else (c_high - c_low)
        atr_pct = (atr / cur_p) * 100
        
        # 1. LONG Candidate Evaluation
        pass_long = (vol_ratio >= 1.8 or lower_wick >= 48.0) and rsi <= 62.0
        score_long = lower_wick * 0.45 + (vol_ratio * 18.0) + (max(0, 50 - rsi) * 0.9)
        
        # 2. SHORT Candidate Evaluation
        pass_short = (vol_ratio >= 1.8 or upper_wick >= 48.0) and rsi >= 45.0
        score_short = upper_wick * 0.45 + (vol_ratio * 18.0) + (max(0, rsi - 50) * 0.9)
        
        return {
            "symbol": symbol,
            "price": cur_p,
            "rsi": round(rsi, 1),
            "vol_ratio": round(vol_ratio, 2),
            "lower_wick": round(lower_wick, 1),
            "upper_wick": round(upper_wick, 1),
            "atr_pct": round(atr_pct, 2),
            "score_long": round(score_long, 1),
            "score_short": round(score_short, 1),
            "pass_long": pass_long,
            "pass_short": pass_short,
            "atr": atr,
            "high": c_high,
            "low": c_low
        }
    except Exception:
        return None

def main():
    universe = get_yolo_universe()
    sys.stdout.write(f"🔬 SCANNING EXPANDED YOLO UNIVERSE: {len(universe)} CONTRACTS (Binance Futures)\n")
    sys.stdout.write("Hardened Filters: Climax Vol >= 1.8x OR Absorption Wick >= 48% | Leverage: 10x-15x Isolated\n")
    sys.stdout.write("=" * 80 + "\n")
    
    results = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(audit_symbol, sym): sym for sym in universe}
        for f in as_completed(futures):
            res = f.result()
            if res:
                results.append(res)
    
    # Filter qualified longs
    qual_longs = [r for r in results if r['pass_long']]
    qual_longs.sort(key=lambda x: x['score_long'], reverse=True)
    
    # Filter qualified shorts
    qual_shorts = [r for r in results if r['pass_short']]
    qual_shorts.sort(key=lambda x: x['score_short'], reverse=True)
    
    print("\n🚀 [TOP QUALIFIED YOLO LONG MOONSHOTS]:")
    if not qual_longs:
        print("  🚫 No long candidates passed the hardened filters (capital preserved).")
    else:
        for r in qual_longs[:5]:
            cur_p = r['price']
            sl = max(cur_p * 0.945, r['low'] - (1.2 * r['atr']))
            risk_pct = (cur_p - sl) / cur_p * 100
            if risk_pct < 2.0:
                sl = cur_p * 0.978
                risk_pct = 2.2
            tp1 = cur_p * (1 + risk_pct * 2.2 / 100) # ~+75% ROE at 15x
            tp2 = cur_p * (1 + risk_pct * 4.5 / 100) # ~+150% ROE at 15x
            roe_tp1 = round(risk_pct * 2.2 * 15, 1)
            roe_tp2 = round(risk_pct * 4.5 * 15, 1)
            
            print(f"• {r['symbol']} (LONG 15x) -> Score: {r['score_long']} | Price: {cur_p}")
            print(f"  Microstructure: Vol Ratio = {r['vol_ratio']}x | Lower Wick = {r['lower_wick']}% | RSI = {r['rsi']}")
            print(f"  Levels: SL = {sl:.6f} (-{risk_pct:.2f}%) | TP1 (+{roe_tp1}% ROE) = {tp1:.6f} | TP2 (+{roe_tp2}% ROE) = {tp2:.6f}")
    
    print("\n🔻 [TOP QUALIFIED YOLO SHORT MOONSHOTS / HEDGES]:")
    if not qual_shorts:
        print("  🚫 No short candidates passed the hardened filters.")
    else:
        for r in qual_shorts[:5]:
            cur_p = r['price']
            sl = min(cur_p * 1.055, r['high'] + (1.2 * r['atr']))
            risk_pct = (sl - cur_p) / cur_p * 100
            if risk_pct < 2.0:
                sl = cur_p * 1.022
                risk_pct = 2.2
            tp1 = cur_p * (1 - risk_pct * 2.2 / 100)
            tp2 = cur_p * (1 - risk_pct * 4.5 / 100)
            roe_tp1 = round(risk_pct * 2.2 * 15, 1)
            roe_tp2 = round(risk_pct * 4.5 * 15, 1)
            
            print(f"• {r['symbol']} (SHORT 15x) -> Score: {r['score_short']} | Price: {cur_p}")
            print(f"  Microstructure: Vol Ratio = {r['vol_ratio']}x | Upper Wick = {r['upper_wick']}% | RSI = {r['rsi']}")
            print(f"  Levels: SL = {sl:.6f} (-{risk_pct:.2f}%) | TP1 (+{roe_tp1}% ROE) = {tp1:.6f} | TP2 (+{roe_tp2}% ROE) = {tp2:.6f}")
            
    # Also show Top Volume Surge in the whole pool
    results.sort(key=lambda x: x['vol_ratio'], reverse=True)
    print("\n⚡ [TOP MEME / HIGH-BETA VOLUME SURGES (Right Now)]:")
    for r in results[:8]:
        print(f"• {r['symbol']}: Vol Ratio = {r['vol_ratio']}x | 15m RSI = {r['rsi']} | ATR = {r['atr_pct']}% | Price = {r['price']}")

if __name__ == "__main__":
    main()
