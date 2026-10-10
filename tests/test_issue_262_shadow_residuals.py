#!/usr/bin/env python3
"""
test_issue_262_shadow_residuals.py - shadow delta-gate regret / replay residuals after #251 (issue #262).

Covers the (symbol, direction, gate) 3600 s registration window across dossiers (deduped_window counter, hook
denial vs dossier rejection, hash idempotency after resolution, legacy rows), the bootstrap clusters linking
dossier and symbol/direction/time window, the MCP sensitivity blocks (unknown blocker R at -1R / 0R / +1.8R, ranking
change line), the swap's delta-gate re-check (swap_blocked / placed / swap_unchecked) and the resting-weight warning,
blocker R dated at its exit (blocker_time_approx otherwise), book items without entry_id kept apart across time,
row_gate defined once, the gate enum in sync across the evaluator prompt, the docs and shadow_tracker.GATE_ENUM.
Hermetic: temp directories only (every shadow_tracker path redirected by TrackerBase), no Binance client, network
blocked.
"""

import io
import os
import re
import sys
import json
import time
import unittest
import contextlib
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (SCRIPTS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import shadow_tracker as st  # noqa: E402
import shadow_analytics as sa  # noqa: E402
import test_issue_251_delta_gate_regret as t251  # noqa: E402  (fixtures only)

DELTA_ROW = {"symbol": "FETUSDT", "direction": "LONG", "gate": "DELTA_GATE", "detail": "K1 BLOCKED: LONG_HEAVY"}


def no_network_index():
    return sa.build_blocker_index([], [], [])


# =============================================================================
# 1. Registration: window dedupe across dossiers
# =============================================================================
class TestWindowDedupe(t251.TrackerBase):

    def age_rows(self, path, seconds):
        rows = st.load_jsonl(path)
        t251.write_jsonl(path, [dict(r, registered_at_ts=r["registered_at_ts"] - seconds) for r in rows])

    def test_new_dossier_within_window_registers_once_then_again_after_it(self):
        self.brief([t251.opp("FETUSDT", confidence=85)])
        self.dossier([DELTA_ROW], sha="a" * 64)
        stats = {}
        self.assertEqual(st.register_from_evaluation(stats), 1)
        self.dossier([DELTA_ROW], sha="b" * 64)
        self.assertEqual(st.register_from_evaluation(stats), 0)
        self.dossier([DELTA_ROW], sha="c" * 64)
        self.assertEqual(st.register_from_evaluation(stats), 0)
        self.assertEqual(stats, {"deduped_window": 2})
        self.assertEqual(len(st.load_jsonl(st.SHADOW_TRADES_FILE)), 1)
        # the first row is now older than the window: a new dossier registers again
        self.age_rows(st.SHADOW_TRADES_FILE, st.DEDUPE_WINDOW_SECONDS + 1)
        self.dossier([DELTA_ROW], sha="d" * 64)
        self.assertEqual(st.register_from_evaluation(stats), 1)
        self.assertEqual(stats, {"deduped_window": 2})

    def stamp_dossier(self, ts):
        with open(st.DOSSIER_FILE, "r", encoding="utf-8") as f:
            record = json.load(f)
        record["timestamp_ts"] = record["recorded_at_ts"] = ts
        t251.write_json(st.DOSSIER_FILE, record)

    def test_rerun_of_a_deduped_dossier_after_the_window_stays_deduped(self):
        """Audit round 1: the window is measured from the dossier's own time, not from the wall clock, so the
        latest dossier re-read by --loop / a repeated --register-from-eval more than 3600 s later registers nothing."""
        self.brief([t251.opp("FETUSDT")])
        self.dossier([DELTA_ROW], sha="a" * 64)
        st.register_from_evaluation()
        first_ts = st.load_jsonl(st.SHADOW_TRADES_FILE)[0]["registered_at_ts"]
        # the first row is now more than the window old; dossier B was recorded 600 s after it
        self.age_rows(st.SHADOW_TRADES_FILE, 4000)
        self.dossier([DELTA_ROW], sha="b" * 64)
        self.stamp_dossier(first_ts - 4000 + 600)
        stats = {}
        self.assertEqual(st.register_from_evaluation(stats), 0)
        self.assertEqual(st.register_from_evaluation(stats), 0)     # the rerun: still nothing
        self.assertEqual(stats, {"deduped_window": 2})
        self.assertEqual(len(st.load_jsonl(st.SHADOW_TRADES_FILE)), 1)
        # a dossier recorded now (outside the window of the first row) registers, at the current time
        self.dossier([DELTA_ROW], sha="c" * 64)
        self.stamp_dossier(int(time.time()))
        self.assertEqual(st.register_from_evaluation(stats), 1)
        self.assertAlmostEqual(st.load_jsonl(st.SHADOW_TRADES_FILE)[-1]["registered_at_ts"], time.time(), delta=5)

    def test_resolved_row_within_window_also_dedupes(self):
        self.brief([t251.opp("FETUSDT")])
        self.dossier([DELTA_ROW], sha="a" * 64)
        st.register_from_evaluation()
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, [dict(r, status="RESOLVED")
                                                   for r in st.load_jsonl(st.SHADOW_TRADES_FILE)])
        t251.write_jsonl(st.SHADOW_TRADES_FILE, [])
        self.dossier([DELTA_ROW], sha="b" * 64)
        stats = {}
        self.assertEqual(st.register_from_evaluation(stats), 0)
        self.assertEqual(stats, {"deduped_window": 1})

    def test_hash_idempotency_holds_after_resolution_outside_the_window(self):
        self.brief([t251.opp("FETUSDT")])
        self.dossier([DELTA_ROW], sha="a" * 64)
        st.register_from_evaluation()
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, [dict(r, status="RESOLVED")
                                                   for r in st.load_jsonl(st.SHADOW_TRADES_FILE)])
        t251.write_jsonl(st.SHADOW_TRADES_FILE, [])
        self.age_rows(st.SHADOW_RESOLVED_FILE, 2 * st.DEDUPE_WINDOW_SECONDS)
        stats = {}
        self.assertEqual(st.register_from_evaluation(stats), 0)   # same dossier hash: never twice
        self.assertEqual(stats, {})                               # the hash rule, not the window

    def test_other_gate_or_direction_registers(self):
        self.brief([t251.opp("FETUSDT"), t251.opp("SOLUSDT", "SHORT")])
        self.dossier([DELTA_ROW, {"symbol": "SOLUSDT", "direction": "SHORT", "gate": "MACRO_SHORT"}], sha="a" * 64)
        self.assertEqual(st.register_from_evaluation(), 2)
        self.brief([t251.opp("FETUSDT"), t251.opp("SOLUSDT", "LONG")])
        self.dossier([{"symbol": "FETUSDT", "direction": "LONG", "gate": "FRICTION"},
                      {"symbol": "SOLUSDT", "direction": "LONG", "gate": "MACRO_SHORT"}], sha="b" * 64)
        stats = {}
        self.assertEqual(st.register_from_evaluation(stats), 2)   # new gate for FET, new direction for SOL
        self.assertEqual(stats, {})

    def test_hook_denial_and_dossier_rejection_of_one_symbol_both_register(self):
        now = int(time.time())
        ev = {"ts": now - 600, "env": "prod", "gate": st.POST_APPROVAL_GATE, "symbol": "FETUSDT", "direction": "LONG",
              "score": 85, "dossier_sha256": "e" * 64, "entry": 10.0, "stop_loss": 9.5, "tp1": 10.9, "tp2": 12.0,
              "source": "session_state_cache", "delta_bias": "LONG_HEAVY", "book": []}
        with open(st.GATE_DENIALS_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
        self.brief([t251.opp("FETUSDT")])
        self.dossier([DELTA_ROW], sha="a" * 64)
        stats = {}
        self.assertEqual(st.register_from_evaluation(stats), 2)
        self.assertEqual(sorted(r["gate"] for r in st.load_jsonl(st.SHADOW_TRADES_FILE)),
                         ["DELTA_GATE", st.POST_APPROVAL_GATE])
        self.assertEqual(stats, {})
        # a second hook denial of the same candidate from another dossier inside the window is deduped
        with open(st.GATE_DENIALS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(ev, ts=now - 300, dossier_sha256="f" * 64)) + "\n")
        self.assertEqual(st.register_from_gate_denials(stats=stats), 0)
        self.assertEqual(stats, {"deduped_window": 1})

    def test_legacy_rows_still_parse_and_take_part(self):
        now = int(time.time())
        t251.write_jsonl(st.SHADOW_TRADES_FILE, [
            {"id": "old1", "symbol": "FETUSDT", "direction": "LONG", "registered_at_ts": now - 60,
             "status": "PENDING_TRIGGER", "rejection_category": "DELTA_GATE_OR_MACRO"},          # legacy: OTHER
            {"id": "old2", "symbol": "TRXUSDT", "direction": "LONG", "registered_at_ts": now - 60,
             "status": "PENDING_TRIGGER", "rejection_category": "DRY_VOLUME_FAKE_TIER_S"},       # legacy: DRY_VOLUME
            {"id": "old3", "symbol": "XRPUSDT", "status": "PENDING_TRIGGER"},                     # no time at all
        ])
        self.brief([t251.opp("FETUSDT"), t251.opp("TRXUSDT", vol=0.4), t251.opp("XRPUSDT")])
        self.dossier([DELTA_ROW, {"symbol": "TRXUSDT", "direction": "LONG", "gate": "DRY_VOLUME"},
                      {"symbol": "XRPUSDT", "direction": "LONG", "gate": "DELTA_GATE"}], sha="a" * 64)
        stats = {}
        self.assertEqual(st.register_from_evaluation(stats), 2)   # FET (OTHER != DELTA_GATE) and XRP
        self.assertEqual(stats, {"deduped_window": 1})            # TRX: the legacy row maps to DRY_VOLUME
        self.assertEqual(st.calculate_efficacy_metrics()["active_shadow_trades"], 5)

    def test_rows_without_hash_keep_the_symbol_rule(self):
        stats = {}
        self.assertIsNotNone(st.register_shadow_trade("ABCUSDT", "LONG", 1.0, 0.9, 1.1, 1.2, 1.0, stats=stats))
        self.assertIsNone(st.register_shadow_trade("ABCUSDT", "SHORT", 1.0, 1.1, 0.9, 0.8, 1.0, stats=stats))
        self.assertEqual(stats, {})

    def test_cli_prints_the_counter(self):
        self.brief([t251.opp("FETUSDT")])
        self.dossier([DELTA_ROW], sha="a" * 64)
        st.register_from_evaluation()
        self.dossier([DELTA_ROW], sha="b" * 64)
        out = io.StringIO()
        with patch.object(sys, "argv", ["shadow_tracker.py", "--register-from-eval"]), \
                contextlib.redirect_stdout(out):
            st.main()
        self.assertIn("Registered 0 candidate(s)", out.getvalue())
        self.assertIn("deduped_window 1", out.getvalue())


