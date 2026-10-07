#!/usr/bin/env python3
"""
test_issue_144_close_path_followups.py - Offline tests for issue #144 (no network, no orders).

Follow-ups to the #137 close path (close_position_market keeps the stop until flat):
1. Exit status: trading_drift_watchdog.main() and night_cutoff_loop.main() return 1 when a close failed (and the night
   cutoff when a position stays unprotected); the watchdog message shows stop_source / stop_protected.
2. Not-flat stop read: retried before healing; every read failed -> the heal still runs and the result is "healed",
   "kept" (inferred from -4130) or "unknown" (stop_protected None), never "none" while a stop may exist.
3. Stable P0 fingerprint: error_detail is symbol + stop source; the raw response goes into context.
4. Quantities are plain decimal strings (never "1e-05") on the KEYS close, the emergency abort and crossed_close.
5. Emergency heal from close_position_market uses the planned SL of the matching audit record when tighter and not
   crossed; other heal callers keep the 2.5% anchor. A rejected/unverified planned-SL stop is retried once at the
   anchor (round 2).
6. Night cutoff: an unreadable positionRisk records "read_error" and main() returns 1 (round 2).
"""

import io
import os
import sys
import time
import tempfile
import unittest
import contextlib
from unittest.mock import patch

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
import report_agent_issue
from test_exit_management import FakeExchange, offline, long_position, stop, write_audit, ALGO_ENDPOINT
from test_issue_137_close_keeps_stop import (CloseExchange, keys_env, posts_of, REJECT_2022, REJECT_1001,
                                             FAILED_CLOSE, ORDER_ENDPOINT)
from test_issue_92_holding_time import FillsExchange, fill, row, HOUR
from test_pending_entries import make_record, write_registry

ALGO_READ = "/fapi/v1/openAlgoOrders"
REJECT_4130 = {"code": -4130, "msg": "An open stop or take profit order with GTE and closePosition in the direction "
                                     "is existing."}
READ_ERROR = {"code": -1003, "msg": "Too many requests; current limit is 2400 request weight per 1 MINUTE."}


class ReadFailExchange(CloseExchange):
    """CloseExchange whose first `read_failures` openAlgoOrders reads return an API error."""

    def __init__(self, positions, closes, read_failures=0, **kw):
        super().__init__(positions, closes, **kw)
        self.read_failures = read_failures

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if method == "GET" and endpoint == ALGO_READ and self.read_failures > 0:
            self.read_failures -= 1
            self.calls.append((method, endpoint, dict(params or {})))
            return dict(READ_ERROR)
        return super().__call__(method, endpoint, params, target_env, retry_count)


def algo_reads(fake):
    return [c for c in fake.calls if c[0] == "GET" and c[1] == ALGO_READ]


# =============================================================================
# 1. Exit status
# =============================================================================
class TestWatchdogExitStatus(unittest.TestCase):

    def overdue_fake(self):
        return FillsExchange([row("BTCUSDT")], fills={"BTCUSDT": [fill("BUY", 1, int(time.time()) - 6 * HOUR)]},
                             algos=[stop(501, 95.0)])

    def run_main(self, close_result, argv=("--env", "testnet", "--auto-exit")):
        out = io.StringIO()
        with offline(self.overdue_fake()), \
                patch("execute_futures_trade.close_position_market", return_value=close_result), \
                contextlib.redirect_stdout(out):
            code = tdw.main(list(argv))
        return code, out.getvalue()

    def test_failed_auto_exit_exits_1_and_prints_stop_source(self):
        code, out = self.run_main(dict(FAILED_CLOSE, stop_protected=True))
        self.assertEqual(code, 1)
        self.assertIn("AUTO-EXIT FAILED", out)
        self.assertIn("stop_source=kept", out)
        self.assertIn("stop_protected=True", out)

    def test_failure_without_error_text_has_a_fallback(self):
        code, out = self.run_main({"success": False, "stop_source": "unknown", "stop_protected": None})
        self.assertEqual(code, 1)
        self.assertIn("close not confirmed (no error text)", out)
        self.assertIn("stop_source=unknown stop_protected=None", out)

    def test_successful_auto_exit_exits_0(self):
        code, _ = self.run_main({"success": True})
        self.assertEqual(code, 0)

    def test_report_only_run_exits_0(self):
        code, _ = self.run_main(dict(FAILED_CLOSE), argv=("--env", "testnet"))
        self.assertEqual(code, 0)

    def test_audit_dead_alpha_still_returns_its_dict(self):
        with offline(self.overdue_fake()), contextlib.redirect_stdout(io.StringIO()):
            rep = tdw.audit_dead_alpha(target_env="testnet", auto_exit=False)
        self.assertIsInstance(rep, dict)
        self.assertEqual(rep["positions"][0]["action_taken"], "RECOMMEND_EXIT")


