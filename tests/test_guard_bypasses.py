#!/usr/bin/env python3
"""
test_guard_bypasses.py - Regression tests for the PreToolUse/PostToolUse hook bypasses found in the
agy harness audit. Every bypass must now be DENIED; sanctioned flows must keep working.

Runs fully offline: outbound HTTP(S) is routed to a dead proxy and no Binance client is ever called.
"""

import os
import io
import sys
import json
import base64
import hashlib
import shutil
import time
import datetime
import tempfile
import subprocess
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
TESTS_DIR = os.path.join(BASE_DIR, "tests")
for _p in (SCRIPTS_DIR, HOOKS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pre_trade_guard  # noqa: E402
import post_trade_sync  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
from dossier_checklist_fixture import checklist_for  # noqa: E402  (the PROD gate re-checks it, issue #223)

GUARD_SCRIPT = os.path.join(HOOKS_DIR, "pre_trade_guard.py")
DEAD_PROXY = "http://127.0.0.1:9"
OFFLINE_ENV = {"HTTPS_PROXY": DEAD_PROXY, "HTTP_PROXY": DEAD_PROXY, "https_proxy": DEAD_PROXY,
               "http_proxy": DEAD_PROXY, "NO_PROXY": "", "no_proxy": ""}

EVALUATOR_CONV_ID = "0a1b2c3d-1111-4222-8333-444455556666"
PARENT_CONV_ID = "fedcba98-7777-4888-9999-000011112222"


def setUpModule():
    os.environ.update(OFFLINE_ENV)


def write_calibrated_store(root, now=None, env="PROD", n=30, expectancy=0.4):
    """Issue #202 fixture: a fresh logs/score_calibration.json whose Tier S buckets (80-89, 90-95) are calibrated
    (n trades with positive net expectancy), so the autonomous Tier S gate is exercised positively by default.
    Issue #207: Student-t bound (t95 df 29 = 1.699) above the +0.1R margin: 30 trades at +0.4R, sd 0.5R -> +0.245R."""
    lcb = round(expectancy - 1.699 * 0.5 / n ** 0.5, 4)
    stats = {"n": n, "wins": n // 2, "win_rate": 0.5, "expectancy_r_net": expectancy, "sd_r_net": 0.5,
             "lcb95_r_net": lcb, "mean_mfe_r": 1.0, "insufficient": n < 20, "calibrated": n >= 30 and lcb > 0.1}
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    with open(os.path.join(root, "logs", "score_calibration.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "score_schema_version": 2,  # issue #207: current score schema
                   "generated_at_ts": int(now if now is not None else time.time()), "env": env,
                   "min_trades": 30, "trades": {}, "unscored": 0, "out_of_range": 0,
                   "buckets": {"80-89": dict(stats), "90-95": dict(stats)}}, f)


def add_radar_snapshots(record):
    """Issue #202 fixture: the radar_snapshots record_evaluation.py joins in, one per scored candidate with the
    radar confidence equal to the dossier score (the gate requires the match)."""
    record["radar_snapshots"] = {
        f"{c['symbol']}|{c['direction']}": {"radar_snapshot": {"confidence": c["score"]}, "radar_snapshot_reason": None}
        for c in record.get("approved_candidates") or [] if c.get("score") is not None}
    return record


class GuardHarness(unittest.TestCase):
    """Isolated workspace (temp dir) with profile, fresh session state and a fake Antigravity brain."""

    def setUp(self):
        self._env = patch.dict(os.environ, dict(OFFLINE_ENV, WSL_DISTRO_NAME="Ubuntu"), clear=False)
        self._env.start()
        os.environ.pop("BINANCE_API_ENV", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.brain = os.path.join(self.root, "brain")
        os.environ["AGY_BRAIN_DIRS"] = self.brain
        os.makedirs(os.path.join(self.root, "logs", "evaluations"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "config"), exist_ok=True)
        with open(os.path.join(self.root, "config", "user_profile.json"), "w", encoding="utf-8") as f:
            json.dump({"profile_completed": True, "yolo_slot_enabled": True, "leverage_standard": 3,
                       "max_open_positions": 5, "autonomous_execution_tier_s": True}, f)
        self.dossier_path = os.path.join(self.root, "logs", "evaluations", "latest_dossier.json")
        self.write_session_state()
        write_calibrated_store(self.root)  # issue #202: Tier S fixtures below carry "score": 85

    def tearDown(self):
        self.tmp.cleanup()
        self._env.stop()

    # ------------------------------------------------------------------ fixtures
    def write_session_state(self, delta_bias="NEUTRAL"):
        state = {"is_valid": True, "last_updated_ts": int(time.time()),
                 "portfolio_exposure": {"delta_bias": delta_bias, "net_notional_delta_usdt": 0.0,
                                        "total_active_positions": 0},
                 "active_positions": []}
        with open(os.path.join(self.root, "logs", "session_state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)

    def write_legacy_dossier(self, symbol="BTCUSDT", direction="LONG"):
        now = int(time.time())
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump({"timestamp_ts": now, "valid_until_ts": now + 1200, "evaluator_agent": "isolated_market_evaluator",
                       "status": "APPROVED", "approved_symbols": [symbol],
                       "approved_candidates": [{"symbol": symbol, "direction": direction, "leverage": 3}]}, f)

    def write_provenance_dossier(self, symbol="BTCUSDT", direction="LONG", parent=PARENT_CONV_ID, extra=None):
        """Fake evaluator subagent transcript + dossier recorded exactly like --from-subagent does.
        `extra` overrides/adds candidate fields (tier, is_yolo, requires_user_confirmation, ...)."""
        conv_dir = os.path.join(self.brain, EVALUATOR_CONV_ID, ".system_generated", "logs")
        os.makedirs(conv_dir, exist_ok=True)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cand = {"symbol": symbol, "direction": direction, "tier": "Tier S", "leverage": 3, "score": 85}
        cand.update(extra or {})
        payload = {"status": "APPROVED", "summary": "test", "approved_candidates": [cand]}
        block = json.dumps(payload)
        steps = [
            {"source": "SYSTEM", "type": "USER_INPUT", "content": f"Subagent invoked sender={parent}", "step_index": 0},
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "step_index": 1, "created_at": created,
             "content": f"Master Dossier\n{checklist_for(payload)}<dossier_json>\n{block}\n</dossier_json>"},
        ]
        transcript = os.path.join(conv_dir, "transcript.jsonl")
        with open(transcript, "w", encoding="utf-8") as f:
            for s in steps:
                f.write(json.dumps(s) + "\n")
        record = add_radar_snapshots(dp.build_record_from_extraction(dp.extract_dossier_from_transcript(transcript)))
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return record

    # ------------------------------------------------------------------ runners
    def run_guard(self, payload, argv=None):
        stdin, stdout, stderr = sys.stdin, sys.stdout, sys.stderr
        try:
            sys.stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
            sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
            with patch("pre_trade_guard.find_workspace_root", return_value=self.root):
                code = pre_trade_guard.main(argv if argv is not None else [])
            out = sys.stdout.getvalue().strip()
            err = sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = stdin, stdout, stderr
        parsed = json.loads(out) if out else {}
        parsed["__exit_code__"] = code
        parsed["__stderr__"] = err
        return parsed

    def agy(self, payload):
        return self.run_guard(payload, argv=["--agy"])

    @staticmethod
    def cmd(command_line, **extra):
        payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": command_line}}}
        payload.update(extra)
        return payload

    @staticmethod
    def mcp(server, tool, arguments, name="call_mcp_tool", **extra):
        payload = {"toolCall": {"name": name, "args": {"ServerName": server, "ToolName": tool, "Arguments": arguments}}}
        payload.update(extra)
        return payload

    def assertDenied(self, res, fragment=None):
        self.assertEqual(res.get("decision"), "deny", res)
        if fragment:
            self.assertIn(fragment, res.get("reason", ""))


class TestMcpBypasses(GuardHarness):

    def test_retired_radar_server_denied_in_every_naming_form(self):
        self.write_legacy_dossier()
        args = {"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3, "target_env": "testnet"}
        forms = [
            self.mcp("crypto_radar", "deploy_futures_trade", args, name="mcp_tool"),
            self.mcp("\"crypto_radar\"", "\"get_open_positions\"", {}),
            self.mcp("crypto-radar", "scan_intraday_market", {}),
            {"toolCall": {"name": "mcp_crypto_radar_deploy_futures_trade", "args": args}},
            {"toolCall": {"name": "mcp_crypto_radar_get_crypto_newsletters", "args": {}}},
        ]
        for payload in forms:
            res = self.agy(payload)
            self.assertDenied(res, "Retired MCP Server")
            self.assertEqual(res.get("__exit_code__"), 0)
        claude = self.run_guard({"tool_name": "mcp__crypto_radar__close_position_market",
                                 "tool_input": {"symbol": "BTCUSDT"}})
        self.assertEqual(claude.get("__exit_code__"), 2)
        self.assertIn("--close-position --symbol", claude["__stderr__"])
        plugin = self.run_guard({"tool_name": "mcp__plugin_desk_crypto_radar__audit_and_trail_all_positions",
                                 "tool_input": {}})
        self.assertEqual(plugin.get("__exit_code__"), 2)
        self.assertIn("position_guardian_loop.py", plugin["__stderr__"])

    def test_retired_radar_reduce_only_argument_is_not_an_exemption(self):
        args = {"symbol": "BTCUSDT", "direction": "LONG", "reduceOnly": True}
        self.assertDenied(self.agy(self.mcp("crypto_radar", "deploy_futures_trade", args)), "Retired MCP Server")

    def test_legacy_radar_tool_names_denied_on_any_server_alias(self):
        for tool in ("deploy_futures_trade", "move_to_breakeven", "close_position_market", "audit_orphan_positions"):
            self.assertDenied(self.agy(self.mcp("some-alias", tool, {"symbol": "BTCUSDT"})), "Retired MCP Server")
        # Unrelated MCP tools keep the normal permission policy
        self.assertEqual(self.agy(self.mcp("notion", "notion-search", {"query": "x"})).get("decision"), "ask")

    def test_tool_execute_wrapping_new_order_denied(self):
        inner = {"toolName": "futures_usds.newOrder",
                 "arguments": {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"}}
        self.assertDenied(self.agy(self.mcp("binance", "tool_execute", inner)), "Choke Point Enforcement")
        # agy JSON-encodes argument values
        encoded = {"toolCall": {"name": "call_mcp_tool", "args": {
            "ServerName": "\"binance\"", "ToolName": "\"tool_execute\"", "Arguments": json.dumps(inner)}}}
        self.assertDenied(self.agy(encoded), "Choke Point Enforcement")
        # Unknown server alias still unwrapped by the inner Binance tool name
        self.assertDenied(self.agy(self.mcp("bnb-gateway", "tool_execute", inner)), "Choke Point Enforcement")

    def test_tool_execute_reduce_only_close_allowed(self):
        inner = {"toolName": "futures_usds.newOrder",
                 "arguments": {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "reduceOnly": "true"}}
        self.assertEqual(self.agy(self.mcp("binance", "tool_execute", inner)).get("decision"), "allow")

    def test_place_multiple_orders_denied(self):
        batch = [{"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"}]
        self.assertDenied(self.agy(self.mcp("binance", "futures_usds.placeMultipleOrders", {"batchOrders": batch})),
                          "Choke Point Enforcement")
        self.assertDenied(self.agy(self.mcp("binance", "tool_execute", {
            "toolName": "futures_usds.placeMultipleOrders", "arguments": {"batchOrders": json.dumps(batch)}})))

    def test_other_binance_write_tools_denied(self):
        for tool in ("futures_usds.modifyOrder", "futures_usds.changeMarginType", "futures_usds.changePositionMode",
                     "futures_usds.newAlgoOrder", "wallet.userUniversalTransfer", "create_spot_newOrder",
                     "futures_usds.someFutureWriteTool"):
            self.assertDenied(self.agy(self.mcp("binance", tool, {"symbol": "BTCUSDT", "side": "BUY"})))
        eager = {"toolCall": {"name": "mcp_binance_futures_usds.newOrder", "args": {"symbol": "BTCUSDT", "side": "BUY"}}}
        self.assertDenied(self.agy(eager), "Choke Point Enforcement")

    def test_binance_read_only_tools_pass_through_ask(self):
        for tool in ("futures_usds.positionInformationV2", "futures_usds.currentAllOpenOrders", "tool_search",
                     "spot.klines", "get_futures_usds_accountBalance"):
            self.assertEqual(self.agy(self.mcp("binance", tool, {"symbol": "BTCUSDT"})).get("decision"), "ask", tool)
        wrapped = {"toolName": "futures_usds.symbolPriceTicker", "arguments": {"symbol": "BTCUSDT"}}
        self.assertEqual(self.agy(self.mcp("binance", "tool_execute", wrapped)).get("decision"), "ask")

    def test_leverage_gate_through_tool_execute(self):
        inner = {"toolName": "futures_usds.changeInitialLeverage", "arguments": {"symbol": "BTCUSDT", "leverage": 20}}
        self.assertDenied(self.agy(self.mcp("binance", "tool_execute", inner)), "Leverage Gate")

    def _set_profile(self, **values):
        path = os.path.join(self.root, "config", "user_profile.json")
        with open(path, encoding="utf-8") as f:
            prof = json.load(f)
        prof.update(values)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prof, f)

    def _yolo_dossier(self, leverage):
        now = int(time.time())
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump({"timestamp_ts": now, "valid_until_ts": now + 1200, "evaluator_agent": "isolated_market_evaluator",
                       "status": "APPROVED", "approved_symbols": ["PEPEUSDT"],
                       "approved_candidates": [{"symbol": "PEPEUSDT", "direction": "LONG", "is_yolo": True,
                                                "leverage": leverage}]}, f)

    def test_leverage_ceiling_and_yolo_cap_from_profile(self):
        self._yolo_dossier(leverage=30)
        lev = lambda n: self.agy(self.mcp("binance", "futures_usds.changeInitialLeverage",
                                          {"symbol": "PEPEUSDT", "leverage": n}))
        # Default ceiling (user_profile.get_leverage_ceiling -> 15x)
        self.assertDenied(lev(16), "absolute desk ceiling of 15x")
        self.assertEqual(lev(15).get("decision"), "allow")
        # YOLO cap from profile leverage_yolo
        self._set_profile(leverage_yolo=10)
        self.assertDenied(lev(12), "YOLO leverage limit (10x")
        # Raised ceiling honoured
        self._set_profile(leverage_ceiling=25, leverage_yolo=20)
        self.assertEqual(lev(20).get("decision"), "allow")
        self.assertDenied(lev(26), "absolute desk ceiling of 25x")

    def test_trade_opening_respects_yolo_cap(self):
        self._yolo_dossier(leverage=15)
        self._set_profile(leverage_yolo=10)
        res = self.agy(self.cmd("python3 scripts/execute_futures_trade.py --symbol PEPEUSDT --direction LONG "
                                "--leverage 12 --is-yolo --env testnet"))
        self.assertDenied(res, "YOLO leverage limit")


class TestRunCommandBypasses(GuardHarness):

    def test_inline_python_send_signed_request_denied(self):
        c = ("python3 -c \"import sys; sys.path.insert(0,'scripts'); import execute_futures_trade as e; "
             "e.send_signed_request('POST','/fapi/v1/order',{'symbol':'BTCUSDT','side':'BUY'})\"")
        self.assertDenied(self.agy(self.cmd(c)), "Inline code")

    def test_heredoc_and_piped_interpreter_denied(self):
        heredoc = "cd scripts && python3 - <<'EOF'\nimport execute_futures_trade as e\ne.place_algo_stop_loss('BTCUSDT','SELL',1)\nEOF"
        self.assertDenied(self.agy(self.cmd(heredoc)))
        piped = "echo 'python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG' | bash"
        self.assertDenied(self.agy(self.cmd(piped)))

    def test_curl_post_to_fapi_denied(self):
        c = "curl -s -X POST -H 'X-MBX-APIKEY: k' 'https://fapi.binance.com/fapi/v1/order?symbol=BTCUSDT&side=BUY&signature=x'"
        self.assertDenied(self.agy(self.cmd(c)), "Raw HTTP write")
        self.assertDenied(self.agy(self.cmd("wget --post-data 'a=b' https://testnet.binancefuture.com/fapi/v1/order")))

    def test_unsanctioned_script_with_trading_primitives_denied(self):
        script = os.path.join(self.root, "scratch_trade.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import execute_futures_trade as e\ne.send_signed_request('POST', '/fapi/v1/order', {})\n")
        self.assertDenied(self.agy(self.cmd(f"python3 {script}")), "outside scripts/")

    def test_chained_risk_flag_does_not_whitelist_trade(self):
        c = ("python3 scripts/execute_futures_trade.py --symbol SOLUSDT --direction LONG && "
             "python3 scripts/trading_doctor.py --heal")
        self.assertDenied(self.agy(self.cmd(c)), "Clean-Room Evaluator Required")

    def test_chained_protect_pending_does_not_whitelist_trade(self):
        c = ("python3 scripts/execute_futures_trade.py --symbol SOLUSDT --direction LONG && "
             "python3 scripts/execute_futures_trade.py --protect-pending")
        self.assertDenied(self.agy(self.cmd(c)), "Clean-Room Evaluator Required")
        self.assertEqual(self.agy(self.cmd("python3 scripts/execute_futures_trade.py --protect-pending --env prod")).get("decision"), "allow")

    def test_batch_deploy_scripts_and_auto_deploy_denied(self):
        self.assertDenied(self.agy(self.cmd("python3 scripts/deploy_fresh_basket.py --help")))
        self.assertDenied(self.agy(self.cmd("python3 scripts/deploy_fomc_batch.py --env prod")), "Batch deploy")
        self.assertDenied(self.agy(self.cmd("python3 scripts/loops/climax_watcher_loop.py --once --auto-deploy --env prod")))

    def test_reading_executor_source_is_not_a_trade(self):
        for c in ("sed -n 1,40p scripts/execute_futures_trade.py",
                  "nl scripts/execute_futures_trade.py | head -20",
                  "cut -c1-80 scripts/execute_futures_trade.py"):
            out = self.agy(self.cmd(c))
            self.assertNotEqual(out.get("decision"), "deny", c)

    def test_multiple_trade_openings_in_one_command_denied(self):
        self.write_legacy_dossier()
        c = ("python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --env testnet; "
             "python3 scripts/execute_futures_trade.py --symbol ETHUSDT --direction LONG --env testnet")
        self.assertDenied(self.agy(self.cmd(c)), "one trade opening per command")

    def test_inline_env_assignment_resolves_prod(self):
        self.write_legacy_dossier()
        c = "BINANCE_API_ENV=prod python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG"
        self.assertDenied(self.agy(self.cmd(c)), "Legacy dossier format")

    def test_echo_into_latest_dossier_denied(self):
        c = "echo '{\"status\":\"APPROVED\",\"approved_symbols\":[\"BTCUSDT\"]}' > logs/evaluations/latest_dossier.json"
        self.assertDenied(self.agy(self.cmd(c)), "Evaluation Trail Protection")
        self.assertDenied(self.agy(self.cmd("cp /tmp/forged.json logs/evaluations/")))
        self.assertDenied(self.agy(self.cmd("cat ~/.gemini/antigravity/brain/x/.system_generated/logs/transcript.jsonl")))

    def test_full_transcript_access_denied(self):
        for c in ("cat ~/.gemini/antigravity/brain/x/.system_generated/logs/transcript_full.jsonl",
                  "sed -i 's/REJECTED/APPROVED/' /home/u/.gemini/antigravity/brain/x/.system_generated/logs/transcript_full.jsonl",
                  "cp /tmp/forged.jsonl ~/.gemini/antigravity-cli/brain/x/.system_generated/logs/transcript_full.jsonl"):
            self.assertDenied(self.agy(self.cmd(c)), "Evaluation Trail Protection")

    def test_session_state_forgery_denied(self):
        c = "echo '{\"is_valid\": true, \"portfolio_exposure\": {\"delta_bias\": \"NEUTRAL\"}}' > logs/session_state.json"
        self.assertDenied(self.agy(self.cmd(c)), "Ground Truth Protection")
        self.assertEqual(self.agy(self.cmd("cat logs/session_state.json")).get("decision"), "ask")

    def test_record_evaluation_manual_paths_denied_in_prod(self):
        c = "python3 scripts/record_evaluation.py --env prod --symbols BTCUSDT --directions LONG"
        self.assertDenied(self.agy(self.cmd(c)), "Manual dossier recording is disabled")
        with patch.dict(os.environ, {"BINANCE_API_ENV": "prod"}):
            self.assertDenied(self.agy(self.cmd("python3 scripts/record_evaluation.py --json-file d.json")))
        ok = self.agy(self.cmd(f"python3 scripts/record_evaluation.py --from-subagent {EVALUATOR_CONV_ID} --env prod"))
        self.assertEqual(ok.get("decision"), "ask")
        testnet = self.agy(self.cmd("python3 scripts/record_evaluation.py --env testnet --symbols BTCUSDT"))
        self.assertEqual(testnet.get("decision"), "ask")

    def test_harness_and_profile_changes_force_ask(self):
        self.assertEqual(self.agy(self.cmd("sed -i 's/deny/allow/' scripts/hooks/pre_trade_guard.py")).get("decision"),
                         "force_ask")
        self.assertEqual(self.agy(self.cmd("python3 scripts/user_profile.py --set-autonomous-tier-s true")).get("decision"),
                         "force_ask")

    def test_harmless_command_is_ask_and_risk_reducing_is_allow(self):
        self.assertEqual(self.agy(self.cmd("ls -la")).get("decision"), "ask")
        self.assertEqual(self.agy(self.cmd("rm -rf build && python3 scripts/trading_doctor.py --heal")).get("decision"),
                         "ask")
        res = self.agy(self.cmd("python3 scripts/execute_futures_trade.py --symbol BTCUSDT --close-position --env prod"))
        self.assertEqual(res.get("decision"), "allow")


class TestDossierProvenance(GuardHarness):

    def deploy(self, direction="LONG", env="prod", symbol="BTCUSDT", **extra):
        return self.agy(self.cmd(f"python3 scripts/execute_futures_trade.py --symbol {symbol} --direction {direction} "
                                 f"--leverage 3 --env {env}", **extra))

    def test_forged_legacy_dossier_rejected_in_prod(self):
        self.write_legacy_dossier()
        self.assertDenied(self.deploy(), "Legacy dossier format")
        self.assertDenied(self.agy(self.cmd(
            "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --env prod")), "Legacy dossier")

    def test_tampered_parent_conversation_id_ignored(self):
        record = self.write_provenance_dossier(parent=PARENT_CONV_ID)
        record["parent_conversation_id"] = "11111111-2222-4333-8444-555566667777"
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        self.assertDenied(self.deploy(conversationId="11111111-2222-4333-8444-555566667777"), "not for the current")

    def test_forged_v2_dossier_without_transcript_rejected(self):
        record = self.write_provenance_dossier()
        record["approved_symbols"] = ["BTCUSDT", "ETHUSDT"]
        record["approved_candidates"].append({"symbol": "ETHUSDT", "direction": "LONG"})
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        # Hash still matches the transcript, but the tampered symbol list is not what the evaluator emitted
        self.assertDenied(self.deploy(symbol="ETHUSDT"), "from what the evaluator")
        os.remove(os.path.join(self.brain, EVALUATOR_CONV_ID, ".system_generated", "logs", "transcript.jsonl"))
        self.assertDenied(self.deploy(), "transcript not found")

    def test_valid_provenance_dossier_allows_deploy(self):
        self.write_provenance_dossier()
        res = self.deploy(conversationId=PARENT_CONV_ID)
        self.assertEqual(res.get("decision"), "allow", res)
        self.assertEqual(res.get("__exit_code__"), 0)
        self.assertEqual(set(res) - {"__exit_code__", "__stderr__"}, {"decision", "reason"})

    def test_truncated_transcript_resolved_from_full_transcript_allows_deploy(self):
        """agy truncates the send_message in transcript.jsonl; the guard re-derives it from transcript_full.jsonl."""
        conv_dir = os.path.join(self.brain, EVALUATOR_CONV_ID, ".system_generated", "logs")
        os.makedirs(conv_dir, exist_ok=True)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {"status": "APPROVED", "target_env": "PROD", "summary": "test",
                   "approved_candidates": [{"symbol": "BTCUSDT", "direction": "LONG", "tier": "Tier S",
                                            "leverage": 3, "score": 85}]}
        block = json.dumps(payload)
        message = ("Master Dossier — régimen σ\n" * 30 + checklist_for(payload)
                   + f"<dossier_json>\n{block}\n</dossier_json>")
        encoded = json.dumps(message, ensure_ascii=False)
        removed = len(encoded.encode("utf-8")) - len(encoded[:50].encode("utf-8"))
        system = {"source": "SYSTEM", "type": "USER_INPUT", "content": f"Subagent invoked sender={PARENT_CONV_ID}",
                  "step_index": 0}
        short = {"source": "MODEL", "type": "PLANNER_RESPONSE", "step_index": 1, "created_at": created, "content": "",
                 "tool_calls": [{"name": "send_message", "args": {
                     "Message": f"{encoded[:50]}\n<truncated {removed} bytes>",
                     "Recipient": json.dumps(PARENT_CONV_ID)}}],
                 "truncated_fields": ["tool_calls"]}
        full = {k: v for k, v in short.items() if k != "truncated_fields"}
        full["tool_calls"] = [{"name": "send_message", "args": {"Message": message, "Recipient": PARENT_CONV_ID}}]
        transcript = os.path.join(conv_dir, "transcript.jsonl")
        for path, rows in ((transcript, (system, short)), (dp.full_transcript_path(transcript), (system, full))):
            with open(path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        record = add_radar_snapshots(dp.build_record_from_extraction(dp.extract_dossier_from_transcript(transcript)))
        self.assertTrue(record["provenance"]["full_transcript_used"])
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        res = self.deploy(conversationId=PARENT_CONV_ID)
        self.assertEqual(res.get("decision"), "allow", res)
        os.remove(dp.full_transcript_path(transcript))
        self.assertDenied(self.deploy(conversationId=PARENT_CONV_ID), "transcript_full.jsonl")

    def test_direction_mismatch_denied(self):
        self.write_provenance_dossier(direction="LONG")
        self.assertDenied(self.deploy(direction="SHORT"), "but the order is SHORT")

    def test_dossier_from_other_conversation_denied(self):
        self.write_provenance_dossier(parent=PARENT_CONV_ID)
        self.assertDenied(self.deploy(conversationId="11111111-2222-4333-8444-555566667777"), "not for the current")

    def test_testnet_relaxed_legacy_dossier_still_allowed(self):
        self.write_legacy_dossier()
        self.assertEqual(self.deploy(env="testnet").get("decision"), "allow")


class TestFileWriteProtection(GuardHarness):

    def write(self, target, content="{}", name="write_to_file"):
        return self.agy({"toolCall": {"name": name, "args": {"TargetFile": target, "CodeContent": content}}})

    def test_write_to_evaluation_trail_denied(self):
        self.assertDenied(self.write("logs/evaluations/latest_dossier.json"), "Evaluation Trail Protection")
        self.assertDenied(self.write(os.path.join(self.root, "logs", "evaluations", "x.json")))
        self.assertDenied(self.write("logs/../logs/evaluations/latest_dossier.json", name="replace_file_content"))
        self.assertDenied(self.write("/home/user/.gemini/antigravity/brain/abc/.system_generated/logs/transcript.jsonl",
                                     name="multi_replace_file_content"))

    def test_write_to_full_transcript_denied(self):
        target = "/home/user/.gemini/antigravity/brain/abc/.system_generated/logs/transcript_full.jsonl"
        for name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            self.assertDenied(self.write(target, name=name), "Evaluation Trail Protection")
        claude = self.run_guard({"tool_name": "Write", "tool_input": {"file_path": target, "content": "{}"}})
        self.assertEqual(claude.get("__exit_code__"), 2)

    def test_harness_files_force_ask_and_others_ask(self):
        self.assertEqual(self.write("scripts/hooks/pre_trade_guard.py").get("decision"), "force_ask")
        self.assertEqual(self.write(".agents/hooks.json").get("decision"), "force_ask")
        self.assertEqual(self.write("scripts/utils/dossier_provenance.py").get("decision"), "force_ask")
        self.assertEqual(self.write("docs/notes.md").get("decision"), "ask")
        self.assertEqual(self.write("scratch/t.py", "send_signed_request('POST', '/fapi/v1/order')").get("decision"),
                         "force_ask")


class TestGroundTruthProtection(GuardHarness):
    """Issue #37: guardian_state.json / pending_entries.json gate PROD orders like session_state.json."""

    WRITERS = {
        "session_state.json": "scripts/sync_session_state.py",
        "guardian_state.json": "scripts/loops/position_guardian_loop.py",
        "pending_entries.json": "scripts/execute_futures_trade.py",
        "hook_heartbeat.json": "scripts/hooks/pre_trade_guard.py",
        "score_calibration.json": "scripts/trading_scorecard.py",  # issue #202
        "trade_outcomes.jsonl": "scripts/trade_outcomes.py",       # issue #202: the store's only input
        "trades_audit.jsonl": "scripts/execute_futures_trade.py",  # issue #202 (PR #204): the outcomes' source
        "primed_brief.json": "scripts/prime_evaluator_brief.py",   # issue #202: the evaluator's input
        "primed_brief_scores.json": "scripts/prime_evaluator_brief.py",
    }
    NEW_FILES = ("guardian_state.json", "pending_entries.json", "hook_heartbeat.json", "score_calibration.json",
                 "trade_outcomes.jsonl", "trades_audit.jsonl", "primed_brief.json", "primed_brief_scores.json")

    def assertGroundTruthDenied(self, res, name, label=""):
        self.assertDenied(res, "Ground Truth Protection")
        self.assertIn(f"logs/{name} may only be written by", res.get("reason", ""), label)
        self.assertIn(self.WRITERS[name], res.get("reason", ""), label)

    def assertNotGroundTruth(self, res, label=""):
        self.assertNotIn("Ground Truth Protection", res.get("reason", "") + res.get("__stderr__", ""), label)

    def shell_vectors(self, name):
        p = f"logs/{name}"
        return [
            f"echo '{{\"mode\": \"loop\"}}' > {p}",
            f"echo '{{}}' >> {p}",
            f"printf x >| {p}",
            f"echo '{{}}' | tee {p}",
            f"rm {p}",
            f"rm -f ./{p}",
            f"mv /tmp/forged.json {p}",
            f"cp /tmp/forged.json {p}",
            f"sed -i 's/old/new/' {p}",
            f"truncate -s 0 {p}",
            f"dd if=/tmp/forged.json of={p}",
            f"git restore {p}",
            f"python3 -c \"import json; json.dump({{'mode': 'loop'}}, open('{p}', 'w'))\"",
            f"python3 -c \"from pathlib import Path; Path('{p}').write_text('{{}}')\"",
            f"python3 -c \"import pathlib; pathlib.Path('{p}').unlink()\"",
            f"node -e \"require('fs').writeFileSync('{p}', '{{}}')\"",
            f"python3 - <<'EOF'\nimport json\nwith open('{p}', 'w') as f:\n    json.dump({{'mode': 'loop'}}, f)\nEOF",
            f"python3 - <<'EOF'\nfrom pathlib import Path\nstate = Path('{p}')\nstate.write_text('{{}}')\nEOF",
            f"find logs -name {name} -delete",
            f"find . -path './{p}' -exec rm {{}} \\;",
        ]

    def test_shell_write_vectors_denied_for_new_files(self):
        for name in self.NEW_FILES:
            for c in self.shell_vectors(name):
                self.assertGroundTruthDenied(self.agy(self.cmd(c)), name, c)

    def test_new_shell_vectors_denied_for_session_state(self):
        for c in ("python3 - <<'EOF'\nimport json\njson.dump({'is_valid': True}, open('logs/session_state.json', 'w'))\nEOF",
                  "python3 -c \"from pathlib import Path; Path('logs/session_state.json').write_text('{}')\"",
                  "find logs -name session_state.json -delete",
                  "find logs -name 'session_*' -exec rm -f {} +"):
            self.assertGroundTruthDenied(self.agy(self.cmd(c)), "session_state.json", c)

    def test_session_state_message_unchanged(self):
        res = self.agy(self.cmd("echo '{}' > logs/session_state.json"))
        self.assertEqual(res.get("reason"), "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Ground Truth Protection): "
                                            "logs/session_state.json may only be written by "
                                            "`python3 scripts/sync_session_state.py`.")

    def test_claude_code_bash_write_denied(self):
        res = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "echo '{}' > logs/guardian_state.json"}})
        self.assertEqual(res.get("__exit_code__"), 2)
        self.assertIn("logs/guardian_state.json may only be written by", res["__stderr__"])

    def test_logs_directory_and_matching_globs_denied(self):
        for c in ("rm -rf logs", "rm -r ./logs/", f"rm -rf {self.root}/logs", "mv logs logs.old",
                  "shred -u logs/*", "rm logs/*.json", "rm logs/*state*", "rm -f logs/{guardian_state,x}.json",
                  "find logs -type f -delete", "find logs -name '*.json' -delete",
                  "cp -r /tmp/forged/. logs/", "cp /tmp/forged/* logs/", "rsync -a /tmp/forged/ logs/",
                  "echo '{}' | tee logs/guardian_stat?.json"):
            res = self.agy(self.cmd(c))
            self.assertDenied(res, "Ground Truth Protection")
            self.assertIn("logs/guardian_state.json may only be written by", res.get("reason", ""), c)
        both = self.agy(self.cmd("rm logs/*state*")).get("reason", "")
        self.assertIn("logs/session_state.json may only be written by", both)
        self.assertNotIn("pending_entries", both)
        self.assertIn("logs/pending_entries.json", self.agy(self.cmd("rm -rf logs")).get("reason", ""))

    def test_globs_and_files_that_cannot_match_are_not_ground_truth(self):
        for c in ("rm logs/*.log", "rm logs/guardian_actions.jsonl", "echo x >> logs/guardian.log",
                  "find logs -name '*.log' -delete", "mv report.txt logs/", "cp report.txt logs/",
                  "rm -rf logs/pr_review", "rm -rf build"):
            res = self.agy(self.cmd(c))
            self.assertNotGroundTruth(res, c)
            self.assertNotEqual(res.get("decision"), "deny", c)

    def test_reads_keep_previous_decision(self):
        for c in ("cat logs/guardian_state.json", "jq . logs/pending_entries.json", "tail -n 5 logs/guardian_state.json",
                  "grep -c symbol logs/pending_entries.json", "cat logs/session_state.json",
                  "python3 -c \"import json; print(json.load(open('logs/guardian_state.json')))\"",
                  "cp logs/guardian_actions.jsonl /tmp/actions.jsonl"):
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "ask", c)

    def test_desk_writers_unchanged(self):
        self.assertEqual(self.agy(self.cmd("python3 scripts/execute_futures_trade.py --protect-pending --env prod"))
                         .get("decision"), "allow")
        self.assertEqual(self.agy(self.cmd("python3 scripts/loops/position_guardian_loop.py --once")).get("decision"),
                         "allow")
        self.assertNotGroundTruth(self.agy(self.cmd("python3 scripts/loops/position_guardian_loop.py --interval 60")))
        self.assertNotGroundTruth(self.agy(self.cmd("python3 scripts/sync_session_state.py")))

    def test_file_tools_denied_for_every_path_form(self):
        for name in self.NEW_FILES:
            targets = [f"logs/{name}", f"./logs/{name}", f"logs/../logs/{name}", os.path.join(self.root, "logs", name),
                       f"C:\\Users\\x\\repo\\logs\\{name}", f"C:/Users/x/repo/LOGS/{name.upper()}"]
            for target in targets:
                for tool in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
                    res = self.agy({"toolCall": {"name": tool, "args": {"TargetFile": target, "CodeContent": "{}"}}})
                    self.assertGroundTruthDenied(res, name, f"{tool} {target}")
                for tool in ("Write", "Edit", "MultiEdit"):
                    res = self.run_guard({"tool_name": tool, "tool_input": {"file_path": target, "content": "{}"}})
                    self.assertEqual(res.get("__exit_code__"), 2, f"{tool} {target}")
                    self.assertIn(f"logs/{name} may only be written by", res["__stderr__"], f"{tool} {target}")
                    self.assertIn(self.WRITERS[name], res["__stderr__"])

    def test_windows_path_to_session_state_denied(self):
        for target in ("C:\\Users\\x\\repo\\logs\\session_state.json", "file:///C:/Users/x/repo/logs/session_state.json"):
            res = self.run_guard({"tool_name": "Write", "tool_input": {"file_path": target, "content": "{}"}})
            self.assertEqual(res.get("__exit_code__"), 2, target)
            self.assertIn("logs/session_state.json may only be written by", res["__stderr__"])
            self.assertGroundTruthDenied(self.agy({"toolCall": {"name": "write_to_file", "args": {
                "TargetFile": target, "CodeContent": "{}"}}}), "session_state.json", target)

    def test_unrelated_log_files_keep_normal_policy(self):
        for target in ("logs/guardian_actions.jsonl", "logs/guardian.log", "docs/guardian_state.md",
                       "logs/old/guardian_state.json.bak"):
            res = self.agy({"toolCall": {"name": "write_to_file", "args": {"TargetFile": target, "CodeContent": "x"}}})
            self.assertEqual(res.get("decision"), "ask", target)

    # ---------------------------------------------------------------- review round 1 findings
    def assertAllGroundTruthDenied(self, commands):
        for c in commands:
            res = self.agy(self.cmd(c))
            self.assertDenied(res, "Ground Truth Protection")
            self.assertIn("logs/guardian_state.json may only be written by", res.get("reason", ""), c)
            self.assertIn("logs/pending_entries.json may only be written by", res.get("reason", ""), c)

    def assertNoneGroundTruth(self, commands):
        for c in commands:
            res = self.agy(self.cmd(c))
            self.assertNotGroundTruth(res, c)
            self.assertNotEqual(res.get("decision"), "deny", c)

    def test_punctuation_run_redirects_denied(self):
        self.assertEqual(pre_trade_guard._tokenize("(printf x)>logs/a; echo 1<>b;>c"),
                         ["(", "printf", "x", ")", ">", "logs/a", ";", "echo", "1", "<>", "b", ";", ">", "c"])
        for c, name in (("(printf x)>logs/guardian_state.json", "guardian_state.json"),
                        ("echo x 1<>logs/pending_entries.json", "pending_entries.json"),
                        ("echo x;>logs/guardian_state.json", "guardian_state.json"),
                        ("(cat /tmp/forged.json)>>logs/guardian_state.json", "guardian_state.json"),
                        ("cat /tmp/forged.json 1<>logs/pending_entries.json", "pending_entries.json"),
                        ("{ cat /tmp/forged.json; }>logs/session_state.json", "session_state.json")):
            self.assertGroundTruthDenied(self.agy(self.cmd(c)), name, c)
        self.assertTrue(pre_trade_guard._is_redirect(")>") and pre_trade_guard._is_redirect("<>"))
        self.assertFalse(pre_trade_guard._is_redirect("<") or pre_trade_guard._is_redirect(")"))

    def test_logs_dir_globs_and_braces_denied(self):
        self.assertAllGroundTruthDenied(["rm -rf {logs,build}", "rm -rf log*", "rm -rf lo[g]s", "mv log? /tmp/x",
                                         "rm -rf *", "rm -rf ..", "rm -rf ../*", f"rm -rf {self.root}",
                                         f"rm -rf {self.root}/lo*", "mv * /tmp/x", "shred -u lo?s/*"])
        self.assertNoneGroundTruth(["rm -rf build/*", "rm -rf /nonexistent/x/*", "rm -rf {build,dist}",
                                    "cp /tmp/x/* /nonexistent/dest/", "rm *.pyc", "mv dist/* /tmp/x"])

    def test_unfiltered_or_negated_find_denied(self):
        self.assertAllGroundTruthDenied([
            "find . -name '*.json' ! -name package.json -delete", "find . -type f -mmin -5 -delete",
            "find . -regex '.*state.*' -delete", "find . -not -name '*.log' -delete", "find .. -type f -delete",
            "find / -newer /tmp/x -delete", "find ~ -type f -delete", "find -L . -type f -delete",
            "find . \\( -type f \\) -delete", "find . -name '*.log' -o -name '*.tmp' -delete",
            f"find {self.root} -mmin -5 -delete", "find logs -type f -exec sh -c 'rm \"$0\"' {} \\;",
            "find . -type f -exec awk -i inplace 1 {} +", "find -type f -delete",
        ])
        self.assertNoneGroundTruth(["find . -name '*.log' -delete", "find build -type f -delete",
                                    "find /nonexistent/build -type f -delete", "find . -type f -exec wc -l {} +",
                                    "find . -type f -exec sed -n 1p {} \\;", "find . -fprint /tmp/list",
                                    "find . -name guardian_state.json", "find logs -name '*.json'"])
        self.assertGroundTruthDenied(self.agy(self.cmd("find . -fprint logs/guardian_state.json")),
                                     "guardian_state.json")

    def test_option_attached_target_directory_denied(self):
        self.assertAllGroundTruthDenied([
            "cp --target-directory=logs /tmp/f/*", "cp -tlogs /tmp/f/*", "mv --target-directory=logs /tmp/f/*",
            "install --target-directory=logs /tmp/f/*", "cp -t logs -r /tmp/f/.", "cp -rtlogs /tmp/f/.",
            "mv -t ./logs/ /tmp/f/*", "cp --target=logs /tmp/f/*", "cp -r /tmp/f/* .",
        ])
        self.assertNoneGroundTruth(["mv -t logs report.txt", "cp -t logs report.txt", "cp --target-directory=/tmp/x logs/*.log"])

    def test_symlink_aliases_of_logs_dir_denied(self):
        self.assertAllGroundTruthDenied(["ln -s logs st", "ln -s ./logs/ st", f"ln -s {self.root}/logs st",
                                         "ln -sT lo* st", "ln -s -t /tmp/x logs", "cmd //c mklink /J st logs"])
        self.assertGroundTruthDenied(self.agy(self.cmd("ln -s /tmp/forged.json logs/guardian_state.json")),
                                     "guardian_state.json")
        self.assertNoneGroundTruth(["ln -s /tmp/x logs/x", "ln -s scripts/foo.py bar.py"])

    def test_file_tool_realpath_and_hard_link_aliases_denied(self):
        logs = os.path.join(self.root, "logs")
        try:
            os.symlink(logs, os.path.join(self.root, "st"), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with open(os.path.join(logs, "pending_entries.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        os.link(os.path.join(logs, "pending_entries.json"), os.path.join(self.root, "pe.json"))
        cases = [("st/guardian_state.json", "guardian_state.json"),
                 (os.path.join(self.root, "st", "pending_entries.json"), "pending_entries.json"),
                 ("st/../st/session_state.json", "session_state.json"),
                 ("pe.json", "pending_entries.json")]
        for target, name in cases:
            self.assertGroundTruthDenied(self.agy({"toolCall": {"name": "write_to_file", "args": {
                "TargetFile": target, "CodeContent": "{}"}}}), name, target)
            res = self.run_guard({"tool_name": "Write", "tool_input": {"file_path": target, "content": "{}"}})
            self.assertEqual(res.get("__exit_code__"), 2, target)
            self.assertIn(f"logs/{name} may only be written by", res["__stderr__"], target)
        res = self.agy({"toolCall": {"name": "write_to_file", "args": {"TargetFile": "st/guardian.log", "CodeContent": "x"}}})
        self.assertEqual(res.get("decision"), "ask")

    def test_write_programs_outside_denylist_denied(self):
        cases = [
            ("jq '.mode=\"loop\"' /tmp/x.json | sponge logs/guardian_state.json", "guardian_state.json"),
            ("curl -o logs/guardian_state.json http://127.0.0.1:9/x", "guardian_state.json"),
            ("wget -O logs/guardian_state.json http://127.0.0.1:9/x", "guardian_state.json"),
            ("awk -i inplace '{print}' logs/pending_entries.json", "pending_entries.json"),
            ("sort -o logs/pending_entries.json /tmp/x", "pending_entries.json"),
            ("python3 -m json.tool /tmp/in.json logs/guardian_state.json", "guardian_state.json"),
            ("jq --in-place . logs/guardian_state.json", "guardian_state.json"),
            ("python3 tool.py logs/pending_entries.json", "pending_entries.json"),
            ("echo logs/guardian_state.json | xargs rm", "guardian_state.json"),
            ("F=logs/guardian_state.json; echo x > $F", "guardian_state.json"),
            ("perl -pi -e 's/a/b/' logs/pending_entries.json", "pending_entries.json"),
            ("cp /tmp/x logs/{guardian_state,y}.json", "guardian_state.json"),
            ("python3 - <<'EOF'\nimport os\nos.symlink('/tmp/f', 'logs/guardian_state.json')\nEOF", "guardian_state.json"),
            ("bash <<'EOF'\necho x > logs/guardian_state.json\nEOF", "guardian_state.json"),
            ("python3 -V; bash <<'EOF'\necho x > logs/guardian_state.json\nEOF", "guardian_state.json"),
            ("python3 - <<'EOF'\n__import__('os').system('cp /tmp/f logs/guardian_state.json')\nEOF", "guardian_state.json"),
            ("git -C . checkout stash@{0} -- logs/pending_entries.json", "pending_entries.json"),
            ("git show HEAD:x > logs/guardian_state.json", "guardian_state.json"),
        ]
        for c, name in cases:
            self.assertGroundTruthDenied(self.agy(self.cmd(c)), name, c)
        self.assertAllGroundTruthDenied(["git clean -fdX", "git clean -xfd", "git stash --all", "git stash push -a",
                                         "git -C . clean -fdx", "git -c core.x=y stash -a",
                                         "find /tmp/f -type f -exec cp {} logs/ \\;"])
        self.assertNoneGroundTruth(["git clean -fd", "git stash", "git stash -u"])

    def test_allowlisted_reads_and_dev_workflow_keep_ask(self):
        for c in ("python3 -m json.tool logs/guardian_state.json", "python3 -m json.tool --indent 2 logs/guardian_state.json",
                  "python3 - <<'EOF'\nimport json\nprint(json.load(open('logs/guardian_state.json')))\nEOF",
                  "jq . < logs/guardian_state.json", "cat logs/guardian_state.json > /tmp/copy.json",
                  "ls -la logs/*.json", "md5sum logs/guardian_state.json", "stat logs/pending_entries.json",
                  "diff logs/guardian_state.json /tmp/x.json", "head -c 200 logs/pending_entries.json | wc -c",
                  "rg -n pending_entries.json scripts/", "grep -rn guardian_state.json scripts tests",
                  "git diff -- scripts/hooks/pre_trade_guard.py", "git log --oneline -- logs/guardian_state.json",
                  "git commit -m \"fix(guard): protect logs/guardian_state.json and logs/pending_entries.json\"",
                  "gh pr create --title x --body \"protects logs/pending_entries.json\"",
                  "echo x >> logs/guardian.log", "find logs -name guardian_state.json"):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)
        # Issue #100: a risk-reducing call carrying a redirect (a token with '>' / '&') is no longer auto-allowed
        # (normal permission policy), and never a ground-truth denial
        res = self.agy(self.cmd("python3 scripts/loops/position_guardian_loop.py --once >> logs/guardian.log 2>&1"))
        self.assertEqual(res.get("decision"), "ask")
        self.assertNotGroundTruth(res)
        self.assertEqual(self.agy(self.cmd("python3 scripts/execute_futures_trade.py --protect-pending --env prod "
                                           "2>&1 | tee -a logs/guardian.log")).get("decision"), "ask")

    def test_heredoc_message_with_write_marker_denied(self):
        msg = "git commit -m \"$(cat <<'EOF'\nfix: block json.dump(state, f) into logs/guardian_state.json\nEOF\n)\""
        self.assertGroundTruthDenied(self.agy(self.cmd(msg)), "guardian_state.json")
        self.assertEqual(self.agy(self.cmd(msg.replace("json.dump(state, f)", "forged writes"))).get("decision"), "ask")
        self.assertEqual(self.agy(self.cmd("git commit -F /tmp/msg.txt")).get("decision"), "ask")

    def test_windows_aliases_denied(self):
        cases = [("logs\\guardian_state.json.", "guardian_state.json"),
                 ("C:\\Users\\x\\repo\\logs\\guardian_state.json. ", "guardian_state.json"),
                 ("logs/guardian_state.json::$DATA", "guardian_state.json"),
                 ("C:\\Users\\x\\repo\\logs\\pending_entries.json:stream:$DATA", "pending_entries.json"),
                 ("C:\\Users\\x\\repo\\logs.\\pending_entries.json", "pending_entries.json"),
                 ("logs/session_state.json...", "session_state.json")]
        for target, name in cases:
            self.assertGroundTruthDenied(self.agy({"toolCall": {"name": "write_to_file", "args": {
                "TargetFile": target, "CodeContent": "{}"}}}), name, target)
            res = self.run_guard({"tool_name": "Write", "tool_input": {"file_path": target, "content": "{}"}})
            self.assertEqual(res.get("__exit_code__"), 2, target)
            self.assertIn(f"logs/{name} may only be written by", res["__stderr__"], target)

    # ---------------------------------------------------------------- review round 2 findings
    def test_allowlisted_programs_with_write_or_exec_options_denied(self):
        forged = '{"env":"prod","dry_run":false,"mode":"loop","interval_seconds":60,"last_cycle_ts":1}'
        cases = [
            (f"git log -1 --format='{forged}' --output=logs/guardian_state.json", "guardian_state.json"),
            ("git show HEAD:x --output logs/guardian_state.json", "guardian_state.json"),
            ("git diff --output=logs/pending_entries.json", "pending_entries.json"),
            ("rg --pre rm . logs/pending_entries.json", "pending_entries.json"),
            ("rg --pre=/tmp/x y logs/guardian_state.json", "guardian_state.json"),
            ("git -c core.fsmonitor='rm -f logs/pending_entries.json; false' status", "pending_entries.json"),
            ("git --config-env=core.pager=EVIL log -- logs/guardian_state.json", "guardian_state.json"),
            ("git --exec-path=/tmp/x status -- logs/guardian_state.json", "guardian_state.json"),
            ("git grep -O'rm -f' x -- logs/pending_entries.json", "pending_entries.json"),
            ("git grep --open-files-in-pager=rm x -- logs/pending_entries.json", "pending_entries.json"),
            ("git diff --ext-diff -- logs/guardian_state.json", "guardian_state.json"),
            ("git fetch --upload-pack='rm logs/pending_entries.json' /tmp/r", "pending_entries.json"),
            ("cat /tmp/forged.json | less -o logs/guardian_state.json", "guardian_state.json"),
            ("cat /tmp/forged.json | less -Ologs/guardian_state.json", "guardian_state.json"),
            ("less --log-file=logs/guardian_state.json /tmp/forged.json", "guardian_state.json"),
            ("less '+!rm logs/pending_entries.json' /tmp/x", "pending_entries.json"),
            ("LESSOPEN='|rm %s' less logs/pending_entries.json", "pending_entries.json"),
            ("env LESSOPEN='|rm %s' less logs/pending_entries.json", "pending_entries.json"),
            ("GIT_EXTERNAL_DIFF=/tmp/x git diff -- logs/guardian_state.json", "guardian_state.json"),
            ("RIPGREP_CONFIG_PATH=/tmp/rc rg x logs/pending_entries.json", "pending_entries.json"),
        ]
        for c, name in cases:
            self.assertGroundTruthDenied(self.agy(self.cmd(c)), name, c)
        # git -c values are shell commands: judged even when they only name the logs/ directory
        self.assertAllGroundTruthDenied(["git -c core.fsmonitor='rm -rf logs; false' status",
                                         "git -c alias.x='!rm -rf logs' x"])
        for c in ("git log --oneline -- logs/guardian_state.json", "git show HEAD -- logs/guardian_state.json",
                  "rg -n pending_entries.json scripts/", "less logs/guardian_state.json", "less -R -S logs/guardian_state.json",
                  "git grep -c guardian_state.json", "git -c core.pager=cat log -1", "git -C . diff --stat"):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)

    def test_git_abbreviated_and_clustered_exec_options_denied(self):
        # git parse-options accepts unique-prefix abbreviations and short clusters (-nOrm = -n -O rm)
        self.assertEqual(pre_trade_guard._git_long_options("--op=cp /tmp/f"), ["open-files-in-pager"])
        self.assertEqual(pre_trade_guard._git_long_options("--upl"), ["upload-pack"])
        self.assertEqual(pre_trade_guard._git_long_options("--rece=x"), ["receive-pack"])
        self.assertIn("exec", pre_trade_guard._git_long_options("--exe=x"))
        self.assertEqual(pre_trade_guard._git_long_options("--conf=x"), ["config-env"])
        for harmless in ("--oneline", "--only-matching", "--or", "--count", "--contains", "--con", "--exclude-standard",
                         "--exit-code", "--recurse-submodules=no", "--update-head-ok", "--no-index", "--", "-O"):
            self.assertEqual(pre_trade_guard._git_long_options(harmless), [], harmless)
        cases = [
            ("git grep --no-index --op='cp /tmp/f' -e . -- logs/guardian_state.json", "guardian_state.json"),
            ("git grep --no-index -nOrm -e . -- logs/guardian_state.json", "guardian_state.json"),
            ("git grep --open='cp /tmp/f' x -- logs/pending_entries.json", "pending_entries.json"),
            ("git log -1 --out=logs/guardian_state.json", "guardian_state.json"),
            ("git diff --ext -- logs/guardian_state.json", "guardian_state.json"),
            ("git show -o x -- logs/guardian_state.json", "guardian_state.json"),
            ("git fetch --upl='rm logs/pending_entries.json' /tmp/r", "pending_entries.json"),
        ]
        for c, name in cases:
            self.assertGroundTruthDenied(self.agy(self.cmd(c)), name, c)
        self.assertAllGroundTruthDenied([
            "git fetch --upl='rm -rf logs;:' .", "git push --rece='rm -rf logs;:' .", "git push --exe='rm -rf logs;:' .",
            "git fetch --upload-pack='rm -rf logs;:' .", "git fetch --upload-pack 'rm -rf logs;:' /tmp/r",
            "git push --receive-pack=/tmp/x .", "git ls-remote --upl /tmp/x .",
            "git grep --no-index -nOrm -e . -- logs", "git grep --no-index -Ocat foo",
        ])
        for c in ("git grep foo", "git log --oneline", "git fetch origin", "git push origin branch",
                  "git log --oneline -- logs/guardian_state.json", "git grep -c guardian_state.json",
                  "git grep -n -o foo -- logs/guardian_state.json", "git fetch --recurse-submodules=no origin",
                  "git diff -O/tmp/order -- scripts", "git grep -n foo -- scripts", "git log -p --stat ."):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)

    def test_command_running_option_values_and_operands_judged(self):
        # Values of command-running options are nested commands and their operands may not reach logs/,
        # whether or not a protected file is named
        self.assertAllGroundTruthDenied([
            "rg -uu --pre rm . logs", "rg --pre=rm foo logs", "rg --pre /tmp/x foo", "rg --pre rm -e foo -- .",
            "rg --pre=\"bash -c 'rm -rf logs'\" foo scripts/", "rg --hostname-bin='rm -rf logs' foo scripts/",
            "less '+!rm -rf logs' /tmp/x", "git grep -O'rm -rf logs' foo -- scripts",
            "git -C . grep --op=/tmp/x foo -- logs",
        ])
        for c in ("rg foo logs/", "rg -n foo scripts/", "rg foo", "rg --pre-glob '*.gz' foo logs/",
                  "rg --pre /tmp/x foo scripts/ tests/", "less +G logs/guardian.log", "less '+/pattern' /tmp/x",
                  "git fetch --upload-pack=/tmp/x /tmp/r"):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)

    def test_nested_shells_inline_code_and_find_exec_on_logs_dir_denied(self):
        self.assertAllGroundTruthDenied([
            "bash -c 'rm -rf logs'", "sh -c 'rm -f logs/*.json'", "eval 'rm -rf logs'", "eval rm -rf logs",
            "cmd //c rd /s /q logs", "python3 -c \"import shutil; shutil.rmtree('logs')\"",
            "node -e \"require('fs').rmSync('logs',{recursive:true})\"",
            "find . -maxdepth 0 -name . -exec rm -rf logs \\;",
            "bash -lc 'mv logs /tmp/x'", "sudo sh -c \"ln -s logs st\"", "bash -c \"bash -c 'rm -rf logs'\"",
            "cmd.exe /c del /s /q logs", "cmd //c move logs C:\\tmp", "powershell -Command \"Remove-Item -Recurse -Force logs\"",
            "pwsh -c 'Move-Item -Path logs -Destination /tmp/x'",
            "find . -type d -name 'lo*' -exec rm -rf {} +", "find . -maxdepth 1 -name logs -exec mv {} /tmp/x \\;",
            "find /tmp -name x -exec sh -c 'rm -rf logs' \\;", f"find {self.root} -maxdepth 0 -exec rm -rf {{}} \\;",
            "python3 -c \"import shutil; shutil.rmtree('.')\"",
            "python3 -c \"import glob,os; [os.remove(p) for p in glob.glob('logs/*')]\"",
            "python3 -c \"import os; os.rename('logs', '/tmp/x')\"", "node -e \"require('fs').renameSync('logs','/tmp/x')\"",
            "python3 -c \"import os; os.symlink('logs', 'st')\"",
            "python3 - <<'EOF'\nimport shutil\nshutil.rmtree('logs')\nEOF",
        ])
        self.assertNoneGroundTruth([
            "bash -c 'echo hi'", "bash scripts/dev/x.sh logs", "sh -c 'rm -rf build'", "eval \"$(ssh-agent -s)\"",
            "find . -name '*.pyc' -exec rm -f {} +", "find . -name __pycache__ -type d -exec rm -rf {} +",
            "find build -type d -exec rm -rf {} +", "cmd //c rd /s /q build", "cmd //c dir logs",
            "python3 -c \"print('a.b'.replace('.', '_'))\"", "python3 -c \"import os; print(os.listdir('logs'))\"",
            "python3 -c \"import shutil; shutil.rmtree('build')\"", "bash -c 'cat logs/guardian.log'",
        ])

    def test_inline_write_markers_and_heredoc_program(self):
        cases = [
            ("node -e \"require('fs').rm('logs/pending_entries.json',()=>{})\"", "pending_entries.json"),
            ("node -e \"const fs=require('fs');fs.writeSync(fs.openSync('logs/guardian_state.json','w'),'{}')\"",
             "guardian_state.json"),
            ("node -e \"const {rm}=require('node:fs/promises'); rm('logs/pending_entries.json')\"", "pending_entries.json"),
            ("python3 -c \"import os; os.execvp('rm',['rm','logs/pending_entries.json'])\"", "pending_entries.json"),
            ("python3 -c \"import os; os.spawnlp(os.P_WAIT,'rm','rm','logs/pending_entries.json')\"", "pending_entries.json"),
            ("python3 -c \"import os; os.posix_spawnp('rm',['rm','logs/pending_entries.json'],{})\"", "pending_entries.json"),
            ("python3 -c \"import pty; pty.spawn(['rm','logs/pending_entries.json'])\"", "pending_entries.json"),
            ("python3 -c \"f=open('logs/guardian_state.json','bw')\"", "guardian_state.json"),
            ("python3 -c \"f=open('logs/guardian_state.json', mode='ab')\"", "guardian_state.json"),
            ("python3 -c \"f=open('logs/guardian_state.json','b+r')\"", "guardian_state.json"),
            ("perl <<'EOF' # python\nunlink \"logs/pending_entries.json\";\nEOF", "pending_entries.json"),
            ("ruby <<'EOF' # node\nFile.delete('logs/pending_entries.json')\nEOF", "pending_entries.json"),
        ]
        for c, name in cases:
            self.assertGroundTruthDenied(self.agy(self.cmd(c)), name, c)
        for c in ("python3 -c \"print(open('logs/guardian_state.json','rb').read())\"",
                  "python3 -c \"print(open('logs/guardian_state.json', 'r').read())\"",
                  "node -e \"console.log(require('fs').readFileSync('logs/guardian_state.json','utf8'))\"",
                  "cat <<'EOF' | python3 -\nimport json\nprint(json.load(open('logs/guardian_state.json')))\nEOF"):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)
        # Issue #98: _strip_interpreter_heredocs was replaced by the lexical pre-pass (_scan_shell); the ground-truth
        # sub-commands keep a perl body (the comment does not make it python) and drop a python body
        subs = pre_trade_guard._ground_truth_subcommands
        self.assertIn(["unlink", "x"], subs("perl <<'EOF' # python\nunlink x;\nEOF"))
        self.assertEqual(subs("python3 - <<'EOF'\nx = 1\nEOF"), [["python3", "-", "<<", "EOF"]])

    def test_unquoted_command_substitution_paths_denied(self):
        self.assertEqual(pre_trade_guard._lift_path_substitutions("rm -rf $(pwd)/logs"), "rm -rf ./logs")
        self.assertEqual(pre_trade_guard._lift_path_substitutions("ln -s $(dirname x)/logs st"),
                         "ln -s ./logs st\ndirname x")
        self.assertAllGroundTruthDenied([
            "rm -rf $(pwd)/logs", "ln -s $(pwd)/logs st", "rm -rf `pwd`/logs", "rm -rf $(pwd -P)/logs",
            "rm -rf $(pwd)", "rm -rf \"$PWD\"", "rm -rf ${PWD}/logs", "rm -rf $(git rev-parse --show-toplevel)",
            "rm -rf $(git rev-parse --show-toplevel)/logs", "rm -rf $(dirname /x/y)/logs", "mv $(pwd)/logs /tmp/x",
            "echo \"$(rm -rf logs)/x\"", "rm -rf $HOME",
        ])
        self.assertNoneGroundTruth(["ls $(pwd)/logs", "echo $(pwd)", "cd $(git rev-parse --show-toplevel)",
                                    "rm -rf $(pwd)/build", "cat $(pwd)/logs/guardian_state.json"])

    # ---------------------------------------------------------------- review round 5 findings (issue #53)
    def test_option_values_not_counted_as_search_paths(self):
        # Vector A: values of value-taking rg / git grep options must not be mistaken for operands, so the
        # default-'.' rule still fires and the command runs on logs/.
        self.assertAllGroundTruthDenied([
            "rg -uu --pre rm -m 1 foo", "rg --pre rm -g '*.json' foo", "rg --pre rm -t json foo",
            "rg --pre rm -A 2 foo", "rg --pre rm --max-depth 2 foo", "rg --pre rm --threads 2 foo",
            "rg --pre rm -m1 foo", "rg --pre rm --max-count=1 foo", "rg --pre rm -tjson foo",
            "git grep --no-index -Orm -m 1 '{'", "git grep --no-index -Orm --max-count 1 '{'",
        ])
        # Vector G over-denial fix: -eOrder = -e Order (a pattern), not -O.
        for c in ("git grep -eOrder -- scripts", "git grep -e Order -- scripts", "rg -A 2 foo scripts/",
                  "rg -m 1 foo scripts/", "git grep -f patterns.txt -- scripts"):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)

    def test_short_aliases_of_command_running_git_options_denied(self):
        self.assertAllGroundTruthDenied([
            "git clone -u 'rm -rf logs;:' file://. /tmp/y", "git ls-remote -u 'rm -rf logs;:' .",
            "git fetch -u 'rm -rf logs;:' .", "git pull -u 'rm -rf logs;:' .",
            "git rebase -x 'rm -rf logs' HEAD", "git difftool --extcmd 'rm -rf logs'",
            "git difftool -x 'rm -rf logs'",
        ])
        for c in ("git clone file://. /tmp/y", "git fetch origin", "git rebase main", "git difftool HEAD~1"):
            self.assertNotGroundTruth(self.agy(self.cmd(c)), c)

    def test_env_var_command_channels_denied(self):
        self.assertAllGroundTruthDenied([
            "GIT_PAGER='rm -rf logs' git grep -O x", "PAGER='rm -rf logs' git log",
            "GIT_EXTERNAL_DIFF='rm -rf logs' git diff", "GIT_SSH_COMMAND='rm -rf logs' git fetch origin",
            "LESSOPEN='|rm -rf logs' less /tmp/x", "env GIT_SEQUENCE_EDITOR='rm -rf logs' git rebase -i HEAD",
            # config / startup-file / library injection: denied outright on any command
            "RIPGREP_CONFIG_PATH=/tmp/rc rg x scripts/", "LD_PRELOAD=/tmp/x.so ls", "BASH_ENV=/tmp/x bash -lc id",
            "GIT_CONFIG_GLOBAL=/tmp/cfg git status", "GIT_CONFIG_COUNT=1 git status",
            "export GIT_CONFIG_KEY_0=core.pager", "GIT_EXEC_PATH=/tmp/x git status",
        ])
        for c in ("PAGER=less git log", "GIT_PAGER=cat git diff", "EDITOR=true git commit --amend",
                  "LANG=C git status", "TZ=UTC date"):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)

    def test_persistent_git_config_of_command_valued_key_denied(self):
        self.assertAllGroundTruthDenied([
            "git config core.pager 'rm -rf logs'", "git config --global alias.x '!rm -rf logs'",
            "git config --add core.fsmonitor /tmp/x", "git config --replace-all core.sshCommand /tmp/x",
            "git config diff.external /tmp/x", "git config filter.lfs.clean /tmp/x",
            "git config remote.origin.uploadpack /tmp/x", "git config credential.helper /tmp/x",
            "git config include.path /tmp/x", "git --config-env=core.pager=EVIL log",
        ])
        for c in ("git config --get core.pager", "git config -l", "git config user.name me",
                  "git config --list", "git -c core.pager=cat log -1"):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)

    def test_other_command_running_git_subcommands_denied(self):
        self.assertAllGroundTruthDenied([
            "git bisect run rm -rf logs", "git submodule foreach 'rm -rf logs'",
            "git submodule foreach --recursive rm -rf logs",
            "git -C logs grep --no-index -Orm x -- '*.json'",
        ])
        for c in ("git bisect run make test", "git submodule foreach 'git status'", "git -C . diff --stat",
                  "git -C subdir log --oneline"):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)

    def test_perl_ruby_inline_markers_and_empty_literal_gap_denied(self):
        cases = [
            ("perl -e 'rename \"logs/pending_entries.json\", \"x\"'", "pending_entries.json"),
            ("perl -e 'remove_tree(\"logs\")'", "guardian_state.json"),
            ("perl -e 'unlink \"logs/guardian_state.json\"'", "guardian_state.json"),
            ("ruby -e 'FileUtils.rm_rf(\"logs\")'", "guardian_state.json"),
            ("ruby -e 'FileUtils.mv(\"logs/pending_entries.json\", \"/tmp/x\")'", "pending_entries.json"),
            ("ruby -e 'File.delete(\"logs/guardian_state.json\")'", "guardian_state.json"),
            ("ruby -e 'File.rename(\"logs/pending_entries.json\", \"x\")'", "pending_entries.json"),
            ("ruby -e 'Dir.rmdir(\"logs\")'", "guardian_state.json"),
            # empty-literal scanner gap: print('') / print("") no longer swallows the following 'logs' literal
            ("python3 -c \"print('');shutil.rmtree('logs')\"", "guardian_state.json"),
            ("python3 -c 'print(\"\");shutil.rmtree(\"logs\")'", "guardian_state.json"),
        ]
        for c, name in cases:
            self.assertGroundTruthDenied(self.agy(self.cmd(c)), name, c)
        for c in ("perl -e 'print \"hello\"'", "ruby -e 'puts 1+1'",
                  "python3 -c \"print('');print('ok')\""):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "ask", c)
            self.assertNotGroundTruth(res, c)

    def test_cd_and_variable_tracking_denied(self):
        self.assertAllGroundTruthDenied([
            "cd logs && rm *.json", "cd logs; mv x.json y.json", "cd ./logs/ && rm -f guardian_state.json",
            "cd \"$X\"; rm x.json", "cd -; rm something.json",
            "D=logs; rm -rf $D", "export D=logs; rm -rf ${D}", "D=logs && rm -rf \"$D\"",
            "rm -rf $UNKNOWN", "rm -rf $TMPDIR/x",
        ])
        for c in ("cd build && rm *.o", "cd logs && cat guardian_state.json", "cd scripts && ls",
                  "D=build; rm -rf $D", "cd /tmp && rm x.json"):
            res = self.agy(self.cmd(c))
            self.assertNotGroundTruth(res, c)
            self.assertNotEqual(res.get("decision"), "deny", c)

    def test_xargs_archive_recursive_copy_and_powershell_encoded_denied(self):
        self.assertAllGroundTruthDenied([
            "find logs -name '*.json' | xargs rm", "ls | xargs rm -f", "cat list | xargs rm",
            "find . -type f | xargs rm",
            "tar -xf a.tar -C logs", "tar xf a.tar -C .", "unzip -d . a.zip", "unzip a.zip",
            "7z x -o./logs a.7z", "cp -a /tmp/x/* .", "rsync -a /tmp/x/ ./", "rsync --files-from=list / .",
            "robocopy /tmp/src logs /E", "powershell -EncodedCommand cm0gLXJmIGxvZ3M=",
            "powershell -enc bm90YmFzZTY0!!!",
        ])
        for c in ("find /tmp/x -type f | xargs rm", "tar -xzf deps.tar.gz -C /tmp/out",
                  "unzip -l a.zip", "cp report.txt logs/", "rsync -a /tmp/x/ /tmp/y/",
                  "find /tmp/build -type f | xargs grep foo"):
            res = self.agy(self.cmd(c))
            self.assertNotGroundTruth(res, c)
            self.assertNotEqual(res.get("decision"), "deny", c)

    def test_any_directory_named_logs_denied(self):
        # Issue #53 round 7 (back to main's rule): any path whose last component is logs counts as logs/, since a
        # symlink (/tmp/r -> repo) or /proc/self/cwd can alias the workspace in ways the hook cannot resolve.
        self.assertAllGroundTruthDenied([
            "rm -rf /tmp/other/logs", "rm -rf build/logs", "rm -rf ../sibling/logs",
            "mv /tmp/a/logs /tmp/b/logs", "rm -rf node_modules/pkg/logs", "rm -rf /tmp/r/logs",
            "rm -rf ./x/../logs", "rm -rf LOGS/", "rm -rf /tmp/r/logs/.",
        ])

    def test_proc_cwd_and_symlink_aliases_of_logs_denied(self):
        # Issue #53 round 7: every row of the reviewer's regression table (main: deny, branch: ask) denies again
        self.assertAllGroundTruthDenied([
            "rm -rf /proc/self/cwd/logs", "mv /proc/self/cwd/logs /tmp/x",
            "ln -s \"$PWD\" /tmp/r && rm -rf /tmp/r/logs", "rm -rf /tmp/r/logs",
            "rm -rf /tmp/other/logs", "rm -rf build/logs",
            "cd /proc/self/cwd/logs && rm -f *.json",
            "find /proc/self/cwd/logs -name '*.json' | xargs rm",
            # /proc/<pid>/{cwd,root,fd} and /dev/fd resolve in the command's process: unknown cwd / ancestor
            "rm -rf /proc/self/cwd", "rm -rf /proc/123/root/x", "rm -rf //proc/self/./cwd",
            "rm -rf /proc/self/task/1/cwd", "rm -rf /dev/fd/3", "mv /proc/self/cwd/x /tmp/y",
            "rm -f /proc/self/cwd/a.json", "cd /proc/self/cwd && rm -f logs/*.json",
            "cd /proc/self/cwd && rm -f x.json", "pushd /proc/1/cwd; rm -f x.json",
            "env -C /proc/self/cwd rm -f x.json", "cd /tmp/r/logs && rm -f *.json",
            "cd /tmp/r/logs && cd sub && rm -f ../*.json", "env -C build/logs rm -f *.json",
            "find /proc/self/cwd -type f -delete", "tar -xf a.tar -C /proc/self/cwd",
            "cp -r /tmp/x/. /proc/self/cwd",
        ])
        for c in ("ls /proc/self/cwd/logs", "cat /proc/self/cwd/logs/a.txt", "cd /proc/self/cwd && ls",
                  "find /proc/self/cwd/logs -name '*.json'", "cd /tmp/r && rm x.json"):
            res = self.agy(self.cmd(c))
            self.assertNotGroundTruth(res, c)
            self.assertNotEqual(res.get("decision"), "deny", c)

    def test_symlink_alias_of_workspace_logs_denied(self):
        # Issue #53 round 7: a real symlink to the workspace root; the hook judges the literal path, never resolves it
        link_dir = tempfile.mkdtemp()
        link = os.path.join(link_dir, "r")
        try:
            os.symlink(self.root, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            shutil.rmtree(link_dir, ignore_errors=True)
            self.skipTest("symlinks not available")
        try:
            posix_link = link.replace("\\", "/")
            self.assertAllGroundTruthDenied([f"rm -rf {posix_link}/logs", f"mv {posix_link}/logs /tmp/x",
                                             f"cd {posix_link}/logs && rm -f *.json"])
        finally:
            if os.path.islink(link):
                os.unlink(link)
            shutil.rmtree(link_dir, ignore_errors=True)

    def test_shell_script_file_content_is_judged(self):
        script = os.path.join(self.root, "evil.sh")
        with open(script, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nrm -rf logs\n")
        safe = os.path.join(self.root, "ok.sh")
        with open(safe, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nls -la\n")
        for c in (f"bash {script}", f"sh {script}", f". {script}", f"source {script}", f"bash < {script}"):
            self.assertDenied(self.agy(self.cmd(c)), "Ground Truth Protection")
        for c in (f"bash {safe}", f"sh {safe}"):
            self.assertNotGroundTruth(self.agy(self.cmd(c)), c)

    # ---------------------------------------------------------------- review round 6 findings (issue #53 round 2)
    def write_fixture_scripts(self):
        """Script files in the workspace root (relative operands resolve there) and a script under scripts/."""
        files = {
            "evil.sh": "#!/bin/sh\nrm -rf logs\n",
            "evil": "rm -rf logs\n",                                  # no shebang: the shell runs it
            "evil_env": "#!/usr/bin/env -S bash -e\nrm -rf logs\n",
            "pyscript": "#!/usr/bin/env python3\nimport shutil\nshutil.rmtree('logs')\n",
            "ok.sh": "#!/bin/sh\n# don't panic: a quote in a comment\nls -la\n",
            "cdlogs.sh": "cd logs\nrm -f *.json\n",
            "self.sh": "source self.sh\n",
            "scripts/desk_bad.sh": "#!/bin/bash\nrm -rf logs\n",
            "tools/evil": "#!/bin/dash\nrm -rf logs\n",
            "a.txt": "x",
        }
        os.makedirs(os.path.join(self.root, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "tools"), exist_ok=True)
        for name, content in files.items():
            with open(os.path.join(self.root, *name.split("/")), "w", encoding="utf-8") as f:
                f.write(content)
        with open(os.path.join(self.root, "big.sh"), "w", encoding="utf-8") as f:
            f.write("#" * (pre_trade_guard.SCRIPT_READ_LIMIT + 1))
        with open(os.path.join(self.root, "latin1.sh"), "wb") as f:
            f.write(b"echo caf\xe9\n")

    def ps(self, command):
        return self.run_guard({"tool_name": "PowerShell", "tool_input": {"command": command}})

    def test_windows_and_drive_mount_logs_paths_denied_whatever_the_root(self):
        # Item 0: a Windows / UNC / WSL-drive path whose last component is logs stays denied (the hook may not be
        # able to map it onto the workspace root); since round 7 so does any other path whose last component is logs.
        self.assertAllGroundTruthDenied([
            "rm -rf /mnt/c/Users/x/repo/logs", "rm -rf C:/Users/x/repo/logs", "rm -rf //server/share/repo/logs",
            "cmd //c rd /s /q C:/Users/x/repo/logs", "rm -rf 'C:\\Users\\x\\repo\\logs'",
            "cd C:/Users/x/repo/logs && rm *.json",
        ])
        for command in ("Remove-Item C:\\Users\\x\\repo\\logs -Recurse", "Remove-Item \\\\server\\share\\logs -Recurse",
                        "Remove-Item /mnt/c/Users/x/repo/logs -Recurse"):
            res = self.ps(command)
            self.assertEqual(res["__exit_code__"], 2, command)
            self.assertIn("may only be written by", res["__stderr__"], command)
        self.assertNoneGroundTruth(["rm -rf C:/tmp/build/*"])
        self.assertAllGroundTruthDenied(["rm -rf /tmp/other/logs", "rm -rf build/logs"])  # round 7: main's rule

    def test_cd_tracking_only_adds_denials(self):
        # Item 1 (blocker): a cd may fail, run in a subshell or be skipped; every possible cwd is judged
        self.assertAllGroundTruthDenied([
            "cd /nonexistent; rm -rf logs", "(cd /tmp); rm -rf logs", "false && cd /tmp; rm -rf logs",
            "cd ~/x/logs && rm -f *.json", "env -C logs rm -f *.json", "env --chdir=logs rm -f *.json",
            "sudo -D logs rm -f *.json", "sudo --chdir=logs rm -f *.json", "cd; rm -rf x/logs.json",
            "pushd /tmp; popd; rm x.json", "cd logs; echo x > guardian_state.tmp", "cd /tmp && rm -rf logs",
        ])
        self.assertNoneGroundTruth(["cd /tmp && rm x.json", "cd build && rm *.o", "cd logs && cat guardian_state.json",
                                    "(cd build && make)", "cd scripts && ls"])

    def test_script_written_on_the_same_line_denied(self):
        # Item 2: the content the hook would read is not what runs
        self.write_fixture_scripts()
        self.assertAllGroundTruthDenied([
            "echo ls > /tmp/a.sh && bash /tmp/a.sh", "printf ls | tee /tmp/a.sh; sh /tmp/a.sh",
            "cat > /tmp/a.sh <<'EOF'\nls\nEOF\nbash /tmp/a.sh", "cp /tmp/x /tmp/a.sh; sh /tmp/a.sh",
            "python3 -c \"open('/tmp/b.sh','w').write('rm -rf logs')\" && bash /tmp/b.sh",
            "bash -c 'echo ls > /tmp/c.sh'; bash /tmp/c.sh", "tar -xf /tmp/a.tar -C /tmp/x && bash ok.sh",
            "echo ls > ok.sh; ./ok.sh",
        ])
        self.assertNoneGroundTruth(["chmod +x ok.sh && ./ok.sh", "bash ok.sh; bash ok.sh", "cat ok.sh && bash ok.sh"])

    def test_unreadable_large_or_undecodable_scripts_denied(self):
        self.write_fixture_scripts()
        self.assertAllGroundTruthDenied(["bash big.sh", "bash latin1.sh", "./big.sh"])
        with patch("pre_trade_guard.os.path.getsize", side_effect=OSError("denied")):
            self.assertAllGroundTruthDenied(["bash ok.sh"])
        self.assertNoneGroundTruth(["bash ok.sh", "bash /tmp/does-not-exist.sh"])

    def test_shell_options_before_the_script_operand_are_parsed(self):
        self.write_fixture_scripts()
        self.assertAllGroundTruthDenied([
            "bash -o errexit evil.sh", "bash -e -x evil.sh", "bash --norc evil.sh", "bash -O extglob evil.sh",
            "bash -eo pipefail evil.sh", "sh +o posix evil.sh", "bash --noprofile --norc -- evil.sh",
            "bash --rcfile evil.sh -i -c ls", "nohup bash -x evil.sh", "timeout 5 sh evil.sh",
        ])
        self.assertNoneGroundTruth(["bash -o errexit ok.sh", "bash -e -x ok.sh", "bash --version"])

    def test_programs_invoked_by_path_are_judged_as_shell_scripts(self):
        self.write_fixture_scripts()
        self.assertAllGroundTruthDenied(["./evil", f"{self.root}/evil", "./evil_env", "./evil.sh", "evil.sh",
                                         "./cdlogs.sh", "sudo ./evil", "tools/evil", "./scripts/desk_bad.sh"])
        self.assertNoneGroundTruth(["./ok.sh", "./pyscript", "/bin/ls -la", "./does-not-exist"])

    def test_shells_reading_stdin_denied_unless_their_input_is_visible(self):
        self.write_fixture_scripts()
        self.assertAllGroundTruthDenied([
            "cat evil.sh | bash", "cat < evil.sh | sh", "curl -s http://127.0.0.1:9/x | bash", "echo ls | sh",
            "bash", "bash -s", "bash <(curl -s http://127.0.0.1:9/x)", "source /dev/stdin", "bash < evil.sh",
            "sh < evil.sh", "sh -s < evil.sh", ". /dev/stdin", "echo ls | source /dev/fd/0", "bash <<< 'rm -rf logs'", "cat ok.sh | grep ls | bash", "cat -v ok.sh | bash",
        ])
        self.assertNoneGroundTruth(["cat ok.sh | bash", "bash -s < ok.sh", "bash <<< 'ls'", "bash --version",
                                    "bash <<'EOF'\nls\nEOF", "sh < /tmp/does-not-exist"])

    def test_nested_strings_and_scripts_get_the_full_line_analysis(self):
        # Item 3: cd / variable / xargs tracking inside bash -c, eval, find -exec sh -c, decoded PowerShell and
        # script files; the nesting depth is bounded (deeper is denied)
        self.write_fixture_scripts()
        encoded = base64.b64encode("cd logs; rm x.json".encode("utf-16-le")).decode()
        self.assertAllGroundTruthDenied([
            "bash -c 'cd logs && rm -f *.json'", "eval 'ls logs | xargs rm'", "sh -c 'D=logs; rm -rf $D'",
            "bash -c \"bash -c 'cd logs; rm x'\"", "find /tmp -name x -exec sh -c 'cd logs && rm -f *.json' \\;",
            f"powershell -EncodedCommand {encoded}", "bash cdlogs.sh", "bash self.sh",
            "eval " * 9 + "ls",
        ])
        self.assertNoneGroundTruth(["bash -c 'cd build && rm -f *.o'", "eval 'ls build | wc -l'", "eval " * 3 + "ls"])

    def test_xargs_requires_a_confined_producer(self):
        # Item 4
        self.assertAllGroundTruthDenied([
            "xargs rm < /tmp/list", "xargs -a /tmp/list rm", "xargs --arg-file=/tmp/list rm",
            "find . -name '*.json' | xargs -I {} rm {}", "find /tmp/x -type f | xargs -I {} rm {}",
            "find /tmp/x | xargs sh -c 'rm \"$@\"' _", "find /tmp/x | xargs perl -e 'unlink @ARGV'",
            "ls | nice xargs rm", "ls | timeout 5 xargs rm", "ls | command xargs rm", "ls | exec xargs rm",
            "ls | stdbuf -oL xargs rm", "ls | env FOO=1 xargs rm", "find /tmp/x -type f | xargs nice rm",
            "find /tmp/x -type f | xargs env rm", "find /tmp/x -type f | xargs -d '\\n' rm",
            "find /tmp/x -type f | xargs -i rm {}", "find /tmp/x -type f | xargs --replace=X rm X",
            "find -L /tmp/x -type f | xargs rm", "find /tmp/x -printf '%f\\n' | xargs rm",
            "find /tmp/x | grep foo | xargs rm", "find /tmp/x -type f | xargs cp /tmp/y",
            "find /tmp/x -type f | xargs truncate -s0", "find /tmp -type f | xargs rm", "ls /tmp/x | xargs rm -rf",
            "cd \"$X\"; find build -type f | xargs rm", "find \"$D\" -type f | xargs rm",
        ])
        self.assertNoneGroundTruth([
            "find /tmp/x -type f | xargs rm", "find /tmp/x -type f -print0 | xargs -0 rm -f",
            "find /tmp/x -type f | xargs -n 1 -P 4 rm", "find /tmp/x -type f | xargs -L 1 rm",
            "find /tmp/x -type f | xargs -s 100 rm", "find /tmp/x -type f | xargs -E END rm",
            "find /tmp/x -type f | xargs --max-args=1 rm", "find /tmp/x -type f | timeout 5 xargs rm",
            "find /tmp/x -type f | nice xargs rm", "find /tmp/x -type f | xargs cp -t /tmp/y",
            "find . -type f | xargs wc -l", "find /tmp/x | xargs -I {} echo {}", "find build -type f | xargs rm",
            "xargs",
        ])

    def test_env_and_config_command_channels_use_allowlists(self):
        # Item 5: pager / editor values are allowlisted, every other channel is denied whatever its value
        self.assertAllGroundTruthDenied([
            "GIT_EXTERNAL_DIFF=rm git diff --no-index logs /tmp/e", "git -c diff.external=rm diff --no-index logs /tmp/e",
            "PAGER='less +!rm' git log -1", "PAGER='less -ologs/x' git log -1", "PAGER=most git log -1",
            "PAGER=$X git log -1", "PAGER=$(echo cat) git log -1", "PAGER=`echo cat` git log -1",
            "EDITOR=vim git commit", "export PAGER='rm -rf logs'; git log", "PAGER+=x git log",
            "nice env GIT_EXTERNAL_DIFF=rm git diff", "nice env LD_PRELOAD=/tmp/x.so ls",
            "time GIT_EXTERNAL_DIFF='rm -rf logs;:' git diff", "GIT_EXTERNAL_DIFF+='rm -rf logs;:' git diff",
            "command env GIT_SSH_COMMAND=x git fetch", "exec env PAGER=most git log", "sudo GIT_PAGER=most git log",
            "env -S 'GIT_EXTERNAL_DIFF=rm git diff'", "env -S 'rm -rf logs'", "env --split-string='rm -rf logs'",
            "git --config-env=core.fsmonitor=X status", "git --config-env core.pager=X log",
            "git -c core.editor=vim commit", "git -c alias.st=status st", "git -c core.pager='less +!rm' log",
            "ENV=/tmp/x sh -c ls", "BASH_ENV=/tmp/x bash -c ls", "ENV=/tmp/x env bash", "export BASH_ENV=/tmp/x",
            "BASH_ENV=/tmp/x ./run.sh", "read PAGER < /tmp/x; git log", "printf -v PAGER rm; git log",
            "for PAGER in rm; do git log; done", "declare -n ref=PAGER; ref=rm; git log", "LESS='+!rm' git log",
            "LESS='-ologs/x' git log", "GIT_DIR=/tmp/r/.git git status", "GIT_WORK_TREE=logs git checkout .",
            "GIT_INDEX_FILE=/tmp/i git add .", "MANPAGER='sh -c x' man ls", "VISUAL=nano crontab -e",
        ])
        self.assertNoneGroundTruth([
            "git -c core.pager=cat log -1", "PAGER=less git log -1", "PAGER='less -R' git log -1",
            "GIT_PAGER= git log -1", "EDITOR=true git commit --amend", "GIT_EDITOR=: git rebase -i HEAD~2",
            "export PAGER=less; git log", "ENV=test python3 -m pytest", "LESS=FRX git log", "LESS=-R git log",
            "git -c pager.log=false log", "env -u PAGER git log", "git --config-env=user.name=X status",
            "GIT_PAGER=/usr/bin/less git log", "unset BASH_ENV; ls",
        ])

    def test_git_config_options_and_read_actions_parsed(self):
        # Item 6: a git config call is a read only with an explicit read action; keys are case-insensitive
        self.assertAllGroundTruthDenied([
            "git config -f .git/config core.hooksPath /tmp/h", "git config --file=.git/config core.hooksPath /tmp/h",
            "git config --type path core.hooksPath /tmp/h", "git config -t path core.hooksPath /tmp/h",
            "git config -z core.pager x", "git config --name-only core.pager x", "git config --show-origin alias.x y",
            "git config --default x core.sshCommand /tmp/x", "git config --comment hi core.fsmonitor /tmp/x",
            "git config set core.pager rm", "git config set --comment=x alias.x '!rm'",
            "git config --unset core.hooksPath", "git config unset alias.x", "git config core.pager",
            "git config --rename-section foo alias", "git config rename-section foo alias", "git config --edit",
            "git config -e", "git config edit", "git config CORE.HOOKSPATH /tmp/h", "git config Alias.X '!rm'",
            "git config --rep core.sshCommand x", "git config --global credential.https://x.helper /tmp/h",
            "git config core.worktree logs", "git config submodule.x.update '!rm -rf logs'",
            "git clone -c core.fsmonitor=x file:///tmp/r /tmp/y", "git clone --config core.hooksPath=/tmp/h /tmp/r y",
            "git clone --template=/tmp/t /tmp/r /tmp/y", "git init --template /tmp/t",
            "git -C logs checkout -- .", "git --work-tree=logs checkout HEAD -- .", "git -C \"$D\" clean -fdx",
        ])
        self.assertNoneGroundTruth([
            "git config core.pager less", "git config core.pager 'less -R'", "git config get core.pager",
            "git config list", "git config --get core.pager", "git config --get-regexp alias", "git config -l",
            "git config --list --show-origin", "git config -f .git/config --get core.hooksPath",
            "git config user.name me", "git config --global user.email a@b", "git config pager.log false",
            "git config --add pager.log false", "git -C \"$D\" status", "git -C logs status",
        ])

    def test_option_value_tables_follow_the_reference_help(self):
        # Item 7: rg (15.1) value-taking options and git unique-prefix long options
        self.assertEqual(pre_trade_guard._resolve_long("thr", pre_trade_guard.GIT_GREP_LONG_OPTIONS), "threads")
        self.assertEqual(pre_trade_guard._resolve_long("max-d", pre_trade_guard.GIT_GREP_LONG_OPTIONS), "max-depth")
        self.assertIsNone(pre_trade_guard._resolve_long("max", pre_trade_guard.GIT_GREP_LONG_OPTIONS))
        self.assertAllGroundTruthDenied([
            "git grep --no-index -Orm --thr 2 x", "git grep --no-index -Orm --max-d 2 x",
            "git grep --no-index -Orm --max-c 1 x", "git grep --no-index --open-f=rm x",
            "rg -uu --pre rm -e a -e b", "rg -uu --pre rm -d 9 foo", "rg -uu --pre rm --max-filesize 1M foo",
            "rg -uu --pre rm --hyperlink-format x foo", "rg -uu --pre rm --pre-glob '*.gz' foo",
            "rg -uu --pre rm -E utf-8 foo", "rg -uu --pre rm --engine auto foo", "rg -uu --pre rm -r x foo",
        ])
        self.assertNoneGroundTruth(["git grep -n --thr 2 foo -- scripts", "rg -d 9 foo scripts/",
                                    "rg --max-filesize 1M foo scripts/", "git grep --max-d 1 -e x -- scripts"])

    def test_powershell_encoded_command_in_any_prefix_spelling(self):
        # Item 8: -e / -ec / -en ... -encodedcommand, with -, -- or / (and -enc:payload); undecodable -> deny
        evil = base64.b64encode("Remove-Item -Recurse logs".encode("utf-16-le")).decode()
        benign = base64.b64encode("Get-ChildItem".encode("utf-16-le")).decode()
        self.assertAllGroundTruthDenied([
            f"powershell -enco {evil}", f"powershell -encodedc {evil}", f"powershell /enc {evil}",
            f"powershell --enc {evil}", f"pwsh -e {evil}", f"pwsh -ec {evil}", f"powershell -enc:{evil}",
            f"powershell.exe -NoProfile -EncodedCommand {evil}", "powershell -enco notbase64!!!", "pwsh -enc",
            "powershell -comm \"Remove-Item -Recurse logs\"",
        ])
        self.assertNoneGroundTruth([f"powershell -enco {benign}", "powershell -EncodedArguments x -Command Get-Date",
                                    "pwsh -ExecutionPolicy Bypass -File C:/tmp/build.ps1"])

    def test_run_time_values_in_write_positions_denied(self):
        # Item 9: unresolved $VAR / ${VAR} / $(...) / `...` anywhere in a write operand, redirect, output option,
        # destructive find root or archive destination
        self.assertAllGroundTruthDenied([
            "D=$(printf logs); rm -rf ./$D", "F=$(printf x); echo {} > \"$F\"", "find \"$D\" -delete",
            "dd if=/dev/zero of=$F", "rm -rf \"$UNKNOWN\"/x", "rm -f /tmp/$NAME", "cp /tmp/x \"$DEST\"",
            "mv \"$SRC\" /tmp/x", "curl -o \"$F\" http://127.0.0.1:9/x", "sort -o $F /tmp/in",
            "tar -xf a.tar -C \"$D\"", "unzip -d $D a.zip", "sed -i s/a/b/ \"$F\"", "echo x >> ${F}.json",
            "find ${D} -name '*.json' -exec rm {} +", "touch `printf logs`/x", "$RM -rf logs",
            "$(which rm) -rf logs", "for f in logs/*; do rm \"$f\"; done", "D=build; for D in logs; do rm -rf $D; done",
            "D=build; D=$(printf logs); rm -rf $D", "D=lo; D+=gs; rm -rf $D", "while read f; do rm \"$f\"; done < /tmp/l",
            "ln -s /tmp/x \"$L\"", "tee -a \"$F\" < /tmp/x", "wget -O $F http://127.0.0.1:9/x",
        ])
        self.assertNoneGroundTruth([
            "dd if=$F of=/tmp/out", "cp \"$SRC\" /tmp/x", "sed -i \"s/a/$B/\" /tmp/f", "cat $F", "\"$PY\" -m pytest",
            "eval \"$(ssh-agent -s)\"", "echo hi > /dev/null 2>&1", "for f in logs/*; do cat \"$f\"; done",
            "echo \"$(date)\" > /tmp/stamp", "D=build; rm -rf $D", "export D=build; rm -rf ${D}/x",
        ])

    def test_over_denial_fixes(self):
        # Items 10 / 11: ENV only for shells, tar modes parsed, single-file copies
        self.write_fixture_scripts()
        self.assertNoneGroundTruth([
            "ENV=test python3 -m pytest", "tar -czf /tmp/o.tgz index.txt", "tar czf /tmp/o.tgz index.txt",
            "tar -tf a.tar", "tar tvf a.tar", "tar -xOf a.tar member", "tar -xf a.tar -C /tmp/out",
            "tar --extract --file=a.tar --directory=/tmp/out", "copy a.txt .", "copy a.txt logs",
            "robocopy /tmp/src .", "xcopy /tmp/src .", "unzip -l a.zip",
        ])
        self.assertAllGroundTruthDenied([
            "tar -xzf deps.tgz", "tar xzf deps.tgz", "tar --ext -f a.tar", "tar --get -f a.tar",
            "tar -xf a.tar --dir logs", "tar -xvzf a.tgz -C .", "tar --to-command='rm -rf logs' -xf a.tar -C /tmp/o",
            "tar -I 'rm -rf logs' -xf a.tar -C /tmp/o", "tar -cf /tmp/o.tar --checkpoint-action=exec='rm -rf logs' x",
            "copy /tmp/nonexistent.txt logs", "copy /tmp/* logs", "robocopy /tmp/src . /E", "robocopy /tmp/src . /MIR",
            "robocopy /tmp/src logs", "xcopy /tmp/src . /S",
        ])
        self.assertGroundTruthDenied(self.agy(self.cmd("tar -cf /tmp/o.tar -g logs/guardian_state.json x")),
                                     "guardian_state.json")

    def test_shell_keywords_wrappers_and_inline_shell_calls_denied(self):
        # Hardening found while closing the findings: keyword-prefixed commands, more wrappers, a quoted newline
        # argument, and shell commands run from inline code
        self.assertAllGroundTruthDenied([
            "if true; then rm -rf logs; fi", "true && { rm -rf logs; }", "! rm -rf logs",
            "while true; do rm -rf logs; done", "rm -rf '\n' logs", "setsid rm -rf logs", "sudo -u root rm -rf logs",
            "timeout -s KILL 5 rm -rf logs", "ionice -c 3 rm -rf logs", "nice -n 10 rm -rf logs",
            "watch -n 1 rm -rf logs", "watch 'rm -rf logs'", "flock /tmp/l -c 'rm -rf logs'",
            "flock /tmp/l rm -rf logs", "su -c 'rm -rf logs'", "env - rm -rf logs",
            "python3 -c \"import os; os.system('rm -rf logs')\"",
            "python3 -c \"import subprocess; subprocess.run(['rm', '-rf', 'logs'])\"",
            "perl -e 'system(\"rm -rf logs\")'", "ruby -e 'system(\"rm\", \"-rf\", \"logs\")'",
            "ruby -e '%x(rm -rf logs)'", "awk 'BEGIN{system(\"rm -rf logs\")}'",
            "node -e \"require('child_process').execSync('rm -rf logs')\"",
            "python3 - <<'EOF'\nimport os\nos.system('rm -rf logs')\nEOF",
        ])
        self.assertGroundTruthDenied(self.agy(self.cmd("time -o logs/guardian_state.json ls")), "guardian_state.json")
        self.assertNoneGroundTruth(["python3 -c \"import subprocess; subprocess.run(['ls', '-la'])\"",
                                    "if true; then ls logs; fi", "timeout 5 make test", "sudo -u root ls"])

    # ---------------------------------------------------------------- review round 7 findings (issue #53 round 3)
    REPORT_ISSUE = os.path.join(BASE_DIR, "scripts", "report_issue.sh")

    def test_report_issue_sh_pin_matches_the_real_file(self):
        with open(self.REPORT_ISSUE, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(pre_trade_guard.DESK_SHELL_SCRIPTS["scripts/report_issue.sh"], digest,
                         "scripts/report_issue.sh changed: update DESK_SHELL_SCRIPTS['scripts/report_issue.sh'] in "
                         f"scripts/hooks/pre_trade_guard.py to {digest} after reviewing the change")
        self.assertEqual(list(pre_trade_guard.DESK_SHELL_SCRIPTS), ["scripts/report_issue.sh"])
        self.assertIn("scripts/report_issue.sh", pre_trade_guard.HARNESS_FILES)
        res = self.agy({"toolCall": {"name": "write_to_file", "args": {"TargetFile": "scripts/report_issue.sh",
                                                                         "CodeContent": "x"}}})
        self.assertEqual(res.get("decision"), "force_ask")
        self.assertEqual(self.agy(self.cmd("cp /tmp/x.sh scripts/report_issue.sh")).get("decision"), "force_ask")

    def test_only_the_pinned_report_issue_sh_is_judged_with_relaxed_rules(self):
        # Round-2 finding 1: any other file under scripts/ (and an edited report_issue.sh) is judged strictly
        os.makedirs(os.path.join(self.root, "scripts"), exist_ok=True)
        desk = os.path.join(self.root, "scripts", "desk.sh")
        for body in ('cd "$(dirname "$0")/../logs" && rm -f *.json', 'D=$(printf logs); rm -rf "$D"',
                     "ls logs/*.json | xargs rm", 'tmp=$(mktemp); rm -f "$tmp"'):
            with open(desk, "w", encoding="utf-8") as f:
                f.write("#!/bin/bash\n" + body + "\n")
            for c in ("bash scripts/desk.sh", "./scripts/desk.sh"):
                self.assertDenied(self.agy(self.cmd(c)), "Ground Truth Protection")
        report = os.path.join(self.root, "scripts", "report_issue.sh")
        shutil.copyfile(self.REPORT_ISSUE, report)
        self.assertNoneGroundTruth([
            './scripts/report_issue.sh --title "x: y" --error "boom" --category tool_error --severity HIGH',
            "bash scripts/report_issue.sh --help",
        ])
        shutil.copyfile(self.REPORT_ISSUE, desk)  # same bytes, other path: not pinned
        self.assertAllGroundTruthDenied(["bash scripts/desk.sh"])
        with open(report, "a", encoding="utf-8") as f:
            f.write("\n# edited\n")  # pin mismatch: strict
        self.assertAllGroundTruthDenied(["./scripts/report_issue.sh --title x --error y"])

    def test_scripts_run_from_a_pinned_script_are_strict(self):
        os.makedirs(os.path.join(self.root, "scripts"), exist_ok=True)
        inner = os.path.join(self.root, "inner.sh")
        with open(inner, "w", encoding="utf-8") as f:
            f.write('rm -f "$UNKNOWN"\n')
        outer = os.path.join(self.root, "scripts", "outer.sh")
        for body, denied in (('rm -f "$tmp"\n', False), ('rm -f "$tmp"\nsource inner.sh\n', True),
                             ('bash "$SCRIPT"\n', True),
                             ('bash -c \'rm -f "$x"\'\n', True), ("ls logs/*.json | xargs rm\n", True)):
            data = ("#!/bin/bash\n" + body).encode()
            with open(outer, "wb") as f:
                f.write(data)
            with patch.dict(pre_trade_guard.DESK_SHELL_SCRIPTS,
                            {"scripts/outer.sh": hashlib.sha256(data).hexdigest()}):
                res = self.agy(self.cmd("bash scripts/outer.sh"))
            self.assertEqual(res.get("decision") == "deny", denied, body)

    def test_audit_budget_is_fail_closed_and_bounded(self):
        # Round-2 finding 2: cwd candidates x nested strings no longer multiply; past the budget -> deny
        fan = ('cd a;cd b;cd c;cd d;bash -c "cd a;cd b;cd c;cd d;bash -c \'cd a;cd b;cd c;cd d;eval \\"cd a;cd b;'
               'cd c;cd d;eval :\\"\'"; rm -rf logs')
        started = time.monotonic()
        self.assertDenied(self.agy(self.cmd(fan)), "Ground Truth Protection")
        self.assertLess(time.monotonic() - started, 2.0)
        started = time.monotonic()
        self.assertEqual(self.agy(self.cmd(fan.replace("; rm -rf logs", ""))).get("decision"), "ask")
        self.assertLess(time.monotonic() - started, 2.0)
        too_many = "; ".join(["true"] * (pre_trade_guard.AUDIT_MAX_SUBCOMMANDS + 10))
        self.assertDenied(self.agy(self.cmd(too_many)), "too complex to audit")
        with patch("pre_trade_guard.AUDIT_DEADLINE_SECONDS", 0.0):
            self.assertDenied(self.agy(self.cmd("ls -la; git status")), "too complex to audit")
        for c in ("ls -la", "git status && git diff --stat", "python3 -m pytest -q 2>&1 | tail -20"):
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "ask", c)
        self.assertEqual(pre_trade_guard._AUDIT["active"], 0)

    def test_git_ext_transport_and_protocol_keys_denied(self):
        # Round-2 finding 3
        self.assertAllGroundTruthDenied([
            "git -c protocol.ext.allow=always clone 'ext::sh -c rm% -rf% logs' /tmp/y",
            "GIT_ALLOW_PROTOCOL=ext git ls-remote 'ext::sh -c rm% -rf% logs'",
            "git config --global protocol.allow always", "git clone ext::sh /tmp/y", "git fetch EXT::sh",
            "git -c Protocol.Allow=always fetch origin", "git --config-env=protocol.ext.allow=V fetch origin",
            "GIT_PROTOCOL_FROM_USER=1 git fetch origin", "git config protocol.ext.allow user",
        ])
        self.assertNoneGroundTruth(["git -c protocol.ext.allow=never fetch origin",
                                    "git config --global protocol.file.allow never", "git fetch origin"])

    def test_programs_by_path_read_only_the_head_unless_shell_scripts(self):
        # Round-2 finding 6: a large binary / non-shell program invoked by path is classified from 4 KiB
        big = os.path.join(self.root, "bigbin")
        with open(big, "wb") as f:
            f.write(b"\x7fELF\0" + b"\0" * (pre_trade_guard.SCRIPT_READ_LIMIT * 2))
        bigpy = os.path.join(self.root, "bigpy")
        with open(bigpy, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\n" + "#" * (pre_trade_guard.SCRIPT_READ_LIMIT * 2))
        bigsh = os.path.join(self.root, "bigsh")
        with open(bigsh, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n" + "#" * (pre_trade_guard.SCRIPT_READ_LIMIT * 2))
        self.assertNoneGroundTruth(["./bigbin", "./bigpy"])
        self.assertAllGroundTruthDenied(["./bigsh"])


class TestRuntimeContracts(GuardHarness):

    def test_agy_mode_never_exits_non_zero(self):
        for raw in ("", "{bad json", json.dumps({"x": 1})):
            res = self.run_guard(raw, argv=["--agy"])
            self.assertEqual(res.get("decision"), "deny")
            self.assertEqual(res.get("__exit_code__"), 0)
            self.assertNotIn("code", res)

    def test_heartbeat_written(self):
        self.agy(self.cmd("ls"))
        with open(os.path.join(self.root, "logs", "hook_heartbeat.json"), encoding="utf-8") as f:
            hb = json.load(f)
        self.assertEqual(hb["hook"], "pre_trade_guard")
        self.assertEqual(hb["mode"], "agy")
        self.assertEqual(hb["decision"], "ask")
        self.assertEqual(hb["tool"], "run_command")

    def test_claude_code_mode_in_process(self):
        res = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "curl -X POST https://fapi.binance.com/fapi/v1/order"}})
        self.assertEqual(res.get("__exit_code__"), 2)
        self.assertIn("Raw HTTP write", res["__stderr__"])
        res = self.run_guard({"tool_name": "mcp__binance__futures_usds.newOrder", "tool_input": {"symbol": "BTCUSDT", "side": "BUY"}})
        self.assertEqual(res.get("__exit_code__"), 2)
        res = self.run_guard({"tool_name": "Write", "tool_input": {"file_path": "logs/evaluations/latest_dossier.json", "content": "{}"}})
        self.assertEqual(res.get("__exit_code__"), 2)
        res = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(res.get("__exit_code__"), 0)
        self.assertEqual(set(res) - {"__exit_code__", "__stderr__"}, set())

    def _subprocess(self, payload, *args):
        env = dict(os.environ)
        env.update(OFFLINE_ENV)
        env[pre_trade_guard.HEARTBEAT_ENV_OVERRIDE] = os.path.join(self.root, "hb.json")
        return subprocess.run([sys.executable, GUARD_SCRIPT, *args], input=json.dumps(payload), text=True,
                              capture_output=True, env=env, timeout=30)

    def test_claude_code_deny_exits_2_subprocess(self):
        p = self._subprocess({"tool_name": "Bash", "tool_input": {
            "command": "python3 -c \"import execute_futures_trade\""}})
        self.assertEqual(p.returncode, 2)
        self.assertIn("Inline code", p.stderr)

    def test_agy_subprocess_contract(self):
        p = self._subprocess(self.mcp("binance", "futures_usds.newOrder", {"symbol": "BTCUSDT", "side": "BUY"}), "--agy")
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertEqual(set(out), {"decision", "reason"})
        self.assertEqual(out["decision"], "deny")

    def test_agents_hooks_json_is_generic_and_relative(self):
        with open(os.path.join(BASE_DIR, ".agents", "hooks.json"), encoding="utf-8") as f:
            raw = f.read()
        for forbidden in ("/mnt/", "/usr/bin", "/home/", "C:\\", "Users"):
            self.assertNotIn(forbidden, raw)
        cfg = json.loads(raw)
        pre = cfg["trading-safety-guard"]["PreToolUse"][0]
        self.assertIn("../scripts/hooks/pre_trade_guard.py --agy", pre["hooks"][0]["command"])
        for tool in ("run_command", "call_mcp_tool", "mcp_tool", "write_to_file", "multi_replace_file_content"):
            import re
            self.assertTrue(re.fullmatch(pre["matcher"], tool), tool)
        agents_dir = os.path.join(BASE_DIR, ".agents")
        self.assertTrue(os.path.isfile(os.path.normpath(os.path.join(agents_dir, "../scripts/hooks/pre_trade_guard.py"))))


class TestPostTradeSync(GuardHarness):

    @patch("post_trade_sync.subprocess.run")
    def test_run_command_no_unbound_local_error(self, mock_run):
        with patch("post_trade_sync.find_workspace_root", return_value=self.root):
            res = post_trade_sync.handle_post_trade_sync(self.cmd("python3 scripts/execute_futures_trade.py --audit-orphans"))
        self.assertTrue(res["order_placed"])
        self.assertFalse(res["is_opening"])

    @patch("post_trade_sync.subprocess.run")
    def test_scans_inspection_and_retired_radar_do_not_sync(self, mock_run):
        for tool in ("get_crypto_newsletters", "scan_intraday_market", "deploy_futures_trade", "move_to_breakeven"):
            res = post_trade_sync.handle_post_trade_sync(self.mcp("crypto_radar", tool, {}))
            self.assertFalse(res["order_placed"], tool)
        for c in ("python3 scripts/broad_market_radar.py --json",
                  "python3 scripts/fetch_newsletters.py --format json",
                  "grep -n move-breakeven scripts/execute_futures_trade.py",
                  "git diff scripts/execute_futures_trade.py | cat",
                  "python3 scripts/execute_futures_trade.py --positions --json",
                  "python3 scripts/execute_futures_trade.py --help",
                  "python3 scripts/loops/position_guardian_loop.py --once --dry-run"):
            res = post_trade_sync.handle_post_trade_sync(self.cmd(c))
            self.assertFalse(res["order_placed"], c)
        mock_run.assert_not_called()

    @patch("post_trade_sync.subprocess.run")
    def test_executor_position_management_syncs_without_audit(self, mock_run):
        os.makedirs(os.path.join(self.root, "scripts"), exist_ok=True)
        open(os.path.join(self.root, "scripts", "sync_session_state.py"), "w").close()
        with patch("execute_futures_trade.audit_orphan_positions") as mock_audit, \
                patch("post_trade_sync.find_workspace_root", return_value=self.root):
            for c in ("python3 scripts/execute_futures_trade.py --move-breakeven --symbol BTCUSDT --env testnet",
                      "python3 scripts/execute_futures_trade.py --close-position --symbol BTCUSDT --env testnet",
                      "python3 scripts/execute_futures_trade.py --protect-pending --env testnet",
                      "python3 scripts/execute_futures_trade.py --protect_pending --env testnet",
                      "python3 scripts/loops/position_guardian_loop.py --once --env testnet"):
                res = post_trade_sync.handle_post_trade_sync(self.cmd(c))
                self.assertTrue(res["order_placed"], c)
                self.assertFalse(res["is_opening"], c)
                self.assertTrue(res["sync_attempted"], c)
            mock_audit.assert_not_called()
            res = post_trade_sync.handle_post_trade_sync(
                self.cmd("python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --env testnet"))
            self.assertTrue(res["is_opening"])
            mock_audit.assert_called_once_with(target_env="testnet", auto_heal=True)

    @patch("post_trade_sync.subprocess.run")
    def test_tool_execute_and_mcp_tool_names_recognized(self, mock_run):
        os.makedirs(os.path.join(self.root, "scripts"), exist_ok=True)
        open(os.path.join(self.root, "scripts", "sync_session_state.py"), "w").close()
        inner = {"toolName": "futures_usds.placeMultipleOrders",
                 "arguments": {"batchOrders": [{"symbol": "BTCUSDT", "side": "BUY"}]}}
        with patch("execute_futures_trade.audit_orphan_positions") as mock_audit, \
                patch("post_trade_sync.find_workspace_root", return_value=self.root):
            res = post_trade_sync.handle_post_trade_sync(self.mcp("binance", "tool_execute", inner, name="mcp_tool"))
            self.assertTrue(res["order_placed"])
            self.assertTrue(res["is_opening"])
            mock_audit.assert_called_once()
            res2 = post_trade_sync.handle_post_trade_sync(
                self.mcp("binance", "futures_usds.newAlgoOrder", {"symbol": "BTCUSDT", "closePosition": "true"}))
            self.assertTrue(res2["order_placed"])
            self.assertFalse(res2["is_opening"])

    def test_main_always_prints_empty_object(self):
        stdin, stdout = sys.stdin, sys.stdout
        try:
            sys.stdin, sys.stdout = io.StringIO("{not json"), io.StringIO()
            post_trade_sync.main()
            self.assertEqual(sys.stdout.getvalue().strip(), "{}")
        finally:
            sys.stdin, sys.stdout = stdin, stdout


class TestQuotedNewlineSplitting(GuardHarness):
    """Issue #53 round 4: only line breaks INSIDE a quoted string are arguments; a closing quote + newline + the next
    line's opening quote is still a command separator (two commands must never merge into one)."""

    CLOSE = "python3 scripts/execute_futures_trade.py --close-position --symbol BTCUSDT"

    def decision(self, command_line):
        return self.agy(self.cmd(command_line)).get("decision")

    def assertAllGroundTruthDenied(self, commands):
        for c in commands:
            res = self.agy(self.cmd(c))
            self.assertDenied(res, "Ground Truth Protection")
            self.assertIn("logs/guardian_state.json may only be written by", res.get("reason", ""), c)

    def assertNoneGroundTruth(self, commands):
        for c in commands:
            res = self.agy(self.cmd(c))
            self.assertNotIn("Ground Truth Protection", res.get("reason", ""), c)
            self.assertNotEqual(res.get("decision"), "deny", c)

    def test_closing_and_opening_quotes_across_lines_stay_separate_commands(self):
        for c in ("echo \"a\"\n\"rm\" -rf logs", "echo 'a'\n'rm' -rf logs",
                  self.CLOSE + " \"x\"\n\"rm\" -rf logs", self.CLOSE + " 'x'\n'rm' -rf logs",
                  self.CLOSE + " \"x\"\n\n\"rm\" -rf logs"):
            self.assertAllGroundTruthDenied([c])
        res = self.run_guard({"tool_name": "Bash", "tool_input": {"command": self.CLOSE + " \"x\"\n\"rm\" -rf logs"}})
        self.assertEqual(res.get("__exit_code__"), 2)
        self.assertEqual(pre_trade_guard.split_subcommands("echo \"a\"\n\"rm\" -rf logs"),
                         [["echo", "a"], ["rm", "-rf", "logs"]])

    def test_trade_opening_hidden_behind_risk_reducing_line_not_allowed(self):
        c = self.CLOSE + " \"x\"\n\"python3\" scripts/execute_futures_trade.py --symbol SOLUSDT --direction LONG"
        self.assertNotEqual(self.decision(c), "allow")
        self.assertNotEqual(self.decision(self.CLOSE + "\npython3 scripts/execute_futures_trade.py --symbol SOLUSDT "
                                          "--direction LONG"), "allow")

    def test_line_breaks_inside_quotes_are_arguments(self):
        self.assertNoneGroundTruth(["printf '%s' '\n'", "git commit -m \"a\n\nb\"",
                                    "cat <<'EOF' > /tmp/n.txt\nit's fine\nEOF"])
        self.assertEqual(pre_trade_guard.split_subcommands("printf '%s' '\n'"), [["printf", "%s", "\n"]])
        self.assertEqual(pre_trade_guard.split_subcommands("git commit -m \"a\n\nb\""),
                         [["git", "commit", "-m", "a\n\nb"]])

    def test_scan_tracks_escapes_and_comments_and_fails_safe_when_unbalanced(self):
        protect = pre_trade_guard._protect_quoted_newlines
        self.assertEqual(protect("echo 'a\nb'\nls"), "echo 'a__newline__b'\nls")
        self.assertEqual(protect("echo \"a\\\"\nb\"\nls"), "echo \"a\\\"__newline__b\"\nls")  # \" stays inside
        self.assertEqual(protect("echo 'a\\'\nls"), "echo 'a\\'\nls")  # backslash is literal inside '...'
        self.assertEqual(protect("echo \\'\nls\necho \\'"), "echo \\'\nls\necho \\'")  # escaped quotes outside
        # Issue #98: the helper now returns the scanner's command text: comments are removed (the line break kept)
        # and the line breaks stay real from the line where an unterminated quote opens
        self.assertEqual(protect("ls # it's\n'rm' -rf logs\necho 'x'"), "ls \n'rm' -rf logs\necho 'x'")
        self.assertEqual(protect("echo it's\nrm -rf logs"), "echo it's\nrm -rf logs")  # unbalanced: unchanged
        self.assertEqual(protect("echo 'a\nb'\necho it's\nls"), "echo 'a__newline__b'\necho it's\nls")
        self.assertEqual(protect("echo a#'\nb'"), "echo a#'__newline__b'")  # '#' inside a word is not a comment
        self.assertAllGroundTruthDenied(["ls # it's\n'rm' -rf logs\necho 'x'", "echo it's\nrm -rf logs"])

    def test_risk_reducing_alone_still_allowed(self):
        self.assertEqual(self.decision(self.CLOSE), "allow")
        # Round 5: a multi-line command is never auto-allowed, even when every line is risk-reducing
        self.assertEqual(self.decision(self.CLOSE + "\n" + self.CLOSE.replace("BTCUSDT", "ETHUSDT")), "ask")


class TestNestedNewlinesAndAutoAllowFailSafe(GuardHarness):
    """Issue #53 round 5: nested command strings keep their real line breaks; an auto-allow needs a flat single-line
    command; risk-reducing scripts count only when they are the script actually executed."""

    CLOSE = "python3 scripts/execute_futures_trade.py --close-position --symbol BTCUSDT"
    CUTOFF = "python3 scripts/loops/night_cutoff_loop.py --env prod"

    def decision(self, command_line):
        return self.agy(self.cmd(command_line)).get("decision")

    def test_nested_command_strings_are_judged_line_by_line(self):
        for c in ("bash -c 'true\nrm -rf logs'", "eval \"true\nrm -rf logs\"", "sh -c 'true\nrm -rf logs'",
                  "bash -c '" + self.CUTOFF + "\nrm -rf logs'"):
            self.assertDenied(self.agy(self.cmd(c)), "Ground Truth Protection")
        res = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "bash -c '" + self.CUTOFF + "\nrm -rf logs'"}})
        self.assertEqual(res.get("__exit_code__"), 2)
        self.assertEqual(pre_trade_guard.split_subcommands("bash -c 'true\nrm -rf logs'"),
                         [["bash", "-c", "true\nrm -rf logs"]])

    def test_hidden_lines_never_auto_allowed(self):
        # Issue #98: the lines a comment / ANSI-C string / quoted "<<EOF" used to hide are now seen, and denied
        for c in (self.CLOSE + " # it's\nrm -rf logs\necho \\'",
                  self.CLOSE + " $'\\''\nrm -rf logs\necho \\'",
                  "python3 scripts/loops/night_cutoff_loop.py \"<<EOF\"\nrm -rf logs close_position.py",
                  "rm -rf logs close_position.py"):
            self.assertDenied(self.agy(self.cmd(c)), "Ground Truth Protection")
            res = self.run_guard({"tool_name": "Bash", "tool_input": {"command": c}})
            self.assertEqual(res.get("__exit_code__"), 2, c)

    def test_flat_single_line_required_for_allow(self):
        # Each of these is risk-reducing for the analysis but carries text it cannot vouch for: ask, never deny
        for c in (self.CLOSE + " # note", self.CLOSE + "\n" + self.CUTOFF, self.CLOSE + " --note $'x'",
                  self.CLOSE + " --note \"$(date)\"", self.CLOSE + " --note `date`",
                  self.CUTOFF + " <<< x", self.CLOSE + " --note 'a\nb'", "\n" + self.CLOSE):
            self.assertEqual(self.decision(c), "ask", c)
        blocker = pre_trade_guard._auto_allow_blocker
        self.assertIn("here-string", blocker(self.CLOSE + " <<< x"))  # (denied anyway: inline code + executor)
        self.assertIsNone(blocker(self.CLOSE))
        self.assertIsNone(blocker(self.CLOSE + "\n"))  # trailing whitespace only
        self.assertIsNone(blocker(self.CLOSE + " --note 'a # b' --tag x#y"))  # quoted / mid-word '#'
        self.assertIsNotNone(blocker(self.CLOSE + " --note __newline__"))
        self.assertIsNotNone(blocker(self.CLOSE + "\r" + self.CUTOFF))
        # Trade openings: gates pass, but a multi-line command is downgraded to ask too
        self.write_provenance_dossier("BTCUSDT", "LONG")
        opening = "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --leverage 3 --env prod"
        self.assertEqual(self.agy(self.cmd(opening, conversationId=PARENT_CONV_ID)).get("decision"), "allow")
        self.assertEqual(self.agy(self.cmd(opening + "\nls -la",
                                           conversationId=PARENT_CONV_ID)).get("decision"), "ask")
        # Issue #98: a comment no longer hides the next lines, so the destructive line is denied
        self.assertDenied(self.agy(self.cmd(opening + " # it's\nrm -rf logs\necho \\'",
                                            conversationId=PARENT_CONV_ID)), "Ground Truth Protection")
        # PowerShell: a multi-line command is not auto-allowed either
        ps = pre_trade_guard.evaluate_powershell_command
        self.assertEqual(ps("python scripts\\execute_futures_trade.py --close-position --symbol BTCUSDT",
                            self.root, self.root, None)[0], "allow")
        self.assertEqual(ps("python scripts\\execute_futures_trade.py --close-position --symbol BTCUSDT\r\n"
                            "python scripts\\execute_futures_trade.py --close-position --symbol ETHUSDT",
                            self.root, self.root, None)[0], "ask")

    def test_risk_reducing_script_must_be_the_executed_one(self):
        script = pre_trade_guard._executed_script
        self.assertEqual(script(["python3", "scripts/loops/night_cutoff_loop.py", "--once"]),
                         "scripts/loops/night_cutoff_loop.py")
        self.assertEqual(script(["python3", "-u", "-X", "dev", "-Wignore", "scripts/close_position.py"]),
                         "scripts/close_position.py")
        self.assertEqual(script(["env", "X=1", "./close_position.py"]), "./close_position.py")
        self.assertEqual(script(["python3", "-c", "x", "close_position.py"]), "")
        self.assertEqual(script(["python3", "-m", "close_position.py"]), "")
        self.assertEqual(script(["rm", "-rf", "logs", "close_position.py"]), "rm")

        def rr(tokens):
            return pre_trade_guard._subcommand_is_risk_reducing(tokens, " ".join(tokens), self.root, self.root)

        for tokens in (["rm", "-rf", "logs", "close_position.py"], ["echo", "night_cutoff_loop.py"],
                       ["python3", "evil_close_position.py"], ["python3", "-c", "x", "close_position.py"],
                       # Issue #100: the retired ghost names are not sanctioned (an agent-made file would be)
                       ["python3", "scripts/close_position.py"], ["python3", "scripts/close_position_market.py"],
                       ["./scripts/audit_orphan_positions.py"],
                       # a night_cutoff_loop.py flag that does not exist
                       ["python3", "scripts/loops/night_cutoff_loop.py", "--once"]):
            self.assertFalse(rr(tokens), tokens)
        for tokens in (["python3", "scripts/loops/night_cutoff_loop.py"],
                       ["python3", "scripts/loops/night_cutoff_loop.py", "--env", "prod", "--auto-ratchet"],
                       ["python3", "scripts/loops/night_cutoff_loop.py", "--overnight-mode=CLOSE_ALL_AT_MARKET"],
                       ["./scripts/loops/night_cutoff_loop.py", "--env", "prod"]):
            self.assertTrue(rr(tokens), tokens)
        self.assertNotEqual(self.decision("echo night_cutoff_loop.py"), "allow")
        self.assertNotEqual(self.decision("python3 scripts/close_position.py"), "allow")
        self.assertEqual(self.decision(self.CUTOFF), "allow")

    def test_sanctioned_and_quoted_newline_commands_unchanged(self):
        self.assertEqual(self.decision(self.CLOSE), "allow")
        for c in ("git commit -m \"a\n\nb\"", "printf '%s' '\n'", "cat <<'EOF' > /tmp/n.txt\nit's fine\nEOF"):
            res = self.agy(self.cmd(c))
            self.assertNotEqual(res.get("decision"), "deny", c)
            self.assertNotIn("Ground Truth Protection", res.get("reason", ""), c)


