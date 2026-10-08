#!/usr/bin/env python3
"""
test_issue_173_close_path_escalation.py - Offline tests for issue #173 (no network, no orders, no writes to logs/).

1. eft.heal_unknown_stop: shared unknown-stop heal (healed + redundant stops listed, -4130 = kept, failed, never a
   close, never raises); close_position_market uses it when every stop read failed.
2. Guardian: stop_unknown_cycles carried across cycles; at 3 the heal runs and a CRITICAL/P0 is filed; reset on a
   successful read; re-report at a later multiple only when the heal failed; --dry-run plans only.
3. Night cutoff SWING_STRUCTURAL_STOP with an unknown stop: heal (no close) + CRITICAL/P0.
4. Guardian dead-alpha close calls pt.dead_alpha_close_decision.
5. Night cutoff per-symbol try/except.
6. Ledger audit_read_error / audit_corrupt_lines and the doctor WARN.
7. Watchdog "stall check unavailable" wording; doctor watchdog read_error WARN text.
8-11. -1008 may exist; MCP NaN/Infinity rejected; step-size qty tolerance; late-index anchor listed as redundant.
12. offline() removes the temp workspace it created.
"""

import io
import os
import sys
import json
import math
import time
import shutil
import tempfile
import unittest
import contextlib
from decimal import Decimal
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
LOOPS_DIR = os.path.join(SCRIPTS_DIR, "loops")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (SCRIPTS_DIR, LOOPS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execute_futures_trade as eft
import trading_drift_watchdog as tdw
import night_cutoff_loop as ncl
import sync_session_state as sss
import trading_doctor
import position_guardian_loop as pgl
from utils import position_timing as pt
from test_exit_management import FakeExchange, offline, long_position, stop, ALGO_ENDPOINT, FILTERS
from test_issue_137_close_keeps_stop import CloseExchange, keys_env, posts_of, REJECT_2022
from test_issue_144_close_path_followups import REJECT_4130, READ_ERROR, ALGO_READ
from test_issue_92_holding_time import FillsExchange, STALLED, HEALTHY, row, fill, HOUR, DOCTOR_PROFILE
from test_issue_150_heal_sensor_followups import LateIndexExchange, audit_ws, write_state, doctor_send

NOW = int(time.time())
ORDER_ENDPOINT = "/fapi/v1/order"


def market_posts(fake):
    return [c for c in fake.calls if c[0] == "POST" and c[1] == ORDER_ENDPOINT and c[2].get("type") == "MARKET"]


class ToggleReadExchange(CloseExchange):
    """CloseExchange whose openAlgoOrders reads fail while fail_reads is True."""

    def __init__(self, positions, closes=("fill",), fail_reads=True, **kw):
        super().__init__(positions, list(closes), **kw)
        self.fail_reads = fail_reads

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if method == "GET" and endpoint == ALGO_READ and self.fail_reads:
            self.calls.append((method, endpoint, dict(params or {})))
            return dict(READ_ERROR)
        return super().__call__(method, endpoint, params, target_env, retry_count)


# =============================================================================
# 1. Shared helper
# =============================================================================
class TestHealUnknownStop(unittest.TestCase):

    def test_healed_lists_redundant_stops_never_cancels(self):
        qty_stop = dict(stop(777, 96.0, close_position=False), reduceOnly=True, quantity="10")
        fake = CloseExchange([long_position()], ["fill"], algos=[qty_stop])
        with keys_env(fake):
            res = eft.heal_unknown_stop("BTCUSDT", long_position(), target_env="prod")
        self.assertEqual(res["result"], "healed")
        self.assertTrue(res["heal"]["verified"])
        self.assertEqual([s["algo_id"] for s in res["redundant_stops"]], [777])
        self.assertIn("next flat close cancels the leftovers", res["note"])
        self.assertEqual(fake.deletes(), [])
        self.assertEqual(market_posts(fake), [])

    def test_4130_is_kept(self):
        fake = CloseExchange([long_position()], ["fill"], reject_new_stops=True, reject_response=REJECT_4130)
        with keys_env(fake):
            res = eft.heal_unknown_stop("BTCUSDT", long_position(), target_env="prod")
        self.assertEqual((res["result"], res["note"]), ("kept", "stop inferred from -4130"))
        self.assertEqual(res["redundant_stops"], [])
        self.assertEqual(market_posts(fake), [], "never a close")

    def test_other_rejection_fails_without_close(self):
        fake = CloseExchange([long_position()], ["fill"], reject_new_stops=True)
        with keys_env(fake):
            res = eft.heal_unknown_stop("BTCUSDT", long_position(), target_env="prod")
        self.assertEqual(res["result"], "failed")
        self.assertFalse(res["heal"]["closed"])
        self.assertEqual(market_posts(fake), [])

    def test_never_raises(self):
        with patch("execute_futures_trade.heal_orphan_position", side_effect=RuntimeError("boom")), \
                patch("execute_futures_trade._planned_sl_for_position", return_value=None):
            res = eft.heal_unknown_stop("BTCUSDT", long_position(), target_env="prod")
        self.assertEqual(res["result"], "failed")
        self.assertIn("boom", res["heal"]["reason"])

    def test_uses_close_on_failure_false_and_planned_sl(self):
        heal = MagicMock(return_value={"verified": False, "reason": "heal_stop_unverified"})
        with patch("execute_futures_trade.heal_orphan_position", heal), \
                patch("execute_futures_trade._planned_sl_for_position", return_value=99.0):
            eft.heal_unknown_stop("BTCUSDT", long_position(), target_env="prod")
        self.assertIs(heal.call_args.kwargs["close_on_failure"], False)
        self.assertEqual(heal.call_args.kwargs["planned_sl"], 99.0)

    def test_close_position_market_uses_the_helper(self):
        fake = ToggleReadExchange([long_position()], [REJECT_2022], reject_new_stops=True,
                                  reject_response=REJECT_4130)
        with keys_env(fake), patch("execute_futures_trade.heal_unknown_stop", wraps=eft.heal_unknown_stop) as helper:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        helper.assert_called_once()
        self.assertEqual((res["stop_source"], res["stop_protected"], res["stop_note"]),
                         ("kept", True, "stop inferred from -4130"))

    def test_close_position_market_skips_the_helper_after_a_good_read(self):
        fake = CloseExchange([long_position()], [REJECT_2022])
        with keys_env(fake), patch("execute_futures_trade.heal_unknown_stop") as helper:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        helper.assert_not_called()
        self.assertEqual(res["stop_source"], "healed")


# =============================================================================
# 2. Guardian persistent UNKNOWN stop
# =============================================================================
class TestGuardianUnknownStopEscalation(unittest.TestCase):

    def setUp(self):
        self.log_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.log_dir, True)

    @contextlib.contextmanager
    def env(self, fake, helper=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(offline(fake))
            stack.enter_context(patch.object(pgl, "DEFAULT_LOG_DIR", self.log_dir))
            stack.enter_context(patch("dynamic_exit_manager.calculate_structural_stop", return_value=None))
            stack.enter_context(patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            report = stack.enter_context(patch("report_agent_issue.report_issue"))
            if helper is not None:
                stack.enter_context(patch("execute_futures_trade.heal_unknown_stop", helper))
            yield report

    def cycle(self, dry_run=False):
        return pgl.run_cycle("testnet", dry_run=dry_run, log_dir=self.log_dir)

    def actions(self, state):
        return [a for a in state["actions"] if a["type"] == "stop_unknown_heal"]

    def test_escalates_at_three_with_real_heal_4130_kept(self):
        fake = ToggleReadExchange([long_position()], reject_new_stops=True, reject_response=REJECT_4130)
        with self.env(fake) as report:
            states = [self.cycle() for _ in range(3)]
        self.assertEqual([s["positions"][0]["stop_unknown_cycles"] for s in states], [1, 2, 3])
        self.assertEqual([len(self.actions(s)) for s in states], [0, 0, 1])
        act = self.actions(states[2])[0]
        self.assertTrue(act["success"])
        self.assertEqual((act["detail"]["result"], act["detail"]["stop_unknown_cycles"]), ("kept", 3))
        self.assertEqual(market_posts(fake), [], "an unknown stop is never closed")
        self.assertEqual(len(posts_of(fake, ALGO_ENDPOINT)), 1)
        report.assert_called_once()
        kw = report.call_args.kwargs
        self.assertEqual((kw["severity"], kw["priority"], kw["category"]), ("CRITICAL", "P0", "risk_gate"))
        self.assertIn("BTCUSDT", kw["title"])
        self.assertIn("3", kw["title"])
        self.assertIn("kept", kw["error_detail"])

    def test_counter_resets_after_a_successful_read(self):
        fake = ToggleReadExchange([long_position()], algos=[stop(501, 95.0)])
        helper = MagicMock(return_value={"result": "failed", "detail": "x", "heal": {}, "redundant_stops": []})
        with self.env(fake, helper) as report:
            self.cycle(), self.cycle()
            fake.fail_reads = False
            ok = self.cycle()
            fake.fail_reads = True
            states = [self.cycle(), self.cycle()]
        self.assertEqual(ok["positions"][0]["stop_unknown_cycles"], 0)
        self.assertEqual([s["positions"][0]["stop_unknown_cycles"] for s in states], [1, 2])
        helper.assert_not_called()
        report.assert_not_called()

    def test_reports_again_at_six_only_when_failed(self):
        for results, expected_reports in ((["failed", "failed"], 2), (["failed", "kept"], 1),
                                          (["healed", "failed"], 2)):
            with self.subTest(results=results):
                shutil.rmtree(self.log_dir, True)
                os.makedirs(self.log_dir)
                fake = ToggleReadExchange([long_position()])
                helper = MagicMock(side_effect=[{"result": r, "detail": r, "heal": {}, "redundant_stops": []}
                                                for r in results])
                with self.env(fake, helper) as report:
                    for _ in range(7):
                        self.cycle()
                self.assertEqual(helper.call_count, 2, "heal at cycles 3 and 6")
                self.assertEqual(report.call_count, expected_reports)
                for call in helper.call_args_list:
                    self.assertEqual(call.args[0], "BTCUSDT")

    def test_dry_run_plans_only(self):
        fake = ToggleReadExchange([long_position()])
        helper = MagicMock()
        with self.env(fake, helper) as report:
            states = [self.cycle(dry_run=True) for _ in range(3)]
        helper.assert_not_called()
        report.assert_not_called()
        act = self.actions(states[2])
        self.assertEqual(len(act), 1)
        self.assertTrue(act[0]["detail"]["planned"])
        self.assertEqual(fake.writes(), [])

    def test_healed_marks_the_view_protected(self):
        fake = ToggleReadExchange([long_position()])
        helper = MagicMock(return_value={"result": "healed", "detail": "ok", "redundant_stops": [],
                                         "heal": {"healed_sl_price": 97.5, "verified": True}})
        with self.env(fake, helper):
            state = [self.cycle() for _ in range(3)][-1]
        view = state["positions"][0]
        self.assertEqual((view["protected"], view["stop_price"]), (True, 97.5))
        self.assertFalse(state["cycle_ok"], "the unreadable stop read is still an error")


# =============================================================================
# 3. Night cutoff SWING with an unknown stop
# =============================================================================
class TestSwingUnknownStop(unittest.TestCase):

    def run_cutoff(self, fake, mode):
        out = io.StringIO()
        with keys_env(fake) as report, patch("os.system", return_value=0), contextlib.redirect_stdout(out):
            summary = ncl.run_night_cutoff(target_env="testnet", overnight_mode=mode)
        return summary, report, out.getvalue()

    def test_swing_heals_kept_without_close_and_reports(self):
        fake = ToggleReadExchange([long_position()], reject_new_stops=True, reject_response=REJECT_4130)
        summary, report, out = self.run_cutoff(fake, "SWING_STRUCTURAL_STOP")
        self.assertEqual(summary["stop_unknown"], ["BTCUSDT"])
        self.assertEqual(summary["stop_unknown_heals"][0]["symbol"], "BTCUSDT")
        self.assertEqual(summary["stop_unknown_heals"][0]["result"], "kept")
        self.assertEqual(summary["close_failures"], [])
        self.assertEqual(market_posts(fake), [])
        report.assert_called_once()
        self.assertEqual((report.call_args.kwargs["severity"], report.call_args.kwargs["priority"]), ("CRITICAL", "P0"))
        self.assertIn("Unknown-stop heal of BTCUSDT: kept", out)

    def test_zero_overnight_still_closes_without_heal(self):
        fake = ToggleReadExchange([long_position()])
        with patch("execute_futures_trade.heal_unknown_stop") as helper:
            summary, report, _ = self.run_cutoff(fake, "ZERO_OVERNIGHT_RISK")
        helper.assert_not_called()
        self.assertEqual(len(market_posts(fake)), 1)
        self.assertNotIn("stop_unknown_heals", summary)


# =============================================================================
# 4. Guardian shared dead-alpha rule
# =============================================================================
class TestGuardianSharedDeadAlphaRule(unittest.TestCase):

    def run_guardian(self, holding):
        log_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, log_dir, True)
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
                patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(STALLED)), \
                patch.object(pgl, "holding_verdict", return_value=dict(holding)), \
                patch.object(pgl.pt, "dead_alpha_close_decision", wraps=pt.dead_alpha_close_decision) as rule, \
                patch("execute_futures_trade.close_position_market", return_value={"success": True}) as close:
            state = pgl.run_cycle("testnet", close_dead_alpha=True, log_dir=log_dir)
        return state, rule, close

    def test_report_only_reason_comes_from_the_rule(self):
        state, rule, close = self.run_guardian({"verdict": "DEAD_ALPHA", "elapsed_hours": 5.0,
                                                "entry_time_source": "trades_audit"})
        rule.assert_called_once_with("DEAD_ALPHA", "trades_audit", "DEAD_ALPHA_STALLED")
        close.assert_not_called()
        da = state["positions"][0]["dead_alpha"]
        self.assertEqual((da["recommendation"], da["close_blocked"]),
                         ("REVIEW_MANUALLY", "entry time source trades_audit: report only"))

    def test_allowed_by_the_rule_closes(self):
        _, rule, close = self.run_guardian({"verdict": "DEAD_ALPHA", "elapsed_hours": 5.0,
                                            "entry_time_source": "userTrades"})
        rule.assert_called_once()
        close.assert_called_once_with("BTCUSDT", target_env="testnet")


