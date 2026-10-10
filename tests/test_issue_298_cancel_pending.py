#!/usr/bin/env python3
"""
test_issue_298_cancel_pending.py - Offline tests for issue #298 item 3.

1. --cancel-pending --symbol S [--entry-id N]: the shared core cancel_pending_record cancels the exchange order, then
   the pre-armed stop, then pops the registry record; a position refuses (exit 2), any failure keeps the record.
2. protect_pending_entries / the position guardian cancel an unfilled resting entry whose planned SL the last price
   has crossed (pending_sl_crossed_cancel); an unreadable price is a warning only.
3. pre_trade_guard auto-allows the exact one-symbol shape without dossier or confirmation; post_trade_sync treats it
   as non-opening (ledger sync, no orphan auto-heal).

No network: every exchange call is faked (send_signed_request, uses_mcp_gateway) and every file goes to a temp dir.
"""

import io
import os
import sys
import json
import time
import tempfile
import unittest
import contextlib
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
for p in (SCRIPTS_DIR, os.path.join(SCRIPTS_DIR, "loops"), os.path.join(SCRIPTS_DIR, "hooks"),
          os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import test_guard_bypasses as tgb  # noqa: E402  (module import: its test classes are not collected twice)
import execute_futures_trade as eft  # noqa: E402
import position_guardian_loop as pgl  # noqa: E402

pre_trade_guard = tgb.pre_trade_guard   # the hook modules (scripts/hooks), not the scripts/ bridge of the same name
post_trade_sync = tgb.post_trade_sync
from test_exit_management import FakeExchange, offline, long_position, stop, run_cli, ALGO_ENDPOINT  # noqa: E402
from test_pending_entries import (make_record, write_registry, read_registry, read_jsonl, entry_algo,  # noqa: E402
                                  INTERNAL_ERROR, HEALTHY)

ORDER_ENDPOINT = "/fapi/v1/order"
KEY = "testnet:BTCUSDT:7001"


def limit_entry(order_id=8001, symbol="BTCUSDT", side="BUY", price="101.0", qty="12"):
    return {"orderId": order_id, "symbol": symbol, "side": side, "type": "LIMIT", "price": price, "origQty": qty,
            "reduceOnly": False}


def short_record(**extra):
    """A consistent SHORT resting entry: trigger 101, SL 105 above it, TPs below it."""
    return make_record(direction="SHORT", sl=105.0, tp1=95.0, tp2=90.0, **extra)


def deletes(fake):
    return [(c[1], c[2]) for c in fake.calls if c[0] == "DELETE"]


@contextlib.contextmanager
def no_opening_path():
    """The cancel must never reach the dossier, lease, gates or trade path."""
    boom = AssertionError("opening path reached by --cancel-pending")
    with patch("execute_futures_trade.enforce_evaluation_dossier", side_effect=boom), \
         patch("execute_futures_trade.check_trading_lease", side_effect=boom), \
         patch("execute_futures_trade.check_mechanical_gates", side_effect=boom), \
         patch("execute_futures_trade.execute_complete_trade", side_effect=boom):
        yield


def cli(fake, ws, argv, mcp=False):
    with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=mcp), \
         no_opening_path():
        code, out, _ = run_cli(argv)
    return code, json.loads(out)


