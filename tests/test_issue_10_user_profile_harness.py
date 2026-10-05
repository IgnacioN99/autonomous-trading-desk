#!/usr/bin/env python3
"""
test_issue_10_user_profile_harness.py - Comprehensive Unit Tests for Issue #10:
Clone-Readiness & Profile-Driven Behavior (User Profile as Single Source of Truth).

Covers:
1. User Profile Schema & Safe Cold-Start Defaults:
   - profile_completed: False
   - autonomous_execution_tier_s: False
   - overnight_mode: ZERO_OVERNIGHT_RISK | CLOSE_ALL_AT_MARKET | SWING_STRUCTURAL_STOP
2. Enforce Profile in Mechanical Gates & Execution Engine:
   - max_open_positions gate rejection
   - yolo_slot_enabled gate rejection
   - leverage_standard dynamic cap
   - risk_pct_equity dynamic sizing
   - Dynamic margin scaling for accounts < $300 balance
3. Enforce Profile in Pre-Trade Guard:
   - Dynamic leverage_standard from user profile
   - Autonomous Tier S execution gate in PROD (requires human confirmation when False)
   - yolo_slot_enabled rejection
   - max_open_positions rejection
4. Enforce Profile in Night Cutoff Loop:
   - CLOSE_ALL_AT_MARKET: closes 100% of positions
   - SWING_STRUCTURAL_STOP: maintains positions with verified SL, ratchets winning to BE
   - ZERO_OVERNIGHT_RISK: ratchets winning to BE and closes unhedged directional positions
5. Blocking Onboarding & Doctor Sensor:
   - Fail-closed exit 1 when profile_completed is False
   - Fail-closed exit 1 when PreToolUse hooks are unconfigured
"""

import os
import sys
import io
import json
import time
import tempfile
import unittest
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
LOOPS_DIR = os.path.join(SCRIPTS_DIR, "loops")