class TestCommentAndHeredocLexicalScan(GuardHarness):
    """Issue #98: comments and heredoc bodies with apostrophes must not hide later lines from Ground Truth Protection.
    One bash-aware lexical pre-pass (_scan_shell) strips comments, joins line continuations, cuts heredoc bodies out
    (tokenized on their own) and confines an unterminated quote to the lines from where it opens."""

    CLOSE = "python3 scripts/execute_futures_trade.py --close-position --symbol BTCUSDT"
    CUTOFF = "python3 scripts/loops/night_cutoff_loop.py --env prod"

    def assertDeniedBothRuntimes(self, commands, fragment="Ground Truth Protection"):
        for c in commands:
            with self.subTest(command=c):
                self.assertDenied(self.agy(self.cmd(c)), fragment)
                res = self.run_guard({"tool_name": "Bash", "tool_input": {"command": c}})
                self.assertEqual(res.get("__exit_code__"), 2, c)

    def assertNotDenied(self, commands):
        for c in commands:
            with self.subTest(command=c):
                res = self.agy(self.cmd(c))
                self.assertNotEqual(res.get("decision"), "deny", c)
                self.assertNotIn("Ground Truth Protection", res.get("reason", ""), c)

    def test_issue_examples_denied(self):
        self.assertDeniedBothRuntimes([
            "ls # it's\nrm -rf logs\necho '",                                    # E1: comment apostrophe
            "cat <<EOF\n'\nEOF\nrm -rf logs\necho '",                            # E2: heredoc body quote
            "python3 scripts/loops/night_cutoff_loop.py \"<<EOF\"\nrm -rf logs",  # E3: quoted <<EOF
            "ls \\\n# it's\nrm -rf logs\necho '",                                # E4: # after a continuation
            "cat <<EOF\n'\nEOF\nrm -rf logs\necho ' # it's",                     # combined
            self.CUTOFF + " # it's\nrm -rf logs\necho '",
        ])

    def test_quoted_heredoc_operator_with_interpreter_head_is_not_a_heredoc(self):
        self.assertDeniedBothRuntimes([
            "python3 - '<<EOF'\nrm -rf logs", "python3 - \"<<EOF\"\nrm -rf logs",
            "node -e 'x' \"<<EOF\"\nrm -rf logs\nEOF", "python3 -c 'print(1)' '<<EOF'\nrm -rf logs\nEOF",
        ])
        self.assertEqual([k for k, _ in pre_trade_guard._scan_shell("python3 - '<<EOF'\nx")["pieces"]], ["code"])

    def test_heredoc_variants(self):
        self.assertDeniedBothRuntimes([
            # several heredocs on one line, the second body holding a quote
            "cat <<A <<B\na\nA\nit's\nB\nrm -rf logs\necho '",
            "cat <<A; cat <<'B'\nx\nA\ny'\nB\nrm -rf logs\necho '",
            # <<- strips the tabs of the body and of the terminator
            "cat <<-EOF\n\tit's\n\tEOF\nrm -rf logs\necho '",
            # quoted / escaped delimiter words
            "cat <<\"EOF\"\n'\nEOF\nrm -rf logs\necho '", "cat <<\\EOF\n'\nEOF\nrm -rf logs\necho '",
            "cat << 'E O'\n'\nE O\nrm -rf logs\necho '",
            # a python body is isolated too: its quote cannot reach the shell lines after the terminator
            "python3 - <<'EOF'\nx = \"it's\"\nEOF\nrm -rf logs\necho '",
        ])
        scan = pre_trade_guard._scan_shell("cat <<A <<-'B' > /tmp/x\na\nA\n\tb'\n\tB\nls")
        bodies = [v for k, v in scan["pieces"] if k == "body"]
        self.assertEqual([(h["delim"], h["quoted"], h["strip"], h["body"], h["terminated"]) for h in bodies],
                         [("A", False, False, "a", True), ("B", True, True, "b'", True)])
        self.assertEqual(scan["pieces"][-1], ("code", "ls"))
        unterminated = pre_trade_guard._scan_shell("cat <<EOF\nx\nrm -rf logs")
        self.assertEqual([v["body"] for k, v in unterminated["pieces"] if k == "body"], ["x\nrm -rf logs"])

    def test_ansi_c_strings_comments_and_here_strings(self):
        self.assertDeniedBothRuntimes([
            "echo $'it\\'s'\nrm -rf logs\necho \\'",
            "ls # <<EOF\nrm -rf logs\nEOF",                 # a comment never opens a heredoc
            "cat <<< x\nrm -rf logs\nx",                    # a here-string is not a heredoc
            "cat <<< \"it's\"\nrm -rf logs",
            "bash -c $'echo hi\\nrm -rf l\\x6fgs'",          # $'...' escapes decoded (\\n, \\x6f)
            "rm -rf $'\\154ogs'",                            # octal escape
        ])
        self.assertEqual(pre_trade_guard.split_subcommands("echo $'it\\'s'\nls"), [["echo", "$it's"], ["ls"]])
        self.assertEqual([k for k, _ in pre_trade_guard._scan_shell("cat <<< x\nls\nx")["pieces"]], ["code"])
        self.assertIn(["rm", "-rf", "logs"], pre_trade_guard.split_subcommands("ls # it's\nrm -rf logs\necho '"))
        self.assertEqual(pre_trade_guard.split_subcommands("ls \\\n-la"), [["ls", "-la"]])

    def test_heredoc_fed_to_a_shell_is_judged(self):
        self.assertDeniedBothRuntimes([
            "sh <<'EOF'\necho x > logs/guardian_state.json\nEOF",
            "cat <<'EOF' | sh\nrm -rf logs\nEOF",
            "bash -s <<'EOF'\n# it's\nrm -rf logs\n# that's\nEOF",
            "echo \"$(bash <<'EOF'\nrm -rf logs\nEOF\n)\"",
        ])
        self.assertNotDenied(["bash <<'EOF'\nls # it's\nEOF", "sh <<'EOF'\necho ok\nEOF"])

    def test_heredoc_inside_quoted_command_substitution(self):
        # The body is part of the quoted argument (a commit message); its quote cannot pair with one outside
        self.assertDeniedBothRuntimes([
            "echo \"$(cat <<'EOF'\n\"\nEOF\n)\"\nrm -rf logs\necho \"",
            "x=$(cat <<'EOF'\n'\nEOF\n)\nrm -rf logs\necho '",
            # a one-line $(cat <<EOF) is lifted but its operator stays for the scanner (its body follows)
            "echo \"$(cat <<EOF)\"\n'\nEOF\nrm -rf logs\necho '",
            # bash expands $(...) in an unquoted body, also one fed to python
            "python3 - <<EOF\nx = \"$(rm -rf logs)\"\nEOF",
        ])
        self.assertNotDenied([
            "git commit -m \"$(cat <<'EOF'\nfix(guard): it's the \"scanner\", don't panic\n\nCo-Authored-By: x\nEOF\n)\"",
        ])
        tokens = pre_trade_guard.split_subcommands("git commit -m \"$(cat <<'EOF'\nit's \"x\"\nEOF\n)\"")
        self.assertEqual(tokens, [["git", "commit", "-m", "$(cat <<'EOF'\nit's \"x\"\nEOF\n)"]])

    def test_arithmetic_shift_is_never_an_interpreter_heredoc(self):
        self.assertDeniedBothRuntimes([
            "echo $((1<<2))\nrm -rf logs",
            "python3 -c 'print(1)' $((1<<X))\nrm -rf logs\nX",
            "(( python3 <<= 1 ))\nrm -rf logs\n=",
        ])

    def test_acceptance_negatives_unaffected(self):
        self.assertNotDenied([
            "git commit -m \"a\n\nb\"", "printf '%s' '\n'", "cat <<'EOF' > /tmp/n.txt\nit's fine\nEOF",
            "git commit -F - <<'EOF'\nfix(guard): it's a lexer change, don't panic\n\nIt's tested.\nEOF",
            "cat > /tmp/msg.txt <<'EOF'\nwe're done # it's\nEOF",
        ])

    def test_auto_allow_blocker_uses_the_scanner(self):
        blocker = pre_trade_guard._auto_allow_blocker
        self.assertIn("comment", blocker(self.CLOSE + " #x"))
        self.assertIsNone(blocker(self.CLOSE + " --note 'a # b' --tag x#y"))
        self.assertIsNotNone(blocker(self.CLOSE + " --note $'#'"))  # ANSI-C string (raw marker)
        self.assertEqual(self.decision(self.CLOSE), "allow")
        self.assertEqual(self.decision(self.CLOSE + " # it's fine"), "ask")

    def decision(self, command_line):
        return self.agy(self.cmd(command_line)).get("decision")

    def test_round2_heredoc_closed_by_delimiter_paren_inside_substitution(self):
        # bash ends a heredoc inside $(...) at `EOF)`: the lines after it are commands
        self.assertDeniedBothRuntimes([
            "git commit -m \"$(cat <<'EOF'\nmsg\nEOF)\"\nrm -rf logs\nEOF\n)\"",
            "X=\"$(python3 - <<EOF\np\nEOF)\"\nrm -rf logs\necho '",
            "X=$(python3 - <<EOF\np\nEOF)\nrm -rf logs",
        ])

    def test_round2_ansi_c_delimiter_and_unterminated_interpreter_body(self):
        self.assertDeniedBothRuntimes([
            "python3 - <<$'EOF'\nx\nEOF\nrm -rf logs\nEOF", "python3 - <<$\"EOF\"\nx\nEOF\nrm -rf logs\nEOF",
            # an unterminated body fed to python is judged (fail closed)
            "python3 - <<EOF\nx\nrm -rf logs", "cat <<'EOF' | node\nrm -rf logs",
        ])
        word = pre_trade_guard._heredoc_word
        self.assertEqual(word("<<$'EOF'", 0), (8, "EOF", True, False))
        self.assertEqual(word("<<$\"EOF\"", 0), (8, "EOF", True, False))
        self.assertNotEqual(self.decision("python3 - <<'EOF'\nprint('ok')\nEOF"), "deny")

    def test_round2_comment_after_bare_arithmetic_command(self):
        self.assertDeniedBothRuntimes(["((1))# it's\nrm -rf logs\necho '", "(( x = 1 )) # it's\nrm -rf logs\necho '"])
        self.assertFalse(pre_trade_guard._scan_shell("echo $((1))#x")["has_comment"])  # $((...)) is part of a word
        self.assertTrue(pre_trade_guard._scan_shell("((1))#x")["has_comment"])

    def test_round2_case_inside_substitution_fails_closed(self):
        self.assertDeniedBothRuntimes(["X=\"$(case a in a) echo \"it's\";; esac)\"\nrm -rf logs\necho '",
                                       "X=$(case a in a) echo ok;; esac)", "echo \"$(true; case a in *) ls;; esac)\""],
                                      fragment="FAIL-CLOSED")
        # `case` as an argument, in a heredoc message body or in quotes is not a case command
        self.assertNotDenied([
            "X=$(git log --grep case -1)", "git log --grep case $(git rev-parse HEAD)", "echo \"$(echo 'case x')\"",
            "git commit -m \"$(cat <<'EOF'\nfix: handle the case where it's empty\ncase x in a) b;; esac\nEOF\n)\"",
            "gh pr create --title x --body \"$(cat <<'EOF'\n## Summary\n- it's done (case 1)\nEOF\n)\"",
            "case x in a) ls;; esac",
        ])

    def test_round3_case_in_a_data_heredoc_body_falls_back_to_line_checks(self):
        # Data for cat / tee: the scan failure falls back to the strict line-by-line check, not a denial
        self.assertNotDenied(["cat > /tmp/s.sh <<'EOF'\nx=$(case $1 in a) echo 1;; esac)\nEOF",
                              "tee /tmp/s.sh <<'EOF' >/dev/null\nn=$(case a in a) ls;; esac)\nEOF"])
        self.assertDeniedBothRuntimes(["cat > /tmp/s.sh <<'EOF'\nx=$(case $1 in a) echo 1;; esac)\nrm -rf logs\nEOF"])
        # A shell runs the body: the scan failure still denies
        self.assertDeniedBothRuntimes(["bash <<'EOF'\nx=$(case $1 in a) echo 1;; esac)\nEOF",
                                       "cat <<'EOF' | sh\nx=$(case $1 in a) echo 1;; esac)\nEOF",
                                       "eval \"$(cat <<'EOF'\nx=$(case a in a) ls;; esac)\nEOF\n)\""],
                                      fragment="FAIL-CLOSED")
        feeds = pre_trade_guard._heredoc_feeds_shell
        self.assertTrue(feeds("bash -s ", ""))
        self.assertTrue(feeds("cat ", " | /bin/sh"))
        self.assertFalse(feeds("cat > /tmp/s.sh ", ""))
        self.assertFalse(feeds("cat ", " | shasum"))

    def test_round2_arithmetic_shift_and_eval_of_a_heredoc_message(self):
        scan = pre_trade_guard._scan_shell("echo $((1<<2))\nls")
        self.assertEqual(scan["pieces"], [("code", "echo $((1<<2))\nls")])
        self.assertEqual(pre_trade_guard.split_subcommands("echo $((1<<2))\nls")[-1], ["ls"])
        self.assertDeniedBothRuntimes(["echo $(( (1<<2) ))\nrm -rf logs"])
        # `$((cat <<EOF` (a subshell whose heredoc bash reads) spanning lines: too ambiguous, denied
        self.assertDeniedBothRuntimes(["echo $((cat <<EOF\n'\nEOF\n) )\nrm -rf logs\necho '"], fragment="FAIL-CLOSED")
        # eval runs the "$(cat <<EOF ...)" text: its body is judged (only git / gh message arguments are not)
        self.assertDeniedBothRuntimes(["eval \"$(cat <<'EOF'\nrm -rf logs\nEOF\n)\"",
                                       "bash -c \"$(cat <<'EOF'\nrm -rf logs\nEOF\n)\""])

    def test_scanner_failure_fails_closed(self):
        with patch("pre_trade_guard._scan_shell", side_effect=pre_trade_guard.ShellScanError("boom")):
            self.assertEqual(self.decision("ls"), "deny")
            res = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
            self.assertEqual(res.get("__exit_code__"), 2)
        limit = pre_trade_guard.HEREDOC_NESTING_LIMIT + 2
        nested = "".join(f"bash <<E{k}\n" for k in range(limit)) + "ls\n" + "".join(
            f"E{k}\n" for k in reversed(range(limit)))
        self.assertEqual(self.decision(nested), "deny")
        shallow = "bash <<E0\nbash <<E1\nls\nE1\nE0"
        self.assertNotEqual(self.decision(shallow), "deny")


