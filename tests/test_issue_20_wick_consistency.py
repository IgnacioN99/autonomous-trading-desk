#!/usr/bin/env python3
"""
test_issue_20_wick_consistency.py - Issue #20: the scanner reported inconsistent wick metrics across fields.

The radar took max(current, previous) per side, so lower and upper could come from different candles (sum
> 100%), and the microstructure engine described the still-open candle of a separate fetch in the ORDER FLOW
text. Now both wicks come from ONE candle (the last closed one), the engine is told its open time, and
candle_wick_pcts() enforces 0 <= side and lower + upper <= 100.

All market data is mocked (fake_urlopen from test_analytics_cli); no network, no orders.
"""

import os
import random
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broad_market_radar as bmr  # noqa: E402
import microstructure_engine as me  # noqa: E402
import test_analytics_cli as tac  # noqa: E402  (fixtures only)

T0 = 1_700_000_000_000   # open time (ms) of the first kline
STEP = 900_000           # 15m in ms


def _k(i, o, h, l, c, v=100.0):
    return [T0 + i * STEP, str(o), str(h), str(l), str(c), str(v)]


def mixed_wick_klines(n=55):
    """Steady decline, then a CLOSED candle with a 70% lower / 20% upper wick, then the still-open candle with a
    ~96% upper wick. The old per-side max mixed them into 70% lower + 96% upper (166% of a range)."""
    ks, price = [], 100.0
    for i in range(n - 2):
        o, c = price, price * 0.995
        ks.append(_k(i, o, o * 1.001, c * 0.999, c))
        price = c
    e = price
    ks.append(_k(n - 2, e, e * 1.03, e * 0.93, e * 1.01, 300))       # lower 70%, upper 20%
    o = e * 1.01
    ks.append(_k(n - 1, o, o * 1.05, o * 0.999, o * 1.001, 300))     # open candle: upper ~96%
    return ks


def upper_wick_short_klines(n=55):
    """Mirror of mixed_wick_klines: steady rally, then a CLOSED candle with a 70% upper / 20% lower wick, then a
    small still-open candle (SHORT setup)."""
    ks, price = [], 100.0
    for i in range(n - 2):
        o, c = price, price * 1.005
        ks.append(_k(i, o, c * 1.001, o * 0.999, c))
        price = c
    e = price
    ks.append(_k(n - 2, e, e * 1.07, e * 0.97, e * 0.99, 300))       # upper 70%, lower 20%
    o = e * 0.99
    ks.append(_k(n - 1, o, o * 1.001, o * 0.999, o * 0.999, 300))
    return ks


def next_open_candle(klines):
    """The kline that opens after klines[-1] (a candle boundary passed between two fetches)."""
    last = klines[-1]
    c = float(last[4])
    return [int(last[0]) + STEP, str(c), str(c * 1.002), str(c * 0.998), str(c * 1.001), "50"]


def micro_routes(radar_klines, micro_klines):
    """Routes for one radar fetch (limit=55) plus a full microstructure fetch (taker / OI / premium / limit=30).
    Aggressive selling (taker ratio 0.7) with a flat OI and price -> BULLISH_ABSORPTION if the lower wick >= 40%."""
    # Binance publishes one taker row per CLOSED period, stamped with that kline's open time (issue #83)
    taker = [{"buySellRatio": "0.7", "buyVol": "70", "sellVol": "100", "timestamp": k[0]} for k in micro_klines[:-1]]
    oi = [{"sumOpenInterest": "1000", "sumOpenInterestValue": "100000"} for _ in range(30)]
    return [("takerlongshortRatio", taker), ("openInterestHist", oi),
            ("premiumIndex", {"lastFundingRate": "0.0001"}),
            ("/fapi/v1/klines", lambda url: micro_klines if "limit=30" in url else radar_klines)]


