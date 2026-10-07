#!/usr/bin/env python3
"""
test_issue_80_yolo_universe_rate_limit.py - Issues #80, #78, #91 and #85 item 1 (YOLO scanner).

1. YOLO universe from /fapi/v1/exchangeInfo metadata (#80): COIN perpetuals tagged "Meme" or in the exact
   CORE_MEME_BASES allowlist; no TradFi perps, no movers, no substring matches; allowlist-only fallback.
2. Never both sides (#80.4): ambiguous symbols are dropped from both lists and reported.
3. Wicks and volume from the last CLOSED candle; trigger extremes span it and the forming candle (#85.1).
4. Gate flags (#78, #91.3): tickSize rounding margin, fractional leverage rounded up, wide_spread.
5. Process-wide market-data rate-limit guard (#91.1): Retry-After, persisted ban, UNAVAILABLE text, opt-in only.
6. 429 on the YOLO equity read (#91.2), cancel before the account read (#91.4), health-file lock (#91.5),
   run id between the brief and the pipeline (#91.6), doctor text (#91.8b), SKILL.md wording (#78.4, #91.7).

No network: urllib is blocked for the whole module and mocked per test. The ban file and the YOLO health file live
in temporary directories (the real logs/ is never read or written).
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import broad_market_radar as bmr
import broad_yolo_scanner as bys
import execute_futures_trade as eft
import funding_arbitrage as fa
import market_regime as mr
import microstructure_engine as me
import prime_evaluator_brief as peb
import quant_risk_engine as qre
import screening_pipeline as sp
import trading_doctor
import user_profile as up
from utils import file_lock
from utils import rate_limit_guard as rlg
from utils import yolo_scan_health as ysh

PROFILE = {
    "profile_completed": True, "risk_pct_equity": 0.005, "max_margin_ratio": 0.30, "yolo_slot_enabled": True,
    "yolo_equity_pct": 0.001, "leverage_standard": 3, "leverage_yolo": 7, "leverage_ceiling": 10,
}
DISABLED_PROFILE = dict(PROFILE, yolo_slot_enabled=False)
UNAVAILABLE_RE = re.compile(r"^UNAVAILABLE: Binance rate limit \(HTTP (429|418)\), retry after "
                            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch, _ban_dir, _ban_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()
    # Module-wide temp ban file (the _IsolatedState tests patch their own): a stale real ban never flips a test.
    _ban_dir = tempfile.TemporaryDirectory()
    _ban_patch = patch.object(rlg, "STATE_FILE", os.path.join(_ban_dir.name, "market_data_rate_limit.json"))
    _ban_patch.start()
    rlg.reset_for_tests()


def tearDownModule():
    _net_patch.stop()
    _ban_patch.stop()
    _ban_dir.cleanup()
    rlg.reset_for_tests()


# =============================================================================
# Fixtures
# =============================================================================
def _info_row(symbol, base, subtype, contract="PERPETUAL", status="TRADING", utype="COIN", tick="0.0000010",
              quote="USDT"):
    return {"symbol": symbol, "pair": symbol, "contractType": contract, "status": status, "baseAsset": base,
            "quoteAsset": quote, "underlyingType": utype, "underlyingSubType": list(subtype),
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": tick},
                        {"filterType": "LOT_SIZE", "stepSize": "1"}]}


# Rows captured from the live exchangeInfo on 2026-10-06 (logs/issue_work/exchange_info_sample.json).
SAMPLE_INFO = {"symbols": [
    _info_row("BTCUSDT", "BTC", ["PoW", "Crypto"], tick="0.10"),
    _info_row("DOGEUSDT", "DOGE", ["Meme", "Crypto"], tick="0.000010"),
    _info_row("1000PEPEUSDT", "1000PEPE", ["Meme", "Crypto"], tick="0.0000001"),
    _info_row("1000BONKUSDT", "1000BONK", ["Meme", "Crypto"], tick="0.0000010"),
    _info_row("WIFUSDT", "WIF", ["Meme", "Crypto"], tick="0.0001000"),
    _info_row("NOTUSDT", "NOT", ["Gaming", "Crypto"], tick="0.0000001"),
    _info_row("ACTUSDT", "ACT", ["AI", "Crypto"], tick="0.0000010"),
    _info_row("THEUSDT", "THE", ["DeFi", "Crypto"], tick="0.0000100"),
    _info_row("PENGUUSDT", "PENGU", ["Meme", "Crypto"], tick="0.0000010"),
    _info_row("IPUSDT", "IP", ["AI"], status="SETTLING", tick="0.000100"),
    _info_row("SKYUSDT", "SKY", ["DeFi", "Crypto"], tick="0.0000100"),
    _info_row("MONUSDT", "MON", ["DeFi", "Crypto"], tick="0.0000100"),
    _info_row("XAUUSDT", "XAU", ["TradFi"], contract="TRADIFI_PERPETUAL", utype="COMMODITY", tick="0.01"),
    _info_row("TSLAUSDT", "TSLA", ["TradFi"], contract="TRADIFI_PERPETUAL", utype="EQUITY", tick="0.01000"),
    _info_row("WDCUSDT", "WDC", ["TradFi"], contract="TRADIFI_PERPETUAL", utype="EQUITY", tick="0.01000"),
    _info_row("STXXUSDT", "STXX", ["TradFi"], contract="TRADIFI_PERPETUAL", utype="EQUITY", tick="0.01000"),
    _info_row("ZHIPUUSDT", "ZHIPU", ["TradFi"], contract="TRADIFI_PERPETUAL", utype="HK_EQUITY", tick="0.01000"),
]}


def _meme_info(symbols, tick="0.0000001"):
    return {"symbols": [_info_row(s, s[:-4], ["Meme", "Crypto"], tick=tick) for s in symbols]}


def _klines(high=1.005, low=0.975, close=1.001, vol=300, n=40, steps=(1.002, 0.997), forming=(1.0005, 0.9995)):
    """Drift, a signal candle (klines[-2]) and a forming candle (high/low factors `forming` of its open)."""
    ks, price = [], 1.0
    up_, down_ = steps
    for i in range(n - 1):
        o = price
        c = price * (up_ if i % 2 == 0 else down_)
        ks.append([1000 + i, str(o), str(max(o, c) * 1.001), str(min(o, c) * 0.999), str(c), str(100 + i % 7)])
        price = c
    o = price
    ks.append([2000, str(o), str(o * high), str(o * low), str(o * close), str(vol)])
    f = o * close
    ks.append([3000, str(f), str(f * forming[0]), str(f * forming[1]), str(f), "100"])
    return ks


LONG_ONLY = _klines()                                      # down drift: RSI < 45, climax volume
BOTH_SIDES = _klines(steps=(1.0025, 0.9975))               # neutral drift: RSI ~50, climax volume -> LONG and SHORT


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return lambda url: urllib.error.HTTPError(url, code, "rate limit", headers, None)


class _Router:
    """urlopen stand-in routed by URL substring; values are payloads, exceptions or callables(url)."""

    def __init__(self, routes):
        self.routes = routes
        self.urls = []
        self._lock = threading.Lock()

    def __call__(self, req, *args, **kwargs):
        url = getattr(req, "full_url", req)
        with self._lock:
            self.urls.append(url)
        for key, value in self.routes:
            if key in url:
                if callable(value) and not isinstance(value, BaseException):
                    value = value(url)
                if isinstance(value, BaseException):
                    raise value
                return _Resp(value)
        raise urllib.error.URLError(f"no mocked route for {url}")

    def count(self, key=""):
        with self._lock:
            return sum(1 for u in self.urls if key in u)


def _yolo_routes(symbols, klines_by_symbol=None, info=None, book=None):
    klines_by_symbol = klines_by_symbol or {}

    def klines(url):
        for sym, ks in klines_by_symbol.items():
            if f"symbol={sym}&" in url:
                return ks
        return LONG_ONLY

    return [("/fapi/v1/exchangeInfo", info if info is not None else _meme_info(symbols)),
            ("/fapi/v1/ticker/bookTicker", book if book is not None else []),
            ("/fapi/v1/klines", klines)]


@contextlib.contextmanager
def _scanner_env(router, profile=PROFILE, equity=12000.0):
    with patch("urllib.request.urlopen", side_effect=router), \
         patch("user_profile.load_user_profile", return_value=dict(profile)), \
         patch("quant_risk_engine.get_account_equity", return_value=equity) as eq:
        yield eq


def run_main(main_fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main_fn(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def _row(trigger, sl, tp1, tp2, price, leverage=7, direction="LONG", **extra):
    row = {"symbol": "XUSDT", "direction": direction, "score": 80.0, "price": price, "trigger": trigger, "sl": sl,
           "tp1": tp1, "tp2": tp2, "risk_pct": 0.0, "leverage": leverage, "margin_usdt": 12.0, "rsi": 40.0,
           "vol_ratio": 2.5, "lower_wick": 20.0}
    row.update(extra)
    return row


class _IsolatedState(unittest.TestCase):
    """Ban file and YOLO health file in a temp dir; the process-wide guard reset around every test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.ban_file = os.path.join(self.tmp, "logs", "market_data_rate_limit.json")
        self.health_file = os.path.join(self.tmp, "logs", "yolo_scan_health.json")
        for p in (patch.object(rlg, "STATE_FILE", self.ban_file), patch.object(ysh, "HEALTH_FILE", self.health_file)):
            p.start()
            self.addCleanup(p.stop)
        rlg.reset_for_tests()
        self.addCleanup(rlg.reset_for_tests)

    def write_ban(self, banned_until, status=429):
        os.makedirs(os.path.dirname(self.ban_file), exist_ok=True)
        with open(self.ban_file, "w", encoding="utf-8") as f:
            json.dump({"banned_until": banned_until, "status": status, "updated_ts": time.time()}, f)

    def read_ban(self):
        with open(self.ban_file, encoding="utf-8") as f:
            return json.load(f)


