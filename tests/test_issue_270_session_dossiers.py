#!/usr/bin/env python3
"""
test_issue_270_session_dossiers.py - Issue #270: two trading sessions on one account.

1. Session-scoped dossiers: record_evaluation.py writes logs/evaluations/dossier_<session>.json plus
   latest_dossier.json (the newest scan overall); the PROD hook reads the calling session's own file, the executor the
   newest verified record approving the order (dossier_provenance.find_approving_dossier), and every sha-bound reader
   (radar snapshot, squeeze fallback, Tier S calibration, YOLO detection, --recheck plan) reads the file holding the
   validated sha256.
2. Origin of positions and resting entries: dossier_session in the score metadata, the `origin` tag of
   sync_session_state and of the brief (only when known).
3. Reporter dedupe across sessions: the open-issue lookup by fingerprint (gh stubbed), fail-open on any lookup
   failure, a locked fingerprint file, and report_issue.sh routed through the same helper.

Hermetic: temp workspaces, fake Claude Code transcripts (CLAUDE_PROJECTS_DIRS), urlopen blocked, the exchange faked,
gh stubbed (subprocess.run patched or a stub on PATH). No .env credentials are read.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (SCRIPTS_DIR, HOOKS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execute_futures_trade as eft  # noqa: E402
import post_trade_sync  # noqa: E402
import pre_trade_guard  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import record_evaluation as rec  # noqa: E402
import report_agent_issue  # noqa: E402
import sync_session_state as sss  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
from utils import recheck_brief as rb  # noqa: E402
from utils import score_calibration as scal  # noqa: E402
from utils import trading_lease  # noqa: E402
import test_guard_bypasses as tgb  # noqa: E402  (module import: its tests are not collected twice)
import test_issue_101_exchange_anchored_gates as t101  # noqa: E402
import test_pending_entries as tpe  # noqa: E402
from test_claude_code_support import write_claude_transcript  # noqa: E402
from test_report_agent_issue_priority import _BacklogCase, fake_response  # noqa: E402
from test_report_issue import CURL_STUB, GH_STUB, SCRIPT as REPORT_ISSUE_SH, session_state  # noqa: E402

SESSION_A = "5e55105e-0000-4000-8000-00000000000a"
SESSION_B = "5e55105e-0000-4000-8000-00000000000b"
SESSION_C = "5e55105e-0000-4000-8000-00000000000c"
AGENT_A = "aaaaaaaaaaaaaaaa1"
AGENT_B = "abbbbbbbbbbbbbbb2"


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


# =============================================================================
# 1. Session-scoped dossiers (recorder, hook, executor, sha-bound readers)
# =============================================================================
class SessionDossierHarness(tgb.GuardHarness):
    """GuardHarness workspace (profile, fresh session state, calibrated Tier S store) plus fake Claude Code
    transcripts; dossiers are recorded through record_evaluation.record_from_claude_subagent."""

    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.root, "claude_projects")
        os.environ[dp.CLAUDE_PROJECTS_ENV] = self.projects  # restored by GuardHarness' patch.dict
        self.now = int(time.time())

    def record(self, session, agent_id, symbol, direction, ts, extra=None):
        """The evaluator of `session` approves symbol + direction (Tier S, score 85, radar row joined)."""
        cand = dict({"symbol": symbol, "direction": direction, "tier": "S", "leverage": 3, "score": 85,
                     "requires_user_confirmation": False}, **(extra or {}))
        payload = {"status": "APPROVED", "target_env": "PROD", "summary": "t", "approved_candidates": [cand]}
        text = f"Master Dossier\n{tgb.checklist_for(payload)}<dossier_json>\n{json.dumps(payload)}\n</dossier_json>"
        write_claude_transcript(Path(self.projects), agent_id, text, dp.EVALUATOR_NAME, session=session, ts=ts)
        with open(os.path.join(self.root, "logs", "primed_brief_scores.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_at_ts": ts - 10, "env": "prod",
                       "rows": [{"symbol": symbol, "direction": direction, "confidence": 85}]}, f)
        with contextlib.redirect_stdout(io.StringIO()):
            return rec.record_from_claude_subagent(agent_id, target_env="prod", base_dir=self.root, shadow=False)

    def record_two_sessions(self):
        """Session A approves BTCUSDT SHORT; session B scans later and approves ETHUSDT LONG (latest = B)."""
        a = self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 60)
        b = self.record(SESSION_B, AGENT_B, "ETHUSDT", "LONG", self.now - 5)
        return a, b

    def session_file(self, session):
        return dp.session_dossier_path(self.root, session)

    def load(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def deploy(self, session, symbol, direction):
        payload = {"session_id": session, "hook_event_name": "PreToolUse", "cwd": self.root, "tool_name": "Bash",
                   "tool_input": {"command": f"python3 scripts/execute_futures_trade.py --symbol {symbol} "
                                             f"--direction {direction} --leverage 3 --env prod"}}
        return self.run_guard(payload)

    def assertAllowed(self, res):
        self.assertEqual(res["__exit_code__"], 0, res)
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "allow", res)

    def assertDeniedWith(self, res, fragment):
        self.assertEqual(res["__exit_code__"], 2, res)
        self.assertIn(fragment, res["__stderr__"])


class TestRecorderPerSessionFiles(SessionDossierHarness):

    def test_two_sessions_keep_their_own_files(self):
        a, b = self.record_two_sessions()
        self.assertEqual(self.load(self.session_file(SESSION_A))["approved_symbols"], ["BTCUSDT"])
        self.assertEqual(self.load(self.session_file(SESSION_B))["approved_symbols"], ["ETHUSDT"])
        self.assertEqual(self.load(self.session_file(SESSION_A)), a)
        self.assertEqual(self.load(self.dossier_path), b)  # latest_dossier.json: the newest scan overall
        with open(os.path.join(self.root, "logs", "evaluations", "evaluations_history.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        self.assertEqual([r["parent_conversation_id"] for r in rows], [SESSION_A, SESSION_B])
        self.assertEqual([r["sha256"] for r in rows], [a["provenance"]["sha256"], b["provenance"]["sha256"]])

    def test_a_new_scan_replaces_only_its_own_session_file(self):
        self.record_two_sessions()
        self.record(SESSION_A, AGENT_A, "SOLUSDT", "LONG", self.now - 2)
        self.assertEqual(self.load(self.session_file(SESSION_A))["approved_symbols"], ["SOLUSDT"])
        self.assertEqual(self.load(self.session_file(SESSION_B))["approved_symbols"], ["ETHUSDT"])
        self.assertEqual(self.load(self.dossier_path)["approved_symbols"], ["SOLUSDT"])

    def test_manual_testnet_record_without_session_writes_latest_only(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rec.record_evaluation_dossier([{"symbol": "BTCUSDT", "direction": "LONG"}], target_env="testnet",
                                          base_dir=self.root, shadow=False)
        self.assertEqual(dp.dossier_paths(self.root), [self.dossier_path])
        self.assertEqual(self.load(self.dossier_path)["approved_symbols"], ["BTCUSDT"])

    def test_old_session_files_are_pruned(self):
        eval_dir = os.path.dirname(self.dossier_path)
        old, fresh = os.path.join(eval_dir, "dossier_old.json"), os.path.join(eval_dir, "dossier_fresh.json")
        for path in (old, fresh):
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
        stale = time.time() - rec.SESSION_DOSSIER_PRUNE_AFTER_S - 60
        os.utime(old, (stale, stale))
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(self.session_file(SESSION_A)))
        self.assertTrue(os.path.exists(self.dossier_path))

    def test_session_path_is_sanitised(self):
        eval_dir = os.path.dirname(self.dossier_path)
        path = dp.session_dossier_path(self.root, "../../etc/x y")
        self.assertEqual(os.path.dirname(path), eval_dir)
        self.assertEqual(os.path.basename(path), "dossier_.._.._etc_x_y.json")
        for bad in (None, "", "   ", "..", ".", 42):
            self.assertIsNone(dp.session_dossier_path(self.root, bad))


class TestHookReadsTheCallingSessionsDossier(SessionDossierHarness):

    def test_session_a_still_trades_after_session_b_recorded_a_scan(self):
        self.record_two_sessions()
        self.assertAllowed(self.deploy(SESSION_A, "BTCUSDT", "SHORT"))   # the bug: B's scan replaced A's approval
        # Issue #280: A's allowed opening claimed the trading lease, so B is denied until it takes it over
        self.assertDeniedWith(self.deploy(SESSION_B, "ETHUSDT", "LONG"), "trading lease is held by session 5e55105e")
        # Issue #287: a takeover is committed only when the environment resolves to PROD (as the script resolves it)
        with patch("post_trade_sync.find_workspace_root", return_value=self.root), \
                patch.dict(os.environ, {"BINANCE_API_ENV": "prod"}):
            post_trade_sync.handle_post_trade_sync({
                "session_id": SESSION_B, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "python3 scripts/trading_lease.py --take"}, "tool_response": {}})
        self.assertAllowed(self.deploy(SESSION_B, "ETHUSDT", "LONG"))

    def test_session_b_trades_once_the_lease_of_a_is_stale(self):
        self.record_two_sessions()
        self.assertAllowed(self.deploy(SESSION_A, "BTCUSDT", "SHORT"))
        lease = self.load(trading_lease.lease_path(self.root))
        lease["heartbeat_at"] -= trading_lease.LEASE_STALE_SECONDS + 1
        with open(trading_lease.lease_path(self.root), "w", encoding="utf-8") as f:
            json.dump(lease, f)
        self.assertAllowed(self.deploy(SESSION_B, "ETHUSDT", "LONG"))
        self.assertEqual(self.load(trading_lease.lease_path(self.root))["session_id"], SESSION_B)

    def test_another_sessions_dossier_never_authorises_a_trade(self):
        self.record_two_sessions()
        self.assertDeniedWith(self.deploy(SESSION_A, "ETHUSDT", "LONG"), "'ETHUSDT' was NOT approved")
        self.assertDeniedWith(self.deploy(SESSION_B, "BTCUSDT", "SHORT"), "'BTCUSDT' was NOT approved")

    def test_a_session_without_its_own_file_keeps_the_current_message(self):
        self.record_two_sessions()
        self.assertDeniedWith(self.deploy(SESSION_C, "ETHUSDT", "LONG"), "not for the current conversation")

    def test_an_unreadable_own_file_denies_without_falling_back_to_latest(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 5)  # latest also approves it
        with open(self.session_file(SESSION_A), "w", encoding="utf-8") as f:
            f.write("{bad")
        self.assertDeniedWith(self.deploy(SESSION_A, "BTCUSDT", "SHORT"), "Failed to read evaluation dossier")

    def test_leverage_gate_reads_the_calling_sessions_file(self):
        self.record(SESSION_A, AGENT_A, "PEPEUSDT", "LONG", self.now - 60, extra={"is_yolo": True, "leverage": 10})
        self.record(SESSION_B, AGENT_B, "ETHUSDT", "LONG", self.now - 5)
        ok, reason = pre_trade_guard.check_leverage_gate("PEPEUSDT", 10, self.root, target_env="prod",
                                                         conversation_id=SESSION_A)
        self.assertTrue(ok, reason)
        ok, reason = pre_trade_guard.check_leverage_gate("PEPEUSDT", 10, self.root, target_env="prod",
                                                         conversation_id=SESSION_B)
        self.assertFalse(ok)


class TestExecutorFindsTheApprovingRecord(SessionDossierHarness):

    def test_right_file_by_symbol_and_direction(self):
        self.record_two_sessions()
        ok, _, cand, path = dp.find_approving_dossier("BTCUSDT", "SHORT", "prod", self.root)
        self.assertTrue(ok)
        self.assertEqual(path, self.session_file(SESSION_A))
        self.assertEqual(cand["dossier_session"], SESSION_A)
        ok, _, cand, path = dp.find_approving_dossier("ETHUSDT", "LONG", "prod", self.root)
        self.assertTrue(ok)
        self.assertEqual(cand["dossier_session"], SESSION_B)
        ok, reason, cand = eft.enforce_evaluation_dossier("BTCUSDT", "SHORT", "prod", base_dir=self.root)
        self.assertTrue(ok, reason)
        self.assertEqual(cand["dossier_sha256"], self.load(self.session_file(SESSION_A))["provenance"]["sha256"])

    def test_newest_approving_record_wins(self):
        a = self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 60)
        b = self.record(SESSION_B, AGENT_B, "BTCUSDT", "SHORT", self.now - 5, extra={"entry": 1.0})
        self.assertNotEqual(a["provenance"]["sha256"], b["provenance"]["sha256"])
        os.remove(self.dossier_path)  # only the two per-session files remain
        ok, _, cand, path = dp.find_approving_dossier("BTCUSDT", "SHORT", "prod", self.root)
        self.assertTrue(ok)
        self.assertEqual((cand["dossier_sha256"], path), (b["provenance"]["sha256"], self.session_file(SESSION_B)))

    def test_no_approving_record_keeps_the_current_denial(self):
        self.record_two_sessions()
        expected = dp.validate_dossier_for_trade("BTCUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(expected[0])
        ok, reason, cand, path = dp.find_approving_dossier("BTCUSDT", "LONG", "prod", self.root)
        self.assertEqual((ok, reason, cand, path), (False, expected[1], None, self.dossier_path))
        ok, reason, _ = eft.enforce_evaluation_dossier("BTCUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn(expected[1], reason)

    def test_an_edited_session_file_is_skipped(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 60)
        self.record(SESSION_B, AGENT_B, "ETHUSDT", "LONG", self.now - 5)
        forged = self.load(self.session_file(SESSION_B))
        forged["approved_symbols"].append("SOLUSDT")
        forged["approved_candidates"].append(dict(forged["approved_candidates"][0], symbol="SOLUSDT"))
        with open(self.session_file(SESSION_B), "w", encoding="utf-8") as f:
            json.dump(forged, f)
        ok, _, _, _ = dp.find_approving_dossier("SOLUSDT", "LONG", "prod", self.root)
        self.assertFalse(ok)


class TestShaBoundReadersReadTheValidatedFile(SessionDossierHarness):
    """Latest = session B; the trade is session A's SHORT: every reader must find A's radar snapshot."""

    def setUp(self):
        super().setUp()
        self.record_two_sessions()
        ok, reason, self.cand, _ = dp.find_approving_dossier("BTCUSDT", "SHORT", "prod", self.root)
        self.assertTrue(ok, reason)

    def test_score_calibration_snapshot(self):
        self.assertEqual(scal.radar_snapshot_matches(self.cand, self.root),
                         (True, "radar snapshot matches the dossier score"))
        self.assertIsNone(scal.snapshot_confirmation_required(self.cand, "prod", self.root))
        self.assertIsNone(scal.tier_s_confirmation_required(self.cand, "prod", {}, self.root))
        # an unknown sha still reads latest_dossier.json and reports dossier_changed (fail closed: ask)
        self.assertEqual(scal.radar_snapshot_matches(dict(self.cand, dossier_sha256="f" * 64), self.root),
                         (False, "dossier_changed"))

    def test_executor_radar_snapshot_and_squeeze_fallback(self):
        with patch("execute_futures_trade._workspace_dir", return_value=self.root):
            row, reason = eft.read_radar_snapshot("BTCUSDT", "SHORT", self.cand["dossier_sha256"])
            meta = eft.build_score_meta(self.cand, "BTCUSDT", "SHORT")
        self.assertEqual((row, reason), ({"symbol": "BTCUSDT", "direction": "SHORT", "confidence": 85}, None))
        self.assertEqual((meta["score_source"], meta["dossier_session"]), ("radar_snapshot", SESSION_A))
        self.assertIsNone(eft.squeeze_fallback_message(self.cand, self.root))
        self.assertIsNone(pre_trade_guard._squeeze_fallback_message(self.cand, self.root))

    def test_yolo_detection_uses_the_trades_own_dossier(self):
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        os.makedirs(os.path.join(ws, "logs", "evaluations"))
        files = {dp.default_dossier_path(ws): ("s-latest", {"symbol": "ETHUSDT", "is_yolo": False}),
                 dp.session_dossier_path(ws, SESSION_A): ("s-a", {"symbol": "PEPEUSDT", "is_yolo": True})}
        for path, (sha, cand) in files.items():
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"provenance": {"sha256": sha}, "approved_candidates": [cand]}, f)
        self.assertEqual(eft.detect_yolo_position("PEPEUSDT", base_dir=ws, record={"dossier_sha256": "s-a"}),
                         (True, "dossier_candidate"))
        self.assertEqual(eft.detect_yolo_position("PEPEUSDT", base_dir=ws, record={"dossier_sha256": None}),
                         (False, None))


class TestRecheckPlanOfTheRightSession(SessionDossierHarness):

    def test_own_plan_is_found_when_latest_is_another_session(self):
        a, _ = self.record_two_sessions()
        plan = rb.load_confirmed_plan("BTCUSDT", "SHORT", "prod", self.root)
        self.assertEqual(plan["sha256"], a["provenance"]["sha256"])
        plan = rb.load_confirmed_plan("BTCUSDT", "SHORT", "prod", self.root, session=SESSION_A)
        self.assertEqual(plan["sha256"], a["provenance"]["sha256"])
        with self.assertRaises(rb.RecheckError):
            rb.load_confirmed_plan("BTCUSDT", "SHORT", "prod", self.root, session=SESSION_B)

    def test_two_sessions_approving_the_same_plan_refuse(self):
        self.record(SESSION_A, AGENT_A, "BTCUSDT", "SHORT", self.now - 60)
        self.record(SESSION_B, AGENT_B, "BTCUSDT", "SHORT", self.now - 5, extra={"entry": 1.0})  # another plan
        with self.assertRaises(rb.RecheckError) as cm:
            rb.load_confirmed_plan("BTCUSDT", "SHORT", "prod", self.root)
        self.assertIn("ambiguous", str(cm.exception))


# =============================================================================
# 2. Origin of positions and resting entries
# =============================================================================
class TestOriginTag(unittest.TestCase):

    def test_score_audit_keys_carry_the_session(self):
        self.assertIn("dossier_session", eft.SCORE_AUDIT_KEYS)
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        with patch("execute_futures_trade._workspace_dir", return_value=ws):
            meta = eft.build_score_meta({"tier": "S", "score": 85, "dossier_sha256": "abc",
                                         "dossier_session": SESSION_A}, "BTCUSDT", "LONG")
        self.assertEqual(meta["dossier_session"], SESSION_A)
        self.assertEqual(eft.score_audit_fields({})["dossier_session"], None)

    def test_origin_tag_only_when_known(self):
        self.assertEqual(sss.origin_tag(None), "")
        self.assertEqual(sss.origin_tag({"dossier_session": None, "dossier_sha256": None}), "")
        self.assertEqual(sss.origin_tag({"dossier_sha256": "9f8e7d6c5b4a3210"}), "dossier:9f8e7d6c")
        self.assertEqual(sss.origin_tag({"dossier_session": SESSION_A, "dossier_sha256": "9f8e7d6c5b4a3210"}),
                         "session:5e55105e dossier:9f8e7d6c")


class TestSyncOrigin(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def sync(self, fake):
        with contextlib.ExitStack() as stack:
            for p in (patch.object(sss, "LOGS_DIR", self.tmp),
                      patch.object(sss, "STATE_FILE", os.path.join(self.tmp, "session_state.json")),
                      patch.object(sss, "AUDIT_LOG", os.path.join(self.tmp, "trades_audit.jsonl")),
                      patch.dict(sys.modules, {"shadow_tracker": None}),
                      patch("execute_futures_trade.send_signed_request", side_effect=fake)):
                stack.enter_context(p)
            return sss.sync_session_state(target_env="prod")

    def registry(self, *records):
        entries = {eft.pending_entry_key(r["target_env"], r["symbol"], r["entry_id"]): r for r in records}
        with open(os.path.join(self.tmp, "pending_entries.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2, "entries": entries}, f)

    def test_resting_entries_and_positions_carry_the_origin_only_when_known(self):
        meta = dict(eft.score_audit_fields({}), dossier_session=SESSION_A, dossier_sha256="9f8e7d6c5b4a3210")
        self.registry(tpe.make_record(env="prod", entry_id="7001", symbol="BTCUSDT", score_meta=meta),
                      tpe.make_record(env="prod", kind="LIMIT", entry_id="77", symbol="ETHUSDT", direction="SHORT",
                                      score_meta=eft.score_audit_fields({})))
        with open(os.path.join(self.tmp, "trades_audit.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"symbol": "SOLUSDT", "direction": "LONG", "target_env": "prod",
                                "dossier_session": SESSION_B, "dossier_sha256": "0123456789abcdef"}) + "\n")
            f.write(json.dumps({"symbol": "ADAUSDT", "direction": "SHORT", "target_env": "prod",
                                "dossier_session": SESSION_B, "dossier_sha256": "0123456789abcdef"}) + "\n")
        fake = t101.LiveExchange(positions=[t101.pos("SOLUSDT", "1", 100), t101.pos("ADAUSDT", "2", 1),
                                            t101.pos("DOTUSDT", "3", 5)],
                                 algos=[{"algoId": 7001, "symbol": "BTCUSDT", "side": "BUY",
                                         "orderType": "STOP_MARKET", "triggerPrice": "101.0", "quantity": "12",
                                         "closePosition": False, "reduceOnly": False}],
                                 orders=[t101.resting_limit(77, "ETHUSDT", "SELL", 98.0, "1.5")])
        state = self.sync(fake)
        self.assertIs(state["is_valid"], True)
        self.assertEqual(state["portfolio_exposure"]["resting_entries"], [
            {"symbol": "BTCUSDT", "dir": "LONG", "kind": "STOP_MARKET", "origin": "session:5e55105e dossier:9f8e7d6c"},
            {"symbol": "ETHUSDT", "dir": "SHORT", "kind": "LIMIT"}])
        origins = {p["symbol"]: p.get("origin") for p in state["active_positions"]}
        # ADAUSDT: the audit record is of the other direction (another trade): unknown origin
        self.assertEqual(origins, {"SOLUSDT": "session:5e55105e dossier:01234567", "ADAUSDT": None,
                                   "DOTUSDT": None})
        self.assertNotIn("origin", next(p for p in state["active_positions"] if p["symbol"] == "DOTUSDT"))


class TestBriefOrigin(unittest.TestCase):

    def brief(self, state):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with patch.object(peb, "BRIEF_FILE", os.path.join(tmp, "primed_brief.json")), \
             patch.object(peb, "ensure_fresh_state", return_value=state), \
             patch.object(peb, "get_latest_screening_payload", return_value={}), \
             patch.object(peb, "load_recent_insights", return_value=[]), \
             patch.object(peb, "build_risk_profile", return_value={}):
            return peb.assemble_primed_brief(target_env="prod")

    @staticmethod
    def state(origin):
        extra = {"origin": origin} if origin else {}
        positions = [dict({"symbol": s, "direction": "LONG", "entry_price": 1.0, "mark_price": 1.0,
                           "unrealized_pnl_usdt": 0.0, "roe_pct": 0.0, "sl_price": 0.9}, **extra)
                     for s in ("SOLUSDT", "DOTUSDT", "ADAUSDT")]
        resting = [dict({"symbol": s, "dir": "SHORT", "kind": "LIMIT"}, **extra) for s in ("ETHUSDT", "VVVUSDT")]
        return {"target_env": "prod", "is_valid": True, "active_positions": positions,
                "portfolio_exposure": {"delta_bias": "DELTA_BALANCED", "delta_bias_incl_resting": "DELTA_BALANCED",
                                       "resting_entries": resting}}

    def test_origin_shown_only_when_known_and_small(self):
        origin = "session:5e55105e dossier:9f8e7d6c"
        tagged, plain = self.brief(self.state(origin)), self.brief(self.state(None))
        self.assertEqual([p.get("origin") for p in tagged["ground_truth_portfolio"]["positions_summary"]],
                         [origin] * 3)
        self.assertEqual(tagged["pending_entries"][0],
                         {"symbol": "ETHUSDT", "dir": "SHORT", "kind": "LIMIT", "origin": origin})
        # unknown origin: the shapes are unchanged (no key, no text)
        self.assertEqual(plain["pending_entries"][0], {"symbol": "ETHUSDT", "dir": "SHORT", "kind": "LIMIT"})
        self.assertTrue(all("origin" not in p for p in plain["ground_truth_portfolio"]["positions_summary"]))
        md_tagged, md_plain = peb.format_markdown_brief(tagged), peb.format_markdown_brief(plain)
        self.assertIn(f"SOLUSDT (LONG PnL: $0.0 from {origin})", md_tagged)
        self.assertIn(f"ETHUSDT (SHORT LIMIT from {origin})", md_tagged)
        self.assertNotIn(" from ", md_plain.split("### ⚖️ Portfolio Ground Truth")[1].split("###")[0])
        # five tagged rows cost a bounded number of bytes, absorbed by the lesson budget
        extra = peb._brief_bytes(tagged) - peb._brief_bytes(plain)
        self.assertLessEqual(extra, 5 * (len(origin) + len('"origin": "",') + 2))


# =============================================================================
# 3. Reporter dedupe across sessions
# =============================================================================
class TestReporterOpenIssueLookup(_BacklogCase):

    def setUp(self):
        super().setUp()
        os.environ["GITHUB_TOKEN"] = "x"  # restored by _BacklogCase's patch.dict(clear=True)
        self.posts = []
        self.open_issues = []
        self.gh_calls = []
        self.gh_mode = "ok"
        for p in (patch.object(report_agent_issue.urllib.request, "urlopen", side_effect=self._urlopen),
                  patch("report_agent_issue.shutil.which", side_effect=self._which),
                  patch("report_agent_issue.subprocess.run", side_effect=self._run)):
            p.start()
            self.patches.append(p)

    def _urlopen(self, req, timeout=None):
        payload = json.loads(req.data.decode("utf-8"))
        self.posts.append(payload)
        number = len(self.posts)
        self.open_issues.append({"number": number, "url": f"https://github.com/owner/repo/issues/{number}",
                                 "body": payload["body"],
                                 "labels": [{"name": label} for label in payload["labels"]]})  # as GitHub lists it
        return fake_response({"number": number, "html_url": f"https://github.com/owner/repo/issues/{number}",
                              "labels": [{"name": label} for label in payload["labels"]]})

    def _which(self, name):
        return None if self.gh_mode == "no_gh" else f"/usr/bin/{name}"

    def _run(self, args, **kw):
        self.gh_calls.append((args, kw))
        if args[:3] != ["gh", "issue", "list"]:
            raise AssertionError(f"unexpected subprocess {args}")
        if self.gh_mode == "timeout":
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))
        if self.gh_mode == "fails":
            return MagicMock(returncode=1, stdout="", stderr="HTTP 503")
        if self.gh_mode == "garbage":
            return MagicMock(returncode=0, stdout="<html>", stderr="")
        if self.gh_mode == "fuzzy":
            return MagicMock(returncode=0, stdout=json.dumps([dict(i, body="no fingerprint here")
                                                              for i in self.open_issues]), stderr="")
        return MagicMock(returncode=0, stdout=json.dumps(self.open_issues), stderr="")

    def report(self, fps_name, title="executor: SL unverified", error="boom"):
        with patch("report_agent_issue.FINGERPRINTS_FILE", os.path.join(self.tmp.name, fps_name)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return report_agent_issue.report_issue(title=title, error_detail=error, severity="HIGH",
                                                   category="tool_error", repo="owner/repo")

    def test_same_failure_from_two_sessions_yields_one_issue(self):
        first = self.report("session_1_fps.json")
        self.assertEqual((first["status"], len(self.posts)), ("PUBLISHED_GITHUB", 1))
        fp = first["fingerprint"]
        self.assertIn(f"| **Fingerprint ID** | `{fp}` |", self.posts[0]["body"])
        # The other session has its own local fingerprint store: only the GitHub lookup can catch it
        second = self.report("session_2_fps.json")
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["fingerprint"], fp)
        with open(os.path.join(self.tmp.name, "session_2_fps.json"), encoding="utf-8") as f:
            entry = json.load(f)[fp]
        self.assertEqual((entry["status"], entry["issue_number"], entry["count"]), ("PUBLISHED_GITHUB", 1, 2))
        args, kw = self.gh_calls[-1]
        self.assertEqual(args, ["gh", "issue", "list", "--repo", "owner/repo", "--state", "open", "--search",
                                f"{fp} in:body", "--json", "number,url,body,labels", "--limit", "10"])
        # Issue #284: in-process callers get the shorter bound; the CLI keeps 5 s
        self.assertEqual(kw["timeout"], report_agent_issue.INPROCESS_LOOKUP_TIMEOUT_S)
        self.assertEqual((report_agent_issue.INPROCESS_LOOKUP_TIMEOUT_S, report_agent_issue.DEDUPE_LOOKUP_TIMEOUT_S),
                         (3.0, 5.0))
        self.assertFalse(os.path.exists(self.backlog))

    def test_any_lookup_failure_creates_as_before(self):
        self.report("seed_fps.json")  # an open issue for the default failure exists
        for i, mode in enumerate(("no_gh", "timeout", "fails", "garbage", "fuzzy")):
            with self.subTest(mode=mode):
                self.gh_mode = mode
                before = len(self.posts)
                res = self.report(f"fps_{mode}.json")
                self.assertEqual(len(self.posts), before + 1, mode)
                self.assertEqual(res["status"], "PUBLISHED_GITHUB")

    def test_offline_queues_without_a_lookup(self):
        os.environ.pop("GITHUB_TOKEN")
        res = self.report("offline_fps.json")
        self.assertEqual(res["status"], "QUEUED_OFFLINE")
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.last_entry()["fingerprint"], res["fingerprint"])


class TestFingerprintFileConcurrency(_BacklogCase):

    def test_concurrent_writers_never_corrupt_or_lose_counts(self):
        fps = os.path.join(self.tmp.name, "fps.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            seed = report_agent_issue.report_issue(title="t", error_detail="e", severity="LOW")  # queued, count 1
            errors = []

            def dedupe():
                try:
                    report_agent_issue.report_issue(title="t", error_detail="e", severity="LOW")
                except Exception as e:  # pragma: no cover - surfaced below
                    errors.append(e)

            def write(i):
                try:
                    report_agent_issue.set_fingerprint(f"{i:016x}", {"count": 1, "status": "QUEUED_OFFLINE"})
                except Exception as e:  # pragma: no cover - surfaced below
                    errors.append(e)
            threads = [threading.Thread(target=dedupe) for _ in range(8)]
            threads += [threading.Thread(target=write, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(errors, [])
        with open(fps, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data[seed["fingerprint"]]["count"], 9)
        self.assertTrue(all(f"{i:016x}" in data for i in range(8)))
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")], [])


# The gh stub of test_report_issue plus `gh issue list`: an open issue carrying STUB_OPEN_ISSUE_FP, else exit 1
GH_STUB_WITH_LIST = GH_STUB.replace("\nexit 1\n", "\n" + r"""if [ "$1" = "issue" ] && [ "$2" = "list" ]; then
  if [ -n "$STUB_OPEN_ISSUE_FP" ]; then
    printf '[{"number": 5, "url": "https://github.com/owner/repo/issues/5", "body": "| **Fingerprint ID** | `%s` |", "labels": [{"name": "severity:%s"}]}]' "$STUB_OPEN_ISSUE_FP" "${STUB_OPEN_ISSUE_SEVERITY:-high}"
    exit 0
  fi
  exit 1
