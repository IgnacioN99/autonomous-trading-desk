#!/usr/bin/env python3
"""
test_pending_entries.py - Offline tests for Issue #33 (post-fill protection of resting entries).

1. Routing: an untriggered STOP_MARKET entry goes to the algo order API (algoType=CONDITIONAL, triggerPrice,
   quantity, closePosition=false), never to /fapi/v1/order; through the MCP gateway it becomes an opening
   (non reduce-only) newAlgoOrder.
2. Registry: resting STOP_MARKET / LIMIT entries are recorded in logs/pending_entries.json; a registry failure
   cancels the placed entry and fails.
3. PROD gates for resting entries: live guardian, no open position, no pending entry on the symbol.
4. protect_pending_entries: planned SL on fill (place / replace / resize), TPs from the actual size, audit
   record, auto-destruct when the SL cannot be verified, timeout cancel, drop rules, fail-closed queries, dry run.
5. Position guardian runs it first (no orphan emergency stop on a fresh fill); CLI --protect-pending.
6. Issue #46: a missing / incomplete registry is cross-checked against the exchange: PROD rejects every new entry
   while an opening order rests without a record (or the check fails); the guardian reports such entries.

No network: every exchange call is faked and every file goes to a temp directory.
"""

import io
import os
import sys
import json
import time
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
from test_exit_management import FakeExchange, offline, long_position, stop, run_cli, ALGO_ENDPOINT

ORDER_ENDPOINT = "/fapi/v1/order"
EX_FILTERS = {"stepSize": 0.001, "minQty": 0.001, "tickSize": 0.01, "precision_qty": 3, "precision_price": 2, "minNotional": 5.0}
EX_PROFILE = {"yolo_slot_enabled": True, "leverage_standard": 3, "leverage_yolo": 15, "max_open_positions": 100,
              "risk_pct_equity": 0.005, "max_margin_ratio": 0.30}
INTERNAL_ERROR = {"code": -1001, "msg": "Internal error; unable to process your request."}
HEALTHY = {"status": "HEALTHY_MOMENTUM", "range_pct": 1.2, "recommendation": "HOLD", "message": "ok"}


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def make_record(kind="STOP_MARKET", entry_id="7001", symbol="BTCUSDT", direction="LONG", env="testnet",
                total_qty=12.0, sl=95.0, tp1=110.0, tp2=120.0, expires_in=3600, **extra):
    now = int(time.time())
    rec = {"kind": kind, "entry_id": str(entry_id), "symbol": symbol, "direction": direction,
           "entry_side": "BUY" if direction == "LONG" else "SELL", "exit_side": "SELL" if direction == "LONG" else "BUY",
           "target_env": env, "trigger_or_limit_price": 101.0, "total_qty": total_qty, "sl_price": sl,
           "tp1_price": tp1, "tp2_price": tp2, "leverage": 3, "is_yolo": False,
           "placed_at_ts": now - 60, "expires_at_ts": now + expires_in}
    rec.update(extra)
    # Issue #126: margin_usdt consistent with the record's own sizing (total_qty x price / leverage), as at
    # registration, unless a test sets it; a shrunk total_qty is tested explicitly (test_issue_48_pending_risk_view).
    rec.setdefault("margin_usdt", round(float(rec["total_qty"]) * float(rec["trigger_or_limit_price"])
                                        / float(rec["leverage"]), 8))
    return rec


def write_registry(ws, *records):
    entries = {eft.pending_entry_key(r["target_env"], r["symbol"], r["entry_id"]): r for r in records}
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    with open(os.path.join(ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "entries": entries}, f)
    return list(entries)


def read_registry(ws):
    path = os.path.join(ws, "logs", "pending_entries.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["entries"]


def read_jsonl(ws, name):
    path = os.path.join(ws, "logs", name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_audit_sl(ws):
    audit = read_jsonl(ws, "trades_audit.jsonl")
    return audit[-1]["sl_price"] if audit else None


def write_guardian_state(ws, env="prod", dry_run=False, age=5, mode="loop", interval_seconds=60):
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    with open(os.path.join(ws, "logs", "guardian_state.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "timestamp": int(time.time()) - age, "env": env, "dry_run": dry_run,
                   "mode": mode, "interval_seconds": interval_seconds,
                   "cycle_ok": True, "positions": [], "actions": [], "errors": []}, f)


def write_session_state(ws, positions=(), bias="DELTA_BALANCED"):
    """A fresh, valid logs/session_state.json consistent with the fake exchange's positionRisk rows (issue #101: PROD
    Gate 0A rejects a missing cache and takes the stricter of the cache and the live view)."""
    active = [{"symbol": p["symbol"], "qty": float(p.get("positionAmt", 0))} for p in positions
              if float(p.get("positionAmt", 0) or 0) != 0]
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    with open(os.path.join(ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
        json.dump({"is_valid": True, "last_updated_ts": int(time.time()), "active_positions": active,
                   "portfolio_exposure": {"total_active_positions": len(active), "delta_bias": bias}}, f)


def entry_algo(algo_id=7001, symbol="BTCUSDT", side="BUY", trigger=101.0):
    """A resting conditional ENTRY (not protective: neither closePosition nor reduceOnly)."""
    return {"algoId": algo_id, "symbol": symbol, "side": side, "orderType": "STOP_MARKET",
            "triggerPrice": str(trigger), "quantity": "12", "closePosition": False, "reduceOnly": False}


def posts(fake, endpoint):
    return [c[2] for c in fake.calls if c[0] == "POST" and c[1] == endpoint]


# ---------------------------------------------------------------------------------------------
# Executor harness (execute_complete_trade with a fake exchange)
# ---------------------------------------------------------------------------------------------
class ExecutorHarness(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.calls = []
        self.positions = []
        self.positions_error = False
        self.open_algos = []      # GET /fapi/v1/openAlgoOrders (resting entries on the exchange, Issue #46)
        self.open_orders = []     # GET /fapi/v1/openOrders
        self.open_errors = {}     # endpoint -> error response

    def fake(self, method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        self.calls.append((method, endpoint, params))
        if method == "GET" and endpoint in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
            if endpoint in self.open_errors:
                return self.open_errors[endpoint]
            listed = self.open_algos if endpoint == "/fapi/v1/openAlgoOrders" else self.open_orders
            return [dict(o) for o in listed if not params.get("symbol") or o["symbol"] == params["symbol"]]
        if endpoint == "/fapi/v1/marginType":
            return {"code": 200, "msg": "success"}
        if endpoint == "/fapi/v1/leverage":
            return {"symbol": params["symbol"], "leverage": params["leverage"]}
        if endpoint == "/fapi/v1/leverageBracket":
            return {"error": "unavailable"}
        if endpoint == "/fapi/v1/ticker/price":
            return {"price": "100.0"}
        if endpoint == "/fapi/v2/positionRisk":
            # positions_error: True fails every query; "symbol" only the per-symbol ones (the all-symbol live
            # snapshot of the PROD gates, issue #101, still succeeds)
            failing = self.positions_error is True or (self.positions_error == "symbol" and params.get("symbol"))
            return {"error": "timeout"} if failing else [dict(p) for p in self.positions]
        if endpoint == ALGO_ENDPOINT and method == "POST":
            if params.get("closePosition") == "true" or params.get("reduceOnly") == "true":
                # A protective stop (issue #36 pre-arm): echoed on GET openAlgoOrders so its verification passes.
                # Entries are not echoed (a later execute() would see them as unregistered resting entries).
                self.open_algos.append({"algoId": 9, "symbol": params["symbol"], "side": params["side"],
                                        "orderType": params["type"], "triggerPrice": str(params["triggerPrice"]),
                                        "closePosition": params.get("closePosition") == "true",
                                        "reduceOnly": params.get("reduceOnly") == "true"})
                return {"algoId": 9}
            return {"algoId": 8}
        if endpoint == ALGO_ENDPOINT and method == "DELETE":
            return {"algoId": params.get("algoId"), "code": "200", "msg": "success"}
        if endpoint == ORDER_ENDPOINT and method == "POST":
            status = "NEW" if params.get("type") == "LIMIT" and params.get("reduceOnly") != "true" else "FILLED"
            return {"orderId": 7, "avgPrice": "100.0", "status": status}
        if endpoint == ORDER_ENDPOINT and method == "DELETE":
            return {"orderId": params.get("orderId"), "status": "CANCELED"}
        return {}

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "DELETE")]

    def execute(self, env="testnet", send=None, mcp=False, **kwargs):
        args = dict(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                    sl_price=97.0, tp1_price=110.0, tp2_price=120.0, target_env=env)
        if env == "testnet":
            args["bypass_eval_gate"] = True
        args.update(kwargs)
        if not os.path.exists(os.path.join(self.ws, "logs", "session_state.json")):
            write_session_state(self.ws, self.positions)  # PROD Gate 0A requires the cache (issue #101), as live
        # place_algo_stop_loss is not mocked: the issue #36 pre-arm reaches self.fake as an algo POST (id 9, echoed).
        with patch("execute_futures_trade.send_signed_request", side_effect=send or self.fake), \
             patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
             patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.enforce_evaluation_dossier",  # eval_result: issue #202 audit tests
                   return_value=getattr(self, "eval_result", (True, "ok", None))), \
             patch("execute_futures_trade.check_mechanical_gates", return_value=(True, None)), \
             patch("execute_futures_trade.get_symbol_filters", return_value=dict(EX_FILTERS)), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=mcp), \
             patch("execute_futures_trade.subprocess.run", side_effect=FileNotFoundError("binance-cli")), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 9})), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("user_profile.load_user_profile", return_value=dict(EX_PROFILE)), \
             patch("report_agent_issue.report_issue") as self.report:   # issue #157: pre-arm anomalies are reported
            return eft.execute_complete_trade(**args)