# =============================================================================
# 5. Night cutoff per-symbol try/except
# =============================================================================
SOL = {"symbol": "SOLUSDT", "positionAmt": "1.0", "entryPrice": "150.0", "markPrice": "148.0",
       "unRealizedProfit": "-2.0", "isolatedMargin": "15.0"}
ETH = dict(SOL, symbol="ETHUSDT")


class TestCutoffPerSymbolTry(unittest.TestCase):

    def run_night(self, mode, close_side_effect=None, filters_side_effect=None):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v2/positionRisk":
                return [dict(SOL), dict(ETH)]
            if endpoint == ALGO_READ:
                return [{"orderType": "STOP_MARKET", "triggerPrice": "145.0"}]
            return []

        def filters(sym, target_env=None):
            if filters_side_effect and sym == "SOLUSDT":
                raise filters_side_effect
            return {"tickSize": 0.1, "precision_price": 1}
        close = MagicMock(side_effect=close_side_effect or (lambda sym, target_env=None: {"success": True}))
        out = io.StringIO()
        with patch("execute_futures_trade.send_signed_request", side_effect=send), \
                patch("execute_futures_trade.get_symbol_filters", side_effect=filters), \
                patch("user_profile.load_user_profile", return_value={"overnight_mode": mode}), \
                patch("execute_futures_trade.close_position_market", close), \
                patch("execute_futures_trade.heal_orphan_position") as heal, \
                patch("execute_futures_trade.move_sl_to_breakeven", return_value={"success": True}), \
                patch("report_agent_issue.report_issue"), \
                patch("time.sleep"), patch("os.system", return_value=0), contextlib.redirect_stdout(out):
            code = ncl.main(["--env", "testnet"])
        heal.assert_not_called()
        return code, out.getvalue(), close

    def closed(self, close):
        return [c.args[0] for c in close.call_args_list]

    def test_exception_before_close_gets_one_close_and_next_symbol_runs(self):
        code, out, close = self.run_night("ZERO_OVERNIGHT_RISK", filters_side_effect=TypeError("no filters"))
        self.assertEqual(code, 1)
        self.assertEqual(self.closed(close), ["SOLUSDT", "ETHUSDT"])
        self.assertIn("SOLUSDT processing failed at stage breakeven: TypeError: no filters", out)
        self.assertIn("close_failures: ['SOLUSDT']", out)
        self.assertIn("ETHUSDT closed at market", out)

    def test_raising_close_is_not_retried(self):
        def close(sym, target_env=None):
            if sym == "SOLUSDT":
                raise RuntimeError("socket closed")
            return {"success": True}
        code, out, mock_close = self.run_night("ZERO_OVERNIGHT_RISK", close_side_effect=close)
        self.assertEqual(code, 1)
        self.assertEqual(self.closed(mock_close), ["SOLUSDT", "ETHUSDT"], "one close per symbol")
        self.assertIn("SOLUSDT processing failed at stage close: RuntimeError: socket closed", out)
        self.assertIn("close_failures: ['SOLUSDT']", out)

    def test_fallback_close_raising_is_recorded(self):
        calls = []

        def close(sym, target_env=None):
            calls.append(sym)
            if sym == "SOLUSDT":
                raise RuntimeError("down")
            return {"success": True}
        code, out, _ = self.run_night("ZERO_OVERNIGHT_RISK", close_side_effect=close,
                                      filters_side_effect=TypeError("no filters"))
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["SOLUSDT", "ETHUSDT"])
        self.assertIn("Failed to close SOLUSDT at market: RuntimeError: down", out)
        self.assertIn("close_failures: ['SOLUSDT']", out)

    def test_swing_exception_never_closes(self):
        code, out, close = self.run_night("SWING_STRUCTURAL_STOP", filters_side_effect=TypeError("no filters"))
        self.assertEqual(code, 1)
        close.assert_not_called()
        self.assertIn("close_failures: ['SOLUSDT']", out)
        self.assertIn("Position ETHUSDT permitted overnight", out)

    def test_close_all_failure_still_processes_the_rest(self):
        def close(sym, target_env=None):
            if sym == "SOLUSDT":
                raise RuntimeError("x")
            return {"success": True}
        code, out, mock_close = self.run_night("CLOSE_ALL_AT_MARKET", close_side_effect=close)
        self.assertEqual(code, 1)
        self.assertEqual(self.closed(mock_close), ["SOLUSDT", "ETHUSDT"])
        self.assertIn("close_failures: ['SOLUSDT']", out)


