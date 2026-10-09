"""
tests/test_ci_review_harness.py
Unit tests for the PR review harness: deterministic triage, reviewer subagent definitions,
the /pr-review skill, the review assembler, the verification gate and the agy review hooks.
Supports standard library unittest (zero extra dependencies) and pytest.
"""

import io
import os
import json
import shutil
import tempfile
import unittest
import subprocess
import sys
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from scripts.ci.triage_pr import triage, REVIEWERS, split_diff_by_file, write_review_context, assign_reviewer_files
from scripts.ci.verify_review import verify_review, check_review, reviewer_status, EXIT_INCOMPLETE, EXIT_CHANGES_REQUIRED
from scripts.ci import pr_review_state as state_mod
from scripts.ci import assemble_review
from scripts.ci.run_pr_audit import strip_frontmatter, build_orchestrator_prompt
from scripts.hooks import post_pr_review_hook as post_hook
from scripts.hooks import pr_review_stop_hook as stop_hook

REPO_ROOT = Path(__file__).resolve().parent.parent
ALL_REVIEWERS = {"agentic_harness", "binance_microstructure", "prompt_engineering", "trading_risk"}
READ_ONLY_TOOLS = {"view_file", "grep_search", "list_dir", "find_by_name", "send_message"}


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter parser for the subset used by agy agent definitions
    (scalars, folded '>-' blocks and '- item' lists)."""
    lines = text.splitlines()
    assert lines[0].strip() == "---", "frontmatter must start with ---"
    end = lines.index("---", 1)
    data, key = {}, None
    for line in lines[1:end]:
        if not line.strip():
            continue
        if line.startswith((" ", "\t")) and key is not None:
            stripped = line.strip()
            if stripped.startswith("- "):
                data[key] = (data[key] if isinstance(data[key], list) else []) + [stripped[2:].strip()]
            else:
                data[key] = (data[key] + " " if isinstance(data[key], str) and data[key] else "") + stripped
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        data[key] = "" if value in (">-", ">", "|") else value.strip('"')
        if value == "true":
            data[key] = True
        elif value == "false":
            data[key] = False
    return data


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
        self.assertEqual(set(manifest["required_reviewers"]), ALL_REVIEWERS)

    def test_triage_fail_closed_on_unclassified_file(self):
        files = ["scripts/unclassified_experimental_tool.py"]
        manifest = triage(files)
        self.assertTrue(manifest["fail_closed_triggered"])
        self.assertEqual(len(manifest["required_reviewers"]), 4)

    def test_triage_maps_reviewers_to_subagent_names(self):
        manifest = triage(["AGENTS.md"])
        for rev in ALL_REVIEWERS:
            details = manifest["reviewer_details"][rev]
            self.assertEqual(details["agent"], f"{rev}_reviewer")
            self.assertEqual(details["doc"], f".agents/agents/{rev}_reviewer/agent.md")
            self.assertTrue((REPO_ROOT / details["doc"]).is_file(), details["doc"])

    def test_triage_reviewer_file_assignment_is_fail_closed(self):
        files = ["scripts/execute_futures_trade.py", "docs/notion_setup_guide.md", "tests/test_new.py"]
        manifest = triage(files)
        assigned = manifest["reviewer_files"]
        # Unclassified file goes to every reviewer; domain files only to their owners
        for rev in ALL_REVIEWERS:
            self.assertIn("tests/test_new.py", assigned[rev])
        self.assertIn("scripts/execute_futures_trade.py", assigned["trading_risk"])
        self.assertNotIn("scripts/execute_futures_trade.py", assigned["prompt_engineering"])
        self.assertIn("docs/notion_setup_guide.md", assigned["prompt_engineering"])
        # Omnibus files go to everyone
        self.assertEqual(assign_reviewer_files(["AGENTS.md"], ["trading_risk"]), {"trading_risk": ["AGENTS.md"]})

    def test_review_context_split_and_index(self):
        diff = (
            "diff --git a/scripts/execute_futures_trade.py b/scripts/execute_futures_trade.py\n"
            "--- a/scripts/execute_futures_trade.py\n+++ b/scripts/execute_futures_trade.py\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/docs/x guide.md b/docs/x guide.md\n--- a/docs/x guide.md\n+++ b/docs/x guide.md\n@@ -1 +1 @@\n-c\n+d\n"
        )
        parts = split_diff_by_file(diff)
        self.assertEqual([p for p, _ in parts], ["scripts/execute_futures_trade.py", "docs/x guide.md"])
        manifest = triage(["scripts/execute_futures_trade.py", "docs/x guide.md"])
        with tempfile.TemporaryDirectory() as tmp:
            ctx = os.path.join(tmp, "pr_review")
            patches = write_review_context(manifest, diff, ctx)
            self.assertEqual(len(patches), 2)
            index = Path(ctx, "index.md").read_text(encoding="utf-8")
            self.assertIn("## Assigned to `trading_risk` (trading_risk_reviewer)", index)
            self.assertTrue(Path(ctx, "diff.patch").is_file())
            for name in os.listdir(os.path.join(ctx, "files")):
                self.assertTrue(name.endswith(".patch"))
                self.assertNotIn(" ", name)
            # Re-running recreates the directory (report.md allowed), but never wipes foreign files
            Path(ctx, "report.md").write_text("x", encoding="utf-8")
            write_review_context(manifest, diff, ctx)
            self.assertFalse(Path(ctx, "report.md").exists())
            Path(ctx, "user_notes.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(ValueError):
                write_review_context(manifest, diff, ctx)
            self.assertTrue(Path(ctx, "user_notes.txt").exists())

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
### Verdict: trading_risk
- **Status:** [APPROVED]
- **Summary:** All risk limits and R:R ratios respected.

### Verdict: binance_microstructure
- **Status:** [APPROVED]
- **Summary:** Precision stepSize and reduceOnly confirmed.

### Final Consolidated Verdict
- **Overall Status:** [APPROVED FOR MERGE]
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
### Verdict: trading_risk
- **Status:** [APPROVED]
"""
            report_file = tmp_path / "report.md"
            report_file.write_text(report, encoding="utf-8")

            success, msg = verify_review(str(manifest_file), str(report_file))
            self.assertFalse(success)
            self.assertIn("FATAL OMISSION DETECTED", msg)
            self.assertIn("prompt_engineering", msg)
            code, _, result = check_review(str(manifest_file), str(report_file))
            self.assertEqual(code, EXIT_INCOMPLETE)
            self.assertEqual(result["missing_ids"], ["prompt_engineering"])

    def test_verify_review_changes_requested(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            manifest = {
                "required_reviewers": ["trading_risk"]
            }
            manifest_file = tmp_path / "manifest.json"
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

            report = """
### Verdict: trading_risk
- **Status:** [CHANGES REQUIRED]
- **Summary:** Stop Loss is too wide and violates the profile risk cap.
"""
            report_file = tmp_path / "report.md"
            report_file.write_text(report, encoding="utf-8")

            success, msg = verify_review(str(manifest_file), str(report_file))
            self.assertFalse(success)
            self.assertIn("CHANGES REQUESTED", msg)
            self.assertEqual(check_review(str(manifest_file), str(report_file))[0], EXIT_CHANGES_REQUIRED)

    def test_verify_review_status_parsing_edge_cases(self):
        # Unfilled template is inconclusive
        self.assertIsNone(reviewer_status("### Verdict: trading_risk\n- **Status:** [APPROVED] | [CHANGES REQUIRED]\n",
                                          "trading_risk"))
        # The consolidated section must not leak into the last reviewer's section
        report = ("### Verdict: trading_risk\n- **Status:** [APPROVED]\n\n"
                  "### Final Consolidated Verdict\n- **Overall Status:** [CHANGES REQUIRED] elsewhere\n")
        self.assertEqual(reviewer_status(report, "trading_risk"), "APPROVED")
        # A reviewer id must not match a longer id
        self.assertEqual(reviewer_status("### Verdict: trading_risk_extra\n- **Status:** [APPROVED]\n",
                                         "trading_risk"), "")
        # Table fallback
        self.assertEqual(reviewer_status("| `agentic_harness` | CHANGES REQUIRED |", "agentic_harness"),
                         "CHANGES REQUIRED")


class TestReviewerSubagents(unittest.TestCase):

    def test_reviewer_agent_frontmatter_is_valid_and_read_only(self):
        self.assertFalse((REPO_ROOT / ".agents" / "reviewers").exists(), "legacy rubric folder must be gone")
        for rev, conf in REVIEWERS.items():
            path = REPO_ROOT / conf["doc"]
            text = path.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            self.assertEqual(fm["name"], conf["agent"], path)
            self.assertEqual(path.parent.name, conf["agent"])
            self.assertTrue(fm.get("description"), path)
            self.assertIn(conf["agent"], fm["description"])
            self.assertIsInstance(fm["tools"], list)
            self.assertTrue(set(fm["tools"]) <= READ_ONLY_TOOLS, f"{path}: {fm['tools']}")
            self.assertIn("send_message", fm["tools"])
            self.assertNotIn("run_command", fm["tools"])
            self.assertEqual(fm["commandExecutionPolicy"], "off")
            self.assertIs(fm["mainAgent"], False)
            self.assertIs(fm["subagent"], True)
            self.assertEqual(fm["model"], "inherit")
            # The output contract must match what verify_review.py parses
            body = strip_frontmatter(text)
            self.assertFalse(body.startswith("---"))
            self.assertIn(f"### Verdict: {rev}", body)
            self.assertIn("[APPROVED]", body)
            self.assertIn("[CHANGES REQUIRED]", body)
            self.assertIn("<output_contract>", body)

    def test_pr_review_skill_references_every_reviewer(self):
        path = REPO_ROOT / ".agents" / "skills" / "pr-review" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        self.assertEqual(fm["name"], "pr-review")
        self.assertTrue(fm["description"])
        for rev, conf in REVIEWERS.items():
            self.assertIn(f"`{rev}`", text)
            self.assertIn(f"`{conf['agent']}`", text)
        for needle in ("invoke_subagent", "scripts/ci/triage_pr.py", "scripts/ci/assemble_review.py",
                       "scripts/ci/verify_review.py", "gh pr comment", "logs/pr_review/report.md",
                       "pr_review_state.py"):
            self.assertIn(needle, text)
        # Skill must be committed (not swallowed by the .agents/skills/* ignore rule)
        res = subprocess.run(["git", "check-ignore", "-q", str(path.relative_to(REPO_ROOT).as_posix())],
                             cwd=REPO_ROOT, capture_output=True)
        self.assertNotEqual(res.returncode, 0, "SKILL.md is gitignored")

    def test_headless_fallback_prompt_uses_agent_bodies(self):
        manifest = triage(["AGENTS.md"])
        prompt = build_orchestrator_prompt(manifest, "diff --git a/x b/x\n")
        self.assertNotIn("mainAgent: false", prompt)  # frontmatter stripped
        self.assertNotIn("audit compliance with AGENTS.md within this domain", prompt)  # no placeholder rubric
        for rev in ALL_REVIEWERS:
            self.assertIn(f"### Verdict: {rev}", prompt)
        self.assertIn("### Final Consolidated Verdict", prompt)


class TestReviewAssembler(unittest.TestCase):

    def _write_transcript(self, brain: Path, conv_id: str, message: str) -> None:
        logs = brain / conv_id / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        steps = [
            {"source": "SYSTEM", "type": "USER_INPUT", "content": "sender=aaaaaaaa-0000-0000-0000-000000000000"},
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "",
             "tool_calls": [{"name": "send_message", "args": {"Message": json.dumps(message)}}]},
        ]
        (logs / "transcript.jsonl").write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")

    def test_assemble_from_subagent_transcripts(self):
        manifest = {"required_reviewers": ["agentic_harness", "trading_risk"], "changed_files": ["a.py"],
                    "base_ref": "origin/main", "head_sha": "abc", "fail_closed_triggered": True}
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            self._write_transcript(brain, "11111111-aaaa-bbbb-cccc-000000000001",
                                   "### Verdict: agentic_harness\n- **Status:** [APPROVED]\n- **Findings:** ok")
            self._write_transcript(brain, "11111111-aaaa-bbbb-cccc-000000000002",
                                   "### Verdict: trading_risk\n- **Status:** [CHANGES REQUIRED]\n- 🔴 x.py:1")
            with mock.patch.dict(os.environ, {"AGY_BRAIN_DIRS": str(brain)}):
                sections, provenance, errors = assemble_review.collect_sections(
                    manifest["required_reviewers"],
                    {"agentic_harness": "11111111-aaaa-bbbb-cccc-000000000001",
                     # wrong mapping: this transcript holds the agentic_harness verdict, not trading_risk
                     "trading_risk": "11111111-aaaa-bbbb-cccc-000000000001"},
                    {},
                )
                self.assertIn("agentic_harness", sections)
                self.assertNotIn("trading_risk", sections)
                self.assertTrue(errors)

                sections, provenance, errors = assemble_review.collect_sections(
                    manifest["required_reviewers"],
                    {"agentic_harness": "11111111-aaaa-bbbb-cccc-000000000001",
                     "trading_risk": "11111111-aaaa-bbbb-cccc-000000000002"},
                    {},
                )
            self.assertEqual(errors, [])
            report, result = assemble_review.build_report(manifest, sections, provenance, pr="13")
            self.assertIn(assemble_review.OVERALL_BLOCKED, report)
            self.assertIn("- **PR:** #13", report)
            self.assertEqual(result["rejected"], ["trading_risk"])

            manifest_file = Path(tmp) / "manifest.json"
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
            report_file = Path(tmp) / "report.md"
            report_file.write_text(report, encoding="utf-8")
            code, _, gate = check_review(str(manifest_file), str(report_file))
            self.assertEqual(code, EXIT_CHANGES_REQUIRED)
            self.assertEqual(gate["approved"], ["agentic_harness"])

    def test_truncated_agy_verdict_recovered_from_full_transcript(self):
        """agy truncates long send_message args in transcript.jsonl; the verdict comes from transcript_full.jsonl."""
        conv = "11111111-aaaa-bbbb-cccc-000000000003"
        message = ("Reviewed the diff file by file — ningún hallazgo crítico.\n" * 40 +
                   "### Verdict: agentic_harness\n- **Status:** [APPROVED]\n- **Findings:** ok")
        encoded = json.dumps(message, ensure_ascii=False)
        prefix = encoded[:80]
        removed = len(encoded.encode("utf-8")) - len(prefix.encode("utf-8"))
        system = {"step_index": 0, "source": "SYSTEM", "type": "USER_INPUT", "status": "DONE",
                  "created_at": "2026-10-05T00:00:00Z", "content": "sender=aaaaaaaa-0000-0000-0000-000000000000"}
        short = {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                 "created_at": "2026-10-05T00:00:05Z", "content": "",
                 "tool_calls": [{"name": "send_message", "args": {"Message": f"{prefix}\n<truncated {removed} bytes>"}}],
                 "truncated_fields": ["tool_calls"]}
        full = dict(short, tool_calls=[{"name": "send_message", "args": {"Message": message}}])
        del full["truncated_fields"]
        manifest = ["agentic_harness"]
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            logs = brain / conv / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            (logs / "transcript.jsonl").write_text(
                "\n".join(json.dumps(r) for r in (system, short)) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGY_BRAIN_DIRS": str(brain)}):
                # Without the full transcript the truncated verdict cannot be read: fail closed
                sections, _, errors = assemble_review.collect_sections(manifest, {"agentic_harness": conv}, {})
                self.assertEqual(sections, {})
                self.assertIn("transcript_full.jsonl", errors[0])

                (logs / "transcript_full.jsonl").write_text(
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in (system, full)) + "\n", encoding="utf-8")
                sections, provenance, errors = assemble_review.collect_sections(manifest, {"agentic_harness": conv}, {})
                self.assertEqual(errors, [])
                self.assertIn("[APPROVED]", sections["agentic_harness"])
                self.assertEqual(provenance["agentic_harness"], f"subagent {conv}")

                # A full row that does not extend the kept prefix is rejected
                forged = dict(full, tool_calls=[{"name": "send_message", "args": {"Message": "X" + message[1:]}}])
                (logs / "transcript_full.jsonl").write_text(
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in (system, forged)) + "\n", encoding="utf-8")
                sections, _, errors = assemble_review.collect_sections(manifest, {"agentic_harness": conv}, {})
                self.assertEqual(sections, {})
                self.assertIn("mismatch", errors[0])

    def test_malformed_agy_rows_reported_not_raised(self):
        """Issue #43: malformed rows (non-list tool_calls, deep nesting) become reviewer errors, never a traceback."""
        conv = "11111111-aaaa-bbbb-cccc-000000000004"
        system = {"step_index": 0, "source": "SYSTEM", "type": "USER_INPUT", "status": "DONE",
                  "created_at": "2026-10-05T00:00:00Z", "content": "sender=aaaaaaaa-0000-0000-0000-000000000000"}
        model = {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                 "created_at": "2026-10-05T00:00:05Z", "content": "", "tool_calls": "send_message"}
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            logs = brain / conv / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"AGY_BRAIN_DIRS": str(brain)}):
                for label, last_line in (("non-list tool_calls", json.dumps(model)),
                                         ("deep nesting", "[" * 100000 + "]" * 100000)):
                    with self.subTest(label):
                        (logs / "transcript.jsonl").write_text(json.dumps(system) + "\n" + last_line + "\n",
                                                               encoding="utf-8")
                        sections, _, errors = assemble_review.collect_sections(
                            ["agentic_harness"], {"agentic_harness": conv}, {})
                        self.assertEqual(sections, {})
                        self.assertEqual(len(errors), 1)
                        self.assertTrue(errors[0].startswith("agentic_harness: "), errors[0])

    def test_marker_quoted_before_the_verdict_row_is_not_an_error(self):
        """Issue #224: a reviewer quoting '\\n<truncated 5 bytes>' before its final verdict row still assembles;
        the same quote after the verdict row is still reported as an error."""
        conv = "11111111-aaaa-bbbb-cccc-000000000005"
        system = {"step_index": 0, "source": "SYSTEM", "type": "USER_INPUT", "status": "DONE",
                  "created_at": "2026-10-05T00:00:00Z", "content": "sender=aaaaaaaa-0000-0000-0000-000000000000"}
        quote = {"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                 "created_at": "2026-10-05T00:00:03Z",
                 "content": "The diff writes 'head\n<truncated 5 bytes>\ntail' for a cut row."}
        verdict = {"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                   "created_at": "2026-10-05T00:00:05Z", "content": "",
                   "tool_calls": [{"name": "send_message", "args": {"Message": json.dumps(
                       "### Verdict: agentic_harness\n- **Status:** [APPROVED]\n- **Findings:** ok")}}]}
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            logs = brain / conv / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"AGY_BRAIN_DIRS": str(brain)}):
                for label, model_rows, ok in (("quote before verdict", (quote, verdict), True),
                                              ("quote after verdict", (verdict, quote), False)):
                    with self.subTest(label):
                        rows = [system] + [dict(r, step_index=i + 1) for i, r in enumerate(model_rows)]
                        (logs / "transcript.jsonl").write_text(
                            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
                        sections, _, errors = assemble_review.collect_sections(
                            ["agentic_harness"], {"agentic_harness": conv}, {})
                        if ok:
                            self.assertEqual(errors, [])
                            self.assertIn("[APPROVED]", sections["agentic_harness"])
                        else:
                            self.assertEqual(sections, {})
                            self.assertEqual(len(errors), 1)
                            self.assertIn("no truncated_fields", errors[0])

    def test_assemble_marks_missing_reviewers_incomplete(self):
        manifest = {"required_reviewers": ["prompt_engineering"], "changed_files": ["docs/a.md"]}
        report, result = assemble_review.build_report(manifest, {}, {}, pr="")
        self.assertIn(assemble_review.OVERALL_INCOMPLETE, report)
        self.assertEqual(result["missing_ids"], ["prompt_engineering"])


class TestPRReviewHooks(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp, "logs", "pr_review_state.json")
        self.env = mock.patch.dict(os.environ, {state_mod.STATE_ENV: self.state_file})
        self.env.start()
        # Hermetic git: the branch never depends on the checkout the suite runs in
        self.checkout_branches = {}  # directory -> `rev-parse --abbrev-ref HEAD` (default fix/checkout)
        self.git_calls = []
        self.git = mock.patch.object(post_hook, "_git", side_effect=self._fake_git)
        self.git.start()

    def tearDown(self):
        self.git.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_git(self, args, directory=None):
        self.git_calls.append((list(args), directory))
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return self.checkout_branches.get(directory, "fix/checkout")
        if args == ["rev-parse", "HEAD"]:
            return f"sha-head-{directory}"
        return ""  # `rev-parse --verify --quiet <branch>`: no such local branch

    def _post(self, command: str, conv: str = "conv-parent", error: str = "", cwd: str = "") -> str:
        payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": command}}, "conversationId": conv}
        if cwd:
            payload["toolCall"]["args"]["Cwd"] = cwd
        if error:
            payload["error"] = error
        with redirect_stderr(io.StringIO()):
            return post_hook.handle_post_tool_use(payload)

    def _stop(self, conv: str = "conv-parent", **extra) -> dict:
        payload = dict({"conversationId": conv, "executionNum": 1, "terminationReason": "model_stop",
                        "fullyIdle": True}, **extra)
        with redirect_stderr(io.StringIO()):
            return stop_hook.decide(payload)

    def test_hook_is_pr_creation_or_push(self):
        is_pr_creation_or_push = post_hook.is_pr_creation_or_push
        # True cases
        self.assertTrue(is_pr_creation_or_push("gh pr create --title 'feat'"))
        self.assertTrue(is_pr_creation_or_push("git push -u origin feat/multi-agent-pr-review"))
        self.assertTrue(is_pr_creation_or_push("git push origin fix/some-bug"))

        # False cases
        self.assertFalse(is_pr_creation_or_push("git push origin main"))
        self.assertFalse(is_pr_creation_or_push("git status"))
        self.assertFalse(is_pr_creation_or_push("python3 scripts/ci/run_pr_audit.py"))
        self.assertFalse(is_pr_creation_or_push("python3 scripts/ci/pr_review_state.py done --reason no_pr"))
        self.assertFalse(is_pr_creation_or_push(""))

    # Issue #115: only executed sub-commands arm, never quoted text or heredoc bodies
    NOT_ARMING = (
        "for c in 'git push origin feat/x'; do echo $c; done",
        "python3 - <<'EOF'\nimport os\n# git push origin x\nEOF",
        "cat > notes.md <<EOF\ngit push -u origin fix/x\ngh pr create\nEOF",
        'echo "gh pr create"',
        "git push origin main",
        "git push origin HEAD:main",
        'grep -n "git push" README.md',
        "git push origin 'fix/x",  # unbalanced quote: cannot tokenize -> never arms
    )
    ARMING = (
        "git push -u origin fix/x",
        "gh pr create --title t --body-file b.md",
        "cd /wt && git push -u origin fix/x",
        "git -C /wt push origin fix/x",
        "git push origin HEAD:fix/x",
        "git push origin +fix/x",
        "env GIT_TRACE=0 git push origin fix/x",
        "bash -c 'git push origin fix/x'",
        "gh pr create --body \"$(cat <<'EOF'\nbody mentioning git push origin main\nEOF\n)\"",
        "git push origin fix/x; Write-Output ok",  # PowerShell-style separator
    )

    def test_issue_115_mentions_do_not_arm(self):
        for command in self.NOT_ARMING:
            with self.subTest(command=command):
                self.assertFalse(post_hook.is_pr_creation_or_push(command))
                self.assertEqual(self._post(command), "ignored")
                self.assertEqual(state_mod.load_state(), {})

    def test_issue_115_executed_push_or_pr_creation_arms(self):
        for command in self.ARMING:
            with self.subTest(command=command):
                self.assertTrue(post_hook.is_pr_creation_or_push(command))
                self.assertEqual(self._post(command), "pending")
                self.assertEqual(state_mod.load_state()["trigger_command"], command[:300])

    def test_issue_115_unparsable_command_logs_detect_error(self):
        os.makedirs(os.path.dirname(self.state_file))  # the events log lives next to the state file
        self.assertEqual(self._post("git push origin 'fix/x"), "ignored")
        events = [json.loads(e) for e in Path(state_mod.events_path()).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events], ["detect_error"])
        self.assertIn("quotation", events[0]["error"])

    def test_issue_115_branch_and_directory_from_the_command(self):
        find = post_hook.find_review_trigger
        self.assertEqual(find("git push -u origin fix/x", "/start").branch, "fix/x")
        self.assertEqual(find("git push origin HEAD:fix/x", "/start").branch, "fix/x")
        self.assertEqual(find("git push origin +refs/heads/fix/x", "/start").branch, "fix/x")
        self.assertEqual(find("git push --force origin fix/x", "/start").branch, "fix/x")
        self.assertEqual(find("gh pr create --base main --head fix/y", "/start").branch, "fix/y")
        self.assertEqual(find("gh pr create -H owner:fix/y", "/start").branch, "fix/y")
        self.assertEqual(find("gh pr create --head=fix/y", "/start").branch, "fix/y")
        # Expansions are unknown from the text: resolved from the directory's checkout instead
        self.assertEqual(find('git push -u origin "$BRANCH"', "/start").branch, "")
        self.assertEqual(find("git push -u origin $(git branch --show-current)", "/start").branch, "")
        self.assertEqual(find("git push origin `git branch --show-current`", "/start").branch, "")
        self.assertEqual(find('gh pr create --head "$B"', "/start").branch, "")
        self.assertEqual(find("gh pr create --fill", "/start"), post_hook.ReviewTrigger("pr_create", "", "/start"))
        self.assertEqual(find("cd /wt && git push", "/start"), post_hook.ReviewTrigger("push", "", "/wt"))
        self.assertEqual(find("cd /wt && cd sub && git push", "/start").directory, os.path.normpath("/wt/sub"))
        self.assertEqual(find("cd $WT && git push", "/start").directory, "/start")  # unknown -> start dir
        self.assertEqual(find("git -C /wt push origin fix/x", "/start").directory, "/wt")
        # Several pushes/PRs: the last one decides
        self.assertEqual(find("git push origin fix/x && gh pr create --fill", "/start").kind, "pr_create")
        self.assertFalse(post_hook.is_pr_creation_or_push("git push origin fix/x && git push origin main"))
        self.assertIsNone(find("git status && echo pushed", "/start"))

    def test_issue_115_branch_resolution_uses_the_command_directory(self):
        self.checkout_branches["/wt"] = "fix/from-worktree"
        trigger = post_hook.find_review_trigger("cd /wt && git push", "/start")
        self.assertEqual(post_hook.resolve_branch_and_sha(trigger), ("fix/from-worktree", "sha-head-/wt"))
        self.assertIn((["rev-parse", "--abbrev-ref", "HEAD"], "/wt"), self.git_calls)
        trigger = post_hook.find_review_trigger("git push origin HEAD:fix/x", "/start")
        self.assertEqual(post_hook.resolve_branch_and_sha(trigger), ("fix/x", "sha-head-/start"))
        # Detached HEAD: unresolved branch still arms, recorded as ""
        self.checkout_branches["/wt"] = "HEAD"
        self.assertEqual(self._post("cd /wt && git push"), "pending")
        self.assertEqual(state_mod.load_state()["branch"], "")

    def test_issue_115_pr_creation_on_main_without_head_does_not_arm(self):
        self.checkout_branches[self.tmp] = "main"
        self.assertEqual(self._post("gh pr create --fill", cwd=self.tmp), "ignored")
        self.assertEqual(state_mod.load_state(), {})
        self.assertEqual(self._post("gh pr create --fill --base main --head fix/x", cwd=self.tmp), "pending")
        self.assertEqual(state_mod.load_state()["branch"], "fix/x")

    def test_issue_115_worktree_push_records_the_worktree_branch(self):
        self.checkout_branches[self.tmp] = "main"  # the session runs in the main checkout
        self.assertEqual(self._post("cd /wt && git push -u origin fix/x", cwd=self.tmp), "pending")
        state = state_mod.load_state()
        self.assertEqual((state["branch"], state["head_sha"]), ("fix/x", "sha-head-/wt"))

    def test_review_post_detection(self):
        self.assertTrue(post_hook.is_review_post("gh pr comment 13 --body-file logs/pr_review/report.md"))
        self.assertTrue(post_hook.is_review_post("gh pr comment 13 --body-file=./logs/pr_review/report.md"))
        self.assertFalse(post_hook.is_review_post("gh pr comment 13 --body 'lgtm'"))
        self.assertFalse(post_hook.is_review_post("cat logs/pr_review/report.md"))

    def test_no_pending_review_never_blocks_stop(self):
        self.assertEqual(self._stop(), {"decision": "stop"})
        self.assertEqual(self._post("git status"), "ignored")
        self.assertEqual(self._stop(), {"decision": "stop"})

    def test_failed_pr_creation_does_not_arm_review(self):
        self.assertEqual(self._post("gh pr create --fill", error="exit status 1"), "ignored")
        self.assertEqual(state_mod.load_state(), {})

    def test_pending_review_continues_until_cap(self):
        self.assertEqual(self._post("gh pr create --fill"), "pending")
        state = state_mod.load_state()
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["conversation_id"], "conv-parent")

        for attempt in range(1, state_mod.MAX_STOP_ATTEMPTS + 1):
            out = self._stop()
            self.assertEqual(out["decision"], "continue")
            self.assertIn("/pr-review", out["reason"])
            self.assertIn(f"{attempt}/{state_mod.MAX_STOP_ATTEMPTS}", out["reason"])
        # Cap reached: stop allowed and marker abandoned (never loops forever)
        self.assertEqual(self._stop(), {"decision": "stop"})
        self.assertEqual(state_mod.load_state()["status"], "abandoned")
        self.assertEqual(self._stop(), {"decision": "stop"})

    def test_stop_allowed_for_other_conversations_errors_and_busy_sessions(self):
        self._post("git push -u origin feat/x")
        self.assertEqual(self._stop(conv="reviewer-subagent"), {"decision": "stop"})
        self.assertEqual(self._stop(terminationReason="error", error="boom"), {"decision": "stop"})
        self.assertEqual(self._stop(fullyIdle=False), {"decision": "stop"})
        self.assertEqual(state_mod.load_state()["stop_attempts"], 0)

    def test_in_progress_review_waits_for_reviewers(self):
        self._post("gh pr create --fill")
        self.assertIsNotNone(state_mod.mark_started())
        self.assertEqual(self._stop(), {"decision": "stop"})
        # A stale in-progress review is re-prompted
        state = state_mod.load_state()
        state["started_ts"] -= state_mod.IN_PROGRESS_GRACE_S + 1
        state_mod.save_state(state)
        self.assertEqual(self._stop()["decision"], "continue")

    def test_review_posted_clears_marker(self):
        self._post("gh pr create --fill")
        self.assertEqual(self._post("gh pr comment 13 --body-file logs/pr_review/report.md", error="exit status 1"),
                         "ignored")
        self.assertEqual(state_mod.load_state()["status"], "pending")
        self.assertEqual(self._post("gh pr comment 13 --body-file logs/pr_review/report.md"), "done")
        self.assertEqual(state_mod.load_state()["status"], "done")
        self.assertEqual(self._stop(), {"decision": "stop"})
        events = Path(state_mod.events_path()).read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(e)["event"] for e in events], ["review_pending", "review_posted"])

    def test_manual_done_and_start_without_marker(self):
        self.assertIsNone(state_mod.mark_started())
        self.assertIsNone(state_mod.mark_done("declined"))
        self._post("gh pr create --fill")
        self.assertEqual(state_mod.mark_done("no_pr")["done_reason"], "no_pr")
        self.assertEqual(self._stop(), {"decision": "stop"})

    def _run_hook_agy_style(self, script: str, payload) -> subprocess.CompletedProcess:
        """Runs a hook the way agy does: cwd = .agents/, `sh -c` command from hooks.json, JSON on stdin."""
        hooks = json.loads((REPO_ROOT / ".agents" / "hooks.json").read_text(encoding="utf-8"))
        commands = [h["command"] for group in hooks["pr-review-trigger"].get("PostToolUse", [])
                    for h in group["hooks"]] + [h["command"] for h in hooks["pr-review-trigger"].get("Stop", [])]
        command = next(c for c in commands if script in c)
        command = command.replace("python3", sys.executable, 1)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(["sh", "-c", command], cwd=REPO_ROOT / ".agents", input=stdin,
                              capture_output=True, text=True, timeout=20, env=dict(os.environ))

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh required")
    def test_hooks_json_wiring_agy_style(self):
        hooks = json.loads((REPO_ROOT / ".agents" / "hooks.json").read_text(encoding="utf-8"))
        trigger = hooks["pr-review-trigger"]
        self.assertTrue(trigger["enabled"])
        self.assertEqual(trigger["PostToolUse"][0]["matcher"], "run_command")
        stop_cmds = [h["command"] for h in trigger["Stop"]]
        self.assertEqual(stop_cmds, ["python3 ../scripts/hooks/pr_review_stop_hook.py"])
        self.assertNotIn("run_pr_audit", json.dumps(hooks))
        for cmd in stop_cmds + [h["command"] for h in trigger["PostToolUse"][0]["hooks"]]:
            self.assertTrue(cmd.startswith("python3 ../scripts/hooks/"), cmd)

        for raw in ("", "not json", "[]"):
            res = self._run_hook_agy_style("pr_review_stop_hook", raw)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(json.loads(res.stdout), {"decision": "stop"})
            res = self._run_hook_agy_style("post_pr_review_hook", raw)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(json.loads(res.stdout), {})

        # --head keeps the subprocess run independent of the checkout's branch (main never arms, #115)
        res = self._run_hook_agy_style("post_pr_review_hook", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "gh pr create --fill --head feat/x"}},
            "conversationId": "conv-parent", "stepIdx": 4})
        self.assertEqual((res.returncode, json.loads(res.stdout)), (0, {}))
        res = self._run_hook_agy_style("pr_review_stop_hook", {
            "conversationId": "conv-parent", "executionNum": 1, "terminationReason": "model_stop", "fullyIdle": True})
        self.assertEqual(res.returncode, 0)
        self.assertEqual(json.loads(res.stdout)["decision"], "continue")


if __name__ == "__main__":
    unittest.main()
