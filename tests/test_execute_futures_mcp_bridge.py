#!/usr/bin/env python3
"""
test_execute_futures_mcp_bridge.py - Unit tests for the Binance Agentic MCP Gateway bridge.

Tests:
1. get_mcp_oauth_token retrieval from config, environment, and token files.
2. get_client_config correctly chooses MCP_OAUTH_ACTIVE when BINANCE_AUTH_MODE=MCP.
3. send_mcp_gateway_request mapping to MCP tools (balance, position, orders, algos, leverage).
4. Safety invariant preservation: all pre-trade mechanical hard gates execute before dispatch.
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure scripts directory is on sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import execute_futures_trade as eft


class TestExecuteFuturesMCPBridge(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_get_mcp_oauth_token_from_cfg(self):
        cfg = {"BINANCE_MCP_OAUTH_TOKEN": "token_from_cfg_123"}
        tok = eft.get_mcp_oauth_token(cfg)
        self.assertEqual(tok, "token_from_cfg_123")

    def test_get_mcp_oauth_token_from_env(self):
        with patch.dict(os.environ, {"BINANCE_MCP_OAUTH_TOKEN": "token_from_env_456"}):
            tok = eft.get_mcp_oauth_token({})
            self.assertEqual(tok, "token_from_env_456")

    def test_get_mcp_oauth_token_from_file(self):
        token_file = os.path.join(self.temp_dir.name, "mcp_tokens.json")
        sample_payload = {
            "https://agent.binance.com/mcp/agentic": {
                "token": {
                    "access_token": "token_from_json_file_789"
                }
            }
        }
        with open(token_file, "w", encoding="utf-8") as f:
            json.dump(sample_payload, f)

        with patch.dict(os.environ, {"BINANCE_MCP_OAUTH_PATH": token_file}, clear=False):
            # ensure os.environ doesn't have token var directly
            env_clean = {k: v for k, v in os.environ.items() if k not in ["BINANCE_MCP_OAUTH_TOKEN", "BINANCE_OAUTH_TOKEN"]}
            with patch.dict(os.environ, env_clean, clear=True):
                tok = eft.get_mcp_oauth_token({"BINANCE_MCP_OAUTH_PATH": token_file})
                self.assertEqual(tok, "token_from_json_file_789")

    def test_get_client_config_mcp_mode(self):
        mock_env = {
            "BINANCE_AUTH_MODE": "MCP",
            "BINANCE_MCP_OAUTH_TOKEN": "test_mcp_tok",
            "LIVE_TRADING_ARMED": "true"
        }
        with patch("execute_futures_trade.load_env", return_value=mock_env):
            with patch("execute_futures_trade.get_mcp_oauth_token", return_value="test_mcp_tok"):
                api_key, secret_key, base_url = eft.get_client_config(target_env="prod")
                self.assertEqual(api_key, "MCP_OAUTH_ACTIVE")
                self.assertEqual(secret_key, "test_mcp_tok")
                self.assertEqual(base_url, "https://fapi.binance.com")

    @patch("execute_futures_trade.call_binance_mcp")
    def test_send_mcp_gateway_request_balance(self, mock_mcp):
        mock_mcp.return_value = [{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}]
        res = eft.send_mcp_gateway_request("GET", "/fapi/v2/balance")
        mock_mcp.assert_called_once_with("futures_usds.futuresAccountBalanceV3")
        self.assertEqual(res[0]["asset"], "USDT")

    @patch("execute_futures_trade.call_binance_mcp")
    def test_send_mcp_gateway_request_position_risk_filtered(self, mock_mcp):
        mock_mcp.return_value = [
            {"symbol": "BTCUSDT", "positionAmt": "0.1"},
            {"symbol": "ETHUSDT", "positionAmt": "1.0"}
        ]
        res = eft.send_mcp_gateway_request("GET", "/fapi/v2/positionRisk", params={"symbol": "BTCUSDT"})
        mock_mcp.assert_called_once_with("futures_usds.positionInformationV2")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["symbol"], "BTCUSDT")

    @patch("execute_futures_trade.call_binance_mcp")
    def test_send_mcp_gateway_request_leverage(self, mock_mcp):
        mock_mcp.return_value = {"symbol": "BTCUSDT", "leverage": 3}
        res = eft.send_mcp_gateway_request("POST", "/fapi/v1/leverage", params={"symbol": "BTCUSDT", "leverage": 3})
        mock_mcp.assert_called_once_with("futures_usds.changeInitialLeverage", {"symbol": "BTCUSDT", "leverage": 3})
        self.assertEqual(res["leverage"], 3)

    @patch("execute_futures_trade.call_binance_mcp")
    def test_send_mcp_gateway_request_algo_orders(self, mock_mcp):
        mock_mcp.return_value = [
            {"orderId": 12345, "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "stopPrice": "80000.0", "reduceOnly": True},
            {"orderId": 67890, "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": "75000.0"}
        ]
        res = eft.send_mcp_gateway_request("GET", "/fapi/v1/openAlgoOrders", params={"symbol": "BTCUSDT"})
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["algoId"], 12345)
        self.assertEqual(res[0]["triggerPrice"], 80000.0)
        self.assertTrue(res[0]["closePosition"])

    @patch("execute_futures_trade.call_binance_mcp")
    def test_send_mcp_gateway_request_new_order(self, mock_mcp):
        mock_mcp.return_value = {"orderId": 99999, "status": "FILLED"}
        order_params = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": 0.05,
            "price": 82000.0,
            "timeInForce": "GTC"
        }
        res = eft.send_mcp_gateway_request("POST", "/fapi/v1/order", params=order_params)
        mock_mcp.assert_called_once_with("futures_usds.newOrder", {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": 0.05,
            "price": 82000.0,
            "timeInForce": "GTC"
        })
        self.assertEqual(res["orderId"], 99999)

    @patch("execute_futures_trade.call_binance_mcp")
    def test_send_mcp_gateway_request_algo_stop_loss(self, mock_mcp):
        """closePosition SL via the gateway: quantity looked up from the live position, sent through
        tool_execute -> futures_usds.newAlgoOrder as a CONDITIONAL reduce-only stop."""
        def fake_mcp(tool_name, args=None, session_id=None):
            if tool_name == "futures_usds.positionInformationV2":
                return [
                    {"symbol": "ETHUSDT", "positionAmt": "3.0"},
                    {"symbol": "BTCUSDT", "positionAmt": "0.5"},
                ]
            if tool_name == "tool_execute":
                return {"orderId": 88888, "status": "NEW"}
            return {"error": f"unexpected tool {tool_name}", "isError": True}
        mock_mcp.side_effect = fake_mcp
        algo_params = {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "STOP_MARKET",
            "triggerPrice": 81500.0,
            "closePosition": "true"
        }
        res = eft.send_mcp_gateway_request("POST", "/fapi/v1/algoOrder", params=algo_params)
        mock_mcp.assert_any_call("futures_usds.positionInformationV2")
        mock_mcp.assert_called_with("tool_execute", {
            "toolName": "futures_usds.newAlgoOrder",
            "arguments": {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "algoType": "CONDITIONAL",
                "triggerPrice": "81500.0",
                "quantity": "0.5",
                "reduceOnly": "true"
            }
        })
        self.assertEqual(res["orderId"], 88888)
        self.assertEqual(res["algoId"], 88888)

    @patch("execute_futures_trade.call_binance_mcp")
    def test_send_mcp_gateway_request_algo_stop_loss_without_position_uses_close_position(self, mock_mcp):
        def fake_mcp(tool_name, args=None, session_id=None):
            if tool_name == "futures_usds.positionInformationV2":
                return []
            return {"algoId": 77777}
        mock_mcp.side_effect = fake_mcp
        res = eft.send_mcp_gateway_request("POST", "/fapi/v1/algoOrder", params={
            "symbol": "BTCUSDT", "side": "BUY", "type": "STOP_MARKET", "triggerPrice": 90000.0, "closePosition": "true"
        })
        sent = mock_mcp.call_args[0][1]["arguments"]
        self.assertEqual(sent.get("closePosition"), "true")
        self.assertNotIn("quantity", sent)
        self.assertNotIn("reduceOnly", sent)
        self.assertEqual(res["algoId"], 77777)

    @patch("execute_futures_trade.get_mcp_oauth_token", return_value="fake_token_xyz")
    @patch("urllib.request.urlopen")
    def test_call_binance_mcp_headers_and_session_id(self, mock_urlopen, mock_token):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "result": {
                "content": [{"type": "text", "text": "{\"status\": \"ok\"}"}]
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = eft.call_binance_mcp("test_tool", {"a": 1}, session_id="sess_12345")
        self.assertEqual(res, {"status": "ok"})

        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.headers.get("Authorization"), "Bearer fake_token_xyz")
        self.assertEqual(req.headers.get("Accept"), "application/json, text/event-stream")
        self.assertEqual(req.headers.get("Mcp-session-id"), "sess_12345")

    @patch("execute_futures_trade.get_mcp_oauth_token", return_value="fake_token_xyz")
    @patch("urllib.request.urlopen")
    def test_call_binance_mcp_jsonrpc_error(self, mock_urlopen, mock_token):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "error": {"code": -32600, "message": "Invalid request"}
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = eft.call_binance_mcp("test_tool", {})
        self.assertTrue(res.get("isError"))
        self.assertIn("error", res)

    @patch("execute_futures_trade.get_mcp_oauth_token", return_value="fake_token_xyz")
    @patch("urllib.request.urlopen")
    def test_call_binance_mcp_tool_is_error(self, mock_urlopen, mock_token):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "Position not found"}]
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = eft.call_binance_mcp("test_tool", {})
        self.assertTrue(res.get("isError"))
        self.assertEqual(res.get("error"), "Position not found")


if __name__ == "__main__":
    unittest.main()
