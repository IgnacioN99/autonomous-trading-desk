#!/usr/bin/env python3
"""
test_shadow_tracker.py - Unit Tests for Counterfactual Shadow Trading & Filter Efficacy Auditor.
Validates registration, deduplication, state transitions, resolution metrics, and FER calculation.
"""

import os
import sys
import json
import time
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Path resolution
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scripts.shadow_tracker as st

class TestShadowTracker(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_shadow_file = st.SHADOW_TRADES_FILE
        self.orig_resolved_file = st.SHADOW_RESOLVED_FILE
        self.orig_brief_file = st.BRIEF_FILE
        self.orig_dossier_file = st.DOSSIER_FILE

        st.SHADOW_TRADES_FILE = os.path.join(self.test_dir, "test_shadow_trades.jsonl")
        st.SHADOW_RESOLVED_FILE = os.path.join(self.test_dir, "test_shadow_resolved.jsonl")
        st.BRIEF_FILE = os.path.join(self.test_dir, "test_primed_brief.json")
        st.DOSSIER_FILE = os.path.join(self.test_dir, "test_latest_dossier.json")

    def tearDown(self):
        st.SHADOW_TRADES_FILE = self.orig_shadow_file
        st.SHADOW_RESOLVED_FILE = self.orig_resolved_file
        st.BRIEF_FILE = self.orig_brief_file
        st.DOSSIER_FILE = self.orig_dossier_file

    def test_01_register_shadow_trade(self):
        """Validates correct initialization of shadow trade record."""
        trade = st.register_shadow_trade(
            symbol="BTCUSDT",
            direction="LONG",
            trigger_price=84000.0,
            sl_price=82500.0,
            tp1_price=86000.0,
            tp2_price=88000.0,
            current_price=83900.0,
            vol_ratio=0.5,
            rejection_reason="Fake Tier S: vol_ratio 0.5x < 1.0x",
            rejection_category="DRY_VOLUME"
        )
        self.assertIsNotNone(trade)
        self.assertEqual(trade["symbol"], "BTCUSDT")
        self.assertEqual(trade["status"], "PENDING_TRIGGER")
        self.assertEqual(trade["target_dollar_risk"], 1.50)

        # Verify persistence in jsonl
        trades = st.load_jsonl(st.SHADOW_TRADES_FILE)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["symbol"], "BTCUSDT")

    def test_02_deduplication_prevention(self):
        """Validates that duplicate candidates are not registered within TTL."""
        t1 = st.register_shadow_trade(
            symbol="ETHUSDT",
            direction="SHORT",
            trigger_price=2700.0,
            sl_price=2750.0,
            tp1_price=2600.0,
            tp2_price=2500.0,
            current_price=2690.0
        )
        self.assertIsNotNone(t1)

        # Attempt duplicate registration immediately
        t2 = st.register_shadow_trade(
            symbol="ETHUSDT",
            direction="SHORT",
            trigger_price=2700.0,
            sl_price=2750.0,
            tp1_price=2600.0,
            tp2_price=2500.0,
            current_price=2690.0
        )
        self.assertIsNone(t2)

        trades = st.load_jsonl(st.SHADOW_TRADES_FILE)
        self.assertEqual(len(trades), 1)

    @patch("scripts.shadow_tracker.fetch_klines")
    def test_03_trigger_activation_and_sl_hit(self, mock_fetch):
        """Simulates price triggering LONG entry and subsequently hitting Stop Loss (True Negative)."""
        st.register_shadow_trade(
            symbol="SOLUSDT",
            direction="LONG",
            trigger_price=120.0,
            sl_price=118.0,
            tp1_price=125.0,
            tp2_price=130.0,
            current_price=119.5
        )

        now_ms = int(time.time()) * 1000
        # Candle 1: High reaches 120.5 -> triggers entry (PENDING -> ACTIVE)
        # Candle 2: Low drops to 117.5 -> hits Stop Loss (ACTIVE -> RESOLVED)
        mock_fetch.return_value = [
            [now_ms, "119.5", "120.5", "119.0", "120.0", "100"],
            [now_ms + 300000, "120.0", "120.2", "117.5", "117.8", "150"]
        ]

        res = st.audit_shadow_trades()
        self.assertEqual(res["newly_resolved"], 1)
        self.assertEqual(res["active_remaining"], 0)

        resolved = st.load_jsonl(st.SHADOW_RESOLVED_FILE)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["outcome"], "STOP_LOSS_HIT")
        self.assertEqual(resolved[0]["classification"], "TRUE_NEGATIVE")
        self.assertEqual(resolved[0]["simulated_pnl_usdt"], -1.50)

    @patch("scripts.shadow_tracker.fetch_klines")
    def test_04_trigger_activation_and_tp_hit(self, mock_fetch):
        """Simulates price triggering SHORT entry and reaching Take Profit 1 (False Negative)."""
        st.register_shadow_trade(
            symbol="XRPUSDT",
            direction="SHORT",
            trigger_price=1.60,
            sl_price=1.63,
            tp1_price=1.55,
            tp2_price=1.50,
            current_price=1.61
        )

        now_ms = int(time.time()) * 1000
        # Candle 1: Low drops to 1.595 -> triggers short
        # Candle 2: Low drops to 1.540 -> reaches TP1 (1.55)
        mock_fetch.return_value = [
            [now_ms, "1.61", "1.61", "1.595", "1.60", "200"],
            [now_ms + 300000, "1.60", "1.60", "1.540", "1.545", "300"]
        ]

        res = st.audit_shadow_trades()
        self.assertEqual(res["newly_resolved"], 1)

        resolved = st.load_jsonl(st.SHADOW_RESOLVED_FILE)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["outcome"], "TP1_HIT")
        self.assertEqual(resolved[0]["classification"], "FALSE_NEGATIVE")
        self.assertGreater(resolved[0]["simulated_pnl_usdt"], 0)

    def test_05_calculate_efficacy_metrics(self):
        """Tests Filter Efficacy Ratio and capital saved calculations."""
        # Seed 3 True Negatives and 1 False Negative
        resolved_records = [
            {"id": "1", "symbol": "A", "classification": "TRUE_NEGATIVE", "simulated_pnl_usdt": -1.50},
            {"id": "2", "symbol": "B", "classification": "TRUE_NEGATIVE", "simulated_pnl_usdt": -1.50},
            {"id": "3", "symbol": "C", "classification": "TRUE_NEGATIVE", "simulated_pnl_usdt": -1.50},
            {"id": "4", "symbol": "D", "classification": "FALSE_NEGATIVE", "simulated_pnl_usdt": 2.70},
        ]
        with open(st.SHADOW_RESOLVED_FILE, "w", encoding="utf-8") as f:
            for r in resolved_records:
                f.write(json.dumps(r) + "\n")

        metrics = st.calculate_efficacy_metrics()
        self.assertEqual(metrics["true_negatives"], 3)
        self.assertEqual(metrics["false_negatives"], 1)
        # FER = 3 / (3 + 1) * 100 = 75.0%
        self.assertEqual(metrics["filter_efficacy_ratio_pct"], 75.0)
        self.assertEqual(metrics["capital_saved_usdt"], 4.50)
        self.assertEqual(metrics["missed_alpha_usdt"], 2.70)
        self.assertEqual(metrics["net_filter_edge_usdt"], 1.80)

if __name__ == "__main__":
    unittest.main()