# ---------------------------------------------------------------------------------------------
# 1. --cancel-pending CLI and the shared core
# ---------------------------------------------------------------------------------------------
class TestCancelPendingCLI(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def test_stop_market_entry_cancelled_and_record_popped(self):
        fake = FakeExchange([], algos=[entry_algo()])
        write_registry(self.ws, make_record())
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "btcusdt", "--env", "testnet"])
        self.assertEqual(code, 0, res)
        self.assertTrue(res["success"])
        self.assertFalse(res["refused"])
        self.assertEqual(res["cancelled"], [{"key": KEY, "kind": "STOP_MARKET", "entry_id": "7001",
                                             "reason": "cancelled"}])
        self.assertEqual(deletes(fake), [(ALGO_ENDPOINT, {"symbol": "BTCUSDT", "algoId": 7001})])
        self.assertEqual([c for c in fake.calls if c[0] == "POST"], [], "a cancel never places an order")
        self.assertEqual(fake.algos, [])
        self.assertEqual(read_registry(self.ws), {})
        self.assertEqual(read_jsonl(self.ws, "trades_audit.jsonl"), [], "no entry audit record")

    def test_cancel_underscore_alias(self):
        fake = FakeExchange([], algos=[entry_algo()])
        write_registry(self.ws, make_record())
        code, res = cli(fake, self.ws, ["--cancel_pending", "--symbol", "BTCUSDT", "--entry_id", "7001",
                                        "--env", "testnet"])
        self.assertEqual(code, 0, res)
        self.assertEqual(read_registry(self.ws), {})

    def test_limit_entry_and_prearm_cancelled_exchange_first_record_last(self):
        fake = FakeExchange([], algos=[stop(9100, 95.0)], open_orders=[limit_entry()])
        write_registry(self.ws, make_record(kind="LIMIT", entry_id="8001", prearm_algo_id=9100))
        real_update = eft.update_pending_entries
        popped_at = []

        def tracking_update(mutate, base_dir=None):
            popped_at.append(len(fake.calls))
            return real_update(mutate, base_dir)
        with patch("execute_futures_trade.update_pending_entries", side_effect=tracking_update):
            code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 0, res)
        self.assertEqual(deletes(fake), [(ORDER_ENDPOINT, {"symbol": "BTCUSDT", "orderId": 8001}),
                                         (ALGO_ENDPOINT, {"symbol": "BTCUSDT", "algoId": 9100})])
        entry_del = fake.write_index("DELETE", ORDER_ENDPOINT)[0]
        prearm_del = fake.write_index("DELETE", ALGO_ENDPOINT)[0]
        self.assertLess(entry_del, prearm_del, "entry cancelled before its pre-arm")
        self.assertEqual(len(popped_at), 1)
        self.assertGreater(popped_at[0], prearm_del, "the record is popped only after both exchange cancels")
        self.assertTrue(res["results"][0]["prearm_cancelled"])
        self.assertEqual((fake.open_orders, fake.algos), ([], []))
        self.assertEqual(read_registry(self.ws), {})

    def test_filled_entry_refused_nothing_cancelled_or_popped(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[entry_algo()])
        write_registry(self.ws, make_record())
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 2, res)
        self.assertEqual((res["success"], res["refused"], res["reason"]), (False, True, "filled"))
        self.assertIn("--close-position", res["message"])
        self.assertEqual(fake.writes(), [])
        self.assertIn(KEY, read_registry(self.ws))

    def test_partial_limit_fill_refused(self):
        fake = FakeExchange([long_position(amt="4", entry="101.0")], open_orders=[limit_entry()])
        write_registry(self.ws, make_record(kind="LIMIT", entry_id="8001"))
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 2, res)
        self.assertEqual(res["reason"], "filled")
        self.assertEqual(fake.writes(), [])
        self.assertEqual(fake.open_orders, [limit_entry()])
        self.assertIn("testnet:BTCUSDT:8001", read_registry(self.ws))

    def test_entry_not_found_keeps_record_and_prearm_even_when_flat(self):
        # -2011 + a flat positionRisk is not proof of "gone" (positionRisk can lag behind a trigger)
        fake = FakeExchange([], algos=[stop(9100, 95.0)])

        def unknown(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "DELETE" and endpoint == ORDER_ENDPOINT:
                fake.calls.append((method, endpoint, dict(params or {})))
                return {"code": -2011, "msg": "Unknown order sent."}
            return fake(method, endpoint, params, target_env)
        write_registry(self.ws, make_record(kind="LIMIT", entry_id="8001", prearm_algo_id=9100))
        code, res = cli(unknown, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 1, res)
        self.assertEqual((res["success"], res["refused"]), (False, False))
        self.assertEqual(res["results"][0]["reason"], "entry_not_found")
        self.assertIn("--positions", res["error"])
        self.assertIn("--protect-pending", res["error"])
        self.assertEqual(res["cancelled"], [])
        self.assertEqual(deletes(fake), [(ORDER_ENDPOINT, {"symbol": "BTCUSDT", "orderId": 8001})],
                         "no pre-arm DELETE: the closePosition stop stays")
        self.assertEqual([a["algoId"] for a in fake.algos], [9100])
        rec = read_registry(self.ws)["testnet:BTCUSDT:8001"]
        self.assertIsNotNone(rec["missing_since_ts"], "marked for the pending_dropped grace path")

    def test_entry_not_found_keeps_an_earlier_missing_mark(self):
        fake = FakeExchange([], fail_cancel=True)   # DELETE algoOrder -> -2011 Unknown order
        since = int(time.time()) - 30
        write_registry(self.ws, make_record(missing_since_ts=since))
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 1, res)
        self.assertEqual(res["results"][0]["reason"], "entry_not_found")
        self.assertEqual(read_registry(self.ws)[KEY]["missing_since_ts"], since)

    def test_limit_cancel_with_executed_qty_is_a_fill(self):
        fake = FakeExchange([], algos=[stop(9100, 95.0)], open_orders=[limit_entry()], cancel_executed_qty="4")
        write_registry(self.ws, make_record(kind="LIMIT", entry_id="8001", prearm_algo_id=9100))
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 2, res)
        self.assertEqual((res["success"], res["refused"], res["reason"]), (False, True, "filled"))
        self.assertEqual(res["results"][0]["executed_qty"], 4.0)
        self.assertEqual(deletes(fake), [(ORDER_ENDPOINT, {"symbol": "BTCUSDT", "orderId": 8001})],
                         "the pre-arm is kept whatever positionRisk says")
        self.assertEqual([a["algoId"] for a in fake.algos], [9100])
        self.assertIn("testnet:BTCUSDT:8001", read_registry(self.ws))

    def test_failed_exchange_cancel_keeps_record(self):
        fake = FakeExchange([], algos=[entry_algo()])

        def failing(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "DELETE":
                fake.calls.append((method, endpoint, dict(params or {})))
                return dict(INTERNAL_ERROR)
            return fake(method, endpoint, params, target_env)
        write_registry(self.ws, make_record())
        code, res = cli(failing, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 1, res)
        self.assertEqual((res["success"], res["refused"], res["reason"]), (False, False, "failed"))
        self.assertEqual(res["results"][0]["reason"], "cancel_failed")
        self.assertEqual(res["cancelled"], [])
        self.assertIn(KEY, read_registry(self.ws))

    def test_failed_prearm_cancel_keeps_record(self):
        fake = FakeExchange([], algos=[stop(9100, 95.0)], open_orders=[limit_entry()])

        def prearm_fails(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "DELETE" and endpoint == ALGO_ENDPOINT:
                fake.calls.append((method, endpoint, dict(params or {})))
                return dict(INTERNAL_ERROR)
            return fake(method, endpoint, params, target_env)
        write_registry(self.ws, make_record(kind="LIMIT", entry_id="8001", prearm_algo_id=9100))
        code, res = cli(prearm_fails, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 1, res)
        self.assertEqual(res["results"][0]["reason"], "prearm_cancel_failed")
        self.assertTrue(res["results"][0]["entry_cancelled"])
        self.assertIn("testnet:BTCUSDT:8001", read_registry(self.ws))

    def test_registry_lock_failure_keeps_record(self):
        fake = FakeExchange([], algos=[entry_algo()])
        write_registry(self.ws, make_record())
        with patch("execute_futures_trade.update_pending_entries",
                   side_effect=eft.PendingRegistryLockError("lock not acquired")):
            code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 1, res)
        self.assertEqual(res["results"][0]["reason"], "registry_write_failed")
        self.assertIn("lock not acquired", res["results"][0]["message"])
        self.assertTrue(res["results"][0]["entry_cancelled"])
        self.assertIn(KEY, read_registry(self.ws))

    def test_entry_id_selects_one_of_two_records(self):
        fake = FakeExchange([], algos=[entry_algo(7001), entry_algo(7002)])
        write_registry(self.ws, make_record(entry_id="7001"), make_record(entry_id="7002"))
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--entry-id", "7002",
                                        "--env", "testnet"])
        self.assertEqual(code, 0, res)
        self.assertEqual([c["key"] for c in res["cancelled"]], ["testnet:BTCUSDT:7002"])
        self.assertEqual(deletes(fake), [(ALGO_ENDPOINT, {"symbol": "BTCUSDT", "algoId": 7002})])
        self.assertEqual(list(read_registry(self.ws)), [KEY])

    def test_without_entry_id_every_unexpired_record_of_the_symbol(self):
        fake = FakeExchange([], algos=[entry_algo(7001), entry_algo(7002), entry_algo(7003),
                                       entry_algo(7004, symbol="ETHUSDT")])
        write_registry(self.ws, make_record(entry_id="7001"), make_record(entry_id="7002"),
                       make_record(entry_id="7003", expires_in=-1),
                       make_record(entry_id="7004", symbol="ETHUSDT"),
                       make_record(entry_id="7005", env="prod"))
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 0, res)
        self.assertEqual([c["key"] for c in res["cancelled"]], [KEY, "testnet:BTCUSDT:7002"])
        self.assertEqual(sorted(read_registry(self.ws)), ["prod:BTCUSDT:7005", "testnet:BTCUSDT:7003",
                                                          "testnet:ETHUSDT:7004"])

    def test_no_match_and_missing_registry(self):
        fake = FakeExchange([], algos=[entry_algo()])
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"])
        self.assertEqual(code, 1, res)
        self.assertEqual((res["success"], res["reason"]), (False, "no_registry"))
        self.assertIn("error", res)
        write_registry(self.ws, make_record())
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--entry-id", "9999",
                                        "--env", "testnet"])
        self.assertEqual(code, 1, res)
        self.assertEqual((res["success"], res["reason"]), (False, "no_match"))
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "ETHUSDT", "--env", "testnet"])
        self.assertEqual((code, res["reason"]), (1, "no_match"))
        self.assertEqual(fake.writes(), [])
        self.assertIn(KEY, read_registry(self.ws))

    def test_missing_symbol(self):
        fake = FakeExchange([], algos=[entry_algo()])
        write_registry(self.ws, make_record())
        code, res = cli(fake, self.ws, ["--cancel-pending", "--env", "testnet"])
        self.assertEqual(code, 1)
        self.assertEqual(res, {"success": False, "error": "--symbol is required for --cancel-pending"})
        self.assertEqual(fake.calls, [])

    def test_exclusive_with_other_modes_and_direction(self):
        for extra in (["--close-position"], ["--auto-heal"], ["--audit-orphans"], ["--protect-pending"],
                      ["--positions"], ["--move-breakeven"], ["--direction", "LONG"]):
            with patch("execute_futures_trade.cancel_pending_entries") as mock_cancel, \
                 patch("execute_futures_trade.protect_pending_entries") as mock_pp, \
                 patch("execute_futures_trade.close_position_market") as mock_close, \
                 patch("execute_futures_trade.audit_orphan_positions") as mock_audit, \
                 patch("execute_futures_trade.move_sl_to_breakeven") as mock_be, \
                 patch("execute_futures_trade.get_positions_report") as mock_pos, \
                 patch("execute_futures_trade.execute_complete_trade") as mock_trade:
                code, out, _ = run_cli(["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet", *extra])
            self.assertEqual(code, 1, extra)
            self.assertFalse(json.loads(out)["success"], extra)
            for m in (mock_cancel, mock_pp, mock_close, mock_audit, mock_be, mock_pos, mock_trade):
                m.assert_not_called()

    def test_mcp_mode(self):
        fake = FakeExchange([], algos=[entry_algo()])
        write_registry(self.ws, make_record())
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "testnet"], mcp=True)
        self.assertEqual(code, 0, res)
        self.assertEqual(deletes(fake), [(ALGO_ENDPOINT, {"symbol": "BTCUSDT", "algoId": 7001})])
        self.assertEqual(read_registry(self.ws), {})

    def test_prod_needs_no_dossier_lease_or_gate_and_honours_env(self):
        fake = FakeExchange([], algos=[entry_algo()])
        write_registry(self.ws, make_record(env="prod"), make_record(env="testnet"))
        code, res = cli(fake, self.ws, ["--cancel-pending", "--symbol", "BTCUSDT", "--env", "prod"])
        self.assertEqual(code, 0, res)
        self.assertEqual(res["env"], "prod")
        self.assertEqual([c["key"] for c in res["cancelled"]], ["prod:BTCUSDT:7001"])
        self.assertEqual(list(read_registry(self.ws)), [KEY], "the testnet record is untouched")


