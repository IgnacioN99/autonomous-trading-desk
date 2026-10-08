#!/usr/bin/env python3
"""
test_issue_150_heal_sensor_followups.py - Offline tests for issues #150, #151, #138 and #152 (no network, no orders).

#150 heal and close: a planned stop indexed late is re-verified (planned_sl_late_indexed); an explicit rejection skips
     the indexing wait; a heal next to an existing stop after failed reads is reported (redundant_stops), never
     cancelled; the heal uses a newer ratcheted SL from session_state.json when tighter and not crossed; the
     stop_protected None contract.
#151 sensors: watchdog / orphan audit / night cutoff / doctor treat unreadable reads as UNKNOWN (never "zero
     positions", never "orphan" + heal); the watchdog P0 contract.
#138 dead alpha: one close rule (pt.dead_alpha_close_decision) for the watchdog --auto-exit and the guardian; the sync
     reads trades_audit.jsonl once; userTrades diagnostics; public pt.norm_env.
#152 MCP numbers without an exponent; hedge mode refused without orders.
"""

import io
import os
import sys
import json
import time
import shutil
import builtins
import tempfile
import unittest
import contextlib
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
import test_issue_92_holding_time as t92
from test_exit_management import FakeExchange, offline, long_position, stop, write_audit, ALGO_ENDPOINT
from test_issue_137_close_keeps_stop import CloseExchange, keys_env, posts_of, REJECT_2022
from test_issue_144_close_path_followups import (ReadFailExchange, RejectFirstStopsExchange, REJECT_4130, READ_ERROR,
                                                 ALGO_READ, algo_reads)
from test_issue_92_holding_time import FillsExchange, STALLED, HEALTHY, row, fill, quiet, HOUR, DOCTOR_PROFILE

NOW = int(time.time())


def audit_ws(direction="LONG", sl_price=99.0, ts=None, **extra):
    """Temp workspace with one matching prod audit record (entry 100, qty 10, like long_position())."""
    ws = tempfile.mkdtemp()
    rec = dict(symbol="BTCUSDT", direction=direction, target_env="prod", entry_price=100.0, total_qty=10.0,
               sl_price=sl_price, timestamp=NOW - 600 if ts is None else ts)
    rec.update(extra)
    write_audit(ws, **rec)
    return ws


def write_state(ws, **overrides):
    """Valid prod session_state.json newer than the audit record with a verified BTCUSDT LONG at break-even."""
    pos = {"symbol": "BTCUSDT", "direction": "LONG", "qty": 10.0, "entry_price": 100.0, "sl_price": 100.2,
           "sl_algo_verified": True}
    pos.update(overrides.pop("position", {}))
    state = {"is_valid": True, "target_env": "prod", "last_updated_ts": NOW, "active_positions": [pos]}
    state.update(overrides)
    with open(os.path.join(ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f)


# =============================================================================
# #150 item 1: late-indexed planned stop
# =============================================================================
class LateIndexExchange(CloseExchange):
    """The first stop placement is accepted with an algoId but stays invisible for `hidden_reads` openAlgoOrders reads
    after it; every later placement is rejected with -4130."""

    def __init__(self, positions, closes=("fill",), hidden_reads=5, **kw):
        super().__init__(positions, list(closes), **kw)
        self.hidden_reads = hidden_reads
        self.planned = None
        self.reads_since = 0

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if method == "POST" and endpoint == ALGO_ENDPOINT:
            self.calls.append((method, endpoint, dict(params)))
            if self.planned is not None:
                return dict(REJECT_4130)
            self.next_id += 1
            self.planned = {"algoId": self.next_id, "symbol": params["symbol"], "side": params["side"],
                            "orderType": params["type"], "triggerPrice": str(params["triggerPrice"]),
                            "closePosition": True, "reduceOnly": False}
            return {"algoId": self.next_id}
        if method == "GET" and endpoint == ALGO_READ and self.planned is not None:
            self.reads_since += 1
            if self.reads_since > self.hidden_reads and self.planned not in self.algos:
                self.algos.append(self.planned)
        return super().__call__(method, endpoint, params, target_env, retry_count)


class TestLateIndexedPlannedStop(unittest.TestCase):

    def test_close_path_long(self):
        fake = LateIndexExchange([long_position()], [REJECT_2022])
        with keys_env(fake) as mock_report, patch("execute_futures_trade._workspace_dir", return_value=audit_ws()):
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("healed", True))
        heal = res["heal"]
        self.assertEqual((heal["sl_source"], heal["healed_sl_price"]), ("planned_sl_late_indexed", 99.0))
        self.assertEqual(heal["new_stop"]["algo_id"], fake.planned["algoId"])
        self.assertEqual(heal["planned_attempt"]["sl_price"], 99.0)
        self.assertEqual(heal["anchor_attempt"], {"sl_price": 97.5, "placement": REJECT_4130})
        self.assertEqual([p["triggerPrice"] for p in posts_of(fake, ALGO_ENDPOINT)], [99.0, 97.5])
        self.assertEqual(fake.deletes(), [])
        mock_report.assert_called_once()

    def test_direct_heal(self):
        fake = LateIndexExchange([long_position()])
        with offline(fake):
            res = eft.heal_orphan_position(long_position(), target_env="prod", planned_sl=99.0)
        self.assertTrue(res["verified"] and res["success"])
        self.assertEqual((res["reason"], res["sl_source"], res["healed_sl_price"]),
                         ("healed", "planned_sl_late_indexed", 99.0))
        # planned wait 4 reads + anchor (-4130, no wait) + re-verify: visible on the 6th read
        self.assertEqual(fake.reads_since, 6)

    def test_never_indexed_is_still_unverified(self):
        fake = LateIndexExchange([long_position()], hidden_reads=99)
        with offline(fake):
            res = eft.heal_orphan_position(long_position(), target_env="prod", planned_sl=99.0)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "heal_stop_unverified")
        self.assertEqual(len(posts_of(fake, ALGO_ENDPOINT)), 2, "still at most two placements")


