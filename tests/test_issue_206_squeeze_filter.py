#!/usr/bin/env python3
"""
Issue #206: squeeze filter for SHORT candidates and the macro rule for altcoin shorts.

- utils/squeeze_filter.py: thresholds, fail-closed micro data, BTC resistance-rejection proxy, alt-short rule.
- Radar enrichment: a SHORT with squeeze risk (or no micro data) is capped at Tier A (score 64, sum rule kept);
  a crowded LONG is only flagged.
- Screening pipeline: hard macro gate for non-BTC SHORTs, fail-closed BTC macro on errors.
- Brief: flags forwarded, SQZ marker, sidecar flag, macro_rejected_shorts, fail-closed default macro.
- Evaluator prompt: RULE 9 and the squeezed-SHORT few-shot.

Offline: urllib is blocked, the brief and the pipeline write only to temp directories.
"""

import json
import math
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broad_market_radar as bmr  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import screening_pipeline as sp  # noqa: E402
from utils import squeeze_filter as sqf  # noqa: E402
import test_analytics_cli as tac  # noqa: E402  (fixtures only)
from test_issue_52_yolo_in_pipeline import _PipelineFakes, _setup  # noqa: E402  (fixtures only)


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch, _ban_dir, _ban_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()
    from utils import rate_limit_guard
    _ban_dir = tempfile.TemporaryDirectory()
    _ban_patch = patch.object(rate_limit_guard, "STATE_FILE",
                              os.path.join(_ban_dir.name, "market_data_rate_limit.json"))
    _ban_patch.start()
    rate_limit_guard.reset_for_tests()


def tearDownModule():
    from utils import rate_limit_guard
    _net_patch.stop()
    _ban_patch.stop()
    _ban_dir.cleanup()
    rate_limit_guard.reset_for_tests()


def _macro(regime="NEUTRAL_CONSOLIDATION", absorption="NONE", allows=True):
    return sp.MacroContext(btc_price=60000.0, btc_regime=regime, btc_regime_desc="", btc_absorption=absorption,
                           btc_taker_ratio=1.0, btc_cvd_30v=0.0, btc_oi_z_score=0.0, btc_tape_bias="BALANCED",
                           btc_tape_imbalance=0.0, allows_alt_shorts=allows)


