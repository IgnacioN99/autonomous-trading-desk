#!/usr/bin/env python3
"""
test_issue_95_trailing_activation.py - Offline tests for the structural trailing activation gate (Issue #95).

A fresh fill must keep its planned Stop Loss: the trail activates only after +1.0R of planned risk (issue #205:
+2.0x ATR_15m alone only with the legacy "r_or_atr" mode or without an R reference) of favourable excursion since
entry on CLOSED 15m candles, or after TP1 fills. Once active the Chandelier stop is
anchored to the extreme since entry. YOLO positions are never trailed before TP1, whatever the entry path.
No network, no orders: the exchange is FakeExchange and klines are synthetic.
"""

import io
import os
import sys
import time
import tempfile
import unittest
import contextlib
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "loops"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import dynamic_exit_manager as dem
import position_guardian_loop as pgl
from test_exit_management import FakeExchange, offline, long_position, stop, write_audit, structural, ALGO_ENDPOINT

BAR_MS = 15 * 60 * 1000
ENTRY = 100.0


def make_klines(pre, post, forming, now_s):
    """pre/post: lists of (high, low, close) for closed bars before / after entry; forming: (high, low, close).
    Returns (klines, entry_ts): entry_ts falls inside the last `pre` bar (the fill candle, excluded from MFE)."""
    rows = pre + post + [forming]
    n = len(rows)
    last_open = int(now_s * 1000) - 60_000  # forming candle opened 1 minute ago
    klines = []
    for i, (h, l, c) in enumerate(rows):
        o_ms = last_open - (n - 1 - i) * BAR_MS
        klines.append([o_ms, str(c), str(h), str(l), str(c), "1000", o_ms + BAR_MS - 1])
    fill_bar_open = klines[len(pre) - 1][0]
    entry_ts = (fill_bar_open + 5 * 60 * 1000) / 1000.0
    return klines, entry_ts


def flat_pre(count=40, center=ENTRY):
    """Zig-zag around center with range 1.0 per bar (ATR ~1) and periodic swing lows/highs."""
    offs = [0.0, 0.3, 0.6, 0.3]
    return [(center + offs[i % 4] + 0.5, center + offs[i % 4] - 0.5, center + offs[i % 4]) for i in range(count)]


def closed_atr(klines):
    closed = klines[:-1]
    return dem.calculate_atr([float(k[2]) for k in closed], [float(k[3]) for k in closed],
                             [float(k[4]) for k in closed], period=14)


@contextlib.contextmanager
def market(klines):
    with patch("dynamic_exit_manager.get_klines_data", return_value=klines) as gk:
        yield gk


def short_position(symbol="BTCUSDT", amt="-10", entry="100.0", mark="100.0", leverage="3"):
    return long_position(symbol=symbol, amt=amt, entry=entry, mark=mark, leverage=leverage)


def user_trades_exchange(*args, **kw):
    """test_issue_106's UserTradesExchange (imported lazily: that module imports this one)."""
    from test_issue_106_exit_manager_hardening import UserTradesExchange
    return UserTradesExchange(*args, **kw)


def reconciling_fills(open_ts):
    """BUY 10 at open_ts then SELL 3 (TP1) 5s later: reconciles to positionAmt 7 opened at open_ts (verified)."""
    from test_issue_106_exit_manager_hardening import buy_fill
    return [buy_fill(open_ts), dict(buy_fill(open_ts + 5, qty="3"), id=2, side="SELL")]


