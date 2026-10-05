#!/usr/bin/env python3
"""
test_issue_52_yolo_in_pipeline.py - Issue #52: screening_pipeline integrates broad_yolo_scanner so the
primed brief carries real YOLO candidates (or an explicit INACTIVE/DISABLED/UNAVAILABLE status) instead of a
hardcoded INACTIVE string.

No network: every pipeline task (macro, radar, stat-arb, funding, news, enrichment, portfolio sync) and the
YOLO scanner are faked; urllib is blocked.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import broad_yolo_scanner as bys
import screening_pipeline as sp
import prime_evaluator_brief as peb

ENABLED_PROFILE = {"yolo_slot_enabled": True, "leverage_standard": 3, "leverage_yolo": 7, "max_margin_ratio": 0.30}
DISABLED_PROFILE = dict(ENABLED_PROFILE, yolo_slot_enabled=False)
YOLO_SIZING = {"margin_usdt": 12.0, "leverage": 7, "leverage_ceiling": 15, "margin_mode": "ISOLATED",
               "yolo_slot_enabled": True}


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def _audit_row(symbol, vol_ratio, lower_wick, price=0.0123):
    """audit_symbol()-like row with numpy scalars (what the real scanner produces)."""
    return {
        "symbol": symbol, "price": np.float64(price), "rsi": np.float64(41.3),
        "vol_ratio": np.float64(vol_ratio), "lower_wick": np.float64(lower_wick), "upper_wick": np.float64(5.0),
        "atr_pct": np.float64(1.8), "score_long": np.float64(80.5), "score_short": np.float64(20.0),
        "atr": np.float64(price * 0.018), "high": np.float64(price * 1.01), "low": np.float64(price * 0.99),
    }


def _long(symbol, vol_ratio=2.6, lower_wick=30.0, price=0.0123):
    return bys.build_levels(_audit_row(symbol, vol_ratio, lower_wick, price), "LONG", YOLO_SIZING)


def _scan_payload(slot_status="CANDIDATE", longs=None, shorts=None):
    longs = list(longs or [])
    return {
        "status": "ok", "command": "yolo", "env": "prod", "interval": "15m", "slot_status": slot_status,
        "sizing": YOLO_SIZING, "recommendation": longs[0] if longs else None, "longs": longs,
        "shorts": list(shorts or []), "volume_surges": [{"symbol": "XUSDT", "vol_ratio": 9.0}],
    }


def _setup(symbol="SOLUSDT"):
    return sp.CandidateSetup(
        symbol=symbol, direction="LONG", tier="Tier A", confidence=60, current_price=100.0, trigger_price=100.5,
        sl_price=97.0, tp1_price=104.0, tp2_price=110.0, rr_ratio=3.0, risk_pct=3.0, rsi_15m=35.0, vol_ratio=1.5,
        lower_wick_pct=55.0, upper_wick_pct=5.0, cvd_delta=0.0, oi_z_score=0.0, regime="CONSOLIDATION",
        absorption="NONE", whale_bias="BALANCED", required_margin=10.0, step_qty=1.0, actual_notional=30.0,
        target_dollar_risk=1.9, reasons=["test"])


class _PipelineFakes(unittest.TestCase):
    """Offline fakes for every non-YOLO pipeline task."""

    def setUp(self):
        macro = sp.MacroContext(btc_price=60000.0, btc_regime="NEUTRAL", btc_regime_desc="", btc_absorption="NONE",
                                btc_taker_ratio=1.0, btc_cvd_30v=0.0, btc_oi_z_score=0.0, btc_tape_bias="BALANCED",
                                btc_tape_imbalance=0.0, allows_alt_shorts=True)
        patches = [
            patch("screening_pipeline.fetch_macro_btc", return_value=macro),
            patch("broad_market_radar.scan_all_liquid_pairs", return_value=[{"symbol": "SOLUSDT"}]),
            patch("quant_risk_engine.scan_coingrated_market_pairs", return_value=[]),
            patch("funding_arbitrage.scan_top_funding_opportunities", return_value=[]),
            patch("screening_pipeline.fetch_news_summary", return_value=["<untrusted_newsletter_data>x</untrusted_newsletter_data>"]),
            patch("screening_pipeline.enrich_and_size_candidate", side_effect=lambda c, env=None: _setup(c["symbol"])),
            patch("sync_session_state.sync_session_state", return_value={"portfolio_exposure": {}, "active_positions": []}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_pipeline(self, profile=ENABLED_PROFILE, **kwargs):
        with patch("user_profile.load_user_profile", return_value=dict(profile)):
            return sp.execute_screening_pipeline(target_env="prod", **kwargs)


class TestPipelineYoloSlot(_PipelineFakes):

    def test_candidate_payload_becomes_active_slot(self):
        longs = [_long("1000PEPEUSDT"), _long("WIFUSDT", vol_ratio=1.1, lower_wick=62.0), _long("DOGEUSDT")]
        shorts = [bys.build_levels(_audit_row("BONKUSDT", 3.0, 5.0), "SHORT", YOLO_SIZING)]
        with patch("broad_yolo_scanner.scan_yolo", return_value=_scan_payload(longs=longs, shorts=shorts)) as scan:
            payload = self.run_pipeline()
        scan.assert_called_once()
        self.assertEqual(scan.call_args.args[0], "prod")
        self.assertEqual(scan.call_args.kwargs.get("interval"), sp.YOLO_SCAN_INTERVAL)
        slot = payload.yolo_slot
        self.assertEqual(slot.status, "ACTIVE")
        self.assertTrue(payload.yolo_slot_status.startswith("ACTIVE: 2 memecoin(s)"), payload.yolo_slot_status)
        self.assertEqual([c.symbol for c in slot.candidates], ["1000PEPEUSDT", "WIFUSDT"])  # capped at 2, LONG only
        self.assertEqual(len(payload.top_candidates), 1)  # standard scan untouched
        dumped = json.loads(payload.model_dump_json())["yolo_slot"]
        for c in dumped["candidates"]:
            self.assertEqual(c["direction"], "LONG")
            for key in ("price", "trigger", "sl", "tp1", "tp2", "vol_ratio", "lower_wick", "rsi", "score"):
                self.assertIs(type(c[key]), float, key)
            self.assertIs(type(c["leverage"]), int)
            self.assertNotIn("qty", c)
            self.assertLess(c["sl"], c["price"])
            self.assertLess(c["price"], c["tp1"])
        self.assertNotIn("shorts", dumped)
        self.assertNotIn("volume_surges", dumped)
        pepe = slot.candidates[0]
        self.assertAlmostEqual(pepe.trigger, float(longs[0]["trigger"]))
        self.assertEqual(pepe.leverage, 7)

    def test_empty_scan_is_inactive(self):
        with patch("broad_yolo_scanner.scan_yolo", return_value=_scan_payload("EMPTY")):
            payload = self.run_pipeline()
        self.assertEqual(payload.yolo_slot.status, "INACTIVE")
        self.assertEqual(payload.yolo_slot.candidates, [])
        self.assertTrue(payload.yolo_slot_status.startswith("INACTIVE: Preserving capital. No memecoin exceeds"))

    def test_profile_disabled_skips_scanner(self):
        with patch("broad_yolo_scanner.scan_yolo") as scan:
            payload = self.run_pipeline(profile=DISABLED_PROFILE)
        scan.assert_not_called()
        self.assertEqual(payload.yolo_slot.status, "DISABLED")
        self.assertEqual(payload.yolo_slot_status, sp.YOLO_DISABLED_STATUS)
        self.assertEqual(payload.yolo_slot.candidates, [])

    def test_slot_disabled_status_from_scanner_maps_to_disabled(self):
        with patch("broad_yolo_scanner.scan_yolo",
                   return_value=_scan_payload("CANDIDATE_SLOT_DISABLED", longs=[_long("1000PEPEUSDT")])):
            payload = self.run_pipeline()
        self.assertEqual(payload.yolo_slot.status, "DISABLED")
        self.assertEqual(payload.yolo_slot.candidates, [])

    def test_scanner_exception_is_unavailable_and_standard_scan_survives(self):
        with patch("broad_yolo_scanner.scan_yolo", side_effect=RuntimeError("No kline data could be fetched")):
            payload = self.run_pipeline()
        self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        self.assertEqual(payload.yolo_slot.candidates, [])
        self.assertTrue(payload.yolo_slot_status.startswith("UNAVAILABLE:"))
        self.assertIn("RuntimeError", payload.yolo_slot_status)
        self.assertEqual([c.symbol for c in payload.top_candidates], ["SOLUSDT"])

    def test_profile_failure_is_unavailable(self):
        with patch("user_profile.load_user_profile", side_effect=OSError("disk")), \
             patch("broad_yolo_scanner.scan_yolo") as scan:
            payload = sp.execute_screening_pipeline(target_env="prod")
        scan.assert_not_called()
        self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        self.assertEqual(len(payload.top_candidates), 1)

    def test_hung_scanner_times_out_promptly(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def _hang(*args, **kwargs):
            release.wait(10)
            return _scan_payload(longs=[_long("1000PEPEUSDT")])

        with patch("broad_yolo_scanner.scan_yolo", side_effect=_hang), \
             patch.object(sp, "YOLO_SCAN_TIMEOUT_S", 0.3):
            t0 = time.time()
            payload = self.run_pipeline()
            elapsed = time.time() - t0
        self.assertLess(elapsed, 3.0, "a hung YOLO scan must not block the pipeline")
        self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        self.assertIn("budget", payload.yolo_slot_status)
        self.assertEqual(payload.yolo_slot.candidates, [])
        self.assertEqual(len(payload.top_candidates), 1)

    def test_candidates_failing_both_filters_are_dropped(self):
        weak = _long("FAKEUSDT", vol_ratio=1.9, lower_wick=49.9)
        with patch("broad_yolo_scanner.scan_yolo", return_value=_scan_payload(longs=[weak])):
            payload = self.run_pipeline()
        self.assertEqual(payload.yolo_slot.status, "INACTIVE")
        self.assertEqual(payload.yolo_slot.candidates, [])
        with patch("broad_yolo_scanner.scan_yolo",
                   return_value=_scan_payload(longs=[weak, _long("WIFUSDT", vol_ratio=1.0, lower_wick=50.0)])):
            payload = self.run_pipeline()
        self.assertEqual([c.symbol for c in payload.yolo_slot.candidates], ["WIFUSDT"])

    def test_incoherent_or_short_entries_are_dropped(self):
        bad_levels = dict(_long("BADUSDT"), sl=1.0)          # SL above price for a LONG
        short_in_longs = dict(_long("SHRTUSDT"), direction="SHORT")
        missing = {k: v for k, v in _long("MISSUSDT").items() if k != "tp2"}
        with patch("broad_yolo_scanner.scan_yolo",
                   return_value=_scan_payload(longs=[bad_levels, short_in_longs, missing])):
            payload = self.run_pipeline()
        self.assertEqual(payload.yolo_slot.status, "INACTIVE")

    def test_include_yolo_false_skips_scanner(self):
        with patch("broad_yolo_scanner.scan_yolo") as scan:
            payload = self.run_pipeline(include_yolo=False)
        scan.assert_not_called()
        self.assertEqual(payload.yolo_slot.status, "UNAVAILABLE")
        self.assertEqual(len(payload.top_candidates), 1)


class TestYoloLevelsFromTrigger(unittest.TestCase):
    """Round 2: the evaluator enters at `trigger`, so gates and risk figures are evaluated from the trigger."""

    def _slot(self, *longs):
        return sp.build_yolo_slot(_scan_payload(longs=list(longs)))[1]

    def test_risk_roe_and_max_loss_recomputed_from_trigger(self):
        raw = _long("1000PEPEUSDT")
        cand = self._slot(raw).candidates[0]
        trigger, sl, lev, margin = float(raw["trigger"]), float(raw["sl"]), raw["leverage"], raw["margin_usdt"]
        risk = (trigger - sl) / trigger
        self.assertGreater(cand.risk_pct, float(raw["risk_pct"]))  # the scanner measured it from the lower price
        self.assertEqual(cand.risk_pct, round(risk * 100, 2))
        self.assertEqual(cand.roe_tp1_pct, round((float(raw["tp1"]) - trigger) / trigger * 100 * lev, 1))
        self.assertEqual(cand.roe_tp2_pct, round((float(raw["tp2"]) - trigger) / trigger * 100 * lev, 1))
        self.assertEqual(cand.max_loss_usdt, round(margin * lev * risk, 2))

    def test_loss_cap_of_35pct_margin_drops_high_leverage_entries(self):
        row = _audit_row("1000PEPEUSDT", 2.6, 30.0)
        ok = bys.build_levels(row, "LONG", dict(YOLO_SIZING, leverage=8))    # ~4.2% x 8 = 0.34 <= 0.35
        too_much = bys.build_levels(row, "LONG", dict(YOLO_SIZING, leverage=9))  # ~4.2% x 9 = 0.38 > 0.35
        self.assertEqual(len(self._slot(ok).candidates), 1)
        self.assertEqual(self._slot(too_much).status, "INACTIVE")
        self.assertEqual(sp.YOLO_MAX_LOSS_MARGIN_FRACTION, 0.35)

    def test_tp1_friction_floor_measured_from_trigger(self):
        raw = _long("1000PEPEUSDT")
        trigger = float(raw["trigger"])
        below = dict(raw, tp1=trigger * 1.003, tp2=trigger * 1.05)
        above = dict(raw, tp1=trigger * 1.004, tp2=trigger * 1.05)
        self.assertEqual(self._slot(below).status, "INACTIVE")
        self.assertEqual(len(self._slot(above).candidates), 1)
        self.assertEqual(sp.YOLO_MIN_TP1_DISTANCE, 0.0035)

    def test_trigger_outside_sl_tp1_band_is_dropped(self):
        raw = _long("1000PEPEUSDT")
        self.assertEqual(self._slot(dict(raw, trigger=float(raw["sl"]) * 0.999)).status, "INACTIVE")
        self.assertEqual(self._slot(dict(raw, trigger=float(raw["tp1"]) * 1.001)).status, "INACTIVE")

    def test_forwarded_floats_rounded_to_six_significant_digits(self):
        raw = _long("1000PEPEUSDT", price=0.0123456789123)
        dumped = json.loads(self._slot(raw).model_dump_json())["candidates"][0]
        self.assertEqual(dumped["price"], 0.0123457)
        for key in ("score", "price", "trigger", "sl", "tp1", "tp2", "rsi", "vol_ratio", "lower_wick"):
            self.assertEqual(dumped[key], float(f"{dumped[key]:.6g}"), key)
            self.assertLessEqual(len(f"{dumped[key]:.15g}".replace(".", "").replace("-", "").lstrip("0")), 6, key)


def _slow_scan_with_inner_executor(*args, **kwargs):
    """Mimics scan_yolo: a real inner ThreadPoolExecutor whose (non-daemon) worker is stuck on the network."""
    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=2) as inner:
        return inner.submit(time.sleep, 2.0).result()


class TestCliExitsDespiteHungYoloScan(_PipelineFakes):
    """Round 2: the --json CLI must not outlive its payload while the YOLO scanner's inner workers hang."""

    def test_hard_exit_only_when_yolo_scan_still_running(self):
        out = io.StringIO()
        with patch("broad_yolo_scanner.scan_yolo", side_effect=_slow_scan_with_inner_executor), \
             patch.object(sp, "YOLO_SCAN_TIMEOUT_S", 0.2), \
             patch("user_profile.load_user_profile", return_value=dict(ENABLED_PROFILE)), \
             patch("sys.stdout", out), \
             patch("os._exit") as hard_exit:
            code = sp.main(["--json", "--env", "prod"])
            sp._exit_without_waiting_for_yolo(code)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["yolo_slot"]["status"], "UNAVAILABLE")
        hard_exit.assert_called_once_with(0)

        with patch("broad_yolo_scanner.scan_yolo", return_value=_scan_payload("EMPTY")), \
             patch("user_profile.load_user_profile", return_value=dict(ENABLED_PROFILE)), \
             patch("sys.stdout", io.StringIO()), \
             patch("os._exit") as hard_exit:
            code = sp.main(["--json", "--env", "prod"])
            sp._exit_without_waiting_for_yolo(code)
        self.assertEqual(code, 0)
        hard_exit.assert_not_called()

    def test_main_block_uses_the_hard_exit_path(self):
        with open(os.path.join(SCRIPTS_DIR, "screening_pipeline.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('if __name__ == "__main__":\n    _run_cli()', src)

    def test_cli_process_exits_promptly_with_hung_inner_workers(self):
        """End to end in a real interpreter: without the hard exit, the inner worker (sleeping 30 s) would be
        joined at interpreter exit and the process would outlive the 20 s timeout below."""
        runner = textwrap.dedent(f"""
            import sys, time
            from unittest.mock import patch
            sys.path.insert(0, {SCRIPTS_DIR!r})
            import screening_pipeline as sp
            from concurrent.futures import ThreadPoolExecutor

            def hung_scan(*a, **k):
                with ThreadPoolExecutor(max_workers=2) as inner:
                    return inner.submit(time.sleep, 30).result()

            def no_network(*a, **k):
                raise OSError("offline test")

            patch("urllib.request.urlopen", side_effect=no_network).start()
            patch("user_profile.load_user_profile", return_value={{"yolo_slot_enabled": True}}).start()
            patch("broad_yolo_scanner.scan_yolo", side_effect=hung_scan).start()
            patch.object(sp, "YOLO_SCAN_TIMEOUT_S", 0.3).start()
            macro = sp.MacroContext(btc_price=1.0, btc_regime="N", btc_regime_desc="", btc_absorption="NONE",
                                    btc_taker_ratio=1.0, btc_cvd_30v=0.0, btc_oi_z_score=0.0, btc_tape_bias="B",
                                    btc_tape_imbalance=0.0, allows_alt_shorts=True)
            patch.object(sp, "fetch_macro_btc", return_value=macro).start()
            patch.object(sp, "fetch_news_summary", return_value=[]).start()
            patch("broad_market_radar.scan_all_liquid_pairs", return_value=[]).start()
            patch("quant_risk_engine.scan_coingrated_market_pairs", return_value=[]).start()
            patch("funding_arbitrage.scan_top_funding_opportunities", return_value=[]).start()
            patch("sync_session_state.sync_session_state", return_value={{}}).start()
            sp._run_cli(["--json", "--env", "prod"])
        """)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run_pipeline_cli.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(runner)
            t0 = time.time()
            res = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=20)
            elapsed = time.time() - t0
        self.assertLess(elapsed, 15.0)
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        payload = json.loads(res.stdout)  # flushed before the hard exit
        self.assertEqual(payload["yolo_slot"]["status"], "UNAVAILABLE")
        self.assertIn("budget", payload["yolo_slot_status"])


class TestClimaxWatcherSkipsYolo(unittest.TestCase):

    def test_watcher_requests_pipeline_without_yolo(self):
        spec = importlib.util.spec_from_file_location(
            "climax_watcher_loop_issue52", os.path.join(SCRIPTS_DIR, "loops", "climax_watcher_loop.py"))
        loop = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loop)
        fake_payload = type("P", (), {"portfolio_context": {}, "top_candidates": []})()
        with patch.object(loop, "execute_screening_pipeline", return_value=fake_payload) as pipe:
            loop.scan_for_climax_setups(target_env="prod")
        self.assertIs(pipe.call_args.kwargs.get("include_yolo"), False)


