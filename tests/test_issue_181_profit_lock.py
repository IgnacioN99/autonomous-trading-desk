#!/usr/bin/env python3
"""
test_issue_181_profit_lock.py - R-based profit-lock ratchet and immediate lock on a verified TP1 fill (issue #183,
Phase 1 of #181).

- calculate_structural_stop: default steps (1R -> True Net BE, 2R -> +1R, 3R -> +2R, +1R per further full R) for
  LONG and SHORT, never loosens, the 0.5x ATR price floor caps the lock, disabled = old result, no lock before True
  Net BE is allowed (#106 cap unchanged) or without R.
- update_position_to_structural_stop: ONDO replay (verified TP1, closed-15m MFE 1.9R, 1m low at +3.45R -> stop at
  +2R in the same call), intrabar read failure, unverified TP1 (no 1m read), YOLO before TP1, dry run.
- user_profile.get_exit_management: defaults, key-by-key merge, invalid values fall back with a warning.
- Guardian: trail_stop action and view["trailing"] carry profit_lock.
Hermetic: FakeExchange, synthetic klines, trade_excursion.fetch_klines_range patched, temp workspaces.
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
from utils import trade_excursion as tx
from test_exit_management import FakeExchange, offline, long_position, stop, write_audit, structural, ALGO_ENDPOINT
from test_issue_95_trailing_activation import make_klines, flat_pre, closed_atr, market, user_trades_exchange
from test_issue_106_exit_manager_hardening import (TICK_FILTERS, LONG_POST, LONG_FORMING, SHORT_POST,
                                                   SHORT_FORMING)
from test_guardian_excursion import GuardianExcursionBase

MIN = 60_000
DEFAULTS = up.get_exit_management({})


def em(**over):
    out = up.get_exit_management({})
    out.update(over)
    return out


def run_calc(direction, x, *, tp1=True, current_sl=None, planned_sl="default", mark=None, exit_management=None,
             **kw):
    """Entry 100, R = 1 (planned SL 99 / 101); one closed 15m bar after entry reaching +x (MFE x R)."""
    is_long = direction == "LONG"
    if is_long:
        post, forming = [(100 + x, 100 + x - 1.0, 100 + x - 0.5)], (100 + x - 0.3, 100 + x - 0.8, 100 + x - 0.5)
        planned = 99.0 if planned_sl == "default" else planned_sl
    else:
        post, forming = [(100 - x + 1.0, 100 - x, 100 - x + 0.5)], (100 - x + 0.8, 100 - x + 0.3, 100 - x + 0.5)
        planned = 101.0 if planned_sl == "default" else planned_sl
    klines, entry_ts = make_klines(flat_pre(), post, forming, time.time())
    cur = planned if current_sl is None else current_sl
    with patch("execute_futures_trade.get_symbol_filters", return_value=dict(TICK_FILTERS)), market(klines):
        calc = dem.calculate_structural_stop("BTCUSDT", direction, 100.0, current_sl_price=cur, target_env="testnet",
                                             planned_sl=planned, entry_ts=entry_ts, tp1_filled=tp1,
                                             mark_price=(100 + x if is_long else 100 - x) if mark is None else mark,
                                             exit_management=exit_management or DEFAULTS, **kw)
    return calc, klines


class TestProfitLockSteps(unittest.TestCase):

    def _check(self, direction, x, lock_r, step_mfe_r, expected_sl, exit_management=None, binding=True):
        calc, _ = run_calc(direction, x, exit_management=exit_management)
        lock = calc["profit_lock"]
        self.assertIsNotNone(lock, calc)
        self.assertEqual(calc["activation_reason"], "tp1_filled")
        self.assertEqual((lock["lock_r"], lock["step_mfe_r"], lock["mfe_source"]), (lock_r, step_mfe_r, "closed_15m"))
        self.assertAlmostEqual(lock["mfe_r"], x, places=6)
        self.assertFalse(lock["capped_by_price_floor"])
        if binding:
            self.assertTrue(lock["binding"], calc)
            self.assertAlmostEqual(calc["new_structural_sl"], expected_sl, places=9)
        return calc

    def test_long_steps(self):
        self._check("LONG", 1.2, 0.0, 1.0, 100.2, binding=False)  # True Net BE (same price as the BE clamp)
        self._check("LONG", 2.4, 1.0, 2.0, 101.0)
        self._check("LONG", 3.45, 2.0, 3.0, 102.0)
        self._check("LONG", 4.3, 3.0, 4.0, 103.0)  # extend_last_step: +1R per further full R

    def test_short_steps(self):
        self._check("SHORT", 1.2, 0.0, 1.0, 99.8, binding=False)
        self._check("SHORT", 2.4, 1.0, 2.0, 99.0)
        self._check("SHORT", 3.45, 2.0, 3.0, 98.0)
        self._check("SHORT", 4.3, 3.0, 4.0, 97.0)

    def test_be_step_puts_stop_at_true_net_be(self):
        long_calc, _ = run_calc("LONG", 1.2)
        short_calc, _ = run_calc("SHORT", 1.2)
        self.assertAlmostEqual(long_calc["profit_lock"]["lock_price"], 100.2, places=9)
        self.assertGreaterEqual(long_calc["new_structural_sl"], 100.2)
        self.assertAlmostEqual(short_calc["profit_lock"]["lock_price"], 99.8, places=9)
        self.assertLessEqual(short_calc["new_structural_sl"], 99.8)

    def test_extend_last_step_false_stops_at_last_step(self):
        for direction, lock_price in (("LONG", 102.0), ("SHORT", 98.0)):
            calc = self._check(direction, 4.3, 2.0, 3.0, None, exit_management=em(extend_last_step=False),
                               binding=False)
            self.assertAlmostEqual(calc["profit_lock"]["lock_price"], lock_price, places=9)
            if direction == "LONG":
                self.assertGreaterEqual(calc["new_structural_sl"], 102.0)
                self.assertLess(calc["new_structural_sl"], 103.0)
            else:
                self.assertLessEqual(calc["new_structural_sl"], 98.0)
                self.assertGreater(calc["new_structural_sl"], 97.0)

    def test_lock_never_loosens_a_tighter_stop(self):
        no_mark = em(lock_on_tp1=False)
        calc, _ = run_calc("LONG", 2.4, current_sl=101.5, mark=105.0, exit_management=no_mark)
        self.assertEqual(calc["profit_lock"]["lock_r"], 1.0)
        self.assertEqual(calc["new_structural_sl"], 101.5)
        self.assertFalse(calc["should_update"])
        self.assertFalse(calc["profit_lock"]["binding"])
        calc, _ = run_calc("SHORT", 2.4, current_sl=98.5, mark=95.0, exit_management=no_mark)
        self.assertEqual(calc["profit_lock"]["lock_r"], 1.0)
        self.assertEqual(calc["new_structural_sl"], 98.5)
        self.assertFalse(calc["should_update"])

    def test_price_floor_caps_the_lock(self):
        calc, klines = run_calc("LONG", 3.45, mark=102.2)
        atr = closed_atr(klines)
        lock = calc["profit_lock"]
        self.assertEqual(lock["lock_r"], 2.0)
        self.assertTrue(lock["capped_by_price_floor"])
        self.assertFalse(lock["binding"])
        self.assertLess(calc["new_structural_sl"], 102.0)
        self.assertLessEqual(calc["new_structural_sl"], 102.2 - 0.5 * atr)
        calc, klines = run_calc("SHORT", 3.45, mark=97.8)
        atr = closed_atr(klines)
        self.assertTrue(calc["profit_lock"]["capped_by_price_floor"])
        self.assertGreater(calc["new_structural_sl"], 98.0)
        self.assertGreaterEqual(calc["new_structural_sl"], 97.8 + 0.5 * atr - 0.1)

    def test_disabled_gives_the_pre_lock_result(self):
        for direction in ("LONG", "SHORT"):
            off, _ = run_calc(direction, 2.4, exit_management=em(profit_lock_enabled=False))
            unreachable, _ = run_calc(direction, 2.4,
                                      exit_management=em(profit_lock_steps=[{"mfe_r": 50.0, "lock_r": 1.0}]))
            on, _ = run_calc(direction, 2.4)
            self.assertIsNone(off["profit_lock"])
            self.assertIsNone(unreachable["profit_lock"])
            self.assertEqual({k: v for k, v in off.items() if k != "chandelier_anchor"},
                             {k: v for k, v in unreachable.items() if k != "chandelier_anchor"})
            if direction == "LONG":
                self.assertLess(off["new_structural_sl"], on["new_structural_sl"])
            else:
                self.assertGreater(off["new_structural_sl"], on["new_structural_sl"])

    def test_no_lock_before_be_is_allowed(self):
        # +1.2R (step 1R reached) but no TP1 and MFE under 2x ATR: the #106 cap (one tick short of entry) stays.
        for direction, post, forming, sl, mark, capped in (("LONG", LONG_POST, LONG_FORMING, 99.0, 101.0, 99.9),
                                                           ("SHORT", SHORT_POST, SHORT_FORMING, 101.0, 99.0, 100.1)):
            klines, entry_ts = make_klines(flat_pre(), post, forming, time.time())
            with patch("execute_futures_trade.get_symbol_filters", return_value=dict(TICK_FILTERS)), market(klines):
                calc = dem.calculate_structural_stop("BTCUSDT", direction, 100.0, current_sl_price=sl,
                                                     target_env="testnet", planned_sl=sl, entry_ts=entry_ts,
                                                     tp1_filled=False, mark_price=mark, exit_management=DEFAULTS)
            self.assertEqual(calc["activation_reason"], "r_multiple")
            self.assertLess(calc["mfe"], 2.0 * closed_atr(klines))
            self.assertIsNone(calc["profit_lock"])
            self.assertEqual(calc["new_structural_sl"], capped)

    def test_no_lock_without_initial_risk(self):
        calc, _ = run_calc("LONG", 2.4, planned_sl=None, current_sl=0.0)
        self.assertEqual(calc["activation_reason"], "tp1_filled")
        self.assertIsNone(calc["initial_risk"])
        self.assertIsNone(calc["profit_lock"])

    def test_tp1_price_and_intrabar_raise_the_lock_mfe_only_on_tp1(self):
        calc, _ = run_calc("LONG", 1.2, intrabar_extreme=103.45, mark=104.0, exit_management=em(lock_on_tp1=True))
        self.assertEqual(calc["profit_lock"]["mfe_source"], "mark")
        calc, _ = run_calc("LONG", 1.2, tp1_price=102.5)
        self.assertEqual((calc["profit_lock"]["mfe_source"], calc["profit_lock"]["lock_r"]), ("tp1_price", 1.0))
        # A malformed or loss-side tp1_price is ignored.
        for bad in ("abc", 95.0, -1):
            calc, _ = run_calc("LONG", 1.2, tp1_price=bad)
            self.assertEqual(calc["profit_lock"]["mfe_source"], "closed_15m")
        # lock_on_tp1 False: only the closed-15m MFE counts.
        calc, _ = run_calc("LONG", 1.2, tp1_price=102.5, intrabar_extreme=103.0,
                           exit_management=em(lock_on_tp1=False))
        self.assertEqual((calc["profit_lock"]["mfe_source"], calc["profit_lock"]["lock_r"]), ("closed_15m", 0.0))


# ONDO replay (2026-10-08): SHORT entry 0.4952, SL 0.5074 (R 0.0122), TP1 0.4732 filled, intrabar low 0.4531
# (+3.45R) while the closed 15m bars since entry only reached 0.47202 (+1.9R), mark 0.4652.
ONDO = "ONDOUSDT"
ONDO_FILTERS = {"stepSize": 1.0, "minQty": 1.0, "tickSize": 0.0001, "precision_qty": 0, "precision_price": 4,
                "minNotional": 5.0}
ONDO_ENTRY, ONDO_SL, ONDO_TP1, ONDO_MARK = 0.4952, 0.5074, 0.4732, 0.4652
ONDO_R = ONDO_SL - ONDO_ENTRY


def ondo_klines():
    c = ONDO_ENTRY
    offs = [0.0, 0.0003, 0.0006, 0.0003]
    pre = [(c + offs[i % 4] + 0.002, c + offs[i % 4] - 0.002, c + offs[i % 4]) for i in range(40)]
    post = [(0.4950, 0.4890, 0.4900), (0.4905, 0.4820, 0.4830), (0.4835, 0.4760, 0.4770),
            (0.4780, 0.47202, 0.4740), (0.4760, 0.4730, 0.4745)]
    return make_klines(pre, post, (0.4750, 0.4640, 0.4652), time.time())


def ondo_fills(open_ts):
    """SELL 10 at open_ts then BUY 3 (TP1) 5s later: reconciles to positionAmt -7 opened at open_ts (verified)."""
    base = {"orderId": 1, "symbol": ONDO, "positionSide": "BOTH", "price": str(ONDO_ENTRY)}
    return [dict(base, id=1, side="SELL", qty="10", time=int(open_ts * 1000)),
            dict(base, id=2, orderId=2, side="BUY", qty="3", price=str(ONDO_TP1), time=int((open_ts + 5) * 1000))]


class OneMinuteBars:
    """Fake trade_excursion.fetch_klines_range for the intrabar read: two closed 1m bars (one with the 0.4531 low)
    and the forming bar (low 0.4400, must be ignored)."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def __call__(self, symbol, interval, start_ms, limit=None, target_env=None, timeout=None):
        self.calls.append({"symbol": symbol, "interval": interval, "start_ms": start_ms, "limit": limit,
                           "target_env": target_env})
        if self.error:
            raise self.error
        forming = int(time.time() * 1000) // MIN * MIN
        rows = [(forming - 2 * MIN, 0.4650, 0.4531), (forming - MIN, 0.4660, 0.4610), (forming, 0.4660, 0.4400)]
        return [[o, str(h), str(h), str(l), str(h), "1", o + MIN - 1] for o, h, l in rows]


