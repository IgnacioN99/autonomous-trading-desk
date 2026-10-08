#!/usr/bin/env python3
"""
test_issue_192_guardian_excursion_followups.py - Guardian excursion tracking robustness (issue #192).

1. The excursion pass has a total time budget: positions not reached keep their previous record unchanged
   (excursion_skipped_budget) and every protective step ran first.
2. At most one position_closed per (env, symbol, side, entry_ts), even after a failed state write.
3. Consecutive tracker failures are counted per record (fail_count, reset on success) and reported once
   (MEDIUM / P2) at EXCURSION_FAIL_REPORT_AFTER; never in --dry-run. A position_closed emit failure is an
   excursion_warnings entry.
4. trade_excursion.klines_base_url resolves each env once per armed cycle; a config error is a
   "klines_host_fallback: <msg>" warning (state excursion_warnings); unarmed callers are unaffected.
5. Each record names its price source ("last_1m+mark").
Hermetic: FakeExchange, temp log dirs, klines fetch patched, urlopen blocked, reporter mocked.
"""

import json
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
import position_guardian_loop as pgl
from utils import trade_excursion as tx
from test_exit_management import FakeExchange, long_position, stop
from test_issue_106_exit_manager_hardening import long_record
from test_position_guardian import read_actions
from test_guardian_excursion import GuardianExcursionBase, KlineSource


def two_positions():
    return FakeExchange([long_position("BTCUSDT", mark="101.0"), long_position("ETHUSDT", mark="101.0")],
                        algos=[stop(501, 95.0, symbol="BTCUSDT"), stop(502, 95.0, symbol="ETHUSDT")])


class TestPassBudget(GuardianExcursionBase):

    def test_slow_fetch_skips_later_positions_and_keeps_their_record(self):
        prev_eth = {"symbol": "ETHUSDT", "side": "LONG", "entry_price": 100.0, "entry_ts": 1, "first_seen_ts": 1,
                    "last_seen_ts": 5, "peak_price": 107.0, "mfe_r": 1.4, "reference_source": None, "partial": True}
        self.write_state(excursions={"ETHUSDT|LONG": prev_eth})
        for sym in ("BTCUSDT", "ETHUSDT"):  # entry an hour ago: both positions have bars to read
            long_record(self.ws, symbol=sym, sl_price=95.0, timestamp=int(time.time()) - 3600)
        events = []
        real_trail = dem.update_position_to_structural_stop

        def trail(symbol, *a, **kw):
            events.append(("trail", symbol))
            return real_trail(symbol, *a, **kw)

        source = KlineSource(lambda o: (101.0, 99.0), events=events)

        def slow(*a, **kw):
            end = time.monotonic() + 0.2  # offline() turns time.sleep into a no-op: wait on the clock
            while time.monotonic() < end:
                pass
            return source(*a, **kw)

        with patch.object(pgl, "EXCURSION_PASS_BUDGET_SECONDS", 0.1), \
                patch("dynamic_exit_manager.update_position_to_structural_stop", side_effect=trail):
            state = self.cycle(two_positions(), slow)
        self.assertEqual(events, [("trail", "BTCUSDT"), ("trail", "ETHUSDT"), ("klines", "BTCUSDT")])
        btc, eth = state["positions"]
        self.assertNotIn("excursion_skipped_budget", btc)
        self.assertIs(eth["excursion_skipped_budget"], True)
        self.assertEqual(state["excursions"]["ETHUSDT|LONG"], prev_eth)  # unchanged, not marked partial anew
        self.assertIn("BTCUSDT|LONG", state["excursions"])
        self.assertTrue(state["cycle_ok"], state["errors"])
        self.assertTrue(all(v["protected"] for v in state["positions"]))

    def test_budget_is_five_seconds(self):
        self.assertEqual(pgl.EXCURSION_PASS_BUDGET_SECONDS, 5)


