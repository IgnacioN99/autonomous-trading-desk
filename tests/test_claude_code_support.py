#!/usr/bin/env python3
"""
test_claude_code_support.py - Claude Code parity with the Antigravity (agy) harness.

Covers the generated .claude/agents + .claude/skills (scripts/dev/sync_claude_assets.py --check),
the Claude hook wiring (.claude/settings.json, settings.local.json.example), CLAUDE.md, .gitignore,
the PR review assembler and hooks with Claude Code payloads/transcripts, and the pre-trade guard
binding a Claude-recorded dossier to the current session.

Runs offline; fake Claude transcripts live in temp dirs exposed through CLAUDE_PROJECTS_DIRS.
"""

import datetime
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR), str(SCRIPTS_DIR / "hooks"), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.ci import assemble_review  # noqa: E402
from scripts.ci import pr_review_state as state_mod  # noqa: E402
from scripts.ci.triage_pr import REVIEWERS, triage  # noqa: E402
from scripts.dev import sync_claude_assets as gen  # noqa: E402
from scripts.hooks import post_pr_review_hook as post_hook  # noqa: E402
from scripts.hooks import pr_review_stop_hook as stop_hook  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
import test_guard_bypasses as tgb  # noqa: E402  (fixtures only; its TestCases are not re-exported)

GENERATOR = REPO_ROOT / "scripts" / "dev" / "sync_claude_assets.py"
READ_ONLY_CLAUDE_TOOLS = {"Read", "Grep", "Glob", "WebSearch", "WebFetch"}
ISSUE_AGENT_TOOLS = {
    "issue_locator": {"Read", "Grep", "Glob"},
    "issue_fixer": {"Read", "Grep", "Glob", "Bash", "Write", "Edit"},
    "issue_auditor": {"Read", "Grep", "Glob"},
}
SESSION = "5e55105e-0000-4000-8000-000000000001"


def frontmatter(text: str) -> dict:
    lines, _ = gen.split_frontmatter(text, "test")
    return gen.parse_frontmatter(lines, "test")


