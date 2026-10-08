#!/usr/bin/env python3
"""
test_issue_79_73_protected_gates.py - Issues #79 and #73.

#79: the gate-bearing modules (scripts/utils/gate_limits.py, scripts/execute_futures_trade.py,
scripts/utils/portfolio_exposure.py, scripts/utils/env_resolver.py, scripts/user_profile.py) are harness files:
agent writes from the file tools, Bash and PowerShell require explicit confirmation (force_ask) in the main
checkout, linked worktrees keep a plain ask, reads and the risk-reducing executor calls keep their decision.
#73: logs/hook_heartbeat.json is ground truth written only by the guard itself; the doctor validates the Claude
Code PreToolUse matcher (.claude/settings.json / settings.local.json).

Hermetic: temp workspaces, no network, no writes to the real logs/.
"""

import json
import os
import shlex
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "scripts"), os.path.join(REPO_ROOT, "scripts", "hooks"),
           os.path.join(REPO_ROOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pre_trade_guard  # noqa: E402
import test_guard_bypasses as tgb  # noqa: E402  (fixtures only)
import test_issue_49_powershell_notebookedit as t49  # noqa: E402  (fixtures only)
import test_issue_148_guard_worktree_paths as t148  # noqa: E402  (fixtures only)
# Last: trading_doctor puts scripts/ first on sys.path, where scripts/post_trade_sync.py would shadow the hook
# module that the fixtures above import
import trading_doctor  # noqa: E402

GATE_MODULES = ("scripts/utils/gate_limits.py", "scripts/execute_futures_trade.py",
                "scripts/utils/portfolio_exposure.py", "scripts/utils/env_resolver.py", "scripts/user_profile.py",
                "scripts/utils/score_calibration.py")  # #202: decides when an autonomous Tier S asks the user
EXECUTOR = "scripts/execute_futures_trade.py"
HEARTBEAT = "hook_heartbeat.json"
HEARTBEAT_WRITER = "scripts/hooks/pre_trade_guard.py"


def setUpModule():
    tgb.setUpModule()


def bash_write_vectors(p):
    return [
        f"echo 'YOLO_LOSS_CAP = 9' > {p}",
        f"echo 'x' >> {p}",
        f"cp /tmp/loose.py {p}",
        f"mv /tmp/loose.py {p}",
        f"echo x | tee {p}",
        f"sed -i 's/0.35/0.95/' {p}",
        f"perl -i -pe 's/0.35/0.95/' {p}",
        f"git checkout -- {p}",
        f"git restore {p}",
        f"python3 -c \"open('{p}', 'w').write('x')\"",
    ]


# =============================================================================
# #79: file tools
# =============================================================================
class TestGateModulesFileTools(t49.PowerShellHarness):

    def test_agy_file_tools_force_ask_with_clear_message(self):
        for rel in GATE_MODULES:
            for name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
                res = self.agy({"toolCall": {"name": name, "args": {"TargetFile": rel, "CodeContent": "x = 1\n"}}})
                self.assertEqual(res.get("decision"), "force_ask", f"{name} {rel}")
                self.assertIn("gate module", res.get("reason", ""), rel)
                self.assertIn("Explicit confirmation required", res.get("reason", ""), rel)

    def test_claude_file_tools_force_ask(self):
        for rel in GATE_MODULES:
            for tool, tool_input in (("Write", {"file_path": rel, "content": "x = 1\n"}),
                                     ("Edit", {"file_path": rel, "old_string": "0.35", "new_string": "0.95"}),
                                     ("MultiEdit", {"file_path": rel, "edits": [{"old_string": "a", "new_string": "b"}]}),
                                     ("NotebookEdit", {"notebook_path": rel, "new_source": "x", "cell_id": "c1",
                                                       "cell_type": "code", "edit_mode": "replace"})):
                self.assertEqual(self.decision(self._claude(tool, tool_input)), "ask", f"{tool} {rel}")
                abs_target = os.path.join(self.root, *rel.split("/"))
                self.assertEqual(self.decision(self._claude(tool, dict(tool_input, file_path=abs_target,
                                                                      notebook_path=abs_target))),
                                 "ask", f"{tool} {abs_target}")

    def test_primitive_free_executor_edit_is_force_ask(self):
        # Before #79 an executor edit without order-placing primitives got a plain ask
        for rel in GATE_MODULES:
            self.assertEqual(pre_trade_guard.evaluate_file_write(rel, "GATE = 1\n", self.root)[0], "force_ask", rel)

    def test_unrelated_scripts_keep_plain_ask(self):
        for rel in ("scripts/sync_session_state.py", "scripts/utils/position_timing.py", "tests/test_gate_limits.py"):
            self.assertEqual(pre_trade_guard.evaluate_file_write(rel, "x = 1\n", self.root)[0], "ask", rel)


class TestGateModulesLinkedWorktree(t148._Base):

    def test_worktree_copies_keep_plain_ask_main_checkout_force_asks(self):
        for rel in GATE_MODULES:
            with self.subTest(rel=rel):
                self.assertEqual(self.decision(os.path.join(self.wt, *rel.split("/")), "x = 1\n"), "ask")
                self.assertEqual(self.decision(os.path.join(self.repo, *rel.split("/")), "x = 1\n"), "force_ask")


# =============================================================================
# #79: shell writes
# =============================================================================
class TestGateModulesBash(t49.PowerShellHarness):

    def test_bash_writes_force_ask(self):
        for rel in GATE_MODULES:
            for c in bash_write_vectors(rel):
                analysis = pre_trade_guard.analyze_run_command(c, self.root, self.root)
                if not (rel == EXECUTOR and analysis.get("deny")):
                    self.assertIn("gate module", analysis.get("force_ask") or "", c)
                res = self.agy(self.cmd(c))
                if rel == EXECUTOR:
                    # Some lines naming the executor are already denied by the trade gates (stricter)
                    self.assertIn(res.get("decision"), ("force_ask", "deny"), c)
                else:
                    self.assertEqual(res.get("decision"), "force_ask", c)
                    self.assertIn("gate module", res.get("reason", ""), c)
            self.assertIn(self.decision(self.bash(f"echo x > {rel}")), ("ask", "deny"), rel)

    def test_bare_basenames_of_never_run_modules_force_ask(self):
        for c in ("cd scripts/utils && sed -i 's/0.35/0.95/' gate_limits.py",
                  "cd scripts/utils && echo x > portfolio_exposure.py",
                  "cd scripts/utils; cp /tmp/x.py env_resolver.py"):
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "force_ask", c)

    def test_reads_keep_previous_decision(self):
        for rel in GATE_MODULES:
            for c in (f"cat {rel}", f"grep -n LOSS {rel}", f"head -n 20 {rel}", f"git diff {rel}",
                      f"git log --oneline -- {rel}", f"wc -l {rel}"):
                self.assertEqual(self.agy(self.cmd(c)).get("decision"), "ask", c)
        for c in ("python3 -c \"import gate_limits\"", "python3 -c \"import portfolio_exposure, env_resolver\"",
                  "python3 -m unittest tests.test_gate_limits", "python3 -m pytest tests/test_gate_limits.py",
                  "git add scripts/utils/gate_limits.py", "git commit -m 'edit scripts/execute_futures_trade.py'"):
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "ask", c)

    def test_risk_reducing_calls_keep_their_decision(self):
        flat = ("python3 scripts/execute_futures_trade.py --close-position --symbol BTCUSDT",
                "python3 scripts/execute_futures_trade.py --move-breakeven --symbol BTCUSDT",
                "python3 scripts/execute_futures_trade.py --protect-pending",
                "python3 scripts/execute_futures_trade.py --auto-heal",
                "python3 scripts/execute_futures_trade.py --audit-orphans",
                "python3 scripts/loops/position_guardian_loop.py --once")
        for c in flat:
            self.assertEqual(self.agy(self.cmd(c)).get("decision"), "allow", c)
            self.assertEqual(self.decision(self.bash(c)), "allow", c)
            for suffix in (" 2>&1", " > /tmp/out.log", " 2>&1 | tee /tmp/out.log", " &", " >> /tmp/out.log 2>&1"):
                res = self.agy(self.cmd(c + suffix))
                self.assertNotIn(res.get("decision"), ("force_ask", "deny"), c + suffix)
        for c in ("python3 scripts/execute_futures_trade.py --positions --json",
                  "python3 scripts/execute_futures_trade.py --positions --json 2>&1 > /tmp/p.json",
                  "python3 scripts/user_profile.py --show", "python3 scripts/user_profile.py --show > /tmp/p.txt"):
            self.assertNotIn(self.agy(self.cmd(c)).get("decision"), ("force_ask", "deny"), c)


