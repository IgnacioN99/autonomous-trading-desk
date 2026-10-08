#!/usr/bin/env python3
"""
test_issue_210_191_208_outcomes.py - follow-ups of the trade reconstruction (issues #210, #191, #208).

#210: entry fills matched by side + qty + time window when the audit entry_order_id is the algoId of a STOP_MARKET
      entry (entry_match "side_qty_window"); "no_entry_fill" rows (no legs, never consume a later trade's fills: the
      ENAUSDT case of #191); R basis = entry_vwap; simulator: KLINES_LIMIT 1000 pages with a pause, 429 / 418 backoff,
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

H = 3600_000
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

    def test_entry_vwap_is_the_r_basis(self):
        # Entry fills 100 x 5 and 102 x 5 (VWAP 101, audit entry 100), SL 95, exit 107: R = 6 / 6 = 1.0.
        self.audit(entry_order_id=1)
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 5, T0), fill(2, 1, "BUY", 102, 5, T0 + 1000),
                             fill(3, 2, "SELL", 107, 10, T0 + H)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        t = self.rows()[0]
        self.assertEqual((t["entry_price"], t["entry_vwap"], t["initial_risk"]), (100.0, 101.0, 6.0))
        self.assertAlmostEqual(t["realized_r_gross"], 1.0, places=4)


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
        sleep_patch = patch("exit_policy_sim.time.sleep")
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

        seq = [http(429, "3"), http(418), [[0, "1", "1", "1", "1", "1", MIN - 1]]]
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=seq):
            rows = eps.fetch_range("BTCUSDT", "1m", 0, MIN, "prod")
        self.assertEqual(len(rows), 1)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [3.0, 2.0])
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

    def __init__(self, day_fills):
        self.day_fills = day_fills
        self.day_params = []

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
            return [dict(f) for f in self.day_fills]
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
        with patch.object(sss, "STATE_FILE", os.path.join(tempfile.mkdtemp(), "s.json")):
            err = sss.write_error_state("x", 0, "now", "prod", 0.0)
        self.assertEqual(sorted(err["closed_today_summary"]), sorted(c))


if __name__ == "__main__":
    unittest.main()
