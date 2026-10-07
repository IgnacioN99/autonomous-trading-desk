#!/usr/bin/env python3
"""
test_execute_futures_hardening.py - Comprehensive Unit Tests for Issue #9 Hardening:
1. CLI Argument Parsing and Dispatching (Finding 2)
2. verify_algo_stop_loss Price Match vs Mismatch (Finding 5)
3. Hard Leverage Ceiling Gates (Finding 8)
4. Dynamic Equity Risk Gate (Finding 13)
5. Atomic Auto-Destruct & Post-Entry Guarantees (Findings 4 & 10)
6. Resting LIMIT Entry Protection (Finding 9)
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock, mock_open

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import execute_futures_trade as eft
import user_profile as up


def isolate_workspace(tc, profile=None, gate1_state=False):
    """
    Points the executor's logs/ (session_state.json, pending_entries.json, audit trails) at a temp workspace for the
    duration of the test, so it never reads or writes the real logs/ (the desk machine trades live). profile: explicit
    user profile instead of the operator's config/user_profile.json. gate1_state: also redirect the module __file__,
    from which GATE 1 (delta, PROD) resolves logs/session_state.json. Returns the temp workspace path.
    """
    tmp = tempfile.TemporaryDirectory()
    tc.addCleanup(tmp.cleanup)
    ws = tmp.name
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    patches = [patch("execute_futures_trade._workspace_dir", return_value=ws)]
    if gate1_state:
        patches.append(patch.object(eft, "__file__", os.path.join(ws, "scripts", "execute_futures_trade.py")))
    if profile is not None:
        patches.append(patch("user_profile.load_user_profile", return_value=dict(profile)))
    for p in patches:
        p.start()
        tc.addCleanup(p.stop)
    return ws


class TestVerifyAlgoStopLoss(unittest.TestCase):
    """[Finding 5] Fix verify_algo_stop_loss logic bug (price match vs mismatch)."""

    def setUp(self):
        self.mock_algos = [
            {
                "algoId": 1001,
                "orderType": "STOP_MARKET",
                "side": "SELL",
                "triggerPrice": 95000.0,
                "closePosition": True
            }
        ]

    @patch("execute_futures_trade.send_signed_request")
    def test_price_match_within_tolerance(self, mock_send):
        mock_send.return_value = self.mock_algos
        # 95100 is within 3% of 95000 (diff is ~0.1%)
        matched, order = eft.verify_algo_stop_loss("BTCUSDT", "SELL", sl_price=95100.0, target_env="testnet")
        self.assertTrue(matched)
        self.assertIsNotNone(order)
        self.assertEqual(order["algoId"], 1001)

    @patch("execute_futures_trade.send_signed_request")
    def test_price_mismatch_fails_closed(self, mock_send):
        mock_send.return_value = self.mock_algos
        # 85000 is ~10.5% away from 95000 (exceeds 3% tolerance)
        matched, order = eft.verify_algo_stop_loss("BTCUSDT", "SELL", sl_price=85000.0, target_env="testnet")
        # Critical verification of Finding 5: must NOT return True!
        self.assertFalse(matched)
        self.assertIsNone(order)

    @patch("execute_futures_trade.send_signed_request")
    def test_price_none_matches_any_valid_sl(self, mock_send):
        mock_send.return_value = self.mock_algos
        matched, order = eft.verify_algo_stop_loss("BTCUSDT", "SELL", sl_price=None, target_env="testnet")
        self.assertTrue(matched)
        self.assertEqual(order["algoId"], 1001)

    @patch("execute_futures_trade.send_signed_request")
    def test_wrong_exit_side_not_matched(self, mock_send):
        mock_send.return_value = self.mock_algos
        matched, order = eft.verify_algo_stop_loss("BTCUSDT", "BUY", sl_price=95000.0, target_env="testnet")
        self.assertFalse(matched)
        self.assertIsNone(order)

    @patch("execute_futures_trade.send_signed_request")
    def test_empty_algos_returns_false(self, mock_send):
        mock_send.return_value = []
        matched, order = eft.verify_algo_stop_loss("BTCUSDT", "SELL", sl_price=95000.0, target_env="testnet")
        self.assertFalse(matched)
        self.assertIsNone(order)


class TestLeverageCeilingGates(unittest.TestCase):
    """[Finding 8] Hard Leverage Ceiling Verification."""

    def setUp(self):
        # Gate 0A reads session_state/pending_entries: temp workspace + cold-start profile (tests may override it)
        isolate_workspace(self, profile=up.DEFAULT_PROFILE)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    def test_leverage_exceeds_15x_absolute_ceiling_rejected(self, mock_eq):
        # 16x leverage must be rejected unconditionally, even if is_yolo is True
        ok, reason = eft.check_mechanical_gates(
            direction="LONG",
            cur_price=100.0,
            sl_price=98.0,
            tp1_price=103.0,
            total_qty=1.0,
            leverage=16,
            target_env="testnet",
            is_yolo=True
        )
        self.assertFalse(ok)
        self.assertIn("15x", reason)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    def test_standard_leverage_above_5x_rejected_without_yolo(self, mock_eq):
        # 10x leverage without YOLO flag must be rejected
        ok, reason = eft.check_mechanical_gates(
            direction="LONG",
            cur_price=100.0,
            sl_price=98.0,
            tp1_price=103.0,
            total_qty=1.0,
            leverage=10,
            target_env="testnet",
            is_yolo=False
        )
        self.assertFalse(ok)
        self.assertIn("exceeds standard limit", reason)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    @patch("user_profile.load_user_profile", return_value={"yolo_slot_enabled": True, "leverage_standard": 3, "max_open_positions": 5})
    def test_yolo_leverage_up_to_15x_allowed(self, mock_prof, mock_eq):
        ok, reason = eft.check_mechanical_gates(
            direction="LONG",
            cur_price=100.0,
            sl_price=98.0,
            tp1_price=103.0,
            total_qty=1.0,
            leverage=15,
            target_env="testnet",
            is_yolo=True
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    @patch("user_profile.load_user_profile", return_value={"leverage_standard": 5, "max_open_positions": 5, "yolo_slot_enabled": True})
    def test_standard_leverage_up_to_5x_allowed(self, mock_prof, mock_eq):
        for lev in [1, 2, 3, 5]:
            ok, reason = eft.check_mechanical_gates(
                direction="LONG",
                cur_price=100.0,
                sl_price=98.0,
                tp1_price=103.0,
                total_qty=1.0,
                leverage=lev,
                target_env="testnet",
                is_yolo=False
            )
            self.assertTrue(ok, f"Failed for leverage {lev}")
            self.assertIsNone(reason)

    def test_invalid_leverage_below_1_rejected(self):
        ok, reason = eft.check_mechanical_gates(
            direction="LONG",
            cur_price=100.0,
            sl_price=98.0,
            tp1_price=103.0,
            total_qty=1.0,
            leverage=0,
            target_env="testnet"
        )
        self.assertFalse(ok)
        self.assertIn("Invalid leverage", reason)


class TestDynamicEquityRiskGate(unittest.TestCase):
    """[Finding 13] Dynamic Equity Risk Gate (Align with User Profile)."""

    @patch("time.time", return_value=1700000000)
    @patch("user_profile.load_user_profile")
    @patch("quant_risk_engine.get_account_equity")
    def test_dynamic_risk_cap_enforced_in_prod(self, mock_equity, mock_profile, mock_time):
        mock_equity.return_value = 1000.0
        # 0.5% risk per trade on $1,000 equity = $5.00 * 1.25 buffer = $6.25 max loss
        mock_profile.return_value = {"risk_pct_equity": 0.005, "leverage_standard": 3}
        valid_state = json.dumps({
            "is_valid": True,
            "last_updated_ts": 1700000000,
            "portfolio_exposure": {"delta_bias": "NEUTRAL"}
        })

        # session_state.json lives in a temp workspace (Gate 0A and the PROD delta gate), never in the real logs/
        ws = isolate_workspace(self, gate1_state=True)
        state_file = os.path.join(ws, "logs", "session_state.json")
        with open(state_file, "w", encoding="utf-8") as f:
            f.write(valid_state)

        # Live PROD gate snapshot (issue #101): a flat exchange consistent with the state above, never a real read
        def flat_exchange(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "GET" and not params and endpoint in ("/fapi/v2/positionRisk", "/fapi/v1/openAlgoOrders",
                                                               "/fapi/v1/openOrders"):
                return []
            raise AssertionError(f"unexpected exchange call {method} {endpoint}")
        for p in (patch("execute_futures_trade.send_signed_request", side_effect=flat_exchange),
                  patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test"))):
            p.start()
            self.addCleanup(p.stop)

        # Loss of $10.00 exceeds $6.25 cap -> rejected
        ok, reason = eft.check_mechanical_gates(
            direction="LONG",
            cur_price=100.0,
            sl_price=90.0, # $10 per unit
            tp1_price=105.0,
            total_qty=1.0, # potential loss = $10.00
            leverage=3,
            target_env="prod",
            is_yolo=False
        )
        self.assertFalse(ok)
        self.assertIn("Monetary risk exceeds allowed cap", reason)

        # Loss of $5.00 is within $6.25 cap -> allowed
        ok_valid, reason_valid = eft.check_mechanical_gates(
            direction="LONG",
            cur_price=100.0,
            sl_price=95.0, # $5 per unit
            tp1_price=105.0,
            total_qty=1.0, # potential loss = $5.00
            leverage=3,
            target_env="prod",
            is_yolo=False
        )
        self.assertTrue(ok_valid)
        self.assertIsNone(reason_valid)


class TestAutoDestructAndFailSafe(unittest.TestCase):
    """[Findings 4 & 10] Fail-Safe Stop Loss Placement & Auto-Destruct Verification."""

    @patch("execute_futures_trade.send_signed_request")
    def test_emergency_abort_market_close_confirmed(self, mock_send):
        def fake_send(method, endpoint, params=None, target_env=None):
            if endpoint == "/fapi/v1/order" and method == "POST":
                return {"orderId": 99001, "status": "FILLED"}
            return {}

        mock_send.side_effect = fake_send
        res = eft.emergency_abort_market_close("BTCUSDT", "SELL", total_qty=0.05, target_env="testnet")
        self.assertTrue(res["success"])
        self.assertTrue(res["confirmed"])
        self.assertEqual(res["symbol"], "BTCUSDT")

    @patch("time.sleep", return_value=None)
    @patch("execute_futures_trade.send_signed_request")
    def test_emergency_abort_retries_and_confirms(self, mock_send, mock_sleep):
        calls = []

        def fake_send(method, endpoint, params=None, target_env=None):
            if endpoint == "/fapi/v1/order" and method == "POST":
                calls.append(1)
                if len(calls) < 2:
                    return {"code": -1001, "msg": "Internal error"}
                return {"orderId": 99002, "status": "FILLED"}
            return {}

        mock_send.side_effect = fake_send
        res = eft.emergency_abort_market_close("ETHUSDT", "BUY", total_qty=1.0, target_env="testnet")
        self.assertTrue(res["success"])
        self.assertTrue(res["confirmed"])
        self.assertEqual(res["retries"], 2)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    @patch("execute_futures_trade.setup_margin_and_leverage")
    @patch("execute_futures_trade.get_symbol_filters")
    @patch("execute_futures_trade.send_signed_request")
    @patch("execute_futures_trade.place_algo_stop_loss")
    @patch("execute_futures_trade.verify_algo_stop_loss")
    @patch("execute_futures_trade.emergency_abort_market_close")
    def test_execute_complete_trade_aborts_when_sl_unconfirmed(
        self, mock_abort, mock_verify_sl, mock_place_sl, mock_send, mock_filters, mock_setup, mock_equity
    ):
        mock_filters.return_value = {
            "stepSize": 0.001, "minQty": 0.001, "tickSize": 0.1,
            "precision_qty": 3, "precision_price": 1, "minNotional": 5.0
        }
        def fake_send(method, endpoint, params=None, target_env=None):
            if endpoint in ['/fapi/v2/balance', '/fapi/v3/balance']:
                return [{'asset': 'USDT', 'balance': '10000.0'}]
            if endpoint == '/fapi/v1/ticker/price':
                return {'price': '100.0'}
            if endpoint == '/fapi/v1/order' and method == 'POST':
                return {'orderId': 50001, 'avgPrice': '100.0', 'status': 'FILLED'}
            return {}

        mock_send.side_effect = fake_send
        mock_place_sl.return_value = {"error": "SL placement error"}
        mock_verify_sl.return_value = (False, None) # SL unconfirmed!
        mock_abort.return_value = {"success": True, "confirmed": True, "order": {"orderId": 50002}}
        isolate_workspace(self, profile=up.DEFAULT_PROFILE)

        res = eft.execute_complete_trade(
            symbol="BTCUSDT",
            direction="LONG",
            leverage=3,
            margin_usdt=100.0,
            sl_price=98.0,
            tp1_price=103.0,
            tp2_price=106.0,
            target_env="testnet",
            bypass_eval_gate=True  # TESTNET-only explicit bypass: this test targets the SL fail-safe
        )

        self.assertFalse(res["success"])
        self.assertTrue(res["emergency_abort"])
        self.assertIn("CRITICAL FAIL-SAFE TRIGGERED", res["error"])
        mock_abort.assert_called_once()


class TestRestingLimitOrders(unittest.TestCase):
    """[Finding 9] Protection against premature reduceOnly orders on resting LIMIT entries."""

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    @patch("execute_futures_trade.setup_margin_and_leverage")
    @patch("execute_futures_trade.get_symbol_filters")
    @patch("execute_futures_trade.send_signed_request")
    def test_resting_limit_order_defers_tp_placement(self, mock_send, mock_filters, mock_setup, mock_equity):
        mock_filters.return_value = {
            "stepSize": 0.001, "minQty": 0.001, "tickSize": 0.1,
            "precision_qty": 3, "precision_price": 1, "minNotional": 5.0
        }
        placed_stops = []   # the issue #36 pre-armed stop, echoed on GET openAlgoOrders

        def fake_send(method, endpoint, params=None, target_env=None):
            if endpoint in ['/fapi/v2/balance', '/fapi/v3/balance']:
                return [{'asset': 'USDT', 'balance': '10000.0'}]
            if endpoint == '/fapi/v1/ticker/price':
                return {'price': '100.0'}
            if endpoint == '/fapi/v1/order' and method == 'POST':
                return {'orderId': 70001, 'status': 'NEW'}
            if endpoint == '/fapi/v1/' + 'algoOrder' and method == 'POST':
                placed_stops.append({'algoId': 70002, 'symbol': params['symbol'], 'side': params['side'],
                                     'orderType': params['type'], 'triggerPrice': str(params['triggerPrice']),
                                     'closePosition': params.get('closePosition') == 'true', 'reduceOnly': False})
                return {'algoId': 70002}
            if endpoint == '/fapi/v1/openAlgoOrders':
                return [dict(s) for s in placed_stops]
            return {}

        mock_send.side_effect = fake_send

        with tempfile.TemporaryDirectory() as ws, patch("execute_futures_trade._workspace_dir", return_value=ws), \
             patch("user_profile.load_user_profile", return_value=dict(up.DEFAULT_PROFILE)):
            res = eft.execute_complete_trade(
                symbol="BTCUSDT",
                direction="LONG",
                leverage=3,
                margin_usdt=100.0,
                sl_price=95.0,
                tp1_price=105.0,
                tp2_price=110.0,
                target_env="testnet",
                order_type="LIMIT",
                limit_price=98.0,
                bypass_eval_gate=True  # TESTNET-only explicit bypass: this test targets resting LIMIT handling
            )

        self.assertTrue(res["success"])
        self.assertTrue(res["pending_limit_entry"])
        self.assertEqual(res["orderId"], 70001)
        self.assertEqual(res["status"], "NEW")
        self.assertIn("deferred until fill", res["message"])
        # Exactly one order dispatched (the LIMIT entry). No premature reduce-only TP orders!
        posted = [c for c in mock_send.call_args_list if c.args[0] == 'POST' and c.args[1] == '/fapi/v1/order']
        self.assertEqual(len(posted), 1)
        self.assertNotIn('reduceOnly', posted[0].args[2])
        # Issue #36: the only algo order is the pre-armed closePosition Stop Loss (never a TP)
        self.assertEqual(res["prearm_status"], "placed")
        self.assertEqual([(s['side'], s['triggerPrice'], s['closePosition']) for s in placed_stops],
                         [('SELL', '95.0', True)])


class TestCLIEntryPoint(unittest.TestCase):
    """[Finding 2] CLI Argument Parsing and Dispatching Verification."""

    @patch("sys.exit")
    @patch("execute_futures_trade.close_position_market")
    def test_cli_close_position(self, mock_close, mock_exit):
        mock_close.return_value = {"success": True, "closed": {"orderId": 123}}
        with patch.object(sys, "argv", ["execute_futures_trade.py", "--symbol", "BTCUSDT", "--close-position", "--env", "testnet"]):
            eft.main()
            mock_close.assert_called_once_with("BTCUSDT", target_env="testnet")
            mock_exit.assert_called_once_with(0)

    @patch("sys.exit")
    def test_cli_close_position_missing_symbol(self, mock_exit):
        with patch.object(sys, "argv", ["execute_futures_trade.py", "--close-position"]):
            eft.main()
            mock_exit.assert_called_once_with(1)

    @patch("sys.exit")
    @patch("execute_futures_trade.audit_orphan_positions")
    def test_cli_audit_orphans(self, mock_audit, mock_exit):
        mock_audit.return_value = {"total_active": 1, "orphans_count": 0, "all_protected": True}
        with patch.object(sys, "argv", ["execute_futures_trade.py", "--audit-orphans", "--env", "testnet"]):
            eft.main()
            mock_audit.assert_called_once_with(target_env="testnet", auto_heal=False)
            mock_exit.assert_called_once_with(0)

    @patch("sys.exit")
    @patch("execute_futures_trade.audit_and_auto_heal_orphans")
    def test_cli_auto_heal(self, mock_heal, mock_exit):
        mock_heal.return_value = {"total_active": 1, "orphans_count": 0, "all_protected": True}
        with patch.object(sys, "argv", ["execute_futures_trade.py", "--auto-heal", "--env", "testnet"]):
            eft.main()
            mock_heal.assert_called_once_with(target_env="testnet")
            mock_exit.assert_called_once_with(0)

    @patch("sys.exit")
    @patch("execute_futures_trade.execute_complete_trade")
    def test_cli_trade_deployment(self, mock_trade, mock_exit):
        mock_trade.return_value = {"success": True, "orderId": 88001}
        with patch.object(sys, "argv", [
            "execute_futures_trade.py",
            "--symbol", "SOLUSDT",
            "--direction", "LONG",
            "--leverage", "3",
            "--margin", "50.0",
            "--env", "testnet",
            "--is-yolo"
        ]):
            eft.main()
            mock_trade.assert_called_once_with(
                symbol="SOLUSDT",
                direction="LONG",
                leverage=3,
                margin_usdt=50.0,
                sl_price=None,
                tp1_price=None,
                tp2_price=None,
                target_env="testnet",
                trigger_price=None,
                order_type="MARKET",
                limit_price=None,
                bypass_delta_gate=False,
                is_yolo=True,
                bypass_eval_gate=False
            )
            mock_exit.assert_called_once_with(0)

    @patch("sys.exit")
    @patch("execute_futures_trade.execute_complete_trade")
    def test_cli_trade_failure_returns_exit_1(self, mock_trade, mock_exit):
        mock_trade.return_value = {"success": False, "error": "Mechanical gate rejection"}
        with patch.object(sys, "argv", [
            "execute_futures_trade.py",
            "--symbol", "SOLUSDT",
            "--direction", "SHORT",
            "--env", "testnet"
        ]):
            eft.main()
            mock_exit.assert_called_once_with(1)

    @patch("execute_futures_trade.send_signed_request")
    def test_setup_margin_and_leverage_clamps_subaccount_to_5x(self, mock_send):
        def fake_send(method, endpoint, params=None, target_env=None):
            if endpoint == '/fapi/v1/marginType':
                return {'code': 200, 'msg': 'success'}
            if endpoint == '/fapi/v1/leverage':
                if params.get('leverage') == 15:
                    return {'code': -4421, 'msg': 'Subaccounts are restricted from using leverage greater than 5x.'}
                if params.get('leverage') == 5:
                    return {'symbol': 'GRASSUSDT', 'leverage': 5}
            return {}
        mock_send.side_effect = fake_send

        lev_res, margin_res, confirmed_lev = eft.setup_margin_and_leverage("GRASSUSDT", 15, target_env="testnet")
        self.assertEqual(confirmed_lev, 5)
        self.assertEqual(lev_res.get('leverage'), 5)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    @patch("execute_futures_trade.setup_margin_and_leverage")
    @patch("execute_futures_trade.get_symbol_filters")
    @patch("execute_futures_trade.send_signed_request")
    def test_execute_complete_trade_fails_closed_when_leverage_fails(
        self, mock_send, mock_filters, mock_setup, mock_equity
    ):
        mock_filters.return_value = {
            "stepSize": 0.001, "minQty": 0.001, "tickSize": 0.1,
            "precision_qty": 3, "precision_price": 1, "minNotional": 5.0
        }
        mock_send.return_value = {'price': '100.0'}
        mock_setup.return_value = ({'code': -4028, 'isError': True, 'error': 'Leverage exceeds account limit'}, {'code': 200, 'msg': 'success'}, 3)
        isolate_workspace(self, profile=up.DEFAULT_PROFILE)

        res = eft.execute_complete_trade(
            symbol="BTCUSDT",
            direction="LONG",
            leverage=10,
            margin_usdt=10.0,
            target_env="testnet",
            bypass_eval_gate=True
        )
        self.assertFalse(res["success"])
        self.assertIn("Failed to configure leverage", res["error"])


if __name__ == "__main__":
    unittest.main()