class TestCandleWickPcts(unittest.TestCase):

    def test_zero_range_is_zero(self):
        self.assertEqual(me.candle_wick_pcts([0, "5", "5", "5", "5", "1"]), (0.0, 0.0))
        self.assertEqual(me.candle_wick_pcts([0, "5", "4", "6", "5", "1"]), (0.0, 0.0))  # high < low: malformed

    def test_valid_candle(self):
        lower, upper = me.candle_wick_pcts([0, "97", "100", "90", "98", "1"])
        self.assertAlmostEqual(lower, 70.0)
        self.assertAlmostEqual(upper, 20.0)

    def test_close_outside_range_is_clamped(self):
        # close above high: raw upper wick would be -50% -> body clamped to [low, high]
        lower, upper = me.candle_wick_pcts([0, "1.0", "1.1", "0.9", "1.2", "1"])
        self.assertAlmostEqual(lower, 50.0)
        self.assertAlmostEqual(upper, 0.0)
        # open above high and close below low: both raw wicks negative -> full-body candle, no wicks
        lower, upper = me.candle_wick_pcts([0, "1.2", "1.1", "0.9", "0.8", "1"])
        self.assertAlmostEqual(lower, 0.0)
        self.assertAlmostEqual(upper, 0.0)
        # close below low on a candle with a real upper wick
        lower, upper = me.candle_wick_pcts([0, "1.0", "1.1", "0.9", "0.7", "1"])
        self.assertAlmostEqual(lower, 0.0)
        self.assertAlmostEqual(upper, 50.0)

    def test_property_random_ohlc_never_exceeds_one_range(self):
        rng = random.Random(20)
        eps = me.WICK_SUM_EPSILON
        for i in range(5000):
            low = rng.uniform(1e-6, 1e5)
            high = low + rng.choice([0.0, rng.uniform(1e-9, low)])
            if i % 5 == 4:   # malformed: open/close anywhere around the range
                o, c = (rng.uniform(low * 0.5, high * 1.5 + 1e-9) for _ in range(2))
            else:
                o, c = (rng.uniform(low, high) for _ in range(2))
            lower, upper = me.candle_wick_pcts([0, repr(o), repr(high), repr(low), repr(c), "1"])
            self.assertGreaterEqual(lower, -eps)
            self.assertGreaterEqual(upper, -eps)
            self.assertLessEqual(lower + upper, 100.0 + eps, (o, high, low, c))


class TestRadarWickCandle(unittest.TestCase):

    def _analyze(self, klines):
        routes = [("/fapi/v1/klines", lambda url: klines)]
        with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(routes)):
            return bmr.analyze_single_symbol("AAAUSDT", interval="15m")

    def test_both_wicks_from_last_closed_candle(self):
        klines = mixed_wick_klines()
        cand = self._analyze(klines)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["direction"], "LONG")
        self.assertEqual(cand["lower_wick"], 70.0)   # klines[-2], not max(-1, -2) per side
        self.assertEqual(cand["upper_wick"], 20.0)   # the open candle's ~96% upper wick is ignored
        self.assertLessEqual(cand["lower_wick"] + cand["upper_wick"], 100.0)
        self.assertEqual(cand["wick_candle_open_time"], klines[-2][0])
        self.assertIn("Massive buyer absorption wick (70%)", cand["reasons"])
        self.assertFalse(any("upper wick" in r or "seller absorption" in r for r in cand["reasons"]))

    def test_malformed_wick_candle_is_clamped(self):
        klines = mixed_wick_klines()
        o, h, l = float(klines[-2][1]), float(klines[-2][2]), float(klines[-2][3])
        klines[-2][4] = repr(h * 1.02)   # close above high (bad print)
        cand = self._analyze(klines)
        self.assertIsNotNone(cand)
        self.assertAlmostEqual(cand["lower_wick"], round((o - l) / (h - l) * 100, 1))
        self.assertEqual(cand["upper_wick"], 0.0)

    def test_property_random_candles_sum_at_most_100(self):
        rng = random.Random(2020)
        checked = 0
        for _ in range(200):
            ks = mixed_wick_klines()
            for k in ks[-2:]:
                low = rng.uniform(50, 100)
                high = low * rng.uniform(1.001, 1.08)
                k[1], k[2], k[3], k[4] = (repr(rng.uniform(low, high)), repr(high), repr(low),
                                          repr(rng.uniform(low, high)))
            cand = self._analyze(ks)
            if cand is None:
                continue
            exp_lower, exp_upper = me.candle_wick_pcts(ks[-2])
            self.assertEqual(cand["lower_wick"], round(exp_lower, 1))
            self.assertEqual(cand["upper_wick"], round(exp_upper, 1))
            self.assertLessEqual(cand["lower_wick"] + cand["upper_wick"], 100.1)  # rounding of two 0.1 fields
            checked += 1
        self.assertGreater(checked, 20)


