#!/usr/bin/env python3
"""
test_trading_scorecard.py - scripts/trading_scorecard.py (issue #200): metrics from logs/trade_outcomes.jsonl
(resolved closed trades of the requested env only), the net -> gross R fallback, tiers from is_yolo / the latest
dossier (never leverage), the MIN_SAMPLE-gated meta-improver, staleness / env-mismatch warnings and a tolerated
shadow-desk failure.
Hermetic: temp workspace via trading_scorecard._workspace_dir, urlopen and send_signed_request blocked.
"""

import io
import os
import sys
import json
import time
import tempfile
import unittest
import contextlib
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import trading_scorecard as sc
import shadow_tracker  # noqa: F401  (patched in the shadow test)

NOW = time.time()
T = int(NOW) - 3 * 86400  # dossier evaluated three days ago (s)
INSUFFICIENT_PREFIX = "Insufficient sample (n="


def row(net=1.0, gross=1.0, env="prod", status="closed", symbol="BTCUSDT", direction="LONG", entry_s=None,
        risk=1.0, qty=1.0, pnls=(1.0,), **extra):
    r = {"symbol": symbol, "direction": direction, "entry_ts": int((entry_s or T) * 1000), "status": status,
         "realized_r_net": net, "realized_r_gross": gross, "initial_risk": risk, "filled_qty": qty,
         "is_yolo": False, "legs": [{"reason": "SL", "realized_pnl": p} for p in pnls], "since": "2026-10-01"}
    if env is not None:
        r["env"] = env
    r.update(extra)
    return r