class TestLoneQuotedNewlineAndWslAutoAllow(GuardHarness):
    """Issue #53 round 6: a lone quoted line break is an argument holding a real newline (never the sentinel), so
    eval / sh -c see the same lines as bash; the executed-script match sees through wsl.exe; a PowerShell backtick
    escape is never auto-allowed."""

    CLOSE = "python3 scripts/execute_futures_trade.py --close-position --symbol BTCUSDT"
    CUTOFF = "python3 scripts/loops/night_cutoff_loop.py --env prod"
    WSL = "wsl.exe -d Ubuntu -- "

    def claude(self, tool, command):
        return self.run_guard({"tool_name": tool, "tool_input": {"command": command}})

    def test_eval_of_lone_quoted_newline_is_denied(self):
        for c in ("eval '\n' rm -rf logs", "eval \"\n\" rm -rf logs",
                  "eval '\n' git -c core.fsmonitor='curl x|sh' status", "eval '\n' rm -rf \"$D\""):
            self.assertDenied(self.agy(self.cmd(c)))
            self.assertEqual(self.claude("Bash", c).get("__exit_code__"), 2, c)
        self.assertDenied(self.agy(self.cmd("eval '\n' rm -rf logs")), "Ground Truth Protection")
        split = pre_trade_guard.split_subcommands
        self.assertEqual(split("eval '\n' rm -rf logs"), [["eval", "\n", "rm", "-rf", "logs"]])
        self.assertEqual(split("eval \"\n\" rm -rf logs"), [["eval", "\n", "rm", "-rf", "logs"]])
        self.assertEqual(split("bash -c '\n'"), [["bash", "-c", "\n"]])
        for c in ("eval '\n' rm -rf logs", "printf '%s' '\n'", "bash -c '\nrm -rf logs'"):
            for tokens in split(c):
                self.assertFalse(any(pre_trade_guard.QUOTED_NEWLINE_SENTINEL in t for t in tokens), c)
        # A quoted line break is still an argument, not a separator; a bare one is still a separator
        self.assertEqual(split("printf '%s' '\n' x"), [["printf", "%s", "\n", "x"]])
        self.assertEqual(split("echo a\necho b"), [["echo", "a"], ["echo", "b"]])

    def test_wsl_wrapped_sanctioned_commands_allowed(self):
        script = pre_trade_guard._executed_script
        self.assertEqual(script(["wsl.exe", "-d", "Ubuntu", "--", "python3", "scripts/loops/night_cutoff_loop.py",
                                 "--env", "prod"]), "scripts/loops/night_cutoff_loop.py")
        self.assertEqual(script(["wsl", "--cd", "/repo", "-u", "me", "-e", "python3", "scripts/trading_doctor.py"]),
                         "scripts/trading_doctor.py")
        self.assertEqual(script(["wsl.exe", "--exec", "./scripts/loops/night_cutoff_loop.py"]),
                         "./scripts/loops/night_cutoff_loop.py")
        self.assertEqual(script(["wsl.exe", "-d", "Ubuntu", "--", "rm", "close_position.py"]), "rm")
        self.assertEqual(script(["wsl.exe", "--import", "x", "close_position.py"]), "wsl.exe")
        self.assertEqual(script(["wsl.exe"]), "wsl.exe")

        def rr(tokens):
            return pre_trade_guard._subcommand_is_risk_reducing(tokens, " ".join(tokens), self.root, self.root)

        for tokens in (["wsl.exe", "-d", "Ubuntu", "--", "rm", "-rf", "/tmp/x", "night_cutoff_loop.py"],
                       # Issue #100: ghost names are not sanctioned, through wsl.exe either
                       ["wsl", "-e", "python3", "scripts/close_position.py"],
                       ["wsl.exe", "--exec", "./scripts/audit_orphan_positions.py"],
                       # a Windows spelling inside wsl is another file for Linux; an absolute path elsewhere too
                       ["wsl.exe", "--", "python3", "scripts\\loops\\night_cutoff_loop.py"],
                       ["wsl.exe", "--", "python3", "/tmp/scripts/loops/night_cutoff_loop.py"]):
            self.assertFalse(rr(tokens), tokens)
        for tokens in (["wsl.exe", "--exec", "./scripts/loops/night_cutoff_loop.py"],
                       ["wsl.exe", "-d", "Ubuntu", "--", "python3", self.root + "/scripts/loops/night_cutoff_loop.py"]):
            self.assertTrue(rr(tokens), tokens)
        # A Windows workspace root maps to the /mnt/<drive> Linux path (lexically; drive paths case-insensitive)
        sanctioned = pre_trade_guard._sanctioned_script
        self.assertEqual(sanctioned("/mnt/c/Repo/scripts/trading_doctor.py", "", "C:\\Repo", True),
                         "scripts/trading_doctor.py")
        self.assertEqual(sanctioned("C:\\Repo\\scripts\\trading_doctor.py", "", "/mnt/c/Repo", False),
                         "scripts/trading_doctor.py")
        self.assertEqual(sanctioned("scripts/trading_doctor.py", "C:\\Repo", "/mnt/c/Repo", False),
                         "scripts/trading_doctor.py")
        self.assertEqual(sanctioned("/mnt/c/repo/SCRIPTS/trading_doctor.py", "", "C:\\Repo", True),
                         "scripts/trading_doctor.py")
        self.assertEqual(sanctioned("scripts/trading_doctor.py", "/c/Repo", "/mnt/c/Repo", False),
                         "scripts/trading_doctor.py")
        self.assertEqual(sanctioned("/c/repo/scripts/trading_doctor.py", "C:\\Repo", "/mnt/c/Repo", False),
                         "scripts/trading_doctor.py")
        self.assertEqual(sanctioned("/c/repo/scripts/trading_doctor.py", "/c/Repo", "/mnt/c/Repo", False),
                         "scripts/trading_doctor.py")
        # Issue #110: Git Bash /c/x is the C: drive only for a Windows-side session cwd (else a Linux directory)
        for cwd in ("", "/mnt/c/Repo", "/home/u"):
            self.assertIsNone(sanctioned("/c/repo/scripts/trading_doctor.py", cwd, "/mnt/c/Repo", False), cwd)
        # Issue #110: \\wsl.localhost\<distro>\x maps to /x only for this distro (WSL_DISTRO_NAME, case-insensitive)
        with patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
            for cwd in ("\\\\wsl.localhost\\Ubuntu\\home\\u\\r", "\\\\wsl$\\Ubuntu\\home\\u\\r",
                        "\\\\WSL.LOCALHOST\\ubuntu\\home\\u\\r"):
                self.assertEqual(sanctioned("scripts/trading_doctor.py", cwd, "/home/u/r", False),
                                 "scripts/trading_doctor.py", cwd)
            self.assertEqual(sanctioned("\\\\wsl$\\Ubuntu\\home\\u\\r\\scripts\\trading_doctor.py", "C:\\Repo",
                                        "/home/u/r", False), "scripts/trading_doctor.py")
            # round 2: a UNC script operand maps only from a Windows-side cwd outside wsl
            self.assertIsNone(sanctioned("\\\\wsl$\\Ubuntu\\home\\u\\r\\scripts\\trading_doctor.py", "", "/home/u/r",
                                         False))
            for cwd in ("\\\\wsl.localhost\\Debian\\home\\u\\r", "\\\\wsl$\\Other\\home\\u\\r"):
                self.assertIsNone(sanctioned("scripts/trading_doctor.py", cwd, "/home/u/r", False), cwd)
            self.assertIsNone(sanctioned("\\\\wsl$\\Debian\\home\\u\\r\\scripts\\trading_doctor.py", "", "/home/u/r",
                                         False))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WSL_DISTRO_NAME", None)
            self.assertIsNone(sanctioned("scripts/trading_doctor.py", "\\\\wsl$\\Ubuntu\\home\\u\\r", "/home/u/r",
                                         False))
        self.assertIsNone(sanctioned("/c/Repo/scripts/trading_doctor.py", "", "/mnt/c/Repo", True))  # Linux /c
        self.assertIsNone(sanctioned("/home/u/R/scripts/trading_doctor.py", "", "/home/u/r", False))  # Linux case
        self.assertIsNone(sanctioned("C:\\Repo\\scripts\\trading_doctor.py", "", "/mnt/c/Repo", True))
        self.assertIsNone(sanctioned("scripts/trading_doctor.py", "/tmp", "/mnt/c/Repo", False))
        self.assertIsNone(sanctioned("../Repo/scripts/x.py", "", "/mnt/c/Repo", False))
        cutoff = self.WSL + self.CUTOFF
        self.assertEqual(self.agy(self.cmd(cutoff)).get("decision"), "allow")
        self.assertEqual(self.claude("Bash", cutoff).get("hookSpecificOutput", {}).get("permissionDecision"), "allow")
        ps = pre_trade_guard.evaluate_powershell_command
        self.assertEqual(ps(cutoff, self.root, self.root, None)[0], "allow")
        self.assertEqual(ps(self.WSL + self.CLOSE, self.root, self.root, None)[0], "allow")
        # Round-5 fail-safe still applies through wsl.exe
        self.assertEqual(self.agy(self.cmd(cutoff + " # it's")).get("decision"), "ask")
        self.assertEqual(ps(cutoff + "\r\n" + cutoff, self.root, self.root, None)[0], "ask")

    def test_powershell_backtick_never_auto_allowed(self):
        ps = pre_trade_guard.evaluate_powershell_command
        self.assertEqual(ps(self.CLOSE, self.root, self.root, None)[0], "allow")
        for c in (self.CLOSE.replace("BTCUSDT", "BTC`USDT"), self.CLOSE + " `", self.WSL + self.CUTOFF + " `"):
            decision, reason = ps(c, self.root, self.root, None)
            self.assertNotEqual(decision, "allow", c)
            self.assertNotEqual(decision, "deny", c)


