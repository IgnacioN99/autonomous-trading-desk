#!/usr/bin/env python3
"""
test_issue_160_pending_risk_followups.py - Offline tests for the #160 follow-ups of the pending-risk view (#48).

#160.1  doctor: a sync whose atomic write failed (state_write_error) is a failed sync (temporal audit skipped).
#160.2  post-trade sync hook: sync_rc carries the sync exit code (sync_attempted, renamed from synced in #189).
#160.3  guardian: a failed crossed close already filed the P0; the later orphan heal of the same symbol in the same
        cycle still heals/closes but files no second P0.
#160.4  hook: the Max Open Positions deny says how a stale registry record is cleared.
#160.5  sync: registry records with no live order and no open position are listed in resting_mismatches; doctor WARN.
#160.6  hook: in PROD a missing delta_bias_incl_resting with pending same-env records denies like UNKNOWN.
#160.7  executor snapshot and sync read the order listings before positionRisk.
#160.8  TP ids / audit_done saves retry a registry lock error; existing reduce-only TPs are adopted, not duplicated.
#160.9  registry tick_size capped at the exchange tick; deterministic record match (nearest price, then entry_id).
#160.11 evaluator prompt: eval_neg_06 / eval_neg_07 few-shots, delta_bias_incl_resting lines, C1.1 enum, RULE 2.

No network (urlopen is blocked) and every file goes to a temp workspace, never the real logs/.
"""

import io
import os
import re
import sys
import json
import time
import shutil
import tempfile
import unittest
import contextlib
import importlib.util
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
LOOPS_DIR = os.path.join(SCRIPTS_DIR, "loops")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, HOOKS_DIR, LOOPS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import sync_session_state as sss
import trading_doctor
import pre_trade_guard
import position_guardian_loop as pgl
from utils import portfolio_exposure as pe
import test_issue_101_exchange_anchored_gates as t101   # module imports only: their tests are not collected twice
import test_issue_92_holding_time as t92
from test_pending_entries import make_record, write_registry, read_registry, read_jsonl, posts, INTERNAL_ERROR
from test_exit_management import FakeExchange, offline, long_position, stop

LiveExchange = t101.LiveExchange
pos = t101.pos
ORDER_ENDPOINT = "/fapi/v1/order"

_spec = importlib.util.spec_from_file_location("post_trade_sync_issue_160", os.path.join(HOOKS_DIR, "post_trade_sync.py"))
pts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pts)


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def tempdir(test):
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d, True)
    return d


def prod_record(**kw):
    kw.setdefault("env", "prod")
    return make_record(**kw)


def keys_algo(algo_id, symbol, side="BUY", trigger=101.0, qty="12", **extra):
    return dict({"algoId": algo_id, "symbol": symbol, "side": side, "orderType": "STOP_MARKET",
                 "triggerPrice": str(trigger), "quantity": qty, "closePosition": False, "reduceOnly": False}, **extra)


def doctor_harness(test):
    """The t92 doctor harness (temp state file, mocked exchange / sync / watchdog), set up without collecting it."""
    h = t92.TestDoctorTemporalAudit("setUp")
    h.setUp()
    test.addCleanup(h.doCleanups)
    return h


# =============================================================================
# #160.1 doctor: state_write_error is a failed sync
# =============================================================================
class TestDoctorFailedStateWrite(unittest.TestCase):

    def test_state_write_error_is_a_failed_sync(self):
        tmp = tempdir(self)
        with patch.object(sss, "STATE_FILE", os.path.join(tmp, "session_state.json")), \
             patch("sync_session_state.sync_session_state",
                   return_value={"is_valid": True, "state_write_error": "OSError: disk full"}):
            ok, detail, warning = trading_doctor.ensure_fresh_ledger("testnet")
        self.assertFalse(ok)
        self.assertIn("disk full", detail)
        self.assertIsNone(warning)

    def test_doctor_skips_the_temporal_audit(self):
        h = doctor_harness(self)
        code, out, _, _, watchdog = h.run_doctor(sync=MagicMock(return_value={"is_valid": True,
                                                                              "state_write_error": "disk full"}))
        watchdog.assert_not_called()
        self.assertIn("Temporal audit skipped: ledger could not be synced", out)
        self.assertIn("disk full", out)
        self.assertNotIn("ledger synced in-process", out)
        self.assertEqual(code, 0, out)