# =============================================================================
# 2. Bootstrap clusters: dossier AND (symbol, direction, window)
# =============================================================================
class TestClusters(unittest.TestCase):

    def test_same_candidate_across_dossiers_is_one_cluster(self):
        rows = [t251.resolved_row("a", "d1", registered_at_ts=10000, blockers=[t251.RESTING_EXPIRED]),
                t251.resolved_row("a", "d2", registered_at_ts=10600, blockers=[t251.RESTING_EXPIRED]),
                t251.resolved_row("a", "d3", registered_at_ts=20000, blockers=[t251.RESTING_EXPIRED]),
                t251.resolved_row("b", "d3", registered_at_ts=20000, blockers=[t251.RESTING_EXPIRED]),
                t251.resolved_row("c", "d4", registered_at_ts=10000, blockers=[t251.RESTING_EXPIRED])]
        self.assertEqual(sa.row_clusters(rows), ["d1", "d1", "d3", "d3", "d4"])
        rep = sa.regret_report(rows, t251.ACTIONS, [], [], resamples=50, seed=1)
        # 5 pairs; before #262 every dossier was its own cluster (4)
        self.assertEqual((rep["conservative"]["n"], rep["conservative"]["n_clusters"]), (5, 3))

    def test_window_chains_and_other_direction_stays_apart(self):
        rows = [t251.resolved_row("a", "d1", registered_at_ts=0),
                t251.resolved_row("a", "d2", registered_at_ts=3000),
                t251.resolved_row("a", "d3", registered_at_ts=6000),
                t251.resolved_row("a", "d4", registered_at_ts=6000, direction="SHORT")]
        self.assertEqual(sa.row_clusters(rows), ["d1", "d1", "d1", "d4"])

    def test_rows_without_time_cluster_by_dossier_or_id(self):
        rows = [{"id": "x", "symbol": "A", "direction": "LONG", "dossier_sha256": "d1"},
                {"id": "y", "symbol": "A", "direction": "LONG", "dossier_sha256": "d1"},
                {"id": "z", "symbol": "A", "direction": "LONG"}]
        self.assertEqual(sa.row_clusters(rows), ["d1", "d1", "z"])
        self.assertEqual(sa.row_clusters([]), [])


