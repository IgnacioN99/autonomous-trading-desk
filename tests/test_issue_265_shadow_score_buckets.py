#!/usr/bin/env python3
"""
test_issue_265_shadow_score_buckets.py - shadow desk score buckets, an advisory early signal for #202 (issue #265).

Covers the score fields on rejected / hook-denial / approved-not-executed rows (score_schema_version only from a
matching sidecar), the approved-candidate ledger and its sweep (once per dossier sha + symbol + direction, only after
valid_until_ts, never with an audit or resting record carrying the sha, processed marks, env filter, YOLO flag, missing
brief), late rows at the dossier's own time, the bucket math at every edge, insufficient_sample, EXPIRED = 0 R, the
source / gate splits, the real-vs-shadow columns, the unchanged FER / regret / calibration / gate counts, and the guard
that shadow rows never enter score_calibration.json or any gate.
Hermetic: temp directories only (every shadow_tracker path redirected by TrackerBase), no Binance client, network blocked.
"""

import io
import os
import re
import sys
import json
import time
import tempfile
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
import trading_scorecard as sc  # noqa: E402
import test_issue_251_delta_gate_regret as t251  # noqa: E402  (fixtures only)
from utils import score_calibration as scal  # noqa: E402
from utils.shadow_common import NOT_EXECUTED_GATE, is_advisory_row  # noqa: E402

SHA = "a" * 64
SHA2 = "b" * 64
SHA3 = "c" * 64
NOW = int(time.time())
TS = NOW - 60            # dossier evaluated a minute ago
VALID = TS + 1200        # 20 min TTL
AFTER = VALID + 10       # a sweep time past the expiry


def cand(symbol, direction="LONG", score=85, tier="Tier S", is_yolo=False):
    long = direction == "LONG"
    return {"symbol": symbol, "direction": direction, "score": score, "tier": tier, "is_yolo": is_yolo,
            "entry": 10.0, "stop_loss": 9.5 if long else 10.5, "tp1": 10.9 if long else 9.1,
            "tp2": 12.0 if long else 8.0, "requires_user_confirmation": False}


class LedgerBase(t251.TrackerBase):

    def approved(self, cands, sha=SHA2, ts=TS, valid=VALID, env="prod", snapshots=None):
        record = {"status": "APPROVED", "target_env": env, "timestamp_ts": ts, "valid_until_ts": valid,
                  "provenance": {"sha256": sha}, "approved_symbols": [c["symbol"] for c in cands],
                  "approved_candidates": cands, "raw_payload": {"target_env": env.upper(), "rejected_candidates": []}}
        if snapshots is not None:
            record["radar_snapshots"] = snapshots
        t251.write_json(st.DOSSIER_FILE, record)
        return record

    def snapshot(self):
        with open(st.DOSSIER_FILE, encoding="utf-8") as f:
            return st.snapshot_approved_candidates(json.load(f))

    def ledger(self):
        return st.load_jsonl(st.approved_ledger_path())

    def ledger_rows(self):
        return [r for r in self.ledger() if not r.get("event")]

    def marks(self):
        return {(r["symbol"], r["direction"]): r["state"] for r in self.ledger() if r.get("event") == "processed"}

    def shadow(self):
        return {r["symbol"]: r for r in st.load_jsonl(st.SHADOW_TRADES_FILE)}