# ---------------------------------------------------------------------------------------------
# 2. Planned SL crossed before the fill (protect_pending_entries and the guardian)
# ---------------------------------------------------------------------------------------------
class TestPlannedSlCrossedCancel(unittest.TestCase):

    def run_protect(self, fake, *records, dry_run=False, env="testnet"):
        ws = tempfile.mkdtemp()
        write_registry(ws, *records)
        with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
            res = eft.protect_pending_entries(target_env=env, dry_run=dry_run)
        return res, ws

    def types(self, res):
        return [a["type"] for a in res["actions"]]

    def test_pure_rule(self):
        self.assertTrue(eft.planned_sl_crossed(True, 95.0, 95.0))
        self.assertTrue(eft.planned_sl_crossed(True, 95.0, 94.0))
        self.assertFalse(eft.planned_sl_crossed(True, 95.0, 95.1))
        self.assertTrue(eft.planned_sl_crossed(False, 105.0, 105.0))
        self.assertFalse(eft.planned_sl_crossed(False, 105.0, 104.9))
        self.assertFalse(eft.planned_sl_crossed(True, 95.0, 0))
        self.assertFalse(eft.planned_sl_crossed(False, 0, 110.0))

    def test_crossed_short_cancelled(self):
        # OPUSDT-like: SHORT trigger 101, planned SL 105, last price 106 before the fill
        fake = FakeExchange([], algos=[entry_algo(side="SELL")], ticker_price=106.0)
        res, ws = self.run_protect(fake, short_record())
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.types(res), ["pending_sl_crossed_cancel"])
        action = res["actions"][0]
        self.assertTrue(action["success"])
        d = action["detail"]
        self.assertEqual((d["kind"], d["entry_id"], d["direction"], d["sl_price"], d["price"]),
                         ("STOP_MARKET", "7001", "SHORT", 105.0, 106.0))
        self.assertEqual(d["cancel"]["reason"], "cancelled")
        self.assertEqual(deletes(fake), [(ALGO_ENDPOINT, {"symbol": "BTCUSDT", "algoId": 7001})])
        self.assertEqual([c for c in fake.calls if c[0] == "POST"], [])
        self.assertEqual(read_registry(ws), {})

    def test_crossed_long_limit_and_prearm_cancelled(self):
        fake = FakeExchange([], algos=[stop(9100, 95.0)], open_orders=[limit_entry()], ticker_price=94.5)
        res, ws = self.run_protect(fake, make_record(kind="LIMIT", entry_id="8001", prearm_algo_id=9100))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.types(res), ["pending_sl_crossed_cancel"])
        self.assertEqual(deletes(fake), [(ORDER_ENDPOINT, {"symbol": "BTCUSDT", "orderId": 8001}),
                                         (ALGO_ENDPOINT, {"symbol": "BTCUSDT", "algoId": 9100})])
        self.assertTrue(res["actions"][0]["detail"]["cancel"]["prearm_cancelled"])
        self.assertEqual(read_registry(ws), {})

    def test_not_crossed_kept_without_writes(self):
        for rec, price in ((make_record(), 95.1), (short_record(), 104.9)):
            fake = FakeExchange([], algos=[entry_algo(side=rec["entry_side"])], ticker_price=price)
            res, ws = self.run_protect(fake, rec)
            self.assertTrue(res["ok"], res["errors"])
            self.assertEqual(res["actions"], [])
            self.assertNotIn("warnings", res)
            self.assertEqual(fake.writes(), [])
            self.assertIn(KEY, read_registry(ws))

    def test_unreadable_price_is_a_warning_only(self):
        for price in (None, 0):
            fake = FakeExchange([], algos=[entry_algo()], ticker_price=price)
            res, ws = self.run_protect(fake, make_record())
            self.assertTrue(res["ok"], res["errors"])
            self.assertEqual(res["errors"], [])
            self.assertEqual(res["actions"], [])
            self.assertEqual([w["stage"] for w in res["warnings"]], ["sl_crossed_price"])
            self.assertEqual(fake.writes(), [])
            self.assertIn(KEY, read_registry(ws))

    def test_dry_run_only_reports(self):
        fake = FakeExchange([], algos=[entry_algo(side="SELL")], ticker_price=106.0)
        res, ws = self.run_protect(fake, short_record(), dry_run=True)
        self.assertEqual(self.types(res), ["pending_sl_crossed_cancel"])
        self.assertEqual((res["actions"][0]["success"], res["actions"][0]["dry_run"]), (False, True))
        self.assertTrue(res["actions"][0]["detail"]["planned"])
        self.assertEqual(fake.writes(), [])
        self.assertIn(KEY, read_registry(ws))

    def test_failed_cancel_is_an_error_and_keeps_record(self):
        fake = FakeExchange([], algos=[entry_algo(side="SELL")], ticker_price=106.0)

        def failing(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "DELETE":
                fake.calls.append((method, endpoint, dict(params or {})))
                return dict(INTERNAL_ERROR)
            return fake(method, endpoint, params, target_env)
        res, ws = self.run_protect(failing, short_record())
        self.assertFalse(res["ok"])
        self.assertEqual(self.types(res), ["pending_sl_crossed_cancel"])
        self.assertFalse(res["actions"][0]["success"])
        self.assertEqual([e["stage"] for e in res["errors"]], ["sl_crossed_cancel"])
        self.assertIn(KEY, read_registry(ws))

    def test_expired_entry_still_timeout_cancel(self):
        fake = FakeExchange([], algos=[entry_algo(side="SELL")], ticker_price=106.0)
        res, ws = self.run_protect(fake, short_record(expires_in=-1))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.types(res), ["pending_timeout_cancel"])
        self.assertEqual(read_registry(ws), {})

    def test_filled_and_partial_fill_paths_unchanged(self):
        # Filled (entry gone, position open): the planned SL / TPs as before, no crossed cancel
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="101.5")], ticker_price=90.0)
        res, _ = self.run_protect(fake, make_record())
        self.assertEqual(self.types(res), ["pending_protect_sl", "pending_tp_placed"])
        # Partial LIMIT fill (entry still open, position open): never cancelled as "crossed"
        fake = FakeExchange([long_position(amt="4", entry="101.0", mark="101.5")], open_orders=[limit_entry()],
                            ticker_price=90.0)
        res, ws = self.run_protect(fake, make_record(kind="LIMIT", entry_id="8001"))
        self.assertNotIn("pending_sl_crossed_cancel", self.types(res))
        self.assertEqual(fake.write_index("DELETE", ORDER_ENDPOINT), [])
        self.assertIn("testnet:BTCUSDT:8001", read_registry(ws))


