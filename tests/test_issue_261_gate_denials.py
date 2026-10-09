#!/usr/bin/env python3
"""
test_issue_261_gate_denials.py - shadow desk: register the hook's delta-gate denials of approved candidates
(DELTA_GATE_POST_APPROVAL, issue #261, deferred from #251).

Covers the hook's record-only append to logs/gate_denials.jsonl (one event per denial with dossier prices, sha, cached
book and source; nothing for allowed trades, non-delta denials, --bypass-delta-gate or a missing candidate; the
decision and reason byte-identical when the recorder raises or logs/ cannot be written; bounded size; best-effort
dedupe), the file's ground-truth protection, the doctor self-test (writes nothing), the tracker intake
(register_from_gate_denials: gate, source, blockers, book, original registered_at_ts, idempotency, missing / garbled
file, runs without filtered_opportunities), book_from_sources vs snapshot_book, and the analytics (by_gate, replay,
caveat). Hermetic: temp workspaces only (every shadow_tracker path redirected), no Binance client, network blocked.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (SCRIPTS_DIR, HOOKS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_guard_bypasses as tgb  # noqa: E402  (fixtures only; imported first, see test_issue_79)
import pre_trade_guard  # noqa: E402
import shadow_tracker as st  # noqa: E402
import shadow_analytics as sa  # noqa: E402
import test_issue_251_delta_gate_regret as t251  # noqa: E402  (fixtures only)

SCRIPT = "python3 scripts/execute_futures_trade.py"
SHA = "b" * 64
GATE = "DELTA_GATE_POST_APPROVAL"
DELTA_REASON = "unbalanced (Delta: "   # the LONG_HEAVY / SHORT_HEAVY denial, not the UNKNOWN-exposure one


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    tgb.setUpModule()
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def read_events(ws):
    path = os.path.join(ws, "logs", "gate_denials.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# =============================================================================
# Hook: evaluate_trade_opening with the dossier check patched (cand carries the dossier prices)
# =============================================================================
class TestHookRecordsDeltaDenial(unittest.TestCase):

    PROFILE = {"autonomous_execution_tier_s": True, "max_open_positions": 5, "leverage_standard": 3,
               "leverage_yolo": 15, "leverage_ceiling": 15, "yolo_slot_enabled": False}

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws, True)
        os.makedirs(os.path.join(self.ws, "logs"))
        self.now = int(time.time())

    def ws_copy(self):
        """A second workspace with the same state / registry (for byte-identical comparisons)."""
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        os.makedirs(os.path.join(other, "logs"))
        for name in ("session_state.json", "pending_entries.json"):
            src = os.path.join(self.ws, "logs", name)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(other, "logs", name))
        return other

    def state(self, bias="LONG_HEAVY", env="prod", net=150.0, positions=None, ws=None, **portfolio):
        positions = positions if positions is not None else [
            {"symbol": "SUIUSDT", "direction": "LONG", "notional_usdt": 120.0, "entry_order_id": 9001,
             "entry_time_ts": self.now - 3600, "unrealized_pnl": 1.0}]
        exposure = dict({"total_active_positions": len(positions), "delta_bias": bias,
                         "delta_bias_incl_resting": bias, "net_notional_delta_usdt": net}, **portfolio)
        with open(os.path.join(ws or self.ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
            json.dump({"is_valid": True, "last_updated_ts": self.now - 30, "target_env": env,
                       "active_positions": positions, "portfolio_exposure": exposure}, f)

    def registry(self, text=None):
        entries = {
            "prod:ZROUSDT:111": {"kind": "STOP_MARKET", "entry_id": "111", "symbol": "ZROUSDT", "direction": "LONG",
                                 "target_env": "prod", "trigger_or_limit_price": 2.0, "total_qty": 50.0,
                                 "placed_at_ts": self.now - 120, "sl_price": 1.9,
                                 "score_meta": {"score": 80, "dossier_score": 80, "tier": "Tier S"}},
            "testnet:ARBUSDT:333": {"kind": "LIMIT", "entry_id": "333", "symbol": "ARBUSDT", "direction": "LONG",
                                    "target_env": "testnet", "trigger_or_limit_price": 1.0, "total_qty": 10.0,
                                    "placed_at_ts": self.now},
        }
        with open(os.path.join(self.ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
            f.write(text if text is not None else json.dumps({"entries": entries}))

    @staticmethod
    def cand(direction="LONG", sha=SHA, **extra):
        c = {"symbol": "FETUSDT", "direction": direction, "requires_user_confirmation": False, "tier": "Tier S",
             "score": 85, "dossier_sha256": sha, "is_yolo": False, "leverage": 3,
             "entry": 1.0, "stop_loss": 0.95 if direction == "LONG" else 1.05,
             "tp1": 1.09 if direction == "LONG" else 0.91, "tp2": 1.2 if direction == "LONG" else 0.8}
        c.update(extra)
        return c

    def hook(self, direction="LONG", env="prod", flags="", cand="default", ws=None, **profile):
        cmd = (f"{SCRIPT} --symbol FETUSDT --direction {direction} --leverage 3 --sl-price 0.5 --env {env} "
               f"{flags}").strip()
        cand = self.cand(direction) if cand == "default" else cand
        with patch("user_profile.load_user_profile", return_value=dict(self.PROFILE, **profile)), \
             patch("pre_trade_guard.check_dossier", return_value=(True, "ok", cand)), \
             patch("pre_trade_guard._tier_s_calibration_message", return_value=None):
            return pre_trade_guard.evaluate_trade_opening(cmd, {"CommandLine": cmd}, {}, ws or self.ws, None)

    def test_delta_denial_writes_exactly_one_event(self):
        self.state()
        self.registry()
        decision, reason = self.hook()
        self.assertEqual(decision, "deny")
        self.assertIn(DELTA_REASON, reason)
        events = read_events(self.ws)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual((ev["gate"], ev["symbol"], ev["direction"], ev["env"]), (GATE, "FETUSDT", "LONG", "prod"))
        self.assertEqual((ev["dossier_sha256"], ev["score"], ev["tier"], ev["is_yolo"]), (SHA, 85, "Tier S", False))
        # dossier prices, not the CLI --sl-price
        self.assertEqual((ev["entry"], ev["stop_loss"], ev["tp1"], ev["tp2"]), (1.0, 0.95, 1.09, 1.2))
        self.assertEqual((ev["source"], ev["session_state_ts"], ev["age_seconds"]),
                         ("session_state_cache", self.now - 30, ev["ts"] - (self.now - 30)))
        self.assertEqual((ev["delta_bias"], ev["net_notional_delta_usdt"]), ("LONG_HEAVY", 150.0))
        self.assertAlmostEqual(ev["ts"], time.time(), delta=5)
        self.assertEqual((ev["book_truncated"], ev["book_error"]), (False, None))
        by_sym = {i["symbol"]: i for i in ev["book"]}
        self.assertEqual(set(by_sym), {"SUIUSDT", "ZROUSDT"})   # the TESTNET record is filtered
        self.assertEqual(by_sym["SUIUSDT"], {"symbol": "SUIUSDT", "direction": "LONG", "notional_usdt": 120.0,
                                             "entry_order_id": 9001, "entry_time_ts": self.now - 3600,
                                             "kind": "position"})
        self.assertEqual(by_sym["ZROUSDT"]["kind"], "resting")
        self.assertEqual(by_sym["ZROUSDT"]["score_meta"], {"score": 80, "dossier_score": 80})
        self.assertEqual((by_sym["ZROUSDT"]["trigger_or_limit_price"], by_sym["ZROUSDT"]["total_qty"],
                          by_sym["ZROUSDT"]["entry_id"]), (2.0, 50.0, "111"))

    def test_shadow_tracker_import_error_is_irrelevant(self):
        """Many tests set sys.modules["shadow_tracker"] = None: the hook never imports it (stdlib-only recorder)."""
        self.state()
        normal = self.hook(ws=self.ws_copy())
        with patch.dict(sys.modules, {"shadow_tracker": None}):
            res = self.hook()
        self.assertEqual(res, normal)
        self.assertEqual([e["gate"] for e in read_events(self.ws)], [GATE])

    def test_short_heavy_denial_is_recorded(self):
        self.state(bias="SHORT_HEAVY", net=-200.0, positions=[])
        decision, reason = self.hook("SHORT")
        self.assertEqual(decision, "deny")
        self.assertIn(DELTA_REASON, reason)
        ev = read_events(self.ws)
        self.assertEqual([(e["direction"], e["delta_bias"], e["book"]) for e in ev], [("SHORT", "SHORT_HEAVY", [])])

    def test_repeated_denial_of_the_same_dossier_is_written_once(self):
        """The hook can judge one tool call several times (wsl.exe, PowerShell) and agents retry: best-effort
        pre-check on (dossier_sha256, symbol, direction); a new dossier is a new event."""
        self.state()
        for _ in range(3):
            self.assertEqual(self.hook()[0], "deny")
        self.assertEqual(len(read_events(self.ws)), 1)
        self.assertEqual(self.hook(cand=self.cand(sha="c" * 64))[0], "deny")
        self.assertEqual([e["dossier_sha256"] for e in read_events(self.ws)], [SHA, "c" * 64])

    def test_decision_byte_identical_when_the_recorder_fails(self):
        self.state()
        self.registry()
        with patch("pre_trade_guard._record_gate_denial", return_value=None):
            baseline = self.hook(ws=self.ws_copy())   # recorder inert: the pre-#261 decision
        normal = self.hook()
        with patch("pre_trade_guard._record_gate_denial", side_effect=RuntimeError("recorder exploded")):
            raised = self.hook(ws=self.ws_copy())
        unwritable = self.ws_copy()
        os.makedirs(os.path.join(unwritable, "logs", "gate_denials.jsonl"))   # open(..., "a") raises
        blocked = self.hook(ws=unwritable)
        self.assertEqual(baseline[0], "deny")
        for res in (normal, raised, blocked):
            self.assertEqual(res, baseline)
        self.assertEqual(len(read_events(self.ws)), 1)
        self.assertTrue(os.path.isdir(os.path.join(unwritable, "logs", "gate_denials.jsonl")))

    def test_no_candidate_writes_nothing(self):
        """TESTNET --bypass-eval-gate skips the dossier: cand stays None (no unbound name), no approved candidate,
        no event; the decision is unchanged."""
        self.state(env="testnet")
        decision, reason = self.hook(env="testnet", flags="--bypass-eval-gate")
        self.assertEqual(decision, "deny")
        self.assertIn(DELTA_REASON, reason)
        self.assertEqual(read_events(self.ws), [])
        with patch("pre_trade_guard._record_gate_denial", side_effect=RuntimeError("boom")):
            self.assertEqual(self.hook(env="testnet", flags="--bypass-eval-gate"), (decision, reason))
        pre_trade_guard._record_gate_denial(self.ws, "prod", self.now, "FETUSDT", "LONG", None, {}, "LONG_HEAVY", 0)
        self.assertEqual(read_events(self.ws), [])

    def test_testnet_denial_is_recorded_with_env(self):
        self.state(env="testnet")
        self.registry()
        self.assertEqual(self.hook(env="testnet")[0], "deny")
        ev = read_events(self.ws)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["env"], "testnet")
        self.assertEqual({i["symbol"] for i in ev[0]["book"] if i["kind"] == "resting"}, {"ARBUSDT"})

    def test_malformed_registry_in_testnet_records_the_error(self):
        self.state(env="testnet")
        self.registry("{broken")
        self.assertEqual(self.hook(env="testnet")[0], "deny")
        ev = read_events(self.ws)
        self.assertEqual(len(ev), 1)
        self.assertIn("JSONDecodeError", ev[0]["book_error"])
        self.assertEqual([i["kind"] for i in ev[0]["book"]], ["position"])

    def test_allowed_trade_writes_nothing(self):
        self.state(bias="DELTA_BALANCED")
        self.assertEqual(self.hook()[0], "allow")
        self.state(bias="LONG_HEAVY")
        self.assertEqual(self.hook("SHORT")[0], "allow")   # a hedge is not denied
        self.assertEqual(read_events(self.ws), [])

    def test_non_delta_denials_write_nothing(self):
        cases = {
            "max open positions": lambda: self.state(positions=[{"symbol": s, "direction": "LONG"} for s in
                                                                ("A", "B", "C", "D", "E")]),
            "stale state": lambda: (self.state(), self._age_state(1000)),
            "invalid state": lambda: self._write_raw_state({"is_valid": False, "error": "sync failed"}),
            "unknown resting exposure": lambda: (self.state(bias="DELTA_BALANCED", delta_bias_incl_resting="UNKNOWN"),
                                                 self.registry()),
        }
        for label, setup in cases.items():
            with self.subTest(case=label):
                for name in ("session_state.json", "pending_entries.json"):
                    path = os.path.join(self.ws, "logs", name)
                    if os.path.exists(path):
                        os.remove(path)
                setup()
                decision, reason = self.hook()
                self.assertEqual(decision, "deny", label)
                self.assertNotIn("Portfolio is bullishly unbalanced", reason)
                self.assertEqual(read_events(self.ws), [], label)

    def _age_state(self, seconds):
        path = os.path.join(self.ws, "logs", "session_state.json")
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state["last_updated_ts"] = self.now - seconds
        self._write_raw_state(state)

    def _write_raw_state(self, state):
        with open(os.path.join(self.ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)

    def test_bypass_delta_gate_writes_nothing(self):
        self.state(env="testnet")
        self.assertEqual(self.hook(env="testnet", flags="--bypass-delta-gate")[0], "allow")
        self.assertEqual(read_events(self.ws), [])

    def test_oversized_book_is_dropped_and_flagged(self):
        positions = [{"symbol": f"COIN{i:03d}USDT", "direction": "LONG", "notional_usdt": 10.0 + i,
                      "entry_order_id": 10 ** 12 + i, "entry_time_ts": self.now - i} for i in range(200)]
        self.state(positions=positions)
        decision, reason = self.hook(max_open_positions=1000)
        self.assertEqual(decision, "deny")
        self.assertIn(DELTA_REASON, reason)
        path =os.path.join(self.ws, "logs", "gate_denials.jsonl")
        with open(path, "rb") as f:
            raw = f.read()
        self.assertLessEqual(len(raw), pre_trade_guard.GATE_DENIAL_MAX_BYTES)
        self.assertEqual(raw.count(b"\n"), 1)
        ev = json.loads(raw)
        self.assertEqual((ev["book"], ev["book_truncated"], ev["dossier_sha256"]), ([], True, SHA))


# =============================================================================
# Hook end to end (provenance-verified dossier) + ground truth + doctor self-test
# =============================================================================
class TestGuardEndToEnd(tgb.GuardHarness):

    PRICES = {"entry": 100.0, "stop_loss": 95.0, "tp1": 109.0, "tp2": 120.0}

    def deploy(self):
        return self.agy(self.cmd(f"{SCRIPT} --symbol BTCUSDT --direction LONG --leverage 3 --env prod",
                                 conversationId=tgb.PARENT_CONV_ID))

    def test_prod_delta_denial_records_the_provenance_sha(self):
        record = self.write_provenance_dossier(extra=self.PRICES)
        self.write_session_state("LONG_HEAVY")
        res = self.deploy()
        self.assertDenied(res, DELTA_REASON)
        ev = read_events(self.root)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["dossier_sha256"], record["provenance"]["sha256"])
        self.assertEqual((ev[0]["symbol"], ev[0]["direction"], ev[0]["entry"], ev[0]["stop_loss"]),
                         ("BTCUSDT", "LONG", 100.0, 95.0))
        # the same denial without the recorder: identical decision and reason
        with patch("pre_trade_guard._record_gate_denial", side_effect=RuntimeError("boom")):
            again = self.deploy()
        self.assertEqual((again.get("decision"), again.get("reason")), (res.get("decision"), res.get("reason")))

    def test_allowed_trade_writes_nothing(self):
        self.write_provenance_dossier(extra=self.PRICES)
        self.assertEqual(self.deploy().get("decision"), "allow")
        self.assertEqual(read_events(self.root), [])

    def test_gate_denials_log_is_ground_truth(self):
        self.assertIn("logs/gate_denials.jsonl", pre_trade_guard.GROUND_TRUTH_FILES)
        for c in ("echo '{}' >> logs/gate_denials.jsonl", "cp /tmp/forged.jsonl logs/gate_denials.jsonl",
                  "rm logs/gate_denials.jsonl", "sed -i 's/x/y/' logs/gate_denials.jsonl",
                  "python3 -c \"open('logs/gate_denials.jsonl', 'a').write('{}')\""):
            with self.subTest(command=c):
                res = self.agy(self.cmd(c))
                self.assertDenied(res, "Ground Truth Protection")
                self.assertIn("logs/gate_denials.jsonl may only be written by", res["reason"])
                self.assertIn("scripts/hooks/pre_trade_guard.py", res["reason"])
        self.assertDenied(self.agy({"toolCall": {"name": "write_to_file", "args": {
            "TargetFile": "logs/gate_denials.jsonl", "CodeContent": "{}"}}}))
        for c in ("cat logs/gate_denials.jsonl", "tail -n 5 logs/gate_denials.jsonl", "wc -l logs/gate_denials.jsonl",
                  "python3 scripts/shadow_tracker.py --register-from-eval"):
            with self.subTest(command=c):
                self.assertNotEqual(self.agy(self.cmd(c)).get("decision"), "deny")

    def test_doctor_self_test_writes_nothing(self):
        import trading_doctor
        self.write_provenance_dossier(extra=self.PRICES)
        self.write_session_state("LONG_HEAVY")
        payload = dict(trading_doctor.SYNTHETIC_NEW_ORDER_PAYLOAD, workspacePaths=[self.root])
        self.assertDenied(self.agy(payload))
        self.assertEqual(read_events(self.root), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs", "gate_denials.jsonl")))


# =============================================================================
# Tracker intake
# =============================================================================
class TestTrackerIntake(t251.TrackerBase):

    def event(self, ts=None, sha=SHA, symbol="FETUSDT", direction="LONG", **extra):
        ts = int(time.time()) - 600 if ts is None else ts
        ev = {"ts": ts, "env": "prod", "gate": GATE, "symbol": symbol, "direction": direction, "score": 85,
              "tier": "Tier S", "dossier_sha256": sha, "entry": 10.0, "stop_loss": 9.5, "tp1": 10.9, "tp2": 12.0,
              "is_yolo": False, "source": "session_state_cache", "session_state_ts": ts - 30, "age_seconds": 30,
              "delta_bias": "LONG_HEAVY", "net_notional_delta_usdt": 150.0, "book_truncated": False,
              "book_error": None,
              "book": [{"symbol": "SUIUSDT", "direction": "LONG", "notional_usdt": 40.0, "entry_order_id": 9001,
                        "entry_time_ts": ts - 3600, "kind": "position"},
                       {"symbol": "OPUSDT", "direction": "SHORT", "notional_usdt": 30.0, "entry_order_id": 9003,
                        "entry_time_ts": ts - 3600, "kind": "position"},
                       {"symbol": "ZROUSDT", "direction": "LONG", "entry_id": "111", "trigger_or_limit_price": 2.0,
                        "total_qty": 50.0, "placed_at_ts": ts - 120, "target_env": "prod", "kind": "resting",
                        "score_meta": {"score": 80, "dossier_score": 80}},
                       {"symbol": "SUIUSDT", "direction": "LONG", "entry_id": "444", "trigger_or_limit_price": 1.0,
                        "total_qty": 10.0, "placed_at_ts": ts, "target_env": "prod", "kind": "resting",
                        "score_meta": {"score": None, "dossier_score": None}}]}
        ev.update(extra)
        return ev

    def write_events(self, events, raw_lines=()):
        with open(st.GATE_DENIALS_FILE, "w", encoding="utf-8") as f:
            for line in raw_lines:
                f.write(line + "\n")
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    def test_registers_row_with_gate_source_blockers_book_and_original_time(self):
        ev = self.event()
        self.write_events([ev])
        t251.write_jsonl(st.TRADES_AUDIT_FILE, [
            {"symbol": "SUIUSDT", "direction": "LONG", "total_qty": 10, "timestamp": ev["ts"] - 3600, "score": 72},
            {"symbol": "SUIUSDT", "direction": "LONG", "total_qty": 5, "timestamp": ev["ts"] + 60, "score": 99},
        ])
        self.assertEqual(st.register_from_gate_denials(), 1)
        row = self.rows()["FETUSDT"]
        self.assertEqual((row["gate"], row["gate_source"], row["rejection_category"]), (GATE, "hook_denial", GATE))
        self.assertEqual((row["registered_at_ts"], row["last_checked_ts"]), (ev["ts"], ev["ts"]))
        self.assertEqual(row["id"], f"shadow_FETUSDT_{ev['ts']}")
        self.assertEqual((row["trigger_price"], row["current_price_at_eval"], row["sl_price"], row["tp1_price"],
                          row["tp2_price"]), (10.0, 10.0, 9.5, 10.9, 12.0))
        self.assertEqual((row["score"], row["dossier_sha256"], row["status"]), (85.0, SHA, "PENDING_TRIGGER"))
        self.assertEqual((row["book_source"], row["book_session_state_ts"], row["delta_bias_at_denial"],
                          row["net_notional_delta_usdt_at_denial"], row["gate_event_env"]),
                         ("session_state_cache", ev["ts"] - 30, "LONG_HEAVY", 150.0, "prod"))
        self.assertIn("LONG_HEAVY", row["gate_detail"])
        # blockers: same direction; SUIUSDT counts once, as the position, scored from the audit record not newer
        # than the denial (72, not the later 99); the SHORT position is in the book only
        by_sym = {b["symbol"]: b for b in row["blockers"]}
        self.assertEqual(set(by_sym), {"SUIUSDT", "ZROUSDT"})
        self.assertEqual((by_sym["SUIUSDT"]["kind"], by_sym["SUIUSDT"]["score"], by_sym["SUIUSDT"]["audit_ts"],
                          by_sym["SUIUSDT"]["entry_id"]), ("position", 72.0, float(ev["ts"] - 3600), "9001"))
        self.assertEqual((by_sym["ZROUSDT"]["kind"], by_sym["ZROUSDT"]["notional"], by_sym["ZROUSDT"]["score"]),
                         ("resting", 100.0, 80.0))
        self.assertEqual({(i["symbol"], i["kind"]) for i in row["book"]},
                         {("SUIUSDT", "position"), ("OPUSDT", "position"), ("ZROUSDT", "resting")})
        self.assertEqual(set(row["book"][0]), {"symbol", "direction", "kind", "score", "notional", "since_ts",
                                               "entry_id"})
        self.assertNotIn("blockers_error", row)

    def test_kline_audit_starts_at_the_denial_time(self):
        ev = self.event()
        self.write_events([ev])
        st.register_from_gate_denials()
        with patch.object(st, "fetch_klines", return_value=[]) as klines:
            st.audit_shadow_trades()
        self.assertEqual(klines.call_args[0][1], (ev["ts"] - 300) * 1000)

    def test_idempotent(self):
        ev = self.event()
        self.write_events([ev, ev])   # the hook may append the same denial twice
        self.assertEqual(st.register_from_gate_denials(), 1)
        self.assertEqual(st.register_from_gate_denials(), 0)
        self.assertEqual(len(st.load_jsonl(st.SHADOW_TRADES_FILE)), 1)
        # still once after the row resolved and left shadow_trades.jsonl
        row = st.load_jsonl(st.SHADOW_TRADES_FILE)[0]
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, [dict(row, status="RESOLVED")])
        t251.write_jsonl(st.SHADOW_TRADES_FILE, [])
        self.assertEqual(st.register_from_gate_denials(), 0)
        self.assertEqual(st.load_jsonl(st.SHADOW_TRADES_FILE), [])
        # another dossier (new sha) or direction is a new row
        self.write_events([ev, self.event(sha="c" * 64), self.event(direction="SHORT", stop_loss=10.5, tp1=9.1,
                                                                    tp2=8.0)])
        self.assertEqual(st.register_from_gate_denials(), 2)

    def test_event_without_sha_keeps_the_symbol_window(self):
        self.write_events([self.event(sha=None)])
        self.assertEqual(st.register_from_gate_denials(), 1)
        self.assertEqual(st.register_from_gate_denials(), 0)

    def test_missing_file_registers_nothing(self):
        self.assertFalse(os.path.exists(st.GATE_DENIALS_FILE))
        self.assertEqual(st.register_from_gate_denials(), 0)

    def test_garbled_file_and_invalid_events_are_skipped(self):
        now = int(time.time())
        bad = [self.event(gate="DELTA_GATE"), self.event(entry=None), self.event(stop_loss="x"),
               self.event(tp2=0), self.event(direction="SIDEWAYS"), self.event(symbol=""),
               self.event(ts=now - 2 * 86400), self.event(ts=now + 3600), dict(self.event(), ts="soon"),
               self.event(book="not-a-list", symbol="OKUSDT", sha="d" * 64)]
        self.write_events(bad, raw_lines=["{broken", "[1, 2]", "null", "\"text\"", ""])
        with open(st.GATE_DENIALS_FILE, "ab") as f:
            f.write(b"\xff\xfe garbage bytes\n")
        self.assertEqual(st.register_from_gate_denials(), 1)
        rows = self.rows()
        self.assertEqual(set(rows), {"OKUSDT"})       # a malformed book still registers, with no blockers
        self.assertEqual((rows["OKUSDT"]["book"], rows["OKUSDT"]["blockers"]), ([], []))

    def test_truncated_or_errored_book_is_flagged(self):
        self.write_events([self.event(book=[], book_truncated=True, book_error="JSONDecodeError: x")])
        st.register_from_gate_denials()
        row = self.rows()["FETUSDT"]
        self.assertEqual((row["book_truncated"], row["blockers_error"], row["blockers"]),
                         (True, "JSONDecodeError: x", []))

    def test_only_the_tail_is_read(self):
        old = [self.event(symbol=f"OLD{i}USDT", sha=f"{i:064d}") for i in range(st.GATE_DENIAL_TAIL_LINES)]
        self.write_events([self.event(symbol="FIRSTUSDT", sha="e" * 64)] + old)
        with patch.object(st, "register_shadow_trade", return_value=None) as reg:
            st.register_from_gate_denials()
        self.assertNotIn("FIRSTUSDT", {c.kwargs["symbol"] for c in reg.call_args_list})
        self.assertEqual(reg.call_count, st.GATE_DENIAL_TAIL_LINES)

    def test_runs_from_register_from_evaluation_without_opportunities(self):
        self.write_events([self.event()])
        self.assertEqual(st.register_from_evaluation(), 1)      # no brief file at all
        self.write_events([self.event(sha="c" * 64)])
        self.brief([])                                           # brief without filtered_opportunities
        self.assertEqual(st.register_from_evaluation(), 1)
        self.write_events([self.event(sha="d" * 64, symbol="WLDUSDT")])
        self.brief([t251.opp("OPUSDT", "SHORT")])
        self.dossier([{"symbol": "OPUSDT", "direction": "SHORT", "score": 70, "gate": "MACRO_SHORT"}])
        self.assertEqual(st.register_from_evaluation(), 2)      # the hook event plus the dossier rejection
        self.assertEqual(self.rows()["OPUSDT"]["gate"], "MACRO_SHORT")

    def test_hook_event_round_trips_into_a_row(self):
        """The hook's own writer feeds the tracker (format contract between the two)."""
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        os.makedirs(os.path.join(ws, "logs"))
        now = int(time.time())
        with open(os.path.join(ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": {"prod:ZROUSDT:111": {
                "entry_id": "111", "symbol": "ZROUSDT", "direction": "LONG", "target_env": "prod",
                "trigger_or_limit_price": 2.0, "total_qty": 50.0, "placed_at_ts": now - 60,
                "score_meta": {"score": 80}}}}, f)
        state = {"last_updated_ts": now - 10, "portfolio_exposure": {"net_notional_delta_usdt": 140.0},
                 "active_positions": [{"symbol": "SUIUSDT", "direction": "LONG", "notional_usdt": 40.0,
                                       "entry_order_id": 9001, "entry_time_ts": now - 3600}]}
        cand = TestHookRecordsDeltaDenial.cand()
        pre_trade_guard._record_gate_denial(ws, "prod", now, "FETUSDT", "LONG", cand, state, "LONG_HEAVY", 10)
        with patch.object(st, "GATE_DENIALS_FILE", os.path.join(ws, "logs", "gate_denials.jsonl")):
            self.assertEqual(st.register_from_gate_denials(), 1)
        row = self.rows()["FETUSDT"]
        self.assertEqual((row["gate"], row["registered_at_ts"], row["trigger_price"], row["dossier_sha256"]),
                         (GATE, now, 1.0, SHA))
        self.assertEqual({(b["symbol"], b["kind"], b["notional"]) for b in row["blockers"]},
                         {("SUIUSDT", "position", 40.0), ("ZROUSDT", "resting", 100.0)})


