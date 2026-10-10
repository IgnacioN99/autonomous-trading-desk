#!/usr/bin/env python3
"""
test_issue_269_entry_quality.py - issue #269: scripts/entry_policy_sim.py (counterfactual entry policies replayed with
the live exit) and the scorecard's entry-quality keys.

- Policies on synthetic 1m paths: current (trigger, gap at the open), fill-bar SL = immediate loss without TP credit,
  closed_candle_confirm 5m / 15m (missed on SL first, timeout, no unclosed bar), atr_buffer per k (pure formula, no
  forming bar), pullback_limit (trade-through, cancel, maker fee), orderflow_veto (veto, pass, no taker volume, no
  forming bar).
- Immediate stop-outs and their delta, real and shadow blocks, EXPIRED rows unfilled, insufficient_sample, the
  deterministic bootstrap and the paired delta, shadow trigger distance, model vs real.
- CLI: --exact-entry-only, exit codes 0 / 1 / 2 (--out outside logs/ before any read), JSON shape and atomic write,
  hook-table flag spellings, triage pattern.
- Scorecard: immediate_stop_outs, mfe_distribution, expectancy_by_direction, rows without mfe_r, calibration store.
Hermetic: SimBase (temp workspace, urlopen and send_signed_request blocked, exchangeInfo stubbed), klines faked.
"""

import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "hooks"),
          os.path.dirname(os.path.abspath(__file__)), BASE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import entry_policy_sim as ent  # noqa: E402
import exit_policy_sim as eps  # noqa: E402
import trading_scorecard as sc  # noqa: E402
from utils import score_calibration as scal  # noqa: E402
from test_exit_policy_sim import SimBase, Market, MultiMarket, outcome, fees_r, FLAT, T0, MIN, M15, NOW_MS, \
    ENTRY_MS, TAKER, MAKER  # noqa: E402
from test_issue_95_trailing_activation import flat_pre  # noqa: E402
import test_trading_scorecard as tsc  # noqa: E402  (fixtures only)

PRE6 = [(100.0, 100.2, 99.8, 100.0)] * 6  # (open, high, low, close) of T0 .. T0 + 5 min; T0 + 5 = real touch bar
QUIET = (100.2, 99.8, 100.0)  # MFE 0.2R from 100 (below the 0.3R immediate threshold)
SL_BAR = (100.0, 98.9, 99.0)
REG_S = (T0 + 30_000) / 1000.0  # shadow registration: armed at the T0 + 1 min bar


def kline(open_ms, bar, span, buy):
    """Raw kline; bar (high, low, close) opens at the close, or (open, high, low, close). buy: taker-buy fraction of
    the volume (index 9); None = a 7-field row without taker volume."""
    o, h, l, c = (bar[2], bar[0], bar[1], bar[2]) if len(bar) == 3 else bar
    row = [open_ms, str(o), str(h), str(l), str(c), "10", open_ms + span - 1]
    if buy is not None:
        row += ["0", 1, str(10 * buy), "0", "0"]
    return row


class EntryMarket(Market):
    """Market with 12-field klines (taker-buy volume at index 9) and configurable pre-touch bars: `pre` from T0 (the
    real touch bar is T0 + 5 min), then `path`. Warmup 15m bars: flat_pre (ATR 1.0) or identical bars of range `atr`;
    buy: taker-buy fraction of every bar, buy_last_closed / buy_forming override the last warmup bar / the 15m bars
    aggregated from the 1m path."""

    def __init__(self, path, pre=PRE6, warmup=40, center=100.0, atr=None, buy=0.5, buy_last_closed=None,
                 buy_forming=None):
        self.k1m = [kline(T0 + i * MIN, b, MIN, buy) for i, b in enumerate(list(pre) + list(path))]
        warm = flat_pre(warmup, center) if atr is None else [(center + atr / 2, center - atr / 2, center)] * warmup
        self.k15m = []
        for i, b in enumerate(warm):
            frac = buy_last_closed if i == warmup - 1 and buy_last_closed is not None else buy
            self.k15m.append(kline(T0 - (warmup - i) * M15, b, M15, frac))
        groups = {}
        for k in self.k1m:
            groups.setdefault(k[0] // M15 * M15, []).append(k)
        for o in sorted(groups):
            g = groups[o]
            bar = (g[0][1], max(float(k[2]) for k in g), min(float(k[3]) for k in g), g[-1][4])
            self.k15m.append(kline(o, bar, M15, buy if buy_forming is None or buy is None else buy_forming))


def shadow(**over):
    row = {"id": "shadow_BTCUSDT_1", "symbol": "BTCUSDT", "direction": "LONG", "registered_at_ts": REG_S,
           "current_price_at_eval": 100.0, "trigger_price": 100.5, "sl_price": 99.5, "tp1_price": 102.0,
           "tp2_price": 105.0, "status": "RESOLVED", "outcome": "STOP_LOSS_HIT", "classification": "TRUE_NEGATIVE",
           "dossier_sha256": None}
    row.update(over)
    return row


def sl_r(entry, sl, risk=None, entry_rate=TAKER):
    """Net R of a full stop at sl: -1R gross minus the entry and taker exit fees."""
    risk = abs(entry - sl) if risk is None else risk
    return -1.0 - (entry_rate * entry + TAKER * sl) / risk


class EntrySimBase(SimBase):

    def esim(self, rows=(), shadows=(), market=None, policies=ent.POLICY_NAMES, **kw):
        fake = market if isinstance(market, MultiMarket) else MultiMarket({"BTCUSDT": market})
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=fake):
            return ent.simulate(list(rows), list(shadows), "prod", list(policies), 48.0, TAKER, MAKER, now_ms=NOW_MS,
                                **kw)

    @staticmethod
    def trade(res, policy, block="real", i=0):
        return res[block]["policies"][policy]["trades"][i]