# =============================================================================
# #150 item 2: no wait on an explicit rejection
# =============================================================================
class TestNoWaitOnExplicitRejection(unittest.TestCase):

    def test_is_explicit_rejection(self):
        self.assertTrue(eft._is_explicit_rejection({"code": -2021, "msg": "would immediately trigger"}))
        self.assertTrue(eft._is_explicit_rejection(dict(REJECT_4130)))
        for placement in ({"code": -1007}, {"code": -1001}, {"code": -1000}, {"code": -1006}, {"error": "x"},
                          {"error": "MCP Gateway Error: timed out", "isError": True}, {"algoId": 1, "code": -1},
                          {"code": "abc"}, None, "text"):
            with self.subTest(placement=placement):
                self.assertFalse(eft._is_explicit_rejection(placement))

    def waits(self, fake):
        wait = MagicMock(return_value=(False, None))
        with offline(fake), patch("execute_futures_trade.wait_for_stop_confirmation", wait):
            res = eft.heal_orphan_position(long_position(), target_env="prod", planned_sl=99.0)
        return res, [c.args[2] for c in wait.call_args_list]

    def test_rejected_planned_skips_its_wait(self):
        res, prices = self.waits(RejectFirstStopsExchange([long_position()], ["fill"], rejections=1))
        self.assertEqual(prices, [97.5], "only the anchor is waited for; the rejected planned is not re-verified")
        self.assertFalse(res["verified"])

    def test_both_rejected_no_wait_at_all(self):
        res, prices = self.waits(RejectFirstStopsExchange([long_position()], ["fill"], rejections=99))
        self.assertEqual(prices, [])
        self.assertEqual(res["reason"], "heal_stop_unverified")

    def test_transport_error_still_waits(self):
        fake = FakeExchange([long_position()], reject_new_stops=True, reject_response={"error": "timeout"})
        _, prices = self.waits(fake)
        self.assertEqual(prices, [99.0, 97.5, 99.0], "planned wait, anchor wait, planned re-verify")


# =============================================================================
# #150 item 3: heal next to an existing stop after failed reads
# =============================================================================
class TestRedundantStopReported(unittest.TestCase):

    def test_qty_stop_reported_not_cancelled(self):
        qty_stop = dict(stop(777, 96.0, close_position=False), reduceOnly=True, quantity="10")
        fake = ReadFailExchange([long_position()], [REJECT_2022], read_failures=4, algos=[qty_stop])
        with keys_env(fake):
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("healed", True))
        self.assertEqual([s["algo_id"] for s in res["redundant_stops"]], [777])
        self.assertIn("next flat close cancels the leftovers", res["stop_note"])
        self.assertEqual([c for c in fake.calls if c[0] == "DELETE" and c[1] == ALGO_ENDPOINT], [])
        self.assertEqual(fake.deletes(), [])

    def test_no_report_when_reads_succeeded(self):
        fake = ReadFailExchange([long_position()], [REJECT_2022], read_failures=0)
        with keys_env(fake):
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertEqual(res["stop_source"], "healed")
        self.assertNotIn("redundant_stops", res)
        self.assertNotIn("stop_note", res)

    def test_failed_extra_read_adds_nothing(self):
        fake = ReadFailExchange([long_position()], [REJECT_2022], read_failures=4)
        # 4 pre-heal failures; the heal's verification read succeeds; then fail the extra read
        orig = fake.__call__
        state = {"verified_reads": 0}

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "GET" and endpoint == ALGO_READ and fake.read_failures == 0:
                state["verified_reads"] += 1
                if state["verified_reads"] > 1:
                    return dict(READ_ERROR)
            return orig(method, endpoint, params, target_env, retry_count)
        with keys_env(send):
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertEqual(res["stop_source"], "healed")
        self.assertNotIn("redundant_stops", res)