# =============================================================================
# 1. Score fields
# =============================================================================
class TestScoreFields(LedgerBase):

    def write_brief(self, generated_at=1000, **kw):
        t251.write_json(st.BRIEF_FILE, {"generated_at_ts": generated_at, "filtered_opportunities": [
            t251.opp("FETUSDT", confidence=85, tier_code="S", tier="Tier S (x)", **kw)]})

    def write_sidecar(self, generated_at=1000):
        t251.write_json(os.path.join(self.dir, "primed_brief_scores.json"), {
            "generated_at_ts": generated_at, "env": "PROD",
            "rows": [{"symbol": "FETUSDT", "direction": "LONG", "confidence": 85, "score_schema_version": 2}]})

    def test_rejected_row_carries_radar_score_tier_source(self):
        self.write_brief()
        self.write_sidecar()
        self.dossier([{"symbol": "FETUSDT", "direction": "LONG", "score": 84, "gate": "DELTA_GATE", "detail": "x"}])
        self.assertEqual(st.register_from_evaluation(), 1)
        row = self.rows()["FETUSDT"]
        self.assertEqual((row["radar_score"], row["dossier_score"], row["tier"], row["is_yolo"]),
                         (85.0, None, "S", False))
        self.assertEqual((row["source"], row["reason"], row["score_schema_version"]), ("rejected", None, 2))
        self.assertEqual(row["score"], 84.0)  # the existing field is unchanged

    def test_schema_version_only_from_a_matching_sidecar(self):
        self.write_brief(generated_at=1000)
        self.write_sidecar(generated_at=999)   # another brief's sidecar
        self.dossier(None)
        st.register_from_evaluation()
        self.assertIsNone(self.rows()["FETUSDT"]["score_schema_version"])
        self.assertEqual(self.rows()["FETUSDT"]["radar_score"], 85.0)

    def test_no_sidecar_is_null(self):
        self.write_brief()
        self.dossier(None)
        st.register_from_evaluation()
        self.assertIsNone(self.rows()["FETUSDT"]["score_schema_version"])

    def test_hook_denial_row(self):
        t251.write_jsonl(st.GATE_DENIALS_FILE, [{
            "gate": st.POST_APPROVAL_GATE, "ts": NOW - 30, "symbol": "ZROUSDT", "direction": "LONG", "entry": 10.0,
            "stop_loss": 9.5, "tp1": 10.9, "tp2": 12.0, "score": 80, "dossier_sha256": SHA, "env": "PROD",
            "tier": "Tier S", "is_yolo": False, "book": [], "delta_bias": "LONG_HEAVY"}])
        self.assertEqual(st.register_from_gate_denials(), 1)
        row = self.shadow()["ZROUSDT"]
        self.assertEqual((row["dossier_score"], row["source"], row["reason"], row["tier"], row["is_yolo"]),
                         (80.0, "approved_not_executed", "delta_denied", "Tier S", False))
        self.assertIsNone(row["radar_score"])
        self.assertFalse(is_advisory_row(row))   # still an existing regret / replay row

    def test_sweep_row_fields(self):
        self.approved([cand("ETHUSDT", score=82)], snapshots={"ETHUSDT|LONG": {"radar_snapshot": {
            "confidence": 82, "score_schema_version": 2}}})
        self.snapshot()
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 1)
        row = self.shadow()["ETHUSDT"]
        self.assertEqual((row["dossier_score"], row["radar_score"], row["tier"], row["is_yolo"],
                          row["score_schema_version"]), (82.0, 82.0, "Tier S", False, 2))
        self.assertEqual((row["source"], row["reason"], row["gate"]), ("approved_not_executed", "not_executed",
                                                                       NOT_EXECUTED_GATE))
        self.assertTrue(is_advisory_row(row))

    def test_old_rows_parse_as_unscored(self):
        out = sa.run_score_bucket_analysis([t251.resolved_row("old", "d1", score=85)], None, resamples=10)
        view = out["views"]["dossier_score"]
        self.assertEqual((view["unscored"], sum(b["n"] for b in view["buckets"])), (1, 0))


