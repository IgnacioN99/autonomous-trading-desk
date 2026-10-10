#!/usr/bin/env python3
"""
test_issue_290_shadow_followups.py - shadow regret / replay residuals after #262 (issue #290).

Covers the advisory PROMOTION GUARD line of the delta-gate report (each failing condition, the passing case, absent
without data, consumed by no code path), deduped_window printed by record_evaluation's shadow line and by the
--loop path of shadow_tracker, the named tied pair when a 0R tie splits under an imputation, the timestamp_ts ->
recorded_at_ts fallback of the dedupe reference with distinct values, the enum-parity helper's failure message and
the shared definitions of utils/shadow_common.py (re-exported by shadow_tracker, imported by shadow_analytics, which
no longer imports the tracker).
Hermetic: temp directories only (every shadow_tracker path redirected by TrackerBase), no Binance client, network
blocked, the loop's sleep patched.
"""

import io
import os
import re
import sys
import copy
import json
import time
import unittest
import contextlib
import importlib.util
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (SCRIPTS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import shadow_tracker as st  # noqa: E402
import shadow_analytics as sa  # noqa: E402
import record_evaluation as rec  # noqa: E402
from utils import shadow_common as sc  # noqa: E402
import test_issue_251_delta_gate_regret as t251  # noqa: E402  (fixtures only)
import test_issue_262_shadow_residuals as t262  # noqa: E402  (fixtures only)

DELTA_ROW = t262.DELTA_ROW


def read(*parts):
    with open(os.path.join(BASE_DIR, *parts), encoding="utf-8") as f:
        return f.read()


# =============================================================================
# 1. Promotion guard
# =============================================================================
def passing_rows(n=sa.MIN_SAMPLE):
    """n DELTA_GATE rows, one dossier and one symbol each, 5000 s apart, each blocked by a resting entry cancelled
    unfilled (known 0R): n pairs in n clusters, n replay events, every blocker R known, no swap attempted."""
    blocker = dict(t251.RESTING_EXPIRED, notional=100.0)
    return [t251.resolved_row(f"p{i}", f"d{i}", blockers=[blocker], book=[blocker],
                              registered_at_ts=10000 + 5000 * i, resolved_at_ts=12000 + 5000 * i) for i in range(n)]


def passing_reports():
    rows = passing_rows()
    regret = sa.regret_report(rows, t251.ACTIONS, [], [], resamples=50, seed=1)
    replay = sa.replay_policies(rows, sa.build_blocker_index(t251.ACTIONS, [], []))
    return regret, replay


class TestPromotionGuard(unittest.TestCase):

    def test_passing_case(self):
        regret, replay = passing_reports()
        self.assertEqual((regret["conservative"]["n"], regret["conservative"]["n_clusters"]), (30, 30))
        self.assertEqual((replay["n_events"], replay["blocker_r_unknown"]), (30, 0))
        self.assertEqual(sa.promotion_guard(regret, replay), {"admissible": True, "failing": []})
        text = sa.format_delta_gate_report(regret, replay)
        guard = [line for line in text.splitlines() if "PROMOTION GUARD" in line]
        self.assertEqual(len(guard), 1)
        self.assertIn("would be admissible for review", guard[0])
        self.assertIn("advisory", guard[0])
        self.assertNotIn("NOT admissible", text)

    def test_each_failing_condition(self):
        def flip_regret_sample(r, _p):
            r["conservative"]["insufficient_sample"] = True

        def flip_replay_sample(_r, p):
            p["insufficient_sample"] = True

        def flip_clusters(r, _p):
            r["conservative"]["n_clusters"] = 3

        def flip_sign(r, _p):
            r["sensitivity"] = {"unknown_blockers": 1, "by_imputed_r": {}, "sign_changes": True}

        def flip_unknown(_r, p):
            p["blocker_r_unknown"] = 2

        def flip_unchecked(_r, p):
            p["policies"]["swap"]["swap_unchecked"] = 1

        cases = ((flip_regret_sample, "insufficient_sample (regret n=30 < 30)"),
                 (flip_replay_sample, "insufficient_sample (replay events 30 < 30)"),
                 (flip_clusters, "n_clusters 3 < 30"),
                 (flip_sign, "sign_changes true"),
                 (flip_unknown, "blocker_r_unknown 2"),
                 (flip_unchecked, "swap: swap_unchecked 1"))
        base_regret, base_replay = passing_reports()
        for flip, token in cases:
            with self.subTest(token=token):
                regret, replay = copy.deepcopy(base_regret), copy.deepcopy(base_replay)
                flip(regret, replay)
                guard = sa.promotion_guard(regret, replay)
                self.assertFalse(guard["admissible"])
                self.assertEqual(len(guard["failing"]), 1)
                self.assertTrue(guard["failing"][0].startswith(token), guard["failing"])
                line = [ln for ln in sa.format_delta_gate_report(regret, replay).splitlines()
                        if "PROMOTION GUARD" in ln]
                self.assertEqual(len(line), 1)
                self.assertIn("is NOT admissible: " + token, line[0])

    def test_sensitivity_without_sign_change_passes(self):
        regret, replay = passing_reports()
        regret["sensitivity"] = {"unknown_blockers": 1, "by_imputed_r": {}, "sign_changes": False}
        self.assertTrue(sa.promotion_guard(regret, replay)["admissible"])

    def test_real_small_sample_lists_every_failure(self):
        no_notional = {"symbol": "LINKUSDT", "direction": "LONG", "kind": "position", "score": None,
                       "entry_id": "p2", "notional": None, "since_ts": 5000}
        rows = [t262.swap_row(notional=100.0, book=[t262.ZRO, t262.SHORT, no_notional])]
        replay = sa.replay_policies(rows, t262.no_network_index(), swap_margin=10)
        regret = sa.regret_report(rows, [], [], [], resamples=50)
        failing = sa.promotion_guard(regret, replay)["failing"]
        self.assertEqual([f.split(" (")[0] for f in failing],
                         ["insufficient_sample", "insufficient_sample", "n_clusters 0 < 30", "sign_changes true",
                          "blocker_r_unknown 1", "swap: swap_unchecked 1"])
        self.assertIn("is NOT admissible", sa.format_delta_gate_report(regret, replay))

    def test_absent_without_data(self):
        regret = sa.regret_report([], [], [], [])
        replay = sa.replay_policies([], t262.no_network_index())
        self.assertIsNone(sa.promotion_guard(regret, replay))
        self.assertNotIn("PROMOTION GUARD", sa.format_delta_gate_report(regret, replay))

    def test_no_code_path_consumes_it(self):
        users = []
        for root, _dirs, files in os.walk(SCRIPTS_DIR):
            for name in files:
                path = os.path.join(root, name)
                if name.endswith(".py") and path != os.path.join(SCRIPTS_DIR, "shadow_analytics.py"):
                    with open(path, encoding="utf-8", errors="replace") as f:
                        if "promotion_guard" in f.read():
                            users.append(path)
        self.assertEqual(users, [])


# =============================================================================
# 2. Report wording: a 0R tie that splits under an imputation names the pair
# =============================================================================
class TestTieSplitWording(unittest.TestCase):

    def test_split_names_the_tied_pairs(self):
        """F worth 0R: at 0R every policy totals 0 (tie); at -1R the policies keeping ZRO (unknown R) drop to -1
        while swap stays 0: no strict 0R order is reversed, but the tied pairs split."""
        res = sa.replay_policies([t262.swap_row(pnl=0.0)], t262.no_network_index(), resting_age_min=0,
                                 swap_margin=10)
        sens = res["sensitivity"]
        self.assertEqual(sens["total_r"]["0R"], {"current": 0.0, "resting_after_n_min": 0.0,
                                                 "resting_fraction": 0.0, "swap": 0.0})
        self.assertEqual((sens["ranking_changes"], sens["ranking_ties"]), ([], ["-1R"]))
        self.assertEqual(sens["tie_splits"], {"-1R": [["current", "swap"], ["resting_after_n_min", "swap"],
                                                      ["resting_fraction", "swap"]]})
        text = sa.format_delta_gate_report(sa.regret_report([], [], [], []), res)
        self.assertIn("differs at -1R (swap > current > resting_after_n_min > resting_fraction): policies tied at "
                      "0R split there: current = swap, resting_after_n_min = swap, resting_fraction = swap.", text)
        self.assertNotIn("differs only by a tie", text)
        self.assertNotIn("CHANGES at", text)

    def test_tie_only_at_the_imputation_keeps_the_generic_wording(self):
        res = sa.replay_policies([t262.swap_row()], t262.no_network_index(), resting_age_min=0, swap_margin=10)
        self.assertEqual(res["sensitivity"]["tie_splits"], {"+1.8R": []})
        text = sa.format_delta_gate_report(sa.regret_report([], [], [], []), res)
        self.assertIn("differs only by a tie at +1.8R", text)
        self.assertNotIn("split there", text)


# =============================================================================
# 3. Counter visibility: record_evaluation and --loop
# =============================================================================
class StopLoop(Exception):
    pass


class TestCounterVisibility(t251.TrackerBase):

    def test_recorder_prints_deduped_window(self):
        self.brief([t251.opp("FETUSDT")])
        self.dossier([DELTA_ROW], sha="a" * 64)
        st.register_from_evaluation()
        # dossier B: FET deduped by the window, SOL registers
        self.brief([t251.opp("FETUSDT"), t251.opp("SOLUSDT")])
        self.dossier([DELTA_ROW, {"symbol": "SOLUSDT", "direction": "LONG", "gate": "FRICTION"}], sha="b" * 64)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rec._register_shadow()
        self.assertIn("1 candidate(s) enrolled", out.getvalue())
        self.assertIn("(deduped_window 1)", out.getvalue())
        # dossier C: nothing new, FET deduped: the line still shows the counter
        self.brief([t251.opp("FETUSDT")])
        self.dossier([DELTA_ROW], sha="c" * 64)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rec._register_shadow()
        self.assertIn("0 candidate(s) enrolled", out.getvalue())
        self.assertIn("(deduped_window 1)", out.getvalue())
        # dossier A again (FET registered from it): the hash rule, nothing registered or deduped: no line (as before)
        self.dossier([DELTA_ROW], sha="a" * 64)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rec._register_shadow()
        self.assertEqual(out.getvalue(), "")

    def test_loop_prints_deduped_window(self):
        self.brief([t251.opp("FETUSDT")])
        self.dossier([DELTA_ROW], sha="a" * 64)
        st.register_from_evaluation()
        self.dossier([DELTA_ROW], sha="b" * 64)
        out = io.StringIO()
        with patch.object(sys, "argv", ["shadow_tracker.py", "--loop", "--interval", "1"]), \
                patch.object(st, "audit_shadow_trades") as audit, \
                patch.object(st, "print_shadow_dashboard"), \
                patch.object(st.time, "sleep", side_effect=StopLoop), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(StopLoop):
                st.main()
        audit.assert_called_once()
        self.assertIn("Registered 0 candidate(s)", out.getvalue())
        self.assertIn("deduped_window 1", out.getvalue())
        self.assertNotIn("Error in shadow loop", out.getvalue())


# =============================================================================
# 4. Dedupe reference: timestamp_ts first, recorded_at_ts as fallback (distinct values)
# =============================================================================
class TestDedupeReferenceFallback(t251.TrackerBase):

    def stamp(self, **fields):
        with open(st.DOSSIER_FILE, "r", encoding="utf-8") as f:
            record = json.load(f)
        for key in ("timestamp_ts", "recorded_at_ts"):
            record.pop(key, None)
        record.update(fields)
        t251.write_json(st.DOSSIER_FILE, record)

    def test_timestamp_ts_first_then_recorded_at_ts(self):
        self.brief([t251.opp("FETUSDT")])
        self.dossier([DELTA_ROW], sha="a" * 64)
        st.register_from_evaluation()
        rows = st.load_jsonl(st.SHADOW_TRADES_FILE)
        first_ts = rows[0]["registered_at_ts"] - 4000          # the first row is now older than the window
        t251.write_jsonl(st.SHADOW_TRADES_FILE, [dict(rows[0], registered_at_ts=first_ts)])
        inside, outside = first_ts + 600, int(time.time())
        stats = {}
        # timestamp_ts inside the window, recorded_at_ts outside: timestamp_ts wins -> deduped
        self.dossier([DELTA_ROW], sha="b" * 64)
        self.stamp(timestamp_ts=inside, recorded_at_ts=outside)
        self.assertEqual(st.register_from_evaluation(stats), 0)
        self.assertEqual(stats, {"deduped_window": 1})
        # no timestamp_ts: the fallback recorded_at_ts (inside) -> deduped
        self.dossier([DELTA_ROW], sha="c" * 64)
        self.stamp(recorded_at_ts=inside)
        self.assertEqual(st.register_from_evaluation(stats), 0)
        self.assertEqual(stats, {"deduped_window": 2})
        # timestamp_ts outside, recorded_at_ts inside: timestamp_ts wins -> registers
        self.dossier([DELTA_ROW], sha="d" * 64)
        self.stamp(timestamp_ts=outside, recorded_at_ts=inside)
        self.assertEqual(st.register_from_evaluation(stats), 1)
        self.assertEqual(stats, {"deduped_window": 2})


# =============================================================================
# 5. Enum-parity helper: clear failure message on wording drift
# =============================================================================
class TestEnumParityMessage(unittest.TestCase):

    def test_missing_start_phrase_names_it(self):
        with self.assertRaises(AssertionError) as ctx:
            t262.TestSingleSources.between("a reworded prompt", "`gate`: the first failing check, one of",
                                           "K5 squeeze risk", "agent.md")
        self.assertIn("agent.md: phrase '`gate`: the first failing check, one of' not found", str(ctx.exception))

    def test_missing_end_phrase_names_it(self):
        with self.assertRaises(AssertionError) as ctx:
            t262.TestSingleSources.between("`gate` from a fixed enum: `OTHER`", "`gate` from a fixed enum:", ";",
                                           "guide.md")
        self.assertIn("guide.md: phrase ';' not found after", str(ctx.exception))

    def test_present_phrases_return_the_clause(self):
        self.assertEqual(t262.TestSingleSources.between("x START a, b END y", "START", "END", "s"), " a, b ")


# =============================================================================
# 6. Shared definitions: re-exports and a single definition site
# =============================================================================
class TestSharedDefinitions(unittest.TestCase):

    def test_re_exports_keep_every_name(self):
        for name in ("GATE_ENUM", "GATE_FALLBACK", "POST_APPROVAL_GATE", "BLOCKER_GATES", "DEDUPE_WINDOW_SECONDS",
                     "row_gate"):
            self.assertIs(getattr(st, name), getattr(sc, name), name)
        self.assertIs(sa.row_gate, sc.row_gate)
        self.assertEqual(sa.CLUSTER_WINDOW_SECONDS, sc.DEDUPE_WINDOW_SECONDS)
        self.assertEqual(st.DEDUPE_WINDOW_SECONDS, 3600)
        self.assertIn("DELTA_GATE", st.GATE_ENUM)
        self.assertEqual(st.row_gate({"rejection_category": "DRY_VOLUME_FAKE_TIER_S"}),
                         ("DRY_VOLUME", "legacy_category"))

    def test_single_definition_site(self):
        common = read("scripts", "utils", "shadow_common.py")
        for pattern in (r"^GATE_ENUM =", r"^GATE_FALLBACK =", r"^POST_APPROVAL_GATE =", r"^BLOCKER_GATES =",
                        r"^DEDUPE_WINDOW_SECONDS =", r"^def row_gate\("):
            self.assertEqual(len(re.findall(pattern, common, re.M)), 1, pattern)
            for script in ("shadow_tracker.py", "shadow_analytics.py"):
                self.assertEqual(re.findall(pattern, read("scripts", script), re.M), [], (script, pattern))

    def test_analytics_loads_without_the_tracker(self):
        self.assertNotRegex(read("scripts", "shadow_analytics.py"), r"(?m)^\s*(from|import) shadow_tracker")
        spec = importlib.util.spec_from_file_location("shadow_analytics_isolated_290",
                                                      os.path.join(SCRIPTS_DIR, "shadow_analytics.py"))
        mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"shadow_tracker": None}):
            spec.loader.exec_module(mod)
        self.assertIs(mod.row_gate, sc.row_gate)


if __name__ == "__main__":
    unittest.main()
