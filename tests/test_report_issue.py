"""
test_report_issue.py - scripts/report_issue.sh publishes through the GitHub CLI (gh).

A stub `gh` on PATH records the payload instead of calling GitHub.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE_DIR, "scripts", "report_issue.sh")

GH_STUB = """#!/usr/bin/env bash
if [ "$1" = "auth" ]; then exit 0; fi
if [ "$1" = "api" ]; then
  cat > "$(dirname "$0")/payload.json"
  if [ -n "$STUB_REJECT_LABELS" ] && grep -q '"labels"' "$(dirname "$0")/payload.json"; then exit 1; fi
  echo "https://github.com/owner/repo/issues/7"
  exit 0
fi
exit 1
"""


@unittest.skipUnless(shutil.which("bash") and os.name != "nt", "requires a POSIX bash")
class TestReportIssueViaGh(unittest.TestCase):

    def setUp(self):
        self.stub_dir = tempfile.mkdtemp()
        stub = os.path.join(self.stub_dir, "gh")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(GH_STUB)
        os.chmod(stub, 0o755)
        self.env = dict(os.environ, PATH=self.stub_dir + os.pathsep + os.environ.get("PATH", ""),
                        GITHUB_REPO="owner/repo", GITHUB_TOKEN="")

    def tearDown(self):
        shutil.rmtree(self.stub_dir, ignore_errors=True)

    def run_script(self, *args, **env):
        return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                              env=dict(self.env, **env), timeout=60)

    def payload(self):
        with open(os.path.join(self.stub_dir, "payload.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_issue_created_through_gh_with_title_and_labels(self):
        res = self.run_script("--title", "executor: SL unverified", "--error", "boom",
                              "--severity", "HIGH", "--category", "tool_error")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("issues/7", res.stdout)
        data = self.payload()
        self.assertEqual(data["title"], "executor: SL unverified")
        self.assertEqual(data["labels"], ["agent-failure", "severity:high", "cat:tool_error"])
        self.assertNotIn("Invalid range", res.stderr)

    def test_secrets_and_amounts_are_redacted(self):
        res = self.run_script("--title", "t", "--error",
                              'Authorization: Bearer abc.def-ghi_123 api_key=ABCDEFGH12345 balance $96.38')
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        body = self.payload()["body"]
        for secret in ("abc.def-ghi_123", "ABCDEFGH12345", "96.38"):
            self.assertNotIn(secret, body)

    def test_retries_without_labels_when_rejected(self):
        res = self.run_script("--title", "t", "--error", "e", STUB_REJECT_LABELS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("labels", self.payload())


if __name__ == "__main__":
    unittest.main()