# =============================================================================
# 1. YOLO universe (#80)
# =============================================================================
class TestYoloUniverse(_IsolatedState):

    def universe(self, info):
        router = _Router([("/fapi/v1/exchangeInfo", info)])
        with patch("urllib.request.urlopen", side_effect=router), patch("sys.stderr", io.StringIO()):
            result = bys.get_yolo_universe()
        self.assertEqual(router.count(), 1)
        return result

    def test_live_sample_rows(self):
        symbols, live, ticks = self.universe(SAMPLE_INFO)
        self.assertTrue(live)
        self.assertEqual(symbols, ["1000BONKUSDT", "1000PEPEUSDT", "DOGEUSDT", "PENGUUSDT", "WIFUSDT"])
        for excluded in ("WDCUSDT", "STXXUSDT", "ZHIPUUSDT", "XAUUSDT", "TSLAUSDT",   # TradFi perps
                         "SKYUSDT",                                                   # DeFi governance mover
                         "ACTUSDT", "THEUSDT", "IPUSDT", "MONUSDT", "NOTUSDT",        # old substring hits
                         "BTCUSDT"):
            self.assertNotIn(excluded, symbols)
        self.assertEqual(ticks["WIFUSDT"], 0.0001)
        self.assertEqual(ticks["1000PEPEUSDT"], 1e-07)

    def test_exact_allowlist_after_multiplier_prefix(self):
        info = {"symbols": [
            _info_row("1000SHIBUSDT", "1000SHIB", []),            # allowlist, no Meme tag
            _info_row("1000000MOGUSDT", "1000000MOG", ["Meme"]),  # Meme tag
            _info_row("1MBABYDOGEUSDT", "1MBABYDOGE", ["Meme", "Crypto"]),
            _info_row("SHIBAUSDT", "SHIBA", ["DeFi"]),            # not an exact allowlist base
            _info_row("PEPECOINUSDT", "PEPECOIN", ["Layer-1"]),
            _info_row("DOGEUSDC", "DOGE", ["Meme"], quote="USDC"),
            _info_row("DOGEUSDT_261225", "DOGE", ["Meme"], contract="CURRENT_QUARTER"),
            _info_row("WIFUSDT", "WIF", ["Meme"], status="SETTLING"),
            _info_row("PEPEXUSDT", "PEPE", ["Meme"], contract="TRADIFI_PERPETUAL", utype="EQUITY"),
        ]}
        symbols, live, ticks = self.universe(info)
        self.assertEqual(symbols, ["1000000MOGUSDT", "1000SHIBUSDT", "1MBABYDOGEUSDT"])
        self.assertEqual(bys._strip_multiplier("1000PEPE"), "PEPE")
        self.assertEqual(bys._strip_multiplier("1000000MOG"), "MOG")
        self.assertEqual(bys._strip_multiplier("1MBABYDOGE"), "BABYDOGE")
        self.assertEqual(bys._strip_multiplier("1000"), "1000")
        self.assertFalse(hasattr(bys, "MEME_KEYWORDS"))  # no substring matching anywhere

    def test_non_rate_limit_failure_falls_back_to_the_allowlist_only(self):
        for failure in (urllib.error.URLError("down"), _http_error(500), {"unexpected": "shape"}):
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(self.universe(failure), (list(bys.CORE_MEMES), False, {}))
        for sym in bys.CORE_MEMES:
            self.assertTrue(sym.endswith("USDT"))
            self.assertIn(bys._strip_multiplier(sym[:-4]), bys.CORE_MEME_BASES, sym)
        for not_meme in ("ACTUSDT", "POPCATUSDT", "GOATUSDT"):
            self.assertNotIn(not_meme, bys.CORE_MEMES)

    def test_scan_uses_exchange_info_and_never_the_24h_ticker(self):
        router = _Router(_yolo_routes([], info=SAMPLE_INFO))
        with _scanner_env(router):
            data = bys.scan_yolo("prod", top=5)
        self.assertEqual(router.count("/fapi/v1/exchangeInfo"), 1)
        self.assertEqual(router.count("ticker/24hr"), 0)  # movers are no longer added
        self.assertEqual(data["universe_size"], 5)
        kline_symbols = {re.search(r"symbol=([A-Z0-9]+)&", u).group(1) for u in router.urls if "/klines" in u}
        self.assertEqual(kline_symbols, {"1000BONKUSDT", "1000PEPEUSDT", "DOGEUSDT", "PENGUUSDT", "WIFUSDT"})
        self.assertTrue(data["universe_from_live_ticker"])
        self.assertEqual({c["symbol"]: c["tick_size"] for c in data["longs"]}["WIFUSDT"], 0.0001)


