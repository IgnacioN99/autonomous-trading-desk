#!/usr/bin/env python3
"""
test_issue_307_worktree_file_writes.py - Issue #307: file-tool writes inside a registered issue worktree (a sibling
<main>-wt-issue-<N> linked to this repository) are allowed by pre_trade_guard.py without a prompt, except the
guard-defining files (WORKTREE_GUARD_DEFINING: the brake and the review path), which keep force_ask. The main
checkout keeps every decision it had; escapes (symlinks, '..', .git, unregistered lookalikes, other repositories,
.claude/worktrees, broken .git files, errors) keep today's decision; a worktree copy of the executor is no longer a
sanctioned risk-reducing script.

Hermetic: fake repositories and worktrees in temporary directories (no git binary, no network, no Binance client,
no .env, no writes to the real logs/).
"""

import os
import shutil
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "scripts"), os.path.join(REPO_ROOT, "scripts", "hooks"),
           os.path.join(REPO_ROOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pre_trade_guard  # noqa: E402
import test_guard_bypasses as tgb  # noqa: E402  (fixtures only)
import test_claude_code_support as tccs  # noqa: E402  (fixtures only)
import test_issue_49_powershell_notebookedit as t49  # noqa: E402  (fixtures only)
import test_issue_148_guard_worktree_paths as t148  # noqa: E402  (fixtures only)

PLAIN = "x = 1\n"
ENDPOINT = t148.ENDPOINT
PRIMITIVES = t148.PRIMITIVES
ALLOWED_IN_WORKTREE = ("scripts/execute_futures_trade.py", "scripts/trade_outcomes.py", "scripts/loops/x.py",
                       "scripts/utils/gate_limits.py", "scripts/dev/other_tool.py", "scripts/record_evaluation.py", "scripts/report_issue.sh",
                       "config/user_profile.json", "tests/test_x.py", "README.md", "AGENTS.md", "CLAUDE.md",
                       ".agents/rules/trading.md", ".agents/agents/issue_fixer/agent.md",
                       ".agents/skills/market-radar/SKILL.md", ".claude/agents/issue_fixer.md", "docs/new_file.md",
                       "logs/issue_work/fixer_report.md", "brand_new.py")
# Owner's list plus the hook's imports and the review / merge machinery (design.md decision 1)
EXPECTED_GUARD_DEFINING = (
    "scripts/hooks/", ".claude/settings.json", ".claude/settings.local.json", ".claude/settings.local.json.example",
    ".agents/hooks.json", "scripts/utils/dossier_provenance.py", "scripts/utils/trading_lease.py",
    "scripts/utils/env_resolver.py", "scripts/utils/score_calibration.py", "scripts/utils/calibration_fallback.py",
    "scripts/utils/atomic_writer.py", "scripts/utils/file_lock.py", "scripts/user_profile.py", "scripts/ci/",
    ".github/workflows/", "scripts/dev/issue_workspace.py", "scripts/dev/sync_claude_assets.py",
    ".agents/skills/pr-review/", ".agents/skills/issue-orchestrator/", ".claude/skills/pr-review/",
    ".claude/skills/issue-orchestrator/",
)
GROUND_TRUTH_LOGS = ("logs/session_state.json", "logs/pending_entries.json", "logs/guardian_state.json",
                     "logs/hook_heartbeat.json", "logs/trades_audit.jsonl", "logs/evaluations/latest_dossier.json")


def _symlink(src: str, dst: str, test: unittest.TestCase) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.symlink(src, dst)
    except (OSError, NotImplementedError):
        test.skipTest("symlinks unavailable")


def _guard_defining_targets():
    """One concrete worktree-relative path per WORKTREE_GUARD_DEFINING entry (a file inside each directory)."""
    return [e + "x.py" if e.endswith("/") else e for e in EXPECTED_GUARD_DEFINING]


class _Base(t148._Base):
    def at(self, root: str, rel: str) -> str:
        return os.path.join(root, *rel.split("/"))


class TestWorktreeAllowed(_Base):

    def test_worktree_files_are_allowed(self):
        for rel in ALLOWED_IN_WORKTREE:
            for content in (PLAIN, PRIMITIVES, ENDPOINT, ""):
                with self.subTest(rel=rel, content=content):
                    self.assertEqual(self.decision(self.at(self.wt, rel), content), "allow")

    def test_allow_reason_names_the_worktree_path(self):
        decision, reason = pre_trade_guard.evaluate_file_write(self.at(self.wt, "scripts/x.py"), PLAIN, self.repo)
        self.assertEqual(decision, "allow")
        self.assertIn("scripts/x.py", reason)

    def test_main_checkout_decisions_unchanged(self):
        pinned = {"scripts/execute_futures_trade.py": "force_ask", "scripts/trade_outcomes.py": "force_ask",
                  "scripts/record_evaluation.py": "force_ask", "config/user_profile.json": "force_ask",
                  ".agents/agents/issue_fixer/agent.md": "force_ask", "scripts/hooks/pre_trade_guard.py": "force_ask",
                  "tests/test_x.py": "ask", "README.md": "ask", "AGENTS.md": "ask", "CLAUDE.md": "ask",
                  ".agents/rules/trading.md": "ask", "logs/issue_work/fixer_report.md": "ask",
                  "scripts/ci/assemble_review.py": "ask", "brand_new.py": "ask"}
        for rel, expected in pinned.items():
            for target in (self.at(self.repo, rel), rel):
                with self.subTest(target=target):
                    self.assertEqual(self.decision(target, PLAIN), expected)
        # Content checks still apply in main
        self.assertEqual(self.decision(self.at(self.repo, "tests/test_x.py"), ENDPOINT), "force_ask")
        self.assertEqual(self.decision(self.at(self.repo, "brand_new.py"), PRIMITIVES), "force_ask")
        for rel in GROUND_TRUTH_LOGS:
            self.assertEqual(self.decision(self.at(self.repo, rel), "{}"), "deny", rel)

    def test_new_file_in_a_new_directory_is_allowed(self):
        self.assertEqual(self.decision(self.at(self.wt, "docs/brand/new/dir/file.md"), PLAIN), "allow")


class TestWorktreeGuardDefining(_Base):

    def test_constant_is_pinned(self):
        self.assertEqual(tuple(pre_trade_guard.WORKTREE_GUARD_DEFINING), EXPECTED_GUARD_DEFINING)

    def test_each_entry_force_asks_in_the_worktree(self):
        owner_named = ["scripts/hooks/pre_trade_guard.py", "scripts/hooks/post_trade_sync.py",
                       "scripts/hooks/issue_fixer_guard.py", ".claude/settings.json", ".claude/settings.local.json",
                       ".claude/settings.local.json.example", ".agents/hooks.json"]
        for rel in _guard_defining_targets() + owner_named:
            for content in (PLAIN, ENDPOINT, PRIMITIVES):
                with self.subTest(rel=rel, content=content):
                    decision, reason = pre_trade_guard.evaluate_file_write(self.at(self.wt, rel), content, self.repo)
                    self.assertEqual(decision, "force_ask")
                    self.assertIn("Explicit confirmation required", reason)

    def test_case_and_ntfs_alias_variants_force_ask(self):
        for rel in ("Scripts/Hooks/pre_trade_guard.py", "SCRIPTS/HOOKS/x.py", ".Claude/Settings.json",
                    ".AGENTS/hooks.JSON", "scripts/hooks./x.py", ".claude/settings.json::$DATA",
                    "scripts/hooks", ".github/Workflows/ci.yml"):
            with self.subTest(rel=rel):
                self.assertEqual(self.decision(self.at(self.wt, rel), PLAIN), "force_ask")

    def test_lookalike_names_are_not_guard_defining(self):
        for rel in ("scripts/hooks_notes.md", ".claude/settings.json.bak", "scripts/dev/other_tool.py",
                    ".agents/skills/pr-reviewer/SKILL.md"):
            with self.subTest(rel=rel):
                self.assertEqual(self.decision(self.at(self.wt, rel), PLAIN), "allow")


class TestWorktreeGroundTruthStillDenied(_Base):

    def test_ground_truth_names_denied_in_worktree(self):
        for rel in GROUND_TRUTH_LOGS:
            with self.subTest(rel=rel):
                self.assertEqual(self.decision(self.at(self.wt, rel), "{}"), "deny")

    def test_issue_work_and_other_logs_allowed(self):
        self.assertEqual(self.decision(self.at(self.wt, "logs/issue_work/design.md"), PLAIN), "allow")
        self.assertEqual(self.decision(self.at(self.wt, "logs/issue_work/review/diff.patch"), PLAIN), "allow")


class TestWorktreeEscapesFailClosed(_Base):

    def setUp(self):
        super().setUp()
        t148._write(self.at(self.repo, "scripts/hooks/pre_trade_guard.py"), PLAIN)
        t148._write(self.at(self.repo, "scripts/plain_tool.py"), PLAIN)
        t148._write(self.at(self.repo, "logs/session_state.json"), "{}")
        t148._write(self.at(self.repo, "logs/notes.txt"), "")
        os.makedirs(self.at(self.wt, "scripts"), exist_ok=True)

    def test_symlink_to_main_harness_file_force_asks(self):
        link = self.at(self.wt, "scripts/link.py")
        _symlink(self.at(self.repo, "scripts/hooks/pre_trade_guard.py"), link, self)
        self.assertEqual(self.decision(link, PLAIN), "force_ask")
        dir_link = self.at(self.wt, "scripts/mainhooks")
        _symlink(self.at(self.repo, "scripts/hooks"), dir_link, self)
        self.assertEqual(self.decision(os.path.join(dir_link, "new.py"), PLAIN), "force_ask")
        outside_link = os.path.join(self.tmp, "elsewhere", "guard.py")
        _symlink(self.at(self.repo, "scripts/hooks/pre_trade_guard.py"), outside_link, self)
        self.assertEqual(self.decision(outside_link, PLAIN), "force_ask")

    def test_symlink_to_main_plain_file_keeps_todays_ask(self):
        link = self.at(self.wt, "scripts/tool.py")
        _symlink(self.at(self.repo, "scripts/plain_tool.py"), link, self)
        self.assertEqual(self.decision(link, PLAIN), "ask")
        self.assertEqual(self.decision(link, PRIMITIVES), "force_ask")

    def test_symlink_into_main_logs(self):
        dir_link = self.at(self.wt, "logs/st")
        _symlink(self.at(self.repo, "logs"), dir_link, self)
        self.assertEqual(self.decision(os.path.join(dir_link, "session_state.json"), "{}"), "deny")
        self.assertEqual(self.decision(os.path.join(dir_link, "notes.txt"), PLAIN), "ask")
        file_link = self.at(self.wt, "logs/issue_work/state.json")
        _symlink(self.at(self.repo, "logs/session_state.json"), file_link, self)
        self.assertEqual(self.decision(file_link, "{}"), "deny")

    def test_dotdot_escapes(self):
        self.assertEqual(self.decision(self.wt + "/scripts/../../trading/scripts/hooks/x.py", PLAIN), "force_ask")
        self.assertEqual(self.decision(self.wt + "/../trading/README.md", PLAIN), "ask")
        self.assertEqual(self.decision(self.wt + "/../outside/x.py", PLAIN), "ask")
        self.assertEqual(self.decision(self.wt + "/scripts/../../outside/x.py", PRIMITIVES), "force_ask")

    def test_git_paths(self):
        for rel in (".git", ".git/hooks/pre-commit", ".GIT", ".git/config"):
            with self.subTest(rel=rel):
                self.assertEqual(self.decision(self.at(self.wt, rel), PLAIN), "force_ask")
        for rel in (".git/info/exclude", "sub/.git/x", ".git./x"):
            with self.subTest(rel=rel):
                self.assertNotEqual(self.decision(self.at(self.wt, rel), PLAIN), "allow")
                self.assertEqual(pre_trade_guard._issue_worktree_rel(self.at(self.wt, rel), self.repo), "")

    def test_unregistered_lookalike_directories(self):
        plain = os.path.join(self.tmp, "trading-wt-issue-9")
        os.makedirs(plain)
        self.assertEqual(self.decision(os.path.join(plain, "scripts", "x.py"), PLAIN), "ask")
        forged = os.path.join(self.tmp, "trading-wt-issue-10")
        os.makedirs(os.path.join(self.common, "worktrees", "ghost"))
        t148._write(os.path.join(forged, ".git"), f"gitdir: {os.path.join(self.common, 'worktrees', 'ghost')}\n")
        self.assertEqual(self.decision(os.path.join(forged, "scripts", "x.py"), PLAIN), "ask")
        borrowed = os.path.join(self.tmp, "trading-wt-issue-11")
        t148._write(os.path.join(borrowed, ".git"), f"gitdir: {os.path.join(self.common, 'worktrees', 'w1')}\n")
        self.assertEqual(self.decision(os.path.join(borrowed, "scripts", "x.py"), PLAIN), "ask")

    def test_linked_worktree_with_other_name_or_place(self):
        named = os.path.join(self.tmp, "trading-feature")
        t148._link_worktree(self.common, named, "w3")
        nested = os.path.join(self.tmp, "nested", "trading-wt-issue-3")
        t148._link_worktree(self.common, nested, "w4")
        suffixed = os.path.join(self.tmp, "trading-wt-issue-5x")
        t148._link_worktree(self.common, suffixed, "w5")
        prefixed = os.path.join(self.tmp, "xtrading-wt-issue-6")
        t148._link_worktree(self.common, prefixed, "w6")
        for root in (named, nested, suffixed, prefixed):
            with self.subTest(root=root):
                self.assertEqual(pre_trade_guard._linked_worktree_rel(os.path.join(root, "scripts", "x.py"),
                                                                      self.repo), "scripts/x.py")
                self.assertEqual(self.decision(os.path.join(root, "scripts", "x.py"), PLAIN), "ask")
                self.assertEqual(self.decision(os.path.join(root, "scripts", "x.py"), ENDPOINT), "force_ask")

    def test_another_repositorys_worktree_named_like_ours(self):
        other_common = t148._make_repo(os.path.join(self.tmp, "other"))
        other_wt = os.path.join(self.tmp, "trading-wt-issue-12")
        t148._link_worktree(other_common, other_wt, "w2")
        self.assertEqual(self.decision(os.path.join(other_wt, "scripts", "x.py"), PLAIN), "ask")

    def test_claude_worktrees_keep_todays_decision(self):
        claude_wt = self.at(self.repo, ".claude/worktrees/agent-1")
        t148._link_worktree(self.common, claude_wt, "agent-1")
        self.assertEqual(self.decision(os.path.join(claude_wt, "tests", "t.py"), PLAIN), "ask")
        self.assertEqual(self.decision(os.path.join(claude_wt, "scripts", "x.py"), ENDPOINT), "force_ask")
        self.assertEqual(pre_trade_guard._issue_worktree_rel(os.path.join(claude_wt, "tests", "t.py"), self.repo), "")

    def test_case_variant_of_the_worktree_path(self):
        variant = os.path.join(self.tmp, "TRADING-WT-ISSUE-1", "scripts", "x.py")
        if os.path.exists(os.path.join(self.tmp, "TRADING-WT-ISSUE-1")):
            self.skipTest("case-insensitive filesystem")
        self.assertNotEqual(self.decision(variant, PLAIN), "allow")

    def test_broken_git_files(self):
        admin = os.path.join(self.common, "worktrees", "w1")
        target = self.at(self.wt, "scripts/x.py")
        for text in ("not a pointer\n", "", f"gitdir: {admin}\n" + "#" * 5000, "gitdir: \n", "\xff\xfe"):
            with self.subTest(text=text[:20]):
                with open(os.path.join(self.wt, ".git"), "w", encoding="latin-1") as f:
                    f.write(text)
                self.assertEqual(self.decision(target, PLAIN), "ask")
        os.remove(os.path.join(self.wt, ".git"))
        os.makedirs(os.path.join(self.wt, ".git"))
        self.assertEqual(self.decision(target, PLAIN), "ask")

    def test_symlinked_git_file_is_not_registered(self):
        t148._write(os.path.join(self.tmp, "real.git"), f"gitdir: {os.path.join(self.common, 'worktrees', 'w1')}\n")
        os.remove(os.path.join(self.wt, ".git"))
        _symlink(os.path.join(self.tmp, "real.git"), os.path.join(self.wt, ".git"), self)
        self.assertEqual(self.decision(self.at(self.wt, "scripts/x.py"), PLAIN), "ask")

    def test_classification_errors_never_allow(self):
        target = self.at(self.wt, "scripts/x.py")
        for name in ("_linked_worktree_rel", "_host_path", "_strip_windows_aliases"):
            with self.subTest(name=name), patch.object(pre_trade_guard, name, side_effect=RuntimeError("boom")):
                self.assertEqual(pre_trade_guard._issue_worktree_rel(target, self.repo), "")
        with patch.object(pre_trade_guard, "_issue_worktree_rel", side_effect=RuntimeError("boom")):
            self.assertEqual(self.decision(target, PLAIN), "ask")
        with patch("os.path.realpath", side_effect=OSError("boom")):
            self.assertIsNone(pre_trade_guard._outside_main_file_write(target, PLAIN, self.repo))

    def test_hard_linked_target_keeps_todays_decision(self):
        linked = self.at(self.wt, "scripts/linked.py")
        try:
            os.link(self.at(self.repo, "scripts/hooks/pre_trade_guard.py"), linked)
        except (OSError, NotImplementedError):
            self.skipTest("hard links unavailable")
        self.assertEqual(self.decision(linked, PLAIN), "ask")
        self.assertEqual(self.decision(linked, ENDPOINT), "force_ask")
        single = self.at(self.wt, "scripts/single.py")
        t148._write(single, PLAIN)
        self.assertEqual(self.decision(single, PLAIN), "allow")
        real_stat = os.stat

        def failing_stat(path, *args, **kwargs):  # only the target's own stat fails (registration still works)
            if os.fspath(path) == single:
                raise OSError("boom")
            return real_stat(path, *args, **kwargs)
        with patch("os.stat", side_effect=failing_stat):
            self.assertEqual(pre_trade_guard._issue_worktree_rel(single, self.repo), "scripts/single.py")
            self.assertEqual(self.decision(single, PLAIN), "ask")

    def test_main_git_symlink_or_file_never_registers(self):
        target = self.at(self.wt, "scripts/x.py")
        moved = os.path.join(self.tmp, "moved.git")
        os.rename(self.common, moved)
        _symlink(moved, self.common, self)
        self.assertEqual(pre_trade_guard._issue_worktree_rel(target, self.repo), "")
        self.assertEqual(self.decision(target, PLAIN), "ask")


class TestHookLaunchedFromWorktree(_Base):

    def test_worktree_base_dir_keeps_todays_behaviour(self):
        self.assertEqual(self.decision(self.at(self.wt, "tests/t.py"), PLAIN, base_dir=self.wt), "ask")
        self.assertEqual(self.decision(self.at(self.wt, "scripts/execute_futures_trade.py"), PLAIN,
                                       base_dir=self.wt), "force_ask")
        self.assertEqual(self.decision(self.at(self.wt, "x.py"), PRIMITIVES, base_dir=self.wt), "force_ask")
        self.assertEqual(self.decision(self.at(self.repo, "scripts/hooks/pre_trade_guard.py"), PLAIN,
                                       base_dir=self.wt), "ask")
        wt2 = os.path.join(self.tmp, "trading-wt-issue-2")
        t148._link_worktree(self.common, wt2, "w2")
        self.assertEqual(self.decision(self.at(wt2, "scripts/x.py"), PLAIN, base_dir=self.wt), "ask")
        self.assertEqual(self.decision(self.at(wt2, "scripts/x.py"), PLAIN), "allow")


class TestWorktreePayloads(t49.PowerShellHarness):
    """The whole hook (Claude Code and agy payloads) with a registered sibling worktree of the harness root."""

    def setUp(self):
        super().setUp()
        common = os.path.join(self.root, ".git")
        os.makedirs(os.path.join(common, "worktrees"))
        self.wt = os.path.realpath(self.root) + "-wt-issue-7"
        self.addCleanup(shutil.rmtree, self.wt, True)
        t148._link_worktree(common, self.wt, "w7")

    def at(self, root: str, rel: str) -> str:
        return os.path.join(root, *rel.split("/"))

    def claude_tools(self, target: str, other: dict = None):
        other = other or {}
        return (("Write", dict({"file_path": target, "content": ENDPOINT}, **other)),
                ("Edit", dict({"file_path": target, "old_string": "a", "new_string": "/fapi/v1/order"}, **other)),
                ("MultiEdit", dict({"file_path": target, "edits": [{"old_string": "a", "new_string": ENDPOINT}]},
                                   **other)),
                ("NotebookEdit", dict({"notebook_path": target, "new_source": ENDPOINT, "cell_id": "c1",
                                       "cell_type": "code", "edit_mode": "replace"}, **other)))

    def test_claude_tools_allowed_in_worktree(self):
        for rel in ("scripts/execute_futures_trade.py", "tests/test_x.py", "notebooks/n.ipynb"):
            for tool, tool_input in self.claude_tools(self.at(self.wt, rel)):
                with self.subTest(rel=rel, tool=tool):
                    self.assertEqual(self.decision(self._claude(tool, tool_input)), "allow")

    def test_claude_tools_guard_defining_and_main_unchanged(self):
        for target, expected in ((self.at(self.wt, "scripts/hooks/pre_trade_guard.py"), "ask"),
                                 (self.at(self.wt, ".claude/settings.json"), "ask"),
                                 (self.at(self.root, "scripts/execute_futures_trade.py"), "ask"),
                                 (self.at(self.root, "tests/test_x.py"), "ask")):
            for tool, tool_input in self.claude_tools(target):
                with self.subTest(target=target, tool=tool):
                    self.assertEqual(self.decision(self._claude(tool, tool_input)), expected)
        # Main checkout without content checks firing: still Claude Code's own prompt (passthrough)
        res = self._claude("Write", {"file_path": self.at(self.root, "tests/test_x.py"), "content": PLAIN})
        self.assertEqual(self.decision(res), "passthrough")
        res = self._claude("Write", {"file_path": self.at(self.wt, "logs/session_state.json"), "content": "{}"})
        self.assertEqual(self.decision(res), "deny")

    def test_every_path_argument_must_be_allowed(self):
        allowed = self.at(self.wt, "tests/a.py")
        for other in (self.at(self.root, "scripts/hooks/x.ipynb"), self.at(self.wt, "scripts/hooks/x.ipynb"),
                      self.at(self.root, "tests/t.ipynb"), "/tmp/elsewhere/x.ipynb"):
            for tool_input in ({"file_path": allowed, "notebook_path": other, "new_source": "x"},
                               {"file_path": allowed, "path": other, "content": "x"},
                               {"file_path": allowed, "notebook_path": ["x"], "new_source": "x"}):
                with self.subTest(other=other, keys=sorted(tool_input)):
                    self.assertNotEqual(self.decision(self._claude("NotebookEdit", tool_input)), "allow")
        both = {"file_path": allowed, "notebook_path": self.at(self.wt, "tests/b.ipynb"), "new_source": "x"}
        self.assertEqual(self.decision(self._claude("NotebookEdit", both)), "allow")

    def test_agy_file_tools(self):
        for name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            res = self.agy({"toolCall": {"name": name, "args": {"TargetFile": self.at(self.wt, "scripts/x.py"),
                                                                "CodeContent": ENDPOINT, "EmptyFile": False}}})
            self.assertEqual(res.get("decision"), "allow", name)
            res = self.agy({"toolCall": {"name": name, "args": {
                "TargetFile": self.at(self.wt, ".agents/hooks.json"), "CodeContent": "{}"}}})
            self.assertEqual(res.get("decision"), "force_ask", name)

    def test_worktree_executor_copy_is_not_sanctioned(self):
        wt_exec = self.at(self.wt, "scripts/execute_futures_trade.py")
        for flags in ("--close-position --symbol BTCUSDT", "--auto-heal", "--audit-orphans",
                      "--move-breakeven --symbol BTCUSDT"):
            res = self.agy(self.cmd(f"python3 {wt_exec} {flags}"))
            self.assertEqual(res.get("decision"), "ask", flags)
            self.assertIn("is not the sanctioned repository script", res.get("reason", ""), flags)
            self.assertEqual(self.agy(self.cmd(f"python3 scripts/execute_futures_trade.py {flags}")).get("decision"),
                             "allow", flags)
            # Round 3: an interpreter named by a path (a worktree file, or any path) is not the bare interpreter
            for interpreter in (self.at(self.wt, "bin/python3"), "/usr/bin/python3"):
                line = f"{interpreter} scripts/execute_futures_trade.py {flags}"
                res = self.agy(self.cmd(line))
                self.assertEqual(res.get("decision"), "ask", line)
                self.assertIn("interpreter by a path", res.get("reason", ""), line)
                self.assertEqual(self.decision(self.bash(line)), "passthrough", line)
        self.assertIsNone(pre_trade_guard._sanctioned_script(wt_exec, self.root, self.root, False))
        self.assertEqual(pre_trade_guard._sanctioned_script(wt_exec, self.root, self.root, False, allow_worktree=True),
                         "scripts/execute_futures_trade.py")

    def test_worktree_executor_cannot_open_without_a_prompt(self):
        # Gates pass (dossier, lease, --confirmed): only WHICH executor file runs decides the allow
        tccs.TestGuardWithClaudeDossier.write_claude_dossier(self, extra={"is_yolo": True,
                                                                         "requires_user_confirmation": False})
        trade = "--symbol BTCUSDT --direction LONG --leverage 3 --env prod --confirmed"
        sanctioned = (f"python3 scripts/execute_futures_trade.py {trade}",
                      f"python3 {self.root}/scripts/execute_futures_trade.py {trade}",
                      f"cd {self.root} && python3 scripts/execute_futures_trade.py {trade}",
                      f"bash -lc 'cd {self.root} && python3 scripts/execute_futures_trade.py {trade}'")
        for line in sanctioned:
            self.assertEqual(self.decision(self.bash(line)), "allow", line)
        wt = self.wt
        drifted = (f"python3 {wt}/scripts/execute_futures_trade.py {trade}",
                   f"python3 {wt}/scripts/x/execute_futures_trade.py {trade}",
                   f"cd {wt} && python3 scripts/execute_futures_trade.py {trade}",
                   f"pushd {wt} && python3 scripts/execute_futures_trade.py {trade}",
                   f"cd {self.root} ; cd {wt} ; python3 scripts/execute_futures_trade.py {trade}",
                   f"bash -lc 'cd {wt} && python3 scripts/execute_futures_trade.py {trade}'",
                   f"env -C {wt} python3 scripts/execute_futures_trade.py {trade}",
                   f"cd - && python3 scripts/execute_futures_trade.py {trade}",
                   f"cd \"$WT\" && python3 scripts/execute_futures_trade.py {trade}")
        for line in drifted:
            with self.subTest(line=line):
                self.assertEqual(self.decision(self.bash(line)), "passthrough")
                with patch("pre_trade_guard.find_workspace_root", return_value=self.root):
                    decision, reason = pre_trade_guard._evaluate_shell_command(line, self.root, self.root, t49.SESSION,
                                                                               runtime="claude")
                self.assertEqual(decision, "ask")
                self.assertIn("not provably the sanctioned repository script", reason)
        for ps_line in (f"Set-Location {wt}; python scripts\\execute_futures_trade.py {trade}",
                        f"cd {wt}; python scripts\\execute_futures_trade.py {trade}",
                        f"python {wt}\\scripts\\execute_futures_trade.py {trade}"):
            self.assertNotEqual(self.decision(self.ps(ps_line)), "allow", ps_line)
        # Round 3: code loaded into the executor's process (PYTHONPATH / sitecustomize, an interpreter named by a
        # path, interpreter options, wrappers) or a cd redirected by CDPATH: the gates pass, the allow becomes an ask
        loaded = (f"PYTHONPATH={wt}/x python3 scripts/execute_futures_trade.py {trade}",
                  f"PYTHONSTARTUP={wt}/x.py python3 scripts/execute_futures_trade.py {trade}",
                  f"{wt}/bin/python3 scripts/execute_futures_trade.py {trade}",
                  f"/usr/bin/python3 scripts/execute_futures_trade.py {trade}",
                  f"python3 -W ignore scripts/execute_futures_trade.py {trade}",
                  f"sudo python3 scripts/execute_futures_trade.py {trade}",
                  f"CDPATH={wt} cd scripts/.. && python3 scripts/execute_futures_trade.py {trade}",
                  f"CDPATH={wt}; cd scripts/.. && python3 scripts/execute_futures_trade.py {trade}",
                  f"export CDPATH={wt}; cd scripts/.. && python3 scripts/execute_futures_trade.py {trade}",
                  f"bash -lc 'CDPATH={wt} cd scripts/.. && python3 scripts/execute_futures_trade.py {trade}'",
                  f"bash -lc 'PYTHONPATH={wt}/x python3 scripts/execute_futures_trade.py {trade}'")
        for line in loaded:
            with self.subTest(line=line):
                self.assertEqual(self.decision(self.bash(line)), "passthrough")
                with patch("pre_trade_guard.find_workspace_root", return_value=self.root):
                    decision, reason = pre_trade_guard._evaluate_shell_command(line, self.root, self.root, t49.SESSION,
                                                                               runtime="claude")
                self.assertEqual(decision, "ask")
                self.assertIn("not provably the sanctioned repository script", reason)
        for line in (f"exec python3 scripts/execute_futures_trade.py {trade}",
                     f"python3 -m scripts.execute_futures_trade {trade}",
                     f"cd ~/x && python3 scripts/execute_futures_trade.py {trade}",
                     f"popd && python3 scripts/execute_futures_trade.py {trade}"):
            self.assertNotEqual(self.decision(self.bash(line)), "allow", line)
        for ps_line in (f"sl {wt}; python scripts\\execute_futures_trade.py {trade}",
                        f"chdir {wt}; python scripts\\execute_futures_trade.py {trade}"):
            self.assertNotEqual(self.decision(self.ps(ps_line)), "allow", ps_line)
        still_allowed = (f"python3 -u scripts/execute_futures_trade.py {trade}",
                         f"BINANCE_API_ENV=prod python3 scripts/execute_futures_trade.py {trade}",
                         f"BINANCE_API_ENV=testnet python3 scripts/execute_futures_trade.py {trade}",
                         f"PYTHONUNBUFFERED=1 python3 scripts/execute_futures_trade.py {trade}",
                         f"wsl.exe -d Ubuntu -- python3 scripts/execute_futures_trade.py {trade}",
                         f"./scripts/execute_futures_trade.py {trade}")
        for line in still_allowed:
            self.assertEqual(self.decision(self.bash(line)), "allow", line)
        self.assertEqual(self.decision(self.ps(f"python scripts\\execute_futures_trade.py {trade}")), "allow")
        # The Bash tool's own cwd drifted into the worktree: the relative operand is the worktree copy
        main_line = sanctioned[0]
        res = self.run_guard({"session_id": t49.SESSION, "hook_event_name": "PreToolUse", "cwd": wt,
                              "tool_name": "Bash", "tool_input": {"command": main_line, "description": "t"}})
        self.assertEqual(self.decision(res), "passthrough")
        self.assertEqual(self.decision(self.bash(main_line)), "allow")


def setUpModule():
    tgb.setUpModule()


if __name__ == "__main__":
    unittest.main()