# =============================================================================
# #160.2 post-trade sync: sync_rc
# =============================================================================
class TestPostTradeSyncRc(unittest.TestCase):

    PAYLOAD = {"toolCall": {"name": "call_mcp_tool", "args": {
        "ServerName": "binance", "ToolName": "futures_usds.newOrder",
        "Arguments": {"symbol": "BTCUSDT", "side": "SELL", "quantity": "0.01", "reduceOnly": True,
                      "env": "testnet"}}}}

    def run_hook(self, proc):
        root = tempdir(self)
        os.makedirs(os.path.join(root, "scripts"))
        with open(os.path.join(root, "scripts", "sync_session_state.py"), "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\n")
        err = io.StringIO()
        with patch.object(pts, "find_workspace_root", return_value=root), \
             patch.object(pts.subprocess, "run", return_value=proc) as run, \
             contextlib.redirect_stderr(err):
            res = pts.handle_post_trade_sync(json.loads(json.dumps(self.PAYLOAD)))
        run.assert_called_once()
        return res, err.getvalue()

    def test_non_zero_exit_is_reported_in_sync_rc(self):
        res, err = self.run_hook(MagicMock(returncode=1))
        self.assertTrue(res["order_placed"])
        self.assertTrue(res["sync_attempted"], "the sync was attempted")
        self.assertEqual(res["sync_rc"], 1)
        self.assertIn("exited 1", err)

    def test_zero_exit_and_non_int_return_code(self):
        self.assertEqual(self.run_hook(MagicMock(returncode=0))[0]["sync_rc"], 0)
        res, _ = self.run_hook(MagicMock())   # a bare mock: returncode is not an int
        self.assertTrue(res["sync_attempted"])
        self.assertIsNone(res["sync_rc"])

    def test_no_order_has_no_sync_rc(self):
        res = pts.handle_post_trade_sync({"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}})
        self.assertEqual((res["sync_attempted"], res["sync_rc"]), (False, None))


# =============================================================================
# #160.3 guardian: one P0 for a crossed close whose heal also fails
# =============================================================================
class TestGuardianSingleP0(unittest.TestCase):

    def setUp(self):
        self.report = MagicMock(return_value={})
        p = patch("report_agent_issue.report_issue", self.report)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def failing_market(fake):
        """Every reduce-only MARKET close is rejected (-1001): neither the crossed close nor a heal-close succeeds."""
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ORDER_ENDPOINT and (params or {}).get("type") == "MARKET":
                fake.calls.append((method, endpoint, dict(params)))
                return dict(INTERNAL_ERROR)
            return fake(method, endpoint, params, target_env)
        return send

    def run_cycle(self, records):
        ws, log_dir = tempdir(self), tempdir(self)
        if records:
            write_registry(ws, *records)
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="94.0")], reject_new_stops=True,
                            reject_response=INTERNAL_ERROR)
        abort = MagicMock(return_value={"confirmed": False, "order": dict(INTERNAL_ERROR)})
        with offline(self.failing_market(fake), workspace=ws), \
             patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
             patch("execute_futures_trade.emergency_abort_market_close", abort), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(t92.HEALTHY)), \
             contextlib.redirect_stdout(io.StringIO()):
            state = pgl.GuardianCycle("testnet", log_dir=log_dir).run()
        return state, abort

    def test_crossed_close_and_failed_heal_report_once(self):
        state, abort = self.run_cycle([make_record(sl=95.0)])
        types = [a["type"] for a in state["actions"]]
        self.assertIn("pending_sl_crossed_close", types)
        self.report.assert_called_once()
        kw = self.report.call_args.kwargs
        self.assertIn("pending_sl_crossed_close", kw["title"])
        self.assertEqual(kw["error_detail"], "BTCUSDT abort not flat or protected at pending_sl_crossed_close")
        # G1: the guardian's heal and its reduce-only close still ran
        abort.assert_called_once()
        heal = [a for a in state["actions"] if a["type"] == "orphan_heal"]
        self.assertEqual(len(heal), 1)
        self.assertEqual(heal[0]["detail"]["reason"], "heal_and_close_failed")

    def test_plain_orphan_heal_failure_still_reports(self):
        state, abort = self.run_cycle([])
        abort.assert_called_once()
        self.report.assert_called_once()
        self.assertIn("heal_orphan_position", self.report.call_args.kwargs["title"])

    def test_crossed_close_detection(self):
        failed = {"type": "pending_sl_crossed_close", "success": False,
                  "detail": {"flat": False, "kept_stops": [], "heal": {"success": False}}}
        self.assertTrue(pgl._crossed_close_reported(failed))
        for other in (dict(failed, success=True),
                      dict(failed, detail=dict(failed["detail"], kept_stops=[{"algo_id": 1}])),
                      dict(failed, detail=dict(failed["detail"], heal={"success": True})),
                      dict(failed, detail={"planned": True}),                       # dry run: nothing reported
                      dict(failed, type="pending_abort")):
            self.assertFalse(pgl._crossed_close_reported(other), other)


