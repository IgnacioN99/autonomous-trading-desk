#!/usr/bin/env python3
"""
test_issue_202_score_calibration.py - Issue #202 (with the #165 "#134 cap rule" and the PR #203 review notes).

The radar `confidence` is a heuristic point score, not a probability. Covered here:
- radar: structured score_components (sum == confidence, caps included), the unconditional 74 cap for rows that
  are not Tier S-eligible (#165), the 1.3x volume reason line, intraday Tier S at 80;
- dossier: `score` with the `conviction_pct` alias (never a rejection), dossier_sha256 on a copy of the candidate;
- brief sidecar logs/primed_brief_scores.json and the recorder's radar_snapshots (outside the provenance sha256);
- the entry audit record (MARKET and resting registration -> fill) and both outcome-row builders;
- utils/score_calibration.py (buckets, store merge, reasons) and the scorecard calibration block / PR #203 notes;
- the calibrated-bucket gate for autonomous Tier S in the executor and the guard (asks the user, never rejects with
  --confirmed; TESTNET, YOLO, A+/A and risk-reducing commands unaffected); the store is ground truth in the guard;
- prompt and docs wording.

Hermetic: temp workspaces, fake exchange, urlopen blocked, no writes to the real logs/.
"""

import contextlib
import datetime
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "hooks"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_guard_bypasses as tgb  # noqa: E402  (fixtures only; imported first, see test_issue_79)
import pre_trade_guard  # noqa: E402
import broad_market_radar as bmr  # noqa: E402
import intraday_radar as ir  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import record_evaluation as rec_eval  # noqa: E402
import trade_outcomes as to  # noqa: E402
import trading_scorecard as sc  # noqa: E402
import execute_futures_trade as eft  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
from utils import score_calibration as scal  # noqa: E402
import test_analytics_cli as tac  # noqa: E402  (fixtures only)
import test_trade_outcomes as tto  # noqa: E402  (fixtures only)
import test_trading_scorecard as tsc  # noqa: E402  (fixtures only)
import test_pending_entries as tpe  # noqa: E402  (fixtures only)
from test_exit_management import FakeExchange, offline, long_position  # noqa: E402

