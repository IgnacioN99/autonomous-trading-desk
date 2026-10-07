#!/usr/bin/env python3
"""
test_issue_66_yolo_scan_hardening.py - Issue #66: YOLO scan inside screening_pipeline.

1. Side-effect contract of broad_yolo_scanner.scan_yolo (the CLI may os._exit while the scan thread runs).
2. UNAVAILABLE runs persisted in logs/yolo_scan_health.json (pipeline + brief) and surfaced by trading_doctor.py.
3. Request weight: HTTP 429/418 stop the scan (RateLimitedError, no CORE_MEMES fallback), caller cancellation,
   8 workers, X-MBX-USED-WEIGHT-1M observability.
4. Spread- and ATR-aware trigger buffer (one bookTicker request).

No network: urllib is blocked for the whole module and mocked per test. Every state file is redirected to a
temporary directory (the real logs/ is never written).
"""

import atexit
import builtins
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Explicit pre-imports of everything scan_yolo imports lazily, so the atexit baseline of the contract test is taken
# after any import-time registration (no unguarded warm-up scan).
import numpy  # noqa: F401
import user_profile  # noqa: F401
import quant_risk_engine as qre
import execute_futures_trade as eft
import broad_yolo_scanner as bys
import prime_evaluator_brief as peb
import screening_pipeline as sp
import trading_doctor
from utils import atomic_writer
from utils import rate_limit_guard as rlg
from utils import yolo_scan_health as ysh

PROFILE = {
    "profile_completed": True, "risk_pct_equity": 0.005, "max_margin_ratio": 0.30, "yolo_slot_enabled": True,
    "yolo_equity_pct": 0.001, "leverage_standard": 3, "leverage_yolo": 7, "leverage_ceiling": 10,
    "operating_mode": "BALANCED_DELTA_NEUTRAL",
}
DISABLED_PROFILE = dict(PROFILE, yolo_slot_enabled=False)


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch, _ban_dir, _ban_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()
    # The pipeline enables the process-wide rate-limit guard: its ban file lives in a temp dir (never logs/).
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
def _klines(side="LONG", high=1.005, low=0.975, close=1.001, vol=300, n=40):
    """Choppy drift, a signal candle (the last CLOSED one, klines[-2]) and a neutral forming candle inside its range
    (issue #85). Defaults: LONG with a ~3.8% stop that passes every gate at 7x."""
    ks, price = [], 1.0
    up, down = (1.002, 0.997) if side == "LONG" else (1.003, 0.998)
    for i in range(n - 1):
        o = price
        c = price * (up if i % 2 == 0 else down)
        ks.append([i, str(o), str(max(o, c) * 1.001), str(min(o, c) * 0.999), str(c), "100"])
        price = c
    o = price
    ks.append([n, str(o), str(o * high), str(o * low), str(o * close), str(vol)])
    f = o * close
    ks.append([n + 1, str(f), str(f * 1.0005), str(f * 0.9995), str(f), "100"])
    return ks


def _exchange_info(symbols, subtype=("Meme", "Crypto"), tick="0.0000001"):
    """exchangeInfo rows (shape of logs/issue_work/exchange_info_sample.json) tagged as Binance memecoins."""
    return {"symbols": [{"symbol": s, "pair": s, "contractType": "PERPETUAL", "status": "TRADING",
                         "baseAsset": s[:-4], "quoteAsset": "USDT", "underlyingType": "COIN",
                         "underlyingSubType": list(subtype),
                         "filters": [{"filterType": "PRICE_FILTER", "tickSize": tick}]} for s in symbols]}


class _Resp:
    def __init__(self, payload, headers=None):
        self._body = json.dumps(payload).encode()
        if headers is not None:
            self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code):
    return lambda url: urllib.error.HTTPError(url, code, "rate limit", None, None)


class _Router:
    """urlopen stand-in routed by URL substring; values are payloads, _Resp, exceptions or callables(url)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, req, *args, **kwargs):
        url = getattr(req, "full_url", req)
        with self._lock:
            self.calls.append((req, args, kwargs))
        for key, value in self.routes:
            if key in url:
                if callable(value) and not isinstance(value, (_Resp, BaseException)):
                    value = value(url)
                if isinstance(value, BaseException):
                    raise value
                return value if isinstance(value, _Resp) else _Resp(value)
        raise urllib.error.URLError(f"no mocked route for {url}")

    def count(self, key):
        with self._lock:
            return sum(1 for req, _, _ in self.calls if key in getattr(req, "full_url", req))


def _book(symbols, bid=0.999, ask=1.001):
    return [{"symbol": s, "bidPrice": str(bid), "bidQty": "1", "askPrice": str(ask), "askQty": "1", "time": 1}
            for s in symbols]


def _routes(symbols=("WIFUSDT",), klines=None, book=None, info=None):
    return [("/fapi/v1/exchangeInfo", _exchange_info(symbols) if info is None else info),
            ("ticker/bookTicker", _book(symbols) if book is None else book),
            ("/fapi/v1/klines", (lambda url: _klines()) if klines is None else klines)]


@contextlib.contextmanager
def _scanner_env(router, profile=PROFILE):
    with patch("urllib.request.urlopen", side_effect=router), \
         patch("user_profile.load_user_profile", return_value=dict(profile)), \
         patch("quant_risk_engine.get_account_equity", return_value=12000.0):
        yield


def _audit_row(price=1.0, atr_pct=0.5):
    return {"symbol": "WIFUSDT", "price": price, "rsi": 40.0, "vol_ratio": 2.6, "lower_wick": 30.0,
            "upper_wick": 30.0, "atr_pct": atr_pct, "score_long": 80.0, "score_short": 80.0,
            "atr": price * atr_pct / 100, "high": price * 1.01, "low": price * 0.99}


SIZING = {"margin_usdt": 12.0, "leverage": 7, "leverage_ceiling": 10, "margin_mode": "ISOLATED",
          "yolo_slot_enabled": True}


class _HealthFileTest(unittest.TestCase):
    """Redirects logs/yolo_scan_health.json to a temp dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rlg.reset_for_tests()
        self.addCleanup(rlg.reset_for_tests)
        self.tmp = tmp.name
        self.health_file = os.path.join(self.tmp, "logs", "yolo_scan_health.json")
        p = patch.object(ysh, "HEALTH_FILE", self.health_file)
        p.start()
        self.addCleanup(p.stop)

    def health(self):
        with open(self.health_file, encoding="utf-8") as f:
            return json.load(f)


