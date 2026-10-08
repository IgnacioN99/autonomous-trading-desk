#!/usr/bin/env python3
"""
test_issue_133_radar_levels.py - Issues #133, #134, #135, #140 and #141 (radar side).

- #133: intraday_radar puts the SL beyond the closed wick, the trigger beyond the more extreme of the closed wick
  candle and the forming candle, and measures every level from the trigger; both radars check the closed candle's
  range in the zero-range guard.
- #134: the microstructure enrichment never lifts a row without institutional volume (or a >= 60% wick) to Tier S.
- #135: a missing taker_candle_matched is unmatched, the regime text labels each value's period, the unscored
  absorption fallthrough never beats the matched case, the brief tier helper, abs:unscored, the doctor dependency
  check.
- #140: signed trigger_distance_pct; rows with the SL or TP2 on the wrong side of the trigger are dropped (stderr).
- #141: over-ceiling rows are counted on stderr; failed rows are logged; the text report spacing.

All market data is mocked; no network, no orders, no writes to logs/.
"""

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broad_market_radar as bmr  # noqa: E402
import intraday_radar as ir  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import screening_pipeline as sp  # noqa: E402
import trading_doctor  # noqa: E402
import test_analytics_cli as tac  # noqa: E402  (fixtures only)
from test_issue_20_wick_consistency import mixed_wick_klines, upper_wick_short_klines  # noqa: E402
from test_issue_86_entry_anchored_levels import radar_short_klines  # noqa: E402


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def _intraday(klines):
    with patch.object(ir, "get_klines", return_value=klines):
        return ir.analyze_symbol("AAAUSDT")


def _broad(klines):
    with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen([("/fapi/v1/klines", lambda url: klines)])):
        return bmr.analyze_single_symbol("AAAUSDT", interval="15m")


def _col(klines, j):
    return [float(k[j]) for k in klines]


def _atr(klines):
    return ir.calculate_atr(_col(klines, 2), _col(klines, 3), _col(klines, 4), period=14)


def _ema20(klines):
    return ir.calculate_ema(_col(klines, 4), period=20)[-1]


# =============================================================================
# #133 intraday_radar levels
# =============================================================================
class TestIntradayLevels(unittest.TestCase):

    def _assert_anchored_at_trigger(self, res, klines, sign):
        trigger, sl = res["trigger"], res["sl"]
        self.assertEqual(res["entry"], trigger)
        self.assertEqual(res["price"], float(klines[-1][4]))
        risk_pct = abs(trigger - sl) / trigger * 100
        self.assertGreaterEqual(risk_pct, 1.0)   # no floor override in these fixtures
        self.assertEqual(res["risk_pct"], round(risk_pct, 2))
        pick = max if sign > 0 else min
        self.assertAlmostEqual(res["tp1"], pick(_ema20(klines), trigger * (1 + sign * risk_pct * 1.8 / 100)))
        self.assertAlmostEqual(res["tp2"], trigger * (1 + sign * risk_pct * 4.0 / 100))
        self.assertEqual(res["rr"], round(abs(res["tp2"] - trigger) / abs(trigger - sl), 2))
        # The trigger is never crossed at scan time
        if sign > 0:
            self.assertGreater(trigger, res["price"])
        else:
            self.assertLess(trigger, res["price"])

    def test_long_sl_beyond_closed_wick_trigger_from_forming_high(self):
        klines = mixed_wick_klines()
        wick_low, wick_high = float(klines[-2][3]), float(klines[-2][2])
        forming_high = float(klines[-1][2])
        self.assertGreater(forming_high, wick_high)   # the forming candle holds the high in this fixture
        res = _intraday(klines)
        self.assertEqual(res["direction"], "LONG")
        self.assertLess(res["sl"], wick_low)
        self.assertAlmostEqual(res["sl"], wick_low - 1.3 * _atr(klines))
        self.assertAlmostEqual(res["trigger"], forming_high * 1.0005)
        self._assert_anchored_at_trigger(res, klines, +1)

    def test_long_trigger_from_wick_high_when_above_forming(self):
        klines = mixed_wick_klines()
        o = float(klines[-1][1])
        klines[-1][2] = repr(o * 1.002)               # forming high now below the wick high
        wick_high = float(klines[-2][2])
        self.assertLess(float(klines[-1][2]), wick_high)
        res = _intraday(klines)
        self.assertEqual(res["direction"], "LONG")
        self.assertAlmostEqual(res["trigger"], wick_high * 1.0005)
        self._assert_anchored_at_trigger(res, klines, +1)

    def test_short_sl_beyond_closed_wick_trigger_from_wick_low(self):
        klines = upper_wick_short_klines()
        wick_low, wick_high = float(klines[-2][3]), float(klines[-2][2])
        self.assertLess(wick_low, float(klines[-1][3]))   # the closed wick candle holds the low
        res = _intraday(klines)
        self.assertEqual(res["direction"], "SHORT")
        self.assertGreater(res["sl"], wick_high)
        self.assertAlmostEqual(res["sl"], wick_high + 1.3 * _atr(klines))
        self.assertAlmostEqual(res["trigger"], wick_low * 0.9995)
        self._assert_anchored_at_trigger(res, klines, -1)

    def test_forming_extreme_used_when_beyond_the_wick(self):
        long_k = mixed_wick_klines()
        long_k[-1][3] = repr(float(long_k[-2][3]) * 0.97)    # forming candle undercuts the wick low
        res = _intraday(long_k)
        self.assertEqual(res["direction"], "LONG")
        self.assertAlmostEqual(res["sl"], float(long_k[-1][3]) - 1.3 * _atr(long_k))
        self._assert_anchored_at_trigger(res, long_k, +1)

        short_k = upper_wick_short_klines()
        short_k[-1][2] = repr(float(short_k[-2][2]) * 1.03)  # forming candle overshoots the wick high
        res = _intraday(short_k)
        self.assertEqual(res["direction"], "SHORT")
        self.assertAlmostEqual(res["sl"], float(short_k[-1][2]) + 1.3 * _atr(short_k))
        self._assert_anchored_at_trigger(res, short_k, -1)


