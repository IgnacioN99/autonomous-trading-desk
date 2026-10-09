#!/usr/bin/env python3
"""
test_issue_180_profile_fallback.py - Offline tests for issue #180 (profile fallback follow-ups of #156).

1. PROD openings (standard and YOLO) are rejected before any exchange call when the profile did not come from
   config/user_profile.json (`_profile_source` "example" / "default"); "user" and a profile without the marker pass;
   TESTNET is unchanged; a loader exception keeps the #207 visible-not-blocking behaviour; no stored Gate 2 cap from
   a fallback profile (placement_loss_cap).
2. trading_doctor.check_user_profile_source: CRITICAL in PROD for a non-user source, wired into run_doctor's 3b.
3. protect-pending: a FILLED record deferred on a non-user profile reports on the first deferral (stage
   loss_cap_profile); resting records and equity-read deferrals still report at PENDING_DEFERRAL_REPORT_AFTER.
4. AGENTS.md states the post-fill ceiling (x 1.25 x 1.2).

No network: every exchange call is faked, user_profile.load_user_profile and report_agent_issue.report_issue are
always mocked, files go to temp dirs.
"""

import io
import os
import sys
import time
import json
import tempfile
import unittest
import contextlib
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
LOOPS_DIR = os.path.join(SCRIPTS_DIR, "loops")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, LOOPS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import trading_doctor
import test_pending_entries as tpe
from test_pending_entries import ExecutorHarness, make_record, write_registry, read_registry, entry_algo
from test_exit_management import FakeExchange, long_position, offline
from test_issue_36_prearm_resting_stop import PROD_PROFILE, types
from test_issue_156_157_stop_lifecycle import protect_runs, ReporterMocked
import test_issue_207_daily_loss_gate as t207
from test_issue_207_daily_loss_gate import DayHarness

FALLBACK_SOURCES = ("example", "default")


# ---------------------------------------------------------------------------------------------
# 1. PROD openings on a fallback profile
# ---------------------------------------------------------------------------------------------
class TestFallbackProfilePredicate(unittest.TestCase):

    def test_only_a_present_non_user_source_is_a_fallback(self):
        self.assertEqual(eft.fallback_profile_source({"_profile_source": "example"}), "example")
        self.assertEqual(eft.fallback_profile_source({"_profile_source": "default"}), "default")
        self.assertIsNone(eft.fallback_profile_source({"_profile_source": "user"}))
        self.assertIsNone(eft.fallback_profile_source({"risk_pct_equity": 0.005}))
        self.assertIsNone(eft.fallback_profile_source({}))
        self.assertIsNone(eft.fallback_profile_source(None))
        self.assertIsNone(eft.fallback_profile_source(["example"]))