class TestFreshPositionNotTrailed(unittest.TestCase):

    def test_fresh_long_keeps_planned_sl(self):
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = FakeExchange([long_position(mark="100.5")], algos=[stop(501, 98.0)])
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=98.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=int(now) - 7)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"], res)
        self.assertFalse(res["updated"])
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertIsNone(res["activation_reason"])
        self.assertEqual(res["reference_source"], "trade_audit")
        self.assertEqual(fake.writes(), [])

    def test_fresh_long_calc_would_have_tightened_without_gate(self):
        # Planned SL (98.0) is wider than 1.8x ATR (~1.8) below price: the old logic tightened it immediately.
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        with patch("execute_futures_trade.get_symbol_filters", return_value={"tickSize": 0.1, "precision_price": 1}), \
             market(klines):
            calc = dem.calculate_structural_stop("BTCUSDT", "LONG", 100.0, current_sl_price=98.0, target_env="testnet",
                                                 planned_sl=98.0, entry_ts=now - 7, tp1_filled=False, mark_price=100.5)
        self.assertGreater(100.5 - 1.8 * calc["atr_15m"], 98.0)
        self.assertFalse(calc["should_update"])
        self.assertEqual(calc["reason"], "trail_not_activated")
        self.assertEqual(calc["new_structural_sl"], 98.0)
        self.assertEqual(calc["bars_since_entry"], 0)
        self.assertEqual(calc["initial_risk"], 2.0)

    def test_fresh_short_keeps_planned_sl(self):
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.2, 99.4, 99.6), now)
        fake = FakeExchange([short_position(mark="99.6")], algos=[stop(601, 102.0, side="BUY")])
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="SHORT", entry_price=100.0, sl_price=102.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=int(now) - 7)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertEqual(fake.writes(), [])

    def test_no_entry_time_means_no_bars_since_entry(self):
        now = time.time()
        klines, _ = make_klines(flat_pre(), [(104.0, 103.0, 103.5)] * 5, (104.0, 103.0, 103.5), now)
        # No audit record and no updateTime: entry_ts None -> not activated even though price ran.
        fake = FakeExchange([long_position(mark="103.5")], algos=[stop(501, 98.0)])
        with offline(fake), market(klines):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertEqual(res["reference_source"], "current_stop")
        self.assertEqual(fake.writes(), [])


class TestClosedBarsOnly(unittest.TestCase):

    def test_forming_candle_spike_does_not_activate(self):
        now = time.time()
        # Closed bars since entry stay below +1R (R=2 -> 102) and below 2x ATR; the forming candle spikes to 103.
        post = [(100.9, 99.9, 100.4), (101.0, 100.0, 100.6)]
        klines, entry_ts = make_klines(flat_pre(), post, (103.0, 100.5, 102.8), now)
        fake = FakeExchange([long_position(mark="102.8")], algos=[stop(501, 98.0)])
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=98.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=entry_ts)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertEqual(fake.writes(), [])