# =============================================================================
# 1. Pure helper
# =============================================================================
class TestSqueezeFilter(unittest.TestCase):

    def test_oi_z_boundary(self):
        self.assertEqual(sqf.short_squeeze_reasons({"oi_z_score": 1.99, "funding_rate_pct": 0.01}), [])
        self.assertEqual(sqf.short_squeeze_reasons({"oi_z_score": 2.0, "funding_rate_pct": 0.01}),
                         ["oi_z>=2.0 (oi_z=2.00)"])

    def test_funding_boundary(self):
        self.assertEqual(sqf.short_squeeze_reasons({"oi_z_score": 0.2, "funding_rate_pct": -0.0099}), [])
        self.assertEqual(sqf.short_squeeze_reasons({"oi_z_score": 0.2, "funding_rate_pct": -0.01}),
                         ["funding<=-0.01% (funding=-0.0100%)"])
        self.assertEqual(sqf.short_squeeze_reasons({"oi_z_score": 2.91, "funding_rate_pct": -0.0211}),
                         ["oi_z>=2.0 (oi_z=2.91)", "funding<=-0.01% (funding=-0.0211%)"])

    def test_numpy_scalars_are_numbers(self):
        self.assertEqual(sqf.short_squeeze_reasons({"oi_z_score": np.float32(2.5), "funding_rate_pct": np.int64(0)}),
                         ["oi_z>=2.0 (oi_z=2.50)"])
        self.assertTrue(sqf.alt_short_macro_reason("ETHUSDT", False, {"allows_alt_shorts": True},
                                                   np.float32(1.5)).endswith("(vol_ratio=1.50)"))

    def test_missing_or_bad_micro_fails_closed(self):
        for micro in (None, {}, {"oi_z_score": 0.2}, {"funding_rate_pct": 0.01},
                      {"oi_z_score": float("nan"), "funding_rate_pct": 0.01},
                      {"oi_z_score": 0.2, "funding_rate_pct": math.inf},
                      {"oi_z_score": "0.2", "funding_rate_pct": 0.01},
                      {"oi_z_score": None, "funding_rate_pct": 0.01},
                      {"oi_z_score": True, "funding_rate_pct": 0.01}):
            self.assertEqual(sqf.short_squeeze_reasons(micro), ["micro_unavailable"], micro)

    def test_long_crowding_boundaries(self):
        self.assertEqual(len(sqf.long_crowding_reasons({"oi_z_score": 2.0, "funding_rate_pct": 0.05})), 2)
        self.assertEqual(sqf.long_crowding_reasons({"oi_z_score": 2.0, "funding_rate_pct": 0.0499}), [])
        self.assertEqual(sqf.long_crowding_reasons({"oi_z_score": 1.99, "funding_rate_pct": 0.2}), [])
        for micro in (None, {}, {"oi_z_score": float("nan"), "funding_rate_pct": 0.2}):
            self.assertEqual(sqf.long_crowding_reasons(micro), [], micro)  # flag only: never fail closed

    def test_btc_rejects_resistance(self):
        self.assertTrue(sqf.btc_rejects_resistance({"btc_absorption": "BEARISH_ABSORPTION"}))
        self.assertTrue(sqf.btc_rejects_resistance({"btc_regime": "SHORT_BUILDUP"}))
        self.assertTrue(sqf.btc_rejects_resistance({"btc_regime": "LONG_UNWINDING"}))
        self.assertTrue(sqf.btc_rejects_resistance(_macro(absorption="BEARISH_ABSORPTION")))  # MacroContext
        for regime in ("LONG_BUILDUP", "SHORT_SQUEEZE", "NEUTRAL_CONSOLIDATION", "UNKNOWN"):
            self.assertFalse(sqf.btc_rejects_resistance({"btc_regime": regime, "btc_absorption": "NONE"}), regime)
        self.assertFalse(sqf.btc_rejects_resistance({"btc_absorption": "BULLISH_ABSORPTION"}))
        for bad in (None, {}, "BEARISH_ABSORPTION", 3, {"btc_regime": ["SHORT_BUILDUP"]}):
            self.assertFalse(sqf.btc_rejects_resistance(bad), bad)

    def test_alt_short_macro_reason(self):
        neutral = {"btc_regime": "NEUTRAL_CONSOLIDATION", "btc_absorption": "NONE", "allows_alt_shorts": True}
        self.assertIsNone(sqf.alt_short_macro_reason("BTCUSDT", False, None))  # BTC exempt
        self.assertIsNone(sqf.alt_short_macro_reason("BTCUSDT", False, dict(neutral, allows_alt_shorts=False)))
        for allows in (False, None, "true", 1):
            self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, dict(neutral, allows_alt_shorts=allows)),
                             "btc_short_squeeze")
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, None), "btc_short_squeeze")
        self.assertIsNone(sqf.alt_short_macro_reason("ETHUSDT", False, dict(neutral, btc_absorption="BEARISH_ABSORPTION")))
        self.assertIsNone(sqf.alt_short_macro_reason("ETHUSDT", True, neutral, 2.5))
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", False, neutral, 2.49),
                         "no_btc_rejection_and_climax<2.5x (vol_ratio=2.49)")
        # Only an exact True passes; the rounded display ratio never decides (a raw 2.45x shows as 2.5)
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", False, neutral, 2.5),
                         "no_btc_rejection_and_climax<2.5x (vol_ratio=2.50)")
        for flag in (None, 1, "true", 3.0):
            self.assertTrue(sqf.alt_short_macro_reason("ETHUSDT", flag, neutral).startswith("no_btc_rejection"), flag)
        self.assertTrue(sqf.alt_short_macro_reason("ETHUSDT", None, neutral).endswith("(vol_ratio=MISSING)"))


