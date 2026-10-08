#!/usr/bin/env python3
"""
test_position_guardian.py - Offline tests for scripts/loops/position_guardian_loop.py (no network, no orders).

- --once --dry-run sends no write request (orphan, tighter structural stop and dead alpha all present).
- Orphan positions are healed with a verified stop; a failed heal closes reduce-only (never opens).
- An exception on one position does not stop the others; a network failure is logged, not raised.
- State is written to guardian_state.json and actions appended to guardian_actions.jsonl.
"""

import io
import os
import sys
import json
import tempfile
import unittest
import contextlib
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
LOOPS_DIR = os.path.join(SCRIPTS_DIR, "loops")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, LOOPS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import position_guardian_loop as pgl
from test_exit_management import FakeExchange, offline, long_position, stop, structural, ALGO_ENDPOINT

HEALTHY = {"status": "HEALTHY_MOMENTUM", "range_pct": 1.2, "recommendation": "HOLD", "message": "ok"}
STALLED = {"status": "DEAD_ALPHA_STALLED", "range_pct": 0.2, "recommendation": "CLOSE_OR_PROTECT", "message": "stalled"}


# Shared holding-time verdict (utils/position_timing, issue #92): the guardian only reports/closes DEAD_ALPHA_STALLED
# when the 15m stall AND this verdict agree. Real-path coverage lives in tests/test_issue_92_holding_time.py.
OVERDUE_STAGNANT = {"verdict": "DEAD_ALPHA", "is_dead_alpha": True, "elapsed_hours": 5.0, "entry_time_source": "userTrades"}


@contextlib.contextmanager
def guardian_env(fake, structural_result=None, dead_alpha=HEALTHY, holding=None):
    log_dir = tempfile.mkdtemp()
    with contextlib.ExitStack() as stack:
        stack.enter_context(offline(fake))
        stack.enter_context(patch.object(pgl, "DEFAULT_LOG_DIR", log_dir))
        stack.enter_context(patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural_result))
        stack.enter_context(patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(dead_alpha)))
        if holding is not None:
            stack.enter_context(patch.object(pgl, "holding_verdict", return_value=dict(holding)))
        yield log_dir