class TestConditionalEntryRouting(ExecutorHarness):

    def test_untriggered_stop_market_uses_algo_order_api(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res["conditional_entry"])
        self.assertEqual(res["orderId"], 8)
        self.assertEqual([c for c in self.calls if c[0] == "POST" and c[1] == ORDER_ENDPOINT], [],
                         "conditional entries must never be sent to /fapi/v1/order (-4120)")
        algo = [c[2] for c in self.calls if c[0] == "POST" and c[1] == ALGO_ENDPOINT]
        self.assertEqual(len(algo), 2, "the entry, then the issue #36 pre-armed stop (KEYS)")
        self.assertEqual(algo[0], {"algoType": "CONDITIONAL", "symbol": "SOLUSDT", "side": "BUY", "type": "STOP_MARKET",
                                   "triggerPrice": 102.34, "quantity": 0.293, "closePosition": "false",
                                   "workingType": "CONTRACT_PRICE"})
        self.assertNotIn("reduceOnly", algo[0])
        self.assertEqual(algo[1], {"algoType": "CONDITIONAL", "symbol": "SOLUSDT", "side": "SELL", "type": "STOP_MARKET",
                                   "triggerPrice": 97.0, "closePosition": "true"})
        self.assertIn("--protect-pending", res["message"])
        self.assertNotIn("deferred to fill.", res["message"])

    def test_stop_market_registered_for_post_fill_protection(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertEqual(res["pending_entry_key"], "testnet:SOLUSDT:8")
        entries = read_registry(self.ws)
        rec = entries["testnet:SOLUSDT:8"]
        self.assertEqual(rec["kind"], "STOP_MARKET")
        self.assertEqual(rec["entry_id"], "8")
        self.assertEqual((rec["symbol"], rec["direction"], rec["entry_side"], rec["exit_side"], rec["target_env"]),
                         ("SOLUSDT", "LONG", "BUY", "SELL", "testnet"))
        self.assertEqual(rec["trigger_or_limit_price"], 102.34)
        self.assertEqual(rec["total_qty"], 0.293)
        self.assertEqual((rec["sl_price"], rec["tp1_price"], rec["tp2_price"]), (97.0, 110.0, 120.0))
        self.assertEqual((rec["leverage"], rec["is_yolo"], rec["margin_usdt"]), (3, False, 10.0))
        self.assertEqual(rec["expires_at_ts"] - rec["placed_at_ts"], eft.PENDING_ENTRY_TIMEOUT_SECONDS)
        self.assertEqual(eft.PENDING_ENTRY_TIMEOUT_SECONDS, 5400)

    def test_resting_limit_registered(self):
        res = self.execute(order_type="LIMIT", limit_price=98.767)
        self.assertTrue(res["pending_limit_entry"], res)
        self.assertEqual(res["pending_entry_key"], "testnet:SOLUSDT:7")
        rec = read_registry(self.ws)["testnet:SOLUSDT:7"]
        self.assertEqual(rec["kind"], "LIMIT")
        self.assertEqual(rec["trigger_or_limit_price"], 98.76)
        self.assertEqual(rec["total_qty"], 0.303)

    def test_registry_failure_cancels_stop_market_entry(self):
        with patch("execute_futures_trade.update_pending_entries", side_effect=IOError("disk full")):
            res = self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertFalse(res["success"])
        self.assertTrue(res["pending_registry_failure"])
        self.assertTrue(res["entry_cancelled"])
        self.assertIn("disk full", res["error"])
        cancels = [c[2] for c in self.calls if c[0] == "DELETE" and c[1] == ALGO_ENDPOINT]
        # the entry, then its issue #36 pre-armed stop (by algo id; no position)
        self.assertEqual(cancels, [{"symbol": "SOLUSDT", "algoId": 8}, {"symbol": "SOLUSDT", "algoId": 9}])
        self.assertTrue(res["prearm_cancelled"])

    def test_registry_failure_cancels_limit_entry(self):
        with patch("execute_futures_trade.update_pending_entries", side_effect=IOError("disk full")):
            res = self.execute(order_type="LIMIT", limit_price=98.767)
        self.assertFalse(res["success"])
        self.assertTrue(res["entry_cancelled"])
        cancels = [c[2] for c in self.calls if c[0] == "DELETE" and c[1] == ORDER_ENDPOINT]
        self.assertEqual(cancels, [{"symbol": "SOLUSDT", "orderId": 7}])

    @patch("execute_futures_trade.call_binance_mcp")
    def test_mcp_conditional_entry_is_an_opening_algo_order(self, mock_mcp):
        def mcp(tool, args=None, session_id=None):
            if tool == "tool_execute":
                return {"orderId": 4242}
            if tool == "futures_usds.positionInformationV2":
                return []
            return {"error": f"unexpected {tool}", "isError": True}
        mock_mcp.side_effect = mcp
        gateway = eft.send_mcp_gateway_request

        def route(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint in ("/fapi/v1/marginType", "/fapi/v1/leverage", "/fapi/v1/leverageBracket", "/fapi/v1/ticker/price"):
                return self.fake(method, endpoint, params, target_env)
            return gateway(method, endpoint, params=params)  # MCP_OAUTH_ACTIVE path of send_signed_request

        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347, send=route, mcp=True)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["orderId"], 4242)
        self.assertEqual(res["prearm_status"], "skipped:mcp", "no pre-arm through the MCP gateway (issue #36)")
        mock_mcp.assert_called_once_with("tool_execute", {
            "toolName": "futures_usds.newAlgoOrder",
            "arguments": {"symbol": "SOLUSDT", "side": "BUY", "type": "STOP_MARKET", "algoType": "CONDITIONAL",
                          "triggerPrice": "102.34", "workingType": "CONTRACT_PRICE", "quantity": "0.293"},
        })
        self.assertIn("testnet:SOLUSDT:4242", read_registry(self.ws))

    @patch("execute_futures_trade.call_binance_mcp")
    def test_mcp_gateway_entry_params_never_reduce_only(self, mock_mcp):
        mock_mcp.return_value = {"algoId": 5}
        eft.send_mcp_gateway_request("POST", ALGO_ENDPOINT, params={
            "algoType": "CONDITIONAL", "symbol": "SOLUSDT", "side": "SELL", "type": "STOP_MARKET",
            "triggerPrice": 98.5, "quantity": 0.5, "closePosition": "false", "workingType": "CONTRACT_PRICE"})
        sent = mock_mcp.call_args[0][1]["arguments"]
        self.assertEqual(sent["quantity"], "0.5")
        self.assertNotIn("reduceOnly", sent)
        self.assertNotIn("closePosition", sent)
        self.assertEqual(mock_mcp.call_count, 1, "no position lookup for an opening order")


class TestStopRecognition(unittest.TestCase):

    def test_resting_entry_algo_never_verifies_as_stop_loss(self):
        # A pending SHORT entry (SELL STOP_MARKET, neither closePosition nor reduceOnly) at the SL price
        entry = entry_algo(side="SELL", trigger=95.0)
        with patch("execute_futures_trade.send_signed_request", return_value=[entry]):
            self.assertEqual(eft.verify_algo_stop_loss("BTCUSDT", "SELL", 95.0, target_env="testnet"), (False, None))
            self.assertEqual(eft.verify_algo_stop_loss("BTCUSDT", "SELL", None, target_env="testnet"), (False, None))
        real = stop(1, 95.0)
        with patch("execute_futures_trade.send_signed_request", return_value=[entry, real]):
            self.assertEqual(eft.verify_algo_stop_loss("BTCUSDT", "SELL", 95.0, target_env="testnet"), (True, real))
        with patch("execute_futures_trade.send_signed_request", return_value=[dict(real, symbol="ETHUSDT")]):
            self.assertFalse(eft.verify_algo_stop_loss("BTCUSDT", "SELL", 95.0, target_env="testnet")[0])

    @patch("execute_futures_trade.call_binance_mcp")
    def test_mcp_open_algo_string_false_is_not_close_position(self, mock_mcp):
        mock_mcp.return_value = [
            {"algoId": 1, "symbol": "BTCUSDT", "side": "BUY", "orderType": "STOP_MARKET", "triggerPrice": "101",
             "closePosition": "false", "reduceOnly": "false"},
            {"algoId": 2, "symbol": "BTCUSDT", "side": "SELL", "orderType": "STOP_MARKET", "triggerPrice": "95",
             "closePosition": "true"},
        ]
        res = eft.send_mcp_gateway_request("GET", "/fapi/v1/openAlgoOrders", params={"symbol": "BTCUSDT"})
        self.assertEqual([r["closePosition"] for r in res], [False, True])
        self.assertFalse(eft.is_protective_stop(res[0], "BUY", "BTCUSDT"))