class TestBookFromSources(t251.TrackerBase):
    """book_from_sources is snapshot_book's pure core: same items from the same sources."""

    def test_same_item_shape_as_snapshot_book(self):
        t251.TestBlockerSnapshot.write_book(self)
        snap = st.snapshot_book("PROD")
        with open(st.SESSION_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        with open(st.PENDING_ENTRIES_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        items = st.book_from_sources(state, entries, st._latest_audit_by_symbol(st.TRADES_AUDIT_FILE), "PROD")
        self.assertEqual(items, snap["items"])
        self.assertTrue(items)
        keys = {"symbol", "direction", "kind", "score", "entry_id", "notional", "since_ts", "audit_ts"}
        for item in items:
            self.assertEqual(set(item), keys)

    def test_registered_at_ts_defaults_to_now(self):
        row = st.register_shadow_trade("ABCUSDT", "LONG", 1.0, 0.9, 1.1, 1.2, 1.0)
        self.assertAlmostEqual(row["registered_at_ts"], time.time(), delta=5)
        old = st.register_shadow_trade("XYZUSDT", "LONG", 1.0, 0.9, 1.1, 1.2, 1.0, registered_at_ts=1700000000)
        self.assertEqual((old["registered_at_ts"], old["registered_at_utc"], old["id"]),
                         (1700000000, "2023-11-14 22:13:20 UTC", "shadow_XYZUSDT_1700000000"))


# =============================================================================
# Analytics
# =============================================================================
class TestAnalytics(unittest.TestCase):

    def test_new_gate_in_regret_by_gate(self):
        self.assertIn(GATE, sa.REGRET_GATES)
        rows = [t251.resolved_row("a", "d1", pnl=2.7, score=85, blockers=[t251.RESTING_EXPIRED]),
                t251.resolved_row("h", "d1", pnl=-1.5, classification="TRUE_NEGATIVE", score=85, gate=GATE,
                                  gate_source="hook_denial", blockers=[t251.POSITION])]
        rep = sa.regret_report(rows, t251.ACTIONS, t251.OUTCOMES, t251.AUDIT, resamples=50, seed=1)
        self.assertEqual({k: v["n"] for k, v in rep["by_gate"].items()}, {"DELTA_GATE": 1, GATE: 1})
        pairs = {p["row_id"]: p for p in rep["pairs"]}
        self.assertEqual((pairs["h"]["gate"], pairs["h"]["regret_r"]), (GATE, -1.5))   # -1R - 0.5R
        self.assertIn(sa.HOOK_DENIAL_NOTE, rep["warnings"])
        text = sa.format_delta_gate_report(rep, sa.replay_policies(rows, sa.build_blocker_index([], [], [])))
        self.assertIn(f"gate {GATE}", text)
        self.assertIn("session_state_cache", text)

    def test_replay_includes_hook_denials_as_their_own_events(self):
        zro = {"symbol": "ZROUSDT", "direction": "LONG", "kind": "resting", "score": 80.0, "entry_id": "z1",
               "notional": 100.0, "since_ts": 9400}
        short = {"symbol": "OPUSDT", "direction": "SHORT", "kind": "position", "score": None, "entry_id": "p1",
                 "notional": 20.0, "since_ts": 5000}
        hook_row = t251.resolved_row("f", "d1", pnl=2.7, score=95, blockers=[zro], book=[zro, short],
                                     notional_usdt=100.0, gate=GATE, gate_source="hook_denial",
                                     registered_at_ts=10600, resolved_at_ts=13000)
        index = sa.build_blocker_index([], [], [])
        alone = sa.replay_policies([hook_row], index, swap_margin=10)
        self.assertEqual((alone["n_events"], alone["n_rows"]), (1, 1))
        self.assertEqual(alone["policies"]["swap"]["placed"], 1)
        self.assertIn(sa.HOOK_DENIAL_NOTE, alone["warnings"])
        # the same dossier's DELTA_GATE row (earlier snapshot) stays a separate event
        dossier_row = t251.resolved_row("a", "d1", "TRUE_NEGATIVE", pnl=-1.5, score=85, blockers=[zro],
                                        book=[zro, short])
        both = sa.replay_policies([dossier_row, hook_row], index)
        self.assertEqual((both["n_events"], both["n_rows"]), (2, 2))
        self.assertEqual([round(s["ts"]) for s in both["policies"]["current"]["exposure"]["series"]], [10000, 10600])
        # a truncated hook book (empty or partial) is never replayed as if the book were that small
        truncated = dict(hook_row, id="t", book=[], book_truncated=True)
        res = sa.replay_policies([truncated], index)
        self.assertEqual((res["n_events"], res["skipped"]["no_book"]), (0, 1))
        self.assertEqual(res["policies"]["resting_after_n_min"]["placed"], 0)

    def test_back_compat_rows_unchanged(self):
        rows = t251.TestReplay().rows()
        res = sa.replay_policies(rows, t251.TestReplay().index(), resting_age_min=30, resting_weight=0.5,
                                 swap_margin=10)
        self.assertEqual((res["n_events"], res["n_rows"]), (2, 3))
        self.assertEqual({k: v["total_r"] for k, v in res["policies"].items()},
                         {"current": 0.5, "resting_after_n_min": -0.5, "resting_fraction": 0.5, "swap": 1.8})


if __name__ == "__main__":
    unittest.main()