# =============================================================================
# 1. Side-effect contract (os._exit safety)
# =============================================================================
# Read-only endpoints scan_yolo may touch: market data, plus the account reads behind the YOLO margin
# (get_yolo_margin -> get_account_equity: KEYS = GET /fapi/v1/time + signed GET /fapi/v2/balance; MCP = one
# read-only tools/call on the Agentic gateway).
FAPI_READ_PATHS = {"/fapi/v1/exchangeInfo", "/fapi/v1/ticker/bookTicker", "/fapi/v1/klines", "/fapi/v1/time",
                   "/fapi/v2/balance"}
MCP_READ_TOOLS = {"futures_usds.futuresAccountBalanceV3"}


@contextlib.contextmanager
def _write_guards(violations):
    """Records (and raises on) any file write during the block. Violations are recorded because audit_symbol
    swallows exceptions."""
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(c in str(mode) for c in "wax+"):
            violations.append(f"open({file!r}, {mode!r})")
            raise AssertionError("write-mode open during scan_yolo")
        return real_open(file, mode, *args, **kwargs)

    def forbidden(name):
        def _f(*args, **kwargs):
            violations.append(name)
            raise AssertionError(f"{name} during scan_yolo")
        return _f

    with patch("builtins.open", side_effect=guarded_open), \
         patch.object(atomic_writer, "atomic_write_json", side_effect=forbidden("atomic_write_json")), \
         patch.object(atomic_writer, "atomic_append_jsonl", side_effect=forbidden("atomic_append_jsonl")), \
         patch.object(ysh, "atomic_write_json", side_effect=forbidden("yolo_scan_health.atomic_write_json")), \
         patch.object(ysh, "record_scan", side_effect=forbidden("record_scan")), \
         patch("os.replace", side_effect=forbidden("os.replace")):
        yield


class TestScanSideEffectContract(unittest.TestCase):

    def test_contract_is_documented(self):
        doc = bys.scan_yolo.__doc__
        for text in ("SIDE-EFFECT CONTRACT", "only read-only requests", "read-only MCP `tools/call`",
                     "no order or other write endpoints (REST or MCP)", "os.makedirs of the config dir",
                     "no atexit handlers or finalizers"):
            self.assertIn(text, doc)
        exit_doc = sp._exit_without_waiting_for_yolo.__doc__
        self.assertIn("side-effect contract of broad_yolo_scanner.scan_yolo", exit_doc)
        self.assertIn("MCP tools/call", " ".join(exit_doc.split()))
        self.assertIn("including a read-only MCP tools/call", " ".join(exit_doc.split()))
        self.assertIn("os._exit", exit_doc)

    def _market_routes(self, symbols=("WIFUSDT", "1000PEPEUSDT", "DOGEUSDT")):
        headers = {"X-MBX-USED-WEIGHT-1M": "120"}
        return [("/fapi/v1/exchangeInfo", _Resp(_exchange_info(symbols), headers)),
                ("ticker/bookTicker", _Resp(_book(symbols), headers)),
                ("/fapi/v1/klines", lambda url: _Resp(_klines(), headers))]

    def _assert_read_only(self, router):
        for req, args, kwargs in router.calls:
            self.assertIsInstance(req, urllib.request.Request)
            self.assertEqual(args, ())
            self.assertNotIn("data", kwargs)
            url = urllib.parse.urlsplit(req.full_url)
            if url.netloc == "agent.binance.com":
                self.assertEqual(url.path, "/mcp/agentic")
                body = json.loads(req.data.decode("utf-8"))
                self.assertEqual(body["method"], "tools/call")
                self.assertIn(body["params"]["name"], MCP_READ_TOOLS)
            else:
                self.assertEqual(url.netloc, "fapi.binance.com", req.full_url)
                self.assertEqual(req.get_method(), "GET")
                self.assertIsNone(req.data)
                self.assertIn(url.path, FAPI_READ_PATHS)

    def test_scan_only_issues_gets_and_writes_nothing(self):
        violations = []
        router = _Router(self._market_routes())
        n_atexit = atexit._ncallbacks() if hasattr(atexit, "_ncallbacks") else None
        # Real user_profile loader (its only write is the allowed os.makedirs of config/); equity mocked.
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("quant_risk_engine.get_account_equity", return_value=12000.0), \
             _write_guards(violations):
            payload = bys.scan_yolo("prod", interval="15m", top=3)
        self.assertEqual(violations, [])
        self.assertEqual(payload["scanned"], 3)  # the scan really ran (every kline audit succeeded)
        self.assertGreaterEqual(len(router.calls), 5)
        self._assert_read_only(router)
        for req, _, _ in router.calls:
            self.assertTrue(req.full_url.startswith(bys.BASE_FAPI + "/fapi/v1/"), req.full_url)
        if n_atexit is not None:
            self.assertEqual(atexit._ncallbacks(), n_atexit)

    def _scan_with_real_equity(self, client_config, extra_routes):
        """scan_yolo with get_account_equity NOT mocked: no session_state.json (missing temp path), so the equity
        comes from the ledger read path of execute_futures_trade."""
        violations = []
        router = _Router(extra_routes + self._market_routes())
        n_atexit = atexit._ncallbacks() if hasattr(atexit, "_ncallbacks") else None
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(qre, "__file__", os.path.join(tmp, "scripts", "quant_risk_engine.py")), \
             patch("urllib.request.urlopen", side_effect=router), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch.object(eft, "get_client_config", return_value=client_config), \
             patch.object(eft, "get_mcp_oauth_token", return_value="test-token"), \
             patch.dict(eft._SERVER_OFFSET, clear=True), \
             _write_guards(violations):
            self.assertFalse(os.path.exists(os.path.join(tmp, "logs", "session_state.json")))
            payload = bys.scan_yolo("prod", interval="15m", top=3)
        self.assertEqual(violations, [])
        self.assertEqual(payload["sizing"]["margin_usdt"], 12.0)  # 0.1% of the 12,000 ledger equity -> [10, 15]
        self.assertEqual(payload["scanned"], 3)
        self._assert_read_only(router)
        if n_atexit is not None:
            self.assertEqual(atexit._ncallbacks(), n_atexit)
        return router

    def test_keys_mode_account_read_is_signed_get_balance(self):
        balance = [{"asset": "USDT", "balance": "12000.0", "availableBalance": "12000.0"}]
        router = self._scan_with_real_equity(
            ("key12345678", "sec12345678", "https://fapi.binance.com"),
            [("/fapi/v1/time", {"serverTime": int(time.time() * 1000)}), ("/fapi/v2/balance", balance)])
        self.assertEqual(router.count("/fapi/v2/balance"), 1)
        self.assertEqual(router.count("/fapi/v1/time"), 1)
        self.assertEqual(router.count("agent.binance.com"), 0)

    def test_mcp_mode_account_read_is_a_read_only_tools_call(self):
        balance = [{"asset": "USDT", "balance": "12000.0", "availableBalance": "12000.0"}]
        mcp = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(balance)}]}}
        router = self._scan_with_real_equity(("MCP_OAUTH_ACTIVE", "test-token", "https://fapi.binance.com"),
                                             [("agent.binance.com/mcp/agentic", mcp)])
        self.assertEqual(router.count("agent.binance.com/mcp/agentic"), 1)
        self.assertEqual(router.count("/fapi/v2/balance"), 0)
        self.assertEqual(router.count("/fapi/v1/time"), 0)