class ScorecardBase(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.logs = os.path.join(self.ws, "logs")
        os.makedirs(os.path.join(self.logs, "evaluations"))
        for p in (patch("trading_scorecard._workspace_dir", return_value=self.ws),
                  patch("execute_futures_trade.send_signed_request", side_effect=AssertionError("Binance call")),
                  patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test"))):
            p.start()
            self.addCleanup(p.stop)
        self.outcomes = os.path.join(self.logs, "trade_outcomes.jsonl")

    def write_outcomes(self, rows):
        with open(self.outcomes, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def write_dossier(self, payload):
        with open(os.path.join(self.logs, "evaluations", "latest_dossier.json"), "w", encoding="utf-8") as f:
            f.write(payload if isinstance(payload, str) else json.dumps(payload))

    def write_insights(self, causes):
        with open(os.path.join(self.logs, "trade_insights.jsonl"), "w", encoding="utf-8") as f:
            for i, c in enumerate(causes):
                f.write(json.dumps({"id": f"i{i}", "root_cause": c}) + "\n")

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sc.main(["--env", "prod"] + argv)
        return code, out.getvalue()


class TestMetrics(ScorecardBase):

    def excluded_rows(self):
        return [row(status="open", net=None, gross=None), row(status="fills_unavailable", net=None, gross=None),
                row(env="testnet", net=5.0), row(env=None, net=5.0), row(net=None, gross=None)]

    def test_metrics_and_exclusions(self):
        self.write_outcomes([row(net=2.0, gross=2.1, risk=1.0, qty=10.0, pnls=(15.0, 6.0)),
                             row(net=-1.0, gross=-0.9, risk=2.0, qty=5.0, pnls=(-9.0,)),
                             row(net=None, gross=0.5, risk=1.0, qty=2.0, pnls=(1.0,))] + self.excluded_rows())
        s = sc.generate_scorecard("prod")
        p = s["performance"]
        self.assertEqual(s["sample_size"], 3)
        self.assertEqual(s["excluded"], {"open": 1, "fills_unavailable": 1, "other_status": 0, "other_env": 1,
                                         "unknown_env": 1, "null_r": 1})
        self.assertEqual(s["r_basis"]["gross_fallback"], 1)
        self.assertEqual((p["n"], p["wins"], p["losses"]), (3, 2, 1))
        self.assertAlmostEqual(p["win_rate_pct"], 66.67, places=2)
        self.assertAlmostEqual(p["profit_factor_r"], 2.5)
        self.assertAlmostEqual(p["expectancy_r"], 0.5)
        self.assertAlmostEqual(p["avg_win_r"], 1.25)
        self.assertAlmostEqual(p["avg_loss_r"], -1.0)
        self.assertAlmostEqual(p["mean_realized_r_gross"], round(1.7 / 3, 4))
        self.assertAlmostEqual(p["mean_realized_r_net"], 0.5)
        self.assertAlmostEqual(p["net_pnl_usdt_gross"], 13.0)
        self.assertIsNone(p["net_pnl_usdt_net"])  # one resolved row has no net R

    def test_net_usdt_and_no_losses_profit_factor(self):
        self.write_outcomes([row(net=2.0, risk=1.0, qty=10.0), row(net=-1.0, risk=2.0, qty=5.0)])
        self.assertAlmostEqual(sc.generate_scorecard("prod")["performance"]["net_pnl_usdt_net"], 10.0)
        self.write_outcomes([row(net=2.0), row(net=0.0)])
        p = sc.generate_scorecard("prod")["performance"]
        self.assertIsNone(p["profit_factor_r"])
        self.assertEqual((p["wins"], p["losses"], p["win_rate_pct"]), (1, 0, 50.0))

    def test_testnet_scored_separately(self):
        self.write_outcomes([row(env="testnet", net=3.0), row(env="mainnet", net=-1.0)])
        s = sc.generate_scorecard("testnet")
        self.assertEqual((s["sample_size"], s["excluded"]["other_env"]), (1, 1))
        self.assertAlmostEqual(s["performance"]["expectancy_r"], 3.0)

    def test_missing_and_empty_file(self):
        s = sc.generate_scorecard("prod")
        self.assertEqual(s["sample_size"], 0)
        self.assertFalse(s["source"]["exists"])
        self.assertEqual(len(s["recommendations"]), 1)
        self.assertTrue(s["recommendations"][0].startswith(INSUFFICIENT_PREFIX + "0 "))
        self.write_outcomes([])
        s = sc.generate_scorecard("prod")
        self.assertEqual((s["sample_size"], s["source"]["exists"], s["performance"]["win_rate_pct"]), (0, True, None))
        code, out = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("Sample: 0 resolved trades", out)


class TestTiers(ScorecardBase):

    def dossier(self, **cand):
        c = {"symbol": "solusdt", "direction": "long", "tier": "Tier A+"}
        c.update(cand)
        self.write_dossier({"timestamp_ts": T, "valid_until_ts": T + 1200, "approved_candidates": [c]})

    def tiers(self, rows):
        self.write_outcomes(rows)
        return {k: v["n"] for k, v in sc.generate_scorecard("prod")["tiers_breakdown"].items()}

    def test_dossier_window_and_yolo(self):
        self.dossier()
        counts = self.tiers([row(symbol="SOLUSDT", entry_s=T + 600),
                             row(symbol="SOLUSDT", entry_s=T + 1200 + 5400 - 1),  # resting fill within 90 min
                             row(symbol="SOLUSDT", entry_s=T + 1200 + 5400 + 10),  # outside the window
                             row(symbol="SOLUSDT", entry_s=T - 10),
                             row(symbol="SOLUSDT", direction="SHORT", entry_s=T + 600),
                             row(symbol="PEPEUSDT", entry_s=T + 600, is_yolo=True)])
        self.assertEqual(counts, {"S": 0, "A+": 2, "A": 0, "YOLO": 1, "unknown": 3})

    def test_tier_labels(self):
        for raw, expected in (("S", "S"), ("Tier S", "S"), ("A", "A"), ("Tier A", "A"), ("A+", "A+"),
                              ("Tier S YOLO", "YOLO"), ("B", "unknown"), (None, "unknown")):
            self.dossier(tier=raw)
            counts = self.tiers([row(symbol="SOLUSDT", entry_s=T + 60)])
            self.assertEqual(counts[expected], 1, raw)

    def test_leverage_never_used_and_no_dossier(self):
        counts = self.tiers([row(leverage=3), row(leverage=10)])
        self.assertEqual(counts["unknown"], 2)
        self.assertEqual(counts["S"] + counts["YOLO"], 0)

    def test_unreadable_dossier_gives_unknown_with_warning(self):
        self.write_dossier("{not json")
        self.write_outcomes([row(symbol="SOLUSDT", direction="LONG", entry_s=T + 60)])
        s = sc.generate_scorecard("prod")
        self.assertEqual(s["tiers_breakdown"]["unknown"]["n"], 1)
        self.assertTrue(any("dossier unreadable" in w for w in s["warnings"]))


class TestMetaImprover(ScorecardBase):

    def recs(self, rows, causes=()):
        self.write_outcomes(rows)
        self.write_insights(causes)
        return sc.generate_scorecard("prod")["recommendations"]

    def test_below_min_sample_only_insufficient_line(self):
        recs = self.recs([row(net=-1.0)] * 19, causes=["BTC_DUMP_CORRELATION"] * 3)
        self.assertEqual(recs, ["Insufficient sample (n=19 < 20 resolved trades): no parameter recommendation."])

    def test_negative_expectancy(self):
        recs = self.recs([row(net=-0.5)] * 20, causes=["BTC_DUMP_CORRELATION"] * 2)
        self.assertEqual(recs, ["Negative expectancy (-0.5000R/trade over 20): review entries before scaling.",
                                sc.CLUSTER_MSG.format(count=2)])

    def test_positive_expectancy_and_profit_factor_scales(self):
        recs = self.recs([row(net=2.0)] * 10 + [row(net=-1.0)] * 10)
        self.assertEqual(recs, [sc.SCALING_MSG.format(n=20)])
        self.assertEqual(self.recs([row(net=1.5)] * 10 + [row(net=-1.0)] * 10), [])  # PF 1.5 < 1.8

    def test_filter_text_never_appears(self):
        for rows in ([], [row(net=-1.0)] * 30, [row(net=0.1)] * 5 + [row(net=-1.0)] * 25):
            self.write_outcomes(rows)
            code, out = self.run_cli([])
            self.assertEqual(code, 0)
            self.assertNotIn("1.8x", out)
            self.assertNotIn("volume filter", out)

    def test_insights_marked_env_agnostic(self):
        self.write_insights(["BTC_DUMP_CORRELATION"])
        s = sc.generate_scorecard("prod")
        self.assertEqual(s["insights"], {"env_agnostic": True, "loss_cause_clusters": {"BTC_DUMP_CORRELATION": 1}})


class TestSourceAndCli(ScorecardBase):

    def test_stale_file_warns(self):
        self.write_outcomes([row()])
        old = NOW - 25 * 3600
        os.utime(self.outcomes, (old, old))
        code, out = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("WARNING", out)
        self.assertIn("re-run trade_outcomes.py", out)
        with open(os.path.join(self.logs, "trading_scorecard.json"), "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertGreater(saved["source"]["age_seconds"], 24 * 3600)
        self.assertEqual((saved["source"]["env_in_rows"], saved["source"]["since_in_rows"]), (["prod"], ["2026-10-01"]))

    def test_fresh_matching_file_has_no_warning(self):
        self.write_outcomes([row()])
        code, out = self.run_cli([])
        self.assertNotIn("WARNING", out)

    def test_env_mismatch_warns(self):
        self.write_outcomes([row(env="testnet")])
        code, out = self.run_cli(["--json"])
        s = json.loads(out)
        self.assertEqual((code, s["sample_size"], s["excluded"]["other_env"]), (0, 0, 1))
        self.assertTrue(any("differs from --env prod" in w for w in s["warnings"]))
        self.write_outcomes([row(env=None)])
        code, out = self.run_cli([])
        self.assertIn("re-run trade_outcomes.py --env prod", out)

    def test_out_and_outcomes_paths(self):
        alt = os.path.join(self.ws, "alt.jsonl")
        with open(alt, "w", encoding="utf-8") as f:
            f.write(json.dumps(row(net=1.0)) + "\n")
        out_path = os.path.join(self.ws, "out", "sc.json")
        code, out = self.run_cli(["--outcomes", alt, "--out", out_path, "--json"])
        self.assertEqual((code, json.loads(out)["sample_size"]), (0, 1))
        self.assertTrue(os.path.exists(out_path))
        self.assertFalse(os.path.exists(os.path.join(self.logs, "trading_scorecard.json")))

    def test_bad_env_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            sc.main(["--env", "staging"])
        self.assertEqual(cm.exception.code, 2)

    def test_shadow_failure_tolerated(self):
        open(os.path.join(self.logs, "shadow_trades.jsonl"), "w").close()
        self.write_outcomes([row()])
        with patch("shadow_tracker.calculate_efficacy_metrics", side_effect=RuntimeError("boom")):
            code, out = self.run_cli([])
            s = sc.generate_scorecard("prod")
        self.assertEqual(code, 0)
        self.assertEqual(s["shadow"], {"error": "RuntimeError: boom"})
        self.assertIn("SHADOW DESK unavailable", out)

    def test_top_level_keys(self):
        s = sc.generate_scorecard("prod")
        for key in ("timestamp_utc", "sample_size", "performance", "tiers_breakdown", "recommendations", "source",
                    "excluded", "r_basis", "score_calibration"):
            self.assertIn(key, s)
        self.assertEqual(set(s["tiers_breakdown"]), {"S", "A+", "A", "YOLO", "unknown"})


if __name__ == "__main__":
    unittest.main()
