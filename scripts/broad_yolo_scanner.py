#!/usr/bin/env python3
"""
broad_yolo_scanner.py - High-Throughput Quantitative YOLO Moonshot Scanner.
Audits 80+ memecoins and hyper-volatile perpetual contracts on Binance Futures.
Enforces Nassim Taleb Barbell Convexity:
- Climax Volume >= 2.0x MA OR Absorption Wick >= 50% (hardened filters, see AGENTS.md)
- Asymmetric Convex Sizing: isolated margin and leverage from config/user_profile.json
  (yolo_margin_fixed / yolo_equity_pct and leverage_yolo, capped at leverage_ceiling)
- TP1 (+2.2R) and TP2 (+4.5R) to preserve right-tail convexity

Read-only: uses public Binance Futures market data and never places orders.

CLI:
    python3 scripts/broad_yolo_scanner.py [--json] [--top N] [--interval 15m|5m|1h] [--env prod|testnet]
Exit codes: 0 ok, 1 data/API error, 2 bad usage.
"""

import sys
import os
import json
import time
import argparse
import contextlib
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_FAPI = "https://fapi.binance.com"
SUPPORTED_INTERVALS = ("5m", "15m", "1h")

# Hardened Barbell filters (AGENTS.md: climax volume >= 2.0x OR absorption wick >= 50%).
MIN_VOL_RATIO = 2.0
MIN_WICK_PCT = 50.0
LONG_MAX_RSI = 65.0
SHORT_MIN_RSI = 45.0
MIN_SCORE = 50.0

# Level construction (risk distance clamped to [MIN_RISK_PCT, MAX_RISK_PCT] of price).
SL_ATR_MULT = 1.2
MIN_RISK_PCT = 2.2
MAX_RISK_PCT = 5.5
TP1_R = 2.2
TP2_R = 4.5
TRIGGER_BUFFER = 0.0008  # next-candle confirmation trigger beyond the signal candle extreme

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

# Core memecoin basket, used as the fallback universe if the 24h ticker endpoint fails.
CORE_MEMES = [
    "1000PEPEUSDT", "DOGEUSDT", "WIFUSDT", "1000BONKUSDT", "1000SHIBUSDT",
    "FLOKIUSDT", "POPCATUSDT", "NEIROUSDT", "PENGUUSDT", "BOMEUSDT", "MOODENGUSDT",
    "BRETTUSDT", "FARTCOINUSDT", "GOATUSDT", "PNUTUSDT", "ACTUSDT", "MEWUSDT"
]

def get_yolo_universe():
    """Returns (symbols, from_live_ticker). Memes by keyword plus extreme movers (>= 4% and >= $5M volume)."""
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

        return sorted(list(candidates)), True
    except Exception as e:
        sys.stderr.write(f"Error fetching ticker list: {e}\n")
        return list(CORE_MEMES), False

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
        rsi = float(100 - (100 / (1 + rs)))

        # ATR calculation
        trs = [
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            for i in range(1, len(closes))
        ]
        atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else (c_high - c_low)
        atr_pct = (atr / cur_p) * 100

        # 1. LONG Candidate Evaluation (hardened: climax volume or buyer absorption wick, RSI not overheated)
        score_long = lower_wick * 0.45 + (vol_ratio * 18.0) + (max(0, 50 - rsi) * 0.9)
        pass_long = ((vol_ratio >= MIN_VOL_RATIO or lower_wick >= MIN_WICK_PCT)
                     and rsi <= LONG_MAX_RSI and score_long >= MIN_SCORE)

        # 2. SHORT Candidate Evaluation (hedge side: climax volume or seller absorption wick)
        score_short = upper_wick * 0.45 + (vol_ratio * 18.0) + (max(0, rsi - 50) * 0.9)
        pass_short = ((vol_ratio >= MIN_VOL_RATIO or upper_wick >= MIN_WICK_PCT)
                      and rsi >= SHORT_MIN_RSI and score_short >= MIN_SCORE)

        return {
            "symbol": symbol,
            "price": cur_p,
            "rsi": round(rsi, 1),
            "vol_ratio": round(vol_ratio, 2),
            "lower_wick": round(lower_wick, 1),
            "upper_wick": round(upper_wick, 1),
            "atr_pct": round(atr_pct, 2),
            "score_long": round(float(score_long), 1),
            "score_short": round(float(score_short), 1),
            "pass_long": bool(pass_long),
            "pass_short": bool(pass_short),
            "atr": atr,
            "high": c_high,
            "low": c_low
        }
    except Exception:
        return None