class TestOndoReplayUpdatePath(unittest.TestCase):

    def _run(self, *, verified=True, yolo=False, amt="-7", minute_bars=None, dry_run=False, profile=None):
        klines, entry_ts = ondo_klines()
        pos = long_position(symbol=ONDO, amt=amt, entry=str(ONDO_ENTRY), mark=str(ONDO_MARK))
        algos = [stop(601, ONDO_SL, side="BUY", symbol=ONDO)]
        fills = ondo_fills(entry_ts) if amt == "-7" else ondo_fills(entry_ts)[:1]
        fake = user_trades_exchange([pos], fills, algos=algos) if verified else FakeExchange([pos], algos=algos)
        minute_bars = minute_bars or OneMinuteBars()
        with offline(fake, profile=profile) as ws, market(klines), \
                patch("execute_futures_trade.get_symbol_filters", return_value=dict(ONDO_FILTERS)), \
                patch("utils.trade_excursion.fetch_klines_range", side_effect=minute_bars):
            write_audit(ws, symbol=ONDO, direction="SHORT", entry_price=ONDO_ENTRY, sl_price=ONDO_SL,
                        tp1_price=ONDO_TP1, total_qty=10, tp1_qty=3, is_yolo=yolo, target_env="testnet",
                        timestamp=entry_ts)
            res = dem.update_position_to_structural_stop(ONDO, target_env="testnet", dry_run=dry_run)
        return res, fake, minute_bars, klines, entry_ts

    def test_verified_tp1_locks_plus_2r_in_the_same_call(self):
        res, fake, bars, klines, entry_ts = self._run()
        # Preconditions: closed 15m bars since entry only reached +1.9R; the price floor does not bind at +2R.
        self.assertAlmostEqual((ONDO_ENTRY - 0.47202) / ONDO_R, 1.9, places=6)
        self.assertLess(ONDO_MARK + 0.5 * closed_atr(klines), ONDO_ENTRY - 2 * ONDO_R)
        self.assertIs(res["tp1_filled"], True)
        self.assertTrue(res["updated"], res)
        self.assertLessEqual(res["new_sl"], ONDO_ENTRY - 2 * ONDO_R)  # 0.4708: at least +2R locked (SHORT)
        self.assertGreater(res["new_sl"], ONDO_MARK)
        lock = res["profit_lock"]
        self.assertEqual(lock["mfe_source"], "intrabar_1m")
        self.assertAlmostEqual(lock["mfe_r"], (ONDO_ENTRY - 0.4531) / ONDO_R, places=3)  # forming 1m bar ignored
        self.assertEqual((lock["lock_r"], lock["step_mfe_r"]), (2.0, 3.0))
        self.assertIn("profit lock +2R", res["message"])
        self.assertEqual(len(bars.calls), 1)
        call = bars.calls[0]
        self.assertEqual((call["symbol"], call["interval"], call["limit"], call["target_env"]),
                         (ONDO, "1m", 31, "testnet"))  # issue #197: forming + fill candle, weight 1
        self.assertGreaterEqual(call["start_ms"], tx.first_post_entry_bar_ms(entry_ts))
        self.assertLess(fake.write_index("POST", ALGO_ENDPOINT)[0], fake.write_index("DELETE", ALGO_ENDPOINT)[0])

    def test_intrabar_failure_still_locks_from_mark_and_tp1(self):
        res, _, bars, _, _ = self._run(minute_bars=OneMinuteBars(error=TimeoutError("timed out")))
        self.assertEqual(len(bars.calls), 1)
        self.assertIn("intrabar_unavailable", res["warnings"])
        self.assertTrue(res["success"], res)
        self.assertTrue(res["updated"], res)
        lock = res["profit_lock"]
        self.assertEqual(lock["mfe_source"], "mark")  # mark +2.46R (tp1_price ignored: no tp1_order_id, #197)
        self.assertEqual(lock["lock_r"], 1.0)
        self.assertLessEqual(res["new_sl"], ONDO_ENTRY - ONDO_R)

    def test_unverified_tp1_makes_no_intrabar_read(self):
        res, _, bars, _, _ = self._run(verified=False)
        self.assertIn("reference_unverified", res["warnings"])
        self.assertIsNone(res["tp1_filled"])
        self.assertEqual(bars.calls, [])
        # Closed-15m steps still apply via +2x ATR_15m (R from the record): +1.9R -> True Net BE, no TP1 lock.
        self.assertEqual(res["activation_reason"], "r_multiple")  # BE allowed by MFE >= 2x ATR_15m
        lock = res["profit_lock"]
        self.assertEqual((lock["mfe_source"], lock["lock_r"]), ("closed_15m", 0.0))
        self.assertAlmostEqual(lock["mfe_r"], 1.9, places=3)

    def test_yolo_before_tp1_unchanged(self):
        res, fake, bars, _, _ = self._run(yolo=True, amt="-10")
        self.assertEqual(res["reason"], "yolo_before_tp1")
        self.assertIs(res["tp1_filled"], False)
        self.assertEqual(bars.calls, [])
        self.assertNotIn("profit_lock", res)
        self.assertEqual(fake.writes(), [])

    def test_yolo_after_tp1_gets_the_same_steps(self):
        res, _, _, _, _ = self._run(yolo=True)
        self.assertTrue(res["updated"], res)
        self.assertEqual(res["profit_lock"]["lock_r"], 2.0)

    def test_dry_run_reports_the_lock_and_writes_nothing(self):
        res, fake, _, _, _ = self._run(dry_run=True)
        self.assertEqual(res["reason"], "dry_run")
        self.assertEqual(res["profit_lock"]["lock_r"], 2.0)
        self.assertLessEqual(res["planned_sl"], ONDO_ENTRY - 2 * ONDO_R)
        self.assertIn("profit lock +2R", res["message"])
        self.assertEqual(fake.writes(), [])

    def test_profile_disabled_makes_no_read_and_no_lock(self):
        profile = {"leverage_standard": 3, "exit_management": {"profit_lock_enabled": False}}
        res, _, bars, _, _ = self._run(profile=profile)
        self.assertEqual(bars.calls, [])
        self.assertIsNone(res["profit_lock"])
        self.assertGreater(res["new_sl"], ONDO_ENTRY - 2 * ONDO_R)  # the closed-15m trail only, no +2R lock

    def test_invalid_profile_warns_and_uses_defaults(self):
        profile = {"exit_management": {"profit_lock_steps": "fast"}}
        res, _, _, _, _ = self._run(profile=profile)
        self.assertTrue(any(w.startswith("exit_management.profit_lock_steps invalid") for w in res["warnings"]))
        self.assertEqual(res["profit_lock"]["lock_r"], 2.0)