def write_claude_transcript(projects: Path, agent_id: str, text: str, agent_type: str,
                            session: str = SESSION, ts: int = None) -> Path:
    d = projects / "-repo" / session / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.fromtimestamp(ts or int(time.time()), datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    rows = [
        {"type": "user", "isSidechain": True, "agentId": agent_id, "sessionId": session, "uuid": "u0",
         "timestamp": stamp, "message": {"role": "user", "content": "Review PR"}},
        {"type": "assistant", "isSidechain": True, "agentId": agent_id, "sessionId": session, "uuid": "u1",
         "timestamp": stamp, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
    ]
    path = d / f"agent-{agent_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (d / f"agent-{agent_id}.meta.json").write_text(json.dumps({"agentType": agent_type, "spawnDepth": 1}),
                                                    encoding="utf-8")
    return path


# =============================================================================
# Generated agents / skills
# =============================================================================
class TestGeneratedClaudeAssets(unittest.TestCase):

    def test_generator_check_passes(self):
        res = subprocess.run([sys.executable, str(GENERATOR), "--check"], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_check_detects_stale_and_orphan_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(REPO_ROOT / ".agents" / "agents", root / ".agents" / "agents")
            for skill in gen.SKILLS:
                shutil.copytree(REPO_ROOT / ".agents" / "skills" / skill, root / ".agents" / "skills" / skill)
            self.assertTrue(gen.check(str(root)))  # nothing generated yet -> missing
            gen.write(str(root))
            self.assertEqual(gen.check(str(root)), [])
            agent = root / ".claude" / "agents" / "trading_risk_reviewer.md"
            agent.write_text(agent.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
            self.assertIn("stale: .claude/agents/trading_risk_reviewer.md", gen.check(str(root)))
            shutil.rmtree(root / ".agents" / "agents" / "trading_risk_reviewer")
            problems = gen.check(str(root))
            self.assertTrue(any("orphan" in p for p in problems), problems)
            gen.write(str(root))
            self.assertFalse(agent.exists())
            self.assertEqual(gen.check(str(root)), [])

    def test_every_agy_agent_has_a_claude_agent(self):
        sources = sorted(p.parent.name for p in (REPO_ROOT / ".agents" / "agents").glob("*/agent.md"))
        generated = sorted(p.stem for p in (REPO_ROOT / ".claude" / "agents").glob("*.md"))
        self.assertEqual(sources, generated)
        self.assertIn(dp.EVALUATOR_NAME, generated)

    def test_claude_agent_frontmatter(self):
        for path in (REPO_ROOT / ".claude" / "agents").glob("*.md"):
            text = path.read_text(encoding="utf-8")
            fm = frontmatter(text)
            self.assertEqual(fm["name"], path.stem)
            tools = {t.strip() for t in fm["tools"].split(",")}
            if path.stem in ISSUE_AGENT_TOOLS:
                # Issue workflow agents run on opus; only the guarded fixer holds write tools and a hook
                self.assertEqual(tools, ISSUE_AGENT_TOOLS[path.stem], path)
                self.assertEqual(fm.get("model"), "opus", path)
                extra = {"hooks"} if path.stem in gen.WRITE_AGENTS else set()
                self.assertEqual(set(fm) - {"name", "description", "tools", "model"}, extra, path)
            else:
                self.assertEqual(set(fm) - {"name", "description", "tools", "model"}, set(), path)
                self.assertTrue(tools <= READ_ONLY_CLAUDE_TOOLS, f"{path}: {tools}")
                self.assertEqual(fm.get("model"), "inherit")
            self.assertIsNone(re.search(r"invoke_subagent|send_message|conversationId|TypeName", fm["description"]))
            self.assertIn(gen.GENERATED_MARKER, text)
            self.assertIn("<claude_code_runtime>", text)
        evaluator = (REPO_ROOT / ".claude" / "agents" / f"{dp.EVALUATOR_NAME}.md").read_text(encoding="utf-8")
        self.assertIn("--from-claude-subagent <agentId>", evaluator)
        self.assertIn("<output_contract>", evaluator)
        self.assertEqual({t.strip() for t in frontmatter(evaluator)["tools"].split(",")},
                         {"Read", "WebSearch", "WebFetch"})
        for rev, conf in REVIEWERS.items():
            text = (REPO_ROOT / ".claude" / "agents" / f"{conf['agent']}.md").read_text(encoding="utf-8")
            self.assertIn(f"### Verdict: {rev}", text)
            self.assertIn(f"--from-claude-subagent {rev}=<agentId>", text)
            self.assertNotIn("Bash", frontmatter(text)["tools"])

    def test_evaluator_uses_visible_precondition_checklist_not_scratch_tags(self):
        """Issue #18: Claude rejects XML-tagged scratch sections; the evaluator publishes a visible checklist."""
        scratch_tags = ("<" + "thinking", "</" + "thinking")
        agent_files = sorted((REPO_ROOT / ".agents" / "agents").glob("*/agent.md")) + \
            sorted((REPO_ROOT / ".claude" / "agents").glob("*.md"))
        self.assertGreaterEqual(len(agent_files), 10)
        for path in agent_files:
            text = path.read_text(encoding="utf-8")
            for tag in scratch_tags:
                self.assertNotIn(tag, text, str(path))
        for rel in (".agents/agents/isolated_market_evaluator/agent.md",
                    f".claude/agents/{dp.EVALUATOR_NAME}.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("## Precondition Checklist", text, rel)
            self.assertIn("<deliberation_protocol>", text, rel)
            self.assertIn("</deliberation_protocol>", text, rel)
        # Every few-shot final response: checklist first, then exactly one <dossier_json> block, which stays
        # extractable by the recorder regex (the checklist never contains the tag or any XML tag).
        source = (REPO_ROOT / ".agents/agents/isolated_market_evaluator/agent.md").read_text(encoding="utf-8")
        finals = re.findall(r"<final_response>([\s\S]*?)</final_response>", source)
        self.assertGreaterEqual(len(finals), 6)
        for final in finals:
            self.assertEqual(final.count("<dossier_json>"), 1)
            self.assertEqual(final.count("</dossier_json>"), 1)
            self.assertLess(final.index("## Precondition Checklist"), final.index("<dossier_json>"))
            self.assertLess(final.index("# QUANTITATIVE EVALUATION MASTER DOSSIER"),
                            final.index("## Precondition Checklist"))
            self.assertEqual(len(dp.DOSSIER_RE.findall(final)), 1)
            self.assertTrue(final.rstrip().endswith("</dossier_json>"))
            dossier = json.loads(dp.DOSSIER_RE.search(final).group(1))
            # The checklist region (up to the next markdown heading) is plain markdown: no <tag> patterns.
            region = re.search(r"## Precondition Checklist\n([\s\S]*?)(?=\n\s*## |\n\s*\(sent to the parent)", final).group(1)
            self.assertIsNone(re.search(r"</?[A-Za-z_][\w-]*[^>\n]*>", region), region)
            # C4.2 equals the dossier status.
            c42 = re.findall(r"C4\.2 Overall status:.*-> (APPROVED|REJECTED|NEUTRAL)\s*$", region, re.M)
            self.assertEqual(c42, [dossier["status"]])
            # Approved candidates never have an unchecked K1-K5 or C3.1 line (K5: issue #206).
            for symbol in dossier["approved_symbols"]:
                for line in region.splitlines():
                    if symbol in line and re.search(r"\b(K1|K2|K3|K4|K5|C3\.1)\b", line):
                        self.assertTrue(line.strip().startswith("- [x]"), line)
            self.assertEqual([c["symbol"] for c in dossier["approved_candidates"]], dossier["approved_symbols"])

    def test_claude_skills(self):
        for skill in gen.SKILLS:
            path = REPO_ROOT / ".claude" / "skills" / skill / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            fm = frontmatter(text)
            self.assertEqual(fm["name"], skill)
            self.assertTrue(fm["description"])
            self.assertIn(gen.GENERATED_MARKER, text)
        planner = (REPO_ROOT / ".claude" / "skills" / "trade-execution-planner" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('subagent_type: "isolated_market_evaluator"', planner)
        self.assertIn("record_evaluation.py --from-claude-subagent <agentId>", planner)
        review = (REPO_ROOT / ".claude" / "skills" / "pr-review" / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("Agent tool call per id", "--from-claude-subagent <id>=<agentId>",
                       "scripts/ci/triage_pr.py", "scripts/ci/verify_review.py", "gh pr comment",
                       "pr_review_state.py"):
            self.assertIn(needle, review)
        for rev, conf in REVIEWERS.items():
            self.assertIn(f"`{conf['agent']}`", review)

    def test_unknown_tool_and_read_only_violations_fail(self):
        base = ("---\nname: x_agent\ndescription: test agent\ntools:\n  - {tool}\n"
                "commandExecutionPolicy: \"off\"\nmodel: inherit\n---\nbody\n")
        with self.assertRaises(gen.SourceError):
            gen.render_agent(".agents/agents/x_agent/agent.md", base.format(tool="teleport"))
        with self.assertRaises(gen.SourceError):
            gen.render_agent(".agents/agents/x_agent/agent.md", base.format(tool="run_command"))
        dest, content = gen.render_agent(".agents/agents/x_agent/agent.md", base.format(tool="view_file"))
        self.assertEqual(dest.replace(os.sep, "/"), ".claude/agents/x_agent.md")
        self.assertIn("tools: Read\n", content)
        with self.assertRaises(gen.SourceError):
            gen.render_agent(".agents/agents/x_agent/agent.md",
                             base.format(tool="view_file").replace("test agent", "uses send_message"))

    def test_skill_override_markers_must_exist(self):
        with self.assertRaises(gen.SourceError):
            gen.apply_overrides("no markers here", [{"start": "X", "end": "Y", "text": ""}], "t")


# =============================================================================
# Hook wiring, CLAUDE.md, .gitignore, AGENTS.md budget
# =============================================================================
class TestClaudeConfiguration(unittest.TestCase):

    def _commands(self, cfg: dict, event: str) -> list:
        return [(group.get("matcher"), h["command"]) for group in cfg["hooks"].get(event, [])
                for h in group["hooks"]]

    def test_settings_json_wiring(self):
        raw = (REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
        cfg = json.loads(raw)
        for forbidden in ("/mnt/", "/home/", "C:\\", "Users", "wsl.exe"):
            self.assertNotIn(forbidden, raw)
        pre = self._commands(cfg, "PreToolUse")
        guarded = ("Bash", "PowerShell", "NotebookEdit", "Write", "Edit", "MultiEdit", "mcp__binance__x")
        self.assertTrue(any("pre_trade_guard.py" in c and all(re.fullmatch(m, t) for t in guarded) for m, c in pre))
        post = self._commands(cfg, "PostToolUse")
        self.assertTrue(any("post_trade_sync.py" in c and all(re.fullmatch(m, t) for t in
                                                                ("Bash", "PowerShell", "mcp__binance__x"))
                            for m, c in post))
        self.assertTrue(any("post_pr_review_hook.py" in c and re.fullmatch(m, "Bash") and re.fullmatch(m, "PowerShell")
                            for m, c in post))
        stop = self._commands(cfg, "Stop")
        self.assertEqual(len(stop), 1)
        self.assertIn("pr_review_stop_hook.py --claude", stop[0][1])
        for _, cmd in pre + post + stop:
            self.assertIn('"$CLAUDE_PROJECT_DIR"/scripts/hooks/', cmd)
            script = cmd.split('"$CLAUDE_PROJECT_DIR"/', 1)[1].split()[0]
            self.assertTrue((REPO_ROOT / script).is_file(), script)

    def test_settings_local_example_matches_project_hooks(self):
        raw = (REPO_ROOT / ".claude" / "settings.local.json.example").read_text(encoding="utf-8")
        cfg = json.loads(raw)
        self.assertIn("<WSL_DISTRO>", raw)
        self.assertIn("<REPO_PATH_IN_WSL>", raw)
        for forbidden in ("/mnt/c", "/home/", "C:\\", "Ubuntu"):
            self.assertNotIn(forbidden, raw)
        project = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        for event in ("PreToolUse", "PostToolUse", "Stop"):
            want = sorted((m, c.split("/scripts/hooks/", 1)[1]) for m, c in self._commands(project, event))
            got = sorted((m, c.split("/scripts/hooks/", 1)[1]) for m, c in self._commands(cfg, event))
            self.assertEqual(want, got, event)

    def test_claude_md_imports_rules(self):
        text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        imports = re.findall(r"^@(\S+)$", text, flags=re.MULTILINE)
        self.assertEqual(imports[0], "AGENTS.md")
        for rule in sorted((REPO_ROOT / ".agents" / "rules").glob("*.md")):
            self.assertIn(rule.relative_to(REPO_ROOT).as_posix(), imports)
        self.assertIn("--from-claude-subagent <agentId>", text)
        self.assertIn("sync_claude_assets.py", text)
        for forbidden in ("/mnt/c", "C:\\", "/home/"):
            self.assertNotIn(forbidden, text)

    def test_agents_md_stays_under_budget(self):
        self.assertLess(len((REPO_ROOT / "AGENTS.md").read_bytes()), 22000)

    @unittest.skipUnless(shutil.which("git") and (REPO_ROOT / ".git").exists(), "git checkout required")
    def test_gitignore_tracks_claude_assets(self):
        if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO_ROOT, capture_output=True).returncode != 0:
            self.skipTest("git cannot read this checkout (e.g. a Windows worktree seen from WSL)")
        tracked =["CLAUDE.md", ".claude/settings.json", ".claude/settings.local.json.example",
                   ".claude/agents/isolated_market_evaluator.md", "scripts/dev/sync_claude_assets.py"]
        tracked += [f".claude/skills/{s}/SKILL.md" for s in gen.SKILLS]
        for rel in tracked:
            res = subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO_ROOT, capture_output=True)
            self.assertEqual(res.returncode, 1, f"{rel} is gitignored")
        for rel in (".claude/settings.local.json", ".claude/worktrees/agent-x/file"):
            res = subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO_ROOT, capture_output=True)
            self.assertEqual(res.returncode, 0, f"{rel} must be gitignored")

    def test_triage_routes_claude_assets(self):
        self.assertEqual(set(triage(["CLAUDE.md"])["required_reviewers"]), set(REVIEWERS))
        manifest = triage([".claude/agents/isolated_market_evaluator.md"])
        self.assertIn("prompt_engineering", manifest["required_reviewers"])
        self.assertIn("agentic_harness", manifest["required_reviewers"])


# =============================================================================
# PR review: assembler with Claude transcripts
# =============================================================================
class TestClaudeReviewAssembler(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.env = mock.patch.dict(os.environ, {dp.CLAUDE_PROJECTS_ENV: str(self.projects)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_assemble_from_claude_transcripts(self):
        write_claude_transcript(self.projects, "a0000000000000a01",
                                "### Verdict: agentic_harness\n- **Status:** [APPROVED]\n- ok", "agentic_harness_reviewer")
        write_claude_transcript(self.projects, "a0000000000000a02",
                                "### Verdict: trading_risk\n- **Status:** [CHANGES REQUIRED]\n- 🔴 x.py:1",
                                "trading_risk_reviewer")
        manifest = {"required_reviewers": ["agentic_harness", "trading_risk"], "changed_files": ["a.py"]}
        sections, provenance, errors = assemble_review.collect_sections(
            manifest["required_reviewers"], {}, {},
            {"agentic_harness": "a0000000000000a01", "trading_risk": "a0000000000000a02"})
        self.assertEqual(errors, [])
        self.assertEqual(provenance["trading_risk"], "claude subagent a0000000000000a02")
        report, result = assemble_review.build_report(manifest, sections, provenance, pr="14")
        self.assertIn(assemble_review.OVERALL_BLOCKED, report)
        self.assertEqual(result["rejected"], ["trading_risk"])

    def test_claude_ids_auto_detected_in_from_subagent(self):
        write_claude_transcript(self.projects, "a0000000000000a03",
                                "### Verdict: agentic_harness\n- **Status:** [APPROVED]", "agentic_harness_reviewer")
        sections, _, errors = assemble_review.collect_sections(
            ["agentic_harness"], {"agentic_harness": "a0000000000000a03"}, {})
        self.assertEqual(errors, [])
        self.assertIn("agentic_harness", sections)

    def test_wrong_agent_type_is_rejected(self):
        # A general-purpose agent (or another reviewer) cannot sign a domain verdict
        write_claude_transcript(self.projects, "a0000000000000a04",
                                "### Verdict: trading_risk\n- **Status:** [APPROVED]", "general-purpose")
        write_claude_transcript(self.projects, "a0000000000000a05",
                                "### Verdict: trading_risk\n- **Status:** [APPROVED]", "agentic_harness_reviewer")
        for agent in ("a0000000000000a04", "a0000000000000a05"):
            sections, _, errors = assemble_review.collect_sections(["trading_risk"], {}, {}, {"trading_risk": agent})
            self.assertNotIn("trading_risk", sections)
            self.assertTrue(errors)
            self.assertIn("trading_risk_reviewer", errors[0])


# =============================================================================
# PR review hooks with Claude Code payloads
# =============================================================================
class TestClaudePRReviewHooks(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp, "logs", "pr_review_state.json")
        self.env = mock.patch.dict(os.environ, {state_mod.STATE_ENV: self.state_file})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, command: str, session: str = SESSION, tool: str = "Bash", **extra) -> str:
        payload = dict({"session_id": session, "hook_event_name": "PostToolUse", "tool_name": tool,
                        "tool_input": {"command": command}, "tool_response": {"stdout": "", "stderr": ""}}, **extra)
        with redirect_stderr(io.StringIO()):
            return post_hook.handle_post_tool_use(payload)

    def _stop(self, session: str = SESSION, **extra) -> dict:
        payload = dict({"session_id": session, "hook_event_name": "Stop", "cwd": "/repo"}, **extra)
        with redirect_stderr(io.StringIO()):
            return stop_hook.decide(payload)

    def test_post_hook_arms_and_closes_with_claude_payloads(self):
        self.assertEqual(self._post("git status"), "ignored")
        self.assertEqual(self._post("gh pr create --fill", tool="Write"), "ignored")
        self.assertEqual(self._post("gh pr create --fill", tool_response={"interrupted": True}), "ignored")
        self.assertEqual(self._post("gh pr create --fill", hook_event_name="PostToolUseFailure"), "ignored")
        self.assertEqual(state_mod.load_state(), {})
        self.assertEqual(self._post("git push -u origin feat/claude"), "pending")
        self.assertEqual(state_mod.load_state()["conversation_id"], SESSION)
        self.assertEqual(self._post("gh pr comment 14 --body-file logs/pr_review/report.md"), "done")

    def test_stop_hook_blocks_with_claude_instructions_until_cap(self):
        self._post("gh pr create --fill")
        for attempt in range(1, state_mod.MAX_STOP_ATTEMPTS + 1):
            out = self._stop()
            self.assertEqual(out["decision"], "continue")
            self.assertIn(".claude/skills/pr-review/SKILL.md", out["reason"])
            self.assertIn("--from-claude-subagent", out["reason"])
            self.assertNotIn("invoke_subagent", out["reason"])
            self.assertIn(f"{attempt}/{state_mod.MAX_STOP_ATTEMPTS}", out["reason"])
        self.assertEqual(self._stop(), {"decision": "stop"})
        self.assertEqual(state_mod.load_state()["status"], "abandoned")

    def test_stop_hook_respects_stop_hook_active_sessions_and_subagents(self):
        self._post("gh pr create --fill")
        self.assertEqual(self._stop(stop_hook_active=True), {"decision": "stop"})
        self.assertEqual(self._stop(session="another-session"), {"decision": "stop"})
        self.assertEqual(self._stop(hook_event_name="SubagentStop", agent_id="a0000000000000001"), {"decision": "stop"})
        self.assertEqual(state_mod.load_state()["stop_attempts"], 0)
        state_mod.mark_started()
        self.assertEqual(self._stop(), {"decision": "stop"})

    def test_claude_output_format(self):
        self.assertEqual(stop_hook.format_output({"decision": "stop"}, "claude"), "")
        out = json.loads(stop_hook.format_output({"decision": "continue", "reason": "r"}, "claude"))
        self.assertEqual(out, {"decision": "block", "reason": "r"})
        self.assertEqual(json.loads(stop_hook.format_output({"decision": "stop"}, "agy")), {"decision": "stop"})

    def _run_settings_hook(self, script: str, payload) -> subprocess.CompletedProcess:
        """Runs a hook exactly as .claude/settings.json declares it (python3 -> this interpreter)."""
        cfg = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        commands = [h["command"] for groups in cfg["hooks"].values() for g in groups for h in g["hooks"]]
        command = next(c for c in commands if script in c)
        argv = shlex.split(command.replace('"$CLAUDE_PROJECT_DIR"', shlex.quote(str(REPO_ROOT))))
        argv[0] = sys.executable
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(REPO_ROOT))
        return subprocess.run(argv, cwd=self.tmp, input=stdin, capture_output=True, text=True, timeout=30, env=env)

    def test_hooks_run_as_wired_in_settings_json(self):
        for raw in ("", "not json", "[]"):
            res = self._run_settings_hook("pr_review_stop_hook", raw)
            self.assertEqual((res.returncode, res.stdout.strip()), (0, ""))
        res = self._run_settings_hook("pr_review_stop_hook", {"session_id": SESSION, "hook_event_name": "Stop"})
        self.assertEqual((res.returncode, res.stdout.strip()), (0, ""))  # nothing pending -> allow stop

        res = self._run_settings_hook("post_pr_review_hook", {
            "session_id": SESSION, "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "gh pr create --fill"}, "tool_response": {"stdout": "https://x/pull/1"}})
        self.assertEqual((res.returncode, json.loads(res.stdout)), (0, {}))
        res = self._run_settings_hook("pr_review_stop_hook", {
            "session_id": SESSION, "hook_event_name": "Stop", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("pr-review", out["reason"])


# =============================================================================
# Pre-trade guard with a Claude-recorded dossier
# =============================================================================
EVALUATOR_AGENT_ID = "a0123456789abcdef"


class TestGuardWithClaudeDossier(tgb.GuardHarness):

    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.root, "claude_projects")
        os.environ[dp.CLAUDE_PROJECTS_ENV] = self.projects  # restored by GuardHarness' patch.dict

    def write_claude_dossier(self, symbol="BTCUSDT", direction="LONG", agent_type=dp.EVALUATOR_NAME, extra=None):
        # "score": 85 lies in the calibrated fixture bucket written by GuardHarness (issue #202)
        cand = {"symbol": symbol, "direction": direction, "tier": "S", "leverage": 3, "score": 85}
        cand.update(extra or {})  # optional candidate overrides (is_yolo, tier, requires_user_confirmation, ...)
        block = json.dumps({"status": "APPROVED", "target_env": "PROD", "summary": "test",
                            "approved_candidates": [cand]})
        path = write_claude_transcript(Path(self.projects), EVALUATOR_AGENT_ID,
                                       f"Master Dossier\n<dossier_json>\n{block}\n</dossier_json>", agent_type)
        record = tgb.add_radar_snapshots(dp.build_record_from_extraction(
            dp.extract_dossier_from_claude_transcript(str(path))))
        record["target_env"] = "prod"
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return path

    def claude_deploy(self, session=SESSION, direction="LONG", symbol="BTCUSDT"):
        payload = {"session_id": session, "hook_event_name": "PreToolUse", "cwd": self.root, "tool_name": "Bash",
                   "tool_input": {"command": f"python3 scripts/execute_futures_trade.py --symbol {symbol} "
                                             f"--direction {direction} --leverage 3 --env prod"}}
        return self.run_guard(payload)

    def test_claude_dossier_from_this_session_allows_deploy(self):
        self.write_claude_dossier()
        res = self.claude_deploy()
        self.assertEqual(res["__exit_code__"], 0, res)
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_claude_dossier_from_other_session_denied(self):
        self.write_claude_dossier()
        res = self.claude_deploy(session="0f0f0f0f-1111-2222-3333-444444444444")
        self.assertEqual(res["__exit_code__"], 2)
        self.assertIn("not for the current", res["__stderr__"])

    def test_claude_dossier_direction_and_agent_type_enforced(self):
        self.write_claude_dossier(direction="LONG")
        res = self.claude_deploy(direction="SHORT")
        self.assertEqual(res["__exit_code__"], 2)
        path = self.write_claude_dossier()
        Path(dp.claude_meta_path(str(path))).write_text(json.dumps({"agentType": "general-purpose"}), encoding="utf-8")
        res = self.claude_deploy()
        self.assertEqual(res["__exit_code__"], 2)
        self.assertIn("general-purpose", res["__stderr__"])

    def _bash(self, command):
        return self.run_guard({"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": command}})

    def test_claude_transcripts_and_root_overrides_are_protected(self):
        res = self._bash("cat ~/.claude/projects/-repo/" + SESSION + "/subagents/agent-a0123456789abcdef.jsonl")
        self.assertEqual(res["__exit_code__"], 2)
        self.assertIn("Evaluation Trail Protection", res["__stderr__"])
        res = self._bash("CLAUDE_PROJECTS_DIRS=/tmp/fake python3 scripts/record_evaluation.py "
                         "--from-claude-subagent a0123456789abcdef")
        self.assertEqual(res["__exit_code__"], 2)
        res = self._bash("export AGY_BRAIN_DIRS=/tmp/fake")
        self.assertEqual(res["__exit_code__"], 2)
        # Mentioning the variables (e.g. grepping for them) stays allowed
        self.assertEqual(self._bash("grep -rn CLAUDE_PROJECTS_DIRS scripts/")["__exit_code__"], 0)
        res = self.run_guard({"tool_name": "Write", "tool_input": {
            "file_path": "/home/u/.claude/projects/-repo/" + SESSION + "/subagents/agent-a0123456789abcdef.jsonl",
            "content": "{}"}})
        self.assertEqual(res["__exit_code__"], 2)
        res = self.run_guard({"tool_name": "Write", "tool_input": {
            "file_path": "/home/u/.claude/projects/-repo/memory/notes.md", "content": "x"}})
        self.assertEqual(res["__exit_code__"], 0)

    def test_record_from_claude_subagent_is_not_manual_recording(self):
        res = self._bash(f"python3 scripts/record_evaluation.py --from-claude-subagent {EVALUATOR_AGENT_ID} --env prod")
        self.assertEqual(res["__exit_code__"], 0)
        res = self._bash("python3 scripts/record_evaluation.py --env prod --symbols BTCUSDT")
        self.assertEqual(res["__exit_code__"], 2)

    def test_claude_agents_dir_is_a_harness_file(self):
        res = self.run_guard({"tool_name": "Edit", "tool_input": {
            "file_path": ".claude/agents/isolated_market_evaluator.md", "old_string": "a", "new_string": "b"}})
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "ask")


if __name__ == "__main__":
    unittest.main()
