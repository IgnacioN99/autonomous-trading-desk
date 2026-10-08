#!/usr/bin/env python3
"""
test_issue_49_powershell_notebookedit.py - Issue #49: the Claude Code PowerShell tool (Windows) and NotebookEdit
must reach pre_trade_guard.py.

Covers the hook matchers (.claude/settings.json and settings.local.json.example), PowerShell commands judged
like Bash (trading primitives get the same decision) plus the PowerShell backstop on protected paths, encoded
PowerShell payloads, NotebookEdit targets and the PostToolUse hooks accepting PowerShell.

Runs fully offline (GuardHarness from test_guard_bypasses); nothing is executed.
"""

import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "scripts" / "hooks"),
           str(REPO_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import post_trade_sync  # noqa: E402
import pre_trade_guard  # noqa: E402
from scripts.hooks import post_pr_review_hook as post_hook  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
import test_guard_bypasses as tgb  # noqa: E402  (fixtures only)
import test_claude_code_support as tccs  # noqa: E402  (fixtures only)

SESSION = tccs.SESSION
GROUND_TRUTH = ("session_state.json", "guardian_state.json", "pending_entries.json", "hook_heartbeat.json",
                "score_calibration.json", "trade_outcomes.jsonl")  # issue #202
WRITERS = {"session_state.json": "sync_session_state.py", "guardian_state.json": "position_guardian_loop.py",
           "pending_entries.json": "execute_futures_trade.py", "hook_heartbeat.json": "pre_trade_guard.py",
           "score_calibration.json": "trading_scorecard.py", "trade_outcomes.jsonl": "trade_outcomes.py"}


def path_forms(rel_windows: str):
    """A logs\\... path in relative, .\\ relative, Windows absolute and WSL absolute forms."""
    return [rel_windows, ".\\" + rel_windows, "C:\\Users\\x\\repo\\" + rel_windows,
            "/mnt/c/Users/x/repo/" + rel_windows.replace("\\", "/")]


def write_commands(p: str):
    """PowerShell commands writing, deleting, moving or aliasing path p."""
    return [
        f"Set-Content -Path {p} -Value '{{}}'",
        f"Set-Content '{p}' '{{}}'",
        f"'{{}}' | Out-File {p}",
        f"'{{}}' > {p}",
        f"Add-Content {p} 'x'",
        f"Remove-Item {p}",
        f"Remove-Item -Force '{p}'",
        f"Copy-Item C:\\tmp\\fake.json {p}",
        f"Move-Item C:\\tmp\\fake.json {p}",
        f"[IO.File]::WriteAllText('{p}', '{{}}')",
        f"New-Item -ItemType SymbolicLink -Path {p} -Target C:\\tmp\\fake.json",
        f"New-Item -ItemType HardLink -Path C:\\tmp\\alias.json -Target {p}",
    ]


# =============================================================================
# Hook matchers
# =============================================================================
class TestSettingsMatchers(unittest.TestCase):

    FILES = (".claude/settings.json", ".claude/settings.local.json.example")

    def _matchers(self, rel: str) -> dict:
        cfg = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))
        out = {}
        for event in ("PreToolUse", "PostToolUse"):
            for group in cfg["hooks"][event]:
                for hook in group["hooks"]:
                    script = re.search(r"scripts/hooks/(\w+)\.py", hook["command"]).group(1)
                    out[script] = group["matcher"]
        return out

    def test_powershell_and_notebookedit_reach_the_guard(self):
        for rel in self.FILES:
            m = self._matchers(rel)
            for tool in ("Bash", "PowerShell", "NotebookEdit", "Write", "Edit", "MultiEdit", "mcp__binance__x"):
                self.assertTrue(re.fullmatch(m["pre_trade_guard"], tool), f"{rel}: {tool}")
            for tool in ("Read", "Grep", "Glob"):
                self.assertFalse(re.fullmatch(m["pre_trade_guard"], tool), f"{rel}: {tool}")

    def test_post_hooks_accept_powershell(self):
        for rel in self.FILES:
            m = self._matchers(rel)
            for tool in ("Bash", "PowerShell", "mcp__binance__x"):
                self.assertTrue(re.fullmatch(m["post_trade_sync"], tool), f"{rel}: {tool}")
            for tool in ("Bash", "PowerShell"):
                self.assertTrue(re.fullmatch(m["post_pr_review_hook"], tool), f"{rel}: {tool}")

    def test_both_settings_files_use_identical_matchers(self):
        self.assertEqual(self._matchers(self.FILES[0]), self._matchers(self.FILES[1]))