class TestGuardianCrossedCancel(unittest.TestCase):

    def run_guardian(self, fake, ws, argv=("--once", "--env", "testnet")):
        log_dir = tempfile.mkdtemp()
        with offline(fake, workspace=ws), patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)), \
             contextlib.redirect_stdout(io.StringIO()):
            code = pgl.main(list(argv))
        with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            state = json.load(f)
        actions_path = os.path.join(log_dir, pgl.ACTIONS_FILE_NAME)
        logged = []
        if os.path.exists(actions_path):
            with open(actions_path, "r", encoding="utf-8") as f:
                logged = [json.loads(line) for line in f if line.strip()]
        return code, state, logged

    def test_guardian_cancels_and_logs_the_action(self):
        fake = FakeExchange([], algos=[entry_algo(side="SELL")], ticker_price=106.0)
        ws = tempfile.mkdtemp()
        write_registry(ws, short_record())
        code, state, logged = self.run_guardian(fake, ws)
        self.assertEqual(code, 0, state["errors"])
        self.assertTrue(state["cycle_ok"])
        self.assertEqual([a["type"] for a in state["actions"]], ["pending_sl_crossed_cancel"])
        self.assertEqual([(a["type"], a["success"], a["detail"]["pending_entry_key"]) for a in logged],
                         [("pending_sl_crossed_cancel", True, KEY)])
        self.assertEqual(read_registry(ws), {})
        self.assertEqual([a for a in state["actions"] if a["type"] == "unknown_resting_entry"], [])

    def test_guardian_entry_not_found_is_a_warning_then_dropped_after_grace(self):
        # SL crossed and the entry triggered at the same moment: the cancel gets -2011
        fake = FakeExchange([], algos=[entry_algo(side="SELL")], ticker_price=106.0, fail_cancel=True)
        ws = tempfile.mkdtemp()
        write_registry(ws, short_record())
        code, state, logged = self.run_guardian(fake, ws)
        self.assertEqual(code, 0, state["errors"])
        self.assertTrue(state["cycle_ok"])
        self.assertEqual(state["errors"], [], "no pending_* error stage (check_guardian_alive stays healthy)")
        self.assertEqual([w["stage"] for w in state["pending_warnings"]], ["sl_crossed_entry_not_found"])
        self.assertEqual([(a["type"], a["success"]) for a in logged], [("pending_sl_crossed_cancel", False)])
        self.assertEqual(logged[0]["detail"]["cancel"]["reason"], "entry_not_found")
        rec = read_registry(ws)[KEY]
        self.assertIsNotNone(rec["missing_since_ts"])
        # Still gone without a position after the grace period: pending_dropped removes the record
        fake.algos = []
        rec["missing_since_ts"] = int(time.time()) - eft.PENDING_MISSING_GRACE_SECONDS - 1
        write_registry(ws, rec)
        code, state, logged = self.run_guardian(fake, ws)
        self.assertEqual(code, 0, state["errors"])
        self.assertEqual([a["type"] for a in state["actions"]], ["pending_dropped"])
        self.assertEqual(read_registry(ws), {})

    def test_guardian_unreadable_price_warning_cycle_ok(self):
        fake = FakeExchange([], algos=[entry_algo()], ticker_price=None)
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record())
        code, state, logged = self.run_guardian(fake, ws)
        self.assertEqual(code, 0, state["errors"])
        self.assertTrue(state["cycle_ok"])
        self.assertEqual(state["errors"], [])
        self.assertEqual([w["stage"] for w in state["pending_warnings"]], ["sl_crossed_price"])
        self.assertEqual(logged, [])
        self.assertIn(KEY, read_registry(ws))

    def test_guardian_dry_run(self):
        fake = FakeExchange([], algos=[entry_algo(side="SELL")], ticker_price=106.0)
        ws = tempfile.mkdtemp()
        write_registry(ws, short_record())
        _, state, _ = self.run_guardian(fake, ws, argv=("--once", "--env", "testnet", "--dry-run"))
        self.assertEqual([(a["type"], a["success"], a["dry_run"]) for a in state["actions"]],
                         [("pending_sl_crossed_cancel", False, True)])
        self.assertEqual(fake.writes(), [])
        self.assertIn(KEY, read_registry(ws))


