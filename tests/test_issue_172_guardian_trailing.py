#!/usr/bin/env python3
"""
test_issue_172_guardian_trailing.py - Issue #172 (guardian / trailing follow-ups after #163/#167).

1. YOLO without a matched record: the newest same symbol / direction / env trades_audit record's is_yolo keeps the
   planned SL (yolo_source "trade_audit_unmatched"); an opposite-direction or other-env record does not.
4. Audit-health memory: the guardian process compares against its own last real-cycle value (two cycles report once,
   a dry-run never suppresses the next real report, a TESTNET loop beside a live PROD loop reports once).
5. stops_requery_failed is a dem warning, listed in the guardian state (trail_warnings) and printed.
6. Doctor: an exception from check_guardian_service is critical in PROD with pending entries (or an unreadable
   registry), else WARN.
7. An empty stop re-read on a flat position gives "position_closed" (no POST, not unprotected).
8. move_sl_to_breakeven re-reads the stops right before the write.
Docs: AGENTS.md TP1-trust rule (byte cap), SKILL / README operator notes.
Hermetic: FakeExchange only, temp workspaces and log dirs, report_agent_issue.report_issue always mocked.
"""

import io
import os
import sys
import json
import time
import tempfile
import unittest
import contextlib
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "loops"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import dynamic_exit_manager as dem
import position_guardian_loop as pgl
import trading_doctor
import sync_session_state as sss
from test_exit_management import FakeExchange, offline, long_position, stop, write_audit, structural, ALGO_ENDPOINT
from test_issue_106_exit_manager_hardening import long_record, UserTradesExchange, buy_fill
from test_issue_163_167_guardian_followups import RereadExchange, corrupt_audit
from test_position_guardian import HEALTHY
from test_guardian_excursion import KlineSource
from test_pending_entries import write_guardian_state

USER_TRADES = "/fapi/v1/userTrades"
POSITION_RISK = "/fapi/v2/positionRisk"


def gets(fake, endpoint):
    return [c for c in fake.calls if c[0] == "GET" and c[1] == endpoint]


class EmptyRereadExchange(RereadExchange):
    """The `at`-th openAlgoOrders read returns [] (the stop is gone). With flat=True the position is closed at that
    moment (flat="empty": positionRisk then answers []); pos_error makes every later positionRisk read fail."""

    def __init__(self, positions, flat=True, pos_error=False, at=2, pos_payload=None, **kw):
        super().__init__(positions, [], at=at, **kw)
        self.flat = flat
        self.pos_error = pos_error
        self.pos_payload = pos_payload  # raw positionRisk answer after the stop vanished (overrides the fake state)
        self.vanished = False

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if method == "GET" and endpoint == "/fapi/v1/openAlgoOrders" and self.algo_reads + 1 == self.at:
            self.vanished = True
            if self.flat == "empty":
                self.positions = []
            elif self.flat:
                for p in self.positions:
                    p["positionAmt"] = "0"
        if self.vanished and self.pos_error and endpoint == POSITION_RISK:
            self.calls.append((method, endpoint, dict(params or {})))
            return {"code": -1001, "msg": "Internal error"}
        if self.vanished and self.pos_payload is not None and endpoint == POSITION_RISK:
            self.calls.append((method, endpoint, dict(params or {})))
            return json.loads(json.dumps(self.pos_payload))
        return super().__call__(method, endpoint, params, target_env, retry_count)


