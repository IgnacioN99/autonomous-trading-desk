#!/usr/bin/env python3
"""
test_issue_telemetry.py - scripts/utils/issue_telemetry.py (issue #25): severity/priority normalization,
label metadata, ledger telemetry against the real session_state.json schema (sign-only PnL, no amounts),
and the shared six-section issue body.
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for path in (SCRIPTS_DIR, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils import issue_telemetry as it  # noqa: E402
from test_report_issue import (  # noqa: E402
    session_state, NOTIONAL_LONG, NOTIONAL_SHORT, MARGIN_LONG, MARGIN_SHORT, FLOATING_PNL, REALIZED_PNL,
    SECTION_HEADINGS,
)


class TestSeverityPriority(unittest.TestCase):

    def test_normalize_severity(self):
        for raw, want in (("HIGH", "HIGH"), ("high", "HIGH"), (" Critical ", "CRITICAL"), ("low", "LOW"),
                          ("Medium", "MEDIUM")):
            self.assertEqual(it.normalize_severity(raw), want)
        for bad in (None, "", "urgent", "P1", "HIGHEST"):
            with self.assertRaises(ValueError):
                it.normalize_severity(bad)

    def test_default_priority_mapping(self):
        self.assertEqual(it.DEFAULT_PRIORITY, {"CRITICAL": "P0", "HIGH": "P1", "MEDIUM": "P2", "LOW": "P3"})
        for sev, prio in it.DEFAULT_PRIORITY.items():
            self.assertEqual(it.normalize_priority(None, sev), prio)
            self.assertEqual(it.normalize_priority("", sev.lower()), prio)

    def test_normalize_priority_override_and_invalid(self):
        self.assertEqual(it.normalize_priority("p0", "LOW"), "P0")
        self.assertEqual(it.normalize_priority(" P3 ", "CRITICAL"), "P3")
        for bad in ("P4", "high", "1", "PP1"):
            with self.assertRaises(ValueError):
                it.normalize_priority(bad, "HIGH")
        with self.assertRaises(ValueError):
            it.normalize_priority(None, "bogus")

    def test_labels_and_specs(self):
        self.assertEqual(it.build_labels("high", None, "tool_error"),
                         ["agent-failure", "severity:high", "priority:P1", "cat:tool_error"])
        self.assertEqual(it.build_labels("MEDIUM", "p0", ""), ["agent-failure", "severity:medium", "priority:P0"])
        self.assertEqual(it.label_spec("severity:critical")[0], "b60205")
        self.assertEqual(it.label_spec("priority:P3"), ("c5def5", "Backlog: when convenient"))
        self.assertEqual(it.label_spec("cat:infra"), ("ededed", ""))
        self.assertEqual(it.title_prefix_from_labels(["agent-failure", "severity:high", "priority:P1"]), "[HIGH/P1] ")
        self.assertEqual(it.title_prefix_from_labels(["severity:low"]), "[LOW] ")
        self.assertEqual(it.title_prefix_from_labels(["agent-failure"]), "")
        self.assertEqual(it.required_labels(["agent-failure", "severity:low", "priority:P3", "cat:x"]),
                         ["severity:low", "priority:P3"])


class TestCategoryAndLegacyLabels(unittest.TestCase):

    def test_normalize_category(self):
        for raw, want in (("tool_error", "tool_error"), ("INFRA", "infra"), (" Risk_Gate ", "risk_gate"),
                          ("quant-logic2", "quant-logic2")):
            self.assertEqual(it.normalize_category(raw), want)
        self.assertEqual(it.normalize_category(""), "")
        for bad in ("tool error", "cat;rm", "a/b", "ünicode", "`x`"):
            with self.assertRaises(ValueError):
                it.normalize_category(bad)
        with self.assertRaises(ValueError):
            it.normalize_category("", required=True)

    def test_with_default_priority(self):
        self.assertEqual(it.with_default_priority(["agent-failure", "severity:medium", "cat:infra"]),
                         ["agent-failure", "severity:medium", "priority:P2", "cat:infra"])
        self.assertEqual(it.with_default_priority(["severity:critical"]), ["severity:critical", "priority:P0"])
        kept = ["agent-failure", "severity:low", "priority:P0"]
        self.assertEqual(it.with_default_priority(kept), kept)
        self.assertEqual(it.with_default_priority(["agent-failure"]), ["agent-failure"])
        self.assertEqual(it.with_default_priority(["severity:bogus"]), ["severity:bogus"])
        self.assertEqual(it.with_default_priority(None), [])


class TestCollectTelemetry(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write_state(self, state):
        with open(os.path.join(self.logs, "session_state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)

    def test_real_schema_keys_and_sign_only_pnl(self):
        self.write_state(session_state())
        with patch.dict(os.environ, {"BINANCE_API_ENV": "prod"}):
            t = it.collect_telemetry(BASE_DIR, self.logs)
        self.assertEqual(t["target_env"], "PROD")
        self.assertEqual(t["ledger"], "session_state.json")
        self.assertIs(t["is_valid"], True)
        self.assertEqual(t["delta_bias"], "LONG_HEAVY")
        self.assertEqual(t["positions_count"], 2)
        self.assertEqual(t["positions"], "BTCUSDT LONG, SOLUSDT SHORT")
        self.assertEqual(t["sl_verified"], "1/2")
        self.assertEqual(t["floating_pnl_sign"], "NEGATIVE")
        self.assertEqual(t["session_pnl_sign"], "POSITIVE")
        self.assertEqual(t["closed_trades"], 3)
        self.assertGreaterEqual(t["state_age_s"], 30)
        self.assertLess(t["state_age_s"], 600)
        self.assertNotIn("hostname", t)
        self.assertIn("python_version", t)
        self.assertIn("platform", t)
        dumped = json.dumps(t) + it.render_telemetry_markdown(t)
        for amount in (NOTIONAL_LONG, NOTIONAL_SHORT, MARGIN_LONG, MARGIN_SHORT, FLOATING_PNL, REALIZED_PNL,
                       98765.43, 3087.31):
            self.assertNotIn(str(abs(amount)), dumped)

    def test_flat_and_error_state(self):
        state = session_state()
        state.update(is_valid=False, active_positions=[])
        state["portfolio_exposure"].update(delta_bias="UNKNOWN", total_floating_pnl_usdt=0.0)
        state["closed_today_summary"].update(net_realized_pnl_usdt=-3.5, closed_trades_count=1)
        self.write_state(state)
        t = it.collect_telemetry(BASE_DIR, self.logs)
        self.assertIs(t["is_valid"], False)
        self.assertEqual(t["positions"], "none")
        self.assertEqual(t["positions_count"], 0)
        self.assertEqual(t["sl_verified"], "0/0")
        self.assertEqual(t["floating_pnl_sign"], "FLAT")
        self.assertEqual(t["session_pnl_sign"], "NEGATIVE")

    def test_missing_state_file(self):
        t = it.collect_telemetry(BASE_DIR, self.logs)
        self.assertEqual(t["ledger"], "no session_state.json")
        for key in it.LEDGER_KEYS:
            self.assertEqual(t[key], "unavailable", key)

    def test_unreadable_state_file(self):
        with open(os.path.join(self.logs, "session_state.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        t = it.collect_telemetry(BASE_DIR, self.logs)
        self.assertEqual(t["ledger"], "unreadable session_state.json")
        self.assertEqual(t["delta_bias"], "unavailable")

    def test_git_failure_and_bad_env_never_raise(self):
        with patch("utils.issue_telemetry.subprocess.run", side_effect=OSError("no git")), \
             patch.dict(os.environ, {"BINANCE_API_ENV": "bogus"}):
            t = it.collect_telemetry(os.path.join(self.logs, "does-not-exist"), self.logs)
        self.assertEqual(t["commit"], "unavailable")
        self.assertEqual(t["branch"], "unavailable")
        self.assertEqual(t["dirty"], "unavailable")
        self.assertEqual(t["target_env"], "BOGUS")  # resolve_env raised -> raw env fallback
        for key, _ in it.TELEMETRY_FIELDS:
            self.assertIn(key, t)

    def test_git_values_parsed(self):
        class R:
            def __init__(self, out):
                self.returncode, self.stdout = 0, out
        outputs = {"--short": "abc1234\n", "--abbrev-ref": "fix/x\n", "--porcelain": " M a.py\n"}

        def fake_run(cmd, **kw):
            return R(next(v for k, v in outputs.items() if k in cmd))
        with patch("utils.issue_telemetry.subprocess.run", side_effect=fake_run):
            t = it.collect_telemetry(BASE_DIR, self.logs)
        self.assertEqual((t["commit"], t["branch"], t["dirty"]), ("abc1234", "fix/x", True))
        self.assertEqual(it.git_summary(t), "`abc1234` on `fix/x` (dirty: true)")

    def test_cli_json_and_markdown(self):
        self.write_state(session_state())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(it.main(["--json", "--logs-dir", self.logs]), 0)
        self.assertEqual(json.loads(out.getvalue())["delta_bias"], "LONG_HEAVY")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(it.main(["--markdown", "--logs-dir", self.logs]), 0)
        self.assertIn("| **Portfolio delta bias** | `LONG_HEAVY` |", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(it.main(["--bogus-flag"]), 0)


class TestRenderIssueBody(unittest.TestCase):

    def telemetry(self):
        return {"target_env": "TESTNET", "commit": "abc1234", "branch": "main", "dirty": False}

    def test_six_headings_in_order_and_priority_row(self):
        body = it.render_issue_body(error_detail="boom", severity="high", category="tool_error",
                                    agent_name="a", telemetry=self.telemetry(), fingerprint="fp123")
        self.assertEqual(list(it.SECTION_HEADINGS), SECTION_HEADINGS)
        positions = [body.find(h) for h in it.SECTION_HEADINGS]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))
        self.assertIn("| **Severity** | **🟠 HIGH** |\n| **Priority** | **🟠 P1** |", body)
        self.assertIn("| **Git** | `abc1234` on `main` (dirty: false) |", body)
        self.assertIn("| **Fingerprint ID** | `fp123` |", body)
        self.assertIn(it.CATEGORY_IMPACT["tool_error"], body)
        self.assertIn(f"- [ ] {it.DEFAULT_ACCEPTANCE}", body)
        self.assertIn("**Reproduction command:** not provided", body)

    def test_structured_fields_and_sanitizer(self):
        body = it.render_issue_body(
            error_detail="err SECRET", severity="LOW", priority="p2", category="quant_logic",
            telemetry=self.telemetry(), repro="python3 x.py (exit 1)", output_text="tail SECRET\n",
            stack_trace="Traceback SECRET", affected_files="a.py:1-5, b.py:9\nc.py",
            root_cause="rc", context="ctx", impact="", remediation="fix it",
            acceptance_criteria="- [ ] first; second\nthird",
            sanitize=lambda s: s.replace("SECRET", "[REDACTED]"))
        self.assertNotIn("SECRET", body)
        self.assertIn("| **Priority** | **🟡 P2** |", body)
        self.assertIn("```bash\npython3 x.py (exit 1)\n```", body)
        self.assertIn("<details><summary>Raw output (tail)</summary>", body)
        self.assertIn("```text\ntail [REDACTED]\n```", body)
        for f in ("a.py:1-5", "b.py:9", "c.py"):
            self.assertIn(f"- `{f}`", body)
        for item in ("first", "second", "third"):
            self.assertIn(f"- [ ] {item}\n", body + "\n")
        self.assertIn(it.GENERIC_IMPACT, body)
        self.assertIn("**Remediation:** fix it", body)

    def test_category_agent_and_telemetry_are_sanitized(self):
        t = dict(self.telemetry(), branch="fix/SECRET", delta_bias="SECRET_BIAS")
        body = it.render_issue_body(error_detail="e", severity="HIGH", category="cat_SECRET",
                                    agent_name="agent SECRET", telemetry=t,
                                    sanitize=lambda s: s.replace("SECRET", "[REDACTED]"))
        self.assertNotIn("SECRET", body)
        self.assertIn("`fix/[REDACTED]`", body)
        self.assertIn("`cat_[REDACTED]`", body)
        self.assertIn("`agent [REDACTED]`", body)

    def test_invalid_severity_raises(self):
        with self.assertRaises(ValueError):
            it.render_issue_body(error_detail="e", severity="urgent")

    def test_read_tail_and_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("".join(f"line {i}\n" for i in range(500)))
            tail = it.read_tail(path)
            self.assertTrue(tail.startswith("line 300\n"))
            self.assertTrue(tail.endswith("line 499\n"))
            self.assertLessEqual(len(it.read_tail(path, max_chars=50)), 50)
            self.assertEqual(len(it.read_capped(path, 100)), 100)
            self.assertIn("could not read", it.read_tail(os.path.join(tmp, "missing.txt")))


class TestIssueFormsAndTriage(unittest.TestCase):
    """Textual checks (PyYAML is not a desk dependency)."""

    def read(self, *parts):
        with open(os.path.join(BASE_DIR, ".github", *parts), encoding="utf-8") as f:
            return f.read()

    def test_blank_issues_disabled(self):
        self.assertIn("blank_issues_enabled: false", self.read("ISSUE_TEMPLATE", "config.yml"))

    def test_forms_require_severity_and_priority(self):
        for form in ("harness_failure.yml", "enhancement.yml"):
            text = self.read("ISSUE_TEMPLATE", form)
            for dropdown in ("id: severity", "id: priority"):
                before, after = text.split(dropdown, 1)
                self.assertEqual(before.rsplit("- type:", 1)[1].strip(), "dropdown", (form, dropdown))
                self.assertIn("required: true", after.split("- type:", 1)[0], (form, dropdown))
            for sev in it.SEVERITIES:
                self.assertIn(f'"{sev} - ', text)
            for prio in it.PRIORITIES:
                self.assertIn(f'"{prio} - ', text)
        harness = self.read("ISSUE_TEMPLATE", "harness_failure.yml")
        self.assertIn('labels: ["agent-failure"]', harness)
        self.assertIn("report_issue.sh", harness)
        for field in ("id: summary", "id: reproduction", "id: error_output", "id: affected_files",
                      "id: root_cause", "id: impact", "id: acceptance_criteria"):
            self.assertIn(field, harness)
        self.assertIn('labels: ["enhancement"]', self.read("ISSUE_TEMPLATE", "enhancement.yml"))

    def test_triage_workflow_flags_missing_labels(self):
        text = self.read("workflows", "issue-triage.yml")
        self.assertIn("types: [opened, reopened, edited, labeled, unlabeled]", text)
        self.assertIn("issues: write", text)
        self.assertIn("concurrency:", text)
        self.assertIn("group: issue-triage-${{ github.event.issue.number }}", text)
        self.assertIn("cancel-in-progress: true", text)
        self.assertIn("GH_TOKEN: ${{ github.token }}", text)
        self.assertIn("severity:*", text)
        self.assertIn("priority:*", text)
        self.assertIn("gh label create needs-triage", text)
        self.assertIn("--add-label needs-triage", text)
        self.assertIn("--remove-label needs-triage", text)


if __name__ == "__main__":
    unittest.main()
