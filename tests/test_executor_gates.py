#!/usr/bin/env python3
"""
test_executor_gates.py - Offline tests for the execution-engine hard gates:
1. Clean-room dossier gate inside the executor (PROD fail-closed, --bypass-eval-gate refused in PROD,
   climax watcher never self-signs in PROD).
3. Isolated margin fail-closed (only -4046 tolerated) + sub-account -4421 auto-clamp.
4. Liquidation gate (LONG/SHORT, 3x/15x/50x) computed with the leverage that actually applies.
5. Single-source desk leverage ceiling (user_profile.get_leverage_ceiling) + --set-leverage-yolo validation.
6. trading_doctor.check_pretool_hook executed against synthetic hooks.json setups.

No network: every Binance call is mocked, and urllib is blocked for the whole module.
"""

import datetime
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import execute_futures_trade as eft
import user_profile as up
import trading_doctor
from utils import dossier_provenance as dp

FILTERS = {"stepSize": 0.001, "minQty": 0.001, "tickSize": 0.01, "precision_qty": 3, "precision_price": 2, "minNotional": 5.0}
PROFILE = {
    "yolo_slot_enabled": True, "leverage_standard": 3, "leverage_yolo": 15,
    "max_open_positions": 100, "risk_pct_equity": 0.005, "max_margin_ratio": 0.30,
}


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def _read_bytes(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _load_module(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BASE_DIR, rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =====================================================================================
# 1. Dossier gate
# =====================================================================================
class _TempWorkspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "logs", "evaluations"), exist_ok=True)
        self.brain = os.path.join(self.root, "brain")
        self.dossier_path = os.path.join(self.root, "logs", "evaluations", "latest_dossier.json")
        self._env = patch.dict(os.environ, {"AGY_BRAIN_DIRS": self.brain})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def write_subagent_dossier(self, candidates, conv_id="abcdef12-3456-7890-abcd-ef1234567890", created=None):
        """Simulates the isolated_market_evaluator transcript + record_evaluation.py --from-subagent."""
        created = created or datetime.datetime.now(datetime.timezone.utc)
        tdir = os.path.join(self.brain, conv_id, ".system_generated", "logs")
        os.makedirs(tdir, exist_ok=True)
        block = json.dumps({"status": "APPROVED", "approved_candidates": candidates, "summary": "test"})
        steps = [
            {"step_index": 0, "source": "SYSTEM", "type": "USER_INPUT", "content": "sender=11111111-2222-3333-4444-555555555555"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
             "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "content": f"Master Dossier\n<dossier_json>{block}</dossier_json>"},
        ]
        tpath = os.path.join(tdir, "transcript.jsonl")
        with open(tpath, "w", encoding="utf-8") as f:
            for s in steps:
                f.write(json.dumps(s) + "\n")
        record = dp.build_record_from_extraction(dp.extract_dossier_from_transcript(tpath))
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return record

    def write_self_signed_dossier(self, symbol="SOLUSDT", direction="LONG", agent="climax_watcher_loop"):
        """Legacy record_evaluation_dossier() output (what climax_watcher_loop used to write in PROD)."""
        now = int(time.time())
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp_ts": now, "valid_until_ts": now + 1200, "evaluator_agent": agent,
                "conversation_id": "clean_room_context", "status": "APPROVED",
                "approved_symbols": [symbol],
                "approved_candidates": [{"symbol": symbol, "direction": direction, "tier": "Tier S"}],
            }, f)


class TestEnforceEvaluationDossier(_TempWorkspace):

    def test_prod_without_dossier_fails_closed(self):
        ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("Evaluation Gate, PROD", reason)
        self.assertIn("No evaluation dossier", reason)

    def test_prod_bypass_refused(self):
        self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG"}])
        ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "prod", bypass_eval_gate=True, base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("refused in PROD", reason)

    def test_prod_valid_subagent_dossier_passes(self):
        self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG", "tier": "Tier S"}])
        ok, reason, cand = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertTrue(ok, reason)
        self.assertEqual(cand["symbol"], "SOLUSDT")

    def test_prod_direction_mismatch_rejected(self):
        self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG"}])
        ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "SHORT", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("SHORT", reason)

    def test_prod_unapproved_symbol_rejected(self):
        self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG"}])
        ok, reason, _ = eft.enforce_evaluation_dossier("ETHUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("NOT approved", reason)

    def test_prod_tampered_dossier_rejected(self):
        self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG"}])
        with open(self.dossier_path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        rec["approved_symbols"].append("PEPEUSDT")
        rec["approved_candidates"].append({"symbol": "PEPEUSDT", "direction": "LONG"})
        rec["provenance"]["sha256"] = "0" * 64
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        ok, reason, _ = eft.enforce_evaluation_dossier("PEPEUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("hash", reason)

    def test_prod_self_signed_watcher_dossier_rejected(self):
        self.write_self_signed_dossier()
        ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("Evaluation Gate, PROD", reason)

    def test_prod_requires_confirmation_when_evaluator_says_so(self):
        self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG", "requires_user_confirmation": True}])
        ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("confirmation", reason)
        ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "prod", confirmed=True, base_dir=self.root)
        self.assertTrue(ok, reason)

    def test_testnet_without_dossier_rejected_with_bypass_hint(self):
        ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "testnet", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("--bypass-eval-gate", reason)

    def test_testnet_explicit_bypass_allowed(self):
        ok, _, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "testnet", bypass_eval_gate=True, base_dir=self.root)
        self.assertTrue(ok)

    def test_testnet_self_signed_watcher_dossier_accepted(self):
        self.write_self_signed_dossier()
        ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "testnet", base_dir=self.root)
        self.assertTrue(ok, reason)

    def test_validator_unavailable_fails_closed(self):
        with patch.object(eft, "validate_dossier_for_trade", None):
            ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("unavailable", reason)


