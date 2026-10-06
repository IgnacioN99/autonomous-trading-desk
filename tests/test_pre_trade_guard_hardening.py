#!/usr/bin/env python3
"""
test_pre_trade_guard_hardening.py - Comprehensive Unit Tests for Issue #9 Hardening:
1. Default-DENY on unrecognized/empty input (Finding 1)
2. Single Choke Point Enforcement & Denial of Direct Binance MCP Order Tools (Finding 1)
3. Structured Risk-Reducing Action Parsing (Finding 1)
4. Strict Delta Direction Parsing (Finding 1)
5. Session State Fail-Closed & Staleness Check (Finding 6)
6. Evaluation Dossier Anti-Spoofing & Expiry Calculation (Finding 7)
7. sync_session_state.py Error Handling & Fail-Closed Guard (Finding 6)
8. execute_futures_trade.py Session State Staleness Gate (Finding 6)
"""

import os
import sys
import io
import json
import time
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure scripts and hooks directories are on sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

import pre_trade_guard
import sync_session_state
import execute_futures_trade as eft

GATE_PROFILE = {"max_open_positions": 3, "leverage_standard": 3, "risk_pct_equity": 0.005, "max_margin_ratio": 0.30}


class TestPreTradeGuardHardening(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.mock_root = self.test_dir.name
        self.logs_dir = os.path.join(self.mock_root, "logs")
        self.eval_dir = os.path.join(self.logs_dir, "evaluations")
        self.scripts_dir = os.path.join(self.mock_root, "scripts")
        os.makedirs(self.eval_dir, exist_ok=True)
        os.makedirs(self.scripts_dir, exist_ok=True)

        self.dossier_path = os.path.join(self.eval_dir, "latest_dossier.json")
        self.state_path = os.path.join(self.logs_dir, "session_state.json")

    def tearDown(self):
        self.test_dir.cleanup()

    def _write_dossier(self, approved_candidates, timestamp_ts=None, valid_until_ts=None, status="APPROVED"):
        now_ts = int(time.time())
        ts = now_ts if timestamp_ts is None else timestamp_ts
        vu = (now_ts + 1200) if valid_until_ts is None else valid_until_ts
        approved_symbols = [c["symbol"] for c in approved_candidates if isinstance(c, dict) and "symbol" in c]
        dossier = {
            "timestamp_ts": ts,
            "valid_until_ts": vu,
            "evaluator_agent": "isolated_market_evaluator",
            "status": status,
            "approved_symbols": approved_symbols,
            "approved_candidates": approved_candidates,
            "summary": "Hardening test evaluation dossier"
        }
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(dossier, f)

    def _write_session_state(self, delta_bias="NEUTRAL", is_valid=True, last_updated_ts=None, error=None):
        now_ts = int(time.time())
        lu = now_ts if last_updated_ts is None else last_updated_ts
        state = {
            "is_valid": is_valid,
            "last_updated_ts": lu,
            "last_updated_utc": "2026-10-01 12:00:00 UTC",
            "target_env": "testnet",
            "portfolio_exposure": {
                "delta_bias": delta_bias,
                "net_notional_delta_usdt": 150.0 if delta_bias == "LONG_HEAVY" else (-150.0 if delta_bias == "SHORT_HEAVY" else 0.0),
                "long_notional_usdt": 150.0 if delta_bias == "LONG_HEAVY" else 0.0,
                "short_notional_usdt": 150.0 if delta_bias == "SHORT_HEAVY" else 0.0,
                "total_active_positions": 1
            },
            "active_positions": []
        }
        if error is not None:
            state["error"] = error
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def _run_guard(self, raw_input_str: str) -> dict:
        stdin_backup = sys.stdin
        stdout_backup = sys.stdout
        try:
            sys.stdin = io.StringIO(raw_input_str)
            sys.stdout = io.StringIO()
            with patch("pre_trade_guard.find_workspace_root", return_value=self.mock_root):
                exit_code = pre_trade_guard.main()
            output = sys.stdout.getvalue().strip()
            parsed = json.loads(output)
            parsed["__exit_code__"] = exit_code
            return parsed
        finally:
            sys.stdin = stdin_backup
            sys.stdout = stdout_backup

    def _run_guard_payload(self, payload: dict) -> dict:
        return self._run_guard(json.dumps(payload))

    @staticmethod
    def _cli_payload(flags: str) -> dict:
        """Trade opening through the single choke point (executor CLI)."""
        return {"toolCall": {"name": "run_command",
                             "args": {"CommandLine": f"python3 scripts/execute_futures_trade.py {flags}"}}}

    # =========================================================================
    # 1. DEFAULT-DENY ON UNRECOGNIZED/EMPTY INPUT (Finding 1)
    # =========================================================================
    def test_default_deny_on_empty_input(self):
        """Empty stdin string must return deny with code 2."""
        res = self._run_guard("")
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertEqual(res.get("__exit_code__"), 2)
        self.assertIn("FAIL-CLOSED", res.get("reason", ""))

    def test_default_deny_on_whitespace_input(self):
        """Whitespace-only stdin string must return deny with code 2."""
        res = self._run_guard("   \n\t  ")
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertEqual(res.get("__exit_code__"), 2)

    def test_default_deny_on_invalid_json(self):
        """Malformed JSON must return deny with code 2."""
        res = self._run_guard("{not-valid-json: true")
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertEqual(res.get("__exit_code__"), 2)
        self.assertIn("Invalid JSON", res.get("reason", ""))

    def test_default_deny_on_missing_toolcall(self):
        """Payload missing toolCall object must return deny with code 2."""
        res = self._run_guard_payload({"random_key": "some_value"})
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertEqual(res.get("__exit_code__"), 2)
        self.assertIn("missing toolCall", res.get("reason", ""))

    def test_default_deny_on_empty_tool_name(self):
        """Payload with empty tool name must return deny with code 2."""
        res = self._run_guard_payload({"toolCall": {"name": "", "args": {}}})
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertEqual(res.get("__exit_code__"), 2)
        self.assertIn("Missing or invalid tool name", res.get("reason", ""))

    # =========================================================================
    # 2. SINGLE CHOKE POINT & DENY DIRECT BINANCE MCP WRITE TOOLS (Finding 1)
    # =========================================================================
    def test_direct_binance_mcp_futures_usds_new_order_denied(self):
        """Direct call to binance:futures_usds.newOrder without reduceOnly must be denied with code 2."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL")

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
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("Choke Point Enforcement", res.get("reason", ""))
        self.assertIn("strictly forbidden", res.get("reason", ""))

    def test_direct_binance_mcp_spot_new_order_denied(self):
        """Direct call to binance:spot.newOrder must be denied with code 2."""
        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "spot.newOrder",
                    "Arguments": {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.01"}
                }
            }
        }
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("Choke Point Enforcement", res.get("reason", ""))

    def test_direct_binance_mcp_futures_coin_new_order_denied(self):
        """Direct call to binance:futures_coin.newOrder must be denied with code 2."""
        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_coin.newOrder",
                    "Arguments": {"symbol": "BTCUSD_PERP", "side": "BUY", "quantity": "1"}
                }
            }
        }
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("Choke Point Enforcement", res.get("reason", ""))

    def test_direct_binance_mcp_margin_new_order_denied(self):
        """Direct call to binance:margin.marginAccountNewOrder must be denied with code 2."""
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
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("Choke Point Enforcement", res.get("reason", ""))

    def test_retired_radar_deploy_denied_even_when_gates_pass(self):
        """The retired crypto_radar:deploy_futures_trade wrapper is no longer a choke point: always denied."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL")
        args = {"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}
        payloads = [
            {"toolCall": {"name": "call_mcp_tool", "args": {"ServerName": "crypto_radar",
                                                             "ToolName": "deploy_futures_trade", "Arguments": args}}},
            {"toolCall": {"name": "mcp_crypto_radar_deploy_futures_trade", "args": args}},
            # Legacy tool name behind an unknown server alias
            {"toolCall": {"name": "call_mcp_tool", "args": {"ServerName": "radar-alias",
                                                             "ToolName": "deploy_futures_trade", "Arguments": args}}},
        ]
        for payload in payloads:
            res = self._run_guard_payload(payload)
            self.assertEqual(res.get("decision"), "deny", payload)
            self.assertEqual(res.get("code"), 2)
            self.assertIn("Retired MCP Server", res.get("reason", ""))
            self.assertIn("scripts/execute_futures_trade.py", res.get("reason", ""))

        # The same order through the executor CLI passes the gates
        res = self._run_guard_payload(self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3"))
        self.assertEqual(res.get("decision"), "allow")
        self.assertEqual(res.get("__exit_code__"), 0)

    def test_approved_choke_point_execute_futures_trade_cli_allowed(self):
        """Opening orders via execute_futures_trade.py CLI are permitted when gates pass."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL")

        payload = {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --dir LONG --leverage 3 --margin 100 --sl 80000 --tp1 85000"
                }
            }
        }
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "allow")

    # =========================================================================
    # 3. STRUCTURED RISK-REDUCING ACTION PARSING (Finding 1)
    # =========================================================================
    def test_structured_reduce_only_mcp_arg_allowed(self):
        """Direct Binance MCP order with reduceOnly=True is recognized as risk-reducing and allowed."""
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
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "allow")
        self.assertIn("Risk-reducing", res.get("reason", ""))

    def test_structured_close_position_mcp_arg_allowed(self):
        """Direct Binance MCP order with closePosition=True is recognized as risk-reducing and allowed."""
        payload = {
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "binance",
                    "ToolName": "futures_usds.newOrder",
                    "Arguments": {"symbol": "BTCUSDT", "side": "SELL", "closePosition": True}
                }
            }
        }
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "allow")

    def test_structured_cli_close_position_flag_allowed(self):
        """CLI command with --close-position flag is recognized as risk-reducing and allowed."""
        payload = {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --close-position"
                }
            }
        }
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "allow")

    def test_structured_cli_auto_heal_flag_allowed(self):
        """trading_doctor.py --heal (its real auto-heal flag) is recognized as risk-reducing and allowed."""
        payload = {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "python3 scripts/trading_doctor.py --heal"
                }
            }
        }
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "allow")

    def test_structured_cli_audit_orphans_flag_allowed(self):
        """CLI command with --audit-orphans flag is recognized as risk-reducing and allowed."""
        payload = {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "python3 scripts/execute_futures_trade.py --audit-orphans"
                }
            }
        }
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "allow")

    def _cmd(self, command_line: str) -> dict:
        return self._run_guard_payload({"toolCall": {"name": "run_command", "args": {"CommandLine": command_line}}})

    def test_structured_cli_protect_pending_flag_allowed(self):
        """--protect-pending (post-fill SL/TPs of resting entries) is risk-reducing: no dossier / session state needed."""
        for cmd in ("python3 scripts/execute_futures_trade.py --protect-pending",
                    "python3 scripts/execute_futures_trade.py --protect-pending --env prod",
                    "python3 scripts/execute_futures_trade.py --protect_pending --env testnet"):
            res = self._cmd(cmd)
            self.assertEqual(res.get("decision"), "allow", cmd)
            self.assertIn("Risk-reducing", res.get("reason", ""))

    def test_cli_protect_pending_does_not_whitelist_chained_trade(self):
        """A --protect-pending sub-command never whitelists a trade opening chained before or after it."""
        for cmd in ("python3 scripts/execute_futures_trade.py --protect-pending && "
                    "python3 scripts/execute_futures_trade.py --symbol SOLUSDT --direction LONG",
                    "python3 scripts/execute_futures_trade.py --symbol SOLUSDT --direction LONG --order-type STOP_MARKET "
                    "--trigger-price 150 ; python3 scripts/execute_futures_trade.py --protect-pending"):
            res = self._cmd(cmd)
            self.assertEqual(res.get("decision"), "deny", cmd)
            self.assertIn("Clean-Room Evaluator Required", res.get("reason", ""))

    def test_cli_move_breakeven_with_symbol_allowed(self):
        """--move-breakeven with exactly one --symbol is risk-reducing (no dossier / session state needed)."""
        for cmd in ("python3 scripts/execute_futures_trade.py --move-breakeven --symbol BTCUSDT",
                    "python3 scripts/execute_futures_trade.py --move-breakeven --symbol=BTCUSDT --env prod",
                    "python3 scripts/execute_futures_trade.py --move_breakeven --symbol BTCUSDT --env testnet"):
            res = self._cmd(cmd)
            self.assertEqual(res.get("decision"), "allow", cmd)
            self.assertIn("Risk-reducing", res.get("reason", ""))
        # Issue #111: --force overrides anti-truncation / YOLO BE-after-TP1: user confirmation, never a denial
        res = self._cmd("python3 scripts/execute_futures_trade.py --move-breakeven --symbol=BTCUSDT --force --env prod")
        self.assertEqual(res.get("decision"), "ask")
        self.assertIn("Forced break-even", res.get("reason", ""))

    def test_cli_move_breakeven_without_single_symbol_denied(self):
        """--move-breakeven needs exactly one --symbol; it never falls through to the trade-opening path."""
        for cmd in ("python3 scripts/execute_futures_trade.py --move-breakeven",
                    "python3 scripts/execute_futures_trade.py --move-breakeven --symbol BTCUSDT --symbol ETHUSDT"):
            res = self._cmd(cmd)
            self.assertEqual(res.get("decision"), "deny", cmd)
            self.assertIn("requires exactly one --symbol", res.get("reason", ""))

    def test_cli_move_breakeven_does_not_whitelist_chained_trade(self):
        """A risk-reducing sub-command never whitelists a trade opening chained after it."""
        res = self._cmd("python3 scripts/execute_futures_trade.py --move-breakeven --symbol BTCUSDT && "
                        "python3 scripts/execute_futures_trade.py --symbol SOLUSDT --direction LONG")
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Clean-Room Evaluator Required", res.get("reason", ""))

    def test_cli_positions_is_read_only_ask(self):
        """--positions is a read-only listing: normal permission policy (ask), never trade-gated."""
        for cmd in ("python3 scripts/execute_futures_trade.py --positions",
                    "python3 scripts/execute_futures_trade.py --positions --json --env prod"):
            res = self._cmd(cmd)
            self.assertEqual(res.get("decision"), "ask", cmd)
            self.assertEqual(res.get("__exit_code__"), 0)

    def test_position_guardian_loop_classification(self):
        """The guardian never opens positions: single cycles (--once) are allowed, long-running ones ask."""
        for cmd in ("python3 scripts/loops/position_guardian_loop.py --once",
                    "python3 scripts/loops/position_guardian_loop.py --once --env prod --json",
                    "python3 scripts/loops/position_guardian_loop.py --once --dry-run --close-dead-alpha",
                    "python3 scripts/loops/position_guardian_loop.py --help"):
            self.assertEqual(self._cmd(cmd).get("decision"), "allow", cmd)
        # Issue #100: the auto-allow needs --once; --interval (even with --dry-run) is a long-running loop
        for cmd in ("python3 scripts/loops/position_guardian_loop.py",
                    "python3 scripts/loops/position_guardian_loop.py --dry-run --interval 60",
                    "python3 scripts/loops/position_guardian_loop.py --once --interval 60",
                    "python3 scripts/loops/position_guardian_loop.py --interval 300 --env prod",
                    "nohup python3 scripts/loops/position_guardian_loop.py --interval 300 &"):
            self.assertEqual(self._cmd(cmd).get("decision"), "ask", cmd)

    def test_generic_close_keyword_in_note_not_risk_reducing(self):
        """CLI command with keyword 'close' in a note or comment is NOT classified as risk-reducing."""
        # Unapproved asset, so if it were misclassified as risk-reducing it would be allowed
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL")

        payload = {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": 'python3 scripts/execute_futures_trade.py --symbol SOLUSDT --dir LONG --note "close to key support"'
                }
            }
        }
        res = self._run_guard_payload(payload)
        # Must NOT be allowed as risk-reducing; it must be caught by unapproved symbol gate
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Asset 'SOLUSDT' was NOT approved", res.get("reason", ""))

    def test_cli_reduce_only_false_not_risk_reducing(self):
        """CLI command with --reduce-only false is NOT treated as risk-reducing."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL")

        payload = {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "python3 scripts/execute_futures_trade.py --symbol SOLUSDT --dir LONG --reduce-only false"
                }
            }
        }
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("Asset 'SOLUSDT' was NOT approved", res.get("reason", ""))

    # =========================================================================
    # 4. STRICT DELTA DIRECTION PARSING (Finding 1)
    # =========================================================================
    def test_delta_direction_conflicting_direction_and_side_denied(self):
        """Conflicting --direction / --side flags (both LONG and SHORT) fail closed."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL")

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --side SELL")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("Conflicting trade directions", res.get("reason", ""))

    def test_delta_direction_missing_in_cli_denied(self):
        """Missing trade direction on the executor command line fails closed."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL")

        payload = self._cli_payload("--symbol BTCUSDT --leverage 3")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("Unable to determine trade direction strictly", res.get("reason", ""))

    def test_delta_direction_conflicting_in_cli_denied(self):
        """Conflicting trade directions in command line (--dir LONG and --dir SHORT) fails closed."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL")

        payload = {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --dir LONG --side SELL"
                }
            }
        }
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("Conflicting trade directions", res.get("reason", ""))

    # =========================================================================
    # 5. SESSION STATE FAIL-CLOSED & STALENESS CHECK (Finding 6)
    # =========================================================================
    def test_missing_session_state_file_denied(self):
        """Missing session_state.json file blocks execution fail-closed."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        if os.path.exists(self.state_path):
            os.remove(self.state_path)

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("session_state.json does not exist", res.get("reason", ""))

    def test_invalid_session_state_flagged_denied(self):
        """session_state.json with is_valid=False blocks execution fail-closed."""
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL", is_valid=False, error="Binance API connection timeout")

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("flagged INVALID", res.get("reason", ""))

    def test_stale_session_state_denied(self):
        """session_state.json older than 300 seconds (e.g. 350s) blocks execution."""
        now_ts = int(time.time())
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL", is_valid=True, last_updated_ts=now_ts - 350)

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("STALE", res.get("reason", ""))

    def test_fresh_session_state_passes(self):
        """Fresh session_state.json (e.g. 30s old) passes staleness check."""
        now_ts = int(time.time())
        self._write_dossier([{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}])
        self._write_session_state(delta_bias="NEUTRAL", is_valid=True, last_updated_ts=now_ts - 30)

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "allow")

    # =========================================================================
    # 6. EVALUATION DOSSIER ANTI-SPOOFING & EXPIRY CALCULATION (Finding 7)
    # =========================================================================
    def test_dossier_cannot_be_forged_with_huge_valid_until_ts(self):
        """Dossier with huge valid_until_ts but old timestamp_ts is clamped to timestamp + 1200s and rejected."""
        now_ts = int(time.time())
        # Created 25 minutes ago (1500s ago), but valid_until_ts claims valid for 1 year
        timestamp_old = now_ts - 1500
        valid_until_huge = now_ts + 31536000  # 1 year in the future
        self._write_dossier(
            [{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}],
            timestamp_ts=timestamp_old,
            valid_until_ts=valid_until_huge
        )
        self._write_session_state(delta_bias="NEUTRAL")

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("expired", res.get("reason", "").lower())

    def test_dossier_missing_timestamp_ts_denied(self):
        """Dossier missing timestamp_ts is rejected fail-closed."""
        now_ts = int(time.time())
        dossier = {
            "valid_until_ts": now_ts + 1200,
            "status": "APPROVED",
            "approved_symbols": ["BTCUSDT"],
            "approved_candidates": [{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}]
        }
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(dossier, f)
        self._write_session_state(delta_bias="NEUTRAL")

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("timestamp_ts is missing", res.get("reason", ""))

    def test_dossier_future_timestamp_ts_denied(self):
        """Dossier with timestamp_ts in the future is rejected as forged."""
        now_ts = int(time.time())
        self._write_dossier(
            [{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}],
            timestamp_ts=now_ts + 600,  # 10 minutes in future
            valid_until_ts=now_ts + 1800
        )
        self._write_session_state(delta_bias="NEUTRAL")

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "deny")
        self.assertEqual(res.get("code"), 2)
        self.assertIn("is in the future", res.get("reason", ""))

    def test_dossier_valid_recent_passes(self):
        """Fresh evaluation dossier (< 20m) with consistent timestamps passes gate."""
        now_ts = int(time.time())
        self._write_dossier(
            [{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}],
            timestamp_ts=now_ts - 60,
            valid_until_ts=now_ts + 1140
        )
        self._write_session_state(delta_bias="NEUTRAL")

        payload = self._cli_payload("--symbol BTCUSDT --direction LONG --leverage 3")
        res = self._run_guard_payload(payload)
        self.assertEqual(res.get("decision"), "allow")

    # =========================================================================
    # 7. SYNC_SESSION_STATE.PY FAIL-CLOSED CHECK (Finding 6)
    # =========================================================================
    @patch("execute_futures_trade.send_signed_request")
    def test_sync_session_state_fails_closed_on_error_dict(self, mock_request):
        """sync_session_state writes is_valid=False and error when positionRisk returns error dict."""
        mock_request.side_effect = lambda method, endpoint, *args, **kwargs: (
            {"price": "65000.0"} if endpoint == "/fapi/v1/ticker/price"
            else {"code": -1021, "msg": "Timestamp for this request was 1000ms ahead"}
        )
        with patch("sync_session_state.STATE_FILE", self.state_path):
            state = sync_session_state.sync_session_state(target_env="testnet")
            self.assertFalse(state.get("is_valid"))
            self.assertIn("Failed to fetch positionRisk", state.get("error", ""))
            self.assertIn("last_updated_ts", state)
            self.assertEqual(state.get("portfolio_exposure", {}).get("delta_bias"), "UNKNOWN")

    @patch("execute_futures_trade.send_signed_request")
    def test_sync_session_state_fails_closed_on_api_exception(self, mock_request):
        """sync_session_state writes is_valid=False when positionRisk raises an exception."""
        def fake_request(method, endpoint, *args, **kwargs):
            if endpoint == "/fapi/v1/ticker/price":
                return {"price": "65000.0"}
            raise ConnectionError("Connection refused by Binance Gateway")

        mock_request.side_effect = fake_request
        with patch("sync_session_state.STATE_FILE", self.state_path):
            state = sync_session_state.sync_session_state(target_env="testnet")
            self.assertFalse(state.get("is_valid"))
            self.assertIn("Connection refused", state.get("error", ""))
            self.assertIn("last_updated_ts", state)

    # =========================================================================
    # 8. EXECUTE_FUTURES_TRADE.PY SESSION STATE GATE (Finding 6)
    # =========================================================================
    def test_execute_futures_trade_stale_session_state_rejected_in_prod(self):
        """execute_futures_trade rejects execution in PROD if session_state is stale."""
        now_ts = int(time.time())
        self._write_session_state(delta_bias="NEUTRAL", is_valid=True, last_updated_ts=now_ts - 400)

        orig_join = os.path.join
        def fake_join(*p):
            if "session_state.json" in p:
                return self.state_path
            return orig_join(*p)

        def flat_exchange(method, endpoint, params=None, target_env=None, retry_count=0):
            # issue #101 live PROD snapshot: flat exchange; any other exchange call is a test bug (never real)
            if method == "GET" and not params and endpoint in ("/fapi/v2/positionRisk", "/fapi/v1/openAlgoOrders",
                                                               "/fapi/v1/openOrders"):
                return []
            raise AssertionError(f"unexpected exchange call {method} {endpoint}")

        # pending_entries.json (Gate 0A) is read from the temp root and the profile is explicit: never the real logs/
        with patch("os.path.join", side_effect=fake_join), \
             patch("execute_futures_trade._workspace_dir", return_value=self.mock_root), \
             patch("execute_futures_trade.send_signed_request", side_effect=flat_exchange), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test")), \
             patch("user_profile.load_user_profile", return_value=dict(GATE_PROFILE)):
            passed, reason = eft.check_mechanical_gates(
                direction="LONG",
                cur_price=65000.0,
                sl_price=60000.0,
                tp1_price=70000.0,
                total_qty=0.01,
                leverage=3,
                target_env="prod"
            )
            self.assertFalse(passed)
            self.assertIn("STALE", reason)

    def test_execute_futures_trade_invalid_session_state_rejected_in_prod(self):
        """execute_futures_trade rejects execution in PROD if session_state is_valid=False."""
        self._write_session_state(delta_bias="NEUTRAL", is_valid=False, error="API sync failure")

        orig_join = os.path.join
        def fake_join(*p):
            if "session_state.json" in p:
                return self.state_path
            return orig_join(*p)

        def flat_exchange(method, endpoint, params=None, target_env=None, retry_count=0):
            # issue #101 live PROD snapshot: flat exchange; any other exchange call is a test bug (never real)
            if method == "GET" and not params and endpoint in ("/fapi/v2/positionRisk", "/fapi/v1/openAlgoOrders",
                                                               "/fapi/v1/openOrders"):
                return []
            raise AssertionError(f"unexpected exchange call {method} {endpoint}")

        # pending_entries.json (Gate 0A) is read from the temp root and the profile is explicit: never the real logs/
        with patch("os.path.join", side_effect=fake_join), \
             patch("execute_futures_trade._workspace_dir", return_value=self.mock_root), \
             patch("execute_futures_trade.send_signed_request", side_effect=flat_exchange), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test")), \
             patch("user_profile.load_user_profile", return_value=dict(GATE_PROFILE)):
            passed, reason = eft.check_mechanical_gates(
                direction="LONG",
                cur_price=65000.0,
                sl_price=60000.0,
                tp1_price=70000.0,
                total_qty=0.01,
                leverage=3,
                target_env="prod"
            )
            self.assertFalse(passed)
            self.assertIn("INVALID", reason)


if __name__ == "__main__":
    unittest.main()