class TestRiskReducingIdentityAndWslReparse(GuardHarness):
    """Issues #100 / #97: the risk-reducing auto-allow trusts only the exact sanctioned script (repo path, exclusive
    flag set) run as one flat command; wsl.exe arguments are judged again as the Linux shell re-parses them, from
    the Bash tool as well as from PowerShell."""

    E = "scripts/execute_futures_trade.py"
    WSL = "wsl.exe -d Ubuntu -- "

    def claude(self, tool, command):
        return self.run_guard({"tool_name": tool, "tool_input": {"command": command}})

    def decisions(self, command_line):
        """(agy, Claude Code Bash) decisions; Claude Code "ask" has no JSON output (passthrough)."""
        claude = self.claude("Bash", command_line)
        return (self.agy(self.cmd(command_line)).get("decision"),
                claude.get("hookSpecificOutput", {}).get("permissionDecision", "deny" if claude.get("__exit_code__")
                                                         == 2 else "ask"))

    def ps(self, command_line):
        return pre_trade_guard.evaluate_powershell_command(command_line, self.root, self.root, None)[0]

    def assertNotAllowed(self, command_line, powershell=True):
        for label, decision in zip(("agy", "claude"), self.decisions(command_line)):
            self.assertNotEqual(decision, "allow", f"{label}: {command_line!r}")
        if powershell:
            self.assertNotEqual(self.ps(command_line), "allow", f"powershell: {command_line!r}")

    def assertAllowed(self, command_line, powershell=True):
        self.assertEqual(self.decisions(command_line), ("allow", "allow"), command_line)
        if powershell:
            self.assertEqual(self.ps(command_line), "allow", f"powershell: {command_line!r}")

    def test_script_names_anywhere_in_the_text_not_auto_allowed(self):
        for c in ("touch /tmp/x position_guardian_loop.py --once", "touch x trading_doctor.py --heal",
                  "rm x trading_doctor.py --heal", "touch x execute_futures_trade.py --close-position",
                  "touch x scripts/loops/night_cutoff_loop.py", "echo scripts/trading_doctor.py --heal"):
            self.assertNotAllowed(c)

    def test_ghost_and_foreign_paths_not_auto_allowed(self):
        for c in ("python3 scripts/close_position.py", "python3 scripts/close_position_market.py",
                  "python3 scripts/audit_orphan_positions.py", "./scripts/close_position.py",
                  f"python3 /tmp/{self.E} --close-position --symbol BTCUSDT",
                  f"python3 /tmp/x/../{self.E} --close-position --symbol BTCUSDT",
                  "python3 /tmp/scripts/loops/position_guardian_loop.py --once",
                  self.WSL + f"python3 /tmp/{self.E} --close-position --symbol BTCUSDT"):
            self.assertNotAllowed(c)
        # The same script by its absolute workspace path is sanctioned
        self.assertAllowed(f"python3 {self.root}/{self.E} --close-position --symbol BTCUSDT", powershell=False)

    def test_mixed_close_and_open_flags_not_auto_allowed(self):
        for flags in ("--close-position --symbol BTCUSDT --direction LONG --leverage 15",
                      "--close-position --symbol BTCUSDT --dir LONG", "--auto-heal --is-yolo",
                      "--audit-orphans --confirmed", "--protect-pending --order-type LIMIT --limit-price 1",
                      "--close-position --symbol BTCUSDT --bypass-eval-gate --env testnet"):
            c = f"python3 {self.E} {flags}"
            self.assertNotAllowed(c)
            # an opening flag sends the sub-command to the trade gates (no dossier here: denied)
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)
        # Unknown flags, stray operands, abbreviations and switch values: not the sanctioned one-liner (ask)
        for args in ("--close-position --symbol BTCUSDT --note x", "--close-position --symbol BTCUSDT extra",
                     "--close-position --symbol BTCUSDT -- x", "--close-pos --symbol BTCUSDT",
                     "--close-position=1 --symbol BTCUSDT", "--close-position --symbol --env prod",
                     "--positions", "--help --interval 5"):
            c = f"python3 {self.E} {args}"
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "ask", c)

    def test_exclusive_flag_sets_of_the_other_scripts(self):
        for c in ("python3 scripts/loops/position_guardian_loop.py --interval 60",
                  "python3 scripts/loops/position_guardian_loop.py --dry-run",
                  "python3 scripts/loops/position_guardian_loop.py --once --interval 60",
                  "python3 scripts/trading_doctor.py", "python3 scripts/trading_doctor.py --auto-heal",
                  "python3 scripts/trading_doctor.py --heal --verbose",
                  "python3 scripts/loops/night_cutoff_loop.py --once",
                  "python3 scripts/loops/climax_watcher_loop.py --once",
                  "python3 scripts/user_profile.py --show"):
            self.assertNotAllowed(c, powershell=False)
        for c in ("python3 scripts/loops/position_guardian_loop.py --once --env prod --dry-run --close-dead-alpha --json",
                  "python3 scripts/trading_doctor.py --heal --env prod",
                  "python3 scripts/loops/night_cutoff_loop.py --env prod --auto-ratchet --overnight-mode CLOSE_ALL_AT_MARKET",
                  "python3 scripts/loops/climax_watcher_loop.py --help", "python3 scripts/user_profile.py -h"):
            self.assertAllowed(c, powershell=False)

    def test_directory_change_disqualifies_auto_allow(self):
        for c in (f"cd /tmp && python3 {self.E} --close-position --symbol BTCUSDT",
                  f"cd {self.root} && python3 {self.E} --close-position --symbol BTCUSDT",
                  f"pushd /tmp; python3 {self.E} --auto-heal", f"env -C /tmp python3 {self.E} --auto-heal",
                  f"wsl.exe --cd /tmp -- python3 {self.E} --auto-heal"):
            self.assertNotAllowed(c)
            self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)

    def test_risk_reducing_with_unsafe_sub_command_not_allowed(self):
        for c in (f"python3 {self.E} --close-position --symbol BTCUSDT && touch /tmp/x",
                  f"python3 scripts/loops/position_guardian_loop.py --once; python3 scripts/other.py",
                  f"python3 {self.E} --auto-heal | tee /tmp/out"):
            self.assertNotAllowed(c)
        # benign neighbours still allow (true / sleep / date)
        self.assertAllowed(f"sleep 1 && python3 {self.E} --auto-heal", powershell=False)

    def test_metacharacter_in_a_token_not_auto_allowed(self):
        for c in (f"python3 {self.E} --close-position --symbol 'BTCUSDT;x'",
                  f"python3 {self.E} --close-position --symbol 'BTC|USDT'",
                  f"python3 {self.E} --close-position --symbol 'BTC&USDT'",
                  f"python3 {self.E} --close-position --symbol 'BTC>USDT'",
                  f"python3 {self.E} --close-position --symbol '$X'",
                  f"python3 scripts/loops/position_guardian_loop.py --once > /tmp/g.log"):
            self.assertNotAllowed(c)
            self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)

    def test_wsl_shell_injection_not_allowed_from_bash_and_powershell(self):
        inject = self.WSL.replace(" -d Ubuntu", "") + \
            f"python3 {self.E} --close-position --symbol 'BTCUSDT;touch${{IFS}}/tmp/x'"
        self.assertNotAllowed(inject)
        # Without a run-time value the re-parse alone exposes the second command
        for c in (f"wsl.exe -- python3 {self.E} --close-position --symbol 'BTCUSDT;touch /tmp/x'",
                  f"wsl.exe -- python3 {self.E} --close-position --symbol 'BTCUSDT&&rm -rf logs'"):
            self.assertNotAllowed(c)
        self.assertEqual(self.agy(self.cmd(f"wsl.exe -- python3 {self.E} --close-position --symbol "
                                           "'BTCUSDT;rm -rf logs'")).get("decision"), "deny")
        lines = pre_trade_guard._bash_wsl_shell_commands(
            f"X=1 wsl.exe -d Ubuntu -- python3 {self.E} --symbol 'B;touch y' && wsl.exe -e ls 'a;b'")
        self.assertEqual(lines, [f"python3 {self.E} --symbol B;touch y"])
        self.assertEqual(pre_trade_guard._bash_wsl_shell_commands("wsl.exe --cd /tmp -- ls"), ["cd /tmp && ls"])

    def test_wsl_reparse_bounded_for_nested_wsl(self):
        nested = "wsl.exe -- " * (pre_trade_guard.NESTED_DEPTH_LIMIT + 3) + f"python3 {self.E} --auto-heal"
        self.assertEqual(self.agy(self.cmd(nested)).get("decision"), "deny")
        self.assertAllowed("wsl.exe -- wsl.exe -- " + f"python3 {self.E} --auto-heal", powershell=False)

    def test_sanctioned_one_liners_stay_allowed(self):
        for c in (f"python3 {self.E} --move-breakeven --symbol BTCUSDT",
                  self.WSL + f"python3 {self.E} --move-breakeven --symbol BTCUSDT",
                  f"python3 {self.E} --close-position --symbol BTCUSDT",
                  self.WSL + f"python3 {self.E} --close-position --symbol BTCUSDT --env prod --json",
                  f"python3 {self.E} --auto-heal", f"python3 {self.E} --audit-orphans --env prod",
                  f"python3 {self.E} --protect-pending", f"python3 {self.E} --move-breakeven --symbol=BTCUSDT --is-yolo",
                  "python3 scripts/loops/position_guardian_loop.py --once",
                  self.WSL + "python3 scripts/loops/night_cutoff_loop.py --env prod",
                  "python3 scripts/trading_doctor.py --heal",
                  "BINANCE_API_ENV=prod python3 scripts/trading_doctor.py --heal"):
            self.assertAllowed(c)
        self.assertEqual(self.ps(f"python scripts\\execute_futures_trade.py --close-position --symbol BTCUSDT"),
                         "allow")
        self.assertEqual(self.ps("python .\\scripts\\loops\\position_guardian_loop.py --once"), "allow")

    def test_audit_budget_small_for_sanctioned_one_liners(self):
        for c in (f"python3 {self.E} --close-position --symbol BTCUSDT",
                  self.WSL + f"python3 {self.E} --close-position --symbol BTCUSDT"):
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "allow", c)
            self.assertLessEqual(pre_trade_guard._AUDIT["count"], 4, c)