# =============================================================================
# #150 item 4: heal from the last ratcheted SL
# =============================================================================
class TestRatchetedSl(unittest.TestCase):

    def heal_price(self, ws, position=None):
        fake = CloseExchange([position or long_position()], [REJECT_2022])
        with keys_env(fake), patch("execute_futures_trade._workspace_dir", return_value=ws):
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertEqual(res["stop_source"], "healed")
        return res["heal"]

    def test_break_even_used_long(self):
        ws = audit_ws()
        write_state(ws)
        heal = self.heal_price(ws)
        self.assertEqual((heal["healed_sl_price"], heal["sl_source"]), (100.2, "planned_sl"))

    def planned(self, ws, position=None):
        position = position or long_position()
        with patch("execute_futures_trade._workspace_dir", return_value=ws):
            return eft._planned_sl_for_position("BTCUSDT", position, "prod")

    def test_negative_cases_fall_back_to_the_audit_sl(self):
        cases = {
            "other env": dict(target_env="testnet"),
            "stale": dict(last_updated_ts=NOW - 3600),
            "invalid": dict(is_valid=False),
            "entry mismatch": dict(position={"entry_price": 105.0}),
            "qty too small": dict(position={"qty": 5.0}),
            "unverified": dict(position={"sl_algo_verified": False}),
            "direction": dict(position={"direction": "SHORT"}),
            "crossed": dict(position={"sl_price": 110.0}),
            "looser": dict(position={"sl_price": 98.0}),
            "zero": dict(position={"sl_price": 0}),
        }
        for name, overrides in cases.items():
            with self.subTest(case=name):
                ws = audit_ws()
                write_state(ws, **overrides)
                self.assertEqual(self.planned(ws), 99.0)

    def test_missing_or_corrupt_state_falls_back(self):
        ws = audit_ws()
        self.assertEqual(self.planned(ws), 99.0)
        with open(os.path.join(ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(self.planned(ws), 99.0)

    def test_short_mirror(self):
        ws = audit_ws(direction="SHORT", sl_price=101.0)
        write_state(ws, position={"direction": "SHORT", "sl_price": 99.8})
        pos = long_position(amt="-10", mark="95.0")
        self.assertEqual(self.planned(ws, pos), 99.8)
        heal = self.heal_price(ws, pos)
        self.assertEqual((heal["healed_sl_price"], heal["sl_source"]), (99.8, "planned_sl"))
        write_state(ws, position={"direction": "SHORT", "sl_price": 94.0})   # crossed for a SHORT (below mark)
        self.assertEqual(self.planned(ws, pos), 101.0)

    def test_no_audit_match_uses_the_anchor(self):
        ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(ws, "logs"))
        write_state(ws)
        self.assertIsNone(self.planned(ws))
        heal = self.heal_price(ws)
        self.assertEqual(heal["healed_sl_price"], 97.5)
        self.assertNotIn("sl_source", heal)


# =============================================================================
# #150 item 5: stop_protected None contract
# =============================================================================
def overdue_fake():
    return FillsExchange([row("BTCUSDT")], fills={"BTCUSDT": [fill("BUY", 1, NOW - 6 * HOUR)]}, algos=[stop(501, 95.0)])


class TestStopProtectedNoneContract(unittest.TestCase):

    def test_none_is_unprotected_in_the_watchdog(self):
        close = {"success": False, "stop_protected": None, "stop_source": "unknown", "error": "x"}
        out = io.StringIO()
        with offline(overdue_fake()), \
                patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(STALLED)), \
                patch("execute_futures_trade.close_position_market", return_value=close), \
                contextlib.redirect_stdout(out):
            rep = tdw.audit_dead_alpha(target_env="testnet", auto_exit=True)
            code = tdw.main(["--env", "testnet", "--auto-exit"])
        self.assertEqual(rep["positions"][0]["action_taken"], "AUTO_EXIT_FAILED")
        self.assertEqual(code, 1)
        self.assertIn("stop_protected=None (treat as UNPROTECTED)", out.getvalue())

    def test_true_is_not_flagged(self):
        close = {"success": False, "stop_protected": True, "stop_source": "kept", "error": "x"}
        out = io.StringIO()
        with offline(overdue_fake()), \
                patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(STALLED)), \
                patch("execute_futures_trade.close_position_market", return_value=close), \
                contextlib.redirect_stdout(out):
            tdw.audit_dead_alpha(target_env="testnet", auto_exit=True)
        self.assertNotIn("treat as UNPROTECTED", out.getvalue())

    def test_documented(self):
        self.assertIn("never test `is False`", eft.__doc__)
        self.assertIn("never test", eft.close_position_market.__doc__)


# =============================================================================
# #151: watchdog positionRisk read error
# =============================================================================
class TestWatchdogReadError(unittest.TestCase):

    def watchdog(self, reply):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v2/positionRisk":
                if isinstance(reply, Exception):
                    raise reply
                return dict(reply)
            return []
        out = io.StringIO()
        with patch("execute_futures_trade.send_signed_request", side_effect=send), contextlib.redirect_stdout(out):
            rep = tdw.audit_dead_alpha(target_env="testnet")
            code = tdw.main(["--env", "testnet"])
        return rep, code, out.getvalue()

    def test_error_reply_and_exception(self):
        for reply in (READ_ERROR, RuntimeError("connection reset")):
            with self.subTest(reply=reply):
                rep, code, out = self.watchdog(reply)
                self.assertTrue(rep["read_error"].startswith("positionRisk unreadable:"))
                self.assertIsNone(rep["active_count"])
                self.assertEqual((rep["dead_alpha_count"], rep["positions"]), (0, []))
                self.assertNotIn("ZERO OPEN POSITIONS", out)
                self.assertIn("POSITION READ FAILED", out)
                self.assertEqual(code, 1)

    def test_empty_list_is_still_zero_positions(self):
        out = io.StringIO()
        with patch("execute_futures_trade.send_signed_request", return_value=[]), contextlib.redirect_stdout(out):
            rep = tdw.audit_dead_alpha(target_env="testnet")
        self.assertEqual(rep["active_count"], 0)
        self.assertNotIn("read_error", rep)
        self.assertIn("ZERO OPEN POSITIONS", out.getvalue())


def run_doctor(send, watchdog=None, auto_heal=False):
    """trading_doctor.run_doctor(testnet) as in test_issue_92 TestDoctorTemporalAudit.run_doctor, with this test's own
    send fake and heal_orphan_position patched."""
    tmp = tempfile.mkdtemp()
    sync = MagicMock(return_value={"is_valid": True})
    watchdog = watchdog or MagicMock(return_value={"dead_alpha_count": 0, "unknown_holding_symbols": []})
    heal = MagicMock(return_value={"success": True, "healed_sl_price": 97.5})
    resp = MagicMock()
    resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
    out = io.StringIO()
    try:
        with patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.get_client_config", return_value=("key12345678", "sec12345678", "http://x")), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("execute_futures_trade.send_signed_request", side_effect=send), \
             patch("execute_futures_trade.heal_orphan_position", heal), \
             patch("user_profile.load_user_profile", return_value=dict(DOCTOR_PROFILE)), \
             patch("trading_doctor.check_pretool_hook", return_value={"ok": True, "critical": [], "warnings": [], "info": []}), \
             patch("shadow_tracker.calculate_efficacy_metrics", side_effect=RuntimeError("skip")), \
             patch("trading_doctor.check_guardian_service", return_value=("ok", "guardian alive (stub)")), \
             patch.object(sss, "STATE_FILE", os.path.join(tmp, "session_state.json")), \
             patch("sync_session_state.sync_session_state", sync), \
             patch("trading_drift_watchdog.audit_dead_alpha", watchdog), \
             contextlib.redirect_stdout(out):
            mock_urlopen.return_value.__enter__.return_value = resp
            code = trading_doctor.run_doctor(target_env="testnet", auto_heal=auto_heal)
    finally:
        shutil.rmtree(tmp, True)
    return code, out.getvalue(), heal, watchdog


