"""
test_report_issue.py - scripts/report_issue.sh publishes structured, labelled reports through the GitHub CLI (gh).

A stub `gh` on PATH records every call (calls.log) and the last payload instead of calling GitHub.
Stub switches (environment variables):
  STUB_REJECT_LABELS=1              POST with labels fails with "HTTP 422" on stderr (repository rejects them)
  STUB_POST_FAILS=<code>            every POST fails with "HTTP <code>" on stderr (e.g. 502)
  STUB_LABELS_FIXED_AFTER_CREATE=1  ...unless a `gh label create` call was recorded before
  STUB_DROP_LABELS=1                POST succeeds but GET issues/7 reports no labels (silently dropped)
  STUB_LABEL_CREATE_FAILS=1         `gh label create` exits 1
  STUB_EDIT_FAILS=1                 `gh issue edit` exits 1
  STUB_AUTH_FAILS=1                 `gh auth status` exits 1 (unauthenticated -> offline backlog)
A stub `curl` always fails (exit 7) so no test can reach api.github.com, unless
  STUB_CURL_MODE=created_without_labels  it prints a 201 response whose labels miss severity/priority
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
SCRIPT = os.path.join(BASE_DIR, "scripts", "report_issue.sh")
HELPER = os.path.join(BASE_DIR, "scripts", "utils", "issue_telemetry.py")
ENV_RESOLVER = os.path.join(BASE_DIR, "scripts", "utils", "env_resolver.py")

SECTION_HEADINGS = [
    "### 1. Executive Summary & Severity Matrix",
    "### 2. Runtime & Ledger Telemetry Snapshot",
    "### 3. Reproduction & Exact Telemetry",
    "### 4. Code Pointers & Root Cause Analysis",
    "### 5. Operational Impact on Trading Desk",
    "### 6. Remediation Plan & Acceptance Criteria",
]

# Bash-only on purpose: it must keep working when the test replaces python3 with a broken stub.
GH_STUB = r"""#!/usr/bin/env bash
DIR="$(dirname "$0")"
echo "$*" >> "$DIR/calls.log"
if [ "$1" = "auth" ]; then [ -n "$STUB_AUTH_FAILS" ] && exit 1; exit 0; fi
if [ "$1" = "api" ] && [ "$2" = "-X" ]; then
  cat > "$DIR/payload.json"
  if [ -n "$STUB_POST_FAILS" ]; then echo "gh: HTTP ${STUB_POST_FAILS}: Bad Gateway" >&2; exit 1; fi
  if [ -n "$STUB_REJECT_LABELS" ] && grep -q '"labels"' "$DIR/payload.json"; then
    if [ -z "$STUB_LABELS_FIXED_AFTER_CREATE" ] || ! grep -q '^label create' "$DIR/calls.log"; then
      echo "gh: HTTP 422: Validation Failed (https://api.github.com/repos/owner/repo/issues)" >&2
      exit 1
    fi
  fi
  echo "https://github.com/owner/repo/issues/7"
  exit 0
fi
if [ "$1" = "api" ]; then
  if [ -z "$STUB_DROP_LABELS" ] && [ -f "$DIR/payload.json" ]; then
    tr '\n' ' ' < "$DIR/payload.json" | grep -o '"labels"[^]]*]' | grep -o '"[^"]*"' | sed '1d' | tr -d '"'
  fi
  exit 0
fi
if [ "$1" = "label" ] && [ "$2" = "create" ]; then
  [ -n "$STUB_LABEL_CREATE_FAILS" ] && exit 1
  exit 0
fi
if [ "$1" = "issue" ] && [ "$2" = "edit" ]; then
  [ -n "$STUB_EDIT_FAILS" ] && exit 1
  exit 0
fi
exit 1
"""

CURL_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$(dirname "$0")/curl_calls.log"
if [ "$STUB_CURL_MODE" = "created_without_labels" ]; then
  printf '{"number": 9, "html_url": "https://github.com/owner/repo/issues/9", "labels": [{"name": "agent-failure"}]}\n201'
  exit 0
fi
exit 7
"""

