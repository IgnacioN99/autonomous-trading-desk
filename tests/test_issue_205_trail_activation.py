#!/usr/bin/env python3
"""
test_issue_205_trail_activation.py - No trail before +1R (issue #205).

- calculate_structural_stop: profile exit_management.trail_activation "r_only" (default), "r_and_atr" and "r_or_atr"
  (legacy) on a wide-stop fixture (2x ATR_15m < 1R, MFE 0.75R), a +1R-but-under-2x-ATR fixture and a no-R fixture
  (+2x ATR_15m stays the only pre-TP1 activation); unknown / missing mode = r_only; the profile is read only when the
  mode decides the result.
- TAOUSDT replay (2026-10-08: entry 261.34, SL 247.16, trail activated by atr_expansion at MFE 0.75R): the planned SL
  is kept under the default, through calculate_structural_stop and update_position_to_structural_stop.
- user_profile.get_exit_management: trail_activation default, valid values, invalid values fall back with a warning.
Hermetic: synthetic klines, injected filters, FakeExchange, temp workspaces.
"""

import os
import sys
import time
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "loops"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import dynamic_exit_manager as dem
import user_profile as up
from test_exit_management import FakeExchange, offline, long_position, stop, write_audit
from test_issue_95_trailing_activation import make_klines, flat_pre, closed_atr, market
from test_issue_106_exit_manager_hardening import TICK_FILTERS, LONG_POST, LONG_FORMING

# Run-up to 103.6 (MFE 3.6 >= 2x ATR ~2) then a pullback (test_issue_95's _long_run).
RUN_POST = [(101.0, 100.0, 100.8), (102.0, 100.8, 101.8), (103.6, 101.8, 103.2), (103.3, 102.0, 102.2)]
RUN_FORMING = (102.6, 102.1, 102.4)


def em(mode=None):
    out = up.get_exit_management({})
    if mode is None:
        out.pop("trail_activation")
    else:
        out["trail_activation"] = mode
    return out


def mirror(bars):
    """SHORT mirror around 100 of (high, low, close) bars."""
    return [(200.0 - l, 200.0 - h, 200.0 - c) for (h, l, c) in bars]


def calc_for(post, forming, planned_sl, mode="r_only", current_sl=None, mark=None, exit_management="mode",
             direction="LONG"):
    klines, entry_ts = make_klines(flat_pre(), post, forming, time.time())
    cur = planned_sl if current_sl is None else current_sl
    with market(klines):
        calc = dem.calculate_structural_stop(
            "BTCUSDT", direction, 100.0, current_sl_price=cur, target_env="testnet", planned_sl=planned_sl,
            entry_ts=entry_ts, tp1_filled=False, mark_price=mark or float(forming[2]),
            exit_management=em(mode) if exit_management == "mode" else exit_management, filters=dict(TICK_FILTERS))
    return calc, klines


