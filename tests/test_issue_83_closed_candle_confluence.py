#!/usr/bin/env python3
"""
test_issue_83_closed_candle_confluence.py - Issue #83 (+ #85 intraday wicks, #19 brief tier label).

Every input scored together describes ONE closed candle: the taker row (ratio, CVD delta) is matched to the wick
candle by its open-time timestamp, the absorption flag needs a >= 40% wick on that candle (price change no longer
counts), a wick/taker mismatch removes the absorption bonus and says so, the radar and intraday scanners measure
`vol_ratio` and wicks on klines[-2], and the brief renders the tier code (S / A+ / A / B+), never the bare "Tier".

All market data is mocked; no network, no orders, no writes to logs/.
"""

import os
import sys
import time
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broad_market_radar as bmr  # noqa: E402
import intraday_radar as ir  # noqa: E402
import microstructure_engine as me  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import test_analytics_cli as tac  # noqa: E402  (fixtures only)
from test_issue_20_wick_consistency import (  # noqa: E402  (fixtures only)
    STEP, mixed_wick_klines, next_open_candle, upper_wick_short_klines)

SELLING = {"buySellRatio": "0.7", "buyVol": "70", "sellVol": "100"}    # aggressive sellers
BUYING = {"buySellRatio": "1.3", "buyVol": "130", "sellVol": "100"}    # aggressive buyers
NEUTRAL = {"buySellRatio": "1.0", "buyVol": "100", "sellVol": "100"}


def taker_rows(klines, default, overrides=None, skip=()):
    """One taker row per CLOSED kline (klines[:-1]), stamped with its open time like Binance does."""
    overrides = overrides or {}
    return [dict(overrides.get(k[0], default), timestamp=k[0]) for k in klines[:-1] if k[0] not in skip]


def routes(micro_klines, taker, radar_klines=None):
    oi = [{"sumOpenInterest": "1000", "sumOpenInterestValue": "100000"} for _ in range(30)]
    return [("takerlongshortRatio", taker), ("openInterestHist", oi),
            ("premiumIndex", {"lastFundingRate": "0.0001"}),
            ("/fapi/v1/klines", lambda url: micro_klines if "limit=30" in url else radar_klines)]


def micro(micro_klines, taker, **kwargs):
    with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(routes(micro_klines, taker))):
        return me.get_symbol_microstructure("AAAUSDT", period="15m", **kwargs)


class TestTakerMatchedToWickCandle(unittest.TestCase):

    def test_taker_cvd_and_wick_from_the_wick_candle_not_the_latest_row(self):
        klines = mixed_wick_klines()[-30:]
        wick = klines[-2]
        # The wick candle's row is selling; a later row (stamped to another candle) is buying and comes last.
        taker = taker_rows(klines, NEUTRAL, {wick[0]: SELLING}) + [dict(BUYING, timestamp=klines[-1][0])]
        m = micro(klines, taker)
        self.assertTrue(m["taker_candle_matched"])
        self.assertFalse(m["wick_candle_mismatch"])
        self.assertEqual(m["wick_candle_open_time"], wick[0])
        self.assertEqual(m["taker_ratio"], 0.7)
        self.assertEqual(m["cvd_current_delta"], -30.0)
        self.assertEqual((m["buy_vol"], m["sell_vol"]), (70.0, 100.0))
        self.assertEqual(m["absorption"], "BULLISH_ABSORPTION")
        self.assertIn("Taker Ratio 0.70", m["absorption_desc"])
        self.assertIn("CVD delta -30", m["absorption_desc"])
        self.assertIn("lower wick 70%", m["absorption_desc"])
        utc = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(wick[0] / 1000))
        self.assertIn(f"on the {utc} candle", m["absorption_desc"])   # the wick candle's open time is cited

    def test_candle_boundary_radar_candle_matched_by_timestamp(self):
        klines = mixed_wick_klines()
        shifted = klines[-29:] + [next_open_candle(klines)]   # radar's [-2] is now micro's [-3]
        wick = klines[-2]
        self.assertEqual(shifted[-3][0], wick[0])
        # Latest closed row (shifted[-2]) is buying; only the radar's wick candle is selling.
        taker = taker_rows(shifted, BUYING, {wick[0]: SELLING})
        self.assertNotEqual(taker[-1]["timestamp"], wick[0])
        with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(routes(shifted, taker, klines))):
            cand = bmr.analyze_single_symbol("AAAUSDT", interval="15m")
            base = cand["confidence"]
            cand = bmr.enrich_candidate_microstructure(cand)
        m = cand["micro"]
        self.assertTrue(m["taker_candle_matched"])
        self.assertEqual(m["wick_candle_open_time"], wick[0])
        self.assertEqual(m["taker_ratio"], 0.7)            # not the latest row's 1.3
        self.assertEqual(m["absorption"], "BULLISH_ABSORPTION")
        self.assertEqual(cand["confidence"], min(95, base + 15))
        self.assertIn(f"🔬 ORDER FLOW: {m['absorption_desc']}", cand["reasons"])

    def test_no_taker_row_for_the_wick_candle_fails_closed(self):
        klines = mixed_wick_klines()[-30:]
        wick = klines[-2]
        taker = taker_rows(klines, SELLING, skip=(wick[0],))
        m = micro(klines, taker)
        self.assertIsNotNone(m)
        self.assertFalse(m["taker_candle_matched"])
        self.assertEqual(m["absorption"], "NONE")
        self.assertEqual(m["taker_ratio"], 0.7)            # latest row still reported for information

    def test_taker_rows_without_timestamp_fail_closed(self):
        klines = mixed_wick_klines()[-30:]
        m = micro(klines, [dict(SELLING) for _ in range(29)])
        self.assertIsNotNone(m)
        self.assertFalse(m["taker_candle_matched"])
        self.assertEqual(m["absorption"], "NONE")
        malformed = [dict(SELLING, timestamp="n/a") for _ in range(29)]
        m = micro(klines, malformed)
        self.assertIsNotNone(m)
        self.assertFalse(m["taker_candle_matched"])
        self.assertEqual(m["absorption"], "NONE")