# =============================================================================
# 2. Health file, pipeline and brief recording, doctor
# =============================================================================
class TestHealthFile(_HealthFileTest):

    def test_increments_on_unavailable_and_resets_on_other_status(self):
        self.assertEqual(ysh.read_health(), {})
        ysh.record_scan("UNAVAILABLE", "YOLO scan rate-limited by Binance", now=100.0)
        ysh.record_scan("UNAVAILABLE", "YOLO scan failed (RuntimeError)", now=200.0)
        h = self.health()
        self.assertEqual(h["consecutive_unavailable"], 2)
        self.assertEqual(h["last_unavailable_reason"], "YOLO scan failed (RuntimeError)")
        self.assertEqual(h["last_unavailable_ts"], 200.0)
        self.assertEqual((h["last_status"], h["updated_ts"]), ("UNAVAILABLE", 200.0))
        self.assertNotIn("last_ok_ts", h)
        for i, status in enumerate(("INACTIVE", "ACTIVE", "DISABLED")):
            ysh.record_scan("UNAVAILABLE", "x", now=300.0 + i)
            ysh.record_scan(status, now=400.0 + i)
            h = ysh.read_health()
            self.assertEqual(h["consecutive_unavailable"], 0, status)
            self.assertEqual((h["last_status"], h["last_ok_ts"], h["updated_ts"]), (status, 400.0 + i, 400.0 + i))
            self.assertEqual(h["last_unavailable_reason"], "x")  # kept for the operator
        self.assertEqual(ysh.YOLO_UNAVAILABLE_WARN_AFTER, 3)
        self.assertEqual(os.path.basename(ysh.HEALTH_FILE), "yolo_scan_health.json")

    def test_default_path_is_workspace_logs(self):
        self.assertEqual(os.path.normpath(ysh.LOGS_DIR), os.path.join(BASE_DIR, "logs"))

    def test_corrupt_file_reads_empty_and_is_rewritten(self):
        os.makedirs(os.path.dirname(self.health_file), exist_ok=True)
        with open(self.health_file, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(ysh.read_health(), {})
        ysh.record_scan("UNAVAILABLE", "r")
        self.assertEqual(self.health()["consecutive_unavailable"], 1)

    def test_fail_open_on_unwritable_path(self):
        blocker = os.path.join(self.tmp, "not_a_dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        err = io.StringIO()
        with patch.object(ysh, "HEALTH_FILE", os.path.join(blocker, "sub", "yolo_scan_health.json")), \
             patch("sys.stderr", err):
            ysh.record_scan("UNAVAILABLE", "r")  # must not raise
            self.assertEqual(ysh.read_health(), {})
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("yolo_scan_health", err.getvalue())


def _macro():
    return sp.MacroContext(btc_price=60000.0, btc_regime="NEUTRAL", btc_regime_desc="", btc_absorption="NONE",
                           btc_taker_ratio=1.0, btc_cvd_30v=0.0, btc_oi_z_score=0.0, btc_tape_bias="BALANCED",
                           btc_tape_imbalance=0.0, allows_alt_shorts=True)


def _scan_payload(slot_status="EMPTY", longs=None):
    longs = list(longs or [])
    return {"status": "ok", "command": "yolo", "env": "prod", "interval": "15m", "slot_status": slot_status,
            "sizing": SIZING, "recommendation": longs[0] if longs else None, "longs": longs, "shorts": []}


class _PipelineTest(_HealthFileTest):
    """Offline fakes for every non-YOLO pipeline task."""

    def setUp(self):
        super().setUp()
        patches = [
            patch("screening_pipeline.fetch_macro_btc", return_value=_macro()),
            patch("broad_market_radar.scan_all_liquid_pairs", return_value=[]),
            patch("quant_risk_engine.scan_coingrated_market_pairs", return_value=[]),
            patch("funding_arbitrage.scan_top_funding_opportunities", return_value=[]),
            patch("screening_pipeline.fetch_news_summary", return_value=[]),
            patch("sync_session_state.sync_session_state", return_value={"portfolio_exposure": {}, "active_positions": []}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_pipeline(self, profile=PROFILE, **kwargs):
        with patch("user_profile.load_user_profile", return_value=dict(profile)), \
             patch("sys.stderr", io.StringIO()):
            return sp.execute_screening_pipeline(target_env="prod", **kwargs)


class TestPipelineRecordsHealth(_PipelineTest):

    def test_unavailable_runs_increment_and_empty_or_active_resets(self):
        for _ in range(3):
            with patch("broad_yolo_scanner.scan_yolo", side_effect=RuntimeError("server text <ignore me>")):
                payload = self.run_pipeline()
            self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        h = self.health()
        self.assertEqual(h["consecutive_unavailable"], 3)
        self.assertEqual(h["last_unavailable_reason"], "YOLO scan failed (RuntimeError)")
        self.assertNotIn("ignore me", json.dumps(h))
        with patch("broad_yolo_scanner.scan_yolo", return_value=_scan_payload("EMPTY")):
            self.assertEqual(self.run_pipeline().yolo_slot.status, "INACTIVE")
        self.assertEqual(self.health()["consecutive_unavailable"], 0)
        self.assertEqual(self.health()["last_status"], "INACTIVE")

        with patch("broad_yolo_scanner.scan_yolo", side_effect=RuntimeError("x")):
            self.run_pipeline()
        row = bys.build_levels(dict(_audit_row(atr_pct=1.8), vol_ratio=2.6), "LONG", SIZING)
        with patch("broad_yolo_scanner.scan_yolo", return_value=_scan_payload("CANDIDATE", longs=[row])):
            self.assertEqual(self.run_pipeline().yolo_slot.status, "ACTIVE")
        self.assertEqual((self.health()["consecutive_unavailable"], self.health()["last_status"]), (0, "ACTIVE"))

    def test_include_yolo_false_never_records(self):
        with patch("broad_yolo_scanner.scan_yolo") as scan, \
             patch.object(ysh, "record_scan", wraps=ysh.record_scan) as rec:
            payload = self.run_pipeline(include_yolo=False)
        scan.assert_not_called()
        rec.assert_not_called()
        self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        self.assertFalse(os.path.exists(self.health_file))

    def _recorded_once(self, **scan_patch):
        with patch("broad_yolo_scanner.scan_yolo", **scan_patch), \
             patch.object(ysh, "record_scan", wraps=ysh.record_scan) as rec:
            payload = self.run_pipeline()
        self.assertEqual(rec.call_count, 1)
        return payload, rec.call_args.args

    def test_records_exactly_once_per_run_on_every_path(self):
        _, args = self._recorded_once(return_value=_scan_payload("EMPTY"))
        self.assertEqual(args, ("INACTIVE", None))
        _, args = self._recorded_once(side_effect=RuntimeError("boom"))
        self.assertEqual(args, ("UNAVAILABLE", "YOLO scan failed (RuntimeError)"))
        payload, args = self._recorded_once(side_effect=bys.RateLimitedError("Binance rate limit (HTTP 429)"))
        self.assertEqual(args, ("UNAVAILABLE", "YOLO scan rate-limited by Binance"))
        self.assertEqual(payload.yolo_slot_status, "UNAVAILABLE: YOLO scan rate-limited by Binance. YOLO slot kept empty.")
        _, args = self._recorded_once(return_value=_scan_payload("CANDIDATE_SLOT_DISABLED"))
        self.assertEqual(args, ("DISABLED", None))

    def test_timeout_records_once_and_cancels_the_scan(self):
        seen = {}

        def _hang(*args, **kwargs):
            ev = kwargs.get("cancel_event")
            seen["event"] = ev
            seen["cancelled"] = ev.wait(10) if ev is not None else False
            return _scan_payload("EMPTY")

        with patch.object(sp, "YOLO_SCAN_TIMEOUT_S", 0.3):
            payload, args = self._recorded_once(side_effect=_hang)
        self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        self.assertEqual(args, ("UNAVAILABLE", f"YOLO scan exceeded its {0.3}s budget"))
        self.assertIsInstance(seen.get("event"), threading.Event)
        self.assertTrue(seen["event"].is_set())
        sp._last_yolo_future.result(timeout=5)
        self.assertTrue(seen["cancelled"])

    def test_standard_scan_failure_cancels_the_running_yolo_scan(self):
        seen = {}
        started = threading.Event()

        def _hang(*args, **kwargs):
            ev = kwargs.get("cancel_event")
            seen["event"] = ev
            started.set()
            seen["cancelled"] = ev.wait(10) if ev is not None else False
            return _scan_payload("EMPTY")

        def _radar_fails_once_scan_runs(*args, **kwargs):
            self.assertTrue(started.wait(5), "the patched YOLO scan never started")
            raise RuntimeError("radar down")

        with patch("broad_yolo_scanner.scan_yolo", side_effect=_hang), \
             patch("broad_market_radar.scan_all_liquid_pairs", side_effect=_radar_fails_once_scan_runs), \
             patch.object(ysh, "record_scan", wraps=ysh.record_scan) as rec, \
             self.assertRaises(RuntimeError):
            self.run_pipeline()
        sp._last_yolo_future.result(timeout=5)
        self.assertTrue(seen["event"].is_set())
        self.assertTrue(seen["cancelled"])
        rec.assert_not_called()  # the crashed run is counted by prime_evaluator_brief, not here

    def test_standard_scan_success_does_not_cancel_the_scan(self):
        with patch("broad_yolo_scanner.scan_yolo", return_value=_scan_payload("EMPTY")):
            self.run_pipeline()
        self.assertFalse(sp._last_yolo_future.yolo_cancel_event.is_set())

    def test_record_failure_never_breaks_the_pipeline(self):
        with patch("broad_yolo_scanner.scan_yolo", return_value=_scan_payload("EMPTY")), \
             patch.object(ysh, "atomic_write_json", side_effect=OSError("read-only fs")):
            payload = self.run_pipeline()
        self.assertEqual(payload.yolo_slot.status, "INACTIVE")


class TestBriefMarksPipelineFailure(_HealthFileTest):

    def _assemble(self, risk_profile, run_result=None, run_side_effect=None):
        brief_file = os.path.join(self.tmp, "primed_brief.json")
        run = patch("prime_evaluator_brief.subprocess.run", return_value=run_result, side_effect=run_side_effect)
        with patch.object(peb, "BRIEF_FILE", brief_file), \
             patch.object(peb, "ensure_fresh_state", return_value={"target_env": "prod"}), \
             patch.object(peb, "load_recent_insights", return_value=[]), \
             patch.object(peb, "build_risk_profile", return_value=dict(risk_profile)), \
             run as mock_run:
            brief = peb.assemble_primed_brief(target_env="prod")
        mock_run.assert_called_once()
        self.assertIn("screening_pipeline.py", " ".join(mock_run.call_args.args[0]))
        return brief

    def test_failed_pipeline_with_slot_enabled_is_unavailable_and_recorded(self):
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout='{"status": "error"}', stderr="boom")
        for kwargs in ({"run_result": failed}, {"run_side_effect": subprocess.TimeoutExpired("x", 60)},
                       {"run_result": subprocess.CompletedProcess(args=[], returncode=0, stdout="[1, 2]")}):
            with self.subTest(**{k: type(v).__name__ for k, v in kwargs.items()}):
                brief = self._assemble({"yolo_slot_enabled": True}, **kwargs)
                self.assertEqual(brief["yolo_slot"], {"status": "UNAVAILABLE",
                                                      "summary": "UNAVAILABLE: screening pipeline failed. YOLO slot kept empty.",
                                                      "candidates": []})
                self.assertIn("UNAVAILABLE: screening pipeline failed.", peb.format_markdown_brief(brief))
        h = self.health()
        self.assertEqual(h["consecutive_unavailable"], 3)
        self.assertEqual(h["last_unavailable_reason"], "screening pipeline failed")

    def test_no_double_count_when_the_pipeline_already_recorded(self):
        def _pipeline_records_then_fails(*args, **kwargs):
            # The subprocess got this far and recorded this run (its DESK_SCAN_RUN_ID) as UNAVAILABLE (issue #91.6).
            ysh.record_scan("UNAVAILABLE", "YOLO scan failed (RuntimeError)", run_id=kwargs["env"][ysh.RUN_ID_ENV])
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="crash after the YOLO wait")

        brief = self._assemble({"yolo_slot_enabled": True}, run_side_effect=_pipeline_records_then_fails)
        self.assertEqual(brief["yolo_slot"]["status"], "UNAVAILABLE")
        h = self.health()
        self.assertEqual(h["consecutive_unavailable"], 1)
        self.assertEqual(h["last_unavailable_reason"], "YOLO scan failed (RuntimeError)")

    def test_record_from_an_earlier_run_does_not_suppress_the_count(self):
        ysh.record_scan("UNAVAILABLE", "YOLO scan rate-limited by Binance", now=time.time() - 120)
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        self._assemble({"yolo_slot_enabled": True}, run_result=failed)
        h = self.health()
        self.assertEqual(h["consecutive_unavailable"], 2)
        self.assertEqual(h["last_unavailable_reason"], "screening pipeline failed")

    def test_failed_pipeline_with_slot_disabled_shows_disabled(self):
        """Issue #91.8c: the DISABLED text, not "INACTIVE: Preserving capital."."""
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        brief = self._assemble({"yolo_slot_enabled": False}, run_result=failed)
        self.assertEqual(brief["yolo_slot"], {"status": "DISABLED", "summary": sp.YOLO_DISABLED_STATUS,
                                              "candidates": []})
        self.assertEqual(sp.YOLO_DISABLED_STATUS, "DISABLED: yolo_slot_enabled is false in the user profile.")
        self.assertFalse(os.path.exists(self.health_file))

    def test_usable_payload_is_not_overridden_or_recorded(self):
        def _ok(*args, **kwargs):
            payload = {"yolo_slot_status": "INACTIVE: Preserving capital. x", "yolo_slot": {"status": "INACTIVE"},
                       "run_id": kwargs["env"][ysh.RUN_ID_ENV]}  # the pipeline echoes this run's id
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload))

        brief = self._assemble({"yolo_slot_enabled": True}, run_side_effect=_ok)
        self.assertEqual(brief["yolo_slot"]["status"], "INACTIVE")
        self.assertFalse(os.path.exists(self.health_file))