class TestExecutorDossierIntegration(_TempWorkspace):
    """execute_complete_trade must reject before ANY Binance write when the dossier gate fails."""

    def _run(self, **kw):
        with patch("execute_futures_trade.find_workspace_root", return_value=self.root), \
             patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.send_signed_request") as mock_send, \
             patch("execute_futures_trade.setup_margin_and_leverage") as mock_setup, \
             patch("execute_futures_trade.get_symbol_filters", return_value=None) as mock_filters, \
             patch("quant_risk_engine.get_account_equity", return_value=1000.0):
            res = eft.execute_complete_trade(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0, **kw)
        return res, mock_send, mock_setup, mock_filters

    def test_prod_no_dossier_blocks_before_any_request(self):
        res, mock_send, mock_setup, mock_filters = self._run(target_env="prod")
        self.assertFalse(res["success"])
        self.assertTrue(res.get("evaluation_gate_rejection"))
        mock_send.assert_not_called()
        mock_setup.assert_not_called()
        mock_filters.assert_not_called()

    def test_prod_bypass_flag_refused(self):
        self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG"}])
        res, mock_send, mock_setup, _ = self._run(target_env="prod", bypass_eval_gate=True)
        self.assertFalse(res["success"])
        self.assertIn("refused in PROD", res["error"])
        mock_send.assert_not_called()
        mock_setup.assert_not_called()

    def test_prod_valid_dossier_passes_gate(self):
        self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG"}])
        res, _, _, mock_filters = self._run(target_env="prod")
        # Gate passed -> execution continued to the (mocked, empty) exchange filters lookup
        mock_filters.assert_called_once()
        self.assertIn("Filters not found", res["error"])

    @patch("sys.exit")
    def test_cli_bypass_eval_gate_in_prod_exits_1(self, mock_exit):
        argv = ["execute_futures_trade.py", "--symbol", "SOLUSDT", "--direction", "LONG", "--env", "prod", "--bypass-eval-gate"]
        with patch.object(sys, "argv", argv), \
             patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.send_signed_request") as mock_send, \
             patch("builtins.print"):
            eft.main()
        mock_exit.assert_called_once_with(1)
        mock_send.assert_not_called()


class TestClimaxWatcherNeverSelfSignsInProd(_TempWorkspace):

    @classmethod
    def setUpClass(cls):
        try:
            cls.loop = _load_module("climax_watcher_loop_under_test", os.path.join("scripts", "loops", "climax_watcher_loop.py"))
        except Exception as e:
            raise unittest.SkipTest(f"climax_watcher_loop not importable: {e}")

    def _candidate(self):
        c = MagicMock()
        c.symbol, c.direction = "SOLUSDT", "LONG"
        c.required_margin, c.sl_price, c.tp1_price, c.tp2_price = 12.5, 95.0, 104.0, 110.0
        return c

    def test_prod_auto_deploy_without_evaluator_dossier_alerts_instead(self):
        with patch("execute_futures_trade.find_workspace_root", return_value=self.root), \
             patch.object(self.loop, "emit_alert") as mock_alert, \
             patch("subprocess.run") as mock_run:
            res = self.loop.auto_deploy_candidate(self._candidate(), 3, "prod")
        self.assertFalse(res["deployed"])
        self.assertTrue(res["blocked"])
        mock_run.assert_not_called()
        self.assertEqual(mock_alert.call_args[0][0], "AUTO_DEPLOY_BLOCKED_NO_EVALUATOR_DOSSIER")

    def test_prod_auto_deploy_with_self_signed_dossier_still_blocked(self):
        self.write_self_signed_dossier()
        with patch("execute_futures_trade.find_workspace_root", return_value=self.root), \
             patch.object(self.loop, "emit_alert"), \
             patch("subprocess.run") as mock_run:
            res = self.loop.auto_deploy_candidate(self._candidate(), 3, "prod")
        self.assertTrue(res["blocked"])
        mock_run.assert_not_called()

    def test_prod_auto_deploy_with_evaluator_dossier_runs_executor_without_confirmed(self):
        self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG"}])
        proc = MagicMock(returncode=0, stdout=json.dumps({"success": True}), stderr="")
        with patch("execute_futures_trade.find_workspace_root", return_value=self.root), \
             patch.object(self.loop, "emit_alert"), \
             patch("subprocess.run", return_value=proc) as mock_run, \
             patch("builtins.print"):
            res = self.loop.auto_deploy_candidate(self._candidate(), 3, "prod")
        self.assertTrue(res["deployed"])
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--confirmed", cmd)
        self.assertNotIn("--bypass-eval-gate", cmd)
        self.assertIn("--sl-price", cmd)

    def test_testnet_keeps_legacy_confirmed_deploy(self):
        cmd = self.loop.build_deploy_command(self._candidate(), 3, "testnet", confirmed=True)
        self.assertIn("--confirmed", cmd)
        self.assertEqual(cmd[cmd.index("--env") + 1], "testnet")

    def test_prod_watcher_does_not_import_or_call_record_evaluation(self):
        src = _read_bytes(os.path.join(BASE_DIR, "scripts", "loops", "climax_watcher_loop.py")).decode("utf-8")
        self.assertNotIn("from record_evaluation import record_evaluation_dossier\nfrom utils", src)
        self.assertIn('if target_env == "prod":', src)