# =============================================================================
# 2. Ledger and sweep
# =============================================================================
class TestLedger(LedgerBase):

    def test_snapshot_once_per_sha_symbol_direction(self):
        self.approved([cand("ETHUSDT"), cand("SOLUSDT", "SHORT", score=70, tier="Tier A+")])
        self.assertEqual(self.snapshot(), 2)
        self.assertEqual(self.snapshot(), 0)
        rows = {r["symbol"]: r for r in self.ledger_rows()}
        self.assertEqual(set(rows), {"ETHUSDT", "SOLUSDT"})
        eth = rows["ETHUSDT"]
        self.assertEqual((eth["sha"], eth["timestamp_ts"], eth["valid_until_ts"], eth["entry"], eth["stop_loss"],
                          eth["tp1"], eth["tp2"], eth["score"], eth["tier"], eth["is_yolo"], eth["target_env"]),
                         (SHA2, TS, VALID, 10.0, 9.5, 10.9, 12.0, 85.0, "Tier S", False, "prod"))
        self.approved([cand("ETHUSDT")], sha=SHA3)   # another dossier: a new row
        self.assertEqual(self.snapshot(), 1)

    def test_missing_brief_does_not_skip_the_ledger(self):
        self.approved([cand("ETHUSDT")])
        self.assertFalse(os.path.exists(st.BRIEF_FILE))
        st.register_from_evaluation()
        self.assertEqual([r["symbol"] for r in self.ledger_rows()], ["ETHUSDT"])

    def test_not_approved_or_without_provenance_snapshots_nothing(self):
        self.dossier(None)   # REJECTED
        self.assertEqual(self.snapshot(), 0)
        record = self.approved([cand("ETHUSDT")])
        record["provenance"] = {}
        self.assertEqual(st.snapshot_approved_candidates(record), 0)
        record.update(provenance={"sha256": SHA2}, valid_until_ts=None)
        self.assertEqual(st.snapshot_approved_candidates(record), 0)
        self.assertEqual(self.ledger(), [])

    def test_registers_only_after_valid_until(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        self.assertEqual(st.sweep_approved_ledger(now_ts=VALID), 0)     # still valid: it may be executed
        self.assertEqual(self.marks(), {})
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 1)
        self.assertEqual(self.marks(), {("ETHUSDT", "LONG"): "registered"})

    def test_late_row_uses_the_dossier_time(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        st.sweep_approved_ledger(now_ts=AFTER)
        row = self.shadow()["ETHUSDT"]
        self.assertEqual(row["registered_at_ts"], TS)
        self.assertEqual((row["trigger_price"], row["sl_price"], row["tp1_price"], row["dossier_sha256"]),
                         (10.0, 9.5, 10.9, SHA2))

    def test_idempotent_rerun_and_intake(self):
        self.approved([cand("ETHUSDT")], ts=NOW - 3000, valid=NOW - 1800)   # already expired at intake
        self.assertEqual(st.register_from_evaluation(), 1)
        self.assertEqual(st.register_from_evaluation(), 0)
        self.assertEqual(st.sweep_approved_ledger(), 0)
        self.assertEqual(len(st.load_jsonl(st.SHADOW_TRADES_FILE)), 1)

    def test_audit_record_means_executed(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        t251.write_jsonl(st.TRADES_AUDIT_FILE, [
            {"symbol": "ETHUSDT", "direction": "LONG", "total_qty": 1, "dossier_sha256": SHA2, "timestamp": TS + 5},
            {"symbol": "ETHUSDT", "direction": "LONG", "event": "cancel", "dossier_sha256": SHA2}])
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 0)
        self.assertEqual(self.marks(), {("ETHUSDT", "LONG"): "executed"})
        self.assertEqual(self.shadow(), {})

    def test_a_trade_under_a_newer_sha_of_the_same_candidate_is_executed(self):
        # scan B re-approved the candidate and the executor used its (newer) sha: the trade is real
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        t251.write_jsonl(st.TRADES_AUDIT_FILE, [
            {"symbol": "ETHUSDT", "direction": "LONG", "total_qty": 1, "dossier_sha256": SHA3, "timestamp": TS + 300,
             "target_env": "prod"}])
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 0)
        self.assertEqual(self.marks(), {("ETHUSDT", "LONG"): "executed"})
        self.assertEqual(self.shadow(), {})

    def test_an_audit_record_of_another_symbol_direction_or_env_does_not_count(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        t251.write_jsonl(st.TRADES_AUDIT_FILE, [
            {"symbol": "SOLUSDT", "direction": "LONG", "total_qty": 1, "dossier_sha256": SHA2, "timestamp": TS + 5},
            {"symbol": "ETHUSDT", "direction": "SHORT", "total_qty": 1, "dossier_sha256": SHA3, "timestamp": TS + 5},
            {"symbol": "ETHUSDT", "direction": "LONG", "total_qty": 1, "dossier_sha256": SHA3, "timestamp": TS + 5,
             "target_env": "testnet"}])
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 1)

    def test_an_older_audit_record_of_the_same_candidate_still_registers(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        t251.write_jsonl(st.TRADES_AUDIT_FILE, [
            {"symbol": "ETHUSDT", "direction": "LONG", "total_qty": 1, "dossier_sha256": SHA3, "timestamp": TS - 3600,
             "target_env": "prod"}])
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 1)

    def write_pending(self, sha=SHA2, symbol="ETHUSDT", placed=None, direction="LONG", env="prod"):
        rec = {"symbol": symbol, "direction": direction, "entry_id": "1", "target_env": env,
               "score_meta": {"dossier_sha256": sha}}
        if placed is not None:
            rec["placed_at_ts"] = placed
        t251.write_json(st.PENDING_ENTRIES_FILE, {"entries": {f"prod:{symbol}:1": rec}})

    def test_never_while_a_resting_entry_exists_nor_after_its_timeout_cancel(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        self.write_pending()
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 0)
        self.assertEqual(self.marks(), {("ETHUSDT", "LONG"): "order_placed"})
        t251.write_json(st.PENDING_ENTRIES_FILE, {"entries": {}})   # the guardian's timeout-cancel popped it
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER + 5000), 0)
        self.assertEqual(self.shadow(), {})

    def test_resting_entry_under_a_newer_sha_of_the_same_candidate_is_an_order(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        self.write_pending(sha=SHA3, placed=TS + 300)
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 0)
        self.assertEqual(self.marks(), {("ETHUSDT", "LONG"): "order_placed"})

    def test_resting_entry_of_another_symbol_direction_or_older_does_not_block(self):
        for symbol, pending in (("ETHUSDT", dict(symbol="SOLUSDT", placed=TS + 300)),
                                ("BNBUSDT", dict(placed=TS - 3600, symbol="ADAUSDT")),   # not even this symbol
                                ("XRPUSDT", dict(symbol="XRPUSDT", placed=TS - 3600)),    # before this dossier
                                ("DOGEUSDT", dict(symbol="DOGEUSDT", placed=TS + 300, direction="SHORT")),
                                ("LTCUSDT", dict(symbol="LTCUSDT", placed=TS + 300, env="testnet"))):
            with self.subTest(symbol=symbol):
                self.approved([cand(symbol)], sha=SHA2 if symbol == "ETHUSDT" else SHA3)
                self.snapshot()
                self.write_pending(sha=SHA, **pending)
                self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 1)
                st.rewrite_jsonl(st.SHADOW_TRADES_FILE, [])   # keep the #262 window dedupe out of the next step

    def test_the_registry_is_read_before_the_audit(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        order = []
        real_exists = os.path.exists

        def spy(path):
            if path in (st.PENDING_ENTRIES_FILE, st.TRADES_AUDIT_FILE):
                order.append(os.path.basename(path))
            return real_exists(path)

        with patch.object(st.os.path, "exists", side_effect=spy):
            st.sweep_approved_ledger(now_ts=AFTER)
        self.assertEqual(order, ["pending_entries_file", "trades_audit_file"])

    def test_unreadable_registry_registers_nothing(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        with open(st.PENDING_ENTRIES_FILE, "w") as f:
            f.write("{broken")
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 0)
        self.assertEqual((self.marks(), self.shadow()), ({}, {}))

    def test_no_double_count_with_the_hook_denial_row(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        t251.write_jsonl(st.GATE_DENIALS_FILE, [{
            "gate": st.POST_APPROVAL_GATE, "ts": TS + 3, "symbol": "ETHUSDT", "direction": "LONG", "entry": 10.0,
            "stop_loss": 9.5, "tp1": 10.9, "tp2": 12.0, "score": 85, "dossier_sha256": SHA2, "env": "PROD",
            "book": []}])
        self.assertEqual(st.register_from_gate_denials(now_ts=AFTER), 1)
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 0)
        rows = st.load_jsonl(st.SHADOW_TRADES_FILE)
        self.assertEqual([(r["gate"], r["reason"]) for r in rows], [(st.POST_APPROVAL_GATE, "delta_denied")])
        self.assertEqual(self.marks(), {("ETHUSDT", "LONG"): "deduped"})

    def test_no_double_count_with_a_rejected_row_of_the_same_dossier(self):
        self.approved([cand("ETHUSDT")])
        self.snapshot()
        st.register_shadow_trade("ETHUSDT", "LONG", 10.0, 9.5, 10.9, 12.0, 10.0, dossier_sha256=SHA2,
                                 gate="OTHER", registered_at_ts=TS)
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 0)
        self.assertEqual(len(st.load_jsonl(st.SHADOW_TRADES_FILE)), 1)

    def test_rejected_row_of_another_gate_in_the_window_does_not_swallow_the_late_row(self):
        self.approved([cand("ETHUSDT")], sha=SHA3)
        self.snapshot()
        st.register_shadow_trade("ETHUSDT", "LONG", 10.0, 9.5, 10.9, 12.0, 10.0, dossier_sha256=SHA,
                                 gate="OTHER", registered_at_ts=TS - 600)
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 1)

    def test_testnet_dossier_never_registers(self):
        self.approved([cand("ETHUSDT")], env="testnet")
        self.snapshot()
        self.assertEqual(self.ledger_rows()[0]["target_env"], "testnet")
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 0)
        self.assertEqual((self.marks(), self.shadow()), ({("ETHUSDT", "LONG"): "not_prod"}, {}))

    def test_yolo_is_registered_but_flagged(self):
        self.approved([cand("PEPEUSDT", tier="YOLO", is_yolo=True, score=60)])
        self.snapshot()
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 1)
        self.assertIs(self.shadow()["PEPEUSDT"]["is_yolo"], True)

    def test_an_old_dossier_still_registers_at_its_own_time(self):
        old = NOW - 3 * 86400   # a weekend gap between the approval and the next intake
        self.approved([cand("ETHUSDT")], ts=old, valid=old + 1200)
        self.snapshot()
        self.assertEqual(st.sweep_approved_ledger(), 1)
        self.assertEqual(self.shadow()["ETHUSDT"]["registered_at_ts"], old)
        self.assertEqual(self.marks(), {("ETHUSDT", "LONG"): "registered"})

    def test_missing_prices_are_marked(self):
        c = cand("ETHUSDT")
        c["tp2"] = None
        self.approved([c])
        self.snapshot()
        self.assertEqual(st.sweep_approved_ledger(now_ts=AFTER), 0)
        self.assertEqual(self.marks(), {("ETHUSDT", "LONG"): "no_prices"})

    def test_audit_cli_sweeps(self):
        self.approved([cand("ETHUSDT")], ts=NOW - 3000, valid=NOW - 1800)
        self.snapshot()
        out = io.StringIO()
        with patch.object(sys, "argv", ["shadow_tracker.py", "--audit"]), \
                patch.object(st, "audit_shadow_trades", return_value={}), \
                patch.object(st, "print_shadow_dashboard"), contextlib.redirect_stdout(out):
            st.main()
        self.assertIn("Registered 1 approved-but-not-executed candidate(s)", out.getvalue())
        self.assertEqual(set(self.shadow()), {"ETHUSDT"})

    def test_intake_prints_the_count(self):
        self.approved([cand("ETHUSDT")], ts=NOW - 3000, valid=NOW - 1800)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            st._register_and_print()
        self.assertIn("approved-but-not-executed 1", out.getvalue())

    def test_the_ledger_stays_in_the_shadow_directory(self):
        self.assertEqual(os.path.dirname(st.approved_ledger_path()), self.dir)


