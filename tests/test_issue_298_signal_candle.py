#!/usr/bin/env python3
"""
test_issue_298_signal_candle.py - Issue #298 run C: the signal candle travels to the dossier and the re-check
(item 2), and the raw radar tier is labelled `radar_tier` with a per-run Tier S divergence count (item 5).

Covers screening_pipeline (signal_candle_open_ts / signal_interval on CandidateSetup, signal_carried_check and the
`signal_carried` re-check status, tier_s_divergence, Tier S rows still sorted first), prime_evaluator_brief (sidecar
only: the brief rows and bytes are unchanged), record_evaluation (attach from the brief's own sidecar, history
`signal_candles` / `tier_s_divergence`, summary lines), utils/recheck_brief (recheck_of inherits the bound signal
candle, the plan reaches the subprocess through RECHECK_PLAN_ENV, recheck_inputs consistency) and the radar's public
payload (`radar_tier` / `radar_tier_code`).

Hermetic: temp workspaces, fake agy transcripts (AGY_BRAIN_DIRS, fixtures of test_issue_267_recheck), faked klines,
ticker and screening steps, urllib blocked, no Binance client and no .env read (explicit --env prod everywhere).
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
TESTS_DIR = os.path.join(BASE_DIR, "tests")
for _p in (SCRIPTS_DIR, HOOKS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broad_market_radar as bmr  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import record_evaluation as rec  # noqa: E402
import screening_pipeline as sp  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
from utils import rate_limit_guard as rlg  # noqa: E402
from utils import recheck_brief as rcb  # noqa: E402
import test_issue_133_radar_levels as t133  # noqa: E402  (fixtures only)
import test_issue_187_lesson_selection as t187  # noqa: E402  (fixtures only)
import test_issue_267_recheck as t267  # noqa: E402  (fixtures only; its TestCases are not re-exported)
import test_issue_271_brief_lesson_budget as t271  # noqa: E402  (fixtures only)
import test_issue_298_recheck_window as tw  # noqa: E402  (fixtures only)

SYMBOL, DIRECTION = t267.SYMBOL, t267.DIRECTION
OLD_CAND = t267.OLD_CAND
STEP = 900  # 15m


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def signal_open(brief_ts):
    """The candle the radar scored for a brief generated at `brief_ts`: the last one closed before it."""
    return (int(brief_ts) // STEP) * STEP - STEP


def fake_klines(open_ts, now, sig_vol=300.0, vol=100.0, low=0.99, high=1.002, before=30, drop_signal=False,
                touch=None):
    """15m klines [openTime ms, o, h, l, c, v] from `before` bars ahead of the signal candle to the forming bar.
    `touch` = (low, high) of the first bar after the signal candle."""
    rows, t, last = [], open_ts - before * STEP, (int(now) // STEP) * STEP
    while t <= last:
        lo, hi = touch if (touch and t == open_ts + STEP) else (low, high)
        if not (drop_signal and t == open_ts):
            rows.append([t * 1000, "1.0", repr(hi), repr(lo), "1.0", repr(sig_vol if t == open_ts else vol)])
        t += STEP
    return rows


def macro(**kw):
    return sp.MacroContext(**dict(dict(btc_price=60000.0, btc_regime="NEUTRAL_CONSOLIDATION", btc_regime_desc="x",
                                       btc_absorption="NONE", btc_taker_ratio=1.0, btc_cvd_30v=0.0,
                                       btc_oi_z_score=0.0, btc_tape_bias="BALANCED", btc_tape_imbalance=0.0,
                                       allows_alt_shorts=True), **kw))


# =============================================================================
# 1. Signal candle: CandidateSetup -> sidecar only (the brief rows and bytes are unchanged)
# =============================================================================
class TestSignalFieldsSidecarOnly(t271.BriefCase):

    def test_candidate_setup_maps_ms_to_seconds_and_the_interval(self):
        row = dict(t133.TestCandidateSetupKeepsRadarFlags.ROW, interval="15m", wick_candle_open_time=1791640800000)
        res, err = t133.TestCandidateSetupKeepsRadarFlags._enrich(self, row)
        self.assertEqual((res.signal_candle_open_ts, res.signal_interval), (1791640800, "15m"), err)
        for bad in (None, "1791640800000", True, 0, -5, 1.79e12):
            res, _ = t133.TestCandidateSetupKeepsRadarFlags._enrich(
                self, dict(row, wick_candle_open_time=bad))
            self.assertIsNone(res.signal_candle_open_ts, bad)
        res, _ = t133.TestCandidateSetupKeepsRadarFlags._enrich(self, dict(t133.TestCandidateSetupKeepsRadarFlags.ROW))
        self.assertEqual((res.signal_candle_open_ts, res.signal_interval), (None, None))

    def test_brief_rows_and_bytes_unchanged_sidecar_rows_carry_them(self):
        """6 radar rows (the #271 session fixture, real lesson ledger): the brief is byte-identical with and without
        the signal fields, which reach the sidecar only."""
        ledger = self.ledger_file(t187.real_shaped_ledger())
        plain = t271.session_screening(6)
        signed = t271.session_screening(6)
        for i, c in enumerate(signed["top_candidates"]):
            c.update(signal_candle_open_ts=1791640800 + i * STEP, signal_interval="15m")
        with redirect_stderr(io.StringIO()):
            before = self.assemble(plain, ledger)
            before_size = os.path.getsize(self.brief_file)
            after = self.assemble(signed, ledger)
            after_size = os.path.getsize(self.brief_file)
        with open(os.path.join(os.path.dirname(self.brief_file), "primed_brief_scores.json"), encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(after["filtered_opportunities"], before["filtered_opportunities"])
        self.assertEqual(after_size, before_size)
        self.assertLessEqual(after_size, peb.BRIEF_BUDGET_BYTES)
        self.assertNotIn("dropped_lessons", after)  # the #271 pin still holds at 6 rows + 8 lessons
        self.assertNotIn("signal_candle_open_ts", json.dumps(after))
        self.assertEqual([(r["signal_candle_open_ts"], r["signal_interval"]) for r in sidecar["rows"]],
                         [(1791640800 + i * STEP, "15m") for i in range(6)])
        self.assertNotIn("tier_s_divergence", sidecar)  # the screening had none
        # The audit score_tier chain: the sidecar row keeps the radar `tier` label (read by build_score_meta)
        self.assertEqual(sidecar["rows"][0]["tier"], "Tier A (Strong Confluence / Hedge)")
        for key in ("signal_candle_open_ts", "signal_interval"):
            self.assertIn(key, peb._SIDECAR_ONLY_KEYS)


# =============================================================================
# 2. Recorder: attach from the brief's own sidecar, history fields, summary lines
# =============================================================================
class _SidecarWorkspace(tw._WindowWorkspace):
    """_WindowWorkspace whose scans are evaluated on a brief with a scores sidecar (no tests of its own)."""

    def write_sidecar(self, brief_ts, rows, divergence=None, env="PROD"):
        path = os.path.join(self.workspace, "logs", "primed_brief_scores.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict({"generated_at_ts": brief_ts, "env": env, "rows": rows},
                           **({"tier_s_divergence": divergence} if divergence else {})), f)

    def scan_with_brief(self, conv, cands, ts, brief_ts, rows=None, divergence=None, env="PROD"):
        """A full-scan dossier evaluated on a brief generated at `brief_ts` whose sidecar holds `rows`."""
        open_ts = signal_open(brief_ts)
        if rows is None:
            rows = [{"symbol": c["symbol"], "direction": c["direction"], "confidence": 95, "tier": "Tier S (x)",
                     "signal_candle_open_ts": open_ts, "signal_interval": "15m"} for c in cands]
        self.write_sidecar(brief_ts, rows, divergence, env)
        payload = {"status": "APPROVED", "evaluator_agent": dp.EVALUATOR_NAME, "target_env": "PROD",
                   "brief_generated_at_ts": brief_ts, "approved_candidates": [dict(c) for c in cands],
                   "summary": "full scan"}
        self.standard_transcript(conv, payload, ts)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(rec, "_register_shadow"), redirect_stdout(out), redirect_stderr(err):
            record = rec.record_from_subagent(conv, target_env="prod", base_dir=self.workspace, now_ts=ts + 5)
        self.scan_out = out.getvalue()
        return record, open_ts


class TestRecorderAttach(_SidecarWorkspace):

    def test_fields_attached_from_the_matching_sidecar_and_persisted(self):
        ts = self.now - 300
        record, open_ts = self.scan_with_brief(tw.CONV_1, [OLD_CAND], ts, ts - 60)
        cand = record["approved_candidates"][0]
        self.assertEqual((cand["signal_candle_open_ts"], cand["signal_interval"]), (open_ts, "15m"))
        for path in (self.dossier_path, dp.session_dossier_path(self.workspace, record["parent_conversation_id"])):
            stored = dp.load_dossier(path)
            self.assertEqual(stored["approved_candidates"][0]["signal_candle_open_ts"], open_ts)
            ok, reason, rebuilt = dp.rebuild_verified_record(stored)  # still verifies (outside the sha256)
            self.assertTrue(ok, reason)
            self.assertNotIn("signal_candle_open_ts", rebuilt["approved_candidates"][0])
            self.assertEqual(dp._verdict_fingerprint(rebuilt), dp._verdict_fingerprint(stored))
        row = self.history()[-1]
        self.assertEqual(row["signal_candles"], {f"{SYMBOL}|{DIRECTION}": {"signal_candle_open_ts": open_ts,
                                                                          "signal_interval": "15m"}})
        expected = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(open_ts))
        self.assertIn(f"       Signal candle: {expected} (15m)", self.scan_out.splitlines())
        # The radar snapshot (Tier S calibration input) is unaffected
        self.assertEqual(record["radar_snapshots"][f"{SYMBOL}|{DIRECTION}"]["radar_snapshot"]["confidence"], 95)

    def test_omitted_when_the_sidecar_is_another_brief_missing_or_another_env(self):
        """Never guessed: a sidecar of another brief (a later scan replaced it), none, or of another environment."""
        ts = self.now - 300
        rows = [{"symbol": SYMBOL, "direction": DIRECTION, "confidence": 95,
                 "signal_candle_open_ts": signal_open(ts - 60), "signal_interval": "15m"}]
        sidecar_path = os.path.join(self.workspace, "logs", "primed_brief_scores.json")
        cases = (("another brief", lambda: self.write_sidecar(ts - 120, rows)),
                 ("missing", lambda: os.path.exists(sidecar_path) and os.remove(sidecar_path)),
                 ("another env", lambda: self.write_sidecar(ts - 60, rows, env="TESTNET")),
                 ("wrong direction", lambda: self.write_sidecar(ts - 60, [dict(rows[0], direction="SHORT")])))
        for i, (label, arrange) in enumerate(cases):
            with self.subTest(label):
                arrange()
                conv = f"5ca1f00{i}-1111-4222-8333-444455556666"
                payload = {"status": "APPROVED", "evaluator_agent": dp.EVALUATOR_NAME, "target_env": "PROD",
                           "brief_generated_at_ts": ts - 60, "approved_candidates": [dict(OLD_CAND)],
                           "summary": "x"}
                self.standard_transcript(conv, payload, ts)
                with patch.object(rec, "_register_shadow"), redirect_stdout(io.StringIO()) as out, \
                        redirect_stderr(io.StringIO()):
                    record = rec.record_from_subagent(conv, target_env="prod", base_dir=self.workspace, now_ts=ts + 5)
                self.assertNotIn("signal_candle_open_ts", record["approved_candidates"][0])
                self.assertNotIn("signal_candles", self.history()[-1])
                self.assertIn("       Signal candle: unknown (no sidecar row of this brief)", out.getvalue().splitlines())

    def test_tier_s_divergence_in_the_history_row_and_summary(self):
        ts = self.now - 300
        record, _ = self.scan_with_brief(tw.CONV_1, [OLD_CAND, dict(OLD_CAND, symbol="ZROUSDT", tier="A+")], ts,
                                         ts - 60, divergence={"radar_tier_s": 11, "brief_tier_s": 1})
        self.assertEqual(self.history()[-1]["tier_s_divergence"],
                         {"radar_tier_s": 11, "brief_tier_s": 1, "dossier_tier_s": 1})
        self.assertIn("   Radar vs brief Tier S: 11 vs 1 (dossier 1; radar_tier is pre-gate, only the dossier tier "
                      "counts)", self.scan_out.splitlines())
        self.assertNotIn("tier_s_divergence", dp.load_dossier(self.dossier_path))  # history row only
        # Missing counts: omitted, never a failure
        self.scan_with_brief(tw.CONV_2, [OLD_CAND], ts + 60, ts)
        self.assertNotIn("tier_s_divergence", self.history()[-1])
        self.assertNotIn("Radar vs brief Tier S", self.scan_out)
        self.scan_with_brief(tw.CONV_3, [OLD_CAND], ts + 120, ts + 60, divergence={"radar_tier_s": "11"})
        self.assertNotIn("tier_s_divergence", self.history()[-1])


# =============================================================================
# 3. recheck_of inherits the bound signal candle; the plan reaches the subprocess
# =============================================================================
class TestRecheckOfInheritsTheSignal(_SidecarWorkspace):

    def test_latest_dossier_signal_reaches_recheck_of_and_the_pipeline(self):
        ts = self.now - 1500
        first, open_ts = self.scan_with_brief(tw.CONV_1, [OLD_CAND], ts, ts - 60)
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        self.assertEqual((brief["recheck_of"]["signal_candle_open_ts"], brief["recheck_of"]["signal_interval"]),
                         (open_ts, "15m"))
        plan = self.fetch.call_args.kwargs["plan"]
        self.assertEqual(plan, {"entry": 1.0, "stop_loss": 0.97, "tp1": 1.054, "tp2": 1.12,
                                "evaluated_ts": first["timestamp_ts"], "signal_candle_open_ts": open_ts,
                                "signal_interval": "15m", "recheck_max_drift_r": 0.25,
                                "recheck_max_age_seconds": 1800})

    def test_earlier_scan_of_the_session_via_the_history_row(self):
        ts = self.now - 1500
        first, open_ts = self.scan_with_brief(tw.CONV_1, [OLD_CAND], ts, ts - 60)
        self.scan_with_brief(tw.CONV_2, [tw.ZRO_CAND], self.now - 600, self.now - 660)
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        self.assertEqual(brief["recheck_of"]["sha256"], self.sha(first))
        self.assertEqual(brief["recheck_of"]["signal_candle_open_ts"], open_ts)
        self.assertEqual(self.fetch.call_args.kwargs["plan"]["signal_candle_open_ts"], open_ts)

    def test_unbound_signal_is_dropped_and_no_plan_is_passed(self):
        """A stored signal candle that does not fit the verified brief_generated_at_ts (edited, misaligned, after
        the brief, too old) never reaches recheck_of: no carry (fail closed)."""
        ts = self.now - 1500
        first, open_ts = self.scan_with_brief(tw.CONV_1, [OLD_CAND], ts, ts - 60)
        for bad in (open_ts + STEP, open_ts + 60, open_ts - 2 * STEP, str(open_ts), True):
            with self.subTest(bad=bad):
                for path in (self.dossier_path, dp.session_dossier_path(self.workspace,
                                                                        first["parent_conversation_id"])):
                    stored = dp.load_dossier(path)
                    stored["approved_candidates"][0]["signal_candle_open_ts"] = bad
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(stored, f)
                code, brief, err = self.run_brief()
                self.assertEqual(code, 0, err)
                self.assertEqual((brief["recheck_of"]["signal_candle_open_ts"], brief["recheck_of"]["signal_interval"]),
                                 (None, None))
                self.assertNotIn("plan", self.fetch.call_args.kwargs)

    def test_evaluator_supplied_signal_is_never_used(self):
        ts = self.now - 1500
        cand = dict(OLD_CAND, signal_candle_open_ts=signal_open(ts - 60), signal_interval="15m")
        self.scan_with_brief(tw.CONV_1, [cand], ts, ts - 60, rows=[])  # the sidecar has no signal row
        self.assertNotIn("signal_candle_open_ts", dp.load_dossier(self.dossier_path)["approved_candidates"][0])
        code, brief, err = self.run_brief()
        self.assertEqual(code, 0, err)
        self.assertIsNone(brief["recheck_of"]["signal_candle_open_ts"])
        self.assertNotIn("plan", self.fetch.call_args.kwargs)

    def test_yolo_is_still_refused(self):
        ts = self.now - 1500
        self.scan_with_brief(tw.CONV_1, [dict(OLD_CAND, is_yolo=True, tier="A")], ts, ts - 60)
        code, _, err = self.run_brief()
        self.assertEqual(code, 2)
        self.assertIn("needs a full scan", err)
        self.fetch.assert_not_called()

    def test_signal_carried_brief_and_recorder_verdict(self):
        ts = self.now - 1500
        first, open_ts = self.scan_with_brief(tw.CONV_1, [OLD_CAND], ts, ts - 60)
        carried = {"ok": True, "signal_candle_open_ts": open_ts, "signal_interval": "15m",
                   "checks": [{"check": "signal_candle", "value": "x", "limit": "y", "ok": True}]}
        payload_fn = (lambda r: t267.live_payload(r, status="signal_carried", cause="carried",
                                                  recheck={"symbol": SYMBOL, "direction": DIRECTION,
                                                           "setup_status": "signal_carried", "cause": "carried",
                                                           "carried": carried}))
        code, brief, err = self.run_brief(payload_fn=payload_fn)
        self.assertEqual(code, 0, err)
        self.assertEqual(brief["recheck"], {"symbol": SYMBOL, "direction": DIRECTION,
                                            "setup_status": "signal_carried", "cause": "carried",
                                            "carried": carried})
        self.assertEqual(brief["filtered_opportunities"], [])
        self.assertIn(f"signal candle {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(open_ts))} (15m)",
                      rcb.recheck_summary(brief))
        # The evaluator re-approves the ORIGINAL plan unchanged: the existing bounds still decide
        record, out, _ = self.record_new(self.new_payload(brief, entry=1.0, stop_loss=0.97, tp1=1.054, tp2=1.12),
                                         brief)
        self.assertTrue(record["recheck_bounds"]["within_bounds"], record["recheck_bounds"])
        self.assertEqual(record["recheck_of"]["signal_candle_open_ts"], open_ts)
        self.assertIn("WITHIN BOUNDS", out)


    def test_approval_on_a_failed_carry_is_out_of_bounds(self):
        """The evaluator wrongly re-approves the original levels on a no_setup brief whose carry failed: identical
        levels, tier and age would pass every level bound, so the setup_status check keeps it OUT OF BOUNDS."""
        ts = self.now - 1500
        self.scan_with_brief(tw.CONV_1, [OLD_CAND], ts, ts - 60)
        carried = {"ok": False, "signal_candle_open_ts": 1, "signal_interval": "15m",
                   "checks": [{"check": "sl_untouched", "value": 0.96, "limit": "> 0.97", "ok": False}]}
        payload_fn = (lambda r: t267.live_payload(r, status="no_setup", cause="radar: no setup",
                                                  recheck={"symbol": SYMBOL, "direction": DIRECTION,
                                                           "setup_status": "no_setup", "cause": "radar: no setup",
                                                           "carried": carried}))
        code, brief, err = self.run_brief(payload_fn=payload_fn)
        self.assertEqual(code, 0, err)
        self.assertEqual(brief["recheck"]["carried"], carried)
        record, out, _ = self.record_new(self.new_payload(brief, entry=1.0, stop_loss=0.97, tp1=1.054, tp2=1.12),
                                         brief)
        failed = {c["check"] for c in record["recheck_bounds"]["checks"] if not c["ok"]}
        self.assertEqual(failed, {"setup_status"})
        self.assertIn("OUT OF BOUNDS", out)
        self.assertIn("[FAIL] setup_status: no_setup", out)


class TestBoundsSetupStatus(unittest.TestCase):
    """utils/recheck_bounds: the setup_status check exists only with the brief's recheck block."""

    NOW = 1_800_000_000
    OLD = {"sha256": "a" * 64, "symbol": SYMBOL, "direction": DIRECTION, "tier": "S", "entry": 1.0,
           "stop_loss": 0.97, "tp1": 1.054, "tp2": 1.15, "evaluated_ts": NOW - 1500}
    NEW = {"status": "APPROVED", "approved_candidates": [{"symbol": SYMBOL, "direction": DIRECTION, "tier": "S",
                                                          "entry": 1.0, "stop_loss": 0.97, "tp1": 1.054,
                                                          "tp2": 1.15, "is_yolo": False}]}
    CARRIED = {"ok": True, "checks": [{"check": "signal_candle", "ok": True}]}

    def verdict(self, recheck=None):
        from utils import recheck_bounds as rb
        kw = {} if recheck is None else {"recheck": recheck}
        return rb.evaluate_recheck_bounds(dict(self.OLD), dict(self.NEW), self.NOW, 0.25, 1800, **kw)

    def failed(self, v):
        return {c["check"] for c in v["checks"] if not c["ok"]}

    def test_absent_block_adds_no_check(self):
        v = self.verdict()
        self.assertTrue(v["within_bounds"], v)
        self.assertNotIn("setup_status", {c["check"] for c in v["checks"]})
        self.assertNotIn("setup_status", {c["check"] for c in self.verdict(recheck="x")["checks"]})

    def test_found_and_a_fully_passed_carry_are_within_bounds(self):
        for block in ({"setup_status": "found"}, {"setup_status": "signal_carried", "carried": self.CARRIED}):
            with self.subTest(block=block):
                v = self.verdict(block)
                self.assertTrue(v["within_bounds"], v)
                self.assertIn("setup_status", {c["check"] for c in v["checks"]})

    def test_gone_unavailable_or_partial_carry_fail_only_that_check(self):
        for block in ({"setup_status": "no_setup"}, {"setup_status": "unavailable"}, {},
                      {"setup_status": "signal_carried"},
                      {"setup_status": "signal_carried", "carried": dict(self.CARRIED, ok=False)},
                      {"setup_status": "signal_carried",
                       "carried": {"ok": True, "checks": [{"check": "rr_tp2", "ok": False}]}},
                      {"setup_status": "signal_carried", "carried": {"ok": True, "checks": []}}):
            with self.subTest(block=block):
                v = self.verdict(block)
                self.assertFalse(v["within_bounds"])
                self.assertEqual(self.failed(v), {"setup_status"})


# =============================================================================
# 4. recheck_inputs and the subprocess transport
# =============================================================================
class TestRecheckInputsAndTransport(unittest.TestCase):

    CARRIED = {"ok": True, "signal_candle_open_ts": 1791640800, "signal_interval": "15m",
               "checks": [{"check": "signal_candle", "ok": True}, {"check": "rr_tp2", "ok": True}]}

    def inputs(self, carried, status="signal_carried", **extra):
        payload = t267.live_payload("rid", status=status, cause="c", **extra)
        payload["recheck"]["carried"] = carried
        return rcb.recheck_inputs(payload, SYMBOL, DIRECTION, "rid")

    def test_signal_carried_needs_every_check_passing(self):
        screening, block = self.inputs(self.CARRIED)
        self.assertEqual((block["setup_status"], block["carried"]), ("signal_carried", self.CARRIED))
        self.assertEqual(screening["top_candidates"], [])
        bad_checks = [dict(self.CARRIED, ok=False),
                      dict(self.CARRIED, checks=[{"check": "signal_candle", "ok": True}, {"check": "rr", "ok": False}]),
                      dict(self.CARRIED, checks=[]), dict(self.CARRIED, checks="x"), dict(self.CARRIED, ok="true")]
        for carried in bad_checks + [None]:
            with self.subTest(carried=carried):
                _, block = self.inputs(carried)
                self.assertEqual(block["setup_status"], "no_setup")
                self.assertIn("without every carried check passing", block["cause"])
        _, block = self.inputs(self.CARRIED, market_data_status="UNAVAILABLE: Binance rate limit (HTTP 429)")
        self.assertEqual(block["setup_status"], "unavailable")
        self.assertNotIn("carried", block)
        _, block = self.inputs(dict(self.CARRIED, ok=False), status="no_setup")
        self.assertEqual((block["setup_status"], block["carried"]["ok"]), ("no_setup", False))

    def test_plan_env_set_only_with_a_plan_and_never_inherited(self):
        class Done:
            returncode, stdout = 0, json.dumps(t267.live_payload("rid"))
        plan = {"entry": 1.0, "signal_candle_open_ts": 1791640800}
        with patch.dict(os.environ, {rcb.RECHECK_PLAN_ENV: '{"entry": 9}'}), \
                patch("subprocess.run", return_value=Done()) as run:
            rcb.fetch_recheck_payload(SYMBOL, DIRECTION, "prod", "rid", BASE_DIR)
            self.assertNotIn(rcb.RECHECK_PLAN_ENV, run.call_args[1]["env"])
            rcb.fetch_recheck_payload(SYMBOL, DIRECTION, "prod", "rid", BASE_DIR, plan=plan)
            self.assertEqual(json.loads(run.call_args[1]["env"][rcb.RECHECK_PLAN_ENV]), plan)
            self.assertEqual(run.call_args[0][0][2:], ["--json", "--env", "prod", "--recheck", "ETHFIUSDT:LONG"])

    def test_cli_reads_the_plan_from_the_environment(self):
        payload = sp.MarketScreeningPayload(timestamp_utc="x", pipeline_latency_ms=0, macro=macro(),
                                            top_candidates=[], actionable_stat_arb=[], yolo_slot_status="x",
                                            news_catalysts_summary=[])
        for raw, expected in (('{"entry": 1.0}', {"plan": {"entry": 1.0}}), ("not json", {}), ("[1]", {})):
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {rcb.RECHECK_PLAN_ENV: raw}), \
                        patch.object(sp, "execute_symbol_recheck", return_value=payload) as run, \
                        patch("sys.stdout", io.StringIO()):
                    self.assertEqual(sp.main(["--json", "--env", "prod", "--recheck", "ethfiusdt:long"]), 0)
                run.assert_called_once_with("ETHFIUSDT", "LONG", target_env="prod", **expected)


