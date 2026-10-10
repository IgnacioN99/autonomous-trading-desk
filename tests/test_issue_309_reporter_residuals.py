#!/usr/bin/env python3
"""
test_issue_309_reporter_residuals.py - Issue #309: reporter dedupe residuals after #284.

1. --sync-backlog files one issue per fingerprint: the most severe queued entry (ties: the oldest).
2. An open issue without a severity:* label does not swallow a CRITICAL / HIGH report (MEDIUM / LOW still dedupe).
3. A failing local hit record in --find-open-issue writes one stderr line (class only) and never fails.

Hermetic: temp backlog / fingerprint files, cleared environment, urlopen blocked, gh and dispatch stubbed.
"""

import contextlib
import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(TESTS_DIR), "scripts")
for _p in (SCRIPTS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import report_agent_issue  # noqa: E402
import test_issue_284_session_residuals as t284  # noqa: E402  (module import: its tests are not collected twice)
from test_report_agent_issue_priority import _BacklogCase  # noqa: E402


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


class TestSyncKeepsMostSevere(_BacklogCase):

    def queue(self, *entries):
        with open(self.backlog, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(e) + "\n" for e in entries))

    @staticmethod
    def entry(fp, title, severity):
        return {"fingerprint": fp, "title": title, "body": "b", "severity": severity,
                "labels": ["agent-failure", f"severity:{severity.lower()}"]}

    def sync(self, dispatch):
        out = io.StringIO()
        with patch("report_agent_issue.dispatch_github_issue", dispatch), \
             patch("report_agent_issue.time.sleep"), patch.dict(os.environ, {"GITHUB_TOKEN": "x"}), \
             contextlib.redirect_stdout(out):
            report_agent_issue.sync_backlog(repo="owner/repo")
        return out.getvalue()

    def queued(self):
        with open(self.backlog, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def ok(self, **kw):
        return MagicMock(return_value={"success": True, "issue_number": 9, "html_url": "u9"})

    def test_high_then_critical_files_only_the_critical(self):
        self.queue(self.entry("fpA", "high one", "HIGH"), self.entry("fpA", "critical one", "CRITICAL"))
        dispatch = self.ok()
        self.sync(dispatch)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.kwargs["title"], "critical one")
        self.assertEqual(self.queued(), [])

    def test_critical_then_high_and_ties_keep_the_oldest(self):
        self.queue(self.entry("fpA", "first", "CRITICAL"), self.entry("fpA", "second", "HIGH"),
                   self.entry("fpB", "old high", "HIGH"), self.entry("fpB", "new high", "HIGH"))
        dispatch = self.ok()
        self.sync(dispatch)
        self.assertEqual([c.kwargs["title"] for c in dispatch.call_args_list], ["first", "old high"])
        self.assertEqual(self.queued(), [])

    def test_two_different_failures_are_both_filed(self):
        self.queue(self.entry("fpA", "a", "HIGH"), self.entry("fpB", "b", "CRITICAL"))
        dispatch = self.ok()
        self.sync(dispatch)
        self.assertEqual(sorted(c.kwargs["title"] for c in dispatch.call_args_list), ["a", "b"])
        self.assertEqual(self.queued(), [])

    def test_entries_without_a_fingerprint_are_untouched(self):
        legacy = {"title": "legacy", "body": "b", "labels": ["agent-failure", "severity:high"]}
        self.queue(legacy, dict(legacy), self.entry("fpA", "a", "LOW"))
        dispatch = self.ok()
        self.sync(dispatch)
        self.assertEqual([c.kwargs["title"] for c in dispatch.call_args_list], ["legacy", "legacy", "a"])

    def test_severity_falls_back_to_the_labels(self):
        low = {"fingerprint": "fpA", "title": "low", "body": "b", "labels": ["severity:low"]}
        crit = {"fingerprint": "fpA", "title": "crit", "body": "b", "labels": ["severity:critical"]}
        self.queue(low, crit)
        dispatch = self.ok()
        self.sync(dispatch)
        self.assertEqual([c.kwargs["title"] for c in dispatch.call_args_list], ["crit"])

    def test_filing_failure_keeps_every_entry_queued(self):
        entries = [self.entry("fpA", "high one", "HIGH"), self.entry("fpA", "critical one", "CRITICAL"),
                   self.entry("fpB", "b", "HIGH")]
        self.queue(*entries)
        dispatch = MagicMock(side_effect=lambda title, **kw: (_ for _ in ()).throw(RuntimeError("down"))
                             if title != "b" else {"success": True, "issue_number": 3, "html_url": "u3"})
        self.sync(dispatch)
        self.assertEqual(sorted(e["title"] for e in self.queued()), ["critical one", "high one"])