def run_main(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = pgl.main(argv)
    return code, out.getvalue()


def read_state(log_dir):
    with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
        return json.load(f)


def read_actions(log_dir):
    path = os.path.join(log_dir, pgl.ACTIONS_FILE_NAME)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class TestGuardianDryRun(unittest.TestCase):

    def test_once_dry_run_sends_no_writes(self):
        positions = [long_position("BTCUSDT"), long_position("ETHUSDT")]
        fake = FakeExchange(positions, algos=[stop(501, 95.0, symbol="BTCUSDT")])  # ETHUSDT is an orphan
        with guardian_env(fake, structural_result=structural(102.0), dead_alpha=STALLED, holding=OVERDUE_STAGNANT) as log_dir, \
             patch("execute_futures_trade.close_position_market") as mock_close, \
             patch("execute_futures_trade.emergency_abort_market_close") as mock_abort:
            code, out = run_main(["--once", "--dry-run", "--env", "testnet", "--json", "--close-dead-alpha"])

        self.assertEqual(fake.writes(), [], "dry run must not send any write request")
        mock_close.assert_not_called()
        mock_abort.assert_not_called()
        self.assertEqual(code, 1)  # the orphan stays unprotected in a dry run
        state = json.loads(out)
        self.assertEqual(state, read_state(log_dir))
        for key in ("schema_version", "timestamp", "timestamp_utc", "env", "dry_run", "cycle_ok", "positions", "actions", "errors"):
            self.assertIn(key, state)
        self.assertTrue(state["dry_run"])
        self.assertEqual(state["env"], "testnet")
        by_sym = {p["symbol"]: p for p in state["positions"]}
        self.assertTrue(by_sym["BTCUSDT"]["protected"])
        self.assertEqual(by_sym["BTCUSDT"]["trailing"]["planned_sl"], 102.0)
        self.assertEqual(by_sym["BTCUSDT"]["dead_alpha"]["status"], "DEAD_ALPHA_STALLED")
        self.assertFalse(by_sym["ETHUSDT"]["protected"])
        types = sorted({a["type"] for a in state["actions"]})
        self.assertEqual(types, ["dead_alpha_close", "orphan_heal", "trail_stop"])
        self.assertTrue(all(a["dry_run"] and not a["success"] for a in state["actions"]))
        self.assertEqual(len(read_actions(log_dir)), len(state["actions"]))


class TestGuardianLive(unittest.TestCase):

    def test_orphan_healed_with_verified_stop_and_trailing_tightens(self):
        positions = [long_position("BTCUSDT"), long_position("ETHUSDT")]
        fake = FakeExchange(positions, algos=[stop(501, 95.0, symbol="BTCUSDT")])
        with guardian_env(fake, structural_result=structural(102.0)) as log_dir:
            code, _ = run_main(["--once", "--env", "testnet"])
        state = read_state(log_dir)
        self.assertEqual(code, 0, state["errors"])
        self.assertTrue(state["cycle_ok"])
        by_sym = {p["symbol"]: p for p in state["positions"]}
        self.assertTrue(by_sym["ETHUSDT"]["protected"])
        heal = [a for a in state["actions"] if a["type"] == "orphan_heal"]
        self.assertEqual(len(heal), 1)
        self.assertTrue(heal[0]["success"])
        self.assertEqual(heal[0]["symbol"], "ETHUSDT")
        # BTC: tighter structural stop placed before the old one was cancelled
        btc_calls = [i for i, c in enumerate(fake.calls) if c[2].get("symbol") == "BTCUSDT" and c[0] in ("POST", "DELETE")]
        self.assertEqual([fake.calls[i][0] for i in btc_calls], ["POST", "DELETE"])
        self.assertTrue(any(a["type"] == "trail_stop" and a["success"] for a in state["actions"]))
        # Guardian never sends an entry order
        self.assertFalse([c for c in fake.calls if c[1] == "/fapi/v1/order" and c[0] == "POST"])
        self.assertEqual(len(read_actions(log_dir)), len(state["actions"]))

    def test_failed_heal_closes_reduce_only(self):
        fake = FakeExchange([long_position("ETHUSDT")], algos=[], reject_new_stops=True)
        with guardian_env(fake, structural_result=None) as log_dir:
            code, _ = run_main(["--once", "--env", "testnet"])
        state = read_state(log_dir)
        self.assertTrue(any(a["type"] == "orphan_close" and a["success"] for a in state["actions"]))
        orders = [c for c in fake.calls if c[1] == "/fapi/v1/order" and c[0] == "POST"]
        self.assertTrue(orders)
        for _, _, params in orders:
            self.assertEqual(params.get("reduceOnly"), "true")
            self.assertEqual(params.get("type"), "MARKET")
            self.assertEqual(params.get("side"), "SELL")
        self.assertEqual(code, 0)

    def test_yolo_position_not_trailed_before_tp1(self):
        fake = FakeExchange([long_position("PEPEUSDT", leverage="15")], algos=[stop(501, 95.0, symbol="PEPEUSDT")])
        with guardian_env(fake, structural_result=structural(102.0)) as log_dir:
            code, _ = run_main(["--once", "--env", "testnet"])
        state = read_state(log_dir)
        self.assertEqual(code, 0)
        self.assertEqual(state["positions"][0]["trailing"]["reason"], "yolo_before_tp1")
        self.assertEqual(fake.writes(), [])

    def test_dead_alpha_reported_only_by_default(self):
        fake = FakeExchange([long_position("BTCUSDT")], algos=[stop(501, 104.0)])
        with guardian_env(fake, structural_result=structural(102.0), dead_alpha=STALLED, holding=OVERDUE_STAGNANT) as log_dir, \
             patch("execute_futures_trade.close_position_market") as mock_close:
            code, _ = run_main(["--once", "--env", "testnet"])
        mock_close.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(read_state(log_dir)["positions"][0]["dead_alpha"]["status"], "DEAD_ALPHA_STALLED")
        self.assertEqual(fake.writes(), [])

    def test_dead_alpha_closed_with_flag(self):
        fake = FakeExchange([long_position("BTCUSDT")], algos=[stop(501, 104.0)])
        with guardian_env(fake, structural_result=structural(102.0), dead_alpha=STALLED, holding=OVERDUE_STAGNANT) as log_dir, \
             patch("execute_futures_trade.close_position_market", return_value={"success": True}) as mock_close:
            run_main(["--once", "--env", "testnet", "--close-dead-alpha"])
        mock_close.assert_called_once_with("BTCUSDT", target_env="testnet")
        self.assertTrue(any(a["type"] == "dead_alpha_close" for a in read_state(log_dir)["actions"]))


class TestGuardianFailSafe(unittest.TestCase):

    def test_exception_on_one_position_does_not_stop_others(self):
        positions = [long_position("BTCUSDT"), long_position("ETHUSDT")]
        fake = FakeExchange(positions, algos=[stop(501, 95.0, symbol="BTCUSDT"), stop(502, 95.0, symbol="ETHUSDT")])
        real = eft.get_open_stop_orders

        def flaky(symbol, exit_side, target_env=None):
            if symbol == "BTCUSDT":
                raise RuntimeError("boom")
            return real(symbol, exit_side, target_env=target_env)

        with guardian_env(fake, structural_result=structural(102.0)) as log_dir, \
             patch("execute_futures_trade.get_open_stop_orders", side_effect=flaky):
            code, _ = run_main(["--once", "--env", "testnet"])
        state = read_state(log_dir)
        self.assertEqual(code, 1)
        by_sym = {p["symbol"]: p for p in state["positions"]}
        self.assertIn("boom", by_sym["BTCUSDT"]["error"])
        self.assertTrue(by_sym["ETHUSDT"]["protected"])
        self.assertTrue(by_sym["ETHUSDT"]["trailing"]["updated"])
        self.assertEqual([e["symbol"] for e in state["errors"]], ["BTCUSDT"])

    def test_network_failure_is_logged_not_raised(self):
        log_dir = tempfile.mkdtemp()
        with patch("execute_futures_trade.send_signed_request", side_effect=OSError("network unreachable")), \
             patch("execute_futures_trade._workspace_dir", return_value=log_dir), \
             patch.object(pgl, "DEFAULT_LOG_DIR", log_dir):
            code, _ = run_main(["--once", "--env", "testnet"])
        self.assertEqual(code, 1)
        state = read_state(log_dir)
        self.assertFalse(state["cycle_ok"])
        self.assertEqual(state["errors"][0]["stage"], "positions_sync")
        self.assertIn("network unreachable", state["errors"][0]["error"])

    def test_unreadable_orders_never_heal_blindly(self):
        fake = FakeExchange([long_position("BTCUSDT")])
        orig = fake.__call__

        def broken(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v1/openAlgoOrders":
                return {"error": "timeout"}
            return orig(method, endpoint, params, target_env)

        log_dir = tempfile.mkdtemp()
        with offline(broken), patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)):
            code, _ = run_main(["--once", "--env", "testnet"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(read_state(log_dir)["errors"][0]["stage"], "orders_query")

    def test_loop_mode_survives_cycle_exception(self):
        calls = {"n": 0}

        def cycle(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("unexpected")

        def stop_after_two(_):
            if calls["n"] >= 2:
                raise KeyboardInterrupt
        with patch.object(pgl, "run_cycle", side_effect=cycle), patch("position_guardian_loop.time.sleep", side_effect=stop_after_two), \
             patch.object(pgl, "DEFAULT_LOG_DIR", tempfile.mkdtemp()), \
             contextlib.redirect_stderr(io.StringIO()):
            code, _ = run_main(["--interval", "10", "--env", "testnet"])
        self.assertEqual(code, 0)
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