CONV_ID = "abcdef12-3456-7890-abcd-ef1234567890"
SCRIPT = "python3 scripts/execute_futures_trade.py"
CALIB_GATE = "Uncalibrated Tier S Score"


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    tgb.setUpModule()
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def write_agy_dossier(brain, dossier_path, candidates, created=None, target_env=None, snapshots=False):
    """Evaluator transcript + record exactly like record_evaluation.py --from-subagent (agy). Returns extraction."""
    created = created or datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
    tdir = os.path.join(brain, CONV_ID, ".system_generated", "logs")
    os.makedirs(tdir, exist_ok=True)
    payload = {"status": "APPROVED", "approved_candidates": candidates, "summary": "test"}
    if target_env:
        payload["target_env"] = target_env
    # Precondition Checklist consistent with the block (checked at record time, issue #27, and at trade time with
    # the K4 result token, issue #223)
    checklist = tgb.checklist_for(payload)
    steps = [{"step_index": 0, "source": "SYSTEM", "type": "USER_INPUT",
              "content": "sender=11111111-2222-3333-4444-555555555555"},
             {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
              "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "content": f"Master Dossier\n{checklist}<dossier_json>{json.dumps(payload)}</dossier_json>"}]
    tpath = os.path.join(tdir, "transcript.jsonl")
    with open(tpath, "w", encoding="utf-8") as f:
        for s in steps:
            f.write(json.dumps(s) + "\n")
    extracted = dp.extract_dossier_from_transcript(tpath)
    if dossier_path:
        os.makedirs(os.path.dirname(dossier_path), exist_ok=True)
        record = dp.build_record_from_extraction(extracted)
        if snapshots:
            tgb.add_radar_snapshots(record)
        with open(dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
    return extracted


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.brain = os.path.join(self.root, "brain")
        self.logs = os.path.join(self.root, "logs")
        self.dossier_path = os.path.join(self.logs, "evaluations", "latest_dossier.json")
        os.makedirs(os.path.join(self.logs, "evaluations"), exist_ok=True)


# =============================================================================
# 1. Radar: components, caps, reason line, intraday threshold
# =============================================================================
def _klines(vol_mult=1.0):
    """55 flat candles (100 +/- 1); the forming candle closes at 99 (a liquidity sweep at the local low for LONG)."""
    out = []
    for i in range(55):
        vol = 100.0 * (vol_mult if i == 53 else 1.0)
        close = 99.0 if i == 54 else 100.0
        out.append([1_000_000 + i * 900_000, "100.0", "101.0", "99.0", str(close), str(vol)])
    return out


def _scored(rsi, lower, upper=0.0, vol=1.0):
    with patch.object(bmr, "fetch_klines", return_value=_klines(vol)), \
         patch.object(bmr, "calculate_rsi", return_value=rsi), \
         patch.object(bmr, "calculate_ema", return_value=[99.0]), \
         patch.object(bmr.me, "candle_wick_pcts", return_value=(lower, upper)):
        return bmr.analyze_single_symbol("AAAUSDT")


class TestRadarScoreComponents(unittest.TestCase):

    def assertSumRule(self, row):
        self.assertEqual(sum(row["score_components"].values()), row["confidence"], row["score_components"])

    def test_ineligible_75_capped_at_74_with_cap_component(self):
        row = _scored(rsi=30, lower=55)            # 25 + 35 + 15 sweep = 75; vol 1.0x, wick < 60% -> ineligible
        self.assertEqual(row["confidence"], 74)
        self.assertEqual(row["score_components"], {"rsi": 25, "wick": 35, "sweep": 15, "cap": -1})
        self.assertFalse(row["tier_s_eligible"])
        self.assertEqual(row["tier_code"], "A+")
        self.assertSumRule(row)

    def test_volume_expansion_reason_line_and_points(self):
        row = _scored(rsi=30, lower=55, vol=1.5)   # +10 at 1.3x-1.8x, and 1.5x >= 1.4x makes it eligible
        self.assertEqual(row["confidence"], 85)
        self.assertEqual(row["score_components"], {"rsi": 25, "wick": 35, "volume": 10, "sweep": 15})
        self.assertIn("Volume expansion 1.5x average", row["reasons"])
        self.assertEqual(row["tier_code"], "S")
        self.assertSumRule(row)

    def test_ineligible_85_capped_and_reason_present(self):
        row = _scored(rsi=30, lower=55, vol=1.35)  # +10 but 1.35x < 1.4x: ineligible -> 74
        self.assertEqual(row["confidence"], 74)
        self.assertEqual(row["score_components"]["cap"], -11)
        self.assertIn("Volume expansion 1.4x average", row["reasons"])  # 1.35 printed with one decimal
        self.assertSumRule(row)

    def test_95_cap_booked(self):
        row = _scored(rsi=20, lower=70, vol=2.0)   # 35 + 35 + 20 + 15 = 105 -> 95
        self.assertEqual(row["confidence"], 95)
        self.assertEqual(row["score_components"]["cap"], -10)
        self.assertSumRule(row)

    def _enrich(self, cand, **micro):
        snap = dict(tac.micro_snapshot("AAAUSDT"), **micro)
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            return bmr.enrich_candidate_microstructure(cand)

    def test_enrichment_deltas_and_caps_keep_the_sum_rule(self):
        base = {"symbol": "AAAUSDT", "direction": "LONG", "reasons": [], "interval": "15m",
                "wick_candle_open_time": 1}
        # 75-79 on an ineligible row -> 74 (unconditional #165 cap)
        for start in (60, 61, 64):
            cand = self._enrich(dict(base, confidence=start, score_components={"rsi": start}),
                                absorption="BULLISH_ABSORPTION", absorption_desc="d")
            self.assertEqual(cand["confidence"], 74, start)
            self.assertEqual(sum(cand["score_components"].values()), 74)
            self.assertEqual(cand["score_components"]["absorption"], 15)
        # eligible row reaches S; a penalty and funding are negative points
        cand = self._enrich(dict(base, confidence=70, tier_s_eligible=True, score_components={"rsi": 35, "wick": 35}),
                            absorption="BULLISH_ABSORPTION", absorption_desc="d")
        self.assertEqual((cand["confidence"], cand["tier_code"]), (85, "S"))
        cand = self._enrich(dict(base, confidence=70, score_components={"rsi": 35, "wick": 35}),
                            regime="SHORT_BUILDUP", funding_rate_pct=0.05)
        self.assertEqual(cand["score_components"]["flow_penalty"], -30)
        self.assertEqual(cand["score_components"]["funding"], -15)
        self.assertEqual(sum(cand["score_components"].values()), cand["confidence"])
        # floor at 20 is booked as positive points; a legacy row without components starts from `base`
        cand = self._enrich(dict(base, confidence=45), regime="SHORT_BUILDUP", funding_rate_pct=0.05)
        self.assertEqual(cand["confidence"], 20)
        self.assertEqual(cand["score_components"], {"base": 45, "flow_penalty": -30, "funding": -15, "floor": 20})

    def test_intraday_tier_s_at_80(self):
        self.assertFalse(ir.intraday_tier_label(79).startswith("Tier S"))
        self.assertTrue(ir.intraday_tier_label(80).startswith("Tier S"))
        self.assertTrue(ir.intraday_tier_label(75).startswith("Tier A"))  # was Tier S before #202

    def test_text_output_says_score_not_percent(self):
        payload = bmr.build_scan_payload([dict(_scored(rsi=30, lower=55, vol=1.5), micro={})], "prod", "15m", 1, 0, 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bmr.print_text_report(payload)
        self.assertIn("-> score 85 (heuristic, not a probability)", out.getvalue())
        self.assertNotIn("-> 85%", out.getvalue())
        self.assertNotIn("Conviction", out.getvalue())


# =============================================================================
# 2-3. Dossier score field and sha256
# =============================================================================
class TestDossierScore(_Workspace):

    def test_score_alias_and_invalid_values(self):
        cases = [({"score": 85}, 85), ({"conviction_pct": 72}, 72), ({"score": 81, "conviction_pct": 60}, 81),
                 ({"score": 84.6}, 85), ({"score": "90"}, 90), ({"score": "high"}, None), ({"score": True}, None),
                 ({"score": -1}, None), ({"score": 101}, None), ({"score": float("nan")}, None), ({}, None),
                 ({"score": None, "conviction_pct": 66}, 66)]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                out = dp.normalize_candidates({"approved_candidates": [dict({"symbol": "x", "direction": "LONG"},
                                                                            **extra)]})
                self.assertEqual(out[0]["score"], expected)

    def test_bad_score_never_rejects_the_dossier(self):
        write_agy_dossier(self.brain, self.dossier_path,
                          [{"symbol": "SOLUSDT", "direction": "LONG", "tier": "A+", "score": "n/a"}])
        ok, reason, cand = dp.validate_dossier_for_trade("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertTrue(ok, reason)
        self.assertIsNone(cand["score"])

    def test_sha256_on_a_copy_of_the_candidate(self):
        extracted = write_agy_dossier(self.brain, self.dossier_path,
                                      [{"symbol": "SOLUSDT", "direction": "LONG", "tier": "S", "score": 85}])
        with open(self.dossier_path, encoding="utf-8") as f:
            before = f.read()
        ok, reason, cand = dp.validate_dossier_for_trade("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertTrue(ok, reason)
        self.assertEqual(cand["dossier_sha256"], extracted["sha256"])
        self.assertEqual(cand["score"], 85)
        cand["score"] = 1
        with open(self.dossier_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        self.assertNotIn("dossier_sha256", json.loads(before)["approved_candidates"][0])
        _, _, again = dp.validate_dossier_for_trade("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertEqual(again["score"], 85)

    def test_testnet_manual_dossier_has_no_sha(self):
        now = int(time.time())
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump({"timestamp_ts": now, "valid_until_ts": now + 1200, "status": "APPROVED",
                       "approved_symbols": ["SOLUSDT"], "provenance": {"source": "manual_testnet"},
                       "approved_candidates": [{"symbol": "SOLUSDT", "direction": "LONG"}]}, f)
        ok, _, cand = dp.validate_dossier_for_trade("SOLUSDT", "LONG", "testnet", base_dir=self.root)
        self.assertTrue(ok)
        self.assertIsNone(cand["dossier_sha256"])


# =============================================================================
# 4. Brief sidecar
# =============================================================================
class TestBriefSidecar(_Workspace):

    COMPONENTS = {"rsi": 35, "wick": 35, "volume": 20, "cap": -5}

    def screening(self):
        opp = {"symbol": "SOLUSDT", "direction": "LONG", "tier": "Tier S (x)", "tier_code": "S", "confidence": 85,
               "current_price": 100.0, "trigger_price": 101.0, "sl_price": 97.0, "tp1_price": 110.0,
               "tp2_price": 120.0, "rr_ratio": 4.0, "reasons": ["RSI", "Wick"], "score_components": self.COMPONENTS,
               "tier_s_eligible": True}
        return {"top_candidates": [opp], "run_id": "x",
                "yolo_slot": {"status": "ACTIVE", "candidates": [{"symbol": "PEPEUSDT", "trigger": 1.0}]}}

    def brief(self, screening):
        with patch.object(peb, "BRIEF_FILE", os.path.join(self.logs, "primed_brief.json")), \
             patch.object(peb, "ensure_fresh_state", return_value={"target_env": "prod"}), \
             patch.object(peb, "get_latest_screening_payload", return_value=screening), \
             patch.object(peb, "load_recent_insights", return_value=[]), \
             patch.object(peb, "build_risk_profile", return_value={}):
            return peb.assemble_primed_brief(target_env="prod")

    def test_sidecar_written_and_brief_unchanged_by_components(self):
        brief = self.brief(self.screening())
        with open(os.path.join(self.logs, "primed_brief_scores.json"), encoding="utf-8") as f:
            side = json.load(f)
        self.assertEqual((side["generated_at_ts"], side["env"]), (brief["generated_at_ts"], "PROD"))
        rows = {r["symbol"]: r for r in side["rows"]}
        self.assertEqual(rows["SOLUSDT"], {"symbol": "SOLUSDT", "direction": "LONG", "confidence": 85,
                                           "tier": "Tier S (x)", "tier_s_eligible": True,
                                           "squeeze_risk": False,  # issue #206: audit flag
                                           "score_components": self.COMPONENTS, "reasons": ["RSI", "Wick"]})
        self.assertEqual(rows["PEPEUSDT"]["direction"], "LONG")
        opp = brief["filtered_opportunities"][0]
        self.assertNotIn("score_components", opp)
        self.assertNotIn("tier_s_eligible", opp)
        plain = self.screening()
        for key in ("score_components", "tier_s_eligible"):
            plain["top_candidates"][0].pop(key)
        size_without = len(json.dumps(self.brief(plain)["filtered_opportunities"]))
        self.assertEqual(len(json.dumps(brief["filtered_opportunities"])), size_without)
        md = peb.format_markdown_brief(brief)
        self.assertIn("| Score |", md)
        self.assertIn("| score 85 |", md)
        self.assertNotIn("85%", md)
        self.assertNotIn("volume", md.split("Filtered Technical Setups")[1].split("YOLO Slot")[0].lower())

    def test_evaluator_never_points_at_the_sidecar(self):
        with open(os.path.join(BASE_DIR, ".agents", "agents", "isolated_market_evaluator", "agent.md"),
                  encoding="utf-8") as f:
            self.assertNotIn("primed_brief_scores", f.read())


# =============================================================================
# 5. Recorder radar_snapshots
# =============================================================================
class TestRecorderSnapshots(_Workspace):

    def record(self, sidecar=None, raw=None, candidates=None):
        extracted = write_agy_dossier(self.brain, None, candidates or [
            {"symbol": "SOLUSDT", "direction": "LONG", "tier": "S", "score": 85},
            {"symbol": "ETHUSDT", "direction": "SHORT", "tier": "A+", "score": 70}])
        ts = extracted["created_at_ts"]
        path = os.path.join(self.logs, "primed_brief_scores.json")
        if raw is not None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(raw)
        elif sidecar is not None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(sidecar(ts), f)
        rec = rec_eval._record_extracted(extracted, "prod", self.root, ts + 5, shadow=False, verbose=False)
        with open(self.dossier_path, encoding="utf-8") as f:
            stored = json.load(f)
        return extracted, rec, stored

    @staticmethod
    def sidecar(gen_offset=-60, env="PROD"):
        def build(ts):
            return {"generated_at_ts": ts + gen_offset, "env": env, "rows": [
                {"symbol": "SOLUSDT", "direction": "LONG", "confidence": 84, "tier": "Tier S (x)",
                 "tier_s_eligible": True, "score_components": {"rsi": 84}, "reasons": ["r"]}]}
        return build

    def test_snapshot_attached_outside_the_hash(self):
        extracted, _, stored = self.record(self.sidecar())
        snaps = stored["radar_snapshots"]
        self.assertEqual(snaps["SOLUSDT|LONG"]["radar_snapshot"]["confidence"], 84)
        self.assertIsNone(snaps["SOLUSDT|LONG"]["radar_snapshot_reason"])
        self.assertEqual(snaps["ETHUSDT|SHORT"], {"radar_snapshot": None, "radar_snapshot_reason": "no_match"})
        self.assertEqual(stored["provenance"]["sha256"], extracted["sha256"])
        self.assertTrue(dp.verify_provenance(stored)[0])
        with patch("time.time", return_value=extracted["created_at_ts"] + 10):
            ok, reason, cand = dp.validate_dossier_for_trade("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertTrue(ok, reason)
        self.assertEqual(cand["dossier_sha256"], extracted["sha256"])

    def test_null_reasons(self):
        cases = {"missing": dict(),
                 "unreadable": dict(raw="{not json"),
                 "stale": dict(sidecar=self.sidecar(gen_offset=-901)),
                 "stale_future": dict(sidecar=self.sidecar(gen_offset=+1))}
        for name, kwargs in cases.items():
            with self.subTest(name=name):
                self.setUp()
                _, _, stored = self.record(**kwargs)
                want = name.split("_")[0]
                for value in stored["radar_snapshots"].values():
                    self.assertEqual(value, {"radar_snapshot": None, "radar_snapshot_reason": want})
        self.setUp()
        _, _, stored = self.record(self.sidecar(gen_offset=-900))  # window edge is inclusive
        self.assertIsNotNone(stored["radar_snapshots"]["SOLUSDT|LONG"]["radar_snapshot"])
        self.setUp()
        _, _, stored = self.record(self.sidecar(env="TESTNET"))   # another env never matches
        self.assertEqual(stored["radar_snapshots"]["SOLUSDT|LONG"]["radar_snapshot_reason"], "no_match")

    def test_non_approved_dossier_gets_no_snapshot(self):
        record = {"status": "REJECTED", "approved_candidates": [], "timestamp_ts": int(time.time())}
        self.assertIsNone(rec_eval.build_radar_snapshots(record, self.root))


# =============================================================================
# 6. Entry audit record (MARKET path, resting registration -> fill)
# =============================================================================
CAND = {"symbol": "SOLUSDT", "direction": "LONG", "tier": "S", "score": 85, "dossier_sha256": "abc123"}
SNAP = {"symbol": "SOLUSDT", "direction": "LONG", "confidence": 84, "tier": "Tier S (x)",
        "score_components": {"rsi": 35, "wick": 35, "volume": 14}, "score_schema_version": 2}
FULL_META = {"score": 84, "score_tier": "Tier S (x)", "score_components": SNAP["score_components"],
             "score_source": "radar_snapshot", "score_missing_reason": None, "dossier_tier": "S",
             "dossier_score": 85, "dossier_sha256": "abc123", "score_schema_version": 2,  # issue #207
             "dossier_session": None}  # issue #270: CAND carries no dossier_session


def write_snapshot_dossier(ws, sha="abc123", snaps=None):
    path = os.path.join(ws, "logs", "evaluations", "latest_dossier.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"provenance": {"sha256": sha}, "radar_snapshots": snaps if snaps is not None else {
            "SOLUSDT|LONG": {"radar_snapshot": SNAP, "radar_snapshot_reason": None}}}, f)


class TestAuditRecordMarket(tpe.ExecutorHarness):

    def audit(self):
        rows = tpe.read_jsonl(self.ws, "trades_audit.jsonl")
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_market_entry_carries_score_fields(self):
        write_snapshot_dossier(self.ws)
        self.eval_result = (True, "ok", dict(CAND))
        res = self.execute()
        self.assertTrue(res["success"], res.get("error"))
        audit = self.audit()
        for key, value in FULL_META.items():
            self.assertEqual(audit[key], value, key)
        self.assertNotEqual(audit.get("provenance"), FULL_META)  # never named `provenance`

    def test_market_entry_without_candidate_is_all_null(self):
        res = self.execute()  # the harness gate returns no candidate
        self.assertTrue(res["success"], res.get("error"))
        audit = self.audit()
        for key in eft.SCORE_AUDIT_KEYS:
            self.assertIsNone(audit[key], key)

    def test_missing_or_foreign_snapshot_gives_reason(self):
        for setup, reason in ((lambda: None, "missing"),
                              (lambda: write_snapshot_dossier(self.ws, sha="other"), "dossier_changed"),
                              (lambda: write_snapshot_dossier(self.ws, snaps={}), "no_match"),
                              (lambda: write_snapshot_dossier(self.ws, snaps={"SOLUSDT|LONG": {
                                  "radar_snapshot": None, "radar_snapshot_reason": "stale"}}), "stale")):
            with self.subTest(reason=reason):
                self.setUp()
                setup()
                self.eval_result = (True, "ok", dict(CAND))
                self.assertTrue(self.execute()["success"])
                audit = self.audit()
                self.assertEqual((audit["score"], audit["score_source"], audit["score_missing_reason"]),
                                 (None, None, reason))
                self.assertEqual((audit["dossier_tier"], audit["dossier_score"], audit["dossier_sha256"]),
                                 ("S", 85, "abc123"))

    def test_resting_registration_stores_score_meta(self):
        write_snapshot_dossier(self.ws)
        self.eval_result = (True, "ok", dict(CAND))
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        rec = tpe.read_registry(self.ws)[res["pending_entry_key"]]
        self.assertEqual(rec["score_meta"], FULL_META)
        self.assertEqual(tpe.read_jsonl(self.ws, "trades_audit.jsonl"), [])

    def test_positional_registration_still_works(self):
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws):
            key, rec = eft.register_resting_entry("LIMIT", 1, "SOLUSDT", "LONG", "BUY", "SELL", "testnet", 100.0,
                                                  1.0, 97.0, 110.0, 120.0, 3, False, 10.0)
        self.assertNotIn("score_meta", rec)


class TestAuditRecordRestingFill(unittest.TestCase):

    def run_protect(self, record):
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        tpe.write_registry(ws, record)
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="101.5")])
        with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
            res = eft.protect_pending_entries(target_env="testnet")
        self.assertTrue(res["ok"], res["errors"])
        audit = tpe.read_jsonl(ws, "trades_audit.jsonl")
        self.assertEqual(len(audit), 1)
        return audit[0]

    def test_fill_copies_score_meta(self):
        audit = self.run_protect(tpe.make_record(score_meta=dict(FULL_META)))
        for key, value in FULL_META.items():
            self.assertEqual(audit[key], value, key)

    def test_old_pending_record_gives_nulls(self):
        audit = self.run_protect(tpe.make_record())
        for key in eft.SCORE_AUDIT_KEYS:
            self.assertIn(key, audit)
            self.assertIsNone(audit[key], key)


# =============================================================================
# 7. Outcome rows
# =============================================================================
class TestOutcomeRows(tto.OutcomesBase):

    META = {"score": 84, "score_tier": "Tier S (x)", "score_components": {"rsi": 84}, "dossier_tier": "S",
            "dossier_score": 85, "dossier_sha256": "abc"}

    def test_resolved_and_unavailable_rows_carry_score_fields(self):
        self.audit(symbol="BTCUSDT", entry_order_id=1, **self.META)
        self.audit(symbol="ETHUSDT", **self.META)
        self.audit(symbol="SOLUSDT", entry_order_id=3)  # pre-#202 record
        fake = tto.FakeFills({"BTCUSDT": [tto.fill(1, 1, "BUY", 100, 10, tto.T0),
                                          tto.fill(2, 2, "SELL", 110, 10, tto.T0 + 3600_000)],
                              "ETHUSDT": {"code": -2015, "msg": "Invalid API-key"},
                              "SOLUSDT": [tto.fill(5, 3, "BUY", 100, 10, tto.T0, symbol="SOLUSDT")]})
        code, _ = self.run_cli(fake, ["--no-klines"])
        self.assertEqual(code, 0)
        rows = {t["symbol"]: t for t in self.rows()}
        self.assertEqual(rows["BTCUSDT"]["status"], "closed")
        self.assertEqual(rows["ETHUSDT"]["status"], "fills_unavailable")
        for sym in ("BTCUSDT", "ETHUSDT"):
            for key, value in self.META.items():
                self.assertEqual(rows[sym][key], value, (sym, key))
        for key in to.SCORE_FIELDS:
            self.assertIn(key, rows["SOLUSDT"])
            self.assertIsNone(rows["SOLUSDT"][key])


# =============================================================================
# 8. score_calibration module
# =============================================================================
def outcome(score, r, env="prod", status="closed", key=0, mfe=1.0, radar=None):
    # Issue #207: rows of the current radar score schema (older or missing versions are excluded from the buckets)
    return {"symbol": f"S{key}USDT", "direction": "LONG", "entry_ts": 1000 + key, "env": env, "status": status,
            "dossier_score": score, "score": radar, "realized_r_net": r, "mfe_r": mfe,
            "score_schema_version": scal.SCORE_SCHEMA_VERSION}


class TestScoreCalibrationModule(_Workspace):

    def test_bucket_boundaries(self):
        cases = {54: None, 55: "55-64", 64: "55-64", 65: "65-74", 74: "65-74", 75: "75-79", 79: "75-79",
                 80: "80-89", 89: "80-89", 90: "90-95", 95: "90-95", 96: None, None: None, "x": None, 100: None}
        for score, label in cases.items():
            self.assertEqual(scal.bucket_for(score), label, score)
        self.assertEqual(scal.bucket_score({"dossier_score": 85, "score": 60}), 85)  # the dossier score keys

    def test_build_calibration_flags(self):
        rows = ([outcome(85, 0.5, key=i) for i in range(30)]            # calibrated
                + [outcome(92, -0.1, key=100 + i) for i in range(30)]   # n ok, expectancy <= 0
                + [outcome(70, 1.0, key=200 + i) for i in range(5)]     # insufficient
                + [outcome(None, 1.0, key=300), outcome(50, 1.0, key=301),
                   outcome(85, None, key=302), outcome(85, 1.0, env="testnet", key=303),
                   outcome(85, 1.0, status="open", key=304)])
        cal = scal.build_calibration(rows, "PROD", 30)
        b = cal["buckets"]
        self.assertEqual((b["80-89"]["n"], b["80-89"]["calibrated"], b["80-89"]["insufficient"]), (30, True, False))
        self.assertEqual((b["80-89"]["wins"], b["80-89"]["win_rate"], b["80-89"]["expectancy_r_net"]), (30, 1.0, 0.5))
        self.assertEqual(b["80-89"]["mean_mfe_r"], 1.0)
        self.assertEqual((b["90-95"]["n"], b["90-95"]["calibrated"]), (30, False))
        self.assertEqual((b["65-74"]["n"], b["65-74"]["insufficient"], b["65-74"]["calibrated"]), (5, True, False))
        self.assertEqual((b["55-64"]["n"], b["55-64"]["win_rate"]), (0, None))
        self.assertEqual((cal["unscored"], cal["out_of_range"]), (1, 1))

    def store(self, **over):
        cal = scal.merge_store(None, [outcome(85, 0.4, key=i) for i in range(30)], now=time.time())
        cal.update(over)
        return cal

    @staticmethod
    def spread(mean, sd, n=30):
        """n values with exactly this mean and sample sd (n-1): half at mean + a, half at mean - a."""
        a = sd * ((n - 1) / n) ** 0.5
        return [mean + a if i % 2 else mean - a for i in range(n)]

    def test_lower_confidence_bound_decides_calibration(self):
        for mean, sd, calibrated in ((0.2, 2.5, False), (0.8, 1.0, True)):
            with self.subTest(mean=mean, sd=sd):
                rows = [outcome(85, r, key=i) for i, r in enumerate(self.spread(mean, sd))]
                store = scal.merge_store(None, rows, now=time.time())
                b = store["buckets"]["80-89"]
                self.assertAlmostEqual(b["expectancy_r_net"], mean, places=4)
                self.assertAlmostEqual(b["sd_r_net"], sd, places=4)
                # Issue #207: Student-t critical value for df = n - 1 = 29 (1.699), not z = 1.645
                self.assertAlmostEqual(b["lcb95_r_net"], round(mean - 1.699 * sd / 30 ** 0.5, 4), places=4)
                self.assertEqual(b["calibrated"], calibrated)
                ok, reason = scal.bucket_is_calibrated(store, 85, "PROD")
                self.assertEqual(ok, calibrated, reason)
                if not calibrated:
                    self.assertIn("lower 95% bound -0.5755R <= 0.1R over n=30", reason)
        self.assertEqual(scal.lower_confidence_bound([1.0]), (None, None))   # n < 2: undefined sd
        one = scal.build_calibration([outcome(85, 5.0)], "PROD", min_trades=1)["buckets"]["80-89"]
        self.assertEqual((one["lcb95_r_net"], one["calibrated"]), (None, False))
        store = scal.merge_store(None, [outcome(85, 5.0)], now=time.time(), min_trades=1)
        self.assertFalse(scal.bucket_is_calibrated(store, 85, "PROD", min_trades=1)[0])

    def test_yolo_rows_never_calibrate_tier_s(self):
        yolo = [dict(outcome(85, 1.0, key=i), is_yolo=True) for i in range(30)]
        self.assertEqual(scal.build_calibration(yolo, "PROD", 30)["buckets"]["80-89"]["n"], 0)
        store = scal.merge_store(None, yolo, now=time.time())
        self.assertEqual((store["trades"], store["buckets"]["80-89"]["calibrated"]), ({}, False))
        self.assertFalse(scal.bucket_is_calibrated(store, 85, "PROD")[0])
        # a stored YOLO row from an older store is skipped by the recompute too; is_yolo is persisted
        legacy = {"trades": {"k": dict(outcome(85, 1.0), is_yolo=True)}}
        self.assertEqual(scal.merge_store(legacy, [], now=1)["buckets"]["80-89"]["n"], 0)
        kept = scal.merge_store(None, [dict(outcome(85, 1.0), is_yolo=False)], now=1)
        self.assertIs(next(iter(kept["trades"].values()))["is_yolo"], False)

    def test_bucket_is_calibrated_reasons(self):
        now = time.time()
        self.assertTrue(scal.bucket_is_calibrated(self.store(), 85, "PROD", now)[0])
        cases = [(None, 85, "missing or unreadable"), ({"env": "PROD"}, 85, "malformed"),
                 (self.store(buckets=[]), 85, "malformed"), (self.store(env="TESTNET"), 85, "env"),
                 (self.store(generated_at_ts=now - 8 * 86400), 85, "stale"),
                 (self.store(generated_at_ts=now + 3600), 85, "stale"),
                 (self.store(), None, "no dossier score"), (self.store(), 50, "outside the calibration buckets"),
                 (self.store(), 92, "n=0 < 30")]
        for cal, score, fragment in cases:
            ok, reason = scal.bucket_is_calibrated(cal, score, "PROD", now)
            self.assertFalse(ok, fragment)
            self.assertIn(fragment, reason)
        neg = scal.merge_store(None, [outcome(85, -0.2, key=i) for i in range(40)], now=now)
        ok, reason = scal.bucket_is_calibrated(neg, 85, "PROD", now)
        self.assertFalse(ok)
        self.assertIn("<= 0.1R", reason)  # issue #207: the minimum margin (default +0.1R)
        ok, reason = scal.bucket_is_calibrated(self.store(), 85, "PROD", now, min_trades=31)
        self.assertEqual((ok, reason), (False, "n=30 < 31"))

    def test_load_with_reason(self):
        self.assertEqual(scal.load_calibration_with_reason(self.root), (None, "calibration store missing"))
        with open(scal.store_path(self.root), "w", encoding="utf-8") as f:
            f.write("{bad")
        self.assertEqual(scal.load_calibration_with_reason(self.root), (None, "calibration store unreadable"))
        with open(scal.store_path(self.root), "w", encoding="utf-8") as f:
            f.write("[]")
        self.assertIsNone(scal.load_calibration(self.root))

    def test_merge_across_overlapping_runs_without_double_counting(self):
        run1 = [outcome(85, 0.5, key=i) for i in range(20)]
        run2 = [outcome(85, 0.5, key=i) for i in range(10, 35)] + [outcome(85, 0.5, key=99, status="open")]
        store = scal.merge_store(scal.merge_store(None, run1, now=1), run2, now=2)
        self.assertEqual(len(store["trades"]), 35)
        self.assertEqual(store["buckets"]["80-89"]["n"], 35)
        self.assertEqual((store["generated_at_ts"], store["env"]), (2, "PROD"))
        changed = scal.merge_store(store, [outcome(85, -5.0, key=0)], now=3)  # same key: newer row wins
        self.assertEqual(changed["buckets"]["80-89"]["n"], 35)
        self.assertLess(changed["buckets"]["80-89"]["expectancy_r_net"], 0.5)

    def test_policy_and_tier_helpers(self):
        # issue #207: the third value is tier_s_calibration_min_lcb_r (default +0.1R)
        self.assertEqual(scal.calibration_policy({}), (True, 30, 0.1))
        self.assertEqual(scal.calibration_policy({"require_calibrated_tier_s": False,
                                                  "tier_s_calibration_min_trades": 5}), (False, 5, 0.1))
        for bad in ("false", 0, None):
            self.assertTrue(scal.calibration_policy({"require_calibrated_tier_s": bad})[0], bad)
        for bad in (0, -3, "30", True, 2.5):
            self.assertEqual(scal.calibration_policy({"tier_s_calibration_min_trades": bad})[1], 30, bad)
        for tier, s in (("S", True), ("Tier S", True), ("Tier S (🔥 Top Score)", True), ("tier s", True),
                        ("A+", False), ("Tier A", False), ("", False), (None, False)):
            self.assertEqual(scal.candidate_is_tier_s({"tier": tier}), s, tier)

    def test_profile_defaults(self):
        import user_profile as up
        self.assertIs(up.DEFAULT_PROFILE["require_calibrated_tier_s"], True)
        self.assertEqual(up.DEFAULT_PROFILE["tier_s_calibration_min_trades"], 30)
        with open(os.path.join(BASE_DIR, "config", "user_profile.json.example"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual((example["require_calibrated_tier_s"], example["tier_s_calibration_min_trades"]), (True, 30))


# =============================================================================
# 9. Gate: executor and guard
# =============================================================================
class TestExecutorCalibrationGate(_Workspace):

    def setUp(self):
        super().setUp()
        self._env = patch.dict(os.environ, {"AGY_BRAIN_DIRS": self.brain})
        self._env.start()
        self.addCleanup(self._env.stop)

    def dossier(self, **cand):
        write_agy_dossier(self.brain, self.dossier_path,
                          [dict({"symbol": "SOLUSDT", "direction": "LONG", "tier": "S", "score": 85,
                                 "requires_user_confirmation": False}, **cand)], snapshots=True)

    def gate(self, env="prod", confirmed=False, **kw):
        return eft.enforce_evaluation_dossier("SOLUSDT", "LONG", env, confirmed=confirmed, base_dir=self.root, **kw)

    def test_calibrated_bucket_is_autonomous(self):
        tgb.write_calibrated_store(self.root)
        self.dossier()
        ok, reason, _ = self.gate()
        self.assertTrue(ok, reason)

    def test_helper_exception_asks_the_user(self):
        tgb.write_calibrated_store(self.root)
        self.dossier()
        with patch.object(scal, "tier_s_confirmation_required", side_effect=RuntimeError("boom")):
            ok, reason, cand = self.gate()
        self.assertFalse(ok)
        self.assertIn("Tier S score bucket 80-89 not calibrated (calibration check failed (RuntimeError))", reason)
        self.assertIsNotNone(cand)
        with patch.object(scal, "tier_s_confirmation_required", side_effect=RuntimeError("boom")):
            self.assertTrue(self.gate(confirmed=True)[0])

    def test_uncalibrated_asks_and_confirmed_proceeds(self):
        self.dossier()
        ok, reason, cand = self.gate()
        self.assertFalse(ok)
        self.assertIn("Tier S score bucket 80-89 not calibrated (calibration store missing): ask the user and rerun "
                      "with --confirmed", reason)
        self.assertEqual(cand["symbol"], "SOLUSDT")
        self.assertTrue(self.gate(confirmed=True)[0])

    def test_flag_off_restores_old_behaviour(self):
        os.makedirs(os.path.join(self.root, "config"), exist_ok=True)
        with open(os.path.join(self.root, "config", "user_profile.json"), "w", encoding="utf-8") as f:
            json.dump({"require_calibrated_tier_s": False}, f)
        self.dossier()
        self.assertTrue(self.gate()[0])

    def test_testnet_not_enforced(self):
        self.dossier()
        ok, reason, _ = self.gate(env="testnet")
        self.assertTrue(ok, reason)

    def test_yolo_and_a_plus_keep_their_own_messages(self):
        self.dossier(is_yolo=True)
        ok, reason, _ = self.gate()
        self.assertIn("YOLO entry", reason)
        self.assertNotIn("score bucket", reason)
        self.dossier(tier="A+", requires_user_confirmation=True, score=70)
        ok, reason, _ = self.gate()
        self.assertIn("pending explicit user confirmation", reason)
        self.assertNotIn("score bucket", reason)
        self.dossier(tier="A+", requires_user_confirmation=False, score=70)  # not Tier S: not this gate's concern
        self.assertTrue(self.gate()[0])


class TestGuardCalibrationGate(tgb.GuardHarness):

    def deploy(self, flags="", env="prod"):
        command = f"{SCRIPT} --symbol BTCUSDT --direction LONG --leverage 3 --env {env} {flags}".strip()
        return self.agy(self.cmd(command, conversationId=tgb.PARENT_CONV_ID))

    def store_path(self):
        return os.path.join(self.root, "logs", "score_calibration.json")

    def test_calibrated_fixture_allows_autonomous_tier_s(self):
        self.write_provenance_dossier()
        self.assertEqual(self.deploy().get("decision"), "allow")

    def overwrite_store(self, text):
        with open(self.store_path(), "w", encoding="utf-8") as f:
            f.write(text)

    def test_each_uncalibrated_reason_asks_like_the_executor(self):
        now = time.time()
        cases = {
            "calibration store missing": lambda: os.remove(self.store_path()),
            "calibration store unreadable": lambda: self.overwrite_store("{bad"),
            "calibration store malformed": lambda: self.overwrite_store('{"env": "PROD"}'),
            "env": lambda: tgb.write_calibrated_store(self.root, env="TESTNET"),
            "stale": lambda: tgb.write_calibrated_store(self.root, now=now - 8 * 86400),
            "n=4 < 30": lambda: tgb.write_calibrated_store(self.root, n=4),
            "<= 0": lambda: tgb.write_calibrated_store(self.root, expectancy=-0.1),
        }
        for fragment, mutate in cases.items():
            with self.subTest(reason=fragment):
                tgb.write_calibrated_store(self.root)
                mutate()
                self.write_provenance_dossier()
                res = self.deploy()
                self.assertDenied(res, CALIB_GATE)
                self.assertIn(fragment, res["reason"])
                self.assertIn("rerun with --confirmed", res["reason"])
                with patch.dict(os.environ, {"BINANCE_API_ENV": "prod"}):
                    ok, ex_reason, _ = eft.enforce_evaluation_dossier("BTCUSDT", "LONG", "prod", base_dir=self.root)
                self.assertFalse(ok)
                message = ex_reason.split("BTCUSDT: ", 1)[1]
                self.assertIn(message, res["reason"])  # one helper, one message
                self.assertEqual(self.deploy("--confirmed").get("decision"), "allow")

    def edit_snapshots(self, snaps):
        with open(self.dossier_path, encoding="utf-8") as f:
            record = json.load(f)
        if snaps is None:
            record.pop("radar_snapshots", None)
        else:
            record["radar_snapshots"] = snaps
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)

    def test_dossier_score_must_match_the_radar_snapshot(self):
        cases = (({"BTCUSDT|LONG": {"radar_snapshot": {"confidence": 82}}}, "score_mismatch (dossier 85 vs radar 82)"),
                 ({"BTCUSDT|LONG": {"radar_snapshot": {"confidence": 85.5}}}, "score_mismatch (dossier 85 vs radar 85.5)"),
                 ({"BTCUSDT|LONG": {"radar_snapshot": {}}}, "score_mismatch (dossier 85 vs radar n/a)"),
                 (None, "radar_snapshot_missing"),
                 ({}, "radar_snapshot_missing"),
                 ("garbage", "radar_snapshot_missing"),
                 ({"BTCUSDT|LONG": {"radar_snapshot": None, "radar_snapshot_reason": "stale"}},
                  "radar_snapshot_missing"),
                 ({"BTCUSDT|SHORT": {"radar_snapshot": {"confidence": 85}}}, "radar_snapshot_missing"))
        for snaps, fragment in cases:
            with self.subTest(snaps=snaps):
                self.write_provenance_dossier()
                self.edit_snapshots(snaps)
                res = self.deploy()
                self.assertDenied(res, CALIB_GATE)
                self.assertIn(f"80-89 not calibrated ({fragment})", res["reason"])
                ok, ex_reason, _ = eft.enforce_evaluation_dossier("BTCUSDT", "LONG", "prod", base_dir=self.root)
                self.assertFalse(ok)
                self.assertIn(ex_reason.split("BTCUSDT: ", 1)[1], res["reason"])  # same message
                self.assertEqual(self.deploy("--confirmed").get("decision"), "allow")
        self.write_provenance_dossier()  # matching snapshot + calibrated store
        self.assertEqual(self.deploy().get("decision"), "allow")
        self.assertTrue(eft.enforce_evaluation_dossier("BTCUSDT", "LONG", "prod", base_dir=self.root)[0])

    def test_tier_s_label_needs_a_tier_s_bucket(self):
        """A Tier S with score 70 never borrows a calibrated 65-74 bucket (executor and guard, same message)."""
        stats = {"n": 40, "wins": 30, "win_rate": 0.75, "expectancy_r_net": 0.9, "sd_r_net": 0.5,
                 "lcb95_r_net": 0.77, "insufficient": False, "calibrated": True}
        with open(self.store_path(), "w", encoding="utf-8") as f:
            json.dump({"generated_at_ts": int(time.time()), "env": "PROD", "score_schema_version": 2,
                       "buckets": {"65-74": stats, "80-89": stats, "90-95": stats}}, f)
        self.write_provenance_dossier(extra={"score": 70})
        res = self.deploy()
        self.assertDenied(res, CALIB_GATE)
        self.assertIn("Tier S score bucket 65-74 not calibrated (tier_s_score_below_80 (score 70))", res["reason"])
        ok, ex_reason, _ = eft.enforce_evaluation_dossier("BTCUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn(ex_reason.split("BTCUSDT: ", 1)[1], res["reason"])
        self.assertEqual(self.deploy("--confirmed").get("decision"), "allow")
        self.assertTrue(eft.enforce_evaluation_dossier("BTCUSDT", "LONG", "prod", base_dir=self.root,
                                                       confirmed=True)[0])
        self.write_provenance_dossier(extra={"score": 85})  # same store, Tier S bucket -> autonomous
        self.assertEqual(self.deploy().get("decision"), "allow")

    def test_snapshot_bound_to_the_validated_dossier_sha(self):
        self.write_provenance_dossier()
        real_check, real_validate = pre_trade_guard.check_dossier, eft.validate_dossier_for_trade

        def guard_check(*a, **kw):
            ok, reason, cand = real_check(*a, **kw)
            return ok, reason, dict(cand, dossier_sha256="f" * 64) if cand else cand

        def exec_validate(*a, **kw):
            ok, reason, cand = real_validate(*a, **kw)
            return ok, reason, dict(cand, dossier_sha256="f" * 64) if cand else cand

        with patch.object(pre_trade_guard, "check_dossier", side_effect=guard_check):
            res = self.deploy()
        self.assertDenied(res, CALIB_GATE)
        self.assertIn("80-89 not calibrated (dossier_changed)", res["reason"])
        with patch.object(eft, "validate_dossier_for_trade", side_effect=exec_validate):
            ok, ex_reason, _ = eft.enforce_evaluation_dossier("BTCUSDT", "LONG", "prod", base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn(ex_reason.split("BTCUSDT: ", 1)[1], res["reason"])
        # unpatched: the guard and the executor both carry the validated sha and match
        self.assertEqual(self.deploy().get("decision"), "allow")
        _, _, cand = eft.enforce_evaluation_dossier("BTCUSDT", "LONG", "prod", base_dir=self.root)
        with open(self.dossier_path, encoding="utf-8") as f:
            self.assertEqual(cand["dossier_sha256"], json.load(f)["provenance"]["sha256"])
        self.assertEqual(scal.radar_snapshot_matches(dict(cand, dossier_sha256=None), self.root),
                         (False, "dossier_changed"))

    def test_unreadable_dossier_record_is_not_a_match(self):
        cand = {"symbol": "BTCUSDT", "direction": "LONG", "score": 85}
        self.assertEqual(scal.radar_snapshot_matches(cand, os.path.join(self.root, "nowhere")),
                         (False, "radar_snapshot_unreadable"))
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            f.write("{bad")
        self.assertEqual(scal.radar_snapshot_matches(cand, self.root), (False, "radar_snapshot_unreadable"))
        msg = scal.tier_s_confirmation_required(dict(cand, tier="S"), "prod", {}, self.root)
        self.assertIn("80-89 not calibrated (radar_snapshot_unreadable)", msg)

    def test_guard_helper_exception_asks_the_user(self):
        self.write_provenance_dossier()
        with patch.object(pre_trade_guard.scal, "tier_s_confirmation_required", side_effect=RuntimeError("boom")):
            res = self.deploy()
            self.assertDenied(res, CALIB_GATE)
            self.assertIn("calibration check failed (RuntimeError)", res["reason"])
            self.assertEqual(self.deploy("--confirmed").get("decision"), "allow")

    def test_score_reasons(self):
        for score, fragment in ((None, "no dossier score"), (50, "outside the calibration buckets"),
                                (77, "tier_s_score_below_80 (score 77)"), (96, "outside the calibration buckets")):
            with self.subTest(score=score):
                extra = {"score": score} if score is not None else {"score": None}
                self.write_provenance_dossier(extra=extra)
                res = self.deploy()
                self.assertDenied(res, CALIB_GATE)
                self.assertIn(fragment, res["reason"])

    def test_flag_off_and_testnet_not_enforced(self):
        os.remove(self.store_path())
        self.write_provenance_dossier()
        res = self.deploy(env="testnet")
        self.assertNotIn(CALIB_GATE, res.get("reason", ""))
        with open(os.path.join(self.root, "config", "user_profile.json"), "w", encoding="utf-8") as f:
            json.dump({"profile_completed": True, "yolo_slot_enabled": True, "leverage_standard": 3,
                       "max_open_positions": 5, "autonomous_execution_tier_s": True,
                       "require_calibrated_tier_s": False}, f)
        self.assertEqual(self.deploy().get("decision"), "allow")

    def test_yolo_a_plus_and_autonomous_off_unchanged(self):
        os.remove(self.store_path())
        self.write_provenance_dossier(extra={"is_yolo": True, "requires_user_confirmation": False})
        res = self.deploy()
        self.assertDenied(res, "YOLO Confirmation")
        self.write_provenance_dossier(extra={"tier": "A+", "requires_user_confirmation": True, "score": 70})
        self.assertDenied(self.deploy(), "User Confirmation Required")
        with open(os.path.join(self.root, "config", "user_profile.json"), "w", encoding="utf-8") as f:
            json.dump({"profile_completed": True, "leverage_standard": 3, "max_open_positions": 5,
                       "autonomous_execution_tier_s": False}, f)
        self.write_provenance_dossier()
        self.assertDenied(self.deploy(), "Autonomous Execution Disabled")

    def test_risk_reducing_commands_never_consult_the_store(self):
        os.remove(self.store_path())
        self.write_provenance_dossier()
        with patch.object(pre_trade_guard.scal, "tier_s_confirmation_required",
                          side_effect=AssertionError("calibration consulted")):
            for flags in ("--close-position --symbol BTCUSDT", "--move-breakeven --symbol BTCUSDT", "--auto-heal",
                          "--audit-orphans", "--protect-pending"):
                with self.subTest(flags=flags):
                    self.assertEqual(self.agy(self.cmd(f"{SCRIPT} {flags} --env prod")).get("decision"), "allow")
            self.assertNotEqual(self.agy(self.cmd(f"{SCRIPT} --positions --json --env prod")).get("decision"), "deny")


# =============================================================================
# 10. Guard: the store is ground truth
# =============================================================================
class TestCalibrationStoreGroundTruth(tgb.GuardHarness):

    def test_shell_and_file_tool_writes_denied(self):
        for c in ("echo '{}' > logs/score_calibration.json", "rm logs/score_calibration.json",
                  "cp /tmp/forged.json logs/score_calibration.json",
                  "python3 -c \"open('logs/score_calibration.json', 'w').write('{}')\""):
            res = self.agy(self.cmd(c))
            self.assertDenied(res, "Ground Truth Protection")
            self.assertIn("logs/score_calibration.json may only be written by", res["reason"], c)
            self.assertIn("python3 scripts/trading_scorecard.py", res["reason"], c)
        res = self.agy({"toolCall": {"name": "write_to_file",
                                     "args": {"TargetFile": "logs/score_calibration.json", "CodeContent": "{}"}}})
        self.assertDenied(res)

    def test_sanctioned_writer_and_reads_allowed(self):
        for c in ("python3 scripts/trading_scorecard.py", "python3 scripts/trading_scorecard.py --env prod --json",
                  "cat logs/score_calibration.json"):
            self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)

    def test_trade_outcomes_jsonl_is_ground_truth(self):
        for c in ("echo '{}' >> logs/trade_outcomes.jsonl", "cp /tmp/forged.jsonl logs/trade_outcomes.jsonl",
                  "rm logs/trade_outcomes.jsonl"):
            res = self.agy(self.cmd(c))
            self.assertDenied(res, "Ground Truth Protection")
            self.assertIn("logs/trade_outcomes.jsonl may only be written by", res["reason"], c)
            self.assertIn("python3 scripts/trade_outcomes.py", res["reason"], c)
        for c in ("python3 scripts/trade_outcomes.py", "python3 scripts/trade_outcomes.py --env prod --json",
                  "cat logs/trade_outcomes.jsonl"):
            self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)
        self.assertIn("logs/trade_outcomes.jsonl", pre_trade_guard.GROUND_TRUTH_FILES)

    def test_trades_audit_jsonl_is_ground_truth(self):
        for c in ("echo '{}' >> logs/trades_audit.jsonl", "cp /tmp/forged.jsonl logs/trades_audit.jsonl",
                  "sed -i 's/x/y/' logs/trades_audit.jsonl", "rm logs/trades_audit.jsonl"):
            res = self.agy(self.cmd(c))
            self.assertDenied(res, "Ground Truth Protection")
            self.assertIn("logs/trades_audit.jsonl may only be written by", res["reason"], c)
            self.assertIn("python3 scripts/execute_futures_trade.py", res["reason"], c)
        for c in ("cat logs/trades_audit.jsonl", "tail -n 5 logs/trades_audit.jsonl",
                  "grep BTCUSDT logs/trades_audit.jsonl", "wc -l logs/trades_audit.jsonl"):
            self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)
        self.assertIn("logs/trades_audit.jsonl", pre_trade_guard.GROUND_TRUTH_FILES)

    def test_sanctioned_audit_writer_invocations_still_allowed(self):
        """The executor is the only writer (entry records, failsafe aborts); none of its commands gets denied."""
        self.write_provenance_dossier()
        for flags in ("--close-position --symbol BTCUSDT", "--move-breakeven --symbol BTCUSDT", "--auto-heal",
                      "--protect-pending", "--audit-orphans"):
            with self.subTest(flags=flags):
                self.assertEqual(self.agy(self.cmd(f"{SCRIPT} {flags} --env prod")).get("decision"), "allow")
        res = self.agy(self.cmd(f"{SCRIPT} --symbol BTCUSDT --direction LONG --leverage 3 --env prod",
                                conversationId=tgb.PARENT_CONV_ID))
        self.assertEqual(res.get("decision"), "allow", res)
        for c in ("python3 scripts/trade_outcomes.py --env prod --json", "python3 scripts/sync_session_state.py",
                  "python3 scripts/loops/position_guardian_loop.py --once"):
            self.assertNotIn("Ground Truth Protection", self.agy(self.cmd(c)).get("reason", ""), c)

    def test_brief_files_are_ground_truth_but_readable(self):
        for name in ("primed_brief.json", "primed_brief_scores.json"):
            for c in (f"echo '{{}}' > logs/{name}", f"cp /tmp/forged.json logs/{name}", f"rm logs/{name}",
                      f"python3 -c \"open('logs/{name}', 'w').write('{{}}')\""):
                res = self.agy(self.cmd(c))
                self.assertDenied(res, "Ground Truth Protection")
                self.assertIn(f"logs/{name} may only be written by `python3 scripts/prime_evaluator_brief.py`",
                              res["reason"], c)
            self.assertDenied(self.agy({"toolCall": {"name": "write_to_file", "args": {
                "TargetFile": f"logs/{name}", "CodeContent": "{}"}}}))
            for c in (f"cat logs/{name}", f"python3 -m json.tool logs/{name}"):
                self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "deny", c)
            # Read tools (the evaluator reads the brief with view_file / Read)
            self.assertNotEqual(self.agy({"toolCall": {"name": "view_file", "args": {
                "AbsolutePath": os.path.join(self.root, "logs", name)}}}).get("decision"), "deny")
            claude = self.run_guard({"tool_name": "Read", "tool_input": {"file_path": os.path.join(self.root, "logs",
                                                                                                    name)}})
            self.assertNotEqual(claude.get("__exit_code__"), 2, claude)
        self.assertIn("logs/primed_brief.json", pre_trade_guard.GROUND_TRUTH_FILES)
        self.assertIn("logs/primed_brief_scores.json", pre_trade_guard.GROUND_TRUTH_FILES)

    def test_documented_brief_commands_not_denied(self):
        """The clean-room flow as documented (CLAUDE.md, AGENTS.md, SKILL, README) still passes the guard."""
        readme = TestPromptAndDocs.read("README.md")
        documented = [l.strip() for l in readme.splitlines() if l.strip().startswith("python3 scripts/prime_evaluator")]
        self.assertTrue(documented)
        for c in documented + ["python3 scripts/prime_evaluator_brief.py", "python3 scripts/prime_evaluator_brief.py "
                               "--env testnet", "python3 scripts/prime_evaluator_brief.py --json",
                               "python3 scripts/prime_evaluator_brief.py --recheck ETHFIUSDT:LONG",  # issue #267
                               "python3 scripts/record_evaluation.py --from-subagent abc",
                               "python3 scripts/record_evaluation.py --from-claude-subagent a0123456789abcdef"]:
            with self.subTest(command=c):
                res = self.agy(self.cmd(c))
                self.assertNotEqual(res.get("decision"), "deny", res)
                self.assertNotIn("Ground Truth Protection", res.get("reason", ""))
        for doc in ("CLAUDE.md", "AGENTS.md", os.path.join(".agents", "skills", "trade-execution-planner", "SKILL.md")):
            for line in TestPromptAndDocs.read(doc).splitlines():
                for cmd in re.findall(r"`(python3 scripts/[^`]+)`", line):
                    if "primed_brief" in cmd:
                        self.fail(f"{doc} documents a command naming a protected brief file: {cmd}")

    def test_suggested_report_issue_commands_are_not_denied(self):
        """Every report_issue.sh command the doctor suggests (placeholders filled) passes the guard and names no
        ground-truth file (issue #202 audit round 3)."""
        import re
        import trading_doctor
        texts = [trading_doctor.ledger_audit_warning({"target_env": "prod", "audit_read_error": "PermissionError: x"},
                                                     "prod"),
                 trading_doctor.ledger_audit_warning({"target_env": "prod", "audit_corrupt_lines": 3}, "prod")]
        with patch("utils.yolo_scan_health.read_health",
                   return_value={"consecutive_unavailable": 99, "last_unavailable_reason": "scan failed"}):
            texts.append(trading_doctor.check_yolo_scan_health({"yolo_slot_enabled": True})[1])
        basenames = [p.rsplit("/", 1)[-1] for p in pre_trade_guard.GROUND_TRUTH_FILES]
        commands = []
        for text in texts:
            found = re.findall(r"\./scripts/report_issue\.sh .*?<file with the raw output>", text)
            self.assertEqual(len(found), 1, text)
            commands.append(found[0].replace("<file with the raw output>", "logs/issue_output_1700000000.log")
                            .replace("<command>", "python3 scripts/prime_evaluator_brief.py").replace("<code>", "1"))
        for command in commands:
            with self.subTest(command=command):
                self.assertNotRegex(command, r"[<>]")
                for name in basenames:
                    self.assertNotIn(name, command)
                res = self.agy(self.cmd(command))
                self.assertNotEqual(res.get("decision"), "deny", res)
                self.assertNotIn("Ground Truth Protection", res.get("reason", ""))
        # control: naming the ledger in the command is what the guard denies
        self.assertDenied(self.agy(self.cmd(commands[0].replace("trades audit ledger", "trades_audit.jsonl"))),
                          "Ground Truth Protection")

    def test_module_is_a_protected_harness_file(self):
        self.assertIn("scripts/utils/score_calibration.py", pre_trade_guard.HARNESS_FILES)
        self.assertIn("logs/score_calibration.json", pre_trade_guard.GROUND_TRUTH_FILES)


# =============================================================================
# 11. Scorecard
# =============================================================================
class TestScorecardCalibration(tsc.ScorecardBase):

    def test_persisted_dossier_tier_beats_the_join(self):
        self.write_dossier({"timestamp_ts": tsc.T, "valid_until_ts": tsc.T + 1200,
                            "approved_candidates": [{"symbol": "SOLUSDT", "direction": "LONG", "tier": "A"}]})
        self.write_outcomes([tsc.row(symbol="SOLUSDT", entry_s=tsc.T + 60, dossier_tier="S"),
                             tsc.row(symbol="SOLUSDT", entry_s=tsc.T + 120),                  # join fallback
                             tsc.row(symbol="SOLUSDT", entry_s=tsc.T + 180, dossier_tier="B")])  # unusable -> join
        tiers = sc.generate_scorecard("prod")["tiers_breakdown"]
        self.assertEqual((tiers["S"]["n"], tiers["A"]["n"]), (1, 2))

    def rows(self, n=30, start=0, r=0.5, score=85):
        # issue #207: current radar score schema (older rows are excluded from the buckets)
        return [tsc.row(net=r, symbol=f"S{i}USDT", entry_s=tsc.T + i, dossier_score=score, score=score - 1,
                        mfe_r=1.2, score_schema_version=scal.SCORE_SCHEMA_VERSION) for i in range(start, start + n)]

    def test_block_text_and_store_written_by_the_cli(self):
        self.write_outcomes(self.rows() + [tsc.row(net=1.0, symbol="NOSCORE", entry_s=tsc.T + 999)])
        code, out = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("CALIBRATION BY SCORE BUCKET (dossier score; heuristic, not a probability", out)
        self.assertIn("PERFORMANCE BY TIER:", out)
        self.assertIn("80-89: n=30 | Win Rate: 100.0% | Exp net: +0.5000R | lcb95: +0.5000R", out)
        self.assertIn("calibrated: yes (calibrated)", out)
        self.assertIn("55-64: n=0 | Win Rate: n/a% | Exp net: n/aR | lcb95: n/aR", out)
        self.assertNotIn("CONVICTION", out)
        with open(os.path.join(self.logs, "trading_scorecard.json"), encoding="utf-8") as f:
            block = json.load(f)["score_calibration"]
        b = {x["bucket"]: x for x in block["buckets"]}
        self.assertEqual([x["bucket"] for x in block["buckets"]], ["55-64", "65-74", "75-79", "80-89", "90-95"])
        self.assertEqual((b["80-89"]["n"], b["80-89"]["calibrated"], b["80-89"]["insufficient"]), (30, True, False))
        self.assertEqual((b["80-89"]["win_rate"], b["80-89"]["expectancy_r_net"], b["80-89"]["mean_mfe_r"]),
                         (1.0, 0.5, 1.2))
        self.assertEqual(b["80-89"]["mean_radar_score"], 84.0)
        self.assertEqual((block["unscored"], block["store_written"]), (1, True))
        store = scal.load_calibration(self.ws)
        self.assertTrue(scal.bucket_is_calibrated(store, 85, "PROD")[0])

    def test_custom_outcomes_file_never_feeds_the_store(self):
        forged = os.path.join(self.ws, "forged.jsonl")
        with open(forged, "w", encoding="utf-8") as f:
            for r in self.rows():
                f.write(json.dumps(r) + "\n")
        for argv in (["--outcomes", forged], ["--outcomes", forged, "--json"]):
            code, out = self.run_cli(argv)
            self.assertEqual(code, 0)
            self.assertIsNone(scal.load_calibration(self.ws), argv)
        with open(os.path.join(self.logs, "trading_scorecard.json"), encoding="utf-8") as f:
            block = json.load(f)["score_calibration"]
        self.assertEqual((block["store_written"], block["buckets"][3]["n"]), (False, 30))  # shown, not persisted
        # an existing store stays unchanged
        self.write_outcomes(self.rows(5))
        self.run_cli([])
        with open(scal.store_path(self.ws), encoding="utf-8") as f:
            before = f.read()
        self.run_cli(["--outcomes", forged])
        with open(scal.store_path(self.ws), encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        # the default path (also when spelled out) still writes it
        self.write_outcomes(self.rows(30))
        self.run_cli(["--outcomes", self.outcomes])
        self.assertEqual(scal.load_calibration(self.ws)["buckets"]["80-89"]["n"], 30)

    def test_store_accumulates_across_overlapping_windows(self):
        self.write_outcomes(self.rows(20))
        self.run_cli([])
        self.write_outcomes(self.rows(20, start=10))   # overlaps trades 10-19
        self.run_cli([])
        store = scal.load_calibration(self.ws)
        self.assertEqual((len(store["trades"]), store["buckets"]["80-89"]["n"]), (30, 30))

    def test_no_prod_rows_never_writes_and_library_call_never_writes(self):
        self.write_outcomes([tsc.row(env="testnet", dossier_score=85)])
        self.run_cli([])
        self.assertIsNone(scal.load_calibration(self.ws))
        self.write_outcomes(self.rows())
        s = sc.generate_scorecard("prod")
        self.assertFalse(s["score_calibration"]["store_written"])
        self.assertEqual(s["score_calibration"]["buckets"][3]["n"], 30)  # computed, not persisted
        self.assertIsNone(scal.load_calibration(self.ws))

    def test_totals_match_trade_outcomes_summarize(self):
        rows = [tsc.row(net=n, gross=g, symbol=f"X{i}", entry_s=tsc.T + i, tp1_filled=False, exit_reason="SL")
                for i, (n, g) in enumerate(((1.5, 1.6), (-1.0, -0.9), (0.3, 0.4), (-0.2, -0.1)))]
        self.write_outcomes(rows)
        s = sc.generate_scorecard("prod")
        summary = to.summarize(rows)
        self.assertEqual(s["sample_size"], len([r for r in rows if r["status"] == "closed"]))
        self.assertAlmostEqual(s["performance"]["mean_realized_r_net"], summary["mean_realized_r_net"])
        self.assertAlmostEqual(s["performance"]["mean_realized_r_gross"], summary["mean_realized_r_gross"])

    def test_scaling_note_needs_net_r_and_is_not_an_instruction(self):
        rows = [tsc.row(net=2.0, symbol=f"W{i}", entry_s=tsc.T + i) for i in range(10)]
        rows += [tsc.row(net=-1.0, symbol=f"L{i}", entry_s=tsc.T + 100 + i) for i in range(9)]
        self.write_outcomes(rows + [tsc.row(net=-1.0, symbol="L9", entry_s=tsc.T + 200)])
        self.assertEqual(sc.generate_scorecard("prod")["recommendations"], [sc.SCALING_MSG.format(n=20)])
        self.write_outcomes(rows + [tsc.row(net=None, gross=-1.0, symbol="G", entry_s=tsc.T + 300)])
        recs = sc.generate_scorecard("prod")["recommendations"]
        self.assertEqual(recs, [])  # a gross-fallback row: no scaling note
        for msg in (sc.SCALING_MSG, sc.CLUSTER_MSG):
            self.assertTrue(msg.startswith("Data note:"))
            self.assertIn("user's explicit decision", msg)
            for word in ("Enforce", "qualifies", "inviolable"):
                self.assertNotIn(word, msg)
        self.assertIn("≥ 1.8", sc.SCALING_MSG)

    def test_no_executor_import(self):
        with open(os.path.join(BASE_DIR, "scripts", "trading_scorecard.py"), encoding="utf-8") as f:
            self.assertNotIn("import execute_futures_trade", f.read())


# =============================================================================
# 12. Prompt and docs
# =============================================================================
class TestPromptAndDocs(unittest.TestCase):

    @staticmethod
    def read(*parts):
        with open(os.path.join(BASE_DIR, *parts), encoding="utf-8") as f:
            return f.read()

    def test_evaluator_prompt_says_score(self):
        text = self.read(".agents", "agents", "isolated_market_evaluator", "agent.md")
        lines = [l for l in text.splitlines() if "conviction" in l.lower()]
        self.assertEqual(len(lines), 1)
        self.assertIn("alias `conviction_pct`", lines[0])
        self.assertIn("NOT a probability", text)
        contract = text.split("<output_contract>")[1].split("</output_contract>")[0]
        must = contract.split("each item MUST include")[1].split("Optional:")[0]
        self.assertIn("`score` (the brief `confidence` copied exactly: never estimated, never omitted", must)
        c41 = next(l for l in text.splitlines() if l.strip().startswith("- C4.1 Confirmation policy"))
        self.assertIn("brief `confidence` next to its dossier `score` (they must be equal)", c41)
        for shot in ("FILUSDT Tier S, confidence 95 = score 95", "SOLUSDT Tier A+, confidence 70 = score 70",
                     "Tier A), confidence 60 = score 60"):
            self.assertIn(shot, text)
        # the five shot dossiers (#206 added the squeezed RLCUSDT SHORT, #223 the downgraded NEARUSDT LONG) + the
        # contract's sample YOLO item (issue #251: the rejected_candidates items, lines with a "gate", counted apart)
        rejected = [l for l in text.splitlines() if '"gate": ' in l]
        self.assertEqual(text.count('"score": ') - sum(l.count('"score": ') for l in rejected), 6)
        self.assertEqual(sum(l.count('"score": null') for l in rejected), 7)
        self.assertIn("RLCUSDT Tier A, confidence 64 = score 64", text)
        self.assertIn("NEARUSDT Tier A, confidence 72 = score 72", text)
        self.assertIn('"leverage": 5, "score": null, "is_yolo": true', text)
        self.assertIn("stays the raw radar `confidence` even when RULE 3 downgrades the tier; never adjust it", text)
        self.assertNotIn("Maximum Conviction", text)

    def test_agents_md_and_docs(self):
        agents = self.read("AGENTS.md")
        self.assertLess(len(agents.encode("utf-8")), 22000)
        self.assertNotIn("Institutional Maximum Conviction", agents)
        self.assertIn("score ≥ 80; heuristic, uncalibrated, not a probability", agents)
        self.assertIn("`require_calibrated_tier_s`, the score bucket is calibrated", agents)
        self.assertIn("require_calibrated_tier_s", self.read("README.md"))
        self.assertIn("--confirmed", self.read(".agents", "skills", "trade-execution-planner", "SKILL.md")
                      .split("Confirmation policy")[1].split("\n")[0])


if __name__ == "__main__":
    unittest.main()