class TestActivation(unittest.TestCase):

    def _long_run(self, sl=98.0, tp1=False):
        now = time.time()
        # Run-up to a 103.6 high, then a pullback: last closed close 102.2 (well below the high since entry).
        post = [(101.0, 100.0, 100.8), (102.0, 100.8, 101.8), (103.6, 101.8, 103.2), (103.3, 102.0, 102.2)]
        klines, entry_ts = make_klines(flat_pre(), post, (102.6, 102.1, 102.4), now)
        amt = "7" if tp1 else "10"
        fake = FakeExchange([long_position(amt=amt, mark="102.4")], algos=[stop(501, sl)])
        return fake, klines, entry_ts

    def test_long_r_multiple_activates_and_anchors_on_high_since_entry(self):
        fake, klines, entry_ts = self._long_run(sl=98.0)
        atr = closed_atr(klines)
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=98.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=entry_ts)
            calc = dem.calculate_structural_stop("BTCUSDT", "LONG", 100.0, current_sl_price=98.0, target_env="testnet",
                                                 planned_sl=98.0, entry_ts=entry_ts, tp1_filled=False, mark_price=102.4,
                                                 reference_source="trade_audit")
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(calc["activation_reason"], "r_multiple")
        self.assertEqual(calc["chandelier_anchor"], 103.6)
        self.assertEqual(calc["reference_source"], "trade_audit")
        self.assertEqual(calc["bars_since_entry"], 4)
        self.assertAlmostEqual(calc["mfe"], 3.6)
        # Anchored to the highest high since entry, not to the last closed close
        chandelier = 103.6 - 1.8 * atr
        self.assertGreater(calc["new_structural_sl"], 102.2 - 1.8 * atr + 0.5)  # old close-anchored level
        self.assertGreaterEqual(calc["new_structural_sl"], eft.round_price(chandelier, 0.1, 1) - 1e-9)
        self.assertLessEqual(calc["new_structural_sl"], 102.4 - 0.5 * atr + 0.05)

        self.assertTrue(res["success"], res)
        self.assertTrue(res["updated"])
        self.assertEqual(res["reason"], "tightened")
        self.assertEqual(res["activation_reason"], "r_multiple")
        self.assertEqual(res["reference_source"], "trade_audit")
        self.assertEqual(res["new_sl"], calc["new_structural_sl"])
        posts = fake.write_index("POST", ALGO_ENDPOINT)
        deletes = fake.write_index("DELETE", ALGO_ENDPOINT)
        self.assertEqual(len(posts), 1)
        self.assertLess(posts[0], deletes[0], "place-then-cancel")
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [res["new_sl"]])

    def test_short_r_multiple_activates_and_anchors_on_low_since_entry(self):
        now = time.time()
        post = [(100.0, 99.0, 99.2), (99.2, 98.0, 98.2), (98.2, 96.4, 96.8), (98.0, 96.7, 97.8)]
        klines, entry_ts = make_klines(flat_pre(), post, (97.9, 97.4, 97.6), now)
        atr = closed_atr(klines)
        fake = FakeExchange([short_position(mark="97.6")], algos=[stop(601, 102.0, side="BUY")])
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="SHORT", entry_price=100.0, sl_price=102.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=entry_ts)
            calc = dem.calculate_structural_stop("BTCUSDT", "SHORT", 100.0, current_sl_price=102.0, target_env="testnet",
                                                 planned_sl=102.0, entry_ts=entry_ts, tp1_filled=False, mark_price=97.6)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(calc["activation_reason"], "r_multiple")
        self.assertEqual(calc["chandelier_anchor"], 96.4)
        # Issue #197: SHORT stops are rounded up (away from price) to the tick.
        self.assertLessEqual(calc["new_structural_sl"], dem._round_stop(96.4 + 1.8 * atr, "SHORT", 0.1, 1) + 1e-9)
        self.assertTrue(res["updated"], res)
        self.assertEqual(res["activation_reason"], "r_multiple")
        self.assertLess(res["new_sl"], 102.0)
        posts = fake.write_index("POST", ALGO_ENDPOINT)
        deletes = fake.write_index("DELETE", ALGO_ENDPOINT)
        self.assertLess(posts[0], deletes[0])

    def _wide_stop(self, profile=None):
        # Issue #205: R = 4.8 (SL 95.2), MFE 3.6 = 0.75R while 2x ATR_15m (~2) is already reached.
        fake, klines, entry_ts = self._long_run(sl=95.2)
        atr = closed_atr(klines)
        self.assertGreaterEqual(3.6, 2.0 * atr)
        self.assertLess(2.0 * atr, 4.8)
        with offline(fake, profile=profile) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=95.2, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=entry_ts)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        return res, fake

    def test_atr_expansion_with_large_r(self):
        # Issue #205 (intended change): 2x ATR_15m before +1R no longer activates the trail by default.
        res, fake = self._wide_stop()
        self.assertTrue(res["success"], res)
        self.assertFalse(res["updated"])
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertIsNone(res["activation_reason"])
        self.assertEqual(res["current_sl"], 95.2)
        self.assertIn("needs +1R on closed 15m bars", res["message"])
        self.assertEqual(fake.writes(), [])

    def test_atr_expansion_with_large_r_modes(self):
        res, fake = self._wide_stop(profile={"exit_management": {"trail_activation": "r_and_atr"}})
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertIn("needs +1R and +2x ATR_15m", res["message"])
        self.assertEqual(fake.writes(), [])
        res, fake = self._wide_stop(profile={"exit_management": {"trail_activation": "r_or_atr"}})  # legacy
        self.assertTrue(res["updated"], res)
        self.assertEqual(res["activation_reason"], "atr_expansion")
        self.assertGreater(res["new_sl"], 95.2)

    def test_tp1_filled_activates(self):
        # Issue #163: TP1 counts only for a reference verified against Binance fills (BUY 10 then SELL 3 -> amt 7).
        now = time.time()
        klines, entry_ts = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = user_trades_exchange([long_position(amt="7", mark="100.5")], reconciling_fills(int(now) - 7),
                                    algos=[stop(501, 90.0)])
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=90.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=int(now) - 7)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["activation_reason"], "tp1_filled")
        self.assertTrue(res["updated"], res)
        self.assertGreater(res["new_sl"], 90.0)

    def test_never_loosens_after_activation(self):
        fake, klines, entry_ts = self._long_run(sl=98.0)
        fake.algos = [stop(501, 102.0)]  # already tighter than any structural level (mark 102.4)
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=98.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=entry_ts)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["activation_reason"], "r_multiple")
        self.assertFalse(res["updated"])
        self.assertIn(res["reason"], ("not_tighter", "stop_would_trigger_immediately"))
        self.assertEqual(fake.writes(), [])
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [102.0])


