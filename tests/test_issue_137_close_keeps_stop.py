#!/usr/bin/env python3
"""
test_issue_137_close_keeps_stop.py - Offline tests for issue #137 (no network, no orders).

close_position_market cancelled every order and algo order (the protective Stop Loss included) BEFORE the reduce-only
MARKET close and never checked the close result, so a rejected close left the position without a stop. Covered here:
1. KEYS and MCP: the MARKET close is the first write; nothing is cancelled before positionRisk shows flat; leftovers
   are cancelled after flat.
2. Rejected / failed close on every attempt (-2022, -1001, exception, MCP isError): no cancel at all, the stop is
   kept, success False, one CRITICAL/P0 report. No stop on the exchange: a verified stop is healed.
3. Partial fill then rejection (retry uses the residual), rejection then success, positionRisk error (no writes),
   reporting failure, cleanup error after flat.
4. Callers: watchdog auto-exit failure -> AUTO_EXIT_FAILED; guardian failed dead-alpha close -> error, cycle not ok.
"""

import io
import os
import sys
import time
import shutil
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
import position_guardian_loop as pgl
from test_exit_management import FakeExchange, offline, long_position, stop, ALGO_ENDPOINT
from test_issue_92_holding_time import FillsExchange, fill, row, quiet, STALLED, HOUR

ORDER_ENDPOINT = "/fapi/v1/order"
FILLED = {"orderId": 55, "status": "FILLED"}
REJECT_2022 = {"code": -2022, "msg": "ReduceOnly Order is rejected."}
REJECT_1001 = {"code": -1001, "msg": "Internal error; unable to process your request."}


class CloseExchange(FakeExchange):
    """FakeExchange whose reduce-only MARKET closes follow a script, one entry per attempt (the last one repeats):
    "fill" (flat), ("partial", residual_amt), an error dict, or an Exception instance (raised)."""

    def __init__(self, positions, closes, **kw):
        super().__init__(positions, **kw)
        self.closes = list(closes)
        self.close_qtys = []

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if method == "POST" and endpoint == ORDER_ENDPOINT and (params or {}).get("type") == "MARKET":
            self.calls.append((method, endpoint, dict(params)))
            self.close_qtys.append(params.get("quantity"))
            step = self.closes.pop(0) if len(self.closes) > 1 else self.closes[0]
            if isinstance(step, Exception):
                raise step
            if step == "fill":
                self.positions = []
                return dict(FILLED)
            if isinstance(step, tuple) and step[0] == "partial":
                for p in self.positions:
                    p["positionAmt"] = step[1]
                return {"orderId": 56, "status": "PARTIALLY_FILLED"}
            return dict(step)
        return super().__call__(method, endpoint, params, target_env, retry_count)

    def deletes(self):
        return [c for c in self.calls if c[0] == "DELETE"]


@contextlib.contextmanager
def keys_env(fake, report_side_effect=None):
    with offline(fake), patch("report_agent_issue.report_issue", side_effect=report_side_effect) as mock_report, \
            contextlib.redirect_stderr(io.StringIO()):
        yield mock_report


class FakeMCP:
    """Stateful stand-in for call_binance_mcp (raw gateway payloads). Records every tool call in order."""

    def __init__(self, amt, closes, algos=None, open_orders=None):
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": amt, "entryPrice": "100.0", "markPrice": "110.0"}]
        self.closes = list(closes)
        self.algos = [dict(a) for a in (algos or [])]
        self.open_orders = [dict(o) for o in (open_orders or [])]
        self.calls = []
        self.next_id = 7000

    def __call__(self, tool, args=None, session_id=None):
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool == "futures_usds.positionInformationV2":
            return [dict(p) for p in self.positions]
        if tool == "futures_usds.currentAllAlgoOpenOrders":
            return [dict(a) for a in self.algos]
        if tool == "futures_usds.currentAllOpenOrders":
            return [dict(o) for o in self.open_orders]
        if tool == "futures_usds.newOrder":
            step = self.closes.pop(0) if len(self.closes) > 1 else self.closes[0]
            if step == "fill":
                self.positions = []
                return dict(FILLED)
            return dict(step)
        if tool == "futures_usds.cancelOrder":
            self.open_orders = [o for o in self.open_orders if int(o["orderId"]) != args["orderId"]]
            return {"orderId": args["orderId"], "status": "CANCELED"}
        if tool == "futures_usds.cancelAlgoOrder":
            self.algos = [a for a in self.algos if int(a["algoId"]) != args["algoId"]]
            return {"algoId": args["algoId"], "code": "200", "msg": "success"}
        if tool == "tool_execute" and args.get("toolName") == "futures_usds.newAlgoOrder":
            a = args["arguments"]
            self.next_id += 1
            self.algos.append({"algoId": self.next_id, "symbol": a["symbol"], "side": a["side"],
                               "orderType": a["type"], "triggerPrice": a["triggerPrice"],
                               "quantity": a.get("quantity"), "reduceOnly": a.get("reduceOnly") == "true"})
            return {"algoId": self.next_id}
        return {"error": f"unexpected {tool}", "isError": True}

    def tools(self):
        return [c[0] for c in self.calls]

    def index(self, tool):
        return [i for i, t in enumerate(self.tools()) if t == tool]