MONETARY_KEY_RE = re.compile(r"usdt|usd|pnl|notional|margin|balance|equity|profit|wallet", re.IGNORECASE)

NOTIONAL_LONG = 4321.87
NOTIONAL_SHORT = 1234.56
MARGIN_LONG = 864.37
MARGIN_SHORT = 246.91
FLOATING_PNL = -17.4321
REALIZED_PNL = 42.1234


def session_state():
    """Mirrors the schema written by scripts/sync_session_state.py."""
    return {
        "is_valid": True,
        "last_updated_ts": int(time.time()) - 30,
        "last_updated_utc": "2026-10-05 10:00:00 UTC",
        "target_env": "testnet",
        "macro_btc": {"price_usdt": 98765.43},
        "portfolio_exposure": {
            "total_active_positions": 2,
            "long_notional_usdt": NOTIONAL_LONG,
            "short_notional_usdt": NOTIONAL_SHORT,
            "net_notional_delta_usdt": 3087.31,
            "delta_bias": "LONG_HEAVY",
            "delta_advice": "Prefer shorts",
            "total_floating_pnl_usdt": FLOATING_PNL,
        },
        "active_positions": [
            {"symbol": "SOLUSDT", "direction": "SHORT", "qty": -8.5, "entry_price": 145.23, "mark_price": 145.31,
             "unrealized_pnl_usdt": -0.68, "roe_pct": -0.27, "leverage": 5, "notional_usdt": NOTIONAL_SHORT,
             "margin_usdt": MARGIN_SHORT, "sl_algo_verified": True},
            {"symbol": "BTCUSDT", "direction": "LONG", "qty": 0.044, "entry_price": 98111.11, "mark_price": 98222.22,
             "unrealized_pnl_usdt": -16.75, "roe_pct": -1.9, "leverage": 5, "notional_usdt": NOTIONAL_LONG,
             "margin_usdt": MARGIN_LONG, "sl_algo_verified": False},
        ],
        "active_sl_algo_orders": [],
        "active_tp_limit_orders": [],
        "closed_today_summary": {
            "closed_trades_count": 3, "wins": 2, "losses": 1, "win_rate_pct": 66.7,
            "gross_realized_pnl_usdt": 45.0, "commissions_usdt": 2.8766, "net_realized_pnl_usdt": REALIZED_PNL,
        },
    }


def summary_state():
    """session_state() plus everything scripts/sync_session_state.py format_markdown_summary renders:
    a positive PnL (+3.20), symbols with digits next to USDT (API3USDT, 1000SHIBUSDT) and a shadow summary."""
    state = session_state()
    for p in state["active_positions"]:
        p.update(sl_price=round(p["entry_price"] * 0.98, 2), tp1_price=round(p["entry_price"] * 1.02, 2),
                 tp2_price=round(p["entry_price"] * 1.05, 2))
    state["active_positions"] += [
        {"symbol": "API3USDT", "direction": "LONG", "qty": 120.0, "entry_price": 1.523, "mark_price": 1.55,
         "unrealized_pnl_usdt": 3.2, "roe_pct": 8.4, "leverage": 5, "notional_usdt": 186.11, "margin_usdt": 37.2,
         "sl_algo_verified": True, "sl_price": 1.48, "tp1_price": 1.6, "tp2_price": 1.7},
        {"symbol": "1000SHIBUSDT", "direction": "SHORT", "qty": -9000.0, "entry_price": 0.01234, "mark_price": 0.01261,
         "unrealized_pnl_usdt": -2.45, "roe_pct": -11.2, "leverage": 5, "notional_usdt": 113.49, "margin_usdt": 19.75,
         "sl_algo_verified": True, "sl_price": 0.0129, "tp1_price": 0.0119, "tp2_price": 0.0112},
    ]
    state["portfolio_exposure"]["total_active_positions"] = 4
    state["shadow_desk_summary"] = {
        "total_resolved": 5, "intraday_fer_pct": 60.0, "filter_efficacy_ratio_pct": 55.0, "true_negatives": 3,
        "false_negatives": 2, "timeouts": 0, "intraday_net_edge_usdt": 12.34, "rolling_fer_pct": 58.0,
        "capital_saved_usdt": 77.65, "missed_alpha_usdt": 5.6, "active_shadow_trades": 2,
    }
    return state