class TestReferenceResolution(unittest.TestCase):

    def _resolve(self, **record):
        fake = FakeExchange([])
        pos = dict(long_position(), updateTime=str(int(time.time() * 1000) - 60_000))
        with offline(fake) as ws:
            write_audit(ws, **record)
            return dem.resolve_trade_reference("BTCUSDT", pos, "LONG", 95.0, "testnet")

    def test_matching_record_used(self):
        sl, ts, src = self._resolve(symbol="BTCUSDT", direction="LONG", entry_price=100.2, sl_price=97.0,
                                    total_qty=10, target_env="testnet", timestamp=1234)
        self.assertEqual((sl, ts, src), (97.0, 1234.0, "trade_audit"))

    def test_other_direction_falls_back_to_current_stop(self):
        sl, ts, src = self._resolve(symbol="BTCUSDT", direction="SHORT", entry_price=100.0, sl_price=103.0,
                                    total_qty=10, target_env="testnet", timestamp=1234)
        self.assertEqual(src, "current_stop")
        self.assertEqual(sl, 95.0)
        self.assertIsNotNone(ts)

    def test_far_entry_falls_back_to_current_stop(self):
        _, _, src = self._resolve(symbol="BTCUSDT", direction="LONG", entry_price=101.0, sl_price=97.0,
                                  total_qty=10, target_env="testnet", timestamp=1234)
        self.assertEqual(src, "current_stop")

    def test_other_env_falls_back_to_current_stop(self):
        _, _, src = self._resolve(symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=97.0,
                                  total_qty=10, target_env="prod", timestamp=1234)
        self.assertEqual(src, "current_stop")

    def test_mismatch_reported_through_update(self):
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = FakeExchange([long_position(mark="100.5")], algos=[stop(501, 98.0)])
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="SHORT", entry_price=100.0, sl_price=103.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=int(now) - 7)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["reference_source"], "current_stop")
        self.assertEqual(fake.writes(), [])


class TestYoloGate(unittest.TestCase):

    def test_yolo_leverage_before_tp1_not_trailed(self):
        fake = FakeExchange([long_position("PEPEUSDT", leverage="15")], algos=[stop(501, 95.0, symbol="PEPEUSDT")])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)) as calc:
            res = dem.update_position_to_structural_stop("PEPEUSDT", target_env="testnet")
        self.assertEqual(res["reason"], "yolo_before_tp1")
        self.assertTrue(res["success"])
        calc.assert_not_called()
        self.assertEqual(fake.writes(), [])

    def test_yolo_audit_flag_via_audit_and_trail_all(self):
        fake = FakeExchange([long_position("PEPEUSDT", leverage="3")], algos=[stop(501, 95.0, symbol="PEPEUSDT")])
        with offline(fake) as ws, \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            write_audit(ws, symbol="PEPEUSDT", direction="LONG", entry_price=100.0, sl_price=95.0, total_qty=10,
                        tp1_qty=3, is_yolo=True, target_env="testnet", timestamp=int(time.time()))
            out = dem.audit_and_trail_all_positions(target_env="testnet")
        self.assertEqual(out["results"][0]["reason"], "yolo_before_tp1")
        self.assertEqual(fake.writes(), [])

    def test_yolo_after_tp1_trails(self):
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = user_trades_exchange([long_position("PEPEUSDT", amt="7", mark="100.5", leverage="15")],
                                    reconciling_fills(int(now) - 600), algos=[stop(501, 90.0, symbol="PEPEUSDT")])
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="PEPEUSDT", direction="LONG", entry_price=100.0, sl_price=90.0, total_qty=10,
                        tp1_qty=3, is_yolo=True, target_env="testnet", timestamp=int(now) - 600)
            out = dem.audit_and_trail_all_positions(target_env="testnet")
        res = out["results"][0]
        self.assertTrue(res["updated"], res)
        self.assertEqual(res["activation_reason"], "tp1_filled")
        posts = fake.write_index("POST", ALGO_ENDPOINT)
        deletes = fake.write_index("DELETE", ALGO_ENDPOINT)
        self.assertLess(posts[0], deletes[0])