def resolve_yolo_sizing(target_env):
    """YOLO margin/leverage from the user profile (never hardcoded): margin via get_yolo_margin(),
    leverage = leverage_yolo capped at the desk leverage ceiling."""
    import user_profile as up
    prof = up.load_user_profile()
    ceiling = up.get_leverage_ceiling(prof)
    try:
        leverage = int(float(prof.get("leverage_yolo", ceiling)))
    except (TypeError, ValueError):
        leverage = ceiling
    leverage = max(1, min(leverage, ceiling))
    margin = float(up.get_yolo_margin(target_env=target_env))
    return {
        "margin_usdt": round(margin, 2),
        "leverage": leverage,
        "leverage_ceiling": ceiling,
        "margin_mode": "ISOLATED",
        "yolo_slot_enabled": bool(prof.get("yolo_slot_enabled", False)),
    }

def build_levels(r, direction, sizing):
    """Trigger, SL, TP1/TP2 and bounded-capital sizing for a qualified candidate.
    The entry is the breakout trigger, so the SL distance (risk_pct), TP1/TP2, ROE, qty and max loss are all
    measured from the trigger, not from the current price (issue #52)."""
    cur_p = r['price']
    leverage = sizing["leverage"]
    margin = sizing["margin_usdt"]
    if direction == "LONG":
        trigger = r['high'] * (1 + TRIGGER_BUFFER)
        sl = max(trigger * (1 - MAX_RISK_PCT / 100), r['low'] - (SL_ATR_MULT * r['atr']))
        risk_pct = (trigger - sl) / trigger * 100
        if risk_pct < MIN_RISK_PCT:
            sl = trigger * (1 - MIN_RISK_PCT / 100)
            risk_pct = MIN_RISK_PCT
        tp1 = trigger * (1 + risk_pct * TP1_R / 100)
        tp2 = trigger * (1 + risk_pct * TP2_R / 100)
        score = r['score_long']
    else:
        trigger = r['low'] * (1 - TRIGGER_BUFFER)
        sl = min(trigger * (1 + MAX_RISK_PCT / 100), r['high'] + (SL_ATR_MULT * r['atr']))
        risk_pct = (sl - trigger) / trigger * 100
        if risk_pct < MIN_RISK_PCT:
            sl = trigger * (1 + MIN_RISK_PCT / 100)
            risk_pct = MIN_RISK_PCT
        tp1 = trigger * (1 - risk_pct * TP1_R / 100)
        tp2 = trigger * (1 - risk_pct * TP2_R / 100)
        score = r['score_short']

    notional = margin * leverage
    roe_tp1 = round(risk_pct * TP1_R * leverage, 1)
    roe_tp2 = round(risk_pct * TP2_R * leverage, 1)
    return {
        "symbol": r['symbol'],
        "direction": direction,
        "score": score,
        "price": cur_p,
        "trigger": trigger,
        "sl": sl,
        "risk_pct": round(risk_pct, 2),
        "tp1": tp1,
        "tp2": tp2,
        "roe_tp1_pct": roe_tp1,
        "roe_tp2_pct": roe_tp2,
        "leverage": leverage,
        "margin_usdt": margin,
        "notional_usdt": round(notional, 2),
        "qty": notional / trigger if trigger > 0 else 0.0,
        "max_loss_usdt": round(notional * risk_pct / 100, 2),
        "gain_tp1_usdt": round(margin * roe_tp1 / 100, 2),
        "gain_tp2_usdt": round(margin * roe_tp2 / 100, 2),
        "rsi": r['rsi'],
        "vol_ratio": r['vol_ratio'],
        "lower_wick": r['lower_wick'],
        "upper_wick": r['upper_wick'],
        "atr_pct": r['atr_pct'],
    }

def scan_yolo(target_env, interval="15m", top=5):
    """Runs the full YOLO scan and returns the JSON-ready payload (raises on unrecoverable data errors)."""
    sizing = resolve_yolo_sizing(target_env)
    universe, live = get_yolo_universe()

    results = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(audit_symbol, sym, interval): sym for sym in universe}
        for f in as_completed(futures):
            res = f.result()
            if res:
                results.append(res)
    if not results:
        raise RuntimeError(f"No kline data could be fetched for the {len(universe)}-symbol YOLO universe.")

    qual_longs = sorted([r for r in results if r['pass_long']], key=lambda x: x['score_long'], reverse=True)
    qual_shorts = sorted([r for r in results if r['pass_short']], key=lambda x: x['score_short'], reverse=True)
    longs = [build_levels(r, "LONG", sizing) for r in qual_longs[:top]]
    shorts = [build_levels(r, "SHORT", sizing) for r in qual_shorts[:top]]
    surges = sorted(results, key=lambda x: x['vol_ratio'], reverse=True)[:8]

    recommendation = longs[0] if longs else None
    if recommendation is None:
        slot_status = "EMPTY"
    elif not sizing["yolo_slot_enabled"]:
        slot_status = "CANDIDATE_SLOT_DISABLED"
    else:
        slot_status = "CANDIDATE"

    return {
        "status": "ok",
        "command": "yolo",
        "env": target_env,
        "interval": interval,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "universe_size": len(universe),
        "universe_from_live_ticker": live,
        "scanned": len(results),
        "filters": {
            "min_vol_ratio": MIN_VOL_RATIO,
            "min_wick_pct": MIN_WICK_PCT,
            "long_max_rsi": LONG_MAX_RSI,
            "short_min_rsi": SHORT_MIN_RSI,
            "min_score": MIN_SCORE,
        },
        "sizing": sizing,
        "slot_status": slot_status,
        "recommendation": recommendation,
        "longs": longs,
        "shorts": shorts,
        "volume_surges": [
            {"symbol": r['symbol'], "vol_ratio": r['vol_ratio'], "rsi": r['rsi'],
             "atr_pct": r['atr_pct'], "price": r['price']}
            for r in surges
        ],
    }