class TestPartiallyFilledLimitAtPlacement(unittest.TestCase):

    def _execute(self, fx, mcp=False, executed_qty="0.1", position_visible=True, filled_at_once=False):
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
            if method == "POST" and endpoint == ORDER_ENDPOINT and params.get("type") == "LIMIT" and params.get("reduceOnly") != "true":
                fx.calls.append((method, endpoint, params))
                if not filled_at_once:  # remainder still resting
                    fx.open_orders.append({"orderId": 7, "symbol": params["symbol"], "side": params["side"], "type": "LIMIT",
                                           "price": str(params["price"]), "origQty": str(params["quantity"]), "reduceOnly": False})
                if position_visible:
                    fx.positions.append(long_position("SOLUSDT", amt="0.1" if not filled_at_once else str(params["quantity"]),
                                                      entry=str(params["price"])))
                resp = {"orderId": 7, "status": "PARTIALLY_FILLED"}
                if executed_qty is not None:
                    resp["executedQty"] = executed_qty
                return resp
            return fx(method, endpoint, params, target_env)

        with patch("execute_futures_trade.send_signed_request", side_effect=send), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=mcp), \
             patch("execute_futures_trade._workspace_dir", return_value=ws), \
             patch("execute_futures_trade.get_symbol_filters", return_value=dict(EX_FILTERS)), \
             patch("execute_futures_trade.check_mechanical_gates", return_value=(True, None)), \
             patch("execute_futures_trade.subprocess.run", side_effect=FileNotFoundError("binance-cli")), \
             patch("execute_futures_trade.time.sleep", return_value=None), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("user_profile.load_user_profile", return_value=dict(EX_PROFILE)):
            res = eft.execute_complete_trade(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                                             sl_price=97.0, tp1_price=110.0, tp2_price=120.0, target_env="testnet",
                                             order_type="LIMIT", limit_price=98.767, bypass_eval_gate=True)
        return res, ws

    def test_partial_fill_registered_and_sl_placed_immediately(self):
        fx = FakeExchange([])
        res, ws = self._execute(fx)
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res["pending_limit_entry"])
        self.assertEqual(res["status"], "PARTIALLY_FILLED")
        self.assertTrue(res["partial_fill_protected"])
        rec = read_registry(ws)["testnet:SOLUSDT:7"]
        self.assertEqual(rec["sl_qty"], 0.1)
        self.assertEqual([(a["side"], float(a["triggerPrice"]), a["closePosition"]) for a in fx.algos], [("SELL", 97.0, True)])
        self.assertEqual([c for c in fx.calls if c[0] == "POST" and c[1] == ORDER_ENDPOINT and c[2].get("reduceOnly") == "true"],
                         [], "no TP sized from total_qty while the LIMIT still rests")

    def test_sl_from_executed_qty_when_position_never_visible_mcp(self):
        fx = FakeExchange([])
        res, ws = self._execute(fx, mcp=True, position_visible=False)  # positionRisk stays empty throughout
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res["partial_fill_protected"])
        sent = posts(fx, ALGO_ENDPOINT)
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["side"], sent[0]["triggerPrice"], sent[0]["quantity"], sent[0]["reduceOnly"]),
                         ("SELL", 97.0, 0.1, "true"))
        self.assertNotIn("closePosition", sent[0])
        self.assertEqual(read_registry(ws)["testnet:SOLUSDT:7"]["sl_qty"], 0.1)
        self.assertIn("TPs are placed once the entry is fully filled", res["message"])

    def test_sl_from_executed_qty_keys_uses_close_position(self):
        fx = FakeExchange([])
        res, _ = self._execute(fx, mcp=False, position_visible=False)
        self.assertTrue(res["partial_fill_protected"])
        sent = posts(fx, ALGO_ENDPOINT)
        self.assertEqual(sent[0]["closePosition"], "true")
        self.assertNotIn("quantity", sent[0])

    def test_missing_executed_qty_fallback_warns_when_nothing_visible(self):
        fx = FakeExchange([])
        res, ws = self._execute(fx, executed_qty=None, position_visible=False)
        self.assertTrue(res["success"])
        self.assertFalse(res["partial_fill_protected"])
        self.assertIn("WARNING: the partial position is not protected yet", res["message"])
        self.assertEqual(posts(fx, ALGO_ENDPOINT), [])
        self.assertIn("testnet:SOLUSDT:7", read_registry(ws), "the guardian loop protects it on its next cycle")

    def test_missing_executed_qty_fallback_uses_position_and_reports_tps(self):
        fx = FakeExchange([])
        res, ws = self._execute(fx, executed_qty=None, filled_at_once=True)  # fully filled before the first check
        self.assertTrue(res["partial_fill_protected"])
        self.assertIn("TPs were placed", res["message"])
        self.assertNotIn("TPs are placed once", res["message"])
        self.assertEqual(read_registry(ws), {})

    def test_partial_fill_unverified_sl_auto_destructs(self):
        fx = FakeExchange([], reject_new_stops=True)
        res, ws = self._execute(fx)
        self.assertFalse(res["success"])
        self.assertTrue(res["emergency_abort"])
        closes = [c[2] for c in fx.calls if c[0] == "POST" and c[1] == ORDER_ENDPOINT and c[2].get("type") == "MARKET"]
        self.assertTrue(closes)
        self.assertTrue(all(c["reduceOnly"] == "true" and c["quantity"] == "0.1" for c in closes))
        self.assertEqual(fx.open_orders, [], "the resting remainder was cancelled")
        self.assertEqual(read_registry(ws), {})


class TestRestingEntryProdGates(ExecutorHarness):

    def _prod_stop(self, **kw):
        return self.execute(env="prod", order_type="STOP_MARKET", trigger_price=102.347, **kw)

    def assertGateRejected(self, res, fragment):
        self.assertFalse(res["success"])
        self.assertTrue(res.get("hard_gate_rejection"))
        self.assertIn("CONDITIONAL ENTRY REJECTED", res["error"])
        self.assertIn(fragment, res["error"])
        self.assertEqual(self.writes(), [], "gates run before any write (margin, leverage or order)")
        self.assertIsNone(read_registry(self.ws))

    def test_missing_guardian_state_rejected(self):
        res = self._prod_stop()
        self.assertGateRejected(res, "guardian is not alive")
        self.assertIn("position_guardian_loop.py --interval 60", res["error"])

    def test_stale_guardian_state_rejected(self):
        write_guardian_state(self.ws, age=2 * 60 + 30 + 5)  # limit for a 60s loop: 2 * interval + 30s
        self.assertGateRejected(self._prod_stop(), "stale")

    def test_guardian_within_loop_age_accepted(self):
        write_guardian_state(self.ws, age=2 * 60 + 30 - 5)
        self.assertTrue(self._prod_stop()["success"])

    def test_single_once_run_does_not_count_as_alive(self):
        write_guardian_state(self.ws, mode="once", interval_seconds=None, age=1)
        res = self._prod_stop()
        self.assertGateRejected(res, "--once")
        self.assertIn("python3 scripts/loops/position_guardian_loop.py --interval 60", res["error"])

    def test_slow_guardian_loop_rejected(self):
        self.assertEqual(eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING, 120)
        write_guardian_state(self.ws, interval_seconds=300, age=1)
        self.assertGateRejected(self._prod_stop(), "exceeds 120s")
        write_guardian_state(self.ws, interval_seconds=120, age=1)
        self.assertTrue(self._prod_stop()["success"])

    def test_dry_run_guardian_rejected(self):
        write_guardian_state(self.ws, dry_run=True)
        self.assertGateRejected(self._prod_stop(), "dry-run")

    def test_wrong_env_guardian_rejected(self):
        write_guardian_state(self.ws, env="testnet")
        self.assertGateRejected(self._prod_stop(), "not 'prod'")

    def test_existing_position_rejected(self):
        write_guardian_state(self.ws)
        self.positions = [{"symbol": "SOLUSDT", "positionAmt": "-1.5", "markPrice": "100", "unRealizedProfit": "0"}]
        self.assertGateRejected(self._prod_stop(), "already has an open position")

    def test_position_query_failure_rejected(self):
        write_guardian_state(self.ws)
        self.positions_error = "symbol"
        self.assertGateRejected(self._prod_stop(), "cannot verify open positions")
        # Issue #101: a failing all-symbol positionRisk (live snapshot) rejects every entry before any write
        self.positions_error = True
        self.calls = []
        res = self._prod_stop()
        self.assertFalse(res["success"])
        self.assertTrue(res.get("hard_gate_rejection"))
        self.assertIn("cannot read the live exchange state for the PROD gates", res["error"])
        self.assertEqual(self.writes(), [])

    def test_existing_pending_entry_rejects_every_entry_type(self):
        write_guardian_state(self.ws)
        write_registry(self.ws, make_record(symbol="SOLUSDT", env="prod", entry_id="555"))
        for kw in (dict(order_type="STOP_MARKET", trigger_price=102.347),   # resting conditional
                   dict(order_type="STOP_MARKET", trigger_price=99.5),      # breached trigger -> MARKET fallthrough
                   dict(order_type="LIMIT", limit_price=98.767),
                   dict(order_type="MARKET")):
            self.calls = []
            res = self.execute(env="prod", **kw)
            self.assertFalse(res["success"], kw)
            self.assertTrue(res.get("hard_gate_rejection"), kw)
            self.assertIn("ENTRY REJECTED: SOLUSDT has a pending resting entry (prod:SOLUSDT:555)", res["error"], kw)
            self.assertEqual(self.writes(), [], kw)
        self.assertEqual(list(read_registry(self.ws)), ["prod:SOLUSDT:555"])

    def test_unreadable_registry_rejects_market_entry(self):
        os.makedirs(os.path.join(self.ws, "logs"), exist_ok=True)
        with open(os.path.join(self.ws, "logs", "pending_entries.json"), "w") as f:
            f.write("{broken")
        res = self.execute(env="prod", order_type="MARKET")
        self.assertFalse(res["success"])
        self.assertIn("ENTRY REJECTED: FAIL-CLOSED", res["error"])
        self.assertEqual(self.writes(), [])

    def test_pending_entry_of_other_symbol_or_env_does_not_block_market(self):
        write_registry(self.ws, make_record(symbol="BTCUSDT", env="prod"), make_record(symbol="SOLUSDT", env="testnet"))
        self.assertTrue(self.execute(env="prod", order_type="MARKET")["success"])

    def test_resting_limit_gated_too(self):
        res = self.execute(env="prod", order_type="LIMIT", limit_price=98.767)
        self.assertGateRejected(res, "guardian is not alive")

    def test_fresh_guardian_accepted(self):
        write_guardian_state(self.ws)
        write_registry(self.ws, make_record(symbol="SOLUSDT", env="testnet", entry_id="555"))  # other env: ignored
        res = self._prod_stop()
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res["conditional_entry"])
        self.assertIn("prod:SOLUSDT:8", read_registry(self.ws))

    def test_market_entry_not_affected(self):
        res = self.execute(env="prod", order_type="MARKET")  # no guardian state, no registry
        self.assertTrue(res["success"], res.get("error"))
        # only the all-symbol live snapshot of the PROD gates (issue #101), never the per-symbol resting-entry check
        self.assertEqual([c[2] for c in self.calls if c[1] == "/fapi/v2/positionRisk"], [{}])


