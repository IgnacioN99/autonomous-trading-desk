"""
Issue orchestrator: the issue_fixer Bash guard, the issue_workspace helper and the generator rules that keep
write tools confined to guarded agents.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts.dev import issue_workspace as iw  # noqa: E402
from scripts.dev import sync_claude_assets as gen  # noqa: E402
from scripts.hooks import issue_fixer_guard as guard  # noqa: E402

WT = "/repo-wt-issue-7"

ALLOWED = [
    f"cd {WT} && python3 -m unittest discover tests/ 2>&1 | tail -5",
    f"cd {WT} && python3 -m unittest tests.test_issue_orchestrator -v",
    f"cd {WT} && python3 -m compileall -q scripts/ tests/ && python3 scripts/dev/sync_claude_assets.py --check",
    "python3 scripts/dev/sync_claude_assets.py",
    "python3 -B tests/test_issue_orchestrator.py",
    f"python3 {WT}/tests/test_issue_orchestrator.py",
    "python3 -m pytest -q tests/test_issue_orchestrator.py",
    "git diff --stat",
    "git -C /repo-wt-issue-7 status --short",
    "git log --oneline -5 -- scripts/dev",
    "git show HEAD:scripts/dev/issue_workspace.py | head -20",
    "grep -rn 'gh pr' scripts | head",
    'find . -name "*.py" | xargs grep -n issue_workspace',
    "ls -la tests && wc -l scripts/dev/*.py",
    "sed -n 1,40p scripts/dev/issue_workspace.py",
    "cat logs/issue_work/design.md",
    "env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_x",
    "timeout 600 python3 -m unittest discover tests/",
    "echo 'git push is only a string here'",
    "mkdir -p tests/fixtures && cp a b",
]

DENIED = [
    ("git commit -m fix", "read-only git"),
    ("git add -A", "read-only git"),
    ("cd /x && git push -u origin fix/issue-7", "read-only git"),
    ("git stash", "read-only git"),
    ("git checkout main", "read-only git"),
    ("git reset --hard HEAD", "read-only git"),
    ("ls; git merge origin/main", "read-only git"),
    ("ls\ngit rebase main", "read-only git"),
    ("git -c alias.st=!sh status", "git -c"),
    ("git diff --ext-diff", "external program"),
    ("git grep -Ovim foo", "external program"),
    ("git", "bare `git`"),
    ("gh pr create --fill", "not available"),
    ("curl https://fapi.binance.com/fapi/v1/time", "not available"),
    ("wget http://example.com", "not available"),
    ("pip install requests", "not available"),
    ("python3 -m pip install requests", "not allowed"),
    ("python3 -c 'print(1)'", "inline code"),
    ("python3 -", "stdin"),
    ("python3", "interactive"),
    ("python3 scripts/sync_session_state.py", "only tests"),
    ("python3 scripts/trading_doctor.py", "only tests"),
    ("python3 scripts/loops/position_guardian_loop.py --once", "only tests"),
    ("./scripts/report_issue.sh --title x", "direct execution"),
    ("scripts/report_issue.sh", "direct execution"),
    ("bash -c 'git push'", "not available"),
    ("sh scripts/x.sh", "not available"),
    ("eval git push", "not available"),
    ("echo $(git push)", "command substitution"),
    ("echo `git push`", "backtick"),
    ("diff <(ls) <(ls)", "process substitution"),
    ("cat > tests/x.py <<EOF\nprint(1)\nEOF", "heredoc"),
    ("env A=1 git push", "read-only git"),
    ("timeout 5 git stash", "read-only git"),
    ("nice -n 5 gh issue list", "not available"),
    ("find . | xargs -n 1 gh", "not available"),
    ("sudo ls", "not available"),
    ("wsl.exe -d Ubuntu -- ls", "not available"),
    ("node -e 'x'", "not available"),
    ("ls 'unterminated", "unparseable"),
    ("", "empty"),
]


def run_hook(payload) -> tuple:
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    err = io.StringIO()
    with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stderr", err):
        code = guard.main()
    return code, err.getvalue()


class TestIssueFixerGuard(unittest.TestCase):

    def test_allowed_commands(self):
        for cmd in ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertEqual(guard.evaluate(cmd), "", cmd)

    def test_denied_commands(self):
        for cmd, why in DENIED:
            with self.subTest(cmd=cmd):
                reason = guard.evaluate(cmd)
                self.assertTrue(reason, f"should be denied: {cmd!r}")
                self.assertIn(why, reason, cmd)

    def test_hook_contract(self):
        self.assertEqual(run_hook({"tool_name": "Bash", "tool_input": {"command": "git diff"}}), (0, ""))
        code, err = run_hook({"tool_name": "Bash", "tool_input": {"command": "git push"}})
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED by issue_fixer_guard", err)
        # Other tools are governed by the fixer's tool list, not by this guard
        self.assertEqual(run_hook({"tool_name": "Edit", "tool_input": {"file_path": "x"}})[0], 0)
        # Fail closed on malformed input
        self.assertEqual(run_hook("not json")[0], 2)
        self.assertEqual(run_hook("[1, 2]")[0], 2)
        self.assertEqual(run_hook({"tool_name": "Bash", "tool_input": {}})[0], 2)

    def test_runs_as_a_script(self):
        script = REPO_ROOT / "scripts" / "hooks" / "issue_fixer_guard.py"
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh issue list"}})
        res = subprocess.run([sys.executable, str(script)], input=payload, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        res = subprocess.run([sys.executable, str(script)], input=payload.replace("gh issue list", "git status"),
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class TestIssueWorkspaceReviewContext(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        repo = self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "scripts", "dev"))
        os.makedirs(os.path.join(repo, "tests"))
        Path(repo, ".gitignore").write_text("logs/issue_work/\n")
        Path(repo, "scripts", "app.py").write_text("VALUE = 1\n")
        Path(repo, "scripts", "dev", "sync_claude_assets.py").write_text("import sys\nsys.exit(0)\n")
        Path(repo, "tests", "test_app.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n")
        _git(repo, "init", "-q")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def test_diff_includes_untracked_files_and_checks(self):
        Path(self.repo, "scripts", "app.py").write_text("VALUE = 2\n")
        Path(self.repo, "scripts", "new_module.py").write_text("NEW = True\n")
        os.makedirs(os.path.join(self.repo, "logs", "issue_work"))
        Path(self.repo, "logs", "issue_work", "design.md").write_text("ignored")
        out = iw.cmd_review_context(self.repo)
        review = Path(self.repo, "logs", "issue_work", "review")
        diff = (review / "diff.patch").read_text()
        self.assertIn("-VALUE = 1", diff)
        self.assertIn("+VALUE = 2", diff)
        self.assertIn("+NEW = True", diff)
        self.assertNotIn("design.md", diff)  # the gitignored work dir is never part of the review diff
        self.assertEqual(out["untracked_files"], ["scripts/new_module.py"])
        checks = json.loads((review / "checks.json").read_text())
        self.assertTrue(checks["checks_ok"])
        self.assertEqual([c["name"] for c in checks["checks"]], ["compileall", "sync_claude_assets", "unittest"])
        self.assertIn("Ran 1 test", checks["checks"][2]["summary"])
        self.assertIn("===== unittest (exit 0) =====", (review / "checks.log").read_text())

    def test_failing_suite_is_reported_not_hidden(self):
        Path(self.repo, "tests", "test_fail.py").write_text(
            "import unittest\n\nclass F(unittest.TestCase):\n    def test_no(self):\n        self.fail('x')\n")
        out = iw.cmd_review_context(self.repo)
        self.assertFalse(out["checks_ok"])
        checks = json.loads(Path(self.repo, "logs", "issue_work", "review", "checks.json").read_text())
        unit = checks["checks"][2]
        self.assertFalse(unit["ok"])
        self.assertIn("FAILED", unit["summary"])

    def test_rejects_non_worktree(self):
        plain = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(plain)
        with self.assertRaises(iw.WorkspaceError):
            iw.cmd_review_context(plain)


class TestIssueWorkspaceInitCleanup(unittest.TestCase):
    """init/cleanup with gh and the network faked; git runs for real on a temp repo with a local 'origin'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "trading")
        _git(self.tmp, "init", "-q", "--bare", self.origin)
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        Path(self.repo, "README.md").write_text("x\n")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        _git(self.repo, "remote", "add", "origin", self.origin)
        _git(self.repo, "push", "-q", "origin", "main")
        self.real_run = iw.run

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def fake_run(self, issue_state="OPEN", merged=True):
        def run(cmd, cwd=None, check=True, timeout=300):
            if cmd[0] == "gh" and cmd[1:3] == ["issue", "view"]:
                body = {"number": int(cmd[3]), "title": "T", "body": "B", "labels": [], "state": issue_state,
                        "url": "u"}
                return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
            if cmd[0] == "gh" and cmd[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps([{"number": 1}] if merged else []), "")
            if cmd[0] == "gh":
                raise AssertionError(f"unexpected gh call {cmd}")
            return self.real_run(cmd, cwd=cwd, check=check, timeout=timeout)
        return run

    def test_init_creates_sibling_worktree_and_issue_file_then_cleanup(self):
        with mock.patch.object(iw, "run", self.fake_run()):
            out = iw.cmd_init(7, "fix-thing", "origin/main", cwd=self.repo)
            self.assertEqual(out["worktree"], os.path.join(self.tmp, "trading-wt-issue-7"))
            self.assertEqual(out["branch"], "fix/issue-7-fix-thing")
            issue = json.loads(Path(out["issue_file"]).read_text())
            self.assertEqual(issue["number"], 7)
            # A second init for the same issue refuses to reuse the worktree
            with self.assertRaises(iw.WorkspaceError):
                iw.cmd_init(7, "again", "origin/main", cwd=self.repo)
            # Called from inside the worktree, the helper still resolves the main checkout
            self.assertEqual(os.path.realpath(iw.main_repo_root(out["worktree"])), os.path.realpath(self.repo))
            res = iw.cmd_cleanup(7, cwd=self.repo)
        self.assertFalse(os.path.exists(out["worktree"]))
        self.assertEqual(res["deleted_branch"], "fix/issue-7-fix-thing")
        self.assertNotIn("fix/issue-7", _git(self.repo, "branch"))

    def test_init_refuses_closed_issue_and_bad_slug(self):
        with mock.patch.object(iw, "run", self.fake_run(issue_state="CLOSED")):
            with self.assertRaises(iw.WorkspaceError):
                iw.cmd_init(8, "x", "origin/main", cwd=self.repo)
        with self.assertRaises(iw.WorkspaceError):
            iw.cmd_init(8, "Bad Slug!", "origin/main", cwd=self.repo)

    def test_cleanup_requires_a_merged_pr_unless_forced(self):
        with mock.patch.object(iw, "run", self.fake_run(merged=False)):
            out = iw.cmd_init(9, "wip", "origin/main", cwd=self.repo)
            with self.assertRaises(iw.WorkspaceError):
                iw.cmd_cleanup(9, cwd=self.repo)
            self.assertTrue(os.path.exists(out["worktree"]))
            iw.cmd_cleanup(9, force=True, cwd=self.repo)
        self.assertFalse(os.path.exists(out["worktree"]))


