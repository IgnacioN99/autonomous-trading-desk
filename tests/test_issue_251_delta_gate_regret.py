#!/usr/bin/env python3
"""
test_issue_251_delta_gate_regret.py - shadow desk: opportunity cost of the delta gate (issue #251).

Covers the typed rejection reason (normalize_rejected_candidates, gate_source dossier vs heuristic fallback, old
dossiers), the blocker snapshot (resting / position kinds, null score, env filter, read error), idempotency on
dossier_sha256 (also after resolution), regret_R (expired-unfilled blocker 0 R, filled blocker, unresolved blocker,
EXPIRED blocked row), the seeded cluster bootstrap, insufficient_sample, the policy replay on a hand-built event log,
back-compat with rows written before #251 and the scorecard's one-line summary.
Hermetic: temp directories only (every shadow_tracker path redirected), klines mocked, Binance / network blocked.
"""

import io
import os
import sys
import json
import time
import tempfile
import unittest
import contextlib
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import shadow_tracker as st
import shadow_analytics as sa
import trading_scorecard as sc

SHA = "a" * 64


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def opp(symbol, direction="LONG", vol=1.6, confidence=None, **extra):
    o = {"symbol": symbol, "direction": direction, "vol_ratio": vol, "current_price": 10.0, "trigger_price": 10.0,
         "sl_price": 9.5 if direction == "LONG" else 10.5, "tp1_price": 10.9 if direction == "LONG" else 9.1,
         "tp2_price": 12.0 if direction == "LONG" else 8.0, "target_dollar_risk": 1.5}
    if confidence is not None:
        o["confidence"] = confidence
    o.update(extra)
    return o


class TrackerBase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name in ("SHADOW_TRADES_FILE", "SHADOW_RESOLVED_FILE", "BRIEF_FILE", "DOSSIER_FILE",
                     "PENDING_ENTRIES_FILE", "SESSION_STATE_FILE", "TRADES_AUDIT_FILE"):
            p = patch.object(st, name, os.path.join(self.dir, name.lower()))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(st, "LOGS_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        p = patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test"))
        p.start()
        self.addCleanup(p.stop)

    def brief(self, opps):
        write_json(st.BRIEF_FILE, {"filtered_opportunities": opps})

    def dossier(self, rejected=None, sha=SHA, status="REJECTED", approved=(), env="PROD", omit_field=False):
        raw = {"status": status, "target_env": env, "approved_symbols": list(approved), "approved_candidates": []}
        if not omit_field:
            raw["rejected_candidates"] = rejected
        record = {"status": status, "approved_symbols": list(approved), "raw_payload": raw}
        if sha is not None:
            record["provenance"] = {"sha256": sha}
        write_json(st.DOSSIER_FILE, record)

    def rows(self):
        return {r["symbol"]: r for r in st.load_jsonl(st.SHADOW_TRADES_FILE)}


class TestNormalize(unittest.TestCase):

    def test_valid_entries(self):
        entries, flags = st.normalize_rejected_candidates([
            {"symbol": "fetusdt", "direction": "long", "score": 85, "gate": "delta_gate", "detail": " K1 BLOCKED "}])
        self.assertEqual(flags, [])
        self.assertEqual(entries, [{"symbol": "FETUSDT", "direction": "LONG", "score": 85.0, "gate": "DELTA_GATE",
                                    "detail": "K1 BLOCKED", "flags": []}])

    def test_unknown_missing_and_malformed(self):
        entries, flags = st.normalize_rejected_candidates([
            {"symbol": "A", "direction": "LONG", "gate": "SQUEEZE_RISK", "score": "high"},
            {"symbol": "B", "direction": "SIDEWAYS"},
            "not-a-dict",
            {"direction": "LONG", "gate": "DELTA_GATE"},
            {"symbol": "C", "direction": "SHORT", "gate": 7, "score": True, "detail": ["x"]},
        ])
        self.assertEqual([e["symbol"] for e in entries], ["A", "B", "C"])
        a, b, c = entries
        self.assertEqual((a["gate"], a["gate_raw"], a["score"]), ("OTHER", "SQUEEZE_RISK", None))
        self.assertEqual(set(a["flags"]), {"unknown_gate", "invalid_score"})
        self.assertEqual((b["direction"], b["gate"], b["gate_raw"]), (None, "OTHER", None))
        self.assertEqual(set(b["flags"]), {"invalid_direction", "missing_gate"})
        self.assertEqual((c["gate"], c["score"], c["detail"]), ("OTHER", None, ""))
        self.assertEqual(flags, ["entry_2_not_a_dict", "entry_3_without_symbol"])

    def test_empty_and_non_list(self):
        self.assertEqual(st.normalize_rejected_candidates(None), ([], []))
        self.assertEqual(st.normalize_rejected_candidates([]), ([], []))
        entries, flags = st.normalize_rejected_candidates({"symbol": "A"})
        self.assertEqual((entries, flags), ([], ["rejected_candidates_not_a_list:dict"]))


