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

import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
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

    def test_normalized_funding_wins(self):
        # 4h symbol: raw -0.006% is -0.012%/8h -> squeeze; the raw value alone would not trigger
        micro = {"oi_z_score": 0.2, "funding_rate_pct": -0.006, "funding_rate_8h_pct": -0.012}
        self.assertEqual(sqf.short_squeeze_reasons(micro), ["funding<=-0.01% (funding=-0.0120%)"])
        self.assertEqual(sqf.short_squeeze_reasons(dict(micro, funding_rate_8h_pct=-0.006)), [])
        self.assertEqual(sqf.short_squeeze_reasons(dict(micro, funding_rate_8h_pct=None)), ["micro_unavailable"])
        self.assertEqual(len(sqf.long_crowding_reasons({"oi_z_score": 2.0, "funding_rate_pct": 0.025,
                                                        "funding_rate_8h_pct": 0.05})), 2)

    def test_funding_interval_helpers(self):
        payload = [{"symbol": "AUSDT", "fundingIntervalHours": 4}, {"symbol": "BUSDT", "fundingIntervalHours": 1},
                   {"symbol": "CUSDT", "fundingIntervalHours": "4"}, {"symbol": "DUSDT", "fundingIntervalHours": 0},
                   {"symbol": "EUSDT", "fundingIntervalHours": True}, "junk", {"fundingIntervalHours": 4}]
        intervals = sqf.parse_funding_intervals(payload)
        self.assertEqual(intervals, {"AUSDT": 4, "BUSDT": 1})
        self.assertEqual(sqf.parse_funding_intervals({"code": -1}), {})
        self.assertEqual(sqf.funding_interval_h("AUSDT", intervals), 4)
        self.assertEqual(sqf.funding_interval_h("ZUSDT", intervals), 8)  # not listed: Binance default
        self.assertEqual(sqf.funding_interval_h("AUSDT", None), 8)
        self.assertAlmostEqual(sqf.normalize_funding_8h(-0.006, 4), -0.012)
        self.assertAlmostEqual(sqf.normalize_funding_8h(-0.006, 8), -0.006)
        self.assertAlmostEqual(sqf.normalize_funding_8h(0.01, 1), 0.08)
        self.assertIsNone(sqf.normalize_funding_8h(None, 4))

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
        squeeze = dict(neutral, btc_regime="SHORT_SQUEEZE")
        for allows in (False, None, "true", 1):
            self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, dict(squeeze, allows_alt_shorts=allows)),
                             "btc_short_squeeze")
            # not allowed for another cause: never labelled a squeeze
            self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, dict(neutral, allows_alt_shorts=allows)),
                             "alt_shorts_not_allowed")
        # unavailable BTC data: no macro, regime UNKNOWN, or the error-path marker (even if allows were True)
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, None), "btc_data_unavailable")
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, dict(neutral, btc_regime="UNKNOWN")),
                         "btc_data_unavailable")
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, dict(neutral, btc_data_ok=False)),
                         "btc_data_unavailable")
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, _macro(regime="UNKNOWN")), "btc_data_unavailable")
        self.assertIsNone(sqf.alt_short_macro_reason("ETHUSDT", False,
                                                     dict(neutral, btc_absorption="BEARISH_ABSORPTION")))
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

    def _enrich(self, direction, micro="default", confidence=70, intervals=None, **micro_kw):
        absorption = "BULLISH_ABSORPTION" if direction == "LONG" else "BEARISH_ABSORPTION"
        snap = (dict(tac.micro_snapshot("AAAUSDT"), absorption=absorption, absorption_desc="desc", **micro_kw)
                if micro == "default" else micro)
        cand = {"symbol": "AAAUSDT", "direction": direction, "confidence": confidence, "reasons": ["RSI"],
                "interval": "15m", "wick_candle_open_time": 1, "tier_s_eligible": True,
                "tier": "Tier S (radar)", "tier_code": "S", "score_components": {"rsi": confidence}}
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            return bmr.enrich_candidate_microstructure(cand, intervals)

    def test_four_hour_funding_normalized_to_8h(self):
        # 4h symbol at -0.006% raw = -0.012%/8h -> squeeze; the same raw rate on an 8h symbol is clean
        cand = self._enrich("SHORT", intervals={"AAAUSDT": 4}, funding_rate_pct=-0.006)
        self.assert_capped(cand, "funding<=-0.01% (funding=-0.0120%)")
        self.assertEqual((cand["funding_rate_pct"], cand["funding_rate_8h_pct"], cand["funding_interval_h"]),
                         (-0.006, -0.012, 4))
        self.assertNotIn("funding_rate_8h_pct", cand["micro"])  # the micro contract is unchanged
        clean = self._enrich("SHORT", intervals={"OTHERUSDT": 4}, funding_rate_pct=-0.006)
        self.assertFalse(clean["squeeze_risk"])
        self.assertEqual((clean["funding_rate_8h_pct"], clean["funding_interval_h"]), (-0.006, 8))
        self.assertEqual(clean["confidence"], 85)
        # crowding uses the normalized rate too: +0.025% per 4h = +0.05%/8h
        crowded = self._enrich("LONG", intervals={"AAAUSDT": 4}, oi_z_score=2.0, funding_rate_pct=0.025)
        self.assertTrue(crowded["long_crowding_risk"])

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
class TestScanFundingInfo(unittest.TestCase):
    """scan_all_liquid_pairs fetches /fapi/v1/fundingInfo once and falls back to 8h with a warning."""

    SYMBOLS = ("AAAUSDT", "DDDUSDT")  # (a symbol containing "BUSD" is filtered out of the universe)

    def scan(self, funding_info):
        info = {"symbols": [{"symbol": s, "underlyingType": "COIN", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                             "status": "TRADING"} for s in self.SYMBOLS]}
        tickers = [{"symbol": s, "quoteVolume": "1000000"} for s in self.SYMBOLS]
        calls = []

        def get_json(url, timeout):
            calls.append(url)
            if "exchangeInfo" in url:
                return info
            if "ticker/24hr" in url:
                return tickers
            if "fundingInfo" in url:
                if isinstance(funding_info, Exception):
                    raise funding_info
                return funding_info
            raise AssertionError(url)

        def analyze(sym, interval):
            return {"symbol": sym, "direction": "SHORT", "confidence": 70, "reasons": [], "interval": interval,
                    "wick_candle_open_time": 1, "tier_s_eligible": True, "score_components": {"rsi": 70}}

        def micro(sym, **kw):
            return dict(tac.micro_snapshot(sym), funding_rate_pct=-0.006)

        status = {}
        with patch.object(bmr, "_get_json", side_effect=get_json), \
             patch.object(bmr, "analyze_single_symbol", side_effect=analyze), \
             patch("microstructure_engine.get_symbol_microstructure", side_effect=micro), \
             patch("sys.stderr", new_callable=io.StringIO):
            rows = bmr.scan_all_liquid_pairs(top_n=10, interval="15m", funding_status=status)
        return {r["symbol"]: r for r in rows}, status, calls

    def test_listed_symbol_normalized_once_per_scan(self):
        rows, status, calls = self.scan([{"symbol": "AAAUSDT", "fundingIntervalHours": 4}])
        self.assertEqual(sum("fundingInfo" in u for u in calls), 1)
        self.assertTrue(rows["AAAUSDT"]["squeeze_risk"])      # -0.012%/8h
        self.assertEqual(rows["AAAUSDT"]["confidence"], 64)
        self.assertFalse(rows["DDDUSDT"]["squeeze_risk"])     # not listed: 8h, -0.006% stays clean
        self.assertEqual(status, {})

    def test_failure_falls_back_to_8h_with_warning(self):
        rows, status, _ = self.scan(urllib.error.URLError("down"))
        self.assertEqual({r["funding_interval_h"] for r in rows.values()}, {8})
        # Issue #207 (PR #214 review): with the interval unknown, a SHORT at a negative raw rate (-0.006%) may be
        # crowded on a shorter interval: flagged funding_interval_unknown and capped as squeeze risk
        self.assertTrue(rows["AAAUSDT"]["squeeze_risk"])
        self.assertIn("funding_interval_unknown", rows["AAAUSDT"]["squeeze_reasons"])
        self.assertIs(rows["AAAUSDT"]["funding_interval_unknown"], True)
        self.assertIn("fundingInfo unavailable", status["warning"])

    def test_rate_limit_ban_propagates(self):
        from utils import rate_limit_guard
        with self.assertRaises(rate_limit_guard.RateLimitedError):
            self.scan(rate_limit_guard.RateLimitedError("banned"))

    def test_http_429_on_funding_info_trips_the_guard(self):
        # the real _get_json: a 429 trips the enabled guard and raises, so the run reports UNAVAILABLE as usual
        from utils import rate_limit_guard
        rate_limit_guard.reset_for_tests()
        self.addCleanup(rate_limit_guard.reset_for_tests)
        rate_limit_guard.enable()

        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "rate limit", {"Retry-After": "20"}, None)

        with patch("urllib.request.urlopen", side_effect=urlopen):
            with self.assertRaises(rate_limit_guard.RateLimitedError):
                bmr.fetch_funding_intervals()
        self.assertTrue(rate_limit_guard.is_banned())