# =============================================================================
# 6. Audit-read visibility
# =============================================================================
class TestAuditReadVisibility(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def read(self, audit_path):
        with patch.object(sss, "AUDIT_LOG", audit_path):
            return sss._read_audit_records()

    def test_unreadable_file(self):
        records, err, corrupt = self.read(self.tmp)   # a directory: exists, but open() fails
        self.assertEqual((records, corrupt), ([], 0))
        self.assertTrue(err)

    def test_corrupt_lines_counted(self):
        path = os.path.join(self.tmp, "trades_audit.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"symbol": "BTCUSDT"}) + "\n\nnot json\n[1]\n")
        records, err, corrupt = self.read(path)
        self.assertEqual((len(records), err, corrupt), (1, None, 2))

    def test_missing_file_is_fine(self):
        self.assertEqual(self.read(os.path.join(self.tmp, "missing.jsonl")), ([], None, 0))

    def sync(self, audit_path):
        fake = FillsExchange([row("BTCUSDT")], fills={"BTCUSDT": [fill("BUY", 1, NOW - HOUR)]})
        with patch.object(sss, "LOGS_DIR", self.tmp), \
                patch.object(sss, "STATE_FILE", os.path.join(self.tmp, "session_state.json")), \
                patch.object(sss, "AUDIT_LOG", audit_path), \
                patch.dict(sys.modules, {"shadow_tracker": None}), \
                patch("execute_futures_trade.send_signed_request", side_effect=fake):
            return sss.sync_session_state(target_env="testnet")

    def test_fields_in_the_ledger(self):
        state = self.sync(os.path.join(self.tmp, "missing.jsonl"))
        self.assertEqual((state["audit_read_error"], state["audit_corrupt_lines"]), (None, 0))
        bad_dir = os.path.join(self.tmp, "audit_dir")
        os.makedirs(bad_dir)
        state = self.sync(bad_dir)
        self.assertTrue(state["audit_read_error"])
        self.assertTrue(state["is_valid"])

    def run_doctor(self, ledger):
        state_file = os.path.join(self.tmp, "session_state.json")
        if os.path.exists(state_file):
            os.remove(state_file)   # a fresh ledger from an earlier run would skip the sync

        def sync(env):
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(dict(ledger, is_valid=True, target_env="testnet", last_updated_ts=NOW), f)
            return {"is_valid": True}
        resp = MagicMock()
        resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        out = io.StringIO()
        with patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
                patch("execute_futures_trade.get_client_config", return_value=("key12345678", "sec12345678", "http://x")), \
                patch("urllib.request.urlopen") as mock_urlopen, \
                patch("execute_futures_trade.send_signed_request",
                      side_effect=doctor_send([row("BTCUSDT")], [stop(501, 95.0)])), \
                patch("user_profile.load_user_profile", return_value=dict(DOCTOR_PROFILE)), \
                patch("trading_doctor.check_pretool_hook", return_value={"ok": True, "critical": [], "warnings": [], "info": []}), \
                patch("shadow_tracker.calculate_efficacy_metrics", side_effect=RuntimeError("skip")), \
                patch("trading_doctor.check_guardian_service", return_value=("ok", "guardian alive (stub)")), \
                patch.object(sss, "STATE_FILE", state_file), \
                patch("sync_session_state.sync_session_state", side_effect=sync), \
                patch("trading_drift_watchdog.audit_dead_alpha",
                      return_value={"dead_alpha_count": 0, "unknown_holding_symbols": []}), \
                patch("report_agent_issue.report_issue") as report, \
                contextlib.redirect_stdout(out):
            mock_urlopen.return_value.__enter__.return_value = resp
            code = trading_doctor.run_doctor(target_env="testnet")
        report.assert_not_called()
        return code, out.getvalue()

    def test_doctor_warns_on_unreadable_or_corrupt(self):
        for ledger, text in (({"audit_read_error": "PermissionError: denied", "audit_corrupt_lines": 0},
                              "is unreadable (PermissionError: denied)"),
                             ({"audit_read_error": None, "audit_corrupt_lines": 3}, "has 3 corrupt line(s)")):
            with self.subTest(ledger=ledger):
                code, out = self.run_doctor(ledger)
                self.assertIn(f"[STATE LEDGER] Ledger sync: logs/trades_audit.jsonl {text}", out)
                self.assertIn("./scripts/report_issue.sh", out)
                self.assertNotEqual(code, 1, "a WARN, never critical")

    def test_doctor_quiet_when_fine(self):
        _, out = self.run_doctor({"audit_read_error": None, "audit_corrupt_lines": 0})
        self.assertNotIn("trades_audit.jsonl", out)

    def test_other_env_ledger_ignored(self):
        self.assertIsNone(trading_doctor.ledger_audit_warning(
            {"target_env": "prod", "audit_read_error": "x"}, "testnet"))


# =============================================================================
# 7. Watchdog wording / doctor watchdog WARN
# =============================================================================
class TestWatchdogWording(unittest.TestCase):

    def run_watchdog(self, **stall):
        fake = FillsExchange([row("BTCUSDT")], fills={"BTCUSDT": [fill("BUY", 1, NOW - 6 * HOUR)]},
                             algos=[stop(501, 95.0)])
        out = io.StringIO()
        with offline(fake), patch("dynamic_exit_manager.check_dead_alpha_timeout", **stall), \
                patch("execute_futures_trade.close_position_market") as close, contextlib.redirect_stdout(out):
            tdw.audit_dead_alpha(target_env="testnet", auto_exit=True)
        close.assert_not_called()
        return out.getvalue()

    def test_unknown_stall_says_unavailable(self):
        out = self.run_watchdog(side_effect=RuntimeError("klines down"))
        self.assertIn("stall check unavailable; review manually", out)
        self.assertNotIn("Coiling", out)

    def test_healthy_range_keeps_coiling(self):
        out = self.run_watchdog(return_value=dict(HEALTHY))
        self.assertIn("Coiling/active range; review manually", out)

    def test_doctor_watchdog_read_error_warn_text(self):
        import test_issue_150_heal_sensor_followups as t150
        wd = MagicMock(return_value={"read_error": "positionRisk unreadable: x", "dead_alpha_count": 0,
                                     "unknown_holding_symbols": []})
        code, out, _, _ = t150.run_doctor(doctor_send([row("BTCUSDT")], [stop(501, 95.0)]), watchdog=wd)
        self.assertIn("watchdog read error: its view may diverge from the exchange", out)
        self.assertIn("OPERATIONAL WITH WARNINGS", out)
        self.assertNotEqual(code, 1)


# =============================================================================
# 8-11. Robustness items
# =============================================================================
class TestRobustness(unittest.TestCase):

    def test_1008_may_exist(self):
        self.assertFalse(eft._is_explicit_rejection({"code": -1008, "msg": "Server is currently overloaded"}))
        wait = MagicMock(return_value=(False, None))
        fake = FakeExchange([long_position()], reject_new_stops=True, reject_response={"code": -1008, "msg": "x"})
        with offline(fake), patch("execute_futures_trade.wait_for_stop_confirmation", wait):
            eft.heal_orphan_position(long_position(), target_env="prod", planned_sl=99.0)
        self.assertEqual([c.args[2] for c in wait.call_args_list], [99.0, 97.5, 99.0], "-1008 waits like a timeout")

    def test_mcp_encoder_rejects_non_finite(self):
        for bad in (float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    eft._mcp_json_dumps({"params": {"quantity": bad}})
        self.assertEqual(json.loads(eft._mcp_json_dumps({"q": 1.5})), {"q": 1.5})

    @patch("execute_futures_trade.get_mcp_oauth_token", return_value="fake_token")
    @patch("urllib.request.urlopen")
    def test_call_binance_mcp_never_sends_nan(self, mock_urlopen, _token):
        with contextlib.redirect_stderr(io.StringIO()):
            res = eft.call_binance_mcp("futures_usds.newOrder", {"symbol": "BTCUSDT", "quantity": math.nan})
        self.assertTrue(res["isError"])
        self.assertIn("MCP Gateway Error", res["error"])
        mock_urlopen.assert_not_called()

    def planned(self, qty, filters=None, filters_error=None):
        ws = audit_ws(self)
        write_state(ws, position={"qty": qty})
        kw = {"side_effect": filters_error} if filters_error else {"return_value": filters or dict(FILTERS)}
        with patch("execute_futures_trade._workspace_dir", return_value=ws), \
                patch("execute_futures_trade.get_symbol_filters", **kw):
            return eft._planned_sl_for_position("BTCUSDT", long_position(), "prod")

    def test_step_size_qty_tolerance(self):
        # stepSize 0.001 -> tolerance 0.0005 for a 10-unit position
        self.assertEqual(self.planned(9.9996), 100.2, "within half a step: the ratcheted SL is used")
        self.assertEqual(self.planned(9.9994), 99.0, "beyond half a step: the audit SL")
        self.assertEqual(self.planned(9.9996, filters_error=RuntimeError("exchangeInfo down")), 99.0,
                         "filters unreadable: 1e-9 tolerance")
        self.assertEqual(self.planned(9.9996, filters_error=TypeError("None filters")), 99.0)

    def test_late_index_lists_accepted_anchor(self):
        class AcceptedAnchorExchange(FakeExchange):
            """Both placements accepted with an id; the planned one appears after `hidden_reads` reads, the anchor
            never does."""

            def __init__(self, positions, hidden_reads=5):
                super().__init__(positions, index_new_stops=False)
                self.hidden_reads, self.planned, self.reads_since = hidden_reads, None, 0

            def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
                res = super().__call__(method, endpoint, params, target_env, retry_count)
                if method == "POST" and endpoint == ALGO_ENDPOINT and self.planned is None:
                    self.planned = {"algoId": res["algoId"], "symbol": params["symbol"], "side": params["side"],
                                    "orderType": params["type"], "triggerPrice": str(params["triggerPrice"]),
                                    "closePosition": True, "reduceOnly": False}
                if method == "GET" and endpoint == ALGO_READ and self.planned is not None:
                    self.reads_since += 1
                    if self.reads_since > self.hidden_reads and self.planned not in self.algos:
                        self.algos.append(self.planned)
                return res
        fake = AcceptedAnchorExchange([long_position()])
        with offline(fake):
            res = eft.heal_orphan_position(long_position(), target_env="prod", planned_sl=99.0)
        self.assertEqual((res["verified"], res["sl_source"]), (True, "planned_sl_late_indexed"))
        anchor_id = res["anchor_attempt"]["placement"]["algoId"]
        self.assertNotEqual(anchor_id, fake.planned["algoId"])
        self.assertEqual(res["redundant_stops"], [{"algo_id": anchor_id, "trigger_price": 97.5,
                                                   "sl_source": "anchor_after_planned_rejected"}])
        self.assertEqual([c for c in fake.calls if c[0] == "DELETE"], [], "listed, never cancelled")

    def test_late_index_with_rejected_anchor_lists_nothing(self):
        fake = LateIndexExchange([long_position()])
        with offline(fake):
            res = eft.heal_orphan_position(long_position(), target_env="prod", planned_sl=99.0)
        self.assertEqual(res["sl_source"], "planned_sl_late_indexed")
        self.assertNotIn("redundant_stops", res)


# =============================================================================
# 12. Test hygiene
# =============================================================================
class TestHygiene(unittest.TestCase):

    def test_offline_removes_its_temp_workspace(self):
        with offline(FakeExchange([])) as ws:
            self.assertTrue(os.path.isdir(ws))
        self.assertFalse(os.path.exists(ws))

    def test_offline_keeps_a_given_workspace(self):
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        with offline(FakeExchange([]), workspace=ws):
            pass
        self.assertTrue(os.path.isdir(ws))


if __name__ == "__main__":
    unittest.main()
