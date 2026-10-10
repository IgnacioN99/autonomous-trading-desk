#!/usr/bin/env python3
"""
test_issue_279_recheck_residuals.py - Residuals of the re-check after dossier expiry (issue #279, follow-up of #267).

Covers the stop-distance bound and the TP1 checks of the bounds verdict (utils/recheck_bounds.py), the profile cap
of recheck_max_drift_r, the exact brief link and the explicit no-verdict line (record_evaluation.attach_recheck),
the printed execution deadline, the chain guard via the evaluations_history.jsonl row
(utils/recheck_brief.load_confirmed_plan) and the failure class in the `recheck.cause` of a failed screening fetch.

Hermetic: temp workspaces and fake agy transcripts (fixtures of test_issue_267_recheck), mocked screening fetch,
urllib blocked, no Binance client and no .env read (explicit --env prod everywhere).
"""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
TESTS_DIR = os.path.join(BASE_DIR, "tests")
for _p in (SCRIPTS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import record_evaluation as rec  # noqa: E402
import user_profile as up  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
from utils import recheck_bounds as rb  # noqa: E402
from utils import recheck_brief as rcb  # noqa: E402
from utils.gate_limits import MIN_TP1_DISTANCE  # noqa: E402
import test_issue_267_recheck as t267  # noqa: E402  (fixtures only; its TestCases are not re-exported)

SYMBOL, DIRECTION = t267.SYMBOL, t267.DIRECTION


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


# =============================================================================
# 1. Bounds verdict: stop distance and TP1 (utils/recheck_bounds.py)
# =============================================================================
class TestStopDistanceAndTp1(unittest.TestCase):
    NOW = 1_800_000_000

    def old(self, **kw):  # R = 0.03, so 0.25R = 0.0075
        return dict({"sha256": "a" * 64, "symbol": SYMBOL, "direction": DIRECTION, "tier": "S", "entry": 1.0,
                     "stop_loss": 0.97, "tp1": 1.054, "tp2": 1.15, "evaluated_ts": self.NOW - 600}, **kw)

    @staticmethod
    def new(**kw):
        cand = dict({"symbol": SYMBOL, "direction": DIRECTION, "tier": "S", "entry": 1.0, "stop_loss": 0.97,
                     "tp1": 1.054, "tp2": 1.15, "is_yolo": False}, **kw)
        return {"status": "APPROVED", "approved_candidates": [cand]}

    def verdict(self, old=None, new=None, drift=0.25):
        return rb.evaluate_recheck_bounds(old or self.old(), new or self.new(), self.NOW, drift, 1800)

    @staticmethod
    def failed(v):
        return {c["check"] for c in v["checks"] if not c["ok"]}

    @staticmethod
    def check(v, name):
        return next(c for c in v["checks"] if c["check"] == name)

    def test_opposite_moves_cannot_widen_the_stop_beyond_the_bound(self):
        # Entry +0.25R and SL -0.25R: each drift is at the bound, the stop is 1.5R of the original plan
        v = self.verdict(new=self.new(entry=1.0075, stop_loss=0.9625))
        self.assertFalse(v["within_bounds"])
        self.assertEqual(self.failed(v), {"stop_distance"})
        self.assertEqual(self.check(v, "stop_distance")["value"], "new 1.50R vs old 1.00R")
        lines = rb.format_recheck_verdict(v)
        self.assertTrue(lines[0].startswith("OUT OF BOUNDS"))
        self.assertTrue(any(l.startswith("[FAIL] stop_distance: new 1.50R vs old 1.00R (limit 0.75R-1.25R)")
                            for l in lines), lines)
        # Within the bound: +0.1R / -0.1R -> 1.2R
        ok = self.verdict(new=self.new(entry=1.003, stop_loss=0.967))
        self.assertTrue(ok["within_bounds"], ok)
        self.assertEqual(self.check(ok, "stop_distance")["value"], "new 1.20R vs old 1.00R")
        self.assertTrue(any(l.startswith("[PASS] stop_distance: new 1.20R vs old 1.00R")
                            for l in rb.format_recheck_verdict(ok)))
        # The profile maximum (0.5R) allows exactly 1.5R
        self.assertTrue(self.verdict(new=self.new(entry=1.0075, stop_loss=0.9625), drift=0.5)["within_bounds"])

    def test_tighter_stop_beyond_the_bound_asks_again(self):
        v = self.verdict(new=self.new(entry=0.9925, stop_loss=0.9775))  # 0.5R of the original stop
        self.assertEqual(self.failed(v), {"stop_distance"})
        self.assertEqual(self.check(v, "stop_distance")["value"], "new 0.50R vs old 1.00R")
        self.assertIn("outside 0.75R-1.25R", self.check(v, "stop_distance")["reason"])

    def test_short_stop_distance(self):
        old = self.old(direction="SHORT", entry=1.0, stop_loss=1.03, tp1=0.946, tp2=0.8)
        new = {"status": "APPROVED", "approved_candidates": [{
            "symbol": SYMBOL, "direction": "SHORT", "tier": "S", "entry": 0.9925, "stop_loss": 1.0375,
            "tp1": 0.946, "tp2": 0.8}]}
        v = rb.evaluate_recheck_bounds(old, new, self.NOW, 0.25, 1800)
        self.assertEqual(self.failed(v), {"stop_distance"})

    def test_stop_distance_not_measurable_fails(self):
        v = self.verdict(old=self.old(stop_loss=1.0))  # zero original R
        self.assertIn("stop_distance", self.failed(v))
        self.assertIn("not measurable", self.check(v, "stop_distance")["reason"])

    def test_tp1_drift_is_reported(self):
        v = self.verdict(new=self.new(tp1=1.06))  # 0.2R
        self.assertTrue(v["within_bounds"], v)
        self.assertEqual(self.check(v, "drift_tp1")["value"], 0.2)
        self.assertTrue(any(l.startswith("[PASS] drift_tp1: 0.2 (limit 0.25)") for l in rb.format_recheck_verdict(v)))
        self.assertEqual(self.failed(self.verdict(new=self.new(tp1=1.0618))), {"drift_tp1"})  # 0.26R

    def test_tp1_below_the_friction_floor_fails(self):
        self.assertEqual(MIN_TP1_DISTANCE, 0.0035)  # the executor's Gate 3 constant (utils/gate_limits.py)
        # Drift loosened to isolate the friction check
        v = self.verdict(new=self.new(tp1=1.0034), drift=3.0)
        self.assertFalse(v["within_bounds"])
        self.assertEqual(self.failed(v), {"tp1_friction"})
        self.assertEqual(self.check(v, "tp1_friction")["value"], "0.34%")
        self.assertTrue(any(l.startswith("[FAIL] tp1_friction: 0.34% (limit >= 0.35%)")
                            for l in rb.format_recheck_verdict(v)))
        self.assertTrue(self.verdict(new=self.new(tp1=1.0036), drift=3.0)["within_bounds"])
        for bad in (0.999, None, "x"):  # wrong side, missing, malformed
            with self.subTest(tp1=bad):
                self.assertIn("tp1_friction", self.failed(self.verdict(new=self.new(tp1=bad), drift=3.0)))
        old = self.old(direction="SHORT", entry=1.0, stop_loss=1.03, tp1=0.946, tp2=0.88)
        short = {"status": "APPROVED", "approved_candidates": [{
            "symbol": SYMBOL, "direction": "SHORT", "tier": "S", "entry": 1.0, "stop_loss": 1.03,
            "tp1": 0.9966, "tp2": 0.88}]}
        self.assertEqual(self.failed(rb.evaluate_recheck_bounds(old, short, self.NOW, 3.0, 1800)), {"tp1_friction"})


# =============================================================================
# 2. Profile cap
# =============================================================================
class TestDriftCap(unittest.TestCase):

    def test_values_above_the_cap_fall_back_to_the_default(self):
        self.assertEqual(up.RECHECK_MAX_DRIFT_R_MAX, 0.5)
        self.assertEqual(up.get_recheck_bounds({"recheck_max_drift_r": 0.5})["recheck_max_drift_r"], 0.5)
        for bad in (0.5000001, 0.75, 1, 2.0, -1, 0, "0.4", None, False, float("nan")):
            with self.subTest(bad=bad):
                self.assertEqual(up.get_recheck_bounds({"recheck_max_drift_r": bad})["recheck_max_drift_r"], 0.25)

    def test_example_file_equals_the_defaults(self):
        with open(os.path.join(BASE_DIR, "config", "user_profile.json.example"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual(up.get_recheck_bounds(example), up.get_recheck_bounds({}))
        self.assertEqual(example["recheck_max_drift_r"], up.DEFAULT_PROFILE["recheck_max_drift_r"])
        self.assertLessEqual(example["recheck_max_drift_r"], up.RECHECK_MAX_DRIFT_R_MAX)


# =============================================================================
# 3. Recorder: exact link, explicit no-verdict line, deadline, profile cap end to end
# =============================================================================
class TestRecorderResiduals(t267._RecheckWorkspace):

    def recheck(self):
        old = self.write_old_dossier()
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        return old, brief

    def write_profile(self, **values):
        os.makedirs(os.path.join(self.workspace, "config"), exist_ok=True)
        with open(os.path.join(self.workspace, "config", "user_profile.json"), "w", encoding="utf-8") as f:
            json.dump(values, f)

    def test_normal_linked_path_unchanged(self):
        old, brief = self.recheck()
        record, out, err = self.record_new(self.new_payload(brief), brief)
        self.assertEqual(record["recheck_of"]["sha256"], old["provenance"]["sha256"])
        self.assertTrue(record["recheck_bounds"]["within_bounds"], record["recheck_bounds"])
        self.assertIn("WITHIN BOUNDS", out)
        self.assertNotIn("NOT LINKED", out)
        self.assertNotIn("RE-CHECK NOT LINKED", err)
        self.assertIn("[PASS] stop_distance: new 1.00R vs old 1.00R", out)
        self.assertIn("[PASS] drift_tp1: 0.1333 (limit 0.25)", out)
        self.assertIn("[PASS] tp1_friction: 5.38% (limit >= 0.35%)", out)

    def test_missing_or_garbled_brief_prints_the_no_verdict_line(self):
        _, brief = self.recheck()
        payload = self.new_payload(brief)
        for i, content in enumerate((None, "{not json", "[1, 2]")):
            with self.subTest(content=content):
                if content is None:
                    os.remove(self.brief_path)
                else:
                    with open(self.brief_path, "w", encoding="utf-8") as f:
                        f.write(content)
                record, out, err = self.record_new(payload, brief, conv=f"eeeeeee{i}-1111-4222-8333-444455556666")
                self.assertNotIn("recheck_of", record)
                self.assertNotIn("recheck_bounds", record)
                self.assertIn("Re-check: NOT LINKED (no verdict)", out)
                self.assertIn("RE-CHECK NOT LINKED: logs/primed_brief.json is missing or unreadable", err)
                self.assertNotIn("WITHIN BOUNDS", out)

    def test_recheck_block_without_recheck_of_is_not_linked(self):
        _, brief = self.recheck()
        with open(self.brief_path, encoding="utf-8") as f:
            data = json.load(f)
        data.pop("recheck_of")
        with open(self.brief_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        record, out, err = self.record_new(self.new_payload(brief), brief)
        self.assertNotIn("recheck_of", record)
        self.assertIn("Re-check: NOT LINKED (no verdict)", out)
        self.assertIn("RE-CHECK NOT LINKED", err)

    def test_printed_deadline_is_the_earlier_of_expiry_and_age_bound(self):
        # Original evaluated 1500 s ago, max age 1800 s: the age bound ends before the new dossier expires
        old, brief = self.recheck()
        record, out, _ = self.record_new(self.new_payload(brief), brief)
        age_until = old["timestamp_ts"] + 1800
        self.assertLess(age_until, record["valid_until_ts"])
        fmt = lambda ts: rec._fmt_utc(ts, "%H:%M:%S UTC")  # noqa: E731
        self.assertIn(f"Deadline: valid until {fmt(record['valid_until_ts'])}, age bound until {fmt(age_until)}: "
                      f"execute before {fmt(age_until)}", out)
        # A longer age bound: the dossier's own expiry is the deadline
        self.write_profile(recheck_max_age_seconds=7200)
        old, brief = self.recheck()
        record, out, _ = self.record_new(self.new_payload(brief), brief, conv="ffffffff-1111-4222-8333-444455556666")
        self.assertIn(f"age bound until {fmt(old['timestamp_ts'] + 7200)}: execute before "
                      f"{fmt(record['valid_until_ts'])}", out)

    def test_deadline_without_readable_bounds(self):
        record = {"status": "NEUTRAL", "valid_until_ts": 1_800_001_200, "recheck_of": {"sha256": "a" * 64},
                  "recheck_bounds": {"within_bounds": False, "checks": [], "reasons": ["bounds check failed (X)"]}}
        out = __import__("io").StringIO()
        with patch("sys.stdout", out):
            rec._print_summary(record, os.path.join(self.workspace, "x.json"), self.workspace, 1_800_000_000)
        self.assertIn("OUT OF BOUNDS", out.getvalue())
        self.assertIn(f"age bound until unknown: execute before {rec._fmt_utc(1_800_001_200, '%H:%M:%S UTC')}",
                      out.getvalue())

    def test_profile_value_above_the_cap_uses_the_default(self):
        self.write_profile(recheck_max_drift_r=1.0)  # valid before #279, now above the 0.5R cap
        _, brief = self.recheck()
        record, out, _ = self.record_new(self.new_payload(brief, entry=1.012, stop_loss=0.982, tp2=1.132), brief)
        self.assertEqual(record["recheck_bounds"]["bounds"]["recheck_max_drift_r"], 0.25)
        self.assertFalse(record["recheck_bounds"]["within_bounds"])
        self.assertIn("OUT OF BOUNDS", out)


# =============================================================================
# 4. Chain guard via the history row (utils/recheck_brief.py)
# =============================================================================
class TestChainGuardHistory(t267._RecheckWorkspace):

    def history_path(self):
        return os.path.join(self.workspace, "logs", "evaluations", "evaluations_history.jsonl")

    def record_recheck_and_strip(self):
        """A recorded re-check dossier whose recheck_of / recheck_bounds were deleted by hand from every dossier
        file (they sit outside the sha256, so the record still verifies)."""
        self.write_old_dossier()
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        record, _, _ = self.record_new(self.new_payload(brief), brief)
        for path in dp.dossier_paths(self.workspace):
            data = dp.load_dossier(path)
            data.pop("recheck_of", None)
            data.pop("recheck_bounds", None)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        ok, reason, _ = dp.rebuild_verified_record(dp.load_dossier(self.dossier_path))
        self.assertTrue(ok, reason)
        return record

    def assertRefused(self, fragment):
        with open(self.brief_path, "rb") as f:
            before = f.read()
        code, _, err = self.run_brief()
        self.assertEqual(code, 2, err)
        self.assertIn(fragment, err)
        self.fetch.assert_not_called()
        with open(self.brief_path, "rb") as f:
            self.assertEqual(f.read(), before)  # no new brief written

    def test_stripped_recheck_is_still_refused_via_history(self):
        record = self.record_recheck_and_strip()
        self.assertEqual(self.history_row(record)["recheck_of"], record["recheck_of"]["sha256"])
        # Issue #284: the refusal names the session of the dossier that is already a re-check
        self.assertRefused(f"the dossier of session {record['parent_conversation_id']} is already a re-check")
        with self.assertRaises(rcb.RecheckError):  # a known session reads the same history
            rcb.load_confirmed_plan(SYMBOL, DIRECTION, "prod", self.workspace,
                                    session=record["parent_conversation_id"])

    def history_row(self, record):
        with open(self.history_path(), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return next(r for r in rows if r.get("sha256") == record["provenance"]["sha256"])

    def test_garbled_history_row_refuses(self):
        record = self.record_recheck_and_strip()
        sha = record["provenance"]["sha256"]
        with open(self.history_path(), encoding="utf-8") as f:
            lines = f.readlines()
        with open(self.history_path(), "w", encoding="utf-8") as f:
            f.writelines(line if sha not in line else line[:60] + sha + "\n" for line in lines)
        self.assertRefused("history row of the latest dossier is unreadable")

    def test_unreadable_history_refuses(self):
        self.record_recheck_and_strip()
        with open(self.history_path(), "wb") as f:
            f.write(b"\xff\xfe\x00 not utf-8\n")
        self.assertRefused("the evaluation history is unreadable")

    def test_original_with_a_plain_history_row_is_re_checked(self):
        old = self.write_old_dossier()
        with open(self.history_path(), "w", encoding="utf-8") as f:
            f.write("{garbled row of another dossier\n")
            f.write(json.dumps({"sha256": old["provenance"]["sha256"], "status": "APPROVED"}) + "\n")
            f.write(json.dumps({"sha256": "f" * 64, "recheck_of": "e" * 64}) + "\n")  # another dossier's re-check
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        self.assertEqual(brief["recheck_of"]["sha256"], old["provenance"]["sha256"])


# =============================================================================
# 5. Failure class in the cause of a failed screening fetch
# =============================================================================
class TestFetchFailureCause(t267._RecheckWorkspace):

    def test_failure_class_reaches_the_cause(self):
        class Done:
            def __init__(self, code, stdout):
                self.returncode, self.stdout = code, stdout
        cases = ((dict(side_effect=subprocess.TimeoutExpired(["x"], 60)), "TimeoutExpired"),
                 (dict(return_value=Done(1, "")), "exit 1"),
                 (dict(return_value=Done(0, "  ")), "empty output"),
                 (dict(return_value=Done(0, "[1]")), "not a JSON object"),
                 (dict(return_value=Done(0, "{bad")), "JSONDecodeError"))
        for kw, failure in cases:
            with self.subTest(failure=failure):
                with patch("subprocess.run", **kw):
                    payload = rcb.fetch_recheck_payload(SYMBOL, DIRECTION, "prod", "rid", self.workspace)
                self.assertEqual(payload, {"recheck_failure": failure})
                screening, block = rcb.recheck_inputs(payload, SYMBOL, DIRECTION, "rid")
                self.assertEqual(screening, {})
                self.assertEqual((block["setup_status"], block["cause"]),
                                 ("unavailable", f"screening pipeline failed ({failure})"))

    def test_brief_carries_the_failure_class(self):
        self.write_old_dossier()
        code, brief, err = self.run_brief(payload_fn=lambda r: {"recheck_failure": "TimeoutExpired"})
        self.assertEqual(code, 0, err)
        self.assertEqual(brief["recheck"]["setup_status"], "unavailable")
        self.assertEqual(brief["recheck"]["cause"], "screening pipeline failed (TimeoutExpired)")
        self.assertEqual(brief["filtered_opportunities"], [])


if __name__ == "__main__":
    unittest.main()