class TestZeroRangeGuardOnClosedCandle(unittest.TestCase):
    """Both radars return None when the CLOSED wick candle has no range, and keep a valid closed-candle setup when
    only the forming candle is flat."""

    @staticmethod
    def _flat(kline):
        p = kline[1]
        kline[2] = kline[3] = kline[4] = p

    def test_flat_closed_candle_returns_none(self):
        for klines in (mixed_wick_klines(), upper_wick_short_klines()):
            self._flat(klines[-2])
            self.assertIsNone(_intraday([list(k) for k in klines]))
            self.assertIsNone(_broad([list(k) for k in klines]))

    def test_flat_forming_candle_keeps_the_setup(self):
        for klines, direction in ((mixed_wick_klines(), "LONG"), (upper_wick_short_klines(), "SHORT")):
            self._flat(klines[-1])
            res = _intraday([list(k) for k in klines])
            self.assertIsNotNone(res, direction)
            self.assertEqual(res["direction"], direction)
            cand = _broad([list(k) for k in klines])
            self.assertIsNotNone(cand, direction)
            self.assertEqual(cand["direction"], direction)


# =============================================================================
# #134 no Tier S lift without institutional volume
# =============================================================================
class TestTierSEligibility(unittest.TestCase):

    def _enrich(self, direction, confidence=70, **flags):
        absorption = "BULLISH_ABSORPTION" if direction == "LONG" else "BEARISH_ABSORPTION"
        snap = dict(tac.micro_snapshot("AAAUSDT"), absorption=absorption, absorption_desc="desc")
        cand = dict({"symbol": "AAAUSDT", "direction": direction, "confidence": confidence, "reasons": [],
                     "interval": "15m", "wick_candle_open_time": 1}, **flags)
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            return bmr.enrich_candidate_microstructure(cand)

    def test_ineligible_row_capped_at_74(self):
        for direction in ("LONG", "SHORT"):
            cand = self._enrich(direction, tier_s_eligible=False)
            self.assertEqual(cand["confidence"], 74, direction)   # 70 + 15 would be 85
            self.assertEqual(cand["tier_code"], "A+")
            self.assertTrue(cand["tier"].startswith("Tier A+"))

    def test_missing_flag_is_not_eligible(self):
        for direction in ("LONG", "SHORT"):
            cand = self._enrich(direction)
            self.assertEqual(cand["confidence"], 74, direction)
            self.assertEqual(cand["tier_code"], "A+")
            cand = self._enrich(direction, tier_s_eligible=1)    # truthy but not exactly True
            self.assertEqual(cand["confidence"], 74, direction)

    def test_eligible_row_reaches_tier_s(self):
        for direction in ("LONG", "SHORT"):
            cand = self._enrich(direction, tier_s_eligible=True)
            self.assertEqual(cand["confidence"], 85, direction)
            self.assertEqual(cand["tier_code"], "S")

    def test_below_80_unchanged_when_ineligible(self):
        cand = self._enrich("LONG", confidence=60, tier_s_eligible=False)
        self.assertEqual(cand["confidence"], 75)

    def test_radar_row_carries_the_exact_flag(self):
        self.assertIs(_broad(mixed_wick_klines())["tier_s_eligible"], True)     # 3.0x volume
        klines = mixed_wick_klines()
        e = float(klines[-2][1])
        # Quiet volume (1.0x) and a 50% lower wick: below both Tier S thresholds
        klines[-2] = [klines[-2][0], repr(e), repr(e * 1.02), repr(e * 0.98), repr(e * 1.004), "100"]
        cand = _broad(klines)
        self.assertEqual(cand["direction"], "LONG")
        self.assertEqual(cand["vol_ratio"], 1.0)
        self.assertLess(cand["lower_wick"], 60)
        self.assertIs(cand["tier_s_eligible"], False)
        self.assertLess(cand["confidence"], 80)


