#!/usr/bin/env python3
"""
test_issue_273_prearm_followups.py - Offline tests for issue #273 (pre-arm residuals after #232).

1. A resting LIMIT pre-arm rejected with -4509 appends one "prearm_rejected" event to logs/guardian_actions.jsonl
   (best effort: a failing append changes nothing); other codes and STOP_MARKET entries write none.
2. A verified pending_protect_sl of mode "place" carries the fill-to-stop timing (exchange fill time when known);
   the guardian forwards it to logs/guardian_actions.jsonl.
3. trading_doctor [PREARM] / [FILL-STOP] informational lines (counts, median, n = 0, malformed lines, env, window);
   they never change the exit code.
4. Protect order at fill: the planned SL is placed before the TPs; an unverified SL auto-destructs, no TP placed.
5. Dead block: a STOP_MARKET entry never reports a pre-arm anomaly (only the LIMIT branch can).
6. trade-execution-planner SKILL.md: pre-arm bullet split (KEYS LIMIT / KEYS STOP_MARKET / MCP) with the window.

No network: every exchange call is faked, files go to temp directories, sleeps are patched.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
LOOPS_DIR = os.path.join(SCRIPTS_DIR, "loops")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, LOOPS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft  # noqa: E402
import position_guardian_loop as pgl  # noqa: E402
import sync_session_state as sss  # noqa: E402
import trading_doctor  # noqa: E402
from utils import atomic_writer  # noqa: E402
import test_pending_entries as tpe  # noqa: E402  (fixtures only)
import test_issue_268_fee_drag as t268  # noqa: E402  (fixtures only)
from test_exit_management import FakeExchange, offline, long_position, stop, ALGO_ENDPOINT  # noqa: E402
from test_issue_36_prearm_resting_stop import run_protect, types  # noqa: E402

ORDER_ENDPOINT = "/fapi/v1/order"
MSG = "TIF GTE can only be used with open positions"


def events(ws, name="guardian_actions.jsonl"):
    path = os.path.join(ws, "logs", name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------------------------
# 1. LIMIT -4509 event
# ---------------------------------------------------------------------------------------------
class TestPrearmRejectedEvent(tpe.ExecutorHarness):

    def send_with_prearm(self, response):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ALGO_ENDPOINT and (params or {}).get("closePosition") == "true":
                self.calls.append((method, endpoint, dict(params)))
                return dict(response)
            return self.fake(method, endpoint, params, target_env)
        return send

    def test_limit_minus_4509_appends_one_event(self):
        before = int(time.time())
        res = self.execute(order_type="LIMIT", limit_price=98.767,
                           send=self.send_with_prearm({"code": -4509, "msg": MSG}))
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["prearm_status"], "rejected:-4509")
        evs = events(self.ws)
        self.assertEqual(len(evs), 1, evs)
        ev = evs[0]
        self.assertEqual({k: ev[k] for k in ("event", "symbol", "direction", "env", "entry_type", "code", "msg")},
                         {"event": "prearm_rejected", "symbol": "SOLUSDT", "direction": "LONG", "env": "testnet",
                          "entry_type": "LIMIT", "code": -4509, "msg": MSG})
        self.assertGreaterEqual(ev["ts"], before)
        self.assertLessEqual(ev["ts"], int(time.time()))
        self.assertNotIn("type", ev, "the guardian-action readers dispatch on type: an event must not have one")

    def test_short_direct_call_and_no_message(self):
        with patch("execute_futures_trade.send_signed_request", side_effect=self.send_with_prearm({"code": -4509})), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("execute_futures_trade._workspace_dir", return_value=self.ws):
            out = eft.prearm_resting_entry_stop("ETHUSDT", "BUY", 103.0, 100.0, target_env="testnet",
                                                entry_type="LIMIT")
        self.assertEqual(out, {"prearm_status": "rejected:-4509"})
        ev = events(self.ws)[0]
        self.assertEqual((ev["symbol"], ev["direction"], ev["msg"]), ("ETHUSDT", "SHORT", None))

    def test_failing_append_changes_nothing(self):
        send = self.send_with_prearm({"code": -4509, "msg": MSG})
        ok = self.execute(order_type="LIMIT", limit_price=98.767, send=send)
        ok_rec = tpe.read_registry(self.ws)["testnet:SOLUSDT:7"]
        self.setUp()
        original = atomic_writer.atomic_append_jsonl

        def failing(path, record):
            if os.path.basename(path) == "guardian_actions.jsonl":
                raise OSError("disk full")
            return original(path, record)
        with patch("utils.atomic_writer.atomic_append_jsonl", side_effect=failing), \
             self.assertLogs("execute_futures_trade", level="WARNING") as logs:
            res = self.execute(order_type="LIMIT", limit_price=98.767, send=send)
        rec = tpe.read_registry(self.ws)["testnet:SOLUSDT:7"]
        for key in ("success", "pending_limit_entry", "orderId", "prearm_status", "prearm_reject_msg", "message",
                    "pending_entry_key"):
            self.assertEqual(res.get(key), ok.get(key), key)
        self.assertNotIn("prearm_anomaly", res)
        self.assertEqual({k: v for k, v in rec.items() if k.startswith("prearm")},
                         {k: v for k, v in ok_rec.items() if k.startswith("prearm")})
        self.assertEqual(events(self.ws), [])
        self.assertTrue(any("prearm_rejected event not logged" in m for m in logs.output), logs.output)
        self.report.assert_not_called()

    def test_other_rejection_codes_write_no_event(self):
        for response in ({"code": -4045, "msg": "Reach max stop order limit."},
                         {"code": -2021, "msg": "Order would immediately trigger."}):
            with self.subTest(code=response["code"]):
                self.setUp()
                res = self.execute(order_type="LIMIT", limit_price=98.767, send=self.send_with_prearm(response))
                self.assertEqual(res["prearm_status"], f"rejected:{response['code']}")
                self.assertEqual(events(self.ws), [])

    def test_stop_market_writes_no_event(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347,
                           send=self.send_with_prearm({"code": -4509, "msg": MSG}))
        self.assertEqual(res["prearm_status"], "skipped:no_position")
        self.assertEqual(events(self.ws), [])


# ---------------------------------------------------------------------------------------------
# 5. Dead block: no pre-arm anomaly on the STOP_MARKET branch
# ---------------------------------------------------------------------------------------------
class TestStopMarketNoAnomalyReport(tpe.ExecutorHarness):

    def test_prearm_error_on_stop_market_is_not_reported(self):
        # The only way a STOP_MARKET pre-arm could be "rejected:" (uses_mcp_gateway raising): entry kept, no report.
        with patch("execute_futures_trade._prearm_resting_entry_stop", side_effect=RuntimeError("boom")):
            res = self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["prearm_status"], "rejected:RuntimeError: boom")
        self.assertNotIn("prearm_anomaly", res)
        self.report.assert_not_called()
        self.assertIn("testnet:SOLUSDT:8", tpe.read_registry(self.ws))

    def test_limit_branch_still_reports(self):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ALGO_ENDPOINT and (params or {}).get("closePosition") == "true":
                return {"code": -4045, "msg": "Reach max stop order limit."}
            return self.fake(method, endpoint, params, target_env)
        res = self.execute(order_type="LIMIT", limit_price=98.767, send=send)
        self.assertEqual(res["prearm_anomaly"]["status"], "rejected:-4045")
        self.report.assert_called_once()


# ---------------------------------------------------------------------------------------------
# 2. Fill-to-stop timing
# ---------------------------------------------------------------------------------------------
def stop_market_record(**extra):
    return tpe.make_record(kind="STOP_MARKET", prearm_status="skipped:no_position", **extra)


class TestFillToStopTiming(unittest.TestCase):

    def protect_sl_detail(self, position, record=None, algos=None):
        fake = FakeExchange([position], algos=algos)
        res, ws, _ = run_protect(fake, record or stop_market_record())
        self.assertTrue(res["ok"], res["errors"])
        return next(a["detail"] for a in res["actions"] if a["type"] == "pending_protect_sl"), res

    def test_exchange_fill_time_used_when_known(self):
        before = time.time()
        fill_ms = int((before - 90) * 1000)
        detail, _ = self.protect_sl_detail(dict(long_position(amt="12", entry="101.0", mark="101.5"),
                                                updateTime=fill_ms),
                                           record=stop_market_record(placed_at_ts=int(before) - 120))
        self.assertEqual((detail["mode"], detail["stop_source"]), ("place", "placed"))
        self.assertEqual(detail["fill_ts_source"], "position_update")
        self.assertAlmostEqual(detail["fill_ts"], fill_ms / 1000.0, places=3)
        self.assertGreaterEqual(detail["fill_detected_ts"], round(before, 3) - 0.001)
        self.assertGreaterEqual(detail["stop_placed_ts"], detail["fill_detected_ts"])
        self.assertAlmostEqual(detail["fill_to_stop_s"], round(detail["stop_placed_ts"] - detail["fill_ts"], 3),
                               places=2)
        self.assertGreaterEqual(detail["fill_to_stop_s"], 89.0)
        self.assertLess(detail["fill_to_stop_s"], 120.0)

    def test_detection_time_without_exchange_fill_time(self):
        for extra in ({}, {"updateTime": 0}, {"updateTime": "bad"},
                      {"updateTime": int((time.time() + 600) * 1000)},    # in the future: not a fill time
                      {"updateTime": int((time.time() - 3600) * 1000)}):  # before the entry was placed (60 s ago)
            with self.subTest(extra=extra):
                detail, _ = self.protect_sl_detail(dict(long_position(amt="12", entry="101.0", mark="101.5"),
                                                        **extra))
                self.assertEqual(detail["fill_ts_source"], "detected")
                self.assertEqual(detail["fill_ts"], detail["fill_detected_ts"])
                self.assertGreaterEqual(detail["fill_to_stop_s"], 0.0)
                self.assertLess(detail["fill_to_stop_s"], 30.0)

    def test_replace_mode_has_no_timing(self):
        # An existing looser stop (e.g. the orphan heal): the planned SL replaces it, the position was not unprotected.
        detail, _ = self.protect_sl_detail(long_position(amt="12", entry="101.0", mark="101.5"),
                                           algos=[stop(601, 90.0)])
        self.assertEqual(detail["mode"], "replace")
        for key in ("fill_ts", "fill_ts_source", "fill_detected_ts", "stop_placed_ts", "fill_to_stop_s"):
            self.assertNotIn(key, detail)

    def test_unverified_or_dry_run_has_no_timing(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")], index_new_stops=False)
        res, _, _ = run_protect(fake, stop_market_record())
        self.assertNotIn("fill_to_stop_s", res["actions"][0]["detail"])
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, _, _ = run_protect(fake, stop_market_record(), dry_run=True)
        self.assertNotIn("fill_to_stop_s", res["actions"][0]["detail"])

    def test_verified_prearm_found_at_fill_has_no_timing(self):
        # The pre-arm protected the fill (stop_source "prearm"): there was no no-stop window to measure.
        ensured = {"verified": True, "source": "prearm", "placement": None, "info": stop(9, 95.0)}
        with patch("execute_futures_trade._ensure_entry_stop", return_value=ensured):
            detail, _ = self.protect_sl_detail(dict(long_position(amt="12", entry="101.0", mark="101.5"),
                                                    updateTime=int(time.time() * 1000)))
        self.assertEqual((detail["mode"], detail["stop_source"]), ("place", "prearm"))
        self.assertNotIn("fill_to_stop_s", detail)

    def test_helper_never_raises(self):
        self.assertEqual(eft._fill_to_stop_fields(None, "x", 1.0), {})
        self.assertEqual(eft._fill_to_stop_fields({"updateTime": 5000}, 100.0, 104.0)["fill_to_stop_s"], 99.0)
        self.assertEqual(eft._fill_to_stop_fields({"updateTime": 103000}, 100.0, 102.0)["fill_to_stop_s"], 0.0,
                         "within the drift allowance, never negative")
        self.assertEqual(eft._fill_to_stop_fields({"updateTime": 5000}, 100.0, 104.0, placed_at_ts=50)["fill_ts_source"],
                         "detected", "a position change before the entry's placement is not its fill")
        self.assertEqual(eft._fill_to_stop_fields({"updateTime": 47000}, 100.0, 104.0, placed_at_ts=50)["fill_ts"],
                         47.0, "5 s of drift allowed before placed_at_ts")

    def test_guardian_forwards_the_timing_to_guardian_actions(self):
        ws = tempfile.mkdtemp()
        tpe.write_registry(ws, stop_market_record())
        fill_ms = int((time.time() - 45) * 1000)
        fake = FakeExchange([dict(long_position(amt="12", entry="101.0", mark="101.5"), updateTime=fill_ms)])
        log_dir = os.path.join(ws, "logs")
        with offline(fake, workspace=ws), patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(tpe.HEALTHY)), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("report_agent_issue.report_issue"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            pgl.main(["--once", "--env", "testnet"])
        rec = next(r for r in events(ws) if r.get("type") == "pending_protect_sl")
        self.assertTrue(rec["success"])
        self.assertEqual(rec["detail"]["fill_ts_source"], "position_update")
        self.assertGreaterEqual(rec["detail"]["fill_to_stop_s"], 44.0)


# ---------------------------------------------------------------------------------------------
# 3. Doctor [PREARM] / [FILL-STOP]
# ---------------------------------------------------------------------------------------------
def write_actions(ws, *lines):
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    with open(os.path.join(ws, "logs", "guardian_actions.jsonl"), "w", encoding="utf-8") as f:
        for line in lines:
            f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")


def rejected_ev(ts, env="prod", code=-4509, entry_type="LIMIT"):
    return {"event": "prearm_rejected", "ts": ts, "env": env, "symbol": "SOLUSDT", "direction": "LONG",
            "entry_type": entry_type, "code": code, "msg": MSG}


def protect_ev(ts, latency, env="prod", success=True, dry_run=False, source="position_update"):
    return {"timestamp": ts, "env": env, "symbol": "SOLUSDT", "type": "pending_protect_sl", "dry_run": dry_run,
            "success": success, "detail": {"mode": "place", "fill_to_stop_s": latency, "fill_ts_source": source}}


class TestDoctorPrearmLines(unittest.TestCase):

    NOW = 1_800_000_000.0

    def lines(self, *records, env="prod"):
        ws = tempfile.mkdtemp()
        if records:
            write_actions(ws, *records)
        return dict(trading_doctor.prearm_fill_stop_lines(ws, env, now=self.NOW))

    def test_counts_median_and_max(self):
        now = self.NOW
        out = self.lines(rejected_ev(now - 60), rejected_ev(now - 3600), rejected_ev(now - 7200, entry_type=None),
                         protect_ev(now - 10, 75.0), protect_ev(now - 20, 110.5), protect_ev(now - 30, 3.0,
                                                                                             source="detected"))
        self.assertIn("3 pre-arm(s) rejected with -4509 in the last 24 h (2 on LIMIT entries)", out["PREARM"])
        self.assertIn("max 110.5 s, median 75.0 s (n=3, 2 from the exchange fill time)", out["FILL-STOP"])
        self.assertIn("about 60-120 s", out["FILL-STOP"])

    def test_n_zero_prints_no_fill_stop_line(self):
        out = self.lines()
        self.assertEqual(out, {"PREARM": "0 pre-arm(s) rejected with -4509 in the last 24 h (0 on LIMIT entries); "
                                         "the guardian places those stops at fill (informational)."})
        out = self.lines(protect_ev(self.NOW - 10, 75.0, success=False), protect_ev(self.NOW - 10, 75.0, dry_run=True),
                         protect_ev(self.NOW - 10, None), protect_ev(self.NOW - 10, -1.0))
        self.assertNotIn("FILL-STOP", out)

    def test_window_env_code_and_malformed_lines_ignored(self):
        now = self.NOW
        out = self.lines("not json {", "", "[1, 2]", '"prearm_rejected"', "{\"event\": \"prearm_rejected\", ",
                         rejected_ev(now - 25 * 3600), rejected_ev(now - 60, env="testnet"),
                         rejected_ev(now - 60, code=-2021), rejected_ev("bad"), rejected_ev(now - 60, env="mainnet"),
                         protect_ev(now - 25 * 3600, 50.0), protect_ev(now - 10, 60.0, env="testnet"),
                         dict(protect_ev(now - 10, 70.0), detail="garbage"), protect_ev(now - 10, True),
                         protect_ev(now - 10, 64.0))
        self.assertIn("1 pre-arm(s) rejected with -4509", out["PREARM"], "mainnet is the prod alias")
        self.assertIn("max 64.0 s, median 64.0 s (n=1,", out["FILL-STOP"])
        testnet = self.lines(rejected_ev(now - 60, env="testnet"), protect_ev(now - 10, 60.0, env="testnet"),
                             env="testnet")
        self.assertIn("1 pre-arm(s)", testnet["PREARM"])
        self.assertIn("n=1", testnet["FILL-STOP"])

    def test_no_protected_file_in_a_command(self):
        out = self.lines(rejected_ev(self.NOW - 60), protect_ev(self.NOW - 10, 75.0))
        for text in out.values():
            self.assertNotIn("`", text)
            self.assertNotIn("python3", text)
            self.assertNotIn("guardian_actions", text)

    def test_run_doctor_prints_lines_and_exit_code_unchanged(self):
        ws = tempfile.mkdtemp()
        write_actions(ws, rejected_ev(time.time() - 60, env="testnet"),
                      protect_ev(time.time() - 10, 80.0, env="testnet"))
        fake = t268._fee_fake()
        with patch.object(sss, "LOGS_DIR", os.path.join(ws, "logs")):
            code, out = t268.TestDoctorFees.run_doctor(self, fake)
        self.assertIn("ℹ️  [PREARM] 1 pre-arm(s) rejected with -4509 in the last 24 h (1 on LIMIT entries)", out)
        self.assertIn("ℹ️  [FILL-STOP] Fill-to-stop in the last 24 h: max 80.0 s, median 80.0 s (n=1,", out)
        with patch.object(sss, "LOGS_DIR", os.path.join(tempfile.mkdtemp(), "logs")):
            empty_code, empty_out = t268.TestDoctorFees.run_doctor(self, fake)
        self.assertNotIn("[FILL-STOP]", empty_out)
        with patch("trading_doctor.prearm_fill_stop_lines", side_effect=RuntimeError("boom")):
            broken_code, broken_out = t268.TestDoctorFees.run_doctor(self, fake)
        self.assertIn("ℹ️  [PREARM] Pre-arm / fill-to-stop stats unavailable (RuntimeError).", broken_out)
        self.assertEqual(code, empty_code)
        self.assertEqual(code, broken_code)
        status = [l for l in out.splitlines() if "STATUS:" in l]
        self.assertEqual(status, [l for l in broken_out.splitlines() if "STATUS:" in l])


# ---------------------------------------------------------------------------------------------
# 4. Protect order at fill (existing behaviour, pinned)
# ---------------------------------------------------------------------------------------------
class TestProtectOrderAtFill(unittest.TestCase):

    def test_planned_sl_placed_before_the_tps(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, ws, _ = run_protect(fake, stop_market_record())
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_protect_sl", "pending_tp_placed"])
        sl_posts = [i for i, c in enumerate(fake.calls) if c[0] == "POST" and c[1] == ALGO_ENDPOINT
                    and c[2].get("closePosition") == "true"]
        tp_posts = [i for i, c in enumerate(fake.calls) if c[0] == "POST" and c[1] == ORDER_ENDPOINT
                    and c[2].get("reduceOnly") == "true"]
        self.assertEqual(len(sl_posts), 1)
        self.assertEqual(len(tp_posts), 2)
        self.assertLess(sl_posts[0], min(tp_posts), "the SL is placed before any TP")
        verify_reads = [i for i, c in enumerate(fake.calls) if c[0] == "GET" and c[1] == "/fapi/v1/openAlgoOrders"
                        and i > sl_posts[0]]
        self.assertTrue(verify_reads and verify_reads[0] < min(tp_posts), "the SL is verified before any TP")

    def test_unverified_sl_auto_destructs_without_tps(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")], reject_new_stops=True,
                            reject_response=tpe.INTERNAL_ERROR)
        res, ws, _ = run_protect(fake, stop_market_record())
        self.assertEqual(types(res), ["pending_protect_sl", "pending_abort"])
        self.assertFalse(res["actions"][0]["success"])
        self.assertTrue(res["actions"][1]["success"])
        self.assertEqual(res["actions"][1]["detail"]["reason"], "planned_sl_unverified")
        order_posts = [c[2] for c in fake.calls if c[0] == "POST" and c[1] == ORDER_ENDPOINT]
        self.assertTrue(order_posts)
        for c in order_posts:
            self.assertEqual((c["type"], c["side"], c["reduceOnly"], c["quantity"]), ("MARKET", "SELL", "true", "12"),
                             "only the reduce-only MARKET close: no TP")
        self.assertEqual(events(ws, "emergency_aborts.jsonl")[0]["event"], "CRITICAL_FAILSAFE_ABORT")
        self.assertEqual(tpe.read_registry(ws), {})


# ---------------------------------------------------------------------------------------------
# 6. Skill wording
# ---------------------------------------------------------------------------------------------
class TestSkillPrearmBullets(unittest.TestCase):

    def test_split_bullets_and_window(self):
        for root in (".agents", ".claude"):
            with self.subTest(root=root):
                with open(os.path.join(BASE_DIR, root, "skills", "trade-execution-planner", "SKILL.md"), "r",
                          encoding="utf-8") as f:
                    text = f.read()
                section = text.split("- Stop Loss pre-arm:", 1)[1].split("- PROD: they require", 1)[0]
                self.assertIn("- KEYS `LIMIT`: the SL is pre-armed when it is not crossed", section)
                self.assertIn("- KEYS `STOP_MARKET`: never pre-armed (`skipped:no_position`)", section)
                self.assertIn("- MCP: never pre-armed (`skipped:mcp`)", section)
                self.assertIn("about 60-120 s", section)
                self.assertIn("`[FILL-STOP]`", section)
                self.assertIn("`prearm_anomaly`", section)


if __name__ == "__main__":
    unittest.main()