class TestRiskReducingPrefixesAndWindowsCwd(GuardHarness):
    """Issue #100 round 2: every level the executed-script walk crosses (wsl -e, env, sudo, python) is checked for
    directory changes, env assignments and interpreter options; Windows / Git Bash session cwds map onto the WSL
    workspace root; --is-yolo is allowed next to --move-breakeven only."""

    E = "scripts/execute_futures_trade.py"
    DOCTOR = "python3 scripts/trading_doctor.py --heal"

    def claude(self, command, tool="Bash", cwd=None):
        payload = {"tool_name": tool, "tool_input": {"command": command}}
        if cwd is not None:
            payload["cwd"] = cwd
        res = self.run_guard(payload)
        return res.get("hookSpecificOutput", {}).get("permissionDecision",
                                                     "deny" if res.get("__exit_code__") == 2 else "ask")

    def ps(self, command_line):
        return pre_trade_guard.evaluate_powershell_command(command_line, self.root, self.root, None)[0]

    def test_directory_change_inside_wsl_exec_levels_not_auto_allowed(self):
        for c in (f"wsl.exe -e env -C /tmp {self.DOCTOR}", f"wsl.exe -e sudo -D /tmp {self.DOCTOR}",
                  f"wsl.exe -e wsl.exe --cd /tmp -- {self.DOCTOR}", f"wsl.exe -- env -C /tmp {self.DOCTOR}",
                  f"wsl.exe -e env --chdir=/tmp python3 {self.E} --auto-heal"):
            for label, decision in (("agy", self.agy(self.cmd(c)).get("decision")), ("bash", self.claude(c)),
                                    ("powershell", self.ps(c))):
                self.assertNotEqual(decision, "allow", f"{label}: {c}")
                self.assertNotEqual(decision, "deny", f"{label}: {c}")
        self.assertEqual(self.agy(self.cmd(f"wsl.exe -e env {self.DOCTOR}")).get("decision"), "allow")

    def test_env_assignments_and_python_options_need_the_allowlist(self):
        for c in (f"PYTHONPATH=/tmp/x {self.DOCTOR}", f"env PYTHONPATH=/tmp/x {self.DOCTOR}",
                  f"PYTHONSTARTUP=/tmp/x.py {self.DOCTOR}", f"wsl.exe -e env PYTHONPATH=/tmp/x {self.DOCTOR}",
                  f"wsl.exe -d Ubuntu -- LD_PRELOAD=/tmp/x.so {self.DOCTOR}",
                  "python3 -i scripts/trading_doctor.py --heal", "python3 -I scripts/trading_doctor.py --heal",
                  "python3 -X importtime scripts/trading_doctor.py --heal",
                  f"python3 -W error {self.E} --auto-heal", f"xargs python3 {self.E} --auto-heal"):
            self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "allow", c)
            self.assertNotEqual(self.claude(c), "allow", c)
        for c in (f"BINANCE_API_ENV=testnet {self.DOCTOR}", f"BINANCE_AUTH_MODE=KEYS BINANCE_API_ENV=prod {self.DOCTOR}",
                  f"env PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 {self.DOCTOR}",
                  f"PYTHONDONTWRITEBYTECODE=1 python3 -u -B -X utf8 {self.E} --auto-heal",
                  f"python3 -Xutf8 {self.E} --close-position --symbol BTCUSDT",
                  f"MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- BINANCE_API_ENV=prod python3 -u {self.E} --auto-heal",
                  f"timeout 60 python3 {self.E} --auto-heal"):
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "allow", c)
            self.assertEqual(self.claude(c), "allow", c)

    def test_is_yolo_only_next_to_move_breakeven(self):
        for flags in ("--move-breakeven --symbol PEPEUSDT --is-yolo", "--move-breakeven --symbol PEPEUSDT --is_yolo",
                      "--move-breakeven --is-yolo --symbol PEPEUSDT --env prod --json"):
            c = f"python3 {self.E} {flags}"
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "allow", c)
            self.assertEqual(self.ps(c), "allow", c)
        for flags in ("--close-position --symbol PEPEUSDT --is-yolo", "--auto-heal --is-yolo",
                      "--symbol PEPEUSDT --is-yolo"):
            c = f"python3 {self.E} {flags}"
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)  # trade gates, no dossier

    def test_windows_and_git_bash_session_cwd_map_to_the_wsl_root(self):
        os.environ[pre_trade_guard.HEARTBEAT_ENV_OVERRIDE] = os.path.join(self.root, "heartbeat.json")
        real_root, self.root = self.root, "/mnt/c/Desk/Trading"
        try:
            for cwd in ("C:\\Desk\\Trading", "/c/Desk/Trading", "c:\\desk\\trading", "/mnt/c/Desk/Trading",
                        "C:\\Desk\\Trading\\scripts\\..", "C:/Desk/Trading/"):
                for c in (f"python3 {self.E} --close-position --symbol BTCUSDT", self.DOCTOR,
                          f"wsl.exe -d Ubuntu -- python3 {self.E} --move-breakeven --symbol BTCUSDT",
                          "python3 C:\\\\Desk\\\\Trading\\\\scripts\\\\trading_doctor.py --heal"):
                    self.assertEqual(self.claude(c, cwd=cwd), "allow", f"{cwd}: {c}")
                # Issue #110: a Git Bash /c/... script is the C: drive only when the session cwd is Windows-side
                git_bash = "python3 /c/Desk/Trading/scripts/trading_doctor.py --heal"
                if cwd.startswith("/mnt/"):
                    self.assertEqual(self.claude(git_bash, cwd=cwd), "ask", f"{cwd}: {git_bash}")
                else:
                    self.assertEqual(self.claude(git_bash, cwd=cwd), "allow", f"{cwd}: {git_bash}")
                self.assertEqual(self.claude("python scripts\\execute_futures_trade.py --auto-heal", "PowerShell",
                                             cwd), "allow", cwd)
            for cwd in ("C:\\Other", "/c/Desk", "D:\\Desk\\Trading"):
                self.assertNotEqual(self.claude(self.DOCTOR, cwd=cwd), "allow", cwd)
            # A native Linux cwd: /c/... is a Linux directory (also with no cwd at all)
            for cwd in ("/mnt/c/Desk/Trading", "/home/u", None):
                self.assertEqual(self.claude("python3 /c/Desk/Trading/scripts/trading_doctor.py --heal", cwd=cwd),
                                 "ask", cwd)
            # Inside wsl a /c/... path is a Linux directory, not the C: drive
            self.assertNotEqual(self.claude("wsl.exe -- python3 /c/Desk/Trading/scripts/trading_doctor.py --heal",
                                            cwd="C:\\Desk\\Trading"), "allow")
        finally:
            self.root = real_root


