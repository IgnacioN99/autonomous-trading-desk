#!/usr/bin/env python3
"""
test_issue_298_recheck_window.py - Issue #298 (run B1, item 1): a re-check finds the approval across the session's
dossiers.

Covers utils/recheck_brief.load_confirmed_plan's search of the session's earlier scans (history rows re-verified from
their evaluator transcripts, window = recheck_max_age_seconds, newest approval wins, one re-check per original
dossier, another session's scans never used, no session id keeps the previous behaviour), the recorder's superseded
approvals block and `superseded_symbols` history field, and the brief-mismatch warning (record_evaluation).

Hermetic: temp workspaces, fake agy transcripts (AGY_BRAIN_DIRS, fixtures of test_issue_267_recheck) and fake Claude
Code transcripts (CLAUDE_PROJECTS_DIRS, SessionDossierHarness), mocked screening fetch, urllib blocked, no Binance
client and no .env read (explicit --env prod everywhere).
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
TESTS_DIR = os.path.join(BASE_DIR, "tests")
for _p in (SCRIPTS_DIR, HOOKS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import record_evaluation as rec  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
from utils import recheck_brief as rcb  # noqa: E402
import test_dossier_provenance as tdp  # noqa: E402  (fixtures only)
import test_issue_267_recheck as t267  # noqa: E402  (fixtures only; its TestCases are not re-exported)
import test_issue_270_session_dossiers as t270  # noqa: E402  (fixtures only)

SYMBOL, DIRECTION = t267.SYMBOL, t267.DIRECTION
OLD_CAND = t267.OLD_CAND
ZRO_CAND = dict(OLD_CAND, symbol="ZROUSDT")
SESSION = tdp.PARENT_ID  # every fake agy transcript names this parent session
CONV_1 = "5ca10001-1111-4222-8333-444455556666"
CONV_2 = "5ca10002-1111-4222-8333-444455556666"
CONV_3 = "5ca10003-1111-4222-8333-444455556666"


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


class _WindowWorkspace(t267._RecheckWorkspace):
    """_RecheckWorkspace whose scans go through the recorder (history rows) in one agy session."""

    def setUp(self):
        super().setUp()
        os.environ[rcb.SESSION_ENV] = SESSION  # restored by _RecheckWorkspace's patch.dict

    def scan(self, conv, cands, ts):
        """A full-scan dossier of SESSION approving `cands`, evaluated at `ts` and recorded 5 s later."""
        payload = {"status": "APPROVED", "evaluator_agent": dp.EVALUATOR_NAME, "target_env": "PROD",
                   "approved_candidates": [dict(c) for c in cands], "summary": "full scan"}
        self.standard_transcript(conv, payload, ts)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(rec, "_register_shadow"), redirect_stdout(out), redirect_stderr(err):
            record = rec.record_from_subagent(conv, target_env="prod", base_dir=self.workspace, now_ts=ts + 5)
        self.scan_out = out.getvalue()
        return record

    @staticmethod
    def sha(record):
        return record["provenance"]["sha256"]

    def history_path(self):
        return os.path.join(self.workspace, "logs", "evaluations", "evaluations_history.jsonl")

    def plan(self, symbol=SYMBOL, direction=DIRECTION, notes=None):
        return rcb.load_confirmed_plan(symbol, direction, "prod", self.workspace, notes=notes)

    def refused(self, fragment, symbol=SYMBOL, direction=DIRECTION):
        with self.assertRaises(rcb.RecheckError) as cm:
            self.plan(symbol, direction)
        self.assertIn(fragment, str(cm.exception))
        return str(cm.exception)


# =============================================================================
# 1. The older scan of the session is re-checked (end to end) and the no-session behaviour
# =============================================================================
class TestOlderScanOfTheSession(_WindowWorkspace):

    def test_newer_scan_without_the_candidate_re_checks_the_older_one(self):
        first = self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        second = self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        self.assertEqual(self.sha(dp.load_dossier(self.dossier_path)), self.sha(second))
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        self.assertEqual(brief["recheck_of"]["sha256"], self.sha(first))
        self.assertEqual((brief["recheck_of"]["entry"], brief["recheck_of"]["evaluated_ts"]),
                         (1.0, first["timestamp_ts"]))
        self.assertIn(f"Re-check of an earlier scan of session {SESSION}: dossier sha256 {self.sha(first)[:16]}", err)
        self.assertIn("the latest dossier does not approve ETHFIUSDT LONG", err)
        # The live re-check dossier: tier, levels and confirmation only from the new candidate; normal provenance
        record, out, _ = self.record_new(self.new_payload(brief, tier="A+", requires_user_confirmation=True), brief)
        self.assertEqual(record["recheck_of"]["sha256"], self.sha(first))
        self.assertEqual(self.history()[-1]["recheck_of"], self.sha(first))
        cand = record["approved_candidates"][0]
        self.assertEqual((cand["tier"], cand["entry"], cand["requires_user_confirmation"]), ("A+", 1.004, True))
        ok, reason, _ = dp.rebuild_verified_record(dp.load_dossier(self.dossier_path))
        self.assertTrue(ok, reason)
        # recheck_bounds still decides (the tier downgrade is out of bounds)
        self.assertFalse(record["recheck_bounds"]["within_bounds"])
        self.assertIn("[FAIL] tier: A+ (limit >= S)", out)

    def test_within_bounds_against_the_older_original(self):
        first = self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        record, out, _ = self.record_new(self.new_payload(brief), brief)
        self.assertEqual(record["recheck_of"]["sha256"], self.sha(first))
        self.assertTrue(record["recheck_bounds"]["within_bounds"], record["recheck_bounds"])
        self.assertIn("WITHIN BOUNDS", out)

    def test_without_a_session_id_the_previous_refusal_and_a_hint(self):
        self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        for value in (None, "", "   "):
            with self.subTest(value=value):
                os.environ.pop(rcb.SESSION_ENV, None)
                if value is not None:
                    os.environ[rcb.SESSION_ENV] = value
                code, _, err = self.run_brief()
                self.assertEqual(code, 2)
                self.assertIn("the latest dossier does not approve ETHFIUSDT LONG: run a full scan", err)
                self.assertIn(f"earlier scans of this session are searched only with {rcb.SESSION_ENV} set", err)
                self.fetch.assert_not_called()
                self.assertFalse(os.path.exists(self.brief_path))

    def test_latest_still_approving_is_used_first(self):
        self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        second = self.scan(CONV_2, [dict(OLD_CAND, entry=1.001), ZRO_CAND], self.now - 600)
        notes = []
        self.assertEqual(self.plan(notes=notes)["sha256"], self.sha(second))
        self.assertEqual(notes, [])


# =============================================================================
# 2. Window, unverifiable rows, unreadable history
# =============================================================================
class TestWindowAndVerification(_WindowWorkspace):

    def test_older_scan_outside_the_window_refuses(self):
        self.scan(CONV_1, [OLD_CAND], self.now - 1900)  # expired, and older than recheck_max_age_seconds (1800)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        msg = self.refused("no approval of ETHFIUSDT LONG in the last 30 min of session")
        self.assertIn("the latest dossier does not approve ETHFIUSDT LONG", msg)
        self.assertIn("1 scans checked, 0 unverifiable, none approves it: run a full scan", msg)
        # The window is the profile's recheck_max_age_seconds
        os.makedirs(os.path.join(self.workspace, "config"), exist_ok=True)
        with open(os.path.join(self.workspace, "config", "user_profile.json"), "w", encoding="utf-8") as f:
            json.dump({"recheck_max_age_seconds": 3600}, f)
        self.assertEqual(self.plan()["evaluated_ts"], self.now - 1900)

    def test_missing_transcript_is_skipped_and_counted(self):
        self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        os.remove(dp.find_subagent_transcript(CONV_1))
        self.refused("2 scans checked, 1 unverifiable, none approves it")

    def test_sha_mismatch_is_skipped_and_never_trusted(self):
        first = self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        # The row names another sha256 than the block its transcript holds (a newer evaluator block, or an edit)
        with open(self.history_path(), encoding="utf-8") as f:
            text = f.read()
        with open(self.history_path(), "w", encoding="utf-8") as f:
            f.write(text.replace(self.sha(first), "d" * 64))
        self.refused("1 unverifiable, none approves it")

    def test_edited_transcript_is_skipped(self):
        self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        path = dp.find_subagent_transcript(CONV_1)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("0.97", "0.95"))  # the block no longer hashes to the row's sha256
        self.refused("1 unverifiable, none approves it")

    def test_unreadable_history_refuses(self):
        self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        with open(self.history_path(), "wb") as f:
            f.write(b"\xff\xfe\x00 not utf-8\n")
        self.refused("the evaluation history is unreadable")

    def test_garbled_row_naming_the_original_refuses(self):
        first = self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        with open(self.history_path(), "a", encoding="utf-8") as f:
            f.write("{garbled " + self.sha(first) + "\n")
        self.refused("is unreadable")


# =============================================================================
# 3. Newest approval wins; one re-check per original; YOLO
# =============================================================================
class TestChoiceAndChainGuard(_WindowWorkspace):

    def test_newest_approval_wins_and_the_others_are_listed(self):
        first = self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        second = self.scan(CONV_2, [dict(OLD_CAND, entry=1.002)], self.now - 900)
        self.scan(CONV_3, [ZRO_CAND], self.now - 300)
        notes = []
        plan = self.plan(notes=notes)
        self.assertEqual((plan["sha256"], plan["entry"]), (self.sha(second), 1.002))
        self.assertEqual(len(notes), 2)
        self.assertIn(self.sha(second)[:16], notes[0])
        self.assertIn(f"ETHFIUSDT LONG also approved in: {self.sha(first)[:16]}", notes[1])

    def test_an_original_already_re_checked_refuses(self):
        first = self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        record, _, _ = self.record_new(self.new_payload(brief), brief)
        self.assertEqual(record["recheck_of"]["sha256"], self.sha(first))
        # The session's latest dossier is that re-check: the existing rule (#279/#284)
        code, _, err = self.run_brief()
        self.assertEqual(code, 2)
        self.assertIn(f"the dossier of session {SESSION} is already a re-check", err)
        # A newer scan without the candidate: the window finds the original, already re-checked
        self.scan(CONV_3, [ZRO_CAND], self.now - 30)
        code, _, err = self.run_brief()
        self.assertEqual(code, 2)
        self.assertIn(f"ETHFIUSDT LONG of the dossier sha256 {self.sha(first)[:16]}… of session {SESSION} was already "
                      "re-checked (one re-check per candidate of an original dossier)", err)
        self.fetch.assert_not_called()


# =============================================================================
# 3b. Per-candidate chain guard (round 2): the issue's 02:39 dossier approving both XPL and PUMP
# =============================================================================
def zro_payload(run_id):
    """Live setup of ZROUSDT LONG (the second candidate of the same original scan)."""
    return t267.live_payload(run_id, top_candidates=[dict(t267.LIVE_ROW, symbol="ZROUSDT")],
                             recheck={"symbol": "ZROUSDT", "direction": DIRECTION, "setup_status": "found",
                                      "cause": None})


class TestPerCandidateChainGuard(_WindowWorkspace):

    def recheck_ethfi(self, status="APPROVED"):
        """Scan 1 approves ETHFIUSDT LONG and ZROUSDT LONG; ETHFIUSDT LONG is re-checked (verdict `status`)."""
        first = self.scan(CONV_1, [OLD_CAND, ZRO_CAND], self.now - 900)
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        record, _, _ = self.record_new(self.new_payload(brief, status=status), brief)
        self.assertEqual(record["recheck_of"]["sha256"], self.sha(first))
        return first

    def strip_candidate_fields(self):
        """History rows as written before round 2 (re-check rows without recheck_symbol / recheck_direction)."""
        rows = self.history()
        with open(self.history_path(), "w", encoding="utf-8") as f:
            for row in rows:
                row.pop("recheck_symbol", None)
                row.pop("recheck_direction", None)
                f.write(json.dumps(row) + "\n")

    def test_history_row_names_the_re_checked_candidate(self):
        first = self.recheck_ethfi()
        row = self.history()[-1]
        self.assertEqual((row["recheck_of"], row["recheck_symbol"], row["recheck_direction"]),
                         (self.sha(first), SYMBOL, DIRECTION))
        self.assertTrue(all("recheck_symbol" not in r for r in self.history()[:-1]))  # full scans: no fields

    def test_neutral_re_check_still_names_the_candidate(self):
        self.recheck_ethfi(status="NEUTRAL")
        row = self.history()[-1]
        self.assertEqual((row["status"], row["recheck_symbol"], row["recheck_direction"]),
                         ("NEUTRAL", SYMBOL, DIRECTION))

    def test_second_candidate_of_the_same_original_is_re_checkable(self):
        first = self.recheck_ethfi()
        # The session's latest dossier is the re-check of ETHFIUSDT LONG: ZROUSDT LONG comes from the original
        code, brief, err = self.run_brief(spec="ZROUSDT:LONG", payload_fn=zro_payload)
        self.assertEqual(code, 0, err)
        self.assertEqual((brief["recheck_of"]["sha256"], brief["recheck_of"]["symbol"]), (self.sha(first), "ZROUSDT"))
        self.assertIn("the latest dossier is a re-check of ETHFIUSDT LONG and does not approve ZROUSDT LONG", err)
        record, _, _ = self.record_new(self.new_payload(brief, symbol="ZROUSDT"), brief,
                                       conv="5ca10004-1111-4222-8333-444455556666")
        self.assertEqual(record["recheck_of"]["sha256"], self.sha(first))
        self.assertEqual(self.history()[-1]["recheck_symbol"], "ZROUSDT")
        # Now both candidates of that original are consumed
        for symbol in ("ZROUSDT", SYMBOL):
            with self.subTest(symbol=symbol):
                self.refused("cannot be re-checked for" if symbol == "ZROUSDT" else "was already re-checked",
                             symbol=symbol)

    def test_second_candidate_after_a_newer_scan(self):
        first = self.recheck_ethfi()
        self.scan(CONV_3, [dict(OLD_CAND, symbol="ARBUSDT")], self.now - 30)
        self.assertEqual(self.plan("ZROUSDT")["sha256"], self.sha(first))
        self.refused(f"ETHFIUSDT LONG of the dossier sha256 {self.sha(first)[:16]}…")

    def test_same_candidate_twice_is_refused_also_after_a_neutral_re_check(self):
        for status in ("APPROVED", "NEUTRAL"):
            with self.subTest(status=status):
                self.recheck_ethfi(status=status)
                # The latest dossier is that re-check: never re-checkable itself
                code, _, err = self.run_brief()
                self.assertEqual(code, 2)
                self.assertIn(f"the dossier of session {SESSION} is already a re-check and cannot be re-checked for "
                              f"{SYMBOL} {DIRECTION}", err)
                # After a newer scan, the original's candidate is consumed
                self.scan(CONV_3, [dict(OLD_CAND, symbol="ARBUSDT")], self.now - 30)
                self.refused("was already re-checked (one re-check per candidate of an original dossier)")
                os.remove(self.history_path())

    def test_old_format_row_blocks_every_candidate_of_the_original(self):
        first = self.recheck_ethfi()
        self.strip_candidate_fields()
        # The latest dossier is a re-check whose row does not name its candidate: refused for any candidate
        self.refused("is already a re-check and cannot be re-checked for ZROUSDT LONG", symbol="ZROUSDT")
        self.scan(CONV_3, [dict(OLD_CAND, symbol="ARBUSDT")], self.now - 30)
        for symbol in ("ZROUSDT", SYMBOL):
            with self.subTest(symbol=symbol):
                self.refused(f"the dossier sha256 {self.sha(first)[:16]}… of session {SESSION} was already "
                             "re-checked by a history row that does not name its candidate", symbol=symbol)

    def test_conflicting_stored_recheck_of_refuses(self):
        self.recheck_ethfi()
        # The row says ETHFIUSDT LONG; a hand edit of the stored recheck_of to name ZRO must not reopen anything
        for path in dp.dossier_paths(self.workspace):
            data = dp.load_dossier(path)
            if isinstance(data.get("recheck_of"), dict):
                data["recheck_of"]["symbol"] = "ZROUSDT"
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
        self.refused("cannot be re-checked for ETHFIUSDT LONG")
        self.refused("cannot be re-checked for ZROUSDT LONG", symbol="ZROUSDT")

    def test_yolo_candidate_still_refused(self):
        self.scan(CONV_1, [dict(OLD_CAND, is_yolo=True, tier="A")], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        self.refused("is a YOLO (memecoin slot) candidate")

    def test_newest_approval_being_yolo_refuses(self):
        self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [dict(OLD_CAND, is_yolo=True, tier="A")], self.now - 900)
        self.scan(CONV_3, [ZRO_CAND], self.now - 300)
        self.refused("needs a full scan")

    def test_old_history_rows_without_new_fields_parse(self):
        first = self.scan(CONV_1, [OLD_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 600)
        rows = self.history()
        self.assertEqual(rows[-1]["superseded_symbols"], ["ETHFIUSDT:LONG"])  # recorded while still unexpired
        self.assertEqual(self.plan()["sha256"], self.sha(first))
        with open(self.history_path(), "w", encoding="utf-8") as f:  # rows written before #298
            for row in rows:
                row.pop("superseded_symbols", None)
                f.write(json.dumps(row) + "\n")
        self.assertEqual(self.plan()["sha256"], self.sha(first))


# =============================================================================
# 4. Another session's scans are never used (Claude Code transcripts)
# =============================================================================
class TestOtherSessionsNeverUsed(t270.SessionDossierHarness):

    AGENT_A2 = "ab0000000000000001"
    AGENT_B2 = "ab0000000000000002"

    def plan(self, session, symbol="BTCUSDT", direction="SHORT", notes=None):
        with patch.dict(os.environ):
            os.environ[rcb.SESSION_ENV] = session
            return rcb.load_confirmed_plan(symbol, direction, "prod", self.root, notes=notes)

    def test_another_sessions_approval_is_not_used(self):
        self.record(t270.SESSION_A, t270.AGENT_A, "BTCUSDT", "SHORT", self.now - 300)
        self.record(t270.SESSION_B, t270.AGENT_B, "ETHUSDT", "LONG", self.now - 60)
        with self.assertRaises(rcb.RecheckError) as cm:
            self.plan(t270.SESSION_B)
        self.assertIn("does not approve BTCUSDT SHORT", str(cm.exception))
        self.assertIn("1 scans checked, 0 unverifiable, none approves it", str(cm.exception))

    def test_a_forged_row_naming_this_session_is_never_trusted(self):
        a = self.record(t270.SESSION_A, t270.AGENT_A, "BTCUSDT", "SHORT", self.now - 300)
        self.record(t270.SESSION_B, t270.AGENT_B, "ETHUSDT", "LONG", self.now - 60)
        path = os.path.join(self.root, "logs", "evaluations", "evaluations_history.jsonl")
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        forged = dict(next(r for r in rows if r["sha256"] == a["provenance"]["sha256"]),
                      parent_conversation_id=t270.SESSION_B)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(forged) + "\n")
        with self.assertRaises(rcb.RecheckError) as cm:
            self.plan(t270.SESSION_B)
        self.assertIn("2 scans checked, 1 unverifiable, none approves it", str(cm.exception))

    def test_own_earlier_scan_is_used(self):
        b1 = self.record(t270.SESSION_B, self.AGENT_B2, "BTCUSDT", "SHORT", self.now - 600)
        self.record(t270.SESSION_A, t270.AGENT_A, "BTCUSDT", "SHORT", self.now - 300, extra={"entry": 1.0})
        self.record(t270.SESSION_B, t270.AGENT_B, "ETHUSDT", "LONG", self.now - 60)
        self.assertEqual(self.plan(t270.SESSION_B)["sha256"], b1["provenance"]["sha256"])


# =============================================================================
# 5. Recorder: superseded approvals and the brief-mismatch warning
# =============================================================================
class TestRecorderSuperseded(_WindowWorkspace):

    def test_unexpired_dropped_approvals_are_listed(self):
        self.scan(CONV_1, [OLD_CAND, ZRO_CAND], self.now - 300)
        self.assertNotIn("Superseded", self.scan_out)
        self.scan(CONV_2, [ZRO_CAND], self.now - 200)
        self.assertIn("Superseded approvals of the previous scan of this session (re-check with "
                      "`prime_evaluator_brief.py --recheck SYMBOL:DIRECTION`", self.scan_out)
        self.assertIn("     - ETHFIUSDT LONG | tier=S | valid until", self.scan_out)
        self.assertNotIn("ZROUSDT LONG | tier", self.scan_out)
        self.assertEqual(self.history()[-1]["superseded_symbols"], ["ETHFIUSDT:LONG"])
        self.assertNotIn("superseded_symbols", self.history()[0])
        self.assertNotIn("superseded", dp.load_dossier(self.dossier_path))  # never stored in the dossier
        # The next scan's previous record (CONV_2) approved only ZRO: nothing to list
        self.scan(CONV_3, [ZRO_CAND], self.now - 100)
        self.assertNotIn("Superseded", self.scan_out)
        self.assertNotIn("superseded_symbols", self.history()[-1])

    def test_expired_approvals_are_not_listed(self):
        self.scan(CONV_1, [OLD_CAND, ZRO_CAND], self.now - 1500)
        self.scan(CONV_2, [ZRO_CAND], self.now - 200)  # recorded after the first scan expired
        self.assertNotIn("Superseded", self.scan_out)
        self.assertNotIn("superseded_symbols", self.history()[-1])

    def test_brief_replaced_before_recording_warns(self):
        with open(self.brief_path, "w", encoding="utf-8") as f:
            json.dump({"generated_at_ts": self.now, "target_env": "PROD"}, f)
        # Evaluated on an earlier brief (e.g. a --recheck brief another session replaced with a normal one)
        payload = self.new_payload({"generated_at_ts": self.now - 100})
        record, out, err = self.record_new(payload, {"generated_at_ts": self.now - 98})
        self.assertNotIn("recheck_of", record)
        self.assertIn(f"BRIEF REPLACED: logs/primed_brief.json (generated_at_ts {self.now}) is not the brief "
                      f"this dossier was evaluated on (brief_generated_at_ts {self.now - 100})", err)
        self.assertIn("Brief replaced before recording: if this dossier answers a --recheck, there is no re-check "
                      "verdict", out)
        # Not a re-check verdict line: the dossier may well be a normal scan
        self.assertNotIn("RE-CHECK NOT LINKED", err)
        self.assertNotIn("Re-check: NOT LINKED", out)
        # The same brief: silent, as before
        record, out, err = self.record_new(self.new_payload({"generated_at_ts": self.now}),
                                           {"generated_at_ts": self.now - 2}, conv=CONV_3)
        self.assertEqual(err, "")
        self.assertNotIn("Re-check", out)


if __name__ == "__main__":
    unittest.main()