class TestActivationModes(unittest.TestCase):

    def test_wide_stop_atr_before_1r(self):
        # R = 4.8 (SL 95.2): MFE 3.6 = 0.75R, 2x ATR_15m already reached.
        expected = {"r_only": None, "r_and_atr": None, "r_or_atr": "atr_expansion"}
        for mode, reason in expected.items():
            with self.subTest(mode=mode):
                calc, klines = calc_for(RUN_POST, RUN_FORMING, 95.2, mode=mode)
                self.assertGreaterEqual(calc["mfe"], 2.0 * closed_atr(klines))
                self.assertLess(calc["mfe"], calc["initial_risk"])
                self.assertAlmostEqual(calc["mfe"] / calc["initial_risk"], 0.75, places=6)
                self.assertEqual(calc["activation_reason"], reason)
                if reason is None:
                    self.assertEqual(calc["reason"], "trail_not_activated")
                    self.assertFalse(calc["should_update"])
                    self.assertEqual(calc["new_structural_sl"], 95.2)
                    self.assertIsNone(calc["profit_lock"])
                else:
                    self.assertTrue(calc["should_update"])
                    self.assertGreater(calc["new_structural_sl"], 95.2)

    def test_short_wide_stop_atr_before_1r(self):
        # SHORT mirror: R = 4.8 (SL 104.8), MFE 3.6 = 0.75R (low 96.4), 2x ATR_15m already reached.
        expected = {"r_only": None, "r_and_atr": None, "r_or_atr": "atr_expansion"}
        for mode, reason in expected.items():
            with self.subTest(mode=mode):
                calc, klines = calc_for(mirror(RUN_POST), mirror([RUN_FORMING])[0], 104.8, mode=mode,
                                        direction="SHORT")
                self.assertGreaterEqual(calc["mfe"], 2.0 * closed_atr(klines))
                self.assertAlmostEqual(calc["initial_risk"], 4.8, places=9)
                self.assertAlmostEqual(calc["mfe"] / calc["initial_risk"], 0.75, places=6)
                self.assertEqual(calc["activation_reason"], reason)
                if reason is None:
                    self.assertEqual(calc["reason"], "trail_not_activated")
                    self.assertFalse(calc["should_update"])
                    self.assertEqual(calc["new_structural_sl"], 104.8)
                    self.assertIsNone(calc["profit_lock"])
                else:
                    self.assertTrue(calc["should_update"])
                    self.assertLess(calc["new_structural_sl"], 104.8)

    def test_r_and_atr_both_reached(self):
        # R = 2 (SL 98): MFE 1.8R and >= 2x ATR: every mode activates on r_multiple.
        for mode in ("r_only", "r_and_atr", "r_or_atr"):
            with self.subTest(mode=mode):
                calc, _ = calc_for(RUN_POST, RUN_FORMING, 98.0, mode=mode)
                self.assertEqual(calc["activation_reason"], "r_multiple")
                self.assertTrue(calc["should_update"])

    def test_1r_reached_under_2x_atr(self):
        # R = 1 (SL 99): MFE 1.2R but under 2x ATR: r_and_atr waits for the ATR expansion too.
        expected = {"r_only": "r_multiple", "r_and_atr": None, "r_or_atr": "r_multiple"}
        for mode, reason in expected.items():
            with self.subTest(mode=mode):
                calc, klines = calc_for(LONG_POST, LONG_FORMING, 99.0, mode=mode, mark=101.0)
                self.assertLess(calc["mfe"], 2.0 * closed_atr(klines))
                self.assertGreaterEqual(calc["mfe"], calc["initial_risk"])
                self.assertEqual(calc["activation_reason"], reason)
                if reason is None:
                    self.assertEqual(calc["new_structural_sl"], 99.0)

    def test_no_r_reference_keeps_atr_expansion_in_every_mode(self):
        # Current stop already above entry (no R): +2x ATR_15m is the only pre-TP1 activation.
        for mode in ("r_only", "r_and_atr", "r_or_atr"):
            with self.subTest(mode=mode):
                calc, _ = calc_for(RUN_POST, RUN_FORMING, None, mode=mode, current_sl=100.2)
                self.assertIsNone(calc["initial_risk"])
                self.assertEqual(calc["activation_reason"], "atr_expansion")

    def test_missing_or_unknown_mode_is_r_only(self):
        for exit_management in (em(None), em("bogus"), {"profit_lock_enabled": False}):
            with self.subTest(exit_management=exit_management):
                calc, _ = calc_for(RUN_POST, RUN_FORMING, 95.2, exit_management=exit_management)
                self.assertEqual(calc["reason"], "trail_not_activated")

    def test_profile_read_only_when_the_mode_decides(self):
        legacy = em("r_or_atr")
        with patch("user_profile.get_exit_management", return_value=legacy) as get:
            calc, _ = calc_for(LONG_POST, LONG_FORMING, 98.0, exit_management=None)  # neither +1R nor 2x ATR
            self.assertIsNone(calc["activation_reason"])
            self.assertEqual(get.call_count, 0)
            calc, _ = calc_for(RUN_POST, RUN_FORMING, 95.2, exit_management=None)  # only ATR reached
            self.assertEqual(calc["activation_reason"], "atr_expansion")
            self.assertEqual(get.call_count, 1)


# TAOUSDT LONG 2026-10-08: entry 261.34, SL 247.16 (R 14.18); the trail activated by atr_expansion at MFE 0.75R and
# closed the trade at 263.99.
TAO = "TAOUSDT"
TAO_ENTRY, TAO_SL = 261.34, 247.16
TAO_FILTERS = {"stepSize": 0.001, "minQty": 0.001, "tickSize": 0.01, "precision_qty": 3, "precision_price": 2,
               "minNotional": 5.0}