class TestAbsorptionNeedsTheWick(unittest.TestCase):

    @staticmethod
    def _small_wick_klines():
        """Wick candle with 10% / 10% wicks; the forming candle is flat (|price change| < 0.15%), which the old
        OR-branch accepted as absorption on its own."""
        klines = mixed_wick_klines()[-30:]
        t = klines[-2][0]
        klines[-2] = [t, "99.2", "101.0", "99.0", "100.8", "300"]
        klines[-1] = [t + STEP, "100.8", "100.9", "100.7", "100.85", "50"]
        return klines

    def test_bullish_flag_not_fired_on_sub_threshold_wick(self):
        klines = self._small_wick_klines()
        m = micro(klines, taker_rows(klines, SELLING))
        self.assertTrue(m["taker_candle_matched"])
        self.assertLess(m["lower_wick_pct"], 40.0)
        self.assertGreaterEqual(m["price_change_pct"], -0.15)
        self.assertEqual(m["absorption"], "NONE")

    def test_bearish_flag_not_fired_on_sub_threshold_wick(self):
        klines = self._small_wick_klines()
        m = micro(klines, taker_rows(klines, BUYING))
        self.assertLess(m["upper_wick_pct"], 40.0)
        self.assertLessEqual(m["price_change_pct"], 0.15)
        self.assertEqual(m["absorption"], "NONE")

    def test_bearish_flag_fires_on_upper_wick_candle(self):
        klines = upper_wick_short_klines()[-30:]
        m = micro(klines, taker_rows(klines, BUYING))
        self.assertEqual(m["absorption"], "BEARISH_ABSORPTION")
        self.assertIn("upper wick 70%", m["absorption_desc"])


class TestMismatchSkipsAbsorptionBonus(unittest.TestCase):

    NOTE = "🔬 ORDER FLOW: absorption not scored (wick/taker candle mismatch)"

    def _enrich(self, direction, absorption, **flags):
        snap = dict(tac.micro_snapshot("AAAUSDT"), absorption=absorption,
                    absorption_desc=f"Active {absorption} desc", **flags)
        cand = {"symbol": "AAAUSDT", "direction": direction, "confidence": 60, "reasons": [],
                "interval": "15m", "wick_candle_open_time": 1}
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            return bmr.enrich_candidate_microstructure(cand)

    def test_control_matched_data_gets_the_bonus(self):
        for direction, absorption in (("LONG", "BULLISH_ABSORPTION"), ("SHORT", "BEARISH_ABSORPTION")):
            cand = self._enrich(direction, absorption, wick_candle_mismatch=False, taker_candle_matched=True)
            self.assertEqual(cand["confidence"], 75, direction)
            self.assertNotIn(self.NOTE, cand["reasons"])

    def test_mismatch_removes_bonus_and_annotates(self):
        for direction, absorption in (("LONG", "BULLISH_ABSORPTION"), ("SHORT", "BEARISH_ABSORPTION")):
            for flags in ({"wick_candle_mismatch": True, "taker_candle_matched": True},
                          {"wick_candle_mismatch": False, "taker_candle_matched": False}):
                cand = self._enrich(direction, absorption, **flags)
                self.assertEqual(cand["confidence"], 60, (direction, flags))
                self.assertIn(self.NOTE, cand["reasons"])
                self.assertFalse(any(absorption in r for r in cand["reasons"]), (direction, flags))

    def test_mismatch_keeps_regime_penalty(self):
        cand = self._enrich("LONG", "BULLISH_ABSORPTION", wick_candle_mismatch=True)
        self.assertEqual(cand["confidence"], 60)
        snap = dict(tac.micro_snapshot("AAAUSDT"), regime="SHORT_BUILDUP", wick_candle_mismatch=True)
        c = {"symbol": "AAAUSDT", "direction": "LONG", "confidence": 60, "reasons": [], "interval": "15m"}
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            c = bmr.enrich_candidate_microstructure(c)
        self.assertEqual(c["confidence"], 30)
        self.assertIn(self.NOTE, c["reasons"])