class TestUnlabelledOpenIssue(t284.TestReporterBoundsAndSeverity):
    """The #284 fixtures (gh / POST stubs), only the tests of this issue."""

    def test_unlabelled_issue_does_not_swallow_critical_or_high(self):
        for severity in ("CRITICAL", "HIGH"):
            with self.subTest(severity=severity):
                self.posts.clear()
                if os.path.exists(self.fps):
                    os.remove(self.fps)
                self.open_with("agent-failure")
                res = self.report(severity)
                self.assertEqual((res["status"], len(self.posts)), ("PUBLISHED_GITHUB", 1))
                self.assertIn(f"severity:{severity.lower()}", self.posts[0]["labels"])

    def test_unlabelled_issue_still_deduplicates_medium_and_low(self):
        for severity in ("MEDIUM", "LOW"):
            with self.subTest(severity=severity):
                if os.path.exists(self.fps):
                    os.remove(self.fps)
                self.open_with("agent-failure")
                self.assertTrue(self.report(severity)["deduplicated"])
                self.assertEqual(self.posts, [])

    def test_labelled_behaviour_is_unchanged(self):
        self.open_with("severity:high")
        self.assertTrue(self.report("HIGH")["deduplicated"])
        os.remove(self.fps)
        self.assertEqual(self.report("CRITICAL")["status"], "PUBLISHED_GITHUB")

    def test_shell_lookup_applies_the_same_rule(self):
        self.open_with("agent-failure")
        out = self.main("--find-open-issue", "--title", self.TITLE, "--error", self.ERROR, "--severity", "CRITICAL",
                        "--repo", "owner/repo")
        self.assertEqual(out, f"fingerprint\t{self.fp}\n")  # the shell creates it
        out = self.main("--find-open-issue", "--title", self.TITLE, "--error", self.ERROR, "--severity", "MEDIUM",
                        "--repo", "owner/repo")
        self.assertIn("open_issue\t", out)

    def test_helper(self):
        f = report_agent_issue.open_issue_outranked
        self.assertTrue(f("CRITICAL", None) and f("high", None))
        self.assertFalse(f("MEDIUM", None) or f("LOW", None) or f(None, None))
        self.assertTrue(f("CRITICAL", "HIGH"))
        self.assertFalse(f("HIGH", "HIGH"))

    def test_hit_record_failure_writes_one_stderr_line_and_keeps_the_hit(self):
        self.open_with("severity:high")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["report_agent_issue.py", "--find-open-issue", "--title", self.TITLE,
                                        "--error", self.ERROR, "--severity", "HIGH", "--repo", "owner/repo"]), \
                patch("report_agent_issue.record_open_issue_hit", side_effect=OSError("secret path /x")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            report_agent_issue.main()
        self.assertEqual(out.getvalue(), f"fingerprint\t{self.fp}\nopen_issue\thttps://github.com/owner/repo/issues/5\n")
        self.assertEqual(err.getvalue().strip().splitlines(), ["note: local hit record failed (OSError)"])


# Do not re-run the inherited #284 tests here
for _name in [n for n in dir(t284.TestReporterBoundsAndSeverity) if n.startswith("test_")]:
    if _name not in TestUnlabelledOpenIssue.__dict__:
        setattr(TestUnlabelledOpenIssue, _name, None)


if __name__ == "__main__":
    unittest.main()