class TestRegistration(TrackerBase):

    def test_typed_gate_vs_heuristic_fallback(self):
        self.brief([opp("FETUSDT", confidence=85), opp("TRXUSDT", vol=0.4, confidence=70),
                    opp("ETHUSDT", confidence=66)])
        self.dossier([{"symbol": "FETUSDT", "direction": "LONG", "score": 85, "gate": "DELTA_GATE",
                       "detail": "K1 BLOCKED: LONG_HEAVY"},
                      {"symbol": "TRXUSDT", "direction": "LONG", "score": None, "gate": "DRY_VOLUME",
                       "detail": "K2 FAKE_TIER_S"}])
        self.assertEqual(st.register_from_evaluation(), 3)
        rows = self.rows()
        fet, trx, eth = rows["FETUSDT"], rows["TRXUSDT"], rows["ETHUSDT"]
        self.assertEqual((fet["gate"], fet["gate_source"], fet["score"], fet["dossier_sha256"]),
                         ("DELTA_GATE", "dossier", 85.0, SHA))
        self.assertEqual(fet["gate_detail"], "K1 BLOCKED: LONG_HEAVY")
        self.assertEqual(fet["rejection_category"], "DELTA_GATE_OR_MACRO")  # back-compat key still written
        self.assertIn("blockers", fet)
        # typed entry without a score: the brief confidence
        self.assertEqual((trx["gate"], trx["gate_source"], trx["score"]), ("DRY_VOLUME", "dossier", 70.0))
        self.assertEqual(trx["rejection_category"], "DRY_VOLUME_FAKE_TIER_S")
        # no typed entry for ETH: flagged heuristic fallback, no blocker snapshot
        self.assertEqual((eth["gate"], eth["gate_source"], eth["score"]), ("OTHER", "heuristic_vol_ratio", 66.0))
        self.assertNotIn("blockers", eth)

    def test_old_dossier_without_field(self):
        self.brief([opp("AERO", vol=0.5), opp("VVV", vol=1.2)])
        self.dossier(omit_field=True, sha=None)
        self.assertEqual(st.register_from_evaluation(), 2)
        rows = self.rows()
        self.assertEqual((rows["AERO"]["gate"], rows["AERO"]["gate_source"]), ("DRY_VOLUME", "heuristic_vol_ratio"))
        self.assertEqual((rows["VVV"]["gate"], rows["VVV"]["gate_source"]), ("OTHER", "heuristic_vol_ratio"))
        self.assertEqual(rows["VVV"]["rejection_category"], "DELTA_GATE_OR_MACRO")
        self.assertIsNone(rows["VVV"]["dossier_sha256"])
        self.assertNotIn("gate_flags", rows["VVV"])

    def test_malformed_field_never_refuses(self):
        self.brief([opp("AERO", vol=1.5)])
        self.dossier(rejected="DELTA_GATE everywhere")
        self.assertEqual(st.register_from_evaluation(), 1)
        row = self.rows()["AERO"]
        self.assertEqual((row["gate"], row["gate_source"]), ("OTHER", "heuristic_vol_ratio"))
        self.assertEqual(row["gate_flags"], ["rejected_candidates_not_a_list:str"])

    def test_unknown_gate_maps_to_fallback_and_flags(self):
        self.brief([opp("GTCUSDT")])
        self.dossier([{"symbol": "GTCUSDT", "direction": "LONG", "gate": "SQUEEZE_RISK", "detail": "x"}])
        st.register_from_evaluation()
        row = self.rows()["GTCUSDT"]
        self.assertEqual((row["gate"], row["gate_source"], row["gate_flags"]), ("OTHER", "dossier", ["unknown_gate"]))

    def test_direction_must_match(self):
        self.brief([opp("SOLUSDT", direction="SHORT")])
        self.dossier([{"symbol": "SOLUSDT", "direction": "LONG", "gate": "DELTA_GATE"}])
        st.register_from_evaluation()
        self.assertEqual(self.rows()["SOLUSDT"]["gate_source"], "heuristic_vol_ratio")