def doctor_send(positions, algos):
    def send(method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == "/fapi/v2/balance":
            return [{"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}]
        if endpoint == "/fapi/v2/positionRisk":
            return positions
        if endpoint == "/fapi/v1/openAlgoOrders":
            return algos
        return []
    return send


class TestDoctorGuards(unittest.TestCase):

    def test_watchdog_read_error_is_a_warning_not_ok(self):
        wd = MagicMock(return_value={"read_error": "positionRisk unreadable: x", "dead_alpha_count": 0,
                                     "unknown_holding_symbols": []})
        code, out, _, _ = run_doctor(doctor_send([row("BTCUSDT")], [stop(501, 95.0)]), watchdog=wd)
        self.assertIn("Temporal audit failed: positionRisk unreadable: x", out)
        self.assertNotIn("Zero Dead Alpha", out)
        self.assertIn("OPERATIONAL WITH WARNINGS", out)

    def test_unknown_holding_warning_has_next_step(self):
        wd = MagicMock(return_value={"dead_alpha_count": 0, "unknown_holding_symbols": ["BTCUSDT"]})
        _, out, _, _ = run_doctor(doctor_send([row("BTCUSDT")], [stop(501, 95.0)]), watchdog=wd)
        self.assertIn("Review manually: python3 scripts/execute_futures_trade.py --positions --json.", out)

    def test_position_risk_unreadable_is_critical(self):
        code, out, heal, wd = run_doctor(doctor_send(dict(READ_ERROR), [stop(501, 95.0)]), auto_heal=True)
        self.assertEqual(code, 1)
        self.assertIn("Orphan audit: positionRisk unreadable", out)
        self.assertIn("open positions UNKNOWN", out)
        self.assertNotIn("Clean portfolio", out)
        heal.assert_not_called()
        wd.assert_not_called()

    def test_algo_orders_unreadable_is_critical_without_heal(self):
        code, out, heal, wd = run_doctor(doctor_send([row("BTCUSDT")], dict(READ_ERROR)), auto_heal=True)
        self.assertEqual(code, 1)
        self.assertIn("stop protection UNKNOWN for ['BTCUSDT']; no heal attempted", out)
        self.assertNotIn("ORPHAN position(s)", out)
        heal.assert_not_called()
        wd.assert_not_called()

    def test_no_positions_and_unreadable_algos_is_clean(self):
        code, out, heal, _ = run_doctor(doctor_send([], dict(READ_ERROR)))
        self.assertIn("Clean portfolio", out)
        heal.assert_not_called()

    def test_doctor_has_no_own_norm_env(self):
        self.assertFalse(hasattr(trading_doctor, "_norm_env"))
        self.assertIs(trading_doctor.pt.norm_env, pt.norm_env)


# =============================================================================
# #151: orphan audit unknown
# =============================================================================
class TestOrphanAuditUnknown(unittest.TestCase):

    def test_unreadable_stops_are_unknown_and_never_healed(self):
        fake = ReadFailExchange([long_position(), long_position(symbol="ETHUSDT")], ["fill"], read_failures=99)
        with offline(fake):
            res = eft.audit_orphan_positions(target_env="testnet", auto_heal=True)
        self.assertEqual((res["unknown_count"], res["orphans_count"], res["all_protected"]), (2, 0, False))
        for info in res["positions"]:
            self.assertEqual(info["protection"], "unknown")
            self.assertFalse(info["is_protected"])
            self.assertIn("orders_error", info)
            self.assertIn("protection UNKNOWN", info["note"])
            self.assertNotIn("auto_heal_attempted", info)
        self.assertEqual(posts_of(fake, ALGO_ENDPOINT), [])
        self.assertEqual(len(algo_reads(fake)), 5, "full retries for the first symbol, one read afterwards")

    def test_transient_failure_is_protected(self):
        fake = ReadFailExchange([long_position()], ["fill"], read_failures=1, algos=[stop(501, 95.0)])
        with offline(fake):
            res = eft.audit_orphan_positions(target_env="testnet", auto_heal=True)
        self.assertEqual(res["positions"][0]["protection"], "protected")
        self.assertEqual((res["unknown_count"], res["orphans_count"], res["all_protected"]), (0, 0, True))

    def test_no_stop_is_orphan_and_healed(self):
        fake = FakeExchange([long_position()])
        with offline(fake):
            res = eft.audit_orphan_positions(target_env="testnet", auto_heal=True)
        info = res["positions"][0]
        self.assertEqual(info["protection"], "orphan")
        self.assertTrue(info["auto_heal_verified"])
        self.assertEqual((res["orphans_count"], res["unknown_count"]), (1, 0))

    @patch("sys.exit")
    def test_cli_exit_1_on_unknown(self, mock_exit):
        unknown = {"total_active": 1, "orphans_count": 0, "unknown_count": 1, "all_protected": False}
        for mode, target in (("--audit-orphans", "audit_orphan_positions"),
                             ("--auto-heal", "audit_and_auto_heal_orphans")):
            with self.subTest(mode=mode):
                mock_exit.reset_mock()
                with patch(f"execute_futures_trade.{target}", return_value=dict(unknown)), \
                        patch.object(sys, "argv", ["execute_futures_trade.py", mode, "--env", "testnet"]), \
                        contextlib.redirect_stdout(io.StringIO()):
                    eft.main()
                mock_exit.assert_called_once_with(1)

    @patch("sys.exit")
    def test_cli_real_path(self, mock_exit):
        fake = ReadFailExchange([long_position()], ["fill"], read_failures=99)
        out = io.StringIO()
        with offline(fake), patch.object(sys, "argv", ["execute_futures_trade.py", "--audit-orphans", "--env", "testnet"]), \
                contextlib.redirect_stdout(out):
            eft.main()
        mock_exit.assert_called_once_with(1)
        self.assertEqual(json.loads(out.getvalue())["unknown_count"], 1)


# =============================================================================
# #151: night cutoff
# =============================================================================
SOL = {"symbol": "SOLUSDT", "positionAmt": "1.0", "entryPrice": "150.0", "markPrice": "148.0",
       "unRealizedProfit": "-2.0", "isolatedMargin": "15.0"}
SOL_STOP = {"orderType": "STOP_MARKET", "triggerPrice": "145.0"}


class TestNightCutoff(unittest.TestCase):

    def run_night(self, mode, positions, algos, close_result=None, heal_result=None):
        reads = []

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v2/positionRisk":
                return positions if isinstance(positions, list) else dict(positions)
            if endpoint == ALGO_READ:
                reads.append(endpoint)
                return [dict(a) for a in algos] if isinstance(algos, list) else dict(algos)
            return []
        close = MagicMock(return_value=close_result or {"success": True})
        heal = MagicMock(return_value=heal_result or {"success": True, "healed_sl_price": 145.0})
        be = MagicMock(return_value={"success": True})
        out = io.StringIO()
        with patch("execute_futures_trade.send_signed_request", side_effect=send), \
                patch("execute_futures_trade.get_symbol_filters", return_value={"tickSize": 0.1, "precision_price": 1}), \
                patch("user_profile.load_user_profile", return_value={"overnight_mode": mode}), \
                patch("execute_futures_trade.close_position_market", close), \
                patch("execute_futures_trade.heal_orphan_position", heal), \
                patch("execute_futures_trade.move_sl_to_breakeven", be), \
                patch("time.sleep"), patch("os.system", return_value=0), contextlib.redirect_stdout(out):
            code = ncl.main(["--env", "testnet"])
        return code, out.getvalue(), close, heal, be, reads

    def test_unreadable_stops_are_unknown_no_action(self):
        # Modes that do not close at market: no heal, ratchet or close (unchanged by PR #170 round 2).
        for mode in ("SWING_STRUCTURAL_STOP",):
            with self.subTest(mode=mode):
                code, out, close, heal, be, reads = self.run_night(mode, [SOL], READ_ERROR)
                self.assertEqual(code, 1)
                self.assertIn("SOLUSDT stop state UNKNOWN", out)
                heal.assert_not_called()
                close.assert_not_called()
                be.assert_not_called()
                self.assertEqual(len(reads), 4, "retried with STOP_VERIFY_RETRY_DELAYS")
                self.assertIn("NIGHT CUTOFF INCOMPLETE", out)
                self.assertIn("stop_unknown: ['SOLUSDT']", out)
                self.assertNotIn("NIGHT CUTOFF COMPLETED", out)

    def test_zero_overnight_unknown_stop_still_closes(self):
        # Closing is risk-reducing: ZERO_OVERNIGHT_RISK closes a stop_unknown position; heal and ratchet stay skipped.
        code, out, close, heal, be, reads = self.run_night("ZERO_OVERNIGHT_RISK", [SOL], READ_ERROR)
        close.assert_called_once_with("SOLUSDT", target_env="testnet")
        heal.assert_not_called()
        be.assert_not_called()
        self.assertEqual(len(reads), 4, "retried with STOP_VERIFY_RETRY_DELAYS")
        self.assertIn("SOLUSDT stop state UNKNOWN", out)
        self.assertIn("SOLUSDT closed at market", out)
        # The unknown read is still reported: exit 1 and the INCOMPLETE banner.
        self.assertEqual(code, 1)
        self.assertIn("NIGHT CUTOFF INCOMPLETE", out)
        self.assertIn("stop_unknown: ['SOLUSDT']", out)
        self.assertNotIn("close_failures", out)
        self.assertNotIn("NIGHT CUTOFF COMPLETED", out)

    def test_zero_overnight_unknown_stop_close_fails(self):
        code, out, close, heal, be, _ = self.run_night("ZERO_OVERNIGHT_RISK", [SOL], READ_ERROR,
                                                        close_result={"success": False, "error": "x"})
        close.assert_called_once_with("SOLUSDT", target_env="testnet")
        heal.assert_not_called()
        be.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("close_failures: ['SOLUSDT']", out)
        self.assertIn("stop_unknown: ['SOLUSDT']", out)
        self.assertIn("NIGHT CUTOFF INCOMPLETE", out)

    def test_summary_key(self):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v2/positionRisk":
                return [dict(SOL)]
            if endpoint == ALGO_READ:
                return dict(READ_ERROR)
            return []
        for close_result, failures in (({"success": True}, []), ({"success": False, "error": "x"}, ["SOLUSDT"])):
            with self.subTest(close_result=close_result):
                with patch("execute_futures_trade.send_signed_request", side_effect=send), \
                        patch("user_profile.load_user_profile", return_value={"overnight_mode": "ZERO_OVERNIGHT_RISK"}), \
                        patch("execute_futures_trade.heal_orphan_position") as heal, \
                        patch("execute_futures_trade.move_sl_to_breakeven") as be, \
                        patch("execute_futures_trade.close_position_market", return_value=close_result) as close, \
                        patch("time.sleep"), patch("os.system", return_value=0), \
                        contextlib.redirect_stdout(io.StringIO()):
                    summary = ncl.run_night_cutoff(target_env="testnet")
                self.assertEqual(summary, {"close_failures": failures, "unprotected": [], "stop_unknown": ["SOLUSDT"]})
                close.assert_called_once_with("SOLUSDT", target_env="testnet")
                heal.assert_not_called()
                be.assert_not_called()

    def test_failure_banners(self):
        cases = [
            ("read_error", ("ZERO_OVERNIGHT_RISK", dict(READ_ERROR), [], None, None)),
            ("close_failures", ("CLOSE_ALL_AT_MARKET", [SOL], [], {"success": False, "error": "x"}, None)),
            ("unprotected", ("SWING_STRUCTURAL_STOP", [SOL], [], None,
                             {"success": False, "closed": False, "reason": "heal_and_close_failed"})),
        ]
        for key, args in cases:
            with self.subTest(key=key):
                code, out, *_ = self.run_night(*args)
                self.assertEqual(code, 1)
                self.assertIn("NIGHT CUTOFF INCOMPLETE", out)
                self.assertIn(key, out)
                self.assertNotIn("NIGHT CUTOFF COMPLETED", out)

    def test_clean_run_success_banner(self):
        for args in (("ZERO_OVERNIGHT_RISK", [], []), ("SWING_STRUCTURAL_STOP", [SOL], [SOL_STOP])):
            with self.subTest(mode=args[0]):
                code, out, *_ = self.run_night(*args)
                self.assertEqual(code, 0)
                self.assertIn("NIGHT CUTOFF COMPLETED", out)
                self.assertNotIn("INCOMPLETE", out)


# =============================================================================
# #151 decision 11: watchdog P0 contract
# =============================================================================
class TestWatchdogP0Contract(unittest.TestCase):

    def test_not_flat_close_files_one_report(self):
        fake = CloseExchange([long_position()], [REJECT_2022], algos=[stop(501, 95.0)])
        with keys_env(fake), patch("execute_futures_trade._report_close_failure") as rep:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        rep.assert_called_once()

    def test_early_returns_file_nothing(self):
        def unknown(method, endpoint, params=None, target_env=None, retry_count=0):
            return dict(READ_ERROR)
        hedge = CloseExchange([dict(long_position(), positionSide="LONG")], ["fill"])
        for name, fake in (("no position", CloseExchange([], ["fill"])), ("unknown", unknown), ("hedge", hedge)):
            with self.subTest(case=name):
                with keys_env(fake) as mock_report, patch("execute_futures_trade._report_close_failure") as rep:
                    res = eft.close_position_market("BTCUSDT", target_env="prod")
                self.assertFalse(res["success"])
                rep.assert_not_called()
                mock_report.assert_not_called()

    def test_watchdog_never_reports_itself(self):
        failed = {"success": False, "stop_protected": None, "stop_source": "unknown", "error": "x"}
        with offline(overdue_fake()), \
                patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(STALLED)), \
                patch("execute_futures_trade.close_position_market", return_value=failed), \
                patch("report_agent_issue.report_issue") as mock_report:
            rep = quiet(tdw.audit_dead_alpha, target_env="testnet", auto_exit=True)
        self.assertEqual(rep["positions"][0]["action_taken"], "AUTO_EXIT_FAILED")
        mock_report.assert_not_called()
        with open(tdw.__file__, "r", encoding="utf-8") as f:
            self.assertNotIn("report_agent_issue", f.read())


# =============================================================================
# #138: one close rule
# =============================================================================
class TestOneCloseRule(unittest.TestCase):

    def test_truth_table(self):
        D, U, A = pt.VERDICT_DEAD_ALPHA, pt.SOURCE_USER_TRADES, pt.SOURCE_TRADES_AUDIT
        self.assertEqual(pt.dead_alpha_close_decision(pt.VERDICT_HEALTHY, U, pt.STALL_STATUS), (False, "not dead alpha"))
        self.assertEqual(pt.dead_alpha_close_decision(pt.VERDICT_UNKNOWN, U, pt.STALL_STATUS)[0], False)
        allowed, reason = pt.dead_alpha_close_decision(D, A, pt.STALL_STATUS)
        self.assertFalse(allowed)
        self.assertIn("report only", reason)
        for status in ("HEALTHY_MOMENTUM", "UNKNOWN"):
            allowed, reason = pt.dead_alpha_close_decision(D, U, status)
            self.assertFalse(allowed)
            self.assertIn("not stalled", reason)
        self.assertEqual(pt.dead_alpha_close_decision(D, U, "DEAD_ALPHA_STALLED"), (True, None))
        self.assertEqual(pt.STALL_STATUS, "DEAD_ALPHA_STALLED")

    def watchdog(self, stall, fake=None):
        out = io.StringIO()
        kw = {"return_value": dict(stall)} if isinstance(stall, dict) else {"side_effect": stall}
        with offline(fake or overdue_fake()), patch("dynamic_exit_manager.check_dead_alpha_timeout", **kw), \
                patch("execute_futures_trade.close_position_market", return_value={"success": True}) as mc, \
                contextlib.redirect_stdout(out):
            rep = tdw.audit_dead_alpha(target_env="testnet", auto_exit=True)
        return rep["positions"][0], mc, out.getvalue()

    def test_healthy_range_skips_the_close(self):
        item, mc, out = self.watchdog(HEALTHY)
        mc.assert_not_called()
        self.assertEqual(item["action_taken"], "RECOMMEND_EXIT")
        self.assertIn("not stalled", item["auto_exit_skipped"])
        self.assertEqual(item["stall_status"], "HEALTHY_MOMENTUM")
        self.assertIn("AUTO-EXIT SKIPPED", out)

    def test_stall_lookup_failure_skips_the_close(self):
        item, mc, _ = self.watchdog(RuntimeError("klines down"))
        mc.assert_not_called()
        self.assertEqual(item["stall_status"], "UNKNOWN")

    def test_stalled_closes(self):
        item, mc, _ = self.watchdog(STALLED)
        mc.assert_called_once_with("BTCUSDT", target_env="testnet")
        self.assertEqual(item["action_taken"], "AUTO_EXIT_CLOSED")

    def test_report_only_run_makes_no_stall_lookup(self):
        with offline(overdue_fake()), patch("dynamic_exit_manager.check_dead_alpha_timeout") as stall:
            quiet(tdw.audit_dead_alpha, target_env="testnet", auto_exit=False)
        stall.assert_not_called()

    def test_watchdog_and_guardian_agree(self):
        closes = []
        for age, mark, upnl in t92.TestDoctorAndGuardianAgree.SCENARIOS:
            for stall in (STALLED, HEALTHY):
                with self.subTest(age=age, mark=mark, upnl=upnl, stall=stall["status"]):
                    def fake():
                        fills = {"BTCUSDT": [fill("BUY", 1, NOW - int(age * HOUR))]} if age is not None else {}
                        return FillsExchange([row("BTCUSDT", mark=mark, upnl=upnl)], fills=fills,
                                             algos=[stop(501, 95.0)],
                                             trades_error=None if age is not None else {"error": "x"})
                    _, wd_close, _ = self.watchdog(stall, fake())
                    log_dir = tempfile.mkdtemp()
                    self.addCleanup(shutil.rmtree, log_dir, True)
                    with offline(fake()), \
                            patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
                            patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(stall)), \
                            patch("execute_futures_trade.close_position_market",
                                  return_value={"success": True}) as g_close:
                        pgl.run_cycle("testnet", close_dead_alpha=True, log_dir=log_dir)
                    self.assertEqual(wd_close.called, g_close.called)
                    closes.append(wd_close.called)
        self.assertEqual(closes.count(True), 1, "only overdue + stagnant + stalled closes")


# =============================================================================
# #138: sync reads the audit once; userTrades diagnostics
# =============================================================================
class TestSyncAuditOnce(unittest.TestCase):

    def run_sync(self, fake, audit=()):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        audit_path = os.path.join(tmp, "trades_audit.jsonl")
        with open(audit_path, "w", encoding="utf-8") as f:
            for r in audit:
                f.write(json.dumps(r) + "\n")
        real_open = builtins.open
        opens = []

        def counting_open(file, *a, **kw):
            if os.path.abspath(str(file)) == os.path.abspath(audit_path):
                opens.append(file)
            return real_open(file, *a, **kw)
        with patch.object(sss, "LOGS_DIR", tmp), \
                patch.object(sss, "STATE_FILE", os.path.join(tmp, "session_state.json")), \
                patch.object(sss, "AUDIT_LOG", audit_path), \
                patch.dict(sys.modules, {"shadow_tracker": None}), \
                patch("builtins.open", side_effect=counting_open), \
                patch("execute_futures_trade.send_signed_request", side_effect=fake):
            state = sss.sync_session_state(target_env="testnet")
        return state, opens

    def test_one_audit_open_and_one_user_trades_call_per_position(self):
        fake = FillsExchange([row("BTCUSDT"), row("ETHUSDT", amt="-1")],
                             fills={"BTCUSDT": [fill("BUY", 1, NOW - 3 * HOUR)]})
        state, opens = self.run_sync(fake, audit=[t92.audit_rec(NOW - 7 * HOUR, symbol="ETHUSDT", direction="SHORT",
                                                                 sl_price=105.0)])
        self.assertEqual(len(opens), 1)
        per_symbol = [c[2]["symbol"] for c in fake.calls if c[1] == "/fapi/v1/userTrades" and "symbol" in c[2]]
        self.assertEqual(sorted(per_symbol), ["BTCUSDT", "ETHUSDT"])
        by = {p["symbol"]: p for p in state["active_positions"]}
        self.assertEqual(by["ETHUSDT"]["entry_time_source"], "trades_audit")
        self.assertEqual(by["BTCUSDT"]["entry_time_source"], "userTrades")
        self.assertNotIn("entry_time_error", by["BTCUSDT"])

    def test_entry_time_error_in_the_ledger(self):
        fake = FillsExchange([row("BTCUSDT")], trades_error={"code": -1003, "msg": "Too many requests"})
        state, _ = self.run_sync(fake)
        pos = state["active_positions"][0]
        self.assertTrue(pos["entry_time_error"].startswith("-1003"))
        self.assertTrue(pos["entry_time_rate_limited"])

    def test_load_audit_metadata_records_param(self):
        recs = [t92.audit_rec(1, sl_price=95.0), t92.audit_rec(2, symbol="ETHUSDT", env="prod")]
        with patch.object(sss, "AUDIT_LOG", os.path.join(tempfile.mkdtemp(), "missing.jsonl")):
            self.assertEqual(sorted(sss.load_audit_metadata("testnet", records=recs)), ["BTCUSDT"])
            self.assertEqual(sss.load_audit_metadata("testnet"), {})


class TestUserTradesDiagnostics(unittest.TestCase):

    def resolve(self, reply):
        def fetch(method, endpoint, params=None, target_env=None):
            if isinstance(reply, Exception):
                raise reply
            return reply
        return pt.resolve_entry_time_detailed("BTCUSDT", "LONG", "1", "testnet", entry_price=100.0, fetch=fetch,
                                              audit_records=[])

    def test_rate_limit_code(self):
        ts, source, diag = self.resolve({"code": -1003, "msg": "Too many requests"})
        self.assertEqual((ts, source), (None, "UNKNOWN"))
        self.assertTrue(diag["user_trades_error"].startswith("-1003"))
        self.assertTrue(diag["rate_limited"])

    def test_http_429_and_418(self):
        for reply in ({"error": "MCP Gateway HTTP 429: slow down"}, {"error": "x", "http_code": 418}):
            with self.subTest(reply=reply):
                self.assertTrue(self.resolve(reply)[2]["rate_limited"])

    def test_other_errors(self):
        diag = self.resolve(RuntimeError("boom"))[2]
        self.assertEqual((diag["user_trades_error"], diag["rate_limited"]), ("exception: RuntimeError: boom", False))
        self.assertEqual(self.resolve({"error": "not mapped"})[2]["user_trades_error"], "not mapped")
        self.assertEqual(self.resolve("text")[2]["user_trades_error"], "unexpected response: str")
        self.assertEqual(len(self.resolve({"error": "x" * 500})[2]["user_trades_error"]), 160)

    def test_fills_have_no_error(self):
        ts, source, diag = self.resolve([fill("BUY", 1, NOW - HOUR)])
        self.assertEqual(source, "userTrades")
        self.assertEqual(diag, {"user_trades_error": None, "rate_limited": False})

    def test_resolve_entry_time_is_still_a_two_tuple(self):
        res = pt.resolve_entry_time("BTCUSDT", "LONG", "1", "testnet", fetch=lambda *a, **k: {"code": -1003})
        self.assertEqual(res, (None, "UNKNOWN"))

    def test_watchdog_item(self):
        fake = FillsExchange([row("BTCUSDT")], trades_error={"code": -1003, "msg": "Too many requests"})
        with offline(fake):
            item = quiet(tdw.audit_dead_alpha, target_env="testnet")["positions"][0]
        self.assertTrue(item["entry_time_error"].startswith("-1003"))
        self.assertTrue(item["entry_time_rate_limited"])
        self.assertIn("Review manually", item["warning"])
        with offline(overdue_fake()):
            item = quiet(tdw.audit_dead_alpha, target_env="testnet")["positions"][0]
        self.assertNotIn("entry_time_error", item)
        self.assertNotIn("entry_time_rate_limited", item)


class TestNormEnv(unittest.TestCase):

    def test_public_norm_env(self):
        self.assertEqual(pt.norm_env("mainnet"), "prod")
        self.assertEqual(pt.norm_env("TESTNET"), "testnet")
        self.assertIsNone(pt.norm_env(""))
        self.assertEqual(pt.norm_env("Weird"), "weird")
        self.assertIs(pt._norm_env, pt.norm_env)


# =============================================================================
# #152: MCP numbers and hedge mode
# =============================================================================
class TestMcpEncoding(unittest.TestCase):

    def test_plain_decimal(self):
        for value, expected in ((1e-05, "0.00001"), (82000.0, "82000.0"), (0.05, "0.05"), (-1e-05, "-0.00001"),
                                ("0.00001234", "0.00001234"), (3, "3.0")):
            with self.subTest(value=value):
                self.assertEqual(eft._plain_decimal(value), expected)

    def test_json_dumps(self):
        obj = {"a": 1e-05, "b": 82000.0, "c": True, "d": None, "e": [0.05, "x"], "f": 7, "g": {"h": 1.234e-08}}
        text = eft._mcp_json_dumps(obj)
        self.assertNotIn("e-", text)
        self.assertNotIn("e+", text)
        self.assertIn("0.00001", text)
        self.assertEqual(json.loads(text), obj)
        self.assertIn('"f": 7', text)

    @patch("execute_futures_trade.get_mcp_oauth_token", return_value="fake_token")
    @patch("urllib.request.urlopen")
    def test_call_binance_mcp_body(self, mock_urlopen, _token):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"result": {"content": [{"type": "text", "text": "{\"ok\": 1}"}]}}).encode()
        mock_urlopen.return_value.__enter__.return_value = resp
        eft.call_binance_mcp("futures_usds.newOrder", {"symbol": "PEPEUSDT", "quantity": 1e-05})
        body = mock_urlopen.call_args[0][0].data.decode()
        self.assertIn('"quantity": 0.00001', body)
        self.assertNotIn("e-05", body)
        self.assertEqual(json.loads(body)["params"]["arguments"]["quantity"], 1e-05)

    @patch("execute_futures_trade.call_binance_mcp")
    def test_algo_order_trigger_and_quantity_strings(self, mock_mcp):
        def fake_mcp(tool_name, args=None, session_id=None):
            if tool_name == "futures_usds.positionInformationV2":
                return [{"symbol": "PEPEUSDT", "positionAmt": "0.00001"}]
            return {"algoId": 1}
        mock_mcp.side_effect = fake_mcp
        eft.send_mcp_gateway_request("POST", "/fapi/v1/algoOrder", params={
            "symbol": "PEPEUSDT", "side": "SELL", "type": "STOP_MARKET", "triggerPrice": 0.00001234,
            "closePosition": "true"})
        sent = mock_mcp.call_args[0][1]["arguments"]
        self.assertEqual((sent["triggerPrice"], sent["quantity"]), ("0.00001234", "0.00001"))
        eft.send_mcp_gateway_request("POST", "/fapi/v1/algoOrder", params={
            "symbol": "PEPEUSDT", "side": "SELL", "type": "STOP_MARKET", "triggerPrice": 0.00001234,
            "quantity": 1e-05, "reduceOnly": "true"})
        self.assertEqual(mock_mcp.call_args[0][1]["arguments"]["quantity"], "0.00001")


