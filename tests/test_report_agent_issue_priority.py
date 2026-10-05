#!/usr/bin/env python3
"""
test_report_agent_issue_priority.py - scripts/report_agent_issue.py (issue #25): mandatory severity + priority
labels, six-section body, CLI validation, and the label fallbacks of dispatch_github_issue (no network:
urllib and gh are patched).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _path in (SCRIPTS_DIR, TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import report_agent_issue  # noqa: E402
from utils import issue_telemetry  # noqa: E402


def fake_response(data):
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class _BacklogCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backlog = os.path.join(self.tmp.name, "issues_backlog.jsonl")
        self.patches = [
            patch("report_agent_issue.BACKLOG_FILE", self.backlog),
            patch("report_agent_issue.FINGERPRINTS_FILE", os.path.join(self.tmp.name, "fps.json")),
            patch("report_agent_issue.derive_github_repo", return_value=None),
            patch.dict(os.environ, {}, clear=True),
            # Never read the real repo's logs/ or git from these tests
            patch("report_agent_issue.LOGS_DIR", self.tmp.name),
            patch("utils.issue_telemetry._git", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def last_entry(self):
        with open(self.backlog, encoding="utf-8") as f:
            return json.loads(f.readlines()[-1])


class TestReportIssuePriority(_BacklogCase):

    def report(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return report_agent_issue.report_issue(title="Mock Anomaly", error_detail=kw.pop("error", "boom"), **kw)

    def test_default_priority_from_severity(self):
        res = self.report(severity="HIGH", category="tool_error")
        self.assertEqual(res["priority"], "P1")
        entry = self.last_entry()
        self.assertEqual(entry["title"], "Mock Anomaly")
        self.assertEqual(entry["priority"], "P1")
        self.assertEqual(entry["labels"], ["agent-failure", "severity:high", "priority:P1", "cat:tool_error"])
        for heading in issue_telemetry.SECTION_HEADINGS:
            self.assertIn(heading, entry["body"])
        self.assertIn("| **Priority** | **🟠 P1** |", entry["body"])
        self.assertIn("| **Fingerprint ID** |", entry["body"])

    def test_lowercase_severity_and_priority_override(self):
        self.report(severity="low", error="first")
        entry = self.last_entry()
        self.assertEqual((entry["severity"], entry["priority"]), ("LOW", "P3"))
        self.assertIn("severity:low", entry["labels"])
        self.assertIn("priority:P3", entry["labels"])

        self.report(severity="medium", priority="p0", error="second")
        entry = self.last_entry()
        self.assertEqual(entry["priority"], "P0")
        self.assertIn("priority:P0", entry["labels"])

    def test_structured_fields_in_body(self):
        out_file = os.path.join(self.tmp.name, "out.txt")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write("".join(f"row {i}\n" for i in range(250)) + "balance $77.10 USDT\n")
        ctx_file = os.path.join(self.tmp.name, "ctx.txt")
        with open(ctx_file, "w", encoding="utf-8") as f:
            f.write("file context")
        self.report(severity="CRITICAL", repro="python3 x.py (exit 2)", root_cause="rc",
                    affected_files="scripts/x.py:1-9", context="inline context", context_file=ctx_file,
                    output_file=out_file, impact="desk impact", acceptance_criteria="a; b")
        body = self.last_entry()["body"]
        self.assertIn("```bash\npython3 x.py (exit 2)\n```", body)
        self.assertIn("- `scripts/x.py:1-9`", body)
        self.assertIn("inline context\n\nfile context", body)
        self.assertIn("row 249", body)
        self.assertNotIn("\nrow 10\n", body)  # only the last 200 lines
        self.assertNotIn("77.10", body)
        self.assertIn("desk impact", body)
        self.assertIn("- [ ] a\n- [ ] b", body)
        self.assertIn("| **Priority** | **🔴 P0** |", body)

    def test_monetary_keys_redacted_in_output_and_context_files(self):
        from test_report_issue import session_state, monetary_values
        state = session_state()
        state["account"] = {"availableBalance": "1523.77", "totalWalletBalance": 2045.61, "equity": -12.5}
        dumped = json.dumps(state, indent=2)
        paths = []
        for name in ("out.json", "ctx.json"):
            paths.append(os.path.join(self.tmp.name, name))
            with open(paths[-1], "w", encoding="utf-8") as f:
                f.write(dumped)
        self.report(severity="HIGH", error="notional_usdt=4321.87 margin_usdt: -864.37",
                    output_file=paths[0], context_file=paths[1])
        body = self.last_entry()["body"]
        self.assertIn('"notional_usdt": "[REDACTED]"', body)
        self.assertIn('notional_usdt="[REDACTED]"', body)
        self.assertIn("LONG_HEAVY", body)
        values = monetary_values(state)
        self.assertGreater(len(values), 10)
        for value in values:
            self.assertNotIn(value, body)

    def test_signed_amounts_from_session_summary_are_redacted(self):
        from test_report_issue import (summary_state, market_summary_text, SUMMARY_AMOUNTS,
                                       SIGNED_AMOUNTS_TEXT)
        summary = market_summary_text()
        for amount in ("$+3087.31", "| -16.75 |", "| +3.20 |"):
            self.assertIn(amount, summary)
        with open(os.path.join(self.tmp.name, "session_state.json"), "w", encoding="utf-8") as f:
            json.dump(summary_state(), f)  # LOGS_DIR is patched to tmp: telemetry lists the symbols
        out_file = os.path.join(self.tmp.name, "sync_stdout.txt")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(summary)
        self.report(severity="HIGH", error=SIGNED_AMOUNTS_TEXT, output_file=out_file)
        body = self.last_entry()["body"]
        for amount in SUMMARY_AMOUNTS:
            self.assertNotIn(amount, body)
        for kept in ("**API3USDT**", "**1000SHIBUSDT**", "**BTCUSDT**", "LONG_HEAVY",
                     "API3USDT and 1000SHIBUSDT rejected", "ROE -0.3% kept", "+8.4%",
                     "API3USDT LONG", "1000SHIBUSDT SHORT"):
            self.assertIn(kept, body)

    def test_sanitizer_redacts_signed_and_backticked_amounts(self):
        cleaned = report_agent_issue.sanitize_telemetry(
            "Net Delta: $+3087.31 | -16.75 | `$+3.20` USDT `+4.10` USDT **5.55** USDT pnl=+7.5 "
            "API3USDT C98USDT 1000SHIBUSDT -0.3% t-stat: -3.34")
        for leaked in ("3087.31", "16.75", "3.20", "4.10", "5.55", "7.5", "3.34"):
            self.assertNotIn(leaked, cleaned)
        for kept in ("API3USDT", "C98USDT", "1000SHIBUSDT", "-0.3%"):
            self.assertIn(kept, cleaned)

    def test_sanitizer_redacts_monetary_keys(self):
        cleaned = report_agent_issue.sanitize_telemetry(
            '{"unrealized_pnl_usdt": -16.75, "net_realized_pnl_usdt": "42.1234", "roe_pct": 1.9} margin=3.5e2')
        for value in ("16.75", "42.1234", "3.5e2"):
            self.assertNotIn(value, cleaned)
        self.assertIn('"roe_pct": 1.9', cleaned)  # unsigned non-monetary values untouched
        self.assertIn('"unrealized_pnl_usdt": "[REDACTED]"', cleaned)

    def test_category_normalized_and_validated(self):
        self.report(severity="HIGH", category="Risk_Gate")
        self.assertEqual(self.last_entry()["labels"][-1], "cat:risk_gate")
        with self.assertRaises(ValueError):
            self.report(severity="HIGH", category="tool error; rm", error="other")

    def test_invalid_values_raise(self):
        with self.assertRaises(ValueError):
            self.report(severity="urgent")
        with self.assertRaises(ValueError):
            self.report(severity="HIGH", priority="P7")

    def test_system_context_uses_real_session_state_keys(self):
        from test_report_issue import session_state
        with open(os.path.join(self.tmp.name, "session_state.json"), "w", encoding="utf-8") as f:
            json.dump(session_state(), f)
        ctx = report_agent_issue.get_system_context()  # LOGS_DIR and git are patched in setUp
        self.assertEqual(ctx["delta_bias"], "LONG_HEAVY")
        self.assertEqual(ctx["commit"], "unavailable")
        self.assertEqual(ctx["floating_pnl_sign"], "NEGATIVE")
        self.assertIn("Delta: LONG_HEAVY, Positions: 2, Floating Bias: NEGATIVE", ctx["portfolio_summary"])


class TestCliValidation(_BacklogCase):

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["report_agent_issue.py", *args]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            report_agent_issue.main()
        return out.getvalue(), err.getvalue()

    def test_invalid_severity_exits_non_zero(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--title", "t", "--error", "e", "--severity", "urgent")
        self.assertNotEqual(cm.exception.code, 0)
        self.assertFalse(os.path.exists(self.backlog))

    def test_invalid_priority_exits_non_zero(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--title", "t", "--error", "e", "--priority", "P5")
        self.assertNotEqual(cm.exception.code, 0)
        self.assertFalse(os.path.exists(self.backlog))

    def test_category_cli_validation(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--title", "t", "--error", "e", "--category", "tool error")
        self.assertNotEqual(cm.exception.code, 0)
        with self.assertRaises(SystemExit):
            self.run_main("--title", "t", "--error", "e", "--category", "not_a_choice")
        self.assertFalse(os.path.exists(self.backlog))
        self.run_main("--title", "t", "--error", "e", "--category", "INFRA")
        self.assertEqual(self.last_entry()["labels"][-1], "cat:infra")

    def test_sync_backlog_injects_default_priority_into_legacy_entries(self):
        legacy = {"title": "legacy", "body": "b", "labels": ["agent-failure", "severity:critical", "cat:infra"]}
        modern = {"title": "modern", "body": "b", "labels": ["agent-failure", "severity:low", "priority:P1"]}
        with open(self.backlog, "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy) + "\n" + json.dumps(modern) + "\n")
        sent = []

        def fake_dispatch(title, body, labels, repo, token=None):
            sent.append((title, labels))
            return {"success": True, "issue_number": 1, "html_url": "u", "labels_applied": True}
        with patch("report_agent_issue.dispatch_github_issue", side_effect=fake_dispatch), \
             patch("report_agent_issue.time.sleep"), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "x"}), \
             contextlib.redirect_stdout(io.StringIO()):
            report_agent_issue.sync_backlog(repo="owner/repo")
        self.assertEqual(sent, [
            ("legacy", ["agent-failure", "severity:critical", "priority:P0", "cat:infra"]),
            ("modern", ["agent-failure", "severity:low", "priority:P1"]),
        ])
        with open(self.backlog, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "")

    def test_lowercase_cli_values(self):
        self.run_main("--title", "t", "--error", "e", "--severity", "medium")
        self.assertEqual(self.last_entry()["labels"][1:3], ["severity:medium", "priority:P2"])
        self.run_main("--title", "t2", "--error", "e2", "--severity", "low", "--priority", "p1",
                      "--repro", "cmd (exit 1)", "--acceptance-criteria", "x")
        entry = self.last_entry()
        self.assertEqual(entry["labels"][1:3], ["severity:low", "priority:P1"])
        self.assertIn("```bash\ncmd (exit 1)\n```", entry["body"])


class TestDispatchLabelFallback(unittest.TestCase):

    LABELS = ["agent-failure", "severity:high", "priority:P1", "cat:tool_error"]

    def setUp(self):
        self.requests = []
        self.responses = []
        p = patch.object(report_agent_issue.urllib.request, "urlopen", side_effect=self._urlopen)
        p.start()
        self.addCleanup(p.stop)

    def _urlopen(self, req, timeout=None):
        self.requests.append(json.loads(req.data.decode("utf-8")))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return fake_response(item)

    def dispatch(self, which=None, run=None):
        out = io.StringIO()
        with patch("report_agent_issue.shutil.which", return_value=which), \
             patch("report_agent_issue.subprocess.run", side_effect=run or AssertionError("gh must not run")), \
             contextlib.redirect_stdout(out):
            res = report_agent_issue.dispatch_github_issue("t", "body", list(self.LABELS), "owner/repo", token="x")
        return res, out.getvalue()

    def test_labels_applied(self):
        self.responses.append({"number": 7, "html_url": "u", "labels": [{"name": l} for l in self.LABELS]})
        res, out = self.dispatch()
        self.assertTrue(res["labels_applied"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["labels"], self.LABELS)
        self.assertNotIn("WARNING", out)

    def test_dropped_labels_warn_with_exact_command(self):
        self.responses.append({"number": 7, "html_url": "u", "labels": [{"name": "agent-failure"}]})
        res, out = self.dispatch()
        self.assertFalse(res["labels_applied"])
        self.assertIn("⚠️", out)
        self.assertIn("gh issue edit 7 --repo owner/repo --add-label severity:high,priority:P1", out)

    def test_dropped_labels_reapplied_via_gh(self):
        self.responses.append({"number": 7, "html_url": "u", "labels": []})
        run = MagicMock(return_value=MagicMock(returncode=0))
        res, out = self.dispatch(which="/usr/bin/gh", run=run)
        self.assertTrue(res["labels_applied"])
        args = run.call_args[0][0]
        self.assertEqual(args, ["gh", "issue", "edit", "7", "--repo", "owner/repo",
                                "--add-label", "severity:high,priority:P1"])

    def test_422_retries_without_labels_with_prefixed_title(self):
        err = urllib.error.HTTPError("u", 422, "Unprocessable Entity", None,
                                     io.BytesIO(b'{"message":"Validation Failed","errors":[{"resource":"Label"}]}'))
        self.responses.extend([err, {"number": 8, "html_url": "u8", "labels": []}])
        res, out = self.dispatch()
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("labels", self.requests[1])
        self.assertEqual(self.requests[1]["title"], "[HIGH/P1] t")
        self.assertEqual(res["issue_number"], 8)
        self.assertFalse(res["labels_applied"])
        self.assertIn("gh issue edit 8 --repo owner/repo --add-label severity:high,priority:P1", out)

    def test_server_error_is_not_retried(self):
        self.responses.append(urllib.error.HTTPError("u", 502, "Bad Gateway", None, io.BytesIO(b"")))
        with self.assertRaises(urllib.error.HTTPError):
            self.dispatch()
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