class TestBlockerSnapshot(TrackerBase):

    def write_book(self):
        now = int(time.time())
        write_json(st.PENDING_ENTRIES_FILE, {"entries": {
            "prod:ZROUSDT:111": {"kind": "STOP_MARKET", "entry_id": "111", "symbol": "ZROUSDT", "direction": "LONG",
                                 "target_env": "prod", "trigger_or_limit_price": 2.0, "total_qty": 50.0,
                                 "placed_at_ts": now - 120, "score_meta": {"score": 80, "dossier_score": 80}},
            "prod:OPUSDT:222": {"kind": "LIMIT", "entry_id": "222", "symbol": "OPUSDT", "direction": "SHORT",
                                "target_env": "prod", "trigger_or_limit_price": 1.0, "total_qty": 30.0,
                                "placed_at_ts": now - 60},
            "testnet:ARBUSDT:333": {"kind": "LIMIT", "entry_id": "333", "symbol": "ARBUSDT", "direction": "LONG",
                                    "target_env": "testnet", "trigger_or_limit_price": 1.0, "total_qty": 10.0,
                                    "placed_at_ts": now},
            "prod:SUIUSDT:444": {"kind": "LIMIT", "entry_id": "444", "symbol": "SUIUSDT", "direction": "LONG",
                                 "target_env": "prod", "trigger_or_limit_price": 1.0, "total_qty": 10.0,
                                 "placed_at_ts": now},
        }})
        write_json(st.SESSION_STATE_FILE, {"target_env": "prod", "last_updated_ts": now, "active_positions": [
            {"symbol": "SUIUSDT", "direction": "LONG", "notional_usdt": 40.0, "entry_order_id": 9001,
             "entry_time_ts": now - 3600},
            {"symbol": "LINKUSDT", "direction": "LONG", "notional_usdt": 60.0, "entry_order_id": 9002,
             "entry_time_ts": now - 7200},
        ]})
        write_jsonl(st.TRADES_AUDIT_FILE, [
            {"symbol": "SUIUSDT", "direction": "LONG", "total_qty": 10, "timestamp": now - 3600, "score": 72},
            {"event": "failsafe", "symbol": "SUIUSDT"},
        ])

    def test_delta_gate_blockers_same_direction(self):
        self.write_book()
        self.brief([opp("FETUSDT", confidence=85)])
        self.dossier([{"symbol": "FETUSDT", "direction": "LONG", "score": 85, "gate": "DELTA_GATE"}])
        st.register_from_evaluation()
        row = self.rows()["FETUSDT"]
        self.assertNotIn("blockers_error", row)
        by_sym = {b["symbol"]: b for b in row["blockers"]}
        # SHORT OPUSDT is not a blocker; the TESTNET entry is filtered; SUIUSDT counts once, as the position
        self.assertEqual(set(by_sym), {"ZROUSDT", "SUIUSDT", "LINKUSDT"})
        self.assertEqual((by_sym["ZROUSDT"]["kind"], by_sym["ZROUSDT"]["score"], by_sym["ZROUSDT"]["entry_id"],
                          by_sym["ZROUSDT"]["notional"]), ("resting", 80.0, "111", 100.0))
        self.assertEqual((by_sym["SUIUSDT"]["kind"], by_sym["SUIUSDT"]["score"], by_sym["SUIUSDT"]["entry_id"]),
                         ("position", 72.0, "9001"))
        self.assertIsNotNone(by_sym["SUIUSDT"]["audit_ts"])
        self.assertEqual((by_sym["LINKUSDT"]["score"], by_sym["LINKUSDT"]["audit_ts"]), (None, None))  # no audit
        self.assertEqual({(i["symbol"], i["direction"]) for i in row["book"]},
                         {("ZROUSDT", "LONG"), ("OPUSDT", "SHORT"), ("SUIUSDT", "LONG"), ("LINKUSDT", "LONG")})

    def test_dossier_without_env_is_flagged_unfiltered(self):
        self.write_book()
        self.brief([opp("FETUSDT")])
        self.dossier([{"symbol": "FETUSDT", "direction": "LONG", "gate": "DELTA_GATE"}], env=None)
        st.register_from_evaluation()
        row = self.rows()["FETUSDT"]
        self.assertIs(row["book_env_unfiltered"], True)
        self.assertIn("ARBUSDT", {b["symbol"] for b in row["blockers"]})  # the TESTNET entry is not filtered

    def test_duplicate_resting_blockers_same_symbol(self):
        self.write_book()
        self.brief([opp("ZROUSDT", confidence=82)])
        self.dossier([{"symbol": "ZROUSDT", "direction": "LONG", "gate": "DUPLICATE_RESTING"}])
        st.register_from_evaluation()
        self.assertEqual([b["entry_id"] for b in self.rows()["ZROUSDT"]["blockers"]], ["111"])

    def test_read_error_yields_empty_blockers(self):
        write_json(st.PENDING_ENTRIES_FILE, "{not json")
        self.brief([opp("FETUSDT")])
        self.dossier([{"symbol": "FETUSDT", "direction": "LONG", "gate": "DELTA_GATE"}])
        self.assertEqual(st.register_from_evaluation(), 1)
        row = self.rows()["FETUSDT"]
        self.assertEqual((row["blockers"], row["book"]), ([], []))
        self.assertTrue(row["blockers_error"].startswith("JSONDecodeError"))

    def test_session_state_env_mismatch_is_an_error(self):
        write_json(st.SESSION_STATE_FILE, {"target_env": "testnet", "active_positions": []})
        snap = st.snapshot_book("PROD")
        self.assertEqual(snap["items"], [])
        self.assertIn("env", snap["error"])

    def test_missing_files_are_empty_sources(self):
        self.assertEqual(st.snapshot_book("PROD"), {"items": [], "error": None, "session_state_ts": None})


