#!/usr/bin/env python3
"""
test_issue_287_lease_followups.py - Issue #287: trading lease residuals after #280.

1. A heartbeat more than LEASE_FUTURE_SKEW_SECONDS in the future is stale (claimed by the next opener); the
   LEASE_STALE_SECONDS boundary is unchanged.
2. post_trade_sync.classify_lease_command: only the lease sub-command's own --env (or BINANCE_API_ENV assignment)
   counts; a takeover is committed only on a known exit 0 or, without an exit code, when the environment resolves to
   PROD like the script resolves it; a brief / record resolving TESTNET never refreshes the holder.
3. File-tool writes to the lease's .lock sidecar are denied like the lease file; other logs/ files are unaffected.
4. One claim (one lock) per hook evaluation across the outer line, the wsl re-parse and PowerShell; a denial is never
   memoised and writes nothing.
5. triage_pr.py requires the lease reviewers for the lease files; the executor test no longer reads the real lease.
6. Planner SKILL.md step 6 lease sub-bullets (both copies) and the trading.md scope notes.

Hermetic: temp workspaces (GuardHarness / SessionDossierHarness), urlopen blocked, no exchange call, no .env read and
nothing written to the real logs/.
"""

import contextlib
import io
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (BASE_DIR, SCRIPTS_DIR, HOOKS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execute_futures_trade as eft  # noqa: E402
import pre_trade_guard  # noqa: E402
from utils import trading_lease as tl  # noqa: E402
import test_issue_280_trading_lease as t280  # noqa: E402  (module import: its tests are not collected twice)
from test_issue_270_session_dossiers import AGENT_A, SESSION_A, SESSION_B  # noqa: E402

post_trade_sync = t280.post_trade_sync
_write_lease = t280._write_lease
_read_bytes = t280._read_bytes
SKEW = tl.LEASE_FUTURE_SKEW_SECONDS


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


# =============================================================================
# 1. Future-dated heartbeat
# =============================================================================
class TestFutureDatedHeartbeat(t280.LeaseHarness):

    def test_skew_boundary(self):
        now = 1_800_000_000
        self.assertEqual(SKEW, 300)
        within = {"session_id": SESSION_A, "acquired_at": now, "heartbeat_at": now + SKEW}
        beyond = dict(within, heartbeat_at=now + SKEW + 1)
        self.assertFalse(tl.is_stale(within, now))
        self.assertTrue(tl.is_stale(beyond, now))
        # The 1800 s rule keeps its strict boundary
        self.assertFalse(tl.is_stale(dict(within, heartbeat_at=now - tl.LEASE_STALE_SECONDS), now))
        self.assertTrue(tl.is_stale(dict(within, heartbeat_at=now - tl.LEASE_STALE_SECONDS - 1), now))

    def test_within_skew_still_holds(self):
        now = 1_800_000_000
        path = _write_lease(self.root, SESSION_A, now=now, age=-SKEW)
        before = _read_bytes(path)
        res = tl.claim_or_refresh(self.root, SESSION_B, "claude", now)
        self.assertEqual((res["allow"], res["kind"]), (False, "other"))
        self.assertEqual(_read_bytes(path), before)
        self.assertIn("(active)", tl.status_line(self.root, now))

    def test_beyond_skew_is_claimed_by_the_next_opener(self):
        now = 1_800_000_000
        _write_lease(self.root, SESSION_A, now=now, age=-(SKEW + 1))
        self.assertIn("STALE", tl.status_line(self.root, now))
        res = tl.claim_or_refresh(self.root, SESSION_B, "agy", now)
        self.assertEqual((res["allow"], res["kind"]), (True, "stale"))
        self.assertEqual((self.lease()["session_id"], self.lease()["heartbeat_at"]), (SESSION_B, now))

    def test_hook_and_executor_treat_it_as_stale(self):
        self.record_two_sessions()
        _write_lease(self.root, SESSION_A, age=-(SKEW + 60))
        self.assertTrue(eft.check_trading_lease("ETHUSDT", "LONG", "prod", None, base_dir=self.root)[0])
        self.assertAllowed(self.deploy(SESSION_B, "ETHUSDT", "LONG"))
        self.assertEqual(self.lease()["session_id"], SESSION_B)
        self.assertLessEqual(self.lease()["heartbeat_at"], int(time.time()))


# =============================================================================
# 2. Takeover commit and refresh environment
# =============================================================================
class TestClassifyLeaseCommand(unittest.TestCase):

    TAKE = "python3 scripts/trading_lease.py --take"
    BRIEF = "python3 scripts/prime_evaluator_brief.py"

    def test_env_mentioned_elsewhere_does_not_suppress(self):
        for command, action in (
                (f"{self.TAKE} && echo --env testnet", "take"),
                (f"grep -n -- '--env testnet' notes.md; {self.TAKE}", "take"),
                (f"python3 scripts/broad_market_radar.py --env testnet --json; {self.TAKE}", "take"),
                (f"{self.BRIEF} --env prod; echo '--env testnet'", "refresh"),
                (f"python3 scripts/broad_market_radar.py --env=testnet && {self.BRIEF}", "refresh"),
                # An inline prefix only scopes to its own command
                (f"BINANCE_API_ENV=testnet python3 scripts/broad_market_radar.py --json; {self.TAKE}", "take"),
                (f"grep BINANCE_API_ENV=testnet notes.md && {self.BRIEF}", "refresh")):
            with self.subTest(command=command):
                self.assertEqual(post_trade_sync.classify_lease_command(command), action)

    def test_own_non_prod_env_suppresses(self):
        for command in (f"{self.TAKE} --env testnet", f"{self.TAKE} --env=testnet", f"{self.TAKE} --env 'testnet'",
                        f"{self.TAKE} --env prod --env testnet", f"{self.TAKE} --env", f"{self.TAKE} --env bogus",
                        f"BINANCE_API_ENV=testnet {self.TAKE}", f"export BINANCE_API_ENV=testnet && {self.TAKE}",
                        f"BINANCE_API_ENV=testnet; {self.TAKE}",
                        f"{self.BRIEF} --env testnet", f"{self.BRIEF} --en testnet",
                        f"env BINANCE_API_ENV=testnet {self.BRIEF}",
                        "python3 scripts/record_evaluation.py --from-claude-subagent a1 --env=testnet"):
            with self.subTest(command=command):
                self.assertEqual(post_trade_sync.classify_lease_command(command), "")

    def test_own_prod_env_and_reported_env(self):
        self.assertEqual(post_trade_sync.classify_lease_command(f"{self.TAKE} --env prod"), "take")
        self.assertEqual(post_trade_sync.classify_lease_command(f"{self.TAKE} --env testnet --env prod"), "take")
        self.assertEqual(post_trade_sync.lease_command(f"{self.TAKE} --env=mainnet"), ("take", "mainnet"))
        self.assertEqual(post_trade_sync.lease_command(f"BINANCE_API_ENV=prod {self.TAKE}"), ("take", "prod"))
        self.assertEqual(post_trade_sync.lease_command(f"{self.BRIEF} --recheck BTCUSDT:SHORT"), ("refresh", None))
        self.assertEqual(post_trade_sync.lease_command("echo --env testnet"), ("", None))


class TestTakeoverCommit(t280.LeaseHarness):

    TAKE = "python3 scripts/trading_lease.py --take"

    def setUp(self):
        super().setUp()
        self.path = _write_lease(self.root, SESSION_A)
        self.before = _read_bytes(self.path)

    def assertNotCommitted(self, res):
        self.assertEqual(res["lease_action"], "")
        self.assertEqual(_read_bytes(self.path), self.before)

    def assertCommitted(self, res):
        self.assertEqual(res["lease_action"], "take")
        self.assertEqual(self.lease()["session_id"], SESSION_B)

    def test_non_zero_exit_code_in_the_payload_never_commits(self):
        for response in ({"exit_code": 2}, {"exitCode": 2}, {"returncode": 1}, {"exit_code": 0, "returncode": 2}):
            with self.subTest(response=response):
                self.assertNotCommitted(self.post(SESSION_B, self.TAKE, tool_response=response)[0])

    def test_unknown_exit_code_never_commits(self):
        for response in ({"exit_code": "0"}, {"exitCode": None}, {"returncode": False}, {"exit_code": 0.0}):
            with self.subTest(response=response):
                self.assertNotCommitted(self.post(SESSION_B, self.TAKE, tool_response=response)[0])

    def test_non_prod_env_resolved_from_binance_api_env_never_commits(self):
        # The script resolves testnet from BINANCE_API_ENV and exits 2; the payload carries no exit code
        self.assertNotCommitted(self.post(SESSION_B, self.TAKE, env="testnet")[0])
        self.assertNotCommitted(self.post(SESSION_B, f"BINANCE_API_ENV=testnet {self.TAKE}")[0])
        with contextlib.redirect_stderr(io.StringIO()):  # an invalid environment is a lease error: nothing written
            self.assertNotCommitted(self.post(SESSION_B, self.TAKE, env="bogus")[0])

    def test_env_mentioned_elsewhere_still_commits(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            res, _run = self.post(SESSION_B, f"{self.TAKE} && echo --env testnet")
        self.assertCommitted(res)

    def test_prod_success_commits(self):
        for response, env, command in (({"exit_code": 0}, "prod", self.TAKE),
                                       ({"stdout": "", "stderr": ""}, "prod", self.TAKE),
                                       (None, "testnet", f"{self.TAKE} --env prod")):  # its own --env wins
            with self.subTest(response=response, env=env, command=command):
                _write_lease(self.root, SESSION_A)
                with contextlib.redirect_stderr(io.StringIO()):
                    res, _run = self.post(SESSION_B, command, tool_response=response, env=env)
                self.assertCommitted(res)


class TestRefreshEnvironment(t280.LeaseHarness):

    BRIEF = "python3 scripts/prime_evaluator_brief.py"
    RECORD = "python3 scripts/record_evaluation.py --from-claude-subagent a0123456789abcdef"

    def test_brief_or_record_resolving_testnet_does_not_refresh_the_holder(self):
        path = _write_lease(self.root, SESSION_A, age=600)
        before = _read_bytes(path)
        for command, env in ((self.BRIEF, "testnet"), (self.RECORD, "testnet"),
                             (f"{self.BRIEF} --recheck BTCUSDT:SHORT", "testnet"),
                             (f"BINANCE_API_ENV=testnet {self.RECORD}", "prod")):
            with self.subTest(command=command, env=env):
                res, _run = self.post(SESSION_A, command, env=env)
                self.assertEqual(res["lease_action"], "")
                self.assertEqual(_read_bytes(path), before)

    def test_brief_or_record_resolving_prod_refreshes_the_holder(self):
        for command, env in ((self.BRIEF, "prod"), (self.RECORD, "prod"), (f"{self.BRIEF} --env prod", "testnet")):
            with self.subTest(command=command, env=env):
                _write_lease(self.root, SESSION_A, age=600)
                old = self.lease()["heartbeat_at"]
                res, _run = self.post(SESSION_A, command, env=env)
                self.assertEqual(res["lease_action"], "refresh")
                self.assertGreater(self.lease()["heartbeat_at"], old + 500)


# =============================================================================
# 3. The lease's .lock sidecar
# =============================================================================
class TestLockSidecarProtection(t280.LeaseHarness):

    def write(self, path, tool="Write"):
        tool_input = ({"file_path": path, "content": "x"} if tool == "Write"
                      else {"file_path": path, "old_string": "a", "new_string": "b"})
        return self.run_guard({"session_id": SESSION_A, "tool_name": tool, "tool_input": tool_input})

    def test_file_tool_write_to_the_sidecar_is_denied(self):
        logs = os.path.join(self.root, "logs")
        for path, tool in ((os.path.join(logs, "trading_lease.json.lock"), "Write"),
                           (os.path.join(logs, "trading_lease.json.lock"), "Edit"),
                           ("logs/trading_lease.json.lock", "Write"),
                           ("C:\\repo\\logs\\TRADING_LEASE.json.lock", "Write"),
                           (os.path.join(logs, "trading_lease.json"), "Write")):
            with self.subTest(path=path, tool=tool):
                res = self.write(path, tool)
                self.assertEqual(res["__exit_code__"], 2, res)
                self.assertIn("Ground Truth Protection", res["__stderr__"])
                self.assertIn("logs/trading_lease.json may only be written by", res["__stderr__"])
        res = self.agy({"toolCall": {"name": "write_to_file", "args": {
            "TargetFile": os.path.join(logs, "trading_lease.json.lock"), "CodeContent": ""}}})
        self.assertEqual(res.get("decision"), "deny", res)

    def test_sidecar_through_a_symlinked_directory_is_denied(self):
        link = os.path.join(self.root, "st")
        os.symlink(os.path.join(self.root, "logs"), link)
        res = self.write(os.path.join(link, "trading_lease.json.lock"))
        self.assertEqual(res["__exit_code__"], 2, res)

    def test_ordinary_logs_files_are_unaffected(self):
        logs = os.path.join(self.root, "logs")
        for name in ("notes.json", "yolo_scan_health.json.lock", "trading_lease.json.lock.bak"):
            with self.subTest(name=name):
                self.assertEqual(pre_trade_guard.evaluate_file_write(os.path.join(logs, name), "x", self.root)[0],
                                 "ask")
        # The shell side already denied the sidecar (substring match) and still does
        self.assertDeniedWith(self.bash(SESSION_A, "rm logs/trading_lease.json.lock"), "Ground Truth Protection")


# =============================================================================
# 4. One claim per hook evaluation
# =============================================================================
class TestClaimMemoization(t280.LeaseHarness):

    def counting_lock(self):
        acquired = []
        real = tl.locked

        @contextlib.contextmanager
        def counting(path, wait_s=None):
            acquired.append(path)
            with real(path, wait_s=wait_s) as held:
                yield held

        return acquired, patch("utils.trading_lease.locked", counting)

    def test_wsl_and_powershell_evaluations_take_the_lock_once(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)
        acquired, lock_patch = self.counting_lock()
        with lock_patch:
            self.assertAllowed(self.bash(SESSION_A, "wsl.exe -d Ubuntu -- " + self.opening()))
            self.assertEqual(len(acquired), 1)
            lease = self.lease()
            self.assertEqual((lease["session_id"], lease["runtime"]), (SESSION_A, "claude"))
            res = self.run_guard({"session_id": SESSION_A, "hook_event_name": "PreToolUse", "cwd": self.root,
                                  "tool_name": "PowerShell", "tool_input": {"command": self.opening()}})
            self.assertEqual(res["__exit_code__"], 0, res)
            self.assertEqual(len(acquired), 2)  # a new hook evaluation claims again (refresh)
        self.assertEqual(self.lease()["acquired_at"], lease["acquired_at"])

    def test_a_denied_attempt_writes_nothing_and_is_not_memoised(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)
        path = _write_lease(self.root, SESSION_B)
        before = _read_bytes(path)
        self.assertDeniedWith(self.bash(SESSION_A, "wsl.exe -d Ubuntu -- " + self.opening()), "Trading Lease")
        self.assertEqual(_read_bytes(path), before)
        os.remove(path)
        cmd = self.opening()
        with pre_trade_guard._audit_scope():
            with patch("utils.trading_lease.locked", t280._never_locked):
                decision, reason = pre_trade_guard.evaluate_trade_opening(cmd, {"CommandLine": cmd}, {}, self.root,
                                                                         SESSION_A)
                self.assertEqual(decision, "deny")
                self.assertIn("lock not acquired", reason)
            self.assertFalse(os.path.exists(path))
            # Same scope, lock available: the earlier denial was not memoised, the claim really happens
            decision, reason = pre_trade_guard.evaluate_trade_opening(cmd, {"CommandLine": cmd}, {}, self.root,
                                                                     SESSION_A)
            self.assertEqual(decision, "allow", reason)
        self.assertEqual(self.lease()["session_id"], SESSION_A)

    def test_memo_is_per_scope_and_per_session(self):
        acquired, lock_patch = self.counting_lock()
        with lock_patch:
            for _ in range(2):
                self.assertIsNone(pre_trade_guard._trading_lease_denial(self.root, SESSION_A, "claude", self.now, True))
            self.assertEqual(len(acquired), 2)  # outside an audit scope nothing is memoised
            with pre_trade_guard._audit_scope():
                for _ in range(3):
                    self.assertIsNone(pre_trade_guard._trading_lease_denial(self.root, SESSION_A, "claude", self.now,
                                                                            True))
                self.assertEqual(len(acquired), 3)
                # Another session in the same scope is judged on its own (A holds a fresh lease: denied)
                self.assertIsNotNone(pre_trade_guard._trading_lease_denial(self.root, SESSION_B, "claude", self.now,
                                                                           True))
                self.assertEqual(len(acquired), 4)
            with pre_trade_guard._audit_scope():
                self.assertIsNone(pre_trade_guard._trading_lease_denial(self.root, SESSION_A, "claude", self.now, True))
            self.assertEqual(len(acquired), 5)
        self.assertEqual(self.lease()["session_id"], SESSION_A)


# =============================================================================
# 5. Triage patterns and the executor test's workspace
# =============================================================================
class TestTriageAndExecutorWorkspace(unittest.TestCase):

    def test_triage_requires_the_lease_reviewers(self):
        from scripts.ci.triage_pr import triage
        for path in ("scripts/utils/trading_lease.py", "scripts/trading_lease.py"):
            with self.subTest(path=path):
                manifest = triage([path])
                self.assertFalse(manifest["fail_closed_triggered"])
                self.assertEqual(manifest["required_reviewers"], ["agentic_harness", "trading_risk"])

    def test_prod_executor_dossier_test_never_reads_the_real_lease(self):
        import test_executor_gates
        seen = []
        real = tl.load

        def recording(base_dir):
            seen.append(os.path.realpath(base_dir))
            return real(base_dir)

        case = test_executor_gates.TestExecutorDossierIntegration("test_prod_valid_dossier_passes_gate")
        result = unittest.TestResult()
        with patch("utils.trading_lease.load", side_effect=recording):
            case.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertTrue(seen)  # the PROD lease cross-check ran
        self.assertNotIn(os.path.realpath(BASE_DIR), seen)


# =============================================================================
# 6. Docs
# =============================================================================
class TestDocs(unittest.TestCase):

    def read(self, *parts):
        with open(os.path.join(BASE_DIR, *parts), encoding="utf-8") as f:
            return f.read()

    def test_planner_lease_sub_bullets_in_both_copies(self):
        sections = []
        for root in (".agents", ".claude"):
            with self.subTest(root=root):
                text = self.read(root, "skills", "trade-execution-planner", "SKILL.md")
                self.assertIn("6. **Execute only through the gated engine:**", text)
                section = text.split("- **Trading lease (PROD, one session at a time):**", 1)[1].split(
                    "7. Field-by-field", 1)[0]
                for fragment in ("\n     - Denial: ", "\n     - Takeover: ", "\n     - Stale: ",
                                 "\n     - Unreadable lease or new session id: ", "always after `/clear`",
                                 "a default `--resume` / `--continue` keeps it", "`--fork-session` gives a new one",
                                 "python3 scripts/trading_lease.py --take", "python3 scripts/trading_lease.py --status"):
                    self.assertIn(fragment, section)
                self.assertNotIn("a resumed conversation the session id is new", section)
                sections.append(section)
        self.assertEqual(sections[0], sections[1])

    def test_trading_rules_state_the_scope_notes(self):
        text = self.read(".agents", "rules", "trading.md")
        line = next(ln for ln in text.splitlines() if ln.startswith("- Trading lease:"))
        for fragment in ("per workspace", "A caller without a session id is allowed while no lease exists and never "
                         "claims", "the executor never claims"):
            self.assertIn(fragment, line)


if __name__ == "__main__":
    unittest.main()
