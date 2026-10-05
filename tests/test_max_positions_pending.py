#!/usr/bin/env python3
"""
test_max_positions_pending.py - Offline tests for Issue #38: the max_open_positions gate (Gate 0A) counts pending
resting entries.

Committed slots = open positions (logs/session_state.json) + symbols with a pending resting entry for the SAME env
in logs/pending_entries.json that have no open position yet (a partially filled LIMIT is counted once). A missing
registry counts zero; an unreadable one fails closed in PROD. The check runs before any exchange write.

No network: every exchange call is faked and every file goes to a temp directory (never the real logs/).
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
from test_pending_entries import make_record, write_registry, ExecutorHarness, EX_PROFILE, ORDER_ENDPOINT

GATE_PREFIX = "MECHANICAL HARD GATE REJECTION: Max open positions limit"


def write_session_state(ws, symbols):
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    state = {
        "is_valid": True,
        "last_updated_ts": int(time.time()),
        "portfolio_exposure": {"delta_bias": "NEUTRAL", "total_active_positions": len(symbols)},
        "active_positions": [{"symbol": s} for s in symbols],
    }
    with open(os.path.join(ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f)


def write_corrupt_registry(ws):
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    with open(os.path.join(ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
        f.write("{not json")


class GateHarness(unittest.TestCase):
    """check_mechanical_gates / check_max_open_positions against a temp workspace."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws, True)

    def gates(self, max_open=3, env="testnet", is_yolo=False):
        prof = {"max_open_positions": max_open, "yolo_slot_enabled": True, "leverage_standard": 3,
                "leverage_yolo": 15, "risk_pct_equity": 0.005}
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("user_profile.load_user_profile", return_value=prof):
            return eft.check_mechanical_gates(direction="LONG", cur_price=100.0, sl_price=98.0, tp1_price=105.0,
                                              total_qty=1.0, leverage=3, target_env=env, is_yolo=is_yolo)


class TestGate0ACountsPendingEntries(GateHarness):

    def test_open_plus_pending_reaching_max_is_rejected_with_counts(self):
        write_session_state(self.ws, ["SYM0USDT", "SYM1USDT"])
        write_registry(self.ws, make_record(symbol="ETHUSDT", env="testnet"))
        ok, reason = self.gates(max_open=3)
        self.assertFalse(ok)
        self.assertIn("Max open positions limit (3) reached", reason)
        self.assertIn("(open 2 + pending 1 >= max 3)", reason)

    def test_open_plus_pending_below_max_is_accepted(self):
        write_session_state(self.ws, ["SYM0USDT"])
        write_registry(self.ws, make_record(symbol="ETHUSDT", env="testnet"))
        ok, reason = self.gates(max_open=3)
        self.assertTrue(ok, reason)
        self.assertIsNone(reason)

    def test_pending_only_reaching_max_is_rejected(self):
        write_registry(self.ws, make_record(symbol="ETHUSDT", entry_id="1"),
                       make_record(symbol="BNBUSDT", entry_id="2", kind="LIMIT"))
        ok, reason = self.gates(max_open=2)
        self.assertFalse(ok)
        self.assertIn("(open 0 + pending 2 >= max 2)", reason)

    def test_pending_record_of_another_env_is_ignored(self):
        write_session_state(self.ws, ["SYM0USDT"])
        write_registry(self.ws, make_record(symbol="ETHUSDT", entry_id="1", env="testnet"),
                       make_record(symbol="BNBUSDT", entry_id="2", env="prod"))
        ok, reason = self.gates(max_open=2)
        self.assertFalse(ok)
        self.assertIn("(open 1 + pending 1 >= max 2)", reason)   # the prod record is not counted on testnet
        ok, reason = self.gates(max_open=3)
        self.assertTrue(ok, reason)

    def test_partially_filled_limit_is_counted_once(self):
        # ETHUSDT has both an open position (partial fill) and its pending LIMIT record: one slot.
        write_session_state(self.ws, ["SYM0USDT", "ETHUSDT"])
        write_registry(self.ws, make_record(kind="LIMIT", symbol="ETHUSDT", entry_id="1"))
        ok, reason = self.gates(max_open=3)
        self.assertTrue(ok, reason)
        write_registry(self.ws, make_record(kind="LIMIT", symbol="ETHUSDT", entry_id="1"),
                       make_record(symbol="BNBUSDT", entry_id="2"))
        ok, reason = self.gates(max_open=3)
        self.assertFalse(ok)
        self.assertIn("(open 2 + pending 1 >= max 3)", reason)

    def test_pending_yolo_record_counts_against_the_limit(self):
        # No separate YOLO slot limit exists: a pending YOLO entry takes a max_open_positions slot like any other.
        write_session_state(self.ws, ["SYM0USDT", "SYM1USDT"])
        write_registry(self.ws, make_record(symbol="PEPEUSDT", is_yolo=True, leverage=15))
        ok, reason = self.gates(max_open=3, is_yolo=True)
        self.assertFalse(ok)
        self.assertIn("(open 2 + pending 1 >= max 3)", reason)

    def test_unreadable_registry_fails_closed_in_prod(self):
        write_corrupt_registry(self.ws)
        ok, reason = self.gates(max_open=3, env="prod")
        self.assertFalse(ok)
        self.assertIn(GATE_PREFIX + " (3)", reason)
        self.assertIn("FAIL-CLOSED", reason)
        self.assertIn("pending entries registry unreadable", reason)

    def test_malformed_registry_fails_closed_in_prod(self):
        os.makedirs(os.path.join(self.ws, "logs"), exist_ok=True)
        with open(os.path.join(self.ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": []}, f)
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws):
            ok, reason = eft.check_max_open_positions({"max_open_positions": 3}, "prod")
        self.assertFalse(ok)
        self.assertIn("FAIL-CLOSED", reason)
        self.assertIn("malformed", reason)

    def test_unreadable_registry_counts_zero_on_testnet(self):
        write_session_state(self.ws, ["SYM0USDT"])
        write_corrupt_registry(self.ws)
        ok, reason = self.gates(max_open=3)
        self.assertTrue(ok, reason)

    def test_missing_registry_keeps_previous_behaviour(self):
        write_session_state(self.ws, ["SYM0USDT", "SYM1USDT"])
        ok, reason = self.gates(max_open=3)
        self.assertTrue(ok, reason)
        write_session_state(self.ws, ["SYM0USDT", "SYM1USDT", "SYM2USDT"])
        ok, reason = self.gates(max_open=3)
        self.assertFalse(ok)
        self.assertIn("Max open positions limit (3) reached", reason)
        self.assertIn("(open 3 + pending 0 >= max 3)", reason)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "logs", "pending_entries.json")))