# =============================================================================
# PowerShell in the pre-trade guard
# =============================================================================
class PowerShellHarness(tgb.GuardHarness):

    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.root, "claude_projects")
        os.environ[dp.CLAUDE_PROJECTS_ENV] = self.projects  # restored by GuardHarness' patch.dict

    def _claude(self, tool: str, tool_input: dict) -> dict:
        return self.run_guard({"session_id": SESSION, "hook_event_name": "PreToolUse", "cwd": self.root,
                               "tool_name": tool, "tool_input": tool_input})

    def ps(self, command: str) -> dict:
        return self._claude("PowerShell", {"command": command, "description": "test", "timeout": 1000})

    def bash(self, command: str) -> dict:
        return self._claude("Bash", {"command": command, "description": "test"})

    @staticmethod
    def decision(res: dict) -> str:
        if res["__exit_code__"] == 2:
            return "deny"
        return res.get("hookSpecificOutput", {}).get("permissionDecision", "passthrough")

    def assertPsDenied(self, command: str, fragment: str = None):
        res = self.ps(command)
        self.assertEqual(res["__exit_code__"], 2, f"{command!r}: {res}")
        if fragment:
            self.assertIn(fragment, res["__stderr__"], command)
        return res

    def assertPsNotDenied(self, command: str):
        res = self.ps(command)
        self.assertEqual(res["__exit_code__"], 0, f"{command!r}: {res['__stderr__']}")
        return res