# =============================================================================
# 1. Fill rules per policy
# =============================================================================
class TestCurrent(EntrySimBase):

    def test_anchor_matches_exit_policy_sim(self):
        path = [FLAT] * 3 + [SL_BAR]
        res = self.esim([outcome()], market=EntryMarket(path), policies=("current",))
        t = self.trade(res, "current")
        self.assertEqual((t["status"], t["fill_price"], t["exit_kind"]), ("filled", 100.0, "sl"))
        self.assertAlmostEqual(t["r"], -1.0 - fees_r(100.0, [(1.0, 99.0, TAKER)]), places=6)
        exit_sim = self.sim([outcome()], Market(path))["policies"]["current"]["trades"][0]
        self.assertAlmostEqual(t["r"], exit_sim["r"], places=6)
        self.assertAlmostEqual(res["real"]["current_real"]["expectancy_r"], exit_sim["r"], places=6)
        mvr = res["real"]["model_vs_real"]
        self.assertEqual((mvr["compared"], mvr["mean_fill_diff_r"], mvr["mean_r_diff"]), (1, 0.0, 0.0))
        self.assertEqual(res["policies"], ["current"])

    def test_shadow_touch_inside_the_bar_and_gap_at_the_open(self):
        inside = [(100.3, 100.6, 100.2, 100.5), (100.5, 100.5, 99.4, 99.5)]
        t = self.trade(self.esim(shadows=[shadow()], market=EntryMarket(inside), policies=("current",)), "current",
                       "shadow")
        self.assertEqual((t["fill_price"], t["exit_kind"]), (100.5, "sl"))
        gap = [(100.7, 100.9, 100.6, 100.8), (100.8, 100.8, 99.4, 99.5)]
        res = self.esim(shadows=[shadow()], market=EntryMarket(gap), policies=("current",))
        t = self.trade(res, "current", "shadow")
        self.assertEqual((t["fill_price"], t["exit_kind"], t["immediate_stop"]), (100.7, "sl", True))
        self.assertAlmostEqual(t["r"], sl_r(100.7, 99.5), places=6)
        self.assertAlmostEqual(t["rr_shift"], (105 - 100.7) / 1.2 - (105 - 100.5) / 1.0, places=6)
        self.assertEqual(res["real"]["n_signals"], 0)

    def test_fill_bar_sl_is_an_immediate_loss_without_tp_credit(self):
        bar = [(100.3, 102.5, 99.4, 100.0)]  # reaches the trigger, TP1 102 and the SL in the fill bar
        t = self.trade(self.esim(shadows=[shadow()], market=EntryMarket(bar), policies=("current",)), "current",
                       "shadow")
        self.assertEqual((t["fill_price"], t["exit_kind"], t["immediate_stop"]), (100.5, "sl", True))
        self.assertAlmostEqual(t["r"], sl_r(100.5, 99.5), places=6)

    def test_gap_past_tp1_is_a_missed_fill(self):
        gap = [(100.7, 100.9, 100.6, 100.8), (100.8, 100.8, 99.4, 99.5)]
        res = self.esim(shadows=[shadow(tp1_price=100.6)], market=EntryMarket(gap), policies=("current",))
        self.assertEqual(res["shadow"]["policies"]["current"]["missed"], {"fill_beyond_tp1": 1})

    def test_untouched_shadow_trigger_is_a_missed_fill(self):
        res = self.esim(shadows=[shadow()], market=EntryMarket([QUIET] * 5), policies=("current",))
        m = res["shadow"]["policies"]["current"]
        self.assertEqual((m["n_eligible"], m["n_filled"], m["missed"], m["expectancy_r_per_signal"]),
                         (1, 0, {"no_touch": 1}, 0.0))


