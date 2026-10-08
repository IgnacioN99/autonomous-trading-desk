#!/usr/bin/env python3
"""
test_exit_management.py - Offline tests for protective stop management (no network, no orders).

1. move_sl_to_breakeven: PLACE-THEN-CANCEL ordering, unverified new stop keeps the old one and cancels nothing,
   YOLO-before-TP1 refusal, anti-truncation (2x ATR_15m) refusal, force override, never loosens.
2. dynamic_exit_manager.update_position_to_structural_stop: never loosens, place-then-cancel, unverified -> failure,
   dry run sends no writes, env resolved via utils.env_resolver (no hardcoded testnet default).
3. Executor CLI: --positions / --move-breakeven exit codes and JSON shape.
"""

import io
import os
import sys
import json
import shutil
import tempfile
import unittest
import contextlib
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import execute_futures_trade as eft
import dynamic_exit_manager as dem

FILTERS = {"stepSize": 0.001, "minQty": 0.001, "tickSize": 0.1, "precision_qty": 3, "precision_price": 1, "minNotional": 5.0}
PROFILE = {"leverage_standard": 3, "leverage_yolo": 15, "leverage_ceiling": 15}
ALGO_ENDPOINT = "/fapi/v1/" + "algoOrder"


class FakeExchange:
    """Stateful in-memory stand-in for send_signed_request. Records every call."""

    def __init__(self, positions, algos=None, open_orders=None, index_new_stops=True, reject_new_stops=False,
                 fail_cancel=False, reject_response=None):
        self.positions = [dict(p) for p in positions]
        self.algos = [dict(a) for a in (algos or [])]
        self.open_orders = [dict(o) for o in (open_orders or [])]
        self.index_new_stops = index_new_stops
        self.reject_new_stops = reject_new_stops
        self.reject_response = reject_response or {"code": -2021, "msg": "Order would immediately trigger."}
        self.fail_cancel = fail_cancel
        self.calls = []
        self.next_id = 9000

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        self.calls.append((method, endpoint, params))
        sym = params.get("symbol")
        if endpoint == "/fapi/v2/positionRisk":
            return [dict(p) for p in self.positions if not sym or p["symbol"] == sym]
        if endpoint == "/fapi/v1/openAlgoOrders":
            return [dict(a) for a in self.algos if not sym or a["symbol"] == sym]
        if endpoint == "/fapi/v1/openOrders":
            return [dict(o) for o in self.open_orders if not sym or o["symbol"] == sym]
        if endpoint == ALGO_ENDPOINT and method == "POST":
            if self.reject_new_stops:
                return dict(self.reject_response)
            self.next_id += 1
            order = {
                "algoId": self.next_id, "symbol": sym, "side": params.get("side"), "orderType": params.get("type"),
                "triggerPrice": str(params.get("triggerPrice")), "quantity": params.get("quantity"),
                "reduceOnly": params.get("reduceOnly") == "true", "closePosition": params.get("closePosition") == "true",
            }
            if self.index_new_stops:
                self.algos.append(order)
            return {"algoId": self.next_id}
        if endpoint == ALGO_ENDPOINT and method == "DELETE":
            if self.fail_cancel:
                return {"code": -2011, "msg": "Unknown order sent."}
            self.algos = [a for a in self.algos if str(a["algoId"]) != str(params.get("algoId"))]
            return {"algoId": params.get("algoId"), "code": "200", "msg": "success"}
        if endpoint == "/fapi/v1/order" and method == "POST":
            return {"orderId": 1, "status": "FILLED"}
        if endpoint == "/fapi/v1/order" and method == "DELETE":
            self.open_orders = [o for o in self.open_orders if str(o.get("orderId")) != str(params.get("orderId"))]
            return {"orderId": params.get("orderId"), "status": "CANCELED"}
        if endpoint == "/fapi/v1/allOpenOrders":
            return []
        return {}

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "DELETE", "PUT")]

    def write_index(self, method, endpoint):
        return [i for i, c in enumerate(self.calls) if c[0] == method and c[1] == endpoint]


