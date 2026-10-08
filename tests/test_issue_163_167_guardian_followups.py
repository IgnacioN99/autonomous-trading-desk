#!/usr/bin/env python3
"""
test_issue_163_167_guardian_followups.py - Issues #163 and #167 (position guardian / dynamic exit manager follow-ups).

#163: TP1 is unknown for an unverified trade reference (no TP1 trail, no BE via TP1, no YOLO trail); the guardian
      reads is_yolo / yolo_source / tp1_filled from dem's result (no stale is_yolo fallback, dem warnings reach the
      state for deferred YOLO positions); one userTrades read per symbol per cycle shared with dead alpha;
      "reference_unverified" once per position; an unreadable / corrupt trades_audit.jsonl is reported once per
      state change.
#167: per-env loop lock with holder details and a legacy-lock guard; a non-PROD loop never overwrites a live PROD
      loop's state; the unlocked fallback is recorded in state and shown by the doctor; the doctor is critical in
      PROD when the guardian is down with PROD resting entries pending; dem re-reads the stops before replacing;
      an unopenable --log-file and a closed stdin in onboarding never crash.
Hermetic: FakeExchange only, temp workspaces and log dirs, report_agent_issue.report_issue always mocked.
"""

import io
import os
import sys
import json
import time
import errno
import tempfile
import unittest
import contextlib
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "loops"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

import execute_futures_trade as eft
import dynamic_exit_manager as dem
import position_guardian_loop as pgl
import install_guardian_service as isg
import trading_doctor
import sync_session_state as sss
import user_profile as up
from test_exit_management import FakeExchange, offline, long_position, stop, write_audit, structural, ALGO_ENDPOINT
from test_issue_106_exit_manager_hardening import UserTradesExchange, long_record, LONG_POST, LONG_FORMING
from test_issue_95_trailing_activation import make_klines, flat_pre, market, reconciling_fills
from test_position_guardian import HEALTHY, STALLED
from test_pending_entries import write_guardian_state

USER_TRADES = "/fapi/v1/userTrades"


def capture(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


def corrupt_audit(ws, lines=1):
    with open(os.path.join(ws, "logs", "trades_audit.jsonl"), "a", encoding="utf-8") as f:
        f.write("{not json\n" * lines)


def user_trades_calls(fake):
    return [c for c in fake.calls if c[0] == "GET" and c[1] == USER_TRADES]


class ReporterMocked(unittest.TestCase):
    """Every guardian cycle here may report the audit health: the reporter is always mocked."""

    def setUp(self):
        p = patch("report_agent_issue.report_issue")
        self.report = p.start()
        self.addCleanup(p.stop)

    def cycle(self, fake, ws, log_dir, prepare=None, calc=None, dead_alpha=HEALTHY, env="testnet", **kw):
        with offline(fake, workspace=ws), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=calc), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(dead_alpha)), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            if prepare:
                prepare(ws)
            state = pgl.run_cycle(env, log_dir=log_dir, **kw)
        self.stderr = err.getvalue()
        return state


# =============================================================================
# #163.1 TP1 unknown for an unverified reference
# =============================================================================
class TestUnverifiedReferenceTp1(unittest.TestCase):

    def _run(self, fake_cls, symbol="BTCUSDT", leverage="3", is_yolo=False):
        now = time.time()
        klines, entry_ts = make_klines(flat_pre(), LONG_POST, LONG_FORMING, now)
        pos = long_position(symbol, amt="7", mark="101.0", leverage=leverage)
        if fake_cls is FakeExchange:
            fake = FakeExchange([pos], algos=[stop(501, 99.0, symbol=symbol)])
        else:
            fake = UserTradesExchange([pos], reconciling_fills(int(entry_ts)), algos=[stop(501, 99.0, symbol=symbol)])
        with offline(fake) as ws, market(klines):
            long_record(ws, symbol=symbol, sl_price=99.0, timestamp=entry_ts, is_yolo=is_yolo)
            res = dem.update_position_to_structural_stop(symbol, target_env="testnet")
        return res, fake

    def test_unverified_record_reduced_past_tp1_gives_no_tp1_activation_and_no_be(self):
        res, fake = self._run(FakeExchange)
        self.assertIn("reference_unverified", res["warnings"])
        self.assertEqual(res["reference_source"], "trade_audit")
        self.assertIsNone(res["tp1_filled"])
        self.assertNotEqual(res["activation_reason"], "tp1_filled")
        # +1R reached (r_multiple) but no BE: the stop stays one tick short of entry.
        self.assertEqual(res["activation_reason"], "r_multiple")
        self.assertTrue(res["updated"], res)
        self.assertLess(res["new_sl"], 100.0)
        self.assertEqual(len(user_trades_calls(fake)), 1)

    def test_verified_record_tp1_activates_and_allows_be(self):
        res, _ = self._run(UserTradesExchange)
        self.assertNotIn("reference_unverified", res["warnings"])
        self.assertIs(res["tp1_filled"], True)
        self.assertEqual(res["activation_reason"], "tp1_filled")
        self.assertTrue(res["updated"], res)
        self.assertGreater(res["new_sl"], 100.0)

    def test_unverified_yolo_record_is_not_trailed(self):
        res, fake = self._run(FakeExchange, symbol="PEPEUSDT", is_yolo=True)
        self.assertEqual(res["reason"], "yolo_before_tp1")
        self.assertTrue(res["success"])
        self.assertEqual((res["is_yolo"], res["yolo_source"], res["tp1_filled"]), (True, "trade_audit", None))
        self.assertEqual(fake.writes(), [])
        res, fake = self._run(UserTradesExchange, symbol="PEPEUSDT", is_yolo=True)
        self.assertEqual(res["activation_reason"], "tp1_filled")
        self.assertTrue(res["updated"], res)