# =============================================================================
# 3. MCP sensitivity
# =============================================================================
ZRO = {"symbol": "ZROUSDT", "direction": "LONG", "kind": "resting", "score": 80.0, "entry_id": "z1",
       "notional": 100.0, "since_ts": 9400}
SHORT = {"symbol": "OPUSDT", "direction": "SHORT", "kind": "position", "score": None, "entry_id": "p1",
         "notional": 20.0, "since_ts": 5000}


def swap_row(notional=20.0, book=None, pnl=2.7, **extra):
    """F (score 95, +1.8R) blocked by ZRO (resting, score 80) on the book ZRO LONG 100 + SHORT 20 (LONG_HEAVY)."""
    return t251.resolved_row("f", "d1", pnl=pnl, score=95, blockers=[ZRO], book=book or [ZRO, SHORT],
                             notional_usdt=notional, **extra)


class TestSensitivity(unittest.TestCase):

    def test_regret_block_with_unknown_blockers(self):
        rows = [t251.resolved_row("a", "d1", pnl=2.7, blockers=[t251.RESTING_EXPIRED]),                 # 1.8 - 0
                t251.resolved_row("e", "d2", "TRUE_NEGATIVE", pnl=-1.5, blockers=[t251.RESTING_UNKNOWN])]  # -1 - ?
        rep = sa.regret_report(rows, t251.ACTIONS, t251.OUTCOMES, t251.AUDIT, resamples=50, seed=1)
        self.assertEqual((rep["conservative"]["n"], rep["conservative"]["mean_regret_r"]), (1, 1.8))  # headline
        sens = rep["sensitivity"]
        self.assertEqual(sens["unknown_blockers"], 1)
        self.assertEqual(list(sens["by_imputed_r"]), ["-1R", "0R", "+1.8R"])
        self.assertEqual({k: v["mean_regret_r"] for k, v in sens["by_imputed_r"].items()},
                         {"-1R": 0.9, "0R": 0.4, "+1.8R": -0.5})
        self.assertEqual({k: v["n"] for k, v in sens["by_imputed_r"].items()}, {"-1R": 2, "0R": 2, "+1.8R": 2})
        self.assertTrue(sens["sign_changes"])
        text = sa.format_delta_gate_report(rep, sa.replay_policies(rows, no_network_index()))
        self.assertIn("excludes the 1 blocker(s) with unknown R", text)
        for label in ("unknown blocker R = -1R", "unknown blocker R = 0R", "unknown blocker R = +1.8R"):
            self.assertIn(label, text)
        self.assertIn("CHANGES with the imputed blocker R", text)

    def test_regret_block_absent_when_every_blocker_is_known(self):
        rows = [t251.resolved_row("a", "d1", pnl=2.7, blockers=[t251.RESTING_EXPIRED])]
        rep = sa.regret_report(rows, t251.ACTIONS, t251.OUTCOMES, t251.AUDIT, resamples=50, seed=1)
        self.assertIsNone(rep["sensitivity"])
        self.assertNotIn("Sensitivity", sa.format_delta_gate_report(rep, sa.replay_policies(rows, no_network_index())))

    def test_replay_block_and_ranking_tie(self):
        """ZRO's outcome is unknown (MCP: no trade_outcomes). swap cancels it and places F (+1.8R); every other
        policy keeps it: at 0R and -1R swap ranks first, at +1.8R it ties the others (a tie, not a reversal)."""
        res = sa.replay_policies([swap_row()], no_network_index(), resting_age_min=0, swap_margin=10)
        self.assertEqual((res["blocker_r_unknown"], res["unknown_blocker_r"]), (1, 0.0))
        self.assertEqual({k: v["total_r"] for k, v in res["policies"].items()},
                         {"current": 0.0, "resting_after_n_min": 0.0, "resting_fraction": 0.0, "swap": 1.8})
        sens = res["sensitivity"]
        self.assertEqual(sens["total_r"]["-1R"], {"current": -1.0, "resting_after_n_min": -1.0,
                                                  "resting_fraction": -1.0, "swap": 1.8})
        self.assertEqual(sens["total_r"]["+1.8R"], {"current": 1.8, "resting_after_n_min": 1.8,
                                                    "resting_fraction": 1.8, "swap": 1.8})
        self.assertEqual(sens["ranking"]["0R"][0], "swap")
        self.assertEqual(sens["ranking"]["+1.8R"][-1], "swap")
        self.assertEqual((sens["ranking_changes"], sens["ranking_ties"]), ([], ["+1.8R"]))
        rep = sa.regret_report([swap_row()], [], [], [], resamples=50)
        text = sa.format_delta_gate_report(rep, res)
        self.assertIn("Headline total R counts the 1 blocker(s) with unknown R at 0R", text)
        self.assertIn("unknown = 0R    ", text)
        self.assertIn("(headline)", text)
        self.assertIn("differs only by a tie at +1.8R", text)
        self.assertNotIn("CHANGES at", text)

    def test_replay_ranking_reversal(self):
        """Same book, F worth +1.0R: at +1.8R the policies keeping ZRO strictly beat swap (a real change)."""
        res = sa.replay_policies([swap_row(pnl=1.5)], no_network_index(), resting_age_min=0, swap_margin=10)
        sens = res["sensitivity"]
        self.assertEqual(sens["total_r"]["0R"]["swap"], 1.0)
        self.assertEqual((sens["ranking_changes"], sens["ranking_ties"]), (["+1.8R"], []))
        self.assertEqual(sens["ranking"]["+1.8R"], ["current", "resting_after_n_min", "resting_fraction", "swap"])
        text = sa.format_delta_gate_report(sa.regret_report([], [], [], []), res)
        self.assertIn("CHANGES at +1.8R", text)
        self.assertNotIn("differs only by a tie", text)

    def test_replay_block_absent_when_every_blocker_is_known(self):
        index = sa.build_blocker_index([], [{"symbol": "ZROUSDT", "audit_ts": 9900.0, "status": "closed",
                                             "realized_r_net": 0.5}],
                                       [{"symbol": "ZROUSDT", "entry_order_id": "z1", "timestamp": 9900}])
        res = sa.replay_policies([swap_row()], index, resting_age_min=0, swap_margin=10)
        self.assertEqual(res["blocker_r_unknown"], 0)
        self.assertIsNone(res["sensitivity"])
        rep = sa.regret_report([swap_row()], [], [{"symbol": "ZROUSDT", "audit_ts": 9900.0, "status": "closed",
                                                   "realized_r_net": 0.5}],
                               [{"symbol": "ZROUSDT", "entry_order_id": "z1", "timestamp": 9900}], resamples=50)
        text = sa.format_delta_gate_report(rep, res)
        self.assertNotIn("Sensitivity", text)
        self.assertNotIn("Headline total R counts", text)


