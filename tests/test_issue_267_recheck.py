#!/usr/bin/env python3
"""
test_issue_267_recheck.py - Confirmation after dossier expiry: targeted re-check (issue #267).

Covers the single-symbol screening path (screening_pipeline.execute_symbol_recheck and its CLI flag), the
`prime_evaluator_brief.py --recheck SYMBOL:DIRECTION` brief (utils/recheck_brief.py), the deterministic bounds
(utils/recheck_bounds.py), the profile keys, the recorder link (record_evaluation.attach_recheck) and the hook /
executor accepting a re-check dossier like any APPROVED dossier.

Hermetic: temp workspaces, fake agy transcripts under AGY_BRAIN_DIRS, mocked screening steps, urllib blocked, no
Binance client and no .env read (explicit --env prod everywhere).
"""

import datetime
import io
import json
import os
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

import broad_market_radar as bmr  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import record_evaluation as rec  # noqa: E402
import screening_pipeline as sp  # noqa: E402
import user_profile as up  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
from utils import rate_limit_guard as rlg  # noqa: E402
from utils import recheck_bounds as rb  # noqa: E402
from utils import recheck_brief as rcb  # noqa: E402
import test_dossier_provenance as tdp  # noqa: E402  (fixtures only; its TestCases are not re-exported)
import test_issue_187_lesson_selection as t187  # noqa: E402  (fixtures only: lesson ledger rows)
import test_executor_gates as teg  # noqa: E402  (fixtures only)
import test_guard_bypasses as tgb  # noqa: E402  (fixtures only)
import execute_futures_trade as eft  # noqa: E402


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


SYMBOL, DIRECTION = "ETHFIUSDT", "LONG"
# Original confirmed plan: R = |entry - SL| = 0.03, so 0.25R = 0.0075
OLD_CAND = {"symbol": SYMBOL, "direction": DIRECTION, "tier": "S", "entry": 1.0, "stop_loss": 0.97, "tp1": 1.054,
            "tp2": 1.12, "leverage": 3, "score": 95, "is_yolo": False, "requires_user_confirmation": True}
# Live setup the screener recomputes (within 0.25R of the old plan on trigger, SL and TP2)
LIVE_ROW = {"symbol": SYMBOL, "direction": DIRECTION, "tier": "Tier S (🔥 Top Score, order flow confirmed)",
            "tier_code": "S", "confidence": 95, "current_price": 1.001, "trigger_price": 1.004, "sl_price": 0.974,
            "tp1_price": 1.058, "tp2_price": 1.124, "rr_ratio": 4.0, "risk_pct": 2.99, "rsi_15m": 27.0,
            "vol_ratio": 2.1, "lower_wick_pct": 61.0, "upper_wick_pct": 4.0, "cvd_delta": 0.0, "oi_z_score": 0.0,
            "regime": "CONSOLIDATION", "absorption": "BULLISH_ABSORPTION", "whale_bias": "BALANCED",
            "required_margin": 10.0, "step_qty": 1.0, "actual_notional": 30.0, "target_dollar_risk": 1.0,
            "reasons": ["RSI 15m extreme oversold (27.0)"], "score_components": {"rsi": 35, "wick": 35, "volume": 25},
            "tier_s_eligible": True, "score_schema_version": 2}
STATE = {"target_env": "prod", "is_valid": True, "active_positions": [],
         "daily_loss_gate": {"blocked": False},
         "portfolio_exposure": {"delta_bias": "DELTA_BALANCED", "delta_bias_incl_resting": "DELTA_BALANCED",
                                "resting_entries": []}}
RISK = {"yolo_slot_enabled": True, "risk_per_trade_usdt": 5.0, "leverage_standard": 3}
OLD_CONV = "0dd0dd00-1111-4222-8333-444455556666"
NEW_CONV = "aaaaaaaa-1111-4222-8333-444455556666"


def live_payload(run_id, status="found", row=None, cause=None, **extra):
    """screening_pipeline.py --recheck --json output (as dict) echoing `run_id`."""
    return dict({"timestamp_utc": "2026-10-09 19:37:00 UTC", "pipeline_latency_ms": 10,
                 "macro": {"btc_price": 60000.0, "allows_alt_shorts": True, "btc_data_ok": True},
                 "top_candidates": [dict(row or LIVE_ROW)] if status == "found" else [],
                 "actionable_stat_arb": [], "top_funding_arbitrage": [], "news_catalysts_summary": [],
                 "yolo_slot_status": sp.RECHECK_YOLO_STATUS, "yolo_slot": {"status": "UNAVAILABLE", "candidates": []},
                 "market_data_status": None, "run_id": run_id, "macro_rejected_shorts": [],
                 "recheck": {"symbol": SYMBOL, "direction": DIRECTION, "setup_status": status, "cause": cause}},
                **extra)