# Confirmation path (real row: trigger 100, SL 99, TP1 102): the 5m bar T0+5..T0+9 closes 100.4 (fill at the T0+10
# open, 100.5); the T0 15m bar pierces 100 intrabar but closes 99.95, so the 15m confirmation waits for the T0+15
# bar (close 100.3 at T0+29) and fills at the T0+30 open (100.35); everything stops at 99 at T0+31.
CONFIRM_PATH = ([(100.3, 99.9, 100.1)] * 3 + [(100.5, 100.1, 100.4), (100.6, 100.4, 100.5)]
                + [(100.5, 100.0, 100.2)] * 3 + [(100.2, 99.9, 99.95)] + [QUIET] * 14
                + [(100.4, 100.0, 100.3), (100.4, 100.2, 100.35), SL_BAR])
WIDE = dict(tp1_price=102.0, tp2_price=105.0)


class TestClosedCandleConfirm(EntrySimBase):

    def test_5m_and_15m_confirmation_fill_at_the_next_open(self):
        res = self.esim([outcome(**WIDE)], market=EntryMarket(CONFIRM_PATH))
        c5, c15 = self.trade(res, "closed_candle_confirm_5m"), self.trade(res, "closed_candle_confirm_15m")
        self.assertEqual((c5["fill_price"], c5["liquidity"], c5["exit_kind"]), (100.5, "taker", "sl"))
        self.assertAlmostEqual(c5["r"], sl_r(100.5, 99.0), places=6)
        self.assertTrue(c5["immediate_stop"])
        # no look-ahead: the T0 15m bar crossed 100 intrabar but closed below it
        self.assertEqual(c15["fill_price"], 100.35)
        self.assertAlmostEqual(c15["r"], sl_r(100.35, 99.0), places=6)
        cur = self.trade(res, "current")
        self.assertEqual((cur["fill_price"], cur["immediate_stop"]), (100.0, False))  # MFE 0.6R before the stop
        self.assertAlmostEqual(cur["r"], sl_r(100.0, 99.0), places=6)
        m = res["real"]["policies"]["closed_candle_confirm_5m"]
        self.assertEqual((m["immediate_stop_outs"], m["immediate_stop_outs_delta_vs_current"]), (1, 1))
        self.assertAlmostEqual(m["mean_rr_shift"], 4.5 / 1.5 - 5.0, places=6)

    def test_missed_when_sl_comes_first_or_after_the_timeout(self):
        res = self.esim([outcome(**WIDE)], market=EntryMarket([SL_BAR, QUIET]))
        for name in ("closed_candle_confirm_5m", "closed_candle_confirm_15m"):
            m = res["real"]["policies"][name]
            self.assertEqual((m["n_filled"], m["missed"], m["expectancy_r_per_signal"]),
                             (0, {"sl_before_fill": 1}, 0.0), name)
        self.assertTrue(self.trade(res, "current")["immediate_stop"])
        late = self.esim([outcome(**WIDE)], market=EntryMarket(CONFIRM_PATH), confirm_minutes=3)
        self.assertEqual(late["real"]["policies"]["closed_candle_confirm_5m"]["missed"], {"timeout": 1})
        self.assertEqual(late["params"]["confirm_minutes"], 3)


# ATR path (ATR_15m = 1.0, level = 100 / 1.0005): k 0.1 -> 100.05 (touch bar), 0.25 -> 100.20 (next bar), 0.5 ->
# 100.45 (gapped through by the T0+7 open 100.5); all stop at 99 at T0+11.
ATR_PATH = [(100.3, 100.0, 100.2), (100.6, 100.3, 100.5)] + [(100.5, 100.0, 100.2)] * 3 + [SL_BAR]