def entry_limit(order_id=77, symbol="ETHUSDT", side="BUY", price=98.0, reduce_only=False):
    """A resting regular order as listed by GET /fapi/v1/openOrders (reduce_only=True: a desk TP)."""
    return {"orderId": order_id, "symbol": symbol, "side": side, "type": "LIMIT", "price": str(price),
            "origQty": "1.5", "reduceOnly": reduce_only, "closePosition": False, "status": "NEW"}


ENTRY_KINDS = (dict(order_type="STOP_MARKET", trigger_price=102.347),   # resting conditional
               dict(order_type="STOP_MARKET", trigger_price=99.5),      # breached trigger -> MARKET fallthrough
               dict(order_type="LIMIT", limit_price=98.767),
               dict(order_type="MARKET"))


class TestUnregisteredRestingEntriesGate(ExecutorHarness):
    """Issue #46: in PROD a missing / incomplete logs/pending_entries.json no longer reads as 'no resting entries':
    every opening order resting on the exchange must have its registry record, else every new entry is rejected
    before any write."""

    def setUp(self):
        super().setUp()
        write_guardian_state(self.ws)  # resting entries would otherwise be rejected by the guardian gate first

    def assertUnregisteredRejected(self, fragments, **kw):
        for entry in ENTRY_KINDS:
            self.calls = []
            res = self.execute(env="prod", **dict(entry, **kw))
            self.assertFalse(res["success"], entry)
            self.assertTrue(res.get("hard_gate_rejection"), entry)
            self.assertIn("not in logs/pending_entries.json", res["error"], entry)
            self.assertIn("Cancel them (or restore their registry records)", res["error"], entry)
            for fragment in fragments:
                self.assertIn(fragment, res["error"], entry)
            self.assertEqual(self.writes(), [], f"no margin/leverage/order write on rejection: {entry}")
            all_symbol_gets = [c[1] for c in self.calls if c[0] == "GET" and c[2] == {}]
            # Issue #160: order listings before positionRisk.
            self.assertEqual(all_symbol_gets, ["/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders", "/fapi/v2/positionRisk"],
                             "bounded: the three GETs of the live snapshot (issue #101), fetched once")

    def test_deleted_registry_with_resting_conditional_entry_rejects_every_entry(self):
        self.open_algos = [entry_algo(algo_id=7001, symbol="BTCUSDT")]
        self.assertUnregisteredRejected(["1 resting entry order(s)", "BTCUSDT STOP_MARKET algo 7001 (STOP_MARKET BUY @ 101.0)"])
        self.assertIsNone(read_registry(self.ws))

    def test_deleted_registry_with_resting_limit_entry_rejects_every_entry(self):
        self.open_orders = [entry_limit()]
        self.assertUnregisteredRejected(["ETHUSDT LIMIT order 77 (LIMIT BUY @ 98.0)"])
        self.assertIsNone(read_registry(self.ws))

    def test_empty_registry_and_unregistered_entry_of_traded_symbol_rejected(self):
        write_registry(self.ws)  # registry present but empty
        self.open_algos = [entry_algo(algo_id=31, symbol="SOLUSDT")]
        self.open_orders = [entry_limit(order_id=32, symbol="SOLUSDT")]
        self.assertUnregisteredRejected(["2 resting entry order(s)", "SOLUSDT STOP_MARKET algo 31", "SOLUSDT LIMIT order 32"])
        self.assertEqual(read_registry(self.ws), {})

    def test_registry_matching_exchange_entries_accepted(self):
        self.open_algos = [entry_algo(algo_id=7001, symbol="BTCUSDT")]
        self.open_orders = [entry_limit(order_id=77, symbol="ETHUSDT")]
        for entry in ENTRY_KINDS:
            # fresh registry each time: a resting SOLUSDT entry placed by the previous run would block the symbol
            write_registry(self.ws, make_record(kind="STOP_MARKET", entry_id="7001", symbol="BTCUSDT", env="prod"),
                           make_record(kind="LIMIT", entry_id="77", symbol="ETHUSDT", env="prod"))
            res = self.execute(env="prod", **entry)
            self.assertTrue(res["success"], (entry, res.get("error")))

    def test_stop_losses_and_take_profits_are_never_entries(self):
        self.open_algos = [stop(501, 95.0, symbol="BTCUSDT"),                                   # closePosition SL
                           dict(stop(502, 96.0, symbol="ETHUSDT", close_position=False), reduceOnly=True, quantity="2"),
                           dict(stop(503, 120.0, symbol="ETHUSDT", close_position=False), orderType="TAKE_PROFIT_MARKET",
                                reduceOnly="true"),
                           dict(stop(504, 94.0, symbol="XRPUSDT"), closePosition="true")]
        self.open_orders = [entry_limit(order_id=601, side="SELL", price=110.0, reduce_only=True),   # desk TP
                            dict(entry_limit(order_id=602, side="SELL", price=120.0), reduceOnly="true")]
        for entry in ENTRY_KINDS:
            registry = os.path.join(self.ws, "logs", "pending_entries.json")
            if os.path.exists(registry):  # missing registry (a resting entry placed by the previous run blocks SOLUSDT)
                os.remove(registry)
            res = self.execute(env="prod", **entry)
            self.assertTrue(res["success"], (entry, res.get("error")))

    def test_record_of_other_env_or_kind_does_not_cover_an_entry(self):
        self.open_algos = [entry_algo(algo_id=7001, symbol="BTCUSDT")]
        for record in (make_record(kind="STOP_MARKET", entry_id="7001", symbol="BTCUSDT", env="testnet"),
                       make_record(kind="LIMIT", entry_id="7001", symbol="BTCUSDT", env="prod"),
                       make_record(kind="STOP_MARKET", entry_id="7001", symbol="ETHUSDT", env="prod")):
            write_registry(self.ws, record)
            self.calls = []
            res = self.execute(env="prod", order_type="MARKET")
            self.assertFalse(res["success"], record)
            self.assertIn("BTCUSDT STOP_MARKET algo 7001", res["error"], record)
            self.assertEqual(self.writes(), [], record)

    def test_open_orders_query_failure_rejected(self):
        for endpoint in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
            self.open_errors = {endpoint: {"code": -1001, "msg": "Internal error"}}
            self.calls = []
            res = self.execute(env="prod", order_type="MARKET")
            self.assertFalse(res["success"], endpoint)
            self.assertTrue(res.get("hard_gate_rejection"), endpoint)
            # the shared live snapshot (issue #101) fails first, before any write
            self.assertIn("FAIL-CLOSED — cannot read the live exchange state for the PROD gates", res["error"])
            self.assertIn(f"{endpoint} query failed", res["error"])
            self.assertEqual(self.writes(), [], endpoint)

    def test_open_orders_query_exception_rejected(self):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "GET" and endpoint == "/fapi/v1/openOrders":
                self.calls.append((method, endpoint, dict(params or {})))
                raise OSError("network unreachable")
            return self.fake(method, endpoint, params, target_env)
        res = self.execute(env="prod", order_type="MARKET", send=send)
        self.assertFalse(res["success"])
        self.assertIn("network unreachable", res["error"])
        self.assertEqual(self.writes(), [])

    def test_testnet_unchanged(self):
        self.open_algos = [entry_algo(algo_id=7001, symbol="BTCUSDT")]
        self.open_orders = [entry_limit()]
        self.open_errors = {}
        for entry in ENTRY_KINDS:
            self.calls = []
            res = self.execute(env="testnet", **entry)
            self.assertTrue(res["success"], (entry, res.get("error")))
            self.assertEqual([c for c in self.calls if c[0] == "GET" and c[2] == {}], [], "no all-symbol cross-check")