# =============================================================================
# 2. Never both sides (#80.4)
# =============================================================================
class TestNeverBothSides(_IsolatedState):

    def test_both_sides_symbol_is_dropped_and_reported(self):
        router = _Router([("/fapi/v1/klines", lambda url: BOTH_SIDES)])
        with patch("urllib.request.urlopen", side_effect=router):
            audit = bys.audit_symbol("WIFUSDT")
        self.assertTrue(audit["pass_long"] and audit["pass_short"], audit)  # the fixture really is ambiguous

        router = _Router(_yolo_routes(["WIFUSDT", "DOGEUSDT"], {"WIFUSDT": BOTH_SIDES, "DOGEUSDT": LONG_ONLY}))
        with _scanner_env(router):
            data = bys.scan_yolo("prod", top=5)
        self.assertEqual([c["symbol"] for c in data["longs"]], ["DOGEUSDT"])
        self.assertEqual(data["shorts"], [])
        self.assertEqual(data["ambiguous_symbols"], ["WIFUSDT"])
        self.assertEqual(data["recommendation"]["symbol"], "DOGEUSDT")

    def test_only_ambiguous_symbols_leave_the_slot_empty(self):
        router = _Router(_yolo_routes(["WIFUSDT"], {"WIFUSDT": BOTH_SIDES}))
        with _scanner_env(router):
            data = bys.scan_yolo("prod", top=5)
        self.assertEqual((data["longs"], data["shorts"], data["slot_status"]), ([], [], "EMPTY"))
        self.assertIsNone(data["recommendation"])
        self.assertEqual(data["ambiguous_symbols"], ["WIFUSDT"])


# =============================================================================
# 3. Closed-candle wicks (#85 item 1, YOLO)
# =============================================================================
class TestClosedCandleWicks(unittest.TestCase):

    def test_wicks_and_volume_from_the_last_closed_candle(self):
        # Forming candle with a higher high and a lower low than the signal candle.
        ks = _klines(high=1.005, low=0.975, close=1.001, vol=300, forming=(1.012, 0.97))
        router = _Router([("/fapi/v1/klines", lambda url: ks)])
        with patch("urllib.request.urlopen", side_effect=router):
            r = bys.audit_symbol("WIFUSDT")
        lower, upper = me.candle_wick_pcts(ks[-2])
        self.assertEqual((r["lower_wick"], r["upper_wick"]), (round(lower, 1), round(upper, 1)))
        self.assertNotEqual(me.candle_wick_pcts(ks[-1]), me.candle_wick_pcts(ks[-2]))
        vols = [float(k[5]) for k in ks]
        self.assertEqual(r["vol_ratio"], round(vols[-2] / (sum(vols[-22:-2]) / 20), 2))
        self.assertNotEqual(r["vol_ratio"], round(vols[-2] / (sum(vols[-12:-2]) / 10), 2))
        self.assertEqual(r["high"], max(float(ks[-2][2]), float(ks[-1][2])))
        self.assertEqual(r["high"], float(ks[-1][2]))   # the forming candle's higher high
        self.assertEqual(r["low"], float(ks[-1][3]))
        self.assertEqual(r["wick_candle_open_time"], ks[-2][0])
        self.assertEqual(r["price"], float(ks[-1][4]))
        # The LONG trigger sits beyond both candles' highs (never an immediately-triggering stop).
        row = bys.build_levels(r, "LONG", {"margin_usdt": 12.0, "leverage": 7})
        self.assertGreater(row["trigger"], float(ks[-1][2]))
        short = bys.build_levels(r, "SHORT", {"margin_usdt": 12.0, "leverage": 7})
        self.assertLess(short["trigger"], float(ks[-1][3]))

    def test_trigger_buffer_uses_unrounded_atr(self):
        r = {"symbol": "X", "price": 1.0, "rsi": 40.0, "vol_ratio": 2.5, "lower_wick": 10.0, "upper_wick": 10.0,
             "atr_pct": 3.0, "atr": 0.03049, "high": 1.01, "low": 0.99, "score_long": 80.0, "score_short": 80.0}
        row = bys.build_levels(r, "LONG", {"margin_usdt": 12.0, "leverage": 7})
        self.assertAlmostEqual(row["trigger_buffer_pct"], round(0.1 * 3.049, 4))  # not 0.1 x the rounded 3.0