class TestIdempotency(TrackerBase):

    @patch("shadow_tracker.fetch_klines")
    def test_same_dossier_not_registered_twice_even_after_resolution(self, mock_klines):
        self.brief([opp("FETUSDT")])
        self.dossier([{"symbol": "FETUSDT", "direction": "LONG", "gate": "DELTA_GATE"}])
        self.assertEqual(st.register_from_evaluation(), 1)
        self.assertEqual(st.register_from_evaluation(), 0)
        now_ms = int(time.time()) * 1000
        mock_klines.return_value = [[now_ms, "9.9", "10.1", "9.4", "9.45", "1"]]  # trigger, then SL
        self.assertEqual(st.audit_shadow_trades()["newly_resolved"], 1)
        self.assertEqual(st.load_jsonl(st.SHADOW_TRADES_FILE), [])
        self.assertEqual(st.register_from_evaluation(), 0)  # resolved row still blocks a re-registration
        # a new dossier for the same candidate registers again, even within the 3600 s window
        self.dossier([{"symbol": "FETUSDT", "direction": "LONG", "gate": "DELTA_GATE"}], sha="b" * 64)
        self.assertEqual(st.register_from_evaluation(), 1)

    def test_rows_without_hash_keep_symbol_dedupe(self):
        self.brief([opp("FETUSDT")])
        self.dossier(omit_field=True, sha=None)
        self.assertEqual(st.register_from_evaluation(), 1)
        self.assertEqual(st.register_from_evaluation(), 0)


class TestEfficacyBackCompat(TrackerBase):

    def test_legacy_rows_keep_metrics_and_gain_gate_counts(self):
        write_jsonl(st.SHADOW_RESOLVED_FILE, [
            {"id": "1", "classification": "TRUE_NEGATIVE", "simulated_pnl_usdt": -1.5,
             "rejection_category": "DRY_VOLUME_FAKE_TIER_S"},
            {"id": "2", "classification": "FALSE_NEGATIVE", "simulated_pnl_usdt": 2.7,
             "rejection_category": "DELTA_GATE_OR_MACRO"},
            {"id": "3", "classification": "EXPIRED", "simulated_pnl_usdt": 0.0, "gate": "DELTA_GATE",
             "gate_source": "dossier"},
        ])
        m = st.calculate_efficacy_metrics()
        self.assertEqual((m["true_negatives"], m["false_negatives"], m["expired"]), (1, 1, 1))
        self.assertEqual(m["filter_efficacy_ratio_pct"], 50.0)
        self.assertEqual(m["gate_counts"], {"DRY_VOLUME": 1, "OTHER": 1, "DELTA_GATE": 1})
        self.assertEqual(m["gate_source_counts"], {"legacy_category": 2, "dossier": 1})


def resolved_row(rid, cluster, classification="FALSE_NEGATIVE", pnl=2.7, score=None, blockers=(), gate="DELTA_GATE",
                 **extra):
    r = {"id": rid, "symbol": rid.upper() + "USDT", "direction": "LONG", "classification": classification,
         "simulated_pnl_usdt": pnl, "target_dollar_risk": 1.5, "gate": gate, "gate_source": "dossier",
         "score": score, "dossier_sha256": cluster, "blockers": list(blockers), "registered_at_ts": 10000,
         "resolved_at_ts": 12000, "trigger_price": 10.0, "sl_price": 9.5}
    r.update(extra)
    return r


RESTING_EXPIRED = {"symbol": "ZROUSDT", "direction": "LONG", "kind": "resting", "score": 80.0, "entry_id": "111"}
POSITION = {"symbol": "SUIUSDT", "direction": "LONG", "kind": "position", "score": None, "entry_id": "9001",
            "audit_ts": 1000}
RESTING_FILLED = {"symbol": "VVVUSDT", "direction": "LONG", "kind": "resting", "score": 80.0, "entry_id": "222"}
RESTING_UNKNOWN = {"symbol": "PONSUSDT", "direction": "LONG", "kind": "resting", "score": 60.0, "entry_id": "333"}
ACTIONS = [{"type": "pending_timeout_cancel", "symbol": "ZROUSDT", "success": True, "dry_run": False,
            "detail": {"entry_id": "111"}},
           {"type": "pending_timeout_cancel", "symbol": "PONSUSDT", "success": True, "dry_run": True,
            "detail": {"entry_id": "333"}}]  # a dry run never counts