MCP_STOP = {"algoId": 801, "symbol": "BTCUSDT", "side": "SELL", "orderType": "STOP_MARKET", "triggerPrice": "95.0",
            "quantity": "10", "reduceOnly": True}
MCP_TP = {"orderId": 901, "symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "price": "120.0", "reduceOnly": True}


@contextlib.contextmanager
def mcp_env(mcp, report_side_effect=None):
    gateway = eft.send_mcp_gateway_request

    def route(method, endpoint, params=None, target_env=None, retry_count=0):
        return gateway(method, endpoint, params=params)  # MCP_OAUTH_ACTIVE path of send_signed_request

    with offline(route), patch("execute_futures_trade.call_binance_mcp", side_effect=mcp), \
            patch("execute_futures_trade.get_client_config", return_value=("MCP_OAUTH_ACTIVE", None, None)), \
            patch("report_agent_issue.report_issue", side_effect=report_side_effect) as mock_report, \
            contextlib.redirect_stderr(io.StringIO()):
        yield mock_report


# =============================================================================
# 1. KEYS
# =============================================================================
class TestKeysClose(unittest.TestCase):

    def test_close_first_then_cancel_leftovers_after_flat(self):
        fake = CloseExchange([long_position()], ["fill"], algos=[stop(501, 95.0)])
        with keys_env(fake) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertTrue(res["success"], res)
        self.assertEqual((res["attempts"], res["cleanup_errors"], res["closed"]), (1, [], FILLED))
        writes = fake.writes()
        self.assertEqual((writes[0][0], writes[0][1], writes[0][2]["type"], writes[0][2]["reduceOnly"],
                          writes[0][2]["quantity"]), ("POST", ORDER_ENDPOINT, "MARKET", "true", "10"),
                         "the reduce-only close is the first write")
        first_delete = fake.calls.index(fake.deletes()[0])
        flat_reads = [i for i, c in enumerate(fake.calls) if c[1] == "/fapi/v2/positionRisk" and i < first_delete]
        self.assertGreaterEqual(len(flat_reads), 2, "positionRisk confirmed flat before any cancel")
        self.assertEqual([c[1] for c in fake.deletes()], ["/fapi/v1/allOpenOrders", ALGO_ENDPOINT])
        self.assertEqual(fake.algos, [], "leftover stop cancelled once flat")
        mock_report.assert_not_called()

    def test_rejected_close_on_every_attempt_keeps_the_stop(self):
        for reject in (REJECT_2022, REJECT_1001, RuntimeError("timed out")):
            with self.subTest(reject=reject):
                fake = CloseExchange([long_position()], [reject], algos=[stop(501, 95.0)])
                with keys_env(fake) as mock_report:
                    res = eft.close_position_market("BTCUSDT", target_env="prod")
                self.assertFalse(res["success"])
                self.assertEqual(fake.deletes(), [], "nothing is cancelled when the close fails")
                self.assertEqual([a["algoId"] for a in fake.algos], [501])
                self.assertEqual((res["attempts"], res["stop_source"], res["stop_protected"], res["position_amt"]),
                                 (3, "kept", True, 10.0))
                self.assertEqual(posts_of(fake, ALGO_ENDPOINT), [], "the kept stop needs no heal")
                self.assertEqual(len(fake.close_qtys), 3)
                mock_report.assert_called_once()
                kw = mock_report.call_args.kwargs
                self.assertEqual((kw["severity"], kw["priority"], kw["category"]), ("CRITICAL", "P0", "risk_gate"))
                self.assertIn("BTCUSDT", kw["title"])

    def test_rejected_close_without_stop_heals_a_verified_stop(self):
        fake = CloseExchange([long_position()], [REJECT_2022])
        with keys_env(fake) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        self.assertEqual(fake.deletes(), [])
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("healed", True))
        self.assertTrue(res["heal"]["verified"])
        self.assertEqual(len(fake.algos), 1)
        self.assertTrue(fake.algos[0]["closePosition"])
        self.assertEqual(fake.algos[0]["side"], "SELL")
        mock_report.assert_called_once()

    def test_rejected_close_and_failed_heal_reports_unprotected(self):
        fake = CloseExchange([long_position()], [REJECT_2022], reject_new_stops=True)
        with keys_env(fake) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        self.assertEqual(fake.deletes(), [])
        self.assertEqual((res["stop_source"], res["stop_protected"]), ("none", False))
        self.assertIn("stop none", mock_report.call_args.kwargs["error_detail"])

    def test_partial_fill_then_rejection_retries_residual_and_keeps_stop(self):
        fake = CloseExchange([long_position(amt="10")], [("partial", "4"), REJECT_2022], algos=[stop(501, 95.0)])
        with keys_env(fake) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        self.assertEqual(fake.close_qtys, ["10", "4", "4"], "retries close the residual, never a zero quantity")
        self.assertEqual(fake.deletes(), [])
        self.assertEqual((res["position_amt"], res["stop_source"]), (4.0, "kept"))
        self.assertEqual([a["algoId"] for a in fake.algos], [501])
        mock_report.assert_called_once()

    def test_first_attempt_rejected_second_succeeds(self):
        fake = CloseExchange([long_position()], [REJECT_1001, "fill"], algos=[stop(501, 95.0)])
        with keys_env(fake) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["attempts"], 2)
        closes = fake.write_index("POST", ORDER_ENDPOINT)
        self.assertEqual(len(closes), 2)
        self.assertTrue(all(i > closes[-1] for i in [fake.calls.index(d) for d in fake.deletes()]),
                        "cancels only after the successful close")
        self.assertEqual(fake.algos, [])
        mock_report.assert_not_called()

    def test_flat_on_reread_after_rejection_is_success(self):
        """The close response was lost but positionRisk shows flat: done, no further close is sent."""
        fake = CloseExchange([long_position()], [RuntimeError("read timeout")], algos=[stop(501, 95.0)])
        original = fake.__call__

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            try:
                return original(method, endpoint, params, target_env, retry_count)
            finally:
                if method == "POST" and endpoint == ORDER_ENDPOINT:
                    fake.positions = []
        with offline(send), patch("report_agent_issue.report_issue") as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertTrue(res["success"], res)
        self.assertEqual((res["attempts"], len(fake.close_qtys)), (1, 1))
        self.assertEqual(fake.algos, [])
        mock_report.assert_not_called()

    def test_lower_case_symbol_is_normalised(self):
        """A lower-case symbol (exchange adapter) must not read as flat before the close is confirmed."""
        fake = CloseExchange([long_position(amt="10")], [("partial", "4"), REJECT_2022], algos=[stop(501, 95.0)])
        with keys_env(fake) as mock_report:
            res = eft.close_position_market("btcusdt", target_env="prod")
        self.assertFalse(res["success"], "a partial fill must not count as flat")
        self.assertEqual(fake.close_qtys, ["10", "4", "4"])
        self.assertTrue(all(c[2].get("symbol") == "BTCUSDT" for c in fake.calls if c[2].get("symbol")),
                        "every request uses the upper-case symbol")
        self.assertEqual(fake.deletes(), [])
        self.assertEqual(res["stop_source"], "kept")
        mock_report.assert_called_once()

        fake = CloseExchange([long_position()], ["fill"], algos=[stop(501, 95.0)])
        with keys_env(fake):
            res = eft.close_position_market("btcusdt", target_env="prod")
        self.assertTrue(res["success"], res)
        self.assertEqual(fake.writes()[0][2]["symbol"], "BTCUSDT")
        self.assertEqual(fake.algos, [])

    def test_short_position_closes_with_buy(self):
        fake = CloseExchange([long_position(amt="-3")], ["fill"], algos=[stop(501, 120.0, side="BUY")])
        with keys_env(fake):
            res = eft.close_position_market("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"], res)
        self.assertEqual((fake.writes()[0][2]["side"], fake.writes()[0][2]["quantity"]), ("BUY", "3"))

    def test_position_risk_error_is_unknown_state_and_sends_nothing(self):
        for err in ({"code": -1003, "msg": "Too many requests"}, RuntimeError("network down"), "<html>502</html>"):
            with self.subTest(err=err):
                fake = CloseExchange([long_position()], ["fill"], algos=[stop(501, 95.0)])

                def send(method, endpoint, params=None, target_env=None, retry_count=0):
                    if endpoint == "/fapi/v2/positionRisk":
                        fake.calls.append((method, endpoint, dict(params or {})))
                        if isinstance(err, Exception):
                            raise err
                        return err
                    return fake(method, endpoint, params, target_env, retry_count)
                with offline(send), patch("report_agent_issue.report_issue") as mock_report:
                    res = eft.close_position_market("BTCUSDT", target_env="prod")
                self.assertFalse(res["success"])
                self.assertIn("position state unknown", res["error"])
                self.assertEqual(fake.writes(), [])
                mock_report.assert_not_called()

    def test_no_open_position_keeps_message(self):
        fake = CloseExchange([], ["fill"])
        with keys_env(fake):
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertEqual(res, {"success": False, "error": "No open position in BTCUSDT"})
        self.assertEqual(fake.writes(), [])

    def test_reporting_failure_never_changes_the_result(self):
        fake = CloseExchange([long_position()], [REJECT_2022], algos=[stop(501, 95.0)])
        with keys_env(fake, report_side_effect=RuntimeError("github down")) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        mock_report.assert_called_once()
        self.assertFalse(res["success"])
        self.assertEqual((res["stop_source"], res["attempts"]), ("kept", 3))
        self.assertEqual(fake.deletes(), [])

    def test_cleanup_error_after_flat_is_still_success(self):
        fake = CloseExchange([long_position()], ["fill"], algos=[stop(501, 95.0)], fail_cancel=True)
        with keys_env(fake) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(res["cleanup_errors"]), 1)
        self.assertIn("501", res["cleanup_errors"][0])
        mock_report.assert_not_called()