# =============================================================================
# 1. Deterministic bounds (utils/recheck_bounds.py)
# =============================================================================
class TestRecheckBounds(unittest.TestCase):
    NOW = 1_800_000_000

    def old(self, **kw):
        return dict({"sha256": "a" * 64, "symbol": SYMBOL, "direction": DIRECTION, "tier": "S", "entry": 1.0,
                     "stop_loss": 0.97, "tp1": 1.054, "tp2": 1.15, "evaluated_ts": self.NOW - 1500}, **kw)  # 5R

    @staticmethod
    def new(status="APPROVED", **kw):
        # Issue #279: TP1 is part of the verdict (drift and friction floor), so the fixtures carry it
        cand = dict({"symbol": SYMBOL, "direction": DIRECTION, "tier": "S", "entry": 1.0, "stop_loss": 0.97,
                     "tp1": 1.054, "tp2": 1.15, "is_yolo": False}, **kw)
        return {"status": status, "approved_candidates": [cand] if status == "APPROVED" else []}

    def verdict(self, old=None, new=None, drift=0.25, age=1800):
        return rb.evaluate_recheck_bounds(old or self.old(), new or self.new(), self.NOW, drift, age)

    def failed(self, v):
        return {c["check"] for c in v["checks"] if not c["ok"]}

    def test_same_plan_is_within_bounds(self):
        v = self.verdict()
        self.assertTrue(v["within_bounds"], v)
        self.assertEqual(v["reasons"], [])
        self.assertEqual({c["check"] for c in v["checks"]},
                         {"status", "symbol_direction", "not_yolo", "tier", "drift_entry", "drift_stop_loss",
                          "drift_tp1", "drift_tp2", "stop_distance", "rr_tp2", "tp1_friction", "age_s"})

    def test_drift_in_r_per_field_at_and_over_the_limit(self):
        for key, base in (("entry", 1.0), ("stop_loss", 0.97), ("tp1", 1.054), ("tp2", 1.15)):
            with self.subTest(field=key):
                at = self.verdict(new=self.new(**{key: base + 0.0075}))  # exactly 0.25R
                self.assertTrue(at["within_bounds"], at)
                over = self.verdict(new=self.new(**{key: base + 0.0078}))  # 0.26R
                self.assertFalse(over["within_bounds"])
                # Issue #279: moving the entry or the SL alone also changes the stop distance by 0.26R
                self.assertEqual(self.failed(over), {f"drift_{key}"} | (
                    {"stop_distance"} if key in ("entry", "stop_loss") else set()))
                down = self.verdict(new=self.new(**{key: base - 0.0078}))  # drift is absolute
                self.assertIn(f"drift_{key}", self.failed(down))
        wide = self.verdict(new=self.new(entry=1.0078), drift=0.5)  # the profile bound is used
        self.assertTrue(wide["within_bounds"], wide)

    def test_tier_floor(self):
        self.assertEqual(self.failed(self.verdict(new=self.new(tier="A+"))), {"tier"})
        self.assertEqual(self.failed(self.verdict(new=self.new(tier="Tier A (Strong)"))), {"tier"})
        up_v = self.verdict(old=self.old(tier="A+"), new=self.new(tier="Tier S"))
        self.assertTrue(up_v["within_bounds"], up_v)
        self.assertTrue(self.verdict(old=self.old(tier="Tier A+ (High Score)"), new=self.new(tier="A+"))["within_bounds"])
        self.assertIn("tier", self.failed(self.verdict(new=self.new(tier="B+"))))  # unknown tier fails
        self.assertIn("tier", self.failed(self.verdict(old=self.old(tier=None))))

    def test_rr_floor(self):
        v = self.verdict(new=self.new(tp2=1.085), drift=3.0)  # R:R 2.83 (drift checks loosened to isolate it)
        self.assertEqual(self.failed(v), {"rr_tp2"})
        self.assertTrue(self.verdict(new=self.new(tp2=1.09), drift=3.0)["within_bounds"])  # exactly 3:1
        wrong_side = self.verdict(new=self.new(stop_loss=1.01), drift=3.0)
        self.assertIn("rr_tp2", self.failed(wrong_side))

    def test_age_ceiling(self):
        self.assertTrue(self.verdict(old=self.old(evaluated_ts=self.NOW - 1800))["within_bounds"])
        self.assertEqual(self.failed(self.verdict(old=self.old(evaluated_ts=self.NOW - 1801))), {"age_s"})
        self.assertIn("age_s", self.failed(self.verdict(old=self.old(evaluated_ts=self.NOW + 60))))
        self.assertIn("age_s", self.failed(self.verdict(old=self.old(evaluated_ts=None))))

    def test_status_not_approved_and_mismatches(self):
        for status in ("NEUTRAL", "REJECTED", ""):
            v = self.verdict(new=self.new(status=status))
            self.assertFalse(v["within_bounds"])
            self.assertIn("status", self.failed(v))
        self.assertIn("symbol_direction", self.failed(self.verdict(new=self.new(symbol="ZROUSDT"))))
        self.assertIn("symbol_direction", self.failed(self.verdict(new=self.new(direction="SHORT"))))
        self.assertIn("not_yolo", self.failed(self.verdict(new=self.new(is_yolo=True))))

    def test_short_and_malformed_levels(self):
        old = self.old(direction="SHORT", entry=1.0, stop_loss=1.03, tp1=0.946, tp2=0.88)
        new = {"status": "APPROVED", "approved_candidates": [{"symbol": SYMBOL, "direction": "SHORT", "tier": "S",
                                                             "entry": 0.999, "stop_loss": 1.03, "tp1": 0.946,
                                                             "tp2": 0.88}]}
        self.assertTrue(rb.evaluate_recheck_bounds(old, new, self.NOW, 0.25, 1800)["within_bounds"])
        for bad in ("x", None, True, float("nan"), 0, -1):
            with self.subTest(bad=bad):
                self.assertFalse(self.verdict(new=self.new(entry=bad))["within_bounds"])
        self.assertFalse(self.verdict(old=self.old(stop_loss=1.0))["within_bounds"])  # zero original R

    def test_format_lists_each_check(self):
        lines = rb.format_recheck_verdict(self.verdict(new=self.new(tier="A")))
        self.assertTrue(lines[0].startswith("OUT OF BOUNDS"))
        self.assertTrue(any(l.startswith("[FAIL] tier") and "limit >= S" in l for l in lines))
        self.assertTrue(any(l.startswith("[PASS] drift_entry: 0.0 (limit 0.25)") for l in lines), lines)
        self.assertTrue(rb.format_recheck_verdict(self.verdict())[0].startswith("WITHIN BOUNDS"))


