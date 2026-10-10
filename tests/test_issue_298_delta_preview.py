#!/usr/bin/env python3
"""
test_issue_298_delta_preview.py - Issue #298 (run B2): the advisory post-trade delta preview of approved candidates
(item 4, utils/delta_fit.py and record_evaluation._print_summary) and the timestamps (item 7: the recorder's brief /
evaluated / valid-until times, recheck_brief.recheck_summary, the executor's resting-entry result).

The issue's numbers: BR LONG ~21 USDT resting; PUMP SHORT ~30 -> (21-30)/51 = -0.18 fits; XPL SHORT ~49 ->
(21-49)/70 = -0.40 SHORT_HEAVY, blocked by the resting BR entry; PUMP + XPL -> (21-79)/100 = -0.58.

Hermetic: temp workspaces, fake agy transcripts (fixtures of test_issue_267_recheck), the executor on the fake
exchange of test_pending_entries (send_signed_request and load_env faked), urllib and sockets blocked, no Binance
client and no .env read (explicit --env prod / testnet everywhere).
"""

import io
import json
import os
import socket
import sys
import time
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
from utils import delta_fit as df  # noqa: E402
from utils.portfolio_exposure import book_exposure  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
from utils import recheck_brief as rcb  # noqa: E402
import test_issue_267_recheck as t267  # noqa: E402  (fixtures only; its TestCases are not re-exported)
import test_pending_entries as tpe  # noqa: E402  (fixtures only)

NOW = 1_800_000_000
BR_REC = {"kind": "LIMIT", "entry_id": "111", "symbol": "BRUSDT", "direction": "LONG", "target_env": "prod",
          "trigger_or_limit_price": 0.3, "total_qty": 70.0, "placed_at_ts": NOW - 600, "expires_at_ts": NOW + 4800,
          "score_meta": {"score": 72}}
BR_LISTED = {"symbol": "BRUSDT", "dir": "LONG", "kind": "LIMIT"}


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def state(now=NOW, env="prod", long_n=0.0, short_n=0.0, listed=(BR_LISTED,), positions=(), mismatches=(),
          **extra):
    """A fresh valid state; delta_bias (positions only) derived from the notionals, as the sync writes it;
    `positions`: (symbol, direction) of the active_positions; `mismatches`: resting_mismatches rows
    ({"symbol", "entry_id", "side"}, the shape of sync_session_state.resting_entry_exposure)."""
    exp = {"long_notional_usdt": long_n, "short_notional_usdt": short_n,
           "delta_bias": book_exposure(long_n, short_n)["delta_bias"],
           "delta_bias_incl_resting": "LONG_HEAVY", "resting_entries": [dict(e) for e in listed],
           "resting_mismatches": [dict(m) for m in mismatches]}
    return dict({"is_valid": True, "last_updated_ts": now - 30, "target_env": env, "portfolio_exposure": exp,
                 "active_positions": [{"symbol": s, "direction": d} for s, d in positions]}, **extra)


def cand(symbol, direction, notional):
    return {"symbol": symbol, "direction": direction, "notional_estimate": notional, "reason": None}


def assess(st, records, cands, now=NOW, env="prod", registry_error=None):
    book, reason = df.book_from_state(st, records, registry_error, env, now)
    return df.evaluate(book, reason, cands)