class TestAtrBuffer(EntrySimBase):

    def test_each_k(self):
        res = self.esim([outcome(**WIDE)], market=EntryMarket(ATR_PATH))
        level = 100.0 / 1.0005
        expected = {"atr_buffer_0_1": (level + 0.1, level + 0.1), "atr_buffer_0_25": (level + 0.25, level + 0.25),
                    "atr_buffer_0_5": (level + 0.5, 100.5)}
        for name, (trigger, fill) in expected.items():
            t = self.trade(res, name)
            self.assertAlmostEqual(t["trigger"], trigger, places=5, msg=name)
            self.assertAlmostEqual(t["fill_price"], fill, places=5, msg=name)
            self.assertAlmostEqual(t["r"], sl_r(t["fill_price"], 99.0), places=6, msg=name)
            self.assertEqual(res["real"]["policies"][name]["fill_rate"], 1.0)

    def test_pure_formula_below_the_current_trigger(self):
        pre = PRE6[:5] + [(99.97, 100.2, 99.8, 100.0)]
        res = self.esim([outcome(**WIDE)], market=EntryMarket(ATR_PATH, pre=pre, atr=0.3),
                        policies=("atr_buffer_0_1",))
        t = self.trade(res, "atr_buffer_0_1")
        self.assertAlmostEqual(t["trigger"], 100.0 / 1.0005 + 0.03, places=5)
        self.assertLess(t["fill_price"], 100.0)  # not max(current, formula)
        self.assertLess(self.trade(res, "current")["fill_price"] - t["fill_price"], 0.03)

    def test_forming_bar_never_moves_the_trigger(self):
        calm = self.esim([outcome(**WIDE)], market=EntryMarket(ATR_PATH), policies=("atr_buffer_0_25",))
        wild_pre = PRE6[:2] + [(100.0, 104.0, 99.2, 100.0)] + PRE6[3:]
        wild = self.esim([outcome(**WIDE)], market=EntryMarket(ATR_PATH, pre=wild_pre), policies=("atr_buffer_0_25",))
        self.assertAlmostEqual(self.trade(wild, "atr_buffer_0_25")["trigger"],
                               self.trade(calm, "atr_buffer_0_25")["trigger"], places=9)


# Pullback path (trigger 100, R 1): 0.25 -> limit 99.75 (touched at T0+6, traded through at T0+7); 0.5 -> 99.50,
# traded through only by the T0+9 bar that also stops at 99 (fill-bar immediate loss, maker entry).
PULLBACK_PATH = [(100.3, 99.75, 100.0), (100.0, 99.7, 99.8), (100.0, 99.6, 99.8), SL_BAR]


class TestPullbackLimit(EntrySimBase):

    def test_trade_through_and_maker_fee(self):
        res = self.esim([outcome(**WIDE)], market=EntryMarket(PULLBACK_PATH))
        p25, p50 = self.trade(res, "pullback_limit_0_25"), self.trade(res, "pullback_limit_0_5")
        self.assertEqual((p25["fill_price"], p25["liquidity"], p25["exit_kind"]), (99.75, "maker", "sl"))
        self.assertAlmostEqual(p25["r"], sl_r(99.75, 99.0, entry_rate=MAKER), places=6)  # maker-corrected replay
        self.assertFalse(p25["immediate_stop"])  # MFE (100.0 - 99.75) / 0.75 = 0.33R before the stop
        self.assertEqual((p50["fill_price"], p50["immediate_stop"]), (99.5, True))
        self.assertAlmostEqual(p50["r"], sl_r(99.5, 99.0, entry_rate=MAKER), places=6)
        self.assertAlmostEqual(res["real"]["policies"]["pullback_limit_0_25"]["mean_fees_r"],
                               (MAKER * 99.75 + TAKER * 99.0) / 0.75, places=6)

    def test_cancelled_after_n_minutes(self):
        res = self.esim([outcome(**WIDE)], market=EntryMarket(PULLBACK_PATH), pullback_minutes=1)
        for name in ("pullback_limit_0_25", "pullback_limit_0_5"):
            self.assertEqual(res["real"]["policies"][name]["missed"], {"cancelled": 1}, name)