AUDIT = [{"symbol": "VVVUSDT", "direction": "LONG", "entry_order_id": "222", "timestamp": 2000, "total_qty": 1}]
OUTCOMES = [{"symbol": "SUIUSDT", "audit_ts": 1000.0, "status": "closed", "realized_r_net": 0.5},
            {"symbol": "VVVUSDT", "audit_ts": 2000.0, "status": "closed", "realized_r_net": -1.0}]


class TestRegret(unittest.TestCase):

    def rows(self):
        return [
            resolved_row("a", "d1", pnl=2.7, score=85, blockers=[RESTING_EXPIRED]),            # 1.8 - 0 = 1.8
            resolved_row("b", "d1", "TRUE_NEGATIVE", pnl=-1.5, score=70, blockers=[RESTING_EXPIRED]),  # -1 - 0
            resolved_row("c", "d2", pnl=2.7, score=90, blockers=[POSITION]),                   # 1.8 - 0.5 = 1.3
            resolved_row("d", "d3", "EXPIRED", pnl=0.0, score=95, blockers=[RESTING_FILLED]),  # 0 - (-1) = 1.0
            resolved_row("e", "d4", pnl=2.7, blockers=[RESTING_UNKNOWN]),                       # unresolved
            resolved_row("f", "d5", pnl=2.7, target_dollar_risk=None, blockers=[RESTING_EXPIRED]),  # no shadow R
            resolved_row("g", "d6", pnl=2.7, blockers=[], blockers_error="JSONDecodeError: x"),
            {"id": "legacy", "classification": "FALSE_NEGATIVE", "simulated_pnl_usdt": 2.7,
             "rejection_category": "DELTA_GATE_OR_MACRO", "vol_ratio": 1.4},  # pre-#251 row: OTHER, ignored
        ]

    def test_shadow_r(self):
        self.assertEqual(sa.shadow_r({"classification": "EXPIRED_UNTRIGGERED"}), 0.0)
        self.assertAlmostEqual(sa.shadow_r({"simulated_pnl_usdt": -1.5, "target_dollar_risk": 1.5}), -1.0)
        self.assertIsNone(sa.shadow_r({"simulated_pnl_usdt": 2.7}))
        self.assertIsNone(sa.shadow_r({"simulated_pnl_usdt": 2.7, "target_dollar_risk": 0}))

    def test_regret_pairs_and_counts(self):
        rep = sa.regret_report(self.rows(), ACTIONS, OUTCOMES, AUDIT, resamples=200, seed=1)
        pairs = {p["row_id"]: p for p in rep["pairs"]}
        self.assertEqual(set(pairs), {"a", "b", "c", "d"})
        self.assertEqual((pairs["a"]["regret_r"], pairs["a"]["blocker_kind"]), (1.8, "resting_expired_unfilled"))
        self.assertEqual(pairs["b"]["regret_r"], -1.0)
        self.assertEqual((pairs["c"]["regret_r"], pairs["c"]["blocker_kind"]), (1.3, "position"))
        self.assertEqual((pairs["d"]["regret_r"], pairs["d"]["blocker_kind"], pairs["d"]["expired"]),
                         (1.0, "resting_filled", True))
        self.assertEqual(rep["counts"], {"rows": 7, "no_shadow_r": 1, "no_blockers": 1, "blockers_error": 1,
                                         "expired_blocked_rows": 1, "blocker_unresolved": 1})
        cons, trig = rep["conservative"], rep["triggered_only"]
        self.assertEqual((cons["n"], cons["n_clusters"], cons["mean_regret_r"]), (4, 3, 0.775))
        self.assertTrue(cons["insufficient_sample"])
        self.assertEqual((trig["n"], trig["mean_regret_r"]), (3, 0.7))
        self.assertEqual({k: v["n"] for k, v in rep["by_blocker_kind"].items()},
                         {"position": 1, "resting_filled": 1, "resting_expired_unfilled": 2})
        self.assertEqual({k: v["n"] for k, v in rep["by_score_delta"].items()},
                         {"< 0": 1, "0-9": 1, "10-19": 1, "unknown": 1})
        self.assertEqual(list(rep["by_gate"]), ["DELTA_GATE"])
        self.assertEqual(rep["definition"], "conservative")
        self.assertIn(sa.SELECTION_BIAS_NOTE, rep["warnings"])
        self.assertIn(sa.GROSS_NET_NOTE, rep["warnings"])

    def test_bootstrap_deterministic_and_clustered(self):
        pairs = [{"cluster": "d1", "regret_r": v} for v in (2.0, -1.0, 0.5)] + [{"cluster": "d2", "regret_r": 1.0}]
        a = sa.bootstrap_mean_ci(pairs, resamples=500, seed=7)
        self.assertEqual(a, sa.bootstrap_mean_ci(pairs, resamples=500, seed=7))
        self.assertLessEqual(a["ci95_low"], a["mean_regret_r"])
        self.assertGreaterEqual(a["ci95_high"], a["mean_regret_r"])
        # One cluster: every resample draws the same cluster, so the CI collapses onto the mean ...
        one = sa.bootstrap_mean_ci([dict(p, cluster="d1") for p in pairs], resamples=500, seed=7)
        self.assertEqual((one["n_clusters"], one["ci95_low"], one["ci95_high"]), (1, 0.625, 0.625))
        # ... while the same values as independent clusters spread it.
        indep = sa.bootstrap_mean_ci([dict(p, cluster=i) for i, p in enumerate(pairs)], resamples=500, seed=7)
        self.assertEqual(indep["n_clusters"], 4)
        self.assertLess(indep["ci95_low"], indep["ci95_high"])

    def test_insufficient_sample_threshold(self):
        pairs = [{"cluster": i, "regret_r": 0.1} for i in range(sa.MIN_SAMPLE)]
        self.assertFalse(sa.bootstrap_mean_ci(pairs, resamples=50)["insufficient_sample"])
        self.assertTrue(sa.bootstrap_mean_ci(pairs[:-1], resamples=50)["insufficient_sample"])
        empty = sa.bootstrap_mean_ci([], resamples=50)
        self.assertEqual((empty["n"], empty["mean_regret_r"], empty["ci95_low"], empty["insufficient_sample"]),
                         (0, None, None, True))

    def test_report_text_has_no_conclusion_below_minimum(self):
        delta = {"regret": sa.regret_report(self.rows(), ACTIONS, OUTCOMES, AUDIT, resamples=50),
                 "replay": sa.replay_policies(self.rows(), sa.build_blocker_index(ACTIONS, OUTCOMES, AUDIT))}
        text = sa.format_delta_gate_report(delta["regret"], delta["replay"])
        self.assertIn("n=4 | n_clusters=3 insufficient_sample", text)
        self.assertIn("no conclusion", text)
        self.assertNotIn("CI excludes 0", text)
        self.assertNotIn("CI includes 0", text)
        self.assertLess(text.index("• conservative (first)"), text.index("• triggered_only"))