class TestPowerShellProtectedPaths(PowerShellHarness):

    def test_ground_truth_writes_denied_in_every_path_form(self):
        for name in GROUND_TRUTH:
            for p in path_forms(f"logs\\{name}"):
                for command in write_commands(p):
                    res = self.assertPsDenied(command, f"logs/{name} may only be written by")
                    self.assertIn(WRITERS[name], res["__stderr__"], command)

    def test_dossier_writes_denied_in_every_path_form(self):
        for p in path_forms("logs\\evaluations\\latest_dossier.json"):
            for command in write_commands(p):
                self.assertPsDenied(command, "Evaluation Trail Protection")

    def test_logs_directory_destruction_and_aliasing_denied(self):
        for command in ("Remove-Item -Recurse .\\logs", "Remove-Item -Recurse -Force logs",
                        "Remove-Item C:\\Users\\x\\repo\\logs -Recurse", "rd /s /q logs", "rm -r -fo logs",
                        "Get-ChildItem logs | Remove-Item -Recurse", "Get-ChildItem .\\logs\\*.json | Remove-Item",
                        "New-Item -ItemType Junction -Path st -Target .\\logs",
                        "New-Item -ItemType SymbolicLink -Path st -Target C:\\Users\\x\\repo\\logs",
                        "Copy-Item -Recurse C:\\tmp\\fake\\* .\\logs", "Move-Item logs C:\\tmp\\old",
                        "Rename-Item logs logs_old", "Remove-Item (Join-Path . logs) -Recurse",
                        "Get-ChildItem logs -Filter *.json | % Delete",
                        "Get-ChildItem logs | ForEach-Object -MemberName Delete",
                        "robocopy C:\\tmp\\empty .\\logs /MIR", "xcopy /E /Y C:\\tmp\\fake logs",
                        "Set-Content logs\\SESSIO~1.JSO '{}'", "Remove-Item * -Recurse -Force"):
            self.assertPsDenied(command, "may only be written by")
        # A glob that can expand to logs/ only matters next to a write construct; navigation is read-only
        for command in ("git add *", "Set-Location logs; Get-Content guardian_state.json", "cd logs; dir"):
            self.assertPsNotDenied(command)

    def test_obfuscated_and_indirect_writes_denied(self):
        for command in (
                "Set-`Content logs\\session_state.json '{}'",
                "Remove-Item `\n logs\\guardian_state.json",
                "${C:\\Users\\x\\repo\\logs\\session_state.json} = '{}'",
                "Get-Item logs\\pending_entries.json | ForEach-Object { $_.Delete() }",
                "(Get-Item logs\\session_state.json).Delete()",
                "Get-Content logs\\session_state.json | Set-Content C:\\tmp\\x.json",
                "Get-Content logs\\session_state.json | Where-Object { Remove-Item logs\\session_state.json }",
                "$p = 'logs\\guardian_state.json'; Clear-Content $p",
                "Invoke-Expression \"Remove-Item logs\\session_state.json\"",
                "& { Set-Content logs\\session_state.json '{}' }",
                ". .\\tmp.ps1 logs\\session_state.json",
                "python tools\\fix.py logs\\session_state.json",
                "Get-Content logs\\session_state.json >> C:\\tmp\\copy.json",
                "Get-Content logs\\session_state.json *> C:\\tmp\\copy.json",
                "Get-Content logs\\session_state.json 2>&1",
        ):
            self.assertPsDenied(command, "may only be written by")

    def test_unparseable_command_fails_closed(self):
        res = self.assertPsDenied("Get-Content 'logs\\session_state.json")
        self.assertIn("FAIL-CLOSED", res["__stderr__"])
        self.assertPsDenied("Write-Output \"unterminated")

    def test_read_only_access_is_not_denied(self):
        for command in (
                "Get-Content logs\\session_state.json | ConvertFrom-Json",
                "Get-Content C:\\Users\\x\\repo\\logs\\guardian_state.json -Raw",
                "gc .\\logs\\pending_entries.json | ConvertFrom-Json | Select-Object -First 1",
                "type logs\\session_state.json",
                "Select-String -Path .\\logs\\pending_entries.json -Pattern symbol",
                "sls is_valid logs\\session_state.json",
                "Test-Path logs\\session_state.json",
                "Get-FileHash logs\\session_state.json",
                "(Get-Content logs\\session_state.json | ConvertFrom-Json).is_valid",
                "Get-Content logs\\session_state.json 2>$null",
                "Get-ChildItem logs", "Get-ChildItem .\\logs\\*.json", "dir C:\\Users\\x\\repo\\logs",
                "git status", "git log --oneline -5",
                "Get-Content \"say `\"hi`\"\" ",
        ):
            res = self.assertPsNotDenied(command)
            self.assertNotEqual(self.decision(res), "allow", command)

    def test_unrelated_writes_keep_normal_policy(self):
        for command in ("Set-Content notes.txt 'x'", "Remove-Item C:\\tmp\\x.txt", "'x' > C:\\tmp\\out.txt",
                        "Copy-Item logs\\guardian.log C:\\tmp\\guardian.log", "New-Item -ItemType Directory build"):
            self.assertEqual(self.decision(self.assertPsNotDenied(command)), "passthrough", command)

    def test_harness_writes_require_confirmation(self):
        self.assertEqual(self.decision(self.ps("Set-Content scripts\\hooks\\pre_trade_guard.py 'x'")), "ask")
        self.assertEqual(self.decision(self.ps("Remove-Item .claude\\settings.json")), "ask")
        self.assertEqual(self.decision(self.bash("echo x > scripts/hooks/pre_trade_guard.py")), "ask")

    def test_bash_read_programs_not_widened(self):
        # PowerShell read cmdlets are only exempt for the PowerShell tool
        self.assertEqual(self.decision(self.bash("Get-Content logs/session_state.json")), "deny")
        self.assertEqual(self.decision(self.bash("sort -o logs/session_state.json x")), "deny")
        self.assertEqual(self.decision(self.bash("cat logs/session_state.json")), "passthrough")


class TestPowerShellEncodedCommands(PowerShellHarness):

    def test_encoded_payloads_denied(self):
        for command in (
                "powershell -EncodedCommand SQBFAFgA",
                "powershell.exe -NoProfile -e JABzAD0A",
                "pwsh -enc SQBFAFgA", "pwsh -ec SQBFAFgA", "powershell /enc SQBFAFgA",
                "powershell -encodedc`ommand SQBFAFgA",
                "Start-Process powershell -ArgumentList '-enc','SQBFAFgA'",
                "iex ([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('SQBFAFgA')))",
                "& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('SQ=='))))",
                "powershell -Command ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('SQ==')))",
        ):
            self.assertPsDenied(command, "Obfuscated Execution")

    def test_plain_powershell_invocations_not_flagged(self):
        for command in ("powershell -ExecutionPolicy Bypass -File C:\\tmp\\build.ps1",
                        "pwsh -NoProfile -Command Get-Date",
                        "[Convert]::FromBase64String('SQ==').Length"):
            res = self.ps(command)
            self.assertNotIn("Obfuscated Execution", res["__stderr__"], command)