# =====================================================================================
# 3. Isolated margin fail-closed
# =====================================================================================
class TestIsolatedMarginFailClosed(unittest.TestCase):

    def _fake_send(self, margin_res, lev_responses=None):
        calls = []
        lev_responses = lev_responses or {}
        def fake(method, endpoint, params=None, target_env=None):
            calls.append((method, endpoint, dict(params or {})))
            if endpoint == "/fapi/v1/marginType":
                return margin_res
            if endpoint == "/fapi/v1/leverage":
                return lev_responses.get(params["leverage"], {"symbol": params["symbol"], "leverage": params["leverage"]})
            if endpoint == "/fapi/v1/ticker/price":
                return {"price": "100.0"}
            if endpoint == "/fapi/v1/order" and method == "POST":
                return {"orderId": 1, "avgPrice": "100.0", "status": "FILLED"}
            return {}
        return fake, calls

    def test_margin_type_responses(self):
        cases = [
            ({"code": 200, "msg": "success"}, True),
            ({"code": -4046, "msg": "No need to change margin type."}, True),
            ({"error": "No need to change margin type.", "isError": True, "code": -4046}, True),
            ({"code": -4048, "msg": "Margin type cannot be changed if there exists position."}, False),
            ({"code": -4047, "msg": "Margin type cannot be changed if there exists open orders."}, False),
            ({"error": "MCP Gateway Error: timed out", "isError": True}, False),
            ({"error": "Binance credentials not configured in .env"}, False),
            ({}, False),
            (None, False),
        ]
        for res, expected in cases:
            ok, reason = eft.margin_type_isolated_confirmed(res)
            self.assertEqual(ok, expected, f"{res} -> {reason}")

    @patch("execute_futures_trade.send_signed_request")
    def test_setup_does_not_touch_leverage_when_margin_fails(self, mock_send):
        fake, calls = self._fake_send({"code": -4048, "msg": "Margin type cannot be changed if there exists position."})
        mock_send.side_effect = fake
        lev_res, margin_res, confirmed = eft.setup_margin_and_leverage("SOLUSDT", 3, target_env="testnet")
        self.assertIsNone(lev_res)
        self.assertIsNone(confirmed)
        self.assertEqual([c[1] for c in calls], ["/fapi/v1/marginType"])

    @patch("execute_futures_trade.send_signed_request")
    def test_already_isolated_proceeds_and_subaccount_clamp_kept(self, mock_send):
        fake, calls = self._fake_send(
            {"code": -4046, "msg": "No need to change margin type."},
            lev_responses={15: {"code": -4421, "msg": "Subaccounts are restricted from using leverage greater than 5x."}},
        )
        mock_send.side_effect = fake
        with patch("builtins.print"):
            lev_res, _, confirmed = eft.setup_margin_and_leverage("PEPEUSDT", 15, target_env="testnet")
        self.assertEqual(confirmed, 5)
        self.assertEqual([c[2].get("leverage") for c in calls if c[1] == "/fapi/v1/leverage"], [15, 5])

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    @patch("execute_futures_trade.get_symbol_filters", return_value=FILTERS)
    @patch("execute_futures_trade.send_signed_request")
    def test_execute_aborts_without_any_order_when_margin_fails(self, mock_send, _f, _e):
        fake, calls = self._fake_send({"code": -4048, "msg": "Margin type cannot be changed if there exists position."})
        mock_send.side_effect = fake
        res = eft.execute_complete_trade(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                                         sl_price=97.0, target_env="testnet", bypass_eval_gate=True)
        self.assertFalse(res["success"])
        self.assertIn("ISOLATED", res["error"])
        self.assertIn("fail-closed", res["error"])
        self.assertFalse([c for c in calls if c[1] == "/fapi/v1/order"])
        self.assertFalse([c for c in calls if c[1] == "/fapi/v1/leverage"])