# =============================================================================
# 2. Radar enrichment
# =============================================================================
class TestRadarSqueezeCap(unittest.TestCase):

    def _enrich(self, direction, micro="default", confidence=70, **micro_kw):
        absorption = "BULLISH_ABSORPTION" if direction == "LONG" else "BEARISH_ABSORPTION"
        snap = (dict(tac.micro_snapshot("AAAUSDT"), absorption=absorption, absorption_desc="desc", **micro_kw)
                if micro == "default" else micro)
        cand = {"symbol": "AAAUSDT", "direction": direction, "confidence": confidence, "reasons": ["RSI"],
                "interval": "15m", "wick_candle_open_time": 1, "tier_s_eligible": True,
                "tier": "Tier S (radar)", "tier_code": "S", "score_components": {"rsi": confidence}}
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            return bmr.enrich_candidate_microstructure(cand)

    def assert_capped(self, cand, reason_prefix):
        self.assertEqual(cand["confidence"], 64)
        self.assertEqual(cand["tier_code"], "A")
        self.assertTrue(cand["tier"].startswith("Tier A ("), cand["tier"])
        self.assertTrue(cand["squeeze_risk"])
        self.assertTrue(cand["squeeze_reasons"][0].startswith(reason_prefix), cand["squeeze_reasons"])
        self.assertEqual(sum(cand["score_components"].values()), cand["confidence"])
        self.assertLess(cand["score_components"]["squeeze_cap"], 0)
        self.assertTrue(cand["reasons"][0].startswith("⚠️ Squeeze risk:"), cand["reasons"])
        self.assertIn("capped at Tier A", cand["reasons"][0])
        self.assertNotIn("volume", cand["reasons"][0].lower())

    def test_oi_spike_caps_short_at_tier_a(self):
        cand = self._enrich("SHORT", oi_z_score=2.91)   # 70 + 15 absorption would be Tier S 85
        self.assert_capped(cand, "oi_z>=2.0 (oi_z=2.91)")
        self.assertEqual(cand["score_components"]["squeeze_cap"], -21)

    def test_negative_funding_caps_short_at_tier_a(self):
        cand = self._enrich("SHORT", funding_rate_pct=-0.0211)  # CROWDED -15 still applies first: 70
        self.assert_capped(cand, "funding<=-0.01% (funding=-0.0211%)")
        self.assertEqual(cand["funding_rate_pct"], -0.0211)
        self.assertEqual(cand["score_components"]["funding"], -15)

    def test_missing_micro_caps_short(self):
        for micro in (None, {}):
            cand = self._enrich("SHORT", micro=micro, confidence=85)
            self.assert_capped(cand, "micro_unavailable")
            self.assertIsNone(cand["funding_rate_pct"])

    def test_missing_micro_field_caps_short(self):
        snap = dict(tac.micro_snapshot("AAAUSDT"), absorption="NONE", oi_z_score=float("nan"))
        cand = self._enrich("SHORT", micro=snap, confidence=80)
        self.assert_capped(cand, "micro_unavailable")

    def test_missing_micro_long_unchanged(self):
        cand = self._enrich("LONG", micro=None, confidence=85)
        self.assertEqual((cand["confidence"], cand["tier_code"]), (85, "S"))
        self.assertNotIn("squeeze_risk", cand)

    def test_low_score_short_only_flagged(self):
        snap = dict(tac.micro_snapshot("AAAUSDT"), oi_z_score=2.5)
        cand = self._enrich("SHORT", micro=snap, confidence=58)
        self.assertEqual((cand["confidence"], cand["tier_code"]), (58, "A"))
        self.assertTrue(cand["squeeze_risk"])
        self.assertNotIn("squeeze_cap", cand["score_components"])
        self.assertEqual(sum(cand["score_components"].values()), 58)

    def test_tier_s_cap_74_then_squeeze_cap(self):
        snap = dict(tac.micro_snapshot("AAAUSDT"), absorption="BEARISH_ABSORPTION", absorption_desc="d",
                    oi_z_score=3.0)
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            cand = bmr.enrich_candidate_microstructure(
                {"symbol": "AAAUSDT", "direction": "SHORT", "confidence": 70, "reasons": [], "interval": "15m",
                 "wick_candle_open_time": 1, "tier_s_eligible": False, "score_components": {"rsi": 70}})
        self.assertEqual(cand["confidence"], 64)
        self.assertEqual(cand["score_components"]["cap"], -11)        # 85 -> 74
        self.assertEqual(cand["score_components"]["squeeze_cap"], -10)  # 74 -> 64
        self.assertEqual(sum(cand["score_components"].values()), 64)

    def test_clean_short_unchanged(self):
        cand = self._enrich("SHORT")
        self.assertEqual((cand["confidence"], cand["tier_code"]), (85, "S"))
        self.assertFalse(cand["squeeze_risk"])
        self.assertEqual(cand["squeeze_reasons"], [])
        self.assertNotIn("squeeze_cap", cand["score_components"])
        self.assertEqual(cand["funding_rate_pct"], 0.01)

    def test_missing_micro_long_has_funding_none(self):
        cand = self._enrich("LONG", micro=None)
        self.assertIn("funding_rate_pct", cand)
        self.assertIsNone(cand["funding_rate_pct"])

    def test_crowded_long_flagged_score_unchanged(self):
        clean = self._enrich("LONG")
        crowded = self._enrich("LONG", oi_z_score=2.0, funding_rate_pct=0.05)
        self.assertTrue(crowded["long_crowding_risk"])
        self.assertEqual(len(crowded["long_crowding_reasons"]), 2)
        self.assertFalse(clean["long_crowding_risk"])
        # +0.05 also triggers the existing CROWDED -15 funding penalty (> 0.035); the flag adds nothing on top
        self.assertEqual(crowded["confidence"], clean["confidence"] - 15)
        self.assertNotIn("squeeze_cap", crowded["score_components"])
        self.assertNotIn("squeeze_risk", crowded)