class TestPowerShellTradingParity(PowerShellHarness):

    OPEN = "--symbol BTCUSDT --direction LONG --leverage 3 --env prod"

    def ps_forms(self, args: str):
        return [f"python scripts\\execute_futures_trade.py {args}",
                f"python.exe .\\scripts\\execute_futures_trade.py {args}",
                f"wsl.exe -d Ubuntu -- python3 scripts/execute_futures_trade.py {args}",
                f"& python scripts\\execute_futures_trade.py {args}"]

    def assertParity(self, args: str, expected: str):
        bash = self.bash(f"python3 scripts/execute_futures_trade.py {args}")
        self.assertEqual(self.decision(bash), expected, args)
        for command in self.ps_forms(args):
            res = self.ps(command)
            self.assertEqual(self.decision(res), expected, command)
            if expected == "deny":
                self.assertEqual(res["__stderr__"], bash["__stderr__"], command)

    def test_opening_without_dossier_denied_like_bash(self):
        self.assertParity(self.OPEN, "deny")
        self.assertIn("Clean-Room Evaluator Required", self.ps(self.ps_forms(self.OPEN)[0])["__stderr__"])

    def test_opening_with_dossier_allowed_like_bash(self):
        tccs.TestGuardWithClaudeDossier.write_claude_dossier(self)
        self.assertParity(self.OPEN, "allow")
        # Same gates: a dossier for LONG does not cover SHORT
        self.assertParity(self.OPEN.replace("LONG", "SHORT"), "deny")

    def test_read_only_and_risk_reducing_modes_match_bash(self):
        self.assertParity("--positions --json", "passthrough")
        self.assertParity("--close-position --symbol BTCUSDT", "allow")
        self.assertParity("--move-breakeven --symbol BTCUSDT", "allow")
        self.assertParity("--audit-orphans", "allow")
        self.assertParity("--move-breakeven", "deny")

    def test_other_trading_primitives_match_bash(self):
        pairs = [
            ("python -c \"import execute_futures_trade\"", "python3 -c \"import execute_futures_trade\""),
            ("curl.exe -X POST https://fapi.binance.com/fapi/v1/order", "curl -X POST https://fapi.binance.com/fapi/v1/order"),
            ("python scripts\\deploy_basket.py --env prod", "python3 scripts/deploy_basket.py --env prod"),
            ("python scripts\\record_evaluation.py --env prod --symbols BTCUSDT",
             "python3 scripts/record_evaluation.py --env prod --symbols BTCUSDT"),
            ("Get-Content logs\\evaluations\\latest_dossier.json", "cat logs/evaluations/latest_dossier.json"),
            ("$env:CLAUDE_PROJECTS_DIRS='C:\\tmp'; python scripts\\record_evaluation.py --from-claude-subagent a1",
             "CLAUDE_PROJECTS_DIRS=/tmp python3 scripts/record_evaluation.py --from-claude-subagent a1"),
        ]
        for ps_cmd, bash_cmd in pairs:
            self.assertEqual(self.decision(self.ps(ps_cmd)), "deny", ps_cmd)
            self.assertEqual(self.decision(self.bash(bash_cmd)), "deny", bash_cmd)

    def test_powershell_raw_http_writes_to_binance_denied(self):
        for command in ("Invoke-RestMethod -Method Post -Uri https://fapi.binance.com/fapi/v1/order -Body $b",
                        "irm https://fapi.binance.com/fapi/v1/order -Method POST",
                        "Invoke-WebRequest -Uri https://agent.binance.com/mcp -Method:Post -Body '{}'"):
            self.assertPsDenied(command, "Choke Point Enforcement")
        self.assertPsNotDenied("Invoke-RestMethod https://fapi.binance.com/fapi/v1/time")

    def test_risk_reducing_compound_with_protected_write_denied(self):
        self.assertPsDenied("python scripts\\execute_futures_trade.py --close-position --symbol BTCUSDT; "
                            "Remove-Item logs\\session_state.json", "may only be written by")


# =============================================================================
# Review round 2: quoting/escapes/comments, nested blocks, encoded runners, wsl.exe
# =============================================================================
ROBOCOPY = "robocopy C:\\tmp\\fake logs session_state.json"
OPEN_BTC = "python scripts\\execute_futures_trade.py --symbol BTCUSDT --direction LONG --env prod"