# =============================================================================
# #163.2/3 YOLO and TP1 come from dem's matched record
# =============================================================================
class TestGuardianUsesDemResult(ReporterMocked):

    def test_dem_result_and_guardian_state_carry_yolo_and_tp1(self):
        now = time.time()
        pos = long_position("PEPEUSDT", amt="7", mark="110.0", leverage="3")

        def prepare(ws):
            long_record(ws, symbol="PEPEUSDT", sl_price=95.0, is_yolo=True, timestamp=int(now) - 600)

        fake = UserTradesExchange([pos], reconciling_fills(int(now) - 600), algos=[stop(501, 95.0, symbol="PEPEUSDT")])
        state = self.cycle(fake, tempfile.mkdtemp(), tempfile.mkdtemp(), prepare=prepare, calc=structural(102.0))
        view = state["positions"][0]
        self.assertEqual((view["is_yolo"], view["yolo_source"], view["tp1_filled"]), (True, "trade_audit", True))
        self.assertTrue(view["trailing"]["updated"], view)

    def test_stale_yolo_record_of_another_trade_is_ignored(self):
        def prepare(ws):
            # Newest raw record: SHORT YOLO (another trade). The live position is a leverage-3 LONG.
            write_audit(ws, symbol="BTCUSDT", direction="SHORT", entry_price=100.0, sl_price=103.0, total_qty=10,
                        tp1_qty=3, is_yolo=True, target_env="testnet", timestamp=int(time.time()) - 60)

        ws = tempfile.mkdtemp()
        fake = FakeExchange([long_position(amt="10", leverage="3")], algos=[stop(501, 95.0)])
        with offline(fake, workspace=ws), patch("dynamic_exit_manager.calculate_structural_stop", return_value=None):
            prepare(ws)
            self.assertEqual(eft.detect_yolo_position("BTCUSDT", leverage=3), (True, "trade_audit"))
            self.assertEqual(eft.detect_yolo_position("BTCUSDT", leverage=3, audit_fallback=False), (False, None))
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertFalse(res["is_yolo"])
        self.assertNotEqual(res["reason"], "yolo_before_tp1")
        state = self.cycle(FakeExchange([long_position(amt="10", leverage="3")], algos=[stop(501, 95.0)]),
                           tempfile.mkdtemp(), tempfile.mkdtemp(), prepare=prepare)
        view = state["positions"][0]
        self.assertFalse(view["is_yolo"])
        self.assertIsNone(view["yolo_source"])
        self.assertNotEqual(view["trailing"]["reason"], "yolo_before_tp1")

    def test_deferred_yolo_position_keeps_dem_warnings(self):
        def prepare(ws):
            long_record(ws, symbol="PEPEUSDT", sl_price=95.0)
            corrupt_audit(ws)

        fake = FakeExchange([long_position("PEPEUSDT", leverage="15")], algos=[stop(501, 95.0, symbol="PEPEUSDT")])
        state = self.cycle(fake, tempfile.mkdtemp(), tempfile.mkdtemp(), prepare=prepare, calc=structural(102.0))
        view = state["positions"][0]
        self.assertEqual(view["trailing"]["reason"], "yolo_before_tp1")
        self.assertIn("audit_corrupt_lines:1", view["trailing"]["warnings"])
        self.assertEqual((view["is_yolo"], view["yolo_source"]), (True, "leverage"))
        self.assertEqual(fake.writes(), [])


