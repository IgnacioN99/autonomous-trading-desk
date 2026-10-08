#!/usr/bin/env python3
"""
test_issue_210_191_208_outcomes.py - follow-ups of the trade reconstruction (issues #210, #191, #208).

#210: entry fills matched by side + qty + time window when the audit entry_order_id is the algoId of a STOP_MARKET
      entry (entry_match "side_qty_window"); "no_entry_fill" rows (no legs, never consume a later trade's fills: the
      ENAUSDT case of #191); R = move from entry_vwap over the sized risk; simulator: KLINES_LIMIT 1000 pages with a pause, 429 / 418 backoff,
      skipped.malformed, no_entry_fill skip, entry_ts_approx from entry_match, ranking / auth_mode notes; the live
      trail caller never injects klines_15m / filters.
#191: nearest-level leg labels (SL / BE overlap, BE slippage, tick-size tolerance), no post-exit print in the MFE,
      userTrades split cap with "truncated", --output restricted to logs/.
#208: closed-today summary per trade (TP1 partial + runner = one trade, scratch, partial_history) and the sync /
      error-state keys.
Hermetic: send_signed_request and klines are fakes, urlopen is blocked, temp workspaces.
"""

import io
import os
import sys
import json
import shutil
import time
import tempfile
import unittest
import contextlib
import urllib.error
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import trade_outcomes as to
import exit_policy_sim as eps
import sync_session_state as sss
import dynamic_exit_manager as dem
from test_trade_outcomes import OutcomesBase, FakeFills, KlinesFake, fill, T0, MIN, NOW_S
from test_exit_policy_sim import SimBase, Market, MultiMarket, outcome, FLAT
from test_exit_management import FakeExchange, offline, long_position, stop
from test_issue_95_trailing_activation import make_klines, flat_pre, market
from test_issue_106_exit_manager_hardening import long_record, LONG_POST, LONG_FORMING
import trading_scorecard as sc
from test_trading_scorecard import ScorecardBase, row as sc_row

H = 3600_000
TAKER_FEE, MAKER_FEE = eps.DEFAULT_TAKER_FEE, eps.DEFAULT_MAKER_FEE
TS = T0 // 1000 + 10  # audit write (s), 10 s after T0
REAL_LOAD_FILTERS = to.load_filters  # OutcomesBase patches trade_outcomes.load_filters


