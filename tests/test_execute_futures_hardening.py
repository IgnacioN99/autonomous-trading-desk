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
import unittest
from unittest.mock import patch, MagicMock, mock_open

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import execute_futures_trade as eft


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
    def test_yolo_leverage_up_to_15x_allowed(self, mock_eq):
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

        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        os.makedirs(log_dir, exist_ok=True)
        state_file = os.path.join(log_dir, "session_state.json")
        orig_content = None
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    orig_content = f.read()
            except Exception:
                pass

        try:
            with open(state_file, "w", encoding="utf-8") as f:
                f.write(valid_state)

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
        finally:
            if orig_content is not None:
                with open(state_file, "w", encoding="utf-8") as f:
                    f.write(orig_content)
            elif os.path.exists(state_file):
                try:
                    os.remove(state_file)
                except Exception:
                    pass


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

        res = eft.execute_complete_trade(
            symbol="BTCUSDT",
            direction="LONG",
            leverage=3,
            margin_usdt=100.0,
            sl_price=98.0,
            tp1_price=103.0,
            tp2_price=106.0,
            target_env="testnet"
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
        def fake_send(method, endpoint, params=None, target_env=None):
            if endpoint in ['/fapi/v2/balance', '/fapi/v3/balance']:
                return [{'asset': 'USDT', 'balance': '10000.0'}]
            if endpoint == '/fapi/v1/ticker/price':
                return {'price': '100.0'}
            if endpoint == '/fapi/v1/order' and method == 'POST':
                return {'orderId': 70001, 'status': 'NEW'}
            return {}

        mock_send.side_effect = fake_send

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
            limit_price=98.0
        )

        self.assertTrue(res["success"])
        self.assertTrue(res["pending_limit_entry"])
        self.assertEqual(res["orderId"], 70001)
        self.assertEqual(res["status"], "NEW")
        self.assertIn("deferred until fill", res["message"])
        # Only 2 requests: ticker and entry order. No premature TP orders dispatched!
        self.assertEqual(mock_send.call_count, 2)


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


if __name__ == "__main__":
    unittest.main()