# =============================================================================
# 4. Swap re-check of the delta gate
# =============================================================================
class TestSwapRecheck(unittest.TestCase):

    def test_swap_that_would_itself_be_denied_is_blocked(self):
        res = sa.replay_policies([swap_row(notional=100.0)], no_network_index(), swap_margin=10)
        s = res["policies"]["swap"]
        self.assertEqual((s["placed"], s["swapped"], s["swap_blocked"], s["swap_unchecked"]), (0, 0, 1, 0))
        # nothing cancelled: the book stays LONG_HEAVY as under current
        self.assertEqual(s["exposure"]["series"], res["policies"]["current"]["exposure"]["series"])
        text = sa.format_delta_gate_report(sa.regret_report([], [], [], []), res)
        self.assertIn("swap_blocked 1 | swap_unchecked 0", text)

    def test_permitted_swap_is_placed(self):
        res = sa.replay_policies([swap_row(notional=20.0)], no_network_index(), swap_margin=10)
        s = res["policies"]["swap"]
        self.assertEqual((s["placed"], s["swapped"], s["swap_blocked"], s["swap_unchecked"]), (1, 1, 0, 0))
        self.assertEqual(s["exposure"]["series"][0]["delta_bias"], "DELTA_BALANCED")   # LONG 20 vs SHORT 20

    def test_book_item_without_notional_is_unchecked(self):
        no_notional = {"symbol": "LINKUSDT", "direction": "LONG", "kind": "position", "score": None,
                       "entry_id": "p2", "notional": None, "since_ts": 5000}
        res = sa.replay_policies([swap_row(notional=100.0, book=[ZRO, SHORT, no_notional])], no_network_index(),
                                 swap_margin=10)
        s = res["policies"]["swap"]
        self.assertEqual((s["placed"], s["swapped"], s["swap_blocked"], s["swap_unchecked"]), (1, 1, 0, 1))

    def test_resting_weight_warning_and_model_note(self):
        res = sa.replay_policies([swap_row()], no_network_index())
        self.assertIn(sa.RESTING_WEIGHT_WARNING, res["warnings"])
        self.assertIn("NOT faithful", sa.RESTING_WEIGHT_WARNING)
        self.assertIn("full weight", sa.REPLAY_MODEL_NOTE)
        self.assertIn("swap_blocked", sa.REPLAY_MODEL_NOTE)
        self.assertNotIn("does not re-check", sa.REPLAY_MODEL_NOTE)
        text = sa.format_delta_gate_report(sa.regret_report([], [], [], []), res)
        self.assertIn("! " + sa.RESTING_WEIGHT_WARNING, text)