def night_send(positions, algos=()):
    def send(method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == "/fapi/v2/positionRisk":
            return [dict(p) for p in positions]
        if endpoint == ALGO_READ:
            return [dict(a) for a in algos]
        return []
    return send


SOL = {"symbol": "SOLUSDT", "positionAmt": "1.0", "entryPrice": "150.0", "markPrice": "148.0",
       "unRealizedProfit": "-2.0", "isolatedMargin": "15.0"}
SOL_STOP = {"orderType": "STOP_MARKET", "triggerPrice": "145.0"}


class TestNightCutoffExitStatus(unittest.TestCase):

    def run_main(self, mode, send, close_result=None, heal_result=None):
        patches = [patch("execute_futures_trade.send_signed_request", side_effect=send),
                   patch("execute_futures_trade.get_symbol_filters", return_value={"tickSize": 0.1, "precision_price": 1}),
                   patch("user_profile.load_user_profile", return_value={"overnight_mode": mode}),
                   patch("os.system")]
        if close_result is not None:
            patches.append(patch("execute_futures_trade.close_position_market", return_value=close_result))
        if heal_result is not None:
            patches.append(patch("execute_futures_trade.heal_orphan_position", return_value=heal_result))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = ncl.main(["--env", "testnet"])
            summary = ncl.run_night_cutoff(target_env="testnet")
        return code, summary

    def test_failed_close_all_exits_1(self):
        code, summary = self.run_main("CLOSE_ALL_AT_MARKET", night_send([SOL]), close_result=dict(FAILED_CLOSE))
        self.assertEqual(code, 1)
        self.assertEqual(summary, {"close_failures": ["SOLUSDT"], "unprotected": []})

    def test_failed_zero_overnight_close_exits_1(self):
        code, summary = self.run_main("ZERO_OVERNIGHT_RISK", night_send([SOL], [SOL_STOP]),
                                      close_result=dict(FAILED_CLOSE))
        self.assertEqual(code, 1)
        self.assertEqual(summary["close_failures"], ["SOLUSDT"])

    def test_unprotected_position_exits_1(self):
        heal = {"success": False, "closed": False, "reason": "heal_and_close_failed"}
        code, summary = self.run_main("SWING_STRUCTURAL_STOP", night_send([SOL]), heal_result=heal)
        self.assertEqual(code, 1)
        self.assertEqual(summary, {"close_failures": [], "unprotected": ["SOLUSDT"]})

    def test_closed_after_failed_heal_is_not_a_failure(self):
        heal = {"success": True, "closed": True, "reason": "closed_after_failed_heal"}
        code, summary = self.run_main("SWING_STRUCTURAL_STOP", night_send([SOL]), heal_result=heal)
        self.assertEqual(code, 0)
        self.assertEqual(summary, {"close_failures": [], "unprotected": []})

    def test_clean_runs_exit_0(self):
        code, _ = self.run_main("CLOSE_ALL_AT_MARKET", night_send([SOL]), close_result={"success": True})
        self.assertEqual(code, 0)
        code, _ = self.run_main("ZERO_OVERNIGHT_RISK", night_send([SOL], [SOL_STOP]), close_result={"success": True})
        self.assertEqual(code, 0)
        code, summary = self.run_main("ZERO_OVERNIGHT_RISK", night_send([]))
        self.assertEqual((code, summary), (0, {"close_failures": [], "unprotected": []}))


# =============================================================================
# 2. Failed stop read on the not-flat path
# =============================================================================
class TestStopReadOnNotFlatPath(unittest.TestCase):

    def close(self, fake):
        with keys_env(fake) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        self.assertEqual(fake.deletes(), [], "nothing is cancelled on a failed close")
        mock_report.assert_called_once()
        return res, mock_report

    def test_read_errors_then_stop_found_is_kept_without_heal(self):
        fake = ReadFailExchange([long_position()], [REJECT_2022], read_failures=2, algos=[stop(501, 95.0)])
        res, _ = self.close(fake)
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("kept", True))
        self.assertEqual(posts_of(fake, ALGO_ENDPOINT), [], "no heal next to an existing stop")
        self.assertEqual(len(algo_reads(fake)), 3, "retried until the first successful read")
        self.assertNotIn("heal", res)

    def test_all_reads_fail_then_heal_verified_is_healed(self):
        # 4 failed reads (0.0 + 3 retry delays); the heal's own verification then reads successfully
        fake = ReadFailExchange([long_position()], [REJECT_2022], read_failures=4)
        res, _ = self.close(fake)
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("healed", True))
        self.assertTrue(res["heal"]["verified"])
        self.assertEqual(len(posts_of(fake, ALGO_ENDPOINT)), 1)

    def test_all_reads_fail_and_heal_rejected_4130_is_kept_inferred(self):
        fake = ReadFailExchange([long_position()], [REJECT_2022], read_failures=99, algos=[stop(501, 95.0)],
                                reject_new_stops=True, reject_response=REJECT_4130)
        res, mock_report = self.close(fake)
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("kept", True))
        self.assertEqual(res["stop_note"], "stop inferred from -4130")
        self.assertIn("stop inferred from -4130", res["error"])
        self.assertEqual([a["algoId"] for a in fake.algos], [501], "no duplicate stop")
        self.assertEqual(mock_report.call_args.kwargs["error_detail"], "BTCUSDT close not confirmed flat; stop kept")

    def test_all_reads_fail_and_other_heal_failure_is_unknown(self):
        fake = ReadFailExchange([long_position()], [REJECT_2022], read_failures=99, reject_new_stops=True)
        res, mock_report = self.close(fake)
        self.assertEqual(res["stop_source"], "unknown")
        self.assertIsNone(res["stop_protected"])
        self.assertNotEqual(res["stop_source"], "none")
        self.assertIn("stop check:", res["error"])
        self.assertEqual(mock_report.call_args.kwargs["error_detail"], "BTCUSDT close not confirmed flat; stop unknown")

    def test_successful_read_without_stop_and_failed_heal_is_none(self):
        fake = ReadFailExchange([long_position()], [REJECT_2022], read_failures=0, reject_new_stops=True)
        res, _ = self.close(fake)
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("none", False))

    def test_successful_read_without_stop_and_4130_is_still_none(self):
        """-4130 is only used to infer a stop when every read failed; a successful empty read is authoritative."""
        fake = ReadFailExchange([long_position()], [REJECT_2022], read_failures=0, reject_new_stops=True,
                                reject_response=REJECT_4130)
        res, _ = self.close(fake)
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("none", False))