# =============================================================================
# 2. Profile keys
# =============================================================================
class TestRecheckProfile(unittest.TestCase):

    def test_defaults_and_example(self):
        self.assertEqual(up.get_recheck_bounds({}), {"recheck_max_drift_r": 0.25, "recheck_max_age_seconds": 1800})
        self.assertEqual(up.get_recheck_bounds(None), up.get_recheck_bounds({}))
        with open(os.path.join(BASE_DIR, "config", "user_profile.json.example"), encoding="utf-8") as f:
            example = json.load(f)
        for key in ("recheck_max_drift_r", "recheck_max_age_seconds"):
            self.assertEqual(example[key], up.DEFAULT_PROFILE[key], key)

    def test_valid_and_invalid_values(self):
        # Issue #279: the valid range is (0, 0.5]
        self.assertEqual(up.get_recheck_bounds({"recheck_max_drift_r": 0.5, "recheck_max_age_seconds": 7200}),
                         {"recheck_max_drift_r": 0.5, "recheck_max_age_seconds": 7200})
        self.assertEqual(up.get_recheck_bounds({"recheck_max_drift_r": 0.1, "recheck_max_age_seconds": 300.0}),
                         {"recheck_max_drift_r": 0.1, "recheck_max_age_seconds": 300})
        for bad in (0, -0.1, 0.51, 1.0, 2, 2.01, "0.3", None, True, float("nan"), float("inf")):
            self.assertEqual(up.get_recheck_bounds({"recheck_max_drift_r": bad})["recheck_max_drift_r"], 0.25, bad)
        for bad in (299, 7201, "900", None, True, float("nan"), -1):
            self.assertEqual(up.get_recheck_bounds({"recheck_max_age_seconds": bad})["recheck_max_age_seconds"],
                             1800, bad)


# =============================================================================
# 3. Single-symbol screening (screening_pipeline.execute_symbol_recheck)
# =============================================================================
class TestSymbolRecheckPipeline(unittest.TestCase):

    def setUp(self):
        tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ban_file = os.path.join(tmp.name, "logs", "market_data_rate_limit.json")
        p = patch.object(rlg, "STATE_FILE", self.ban_file)
        p.start()
        self.addCleanup(p.stop)
        rlg.reset_for_tests()
        self.addCleanup(rlg.reset_for_tests)
        self.macro = sp.MacroContext(btc_price=60000.0, btc_regime="NEUTRAL_CONSOLIDATION", btc_regime_desc="x",
                                     btc_absorption="NONE", btc_taker_ratio=1.0, btc_cvd_30v=0.0, btc_oi_z_score=0.0,
                                     btc_tape_bias="BALANCED", btc_tape_imbalance=0.0, allows_alt_shorts=True)
        self.row = {"symbol": SYMBOL, "direction": DIRECTION, "confidence": 95, "price": 1.001, "trigger": 1.004,
                    "sl": 0.974, "tp1": 1.058, "tp2": 1.124, "tier": "Tier S", "vol_ratio": 2.1,
                    "alt_short_climax_ok": False}
        self.setup = sp.CandidateSetup(**{k: v for k, v in LIVE_ROW.items() if k in sp.CandidateSetup.model_fields})
        self.mocks = {}
        for target, kw in (("screening_pipeline.fetch_macro_btc", {"return_value": self.macro}),
                           ("broad_market_radar.analyze_single_symbol", {"side_effect": lambda s, i: dict(self.row)}),
                           ("broad_market_radar.fetch_funding_intervals", {"return_value": ({}, None)}),
                           ("broad_market_radar.enrich_candidate_microstructure",
                            {"side_effect": lambda c, f, funding_interval_unknown=False: c}),
                           ("screening_pipeline.enrich_and_size_candidate", {"return_value": self.setup}),
                           ("screening_pipeline._start_yolo_scan", {}),
                           ("broad_market_radar.scan_all_liquid_pairs", {})):
            p = patch(target, **kw)
            self.mocks[target.rsplit(".", 1)[1]] = p.start()
            self.addCleanup(p.stop)

    def run_recheck(self, symbol=SYMBOL, direction=DIRECTION):
        with patch("sys.stderr", io.StringIO()):
            payload = sp.execute_symbol_recheck(symbol, direction, target_env="prod")
        self.mocks["_start_yolo_scan"].assert_not_called()  # include_yolo=False semantics
        self.mocks["scan_all_liquid_pairs"].assert_not_called()  # one symbol, never the 80-pair scan
        return payload

    def test_found(self):
        payload = self.run_recheck()
        self.assertEqual(payload.recheck, {"symbol": SYMBOL, "direction": DIRECTION, "setup_status": "found",
                                           "cause": None})
        self.assertEqual([c.symbol for c in payload.top_candidates], [SYMBOL])
        self.assertEqual((payload.actionable_stat_arb, payload.top_funding_arbitrage), ([], []))
        self.assertEqual(payload.yolo_slot.candidates, [])
        self.assertIsNone(payload.market_data_status)
        self.mocks["analyze_single_symbol"].assert_called_once_with(SYMBOL, bmr.DEFAULT_INTERVAL)
        self.assertFalse(rlg.is_enabled())  # the scan session restored the guard

    def assertNoSetup(self, fragment, **kw):
        payload = self.run_recheck(**kw)
        self.assertEqual(payload.recheck["setup_status"], "no_setup", payload.recheck)
        self.assertIn(fragment, payload.recheck["cause"])
        self.assertEqual(payload.top_candidates, [])
        return payload

    def test_no_setup_causes(self):
        self.mocks["analyze_single_symbol"].side_effect = lambda s, i: None
        self.assertNoSetup("radar: no setup for ETHFIUSDT")
        self.mocks["analyze_single_symbol"].side_effect = lambda s, i: dict(self.row, direction="SHORT")
        self.assertNoSetup("now scores SHORT, not LONG")
        self.mocks["analyze_single_symbol"].side_effect = lambda s, i: dict(
            self.row, risk_pct_over_ceiling=True, disqualify_reason="risk_pct 5.20% > 5.0% intraday ceiling")
        self.assertNoSetup("risk_pct 5.20% > 5.0%")
        self.mocks["analyze_single_symbol"].side_effect = lambda s, i: dict(self.row, confidence=50)
        self.assertNoSetup("confidence 50 < 55")
        self.mocks["analyze_single_symbol"].side_effect = lambda s, i: dict(self.row)
        self.mocks["enrich_and_size_candidate"].return_value = None
        self.assertNoSetup("sizing")

    def test_alt_short_macro_gate(self):
        self.macro.allows_alt_shorts = False
        self.mocks["analyze_single_symbol"].side_effect = lambda s, i: dict(self.row, direction="SHORT")
        payload = self.assertNoSetup("alt-short macro gate: alt_shorts_not_allowed", direction="SHORT")
        self.assertEqual(payload.macro_rejected_shorts[0]["symbol"], SYMBOL)
        self.mocks["enrich_and_size_candidate"].assert_not_called()

    def test_persisted_ban_is_unavailable_without_binance_calls(self):
        os.makedirs(os.path.dirname(self.ban_file), exist_ok=True)
        with open(self.ban_file, "w", encoding="utf-8") as f:
            json.dump({"banned_until": time.time() + 600, "status": 418, "updated_ts": time.time()}, f)
        payload = self.run_recheck()
        self.assertEqual(payload.recheck["setup_status"], "unavailable")
        self.assertTrue(payload.recheck["cause"].startswith("UNAVAILABLE: Binance rate limit"))
        self.assertTrue(payload.market_data_status.startswith("UNAVAILABLE"))
        self.assertEqual(payload.top_candidates, [])
        self.assertFalse(payload.macro.allows_alt_shorts)
        self.mocks["analyze_single_symbol"].assert_not_called()
        self.mocks["fetch_macro_btc"].assert_not_called()

    def test_ban_tripped_during_the_recheck_is_unavailable(self):
        def tripped(symbol, interval):
            rlg.trip(429)  # a fetch inside the radar hit a 429 and swallowed it
            return dict(self.row)
        self.mocks["analyze_single_symbol"].side_effect = tripped
        payload = self.run_recheck()
        self.assertEqual(payload.recheck["setup_status"], "unavailable")
        self.assertEqual(payload.top_candidates, [])
        with open(self.ban_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], 429)  # persisted like a full scan

    def test_failed_step_is_unavailable(self):
        self.mocks["fetch_funding_intervals"].side_effect = ValueError("boom")
        payload = self.run_recheck()
        self.assertEqual(payload.recheck["setup_status"], "unavailable")
        self.assertIn("ValueError", payload.recheck["cause"])
        self.assertEqual(payload.top_candidates, [])

    def test_cli_flag(self):
        with patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(sp.main(["--json", "--env", "prod", "--recheck", "ETHFIUSDT"]), 2)
        self.assertIn("SYMBOL:DIRECTION", err.getvalue())
        out = io.StringIO()
        with patch.object(sp, "execute_symbol_recheck", return_value=self.run_recheck()) as run, \
                patch.object(sp, "execute_screening_pipeline") as full, patch("sys.stdout", out):
            self.assertEqual(sp.main(["--json", "--env", "prod", "--recheck", "ethfiusdt:long"]), 0)
        run.assert_called_once_with("ETHFIUSDT", "LONG", target_env="prod")
        full.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["recheck"]["setup_status"], "found")