class TestGateModulesPowerShell(t49.PowerShellHarness):

    def test_powershell_writes_force_ask(self):
        for rel in GATE_MODULES:
            for p in (rel.replace("/", "\\"), ".\\" + rel.replace("/", "\\"), rel):
                for command in t49.write_commands(p) + [f"Get-Content C:\\tmp\\x.py | Set-Content {p}",
                                                        f"Clear-Content {p}", f"Tee-Object -FilePath {p}"]:
                    full = pre_trade_guard.normalize_powershell_command(command)
                    _, force_ask = pre_trade_guard.powershell_backstop(full, self.root, self.root)
                    self.assertIn("gate module", force_ask or "", command)
                    expected = ("ask", "deny") if rel == EXECUTOR else ("ask",)
                    self.assertIn(self.decision(self.ps(command)), expected, command)

    def test_powershell_parenthesized_and_variable_targets_force_ask(self):
        # Audit round 1: write targets of the run-as-program modules inside (...) or through a variable
        for rel in (EXECUTOR, "scripts/user_profile.py"):
            p = rel.replace("/", "\\")
            for command in (f"Set-Content -Path ('{p}') -Value x",
                            f"Set-Content -Path (Resolve-Path {p}) -Value x",
                            f"Copy-Item C:\\tmp\\x.py -Destination (Join-Path . {p})",
                            f"$p='{p}'; Set-Content $p x",
                            f"$p = \"{p}\"; Remove-Item $p",
                            f"python -c \"print(1)\"; Set-Content -Path ('{p}') -Value x",
                            # Audit round 2: an argument named like an interpreter is not the program
                            f"Copy-Item C:/tmp/python {p}", f"Move-Item C:/tmp/python {p}",
                            f"Set-Content -Value py {p}", f"Copy-Item C:\\tmp\\python.exe {p}"):
                full = pre_trade_guard.normalize_powershell_command(command)
                _, force_ask = pre_trade_guard.powershell_backstop(full, self.root, self.root)
                self.assertIn("gate module", force_ask or "", command)
                expected = ("ask", "deny") if rel == EXECUTOR else ("ask",)
                self.assertIn(self.decision(self.ps(command)), expected, command)

    def test_powershell_runs_with_constructs_are_not_writes(self):
        for command in (
                "python scripts\\execute_futures_trade.py --audit-orphans | Out-File (Join-Path $env:TEMP a.log)",
                "python scripts\\execute_futures_trade.py --audit-orphans | "
                "Out-File \"C:\\tmp\\a_$((Get-Date).ToString('yyyyMMdd')).log\"",
                "python -u scripts\\execute_futures_trade.py --close-position --symbol BTCUSDT 2>&1 | "
                "Tee-Object -FilePath (Join-Path $env:TEMP c.log)",
                "py C:\\repo\\scripts\\user_profile.py --show | Out-File (Join-Path $env:TEMP p.txt)",
                "wsl.exe -d Ubuntu -- python3 scripts/execute_futures_trade.py --auto-heal > C:\\tmp\\h.log",
                "& C:\\Python312\\python.exe scripts\\execute_futures_trade.py --audit-orphans 2>&1 | Out-File x.log",
                "& \"C:\\Python312\\python.exe\" scripts\\execute_futures_trade.py --close-position --symbol BTCUSDT "
                "2>&1"):
            full = pre_trade_guard.normalize_powershell_command(command)
            self.assertFalse(pre_trade_guard._powershell_writes_gate_program(
                full, pre_trade_guard._powershell_tokens(full)), command)
            self.assertNotIn(self.decision(self.ps(command)), ("ask", "deny"), command)

    def test_powershell_reads_stay_passthrough(self):
        for rel in GATE_MODULES:
            p = rel.replace("/", "\\")
            for command in (f"Get-Content {p}", f"Select-String -Path {p} -Pattern LOSS", f"Test-Path {p}",
                            f"Get-FileHash {p}", f"Get-Content {p} | Select-Object -First 5"):
                if rel == "scripts/execute_futures_trade.py":
                    # A PowerShell command naming the executor goes through the trade gates (decision unchanged by
                    # #79); it must not become a harness confirmation
                    self.assertNotEqual(self.decision(self.ps(command)), "ask", command)
                else:
                    self.assertEqual(self.decision(self.ps(command)), "passthrough", command)
        self.assertNotEqual(self.decision(self.ps("python -m pytest tests\\test_gate_limits.py > C:\\tmp\\o.txt")),
                            "deny")
        self.assertNotEqual(self.decision(self.ps("python -m pytest tests\\test_gate_limits.py > C:\\tmp\\o.txt")),
                            "ask")

    def test_powershell_risk_reducing_calls_keep_their_decision(self):
        for args in ("--close-position --symbol BTCUSDT", "--move-breakeven --symbol BTCUSDT", "--protect-pending",
                     "--auto-heal", "--audit-orphans"):
            base = f"python scripts\\execute_futures_trade.py {args}"
            self.assertEqual(self.decision(self.ps(base)), "allow", base)
            self.assertEqual(self.decision(self.ps(f"& {base}")), "allow", base)
            for command in (f"{base} 2>&1", f"{base} > C:\\tmp\\out.log", f"{base} | Out-File C:\\tmp\\out.log",
                            f"{base} 2>&1 | Tee-Object -FilePath C:\\tmp\\out.log", f"& {base} 2>&1"):
                self.assertNotIn(self.decision(self.ps(command)), ("ask", "deny"), command)
        guardian = "python scripts\\loops\\position_guardian_loop.py --once"
        self.assertEqual(self.decision(self.ps(guardian)), "allow")
        self.assertNotIn(self.decision(self.ps(guardian + " 2>&1 | Out-File C:\\tmp\\g.log")), ("ask", "deny"))
        for command in ("python scripts\\execute_futures_trade.py --positions --json > C:\\tmp\\p.json",
                        "python scripts\\user_profile.py --show", "python scripts\\user_profile.py --show 2>&1",
                        "& python scripts\\user_profile.py --show | Out-File C:\\tmp\\p.txt"):
            self.assertNotIn(self.decision(self.ps(command)), ("ask", "deny"), command)


