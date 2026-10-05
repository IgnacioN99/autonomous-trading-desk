#!/usr/bin/env python3
"""
test_binance_mcp_guard.py - Unit tests for Binance MCP safety guards and environment isolation.

Tests:
1. Interception of binance:futures_usds.newOrder with BUY and SELL.
2. Delta-neutral gate blocking BUY when LONG_HEAVY.
3. Delta-neutral gate allowing SELL when LONG_HEAVY (short hedge).
4. Delta-neutral gate blocking SELL when SHORT_HEAVY and allowing BUY.
5. reduceOnly and closePosition exemptions (risk-reducing actions allowed immediately).
6. Unapproved asset rejection by Clean-Room Evaluator Gate.
7. Excessive leverage rejection (> 3x) on futures_usds.changeInitialLeverage unless YOLO.
8. PROD invariant enforcement (gate bypasses strictly forbidden in PROD).
9. Fail-closed behavior on unexpected exceptions or errors.
10. post_trade_sync.py MCP event detection for opening vs closing orders and sync/audit triggers.
"""

import os
import sys
import json
import time
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure scripts directory is on sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, HOOKS_DIR)

import pre_trade_guard
import post_trade_sync


class TestBinanceMCPGuard(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.mock_root = self.test_dir.name
        self.logs_dir = os.path.join(self.mock_root, "logs")
        self.eval_dir = os.path.join(self.logs_dir, "evaluations")
        self.scripts_dir = os.path.join(self.mock_root, "scripts")
        os.makedirs(self.eval_dir, exist_ok=True)
        os.makedirs(self.scripts_dir, exist_ok=True)

        # Create dummy sync_session_state.py so os.path.exists passes in tests
        with open(os.path.join(self.scripts_dir, "sync_session_state.py"), "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\n")

        # Create mock user profile in self.mock_root for isolated test execution
        self.config_dir = os.path.join(self.mock_root, "config")
        os.makedirs(self.config_dir, exist_ok=True)
        self.profile_path = os.path.join(self.config_dir, "user_profile.json")
        with open(self.profile_path, "w", encoding="utf-8") as f:
            json.dump({
                "profile_completed": True,
                "yolo_slot_enabled": True,
                "leverage_standard": 3,
                "max_open_positions": 5,
                "autonomous_execution_tier_s": True
            }, f)

        self.dossier_path = os.path.join(self.eval_dir, "latest_dossier.json")
        self.state_path = os.path.join(self.logs_dir, "session_state.json")

    def tearDown(self):
        self.test_dir.cleanup()

    def _write_dossier(self, approved_candidates, valid_seconds=1200, status="APPROVED"):
        now_ts = int(time.time())
        approved_symbols = [c["symbol"] for c in approved_candidates if isinstance(c, dict) and "symbol" in c]
        dossier = {
            "timestamp_ts": now_ts,
            "valid_until_ts": now_ts + valid_seconds,
            "evaluator_agent": "isolated_market_evaluator",
            "status": status,
            "approved_symbols": approved_symbols,
            "approved_candidates": approved_candidates,
            "summary": "Test evaluation dossier"
        }
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(dossier, f)

    def _write_session_state(self, delta_bias="NEUTRAL", net_delta=0.0):
        now_ts = int(time.time())
        state = {
            "is_valid": True,
            "last_updated_ts": now_ts,
            "last_updated_utc": "2026-09-30 16:00:00 UTC",
            "target_env": "testnet",
            "portfolio_exposure": {
                "delta_bias": delta_bias,
                "net_notional_delta_usdt": net_delta,
                "long_notional_usdt": 100.0 if delta_bias == "LONG_HEAVY" else 0.0,
                "short_notional_usdt": 100.0 if delta_bias == "SHORT_HEAVY" else 0.0,
                "total_active_positions": 1
            },
            "active_positions": []
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def _run_guard(self, payload: dict) -> dict:
        import io
        stdin_backup = sys.stdin
        stdout_backup = sys.stdout
        try:
            sys.stdin = io.StringIO(json.dumps(payload))
            sys.stdout = io.StringIO()
            with patch("pre_trade_guard.find_workspace_root", return_value=self.mock_root):
                pre_trade_guard.main()
            output = sys.stdout.getvalue().strip()
            return json.loads(output)
        finally:
            sys.stdin = stdin_backup
            sys.stdout = stdout_backup

    @staticmethod
    def _cli_payload(flags: str) -> dict:
        """Trade opening through the single choke point (executor CLI)."""
        return {"toolCall": {"name": "run_command",
                             "args": {"CommandLine": f"python3 scripts/execute_futures_trade.py {flags}"}}}

    # -------------------------------------------------------------------------
    # 1. INTERCEPTION OF OPENING ORDERS VIA APPROVED CHOKE POINT (BUY & SELL)
    # -------------------------------------------------------------------------
    def test_intercept_binance_new_order_buy_and_sell(self):
        """Verifies executor trade openings are intercepted and allowed under neutral conditions."""
        self._write_dossier([
            {"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3},
            {"symbol": "ETHUSDT", "direction": "SHORT", "leverage": 3}
        ])
        self._write_session_state(delta_bias="NEUTRAL", net_delta=0.0)

        # BUY order on approved symbol
        payload_buy = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res_buy = self._run_guard(payload_buy)
        self.assertEqual(res_buy.get("decision"), "allow")

        # SELL order on approved symbol
        payload_sell = self._cli_payload("--symbol ETHUSDT --direction SHORT --leverage 3")
        res_sell = self._run_guard(payload_sell)
        self.assertEqual(res_sell.get("decision"), "allow")

    # -------------------------------------------------------------------------
    # 2. DELTA GATE BLOCKING BUY WHEN LONG_HEAVY
    # -------------------------------------------------------------------------
    def test_delta_gate_blocks_buy_when_long_heavy(self):
        """Verifies opening additional LONG / BUY orders is blocked when portfolio is LONG_HEAVY."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="LONG_HEAVY", net_delta=150.0)

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Delta-Neutral Hard Gate", res.get("reason", ""))
        self.assertIn("LONG_HEAVY", res.get("reason", ""))

    # -------------------------------------------------------------------------
    # 3. DELTA GATE ALLOWING SELL WHEN LONG_HEAVY (SHORT HEDGE)
    # -------------------------------------------------------------------------
    def test_delta_gate_allows_sell_when_long_heavy_hedge(self):
        """Verifies SELL (Short hedge) is permitted even when portfolio is LONG_HEAVY."""
        self._write_dossier([{"symbol": "ETHUSDT", "direction": "SHORT", "leverage": 3}])
        self._write_session_state(delta_bias="LONG_HEAVY", net_delta=150.0)

        payload = self._cli_payload("--symbol ETHUSDT --direction SHORT --leverage 3")
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "allow")

    # -------------------------------------------------------------------------
    # 4. DELTA GATE BLOCKING SELL WHEN SHORT_HEAVY
    # -------------------------------------------------------------------------
    def test_delta_gate_blocks_sell_when_short_heavy(self):
        """Verifies opening additional SHORT / SELL orders is blocked when portfolio is SHORT_HEAVY."""
        self._write_dossier([
            {"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3},
            {"symbol": "ETHUSDT", "direction": "SHORT", "leverage": 3}
        ])
        self._write_session_state(delta_bias="SHORT_HEAVY", net_delta=-200.0)

        # SELL order should be blocked
        payload_sell = self._cli_payload("--symbol ETHUSDT --direction SHORT --leverage 3")
        res_sell = self._run_guard(payload_sell)
        self.assertEqual(res_sell.get("decision"), "deny")
        self.assertIn("SHORT_HEAVY", res_sell.get("reason", ""))

        # BUY order should be allowed (acts as long hedge)
        payload_buy = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res_buy = self._run_guard(payload_buy)
        self.assertEqual(res_buy.get("decision"), "allow")

    # -------------------------------------------------------------------------
    # 5. REDUCEONLY AND CLOSEPOSITION EXEMPTIONS
    # -------------------------------------------------------------------------
    def test_reduce_only_exemption(self):
        """Verifies reduceOnly and closePosition bypass delta and evaluator gates unconditionally."""
        # Unbalanced portfolio and NO evaluation dossier
        self._write_session_state(delta_bias="LONG_HEAVY", net_delta=300.0)

        # reduceOnly as boolean True
        payload_reduce_bool = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.newOrder",
                    "Arguments": {
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "type": "MARKET",
                        "quantity": "0.01",
                        "reduceOnly": True
                    }
                }
            }
        }
        res1 = self._run_guard(payload_reduce_bool)
        self.assertEqual(res1.get("decision"), "allow")
        self.assertIn("Risk-reducing", res1.get("reason", ""))

        # reduceOnly as string "true"
        payload_reduce_str = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.newOrder",
                    "Arguments": {
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "type": "MARKET",
                        "quantity": "0.01",
                        "reduceOnly": "true"
                    }
                }
            }
        }
        res2 = self._run_guard(payload_reduce_str)
        self.assertEqual(res2.get("decision"), "allow")

        # closePosition as boolean True
        payload_close_pos = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.newOrder",
                    "Arguments": {
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "type": "MARKET",
                        "quantity": "0.01",
                        "closePosition": True
                    }
                }
            }
        }
        res3 = self._run_guard(payload_close_pos)
        self.assertEqual(res3.get("decision"), "allow")

        # reduceOnly: false MUST NOT be treated as risk-reducing
        payload_reduce_false = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.newOrder",
                    "Arguments": {
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "type": "MARKET",
                        "quantity": "0.01",
                        "reduceOnly": False
                    }
                }
            }
        }
        res4 = self._run_guard(payload_reduce_false)
        # Should be blocked because no valid dossier exists
        self.assertEqual(res4.get("decision"), "deny")

    # -------------------------------------------------------------------------
    # 6. UNAPPROVED ASSET REJECTION
    # -------------------------------------------------------------------------
    def test_unapproved_asset_rejection(self):
        """Verifies opening orders on assets outside the evaluation dossier are rejected."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL", net_delta=0.0)

        payload = self._cli_payload("--symbol SOLUSDT --direction LONG --leverage 3")
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Asset 'SOLUSDT' was NOT approved", res.get("reason", ""))

    # -------------------------------------------------------------------------
    # 7. EXCESSIVE LEVERAGE REJECTION ON changeInitialLeverage
    # -------------------------------------------------------------------------
    def test_leverage_gate_standard_allowed(self):
        """Verifies standard leverage changes (<= 3x) are authorized without restrictions."""
        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.changeInitialLeverage",
                    "Arguments": {"symbol": "BTCUSDT", "leverage": 3}
                }
            }
        }
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "allow")
        self.assertIn("Standard leverage", res.get("reason", ""))

    def test_leverage_gate_excessive_denied_without_yolo(self):
        """Verifies leverage > 3x is blocked if the asset is not authorized as YOLO."""
        # Standard candidate with leverage: 3
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])

        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.changeInitialLeverage",
                    "Arguments": {"symbol": "BTCUSDT", "leverage": 10}
                }
            }
        }
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Leverage Gate", res.get("reason", ""))
        self.assertIn("not authorized as a YOLO moonshot", res.get("reason", ""))

    def test_leverage_gate_excessive_allowed_with_yolo(self):
        """Verifies leverage up to 15x is allowed if authorized as YOLO in the dossier."""
        self._write_dossier([
            {"symbol": "PEPEUSDT", "direction": "LONG", "is_yolo": True, "leverage": 15, "tier": "Tier S YOLO"}
        ])

        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.changeInitialLeverage",
                    "Arguments": {"symbol": "PEPEUSDT", "leverage": 15}
                }
            }
        }
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "allow")
        self.assertIn("YOLO moonshot leverage", res.get("reason", ""))

    # -------------------------------------------------------------------------
    # 8. PRODUCTION INVARIANT: GATE BYPASSES FORBIDDEN IN PROD
    # -------------------------------------------------------------------------
    def test_prod_invariant_blocks_gate_bypasses(self):
        """Verifies gate bypass flags are strictly denied in PROD environment."""
        with patch.dict(os.environ, {"BINANCE_API_ENV": "PROD"}):
            payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3 --bypass-eval-gate")
            res = self._run_guard(payload)
            self.assertEqual(res.get("decision"), "deny")
            self.assertIn("PROD INVARIANT VIOLATION", res.get("reason", ""))

    def test_testnet_bypass_flag_is_not_mistaken_for_inline_eval(self):
        """`--bypass-eval-gate` is a sanctioned TESTNET flag, not a shell `eval`; real `eval` stays denied."""
        self._write_session_state(delta_bias="NEUTRAL", net_delta=0.0)
        res = self._run_guard(self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3 --bypass-eval-gate --env testnet"))
        self.assertEqual(res.get("decision"), "allow")
        res = self._run_guard({"toolCall": {"name": "run_command", "args": {
            "CommandLine": "eval \"python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG\""}}})
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Inline code", res.get("reason", ""))

    # -------------------------------------------------------------------------
    # 9. MARGIN ACCOUNT NEW ORDER ENFORCEMENT
    # -------------------------------------------------------------------------
    def test_margin_account_new_order_enforces_gates(self):
        """Verifies margin.marginAccountNewOrder direct write calls are denied by choke point enforcement."""
        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "margin.marginAccountNewOrder",
                    "Arguments": {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.01"}
                }
            }
        }
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Choke Point Enforcement", res.get("reason", ""))

    # -------------------------------------------------------------------------
    # 10. POST-TRADE SYNC MCP EVENT DETECTION & TRIGGERS
    # -------------------------------------------------------------------------
    @patch("post_trade_sync.subprocess.run")
    def test_post_trade_sync_mcp_opening_order(self, mock_subprocess):
        """Verifies post_trade_sync triggers sync AND orphan audit for opening orders."""
        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.newOrder",
                    "Arguments": {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.01"}
                }
            }
        }

        with patch("execute_futures_trade.audit_orphan_positions") as mock_audit:
            with patch("post_trade_sync.find_workspace_root", return_value=self.mock_root):
                res = post_trade_sync.handle_post_trade_sync(payload)

        self.assertTrue(res["order_placed"])
        self.assertTrue(res["is_opening"])
        self.assertTrue(res["synced"])
        self.assertTrue(res["audit_healed"])
        mock_subprocess.assert_called_once()
        mock_audit.assert_called_once_with(target_env="testnet", auto_heal=True)

    @patch("post_trade_sync.subprocess.run")
    def test_post_trade_sync_mcp_closing_order(self, mock_subprocess):
        """Verifies post_trade_sync triggers sync but SKIPS orphan audit for closing orders."""
        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.newOrder",
                    "Arguments": {"symbol": "BTCUSDT", "side": "SELL", "quantity": "0.01", "reduceOnly": True}
                }
            }
        }

        with patch("execute_futures_trade.audit_orphan_positions") as mock_audit:
            with patch("post_trade_sync.find_workspace_root", return_value=self.mock_root):
                res = post_trade_sync.handle_post_trade_sync(payload)

        self.assertTrue(res["order_placed"])
        self.assertFalse(res["is_opening"])
        self.assertTrue(res["synced"])
        self.assertFalse(res["audit_healed"])
        mock_subprocess.assert_called_once()
        mock_audit.assert_not_called()

    @patch("post_trade_sync.subprocess.run")
    def test_post_trade_sync_unrelated_tool_ignored(self, mock_subprocess):
        """Verifies post_trade_sync ignores non-trading tool calls."""
        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "spot.klines",
                    "Arguments": {"symbol": "BTCUSDT"}
                }
            }
        }

        res = post_trade_sync.handle_post_trade_sync(payload)
        self.assertFalse(res["order_placed"])
        self.assertFalse(res["synced"])
    def test_unapproved_asset_rejection_empty_approved_list(self):
        """Verifies order is rejected fail-closed if dossier has empty approved_symbols list."""
        self._write_dossier([])  # Empty approved list
        self._write_session_state(delta_bias="NEUTRAL", net_delta=0.0)

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Asset 'BTCUSDT' was NOT approved", res.get("reason", ""))

    def test_unapproved_asset_rejection_missing_symbol(self):
        """Verifies order is rejected fail-closed if target symbol cannot be extracted."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL", net_delta=0.0)

        payload = self._cli_payload("--direction LONG --leverage 3")
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Unable to determine the order symbol deterministically", res.get("reason", ""))

    def test_algoorder_not_unconditional_risk_reducing(self):
        """Verifies tool calls with 'algoorder' in name require explicit reduceOnly or closePosition flag."""
        # Unapproved symbol, no dossier
        self._write_session_state(delta_bias="NEUTRAL", net_delta=0.0)

        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.newAlgoOrder",
                    "Arguments": {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.01"}
                }
            }
        }
        res = self._run_guard(payload)
        # Without reduceOnly, this must NOT be automatically allowed
        self.assertEqual(res.get("decision"), "deny")

    # -------------------------------------------------------------------------
    # 11. RETIRED crypto_radar MCP SERVER
    # -------------------------------------------------------------------------
    def test_retired_radar_server_denied_even_with_valid_gates(self):
        """Every crypto_radar call is denied (fail closed if a stale config still starts the server)."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL", net_delta=0.0)
        for tool, args in (("deploy_futures_trade", {"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}),
                           ("move_to_breakeven", {"symbol": "BTCUSDT"}),
                           ("scan_intraday_market", {})):
            res = self._run_guard({"toolCall": {"name": "call_mcp_tool", "args": {
                "ServerName": "crypto_radar", "ToolName": tool, "Arguments": args}}})
            self.assertEqual(res.get("decision"), "deny", tool)
            self.assertIn("Retired MCP Server", res.get("reason", ""))

    @patch("post_trade_sync.subprocess.run")
    def test_post_trade_sync_ignores_retired_radar_calls(self, mock_subprocess):
        payload = {"toolCall": {"name": "call_mcp_tool", "args": {
            "ServerName": "crypto_radar", "ToolName": "deploy_futures_trade",
            "Arguments": {"symbol": "BTCUSDT", "direction": "LONG"}}}}
        res = post_trade_sync.handle_post_trade_sync(payload)
        self.assertFalse(res["order_placed"])
        mock_subprocess.assert_not_called()

    def test_delta_gate_fail_closed_on_corrupt_session_state(self):
        """Verifies order is blocked fail-closed if session_state.json is corrupt or unreadable."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        # Write corrupted JSON to session_state
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("{corrupt_json: invalid}")

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("FAIL-CLOSED", res.get("reason", ""))


if __name__ == "__main__":
    unittest.main()