# =====================================================================================
# 4. Liquidation gate
# =====================================================================================
class TestLiquidationGate(unittest.TestCase):

    def test_liquidation_formula_long_short(self):
        mmr = 0.01
        for lev in (3, 15, 50):
            long_liq = eft.estimate_isolated_liquidation_price("LONG", 100.0, lev, mmr)
            short_liq = eft.estimate_isolated_liquidation_price("SHORT", 100.0, lev, mmr)
            self.assertAlmostEqual(long_liq, 100.0 * (1 - 1 / lev) / (1 - mmr), places=9)
            self.assertAlmostEqual(short_liq, 100.0 * (1 + 1 / lev) / (1 + mmr), places=9)
            self.assertLess(long_liq, 100.0)
            self.assertGreater(short_liq, 100.0)
        # Concrete anchors (1% MMR): LONG 3x 67.34, 15x 94.28, 50x 98.99 | SHORT 3x 132.01, 15x 105.61, 50x 100.99
        self.assertAlmostEqual(eft.estimate_isolated_liquidation_price("LONG", 100, 3), 67.3401, places=3)
        self.assertAlmostEqual(eft.estimate_isolated_liquidation_price("LONG", 100, 15), 94.2761, places=3)
        self.assertAlmostEqual(eft.estimate_isolated_liquidation_price("LONG", 100, 50), 98.9899, places=3)
        self.assertAlmostEqual(eft.estimate_isolated_liquidation_price("SHORT", 100, 3), 132.0132, places=3)
        self.assertAlmostEqual(eft.estimate_isolated_liquidation_price("SHORT", 100, 15), 105.6106, places=3)
        self.assertAlmostEqual(eft.estimate_isolated_liquidation_price("SHORT", 100, 50), 100.9901, places=3)

    def test_maintenance_amount_moves_liquidation_away(self):
        base = eft.estimate_isolated_liquidation_price("LONG", 100, 15, 0.01)
        with_cum = eft.estimate_isolated_liquidation_price("LONG", 100, 15, 0.01, maint_amount=5.0, qty=10)
        self.assertLess(with_cum, base)

    def _gate(self, direction, sl, lev, mmr=None):
        return eft.check_liquidation_gate(direction, 100.0, sl, lev, maint_margin_ratio=mmr)

    def test_long_cases(self):
        # (leverage, accepted SL, rejected SL) with the 80% buffer and 1% fallback MMR
        for lev, ok_sl, bad_sl in ((3, 75.0, 72.0), (15, 95.5, 95.3), (50, 99.2, 99.1)):
            ok, msg, d = self._gate("LONG", ok_sl, lev)
            self.assertTrue(ok, msg)
            ok, msg, d = self._gate("LONG", bad_sl, lev)
            self.assertFalse(ok, f"{lev}x SL {bad_sl} should be rejected")
            self.assertIn("Liquidation Gate", msg)
            self.assertIn(f"LONG {lev}x", msg)
            self.assertIn("est. isolated liquidation", msg)
            self.assertIn("MMR 1.00% [fallback]", msg)
            self.assertIn("80%", msg)

    def test_short_cases(self):
        for lev, ok_sl, bad_sl in ((3, 125.0, 126.0), (15, 104.4, 104.6), (50, 100.78, 100.82)):
            ok, msg, _ = self._gate("SHORT", ok_sl, lev)
            self.assertTrue(ok, msg)
            ok, msg, _ = self._gate("SHORT", bad_sl, lev)
            self.assertFalse(ok, f"{lev}x SL {bad_sl} should be rejected")
            self.assertIn(f"SHORT {lev}x", msg)
            self.assertIn("SL <=", msg)

    def test_sl_beyond_liquidation_rejected(self):
        ok, msg, d = self._gate("LONG", 90.0, 15)  # liq ~94.28
        self.assertFalse(ok)
        self.assertIn("at/beyond the liquidation price", msg)
        ok, msg, _ = self._gate("SHORT", 110.0, 15)  # liq ~105.61
        self.assertFalse(ok)
        self.assertIn("at/beyond the liquidation price", msg)

    def test_sl_on_wrong_side_rejected(self):
        ok, msg, _ = self._gate("LONG", 101.0, 3)
        self.assertFalse(ok)
        self.assertIn("wrong side", msg)
        ok, msg, _ = self._gate("SHORT", 99.0, 3)
        self.assertFalse(ok)
        self.assertIn("wrong side", msg)

    def test_higher_mmr_tightens_gate(self):
        ok, _, _ = self._gate("LONG", 95.5, 15, mmr=0.01)
        self.assertTrue(ok)
        ok, msg, _ = self._gate("LONG", 95.5, 15, mmr=0.025)
        self.assertFalse(ok)
        self.assertIn("MMR 2.50%", msg)

    def test_bracket_lookup_parsing_and_fallback(self):
        brackets = [{"symbol": "SOLUSDT", "brackets": [
            {"bracket": 2, "initialLeverage": 50, "notionalCap": 50000, "notionalFloor": 5000, "maintMarginRatio": 0.01, "cum": 25.0},
            {"bracket": 1, "initialLeverage": 75, "notionalCap": 5000, "notionalFloor": 0, "maintMarginRatio": 0.005, "cum": 0.0},
        ]}]
        with patch("execute_futures_trade.send_signed_request", return_value=brackets):
            self.assertEqual(eft.get_maint_margin_bracket("SOLUSDT", 1000.0), (0.005, 0.0, "leverageBracket"))
            self.assertEqual(eft.get_maint_margin_bracket("SOLUSDT", 10000.0), (0.01, 25.0, "leverageBracket"))
        with patch("execute_futures_trade.send_signed_request", return_value=brackets[0]):
            self.assertEqual(eft.get_maint_margin_bracket("SOLUSDT", 1000.0)[2], "leverageBracket")
        for bad in ({"error": "HTTP 401"}, [], None, {"code": -2015, "msg": "Invalid API-key"}):
            with patch("execute_futures_trade.send_signed_request", return_value=bad):
                self.assertEqual(eft.get_maint_margin_bracket("SOLUSDT", 1000.0), (eft.DEFAULT_MAINT_MARGIN_RATIO, 0.0, "fallback"))

    def test_mcp_gateway_maps_bracket_read(self):
        with patch("execute_futures_trade.call_binance_mcp", return_value=[]) as mock_mcp:
            eft.send_mcp_gateway_request("GET", "/fapi/v1/leverageBracket", {"symbol": "SOLUSDT"})
        mock_mcp.assert_called_once_with("futures_usds.notionalAndLeverageBrackets", {"symbol": "SOLUSDT"})

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    def test_mechanical_gates_include_liquidation_gate(self, _eq):
        with patch("user_profile.load_user_profile", return_value=dict(PROFILE)):
            ok, msg = eft.check_mechanical_gates("LONG", 100.0, 94.0, 103.0, 1.0, 15, target_env="testnet", is_yolo=True)
        self.assertFalse(ok)
        self.assertIn("Liquidation Gate", msg)