class TestReplay(unittest.TestCase):
    """Hand-built event log. Book of dossier d1 (ts 10000): resting LONG ZRO (notional 100, 10 min old, score 80,
    filled later at +0.5R) and a SHORT position (notional 20): LONG_HEAVY (delta_ratio 0.667). Candidates: F (score
    95, explicit notional 100, TP1 +1.8R) and A (score 85, derived notional 1.5 / 0.5 x 10 = 30, SL -1R). Dossier d2
    (ts 20000, same book, ZRO now old) blocks G (score 70, SL -1R)."""

    def rows(self):
        zro = {"symbol": "ZROUSDT", "direction": "LONG", "kind": "resting", "score": 80.0, "entry_id": "z1",
               "notional": 100.0, "since_ts": 9400}
        short = {"symbol": "OPUSDT", "direction": "SHORT", "kind": "position", "score": None, "entry_id": "p1",
                 "notional": 20.0, "since_ts": 5000}
        book = [zro, short]
        return [
            resolved_row("f", "d1", pnl=2.7, score=95, blockers=[zro], book=book, notional_usdt=100.0),
            resolved_row("a", "d1", "TRUE_NEGATIVE", pnl=-1.5, score=85, blockers=[zro], book=book,
                         resolved_at_ts=15000),
            resolved_row("g", "d2", "TRUE_NEGATIVE", pnl=-1.5, score=70, blockers=[zro], book=book,
                         registered_at_ts=20000, resolved_at_ts=22000),
            resolved_row("h", "d9", pnl=2.7, gate="DRY_VOLUME", book=book),  # other gate: not replayed
            resolved_row("old", "d8", pnl=2.7),                              # DELTA_GATE row without a book
        ]

    def index(self):
        return sa.build_blocker_index([], [{"symbol": "ZROUSDT", "audit_ts": 9900.0, "status": "closed",
                                            "realized_r_net": 0.5}],
                                      [{"symbol": "ZROUSDT", "entry_order_id": "z1", "timestamp": 9900}])

    def test_policy_totals(self):
        res = sa.replay_policies(self.rows(), self.index(), resting_age_min=30, resting_weight=0.5, swap_margin=10)
        p = res["policies"]
        self.assertEqual((res["n_events"], res["n_rows"], res["skipped"]["no_book"]), (2, 3, 1))
        self.assertEqual((res["blockers"], res["blocker_r_unknown"], res["notional_derived"]), (1, 0, 2))
        # (a) current: nothing placed; the blocker counts once although it blocked two dossiers
        self.assertEqual((p["current"]["total_r"], p["current"]["placed"]), (0.5, 0))
        # (b) after 30 min: ZRO (10 min old) does not count -> F (100) would tip the book LONG_HEAVY, A (30) passes
        b = p["resting_after_n_min"]
        self.assertEqual((b["total_r"], b["placed"], b["max_drawdown_r"]), (-0.5, 1, 1.0))
        self.assertEqual(b["placements"], [{"id": "a", "symbol": "AUSDT", "ts": 10000, "shadow_r": -1.0,
                                            "notional_derived": True}])
        # (b) fraction 0.5: ZRO at 50 vs SHORT 20 is still LONG_HEAVY -> nothing placed
        self.assertEqual((p["resting_fraction"]["total_r"], p["resting_fraction"]["placed"]), (0.5, 0))
        # (c) swap: F (95) beats ZRO (80) by >= 10 -> ZRO cancelled (its +0.5R gone), F placed (+1.8R)
        c = p["swap"]
        self.assertEqual((c["total_r"], c["placed"], c["swapped"]), (1.8, 1, 1))
        self.assertEqual([x["id"] for x in c["placements"]], ["f"])
        # exposure: current book stays LONG_HEAVY at both events
        e = p["current"]["exposure"]
        self.assertEqual((e["points"], e["heavy_share"], e["max_abs_delta_ratio"]), (2, 1.0, 0.666667))
        self.assertTrue(res["insufficient_sample"])
        for note in (sa.IN_SAMPLE_NOTE, sa.RANKING_NOTE, sa.SELECTION_BIAS_NOTE):
            self.assertIn(note, res["warnings"])

    def test_resting_blocker_that_filled_is_one_trade(self):
        """Audit round 1: ZRO z1 rests in event 1 and is a position with the same entry_id in event 2 (a filled
        resting entry keeps its entry_id). current counts its +0.5R once; swap cancels it at event 1, so it is
        neither in event 2's book nor in the contributions."""
        zro_resting = {"symbol": "ZROUSDT", "direction": "LONG", "kind": "resting", "score": 80.0, "entry_id": "z1",
                       "notional": 100.0, "since_ts": 9400}
        zro_position = {"symbol": "ZROUSDT", "direction": "LONG", "kind": "position", "score": 80.0,
                        "entry_id": "z1", "notional": 100.0, "since_ts": 15000, "audit_ts": 9900}
        short = {"symbol": "OPUSDT", "direction": "SHORT", "kind": "position", "score": None, "entry_id": "p1",
                 "notional": 20.0, "since_ts": 5000}
        rows = [
            resolved_row("f", "d1", pnl=2.7, score=95, blockers=[zro_resting], book=[zro_resting, short],
                         notional_usdt=100.0),
            resolved_row("g", "d2", "TRUE_NEGATIVE", pnl=-1.5, score=70, blockers=[zro_position],
                         book=[zro_position, short], registered_at_ts=20000, resolved_at_ts=22000),
        ]
        res = sa.replay_policies(rows, self.index(), swap_margin=10)
        p = res["policies"]
        self.assertEqual((res["blockers"], res["blocker_r_unknown"]), (1, 0))
        self.assertEqual((p["current"]["total_r"], p["current"]["placed"]), (0.5, 0))
        self.assertEqual((p["swap"]["total_r"], p["swap"]["placed"], p["swap"]["swapped"]), (1.8, 1, 1))
        # event 2 under swap: the position twin of the cancelled entry is gone, only the SHORT remains
        self.assertEqual(p["swap"]["exposure"]["series"][1]["net_notional"], -20.0)
        self.assertEqual(p["current"]["exposure"]["series"][1]["net_notional"], 80.0)

    def test_candidate_notional(self):
        self.assertEqual(sa.candidate_notional({"notional_usdt": 250.0}), (250.0, False))
        self.assertEqual(sa.candidate_notional({"target_dollar_risk": 1.5, "trigger_price": 10.0, "sl_price": 9.5}),
                         (30.0, True))
        self.assertEqual(sa.candidate_notional({"target_dollar_risk": 1.5, "trigger_price": 10.0,
                                                "sl_price": 10.0}), (None, False))


