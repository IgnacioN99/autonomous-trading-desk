#!/usr/bin/env python3
"""
broad_yolo_scanner.py - High-Throughput Quantitative YOLO Moonshot Scanner.
Audits 80+ memecoins and hyper-volatile perpetual contracts on Binance Futures.
Enforces Nassim Taleb Barbell Convexity:
- Climax Volume >= 2.0x MA OR Absorption Wick >= 50%, never with dry volume < 1.0x (hardened filters, see AGENTS.md)
- Asymmetric Convex Sizing: isolated margin and leverage from config/user_profile.json
  (yolo_margin_fixed / yolo_equity_pct and leverage_yolo, capped at leverage_ceiling)
- TP1 (+2.2R) and TP2 (+4.5R) to preserve right-tail convexity
- Executor gates (issue #64): every long/short row carries `gate_ok` / `gate_failures` from yolo_gate_failures()
  (level coherence, 0.35% TP1 friction floor, 35%-of-margin YOLO loss cap, TP1 >= 1.8R / TP2 >= 3:1), checked
  on 6-significant-digit prices with an adverse rounding margin. Gates run on every qualified row before the
  --top cut; gate-passing rows are listed first (score order within each group), failing rows are flagged; the
  `recommendation` is the top gate-passing long (or null, slot EMPTY).

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
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.gate_limits import MIN_TP1_DISTANCE, YOLO_MAX_LOSS_MARGIN_FRACTION

BASE_FAPI = "https://fapi.binance.com"
SUPPORTED_INTERVALS = ("5m", "15m", "1h")

# Hardened Barbell filters (AGENTS.md: climax volume >= 2.0x OR absorption wick >= 50%).
MIN_VOL_RATIO = 2.0
MIN_WICK_PCT = 50.0
# Volume floor on every path: a wick printed on dry volume (< 1.0x) is thin-book noise, not absorption.
MIN_VOL_FLOOR = 1.0
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

# Executor gates applied to every row (yolo_gate_failures). The loss cap (GATE 2, YOLO) and the friction floor
# (GATE 3) come from scripts/utils/gate_limits.py, the module execute_futures_trade.py enforces.
# Desk R:R floors measured from the trigger entry: TP1 must reach +1.8R (fees + free trade) and TP2 >= 3:1.
YOLO_MIN_R_TP1 = 1.8
YOLO_MIN_RR_TP2 = 3.0
YOLO_SIG_DIGITS = 6  # levels forwarded to the evaluator are rounded to 6 significant digits (token budget)
# Adverse margin for the checks on rounded prices: SL moved away from the trigger and TP1/TP2 moved toward it by
# 0.05% of their price. Covers the 6-significant-digit rounding (<= 0.005%) plus the executor's ROUND_DOWN snap to
# tickSize, which on memecoin books with few price digits can move a level by a few hundredths of a percent.
YOLO_ROUNDING_MARGIN = 0.0005

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
        pass_long = (vol_ratio >= MIN_VOL_FLOOR and (vol_ratio >= MIN_VOL_RATIO or lower_wick >= MIN_WICK_PCT)
                     and rsi <= LONG_MAX_RSI and score_long >= MIN_SCORE)

        # 2. SHORT Candidate Evaluation (hedge side: climax volume or seller absorption wick)
        score_short = upper_wick * 0.45 + (vol_ratio * 18.0) + (max(0, rsi - 50) * 0.9)
        pass_short = (vol_ratio >= MIN_VOL_FLOOR and (vol_ratio >= MIN_VOL_RATIO or upper_wick >= MIN_WICK_PCT)
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

def _sig(x, digits=YOLO_SIG_DIGITS):
    """Plain float rounded to `digits` significant digits."""
    return float(f"{float(x):.{digits}g}")

def yolo_gate_failures(row):
    """Names of the executor gates a LONG or SHORT build_levels() row fails when entered at its trigger
    (empty list = passes). Pure function; the Barbell volume filter is not part of it.

    Checks run on trigger/SL/TP1/TP2 rounded to YOLO_SIG_DIGITS significant digits (the values forwarded to the
    evaluator):
    - coherence: finite levels, LONG 0 < sl < price and sl < trigger < tp1 <= tp2 (SHORT mirrored:
      price < sl and 0 < tp2 <= tp1 < trigger < sl), leverage >= 1 and margin > 0. Rounded values, no margin.
    - friction (GATE 3), loss_cap (GATE 2 YOLO), rr_tp1 / rr_tp2 (desk R:R floors): worst case, with the SL moved
      away from the trigger and TP1/TP2 moved toward it by YOLO_ROUNDING_MARGIN of their price.
    """
    try:
        direction = str(row["direction"]).upper()
        price = float(row["price"])
        trigger, sl = _sig(row["trigger"]), _sig(row["sl"])
        tp1, tp2 = _sig(row["tp1"]), _sig(row["tp2"])
        leverage, margin = int(row["leverage"]), float(row["margin_usdt"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return ["coherence"]

    finite = all(math.isfinite(v) for v in (price, trigger, sl, tp1, tp2, margin))
    if direction == "LONG":
        coherent = finite and 0.0 < sl < price and sl < trigger < tp1 <= tp2
    elif direction == "SHORT":
        coherent = finite and 0.0 < price < sl and 0.0 < tp2 <= tp1 < trigger < sl
    else:
        coherent = False
    if not (coherent and leverage >= 1 and margin > 0.0):
        return ["coherence"]

    m = YOLO_ROUNDING_MARGIN
    if direction == "LONG":
        risk = trigger - sl * (1 - m)
        reward_tp1 = tp1 * (1 - m) - trigger
        reward_tp2 = tp2 * (1 - m) - trigger
    else:
        # SHORT rows are hedge-only (never the recommendation). The executor rounds the trigger DOWN to tickSize
        # before its gates, which moves a SHORT entry adversely (closer to TP, farther from SL); the SL (and
        # TP) margin covers that as long as one tick is <= YOLO_ROUNDING_MARGIN (0.05%) of the price.
        risk = sl * (1 + m) - trigger
        reward_tp1 = trigger - tp1 * (1 + m)
        reward_tp2 = trigger - tp2 * (1 + m)

    failures = []
    if reward_tp1 / trigger < MIN_TP1_DISTANCE:
        failures.append("friction")
    if risk / trigger * leverage > YOLO_MAX_LOSS_MARGIN_FRACTION:
        failures.append("loss_cap")
    if reward_tp1 / risk < YOLO_MIN_R_TP1:
        failures.append("rr_tp1")
    if reward_tp2 / risk < YOLO_MIN_RR_TP2:
        failures.append("rr_tp2")
    return failures

def _flag_gates(row):
    """Adds gate_ok / gate_failures to a build_levels() row (flagged, never dropped)."""
    failures = yolo_gate_failures(row)
    row["gate_ok"] = not failures
    row["gate_failures"] = failures
    return row

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
    # Gates run on every qualified row before the top-N cut, so a gate-passing row ranked below N is not lost
    # (wide-wick top scorers are the most likely to fail the loss cap at high leverage). Gate-passing rows come
    # first; the sort is stable, so score order is kept within each group. Failing rows still fill the list when
    # fewer than `top` rows pass.
    longs = sorted((_flag_gates(build_levels(r, "LONG", sizing)) for r in qual_longs),
                   key=lambda c: not c["gate_ok"])[:top]
    shorts = sorted((_flag_gates(build_levels(r, "SHORT", sizing)) for r in qual_shorts),
                    key=lambda c: not c["gate_ok"])[:top]
    surges = sorted(results, key=lambda x: x['vol_ratio'], reverse=True)[:8]

    # Only a long the executor would accept at its trigger can fill the slot (issue #64).
    recommendation = next((c for c in longs if c["gate_ok"]), None)
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
            "min_vol_floor": MIN_VOL_FLOOR,
            "long_max_rsi": LONG_MAX_RSI,
            "short_min_rsi": SHORT_MIN_RSI,
            "min_score": MIN_SCORE,
            "min_tp1_distance": MIN_TP1_DISTANCE,
            "max_loss_margin_fraction": YOLO_MAX_LOSS_MARGIN_FRACTION,
            "min_r_tp1": YOLO_MIN_R_TP1,
            "min_rr_tp2": YOLO_MIN_RR_TP2,
            "rounding_margin": YOLO_ROUNDING_MARGIN,
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
    print(f"Hardened Filters: Climax Vol >= {MIN_VOL_RATIO}x OR Absorption Wick >= {MIN_WICK_PCT:.0f}% (Vol >= {MIN_VOL_FLOOR}x) | "
          f"Sizing (profile): {s['margin_usdt']:.2f} USDT isolated at {lev}x (ceiling {s['leverage_ceiling']}x) | "
          f"YOLO slot {'ENABLED' if s['yolo_slot_enabled'] else 'DISABLED'}")
    print("=" * 80)

    def _print(cands, wick_key, wick_label):
        for c in cands:
            print(f"• {c['symbol']} ({c['direction']} {lev}x) -> Score: {c['score']} | Price: {c['price']} | Trigger: {c['trigger']:.6f}")
            print(f"  Microstructure: Vol Ratio = {c['vol_ratio']}x | {wick_label} = {c[wick_key]}% | RSI = {c['rsi']}")
            print(f"  Levels: SL = {c['sl']:.6f} (-{c['risk_pct']:.2f}% | max loss -{c['max_loss_usdt']:.2f} USDT) | "
                  f"TP1 (+{c['roe_tp1_pct']}% ROE) = {c['tp1']:.6f} | TP2 (+{c['roe_tp2_pct']}% ROE) = {c['tp2']:.6f}")
            if not c.get("gate_ok", True):
                print(f"  GATE FAIL: {', '.join(c.get('gate_failures') or [])} (the executor would reject it; not a slot candidate)")

    print("\n🚀 [TOP QUALIFIED YOLO LONG MOONSHOTS]:")
    if not p["longs"]:
        print("  🚫 No long candidates passed the hardened filters. The YOLO slot remains empty (capital preserved).")
    else:
        _print(p["longs"], "lower_wick", "Lower Wick")
        if p["recommendation"] is None:
            print("  🚫 No long candidate passes the executor gates. The YOLO slot remains empty (capital preserved).")

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