def _flat_klines(vol_mult):
    """55 flat candles; the closed candle [-2] has `vol_mult` x the volume of the 20 before it."""
    return [[1_000_000 + i * 900_000, "100.0", "101.0", "99.0", "99.0" if i == 54 else "100.0",
             str(100.0 * (vol_mult if i == 53 else 1.0))] for i in range(55)]


class TestRadarClimaxFlag(unittest.TestCase):
    """alt_short_climax_ok comes from the unrounded vol_ratio, never from the one-decimal display value."""

    def scored(self, vol):
        with patch.object(bmr, "fetch_klines", return_value=_flat_klines(vol)), \
             patch.object(bmr, "calculate_rsi", return_value=30), \
             patch.object(bmr, "calculate_ema", return_value=[99.0]), \
             patch.object(bmr.me, "candle_wick_pcts", return_value=(55, 0.0)):
            return bmr.analyze_single_symbol("AAAUSDT")

    def test_flag_uses_unrounded_ratio(self):
        row = self.scored(2.45)
        self.assertEqual(row["vol_ratio"], 2.5)          # displayed rounded up
        self.assertIs(row["alt_short_climax_ok"], False)  # but 2.45 < 2.5
        self.assertIs(self.scored(2.5)["alt_short_climax_ok"], True)
        self.assertIs(self.scored(1.0)["alt_short_climax_ok"], False)


# =============================================================================
# 3. Screening pipeline
# =============================================================================
def _short_setup(symbol, vol_ratio, climax_ok=False):
    return _setup(symbol).model_copy(update={"direction": "SHORT", "vol_ratio": vol_ratio,
                                             "alt_short_climax_ok": climax_ok})