def _short_row(symbol, vol_ratio=1.0, climax_ok=False):
    return {"symbol": symbol, "direction": "SHORT", "vol_ratio": vol_ratio, "alt_short_climax_ok": climax_ok}


class TestPipelineMacroGate(_PipelineFakes):

    # CCCUSDT: a raw 2.45x shows as 2.5 but its exact flag is False, so it is dropped
    ROWS = [{"symbol": "AAAUSDT", "direction": "SHORT", "vol_ratio": 1.8, "alt_short_climax_ok": False},
            {"symbol": "BBBUSDT", "direction": "SHORT", "vol_ratio": 2.5, "alt_short_climax_ok": True},
            {"symbol": "CCCUSDT", "direction": "SHORT", "vol_ratio": 2.5, "alt_short_climax_ok": False},
            {"symbol": "BTCUSDT", "direction": "SHORT", "vol_ratio": 0.5},
            {"symbol": "SOLUSDT", "direction": "LONG", "vol_ratio": 0.5}]

    def run_gate(self, macro, rows=None):
        self.enriched = []

        def enrich(c, env=None):  # like enrich_and_size_candidate: copies the flag and the gate's check
            self.enriched.append(c["symbol"])
            s = _setup(c["symbol"])
            return s.model_copy(update={"direction": c["direction"], "vol_ratio": c["vol_ratio"],
                                        "alt_short_climax_ok": c.get("alt_short_climax_ok") is True,
                                        "macro_short_check": c.get("macro_short_check")})
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
        for regime, reason in (("SHORT_SQUEEZE", "btc_short_squeeze"),
                               ("NEUTRAL_CONSOLIDATION", "alt_shorts_not_allowed")):
            kept, payload = self.run_gate(_macro(regime=regime, absorption="BEARISH_ABSORPTION", allows=False))
            self.assertEqual(set(kept), {"BTCUSDT", "SOLUSDT"})
            self.assertEqual(sorted((r["symbol"], r["reason"]) for r in payload.macro_rejected_shorts),
                             [("AAAUSDT", reason), ("BBBUSDT", reason), ("CCCUSDT", reason)])

    def test_unknown_btc_regime_drops_alt_shorts(self):
        # Round 4: unavailable BTC data blocks every alt SHORT, even one with a climax (fail closed)
        for macro in (_macro(regime="UNKNOWN"), _macro().model_copy(update={"btc_data_ok": False})):
            kept, payload = self.run_gate(macro)
            self.assertEqual(set(kept), {"BTCUSDT", "SOLUSDT"})
            self.assertEqual({r["reason"] for r in payload.macro_rejected_shorts}, {"btc_data_unavailable"})

    def test_rejected_list_bounded(self):
        rows = [_short_row(f"S{i:02d}USDT") for i in range(11)]
        with patch.object(sp, "MACRO_REJECTED_SHORTS_MAX", 3):
            kept, payload = self.run_gate(_macro(), rows=rows)
        self.assertEqual(kept, {})
        self.assertEqual(len(payload.macro_rejected_shorts), 3)

    def test_gate_runs_before_the_enrichment_slice(self):
        # Round 4: two dropped alt SHORTs ranked first no longer use enrichment slots; 10 LONGs fill them
        rows = [_short_row("S1USDT"), _short_row("S2USDT")]
        rows += [{"symbol": f"L{i:02d}USDT", "direction": "LONG", "vol_ratio": 1.0} for i in range(11)]
        self.run_gate(_macro(), rows=rows)
        self.assertEqual(sorted(self.enriched), [f"L{i:02d}USDT" for i in range(10)])

    def test_funding_info_warning_reaches_the_payload(self):
        def radar(*a, funding_status=None, **k):
            funding_status["warning"] = "fundingInfo unavailable (URLError): funding read as 8h for every symbol"
            return list(self.ROWS)
        _, payload = self.run_gate(_macro())
        self.assertIsNone(payload.funding_info_warning)
        with patch("broad_market_radar.scan_all_liquid_pairs", side_effect=radar):
            payload = self.run_pipeline(include_yolo=False)
        self.assertIn("fundingInfo unavailable", payload.funding_info_warning)
        self.assertTrue(payload.top_candidates)  # the scan is not blocked

    def test_gate_helper_is_pure_and_bounded_at_ten(self):
        rows = [_short_row(f"S{i:02d}USDT") for i in range(12)] + [_short_row("OKUSDT", 3.0, True), "junk"]
        kept, rejected = sp.apply_alt_short_macro_gate(rows, _macro())
        self.assertEqual(([r["symbol"] for r in kept], len(rejected)), (["OKUSDT"], 10))
        self.assertEqual(kept[0]["macro_short_check"], "climax>=2.5x")
        self.assertNotIn("macro_short_check", rows[0])  # dropped rows are not touched