# =============================================================================
# 4. Gate flags (#78, #91.3)
# =============================================================================
# Loss cap at 7x: stop 4.9% from the trigger. Worst case with the 0.05% margin = 0.346 <= 0.35; with one 0.0001
# tick at a 0.05 price (0.2%) = 0.356 > 0.35.
TICK_BORDERLINE = _row(trigger=0.05, sl=0.04755, tp1=0.05539, tp2=0.061025, price=0.0495)
# 4.5% stop: x7 = 0.318 passes, x8 = 0.364 fails.
LEVERAGE_BORDERLINE = _row(trigger=1.0, sl=0.955, tp1=1.099, tp2=1.2025, price=0.99)


class TestGateFlags(unittest.TestCase):

    def test_coarse_tick_flips_a_borderline_row(self):
        self.assertEqual(bys.yolo_gate_failures(TICK_BORDERLINE), [])
        self.assertEqual(bys.yolo_gate_failures(dict(TICK_BORDERLINE, tick_size=None)), [])
        self.assertEqual(bys.yolo_gate_failures(dict(TICK_BORDERLINE, tick_size=1e-7)), [])  # fine tick: 0.05% kept
        coarse = bys._flag_gates(dict(TICK_BORDERLINE), 0.0001)
        self.assertEqual(coarse["tick_size"], 0.0001)
        self.assertFalse(coarse["gate_ok"])
        self.assertEqual(coarse["gate_failures"], ["loss_cap"])
        self.assertIsNone(sp._to_yolo_candidate(coarse))                  # the pipeline re-checks with the tick
        self.assertIsNotNone(sp._to_yolo_candidate(dict(TICK_BORDERLINE)))

    def test_fractional_leverage_is_checked_rounded_up(self):
        self.assertEqual(bys.yolo_gate_failures(LEVERAGE_BORDERLINE), [])
        self.assertEqual(bys.yolo_gate_failures(dict(LEVERAGE_BORDERLINE, leverage=7.9)), ["loss_cap"])
        self.assertEqual(bys.yolo_gate_failures(dict(LEVERAGE_BORDERLINE, leverage=8)), ["loss_cap"])
        self.assertIsNone(sp._to_yolo_candidate(dict(LEVERAGE_BORDERLINE, leverage=7.9)))
        cand = sp._to_yolo_candidate(dict(LEVERAGE_BORDERLINE, leverage=7.0))
        self.assertEqual(cand.leverage, 7)
        self.assertIsInstance(cand.leverage, int)
        self.assertEqual(bys.yolo_gate_failures(dict(LEVERAGE_BORDERLINE, leverage=float("nan"))), ["coherence"])

    def test_wide_spread_flag(self):
        self.assertEqual(bys.TRIGGER_BUFFER_MAX, 0.005)
        for spread, flagged in ((0.6, True), (0.51, True), (0.5, False), (0.1, False), (None, False)):
            with self.subTest(spread=spread):
                failures = bys.yolo_gate_failures(dict(LEVERAGE_BORDERLINE, spread_pct=spread))
                self.assertEqual("wide_spread" in failures, flagged)
        self.assertIsNone(sp._to_yolo_candidate(dict(LEVERAGE_BORDERLINE, spread_pct=0.6)))

    def test_scanner_flags_a_wide_spread_row(self):
        book = [{"symbol": "WIFUSDT", "bidPrice": "0.997", "askPrice": "1.003"},     # 0.6% spread
                {"symbol": "DOGEUSDT", "bidPrice": "0.9999", "askPrice": "1.0001"}]
        router = _Router(_yolo_routes(["WIFUSDT", "DOGEUSDT"], book=book))
        with _scanner_env(router):
            data = bys.scan_yolo("prod", top=5)
        rows = {c["symbol"]: c for c in data["longs"]}
        self.assertEqual(rows["WIFUSDT"]["spread_pct"], 0.6)
        self.assertFalse(rows["WIFUSDT"]["gate_ok"])
        self.assertIn("wide_spread", rows["WIFUSDT"]["gate_failures"])
        self.assertTrue(rows["DOGEUSDT"]["gate_ok"])
        self.assertEqual(data["recommendation"]["symbol"], "DOGEUSDT")