class TestPipelineMacroGate(_PipelineFakes):

    # CCCUSDT: a raw 2.45x shows as 2.5 but its exact flag is False, so it is dropped
    ROWS = [{"symbol": "AAAUSDT", "direction": "SHORT", "vol_ratio": 1.8, "alt_short_climax_ok": False},
            {"symbol": "BBBUSDT", "direction": "SHORT", "vol_ratio": 2.5, "alt_short_climax_ok": True},
            {"symbol": "CCCUSDT", "direction": "SHORT", "vol_ratio": 2.5, "alt_short_climax_ok": False},
            {"symbol": "BTCUSDT", "direction": "SHORT", "vol_ratio": 0.5},
            {"symbol": "SOLUSDT", "direction": "LONG", "vol_ratio": 0.5}]

    def run_gate(self, macro, rows=None):
        def enrich(c, env=None):  # the real enrich_and_size_candidate copies the flag with `is True`
            s = _setup(c["symbol"])
            return s.model_copy(update={"direction": c["direction"], "vol_ratio": c["vol_ratio"],
                                        "alt_short_climax_ok": c.get("alt_short_climax_ok") is True})
        for p in (patch("screening_pipeline.fetch_macro_btc", return_value=macro),
                  patch("broad_market_radar.scan_all_liquid_pairs", return_value=list(rows or self.ROWS)),
                  patch("screening_pipeline.enrich_and_size_candidate", side_effect=enrich)):
            p.start()
            self.addCleanup(p.stop)
        payload = self.run_pipeline(include_yolo=False)
        return {c.symbol: c for c in payload.top_candidates}, payload

    def test_alt_short_dropped_without_btc_rejection_or_climax(self):
        kept, payload = self.run_gate(_macro())
        self.assertNotIn("AAAUSDT", kept)
        self.assertNotIn("CCCUSDT", kept)
        # candidate order follows thread completion, so compare sorted
        self.assertEqual(sorted(payload.macro_rejected_shorts, key=lambda r: r["symbol"]),
                         [{"symbol": "AAAUSDT", "direction": "SHORT",
                           "reason": "no_btc_rejection_and_climax<2.5x (vol_ratio=1.80)"},
                          {"symbol": "CCCUSDT", "direction": "SHORT",
                           "reason": "no_btc_rejection_and_climax<2.5x (vol_ratio=2.50)"}])
        self.assertEqual(kept["BBBUSDT"].macro_short_check, "climax>=2.5x")
        self.assertIsNone(kept["BTCUSDT"].macro_short_check)  # exempt
        self.assertIsNone(kept["SOLUSDT"].macro_short_check)  # LONG untouched

    def test_btc_rejection_keeps_alt_shorts(self):
        for macro in (_macro(absorption="BEARISH_ABSORPTION"), _macro(regime="SHORT_BUILDUP")):
            kept, payload = self.run_gate(macro)
            self.assertEqual(set(kept), {"AAAUSDT", "BBBUSDT", "CCCUSDT", "BTCUSDT", "SOLUSDT"})
            self.assertEqual(payload.macro_rejected_shorts, [])
            self.assertEqual(kept["AAAUSDT"].macro_short_check, "btc_rejection")

    def test_alt_shorts_dropped_when_not_allowed(self):
        kept, payload = self.run_gate(_macro(absorption="BEARISH_ABSORPTION", allows=False))
        self.assertEqual(set(kept), {"BTCUSDT", "SOLUSDT"})
        # candidate order follows thread completion, so compare sorted
        self.assertEqual(sorted((r["symbol"], r["reason"]) for r in payload.macro_rejected_shorts),
                         [("AAAUSDT", "btc_short_squeeze"), ("BBBUSDT", "btc_short_squeeze"),
                          ("CCCUSDT", "btc_short_squeeze")])

    def test_unknown_btc_regime_needs_climax(self):
        kept, _ = self.run_gate(_macro(regime="UNKNOWN"))
        self.assertEqual(set(kept), {"BBBUSDT", "BTCUSDT", "SOLUSDT"})

    def test_rejected_list_bounded(self):
        rows = [{"symbol": f"S{i:02d}USDT", "direction": "SHORT", "vol_ratio": 1.0} for i in range(10)]
        rows += [{"symbol": "XUSDT", "direction": "SHORT", "vol_ratio": 1.0}]
        with patch.object(sp, "MACRO_REJECTED_SHORTS_MAX", 3):
            kept, payload = self.run_gate(_macro(), rows=rows)
        self.assertEqual(kept, {})
        self.assertEqual(len(payload.macro_rejected_shorts), 3)

    def test_gate_helper_bounded_at_ten(self):
        cands = [_short_setup(f"S{i:02d}USDT", 1.0) for i in range(12)]
        kept, rejected = sp.apply_alt_short_macro_gate(cands, _macro())
        self.assertEqual((kept, len(rejected)), ([], 10))