class TestRiskAutoAllowResiduals(GuardHarness):
    """Issues #110 / #111: only RISK_WRAPPERS keep the risk-reducing auto-allow (sudo / chroot / ... ask), a forced
    break-even asks, and a gated trade opening next to a blocked risk-reducing sub-command never ends "allow"."""

    E = "scripts/execute_futures_trade.py"
    DOCTOR = "python3 scripts/trading_doctor.py --heal"

    def decisions(self, command_line):
        """(agy, Claude Code Bash, PowerShell) decisions."""
        claude = self.run_guard({"tool_name": "Bash", "tool_input": {"command": command_line}})
        return (self.agy(self.cmd(command_line)).get("decision"),
                claude.get("hookSpecificOutput", {}).get("permissionDecision",
                                                         "deny" if claude.get("__exit_code__") == 2 else "ask"),
                pre_trade_guard.evaluate_powershell_command(command_line, self.root, self.root, None)[0])

    def assertAsk(self, command_line):
        for label, decision in zip(("agy", "bash", "powershell"), self.decisions(command_line)):
            self.assertEqual(decision, "ask", f"{label}: {command_line}")

    def test_privileged_root_and_shell_changing_wrappers_ask(self):
        close = f"python3 {self.E} --close-position --symbol BTCUSDT"
        for c in (f"sudo -i {self.DOCTOR}", f"sudo -s {self.DOCTOR}", f"sudo -E {self.DOCTOR}", f"sudo {self.DOCTOR}",
                  f"sudo -u root {close}", f"sudo --login {close}", f"doas {self.DOCTOR}",
                  f"chroot /tmp {self.DOCTOR}", f"setsid {self.DOCTOR}", f"ionice -c 3 {self.DOCTOR}",
                  f"taskset 1 {self.DOCTOR}", f"flock /tmp/l {self.DOCTOR}", f"time {self.DOCTOR}",
                  f"exec {self.DOCTOR}", f"command {self.DOCTOR}", f"uv run {self.DOCTOR}",
                  f"nice sudo {self.DOCTOR}", f"timeout 60 sudo -i {close}", f"env BINANCE_API_ENV=prod sudo {close}",
                  f"wsl.exe -e sudo -i {self.DOCTOR}", f"wsl.exe -d Ubuntu -- sudo -E {close}",
                  f"wsl.exe -d Ubuntu -- chroot /tmp {self.DOCTOR}"):
            self.assertAsk(c)

    def test_allowlisted_wrappers_keep_the_auto_allow(self):
        for c in (f"timeout 60 {self.DOCTOR}", f"timeout -s KILL 60 python3 {self.E} --auto-heal",
                  f"nice -n 10 {self.DOCTOR}", f"nohup {self.DOCTOR}", f"stdbuf -oL python3 {self.E} --auto-heal",
                  f"env BINANCE_API_ENV=prod {self.DOCTOR}",
                  f"nice timeout 60 env BINANCE_API_ENV=prod python3 -u {self.E} --close-position --symbol BTCUSDT",
                  f"wsl.exe -d Ubuntu -- nohup timeout 60 {self.DOCTOR}"):
            self.assertEqual(self.decisions(c), ("allow", "allow", "allow"), c)

    def test_forced_break_even_asks(self):
        for flags in ("--move-breakeven --symbol BTCUSDT --force", "--move-breakeven --symbol=BTCUSDT --force",
                      "--move-breakeven --symbol PEPEUSDT --is-yolo --force",
                      "--move-breakeven --symbol BTCUSDT --force --env prod --json"):
            c = f"python3 {self.E} {flags}"
            self.assertAsk(c)
            self.assertIn("--force", self.agy(self.cmd(c)).get("reason", ""), c)
        # --force only acts with --move-breakeven: next to another risk flag it is a no-op and stays allowed
        self.assertEqual(self.decisions(f"python3 {self.E} --close-position --symbol BTCUSDT --force"),
                         ("allow", "allow", "allow"))
        for flags in ("--move-breakeven --symbol BTCUSDT", "--move-breakeven --symbol PEPEUSDT --is-yolo"):
            c = f"python3 {self.E} {flags}"
            self.assertEqual(self.decisions(c), ("allow", "allow", "allow"), c)

    def test_wsl_user_and_foreign_distribution_ask(self):
        for c in (f"wsl.exe -u root -- {self.DOCTOR}", f"wsl.exe --user root -- {self.DOCTOR}",
                  f"wsl.exe -d Ubuntu -u root -e python3 {self.E} --auto-heal",
                  f"wsl.exe -d Debian -- {self.DOCTOR}", f"wsl --distribution Other -e python3 {self.E} --auto-heal",
                  f"wsl.exe -e wsl.exe -u root -- {self.DOCTOR}",
                  f"wsl.exe --shell-type login -- {self.DOCTOR}", f"wsl.exe --shell-type LOGIN -e {self.DOCTOR}",
                  f"wsl.exe -d Ubuntu --shell-type=login -- {self.DOCTOR}"):
            self.assertAsk(c)
        for c in (f"wsl.exe --shell-type standard -- {self.DOCTOR}", f"wsl.exe --shell-type none -e {self.DOCTOR}"):
            self.assertEqual(self.decisions(c), ("allow", "allow", "allow"), c)
        for c in (f"MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- python3 {self.E} --auto-heal",
                  f"wsl.exe -d ubuntu -- {self.DOCTOR}", f"wsl.exe --distribution UBUNTU -e {self.DOCTOR}",
                  f"wsl.exe -- {self.DOCTOR}"):
            self.assertEqual(self.decisions(c), ("allow", "allow", "allow"), c)
        os.environ.pop("WSL_DISTRO_NAME", None)  # unknown own distro: any -d asks, no -d still allowed
        self.assertAsk(f"wsl.exe -d Ubuntu -- {self.DOCTOR}")
        self.assertEqual(self.decisions(f"wsl.exe -- {self.DOCTOR}"), ("allow", "allow", "allow"))

    def test_unc_paths_map_only_from_a_windows_cwd_outside_wsl(self):
        sanctioned = pre_trade_guard._sanctioned_script
        unc = "//wsl.localhost/Ubuntu/home/u/r/scripts/trading_doctor.py"
        self.assertEqual(sanctioned(unc, "C:\\Repo", "/home/u/r", False), "scripts/trading_doctor.py")
        self.assertIsNone(sanctioned(unc, "C:\\Repo", "/home/u/r", True))  # inside wsl: a Linux // path
        for cwd in ("", "/home/u/r", "/mnt/c/Repo"):  # Linux-native cwd
            self.assertIsNone(sanctioned(unc, cwd, "/home/u/r", False), cwd)
        self.assertEqual(pre_trade_guard._lexical_host_path("//wsl.localhost/Ubuntu/x", git_bash=False),
                         "//wsl.localhost/Ubuntu/x")

    def test_move_breakeven_off_the_sanctioned_path_asks_never_denies(self):
        for c in ("python3 /tmp/elsewhere/scripts/execute_futures_trade.py --move-breakeven --symbol PEPEUSDT --is-yolo",
                  f"python3 {self.E} --move-breakeven --symbol PEPEUSDT --is-yolo --unknown",
                  f"python3 {self.E} --move-breakeven --symbol PEPEUSDT --is-y",
                  f"python3 {self.E} --move-breakeven --symbol PEPEUSDT --is-yolo --forc"):
            self.assertAsk(c)
        # An opening option next to --move-breakeven is still judged by the trade gates (no dossier: deny)
        self.assertEqual(self.agy(self.cmd(f"python3 {self.E} --move-breakeven --symbol PEPEUSDT --direction LONG"))
                         .get("decision"), "deny")

    def test_directory_change_before_a_pure_opening_keeps_the_gate_decision(self):
        opening = f"python3 {self.E} --symbol BTCUSDT --direction LONG"
        with patch.object(pre_trade_guard, "evaluate_trade_opening", return_value=("allow", "Gates passed.")):
            for line in (f"cd {self.root} && {opening}", f"pushd {self.root} && {opening}"):
                self.assertEqual(pre_trade_guard._evaluate_shell_command(line, self.root, self.root, None)[0],
                                 "allow", line)
            # ... but a blocked risk-reducing sub-command next to the opening still asks
            line = f"cd {self.root} && {opening} && python3 {self.E} --auto-heal"
            self.assertEqual(pre_trade_guard._evaluate_shell_command(line, self.root, self.root, None)[0], "ask")

    def test_trade_opening_with_blocked_risk_reducing_subcommand_never_allowed(self):
        opening = f"python3 {self.E} --symbol BTCUSDT --direction LONG"
        with patch.object(pre_trade_guard, "evaluate_trade_opening", return_value=("allow", "Gates passed.")):
            for line in (f"{opening} && sudo python3 {self.E} --auto-heal",
                         f"{opening} && PYTHONPATH=/tmp/x {self.DOCTOR}",
                         f"{opening} && wsl.exe -e env -C /tmp {self.DOCTOR}"):
                analysis = pre_trade_guard.analyze_run_command(line, self.root, self.root)
                self.assertTrue(analysis["risk_blocker"], line)
                decision, _reason = pre_trade_guard._evaluate_shell_command(line, self.root, self.root, None)
                self.assertEqual(decision, "ask", line)
            # Unchanged: a gated opening alone still passes
            self.assertEqual(pre_trade_guard._evaluate_shell_command(opening, self.root, self.root, None)[0],
                             "allow")