# =============================================================================
# #163.4 one userTrades read per symbol per cycle
# =============================================================================
class TestSharedUserTrades(ReporterMocked):

    def test_one_user_trades_call_with_matched_record_and_stall(self):
        now = time.time()
        open_ts = int(now) - 5 * 3600
        pos = long_position(amt="10", mark="100.5")

        def prepare(ws):
            long_record(ws, sl_price=95.0, timestamp=open_ts)

        fake = UserTradesExchange([pos], [dict(reconciling_fills(open_ts)[0])], algos=[stop(501, 95.0)])
        state = self.cycle(fake, tempfile.mkdtemp(), tempfile.mkdtemp(), prepare=prepare, dead_alpha=STALLED)
        self.assertEqual(len(user_trades_calls(fake)), 1)
        self.assertEqual(state["positions"][0]["dead_alpha"]["entry_time_source"], "userTrades")
        self.assertFalse(state["positions"][0]["reference_unverified"])

    def test_cached_unavailable_response_falls_through_to_the_audit_record(self):
        def prepare(ws):
            long_record(ws, sl_price=95.0, timestamp=int(time.time()) - 5 * 3600)

        fake = FakeExchange([long_position(amt="10", mark="100.5")], algos=[stop(501, 95.0)])
        state = self.cycle(fake, tempfile.mkdtemp(), tempfile.mkdtemp(), prepare=prepare, dead_alpha=STALLED)
        self.assertEqual(len(user_trades_calls(fake)), 1)
        self.assertEqual(state["positions"][0]["dead_alpha"]["entry_time_source"], "trades_audit")
        self.assertTrue(state["positions"][0]["reference_unverified"])

    def test_no_candidate_record_and_no_stall_makes_no_user_trades_call(self):
        fake = UserTradesExchange([long_position(amt="10")], [], algos=[stop(501, 95.0)])
        state = self.cycle(fake, tempfile.mkdtemp(), tempfile.mkdtemp())
        self.assertEqual(user_trades_calls(fake), [])
        self.assertEqual(state["positions"][0]["trailing"]["reason"], "structural_stop_unavailable")
        self.assertFalse(state["positions"][0]["reference_unverified"])

    def test_cache_key_and_passthrough(self):
        cycle = pgl.GuardianCycle("testnet")
        send = MagicMock(side_effect=[[{"id": 1}], {"code": -1}, [{"x": 1}], [{"y": 2}]])
        with patch("execute_futures_trade.send_signed_request", send):
            a = cycle._fetch("GET", USER_TRADES, {"symbol": "btcusdt", "limit": 1000}, target_env="testnet")
            b = cycle._fetch("GET", USER_TRADES, {"symbol": "BTCUSDT", "limit": 1000}, target_env="testnet")
            c = cycle._fetch("GET", USER_TRADES, {"symbol": "ETHUSDT", "limit": 1000}, target_env="testnet")
            d = cycle._fetch("GET", USER_TRADES, {"symbol": "ETHUSDT", "limit": 1000}, target_env="testnet")
            e = cycle._fetch("GET", "/fapi/v1/openAlgoOrders", {"symbol": "BTCUSDT"}, target_env="testnet")
            f = cycle._fetch("GET", "/fapi/v1/openAlgoOrders", {"symbol": "BTCUSDT"}, target_env="testnet")
        self.assertEqual((a, b, c, d, e, f), ([{"id": 1}], [{"id": 1}], {"code": -1}, {"code": -1}, [{"x": 1}], [{"y": 2}]))
        self.assertEqual(send.call_count, 4)

    def test_exceptions_are_not_cached(self):
        cycle = pgl.GuardianCycle("testnet")
        send = MagicMock(side_effect=[RuntimeError("timeout"), [{"id": 1}]])
        with patch("execute_futures_trade.send_signed_request", send):
            with self.assertRaises(RuntimeError):
                cycle._fetch("GET", USER_TRADES, {"symbol": "BTCUSDT", "limit": 1000})
            self.assertEqual(cycle._fetch("GET", USER_TRADES, {"symbol": "BTCUSDT", "limit": 1000}), [{"id": 1}])