class TestRadarStopBeyondWick(unittest.TestCase):
    """Round 2: the SL is anchored beyond the more extreme of the forming candle and the wick candle, so it never
    sits inside the absorption wick that justifies the trade. TP / R:R stay derived from the final SL."""

    def _analyze(self, klines):
        routes = [("/fapi/v1/klines", lambda url: klines)]
        with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(routes)):
            return bmr.analyze_single_symbol("AAAUSDT", interval="15m")

    @staticmethod
    def _atr(klines):
        f = lambda j: [float(k[j]) for k in klines]  # noqa: E731
        return bmr.calculate_atr(f(2), f(3), f(4), period=14)

    def _assert_levels_from_sl(self, cand, sign):
        entry, sl = cand["price"], cand["sl"]
        risk_pct = abs(entry - sl) / entry * 100
        self.assertGreaterEqual(risk_pct, 1.4)   # no risk-floor override in these fixtures
        self.assertAlmostEqual(cand["risk_pct"], round(risk_pct, 2))
        self.assertAlmostEqual(cand["tp2"], entry * (1 + sign * risk_pct * 4.0 / 100))
        self.assertAlmostEqual(cand["rr"], round(abs(cand["tp2"] - entry) / abs(entry - sl), 2))

    def test_long_sl_strictly_below_wick_candle_low(self):
        klines = mixed_wick_klines()
        cand = self._analyze(klines)
        self.assertEqual(cand["direction"], "LONG")
        wick_low = float(klines[-2][3])
        self.assertLess(float(klines[-2][3]), float(klines[-1][3]))   # the wick candle holds the extreme
        self.assertLess(cand["sl"], wick_low)
        self.assertAlmostEqual(cand["sl"], wick_low - 1.3 * self._atr(klines))
        self._assert_levels_from_sl(cand, +1)

    def test_short_sl_strictly_above_wick_candle_high(self):
        klines = upper_wick_short_klines()
        cand = self._analyze(klines)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["direction"], "SHORT")
        self.assertEqual((cand["upper_wick"], cand["lower_wick"]), (70.0, 20.0))
        wick_high = float(klines[-2][2])
        self.assertGreater(wick_high, float(klines[-1][2]))
        self.assertGreater(cand["sl"], wick_high)
        self.assertAlmostEqual(cand["sl"], wick_high + 1.3 * self._atr(klines))
        self._assert_levels_from_sl(cand, -1)

    def test_forming_candle_extreme_still_used_when_beyond_the_wick(self):
        long_k = mixed_wick_klines()
        long_k[-1][3] = repr(float(long_k[-2][3]) * 0.97)    # forming candle undercuts the wick low
        cand = self._analyze(long_k)
        self.assertEqual(cand["direction"], "LONG")
        self.assertAlmostEqual(cand["sl"], float(long_k[-1][3]) - 1.3 * self._atr(long_k))
        self._assert_levels_from_sl(cand, +1)

        short_k = upper_wick_short_klines()
        short_k[-1][2] = repr(float(short_k[-2][2]) * 1.03)  # forming candle overshoots the wick high
        cand = self._analyze(short_k)
        self.assertEqual(cand["direction"], "SHORT")
        self.assertAlmostEqual(cand["sl"], float(short_k[-1][2]) + 1.3 * self._atr(short_k))
        self._assert_levels_from_sl(cand, -1)

    def test_shared_fixture_sl_below_the_absorbed_wick(self):
        klines = tac.radar_long_klines()
        cand = self._analyze(klines)
        self.assertEqual(cand["direction"], "LONG")
        self.assertLess(cand["sl"], float(klines[-2][3]))


