"""
Issue orchestrator: the issue_fixer Bash guard, the issue_workspace helper and the generator rules that keep
write tools confined to guarded agents.
"""

import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts.dev import issue_workspace as iw  # noqa: E402
from scripts.dev import sync_claude_assets as gen  # noqa: E402
from scripts.hooks import issue_fixer_guard as guard  # noqa: E402

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _make_repo(root: str, name: str = "trading") -> str:
    repo = os.path.join(root, name)
    os.makedirs(os.path.join(repo, "scripts", "hooks"))
    Path(repo, "README.md").write_text("x\n")
    Path(repo, "scripts", "hooks", "issue_fixer_guard.py").write_text("# running guard\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


def _guard_check_block(skill: str) -> str:
    """The issue-orchestrator skill's "Guard check" bullet with its indented sub-bullets (#256 split it)."""
    lines = skill.splitlines()
    start = next(i for i, line in enumerate(lines) if "**Guard check.**" in line)
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip() or len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return "\n".join(block)


FAKE_HOME = "/nonexistent-issue-fixer-test-home"  # never under the temp dir, never read

# {wt} = the linked issue worktree, {main} = the main checkout (where the running hooks live)
ALLOWED = [
    "cd {wt} && python3 -m unittest discover tests/ 2>&1 | tail -5",
    "cd {wt} && python3 -m unittest tests.test_issue_orchestrator -v",
    "cd {wt} && python3 -m compileall -q scripts/ tests/ && python3 scripts/dev/sync_claude_assets.py --check",
    "cd {wt} && python3 scripts/dev/sync_claude_assets.py",
    "cd {wt} && python3 -B tests/test_issue_orchestrator.py",
    "cd {wt} && python3 {wt}/tests/test_issue_orchestrator.py",
    "cd {wt} && python3 -m pytest -q tests/test_issue_orchestrator.py",
    "cd {wt} && git diff --stat",
    "cd {wt} && git status --short && git log --oneline -5 -- scripts/dev",
    "cd {wt} && git show HEAD:scripts/dev/issue_workspace.py | head -20",
    "cd {wt} && grep -rn 'gh pr' scripts | head",
    'cd {wt} && find . -name "*.py" | xargs grep -n issue_workspace',
    "cd {wt} && ls -la tests && wc -l scripts/dev/*.py",
    "cd {wt} && sed -n 1,40p scripts/dev/issue_workspace.py",
    "cd {wt} && sed -i 's/old/new/g' scripts/app.py",
    "cd {wt} && cat logs/issue_work/design.md",
    "cd {wt} && env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_x",
    "cd {wt} && timeout 600 python3 -m unittest discover tests/",
    "cd {wt} && echo 'git push is only a string here'",
    "cd {wt} && mkdir -p tests/fixtures && cp a b",
    "cd {wt}\npython3 -m unittest tests.test_x",
    "cd {wt}/scripts && cd .. && ls",
    # #129: quoted patterns are literal; reads of the stdlib and the temp dir; writes inside the worktree
    "cd {wt} && grep -rn '/fapi/v1/order' scripts",
    'cd {wt} && grep -rn "/fapi/v1/order" scripts',
    "cd {wt} && sed -n '/a/,/b/p' scripts/app.py",
    "cd {wt} && grep -n 'print $1' scripts/app.py",
    "cd {wt} && cat {stdlib}/__init__.py",
    "cd {wt} && ls /tmp",
    "cd {wt} && python3 -m unittest tests.test_x > /tmp/issue_fixer_out.txt 2>&1",
    "cd {wt} && echo notes > logs/issue_work/notes.md",
    "cd {wt} && cd scripts && ls && cd .. && git status",
    "cd {wt} && LC_ALL=C TZ=UTC python3 -m unittest tests.test_x",
    "cd {wt} && python3 -m unittest \\\n  tests.test_x",
    "cd {wt} && git status # show the changes; rm -rf / is only a comment here",
    "cd {wt} && grep -n 'a#b' scripts/app.py",
    "cd {wt} && ls x#y; git status",
    'cd {wt} && python3 -m unittest tests.test_x; echo "exit=$?"',
    "cd {wt} && echo $? && printf '%s\\n' \"$?\"",
    "cd {wt} && git ls-files | xargs wc -l",
    # #230: character sets, field / key / delimiter values are not paths; a cd chain followed by `;` is tracked
    "cd {wt} && tr / .",
    "cd {wt} && git log --format=%an | tr / . | sort -u",
    "cd {wt} && cut -d/ -f1 f",
    "cd {wt} && cut --delimiter=/ --fields=2 f",
    "cd {wt} && sort -t/ -k2 f",
    "cd {wt} && sort -S 1M -t / -k 2 f",
    "cd {wt} && uniq -f 1 -s 2 -w 3 f",
    "cd {wt} && cd scripts; git status",
    "cd {wt}/scripts && cd ..; ls tests",
    "cd {wt} && cd scripts\nls",
    "cd {wt} && cd scripts && cd ..; ls",
    "cd {wt} && cd scripts && python3 ../tests/x.py; ls",
    "cd {wt} && rg -n pattern scripts",
]

DENIED = [
    ("cd {wt} && git commit -m fix", "read-only git"),
    ("cd {wt} && git add -A", "read-only git"),
    ("cd {wt} && git push -u origin fix/issue-7", "read-only git"),
    ("cd {wt} && git stash", "read-only git"),
    ("cd {wt} && git checkout main", "read-only git"),
    ("cd {wt} && git reset --hard HEAD", "read-only git"),
    ("cd {wt}; ls; git merge origin/main", "read-only git"),
    ("cd {wt}\nls\ngit rebase main", "read-only git"),
    ("cd {wt} && git -c alias.st=!sh status", "git -c"),
    ("cd {wt} && git diff --ext-diff", "external program"),
    ("cd {wt} && git grep -Ovim foo", "external program"),
    ("cd {wt} && git", "bare `git`"),
    ("cd {wt} && gh pr create --fill", "not available"),
    ("cd {wt} && curl https://fapi.binance.com/fapi/v1/time", "not available"),
    ("cd {wt} && wget http://example.com", "not available"),
    ("cd {wt} && pip install requests", "not available"),
    ("cd {wt} && python3 -m pip install requests", "not allowed"),
    ("cd {wt} && python3 -c 'print(1)'", "inline code"),
    ("cd {wt} && python3 -", "stdin"),
    ("cd {wt} && python3", "interactive"),
    ("cd {wt} && python3 scripts/sync_session_state.py", "only tests"),
    ("cd {wt} && python3 scripts/trading_doctor.py", "only tests"),
    ("cd {wt} && python3 scripts/loops/position_guardian_loop.py --once", "only tests"),
    ("cd {wt} && ./scripts/report_issue.sh --title x", "direct execution"),
    ("cd {wt} && scripts/report_issue.sh", "direct execution"),
    ("cd {wt} && bash -c 'git push'", "not available"),
    ("cd {wt} && sh scripts/x.sh", "not available"),
    ("cd {wt} && eval git push", "not available"),
    ("cd {wt} && echo $(git push)", "command substitution"),
    ("cd {wt} && echo `git push`", "backtick"),
    ("cd {wt} && diff <(ls) <(ls)", "process substitution"),
    ("cd {wt} && cat > tests/x.py <<EOF\nprint(1)\nEOF", "heredoc"),
    ("cd {wt} && env A=1 git push", "read-only git"),
    ("cd {wt} && env -S 'gh issue list'", "env -S"),
    ("cd {wt} && timeout 5 git stash", "read-only git"),
    ("cd {wt} && nice -n 5 gh issue list", "not available"),
    ("cd {wt} && find . | xargs -n 1 gh", "not available"),
    ("cd {wt} && find . -name x -exec gh issue list ;", "find -exec"),
    ("cd {wt} && awk 'BEGIN{{system(\"gh\")}}'", "not available"),
    ("cd {wt} && sed -n '1e gh issue list' f", "sed e/w"),
    ("cd {wt} && sed 's/a/b/e' f", "sed e/w"),
    ("cd {wt} && sed 's/a/b/w /tmp/out' f", "sed e/w"),
    ("cd {wt} && make test", "not available"),
    ("cd {wt} && sudo ls", "not available"),
    ("cd {wt} && wsl.exe -d Ubuntu -- ls", "not available"),
    ("cd {wt} && node -e 'x'", "not available"),
    ("cd {wt} && ls 'unterminated", "unparseable"),
    ("", "empty"),
    # confinement: worktree first, never the main checkout
    ("python3 -m unittest discover tests/", "must start with `cd"),
    ("git diff", "must start with `cd"),
    ("cd {main} && python3 -m unittest discover tests/", "must start with `cd"),
    ("cd tests && ls", "must start with `cd"),
    ("cd /tmp && ls", "must start with `cd"),
    ("cd {wt} && cd {main} && ls", "leaves the issue worktree"),
    ("cd {wt} && sed -i s/x/y/ {main}/scripts/hooks/issue_fixer_guard.py", "main checkout"),
    ("cd {wt} && cp tests/x.py ../trading/scripts/hooks/issue_fixer_guard.py", "main checkout"),
    ("cd {wt} && echo x > {main}/scripts/hooks/issue_fixer_guard.py", "main checkout"),
    ("cd {wt} && sed -i s/x/y/ \"$CLAUDE_PROJECT_DIR\"/scripts/hooks/issue_fixer_guard.py", "CLAUDE_PROJECT_DIR"),
    ("cd {wt} && cat ~/.gitconfig", "home-directory"),
    # #129.1-3: launchers, builtins, archivers and environment variables
    ("cd {wt} && setsid sh -c x", "not available"),
    ("cd {wt} && flock f sh -c x", "not available"),
    ("cd {wt} && ionice sh", "not available"),
    ("cd {wt} && tar --checkpoint-action=exec=x -cf a.tar f", "not available"),
    ("cd {wt} && zip -TT x a.zip f", "not available"),
    ("cd {wt} && GIT_EXTERNAL_DIFF=x git diff", "environment variable GIT_EXTERNAL_DIFF"),
    ("cd {wt} && LD_PRELOAD=x ls", "environment variable LD_PRELOAD"),
    ("cd {wt} && PAGER=x; git log", "only assigns"),
    ("cd {wt} && export PAGER=x && git log", "not available"),
    ("cd {wt} && env BASH_ENV=x ls", "environment variable BASH_ENV"),
    ("cd {wt} && env -C / ls", "env -C"),
    ("cd {wt} && pushd scripts", "not available"),
    ("cd {wt} && if true; then ls; fi", "compound"),
    ("cd {wt} && {{ ls; }}", "compound"),
    # #129.4: python scripts resolve inside the issue worktree
    ("cd {wt} && python3 tests/../scripts/trading_doctor.py", "only tests"),
    ("cd {wt} && python3 {wt2}/tests/x.py", "another issue worktree"),
    # #129.5: expansions, globs and braces
    ("cd {wt} && cat {wt}/*/../../trading/x", "main checkout"),
    ("cd {wt} && cp a {wt}/{{b,../x}}", "brace expansion"),
    ("cd {wt} && echo x > $PWD/../x", "expansion"),
    ("cd {wt} && echo x > $OLDPWD/x", "expansion"),
    ("cd {wt} && cat \"$HOME\"/x", "expansion"),
    ("cd {wt} && cat $'\\x2e\\x2e'/x", "expansion"),
    ("cd {wt} && cat ~root/x", "tilde expansion"),
    ("cd {wt} && rm -f tests/*.py", "glob"),
    # #129.5 and .8: writes outside the worktree or to protected files
    ("cd {wt} && tee /etc/x", "outside the issue worktree"),
    ("cd {wt} && cp a {home}/.claude/settings.json", "home directory"),
    ("cd {wt} && echo x > /tmp/../etc/y", "outside the issue worktree"),
    ("cd {wt} && sed -i s/a/b/ {wt}/.claude/settings.json", ".claude/"),
    ("cd {wt} && sort -o .agents/hooks.json f", "hook configuration"),
    ("cd {wt} && ln -s ../x y", "not available"),
    ("cd {wt} && echo x > logs/issue_work/guard_heartbeat.json", "written only by"),
    ("cd {wt} && cp a logs/issue_work/fixer_binding.json", "written only by"),
    ("cd {wt} && find . -name fixer_binding.json -delete", "find -exec"),
    ("cd {wt} && cat list | xargs rm", "xargs"),
    ("cd {wt} && ls;>/etc/x", "outside the issue worktree"),
    ("cd {wt} && ls &&>x git push", "read-only git"),
    # #129.5: cd must keep the tracked directory equal to the real one
    ("cd {wt} && (cd scripts) && cp a ../x", "subshell"),
    ("cd {wt} && false && cd scripts; cp a ../x", "conditional cd"),
    ("cd {wt} && ls || cd scripts", "next to ||"),
    ("cd {wt} && cd scripts | ls", "pipeline"),
    ("cd {wt} && cd nonexistent && ls", "not an existing directory"),
    ("cd {wt} && cd -", "not allowed"),
    ("cd {wt} && command cd ..", "plain `cd"),
    ("cd {wt}/nonexistent; ls", "must start with `cd"),
    # escapes, comments and continuations must not merge lines into one segment
    ("cd {wt} && echo \\\"\ngit push", "read-only git"),
    ("cd {wt} && ls # \"\ngit push", "read-only git"),
    ("cd {wt} && ls # \"\ngit push\n\"", "unparseable"),
    ("cd {wt} && ls \\\\\ngit push", "read-only git"),
    # a `#` starts a comment only at a word start; a carriage return (an ordinary character in bash) is denied
    # outright (#230)
    ("cd {wt} && ls x\\ #; git push", "read-only git"),
    ("cd {wt} && ls x\\;#; git push", "read-only git"),
    ("cd {wt} && ls x\r#; git push", "carriage return in command; send LF line endings"),
    ("cd {wt} && ls \\\r\ngit push", "carriage return in command; send LF line endings"),
    ("cd {wt}\ncd scripts\r\ncp a ../trading/README.md", "carriage return in command; send LF line endings"),
    ("cd {wt}\r\ngit status", "carriage return in command; send LF line endings"),
    # globs: unknown programs (they may write) take none; the directory part before the glob is checked
    ("cd {wt} && unlink ../[^x]rading/scripts/hooks/issue_fixer_guard.py", "may write"),
    ("cd {wt} && mkdir -p tests/x && unlink tests/x/../../../[t]rading/scripts/hooks/issue_fixer_guard.py",
     "may write"),
    ("cd {wt} && shred tests/*.py", "may write"),
    ("cd {wt} && cat {main}/scripts/[^h]ooks/x", "main checkout"),
    # xargs feeds only read-only programs (its arguments come from stdin)
    ("cd {wt} && echo ../trading/scripts/hooks/issue_fixer_guard.py | xargs unlink", "xargs"),
    ("cd {wt} && echo -i s/a/b/ /etc/x | xargs sed", "xargs"),
    ("cd {wt} && echo -o /etc/x f | xargs sort", "xargs"),
    ("cd {wt} && echo a b | xargs uniq", "xargs"),
    ("cd {wt} && echo --output=/etc/x | xargs git log", "xargs"),
    ("cd {wt} && ls | xargs env", "xargs"),
    ("cd {wt} && ls | xargs timeout 5", "xargs"),
    # `$?` only as echo/printf text, never in a path, a cd or a redirection
    ("cd {wt} && cat tests/$?", "expansion"),
    ("cd {wt} && echo x > tests/$?", "expansion"),
    ("cd {wt} && cd $?", "expansion"),
    ("cd {wt}/$?; ls", "expansion"),
    # #129.6: one command, one worktree
    ("cd {wt} && cd {wt2}", "leaves the issue worktree"),
    ("cd {wt} && cat {wt2}/README.md", "another issue worktree"),
    ("cd {wt} && cp README.md {wt2}/x", "another issue worktree"),
    # #230: bracket globs are matched as bash reads them ([^x] is a negation), so the sibling checkout is found
    ("cd {wt} && cat ../[^x]rading/README.md", "main checkout"),
    ("cd {wt} && cat ../[!x]rading/README.md", "main checkout"),
    ("cd {wt} && cat ../[[:alpha:]]rading/README.md", "main checkout"),
    ("cd {wt} && cat ../tr[]a]ding/README.md", "main checkout"),
    # #230: options that run programs
    ("cd {wt} && rg --pre x pattern scripts", "rg --pre"),
    ("cd {wt} && rg --pre=x pattern scripts", "rg --pre"),
    ("cd {wt} && rg --pre-glob '*.py' --pre x pattern", "rg --pre"),
    ("cd {wt} && rg --hostname-bin x --hyperlink-format default pattern", "rg --pre"),
    ("cd {wt} && rg --hostname-bin=x pattern", "rg --pre"),
    ("cd {wt} && sort --compress-program=x f", "sort --compress-program"),
    ("cd {wt} && sort --compress-program x f", "sort --compress-program"),
    ("cd {wt} && sort --compress x f", "sort --compress-program"),
    ("cd {wt} && sort --co=x f", "sort --compress-program"),
    # #230: conditional cd chains: only cds may precede a skippable cd; each possible directory is checked
    ("cd {wt} && ls && cd scripts; cp a ../x", "conditional cd"),
    ("cd {wt}/scripts && cd ..; cat ../../trading/README.md", "a conditional cd may have been skipped"),
    ("cd {wt} && cd scripts; ls || cat x", "conditional cd"),
    ("cd {wt} && cd scripts && ls & cat x", "background list"),
    ("cd {wt} && cd scripts; (ls)", "conditional cd"),
    # #230: a background list holding the leading cd leaves the rest in the session's directory
    ("cd {wt} && ls & echo x > README.md", "background list"),
    ("cd {wt} && (ls) & echo x > README.md", "background list"),
]


class TestIssueFixerGuard(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.main = _make_repo(cls.tmp)
        cls.wt = os.path.join(cls.tmp, "trading-wt-issue-7")
        _git(cls.main, "worktree", "add", "-q", cls.wt, "-b", "fix/issue-7-x")
        os.makedirs(os.path.join(cls.wt, "scripts"), exist_ok=True)
        # A second issue worktree (another session's)
        cls.wt2 = os.path.join(cls.tmp, "trading-wt-issue-8")
        _git(cls.main, "worktree", "add", "-q", cls.wt2, "-b", "fix/issue-8-y")
        os.makedirs(os.path.join(cls.wt2, "tests"), exist_ok=True)
        cls.conf = guard.Confinement.from_project_dir(cls.main)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["rm", "-rf", cls.tmp], check=False)

    def fmt(self, cmd):
        return cmd.format(wt=self.wt, main=self.main, wt2=self.wt2, home=FAKE_HOME,
                          stdlib=os.path.dirname(json.__file__))

    def run_hook(self, payload, project_dir=None):
        stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        err = io.StringIO()
        env = {"CLAUDE_PROJECT_DIR": project_dir or self.main}
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stderr", err), \
                mock.patch.dict(os.environ, env):
            code = guard.main()
        return code, err.getvalue()

    def test_allowed_commands(self):
        for cmd in ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertEqual(guard.evaluate(self.fmt(cmd), self.conf), "", cmd)

    def test_denied_commands(self):
        # A home directory outside the temp dir, whatever HOME the suite runs with (review-context sets a fresh
        # temporary one)
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}):
            for cmd, why in DENIED:
                with self.subTest(cmd=cmd):
                    reason = guard.evaluate(self.fmt(cmd), self.conf)
                    self.assertTrue(reason, f"should be denied: {cmd!r}")
                    self.assertIn(why, reason, cmd)

    def test_edit_tools_are_confined_to_the_worktree(self):
        allowed = [f"{self.wt}/scripts/app.py", f"{self.wt}/tests/test_new.py", f"{self.wt}/AGENTS.md",
                   f"{self.wt}/.agents/agents/issue_fixer/agent.md", f"{self.wt}/logs/issue_work/fixer_report.md",
                   f"{self.wt}/scripts/hooks/pre_trade_guard.py"]
        denied = [
            (f"{self.main}/scripts/hooks/issue_fixer_guard.py", "outside the issue worktrees"),
            (f"{self.main}/README.md", "outside the issue worktrees"),
            ("/etc/hosts", "outside the issue worktrees"),
            ("scripts/app.py", "absolute path"),
            (f"{self.wt}/.git/config", "git internals"),
            (f"{self.wt}/.claude/agents/issue_fixer.md", ".claude/"),
            (f"{self.wt}/.claude/settings.json", ".claude/"),
            (f"{self.wt}/.agents/hooks.json", "hook configuration"),
            (f"{self.wt}/logs/session_state.json", "logs/issue_work/"),
            (f"{self.wt}/logs/issue_work", "logs/issue_work/"),
            (f"{self.wt}/scripts/../../trading/README.md", "outside the issue worktrees"),
        ]
        for tool in ("Edit", "Write", "MultiEdit"):
            for path in allowed:
                with self.subTest(tool=tool, path=path):
                    payload = {"tool_name": tool, "tool_input": {"file_path": path}}
                    self.assertEqual(guard.evaluate_payload(payload, self.conf), "")
            for path, why in denied:
                with self.subTest(tool=tool, path=path):
                    payload = {"tool_name": tool, "tool_input": {"file_path": path}}
                    self.assertIn(why, guard.evaluate_payload(payload, self.conf))
        nb = {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": f"{self.main}/x.ipynb"}}
        self.assertIn("outside", guard.evaluate_payload(nb, self.conf))

    def test_hook_contract(self):
        ok = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt} && git diff"}}
        self.assertEqual(self.run_hook(ok), (0, ""))
        code, err = self.run_hook({"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt} && git push"}})
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED by issue_fixer_guard", err)
        edit = {"tool_name": "Edit", "tool_input": {"file_path": f"{self.main}/scripts/hooks/issue_fixer_guard.py"}}
        self.assertEqual(self.run_hook(edit)[0], 2)
        edit_ok = {"tool_name": "Write", "tool_input": {"file_path": f"{self.wt}/tests/test_new.py"}}
        self.assertEqual(self.run_hook(edit_ok)[0], 0)
        # The guard resolves the main checkout also when the session runs inside a worktree
        self.assertEqual(self.run_hook(edit, project_dir=self.wt)[0], 2)
        # Tools outside its matcher pass through
        self.assertEqual(self.run_hook({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}})[0], 0)
        # Fail closed on malformed input or an unresolvable repository
        self.assertEqual(self.run_hook("not json")[0], 2)
        self.assertEqual(self.run_hook("[1, 2]")[0], 2)
        self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {}})[0], 2)
        plain = tempfile.mkdtemp()
        try:
            self.assertEqual(self.run_hook(ok, project_dir=plain)[0], 2)
        finally:
            os.rmdir(plain)

    def test_runs_as_a_script(self):
        script = REPO_ROOT / "scripts" / "hooks" / "issue_fixer_guard.py"
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.main)
        for command, expected in ((f"cd {self.wt} && gh issue list", 2), (f"cd {self.wt} && git status", 0)):
            payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
            res = subprocess.run([sys.executable, str(script)], input=payload, capture_output=True, text=True,
                                 env=env)
            self.assertEqual(res.returncode, expected, command)

    # ----- #129: denylist and environment allowlist -----
    def test_launchers_builtins_and_archivers_are_denied(self):
        for prog in ("setsid", "flock", "ionice", "chrt", "taskset", "unshare", "nsenter", "chroot", "runuser",
                     "sg", "faketime", "tar", "zip", "unzip", "patch", "ln", "export", "declare", "typeset",
                     "alias", "set", "trap", "shopt", "builtin", "eval", "exec", "source", "."):
            with self.subTest(prog=prog):
                self.assertIn("not available", guard.evaluate(f"cd {self.wt} && {prog} x", self.conf))

    def test_environment_variables_need_the_allowlist(self):
        for name in sorted(guard.ENV_ALLOWED):
            with self.subTest(name=name):
                self.assertEqual(guard.evaluate(f"cd {self.wt} && {name}=1 git status", self.conf), "")
                self.assertEqual(guard.evaluate(f"cd {self.wt} && env {name}=1 git status", self.conf), "")
        for name in ("GIT_PAGER", "PAGER", "EDITOR", "PYTHONPATH", "PYTHONSTARTUP", "GIT_CONFIG_COUNT",
                     "BASH_ENV", "LD_PRELOAD", "CDPATH"):
            with self.subTest(name=name):
                self.assertIn(f"environment variable {name}",
                              guard.evaluate(f"cd {self.wt} && {name}=x git status", self.conf))
                self.assertIn(f"environment variable {name}",
                              guard.evaluate(f"cd {self.wt} && env {name}=x git status", self.conf))
                self.assertIn("only assigns", guard.evaluate(f"cd {self.wt} && {name}=x && git status", self.conf))

    def test_quoting_analysis_agrees_with_the_shell_split(self):
        cases = ['cd {wt} && grep -n "a b" \'c d\' e\\ f', "cd {wt} && echo ''", 'cd {wt} && echo "x\\"y"',
                 "cd {wt} && ls>/dev/null 2>&1|tail -1"]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                prepared = guard._unquoted_newlines_to_separators(self.fmt(cmd))
                self.assertEqual([w.text for w in guard._scan_words(prepared)], guard._shlex_tokens(prepared))
                self.assertEqual(guard.evaluate(self.fmt(cmd), self.conf), "")
        # `$` inside single quotes is literal; inside double quotes or escaped it is checked or literal
        self.assertEqual(guard.evaluate(f"cd {self.wt} && grep -n '$HOME' scripts", self.conf), "")
        self.assertEqual(guard.evaluate(f"cd {self.wt} && grep -n \\$HOME scripts", self.conf), "")
        self.assertIn("expansion", guard.evaluate(f'cd {self.wt} && grep -n "$HOME" scripts', self.conf))

    # ----- #129.8: protected files -----
    def test_running_guard_and_work_files_are_protected(self):
        copy = os.path.realpath(os.path.join(self.wt, "scripts", "hooks", "issue_fixer_guard.py"))

        def edit(path, tool="Edit"):
            return {"tool_name": tool, "tool_input": {"file_path": path}}

        bash_writes = ("sed -i s/a/b/ scripts/hooks/issue_fixer_guard.py", "echo x > scripts/hooks/issue_fixer_guard.py",
                       "cp a scripts/hooks/issue_fixer_guard.py", "rm scripts/hooks/issue_fixer_guard.py")
        # Session launched from the main checkout: the worktree copy is not the running guard and stays editable
        with mock.patch.object(guard, "RUNNING_GUARD", os.path.join(self.main, "scripts", "hooks",
                                                                    "issue_fixer_guard.py")):
            self.assertEqual(guard.evaluate_payload(edit(copy), self.conf), "")
            for cmd in bash_writes:
                self.assertEqual(guard.evaluate(f"cd {self.wt} && {cmd}", self.conf), "", cmd)
        # Session launched from the worktree: its copy is the running guard
        with mock.patch.object(guard, "RUNNING_GUARD", copy):
            for tool in ("Edit", "Write", "MultiEdit"):
                self.assertIn("running guard", guard.evaluate_payload(edit(copy, tool), self.conf))
            for cmd in bash_writes:
                self.assertIn("running guard", guard.evaluate(f"cd {self.wt} && {cmd}", self.conf), cmd)
        for name in ("guard_heartbeat.json", "fixer_binding.json"):
            for tree in (self.wt, self.wt2):
                path = os.path.join(tree, "logs", "issue_work", name)
                for tool in ("Edit", "Write", "MultiEdit"):
                    self.assertIn("written only by", guard.evaluate_payload(edit(path, tool), self.conf))
            self.assertIn("written only by", guard.evaluate(f"cd {self.wt} && tee -a logs/issue_work/{name}",
                                                            self.conf))
        # Reading them stays allowed
        self.assertEqual(guard.evaluate(f"cd {self.wt} && cat logs/issue_work/fixer_binding.json", self.conf), "")

    # ----- #129.6: binding -----
    def _bind(self, tree, session_id=None, worktree=None, issue=8):
        work = os.path.join(tree, "logs", "issue_work")
        os.makedirs(work, exist_ok=True)
        marker = os.path.join(work, "fixer_binding.json")
        Path(marker).write_text(json.dumps({"issue": issue, "worktree": worktree or tree,
                                            "branch": f"fix/issue-{issue}-y", "created_ts": 1,
                                            "session_id": session_id}))
        self.addCleanup(lambda: os.path.exists(marker) and os.remove(marker))
        return marker

    def test_binding_marker_is_claimed_by_the_first_session(self):
        marker = self._bind(self.wt2)
        bash = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt2} && git status"}}

        def bound():
            return json.loads(Path(marker).read_text())["session_id"]

        # No session id: no claim and no session check
        self.assertEqual(self.run_hook(bash), (0, ""))
        self.assertIsNone(bound())
        # A denied call never claims
        self.assertEqual(self.run_hook({"tool_name": "Bash", "session_id": "sess-A",
                                        "tool_input": {"command": f"cd {self.wt2} && git push"}})[0], 2)
        self.assertIsNone(bound())
        # The first allowed call with a session id claims the worktree; the same session keeps working
        self.assertEqual(self.run_hook(dict(bash, session_id="sess-A"))[0], 0)
        self.assertEqual(bound(), "sess-A")
        self.assertEqual(self.run_hook(dict(bash, session_id="sess-A"))[0], 0)
        # Another session is denied, through Bash and the edit tools
        code, err = self.run_hook(dict(bash, session_id="sess-B"))
        self.assertEqual(code, 2)
        self.assertIn("bound to another session", err)
        write = {"tool_name": "Write", "tool_input": {"file_path": f"{self.wt2}/tests/test_x.py"},
                 "session_id": "sess-B"}
        self.assertIn("bound to another session", guard.evaluate_payload(write, self.conf))
        self.assertEqual(guard.evaluate_payload(dict(write, session_id="sess-A"), self.conf), "")
        self.assertEqual(bound(), "sess-A")
        # A marker that names another worktree, or an unreadable one, denies (fail closed)
        self._bind(self.wt2, worktree=self.wt)
        self.assertIn("names another worktree", guard.evaluate_payload(bash, self.conf))
        Path(marker).write_text("{not json")
        self.assertIn("unreadable binding marker", guard.evaluate_payload(bash, self.conf))
        Path(marker).write_text("[1]")
        self.assertIn("malformed binding marker", guard.evaluate_payload(bash, self.conf))

    def test_legacy_worktree_without_marker_is_accepted_unbound(self):
        marker = os.path.join(self.wt, "logs", "issue_work", "fixer_binding.json")
        self.assertFalse(os.path.exists(marker))
        for sid in ("sess-X", "sess-Y", None):
            payload = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt} && git status"}}
            if sid:
                payload["session_id"] = sid
            self.assertEqual(self.run_hook(payload)[0], 0)
        self.assertFalse(os.path.exists(marker))

    # ----- #129.7: heartbeat -----
    def test_heartbeat_written_by_main_on_every_decision(self):
        hb = os.path.join(self.wt, "logs", "issue_work", "guard_heartbeat.json")
        if os.path.exists(hb):
            os.remove(hb)
        ok = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt} && git status"}, "session_id": "s1"}
        # evaluate and evaluate_payload stay pure
        self.assertEqual(guard.evaluate(ok["tool_input"]["command"], self.conf), "")
        self.assertEqual(guard.evaluate_payload(ok, self.conf), "")
        self.assertFalse(os.path.exists(hb))
        before = int(time.time())
        self.assertEqual(self.run_hook(ok), (0, ""))
        beat = json.loads(Path(hb).read_text())
        self.assertEqual({k: beat[k] for k in ("tool", "decision", "reason", "session_id")},
                         {"tool": "Bash", "decision": "allow", "reason": None, "session_id": "s1"})
        self.assertGreaterEqual(beat["ts"], before)
        self.assertEqual(self.run_hook({"tool_name": "Bash",
                                        "tool_input": {"command": f"cd {self.wt} && git push"}})[0], 2)
        beat = json.loads(Path(hb).read_text())
        self.assertEqual(beat["decision"], "deny")
        self.assertIn("read-only git", beat["reason"])
        self.assertIsNone(beat["session_id"])
        self.assertEqual(self.run_hook({"tool_name": "Write", "tool_input": {"file_path": f"{self.wt}/.claude/x"}})[0],
                         2)
        self.assertEqual(json.loads(Path(hb).read_text())["tool"], "Write")
        # No worktree resolved: nothing is written
        os.remove(hb)
        self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {"command": "git status"}})[0], 2)
        self.assertFalse(os.path.exists(hb))
        # A write error never changes the decision
        with mock.patch.object(guard, "_atomic_write_json", side_effect=OSError("disk full")):
            self.assertEqual(self.run_hook(ok), (0, ""))
            self.assertEqual(self.run_hook(dict(ok, tool_input={"command": f"cd {self.wt} && git push"}))[0], 2)

    def test_main_denies_when_the_evaluation_crashes(self):
        hb = os.path.join(self.wt, "logs", "issue_work", "guard_heartbeat.json")
        ok = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt} && git status"}, "session_id": "s1"}
        self.assertEqual(self.run_hook(ok), (0, ""))
        with mock.patch.object(guard, "evaluate_payload", side_effect=RuntimeError("boom")):
            code, err = self.run_hook(ok)
        self.assertEqual(code, 2)
        self.assertIn("internal guard error (RuntimeError: boom)", err)
        self.assertIn("BLOCKED by issue_fixer_guard", err)
        beat = json.loads(Path(hb).read_text())
        self.assertEqual((beat["decision"], beat["session_id"]), ("deny", "s1"))

    def check_guard(self, tree, since):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = iw.main(["check-guard", tree, "--since", str(since)])
        return code, json.loads(buf.getvalue())

    def _heartbeat(self, tree):
        hb = Path(tree, "logs", "issue_work", "guard_heartbeat.json")
        if hb.exists():
            hb.unlink()
        self.addCleanup(lambda: hb.exists() and hb.unlink())
        return hb

    def _key(self, issue=8):
        keys = os.path.join(self.main, "logs", "issue_work_keys")
        os.makedirs(keys, exist_ok=True)
        path = os.path.join(keys, f"{issue}.key")
        Path(path).write_text("ab" * 32 + "\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_check_guard_reads_the_guard_heartbeat(self):
        # A legacy worktree (no marker, no key; issue 7 from its -wt-issue-7 name) passes on the timestamp alone
        hb = self._heartbeat(self.wt)
        legacy = {"signed": False, "sig_ok": None, "issue": 7}
        self.assertEqual(self.check_guard(self.wt, 100),
                         (2, {"ok": False, "heartbeat_ts": None, "since": 100, "session_id": None,
                              "binding_claim": None, "marker_session_id_null": None, **legacy}))
        guard.write_heartbeat(self.wt, "Bash", "", "s1")
        ts = json.loads(hb.read_text())["ts"]
        self.assertEqual(self.check_guard(self.wt, ts),
                         (0, {"ok": True, "heartbeat_ts": ts, "since": ts, "session_id": "s1",
                              "binding_claim": "n/a", "marker_session_id_null": None, **legacy}))
        self.assertEqual(self.check_guard(self.wt, ts + 1)[0], 2)  # stale: written before the fixer launch
        hb.write_text("{bad")
        self.assertEqual(self.check_guard(self.wt, 0)[0], 2)
        # Outside a repository the key cannot be looked up: fail closed
        plain = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", plain], check=False)
        guard.write_heartbeat(plain, "Bash", "", "s1")
        code, out = self.check_guard(plain, 0)
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        self.assertIn("cannot resolve the main checkout", out["warning"])

    # ----- #230: signed heartbeat -----
    def test_signed_heartbeat_verifies_and_tampering_fails(self):
        self._bind(self.wt2)
        self._key(8)
        hb = self._heartbeat(self.wt2)
        before = int(time.time())
        ok = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt2} && git status"}, "session_id": "sess-A"}
        self.assertEqual(self.run_hook(ok), (0, ""))
        beat = json.loads(hb.read_text())
        self.assertEqual((beat["signed"], beat["issue"], beat["binding_claim"], beat["worktree"]),
                         (True, 8, "claimed", os.path.realpath(self.wt2)))
        self.assertEqual(beat["sig"], iw.heartbeat_signature(bytes.fromhex("ab" * 32), beat))
        code, out = self.check_guard(self.wt2, before)
        self.assertEqual(code, 0, out)
        self.assertEqual((out["signed"], out["sig_ok"], out["binding_claim"], out["issue"]), (True, True, "claimed", 8))
        self.assertNotIn("warning", out)
        self.assertEqual(self.run_hook(ok), (0, ""))
        beat = json.loads(hb.read_text())
        self.assertEqual(beat["binding_claim"], "already_bound")
        self.assertEqual(self.check_guard(self.wt2, before)[0], 0)
        # Any signed field changed, a missing signature or a signature made with another key fails (exit 2)
        tampered = [dict(beat, **{field: value}) for field, value in (
            ("ts", beat["ts"] + 60), ("decision", "deny"), ("session_id", "sess-X"), ("binding_claim", "n/a"),
            ("issue", 7), ("worktree", self.wt))]
        unsigned = {k: v for k, v in beat.items() if k != "sig"}
        tampered += [unsigned, dict(unsigned, signed=False),
                     dict(beat, sig=guard.heartbeat_signature(b"\x01" * 32, beat)), dict(beat, sig="é")]
        for forged in tampered:
            with self.subTest(forged=forged):
                hb.write_text(json.dumps(forged))
                code, out = self.check_guard(self.wt2, before)
                self.assertEqual(code, 2)
                self.assertFalse(out["sig_ok"])
                self.assertIn("heartbeat signature missing or invalid", out["warning"])
        # A heartbeat written by an old guard (no signature) in a worktree that has a key fails too
        hb.write_text(json.dumps({"ts": int(time.time()), "tool": "Bash", "decision": "allow", "reason": None,
                                  "session_id": "sess-A"}))
        self.assertEqual(self.check_guard(self.wt2, before)[0], 2)
        # The guard and check-guard sign the same fields the same way
        self.assertEqual(guard.SIGNED_FIELDS, iw.SIGNED_FIELDS)
        self.assertEqual(guard.KEYS_DIR, iw.KEYS_DIR.replace(os.sep, "/"))
        self.assertEqual(guard.heartbeat_signature(b"k", beat), iw.heartbeat_signature(b"k", beat))

    def test_heartbeat_is_unsigned_without_marker_or_key(self):
        hb = self._heartbeat(self.wt2)
        ok = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt2} && git status"}, "session_id": "sess-A"}
        # Key but no marker: legacy worktree, unsigned
        self._key(8)
        self.assertEqual(self.run_hook(ok), (0, ""))
        beat = json.loads(hb.read_text())
        self.assertEqual((beat["signed"], beat["issue"], beat["binding_claim"]), (False, None, "n/a"))
        self.assertNotIn("sig", beat)
        # Marker but no key: unsigned, and check-guard passes on the timestamp (no key to verify against)
        self._bind(self.wt2, issue=9)
        self.assertEqual(self.run_hook(ok), (0, ""))
        beat = json.loads(hb.read_text())
        self.assertEqual((beat["signed"], beat["issue"]), (False, 9))
        code, out = self.check_guard(self.wt2, beat["ts"])
        self.assertEqual((code, out["signed"], out["sig_ok"], out["issue"]), (0, False, None, 9))
        # A malformed key never changes the decision; the heartbeat stays unsigned and check-guard fails
        Path(self._key(9)).write_text("not hex\n")
        self.assertEqual(self.run_hook(ok), (0, ""))
        self.assertFalse(json.loads(hb.read_text())["signed"])
        code, out = self.check_guard(self.wt2, 0)
        self.assertEqual(code, 2)
        self.assertIn("unreadable heartbeat key", out["warning"])

    # ----- #230: heartbeat keys are not readable through Read / Grep / Glob -----
    def test_heartbeat_keys_are_unreadable(self):
        key = self._key(8)
        keys = os.path.dirname(key)
        link = os.path.join(self.wt, "tests", "key_link")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(key, link)
        self.addCleanup(os.remove, link)
        denied = [
            ("Read", {"file_path": key}),
            ("Read", {"file_path": f"{self.main}/logs/x/../issue_work_keys/8.key"}),
            ("Read", {"file_path": link}),
            ("Grep", {"pattern": ".", "path": keys}),
            ("Grep", {"pattern": ".", "path": key}),
            ("Grep", {"pattern": ".", "path": f"{self.main}/logs", "glob": "*.key"}),
            ("Grep", {"pattern": ".", "path": self.main, "glob": "**/*"}),
            ("Grep", {"pattern": ".", "glob": "*.key"}),  # no path: the session's directory, the main checkout
            ("Grep", {"pattern": ".", "path": self.wt, "glob": "../trading/logs/issue_work_keys/*"}),
            ("Glob", {"pattern": "*.key", "path": keys}),
            ("Glob", {"pattern": f"{keys}/*"}),
            ("Glob", {"pattern": "logs/issue_work_keys/*.key"}),
            # #250: a Grep or Glob rooted at an ancestor of the keys is denied with or without a glob (these two
            # rows were allowed before, relying on .gitignore)
            ("Grep", {"pattern": "x", "path": self.main}),
            ("Glob", {"pattern": "**/*.md"}),
            ("Grep", {"pattern": ".", "path": f"{self.main}/logs"}),
            ("Glob", {"pattern": "*.py", "path": self.main}),
            ("Grep", {"pattern": ".", "path": self.tmp}),
            ("Glob", {"pattern": "*.md", "path": "/"}),
            ("Grep", {"pattern": ".", "path": f"{self.main}/scripts/.."}),
            # #256: a Grep glob that can match <N>.key is denied outside a linked worktree; braces do not parse
            ("Grep", {"pattern": ".", "path": "/etc", "glob": "*"}),
            ("Grep", {"pattern": ".", "path": "/etc", "glob": "**/*.key"}),
            ("Grep", {"pattern": ".", "path": "/etc", "glob": "*.{py,md}"}),
        ]
        for tool, tool_input in denied:
            with self.subTest(tool=tool, tool_input=tool_input):
                payload = {"tool_name": tool, "tool_input": tool_input, "cwd": self.main}
                self.assertIn("heartbeat keys", guard.evaluate_payload(payload, self.conf))
                code, err = self.run_hook(payload)
                self.assertEqual(code, 2)
                self.assertIn("heartbeat keys", err)
        # Every other read passes, and reads neither claim a binding nor write a heartbeat
        hb = self._heartbeat(self.wt)
        allowed = [
            ("Read", {"file_path": "/etc/hosts"}),
            ("Read", {"file_path": f"{self.main}/README.md"}),
            ("Read", {"file_path": f"{self.wt}/README.md"}),
            ("Grep", {"pattern": "x", "path": self.wt, "glob": "*.py"}),
            ("Grep", {"pattern": "issue_work_keys", "path": self.wt}),
            ("Glob", {"pattern": "**/*.py", "path": self.wt}),
            ("Grep", {"pattern": "x", "path": f"{self.wt}/scripts", "glob": "**/test_*.py"}),
            ("Grep", {"pattern": "x", "path": self.wt2, "glob": "*.md"}),
            ("Glob", {"pattern": "*", "path": self.wt}),
            ("Grep", {"pattern": "x", "path": self.wt}),
            # #256: inside a linked worktree (which cannot hold the keys) any Grep glob passes; these three rows
            # were denied by #250 whatever the root
            ("Grep", {"pattern": ".", "path": self.wt, "glob": "*"}),
            ("Grep", {"pattern": ".", "path": self.wt, "glob": "**/*.key"}),
            ("Grep", {"pattern": ".", "path": self.wt, "glob": "*.{py,md}"}),
        ]
        for tool, tool_input in allowed:
            with self.subTest(tool=tool, tool_input=tool_input):
                payload = {"tool_name": tool, "tool_input": tool_input, "cwd": self.main, "session_id": "s1"}
                self.assertEqual(self.run_hook(payload), (0, ""))
        self.assertFalse(hb.exists())

    # ----- #250: case variants (DrvFs is case-insensitive) and other names of a protected path -----
    def test_case_variants_of_protected_paths_are_denied(self):
        self._key(8)
        for tool, tool_input in (
                ("Read", {"file_path": f"{self.main}/LOGS/Issue_Work_Keys/8.key"}),
                ("Read", {"file_path": f"{self.main}/logs/ISSUE_WORK_KEYS/8.key"}),
                ("Grep", {"pattern": ".", "path": f"{self.main}/Logs/Issue_Work_Keys"}),
                ("Glob", {"pattern": "LOGS/Issue_Work_Keys/*"}),
                ("Grep", {"pattern": ".", "path": "/etc", "glob": "*.KEY"})):
            with self.subTest(tool=tool, tool_input=tool_input):
                payload = {"tool_name": tool, "tool_input": tool_input, "cwd": self.main}
                self.assertIn("heartbeat keys", guard.evaluate_payload(payload, self.conf))

        def edit(path):
            return guard.evaluate_payload({"tool_name": "Write", "tool_input": {"file_path": path}}, self.conf)

        for path, why in ((f"{self.wt}/LOGS/session_state.json", "logs/issue_work/"),
                          (f"{self.wt}/Logs/x", "logs/issue_work/"),
                          (f"{self.wt}/.GIT/config", "git internals"),
                          (f"{self.wt}/scripts/.Git/hooks/x", "git internals"),
                          (f"{self.wt}/.CLAUDE/agents/issue_fixer.md", ".claude/"),
                          (f"{self.wt}/.Agents/Hooks.json", "hook configuration"),
                          (f"{self.wt}/logs/ISSUE_WORK/guard_heartbeat.json", "written only by"),
                          (f"{self.wt}/Logs/Issue_Work/Fixer_Binding.json", "written only by")):
            with self.subTest(path=path):
                self.assertIn(why, edit(path))
        self.assertIn("logs/issue_work/", guard.evaluate(f"cd {self.wt} && echo x > LOGS/x", self.conf))
        # Normal names stay writable
        for path in (f"{self.wt}/logs/issue_work/notes.md", f"{self.wt}/.agents/agents/x/agent.md",
                     f"{self.wt}/scripts/logs_helper.py", f"{self.wt}/tests/Logs/x.py"):
            with self.subTest(path=path):
                self.assertEqual(edit(path), "")
        # The running guard under another case, or another name of the same file (st_dev / st_ino)
        copy = os.path.join(self.wt, "scripts", "hooks", "issue_fixer_guard.py")  # committed by _make_repo
        alias = os.path.join(self.wt, "scripts", "hooks", "guard_alias.py")
        os.link(copy, alias)
        self.addCleanup(os.remove, alias)
        with mock.patch.object(guard, "RUNNING_GUARD", os.path.realpath(copy)):
            self.assertIn("running guard", edit(os.path.join(self.wt, "scripts", "hooks", "Issue_Fixer_Guard.py")))
            self.assertIn("running guard", edit(alias))
        self.assertEqual(edit(alias), "")

    def test_another_name_of_the_keys_directory_is_denied(self):
        keys = os.path.realpath(os.path.dirname(self._key(8)))
        alias = os.path.join(os.path.dirname(keys), "KEYS~1")
        real_same = guard._same_file

        def same_file(a, b):  # what a short name or a case variant looks like on DrvFs: same st_dev / st_ino
            pair = {os.path.realpath(a), os.path.realpath(b)}
            return pair == {alias, keys} or real_same(a, b)

        with mock.patch.object(guard, "_same_file", side_effect=same_file):
            for tool, tool_input in (("Read", {"file_path": f"{alias}/8.key"}),
                                     ("Grep", {"pattern": ".", "path": alias})):
                with self.subTest(tool=tool):
                    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": self.main}
                    self.assertIn("heartbeat keys", guard.evaluate_payload(payload, self.conf))
        payload = {"tool_name": "Read", "tool_input": {"file_path": f"{alias}/8.key"}, "cwd": self.main}
        self.assertEqual(guard.evaluate_payload(payload, self.conf), "")

    def test_grep_glob_that_can_match_a_key(self):
        for glob in ("*", "**/*", "*.key", "**/*.key", "8.key", "250.key", "1?.key", "*.k*", "[0-9].key", "*.KEY",
                     "issue_work_keys", "*_keys", "*e*", "*.{py,key}", "{a,b}.py", "!*.py", "logs/", "[abc",
                     "x\\", "../trading/logs/*.key"):
            with self.subTest(glob=glob):
                self.assertTrue(guard._glob_can_match_key(glob))
        for glob in ("*.py", "**/*.py", "test_*.py", "*.md", "*.json", "README.md", "9.keys", "key.py",
                     "\\*.key", "[!k]*.py", "scripts/**/*.txt"):
            with self.subTest(glob=glob):
                self.assertFalse(guard._glob_can_match_key(glob))

    # ----- #250: reads spawn git only for the rules that need the main checkout -----
    def test_ordinary_reads_spawn_no_git(self):
        self._key(8)
        real_run = subprocess.run
        calls = []

        def spy(cmd, *args, **kwargs):
            if cmd and cmd[0] == "git":
                calls.append(cmd)
            return real_run(cmd, *args, **kwargs)

        ordinary = [
            ("Read", {"file_path": f"{self.wt}/README.md"}),
            ("Read", {"file_path": f"{self.main}/README.md"}),
            ("Read", {"file_path": "/etc/hosts"}),
            ("Read", {"file_path": f"{self.main}/logs/issue_work_keys/8.key"}),  # denied without git
            ("Grep", {"pattern": "x", "path": self.wt, "glob": "*.py"}),
            ("Grep", {"pattern": "x", "path": f"{self.wt}/scripts"}),
            ("Glob", {"pattern": "**/*.py", "path": self.wt}),
            ("Grep", {"pattern": "x", "path": self.main}),  # holds the keys: denied without git
            # #256: also with the session's cwd inside the worktree, and with any glob
            ("Grep", {"pattern": "x", "path": self.wt}),
            ("Grep", {"pattern": "x", "path": self.wt, "glob": "*"}),
            ("Glob", {"pattern": "*", "path": self.wt}),
            ("Glob", {"pattern": f"{self.wt}/scripts/*"}),
        ]
        with mock.patch.object(guard.subprocess, "run", side_effect=spy):
            for cwd in (self.main, self.wt):
                for tool, tool_input in ordinary:
                    with self.subTest(cwd=cwd, tool=tool, tool_input=tool_input):
                        self.run_hook({"tool_name": tool, "tool_input": tool_input, "cwd": cwd})
                        self.assertEqual(calls, [])
            # No path: the session's directory, the worktree
            for tool_input in ({"pattern": "x"}, {"pattern": "x", "glob": "*.{py,md}"}):
                with self.subTest(tool_input=tool_input):
                    payload = {"tool_name": "Grep", "tool_input": tool_input, "cwd": f"{self.wt}/scripts"}
                    self.assertEqual(self.run_hook(payload), (0, ""))
                    self.assertEqual(calls, [])
            # A root outside any linked worktree needs the main checkout; so do Bash and the edit tools
            self.assertEqual(self.run_hook({"tool_name": "Grep", "tool_input": {"pattern": ".", "path": self.tmp},
                                            "cwd": self.main})[0], 2)
            self.assertTrue(calls)
            calls.clear()
            self.assertEqual(self.run_hook({"tool_name": "Bash",
                                            "tool_input": {"command": f"cd {self.wt} && git status"}})[0], 0)
            self.assertTrue(calls)
        # Unresolvable repository: an ordinary read passes, a read that needs the main checkout is denied
        plain = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, plain)
        read = {"tool_name": "Read", "tool_input": {"file_path": f"{self.wt}/README.md"}, "cwd": self.main}
        self.assertEqual(self.run_hook(read, project_dir=plain), (0, ""))
        for payload in ({"tool_name": "Grep", "tool_input": {"pattern": "x"}},
                        {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
                        {"tool_name": "Glob", "tool_input": {"pattern": "*", "path": "/"}, "cwd": self.main}):
            with self.subTest(payload=payload):
                code, err = self.run_hook(payload, project_dir=plain)
                self.assertEqual(code, 2)
                self.assertIn("cannot resolve the repository", err)

    # ----- #256: Grep globs inside the worktree, submodules, Glob patterns that leave their root -----
    def _evaluate_read(self, tool, tool_input, cwd):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}
        reason = guard.evaluate_payload(payload, self.conf)
        self.assertEqual(self.run_hook(payload)[0], 2 if reason else 0)
        return reason

    def test_grep_globs_inside_the_worktree_pass(self):
        keys = os.path.dirname(self._key(8))
        outside = tempfile.mkdtemp()  # not a worktree, not an ancestor of the keys
        self.addCleanup(os.rmdir, outside)
        globs = ("*", "**/*", "scripts/**", "*.{py,md}", "**/*.key")
        for cwd in (self.main, self.wt):
            for root in (self.wt, f"{self.wt}/scripts"):
                for glob in globs:
                    with self.subTest(cwd=cwd, root=root, glob=glob):
                        self.assertEqual(self._evaluate_read("Grep", {"pattern": "x", "path": root, "glob": glob},
                                                             cwd), "")
            # The keys directory, under it or an ancestor: denied whatever the glob
            for root in (keys, f"{keys}/8.key", f"{keys}/sub", self.main, f"{self.main}/logs", self.tmp):
                for glob in ("*", "*.py"):
                    with self.subTest(cwd=cwd, root=root, glob=glob):
                        self.assertIn("heartbeat keys", self._evaluate_read(
                            "Grep", {"pattern": "x", "path": root, "glob": glob}, cwd))
            # Outside any linked worktree a glob that can match <N>.key is denied; a narrow one passes
            for glob in globs:
                with self.subTest(cwd=cwd, root=outside, glob=glob):
                    self.assertIn("heartbeat keys", self._evaluate_read(
                        "Grep", {"pattern": "x", "path": outside, "glob": glob}, cwd))
            self.assertEqual(self._evaluate_read("Grep", {"pattern": "x", "path": outside, "glob": "*.py"}, cwd), "")

    def test_submodule_git_file_is_not_a_linked_worktree(self):
        sup = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", sup], check=False)
        os.makedirs(os.path.join(sup, ".git", "modules", "sub"))
        os.makedirs(os.path.join(sup, "sub", "src"))
        Path(sup, "sub", ".git").write_text("gitdir: ../.git/modules/sub\n")
        os.makedirs(os.path.join(sup, "odd"))
        Path(sup, "odd", ".git").write_text("not a pointer\n")
        for path in (os.path.join(sup, "sub"), os.path.join(sup, "sub", "src"), os.path.join(sup, "odd"), sup):
            with self.subTest(path=path):
                self.assertFalse(guard._in_linked_worktree(path))
                self.assertEqual(guard._linked_worktree_top(path), "")
        for path in (self.wt, os.path.join(self.wt, "scripts")):
            with self.subTest(path=path):
                self.assertTrue(guard._in_linked_worktree(path))
                self.assertEqual(guard._linked_worktree_top(path), os.path.realpath(self.wt))
        self.assertEqual(guard._linked_worktree_top(self.main), "")
        # A submodule root gets no fast path: git runs, and a glob that can match <N>.key is denied there
        real_run = subprocess.run
        calls = []

        def spy(cmd, *args, **kwargs):
            if cmd and cmd[0] == "git":
                calls.append(cmd)
            return real_run(cmd, *args, **kwargs)

        sub = os.path.join(sup, "sub")
        with mock.patch.object(guard.subprocess, "run", side_effect=spy):
            code, err = self.run_hook({"tool_name": "Grep", "tool_input": {"pattern": "x", "path": sub, "glob": "*"},
                                       "cwd": self.main})
        self.assertEqual(code, 2)
        self.assertIn("heartbeat keys", err)
        self.assertTrue(calls)

    def test_glob_pattern_that_leaves_its_root_is_denied(self):
        self._key(8)
        for cwd in (self.main, self.wt):
            for tool_input in ({"pattern": "../trading/logs/*/*.key", "path": self.wt},
                               {"pattern": "**/../../trading/logs/*/*", "path": self.wt},
                               {"pattern": "scripts/../../trading/logs/*/8.key", "path": self.wt},
                               {"pattern": "..\\trading\\logs\\*\\*.key", "path": self.wt},
                               {"pattern": "{scripts,/etc}/*", "path": self.wt},
                               # brace expansion runs before the path split: `..` or `/` inside a group
                               {"pattern": "{.,..}/trading/logs/*/*.key", "path": self.wt},
                               {"pattern": "{..,x}/trading/logs/*/*.key", "path": self.wt},
                               {"pattern": "{.,x}{.,y}/trading/logs/*/*.key", "path": self.wt},
                               {"pattern": "{x,{y,..}}/trading/logs/*/*", "path": self.wt},
                               {"pattern": "{a,{b,/etc}}/*", "path": self.wt},
                               {"pattern": "{/etc,x}/*", "path": self.wt},
                               {"pattern": "{a,b}" * 7 + "/*", "path": self.wt},  # past the expansion limit
                               # an absolute pattern ignores `path`: rooted at its directory before the wildcard
                               {"pattern": f"{self.main}/logs/*/8.key", "path": self.wt},
                               {"pattern": f"{self.tmp}/*/logs/*/*.key", "path": self.wt}):
                with self.subTest(cwd=cwd, tool_input=tool_input):
                    self.assertIn("heartbeat keys", self._evaluate_read("Glob", tool_input, cwd))
            for tool_input in ({"pattern": "**/*.py", "path": self.wt},
                               {"pattern": "scripts/**", "path": self.wt},
                               {"pattern": "notes..md", "path": self.wt},
                               {"pattern": "*.{py,md}", "path": self.wt},
                               {"pattern": "test_{a,b}.py", "path": self.wt},
                               {"pattern": "*{.py,.md}", "path": self.wt},
                               {"pattern": "a{1..3}.py", "path": self.wt},
                               {"pattern": "{scripts,tests}/**/*.py", "path": self.wt},
                               {"pattern": f"{self.wt}/{{scripts,tests}}/*.py"},
                               {"pattern": f"{self.wt}/scripts/*.py"},
                               {"pattern": f"{self.wt}/**/*.py", "path": self.main}):
                with self.subTest(cwd=cwd, tool_input=tool_input):
                    self.assertEqual(self._evaluate_read("Glob", tool_input, cwd), "")

    # ----- #250: check-guard keeps the claim history in marker_session_id_null -----
    def test_check_guard_reports_whether_the_marker_was_claimed(self):
        self._heartbeat(self.wt2)
        marker = self._bind(self.wt2)
        ok = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt2} && git status"}}
        before = int(time.time())
        self.assertEqual(self.run_hook(ok), (0, ""))  # no session id: nothing claimed
        out = self.check_guard(self.wt2, before)[1]
        self.assertEqual((out["binding_claim"], out["marker_session_id_null"]), ("n/a", True))
        self.assertEqual(self.run_hook(dict(ok, session_id="sess-A")), (0, ""))
        self.assertIs(self.check_guard(self.wt2, before)[1]["marker_session_id_null"], False)
        # A later call that claims nothing (n/a) no longer hides that the marker was claimed
        self.assertEqual(self.run_hook(ok), (0, ""))
        out = self.check_guard(self.wt2, before)[1]
        self.assertEqual((out["binding_claim"], out["marker_session_id_null"]), ("n/a", False))
        # No marker, or an unreadable one: null
        Path(marker).write_text("[1]")
        self.assertIsNone(self.check_guard(self.wt2, before)[1]["marker_session_id_null"])
        os.remove(marker)
        self.assertIsNone(self.check_guard(self.wt2, before)[1]["marker_session_id_null"])

    def test_generated_fixer_hook_covers_read_tools(self):
        text = (REPO_ROOT / ".claude" / "agents" / "issue_fixer.md").read_text(encoding="utf-8")
        head = text.split("\n---\n", 1)[0]
        matcher = next(ln for ln in head.splitlines() if "matcher:" in ln).split("matcher:", 1)[1].strip()
        self.assertEqual(set(matcher.split("|")),
                         {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep", "Glob"})

    # ----- #230: the heartbeat goes only to the worktree the decision validated -----
    def test_heartbeat_target_is_the_validated_worktree(self):
        hb1, hb2 = self._heartbeat(self.wt), self._heartbeat(self.wt2)

        def call(command, session_id="sess-A"):
            return self.run_hook({"tool_name": "Bash", "tool_input": {"command": command},
                                  "session_id": session_id})[0]

        # Binding denial: wt2 is bound to another session; the denied call leaves wt2's heartbeat alone
        self._bind(self.wt2, session_id="sess-B")
        self.assertEqual(call(f"cd {self.wt2} && git status"), 2)
        self.assertFalse(hb2.exists())
        # Cross-tree denial: the caller is bound to wt; a denied call aimed at unbound wt2 writes nothing there
        self._bind(self.wt, session_id="sess-A", issue=7)
        self._bind(self.wt2)
        self.assertEqual(call(f"cd {self.wt2} && git push"), 2)
        self.assertFalse(hb2.exists())
        self.assertEqual(json.loads(Path(self.wt2, "logs", "issue_work", "fixer_binding.json").read_text())
                         ["session_id"], None)
        # ... nor at a legacy worktree (no marker)
        os.remove(os.path.join(self.wt2, "logs", "issue_work", "fixer_binding.json"))
        self.assertEqual(call(f"cd {self.wt2} && git push"), 2)
        self.assertFalse(hb2.exists())
        # A marker naming another worktree: nothing is written
        self._bind(self.wt2, worktree=self.wt)
        self.assertEqual(call(f"cd {self.wt2} && git push", session_id="sess-C"), 2)
        self.assertFalse(hb2.exists())
        # A deny inside the caller's own bound worktree still writes the heartbeat, also when the command names
        # another worktree
        self.assertEqual(call(f"cd {self.wt} && git push"), 2)
        self.assertEqual(json.loads(hb1.read_text())["decision"], "deny")
        hb1.unlink()
        self.assertEqual(call(f"cd {self.wt} && cat {self.wt2}/README.md"), 2)
        self.assertIn("another issue worktree", json.loads(hb1.read_text())["reason"])
        self.assertFalse(hb2.exists())
        # An allowed call writes to its worktree
        self.assertEqual(call(f"cd {self.wt} && git status"), 0)
        self.assertEqual(json.loads(hb1.read_text())["decision"], "allow")
        self.assertFalse(hb2.exists())

    # ----- #230: a failed claim is recorded and reported -----
    def test_failed_binding_claim_is_recorded(self):
        marker = self._bind(self.wt2)
        hb = self._heartbeat(self.wt2)
        real_write = guard._atomic_write_json

        def marker_write_fails(path, data):
            if path.endswith(guard.BINDING_FILE):
                raise OSError("read-only marker")
            return real_write(path, data)

        ok = {"tool_name": "Bash", "tool_input": {"command": f"cd {self.wt2} && git status"}, "session_id": "sess-A"}
        before = int(time.time())
        with mock.patch.object(guard, "_atomic_write_json", side_effect=marker_write_fails):
            self.assertEqual(self.run_hook(ok), (0, ""))  # the decision never changes
        self.assertIsNone(json.loads(Path(marker).read_text())["session_id"])
        self.assertEqual(json.loads(hb.read_text())["binding_claim"], "failed")
        code, out = self.check_guard(self.wt2, before)
        self.assertEqual((code, out["binding_claim"], out["warning"]), (0, "failed", "binding claim failed"))
        # The statuses of claim_binding
        self.assertEqual(guard.claim_binding("", "sess-A"), "n/a")
        self.assertEqual(guard.claim_binding(self.wt2, None), "n/a")
        self.assertEqual(guard.claim_binding(self.wt, "sess-A"), "n/a")  # legacy: no marker
        self.assertEqual(guard.claim_binding(self.wt2, "sess-A"), "claimed")
        self.assertEqual(guard.claim_binding(self.wt2, "sess-A"), "already_bound")
        Path(marker).write_text("{bad")
        self.assertEqual(guard.claim_binding(self.wt2, "sess-A"), "failed")


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

    # ----- #230: the checks never see the orchestrator's credentials or home directory -----
    CREDENTIALS = {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s", "BINANCE_MCP_OAUTH_PATH": "/x/oauth.json",
                   "NOTION_TOKEN": "n", "GITHUB_TOKEN": "g", "GMAIL_APP_PASSWORD": "p", "ENV_FILE": "/x/.env",
                   "GH_TOKEN": "t", "GH_ENTERPRISE_TOKEN": "e", "GEMINI_API_KEY": "m",
                   # #250: never listed anywhere, dropped by the allowlist
                   "ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "AWS_ACCESS_KEY_ID": "i",
                   "AWS_SECRET_ACCESS_KEY": "w", "CLAUDE_CODE_MESSAGING_TOKEN": "c", "SOME_SERVICE_PASSWORD": "x"}

    def test_checks_run_without_credentials_or_home(self):
        home = os.path.join(self.tmp, "orchestrator-home")
        os.makedirs(home)
        Path(self.repo, "tests", "test_env.py").write_text(
            "import os\nimport unittest\n\n\nclass Env(unittest.TestCase):\n"
            "    def test_scrubbed(self):\n"
            f"        leaked = sorted(k for k in os.environ if k in {sorted(self.CREDENTIALS)!r})\n"
            "        self.assertEqual(leaked, [])\n"
            f"        self.assertNotEqual(os.path.realpath(os.environ['HOME']), {os.path.realpath(home)!r})\n"
            "        self.assertTrue(os.path.isdir(os.environ['HOME']))\n"
            "        self.assertEqual(os.environ.get('TZ'), 'UTC')\n")
        with mock.patch.dict(os.environ, dict(self.CREDENTIALS, HOME=home, TZ="UTC")):
            out = iw.cmd_review_context(self.repo)
        log = Path(self.repo, "logs", "issue_work", "review", "checks.log").read_text()
        self.assertTrue(out["checks_ok"], log)
        self.assertIn("Ran 2 tests", out["checks"]["unittest"])

    def test_check_env_drops_credentials_and_replaces_home(self):
        kept = {"TZ": "UTC", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "LC_CTYPE": "C.UTF-8", "TERM": "dumb",
                "TMPDIR": tempfile.gettempdir(), "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        # #250: an allowlist, so any variable outside it is dropped, not only the listed credentials
        dropped = {"WSL_DISTRO_NAME": "Ubuntu", "XDG_RUNTIME_DIR": "/run/user/1", "USER": "nacho",
                   "GIT_EDITOR": "vi", "MY_PATH": "/x", "HOME_DIR": "/x", "XLC_ALL": "x"}
        with mock.patch.dict(os.environ, dict(self.CREDENTIALS, HOME="/home/orchestrator", **kept, **dropped)):
            env = iw.check_env("/tmp/fresh-home")
            path = os.environ["PATH"]
        self.assertFalse(set(self.CREDENTIALS) & set(env))
        self.assertFalse(set(dropped) & set(env))
        self.assertEqual(env["HOME"], "/tmp/fresh-home")
        self.assertEqual(env["PATH"], path)
        self.assertEqual({k: env[k] for k in kept}, kept)
        self.assertTrue(env["PYTHONUSERBASE"])
        for name in env:
            self.assertTrue(name in {"PATH", "LANG", "TZ", "TERM", "TMPDIR", "HOME"}
                            or name.startswith(("LC_", "PYTHON")), name)
        # The temporary home of a run is removed afterwards
        homes = []
        real_run = subprocess.run

        def spy(cmd, *args, **kwargs):
            if kwargs.get("env") is not None:
                homes.append(kwargs["env"]["HOME"])
                self.assertFalse(set(self.CREDENTIALS) & set(kwargs["env"]))
            return real_run(cmd, *args, **kwargs)

        with mock.patch.dict(os.environ, self.CREDENTIALS), mock.patch.object(iw.subprocess, "run", side_effect=spy):
            iw.cmd_review_context(self.repo)
        self.assertEqual(len(homes), 3)
        self.assertEqual(len(set(homes)), 1)
        self.assertFalse(os.path.exists(homes[0]))


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

    def fake_run(self, issue_state="OPEN", merged=True, title="T", labels=()):
        def run(cmd, cwd=None, check=True, timeout=300):
            if cmd[0] == "gh" and cmd[1:3] == ["issue", "view"]:
                body = {"number": int(cmd[3]), "title": title, "body": "B",
                        "labels": [{"name": n} for n in labels], "state": issue_state, "url": "u"}
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
            # No labels and a neutral title: the deterministic triage falls to build/medium
            self.assertEqual(issue["routing"]["route"], "build")
            self.assertEqual(issue["routing"]["risk"], "medium")
            self.assertEqual(out["routing"], issue["routing"])
            # The fixer binding marker, unclaimed, which the guard accepts and claims for the first session
            marker = json.loads(Path(out["work_dir"], "fixer_binding.json").read_text())
            self.assertIsInstance(marker["created_ts"], int)
            self.assertEqual(marker, {"issue": 7, "worktree": out["worktree"], "branch": "fix/issue-7-fix-thing",
                                      "created_ts": marker["created_ts"], "session_id": None})
            tree = guard.Confinement.from_project_dir(self.repo).worktree_of(out["worktree"])
            guard.check_binding(tree, "sess-A")
            self.assertEqual(guard.claim_binding(tree, "sess-A"), "claimed")
            with self.assertRaises(guard.Denied):
                guard.check_binding(tree, "sess-B")
            # #230: the heartbeat key lives in the main checkout, 0600, 32 random bytes as hex
            key = os.path.join(self.repo, "logs", "issue_work_keys", "7.key")
            self.assertEqual(out["key_file"], key)
            self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)
            self.assertRegex(Path(key).read_text(), r"^[0-9a-f]{64}\n$")
            # ... and the guard signs the worktree's heartbeat with it, which check-guard verifies
            guard.write_heartbeat(tree, "Bash", "", "sess-A", "claimed", os.path.realpath(self.repo))
            check = iw.cmd_check_guard(out["worktree"], 0)
            self.assertEqual((check["ok"], check["signed"], check["sig_ok"], check["issue"]), (True, True, True, 7))
            # A second init for the same issue refuses to reuse the worktree
            with self.assertRaises(iw.WorkspaceError):
                iw.cmd_init(7, "again", "origin/main", cwd=self.repo)
            # Called from inside the worktree, the helper still resolves the main checkout
            self.assertEqual(os.path.realpath(iw.main_repo_root(out["worktree"])), os.path.realpath(self.repo))
            res = iw.cmd_cleanup(7, cwd=self.repo)
        self.assertFalse(os.path.exists(out["worktree"]))
        self.assertEqual(res["deleted_branch"], "fix/issue-7-fix-thing")
        self.assertNotIn("fix/issue-7", _git(self.repo, "branch"))
        self.assertFalse(os.path.exists(key))
        self.assertTrue(res["removed_key"])

    def test_init_routes_risk_gate_issue_to_deep(self):
        with mock.patch.object(iw, "run", self.fake_run(labels=["bug", "cat:risk_gate"])):
            out = iw.cmd_init(10, "gate", "origin/main", cwd=self.repo)
            issue = json.loads(Path(out["issue_file"]).read_text())
            iw.cmd_cleanup(10, force=True, cwd=self.repo)
        self.assertEqual(issue["routing"], {"route": "deep", "risk": "high", "reason": "label cat:risk_gate"})
        self.assertEqual(out["routing"], issue["routing"])

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


def _labels(*names):
    return [{"name": n, "color": "x"} for n in names]


class TestClassifyIssue(unittest.TestCase):
    """Deterministic triage: deep/high, then quick/low, else build/medium (first match wins)."""

    def route(self, title="Neutral title", labels=()):
        out = iw.classify_issue(title, labels)
        self.assertTrue(out["reason"])
        self.assertEqual(out["risk"], {"deep": "high", "quick": "low", "build": "medium"}[out["route"]])
        return out["route"]

    def test_deep_labels(self):
        for label in ("cat:risk_gate", "severity:high", "severity:critical"):
            self.assertEqual(self.route(labels=_labels("bug", label)), "deep", label)
        self.assertEqual(iw.classify_issue("x", _labels("severity:critical"))["reason"], "label severity:critical")

    def test_deep_title_keywords(self):
        for title in ("executor: rejects valid order", "execute_futures_trade crashes on --close-position",
                      "pre_trade_guard denies sync", "Delta gate counts resting entries twice",
                      "guardian loop skips BE", "stop verification retries too few", "Stop-loss verification",
                      "issue_fixer_guard blocks pytest", "pre-trade hook timeout", "Hooks fail on Windows",
                      "Gates relax in TESTNET", "evaluator prompt misses lessons",
                      "isolated_market_evaluator rejects fresh brief"):
            self.assertEqual(self.route(title=title), "deep", title)
        self.assertEqual(iw.classify_issue("Delta gate double count", [])["reason"], "title keyword 'gate'")

    def test_title_word_boundaries(self):
        for title in ("aggregate stats", "safeguard docs", "hooking up colours", "gateway timeout",
                      "executors list"):
            self.assertEqual(self.route(title=title), "build", title)

    def test_quick_rules(self):
        self.assertEqual(self.route(labels=_labels("documentation")), "quick")
        self.assertEqual(self.route(labels=_labels("severity:low", "cat:infra")), "quick")
        self.assertEqual(self.route(labels=_labels("cat:tool_error", "severity:low")), "quick")
        self.assertEqual(iw.classify_issue("x", _labels("severity:low", "cat:infra"))["reason"],
                         "severity:low with cat:infra")
        self.assertEqual(self.route(labels=_labels("severity:low")), "build")
        self.assertEqual(self.route(labels=_labels("cat:infra")), "build")
        self.assertEqual(self.route(labels=_labels("severity:low", "cat:quant_logic")), "build")

    def test_first_match_order(self):
        self.assertEqual(self.route(labels=_labels("documentation", "cat:risk_gate")), "deep")
        self.assertEqual(self.route(title="gate wording", labels=_labels("documentation")), "deep")
        self.assertEqual(self.route(title="guard", labels=_labels("severity:low", "cat:infra")), "deep")

    def test_default_and_malformed_inputs(self):
        self.assertEqual(iw.classify_issue("Neutral", []),
                         {"route": "build", "risk": "medium", "reason": "default (no deep/quick rule matched)"})
        for labels in (None, "cat:risk_gate", [{"nope": 1}, 3], {"name": "cat:risk_gate"}, [{"name": 5}]):
            self.assertEqual(self.route(labels=labels), "build", labels)
        self.assertEqual(self.route(title=None), "build")
        self.assertEqual(self.route(title=42), "build")

    def test_plain_strings_and_case_insensitive_labels(self):
        self.assertEqual(self.route(labels=["Cat:Risk_Gate"]), "deep")
        self.assertEqual(self.route(labels=[" DOCUMENTATION "]), "quick")
        self.assertEqual(self.route(labels=[{"name": "Severity:LOW"}, "CAT:INFRA"]), "quick")


class TestRecordRoute(unittest.TestCase):
    """record-route on a real temp repo with a linked issue worktree; the log must land in the MAIN checkout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_repo(self.tmp)
        self.wt = iw.worktree_path(self.repo, 5)
        _git(self.repo, "worktree", "add", "-q", self.wt, "-b", "fix/issue-5-x")
        self.log = os.path.join(self.repo, "logs", "issue_routing.jsonl")
        self.src = os.path.join(self.wt, "logs", "issue_work", "route_record.json")
        os.makedirs(os.path.dirname(self.src))

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def set_route_auto(self, route):
        Path(self.wt, "logs", "issue_work", "issue.json").write_text(
            json.dumps({"number": 5, "routing": {"route": route, "risk": "x", "reason": "r"}}))

    def record(self, **overrides):
        rec = {"route_final": "build", "upgrade_reason": None,
               "fixer_models": [{"model": "sonnet", "effort": "medium"}, {"model": "opus", "effort": "medium"}],
               "auditor_model": {"model": "opus", "effort": "high"}, "approved_round": 2,
               "escalations": [{"round": 2, "kind": "capability"}], "merged": True}
        rec.update(overrides)
        Path(self.src).write_text(json.dumps(rec))
        return rec

    def main(self, *args):
        real_root = iw.main_repo_root
        buf = io.StringIO()
        # main() resolves the repo from the process cwd: point it at the temp worktree, never the real checkout
        with mock.patch.object(iw, "main_repo_root", lambda cwd=None: real_root(self.wt)), \
                mock.patch("sys.stdout", buf):
            code = iw.main(["record-route", "5", "--from", self.src, *args])
        return code, json.loads(buf.getvalue())

    def lines(self):
        if not os.path.exists(self.log):
            return []
        return Path(self.log).read_text().splitlines()

    def test_valid_record_appends_to_main_checkout(self):
        self.set_route_auto("build")
        self.record()
        out = iw.cmd_record_route(5, self.src, cwd=self.wt)
        self.assertEqual(os.path.realpath(out["log"]), os.path.realpath(self.log))
        self.assertFalse(os.path.exists(os.path.join(self.wt, "logs", "issue_routing.jsonl")))
        lines = self.lines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec, out["record"])
        self.assertEqual(rec["schema"], 1)
        self.assertEqual(rec["issue"], 5)
        self.assertEqual(rec["route_auto"], "build")
        self.assertEqual(rec["route_final"], "build")
        self.assertEqual(rec["rounds"], 2)
        self.assertEqual(rec["approved_round"], 2)
        self.assertEqual(rec["escalations"], [{"round": 2, "kind": "capability"}])
        self.assertTrue(rec["merged"])
        self.assertRegex(rec["recorded_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn("post_merge_bugs", rec)
        self.assertEqual(lines[0], json.dumps(rec, sort_keys=True))
        # A second record appends and keeps the first line intact
        self.record(route_final="deep", upgrade_reason="locator found a gate",
                    fixer_models=[{"model": "opus", "effort": "high"}], escalations=[], approved_round=1)
        iw.cmd_record_route(5, self.src, cwd=self.repo)
        lines2 = self.lines()
        self.assertEqual(len(lines2), 2)
        self.assertEqual(lines2[0], lines[0])
        self.assertEqual(json.loads(lines2[1])["route_final"], "deep")

    def test_route_auto_missing_is_null(self):
        self.record(route_final="quick", fixer_models=[{"model": "haiku", "effort": "medium"}], escalations=[],
                    approved_round=0, merged=False)
        code, out = self.main()
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["record"]["route_auto"])
        self.assertEqual(len(self.lines()), 1)

    def test_invalid_records_exit_2_and_write_nothing(self):
        self.set_route_auto("deep")
        deep_fixers = [{"model": "opus", "effort": "high"}]
        cases = {
            "downgrade": {"route_final": "build"},
            "bad model": {"route_final": "deep", "fixer_models": [{"model": "gpt", "effort": "high"}],
                          "escalations": [], "approved_round": 1},
            "bad effort": {"route_final": "deep", "fixer_models": [{"model": "opus", "effort": "turbo"}],
                           "escalations": [], "approved_round": 1},
            "bad auditor": {"route_final": "deep", "fixer_models": deep_fixers, "escalations": [],
                            "approved_round": 1, "auditor_model": {"model": "opus"}},
            "4 rounds": {"route_final": "deep", "fixer_models": deep_fixers * 4,
                         "escalations": [{"round": k, "kind": "effort"} for k in (2, 3, 4)], "approved_round": 4},
            "no rounds": {"route_final": "deep", "fixer_models": [], "escalations": [], "approved_round": 0},
            "approved out of range": {"route_final": "deep", "fixer_models": deep_fixers, "escalations": [],
                                      "approved_round": 2},
            "approved bool": {"route_final": "deep", "fixer_models": deep_fixers, "escalations": [],
                              "approved_round": True},
            "escalations length": {"route_final": "deep", "fixer_models": deep_fixers},
            "escalation kind": {"route_final": "deep", "escalations": [{"round": 2, "kind": "luck"}]},
            "escalation round": {"route_final": "deep", "escalations": [{"round": 3, "kind": "effort"}]},
            "unknown key": {"route_final": "deep", "post_merge_bugs": 0},
            "merged not bool": {"route_final": "deep", "merged": "yes"},
            "bad route": {"route_final": "huge"},
        }
        for name, overrides in cases.items():
            self.record(**overrides)
            code, out = self.main()
            self.assertEqual(code, 2, name)
            self.assertFalse(out["ok"], name)
            self.assertEqual(self.lines(), [], name)
        # Missing required key
        rec = self.record(route_final="deep")
        del rec["merged"]
        Path(self.src).write_text(json.dumps(rec))
        self.assertEqual(self.main()[0], 2)
        # Invalid JSON, a non-object and a missing file
        for text in ("{not json", "[1, 2]"):
            Path(self.src).write_text(text)
            self.assertEqual(self.main()[0], 2, text)
        os.remove(self.src)
        self.assertEqual(self.main()[0], 2)
        self.assertEqual(self.lines(), [])

    def test_upgrade_requires_reason(self):
        self.set_route_auto("quick")
        self.record(upgrade_reason=None)
        self.assertEqual(self.main()[0], 2)
        self.record(upgrade_reason="   ")
        self.assertEqual(self.main()[0], 2)
        self.assertEqual(self.lines(), [])
        self.record(upgrade_reason="touches a gate")
        code, out = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(out["record"]["route_auto"], "quick")
        self.assertEqual(out["record"]["upgrade_reason"], "touches a gate")
        self.assertEqual(len(self.lines()), 1)


class TestRebind(unittest.TestCase):
    """#230: rebind <N> resets the fixer binding on a real temp repo with a linked issue worktree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_repo(self.tmp)
        self.wt = iw.worktree_path(self.repo, 5)
        _git(self.repo, "worktree", "add", "-q", self.wt, "-b", "fix/issue-5-x")
        work = os.path.join(self.wt, "logs", "issue_work")
        os.makedirs(work)
        self.marker = os.path.join(work, "fixer_binding.json")
        self.hb = os.path.join(work, "guard_heartbeat.json")
        self.write_marker("sess-A")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def write_marker(self, session_id):
        Path(self.marker).write_text(json.dumps({"issue": 5, "worktree": self.wt, "branch": "fix/issue-5-x",
                                                 "created_ts": 1, "session_id": session_id}))

    def beat(self, session_id, age):
        Path(self.hb).write_text(json.dumps({"ts": int(time.time()) - age, "session_id": session_id}))

    def bound(self):
        return json.loads(Path(self.marker).read_text())["session_id"]

    def main(self, *args):
        real_root = iw.main_repo_root
        buf = io.StringIO()
        with mock.patch.object(iw, "main_repo_root", lambda cwd=None: real_root(self.wt)), \
                mock.patch("sys.stdout", buf):
            code = iw.main(["rebind", "5", *args])
        return code, json.loads(buf.getvalue())

    def test_rebind_resets_the_session(self):
        out = iw.cmd_rebind(5, cwd=self.repo)
        self.assertEqual((out["ok"], out["previous_session_id"], out["forced"]), (True, "sess-A", False))
        self.assertIsNone(self.bound())
        marker = json.loads(Path(self.marker).read_text())
        self.assertEqual(marker, {"issue": 5, "worktree": self.wt, "branch": "fix/issue-5-x", "created_ts": 1,
                                  "session_id": None})
        # A heartbeat older than 10 minutes, or one without a session id, does not block it
        for session_id, age in (("sess-A", iw.REBIND_LIVE_SECONDS + 1), (None, 5)):
            with self.subTest(session_id=session_id, age=age):
                self.write_marker("sess-A")
                self.beat(session_id, age)
                code, out = self.main()
                self.assertEqual(code, 0, out)
                self.assertEqual(out["previous_session_id"], "sess-A")
                self.assertIsNone(self.bound())

    def test_rebind_refuses_while_a_session_looks_live_unless_forced(self):
        # The bound session or another one wrote the heartbeat less than 10 minutes ago
        for session_id in ("sess-A", "sess-B"):
            with self.subTest(session_id=session_id):
                self.write_marker("sess-A")
                self.beat(session_id, 30)
                with self.assertRaises(iw.RebindRefused):
                    iw.cmd_rebind(5, cwd=self.repo)
                code, out = self.main()
                self.assertEqual(code, 2)
                self.assertFalse(out["ok"])
                self.assertIn("--force", out["error"])
                self.assertEqual(self.bound(), "sess-A")
                code, out = self.main("--force")
                self.assertEqual(code, 0)
                self.assertTrue(out["forced"])
                self.assertEqual(out["heartbeat_session_id"], session_id)
                self.assertIsNone(self.bound())

    def test_rebind_without_marker_fails(self):
        os.remove(self.marker)
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertIn("no readable binding marker", out["error"])


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

    def test_issue_prompts_state_guard_scope_checklists_and_axioms(self):
        def source(name):
            return (REPO_ROOT / ".agents" / "agents" / name / "agent.md").read_text(encoding="utf-8")

        fixer, auditor = source("issue_fixer"), source("issue_auditor")
        # #130.1: the guard exists only under Claude Code
        self.assertNotIn("A guard confines you", fixer)
        self.assertIn("Under Claude Code a guard (`scripts/hooks/issue_fixer_guard.py`) enforces these limits; "
                      "under agy no guard runs: if a command or edit falls outside these limits, do NOT run it.", fixer)
        self.assertNotIn("these limits are yours to respect", fixer)
        # #130.2: visible checklists, the fixer's inside <deliberation_protocol>, every item unchecked with its
        # evidence slot; the auditor's before its VERDICT line
        for item in ("<deliberation_protocol>", "</deliberation_protocol>", "## Fixer Checklist",
                     "- [ ] design.md read and followed (deviations: none | <list>)",
                     "- [ ] narrow tests run: `<command>` -> <N> tests, <OK|FAILED>",
                     "- [ ] full gate run: `<command>` -> <N> tests, <OK|FAILED>",
                     "- [ ] new tests hermetic (Binance client faked, no .env read): <how>",
                     "Mark an item `[x]` only with its evidence on that line",
                     "Never mark an item you did not verify."):
            self.assertIn(item, fixer)
        self.assertNotIn("- [x] design.md read and followed", fixer)
        self.assertNotIn("- [x]", fixer)
        block_start, block_end = fixer.index("<deliberation_protocol>"), fixer.index("</deliberation_protocol>")
        self.assertLess(block_start, fixer.index("## Fixer Checklist"))
        self.assertLess(fixer.index("## Fixer Checklist"), block_end)
        self.assertLess(block_end, fixer.index("<output_contract>"))
        self.assertLess(fixer.index("## Fixer Checklist"), fixer.index("## Fixer Report: issue #<n>"))
        for item in ("## Verdict Checklist", "- [ ] checks_ok is true", "checks_ok: <true|false>",
                     "unittest: <summary>", "- [ ] every acceptance criterion has a test",
                     "- [ ] every design decision followed", "- [ ] desk invariants intact", "- [ ] tests hermetic",
                     "any item left `- [ ]` is a required change"):
            self.assertIn(item, auditor)
        self.assertLess(auditor.index("## Verdict Checklist"),
                        auditor.index("VERDICT: APPROVE | VERDICT: CHANGES_REQUESTED"))
        # #231: the auditor's checklist and its marking rules sit in <deliberation_protocol>, just before
        # <output_contract>, as the fixer's do
        a_start, a_end = auditor.index("<deliberation_protocol>"), auditor.index("</deliberation_protocol>")
        self.assertLess(auditor.index("</few_shot_examples>"), a_start)
        self.assertLess(a_start, auditor.index("## Verdict Checklist"))
        self.assertLess(auditor.index("## Verdict Checklist"), a_end)
        self.assertLess(auditor.index("any item left `- [ ]` is a required change"), a_end)
        self.assertLess(a_start, auditor.index("Never mark an item you did not verify"))
        self.assertLess(auditor.index("Never mark an item you did not verify"), a_end)
        self.assertEqual(auditor[a_end:auditor.index("<output_contract>")].strip(), "</deliberation_protocol>")
        contract = auditor.split("<output_contract>", 1)[1].split("</output_contract>", 1)[0]
        self.assertNotIn("## Verdict Checklist", contract)
        self.assertNotIn("- [ ]", contract)
        self.assertIn("the Verdict Checklist from `<deliberation_protocol>` first", contract)
        # #231: a compact evidence list for bundled issues
        self.assertIn("with many criteria, one compact line", auditor)
        # #231: the fixer's agy rule is an invariant; the Allowed list is the source of truth
        operational = fixer.split("<operational_environment>", 1)[1].split("</operational_environment>", 1)[0]
        fixer_invariants = fixer.split("<invariants_and_rules>", 1)[1].split("</invariants_and_rules>", 1)[0]
        agy_rule = "under agy no guard runs: if a command or edit falls outside these limits, do NOT run it."
        self.assertIn(agy_rule, fixer_invariants)
        self.assertNotIn(agy_rule, operational)
        self.assertIn("Anything outside the Allowed list is denied; notable examples:", operational)
        self.assertNotIn("  - Denied:", operational)
        # #130.3: the quantitative axioms in the auditor's invariants
        invariants = auditor.split("<invariants_and_rules>", 1)[1].split("</invariants_and_rules>", 1)[0]
        for axiom in ("risk_pct_equity", "1.8R", "4.0R", "+0.2%", "+2.0 × ATR_15m", "-3.34", "1,000", "10-day",
                      "trading_risk_reviewer/agent.md"):
            self.assertIn(axiom, invariants)
        # #130.5: credentials reminder in both prompts
        for text in (fixer, auditor):
            self.assertIn("never reads `.env` credentials", text)
        # #130.4 and #129.7: red checks go back to the fixer; the guard heartbeat is checked
        skill = (REPO_ROOT / ".agents" / "skills" / "issue-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
        step_7a = skill.split("   a. Run `python3 scripts/dev/issue_workspace.py review-context", 1)[1].split("\n")[0]
        self.assertIn("`checks_ok: false`", step_7a)
        self.assertIn("straight back to the fixer", step_7a)
        self.assertIn("BEFORE any audit (never audit a red suite)", step_7a)
        self.assertIn("counts toward the 3-round budget", step_7a)
        self.assertNotIn("together with the audit", step_7a)
        self.assertIn("issue_workspace.py check-guard <WORKTREE> --since <launch_ts>", skill)
        self.assertIn("a confinement policy, not a sandbox", skill)
        # A binding denial after an escalation or a restarted / resumed orchestrator session has a remedy (#230:
        # through rebind, never a hand edit)
        recovery = next(line for line in skill.splitlines() if "bound to another session" in line)
        for text in ("step 7c", "restarted or resumed", "`session_id` to null",
                     "python3 scripts/dev/issue_workspace.py rebind <N>", "never edit the marker by hand",
                     "--force", "confirm no other session works in that worktree"):
            self.assertIn(text, recovery)
        # #230: under agy the user confirms an unguarded fixer before it is launched; a failed claim is reported
        guard_check = _guard_check_block(skill)
        for text in ("Under agy no guard runs: before launching `issue_fixer`", "ask whether to proceed",
                     "launch it only after an explicit yes", "`binding_claim: failed`", "`signed`, `sig_ok`"):
            self.assertIn(text, guard_check)
        self.assertNotIn("tell the user once", guard_check)
        # #230: the main-checkout writes and the scrubbed review checks
        self.assertIn("`logs/issue_work_keys/<N>.key`", skill)
        self.assertIn("the routing log and the issue key above are the only exceptions", skill)
        self.assertIn("the fixer's own test runs are not scrubbed", skill)

    def test_issue_250_prompt_and_doc_followups(self):
        def text(*parts):
            return REPO_ROOT.joinpath(*parts).read_text(encoding="utf-8")

        # The agy denied list extends the notable examples (advisory: no guard runs under agy)
        for fixer in (text(".agents", "agents", "issue_fixer", "agent.md"), text(".claude", "agents", "issue_fixer.md")):
            operational = fixer.split("<operational_environment>", 1)[1].split("</operational_environment>", 1)[0]
            examples = next(ln for ln in operational.splitlines()
                            if "Anything outside the Allowed list is denied; notable examples:" in ln)
            for item in ("git writes", "`python -c`", "heredocs", "`$(...)`", "`$VAR`", "`find -exec`", "awk",
                         "launchers", "tar/zip"):
                self.assertIn(item, examples)
            self.assertNotIn("  - Denied:", operational)
            self.assertIn("the main checkout, where every search is denied (it holds the heartbeat keys)", operational)
        for skill in (text(".agents", "skills", "issue-orchestrator", "SKILL.md"),
                      text(".claude", "skills", "issue-orchestrator", "SKILL.md")):
            # Step 7a points to issue_workspace.py instead of repeating the variable list
            step_7a = skill.split("   a. Run `python3 scripts/dev/issue_workspace.py review-context", 1)[1].split("\n")[0]
            self.assertIn("the environment allowlist of `issue_workspace.py` `check_env`", step_7a)
            self.assertIn("the fixer's own test runs are not scrubbed", step_7a)
            for gone in ("only those are scrubbed", "GEMINI_API_KEY", "`BINANCE_*`"):
                self.assertNotIn(gone, step_7a)
            # A read-only fixer round leaves no heartbeat
            guard_check = _guard_check_block(skill)
            self.assertIn("A fixer round with no Edit/Write/Bash call leaves no heartbeat (reads write none), so "
                          "`check-guard` exits 2.", guard_check)
        # The checklist heading convention in the guide's section 4.4
        guide = text("docs", "agent_prompt_engineering_guide.md")
        section = guide.split("## 4.4 ", 1)[1].split("\n# ", 1)[0]
        self.assertIn("**Repository heading convention:**", section)
        for heading in ("`## Precondition Checklist`", "`## Fixer Checklist`", "`## Verdict Checklist`"):
            self.assertIn(heading, section)
        # The documented check_env allowlist matches the code
        self.assertEqual(iw.CHECK_ENV_NAMES, {"PATH", "LANG", "TZ", "TERM", "TMPDIR"})
        self.assertEqual(iw.CHECK_ENV_PREFIXES, ("LC_", "PYTHON"))

    def test_issue_256_prompt_and_skill_followups(self):
        def text(*parts):
            return REPO_ROOT.joinpath(*parts).read_text(encoding="utf-8")

        # The fixer's grep_search bullet: any glob inside the worktree, the key glob rule only outside it
        for fixer in (text(".agents", "agents", "issue_fixer", "agent.md"), text(".claude", "agents", "issue_fixer.md")):
            operational = fixer.split("<operational_environment>", 1)[1].split("</operational_environment>", 1)[0]
            bullet = next(ln for ln in operational.splitlines() if "every grep_search and list_dir call" in ln)
            self.assertIn("inside WORKTREE any grep_search glob works (`*`, braces)", bullet)
            self.assertIn("outside it a glob that could match `<N>.key` is denied", bullet)
            self.assertIn("a list_dir pattern may not contain `..`", bullet)
            self.assertNotIn("is denied anywhere", bullet)
        for skill in (text(".agents", "skills", "issue-orchestrator", "SKILL.md"),
                      text(".claude", "skills", "issue-orchestrator", "SKILL.md")):
            block = _guard_check_block(skill)
            lead, subs = block.split("\n")[0], block.split("\n")[1:]
            # Split into sub-bullets
            self.assertGreaterEqual(len(subs), 5)
            self.assertTrue(all(ln.lstrip().startswith("- ") for ln in subs))
            self.assertLess(len(lead), 400)
            # How to read marker_session_id_null
            marker = next(ln for ln in subs if "`marker_session_id_null`" in ln)
            for item in ("`true` = the binding marker is not claimed yet", "`false` = the marker is bound to a session",
                         "`null` = no readable marker (a legacy worktree)"):
                self.assertIn(item, marker)
            # Exit 2 and a read-only round: stop and ask the user, never a retry
            self.assertIn("stop and ask the user before any audit", block)
            self.assertIn("Exit 2 is never a retry trigger", block)
            self.assertIn("Treat such a read-only round like any exit 2: stop and ask the user.", block)
        # The guard docstring records the hardlink limit
        self.assertIn("A hardlink to a key file inside the worktree is not covered", " ".join(guard.__doc__.split()))
        # check_env documents why the network variables are dropped
        for name in ("SSL_CERT_FILE", "HTTP_PROXY", "HTTPS_PROXY", "hermetic"):
            self.assertIn(name, iw.check_env.__doc__)
        # No over-wide line in the issue_workspace docstring
        self.assertEqual([ln for ln in iw.__doc__.splitlines() if len(ln) > 120], [])
        self.assertNotIn("SSL_CERT_FILE", iw.CHECK_ENV_NAMES)

    def test_auditor_axioms_match_the_trading_risk_reviewer(self):
        """#231: every axiom token the auditor pins also appears in the trading_risk_reviewer source."""
        auditor = (REPO_ROOT / ".agents" / "agents" / "issue_auditor" / "agent.md").read_text(encoding="utf-8")
        reviewer = (REPO_ROOT / ".agents" / "agents" / "trading_risk_reviewer" / "agent.md").read_text(
            encoding="utf-8")

        def normalized(text):
            # `+2.0 × ATR_15m` (auditor), `$+2.0 \times \text{ATR}_{15m}$` and `+2.0x ATR_15m` (reviewer)
            text = re.sub(r"\\text\{(\w+)\}", r"\1", text)
            text = re.sub(r"_\{(\w+)\}", r"_\1", text)
            return re.sub(r"\s*(?:×|\\times|x)\s*(?=ATR)", " x ", text)

        invariants = auditor.split("<invariants_and_rules>", 1)[1].split("</invariants_and_rules>", 1)[0]
        for axiom in ("risk_pct_equity", "1.8R", "4.0R", "+0.2%", "+2.0 × ATR_15m", "-3.34", "1,000", "10-day"):
            with self.subTest(axiom=axiom):
                self.assertIn(axiom, invariants)
                self.assertIn(normalized(axiom), normalized(reviewer))
        self.assertIn("+2.0 x ATR_15m", normalized(reviewer))

    def test_skill_is_generated_and_names_every_subagent(self):
        skill = (REPO_ROOT / ".claude" / "skills" / "issue-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
        for name in ("issue_locator", "issue_fixer", "issue_auditor", "issue_workspace.py", "pr-review"):
            self.assertIn(name, skill)
        self.assertIn(gen.GENERATED_MARKER, skill)


if __name__ == "__main__":
    unittest.main()
