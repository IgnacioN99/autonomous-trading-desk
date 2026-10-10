#!/usr/bin/env python3
"""
test_issue_280_trading_lease.py - Issue #280: one trading writer (lease) for PROD opening orders.

1. utils/trading_lease.py: load / validate, staleness, check_opening, the atomic claim_or_refresh under a raising
   lock, refresh_if_holder (never acquires) and take.
2. PreToolUse hook: session A claims at an allowed PROD opening, session B is denied naming A (Claude Code and agy
   payloads), a denied attempt writes nothing, stale takeover, unknown callers, unreadable / malformed lease and lock
   failure (PROD deny, TESTNET lax), idempotent claim across the wsl / PowerShell re-evaluation, every risk-reducing
   entry point allowed for B while A holds (and with an unreadable lease), the doctor self-test payload, protection of
   the lease file and the module.
3. Takeover: `trading_lease.py --take` is force_ask, committed only by PostToolUse after a successful run from a known
   session; --status is read-only and auto-allowed.
4. PostToolUse refresh by the holder's brief / record commands (never an acquisition).
5. Executor cross-check (check_trading_lease and its wiring in execute_complete_trade).
6. Visibility: the doctor's [LEASE] line and the brief's holder line.

Hermetic: temp workspaces, fake Claude Code / agy transcripts, urlopen blocked, the exchange faked (any signed request
fails the test). No .env credentials are read and nothing is written to the real logs/.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (SCRIPTS_DIR, HOOKS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import post_trade_sync as _post_trade_sync  # noqa: E402
import execute_futures_trade as eft  # noqa: E402
import pre_trade_guard  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import trading_doctor  # noqa: E402
from utils import trading_lease as tl  # noqa: E402
import test_guard_bypasses as tgb  # noqa: E402  (module import: its tests are not collected twice)
import test_issue_101_exchange_anchored_gates as t101  # noqa: E402
import test_issue_172_guardian_trailing as t172  # noqa: E402
from test_issue_270_session_dossiers import (AGENT_A, AGENT_B, SESSION_A, SESSION_B,  # noqa: E402
                                             SessionDossierHarness)

import importlib.util  # noqa: E402

# scripts/post_trade_sync.py is a wrapper around the hook module: whichever one sys.path resolved, test the hook itself
post_trade_sync = getattr(_post_trade_sync, "_mod", _post_trade_sync)

_cli_spec = importlib.util.spec_from_file_location("trading_lease_cli", os.path.join(SCRIPTS_DIR, "trading_lease.py"))
lease_cli = importlib.util.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(lease_cli)

AGY_SESSION = tgb.PARENT_CONV_ID


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


@contextlib.contextmanager
def _never_locked(path, wait_s=None):
    yield False


def _write_lease(base_dir, session, runtime="claude", age=0, now=None, raw=None):
    path = tl.lease_path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = int(time.time() if now is None else now)
    with open(path, "w", encoding="utf-8") as f:
        if raw is not None:
            f.write(raw)
        else:
            json.dump({"session_id": session, "runtime": runtime, "acquired_at": now - age - 10,
                       "heartbeat_at": now - age, "env": "prod"}, f)
    return path


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# =============================================================================
# 1. The lease module
# =============================================================================
class TestLeaseModule(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.now = 1_800_000_000

    def lease(self):
        with open(tl.lease_path(self.root), encoding="utf-8") as f:
            return json.load(f)

    def test_claim_writes_the_record_and_is_idempotent(self):
        self.assertEqual(tl.check_opening(self.root, SESSION_A, self.now)["kind"], "none")
        res = tl.claim_or_refresh(self.root, SESSION_A, "claude", self.now)
        self.assertTrue(res["allow"])
        self.assertEqual(self.lease(), {"session_id": SESSION_A, "runtime": "claude", "acquired_at": self.now,
                                        "heartbeat_at": self.now, "env": "prod"})
        res = tl.claim_or_refresh(self.root, SESSION_A, "claude", self.now + 30)
        self.assertEqual((res["allow"], res["kind"]), (True, "held"))
        self.assertEqual((self.lease()["acquired_at"], self.lease()["heartbeat_at"]), (self.now, self.now + 30))

    def test_another_fresh_holder_is_denied_and_nothing_is_written(self):
        path = _write_lease(self.root, SESSION_A, now=self.now)
        before = _read_bytes(path)
        for fn in (lambda: tl.check_opening(self.root, SESSION_B, self.now),
                   lambda: tl.claim_or_refresh(self.root, SESSION_B, "agy", self.now)):
            res = fn()
            self.assertEqual((res["allow"], res["kind"], res["holder"]["session_id"]), (False, "other", SESSION_A))
        self.assertEqual(_read_bytes(path), before)
        reason = tl.denial_reason(tl.check_opening(self.root, SESSION_B, self.now), self.now)
        self.assertIn("held by session 5e55105e... (claude), last heartbeat 0s ago", reason)
        self.assertIn(tl.TAKE_COMMAND, reason)
        self.assertNotIn("trading_lease.json", reason)

    def test_stale_lease_is_claimed_by_the_next_opener(self):
        _write_lease(self.root, SESSION_A, now=self.now, age=tl.LEASE_STALE_SECONDS + 1)
        res = tl.claim_or_refresh(self.root, SESSION_B, "agy", self.now)
        self.assertEqual((res["allow"], res["kind"]), (True, "stale"))
        self.assertEqual((self.lease()["session_id"], self.lease()["runtime"], self.lease()["acquired_at"]),
                         (SESSION_B, "agy", self.now))
        # Exactly at the threshold it still holds
        _write_lease(self.root, SESSION_A, now=self.now, age=tl.LEASE_STALE_SECONDS)
        self.assertFalse(tl.check_opening(self.root, SESSION_B, self.now)["allow"])

    def test_unknown_caller_never_claims(self):
        res = tl.claim_or_refresh(self.root, None, "claude", self.now)
        self.assertTrue(res["allow"])
        self.assertFalse(os.path.exists(tl.lease_path(self.root)))
        _write_lease(self.root, SESSION_A, now=self.now)
        res = tl.claim_or_refresh(self.root, None, "claude", self.now)
        self.assertEqual((res["allow"], res["kind"]), (False, "unknown_caller"))
        self.assertIn("no session id", tl.denial_reason(res, self.now))

    def test_unreadable_or_malformed_lease_is_an_error(self):
        for raw in ("{bad", "[]", json.dumps({"runtime": "claude", "acquired_at": 1, "heartbeat_at": 1}),
                    json.dumps({"session_id": "x", "acquired_at": 1, "heartbeat_at": True}),
                    json.dumps({"session_id": "  ", "acquired_at": 1, "heartbeat_at": 1})):
            with self.subTest(raw=raw):
                _write_lease(self.root, None, raw=raw)
                for session in (SESSION_A, None):
                    res = tl.check_opening(self.root, session, self.now)
                    self.assertEqual((res["allow"], res["kind"]), (False, "error"))
                res = tl.claim_or_refresh(self.root, SESSION_A, "claude", self.now)
                self.assertEqual((res["allow"], res["kind"]), (False, "error"))
                self.assertEqual(_read_bytes(tl.lease_path(self.root)).decode(), raw)
                with self.assertRaises(tl.LeaseError):
                    tl.refresh_if_holder(self.root, SESSION_A, self.now)
        # runtime / env are informational: a record without them is still a lease
        _write_lease(self.root, None, raw=json.dumps({"session_id": SESSION_A, "acquired_at": 1, "heartbeat_at": 2}))
        self.assertEqual(tl.load(self.root)["session_id"], SESSION_A)

    def test_lock_not_acquired_raises_and_writes_nothing(self):
        with patch("utils.trading_lease.locked", _never_locked):
            res = tl.claim_or_refresh(self.root, SESSION_A, "claude", self.now)
            self.assertEqual((res["allow"], res["kind"]), (False, "error"))
            self.assertIn("lock not acquired", res["error"])
            self.assertFalse(os.path.exists(tl.lease_path(self.root)))
            with self.assertRaises(tl.LeaseError):
                tl.take(self.root, SESSION_A, "claude", self.now)
            _write_lease(self.root, SESSION_A, now=self.now - 50)
            with self.assertRaises(tl.LeaseError):
                tl.refresh_if_holder(self.root, SESSION_A, self.now)
        self.assertEqual(self.lease()["heartbeat_at"], self.now - 50)

    def test_refresh_only_by_the_holder_and_never_acquires(self):
        self.assertFalse(tl.refresh_if_holder(self.root, SESSION_A, self.now))
        self.assertFalse(os.path.exists(tl.lease_path(self.root)))
        _write_lease(self.root, SESSION_A, now=self.now - 100)
        self.assertFalse(tl.refresh_if_holder(self.root, SESSION_B, self.now))
        self.assertEqual(self.lease()["heartbeat_at"], self.now - 100)
        self.assertTrue(tl.refresh_if_holder(self.root, SESSION_A, self.now))
        self.assertEqual((self.lease()["heartbeat_at"], self.lease()["acquired_at"]), (self.now, self.now - 110))

    def test_take_replaces_any_lease(self):
        _write_lease(self.root, None, raw="{bad")
        tl.take(self.root, SESSION_B, "agy", self.now)
        self.assertEqual((self.lease()["session_id"], self.lease()["runtime"]), (SESSION_B, "agy"))
        with self.assertRaises(tl.LeaseError):
            tl.take(self.root, "", "agy", self.now)

    def test_status_line_never_names_the_file(self):
        lines = [tl.status_line(self.root, self.now)]
        _write_lease(self.root, SESSION_A, now=self.now)
        lines.append(tl.status_line(self.root, self.now))
        _write_lease(self.root, SESSION_A, now=self.now, age=tl.LEASE_STALE_SECONDS + 5)
        lines.append(tl.status_line(self.root, self.now))
        _write_lease(self.root, None, raw="{bad")
        lines.append(tl.status_line(self.root, self.now))
        self.assertIn("free", lines[0])
        self.assertIn("held by session 5e55105e... (claude)", lines[1])
        self.assertIn("(active)", lines[1])
        self.assertIn("STALE", lines[2])
        self.assertIn("unreadable", lines[3])
        for line in lines:
            self.assertNotIn("trading_lease.json", line)


# =============================================================================
# 2. PreToolUse hook
# =============================================================================
class LeaseHarness(SessionDossierHarness):

    def lease_path(self):
        return tl.lease_path(self.root)

    def lease(self):
        with open(self.lease_path(), encoding="utf-8") as f:
            return json.load(f)

    def bash(self, session, command):
        payload = {"session_id": session, "hook_event_name": "PreToolUse", "cwd": self.root, "tool_name": "Bash",
                   "tool_input": {"command": command}}
        return self.run_guard(payload)

    def post(self, session, command, tool_response=None, env="prod", **extra):
        payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": command},
                   "tool_response": {"stdout": "", "stderr": ""} if tool_response is None else tool_response}
        if session is not None:
            payload["session_id"] = session
        payload.update(extra)
        # Issue #287: without --env the hook resolves the environment like the script (BINANCE_API_ENV here)
        with patch.dict(os.environ, {"BINANCE_API_ENV": env}), \
                patch.object(post_trade_sync, "find_workspace_root", return_value=self.root), \
                patch.object(post_trade_sync.subprocess, "run") as run:
            res = post_trade_sync.handle_post_trade_sync(payload)
        return res, run

    def opening(self, symbol="BTCUSDT", direction="SHORT", env="prod"):
        return (f"python3 scripts/execute_futures_trade.py --symbol {symbol} --direction {direction} --leverage 3 "
                f"--env {env}")


class TestHookLease(LeaseHarness):

    def test_claude_session_a_claims_and_b_is_denied_naming_a(self):
        self.record_two_sessions()
        self.assertFalse(os.path.exists(self.lease_path()))
        self.assertAllowed(self.deploy(SESSION_A, "BTCUSDT", "SHORT"))
        lease = self.lease()
        self.assertEqual((lease["session_id"], lease["runtime"], lease["env"]), (SESSION_A, "claude", "prod"))
        before = _read_bytes(self.lease_path())
        res = self.deploy(SESSION_B, "ETHUSDT", "LONG")
        self.assertDeniedWith(res, "BLOCKED BY PRE-TOOL-USE HOOK (Trading Lease)")
        self.assertIn("held by session 5e55105e... (claude), last heartbeat", res["__stderr__"])
        self.assertIn("python3 scripts/trading_lease.py --take", res["__stderr__"])
        self.assertEqual(_read_bytes(self.lease_path()), before)  # a denied attempt writes nothing
        self.assertAllowed(self.deploy(SESSION_A, "BTCUSDT", "SHORT"))
        self.assertEqual(self.lease()["acquired_at"], lease["acquired_at"])

    def test_agy_session_is_denied_while_a_claude_session_holds(self):
        self.write_provenance_dossier(symbol="BTCUSDT", direction="LONG", parent=AGY_SESSION)
        _write_lease(self.root, SESSION_A)
        res = self.agy(self.cmd(self.opening(direction="LONG"), conversationId=AGY_SESSION))
        self.assertDenied(res, "trading lease is held by session 5e55105e... (claude)")
        self.assertEqual(self.lease()["session_id"], SESSION_A)
        # Once it is free the agy session claims it, with its runtime
        os.remove(self.lease_path())
        self.assertEqual(self.agy(self.cmd(self.opening(direction="LONG"), conversationId=AGY_SESSION))
                         .get("decision"), "allow")
        self.assertEqual((self.lease()["session_id"], self.lease()["runtime"]), (AGY_SESSION, "agy"))
        # and a Claude session is now denied naming the agy holder
        self.record(SESSION_B, AGENT_B, "ETHUSDT", "LONG", self.now - 5)
        self.assertDeniedWith(self.deploy(SESSION_B, "ETHUSDT", "LONG"), f"session {AGY_SESSION[:8]}... (agy)")

    def test_denied_openings_write_no_lease(self):
        self.record_two_sessions()
        self.assertDeniedWith(self.deploy(SESSION_A, "ETHUSDT", "LONG"), "'ETHUSDT' was NOT approved")
        self.write_session_state(delta_bias="SHORT_HEAVY")
        self.assertDeniedWith(self.deploy(SESSION_A, "BTCUSDT", "SHORT"), "Delta-Neutral Hard Gate")
        self.assertFalse(os.path.exists(self.lease_path()))
        # After those denials the right session still trades (and claims)
        self.write_session_state()
        self.assertAllowed(self.deploy(SESSION_A, "BTCUSDT", "SHORT"))
        self.assertEqual(self.lease()["session_id"], SESSION_A)

    def test_stale_lease_is_taken_over_by_the_next_opener(self):
        self.record_two_sessions()
        _write_lease(self.root, SESSION_A, age=tl.LEASE_STALE_SECONDS + 1)
        self.assertAllowed(self.deploy(SESSION_B, "ETHUSDT", "LONG"))
        self.assertEqual(self.lease()["session_id"], SESSION_B)

    def _direct(self, session):
        cmd = self.opening()
        return pre_trade_guard.evaluate_trade_opening(cmd, {"CommandLine": cmd}, {}, self.root, session)

    def test_unknown_caller_without_lease_is_allowed_and_never_claims(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)
        decision, reason = self._direct(None)
        self.assertEqual(decision, "allow", reason)
        self.assertFalse(os.path.exists(self.lease_path()))

    def test_unknown_caller_with_a_lease_is_denied(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)
        _write_lease(self.root, SESSION_A)
        decision, reason = self._direct(None)
        self.assertEqual(decision, "deny")
        self.assertIn("carries no session id", reason)
        # Any lease file with an unknown caller denies, also a stale one (only a known session may claim it)
        path = _write_lease(self.root, SESSION_A, age=tl.LEASE_STALE_SECONDS + 1)
        before = _read_bytes(path)
        decision, reason = self._direct(None)
        self.assertEqual(decision, "deny")
        self.assertIn("carries no session id", reason)
        self.assertEqual(_read_bytes(path), before)

    def test_unreadable_or_malformed_lease_denies_in_prod_only(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)
        for raw in ("{bad", json.dumps({"session_id": SESSION_A})):
            with self.subTest(raw=raw):
                path = _write_lease(self.root, None, raw=raw)
                res = self.deploy(SESSION_A, "BTCUSDT", "SHORT")
                self.assertDeniedWith(res, "Trading Lease")
                self.assertIn("trading lease", res["__stderr__"])
                self.assertEqual(_read_bytes(path).decode(), raw)
        # TESTNET never reads it
        self.write_legacy_dossier()
        res = self.agy(self.cmd(self.opening(direction="LONG", env="testnet"), conversationId=AGY_SESSION))
        self.assertEqual(res.get("decision"), "allow", res)
        self.assertEqual(_read_bytes(self.lease_path()).decode(), json.dumps({"session_id": SESSION_A}))

    def test_testnet_opening_never_claims(self):
        self.write_legacy_dossier()
        res = self.agy(self.cmd(self.opening(direction="LONG", env="testnet"), conversationId=AGY_SESSION))
        self.assertEqual(res.get("decision"), "allow", res)
        self.assertFalse(os.path.exists(self.lease_path()))

    def test_lock_failure_denies_in_prod(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)
        with patch("utils.trading_lease.locked", _never_locked):
            res = self.deploy(SESSION_A, "BTCUSDT", "SHORT")
        self.assertDeniedWith(res, "lock not acquired")
        self.assertFalse(os.path.exists(self.lease_path()))

    def test_missing_module_or_error_denies_in_prod(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)
        with patch.object(pre_trade_guard, "tl", None):
            self.assertDeniedWith(self.deploy(SESSION_A, "BTCUSDT", "SHORT"), "lease module")
        with patch("utils.trading_lease.check_opening", side_effect=RuntimeError("boom")):
            self.assertDeniedWith(self.deploy(SESSION_A, "BTCUSDT", "SHORT"), "lease check failed (RuntimeError)")
        self.assertFalse(os.path.exists(self.lease_path()))

    def test_claim_is_idempotent_across_wsl_and_powershell_re_evaluation(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)
        _write_lease(self.root, SESSION_A, age=100)
        acquired = self.lease()["acquired_at"]
        calls = []
        real = tl.claim_or_refresh

        def counting(*args, **kwargs):
            calls.append(args[1])
            return real(*args, **kwargs)

        with patch("utils.trading_lease.claim_or_refresh", side_effect=counting):
            res = self.bash(SESSION_A, "wsl.exe -d Ubuntu -- " + self.opening())
            self.assertEqual(res["__exit_code__"], 0, res)
            self.assertEqual(len(calls), 1)  # issue #287: the outer line and the wsl re-parse share one claim
            res = self.run_guard({"session_id": SESSION_A, "hook_event_name": "PreToolUse", "cwd": self.root,
                                  "tool_name": "PowerShell", "tool_input": {"command": self.opening()}})
            self.assertEqual(res["__exit_code__"], 0, res)
        self.assertEqual(set(calls), {SESSION_A})
        self.assertEqual((self.lease()["session_id"], self.lease()["acquired_at"]), (SESSION_A, acquired))
        self.assertGreater(self.lease()["heartbeat_at"], int(time.time()) - 5)
        # Two direct evaluations by the holder keep acquired_at
        self.assertEqual(self._direct(SESSION_A)[0], "allow")
        self.assertEqual(self._direct(SESSION_A)[0], "allow")
        self.assertEqual(self.lease()["acquired_at"], acquired)

    E = "scripts/execute_futures_trade.py"
    RISK_REDUCING = (
        f"python3 {E} --close-position --symbol BTCUSDT --env prod",
        f"python3 {E} --move-breakeven --symbol BTCUSDT --env prod",
        f"python3 {E} --audit-orphans --env prod",
        f"python3 {E} --auto-heal --env prod",
        f"python3 {E} --protect-pending --env prod",
        "python3 scripts/loops/position_guardian_loop.py --once --env prod",
        "python3 scripts/loops/night_cutoff_loop.py --env prod --auto-ratchet",
        "python3 scripts/trading_doctor.py --heal --env prod",
    )

    def _assert_risk_reducing_allowed_for_b(self):
        for command in self.RISK_REDUCING:
            with self.subTest(command=command):
                res = self.bash(SESSION_B, command)
                self.assertAllowed(res)
                res = self.agy(self.cmd(command, conversationId=AGY_SESSION))
                self.assertEqual(res.get("decision"), "allow", res)
        # --positions is read-only (normal policy), never denied by the lease
        res = self.bash(SESSION_B, f"python3 {self.E} --positions --json --env prod")
        self.assertEqual(res["__exit_code__"], 0, res)
        # MCP reduce-only / close / cancel (allow), read-only (ask) and the leverage gate at standard leverage (allow)
        reduce_only = {"toolName": "futures_usds.newOrder",
                       "arguments": {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "reduceOnly": "true"}}
        for payload, decision in (
                (self.mcp("binance", "tool_execute", reduce_only, conversationId=SESSION_B), "allow"),
                (self.mcp("binance", "futures_usds.newAlgoOrder", {"symbol": "BTCUSDT", "closePosition": "true"},
                          conversationId=SESSION_B), "allow"),
                (self.mcp("binance", "futures_usds.cancelOrder", {"symbol": "BTCUSDT", "orderId": 1},
                          conversationId=SESSION_B), "allow"),
                (self.mcp("binance", "futures_usds.positionInformationV2", {"symbol": "BTCUSDT"},
                          conversationId=SESSION_B), "ask"),
                (self.mcp("binance", "futures_usds.changeInitialLeverage", {"symbol": "BTCUSDT", "leverage": 3},
                          conversationId=SESSION_B), "allow")):
            with self.subTest(payload=payload["toolCall"]["args"]["ToolName"]):
                self.assertEqual(self.agy(payload).get("decision"), decision)

    def test_risk_reducing_entry_points_never_need_the_lease(self):
        path = _write_lease(self.root, SESSION_A)
        before = _read_bytes(path)
        self._assert_risk_reducing_allowed_for_b()
        self.assertEqual(_read_bytes(path), before)

    def test_risk_reducing_entry_points_never_read_the_lease(self):
        path = _write_lease(self.root, None, raw="{bad")
        self._assert_risk_reducing_allowed_for_b()
        self.assertEqual(_read_bytes(path), b"{bad")

    def test_doctor_self_test_payload_writes_no_lease(self):
        payload = dict(trading_doctor.SYNTHETIC_NEW_ORDER_PAYLOAD, workspacePaths=[self.root])
        self.assertDenied(self.agy(payload))
        self.assertFalse(os.path.exists(self.lease_path()))


class TestLeaseProtection(LeaseHarness):

    def test_lease_file_is_ground_truth(self):
        self.assertIn("logs/trading_lease.json", pre_trade_guard.GROUND_TRUTH_FILES)
        self.assertIn("post_trade_sync.py", pre_trade_guard.GROUND_TRUTH_FILES["logs/trading_lease.json"])
        for command in ("echo '{}' > logs/trading_lease.json", "rm logs/trading_lease.json",
                        "cp /tmp/x logs/trading_lease.json"):
            with self.subTest(command=command):
                self.assertDeniedWith(self.bash(SESSION_A, command), "Ground Truth Protection")
        res = self.run_guard({"session_id": SESSION_A, "tool_name": "Write", "tool_input": {
            "file_path": os.path.join(self.root, "logs", "trading_lease.json"), "content": "{}"}})
        self.assertEqual(res["__exit_code__"], 2, res)
        # Reading it stays possible
        self.assertEqual(self.bash(SESSION_A, "cat logs/trading_lease.json")["__exit_code__"], 0)

    def test_lease_module_is_a_harness_file(self):
        self.assertIn("scripts/utils/trading_lease.py", pre_trade_guard.HARNESS_FILES)
        res = self.agy(self.cmd("echo x >> scripts/utils/trading_lease.py", conversationId=AGY_SESSION))
        self.assertEqual(res.get("decision"), "force_ask", res)
        res = self.agy({"toolCall": {"name": "write_to_file", "args": {
            "TargetFile": os.path.join(self.root, "scripts", "utils", "trading_lease.py"), "CodeContent": "x"}}})
        self.assertEqual(res.get("decision"), "force_ask", res)

    def test_lease_cli_is_a_harness_file_and_status_stays_allowed(self):
        """Round 2 (audit): an edited CLI could write the lease when its auto-allowed --status runs."""
        self.assertIn("scripts/trading_lease.py", pre_trade_guard.HARNESS_FILES)
        cli = os.path.join(self.root, "scripts", "trading_lease.py")
        for payload in (
                {"toolCall": {"name": "write_to_file", "args": {"TargetFile": cli, "CodeContent": "x"}}},
                {"toolCall": {"name": "replace_file_content", "args": {"TargetFile": cli, "TargetContent": "a",
                                                                       "ReplacementContent": "b"}}}):
            self.assertEqual(self.agy(payload).get("decision"), "force_ask", payload)
        res = self.run_guard({"session_id": SESSION_A, "tool_name": "Edit", "tool_input": {
            "file_path": cli, "old_string": "a", "new_string": "b"}})
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "ask", res)  # force_ask -> explicit ask
        for command in ("echo x >> scripts/trading_lease.py", "sed -i s/a/b/ scripts/trading_lease.py",
                        "cp /tmp/x scripts/trading_lease.py"):
            with self.subTest(command=command):
                self.assertEqual(self.agy(self.cmd(command, conversationId=AGY_SESSION)).get("decision"),
                                 "force_ask")
        self.assertAllowed(self.bash(SESSION_A, "python3 scripts/trading_lease.py --status"))


# =============================================================================
# 3. Takeover and status
# =============================================================================
class TestTakeover(LeaseHarness):

    TAKE = "python3 scripts/trading_lease.py --take"

    def test_take_is_force_ask_and_the_pretooluse_hook_writes_nothing(self):
        path = _write_lease(self.root, SESSION_A)
        before = _read_bytes(path)
        res = self.agy(self.cmd(self.TAKE, conversationId=AGY_SESSION))
        self.assertEqual(res.get("decision"), "force_ask", res)
        self.assertIn("takes over the trading lease", res.get("reason", ""))
        res = self.bash(SESSION_B, self.TAKE)  # Claude Code: force_ask -> an explicit "ask"
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "ask", res)
        res = self.run_guard({"session_id": SESSION_B, "tool_name": "PowerShell",
                              "tool_input": {"command": "python scripts\\trading_lease.py --take"}})
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "ask", res)
        self.assertEqual(_read_bytes(path), before)

    def test_post_tool_use_commits_the_takeover_after_a_successful_run(self):
        _write_lease(self.root, SESSION_A)
        res, run = self.post(SESSION_B, self.TAKE)
        self.assertEqual(res["lease_action"], "take")
        self.assertFalse(res["order_placed"])
        run.assert_not_called()
        self.assertEqual((self.lease()["session_id"], self.lease()["runtime"]), (SESSION_B, "claude"))
        # agy payload: conversationId and runtime agy
        payload = self.cmd(self.TAKE, conversationId=AGY_SESSION)
        with patch.dict(os.environ, {"BINANCE_API_ENV": "prod"}), \
                patch.object(post_trade_sync, "find_workspace_root", return_value=self.root):
            self.assertEqual(post_trade_sync.handle_post_trade_sync(payload)["lease_action"], "take")
        self.assertEqual((self.lease()["session_id"], self.lease()["runtime"]), (AGY_SESSION, "agy"))

    def test_failed_run_unknown_session_or_other_commands_change_nothing(self):
        path = _write_lease(self.root, SESSION_A)
        before = _read_bytes(path)
        cases = [
            (SESSION_B, self.TAKE, {"exit_code": 1}, {}),
            (SESSION_B, self.TAKE, {"interrupted": True}, {}),
            (SESSION_B, self.TAKE, {}, {"hook_event_name": "PostToolUseFailure"}),
            (SESSION_B, self.TAKE, {}, {"error": "Exit code 2"}),
            (None, self.TAKE, None, {}),
            ("", self.TAKE, None, {}),
            (SESSION_B, "python3 scripts/trading_lease.py --take --env testnet", None, {}),
            (SESSION_B, "python3 scripts/trading_lease.py --take --help", None, {}),
            (SESSION_B, "grep -n -- --take scripts/trading_lease.py", None, {}),
            (SESSION_B, "cat scripts/trading_lease.py --take", None, {}),
            (SESSION_B, "python3 scripts/trading_lease.py --status", None, {}),
            (SESSION_B, "python3 scripts/trading_lease.py --tak", None, {}),
            (SESSION_B, "python3 --take scripts/trading_lease.py", None, {}),  # never force-asked: never committed
        ]
        for session, command, response, extra in cases:
            with self.subTest(command=command, response=response, extra=extra, session=session):
                res, _run = self.post(session, command, tool_response=response, **extra)
                self.assertEqual(res["lease_action"], "")
                self.assertEqual(_read_bytes(path), before)

    def test_status_is_read_only_and_auto_allowed(self):
        path = _write_lease(self.root, SESSION_A)
        before = _read_bytes(path)
        for command in ("python3 scripts/trading_lease.py --status", "python3 scripts/trading_lease.py --status --json",
                        "python3 scripts/trading_lease.py --json --env prod"):
            with self.subTest(command=command):
                res = self.bash(SESSION_B, command)
                self.assertAllowed(res)
                self.assertIn("read-only analysis script (scripts/trading_lease.py)", res["hookSpecificOutput"]
                              ["permissionDecisionReason"])
        res = self.run_guard({"session_id": SESSION_B, "tool_name": "PowerShell",
                              "tool_input": {"command": "python scripts\\trading_lease.py --status"}})
        self.assertEqual(res["__exit_code__"], 0, res)
        self.assertNotIn("harness", json.dumps(res).lower())
        self.assertEqual(_read_bytes(path), before)

    def test_cli_status_and_take_write_nothing(self):
        path = _write_lease(self.root, SESSION_A)
        before = _read_bytes(path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lease_cli.main(["--status", "--json", "--env", "prod"], base_dir=self.root), 0)
        status = json.loads(out.getvalue())
        self.assertEqual((status["holder"]["session_id"], status["holder"]["stale"]), (SESSION_A, False))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lease_cli.main(["--env", "prod"], base_dir=self.root), 0)
        self.assertIn("held by session 5e55105e", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lease_cli.main(["--take", "--env", "prod"], base_dir=self.root), 0)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lease_cli.main(["--take", "--env", "testnet"], base_dir=self.root), 2)
            with self.assertRaises(SystemExit):
                lease_cli.main(["--tak", "--env", "prod"], base_dir=self.root)  # no abbreviations
        self.assertEqual(_read_bytes(path), before)
        _write_lease(self.root, None, raw="{bad")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lease_cli.main(["--status"], base_dir=self.root), 1)


# =============================================================================
# 4. PostToolUse refresh by the holder
# =============================================================================
class TestPostToolUseRefresh(LeaseHarness):

    BRIEF = "python3 scripts/prime_evaluator_brief.py --env prod"
    RECORD = "python3 scripts/record_evaluation.py --from-claude-subagent a0123456789abcdef --env prod"

    def test_holder_brief_and_record_refresh_the_heartbeat(self):
        for command in (self.BRIEF, self.RECORD, "python3 scripts/prime_evaluator_brief.py --recheck BTCUSDT:SHORT"):
            with self.subTest(command=command):
                _write_lease(self.root, SESSION_A, age=600)
                old = self.lease()
                res, run = self.post(SESSION_A, command)
                self.assertEqual(res["lease_action"], "refresh")
                run.assert_not_called()
                self.assertEqual(self.lease()["acquired_at"], old["acquired_at"])
                self.assertGreater(self.lease()["heartbeat_at"], old["heartbeat_at"] + 500)

    def test_non_holder_failed_or_inspection_changes_nothing(self):
        path = _write_lease(self.root, SESSION_A, age=600)
        before = _read_bytes(path)
        for session, command, response in ((SESSION_B, self.BRIEF, None), (SESSION_A, self.BRIEF, {"exit_code": 1}),
                                           (SESSION_A, "cat scripts/prime_evaluator_brief.py", None),
                                           (SESSION_A, "python3 scripts/prime_evaluator_brief.py --help", None),
                                           (SESSION_A, "python3 scripts/prime_evaluator_brief.py --env testnet", None),
                                           (SESSION_A, "python3 scripts/broad_market_radar.py --json", None),
                                           (None, self.BRIEF, None)):
            with self.subTest(session=session, command=command):
                res, _run = self.post(session, command, tool_response=response)
                self.assertEqual(res["lease_action"], "")
                self.assertEqual(_read_bytes(path), before)

    def test_brief_or_record_never_acquire(self):
        for command in (self.BRIEF, self.RECORD):
            res, _run = self.post(SESSION_A, command)
            self.assertEqual(res["lease_action"], "")
        self.assertFalse(os.path.exists(self.lease_path()))

    def test_lease_errors_never_break_the_hook(self):
        _write_lease(self.root, None, raw="{bad")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res, _run = self.post(SESSION_A, self.BRIEF)
        self.assertEqual(res["lease_action"], "")
        self.assertIn("LEASE ERROR", err.getvalue())
        self.assertEqual(_read_bytes(self.lease_path()), b"{bad")

    def test_lease_handling_failure_never_skips_the_sync(self):
        os.makedirs(os.path.join(self.root, "scripts"), exist_ok=True)
        open(os.path.join(self.root, "scripts", "sync_session_state.py"), "w").close()
        close = "python3 scripts/execute_futures_trade.py --close-position --symbol BTCUSDT --env prod"
        err = io.StringIO()
        with patch.object(post_trade_sync, "classify_lease_command", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            res, run = self.post(SESSION_A, close)
        self.assertEqual(res["lease_action"], "")
        self.assertTrue(res["order_placed"])
        self.assertTrue(res["sync_attempted"])
        run.assert_called_once()
        self.assertIn("LEASE ERROR", err.getvalue())
        # An odd tool_response shape is not a failure signal and breaks nothing
        _write_lease(self.root, SESSION_A, age=600)
        res, _run = self.post(SESSION_A, self.BRIEF, tool_response="not a dict")
        self.assertEqual(res["lease_action"], "refresh")


# =============================================================================
# 5. Executor cross-check
# =============================================================================
class TestExecutorLeaseCheck(LeaseHarness):

    def check(self, symbol, direction, cand, env="prod", **kw):
        return eft.check_trading_lease(symbol, direction, env, cand, base_dir=self.root, **kw)

    def approving(self, symbol, direction):
        ok, reason, cand = eft.enforce_evaluation_dossier(symbol, direction, "prod", base_dir=self.root)
        self.assertTrue(ok, reason)
        return cand

    def test_no_lease_or_stale_lease_allows(self):
        self.record_two_sessions()
        cand = self.approving("ETHUSDT", "LONG")
        self.assertEqual(self.check("ETHUSDT", "LONG", cand), (True, "Trading lease free or stale.", cand))
        self.assertFalse(os.path.exists(self.lease_path()))
        _write_lease(self.root, SESSION_A, age=tl.LEASE_STALE_SECONDS + 1)
        self.assertTrue(self.check("ETHUSDT", "LONG", cand)[0])

    def test_holders_dossier_passes(self):
        self.record_two_sessions()
        _write_lease(self.root, SESSION_A)
        cand = self.approving("BTCUSDT", "SHORT")
        self.assertEqual(cand["dossier_session"], SESSION_A)
        ok, _reason, got = self.check("BTCUSDT", "SHORT", cand)
        self.assertTrue(ok)
        self.assertIs(got, cand)

    def test_another_sessions_dossier_is_denied_naming_the_holder(self):
        self.record_two_sessions()
        _write_lease(self.root, SESSION_A)
        cand = self.approving("ETHUSDT", "LONG")
        self.assertEqual(cand["dossier_session"], SESSION_B)
        ok, reason, got = self.check("ETHUSDT", "LONG", cand)
        self.assertFalse(ok)
        self.assertIsNone(got)
        self.assertIn("Trading Lease, PROD", reason)
        self.assertIn("held by session 5e55105e... (claude)", reason)
        # A candidate without a session (no dossier record) is denied too
        self.assertFalse(self.check("ETHUSDT", "LONG", None)[0])

    def test_newest_record_of_another_session_does_not_deny_the_holder(self):
        a = self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 60)
        self.record(SESSION_B, AGENT_B, "BTCUSDT", "SHORT", self.now - 5, extra={"entry": 1.0})
        _write_lease(self.root, SESSION_A)
        cand = self.approving("BTCUSDT", "SHORT")
        self.assertEqual(cand["dossier_session"], SESSION_B)  # the newest approving record overall
        ok, reason, got = self.check("BTCUSDT", "SHORT", cand)
        self.assertTrue(ok, reason)
        self.assertEqual((got["dossier_session"], got["dossier_sha256"]), (SESSION_A, a["provenance"]["sha256"]))

    def test_holders_own_candidate_keeps_its_confirmation_rule(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 60, extra={"requires_user_confirmation": True})
        self.record(SESSION_B, AGENT_B, "BTCUSDT", "SHORT", self.now - 5, extra={"entry": 1.0})
        _write_lease(self.root, SESSION_A)
        cand = self.approving("BTCUSDT", "SHORT")
        ok, reason, _ = self.check("BTCUSDT", "SHORT", cand)
        self.assertFalse(ok)
        self.assertIn("pending explicit user confirmation", reason)
        self.assertTrue(self.check("BTCUSDT", "SHORT", cand, confirmed=True)[0])

    def test_unreadable_lease_denies_in_prod_and_testnet_is_untouched(self):
        self.record_two_sessions()
        cand = self.approving("BTCUSDT", "SHORT")
        _write_lease(self.root, None, raw="{bad")
        ok, reason, _ = self.check("BTCUSDT", "SHORT", cand)
        self.assertFalse(ok)
        self.assertIn("FAIL-CLOSED", reason)
        self.assertEqual(self.check("BTCUSDT", "SHORT", cand, env="testnet"),
                         (True, "TESTNET: the trading lease is not applied.", cand))

    def test_execute_complete_trade_rejects_before_any_exchange_call(self):
        ws = t101.Workspace("setUp")
        ws.setUp()
        self.addCleanup(ws.doCleanups)
        _write_lease(ws.ws, SESSION_A)
        fake = MagicMock(side_effect=AssertionError("exchange must not be called"))
        res = ws.execute(fake)  # enforce_evaluation_dossier faked: a candidate without a dossier session
        self.assertFalse(res["success"])
        self.assertIn("Trading Lease, PROD", res["error"])
        self.assertTrue(res["evaluation_gate_rejection"])
        fake.assert_not_called()
        # A stale lease holds nothing: the order goes through (the executor never claims)
        path = _write_lease(ws.ws, SESSION_A, age=tl.LEASE_STALE_SECONDS + 1)
        before = _read_bytes(path)
        ws.write_state([])
        t101.write_registry(ws.ws)
        live = t101.LiveExchange()
        res = ws.execute(live)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(_read_bytes(path), before)
        # TESTNET (bypass-eval-gate): the lease is never read
        _write_lease(ws.ws, None, raw="{bad")
        with patch("execute_futures_trade.check_trading_lease") as check:
            res = ws.execute(t101.LiveExchange(), env="testnet")
        self.assertTrue(res["success"], res.get("error"))
        check.assert_not_called()

    def test_risk_reducing_paths_never_call_it(self):
        import inspect
        for name in ("close_position_market", "move_sl_to_breakeven", "audit_orphan_positions",
                     "protect_pending_entries", "heal_orphan_position", "get_positions_report",
                     "enforce_evaluation_dossier"):
            self.assertNotIn("check_trading_lease", inspect.getsource(getattr(eft, name)), name)
        self.assertIn("check_trading_lease(", inspect.getsource(eft._execute_complete_trade_pass))


# =============================================================================
# 6. Visibility
# =============================================================================
class TestVisibility(LeaseHarness):

    def test_doctor_prints_the_lease_line_and_never_fails_on_it(self):
        doctor = t172.TestDoctorGuardianException("setUp")  # its offline run_doctor harness (exchange faked)
        doctor.setUp()
        self.addCleanup(doctor.doCleanups)
        logs = os.path.join(self.root, "logs")
        _write_lease(self.root, SESSION_A)
        code, out = doctor._run_doctor(**{"sync_session_state.LOGS_DIR": logs})
        lines = [line for line in out.splitlines() if "[LEASE]" in line]
        self.assertEqual(len(lines), 1, out)
        self.assertIn("ℹ️  [LEASE] Trading lease held by session 5e55105e... (claude)", lines[0])
        self.assertNotIn("trading_lease.json", lines[0])
        # Informational only: an unreadable lease neither fails the doctor nor adds a warning
        _write_lease(self.root, None, raw="{bad")
        code2, out2 = doctor._run_doctor(**{"sync_session_state.LOGS_DIR": logs})
        self.assertEqual(code2, code)
        self.assertIn("ℹ️  [LEASE] Trading lease unreadable", out2)
        self.assertNotIn("❌ [LEASE]", out2)
        self.assertNotIn("⚠️  [LEASE]", out2)

    def test_brief_prints_the_holder_outside_the_json_brief(self):
        _write_lease(self.root, SESSION_A)
        brief = {"env": "prod"}
        with patch.object(peb, "BASE_DIR", self.root), \
                patch.object(peb, "assemble_primed_brief", return_value=brief), \
                patch.object(peb, "format_markdown_brief", return_value="BRIEF"):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(peb.main(["--env", "prod"]), 0)
            self.assertIn("Trading lease held by session 5e55105e", out.getvalue())
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(peb.main(["--env", "prod", "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue()), brief)  # stdout stays the JSON brief
            self.assertIn("Trading lease held by session", err.getvalue())
        self.assertNotIn("lease", json.dumps(brief))


if __name__ == "__main__":
    unittest.main()