class TestReviewRound2(unittest.TestCase):

    def test_stale_audit_record_tp1_state_ignored(self):
        # Stale SHORT record (10 qty, TP1 3) + a new LONG of 5: detect_tp1_filled alone would say True.
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = FakeExchange([long_position(amt="5", mark="100.5")], algos=[stop(501, 98.0)])
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="SHORT", entry_price=100.0, sl_price=103.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=int(now) - 3600)
            self.assertTrue(eft.detect_tp1_filled("BTCUSDT", 5)[0])
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertEqual(res["reference_source"], "current_stop")
        self.assertIsNone(res["activation_reason"])
        self.assertEqual(fake.writes(), [])

    def test_stale_audit_record_does_not_open_yolo_gate(self):
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = FakeExchange([long_position(amt="5", mark="100.5", leverage="15")], algos=[stop(501, 98.0)])
        with offline(fake) as ws, market(klines):
            write_audit(ws, symbol="BTCUSDT", direction="SHORT", entry_price=100.0, sl_price=103.0, total_qty=10,
                        tp1_qty=3, is_yolo=False, target_env="testnet", timestamp=int(now) - 3600)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["reason"], "yolo_before_tp1")
        self.assertEqual(fake.writes(), [])

    def test_current_stop_at_breakeven_gives_no_r_multiple(self):
        now = time.time()
        # Closed bars since entry reach +1.0 (MFE): >= 1R if R were |100 - 100.2|, but < 2x ATR.
        post = [(100.9, 99.9, 100.4), (101.0, 100.0, 100.6)]
        klines, entry_ts = make_klines(flat_pre(), post, (100.9, 100.4, 100.7), now)
        pos = dict(long_position(mark="100.7"), updateTime=str(int(entry_ts * 1000)))
        fake = FakeExchange([pos], algos=[stop(501, 100.2)])
        with offline(fake), market(klines):
            calc = dem.calculate_structural_stop("BTCUSDT", "LONG", 100.0, current_sl_price=100.2, target_env="testnet",
                                                 entry_ts=entry_ts, mark_price=100.7)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertIsNone(calc["initial_risk"])
        self.assertGreater(calc["bars_since_entry"], 0)
        self.assertEqual(calc["reason"], "trail_not_activated")
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertEqual(res["reference_source"], "current_stop")
        self.assertEqual(fake.writes(), [])

    def test_short_stop_in_profit_gives_no_r(self):
        now = time.time()
        klines, entry_ts = make_klines(flat_pre(), [(100.0, 99.0, 99.2)], (99.5, 99.0, 99.2), now)
        with patch("execute_futures_trade.get_symbol_filters", return_value={"tickSize": 0.1, "precision_price": 1}), \
             market(klines):
            calc = dem.calculate_structural_stop("BTCUSDT", "SHORT", 100.0, current_sl_price=99.8, target_env="testnet",
                                                 entry_ts=entry_ts, mark_price=99.2)
        self.assertIsNone(calc["initial_risk"])
        self.assertEqual(calc["reason"], "trail_not_activated")

    def test_zero_atr_never_activates_even_after_tp1(self):
        now = time.time()
        flat = [(100.0, 100.0, 100.0)] * 40
        klines, entry_ts = make_klines(flat, [(100.0, 100.0, 100.0)] * 3, (100.0, 100.0, 100.0), now)
        with patch("execute_futures_trade.get_symbol_filters", return_value={"tickSize": 0.1, "precision_price": 1}), \
             market(klines):
            calc = dem.calculate_structural_stop("BTCUSDT", "LONG", 99.0, current_sl_price=97.0, target_env="testnet",
                                                 planned_sl=97.0, entry_ts=entry_ts, tp1_filled=True, mark_price=100.0)
        self.assertEqual(calc["atr_15m"], 0)
        self.assertIsNone(calc["activation_reason"])
        self.assertEqual(calc["reason"], "trail_not_activated")
        self.assertFalse(calc["should_update"])
        self.assertEqual(calc["new_structural_sl"], 97.0)


class TestGuardianRecordsActivation(unittest.TestCase):

    def test_trail_stop_action_carries_activation_reason(self):
        import json
        log_dir = tempfile.mkdtemp()
        fake = FakeExchange([long_position("BTCUSDT")], algos=[stop(501, 95.0)])
        calc = dict(structural(102.0), activation_reason="r_multiple", reason="activated")
        healthy = {"status": "HEALTHY_MOMENTUM", "range_pct": 1.2, "recommendation": "HOLD", "message": "ok"}
        with offline(fake), patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=calc), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=healthy), \
             contextlib.redirect_stdout(io.StringIO()):
            code = pgl.main(["--once", "--env", "testnet"])
        self.assertEqual(code, 0)
        with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            state = json.load(f)
        trail = [a for a in state["actions"] if a["type"] == "trail_stop"]
        self.assertEqual(len(trail), 1)
        self.assertTrue(trail[0]["success"])
        self.assertEqual(trail[0]["detail"]["activation_reason"], "r_multiple")
        self.assertIn("reference_source", trail[0]["detail"])
        self.assertEqual(state["positions"][0]["trailing"]["activation_reason"], "r_multiple")


if __name__ == "__main__":
    unittest.main()