# =============================================================================
# 3. Bucket math and report
# =============================================================================
def srow(i, score, classification="FALSE_NEGATIVE", pnl=2.7, source="approved_not_executed", gate=NOT_EXECUTED_GATE,
         field="dossier_score", **extra):
    r = t251.resolved_row(f"r{i}", f"d{i}", classification=classification, pnl=pnl, gate=gate)
    r.update({field: score, "source": source, "registered_at_ts": 10000 + i * 7200, "vol_ratio": 1.5,
              "max_favorable_excursion_pct": 1.0, "max_adverse_excursion_pct": -0.5})
    r.update(extra)
    return r


class TestBuckets(unittest.TestCase):

    def run_rows(self, rows, cal=None):
        return sa.run_score_bucket_analysis(rows, cal, resamples=50)

    def by_label(self, out, view="dossier_score"):
        return {b["bucket"]: b for b in out["views"][view]["buckets"]}

    def test_edges(self):
        cases = {54: "out_of_range", 55: "55-64", 64: "55-64", 65: "65-74", 74: "65-74", 75: "75-79", 79: "75-79",
                 80: "80-89", 89: "80-89", 90: "90-95", 95: "90-95", 96: "out_of_range"}
        out = self.run_rows([srow(s, s) for s in cases])
        buckets = self.by_label(out)
        view = out["views"]["dossier_score"]
        self.assertEqual(list(buckets), ["55-64", "65-74", "75-79", "80-89", "90-95"])
        self.assertEqual(list(buckets), scal.BUCKET_LABELS)
        for label in scal.BUCKET_LABELS:
            self.assertEqual(buckets[label]["n"], sum(1 for v in cases.values() if v == label), label)
        self.assertEqual((view["out_of_range"], view["unscored"]), (2, 0))

    def test_insufficient_sample_below_30(self):
        rows = [srow(i, 85) for i in range(29)]
        self.assertTrue(self.by_label(self.run_rows(rows))["80-89"]["insufficient_sample"])
        rows.append(srow(29, 85))
        b = self.by_label(self.run_rows(rows))["80-89"]
        self.assertEqual((b["n"], b["insufficient_sample"]), (30, False))
        self.assertIn("insufficient_sample", sa.format_score_bucket_report(self.run_rows(rows[:5])))

    def test_hit_rate_mean_r_and_excursions(self):
        rows = [srow(0, 85), srow(1, 85), srow(2, 85, "TRUE_NEGATIVE", -1.5)]
        b = self.by_label(self.run_rows(rows))["80-89"]
        self.assertEqual((b["tp1_hits"], b["sl_hits"], b["n_conclusive"]), (2, 1, 3))
        self.assertAlmostEqual(b["hit_rate"], 2 / 3, places=4)
        self.assertAlmostEqual(b["mean_r"], (1.8 + 1.8 - 1.0) / 3, places=4)
        self.assertEqual((b["mean_mfe_pct"], b["mean_mae_pct"]), (1.0, -0.5))
        self.assertIsNotNone(b["ci95_low"])

    def test_expired_rows_count_zero_r(self):
        rows = [srow(0, 85), srow(1, 85, "EXPIRED", 0.0), srow(2, 85, "EXPIRED_UNTRIGGERED", None)]
        b = self.by_label(self.run_rows(rows))["80-89"]
        self.assertEqual((b["n"], b["expired"]), (3, 2))
        self.assertAlmostEqual(b["mean_r"], 0.6, places=4)
        self.assertEqual(b["hit_rate"], 1.0)               # EXPIRED rows are not conclusive
        self.assertEqual(b["mean_mfe_pct"], 1.0)           # and carry no excursion

    def test_rows_without_a_shadow_r_are_counted_apart(self):
        b = self.by_label(self.run_rows([srow(0, 85), srow(1, 85, target_dollar_risk=0)]))["80-89"]
        self.assertEqual((b["n"], b["no_r"]), (1, 1))

    def test_split_by_source_and_gate(self):
        rows = [srow(0, 85), srow(1, 85, "TRUE_NEGATIVE", -1.5, source="approved_not_executed",
                                  gate="DELTA_GATE_POST_APPROVAL"),
                srow(2, 85, source="rejected", gate="DRY_VOLUME")]
        b = self.by_label(self.run_rows(rows))["80-89"]
        self.assertEqual({k: v["n"] for k, v in b["by_source"].items()}, {"approved_not_executed": 2, "rejected": 1})
        self.assertEqual({k: v["n"] for k, v in b["by_gate"].items()},
                         {NOT_EXECUTED_GATE: 1, "DELTA_GATE_POST_APPROVAL": 1, "DRY_VOLUME": 1})
        self.assertEqual(b["by_gate"]["DELTA_GATE_POST_APPROVAL"]["sl_hits"], 1)

    def test_yolo_is_excluded_and_counted(self):
        out = self.run_rows([srow(0, 85), srow(1, 85, is_yolo=True)])
        self.assertEqual(self.by_label(out)["80-89"]["n"], 1)
        self.assertEqual(out["views"]["dossier_score"]["yolo_excluded"], 1)

    def test_radar_view_covers_only_rows_without_a_dossier_score(self):
        rows = [srow(0, None, source="rejected", gate="OTHER", radar_score=85.0),
                srow(1, 85, radar_score=85.0),                 # has a dossier score: dossier view only
                srow(2, None, source="rejected", gate="OTHER")]  # no score at all
        out = self.run_rows(rows)
        self.assertEqual(self.by_label(out, "radar_score")["80-89"]["n"], 1)
        self.assertEqual(self.by_label(out)["80-89"]["n"], 1)
        self.assertEqual(out["views"]["radar_score"]["unscored"], 1)
        self.assertEqual(out["views"]["dossier_score"]["unscored"], 2)

    def test_real_vs_shadow_side_by_side(self):
        cal = {"buckets": {"80-89": {"n": 12, "expectancy_r_net": 0.31}}}
        out = self.run_rows([srow(0, 85)], cal)
        buckets = self.by_label(out)
        self.assertEqual(buckets["80-89"]["real"], {"n": 12, "expectancy_r_net": 0.31})
        self.assertEqual(buckets["55-64"]["real"], {"n": 0, "expectancy_r_net": None})
        self.assertTrue(out["real_store"])
        text = sa.format_score_bucket_report(out)
        self.assertIn("real PROD n=12 mean net +0.310R", text)
        self.assertIn("SIMULATED", text)
        self.assertIn("not_executed", text)

    def test_without_a_store(self):
        out = self.run_rows([srow(0, 85)])
        self.assertIsNone(self.by_label(out)["80-89"]["real"])
        self.assertFalse(out["real_store"])
        self.assertIn("n/a (no store)", sa.format_score_bucket_report(out))
        self.assertIsNone(self.by_label(self.run_rows([srow(0, 85)], {"no": "buckets"}))["80-89"]["real"])

    def test_empty_input(self):
        out = self.run_rows([])
        self.assertTrue(out["simulated"])
        self.assertEqual(sum(b["n"] for b in out["views"]["dossier_score"]["buckets"]), 0)
        self.assertIn("APPLICATION F", sa.format_score_bucket_report(out))