class TestDoctorYoloScanCheck(_HealthFileTest):

    def _write(self, count, reason="YOLO scan rate-limited by Binance"):
        for _ in range(count):
            ysh.record_scan("UNAVAILABLE", reason)

    def test_check_levels(self):
        self.assertEqual(trading_doctor.check_yolo_scan_health(PROFILE)[0], "info")  # no file yet
        self.assertIn("No YOLO scan recorded yet", trading_doctor.check_yolo_scan_health(PROFILE)[1])
        self._write(2)
        self.assertEqual(trading_doctor.check_yolo_scan_health(PROFILE)[0], "ok")
        self._write(1)
        level, msg = trading_doctor.check_yolo_scan_health(PROFILE)
        self.assertEqual(level, "warn")
        self.assertIn("3 consecutive runs", msg)
        self.assertIn("YOLO scan rate-limited by Binance", msg)
        self.assertIn("./scripts/report_issue.sh --category tool_error --severity MEDIUM", msg)
        self.assertEqual(trading_doctor.check_yolo_scan_health(DISABLED_PROFILE)[0], "skip")

    def _run_doctor(self, profile):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        out = io.StringIO()
        with patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.get_client_config",
                   return_value=("key12345678", "sec12345678", "http://binance.mock")), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("execute_futures_trade.send_signed_request",
                   side_effect=lambda method, endpoint, params=None, target_env=None: [
                       {"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}]
                   if endpoint == "/fapi/v2/balance" else []), \
             patch("user_profile.load_user_profile", return_value=dict(profile)), \
             patch("trading_doctor.check_pretool_hook",
                   return_value={"ok": True, "critical": [], "warnings": [], "info": []}), \
             patch("shadow_tracker.calculate_efficacy_metrics", side_effect=RuntimeError("skip")), \
             contextlib.redirect_stdout(out):
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            code = trading_doctor.run_doctor(target_env="testnet")
        return code, out.getvalue()

    def test_doctor_warns_at_three_without_changing_exit_code(self):
        self._write(3)
        code, out = self._run_doctor(PROFILE)
        self.assertEqual(code, 0, out)
        self.assertIn("⚠️  [YOLO_SCAN] YOLO scan UNAVAILABLE in 3 consecutive runs", out)
        self.assertIn("STATUS: OPERATIONAL WITH WARNINGS", out)
        self.assertNotIn("SYSTEM DISABLED", out)

    def test_doctor_ok_below_threshold(self):
        self._write(2)
        code, out = self._run_doctor(PROFILE)
        self.assertEqual(code, 0, out)
        self.assertIn("✅ [YOLO_SCAN]", out)
        self.assertNotIn("⚠️  [YOLO_SCAN]", out)

    def test_doctor_silent_when_slot_disabled(self):
        self._write(5)
        code, out = self._run_doctor(DISABLED_PROFILE)
        self.assertEqual(code, 0, out)
        self.assertNotIn("[YOLO_SCAN]", out)
        self.assertNotIn("consecutive runs", out)

    def test_doctor_info_line_when_no_scan_recorded(self):
        code, out = self._run_doctor(PROFILE)
        self.assertEqual(code, 0, out)
        self.assertIn("ℹ️  [YOLO_SCAN] No YOLO scan recorded yet.", out)


# =============================================================================
# 3. Rate limits, cancellation, workers, weight header
# =============================================================================
class TestRateLimit(unittest.TestCase):

    def setUp(self):
        rlg.reset_for_tests()
        self.addCleanup(rlg.reset_for_tests)

    def test_universe_fetch_raises_on_429_and_418_without_core_memes_fallback(self):
        for code in (429, 418):
            with self.subTest(code=code):
                router = _Router([("/fapi/v1/exchangeInfo", _http_error(code))])
                with patch("urllib.request.urlopen", side_effect=router), \
                     self.assertRaises(bys.RateLimitedError) as ctx:
                    bys.get_yolo_universe()
                self.assertIn(f"HTTP {code}", str(ctx.exception))
                self.assertIsInstance(ctx.exception, RuntimeError)
        # Other errors keep the CORE_MEMES (allowlist) fallback, without tick sizes.
        router = _Router([("/fapi/v1/exchangeInfo", _http_error(500))])
        with patch("urllib.request.urlopen", side_effect=router), patch("sys.stderr", io.StringIO()):
            self.assertEqual(bys.get_yolo_universe(), (list(bys.CORE_MEMES), False, {}))

    def test_universe_429_aborts_scan_before_any_other_request(self):
        router = _Router([("/fapi/v1/exchangeInfo", _http_error(429))] + _routes()[1:])
        with _scanner_env(router), self.assertRaises(bys.RateLimitedError):
            bys.scan_yolo("prod")
        self.assertEqual(len(router.calls), 1)

    def test_kline_429_stops_further_requests_and_scan_raises(self):
        symbols = [f"MEME{i}USDT" for i in range(30)]
        router = _Router(_routes(symbols, klines=_http_error(429)))
        with _scanner_env(router), patch.object(bys, "YOLO_SCAN_WORKERS", 1), \
             self.assertRaises(bys.RateLimitedError) as ctx:
            bys.scan_yolo("prod")
        self.assertEqual(str(ctx.exception), "Binance rate limit (HTTP 429)")
        self.assertEqual(router.count("/fapi/v1/klines"), 1)  # sequential: nothing after the first 429

        router = _Router(_routes(symbols, klines=_http_error(418)))
        with _scanner_env(router), self.assertRaises(bys.RateLimitedError) as ctx:
            bys.scan_yolo("prod")
        self.assertIn("HTTP 418", str(ctx.exception))
        self.assertLessEqual(router.count("/fapi/v1/klines"), bys.YOLO_SCAN_WORKERS)  # at most the in-flight ones
        self.assertLess(router.count("/fapi/v1/klines"), len(symbols))

    def test_one_429_among_successes_still_fails_the_scan(self):
        symbols = [f"MEME{i}USDT" for i in range(10)]
        calls = {"n": 0}

        def klines(url):
            calls["n"] += 1
            return urllib.error.HTTPError(url, 429, "x", None, None) if calls["n"] == 3 else _klines()

        router = _Router(_routes(symbols, klines=klines))
        with _scanner_env(router), patch.object(bys, "YOLO_SCAN_WORKERS", 1), \
             self.assertRaises(bys.RateLimitedError):
            bys.scan_yolo("prod")
        self.assertEqual(router.count("/fapi/v1/klines"), 3)

    def test_book_ticker_429_raises(self):
        router = _Router(_routes(book=_http_error(429)))
        with _scanner_env(router), self.assertRaises(bys.RateLimitedError):
            bys.scan_yolo("prod")
        self.assertEqual(router.count("/fapi/v1/klines"), 0)

    def test_pipeline_reports_fixed_rate_limit_reason(self):
        router = _Router(_routes(["WIFUSDT", "DOGEUSDT"], klines=_http_error(429)))
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ysh, "HEALTH_FILE", os.path.join(tmp, "yolo_scan_health.json")), \
             patch("urllib.request.urlopen", side_effect=router), \
             patch("quant_risk_engine.get_account_equity", return_value=12000.0), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch("screening_pipeline.fetch_macro_btc", return_value=_macro()), \
             patch("broad_market_radar.scan_all_liquid_pairs", return_value=[]), \
             patch("quant_risk_engine.scan_coingrated_market_pairs", return_value=[]), \
             patch("funding_arbitrage.scan_top_funding_opportunities", return_value=[]), \
             patch("screening_pipeline.fetch_news_summary", return_value=[]), \
             patch("sync_session_state.sync_session_state", return_value={}), \
             patch("sys.stderr", io.StringIO()):
            payload = sp.execute_screening_pipeline(target_env="prod")
            health = ysh.read_health()
        self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        self.assertEqual(payload.yolo_slot_status, "UNAVAILABLE: YOLO scan rate-limited by Binance. YOLO slot kept empty.")
        self.assertEqual(health["last_unavailable_reason"], "YOLO scan rate-limited by Binance")
        self.assertEqual(health["consecutive_unavailable"], 1)

    def test_scan_uses_eight_workers(self):
        self.assertEqual(bys.YOLO_SCAN_WORKERS, 8)
        seen = {}
        real_tpe = bys.ThreadPoolExecutor

        def spy(*args, **kwargs):
            seen["max_workers"] = kwargs.get("max_workers", args[0] if args else None)
            return real_tpe(*args, **kwargs)

        with _scanner_env(_Router(_routes())), patch.object(bys, "ThreadPoolExecutor", side_effect=spy):
            bys.scan_yolo("prod")
        self.assertEqual(seen["max_workers"], 8)


class TestCancellation(unittest.TestCase):

    def test_no_new_requests_after_cancel_event_is_set(self):
        symbols = [f"MEME{i}USDT" for i in range(20)]
        cancel = threading.Event()
        calls = {"n": 0}

        def klines(url):
            calls["n"] += 1
            if calls["n"] == 3:
                cancel.set()
            return _klines()

        router = _Router(_routes(symbols, klines=klines))
        with _scanner_env(router), patch.object(bys, "YOLO_SCAN_WORKERS", 1), \
             self.assertRaises(bys.ScanCancelledError):
            bys.scan_yolo("prod", cancel_event=cancel)
        self.assertEqual(router.count("/fapi/v1/klines"), 3)

    def test_cancel_before_start_issues_no_request(self):
        cancel = threading.Event()
        cancel.set()
        router = _Router(_routes())
        with _scanner_env(router), self.assertRaises(bys.ScanCancelledError):
            bys.scan_yolo("prod", cancel_event=cancel)
        self.assertEqual(router.calls, [])

    def test_pipeline_passes_a_cancel_event_to_the_scan(self):
        with patch("broad_yolo_scanner.scan_yolo", return_value=_scan_payload()) as scan:
            fut = sp._start_yolo_scan("prod")
            fut.result(timeout=5)
        ev = scan.call_args.kwargs.get("cancel_event")
        self.assertIsInstance(ev, threading.Event)
        self.assertIs(fut.yolo_cancel_event, ev)
        self.assertFalse(ev.is_set())


class TestWeightHeader(unittest.TestCase):

    def _scan(self, weights):
        it = iter(weights)
        lock = threading.Lock()

        def resp(payload):
            with lock:
                w = next(it, None)
            return _Resp(payload, {"X-MBX-USED-WEIGHT-1M": str(w)} if w is not None else {})

        symbols = ["WIFUSDT", "DOGEUSDT"]
        routes = [("/fapi/v1/exchangeInfo", lambda url: resp(_exchange_info(symbols))),
                  ("ticker/bookTicker", lambda url: resp(_book(symbols))),
                  ("/fapi/v1/klines", lambda url: resp(_klines()))]
        err = io.StringIO()
        with _scanner_env(_Router(routes)), patch("sys.stderr", err):
            payload = bys.scan_yolo("prod")
        return payload, err.getvalue()

    def test_max_used_weight_captured_without_warning_below_threshold(self):
        payload, err = self._scan(["45", "50", "1799", "60"])
        self.assertEqual(payload["max_used_weight_1m"], 1799)
        self.assertNotIn("request weight", err)

    def test_warning_at_threshold_printed_once(self):
        payload, err = self._scan(["1800", "1900", "1850", "100"])
        self.assertEqual(payload["max_used_weight_1m"], 1900)
        self.assertEqual(err.count("request weight"), 1)
        self.assertEqual(bys.WEIGHT_WARN_THRESHOLD, 1800)

    def test_missing_headers_and_mocks_give_null(self):
        payload, _ = self._scan([])
        self.assertIsNone(payload["max_used_weight_1m"])
        guard = bys._ScanGuard()
        guard.note_weight(MagicMock())   # MagicMock headers: ignored, never int(MagicMock())
        guard.note_weight(object())
        guard.note_weight(_Resp([], {"X-MBX-USED-WEIGHT-1M": "garbage"}))
        self.assertIsNone(guard.max_used_weight)


# =============================================================================
# 4. Spread- and ATR-aware trigger buffer
# =============================================================================
class TestTriggerBuffer(unittest.TestCase):

    def test_constants(self):
        self.assertEqual((bys.TRIGGER_BUFFER, bys.TRIGGER_SPREAD_MULT, bys.TRIGGER_ATR_FRAC, bys.TRIGGER_BUFFER_MAX),
                         (0.0008, 1.0, 0.1, 0.005))

    def test_floor_when_spread_and_atr_are_small(self):
        self.assertAlmostEqual(bys.trigger_buffer(0.0001, 0.5), 0.0008)
        row = bys.build_levels(_audit_row(atr_pct=0.5), "LONG", SIZING, spread=0.0001)
        self.assertAlmostEqual(row["trigger"], 1.01 * 1.0008)
        self.assertEqual(row["trigger_buffer_pct"], 0.08)
        self.assertEqual(row["spread_pct"], 0.01)

    def test_wide_spread_widens_the_buffer(self):
        self.assertAlmostEqual(bys.trigger_buffer(0.003, 0.5), 0.003)
        row = bys.build_levels(_audit_row(atr_pct=0.5), "LONG", SIZING, spread=0.003)
        self.assertAlmostEqual(row["trigger"], 1.01 * 1.003)
        self.assertEqual(row["trigger_buffer_pct"], 0.3)

    def test_large_atr_widens_the_buffer(self):
        self.assertAlmostEqual(bys.trigger_buffer(0.0001, 3.0), 0.003)
        row = bys.build_levels(_audit_row(atr_pct=3.0), "LONG", SIZING, spread=0.0001)
        self.assertAlmostEqual(row["trigger"], 1.01 * 1.003)

    def test_buffer_capped_at_half_percent(self):
        self.assertAlmostEqual(bys.trigger_buffer(0.02, 0.5), 0.005)
        self.assertAlmostEqual(bys.trigger_buffer(None, 12.0), 0.005)
        row = bys.build_levels(_audit_row(atr_pct=12.0), "LONG", SIZING, spread=0.02)
        self.assertAlmostEqual(row["trigger"], 1.01 * 1.005)
        self.assertEqual(row["trigger_buffer_pct"], 0.5)

    def test_long_and_short_are_mirrored(self):
        r = _audit_row(atr_pct=0.5)
        long_row = bys.build_levels(r, "LONG", SIZING, spread=0.002)
        short_row = bys.build_levels(r, "SHORT", SIZING, spread=0.002)
        self.assertAlmostEqual(long_row["trigger"], r["high"] * 1.002)
        self.assertAlmostEqual(short_row["trigger"], r["low"] * 0.998)
        self.assertEqual(long_row["trigger_buffer_pct"], short_row["trigger_buffer_pct"])
        self.assertEqual(long_row["spread_pct"], short_row["spread_pct"])

    def test_missing_or_invalid_spread_falls_back_to_atr_and_floor(self):
        for bad in (None, float("nan"), float("inf"), -0.01, "x"):
            with self.subTest(spread=bad):
                self.assertAlmostEqual(bys.trigger_buffer(bad, 3.0), 0.003)
                self.assertAlmostEqual(bys.trigger_buffer(bad, 0.2), 0.0008)
                row = bys.build_levels(_audit_row(atr_pct=3.0), "LONG", SIZING, spread=bad)
                self.assertIsNone(row["spread_pct"])
                self.assertAlmostEqual(row["trigger"], 1.01 * 1.003)
        self.assertAlmostEqual(bys.trigger_buffer(None, None), 0.0008)
        self.assertAlmostEqual(bys.trigger_buffer(None, float("nan")), 0.0008)
        # Existing three-argument callers keep working (spread defaults to None).
        self.assertIsNone(bys.build_levels(_audit_row(), "LONG", SIZING)["spread_pct"])

    def test_scan_uses_one_book_ticker_request_and_the_symbol_spread(self):
        symbols = ["WIFUSDT", "DOGEUSDT"]
        book = _book(["WIFUSDT"], bid=0.997, ask=1.003) + _book(["DOGEUSDT"], bid=0.9999, ask=1.0001) \
            + _book(["BTCUSDT"], bid=1, ask=2) + [{"symbol": "BROKEN", "bidPrice": "x"}]
        router = _Router(_routes(symbols, book=book))
        with _scanner_env(router):
            data = bys.scan_yolo("prod")
        self.assertEqual(router.count("ticker/bookTicker"), 1)
        bt = next(c for c in router.calls if "bookTicker" in c[0].full_url)[0]
        self.assertNotIn("symbol=", bt.full_url)  # weight 5, not 2 per symbol
        rows = {c["symbol"]: c for c in data["longs"]}
        self.assertEqual(rows["WIFUSDT"]["spread_pct"], 0.6)
        self.assertEqual(rows["WIFUSDT"]["trigger_buffer_pct"], 0.5)  # 0.6% spread, capped at 0.5%
        self.assertEqual(rows["DOGEUSDT"]["spread_pct"], 0.02)
        self.assertGreater(rows["WIFUSDT"]["trigger"], rows["DOGEUSDT"]["trigger"])

    def test_book_ticker_failure_still_scans_without_spread(self):
        for failure in (urllib.error.URLError("down"), _http_error(500), {"unexpected": "shape"}):
            with self.subTest(failure=type(failure).__name__):
                router = _Router(_routes(["WIFUSDT"], book=failure))
                with _scanner_env(router), patch("sys.stderr", io.StringIO()):
                    data = bys.scan_yolo("prod")
                self.assertEqual(data["slot_status"], "CANDIDATE")
                rec = data["recommendation"]
                self.assertIsNone(rec["spread_pct"])
                self.assertTrue(rec["gate_ok"])
                self.assertGreaterEqual(rec["trigger_buffer_pct"], 0.08)


if __name__ == "__main__":
    unittest.main()