# =============================================================================
# 1. YOLO without a matched record
# =============================================================================
class TestUnmatchedYoloRecord(unittest.TestCase):

    def _update(self, **record):
        fake = FakeExchange([long_position(amt="10", leverage="3")], algos=[stop(501, 95.0)])
        calc = MagicMock(return_value=structural(102.0))
        with offline(fake) as ws, patch("dynamic_exit_manager.calculate_structural_stop", calc):
            long_record(ws, **dict({"sl_price": 95.0}, **record))
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        return res, fake, calc

    def assert_deferred(self, res, fake, calc):
        self.assertEqual(res["reason"], "yolo_before_tp1")
        self.assertTrue(res["success"])
        self.assertEqual((res["is_yolo"], res["yolo_source"], res["tp1_filled"]),
                         (True, "trade_audit_unmatched", None))
        self.assertEqual(res["current_sl"], 95.0, "planned SL kept")
        self.assertEqual(fake.writes(), [])
        self.assertEqual(gets(fake, USER_TRADES), [], "no matched record: no userTrades read")
        calc.assert_not_called()

    def test_slipped_fill_with_newest_yolo_record_is_not_trailed(self):
        res, fake, calc = self._update(entry_price=101.0, is_yolo=True)  # 1% from the live entry 100.0
        self.assert_deferred(res, fake, calc)

    def test_qty_mismatch_with_newest_yolo_record_is_not_trailed(self):
        res, fake, calc = self._update(total_qty=5, is_yolo=True)  # live size 10 > recorded total 5
        self.assert_deferred(res, fake, calc)

    def test_opposite_direction_yolo_record_is_not_yolo(self):
        res, _, _ = self._update(direction="SHORT", entry_price=101.0, sl_price=105.0, is_yolo=True)
        self.assertFalse(res["is_yolo"])
        self.assertIsNone(res["yolo_source"])
        self.assertTrue(res["updated"], res)

    def test_other_env_yolo_record_is_not_yolo(self):
        res, _, _ = self._update(entry_price=101.0, is_yolo=True, target_env="prod")
        self.assertFalse(res["is_yolo"])
        self.assertTrue(res["updated"], res)

    def test_non_yolo_unmatched_record_still_trails(self):
        res, _, _ = self._update(entry_price=101.0, is_yolo=False)
        self.assertFalse(res["is_yolo"])
        self.assertTrue(res["updated"], res)

    def _update_with_fills(self, record_ts, open_ts):
        fake = UserTradesExchange([long_position(amt="10", leverage="3")], [buy_fill(open_ts)],
                                  algos=[stop(501, 95.0)])
        with offline(fake) as ws, \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            long_record(ws, sl_price=95.0, is_yolo=True, timestamp=record_ts)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        return res, fake

    def test_record_proven_stale_by_user_trades_is_not_a_yolo_candidate(self):
        # Round 2: the YOLO record matches entry and size but is 3h older than the position's Binance open time,
        # so it belongs to an earlier trade: a later standard position is trailed normally.
        now = int(time.time())
        res, fake = self._update_with_fills(record_ts=now - 3 * 3600, open_ts=now - 600)
        self.assertEqual(len(gets(fake, USER_TRADES)), 1)
        self.assertEqual(res["reference_source"], "current_stop")
        self.assertFalse(res["is_yolo"])
        self.assertIsNone(res["yolo_source"])
        self.assertTrue(res["updated"], res)

    def test_record_verified_fresh_by_user_trades_stays_yolo(self):
        now = int(time.time())
        res, fake = self._update_with_fills(record_ts=now - 600, open_ts=now - 600)
        self.assertEqual(res["reason"], "yolo_before_tp1")
        self.assertEqual(res["yolo_source"], "trade_audit")
        self.assertEqual(fake.writes(), [])

    def test_trade_reference_tuples_unchanged(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        with offline(fake) as ws:
            long_record(ws, entry_price=101.0, is_yolo=True)
            full = dem._resolve_trade_reference_full("BTCUSDT", long_position(), "LONG", 95.0, "testnet")
            short = dem.resolve_trade_reference("BTCUSDT", long_position(), "LONG", 95.0, "testnet")
        self.assertEqual(len(full), 5)
        self.assertEqual(len(short), 3)
        self.assertEqual(full[2:4], ("current_stop", None))


# =============================================================================
# 4. Audit-health memory
# =============================================================================
class TestAuditHealthMemory(unittest.TestCase):

    def setUp(self):
        p = patch("report_agent_issue.report_issue")
        self.report = p.start()
        self.addCleanup(p.stop)
        self.ws = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.ws, "logs")
        long_record(self.ws, sl_price=95.0)
        corrupt_audit(self.ws)

    def cycle(self, env="testnet", **kw):
        fake = FakeExchange([long_position(amt="10", mark="100.5")], algos=[stop(501, 95.0)])
        with offline(fake, workspace=self.ws), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)), \
             contextlib.redirect_stderr(io.StringIO()):
            return pgl.run_cycle(env, log_dir=self.log_dir, **kw)

    def test_two_cycles_in_one_process_report_once(self):
        memory = pgl.AuditHealthMemory()
        self.assertEqual(self.cycle(memory=memory)["audit_health"], "corrupt")
        self.cycle(memory=memory)
        self.assertEqual(self.report.call_count, 1)

    def test_testnet_loop_beside_live_prod_loop_reports_once(self):
        write_guardian_state(self.ws, env="prod", age=5)
        memory = pgl.AuditHealthMemory()
        for _ in range(3):
            self.cycle(memory=memory, mode="loop", interval_seconds=60)
        self.assertEqual(self.report.call_count, 1)
        with open(os.path.join(self.log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["env"], "prod", "the PROD loop's state is never overwritten")

    def test_without_memory_the_testnet_loop_beside_prod_reports_every_cycle(self):
        # Documents why _run_main passes a memory: run_cycle without one keeps the state-file-only comparison.
        write_guardian_state(self.ws, env="prod", age=5)
        for _ in range(2):
            self.cycle(mode="loop", interval_seconds=60)
        self.assertEqual(self.report.call_count, 2)

    def test_dry_run_then_real_cycle_reports(self):
        memory = pgl.AuditHealthMemory()
        self.assertEqual(self.cycle(memory=memory, dry_run=True)["audit_health"], "corrupt")
        self.report.assert_not_called()
        self.cycle(memory=memory)
        self.assertEqual(self.report.call_count, 1)

    def test_dry_run_state_from_another_process_does_not_suppress_the_report(self):
        state = self.cycle(dry_run=True)  # a --dry-run --once process: writes audit_health "corrupt"
        self.assertTrue(state["dry_run"])
        self.cycle(memory=pgl.AuditHealthMemory())  # a fresh real process
        self.assertEqual(self.report.call_count, 1)

    def test_restart_after_a_reported_state_does_not_report_again(self):
        self.cycle(memory=pgl.AuditHealthMemory())
        self.cycle(memory=pgl.AuditHealthMemory())
        self.assertEqual(self.report.call_count, 1)

    def test_corrupt_ok_corrupt_reports_twice(self):
        memory = pgl.AuditHealthMemory()
        self.cycle(memory=memory)
        os.remove(os.path.join(self.log_dir, "trades_audit.jsonl"))
        long_record(self.ws, sl_price=95.0)
        self.assertEqual(self.cycle(memory=memory)["audit_health"], "ok")
        corrupt_audit(self.ws)
        self.cycle(memory=memory)
        self.assertEqual(self.report.call_count, 2)

    def test_run_main_shares_one_memory_across_loop_cycles(self):
        seen = []

        def cycle_fn(*a, **kw):
            seen.append(kw.get("memory"))
            if len(seen) >= 2:
                raise KeyboardInterrupt
            return {"cycle_ok": True}

        args = MagicMock(interval=60, once=False, dry_run=False, close_dead_alpha=False, json_output=False)
        with patch.object(pgl, "run_cycle", side_effect=cycle_fn), patch.object(pgl, "format_state", return_value=""), \
             patch("position_guardian_loop.time.sleep", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                pgl._run_main(args, "testnet")
        self.assertEqual(len(seen), 2)
        self.assertIsInstance(seen[0], pgl.AuditHealthMemory)
        self.assertIs(seen[0], seen[1])


# =============================================================================
# 5. stops_requery_failed is visible
# =============================================================================
class TestRequeryFailureVisible(unittest.TestCase):

    def test_dem_adds_a_warning_and_stays_success(self):
        fake = RereadExchange([long_position()], {"code": -1001, "msg": "Internal error"}, algos=[stop(501, 95.0)])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["reason"], "stops_requery_failed")
        self.assertTrue(res["success"])
        self.assertNotIn("error", res)
        requery = [w for w in res["warnings"] if w.startswith("stops_requery_failed:")]
        self.assertEqual(len(requery), 1)
        self.assertIn("Internal error", requery[0])
        self.assertEqual(fake.writes(), [])

    def test_guardian_lists_and_prints_the_warning(self):
        # Reads: 1 guardian orphan audit, 2 dem, 3 dem's re-read (fails).
        fake = RereadExchange([long_position()], {"code": -1001, "msg": "Internal error"}, at=3,
                              algos=[stop(501, 95.0)])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)), \
             patch("report_agent_issue.report_issue"), contextlib.redirect_stderr(io.StringIO()):
            state = pgl.run_cycle("testnet", log_dir=tempfile.mkdtemp())
        self.assertTrue(state["cycle_ok"])
        self.assertEqual(state["errors"], [])
        self.assertEqual(len(state["trail_warnings"]), 1)
        self.assertEqual(state["trail_warnings"][0]["symbol"], "BTCUSDT")
        self.assertTrue(state["trail_warnings"][0]["warning"].startswith("stops_requery_failed:"))
        self.assertIn("~ warning trailing BTCUSDT: stops_requery_failed:", pgl.format_state(state))

    def test_reference_notices_are_not_listed(self):
        fake = FakeExchange([long_position(amt="10", mark="100.5")], algos=[stop(501, 95.0)])
        ws = tempfile.mkdtemp()
        with offline(fake, workspace=ws), patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)), \
             patch("report_agent_issue.report_issue"), contextlib.redirect_stderr(io.StringIO()):
            long_record(ws, sl_price=95.0)
            corrupt_audit(ws)
            state = pgl.run_cycle("testnet", log_dir=tempfile.mkdtemp())
        self.assertIn("reference_unverified", state["positions"][0]["trailing"]["warnings"])
        self.assertEqual(state["trail_warnings"], [])