# =============================================================================
# 1. The pure helper (utils/delta_fit.py)
# =============================================================================
class TestDeltaFitIssueNumbers(unittest.TestCase):

    def test_pump_fits_xpl_blocked_by_the_resting_br_entry(self):
        res = assess(state(), [dict(BR_REC)], [cand("PUMPUSDT", "SHORT", 30.0), cand("XPLUSDT", "SHORT", 49.0)])
        pump, xpl = res["candidates"]
        self.assertEqual(pump["status"], df.FITS)
        self.assertEqual(round(pump["ratio"], 2), -0.18)
        self.assertFalse(pump["blocked_by_resting"])
        self.assertEqual(xpl["status"], df.BLOCKED)
        self.assertEqual(round(xpl["ratio"], 2), -0.40)
        self.assertTrue(xpl["blocked_by_resting"])
        self.assertEqual([(b["symbol"], b["direction"], b["entry_id"]) for b in xpl["blockers"]],
                         [("BRUSDT", "LONG", "111")])
        self.assertAlmostEqual(xpl["blockers"][0]["notional"], 21.0)
        self.assertEqual(res["cumulative"]["status"], "ok")
        self.assertEqual(round(res["cumulative"]["ratio"], 2), -0.58)
        self.assertEqual(res["cumulative"]["bias"], "SHORT_HEAVY")
        self.assertEqual((round(res["book"]["ratio"], 2), res["book"]["resting"]), (1.0, 1))
        # Printed text
        self.assertEqual(df.format_candidate(pump), ["Delta (est.): fits (ratio -0.18, ~30 USDT)"])
        lines = df.format_candidate(xpl)
        self.assertEqual(lines[0], "Delta (est.): BLOCKED by delta (ratio -0.40, ~49 USDT): resting BRUSDT LONG "
                                   "~21 USDT (entry 111, expires 2027-01-15 09:20 UTC, score 72)")
        self.assertEqual(lines[1:], ["swap: python3 scripts/execute_futures_trade.py --cancel-pending --symbol "
                                     "BRUSDT --entry-id 111"])
        summary = df.format_summary(res, 2)
        self.assertIn("book now +1.00 (positions + 1 resting), after all 2 -0.58 (SHORT_HEAVY)", summary)
        self.assertIn(df.ESTIMATE_NOTE, summary)

    def test_a_book_already_heavy_on_that_side_blocks_only_that_side(self):
        res = assess(state(short_n=100.0, listed=()), [], [cand("AUSDT", "SHORT", 1.0), cand("BUSDT", "LONG", 10.0)])
        short, long_ = res["candidates"]
        self.assertEqual(short["status"], df.BLOCKED)
        self.assertFalse(short["blocked_by_resting"])
        self.assertEqual(df.format_candidate(short),
                         ["Delta (est.): BLOCKED by delta (ratio -1.00, ~1 USDT): SHORT_HEAVY even without the resting "
                          "entries"])
        self.assertEqual(long_["status"], df.FITS)  # still SHORT_HEAVY after it, but Gate 1 only blocks a LONG on LONG_HEAVY

    def test_the_state_label_blocks_like_gate_1_even_when_resting_balances_the_book(self):
        # Positions LONG 100 (label LONG_HEAVY), a resting SHORT ~100: the combined book is 0.00, a LONG ~10 would be
        # +0.05, but Gate 1 also rejects on session_state.json's positions-only label
        short_rest = dict(BR_REC, symbol="SRTUSDT", direction="SHORT", entry_id="555", trigger_or_limit_price=1.0,
                          total_qty=100.0)
        st = state(long_n=100.0, listed=({"symbol": "SRTUSDT", "dir": "SHORT", "kind": "LIMIT"},))
        self.assertEqual(st["portfolio_exposure"]["delta_bias"], "LONG_HEAVY")
        res = assess(st, [short_rest], [cand("AUSDT", "LONG", 10.0), cand("BUSDT", "SHORT", 10.0)])
        long_, short = res["candidates"]
        self.assertEqual(round(res["book"]["ratio"], 2), 0.0)
        self.assertEqual(long_["status"], df.BLOCKED)
        self.assertFalse(long_["blocked_by_resting"])
        self.assertEqual(short["status"], df.FITS)
        # The label is the one Gate 1 reads (portfolio_exposure.delta_bias, else portfolio_delta_bias)
        st["portfolio_exposure"].pop("delta_bias")
        st["portfolio_delta_bias"] = "LONG_HEAVY"
        self.assertEqual(assess(st, [short_rest], [cand("AUSDT", "LONG", 10.0)])["candidates"][0]["status"],
                         df.BLOCKED)

    def test_an_order_may_tip_an_empty_book_as_in_gate_1(self):
        res = assess(state(listed=()), [], [cand("XPLUSDT", "SHORT", 49.0)])
        self.assertEqual(res["candidates"][0]["status"], df.FITS)
        self.assertEqual(res["candidates"][0]["ratio"], -1.0)
        # On a non-empty book (a position only) the same order is blocked, and not by a resting entry
        res = assess(state(long_n=21.0, listed=()), [], [cand("XPLUSDT", "SHORT", 49.0)])
        self.assertEqual(res["candidates"][0]["status"], df.BLOCKED)
        self.assertFalse(res["candidates"][0]["blocked_by_resting"])

    def test_registry_record_absent_from_the_state_is_not_counted(self):
        # Dead: placed before the sync (NOW - 600 < last_updated_ts NOW - 30) and not among its resting_mismatches
        dead = dict(BR_REC, symbol="ADAUSDT", entry_id="222", trigger_or_limit_price=1.0, total_qty=500.0)
        self.assertLess(dead["placed_at_ts"], state()["last_updated_ts"])
        res = assess(state(), [dict(BR_REC), dead], [cand("PUMPUSDT", "SHORT", 30.0)])
        self.assertEqual(res["book"]["resting"], 1)
        self.assertEqual(round(res["candidates"][0]["ratio"], 2), -0.18)
        # The state lists BR once: a second BR LONG record is not counted twice
        twin = dict(BR_REC, entry_id="333")
        res = assess(state(), [dict(BR_REC), twin], [cand("PUMPUSDT", "SHORT", 30.0)])
        self.assertEqual(res["book"]["resting"], 1)

    def test_two_resting_entries_blocking_together_are_both_named(self):
        other = dict(BR_REC, symbol="ONEUSDT", entry_id="444", trigger_or_limit_price=1.0, total_qty=10.0)
        st = state(listed=(BR_LISTED, {"symbol": "ONEUSDT", "dir": "LONG", "kind": "LIMIT"}))
        res = assess(st, [dict(BR_REC), other], [cand("XPLUSDT", "SHORT", 80.0)])
        row = res["candidates"][0]
        # (31 - 80) / 111 = -0.44: removing either one alone still leaves a non-empty book tipped heavy
        self.assertEqual(row["status"], df.BLOCKED)
        self.assertTrue(row["blocked_by_resting"])
        self.assertEqual(sorted(b["symbol"] for b in row["blockers"]), ["BRUSDT", "ONEUSDT"])
        self.assertEqual(len([l for l in df.format_candidate(row) if l.startswith("swap: ")]), 2)

    def test_unsafe_ids_get_no_swap_command(self):
        weird = dict(BR_REC, entry_id="1; rm -rf /")
        row = assess(state(), [weird], [cand("XPLUSDT", "SHORT", 49.0)])["candidates"][0]
        self.assertTrue(row["blocked_by_resting"])
        self.assertFalse([l for l in df.format_candidate(row) if l.startswith("swap: ")])