class TestGeneratorWriteAgents(unittest.TestCase):
    BASE = ("---\nname: {name}\ndescription: >-\n  Test agent.\ntools:\n{tools}model: opus\n---\nbody\n")

    def render(self, name, tools):
        listing = "".join(f"  - {t}\n" for t in tools)
        return gen.render_agent(f".agents/agents/{name}/agent.md", self.BASE.format(name=name, tools=listing))

    def test_write_tools_only_for_guarded_agents(self):
        with self.assertRaises(gen.SourceError):
            self.render("rogue_agent", ["view_file", "run_command"])
        with self.assertRaises(gen.SourceError):
            self.render("rogue_agent", ["view_file", "write_to_file"])
        with mock.patch.object(gen, "WRITE_AGENTS", {"issue_fixer", "unguarded"}):
            with self.assertRaises(gen.SourceError):
                self.render("unguarded", ["run_command"])
        _, content = self.render("issue_fixer", ["view_file", "run_command", "replace_file_content"])
        self.assertIn("tools: Read, Bash, Edit", content)
        self.assertIn("issue_fixer_guard.py || exit 2", content)
        self.assertIn("`run_command` = Bash", content)

    def test_generated_issue_agents(self):
        expected = {
            "issue_locator": {"Read", "Grep", "Glob"},
            "issue_fixer": {"Read", "Grep", "Glob", "Bash", "Write", "Edit"},
            "issue_auditor": {"Read", "Grep", "Glob"},
        }
        for name, tools in expected.items():
            text = (REPO_ROOT / ".claude" / "agents" / f"{name}.md").read_text(encoding="utf-8")
            head = text.split("\n---\n", 1)[0]
            line = next(ln for ln in head.splitlines() if ln.startswith("tools:"))
            self.assertEqual({t.strip() for t in line[len("tools:"):].split(",")}, tools, name)
            self.assertIn("model: opus", head, name)
            self.assertEqual("hooks:" in head, name == "issue_fixer", name)
            for banned in ("WebSearch", "WebFetch", "Agent"):
                self.assertNotIn(banned, line, name)

    def test_issue_agent_prompts_follow_the_xml_contract(self):
        for name in ("issue_locator", "issue_fixer", "issue_auditor"):
            text = (REPO_ROOT / ".agents" / "agents" / name / "agent.md").read_text(encoding="utf-8")
            for tag in ("identity_and_role", "operational_environment", "tool_use_protocol",
                        "invariants_and_rules", "negative_constraints", "few_shot_examples", "output_contract"):
                self.assertIn(f"<{tag}>", text, f"{name}: <{tag}>")
                self.assertIn(f"</{tag}>", text, f"{name}: </{tag}>")
            self.assertIn('<example type="negative">', text, name)
        auditor = (REPO_ROOT / ".agents" / "agents" / "issue_auditor" / "agent.md").read_text(encoding="utf-8")
        self.assertIn("VERDICT: APPROVE", auditor)
        self.assertIn("VERDICT: CHANGES_REQUESTED", auditor)

    def test_skill_is_generated_and_names_every_subagent(self):
        skill = (REPO_ROOT / ".claude" / "skills" / "issue-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
        for name in ("issue_locator", "issue_fixer", "issue_auditor", "issue_workspace.py", "pr-review"):
            self.assertIn(name, skill)
        self.assertIn(gen.GENERATED_MARKER, skill)


if __name__ == "__main__":
    unittest.main()
