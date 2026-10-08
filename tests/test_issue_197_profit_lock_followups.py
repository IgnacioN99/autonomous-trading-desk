#!/usr/bin/env python3
"""
test_issue_197_profit_lock_followups.py - Profit-lock follow-ups of PR #196 (issue #197).

1. Steps above True Net BE need an audit R: with the "current_stop" fallback only the BE step applies.
2. Profile validator: every step needs mfe_r - lock_r >= 0.5.
3. Intrabar window (dem._intrabar_start_ms, mirrored by exit_policy_sim): from the first bar after entry when the fill
   candle is the previous 15m candle, else the forming candle's open; limit 31.
4. A failing start-time computation yields "intrabar_unavailable" plus intrabar_error "<Type>: <msg>".
5. tp1_price is MFE proof only when the record's tp1_order_id is among the userTrades fills already read.
6. / 7. SHORT stops rounded up to the tick (dem._round_stop); profit_lock.binding compares tick-rounded values.
8. Guardian: exit_management read once per cycle; an invalid-profile warning is listed once.
Hermetic: FakeExchange / UserTradesExchange, synthetic klines, fetch_klines_range patched, temp workspaces.
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
import exit_policy_sim as eps
import user_profile as up
from utils import trade_excursion as tx
from test_exit_management import FakeExchange, offline, long_position, stop, write_audit
from test_issue_95_trailing_activation import make_klines, market, user_trades_exchange
from test_issue_181_profit_lock import (run_calc, em, ONDO, ONDO_FILTERS, ONDO_ENTRY, ONDO_SL, ONDO_TP1, ONDO_MARK,
                                        ONDO_R, ondo_klines, ondo_fills, OneMinuteBars)
from test_guardian_excursion import GuardianExcursionBase

MIN = 60_000
M15 = 15 * MIN
USER_TRADES = "/fapi/v1/userTrades"


class TestCurrentStopReference(unittest.TestCase):

    def test_current_stop_r_gets_the_be_step_only(self):
        for direction, sl, be in (("LONG", 99.0, 100.2), ("SHORT", 101.0, 99.8)):
            for x in (2.4, 4.3):
                with self.subTest(direction=direction, x=x):
                    calc, _ = run_calc(direction, x, planned_sl=None, current_sl=sl)  # R from the current stop
                    self.assertEqual(calc["reference_source"], "current_stop")
                    self.assertEqual(calc["initial_risk"], 1.0)
                    lock = calc["profit_lock"]
                    self.assertEqual((lock["lock_r"], lock["step_mfe_r"]), (0.0, 1.0))
                    self.assertAlmostEqual(lock["lock_price"], be, places=9)

    def test_audit_reference_still_gets_every_step(self):
        calc, _ = run_calc("LONG", 2.4, reference_source="trade_audit")
        self.assertEqual(calc["profit_lock"]["lock_r"], 1.0)
        calc, _ = run_calc("SHORT", 4.3, reference_source="trade_audit")
        self.assertEqual(calc["profit_lock"]["lock_r"], 3.0)


class TestStepGap(unittest.TestCase):

    def test_gap_below_half_r_is_invalid(self):
        for steps in ([{"mfe_r": 2.0, "lock_r": 1.99}], [{"mfe_r": 1.0, "lock_r": 0.6}],
                      [{"mfe_r": 1.0, "lock_r": 0.0}, {"mfe_r": 2.0, "lock_r": 1.75}]):
            with self.subTest(steps=steps):
                out = up.get_exit_management({"exit_management": {"profit_lock_steps": steps}})
                self.assertEqual(out["profit_lock_steps"], up.DEFAULT_EXIT_MANAGEMENT["profit_lock_steps"])
                self.assertEqual(len(out["warnings"]), 1)
                self.assertIn("mfe_r - lock_r >= 0.5", out["warnings"][0])

    def test_gap_of_half_r_is_valid(self):
        steps = [{"mfe_r": 1.0, "lock_r": 0.5}, {"mfe_r": 2.0, "lock_r": 1.5}]
        out = up.get_exit_management({"exit_management": {"profit_lock_steps": steps}})
        self.assertEqual(out["warnings"], [])
        self.assertEqual(out["profit_lock_steps"], steps)

    def test_defaults_and_sim_policies_are_valid(self):
        self.assertIsNone(up._profit_lock_steps_error(up.DEFAULT_EXIT_MANAGEMENT["profit_lock_steps"]))
        for name, policy in eps.build_policies().items():
            steps = (policy.get("lock") or {}).get("profit_lock_steps")
            if steps:
                self.assertIsNone(up._profit_lock_steps_error(steps), name)


class TestIntrabarWindow(unittest.TestCase):
    FORMING = 1_800_000_000_000 // M15 * M15

    def test_rule(self):
        now = self.FORMING + 7 * MIN + 30_000
        cases = (
            (self.FORMING + 2 * MIN + 10_000, self.FORMING + 3 * MIN),  # fill in the forming candle
            (self.FORMING - 15 * MIN + 10_000, self.FORMING - 14 * MIN),  # fill candle = the previous candle
            (self.FORMING - MIN + 5_000, self.FORMING),  # last minute of the previous candle
            (self.FORMING - 16 * MIN, self.FORMING - 15 * MIN),  # last minute two candles back: nothing uncounted lost
            (self.FORMING - 17 * MIN, self.FORMING),  # older fill candle: forming candle only (under-reads, safe)
            (self.FORMING - 3 * M15, self.FORMING),
        )
        for entry_ms, start in cases:
            with self.subTest(entry_ms=entry_ms):
                self.assertEqual(dem._intrabar_start_ms(entry_ms / 1000.0, now), start)
        self.assertEqual(dem.INTRABAR_KLINES_LIMIT, 31)
        # Worst case (fill in the first minute of the previous candle, now at the end of the forming one): 14 + 15
        # bars, inside one limit-31 read.
        start = dem._intrabar_start_ms((self.FORMING - 15 * MIN + 1_000) / 1000.0, self.FORMING + 15 * MIN - 1)
        self.assertLessEqual((self.FORMING + 15 * MIN - start) // MIN, dem.INTRABAR_KLINES_LIMIT)

    def test_bad_entry_ts_raises_inside_the_rule(self):
        with self.assertRaises((TypeError, ValueError)):
            dem._intrabar_start_ms(float("nan"), self.FORMING)


def ondo_prev_candle_klines():
    """ONDO pre-entry bars only: the entry falls in the 15m candle right before the forming one (the fill candle)."""
    c = ONDO_ENTRY
    offs = [0.0, 0.0003, 0.0006, 0.0003]
    pre = [(c + offs[i % 4] + 0.002, c + offs[i % 4] - 0.002, c + offs[i % 4]) for i in range(40)]
    return make_klines(pre, [], (0.4750, 0.4640, 0.4652), time.time())


class OndoUpdateBase(unittest.TestCase):

    def run_update(self, *, klines_and_ts=None, minute_bars=None, mark=ONDO_MARK, **record):
        klines, entry_ts = klines_and_ts or ondo_klines()
        pos = long_position(symbol=ONDO, amt="-7", entry=str(ONDO_ENTRY), mark=str(mark))
        fake = user_trades_exchange([pos], ondo_fills(entry_ts), algos=[stop(601, ONDO_SL, side="BUY", symbol=ONDO)])
        minute_bars = minute_bars or OneMinuteBars()
        rec = dict(symbol=ONDO, direction="SHORT", entry_price=ONDO_ENTRY, sl_price=ONDO_SL, tp1_price=ONDO_TP1,
                   total_qty=10, tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=entry_ts)
        rec.update(record)
        with offline(fake) as ws, market(klines), \
                patch("execute_futures_trade.get_symbol_filters", return_value=dict(ONDO_FILTERS)), \
                patch("utils.trade_excursion.fetch_klines_range", side_effect=minute_bars):
            write_audit(ws, **rec)
            res = dem.update_position_to_structural_stop(ONDO, target_env="testnet", dry_run=True)
        return res, fake, minute_bars, entry_ts


class TestIntrabarUpdatePath(OndoUpdateBase):

    def test_fill_candle_before_the_forming_one_is_read(self):
        res, _, bars, entry_ts = self.run_update(klines_and_ts=ondo_prev_candle_klines())
        self.assertEqual(len(bars.calls), 1)
        call = bars.calls[0]
        self.assertEqual(call["start_ms"], tx.first_post_entry_bar_ms(entry_ts))
        self.assertEqual(call["limit"], 31)
        self.assertEqual(res["profit_lock"]["mfe_source"], "intrabar_1m")
        self.assertEqual(res["profit_lock"]["lock_r"], 2.0)

    def test_older_fill_candle_reads_the_forming_candle(self):
        before = int(time.time() * 1000)
        _, _, bars, entry_ts = self.run_update()  # ONDO: entry five closed candles before the forming one
        start = bars.calls[0]["start_ms"]
        self.assertEqual(start % M15, 0)
        self.assertGreaterEqual(start, before // M15 * M15)
        self.assertGreater(start, tx.first_post_entry_bar_ms(entry_ts))

    def test_start_time_error_is_intrabar_unavailable_with_type(self):
        with patch("utils.trade_excursion.first_post_entry_bar_ms", side_effect=ValueError("bad entry_ts")):
            res, _, bars, _ = self.run_update()
        self.assertEqual(bars.calls, [])
        self.assertIn("intrabar_unavailable", res["warnings"])
        self.assertEqual(res["intrabar_error"], "ValueError: bad entry_ts")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["profit_lock"]["mfe_source"], "mark")  # the trail and the lock still run

    def test_fetch_error_carries_its_type(self):
        res, _, _, _ = self.run_update(minute_bars=OneMinuteBars(error=TimeoutError("timed out")))
        self.assertIn("intrabar_unavailable", res["warnings"])
        self.assertEqual(res["intrabar_error"], "TimeoutError: timed out")


class TestTp1OrderIdProof(OndoUpdateBase):
    # tp1_price 0.4600 = +2.89R (beats the closed-15m +1.9R and the mark +1.33R); the 1m read fails.
    DEEP_TP1 = 0.4600
    MARK = 0.4790

    def _run(self, **record):
        return self.run_update(minute_bars=OneMinuteBars(error=OSError("down")), mark=self.MARK,
                               tp1_price=self.DEEP_TP1, **record)

    def test_tp1_price_counts_with_its_order_id_among_the_fills(self):
        res, fake, _, _ = self._run(tp1_order_id=2)  # ondo_fills: the TP1 BUY fill has orderId 2
        lock = res["profit_lock"]
        self.assertEqual(lock["mfe_source"], "tp1_price")
        self.assertAlmostEqual(lock["mfe_r"], (ONDO_ENTRY - self.DEEP_TP1) / ONDO_R, places=3)
        self.assertEqual(lock["lock_r"], 1.0)
        self.assertEqual(len([c for c in fake.calls if c[1] == USER_TRADES]), 1)  # no extra request

    def test_tp1_price_ignored_without_a_matching_fill(self):
        for record in ({"tp1_order_id": 999}, {}):
            with self.subTest(record=record):
                res, fake, _, _ = self._run(**record)
                lock = res["profit_lock"]
                self.assertEqual(lock["mfe_source"], "closed_15m")
                self.assertEqual(lock["lock_r"], 0.0)
                self.assertEqual(len([c for c in fake.calls if c[1] == USER_TRADES]), 1)

    def test_missing_tp1_fill_is_reported(self):
        res, _, _, _ = self._run(tp1_order_id=999)
        self.assertIn("tp1_fill_not_in_user_trades", res.get("warnings") or [])
        res, _, _, _ = self._run(tp1_order_id=2)
        self.assertNotIn("tp1_fill_not_in_user_trades", res.get("warnings") or [])

    def test_string_order_ids_match(self):
        res, _, _, _ = self._run(tp1_order_id="2")
        self.assertEqual(res["profit_lock"]["mfe_source"], "tp1_price")


class TestRounding(unittest.TestCase):

    def test_round_stop_at_tick_boundaries(self):
        cases = (
            (99.81, "SHORT", 0.1, 1, 99.9), (99.8, "SHORT", 0.1, 1, 99.8),
            (99.80000000000001, "SHORT", 0.1, 1, 99.8),  # float noise never costs a tick
            (99.89, "LONG", 0.1, 1, 99.8), (99.8, "LONG", 0.1, 1, 99.8), (100.19999999999999, "LONG", 0.1, 1, 100.2),
            (0.47081, "SHORT", 0.0001, 4, 0.4709), (0.47089, "LONG", 0.0001, 4, 0.4708),
            (101.4, "LONG", 1.0, 0, 101.0), (101.4, "SHORT", 1.0, 0, 102.0),
        )
        for price, side, tick, prec, expected in cases:
            with self.subTest(price=price, side=side):
                self.assertEqual(dem._round_stop(price, side, tick, prec), expected)

    def test_short_one_tick_cap_and_stop_rounded_up(self):
        from test_issue_106_exit_manager_hardening import TestDeadZoneCap, SHORT_POST, SHORT_FORMING
        calc, klines, _ = TestDeadZoneCap._calc(None, "SHORT", SHORT_POST, SHORT_FORMING, 101.0, 101.0, 99.0)
        self.assertEqual(calc["activation_reason"], "r_multiple")
        self.assertEqual(calc["new_structural_sl"], 100.1)  # one tick above entry, never inside the dead zone

    def test_short_one_tick_cap_with_off_grid_entry(self):
        # Averaged fills: entry 100.05 is off the 0.1 grid. Pre-BE r_multiple (R = 1, MFE 1.25R < 2x ATR): the cap is
        # entry + tick rounded UP (100.2), still strictly above entry (round_price gave 100.1).
        from decimal import Decimal
        from test_issue_106_exit_manager_hardening import SHORT_POST, SHORT_FORMING, TICK_FILTERS
        from test_issue_95_trailing_activation import flat_pre, closed_atr
        entry = 100.05
        klines, entry_ts = make_klines(flat_pre(), SHORT_POST, SHORT_FORMING, time.time())
        with market(klines):
            calc = dem.calculate_structural_stop("BTCUSDT", "SHORT", entry, current_sl_price=101.05,
                                                 target_env="testnet", planned_sl=101.05, entry_ts=entry_ts,
                                                 tp1_filled=False, mark_price=99.0, exit_management=em(),
                                                 filters=dict(TICK_FILTERS))
        self.assertEqual(calc["activation_reason"], "r_multiple")
        self.assertLess(calc["mfe"], 2.0 * closed_atr(klines))
        cap = dem._round_stop(Decimal(str(entry)) + Decimal("0.1"), "SHORT", 0.1, 1)
        self.assertEqual(cap, 100.2)
        self.assertEqual(calc["new_structural_sl"], cap)
        self.assertGreater(calc["new_structural_sl"], entry)
        self.assertTrue(calc["should_update"])

    def test_binding_compares_tick_rounded_values(self):
        # +3.5R with a 3R -> +1R step: the chandelier sits just above the +1R lock (101.0) inside one 1.0 tick, so on
        # the tick the stop is the lock price (binding); with a 0.1 tick the trail is tighter (not binding).
        settings = em(lock_on_tp1=False, extend_last_step=False,
                      profit_lock_steps=[{"mfe_r": 1.0, "lock_r": 0.0}, {"mfe_r": 3.0, "lock_r": 1.0}])
        coarse = {"tickSize": 1.0, "precision_price": 0}
        calc, _ = run_calc("LONG", 3.5, mark=110.0, exit_management=settings, filters=coarse)
        chandelier = calc["chandelier_anchor"] - 1.8 * calc["atr_15m"]
        self.assertGreater(chandelier, 101.0)
        self.assertLess(chandelier, 102.0)
        self.assertEqual(calc["profit_lock"]["lock_r"], 1.0)
        self.assertEqual(calc["new_structural_sl"], 101.0)
        self.assertTrue(calc["profit_lock"]["binding"])
        fine = {"tickSize": 0.1, "precision_price": 1}
        calc, _ = run_calc("LONG", 3.5, mark=110.0, exit_management=settings, filters=fine)
        self.assertGreater(calc["new_structural_sl"], 101.0)
        self.assertFalse(calc["profit_lock"]["binding"])


class TestGuardianReadsProfileOnce(GuardianExcursionBase):

    def _cycle_with_profile(self, profile):
        real = up.get_exit_management
        fake = FakeExchange([long_position("BTCUSDT", mark="101.0"), long_position("ETHUSDT", mark="101.0")],
                            algos=[stop(501, 95.0, symbol="BTCUSDT"), stop(502, 95.0, symbol="ETHUSDT")])
        with patch("user_profile.get_exit_management", side_effect=lambda *a, **k: real(profile)) as get:
            state = self.cycle(fake)
        return state, get

    def test_one_read_per_cycle(self):
        state, get = self._cycle_with_profile({})
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(state["positions"]), 2)
        self.assertEqual(state["exit_management_warnings"], [])

    def test_invalid_profile_warning_listed_once(self):
        bad = {"exit_management": {"trail_activation": "fast"}}
        first, _ = self._cycle_with_profile(bad)
        listed = [w for w in first["trail_warnings"] if w["warning"].startswith("exit_management.trail_activation")]
        self.assertEqual(listed, [{"symbol": None, "warning": first["exit_management_warnings"][0]}])
        for view in first["positions"]:  # never repeated per position
            self.assertFalse(any(str(w).startswith("exit_management") for w in view["trailing"].get("warnings", [])))
        for _ in range(2):  # later cycles: still recorded, not listed again (no alternation)
            later, _ = self._cycle_with_profile(bad)
            self.assertEqual(later["exit_management_warnings"], first["exit_management_warnings"])
            self.assertEqual([w for w in later["trail_warnings"] if str(w["warning"]).startswith("exit_management")],
                             [])

    def test_warnings_carried_when_no_trailing_ran(self):
        self.write_state(exit_management_warnings=["exit_management.trail_activation invalid: x"])
        with patch("user_profile.get_exit_management") as get:
            state = self.cycle(FakeExchange([], algos=[]))
        get.assert_not_called()
        self.assertEqual(state["exit_management_warnings"], ["exit_management.trail_activation invalid: x"])


class TestTrueNetBeOnCoarseTick(unittest.TestCase):
    """PR #211 review: tick 0.1 on a 66.67 entry (0.15% of price). Rounding the BE floor away from price would leave
    it up to one tick inside the 0.2% fee buffer; it is rounded toward price instead."""
    ENTRY = 66.67
    FILTERS = {"tickSize": 0.1, "precision_price": 1}

    def _calc(self, direction, *, lock):
        from test_issue_95_trailing_activation import flat_pre
        e = self.ENTRY
        if direction == "LONG":  # MFE 0.33 = 1.1R (R 0.3): BE step reached; trail well below BE; mark far above
            post, forming, sl, mark = [(e + 0.33, e - 0.2, e + 0.1)], (e + 0.2, e - 0.1, e + 0.1), e - 0.3, e + 4.0
        else:
            post, forming, sl, mark = [(e + 0.2, e - 0.33, e - 0.1)], (e + 0.1, e - 0.2, e - 0.1), e + 0.3, e - 4.0
        klines, entry_ts = make_klines(flat_pre(center=e), post, forming, time.time())
        # lock_on_tp1 off: the far mark (price-floor room) must not raise the lock MFE beyond the BE step.
        settings = em(lock_on_tp1=False) if lock else em(profit_lock_enabled=False)
        with market(klines):
            return dem.calculate_structural_stop("XUSDT", direction, e, current_sl_price=sl, target_env="testnet",
                                                 planned_sl=sl, entry_ts=entry_ts, tp1_filled=True, mark_price=mark,
                                                 reference_source="trade_audit", exit_management=settings,
                                                 filters=dict(self.FILTERS))

    def _on_grid(self, price):
        return abs(price / 0.1 - round(price / 0.1)) < 1e-6

    def test_long_be_at_least_the_buffer(self):
        for lock in (False, True):
            with self.subTest(lock=lock):
                calc = self._calc("LONG", lock=lock)
                sl = calc["new_structural_sl"]
                self.assertEqual(sl, 66.9)  # entry x 1.002 = 66.803 rounded UP (66.8 would be a 0.195% buffer)
                self.assertGreaterEqual(sl, self.ENTRY * 1.002)
                self.assertTrue(self._on_grid(sl))
                if lock:
                    self.assertEqual(calc["profit_lock"]["lock_r"], 0.0)
                    self.assertEqual(calc["profit_lock"]["lock_price"], 66.9)
                    self.assertTrue(calc["profit_lock"]["binding"])

    def test_short_be_at_least_the_buffer(self):
        for lock in (False, True):
            with self.subTest(lock=lock):
                calc = self._calc("SHORT", lock=lock)
                sl = calc["new_structural_sl"]
                self.assertEqual(sl, 66.5)  # entry x 0.998 = 66.537 rounded DOWN (66.6 would be inside the buffer)
                self.assertLessEqual(sl, self.ENTRY * 0.998)
                self.assertTrue(self._on_grid(sl))
                if lock:
                    self.assertEqual(calc["profit_lock"]["lock_price"], 66.5)

    def test_true_net_be_helper(self):
        self.assertEqual(dem._true_net_be(100.0, True, 0.1, 1), 100.2)  # on-grid: unchanged (no float-noise tick)
        self.assertEqual(dem._true_net_be(100.0, False, 0.1, 1), 99.8)
        self.assertEqual(dem._true_net_be(66.67, True, 0.1, 1), 66.9)
        self.assertEqual(dem._true_net_be(66.67, False, 0.1, 1), 66.5)
        for v in (100.2, 66.9):  # the final away-from-price rounding leaves an on-grid BE unchanged
            self.assertEqual(dem._round_stop(v, "LONG", 0.1, 1), v)
        for v in (99.8, 66.5):
            self.assertEqual(dem._round_stop(v, "SHORT", 0.1, 1), v)

    def test_long_one_tick_cap_uses_round_stop(self):
        from decimal import Decimal
        self.assertEqual(dem._round_stop(Decimal("100.05") - Decimal("0.1"), "LONG", 0.1, 1), 99.9)


class TestGuardianProfileReadFailure(GuardianExcursionBase):

    def _two(self):
        return FakeExchange([long_position("BTCUSDT", mark="101.0"), long_position("ETHUSDT", mark="101.0")],
                            algos=[stop(501, 95.0, symbol="BTCUSDT"), stop(502, 95.0, symbol="ETHUSDT")])

    def test_failure_cached_for_the_cycle_and_dem_loads_itself(self):
        real = up.get_exit_management
        calls = []

        def flaky(*a, **k):
            calls.append(a)
            if len(calls) == 1:
                raise RuntimeError("profile boom")
            return real({})

        seen = []
        real_trail = dem.update_position_to_structural_stop

        def trail(symbol, *a, **kw):
            seen.append(kw.get("exit_management"))
            return real_trail(symbol, *a, **kw)

        with patch("user_profile.get_exit_management", side_effect=flaky), \
                patch("dynamic_exit_manager.update_position_to_structural_stop", side_effect=trail):
            state = self.cycle(self._two())
        errors = [e for e in state["errors"] if e["stage"] == "exit_management"]
        self.assertEqual(len(errors), 1)
        self.assertIn("RuntimeError: profile boom", errors[0]["error"])
        self.assertEqual(seen, [None, None])  # dem falls back to its own load
        self.assertEqual(len(calls), 3)  # one failed cycle read + one dem load per position
        self.assertEqual([e for e in state["errors"] if e["stage"] == "trailing"], [])

    def test_each_dem_call_gets_its_own_copy(self):
        seen = []
        real_trail = dem.update_position_to_structural_stop

        def trail(symbol, *a, **kw):
            em_ = kw["exit_management"]
            seen.append([dict(s) for s in em_["profit_lock_steps"]])
            em_["profit_lock_steps"].clear()  # a misbehaving callee must not affect the next position
            em_["profit_lock_enabled"] = False
            return real_trail(symbol, *a, **kw)

        with patch("dynamic_exit_manager.update_position_to_structural_stop", side_effect=trail):
            self.cycle(self._two())
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], up.DEFAULT_EXIT_MANAGEMENT["profit_lock_steps"])
        self.assertEqual(seen[1], up.DEFAULT_EXIT_MANAGEMENT["profit_lock_steps"])


class TestSimMirrorsTheLiveWindow(unittest.TestCase):

    def test_sim_uses_dem_rule(self):
        import inspect
        self.assertIn("dem._intrabar_start_ms(", inspect.getsource(eps.replay))
        self.assertIn("legacy_r_or_atr", eps.POLICY_NAMES)
        self.assertEqual(eps.build_policies()["legacy_r_or_atr"]["lock"]["trail_activation"], "r_or_atr")
        self.assertEqual(eps.build_policies()["current"]["lock"]["trail_activation"], "r_only")


if __name__ == "__main__":
    unittest.main()