# =============================================================================
# 3. Stable P0 fingerprint
# =============================================================================
class TestStableFingerprint(unittest.TestCase):

    def report_kwargs(self, closes):
        fake = CloseExchange([long_position()], closes, algos=[stop(501, 95.0)])
        with keys_env(fake) as mock_report:
            eft.close_position_market("BTCUSDT", target_env="prod")
        mock_report.assert_called_once()
        return mock_report.call_args.kwargs

    def test_different_attempts_and_responses_share_one_fingerprint(self):
        a = self.report_kwargs([REJECT_2022])
        b = self.report_kwargs([("partial", "4"), REJECT_1001])
        self.assertEqual(a["error_detail"], "BTCUSDT close not confirmed flat; stop kept")
        self.assertEqual((a["title"], a["error_detail"]), (b["title"], b["error_detail"]))
        self.assertEqual(report_agent_issue.compute_fingerprint(a["title"], a["error_detail"]),
                         report_agent_issue.compute_fingerprint(b["title"], b["error_detail"]))
        self.assertIn("-2022", a["context"])
        self.assertIn("-1001", b["context"])
        self.assertIn("after 3 attempt(s)", a["context"])

    def test_stop_source_changes_the_fingerprint(self):
        kept = self.report_kwargs([REJECT_2022])
        fake = CloseExchange([long_position()], [REJECT_2022], reject_new_stops=True)
        with keys_env(fake) as mock_report:
            eft.close_position_market("BTCUSDT", target_env="prod")
        none = mock_report.call_args.kwargs
        self.assertNotEqual(report_agent_issue.compute_fingerprint(kept["title"], kept["error_detail"]),
                            report_agent_issue.compute_fingerprint(none["title"], none["error_detail"]))


