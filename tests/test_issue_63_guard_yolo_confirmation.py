#!/usr/bin/env python3
"""
test_issue_63_guard_yolo_confirmation.py - Issue #63: pre_trade_guard must mirror the executor's PROD rule that
YOLO entries (and candidates flagged requires_user_confirmation) always need --confirmed, even with
autonomous_execution_tier_s enabled.

Runs fully offline in a temp workspace (GuardHarness from test_guard_bypasses); nothing is executed.
"""

import json
import os
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "scripts" / "hooks"),
           str(REPO_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pre_trade_guard  # noqa: E402
import test_guard_bypasses as tgb  # noqa: E402  (fixtures only)
import test_claude_code_support as tccs  # noqa: E402  (fixtures only)
import test_issue_49_powershell_notebookedit as t49  # noqa: E402  (fixtures only)

YOLO_GATE = "YOLO Confirmation"
CONFIRM_GATE = "User Confirmation Required"
SCRIPT = "python3 scripts/execute_futures_trade.py"
EXECUTOR_SOURCE = REPO_ROOT / "scripts" / "execute_futures_trade.py"


def executor_cli_options() -> set:
    """Long option strings registered with add_argument in the executor's argparse."""
    calls = re.findall(r"add_argument\(([^)]*)", EXECUTOR_SOURCE.read_text(encoding="utf-8"))
    return {opt for call in calls for opt in re.findall(r"[\"'](--[A-Za-z0-9_-]+)[\"']", call)}


class YoloGuardHarness(tgb.GuardHarness):
    """Profile from GuardHarness: autonomous_execution_tier_s=True, yolo_slot_enabled=True, leverage_standard=3."""

    def deploy(self, flags="", env="prod", symbol="PEPEUSDT", direction="LONG", prefix=""):
        command = f"{prefix}{SCRIPT} --symbol {symbol} --direction {direction} --leverage 3 --env {env} {flags}".strip()
        return self.agy(self.cmd(command, conversationId=tgb.PARENT_CONV_ID))

    def dossier(self, symbol="PEPEUSDT", **cand):
        self.write_provenance_dossier(symbol=symbol, extra=cand)

    def assertAllowed(self, res):
        self.assertEqual(res.get("decision"), "allow", res)

    def assertNotNewGate(self, res):
        reason = res.get("reason", "")
        self.assertNotIn(YOLO_GATE, reason, res)
        self.assertNotIn(CONFIRM_GATE, reason, res)


class TestYoloConfirmationProd(YoloGuardHarness):

    def test_yolo_candidate_without_confirmed_denied(self):
        self.dossier(is_yolo=True, tier="Tier S", requires_user_confirmation=False)
        res = self.deploy()
        self.assertDenied(res, YOLO_GATE)
        self.assertIn("PEPEUSDT is a YOLO entry", res["reason"])
        self.assertIn("--confirmed", res["reason"])
        self.assertNotIn("execute_futures_trade.py", res["reason"])  # never echoes the raw command

    def test_yolo_candidate_with_confirmed_allowed(self):
        self.dossier(is_yolo=True, tier="Tier S", requires_user_confirmation=False)
        self.assertAllowed(self.deploy("--confirmed"))
        self.assertAllowed(self.deploy("--user-confirmed"))

    def test_abbreviated_confirmation_flags_denied(self):
        """Brief decision 4: only the exact --confirmed / --user-confirmed count (abbreviations fail closed)."""
        self.dossier(is_yolo=True, tier="Tier S", requires_user_confirmation=False)
        for flag in ("--co", "--conf", "--confirm", "--u", "--user-c", "--user-conf"):
            with self.subTest(flag=flag):
                self.assertDenied(self.deploy(flag), YOLO_GATE)

    def test_text_that_never_reaches_the_executor_as_confirmation_denied(self):
        """Round 2: confirmation is read from the executor's own arguments, not from the raw command text."""
        self.dossier(is_yolo=True, tier="Tier S", requires_user_confirmation=False)
        cases = {
            "comment": self.deploy("# --confirmed"),
            "env assignment": self.deploy(prefix="X=--confirmed "),
            "explicit value": self.deploy("--user-confirmed=false"),
            "unregistered underscore alias": self.deploy("--user_confirmed"),
            "ambiguous prefix (--close-position / --confirmed)": self.deploy("--c"),
            "quoted value": self.deploy("--order-type 'LIMIT --confirmed'"),
            "wrong case": self.deploy("--CONFIRMED"),
        }
        for label, res in cases.items():
            with self.subTest(case=label):
                self.assertDenied(res, YOLO_GATE)
        # A confirmation before the comment still counts: the confirmation gates pass, and #53's
        # "no auto-allow with a # comment" rule turns the allow into an ask.
        res = self.deploy("--confirmed # user said yes")
        self.assertEqual(res.get("decision"), "ask", res)
        self.assertNotNewGate(res)

    def test_executor_confirmed_helper(self):
        ok = pre_trade_guard.executor_confirmed
        self.assertTrue(ok(f"{SCRIPT} --symbol X --confirmed"))
        self.assertTrue(ok(f"wsl.exe -d Ubuntu -- {SCRIPT} --symbol X --user-confirmed"))
        self.assertFalse(ok(f"--confirmed {SCRIPT} --symbol X"))
        self.assertFalse(ok(f"CONFIRMED=--confirmed {SCRIPT} --symbol X"))
        self.assertFalse(ok(f"{SCRIPT} --symbol X #--confirmed"))
        self.assertFalse(ok(f"{SCRIPT} --symbol X --confirmed=true"))
        self.assertFalse(ok("python3 other.py --confirmed"))
        self.assertFalse(ok(f"{SCRIPT} --symbol X --conf"))
        # sh/bash -c strings are re-tokenised; arguments after the -c string are $0/$1, not executor flags
        self.assertTrue(ok(f"bash -lc 'cd /mnt/c/repo && {SCRIPT} --symbol X --confirmed'"))
        self.assertTrue(ok(f"wsl.exe -d Ubuntu -- bash -lc 'cd /mnt/c/repo && {SCRIPT} --symbol X --confirmed'"))
        self.assertTrue(ok(f"sh -c \"bash -c '{SCRIPT} --symbol X --confirmed'\""))
        self.assertFalse(ok(f"bash -lc '{SCRIPT} --symbol X' --confirmed"))
        self.assertFalse(ok(f"bash -lc 'cd /mnt/c/repo && {SCRIPT} --symbol X # --confirmed'"))
        self.assertFalse(ok(f"bash -c '{SCRIPT} --symbol X --confirmed; {SCRIPT} --symbol Y'"))

    def test_yolo_candidate_executor_truthiness_denied(self):
        # The executor's _truthy treats '1' / 'yes' as YOLO; the hook must not be laxer.
        for value in ("1", "yes", "TRUE"):
            with self.subTest(value=value):
                self.dossier(is_yolo=value, requires_user_confirmation=False)
                self.assertDenied(self.deploy(), YOLO_GATE)

    def test_is_yolo_flag_on_non_yolo_candidate_denied(self):
        self.dossier(requires_user_confirmation=False)
        self.assertDenied(self.deploy("--is-yolo"), YOLO_GATE)
        self.assertDenied(self.deploy("--is_yolo"), YOLO_GATE)
        self.assertAllowed(self.deploy("--is-yolo --confirmed"))

    def test_abbreviated_is_yolo_flag_denied(self):
        self.dossier(requires_user_confirmation=False)
        for flag in ("--is", "--is-", "--is_", "--is-y", "--is_yol", "--is-yo", "--IS-YOLO"):
            with self.subTest(flag=flag):
                self.assertDenied(self.deploy(flag), YOLO_GATE)
                self.assertAllowed(self.deploy(f"{flag} --confirmed"))

    def test_is_yolo_regex_matches_no_other_executor_option(self):
        options = executor_cli_options()
        is_options = {o for o in options if o.startswith("--is")}
        self.assertEqual(is_options, {"--is-yolo", "--is_yolo"})
        for opt in options - is_options:
            self.assertIsNone(pre_trade_guard.IS_YOLO_FLAG_RE.search(f"{SCRIPT} {opt} x"), opt)
        for text in ("--isolated", "--margin-is", "x--is"):
            self.assertIsNone(pre_trade_guard.IS_YOLO_FLAG_RE.search(text), text)

    def test_yolo_tier_or_strategy_candidate_denied(self):
        for cand in ({"tier": "YOLO"}, {"tier": "Tier S", "strategy": "Conditional YOLO Moonshot"}):
            with self.subTest(cand=cand):
                self.dossier(requires_user_confirmation=False, **cand)
                self.assertDenied(self.deploy(), YOLO_GATE)
                self.assertAllowed(self.deploy("--confirmed"))

    def evaluate(self, args=None, mcp_args=None):
        # The shell paths pass {"CommandLine": ...} / {}; the args / mcp_args keys are the agy/MCP-style inputs
        # of evaluate_trade_opening, so they are exercised directly.
        cmd = f"{SCRIPT} --symbol PEPEUSDT --direction LONG --leverage 3 --env prod"
        return pre_trade_guard.evaluate_trade_opening(cmd, dict({"CommandLine": cmd}, **(args or {})),
                                                      mcp_args or {}, self.root, tgb.PARENT_CONV_ID)

    def test_mcp_style_args_use_executor_truthiness(self):
        self.dossier(requires_user_confirmation=False)
        self.assertEqual(self.evaluate()[0], "allow")
        for key in ("args", "mcp_args"):
            with self.subTest(key=key):
                decision, reason = self.evaluate(**{key: {"is_yolo": "yes"}})
                self.assertEqual(decision, "deny")
                self.assertIn(YOLO_GATE, reason)
                self.assertEqual(self.evaluate(**{key: {"is_yolo": "yes", "confirmed": "1"}})[0], "allow")
        self.dossier(is_yolo=True, requires_user_confirmation=False)
        self.assertEqual(self.evaluate(args={"confirmed": "1"})[0], "allow")
        self.assertEqual(self.evaluate(mcp_args={"user_confirmed": "yes"})[0], "allow")
        decision, reason = self.evaluate(args={"confirmed": "0"})
        self.assertEqual(decision, "deny")
        self.assertIn(YOLO_GATE, reason)

    def test_non_yolo_tier_s_fast_track_unchanged(self):
        self.dossier(symbol="BTCUSDT", tier="Tier S", requires_user_confirmation=False)
        self.assertAllowed(self.deploy(symbol="BTCUSDT"))
        # Missing flag (evaluator omitted it) is not a confirmation requirement either
        self.write_provenance_dossier(symbol="BTCUSDT")
        self.assertAllowed(self.deploy(symbol="BTCUSDT"))


class TestUserConfirmationRequiredProd(YoloGuardHarness):

    def test_requires_user_confirmation_denied_without_confirmed(self):
        self.dossier(symbol="ETHUSDT", tier="Tier A", requires_user_confirmation=True)
        res = self.deploy(symbol="ETHUSDT")
        self.assertDenied(res, CONFIRM_GATE)
        self.assertIn("--confirmed", res["reason"])
        self.assertAllowed(self.deploy("--confirmed", symbol="ETHUSDT"))

    def test_string_flag_also_requires_confirmation(self):
        self.dossier(symbol="ETHUSDT", tier="Tier A+", requires_user_confirmation="true")
        self.assertDenied(self.deploy(symbol="ETHUSDT"), CONFIRM_GATE)

    def test_confirmation_checked_before_yolo(self):
        # Same order as execute_futures_trade.enforce_evaluation_dossier
        self.dossier(tier="Tier A", is_yolo=True, requires_user_confirmation=True)
        self.assertDenied(self.deploy(), CONFIRM_GATE)
        self.assertAllowed(self.deploy("--confirmed"))

    def test_autonomous_disabled_check_unchanged(self):
        with open(os.path.join(self.root, "config", "user_profile.json"), "w", encoding="utf-8") as f:
            json.dump({"profile_completed": True, "yolo_slot_enabled": True, "leverage_standard": 3,
                       "max_open_positions": 5, "autonomous_execution_tier_s": False}, f)
        self.dossier(symbol="BTCUSDT", requires_user_confirmation=False)
        self.assertDenied(self.deploy(symbol="BTCUSDT"), "Autonomous Execution Disabled")
        self.assertAllowed(self.deploy("--confirmed", symbol="BTCUSDT"))
        # Same executor-argument detection as the new gates
        for flags, prefix in (("# --confirmed", ""), ("", "X=--confirmed "), ("--user_confirmed", ""),
                              ("--user-confirmed=false", "")):
            with self.subTest(flags=flags, prefix=prefix):
                self.assertDenied(self.deploy(flags, symbol="BTCUSDT", prefix=prefix), "Autonomous Execution Disabled")
        self.assertAllowed(self.deploy("--user-confirmed", symbol="BTCUSDT"))


class TestUnchangedOutsideProdOpenings(YoloGuardHarness):

    def test_testnet_yolo_without_confirmed_not_denied_by_new_gates(self):
        self.dossier(is_yolo=True, tier="YOLO", requires_user_confirmation=True)
        res = self.deploy("--is-yolo", env="testnet")
        self.assertNotNewGate(res)
        self.assertAllowed(res)

    def test_risk_reducing_commands_on_yolo_symbol_allowed(self):
        self.dossier(is_yolo=True, tier="YOLO", requires_user_confirmation=True)
        for flags in ("--close-position --symbol PEPEUSDT", "--move-breakeven --symbol PEPEUSDT", "--auto-heal",
                      "--audit-orphans", "--protect-pending"):
            with self.subTest(flags=flags):
                res = self.agy(self.cmd(f"{SCRIPT} {flags} --env prod"))
                self.assertAllowed(res)
        self.assertNotEqual(self.agy(self.cmd(f"{SCRIPT} --positions --json --env prod")).get("decision"), "deny")


class TestArgTruthyHelper(unittest.TestCase):

    def test_matches_executor_truthy(self):
        for v in (True, "true", "True", " TRUE ", "1", "yes", "YES", '"yes"'):
            self.assertTrue(pre_trade_guard._arg_truthy(v), v)
        for v in (None, False, "", "0", "no", "false", 0):
            self.assertFalse(pre_trade_guard._arg_truthy(v), v)
        # _is_true keeps its narrower meaning for the other call sites
        self.assertFalse(pre_trade_guard._is_true("1"))


class TestPowerShellYoloParity(t49.PowerShellHarness):

    OPEN = "--symbol BTCUSDT --direction LONG --leverage 3 --env prod"
    ps_forms = t49.TestPowerShellTradingParity.ps_forms
    assertParity = t49.TestPowerShellTradingParity.assertParity

    def test_yolo_candidate_denied_like_bash(self):
        tccs.TestGuardWithClaudeDossier.write_claude_dossier(self, extra={"is_yolo": True,
                                                                         "requires_user_confirmation": False})
        self.assertParity(self.OPEN, "deny")
        self.assertIn(YOLO_GATE, self.ps(self.ps_forms(self.OPEN)[0])["__stderr__"])
        self.assertParity(self.OPEN + " --confirmed", "allow")

    def test_is_yolo_flag_denied_like_bash(self):
        tccs.TestGuardWithClaudeDossier.write_claude_dossier(self, extra={"requires_user_confirmation": False})
        self.assertParity(self.OPEN, "allow")
        self.assertParity(self.OPEN + " --is-y", "deny")
        self.assertParity(self.OPEN + " --is-yolo --confirmed", "allow")

    def test_requires_user_confirmation_denied_like_bash(self):
        tccs.TestGuardWithClaudeDossier.write_claude_dossier(self, extra={"tier": "A",
                                                                         "requires_user_confirmation": True})
        self.assertParity(self.OPEN, "deny")
        self.assertIn(CONFIRM_GATE, self.ps(self.ps_forms(self.OPEN)[0])["__stderr__"])


class TestShellWrappedConfirmation(t49.PowerShellHarness):
    """Round 3: --confirmed inside bash -lc '...' (directly or through wsl.exe) reaches the executor."""

    TRADE = "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --leverage 3 --env prod"

    def setUp(self):
        super().setUp()
        tccs.TestGuardWithClaudeDossier.write_claude_dossier(self, extra={"is_yolo": True,
                                                                         "requires_user_confirmation": False})

    def forms(self, inner: str):
        return [f"bash -lc '{inner}'", f"wsl.exe -d Ubuntu -- bash -lc '{inner}'"]

    def assertBothShells(self, command: str, expected: str, fragment: str = None):
        for label, res in (("bash", self.bash(command)), ("powershell", self.ps(command))):
            with self.subTest(shell=label, command=command):
                self.assertEqual(self.decision(res), expected, res)
                if fragment:
                    self.assertIn(fragment, res["__stderr__"])

    def test_confirmed_inside_bash_c_string_allowed(self):
        direct, via_wsl = self.forms(f"cd /mnt/c/repo && {self.TRADE} --confirmed")
        self.assertBothShells(direct, "allow")
        self.assertEqual(self.decision(self.bash(via_wsl)), "allow")
        # PowerShell judges wsl.exe itself as an unlisted command: normal permission prompt (same as before #63),
        # never a confirmation denial.
        self.assertEqual(self.decision(self.ps(via_wsl)), "passthrough")

    def test_unconfirmed_inside_bash_c_string_denied(self):
        for command in self.forms(f"cd /mnt/c/repo && {self.TRADE}"):
            self.assertBothShells(command, "deny", YOLO_GATE)

    def test_flag_after_the_c_string_is_not_a_confirmation(self):
        for command in (f"bash -lc '{self.TRADE}' --confirmed",
                        f"wsl.exe -d Ubuntu -- bash -lc '{self.TRADE}' --confirmed"):
            self.assertBothShells(command, "deny", YOLO_GATE)

    def test_backslash_line_continuation_before_confirmed(self):
        # The hook's shlex tokenizer does not join backslash-newline continuations: the newline splits the command,
        # so --confirmed on the continuation line is not seen and the order is denied (fails closed).
        for command in (f"{self.TRADE} \\\n  --confirmed", f"bash -lc '{self.TRADE} \\\n  --confirmed'"):
            res = self.bash(command)
            self.assertEqual(self.decision(res), "deny", res)
            self.assertIn(YOLO_GATE, res["__stderr__"])
        self.assertEqual(self.decision(self.bash(f"{self.TRADE} --confirmed")), "allow")


if __name__ == "__main__":
    unittest.main()