class TestAnalyticsBackCompat(unittest.TestCase):

    LEGACY = [{"symbol": "PUMPUSDT", "direction": "LONG", "vol_ratio": 0.4, "classification": "FALSE_NEGATIVE",
               "simulated_pnl_usdt": 2.7, "max_favorable_excursion_pct": 2.1, "max_adverse_excursion_pct": -0.4,
               "activated_at_ts": 1000, "resolved_at_ts": 4600, "rejection_reason": "Fake Tier S",
               "rejection_category": "DRY_VOLUME_FAKE_TIER_S", "target_dollar_risk": 1.5},
              {"symbol": "ZROUSDT", "direction": "LONG", "vol_ratio": 1.2, "classification": "TRUE_NEGATIVE",
               "simulated_pnl_usdt": -1.5, "max_favorable_excursion_pct": 0.2, "max_adverse_excursion_pct": -2.0,
               "activated_at_ts": 1000, "resolved_at_ts": 4000, "rejection_reason": "Rejected by delta gate",
               "rejection_category": "DELTA_GATE_OR_MACRO", "target_dollar_risk": 1.5}]

    def test_legacy_rows_through_analytics(self):
        logs = tempfile.mkdtemp()  # no guardian_actions / trade_outcomes / trades_audit: empty data
        delta = sa.delta_gate_analysis(self.LEGACY, logs, resamples=50)
        self.assertEqual(delta["regret"]["conservative"]["n"], 0)
        self.assertEqual(delta["regret"]["counts"]["rows"], 0)
        self.assertEqual(delta["replay"]["n_events"], 0)
        self.assertEqual(delta["replay"]["policies"]["current"]["total_r"], 0)
        self.assertIn("insufficient_sample", sa.format_delta_gate_report(delta["regret"], delta["replay"]))
        cal = sa.run_calibration_analysis(self.LEGACY + [resolved_row("x", "d1", vol_ratio=1.5)])
        self.assertEqual(sum(b["total_setups"] for b in cal.values()), 3)

    def test_cli_json_is_read_only(self):
        logs = tempfile.mkdtemp()
        resolved_path = os.path.join(logs, "shadow_resolved.jsonl")
        write_jsonl(resolved_path, self.LEGACY)
        before = sorted(os.listdir(logs))
        out = io.StringIO()
        with patch.object(sa, "RESOLVED_FILE", resolved_path), patch.object(sa, "LOGS_DIR", logs), \
                contextlib.redirect_stdout(out):
            sa.main(["--json", "--resamples", "20"])
        self.assertEqual(set(json.loads(out.getvalue())), {"regret", "replay"})
        text = io.StringIO()
        with patch.object(sa, "RESOLVED_FILE", resolved_path), patch.object(sa, "LOGS_DIR", logs), \
                contextlib.redirect_stdout(text):
            sa.main(["--resamples", "20"])
        self.assertIn("DODGE AUDIT & PROOF OF EDGE", text.getvalue())
        self.assertIn("DELTA GATE POLICY REPLAY", text.getvalue())
        self.assertEqual(sorted(os.listdir(logs)), before)