# =============================================================================
# 5. Process-wide rate-limit guard (#91.1)
# =============================================================================
class TestGuardUnit(_IsolatedState):

    def test_disabled_guard_changes_nothing(self):
        self.assertFalse(rlg.is_enabled())
        rlg.trip(429, "30")
        self.assertFalse(rlg.is_banned())
        rlg.raise_if_banned()
        router = _Router([("fapi", _http_error(429, "30"))])
        with patch("urllib.request.urlopen", side_effect=router):
            for fetch in (lambda: me.fetch_json("https://fapi.binance.com/fapi/v1/klines?x"),
                          lambda: qre.fetch_json("https://fapi.binance.com/fapi/v1/klines?x"),
                          lambda: fa.fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex"),
                          lambda: mr.fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex"),
                          lambda: bmr.fetch_klines("BTCUSDT")):
                with self.assertRaises(urllib.error.HTTPError) as ctx:  # exactly as before: no RateLimitedError
                    fetch()
                self.assertNotIsInstance(ctx.exception, rlg.RateLimitedError)
        self.assertEqual(router.count(), 5)
        rlg.persist()
        self.assertFalse(os.path.exists(self.ban_file))

    def test_retry_after_parsing_and_defaults(self):
        self.assertEqual(rlg.parse_retry_after("30", 429), 30.0)
        self.assertEqual(rlg.parse_retry_after(b"7", 418), 7.0)
        self.assertEqual(rlg.parse_retry_after(None, 429), 60.0)
        self.assertEqual(rlg.parse_retry_after(None, 418), 120.0)
        self.assertEqual(rlg.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 429), 60.0)
        self.assertEqual(rlg.parse_retry_after("-5", 418), 120.0)
        self.assertEqual(rlg.parse_retry_after("999999999", 418), 3 * 24 * 3600.0)

    def test_429_with_retry_after_trips_persists_and_blocks_every_fetch(self):
        rlg.enable()
        router = _Router([("fapi", _http_error(429, "30"))])
        t0 = time.time()
        with patch("urllib.request.urlopen", side_effect=router):
            with self.assertRaises(rlg.RateLimitedError) as ctx:
                me.fetch_json("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT")
            self.assertEqual(ctx.exception.status, 429)
            self.assertIsInstance(ctx.exception, RuntimeError)
            self.assertIs(bys.RateLimitedError, rlg.RateLimitedError)
            for fetch in (lambda: me.fetch_json("https://fapi.binance.com/x"),
                          lambda: qre.fetch_json("https://fapi.binance.com/x"),
                          lambda: fa.fetch_json("https://fapi.binance.com/x"),
                          lambda: mr.fetch_json("https://fapi.binance.com/x"),
                          lambda: bmr.fetch_klines("BTCUSDT"),
                          lambda: bys._get_json("https://fapi.binance.com/x", 1)):
                with self.assertRaises(rlg.RateLimitedError):
                    fetch()
        self.assertEqual(router.count(), 1)  # nothing after the first 429
        self.assertTrue(t0 + 30 <= rlg.banned_until() <= time.time() + 30)
        self.assertRegex(rlg.unavailable_text(), UNAVAILABLE_RE)
        self.assertFalse(os.path.exists(self.ban_file))  # trip() never writes
        rlg.persist()
        saved = self.read_ban()
        self.assertEqual(saved["status"], 429)
        self.assertAlmostEqual(saved["banned_until"], rlg.banned_until(), places=3)

    def test_418_without_header_bans_120s(self):
        rlg.enable()
        router = _Router([("fapi", _http_error(418))])
        t0 = time.time()
        with patch("urllib.request.urlopen", side_effect=router), self.assertRaises(rlg.RateLimitedError):
            bys._get_json("https://fapi.binance.com/fapi/v1/klines?x", 1)
        self.assertTrue(t0 + 120 <= rlg.banned_until() <= time.time() + 120)
        self.assertIn("(HTTP 418)", rlg.unavailable_text())

    def test_persisted_ban_restored_only_while_active(self):
        now = time.time()
        self.write_ban(now + 50, 418)
        rlg.enable(now=now)
        self.assertTrue(rlg.is_banned(now=now))
        self.assertFalse(rlg.is_banned(now=now + 51))
        rlg.reset_for_tests()
        self.write_ban(now - 1)                       # expired: ignored
        rlg.enable(now=now)
        self.assertFalse(rlg.is_banned(now=now))
        rlg.reset_for_tests()
        with open(self.ban_file, "w", encoding="utf-8") as f:
            f.write("{corrupt")
        rlg.enable()
        self.assertFalse(rlg.is_banned())

    def test_state_file_name_and_location(self):
        with open(os.path.join(SCRIPTS_DIR, "utils", "rate_limit_guard.py"), encoding="utf-8") as f:
            self.assertIn('os.path.join(BASE_DIR, "logs", "market_data_rate_limit.json")', f.read())
        for protected in ("session_state.json", "guardian_state.json", "pending_entries.json"):
            self.assertNotIn(protected, "market_data_rate_limit.json")


