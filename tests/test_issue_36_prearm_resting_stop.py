#!/usr/bin/env python3
"""
test_issue_36_prearm_resting_stop.py - Offline tests for issues #36, #39 and #118 (protect-pending hardening).

#36  A resting entry (untriggered STOP_MARKET, resting LIMIT) gets its planned Stop Loss pre-armed at placement as a
     closePosition STOP_MARKET on KEYS when it is not crossed; the record (schema v2) stores the outcome; the guardian
     verifies the pre-arm at fill (no second placement), places the planned stop when it was consumed, reads -4130 as
     "kept", and cancels the pre-arm (by algo id only) on every entry-ending path without a position.
#39  KEYS closePosition stops are never resized; the inline partial-fill abort and the crossed-SL close use the live
     size (the entry remainder is cancelled first); a failed crossed close in place mode gets an orphan-heal stop; the
     audit record logs the real R:R to TP2, the TP1 distance and fill-quality flags.
#118 PROD: a record whose SL distance exceeds the Gate 2 loss cap is untrusted (standard and YOLO); a failed equity
     read defers the check; TESTNET skips it; a kept stop that does not cover the position is replaced.

No network: every exchange call is faked, files go to temp directories, sleeps are patched.
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
from test_exit_management import FakeExchange, offline, long_position, stop, ALGO_ENDPOINT
from test_pending_entries import (ExecutorHarness, make_record, write_registry, read_registry, read_jsonl, entry_algo,
                                  posts, ORDER_ENDPOINT, EX_FILTERS, EX_PROFILE)

PROD_PROFILE = {"risk_pct_equity": 0.005, "leverage_standard": 3, "leverage_yolo": 15, "leverage_ceiling": 15,
                "yolo_slot_enabled": True}
MINUS_4130 = {"code": -4130, "msg": "An open stop or take profit order with GTE and closePosition in the direction is existing."}
MINUS_2021 = {"code": -2021, "msg": "Order would immediately trigger."}


def prearmed(algo_id=9, price=95.0, **extra):
    """v2 fields of a verified pre-arm (as written by prearm_resting_entry_stop)."""
    return dict(dict(prearm_status="placed", prearm_algo_id=algo_id, prearm_price=price, sl_close_position=True,
                     sl_qty=None), **extra)


def limit_order(order_id=8001, qty="12", price="101.0"):
    return {"orderId": order_id, "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": price, "origQty": qty,
            "reduceOnly": False}


def registry_file(ws):
    with open(os.path.join(ws, "logs", "pending_entries.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def deletes(fake, endpoint=ALGO_ENDPOINT):
    return [c[2] for c in fake.calls if c[0] == "DELETE" and c[1] == endpoint]


def run_protect(send, *records, env="testnet", mcp=False, profile=None, equity=10000.0, dry_run=False):
    """protect_pending_entries with a fake exchange; equity: a number, or an exception raised by the read."""
    ws = tempfile.mkdtemp()
    if records:
        write_registry(ws, *records)
    eq = patch("quant_risk_engine.get_account_equity", side_effect=equity) if isinstance(equity, Exception) else \
        patch("quant_risk_engine.get_account_equity", return_value=equity)
    with offline(send, workspace=ws, profile=profile or PROD_PROFILE), eq as mock_equity, \
         patch("execute_futures_trade.uses_mcp_gateway", return_value=mcp):
        res = eft.protect_pending_entries(target_env=env, dry_run=dry_run)
    return res, ws, mock_equity


def types(res):
    return [a["type"] for a in res["actions"]]


# ---------------------------------------------------------------------------------------------
# #36: pre-arm at placement
# ---------------------------------------------------------------------------------------------
class TestPrearmAtPlacement(ExecutorHarness):

    def algo_posts(self):
        return [c[2] for c in self.calls if c[0] == "POST" and c[1] == ALGO_ENDPOINT]

    def test_long_stop_market_prearmed_and_recorded_v2(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual((res["prearm_status"], res["prearm_algo_id"]), ("placed", 9))
        self.assertIn("Stop Loss pre-armed at 97.0", res["message"])
        sent = self.algo_posts()
        self.assertEqual([(s["side"], s["type"], s.get("closePosition")) for s in sent],
                         [("BUY", "STOP_MARKET", "false"), ("SELL", "STOP_MARKET", "true")],
                         "the entry is algo POST [0], the pre-arm [1]")
        self.assertNotIn("quantity", sent[1])
        self.assertNotIn("reduceOnly", sent[1])
        rec = read_registry(self.ws)["testnet:SOLUSDT:8"]
        self.assertEqual((rec["prearm_status"], rec["prearm_algo_id"], rec["prearm_price"]), ("placed", 9, 97.0))
        self.assertIs(rec["sl_close_position"], True)
        self.assertIn("sl_qty", rec)
        self.assertIsNone(rec["sl_qty"])
        self.assertEqual(registry_file(self.ws)["schema_version"], 2)
        self.assertEqual(eft.PENDING_ENTRIES_SCHEMA_VERSION, 2)

    def test_short_stop_market_prearmed_with_buy_stop_above(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=97.653, direction="SHORT", sl_price=103.0,
                           tp1_price=90.0, tp2_price=80.0)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["prearm_status"], "placed")
        sent = self.algo_posts()
        self.assertEqual((sent[1]["side"], sent[1]["triggerPrice"], sent[1]["closePosition"]), ("BUY", 103.0, "true"))
        rec = read_registry(self.ws)["testnet:SOLUSDT:8"]
        self.assertEqual((rec["direction"], rec["prearm_status"], rec["prearm_price"]), ("SHORT", "placed", 103.0))

    def test_resting_limit_prearmed(self):
        res = self.execute(order_type="LIMIT", limit_price=98.767)
        self.assertTrue(res["pending_limit_entry"], res)
        self.assertEqual(res["prearm_status"], "placed")
        self.assertIn("deferred until fill", res["message"])
        self.assertEqual([(s["side"], s["triggerPrice"], s["closePosition"]) for s in self.algo_posts()],
                         [("SELL", 97.0, "true")])
        entry_index = next(i for i, c in enumerate(self.calls) if c[0] == "POST" and c[1] == ORDER_ENDPOINT)
        prearm_index = next(i for i, c in enumerate(self.calls) if c[0] == "POST" and c[1] == ALGO_ENDPOINT)
        self.assertLess(entry_index, prearm_index, "the pre-arm is placed after the entry")
        self.assertEqual(read_registry(self.ws)["testnet:SOLUSDT:7"]["prearm_status"], "placed")

    def test_crossed_sl_skipped(self):
        # LONG breakout: trigger 102.34 above the 100 price, SL 100.5 between them (already crossed)
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347, sl_price=100.5)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["prearm_status"], "skipped:crossed")
        self.assertEqual(len(self.algo_posts()), 1, "only the entry")
        rec = read_registry(self.ws)["testnet:SOLUSDT:8"]
        self.assertEqual(rec["prearm_status"], "skipped:crossed")
        self.assertNotIn("prearm_algo_id", rec)
        self.assertNotIn("sl_close_position", rec)
        # SHORT: SL 99.5 below the 100 price
        self.calls, self.open_algos = [], []
        res = self.execute(order_type="STOP_MARKET", trigger_price=97.653, direction="SHORT", sl_price=99.5,
                           tp1_price=90.0, tp2_price=80.0, symbol="ETHUSDT")
        self.assertEqual(res["prearm_status"], "skipped:crossed")
        self.assertEqual(len(self.algo_posts()), 1)

    def test_mcp_skipped(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347, mcp=True)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["prearm_status"], "skipped:mcp")
        self.assertEqual(len(self.algo_posts()), 1)
        self.assertEqual(read_registry(self.ws)["testnet:SOLUSDT:8"]["prearm_status"], "skipped:mcp")
        res = self.execute(order_type="LIMIT", limit_price=98.767, mcp=True, symbol="ETHUSDT")
        self.assertEqual(res["prearm_status"], "skipped:mcp")

    def test_rejected_prearm_keeps_the_entry(self):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ALGO_ENDPOINT and (params or {}).get("closePosition") == "true":
                self.calls.append((method, endpoint, dict(params)))
                return dict(MINUS_2021)
            return self.fake(method, endpoint, params, target_env)
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347, send=send)
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res["conditional_entry"])
        self.assertEqual(res["prearm_status"], "rejected:-2021")
        self.assertIn("Stop Loss not pre-armed (rejected:-2021)", res["message"])
        self.assertEqual(self.writes()[-1][1], ALGO_ENDPOINT)
        self.assertEqual([c for c in self.calls if c[0] == "DELETE"], [], "a rejected pre-arm never cancels the entry")
        rec = read_registry(self.ws)["testnet:SOLUSDT:8"]
        self.assertEqual(rec["prearm_status"], "rejected:-2021")
        self.assertNotIn("prearm_algo_id", rec)
        self.assertNotIn("sl_close_position", rec)

    def test_unexpected_prearm_error_still_registers_the_entry(self):
        with patch("execute_futures_trade._prearm_resting_entry_stop", side_effect=RuntimeError("boom")):
            res = self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["prearm_status"], "rejected:RuntimeError: boom")
        self.assertEqual(read_registry(self.ws)["testnet:SOLUSDT:8"]["prearm_status"], "rejected:RuntimeError: boom")
        self.assertEqual([c for c in self.calls if c[0] == "DELETE"], [])

    def test_registry_failure_cancels_limit_and_its_prearm(self):
        with patch("execute_futures_trade.update_pending_entries", side_effect=IOError("disk full")):
            res = self.execute(order_type="LIMIT", limit_price=98.767)
        self.assertFalse(res["success"])
        self.assertTrue(res["entry_cancelled"])
        self.assertTrue(res["prearm_cancelled"])
        self.assertEqual([c[2] for c in self.calls if c[0] == "DELETE" and c[1] == ORDER_ENDPOINT],
                         [{"symbol": "SOLUSDT", "orderId": 7}])
        self.assertEqual([c[2] for c in self.calls if c[0] == "DELETE" and c[1] == ALGO_ENDPOINT],
                         [{"symbol": "SOLUSDT", "algoId": 9}], "by algo id only")

    def test_registry_failure_keeps_prearm_when_a_position_exists(self):
        self.positions = [{"symbol": "SOLUSDT", "positionAmt": "0.1", "entryPrice": "98.76", "markPrice": "99",
                           "unRealizedProfit": "0"}]  # the LIMIT filled before the cancel
        with patch("execute_futures_trade.update_pending_entries", side_effect=IOError("disk full")):
            res = self.execute(order_type="LIMIT", limit_price=98.767)
        self.assertFalse(res["prearm_cancelled"])
        self.assertIn("was not cancelled", res["error"])
        self.assertEqual([c for c in self.calls if c[0] == "DELETE" and c[1] == ALGO_ENDPOINT], [],
                         "the pre-arm protects the fill")

    def test_market_entry_not_prearmed(self):
        res = self.execute(order_type="MARKET")
        self.assertTrue(res["success"], res.get("error"))
        self.assertNotIn("prearm_status", res)
        self.assertEqual(len(self.algo_posts()), 1, "only the post-fill Stop Loss")


# ---------------------------------------------------------------------------------------------
# #36: verification at fill (_ensure_entry_stop) and pre-arm cancel paths
# ---------------------------------------------------------------------------------------------
class TestPrearmAtFill(unittest.TestCase):

    def test_v1_record_read_as_not_prearmed(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, ws, _ = run_protect(fake, make_record())   # write_registry writes schema_version 1
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_protect_sl", "pending_tp_placed"])
        self.assertEqual(res["actions"][0]["detail"]["stop_source"], "placed")
        self.assertEqual([(s["triggerPrice"], s["closePosition"]) for s in posts(fake, ALGO_ENDPOINT)], [(95.0, "true")])

    def test_rejected_prearm_guardian_still_protects_at_fill(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, ws, _ = run_protect(fake, make_record(prearm_status="rejected:-2021"))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual([s["triggerPrice"] for s in posts(fake, ALGO_ENDPOINT)], [95.0])
        self.assertEqual(read_registry(ws), {})

    def test_verified_prearm_kept_no_second_placement_no_resize(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")], algos=[stop(9, 95.0)])
        res, ws, _ = run_protect(fake, make_record(**prearmed()))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_tp_placed"])
        self.assertEqual(posts(fake, ALGO_ENDPOINT), [], "no second stop, no resize")
        self.assertEqual(deletes(fake), [])
        audit = read_jsonl(ws, "trades_audit.jsonl")
        self.assertEqual((audit[0]["sl_price"], audit[0]["sl_algo_id"]), (95.0, 9))
        self.assertEqual(read_registry(ws), {})

    def test_partial_fill_with_verified_prearm_not_resized(self):
        fake = FakeExchange([long_position(amt="4", entry="101.0")], algos=[stop(9, 95.0)], open_orders=[limit_order()])
        rec = make_record(kind="LIMIT", entry_id="8001", **prearmed())
        ws = tempfile.mkdtemp()
        write_registry(ws, rec)
        with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
            res1 = eft.protect_pending_entries(target_env="testnet")
            self.assertEqual(read_registry(ws)["testnet:BTCUSDT:8001"]["sl_algo_id"], 9)
            fake.positions = [long_position(amt="10", entry="101.0")]   # the fill grows
            res2 = eft.protect_pending_entries(target_env="testnet")
        self.assertTrue(res1["ok"] and res2["ok"], (res1["errors"], res2["errors"]))
        self.assertEqual(res1["actions"] + res2["actions"], [])
        self.assertEqual(fake.writes(), [], "a closePosition pre-arm covers any size")

    def test_consumed_prearm_planned_stop_placed(self):
        # The pre-arm triggered before the fill (gone from openAlgoOrders): the planned SL is placed as before
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, ws, _ = run_protect(fake, make_record(**prearmed()))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["actions"][0]["detail"]["stop_source"], "placed")
        self.assertEqual([(s["triggerPrice"], s["closePosition"]) for s in posts(fake, ALGO_ENDPOINT)], [(95.0, "true")])
        self.assertEqual(read_registry(ws), {})

    def test_minus_4130_on_placement_means_kept(self):
        # The pre-arm exists but is not indexed yet when the stops are listed: the placement gets -4130, and the
        # progressive re-verification finds it. No auto-destruct.
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")], reject_new_stops=True,
                            reject_response=MINUS_4130)

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            res = fake(method, endpoint, params, target_env)
            if method == "POST" and endpoint == ALGO_ENDPOINT:
                fake.algos.append(stop(9, 95.0))   # indexed now
            return res
        with patch("execute_futures_trade.emergency_abort_market_close") as mock_abort:
            res, ws, _ = run_protect(send, make_record(**prearmed()))
        mock_abort.assert_not_called()
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["actions"][0]["detail"]["stop_source"], "kept")
        self.assertTrue(res["actions"][0]["success"])
        self.assertEqual([c for c in posts(fake, ORDER_ENDPOINT) if c.get("type") == "MARKET"], [])
        self.assertEqual(read_registry(ws), {})

    def test_ensure_entry_stop_minus_4130_without_listing_is_unverified(self):
        fake = FakeExchange([], reject_new_stops=True, reject_response=MINUS_4130)
        with offline(fake):
            out = eft._ensure_entry_stop("BTCUSDT", "SELL", 95.0, target_env="testnet")
        self.assertEqual((out["verified"], out["source"]), (False, None))

    def test_timeout_cancels_entry_and_prearm_by_id_only(self):
        other = stop(555, 90.0)   # another protective stop on the symbol: never swept
        fake = FakeExchange([], algos=[entry_algo(), stop(9, 95.0), other])
        res, ws, _ = run_protect(fake, make_record(expires_in=-1, **prearmed()))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_timeout_cancel"])
        self.assertTrue(res["actions"][0]["detail"]["prearm_cancelled"])
        self.assertEqual(deletes(fake), [{"symbol": "BTCUSDT", "algoId": 7001}, {"symbol": "BTCUSDT", "algoId": 9}])
        self.assertEqual([a["algoId"] for a in fake.algos], [555])
        self.assertEqual(read_registry(ws), {})

    def test_timeout_keeps_prearm_when_the_entry_filled_meanwhile(self):
        fake = FakeExchange([], algos=[entry_algo(), stop(9, 95.0)])

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            res = fake(method, endpoint, params, target_env)
            if method == "DELETE" and (params or {}).get("algoId") == 7001:
                fake.positions = [long_position(amt="3", entry="101.0")]   # partial trigger raced the cancel
            return res
        res, ws, _ = run_protect(send, make_record(expires_in=-1, **prearmed()))
        self.assertFalse(res["ok"])
        self.assertEqual(res["errors"][0]["stage"], "prearm_cancel")
        self.assertEqual(deletes(fake), [{"symbol": "BTCUSDT", "algoId": 7001}])
        self.assertIn(9, [a["algoId"] for a in fake.algos])
        self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_dropped_entry_cancels_prearm(self):
        fake = FakeExchange([], algos=[stop(9, 95.0)])
        res, ws, _ = run_protect(fake, make_record(missing_since_ts=0, **prearmed()))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_dropped"])
        self.assertEqual(deletes(fake), [{"symbol": "BTCUSDT", "algoId": 9}])
        self.assertEqual(read_registry(ws), {})

    def test_failed_prearm_cancel_keeps_record_for_next_run(self):
        fake = FakeExchange([], algos=[stop(9, 95.0)], fail_cancel=True)
        res, ws, _ = run_protect(fake, make_record(missing_since_ts=0, **prearmed()))
        self.assertFalse(res["ok"])
        self.assertEqual(res["errors"][0]["stage"], "prearm_cancel")
        self.assertFalse(res["actions"][0]["success"])
        self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_prearm_id_of_another_symbol_never_cancelled(self):
        fake = FakeExchange([], algos=[stop(555, 95.0, symbol="ETHUSDT")])
        res, ws, _ = run_protect(fake, make_record(missing_since_ts=0, **prearmed(algo_id=555)))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(deletes(fake), [], "not listed for BTCUSDT: nothing to cancel")
        self.assertEqual(read_registry(ws), {})

    def test_untrusted_record_cancels_entry_and_prearm(self):
        fake = FakeExchange([], algos=[entry_algo(), stop(9, 95.0)])   # entry quantity 12
        res, ws, _ = run_protect(fake, make_record(total_qty=5.0, **prearmed()))
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertTrue(res["actions"][0]["success"])
        self.assertTrue(res["actions"][0]["detail"]["prearm_cancelled"])
        self.assertEqual(deletes(fake), [{"symbol": "BTCUSDT", "algoId": 7001}, {"symbol": "BTCUSDT", "algoId": 9}])
        self.assertEqual(read_registry(ws), {})


# ---------------------------------------------------------------------------------------------
# #39
# ---------------------------------------------------------------------------------------------
class TestKeysCoveringStopNotResized(unittest.TestCase):

    def test_keys_close_position_stop_not_resized_mcp_resized(self):
        rec = make_record(sl=95.0, sl_qty=4.0)
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(701, 95.0)])
        res, _, _ = run_protect(fake, rec, mcp=False)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(posts(fake, ALGO_ENDPOINT), [], "KEYS: closePosition covers any size")
        self.assertEqual(deletes(fake), [])
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(701, 95.0)])
        res, _, _ = run_protect(fake, rec, mcp=True)
        self.assertEqual(res["actions"][0]["detail"]["mode"], "resize")
        self.assertEqual([(s["triggerPrice"], s["quantity"]) for s in posts(fake, ALGO_ENDPOINT)], [(95.0, "10")])

    def test_keys_short_quantity_stop_resized(self):
        short = dict(stop(701, 96.0, close_position=False), reduceOnly=True, quantity="4")
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[short])
        res, _, _ = run_protect(fake, make_record(sl=95.0, sl_qty=10.0))   # a stale sl_qty is ignored on KEYS
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["actions"][0]["detail"]["mode"], "resize")
        self.assertEqual([(s["triggerPrice"], s["quantity"]) for s in posts(fake, ALGO_ENDPOINT)], [(96.0, "10")])
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [96.0])


def partial_fill_execute(fx, grow_to=None, position_reads_fail=False):
    """execute_complete_trade with a LIMIT that answers PARTIALLY_FILLED (executedQty 0.1) and whose position grows
    to `grow_to` between the response and the cancel of the remainder."""
    ws = tempfile.mkdtemp()

    def send(method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        if position_reads_fail and endpoint == "/fapi/v2/positionRisk":
            return {"error": "timeout"}
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
            fx.open_orders.append({"orderId": 7, "symbol": "SOLUSDT", "side": "BUY", "type": "LIMIT",
                                   "price": str(params["price"]), "origQty": str(params["quantity"]), "reduceOnly": False})
            fx.positions.append(long_position("SOLUSDT", amt="0.1", entry=str(params["price"])))
            return {"orderId": 7, "status": "PARTIALLY_FILLED", "executedQty": "0.1"}
        res = fx(method, endpoint, params, target_env)
        if method == "DELETE" and endpoint == ORDER_ENDPOINT and grow_to:
            fx.positions = [long_position("SOLUSDT", amt=grow_to, entry="98.76")]
        return res

    with patch("execute_futures_trade.send_signed_request", side_effect=send), \
         patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
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


class TestPartialFillAbortLiveSize(unittest.TestCase):

    def test_abort_closes_the_live_size_after_cancelling_the_remainder(self):
        fx = FakeExchange([], reject_new_stops=True, reject_response={"code": -1001, "msg": "Internal error"})
        res, _ = partial_fill_execute(fx, grow_to="0.15")
        self.assertTrue(res["emergency_abort"])
        self.assertNotIn("prearm_status", res, "a PARTIALLY_FILLED LIMIT is not pre-armed")
        cancel = fx.write_index("DELETE", ORDER_ENDPOINT)
        closes = [i for i, c in enumerate(fx.calls) if c[0] == "POST" and c[1] == ORDER_ENDPOINT
                  and c[2].get("type") == "MARKET"]
        self.assertTrue(cancel and closes)
        self.assertLess(cancel[0], closes[0])
        self.assertEqual({fx.calls[i][2]["quantity"] for i in closes}, {"0.15"}, "live size, not executedQty 0.1")

    def test_abort_falls_back_to_executed_qty_when_the_reread_fails(self):
        fx = FakeExchange([], reject_new_stops=True, reject_response={"code": -1001, "msg": "Internal error"})
        res, _ = partial_fill_execute(fx, grow_to="0.15", position_reads_fail=True)
        closes = [c[2] for c in fx.calls if c[0] == "POST" and c[1] == ORDER_ENDPOINT and c[2].get("type") == "MARKET"]
        self.assertTrue(res["emergency_abort"])
        self.assertTrue(closes)
        self.assertEqual({c["quantity"] for c in closes}, {"0.1"})


class TestCrossedCloseOrderAndHeal(unittest.TestCase):

    def test_remainder_cancelled_before_closing_the_live_size(self):
        fake = FakeExchange([long_position(amt="4", entry="101.0", mark="94.0")], open_orders=[limit_order()])

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ORDER_ENDPOINT and (params or {}).get("type") == "MARKET":
                fake.calls.append((method, endpoint, dict(params)))
                fake.positions = []
                return {"orderId": 55, "status": "FILLED"}
            res = fake(method, endpoint, params, target_env)
            if method == "DELETE" and endpoint == ORDER_ENDPOINT:
                fake.positions = [long_position(amt="5", entry="101.0", mark="94.0")]  # filled until the cancel
            return res
        res, ws, _ = run_protect(send, make_record(kind="LIMIT", entry_id="8001"))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_sl_crossed_close"])
        writes = fake.writes()
        self.assertEqual((writes[0][0], writes[0][1], writes[0][2]["orderId"]), ("DELETE", ORDER_ENDPOINT, 8001))
        self.assertEqual((writes[1][1], writes[1][2]["type"], writes[1][2]["quantity"], writes[1][2]["reduceOnly"]),
                         (ORDER_ENDPOINT, "MARKET", "5", "true"))
        self.assertEqual(read_registry(ws), {})

    def test_failed_close_in_place_mode_heals_the_position(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="94.0")])   # no stop at all

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ORDER_ENDPOINT and (params or {}).get("type") == "MARKET":
                fake.calls.append((method, endpoint, dict(params)))
                return {"code": -1001, "msg": "Internal error"}
            return fake(method, endpoint, params, target_env)
        res, ws, _ = run_protect(send, make_record(sl=95.0))
        self.assertFalse(res["ok"])
        self.assertEqual(res["errors"][0]["stage"], "sl_crossed_close")
        heal = res["actions"][0]["detail"]["heal"]
        self.assertTrue(heal["success"], heal)
        # planned 95 is crossed (mark 94): the heal uses its anchor, 2.5% below min(entry, mark) = 94 -> 91.6
        self.assertEqual([(s["triggerPrice"], s["closePosition"]) for s in posts(fake, ALGO_ENDPOINT)], [(91.6, "true")])
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [91.6])
        self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_failed_close_with_existing_stop_never_heals(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="99.0")], algos=[stop(601, 98.4)])

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ORDER_ENDPOINT and (params or {}).get("type") == "MARKET":
                fake.calls.append((method, endpoint, dict(params)))
                return {"code": -1001, "msg": "Internal error"}
            return fake(method, endpoint, params, target_env)
        res, ws, _ = run_protect(send, make_record(sl=99.4))
        self.assertIsNone(res["actions"][0]["detail"]["heal"])
        self.assertEqual(posts(fake, ALGO_ENDPOINT), [])


class TestFillQualityAudit(unittest.TestCase):

    def test_audit_fields_from_real_entry_and_stop_in_force(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])
        res, ws, _ = run_protect(fake, make_record(sl=95.0, tp1=110.0, tp2=120.0))
        audit = read_jsonl(ws, "trades_audit.jsonl")[0]
        self.assertAlmostEqual(audit["realized_rr_tp2"], round(19.0 / 6.0, 4))
        self.assertAlmostEqual(audit["tp1_distance_pct"], round(9.0 / 101.0 * 100, 4))
        self.assertEqual(audit["fill_quality_flags"], [])

    def test_flags_for_slipped_fill(self):
        # The STOP_MARKET filled at 104 (slippage): R:R to TP2 (16/9) < 3 and TP1 104.2 is 0.19% away (< 0.35%)
        fake = FakeExchange([long_position(amt="12", entry="104.0", mark="104.1")])
        run = run_protect(fake, make_record(sl=95.0, tp1=104.2, tp2=120.0))
        audit = read_jsonl(run[1], "trades_audit.jsonl")[0]
        self.assertEqual(audit["fill_quality_flags"], ["rr_below_3", "tp1_below_friction"])
        self.assertAlmostEqual(audit["realized_rr_tp2"], round(16.0 / 9.0, 4))

    def test_pure_helper_short_and_break_even(self):
        out = eft.fill_quality_fields(False, 100.0, 103.0, 99.0, 88.0)
        self.assertEqual((out["realized_rr_tp2"], out["tp1_distance_pct"], out["fill_quality_flags"]),
                         (4.0, 1.0, []))
        out = eft.fill_quality_fields(True, 100.0, 100.2, 101.0, 110.0)   # stop beyond entry: R:R undefined
        self.assertIsNone(out["realized_rr_tp2"])
        self.assertEqual(out["fill_quality_flags"], [])


# ---------------------------------------------------------------------------------------------
# #118
# ---------------------------------------------------------------------------------------------
class TestRecordLossCap(unittest.TestCase):
    """make_record: loss at SL = |101 - 95| x 12 = 72 USDT. Standard cap = equity x 0.5% x 1.25."""

    def test_prod_standard_breach_cancels_resting_entry(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws, mock_eq = run_protect(fake, make_record(env="prod"), env="prod", equity=1000.0)   # cap 6.25
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertEqual(res["actions"][0]["detail"]["reason"], "resting_entry_mismatch")
        self.assertIn("exceeds the Gate 2 loss cap 6.25", " ".join(res["actions"][0]["detail"]["mismatches"]))
        self.assertEqual(deletes(fake), [{"symbol": "BTCUSDT", "algoId": 7001}])
        self.assertEqual(read_registry(ws), {})
        mock_eq.assert_called_once_with("prod")

    def test_prod_standard_within_cap_kept(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws, _ = run_protect(fake, make_record(env="prod"), env="prod", equity=20000.0)   # cap 125
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["actions"], [])
        self.assertEqual(fake.writes(), [])
        self.assertIn("prod:BTCUSDT:7001", read_registry(ws))

    def test_unrealized_loss_lowers_the_cap(self):
        # 12000 wallet -> cap 75 >= 72; an open loss of -1000 elsewhere -> min(12000, 11000) x 0.625% = 68.75 < 72
        other = dict(long_position(symbol="ETHUSDT", amt="1", entry="100", mark="90"), unRealizedProfit="-1000")
        fake = FakeExchange([other], algos=[entry_algo()])
        res, ws, _ = run_protect(fake, make_record(env="prod"), env="prod", equity=12000.0)
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertIn("68.75", " ".join(res["actions"][0]["detail"]["mismatches"]))

    def test_prod_standard_breach_filled_uses_orphan_heal(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")])   # entry gone, no stop
        res, ws, _ = run_protect(fake, make_record(env="prod"), env="prod", equity=1000.0)
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertEqual(res["actions"][0]["detail"]["reason"], "filled_with_invalid_sl")
        self.assertTrue(res["actions"][0]["detail"]["heal"]["success"])
        # the record SL (95) is never used: 2.5% anchor below min(entry 101, mark 101.5) -> 98.4
        self.assertEqual([s["triggerPrice"] for s in posts(fake, ALGO_ENDPOINT)], [98.4])
        self.assertEqual(posts(fake, ORDER_ENDPOINT), [], "no TP from an untrusted record")
        self.assertEqual(read_registry(ws), {})

    def test_prod_yolo_breach_resting_uses_record_leverage_without_equity_read(self):
        fake = FakeExchange([], algos=[entry_algo()])
        # margin 101 x 12 / 15 = 80.8 -> cap max(3.75, 28.28) < 72
        res, ws, mock_eq = run_protect(fake, make_record(env="prod", is_yolo=True, leverage=15), env="prod",
                                       equity=RuntimeError("must not be read"))
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertIn("YOLO margin cap at 15x", " ".join(res["actions"][0]["detail"]["mismatches"]))
        mock_eq.assert_not_called()
        self.assertEqual(read_registry(ws), {})

    def test_prod_yolo_filled_uses_live_position_leverage(self):
        rec = make_record(env="prod", is_yolo=True, leverage=3)   # record 3x: cap 141.4 >= 72
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5", leverage="3")])
        res, _, _ = run_protect(fake, rec, env="prod")
        self.assertEqual(types(res), ["pending_protect_sl", "pending_tp_placed"])
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5", leverage="15")])   # live 15x
        res, _, _ = run_protect(fake, rec, env="prod")
        self.assertEqual(types(res), ["pending_record_mismatch"])
        self.assertIn("YOLO margin cap at 15x", " ".join(res["actions"][0]["detail"]["mismatches"]))

    def test_failed_equity_read_defers_the_check(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws, _ = run_protect(fake, make_record(env="prod"), env="prod", equity=RuntimeError("balance timeout"))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["actions"], [])
        self.assertEqual(fake.writes(), [])
        self.assertEqual([w["stage"] for w in res["warnings"]], ["loss_cap_check"])
        self.assertIn("balance timeout", res["warnings"][0]["warning"])
        self.assertIn("prod:BTCUSDT:7001", read_registry(ws))

    def test_failed_position_read_defers_the_check(self):
        fake = FakeExchange([], algos=[entry_algo()])

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v2/positionRisk" and not (params or {}).get("symbol"):
                return {"code": -1001, "msg": "Internal error"}
            return fake(method, endpoint, params, target_env)
        res, ws, _ = run_protect(send, make_record(env="prod"), env="prod", equity=1000.0)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(fake.writes(), [])
        self.assertEqual(len(res["warnings"]), 1)

    def test_equity_read_once_per_run(self):
        fake = FakeExchange([], algos=[entry_algo(), entry_algo(algo_id=7002, symbol="ETHUSDT")])
        res, _, mock_eq = run_protect(fake, make_record(env="prod"),
                                      make_record(env="prod", entry_id="7002", symbol="ETHUSDT"),
                                      env="prod", equity=20000.0)
        self.assertTrue(res["ok"], res["errors"])
        mock_eq.assert_called_once()

    def test_testnet_skips_the_check(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws, mock_eq = run_protect(fake, make_record(), env="testnet", equity=1.0)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["actions"], [])
        mock_eq.assert_not_called()
        self.assertNotIn("warnings", res)

    def test_pure_helper_matches_gate_2(self):
        rec = make_record(env="prod")
        self.assertIsNone(eft.record_loss_cap_problem(rec, 101.0, 11520.0, PROD_PROFILE))       # cap 72.0
        self.assertIsNotNone(eft.record_loss_cap_problem(rec, 101.0, 11519.0, PROD_PROFILE))
        cap, _, _, _ = eft.monetary_loss_cap(11520.0, PROD_PROFILE, is_testnet=False, is_yolo=False, ref=101.0,
                                             total_qty=12.0, leverage=3)
        self.assertAlmostEqual(cap, 72.0)


class TestKeptStopCoverageUntrusted(unittest.TestCase):
    """Issue #118: an untrusted record keeps the existing stop only if it covers the whole position."""

    def record(self):
        return make_record(sl=105.0)   # SL above entry for a LONG: untrusted (filled_with_invalid_sl)

    def test_short_quantity_stop_replaced_on_keys(self):
        short = dict(stop(601, 98.4, close_position=False), reduceOnly=True, quantity="4")
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[short])
        res, ws, _ = run_protect(fake, self.record())
        self.assertEqual(types(res), ["pending_record_mismatch"])
        rep = res["actions"][0]["detail"]["coverage_replace"]
        self.assertTrue(rep["success"])
        sent = posts(fake, ALGO_ENDPOINT)
        self.assertEqual([(s["triggerPrice"], s["quantity"], s["reduceOnly"]) for s in sent], [(98.4, "10", "true")])
        self.assertLess(fake.write_index("POST", ALGO_ENDPOINT)[0], fake.write_index("DELETE", ALGO_ENDPOINT)[0])
        self.assertEqual([(float(a["triggerPrice"]), a["quantity"]) for a in fake.algos], [(98.4, "10")])
        self.assertEqual(read_registry(ws), {})

    def test_covering_stop_kept_on_keys(self):
        for kept in (stop(601, 98.4), dict(stop(601, 98.4, close_position=False), reduceOnly=True, quantity="10")):
            fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[kept])
            res, _, _ = run_protect(fake, self.record())
            self.assertNotIn("coverage_replace", res["actions"][0]["detail"])
            self.assertEqual(fake.writes(), [])

    def test_mcp_always_places_a_covering_stop(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(601, 98.4)])
        res, _, _ = run_protect(fake, self.record(), mcp=True)
        self.assertTrue(res["actions"][0]["detail"]["coverage_replace"]["success"])
        self.assertEqual([(s["triggerPrice"], s["quantity"]) for s in posts(fake, ALGO_ENDPOINT)], [(98.4, "10")])

    def test_unverified_replacement_keeps_the_existing_stop(self):
        short = dict(stop(601, 98.4, close_position=False), reduceOnly=True, quantity="4")
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[short], index_new_stops=False)
        res, _, _ = run_protect(fake, self.record())
        self.assertIn("record_mismatch_coverage", [e["stage"] for e in res["errors"]])
        self.assertEqual(deletes(fake), [])
        self.assertEqual([a["algoId"] for a in fake.algos], [601])


if __name__ == "__main__":
    unittest.main()