# =============================================================================
# 6. Doctor exception path
# =============================================================================
class TestDoctorGuardianException(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.ws, "logs"), exist_ok=True)
        p = patch("execute_futures_trade._workspace_dir", return_value=self.ws)
        p.start()
        self.addCleanup(p.stop)

    def registry(self, *envs, raw=None):
        with open(os.path.join(self.ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
            if raw is not None:
                f.write(raw)
            else:
                json.dump({"entries": {f"{env}:BTCUSDT:{i}": {"symbol": "BTCUSDT", "target_env": env}
                                       for i, env in enumerate(envs)}}, f)

    def test_prod_with_pending_entry_is_critical(self):
        self.registry("prod")
        level, msg = trading_doctor.guardian_check_failed("prod", RuntimeError("boom"))
        self.assertEqual(level, "critical")
        self.assertIn("RuntimeError", msg)
        self.assertIn("1 pending PROD resting entry", msg)
        self.assertIn("python3 scripts/execute_futures_trade.py --protect-pending", msg)

    def test_prod_with_unreadable_registry_is_critical(self):
        self.registry(raw="{not json")
        self.assertEqual(trading_doctor.guardian_check_failed("prod", RuntimeError("boom"))[0], "critical")
        with patch("execute_futures_trade.load_pending_entries", side_effect=OSError("io")):
            level, msg = trading_doctor.guardian_check_failed("prod", RuntimeError("boom"))
        self.assertEqual(level, "critical")
        self.assertIn("pending entries registry unreadable", msg)

    def test_prod_without_pending_and_testnet_warn(self):
        self.assertEqual(trading_doctor.guardian_check_failed("prod", RuntimeError("boom")),
                         ("warn", "Position guardian liveness unreadable (RuntimeError)."))
        self.registry("testnet")
        self.assertEqual(trading_doctor.guardian_check_failed("prod", RuntimeError("boom"))[0], "warn")
        self.registry("prod")
        self.assertEqual(trading_doctor.guardian_check_failed("testnet", RuntimeError("boom"))[0], "warn")

    def _run_doctor(self, **patches):
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
             patch("utils.dossier_provenance._is_wsl", return_value=True), \
             patch.object(sss, "STATE_FILE", os.path.join(self.ws, "session_state.json")), \
             patch("sync_session_state.sync_session_state", MagicMock(return_value={"is_valid": True})), \
             patch("trading_doctor.check_guardian_service", side_effect=RuntimeError("boom")), \
             contextlib.ExitStack() as stack:
            for target, value in patches.items():
                stack.enter_context(patch(target, value))
            mock_urlopen.return_value.__enter__.return_value = resp
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = trading_doctor.run_doctor(target_env="testnet")
        return code, out.getvalue()

    def test_run_doctor_routes_the_exception_through_the_fallback(self):
        _, out = self._run_doctor()
        self.assertIn("⚠️  [GUARDIAN] Position guardian liveness unreadable (RuntimeError).", out)
        code, out = self._run_doctor(**{"trading_doctor.guardian_check_failed":
                                        MagicMock(return_value=("critical", "guardian unreadable with pending"))})
        self.assertEqual(code, 1, out)
        self.assertIn("❌ [GUARDIAN] guardian unreadable with pending", out)


# =============================================================================
# 7. Empty stop re-read on a flat position
# =============================================================================
class TestEmptyRereadFlatPosition(unittest.TestCase):

    def _update(self, fake):
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            return dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")

    def test_flat_position_gives_position_closed_without_a_post(self):
        fake = EmptyRereadExchange([long_position()], flat=True, algos=[stop(501, 95.0)])
        res = self._update(fake)
        self.assertEqual(res["reason"], "position_closed")
        self.assertTrue(res["success"])
        self.assertFalse(res["updated"])
        self.assertNotIn("error", res)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(len(gets(fake, POSITION_RISK)), 2, "initial read + one re-check")

    def test_still_open_position_is_reprotected(self):
        fake = EmptyRereadExchange([long_position()], flat=False, algos=[stop(501, 95.0)])
        res = self._update(fake)
        self.assertTrue(res["updated"], res)
        post = fake.write_index("POST", ALGO_ENDPOINT)[0]
        self.assertEqual(len([c for c in fake.calls[:post] if c[1] == POSITION_RISK]), 2)

    def test_position_read_error_is_reprotected(self):
        fake = EmptyRereadExchange([long_position()], flat=True, pos_error=True, algos=[stop(501, 95.0)])
        res = self._update(fake)
        self.assertNotEqual(res["reason"], "position_closed")
        self.assertEqual(len(fake.write_index("POST", ALGO_ENDPOINT)), 1, "unknown position state: re-protect")

    def test_empty_position_read_is_reprotected(self):
        fake = EmptyRereadExchange([long_position()], flat="empty", algos=[stop(501, 95.0)])
        res = self._update(fake)
        self.assertNotEqual(res["reason"], "position_closed")
        self.assertEqual(len(fake.write_index("POST", ALGO_ENDPOINT)), 1, "empty positionRisk is not proof of flat")

    def test_payload_without_proof_of_flat_is_reprotected(self):
        # Round 2: no row for the symbol, or no parsable positionAmt, is not proof that the position is flat.
        payloads = {
            "empty row": [{}],
            "other symbol only": [dict(long_position("ETHUSDT"), positionAmt="0")],
            "non-numeric positionAmt": [dict(long_position(), positionAmt="n/a")],
            "missing positionAmt": [{"symbol": "BTCUSDT", "entryPrice": "100.0"}],
            "null positionAmt": [dict(long_position(), positionAmt=None)],
            "flat row plus empty row": [dict(long_position(), positionAmt="0"), {}],
        }
        for name, payload in payloads.items():
            with self.subTest(name):
                fake = EmptyRereadExchange([long_position()], flat=False, pos_payload=payload,
                                           algos=[stop(501, 95.0)])
                res = self._update(fake)
                self.assertNotEqual(res["reason"], "position_closed")
                self.assertEqual(len(fake.write_index("POST", ALGO_ENDPOINT)), 1)

    def test_is_flat_helper(self):
        cases = [
            ([dict(long_position(), positionAmt="0")], True),
            ([dict(long_position(), positionAmt="0.000")], True),
            ([dict(long_position(), positionAmt="0"), dict(long_position("ETHUSDT"), positionAmt="2")], True),
            ([dict(long_position(), positionAmt="0"), dict(long_position(), positionAmt="1")], False),
            ([{}], False),
            ([], False),
            ([dict(long_position("ETHUSDT"), positionAmt="0")], False),
            ([dict(long_position(), positionAmt="abc")], False),
            ({"code": -1001}, False),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload), \
                 patch("execute_futures_trade.send_signed_request", return_value=payload):
                self.assertIs(dem._position_is_flat("BTCUSDT", "testnet"), expected)
        with patch("execute_futures_trade.send_signed_request", side_effect=OSError("down")):
            self.assertFalse(dem._position_is_flat("BTCUSDT", "testnet"))

    def test_no_position_read_when_the_reread_finds_a_stop(self):
        fake = RereadExchange([long_position()], [stop(777, 96.0)], algos=[stop(501, 95.0)])
        res = self._update(fake)
        self.assertTrue(res["updated"], res)
        self.assertEqual(len(gets(fake, POSITION_RISK)), 1)

    def test_guardian_treats_it_as_closed(self):
        # Reads: 1 guardian orphan audit, 2 dem, 3 dem's re-read (stop triggered, position flat).
        fake = EmptyRereadExchange([long_position()], flat=True, at=3, algos=[stop(501, 95.0)])
        da = MagicMock(return_value=dict(HEALTHY))
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", da), \
             patch("report_agent_issue.report_issue"), contextlib.redirect_stderr(io.StringIO()):
            state = pgl.run_cycle("testnet", log_dir=tempfile.mkdtemp())
        view = state["positions"][0]
        self.assertEqual(view["trailing"]["reason"], "position_closed")
        self.assertTrue(view["protected"])
        self.assertEqual(view["size"], 0.0)
        self.assertTrue(state["cycle_ok"])
        self.assertEqual(state["errors"], [])
        self.assertEqual([a["type"] for a in state["actions"]], [])
        self.assertEqual(fake.writes(), [])
        da.assert_not_called()

    def test_position_closed_keeps_the_excursion_and_reports_last_stop_r(self):
        # Round 3 (#182 interaction): no 1m klines read for the flattened position, its excursion keeps the last stop,
        # and the next cycle's position_closed action reports a numeric last_stop_r.
        ws = tempfile.mkdtemp()
        log_dir = os.path.join(ws, "logs")
        long_record(ws, sl_price=95.0, timestamp=int(time.time()) - 3600)

        def run(fake, calc):
            klines = KlineSource(lambda o: (100.5, 99.5))
            with offline(fake, workspace=ws), \
                 patch("utils.trade_excursion.fetch_klines_range", side_effect=klines), \
                 patch("dynamic_exit_manager.calculate_structural_stop", return_value=calc), \
                 patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)), \
                 patch("report_agent_issue.report_issue"), contextlib.redirect_stderr(io.StringIO()):
                return pgl.run_cycle("testnet", log_dir=log_dir), klines

        state, _ = run(FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)]), None)
        self.assertEqual(state["excursions"]["BTCUSDT|LONG"]["last_stop_price"], 95.0)

        # Reads: 1 guardian orphan audit, 2 dem, 3 dem's re-read (stop triggered, position flat).
        fake = EmptyRereadExchange([long_position(mark="101.0")], flat=True, at=3, algos=[stop(501, 95.0)])
        state, klines = run(fake, structural(100.5))
        view = state["positions"][0]
        self.assertEqual(view["trailing"]["reason"], "position_closed")
        self.assertEqual(view["stop_price"], 95.0, "the last stop read this cycle is kept")
        self.assertEqual([c for c in klines.calls if c[0] == "BTCUSDT"], [])
        self.assertEqual(state["excursions"]["BTCUSDT|LONG"]["last_stop_price"], 95.0)
        self.assertEqual([a for a in state["actions"] if a["type"] == "position_closed"], [])

        state, _ = run(FakeExchange([]), None)
        closed = [a for a in state["actions"] if a["type"] == "position_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["detail"]["last_stop_r"], -1.0)  # (95 - 100) / 5


# =============================================================================
# 8. move_sl_to_breakeven re-reads the stops before writing
# =============================================================================
class TestBreakEvenReread(unittest.TestCase):

    def _move(self, second, force=False):
        fake = RereadExchange([long_position()], second, algos=[stop(501, 95.0)])
        with offline(fake), patch("execute_futures_trade.get_atr_15m", return_value=2.0):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", force=force)
        return res, fake

    def test_fresher_stop_at_breakeven_is_a_noop(self):
        for force in (False, True):
            res, fake = self._move([stop(777, 100.5)], force=force)
            self.assertTrue(res["success"], res)
            self.assertEqual(res["reason"], "already_at_breakeven")
            self.assertIn("fresh read", res["message"])
            self.assertEqual(res["old_stop"]["algo_id"], 777)
            self.assertEqual(fake.writes(), [])

    def test_reread_failure_proceeds_with_the_first_read(self):
        res, fake = self._move({"code": -1001, "msg": "Internal error"})
        self.assertEqual(res["reason"], "moved", res)
        self.assertEqual(res["cancelled_old_stop_ids"], [501])

    def test_fresh_looser_stop_is_the_one_replaced(self):
        res, fake = self._move([stop(777, 96.0)])
        self.assertEqual(res["reason"], "moved", res)
        self.assertEqual(res["cancelled_old_stop_ids"], [777])
        self.assertEqual(res["old_stop"]["algo_id"], 777)
        self.assertEqual([s["algo_id"] for s in res["old_stops"]], [777])
        self.assertGreaterEqual(len(gets(fake, "/fapi/v1/openAlgoOrders")), 2)
        self.assertIn("previous stop 96.0", res["message"])


# =============================================================================
# Docs
# =============================================================================
class TestDocs(unittest.TestCase):

    def read(self, *parts):
        with open(os.path.join(BASE_DIR, *parts), "r", encoding="utf-8") as f:
            return f.read()

    def test_agents_md_tp1_trust_rule_within_cap(self):
        self.assertLess(os.path.getsize(os.path.join(BASE_DIR, "AGENTS.md")), 22000)
        layer8 = self.read("AGENTS.md").split("**Layer 8:", 1)[1].split("\n1. **Phase 1", 1)[0]
        self.assertIn("Unverified trade reference = TP1 unknown: no TP1-based BE or trail.", layer8)

    def test_skill_and_readme_operator_notes(self):
        skill = self.read(".agents", "skills", "trade-execution-planner", "SKILL.md")
        self.assertIn("tell the user not to log off or close the guardian console", skill)
        self.assertIn("MCP: userTrades is unavailable", skill)
        readme = self.read("README.md")
        self.assertIn("python3 scripts/execute_futures_trade.py --move-breakeven --symbol <S>", readme)


if __name__ == "__main__":
    unittest.main()