fi
exit 1
""")


@unittest.skipUnless(shutil.which("bash") and os.name != "nt", "requires a POSIX bash")
class TestReportIssueShDedupe(unittest.TestCase):

    TITLE, ERROR = "executor: SL unverified", "boom"

    def setUp(self):
        self.stub_dir = tempfile.mkdtemp()
        self.logs_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.stub_dir, True)
        self.addCleanup(shutil.rmtree, self.logs_dir, True)
        for name, content in (("gh", GH_STUB_WITH_LIST), ("curl", CURL_STUB)):
            stub = os.path.join(self.stub_dir, name)
            with open(stub, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(stub, 0o755)
        with open(os.path.join(self.logs_dir, "session_state.json"), "w", encoding="utf-8") as f:
            json.dump(session_state(), f)
        self.env = dict(os.environ, PATH=self.stub_dir + os.pathsep + os.environ.get("PATH", ""),
                        GITHUB_REPO="owner/repo", GITHUB_TOKEN="", BINANCE_API_ENV="TESTNET",
                        ISSUE_REPORTER_LOGS_DIR=self.logs_dir)
        for key in ("STUB_AUTH_FAILS", "STUB_POST_FAILS", "STUB_OPEN_ISSUE_FP", "STUB_REJECT_LABELS",
                    "STUB_DROP_LABELS", "STUB_CURL_MODE"):
            self.env.pop(key, None)
        self.fp = report_agent_issue.compute_fingerprint(report_agent_issue.sanitize_telemetry(self.TITLE),
                                                         self.ERROR)

    def run_script(self, **env):
        return subprocess.run(["bash", REPORT_ISSUE_SH, "--title", self.TITLE, "--error", self.ERROR,
                               "--severity", "HIGH", "--category", "tool_error"],
                              capture_output=True, text=True, env=dict(self.env, **env), timeout=90)

    def calls(self):
        path = os.path.join(self.stub_dir, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f]

    def test_body_carries_the_shared_fingerprint_and_lookup_fails_open(self):
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("issues/7", res.stdout)
        with open(os.path.join(self.stub_dir, "payload.json"), encoding="utf-8") as f:
            body = json.load(f)["body"]
        self.assertIn(f"| **Fingerprint ID** | `{self.fp}` |", body)
        self.assertIn(f"issue list --repo owner/repo --state open --search {self.fp} in:body --json "
                      "number,url,body,labels --limit 10", self.calls())

    def test_open_issue_with_the_fingerprint_is_not_duplicated(self):
        res = self.run_script(STUB_OPEN_ISSUE_FP=self.fp)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("Deduplicated: open issue #5", res.stdout)
        self.assertIn("https://github.com/owner/repo/issues/5", res.stdout)
        self.assertFalse(any(c.startswith("api -X POST") for c in self.calls()))
        self.assertFalse(os.path.exists(os.path.join(self.logs_dir, "issues_backlog.jsonl")))

    def test_offline_still_queues(self):
        res = self.run_script(STUB_AUTH_FAILS="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with open(os.path.join(self.logs_dir, "issues_backlog.jsonl"), encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(entries), 1)
        self.assertIn(self.fp, entries[0]["body"])
        self.assertFalse(any(c.startswith("api -X POST") for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