class TestPowerShellQuotingAndComments(PowerShellHarness):

    def test_backtick_is_literal_inside_single_quotes(self):
        res = self.assertPsDenied(f"echo 'a`'; {OPEN_BTC}; echo '`'", "Clean-Room Evaluator Required")
        bash = self.bash("echo 'a`'; python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG "
                         "--env prod; echo '`'")
        self.assertEqual(self.decision(bash), "deny")
        self.assertEqual(res["__stderr__"], bash["__stderr__"])
        self.assertPsDenied(f"echo 'a`'; {ROBOCOPY}; echo '`'", "may only be written by")
        self.assertPsNotDenied("Write-Output 'a`b' 'it''s'")

    def test_unicode_quotes_and_dashes(self):
        self.assertPsDenied(f"echo 'x\u2019; {ROBOCOPY}; echo \u2018y'", "may only be written by")
        self.assertPsDenied(f"echo \u201cx\u201d; {ROBOCOPY}; echo \u201ey\"", "may only be written by")
        for dash in ("\u2013", "\u2014", "\u2015"):
            self.assertPsDenied(f"powershell {dash}enc SQBFAFgA", "Obfuscated Execution")
            self.assertPsDenied(f"powershell {dash}EncodedCommand SQBFAFgA", "Obfuscated Execution")

    def test_comments_are_stripped(self):
        # A comment cannot turn a trade opening into a risk-reducing action
        self.assertPsDenied(f"{OPEN_BTC} <# --close-position #>", "Clean-Room Evaluator Required")
        self.assertPsDenied(f"{OPEN_BTC} # --close-position", "Clean-Room Evaluator Required")
        # A quote inside a comment does not desynchronise the quoting of the next line
        self.assertPsDenied(f"Get-Date # don't\n{ROBOCOPY} # '", "may only be written by")
        self.assertPsDenied(f"Get-Date <# it's #> ; {ROBOCOPY}", "may only be written by")
        self.assertPsNotDenied("Get-ChildItem # it's fine")
        self.assertPsNotDenied("git log --format=%h#%s -3")
        self.assertPsNotDenied("Get-Content logs\\session_state.json <# don't #> | ConvertFrom-Json")

    def test_unparseable_text_fails_closed(self):
        for command in ("Get-Date <# open", "Get-Date )", "Get-Date (", "Write-Output 'open",
                        "Get-ChildItem | Where-Object { $_.Length", "Write-Output \"$(Get-Date\""):
            self.assertIn("FAIL-CLOSED", self.assertPsDenied(command)["__stderr__"], command)


class TestPowerShellNestedBlocks(PowerShellHarness):

    def test_code_inside_blocks_is_judged(self):
        for command in (
                f"Get-Date | ? {{ {ROBOCOPY} }}",
                f"Get-ChildItem | Where-Object {{ {ROBOCOPY} }}",
                f"Get-ChildItem | Sort-Object {{ {ROBOCOPY} }}",
                f"Get-ChildItem | Select-Object @{{n='x';e={{ {ROBOCOPY} }}}}",
                f"Get-ChildItem | ForEach-Object {{ {ROBOCOPY} }}",
                f"Get-Item C:\\tmp\\x | Get-Content -Path {{ {ROBOCOPY} }}",
                f"Get-Content \"x$({ROBOCOPY})\"",
                f"echo ({ROBOCOPY})",
                f"echo @({ROBOCOPY})",
                f"echo $({ROBOCOPY})",
                f"$x = {ROBOCOPY}",
                f"$x = ({ROBOCOPY})",
                f"Write-Output @\"\nnote $({ROBOCOPY})\n\"@",
        ):
            self.assertPsDenied(command, "may only be written by")

    def test_trade_opening_inside_a_subexpression_is_gated(self):
        self.assertPsDenied(f"echo \"$({OPEN_BTC})\"", "Clean-Room Evaluator Required")
        self.assertPsDenied(f"Write-Output {{ {OPEN_BTC} }}", "Clean-Room Evaluator Required")
        self.assertPsDenied(f"{OPEN_BTC} --confirmed; echo $({OPEN_BTC.replace('BTCUSDT', 'ETHUSDT')})")

    def test_nested_blocks_never_auto_allow(self):
        for command in ("python scripts\\execute_futures_trade.py --close-position --symbol "
                        "\"BTCUSDT$(Remove-Item C:\\data -Recurse)\"",
                        "python scripts\\execute_futures_trade.py --close-position --symbol (Write-Output BTCUSDT)"):
            res = self.assertPsNotDenied(command)
            self.assertNotEqual(self.decision(res), "allow", command)
        tccs.TestGuardWithClaudeDossier.write_claude_dossier(self)
        # Gates pass, but nested blocks downgrade allow to ask (Claude mode: no output = normal permission policy)
        res = self.ps(f"{OPEN_BTC} --leverage $(3)")
        self.assertEqual(self.decision(res), "passthrough", res)
        self.assertEqual(pre_trade_guard.evaluate_powershell_command(
            f"{OPEN_BTC} --leverage $(3)", self.root, self.root, SESSION)[0], "ask")
        self.assertEqual(self.decision(self.ps(f"{OPEN_BTC} --leverage 3")), "allow")

    def test_depth_limit(self):
        deep = "(" * (pre_trade_guard.NESTED_DEPTH_LIMIT + 2) + "Get-Date" + ")" * (pre_trade_guard.NESTED_DEPTH_LIMIT + 2)
        self.assertIn("nested deeper", self.assertPsDenied(deep)["__stderr__"])
        ok = "(" * 3 + "Get-Date" + ")" * 3
        self.assertPsNotDenied(ok)

    def test_harmless_blocks_and_reads_stay_allowed(self):
        for command in ("Get-ChildItem | Where-Object { $_.Length -gt 0 }",
                        "Get-Content logs\\session_state.json | ConvertFrom-Json | Select-Object -Property x",
                        "Get-Content logs\\session_state.json | Where-Object { $_ -match 'is_valid' }",
                        "if (Test-Path logs\\session_state.json) { Get-Content logs\\session_state.json }",
                        "Write-Output \"`$(robocopy is not run here) session_state.json\"",
                        "Get-ChildItem | Sort-Object { $_.LastWriteTime } | Select-Object -First 1"):
            self.assertPsNotDenied(command)