def posts_of(fake, endpoint):
    return [c[2] for c in fake.calls if c[0] == "POST" and c[1] == endpoint]


# =============================================================================
# 2. MCP gateway
# =============================================================================
class TestMcpClose(unittest.TestCase):

    def test_close_first_then_cancel_leftovers_after_flat(self):
        mcp = FakeMCP("10", ["fill"], algos=[MCP_STOP], open_orders=[MCP_TP])
        with mcp_env(mcp) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertTrue(res["success"], res)
        new_order = mcp.index("futures_usds.newOrder")
        cancels = mcp.index("futures_usds.cancelOrder") + mcp.index("futures_usds.cancelAlgoOrder")
        self.assertEqual(len(new_order), 1)
        self.assertEqual(mcp.calls[new_order[0]][1], {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET",
                                                      "quantity": 10.0, "reduceOnly": "true"})
        self.assertEqual(len(cancels), 2)
        self.assertTrue(all(i > new_order[0] for i in cancels), "nothing cancelled before the close")
        self.assertEqual((mcp.algos, mcp.open_orders), ([], []))
        mock_report.assert_not_called()

    def test_gateway_error_on_every_attempt_keeps_the_stop(self):
        mcp = FakeMCP("10", [{"error": "MCP Gateway Error: timed out", "isError": True}],
                      algos=[MCP_STOP], open_orders=[MCP_TP])
        with mcp_env(mcp) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        self.assertEqual(len(mcp.index("futures_usds.newOrder")), 3)
        self.assertEqual(mcp.index("futures_usds.cancelOrder") + mcp.index("futures_usds.cancelAlgoOrder"), [])
        self.assertEqual(([a["algoId"] for a in mcp.algos], len(mcp.open_orders)), ([801], 1))
        self.assertEqual(res["stop_source"], "kept")
        mock_report.assert_called_once()
        self.assertEqual(mock_report.call_args.kwargs["severity"], "CRITICAL")

    def test_gateway_error_without_stop_heals_reduce_only_stop(self):
        mcp = FakeMCP("10", [{"error": "MCP Gateway Error: 503", "isError": True}])
        with mcp_env(mcp) as mock_report:
            res = eft.close_position_market("BTCUSDT", target_env="prod")
        self.assertFalse(res["success"])
        self.assertEqual(mcp.index("futures_usds.cancelOrder") + mcp.index("futures_usds.cancelAlgoOrder"), [])
        self.assertEqual(res["stop_source"], "healed")
        self.assertEqual(len(mcp.algos), 1)
        self.assertEqual((mcp.algos[0]["side"], mcp.algos[0]["quantity"], mcp.algos[0]["reduceOnly"]),
                         ("SELL", "10", True))   # issue #152: plain decimal (format_order_qty)
        mock_report.assert_called_once()