# Every money figure format_markdown_summary(summary_state()) prints (signed, $-prefixed, bold, table cells)
SUMMARY_AMOUNTS = ("3087.31", "16.75", "3.20", "0.68", "2.45", "4321.87", "1234.56", "246.91", "864.37", "37.20",
                   "19.75", "17.4321", "42.1234", "2.8766", "98,765.43", "765.43", "12.34", "77.65", "5.60")
SIGNED_AMOUNTS_TEXT = ("API3USDT and 1000SHIBUSDT rejected: **Net Delta:** $+3087.31, cell | -16.75 |, "
                       "`$+3.20` USDT, `+3.20` USDT, (-0.68), ROE -0.3% kept")


def market_summary_text():
    """The real stdout summary of scripts/sync_session_state.py for summary_state()."""
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    from sync_session_state import format_markdown_summary
    return format_markdown_summary(summary_state())


def monetary_values(obj, key=""):
    """Every numeric value stored under a monetary key (as json.dumps renders it, sign stripped)."""
    if isinstance(obj, dict):
        return [v for k, val in obj.items() for v in monetary_values(val, k)]
    if isinstance(obj, list):
        return [v for item in obj for v in monetary_values(item, key)]
    if isinstance(obj, (int, float)) and not isinstance(obj, bool) and MONETARY_KEY_RE.search(key):
        return [json.dumps(abs(obj))]
    return []