# =============================================================================
# 5. signal_carried_check and the re-check status (screening_pipeline)
# =============================================================================
class TestSignalCarriedRecheck(unittest.TestCase):

    def setUp(self):
        t267.TestSymbolRecheckPipeline.setUp(self)  # mocked radar steps, temp ban file
        self.mocks["analyze_single_symbol"].side_effect = lambda s, i: None  # the live signal is gone
        self.now = int(time.time())
        self.evaluated = self.now - 600
        self.open_ts = signal_open(self.evaluated - 60)
        self.plan = {"entry": 1.004, "stop_loss": 0.974, "tp1": 1.058, "tp2": 1.124, "evaluated_ts": self.evaluated,
                     "signal_candle_open_ts": self.open_ts, "signal_interval": "15m", "recheck_max_drift_r": 0.25,
                     "recheck_max_age_seconds": 1800}
        self.klines = fake_klines(self.open_ts, self.now)
        self.price = "1.001"
        for target, kw in (("broad_market_radar.fetch_klines", {"side_effect": lambda *a, **k: self.klines}),
                           ("microstructure_engine.fetch_json", {"side_effect": lambda url: {"price": self.price}})):
            p = patch(target, **kw)
            self.mocks[target.rsplit(".", 1)[1]] = p.start()
            self.addCleanup(p.stop)

    def run_recheck(self, plan=None, direction=DIRECTION, symbol=SYMBOL):
        with patch("sys.stderr", io.StringIO()):
            return sp.execute_symbol_recheck(symbol, direction, target_env="prod",
                                             plan=self.plan if plan is None else plan).recheck

    def failed(self, rc):
        return {c["check"] for c in rc["carried"]["checks"] if not c["ok"]}

    def test_signal_vanished_but_levels_valid_is_carried(self):
        rc = self.run_recheck()
        self.assertEqual(rc["setup_status"], "signal_carried", rc)
        self.assertTrue(rc["carried"]["ok"])
        self.assertEqual([c["check"] for c in rc["carried"]["checks"]],
                         ["signal_candle", "sl_untouched", "live_price", "trigger_crossed_r", "rr_tp2",
                          "alt_short_macro"])
        self.assertEqual((rc["carried"]["signal_candle_open_ts"], rc["carried"]["signal_interval"]),
                         (self.open_ts, "15m"))
        rr = next(c for c in rc["carried"]["checks"] if c["check"] == "rr_tp2")
        self.assertEqual(rr["value"], 4.0)  # from the trigger: not crossed, the executor enters there
        self.mocks["enrich_and_size_candidate"].assert_not_called()  # no live candidate row

    def test_sl_touched_since_the_signal_candle_fails(self):
        self.klines = fake_klines(self.open_ts, self.now, touch=(0.973, 1.002))
        rc = self.run_recheck()
        self.assertEqual(rc["setup_status"], "no_setup")
        self.assertEqual(self.failed(rc), {"sl_untouched"})
        self.assertIn("signal not carried (failed: sl_untouched)", rc["cause"])

    def test_trigger_crossed_beyond_the_drift_bound_fails(self):
        plan = dict(self.plan, tp2=1.2)  # a far TP2 isolates the trigger check from the R:R one
        self.price = repr(1.004 + 0.26 * 0.03)
        rc = self.run_recheck(plan=plan)
        self.assertEqual(self.failed(rc), {"trigger_crossed_r"})
        self.price = repr(1.004 + 0.2 * 0.03)  # within 0.25R: still carried, R:R from the live price (market entry)
        rc = self.run_recheck(plan=plan)
        self.assertEqual(rc["setup_status"], "signal_carried", rc)
        rr = next(c for c in rc["carried"]["checks"] if c["check"] == "rr_tp2")
        live = 1.004 + 0.2 * 0.03
        self.assertEqual(rr["value"], round((1.2 - live) / (live - 0.974), 3))

    def test_live_rr_below_three_fails(self):
        rc = self.run_recheck(plan=dict(self.plan, tp2=1.08))  # 2.53R from the trigger
        self.assertEqual(self.failed(rc), {"rr_tp2"})
        self.price = repr(1.004 + 0.25 * 0.03)  # crossed: 3.0R from the trigger, 2.6R from the live price
        rc = self.run_recheck(plan=dict(self.plan, tp2=1.094))
        self.assertEqual(self.failed(rc), {"rr_tp2"})

    def short_plan(self):
        return dict(self.plan, entry=1.0, stop_loss=1.03, tp1=0.946, tp2=0.88)

    def test_alt_short_macro_is_re_evaluated_live_without_a_row(self):
        self.price = "1.001"
        self.macro.btc_tape_bias = "BULLISH_PRESSURE"
        # The signal candle's volume is 3.0x the 20 bars before it: climax >= 2.5x carries
        rc = self.run_recheck(plan=self.short_plan(), direction="SHORT")
        self.assertEqual(rc["setup_status"], "signal_carried", rc)
        macro_check = next(c for c in rc["carried"]["checks"] if c["check"] == "alt_short_macro")
        self.assertEqual(macro_check["value"], "climax>=2.5x")
        # 1.4x (the XPL case): no BTC rejection and no climax -> macro rejects, even with no radar row
        self.klines = fake_klines(self.open_ts, self.now, sig_vol=140.0)
        with patch("sys.stderr", io.StringIO()):
            payload = sp.execute_symbol_recheck(SYMBOL, "SHORT", target_env="prod", plan=self.short_plan())
        self.assertEqual(payload.recheck["setup_status"], "no_setup")
        self.assertIn("alt_short_macro", payload.recheck["cause"])
        self.assertEqual(payload.macro_rejected_shorts[0]["symbol"], SYMBOL)
        # BTC rejects resistance: carried without the climax
        self.macro.btc_absorption = "BEARISH_ABSORPTION"
        rc = self.run_recheck(plan=self.short_plan(), direction="SHORT")
        self.assertEqual(rc["setup_status"], "signal_carried", rc)
        # Alt shorts not allowed now (BTC squeeze or BTC data unavailable) -> fail
        for kw in ({"allows_alt_shorts": False, "btc_regime": "SHORT_SQUEEZE"}, {"btc_data_ok": False}):
            with self.subTest(kw=kw):
                for k, v in kw.items():
                    setattr(self.macro, k, v)
                rc = self.run_recheck(plan=self.short_plan(), direction="SHORT")
                self.assertEqual(self.failed(rc), {"alt_short_macro"})
        # Fewer than 20 bars before the signal candle: climax unknown (fail closed)
        self.macro.btc_absorption, self.macro.allows_alt_shorts = "NONE", True
        self.macro.btc_regime, self.macro.btc_data_ok = "NEUTRAL_CONSOLIDATION", True
        self.klines = fake_klines(self.open_ts, self.now, before=10)
        rc = self.run_recheck(plan=self.short_plan(), direction="SHORT")
        self.assertEqual(self.failed(rc), {"alt_short_macro"})

    def test_missing_or_unverifiable_inputs_fail_closed(self):
        cases = {
            "klines error": (lambda: setattr(self, "klines", None) or self.mocks["fetch_klines"].configure_mock(
                side_effect=OSError("down")), {"signal_candle", "sl_untouched"}),
            "candle not in klines": (lambda: setattr(self, "klines", fake_klines(self.open_ts, self.now,
                                                                                 drop_signal=True)),
                                     {"signal_candle", "sl_untouched"}),
            "no live price": (lambda: setattr(self, "price", None), {"live_price", "trigger_crossed_r", "rr_tp2"}),
            "bad live price": (lambda: setattr(self, "price", "nan"), {"live_price", "trigger_crossed_r", "rr_tp2"}),
            "ticker error": (lambda: self.mocks["fetch_json"].configure_mock(side_effect=ValueError("x")),
                             {"live_price", "trigger_crossed_r", "rr_tp2"}),
        }
        for label, (arrange, expected) in cases.items():
            with self.subTest(label):
                self.klines = fake_klines(self.open_ts, self.now)
                self.price = "1.001"
                self.mocks["fetch_klines"].configure_mock(side_effect=lambda *a, **k: self.klines)
                self.mocks["fetch_json"].configure_mock(side_effect=lambda url: {"price": self.price})
                arrange()
                rc = self.run_recheck()
                self.assertEqual(rc["setup_status"], "no_setup", rc)
                self.assertEqual(self.failed(rc), expected)
        # The signal candle still forming (no bar after it)
        self.mocks["fetch_klines"].configure_mock(side_effect=lambda *a, **k: self.klines)
        self.mocks["fetch_json"].configure_mock(side_effect=lambda url: {"price": "1.001"})
        self.klines = fake_klines(self.open_ts, self.open_ts)
        rc = self.run_recheck()
        self.assertIn("signal_candle", self.failed(rc))

    def test_rate_limit_during_the_carry_is_unavailable(self):
        def banned(*a, **k):
            rlg.trip(429)
            raise rlg.RateLimitedError("429")
        self.mocks["fetch_klines"].configure_mock(side_effect=banned)
        rc = self.run_recheck()
        self.assertEqual(rc["setup_status"], "unavailable")
        self.assertNotIn("carried", rc)

    def test_no_carry_outside_the_window_without_a_signal_or_on_another_interval(self):
        for plan in (dict(self.plan, evaluated_ts=self.now - 1801), dict(self.plan, evaluated_ts=self.now + 60),
                     {k: v for k, v in self.plan.items() if k != "signal_candle_open_ts"},
                     dict(self.plan, signal_candle_open_ts=None), dict(self.plan, signal_interval="1h"),
                     dict(self.plan, signal_candle_open_ts=float(self.open_ts)),
                     dict(self.plan, recheck_max_age_seconds=None), {}):
            with self.subTest(plan=plan):
                rc = self.run_recheck(plan=plan)
                self.assertEqual(rc, {"symbol": SYMBOL, "direction": DIRECTION, "setup_status": "no_setup",
                                      "cause": rc["cause"]})
                self.assertTrue(rc["cause"].startswith("radar: no setup for ETHFIUSDT"))
                self.assertNotIn("signal not carried", rc["cause"])
        self.mocks["fetch_klines"].assert_not_called()

    def test_direction_flip_and_found_keep_todays_behaviour(self):
        row = dict(self.row, direction="SHORT")
        self.mocks["analyze_single_symbol"].side_effect = lambda s, i: dict(row)
        rc = self.run_recheck()
        self.assertEqual(rc, {"symbol": SYMBOL, "direction": DIRECTION, "setup_status": "no_setup",
                              "cause": "radar: ETHFIUSDT now scores SHORT, not LONG"})
        self.mocks["analyze_single_symbol"].side_effect = lambda s, i: dict(self.row)
        rc = self.run_recheck()
        self.assertEqual(rc, {"symbol": SYMBOL, "direction": DIRECTION, "setup_status": "found", "cause": None})
        self.mocks["fetch_klines"].assert_not_called()