class TestRadarVolRatioOnClosedCandle(unittest.TestCase):

    def _analyze(self, klines):
        with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen([("/fapi/v1/klines", lambda url: klines)])):
            return bmr.analyze_single_symbol("AAAUSDT", interval="15m")

    def test_forming_candle_volume_is_ignored(self):
        klines = mixed_wick_klines()          # closed wick candle 300 vs 100 average
        klines[-1][5] = "100000"              # huge partial volume on the forming candle
        cand = self._analyze(klines)
        self.assertEqual(cand["vol_ratio"], 3.0)
        self.assertIn("Volume climax 3.0x average", cand["reasons"])

    def test_quiet_closed_candle_is_not_a_climax(self):
        klines = mixed_wick_klines()
        klines[-2][5] = "100"
        klines[-1][5] = "100000"
        cand = self._analyze(klines)
        self.assertEqual(cand["vol_ratio"], 1.0)
        self.assertFalse(any("Volume climax" in r for r in cand["reasons"]))


class TestIntradayRadarClosedCandle(unittest.TestCase):

    def _analyze(self, klines):
        with patch.object(ir, "get_klines", return_value=klines), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             patch.object(ir.me, "candle_wick_pcts", wraps=me.candle_wick_pcts) as spy:
            return ir.analyze_symbol("AAAUSDT"), spy

    def test_wicks_from_helper_on_last_closed_candle(self):
        klines = mixed_wick_klines()
        res, spy = self._analyze(klines)
        spy.assert_called_once_with(klines[-2])
        self.assertIsNotNone(res)
        self.assertEqual(res["direction"], "LONG")
        lower, _ = me.candle_wick_pcts(klines[-2])
        self.assertIn(f"Buyer absorption wick ({lower:.0f}% of candle)", res["reasons"])
        self.assertFalse(any("Seller absorption" in r for r in res["reasons"]))  # forming ~96% upper wick ignored

    def test_vol_ratio_on_last_closed_candle(self):
        klines = mixed_wick_klines()
        klines[-1][5] = "100000"
        res, _ = self._analyze(klines)
        self.assertEqual(res["vol_ratio"], 3.0)


class TestBriefTierCode(unittest.TestCase):

    ALLOWED = {"S", "A+", "A", "B+"}

    def _tiers(self, opps):
        brief = {
            "timestamp_utc": "2026-10-06 00:00:00 UTC", "target_env": "prod", "generated_at_ts": 0,
            "macro_btc": {"btc_price": 60000.0},
            "ground_truth_portfolio": {
                "delta_bias": "NEUTRAL", "active_positions_count": 0, "positions_summary": [],
                "net_delta_usdt": 0.0, "long_notional_usdt": 0.0, "short_notional_usdt": 0.0,
                "realized_pnl_today": 0.0, "floating_pnl_usdt": 0.0, "tactical_rule": "-",
            },
            "filtered_opportunities": opps,
        }
        md = peb.format_markdown_brief(brief)
        rows = [ln for ln in md.splitlines() if ln.startswith("| **")]
        return [r.split("|")[3].strip() for r in rows]

    @staticmethod
    def _opp(sym, **kw):
        return dict({"symbol": sym, "direction": "LONG", "confidence": 70, "current_price": 1.0,
                     "sl_price": 0.95, "tp1_price": 1.1, "tp2_price": 1.2, "rr_ratio": 4.0, "reasons": []}, **kw)

    def test_labels_render_their_code(self):
        got = self._tiers([
            self._opp("S1", tier="Tier S (🔥 Maximum Institutional Conviction)"),
            self._opp("S2", tier="Tier S (🔥 Maximum Microstructural Conviction)"),
            self._opp("AP", tier="Tier A+ (High Conviction)"),
            self._opp("A1", tier="Tier A (Strong Confluence)"),
            self._opp("A2", tier="Tier A"),
            self._opp("BP", tier="Tier B+ (Moderate Opportunity)"),
        ])
        self.assertEqual(got, ["S", "S", "A+", "A", "A", "B+"])
        self.assertTrue(set(got) <= self.ALLOWED)
        self.assertNotIn("Tier", got)

    def test_tier_code_wins_when_present(self):
        got = self._tiers([self._opp("X", tier="Tier S (...)", tier_code="A+")])
        self.assertEqual(got, ["A+"])

    def test_unknown_label_renders_question_mark(self):
        got = self._tiers([self._opp("X", tier="Disqualified (<55%)"), self._opp("Y")])
        self.assertEqual(got, ["?", "?"])


if __name__ == "__main__":
    unittest.main()