class TestGitConfigFileChannels(GuardHarness):
    """Issue #99: writing git config / hook files directly (.git/config, .git/hooks/*, ~/.gitconfig,
    ~/.config/git/config, worktree / submodule config, a .git gitdir file) is the same command channel as
    `git -c core.fsmonitor=...`: a later innocent git command runs the payload. Shell writes are denied, file tools
    require explicit confirmation, reads and git's own config handling are unaffected."""

    LABEL = "Git Config Channel Protection"

    WRITES = [
        "printf '[core]\\n\\tfsmonitor = rm -rf logs\\n' >> .git/config; git status",
        "echo 'rm -rf logs' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit; git commit -m x",
        "chmod +x .git/hooks/pre-commit",
        "cp /tmp/x .git/hooks/pre-commit",
        "cp /tmp/x .git/hooks",
        "cp -t .git/hooks /tmp/pre-commit",
        "install -m 755 /tmp/x .git/hooks/post-checkout",
        "mv /tmp/x .git/hooks/pre-push",
        "ln -s /tmp/x .git/hooks/post-checkout",
        "touch .git/hooks/pre-commit",
        "sed -i 's/a/b/' .git/config",
        "perl -pi -e 's/a/b/' .git/config",
        "dd if=/tmp/x of=.git/config",
        "curl -o .git/hooks/pre-commit http://127.0.0.1:9/x",
        "tar -xf /tmp/h.tar -C .git/hooks",
        "unzip /tmp/h.zip -d .git",
        "echo '[alias] st = !rm' | tee -a ~/.gitconfig",
        "echo x >> ~/.gitconfig",
        "echo x >> /home/u/.gitconfig.local",
        "echo x >> $HOME/.config/git/config",
        "echo x >> ${HOME}/.config/git/config",
        "echo x > /etc/gitconfig",
        "echo x >> .git/config.worktree",
        "echo x >> .git/worktrees/wt/config.worktree",
        "echo /tmp/evil > .git/worktrees/wt/commondir",
        "echo x >> .git/modules/sub/config",
        "echo x > .git/modules/sub/hooks/pre-commit",
        "echo 'gitdir: /tmp/e' > .git",
        "echo x > ./.GIT/CONFIG",
        "echo x > /abs/repo/.git/hooks/pre-commit",
        "echo x > 'C:\\Users\\x\\repo\\.git\\config'",
        "echo x > .git/hoo*/pre-commit",
        "echo x > .gi?/config",
        "cd .git/hooks && echo x > pre-commit",
        "cd .git && echo x >> config",
        "bash -c 'echo x >> .git/config'",
        "sh -c \"cp /tmp/x .git/hooks/pre-commit\"",
        "find /tmp/h -name x -exec cp {} .git/hooks/pre-commit \\;",
        "cat > .git/hooks/pre-commit <<EOF\nrm -rf logs\nEOF",
        "python3 -c \"open('.git/config','a').write('[core]')\"",
        "python3 -c \"import os; os.system('echo x >> .git/config')\"",
        "wsl.exe -e bash -c 'echo x >> .git/config'",
        # round 2: link sources (a link aliases its source)
        "ln -s .git/config x; echo y >> x", "ln .git/config x", "ln -t . .git/config", "ln -sr .git/config x",
        "link .git/config x", "cp -l .git/config x", "cp -s .git/config x", "cp --link .git/config x",
        "cp -as .git/config x", "ln -s .git/hooks h; cp /tmp/x h/pre-commit", "ln -s .git g",
        "rsync -a --link-dest=.git/hooks /tmp/h/ /tmp/out/",
        # round 2: programs whose writes are not modelled (catch-all)
        "patch .git/config /tmp/p.diff", "echo x | sponge .git/config", "ed -s .git/config < /tmp/cmds",
        "ex -sc wq .git/config", "vim -c wq .git/config", "awk -i inplace 1 .git/config",
        "perl -e \"open(F,q(>>.git/config))\"", "sudo vim .git/hooks/pre-commit", "nano ~/.gitconfig",
        "code .git/config", "xxd -r /tmp/x.hex > .git/config", "xxd -r /tmp/x.hex .git/config",
        "xxd -revert /tmp/x.hex .git/hooks/pre-commit",
        # round 2: git pointed at another global config / repository
        "HOME=/tmp/h git status", "XDG_CONFIG_HOME=/tmp/x git log -1", "env HOME=/tmp/h git status",
        "git --git-dir=/tmp/r/.git status", "git --git-dir /tmp/r/.git log",
    ]
    POWERSHELL_WRITES = [
        "Set-Content -Path .git\\hooks\\pre-commit -Value 'rm -rf logs'",
        "Add-Content .git/config '[core]'",
        "'x' | Out-File -FilePath $HOME\\.gitconfig -Append",
        "Copy-Item C:\\tmp\\x .git\\hooks\\pre-commit",
        "New-Item -ItemType File .git/hooks/post-checkout",
        "echo x > .git/config",
        "wsl.exe -- bash -c 'echo x >> .git/config'",
    ]
    UNAFFECTED = [
        "git status", "git commit -F msg.txt", "git config --get user.name", "cat .git/config",
        "git config -f .git/config --get core.hooksPath", "git config -f .git/config --list",
        "grep fsmonitor .git/config", "ls .git/hooks", "cp .git/config /tmp/config.bak", "cat ~/.gitconfig",
        "echo x >> .gitignore", "echo x >> .gitattributes", "echo x > .github/workflows/x.yml",
        "echo x >> .gitmodules", "git config -f .git/config user.name x", "git config user.name x",
        "rm .git/hooks/pre-commit.sample", "echo x > notes/git-config.md", "head -1 .git/HEAD",
        "mv .git/hooks/pre-commit /tmp/pre-commit.bak",
        "echo x >> .git/info/exclude", "echo x > .git/info/sparse-checkout",
        "git worktree add ../x -b y origin/main", "git worktree remove ../x --force", "git fetch -q origin",
        "git pull --ff-only origin main", "git merge --no-edit origin/main", "git branch -D y",
        "gh pr create --body-file f.md", "pre-commit install", "git commit -F .git/COMMIT_EDITMSG",
        "cp .git/config /tmp/x", "ln -s /tmp/a /tmp/b", "cp -a src/ /tmp/dst/", "HOME=/tmp/h ls",
        "git -C /tmp/r status", "vim notes.md", "wsl.exe -e cat .git/config",
        "du -sh .git", "tree .git/hooks", "od -c .git/config", "hexdump -C .git/config", "nl .git/config",
        "xxd .git/config", "xxd -g1 .git/hooks/pre-commit.sample",
    ]

    def bash(self, command_line):
        return self.run_guard({"tool_name": "Bash", "tool_input": {"command": command_line}})

    def ps(self, command_line):
        return pre_trade_guard.evaluate_powershell_command(command_line, self.root, self.root, None)

    def test_shell_writes_denied_with_the_git_channel_reason(self):
        failures = []
        for c in self.WRITES:
            res, claude = self.agy(self.cmd(c)), self.bash(c)
            if (res.get("decision") != "deny" or self.LABEL not in res.get("reason", "")
                    or "Ground Truth Protection" in res.get("reason", "") or claude.get("__exit_code__") != 2
                    or self.LABEL not in claude["__stderr__"]):
                failures.append((c, res.get("decision"), res.get("reason", "")[:90]))
        self.assertEqual(failures, [])

    def test_powershell_writes_denied(self):
        for c in self.WRITES[:4] + self.POWERSHELL_WRITES:
            decision, reason = self.ps(c)
            self.assertEqual(decision, "deny", c)
            self.assertIn(self.LABEL, reason, c)

    def test_reads_and_git_config_handling_unaffected(self):
        for c in self.UNAFFECTED:
            res = self.agy(self.cmd(c))
            self.assertNotEqual(res.get("decision"), "deny", f"{c}: {res}")
            self.assertNotIn(self.LABEL, res.get("reason", "") + res.get("__stderr__", ""), c)
        for c in ("Get-Content .git/config", "Select-String fsmonitor .git/config", "git status",
                  "git config -f .git/config --get core.hooksPath", "Add-Content .gitignore x"):
            decision, reason = self.ps(c)
            self.assertNotEqual(decision, "deny", f"{c}: {reason}")
        # A persistent `git config` write stays judged by the key rules only (issue #88)
        self.assertDenied(self.agy(self.cmd("git config -f .git/config core.hooksPath /tmp/h")))
        self.assertNotIn(self.LABEL, self.agy(self.cmd("git config -f .git/config core.hooksPath /tmp/h"))["reason"])

    def test_bash_wsl_linux_command_judged(self):
        # Found while fixing #99: from the Bash tool, `wsl.exe -e <cmd>` / `wsl.exe -- <cmd>` run <cmd> in Linux;
        # its tokens are judged like PowerShell's wsl calls (quoting kept), not only the re-parsed joined text
        for c in ("wsl.exe -e rm -rf logs", "wsl.exe -e bash -c 'rm -rf logs'", "wsl.exe -- bash -c 'rm -rf logs'",
                  "wsl.exe -d Ubuntu -e tee logs/guardian_state.json"):
            self.assertDenied(self.agy(self.cmd(c)), "Ground Truth Protection")
            self.assertEqual(self.bash(c).get("__exit_code__"), 2, c)
        for c in ("wsl.exe -e tee .git/config", "wsl.exe -- bash -c 'echo x >> .git/config'",
                  "wsl.exe -e ls > .git/config"):
            self.assertDenied(self.agy(self.cmd(c)), self.LABEL)
        # The outer tokens keep every Bash rule (env channels, run-time values, unknown cwd) after the unwrap
        for c in ("PAGER=most wsl.exe -e git log -1", "GIT_CONFIG_PARAMETERS=x wsl.exe -e git status",
                  "cd \"$X\"; wsl.exe -e tee state.json", "LD_PRELOAD=/tmp/x.so wsl.exe -e ls",
                  "wsl.exe -e ls > $Y", "wsl.exe --cd /tmp -e tee state.json"):
            self.assertDenied(self.agy(self.cmd(c)))
        for c in ("wsl.exe -e ls logs", "wsl.exe -- git status", "wsl.exe -e cat .git/config"):
            self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)

    def test_both_channels_named_together(self):
        res = self.agy(self.cmd("echo x | tee .git/config logs/guardian_state.json"))
        self.assertDenied(res, "Ground Truth Protection")
        self.assertIn(self.LABEL, res["reason"])

    def test_file_tools_force_ask(self):
        for target in (".git/config", ".git/hooks/pre-commit", "~/.gitconfig", ".git/config.worktree",
                       os.path.join(self.root, ".git", "hooks", "post-checkout"), "C:\\repo\\.git\\config",
                       ".git/worktrees/wt/config", "/home/u/.config/git/config", ".git"):
            for name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
                res = self.agy({"toolCall": {"name": name, "args": {"TargetFile": target, "CodeContent": "x"}}})
                self.assertEqual(res.get("decision"), "force_ask", f"{name} {target}")
                self.assertIn("git config / hook file", res.get("reason", ""), target)
            for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"),
                              ("NotebookEdit", "notebook_path")):
                claude = self.run_guard({"tool_name": tool, "tool_input": {key: target, "content": "x"}})
                self.assertEqual(claude.get("hookSpecificOutput", {}).get("permissionDecision"), "ask",
                                 f"{tool} {target}: {claude}")
        for target in (".gitignore", ".gitattributes", ".github/workflows/x.yml", ".gitmodules", "docs/git.md",
                       ".git/info/exclude"):
            res = self.agy({"toolCall": {"name": "write_to_file", "args": {"TargetFile": target, "CodeContent": "x"}}})
            self.assertEqual(res.get("decision"), "ask", target)

    def test_path_matcher(self):
        match = pre_trade_guard._git_exec_config_path
        for p in (".git/config", ".git/config.worktree", ".git/hooks", ".git/hooks/", ".git/hooks/pre-commit",
                  ".git/worktrees/x/config.worktree", ".git/modules/x/config",
                  ".git/modules/a/modules/b/hooks/y", ".git", "foo/.git", "~/.gitconfig", "/x/.gitconfig",
                  "/x/.config/git/config", "/etc/gitconfig", "C:\\r\\.git\\config", ".git/hooks/../config",
                  "~/.config/git/c*", "{.git/config,x}"):
            self.assertTrue(match(p), p)
        for p in (".gitignore", ".gitattributes", ".gitmodules", ".github/workflows/x.yml", ".git/index",
                  ".git/info/exclude", ".git/info", ".git/info/attributes",
                  ".git/HEAD", "msg.txt", "+x", "config", "git/config", "*", "logs/x.json", ".github"):
            self.assertFalse(match(p), p)


