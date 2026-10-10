#!/usr/bin/env python3
"""
test_issue_268_fee_drag.py - Issue #268: fees consume the gross edge; measure them in R, report them, gate on them.

1. utils/gate_limits: TAKER_FEE_RATE / MAKER_FEE_RATE and expected_fee_r (taker entry + taker SL, None on bad input).
2. user_profile: max_fee_r (null in DEFAULT_PROFILE and the example) and get_max_fee_r (OFF / threshold / invalid).
3. Executor Gate 3B (PROD only, OFF by default): boundaries for LONG and SHORT, invalid values reject in PROD and are
   ignored in TESTNET, cannot-compute rejects, an unreadable profile is OFF, risk-reducing paths never call it.
4. trade_outcomes: per-leg maker flag, fees_r, entry / exit liquidity, stop_distance_pct, nulls, old rows.
5. trading_scorecard: fees block, by stop bucket, gross vs net on the same rows, LONG / SHORT split, the fee-threshold
   back-test and the unchanged calibration store.
6. Screening candidates carry expected_fee_r; the brief shows it as fee_r and drops a null; the brief still fits.
7. trading_doctor [FEES] line: KEYS ok, [] reply, error reply, MCP unavailable; exit code unchanged.

Hermetic: Binance client faked (send_signed_request), urlopen blocked, temp workspaces, the profile patched.
"""

import contextlib
import inspect
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "hooks"),
           os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execute_futures_trade as eft  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import screening_pipeline as sp  # noqa: E402
import sync_session_state as sss  # noqa: E402
import trading_doctor  # noqa: E402
import trading_scorecard as sc  # noqa: E402
import user_profile as up  # noqa: E402
from utils import gate_limits as gl  # noqa: E402
from utils import score_calibration as scal  # noqa: E402

import test_issue_187_lesson_selection as t187  # noqa: E402  (fixtures only)
import test_issue_271_brief_lesson_budget as t271  # noqa: E402  (fixtures only)
import test_issue_52_yolo_in_pipeline as t52  # noqa: E402  (fixtures only)
import test_trade_outcomes as tto  # noqa: E402  (fixtures only)
import test_trading_scorecard as tsc  # noqa: E402  (fixtures only)

fill = tto.fill
H = 3600_000


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