def long_position(symbol="BTCUSDT", amt="10", entry="100.0", mark="110.0", leverage="3"):
    return {"symbol": symbol, "positionAmt": amt, "entryPrice": entry, "markPrice": mark, "leverage": leverage,
            "unRealizedProfit": "100.0", "isolatedMargin": "333.0", "liquidationPrice": "67.5", "marginType": "isolated"}


def stop(algo_id, trigger, side="SELL", symbol="BTCUSDT", close_position=True):
    return {"algoId": algo_id, "symbol": symbol, "side": side, "orderType": "STOP_MARKET",
            "triggerPrice": str(trigger), "closePosition": close_position, "reduceOnly": False}


@contextlib.contextmanager
def offline(fake, workspace=None, profile=None):
    """Patches every exchange / filesystem touchpoint used by the exit manager. A temp workspace created here (no
    workspace given) is removed when the block exits."""
    own = None if workspace else tempfile.mkdtemp()
    ws = workspace or own
    try:
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("execute_futures_trade.get_symbol_filters", return_value=dict(FILTERS)), \
             patch("execute_futures_trade.subprocess.run", side_effect=FileNotFoundError("binance-cli")), \
             patch("execute_futures_trade.time.sleep", return_value=None), \
             patch("execute_futures_trade._workspace_dir", return_value=ws), \
             patch("user_profile.load_user_profile", return_value=dict(profile or PROFILE)), \
             patch("utils.trade_excursion.fetch_klines_range", return_value=[]):  # guardian excursion tracking (#182)
            yield ws
    finally:
        if own:
            shutil.rmtree(own, ignore_errors=True)


