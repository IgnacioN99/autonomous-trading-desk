#!/usr/bin/env python3
"""
test_issue_22_entry_based_sizing.py - Issue #22: the evaluator brief sizes candidates from the
conditional entry (breakout trigger), not from the current price, so the loss at SL of the order that
will actually be sent stays within the profile risk per trade.

No network: equity, symbol filters, profile and the live tape are mocked; urllib is blocked.
"""

import os
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import screening_pipeline as sp
import prime_evaluator_brief as peb

# BEAMXUSDT-like filters (integer quantities, 8-decimal prices)
BEAMX_FILTERS = {"stepSize": 1.0, "minQty": 1.0, "tickSize": 0.00000001, "precision_qty": 0,
                 "precision_price": 8, "minNotional": 5.0}
PROFILE = {"risk_pct_equity": 0.005, "leverage_standard": 3, "max_margin_ratio": 0.30}
EQUITY = 386.0  # 0.5% -> risk_per_trade_usdt 1.93 (the issue's real case)


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


class TestBriefSizesFromTrigger(unittest.TestCase):

    def _enrich(self, candidate):
        with patch("quant_risk_engine.get_account_equity", return_value=EQUITY), \
             patch("execute_futures_trade.get_symbol_filters", return_value=BEAMX_FILTERS), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch("microstructure_engine.get_live_aggtrades_tape", return_value={"live_bias": "BALANCED"}):
            res = sp.enrich_and_size_candidate(candidate, target_env="prod")
        self.assertIsNotNone(res, "enrich_and_size_candidate returned None")
        return res

    def _assert_risk_within_target(self, res, trigger, sl):
        target = EQUITY * PROFILE["risk_pct_equity"]
        loss_at_trigger = res.step_qty * abs(trigger - sl)
        one_step = BEAMX_FILTERS["stepSize"] * abs(trigger - sl)
        self.assertLessEqual(loss_at_trigger, target + one_step,
                             f"loss at SL from trigger {loss_at_trigger:.4f} > target {target:.2f}")
        self.assertEqual(res.sizing_entry_price, trigger)
        self.assertEqual(res.risk_pct, round(abs(trigger - sl) / trigger * 100, 2))

    def test_long_beamx_sized_at_trigger(self):
        trigger, sl, price = 0.00252326, 0.00240840, 0.00250900  # SL 4.55% from trigger, ~4.01% from price
        res = self._enrich({"symbol": "BEAMXUSDT", "direction": "LONG", "price": price, "trigger": trigger,
                            "sl": sl, "tp1": 0.0027, "tp2": 0.0029, "rr": 3.0, "risk_pct": 4.01})
        self._assert_risk_within_target(res, trigger, sl)
        self.assertEqual(res.current_price, price)
        self.assertEqual(res.trigger_price, trigger)
        self.assertEqual(res.risk_pct, 4.55)

    def test_short_mirror_sized_at_trigger(self):
        trigger, sl, price = 0.00248000, 0.00260000, 0.00250000  # trigger below price, SL above
        res = self._enrich({"symbol": "BEAMXUSDT", "direction": "SHORT", "price": price, "trigger": trigger,
                            "sl": sl, "tp1": 0.0023, "tp2": 0.0021, "rr": 3.0, "risk_pct": 4.0})
        self._assert_risk_within_target(res, trigger, sl)
        self.assertEqual(res.current_price, price)

    def test_without_trigger_falls_back_to_current_price(self):
        price, sl = 0.0025, 0.0024
        res = self._enrich({"symbol": "BEAMXUSDT", "direction": "LONG", "price": price, "sl": sl})
        self.assertEqual(res.sizing_entry_price, price)
        self.assertEqual(res.trigger_price, price)

    def test_zero_or_null_trigger_agrees_between_sizing_and_trigger_price(self):
        price, sl = 0.0025, 0.0024
        for trig in (0, 0.0, None):
            res = self._enrich({"symbol": "BEAMXUSDT", "direction": "LONG", "price": price, "trigger": trig, "sl": sl})
            self.assertEqual(res.sizing_entry_price, price, trig)
            self.assertEqual(res.trigger_price, price, trig)


class TestBriefMarkdownTriggerColumn(unittest.TestCase):

    def _brief(self, opps):
        return {
            "timestamp_utc": "2026-10-05 00:00:00 UTC", "target_env": "prod", "generated_at_ts": 0,
            "macro_btc": {"btc_price": 60000.0},
            "ground_truth_portfolio": {
                "delta_bias": "NEUTRAL", "active_positions_count": 0, "positions_summary": [],
                "net_delta_usdt": 0.0, "long_notional_usdt": 0.0, "short_notional_usdt": 0.0,
                "realized_pnl_today": 0.0, "floating_pnl_usdt": 0.0, "tactical_rule": "-",
            },
            "filtered_opportunities": opps,
        }

    def test_setups_table_shows_trigger(self):
        md = peb.format_markdown_brief(self._brief([
            {"symbol": "BEAMXUSDT", "direction": "LONG", "tier": "Tier A", "confidence": 60,
             "current_price": 0.002509, "trigger_price": 0.00252326, "sl_price": 0.0024084,
             "tp1_price": 0.0027, "tp2_price": 0.0029, "rr_ratio": 3.0, "target_dollar_risk": 1.93, "reasons": []},
            {"symbol": "OLDUSDT", "direction": "SHORT", "tier": "Tier A", "confidence": 60,
             "current_price": 1.0, "sl_price": 1.05, "tp1_price": 0.95, "tp2_price": 0.9, "rr_ratio": 3.0, "reasons": []},
        ]))
        self.assertIn("| Price | Trigger | SL |", md)
        self.assertIn("| 0.002509 | 0.00252326 | 0.0024084 |", md)
        self.assertIn("| 1.0 | - | 1.05 |", md)


if __name__ == "__main__":
    unittest.main()
