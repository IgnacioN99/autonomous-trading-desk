"""
tests/test_ci_review_harness.py
Unit tests for the Deterministic PR Triage and Review Verification Gate.
Supports standard library unittest (zero extra dependencies) and pytest.
"""

import json
import unittest
import tempfile
from pathlib import Path
from scripts.ci.triage_pr import triage
from scripts.ci.verify_review import verify_review


class TestCIReviewHarness(unittest.TestCase):

    def test_triage_trading_execution_file(self):
        files = ["scripts/execute_futures_trade.py"]
        manifest = triage(files)
        self.assertFalse(manifest["fail_closed_triggered"])
        self.assertIn("trading_risk", manifest["required_reviewers"])
        self.assertIn("binance_microstructure", manifest["required_reviewers"])
        self.assertNotIn("prompt_engineering", manifest["required_reviewers"])

    def test_triage_prompt_file(self):
        files = ["docs/agent_prompt_engineering_guide.md"]
        manifest = triage(files)
        self.assertFalse(manifest["fail_closed_triggered"])
        self.assertIn("prompt_engineering", manifest["required_reviewers"])
        self.assertNotIn("trading_risk", manifest["required_reviewers"])

    def test_triage_fail_closed_on_core_rules(self):
        files = ["AGENTS.md"]
        manifest = triage(files)
        self.assertTrue(manifest["fail_closed_triggered"])
        self.assertEqual(len(manifest["required_reviewers"]), 4)
        self.assertEqual(
            set(manifest["required_reviewers"]),
            {
                "agentic_harness",
                "binance_microstructure",
                "prompt_engineering",
                "trading_risk",
            },
        )

    def test_triage_fail_closed_on_unclassified_file(self):
        files = ["scripts/unclassified_experimental_tool.py"]
        manifest = triage(files)
        self.assertTrue(manifest["fail_closed_triggered"])
        self.assertEqual(len(manifest["required_reviewers"]), 4)

    def test_verify_review_success(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            manifest = {
                "required_reviewers": ["trading_risk", "binance_microstructure"]
            }
            manifest_file = tmp_path / "manifest.json"
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

            report = """
# PR Audit Report
### Veredicto: trading_risk
- **Estado:** [APROBADO]
- **Resumen:** All risk limits and R:R ratios respected.

### Veredicto: binance_microstructure
- **Estado:** [APROBADO]
- **Resumen:** Precision stepSize and reduceOnly confirmed.
"""
            report_file = tmp_path / "report.md"
            report_file.write_text(report, encoding="utf-8")

            success, msg = verify_review(str(manifest_file), str(report_file))
            self.assertTrue(success)
            self.assertIn("APPROVED", msg)

    def test_verify_review_missing_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            manifest = {
                "required_reviewers": ["trading_risk", "prompt_engineering"]
            }
            manifest_file = tmp_path / "manifest.json"
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

            # Report only contains trading_risk, missing prompt_engineering
            report = """
### Veredicto: trading_risk
- **Estado:** [APROBADO]
"""
            report_file = tmp_path / "report.md"
            report_file.write_text(report, encoding="utf-8")

            success, msg = verify_review(str(manifest_file), str(report_file))
            self.assertFalse(success)
            self.assertIn("FATAL OMISSION DETECTED", msg)
            self.assertIn("prompt_engineering", msg)

    def test_verify_review_changes_requested(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            manifest = {
                "required_reviewers": ["trading_risk"]
            }
            manifest_file = tmp_path / "manifest.json"
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

            report = """
### Veredicto: trading_risk
- **Estado:** [CAMBIOS REQUERIDOS]
- **Resumen:** Stop Loss is too wide and violates $1.50 cap.
"""
            report_file = tmp_path / "report.md"
            report_file.write_text(report, encoding="utf-8")

            success, msg = verify_review(str(manifest_file), str(report_file))
            self.assertFalse(success)
            self.assertIn("CHANGES REQUESTED", msg)

    def test_hook_is_pr_creation_or_push(self):
        from scripts.hooks.post_pr_review_hook import is_pr_creation_or_push
        # True cases
        self.assertTrue(is_pr_creation_or_push("gh pr create --title 'feat'"))
        self.assertTrue(is_pr_creation_or_push("git push -u origin feat/multi-agent-pr-review"))
        self.assertTrue(is_pr_creation_or_push("git push origin fix/some-bug"))
        
        # False cases
        self.assertFalse(is_pr_creation_or_push("git push origin main"))
        self.assertFalse(is_pr_creation_or_push("git status"))
        self.assertFalse(is_pr_creation_or_push("python3 scripts/ci/run_pr_audit.py"))
        self.assertFalse(is_pr_creation_or_push(""))


if __name__ == "__main__":
    unittest.main()