def write_audit(ws, **record):
    logs = os.path.join(ws, "logs")
    os.makedirs(logs, exist_ok=True)
    with open(os.path.join(logs, "trades_audit.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


class TestBreakEvenPlaceThenCancel(unittest.TestCase):

    def test_new_stop_placed_and_verified_before_old_cancelled(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        with offline(fake), patch("execute_futures_trade.get_atr_15m", return_value=2.0):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")

        self.assertTrue(res["success"], res)
        self.assertEqual(res["reason"], "moved")
        self.assertFalse(res["refused"])
        self.assertEqual(res["old_stop"]["trigger_price"], 95.0)
        self.assertEqual(res["new_stop"]["trigger_price"], 100.2)  # True Net BE = entry * 1.002
        self.assertEqual(res["cancelled_old_stop_ids"], [501])

        posts = fake.write_index("POST", ALGO_ENDPOINT)
        deletes = fake.write_index("DELETE", ALGO_ENDPOINT)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(deletes), 1)
        self.assertLess(posts[0], deletes[0], "the new stop must be placed before any cancel")
        # A verification read happens between placement and cancel
        reads_between = [c for c in fake.calls[posts[0]:deletes[0]] if c[1] == "/fapi/v1/openAlgoOrders"]
        self.assertTrue(reads_between)
        # Replacement stop is reduce-only and quantity-based (no closePosition conflict with the old stop)
        sent = fake.calls[posts[0]][2]
        self.assertEqual(sent["reduceOnly"], "true")
        self.assertEqual(sent["quantity"], "10")
        self.assertNotIn("closePosition", sent)
        self.assertEqual(sent["side"], "SELL")
        # Exactly one protective stop remains, at break-even
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [100.2])

    def test_unverified_new_stop_keeps_old_and_cancels_nothing(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)], index_new_stops=False)
        with offline(fake), patch("execute_futures_trade.get_atr_15m", return_value=2.0):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")

        self.assertFalse(res["success"])
        self.assertFalse(res["refused"])
        self.assertEqual(res["reason"], "new_stop_unverified")
        self.assertIsNone(res["new_stop"])
        self.assertEqual(res["old_stop"]["algo_id"], 501)
        self.assertEqual(fake.write_index("DELETE", ALGO_ENDPOINT), [])
        self.assertIn(501, [a["algoId"] for a in fake.algos])
        self.assertIn("error", res)

    def test_rejected_new_stop_keeps_old_and_cancels_nothing(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)], reject_new_stops=True)
        with offline(fake), patch("execute_futures_trade.get_atr_15m", return_value=2.0):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertFalse(res["success"])
        self.assertEqual(res["reason"], "new_stop_unverified")
        self.assertEqual(fake.write_index("DELETE", ALGO_ENDPOINT), [])
        self.assertEqual([a["algoId"] for a in fake.algos], [501])

    def test_old_stop_is_not_mistaken_for_new_stop(self):
        # Old stop sits within the legacy 3% verification tolerance of break-even; it must not count as verified.
        fake = FakeExchange([long_position()], algos=[stop(501, 99.0)], index_new_stops=False)
        with offline(fake), patch("execute_futures_trade.get_atr_15m", return_value=2.0):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertFalse(res["success"])
        self.assertEqual(fake.write_index("DELETE", ALGO_ENDPOINT), [])

    def test_short_position_moves_down_to_breakeven(self):
        pos = long_position(amt="-5", entry="100.0", mark="90.0")
        fake = FakeExchange([pos], algos=[stop(601, 105.0, side="BUY")])
        with offline(fake), patch("execute_futures_trade.get_atr_15m", return_value=2.0):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["direction"], "SHORT")
        self.assertEqual(res["new_stop"]["trigger_price"], 99.8)
        self.assertEqual(res["new_stop"]["side"], "BUY")
        self.assertLess(fake.write_index("POST", ALGO_ENDPOINT)[0], fake.write_index("DELETE", ALGO_ENDPOINT)[0])

    def test_forced_breakeven_places_stop_at_true_net_buffer(self):
        # Long position: entry 100.0 -> True Net BE is 100.2 (+0.2% fee buffer)
        fake_long = FakeExchange([long_position(entry="100.0", mark="105.0")], algos=[stop(501, 95.0)])
        with offline(fake_long), patch("execute_futures_trade.get_atr_15m", return_value=2.0):
            res_long = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", force=True)
        self.assertTrue(res_long["success"], res_long)
        self.assertEqual(res_long["direction"], "LONG")
        self.assertEqual(res_long["new_stop"]["trigger_price"], 100.2)

        # Short position: entry 100.0 -> True Net BE is 99.8 (-0.2% fee buffer)
        pos_short = long_position(amt="-5", entry="100.0", mark="90.0")
        fake_short = FakeExchange([pos_short], algos=[stop(601, 105.0, side="BUY")])
        with offline(fake_short), patch("execute_futures_trade.get_atr_15m", return_value=2.0):
            res_short = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", force=True)
        self.assertTrue(res_short["success"], res_short)
        self.assertEqual(res_short["direction"], "SHORT")
        self.assertEqual(res_short["new_stop"]["trigger_price"], 99.8)

    def test_already_at_breakeven_is_never_loosened(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 103.0)])
        with offline(fake):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", force=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["reason"], "already_at_breakeven")
        self.assertEqual(fake.writes(), [])

    def test_stop_that_would_trigger_immediately_is_refused_even_with_force(self):
        fake = FakeExchange([long_position(mark="100.1")], algos=[stop(501, 95.0)])
        with offline(fake):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", force=True)
        self.assertFalse(res["success"])
        self.assertTrue(res["refused"])
        self.assertEqual(res["reason"], "breakeven_would_trigger_immediately")
        self.assertEqual(fake.writes(), [])

    def test_no_position(self):
        fake = FakeExchange([])
        with offline(fake):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertFalse(res["success"])
        self.assertEqual(res["reason"], "no_position")
        self.assertEqual(fake.writes(), [])

    def test_unreadable_stops_fail_closed_without_writes(self):
        fake = FakeExchange([long_position()])
        orig = fake.__call__

        def broken(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v1/openAlgoOrders":
                return {"error": "timeout"}
            return orig(method, endpoint, params, target_env)
        with offline(broken):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", force=True)
        self.assertFalse(res["success"])
        self.assertEqual(res["reason"], "orders_query_failed")
        self.assertEqual(fake.writes(), [])


class TestBreakEvenRules(unittest.TestCase):

    def test_yolo_before_tp1_refused_via_trade_audit(self):
        fake = FakeExchange([long_position(leverage="3")], algos=[stop(501, 95.0)])
        with offline(fake) as ws:
            write_audit(ws, symbol="BTCUSDT", total_qty=10, tp1_qty=3, is_yolo=True)
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertFalse(res["success"])
        self.assertTrue(res["refused"])
        self.assertEqual(res["reason"], "yolo_tp1_not_filled")
        self.assertEqual(res["rules"]["yolo_source"], "trade_audit")
        self.assertFalse(res["rules"]["tp1_filled"])
        self.assertEqual(fake.writes(), [])

    def test_yolo_before_tp1_refused_via_dossier_candidate(self):
        from utils.dossier_provenance import default_dossier_path
        fake = FakeExchange([long_position(leverage="3")], algos=[stop(501, 95.0)])
        with offline(fake) as ws:
            path = default_dossier_path(ws)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"approved_candidates": [{"symbol": "BTCUSDT", "direction": "LONG", "is_yolo": True}]}, f)
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertTrue(res["refused"])
        self.assertEqual(res["rules"]["yolo_source"], "dossier_candidate")
        self.assertEqual(fake.writes(), [])

    def test_yolo_detected_from_leverage(self):
        fake = FakeExchange([long_position(leverage="15")], algos=[stop(501, 95.0)])
        with offline(fake):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertTrue(res["refused"])
        self.assertEqual(res["reason"], "yolo_tp1_not_filled")
        self.assertEqual(res["rules"]["yolo_source"], "leverage")
        self.assertEqual(fake.writes(), [])

    def test_explicit_yolo_flag_refused(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        with offline(fake):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", is_yolo=True)
        self.assertTrue(res["refused"])
        self.assertEqual(res["rules"]["yolo_source"], "explicit_flag")

    def test_force_overrides_yolo_rule(self):
        fake = FakeExchange([long_position(leverage="15")], algos=[stop(501, 95.0)])
        with offline(fake):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", force=True)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["reason"], "moved")
        self.assertTrue(res["rules"]["force"])
        self.assertLess(fake.write_index("POST", ALGO_ENDPOINT)[0], fake.write_index("DELETE", ALGO_ENDPOINT)[0])

    def test_yolo_after_tp1_fill_allowed(self):
        fake = FakeExchange([long_position(amt="7", leverage="15")], algos=[stop(501, 95.0)])
        with offline(fake) as ws:
            write_audit(ws, symbol="BTCUSDT", total_qty=10, tp1_qty=3, is_yolo=True)
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"], res)
        self.assertTrue(res["rules"]["is_yolo"])
        self.assertTrue(res["rules"]["tp1_filled"])

    def test_standard_insufficient_expansion_refused(self):
        fake = FakeExchange([long_position(mark="105.0")], algos=[stop(501, 95.0)])
        with offline(fake), patch("execute_futures_trade.get_atr_15m", return_value=5.0):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertTrue(res["refused"])
        self.assertEqual(res["reason"], "insufficient_expansion")
        self.assertEqual(res["rules"]["expansion_atr_multiple"], 1.0)
        self.assertEqual(fake.writes(), [])

    def test_standard_unknown_atr_refused(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        with offline(fake), patch("execute_futures_trade.get_atr_15m", return_value=None):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertTrue(res["refused"])
        self.assertEqual(fake.writes(), [])

    def test_standard_after_tp1_fill_allowed_without_expansion(self):
        fake = FakeExchange([long_position(amt="7", mark="101.0")], algos=[stop(501, 95.0)])
        with offline(fake) as ws, patch("execute_futures_trade.get_atr_15m", return_value=50.0):
            write_audit(ws, symbol="BTCUSDT", total_qty=10, tp1_qty=3, is_yolo=False)
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"], res)
        self.assertTrue(res["rules"]["tp1_filled"])

    def test_dry_run_sends_no_writes(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        with offline(fake):
            res = eft.move_sl_to_breakeven("BTCUSDT", target_env="testnet", force=True, dry_run=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["reason"], "dry_run")
        self.assertEqual(fake.writes(), [])


def structural(new_sl, cur_price=110.0):
    return {"new_structural_sl": new_sl, "current_price": cur_price, "is_profit_locked": new_sl > 100.0,
            "locked_roe_pct": round((new_sl - 100.0), 2), "should_update": True}


class TestStructuralTrailing(unittest.TestCase):

    def test_never_loosens(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 104.0)])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"])
        self.assertFalse(res["updated"])
        self.assertEqual(res["reason"], "not_tighter")
        self.assertEqual(fake.writes(), [])

    def test_never_loosens_short(self):
        pos = long_position(amt="-5", entry="100.0", mark="90.0")
        fake = FakeExchange([pos], algos=[stop(601, 95.0, side="BUY")])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(97.0, 90.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"])
        self.assertFalse(res["updated"])
        self.assertEqual(fake.writes(), [])

    def test_tightens_place_then_cancel(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"], res)
        self.assertTrue(res["updated"])
        self.assertEqual(res["new_sl"], 102.0)
        posts = fake.write_index("POST", ALGO_ENDPOINT)
        deletes = fake.write_index("DELETE", ALGO_ENDPOINT)
        self.assertLess(posts[0], deletes[0])
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [102.0])

    def test_unverified_without_previous_stop_is_failure(self):
        fake = FakeExchange([long_position()], algos=[], index_new_stops=False)
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertFalse(res["success"])
        self.assertEqual(res["reason"], "new_stop_unverified")
        self.assertEqual(fake.write_index("DELETE", ALGO_ENDPOINT), [])

    def test_unverified_with_previous_stop_keeps_it(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)], index_new_stops=False)
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertFalse(res["success"])
        self.assertEqual(fake.write_index("DELETE", ALGO_ENDPOINT), [])
        self.assertEqual([a["algoId"] for a in fake.algos], [501])

    def test_no_stop_and_no_structural_level_is_failure(self):
        fake = FakeExchange([long_position()], algos=[])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=None):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertFalse(res["success"])
        self.assertEqual(fake.writes(), [])

    def test_stop_on_wrong_side_of_price_not_placed(self):
        fake = FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertTrue(res["success"])
        self.assertEqual(res["reason"], "stop_would_trigger_immediately")
        self.assertEqual(fake.writes(), [])

    def test_dry_run_sends_no_writes(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        with offline(fake), patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet", dry_run=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["planned_sl"], 102.0)
        self.assertEqual(fake.writes(), [])

    def test_env_defaults_resolved_not_hardcoded(self):
        import inspect
        for fn in (dem.calculate_structural_stop, dem.update_position_to_structural_stop,
                   dem.audit_and_trail_all_positions, dem.check_dead_alpha_timeout):
            self.assertIsNone(inspect.signature(fn).parameters["target_env"].default, fn.__name__)
        fake = FakeExchange([long_position()], algos=[stop(501, 104.0)])
        with offline(fake), patch.dict(os.environ, {"BINANCE_API_ENV": "prod"}), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=structural(102.0)):
            res = dem.update_position_to_structural_stop("BTCUSDT")
        self.assertEqual(res["env"], "prod")