class TestScorecardLine(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.logs = os.path.join(self.ws, "logs")
        os.makedirs(os.path.join(self.logs, "evaluations"))
        for p in (patch("trading_scorecard._workspace_dir", return_value=self.ws),
                  patch("execute_futures_trade.send_signed_request", side_effect=AssertionError("Binance call")),
                  patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test")),
                  patch("shadow_tracker.calculate_efficacy_metrics",
                        side_effect=lambda: {"active_shadow_trades": 0, "total_resolved": 2})):
            p.start()
            self.addCleanup(p.stop)
        open(os.path.join(self.logs, "shadow_trades.jsonl"), "w").close()

    def test_line_when_data_exist(self):
        write_jsonl(os.path.join(self.logs, "shadow_resolved.jsonl"),
                    [resolved_row("a", "d1", pnl=2.7, score=85, blockers=[RESTING_EXPIRED])])
        write_jsonl(os.path.join(self.logs, "guardian_actions.jsonl"), ACTIONS)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sc.main(["--env", "prod"])
        s = sc.generate_scorecard("prod")
        self.assertEqual(s["shadow"]["delta_regret"]["n"], 1)
        self.assertEqual(s["shadow"]["delta_regret"]["mean_regret_r"], 1.8)
        self.assertTrue(s["shadow"]["delta_regret"]["insufficient_sample"])
        self.assertIn("Delta-gate regret (conservative", out.getvalue())
        self.assertIn("insufficient_sample", out.getvalue())

    def test_no_line_without_blocked_rows(self):
        write_jsonl(os.path.join(self.logs, "shadow_resolved.jsonl"), TestAnalyticsBackCompat.LEGACY)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sc.main(["--env", "prod"])
        self.assertNotIn("delta_regret", sc.generate_scorecard("prod")["shadow"])
        self.assertNotIn("Delta-gate regret", out.getvalue())


if __name__ == "__main__":
    unittest.main()