class TestLiquidationGateUsesEffectiveLeverage(unittest.TestCase):
    """The liquidation gate runs after setup confirms leverage (incl. the -4421 clamp) and before the entry."""

    def _execute(self, lev_responses, bracket=None):
        calls = []
        def fake(method, endpoint, params=None, target_env=None):
            calls.append((method, endpoint, dict(params or {})))
            if endpoint == "/fapi/v1/marginType":
                return {"code": 200, "msg": "success"}
            if endpoint == "/fapi/v1/leverage":
                return lev_responses.get(params["leverage"], {"symbol": params["symbol"], "leverage": params["leverage"]})
            if endpoint == "/fapi/v1/leverageBracket":
                return bracket if bracket is not None else {"error": "unavailable"}
            if endpoint == "/fapi/v1/ticker/price":
                return {"price": "100.0"}
            if endpoint == "/fapi/v1/order" and method == "POST":
                return {"orderId": 7, "avgPrice": "100.0", "status": "FILLED"}
            return {}
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("execute_futures_trade.get_symbol_filters", return_value=FILTERS), \
             patch("execute_futures_trade.place_algo_stop_loss", return_value={"algoId": 9}), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 9})), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch("builtins.print"), \
             patch("utils.atomic_writer.atomic_append_jsonl") as _no_audit_write, \
             patch("provenance_stamp.stamp_trade_record", side_effect=lambda rec, **kw: rec):
            res = eft.execute_complete_trade(
                symbol="PEPEUSDT", direction="LONG", leverage=15, margin_usdt=10.0,
                sl_price=94.0, tp1_price=110.0, tp2_price=120.0, target_env="testnet",
                is_yolo=True, bypass_eval_gate=True,
            )
        return res, calls

    def test_rejected_at_confirmed_15x_before_entry(self):
        res, calls = self._execute({})
        self.assertFalse(res["success"])
        self.assertTrue(res.get("hard_gate_rejection"))
        self.assertIn("Liquidation Gate", res["error"])
        self.assertIn("LONG 15x", res["error"])
        self.assertFalse([c for c in calls if c[1] == "/fapi/v1/order"])

    def test_passes_when_subaccount_clamp_confirms_5x(self):
        res, calls = self._execute({15: {"code": -4421, "msg": "Subaccounts are restricted from using leverage greater than 5x."}})
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["leverage"], 5)
        self.assertTrue([c for c in calls if c[1] == "/fapi/v1/order"])

    def test_bracket_mmr_is_used_when_available(self):
        bracket = [{"symbol": "PEPEUSDT", "brackets": [
            {"bracket": 1, "notionalFloor": 0, "notionalCap": 100000, "maintMarginRatio": 0.15, "cum": 0}]}]
        res, calls = self._execute({15: {"code": -4421, "msg": "Subaccounts are restricted from using leverage greater than 5x."}}, bracket)
        # 5x with a 15% MMR: liquidation ~94.1 -> SL 94 is beyond the 80% buffer
        self.assertFalse(res["success"])
        self.assertIn("[leverageBracket]", res["error"])
        self.assertFalse([c for c in calls if c[1] == "/fapi/v1/order"])