# =============================================================================
# 4. FER / regret / calibration / gate counts unchanged
# =============================================================================
class TestUnchangedWithAdvisoryRows(LedgerBase):

    def base_rows(self):
        excursions = {"max_favorable_excursion_pct": 1.0, "max_adverse_excursion_pct": -0.5, "vol_ratio": 1.2}
        return [t251.resolved_row("a", "d1", pnl=2.7, score=85, blockers=[t251.RESTING_EXPIRED],
                                  activated_at_ts=1000, **excursions),
                t251.resolved_row("b", "d2", "TRUE_NEGATIVE", -1.5, gate="DRY_VOLUME", activated_at_ts=1000,
                                  resolved_at_ts=4000, **dict(excursions, vol_ratio=0.4))]

    def advisory_rows(self):
        return [srow(10, 85, "TRUE_NEGATIVE", -1.5, reason="not_executed", activated_at_ts=1000,
                     book=[], blockers=[t251.RESTING_EXPIRED]),
                srow(11, 90, "FALSE_NEGATIVE", 2.7, reason="not_executed", activated_at_ts=1000,
                     gate="DELTA_GATE", blockers=[t251.RESTING_EXPIRED])]

    def test_analytics_outputs_are_equal(self):
        base, extra = self.base_rows(), self.base_rows() + self.advisory_rows()
        for fn in (sa.run_calibration_analysis, sa.run_alpha_leakage_analysis, sa.run_dodge_audit,
                   sa.run_intraday_hygiene_audit):
            self.assertEqual(fn(base), fn(extra), fn.__name__)
        logs = tempfile.mkdtemp()
        with_rows = sa.delta_gate_analysis(extra, logs, resamples=20)
        without = sa.delta_gate_analysis(base, logs, resamples=20)
        self.assertEqual(json.dumps(with_rows, sort_keys=True), json.dumps(without, sort_keys=True))

    def test_hook_denial_rows_stay_in_regret(self):
        denial = srow(12, 80, reason="delta_denied", gate="DELTA_GATE_POST_APPROVAL",
                      blockers=[t251.RESTING_EXPIRED])
        logs = tempfile.mkdtemp()
        out = sa.delta_gate_analysis([denial], logs, resamples=20)
        self.assertEqual(out["regret"]["counts"]["rows"], 1)

    def test_efficacy_metrics_and_gate_counts_are_equal(self):
        base = self.base_rows()
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, base)
        t251.write_jsonl(st.SHADOW_TRADES_FILE, [{"symbol": "X", "direction": "LONG", "status": "ACTIVE",
                                                  "target_dollar_risk": 1.5}])
        before = json.dumps(st.calculate_efficacy_metrics(), sort_keys=True)
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, base + self.advisory_rows())
        t251.write_jsonl(st.SHADOW_TRADES_FILE, [{"symbol": "X", "direction": "LONG", "status": "ACTIVE",
                                                  "target_dollar_risk": 1.5},
                                                 dict(self.advisory_rows()[0], status="PENDING_TRIGGER")])
        m = st.calculate_efficacy_metrics()
        self.assertEqual(json.dumps(m, sort_keys=True), before)
        self.assertEqual(m["gate_counts"], {"DELTA_GATE": 1, "DRY_VOLUME": 1})

    def test_the_buckets_do_see_the_advisory_rows(self):
        out = sa.run_score_bucket_analysis(self.base_rows() + self.advisory_rows(), None, resamples=20)
        buckets = {b["bucket"]: b for b in out["views"]["dossier_score"]["buckets"]}
        self.assertEqual((buckets["80-89"]["n"], buckets["90-95"]["n"]), (1, 1))