class TestFindUnregisteredRestingEntries(unittest.TestCase):

    def find(self, send, *records, env="prod"):
        ws = tempfile.mkdtemp()
        if records:
            write_registry(ws, *records)
        with patch("execute_futures_trade.send_signed_request", side_effect=send), \
             patch("execute_futures_trade._workspace_dir", return_value=ws):
            return eft.find_unregistered_resting_entries(env)

    def test_reports_unknown_entries_with_cancel_kind(self):
        fake = FakeExchange([], algos=[entry_algo(algo_id=7001), stop(501, 95.0)],
                            open_orders=[entry_limit(order_id=77), entry_limit(order_id=78, reduce_only=True)])
        unknown, err = self.find(fake, make_record(kind="LIMIT", entry_id="99", symbol="ETHUSDT", env="prod"))
        self.assertIsNone(err)
        self.assertEqual(unknown, [
            {"symbol": "BTCUSDT", "source": "algo", "kind": "STOP_MARKET", "id": 7001, "type": "STOP_MARKET",
             "side": "BUY", "price": 101.0, "quantity": "12"},
            {"symbol": "ETHUSDT", "source": "order", "kind": "LIMIT", "id": 77, "type": "LIMIT",
             "side": "BUY", "price": 98.0, "quantity": "1.5"},
        ])
        self.assertEqual(fake.calls, [("GET", "/fapi/v1/openAlgoOrders", {}), ("GET", "/fapi/v1/openOrders", {})])

    def test_missing_registry_and_no_entries_is_ok(self):
        fake = FakeExchange([], algos=[stop(501, 95.0)])
        self.assertEqual(self.find(fake), ([], None))

    def test_unreadable_registry_is_an_error(self):
        ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(ws, "logs"))
        with open(os.path.join(ws, "logs", "pending_entries.json"), "w") as f:
            f.write("{broken")
        with patch("execute_futures_trade.send_signed_request", side_effect=FakeExchange([])), \
             patch("execute_futures_trade._workspace_dir", return_value=ws):
            unknown, err = eft.find_unregistered_resting_entries("prod")
        self.assertEqual(unknown, [])
        self.assertIn("unreadable", err)

    @patch("execute_futures_trade.call_binance_mcp")
    def test_mcp_gateway_listing_all_symbols(self, mock_mcp):
        def mcp(tool, args=None, session_id=None):
            if tool == "futures_usds.currentAllAlgoOpenOrders":
                return [
                    {"algoId": 4242, "symbol": "BTCUSDT", "side": "BUY", "orderType": "STOP_MARKET", "triggerPrice": "101",
                     "quantity": "1", "closePosition": "false", "reduceOnly": "false", "algoStatus": "NEW"},
                    {"algoId": 5, "symbol": "BTCUSDT", "side": "SELL", "orderType": "STOP_MARKET", "triggerPrice": "95",
                     "quantity": "1", "closePosition": "false", "reduceOnly": "true"},             # MCP qty-based SL
                    {"algoId": 6, "symbol": "ETHUSDT", "side": "SELL", "orderType": "STOP_MARKET", "triggerPrice": "90",
                     "closePosition": "true"},
                ]
            if tool == "futures_usds.currentAllOpenOrders":
                return [{"orderId": 11, "symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "price": "110",
                         "origQty": "0.3", "reduceOnly": "true", "closePosition": False},          # TP
                        {"orderId": 12, "symbol": "SOLUSDT", "side": "BUY", "type": "LIMIT", "price": "98",
                         "origQty": "2", "reduceOnly": False, "closePosition": False}]             # LIMIT entry
            return {"error": f"unexpected {tool}", "isError": True}
        mock_mcp.side_effect = mcp

        def route(method, endpoint, params=None, target_env=None, retry_count=0):
            return eft.send_mcp_gateway_request(method, endpoint, params=params)  # MCP_OAUTH_ACTIVE path

        unknown, err = self.find(route)
        self.assertIsNone(err)
        self.assertEqual([(u["symbol"], u["kind"], u["id"]) for u in unknown],
                         [("BTCUSDT", "STOP_MARKET", 4242), ("SOLUSDT", "LIMIT", 12)])
        self.assertEqual([c[0] for c in mock_mcp.call_args_list],
                         [("futures_usds.currentAllAlgoOpenOrders", {}), ("futures_usds.currentAllOpenOrders", {})])
        unknown, err = self.find(route, make_record(kind="STOP_MARKET", entry_id="4242", symbol="BTCUSDT", env="prod"),
                                 make_record(kind="LIMIT", entry_id="12", symbol="SOLUSDT", env="prod"))
        self.assertEqual((unknown, err), ([], None))

    @patch("execute_futures_trade.call_binance_mcp")
    def test_mcp_gateway_error_fails_closed(self, mock_mcp):
        mock_mcp.return_value = {"error": "gateway down", "isError": True}
        unknown, err = self.find(lambda m, e, params=None, target_env=None, retry_count=0:
                                 eft.send_mcp_gateway_request(m, e, params=params))
        self.assertEqual(unknown, [])
        self.assertIn("/fapi/v1/openAlgoOrders query failed", err)


