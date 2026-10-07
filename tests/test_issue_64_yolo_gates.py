#!/usr/bin/env python3
"""
test_issue_64_yolo_gates.py - Issue #64: YOLO levels are checked against the executor gates everywhere.

- scripts/utils/gate_limits.py is the single source of the GATE 2 YOLO loss cap and the GATE 3 friction floor
  (executor, scanner and pipeline all use it; executor behaviour unchanged).
- broad_yolo_scanner.yolo_gate_failures() is the one gate function (coherence, friction, loss cap, R:R floors) used
  by the standalone scanner (rows flagged with gate_ok / gate_failures, recommendation = top gate-passing LONG)
  and by screening_pipeline._to_yolo_candidate.
- The checks run on 6-significant-digit prices with an adverse rounding margin.

No network: urllib is mocked/blocked for the whole module; no orders are ever sent.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import broad_yolo_scanner as bys
import execute_futures_trade as eft
import screening_pipeline as sp
from utils import gate_limits as gl
from utils import rate_limit_guard as rlg

PROFILE = {
    "profile_completed": True, "risk_pct_equity": 0.005, "max_margin_ratio": 0.30, "yolo_slot_enabled": True,
    "yolo_equity_pct": 0.001, "leverage_standard": 3, "leverage_yolo": 7, "leverage_ceiling": 10,
    "max_open_positions": 100,
}


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch, _ban_dir, _ban_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()
    # Issue #91: the market-data rate-limit ban file lives in a temp dir (a stale real ban never flips a test).
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
def _klines(side, high, low, close, vol, n=40, steps=None):
    """Choppy drift (down for 'LONG', up for 'SHORT', or `steps` = (up, down)), a signal candle (high/low/close
    relative to its open) with volume `vol` (previous bars trade 100), then a neutral forming candle inside its range:
    the signal candle is the last CLOSED one, klines[-2] (issue #85)."""
    ks, price = [], 1.0
    up, down = steps or ((1.002, 0.997) if side == "LONG" else (1.003, 0.998))
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


# LONG rows built by the real scanner from these candles (leverage 7):
LONG_OK = _klines("LONG", 1.005, 0.975, 1.001, 300)          # ~3.8% stop x7 = 0.27: passes every gate
LONG_OVER_CAP = _klines("LONG", 1.005, 0.95, 1.001, 500)     # 5.5% stop x7 = 0.385 > 0.35: loss_cap (higher score)
LONG_SL_ABOVE = _klines("LONG", 1.08, 0.99, 1.0, 600)        # 8% upper wick: SL above the current price
# SHORT rows. Issue #80: a symbol qualifying both sides is dropped from both, so every SHORT fixture fails the LONG
# filters: wick path on 1.5-1.8x volume (no climax volume, small lower wick) or RSI above LONG_MAX_RSI.
SHORT_OK = _klines("SHORT", 1.025, 0.995, 0.999, 150)        # ~3.9% stop: passes
SHORT_OVER_CAP = _klines("SHORT", 1.05, 0.995, 0.999, 180)   # 5.5% stop x7 = 0.385 > 0.35: loss_cap (higher score)
SHORT_SL_BELOW = _klines("SHORT", 1.01, 0.92, 1.0, 600, steps=(1.005, 0.998))  # 8% lower wick, RSI > 65: SL below price

class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(klines_by_symbol):
    """exchangeInfo = the given symbols as Binance-tagged memecoin perpetuals (fine tick); klines routed by symbol."""
    info = {"symbols": [{"symbol": s, "contractType": "PERPETUAL", "status": "TRADING", "baseAsset": s[:-4],
                         "quoteAsset": "USDT", "underlyingType": "COIN", "underlyingSubType": ["Meme", "Crypto"],
                         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0000001"}]}
                        for s in klines_by_symbol]}

    def _open(req, timeout=None):
        url = getattr(req, "full_url", req)
        if "/fapi/v1/exchangeInfo" in url:
            return _Resp(info)
        if "ticker/bookTicker" in url:
            return _Resp([])  # issue #66: no book data -> spread unknown, trigger buffer from ATR / floor
        for sym, ks in klines_by_symbol.items():
            if "/fapi/v1/klines" in url and f"symbol={sym}&" in url:
                return _Resp(ks)
        raise AssertionError(f"unexpected URL {url}")
    return _open


def _scan(klines_by_symbol, profile=PROFILE, top=5):
    with patch("urllib.request.urlopen", side_effect=_urlopen(klines_by_symbol)), \
         patch("user_profile.load_user_profile", return_value=dict(profile)), \
         patch("quant_risk_engine.get_account_equity", return_value=12000.0):
        return bys.scan_yolo("prod", interval="15m", top=top)


def _row(trigger, sl, tp1, tp2, price=0.99, leverage=7, direction="LONG"):
    """Hand-built scanner row passing the Barbell volume filter."""
    return {"symbol": "1000PEPEUSDT", "direction": direction, "score": 80.0, "price": price, "trigger": trigger,
            "sl": sl, "tp1": tp1, "tp2": tp2, "risk_pct": 0.0, "roe_tp1_pct": 0.0, "roe_tp2_pct": 0.0,
            "leverage": leverage, "margin_usdt": 12.0, "max_loss_usdt": 0.0, "rsi": 40.0, "vol_ratio": 2.5,
            "lower_wick": 20.0}


# Loss cap: 4.98% stop x7 = 0.3486 <= 0.35 on the raw prices, but the worst-case (SL 0.05% farther) is 0.352 > 0.35.
BORDERLINE_LONG = _row(trigger=1.0, sl=0.9502, tp1=1.10956, tp2=1.2241)
# 4% stop x7 = 0.28, TP1 2.2R, TP2 4.5R: far from every limit.
COMFORTABLE_LONG = _row(trigger=1.0123456789, sl=0.9718518517, tp1=1.1014518518, tp2=1.2346913578)


# =============================================================================
# 1. Shared gate constants
# =============================================================================
class TestSharedGateLimits(unittest.TestCase):

    def test_values_and_single_source(self):
        self.assertEqual((gl.YOLO_MAX_LOSS_MARGIN_FRACTION, gl.YOLO_MIN_LOSS_CAP_USDT, gl.MIN_TP1_DISTANCE),
                         (0.35, 3.75, 0.0035))
        for mod in (eft, bys, sp):
            with self.subTest(module=mod.__name__):
                self.assertIs(mod.YOLO_MAX_LOSS_MARGIN_FRACTION, gl.YOLO_MAX_LOSS_MARGIN_FRACTION)
                self.assertIs(mod.MIN_TP1_DISTANCE, gl.MIN_TP1_DISTANCE)
        self.assertIs(eft.YOLO_MIN_LOSS_CAP_USDT, gl.YOLO_MIN_LOSS_CAP_USDT)

    def test_pipeline_aliases(self):
        self.assertEqual(sp.YOLO_MIN_TP1_DISTANCE, gl.MIN_TP1_DISTANCE)
        self.assertEqual(sp.YOLO_MAX_LOSS_MARGIN_FRACTION, gl.YOLO_MAX_LOSS_MARGIN_FRACTION)
        self.assertEqual((sp.YOLO_MIN_R_TP1, sp.YOLO_MIN_RR_TP2), (bys.YOLO_MIN_R_TP1, bys.YOLO_MIN_RR_TP2))
        self.assertEqual((bys.YOLO_MIN_R_TP1, bys.YOLO_MIN_RR_TP2), (1.8, 3.0))
        self.assertEqual(sp.YOLO_SIG_DIGITS, bys.YOLO_SIG_DIGITS)
        self.assertIs(sp._sig, bys._sig)
        self.assertEqual(bys.YOLO_ROUNDING_MARGIN, 0.0005)
        with open(os.path.join(SCRIPTS_DIR, "screening_pipeline.py"), encoding="utf-8") as f:
            self.assertNotIn("mirrored here (not imported)", f.read())


# =============================================================================
# 2. Executor GATE 2 (YOLO) / GATE 3 use the shared constants, behaviour unchanged
# =============================================================================
def _flat_exchange_reads_only(method, endpoint, params=None, target_env=None, retry_count=0):
    if method == "GET" and not params and endpoint in ("/fapi/v2/positionRisk", "/fapi/v1/openAlgoOrders",
                                                       "/fapi/v1/openOrders"):
        return []
    raise AssertionError(f"gate check must not send other requests ({method} {endpoint})")


class TestExecutorGatesUseSharedLimits(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = tmp.name
        os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
        with open(os.path.join(ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
            json.dump({"is_valid": True, "last_updated_ts": int(time.time()),
                       "portfolio_exposure": {"delta_bias": "NEUTRAL"}}, f)
        patches = [
            patch("execute_futures_trade._workspace_dir", return_value=ws),
            patch.object(eft, "__file__", os.path.join(ws, "scripts", "execute_futures_trade.py")),
            patch("user_profile.load_user_profile", return_value=dict(PROFILE)),
            patch("quant_risk_engine.get_account_equity", return_value=1000.0),
            # the gate check only reads the live PROD snapshot (issue #101): a flat exchange, as in session_state
            patch("execute_futures_trade.send_signed_request", side_effect=_flat_exchange_reads_only),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _yolo(self, sl, qty=1.0):
        # Entry 100, 5x: margin = 100 x qty / 5; YOLO cap = max(3.75, 0.35 x margin).
        return eft.check_mechanical_gates("LONG", 100.0, sl, 110.0, qty, 5, target_env="prod", is_yolo=True)

    def test_yolo_loss_cap_just_under_and_over_35pct(self):
        ok, msg = self._yolo(93.1)            # loss 6.90 = 34.5% of the 20 USDT margin
        self.assertTrue(ok, msg)
        ok, msg = self._yolo(92.9)            # loss 7.10 = 35.5% > 7.00 cap
        self.assertFalse(ok)
        self.assertIn("Monetary risk exceeds allowed cap ($7.10 > $7.00 USDT", msg)

    def test_yolo_minimum_cap_of_3_75_usdt(self):
        ok, msg = self._yolo(92.6, qty=0.5)   # margin 10: 0.35 x 10 = 3.50 < 3.75 floor; loss 3.70 passes
        self.assertTrue(ok, msg)
        ok, msg = self._yolo(92.4, qty=0.5)   # loss 3.80 > 3.75
        self.assertFalse(ok)
        self.assertIn("> $3.75 USDT", msg)

    def test_friction_floor_034_vs_036(self):
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 100.34, 1.0, 3, target_env="prod")
        self.assertFalse(ok)
        self.assertIn("Distance to TP1 (0.34%) below 0.35% friction floor", msg)
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 100.36, 1.0, 3, target_env="prod")
        self.assertTrue(ok, msg)
        ok, msg = eft.check_mechanical_gates("SHORT", 100.0, 102.0, 99.66, 1.0, 3, target_env="prod")
        self.assertFalse(ok)
        ok, msg = eft.check_mechanical_gates("SHORT", 100.0, 102.0, 99.64, 1.0, 3, target_env="prod")
        self.assertTrue(ok, msg)

    def test_gates_read_the_shared_names(self):
        """Patching the executor's imported names changes the gates: no inline literal is left."""
        with patch.object(eft, "MIN_TP1_DISTANCE", 0.001):
            ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 100.2, 1.0, 3, target_env="prod")
            self.assertTrue(ok, msg)
            ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 100.05, 1.0, 3, target_env="prod")
            self.assertIn("below 0.10% friction floor", msg)
        with patch.object(eft, "YOLO_MAX_LOSS_MARGIN_FRACTION", 0.50):
            ok, msg = self._yolo(92.9)        # 7.10 <= 0.50 x 20
            self.assertTrue(ok, msg)
        with patch.object(eft, "YOLO_MIN_LOSS_CAP_USDT", 10.0):
            ok, msg = self._yolo(92.9)        # 7.10 <= 10.00 floor
            self.assertTrue(ok, msg)


# =============================================================================
# 3. Gate function
# =============================================================================
class TestYoloGateFailures(unittest.TestCase):

    def test_long_failures(self):
        self.assertEqual(bys.yolo_gate_failures(COMFORTABLE_LONG), [])
        self.assertEqual(bys.yolo_gate_failures(dict(COMFORTABLE_LONG, sl=1.0)), ["coherence"])     # SL > price
        self.assertEqual(bys.yolo_gate_failures(dict(COMFORTABLE_LONG, tp2=float("nan"))), ["coherence"])
        self.assertEqual(bys.yolo_gate_failures(dict(COMFORTABLE_LONG, leverage=0)), ["coherence"])
        self.assertEqual(bys.yolo_gate_failures({k: v for k, v in COMFORTABLE_LONG.items() if k != "sl"}),
                         ["coherence"])
        self.assertEqual(bys.yolo_gate_failures(dict(COMFORTABLE_LONG, direction="FLAT")), ["coherence"])
        self.assertEqual(bys.yolo_gate_failures(dict(COMFORTABLE_LONG, leverage=9)), ["loss_cap"])
        tight = _row(price=0.9995, trigger=1.0, sl=0.999, tp1=1.003, tp2=1.006)
        self.assertIn("friction", bys.yolo_gate_failures(tight))
        self.assertEqual(bys.yolo_gate_failures(_row(trigger=1.0, sl=0.98, tp1=1.034, tp2=1.09)), ["rr_tp1"])
        self.assertEqual(bys.yolo_gate_failures(_row(trigger=1.0, sl=0.98, tp1=1.04, tp2=1.055)), ["rr_tp2"])

    def test_short_mirrored(self):
        short = _row(direction="SHORT", price=1.01, trigger=1.0, sl=1.04, tp1=0.912, tp2=0.82)
        self.assertEqual(bys.yolo_gate_failures(short), [])
        self.assertEqual(bys.yolo_gate_failures(dict(short, sl=1.005)), ["coherence"])     # SL below price
        self.assertEqual(bys.yolo_gate_failures(dict(short, tp1=1.001)), ["coherence"])    # TP1 above trigger
        self.assertEqual(bys.yolo_gate_failures(dict(short, leverage=9)), ["loss_cap"])
        self.assertEqual(bys.yolo_gate_failures(dict(short, tp1=0.93)), ["rr_tp1"])
        self.assertEqual(bys.yolo_gate_failures(dict(short, tp2=0.885)), ["rr_tp2"])
        tight = _row(direction="SHORT", price=1.0005, trigger=1.0, sl=1.001, tp1=0.997, tp2=0.994)
        self.assertIn("friction", bys.yolo_gate_failures(tight))


# =============================================================================
# 4. Standalone scanner: flag, never drop; recommendation = top gate-passing LONG
# =============================================================================
class TestScannerFlagsGateFailures(unittest.TestCase):

    def test_failing_longs_flagged_and_skipped_by_recommendation(self):
        data = _scan({"1000PEPEUSDT": LONG_OVER_CAP, "DOGEUSDT": LONG_SL_ABOVE, "WIFUSDT": LONG_OK})
        longs = {c["symbol"]: c for c in data["longs"]}
        self.assertEqual(set(longs), {"1000PEPEUSDT", "DOGEUSDT", "WIFUSDT"})   # listed, not dropped
        # WIFUSDT has the lowest score of the three but is the only one passing the gates
        self.assertFalse(longs["1000PEPEUSDT"]["gate_ok"])
        self.assertEqual(longs["1000PEPEUSDT"]["gate_failures"], ["loss_cap"])
        self.assertFalse(longs["DOGEUSDT"]["gate_ok"])
        self.assertEqual(longs["DOGEUSDT"]["gate_failures"], ["coherence"])
        self.assertGreater(longs["DOGEUSDT"]["sl"], longs["DOGEUSDT"]["price"])
        self.assertTrue(longs["WIFUSDT"]["gate_ok"])
        self.assertEqual(longs["WIFUSDT"]["gate_failures"], [])
        self.assertEqual(data["recommendation"]["symbol"], "WIFUSDT")
        self.assertTrue(data["recommendation"]["gate_ok"])
        self.assertEqual(data["slot_status"], "CANDIDATE")
        for row in data["shorts"]:
            self.assertIn("gate_ok", row)
            self.assertIsInstance(row["gate_failures"], list)
        f = data["filters"]
        self.assertEqual((f["min_tp1_distance"], f["max_loss_margin_fraction"], f["min_r_tp1"], f["min_rr_tp2"],
                          f["rounding_margin"]), (0.0035, 0.35, 1.8, 3.0, 0.0005))
        self.assertEqual(f["min_vol_ratio"], 2.0)  # existing keys kept
        json.dumps(data)

    def test_gate_passing_rows_listed_first_in_score_order(self):
        """Round 2: passing rows first, failing rows after; score order kept within each group (stable sort)."""
        data = _scan({"1000PEPEUSDT": LONG_OVER_CAP, "DOGEUSDT": LONG_SL_ABOVE, "WIFUSDT": LONG_OK,
                      "1000BONKUSDT": SHORT_OVER_CAP, "1000FLOKIUSDT": SHORT_OK})
        rows = data["longs"]
        self.assertEqual([c["symbol"] for c in rows], ["WIFUSDT", "1000PEPEUSDT", "DOGEUSDT"])
        self.assertEqual([c["gate_ok"] for c in rows], [True, False, False])
        failing = [c for c in rows if not c["gate_ok"]]
        self.assertGreater(failing[0]["score"], failing[1]["score"])   # 1000PEPE (score ~135) before DOGE (~119)
        self.assertGreater(failing[0]["score"], rows[0]["score"])      # the passing row outranks a higher score
        # Shorts too: 1000BONK's SHORT fails the cap with the higher score, 1000FLOKI's SHORT passes and comes first.
        self.assertEqual([(c["symbol"], c["gate_ok"]) for c in data["shorts"]],
                         [("1000FLOKIUSDT", True), ("1000BONKUSDT", False)])
        self.assertEqual(data["shorts"][1]["gate_failures"], ["loss_cap"])
        self.assertGreater(data["shorts"][1]["score"], data["shorts"][0]["score"])
        self.assertEqual(data["ambiguous_symbols"], [])

    def test_gate_passing_long_below_top_cut_is_recommended(self):
        """Round 2: top=1 and only the rank-2 long (by score) passes: it is the recommendation and the listed row."""
        data = _scan({"1000PEPEUSDT": LONG_OVER_CAP, "WIFUSDT": LONG_OK}, top=1)
        self.assertEqual([c["symbol"] for c in data["longs"]], ["WIFUSDT"])
        self.assertTrue(data["longs"][0]["gate_ok"])
        self.assertEqual(data["recommendation"]["symbol"], "WIFUSDT")
        self.assertEqual(data["slot_status"], "CANDIDATE")
        # The screening pipeline (YOLO_SCAN_TOP cut in the scanner) forwards it to the evaluator.
        status, slot = sp.build_yolo_slot(data)
        self.assertEqual(slot.status, "ACTIVE")
        self.assertEqual([c.symbol for c in slot.candidates], ["WIFUSDT"])
        self.assertTrue(status.startswith("ACTIVE: 1 memecoin(s)"))
        # Failing rows still fill the list when fewer than `top` pass.
        data = _scan({"1000PEPEUSDT": LONG_OVER_CAP, "WIFUSDT": LONG_OK}, top=2)
        self.assertEqual([(c["symbol"], c["gate_ok"]) for c in data["longs"]],
                         [("WIFUSDT", True), ("1000PEPEUSDT", False)])

    def test_pipeline_scan_with_yolo_scan_top_picks_gate_passing_long(self):
        """Round 2: through screening_pipeline's own scan call (top=YOLO_SCAN_TOP) on mocked HTTP: four
        higher-scored failing longs do not hide the passing one."""
        klines = {"1000PEPEUSDT": LONG_OVER_CAP, "DOGEUSDT": LONG_SL_ABOVE, "WIFUSDT": LONG_OK,
                  "BONKUSDT": _klines("LONG", 1.005, 0.95, 1.001, 450),
                  "FLOKIUSDT": _klines("LONG", 1.005, 0.95, 1.001, 400)}
        with patch("urllib.request.urlopen", side_effect=_urlopen(klines)), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch("quant_risk_engine.get_account_equity", return_value=12000.0):
            scan = sp._start_yolo_scan("prod").result(timeout=30)
        self.assertEqual(sp.YOLO_SCAN_TOP, 3)
        self.assertEqual(len(scan["longs"]), sp.YOLO_SCAN_TOP)
        self.assertEqual(scan["longs"][0]["symbol"], "WIFUSDT")
        self.assertEqual([c["gate_ok"] for c in scan["longs"]], [True, False, False])
        self.assertEqual(scan["recommendation"]["symbol"], "WIFUSDT")
        slot = sp.build_yolo_slot(scan)[1]
        self.assertEqual([c.symbol for c in slot.candidates], ["WIFUSDT"])

    def test_no_gate_passing_long_leaves_slot_empty(self):
        data = _scan({"1000PEPEUSDT": LONG_OVER_CAP, "DOGEUSDT": LONG_SL_ABOVE})
        self.assertEqual(len(data["longs"]), 2)
        self.assertTrue(all(not c["gate_ok"] for c in data["longs"]))
        self.assertIsNone(data["recommendation"])
        self.assertEqual(data["slot_status"], "EMPTY")
        with patch("user_profile.load_user_profile", return_value=dict(PROFILE)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bys.print_text_report(data)
        self.assertIn("GATE FAIL: loss_cap", out.getvalue())
        self.assertIn("GATE FAIL: coherence", out.getvalue())
        self.assertIn("No long candidate passes the executor gates", out.getvalue())

    def test_short_rows_mirrored(self):
        data = _scan({"1000PEPEUSDT": SHORT_SL_BELOW, "DOGEUSDT": SHORT_OK})
        shorts = {c["symbol"]: c for c in data["shorts"]}
        self.assertEqual(set(shorts), {"1000PEPEUSDT", "DOGEUSDT"})
        self.assertEqual(shorts["1000PEPEUSDT"]["gate_failures"], ["coherence"])
        self.assertLess(shorts["1000PEPEUSDT"]["sl"], shorts["1000PEPEUSDT"]["price"])
        self.assertFalse(shorts["1000PEPEUSDT"]["gate_ok"])
        self.assertTrue(shorts["DOGEUSDT"]["gate_ok"])
        self.assertEqual(shorts["DOGEUSDT"]["gate_failures"], [])


# =============================================================================
# 5. Checks on rounded values with the adverse margin (scanner and pipeline agree)
# =============================================================================
class TestRoundedWorstCaseChecks(unittest.TestCase):

    def test_borderline_long_rejected_by_scanner_and_pipeline(self):
        trigger, sl = BORDERLINE_LONG["trigger"], BORDERLINE_LONG["sl"]
        self.assertLessEqual((trigger - sl) / trigger * 7, 0.35)    # passes on the raw prices
        self.assertEqual(bys.yolo_gate_failures(BORDERLINE_LONG), ["loss_cap"])
        self.assertIsNone(sp._to_yolo_candidate(BORDERLINE_LONG))
        with patch.object(bys, "build_levels",
                          side_effect=lambda r, d, s, spread=None: dict(BORDERLINE_LONG, symbol=r["symbol"], direction=d)):
            data = _scan({"WIFUSDT": LONG_OK})
        self.assertEqual(data["longs"][0]["gate_failures"], ["loss_cap"])
        self.assertIsNone(data["recommendation"])
        self.assertEqual(data["slot_status"], "EMPTY")

    def test_gates_evaluate_the_rounded_prices(self):
        """Round 2: trigger 1.0000001 and TP1 1.0000004 both round to 1.0 at 6 significant digits, so the rounded
        levels are incoherent (trigger == tp1). On the raw prices the row is coherent and fails friction/R:R instead."""
        row = _row(trigger=1.0000001, sl=0.96, tp1=1.0000004, tp2=1.2)
        self.assertLess(row["trigger"], row["tp1"])                       # raw levels are coherent
        self.assertEqual(bys._sig(row["trigger"]), bys._sig(row["tp1"]))  # rounded ones are not
        self.assertEqual(bys.yolo_gate_failures(row), ["coherence"])
        self.assertIsNone(sp._to_yolo_candidate(row))
        with patch.object(bys, "_sig", side_effect=float):                # without rounding: a different verdict
            self.assertEqual(bys.yolo_gate_failures(row), ["friction", "rr_tp1"])

    def test_comfortable_long_unaffected_and_emits_checked_rounded_values(self):
        self.assertEqual(bys.yolo_gate_failures(COMFORTABLE_LONG), [])
        cand = sp._to_yolo_candidate(COMFORTABLE_LONG)
        self.assertIsNotNone(cand)
        for key in ("trigger", "sl", "tp1", "tp2"):
            self.assertEqual(getattr(cand, key), float(f"{COMFORTABLE_LONG[key]:.6g}"), key)
        self.assertEqual(cand.risk_pct, round((cand.trigger - cand.sl) / cand.trigger * 100, 2))
        self.assertEqual(cand.rr_tp2, round((cand.tp2 - cand.trigger) / (cand.trigger - cand.sl), 2))
        slot = sp.build_yolo_slot({"interval": "15m", "slot_status": "CANDIDATE",
                                   "longs": [BORDERLINE_LONG, COMFORTABLE_LONG]})[1]
        self.assertEqual(slot.status, "ACTIVE")
        self.assertEqual(len(slot.candidates), 1)
        self.assertEqual(slot.candidates[0].trigger, cand.trigger)


if __name__ == "__main__":
    unittest.main()