class TestOrderflowVeto(EntrySimBase):

    PATH = [QUIET] * 3 + [SL_BAR]

    def test_veto_counts_as_unfilled_and_lowers_immediate_stop_outs(self):
        res = self.esim([outcome()], market=EntryMarket(self.PATH, buy_last_closed=0.2),
                        policies=("current", "orderflow_veto"))
        t = self.trade(res, "orderflow_veto")
        self.assertEqual((t["status"], t["oib"]), ("vetoed", -0.6))
        m, cur = res["real"]["policies"]["orderflow_veto"], res["real"]["policies"]["current"]
        self.assertEqual((m["n_eligible"], m["n_filled"], m["vetoed"], m["expectancy_r_per_signal"]), (1, 0, 1, 0.0))
        self.assertEqual((cur["immediate_stop_outs"], m["immediate_stop_outs"],
                          m["immediate_stop_outs_delta_vs_current"]), (1, 0, -1))
        delta = m["paired_delta_vs_current"]
        self.assertAlmostEqual(delta["mean_delta_r"], -sl_r(100.0, 99.0), places=6)
        self.assertEqual((delta["n"], delta["n_clusters"], delta["insufficient_sample"]), (1, 1, True))
        self.assertIsNone(cur["paired_delta_vs_current"])

    def test_short_veto_and_threshold(self):
        row = outcome(direction="SHORT", sl_price=101.0, tp1_price=98.0, tp2_price=95.0)
        res = self.esim([row], market=EntryMarket([QUIET], buy_last_closed=0.8), policies=("orderflow_veto",))
        self.assertEqual(self.trade(res, "orderflow_veto")["status"], "vetoed")
        res = self.esim([outcome()], market=EntryMarket([QUIET], buy_last_closed=0.43), policies=("orderflow_veto",))
        self.assertEqual(self.trade(res, "orderflow_veto")["status"], "filled")  # OIB -0.14: below the threshold

    def test_pass_uses_the_last_closed_bar_only(self):
        # the forming T0 15m bar is all sells (OIB -1) but is not closed at the touch: the closed bar decides
        res = self.esim([outcome()], market=EntryMarket(self.PATH, buy_forming=0.0),
                        policies=("current", "orderflow_veto"))
        t, cur = self.trade(res, "orderflow_veto"), self.trade(res, "current")
        self.assertEqual((t["status"], t["oib"], t["fill_price"]), ("filled", 0.0, cur["fill_price"]))
        self.assertAlmostEqual(t["r"], cur["r"], places=9)

    def test_no_taker_volume_is_unavailable_never_guessed(self):
        res = self.esim([outcome()], market=EntryMarket(self.PATH, buy=None), policies=("current", "orderflow_veto"))
        m = res["real"]["policies"]["orderflow_veto"]
        self.assertEqual((m["n_eligible"], m["n_filled"], m["vetoed"], m["unavailable"]),
                         (0, 0, 0, {"unavailable_no_taker_volume": 1}))
        self.assertEqual(m["paired_delta_vs_current"]["n"], 0)
        self.assertEqual(res["real"]["policies"]["current"]["n_filled"], 1)
        self.assertTrue(any("kline-derived OIB" in n for n in res["notes"]))