class TestDeltaFitUnknown(unittest.TestCase):

    def assertAllUnknown(self, res, fragment):
        for row in res["candidates"]:
            self.assertEqual(row["status"], df.UNKNOWN)
            line = df.format_candidate(row)[0]
            self.assertTrue(line.startswith("Delta (est.): UNKNOWN ("), line)
            self.assertIn(fragment, line)
            self.assertNotIn("fits", line)
        self.assertEqual(res["cumulative"]["status"], df.UNKNOWN)
        self.assertIn("UNKNOWN", df.format_summary(res, len(res["candidates"])))

    def test_stale_other_env_missing_invalid_state_and_unreadable_registry(self):
        cands = [cand("PUMPUSDT", "SHORT", 30.0)]
        self.assertAllUnknown(assess(state(now=NOW - 301 + 30), [dict(BR_REC)], cands), "stale (301s > 300s)")
        self.assertAllUnknown(assess(state(last_updated_ts=0), [dict(BR_REC)], cands), "stale (unknown age")
        self.assertAllUnknown(assess(state(env="testnet"), [dict(BR_REC)], cands), "is for TESTNET")
        self.assertAllUnknown(assess(None, [], cands), "missing or unreadable")
        self.assertAllUnknown(assess(state(is_valid=False), [], cands), "invalid")
        self.assertAllUnknown(assess(state(error="boom"), [], cands), "invalid")
        self.assertAllUnknown(assess(state(), [], cands, registry_error="pending entries registry malformed"),
                              "registry malformed")
        unknown_resting = state()
        unknown_resting["portfolio_exposure"]["delta_bias_incl_resting"] = "UNKNOWN"
        self.assertAllUnknown(assess(unknown_resting, [dict(BR_REC)], cands), "resting entries unknown")
        bad_qty = dict(BR_REC, total_qty="x")
        self.assertAllUnknown(assess(state(), [bad_qty], cands), "without a readable notional")
        for res in (assess(None, [], cands),):
            self.assertIn(df.BOOK_UNAVAILABLE, df.format_candidate(res["candidates"][0])[0])

    def test_an_unknown_candidate_makes_the_cumulative_unknown(self):
        res = assess(state(), [dict(BR_REC)], [cand("PUMPUSDT", "SHORT", 30.0),
                                               dict(cand("XPLUSDT", "SHORT", None), reason="no sizing")])
        self.assertEqual(res["candidates"][0]["status"], df.FITS)
        self.assertEqual(res["candidates"][1]["status"], df.UNKNOWN)
        self.assertEqual(res["cumulative"]["status"], df.UNKNOWN)
        self.assertIn("XPLUSDT SHORT: no sizing", df.format_summary(res, 2))

    def test_load_registry_records(self):
        import tempfile
        with tempfile.TemporaryDirectory() as logs:
            self.assertEqual(df.load_registry_records(logs, "prod"), ([], None))
            path = os.path.join(logs, "pending_entries.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertIn("unreadable", df.load_registry_records(logs, "prod")[1])
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
            self.assertEqual(df.load_registry_records(logs, "prod"), ([], "pending entries registry malformed"))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"entries": {"a": dict(BR_REC), "b": dict(BR_REC, target_env="testnet"), "c": "x"}}, f)
            self.assertEqual(df.load_registry_records(logs, "prod"), ([dict(BR_REC)], None))