# =============================================================================
# 4. Quantity formatting
# =============================================================================
class TestQuantityFormatting(unittest.TestCase):

    def test_format_order_qty(self):
        cases = [("0.00001", "0.00001"), ("-0.00001", "0.00001"), (1e-05, "0.00001"), (-1e-05, "0.00001"),
                 (1e-08, "0.00000001"), ("10", "10"), (10.0, "10"), ("10.500", "10.5"), ("-3", "3"),
                 ("-0", "0"), (-0.0, "0"), (1234567.0, "1234567"), (0.1, "0.1")]
        for value, expected in cases:
            with self.subTest(value=value):
                got = eft.format_order_qty(value)
                self.assertEqual(got, expected)
                self.assertNotIn("e", got.lower())

    def test_keys_close_sends_plain_decimal(self):
        for amt, side in (("0.00001", "SELL"), ("-0.00001", "BUY")):
            with self.subTest(amt=amt):
                fake = CloseExchange([long_position(amt=amt)], ["fill"])
                with keys_env(fake):
                    res = eft.close_position_market("BTCUSDT", target_env="prod")
                self.assertTrue(res["success"], res)
                self.assertEqual(fake.close_qtys, ["0.00001"])
                self.assertEqual(fake.writes()[0][2]["side"], side)

    def test_float_position_amt_never_scientific(self):
        fake = CloseExchange([long_position(amt=1e-05)], ["fill"])
        with keys_env(fake):
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertTrue(res["success"], res)
        self.assertEqual(fake.close_qtys, ["0.00001"])

    def test_emergency_abort_sends_plain_decimal(self):
        fake = FakeExchange([long_position(amt="0.00001")])
        with offline(fake), contextlib.redirect_stderr(io.StringIO()):
            res = eft.emergency_abort_market_close("BTCUSDT", "SELL", 1e-05, target_env="prod")
        self.assertTrue(res["confirmed"], res)
        sent = [c[2] for c in fake.calls if c[0] == "POST" and c[1] == ORDER_ENDPOINT]
        self.assertEqual([s["quantity"] for s in sent], ["0.00001"])

    def test_crossed_close_sends_plain_decimal(self):
        fake = FakeExchange([long_position(amt="0.00001", entry="101.0", mark="99.0")], algos=[stop(601, 98.4)])

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ORDER_ENDPOINT and (params or {}).get("type") == "MARKET":
                fake.calls.append((method, endpoint, dict(params)))
                fake.positions = []
                return {"orderId": 55, "status": "FILLED"}
            return fake(method, endpoint, params, target_env)
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(sl=99.4, total_qty=0.00001))
        with offline(send, workspace=ws):
            res = eft.protect_pending_entries(target_env="testnet")
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual([a["type"] for a in res["actions"]], ["pending_sl_crossed_close"])
        closes = [c[2] for c in fake.calls if c[0] == "POST" and c[1] == ORDER_ENDPOINT]
        self.assertEqual([c["quantity"] for c in closes], ["0.00001"])