# =============================================================================
# 6. radar_tier at the radar boundary; Tier S still sorts first; divergence counts
# =============================================================================
class TestRadarTierBoundary(unittest.TestCase):

    def test_public_payload_and_text_say_radar_tier(self):
        row = {"symbol": "XPLUSDT", "direction": "SHORT", "confidence": 80, "tier": "Tier S (🔥 Top Score, x)",
               "tier_code": "S", "price": 0.0867, "trigger": 0.0866, "sl": 0.0878, "tp1": 0.085, "tp2": 0.082,
               "rr": 4.0, "risk_pct": 1.4, "reasons": ["r"]}
        with patch.object(bmr, "_standard_leverage", return_value=3):
            payload = bmr.build_scan_payload([row], "prod", "15m", 1, 0, 1)
        cand = payload["candidates"][0]
        self.assertEqual((cand["radar_tier"], cand["radar_tier_code"]), (row["tier"], "S"))
        self.assertNotIn("tier", cand)
        self.assertNotIn("tier_code", cand)
        self.assertEqual(row["tier"], "Tier S (🔥 Top Score, x)")  # the internal row the pipeline reads is intact
        out = io.StringIO()
        with redirect_stdout(out):
            bmr.print_text_report(payload)
        self.assertIn("• Radar Tier S (🔥 Top Score, x) (pre-gate) | XPLUSDT (SHORT)", out.getvalue())