# =============================================================================
# #135 polish
# =============================================================================
class TestAbsorptionScoredFlag(unittest.TestCase):

    NOTE = "🔬 ORDER FLOW: absorption not scored (wick/taker candle mismatch)"

    def _enrich(self, direction, snap_over, drop=()):
        snap = dict(tac.micro_snapshot("AAAUSDT"), **snap_over)
        for k in drop:
            snap.pop(k, None)
        cand = {"symbol": "AAAUSDT", "direction": direction, "confidence": 60, "reasons": [], "interval": "15m",
                "tier_s_eligible": True}
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            return bmr.enrich_candidate_microstructure(cand)

    def test_missing_taker_candle_matched_is_unmatched(self):
        for direction, absorption in (("LONG", "BULLISH_ABSORPTION"), ("SHORT", "BEARISH_ABSORPTION")):
            cand = self._enrich(direction, {"absorption": absorption, "absorption_desc": "d"},
                                drop=("taker_candle_matched",))
            self.assertEqual(cand["confidence"], 60, direction)
            self.assertIn(self.NOTE, cand["reasons"])
            self.assertIs(cand["absorption_scored"], False)
            matched = self._enrich(direction, {"absorption": absorption, "absorption_desc": "d"})
            self.assertEqual(matched["confidence"], 75, direction)
            self.assertIs(matched["absorption_scored"], True)

    def test_unscored_fallthrough_never_beats_matched_absorption(self):
        cases = (("LONG", "BULLISH_ABSORPTION", "LONG_BUILDUP"), ("SHORT", "BEARISH_ABSORPTION", "SHORT_SQUEEZE"))
        for direction, absorption, regime in cases:
            common = {"absorption": absorption, "absorption_desc": "d", "regime": regime, "regime_desc": "r"}
            matched = self._enrich(direction, common)
            unscored = self._enrich(direction, dict(common, taker_candle_matched=False))
            self.assertEqual(unscored["confidence"], 70, direction)   # regime +10 only
            self.assertLessEqual(unscored["confidence"], matched["confidence"], direction)

    def test_macro_penalty_labels_each_period(self):
        for direction, regime in (("LONG", "SHORT_BUILDUP"), ("SHORT", "LONG_BUILDUP")):
            cand = self._enrich(direction, {"regime": regime, "price_change_pct": -0.42, "oi_change_pct": 0.55,
                                            "taker_ratio": 0.8})
            penalty = next(r for r in cand["reasons"] if r.startswith("⚠️ MACRO PENALTY"))
            for text in ("ΔP(forming)=-0.42%", "OI(latest)=+0.55%", "Taker(closed)=0.80"):
                self.assertIn(text, penalty)


class TestRegimeDescLabels(unittest.TestCase):

    def test_regime_desc_labels_forming_price_and_latest_oi(self):
        from test_issue_83_closed_candle_confluence import micro, taker_rows, SELLING
        klines = mixed_wick_klines()[-30:]
        m = micro(klines, taker_rows(klines, SELLING))
        self.assertIn("OI(latest)", m["regime_desc"])