# =============================================================================
# 5. Heal distance from the planned SL
# =============================================================================
class TestHealUsesPlannedSl(unittest.TestCase):

    def close_with_audit(self, position, direction="LONG", **audit):
        ws = tempfile.mkdtemp()
        if audit:
            rec = dict(symbol="BTCUSDT", direction=direction, target_env="prod", entry_price=100.0, total_qty=10.0,
                       timestamp=int(time.time()) - 600)
            rec.update(audit)
            write_audit(ws, **rec)
        fake = CloseExchange([position], [REJECT_2022])
        with offline(fake, workspace=ws), patch("report_agent_issue.report_issue"), \
                contextlib.redirect_stderr(io.StringIO()):
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        self.assertEqual(res["stop_source"], "healed")
        sent = posts_of(fake, ALGO_ENDPOINT)
        self.assertEqual(len(sent), 1)
        return res["heal"]["healed_sl_price"], sent[0]["triggerPrice"]

    def test_tighter_uncrossed_planned_sl_is_used_long(self):
        # entry 100, mark 110: 2.5% anchor = 97.5; planned 99.0 is tighter and below mark
        self.assertEqual(self.close_with_audit(long_position(), sl_price=99.0), (99.0, 99.0))

    def test_tighter_uncrossed_planned_sl_is_used_short(self):
        # SHORT entry 100, mark 95: anchor = 102.5; planned 101.0 is tighter and above mark
        pos = long_position(amt="-10", mark="95.0")
        self.assertEqual(self.close_with_audit(pos, direction="SHORT", sl_price=101.0), (101.0, 101.0))

    def test_crossed_planned_sl_falls_back_to_anchor(self):
        self.assertEqual(self.close_with_audit(long_position(), sl_price=111.0), (97.5, 97.5))
        pos = long_position(amt="-10", mark="95.0")
        # 2.5% anchor 100 * 1.025, rounded down to the 0.1 tick (pre-existing round_price behaviour)
        self.assertEqual(self.close_with_audit(pos, direction="SHORT", sl_price=94.0), (102.4, 102.4))

    def test_looser_planned_sl_never_loosens(self):
        self.assertEqual(self.close_with_audit(long_position(), sl_price=90.0), (97.5, 97.5))

    def test_no_or_unmatched_record_uses_anchor(self):
        self.assertEqual(self.close_with_audit(long_position()), (97.5, 97.5))
        self.assertEqual(self.close_with_audit(long_position(), sl_price=99.0, entry_price=120.0), (97.5, 97.5))
        self.assertEqual(self.close_with_audit(long_position(), sl_price=99.0, direction="SHORT"), (97.5, 97.5))
        self.assertEqual(self.close_with_audit(long_position(), sl_price=99.0, target_env="testnet"), (97.5, 97.5))
        self.assertEqual(self.close_with_audit(long_position(), sl_price=99.0, total_qty=5.0), (97.5, 97.5))

    def test_other_heal_callers_keep_the_anchor(self):
        ws = tempfile.mkdtemp()
        write_audit(ws, symbol="BTCUSDT", direction="LONG", target_env="prod", entry_price=100.0, total_qty=10.0,
                    sl_price=99.0, timestamp=int(time.time()) - 600)
        fake = FakeExchange([long_position()])
        with offline(fake, workspace=ws):
            res = eft.heal_orphan_position(long_position(), target_env="prod")
        self.assertTrue(res["verified"])
        self.assertEqual(res["healed_sl_price"], 97.5)
        self.assertNotIn("sl_source", res)


class RejectFirstStopsExchange(CloseExchange):
    """CloseExchange that rejects the first `rejections` stop placements with -2021, then accepts them."""

    def __init__(self, positions, closes, rejections, **kw):
        super().__init__(positions, closes, **kw)
        self.rejections = rejections

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if method == "POST" and endpoint == ALGO_ENDPOINT:
            self.reject_new_stops = self.rejections > 0
            self.rejections -= 1
        return super().__call__(method, endpoint, params, target_env, retry_count)