# =============================================================================
# 5. Guard: shadow rows never reach the real store or a gate
# =============================================================================
class TestNoGateUse(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.logs = os.path.join(self.ws, "logs")
        os.makedirs(os.path.join(self.logs, "evaluations"))
        for p in (patch("trading_scorecard._workspace_dir", return_value=self.ws),
                  patch("execute_futures_trade.send_signed_request", side_effect=AssertionError("Binance call")),
                  patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test"))):
            p.start()
            self.addCleanup(p.stop)
        self.store_path = os.path.join(self.logs, "score_calibration.json")
        self.store = {"schema_version": 1, "generated_at_ts": NOW, "env": "PROD", "min_trades": 30,
                      "score_schema_version": scal.SCORE_SCHEMA_VERSION,
                      "trades": {"ETHUSDT|LONG|1": {"symbol": "ETHUSDT", "direction": "LONG", "env": "prod",
                                                    "status": "closed", "dossier_score": 82, "realized_r_net": 0.5,
                                                    "score_schema_version": scal.SCORE_SCHEMA_VERSION}},
                      "buckets": {label: {"n": 1 if label == "80-89" else 0, "expectancy_r_net": 0.5 if label ==
                                          "80-89" else None} for label in scal.BUCKET_LABELS},
                      "unscored": 0, "out_of_range": 0, "excluded_schema": 0}
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(self.store, f)
        with open(self.store_path, "rb") as f:
            self.before = f.read()
        t251.write_jsonl(os.path.join(self.logs, "shadow_resolved.jsonl"),
                         [srow(i, 85) for i in range(35)] + [srow(100, None, source="rejected", gate="OTHER",
                                                                  radar_score=70.0)])

    def assert_store_untouched(self):
        with open(self.store_path, "rb") as f:
            self.assertEqual(f.read(), self.before)

    def test_scorecard_leaves_the_store_alone_and_reports_simulated(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sc.main(["--env", "prod"]), 0)
        self.assert_store_untouched()
        card = sc.generate_scorecard("prod")
        block = card["shadow_score_buckets"]
        self.assertIs(block["simulated"], True)
        self.assertNotIn("shadow_score_buckets", card["score_calibration"])
        buckets = {b["bucket"]: b for b in block["views"]["dossier_score"]["buckets"]}
        self.assertEqual((buckets["80-89"]["n"], buckets["80-89"]["real"]["n"]), (35, 1))
        self.assertFalse(buckets["80-89"]["insufficient_sample"])
        self.assertEqual(card["score_calibration"]["buckets"][3]["n"], 1)   # the real bucket ignores 35 shadow rows
        self.assertIn("SHADOW SCORE BUCKETS (SIMULATED", out.getvalue())
        with open(self.store_path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["trades"]), 1)

    def test_scorecard_json_carries_the_block_top_level(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sc.main(["--env", "prod", "--json"])
        self.assertTrue(json.loads(out.getvalue())["shadow_score_buckets"]["simulated"])

    def test_scorecard_block_survives_a_missing_store(self):
        os.remove(self.store_path)
        block = sc.generate_scorecard("prod")["shadow_score_buckets"]
        self.assertIsNone(block["views"]["dossier_score"]["buckets"][3]["real"])

    def test_analytics_leaves_the_store_alone(self):
        out = io.StringIO()
        with patch.object(sa, "RESOLVED_FILE", os.path.join(self.logs, "shadow_resolved.jsonl")), \
                patch.object(sa, "LOGS_DIR", self.logs), contextlib.redirect_stdout(out):
            sa.main(["--resamples", "20"])
        self.assertIn("APPLICATION F", out.getvalue())
        self.assertIn("real PROD n=1 mean net +0.500R", out.getvalue())
        self.assert_store_untouched()
        js = io.StringIO()
        with patch.object(sa, "RESOLVED_FILE", os.path.join(self.logs, "shadow_resolved.jsonl")), \
                patch.object(sa, "LOGS_DIR", self.logs), contextlib.redirect_stdout(js):
            sa.main(["--json", "--resamples", "20"])
        self.assertIn("score_buckets", json.loads(js.getvalue()))
        self.assert_store_untouched()

    def test_largest_risk_line_ignores_advisory_rows(self):
        big = srow(500, 85, reason="not_executed", target_dollar_risk=999.0)
        t251.write_jsonl(os.path.join(self.logs, "shadow_resolved.jsonl"), [srow(1, 85), big])
        out = io.StringIO()
        with patch.object(sa, "RESOLVED_FILE", os.path.join(self.logs, "shadow_resolved.jsonl")), \
                patch.object(sa, "LOGS_DIR", self.logs), contextlib.redirect_stdout(out):
            sa.main(["--resamples", "20"])
        line = next(l for l in out.getvalue().splitlines() if l.startswith("Largest target_dollar_risk"))
        self.assertNotIn("r500", line)

    def test_nothing_that_gates_imports_shadow_code(self):
        pattern = re.compile(r"^\s*(?:from|import)\s+\S*shadow", re.M)
        for rel in ("scripts/utils/score_calibration.py", "scripts/execute_futures_trade.py",
                    "scripts/hooks/pre_trade_guard.py", "scripts/utils/gate_limits.py",
                    "scripts/utils/portfolio_exposure.py"):
            with open(os.path.join(BASE_DIR, rel), encoding="utf-8") as f:
                self.assertIsNone(pattern.search(f.read()), rel)


if __name__ == "__main__":
    unittest.main()