class TestEntryBasedRiskGates(unittest.TestCase):
    """Issue #22: PROD risk (GATE 2) and friction (GATE 3) gates are measured from the effective entry."""

    STATE_FILE = os.path.join(BASE_DIR, "logs", "session_state.json")

    def setUp(self):
        os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
        self._orig = _read_bytes(self.STATE_FILE) if os.path.exists(self.STATE_FILE) else None
        with open(self.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"is_valid": True, "last_updated_ts": int(time.time()),
                       "portfolio_exposure": {"delta_bias": "NEUTRAL"}}, f)
        # equity 1000 x 0.5% x 1.25 buffer = $6.25 PROD loss cap
        self._patches = [patch("quant_risk_engine.get_account_equity", return_value=1000.0),
                         patch("user_profile.load_user_profile", return_value=dict(PROFILE))]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        if self._orig is not None:
            with open(self.STATE_FILE, "wb") as f:
                f.write(self._orig)
        elif os.path.exists(self.STATE_FILE):
            os.remove(self.STATE_FILE)

    def test_long_loss_measured_from_trigger_above_current(self):
        # At cur_price 100: |100-95|*1.2 = $6.00 <= $6.25; from trigger 101: $7.20 > $6.25
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 95.0, 110.0, 1.2, 3, target_env="prod")
        self.assertTrue(ok, msg)
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 95.0, 110.0, 1.2, 3, target_env="prod", entry_price=101.0)
        self.assertFalse(ok)
        self.assertIn("Monetary risk exceeds allowed cap", msg)
        self.assertIn("entry ref 101.0", msg)

    def test_short_loss_measured_from_trigger_below_current(self):
        ok, msg = eft.check_mechanical_gates("SHORT", 100.0, 105.0, 90.0, 1.2, 3, target_env="prod")
        self.assertTrue(ok, msg)
        ok, msg = eft.check_mechanical_gates("SHORT", 100.0, 105.0, 90.0, 1.2, 3, target_env="prod", entry_price=99.0)
        self.assertFalse(ok)
        self.assertIn("Monetary risk exceeds allowed cap", msg)

    def test_friction_floor_measured_from_entry(self):
        # TP1 100.4 is 0.40% from the current price but 0.30% from the 100.1 trigger
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 100.4, 1.0, 3, target_env="prod")
        self.assertTrue(ok, msg)
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 100.4, 1.0, 3, target_env="prod", entry_price=100.1)
        self.assertFalse(ok)
        self.assertIn("below 0.35% friction floor", msg)
        self.assertIn("entry ref 100.1", msg)

    def test_long_tp1_below_trigger_rejected(self):
        # TP1 102.5 is 0.49% away from the 103 trigger but on the wrong side of a LONG entry
        ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 102.5, 1.0, 3, target_env="prod", entry_price=103.0)
        self.assertFalse(ok)
        self.assertIn("below 0.35% friction floor", msg)
        self.assertIn("wrong side of entry", msg)

    def test_short_tp1_above_trigger_rejected(self):
        ok, msg = eft.check_mechanical_gates("SHORT", 100.0, 102.0, 97.5, 1.0, 3, target_env="prod", entry_price=97.0)
        self.assertFalse(ok)
        self.assertIn("below 0.35% friction floor", msg)
        self.assertIn("wrong side of entry", msg)


class TestExecutorSizesAtEffectiveEntry(unittest.TestCase):
    """Issue #22: execute_complete_trade sizes and gates from the limit/trigger price of the order it sends."""

    def _execute(self, **kwargs):
        calls = []
        def fake(method, endpoint, params=None, target_env=None):
            calls.append((method, endpoint, dict(params or {})))
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
            if endpoint == "/fapi/v1/" + "algoOrder" and method == "POST":
                return {"algoId": 8}
            return {}
        args = dict(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                    sl_price=97.0, tp1_price=110.0, tp2_price=120.0, target_env="testnet", bypass_eval_gate=True)
        args.update(kwargs)
        gates = MagicMock(wraps=eft.check_mechanical_gates)
        ws = tempfile.mkdtemp()  # pending_entries.json of resting entries never touches the real logs/
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("execute_futures_trade._workspace_dir", return_value=ws), \
             patch("execute_futures_trade.check_mechanical_gates", gates), \
             patch("execute_futures_trade.get_symbol_filters", return_value=FILTERS), \
             patch("execute_futures_trade.place_algo_stop_loss", return_value={"algoId": 9}), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 9})), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch("builtins.print"), \
             patch("utils.atomic_writer.atomic_append_jsonl"), \
             patch("provenance_stamp.stamp_trade_record", side_effect=lambda rec, **kw: rec):
            res = eft.execute_complete_trade(**args)
        orders = [c[2] for c in calls if c[1] in ("/fapi/v1/order", "/fapi/v1/" + "algoOrder") and c[0] == "POST"]
        return res, orders, gates

    def _gate_kwargs(self, gates):
        gates.assert_called_once()
        return gates.call_args.kwargs

    def test_stop_market_not_breached_sized_and_gated_at_trigger(self):
        res, orders, gates = self._execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res.get("conditional_entry"))
        expected = eft.round_step(10.0 * 3 / 102.34, FILTERS["stepSize"], FILTERS["precision_qty"])
        self.assertEqual(expected, 0.293)
        self.assertEqual(orders[0]["type"], "STOP_MARKET")
        self.assertEqual(orders[0]["algoType"], "CONDITIONAL")
        self.assertEqual(orders[0]["triggerPrice"], 102.34)
        self.assertEqual(orders[0]["quantity"], expected)
        self.assertLess(orders[0]["quantity"], eft.round_step(30.0 / 100.0, FILTERS["stepSize"], FILTERS["precision_qty"]))
        kw = self._gate_kwargs(gates)
        self.assertEqual(kw.get("entry_price"), 102.34)
        self.assertEqual(kw.get("liq_entry_price"), 102.34)

    def test_default_sl_anchored_at_trigger(self):
        res, orders, gates = self._execute(order_type="STOP_MARKET", trigger_price=102.347,
                                           sl_price=None, tp1_price=None, tp2_price=None)
        self.assertTrue(res["success"], res.get("error"))
        self.assertAlmostEqual(gates.call_args.args[2], 102.34 * 0.98)
        self.assertAlmostEqual(gates.call_args.args[3], 102.34 * 1.03)

    def test_breached_trigger_uses_current_price(self):
        res, orders, gates = self._execute(order_type="STOP_MARKET", trigger_price=99.5)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(orders[0]["type"], "MARKET")
        self.assertEqual(orders[0]["quantity"], 0.3)
        kw = self._gate_kwargs(gates)
        self.assertEqual(kw.get("entry_price"), 100.0)
        self.assertEqual(kw.get("liq_entry_price"), 100.0)

    def test_limit_entry_uses_rounded_limit_price(self):
        res, orders, gates = self._execute(order_type="LIMIT", limit_price=98.767)
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res.get("pending_limit_entry"))
        expected = eft.round_step(30.0 / 98.76, FILTERS["stepSize"], FILTERS["precision_qty"])
        self.assertEqual(expected, 0.303)
        self.assertEqual(orders[0]["price"], 98.76)
        self.assertEqual(orders[0]["quantity"], expected)
        self.assertEqual(self._gate_kwargs(gates).get("entry_price"), 98.76)

    def test_market_entry_quantity_unchanged(self):
        res, orders, gates = self._execute(order_type="MARKET")
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(orders[0]["type"], "MARKET")
        self.assertEqual(orders[0]["quantity"], 0.3)
        self.assertEqual(self._gate_kwargs(gates).get("entry_price"), 100.0)