# ===================================================================================================== #210 matching
class TestEntryMatching(OutcomesBase):

    def test_stop_market_algo_id_matched_by_side_qty_window(self):
        # The audit stores the algoId (3953); userTrades reports the child orderId (2014), 40 s before the write.
        self.audit(entry_order_id=3953, timestamp=TS)
        fills = {"BTCUSDT": [fill(1, 2014, "BUY", 100.0, 10, TS * 1000 - 40_000, comm=0.5),
                             fill(2, 77, "SELL", 110.0, 10, TS * 1000 + H, pnl=100.0, comm=0.5)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        t = self.rows()[0]
        self.assertEqual((t["entry_match"], t["status"], t["entry_ts"]), ("side_qty_window", "closed", TS * 1000 - 40_000))
        self.assertTrue(t["entry_commission_included"])
        self.assertAlmostEqual(t["realized_r_net"], (100.0 - 1.0) / 50.0, places=4)

    def test_order_id_match_is_labelled(self):
        self.audit(entry_order_id=1)
        self.run_cli(FakeFills({"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 2, "SELL", 110, 10, T0 + H)]}),
                     ["--no-klines"])
        self.assertEqual(self.rows()[0]["entry_match"], "order_id")

    def test_window_qty_and_closing_fills_reject_a_match(self):
        # Outside the 90 min window, another qty, a closing BUY (realizedPnl != 0): none is this LONG's entry.
        cases = {"window": fill(1, 50, "BUY", 100, 10, TS * 1000 - 5401_000),
                 "qty": fill(1, 50, "BUY", 100, 7, TS * 1000 - 30_000),
                 "closing": fill(1, 50, "BUY", 100, 10, TS * 1000 - 30_000, pnl=-3.0),
                 "after": fill(1, 50, "BUY", 100, 10, TS * 1000 + 61_000)}
        for name, entry_fill in cases.items():
            with self.subTest(name):
                with open(os.path.join(self.logs, "trades_audit.jsonl"), "w", encoding="utf-8"):
                    pass
                self.audit(entry_order_id=3953, timestamp=TS)
                self.run_cli(FakeFills({"BTCUSDT": [entry_fill, fill(2, 77, "SELL", 110, 10, TS * 1000 + H)]}),
                             ["--no-klines", "--json"])
                t = self.rows()[0]
                self.assertEqual((t["status"], t["entry_match"], t["legs"], t["filled_qty"]),
                                 ("no_entry_fill", "no_entry_fill", [], None))

    def test_step_size_tolerance_and_closest_group(self):
        self.filters = {"BTCUSDT": {"tickSize": 0.1, "stepSize": 0.001}}
        self.audit(entry_order_id=3953, timestamp=TS, total_qty=10.0)
        fills = {"BTCUSDT": [fill(1, 60, "BUY", 99.0, 10.0005, TS * 1000 - 3000_000),
                             fill(2, 61, "BUY", 100.0, 10.0008, TS * 1000 - 20_000),
                             fill(3, 77, "SELL", 110.0, 10.0008, TS * 1000 + H)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        by_dir = self.rows()
        self.assertEqual(by_dir[0]["entry_match"], "side_qty_window")
        self.assertEqual(by_dir[0]["entry_ts"], TS * 1000 - 20_000)  # the group closest to the audit write
        self.assertEqual(by_dir[0]["entry_vwap"], 100.0)

    def test_window_starts_at_the_previous_same_symbol_record(self):
        # Previous record 10 min before: a fill 20 min before the second write belongs to neither.
        self.audit(entry_order_id=1, timestamp=TS - 600)
        self.audit(entry_order_id=3953, timestamp=TS, direction="SHORT", sl_price=105.0)
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 10, (TS - 600) * 1000 - 5000),
                             fill(2, 61, "SELL", 100, 10, TS * 1000 - 1200_000)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        by = {t["direction"]: t for t in self.rows()}
        self.assertEqual(by["SHORT"]["status"], "no_entry_fill")

    def test_ena_case_unfilled_record_never_consumes_a_later_entry(self):
        # ENAUSDT LONG of 2026-10-03 (no fill on this account), then a STOP_MARKET SHORT (algoId 999, child 555, 185)
        # closed by a BUY: the LONG gets no legs, the SHORT entry SELL is not a LONG exit leg.
        self.audit(symbol="ENAUSDT", entry_order_id=111, timestamp=TS - 3 * 86400, total_qty=185.0, entry_price=0.25,
                   sl_price=0.24)
        self.audit(symbol="ENAUSDT", direction="SHORT", entry_order_id=999, timestamp=TS, total_qty=185.0,
                   entry_price=0.23647, sl_price=0.245)
        fills = {"ENAUSDT": [fill(1, 555, "SELL", 0.23647, 185, TS * 1000 - 15_000, symbol="ENAUSDT"),
                             fill(2, 556, "BUY", 0.22, 185, TS * 1000 + H, pnl=3.0, symbol="ENAUSDT")]}
        code, out = self.run_cli(FakeFills(fills), ["--no-klines", "--json"])
        self.assertEqual(code, 0)
        by = {t["direction"]: t for t in self.rows()}
        self.assertEqual((by["LONG"]["status"], by["LONG"]["legs"]), ("no_entry_fill", []))
        self.assertEqual((by["SHORT"]["status"], by["SHORT"]["entry_match"]), ("closed", "side_qty_window"))
        self.assertEqual([l["order_id"] for l in by["SHORT"]["legs"]], [556])
        s = json.loads(out)
        self.assertEqual((s["closed"], s["summary"]["by_exit_reason"]), (1, {"MANUAL_OR_OTHER": 1}))

    def test_no_entry_fill_still_bounds_an_earlier_same_direction_trade(self):
        # LONG 1 filled (order id), LONG 2 never filled: LONG 1 consumes only fills before LONG 2's timestamp - 120 s.
        self.audit(entry_order_id=1, timestamp=TS)
        self.audit(entry_order_id=2, timestamp=TS + 7200)
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 9, "SELL", 101, 4, T0 + H),
                             fill(3, 8, "SELL", 102, 6, (TS + 7200) * 1000 + H)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        first, second = self.rows()
        self.assertEqual((first["status"], [l["order_id"] for l in first["legs"]]), ("open", [9]))
        self.assertEqual(second["status"], "no_entry_fill")

    def test_side_qty_assignment_is_global_not_greedy(self):
        # R1 written at T, R2 at T + 30 s, same qty. R1's child fill at T - 50 s, R2's at T + 20 s: greedy oldest-first
        # would give R1 the T + 20 s fill (closer) and leave R2 without one (its window starts at T).
        self.audit(entry_order_id=9001, timestamp=TS)
        self.audit(entry_order_id=9002, timestamp=TS + 30)
        fills = {"BTCUSDT": [fill(1, 71, "BUY", 100, 10, TS * 1000 - 50_000),
                             fill(2, 72, "BUY", 100, 10, TS * 1000 + 20_000)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        first, second = sorted(self.rows(), key=lambda t: t["entry_ts"])
        self.assertEqual((first["entry_match"], first["entry_ts"]), ("side_qty_window", TS * 1000 - 50_000))
        self.assertEqual((second["entry_match"], second["entry_ts"]), ("side_qty_window", TS * 1000 + 20_000))
        recs = [dict(symbol="BTCUSDT", direction="LONG", total_qty=10.0, timestamp=TS, entry_order_id=9001),
                dict(symbol="BTCUSDT", direction="LONG", total_qty=10.0, timestamp=TS + 30, entry_order_id=9002)]
        matches, foreign = to.match_entries(recs, fills["BTCUSDT"])
        self.assertEqual([[f["orderId"] for f in m["fills"]] for m in matches], [[71], [72]])
        self.assertEqual((foreign[0], foreign[1]), ({"72", "9002"}, {"71", "9001"}))
        # Equal distance: the older record wins
        tie = [fill(3, 73, "BUY", 100, 10, (TS + 15) * 1000)]
        matches, _ = to.match_entries(recs, tie)
        self.assertEqual([m["match"] for m in matches], ["side_qty_window", "no_entry_fill"])

    def test_zero_pnl_tp_fill_of_another_record_is_never_an_entry(self):
        # A SHORT's TP1 BUY (orderId 300) closing exactly at its entry (realizedPnl 0) with the LONG's qty, 20 s
        # before the LONG's write: not the LONG's entry.
        self.audit(direction="SHORT", sl_price=105.0, entry_order_id=1, tp1_order_id=300, timestamp=TS - 3600)
        self.audit(entry_order_id=9001, timestamp=TS)
        fills = {"BTCUSDT": [fill(1, 1, "SELL", 100, 10, (TS - 3600) * 1000 - 5000),
                             fill(2, 300, "BUY", 100, 10, TS * 1000 - 20_000, pnl=0.0)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        by = {t["direction"]: t for t in self.rows()}
        self.assertEqual(by["LONG"]["status"], "no_entry_fill")
        self.assertEqual([l["order_id"] for l in by["SHORT"]["legs"]], [300])

    def test_entry_vwap_is_the_move_basis_over_the_sized_risk(self):
        # Entry fills 100 x 5 and 102 x 5 (VWAP 101, audit entry 100), SL 95, exit 107: move 6 from the VWAP over the
        # sized risk |100 - 95| = 5 -> 1.2R (PR #212 review: R denominator = the risk the order was sized on).
        self.audit(entry_order_id=1)
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 5, T0), fill(2, 1, "BUY", 102, 5, T0 + 1000),
                             fill(3, 2, "SELL", 107, 10, T0 + H)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        t = self.rows()[0]
        self.assertEqual((t["entry_price"], t["entry_vwap"], t["initial_risk"]), (100.0, 101.0, 5.0))
        self.assertAlmostEqual(t["realized_r_gross"], 1.2, places=4)


# ======================================================================================================= #191 labels
class TestLegLabels(OutcomesBase):

    def rec(self, **over):
        base = dict(symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=95.0)
        base.update(over)
        return base

    def test_overlapping_sl_and_be_bands_nearest_level_wins(self):
        # SL 99.9 (0.1 % below entry): a fill at 100.15 is inside the SL tolerance (0.3 %) but nearest to BE 100.2.
        rec = self.rec(sl_price=99.9)
        self.assertEqual(to.leg_reason({"orderId": 9, "price": "100.15", "time": T0 + H}, rec, [], T0), "BREAKEVEN")
        self.assertEqual(to.leg_reason({"orderId": 9, "price": "99.92", "time": T0 + H}, rec, [], T0), "SL")
        # Through the CLI as well
        self.audit(entry_order_id=1, sl_price=99.9)
        self.run_cli(FakeFills({"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 9, "SELL", 100.15, 10, T0 + H)]}),
                     ["--no-klines"])
        self.assertEqual(self.rows()[0]["exit_reason"], "BREAKEVEN")

    def test_be_fill_with_slippage_beyond_the_level_is_breakeven(self):
        self.assertEqual(to.leg_reason({"orderId": 9, "price": "100.3", "time": T0 + H}, self.rec(), [], T0),
                         "BREAKEVEN")
        short = self.rec(direction="SHORT", sl_price=105.0)
        self.assertEqual(to.leg_reason({"orderId": 9, "price": "99.7", "time": T0 + H}, short, [], T0), "BREAKEVEN")
        # The unfavourable side is not break-even any more, and +0.5 % is a manual exit
        self.assertEqual(to.leg_reason({"orderId": 9, "price": "99.9", "time": T0 + H}, self.rec(), [], T0),
                         "MANUAL_OR_OTHER")
        self.assertEqual(to.leg_reason({"orderId": 9, "price": "100.5", "time": T0 + H}, self.rec(), [], T0),
                         "MANUAL_OR_OTHER")

    def test_trailed_stop_nearest_of_several_levels(self):
        rec = self.rec()
        trails = [((T0 + 10 * MIN) / 1000, 102.0), ((T0 + 20 * MIN) / 1000, 102.4)]
        f = {"orderId": 9, "price": "102.35", "time": T0 + H}
        self.assertEqual(to.leg_reason(f, rec, trails, T0), "TRAILED_STOP")
        self.assertEqual(to.leg_reason(f, rec, [((T0 - MIN) / 1000, 102.4)], T0), "MANUAL_OR_OTHER")  # before entry

    def test_tick_size_widens_the_stop_tolerance(self):
        # Price 0.00954, SL 0.0095: 0.3 % = 0.0000286 < distance 0.00004 = 2 ticks of 0.00002.
        rec = self.rec(entry_price=0.0100, sl_price=0.0095)
        f = {"orderId": 9, "price": "0.00954", "time": T0 + H}
        self.assertEqual(to.leg_reason(f, rec, [], T0), "MANUAL_OR_OTHER")
        self.assertEqual(to.leg_reason(f, rec, [], T0, tick=0.00002), "SL")
        self.filters = {"BTCUSDT": {"tickSize": 0.00002, "stepSize": 1.0}}
        self.audit(entry_order_id=1, entry_price=0.0100, sl_price=0.0095)
        self.run_cli(FakeFills({"BTCUSDT": [fill(1, 1, "BUY", 0.0100, 10, T0),
                                            fill(2, 9, "SELL", 0.00954, 10, T0 + H)]}), ["--no-klines"])
        self.assertEqual(self.rows()[0]["exit_reason"], "SL")

    def test_exchange_info_failure_keeps_percent_tolerance(self):
        with patch("exit_policy_sim.fetch_exchange_info", side_effect=OSError("down")):
            self.assertEqual(REAL_LOAD_FILTERS("prod"), {})
        with patch("exit_policy_sim.fetch_exchange_info",
                   return_value={"symbols": [{"symbol": "BTCUSDT", "filters": [
                       {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                       {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"}]}]}):
            self.assertEqual(REAL_LOAD_FILTERS("prod")["BTCUSDT"]["tickSize"], 0.1)


class TestExitMinuteAndRequestCap(OutcomesBase):

    def test_mfe_excludes_prints_after_the_exit(self):
        self.audit(entry_order_id=1)
        exit_ms = T0 + 2 * H + 30_000  # mid-minute exit
        exit_bar = exit_ms // MIN * MIN
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 9, "SELL", 101, 10, exit_ms)]}
        klines = KlinesFake(lambda s, o: (130.0, 99.0) if o == exit_bar else (102.0, 99.0))
        self.run_cli(FakeFills(fills), [], klines=klines)
        t = self.rows()[0]
        self.assertEqual(t["mfe_r"], 0.4)  # 102, not the 130 printed after the exit inside the exit minute

    def test_klines_pages_of_1000_with_shared_429_backoff(self):
        self.assertEqual(to.KLINES_LIMIT, 1000)
        self.audit(entry_order_id=1)
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 9, "SELL", 101, 10, T0 + 2 * H)]}
        inner = KlinesFake(lambda s, o: (102.0, 99.0))
        calls = []

        def flaky(*a, **k):
            calls.append(a)
            if len(calls) == 1:
                raise urllib.error.HTTPError("u", 429, "limited", {"Retry-After": "2"}, None)
            return inner(*a, **k)

        with patch("utils.trade_excursion.time.sleep") as sleep:
            self.run_cli(FakeFills(fills), [], klines=flaky)
        t = self.rows()[0]
        self.assertEqual(t["mfe_r"], 0.4)
        self.assertNotIn("klines_error", t)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0])
        self.assertEqual({c[3] for c in calls}, {1000})

    def test_split_cap_sets_truncated_and_a_warning(self):
        t_entry = T0 - 30_000
        fills = [fill(1, 1, "BUY", 100, 2500, t_entry)]
        fills += [fill(10 + i, 500 + i, "SELL", 101, 1, t_entry + 1000 + 1000 * (i // 3)) for i in range(2500)]
        self.audit(total_qty=2500.0, entry_order_id=1)
        for patched in ({"MAX_SPLIT_DEPTH": 2}, {"MAX_REQUESTS_PER_SYMBOL": 3}):
            with self.subTest(patched), patch.multiple(to, **patched):
                fake = FakeFills({"BTCUSDT": fills})
                code, out = self.run_cli(fake, ["--no-klines", "--json"])
                self.assertEqual(code, 0)
                self.assertTrue(self.rows()[0]["truncated"])
                self.assertTrue(any("BTCUSDT" in w for w in json.loads(out)["warnings"]))
                if "MAX_REQUESTS_PER_SYMBOL" in patched:
                    self.assertEqual(len(fake.calls), 3)
        budget = {}
        with patch("execute_futures_trade.send_signed_request", side_effect=FakeFills({"BTCUSDT": fills})):
            got, err = to.fetch_fills("BTCUSDT", t_entry - 5 * MIN, NOW_S * 1000, "prod", budget=budget)
        self.assertIsNone(err)
        self.assertFalse(budget["truncated"])
        self.assertLessEqual(budget["requests"], to.MAX_REQUESTS_PER_SYMBOL)
        self.assertEqual(len(got), 2501)

    def test_output_outside_logs_exits_2_without_writing(self):
        self.audit(entry_order_id=1)
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        os.symlink(outside, os.path.join(self.logs, "escape"))
        fake = FakeFills({"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0)]})
        for path in (os.path.join(outside, "x.jsonl"), os.path.join(self.logs, "..", "x.jsonl"),
                     os.path.join(self.logs, "escape", "x.jsonl"), self.logs):
            with self.subTest(path), contextlib.redirect_stderr(io.StringIO()):
                code, _ = self.run_cli(fake, ["--no-klines", "--output", path])
                self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(outside), [])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "x.jsonl")))


# ========================================================================================================== #210 sim
class TestSimRobustness(SimBase):

    def setUp(self):
        super().setUp()
        sleep_patch = patch("utils.trade_excursion.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_pages_of_1000_with_a_pause_between_pages(self):
        self.assertEqual(eps.KLINES_LIMIT, 1000)
        calls = []

        def fake(symbol, interval, start_ms, limit, env, timeout=None):
            calls.append((start_ms, limit))
            n = limit if len(calls) < 3 else 10
            return [[start_ms + i * MIN, "1", "1", "1", "1", "1", start_ms + i * MIN + MIN - 1] for i in range(n)]

        with patch("utils.trade_excursion.fetch_klines_range", side_effect=fake):
            rows = eps.fetch_range("BTCUSDT", "1m", 0, 10 ** 12, "prod")
        self.assertEqual(len(rows), 2010)
        self.assertEqual({c[1] for c in calls}, {1000})
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [eps.KLINES_PAGE_SLEEP_SECONDS] * 2)

    def test_429_backoff_honours_retry_after_then_gives_up(self):
        def http(code, retry_after=None):
            return urllib.error.HTTPError("u", code, "limited", {"Retry-After": retry_after} if retry_after else {},
                                          None)

        seq = [http(429, "3"), http(429), [[0, "1", "1", "1", "1", "1", MIN - 1]]]
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=seq):
            rows = eps.fetch_range("BTCUSDT", "1m", 0, MIN, "prod")
        self.assertEqual(len(rows), 1)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [3.0, 2.0])
        # PR #212 review: 418 = IP ban and a Retry-After above the cap are never retried into.
        for err in (http(418), http(418, "5"), http(429, str(eps.trade_excursion.KLINES_MAX_RETRY_AFTER_SECONDS + 1))):
            self.sleep.reset_mock()
            with patch("utils.trade_excursion.fetch_klines_range", side_effect=[err]) as f, \
                    self.assertRaises(urllib.error.HTTPError):
                eps.fetch_range("BTCUSDT", "1m", 0, MIN, "prod")
            self.assertEqual(f.call_count, 1)
            self.sleep.assert_not_called()
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=[http(429)] * 3) as f, \
                self.assertRaises(urllib.error.HTTPError):
            eps.fetch_range("BTCUSDT", "1m", 0, MIN, "prod")
        self.assertEqual(f.call_count, eps.KLINES_MAX_TRIES)
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=[http(500)]) as f, \
                self.assertRaises(urllib.error.HTTPError):
            eps.fetch_range("BTCUSDT", "1m", 0, MIN, "prod")
        self.assertEqual(f.call_count, 1)  # not a rate limit: no retry

    def test_rate_limited_trade_is_a_klines_error(self):
        err = urllib.error.HTTPError("u", 429, "limited", {}, None)
        res = self.sim([outcome()], MultiMarket({"BTCUSDT": err}))
        self.assertEqual(res["skipped"], {"klines_error": 1})

    def test_no_entry_fill_rows_and_entry_match_flags(self):
        rows = [outcome(status="no_entry_fill"),
                outcome(symbol="ETHUSDT", entry_match="legacy", entry_commission_included=False),
                outcome(symbol="SOLUSDT", entry_match="side_qty_window", entry_commission_included=True)]
        sl_path = [FLAT, (100.0, 98.9, 99.0)]
        res = self.sim(rows, MultiMarket({s: Market(sl_path) for s in ("ETHUSDT", "SOLUSDT")}))
        self.assertEqual(res["skipped"], {"no_entry_fill": 1})
        flags = {t["symbol"]: t["entry_ts_approx"] for t in res["policies"]["current"]["trades"]}
        self.assertEqual(flags, {"ETHUSDT": True, "SOLUSDT": False})
        self.assertTrue(eps.entry_ts_approx({"entry_commission_included": False}))  # pre-entry_match rows
        self.assertFalse(eps.entry_ts_approx({"entry_match": "order_id"}))

    def test_ranking_caveat_and_auth_mode_note(self):
        res = self.sim([outcome()], Market([FLAT, (100.0, 98.9, 99.0)]))
        self.assertIn(eps.RANKING_NOTE, res["warnings"])
        self.assertIn(eps.AUTH_MODE_NOTE, res["warnings"])
        self.assertIn("reviewed PR", eps.RANKING_NOTE)
        self.assertIn("KEYS", eps.AUTH_MODE_NOTE)

    def test_replay_starts_from_entry_vwap_for_every_policy(self):
        # Row given directly: replay starts at the VWAP 100.5 (trade_outcomes would emit initial_risk 1.0 = |100 - 99|).
        row = outcome(entry_price=100.0, entry_vwap=100.5, initial_risk=1.5)
        t, _ = self.one(row, [FLAT, (100.0, 98.9, 99.0)], "current")
        self.assertAlmostEqual(t["r"], -1.0 - (TAKER_FEE * 100.5 + TAKER_FEE * 99.0) / 1.5, places=6)
        # close_at_0_5r: target = 100.5 + 0.5 x 1.5 = 101.25 (100.75 from the audit entry would fill on 101.0)
        t, _ = self.one(row, [FLAT, (101.0, 100.4, 100.9), (101.3, 100.9, 101.2)], "close_at_0_5r")
        self.assertAlmostEqual(t["r"], 0.5 - (TAKER_FEE * 100.5 + MAKER_FEE * 101.25) / 1.5, places=6)
        self.assertEqual(eps.entry_basis(outcome()), 100.0)  # no entry_vwap: the audit entry
        self.assertEqual(eps.entry_basis(outcome(entry_vwap=None)), 100.0)

    def test_malformed_lines_are_counted(self):
        with open(os.path.join(self.logs, "trade_outcomes.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(outcome()) + "\n{broken\n[1, 2]\n\n")
        out = io.StringIO()
        fake = MultiMarket({"BTCUSDT": Market([FLAT, (100.0, 98.9, 99.0)])})
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=fake), contextlib.redirect_stdout(out):
            code = eps.main(["--env", "prod", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["skipped"], {"malformed": 2})


class TestLiveTrailCallerInjectsNothing(unittest.TestCase):

    def test_update_position_passes_neither_klines_nor_filters(self):
        klines, entry_ts = make_klines(flat_pre(), LONG_POST, LONG_FORMING, time.time())
        fake = FakeExchange([long_position(mark="101.0")], algos=[stop(501, 99.0)])
        with offline(fake) as ws, market(klines), \
                patch.object(dem, "calculate_structural_stop", wraps=dem.calculate_structural_stop) as calc:
            long_record(ws, timestamp=entry_ts)
            dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet", dry_run=True)
        self.assertEqual(calc.call_count, 1)
        kwargs = calc.call_args.kwargs
        self.assertNotIn("klines_15m", kwargs)
        self.assertNotIn("filters", kwargs)


# ======================================================================================================= #208 summary
DAY = (NOW_S // 86400) * 86400 * 1000  # today 00:00 UTC (ms)


class TestClosedTodaySummary(unittest.TestCase):

    def rec(self, **over):
        base = dict(symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=95.0, total_qty=10.0,
                    target_env="prod", timestamp=DAY // 1000 + 600, entry_order_id=1, tp1_order_id=2)
        base.update(over)
        return base

    def test_tp1_partial_plus_trailed_runner_is_one_trade(self):
        fills = [fill(1, 1, "BUY", 100, 10, DAY + 590_000, comm=0.5),
                 fill(2, 2, "SELL", 109, 3, DAY + 590_000 + H, pnl=27.0, comm=0.1),
                 fill(3, 8, "SELL", 104, 7, DAY + 590_000 + 2 * H, pnl=28.0, comm=0.2)]
        s = to.summarize_closed_today([self.rec()], fills, DAY, "prod")
        self.assertEqual((s["trades_closed"], s["wins"], s["losses"], s["scratches"]), (1, 1, 0, 0))
        self.assertEqual((s["fills_closed"], s["partial_history"]), (2, 0))
        self.assertAlmostEqual(s["realized_r_net_sum"], round((55.0 - 0.8) / 50.0, 4), places=4)
        self.assertAlmostEqual(s["realized_r_gross_sum"], (27 + 28) / 50.0, places=4)
        # Only the TP1 so far: open, not counted
        s = to.summarize_closed_today([self.rec()], fills[:2], DAY, "prod")
        self.assertEqual((s["trades_closed"], s["wins"], s["fills_closed"]), (0, 0, 1))

    def test_scratch_loss_and_other_env(self):
        recs = [self.rec(),
                self.rec(symbol="ETHUSDT", entry_order_id=10),
                self.rec(symbol="SOLUSDT", entry_order_id=20, target_env="testnet")]
        fills = [fill(1, 1, "BUY", 100, 10, DAY + 590_000), fill(2, 9, "SELL", 100.1, 10, DAY + H, pnl=1.0),
                 fill(3, 10, "BUY", 100, 10, DAY + 590_000, symbol="ETHUSDT"),
                 fill(4, 11, "SELL", 96, 10, DAY + H, pnl=-40.0, symbol="ETHUSDT"),
                 fill(5, 20, "BUY", 100, 10, DAY + 590_000, symbol="SOLUSDT"),
                 fill(6, 21, "SELL", 110, 10, DAY + H, pnl=100.0, symbol="SOLUSDT")]
        s = to.summarize_closed_today(recs, fills, DAY, "prod")
        self.assertEqual((s["trades_closed"], s["wins"], s["losses"], s["scratches"]), (2, 0, 1, 1))
        self.assertEqual(s["fills_closed"], 3)

    def test_two_overnight_records_same_direction_count_once(self):
        # Two LONG records written before today (no fill in the window): only the newest takes today's legs.
        recs = [self.rec(timestamp=DAY // 1000 - 7200, entry_order_id=1),
                self.rec(timestamp=DAY // 1000 - 3600, entry_order_id=5)]
        today = [fill(2, 9, "SELL", 105, 10, DAY + H, pnl=50.0)]
        s = to.summarize_closed_today(recs, today, DAY, "prod")
        self.assertEqual((s["trades_closed"], s["wins"], s["partial_history"]), (1, 1, 1))
        matches, _ = to.match_entries(recs, today, window_start_ms=DAY)
        self.assertEqual([m["match"] for m in matches], ["partial_history", "partial_history"])

    def test_overnight_entry_counts_with_partial_history(self):
        yesterday = DAY // 1000 - 3600
        # Closed today in full by today's fills (entry fill yesterday, outside the fetched window)
        s = to.summarize_closed_today([self.rec(timestamp=yesterday)],
                                      [fill(2, 9, "SELL", 105, 10, DAY + H, pnl=50.0)], DAY, "prod")
        self.assertEqual((s["trades_closed"], s["wins"], s["partial_history"]), (1, 1, 1))
        # TP1 yesterday, runner today: today's legs do not close the audit qty; counted only when no position is open
        runner = [fill(3, 8, "SELL", 104, 7, DAY + H, pnl=28.0)]
        s = to.summarize_closed_today([self.rec(timestamp=yesterday)], runner, DAY, "prod")
        self.assertEqual(s["trades_closed"], 0)
        s = to.summarize_closed_today([self.rec(timestamp=yesterday)], runner, DAY, "prod", open_positions=set())
        self.assertEqual((s["trades_closed"], s["wins"], s["partial_history"]), (1, 1, 1))
        s = to.summarize_closed_today([self.rec(timestamp=yesterday)], runner, DAY, "prod",
                                      open_positions={("BTCUSDT", "LONG")})
        self.assertEqual(s["trades_closed"], 0)


class DayExchange:
    """Flat book; GET /fapi/v1/userTrades without a symbol returns the day's fills (records its params)."""

    def __init__(self, day_fills, error=None, error_pages=(1,)):
        self.day_fills = day_fills
        self.day_params = []
        self.error, self.error_pages = error, set(error_pages)  # error payload on these page numbers (1-based)

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        if method != "GET":
            raise AssertionError(f"write request {method} {endpoint}")
        if endpoint == "/fapi/v1/ticker/price":
            return {"price": "100.0"}
        if endpoint in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders", "/fapi/v2/positionRisk"):
            return []
        if endpoint == "/fapi/v1/userTrades" and "symbol" not in params:
            self.day_params.append(params)
            if self.error is not None and len(self.day_params) in self.error_pages:
                return dict(self.error)
            rows = sorted((f for f in self.day_fills if f["time"] >= params["startTime"]),
                          key=lambda f: (f["time"], f["id"]))
            return [dict(f) for f in rows[:params["limit"]]]
        raise AssertionError(f"unexpected request {endpoint}")


class TestSyncClosedTodayKeys(unittest.TestCase):

    def run_sync(self, fake, audit=()):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "session_state.json")
        with open(os.path.join(tmp, "trades_audit.jsonl"), "w", encoding="utf-8") as f:
            for r in audit:
                f.write(json.dumps(r) + "\n")
        with patch.object(sss, "LOGS_DIR", tmp), patch.object(sss, "STATE_FILE", path), \
             patch.object(sss, "AUDIT_LOG", os.path.join(tmp, "trades_audit.jsonl")), \
             patch.object(sss, "get_start_of_day_utc", return_value=DAY), \
             patch.dict(sys.modules, {"shadow_tracker": None}), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             patch("execute_futures_trade.send_signed_request", side_effect=fake):
            return sss.sync_session_state(target_env="prod")

    def test_unreadable_day_fills_are_visible(self):
        """PR #212 review: a non-list first userTrades read is flagged, not a silent 0."""
        state = self.run_sync(DayExchange([], error={"code": -1102, "msg": "symbol required"}), audit=[])
        c = state["closed_today_summary"]
        self.assertEqual(c["counted_by"], "unavailable")
        self.assertIn("symbol required", c["fills_error"])

    def test_partly_unmatched_symbols_are_named(self):
        """PR #212 review: one symbol counted per trade, another flat symbol with closing fills and no audit trade:
        the counts stay per trade and the left-out symbol is named."""
        rec = dict(symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=95.0, total_qty=10.0,
                   target_env="prod", timestamp=DAY // 1000 + 600, entry_order_id=1, tp1_order_id=2)
        fills = [fill(1, 1, "BUY", 100, 10, DAY + 590_000, comm=0.5),
                 fill(2, 2, "SELL", 109, 10, DAY + 590_000 + H, pnl=90.0, comm=0.1),
                 dict(fill(9, 9, "SELL", 104, 7, DAY + 3 * H, pnl=28.0, comm=0.2), symbol="ETHUSDT")]
        state = self.run_sync(DayExchange(fills), audit=[rec])
        c = state["closed_today_summary"]
        self.assertEqual((c["closed_trades_count"], c["counted_by"]), (1, "trades"))
        self.assertIn("ETHUSDT", c["trade_summary_error"])

    def test_day_fill_exception_reads_as_unavailable(self):
        def boom(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v1/userTrades":
                raise ConnectionError("reset")
            return DayExchange([])(method, endpoint, params, target_env, retry_count)
        state = self.run_sync(boom, audit=[])
        c = state["closed_today_summary"]
        self.assertEqual(c["counted_by"], "unavailable")
        self.assertIn("ConnectionError", c["fills_error"])

    def test_unmatched_closing_fills_fall_back_to_fill_counts(self):
        """PR #212 review: closing fills of a flat symbol with no matching audit trade (e.g. a manual exit of a
        position missing from the audit) never read as 0 closed trades."""
        fills = [fill(9, 9, "SELL", 104, 7, DAY + 3 * H, pnl=28.0, comm=0.2)]
        state = self.run_sync(DayExchange(fills), audit=[])
        c = state["closed_today_summary"]
        self.assertEqual((c["closed_trades_count"], c["wins"], c["counted_by"]), (1, 1, "fills"))
        self.assertIn("without a matching audit trade", c["trade_summary_error"])

    def test_summary_failure_falls_back_to_fill_counts(self):
        """PR #212 review: a summarize_closed_today exception never reports 0 closed trades after real exits."""
        rec = dict(symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=95.0, total_qty=10.0,
                   target_env="prod", timestamp=DAY // 1000 + 600, entry_order_id=1, tp1_order_id=2)
        fills = [fill(1, 1, "BUY", 100, 10, DAY + 590_000, comm=0.5),
                 fill(2, 2, "SELL", 109, 3, DAY + 590_000 + H, pnl=27.0, comm=0.1),
                 fill(3, 8, "SELL", 94, 7, DAY + 590_000 + 2 * H, pnl=-42.0, comm=0.2)]
        with patch("trade_outcomes.summarize_closed_today", side_effect=RuntimeError("boom")):
            state = self.run_sync(DayExchange(fills), audit=[rec])
        c = state["closed_today_summary"]
        self.assertTrue(state["is_valid"], state)
        self.assertEqual((c["closed_trades_count"], c["wins"], c["losses"]), (2, 1, 1))
        self.assertEqual(c["counted_by"], "fills")
        self.assertIn("RuntimeError: boom", c["trade_summary_error"])
        self.assertIsNone(c["realized_r_net"])
        self.assertIn("Realized R (net):** n/a", sss.format_markdown_summary(state))

    def test_sync_counts_trades_and_mirrors_keys_in_the_error_state(self):
        rec = dict(symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=95.0, total_qty=10.0,
                   target_env="prod", timestamp=DAY // 1000 + 600, entry_order_id=1, tp1_order_id=2)
        fills = [fill(1, 1, "BUY", 100, 10, DAY + 590_000, comm=0.5),
                 fill(2, 2, "SELL", 109, 3, DAY + 590_000 + H, pnl=27.0, comm=0.1),
                 fill(3, 8, "SELL", 104, 7, DAY + 590_000 + 2 * H, pnl=28.0, comm=0.2)]
        fake = DayExchange(fills)
        state = self.run_sync(fake, audit=[rec])
        self.assertTrue(state["is_valid"], state)
        self.assertEqual(fake.day_params[0]["limit"], 1000)
        c = state["closed_today_summary"]
        self.assertEqual((c["closed_trades_count"], c["wins"], c["losses"], c["scratches"]), (1, 1, 0, 0))
        self.assertEqual((c["fills_closed"], c["partial_history"], c["win_rate_pct"]), (2, 0, 100.0))
        self.assertAlmostEqual(c["realized_r_net"], round((55.0 - 0.8) / 50.0, 4), places=4)
        self.assertAlmostEqual(c["gross_realized_pnl_usdt"], 55.0)
        self.assertAlmostEqual(c["commissions_usdt"], 0.8)
        md = sss.format_markdown_summary(state)
        self.assertIn("Closed Trades Today:** 1 (Wins: 1 | Losses: 0 | Scratches: 0", md)
        self.assertIn("Realized R (net):** +1.08R", md)
        self.assertIn("Closing fills: 2", md)
        self.assertFalse(c["truncated"])
        with patch.object(sss, "STATE_FILE", os.path.join(tempfile.mkdtemp(), "s.json")):
            err = sss.write_error_state("x", 0, "now", "prod", 0.0)
        self.assertEqual(sorted(err["closed_today_summary"]), sorted(c))

    def day_fills(self, n, same_ms=False):
        return [fill(100 + i, 900 + i, "SELL", 101, 1, DAY + 1000 + (0 if same_ms else 1000 * i), pnl=1.0)
                for i in range(n)]

    def test_day_fills_are_paged_until_a_short_page(self):
        fake = DayExchange(self.day_fills(2500))
        with patch("execute_futures_trade.send_signed_request", side_effect=fake):
            fills, truncated = sss.fetch_day_fills(DAY, "prod")
        self.assertEqual((len(fills), truncated, len(fake.day_params)), (2500, False, 3))
        self.assertEqual([p["limit"] for p in fake.day_params], [1000] * 3)
        self.assertEqual(fake.day_params[0]["startTime"], DAY)
        self.assertEqual([f["id"] for f in fills], sorted(f["id"] for f in fills))
        state = self.run_sync(DayExchange(self.day_fills(2500)))
        self.assertEqual((state["closed_today_summary"]["fills_closed"], state["closed_today_summary"]["truncated"]),
                         (2500, False))

    def test_same_fill_id_on_two_symbols_is_kept_twice(self):
        # userTrades ids are per symbol: BTC id 7 and ETH id 7 are two fills
        fills = [fill(7, 1, "SELL", 101, 1, DAY + 1000, pnl=1.0, symbol="BTCUSDT"),
                 fill(7, 2, "SELL", 51, 1, DAY + 1000, pnl=-1.0, symbol="ETHUSDT")]
        fake = DayExchange(fills)
        with patch("execute_futures_trade.send_signed_request", side_effect=fake):
            got, truncated = sss.fetch_day_fills(DAY, "prod")
        self.assertEqual(sorted((f["symbol"], f["id"]) for f in got), [("BTCUSDT", 7), ("ETHUSDT", 7)])
        self.assertFalse(truncated)
        self.assertEqual(self.run_sync(DayExchange(fills))["closed_today_summary"]["fills_closed"], 2)

    def test_page_cap_same_millisecond_and_failed_page_set_truncated(self):
        for fake, expected in ((DayExchange(self.day_fills(12000)), (1000 + 9 * 999, True, 10)),
                               (DayExchange(self.day_fills(1500, same_ms=True)), (1000, True, 2)),
                               (DayExchange(self.day_fills(2500), error={"code": -1003}, error_pages=(2,)),
                                (1000, True, 2))):
            with patch("execute_futures_trade.send_signed_request", side_effect=fake):
                fills, truncated = sss.fetch_day_fills(DAY, "prod")
            self.assertEqual((len(fills), truncated, len(fake.day_params)), expected)
        with patch("execute_futures_trade.send_signed_request",
                   side_effect=DayExchange([], error={"code": -1003, "msg": "x"})):
            res, truncated = sss.fetch_day_fills(DAY, "prod")
        self.assertEqual((res, truncated), ({"code": -1003, "msg": "x"}, False))  # first read failed: unreadable
        state = self.run_sync(DayExchange(self.day_fills(12000)))
        self.assertTrue(state["closed_today_summary"]["truncated"])
        self.assertIn("day fills truncated", sss.format_markdown_summary(state))


# ================================================================================ #191 offline CLI output paths
class TestOfflineOutputPaths(ScorecardBase):

    def test_scorecard_out_inside_logs_never_the_store_never_through_a_hard_link(self):
        self.write_outcomes([sc_row()])
        sentinel = os.path.join(self.ws, "sentinel.json")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write('{"keep": true}')
        report = os.path.join(self.logs, "trading_scorecard.json")
        os.link(sentinel, report)  # a hard link planted at the report path
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sc.main(["--env", "prod", "--json"]), 0)
        with open(sentinel, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"keep": true}')
        with open(report, encoding="utf-8") as f:
            self.assertIn("sample_size", json.load(f))
        store = os.path.join(self.ws, "logs", "score_calibration.json")
        with open(store, "w", encoding="utf-8") as f:
            f.write('{"store": 1}')
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        for out in (store, os.path.join(outside, "sc.json"), os.path.join(self.logs, "..", "sc.json")):
            with self.subTest(out), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sc.main(["--env", "prod", "--out", out]), 2)
        with open(store, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"store": 1}')  # refused before the run wrote anything
        self.assertEqual(os.listdir(outside), [])

    def test_sim_out_must_stay_inside_logs(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
                patch("utils.trade_excursion.fetch_klines_range", side_effect=AssertionError("klines read")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(eps.main(["--env", "prod", "--out", os.path.join(outside, "x.json")]), 2)
        self.assertEqual(os.listdir(outside), [])




class TestCalibrationStoreRekeying(unittest.TestCase):
    """PR #212 review (trading_risk): a re-keyed trade (better entry matching) is counted once in the Tier S
    calibration store (replaced, never added); open / no_entry_fill / fills_unavailable / truncated rows never delete."""

    AUDIT = 1791470000

    def row(self, **kw):
        base = {"symbol": "BTCUSDT", "direction": "SHORT", "env": "prod", "status": "closed", "is_yolo": False,
                "audit_ts": self.AUDIT, "entry_ts": self.AUDIT * 1000 - 3000, "realized_r_net": 1.0, "mfe_r": 1.5,
                "score": 85, "dossier_score": 85}
        base.update(kw)
        return base

    def legacy_store(self):
        from utils import score_calibration as sc
        legacy = self.row(entry_ts=self.AUDIT * 1000 - sc.LEGACY_ENTRY_SLACK_MS)
        legacy.pop("audit_ts")
        return sc.merge_store(None, [legacy], now=1)

    def test_legacy_key_replaced_not_added(self):
        from utils import score_calibration as sc
        store = self.legacy_store()
        self.assertEqual(len(store["trades"]), 1)
        merged = sc.merge_store(store, [self.row(entry_match="side_qty_window")], now=2)
        self.assertEqual(list(merged["trades"]), [sc.trade_key(self.row())])

    def test_same_audit_ts_rekeyed(self):
        from utils import score_calibration as sc
        first = sc.merge_store(None, [self.row(entry_ts=self.AUDIT * 1000 - 9000)], now=1)
        merged = sc.merge_store(first, [self.row()], now=2)
        self.assertEqual(len(merged["trades"]), 1)

    def test_non_closed_or_degraded_rows_never_delete(self):
        """Dropping a stored loss could lift a bucket over its threshold (loosen the Tier S gate): never delete."""
        from utils import score_calibration as sc
        store = self.legacy_store()
        for row in (self.row(status="no_entry_fill", entry_ts=self.AUDIT * 1000), self.row(status="open"),
                    self.row(status="fills_unavailable"), self.row(truncated=True), self.row(truncated=True, entry_ts=1)):
            with self.subTest(row=row):
                merged = sc.merge_store(store, [row], now=2)
                self.assertEqual(merged["trades"], store["trades"])

    def test_truncated_rows_not_stored(self):
        from utils import score_calibration as sc
        merged = sc.merge_store(None, [self.row(truncated=True)], now=1)
        self.assertEqual(merged["trades"], {})

    def test_other_symbol_or_direction_untouched(self):
        from utils import score_calibration as sc
        store = self.legacy_store()
        merged = sc.merge_store(store, [self.row(direction="LONG"), self.row(symbol="ETHUSDT")], now=2)
        self.assertEqual(len(merged["trades"]), 3)


class TestRSizedRiskBasis(unittest.TestCase):

    def test_r_is_measured_on_the_sized_risk(self):
        """PR #212 review: R = real move from the fill basis over the risk the order was sized on (audit entry - SL),
        so adverse entry slippage is never hidden: fill 102 -> exit 92 on a planned 100 / SL 90 is -1.0R; stopped at
        90 it would be -1.2R."""
        rec = {"symbol": "BTCUSDT", "direction": "LONG", "entry_price": 100.0, "sl_price": 90.0, "total_qty": 1.0,
               "timestamp": 1000, "entry_order_id": 7}
        fills = [{"id": 1, "orderId": 7, "side": "BUY", "price": "102", "qty": "1", "time": 999000,
                  "realizedPnl": "0", "commission": "0", "commissionAsset": "USDT"},
                 {"id": 2, "orderId": 8, "side": "SELL", "price": "92", "qty": "1", "time": 1001000,
                  "realizedPnl": "-10", "commission": "0", "commissionAsset": "USDT"}]
        out = to.resolve_trade(rec, fills, {}, [], None, "prod", klines=False)
        self.assertEqual(out["audit_ts"], 1000)
        self.assertAlmostEqual(out["initial_risk"], 10.0)
        self.assertAlmostEqual(out["entry_vwap"], 102.0)
        self.assertAlmostEqual(out["realized_r_gross"], -1.0, places=3)
        self.assertAlmostEqual(out["realized_r_net"], -1.0, places=3)
        self.assertNotIn("realized_r_budget", out)
        fills[1].update(price="90", realizedPnl="-12")
        out = to.resolve_trade(rec, fills, {}, [], None, "prod", klines=False)
        self.assertAlmostEqual(out["realized_r_gross"], -1.2, places=3)


class TestUserTradesPacing(unittest.TestCase):
    """PR #212 review: userTrades requests are paced, a rate limit is retried with backoff, a ban never is."""

    def run_window(self, replies):
        calls = []

        def fake(method, endpoint, params=None, target_env=None, retry_count=0):
            calls.append(params)
            return replies[min(len(calls), len(replies)) - 1]

        budget = {"requests": 0}
        seen = {}
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
                patch("trade_outcomes.time.sleep") as sleep:
            err = to._fetch_window("BTCUSDT", 0, 10, "prod", seen, budget)
        return err, calls, sleep, seen

    def test_rate_limit_retried_then_succeeds(self):
        err, calls, sleep, seen = self.run_window([{"code": -1003, "msg": "Too many requests"},
                                                   [{"id": 1, "time": 5}]])
        self.assertIsNone(err)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0])
        self.assertIn("1", seen)

    def test_ban_never_retried(self):
        for reply in ({"error": "HTTP 418", "http_code": 418},
                      {"code": -1003, "msg": "Way too many requests; IP banned"}):
            err, calls, sleep, _ = self.run_window([reply, [{"id": 1, "time": 5}]])
            self.assertIsNotNone(err)
            self.assertEqual(len(calls), 1)
            sleep.assert_not_called()

    def test_rate_limit_gives_up_after_max_tries(self):
        err, calls, _, _ = self.run_window([{"code": -1003, "msg": "Too many requests"}])
        self.assertIsNotNone(err)
        self.assertEqual(len(calls), to.USER_TRADES_MAX_TRIES)


if __name__ == "__main__":
    unittest.main()
