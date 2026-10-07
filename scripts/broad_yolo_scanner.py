#!/usr/bin/env python3
"""
broad_yolo_scanner.py - High-Throughput Quantitative YOLO Moonshot Scanner.
Audits the memecoin perpetuals of Binance Futures. Universe (issue #80), from one /fapi/v1/exchangeInfo request:
status TRADING, quoteAsset USDT, contractType PERPETUAL, underlyingType COIN (no TradFi equity/ETF/commodity
perps) AND (Binance's "Meme" underlyingSubType tag OR the base asset, without a 1000/1000000/1M multiplier prefix,
exactly in CORE_MEME_BASES). No substring matching and no 24h movers. A non-rate-limit exchangeInfo failure falls
back to CORE_MEMES (the allowlist symbols only). A symbol that qualifies both LONG and SHORT is dropped from both
lists and reported in `ambiguous_symbols`.
Wicks and volume ratio come from the last CLOSED candle (klines[-2], issue #85); the trigger and SL extremes span
that candle and the forming one.
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
- Spread- and ATR-aware trigger buffer (issue #66): the breakout trigger sits `buffer` beyond the signal candle
  extreme (LONG high x (1 + buffer), SHORT low x (1 - buffer)) with
  buffer = min(TRIGGER_BUFFER_MAX, max(TRIGGER_BUFFER, TRIGGER_SPREAD_MULT x spread, TRIGGER_ATR_FRAC x ATR%)),
  i.e. floor 0.08%, cap 0.5%. The spread is (ask - bid) / mid from one bookTicker request; a missing spread or ATR
  term is skipped (the buffer is not a gate). Rows carry `trigger_buffer_pct` and `spread_pct` (null if unknown).
- Gate flags (issues #78, #91.3): the adverse rounding margin is max(YOLO_ROUNDING_MARGIN, tickSize / price) with
  tickSize from exchangeInfo (row `tick_size`); fractional leverage is checked rounded up; a spread above
  TRIGGER_BUFFER_MAX fails the row as `wide_spread`.

Request weight per run (Binance IP limit: 2400 / minute, shared with the 80-pair radar that runs concurrently in
screening_pipeline.py): exchangeInfo 1 + bookTicker without symbol 5 + klines limit=40 1 per symbol (~6 + universe
size). The scan uses YOLO_SCAN_WORKERS concurrent kline requests, reports the highest `X-MBX-USED-WEIGHT-1M`
header seen as `max_used_weight_1m` (null when no header was seen) and warns on stderr at WEIGHT_WARN_THRESHOLD.
On HTTP 429 (rate limit) or 418 (IP auto-ban) it stops issuing requests and raises RateLimitedError (the universe
fetch never falls back to CORE_MEMES on 429/418); with the process-wide guard enabled (utils/rate_limit_guard.py,
CLI and pipeline) the ban honours Retry-After and is persisted for the next runs. An optional cancel_event (set by
the pipeline when its time budget expires) also stops further requests.

Read-only: uses public Binance Futures market data and never places orders. scan_yolo() and everything it calls
must stay side-effect free (see the scan_yolo docstring): the pipeline may hard-exit while a scan is running.

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
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import microstructure_engine as me
from utils.gate_limits import MIN_TP1_DISTANCE, YOLO_MAX_LOSS_MARGIN_FRACTION
from utils import rate_limit_guard

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
TRIGGER_BUFFER = 0.0008  # floor of the next-candle confirmation trigger beyond the signal candle extreme
TRIGGER_SPREAD_MULT = 1.0  # buffer >= 1x the bid/ask spread (as a fraction of mid)
TRIGGER_ATR_FRAC = 0.1  # buffer >= 10% of the ATR (as a fraction of price)
TRIGGER_BUFFER_MAX = 0.005  # buffer cap: 0.5%

# Request budget (issue #66). The scan runs concurrently with the 80-pair radar in screening_pipeline.py.
YOLO_SCAN_WORKERS = 8
WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"
WEIGHT_WARN_THRESHOLD = 1800  # 75% of Binance's 2400 request weight / minute IP limit
RATE_LIMIT_HTTP_CODES = rate_limit_guard.RATE_LIMIT_HTTP_CODES  # 429 = limit broken, 418 = IP auto-banned

# Executor gates applied to every row (yolo_gate_failures). The loss cap (GATE 2, YOLO) and the friction floor
# (GATE 3) come from scripts/utils/gate_limits.py, the module execute_futures_trade.py enforces.
# Desk R:R floors measured from the trigger entry: TP1 must reach +1.8R (fees + free trade) and TP2 >= 3:1.
YOLO_MIN_R_TP1 = 1.8
YOLO_MIN_RR_TP2 = 3.0
YOLO_SIG_DIGITS = 6  # levels forwarded to the evaluator are rounded to 6 significant digits (token budget)
# Adverse margin for the checks on rounded prices: SL moved away from the trigger and TP1/TP2 moved toward it by
# 0.05% of their price. Covers the 6-significant-digit rounding (<= 0.005%) plus the executor's ROUND_DOWN snap to
# tickSize, which on memecoin books with few price digits can move a level by a few hundredths of a percent.
# When exchangeInfo gives the symbol's tickSize, the margin is max(YOLO_ROUNDING_MARGIN, tickSize / price) (issue
# #78). Conservative bias: on a tight stop the fixed 0.05% shift inflates the measured risk (about +50% on a ~0.1%
# stop), so a borderline tight-stop row may be flagged although the executor would accept it.
YOLO_ROUNDING_MARGIN = 0.0005

# Desk memecoins (AGENTS.md Barbell list plus SHIB and FLOKI): exact base assets, matched after stripping a
# leading multiplier prefix (1000PEPE -> PEPE). Symbols Binance tags "Meme" in underlyingSubType also qualify.
CORE_MEME_BASES = frozenset({"PEPE", "WIF", "BONK", "DOGE", "NEIRO", "PENGU", "BOME", "MOODENG", "SHIB", "FLOKI"})
BASE_MULTIPLIER_PREFIXES = ("1000000", "1000", "1M")  # longest first
MEME_SUBTYPE = "Meme"

# Fallback universe when exchangeInfo fails (never on 429/418): the CORE_MEME_BASES perpetuals only.
CORE_MEMES = [
    "1000PEPEUSDT", "DOGEUSDT", "WIFUSDT", "1000BONKUSDT", "1000SHIBUSDT",
    "1000FLOKIUSDT", "NEIROUSDT", "PENGUUSDT", "BOMEUSDT", "MOODENGUSDT",
]

# Process-wide class (utils/rate_limit_guard.py), re-exported under its historical name.
RateLimitedError = rate_limit_guard.RateLimitedError


class ScanCancelledError(RuntimeError):
    """The caller set the scan's cancel_event (e.g. the pipeline's time budget expired)."""


class _ScanGuard:
    """Per-scan request state: rate-limit trip, caller cancellation and the highest used-weight header seen."""

    def __init__(self, cancel_event=None):
        self.cancel_event = cancel_event
        self.rate_limited = threading.Event()
        self.rate_limit_code = None
        self.max_used_weight = None
        self._weight_warned = False
        self._lock = threading.Lock()

    def cancelled(self):
        return self.cancel_event is not None and self.cancel_event.is_set()

    def stopped(self):
        return self.rate_limited.is_set() or rate_limit_guard.is_banned() or self.cancelled()

    def trip(self, code):
        with self._lock:
            if self.rate_limit_code is None:
                self.rate_limit_code = int(code)
        self.rate_limited.set()

    def note_weight(self, resp):
        """Tracks X-MBX-USED-WEIGHT-1M (tolerates missing headers and mocks without .headers)."""
        headers = getattr(resp, "headers", None)
        getter = getattr(headers, "get", None)
        if not callable(getter):
            return
        try:
            raw = getter(WEIGHT_HEADER)
            if isinstance(raw, bytes):
                raw = raw.decode()
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                return
            used = int(str(raw).strip())
        except (TypeError, ValueError, UnicodeDecodeError):
            return
        with self._lock:
            if self.max_used_weight is None or used > self.max_used_weight:
                self.max_used_weight = used
            warn = used >= WEIGHT_WARN_THRESHOLD and not self._weight_warned
            if warn:
                self._weight_warned = True
        if warn:
            sys.stderr.write(f"WARNING: Binance request weight {used}/2400 per minute used by this IP "
                             f"(>= {WEIGHT_WARN_THRESHOLD}) during the YOLO scan.\n")

    def raise_if_stopped(self):
        if self.rate_limited.is_set():
            raise RateLimitedError(f"Binance rate limit (HTTP {self.rate_limit_code})", status=self.rate_limit_code)
        rate_limit_guard.raise_if_banned()  # process-wide ban (enabled guard only)
        if self.cancelled():
            raise ScanCancelledError("YOLO scan cancelled by the caller")


def _get_json(url, timeout, guard=None):
    """GET `url` and decode JSON. HTTP 429/418 trip the guard (and the process-wide guard, with Retry-After) and
    raise RateLimitedError; a stopped guard or an active process-wide ban raises before any request is issued."""
    if guard is not None:
        guard.raise_if_stopped()
    else:
        rate_limit_guard.raise_if_banned()
    req = urllib.request.Request(url, headers={"User-Agent": "BinanceAgentic/1.0"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if guard is not None:
                guard.note_weight(resp)
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in RATE_LIMIT_HTTP_CODES:
            if guard is not None:
                guard.trip(e.code)
            rate_limit_guard.trip(e.code, rate_limit_guard.retry_after_header(e))  # no-op while disabled
            raise RateLimitedError(f"Binance rate limit (HTTP {e.code})", status=e.code) from None
        raise


def _strip_multiplier(base):
    """Base asset without a leading contract multiplier (1000PEPE -> PEPE, 1MBABYDOGE -> BABYDOGE)."""
    for prefix in BASE_MULTIPLIER_PREFIXES:
        if base.startswith(prefix) and len(base) > len(prefix):
            return base[len(prefix):]
    return base


def is_yolo_eligible(info):
    """True for one exchangeInfo symbol row that belongs to the YOLO universe (see the module docstring)."""
    if not isinstance(info, dict):
        return False
    if (info.get("status") != "TRADING" or info.get("quoteAsset") != "USDT"
            or info.get("contractType") != "PERPETUAL" or info.get("underlyingType") != "COIN"):
        return False
    subtypes = info.get("underlyingSubType") or []
    if isinstance(subtypes, str):
        subtypes = [subtypes]
    return MEME_SUBTYPE in subtypes or _strip_multiplier(str(info.get("baseAsset") or "")) in CORE_MEME_BASES


def _tick_size(info):
    """PRICE_FILTER.tickSize of an exchangeInfo symbol row (None when missing or invalid)."""
    for f in info.get("filters") or []:
        if isinstance(f, dict) and f.get("filterType") == "PRICE_FILTER":
            tick = _valid_fraction(f.get("tickSize"))
            return tick if tick else None
    return None


def get_yolo_universe(guard=None):
    """Returns (symbols, from_live_exchange_info, tick_sizes {symbol: tickSize or None}) from ONE exchangeInfo
    request (weight 1). Falls back to (CORE_MEMES, False, {}) on errors, except HTTP 429/418 (RateLimitedError is
    raised: never keep hitting Binance after a rate limit)."""
    try:
        data = _get_json(f"{BASE_FAPI}/fapi/v1/exchangeInfo", 8, guard)
        symbols, ticks = [], {}
        for info in data["symbols"]:
            if is_yolo_eligible(info) and info.get("symbol") not in ticks:
                symbols.append(info["symbol"])
                ticks[info["symbol"]] = _tick_size(info)
        return sorted(symbols), True, ticks
    except (RateLimitedError, ScanCancelledError):
        raise
    except Exception as e:
        sys.stderr.write(f"Error fetching exchangeInfo (YOLO universe = CORE_MEMES allowlist): {e}\n")
        return list(CORE_MEMES), False, {}

def get_spreads(universe, guard=None):
    """{symbol: (ask - bid) / mid} for the universe from ONE bookTicker request without symbol (weight 5).
    Non-rate-limit errors return {} (the spread only widens the trigger buffer; it is not a gate)."""
    wanted = set(universe)
    try:
        data = _get_json(f"{BASE_FAPI}/fapi/v1/ticker/bookTicker", 5, guard)
    except (RateLimitedError, ScanCancelledError):
        raise
    except Exception as e:
        sys.stderr.write(f"Error fetching book tickers (trigger buffer without spread): {e}\n")
        return {}
    spreads = {}
    for d in data if isinstance(data, list) else []:
        try:
            sym = d.get("symbol")
            if sym not in wanted:
                continue
            bid, ask = float(d["bidPrice"]), float(d["askPrice"])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        mid = (ask + bid) / 2
        if math.isfinite(mid) and bid > 0 and ask >= bid:
            spreads[sym] = (ask - bid) / mid
    return spreads

def audit_symbol(symbol, interval="15m", guard=None):
    """Kline audit of one symbol (None on any error). Returns None without a request once `guard` is stopped
    (rate limit or caller cancellation)."""
    if guard is not None and guard.stopped():
        return None
    url = f"{BASE_FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=40"
    try:
        klines = _get_json(url, 4, guard)

        if len(klines) < 25:
            return None

        closes = [float(x[4]) for x in klines]
        highs = [float(x[2]) for x in klines]
        lows = [float(x[3]) for x in klines]
        opens = [float(x[1]) for x in klines]
        vols = [float(x[5]) for x in klines]

        cur_p = closes[-1]
        # Signal = the last CLOSED candle (klines[-1] is still forming; issue #85): wicks and volume ratio come
        # from it. The trigger/SL extremes span it and the forming candle, so the trigger is never already hit.
        lower_wick, upper_wick = me.candle_wick_pcts(klines[-2])
        c_high, c_low = max(highs[-2], highs[-1]), min(lows[-2], lows[-1])

        # Volume acceleration of the signal candle vs the 20 candles before it
        prior_vols = vols[-22:-2]
        avg_v = sum(prior_vols) / len(prior_vols) if prior_vols else 0.0
        vol_ratio = vols[-2] / avg_v if avg_v > 0 else 1.0

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
            "low": c_low,
            "wick_candle_open_time": klines[-2][0],
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

def _valid_fraction(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None

def trigger_buffer(spread_frac=None, atr_pct=None):
    """Trigger buffer as a fraction of price: min(TRIGGER_BUFFER_MAX, max(TRIGGER_BUFFER,
    TRIGGER_SPREAD_MULT x spread_frac, TRIGGER_ATR_FRAC x atr_pct / 100)). Missing or invalid terms are skipped."""
    terms = [TRIGGER_BUFFER]
    spread = _valid_fraction(spread_frac)
    if spread is not None:
        terms.append(TRIGGER_SPREAD_MULT * spread)
    atr = _valid_fraction(atr_pct)
    if atr is not None:
        terms.append(TRIGGER_ATR_FRAC * atr / 100)
    return min(TRIGGER_BUFFER_MAX, max(terms))

def build_levels(r, direction, sizing, spread=None):
    """Trigger, SL, TP1/TP2 and bounded-capital sizing for a qualified candidate.
    The entry is the breakout trigger, so the SL distance (risk_pct), TP1/TP2, ROE, qty and max loss are all
    measured from the trigger, not from the current price (issue #52). The trigger buffer is spread- and
    ATR-aware (trigger_buffer(); `spread` = (ask - bid) / mid or None)."""
    cur_p = r['price']
    leverage = sizing["leverage"]
    margin = sizing["margin_usdt"]
    atr_pct = r['atr'] / cur_p * 100 if r.get('atr') is not None and cur_p else r.get('atr_pct')
    buffer = trigger_buffer(spread, atr_pct)  # unrounded ATR % (issue #91.8)
    spread_frac = _valid_fraction(spread)
    if direction == "LONG":
        trigger = r['high'] * (1 + buffer)
        sl = max(trigger * (1 - MAX_RISK_PCT / 100), r['low'] - (SL_ATR_MULT * r['atr']))
        risk_pct = (trigger - sl) / trigger * 100
        if risk_pct < MIN_RISK_PCT:
            sl = trigger * (1 - MIN_RISK_PCT / 100)
            risk_pct = MIN_RISK_PCT
        tp1 = trigger * (1 + risk_pct * TP1_R / 100)
        tp2 = trigger * (1 + risk_pct * TP2_R / 100)
        score = r['score_long']
    else:
        trigger = r['low'] * (1 - buffer)
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
        "trigger_buffer_pct": round(buffer * 100, 4),
        "spread_pct": round(spread_frac * 100, 4) if spread_frac is not None else None,
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
      away from the trigger and TP1/TP2 moved toward it by max(YOLO_ROUNDING_MARGIN, tick_size / price) of their
      price (row `tick_size` from exchangeInfo, optional). A fractional leverage is checked rounded up.
    - wide_spread: row `spread_pct` (percent) above TRIGGER_BUFFER_MAX x 100 (the trigger then sits less than one
      spread beyond the candle extreme: illiquid book).
    """
    try:
        direction = str(row["direction"]).upper()
        price = float(row["price"])
        trigger, sl = _sig(row["trigger"]), _sig(row["sl"])
        tp1, tp2 = _sig(row["tp1"]), _sig(row["tp2"])
        leverage, margin = math.ceil(float(row["leverage"])), float(row["margin_usdt"])
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
    tick = _valid_fraction(row.get("tick_size"))
    if tick:
        m = max(m, tick / price)  # coarse ticks: the executor's ROUND_DOWN snap can move a level by a full tick
    if direction == "LONG":
        risk = trigger - sl * (1 - m)
        reward_tp1 = tp1 * (1 - m) - trigger
        reward_tp2 = tp2 * (1 - m) - trigger
    else:
        # SHORT rows are hedge-only (never the recommendation). The executor rounds the trigger DOWN to tickSize
        # before its gates, which moves a SHORT entry adversely (closer to TP, farther from SL); the SL (and
        # TP) margin covers that because it is at least one tick (tick_size / price) when the tick is known.
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
    spread_pct = _valid_fraction(row.get("spread_pct"))
    if spread_pct is not None and spread_pct > TRIGGER_BUFFER_MAX * 100:
        failures.append("wide_spread")
    return failures

def _flag_gates(row, tick_size=None):
    """Adds tick_size (exchangeInfo, or None) and gate_ok / gate_failures to a build_levels() row (flagged, never
    dropped)."""
    row["tick_size"] = tick_size
    failures = yolo_gate_failures(row)
    row["gate_ok"] = not failures
    row["gate_failures"] = failures
    return row

def scan_yolo(target_env, interval="15m", top=5, cancel_event=None):
    """Runs the full YOLO scan and returns the JSON-ready payload (raises on unrecoverable data errors).

    Raises RateLimitedError on HTTP 429/418 (no further requests are issued) and ScanCancelledError when
    `cancel_event` (optional threading.Event) is set; queued kline work is dropped in both cases.

    SIDE-EFFECT CONTRACT (issue #66): screening_pipeline.py may os._exit() the process while this scan is still
    running in its thread. scan_yolo and everything it calls must therefore stay side-effect free:
      - only read-only requests: HTTP GET market data, plus the read-only account reads behind the YOLO margin
        (user_profile.get_yolo_margin -> quant_risk_engine.get_account_equity: GET /fapi/v1/time and signed GET
        /fapi/v2/balance with KEYS auth, or a read-only MCP `tools/call` JSON-RPC POST such as
        futures_usds.futuresAccountBalanceV3 with BINANCE_AUTH_MODE=MCP);
      - no order or other write endpoints (REST or MCP);
      - no file writes except the idempotent os.makedirs of the config dir done by user_profile.load_user_profile;
      - no atexit handlers or finalizers it relies on.
    Health recording (utils/yolo_scan_health.py) happens in the pipeline's main thread, never here.
    """
    guard = _ScanGuard(cancel_event)
    guard.raise_if_stopped()  # a cancel (or an active ban) set before the start: no account read (issue #91.4)
    sizing = resolve_yolo_sizing(target_env)
    guard.raise_if_stopped()
    universe, live, ticks = get_yolo_universe(guard)
    spreads = get_spreads(universe, guard)

    results = []
    executor = ThreadPoolExecutor(max_workers=YOLO_SCAN_WORKERS)
    try:
        futures = [executor.submit(audit_symbol, sym, interval, guard) for sym in universe]
        for f in as_completed(futures):
            res = f.result()
            if res:
                results.append(res)
            if guard.stopped():
                break
    finally:
        executor.shutdown(wait=True, cancel_futures=True)  # queued work is dropped once the guard stopped
    guard.raise_if_stopped()
    if not results:
        raise RuntimeError(f"No kline data could be fetched for the {len(universe)}-symbol YOLO universe.")

    # Never both sides (issue #80): a symbol qualifying LONG and SHORT has no directional edge; it is dropped from
    # both lists (never eligible for the slot) and reported in ambiguous_symbols.
    ambiguous = sorted({r['symbol'] for r in results if r['pass_long'] and r['pass_short']})
    qual_longs = sorted([r for r in results if r['pass_long'] and r['symbol'] not in ambiguous],
                        key=lambda x: x['score_long'], reverse=True)
    qual_shorts = sorted([r for r in results if r['pass_short'] and r['symbol'] not in ambiguous],
                         key=lambda x: x['score_short'], reverse=True)
    # Gates run on every qualified row before the top-N cut, so a gate-passing row ranked below N is not lost
    # (wide-wick top scorers are the most likely to fail the loss cap at high leverage). Gate-passing rows come
    # first; the sort is stable, so score order is kept within each group. Failing rows still fill the list when
    # fewer than `top` rows pass.
    longs = sorted((_flag_gates(build_levels(r, "LONG", sizing, spreads.get(r['symbol'])), ticks.get(r['symbol']))
                    for r in qual_longs), key=lambda c: not c["gate_ok"])[:top]
    shorts = sorted((_flag_gates(build_levels(r, "SHORT", sizing, spreads.get(r['symbol'])), ticks.get(r['symbol']))
                     for r in qual_shorts), key=lambda c: not c["gate_ok"])[:top]
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
        "max_used_weight_1m": guard.max_used_weight,
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
        "ambiguous_symbols": ambiguous,
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
    with rate_limit_guard.scan_session():  # process-wide 429/418 guard; a recorded ban is persisted on exit
        err = rate_limit_guard.error_payload("yolo", env) if rate_limit_guard.is_banned() else None
        if err is None:
            try:
                with contextlib.redirect_stdout(sys.stderr if args.json else real_stdout):
                    payload = scan_yolo(env, interval=args.interval, top=args.top)
            except Exception as e:
                err = {"status": "error", "command": "yolo", "env": env, "error": f"{type(e).__name__}: {e}"}
                if isinstance(e, RateLimitedError):
                    err["market_data_status"] = rate_limit_guard.unavailable_text()
    if err is not None:
        if args.json:
            emit_json(err, real_stdout)
        else:
            sys.stderr.write(f"YOLO scan failed: {err.get('market_data_status') or err['error']}\n")
        return 1

    if args.json:
        emit_json(payload, real_stdout)
    else:
        print_text_report(payload)
    return 0

if __name__ == "__main__":
    sys.exit(main())
