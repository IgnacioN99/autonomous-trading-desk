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

    def write_provenance_dossier(self, symbol="BTCUSDT", direction="LONG", parent=PARENT_CONV_ID, extra=None):
        """Fake evaluator subagent transcript + dossier recorded exactly like --from-subagent does.
        `extra` overrides/adds candidate fields (tier, is_yolo, requires_user_confirmation, ...)."""
        conv_dir = os.path.join(self.brain, EVALUATOR_CONV_ID, ".system_generated", "logs")
        os.makedirs(conv_dir, exist_ok=True)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cand = {"symbol": symbol, "direction": direction, "tier": "Tier S", "leverage": 3}
        cand.update(extra or {})
        block = json.dumps({"status": "APPROVED", "summary": "test", "approved_candidates": [cand]})
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

    def test_truncated_transcript_resolved_from_full_transcript_allows_deploy(self):
        """agy truncates the send_message in transcript.jsonl; the guard re-derives it from transcript_full.jsonl."""
        conv_dir = os.path.join(self.brain, EVALUATOR_CONV_ID, ".system_generated", "logs")
        os.makedirs(conv_dir, exist_ok=True)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        block = json.dumps({"status": "APPROVED", "target_env": "PROD", "summary": "test",
                            "approved_candidates": [{"symbol": "BTCUSDT", "direction": "LONG", "tier": "Tier S",
                                                     "leverage": 3}]})
        message = "Master Dossier — régimen σ\n" * 30 + f"<dossier_json>\n{block}\n</dossier_json>"
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
        record = dp.build_record_from_extraction(dp.extract_dossier_from_transcript(transcript))
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
    }
    NEW_FILES = ("guardian_state.json", "pending_entries.json")

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
        self.assertEqual(self.agy(self.cmd("python3 scripts/loops/position_guardian_loop.py --once >> logs/guardian.log 2>&1"))
                         .get("decision"), "allow")
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
        lines = pre_trade_guard._strip_interpreter_heredocs("perl <<'EOF' # python\nunlink x;\nEOF").split("\n")
        self.assertIn("unlink x;", lines)
        self.assertNotIn("x = 1", pre_trade_guard._strip_interpreter_heredocs("python3 - <<'EOF'\nx = 1\nEOF").split("\n"))

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