class TestFetchMacroFailClosed(unittest.TestCase):

    def test_exception_path_disallows_alt_shorts(self):
        with patch("microstructure_engine.get_symbol_microstructure", side_effect=RuntimeError("down")):
            macro = sp.fetch_macro_btc()
        self.assertFalse(macro.allows_alt_shorts)
        self.assertEqual(macro.btc_regime, "UNKNOWN")
        self.assertIn("BTC data unavailable", macro.macro_warning)
        self.assertIs(macro.btc_data_ok, False)
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, macro, 9.0), "btc_data_unavailable")

    def test_empty_btc_micro_blocks_alt_shorts(self):
        # Round 4: empty micro data (regime UNKNOWN) now blocks alt shorts like the exception path
        for empty in (None, {}):
            with patch("microstructure_engine.get_symbol_microstructure", return_value=empty), \
                 patch("microstructure_engine.get_live_aggtrades_tape", return_value=None), \
                 patch("microstructure_engine.fetch_json", return_value={"price": "60000"}):
                macro = sp.fetch_macro_btc()
            self.assertEqual(macro.btc_regime, "UNKNOWN")
            self.assertFalse(macro.allows_alt_shorts)
            self.assertIs(macro.btc_data_ok, False)
            self.assertIn("BTC data unavailable", macro.macro_warning)
            self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, macro, 2.5), "btc_data_unavailable")

    def test_live_regimes(self):
        for regime, allows in (("SHORT_SQUEEZE", False), ("NEUTRAL_CONSOLIDATION", True), ("SHORT_BUILDUP", True)):
            with patch("microstructure_engine.get_symbol_microstructure", return_value={"regime": regime}), \
                 patch("microstructure_engine.get_live_aggtrades_tape", return_value=None), \
                 patch("microstructure_engine.fetch_json", return_value={"price": "60000"}):
                macro = sp.fetch_macro_btc()
            self.assertEqual((macro.allows_alt_shorts, macro.btc_data_ok), (allows, True), regime)
        self.assertEqual(sqf.alt_short_macro_reason("ETHUSDT", True, macro.model_copy(
            update={"btc_regime": "SHORT_SQUEEZE", "allows_alt_shorts": False})), "btc_short_squeeze")

    def test_rate_limited_macro_marks_btc_data_unavailable(self):
        self.assertIs(sp._rate_limited_macro("x").btc_data_ok, False)


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
            four_h = sp.enrich_and_size_candidate(dict(row, funding_rate_8h_pct=-0.0422, funding_interval_h=4,
                                                       macro_short_check="climax>=2.5x"), "prod")
        self.assertEqual((four_h.funding_rate_8h_pct, four_h.funding_interval_h, four_h.macro_short_check),
                         (-0.0422, 4, "climax>=2.5x"))
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
        short.update(funding_rate_8h_pct=-0.0211, funding_interval_h=8)
        long_.update(funding_rate_8h_pct=0.12, funding_interval_h=4)
        return {"top_candidates": [short, long_], "run_id": "x",
                "macro": {"btc_price": 60000.0, "allows_alt_shorts": True},
                "macro_rejected_shorts": [{"symbol": "FILUSDT", "direction": "SHORT", "reason": "btc_short_squeeze"},
                                          {"symbol": "UNIUSDT", "direction": "SHORT", "reason": "x"}]}

    def test_flags_forwarded_and_marked(self):
        brief = self.brief(self.screening())
        opps = {o["symbol"]: o for o in brief["filtered_opportunities"]}
        for key in ("funding_rate_pct", "squeeze_risk", "squeeze_reasons", "macro_short_check"):
            self.assertIn(key, opps["RLCUSDT"], key)
        self.assertTrue(opps["RLCUSDT"]["squeeze_risk"])
        for opp in opps.values():  # pipeline-only gate input, never in the evaluator brief
            self.assertNotIn("alt_short_climax_ok", opp)
        self.assertEqual(opps["RLCUSDT"]["macro_short_check"], "climax>=2.5x")
        self.assertTrue(opps["SOLUSDT"]["long_crowding_risk"])
        # Round 4: unset values are dropped (token budget); set values stay
        self.assertNotIn("long_crowding_risk", opps["RLCUSDT"])
        for key in ("squeeze_risk", "squeeze_reasons", "macro_short_check"):
            self.assertNotIn(key, opps["SOLUSDT"], key)
        # 8h-normalized funding only for a non-8h symbol
        self.assertNotIn("funding_rate_8h_pct", opps["RLCUSDT"])
        self.assertNotIn("funding_interval_h", opps["RLCUSDT"])
        self.assertEqual((opps["SOLUSDT"]["funding_rate_8h_pct"], opps["SOLUSDT"]["funding_interval_h"]), (0.12, 4))
        self.assertEqual(brief["macro_rejected_shorts"], ["FILUSDT", "UNIUSDT"])
        md = peb.format_markdown_brief(brief)
        rlc = next(l for l in md.splitlines() if "**RLCUSDT**" in l)
        sol = next(l for l in md.splitlines() if "**SOLUSDT**" in l)
        # one representation of the squeeze in the table: the SQZ marker, not the radar reason line too
        self.assertIn("| SQZ; RSI 81 |", rlc)
        self.assertNotIn("Squeeze risk", rlc)
        self.assertIn("| LONG-CROWD; RSI 22", sol)
        self.assertNotIn("SQZ", sol)
        self.assertIn("**Macro-rejected alt SHORTs (2):** FILUSDT, UNIUSDT", md)
        self.assertNotIn("volume", md.split("Filtered Technical Setups")[1].split("YOLO Slot")[0].lower())
        with open(os.path.join(self.logs, "primed_brief_scores.json"), encoding="utf-8") as f:
            rows = {r["symbol"]: r for r in json.load(f)["rows"]}
        self.assertIs(rows["RLCUSDT"]["squeeze_risk"], True)
        self.assertNotIn("alt_short_climax_ok", rows["RLCUSDT"])  # not an audit field either
        self.assertIs(rows["SOLUSDT"]["squeeze_risk"], False)

    def test_unset_values_dropped_but_falsy_lookalikes_kept(self):
        row = {"symbol": "X", "squeeze_risk": False, "squeeze_reasons": [], "long_crowding_risk": False,
               "macro_short_check": None, "funding_rate_pct": None, "confidence": 60}
        self.assertEqual(peb._brief_opportunity(row), {"symbol": "X", "confidence": 60})
        kept = dict(row, funding_rate_pct=0.0, squeeze_risk=True)  # a real 0.0 funding is a value
        self.assertEqual(peb._brief_opportunity(kept),
                         {"symbol": "X", "confidence": 60, "funding_rate_pct": 0.0, "squeeze_risk": True})

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
        k5 = items.split("- K5")[1].split("\n")[0]
        self.assertIn("squeeze_risk", k5)
        # Round 4: K5 is informational (caps, never rejects) and LONGs carry no K5 line
        self.assertIn("LONGs have no K5 line", k5)
        self.assertIn("Always `[x]`", k5)
        semantics = next(l for l in self.text.splitlines() if l.startswith("- Checkbox semantics"))
        self.assertIn("C2.2, K5, C4.1) are ALWAYS `[x]`", semantics)
        self.assertNotIn("K1-K5", semantics)
        self.assertNotIn("K1-K3 and K5 lines", self.text)
        self.assertEqual(self.text.count("(K5 CAPPED limits the tier to A)"), 2)
        for line in re.findall(r"- \[.\] \S+ \S+ K5 .*", self.text):
            self.assertTrue(line.startswith("- [x]"), line)
            self.assertIn(" SHORT K5 ", line)

    def test_one_list_of_squeeze_reasons(self):
        rule9 = self.text.split("- RULE 9")[1].split("</operational_rules>")[0]
        self.assertIn("oi_z >= 2.0, funding <= -0.01%/8h, or micro data missing", rule9)
        with open(os.path.join(BASE_DIR, "AGENTS.md"), encoding="utf-8") as f:
            agents = f.read()
        self.assertIn("OI z ≥ 2, funding ≤ -0.01%/8h or no micro data", agents)
        with open(os.path.join(BASE_DIR, ".agents", "skills", "market-radar", "SKILL.md"), encoding="utf-8") as f:
            skill = f.read()
        self.assertIn("-0.01% per 8h", skill)
        self.assertIn("or without micro data", skill)

    def test_approved_shots_carry_basket_and_verdict_sections(self):
        finals = re.findall(r"<final_response>([\s\S]*?)</final_response>", self.text)
        approved = 0
        for final in finals:
            dossier = json.loads(re.search(r"<dossier_json>([\s\S]*?)</dossier_json>", final).group(1))
            if dossier["status"] != "APPROVED":
                continue
            approved += 1
            head = final.split("<dossier_json>")[0]
            self.assertIn("## 2. Approved Quantitative Basket", head, dossier["approved_symbols"])
            self.assertIn("## 6. Execution Verdict", head, dossier["approved_symbols"])
            self.assertLess(head.index("## 2. Approved Quantitative Basket"), head.index("## 6. Execution Verdict"))
        self.assertEqual(approved, 4)

    def test_squeezed_short_few_shot(self):
        self.assertNotIn("eval_neg_08_squeeze_short_tier_a", self.text)
        self.assertIn("<!-- EXAMPLE 12: POSITIVE - SQUEEZED SHORT CAPPED", self.text)
        shot = re.search(r'<example id="eval_pos_04_squeeze_short_capped">([\s\S]*?)</example>', self.text)
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
        # macro_short_check is per-candidate evidence: on the K1 line, not on the BTC-level C2.1 line
        c21 = next(l for l in body.splitlines() if "C2.1" in l)
        k1 = next(l for l in body.splitlines() if "RLCUSDT SHORT K1" in l)
        self.assertNotIn("macro_short_check", c21)
        self.assertIn("macro_short_check climax>=2.5x", k1)
        self.assertIn("Pending User Confirmation", body.split("## 6. Execution Verdict")[1])

    def test_eval_pos_01_passes_macro_gate(self):
        shot = re.search(r'<example id="eval_pos_01_tier_s_approved">([\s\S]*?)</example>', self.text).group(1)
        scenario = shot.split("<scenario>")[1].split("</scenario>")[0]
        self.assertIn("BEARISH_ABSORPTION", scenario)
        self.assertIn("oi_z", scenario)
        self.assertNotIn("squeeze_risk true", scenario)


if __name__ == "__main__":
    unittest.main()