# =============================================================================
# 1. Fee model
# =============================================================================
class TestExpectedFeeR(unittest.TestCase):

    def test_constants_match_the_replay_defaults(self):
        self.assertEqual((gl.TAKER_FEE_RATE, gl.MAKER_FEE_RATE), (0.0005, 0.0002))
        with open(os.path.join(BASE_DIR, "scripts", "exit_policy_sim.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("DEFAULT_TAKER_FEE = 0.0005", text)
        self.assertIn("DEFAULT_MAKER_FEE = 0.0002", text)
        with open(os.path.join(BASE_DIR, "scripts", "utils", "gate_limits.py"), encoding="utf-8") as f:
            self.assertNotIn("import exit_policy_sim", f.read())

    def test_values(self):
        self.assertAlmostEqual(gl.expected_fee_r(1.5), 0.1 / 1.5)
        self.assertAlmostEqual(gl.expected_fee_r(2.0), 0.05)
        self.assertAlmostEqual(gl.expected_fee_r(4), 0.025)
        self.assertAlmostEqual(gl.expected_fee_r("2.5"), 0.04)
        self.assertAlmostEqual(gl.expected_fee_r(2.0, entry_rate=gl.MAKER_FEE_RATE), 0.035)
        self.assertGreater(gl.expected_fee_r(1.5), gl.expected_fee_r(2.0))  # a tighter stop costs more R

    def test_none_for_bad_input(self):
        for bad in (None, 0, 0.0, -1.0, float("nan"), float("inf"), "x", True, False, [], {}):
            self.assertIsNone(gl.expected_fee_r(bad), bad)


# =============================================================================
# 2. Profile
# =============================================================================
class TestProfileMaxFeeR(unittest.TestCase):

    def test_default_and_example_are_off(self):
        self.assertIn("max_fee_r", up.DEFAULT_PROFILE)
        self.assertIsNone(up.DEFAULT_PROFILE["max_fee_r"])
        with open(os.path.join(BASE_DIR, "config", "user_profile.json.example"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertIn("max_fee_r", example)
        self.assertIsNone(example["max_fee_r"])
        off = {"max_fee_r": None, "error": None}
        for prof in ({}, None, {"max_fee_r": None}, dict(up.DEFAULT_PROFILE), "not a dict"):
            self.assertEqual(up.get_max_fee_r(prof), off, prof)

    def test_valid_threshold(self):
        self.assertEqual(up.get_max_fee_r({"max_fee_r": 0.06}), {"max_fee_r": 0.06, "error": None})
        self.assertEqual(up.get_max_fee_r({"max_fee_r": 1}), {"max_fee_r": 1.0, "error": None})

    def test_invalid_present_value_is_an_error_never_off(self):
        for bad in ("0.06", 0, 0.0, -0.05, float("nan"), float("inf"), True, False, [0.06], {"v": 1}):
            cfg = up.get_max_fee_r({"max_fee_r": bad})
            self.assertIsNone(cfg["max_fee_r"], bad)
            self.assertIn("max_fee_r must be null or a positive finite number", cfg["error"], bad)


# =============================================================================
# 3. Executor Gate 3B
# =============================================================================
PROFILE = {
    "profile_completed": True, "risk_pct_equity": 0.005, "max_margin_ratio": 0.30, "yolo_slot_enabled": True,
    "yolo_equity_pct": 0.001, "leverage_standard": 3, "leverage_yolo": 7, "leverage_ceiling": 10,
    "max_open_positions": 100,
}


def _flat_exchange_reads_only(method, endpoint, params=None, target_env=None, retry_count=0):
    if method == "GET" and not params and endpoint in ("/fapi/v2/positionRisk", "/fapi/v1/openAlgoOrders",
                                                       "/fapi/v1/openOrders"):
        return []
    raise AssertionError(f"gate check must not send other requests ({method} {endpoint})")


def _stop_pct(ref, sl):
    """The gate's stop distance expression (percent of the effective entry)."""
    return abs(ref - sl) / ref * 100


class TestFeeGate(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = tmp.name
        os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
        with open(os.path.join(ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
            json.dump({"is_valid": True, "last_updated_ts": int(time.time()),
                       "portfolio_exposure": {"delta_bias": "NEUTRAL"}}, f)
        self.profile = dict(PROFILE)
        patches = [
            patch("execute_futures_trade._workspace_dir", return_value=ws),
            patch.object(eft, "__file__", os.path.join(ws, "scripts", "execute_futures_trade.py")),
            patch("user_profile.load_user_profile", side_effect=lambda *a, **k: dict(self.profile)),
            patch("quant_risk_engine.get_account_equity", return_value=1000.0),
            patch("execute_futures_trade.send_signed_request", side_effect=_flat_exchange_reads_only),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def gate(self, direction, sl, env="prod", tp1=None, **kw):
        tp1 = tp1 if tp1 is not None else (110.0 if direction == "LONG" else 90.0)
        return eft.check_mechanical_gates(direction, 100.0, sl, tp1, 1.0, 3, target_env=env, **kw)

    def test_off_by_default_and_when_null(self):
        for prof in (dict(PROFILE), dict(PROFILE, max_fee_r=None)):
            self.profile = prof
            ok, msg = self.gate("LONG", 99.5)  # 0.5% stop: expected fee 0.2R, far above any sane threshold
            self.assertTrue(ok, msg)
            ok, msg = self.gate("SHORT", 100.5)
            self.assertTrue(ok, msg)

    def test_boundaries_long_and_short(self):
        for direction, sl_eq, sl_tighter, sl_wider in (("LONG", 98.0, 98.01, 97.99), ("SHORT", 102.0, 101.99, 102.01)):
            with self.subTest(direction=direction):
                threshold = gl.expected_fee_r(_stop_pct(100.0, sl_eq))  # equal by construction (2% stop: 0.05R)
                self.assertAlmostEqual(threshold, 0.05)
                self.profile = dict(PROFILE, max_fee_r=threshold)
                ok, msg = self.gate(direction, sl_eq)
                self.assertTrue(ok, msg)  # equal passes (rejects only above)
                ok, msg = self.gate(direction, sl_wider)
                self.assertTrue(ok, msg)  # just below the threshold
                ok, msg = self.gate(direction, sl_tighter)
                self.assertFalse(ok)  # just above the threshold
                self.assertTrue(msg.startswith("MECHANICAL HARD GATE REJECTION: Expected fee 0.0503R"), msg)
                self.assertIn("max_fee_r 0.05R", msg)
                self.assertIn("1.99% stop distance", msg)

    def test_effective_entry_is_the_reference(self):
        # current price 100 but a conditional entry at 101 with SL 99: 1.98% stop from the entry, not 1% from price
        self.profile = dict(PROFILE, max_fee_r=0.06)
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 99.0, 110.0, 1.0, 3, target_env="prod",
                                             entry_price=101.0)
        self.assertTrue(ok, msg)
        self.profile = dict(PROFILE, max_fee_r=0.05)
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 99.0, 110.0, 1.0, 3, target_env="prod",
                                             entry_price=101.0)
        self.assertFalse(ok)
        self.assertIn("entry ref 101.0", msg)

    def test_invalid_value_rejects_in_prod_and_is_ignored_in_testnet(self):
        for bad in ("0.06", 0, -0.05, float("nan"), True):
            with self.subTest(bad=bad):
                self.profile = dict(PROFILE, max_fee_r=bad)
                ok, msg = self.gate("LONG", 95.0)  # 5% stop: 0.02R fee, rejected only for the value
                self.assertFalse(ok)
                self.assertIn("FAIL-CLOSED — invalid profile fee-in-R threshold", msg)
                ok, msg = self.gate("LONG", 95.0, env="testnet")
                self.assertTrue(ok, msg)

    def test_testnet_is_off_even_with_a_threshold(self):
        self.profile = dict(PROFILE, max_fee_r=0.01)
        ok, msg = self.gate("LONG", 99.5, env="testnet")
        self.assertTrue(ok, msg)

    def test_cannot_compute_rejects_in_prod(self):
        self.profile = dict(PROFILE, max_fee_r=0.06)
        with patch.object(eft, "expected_fee_r", return_value=None):
            ok, msg = self.gate("LONG", 98.0)
        self.assertFalse(ok)
        self.assertIn("FAIL-CLOSED — cannot compute the expected fee in R", msg)
        self.assertIn("entry ref 100.0, SL 98.0", msg)

    def test_unreadable_profile_is_off(self):
        with patch("user_profile.load_user_profile", side_effect=OSError("unreadable")):
            ok, msg = self.gate("LONG", 99.5)
        self.assertTrue(ok, msg)

    def test_yolo_orders_are_gated_too(self):
        self.profile = dict(PROFILE, max_fee_r=0.04)
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 110.0, 1.0, 5, target_env="prod", is_yolo=True)
        self.assertFalse(ok)
        self.assertIn("Expected fee 0.0500R", msg)

    def test_risk_reducing_paths_never_call_the_gate(self):
        source = inspect.getsource(eft)
        self.assertEqual(source.count("get_max_fee_r("), 1)
        self.assertIn("get_max_fee_r(", inspect.getsource(eft.check_mechanical_gates))
        # Gate 3B lives inside check_mechanical_gates, whose only caller is the opening path
        self.assertEqual(source.count("check_mechanical_gates("), 2)  # the definition and one call
        self.assertIn("check_mechanical_gates(", inspect.getsource(eft._execute_complete_trade_pass))
        for name in ("close_position_market", "move_sl_to_breakeven", "audit_orphan_positions",
                     "protect_pending_entries", "heal_orphan_position", "get_positions_report"):
            body = inspect.getsource(getattr(eft, name))
            for needle in ("check_mechanical_gates(", "get_max_fee_r", "expected_fee_r", "max_fee_r"):
                self.assertNotIn(needle, body, (name, needle))

    def test_signature_backward_compatible(self):
        params = list(inspect.signature(eft.check_mechanical_gates).parameters)
        self.assertEqual(params[:6], ["direction", "cur_price", "sl_price", "tp1_price", "total_qty", "leverage"])
        self.assertNotIn("max_fee_r", params)


# =============================================================================
# 4. trade_outcomes
# =============================================================================
class TestOutcomeFees(tto.OutcomesBase):

    def ondo(self, entry_maker=False, exit_makers=(True, False), asset="USDT"):
        self.audit(symbol="ONDOUSDT", direction="SHORT", entry_price=1.0, sl_price=1.05, total_qty=1000.0,
                   entry_order_id=11, tp1_order_id=12, tp2_order_id=13, tp1_price=0.91, tp2_price=0.8)
        fills = [fill(1, 11, "SELL", 1.0, 1000, tto.T0, comm=0.3, symbol="ONDOUSDT", maker=entry_maker),
                 fill(2, 12, "BUY", 0.91, 300, tto.T0 + H, pnl=27.0, comm=0.1, symbol="ONDOUSDT",
                      maker=exit_makers[0], asset=asset),
                 fill(3, 99, "BUY", 0.86, 700, tto.T0 + 2 * H, pnl=98.0, comm=0.2, symbol="ONDOUSDT",
                      maker=exit_makers[1])]
        code, _out = self.run_cli(tto.FakeFills({"ONDOUSDT": fills}), ["--json", "--no-klines"])
        self.assertEqual(code, 0)
        return self.rows()[0]

    def test_fees_r_and_mixed_exit(self):
        t = self.ondo()
        self.assertEqual(t["status"], "closed")
        self.assertAlmostEqual(t["fees_r"], (0.3 + 0.1 + 0.2) / 50.0, places=4)  # same denominator as net R
        self.assertAlmostEqual(t["realized_r_gross"] - t["fees_r"], 2.5 - 0.012, places=4)
        self.assertEqual((t["entry_liquidity"], t["exit_liquidity"]), ("taker", "mixed"))
        self.assertEqual([l["maker"] for l in t["legs"]], [True, False])
        self.assertAlmostEqual(t["stop_distance_pct"], 5.0, places=4)

    def test_maker_entry_and_all_maker_exits(self):
        t = self.ondo(entry_maker=True, exit_makers=(True, True))
        self.assertEqual((t["entry_liquidity"], t["exit_liquidity"]), ("maker", "maker"))

    def test_taker_exits(self):
        t = self.ondo(exit_makers=(False, False))
        self.assertEqual((t["entry_liquidity"], t["exit_liquidity"]), ("taker", "taker"))

    def test_non_usdt_commission_nulls_fees_r_like_net(self):
        t = self.ondo(asset="BNB")
        self.assertIsNone(t["fees_r"])
        self.assertIsNone(t["realized_r_net"])
        self.assertIsNotNone(t["realized_r_gross"])
        self.assertEqual(t["exit_liquidity"], "mixed")  # the liquidity flags do not depend on the asset

    def test_missing_maker_flag_gives_null_liquidity(self):
        self.audit(entry_order_id=1)
        entry = fill(1, 1, "BUY", 100, 10, tto.T0)
        del entry["maker"]
        self.run_cli(tto.FakeFills({"BTCUSDT": [entry, fill(2, 2, "SELL", 110, 10, tto.T0 + H)]}), ["--no-klines"])
        t = self.rows()[0]
        self.assertIsNone(t["entry_liquidity"])
        self.assertEqual(t["exit_liquidity"], "taker")

    def test_legacy_record_without_entry_fills(self):
        self.audit()  # no entry_order_id: legacy match, no entry fills
        self.run_cli(tto.FakeFills({"BTCUSDT": [fill(2, 2, "SELL", 110, 10, tto.T0 + H, comm=0.5)]}),
                     ["--no-klines"])
        t = self.rows()[0]
        self.assertEqual(t["entry_match"], "legacy")
        self.assertIsNone(t["entry_liquidity"])
        self.assertEqual(t["exit_liquidity"], "taker")
        self.assertAlmostEqual(t["fees_r"], 0.5 / 50.0, places=4)  # legs only (entry_commission_included false)
        self.assertFalse(t["entry_commission_included"])

    def test_no_entry_fill_and_fills_unavailable_carry_nulls(self):
        self.audit(entry_order_id=3953)
        self.audit(symbol="ETHUSDT", entry_order_id=7)
        self.run_cli(tto.FakeFills({"BTCUSDT": [], "ETHUSDT": {"code": -2015, "msg": "Invalid API-key"}}),
                     ["--no-klines"])
        by = {t["symbol"]: t for t in self.rows()}
        self.assertEqual((by["BTCUSDT"]["status"], by["ETHUSDT"]["status"]), ("no_entry_fill", "fills_unavailable"))
        for t in by.values():
            for key in ("fees_r", "entry_liquidity", "exit_liquidity", "stop_distance_pct"):
                self.assertIn(key, t)
                self.assertIsNone(t[key], (t["status"], key))

    def test_old_rows_without_the_fields_still_parse(self):
        old = {"symbol": "BTCUSDT", "direction": "LONG", "status": "closed", "env": "prod", "realized_r_net": 1.0,
               "realized_r_gross": 1.1, "initial_risk": 5.0, "entry_price": 100.0, "filled_qty": 1.0, "legs": []}
        self.assertIsNone(old.get("fees_r"))
        self.assertEqual(sc._stop_distance_pct(old), 5.0)  # reconstructed from initial_risk / entry_price
        self.assertEqual(sc._fees_block([old])["fees_r_unavailable"], 1)


# =============================================================================
# 5. Scorecard
# =============================================================================
def frow(net, gross, fees=None, stop=None, direction="LONG", **extra):
    r = tsc.row(net=net, gross=gross, direction=direction, **extra)
    if fees is not None:
        r["fees_r"] = fees
    if stop is not None:
        r["stop_distance_pct"] = stop
    return r


class TestScorecardFees(tsc.ScorecardBase):

    def test_fees_block_buckets_gross_vs_net_and_direction(self):
        rows = [frow(1.0, 1.05, fees=0.05, stop=1.4), frow(-1.0, -0.93, fees=0.07, stop=1.5, direction="SHORT"),
                frow(2.0, 2.04, fees=0.04, stop=2.0), frow(-1.0, -0.97, fees=0.03, stop=3.0, direction="SHORT"),
                frow(0.5, 0.52, fees=0.02, stop=4.0),
                frow(None, 0.8, stop=2.5),  # BNB-style row: no net R, no fees_r (gross fallback)
                frow(1.0, 1.0),  # old row: no fees_r, no stop distance, no entry price
                frow(1.0, 1.0, env="testnet", fees=9.0)]  # other env: excluded everywhere
        self.write_outcomes(rows)
        s = sc.generate_scorecard("prod")
        self.assertEqual(s["sample_size"], 7)
        fees = s["fees"]
        self.assertEqual((fees["n_with_fees_r"], fees["fees_r_unavailable"]), (5, 2))
        self.assertAlmostEqual(fees["total_fees_r"], 0.21)
        self.assertAlmostEqual(fees["mean_fees_r"], 0.042)
        self.assertIn("BNB", fees["note"])
        buckets = {b["bucket"]: b for b in s["fees_by_stop_bucket"]["buckets"]}
        self.assertEqual(list(buckets), ["<1.5", "1.5-2", "2-3", "3-4", ">=4"])
        self.assertEqual({k: b["n"] for k, b in buckets.items()}, {"<1.5": 1, "1.5-2": 1, "2-3": 2, "3-4": 1, ">=4": 1})
        self.assertEqual((buckets["2-3"]["n_with_fees_r"], buckets["2-3"]["mean_fees_r"]), (1, 0.04))
        self.assertEqual(buckets["1.5-2"]["mean_fees_r"], 0.07)  # 1.5 is the lower edge of 1.5-2
        self.assertEqual(s["fees_by_stop_bucket"]["stop_distance_unavailable"], 1)
        gvn = s["gross_vs_net"]
        self.assertEqual((gvn["n"], gvn["rows_without_both"]), (6, 1))  # the same rows for both expectancies
        self.assertAlmostEqual(gvn["expectancy_r_gross"], (1.05 - 0.93 + 2.04 - 0.97 + 0.52 + 1.0) / 6, places=4)
        self.assertAlmostEqual(gvn["expectancy_r_net"], (1.0 - 1.0 + 2.0 - 1.0 + 0.5 + 1.0) / 6, places=4)
        d = s["direction_split"]
        self.assertEqual((d["LONG"]["n"], d["SHORT"]["n"]), (5, 2))
        self.assertEqual((d["SHORT"]["win_rate_pct"], d["SHORT"]["total_r"]), (0.0, -2.0))
        self.assertAlmostEqual(d["LONG"]["total_r"], 1.0 + 2.0 + 0.5 + 0.8 + 1.0)  # R basis: net, else gross
        self.assertAlmostEqual(d["LONG"]["expectancy_r_net"], (1.0 + 2.0 + 0.5 + 1.0) / 4)
        self.assertAlmostEqual(d["SHORT"]["expectancy_r_gross"], -0.95)
        self.assertEqual(set(s["tiers_breakdown"]), {"S", "A+", "A", "YOLO", "unknown"})
        code, out = self.run_cli([])
        self.assertEqual(code, 0)
        for text in ("💸 FEES IN R", "Mean per trade: 0.0420R", "Stop 1.5-2%: n=1", "↔️  BY DIRECTION:",
                     "- SHORT: 2 trades", "FEE-IN-R GATE BACK-TEST", "recommends no threshold"):
            self.assertIn(text, out)

    def test_backtest_boundaries_and_insufficient_n(self):
        # 2% stop: expected fee exactly expected_fee_r(2.0) = 0.05R -> kept at 0.05 (the gate rejects only above)
        rows = [frow(-1.0, -0.95, stop=2.0) for _ in range(10)] + [frow(1.0, 1.02, stop=4.0) for _ in range(3)]
        rows.append(frow(0.5, 0.5, initial_risk=1.0, entry_price=50.0))  # reconstructed: 2% stop
        rows.append(frow(None, 0.3, stop=2.0))  # no net R: counted, not used
        rows.append(frow(0.2, 0.2, risk=None))  # no stop distance at all
        self.write_outcomes(rows)
        bt = sc.generate_scorecard("prod")["fee_threshold_backtest"]
        self.assertEqual((bt["n"], bt["rows_without_net_r"], bt["rows_without_stop_distance"]), (14, 1, 1))
        self.assertEqual(bt["fee_model"], "taker entry + taker SL")
        self.assertIn("recommends no threshold", bt["note"])
        by = {t["max_fee_r"]: t for t in bt["thresholds"]}
        self.assertEqual(list(by), [0.03, 0.04, 0.05, 0.06, 0.08, 0.10])
        self.assertEqual((by[0.04]["kept"]["n"], by[0.04]["rejected"]["n"]), (3, 11))
        self.assertEqual((by[0.05]["kept"]["n"], by[0.05]["rejected"]["n"]), (14, 0))
        self.assertTrue(by[0.04]["kept"]["insufficient_n"])
        self.assertFalse(by[0.04]["rejected"]["insufficient_n"])
        self.assertAlmostEqual(by[0.04]["kept"]["expectancy_r_net"], 1.0)
        self.assertAlmostEqual(by[0.04]["rejected"]["total_r_net"], -9.5)
        self.assertIsNone(by[0.05]["rejected"]["expectancy_r_net"])
        self.assertTrue(by[0.05]["rejected"]["insufficient_n"])
        self.assertFalse(by[0.03]["rejected"]["insufficient_n"])

    def test_calibration_store_keeps_only_its_fields(self):
        rows = [frow(1.0, 1.05, fees=0.05, stop=2.0, dossier_score=85, score_schema_version=scal.SCORE_SCHEMA_VERSION,
                     audit_ts=1000 + i, entry_liquidity="taker", exit_liquidity="mixed") for i in range(3)]
        self.write_outcomes(rows)
        self.assertEqual(self.run_cli([])[0], 0)
        store = scal.load_calibration(self.ws)
        self.assertTrue(store["trades"])
        for trade in store["trades"].values():
            self.assertTrue(set(trade) <= set(scal._STORE_FIELDS), set(trade) - set(scal._STORE_FIELDS))
            for key in ("fees_r", "stop_distance_pct", "entry_liquidity", "exit_liquidity"):
                self.assertNotIn(key, trade)


# =============================================================================
# 6. Screening candidates and the brief
# =============================================================================
class TestCandidatesAndBrief(unittest.TestCase):

    def test_radar_candidate_carries_expected_fee_r(self):
        row = {"symbol": "AAAUSDT", "direction": "SHORT", "price": 100.0, "sl": 102.0, "trigger": 100.0,
               "tp1": 95.0, "tp2": 88.0, "confidence": 64, "tier": "Tier A (x)", "tier_code": "A", "micro": {}}
        sizing = {"required_margin": 10.0, "step_qty": 1.0, "actual_notional": 30.0, "actual_dollar_risk": 1.9}
        with patch("quant_risk_engine.calculate_dynamic_equity_sizing", return_value=sizing), \
             patch("microstructure_engine.get_live_aggtrades_tape", return_value={}), \
             patch.object(sp, "_profile_standard_sizing", return_value=(3, 0.30)):
            setup = sp.enrich_and_size_candidate(row, "prod")
            tight = sp.enrich_and_size_candidate(dict(row, sl=101.5), "prod")
        self.assertEqual(setup.risk_pct, 2.0)
        self.assertEqual(setup.expected_fee_r, 0.05)
        self.assertEqual(tight.expected_fee_r, round(gl.expected_fee_r(1.5), 3))  # 0.067
        self.assertIn("expected_fee_r", setup.model_dump())

    def test_yolo_candidate_carries_expected_fee_r(self):
        raw = t52.TestYoloLevelsFromTrigger._raw(price=0.995, trigger=1.0, sl=0.98, tp1=1.038, tp2=1.0622)
        cand = sp.build_yolo_slot(t52._scan_payload(longs=[raw]))[1].candidates[0]
        self.assertEqual(cand.risk_pct, 2.0)
        self.assertEqual(cand.expected_fee_r, 0.05)

    def test_fee_r_helper_none(self):
        self.assertIsNone(sp._fee_r(0))
        self.assertIsNone(sp._fee_r(None))

    def test_brief_rows_rename_and_drop_null(self):
        row = peb._brief_opportunity({"symbol": "A", "expected_fee_r": 0.05, "risk_pct": 2.0})
        self.assertEqual(row, {"symbol": "A", "fee_r": 0.05, "risk_pct": 2.0})
        self.assertEqual(peb._brief_opportunity({"symbol": "A", "expected_fee_r": None}), {"symbol": "A"})
        self.assertEqual(peb._DROP_WHEN_UNSET["fee_r"], None)
        slot = peb.build_yolo_slot_brief({"yolo_slot": {"status": "ACTIVE", "candidates": [
            {"symbol": "PEPE", "risk_pct": 2.0, "expected_fee_r": 0.05}, {"symbol": "WIF", "expected_fee_r": None}]}})
        self.assertEqual(slot["candidates"], [{"symbol": "PEPE", "risk_pct": 2.0, "fee_r": 0.05}, {"symbol": "WIF"}])


class TestBriefSizeWithFeeR(t271.BriefCase):

    def test_session_brief_with_fee_r_still_fits(self):
        screening = t271.session_screening(4)
        for c in screening["top_candidates"]:
            c["expected_fee_r"] = round(gl.expected_fee_r(c["risk_pct"]), 3)
        ledger = t187.real_shaped_ledger()
        with contextlib.redirect_stderr(io.StringIO()), \
             patch.object(peb, "load_recent_insights", wraps=peb.load_recent_insights) as fn:
            brief = self.assemble(screening, self.ledger_file(ledger))
        budget = fn.call_args_list[0].kwargs["budget_bytes"]
        self.assertTrue(all(o["fee_r"] == 0.049 and "expected_fee_r" not in o for o in brief["filtered_opportunities"]))
        self.assertLessEqual(os.path.getsize(self.brief_file), peb.BRIEF_BUDGET_BYTES)
        self.assertGreaterEqual(budget, peb.LESSON_FLOOR_BYTES)
        self.assertEqual((peb.BRIEF_BUDGET_BYTES, peb.LESSON_BUDGET_BYTES, peb.LESSON_FLOOR_BYTES), (11600, 5000, 3000))


class TestEvaluatorPrompt(unittest.TestCase):

    def test_one_rule_4_sentence_and_generated_copy(self):
        with open(t271.AGENT_MD, encoding="utf-8") as f:
            text = f.read()
        rule4 = text.split("- RULE 4 (Financial Friction Filter):")[1].split("- RULE 5")[0]
        self.assertEqual(text.count("`fee_r`"), 1)
        self.assertIn("`fee_r`", rule4)
        self.assertIn("never a reason to approve", rule4)
        with open(t271.CLAUDE_MD, encoding="utf-8") as f:
            self.assertIn(rule4, f.read())


# =============================================================================
# 7. Doctor
# =============================================================================
def _fee_fake(rate=None, burn=None, raise_on=None):
    def fake(method, endpoint, params=None, target_env=None, **kw):
        if method != "GET":
            raise AssertionError(f"doctor must stay read-only ({method} {endpoint})")
        if endpoint == raise_on:
            raise RuntimeError("boom")
        if endpoint == "/fapi/v2/balance":
            return [{"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}]
        if endpoint == "/fapi/v1/commissionRate":
            assert params == {"symbol": "BTCUSDT"}, params
            return rate if rate is not None else []
        if endpoint == "/fapi/v1/feeBurn":
            return burn if burn is not None else []
        return []
    return fake


class TestDoctorFees(unittest.TestCase):

    def line(self, fake, mcp=False):
        with patch("execute_futures_trade.send_signed_request", side_effect=fake) as m:
            return trading_doctor.fee_status_line("prod", mcp=mcp), m

    def test_keys_ok(self):
        text, m = self.line(_fee_fake({"symbol": "BTCUSDT", "makerCommissionRate": "0.000200",
                                       "takerCommissionRate": "0.000500"}, {"feeBurn": True}))
        self.assertIn("BTCUSDT commission maker 0.0200% / taker 0.0500%", text)
        self.assertIn("BNB fee discount ON", text)
        self.assertEqual([c.args[1] for c in m.call_args_list], ["/fapi/v1/commissionRate", "/fapi/v1/feeBurn"])
        text, _ = self.line(_fee_fake({"takerCommissionRate": "0.0004"}, {"feeBurn": False}))
        self.assertIn("maker n/a / taker 0.0400%", text)  # a missing field is tolerated
        self.assertIn("BNB fee discount OFF", text)

    def test_list_and_error_replies(self):
        text, _ = self.line(_fee_fake())  # [] replies (the existing doctor fakes)
        self.assertIn("commission rate unavailable ([])", text)
        self.assertIn("BNB fee discount status unavailable ([])", text)
        text, _ = self.line(_fee_fake({"code": -2015, "msg": "Invalid API-key"}, {"error": "HTTP 400"}))
        self.assertIn("commission rate unavailable", text)
        self.assertIn("BNB fee discount status unavailable", text)
        text, _ = self.line(_fee_fake({"feeBurn": "true"}, {"feeBurn": "yes"}, raise_on="/fapi/v1/commissionRate"))
        self.assertIn("commission rate unavailable (RuntimeError)", text)
        self.assertIn("BNB fee discount status unavailable", text)

    def test_mcp_mode_makes_no_request(self):
        text, m = self.line(_fee_fake(), mcp=True)
        self.assertEqual(text, "Fee tier and BNB fee discount unavailable in MCP mode.")
        m.assert_not_called()

    def run_doctor(self, fake, api_key="key12345678"):
        ws = tempfile.mkdtemp()
        resp = MagicMock()
        resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        profile = {"profile_completed": True, "risk_pct_equity": 0.005, "yolo_slot_enabled": False}
        with patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.get_client_config", return_value=(api_key, "sec12345678", "http://x")), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("user_profile.load_user_profile", return_value=profile), \
             patch("trading_doctor.check_pretool_hook",
                   return_value={"ok": True, "critical": [], "warnings": [], "info": []}), \
             patch("shadow_tracker.calculate_efficacy_metrics", side_effect=RuntimeError("skip")), \
             patch.object(sss, "STATE_FILE", os.path.join(ws, "session_state.json")), \
             patch("sync_session_state.sync_session_state", MagicMock(return_value={"is_valid": True})), \
             patch("trading_doctor.check_guardian_service", return_value=("ok", "guardian alive")), \
             patch("trading_doctor.calibration_store_warning", return_value=None):
            mock_urlopen.return_value.__enter__.return_value = resp
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = trading_doctor.run_doctor(target_env="testnet")
        return code, out.getvalue()

    def test_exit_code_and_status_unchanged(self):
        ok_code, ok_out = self.run_doctor(_fee_fake({"makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"},
                                                    {"feeBurn": True}))
        bad_code, bad_out = self.run_doctor(_fee_fake(raise_on="/fapi/v1/feeBurn"))
        empty_code, empty_out = self.run_doctor(_fee_fake())
        self.assertEqual(ok_code, bad_code)
        self.assertEqual(ok_code, empty_code)
        self.assertIn("ℹ️  [FEES] BTCUSDT commission maker 0.0200% / taker 0.0500%; BNB fee discount ON", ok_out)
        self.assertIn("ℹ️  [FEES] commission rate unavailable", empty_out)
        self.assertIn("BNB fee discount status unavailable (RuntimeError)", bad_out)
        status = [l for l in ok_out.splitlines() if "STATUS:" in l]
        self.assertEqual(status, [l for l in empty_out.splitlines() if "STATUS:" in l])
        self.assertEqual(status, [l for l in bad_out.splitlines() if "STATUS:" in l])
        _code, mcp_out = self.run_doctor(_fee_fake(), api_key="MCP_OAUTH_ACTIVE")
        self.assertIn("ℹ️  [FEES] Fee tier and BNB fee discount unavailable in MCP mode.", mcp_out)


if __name__ == "__main__":
    unittest.main()