class TestPowerShellEncodedRunners(PowerShellHarness):

    def test_execution_context_runners_denied(self):
        payload = "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('SQBFAFgA'))"
        for command in (f"$ExecutionContext.InvokeCommand.InvokeScript({payload})",
                        f"$ExecutionContext.InvokeCommand.NewScriptBlock({payload}).Invoke()",
                        f"[ScriptBlock]::Create({payload}).Invoke()",
                        f"$s = {payload}; $sb = [scriptblock]::Create($s); $sb.Invoke()"):
            self.assertPsDenied(command, "Obfuscated Execution")


class TestPowerShellWsl(PowerShellHarness):

    def test_wsl_reads_judged_with_bash_rules(self):
        for command in ("wsl.exe -d Ubuntu -- cat logs/session_state.json",
                        "wsl.exe -d Ubuntu -- head -n 5 logs/guardian_state.json",
                        "wsl.exe -d Ubuntu -- jq .is_valid logs/session_state.json",
                        "wsl -- grep -c symbol logs/pending_entries.json",
                        "wsl.exe --cd /mnt/c/x -u nacho -e cat logs/session_state.json",
                        "C:\\Windows\\System32\\wsl.exe -d Ubuntu -- tail logs/session_state.json"):
            self.assertPsNotDenied(command)

    def test_wsl_writes_denied(self):
        for command in ("wsl.exe -d Ubuntu -- bash -lc 'echo {} > logs/session_state.json'",
                        "wsl.exe -- rm logs/pending_entries.json",
                        "wsl.exe -d Ubuntu -- cp /tmp/x logs/session_state.json",
                        "wsl.exe -d Ubuntu -- sed -i s/a/b/ logs/guardian_state.json",
                        "wsl.exe -d Ubuntu -- jq -i . logs/session_state.json",
                        "wsl.exe -d Ubuntu -- bash -c 'rm -rf logs'",
                        "wsl --import Distro C:\\x logs\\session_state.json",
                        "wsl.exe -d Ubuntu -- cat logs/session_state.json > C:\\tmp\\x.json"):
            self.assertPsDenied(command, "may only be written by")
        self.assertPsDenied("wsl.exe -d Ubuntu -- cat logs/evaluations/latest_dossier.json",
                            "Evaluation Trail Protection")