for p in [BASE_DIR, SCRIPTS_DIR, HOOKS_DIR, LOOPS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

import user_profile as up
import execute_futures_trade as eft
import pre_trade_guard
import night_cutoff_loop
import trading_doctor

_REAL_EXISTS = os.path.exists


class TestUserProfileSchemaDefaults(unittest.TestCase):
    """1. User Profile Schema & Safe Cold-Start Defaults."""

    def test_default_profile_cold_start_invariants(self):
        """Verifies safe defaults in DEFAULT_PROFILE."""
        self.assertFalse(up.DEFAULT_PROFILE["profile_completed"])
        self.assertFalse(up.DEFAULT_PROFILE["autonomous_execution_tier_s"])
        self.assertFalse(up.DEFAULT_PROFILE["yolo_slot_enabled"])
        self.assertEqual(up.DEFAULT_PROFILE["overnight_mode"], "ZERO_OVERNIGHT_RISK")
        self.assertEqual(up.DEFAULT_PROFILE["max_open_positions"], 3)
        self.assertEqual(up.DEFAULT_PROFILE["leverage_standard"], 3)
        self.assertEqual(up.DEFAULT_PROFILE["risk_pct_equity"], 0.005)

    def test_example_file_matches_cold_start_schema(self):
        """Verifies config/user_profile.json.example matches safe defaults."""
        example_path = os.path.join(BASE_DIR, "config", "user_profile.json.example")
        self.assertTrue(os.path.exists(example_path))
        with open(example_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertFalse(data.get("profile_completed"))
        self.assertFalse(data.get("autonomous_execution_tier_s"))
        self.assertFalse(data.get("yolo_slot_enabled"))
        self.assertEqual(data.get("overnight_mode"), "ZERO_OVERNIGHT_RISK")
        self.assertEqual(data.get("max_open_positions"), 3)
        self.assertEqual(data.get("leverage_standard"), 3)
        self.assertEqual(data.get("risk_pct_equity"), 0.005)

    def test_save_and_load_user_profile(self):
        """Verifies persistence and profile_completed flag upon onboarding."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_profile = os.path.join(tmp_dir, "user_profile.json")
            with patch("user_profile.PROFILE_FILE", tmp_profile), \
                 patch("user_profile.CONFIG_DIR", tmp_dir):
                # When no file exists, loads default profile
                prof = up.load_user_profile()
                self.assertFalse(prof["profile_completed"])
                self.assertFalse(prof["autonomous_execution_tier_s"])

                # Save updates
                up.save_user_profile({
                    "risk_pct_equity": 0.01,
                    "autonomous_execution_tier_s": True,
                    "overnight_mode": "SWING_STRUCTURAL_STOP"
                })
                loaded = up.load_user_profile()
                self.assertTrue(loaded["profile_completed"])
                self.assertTrue(loaded["autonomous_execution_tier_s"])
                self.assertEqual(loaded["risk_pct_equity"], 0.01)
                self.assertEqual(loaded["overnight_mode"], "SWING_STRUCTURAL_STOP")


class TestMechanicalGatesProfileEnforcement(unittest.TestCase):
    """2. Mechanical Gates & Execution Engine Profile Enforcement."""

    def setUp(self):
        self.state_file = os.path.join(BASE_DIR, "logs", "session_state.json")
        self.orig_state = None
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    self.orig_state = f.read()
            except Exception:
                pass

    def tearDown(self):
        if self.orig_state is not None:
            with open(self.state_file, "w", encoding="utf-8") as f:
                f.write(self.orig_state)

    def _write_session_state(self, active_count=0):
        state = {
            "is_valid": True,
            "last_updated_ts": int(time.time()),
            "portfolio_exposure": {
                "delta_bias": "NEUTRAL",
                "total_active_positions": active_count
            },
            "active_positions": [{"symbol": f"SYM{i}USDT"} for i in range(active_count)]
        }
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    def test_max_open_positions_gate_rejection(self, mock_equity):
        """Orders must be rejected when total_active_positions >= max_open_positions."""
        self._write_session_state(active_count=3)
        mock_prof = {
            "max_open_positions": 3,
            "yolo_slot_enabled": True,
            "leverage_standard": 3,
            "risk_pct_equity": 0.005
        }

        with patch("user_profile.load_user_profile", return_value=mock_prof):
            # With 3 active positions and limit=3, order must be rejected
            ok, reason = eft.check_mechanical_gates(
                direction="LONG",
                cur_price=100.0,
                sl_price=98.0,
                tp1_price=105.0,
                total_qty=1.0,
                leverage=3,
                target_env="testnet"
            )
            self.assertFalse(ok)
            self.assertIn("Max open positions limit (3) reached", reason)

            # With 2 active positions, order passes
            self._write_session_state(active_count=2)
            ok, reason = eft.check_mechanical_gates(
                direction="LONG",
                cur_price=100.0,
                sl_price=98.0,
                tp1_price=105.0,
                total_qty=1.0,
                leverage=3,
                target_env="testnet"
            )
            self.assertTrue(ok)
            self.assertIsNone(reason)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    def test_yolo_slot_disabled_rejection(self, mock_equity):
        """YOLO orders must be rejected when yolo_slot_enabled is False in user profile."""
        self._write_session_state(active_count=0)
        mock_prof = {
            "max_open_positions": 3,
            "yolo_slot_enabled": False, # YOLO disabled
            "leverage_standard": 3,
            "risk_pct_equity": 0.005
        }

        with patch("user_profile.load_user_profile", return_value=mock_prof):
            ok, reason = eft.check_mechanical_gates(
                direction="LONG",
                cur_price=100.0,
                sl_price=98.0,
                tp1_price=105.0,
                total_qty=1.0,
                leverage=10,
                target_env="testnet",
                is_yolo=True
            )
            self.assertFalse(ok)
            self.assertIn("YOLO moonshot slot is disabled in user profile", reason)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    def test_dynamic_leverage_standard_cap(self, mock_equity):
        """Standard orders exceeding user profile leverage_standard must be rejected."""
        self._write_session_state(active_count=0)
        mock_prof = {
            "max_open_positions": 3,
            "yolo_slot_enabled": False,
            "leverage_standard": 2, # Conservative 2x cap
            "risk_pct_equity": 0.005
        }

        with patch("user_profile.load_user_profile", return_value=mock_prof):
            # 3x exceeds 2x limit -> rejected
            ok, reason = eft.check_mechanical_gates(
                direction="LONG",
                cur_price=100.0,
                sl_price=98.0,
                tp1_price=105.0,
                total_qty=1.0,
                leverage=3,
                target_env="testnet",
                is_yolo=False
            )
            self.assertFalse(ok)
            self.assertIn("exceeds standard limit (2x)", reason)

            # 2x is allowed
            ok, reason = eft.check_mechanical_gates(
                direction="LONG",
                cur_price=100.0,
                sl_price=98.0,
                tp1_price=105.0,
                total_qty=1.0,
                leverage=2,
                target_env="testnet",
                is_yolo=False
            )
            self.assertTrue(ok)
            self.assertIsNone(reason)

    @patch("quant_risk_engine.get_account_equity", return_value=100.0)
    def test_dynamic_margin_scaling_small_accounts(self, mock_equity):
        """Accounts < $300 balance scale margin dynamically so the 30% cap is not violated."""
        # Account equity = $100.00, max_margin_ratio = 0.30
        # Dynamic margin = min(100.0, max(5.0, 100 * 0.30 * 0.5)) = $15.00 USDT
        mock_prof = {
            "max_margin_ratio": 0.30,
            "leverage_standard": 3,
            "risk_pct_equity": 0.005,
            "max_open_positions": 3
        }

        def send_mock(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v1/ticker/price":
                return {"price": "100.0"}
            if endpoint == "/fapi/v1/order":
                return {"orderId": 999, "avgPrice": "100.0", "status": "FILLED"}
            return {}

        with patch("user_profile.load_user_profile", return_value=mock_prof), \
             patch("execute_futures_trade.get_symbol_filters", return_value={"stepSize": 0.01, "precision_qty": 2, "tickSize": 0.01, "precision_price": 2, "minQty": 0.01, "minNotional": 5.0}), \
             patch("execute_futures_trade.send_signed_request", side_effect=send_mock), \
             patch("execute_futures_trade.check_mechanical_gates", return_value=(True, None)), \
             patch("execute_futures_trade.setup_margin_and_leverage", return_value=True), \
             patch("execute_futures_trade.place_algo_stop_loss", return_value={"algoId": 888}), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 888})):

            # With default margin_usdt=100.0, it scales down to $15.00 without triggering margin guardrail
            res = eft.execute_complete_trade(
                symbol="BTCUSDT",
                direction="LONG",
                leverage=3,
                margin_usdt=100.0,
                target_env="testnet",
                bypass_eval_gate=True  # TESTNET-only explicit bypass: this test targets margin scaling
            )
            self.assertTrue(res.get("success"), f"Expected success but got: {res.get('error')}")
            # Real margin should be scaled to $15.00
            self.assertEqual(res.get("real_margin"), 15.0)


class TestPreTradeGuardProfileEnforcement(unittest.TestCase):
    """3. Enforce User Profile in Pre-Trade Guard."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.mock_root = self.test_dir.name
        self.logs_dir = os.path.join(self.mock_root, "logs")
        self.eval_dir = os.path.join(self.logs_dir, "evaluations")
        os.makedirs(self.eval_dir, exist_ok=True)

        self.dossier_path = os.path.join(self.eval_dir, "latest_dossier.json")
        self.state_path = os.path.join(self.logs_dir, "session_state.json")
        # PROD dossiers must carry evaluator-subagent provenance: simulate the agy brain dir
        self.brain_dir = os.path.join(self.mock_root, "brain")
        self._brain_env = patch.dict(os.environ, {"AGY_BRAIN_DIRS": self.brain_dir})
        self._brain_env.start()

    def tearDown(self):
        self._brain_env.stop()
        self.test_dir.cleanup()

    def _write_dossier(self, approved_candidates):
        """Writes a dossier exactly as `record_evaluation.py --from-subagent` would (schema v2 + provenance)."""
        import datetime
        from utils import dossier_provenance as dp
        candidates = [dict(c, direction=c.get("direction", "LONG")) for c in approved_candidates]
        conv_id = "abcdef12-3456-7890-abcd-ef1234567890"
        tdir = os.path.join(self.brain_dir, conv_id, ".system_generated", "logs")
        os.makedirs(tdir, exist_ok=True)
        block = json.dumps({"status": "APPROVED", "approved_candidates": candidates, "summary": "test"})
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tpath = os.path.join(tdir, "transcript.jsonl")
        with open(tpath, "w", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 0, "source": "SYSTEM", "type": "USER_INPUT",
                                "content": "sender=11111111-2222-3333-4444-555555555555"}) + "\n")
            f.write(json.dumps({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": created,
                                "content": f"<dossier_json>{block}</dossier_json>"}) + "\n")
        record = dp.build_record_from_extraction(dp.extract_dossier_from_transcript(tpath))
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)

    def _write_session_state(self, total_active=0):
        state = {
            "is_valid": True,
            "last_updated_ts": int(time.time()),
            "portfolio_exposure": {
                "delta_bias": "NEUTRAL",
                "total_active_positions": total_active
            },
            "active_positions": []
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def _run_guard(self, raw_input_str: str) -> dict:
        stdin_backup = sys.stdin
        stdout_backup = sys.stdout
        try:
            sys.stdin = io.StringIO(raw_input_str)
            sys.stdout = io.StringIO()
            with patch("pre_trade_guard.find_workspace_root", return_value=self.mock_root):
                pre_trade_guard.main()
            output = sys.stdout.getvalue().strip()
            return json.loads(output)
        finally:
            sys.stdin = stdin_backup
            sys.stdout = stdout_backup

    def test_autonomous_tier_s_disabled_in_prod_denies_without_confirmation(self):
        """In PROD, when autonomous_execution_tier_s is False, opening order requires confirmation."""
        self._write_dossier([{"symbol": "BTCUSDT", "tier": "Tier S", "leverage": 3}])
        self._write_session_state(total_active=0)
        mock_prof = {
            "profile_completed": True,
            "autonomous_execution_tier_s": False, # Disabled!
            "max_open_positions": 3,
            "leverage_standard": 3,
            "yolo_slot_enabled": False
        }

        with patch("user_profile.load_user_profile", return_value=mock_prof):
            payload = json.dumps({
                "toolCall": {
                    "name": "run_command",
                    "args": {
                        "CommandLine": "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --env prod"
                    }
                }
            })
            res = self._run_guard(payload)
            self.assertEqual(res.get("decision"), "deny")
            self.assertIn("Autonomous Execution Disabled", res.get("reason", ""))

            # When explicit --confirmed flag is provided, confirmation is satisfied
            payload_confirmed = json.dumps({
                "toolCall": {
                    "name": "run_command",
                    "args": {
                        "CommandLine": "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --env prod --confirmed"
                    }
                }
            })
            res_confirmed = self._run_guard(payload_confirmed)
            self.assertEqual(res_confirmed.get("decision"), "allow")

    def test_autonomous_tier_s_enabled_in_prod_allows_fast_track(self):
        """In PROD, when autonomous_execution_tier_s is True, opening order is allowed without confirmation."""
        self._write_dossier([{"symbol": "BTCUSDT", "tier": "Tier S", "leverage": 3}])
        self._write_session_state(total_active=0)
        mock_prof = {
            "profile_completed": True,
            "autonomous_execution_tier_s": True, # Enabled!
            "max_open_positions": 3,
            "leverage_standard": 3,
            "yolo_slot_enabled": False
        }

        with patch("user_profile.load_user_profile", return_value=mock_prof):
            payload = json.dumps({
                "toolCall": {
                    "name": "run_command",
                    "args": {
                        "CommandLine": "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --env prod"
                    }
                }
            })
            res = self._run_guard(payload)
            self.assertEqual(res.get("decision"), "allow")

    def test_pre_trade_guard_blocks_when_max_positions_reached(self):
        """Pre-trade guard denies opening orders when max_open_positions limit is reached."""
        self._write_dossier([{"symbol": "BTCUSDT", "tier": "Tier S", "leverage": 3}])
        self._write_session_state(total_active=3) # 3 active positions
        mock_prof = {
            "profile_completed": True,
            "autonomous_execution_tier_s": True,
            "max_open_positions": 3,
            "leverage_standard": 3,
            "yolo_slot_enabled": False
        }

        with patch("user_profile.load_user_profile", return_value=mock_prof):
            payload = json.dumps({
                "toolCall": {
                    "name": "run_command",
                    "args": {
                        "CommandLine": "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --dir LONG --env testnet"
                    }
                }
            })
            res = self._run_guard(payload)
            self.assertEqual(res.get("decision"), "deny")
            self.assertIn("Max Open Positions Gate", res.get("reason", ""))

    def test_pre_trade_guard_blocks_yolo_when_disabled(self):
        """Pre-trade guard denies YOLO trades when yolo_slot_enabled is False in profile."""
        self._write_dossier([{"symbol": "PEPEUSDT", "tier": "YOLO", "is_yolo": True, "leverage": 15}])
        self._write_session_state(total_active=0)
        mock_prof = {
            "profile_completed": True,
            "autonomous_execution_tier_s": True,
            "max_open_positions": 3,
            "leverage_standard": 3,
            "yolo_slot_enabled": False # YOLO disabled!
        }

        with patch("user_profile.load_user_profile", return_value=mock_prof):
            payload = json.dumps({
                "toolCall": {
                    "name": "run_command",
                    "args": {
                        "CommandLine": "python3 scripts/execute_futures_trade.py --symbol PEPEUSDT --dir LONG --leverage 15 --is-yolo --env testnet"
                    }
                }
            })
            res = self._run_guard(payload)
            self.assertEqual(res.get("decision"), "deny")
            self.assertIn("YOLO moonshot slot is disabled in user profile", res.get("reason", ""))


class TestNightCutoffLoopOvernightModes(unittest.TestCase):
    """4. Enforce User Profile in Night Cutoff Loop."""

    @patch("execute_futures_trade.send_signed_request")
    @patch("execute_futures_trade.close_position_market")
    def test_close_all_at_market_mode(self, mock_close, mock_send):
        """In CLOSE_ALL_AT_MARKET mode, 100% of open positions are closed at market."""
        # Active positions: BTC and ETH
        mock_send.side_effect = lambda method, endpoint, params=None, target_env=None: [
            {"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "100.0", "markPrice": "102.0", "unRealizedProfit": "0.2", "isolatedMargin": "10.0"},
            {"symbol": "ETHUSDT", "positionAmt": "-1.0", "entryPrice": "2000.0", "markPrice": "1990.0", "unRealizedProfit": "10.0", "isolatedMargin": "50.0"}
        ] if endpoint == "/fapi/v2/positionRisk" else []
        mock_close.return_value = {"success": True}

        with patch("user_profile.load_user_profile", return_value={"overnight_mode": "CLOSE_ALL_AT_MARKET"}), \
             patch("os.system"):
            night_cutoff_loop.run_night_cutoff(target_env="testnet")
            # Must close both positions
            self.assertEqual(mock_close.call_count, 2)
            mock_close.assert_any_call("BTCUSDT", target_env="testnet")
            mock_close.assert_any_call("ETHUSDT", target_env="testnet")

    @patch("execute_futures_trade.send_signed_request")
    @patch("execute_futures_trade.close_position_market")
    @patch("execute_futures_trade.move_sl_to_breakeven")
    def test_swing_structural_stop_mode(self, mock_be, mock_close, mock_send):
        """In SWING_STRUCTURAL_STOP mode, positions with verified SL remain open (never closed at market)."""
        mock_send.side_effect = lambda method, endpoint, params=None, target_env=None: [
            {"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "100.0", "markPrice": "105.0", "unRealizedProfit": "5.0", "isolatedMargin": "10.0"}
        ] if endpoint == "/fapi/v2/positionRisk" else (
            [{"orderType": "STOP_MARKET", "triggerPrice": "98.0"}] if endpoint == "/fapi/v1/openAlgoOrders" else []
        )
        mock_be.return_value = {"success": True}

        with patch("user_profile.load_user_profile", return_value={"overnight_mode": "SWING_STRUCTURAL_STOP"}), \
             patch("execute_futures_trade.get_symbol_filters", return_value={"tickSize": 0.1, "precision_price": 1}), \
             patch("os.system"):
            night_cutoff_loop.run_night_cutoff(target_env="testnet")
            # Must NOT close position
            mock_close.assert_not_called()
            # Must ratchet winning position (+50% ROE) to BE
            mock_be.assert_called_once_with("BTCUSDT", target_env="testnet")

    @patch("execute_futures_trade.send_signed_request")
    @patch("execute_futures_trade.close_position_market")
    def test_zero_overnight_risk_closes_unhedged_positions(self, mock_close, mock_send):
        """In ZERO_OVERNIGHT_RISK mode, positions not ratcheted to BE are closed at market."""
        # Position in loss (unhedged directional risk)
        mock_send.side_effect = lambda method, endpoint, params=None, target_env=None: [
            {"symbol": "SOLUSDT", "positionAmt": "1.0", "entryPrice": "150.0", "markPrice": "148.0", "unRealizedProfit": "-2.0", "isolatedMargin": "15.0"}
        ] if endpoint == "/fapi/v2/positionRisk" else (
            [{"orderType": "STOP_MARKET", "triggerPrice": "145.0"}] if endpoint == "/fapi/v1/openAlgoOrders" else []
        )
        mock_close.return_value = {"success": True}

        with patch("user_profile.load_user_profile", return_value={"overnight_mode": "ZERO_OVERNIGHT_RISK"}), \
             patch("execute_futures_trade.get_symbol_filters", return_value={"tickSize": 0.1, "precision_price": 1}), \
             patch("os.system"):
            night_cutoff_loop.run_night_cutoff(target_env="testnet")
            # Unhedged position must be closed at market to guarantee zero overnight risk
            mock_close.assert_called_once_with("SOLUSDT", target_env="testnet")


class TestTradingDoctorBlockingSensor(unittest.TestCase):
    """5. Blocking Onboarding & Doctor Sensor."""

    @patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"})
    @patch("execute_futures_trade.get_client_config", return_value=("key12345678", "sec12345678", "http://binance.mock"))
    @patch("urllib.request.urlopen")
    @patch("execute_futures_trade.send_signed_request")
    def test_doctor_fails_closed_when_profile_not_completed(self, mock_send, mock_urlopen, mock_cfg, mock_env):
        """Trading doctor must exit with code 1 if profile_completed is False."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        mock_send.side_effect = lambda method, endpoint, params=None, target_env=None: [
            {"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}
        ] if endpoint == "/fapi/v2/balance" else []

        # Profile is NOT completed (hook self-test stubbed: this test isolates the profile check)
        with patch("user_profile.load_user_profile", return_value={"profile_completed": False}),              patch("trading_doctor.check_pretool_hook", return_value={"ok": True, "critical": [], "warnings": [], "info": []}):
            exit_code = trading_doctor.run_doctor(target_env="testnet")
            self.assertEqual(exit_code, 1, "Doctor must return 1 (Fail-Closed) when profile onboarding is not completed.")

    @patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"})
    @patch("execute_futures_trade.get_client_config", return_value=("key12345678", "sec12345678", "http://binance.mock"))
    @patch("urllib.request.urlopen")
    @patch("execute_futures_trade.send_signed_request")
    def test_doctor_fails_closed_when_hooks_unconfigured(self, mock_send, mock_urlopen, mock_cfg, mock_env):
        """Trading doctor must exit with code 1 if PreToolUse hooks are missing."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        mock_send.side_effect = lambda method, endpoint, params=None, target_env=None: [
            {"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}
        ] if endpoint == "/fapi/v2/balance" else []

        real_exists = os.path.exists
        with patch("user_profile.load_user_profile", return_value={"profile_completed": True, "risk_pct_equity": 0.005, "operating_mode": "BALANCED_DELTA_NEUTRAL"}), \
             patch("os.path.exists", side_effect=lambda p: False if (".agents" in str(p) or ".claude" in str(p)) else real_exists(p)):
            exit_code = trading_doctor.run_doctor(target_env="testnet")
            self.assertEqual(exit_code, 1, "Doctor must return 1 (Fail-Closed) when PreToolUse hooks are unconfigured.")


if __name__ == "__main__":
    unittest.main()