class TestRadarMicroAgreement(unittest.TestCase):

    def _scan_and_enrich(self, radar_klines, micro_klines):
        with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(micro_routes(radar_klines, micro_klines))):
            cand = bmr.analyze_single_symbol("AAAUSDT", interval="15m")
            self.assertIsNotNone(cand)
            return bmr.enrich_candidate_microstructure(cand)

    def _assert_agree(self, cand):
        micro = cand["micro"]
        self.assertEqual(micro["lower_wick_pct"], cand["lower_wick"])
        self.assertEqual(micro["upper_wick_pct"], cand["upper_wick"])
        self.assertEqual(micro["wick_candle_open_time"], cand["wick_candle_open_time"])
        self.assertFalse(micro["wick_candle_mismatch"])
        self.assertEqual(micro["absorption"], "BULLISH_ABSORPTION")
        flow = [r for r in cand["reasons"] if r.startswith("🔬 ORDER FLOW:")]
        self.assertEqual(len(flow), 1)
        self.assertIn(f"lower wick {cand['lower_wick']:.0f}%", flow[0])
        self.assertIn(f"lower wick {micro['lower_wick_pct']:.0f}%", micro["absorption_desc"])

    def test_fields_and_order_flow_text_describe_the_same_candle(self):
        klines = mixed_wick_klines()
        cand = self._scan_and_enrich(klines, klines[-30:])
        self._assert_agree(cand)
        self.assertEqual(cand["lower_wick"], 70.0)
        self.assertIn("lower wick 70%", cand["reasons"][-1])

    def test_candle_boundary_between_fetches_keeps_the_scored_candle(self):
        klines = mixed_wick_klines()
        shifted = klines[-29:] + [next_open_candle(klines)]   # radar's [-2] is now micro's [-3]
        self.assertNotEqual(shifted[-2][0], klines[-2][0])
        cand = self._scan_and_enrich(klines, shifted)
        self._assert_agree(cand)
        self.assertEqual(cand["micro"]["lower_wick_pct"], 70.0)

    def test_radar_passes_wick_candle_open_time(self):
        klines = mixed_wick_klines()
        routes = [("/fapi/v1/klines", lambda url: klines)]
        with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(routes)), \
             patch("microstructure_engine.get_symbol_microstructure", return_value=None) as mock_micro:
            cand = bmr.analyze_single_symbol("AAAUSDT", interval="15m")
            bmr.enrich_candidate_microstructure(cand)
        self.assertEqual(mock_micro.call_args.kwargs["wick_candle_open_time"], klines[-2][0])


class TestMicroWickCandleSelection(unittest.TestCase):

    def _micro(self, micro_klines, **kwargs):
        with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(micro_routes([], micro_klines))):
            return me.get_symbol_microstructure("AAAUSDT", period="15m", **kwargs)

    def test_missing_open_time_falls_back_to_last_closed_with_mismatch_flag(self):
        klines = mixed_wick_klines()[-30:]
        m = self._micro(klines, wick_candle_open_time=T0 - STEP)   # not in this fetch
        self.assertIsNotNone(m)
        self.assertTrue(m["wick_candle_mismatch"])
        self.assertEqual(m["wick_candle_open_time"], klines[-2][0])
        self.assertEqual((m["lower_wick_pct"], m["upper_wick_pct"]), (70.0, 20.0))

    def test_default_is_last_closed_candle_not_the_open_one(self):
        klines = mixed_wick_klines()[-30:]
        m = self._micro(klines)
        self.assertFalse(m["wick_candle_mismatch"])
        self.assertEqual(m["wick_candle_open_time"], klines[-2][0])
        self.assertEqual((m["lower_wick_pct"], m["upper_wick_pct"]), (70.0, 20.0))
        self.assertIn("lower wick 70%", m["absorption_desc"])

    def test_explicit_open_time_selects_that_candle(self):
        klines = mixed_wick_klines()[-30:]
        target = klines[-3]
        m = self._micro(klines, wick_candle_open_time=target[0])
        self.assertFalse(m["wick_candle_mismatch"])
        self.assertEqual(m["wick_candle_open_time"], target[0])
        exp = me.candle_wick_pcts(target)
        self.assertEqual((m["lower_wick_pct"], m["upper_wick_pct"]), (round(exp[0], 1), round(exp[1], 1)))

    def test_zero_range_wick_candle(self):
        klines = mixed_wick_klines()[-30:]
        p = klines[-2][4]
        klines[-2] = [klines[-2][0], p, p, p, p, "10"]
        m = self._micro(klines)
        self.assertEqual((m["lower_wick_pct"], m["upper_wick_pct"]), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