class TestPipelineTierS(unittest.TestCase):
    """The real enrich_and_size_candidate on internal radar rows (sizing faked), so a silent rename of the internal
    `tier` key would turn every row into Tier A and fail here."""

    def setUp(self):
        self.macro = macro()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rlg.reset_for_tests()
        self.addCleanup(rlg.reset_for_tests)
        self.rows = []
        patches = [
            patch("screening_pipeline.fetch_macro_btc", side_effect=lambda: self.macro),
            patch("broad_market_radar.scan_all_liquid_pairs", side_effect=lambda *a, **k: [dict(r) for r in self.rows]),
            patch("quant_risk_engine.scan_coingrated_market_pairs", return_value=[]),
            patch("funding_arbitrage.scan_top_funding_opportunities", return_value=[]),
            patch("screening_pipeline.fetch_news_summary", return_value=[]),
            patch("sync_session_state.sync_session_state", return_value={"portfolio_exposure": {},
                                                                         "active_positions": []}),
            patch("utils.yolo_scan_health.HEALTH_FILE", os.path.join(tmp.name, "yolo_scan_health.json")),
            patch("utils.rate_limit_guard.STATE_FILE", os.path.join(tmp.name, "market_data_rate_limit.json")),
            patch("quant_risk_engine.get_account_equity", return_value=386.0),
            patch("execute_futures_trade.get_symbol_filters",
                  return_value={"stepSize": 1.0, "minQty": 1.0, "tickSize": 0.00000001, "precision_qty": 0,
                                "precision_price": 8, "minNotional": 5.0}),
            patch("user_profile.load_user_profile",
                  return_value={"risk_pct_equity": 0.005, "leverage_standard": 3, "max_margin_ratio": 0.30,
                                "yolo_slot_enabled": False}),
            patch("microstructure_engine.get_live_aggtrades_tape", return_value={"live_bias": "BALANCED"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def row(symbol, conf, code, direction="LONG", **kw):
        label = {"S": "Tier S (🔥 Top Score, order flow confirmed)", "A+": "Tier A+ (High Confirmed Score)",
                 "A": "Tier A (Strong Confluence / Hedge)"}[code]
        base = (dict(price=0.002509, trigger=0.00252326, sl=0.0024084, tp1=0.0027, tp2=0.0029) if direction == "LONG"
                else dict(price=0.002509, trigger=0.0025, sl=0.0026, tp1=0.0024, tp2=0.0021))
        return dict(base, symbol=symbol, direction=direction, confidence=conf, tier=label, tier_code=code,
                    interval="15m", wick_candle_open_time=1791640800000, alt_short_climax_ok=False, **kw)

    def run_pipeline(self):
        with redirect_stderr(io.StringIO()):
            return sp.execute_screening_pipeline(target_env="prod", include_yolo=False)

    def test_tier_s_rows_sort_first_and_divergence_is_counted(self):
        self.rows = [self.row("AUSDT", 64, "A"), self.row("SUSDT", 82, "S"), self.row("PUSDT", 79, "A+"),
                     self.row("XSHORTUSDT", 90, "S", direction="SHORT"), self.row("S2USDT", 80, "S")]
        payload = self.run_pipeline()
        self.assertEqual([(c.symbol, c.tier_code) for c in payload.top_candidates],
                         [("SUSDT", "S"), ("S2USDT", "S"), ("PUSDT", "A+"), ("AUSDT", "A")])
        self.assertTrue(payload.top_candidates[0].tier.startswith("Tier S"))
        self.assertEqual(payload.top_candidates[0].signal_candle_open_ts, 1791640800)
        # 3 radar Tier S rows (the alt SHORT among them, dropped by the macro gate) vs 2 in the brief
        self.assertEqual(payload.tier_s_divergence, {"radar_tier_s": 3, "brief_tier_s": 2})
        self.assertEqual([r["symbol"] for r in payload.macro_rejected_shorts], ["XSHORTUSDT"])

    def test_divergence_reaches_the_sidecar_top_level_only(self):
        self.rows = [self.row("SUSDT", 82, "S"), self.row("AUSDT", 64, "A")]
        screening = json.loads(self.run_pipeline().model_dump_json())
        brief = t271.BriefCase.assemble(self, screening)
        with open(os.path.join(os.path.dirname(self.brief_file), "primed_brief_scores.json"), encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["tier_s_divergence"], {"radar_tier_s": 1, "brief_tier_s": 1})
        self.assertNotIn("tier_s_divergence", json.dumps(brief))
        self.assertEqual(sidecar["rows"][0]["signal_candle_open_ts"], 1791640800)
        # build_radar_snapshots reads only the rows: unaffected by the new top-level key
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = tmp.name
        os.makedirs(os.path.join(workspace, "logs"))
        with open(os.path.join(workspace, "logs", "primed_brief_scores.json"), "w", encoding="utf-8") as f:
            json.dump(sidecar, f)
        record = {"status": "APPROVED", "timestamp_ts": sidecar["generated_at_ts"] + 5, "target_env": "prod",
                  "approved_candidates": [{"symbol": "SUSDT", "direction": "LONG"}]}
        snap = rec.build_radar_snapshots(record, workspace)["SUSDT|LONG"]
        self.assertEqual((snap["radar_snapshot"]["confidence"], snap["radar_snapshot_reason"]), (82, None))

    def test_rate_limited_run_has_no_divergence(self):
        self.rows = [self.row("SUSDT", 82, "S")]
        with patch("screening_pipeline.fetch_macro_btc", side_effect=rlg.RateLimitedError("429")):
            payload = self.run_pipeline()
        self.assertIsNone(payload.tier_s_divergence)


if __name__ == "__main__":
    unittest.main()
