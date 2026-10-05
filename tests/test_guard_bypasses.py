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
import time
import datetime
import tempfile
import subprocess
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
for _p in (SCRIPTS_DIR, HOOKS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pre_trade_guard  # noqa: E402
import post_trade_sync  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402

GUARD_SCRIPT = os.path.join(HOOKS_DIR, "pre_trade_guard.py")
DEAD_PROXY = "http://127.0.0.1:9"
OFFLINE_ENV = {"HTTPS_PROXY": DEAD_PROXY, "HTTP_PROXY": DEAD_PROXY, "https_proxy": DEAD_PROXY,
               "http_proxy": DEAD_PROXY, "NO_PROXY": "", "no_proxy": ""}

EVALUATOR_CONV_ID = "0a1b2c3d-1111-4222-8333-444455556666"
PARENT_CONV_ID = "fedcba98-7777-4888-9999-000011112222"


def setUpModule():
    os.environ.update(OFFLINE_ENV)


class GuardHarness(unittest.TestCase):
    """Isolated workspace (temp dir) with profile, fresh session state and a fake Antigravity brain."""

    def setUp(self):
        self._env = patch.dict(os.environ, dict(OFFLINE_ENV), clear=False)
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

    def write_provenance_dossier(self, symbol="BTCUSDT", direction="LONG", parent=PARENT_CONV_ID):
        """Fake evaluator subagent transcript + dossier recorded exactly like --from-subagent does."""
        conv_dir = os.path.join(self.brain, EVALUATOR_CONV_ID, ".system_generated", "logs")
        os.makedirs(conv_dir, exist_ok=True)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        block = json.dumps({"status": "APPROVED", "summary": "test",
                            "approved_candidates": [{"symbol": symbol, "direction": direction, "tier": "Tier S",
                                                     "leverage": 3}]})
        steps = [
            {"source": "SYSTEM", "type": "USER_INPUT", "content": f"Subagent invoked sender={parent}", "step_index": 0},
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "step_index": 1, "created_at": created,
             "content": f"Master Dossier\n<dossier_json>\n{block}\n</dossier_json>"},
        ]
        transcript = os.path.join(conv_dir, "transcript.jsonl")
        with open(transcript, "w", encoding="utf-8") as f:
            for s in steps:
                f.write(json.dumps(s) + "\n")
        record = dp.build_record_from_extraction(dp.extract_dossier_from_transcript(transcript))
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
             "python3 scripts/trading_doctor.py --auto-heal")
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
        self.assertEqual(self.agy(self.cmd("rm -rf build && python3 scripts/trading_doctor.py --auto-heal")).get("decision"),
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

    def test_harness_files_force_ask_and_others_ask(self):
        self.assertEqual(self.write("scripts/hooks/pre_trade_guard.py").get("decision"), "force_ask")
        self.assertEqual(self.write(".agents/hooks.json").get("decision"), "force_ask")
        self.assertEqual(self.write("scripts/utils/dossier_provenance.py").get("decision"), "force_ask")
        self.assertEqual(self.write("docs/notes.md").get("decision"), "ask")
        self.assertEqual(self.write("scratch/t.py", "send_signed_request('POST', '/fapi/v1/order')").get("decision"),
                         "force_ask")


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
                self.assertTrue(res["synced"], c)
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


if __name__ == "__main__":
    unittest.main()