# =============================================================================
# 3. Callers
# =============================================================================
FAILED_CLOSE = {"success": False, "error": "Reduce-only MARKET close of BTCUSDT not confirmed flat", "stop_source": "kept"}


class TestCallersBranchOnSuccess(unittest.TestCase):

    def overdue_fake(self):
        return FillsExchange([row("BTCUSDT")], fills={"BTCUSDT": [fill("BUY", 1, int(time.time()) - 6 * HOUR)]},
                             algos=[stop(501, 95.0)])

    def test_watchdog_auto_exit_failure(self):
        with offline(self.overdue_fake()), \
                patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(STALLED)), \
                patch("execute_futures_trade.close_position_market", return_value=dict(FAILED_CLOSE)) as mc:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rep = tdw.audit_dead_alpha(target_env="testnet", auto_exit=True)
        mc.assert_called_once_with("BTCUSDT", target_env="testnet")
        item = rep["positions"][0]
        self.assertEqual(item["action_taken"], "AUTO_EXIT_FAILED")
        self.assertEqual(item["close_result"], FAILED_CLOSE)
        self.assertIn("AUTO-EXIT FAILED", out.getvalue())
        self.assertNotIn("Position closed at market", out.getvalue())

    def test_watchdog_auto_exit_success_unchanged(self):
        with offline(self.overdue_fake()), \
                patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(STALLED)), \
                patch("execute_futures_trade.close_position_market", return_value={"success": True}):
            rep = quiet(tdw.audit_dead_alpha, target_env="testnet", auto_exit=True)
        self.assertEqual(rep["positions"][0]["action_taken"], "AUTO_EXIT_CLOSED")

    def run_guardian(self, close_result):
        log_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, log_dir, True)
        fake = FillsExchange([row("BTCUSDT", mark="100.5", upnl="0.5")],
                             fills={"BTCUSDT": [fill("BUY", 1, int(time.time()) - 5 * HOUR)]}, algos=[stop(501, 95.0)])
        with offline(fake), \
                patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
                patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(STALLED)), \
                patch("execute_futures_trade.close_position_market", return_value=close_result) as mock_close:
            state = pgl.run_cycle("testnet", close_dead_alpha=True, log_dir=log_dir)
        mock_close.assert_called_once_with("BTCUSDT", target_env="testnet")
        return state

    def test_guardian_failed_dead_alpha_close_is_an_error(self):
        state = self.run_guardian(dict(FAILED_CLOSE))
        errs = [e for e in state["errors"] if e["stage"] == "dead_alpha_close"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["symbol"], "BTCUSDT")
        self.assertIn("not confirmed flat", errs[0]["error"])
        self.assertFalse(state["cycle_ok"])

    def test_guardian_successful_dead_alpha_close_is_not_an_error(self):
        state = self.run_guardian({"success": True})
        self.assertFalse([e for e in state["errors"] if e["stage"] == "dead_alpha_close"])


if __name__ == "__main__":
    unittest.main()