class TestBriefYoloSlot(unittest.TestCase):

    def _structured_screening(self):
        cands = [sp.YoloCandidate(**{k: v for k, v in _long(s).items() if k in sp.YoloCandidate.model_fields})
                 for s in ("1000PEPEUSDT", "WIFUSDT")]
        payload = {"yolo_slot_status": "ACTIVE: 2 memecoin(s) pass " + sp.YOLO_FILTER_TEXT + ".",
                   "yolo_slot": sp.YoloSlot(status="ACTIVE", interval="15m", candidates=cands).model_dump()}
        return json.loads(json.dumps(payload))

    def _assemble(self, screening):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(peb, "BRIEF_FILE", os.path.join(tmp, "primed_brief.json")), \
             patch.object(peb, "ensure_fresh_state", return_value={"target_env": "prod"}), \
             patch.object(peb, "get_latest_screening_payload", return_value=screening), \
             patch.object(peb, "load_recent_insights", return_value=[]), \
             patch.object(peb, "build_risk_profile", return_value={"risk_per_trade_usdt": 1.93,
                                                                   "risk_pct_equity": 0.005, "leverage_yolo": 7}):
            brief = peb.assemble_primed_brief(target_env="prod")
            with open(os.path.join(tmp, "primed_brief.json"), encoding="utf-8") as f:
                on_disk = json.load(f)
        self.assertEqual(on_disk["yolo_slot"], brief["yolo_slot"])
        return brief

    def test_structured_payload_reaches_brief_and_markdown(self):
        brief = self._assemble(self._structured_screening())
        slot = brief["yolo_slot"]
        self.assertEqual(slot["status"], "ACTIVE")
        self.assertTrue(slot["summary"].startswith("ACTIVE: 2 memecoin(s)"))
        self.assertEqual([c["symbol"] for c in slot["candidates"]], ["1000PEPEUSDT", "WIFUSDT"])
        self.assertIn("trigger", slot["candidates"][0])
        md = peb.format_markdown_brief(brief)
        self.assertIn("**YOLO Slot:** ACTIVE: 2 memecoin(s)", md)
        self.assertIn("- **1000PEPEUSDT** LONG 7x | Trigger", md)
        self.assertIn("- **WIFUSDT** LONG 7x", md)

    def test_legacy_string_and_empty_payload_fall_back(self):
        legacy = self._assemble({"yolo_slot_status": "INACTIVE: Preserving capital. legacy"})
        self.assertEqual(legacy["yolo_slot"], {"status": "INACTIVE",
                                               "summary": "INACTIVE: Preserving capital. legacy", "candidates": []})
        empty = self._assemble({})
        self.assertEqual(empty["yolo_slot"], {"status": "INACTIVE", "summary": "INACTIVE: Preserving capital.",
                                              "candidates": []})
        self.assertIn("**YOLO Slot:** INACTIVE: Preserving capital.", peb.format_markdown_brief(empty))

    def test_non_active_slot_never_forwards_candidates(self):
        screening = self._structured_screening()
        screening["yolo_slot"]["status"] = "UNAVAILABLE"
        screening["yolo_slot_status"] = "UNAVAILABLE: test."
        slot = peb.build_yolo_slot_brief(screening)
        self.assertEqual(slot["status"], "UNAVAILABLE")
        self.assertEqual(slot["candidates"], [])

    def test_markdown_tolerates_pre_issue_52_string_brief(self):
        brief = self._assemble({})
        brief["yolo_slot"] = "INACTIVE: old string brief"
        self.assertIn("**YOLO Slot:** INACTIVE: old string brief", peb.format_markdown_brief(brief))

    def test_pipeline_json_round_trips_into_brief(self):
        """End-to-end contract: the pipeline's JSON dump is what prime_evaluator_brief consumes."""
        payload = sp.MarketScreeningPayload(
            timestamp_utc="t", pipeline_latency_ms=1,
            macro=sp.MacroContext(btc_price=1.0, btc_regime="N", btc_regime_desc="", btc_absorption="NONE",
                                  btc_taker_ratio=1.0, btc_cvd_30v=0.0, btc_oi_z_score=0.0, btc_tape_bias="B",
                                  btc_tape_imbalance=0.0, allows_alt_shorts=True),
            top_candidates=[], actionable_stat_arb=[],
            yolo_slot_status=sp.build_yolo_slot(_scan_payload(longs=[_long("1000PEPEUSDT")]))[0],
            yolo_slot=sp.build_yolo_slot(_scan_payload(longs=[_long("1000PEPEUSDT")]))[1],
            news_catalysts_summary=[])
        slot = peb.build_yolo_slot_brief(json.loads(payload.model_dump_json()))
        self.assertEqual(slot["status"], "ACTIVE")
        self.assertEqual(slot["candidates"][0]["symbol"], "1000PEPEUSDT")


class TestEvaluatorPromptReadsYoloCandidates(unittest.TestCase):

    def test_rule_6_points_at_brief_yolo_candidates(self):
        with open(os.path.join(BASE_DIR, ".agents", "agents", "isolated_market_evaluator", "agent.md"),
                  encoding="utf-8") as f:
            text = f.read()
        self.assertIn("YOLO candidates come ONLY from `brief.yolo_slot.candidates`", text)
        self.assertIn("`brief.yolo_slot.status` is not `ACTIVE`", text)


if __name__ == "__main__":
    unittest.main()