class TestFetchMacroFailClosed(unittest.TestCase):

    def test_exception_path_disallows_alt_shorts(self):
        with patch("microstructure_engine.get_symbol_microstructure", side_effect=RuntimeError("down")):
            macro = sp.fetch_macro_btc()
        self.assertFalse(macro.allows_alt_shorts)
        self.assertEqual(macro.btc_regime, "UNKNOWN")
        self.assertIn("BTC data unavailable", macro.macro_warning)
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, macro, 9.0), "btc_short_squeeze")

    def test_missing_btc_micro_keeps_flag_but_no_rejection(self):
        with patch("microstructure_engine.get_symbol_microstructure", return_value=None), \
             patch("microstructure_engine.get_live_aggtrades_tape", return_value=None), \
             patch("microstructure_engine.fetch_json", return_value={"price": "60000"}):
            macro = sp.fetch_macro_btc()
        self.assertEqual(macro.btc_regime, "UNKNOWN")
        self.assertTrue(macro.allows_alt_shorts)
        self.assertFalse(sqf.btc_rejects_resistance(macro))
        self.assertIsNotNone(sqf.alt_short_macro_reason("ETHUSDT", False, macro, 2.49))
        self.assertIsNone(sqf.alt_short_macro_reason("ETHUSDT", True, macro, 2.5))


class TestCandidateSetupFields(unittest.TestCase):

    def test_fields_filled_from_radar_row(self):
        row = {"symbol": "AAAUSDT", "direction": "SHORT", "price": 100.0, "sl": 103.0, "trigger": 99.0,
               "tp1": 95.0, "tp2": 88.0, "confidence": 64, "tier": "Tier A (x)", "tier_code": "A",
               "squeeze_risk": True, "squeeze_reasons": ["oi_z>=2.0 (oi_z=2.91)"],
               "micro": dict(tac.micro_snapshot("AAAUSDT"), oi_z_score=2.91, funding_rate_pct=-0.0211)}
        sizing = {"required_margin": 10.0, "step_qty": 1.0, "actual_notional": 30.0, "actual_dollar_risk": 1.9}
        with patch("quant_risk_engine.calculate_dynamic_equity_sizing", return_value=sizing), \
             patch("microstructure_engine.get_live_aggtrades_tape", return_value={}), \
             patch.object(sp, "_profile_standard_sizing", return_value=(3, 0.30)):
            setup = sp.enrich_and_size_candidate(row, "prod")
        self.assertEqual(setup.funding_rate_pct, -0.0211)
        self.assertTrue(setup.squeeze_risk)
        self.assertEqual(setup.squeeze_reasons, ["oi_z>=2.0 (oi_z=2.91)"])
        self.assertFalse(setup.long_crowding_risk)
        self.assertIsNone(setup.macro_short_check)
        self.assertIs(setup.alt_short_climax_ok, False)  # missing in the row -> False (fail closed)
        with patch("quant_risk_engine.calculate_dynamic_equity_sizing", return_value=sizing), \
             patch("microstructure_engine.get_live_aggtrades_tape", return_value={}), \
             patch.object(sp, "_profile_standard_sizing", return_value=(3, 0.30)):
            self.assertIs(sp.enrich_and_size_candidate(dict(row, alt_short_climax_ok=True), "prod")
                          .alt_short_climax_ok, True)
            self.assertIs(sp.enrich_and_size_candidate(dict(row, alt_short_climax_ok=1), "prod")
                          .alt_short_climax_ok, False)
        legacy = _setup("SOLUSDT")  # keyword construction without the new fields still works
        self.assertEqual((legacy.squeeze_risk, legacy.squeeze_reasons, legacy.funding_rate_pct,
                          legacy.alt_short_climax_ok), (False, [], None, False))