def run_cli(argv):
    out = io.StringIO()
    with patch.object(sys, "argv", ["execute_futures_trade.py"] + argv), patch("sys.exit") as mock_exit, \
         contextlib.redirect_stdout(out):
        eft.main()
    code = mock_exit.call_args[0][0] if mock_exit.call_args else None
    return code, out.getvalue(), mock_exit


class TestExecutorCLI(unittest.TestCase):

    def test_positions_json_shape_and_protection(self):
        positions = [long_position("BTCUSDT"), long_position("ETHUSDT", amt="-2", entry="2000", mark="1990")]
        algos = [stop(501, 95.0)]
        orders = [{"orderId": 77, "symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "price": "120.0",
                   "origQty": "3", "reduceOnly": True}]
        fake = FakeExchange(positions, algos=algos, open_orders=orders)
        with offline(fake):
            code, out, _ = run_cli(["--positions", "--json", "--env", "testnet"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        for key in ("success", "env", "timestamp", "count", "all_protected", "error", "positions"):
            self.assertIn(key, data)
        self.assertTrue(data["success"])
        self.assertEqual(data["env"], "testnet")
        self.assertEqual(data["count"], 2)
        self.assertFalse(data["all_protected"])
        btc, eth = data["positions"]
        for key in ("symbol", "side", "size", "entry_price", "mark_price", "leverage", "unrealized_pnl",
                    "liquidation_price", "protected", "stop_orders", "take_profit_orders", "orders_error"):
            self.assertIn(key, btc)
        self.assertTrue(btc["protected"])
        self.assertEqual(btc["stop_orders"][0]["trigger_price"], 95.0)
        self.assertEqual(btc["take_profit_orders"][0]["price"], 120.0)
        self.assertEqual(eth["side"], "SHORT")
        self.assertFalse(eth["protected"])
        self.assertEqual(fake.writes(), [], "--positions must be read-only")

    def test_positions_api_error_exit_1(self):
        with patch("execute_futures_trade.send_signed_request", return_value={"code": -1003, "msg": "Too many requests"}):
            code, out, _ = run_cli(["--positions", "--json", "--env", "testnet"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data["success"])
        self.assertTrue(data["error"])

    def test_positions_text_output(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        with offline(fake):
            code, out, _ = run_cli(["--positions", "--env", "testnet"])
        self.assertEqual(code, 0)
        self.assertIn("BTCUSDT LONG", out)

    def _move(self, result, extra=()):
        with patch("execute_futures_trade.move_sl_to_breakeven", return_value=result) as mock_be:
            code, out, _ = run_cli(["--move-breakeven", "--symbol", "btcusdt", "--env", "testnet", "--json", *extra])
        return code, out, mock_be

    def test_move_breakeven_success_exit_0(self):
        code, out, mock_be = self._move({"success": True, "refused": False, "reason": "moved", "message": "ok",
                                         "old_stop": None, "new_stop": None})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["reason"], "moved")
        mock_be.assert_called_once_with("BTCUSDT", target_env="testnet", force=False, is_yolo=None)

    def test_move_breakeven_refused_exit_2(self):
        code, _, _ = self._move({"success": False, "refused": True, "reason": "yolo_tp1_not_filled", "message": "no"})
        self.assertEqual(code, 2)

    def test_move_breakeven_failure_exit_1(self):
        code, _, _ = self._move({"success": False, "refused": False, "reason": "new_stop_unverified", "message": "x"})
        self.assertEqual(code, 1)

    def test_move_breakeven_force_and_yolo_flags_forwarded(self):
        _, _, mock_be = self._move({"success": True, "refused": False, "reason": "moved", "message": "ok"},
                                   extra=("--force", "--is-yolo"))
        mock_be.assert_called_once_with("BTCUSDT", target_env="testnet", force=True, is_yolo=True)

    def test_move_breakeven_requires_symbol(self):
        with patch("execute_futures_trade.move_sl_to_breakeven") as mock_be:
            code, _, _ = run_cli(["--move-breakeven", "--env", "testnet"])
        self.assertEqual(code, 1)
        mock_be.assert_not_called()

    def test_move_breakeven_cannot_be_combined_with_trade_flags(self):
        with patch("execute_futures_trade.move_sl_to_breakeven") as mock_be, \
             patch("execute_futures_trade.execute_complete_trade") as mock_trade:
            code, _, _ = run_cli(["--move-breakeven", "--symbol", "BTCUSDT", "--direction", "LONG", "--env", "testnet"])
        self.assertEqual(code, 1)
        mock_be.assert_not_called()
        mock_trade.assert_not_called()

    def test_move_breakeven_end_to_end_refusal_exit_2(self):
        fake = FakeExchange([long_position(leverage="15")], algos=[stop(501, 95.0)])
        with offline(fake):
            code, out, _ = run_cli(["--move-breakeven", "--symbol", "BTCUSDT", "--env", "testnet", "--json"])
        self.assertEqual(code, 2)
        data = json.loads(out)
        for key in ("success", "refused", "reason", "message", "symbol", "env", "old_stop", "new_stop", "rules"):
            self.assertIn(key, data)
        self.assertEqual(fake.writes(), [])


if __name__ == "__main__":
    unittest.main()