class TestBriefTierHelperAndUnscored(unittest.TestCase):

    def test_tier_label(self):
        self.assertEqual(peb._TIER_CODES, ("S", "A+", "A", "B+"))
        self.assertEqual(peb._tier_label({"tier_code": "A+", "tier": "Tier S (x)"}), "A+")
        self.assertEqual(peb._tier_label({"tier_code": "DISQUALIFIED", "tier": "Tier A (x)"}), "A")
        self.assertEqual(peb._tier_label({"tier": "Tier B+ (Moderate Opportunity)"}), "B+")
        self.assertEqual(peb._tier_label({"tier": "Disqualified (<55%)"}), "?")
        self.assertEqual(peb._tier_label({}), "?")

    def _rows(self, opps):
        brief = {
            "timestamp_utc": "2026-10-08 00:00:00 UTC", "target_env": "prod", "generated_at_ts": 0,
            "macro_btc": {"btc_price": 60000.0},
            "ground_truth_portfolio": {
                "delta_bias": "NEUTRAL", "active_positions_count": 0, "positions_summary": [],
                "net_delta_usdt": 0.0, "long_notional_usdt": 0.0, "short_notional_usdt": 0.0,
                "realized_pnl_today": 0.0, "floating_pnl_usdt": 0.0, "tactical_rule": "-",
            },
            "filtered_opportunities": opps,
        }
        return [ln for ln in peb.format_markdown_brief(brief).splitlines() if ln.startswith("| **")]

    def test_abs_unscored_shown_only_when_false(self):
        base = {"direction": "LONG", "confidence": 70, "tier_code": "A+", "reasons": ["r1", "r2", "r3"]}
        rows = self._rows([dict(base, symbol="U", absorption_scored=False),
                           dict(base, symbol="M", absorption_scored=True), dict(base, symbol="L")])
        self.assertIn("abs:unscored; r1; r2 |", rows[0])
        self.assertNotIn("abs:unscored", rows[1])
        self.assertNotIn("abs:unscored", rows[2])
        self.assertTrue(all(r.split("|")[3].strip() == "A+" for r in rows))


class TestCandidateSetupKeepsRadarFlags(unittest.TestCase):

    ROW = {"symbol": "BEAMXUSDT", "direction": "LONG", "price": 0.002509, "trigger": 0.00252326,
           "sl": 0.0024084, "tp1": 0.0027, "tp2": 0.0029, "tier": "Tier A+ (High Confirmed Conviction)"}

    def _enrich(self, row):
        err = io.StringIO()
        with patch("quant_risk_engine.get_account_equity", return_value=386.0), \
             patch("execute_futures_trade.get_symbol_filters",
                   return_value={"stepSize": 1.0, "minQty": 1.0, "tickSize": 0.00000001, "precision_qty": 0,
                                 "precision_price": 8, "minNotional": 5.0}), \
             patch("user_profile.load_user_profile",
                   return_value={"risk_pct_equity": 0.005, "leverage_standard": 3, "max_margin_ratio": 0.30}), \
             patch("microstructure_engine.get_live_aggtrades_tape", return_value={"live_bias": "BALANCED"}), \
             contextlib.redirect_stderr(err):
            return sp.enrich_and_size_candidate(row, target_env="prod"), err.getvalue()

    def test_tier_code_and_absorption_scored_kept(self):
        res, _ = self._enrich(dict(self.ROW, tier_code="A+", absorption_scored=True))
        self.assertEqual(res.tier_code, "A+")
        self.assertIs(res.absorption_scored, True)
        self.assertEqual(res.model_dump()["tier_code"], "A+")
        res, _ = self._enrich(dict(self.ROW, absorption_scored=False))
        self.assertIsNone(res.tier_code)
        self.assertIs(res.absorption_scored, False)
        res, _ = self._enrich(dict(self.ROW))       # no enrichment flag: absorption was never scored
        self.assertIs(res.absorption_scored, False)

    def test_defaults_keep_existing_constructors(self):
        fields = sp.CandidateSetup.model_fields
        self.assertIsNone(fields["tier_code"].default)
        self.assertIs(fields["absorption_scored"].default, True)

    # ---- #140 wrong-side rows and #141.4 logged drops ----

    def test_wrong_side_sl_dropped_with_stderr(self):
        for row in (dict(self.ROW, sl=0.0026),                                   # LONG SL above the trigger
                    dict(self.ROW, direction="SHORT", sl=0.0024, tp1=0.0024, tp2=0.0023)):   # SHORT SL below
            res, err = self._enrich(row)
            self.assertIsNone(res, row["direction"])
            self.assertIn("BEAMXUSDT", err)
            self.assertIn("SL on the wrong side", err)

    def test_wrong_side_tp2_dropped_with_stderr(self):
        for row in (dict(self.ROW, tp2=0.0025),                                  # LONG TP2 below the trigger
                    dict(self.ROW, direction="SHORT", sl=0.0026, tp1=0.0024, tp2=0.0026)):   # SHORT TP2 above
            res, err = self._enrich(row)
            self.assertIsNone(res, row["direction"])
            self.assertIn("TP2 on the wrong side", err)

    def test_right_side_rows_keep_a_positive_rr(self):
        res, err = self._enrich(dict(self.ROW))
        self.assertGreater(res.rr_ratio, 0)
        self.assertEqual(err, "")
        res, _ = self._enrich(dict(self.ROW, direction="SHORT", sl=0.0026, tp1=0.0024, tp2=0.0023))
        self.assertGreater(res.rr_ratio, 0)

    def test_unexpected_error_logged_with_type_and_symbol(self):
        res, err = self._enrich(dict(self.ROW, sl="not-a-price"))
        self.assertIsNone(res)
        self.assertIn("BEAMXUSDT", err)
        self.assertIn("ValueError", err)