class TestNotionalEstimate(unittest.TestCase):
    BRIEF = {"generated_at_ts": NOW - 100, "risk_profile": {"risk_per_trade_usdt": 5.0, "account_equity_usdt": 100.0,
                                                           "max_margin_ratio": 0.3}}

    def test_risk_based_and_margin_capped(self):
        c = {"entry": 1.0, "stop_loss": 0.9, "leverage": 3}
        self.assertAlmostEqual(df.estimate_notional(c, self.BRIEF, NOW - 100)[0], 50.0)  # 5 / 0.1 x 1.0 < 90
        self.assertAlmostEqual(df.estimate_notional(dict(c, leverage=1), self.BRIEF, NOW - 100)[0], 30.0)  # cap

    def test_unknown_inputs(self):
        c = {"entry": 1.0, "stop_loss": 0.9, "leverage": 3}
        for args, fragment in (
                ((c, self.BRIEF, NOW - 99), "brief was replaced"),
                ((c, self.BRIEF, None), "brief was replaced"),
                ((c, None, NOW - 100), "missing or unreadable"),
                ((c, dict(self.BRIEF, risk_profile=None), NOW - 100), "missing risk_profile"),
                ((c, {"generated_at_ts": NOW - 100, "risk_profile": {"risk_per_trade_usdt": 5.0,
                                                                     "account_equity_usdt": 100.0,
                                                                     "max_margin_ratio": None}}, NOW - 100),
                 "missing risk_profile"),
                ((dict(c, leverage=None), self.BRIEF, NOW - 100), "missing risk_profile"),
                ((dict(c, stop_loss=1.0), self.BRIEF, NOW - 100), "entry equals stop_loss"),
                ((dict(c, entry="abc"), self.BRIEF, NOW - 100), "missing risk_profile")):
            notional, reason = df.estimate_notional(*args)
            self.assertIsNone(notional, fragment)
            self.assertIn(fragment, reason)

    def test_non_positive_inputs_are_unknown(self):
        c = {"entry": 1.0, "stop_loss": 0.9, "leverage": 3}
        rp = self.BRIEF["risk_profile"]
        for cand_, profile in ((dict(c, leverage=0), rp), (dict(c, leverage=-3), rp),
                               (c, dict(rp, account_equity_usdt=0)), (c, dict(rp, risk_per_trade_usdt=-1.0)),
                               (c, dict(rp, max_margin_ratio=0))):
            notional, reason = df.estimate_notional(cand_, dict(self.BRIEF, risk_profile=profile), NOW - 100)
            self.assertIsNone(notional, (cand_, profile))
            self.assertIn("non-positive", reason)

    def test_yolo_is_sized_from_its_margin(self):
        brief = dict(self.BRIEF, risk_profile=dict(self.BRIEF["risk_profile"], yolo_margin_usdt=10.0))
        c = {"entry": 1.0, "stop_loss": 0.9, "leverage": 5, "is_yolo": True}
        self.assertEqual(df.estimate_notional(c, brief, NOW - 100), (50.0, None))  # 10 x 5, not the risk formula
        self.assertEqual(df.estimate_notional(dict(c, is_yolo="true"), brief, NOW - 100), (50.0, None))
        for cand_, b in ((c, self.BRIEF), (dict(c, leverage=None), brief), (dict(c, leverage=0), brief),
                         (c, dict(brief, risk_profile=dict(brief["risk_profile"], yolo_margin_usdt=0)))):
            notional, reason = df.estimate_notional(cand_, b, NOW - 100)
            self.assertIsNone(notional)
            self.assertIn("yolo_margin_usdt", reason)


class TestStateListedWithoutRecord(unittest.TestCase):
    """Audit round 1: a resting entry the state lists but the registry lacks is UNKNOWN, never zero exposure."""

    def test_listed_entry_without_record_is_unknown(self):
        cands = [cand("XPLUSDT", "SHORT", 49.0)]
        for st, records in ((state(), []),  # BR filled and its record dropped while the state is still fresh
                            (state(listed=(BR_LISTED, BR_LISTED)), [dict(BR_REC)])):  # listed twice, one record
            res = assess(st, records, cands)
            self.assertEqual(res["candidates"][0]["status"], df.UNKNOWN)
            self.assertIn("session_state.json lists resting BRUSDT LONG without a registry record",
                          res["candidates"][0]["reason"])
            self.assertIn(df.BOOK_UNAVAILABLE, res["candidates"][0]["reason"])
            self.assertEqual(res["cumulative"]["status"], df.UNKNOWN)
        # A missing registry file (no records, no error) with a listed entry: UNKNOWN too
        import tempfile
        with tempfile.TemporaryDirectory() as logs:
            records, err = df.load_registry_records(logs, "prod")
            book, reason = df.book_from_state(state(), records, err, "prod", NOW)
        self.assertIsNone(book)
        self.assertIn("without a registry record", reason)
        # Nothing listed and no file: an empty resting book
        book, reason = df.book_from_state(state(listed=()), [], None, "prod", NOW)
        self.assertEqual((book["resting"], reason), ([], None))


class TestPartiallyFilledRestingEntry(unittest.TestCase):
    """Audit round 2: the sync skips a registry record whose symbol has a position, but Gate 1 counts a partial
    fill's unfilled remainder (portfolio_exposure.resting_opening_legs: quantity - executedQty)."""

    def test_record_on_a_symbol_with_a_position_is_unknown(self):
        # BR LONG LIMIT 21 half filled: position LONG 10.5 (+ position SHORT 10 elsewhere), 10.5 still resting. Gate 1:
        # L21 / S10 -> +0.35 with a LONG ~10 -> LONG_HEAVY, rejected; the cache alone would say +0.34 fits.
        st = state(long_n=10.5, short_n=10.0, listed=(), positions=(("BRUSDT", "LONG"), ("ZZZUSDT", "SHORT")))
        res = assess(st, [dict(BR_REC)], [cand("AUSDT", "LONG", 10.0)])
        row = res["candidates"][0]
        self.assertEqual(row["status"], df.UNKNOWN)
        self.assertIn("possible partially filled resting entry BRUSDT LONG: remainder not in the cache", row["reason"])
        self.assertIn(df.BOOK_UNAVAILABLE, row["reason"])
        self.assertNotIn("fits", df.format_candidate(row)[0])
        self.assertEqual(res["cumulative"]["status"], df.UNKNOWN)
        self.assertIn("UNKNOWN (possible partially filled", df.format_summary(res, 1))
        # Without that record the same cache is a plain book (+0.34 fits), so the record is what makes it UNKNOWN
        self.assertEqual(assess(st, [], [cand("AUSDT", "LONG", 10.0)])["candidates"][0]["status"], df.FITS)

    def test_malformed_active_positions_is_unknown(self):
        for positions in (None, "x", [1]):
            st = state(listed=(), active_positions=positions)
            self.assertIn("active_positions", assess(st, [], [cand("AUSDT", "LONG", 1.0)])["candidates"][0]["reason"])


