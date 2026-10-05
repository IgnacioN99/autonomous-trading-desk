#!/usr/bin/env python3
"""
test_foundation_issue_9.py - Comprehensive Verification for Issue #9 Foundation Layer:
1. Centralized Environment Resolution (Finding 3)
2. Dependency verification in requirements.txt (Finding 11)
3. Safe default in prod.env.example (Finding 19)
4. Safe Issue Reporting & Sanitization (Finding 18)
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from utils.env_resolver import (
    resolve_env,
    is_prod_environment,
    get_env_config,
    parse_env_file,
    find_workspace_root
)
import report_agent_issue


class TestCentralizedEnvResolver(unittest.TestCase):
    """[Finding 3] Centralized Environment Resolution Verification."""

    def setUp(self):
        self.orig_env = os.environ.get("BINANCE_API_ENV")
        if "BINANCE_API_ENV" in os.environ:
            del os.environ["BINANCE_API_ENV"]

    def tearDown(self):
        if self.orig_env is not None:
            os.environ["BINANCE_API_ENV"] = self.orig_env
        elif "BINANCE_API_ENV" in os.environ:
            del os.environ["BINANCE_API_ENV"]

    def test_explicit_env_normalization_prod(self):
        for val in ["prod", "PROD", "production", "Production", "mainnet", "MAINNET"]:
            self.assertEqual(resolve_env(val), "prod")
            self.assertTrue(is_prod_environment(val))

    def test_explicit_env_normalization_testnet(self):
        for val in ["testnet", "TESTNET", "Testnet"]:
            self.assertEqual(resolve_env(val), "testnet")
            self.assertFalse(is_prod_environment(val))

    def test_explicit_env_invalid_fail_closed(self):
        for invalid in ["dev", "staging", "invalid", "sandbox", ""]:
            with self.assertRaises(ValueError):
                resolve_env(invalid)

    def test_os_environ_resolution(self):
        os.environ["BINANCE_API_ENV"] = "PROD"
        self.assertEqual(resolve_env(), "prod")
        self.assertTrue(is_prod_environment())

        os.environ["BINANCE_API_ENV"] = "testnet"
        self.assertEqual(resolve_env(), "testnet")
        self.assertFalse(is_prod_environment())

        os.environ["BINANCE_API_ENV"] = "unknown_env"
        with self.assertRaises(ValueError):
            resolve_env()

    @patch("utils.env_resolver.find_workspace_root")
    @patch("utils.env_resolver.parse_env_file")
    @patch("os.path.exists")
    def test_root_env_file_resolution(self, mock_exists, mock_parse, mock_root):
        mock_root.return_value = "/mock/workspace"

        def fake_exists(path):
            return path == "/mock/workspace/.env"
        mock_exists.side_effect = fake_exists

        def fake_parse(path):
            if path == "/mock/workspace/.env":
                return {"BINANCE_API_ENV": "production"}
            return {}
        mock_parse.side_effect = fake_parse

        self.assertEqual(resolve_env(), "prod")

    @patch("utils.env_resolver.find_workspace_root")
    @patch("utils.env_resolver.parse_env_file")
    @patch("os.path.exists")
    def test_prod_env_fallback(self, mock_exists, mock_parse, mock_root):
        mock_root.return_value = "/mock/workspace"

        def fake_exists(path):
            return path == "/mock/workspace/config/environments/prod.env"
        mock_exists.side_effect = fake_exists

        def fake_parse(path):
            if path == "/mock/workspace/config/environments/prod.env":
                return {"BINANCE_API_ENV": "PROD"}
            return {}
        mock_parse.side_effect = fake_parse

        self.assertEqual(resolve_env(), "prod")

    @patch("utils.env_resolver.find_workspace_root")
    @patch("utils.env_resolver.parse_env_file")
    @patch("os.path.exists")
    def test_safe_mode_default_testnet(self, mock_exists, mock_parse, mock_root):
        mock_root.return_value = "/mock/workspace"
        mock_exists.return_value = False
        mock_parse.return_value = {}
        # When nothing is present, must default to 'testnet' in safe mode
        self.assertEqual(resolve_env(), "testnet")

    def test_get_env_config_keys(self):
        cfg = get_env_config("testnet")
        self.assertIsInstance(cfg, dict)
        self.assertEqual(cfg.get("BINANCE_API_ENV"), "TESTNET")


class TestRequirementsDependencies(unittest.TestCase):
    """[Finding 11] Missing Dependencies in requirements.txt."""

    def test_mcp_and_pytest_in_requirements(self):
        req_path = os.path.join(BASE_DIR, "requirements.txt")
        self.assertTrue(os.path.exists(req_path))
        with open(req_path, "r", encoding="utf-8") as f:
            content = f.read()

        # radar_mcp_server.py imports mcp.server.mcpserver (2.x SDK only)
        self.assertIn("mcp>=2.0,<3", content)
        self.assertIn("pytest>=7.0.0", content)


class TestProdEnvExampleSafeDefaults(unittest.TestCase):
    """[Finding 19] Safe default in config/environments/prod.env.example."""

    def test_live_trading_armed_false_default(self):
        example_path = os.path.join(BASE_DIR, "config", "environments", "prod.env.example")
        self.assertTrue(os.path.exists(example_path))
        with open(example_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("LIVE_TRADING_ARMED=false", content)
        self.assertNotIn("LIVE_TRADING_ARMED=true", content)


class TestSafeIssueReporting(unittest.TestCase):
    """[Finding 18] Safe Issue Reporting & Telemetry Sanitization."""

    def test_no_hardcoded_default_repo(self):
        self.assertIsNone(report_agent_issue.DEFAULT_REPO)

    def test_derive_github_repo_from_env(self):
        with patch.dict(os.environ, {"GITHUB_REPO": "custom-org/custom-repo"}):
            self.assertEqual(report_agent_issue.derive_github_repo(), "custom-org/custom-repo")

    def test_derive_github_repo_from_git_remote(self):
        with patch.dict(os.environ, {}, clear=True):
            if "GITHUB_REPO" in os.environ:
                del os.environ["GITHUB_REPO"]
            with patch("subprocess.run") as mock_sub:
                mock_sub.return_value = MagicMock(
                    returncode=0,
                    stdout="git@github.com:my-org/my-desk.git\n"
                )
                self.assertEqual(report_agent_issue.derive_github_repo(), "my-org/my-desk")

    def test_derive_github_repo_non_github_failsafe(self):
        with patch.dict(os.environ, {}, clear=True):
            if "GITHUB_REPO" in os.environ:
                del os.environ["GITHUB_REPO"]
            with patch("subprocess.run") as mock_sub:
                mock_sub.return_value = MagicMock(
                    returncode=0,
                    stdout="git@gitlab.com:my-org/my-desk.git\n"
                )
                self.assertIsNone(report_agent_issue.derive_github_repo())

    def test_sanitize_telemetry_masks_sensitive_tokens(self):
        raw = (
            "Failure with ghp_1234567890abcdef1234567890abcdef and "
            "secret_notionTokenAbc1234567890 and Bearer eyJhbGciOiJIUzI1NiJ9.test.sig "
            "and api_key='WcN1JJd99lR9cQagSBLPgWvaneXxateXouLqndue6MGK3ga4faIOieMQwomIHP8S'"
        )
        cleaned = report_agent_issue.sanitize_telemetry(raw)
        self.assertNotIn("ghp_1234567890abcdef1234567890abcdef", cleaned)
        self.assertNotIn("secret_notionTokenAbc1234567890", cleaned)
        self.assertNotIn("WcN1JJd99lR9cQagSBLPgWvaneXxateXouLqndue6MGK3ga4faIOieMQwomIHP8S", cleaned)
        self.assertIn("[REDACTED_GH_TOKEN]", cleaned)
        self.assertIn("[REDACTED_NOTION_TOKEN]", cleaned)
        self.assertIn("api_key=[REDACTED]", cleaned)

    def test_sanitize_telemetry_masks_exact_balances(self):
        raw = "User has $15420.50 USDT available in balance. Wallet: {\"totalWalletBalance\": \"15420.50\", \"unrealizedProfit\": \"250.25\"}"
        cleaned = report_agent_issue.sanitize_telemetry(raw)
        self.assertNotIn("15420.50", cleaned)
        self.assertNotIn("250.25", cleaned)
        self.assertIn("[REDACTED", cleaned)

    def test_safe_queuing_when_no_repo_or_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backlog = os.path.join(tmpdir, "issues_backlog.jsonl")
            fps = os.path.join(tmpdir, "issues_fingerprints.json")
            with patch("report_agent_issue.BACKLOG_FILE", backlog), \
                 patch("report_agent_issue.FINGERPRINTS_FILE", fps), \
                 patch("report_agent_issue.derive_github_repo", return_value=None), \
                 patch.dict(os.environ, {}, clear=True):

                res = report_agent_issue.report_issue(
                    title="Mock Anomaly",
                    error_detail="Something failed with balance $500 USDT",
                    category="agent_failure",
                    severity="HIGH"
                )

                self.assertEqual(res["status"], "QUEUED_OFFLINE")
                self.assertEqual(res["repo"], "local_backlog")
                self.assertTrue(os.path.exists(backlog))
                with open(backlog, "r", encoding="utf-8") as f:
                    entry = json.loads(f.readline())
                    self.assertEqual(entry["title"], "Mock Anomaly")
                    self.assertNotIn("$500 USDT", entry["body"])

    def test_report_issue_sh_script_sanitization_and_queuing(self):
        import subprocess
        script_path = os.path.join(SCRIPTS_DIR, "report_issue.sh")
        self.assertTrue(os.path.exists(script_path))

        # Run bash script with sensitive tokens and balances
        env = os.environ.copy()
        env["GITHUB_TOKEN"] = ""
        env["GITHUB_REPO"] = ""
        res = subprocess.run(
            [
                "bash",
                script_path,
                "--title", "Test Anomaly ghp_abcdef1234567890abcdef",
                "--error", "Failed with balance $9999.00 USDT and secret_token1234567890",
                "--severity", "HIGH"
            ],
            capture_output=True,
            text=True,
            env=env
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Issue saved in local backlog", res.stdout)

        # Check backlog entry
        backlog_file = os.path.join(BASE_DIR, "logs", "issues_backlog.jsonl")
        self.assertTrue(os.path.exists(backlog_file))
        with open(backlog_file, "r", encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
            last_line = lines[-1]
            last_item = json.loads(last_line)
            self.assertNotIn("ghp_abcdef1234567890abcdef", last_item["title"])
            self.assertNotIn("secret_token1234567890", last_item["body"])
            self.assertNotIn("$9999.00 USDT", last_item["body"])


class TestScriptsEnvResolverIntegration(unittest.TestCase):
    """Verify all 5 target scripts correctly import and integrate with resolve_env."""

    def test_trading_doctor_uses_resolve_env(self):
        import trading_doctor
        self.assertTrue(hasattr(trading_doctor, "resolve_env"))
        with patch("trading_doctor.resolve_env", return_value="testnet") as mock_resolve, \
             patch("execute_futures_trade.load_env", return_value={}), \
             patch("execute_futures_trade.get_client_config", return_value=("key", "sec", "http://")):
            trading_doctor.run_doctor(target_env="testnet")
            mock_resolve.assert_called_with("testnet")

    def test_post_trade_sync_uses_resolve_env(self):
        import post_trade_sync
        self.assertTrue(os.path.exists(os.path.join(SCRIPTS_DIR, "post_trade_sync.py")))
        self.assertTrue(os.path.exists(os.path.join(SCRIPTS_DIR, "hooks", "post_trade_sync.py")))

    def test_night_cutoff_loop_uses_resolve_env(self):
        sys.path.insert(0, os.path.join(SCRIPTS_DIR, "loops"))
        import night_cutoff_loop
        self.assertTrue(hasattr(night_cutoff_loop, "resolve_env"))
        with patch("night_cutoff_loop.resolve_env", return_value="testnet") as mock_resolve, \
             patch("execute_futures_trade.send_signed_request", return_value=[]):
            night_cutoff_loop.run_night_cutoff(target_env="testnet")
            mock_resolve.assert_called_with("testnet")

    def test_trading_drift_watchdog_uses_resolve_env(self):
        import trading_drift_watchdog
        self.assertTrue(hasattr(trading_drift_watchdog, "resolve_env"))
        with patch("trading_drift_watchdog.resolve_env", return_value="testnet") as mock_resolve, \
             patch("execute_futures_trade.send_signed_request", return_value=[]):
            trading_drift_watchdog.audit_dead_alpha(target_env="testnet")
            mock_resolve.assert_called_with("testnet")

    def test_sync_notion_journal_uses_resolve_env(self):
        import sync_notion_journal
        self.assertTrue(hasattr(sync_notion_journal, "resolve_env"))
        with patch("sync_notion_journal.resolve_env", return_value="testnet") as mock_resolve, \
             patch("execute_futures_trade.send_signed_request", return_value=[]):
            sync_notion_journal.get_binance_trade_history(target_env="testnet")
            mock_resolve.assert_called_with("testnet")


if __name__ == "__main__":
    unittest.main()