class TestDoctorDependencyCheck(unittest.TestCase):

    def test_all_present_is_ok(self):
        with patch("importlib.util.find_spec", return_value=object()):
            level, msg = trading_doctor.check_dependencies()
        self.assertEqual(level, "ok")
        self.assertEqual(trading_doctor.DEPENDENCY_MODULES, ("numpy", "pydantic", "statsmodels"))

    def test_missing_module_is_a_warning_only(self):
        with patch("importlib.util.find_spec", side_effect=lambda n: None if n == "statsmodels" else object()):
            level, msg = trading_doctor.check_dependencies()
        self.assertEqual(level, "warn")
        self.assertIn("statsmodels", msg)
        self.assertNotIn("numpy", msg)


# =============================================================================
# #140 signed trigger distance, #141 radar observability
# =============================================================================
class TestSignedTriggerDistance(unittest.TestCase):

    def test_positive_when_trigger_ahead(self):
        for klines, direction in ((tac.radar_long_klines(), "LONG"), (radar_short_klines(), "SHORT")):
            cand = _broad(klines)
            self.assertEqual(cand["direction"], direction)
            price, trig = cand["price"], cand["trigger"]
            signed = (trig - price) if direction == "LONG" else (price - trig)
            self.assertGreater(cand["trigger_distance_pct"], 0)
            self.assertEqual(cand["trigger_distance_pct"], round(signed / price * 100, 2))

    def test_negative_when_crossed(self):
        # Malformed forming candle (close above its high): the only way the trigger is crossed at scan time
        klines = tac.radar_long_klines()
        klines[-1][4] = repr(float(klines[-1][2]) * 1.01)
        cand = _broad(klines)
        self.assertEqual(cand["direction"], "LONG")
        self.assertLess(cand["trigger_distance_pct"], 0)


class TestRadarObservability(unittest.TestCase):

    def test_over_ceiling_rows_counted_on_stderr(self):
        by_symbol = {"LOKUSDT": tac.radar_long_klines(), "LBADUSDT": tac.radar_long_klines(wick_low=0.94),
                     "SBADUSDT": radar_short_klines(wick_high=1.06)}
        info = {"symbols": [{"symbol": s, "underlyingType": "COIN", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                             "status": "TRADING"} for s in by_symbol]}
        tickers = [{"symbol": s, "quoteVolume": str(1000000 * (i + 1))} for i, s in enumerate(by_symbol)]

        def klines(url):
            return next(k for s, k in by_symbol.items() if f"symbol={s}&" in url)

        routes = [("exchangeInfo", info), ("ticker/24hr", tickers), ("/fapi/v1/klines", klines)]
        err = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(routes)), \
             patch("microstructure_engine.get_symbol_microstructure", side_effect=tac.micro_snapshot), \
             contextlib.redirect_stderr(err):
            res = bmr.scan_all_liquid_pairs(top_n=10, interval="15m")
        self.assertIsInstance(res, list)
        self.assertEqual([c["symbol"] for c in res], ["LOKUSDT"])
        self.assertIn("dropped 2 row(s)", err.getvalue())
        self.assertIn("LBADUSDT, SBADUSDT", err.getvalue())

    def test_text_report_trigger_spacing(self):
        cand = dict(_broad(tac.radar_long_klines()), roe_est_pct=1.0, micro=None)
        payload = {"interval": "15m", "env": "prod", "qualified_count": 1, "leverage_standard": 3,
                   "candidates": [cand]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bmr.print_text_report(payload)
        self.assertIn(f"Trigger (entry): {cand['trigger']:.4f}", out.getvalue())


if __name__ == "__main__":
    unittest.main()