# =============================================================================
# 5. Blocker timing and identity
# =============================================================================
def position(symbol, audit_ts, entry_id=None, since_ts=None):
    return {"symbol": symbol, "direction": "LONG", "kind": "position", "score": None, "entry_id": entry_id,
            "notional": 50.0, "since_ts": since_ts, "audit_ts": audit_ts}


class TestBlockerTiming(unittest.TestCase):
    """Two events (ts 10000 and 20000), each with a -1R and a +1R blocker. Dated at the first event the cumulative
    R is -1, 0, -1, 0 (max drawdown 1); dated at the exits (+1R ones close first) it is +1, +2, +1, 0 (drawdown 2)."""

    def rows(self):
        b1, b2 = position("AAAUSDT", 1001, "1"), position("BBBUSDT", 1002, "2")
        b3, b4 = position("CCCUSDT", 1003, "3"), position("DDDUSDT", 1004, "4")
        return [t251.resolved_row("x", "d1", blockers=[b1, b2], book=[b1, b2], registered_at_ts=10000),
                t251.resolved_row("y", "d2", blockers=[b3, b4], book=[b3, b4], registered_at_ts=20000)]

    @staticmethod
    def outcomes(with_exit):
        out = []
        for sym, ts, r, exit_s in (("AAAUSDT", 1001, -1.0, 30000), ("BBBUSDT", 1002, 1.0, 11000),
                                   ("CCCUSDT", 1003, -1.0, 31000), ("DDDUSDT", 1004, 1.0, 21000)):
            row = {"symbol": sym, "audit_ts": float(ts), "status": "closed", "realized_r_net": r}
            if with_exit:
                row["exit_ts"] = exit_s * 1000   # trade_outcomes exit_ts is in ms
            out.append(row)
        return out

    def test_exit_dated_vs_approximate(self):
        exact = sa.replay_policies(self.rows(), sa.build_blocker_index([], self.outcomes(True), []))
        approx = sa.replay_policies(self.rows(), sa.build_blocker_index([], self.outcomes(False), []))
        self.assertEqual((exact["blocker_time_approx"], approx["blocker_time_approx"]), (0, 4))
        self.assertEqual(exact["policies"]["current"]["total_r"], approx["policies"]["current"]["total_r"])
        self.assertEqual((exact["policies"]["current"]["max_drawdown_r"],
                          approx["policies"]["current"]["max_drawdown_r"]), (2.0, 1.0))
        text = sa.format_delta_gate_report(sa.regret_report([], [], [], []), approx)
        self.assertIn("blocker_time_approx 4", text)

    def test_blocker_exit_ts(self):
        index = sa.build_blocker_index([], self.outcomes(True) + [
            {"symbol": "EEEUSDT", "audit_ts": 1005.0, "status": "open", "exit_ts": None}], [])
        self.assertEqual(sa.blocker_exit_ts(position("BBBUSDT", 1002), index), 11000.0)
        self.assertIsNone(sa.blocker_exit_ts(position("EEEUSDT", 1005), index))
        self.assertIsNone(sa.blocker_exit_ts(position("ZZZUSDT", 999), index))

    def test_items_without_entry_id_do_not_collapse_across_time(self):
        early, late = position("SUIUSDT", 1000, since_ts=5000), position("SUIUSDT", 2000, since_ts=15000)
        index = sa.build_blocker_index([], [
            {"symbol": "SUIUSDT", "audit_ts": 1000.0, "status": "closed", "realized_r_net": 0.5},
            {"symbol": "SUIUSDT", "audit_ts": 2000.0, "status": "closed", "realized_r_net": -1.0}], [])
        rows = [t251.resolved_row("x", "d1", blockers=[early], book=[early], registered_at_ts=10000),
                t251.resolved_row("y", "d2", blockers=[late], book=[late], registered_at_ts=20000)]
        res = sa.replay_policies(rows, index)
        self.assertEqual(res["blockers"], 2)            # one key per symbol/direction/kind before #262
        self.assertEqual(res["policies"]["current"]["total_r"], -0.5)
        # without since_ts their audit_ts tells them apart
        rows_no_since = [dict(r, blockers=[dict(b, since_ts=None) for b in r["blockers"]]) for r in rows]
        self.assertEqual(sa.replay_policies(rows_no_since, index)["blockers"], 2)

    def test_same_item_without_entry_id_or_since_ts_counts_once(self):
        """Audit round 1: one position (no entry_id, no since_ts: MCP / manual positions) blocking two events is one
        trade, keyed by its audit_ts; its R counts once (not once per event)."""
        sui = position("SUIUSDT", 1000)
        index = sa.build_blocker_index([], [{"symbol": "SUIUSDT", "audit_ts": 1000.0, "status": "closed",
                                             "realized_r_net": 0.5}], [])
        rows = [t251.resolved_row("x", "d1", blockers=[sui], book=[sui], registered_at_ts=10000),
                t251.resolved_row("y", "d2", blockers=[dict(sui)], book=[dict(sui)], registered_at_ts=20000)]
        res = sa.replay_policies(rows, index)
        self.assertEqual((res["blockers"], res["policies"]["current"]["total_r"]), (1, 0.5))
        # unknown outcome (MCP): one unknown blocker, imputed once in the sensitivity
        mcp = sa.replay_policies(rows, no_network_index())
        self.assertEqual((mcp["blockers"], mcp["blocker_r_unknown"]), (1, 1))
        self.assertEqual(mcp["sensitivity"]["total_r"]["+1.8R"]["current"], 1.8)
        # neither since_ts nor audit_ts: the time cannot be checked, the pre-#262 collapsed key (counted once)
        bare = [dict(r, blockers=[dict(sui, audit_ts=None)]) for r in rows]
        self.assertEqual(sa.replay_policies(bare, no_network_index())["blockers"], 1)

    def test_items_with_entry_id_keep_one_trade_identity(self):
        resting = dict(ZRO)
        filled = dict(ZRO, kind="position", since_ts=15000, audit_ts=9900)
        rows = [t251.resolved_row("x", "d1", blockers=[resting], book=[resting, SHORT], registered_at_ts=10000),
                t251.resolved_row("y", "d2", blockers=[filled], book=[filled, SHORT], registered_at_ts=20000)]
        self.assertEqual(sa.replay_policies(rows, no_network_index())["blockers"], 1)