class TestUnlistedRecordThatMayRest(unittest.TestCase):
    """Audit round 3: a same-env record the state does not list may still rest (placed after the sync, or not yet
    indexed at it and so in resting_mismatches); Gate 1 counts it live, so the preview is UNKNOWN."""
    MISMATCH = {"symbol": "BRUSDT", "entry_id": "111", "side": "LONG"}

    def check_unknown(self, st, rec_):
        res = assess(st, [rec_], [cand("XPLUSDT", "SHORT", 49.0)])
        row = res["candidates"][0]
        self.assertEqual(row["status"], df.UNKNOWN)
        self.assertIn("resting entry BRUSDT LONG not matched at the last sync (placed after it or not yet indexed)",
                      row["reason"])
        self.assertIn(df.BOOK_UNAVAILABLE, row["reason"])
        self.assertEqual(res["cumulative"]["status"], df.UNKNOWN)

    def test_record_in_resting_mismatches(self):
        self.check_unknown(state(listed=(), mismatches=(self.MISMATCH,)), dict(BR_REC))  # placed before the sync
        # A mismatch row without an entry id matches by symbol
        self.check_unknown(state(listed=(), mismatches=({"symbol": "BRUSDT", "side": "LONG"},)), dict(BR_REC))

    def test_record_placed_at_or_after_the_sync(self):
        self.check_unknown(state(listed=()), dict(BR_REC, placed_at_ts=NOW - 10))  # sync at NOW - 30
        self.check_unknown(state(listed=()), dict(BR_REC, placed_at_ts=NOW - 30))

    def test_record_without_placed_at_ts(self):
        rec_ = dict(BR_REC)
        rec_.pop("placed_at_ts")
        self.check_unknown(state(listed=()), rec_)
        self.check_unknown(state(listed=()), dict(BR_REC, placed_at_ts="x"))

    def test_older_record_not_in_mismatches_is_dead(self):
        other = {"symbol": "ADAUSDT", "entry_id": "999", "side": "LONG"}
        res = assess(state(listed=(), mismatches=(other,)), [dict(BR_REC)], [cand("XPLUSDT", "SHORT", 49.0)])
        self.assertEqual(res["candidates"][0]["status"], df.FITS)  # empty book, as Gate 1 would see it
        self.assertEqual(res["book"]["resting"], 0)

    def test_malformed_mismatches_is_unknown(self):
        st = state(listed=())
        st["portfolio_exposure"]["resting_mismatches"] = "x"
        self.assertIn("resting_mismatches", assess(st, [], [cand("XPLUSDT", "SHORT", 49.0)])["candidates"][0]["reason"])


class TestBlockersAndSwapText(unittest.TestCase):

    def test_same_side_and_opposite_side_entries_blocking_together_are_both_named(self):
        # BR LONG 21 + SRT SHORT 10 resting, XPL SHORT 49: (21-59)/80 = -0.48. Without BR: (0-59)/59 = -1.00 on a
        # non-empty book; without SRT: (21-49)/70 = -0.40. Only cancelling both lets it fit, so both are named (naming
        # only the opposite-side BR would offer a swap that still leaves XPL blocked).
        srt = dict(BR_REC, symbol="SRTUSDT", direction="SHORT", entry_id="555", trigger_or_limit_price=1.0,
                   total_qty=10.0)
        st = state(listed=(BR_LISTED, {"symbol": "SRTUSDT", "dir": "SHORT", "kind": "LIMIT"}))
        row = assess(st, [dict(BR_REC), srt], [cand("XPLUSDT", "SHORT", 49.0)])["candidates"][0]
        self.assertTrue(row["blocked_by_resting"])
        self.assertEqual(sorted(b["symbol"] for b in row["blockers"]), ["BRUSDT", "SRTUSDT"])
        swaps = [l for l in df.format_candidate(row) if l.startswith("swap: ")]
        self.assertEqual(len(swaps), 2)
        self.assertTrue(all(l.endswith(" (cancel together)") for l in swaps), swaps)
        # A single sufficient blocker is not marked
        row = assess(state(), [dict(BR_REC)], [cand("XPLUSDT", "SHORT", 49.0)])["candidates"][0]
        self.assertNotIn("(cancel together)", " ".join(df.format_candidate(row)))

    def test_swap_text_names_a_non_prod_env(self):
        row = assess(state(), [dict(BR_REC)], [cand("XPLUSDT", "SHORT", 49.0)])["candidates"][0]
        self.assertTrue(df.format_candidate(row, "prod")[1].endswith("--entry-id 111"))
        self.assertTrue(df.format_candidate(row, "testnet")[1].endswith("--entry-id 111 --env testnet"))