# ---------------------------------------------------------------------------------------------
# 3. Hooks
# ---------------------------------------------------------------------------------------------
class TestCancelPendingHook(tgb.GuardHarness):

    E = "scripts/execute_futures_trade.py"

    def decisions(self, command_line):
        claude = self.run_guard({"tool_name": "Bash", "tool_input": {"command": command_line}})
        return (self.agy(self.cmd(command_line)).get("decision"),
                claude.get("hookSpecificOutput", {}).get("permissionDecision",
                                                         "deny" if claude.get("__exit_code__") == 2 else "ask"))

    def ps(self, command_line):
        return pre_trade_guard.evaluate_powershell_command(command_line, self.root, self.root, None)[0]

    def test_auto_allowed_without_dossier_or_confirmation(self):
        self.assertFalse(os.path.exists(self.dossier_path))
        for args in ("--cancel-pending --symbol OPUSDT",
                     "--cancel-pending --symbol OPUSDT --entry-id 4000001958599102",
                     "--cancel-pending --symbol OPUSDT --entry-id 4000001958599102 --env prod --json",
                     "--cancel_pending --symbol=OPUSDT --entry_id=17 --env testnet"):
            c = f"python3 {self.E} {args}"
            self.assertEqual(self.decisions(c), ("allow", "allow"), c)
            self.assertEqual(self.ps(c), "allow", c)

    def test_opening_flags_go_to_the_trade_gates(self):
        for flags in ("--direction SHORT", "--confirmed", "--leverage 5", "--dir SHORT", "--is-yolo"):
            c = f"python3 {self.E} --cancel-pending --symbol OPUSDT {flags}"
            self.assertNotIn("allow", self.decisions(c), c)
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)

    def test_not_the_sanctioned_shape_asks(self):
        for args in ("--cancel-pend --symbol OPUSDT", "--cancel-pending --symbol OPUSDT --symbol XPLUSDT",
                     "--cancel-pending", "--cancel-pending --symbol OPUSDT --entry-id",
                     "--cancel-pending --symbol OPUSDT --entry-id -5",
                     "--close-position --symbol OPUSDT --entry-id 5",
                     "--cancel-pending=1 --symbol OPUSDT", "--cancel-pending --symbol OPUSDT extra"):
            c = f"python3 {self.E} {args}"
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "ask", c)
            self.assertNotEqual(self.ps(c), "allow", c)

    def test_flag_tables(self):
        self.assertTrue({"--cancel-pending", "--cancel_pending"} <= pre_trade_guard.EXECUTOR_RISK_FLAGS)
        self.assertIn("--cancel-pending", pre_trade_guard.GROUND_TRUTH_FILES["logs/pending_entries.json"])
        self.assertTrue({"--cancel-pending", "--cancel_pending"} <= post_trade_sync.EXECUTOR_NON_OPENING_FLAGS)

    @patch("post_trade_sync.subprocess.run")
    def test_post_trade_sync_runs_without_orphan_audit(self, mock_run):
        os.makedirs(os.path.join(self.root, "scripts"), exist_ok=True)
        open(os.path.join(self.root, "scripts", "sync_session_state.py"), "w").close()
        with patch("execute_futures_trade.audit_orphan_positions") as mock_audit, \
                patch("post_trade_sync.find_workspace_root", return_value=self.root):
            for c in ("python3 scripts/execute_futures_trade.py --cancel-pending --symbol OPUSDT --env testnet",
                      "python3 scripts/execute_futures_trade.py --cancel_pending --symbol OPUSDT --entry-id 7 "
                      "--env testnet"):
                res = post_trade_sync.handle_post_trade_sync(self.cmd(c))
                self.assertTrue(res["order_placed"], c)
                self.assertFalse(res["is_opening"], c)
                self.assertTrue(res["sync_attempted"], c)
            mock_audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