class TestScanEntryPointsHonourTheBan(_IsolatedState):
    """A persisted ban: every scan CLI prints the fixed UNAVAILABLE text and never calls Binance."""

    def _cli(self, main_fn, argv):
        router = _Router([("", _no_network)])
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch("quant_risk_engine.get_account_equity", side_effect=AssertionError("account read")), \
             patch("execute_futures_trade.send_signed_request", side_effect=AssertionError("signed read")):
            code, out, err = run_main(main_fn, argv)
        self.assertEqual(router.count(), 0)
        return code, out, err

    def test_every_scan_cli_skips_while_banned(self):
        self.write_ban(time.time() + 300, 429)
        for name, main_fn, argv in (("yolo", bys.main, ["--json", "--env", "prod"]),
                                    ("scan", bmr.main, ["--json", "--env", "prod"]),
                                    ("pairs", qre.main, ["pairs", "--json", "--env", "prod"]),
                                    ("funding", fa.main, ["--json", "--env", "prod"]),
                                    ("regime", mr.main, ["--json", "--env", "prod"])):
            with self.subTest(cli=name):
                code, out, _ = self._cli(main_fn, argv)
                self.assertEqual(code, 1)
                data = json.loads(out)
                self.assertEqual(data["status"], "error")
                self.assertRegex(data["market_data_status"], UNAVAILABLE_RE)
                self.assertFalse(rlg.is_enabled())  # scoped to the CLI run

    def test_yolo_cli_429_persists_and_the_next_run_skips_until_expiry(self):
        router = _Router(_yolo_routes(["WIFUSDT"], klines_by_symbol={"WIFUSDT": _http_error(429, "30")}))
        router.routes[-1] = ("/fapi/v1/klines", _http_error(429, "30"))
        t0 = time.time()
        with _scanner_env(router):
            code, out, _ = run_main(bys.main, ["--json", "--env", "prod"])
        self.assertEqual(code, 1)
        self.assertRegex(json.loads(out)["market_data_status"], UNAVAILABLE_RE)
        saved = self.read_ban()
        self.assertEqual(saved["status"], 429)
        self.assertTrue(t0 + 30 <= saved["banned_until"] <= time.time() + 30)

        rlg.reset_for_tests()  # a new process: the ban comes from the file
        code, out, _ = self._cli(bys.main, ["--json", "--env", "prod"])
        self.assertEqual(code, 1)
        self.assertRegex(json.loads(out)["market_data_status"], UNAVAILABLE_RE)

        rlg.reset_for_tests()
        self.write_ban(time.time() - 1)  # expired
        router = _Router(_yolo_routes(["WIFUSDT"]))
        with _scanner_env(router):
            code, out, _ = run_main(bys.main, ["--json", "--env", "prod"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "ok")
        self.assertGreater(router.count("/fapi/v1/klines"), 0)

    def test_radar_swallowed_kline_429_fails_the_run(self):
        info = {"symbols": [{"symbol": "AAAUSDT", "underlyingType": "COIN", "contractType": "PERPETUAL",
                             "quoteAsset": "USDT", "status": "TRADING"}]}
        router = _Router([("exchangeInfo", info), ("ticker/24hr", [{"symbol": "AAAUSDT", "quoteVolume": "1"}]),
                          ("/fapi/v1/klines", _http_error(429, "45"))])
        with patch("urllib.request.urlopen", side_effect=router):
            code, out, _ = run_main(bmr.main, ["--json", "--env", "prod"])
        self.assertEqual(code, 1)  # analyze_single_symbol swallowed the error; the tripped guard still fails it
        self.assertRegex(json.loads(out)["market_data_status"], UNAVAILABLE_RE)
        self.assertEqual(self.read_ban()["status"], 429)


class _PipelineMocks(_IsolatedState):

    def setUp(self):
        super().setUp()
        macro = sp.MacroContext(btc_price=1.0, btc_regime="N", btc_regime_desc="", btc_absorption="NONE",
                                btc_taker_ratio=1.0, btc_cvd_30v=0.0, btc_oi_z_score=0.0, btc_tape_bias="B",
                                btc_tape_imbalance=0.0, allows_alt_shorts=True)
        setup = sp.CandidateSetup(
            symbol="SOLUSDT", direction="LONG", tier="Tier A", confidence=60, current_price=100.0,
            trigger_price=100.5, sl_price=97.0, tp1_price=104.0, tp2_price=110.0, rr_ratio=3.0, risk_pct=3.0,
            rsi_15m=35.0, vol_ratio=1.5, lower_wick_pct=55.0, upper_wick_pct=5.0, cvd_delta=0.0, oi_z_score=0.0,
            regime="C", absorption="NONE", whale_bias="B", required_margin=10.0, step_qty=1.0, actual_notional=30.0,
            target_dollar_risk=1.9, reasons=["t"])
        self.radar = patch("broad_market_radar.scan_all_liquid_pairs", return_value=[{"symbol": "SOLUSDT"}])
        patches = [
            patch("screening_pipeline.fetch_macro_btc", return_value=macro),
            self.radar,
            patch("quant_risk_engine.scan_coingrated_market_pairs", return_value=[]),
            patch("funding_arbitrage.scan_top_funding_opportunities", return_value=[]),
            patch("screening_pipeline.fetch_news_summary", return_value=[]),
            patch("screening_pipeline.enrich_and_size_candidate", return_value=setup),
            patch("sync_session_state.sync_session_state", return_value={}),
        ]
        self.mocks = {}
        for p in patches:
            self.mocks[p.attribute] = p.start()
            self.addCleanup(p.stop)

    def run_pipeline(self, router, profile=DISABLED_PROFILE, env=None):
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("user_profile.load_user_profile", return_value=dict(profile)), \
             patch("quant_risk_engine.get_account_equity", return_value=12000.0) as eq, \
             patch.dict(os.environ, env or {}), patch("sys.stderr", io.StringIO()):
            if not env:
                os.environ.pop(ysh.RUN_ID_ENV, None)  # restored by patch.dict
            payload = sp.execute_screening_pipeline(target_env="prod")
            if sp._last_yolo_future is not None:
                with contextlib.suppress(Exception):
                    sp._last_yolo_future.result(timeout=5)  # the scan thread finishes inside the patches
        self.equity_mock = eq
        return payload


class TestPipelineMarketDataStatus(_PipelineMocks):

    def test_429_in_the_standard_scan_reports_unavailable_and_the_next_run_skips_binance(self):
        self.mocks["scan_all_liquid_pairs"].side_effect = lambda *a, **k: bmr._get_json(
            "https://fapi.binance.com/fapi/v1/exchangeInfo", 8)
        router = _Router([("exchangeInfo", _http_error(429, "30"))])
        payload = self.run_pipeline(router)
        self.assertRegex(payload.market_data_status, UNAVAILABLE_RE)
        self.assertEqual((payload.top_candidates, payload.actionable_stat_arb), ([], []))
        self.assertIsNone(payload.top_funding_arbitrage)
        self.assertFalse(payload.macro.allows_alt_shorts)
        self.assertEqual(payload.yolo_slot.status, "DISABLED")
        self.assertEqual(self.read_ban()["status"], 429)
        self.assertFalse(rlg.is_enabled())

        rlg.reset_for_tests()  # next process
        self.mocks["scan_all_liquid_pairs"].reset_mock()
        router = _Router([("", _no_network)])
        payload = self.run_pipeline(router, profile=PROFILE)
        self.assertEqual(router.count(), 0)
        self.mocks["scan_all_liquid_pairs"].assert_not_called()
        self.equity_mock.assert_not_called()       # the YOLO scan stopped before its account read
        self.assertRegex(payload.market_data_status, UNAVAILABLE_RE)
        self.assertEqual(payload.top_candidates, [])
        self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        self.assertEqual(payload.yolo_slot_status, "UNAVAILABLE: YOLO scan rate-limited by Binance. YOLO slot kept empty.")

    def test_pipeline_cli_exits_0_with_the_unavailable_payload(self):
        self.write_ban(time.time() + 300, 418)
        router = _Router([("", _no_network)])
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("user_profile.load_user_profile", return_value=dict(DISABLED_PROFILE)):
            code, out, _ = run_main(sp.main, ["--json", "--env", "prod"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertRegex(data["market_data_status"], UNAVAILABLE_RE)
        self.assertIn("(HTTP 418)", data["market_data_status"])
        self.assertEqual(data["top_candidates"], [])
        self.assertEqual(router.count(), 0)

    def test_swallowed_429_drops_the_candidates(self):
        def radar(*a, **k):
            try:  # a per-symbol fetch that swallows errors, like analyze_single_symbol
                bmr.fetch_klines("AAAUSDT")
            except Exception:
                pass
            return [{"symbol": "SOLUSDT"}]

        self.mocks["scan_all_liquid_pairs"].side_effect = radar
        payload = self.run_pipeline(_Router([("klines", _http_error(429, "20"))]))
        self.assertRegex(payload.market_data_status, UNAVAILABLE_RE)
        self.assertEqual(payload.top_candidates, [])

    def test_swallowed_429_after_an_active_yolo_scan_empties_the_slot(self):
        """Audit round 1: the YOLO scan finished ACTIVE, then the radar hit a swallowed 429: no YOLO candidate."""
        seen = {}

        def radar(*a, **k):
            seen["scan"] = sp._last_yolo_future.result(timeout=5)  # the YOLO scan is done (CANDIDATE)
            try:
                bmr.fetch_klines("AAAUSDT")
            except Exception:
                pass
            return [{"symbol": "SOLUSDT"}]

        self.mocks["scan_all_liquid_pairs"].side_effect = radar
        routes = _yolo_routes(["WIFUSDT"])
        router = _Router([("symbol=AAAUSDT", _http_error(429, "20"))] + routes)
        payload = self.run_pipeline(router, profile=PROFILE)
        self.assertEqual(seen["scan"]["slot_status"], "CANDIDATE")  # it would have been ACTIVE
        self.assertRegex(payload.market_data_status, UNAVAILABLE_RE)
        self.assertEqual(payload.top_candidates, [])
        self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        self.assertEqual(payload.yolo_slot.candidates, [])
        self.assertEqual(payload.yolo_slot_status,
                         "UNAVAILABLE: YOLO scan rate-limited by Binance. YOLO slot kept empty.")
        h = ysh.read_health()
        self.assertEqual((h["last_status"], h["last_unavailable_reason"]),
                         ("UNAVAILABLE", "YOLO scan rate-limited by Binance"))

    def test_ban_keeps_a_disabled_slot_disabled(self):
        self.mocks["scan_all_liquid_pairs"].side_effect = lambda *a, **k: bmr._get_json(
            "https://fapi.binance.com/fapi/v1/exchangeInfo", 8)
        payload = self.run_pipeline(_Router([("exchangeInfo", _http_error(429))]), profile=DISABLED_PROFILE)
        self.assertEqual(payload.yolo_slot.status, "DISABLED")

    def test_no_ban_keeps_the_payload_unchanged(self):
        payload = self.run_pipeline(_Router([("", _no_network)]))
        self.assertIsNone(payload.market_data_status)
        self.assertEqual([c.symbol for c in payload.top_candidates], ["SOLUSDT"])
        self.assertFalse(os.path.exists(self.ban_file))


class TestRiskReducingPathsNeverEnableTheGuard(unittest.TestCase):

    ENTRY_POINTS = {"broad_yolo_scanner.py", "broad_market_radar.py", "screening_pipeline.py",
                    "quant_risk_engine.py", "funding_arbitrage.py", "market_regime.py"}

    def test_only_scan_entry_points_enable_it(self):
        enabling = set()
        for root, _, files in os.walk(SCRIPTS_DIR):
            for name in files:
                if not name.endswith(".py") or name == "rate_limit_guard.py":
                    continue
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    src = f.read()
                if "rate_limit_guard.enable(" in src or "rate_limit_guard.scan_session(" in src:
                    enabling.add(name)
        self.assertEqual(enabling, self.ENTRY_POINTS)
        for path in ("execute_futures_trade.py", "dynamic_exit_manager.py", os.path.join("loops", "position_guardian_loop.py"),
                     os.path.join("loops", "night_cutoff_loop.py")):
            with open(os.path.join(SCRIPTS_DIR, path), encoding="utf-8") as f:
                self.assertNotIn("rate_limit_guard", f.read(), path)

    def test_executor_and_guardian_imports_leave_it_disabled(self):
        rlg.reset_for_tests()
        loops = os.path.join(SCRIPTS_DIR, "loops")
        if loops not in sys.path:
            sys.path.insert(0, loops)
        import position_guardian_loop  # noqa: F401
        self.assertIsNotNone(eft.send_signed_request)
        self.assertFalse(rlg.is_enabled())
        rlg.trip(418)
        self.assertFalse(rlg.is_banned())


# =============================================================================
# 6. Other #91 items
# =============================================================================
class TestEquityRateLimit(_IsolatedState):

    def test_rate_limited_equity_read_raises_instead_of_a_default_margin(self):
        for answer in ({"code": -1003, "msg": "Too many requests"}, {"error": "HTTP 429: {}"},
                       {"error": "HTTP 418: banned"},
                       {"error": "MCP Gateway HTTP 429: x", "isError": True, "http_code": 429},   # MCP auth mode
                       {"error": "MCP Gateway HTTP 418: x", "isError": True, "http_code": 418}):
            with self.subTest(answer=answer), \
                 patch("execute_futures_trade.send_signed_request", return_value=answer), \
                 patch("user_profile.load_user_profile", return_value=dict(PROFILE)):
                with self.assertRaises(rlg.RateLimitedError):
                    qre.get_account_equity(target_env="prod")
                with self.assertRaises(rlg.RateLimitedError):
                    up.get_yolo_margin(target_env="prod")
                with self.assertRaises(rlg.RateLimitedError):
                    bys.resolve_yolo_sizing("prod")

    def test_scan_stops_before_market_data_on_a_rate_limited_equity_read(self):
        router = _Router(_yolo_routes(["WIFUSDT"]))
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch("execute_futures_trade.send_signed_request", return_value={"code": -1003, "msg": "x"}), \
             self.assertRaises(bys.RateLimitedError):
            bys.scan_yolo("prod")
        self.assertEqual(router.count(), 0)

    def test_other_failures_keep_the_default_margin(self):
        with patch("execute_futures_trade.send_signed_request", return_value={"error": "HTTP 500: boom"}), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(RuntimeError) as ctx:
                qre.get_account_equity(target_env="prod")
            self.assertNotIsInstance(ctx.exception, rlg.RateLimitedError)
            self.assertEqual(up.get_yolo_margin(target_env="prod"), 10.0)  # 100 x 0.1% clamped to [10, 15]


class TestCancelBeforeStart(unittest.TestCase):

    def test_no_account_read_after_a_cancel(self):
        cancel = threading.Event()
        cancel.set()
        router = _Router(_yolo_routes(["WIFUSDT"]))
        with _scanner_env(router) as eq, patch.object(bys, "resolve_yolo_sizing", wraps=bys.resolve_yolo_sizing) as rs, \
             self.assertRaises(bys.ScanCancelledError):
            bys.scan_yolo("prod", cancel_event=cancel)
        eq.assert_not_called()
        rs.assert_not_called()
        self.assertEqual(router.count(), 0)


class TestHealthFileLock(_IsolatedState):

    def test_concurrent_record_scan_calls_are_all_counted(self):
        real_write = ysh.atomic_write_json

        def slow_write(path, data):
            time.sleep(0.05)  # widen the read-modify-write window
            return real_write(path, data)

        threads = [threading.Thread(target=ysh.record_scan, args=("UNAVAILABLE", "r")) for _ in range(6)]
        with patch.object(ysh, "atomic_write_json", side_effect=slow_write):
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(ysh.read_health()["consecutive_unavailable"], 6)

    def test_bounded_wait_then_proceeds_without_the_lock(self):
        target = os.path.join(self.tmp, "x.json")
        err = io.StringIO()
        with file_lock.locked(target) as held:
            self.assertTrue(held)
            t0 = time.monotonic()
            with patch("sys.stderr", err), file_lock.locked(target, wait_s=0.2) as second:
                self.assertFalse(second)
            self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIn("proceeding without the lock", err.getvalue())
        with file_lock.locked(target) as again:  # released
            self.assertTrue(again)


class TestRunId(_PipelineMocks):

    def _assemble(self, risk_profile, run):
        brief_file = os.path.join(self.tmp, "primed_brief.json")
        with patch.object(peb, "BRIEF_FILE", brief_file), \
             patch.object(peb, "ensure_fresh_state", return_value={"target_env": "prod"}), \
             patch.object(peb, "load_recent_insights", return_value=[]), \
             patch.object(peb, "build_risk_profile", return_value=dict(risk_profile)), \
             patch("prime_evaluator_brief.subprocess.run", side_effect=run) as mock_run:
            brief = peb.assemble_primed_brief(target_env="prod")
        self.run_id = mock_run.call_args.kwargs["env"][ysh.RUN_ID_ENV]
        self.assertRegex(self.run_id, r"^[0-9a-f]{32}$")
        return brief

    def test_pipeline_echoes_and_records_the_run_id(self):
        router = _Router(_yolo_routes(["WIFUSDT"]))
        payload = self.run_pipeline(router, profile=PROFILE, env={ysh.RUN_ID_ENV: "abc123"})
        self.assertEqual(payload.run_id, "abc123")
        self.assertEqual(ysh.read_health()["last_run_id"], "abc123")
        payload = self.run_pipeline(_Router(_yolo_routes(["WIFUSDT"])), profile=PROFILE)
        self.assertIsNone(payload.run_id)
        self.assertNotIn("last_run_id", ysh.read_health())

    def _payload(self, run_id, **extra):
        data = {"yolo_slot_status": "INACTIVE: x", "yolo_slot": {"status": "INACTIVE"}, "run_id": run_id}
        data.update(extra)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(data))

    def test_mismatched_or_missing_run_id_fails_closed(self):
        for echoed in ("someone-else", None):
            with self.subTest(echoed=echoed):
                brief = self._assemble({"yolo_slot_enabled": True}, lambda *a, **k: self._payload(echoed))
                self.assertEqual(brief["yolo_slot"], {"status": "UNAVAILABLE", "candidates": [],
                                                      "summary": peb.YOLO_RUN_ID_MISMATCH_SUMMARY})
                self.assertEqual(ysh.read_health()["last_run_id"], self.run_id)
        self.assertEqual(ysh.read_health()["consecutive_unavailable"], 2)
        self.assertEqual(ysh.read_health()["last_unavailable_reason"], "screening payload run id mismatch")

    def test_matching_run_id_is_trusted(self):
        brief = self._assemble({"yolo_slot_enabled": True},
                               lambda *a, **k: self._payload(k["env"][ysh.RUN_ID_ENV]))
        self.assertEqual(brief["yolo_slot"]["status"], "INACTIVE")
        self.assertFalse(os.path.exists(self.health_file))

    def test_ok_recorded_then_crash_is_counted(self):
        """Issue #91.6: the subprocess recorded OK for this run, then crashed while writing its output."""
        def run(*a, **k):
            ysh.record_scan("ACTIVE", run_id=k["env"][ysh.RUN_ID_ENV], now=time.time() + 5)
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="crash")

        brief = self._assemble({"yolo_slot_enabled": True}, run)
        self.assertEqual(brief["yolo_slot"]["summary"], peb.YOLO_PIPELINE_FAILED_SUMMARY)
        h = ysh.read_health()
        self.assertEqual((h["consecutive_unavailable"], h["last_status"]), (1, "UNAVAILABLE"))

    def test_disabled_slot_and_failed_pipeline_shows_disabled_text(self):
        brief = self._assemble({"yolo_slot_enabled": False},
                               lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=1, stdout=""))
        self.assertEqual(brief["yolo_slot"]["status"], "DISABLED")
        self.assertEqual(brief["yolo_slot"]["summary"], sp.YOLO_DISABLED_STATUS)
        self.assertIn(f"**YOLO Slot:** {sp.YOLO_DISABLED_STATUS}", peb.format_markdown_brief(brief))
        self.assertFalse(os.path.exists(self.health_file))

    def test_market_data_status_forwarded_verbatim(self):
        text = "UNAVAILABLE: Binance rate limit (HTTP 429), retry after 2026-10-06T12:00:00Z"
        brief = self._assemble({"yolo_slot_enabled": False},
                               lambda *a, **k: self._payload(k["env"][ysh.RUN_ID_ENV], market_data_status=text))
        self.assertEqual(brief["market_data_status"], text)
        self.assertIn(f"**Market data:** {text}", peb.format_markdown_brief(brief))
        ok = self._assemble({"yolo_slot_enabled": False},
                            lambda *a, **k: self._payload(k["env"][ysh.RUN_ID_ENV]))
        self.assertIsNone(ok["market_data_status"])
        self.assertNotIn("**Market data:**", peb.format_markdown_brief(ok))


class TestDocsAndDoctor(_IsolatedState):

    def test_doctor_warning_names_the_structured_flags(self):
        for _ in range(3):
            ysh.record_scan("UNAVAILABLE", "r")
        level, msg = trading_doctor.check_yolo_scan_health(PROFILE)
        self.assertEqual(level, "warn")
        for flag in ("./scripts/report_issue.sh", "--repro", "--output-file", "--severity MEDIUM"):
            self.assertIn(flag, msg)

    def test_market_radar_skill_documents_the_gates(self):
        with open(os.path.join(BASE_DIR, ".agents", "skills", "market-radar", "SKILL.md"), encoding="utf-8") as f:
            skill = f.read()
        for text in ("If `gate_ok` is false, NEVER propose, recommend or forward that row.",
                     "SL distance × leverage ≤ 0.35", "3.75 USDT", "desk floors, not executor gates",
                     "(ask − bid) / mid × 100", "ambiguous_symbols", "wide_spread", "market_data_status",
                     "underlyingSubType", "tickSize"):
            self.assertIn(text, skill)
        self.assertNotIn("mirroring the PROD executor gates", skill)


if __name__ == "__main__":
    unittest.main()
