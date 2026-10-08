#!/usr/bin/env python3
"""
test_issue_156_157_stop_lifecycle.py - Offline tests for issues #156 and #157 (non-TESTNET items).

#156 protect-pending loss-cap re-check (PROD): a filled record is sized on max(total_qty, |positionAmt|); a YOLO
     record without leverage is untrusted; a FILLED record whose loss exceeds the live cap only by equity drift (within
     min(stored Gate 2 cap, live x 1.2)) stays trusted with a warning; consecutive deferrals are counted and reported
     once; a profile that is not the user's defers the check; pending_tp_placed shows fill_quality_flags; the guardian
     forwards protect-pending warnings (never errors).
#157 pre-arm anomalies are reported (not -2021, never skipped:*); -4130 with failed confirmation reads gets one more
     listing; MARKET-entry stops are verified by algo id only; prearm_price is the listed trigger; _prearm_note wording.

No network: every exchange call is faked, report_agent_issue.report_issue is always mocked, files go to temp dirs.
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
import user_profile as up
from utils.gate_limits import PENDING_DRIFT_CAP_TOLERANCE
from test_exit_management import FakeExchange, offline, long_position, stop, ALGO_ENDPOINT
from test_pending_entries import (ExecutorHarness, make_record, write_registry, read_registry, entry_algo, posts,
                                  write_guardian_state, ORDER_ENDPOINT, EX_FILTERS, EX_PROFILE, HEALTHY)
from test_issue_36_prearm_resting_stop import run_protect, types, deletes, PROD_PROFILE, MINUS_4130, MINUS_2021

ALGO_READ = "/fapi/v1/openAlgoOrders"
READ_ERROR = {"code": -1003, "msg": "Too many requests"}


def eth_loss_row(upnl="-1000"):
    """An open position elsewhere whose unrealized loss lowers the live Gate 2 cap (all-symbol positionRisk only)."""
    return dict(long_position(symbol="ETHUSDT", amt="1", entry="100", mark="90"), unRealizedProfit=upnl)


class ReporterMocked(unittest.TestCase):
    """report_agent_issue.report_issue is mocked in every test (no issue is ever filed or queued from tests)."""

    def setUp(self):
        p = patch("report_agent_issue.report_issue")
        self.report = p.start()
        self.addCleanup(p.stop)


# ---------------------------------------------------------------------------------------------
# #156.1 filled sizing
# ---------------------------------------------------------------------------------------------
class TestFilledSizing(ReporterMocked):
    """Record total_qty 6 (SL 95 from 101: loss 36); wallet 8000 -> cap 8000 x 0.625% = 50."""

    def test_position_larger_than_edited_total_qty_breaches(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, ws, _ = run_protect(fake, make_record(env="prod", total_qty=6.0), env="prod", equity=8000.0)
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertEqual(res["actions"][0]["detail"]["reason"], "filled_with_invalid_sl")
        self.assertIn("72.00 USDT for 12.0", " ".join(res["actions"][0]["detail"]["mismatches"]))
        self.assertEqual(posts(fake, ORDER_ENDPOINT), [], "no TP from an untrusted record")

    def test_position_matching_total_qty_within_cap(self):
        fake = FakeExchange([long_position(amt="6", entry="101.0", mark="101.5")])
        res, _, _ = run_protect(fake, make_record(env="prod", total_qty=6.0), env="prod", equity=8000.0)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_protect_sl", "pending_tp_placed"])

    def test_pure_helper_uses_the_larger_quantity(self):
        rec = make_record(env="prod", total_qty=6.0)
        self.assertIsNone(eft.record_loss_cap_problem(rec, 101.0, 8000.0, PROD_PROFILE))
        self.assertIsNone(eft.record_loss_cap_problem(rec, 101.0, 8000.0, PROD_PROFILE, position_qty=-4.0))
        self.assertIsNotNone(eft.record_loss_cap_problem(rec, 101.0, 8000.0, PROD_PROFILE, position_qty=-12.0))


# ---------------------------------------------------------------------------------------------
# #156.2 YOLO leverage
# ---------------------------------------------------------------------------------------------
class TestYoloLeverage(ReporterMocked):

    def test_resting_yolo_record_without_leverage_untrusted(self):
        rec = make_record(env="prod", is_yolo=True)
        rec.pop("leverage")
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws, mock_eq = run_protect(fake, rec, env="prod", equity=RuntimeError("must not be read"))
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertIn("yolo record without leverage", " ".join(res["actions"][0]["detail"]["mismatches"]))
        self.assertEqual(deletes(fake), [{"symbol": "BTCUSDT", "algoId": 7001}])
        mock_eq.assert_not_called()
        self.assertEqual(read_registry(ws), {})

    def test_filled_yolo_record_without_leverage_uses_the_live_leverage(self):
        rec = make_record(env="prod", is_yolo=True)
        rec.pop("leverage")
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5", leverage="3")])
        res, _, _ = run_protect(fake, rec, env="prod")
        self.assertEqual(types(res), ["pending_protect_sl", "pending_tp_placed"])

    def test_standard_record_without_leverage_unchanged(self):
        rec = make_record(env="prod")
        rec.pop("leverage")
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws, _ = run_protect(fake, rec, env="prod", equity=20000.0)   # cap 125 >= 72
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["actions"], [])
        self.assertIn("prod:BTCUSDT:7001", read_registry(ws))


# ---------------------------------------------------------------------------------------------
# #156.3 equity drift at fill
# ---------------------------------------------------------------------------------------------
class TestEquityDrift(ReporterMocked):
    """Loss at SL 72. Filled: wallet 12000, uPnL +100 (BTC) - 1000 (ETH) -> live cap 11100 x 0.625% = 69.375."""

    def filled(self, **rec_extra):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5"), eth_loss_row()])
        return fake, make_record(env="prod", **rec_extra)

    def test_drift_within_tolerance_keeps_filled_record_trusted(self):
        fake, rec = self.filled(gate2_loss_cap_usdt=75.0)
        res, ws, _ = run_protect(fake, rec, env="prod", equity=12000.0)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_protect_sl", "pending_tp_placed"])
        self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_drift"])
        text = res["warnings"][0]["warning"]
        for fragment in ("72.00", "69.38", "75.00"):
            self.assertIn(fragment, text)
        self.assertEqual(read_registry(ws), {})

    def test_drift_above_tolerance_untrusted(self):
        # wallet 9000, uPnL +100 -> live cap 56.25; 56.25 x 1.2 = 67.5 < 72 although the stored cap is 75
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, _, _ = run_protect(fake, make_record(env="prod", gate2_loss_cap_usdt=75.0), env="prod", equity=9000.0)
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertEqual(res["actions"][0]["detail"]["reason"], "filled_with_invalid_sl")
        self.assertEqual(PENDING_DRIFT_CAP_TOLERANCE, 1.2)

    def test_forged_huge_stored_cap_bounded_by_live_tolerance(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, _, _ = run_protect(fake, make_record(env="prod", gate2_loss_cap_usdt=1e9), env="prod", equity=9000.0)
        self.assertEqual(types(res), ["pending_record_mismatch"])

    def test_loss_above_stored_cap_untrusted(self):
        fake, rec = self.filled(gate2_loss_cap_usdt=70.0)   # min(70, 83.25) < 72
        res, _, _ = run_protect(fake, rec, env="prod", equity=12000.0)
        self.assertEqual(types(res), ["pending_record_mismatch"])

    def test_resting_entry_with_drift_cancelled_as_before(self):
        # wallet 12000, ETH uPnL -1000 -> live cap 68.75 < 72: the live cap governs a resting entry
        fake = FakeExchange([eth_loss_row()], algos=[entry_algo()])
        res, ws, _ = run_protect(fake, make_record(env="prod", gate2_loss_cap_usdt=75.0), env="prod", equity=12000.0)
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertEqual(res["actions"][0]["detail"]["reason"], "resting_entry_mismatch")
        self.assertEqual(deletes(fake), [{"symbol": "BTCUSDT", "algoId": 7001}])
        self.assertEqual(read_registry(ws), {})

    def test_yolo_live_leverage_increase_is_not_drift(self):
        # record 3x: stored YOLO cap 101 x 12 / 3 x 0.35 = 141.4; live 6x halves it to 70.7 < 72 (within x1.2)
        rec = make_record(env="prod", is_yolo=True, leverage=3, gate2_loss_cap_usdt=141.4)
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5", leverage="6")])
        res, _, _ = run_protect(fake, rec, env="prod")
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertIn("YOLO margin cap at 6x", " ".join(res["actions"][0]["detail"]["mismatches"]))
        self.assertNotIn("warnings", res)

    def test_legacy_record_without_stored_cap_breaches_as_before(self):
        fake, rec = self.filled()
        res, _, _ = run_protect(fake, rec, env="prod", equity=12000.0)
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertNotIn("warnings", res)


class TestStoredCapAtRegistration(ExecutorHarness):

    def setUp(self):
        super().setUp()
        p = patch("report_agent_issue.report_issue")
        p.start()
        self.addCleanup(p.stop)

    def test_prod_resting_entry_stores_the_gate_2_cap(self):
        write_guardian_state(self.ws)
        res = self.execute(env="prod", order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        rec = read_registry(self.ws)["prod:SOLUSDT:8"]
        self.assertAlmostEqual(rec["gate2_loss_cap_usdt"], 10000.0 * 0.005 * 1.25)   # no open position: uPnL 0
        self.assertEqual(eft.PENDING_ENTRIES_SCHEMA_VERSION, 2)

    def test_testnet_record_has_no_stored_cap(self):
        self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertNotIn("gate2_loss_cap_usdt", read_registry(self.ws)["testnet:SOLUSDT:8"])


# ---------------------------------------------------------------------------------------------
# #156.4 repeated deferrals, #156.5 profile fallback
# ---------------------------------------------------------------------------------------------
def protect_runs(fake, ws, runs, equity, env="prod", profile=None, filters=True):
    """protect_pending_entries `runs` times on the same workspace (the registry persists between runs)."""
    eq = patch("quant_risk_engine.get_account_equity", side_effect=equity) if isinstance(equity, Exception) else \
        patch("quant_risk_engine.get_account_equity", return_value=equity)
    out = []
    with offline(fake, workspace=ws, profile=profile or PROD_PROFILE), eq, \
         patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
        with contextlib.ExitStack() as stack:
            if not filters:
                stack.enter_context(patch("execute_futures_trade.get_symbol_filters", return_value=None))
            for _ in range(runs):
                out.append(eft.protect_pending_entries(target_env=env))
    return out


class TestRepeatedDeferrals(ReporterMocked):

    KEY = "prod:BTCUSDT:7001"

    def test_counter_increments_reports_once_at_three_and_resets(self):
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(env="prod"))
        fake = FakeExchange([], algos=[entry_algo()])
        counts = []
        for _ in range(4):
            res = protect_runs(fake, ws, 1, RuntimeError("balance timeout"))[0]
            self.assertTrue(res["ok"], res["errors"])
            self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_check"])
            counts.append(read_registry(ws)[self.KEY]["check_deferrals"])
        self.assertEqual(counts, [1, 2, 3, 4])
        self.assertEqual(eft.PENDING_DEFERRAL_REPORT_AFTER, 3)
        self.report.assert_called_once()
        kw = self.report.call_args.kwargs
        self.assertEqual((kw["severity"], kw["category"]), ("HIGH", "risk_gate"))
        self.assertIn("BTCUSDT", kw["title"])
        self.assertIn("deferred", kw["title"])
        self.assertEqual(fake.writes(), [])
        res = protect_runs(fake, ws, 1, 20000.0)[0]   # both checks ran: reset
        self.assertTrue(res["ok"], res["errors"])
        self.assertNotIn("check_deferrals", read_registry(ws)[self.KEY])
        self.report.assert_called_once()

    def test_success_between_deferrals_resets_the_count(self):
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(env="prod"))
        fake = FakeExchange([], algos=[entry_algo()])
        protect_runs(fake, ws, 2, RuntimeError("balance timeout"))
        protect_runs(fake, ws, 1, 20000.0)
        protect_runs(fake, ws, 2, RuntimeError("balance timeout"))
        self.assertEqual(read_registry(ws)[self.KEY]["check_deferrals"], 2)
        self.report.assert_not_called()

    def test_qty_check_deferral_counts(self):
        # total_qty 12 below the record's own sizing (margin 600 x 3 / 101 = 17.8): flagged, no stepSize anywhere
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(margin_usdt=600.0))
        fake = FakeExchange([], algos=[entry_algo()])
        out = protect_runs(fake, ws, 3, 10000.0, env="testnet", filters=False)
        self.assertEqual([w["stage"] for w in out[-1]["warnings"]], ["qty_check"])
        self.assertEqual(read_registry(ws)["testnet:BTCUSDT:7001"]["check_deferrals"], 3)
        self.report.assert_called_once()

    def test_report_failure_is_a_warning_only(self):
        self.report.side_effect = RuntimeError("gh down")
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(env="prod", check_deferrals=2))
        fake = FakeExchange([], algos=[entry_algo()])
        res = protect_runs(fake, ws, 1, RuntimeError("balance timeout"))[0]
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_check", "deferral_report"])
        self.assertIn("gh down", res["warnings"][1]["warning"])


class TestProfileFallbackDefers(ReporterMocked):

    def test_non_user_profile_defers_the_check(self):
        for source in ("example", "default"):
            ws = tempfile.mkdtemp()
            write_registry(ws, make_record(env="prod"))
            fake = FakeExchange([], algos=[entry_algo()])
            res = protect_runs(fake, ws, 1, 1000.0, profile=dict(PROD_PROFILE, _profile_source=source))[0]
            self.assertTrue(res["ok"], res["errors"])
            self.assertEqual(res["actions"], [], "cap 6.25 < 72 would cancel: deferred instead")
            self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_check"])
            self.assertIn(f"source: {source}", res["warnings"][0]["warning"])
            self.assertEqual(read_registry(ws)["prod:BTCUSDT:7001"]["check_deferrals"], 1)

    def test_yolo_record_checked_whatever_the_profile_source(self):
        # the YOLO margin cap uses neither the profile nor equity: a breach is still untrusted (margin 80.8 at 15x)
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(env="prod", is_yolo=True, leverage=15))
        fake = FakeExchange([], algos=[entry_algo()])
        res = protect_runs(fake, ws, 1, RuntimeError("must not be read"),
                           profile=dict(PROD_PROFILE, _profile_source="example"))[0]
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertIn("YOLO margin cap at 15x", " ".join(res["actions"][0]["detail"]["mismatches"]))
        self.assertNotIn("warnings", res)

    def test_user_profile_checked(self):
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(env="prod"))
        fake = FakeExchange([], algos=[entry_algo()])
        res = protect_runs(fake, ws, 1, 1000.0, profile=dict(PROD_PROFILE, _profile_source="user"))[0]
        self.assertEqual(types(res), ["pending_record_mismatch"])


class TestProfileSourceMarker(unittest.TestCase):

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_loader_stamps_the_source(self):
        tmp = tempfile.mkdtemp()
        self.assertEqual(up.load_user_profile(base_dir=tmp)["_profile_source"], "default")
        self.assertNotIn("_profile_source", up.DEFAULT_PROFILE)
        self.write(os.path.join(tmp, "config", "user_profile.json.example"), {"risk_pct_equity": 0.01})
        self.assertEqual(up.load_user_profile(base_dir=tmp)["_profile_source"], "example")
        self.write(os.path.join(tmp, "config", "user_profile.json"), "{corrupt")
        self.assertEqual(up.load_user_profile(base_dir=tmp)["_profile_source"], "example")
        self.write(os.path.join(tmp, "config", "user_profile.json"), {"risk_pct_equity": 0.02})
        prof = up.load_user_profile(base_dir=tmp)
        self.assertEqual((prof["_profile_source"], prof["risk_pct_equity"]), ("user", 0.02))

    def test_save_never_persists_the_marker(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "user_profile.json")
        with patch("user_profile.PROFILE_FILE", path), patch("user_profile.CONFIG_DIR", tmp):
            current = up.load_user_profile()
            self.assertEqual(current["_profile_source"], "default")
            self.assertTrue(up.save_user_profile(dict(current, risk_pct_equity=0.01)))
            with open(path, "r", encoding="utf-8") as f:
                self.assertNotIn("_profile_source", json.load(f))
            self.assertEqual(up.load_user_profile()["_profile_source"], "user")


# ---------------------------------------------------------------------------------------------
# #156.6 fill quality flags, #156.7 guardian forwards warnings
# ---------------------------------------------------------------------------------------------
class TestFillQualityVisible(ReporterMocked):

    def test_flags_in_pending_tp_placed_detail(self):
        fake = FakeExchange([long_position(amt="12", entry="104.0", mark="104.1")])   # slipped STOP_MARKET fill
        res, _, _ = run_protect(fake, make_record(sl=95.0, tp1=104.2, tp2=120.0))
        tp = next(a for a in res["actions"] if a["type"] == "pending_tp_placed")
        self.assertEqual(tp["detail"]["fill_quality_flags"], ["rr_below_3", "tp1_below_friction"])

    def test_clean_fill_has_empty_flags(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, _, _ = run_protect(fake, make_record())
        tp = next(a for a in res["actions"] if a["type"] == "pending_tp_placed")
        self.assertEqual(tp["detail"]["fill_quality_flags"], [])


class TestGuardianForwardsWarnings(ReporterMocked):

    def run_loop(self, fake, ws, filters=True):
        log_dir = os.path.join(ws, "logs")
        out = io.StringIO()
        with offline(fake, workspace=ws), patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("position_guardian_loop.time.sleep", side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(out), contextlib.ExitStack() as stack:
            if not filters:
                stack.enter_context(patch("execute_futures_trade.get_symbol_filters", return_value=None))
            code = pgl.main(["--interval", "60", "--env", "testnet"])
            alive = eft.check_guardian_alive("testnet")
        with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            return code, json.load(f), out.getvalue(), alive

    def test_deferral_warning_in_state_and_output_guardian_still_alive(self):
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(margin_usdt=600.0))   # qty check flagged, filters unavailable: deferred
        code, state, output, alive = self.run_loop(FakeExchange([], algos=[entry_algo()]), ws, filters=False)
        self.assertEqual(code, 0)
        self.assertEqual([(w["stage"], w["symbol"], w["key"]) for w in state["pending_warnings"]],
                         [("qty_check", "BTCUSDT", "testnet:BTCUSDT:7001")])
        self.assertEqual(state["errors"], [])
        self.assertEqual(state["error_stages"], [])
        self.assertTrue(state["cycle_ok"])
        self.assertIn("~ warning qty_check BTCUSDT: total_qty check deferred", output)
        self.assertTrue(alive[0], alive[1])

    def test_no_warnings_empty_list_and_fill_flags_in_action(self):
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(sl=95.0, tp1=104.2, tp2=120.0))
        code, state, output, _ = self.run_loop(FakeExchange([long_position(amt="12", entry="104.0", mark="104.1")]), ws)
        self.assertEqual(state["pending_warnings"], [])
        self.assertNotIn("~ warning", output)
        tp = next(a for a in state["actions"] if a["type"] == "pending_tp_placed")
        self.assertEqual(tp["detail"]["fill_quality_flags"], ["rr_below_3", "tp1_below_friction"])


# ---------------------------------------------------------------------------------------------
# #157.2 pre-arm anomalies, #157.6a prearm_price, #157.6b _prearm_note
# ---------------------------------------------------------------------------------------------
class TestPrearmAnomalies(ExecutorHarness):

    def send_with_prearm(self, response, echo=False):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ALGO_ENDPOINT and (params or {}).get("closePosition") == "true":
                if echo:
                    return self.fake(method, endpoint, params, target_env)
                self.calls.append((method, endpoint, dict(params)))
                return dict(response)
            return self.fake(method, endpoint, params, target_env)
        return send

    def test_rejected_code_reported_entry_kept(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347,
                           send=self.send_with_prearm({"code": -4045, "msg": "Reach max stop order limit."}))
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["prearm_status"], "rejected:-4045")
        self.assertEqual(res["prearm_anomaly"]["status"], "rejected:-4045")
        self.assertIn("not pre-armed (rejected:-4045)", res["prearm_anomaly"]["message"])
        self.report.assert_called_once()
        kw = self.report.call_args.kwargs
        self.assertEqual((kw["severity"], kw["category"]), ("MEDIUM", "risk_gate"))
        self.assertIn("SOLUSDT", kw["title"])
        self.assertIn("testnet:SOLUSDT:8", read_registry(self.ws))
        self.assertEqual([c for c in self.calls if c[0] == "DELETE"], [])

    def test_unverified_prearm_reported_with_may_exist_wording(self):
        res = self.execute(order_type="LIMIT", limit_price=98.767, send=self.send_with_prearm({"algoId": 9}))
        self.assertTrue(res["pending_limit_entry"], res)
        self.assertEqual(res["prearm_status"], "rejected:unverified")
        self.assertIn("may exist (unverified, algo id 9)", res["message"])
        self.assertNotIn("Stop Loss not pre-armed", res["message"])
        self.assertEqual(res["prearm_anomaly"]["status"], "rejected:unverified")
        self.report.assert_called_once()

    def test_minus_2021_not_an_anomaly(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347, send=self.send_with_prearm(MINUS_2021))
        self.assertEqual(res["prearm_status"], "rejected:-2021")
        self.assertIn("Stop Loss not pre-armed (rejected:-2021)", res["message"])
        self.assertNotIn("prearm_anomaly", res)
        self.report.assert_not_called()

    def test_skipped_mcp_and_placed_not_anomalies(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347, mcp=True)
        self.assertEqual(res["prearm_status"], "skipped:mcp")
        self.assertNotIn("prearm_anomaly", res)
        self.report.assert_not_called()
        res = self.execute(order_type="LIMIT", limit_price=98.767, symbol="ETHUSDT")
        self.assertEqual(res["prearm_status"], "placed")
        self.assertNotIn("prearm_anomaly", res)
        self.report.assert_not_called()


class TestPrearmPriceAndNote(unittest.TestCase):

    def test_prearm_price_is_the_listed_tick_rounded_trigger(self):
        fake = FakeExchange([])
        with offline(fake), patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
            out = eft.prearm_resting_entry_stop("BTCUSDT", "SELL", 94.99, 100.0, target_env="testnet", tick_size=0.1)
        self.assertEqual(out["prearm_status"], "placed")
        self.assertEqual(out["prearm_price"], 94.9, "tickSize 0.1, rounded down by place_algo_stop_loss")

    def test_unverified_prearm_keeps_the_requested_price(self):
        fake = FakeExchange([], index_new_stops=False)
        with offline(fake), patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
            out = eft.prearm_resting_entry_stop("BTCUSDT", "SELL", 94.99, 100.0, target_env="testnet", tick_size=0.1)
        self.assertEqual((out["prearm_status"], out["prearm_price"]), ("rejected:unverified", 94.99))

    def test_note_wording(self):
        self.assertIn("may exist (unverified, algo id 77)",
                      eft._prearm_note({"prearm_status": "rejected:unverified", "prearm_algo_id": 77}, 95.0))
        self.assertEqual(eft._prearm_note({"prearm_status": "rejected:-2021"}, 95.0),
                         "Stop Loss not pre-armed (rejected:-2021). ")
        self.assertIn("pre-armed at 95.0", eft._prearm_note({"prearm_status": "placed", "prearm_algo_id": 9}, 95.0))
        self.assertIsNone(eft._prearm_anomaly({"prearm_status": "skipped:crossed"}))
        self.assertIsNone(eft._prearm_anomaly({"prearm_status": "placed"}))


# ---------------------------------------------------------------------------------------------
# #157.4 -4130 with failed confirmation reads
# ---------------------------------------------------------------------------------------------
class TestMinus4130ExtraListing(ReporterMocked):

    def send(self, fake, failed_reads):
        """The planned SL placement gets -4130 (the stop exists, e.g. a pre-arm); the next `failed_reads`
        openAlgoOrders reads fail."""
        state = {"fail": 0}

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "GET" and endpoint == ALGO_READ and state["fail"] > 0:
                state["fail"] -= 1
                fake.calls.append((method, endpoint, dict(params or {})))
                return dict(READ_ERROR)
            res = fake(method, endpoint, params, target_env)
            if method == "POST" and endpoint == ALGO_ENDPOINT:
                fake.algos.append(stop(9, 95.0))
                state["fail"] = failed_reads
            return res
        return send

    def test_extra_listing_finds_the_stop_kept_no_destruct(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")], reject_new_stops=True,
                            reject_response=MINUS_4130)
        with patch("execute_futures_trade.emergency_abort_market_close") as mock_abort:
            res, ws, _ = run_protect(self.send(fake, failed_reads=1 + len(eft.STOP_VERIFY_RETRY_DELAYS)),
                                     make_record())
        mock_abort.assert_not_called()
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_protect_sl", "pending_tp_placed"])
        self.assertEqual(res["actions"][0]["detail"]["stop_source"], "kept")
        self.assertEqual(res["actions"][0]["detail"]["new_stop"]["algo_id"], 9)
        self.assertEqual([c for c in posts(fake, ORDER_ENDPOINT) if c.get("type") == "MARKET"], [])

    def test_extra_listing_also_fails_auto_destructs(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")], reject_new_stops=True,
                            reject_response=MINUS_4130)
        reads = 2 * (1 + len(eft.STOP_VERIFY_RETRY_DELAYS))
        with patch("execute_futures_trade.emergency_abort_market_close",
                   return_value={"confirmed": True, "order": {"orderId": 1}}) as mock_abort:
            res, ws, _ = run_protect(self.send(fake, failed_reads=reads), make_record())
        mock_abort.assert_called_once()
        self.assertEqual(types(res), ["pending_protect_sl", "pending_abort"])
        self.assertIsNone(res["actions"][0]["detail"]["stop_source"])

    def test_ensure_entry_stop_extra_listing_ignores_other_symbols_and_sides(self):
        fake = FakeExchange([], algos=[stop(555, 95.0, symbol="ETHUSDT"), stop(556, 105.0, side="BUY")],
                            reject_new_stops=True, reject_response=MINUS_4130)
        with offline(fake):
            out = eft._ensure_entry_stop("BTCUSDT", "SELL", 95.0, target_env="testnet")
        self.assertEqual((out["verified"], out["source"]), (False, None))


# ---------------------------------------------------------------------------------------------
# #157.5 MARKET verification by algo id
# ---------------------------------------------------------------------------------------------
def market_execute(fx):
    """execute_complete_trade (TESTNET MARKET entry) with the real verify_algo_stop_loss on a fake exchange."""
    ws = tempfile.mkdtemp()

    def send(method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        if endpoint == "/fapi/v1/marginType":
            return {"code": 200, "msg": "success"}
        if endpoint == "/fapi/v1/leverage":
            return {"symbol": params["symbol"], "leverage": params["leverage"]}
        if endpoint == "/fapi/v1/leverageBracket":
            return {"error": "unavailable"}
        if endpoint == "/fapi/v1/ticker/price":
            return {"price": "100.0"}
        return fx(method, endpoint, params, target_env)

    with patch("execute_futures_trade.send_signed_request", side_effect=send), \
         patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
         patch("execute_futures_trade._workspace_dir", return_value=ws), \
         patch("execute_futures_trade.get_symbol_filters", return_value=dict(EX_FILTERS)), \
         patch("execute_futures_trade.check_mechanical_gates", return_value=(True, None)), \
         patch("execute_futures_trade.subprocess.run", side_effect=FileNotFoundError("binance-cli")), \
         patch("execute_futures_trade.time.sleep", return_value=None), \
         patch("execute_futures_trade.emergency_abort_market_close",
               return_value={"confirmed": True, "order": {"orderId": 2}}) as mock_abort, \
         patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
         patch("user_profile.load_user_profile", return_value=dict(EX_PROFILE)):
        res = eft.execute_complete_trade(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                                         sl_price=97.0, tp1_price=110.0, tp2_price=120.0, target_env="testnet",
                                         order_type="MARKET", bypass_eval_gate=True)
    return res, mock_abort


class TestMarketVerifyByAlgoId(ReporterMocked):

    def leftover(self):
        return stop(555, 97.0, symbol="SOLUSDT")   # e.g. a pre-arm left by an ended resting entry, same price

    def test_leftover_stop_at_same_price_does_not_verify(self):
        fx = FakeExchange([], algos=[self.leftover()], index_new_stops=False)   # the new stop is never listed
        res, mock_abort = market_execute(fx)
        self.assertFalse(res["success"])
        self.assertTrue(res["emergency_abort"])
        mock_abort.assert_called_once()

    def test_matching_algo_id_verifies(self):
        fx = FakeExchange([], algos=[self.leftover()])
        res, mock_abort = market_execute(fx)
        self.assertTrue(res["success"], res.get("error"))
        mock_abort.assert_not_called()
        self.assertEqual(res["sl_algo_order"]["algoId"], 9001, "this placement's stop, not the leftover 555")

    def test_minus_4130_leftover_auto_destructs_and_reports(self):
        fx = FakeExchange([], algos=[self.leftover()], reject_new_stops=True, reject_response=MINUS_4130)
        res, mock_abort = market_execute(fx)
        self.assertTrue(res["emergency_abort"])
        mock_abort.assert_called_once()
        self.assertIn("leftover", res["error"])
        self.assertIn("-4130", res["error"])
        self.report.assert_called_once()
        kw = self.report.call_args.kwargs
        self.assertEqual((kw["severity"], kw["category"]), ("HIGH", "risk_gate"))

    def test_verify_by_algo_id_unit(self):
        fx = FakeExchange([], algos=[stop(555, 95.0), stop(9, 80.0)])
        with offline(fx):
            self.assertEqual(eft.verify_algo_stop_loss("BTCUSDT", "SELL", 95.0, target_env="testnet", algo_id=777),
                             (False, None))
            ok, info = eft.verify_algo_stop_loss("BTCUSDT", "SELL", 95.0, target_env="testnet", algo_id=9)
            self.assertTrue(ok)
            self.assertEqual(info["algoId"], 9, "by id only, whatever the price")
            self.assertEqual(eft.verify_algo_stop_loss("BTCUSDT", "SELL", 95.0, target_env="testnet")[1]["algoId"], 555)


if __name__ == "__main__":
    unittest.main()