# =============================================================================
# 4. Brief
# =============================================================================
class TestBriefFlags(unittest.TestCase):

    def setUp(self):
        self.logs = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.logs, ignore_errors=True)

    def brief(self, screening, state=None):
        with patch.object(peb, "BRIEF_FILE", os.path.join(self.logs, "primed_brief.json")), \
             patch.object(peb, "ensure_fresh_state", return_value=state or {"target_env": "prod"}), \
             patch.object(peb, "get_latest_screening_payload", return_value=screening), \
             patch.object(peb, "load_recent_insights", return_value=[]), \
             patch.object(peb, "build_risk_profile", return_value={}):
            return peb.assemble_primed_brief(target_env="prod")

    @staticmethod
    def screening():
        short = {"symbol": "RLCUSDT", "direction": "SHORT", "tier": "Tier A (x)", "tier_code": "A",
                 "confidence": 64, "current_price": 1.0, "trigger_price": 0.99, "sl_price": 1.03,
                 "tp1_price": 0.95, "tp2_price": 0.88, "rr_ratio": 2.75,
                 "reasons": ["⚠️ Squeeze risk: oi_z>=2.0 (oi_z=2.91) → capped at Tier A", "RSI 81"],
                 "funding_rate_pct": -0.0211, "squeeze_risk": True, "squeeze_reasons": ["oi_z>=2.0 (oi_z=2.91)"],
                 "long_crowding_risk": False, "macro_short_check": "climax>=2.5x", "alt_short_climax_ok": True}
        long_ = dict(short, symbol="SOLUSDT", direction="LONG", reasons=["RSI 22"], squeeze_risk=False,
                     squeeze_reasons=[], long_crowding_risk=True, macro_short_check=None, funding_rate_pct=0.06)
        return {"top_candidates": [short, long_], "run_id": "x",
                "macro": {"btc_price": 60000.0, "allows_alt_shorts": True},
                "macro_rejected_shorts": [{"symbol": "FILUSDT", "direction": "SHORT", "reason": "btc_short_squeeze"},
                                          {"symbol": "UNIUSDT", "direction": "SHORT", "reason": "x"}]}

    def test_flags_forwarded_and_marked(self):
        brief = self.brief(self.screening())
        opps = {o["symbol"]: o for o in brief["filtered_opportunities"]}
        for key in ("funding_rate_pct", "squeeze_risk", "squeeze_reasons", "long_crowding_risk",
                    "macro_short_check"):
            self.assertIn(key, opps["RLCUSDT"], key)
        self.assertTrue(opps["RLCUSDT"]["squeeze_risk"])
        for opp in opps.values():  # pipeline-only gate input, never in the evaluator brief
            self.assertNotIn("alt_short_climax_ok", opp)
        self.assertEqual(opps["RLCUSDT"]["macro_short_check"], "climax>=2.5x")
        self.assertTrue(opps["SOLUSDT"]["long_crowding_risk"])
        self.assertEqual(brief["macro_rejected_shorts"], ["FILUSDT", "UNIUSDT"])
        md = peb.format_markdown_brief(brief)
        rlc = next(l for l in md.splitlines() if "**RLCUSDT**" in l)
        sol = next(l for l in md.splitlines() if "**SOLUSDT**" in l)
        self.assertIn("| SQZ; ⚠️ Squeeze risk:", rlc)
        self.assertIn("| LONG-CROWD; RSI 22", sol)
        self.assertNotIn("SQZ", sol)
        self.assertIn("**Macro-rejected alt SHORTs (2):** FILUSDT, UNIUSDT", md)
        self.assertNotIn("volume", md.split("Filtered Technical Setups")[1].split("YOLO Slot")[0].lower())
        with open(os.path.join(self.logs, "primed_brief_scores.json"), encoding="utf-8") as f:
            rows = {r["symbol"]: r for r in json.load(f)["rows"]}
        self.assertIs(rows["RLCUSDT"]["squeeze_risk"], True)
        self.assertNotIn("alt_short_climax_ok", rows["RLCUSDT"])  # not an audit field either
        self.assertIs(rows["SOLUSDT"]["squeeze_risk"], False)

    def test_no_rejected_shorts_no_key(self):
        screening = self.screening()
        screening["macro_rejected_shorts"] = []
        brief = self.brief(screening)
        self.assertNotIn("macro_rejected_shorts", brief)
        self.assertNotIn("Macro-rejected", peb.format_markdown_brief(brief))

    def test_default_macro_fails_closed(self):
        screening = self.screening()
        screening.pop("macro")
        brief = self.brief(screening)
        self.assertIs(brief["macro_btc"]["allows_alt_shorts"], False)