class TestProdEntryRejectsFallbackProfile(ExecutorHarness):

    def execute_with(self, profile, **kwargs):
        """ExecutorHarness.execute with its load_user_profile mock returning `profile`."""
        with patch.object(tpe, "EX_PROFILE", profile):
            return self.execute(**kwargs)

    def assert_rejected_before_any_call(self, res, source):
        self.assertFalse(res["success"])
        self.assertIs(res["hard_gate_rejection"], True)
        self.assertIn(f"fallback profile: {source}", res["error"])
        self.assertIn("config/user_profile.json", res["error"])
        self.assertEqual(self.calls, [], "no exchange call (filters, ticker, snapshot, leverage, margin, order)")

    def test_prod_standard_entry_rejected_for_each_fallback_source(self):
        for source in FALLBACK_SOURCES:
            self.calls = []
            res = self.execute_with(dict(tpe.EX_PROFILE, _profile_source=source), env="prod")
            self.assert_rejected_before_any_call(res, source)

    def test_prod_yolo_entry_rejected(self):
        res = self.execute_with(dict(tpe.EX_PROFILE, _profile_source="example"), env="prod", is_yolo=True,
                                leverage=15)
        self.assert_rejected_before_any_call(res, "example")

    def test_prod_resting_entry_rejected(self):
        tpe.write_guardian_state(self.ws)
        res = self.execute_with(dict(tpe.EX_PROFILE, _profile_source="default"), env="prod",
                                order_type="STOP_MARKET", trigger_price=102.347)
        self.assert_rejected_before_any_call(res, "default")
        self.assertIsNone(read_registry(self.ws))

    def test_prod_user_profile_passes_and_stores_the_cap(self):
        tpe.write_guardian_state(self.ws)
        res = self.execute_with(dict(tpe.EX_PROFILE, _profile_source="user"), env="prod",
                                order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        rec = read_registry(self.ws)["prod:SOLUSDT:8"]
        self.assertAlmostEqual(rec["gate2_loss_cap_usdt"], 10000.0 * 0.005 * 1.25)

    def test_prod_profile_without_marker_passes(self):
        # Mocked loaders (and older callers) return no _profile_source: not treated as a fallback.
        res = self.execute(env="prod")
        self.assertTrue(res["success"], res.get("error"))

    def test_testnet_fallback_profile_not_rejected(self):
        res = self.execute_with(dict(tpe.EX_PROFILE, _profile_source="example"), env="testnet")
        self.assertTrue(res["success"], res.get("error"))
        self.assertNotEqual(self.writes(), [])

    def test_placement_loss_cap_not_stored_for_a_fallback_profile(self):
        # Defense in depth: the entry gate (first call) is passed, placement_loss_cap (second call) sees a fallback.
        tpe.write_guardian_state(self.ws)
        with patch.object(eft, "fallback_profile_source", side_effect=[None, "example"]) as pred:
            res = self.execute_with(dict(tpe.EX_PROFILE, _profile_source="user"), env="prod",
                                    order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(pred.call_count, 2)
        self.assertNotIn("gate2_loss_cap_usdt", read_registry(self.ws)["prod:SOLUSDT:8"])


class TestUnreadableProfileUnchanged(DayHarness):

    def test_loader_exception_stays_visible_not_blocking(self):
        """A loader exception (prof = {}) is not a fallback source: the #207 behaviour is kept (owner decision)."""
        with contextlib.redirect_stderr(io.StringIO()), \
             patch("user_profile.load_user_profile", side_effect=ValueError("bad json")):
            # The #207 helper (DayHarness fixture, caller-controlled loader); its test class is not imported here.
            res = t207.TestExecutorIntegration._execute_without_profile_patch(self)
        self.assertTrue(res["success"], res.get("error"))
        self.assertIs(tpe.read_jsonl(self.ws, "trades_audit.jsonl")[-1]["profile_unreadable"], True)


# ---------------------------------------------------------------------------------------------
# 2. Doctor
# ---------------------------------------------------------------------------------------------
class TestDoctorProfileSource(unittest.TestCase):

    def test_prod_non_user_source_is_critical(self):
        for source in FALLBACK_SOURCES:
            level, msg = trading_doctor.check_user_profile_source({"_profile_source": source}, "prod")
            self.assertEqual(level, "critical")
            self.assertIn("config/user_profile.json is missing or unreadable", msg)
            self.assertIn(source, msg)

    def test_user_and_missing_marker_ok(self):
        for profile in ({"_profile_source": "user"}, {"profile_completed": True}, {}):
            self.assertEqual(trading_doctor.check_user_profile_source(profile, "prod")[0], "ok")

    def test_testnet_non_user_source_no_new_critical(self):
        self.assertEqual(trading_doctor.check_user_profile_source({"_profile_source": "example"}, "testnet")[0], "ok")

    def run_doctor_until_3b(self, profile, env):
        """run_doctor through its 3b profile check; stopped at 3c (find_workspace_root raises). Returns stdout."""
        class StopAfter3b(Exception):
            pass

        resp = MagicMock()
        resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        out = io.StringIO()
        with patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.get_client_config",
                   return_value=("key12345678", "sec12345678", "http://binance.mock")), \
             patch("urllib.request.urlopen") as urlopen, \
             patch("execute_futures_trade.send_signed_request",
                   return_value=[{"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}]), \
             patch("user_profile.load_user_profile", return_value=profile), \
             patch("trading_doctor.find_workspace_root", side_effect=StopAfter3b()), \
             contextlib.redirect_stdout(out):
            urlopen.return_value.__enter__.return_value = resp
            with self.assertRaises(StopAfter3b):
                trading_doctor.run_doctor(target_env=env)
        return out.getvalue()

    def test_run_doctor_reports_the_fallback_in_prod_only(self):
        profile = {"profile_completed": True, "risk_pct_equity": 0.005, "_profile_source": "example"}
        self.assertIn("config/user_profile.json is missing or unreadable", self.run_doctor_until_3b(profile, "prod"))
        self.assertNotIn("missing or unreadable", self.run_doctor_until_3b(profile, "testnet"))
        user = dict(profile, _profile_source="user")
        self.assertNotIn("missing or unreadable", self.run_doctor_until_3b(user, "prod"))


# ---------------------------------------------------------------------------------------------
# 3. Faster escalation for filled records
# ---------------------------------------------------------------------------------------------
class TestFilledRecordProfileDeferral(ReporterMocked):

    KEY = "prod:BTCUSDT:7001"

    def filled(self, **rec_extra):
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(env="prod", **rec_extra))
        return ws, FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])

    def test_first_deferral_reports_once_and_protection_proceeds(self):
        for source in FALLBACK_SOURCES:
            self.report.reset_mock()
            ws, fake = self.filled()
            res = protect_runs(fake, ws, 1, 1000.0, profile=dict(PROD_PROFILE, _profile_source=source))[0]
            self.assertTrue(res["ok"], res["errors"])
            self.assertEqual(types(res), ["pending_protect_sl", "pending_tp_placed"], "never blocks protection")
            self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_profile"])
            self.assertIn(f"source: {source}", res["warnings"][0]["warning"])
            self.report.assert_called_once()
            kw = self.report.call_args.kwargs
            self.assertEqual((kw["severity"], kw["category"]), ("HIGH", "risk_gate"))
            self.assertIn("filled BTCUSDT", kw["title"])
            self.assertIn("config/user_profile.json", kw["title"])
            self.assertEqual(kw["error_detail"], "BTCUSDT filled record loss-cap check deferred: non-user profile")

    def test_second_consecutive_deferral_does_not_report_again(self):
        ws, fake = self.filled(check_deferrals=1)
        res = protect_runs(fake, ws, 1, 1000.0, profile=dict(PROD_PROFILE, _profile_source="example"))[0]
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_profile"])
        self.report.assert_not_called()

    def test_profile_deferral_after_other_deferrals_reports_at_the_threshold(self):
        ws, fake = self.filled(check_deferrals=eft.PENDING_DEFERRAL_REPORT_AFTER - 1)
        protect_runs(fake, ws, 1, 1000.0, profile=dict(PROD_PROFILE, _profile_source="example"))
        self.report.assert_called_once()
        first_ws, first_fake = self.filled()
        protect_runs(first_fake, first_ws, 1, 1000.0, profile=dict(PROD_PROFILE, _profile_source="example"))
        (_, at_threshold), (_, at_first) = [(c.args, c.kwargs) for c in self.report.call_args_list]
        self.assertEqual((at_threshold["title"], at_threshold["error_detail"]),
                         (at_first["title"], at_first["error_detail"]), "same fingerprint: the 24h dedup holds")

    def test_dry_run_never_reports(self):
        ws, fake = self.filled()
        with patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("quant_risk_engine.get_account_equity", return_value=1000.0), \
             offline(fake, workspace=ws, profile=dict(PROD_PROFILE, _profile_source="example")):
            res = eft.protect_pending_entries(target_env="prod", dry_run=True)
        self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_profile"])
        self.report.assert_not_called()

    def test_filled_equity_read_deferral_still_waits_for_the_threshold(self):
        ws, fake = self.filled()
        res = protect_runs(fake, ws, 1, RuntimeError("balance timeout"),
                           profile=dict(PROD_PROFILE, _profile_source="user"))[0]
        self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_check"])
        self.report.assert_not_called()

    def test_resting_record_non_user_profile_reports_at_three(self):
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(env="prod"))
        fake = FakeExchange([], algos=[entry_algo()])
        out = protect_runs(fake, ws, eft.PENDING_DEFERRAL_REPORT_AFTER, 1000.0,
                           profile=dict(PROD_PROFILE, _profile_source="example"))
        for res in out:
            self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_check"])
        self.assertEqual(read_registry(ws)[self.KEY]["check_deferrals"], eft.PENDING_DEFERRAL_REPORT_AFTER)
        self.report.assert_called_once()
        self.assertIn("record checks of BTCUSDT deferred 3 consecutive runs", self.report.call_args.kwargs["title"])


# ---------------------------------------------------------------------------------------------
# 4. AGENTS.md
# ---------------------------------------------------------------------------------------------
class TestAgentsDocumentsTheCeiling(unittest.TestCase):

    def test_post_fill_ceiling_sentence(self):
        with open(os.path.join(BASE_DIR, "AGENTS.md"), "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")
        self.assertIn("Post-fill ceiling: live equity × `risk_pct_equity` × 1.25 × 1.2 (≈ 1.5× risk)", text)
        self.assertIn("PROD entries need `config/user_profile.json` (no fallback)", text)
        self.assertLess(len(raw), 22000)


if __name__ == "__main__":
    unittest.main()