class TestGate0ABeforeAnyWrite(ExecutorHarness):
    """execute_complete_trade rejects on committed slots before setup_margin_and_leverage or any order."""

    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, self.ws, True)

    def run_trade(self, max_open, env="testnet", **kwargs):
        # ExecutorHarness.execute loads test_pending_entries.EX_PROFILE: override its limit for this call only.
        with patch.dict(EX_PROFILE, {"max_open_positions": max_open}), \
             patch("execute_futures_trade.check_guardian_alive", return_value=(True, "alive")):
            return self.execute(env=env, **kwargs)

    def test_rejected_before_any_exchange_write(self):
        write_session_state(self.ws, ["SYM0USDT"])
        write_registry(self.ws, make_record(symbol="ETHUSDT", entry_id="1"),
                       make_record(symbol="BNBUSDT", entry_id="2", kind="LIMIT"))
        # check_mechanical_gates is mocked to pass by the harness: the rejection must come from the pre-write check.
        res = self.run_trade(max_open=3)
        self.assertFalse(res["success"])
        self.assertTrue(res["hard_gate_rejection"])
        self.assertIn("Max open positions limit (3) reached (open 1 + pending 2 >= max 3)", res["error"])
        self.assertEqual(self.writes(), [], "no margin/leverage/order write may happen on a Gate 0A rejection")

    def test_rejected_before_any_exchange_write_in_prod(self):
        write_session_state(self.ws, ["SYM0USDT", "SYM1USDT"])
        write_registry(self.ws, make_record(symbol="ETHUSDT", entry_id="1", env="prod"))
        res = self.run_trade(max_open=3, env="prod")
        self.assertFalse(res["success"])
        self.assertTrue(res["hard_gate_rejection"])
        self.assertIn("(open 2 + pending 1 >= max 3)", res["error"])
        self.assertEqual(self.writes(), [])

    def test_resting_entries_placed_by_the_executor_take_slots(self):
        # No open position; every resting LIMIT entry registered by the executor counts on the next entry.
        res1 = self.run_trade(max_open=2, symbol="SOLUSDT", order_type="LIMIT", limit_price=98.767)
        self.assertTrue(res1["success"], res1.get("error"))
        self.assertTrue(res1["pending_limit_entry"])
        res2 = self.run_trade(max_open=2, symbol="ETHUSDT", order_type="LIMIT", limit_price=98.767)
        self.assertTrue(res2["success"], res2.get("error"))
        n_writes = len(self.writes())
        res3 = self.run_trade(max_open=2, symbol="BNBUSDT", order_type="LIMIT", limit_price=98.767)
        self.assertFalse(res3["success"])
        self.assertIn("(open 0 + pending 2 >= max 2)", res3["error"])
        self.assertEqual(len(self.writes()), n_writes, "the rejected entry must not write to the exchange")
        self.assertEqual([c for c in self.writes() if c[2].get("symbol") == "BNBUSDT"], [])
        self.assertEqual(len([c for c in self.writes() if c[1] == ORDER_ENDPOINT]), 2)

    def test_accepted_when_committed_slots_below_max(self):
        write_session_state(self.ws, ["SYM0USDT"])
        write_registry(self.ws, make_record(symbol="ETHUSDT", entry_id="1"))
        res = self.run_trade(max_open=3)
        self.assertTrue(res["success"], res.get("error"))