class TestExitManagementProfile(unittest.TestCase):

    def test_defaults(self):
        for profile in ({}, {"exit_management": {}}, {"exit_management": None}):
            out = up.get_exit_management(profile)
            self.assertEqual(out["warnings"], [])
            self.assertTrue(out["profit_lock_enabled"])
            self.assertTrue(out["extend_last_step"])
            self.assertTrue(out["lock_on_tp1"])
            self.assertEqual(out["profit_lock_steps"], [{"mfe_r": 1.0, "lock_r": 0.0}, {"mfe_r": 2.0, "lock_r": 1.0},
                                                        {"mfe_r": 3.0, "lock_r": 2.0}])

    def test_defaults_are_not_shared(self):
        out = up.get_exit_management({})
        out["profit_lock_steps"][0]["lock_r"] = 0.5
        self.assertEqual(up.get_exit_management({})["profit_lock_steps"][0]["lock_r"], 0.0)

    def test_loads_profile_at_call_time(self):
        with patch("user_profile.load_user_profile", return_value={"exit_management": {"lock_on_tp1": False}}):
            out = up.get_exit_management()
        self.assertFalse(out["lock_on_tp1"])
        with patch("user_profile.load_user_profile", side_effect=OSError("boom")):
            out = up.get_exit_management()
        self.assertTrue(out["profit_lock_enabled"])
        self.assertEqual(len(out["warnings"]), 1)

    def test_partial_override_merged_key_by_key(self):
        out = up.get_exit_management({"exit_management": {
            "extend_last_step": False, "profit_lock_steps": [{"mfe_r": 1.5, "lock_r": 0}, {"mfe_r": 2.5, "lock_r": 1}]}})
        self.assertEqual(out["warnings"], [])
        self.assertFalse(out["extend_last_step"])
        self.assertTrue(out["profit_lock_enabled"])
        self.assertTrue(out["lock_on_tp1"])
        self.assertEqual(out["profit_lock_steps"], [{"mfe_r": 1.5, "lock_r": 0.0}, {"mfe_r": 2.5, "lock_r": 1.0}])

    def test_invalid_values_fall_back_per_key(self):
        cases = {
            "profit_lock_enabled": ["yes", 1, None],
            "extend_last_step": ["true", 0],
            "lock_on_tp1": [1.0, []],
            "profit_lock_steps": [
                [], "steps", {"mfe_r": 1.0, "lock_r": 0.0}, [1.0, 2.0],
                [{"mfe_r": 2.0, "lock_r": 1.0}, {"mfe_r": 1.0, "lock_r": 0.0}],   # not ascending
                [{"mfe_r": 1.0, "lock_r": 0.0}, {"mfe_r": 1.0, "lock_r": 0.5}],   # not strictly ascending
                [{"mfe_r": 1.0, "lock_r": 1.0}],                                   # lock_r == mfe_r
                [{"mfe_r": 1.0, "lock_r": -0.5}],                                  # negative lock_r
                [{"mfe_r": 0.0, "lock_r": 0.0}],                                   # mfe_r <= 0
                [{"mfe_r": "2", "lock_r": 1.0}],                                   # non-numeric
                [{"mfe_r": True, "lock_r": 0.0}],                                  # bool is not a number
                [{"mfe_r": float("nan"), "lock_r": 0.0}],
                [{"mfe_r": 2.0}],                                                  # missing lock_r
            ],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    out = up.get_exit_management({"exit_management": {key: value, "extend_last_step": False}
                                                  if key != "extend_last_step" else {key: value}})
                    self.assertEqual(out[key], DEFAULTS[key])
                    self.assertEqual(len(out["warnings"]), 1, out["warnings"])
                    self.assertTrue(out["warnings"][0].startswith(f"exit_management.{key} invalid"))
                    if key != "extend_last_step":  # the valid sibling key is still applied
                        self.assertFalse(out["extend_last_step"])

    def test_non_object_section_falls_back(self):
        for raw in ("on", [1], 3):
            out = up.get_exit_management({"exit_management": raw})
            self.assertEqual({k: v for k, v in out.items() if k != "warnings"},
                             {k: v for k, v in DEFAULTS.items() if k != "warnings"})
            self.assertEqual(len(out["warnings"]), 1)

    def test_example_profile_carries_the_defaults(self):
        import json
        with open(os.path.join(BASE_DIR, "config", "user_profile.json.example"), encoding="utf-8") as f:
            example = json.load(f)
        out = up.get_exit_management(example)
        self.assertEqual(out["warnings"], [])
        self.assertEqual(example["exit_management"]["profit_lock_steps"], DEFAULTS["profit_lock_steps"])


class TestGuardianCarriesProfitLock(GuardianExcursionBase):

    def test_trail_stop_action_and_view_carry_profit_lock(self):
        lock = {"mfe_r": 2.4, "mfe_source": "intrabar_1m", "step_mfe_r": 2.0, "lock_r": 1.0, "lock_price": 105.0,
                "capped_by_price_floor": False, "binding": True}
        fake = FakeExchange([long_position(mark="110.0")], algos=[stop(501, 95.0)])
        state = self.cycle(fake, calc=dict(structural(105.0), profit_lock=lock))
        view = state["positions"][0]
        self.assertTrue(view["trailing"]["updated"], view)
        self.assertEqual(view["trailing"]["profit_lock"], lock)
        actions = [a for a in state["actions"] if a["type"] == "trail_stop"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["detail"]["profit_lock"], lock)

    def test_dry_run_action_carries_profit_lock(self):
        lock = {"mfe_r": 3.1, "mfe_source": "closed_15m", "step_mfe_r": 3.0, "lock_r": 2.0, "lock_price": 105.0,
                "capped_by_price_floor": False, "binding": True}
        fake = FakeExchange([long_position(mark="110.0")], algos=[stop(501, 95.0)])
        state = self.cycle(fake, calc=dict(structural(105.0), profit_lock=lock), dry_run=True)
        actions = [a for a in state["actions"] if a["type"] == "trail_stop"]
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0]["detail"]["planned"])
        self.assertEqual(actions[0]["detail"]["profit_lock"], lock)
        self.assertEqual(fake.writes(), [])


if __name__ == "__main__":
    unittest.main()