TAO_HIGH = round(TAO_ENTRY + 0.75 * (TAO_ENTRY - TAO_SL), 3)  # 271.975: MFE 0.75R


def tao_klines():
    c = TAO_ENTRY
    offs = [0.0, 1.2, 2.4, 1.2]
    pre = [(c + offs[i % 4] + 2.0, c + offs[i % 4] - 2.0, c + offs[i % 4]) for i in range(40)]
    post = [(264.0, 260.5, 263.5), (268.0, 263.0, 267.0), (TAO_HIGH, 266.5, 270.0), (270.5, 266.0, 267.0)]
    return make_klines(pre, post, (268.0, 266.5, 267.2), time.time())


class TestTaoReplay(unittest.TestCase):

    def _calc(self, mode):
        klines, entry_ts = tao_klines()
        with market(klines):
            calc = dem.calculate_structural_stop(TAO, "LONG", TAO_ENTRY, current_sl_price=TAO_SL, target_env="prod",
                                                 planned_sl=TAO_SL, entry_ts=entry_ts, tp1_filled=False,
                                                 mark_price=267.2, reference_source="trade_audit",
                                                 exit_management=em(mode), filters=dict(TAO_FILTERS))
        return calc, klines

    def test_default_keeps_the_planned_stop(self):
        calc, klines = self._calc("r_only")
        self.assertGreaterEqual(calc["mfe"], 2.0 * closed_atr(klines))  # the old atr_expansion condition held
        self.assertAlmostEqual(calc["mfe"] / calc["initial_risk"], 0.75, places=6)
        self.assertIsNone(calc["activation_reason"])
        self.assertEqual(calc["reason"], "trail_not_activated")
        self.assertFalse(calc["should_update"])
        self.assertEqual(calc["new_structural_sl"], TAO_SL)

    def test_legacy_mode_reproduces_the_early_trail(self):
        calc, _ = self._calc("r_or_atr")
        self.assertEqual(calc["activation_reason"], "atr_expansion")
        self.assertTrue(calc["should_update"])
        self.assertGreater(calc["new_structural_sl"], TAO_SL)

    def test_update_path_writes_nothing(self):
        klines, entry_ts = tao_klines()
        fake = FakeExchange([long_position(symbol=TAO, entry=str(TAO_ENTRY), mark="267.2")],
                            algos=[stop(701, TAO_SL, symbol=TAO)])
        with offline(fake) as ws, market(klines), \
                patch("execute_futures_trade.get_symbol_filters", return_value=dict(TAO_FILTERS)):
            write_audit(ws, symbol=TAO, direction="LONG", entry_price=TAO_ENTRY, sl_price=TAO_SL, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="prod", timestamp=entry_ts)
            res = dem.update_position_to_structural_stop(TAO, target_env="prod")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertEqual(res["reference_source"], "trade_audit")
        self.assertEqual(res["current_sl"], TAO_SL)
        self.assertEqual(fake.writes(), [])


class TestTrailActivationProfile(unittest.TestCase):

    def test_default(self):
        self.assertEqual(up.get_exit_management({})["trail_activation"], "r_only")
        self.assertEqual(up.DEFAULT_EXIT_MANAGEMENT["trail_activation"], "r_only")

    def test_valid_values(self):
        for mode in up.TRAIL_ACTIVATION_MODES:
            out = up.get_exit_management({"exit_management": {"trail_activation": mode}})
            self.assertEqual((out["trail_activation"], out["warnings"]), (mode, []))

    def test_invalid_values_fall_back_with_a_warning(self):
        for value in ("R_ONLY", "", "atr", None, 1, True, ["r_only"]):
            with self.subTest(value=value):
                out = up.get_exit_management({"exit_management": {"trail_activation": value,
                                                                  "extend_last_step": False}})
                self.assertEqual(out["trail_activation"], "r_only")
                self.assertEqual(len(out["warnings"]), 1, out["warnings"])
                self.assertTrue(out["warnings"][0].startswith("exit_management.trail_activation invalid"))
                self.assertFalse(out["extend_last_step"])  # the valid sibling key still applies

    def test_example_profile_uses_the_default(self):
        import json
        with open(os.path.join(BASE_DIR, "config", "user_profile.json.example"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual(example["exit_management"]["trail_activation"], "r_only")


if __name__ == "__main__":
    unittest.main()
