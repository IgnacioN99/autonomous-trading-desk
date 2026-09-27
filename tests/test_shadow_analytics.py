#!/usr/bin/env python3
"""
tests/test_shadow_analytics.py — Unit Tests for Shadow Desk Forensic Analytics & Calibration Engine
"""

import unittest
from scripts.shadow_analytics import run_calibration_analysis, run_alpha_leakage_analysis, run_dodge_audit, run_intraday_hygiene_audit, format_terminal_report

class TestShadowAnalytics(unittest.TestCase):
    def setUp(self):
        self.sample_resolved = [
            {
                "symbol": "PUMPUSDT",
                "direction": "LONG",
                "vol_ratio": 0.4,
                "classification": "FALSE_NEGATIVE",
                "simulated_pnl_usdt": 2.70,
                "max_favorable_excursion_pct": 2.18,
                "max_adverse_excursion_pct": -0.39,
                "activated_at_ts": 1000,
                "resolved_at_ts": 4600,  # 3600s = 1.0h -> Intraday
                "rejection_reason": "Fake Tier S: vol_ratio 0.4x < 1.0x"
            },
            {
                "symbol": "1000PEPEUSDT",
                "direction": "LONG",
                "vol_ratio": 0.3,
                "classification": "TRUE_NEGATIVE",
                "simulated_pnl_usdt": -1.50,
                "max_favorable_excursion_pct": 2.32,
                "max_adverse_excursion_pct": -2.51,
                "activated_at_ts": 1000,
                "resolved_at_ts": 5000,  # 4000s = 1.1h -> Intraday
                "rejection_reason": "Fake Tier S: vol_ratio 0.3x < 1.0x"
            },
            {
                "symbol": "QNTUSDT",
                "direction": "SHORT",
                "vol_ratio": 0.2,
                "classification": "TRUE_NEGATIVE",
                "simulated_pnl_usdt": -1.41,
                "max_favorable_excursion_pct": 0.28,
                "max_adverse_excursion_pct": -3.71,
                "activated_at_ts": 1000,
                "resolved_at_ts": 8000,  # 7000s = 1.9h -> Intraday
                "rejection_reason": "Fake Tier S: vol_ratio 0.2x < 1.0x"
            },
            {
                "symbol": "RAREUSDT",
                "direction": "LONG",
                "vol_ratio": 0.9,
                "classification": "TRUE_NEGATIVE",
                "simulated_pnl_usdt": -1.50,
                "max_favorable_excursion_pct": 7.19,
                "max_adverse_excursion_pct": -11.90,
                "activated_at_ts": 1000,
                "resolved_at_ts": 20000,  # 19000s = 5.3h -> Multi-day drift (>4h)
                "rejection_reason": "Fake Tier S: vol_ratio 0.9x < 1.0x"
            },
            {
                "symbol": "XLMUSDT",
                "direction": "LONG",
                "vol_ratio": 0.5,
                "classification": "TIMEOUT_CLOSED",
                "simulated_pnl_usdt": 0.25,
                "activated_at_ts": 1000,
                "resolved_at_ts": 15400,
                "rejection_reason": "Fake Tier S"
            }
        ]

    def test_calibration_buckets(self):
        cal = run_calibration_analysis(self.sample_resolved)
        self.assertIn("<= 0.2x (Extreme Illiquidity)", cal)
        self.assertIn("0.21x - 0.5x (Thin Volume)", cal)
        self.assertIn("> 0.5x (Approaching Institutional)", cal)

        low = cal["<= 0.2x (Extreme Illiquidity)"]
        self.assertEqual(low["total_setups"], 1)
        self.assertEqual(low["true_negatives_avoided_sl"], 1)
        self.assertEqual(low["filter_efficacy_pct"], 100.0)

    def test_alpha_leakage(self):
        leakage = run_alpha_leakage_analysis(self.sample_resolved)
        self.assertEqual(len(leakage), 1)
        self.assertEqual(leakage[0]["symbol"], "PUMPUSDT")
        self.assertEqual(leakage[0]["simulated_pnl_usdt"], 2.70)

    def test_dodge_audit(self):
        dodges = run_dodge_audit(self.sample_resolved)
        self.assertEqual(len(dodges), 3)
        # Verify sorted by absolute MAE descending: RARE (-11.9%) should be first
        self.assertEqual(dodges[0]["symbol"], "RAREUSDT")
        self.assertEqual(dodges[0]["mae_pct"], -11.90)

    def test_intraday_hygiene_audit(self):
        hygiene = run_intraday_hygiene_audit(self.sample_resolved)
        self.assertEqual(hygiene["intraday_trades"], 3)  # PUMP, PEPE, QNT
        self.assertEqual(hygiene["drift_trades"], 1)     # RARE (5.3h)
        self.assertEqual(hygiene["timeout_closed"], 1)   # XLM
        # Intraday FER: 2 TN / (2 TN + 1 FN) = 66.7%
        self.assertEqual(hygiene["intraday_fer_pct"], 66.7)
        self.assertEqual(hygiene["intraday_saved"], 2.91)
        self.assertEqual(hygiene["intraday_missed"], 2.70)

    def test_format_terminal_report(self):
        cal = run_calibration_analysis(self.sample_resolved)
        leakage = run_alpha_leakage_analysis(self.sample_resolved)
        dodges = run_dodge_audit(self.sample_resolved)
        hygiene = run_intraday_hygiene_audit(self.sample_resolved)
        report = format_terminal_report(cal, leakage, dodges, hygiene)
        self.assertIn("INTRADAY HORIZON & SAMPLE HYGIENE AUDIT", report)
        self.assertIn("PARAMETER THRESHOLD CALIBRATION", report)
        self.assertIn("ALPHA LEAKAGE FORENSIC", report)
        self.assertIn("DODGE AUDIT & PROOF OF EDGE", report)

if __name__ == "__main__":
    unittest.main()