# =============================================================================
# #73.2: heartbeat is ground truth
# =============================================================================
class TestHeartbeatProtected(t49.PowerShellHarness):

    def assertHeartbeatDenied(self, reason, label=""):
        self.assertIn("Ground Truth Protection", reason, label)
        self.assertIn(f"logs/{HEARTBEAT} may only be written by", reason, label)
        self.assertIn(HEARTBEAT_WRITER, reason, label)

    def test_bash_writes_denied(self):
        for c in tgb.TestGroundTruthProtection.shell_vectors(self, HEARTBEAT):
            res = self.agy(self.cmd(c))
            self.assertEqual(res.get("decision"), "deny", c)
            self.assertHeartbeatDenied(res.get("reason", ""), c)
        res = self.bash(f"echo '{{\"last_seen_ts\": {int(time.time())}}}' > logs/{HEARTBEAT}")
        self.assertEqual(self.decision(res), "deny")
        self.assertHeartbeatDenied(res["__stderr__"])

    def test_powershell_writes_denied(self):
        for p in t49.path_forms(f"logs\\{HEARTBEAT}"):
            for command in t49.write_commands(p):
                res = self.ps(command)
                self.assertEqual(self.decision(res), "deny", command)
                self.assertHeartbeatDenied(res["__stderr__"], command)

    def test_file_tools_denied(self):
        for target in (f"logs/{HEARTBEAT}", os.path.join(self.root, "logs", HEARTBEAT),
                       f"C:\\Users\\x\\repo\\logs\\{HEARTBEAT}"):
            for name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
                res = self.agy({"toolCall": {"name": name, "args": {"TargetFile": target, "CodeContent": "{}"}}})
                self.assertEqual(res.get("decision"), "deny", f"{name} {target}")
                self.assertHeartbeatDenied(res.get("reason", ""), target)
            for tool, tool_input in (("Write", {"file_path": target, "content": "{}"}),
                                     ("Edit", {"file_path": target, "old_string": "1", "new_string": "2"}),
                                     ("MultiEdit", {"file_path": target, "edits": []}),
                                     ("NotebookEdit", {"notebook_path": target, "new_source": "x"})):
                res = self._claude(tool, tool_input)
                self.assertEqual(res["__exit_code__"], 2, f"{tool} {target}")
                self.assertHeartbeatDenied(res["__stderr__"], target)

    def test_reads_not_denied(self):
        for c in (f"cat logs/{HEARTBEAT}", f"jq . logs/{HEARTBEAT}", "python3 scripts/trading_doctor.py"):
            self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)
        for command in (f"Get-Content logs\\{HEARTBEAT} | ConvertFrom-Json", f"Test-Path logs\\{HEARTBEAT}"):
            self.assertEqual(self.decision(self.ps(command)), "passthrough", command)

    def test_guard_still_refreshes_its_heartbeat(self):
        path = os.path.join(self.root, "logs", HEARTBEAT)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"hook": "pre_trade_guard", "last_seen_ts": 1}, f)
        self.agy(self.cmd(f"echo x > logs/{HEARTBEAT}"))
        with open(path, encoding="utf-8") as f:
            hb = json.load(f)
        self.assertEqual(hb["hook"], "pre_trade_guard")
        self.assertEqual(hb["decision"], "deny")
        self.assertGreater(hb["last_seen_ts"], time.time() - 60)