class TestGate0ATestsNeverTouchRealLogs(unittest.TestCase):
    """
    Guard: the Gate 0A tests (this module) and the executor tests that reach check_max_open_positions use a temp
    workspace, so they never create the real logs/pending_entries.json / logs/session_state.json (the desk machine
    trades live). A real file that already exists is not checked (a live sync may legitimately rewrite it).
    """

    REAL_FILES = [os.path.join(BASE_DIR, "logs", name) for name in ("pending_entries.json", "session_state.json")]
    ISOLATED_TESTS = [
        "test_max_positions_pending.TestGate0ACountsPendingEntries",
        "test_max_positions_pending.TestGate0ABeforeAnyWrite",
        "test_execute_futures_hardening.TestLeverageCeilingGates",
        "test_execute_futures_hardening.TestDynamicEquityRiskGate",
        "test_execute_futures_hardening.TestAutoDestructAndFailSafe.test_execute_complete_trade_aborts_when_sl_unconfirmed",
        "test_execute_futures_hardening.TestRestingLimitOrders",
        "test_execute_futures_hardening.TestCLIEntryPoint.test_execute_complete_trade_fails_closed_when_leverage_fails",
        "test_executor_gates.TestIsolatedMarginFailClosed.test_execute_aborts_without_any_order_when_margin_fails",
        "test_executor_gates.TestLiquidationGate.test_mechanical_gates_include_liquidation_gate",
        "test_executor_gates.TestLiquidationGateUsesEffectiveLeverage",
        "test_executor_gates.TestEntryBasedRiskGates",
        "test_executor_gates.TestExecutorSizesAtEffectiveEntry",
        "test_executor_gates.TestLeverageCeilingSingleSource.test_executor_gate_follows_profile_ceiling",
        "test_executor_gates.TestLeverageCeilingSingleSource.test_yolo_leverage_capped_by_profile",
        "test_issue_10_user_profile_harness.TestMechanicalGatesProfileEnforcement",
        "test_pre_trade_guard_hardening.TestPreTradeGuardHardening.test_execute_futures_trade_stale_session_state_rejected_in_prod",
        "test_pre_trade_guard_hardening.TestPreTradeGuardHardening.test_execute_futures_trade_invalid_session_state_rejected_in_prod",
    ]

    def test_isolated_tests_do_not_create_real_log_files(self):
        watched = [p for p in self.REAL_FILES if not os.path.exists(p)]
        if not watched:
            self.skipTest("real logs/pending_entries.json and logs/session_state.json already exist")
        suite = unittest.defaultTestLoader.loadTestsFromNames(self.ISOLATED_TESTS)
        self.assertGreater(suite.countTestCases(), 30)
        result = unittest.TestResult()
        with patch("builtins.print"):
            suite.run(result)
        problems = [f"{t.id()}: {tb.strip().splitlines()[-1]}" for t, tb in result.errors + result.failures]
        self.assertTrue(result.wasSuccessful(), problems)
        for path in watched:
            self.assertFalse(os.path.exists(path), f"an isolated test created the real {path}")


if __name__ == "__main__":
    unittest.main()