# =====================================================================================
# 5. Leverage configuration (single source of truth)
# =====================================================================================
class TestLeverageCeilingSingleSource(unittest.TestCase):

    def test_get_leverage_ceiling(self):
        self.assertEqual(up.get_leverage_ceiling({}), 15)
        self.assertEqual(up.get_leverage_ceiling({"leverage_ceiling": 25}), 25)
        self.assertEqual(up.get_leverage_ceiling({"leverage_ceiling": "30"}), 30)
        self.assertEqual(up.get_leverage_ceiling({"leverage_ceiling": 500}), up.MAX_LEVERAGE_CEILING)
        self.assertEqual(up.get_leverage_ceiling({"leverage_ceiling": 0}), up.DEFAULT_LEVERAGE_CEILING)
        self.assertEqual(up.get_leverage_ceiling({"leverage_ceiling": "abc"}), up.DEFAULT_LEVERAGE_CEILING)
        self.assertEqual(up.MAX_LEVERAGE_CEILING, 125)

    def test_generic_defaults(self):
        self.assertEqual(up.DEFAULT_PROFILE["leverage_standard"], 3)
        self.assertEqual(up.DEFAULT_PROFILE["leverage_yolo"], 15)
        self.assertEqual(up.DEFAULT_PROFILE["leverage_ceiling"], 15)
        with open(os.path.join(BASE_DIR, "config", "user_profile.json.example"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual(example["leverage_ceiling"], 15)
        self.assertEqual(example["leverage_yolo"], 15)

    def test_no_hardcoded_15x_ceiling_left(self):
        for rel in ("scripts/execute_futures_trade.py", "scripts/hooks/pre_trade_guard.py"):
            with open(os.path.join(BASE_DIR, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("leverage > 15", src, rel)
            self.assertIn("get_leverage_ceiling", src, rel)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    def test_executor_gate_follows_profile_ceiling(self, _eq):
        prof = dict(PROFILE, leverage_ceiling=20, leverage_yolo=20)
        with patch("user_profile.load_user_profile", return_value=prof):
            ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 103.0, 1.0, 20, target_env="testnet", is_yolo=True)
            self.assertTrue(ok, msg)
            ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 103.0, 1.0, 21, target_env="testnet", is_yolo=True)
            self.assertFalse(ok)
            self.assertIn("ceiling of 20x", msg)

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    def test_yolo_leverage_capped_by_profile(self, _eq):
        with patch("user_profile.load_user_profile", return_value=dict(PROFILE, leverage_yolo=5)):
            ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 103.0, 1.0, 10, target_env="testnet", is_yolo=True)
        self.assertFalse(ok)
        self.assertIn("YOLO leverage limit (5x", msg)

    def test_validate_leverage_setting(self):
        self.assertIsNone(up.validate_leverage_setting("leverage_yolo", 15, 15))
        self.assertIsNone(up.validate_leverage_setting("leverage_yolo", 1, 15))
        self.assertIsNotNone(up.validate_leverage_setting("leverage_yolo", 16, 15))
        self.assertIsNotNone(up.validate_leverage_setting("leverage_yolo", 0, 15))

    def test_cli_set_leverage_yolo_rejects_out_of_range_without_writing(self):
        profile_file = up.PROFILE_FILE
        before = _read_bytes(profile_file)
        for bad in ("0", "500"):
            proc = subprocess.run(
                [sys.executable, os.path.join(SCRIPTS_DIR, "user_profile.py"), "--set-leverage-yolo", bad],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("leverage_yolo", proc.stderr)
        after = _read_bytes(profile_file)
        self.assertEqual(before, after)


# =====================================================================================
# 6. trading_doctor PreToolUse hook self-test (isolated, synthetic hooks.json)
# =====================================================================================
GUARD_DENY = '''import json, sys
p = json.loads(sys.stdin.read())
tool = p["toolCall"]["args"].get("ToolName", "")
print(json.dumps({"decision": "deny" if tool.endswith("newOrder") else "allow", "reason": "test"}))
'''
GUARD_ALLOW = '''import json, sys
sys.stdin.read()
print(json.dumps({"decision": "allow"}))
'''
GUARD_EXIT2 = '''import json, sys
sys.stdin.read()
print(json.dumps({"decision": "deny"}))
sys.exit(2)
'''


@unittest.skipIf(os.name == "nt", "agy runs hooks via sh -c on POSIX; Windows path covered by test_windows_skips_selftest")
class TestDoctorHookSelfTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, ".agents"))
        os.makedirs(os.path.join(self.root, "scripts", "hooks"))
        os.makedirs(os.path.join(self.root, "logs"))

    def tearDown(self):
        self._tmp.cleanup()

    def _setup(self, guard_src, command=None, matcher="run_command|call_mcp_tool", enabled=True):
        with open(os.path.join(self.root, "scripts", "hooks", "guard.py"), "w", encoding="utf-8") as f:
            f.write(guard_src)
        command = command or f"{shlex.quote(sys.executable)} ../scripts/hooks/guard.py --agy"
        cfg = {"trading-safety-guard": {"enabled": enabled, "PreToolUse": [
            {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": 5}]}]}}
        with open(os.path.join(self.root, ".agents", "hooks.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def test_guard_that_denies_passes(self):
        self._setup(GUARD_DENY)
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertTrue(rep["ok"], rep)
        self.assertFalse(rep["critical"])
        self.assertTrue(any("denied" in m for m in rep["info"]))

    def test_guard_that_allows_is_critical(self):
        self._setup(GUARD_ALLOW)
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("did not deny" in m for m in rep["critical"]))

    def test_nonzero_exit_is_critical(self):
        self._setup(GUARD_EXIT2)
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("exited 2" in m for m in rep["critical"]))

    def test_missing_script_is_critical(self):
        self._setup(GUARD_DENY, command=f"{shlex.quote(sys.executable)} ../scripts/hooks/missing_guard.py --agy")
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("script not found" in m for m in rep["critical"]))

    def test_matcher_not_covering_call_mcp_tool_is_critical(self):
        self._setup(GUARD_DENY, matcher="run_command")
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("call_mcp_tool" in m for m in rep["critical"]))

    def test_disabled_hook_is_critical(self):
        self._setup(GUARD_DENY, enabled=False)
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertFalse(rep["ok"])

    def test_missing_hooks_json_is_critical(self):
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("hooks.json not found" in m for m in rep["critical"]))

    def test_heartbeat_reported_and_selftest_flagged(self):
        live_hb = os.path.join(self.root, "logs", "hook_heartbeat.json")
        self._setup(GUARD_DENY + (
            "import os\n"
            "assert os.environ.get('TRADING_HOOK_SELFTEST') == '1'\n"
            "hb = os.environ['PRE_TRADE_GUARD_HEARTBEAT_FILE']\n"
            f"assert os.path.abspath(hb) != os.path.abspath({live_hb!r})\n"
            "open(hb, 'w').write('{}')\n"
        ))
        now = time.time()
        with open(os.path.join(self.root, "logs", "hook_heartbeat.json"), "w", encoding="utf-8") as f:
            json.dump({"hook": "pre_trade_guard", "mode": "agy", "last_seen_ts": now - 120,
                       "last_seen_utc": "x", "tool": "run_command", "decision": "allow"}, f)
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertTrue(rep["ok"], rep)
        self.assertTrue(rep["heartbeat"]["present"])
        self.assertGreaterEqual(rep["heartbeat"]["age_s"], 119)
        self.assertTrue(any("heartbeat" in m.lower() for m in rep["info"]))
        # the self-test did not refresh the live heartbeat
        with open(live_hb, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["last_seen_ts"], now - 120)

    def test_no_heartbeat_is_warning_only(self):
        self._setup(GUARD_DENY)
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertTrue(rep["ok"])
        self.assertTrue(any("hook_heartbeat" in m for m in rep["warnings"]))

    def test_absolute_interpreter_warns(self):
        self._setup(GUARD_DENY, command=f"{sys.executable} ../scripts/hooks/guard.py --agy")
        rep = trading_doctor.check_pretool_hook(self.root)
        self.assertTrue(rep["ok"], rep)
        self.assertTrue(any("machine-specific interpreter" in m for m in rep["warnings"]))


class TestDoctorHookWindows(unittest.TestCase):
    def test_windows_skips_selftest(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".agents"))
            os.makedirs(os.path.join(root, "scripts", "hooks"))
            open(os.path.join(root, "scripts", "hooks", "guard.py"), "w").close()
            with open(os.path.join(root, ".agents", "hooks.json"), "w", encoding="utf-8") as f:
                json.dump({"g": {"PreToolUse": [{"matcher": "call_mcp_tool", "hooks": [
                    {"command": "python ../scripts/hooks/guard.py"}]}]}}, f)
            with patch("trading_doctor.os.name", "nt"), patch("subprocess.run") as mock_run:
                rep = trading_doctor.check_pretool_hook(root)
            mock_run.assert_not_called()
            self.assertTrue(rep["ok"])
            self.assertTrue(any("skipped on Windows" in m for m in rep["warnings"]))


if __name__ == "__main__":
    unittest.main()