class TestPlannedSlRejectedRetriesAtAnchor(unittest.TestCase):
    """Round 2: a rejected/unverified planned-SL heal stop is retried once at the 2.5% anchor, never looser."""

    def close_with_planned(self, position, direction, planned, rejections):
        ws = tempfile.mkdtemp()
        write_audit(ws, symbol="BTCUSDT", direction=direction, target_env="prod", entry_price=100.0, total_qty=10.0,
                    sl_price=planned, timestamp=int(time.time()) - 600)
        fake = RejectFirstStopsExchange([position], [REJECT_2022], rejections)
        with offline(fake, workspace=ws), patch("report_agent_issue.report_issue"), \
                contextlib.redirect_stderr(io.StringIO()):
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        return res, [p["triggerPrice"] for p in posts_of(fake, ALGO_ENDPOINT)], fake

    def test_long_planned_rejected_retry_at_anchor_succeeds(self):
        res, triggers, fake = self.close_with_planned(long_position(), "LONG", 99.0, rejections=1)
        self.assertEqual(triggers, [99.0, 97.5])
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("healed", True))
        heal = res["heal"]
        self.assertEqual((heal["healed_sl_price"], heal["sl_source"]), (97.5, "anchor_after_planned_rejected"))
        self.assertEqual(heal["planned_attempt"]["sl_price"], 99.0)
        self.assertEqual(heal["planned_attempt"]["placement"]["code"], -2021)
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [97.5], "exactly one stop")

    def test_short_planned_rejected_retry_at_anchor_succeeds(self):
        pos = long_position(amt="-10", mark="95.0")
        res, triggers, _ = self.close_with_planned(pos, "SHORT", 101.0, rejections=1)
        # anchor 100 * 1.025 rounded down to the 0.1 tick (pre-existing round_price behaviour)
        self.assertEqual(triggers, [101.0, 102.4])
        self.assertEqual(res["stop_source"], "healed")
        self.assertEqual(res["heal"]["sl_source"], "anchor_after_planned_rejected")

    def test_long_both_attempts_fail_never_loosens_beyond_anchor(self):
        res, triggers, _ = self.close_with_planned(long_position(), "LONG", 99.0, rejections=99)
        self.assertEqual(triggers, [99.0, 97.5], "one retry, at the anchor, nothing looser")
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("none", False))
        self.assertFalse(res["heal"]["verified"])
        self.assertEqual(res["heal"]["reason"], "heal_stop_unverified")

    def test_short_both_attempts_fail_never_loosens_beyond_anchor(self):
        pos = long_position(amt="-10", mark="95.0")
        res, triggers, _ = self.close_with_planned(pos, "SHORT", 101.0, rejections=99)
        self.assertEqual(triggers, [101.0, 102.4])
        self.assertEqual(res["stop_source"], "none")
        self.assertFalse(res["heal"]["verified"])

    def test_anchor_rejection_is_not_retried(self):
        """Without a usable planned SL there is a single attempt (the anchor is already the loosest stop)."""
        res, triggers, _ = self.close_with_planned(long_position(), "LONG", 90.0, rejections=99)
        self.assertEqual(triggers, [97.5])
        self.assertNotIn("sl_source", res["heal"])


class TestNightCutoffPositionReadError(unittest.TestCase):

    def run_main(self, position_risk):
        calls = []

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            calls.append(endpoint)
            if endpoint == "/fapi/v2/positionRisk":
                if isinstance(position_risk, Exception):
                    raise position_risk
                return dict(position_risk)
            return []
        out = io.StringIO()
        with patch("execute_futures_trade.send_signed_request", side_effect=send), \
                patch("user_profile.load_user_profile", return_value={"overnight_mode": "ZERO_OVERNIGHT_RISK"}), \
                patch("os.system"), contextlib.redirect_stdout(out):
            code = ncl.main(["--env", "testnet"])
            summary = ncl.run_night_cutoff(target_env="testnet")
        return code, summary, out.getvalue(), calls

    def test_unreadable_position_risk_exits_1_with_read_error(self):
        for position_risk in (READ_ERROR, RuntimeError("connection reset")):
            with self.subTest(position_risk=position_risk):
                code, summary, out, calls = self.run_main(position_risk)
                self.assertEqual(code, 1)
                self.assertIn("read_error", summary)
                self.assertEqual((summary["close_failures"], summary["unprotected"]), ([], []))
                self.assertNotIn("ZERO OPEN POSITIONS", out)
                self.assertNotIn("/fapi/v1/openOrders", calls, "no orphan cleanup while positions are unknown")


if __name__ == "__main__":
    unittest.main()