# ---------------------------------------------------------------------------------------------
# protect_pending_entries
# ---------------------------------------------------------------------------------------------
class TestProtectPendingEntries(unittest.TestCase):

    def run_protect(self, fake, *records, dry_run=False, env="testnet", mcp=False):
        ws = tempfile.mkdtemp()
        if records:
            write_registry(ws, *records)
        with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=mcp):
            res = eft.protect_pending_entries(target_env=env, dry_run=dry_run)
        return res, ws

    def types(self, res):
        return [a["type"] for a in res["actions"]]

    def test_filled_entry_gets_planned_sl_and_tps_from_actual_size(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="101.5")])  # entry 7001 no longer open
        res, ws = self.run_protect(fake, make_record(total_qty=12.0))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.types(res), ["pending_protect_sl", "pending_tp_placed"])
        sl = posts(fake, ALGO_ENDPOINT)
        self.assertEqual(len(sl), 1)
        self.assertEqual((sl[0]["side"], sl[0]["triggerPrice"], sl[0]["closePosition"]), ("SELL", 95.0, "true"))
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [95.0])
        tps = posts(fake, ORDER_ENDPOINT)
        self.assertEqual([(t["type"], t["side"], t["reduceOnly"], t["timeInForce"]) for t in tps], [("LIMIT", "SELL", "true", "GTC")] * 2)
        self.assertEqual([(t["price"], t["quantity"]) for t in tps], [(110.0, 3.0), (120.0, 7.0)])  # 30/70 of 10, not 12
        self.assertLess(fake.write_index("POST", ALGO_ENDPOINT)[0], fake.write_index("POST", ORDER_ENDPOINT)[0])
        audit = read_jsonl(ws, "trades_audit.jsonl")
        self.assertEqual(len(audit), 1)
        self.assertNotIn("event", audit[0])
        self.assertEqual((audit[0]["total_qty"], audit[0]["tp1_qty"], audit[0]["tp2_qty"]), (10.0, 3.0, 7.0))
        self.assertEqual((audit[0]["entry_order_id"], audit[0]["pending_entry_key"]), ("7001", "testnet:BTCUSDT:7001"))
        self.assertEqual((audit[0]["sl_price"], audit[0]["is_yolo"], audit[0]["target_env"]), (95.0, False, "testnet"))
        self.assertIsNotNone(audit[0]["sl_algo_id"])
        self.assertIn("provenance_stamp", audit[0])
        self.assertEqual(eft.latest_trade_audit_record("BTCUSDT", ws)["total_qty"], 10.0)
        self.assertEqual(read_registry(ws), {})

    def test_looser_orphan_heal_stop_replaced_place_then_cancel(self):
        # Planned SL 99.4 (tight); the 2.5% orphan heal at 98.4 is LOOSER for a LONG -> replaced
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(601, 98.4)])
        res, ws = self.run_protect(fake, make_record(sl=99.4))
        self.assertTrue(res["ok"], res["errors"])
        p, d = fake.write_index("POST", ALGO_ENDPOINT), fake.write_index("DELETE", ALGO_ENDPOINT)
        self.assertEqual(len(p), 1)
        self.assertEqual(len(d), 1)
        self.assertLess(p[0], d[0], "the planned SL is placed and verified before the heal stop is cancelled")
        self.assertEqual(fake.calls[d[0]][2]["algoId"], 601)
        sent = fake.calls[p[0]][2]
        self.assertEqual((sent["triggerPrice"], sent["reduceOnly"], sent["quantity"]), (99.4, "true", "10"))
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [99.4])
        self.assertEqual(res["actions"][0]["detail"]["mode"], "replace")
        self.assertEqual(read_audit_sl(ws), 99.4)
        self.assertEqual(read_registry(ws), {})

    def test_tighter_trailed_stop_never_moved_back(self):
        # The guardian trailed (or --move-breakeven moved) the stop 95 -> 99.5: the planned 95 is never restored
        for trailed in (99.5, 101.2):  # tighter, and beyond entry (break-even)
            fake = FakeExchange([long_position(amt="10", entry="101.0", mark="103.0")], algos=[stop(701, trailed)])
            res, ws = self.run_protect(fake, make_record(sl=95.0, sl_qty=10.0))
            self.assertTrue(res["ok"], res["errors"])
            self.assertEqual(posts(fake, ALGO_ENDPOINT), [], f"no stop placement at all ({trailed})")
            self.assertEqual(fake.write_index("DELETE", ALGO_ENDPOINT), [])
            self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [trailed])
            self.assertEqual(self.types(res), ["pending_tp_placed"])
            self.assertEqual(read_audit_sl(ws), trailed)

    def test_resize_keeps_the_tighter_existing_price(self):
        limit = {"orderId": 8001, "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": "101.0",
                 "origQty": "12", "reduceOnly": False}
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(701, 99.5, close_position=False)],
                            open_orders=[limit])
        fake.algos[0]["reduceOnly"] = True
        res, ws = self.run_protect(fake, make_record(kind="LIMIT", entry_id="8001", sl=95.0, sl_qty=4.0))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["actions"][0]["detail"]["mode"], "resize")
        sent = posts(fake, ALGO_ENDPOINT)
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["triggerPrice"], sent[0]["quantity"], sent[0]["reduceOnly"]), (99.5, "10", "true"))
        self.assertLess(fake.write_index("POST", ALGO_ENDPOINT)[0], fake.write_index("DELETE", ALGO_ENDPOINT)[0])
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [99.5])
        self.assertEqual(read_registry(ws)["testnet:BTCUSDT:8001"]["sl_qty"], 10.0)

    def test_tighter_heal_stop_of_unknown_size_resized_to_full_position(self):
        # MCP orphan heal: reduce-only stop sized for the 4-unit partial fill, TIGHTER (98.4) than the plan (95)
        limit = {"orderId": 8001, "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": "101.0",
                 "origQty": "10", "reduceOnly": False}
        heal = dict(stop(601, 98.4, close_position=False), reduceOnly=True, quantity="4")
        fake = FakeExchange([long_position(amt="4", entry="101.0")], algos=[heal], open_orders=[limit])
        ws = tempfile.mkdtemp()
        # no sl_qty: coverage unknown (total_qty matches the resting LIMIT's origQty, issue #101 cross-check).
        # MCP: its listing carries no quantity (on KEYS the listed quantity 4 covers the 4-unit fill, issue #118).
        write_registry(ws, make_record(kind="LIMIT", entry_id="8001", sl=95.0, total_qty=10.0))
        with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=True):
            res1 = eft.protect_pending_entries(target_env="testnet")
            self.assertTrue(res1["ok"], res1["errors"])
            self.assertEqual(res1["actions"][0]["detail"]["mode"], "resize")
            self.assertTrue(res1["actions"][0]["detail"]["coverage_unknown"])
            self.assertEqual(read_registry(ws)["testnet:BTCUSDT:8001"]["sl_qty"], 4.0)
            # The LIMIT fills completely: the stop must cover all 10 units before TPs and the drop
            fake.positions = [long_position(amt="10", entry="101.0")]
            fake.open_orders = []
            res2 = eft.protect_pending_entries(target_env="testnet")
        self.assertTrue(res2["ok"], res2["errors"])
        self.assertEqual(self.types(res2), ["pending_protect_sl", "pending_tp_placed"])
        sent = posts(fake, ALGO_ENDPOINT)
        self.assertEqual([(s["triggerPrice"], s["quantity"]) for s in sent], [(98.4, "4"), (98.4, "10")],
                         "always at the tighter heal price, never moved to the looser planned 95")
        self.assertEqual([(float(a["triggerPrice"]), a["quantity"]) for a in fake.algos], [(98.4, "10")])
        self.assertEqual([t["quantity"] for t in posts(fake, ORDER_ENDPOINT)], [3.0, 7.0])
        self.assertEqual(read_audit_sl(ws), 98.4)
        self.assertEqual(read_registry(ws), {})

    def test_tighter_stop_of_unknown_size_resized_once_when_already_filled(self):
        heal = dict(stop(601, 98.4, close_position=False), reduceOnly=True, quantity="4")
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[heal])
        res, ws = self.run_protect(fake, make_record(sl=95.0))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual([(s["triggerPrice"], s["quantity"]) for s in posts(fake, ALGO_ENDPOINT)], [(98.4, "10")])
        self.assertLess(fake.write_index("POST", ALGO_ENDPOINT)[0], fake.write_index("DELETE", ALGO_ENDPOINT)[0])
        self.assertEqual(read_registry(ws), {})

    def _crossed_fake(self, mark, close_ok=True, **kw):
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark=mark)], algos=[stop(601, 98.4)], **kw)

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ORDER_ENDPOINT and (params or {}).get("type") == "MARKET":
                fake.calls.append((method, endpoint, dict(params)))
                if not close_ok:
                    return {"code": -1001, "msg": "Internal error"}
                fake.positions = []  # flat
                return {"orderId": 55, "status": "FILLED"}
            return fake(method, endpoint, params, target_env)
        return fake, send

    def _run_crossed(self, send, rec):
        ws = tempfile.mkdtemp()
        write_registry(ws, rec)
        with offline(send, workspace=ws):
            return eft.protect_pending_entries(target_env="testnet"), ws

    def test_planned_sl_already_crossed_closes_before_cancelling_stops(self):
        fake, send = self._crossed_fake(mark="99.0")  # LONG plan 99.4, mark 99.0: crossed; heal 98.4 still active
        res, ws = self._run_crossed(send, make_record(sl=99.4))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.types(res), ["pending_sl_crossed_close"])
        writes = fake.writes()
        self.assertEqual((writes[0][1], writes[0][2]["type"], writes[0][2]["reduceOnly"], writes[0][2]["quantity"]),
                         (ORDER_ENDPOINT, "MARKET", "true", "10"), "the close comes first, stops still in place")
        self.assertEqual(posts(fake, ALGO_ENDPOINT), [], "no stop placement at a crossed price")
        self.assertEqual(fake.algos, [], "leftover stop cancelled only once flat")
        self.assertEqual(read_registry(ws), {})

    def test_minus_2021_on_replace_closes_at_market(self):
        fake, send = self._crossed_fake(mark="101.5", reject_new_stops=True)  # mark not beyond, but Binance says -2021
        res, ws = self._run_crossed(send, make_record(sl=99.4))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.types(res), ["pending_protect_sl", "pending_sl_crossed_close"])
        self.assertTrue(res["actions"][1]["success"])
        self.assertEqual(read_registry(ws), {})

    def test_crossed_close_not_confirmed_keeps_stops_and_record(self):
        fake, send = self._crossed_fake(mark="99.0", close_ok=False)
        res, ws = self._run_crossed(send, make_record(sl=99.4))
        self.assertFalse(res["ok"])
        self.assertEqual(res["errors"][0]["stage"], "sl_crossed_close")
        self.assertFalse(res["actions"][0]["success"])
        self.assertEqual([c for c in fake.writes() if c[0] == "DELETE"], [], "existing stops are never cancelled")
        self.assertIn(601, [a["algoId"] for a in fake.algos])
        self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_crossed_dry_run_sends_nothing(self):
        fake, send = self._crossed_fake(mark="99.0")
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(sl=99.4))
        with offline(send, workspace=ws):
            res = eft.protect_pending_entries(target_env="testnet", dry_run=True)
        self.assertEqual(self.types(res), ["pending_sl_crossed_close"])
        self.assertEqual(fake.writes(), [])

    def test_unverified_replace_keeps_existing_stop_no_auto_destruct(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(601, 98.4)], index_new_stops=False)
        with patch("execute_futures_trade.emergency_abort_market_close") as mock_abort:
            res, ws = self.run_protect(fake, make_record(sl=99.4))
        mock_abort.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertEqual(res["errors"][0]["stage"], "protect_sl")
        self.assertEqual(self.types(res), ["pending_protect_sl"])
        self.assertEqual(fake.write_index("DELETE", ALGO_ENDPOINT), [], "the existing stop is never cancelled")
        self.assertIn(601, [a["algoId"] for a in fake.algos])
        self.assertEqual(posts(fake, ORDER_ENDPOINT), [], "no market close, no TPs")
        self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_unverified_resize_keeps_existing_stop_no_auto_destruct(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(701, 95.0)], index_new_stops=False)
        with patch("execute_futures_trade.emergency_abort_market_close") as mock_abort:
            # MCP: a closePosition stop on KEYS covers any size and is never resized (issue #39)
            res, ws = self.run_protect(fake, make_record(sl=95.0, sl_qty=4.0), mcp=True)
        mock_abort.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertEqual(res["actions"][0]["detail"]["mode"], "resize")
        self.assertIn(701, [a["algoId"] for a in fake.algos])
        self.assertEqual(read_registry(ws)["testnet:BTCUSDT:7001"]["sl_qty"], 4.0)

    def test_unverified_sl_triggers_auto_destruct_with_actual_size(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0")], reject_new_stops=True, reject_response=INTERNAL_ERROR)
        res, ws = self.run_protect(fake, make_record(total_qty=12.0))
        self.assertEqual(self.types(res), ["pending_protect_sl", "pending_abort"])
        self.assertFalse(res["actions"][0]["success"])
        self.assertTrue(res["actions"][1]["success"])
        closes = posts(fake, ORDER_ENDPOINT)
        self.assertTrue(closes)
        for c in closes:
            self.assertEqual((c["type"], c["side"], c["reduceOnly"], c["quantity"]), ("MARKET", "SELL", "true", "10"))
        self.assertEqual(read_jsonl(ws, "emergency_aborts.jsonl")[0]["event"], "CRITICAL_FAILSAFE_ABORT")
        self.assertEqual(read_registry(ws), {})

    def test_partial_limit_fill_keeps_record_then_resizes_then_places_tps(self):
        limit = {"orderId": 8001, "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": "101.0",
                 "origQty": "12", "reduceOnly": False}
        fake = FakeExchange([long_position(amt="4", entry="101.0")], open_orders=[limit])
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(kind="LIMIT", entry_id="8001"))
        # MCP: the gateway sizes the closePosition stop reduce-only for the partial fill, so growth is resized (on
        # KEYS the closePosition stop covers any size: TestKeysCoveringStopNotResized, issue #39)
        with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=True):
            res1 = eft.protect_pending_entries(target_env="testnet")
            rec = read_registry(ws)["testnet:BTCUSDT:8001"]
            self.assertTrue(res1["ok"], res1["errors"])
            self.assertEqual(self.types(res1), ["pending_protect_sl"])
            self.assertEqual(posts(fake, ORDER_ENDPOINT), [], "no TPs while the LIMIT entry is still open")
            self.assertEqual(rec["sl_qty"], 4.0)
            # The fill grows: the planned SL is resized place-then-cancel
            fake.positions = [long_position(amt="10", entry="101.0")]
            res2 = eft.protect_pending_entries(target_env="testnet")
            self.assertEqual(res2["actions"][0]["detail"]["mode"], "resize")
            self.assertEqual(posts(fake, ALGO_ENDPOINT)[-1]["quantity"], "10")
            self.assertEqual(len(fake.algos), 1)
            self.assertIn("testnet:BTCUSDT:8001", read_registry(ws))
            # Fully filled: entry gone -> TPs from the actual size, record dropped
            fake.open_orders = []
            res3 = eft.protect_pending_entries(target_env="testnet")
        self.assertEqual(self.types(res3), ["pending_tp_placed"])
        self.assertEqual([t["quantity"] for t in posts(fake, ORDER_ENDPOINT)], [3.0, 7.0])
        self.assertEqual(read_registry(ws), {})

    def test_untriggered_expired_entry_cancelled_and_dropped(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws = self.run_protect(fake, make_record(expires_in=-1))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.types(res), ["pending_timeout_cancel"])
        self.assertEqual([c[2] for c in fake.writes()], [{"symbol": "BTCUSDT", "algoId": 7001}])
        self.assertEqual(fake.algos, [])
        self.assertEqual(read_registry(ws), {})

    def test_untriggered_entry_not_expired_kept_without_writes(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws = self.run_protect(fake, make_record())
        self.assertTrue(res["ok"])
        self.assertEqual(res["actions"], [])
        self.assertEqual(fake.writes(), [])
        self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_entry_gone_without_position_dropped_only_after_grace(self):
        self.assertEqual(eft.PENDING_MISSING_GRACE_SECONDS, 60)
        fake = FakeExchange([])
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record())
        with offline(fake, workspace=ws):
            res1 = eft.protect_pending_entries(target_env="testnet")
            rec = read_registry(ws)["testnet:BTCUSDT:7001"]
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["actions"], [], "first sight: positionRisk may lag, never dropped")
            self.assertIsNotNone(rec["missing_since_ts"])
            res2 = eft.protect_pending_entries(target_env="testnet")  # < 60s later: still kept
            self.assertEqual(res2["actions"], [])
            self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))
        res3, ws3 = self.run_protect(fake, make_record(missing_since_ts=int(time.time()) - 61))
        self.assertEqual(self.types(res3), ["pending_dropped"])
        self.assertEqual(read_registry(ws3), {})
        self.assertEqual(fake.writes(), [])

    def test_missing_mark_cleared_when_entry_or_position_reappears(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws = self.run_protect(fake, make_record(missing_since_ts=int(time.time()) - 600))
        self.assertEqual(res["actions"], [])
        self.assertNotIn("missing_since_ts", read_registry(ws)["testnet:BTCUSDT:7001"])
        fake = FakeExchange([long_position(amt="10", entry="101.0")])  # triggered: position visible again
        res, ws = self.run_protect(fake, make_record(missing_since_ts=int(time.time()) - 600))
        self.assertEqual(self.types(res), ["pending_protect_sl", "pending_tp_placed"])
        self.assertEqual(read_registry(ws), {})

    def test_failed_entry_cancel_before_abort_keeps_record(self):
        limit = {"orderId": 8001, "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": "101.0",
                 "origQty": "12", "reduceOnly": False}
        fake = FakeExchange([long_position(amt="4", entry="101.0")], open_orders=[limit], reject_new_stops=True,
                            reject_response=INTERNAL_ERROR)

        def no_cancel(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "DELETE" and endpoint == ORDER_ENDPOINT:
                return {"code": -2011, "msg": "Unknown order sent."}
            return fake(method, endpoint, params, target_env)
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(kind="LIMIT", entry_id="8001"))
        with offline(no_cancel, workspace=ws):
            res = eft.protect_pending_entries(target_env="testnet")
        self.assertEqual(self.types(res), ["pending_protect_sl", "pending_abort"])
        self.assertTrue(res["actions"][1]["success"], "the auto-destruct itself succeeded")
        self.assertFalse(res["ok"])
        self.assertEqual(res["errors"][0]["stage"], "abort_entry_cancel")
        self.assertIn("testnet:BTCUSDT:8001", read_registry(ws))

    def test_tp_failure_keeps_record_and_retries_only_missing_tp(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(701, 95.0)])
        state = {"fail_tp2": True}

        def flaky(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ORDER_ENDPOINT and (params or {}).get("price") == 120.0 and state["fail_tp2"]:
                fake.calls.append((method, endpoint, dict(params)))
                return {"code": -1001, "msg": "Internal error"}
            return fake(method, endpoint, params, target_env)
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(sl_qty=10.0))
        with offline(flaky, workspace=ws):
            res1 = eft.protect_pending_entries(target_env="testnet")
            rec = read_registry(ws)["testnet:BTCUSDT:7001"]
            self.assertFalse(res1["ok"])
            self.assertEqual(res1["errors"][0]["stage"], "take_profit")
            self.assertEqual((rec["tp1_order_id"], rec.get("tp2_order_id"), rec["tp_placed"]), (1, None, False))
            self.assertEqual((rec["tp1_qty"], rec["tp2_qty"]), (3.0, 7.0))
            self.assertEqual(read_jsonl(ws, "trades_audit.jsonl"), [], "no audit before both TPs exist")
            state["fail_tp2"] = False
            fake.positions = [long_position(amt="9", entry="101.0")]  # size changed meanwhile: split is kept
            res2 = eft.protect_pending_entries(target_env="testnet")
        self.assertTrue(res2["ok"], res2["errors"])
        tps = posts(fake, ORDER_ENDPOINT)
        self.assertEqual([(t["price"], t["quantity"]) for t in tps], [(110.0, 3.0), (120.0, 7.0), (120.0, 7.0)],
                         "TP1 placed once; only the missing TP2 is retried")
        self.assertTrue(res2["actions"][0]["detail"]["retried"])
        self.assertEqual(len(read_jsonl(ws, "trades_audit.jsonl")), 1)
        self.assertEqual(read_registry(ws), {})

    def test_tps_already_placed_only_finish_audit_and_drop(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(701, 95.0)])
        res, ws = self.run_protect(fake, make_record(sl_qty=10.0, tp_placed=True, tp1_qty=3.0, tp2_qty=7.0,
                                                     tp1_order_id=11, tp2_order_id=12))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(fake.writes(), [], "no TP is placed twice")
        audit = read_jsonl(ws, "trades_audit.jsonl")
        self.assertEqual((audit[0]["tp1_order_id"], audit[0]["tp2_order_id"]), (11, 12))
        self.assertEqual(read_registry(ws), {})
        # audit_done set (e.g. the drop failed last time): never a second audit record
        res, ws = self.run_protect(fake, make_record(sl_qty=10.0, tp_placed=True, tp1_qty=3.0, tp2_qty=7.0,
                                                     tp1_order_id=11, tp2_order_id=12, audit_done=True))
        self.assertEqual(read_jsonl(ws, "trades_audit.jsonl"), [])
        self.assertEqual(read_registry(ws), {})

    def test_query_errors_keep_record_and_fail(self):
        for broken_endpoint in ("/fapi/v1/openAlgoOrders", "/fapi/v2/positionRisk"):
            fake = FakeExchange([long_position(amt="10")])

            def broken(method, endpoint, params=None, target_env=None, retry_count=0, _ep=broken_endpoint, _f=fake):
                if endpoint == _ep:
                    return {"error": "timeout"}
                return _f(method, endpoint, params, target_env)
            ws = tempfile.mkdtemp()
            write_registry(ws, make_record())
            with offline(broken, workspace=ws):
                res = eft.protect_pending_entries(target_env="testnet")
            self.assertFalse(res["ok"], broken_endpoint)
            self.assertTrue(res["errors"], broken_endpoint)
            self.assertEqual(fake.writes(), [], broken_endpoint)
            self.assertIn("testnet:BTCUSDT:7001", read_registry(ws), broken_endpoint)

    def test_corrupt_registry_fails_closed(self):
        ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(ws, "logs"))
        with open(os.path.join(ws, "logs", "pending_entries.json"), "w") as f:
            f.write("{not json")
        fake = FakeExchange([long_position()])
        with offline(fake, workspace=ws):
            res = eft.protect_pending_entries(target_env="testnet")
        self.assertFalse(res["ok"])
        self.assertEqual(res["errors"][0]["stage"], "registry")
        self.assertEqual(fake.calls, [])

    def test_dry_run_sends_no_writes_and_keeps_registry(self):
        for algos in ([], [stop(601, 98.4)]):  # place / replace (heal looser than the planned 99.4)
            fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=algos)
            res, ws = self.run_protect(fake, make_record(sl=99.4), dry_run=True)
            self.assertEqual(self.types(res), ["pending_protect_sl"])
            self.assertEqual(fake.writes(), [])
            self.assertTrue(res["actions"])
            self.assertTrue(all(a["dry_run"] and not a["success"] and a["detail"]["planned"] for a in res["actions"]))
            self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws = self.run_protect(fake, make_record(expires_in=-1), dry_run=True)
        self.assertEqual(self.types(res), ["pending_timeout_cancel"])
        self.assertEqual(fake.writes(), [])
        self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_records_of_other_env_ignored(self):
        fake = FakeExchange([long_position(amt="10")])
        res, ws = self.run_protect(fake, make_record(env="prod"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["actions"], [])
        self.assertEqual(fake.calls, [])
        self.assertIn("prod:BTCUSDT:7001", read_registry(ws))

    def test_no_registry_is_a_no_op(self):
        fake = FakeExchange([long_position()])
        res, ws = self.run_protect(fake)
        self.assertEqual(res, {"ok": True, "env": "testnet", "dry_run": False, "actions": [], "errors": []})
        self.assertEqual(fake.calls, [])
        self.assertIsNone(read_registry(ws))


# ---------------------------------------------------------------------------------------------
# Position guardian integration and CLI
# ---------------------------------------------------------------------------------------------
class TestGuardianProtectsPendingEntries(unittest.TestCase):

    def run_guardian(self, fake, ws, argv=("--once", "--env", "testnet")):
        log_dir = tempfile.mkdtemp()
        out = io.StringIO()
        with offline(fake, workspace=ws), patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)), \
             contextlib.redirect_stdout(out):
            code = pgl.main(list(argv))
        with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            return code, json.load(f)

    def test_filled_pending_entry_gets_planned_stop_not_orphan_heal(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="101.5")])
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record())
        code, state = self.run_guardian(fake, ws)
        self.assertEqual(code, 0, state["errors"])
        self.assertTrue(state["cycle_ok"])
        types = [a["type"] for a in state["actions"]]
        self.assertIn("pending_protect_sl", types)
        self.assertIn("pending_tp_placed", types)
        self.assertNotIn("orphan_heal", types)
        self.assertEqual([p["triggerPrice"] for p in posts(fake, ALGO_ENDPOINT)], [95.0], "no 2.5% emergency stop")
        self.assertTrue(state["positions"][0]["protected"])
        self.assertEqual(state["positions"][0]["stop_price"], 95.0)
        self.assertEqual(read_registry(ws), {})
        self.assertEqual(state["actions"][0]["detail"]["pending_entry_key"], "testnet:BTCUSDT:7001")

    def test_pending_query_error_marks_cycle_not_ok(self):
        fake = FakeExchange([])

        def broken(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v1/openAlgoOrders":
                return {"error": "timeout"}
            return fake(method, endpoint, params, target_env)
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record())
        code, state = self.run_guardian(broken, ws)
        self.assertEqual(code, 1)
        self.assertFalse(state["cycle_ok"])
        self.assertEqual(state["errors"][0]["stage"], "pending_orders_query")
        self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_guardian_state_mode_and_liveness(self):
        ws = tempfile.mkdtemp()
        log_dir = os.path.join(ws, "logs")
        fake = FakeExchange([])

        def run(argv):
            with offline(fake, workspace=ws), patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
                 patch("position_guardian_loop.time.sleep", side_effect=KeyboardInterrupt), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = pgl.main(argv)
                alive = eft.check_guardian_alive("testnet")
            with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
                return code, json.load(f), alive

        _, state, alive = run(["--once", "--env", "testnet"])
        self.assertEqual((state["mode"], state["interval_seconds"]), ("once", None))
        self.assertFalse(alive[0], "a single --once run never counts as a live guardian")
        code, state, alive = run(["--interval", "60", "--env", "testnet"])
        self.assertEqual(code, 0)
        self.assertEqual((state["mode"], state["interval_seconds"]), ("loop", 60))
        self.assertTrue(alive[0], alive[1])
        _, state, alive = run(["--interval", "300", "--env", "testnet"])
        self.assertEqual(state["interval_seconds"], 300)
        self.assertFalse(alive[0])
        _, state, alive = run(["--interval", "60", "--env", "testnet", "--dry-run"])
        self.assertFalse(alive[0])

    def test_no_registry_guardian_unchanged(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        code, state = self.run_guardian(fake, tempfile.mkdtemp())
        self.assertEqual(code, 0)
        self.assertEqual(state["actions"], [])
        self.assertEqual(fake.writes(), [])


class TestGuardianReportsUnknownRestingEntries(unittest.TestCase):
    """Issue #46: each guardian cycle cross-checks the exchange against logs/pending_entries.json; an opening order
    resting without a record is reported (error + action, cycle not ok) and never cancelled."""

    run_guardian = TestGuardianProtectsPendingEntries.run_guardian

    def unknown_fake(self):
        return FakeExchange([long_position()], algos=[stop(501, 95.0), entry_algo(algo_id=7001, symbol="XRPUSDT")],
                            open_orders=[entry_limit(order_id=77),
                                         entry_limit(order_id=78, symbol="BTCUSDT", side="SELL", price=110.0,
                                                     reduce_only=True)])

    def assertReported(self, state, fake, dry_run):
        self.assertFalse(state["cycle_ok"])
        unknown = [a for a in state["actions"] if a["type"] == "unknown_resting_entry"]
        self.assertEqual([(a["symbol"], a["detail"]["kind"], a["detail"]["id"]) for a in unknown],
                         [("XRPUSDT", "STOP_MARKET", 7001), ("ETHUSDT", "LIMIT", 77)])
        self.assertTrue(all(not a["success"] and a["dry_run"] is dry_run for a in unknown))
        self.assertIn("without a logs/pending_entries.json record", unknown[0]["detail"]["message"])
        self.assertEqual([(e["symbol"], e["stage"]) for e in state["errors"]],
                         [("XRPUSDT", "pending_unknown_entry"), ("ETHUSDT", "pending_unknown_entry")])
        self.assertEqual(fake.writes(), [], "unknown entries are never cancelled by the guardian")
        self.assertTrue(state["positions"][0]["protected"])

    def test_unknown_entries_reported_not_cancelled(self):
        for env in ("testnet", "prod"):
            fake = self.unknown_fake()
            ws = tempfile.mkdtemp()
            code, state = self.run_guardian(fake, ws, argv=("--once", "--env", env))
            self.assertEqual(code, 1, env)
            self.assertReported(state, fake, dry_run=False)
            self.assertIsNone(read_registry(ws), "the registry is never written by the check")

    def test_dry_run_reports_without_writes(self):
        fake = self.unknown_fake()
        code, state = self.run_guardian(fake, tempfile.mkdtemp(), argv=("--once", "--env", "prod", "--dry-run"))
        self.assertEqual(code, 1)
        self.assertReported(state, fake, dry_run=True)

    def test_registered_entries_are_not_reported(self):
        fake = self.unknown_fake()
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(kind="STOP_MARKET", entry_id="7001", symbol="XRPUSDT", env="prod"),
                       make_record(kind="LIMIT", entry_id="77", symbol="ETHUSDT", env="prod", total_qty=1.5,
                                   trigger_or_limit_price=98.0))   # matches the resting order (issue #101)
        code, state = self.run_guardian(fake, ws, argv=("--once", "--env", "prod"))
        self.assertEqual(code, 0, state["errors"])
        self.assertTrue(state["cycle_ok"])
        self.assertEqual([a for a in state["actions"] if a["type"] == "unknown_resting_entry"], [])

    def test_cross_check_query_error_marks_cycle_not_ok(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])

        def broken(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v1/openOrders" and not (params or {}).get("symbol"):
                return {"code": -1001, "msg": "Internal error"}
            return fake(method, endpoint, params, target_env)
        code, state = self.run_guardian(broken, tempfile.mkdtemp(), argv=("--once", "--env", "prod"))
        self.assertEqual(code, 1)
        self.assertFalse(state["cycle_ok"])
        self.assertEqual([e["stage"] for e in state["errors"]], ["pending_unknown_entry"])
        self.assertIn("/fapi/v1/openOrders query failed", state["errors"][0]["error"])
        self.assertEqual(fake.writes(), [])


class TestProtectPendingCLI(unittest.TestCase):

    def test_dispatch_and_exit_codes(self):
        for ok, expected in ((True, 0), (False, 1)):
            with patch("execute_futures_trade.protect_pending_entries",
                       return_value={"ok": ok, "actions": [], "errors": []}) as mock_pp:
                code, out, _ = run_cli(["--protect-pending", "--env", "testnet"])
            self.assertEqual(code, expected)
            mock_pp.assert_called_once_with(target_env="testnet")
            self.assertEqual(json.loads(out)["ok"], ok)
        with patch("execute_futures_trade.protect_pending_entries", return_value={"ok": True}) as mock_pp:
            run_cli(["--protect_pending", "--env", "testnet"])
        mock_pp.assert_called_once()

    def test_exclusive_with_other_modes_and_direction(self):
        for extra in (["--close-position", "--symbol", "BTCUSDT"], ["--auto-heal"], ["--audit-orphans"],
                      ["--positions"], ["--move-breakeven", "--symbol", "BTCUSDT"],
                      ["--symbol", "BTCUSDT", "--direction", "LONG"]):
            with patch("execute_futures_trade.protect_pending_entries") as mock_pp, \
                 patch("execute_futures_trade.close_position_market") as mock_close, \
                 patch("execute_futures_trade.audit_orphan_positions") as mock_audit, \
                 patch("execute_futures_trade.execute_complete_trade") as mock_trade:
                code, _, _ = run_cli(["--protect-pending", "--env", "testnet", *extra])
            self.assertEqual(code, 1, extra)
            for m in (mock_pp, mock_close, mock_audit, mock_trade):
                m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