class TestRegistryReaderParity(unittest.TestCase):
    """delta_fit.load_registry_records keeps sync_session_state._load_registry_records' semantics (audit round 1)."""

    def test_same_records_and_error_presence(self):
        import tempfile
        import sync_session_state as sss
        with tempfile.TemporaryDirectory() as logs, patch.object(sss, "LOGS_DIR", logs):
            path = os.path.join(logs, "pending_entries.json")
            contents = [None, "{not json", json.dumps([]), json.dumps({"entries": []}),
                        json.dumps({"entries": {"a": dict(BR_REC), "b": dict(BR_REC, target_env="testnet"),
                                                "c": "x", "d": dict(BR_REC, entry_id="2")}})]
            for content in contents:
                if content is None:
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                for env in ("prod", "testnet"):
                    mine, theirs = df.load_registry_records(logs, env), sss._load_registry_records(env)
                    self.assertEqual(mine[0], theirs[0], (content, env))
                    self.assertEqual(mine[1] is None, theirs[1] is None, (content, env))


# =============================================================================
# 2. The recorder summary (record_evaluation._print_summary)
# =============================================================================
class _PreviewWorkspace(t267._RecheckWorkspace):
    """A normal (non re-check) brief with a risk_profile, the cached book and the registry in the workspace."""

    def setUp(self):
        super().setUp()
        self.brief_ts = self.now - 60
        self.logs = os.path.join(self.workspace, "logs")
        self.write_json("primed_brief.json", {"generated_at_ts": self.brief_ts, "target_env": "PROD",
                                              "risk_profile": {"risk_per_trade_usdt": 3.0,
                                                               "account_equity_usdt": 1000.0,
                                                               "max_margin_ratio": 0.3}})
        self.write_json("session_state.json", state(now=self.now))
        self.write_json("pending_entries.json", {"schema_version": 2, "entries": {
            "prod:BRUSDT:111": dict(BR_REC, placed_at_ts=self.now - 600, expires_at_ts=self.now + 4800)}})

    def write_json(self, name, data):
        with open(os.path.join(self.logs, name), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def payload(self, cands=None, brief_ts="default"):
        # PUMP: 3 / 0.1 x 1.0 = 30 USDT; XPL: 3 / 0.03 x 0.49 = 49 USDT (cap 1000 x 0.3 x 3 = 900)
        cands = cands if cands is not None else [
            {"symbol": "PUMPUSDT", "direction": "SHORT", "tier": "A+", "entry": 1.0, "stop_loss": 1.1, "tp1": 0.9,
             "tp2": 0.6, "leverage": 3, "score": 70, "is_yolo": False, "requires_user_confirmation": True},
            {"symbol": "XPLUSDT", "direction": "SHORT", "tier": "A+", "entry": 0.49, "stop_loss": 0.52, "tp1": 0.46,
             "tp2": 0.37, "leverage": 3, "score": 68, "is_yolo": False, "requires_user_confirmation": True}]
        p = {"status": "APPROVED", "evaluator_agent": dp.EVALUATOR_NAME, "target_env": "PROD",
             "approved_candidates": cands, "summary": "scan"}
        if brief_ts is not None:
            p["brief_generated_at_ts"] = self.brief_ts if brief_ts == "default" else brief_ts
        return p

    def record(self, payload, conv=t267.NEW_CONV):
        return self.record_new(payload, {"generated_at_ts": self.brief_ts}, conv=conv, ts=self.now - 10)

    def snapshot_logs(self):
        out = {}
        for root, _dirs, files in os.walk(self.logs):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as f:
                    out[os.path.relpath(path, self.logs)] = f.read()
        return out


class TestRecorderDeltaPreview(_PreviewWorkspace):

    def test_delta_lines_cumulative_line_and_timestamps(self):
        record, out, err = self.record(self.payload())
        self.assertEqual(err, "")
        lines = out.splitlines()
        pump = lines.index(next(l for l in lines if l.startswith("     - PUMPUSDT SHORT")))
        self.assertEqual(lines[pump + 1], "       Delta (est.): fits (ratio -0.18, ~30 USDT)")
        xpl = lines.index(next(l for l in lines if l.startswith("     - XPLUSDT SHORT")))
        self.assertTrue(lines[xpl + 1].startswith("       Delta (est.): BLOCKED by delta (ratio -0.40, ~49 USDT): "
                                                  "resting BRUSDT LONG ~21 USDT (entry 111, expires "), lines[xpl + 1])
        self.assertEqual(lines[xpl + 2], "       swap: python3 scripts/execute_futures_trade.py --cancel-pending "
                                         "--symbol BRUSDT --entry-id 111")
        self.assertIn("   Delta (est.) of the approved set: book now +1.00 (positions + 1 resting), after all 2 -0.58 "
                      f"(SHORT_HEAVY); {df.ESTIMATE_NOTE}", lines)
        # Existing lines and markers unchanged
        self.assertIn("REQUIRES USER CONFIRMATION in chat before executing", lines[pump])
        self.assertIn("   Approved (2):", lines)
        self.assertIn("Valid until", out)
        self.assertIn("   Summary: scan", lines)
        self.assertTrue(any(l.startswith("   Provenance: agy subagent transcript step") for l in lines))
        self.assertTrue(any(l.startswith("   Location: ") for l in lines))
        # Item 7: brief generated at, evaluated at and valid until, with their dates
        self.assertIn(f"   Brief generated at: {rec._fmt_utc(self.brief_ts)}", lines)
        self.assertIn(f"   Evaluated at: {record['timestamp_utc']} | Valid until: {rec._fmt_utc(record['valid_until_ts'])} ",
                      out)
        self.assertRegex(record["timestamp_utc"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$")
        # Nothing of the preview is stored in the dossier
        stored = dp.load_dossier(self.dossier_path)
        self.assertFalse([k for k in stored if "delta" in k.lower()])
        self.assertFalse([k for c in stored["approved_candidates"] for k in c if "delta" in k.lower()])

    def test_nothing_written_besides_the_recorder_files_and_no_socket(self):
        before = self.snapshot_logs()
        with patch.object(socket, "socket", side_effect=AssertionError("socket opened")):
            self.record(self.payload())
        after = self.snapshot_logs()
        for name in ("primed_brief.json", "session_state.json", "pending_entries.json"):
            self.assertEqual(after[name], before[name])
        new = sorted(set(after) - set(before))
        self.assertTrue(new and all(n.startswith("evaluations" + os.sep) for n in new), new)
        self.assertNotIn("gate_denials.jsonl", after)

    def test_stale_state_prints_unknown_never_fits(self):
        self.write_json("session_state.json", state(now=self.now - 400))
        _, out, err = self.record(self.payload())
        self.assertEqual(err, "")
        self.assertNotIn("Delta (est.): fits", out)
        self.assertEqual(out.count("Delta (est.): UNKNOWN (session_state.json stale"), 2)
        self.assertIn("Delta (est.) of the approved set: UNKNOWN (session_state.json stale", out)
        self.assertIn(df.BOOK_UNAVAILABLE, out)

    def test_registry_file_absent_while_the_state_lists_an_entry(self):
        os.remove(os.path.join(self.logs, "pending_entries.json"))
        _, out, err = self.record(self.payload())
        self.assertEqual(err, "")
        self.assertNotIn("Delta (est.): fits", out)
        self.assertNotIn("BLOCKED", out)
        self.assertEqual(out.count("Delta (est.): UNKNOWN (session_state.json lists resting BRUSDT LONG without a "
                                   "registry record"), 2)
        self.assertIn("Delta (est.) of the approved set: UNKNOWN (session_state.json lists resting BRUSDT LONG", out)

    def test_partially_filled_resting_entry_is_unknown_end_to_end(self):
        # BR's record is still in the registry, the sync left it out of resting_entries (its symbol has a position)
        self.write_json("session_state.json", state(now=self.now, long_n=10.5, short_n=10.0, listed=(),
                                                    positions=(("BRUSDT", "LONG"), ("ZZZUSDT", "SHORT"))))
        _, out, err = self.record(self.payload())
        self.assertEqual(err, "")
        self.assertNotIn("Delta (est.): fits", out)
        self.assertNotIn("BLOCKED", out)
        self.assertEqual(out.count("Delta (est.): UNKNOWN (possible partially filled resting entry BRUSDT LONG: "
                                   "remainder not in the cache"), 2)
        self.assertIn("Delta (est.) of the approved set: UNKNOWN (possible partially filled resting entry BRUSDT", out)

    def test_record_placed_after_the_sync_is_unknown_end_to_end(self):
        # BR LONG placed after the (still fresh) state was written: not in resting_entries, Gate 1 counts it live
        self.write_json("session_state.json", state(now=self.now, listed=()))
        self.write_json("pending_entries.json", {"schema_version": 2, "entries": {
            "prod:BRUSDT:111": dict(BR_REC, placed_at_ts=self.now - 20, expires_at_ts=self.now + 5380)}})
        _, out, err = self.record(self.payload())
        self.assertEqual(err, "")
        self.assertNotIn("Delta (est.): fits", out)
        self.assertEqual(out.count("Delta (est.): UNKNOWN (resting entry BRUSDT LONG not matched at the last sync"), 2)
        self.assertIn("Delta (est.) of the approved set: UNKNOWN (resting entry BRUSDT LONG", out)

    def test_testnet_dossier_prints_not_applicable(self):
        self.write_json("session_state.json", state(now=self.now, env="testnet"))
        record = {"status": "APPROVED", "target_env": "testnet", "valid_until_ts": self.now + 600,
                  "raw_payload": {"brief_generated_at_ts": self.brief_ts},
                  "approved_candidates": [{"symbol": "XPLUSDT", "direction": "SHORT", "entry": 0.49,
                                           "stop_loss": 0.52, "leverage": 3}]}
        buf = io.StringIO()
        with redirect_stdout(buf):
            rec._print_summary(record, os.path.join(self.workspace, "x.json"), self.workspace, self.now)
        out = buf.getvalue()
        self.assertIn("       Delta (est.): n/a (TESTNET, Gate 1 not enforced)", out.splitlines())
        self.assertIn("   Delta (est.) of the approved set: n/a (TESTNET, Gate 1 not enforced)", out.splitlines())
        self.assertNotIn("BLOCKED", out)
        self.assertNotIn("swap:", out)

    def test_brief_replaced_or_absent_gives_unknown(self):
        _, out, _ = self.record(self.payload(brief_ts=self.brief_ts - 5))
        self.assertEqual(out.count("Delta (est.): UNKNOWN (the brief was replaced"), 2)
        self.assertNotIn("Delta (est.): fits", out)
        _, out, _ = self.record(self.payload(brief_ts=None), conv="abcdabcd-1111-4222-8333-444455556666")
        self.assertIn("Delta (est.): UNKNOWN (the brief was replaced or the dossier names none", out)
        self.assertIn("   Brief generated at: unknown", out.splitlines())

    def test_garbage_inputs_never_raise(self):
        with open(os.path.join(self.logs, "session_state.json"), "w", encoding="utf-8") as f:
            f.write("{garbage")
        with open(os.path.join(self.logs, "pending_entries.json"), "w", encoding="utf-8") as f:
            f.write("[1, 2")
        _, out, _ = self.record(self.payload())
        self.assertEqual(out.count("Delta (est.): UNKNOWN ("), 2)
        # Direct call with malformed candidates and record fields
        record = {"status": "APPROVED", "target_env": None, "valid_until_ts": NOW + 60, "raw_payload": None,
                  "approved_candidates": [{"symbol": "AUSDT", "direction": None, "entry": "x"},
                                          {"symbol": "BUSDT", "direction": "LONG", "entry": 1, "stop_loss": 1}]}
        buf = io.StringIO()
        with redirect_stdout(buf):
            rec._print_summary(record, os.path.join(self.workspace, "x.json"), self.workspace, NOW)
        self.assertEqual(buf.getvalue().count("Delta (est.): UNKNOWN ("), 2)
        self.assertIn("Brief generated at: unknown", buf.getvalue())

    def test_a_failing_helper_is_unknown(self):
        with patch.object(df, "evaluate", side_effect=RuntimeError("boom")):
            _, out, err = self.record(self.payload())
        self.assertEqual(err, "")
        self.assertEqual(out.count("Delta (est.): UNKNOWN (preview failed: RuntimeError)"), 2)
        self.assertIn("Delta (est.) of the approved set: UNKNOWN (preview failed: RuntimeError)", out)

    def test_no_preview_without_approval(self):
        _, out, _ = self.record(dict(self.payload(cands=[]), status="NEUTRAL"))
        self.assertIn("No trade authorized by this dossier.", out)
        self.assertNotIn("Delta (est.)", out)


# =============================================================================
# 3. Item 7: recheck_summary and the executor's resting-entry result
# =============================================================================
class TestRecheckSummaryTimes(unittest.TestCase):

    def test_old_plan_times_are_named(self):
        brief = {"recheck": {"symbol": "ETHFIUSDT", "direction": "LONG", "setup_status": "found"},
                 "recheck_of": {"sha256": "a" * 64, "tier": "S", "entry": 1.0, "stop_loss": 0.97, "tp2": 1.12,
                                "evaluated_ts": NOW, "valid_until_ts": NOW + 1200}}
        line = rcb.recheck_summary(brief)
        self.assertTrue(line.startswith("**Re-check:** ETHFIUSDT LONG -> `found`"))
        self.assertIn("evaluated at 2027-01-15 08:00:00 UTC, valid until 2027-01-15 08:20:00 UTC", line)
        brief["recheck_of"].pop("evaluated_ts")
        self.assertIn("evaluated at unknown, valid until 2027-01-15 08:20:00 UTC", rcb.recheck_summary(brief))


class TestRestingEntryResultTimes(tpe.ExecutorHarness):

    def check_times(self, res):
        self.assertTrue(res["success"], res.get("error"))
        reg = tpe.read_registry(self.ws)[res["pending_entry_key"]]
        self.assertEqual(res["placed_at_ts"], reg["placed_at_ts"])
        self.assertEqual(res["expires_at_ts"], reg["expires_at_ts"])
        fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))  # noqa: E731
        self.assertEqual(res["placed_at_utc"], fmt(reg["placed_at_ts"]))
        self.assertEqual(res["expires_at_utc"], fmt(reg["expires_at_ts"]))

    def test_stop_market_result(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res.get("conditional_entry"), res)
        self.check_times(res)

    def test_limit_result(self):
        res = self.execute(order_type="LIMIT", limit_price=98.767)
        self.assertTrue(res.get("pending_limit_entry"), res)
        self.check_times(res)


if __name__ == "__main__":
    unittest.main()