@unittest.skipUnless(shutil.which("bash") and os.name != "nt", "requires a POSIX bash")
class TestReportIssueViaGh(unittest.TestCase):

    def setUp(self):
        self.stub_dir = tempfile.mkdtemp()
        self.logs_dir = tempfile.mkdtemp()
        for name, content in (("gh", GH_STUB), ("curl", CURL_STUB)):
            stub = os.path.join(self.stub_dir, name)
            with open(stub, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(stub, 0o755)
        with open(os.path.join(self.logs_dir, "session_state.json"), "w", encoding="utf-8") as f:
            json.dump(session_state(), f)
        self.env = dict(os.environ, PATH=self.stub_dir + os.pathsep + os.environ.get("PATH", ""),
                        GITHUB_REPO="owner/repo", GITHUB_TOKEN="", BINANCE_API_ENV="TESTNET",
                        ISSUE_REPORTER_LOGS_DIR=self.logs_dir)
        for key in ("STUB_REJECT_LABELS", "STUB_LABELS_FIXED_AFTER_CREATE", "STUB_DROP_LABELS",
                    "STUB_LABEL_CREATE_FAILS", "STUB_EDIT_FAILS", "STUB_AUTH_FAILS", "STUB_POST_FAILS",
                    "STUB_CURL_MODE"):
            self.env.pop(key, None)

    def tearDown(self):
        shutil.rmtree(self.stub_dir, ignore_errors=True)
        shutil.rmtree(self.logs_dir, ignore_errors=True)

    def run_script(self, *args, script=SCRIPT, path_prefix=None, **env):
        run_env = dict(self.env, **env)
        if path_prefix:
            run_env["PATH"] = path_prefix + os.pathsep + run_env["PATH"]
        return subprocess.run(["bash", script, *args], capture_output=True, text=True, env=run_env, timeout=90)

    def payload(self):
        with open(os.path.join(self.stub_dir, "payload.json"), encoding="utf-8") as f:
            return json.load(f)

    def calls(self):
        path = os.path.join(self.stub_dir, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f]

    def assert_six_sections(self, body):
        positions = [body.find(h) for h in SECTION_HEADINGS]
        self.assertNotIn(-1, positions, body)
        self.assertEqual(positions, sorted(positions))

    def isolated_copy(self, with_helper=True):
        """Copies the script (and optionally the telemetry helper) into a fresh temp tree."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "scripts", "utils"))
        shutil.copy(SCRIPT, os.path.join(tmp, "scripts", "report_issue.sh"))
        if with_helper:
            shutil.copy(HELPER, os.path.join(tmp, "scripts", "utils", "issue_telemetry.py"))
            shutil.copy(ENV_RESOLVER, os.path.join(tmp, "scripts", "utils", "env_resolver.py"))
        return tmp

    # (a)
    def test_issue_created_through_gh_with_title_and_labels(self):
        res = self.run_script("--title", "executor: SL unverified", "--error", "boom",
                              "--severity", "HIGH", "--category", "tool_error")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("issues/7", res.stdout)
        data = self.payload()
        self.assertEqual(data["title"], "executor: SL unverified")
        self.assertEqual(data["labels"], ["agent-failure", "severity:high", "priority:P1", "cat:tool_error"])
        self.assertNotIn("Invalid range", res.stderr)
        # Labels were verified after the create and nothing needed fixing
        self.assertTrue(any(c.startswith("api repos/owner/repo/issues/7") for c in self.calls()))
        self.assertFalse(any(c.startswith("issue edit") for c in self.calls()))
        self.assertNotIn("WARNING", res.stdout)

    def test_secrets_and_amounts_are_redacted(self):
        res = self.run_script("--title", "t", "--error",
                              'Authorization: Bearer abc.def-ghi_123 api_key=ABCDEFGH12345 balance $96.38')
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        body = self.payload()["body"]
        for secret in ("abc.def-ghi_123", "ABCDEFGH12345", "96.38"):
            self.assertNotIn(secret, body)

    # (b)
    def test_structured_flags_render_six_sections(self):
        ctx_file = os.path.join(self.logs_dir, "context.txt")
        with open(ctx_file, "w", encoding="utf-8") as f:
            f.write("Context from file: guardian loop was mid-cycle\n")
        out_file = os.path.join(self.logs_dir, "output.txt")
        with open(out_file, "w", encoding="utf-8") as f:
            for i in range(300):
                f.write(f"output line {i:03d}\n")
            f.write("Traceback: RuntimeError secret_abcdefghij1234567890 cost $55.10\n")
        res = self.run_script(
            "--title", "guardian: trailing stop not moved", "--error", "stop stayed at entry",
            "--severity", "HIGH", "--category", "risk_gate",
            "--repro", "python3 scripts/loops/position_guardian_loop.py --once --dry-run (exit 1)",
            "--root-cause", "ATR computed on stale candles",
            "--affected-files", "scripts/loops/position_guardian_loop.py:120-160, scripts/utils/atomic_writer.py:18",
            "--context", "Agent ran the guardian after TP1 filled",
            "--context-file", ctx_file,
            "--output-file", out_file,
            "--impact", "Winning position kept full risk",
            "--acceptance-criteria", "Stop ratchets after TP1; regression test in tests/",
            "--remediation", "Refresh candles before computing ATR",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        body = self.payload()["body"]
        self.assert_six_sections(body)
        self.assertIn("| **Priority** | **🟠 P1** |", body)
        self.assertLess(body.find("| **Severity** |"), body.find("| **Priority** |"))
        self.assertLess(body.find("| **Priority** |"), body.find("| **Category** |"))
        self.assertIn("```bash\npython3 scripts/loops/position_guardian_loop.py --once --dry-run (exit 1)\n```", body)
        self.assertIn("- `scripts/loops/position_guardian_loop.py:120-160`", body)
        self.assertIn("- `scripts/utils/atomic_writer.py:18`", body)
        self.assertIn("ATR computed on stale candles", body)
        self.assertIn("Agent ran the guardian after TP1 filled", body)
        self.assertIn("Context from file: guardian loop was mid-cycle", body)
        self.assertLess(body.find("Agent ran the guardian"), body.find("Context from file"))
        self.assertIn("Winning position kept full risk", body)
        self.assertIn("- [ ] Stop ratchets after TP1", body)
        self.assertIn("- [ ] regression test in tests/", body)
        self.assertIn("Refresh candles before computing ATR", body)
        self.assertIn("<details><summary>Raw output (tail)</summary>", body)
        self.assertIn("output line 299", body)
        self.assertNotIn("output line 050", body)  # only the last 200 lines
        self.assertNotIn("secret_abcdefghij1234567890", body)
        self.assertNotIn("55.10", body)

    def test_defaults_when_structured_flags_omitted(self):
        res = self.run_script("--title", "t", "--error", "e", "--category", "infra")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        body = self.payload()["body"]
        self.assert_six_sections(body)
        self.assertIn("Infrastructure/API degradation", body)
        self.assertIn("- [ ] Regression test added in tests/ covering this failure", body)
        self.assertIn("**Reproduction command:** not provided", body)

    # (c)
    @unittest.skipUnless(shutil.which("git"), "requires git")
    def test_telemetry_has_git_and_ledger_without_amounts(self):
        tmp = self.isolated_copy(with_helper=True)
        git = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
        subprocess.run(git + ["init", "-q"], check=True, capture_output=True)
        subprocess.run(git + ["symbolic-ref", "HEAD", "refs/heads/telemetry-branch"], check=True, capture_output=True)
        subprocess.run(git + ["add", "-A"], check=True, capture_output=True)
        subprocess.run(git + ["commit", "-q", "-m", "fixture"], check=True, capture_output=True)
        commit = subprocess.run(["git", "-C", tmp, "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, check=True).stdout.strip()

        res = self.run_script("--title", "t", "--error", "e",
                              script=os.path.join(tmp, "scripts", "report_issue.sh"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        body = self.payload()["body"]
        self.assert_six_sections(body)
        self.assertIn(f"`{commit}` on `telemetry-branch`", body)
        self.assertIn("LONG_HEAVY", body)
        self.assertIn("BTCUSDT LONG", body)
        self.assertIn("SOLUSDT SHORT", body)
        self.assertIn("`1/2`", body)  # one of two stops verified
        self.assertIn("NEGATIVE", body)  # floating PnL sign only
        self.assertIn("POSITIVE", body)  # realized PnL sign only
        self.assertIn("`TESTNET`", body)
        for amount in (NOTIONAL_LONG, NOTIONAL_SHORT, MARGIN_LONG, MARGIN_SHORT, FLOATING_PNL, REALIZED_PNL,
                       3087.31, 98765.43, 145.23, 98111.11):
            self.assertNotIn(str(amount), body)
            self.assertNotIn(str(abs(amount)), body)
        self.assertIsNone(re.search(r"\d+(?:\.\d+)?\s*USDT", body))

    # (d)
    def test_invalid_severity_exits_2_and_sends_nothing(self):
        res = self.run_script("--title", "t", "--error", "e", "--severity", "URGENT")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("invalid --severity", res.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.stub_dir, "payload.json")))
        self.assertFalse(any(c.startswith("api -X POST") for c in self.calls()))
        self.assertFalse(os.path.exists(os.path.join(self.logs_dir, "issues_backlog.jsonl")))

    def test_invalid_priority_exits_2(self):
        res = self.run_script("--title", "t", "--error", "e", "--priority", "P9")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("invalid --priority", res.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.stub_dir, "payload.json")))

    # (e)
    def test_lowercase_severity_is_normalized_with_default_priority(self):
        res = self.run_script("--title", "t", "--error", "e", "--severity", "low", "--category", "infra")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.payload()
        self.assertEqual(data["labels"], ["agent-failure", "severity:low", "priority:P3", "cat:infra"])
        self.assertIn("**🔵 LOW**", data["body"])
        self.assertIn("**🔵 P3**", data["body"])

    # (f)
    def test_priority_override(self):
        res = self.run_script("--title", "t", "--error", "e", "--severity", "MEDIUM", "--priority", "p0")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        labels = self.payload()["labels"]
        self.assertIn("severity:medium", labels)
        self.assertIn("priority:P0", labels)
        self.assertNotIn("priority:P2", labels)

    # (g)
    def test_rejected_labels_are_created_then_labelled_retry(self):
        res = self.run_script("--title", "t", "--error", "e", "--category", "tool_error",
                              STUB_REJECT_LABELS="1", STUB_LABELS_FIXED_AFTER_CREATE="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.payload()
        self.assertEqual(data["title"], "t")
        self.assertEqual(data["labels"], ["agent-failure", "severity:high", "priority:P1", "cat:tool_error"])
        created = [c for c in self.calls() if c.startswith("label create")]
        for label in data["labels"]:
            self.assertTrue(any(c.startswith(f"label create {label} ") and "--force" in c for c in created),
                            (label, created))
        self.assertTrue(any("severity:high" in c and "--color d93f0b" in c for c in created))
        self.assertTrue(any("priority:P1" in c and "--color d93f0b" in c for c in created))
        self.assertIn("issues/7", res.stdout)

    # (h)
    def test_label_create_fails_falls_back_to_prefixed_title(self):
        res = self.run_script("--title", "t", "--error", "e",
                              STUB_REJECT_LABELS="1", STUB_LABEL_CREATE_FAILS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.payload()
        self.assertNotIn("labels", data)
        self.assertTrue(data["title"].startswith("[HIGH/P1] "), data["title"])
        self.assertEqual(data["title"], "[HIGH/P1] t")
        self.assertIn("issue edit 7 --repo owner/repo --add-label severity:high,priority:P1", self.calls())
        self.assertIn("issues/7", res.stdout)

    def test_label_fix_up_failure_prints_exact_command(self):
        res = self.run_script("--title", "t", "--error", "e",
                              STUB_REJECT_LABELS="1", STUB_LABEL_CREATE_FAILS="1", STUB_EDIT_FAILS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("⚠️", res.stdout)
        self.assertIn("gh issue edit 7 --repo owner/repo --add-label severity:high,priority:P1", res.stdout)

    # (i)
    def test_silently_dropped_labels_are_reapplied(self):
        res = self.run_script("--title", "t", "--error", "e", "--severity", "CRITICAL", STUB_DROP_LABELS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.payload()["title"], "t")
        self.assertIn("issue edit 7 --repo owner/repo --add-label severity:critical,priority:P0", self.calls())

    def test_silently_dropped_labels_warn_when_edit_fails(self):
        res = self.run_script("--title", "t", "--error", "e", STUB_DROP_LABELS="1", STUB_EDIT_FAILS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("gh issue edit 7 --repo owner/repo --add-label severity:high,priority:P1", res.stdout)

    # (j)
    def test_sync_publishes_queued_payload_with_both_labels(self):
        queued = {"title": "queued failure", "body": "b",
                  "labels": ["agent-failure", "severity:critical", "priority:P0", "cat:infra"]}
        backlog = os.path.join(self.logs_dir, "issues_backlog.jsonl")
        with open(backlog, "w", encoding="utf-8") as f:
            f.write(json.dumps(queued) + "\n")
        res = self.run_script("--sync")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("1 published", res.stdout)
        data = self.payload()
        self.assertEqual(data["title"], "queued failure")
        self.assertEqual(data["labels"], queued["labels"])
        with open(backlog, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "")

    def test_offline_backlog_keeps_both_labels(self):
        res = self.run_script("--title", "t", "--error", "e", "--severity", "medium", STUB_AUTH_FAILS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with open(os.path.join(self.logs_dir, "issues_backlog.jsonl"), encoding="utf-8") as f:
            entry = json.loads(f.readline())
        self.assertIn("severity:medium", entry["labels"])
        self.assertIn("priority:P2", entry["labels"])
        self.assertFalse(any(c.startswith("api -X POST") for c in self.calls()))

    # (k)
    def test_telemetry_degrades_without_helper(self):
        tmp = self.isolated_copy(with_helper=False)
        res = self.run_script("--title", "t", "--error", "e", "--repro", "cmd (exit 3)",
                              script=os.path.join(tmp, "scripts", "report_issue.sh"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        body = self.payload()["body"]
        self.assert_six_sections(body)
        self.assertIn("unavailable (python3 or scripts/utils/issue_telemetry.py missing)", body)
        self.assertIn("| **Priority** | **🟠 P1** |", body)
        self.assertIn("`TESTNET`", body)

    def test_report_survives_broken_python(self):
        broken = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, broken, True)
        with open(os.path.join(broken, "python3"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(broken, "python3"), 0o755)
        res = self.run_script("--title", 'quote " and tab\there', "--error", "line1\nline2 \"q\" \\ back",
                              "--severity", "low", path_prefix=broken)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.payload()  # still valid JSON without python3
        self.assertEqual(data["title"], 'quote " and tab\there')
        self.assertEqual(data["labels"], ["agent-failure", "severity:low", "priority:P3", "cat:agent_failure"])
        self.assertIn('line1\nline2 "q" \\ back', data["body"])
        self.assert_six_sections(data["body"])

    def test_label_fallback_and_sync_without_python(self):
        broken = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, broken, True)
        with open(os.path.join(broken, "python3"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(broken, "python3"), 0o755)
        res = self.run_script("--title", "t", "--error", "e", "--severity", "medium", path_prefix=broken,
                              STUB_REJECT_LABELS="1", STUB_LABEL_CREATE_FAILS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.payload()
        self.assertNotIn("labels", data)
        self.assertEqual(data["title"], "[MEDIUM/P2] t")
        self.assertIn("issue edit 7 --repo owner/repo --add-label severity:medium,priority:P2", self.calls())

        backlog = os.path.join(self.logs_dir, "issues_backlog.jsonl")
        with open(backlog, "w", encoding="utf-8") as f:
            f.write(json.dumps({"title": "queued", "body": "b",
                                "labels": ["agent-failure", "severity:low", "priority:P3"]}) + "\n")
        res = self.run_script("--sync", path_prefix=broken)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("1 published", res.stdout)
        self.assertEqual(self.payload()["labels"], ["agent-failure", "severity:low", "priority:P3"])

    # Round 2 -----------------------------------------------------------------
    def test_monetary_keys_redacted_in_output_and_context_files(self):
        state = session_state()
        state["account"] = {"availableBalance": "1523.77", "totalWalletBalance": 2045.61, "equity": -12.5}
        dumped = json.dumps(state, indent=2)
        out_file = os.path.join(self.logs_dir, "out.json")
        ctx_file = os.path.join(self.logs_dir, "ctx.json")
        for path in (out_file, ctx_file):
            with open(path, "w", encoding="utf-8") as f:
                f.write(dumped)
        res = self.run_script("--title", "t", "--error", "notional_usdt=4321.87 margin_usdt: -864.37",
                              "--output-file", out_file, "--context-file", ctx_file)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        body = self.payload()["body"]
        self.assertIn('"notional_usdt": "[REDACTED]"', body)
        self.assertIn('"availableBalance": "[REDACTED]"', body)
        self.assertIn('notional_usdt="[REDACTED]"', body)
        self.assertIn("LONG_HEAVY", body)  # non-monetary content survives
        values = monetary_values(state)
        self.assertGreater(len(values), 10)
        for value in values:
            self.assertNotIn(value, body)

    def test_signed_amounts_from_session_summary_are_redacted(self):
        summary = market_summary_text()
        for amount in ("$+3087.31", "| -16.75 |", "| +3.20 |", "**API3USDT**", "**1000SHIBUSDT**"):
            self.assertIn(amount, summary)  # the fixture really exercises the leaky formats
        with open(os.path.join(self.logs_dir, "session_state.json"), "w", encoding="utf-8") as f:
            json.dump(summary_state(), f)
        out_file = os.path.join(self.logs_dir, "sync_stdout.txt")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(summary)
        res = self.run_script("--title", "API3USDT: SL rejected", "--error", SIGNED_AMOUNTS_TEXT,
                              "--output-file", out_file)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.payload()
        body = data["body"]
        for amount in SUMMARY_AMOUNTS:
            self.assertNotIn(amount, body)
        self.assertEqual(data["title"], "API3USDT: SL rejected")
        for kept in ("**API3USDT**", "**1000SHIBUSDT**", "**BTCUSDT**", "**SOLUSDT**", "LONG_HEAVY",
                     "API3USDT and 1000SHIBUSDT rejected", "ROE -0.3% kept", "+8.4%",
                     "API3USDT LONG", "1000SHIBUSDT SHORT"):  # last two: telemetry table
            self.assertIn(kept, body)

    def test_non_422_gh_failure_queues_without_label_fallback(self):
        res = self.run_script("--title", "t", "--error", "e", STUB_POST_FAILS="502")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(len([c for c in self.calls() if c.startswith("api -X POST")]), 1)
        self.assertFalse(any(c.startswith("label create") for c in self.calls()))
        self.assertFalse(any(c.startswith("issue edit") for c in self.calls()))
        self.assertIn("HTTP 502", res.stdout)
        self.assertIn("Issue saved in local backlog", res.stdout)
        with open(os.path.join(self.logs_dir, "issues_backlog.jsonl"), encoding="utf-8") as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["title"], "t")
        self.assertIn("priority:P1", entry["labels"])

    def test_sync_injects_default_priority_into_legacy_entries(self):
        legacy = {"title": "legacy", "body": "b \"severity:high\" quoted in body",
                  "labels": ["agent-failure", "severity:medium", "cat:infra"]}
        with open(os.path.join(self.logs_dir, "issues_backlog.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy) + "\n")
        res = self.run_script("--sync")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.payload()
        self.assertEqual(data["labels"], ["agent-failure", "severity:medium", "priority:P2", "cat:infra"])
        self.assertEqual(data["body"], legacy["body"])

    def test_curl_path_warns_when_labels_missing(self):
        res = self.run_script("--title", "t", "--error", "e", GITHUB_TOKEN="test-token", STUB_AUTH_FAILS="1",
                              STUB_CURL_MODE="created_without_labels")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("GITHUB ISSUE CREATED SUCCESSFULLY: #9", res.stdout)
        self.assertIn("gh issue edit 9 --repo owner/repo --add-label severity:high,priority:P1", res.stdout)

    def test_curl_network_failure_falls_back_to_backlog(self):
        res = self.run_script("--title", "t", "--error", "e", GITHUB_TOKEN="test-token", STUB_AUTH_FAILS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("HTTP 000", res.stdout)
        self.assertIn("Issue saved in local backlog", res.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.stub_dir, "curl_calls.log")))

        res = self.run_script("--sync", GITHUB_TOKEN="test-token", STUB_AUTH_FAILS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("0 published, 1 remaining", res.stdout)
        with open(os.path.join(self.logs_dir, "issues_backlog.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len([l for l in f if l.strip()]), 1)
        self.assertEqual([n for n in os.listdir(self.logs_dir) if ".tmp." in n], [])  # mv ran

    def test_category_validated_and_normalized(self):
        res = self.run_script("--title", "t", "--error", "e", "--category", "tool error; rm -rf")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("invalid --category", res.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.stub_dir, "payload.json")))
        res = self.run_script("--title", "t", "--error", "e", "--category", "Risk_Gate")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.payload()
        self.assertEqual(data["labels"][-1], "cat:risk_gate")
        self.assertIn("| **Category** | `risk_gate` |", data["body"])

    def test_agent_name_is_sanitized(self):
        res = self.run_script("--title", "t", "--error", "e", "--agent", "bot ghp_abcdefghijklmnopqrstuvwxyz123")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        body = self.payload()["body"]
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123", body)
        self.assertIn("`bot [REDACTED_GH_TOKEN]`", body)

    def test_help_documents_new_flags(self):
        res = self.run_script("--help")
        self.assertEqual(res.returncode, 0)
        for flag in ("--priority", "--repro", "--root-cause", "--affected-files", "--context-file",
                     "--output-file", "--impact", "--acceptance-criteria", "CRITICAL->P0", "LOW->P3"):
            self.assertIn(flag, res.stdout)


if __name__ == "__main__":
    unittest.main()