class TestRound3ReviewFindings(PowerShellHarness):

    def test_lone_carriage_return_ends_lines(self):
        self.assertPsDenied("Get-Date #\rRemove-Item logs\\session_state.json", "may only be written by")
        for command in (f"Get-Date #\r{OPEN_BTC}", f"echo x\r{OPEN_BTC}"):
            self.assertPsDenied(command, "Clean-Room Evaluator Required")
        self.assertPsNotDenied("Get-Date # it's a comment\r\nGet-ChildItem logs")

    def test_wsl_default_shell_reparses_joined_arguments(self):
        for command in ("wsl.exe -d Ubuntu -- cat /tmp/fake '>logs/session_state.json'",
                        "wsl cat /tmp/fake '>' logs/guardian_state.json",
                        "wsl.exe -d Ubuntu -- echo x '&&' rm -rf logs"):
            self.assertPsDenied(command, "may only be written by")
        self.assertPsNotDenied("wsl.exe -d Ubuntu -- cat logs/session_state.json")
        # -e/--exec runs the program without a shell: the quoted '>' is a literal file name for cat
        self.assertPsNotDenied("wsl.exe -d Ubuntu -e cat /tmp/fake '>logs/session_state.json'")
        self.assertPsDenied("wsl.exe -d Ubuntu -- python3 scripts/execute_futures_trade.py --symbol BTCUSDT "
                            "--direction LONG --env prod", "Clean-Room Evaluator Required")
        self.assertEqual(self.decision(self.ps("wsl.exe -d Ubuntu -- python3 scripts/execute_futures_trade.py "
                                               "--close-position --symbol BTCUSDT")), "allow")

    def test_piping_into_powershell_interpreters_is_inline_code(self):
        quoted = OPEN_BTC.replace("\\", "/")
        for command in (f"echo '{quoted}' | iex",
                        f"echo '{OPEN_BTC}' | Invoke-Expression",
                        f"Invoke-Expression '{OPEN_BTC}'",
                        f"iex \"{OPEN_BTC}\"",
                        f"'{OPEN_BTC}' | powershell -",
                        f"'{OPEN_BTC}' | pwsh -Command -",
                        f"'{OPEN_BTC}' | C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                        f"[scriptblock]::Create('{OPEN_BTC}').Invoke()"):
            self.assertPsDenied(command, "Choke Point Enforcement")
        bash = self.bash("echo 'python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG' | bash")
        self.assertEqual(self.decision(bash), "deny")
        self.assertPsNotDenied("Get-ChildItem | Select-Object Name")


class TestFallbackNormaliserAgrees(unittest.TestCase):

    def test_fallback_matches_guard_escapes(self):
        for text in ("Set-`Content C:\\repo\\logs\\x", "a `\r\nb `\nc", "Write-Output \"a `\"b`\" c\"",
                     "powershell \u2013enc X", "python scripts\\execute_futures_trade.py --close-position --symbol X",
                     "echo \u201cx\u201d"):
            self.assertEqual(post_trade_sync._fallback_powershell_text(text),
                             pre_trade_guard.normalize_powershell_command(text), text)


# =============================================================================
# NotebookEdit
# =============================================================================
class TestNotebookEdit(PowerShellHarness):

    def notebook(self, path: str) -> dict:
        return self._claude("NotebookEdit", {"notebook_path": path, "new_source": "x", "cell_id": "c1",
                                             "cell_type": "code", "edit_mode": "replace"})

    def test_protected_targets_denied(self):
        for name in GROUND_TRUTH:
            for p in (f"logs/{name}", f"C:\\Users\\x\\repo\\logs\\{name}", f"/mnt/c/Users/x/repo/logs/{name}"):
                res = self.notebook(p)
                self.assertEqual(res["__exit_code__"], 2, p)
                self.assertIn(f"logs/{name} may only be written by", res["__stderr__"], p)
        for p in ("logs/evaluations/latest_dossier.json", "C:\\Users\\x\\repo\\logs\\evaluations\\latest_dossier.json",
                  "/mnt/c/Users/x/repo/logs/evaluations/latest_dossier.json",
                  "/home/u/.claude/projects/-repo/" + SESSION + "/subagents/agent-a0123456789abcdef.jsonl"):
            res = self.notebook(p)
            self.assertEqual(res["__exit_code__"], 2, p)
            self.assertIn("Evaluation Trail Protection", res["__stderr__"], p)

    def test_windows_dossier_path_denied_for_every_file_tool(self):
        # A Windows absolute path is not absolute for the hook under WSL: it must still hit the trail protection
        for target in ("C:\\Users\\x\\repo\\logs\\evaluations\\latest_dossier.json",
                       "file:///C:/Users/x/repo/logs/evaluations/latest_dossier.json",
                       "C:\\Users\\x\\repo\\LOGS\\Evaluations.\\latest_dossier.json"):
            for tool in ("Write", "Edit", "MultiEdit"):
                res = self._claude(tool, {"file_path": target, "content": "{}"})
                self.assertEqual(res["__exit_code__"], 2, f"{tool} {target}")
                self.assertIn("Evaluation Trail Protection", res["__stderr__"])
        res = self._claude("Write", {"file_path": "C:\\Users\\x\\repo\\docs\\evaluations.md", "content": "x"})
        self.assertEqual(res["__exit_code__"], 0)

    def test_ordinary_notebook_not_denied(self):
        for p in ("research/analysis.ipynb", "C:\\Users\\x\\repo\\research\\analysis.ipynb"):
            res = self.notebook(p)
            self.assertEqual(res["__exit_code__"], 0, p)
            self.assertNotIn("permissionDecision", json.dumps(res.get("hookSpecificOutput", {})), p)


# =============================================================================
# PostToolUse hooks with PowerShell payloads
# =============================================================================
class TestPostHooksAcceptPowerShell(tgb.GuardHarness):

    @staticmethod
    def payload(command: str, tool: str = "PowerShell") -> dict:
        return {"session_id": SESSION, "hook_event_name": "PostToolUse", "tool_name": tool,
                "tool_input": {"command": command}, "tool_response": {"stdout": "", "stderr": ""}}

    @patch("post_trade_sync.subprocess.run")
    def test_post_trade_sync_classifies_powershell_like_bash(self, mock_run):
        os.makedirs(os.path.join(self.root, "scripts"), exist_ok=True)
        open(os.path.join(self.root, "scripts", "sync_session_state.py"), "w").close()
        with patch("execute_futures_trade.audit_orphan_positions") as mock_audit, \
                patch("post_trade_sync.find_workspace_root", return_value=self.root):
            for command in ("python scripts\\execute_futures_trade.py --close-position --symbol BTCUSDT --env testnet",
                            "wsl.exe -d Ubuntu -- python3 scripts/execute_futures_trade.py --move-breakeven "
                            "--symbol BTCUSDT --env testnet"):
                res = post_trade_sync.handle_post_trade_sync(self.payload(command))
                self.assertTrue(res["order_placed"], command)
                self.assertFalse(res["is_opening"], command)
                self.assertTrue(res["synced"], command)
            mock_audit.assert_not_called()
            res = post_trade_sync.handle_post_trade_sync(self.payload(
                "python scripts\\execute_futures_trade.py --symbol BTCUSDT --direction LONG --env testnet"))
            self.assertTrue(res["is_opening"])
            mock_audit.assert_called_once_with(target_env="testnet", auto_heal=True)
        for command in ("python scripts\\execute_futures_trade.py --positions --json",
                        "Get-Content scripts\\execute_futures_trade.py"):
            res = post_trade_sync.handle_post_trade_sync(self.payload(command))
            self.assertFalse(res["order_placed"], command)

    def test_post_trade_sync_fallback_without_guard(self):
        with patch.object(post_trade_sync, "_guard", None):
            call = post_trade_sync._normalize(self.payload("python scripts\\execute_futures_trade.py --audit-orphans"))
        self.assertEqual(call["kind"], "run_command")
        self.assertEqual(post_trade_sync.classify_command(call["command"]), (True, False))

    def test_post_pr_review_hook_reads_powershell_commands(self):
        self.assertEqual(post_hook.extract_command(self.payload("gh pr create --fill")), "gh pr create --fill")
        self.assertEqual(post_hook.extract_command(self.payload("gh pr create --fill", tool="Write")), "")


class TestNormalization(unittest.TestCase):

    def test_powershell_normalisation(self):
        norm = pre_trade_guard.normalize_powershell_command
        self.assertEqual(norm("Set-`Content C:\\repo\\logs\\x"), "Set-Content C:/repo/logs/x")
        self.assertEqual(norm("a `\r\nb `\nc"), "a  b  c")
        self.assertEqual(norm("Write-Output \"a `\"b`\" c\""), "Write-Output \"a b c\"")

    def test_normalize_tool_call_tags_powershell(self):
        call = pre_trade_guard.normalize_tool_call({"tool_name": "PowerShell", "cwd": "C:\\repo",
                                                    "tool_input": {"command": "git status"}})
        self.assertEqual((call["kind"], call["shell"], call["command"], call["cwd"]),
                         ("run_command", "powershell", "git status", "C:\\repo"))
        call = pre_trade_guard.normalize_tool_call({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual((call["kind"], call["shell"]), ("run_command", "bash"))
        call = pre_trade_guard.normalize_tool_call({"tool_name": "NotebookEdit",
                                                    "tool_input": {"notebook_path": "logs/session_state.json"}})
        self.assertEqual((call["kind"], call["target_file"]), ("file_write", "logs/session_state.json"))


if __name__ == "__main__":
    unittest.main()