# =============================================================================
# #73.1: doctor validates the Claude Code matcher
# =============================================================================
REAL_MATCHER = "Bash|PowerShell|NotebookEdit|mcp__.*|Write|Edit|MultiEdit"
GUARD_CMD = "python3 \"$CLAUDE_PROJECT_DIR\"/scripts/hooks/pre_trade_guard.py"


def claude_settings(matcher=REAL_MATCHER, command=GUARD_CMD):
    return {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [{"type": "command", "command": command}]}],
                      "PostToolUse": [{"matcher": "Bash|PowerShell|mcp__.*", "hooks": [
                          {"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR\"/scripts/hooks/post_trade_sync.py"}]}]}}


class TestDoctorClaudeMatcher(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "scripts", "hooks"))
        os.makedirs(os.path.join(self.root, "logs"))
        with open(os.path.join(self.root, "scripts", "hooks", "guard.py"), "w", encoding="utf-8") as f:
            f.write("print('{}')\n")

    def agy_hooks(self):
        os.makedirs(os.path.join(self.root, ".agents"), exist_ok=True)
        cmd = f"{shlex.quote(sys.executable)} ../scripts/hooks/guard.py --agy"
        cfg = {"trading-safety-guard": {"enabled": True, "PreToolUse": [
            {"matcher": "run_command|call_mcp_tool", "hooks": [{"type": "command", "command": cmd}]}]}}
        with open(os.path.join(self.root, ".agents", "hooks.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def claude(self, name, cfg):
        os.makedirs(os.path.join(self.root, ".claude"), exist_ok=True)
        with open(os.path.join(self.root, ".claude", name), "w", encoding="utf-8") as f:
            f.write(cfg if isinstance(cfg, str) else json.dumps(cfg))

    def report(self):
        self.agy_hooks()
        return trading_doctor.check_pretool_hook(self.root, run_selftest=False)

    def test_real_settings_pass(self):
        # Committed file only: a machine-specific settings.local.json must not reach the test
        with open(os.path.join(REPO_ROOT, ".claude", "settings.json"), encoding="utf-8") as f:
            self.claude("settings.json", f.read())
        rep = self.report()
        self.assertTrue(rep["ok"], rep)
        self.assertTrue(any("Claude Code" in m for m in rep["info"]), rep)

    def test_real_example_as_local_copy_passes(self):
        with open(os.path.join(REPO_ROOT, ".claude", "settings.local.json.example"), encoding="utf-8") as f:
            self.claude("settings.local.json", f.read())
        rep = self.report()
        self.assertTrue(rep["ok"], rep)

    def test_each_missing_tool_is_critical(self):
        alternatives = REAL_MATCHER.split("|")
        expected = {"Bash": "Bash", "PowerShell": "PowerShell", "NotebookEdit": "NotebookEdit",
                    "mcp__.*": "mcp__binance__x", "Write": "Write", "Edit": "Edit", "MultiEdit": "MultiEdit"}
        for alt in alternatives:
            with self.subTest(alt=alt):
                self.claude("settings.json", claude_settings("|".join(a for a in alternatives if a != alt)))
                rep = self.report()
                self.assertFalse(rep["ok"], alt)
                crit = " ".join(rep["critical"])
                self.assertIn(expected[alt], crit)
                self.assertIn(".claude/settings", crit)

    def test_command_without_guard_is_critical(self):
        self.claude("settings.json", claude_settings(command="python3 \"$CLAUDE_PROJECT_DIR\"/scripts/hooks/other.py"))
        rep = self.report()
        self.assertFalse(rep["ok"])
        self.assertTrue(any("pre_trade_guard.py" in m for m in rep["critical"]), rep)

    def test_guard_only_on_post_tool_use_is_critical(self):
        cfg = claude_settings(command="python3 x.py")
        cfg["hooks"]["PostToolUse"][0]["matcher"] = REAL_MATCHER
        cfg["hooks"]["PostToolUse"][0]["hooks"][0]["command"] = GUARD_CMD
        self.claude("settings.json", cfg)
        self.assertFalse(self.report()["ok"])

    def test_unparsable_or_malformed_settings_are_critical(self):
        for bad in ("{not json", "[]", json.dumps({"hooks": []}), json.dumps({"hooks": {"PreToolUse": {}}})):
            with self.subTest(bad=bad):
                self.claude("settings.json", bad)
                rep = self.report()
                self.assertFalse(rep["ok"], bad)
                self.assertTrue(any(".claude/settings.json" in m for m in rep["critical"]), rep)

    def test_unparsable_local_copy_is_critical(self):
        self.claude("settings.json", claude_settings())
        self.claude("settings.local.json", "{oops")
        rep = self.report()
        self.assertFalse(rep["ok"])
        self.assertTrue(any(".claude/settings.local.json" in m for m in rep["critical"]), rep)

    def test_permissions_only_local_copy_adds_nothing(self):
        # Claude Code writes settings.local.json with only permissions ("don't ask again")
        self.claude("settings.json", claude_settings())
        self.claude("settings.local.json", {"permissions": {"allow": []}})
        rep = self.report()
        self.assertTrue(rep["ok"], rep)

    def test_permissions_only_file_alone_is_critical(self):
        for cfg in ({"permissions": {"allow": []}}, {"hooks": {"Stop": []}}):
            with self.subTest(cfg=cfg):
                self.claude("settings.local.json", cfg)
                rep = self.report()
                self.assertFalse(rep["ok"])
                self.assertTrue(any("does not route" in m and "Bash" in m for m in rep["critical"]), rep)

    def test_no_claude_dir_is_skipped(self):
        rep = self.report()
        self.assertTrue(rep["ok"], rep)
        self.assertTrue(any("Claude Code" in m and "skipped" in m for m in rep["info"]), rep)

    def test_union_of_both_files_covers(self):
        self.claude("settings.json", claude_settings("Bash|PowerShell|NotebookEdit"))
        self.claude("settings.local.json", claude_settings(
            "mcp__.*|Write|Edit|MultiEdit",
            "wsl.exe -d Ubuntu -- python3 /home/u/trading/scripts/hooks/pre_trade_guard.py"))
        rep = self.report()
        self.assertTrue(rep["ok"], rep)

    def test_checked_independently_of_agy_hooks(self):
        self.claude("settings.json", "{broken")
        rep = trading_doctor.check_pretool_hook(self.root, run_selftest=False)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("hooks.json not found" in m for m in rep["critical"]), rep)
        self.assertTrue(any(".claude/settings.json" in m for m in rep["critical"]), rep)


if __name__ == "__main__":
    unittest.main()