# =============================================================================
# 6. Single sources: row_gate and the gate enum
# =============================================================================
class TestSingleSources(unittest.TestCase):

    @staticmethod
    def read(*parts):
        with open(os.path.join(BASE_DIR, *parts), encoding="utf-8") as f:
            return f.read()

    def test_row_gate_defined_once(self):
        """Issue #290: one definition, in utils/shadow_common.py (re-exported by shadow_tracker)."""
        self.assertIs(sa.row_gate, st.row_gate)
        self.assertNotIn("def row_gate", self.read("scripts", "shadow_analytics.py"))
        self.assertNotIn("def row_gate", self.read("scripts", "shadow_tracker.py"))
        self.assertEqual(self.read("scripts", "utils", "shadow_common.py").count("def row_gate"), 1)

    @staticmethod
    def between(text, start, end, source):
        """text between start and the next end; an AssertionError naming the missing phrase when the wording of
        source drifted (issue #290: not an IndexError)."""
        if start not in text:
            raise AssertionError(f"{source}: phrase {start!r} not found (wording changed? update this test)")
        rest = text.split(start, 1)[1]
        if end not in rest:
            raise AssertionError(f"{source}: phrase {end!r} not found after {start!r} (wording changed? update "
                                 f"this test)")
        return rest.split(end, 1)[0]

    def test_gate_enum_matches_prompt_docs_and_tracker(self):
        prompt = self.read(".agents", "agents", "isolated_market_evaluator", "agent.md")
        sentence = self.between(prompt, "`gate`: the first failing check, one of", "K5 squeeze risk",
                                "isolated_market_evaluator/agent.md")
        prompt_enum = re.findall(r'`"([A-Z_]+)"`', sentence)
        docs = self.read("docs", "agent_prompt_engineering_guide.md")
        clause = self.between(docs, "`gate` from a fixed enum:", ";", "docs/agent_prompt_engineering_guide.md")
        docs_enum = re.findall(r"`([A-Z_]+)`", clause)
        evaluator_enum = [g for g in st.GATE_ENUM if g != st.POST_APPROVAL_GATE]
        self.assertEqual(len(prompt_enum), len(set(prompt_enum)))
        self.assertEqual(set(prompt_enum), set(evaluator_enum))
        self.assertEqual(sorted(docs_enum), sorted(prompt_enum))
        # the hook-only gate is never offered to the evaluator
        self.assertIn(st.POST_APPROVAL_GATE, st.GATE_ENUM)
        self.assertNotIn(st.POST_APPROVAL_GATE, sentence)
        self.assertNotIn(st.POST_APPROVAL_GATE, clause)


if __name__ == "__main__":
    unittest.main()