# =============================================================================
# #160.4 / #160.6 hook: deny hint and missing delta_bias_incl_resting
# =============================================================================
class TestHookPendingFollowups(unittest.TestCase):

    PROFILE = {"autonomous_execution_tier_s": True, "max_open_positions": 3, "leverage_standard": 3,
               "leverage_yolo": 15, "leverage_ceiling": 15, "yolo_slot_enabled": False}

    def setUp(self):
        self.ws = tempdir(self)
        os.makedirs(os.path.join(self.ws, "logs"))

    def state(self, symbols=("BTCUSDT", "ETHUSDT"), bias="DELTA_BALANCED", env="prod", **portfolio):
        exposure = dict({"total_active_positions": len(symbols), "delta_bias": bias}, **portfolio)
        with open(os.path.join(self.ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
            json.dump({"is_valid": True, "last_updated_ts": int(time.time()), "target_env": env,
                       "active_positions": [{"symbol": s} for s in symbols], "portfolio_exposure": exposure}, f)

    def hook(self, direction="LONG", env="prod"):
        cmd = (f"python3 scripts/execute_futures_trade.py --symbol SOLUSDT --direction {direction} --leverage 3 "
               f"--env {env}")
        cand = {"symbol": "SOLUSDT", "direction": direction, "requires_user_confirmation": False}
        # Issue #207: the candidate has no stored dossier record, so a SHORT would ask for its missing radar snapshot
        # before the delta pre-check under test; that confirmation gate is covered in test_issue_206_squeeze_backstop.
        with patch("user_profile.load_user_profile", return_value=dict(self.PROFILE)), \
             patch("pre_trade_guard.check_dossier", return_value=(True, "ok", cand)), \
             patch("pre_trade_guard._tier_s_calibration_message", return_value=None):
            return pre_trade_guard.evaluate_trade_opening(cmd, {"CommandLine": cmd}, {}, self.ws, None)

    def test_max_open_positions_deny_explains_stale_records(self):
        self.state()
        write_registry(self.ws, prod_record(symbol="XRPUSDT"))
        decision, reason = self.hook()
        self.assertEqual(decision, "deny")
        self.assertIn("Max Open Positions Gate", reason)
        self.assertIn("Active positions (2) + pending resting entries (1)", reason)
        self.assertIn("python3 scripts/execute_futures_trade.py --protect-pending", reason)
        self.assertIn("position guardian", reason)
        self.assertIn("60 s", reason)

    def test_max_open_positions_deny_without_pending_has_no_hint(self):
        self.state(symbols=("BTCUSDT", "ETHUSDT", "ADAUSDT"))
        decision, reason = self.hook()
        self.assertEqual(decision, "deny")
        self.assertNotIn("--protect-pending", reason)

    def test_malformed_registry_deny_says_how_to_recover(self):
        self.state()
        with open(os.path.join(self.ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
            f.write("{broken")
        decision, reason = self.hook()
        self.assertEqual(decision, "deny")
        self.assertIn("FAIL-CLOSED", reason)
        self.assertIn("Repair or restore logs/pending_entries.json", reason)

    def test_prod_missing_key_with_pending_records_denies(self):
        self.state(symbols=("BTCUSDT",), bias="DELTA_BALANCED")   # no delta_bias_incl_resting key
        write_registry(self.ws, prod_record(symbol="XRPUSDT"))
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                decision, reason = self.hook(direction)
                self.assertEqual(decision, "deny")
                self.assertIn("resting-entry exposure is UNKNOWN", reason)
                self.assertIn("delta_bias_incl_resting missing", reason)
                self.assertIn("XRPUSDT", reason)

    def test_prod_missing_key_without_pending_records_falls_back(self):
        self.state(symbols=("BTCUSDT",), bias="DELTA_BALANCED")
        self.assertEqual(self.hook("LONG")[0], "allow")
        write_registry(self.ws, prod_record(symbol="BTCUSDT"),          # on an open position: not pending
                       make_record(symbol="XRPUSDT", env="testnet"))    # other env
        self.assertEqual(self.hook("LONG")[0], "allow")
        self.state(symbols=("BTCUSDT",), bias="LONG_HEAVY")
        self.assertEqual(self.hook("LONG")[0], "deny")                    # still the delta_bias fallback


# =============================================================================
# #160.5 / #160.7 sync: resting_mismatches and listing read order; doctor WARN
# =============================================================================
class TestSyncMismatchesAndReadOrder(unittest.TestCase):

    def setUp(self):
        self.tmp = tempdir(self)

    def sync(self, fake):
        with patch.object(sss, "LOGS_DIR", self.tmp), \
             patch.object(sss, "STATE_FILE", os.path.join(self.tmp, "session_state.json")), \
             patch.object(sss, "AUDIT_LOG", os.path.join(self.tmp, "trades_audit.jsonl")), \
             patch.dict(sys.modules, {"shadow_tracker": None}), \
             patch("execute_futures_trade.send_signed_request", side_effect=fake):
            return sss.sync_session_state(target_env="prod")

    def registry(self, *records):
        entries = {eft.pending_entry_key(r["target_env"], r["symbol"], r["entry_id"]): r for r in records}
        with open(os.path.join(self.tmp, "pending_entries.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2, "entries": entries}, f)

    def test_unmatched_record_is_listed_not_counted(self):
        self.registry(prod_record(entry_id="7001", symbol="BTCUSDT"),                         # resting: counts
                      prod_record(entry_id="555", symbol="ETHUSDT", direction="SHORT"),       # id unknown live
                      prod_record(entry_id="7003", symbol="SOLUSDT"))                         # open position
        fake = LiveExchange(positions=[pos("SOLUSDT", "1", 100), pos("XRPUSDT", "-1", 100)],
                            algos=[keys_algo(7001, "BTCUSDT"), keys_algo(9999, "ETHUSDT", side="SELL")])
        exp = self.sync(fake)["portfolio_exposure"]
        self.assertEqual(exp["resting_entries"], [{"symbol": "BTCUSDT", "dir": "LONG", "kind": "STOP_MARKET"}])
        self.assertEqual(exp["resting_mismatches"], [{"symbol": "ETHUSDT", "entry_id": "555", "side": "SHORT"}])
        self.assertEqual(exp["delta_bias_incl_resting"], "LONG_HEAVY", "the bias stays known")
        with open(os.path.join(self.tmp, "session_state.json"), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["portfolio_exposure"]["resting_mismatches"], exp["resting_mismatches"])

    def test_no_mismatch_and_error_state(self):
        self.registry(prod_record(entry_id="7001", symbol="BTCUSDT"))
        exp = self.sync(LiveExchange(algos=[keys_algo(7001, "BTCUSDT")]))["portfolio_exposure"]
        self.assertEqual(exp["resting_mismatches"], [])
        err = self.sync(LiveExchange(errors={"/fapi/v2/positionRisk": {"code": -1, "msg": "x"}}))
        self.assertIs(err["is_valid"], False)
        self.assertEqual(err["portfolio_exposure"]["resting_mismatches"], [])

    def test_listings_are_read_before_position_risk(self):
        fake = LiveExchange(positions=[pos("SOLUSDT", "1", 100)])
        self.sync(fake)
        gets = [c[1] for c in fake.calls if c[0] == "GET"]
        risk = gets.index("/fapi/v2/positionRisk")
        self.assertLess(gets.index("/fapi/v1/openAlgoOrders"), risk)
        self.assertLess(gets.index("/fapi/v1/openOrders"), risk)
        self.assertEqual(gets.count("/fapi/v1/openAlgoOrders"), 1)
        self.assertEqual(gets.count("/fapi/v1/openOrders"), 1)

    def test_raised_listing_read_is_unknown_and_failed_positions_still_fail_closed(self):
        self.registry(prod_record(entry_id="7001", symbol="BTCUSDT"))
        state = self.sync(LiveExchange(errors={"/fapi/v1/openAlgoOrders": ConnectionError("down")}))
        self.assertIs(state["is_valid"], True)
        self.assertEqual(state["portfolio_exposure"]["delta_bias_incl_resting"], "UNKNOWN")
        state = self.sync(LiveExchange(errors={e: ConnectionError("down") for e in
                                               ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders",
                                                "/fapi/v2/positionRisk")}))
        self.assertIs(state["is_valid"], False)

    def test_doctor_warns_on_mismatches(self):
        mismatch = [{"symbol": "ETHUSDT", "entry_id": "555", "side": "SHORT"}]
        h = doctor_harness(self)
        h.write_state(age_s=10, portfolio_exposure={"resting_mismatches": mismatch})
        code, out, _, _, _ = h.run_doctor()
        self.assertIn("[STATE LEDGER] Pending registry record(s) with no live entry order", out)
        self.assertIn("ETHUSDT#555", out)
        self.assertIn("--protect-pending", out)
        self.assertIn("OPERATIONAL WITH WARNINGS", out)
        self.assertEqual(code, 0, out)
        h.write_state(age_s=10, portfolio_exposure={"resting_mismatches": []})
        self.assertNotIn("Pending registry record(s)", h.run_doctor()[1])

    def test_doctor_ignores_other_env_ledger(self):
        self.assertIsNone(trading_doctor.ledger_resting_mismatch_warning(
            {"target_env": "prod", "portfolio_exposure": {"resting_mismatches": [{"symbol": "X"}]}}, "testnet"))


class TestSnapshotReadOrder(unittest.TestCase):

    def test_order_listings_before_position_risk(self):
        tmp = tempdir(self)
        fake = LiveExchange(positions=[pos("SOLUSDT", "1", 100)])
        with patch("execute_futures_trade._workspace_dir", return_value=tmp), \
             patch("execute_futures_trade.send_signed_request", side_effect=fake):
            snap, err = eft.fetch_live_gate_snapshot("prod")
        self.assertIsNone(err)
        self.assertEqual([c[1] for c in fake.calls],
                         ["/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders", "/fapi/v2/positionRisk"])
        self.assertEqual(snap["exposure"]["symbols"], ["SOLUSDT"])


# =============================================================================
# #160.8 TP ids on a registry lock failure
# =============================================================================
class TestTakeProfitIdsOnLockFailure(unittest.TestCase):

    def setUp(self):
        self.ws = tempdir(self)
        self.lock = {"left": 0}
        self.real_update = eft.update_pending_entries
        self.real_place = eft.place_take_profit_orders
        self.real_audit = eft.append_trade_audit_record

    def locked_update(self, mutate, base_dir=None):
        if self.lock["left"] > 0:
            self.lock["left"] -= 1
            raise eft.PendingRegistryLockError("registry lock not acquired")
        return self.real_update(mutate, base_dir)

    def arm_after(self, real, failures):
        """Calls the real function, then arms `failures` lock errors for the next registry saves."""
        def wrapped(*args, **kwargs):
            res = real(*args, **kwargs)
            self.lock["left"] = failures
            return res
        return wrapped

    def protect(self, send, tp_lock_failures=0, audit_lock_failures=0):
        with offline(send, workspace=self.ws), \
             patch("execute_futures_trade.update_pending_entries", side_effect=self.locked_update), \
             patch("execute_futures_trade.place_take_profit_orders",
                   side_effect=self.arm_after(self.real_place, tp_lock_failures)), \
             patch("execute_futures_trade.append_trade_audit_record",
                   side_effect=self.arm_after(self.real_audit, audit_lock_failures)), \
             patch("provenance_stamp.stamp_trade_record", side_effect=lambda rec, **kw: rec):
            return eft.protect_pending_entries(target_env="testnet")

    @staticmethod
    def exchange(open_orders=None):
        return FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(701, 95.0)],
                            open_orders=open_orders)

    @staticmethod
    def tp_order(order_id, price, side="SELL", reduce_only=True, order_type="LIMIT", qty="3", executed="0"):
        return {"orderId": order_id, "symbol": "BTCUSDT", "side": side, "type": order_type, "price": str(price),
                "origQty": qty, "executedQty": executed, "reduceOnly": reduce_only, "status": "NEW"}

    def test_tp_save_is_retried_on_a_lock_error(self):
        write_registry(self.ws, make_record(sl_qty=10.0))
        fake = self.exchange()
        with self.assertLogs("execute_futures_trade", level="WARNING") as logs:
            res = self.protect(fake, tp_lock_failures=2)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(len([m for m in logs.output if "retried in" in m]), 2)
        self.assertEqual([(t["price"], t["quantity"]) for t in posts(fake, ORDER_ENDPOINT)], [(110.0, 3.0), (120.0, 7.0)])
        tp = [a for a in res["actions"] if a["type"] == "pending_tp_placed"][0]
        self.assertTrue(tp["success"])
        self.assertEqual(len(read_jsonl(self.ws, "trades_audit.jsonl")), 1)
        self.assertEqual(read_registry(self.ws), {})

    def test_audit_done_save_is_retried_on_a_lock_error(self):
        write_registry(self.ws, make_record(sl_qty=10.0, tp_placed=True, tp1_qty=3.0, tp2_qty=7.0,
                                            tp1_order_id=11, tp2_order_id=12))
        with self.assertLogs("execute_futures_trade", level="WARNING"):
            res = self.protect(self.exchange(), audit_lock_failures=1)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(len(read_jsonl(self.ws, "trades_audit.jsonl")), 1)
        self.assertEqual(read_registry(self.ws), {})

    def test_lost_tp_ids_are_adopted_on_the_next_run(self):
        write_registry(self.ws, make_record(sl_qty=10.0))
        fake = self.exchange()
        with self.assertLogs("execute_futures_trade", level="WARNING"):
            res1 = self.protect(fake, tp_lock_failures=3)   # every attempt fails: the ids are not persisted
        self.assertEqual(res1["errors"][0]["stage"], "exception")
        rec = read_registry(self.ws)["testnet:BTCUSDT:7001"]
        self.assertIsNone(rec.get("tp1_order_id"))
        self.assertFalse(rec.get("tp_placed"))
        self.assertEqual(len(posts(fake, ORDER_ENDPOINT)), 2)
        # the two TPs rest on the exchange (plus an unrelated reduce-only LIMIT and a non-reduce-only one)
        fake.open_orders = [self.tp_order(501, 110.0), self.tp_order(502, 120.0, qty="7"), self.tp_order(503, 115.0),
                            self.tp_order(504, 110.0, reduce_only=False)]
        fake.calls = []
        res2 = self.protect(fake)
        self.assertTrue(res2["ok"], res2["errors"])
        self.assertEqual(posts(fake, ORDER_ENDPOINT), [], "no duplicate TP placed")
        tp = [a for a in res2["actions"] if a["type"] == "pending_tp_placed"][0]
        self.assertEqual((tp["detail"]["tp1_order_id"], tp["detail"]["tp2_order_id"]), (501, 502))
        self.assertEqual(tp["detail"]["adopted_existing"], ["tp1_order_id", "tp2_order_id"])
        audit = read_jsonl(self.ws, "trades_audit.jsonl")
        self.assertEqual((audit[-1]["tp1_order_id"], audit[-1]["tp2_order_id"]), (501, 502))
        self.assertEqual(read_registry(self.ws), {})

    def test_only_the_missing_tp_is_adopted(self):
        write_registry(self.ws, make_record(sl_qty=10.0, tp1_qty=3.0, tp2_qty=7.0, tp1_order_id=501))
        fake = self.exchange(open_orders=[self.tp_order(501, 110.0), self.tp_order(502, 120.0, qty="7")])
        res = self.protect(fake)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(posts(fake, ORDER_ENDPOINT), [])
        tp = [a for a in res["actions"] if a["type"] == "pending_tp_placed"][0]
        self.assertEqual((tp["detail"]["tp1_order_id"], tp["detail"]["tp2_order_id"]), (501, 502))
        self.assertEqual(tp["detail"]["adopted_existing"], ["tp2_order_id"])

    def test_reconcile_read_failure_places_as_before_with_a_warning(self):
        write_registry(self.ws, make_record(sl_qty=10.0))
        fake = self.exchange(open_orders=[self.tp_order(501, 110.0)])

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "GET" and endpoint == "/fapi/v1/openOrders" and (params or {}).get("symbol"):
                fake.calls.append((method, endpoint, dict(params)))
                return dict(INTERNAL_ERROR)
            return fake(method, endpoint, params, target_env)
        res = self.protect(send)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual([t["price"] for t in posts(fake, ORDER_ENDPOINT)], [110.0, 120.0])
        self.assertEqual([w["stage"] for w in res["warnings"]], ["tp_reconcile"])
        self.assertEqual(read_registry(self.ws), {})

    def test_find_resting_take_profits_matching(self):
        # Issue #189: half a tick (0.05); 110.06 just outside comes first, 110.04 inside
        orders = [self.tp_order(6, 110.06), self.tp_order(1, 110.04), self.tp_order(2, 110.2),
                  self.tp_order(3, 120.0, side="BUY"), self.tp_order(4, 120.0, order_type="STOP_MARKET"),
                  self.tp_order(5, 120.0)]
        with patch("execute_futures_trade.send_signed_request", return_value=orders):
            found, err = eft.find_resting_take_profits("BTCUSDT", "SELL", {"tp1": 110.0, "tp2": 120.0}, 0.1)
            self.assertIsNone(err)
            self.assertEqual(found, {"tp1": 1, "tp2": 5})   # within half a tick; exit side LIMIT reduce-only only
            found, _ = eft.find_resting_take_profits("BTCUSDT", "SELL", {"tp1": 110.0}, 0.1, exclude_ids=[1])
            self.assertEqual(found, {})
            found, _ = eft.find_resting_take_profits("BTCUSDT", "SELL", {"tp1": 110.0}, None)
            self.assertEqual(found, {"tp1": 1}, "0.05% of price without a tick (0.055), never an exact compare")


# =============================================================================
# #160.9 registry tolerances
# =============================================================================
def quantityless_info(price, side="BUY"):
    return {"symbol": "BTCUSDT", "source": "algo", "kind": "STOP_MARKET", "id": None, "type": "STOP_MARKET",
            "side": side, "price": price, "quantity": None, "executed_qty": None}


class TestRegistryTolerances(t101.Workspace):

    def test_registry_tick_is_capped_at_the_exchange_tick(self):
        rec = prod_record(tick_size=10.0)
        self.assertEqual(eft.record_price_tolerances([rec]), {"BTCUSDT": 5.0})
        self.assertEqual(eft.record_price_tolerances([rec], {"BTCUSDT": 0.01}), {"BTCUSDT": 0.005})
        self.assertEqual(eft.record_price_tolerances([rec], {"ETHUSDT": 0.01}), {"BTCUSDT": 5.0})   # unknown tick
        small = prod_record(tick_size=0.001)
        self.assertEqual(eft.record_price_tolerances([small], {"BTCUSDT": 0.01}), {"BTCUSDT": 0.0005})   # never wider

    def test_nearest_record_wins(self):
        recs = [prod_record(entry_id="9001", total_qty=5.0, trigger_or_limit_price=101.0),
                prod_record(entry_id="9002", total_qty=2.0, trigger_or_limit_price=100.0)]
        legs = pe.resting_opening_legs([quantityless_info(100.25)], recs, price_tol_by_symbol={"BTCUSDT": 1.0})
        self.assertEqual(legs[0]["qty"], 2.0)

    def test_equal_distance_is_broken_by_entry_id(self):
        a = prod_record(entry_id="9001", total_qty=5.0, trigger_or_limit_price=101.0)
        b = prod_record(entry_id="9002", total_qty=2.0, trigger_or_limit_price=101.0)
        for recs in ([a, b], [b, a]):
            legs = pe.resting_opening_legs([quantityless_info(101.25)], recs, price_tol_by_symbol={"BTCUSDT": 0.5})
            self.assertEqual(legs[0]["qty"], 5.0, "9001 < 9002 whatever the registry order")
        legs = pe.resting_opening_legs([quantityless_info(100.5)],
                                       [prod_record(entry_id="9002", total_qty=2.0, trigger_or_limit_price=101.0),
                                        prod_record(entry_id="9001", total_qty=5.0, trigger_or_limit_price=100.0)],
                                       price_tol_by_symbol={"BTCUSDT": 0.5})
        self.assertEqual(legs[0]["qty"], 5.0, "exactly between two records: the lower entry_id")

    def gate(self, rec, **kw):
        self.write_state([])
        write_registry(self.ws, rec)
        fake = LiveExchange(algos=[{"symbol": "BTCUSDT", "side": "BUY", "triggerPrice": 101.3,
                                    "orderType": "STOP_MARKET", "closePosition": False}])
        with patch("execute_futures_trade.check_max_open_positions", return_value=(True, None)):
            return self.gates(fake, "SHORT", **kw)

    def test_gate1_uses_the_capped_tolerance(self):
        rec = prod_record(total_qty=2.0, tick_size=1.0)   # inflated tick: half = 0.5 matches 101.3
        ok, msg = self.gate(rec)
        self.assertTrue(ok, msg)
        ok, msg = self.gate(rec, exchange_ticks={"BTCUSDT": 0.01})
        self.assertFalse(ok)
        self.assertIn("delta-neutral gate cannot measure the portfolio", msg)

    def test_executor_passes_the_ordered_symbols_tick(self):
        self.write_state([])
        with patch("execute_futures_trade.check_mechanical_gates", return_value=(False, "stub rejection")) as gates:
            res = self.execute(LiveExchange(), env="prod")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "stub rejection")
        self.assertEqual(gates.call_args.kwargs["exchange_ticks"], {"SOLUSDT": t101.EX_FILTERS["tickSize"]})


# =============================================================================
# #160.11 evaluator prompt
# =============================================================================
class TestEvaluatorPrompt(unittest.TestCase):

    SOURCE = os.path.join(BASE_DIR, ".agents", "agents", "isolated_market_evaluator", "agent.md")
    GENERATED = os.path.join(BASE_DIR, ".claude", "agents", "isolated_market_evaluator.md")

    @classmethod
    def setUpClass(cls):
        with open(cls.SOURCE, "r", encoding="utf-8") as f:
            cls.text = f.read()

    def example(self, example_id):
        return self.text.split(f'<example id="{example_id}">')[1].split("</example>")[0]

    def test_pending_unreadable_few_shot(self):
        shot = self.example("eval_neg_06_pending_unreadable_abort")
        self.assertRegex(shot, r"- \[x\] C1\.1 .*-> UNREADABLE")
        self.assertRegex(shot, r"- \[x\] C1\.2 .*-> BOTH")
        self.assertRegex(shot, r"- \[ \] \S+ \S+ K1 .*-> BLOCKED")
        self.assertRegex(shot, r"- \[ \] C4\.2 .*-> REJECTED")
        self.assertIn('"status": "REJECTED"', shot)
        self.assertIn('"approved_candidates": []', shot)

    def test_resting_entries_long_heavy_few_shot(self):
        shot = self.example("eval_neg_07_resting_entries_long_heavy")
        self.assertIn("delta_bias DELTA_BALANCED", shot)
        self.assertRegex(shot, r"- \[x\] C1\.1 Portfolio delta_bias_incl_resting: LONG_HEAVY .*-> LONG_HEAVY")
        self.assertRegex(shot, r"- \[ \] \S+ LONG K1 .*-> BLOCKED")
        self.assertIn('"status": "REJECTED"', shot)

    def test_checklist_lines_use_delta_bias_incl_resting_and_the_emitted_enum(self):
        self.assertNotIn("C1.1 Portfolio delta_bias:", self.text)
        finals = re.findall(r"<final_response>([\s\S]*?)</final_response>", self.text)
        self.assertEqual(len(re.findall(r"C1\.1 Portfolio delta_bias_incl_resting:", self.text)), len(finals))
        for line in re.findall(r"- \[x\] C1\.1 .*", self.text):
            self.assertRegex(line, r"-> (LONG_HEAVY|SHORT_HEAVY|DELTA_BALANCED|NEUTRAL|UNREADABLE)$")
        enum = next(line for line in self.text.splitlines() if line.strip().startswith("- C1.1 Portfolio delta"))
        for value in ("LONG_HEAVY", "SHORT_HEAVY", "DELTA_BALANCED", "NEUTRAL", "UNREADABLE"):
            self.assertIn(value, enum)
        self.assertNotIn("/ BALANCED /", enum)
        self.assertNotIn("FLAT", enum)

    def test_rule_2_book_bullet(self):
        rule2 = self.text.split("- RULE 2")[1].split("- RULE 3")[0]
        self.assertIn("`state_sync: FAILED`", rule2)
        self.assertIn("= C1.2 BOTH, K1 BLOCKED", rule2)
        self.assertNotIn("C1 FAIL", self.text)
        # Issue #189: the brief always emits both keys; a missing one fails closed (no longer "absent means OK")
        self.assertNotIn("key means OK", rule2)
        self.assertIn("a MISSING key (a brief from an older run) counts as the bad value", rule2)

    def test_generated_copy_is_in_sync(self):
        with open(self.GENERATED, "r", encoding="utf-8") as f:
            generated = f.read()
        for needle in ("eval_neg_06_pending_unreadable_abort", "eval_neg_07_resting_entries_long_heavy",
                       "= C1.2 BOTH, K1 BLOCKED"):
            self.assertIn(needle, generated)


if __name__ == "__main__":
    unittest.main()