def _json_default(obj):
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

def print_text_report(p):
    s = p["sizing"]
    lev = s["leverage"]
    print(f"🔬 SCANNING EXPANDED YOLO UNIVERSE: {p['universe_size']} CONTRACTS (Binance Futures, {p['interval']}, env={p['env']})")
    print(f"Hardened Filters: Climax Vol >= {MIN_VOL_RATIO}x OR Absorption Wick >= {MIN_WICK_PCT:.0f}% | "
          f"Sizing (profile): {s['margin_usdt']:.2f} USDT isolated at {lev}x (ceiling {s['leverage_ceiling']}x) | "
          f"YOLO slot {'ENABLED' if s['yolo_slot_enabled'] else 'DISABLED'}")
    print("=" * 80)

    def _print(cands, wick_key, wick_label):
        for c in cands:
            print(f"• {c['symbol']} ({c['direction']} {lev}x) -> Score: {c['score']} | Price: {c['price']} | Trigger: {c['trigger']:.6f}")
            print(f"  Microstructure: Vol Ratio = {c['vol_ratio']}x | {wick_label} = {c[wick_key]}% | RSI = {c['rsi']}")
            print(f"  Levels: SL = {c['sl']:.6f} (-{c['risk_pct']:.2f}% | max loss -{c['max_loss_usdt']:.2f} USDT) | "
                  f"TP1 (+{c['roe_tp1_pct']}% ROE) = {c['tp1']:.6f} | TP2 (+{c['roe_tp2_pct']}% ROE) = {c['tp2']:.6f}")

    print("\n🚀 [TOP QUALIFIED YOLO LONG MOONSHOTS]:")
    if not p["longs"]:
        print("  🚫 No long candidates passed the hardened filters. The YOLO slot remains empty (capital preserved).")
    else:
        _print(p["longs"], "lower_wick", "Lower Wick")

    print("\n🔻 [TOP QUALIFIED YOLO SHORT MOONSHOTS / HEDGES]:")
    if not p["shorts"]:
        print("  🚫 No short candidates passed the hardened filters.")
    else:
        _print(p["shorts"], "upper_wick", "Upper Wick")

    print("\n⚡ [TOP MEME / HIGH-BETA VOLUME SURGES (Right Now)]:")
    for r in p["volume_surges"]:
        print(f"• {r['symbol']}: Vol Ratio = {r['vol_ratio']}x | RSI = {r['rsi']} | ATR = {r['atr_pct']}% | Price = {r['price']}")
    print("\n⚠️ PROTECTION RULE: Do not move SL to Break-Even before TP1 fills; let the right tail run.")

def main(argv=None):
    parser = argparse.ArgumentParser(description="YOLO moonshot scanner (read-only, never places orders)")
    parser.add_argument("--json", action="store_true", help="Print a single JSON document on stdout (diagnostics go to stderr)")
    parser.add_argument("--top", type=int, default=5, help="Max qualified candidates per side (default 5)")
    parser.add_argument("--interval", default="15m", choices=SUPPORTED_INTERVALS, help="Candle interval (default 15m)")
    parser.add_argument("--env", default=None, help="prod|testnet (resolved via env_resolver; sets the equity used for YOLO margin)")
    args = parser.parse_args(argv)

    if args.top < 1:
        parser.print_usage(sys.stderr)
        sys.stderr.write("error: --top must be >= 1\n")
        return 2
    try:
        from utils.env_resolver import resolve_env
        env = resolve_env(args.env)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    real_stdout = sys.stdout
    try:
        with contextlib.redirect_stdout(sys.stderr if args.json else real_stdout):
            payload = scan_yolo(env, interval=args.interval, top=args.top)
    except Exception as e:
        err = {"status": "error", "command": "yolo", "env": env, "error": f"{type(e).__name__}: {e}"}
        if args.json:
            emit_json(err, real_stdout)
        else:
            sys.stderr.write(f"YOLO scan failed: {err['error']}\n")
        return 1

    if args.json:
        emit_json(payload, real_stdout)
    else:
        print_text_report(payload)
    return 0

if __name__ == "__main__":
    sys.exit(main())