class TestPositionClosedDedupe(GuardianExcursionBase):

    def test_failed_state_write_gives_one_record(self):
        long_record(self.ws, sl_price=95.0, timestamp=int(time.time()) - 3600)
        first = self.cycle(FakeExchange([long_position(mark="104.0")], algos=[stop(501, 102.0)]))
        self.assertIn("BTCUSDT|LONG", first["excursions"])
        with patch("position_guardian_loop.atomic_write_json", side_effect=OSError("disk full")):
            second = self.cycle(FakeExchange([], algos=[]))
        self.assertEqual(len(self.closed_actions(second)), 1)
        self.assertEqual(self.read_state()["excursions"], first["excursions"])  # the failed write kept the old state
        third = self.cycle(FakeExchange([], algos=[]))
        self.assertEqual(self.closed_actions(third), [])
        self.assertEqual(third["excursions"], {})
        logged = [r for r in read_actions(self.log_dir) if r["type"] == "position_closed"]
        self.assertEqual(len(logged), 1)

    def test_other_entry_ts_or_env_is_not_a_duplicate(self):
        rec = {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0, "entry_ts": 1000, "initial_risk": 5.0,
               "last_stop_price": 95.0}
        old = {"timestamp": 1, "env": "testnet", "symbol": "BTCUSDT", "type": "position_closed", "dry_run": False,
               "success": True, "detail": dict(rec, entry_ts=999)}
        other_env = dict(old, env="prod", detail=dict(rec))
        with open(os.path.join(self.log_dir, pgl.ACTIONS_FILE_NAME), "w", encoding="utf-8") as f:
            for r in (old, other_env):
                f.write(json.dumps(r) + "\n")
        self.write_state(excursions={"BTCUSDT|LONG": rec})
        state = self.cycle(FakeExchange([], algos=[]))
        self.assertEqual(len(self.closed_actions(state)), 1)

    def test_tail_window(self):
        path = os.path.join(self.log_dir, "tail.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(f"line {i}\n" for i in range(2000)))
        self.assertEqual(pgl._tail_lines(path, 3, block=7), ["line 1997", "line 1998", "line 1999"])
        self.assertEqual(len(pgl._tail_lines(path, 500)), 500)
        self.assertEqual(pgl._tail_lines(os.path.join(self.log_dir, "missing"), 5), [])


class TestFailureEscalation(GuardianExcursionBase):

    def setUp(self):
        super().setUp()
        long_record(self.ws, sl_price=95.0, timestamp=int(time.time()) - 3600)  # bars to read: the fetch runs

    def failing_cycle(self, **kw):
        with patch("report_agent_issue.report_issue") as rep:
            state = self.cycle(FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)]),
                               KlineSource(None, error=OSError("klines down")), **kw)
        return state, rep

    def test_counts_and_reports_once(self):
        self.write_state(excursions={"BTCUSDT|LONG": {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0,
                                                      "peak_price": 104.0, "fail_count": 8}})
        state, rep = self.failing_cycle()
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual((rec["fail_count"], rec["reported"], rec["peak_price"]), (9, False, 104.0))
        rep.assert_not_called()
        state, rep = self.failing_cycle()
        self.assertEqual((state["excursions"]["BTCUSDT|LONG"]["fail_count"],
                          state["excursions"]["BTCUSDT|LONG"]["reported"]), (10, True))
        self.assertEqual(rep.call_count, 1)
        kw = rep.call_args.kwargs
        self.assertEqual((kw["severity"], kw["priority"]), ("MEDIUM", "P2"))
        self.assertIn("BTCUSDT", kw["title"])
        self.assertTrue(state["cycle_ok"])
        self.assertEqual(state["errors"], [])
        state, rep = self.failing_cycle()
        self.assertEqual(state["excursions"]["BTCUSDT|LONG"]["fail_count"], 11)
        rep.assert_not_called()

    def test_success_resets_the_counter(self):
        self.write_state(excursions={"BTCUSDT|LONG": {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0,
                                                      "fail_count": 12, "reported": True}})
        state = self.cycle(FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)]))
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual((rec["fail_count"], rec["reported"]), (0, False))

    def test_failure_from_first_sight_gets_a_minimal_record(self):
        state, _ = self.failing_cycle()
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual((rec["symbol"], rec["side"], rec["entry_price"], rec["fail_count"]),
                         ("BTCUSDT", "LONG", 100.0, 1))
        self.assertTrue(rec["partial"])
        self.assertEqual(rec["price_source"], "last_1m+mark")

    def test_dry_run_never_reports(self):
        self.write_state(excursions={"BTCUSDT|LONG": {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0,
                                                      "fail_count": 9}})
        state, rep = self.failing_cycle(dry_run=True)
        rep.assert_not_called()
        self.assertEqual((state["excursions"]["BTCUSDT|LONG"]["fail_count"],
                          state["excursions"]["BTCUSDT|LONG"]["reported"]), (10, False))

    def test_emit_failure_is_an_excursion_warning(self):
        self.write_state(excursions={"ETHUSDT|LONG": {"symbol": "ETHUSDT", "side": "LONG", "entry_price": 100.0}})
        with patch.object(pgl.GuardianCycle, "_logged_position_closed_keys", side_effect=RuntimeError("boom")):
            state = self.cycle(FakeExchange([], algos=[]))
        self.assertEqual(state["excursion_warnings"],
                         [{"symbol": None, "stage": "position_closed", "warning": "RuntimeError: boom"}])
        self.assertTrue(state["cycle_ok"])
        self.assertEqual(self.read_state()["excursion_warnings"], state["excursion_warnings"])