# =============================================================================
# 5. Evaluator prompt
# =============================================================================
class TestEvaluatorPrompt(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BASE_DIR, ".agents", "agents", "isolated_market_evaluator", "agent.md"),
                  encoding="utf-8") as f:
            cls.text = f.read()

    def test_rule_9_present(self):
        rules = self.text.split("<operational_rules>")[1].split("</operational_rules>")[0]
        rule9 = rules.split("- RULE 9")[1]
        self.assertIn("squeeze_risk", rule9)
        self.assertIn("requires_user_confirmation: true", rule9)
        self.assertIn("macro_rejected_shorts", rule9)
        self.assertLess(rules.index("- RULE 8"), rules.index("- RULE 9"))

    def test_squeeze_check_in_checklist(self):
        items = self.text.split("<checklist_items>")[1].split("</checklist_items>")[0]
        self.assertIn("K5", items)
        self.assertIn("squeeze_risk", items.split("K5")[1].split("\n")[0])

    def test_squeezed_short_few_shot(self):
        shot = re.search(r'<example id="eval_neg_08_squeeze_short_tier_a">([\s\S]*?)</example>', self.text)
        self.assertIsNotNone(shot)
        body = shot.group(1)
        self.assertIn("oi_z 2.91", body)
        self.assertIn("-0.0211%", body)
        self.assertIn("7.6x", body)
        dossier = json.loads(re.search(r"<dossier_json>([\s\S]*?)</dossier_json>", body).group(1))
        cand = dossier["approved_candidates"][0]
        self.assertEqual((cand["direction"], cand["tier"], cand["score"]), ("SHORT", "A", 64))
        self.assertIs(cand["requires_user_confirmation"], True)
        self.assertIn("C1.1 Portfolio delta_bias_incl_resting:", body)
        self.assertIn("Tier A, confidence 64 = score 64", body)

    def test_eval_pos_01_passes_macro_gate(self):
        shot = re.search(r'<example id="eval_pos_01_tier_s_approved">([\s\S]*?)</example>', self.text).group(1)
        scenario = shot.split("<scenario>")[1].split("</scenario>")[0]
        self.assertIn("BEARISH_ABSORPTION", scenario)
        self.assertIn("oi_z", scenario)
        self.assertNotIn("squeeze_risk true", scenario)


if __name__ == "__main__":
    unittest.main()