HEDGE_ROW = dict(long_position(), positionSide="LONG")


class TestHedgeMode(unittest.TestCase):

    def test_close_refused_without_orders(self):
        fake = CloseExchange([HEDGE_ROW], ["fill"], algos=[stop(501, 95.0)])
        with keys_env(fake) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertEqual((res["success"], res["hedge_mode"]), (False, True))
        self.assertIn("hedge mode", res["error"])
        self.assertEqual(fake.writes(), [])
        mock_report.assert_not_called()

    def test_heal_refused_without_any_order(self):
        fake = FakeExchange([HEDGE_ROW])
        with offline(fake), patch("report_agent_issue.report_issue") as mock_report, \
                patch("execute_futures_trade.get_symbol_filters") as filters:
            res = eft.heal_orphan_position(dict(HEDGE_ROW), target_env="prod", close_on_failure=True)
        self.assertEqual(res["reason"], "hedge_mode_unsupported")
        self.assertEqual(res["error"], eft.HEDGE_MODE_UNSUPPORTED)
        self.assertFalse(res["success"] or res["closed"])
        self.assertEqual(fake.calls, [])
        filters.assert_not_called()
        mock_report.assert_not_called()

    def test_move_breakeven_refused(self):
        fake = FakeExchange([HEDGE_ROW], algos=[stop(501, 95.0)])
        with offline(fake):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", force=True)
        self.assertEqual((res["success"], res["reason"]), (False, "hedge_mode_unsupported"))
        self.assertEqual(fake.writes(), [])

    def test_audit_refused(self):
        fake = FakeExchange([HEDGE_ROW])
        with offline(fake):
            res = eft.audit_orphan_positions(target_env="testnet", auto_heal=True)
        self.assertTrue(res["hedge_mode"])
        self.assertIn("error", res)
        self.assertEqual(fake.writes(), [])

    def test_one_way_rows_unchanged(self):
        for side in (None, "BOTH"):
            with self.subTest(position_side=side):
                pos = long_position() if side is None else dict(long_position(), positionSide=side)
                self.assertFalse(eft._is_hedge_mode_row(pos))
                fake = CloseExchange([pos], ["fill"])
                with keys_env(fake):
                    res = eft.close_position_market("BTCUSDT", target_env="prod")
                self.assertTrue(res["success"], res)
                self.assertNotIn("hedge_mode", res)


if __name__ == "__main__":
    unittest.main()