class TestKlinesHost(unittest.TestCase):

    def tearDown(self):
        tx.reset_klines_host_cache(enabled=False)

    def test_armed_cache_resolves_each_env_once(self):
        with patch("execute_futures_trade.get_client_config", return_value=("k", "s", "https://x.example/")) as cfg:
            tx.reset_klines_host_cache()
            self.assertEqual(tx.klines_base_url("prod"), "https://x.example")
            self.assertEqual(tx.klines_base_url("prod"), "https://x.example")
            tx.klines_base_url("testnet")
            self.assertEqual(cfg.call_count, 2)
            tx.reset_klines_host_cache()
            tx.klines_base_url("prod")
            self.assertEqual(cfg.call_count, 3)

    def test_unarmed_callers_are_unaffected(self):
        with patch("execute_futures_trade.get_client_config", return_value=(None, None, None)) as cfg:
            tx.klines_base_url("prod")
            tx.klines_base_url("prod")
        self.assertEqual(cfg.call_count, 2)
        self.assertEqual(tx.klines_host_warnings(), [])

    def test_config_error_is_a_warning(self):
        with patch("execute_futures_trade.get_client_config", side_effect=RuntimeError("bad .env")):
            self.assertEqual(tx.resolve_klines_host("testnet"),
                             ("https://testnet.binancefuture.com", "klines_host_fallback: RuntimeError: bad .env"))
            tx.reset_klines_host_cache()
            tx.klines_base_url("prod")
        self.assertEqual(tx.klines_host_warnings(), ["klines_host_fallback: RuntimeError: bad .env"])


class TestGuardianKlinesHost(GuardianExcursionBase):

    def test_once_per_cycle_and_warning_in_state(self):
        long_record(self.ws, symbol="BTCUSDT", sl_price=95.0, timestamp=int(time.time()) - 3600)
        long_record(self.ws, symbol="ETHUSDT", sl_price=95.0, timestamp=int(time.time()) - 3600)
        source = KlineSource(lambda o: (101.0, 99.0))
        hosts = []

        def fetch(symbol, interval, start_ms, limit, target_env, timeout=None):
            hosts.append(tx.klines_base_url(target_env))  # what the real fetch_klines_range does first
            return source(symbol, interval, start_ms, limit, target_env, timeout)

        with patch("execute_futures_trade.get_client_config", side_effect=RuntimeError("bad .env")) as cfg:
            state = self.cycle(two_positions(), fetch)
        self.assertEqual(hosts, ["https://testnet.binancefuture.com"] * 2)
        self.assertEqual(cfg.call_count, 1)
        self.assertEqual(state["excursion_warnings"], [{"symbol": None, "stage": "klines_host",
                                                        "warning": "klines_host_fallback: RuntimeError: bad .env"}])
        self.assertTrue(state["cycle_ok"], state["errors"])
        self.assertIsNone(tx._HOST_CACHE)  # disarmed after the cycle


class TestPriceSource(GuardianExcursionBase):

    def test_record_names_its_price_source(self):
        long_record(self.ws, sl_price=95.0, timestamp=int(time.time()) - 3600)
        state = self.cycle(FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)]))
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual(rec["price_source"], tx.PRICE_SOURCE)
        self.assertEqual(tx.PRICE_SOURCE, "last_1m+mark")
        self.assertEqual((rec["fail_count"], rec["reported"]), (0, False))
        doc = pgl.__doc__
        self.assertIn('"price_source": "last_1m+mark"', doc)


if __name__ == "__main__":
    unittest.main()