class TestUnifiedBatchIssues(TestRiskAutoAllowResiduals):
    """Unified regression tests for batch issues #21, #59, #71, #104, #124, #154."""

    def test_issue_21_wsl_bash_wrapper_unwrapping(self):
        # 1. Read-only executor invocation wrapped in wsl and bash -lc is unwrapped and not denied as an opening
        ro_cmd = "wsl.exe -d Ubuntu -- bash -lc 'cd repo && python3 scripts/execute_futures_trade.py --positions --json'"
        res_ro = self.agy(self.cmd(ro_cmd))
        self.assertNotEqual(res_ro.get("decision"), "deny", res_ro)
        self.assertEqual(res_ro.get("decision"), "ask")

        # 2. Trade opening inside bash -lc '...' is unwrapped, classified as a trade opening, and denied without dossier
        open_cmd = f"bash -lc 'python3 {self.E} --symbol BTCUSDT --direction LONG --leverage 3'"
        res_open = self.agy(self.cmd(open_cmd))
        self.assertEqual(res_open.get("decision"), "deny", res_open)
        self.assertIn("Clean-Room Evaluator Required", res_open.get("reason", ""))

        # 3. git commit -m with harness file names is not denied
        for msg in ('git commit -m "update record_evaluation.py"',
                    'git commit -m "fix latest_dossier.json"',
                    'git commit --message="refactor scripts/hooks/pre_trade_guard.py"'):
            res_git = self.agy(self.cmd(msg))
            self.assertNotEqual(res_git.get("decision"), "deny", f"{msg}: {res_git}")

    def test_issue_59_report_issue_classification(self):
        # 1. report_issue.sh with an executor command in --repro argument is allowed / ask, not denied as a trade
        repro_cmd = ('./scripts/report_issue.sh --title "test bug" --category bug '
                     '--repro "python3 scripts/execute_futures_trade.py --direction LONG"')
        res = self.agy(self.cmd(repro_cmd))
        self.assertNotEqual(res.get("decision"), "deny", res)

        # 2. Compound command running report_issue.sh then executor is denied without dossier
        compound_cmd = ('./scripts/report_issue.sh --title "test"; '
                        f'python3 {self.E} --symbol BTCUSDT --direction LONG')
        res_compound = self.agy(self.cmd(compound_cmd))
        self.assertEqual(res_compound.get("decision"), "deny", res_compound)

        # 3. Command substitution inside report_issue argument is denied as active trade execution
        subst_cmd = ('./scripts/report_issue.sh --repro '
                     f'"$(python3 {self.E} --symbol BTCUSDT --direction LONG)"')
        res_subst = self.agy(self.cmd(subst_cmd))
        self.assertEqual(res_subst.get("decision"), "deny", res_subst)

    def test_issue_71_strict_numeric_leverage(self):
        # 1. Non-numeric leverage via shell substitution is denied with explicit leverage gate error
        c1 = f"python3 {self.E} --symbol BTCUSDT --direction LONG --leverage $(echo 50)"
        res1 = self.agy(self.cmd(c1))
        self.assertEqual(res1.get("decision"), "deny")
        self.assertIn("leverage must be a literal number", res1.get("reason", ""))

        # 2. Non-numeric leverage via shell variable is denied
        c2 = f'python3 {self.E} --symbol BTCUSDT --direction LONG --leverage "$X"'
        res2 = self.agy(self.cmd(c2))
        self.assertEqual(res2.get("decision"), "deny")
        self.assertIn("leverage must be a literal number", res2.get("reason", ""))

        # 3. Direct trade evaluation with invalid / float leverage in mcp_args
        for bad_lev in ("50.5", "$X", "invalid"):
            decision, reason = pre_trade_guard.evaluate_trade_opening(
                "", {}, {"symbol": "BTCUSDT", "direction": "LONG", "leverage": bad_lev},
                self.root, None
            )
            self.assertEqual(decision, "deny")
            self.assertIn("leverage must be a literal number", reason)

    def test_issue_104_confirmation_and_redirect_parsing(self):
        # 1. Output redirect > --confirmed is not parsed as --confirmed
        c_redir = f"python3 {self.E} --symbol BTCUSDT --direction LONG > --confirmed"
        self.assertFalse(pre_trade_guard.executor_confirmed(c_redir))

        # 2. Another subcommand's --confirmed is not attributed to the executor
        c_echo = f"echo --confirmed && python3 {self.E} --symbol BTCUSDT --direction LONG"
        self.assertFalse(pre_trade_guard.executor_confirmed(c_echo))

        # 3. Flag after redirect or comment is not parsed as executor flag
        c_comment = f"python3 {self.E} --symbol BTCUSDT --direction LONG # --confirmed"
        self.assertFalse(pre_trade_guard.executor_confirmed(c_comment))

    def test_issue_124_wsl_opt_val_and_offpath_reasons(self):
        # 1. --shell-type=login reports descriptive login shell reason
        c_login = f"wsl.exe -d Ubuntu --shell-type=login -- {self.DOCTOR}"
        self.assertAsk(c_login)
        res_login = self.agy(self.cmd(c_login))
        self.assertIn("login shell", res_login.get("reason", ""))

        # 2. --shell-type=standard is allowed
        c_std = f"wsl.exe -d Ubuntu --shell-type=standard -- {self.DOCTOR}"
        self.assertEqual(self.decisions(c_std), ("allow", "allow", "allow"))

        # 3. Off-path script reports descriptive unsanctioned repository script reason
        c_off = "python3 /tmp/elsewhere/scripts/execute_futures_trade.py --move-breakeven --symbol PEPEUSDT --is-yolo"
        res_off = self.agy(self.cmd(c_off))
        self.assertEqual(res_off.get("decision"), "ask")
        self.assertIn("Script path '/tmp/elsewhere/scripts/execute_futures_trade.py' is not the sanctioned repository script; user confirmation required.",
                      res_off.get("reason", ""))

        # 4. --fo abbreviation next to --move-breakeven asks, never auto-allowed
        c_fo = f"python3 {self.E} --move-breakeven --symbol BTCUSDT --fo"
        self.assertAsk(c_fo)

    def test_issue_154_linked_worktree_executor_path(self):
        # Create a hermetic linked worktree
        git_dir = os.path.join(self.root, ".git")
        os.makedirs(os.path.join(git_dir, "worktrees", "wt1"), exist_ok=True)
        wt_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(wt_tmp.cleanup)
        wt_root = os.path.realpath(wt_tmp.name)
        os.makedirs(os.path.join(wt_root, "scripts"), exist_ok=True)
        with open(os.path.join(git_dir, "worktrees", "wt1", "gitdir"), "w") as f:
            f.write(os.path.join(wt_root, ".git") + "\n")
        with open(os.path.join(git_dir, "worktrees", "wt1", "commondir"), "w") as f:
            f.write("../..\n")
        with open(os.path.join(wt_root, ".git"), "w") as f:
            f.write(f"gitdir: {os.path.join(git_dir, 'worktrees', 'wt1')}\n")

        # Issue #307 (replaces #154's parity): worktree copies are edited without a prompt, so running one is
        # unreviewed code: a plain ask like any unsanctioned script; the main checkout's executor keeps its auto-allow
        wt_script = os.path.join(wt_root, "scripts", "execute_futures_trade.py")
        c_wt = f"python3 {wt_script} --close-position --symbol BTCUSDT"
        res_wt = self.agy(self.cmd(c_wt))
        self.assertEqual(res_wt.get("decision"), "ask")
        self.assertIn("is not the sanctioned repository script", res_wt.get("reason", ""))
        self.assertIsNone(pre_trade_guard._sanctioned_script(wt_script, self.root, self.root, False))
        self.assertEqual(self.agy(self.cmd(f"python3 {self.E} --close-position --symbol BTCUSDT")).get("decision"),
                         "allow")


class TestReadOnlyAnalysisScripts(GuardHarness):
    """Issue #191.6: trade_outcomes.py / trading_scorecard.py / exit_policy_sim.py as one plain call are auto-allowed
    (read-only analysis script); chaining, redirects, unknown flags, paths outside logs/ and a write flag naming
    anything but the script's own output ask; ground-truth and evaluation-trail targets stay denied."""

    TO, SC, SIM = "scripts/trade_outcomes.py", "scripts/trading_scorecard.py", "scripts/exit_policy_sim.py"
    decisions = TestRiskAutoAllowResiduals.decisions

    def test_plain_runs_are_allowed_as_read_only(self):
        for c in (f"python3 {self.TO} --json", f"python3 {self.TO}",
                  f"python3 {self.TO} --since 2026-10-01 --env prod --no-klines --json --symbol BTCUSDT",
                  f"python3 {self.TO} --output logs/trade_outcomes.jsonl",
                  f"python3 {self.TO} --output=logs/trade_outcomes.jsonl",
                  f"python3 {self.TO} --output {os.path.join(self.root, 'logs', 'trade_outcomes.jsonl')}",
                  f"python3 {self.SC} --json --out logs/trading_scorecard.json --outcomes logs/old/outcomes.jsonl",
                  f"python3 {self.SC} --out logs/trading_scorecard.json",
                  f"python3 {self.SIM} --env prod --policies current,close_at_0_5r --horizon-hours 24 "
                  f"--taker-fee 0.0005 --trail-cadence 15m --exact-entry-only --json --out logs/exit_policy_sim.json",
                  f"python3 {os.path.join(self.root, self.TO)} --json", f"python3 -u {self.TO} --help",
                  f"env BINANCE_API_ENV=prod python3 {self.TO} --json",
                  f"wsl.exe -d Ubuntu -- python3 {self.SIM} --json",
                  f"wsl.exe -d Ubuntu -- python3 {self.TO} --output logs/trade_outcomes.jsonl"):
            self.assertEqual(self.decisions(c), ("allow", "allow", "allow"), c)
        reason = self.agy(self.cmd(f"python3 {self.TO} --json")).get("reason", "")
        self.assertIn("read-only analysis script", reason)

    def test_chaining_redirects_and_unknown_flags_ask(self):
        for c in (f"python3 {self.TO} --json && echo done", f"cd {self.root} && python3 {self.TO} --json",
                  f"python3 {self.TO} --json; ls", f"python3 {self.TO} --json | tee /tmp/x",
                  f"python3 {self.TO} --json > /tmp/out.json", f"python3 {self.TO} --json 2>&1",
                  f"python3 {self.TO} --out logs/x.jsonl",  # abbreviation of --output: not recognised
                  f"python3 {self.TO} --json extra", f"python3 {self.TO} --json=1", f"python3 {self.TO} --since",
                  f"PYTHONPATH=/tmp python3 {self.TO} --json", f"python3 -i {self.TO} --json",
                  f"sudo python3 {self.TO} --json", f"python3 /tmp/{self.TO} --json",
                  f"python3 {self.TO} --json && python3 scripts/trading_doctor.py --heal",
                  f"python3 scripts/trading_doctor.py --heal && python3 {self.TO} --output /tmp/x.jsonl"):
            for label, decision in zip(("agy", "bash", "powershell"), self.decisions(c)):
                self.assertEqual(decision, "ask", f"{label}: {c}")
        # Two own-output calls chained: each sub-command is exempt from the ground-truth denial, so the Bash line
        # asks (never an auto-allow); PowerShell's backstop exempts a single statement only, so it stays denied
        own = f"python3 {self.TO} --output logs/trade_outcomes.jsonl"
        c = f"{own} && {own}"
        self.assertEqual(self.decisions(c), ("ask", "ask", "deny"))

    def test_interpreter_must_be_a_bare_python_name(self):
        # An agent-made ./python3 or /tmp/python3 could forge the file: never exempt, never auto-allowed
        for interp in ("./python3", "/tmp/python3", "bin/python3", "/tmp/bin/python"):
            self.assertEqual(self.decisions(f"{interp} {self.TO} --output logs/trade_outcomes.jsonl"),
                             ("deny", "deny", "deny"), interp)
            for label, decision in zip(("agy", "bash", "powershell"), self.decisions(f"{interp} {self.TO} --json")):
                self.assertEqual(decision, "ask", f"{label}: {interp}")
        self.assertEqual(self.decisions(f"python {self.TO} --json"), ("allow", "allow", "allow"))
        self.assertEqual(self.decisions(f"python3.12 {self.TO} --json"), ("allow", "allow", "allow"))

    def test_paths_outside_logs_or_foreign_outputs_ask(self):
        os.symlink(tempfile.gettempdir(), os.path.join(self.root, "logs", "escape"))
        for c in (f"python3 {self.TO} --output logs/o/x.jsonl", f"python3 {self.SIM} --out logs/sim_b.json",
                  f"python3 {self.TO} --output /tmp/x.jsonl", f"python3 {self.TO} --output ../x.jsonl",
                  # own basename outside logs/: not a ground-truth path, so ask (not deny)
                  f"python3 {self.TO} --output /tmp/trade_outcomes.jsonl",
                  f"python3 {self.TO} --output logs/../x.jsonl", f"python3 {self.TO} --output logs/escape/x.jsonl",
                  f"python3 {self.TO} --output logs/trading_scorecard.json",
                  f"python3 {self.SC} --outcomes /tmp/forged.jsonl",
                  f"python3 {self.SIM} --out logs/guardian_actions.jsonl",
                  f"python3 {self.SIM} --outcomes ../x.jsonl --json"):
            for label, decision in zip(("agy", "bash", "powershell"), self.decisions(c)):
                self.assertEqual(decision, "ask", f"{label}: {c}")
        # The logs/ directory itself as the output: never allowed (PowerShell's logs-directory rule denies it)
        self.assertNotIn("allow", self.decisions(f"python3 {self.TO} --output logs"))

    def test_ground_truth_and_evaluation_targets_stay_denied(self):
        # Another script's ground-truth file (also read through --outcomes, or reached by a glob) is denied: only
        # its sole sanctioned writer may name it (issues #202 / #191)
        for c in (f"python3 {self.TO} --output logs/session_state.json",
                  f"python3 {self.SIM} --out logs/pending_entries.json",
                  f"python3 {self.SC} --out logs/evaluations/latest_dossier.json",
                  f"python3 {self.TO} --output 'logs/*.jsonl'",
                  f"python3 {self.TO} --output logs/trades_audit.jsonl",
                  f"python3 {self.TO} --output logs/primed_brief.json",
                  f"python3 {self.TO} --output logs/score_calibration.json",
                  # the scorecard writes the calibration store itself; --out naming it would overwrite the store
                  f"python3 {self.SC} --out logs/score_calibration.json",
                  f"python3 {self.SC} --out logs/trade_outcomes.jsonl",
                  f"python3 {self.SC} --outcomes logs/trade_outcomes.jsonl",
                  f"python3 {self.SIM} --out logs/score_calibration.json",
                  f"python3 {self.SIM} --out logs/trade_outcomes.jsonl.",
                  f"python3 scripts/trading_doctor.py --output logs/trade_outcomes.jsonl",
                  f"python3 {self.TO} --output logs/trade_outcomes.jsonl > logs/trades_audit.jsonl",
                  f"python3 {self.TO} --json && cp /tmp/x logs/trade_outcomes.jsonl"):
            self.assertEqual(self.decisions(c), ("deny", "deny", "deny"), c)

    def test_entry_policy_sim(self):
        # Issue #269: the entry simulator is a read-only analysis script with its own output and read-only
        # --outcomes / --shadow inputs inside logs/
        ent = "scripts/entry_policy_sim.py"
        for c in (f"python3 {ent} --exact-entry-only --json",
                  f"python3 {ent} --env prod --shadow logs/shadow_resolved.jsonl --policies current,orderflow_veto "
                  f"--horizon-hours 24 --taker-fee 0.0005 --maker-fee 0.0002 --trail-cadence 15m "
                  f"--confirm-minutes 45 --pullback-minutes 20 --exact-entry-only --json "
                  f"--out logs/entry_policy_sim.json",
                  f"python3 {ent} --outcomes logs/old/outcomes.jsonl --json"):
            self.assertEqual(self.decisions(c), ("allow", "allow", "allow"), c)
        self.assertIn("read-only analysis script", self.agy(self.cmd(f"python3 {ent} --json")).get("reason", ""))
        for c in (f"python3 {ent} --out logs/exit_policy_sim.json",  # another read-only script's output
                  f"python3 {self.SIM} --out logs/entry_policy_sim.json",
                  f"python3 {ent} --out logs/x.json", f"python3 {ent} --out /tmp/entry_policy_sim.json",
                  f"python3 {ent} --shadow /tmp/forged.jsonl", f"python3 {ent} --shadow ../x.jsonl --json",
                  f"python3 {ent} --json --unknown-flag", f"python3 {ent} --json && echo done"):
            for label, decision in zip(("agy", "bash", "powershell"), self.decisions(c)):
                self.assertEqual(decision, "ask", f"{label}: {c}")
        # The ground-truth outcomes file through --outcomes, and another writer's file through --out, stay denied
        for c in (f"python3 {ent} --outcomes logs/trade_outcomes.jsonl",
                  f"python3 {ent} --out logs/score_calibration.json"):
            self.assertEqual(self.decisions(c), ("deny", "deny", "deny"), c)


if __name__ == "__main__":
    unittest.main()
