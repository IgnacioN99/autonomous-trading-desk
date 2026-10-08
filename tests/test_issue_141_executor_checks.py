#!/usr/bin/env python3
"""
test_issue_141_executor_checks.py - Issue #141 (executor side).

- Item 2: on a fresh entry, a TP2 that rounds onto or past the effective entry aborts before any write, like SL/TP1.
- Item 3: when one stepSize bump still leaves the entry below minNotional, the executor rejects locally (no entry
  order, no algo order) instead of letting Binance answer -4164.
- Item 5: split_take_profit_quantities with a SHORT reference price (TP1 below the entry).

Testnet, fake exchange (send_signed_request), urllib blocked, temp workspace: no network, no orders, no logs/.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execute_futures_trade as eft  # noqa: E402

FILTERS = {"stepSize": 0.001, "minQty": 0.001, "tickSize": 0.01, "precision_qty": 3, "precision_price": 2,
           "minNotional": 5.0}
PROFILE = {"yolo_slot_enabled": True, "leverage_standard": 3, "leverage_yolo": 15, "max_open_positions": 100,
           "risk_pct_equity": 0.005, "max_margin_ratio": 0.30}
ALGO_ENDPOINT = "/fapi/v1/" + "algoOrder"


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def _execute(**kwargs):
    calls = []
    placed_stops = []

    def place_stop(symbol, exit_side, sl_price, target_env=None, quantity=None):
        placed_stops.append({"algoId": 9, "symbol": symbol, "side": exit_side, "orderType": "STOP_MARKET",
                             "triggerPrice": str(sl_price), "closePosition": quantity is None,
                             "reduceOnly": quantity is not None})
        return {"algoId": 9}

    def fake(method, endpoint, params=None, target_env=None):
        calls.append((method, endpoint, dict(params or {})))
        if endpoint == "/fapi/v1/openAlgoOrders":
            return [dict(s) for s in placed_stops]
        if endpoint == "/fapi/v1/marginType":
            return {"code": 200, "msg": "success"}
        if endpoint == "/fapi/v1/leverage":
            return {"symbol": params["symbol"], "leverage": params["leverage"]}
        if endpoint == "/fapi/v1/leverageBracket":
            return {"error": "unavailable"}
        if endpoint == "/fapi/v1/ticker/price":
            return {"price": "100.0"}
        if endpoint == "/fapi/v1/order" and method == "POST":
            status = "NEW" if params.get("type") == "LIMIT" else "FILLED"
            return {"orderId": 7, "avgPrice": "100.0", "status": status}
        if endpoint == ALGO_ENDPOINT and method == "POST":
            return {"algoId": 8}
        return {}
    args = dict(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                sl_price=97.0, tp1_price=110.0, tp2_price=120.0, target_env="testnet", bypass_eval_gate=True)
    args.update(kwargs)
    gates = MagicMock(wraps=eft.check_mechanical_gates)
    ws = tempfile.mkdtemp()
    with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
         patch("execute_futures_trade._workspace_dir", return_value=ws), \
         patch("execute_futures_trade.check_mechanical_gates", gates), \
         patch("execute_futures_trade.get_symbol_filters", return_value=FILTERS), \
         patch("execute_futures_trade.place_algo_stop_loss", side_effect=place_stop) as stop_mock, \
         patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 9})), \
         patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
         patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
         patch("builtins.print"), \
         patch("utils.atomic_writer.atomic_append_jsonl"), \
         patch("provenance_stamp.stamp_trade_record", side_effect=lambda rec, **kw: rec):
        res = eft.execute_complete_trade(**args)
    return res, calls, gates, stop_mock


def _writes(calls):
    return [c for c in calls if c[0] in ("POST", "DELETE", "PUT")]


def _order_writes(calls):
    return [c for c in _writes(calls) if c[1] in ("/fapi/v1/order", ALGO_ENDPOINT)]


class TestTp2WrongSideAborts(unittest.TestCase):

    def _assert_aborted_before_any_write(self, res, calls, gates, needle):
        self.assertFalse(res["success"])
        self.assertIn(needle, res["error"])
        self.assertIn("fail-closed", res["error"])
        self.assertEqual(_writes(calls), [])   # no leverage/margin setup, no order
        gates.assert_not_called()

    def test_long_tp2_rounding_onto_entry_aborts(self):
        res, calls, gates, _ = _execute(tp2_price=100.004)
        self._assert_aborted_before_any_write(res, calls, gates, "Rounded TP2 100.0")

    def test_long_tp2_below_entry_aborts(self):
        res, calls, gates, _ = _execute(tp2_price=99.0)
        self._assert_aborted_before_any_write(res, calls, gates, "Rounded TP2 99.0")

    def test_short_tp2_above_entry_aborts(self):
        res, calls, gates, _ = _execute(direction="SHORT", sl_price=103.0, tp1_price=90.0, tp2_price=101.0)
        self._assert_aborted_before_any_write(res, calls, gates, "Rounded TP2 101.0")

    def test_valid_tp2_still_executes(self):
        res, calls, _, _ = _execute(direction="SHORT", sl_price=103.0, tp1_price=90.0, tp2_price=80.0)
        self.assertTrue(res["success"], res.get("error"))


class TestLocalMinNotionalReject(unittest.TestCase):

    def test_below_min_notional_after_bump_rejects_locally(self):
        # 1.0 x 3 / 100 -> 0.03; one bump -> 0.031 x 100 = 3.1 USDT, still below 5
        res, calls, gates, stop_mock = _execute(margin_usdt=1.0)
        self.assertFalse(res["success"])
        self.assertIn("minNotional", res["error"])
        self.assertIn("fail-closed", res["error"])
        self.assertEqual(_order_writes(calls), [])   # no entry order and no algo order (setup calls may happen)
        stop_mock.assert_not_called()
        gates.assert_not_called()

    def test_conditional_entry_below_min_notional_rejects_locally(self):
        res, calls, _, stop_mock = _execute(order_type="STOP_MARKET", trigger_price=102.347, margin_usdt=1.0)
        self.assertFalse(res["success"])
        self.assertIn("minNotional", res["error"])
        self.assertEqual(_order_writes(calls), [])
        stop_mock.assert_not_called()

    def test_bump_that_reaches_min_notional_still_executes(self):
        # 1.65 x 3 / 100 -> 0.049 (4.9 USDT); one bump -> 0.05 = 5 USDT
        res, calls, _, _ = _execute(margin_usdt=1.65)
        self.assertTrue(res["success"], res.get("error"))
        entry = [c[2] for c in calls if c[1] == "/fapi/v1/order" and not c[2].get("reduceOnly")]
        self.assertEqual(float(entry[0]["quantity"]), 0.05)


class TestSplitTakeProfitQuantitiesShort(unittest.TestCase):

    def test_short_tp1_below_entry_grows_the_bump(self):
        # SHORT entry ~100, TP1 at 90: 30% of 0.1 = 0.03 x 90 = 2.7 USDT < 5, so the TP1 leg is bumped to
        # ceil(5 / 90 / 0.001) = 56 steps. For small SHORTs the TP1 leg therefore exceeds 30% (here 56%): the lower
        # the TP1 price, the more quantity minNotional needs.
        tp1_qty, tp2_qty = eft.split_take_profit_quantities(0.1, FILTERS, 90.0)
        self.assertEqual(tp1_qty, 0.056)
        self.assertEqual(tp2_qty, 0.044)
        self.assertGreaterEqual(tp1_qty * 90.0, 5.0)
        self.assertGreater(tp1_qty / 0.1, 0.30)
        long_tp1 = eft.split_take_profit_quantities(0.1, FILTERS, 110.0)[0]
        self.assertGreater(tp1_qty, long_tp1)   # same size, LONG TP1 above the entry needs less

    def test_large_short_keeps_30_70(self):
        self.assertEqual(eft.split_take_profit_quantities(1.0, FILTERS, 90.0), (0.3, 0.7))


if __name__ == "__main__":
    unittest.main()