# =============================================================================
# 4. The --recheck brief and the recorder link
# =============================================================================
class _RecheckWorkspace(tdp.TranscriptFixture):

    def setUp(self):
        super().setUp()
        # Issue #284: CLAUDE_CODE_SESSION_ID selects a dossier file; these fixtures run without one (hermetic)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(rcb.SESSION_ENV, None)
        self.brief_path = os.path.join(self.workspace, "logs", "primed_brief.json")
        self.dossier_path = dp.default_dossier_path(self.workspace)

    def write_old_dossier(self, cand=None, status="APPROVED", age_s=1500, env="PROD"):
        """The original dossier the user confirmed, recorded `age_s` ago (expired after 1200 s)."""
        payload = {"status": status, "evaluator_agent": dp.EVALUATOR_NAME, "target_env": env,
                   "approved_candidates": [dict(cand or OLD_CAND)] if status == "APPROVED" else [],
                   "summary": "original evaluation"}
        ts = self.now - age_s
        path = self.standard_transcript(OLD_CONV, payload, ts)
        record = dp.build_record_from_extraction(dp.extract_dossier_from_transcript(path), recorded_at_ts=ts + 5)
        record["target_env"] = env.lower()
        # Issue #270: like record_evaluation._persist, the session's own file gets the same record
        for path in (self.dossier_path, dp.session_dossier_path(self.workspace, record["parent_conversation_id"])):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(record, f)
        return record

    def run_brief(self, spec=f"{SYMBOL}:{DIRECTION}", payload_fn=live_payload, risk=None, insights_file=None):
        """`insights_file`: a temp lesson ledger read by the real lesson selection (else no lessons)."""
        out, err = io.StringIO(), io.StringIO()
        lessons = (patch.object(peb, "INSIGHTS_FILE", insights_file) if insights_file
                   else patch.object(peb, "load_recent_insights", return_value=[]))
        with patch.object(peb, "BASE_DIR", self.workspace), patch.object(peb, "BRIEF_FILE", self.brief_path), \
                patch.object(peb, "ensure_fresh_state", side_effect=lambda **k: json.loads(json.dumps(STATE))), \
                lessons, \
                patch.object(peb, "build_risk_profile", return_value=dict(risk or RISK)), \
                patch.object(peb, "get_latest_screening_payload") as full, \
                patch.object(peb, "_record_pipeline_failure") as yolo_failure, \
                patch.object(rcb, "fetch_recheck_payload",
                             side_effect=lambda s, d, e, run_id, base: payload_fn(run_id)) as fetch, \
                redirect_stdout(out), redirect_stderr(err):
            code = peb.main(["--env", "prod", "--json", "--recheck", spec])
        full.assert_not_called()  # never the full scan
        yolo_failure.assert_not_called()  # a re-check is not a YOLO scan run
        self.fetch = fetch
        brief = None
        if code == 0:
            with open(self.brief_path, encoding="utf-8") as f:
                brief = json.load(f)
        return code, brief, err.getvalue()

    def record_new(self, payload, brief, conv=NEW_CONV, ts=None):
        """The re-check evaluator's dossier, recorded like --from-subagent (stdout captured)."""
        ts = ts if ts is not None else int(brief["generated_at_ts"]) + 2
        self.standard_transcript(conv, payload, ts)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(rec, "_register_shadow"), redirect_stdout(out), redirect_stderr(err):
            record = rec.record_from_subagent(conv, target_env="prod", base_dir=self.workspace, now_ts=ts + 3)
        return record, out.getvalue(), err.getvalue()

    @staticmethod
    def new_payload(brief, status="APPROVED", **cand):
        c = dict({"symbol": SYMBOL, "direction": DIRECTION, "tier": "S", "entry": 1.004, "stop_loss": 0.974,
                  "tp1": 1.058, "tp2": 1.124, "leverage": 3, "score": 95, "is_yolo": False,
                  "requires_user_confirmation": True}, **cand)
        return {"status": status, "evaluator_agent": dp.EVALUATOR_NAME, "target_env": "PROD",
                "brief_source": "file", "brief_generated_at_ts": brief["generated_at_ts"],
                "approved_candidates": [c] if status == "APPROVED" else [], "summary": f"re-check {status}"}

    def history(self):
        with open(os.path.join(self.workspace, "logs", "evaluations", "evaluations_history.jsonl"),
                  encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class TestRecheckBrief(_RecheckWorkspace):

    def test_fresh_single_candidate_brief_from_the_live_pipeline(self):
        old = self.write_old_dossier()
        with open(self.dossier_path, "rb") as f:
            dossier_bytes = f.read()
        # A previous full-scan brief with the old levels: the re-check never reads it
        with open(self.brief_path, "w", encoding="utf-8") as f:
            json.dump({"filtered_opportunities": [dict(LIVE_ROW, trigger_price=1.0, sl_price=0.97)]}, f)
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fetch.call_args[0][:3], (SYMBOL, DIRECTION, "prod"))
        self.assertEqual([(o["symbol"], o["trigger_price"], o["sl_price"]) for o in brief["filtered_opportunities"]],
                         [(SYMBOL, 1.004, 0.974)])
        self.assertEqual(brief["recheck"], {"symbol": SYMBOL, "direction": DIRECTION, "setup_status": "found",
                                            "cause": None})
        self.assertEqual(brief["recheck_of"], {
            "sha256": old["provenance"]["sha256"], "symbol": SYMBOL, "direction": DIRECTION, "tier": "S",
            "score": 95, "entry": 1.0, "stop_loss": 0.97, "tp1": 1.054, "tp2": 1.12, "leverage": 3,
            "evaluated_ts": old["timestamp_ts"], "valid_until_ts": old["valid_until_ts"]})
        # Normal header and ground truth, minimal-but-present blocks
        for key in ("generated_at_ts", "max_age_seconds", "target_env", "market_data_status", "risk_profile",
                    "ground_truth_portfolio", "pending_entries", "macro_btc", "daily_loss_gate"):
            self.assertIn(key, brief)
        self.assertEqual((brief["pending_entries_status"], brief["state_sync"]), ("OK", "OK"))
        # Issue #271 compact shapes of the (empty) Engine 2 blocks; no lesson was dropped
        self.assertEqual(brief["committed_memory_lessons"], [])
        self.assertEqual(brief["stat_arb_pairs"], {"pairs_scanned": 0, "actionable": 0, "rows": [], "near_miss": []})
        self.assertEqual(brief["funding_arbitrage_desk"], {"rows": 0})
        self.assertNotIn("dropped_lessons", brief)
        self.assertNotIn("lesson_budget_exceeded", brief)
        self.assertEqual(brief["yolo_slot"]["candidates"], [])
        self.assertEqual(brief["yolo_slot"]["status"], "UNAVAILABLE")
        self.assertEqual(brief["target_env"], "PROD")
        self.assertLessEqual(peb._brief_bytes(brief), peb.BRIEF_BUDGET_BYTES)
        # The sidecar row (Tier S calibration snapshot) and no dossier write
        with open(os.path.join(self.workspace, "logs", "primed_brief_scores.json"), encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual([(r["symbol"], r["direction"], r["confidence"]) for r in sidecar["rows"]],
                         [(SYMBOL, DIRECTION, 95)])
        self.assertEqual(sidecar["generated_at_ts"], brief["generated_at_ts"])
        with open(self.dossier_path, "rb") as f:
            self.assertEqual(f.read(), dossier_bytes)

    def test_markdown_output_names_the_recheck(self):
        self.write_old_dossier()
        out = io.StringIO()
        with patch.object(peb, "BASE_DIR", self.workspace), patch.object(peb, "BRIEF_FILE", self.brief_path), \
                patch.object(peb, "ensure_fresh_state", return_value=dict(STATE)), \
                patch.object(peb, "load_recent_insights", return_value=[]), \
                patch.object(peb, "build_risk_profile", return_value=dict(RISK)), \
                patch.object(rcb, "fetch_recheck_payload", side_effect=lambda s, d, e, r, b: live_payload(r)), \
                redirect_stdout(out):
            self.assertEqual(peb.main(["--env", "prod", "--recheck", "ETHFIUSDT:LONG"]), 0)
        self.assertIn("**Re-check:** ETHFIUSDT LONG -> `found`", out.getvalue())

    def test_recheck_blocks_count_toward_the_lesson_budget(self):
        """With a ledger larger than the budget (#271 selection), the re-check brief, recheck blocks included,
        stays under BRIEF_BUDGET_BYTES, keeps the candidate's own lesson and lists the dropped ones."""
        self.write_old_dossier()
        ledger = [t187.lesson(f"ins-17000000{i:02d}-aaaa{i:02d}", "UNI", "SHORT", text=f"{i} " + "u" * 400)
                  for i in range(30)]
        ledger.insert(0, t187.lesson("ins-1699999999-ethfi0", "ETHFI", "LONG", text="ETHFI long lesson " + "e" * 300))
        path = os.path.join(self.workspace, "trade_insights.jsonl")
        t187.write_jsonl(path, ledger)
        code, brief, err = self.run_brief(insights_file=path)
        self.assertEqual(code, 0, err)
        self.assertLessEqual(peb._brief_bytes(brief), peb.BRIEF_BUDGET_BYTES)
        self.assertEqual(brief["recheck"]["setup_status"], "found")
        self.assertIn("recheck_of", brief)
        self.assertTrue(brief["committed_memory_lessons"])
        self.assertTrue(brief["committed_memory_lessons"][0]["lesson"].startswith("ETHFI long lesson"))
        self.assertTrue(brief["dropped_lessons"])
        self.assertIn("WARNING: lesson budget", err)

    def test_no_setup_and_unavailable_variants(self):
        self.write_old_dossier()
        cases = [
            (lambda r: live_payload(r, status="no_setup", cause="radar: ETHFIUSDT now scores SHORT, not LONG"),
             "no_setup", "now scores SHORT"),
            (lambda r: {}, "unavailable", "screening pipeline failed"),
            (lambda r: live_payload("another-run"), "unavailable", "run id mismatch"),
            (lambda r: dict(live_payload(r), recheck={"symbol": "ZROUSDT", "direction": DIRECTION,
                                                      "setup_status": "found"}), "unavailable", "another symbol"),
            (lambda r: live_payload(r, status="unavailable", cause="UNAVAILABLE: Binance rate limit (HTTP 418)",
                                    market_data_status="UNAVAILABLE: Binance rate limit (HTTP 418), retry after x"),
             "unavailable", "rate limit"),
            (lambda r: live_payload(r, top_candidates=[]), "unavailable", "without its candidate row"),
            (lambda r: live_payload(r, top_candidates=[dict(LIVE_ROW, direction="SHORT")]), "unavailable",
             "without its candidate row"),
        ]
        for payload_fn, status, fragment in cases:
            with self.subTest(fragment=fragment):
                code, brief, err = self.run_brief(payload_fn=payload_fn)
                self.assertEqual(code, 0, err)
                self.assertEqual(brief["recheck"]["setup_status"], status)
                self.assertIn(fragment, brief["recheck"]["cause"])
                self.assertEqual(brief["filtered_opportunities"], [])
                self.assertIn("recheck_of", brief)
        code, brief, _ = self.run_brief(payload_fn=lambda r: live_payload(
            r, market_data_status="UNAVAILABLE: Binance rate limit (HTTP 429), retry after x"))
        self.assertEqual(brief["market_data_status"][:11], "UNAVAILABLE")
        self.assertEqual((brief["recheck"]["setup_status"], brief["filtered_opportunities"]), ("unavailable", []))

    def test_invalid_argument_and_refusals_write_no_brief(self):
        self.write_old_dossier()
        for spec in ("ETHFIUSDT", "ETHFIUSDT:UP", ":LONG", "ETHFI-USDT:LONG", "A:B:LONG"):
            with self.subTest(spec=spec):
                code, _, err = self.run_brief(spec=spec)
                self.assertEqual(code, 2)
                self.assertIn("SYMBOL:DIRECTION", err)
                self.assertFalse(os.path.exists(self.brief_path))
                self.fetch.assert_not_called()
        code, _, err = self.run_brief(spec="ETHFIUSDT:SHORT")
        self.assertEqual(code, 2)
        self.assertIn("does not approve ETHFIUSDT SHORT", err)
        code, _, err = self.run_brief(spec="ZROUSDT:LONG")
        self.assertIn("does not approve ZROUSDT LONG", err)
        self.assertFalse(os.path.exists(self.brief_path))

    def test_yolo_candidate_needs_a_full_scan(self):
        self.write_old_dossier(cand=dict(OLD_CAND, is_yolo=True, tier="A"))
        code, _, err = self.run_brief()
        self.assertEqual(code, 2)
        self.assertIn("needs a full scan", err)
        self.fetch.assert_not_called()

    def test_snapshot_only_from_a_verified_approved_record(self):
        # Forged: a verified record edited to approve another symbol (hash still matches, fingerprint does not)
        record = self.write_old_dossier()
        record["approved_symbols"].append("ZROUSDT")
        record["approved_candidates"].append(dict(OLD_CAND, symbol="ZROUSDT"))
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        code, _, err = self.run_brief(spec="ZROUSDT:LONG")
        self.assertEqual(code, 2)
        self.assertIn("not provenance-verified", err)
        # Edited levels are never used: the snapshot comes from the rebuilt record
        record = self.write_old_dossier()
        record["approved_candidates"][0]["entry"] = 0.5
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        self.assertEqual(brief["recheck_of"]["entry"], 1.0)
        # Hand-written legacy dossier, missing dossier, NEUTRAL dossier, another environment. Issue #270: the
        # session's own file is removed too, else its verified plan (still on disk) would be re-checked
        os.remove(dp.session_dossier_path(self.workspace, record["parent_conversation_id"]))
        now = int(time.time())
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump({"timestamp_ts": now, "valid_until_ts": now + 1200, "status": "APPROVED",
                       "evaluator_agent": dp.EVALUATOR_NAME, "approved_symbols": [SYMBOL],
                       "approved_candidates": [OLD_CAND]}, f)
        self.assertIn("not provenance-verified", self.run_brief()[2])
        os.remove(self.dossier_path)
        self.assertIn("no readable evaluation dossier", self.run_brief()[2])
        self.write_old_dossier(status="NEUTRAL")
        self.assertIn("NEUTRAL, not APPROVED", self.run_brief()[2])
        self.write_old_dossier(env="TESTNET")
        self.assertIn("evaluated for TESTNET, not PROD", self.run_brief()[2])
        self.fetch.assert_not_called()

    def test_pipeline_subprocess_argv(self):
        class Done:
            returncode, stdout = 0, json.dumps(live_payload("rid"))
        with patch("subprocess.run", return_value=Done()) as run:
            payload = rcb.fetch_recheck_payload(SYMBOL, DIRECTION, "prod", "rid", self.workspace)
        argv = run.call_args[0][0]
        self.assertEqual(argv[1:], [os.path.join(self.workspace, "scripts", "screening_pipeline.py"), "--json",
                                    "--env", "prod", "--recheck", "ETHFIUSDT:LONG"])
        self.assertEqual(run.call_args[1]["env"][rcb.RUN_ID_ENV], "rid")
        self.assertEqual(run.call_args[1]["timeout"], 60)
        self.assertEqual(payload["recheck"]["setup_status"], "found")
        with patch("subprocess.run", side_effect=OSError("x")):  # issue #279: the failure class is kept
            self.assertEqual(rcb.fetch_recheck_payload(SYMBOL, DIRECTION, "prod", "rid", self.workspace),
                             {"recheck_failure": "OSError"})


class TestRecorderRecheckLink(_RecheckWorkspace):

    def recheck(self, **cand):
        old = self.write_old_dossier()
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        return old, brief

    def test_within_bounds_links_outside_the_hash(self):
        old, brief = self.recheck()
        payload = self.new_payload(brief)
        record, out, err = self.record_new(payload, brief)
        self.assertEqual(record["recheck_of"]["sha256"], old["provenance"]["sha256"])
        self.assertTrue(record["recheck_bounds"]["within_bounds"], record["recheck_bounds"])
        self.assertEqual(record["recheck_bounds"]["bounds"],
                         {"recheck_max_drift_r": 0.25, "recheck_max_age_seconds": 1800})
        self.assertIn("WITHIN BOUNDS", out)
        self.assertIn(f"Re-check of dossier sha256 {old['provenance']['sha256'][:16]}", out)
        self.assertIn("Deadline: valid until", out)
        self.assertIn("[PASS] drift_entry: 0.1333 (limit 0.25)", out)
        # Stored outside the hash: provenance and the verdict fingerprint are those of the evaluator's block
        stored = dp.load_dossier(self.dossier_path)
        self.assertEqual(stored["recheck_of"], brief["recheck_of"])
        ok, reason, rebuilt = dp.rebuild_verified_record(stored)
        self.assertTrue(ok, reason)
        self.assertEqual(dp._verdict_fingerprint(rebuilt), dp._verdict_fingerprint(stored))
        self.assertNotIn("recheck_of", stored["raw_payload"])
        self.assertNotEqual(stored["provenance"]["sha256"], old["provenance"]["sha256"])
        # Radar snapshot of the re-check sidecar (Tier S calibration input)
        self.assertEqual(stored["radar_snapshots"][f"{SYMBOL}|{DIRECTION}"]["radar_snapshot"]["confidence"], 95)
        # History row carries the old sha256; the trade gate accepts it like any APPROVED dossier
        row = self.history()[-1]
        self.assertEqual((row["recheck_of"], row["recheck_within_bounds"]), (old["provenance"]["sha256"], True))
        ok, reason, cand = dp.validate_dossier_for_trade(SYMBOL, DIRECTION, env="prod", base_dir=self.workspace,
                                                        now_ts=int(brief["generated_at_ts"]) + 10)
        self.assertTrue(ok, reason)
        self.assertEqual(cand["entry"], 1.004)

    def test_a_recheck_dossier_is_never_re_checked(self):
        """Audit round 1: drift and age are measured against the ORIGINAL plan only, so a latest dossier that is
        itself a re-check (within or out of bounds) refuses a second --recheck before any market read."""
        for cand in ({}, {"entry": 1.009}):  # within bounds, then 0.3R out of bounds
            with self.subTest(cand=cand):
                _, brief = self.recheck()
                record, _, _ = self.record_new(self.new_payload(brief, **cand), brief)
                self.assertIn("recheck_of", record)
                with open(self.brief_path, "rb") as f:
                    brief_bytes = f.read()
                code, _, err = self.run_brief()
                self.assertEqual(code, 2)
                # Issue #284: the refusal names the session of the dossier that is already a re-check
                self.assertIn(f"the dossier of session {record['parent_conversation_id']} is already a re-check: "
                              "ask the user again or run a full scan", err)
                self.fetch.assert_not_called()
                with open(self.brief_path, "rb") as f:
                    self.assertEqual(f.read(), brief_bytes)  # no new brief written

    def test_drift_beyond_bounds_asks_again(self):
        _, brief = self.recheck()
        record, out, _ = self.record_new(self.new_payload(brief, entry=1.02, stop_loss=0.99, tp2=1.14), brief)
        self.assertFalse(record["recheck_bounds"]["within_bounds"])
        self.assertIn("OUT OF BOUNDS", out)
        self.assertIn("ask the user again", out)
        self.assertIn("[FAIL] drift_entry", out)
        self.assertFalse(self.history()[-1]["recheck_within_bounds"])

    def test_profile_bounds_are_used(self):
        os.makedirs(os.path.join(self.workspace, "config"), exist_ok=True)
        with open(os.path.join(self.workspace, "config", "user_profile.json"), "w", encoding="utf-8") as f:
            json.dump({"recheck_max_drift_r": 0.5}, f)  # issue #279: the profile maximum
        _, brief = self.recheck()
        # 0.4R on trigger, SL and TP2 (out of the default 0.25R), the stop distance unchanged
        record, out, _ = self.record_new(self.new_payload(brief, entry=1.012, stop_loss=0.982, tp2=1.132), brief)
        self.assertTrue(record["recheck_bounds"]["within_bounds"], record["recheck_bounds"])
        self.assertIn("WITHIN BOUNDS", out)

    def test_setup_gone_means_no_trade(self):
        self.write_old_dossier()
        code, brief, err = self.run_brief(payload_fn=lambda r: live_payload(
            r, status="no_setup", cause="radar: no setup for ETHFIUSDT"))
        self.assertEqual(code, 0, err)
        record, out, _ = self.record_new(self.new_payload(brief, status="NEUTRAL"), brief)
        self.assertEqual(record["status"], "NEUTRAL")
        self.assertFalse(record["recheck_bounds"]["within_bounds"])
        self.assertIn("the re-check dossier is NEUTRAL: no trade", out)
        self.assertIn("No trade authorized", out)
        ok, reason, _ = dp.validate_dossier_for_trade(SYMBOL, DIRECTION, env="prod", base_dir=self.workspace,
                                                     now_ts=int(brief["generated_at_ts"]) + 10)
        self.assertFalse(ok)
        self.assertIn("NEUTRAL", reason)

    def test_tier_downgrade_asks_again(self):
        _, brief = self.recheck()
        record, out, _ = self.record_new(self.new_payload(brief, tier="A+"), brief)
        self.assertFalse(record["recheck_bounds"]["within_bounds"])
        self.assertIn("[FAIL] tier: A+ (limit >= S)", out)
        self.assertIn("OUT OF BOUNDS", out)

    def test_fails_soft_without_a_linked_recheck_brief(self):
        # No brief at all: recorded, never linked; issue #279: an explicit no-verdict line instead of silence
        record, out, err = self.record_new(self.new_payload({"generated_at_ts": self.now}), {"generated_at_ts":
                                                                                            self.now - 2})
        self.assertNotIn("recheck_of", record)
        self.assertNotIn("Re-check of dossier", out)
        self.assertIn("Re-check: NOT LINKED (no verdict)", out)
        self.assertIn("RE-CHECK NOT LINKED", err)
        self.assertNotIn("recheck_of", self.history()[-1])
        # A normal brief (no recheck_of)
        with open(self.brief_path, "w", encoding="utf-8") as f:
            json.dump({"generated_at_ts": self.now, "target_env": "PROD"}, f)
        record, out, err = self.record_new(self.new_payload({"generated_at_ts": self.now}), {"generated_at_ts":
                                                                                            self.now - 2})
        self.assertNotIn("recheck_of", record)
        self.assertEqual(err, "")
        self.assertNotIn("Re-check", out)
        # A re-check brief this dossier was not evaluated on
        _, brief = self.recheck()
        payload = dict(self.new_payload(brief), brief_generated_at_ts=int(brief["generated_at_ts"]) - 500)
        record, out, err = self.record_new(payload, brief)
        self.assertNotIn("recheck_of", record)
        self.assertIn("RE-CHECK NOT LINKED", err)
        self.assertNotIn("WITHIN BOUNDS", out)
        self.assertIn("Re-check: NOT LINKED (no verdict)", out)

    def test_no_link_without_brief_generated_at_ts(self):
        """Issue #279: the 600 s window fallback is gone; a re-check dossier without brief_generated_at_ts is never
        linked, however close to the brief it was emitted."""
        _, brief = self.recheck()
        payload = self.new_payload(brief)
        payload.pop("brief_generated_at_ts")
        for conv, ts in (("bbbbbbbb-1111-4222-8333-444455556666", int(brief["generated_at_ts"]) + 2),
                         ("cccccccc-1111-4222-8333-444455556666", int(brief["generated_at_ts"]) + 605),
                         ("dddddddd-1111-4222-8333-444455556666", int(brief["generated_at_ts"]) - 1)):
            with self.subTest(ts=ts):
                record, out, err = self.record_new(payload, brief, conv=conv, ts=ts)
                self.assertNotIn("recheck_of", record)
                self.assertNotIn("recheck_bounds", record)
                self.assertIn("RE-CHECK NOT LINKED", err)
                self.assertIn("Re-check: NOT LINKED (no verdict)", out)
                self.assertNotIn("WITHIN BOUNDS", out)
                self.assertNotIn("recheck_of", self.history()[-1])


# =============================================================================
# 5. The hook and the executor accept a re-check dossier like any APPROVED dossier
# =============================================================================
RECHECK_EXTRA = {"recheck_of": {"sha256": "b" * 64, "symbol": "BTCUSDT", "direction": "LONG", "tier": "S",
                                "entry": 1.0, "stop_loss": 0.97, "tp2": 1.12, "evaluated_ts": 1},
                 "recheck_bounds": {"within_bounds": False, "checks": [], "reasons": ["x"]}}


class TestGuardAcceptsRecheckDossier(tgb.GuardHarness):

    def deploy(self, symbol="BTCUSDT", direction="LONG"):
        return self.agy(self.cmd(f"python3 scripts/execute_futures_trade.py --symbol {symbol} --direction "
                                 f"{direction} --leverage 3 --env prod"))

    def write_recheck_record(self, age_s=0):
        record = self.write_provenance_dossier()
        if age_s:  # an older evaluator transcript: the same record, emitted age_s ago
            path = record["provenance"]["transcript_path"]
            rows = tdp.TranscriptFixture.load_rows(path)
            rows[1]["created_at"] = datetime.datetime.fromtimestamp(
                time.time() - age_s, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            tdp.TranscriptFixture.dump_rows(path, rows)
            record = tgb.add_radar_snapshots(dp.build_record_from_extraction(dp.extract_dossier_from_transcript(path)))
        record.update(json.loads(json.dumps(RECHECK_EXTRA)))
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return record

    def test_recheck_dossier_allowed_like_any_approved_dossier(self):
        self.write_recheck_record()
        res = self.deploy()
        self.assertEqual(res.get("decision"), "allow", res)
        self.assertDenied(self.deploy(direction="SHORT"))  # direction still enforced

    def test_expired_original_still_denied(self):
        self.write_recheck_record(age_s=1500)
        self.assertDenied(self.deploy(), "expired")


class TestExecutorAcceptsRecheckDossier(teg._TempWorkspace):

    def write(self, created=None):
        record = self.write_subagent_dossier([{"symbol": "SOLUSDT", "direction": "LONG", "tier": "S"}],
                                             created=created)
        record.update(json.loads(json.dumps(RECHECK_EXTRA)))
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)

    def test_recheck_dossier_accepted(self):
        self.write()
        ok, reason, cand = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "prod", base_dir=self.root)
        self.assertTrue(ok, reason)
        self.assertEqual(cand["symbol"], "SOLUSDT")

    def test_expired_original_still_rejected(self):
        self.write(created=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1500))
        ok, reason, _ = eft.enforce_evaluation_dossier("SOLUSDT", "LONG", "prod", confirmed=True, base_dir=self.root)
        self.assertFalse(ok)
        self.assertIn("expired", reason)


if __name__ == "__main__":
    unittest.main()