# =============================================================================
# #163.5 reference_unverified once per position
# =============================================================================
class TestReferenceUnverifiedOnce(ReporterMocked):

    def _fake(self):
        return FakeExchange([long_position(amt="10", mark="100.5")], algos=[stop(501, 95.0)])

    def _prepare(self, ws):
        long_record(ws, sl_price=95.0)

    def test_warning_once_then_flag_only(self):
        ws, log_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        v1 = self.cycle(self._fake(), ws, log_dir, prepare=self._prepare)["positions"][0]
        self.assertTrue(v1["reference_unverified"])
        self.assertIn("reference_unverified", v1["trailing"]["warnings"])
        v2 = self.cycle(self._fake(), ws, log_dir)["positions"][0]
        self.assertTrue(v2["reference_unverified"])
        self.assertNotIn("reference_unverified", v2["trailing"]["warnings"])
        # A verified (or absent) cycle resets it: the warning returns afterwards.
        self.cycle(FakeExchange([]), ws, log_dir)
        v4 = self.cycle(self._fake(), ws, log_dir)["positions"][0]
        self.assertIn("reference_unverified", v4["trailing"]["warnings"])

    def test_previous_state_of_another_env_does_not_suppress(self):
        ws, log_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump({"env": "prod", "mode": "once", "timestamp": int(time.time()), "interval_seconds": None,
                       "positions": [{"symbol": "BTCUSDT", "side": "LONG", "reference_unverified": True}]}, f)
        view = self.cycle(self._fake(), ws, log_dir, prepare=self._prepare)["positions"][0]
        self.assertIn("reference_unverified", view["trailing"]["warnings"])

    def test_tp1_gate_uses_raw_warnings_after_suppression(self):
        ws, log_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        fake = lambda: FakeExchange([long_position(amt="7", mark="110.0")], algos=[stop(501, 95.0)])
        self.cycle(fake(), ws, log_dir, prepare=self._prepare)
        view = self.cycle(fake(), ws, log_dir)["positions"][0]
        self.assertNotIn("reference_unverified", view["trailing"]["warnings"])
        self.assertIsNone(view["tp1_filled"])


# =============================================================================
# #163.6 trades_audit health escalation
# =============================================================================
class TestAuditHealthEscalation(ReporterMocked):

    def _fake(self):
        return FakeExchange([long_position(amt="10", mark="100.5")], algos=[stop(501, 95.0)])

    def test_corrupt_reported_once_per_state_change(self):
        ws, log_dir = tempfile.mkdtemp(), tempfile.mkdtemp()

        def corrupt(w):
            long_record(w, sl_price=95.0)
            corrupt_audit(w)

        state = self.cycle(self._fake(), ws, log_dir, prepare=corrupt)
        self.assertEqual(state["audit_health"], "corrupt")
        self.report.assert_called_once()
        kw = self.report.call_args.kwargs
        self.assertEqual((kw["severity"], kw["priority"], kw["category"]), ("MEDIUM", "P2", "risk_gate"))
        self.assertEqual(kw["error_detail"], "trades_audit.jsonl corrupt")
        self.assertEqual(kw["title"], "position_guardian_loop: logs/trades_audit.jsonl is corrupt")
        self.assertEqual(kw["agent_name"], "position_guardian_loop")
        self.assertIn("env=testnet", kw["context"])
        self.assertIn("audit_corrupt_lines:1", kw["context"])
        self.assertTrue(state["cycle_ok"])

        self.cycle(self._fake(), ws, log_dir)
        self.assertEqual(self.report.call_count, 1, "same condition: not reported again")

        os.remove(os.path.join(ws, "logs", "trades_audit.jsonl"))
        long_record(ws, sl_price=95.0)
        self.assertEqual(self.cycle(self._fake(), ws, log_dir)["audit_health"], "ok")
        self.assertEqual(self.report.call_count, 1)

        state = self.cycle(self._fake(), ws, log_dir, prepare=corrupt_audit)
        self.assertEqual(state["audit_health"], "corrupt")
        self.assertEqual(self.report.call_count, 2)

    def test_unreadable_is_high_p1(self):
        ws, log_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        state = self.cycle(self._fake(), ws, log_dir,
                           prepare=lambda w: os.makedirs(os.path.join(w, "logs", "trades_audit.jsonl")))
        self.assertEqual(state["audit_health"], "unreadable")
        kw = self.report.call_args.kwargs
        self.assertEqual((kw["severity"], kw["priority"]), ("HIGH", "P1"))
        self.assertEqual(kw["error_detail"], "trades_audit.jsonl unreadable")

    def test_dry_run_never_reports(self):
        ws, log_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        state = self.cycle(self._fake(), ws, log_dir, prepare=lambda w: (long_record(w), corrupt_audit(w)),
                           dry_run=True)
        self.assertEqual(state["audit_health"], "corrupt")
        self.report.assert_not_called()

    def test_reporter_failure_never_breaks_the_cycle(self):
        self.report.side_effect = RuntimeError("github down")
        ws, log_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        state = self.cycle(self._fake(), ws, log_dir, prepare=lambda w: (long_record(w), corrupt_audit(w)))
        self.assertTrue(state["cycle_ok"])
        self.assertIn("health report could not be filed", self.stderr)
        with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["audit_health"], "corrupt")

    def test_no_positions_carries_previous_value(self):
        ws, log_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump({"env": "testnet", "mode": "once", "audit_health": "corrupt"}, f)
        self.assertEqual(self.cycle(FakeExchange([]), ws, log_dir)["audit_health"], "corrupt")
        self.report.assert_not_called()
        self.assertIsNone(self.cycle(FakeExchange([]), ws, tempfile.mkdtemp())["audit_health"])