# =============================================================================
# 2. Blocks, samples, bootstrap
# =============================================================================
class TestBlocks(EntrySimBase):

    def test_real_and_shadow_are_separate(self):
        gap = [(100.7, 100.9, 100.6, 100.8), (100.8, 100.8, 98.9, 99.0)]
        res = self.esim([outcome()], [shadow()], market=EntryMarket(gap), policies=("current",))
        self.assertEqual((res["real"]["n_signals"], res["shadow"]["n_signals"], res["n_replayed"]), (1, 1, 2))
        self.assertEqual(self.trade(res, "current", "real")["anchor_ms"], ENTRY_MS)
        self.assertEqual(self.trade(res, "current", "shadow")["anchor_ms"], int(REG_S * 1000))
        self.assertEqual(self.trade(res, "current", "shadow")["fill_price"], 100.7)
        self.assertEqual(res["real"]["trigger_distance"]["available"], False)
        self.assertIn("not a random sample", res["shadow"]["selection_bias_note"])
        self.assertNotIn("current_real", res["shadow"])
        self.assertTrue(res["counterfactual"] and res["in_sample"])

    def test_expired_rows_are_unfilled_without_klines(self):
        fake = MultiMarket({})
        rows = [shadow(outcome="EXPIRED_UNTRIGGERED", classification="EXPIRED"),
                shadow(symbol="ETHUSDT", outcome="EXPIRED", classification="EXPIRED")]
        with patch("exit_policy_sim.fetch_exchange_info", side_effect=AssertionError("not needed")):
            res = self.esim(shadows=rows, market=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual((res["shadow"]["n_signals"], res["shadow"]["expired_unfilled"]), (2, 2))
        for name in ent.POLICY_NAMES:
            m = res["shadow"]["policies"][name]
            self.assertEqual((m["n_eligible"], m["n_filled"], m["fill_rate"], m["expectancy_r_per_signal"],
                              m["missed"]), (2, 0, 0.0, 0.0, {"expired": 2}), name)
        self.assertFalse(res["ok"])

    def test_insufficient_sample_below_50(self):
        expired = dict(outcome="EXPIRED", classification="EXPIRED")
        res = self.esim(shadows=[shadow(**expired) for _ in range(50)], market=MultiMarket({}))
        self.assertFalse(res["shadow"]["insufficient_sample"])
        self.assertFalse(res["shadow"]["policies"]["current"]["insufficient_sample"])
        self.assertFalse(res["shadow"]["policies"]["orderflow_veto"]["paired_delta_vs_current"]["insufficient_sample"])
        res = self.esim(shadows=[shadow(**expired) for _ in range(49)], market=MultiMarket({}))
        self.assertTrue(res["shadow"]["insufficient_sample"])
        self.assertTrue(res["real"]["insufficient_sample"])  # no real rows
        self.assertEqual(ent.MIN_SAMPLE, 50)

    def test_shadow_rows_skipped_by_reason(self):
        rows = [shadow(status="PENDING_TRIGGER"), shadow(trigger_price=None), shadow(sl_price=101.0),
                shadow(direction="FLAT")]
        res = self.esim(shadows=rows, market=MultiMarket({}))
        self.assertEqual(res["shadow"]["skipped"], {"not_closed": 1, "no_levels": 3})

    def test_trigger_distance_buckets_for_shadow_rows(self):
        expired = dict(outcome="EXPIRED", classification="EXPIRED")
        gap = [(100.7, 100.9, 100.6, 100.8), (100.8, 100.8, 99.4, 99.5)]
        rows = [shadow(trigger_price=100.1, **expired), shadow(),  # 0.1% and 0.5%
                shadow(trigger_price=102.0, tp1_price=104.0, **expired),  # 2%
                shadow(current_price_at_eval=None, **expired)]
        td = self.esim(shadows=rows, market=EntryMarket(gap), policies=("current",))["shadow"]["trigger_distance"]
        by = {b["bucket"]: b for b in td["buckets"]}
        self.assertEqual({k: b["n"] for k, b in by.items()}, {"<0.25%": 1, "0.25-1%": 1, ">=1%": 1})
        self.assertEqual(td["unavailable"], 1)
        self.assertEqual(by["0.25-1%"]["policies"]["current"]["n_filled"], 1)
        self.assertAlmostEqual(by["0.25-1%"]["policies"]["current"]["expectancy_r_filled"], sl_r(100.7, 99.5), places=6)
        self.assertEqual(by["<0.25%"]["policies"]["current"]["expectancy_r_per_signal"], 0.0)

    def test_paired_delta_clustered_by_dossier(self):
        rows = [outcome(dossier_sha256="d1"), outcome(symbol="ETHUSDT", dossier_sha256="d1"),
                outcome(symbol="SOLUSDT")]
        mk = {s: EntryMarket([QUIET] * 3 + [SL_BAR], buy_last_closed=0.2) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")}
        res = self.esim(rows, market=MultiMarket(mk), policies=("current", "orderflow_veto"))
        d = res["real"]["policies"]["orderflow_veto"]["paired_delta_vs_current"]
        self.assertEqual((d["n"], d["n_clusters"]), (3, 2))
        self.assertAlmostEqual(d["mean_delta_r"], -sl_r(100.0, 99.0), places=6)
        self.assertAlmostEqual(d["ci95_low"], d["ci95_high"], places=9)  # identical deltas


class TestBootstrap(unittest.TestCase):

    def test_deterministic_and_clustered(self):
        items = [("a", 1.0), ("a", -1.0), ("b", 0.5), ("c", 2.0), ("d", -0.4)]
        first, second = ent.bootstrap_mean_ci(items), ent.bootstrap_mean_ci(items)
        self.assertEqual(first, second)
        self.assertEqual((first["n"], first["n_clusters"], first["seed"]), (5, 4, ent.BOOTSTRAP_SEED))
        self.assertAlmostEqual(first["mean_delta_r"], 2.1 / 5)
        self.assertLessEqual(first["ci95_low"], first["mean_delta_r"])
        self.assertGreaterEqual(first["ci95_high"], first["mean_delta_r"])
        self.assertLess(first["ci95_low"], first["ci95_high"])
        empty = ent.bootstrap_mean_ci([])
        self.assertEqual((empty["n"], empty["mean_delta_r"], empty["ci95_low"]), (0, None, None))


# =============================================================================
# 3. CLI, hook table, triage
# =============================================================================
class TestCli(EntrySimBase):

    def write(self, name, rows):
        with open(os.path.join(self.logs, name), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def run_cli(self, argv, market=None):
        out = io.StringIO()
        fake = MultiMarket({"BTCUSDT": market or EntryMarket([QUIET] * 3 + [SL_BAR])})
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=fake), contextlib.redirect_stdout(out):
            code = ent.main(argv)
        return code, out.getvalue()

    def test_json_shape_exact_entry_only_and_atomic_write(self):
        self.write("trade_outcomes.jsonl", [outcome(), outcome(entry_commission_included=False)])
        self.write("shadow_resolved.jsonl", [shadow(outcome="EXPIRED", classification="EXPIRED")])
        code, out = self.run_cli(["--env", "prod", "--json", "--exact-entry-only"])
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(set(res), {"ok", "env", "counterfactual", "in_sample", "horizon_hours", "trail_cadence",
                                    "exact_entry_only", "fees", "params", "policies", "n_replayed", "real", "shadow",
                                    "notes", "warnings", "elapsed_seconds"})
        self.assertEqual((res["real"]["n_signals"], res["real"]["skipped"]), (1, {"entry_ts_approx": 1}))
        self.assertEqual(res["shadow"]["n_signals"], 1)
        self.assertEqual(res["policies"], list(ent.POLICY_NAMES))
        self.assertIn(ent.DECISION_NOTE, res["notes"])
        self.assertTrue(res["exact_entry_only"])
        with open(os.path.join(self.logs, "entry_policy_sim.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), res)
        self.assertEqual([n for n in os.listdir(self.logs) if n.startswith(".entry_policy_sim")], [])

    def test_human_mode(self):
        self.write("trade_outcomes.jsonl", [outcome()])
        code, out = self.run_cli(["--env", "prod", "--policies", "orderflow_veto"])
        self.assertEqual(code, 0)
        for text in ("REAL trades (decide)", "SHADOW candidates", "current", "orderflow_veto", "insufficient_sample"):
            self.assertIn(text, out)
        self.assertNotIn("pullback_limit_0_5 ", out)

    def test_exit_1_without_signals(self):
        code, out = self.run_cli(["--env", "prod", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.logs, "entry_policy_sim.json")))

    def test_exit_2_on_bad_args_and_out_outside_logs_before_any_read(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                patch("exit_policy_sim._read_jsonl", side_effect=AssertionError("read before --out check")):
            for argv in (["--policies", "nope"], ["--horizon-hours", "0"], ["--confirm-minutes", "0"],
                         ["--pullback-minutes", "-1"], ["--out", os.path.join(self.ws, "entry_policy_sim.json")],
                         ["--out", os.path.join(self.logs, "..", "x.json")]):
                self.assertEqual(self.run_cli(["--env", "prod"] + argv)[0], 2, argv)
            with self.assertRaises(SystemExit) as cm:
                self.run_cli(["--confirm-minutes", "abc"])
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "entry_policy_sim.json")))

    def test_hook_table_matches_the_cli_flags(self):
        import pre_trade_guard
        allowed, path_flags, own = pre_trade_guard.READ_ONLY_SCRIPTS["scripts/entry_policy_sim.py"]
        with open(os.path.join(BASE_DIR, "scripts", "entry_policy_sim.py"), encoding="utf-8") as f:
            cli = set(re.findall(r'add_argument\("(--[a-z-]+)"', f.read()))
        self.assertEqual(set(allowed) - set(pre_trade_guard.HELP_FLAGS), cli)
        self.assertEqual(path_flags, {"--out": "write", "--outcomes": "read", "--shadow": "read"})
        self.assertEqual(own, ("logs/entry_policy_sim.json",))
        self.assertIn("logs/entry_policy_sim.json", pre_trade_guard.READ_ONLY_FOREIGN_OUTPUTS)
        for flag in ("--exact-entry-only", "--json"):
            self.assertFalse(allowed[flag])

    def test_triage_routes_to_trading_risk(self):
        from scripts.ci.triage_pr import triage
        self.assertIn("trading_risk", triage(["scripts/entry_policy_sim.py"])["required_reviewers"])


class TestReadOnly(unittest.TestCase):

    def test_no_order_path_and_no_engine_copy(self):
        with open(os.path.join(BASE_DIR, "scripts", "entry_policy_sim.py"), encoding="utf-8") as f:
            text = f.read()
        for forbidden in ("send_signed_request", "place_order", "/fapi/v1/order", "def replay(", "class TradePath",
                          "import shadow_tracker", "import shadow_analytics"):
            self.assertNotIn(forbidden, text)
        self.assertIn("eps.replay(", text)


# =============================================================================
# 4. Scorecard entry-quality keys
# =============================================================================
class TestScorecardEntryQuality(tsc.ScorecardBase):

    def test_immediate_stop_outs_mfe_histogram_and_direction(self):
        tp_legs = [{"reason": "TP1", "realized_pnl": 0.5}, {"reason": "TP2", "realized_pnl": 2.0}]
        rows = [tsc.row(net=-1.0, exit_reason="SL", mfe_r=0.1),
                tsc.row(net=-1.0, exit_reason="SL", mfe_r=0.31, direction="SHORT"),
                tsc.row(net=2.5, exit_reason="TP2", mfe_r=2.5, legs=tp_legs),
                tsc.row(net=-1.0, mfe_r=0.2),  # no exit_reason: every leg is SL
                tsc.row(net=-1.0),  # older row: no mfe_r
                tsc.row(net=0.4, exit_reason="TRAIL", mfe_r=0.0, legs=[{"reason": "TRAIL", "realized_pnl": 0.4}])]
        self.write_outcomes(rows)
        s = sc.generate_scorecard("prod")
        imm = s["immediate_stop_outs"]
        self.assertEqual((imm["count"], imm["n_with_mfe_r"], imm["unavailable"], imm["share"]), (2, 5, 1, 0.4))
        mfe = s["mfe_distribution"]
        self.assertEqual({b["bucket"]: b["n"] for b in mfe["buckets"]},
                         {"<0.3": 3, "0.3-0.5": 1, "0.5-1": 0, "1-1.8": 0, "1.8-3": 1, ">=3": 0})
        self.assertEqual(mfe["edges"], [0.0, 0.3, 0.5, 1.0, 1.8, 3.0, None])
        self.assertEqual((mfe["n"], mfe["unavailable"], mfe["median_mfe_r"]), (5, 1, 0.2))
        self.assertAlmostEqual(mfe["mean_mfe_r"], 3.11 / 5, places=4)
        by_dir = s["expectancy_by_direction"]
        self.assertEqual(by_dir, {"source": "direction_split", "LONG": s["direction_split"]["LONG"]["expectancy_r"],
                                  "SHORT": s["direction_split"]["SHORT"]["expectancy_r"]})
        self.assertFalse(s["expectancy_by_trigger_distance"]["available"])
        code, out = self.run_cli([])
        self.assertEqual(code, 0)
        for text in ("ENTRY QUALITY (data, not an instruction)", "Immediate stop-outs (SL with MFE < 0.3R): 2 of 5",
                     "(40.0%)", "<0.3: 3", "trigger distance is not stored for real trades", "entry_policy_sim"):
            self.assertIn(text, out)

    def test_rows_without_mfe_degrade(self):
        self.write_outcomes([tsc.row(net=-1.0), tsc.row(net=1.0, exit_reason="TP2")])
        s = sc.generate_scorecard("prod")
        self.assertEqual((s["immediate_stop_outs"]["count"], s["immediate_stop_outs"]["share"],
                          s["immediate_stop_outs"]["unavailable"]), (0, None, 2))
        self.assertEqual((s["mfe_distribution"]["n"], s["mfe_distribution"]["mean_mfe_r"],
                          s["mfe_distribution"]["median_mfe_r"]), (0, None, None))
        self.assertIn("n/a", sc.format_scorecard_report(s))

    def test_calibration_store_keeps_only_its_fields(self):
        rows = [tsc.row(net=-1.0, exit_reason="SL", mfe_r=0.1, dossier_score=85, audit_ts=1000 + i,
                        score_schema_version=scal.SCORE_SCHEMA_VERSION) for i in range(3)]
        self.write_outcomes(rows)
        self.assertEqual(self.run_cli([])[0], 0)
        store = scal.load_calibration(self.ws)
        self.assertTrue(store["trades"])
        for key in ("immediate_stop_outs", "mfe_distribution", "expectancy_by_direction"):
            self.assertNotIn(key, store)
        for trade in store["trades"].values():
            self.assertTrue(set(trade) <= set(scal._STORE_FIELDS), set(trade) - set(scal._STORE_FIELDS))


if __name__ == "__main__":
    unittest.main()
