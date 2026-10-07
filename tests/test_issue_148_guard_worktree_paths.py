#!/usr/bin/env python3
"""
test_issue_148_guard_worktree_paths.py - Issue #148: file-tool writes inside a linked git worktree of the same
repository (a sibling <repo>-wt-issue-<N>) were judged as outside scripts/ and tests/ and got a false force_ask.

Hermetic: fake repositories and worktrees are built in temporary directories (no git binary, no network, no
writes to the real logs/).
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
for _p in (SCRIPTS_DIR, HOOKS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pre_trade_guard  # noqa: E402

PRIMITIVES = "from execute_futures_trade import x\nsend_signed_request('GET', '/fapi/v2/account', {})\n"
ENDPOINT = "fake.calls.append('/fapi/v1/order')\n"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _make_repo(root: str) -> str:
    """A fake main checkout with a real .git/ directory; returns its common git dir."""
    git_dir = os.path.join(root, ".git")
    os.makedirs(os.path.join(git_dir, "worktrees"))
    _write(os.path.join(git_dir, "HEAD"), "ref: refs/heads/main\n")
    return git_dir


def _link_worktree(common: str, wt_root: str, name: str) -> str:
    """Registers wt_root as linked worktree <name> of the repository whose common dir is `common`."""
    admin = os.path.join(common, "worktrees", name)
    os.makedirs(admin, exist_ok=True)
    _write(os.path.join(admin, "gitdir"), os.path.join(wt_root, ".git") + "\n")
    _write(os.path.join(admin, "commondir"), "../..\n")
    _write(os.path.join(wt_root, ".git"), f"gitdir: {admin}\n")
    return admin


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.tmp, "trading")
        self.common = _make_repo(self.repo)
        self.wt = os.path.join(self.tmp, "trading-wt-issue-1")
        _link_worktree(self.common, self.wt, "w1")

    def decision(self, target: str, content: str = PRIMITIVES, base_dir: str = "") -> str:
        return pre_trade_guard.evaluate_file_write(target, content, base_dir or self.repo)[0]


class TestLinkedWorktreeFileWrites(_Base):
    def test_worktree_tests_and_scripts_are_in_scope(self):
        for rel in ("tests/test_x.py", "scripts/x.py", "scripts/utils/y.py"):
            with self.subTest(rel=rel):
                self.assertEqual(self.decision(os.path.join(self.wt, *rel.split("/"))), "ask")

    def test_worktree_root_file_still_force_ask(self):
        self.assertEqual(self.decision(os.path.join(self.wt, "x.py")), "force_ask")
        self.assertEqual(self.decision(os.path.join(self.wt, "docs", "x.py")), "force_ask")

    def test_forged_git_file_without_back_pointer_force_ask(self):
        forged = os.path.join(self.tmp, "forged")
        _write(os.path.join(forged, ".git"), f"gitdir: {os.path.join(self.common, 'worktrees', 'w1')}\n")
        self.assertEqual(self.decision(os.path.join(forged, "tests", "t.py")), "force_ask")

    def test_forged_git_file_pointing_at_unregistered_worktree_dir_force_ask(self):
        forged = os.path.join(self.tmp, "forged2")
        os.makedirs(os.path.join(self.common, "worktrees", "ghost"))
        _write(os.path.join(forged, ".git"), f"gitdir: {os.path.join(self.common, 'worktrees', 'ghost')}\n")
        self.assertEqual(self.decision(os.path.join(forged, "tests", "t.py")), "force_ask")

    def test_worktree_of_another_repository_force_ask(self):
        other_common = _make_repo(os.path.join(self.tmp, "other"))
        other_wt = os.path.join(self.tmp, "other-wt")
        _link_worktree(other_common, other_wt, "w2")
        self.assertEqual(self.decision(os.path.join(other_wt, "tests", "t.py")), "force_ask")

    def test_independent_repository_force_ask(self):
        independent = os.path.join(self.tmp, "independent")
        _make_repo(independent)
        self.assertEqual(self.decision(os.path.join(independent, "tests", "t.py")), "force_ask")

    def test_plain_directory_without_git_force_ask(self):
        self.assertEqual(self.decision(os.path.join(self.tmp, "plain", "tests", "t.py")), "force_ask")

    def test_order_endpoint_content_force_ask_in_worktree(self):
        self.assertEqual(self.decision(os.path.join(self.wt, "tests", "t.py"), ENDPOINT), "force_ask")
        self.assertEqual(self.decision(os.path.join(self.wt, "scripts", "x.py"), ENDPOINT), "force_ask")

    def test_main_checkout_regressions(self):
        self.assertEqual(self.decision(os.path.join(self.repo, "tests", "t.py")), "ask")
        self.assertEqual(self.decision(os.path.join(self.repo, "x.py")), "force_ask")
        self.assertEqual(self.decision(os.path.join(self.repo, "tests", "t.py"), ENDPOINT), "force_ask")

    def test_main_checkout_ground_truth_and_trail_denials_unchanged(self):
        for rel in ("logs/session_state.json", "logs/guardian_state.json", "logs/evaluations/latest_dossier.json"):
            with self.subTest(rel=rel):
                self.assertEqual(self.decision(os.path.join(self.repo, *rel.split("/")), "{}"), "deny")

    def test_live_checkout_that_is_a_linked_worktree(self):
        self.assertEqual(self.decision(os.path.join(self.wt, "tests", "t.py"), base_dir=self.wt), "ask")
        self.assertEqual(self.decision(os.path.join(self.wt, "x.py"), base_dir=self.wt), "force_ask")

    def test_worktree_harness_copy_keeps_plain_ask(self):
        target = os.path.join(self.wt, "scripts", "hooks", "pre_trade_guard.py")
        self.assertEqual(self.decision(target, "x = 1\n"), "ask")


class TestLinkedWorktreeRelHelper(_Base):
    def rel(self, target: str, base_dir: str = "") -> str:
        return pre_trade_guard._linked_worktree_rel(target, base_dir or self.repo)

    def test_returns_worktree_relative_path(self):
        self.assertEqual(self.rel(os.path.join(self.wt, "tests", "t.py")), "tests/t.py")

    def test_live_checkout_itself_returns_empty(self):
        self.assertEqual(self.rel(os.path.join(self.repo, "tests", "t.py")), "")

    def test_live_checkout_is_linked_worktree_resolves_common_dir(self):
        wt2 = os.path.join(self.tmp, "trading-wt-issue-2")
        _link_worktree(self.common, wt2, "w2")
        self.assertEqual(self.rel(os.path.join(wt2, "scripts", "x.py"), base_dir=self.wt), "scripts/x.py")

    def test_oversized_git_file_returns_empty(self):
        admin = os.path.join(self.common, "worktrees", "w1")
        _write(os.path.join(self.wt, ".git"), f"gitdir: {admin}\n" + "#" * 5000)
        self.assertEqual(self.rel(os.path.join(self.wt, "tests", "t.py")), "")

    def test_unreadable_git_file_returns_empty(self):
        with patch("builtins.open", side_effect=PermissionError("denied")):
            self.assertEqual(self.rel(os.path.join(self.wt, "tests", "t.py")), "")

    def test_malformed_git_file_returns_empty(self):
        _write(os.path.join(self.wt, ".git"), "not a pointer\n")
        self.assertEqual(self.rel(os.path.join(self.wt, "tests", "t.py")), "")

    def test_symlinked_git_file_to_real_worktree_force_ask(self):
        forged = os.path.join(self.tmp, "forged-link")
        os.makedirs(os.path.join(forged, "tests"))
        try:
            os.symlink(os.path.join(self.wt, ".git"), os.path.join(forged, ".git"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        target = os.path.join(forged, "tests", "t.py")
        self.assertEqual(self.rel(target), "")
        self.assertEqual(pre_trade_guard.evaluate_file_write(target, PRIMITIVES, self.repo)[0], "force_ask")
        self.assertEqual(self.rel(os.path.join(self.wt, "tests", "t.py")), "tests/t.py")

    def test_symlink_escaping_worktree_returns_empty(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        link = os.path.join(self.wt, "tests")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.rel(os.path.join(link, "t.py")), "")


if __name__ == "__main__":
    unittest.main()