# =============================================================================
# #167 per-env lock, holder details, legacy lock
# =============================================================================
@unittest.skipIf(fcntl is None, "fcntl not available")
class TestPerEnvLock(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.ws, "logs")
        os.makedirs(self.log_dir, exist_ok=True)

    def hold(self, name, content=None):
        fh = open(os.path.join(self.log_dir, name), "a+")
        self.addCleanup(fh.close)
        if content is not None:
            fh.write(content)
            fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh

    def run_loop(self, argv, cycle=None):
        cycle = cycle or MagicMock(return_value={"cycle_ok": True})
        with offline(FakeExchange([]), workspace=self.ws), patch.object(pgl, "DEFAULT_LOG_DIR", self.log_dir), \
             patch("position_guardian_loop.time.sleep", side_effect=KeyboardInterrupt), \
             patch.object(pgl, "run_cycle", cycle), patch.object(pgl, "format_state", return_value="CYCLE RAN"):
            code, out, err = capture(pgl.main, argv)
        return code, out, err, cycle

    def test_testnet_lock_does_not_block_prod(self):
        self.hold(pgl.lock_file_name("testnet"))
        code, out, _, cycle = self.run_loop(["--interval", "60", "--env", "prod"])
        self.assertEqual(code, 0)
        self.assertIn("CYCLE RAN", out)
        cycle.assert_called_once()

    def test_held_prod_lock_reports_holder(self):
        holder = {"env": "prod", "interval_seconds": 60, "pid": 4242, "started_ts": 1}
        self.hold(pgl.lock_file_name("prod"), json.dumps(holder))
        code, out, _, cycle = self.run_loop(["--interval", "60", "--env", "prod", "--json"])
        self.assertEqual(code, 0)
        cycle.assert_not_called()
        self.assertEqual(json.loads(out), {
            "success": True, "already_running": True, "env": "prod", "holder": holder,
            "message": "another guardian loop for prod is already running (interval 60s, pid 4242); exiting"})
        code, out, _, _ = self.run_loop(["--interval", "60", "--env", "prod"])
        self.assertIn("another guardian loop for prod is already running (interval 60s, pid 4242); exiting", out)

    def test_holder_without_content(self):
        self.hold(pgl.lock_file_name("prod"))
        _, out, _, cycle = self.run_loop(["--interval", "60", "--env", "prod"])
        self.assertIn("another guardian loop for prod is already running (holder details unavailable); exiting", out)
        cycle.assert_not_called()

    def test_running_loop_writes_holder_json_with_clamped_interval(self):
        seen = {}

        def cycle_fn(*a, **kw):
            with open(os.path.join(self.log_dir, pgl.lock_file_name("testnet")), "r", encoding="utf-8") as f:
                seen.update(json.loads(f.read()))
            return {"cycle_ok": True}

        code, _, _, _ = self.run_loop(["--interval", "5", "--env", "testnet"], cycle=MagicMock(side_effect=cycle_fn))
        self.assertEqual(code, 0)
        self.assertEqual((seen["env"], seen["interval_seconds"], seen["pid"]), ("testnet", 10, os.getpid()))
        self.assertIsInstance(seen["started_ts"], int)

    def test_legacy_lock_held_makes_a_new_loop_exit(self):
        legacy = self.hold(pgl.LEGACY_LOCK_FILE_NAME)
        code, out, _, cycle = self.run_loop(["--interval", "60", "--env", "prod", "--json"])
        self.assertEqual(code, 0)
        cycle.assert_not_called()
        payload = json.loads(out)
        self.assertEqual(payload["holder"], {"legacy": True})
        self.assertEqual(payload["message"], "a pre-upgrade guardian loop holds logs/guardian_loop.lock; restart the "
                                             "guardian task; exiting")
        with open(os.path.join(self.log_dir, pgl.lock_file_name("prod")), "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # the per-env lock was released
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fcntl.flock(legacy.fileno(), fcntl.LOCK_UN)
        code, out, _, cycle = self.run_loop(["--interval", "60", "--env", "prod"])
        self.assertIn("CYCLE RAN", out, "an unheld legacy file never blocks")
        cycle.assert_called_once()


# =============================================================================
# #167 state ownership
# =============================================================================
class TestStateOwnership(ReporterMocked):

    def setUp(self):
        super().setUp()
        self.ws = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.ws, "logs")

    def read_state(self):
        with open(os.path.join(self.log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            return json.load(f)

    def test_testnet_loop_never_overwrites_a_live_prod_loop_state(self):
        write_guardian_state(self.ws, env="prod", age=5)
        before = self.read_state()
        fake = FakeExchange([long_position("ETHUSDT")], algos=[])  # orphan: heal action
        self.cycle(fake, self.ws, self.log_dir, mode="loop", interval_seconds=60)
        self.assertEqual(self.read_state(), before)
        self.assertIn("a live prod guardian loop owns logs/guardian_state.json", self.stderr)
        with open(os.path.join(self.log_dir, pgl.ACTIONS_FILE_NAME), "r", encoding="utf-8") as f:
            actions = [json.loads(line) for line in f if line.strip()]
        self.assertEqual([a["type"] for a in actions], ["orphan_heal"])
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws):
            self.assertTrue(eft.check_guardian_alive("prod")[0])

    def test_prod_loop_overwrites_a_live_testnet_loop_state(self):
        write_guardian_state(self.ws, env="testnet", age=5)
        self.cycle(FakeExchange([]), self.ws, self.log_dir, env="prod", mode="loop", interval_seconds=60)
        self.assertEqual(self.read_state()["env"], "prod")

    def test_stale_prod_state_is_overwritten_and_once_still_never_overwrites(self):
        write_guardian_state(self.ws, env="prod", age=1000)
        self.cycle(FakeExchange([]), self.ws, self.log_dir, mode="loop", interval_seconds=60)
        self.assertEqual(self.read_state()["env"], "testnet")
        write_guardian_state(self.ws, env="testnet", age=5)
        before = self.read_state()
        self.cycle(FakeExchange([]), self.ws, self.log_dir, mode="once")
        self.assertEqual(self.read_state(), before)
        self.assertIn("a live testnet guardian loop owns", self.stderr)


# =============================================================================
# #167 unlocked fallback recorded in state and shown by the doctor
# =============================================================================
class TestUnlockedFallback(ReporterMocked):

    def test_lock_warning_in_state_and_format(self):
        ws = tempfile.mkdtemp()
        log_dir = os.path.join(ws, "logs")
        fake_fcntl = MagicMock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        fake_fcntl.flock.side_effect = OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
        with offline(FakeExchange([]), workspace=ws), patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
             patch.object(pgl, "fcntl", fake_fcntl), \
             patch("position_guardian_loop.time.sleep", side_effect=KeyboardInterrupt):
            code, out, err = capture(pgl.main, ["--interval", "60", "--env", "testnet"])
        self.assertEqual(code, 0)
        with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertIn("cannot lock guardian_loop.testnet.lock", state["lock_warning"])
        self.assertIn("running without the single-instance lock", state["lock_warning"])
        self.assertTrue(state["cycle_ok"])
        self.assertEqual(state["errors"], [])
        self.assertIn("  ! lock: cannot lock guardian_loop.testnet.lock", out)
        self.assertIn("running without the single-instance lock", err)

    def test_doctor_warns_on_alive_unlocked_guardian(self):
        ws = tempfile.mkdtemp()
        write_guardian_state(ws, env="prod", age=1)
        path = os.path.join(ws, "logs", "guardian_state.json")
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state["lock_warning"] = "cannot lock guardian_loop.prod.lock (ENOLCK); running without the single-instance lock"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        with patch("execute_futures_trade._workspace_dir", return_value=ws):
            level, msg = trading_doctor.check_guardian_service("prod")
            self.assertEqual(level, "warn")
            self.assertIn("WITHOUT the single-instance lock", msg)
            self.assertIn("cannot lock guardian_loop.prod.lock", msg)
            state["lock_warning"] = None
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            self.assertEqual(trading_doctor.check_guardian_service("prod")[0], "ok")


# =============================================================================
# #167 doctor severity
# =============================================================================
class TestDoctorGuardianSeverity(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.ws, "logs"), exist_ok=True)
        for p in (patch("execute_futures_trade._workspace_dir", return_value=self.ws),
                  patch("utils.dossier_provenance._is_wsl", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def registry(self, *envs, raw=None):
        with open(os.path.join(self.ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
            if raw is not None:
                f.write(raw)
            else:
                json.dump({"entries": {f"{env}:BTCUSDT:{i}": {"symbol": "BTCUSDT", "target_env": env}
                                       for i, env in enumerate(envs)}}, f)

    def test_prod_guardian_down_with_prod_pending_entry_is_critical(self):
        self.registry("prod", "testnet")
        level, msg = trading_doctor.check_guardian_service("prod")
        self.assertEqual(level, "critical")
        self.assertIn("1 pending PROD resting entry", msg)
        self.assertIn("python3 scripts/execute_futures_trade.py --protect-pending", msg)
        self.assertIn("MCP: no pre-armed SL", msg)
        self.assertIn("python3 scripts/install_guardian_service.py --install --env prod", msg)

    def test_unreadable_registry_is_critical(self):
        self.registry(raw="{not json")
        level, msg = trading_doctor.check_guardian_service("prod")
        self.assertEqual(level, "critical")
        self.assertIn("pending entries registry unreadable", msg)
        with patch("execute_futures_trade.load_pending_entries", side_effect=OSError("io")):
            self.assertEqual(trading_doctor.check_guardian_service("prod")[0], "critical")

    def test_only_testnet_records_or_testnet_target_warn(self):
        self.registry("testnet")
        self.assertEqual(trading_doctor.check_guardian_service("prod")[0], "warn")
        self.assertEqual(trading_doctor.check_guardian_service("testnet")[0], "warn")
        self.registry(raw="{not json")
        self.assertEqual(trading_doctor.check_guardian_service("testnet")[0], "warn")

    def test_alive_guardian_is_ok_even_with_pending_entries(self):
        self.registry("prod")
        write_guardian_state(self.ws, env="prod", age=1)
        self.assertEqual(trading_doctor.check_guardian_service("prod")[0], "ok")

    def test_run_doctor_fails_on_a_critical_guardian(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        profile = {"profile_completed": True, "risk_pct_equity": 0.005, "yolo_slot_enabled": False}
        with patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.get_client_config", return_value=("key12345678", "sec12345678", "http://x")), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("execute_futures_trade.send_signed_request",
                   side_effect=lambda method, endpoint, params=None, target_env=None, **kw: [
                       {"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}]
                   if endpoint == "/fapi/v2/balance" else []), \
             patch("user_profile.load_user_profile", return_value=profile), \
             patch("trading_doctor.check_pretool_hook", return_value={"ok": True, "critical": [], "warnings": [], "info": []}), \
             patch("shadow_tracker.calculate_efficacy_metrics", side_effect=RuntimeError("skip")), \
             patch.object(sss, "STATE_FILE", os.path.join(self.ws, "session_state.json")), \
             patch("sync_session_state.sync_session_state", MagicMock(return_value={"is_valid": True})), \
             patch("trading_doctor.check_guardian_service", return_value=("critical", "guardian down with pending")):
            mock_urlopen.return_value.__enter__.return_value = resp
            code, out, _ = capture(trading_doctor.run_doctor, target_env="testnet")
        self.assertEqual(code, 1, out)
        self.assertIn("❌ [GUARDIAN] guardian down with pending", out)


# =============================================================================
# #167 dem re-reads the stops right before replacing
# =============================================================================
class RereadExchange(FakeExchange):
    """FakeExchange whose `at`-th GET /fapi/v1/openAlgoOrders (default the second: dem's re-read when dem is called
    alone) answers `second` (a stop list, which then becomes the exchange's state, or an error payload)."""

    def __init__(self, positions, second, at=2, **kw):
        super().__init__(positions, **kw)
        self.second = second
        self.at = at
        self.algo_reads = 0

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if method == "GET" and endpoint == "/fapi/v1/openAlgoOrders":
            self.algo_reads += 1
            if self.algo_reads == self.at:
                self.calls.append((method, endpoint, dict(params or {})))
                if isinstance(self.second, list):
                    self.algos = [dict(a) for a in self.second]
                    return [dict(a) for a in self.second]
                return dict(self.second)
        return super().__call__(method, endpoint, params, target_env, retry_count)


class TestRereadBeforeReplace(unittest.TestCase):

    def _update(self, second):
        fake = RereadExchange([long_position()], second, algos=[stop(501, 95.0)])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)), \
             patch("execute_futures_trade.replace_protective_stop", wraps=eft.replace_protective_stop) as rep:
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        return res, fake, rep

    def test_fresher_tighter_stop_gives_not_tighter(self):
        res, fake, rep = self._update([stop(777, 103.0)])
        self.assertEqual(res["reason"], "not_tighter")
        self.assertTrue(res["success"])
        self.assertEqual((res["previous_sl"], res["current_sl"]), (95.0, 103.0))
        self.assertIn("(103.0)", res["message"])
        rep.assert_not_called()
        self.assertEqual(fake.writes(), [])

    def test_reread_error_keeps_everything(self):
        res, fake, rep = self._update({"code": -1001, "msg": "Internal error"})
        self.assertEqual(res["reason"], "stops_requery_failed")
        self.assertTrue(res["success"], "the old stop still protects the position")
        self.assertNotIn("error", res)
        self.assertIn("Cannot re-read current stops", res["message"])
        rep.assert_not_called()
        self.assertEqual(fake.writes(), [])

    def test_vanished_stop_on_reread_is_reprotected(self):
        res, fake, rep = self._update([])
        # Stop gone between the reads: the new stop is still placed (re-protects), with no old stop to cancel.
        rep.assert_called_once()
        self.assertEqual(rep.call_args[0][4], [])
        self.assertTrue(res["updated"], res)
        self.assertEqual(fake.write_index("DELETE", ALGO_ENDPOINT), [])

    def test_replace_receives_the_fresh_stops(self):
        res, fake, rep = self._update([stop(777, 96.0)])
        self.assertTrue(res["updated"], res)
        rep.assert_called_once()
        self.assertEqual([o["algoId"] for o in rep.call_args[0][4]], [777])
        post = fake.write_index("POST", ALGO_ENDPOINT)[0]
        reads_before_post = [c for c in fake.calls[:post] if c[1] == "/fapi/v1/openAlgoOrders"]
        self.assertEqual(len(reads_before_post), 2)
        self.assertEqual(res["cancelled_old_stop_ids"], [777])

    def test_guardian_marks_unprotected_when_the_stop_vanished_and_the_new_one_fails(self):
        # Reads: 1 guardian orphan audit, 2 dem, 3 dem's re-read (the stop is gone).
        fake = RereadExchange([long_position()], [], at=3, algos=[stop(501, 95.0)], index_new_stops=False)
        with offline(fake), patch.object(pgl, "DEFAULT_LOG_DIR", tempfile.mkdtemp()), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)), \
             patch("report_agent_issue.report_issue"), contextlib.redirect_stderr(io.StringIO()):
            state = pgl.run_cycle("testnet", log_dir=tempfile.mkdtemp())
        view = state["positions"][0]
        self.assertEqual(view["trailing"]["reason"], "new_stop_unverified")
        self.assertEqual(view["trailing"]["previous_sl"], 95.0)
        self.assertIn("NO verified stop", view["trailing"]["message"])
        self.assertFalse(view["protected"])
        self.assertFalse(state["cycle_ok"])

    def test_dry_run_makes_no_reread(self):
        fake = RereadExchange([long_position()], {"code": -1}, algos=[stop(501, 95.0)])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet", dry_run=True)
        self.assertEqual(res["reason"], "dry_run")
        self.assertEqual(fake.algo_reads, 1)


# =============================================================================
# #167 startup and onboarding robustness
# =============================================================================
class TestStartupRobustness(ReporterMocked):

    def test_unopenable_log_file_runs_without_it(self):
        ws = tempfile.mkdtemp()
        regular = os.path.join(ws, "file")
        with open(regular, "w", encoding="utf-8") as f:
            f.write("x")
        bad = os.path.join(regular, "guardian.log")
        with offline(FakeExchange([]), workspace=ws), patch.object(pgl, "DEFAULT_LOG_DIR", os.path.join(ws, "logs")):
            code, out, err = capture(pgl.main, ["--once", "--env", "testnet", "--log-file", bad])
        self.assertEqual(code, 0)
        self.assertIn("GUARDIAN TESTNET", out)
        lines = [line for line in err.splitlines() if "running without the log file" in line]
        self.assertEqual(len(lines), 1, err)
        self.assertIn(f"cannot open --log-file {bad}", lines[0])
        self.assertNotIsInstance(sys.stdout, pgl._TeeStream)
        self.assertNotIsInstance(sys.stderr, pgl._TeeStream)

    def test_onboarding_offer_with_closed_stdin(self):
        install = MagicMock(return_value=0)
        with patch.object(isg, "is_wsl", return_value=True), patch.object(isg, "install", install), \
             patch("builtins.input", side_effect=EOFError):
            _, out, _ = capture(up._offer_guardian_service)
        install.assert_not_called()
        self.assertIn("Skipped. Install it later with: python3 scripts/install_guardian_service.py --install", out)


if __name__ == "__main__":
    unittest.main()
