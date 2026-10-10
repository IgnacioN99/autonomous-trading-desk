#!/usr/bin/env python3
"""
test_issue_275_gate_denial_residuals.py - residuals of the hook's delta-gate denial log (issue #275, after #261).

Covers: notional_usdt / risk_usdt / notional_derived on the hook event (equity of the cached session state x the
profile's risk_pct_equity; omitted when either is missing), copied by the tracker into the shadow row and used by the
replay (legacy hook rows counted notional_derived_default); bounded hook reads (dedupe tail, oversized registry
skipped and flagged book_truncated); decision, reason and exit code byte-identical with a failing / oversized
recorder and the gate_denial_recorder_errors heartbeat counter (shown by the doctor); the fallback identity of
events without a sha (TESTNET) in the hook dedupe and the tracker idempotency, also after resolution; #251 rows with
blockers_error and an empty book skipped as no_book; --audit / --loop pick-up, one trades_audit.jsonl read per
intake call and the garbled-line count. Hermetic: temp workspaces only, no Binance client, network blocked.
"""

import io
import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import contextlib
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
import trading_doctor  # noqa: E402
from utils import shadow_common  # noqa: E402
import test_issue_251_delta_gate_regret as t251  # noqa: E402  (fixtures only)
import test_issue_261_gate_denials as t261  # noqa: E402  (fixtures only)

SCRIPT = "python3 scripts/execute_futures_trade.py"
SHA = "b" * 64
GATE = "DELTA_GATE_POST_APPROVAL"
DELTA_REASON = "unbalanced (Delta: "


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    tgb.setUpModule()
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


read_events = t261.read_events


class _ReadSpy:
    """File proxy recording the size argument of every read()."""

    def __init__(self, f, reads):
        self._f, self._reads = f, reads

    def read(self, n=-1):
        self._reads.append(n)
        return self._f.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()

    def __getattr__(self, name):
        return getattr(self._f, name)


def spy_open(reads, names=("gate_denials.jsonl", "pending_entries.json")):
    """An open() for pre_trade_guard that records the binary-mode reads of the named logs/ files (the recorder's;
    the gates' own text-mode reads are not recorded)."""
    def _open(path, mode="r", *args, **kwargs):
        f = open(path, mode, *args, **kwargs)
        name = os.path.basename(str(path))
        if "b" in mode and name in names:
            return _ReadSpy(f, reads.setdefault(name, []))
        return f
    return _open


# =============================================================================
# Hook: evaluate_trade_opening with the dossier check patched (as in #261)
# =============================================================================
class HookBase(unittest.TestCase):

    PROFILE = dict(t261.TestHookRecordsDeltaDenial.PROFILE)

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws, True)
        os.makedirs(os.path.join(self.ws, "logs"))
        self.now = int(time.time())
        pre_trade_guard._gate_denial_recorder_errors = 0
        self.addCleanup(setattr, pre_trade_guard, "_gate_denial_recorder_errors", 0)

    def state(self, env="prod", equity=None, positions=None, ws=None, bias="LONG_HEAVY"):
        positions = positions if positions is not None else [
            {"symbol": "SUIUSDT", "direction": "LONG", "notional_usdt": 120.0, "entry_order_id": 9001,
             "entry_time_ts": self.now - 3600}]
        state = {"is_valid": True, "last_updated_ts": self.now - 30, "target_env": env,
                 "active_positions": positions,
                 "portfolio_exposure": {"total_active_positions": len(positions), "delta_bias": bias,
                                        "delta_bias_incl_resting": bias, "net_notional_delta_usdt": 150.0}}
        if equity is not None:
            state["operating_balance"] = {"total_wallet_balance_usdt": equity}
        with open(os.path.join(ws or self.ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)
        return state

    def hook(self, direction="LONG", env="prod", cand="default", ws=None, **profile):
        cmd = f"{SCRIPT} --symbol FETUSDT --direction {direction} --leverage 3 --sl-price 0.5 --env {env}"
        cand = t261.TestHookRecordsDeltaDenial.cand(direction) if cand == "default" else cand
        with patch("user_profile.load_user_profile", return_value=dict(self.PROFILE, **profile)), \
             patch("pre_trade_guard.check_dossier", return_value=(True, "ok", cand)), \
             patch("pre_trade_guard._tier_s_calibration_message", return_value=None):
            return pre_trade_guard.evaluate_trade_opening(cmd, {"CommandLine": cmd}, {}, ws or self.ws, None)


class TestHookSizing(HookBase):

    def test_event_carries_notional_and_risk(self):
        """risk_usdt = equity x risk_pct_equity; notional = risk / |entry - stop_loss| x entry (dossier prices)."""
        for risk_pct in (0.005, 0.5):   # a percentage above 0.05 reads as percent, as the executor does
            with self.subTest(risk_pct_equity=risk_pct):
                path = os.path.join(self.ws, "logs", "gate_denials.jsonl")
                if os.path.exists(path):
                    os.remove(path)
                self.state(equity=1000.0)
                decision, reason = self.hook(risk_pct_equity=risk_pct)
                self.assertEqual(decision, "deny")
                self.assertIn(DELTA_REASON, reason)
                ev = read_events(self.ws)[0]
                self.assertEqual((ev["risk_usdt"], ev["notional_usdt"], ev["notional_derived"], ev["equity_source"]),
                                 (5.0, 100.0, True, "session_state"))

    def test_fields_omitted_without_equity_or_risk(self):
        for label, equity, profile in (("no equity", None, {"risk_pct_equity": 0.005}),
                                       ("no profile risk", 1000.0, {})):
            with self.subTest(case=label):
                path = os.path.join(self.ws, "logs", "gate_denials.jsonl")
                if os.path.exists(path):
                    os.remove(path)
                self.state(equity=equity)
                self.assertEqual(self.hook(**profile)[0], "deny")
                ev = read_events(self.ws)[0]
                for key in ("notional_usdt", "risk_usdt", "notional_derived", "equity_source"):
                    self.assertNotIn(key, ev)

    def test_sizing_never_guesses(self):
        cand = t261.TestHookRecordsDeltaDenial.cand()
        prof = {"risk_pct_equity": 0.005}
        good = {"target_env": "prod", "operating_balance": {"total_wallet_balance_usdt": 1000.0}}
        self.assertEqual(pre_trade_guard._gate_denial_sizing(cand, good, "prod", prof, self.ws, self.now),
                         {"notional_usdt": 100.0, "risk_usdt": 5.0, "notional_derived": True,
                          "equity_source": "session_state"})
        cases = {
            "other env": (cand, good, "testnet", prof),
            "zero equity": (cand, {"operating_balance": {"total_wallet_balance_usdt": 0}}, "prod", prof),
            "text equity": (cand, {"operating_balance": {"total_wallet_balance_usdt": "x"}}, "prod", prof),
            "no balance": (cand, {"target_env": "prod"}, "prod", prof),
            "bool risk": (cand, good, "prod", {"risk_pct_equity": True}),
            "negative risk": (cand, good, "prod", {"risk_pct_equity": -0.01}),
            "no profile": (cand, good, "prod", None),
            "entry = stop": (dict(cand, stop_loss=1.0), good, "prod", prof),
            "no stop": (dict(cand, stop_loss=None), good, "prod", prof),
            "nan equity": (cand, {"operating_balance": {"total_wallet_balance_usdt": float("nan")}}, "prod", prof),
        }
        for label, args in cases.items():   # no logs/primed_brief.json in the workspace: no fallback either
            with self.subTest(case=label):
                self.assertEqual(pre_trade_guard._gate_denial_sizing(*args, self.ws, self.now), {})


class TestHookBriefFallback(HookBase):
    """Issue #275 round 2: equity from logs/primed_brief.json when the session state has none."""

    def brief(self, env="PROD", age=600, equity=2000.0, raw=None, pad=0):
        path = os.path.join(self.ws, "logs", "primed_brief.json")
        with open(path, "w", encoding="utf-8") as f:
            if raw is not None:
                f.write(raw)
            else:
                json.dump({"generated_at_ts": self.now - age, "target_env": env, "padding": "x" * pad,
                           "risk_profile": {"account_equity_usdt": equity, "risk_pct_equity": 0.5}}, f)
        return path

    def event(self, **profile):
        path = os.path.join(self.ws, "logs", "gate_denials.jsonl")
        if os.path.exists(path):
            os.remove(path)
        decision, reason = self.hook(**dict({"risk_pct_equity": 0.005}, **profile))
        self.assertEqual(decision, "deny")
        self.assertIn(DELTA_REASON, reason)
        return read_events(self.ws)[0]

    def assert_unsized(self, ev):
        for key in ("notional_usdt", "risk_usdt", "notional_derived", "equity_source"):
            self.assertNotIn(key, ev)

    def test_brief_equity_used_when_the_state_has_none(self):
        self.state()                       # no operating_balance
        path = self.brief()
        reads = {}
        with patch.object(pre_trade_guard, "open", spy_open(reads, ("primed_brief.json",)), create=True):
            ev = self.event()
        # the profile's risk (0.005), not the brief's: 2000 x 0.005 = 10 USDT over a 0.05 stop at 1.0
        self.assertEqual((ev["risk_usdt"], ev["notional_usdt"], ev["notional_derived"], ev["equity_source"]),
                         (10.0, 200.0, True, "primed_brief"))
        self.assertEqual(reads["primed_brief.json"], [pre_trade_guard.GATE_DENIAL_BRIEF_MAX_BYTES + 1])
        self.assertTrue(os.path.isfile(path))

    def test_session_state_equity_wins_over_the_brief(self):
        self.state(equity=1000.0)
        self.brief(equity=2000.0)
        reads = {}
        with patch.object(pre_trade_guard, "open", spy_open(reads, ("primed_brief.json",)), create=True):
            ev = self.event()
        self.assertEqual((ev["risk_usdt"], ev["equity_source"]), (5.0, "session_state"))
        self.assertNotIn("primed_brief.json", reads)       # not even read
        # a state equity of another env is unusable: the (same-env) brief is used
        self.state(env="testnet", equity=1000.0)
        self.assertEqual(self.event()["equity_source"], "primed_brief")

    def test_unusable_brief_is_ignored(self):
        self.state()
        cases = {
            "stale (> 6 h)": dict(age=pre_trade_guard.GATE_DENIAL_BRIEF_MAX_AGE_SECONDS + 60),
            "from the future": dict(age=-3600),
            "other env": dict(env="TESTNET"),
            "no equity": dict(equity=None),
            "zero equity": dict(equity=0),
            "bool equity": dict(equity=True),
            "garbled": dict(raw="{not json"),
            "not an object": dict(raw="[1, 2]"),
            "no risk_profile": dict(raw=json.dumps({"generated_at_ts": self.now, "target_env": "PROD"})),
            "oversized": dict(pad=pre_trade_guard.GATE_DENIAL_BRIEF_MAX_BYTES),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                self.brief(**kwargs)
                reads = {}
                with patch.object(pre_trade_guard, "open", spy_open(reads, ("primed_brief.json",)), create=True):
                    ev = self.event()
                self.assert_unsized(ev)
                if label == "oversized":
                    self.assertNotIn("primed_brief.json", reads)   # never opened above the cap
                self.assertEqual(pre_trade_guard._gate_denial_recorder_errors, 0)   # the event is still written
        # just inside the age limit: used
        self.brief(age=pre_trade_guard.GATE_DENIAL_BRIEF_MAX_AGE_SECONDS - 60)
        self.assertEqual(self.event()["equity_source"], "primed_brief")
        # a directory where the brief should be: ignored, the event is still written
        os.remove(os.path.join(self.ws, "logs", "primed_brief.json"))
        os.makedirs(os.path.join(self.ws, "logs", "primed_brief.json"))
        self.assert_unsized(self.event())

    def test_decision_identical_with_any_brief(self):
        self.state()
        with patch("pre_trade_guard._record_gate_denial", return_value=None):
            baseline = self.hook(risk_pct_equity=0.005)
        self.assertEqual(baseline[0], "deny")
        for kwargs in ({}, dict(raw="{not json"), dict(pad=pre_trade_guard.GATE_DENIAL_BRIEF_MAX_BYTES),
                       dict(env="TESTNET")):
            with self.subTest(brief=kwargs):
                self.brief(**kwargs)
                self.assertEqual(self.hook(risk_pct_equity=0.005), baseline)



class TestHookBoundedReads(HookBase):

    def test_dedupe_tail_read_is_bounded(self):
        """A large log: one bounded read of the tail; a match inside it dedupes, one before it does not."""
        self.state()
        path = os.path.join(self.ws, "logs", "gate_denials.jsonl")
        match = json.dumps({"ts": self.now, "env": "prod", "gate": GATE, "symbol": "FETUSDT", "direction": "LONG",
                            "dossier_sha256": SHA})
        filler = json.dumps({"ts": self.now, "env": "prod", "gate": GATE, "symbol": "OTHERUSDT",
                             "direction": "LONG", "dossier_sha256": "f" * 64}) + "\n"
        for label, content, written in (("match in the tail", filler * 2000 + match + "\n", False),
                                        ("match before the tail", match + "\n" + filler * 2000, True)):
            with self.subTest(case=label):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                size = os.path.getsize(path)
                reads = {}
                with patch.object(pre_trade_guard, "open", spy_open(reads), create=True):
                    self.assertEqual(self.hook()[0], "deny")
                self.assertEqual(os.path.getsize(path) > size, written)
                self.assertEqual(reads["gate_denials.jsonl"], [pre_trade_guard.GATE_DENIAL_DEDUPE_TAIL_BYTES])

    def test_oversized_registry_is_skipped_and_flagged(self):
        self.state()
        entries = {f"prod:R{i}USDT:{i}": {"entry_id": str(i), "symbol": f"R{i}USDT", "direction": "LONG",
                                         "target_env": "prod", "trigger_or_limit_price": 1.0, "total_qty": 1.0,
                                         "placed_at_ts": self.now} for i in range(5)}
        with open(os.path.join(self.ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f)
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        os.makedirs(os.path.join(other, "logs"))
        for name in ("session_state.json", "pending_entries.json"):
            shutil.copy(os.path.join(self.ws, "logs", name), os.path.join(other, "logs", name))
        with patch("pre_trade_guard._record_gate_denial", return_value=None):
            baseline = self.hook(ws=other, max_open_positions=100)
        reads = {}
        with patch.object(pre_trade_guard, "GATE_DENIAL_REGISTRY_MAX_BYTES", 200), \
                patch.object(pre_trade_guard, "open", spy_open(reads), create=True):
            res = self.hook(max_open_positions=100)
        self.assertEqual(res, baseline)
        ev = read_events(self.ws)[0]
        self.assertEqual((ev["book_truncated"], ev["book_error"]), (True, None))
        self.assertEqual([i["kind"] for i in ev["book"]], ["position"])
        # the gate's own registry read (text mode) is untouched; the recorder never parsed the oversized file
        self.assertNotIn("pending_entries.json", reads)
        # under the cap: one bounded read, the resting entries are in the book
        os.remove(os.path.join(self.ws, "logs", "gate_denials.jsonl"))
        reads.clear()
        with patch.object(pre_trade_guard, "open", spy_open(reads), create=True):
            self.hook(max_open_positions=100)
        ev = read_events(self.ws)[0]
        self.assertFalse(ev["book_truncated"])
        self.assertEqual(sum(1 for i in ev["book"] if i["kind"] == "resting"), 5)
        self.assertEqual(reads["pending_entries.json"], [pre_trade_guard.GATE_DENIAL_REGISTRY_MAX_BYTES + 1])

    def test_recorder_error_counter(self):
        """Write error and size cap count; dedupe and a missing candidate do not."""
        self.state()
        self.assertEqual(self.hook()[0], "deny")                         # written
        self.assertEqual(self.hook()[0], "deny")                         # deduped
        self.assertEqual(self.hook(cand=None)[0], "deny")                # no candidate
        self.assertEqual(pre_trade_guard._gate_denial_recorder_errors, 0)
        with patch.object(pre_trade_guard, "GATE_DENIAL_MAX_BYTES", 10):
            self.assertEqual(self.hook(cand=t261.TestHookRecordsDeltaDenial.cand(sha="c" * 64))[0], "deny")
        self.assertEqual(pre_trade_guard._gate_denial_recorder_errors, 1)
        unwritable = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, unwritable, True)
        os.makedirs(os.path.join(unwritable, "logs", "gate_denials.jsonl"))
        self.state(ws=unwritable)
        self.assertEqual(self.hook(ws=unwritable)[0], "deny")
        self.assertEqual(pre_trade_guard._gate_denial_recorder_errors, 2)


# =============================================================================
# Hook end to end: decision / reason / exit code and the heartbeat counter
# =============================================================================
class TestGuardEndToEnd(tgb.GuardHarness):

    PRICES = {"entry": 100.0, "stop_loss": 95.0, "tp1": 109.0, "tp2": 120.0}

    def setUp(self):
        super().setUp()
        pre_trade_guard._gate_denial_recorder_errors = 0
        self.addCleanup(setattr, pre_trade_guard, "_gate_denial_recorder_errors", 0)

    def payload(self):
        return self.cmd(f"{SCRIPT} --symbol BTCUSDT --direction LONG --leverage 3 --env prod",
                        conversationId=tgb.PARENT_CONV_ID)

    def outcome(self, argv):
        res = self.run_guard(self.payload(), argv=argv)
        return (res.get("decision"), res.get("reason"), res.get("code"), res["__exit_code__"], res["__stderr__"])

    def heartbeat(self):
        with open(os.path.join(self.root, "logs", "hook_heartbeat.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_decision_reason_exit_code_identical_and_counter(self):
        self.write_provenance_dossier(extra=self.PRICES)
        self.write_session_state("LONG_HEAVY")
        path = os.path.join(self.root, "logs", "gate_denials.jsonl")
        for argv in (["--agy"], []):     # agy (exit 0, JSON) and legacy (exit code 2)
            with self.subTest(argv=argv):
                if os.path.isdir(path):
                    os.rmdir(path)
                elif os.path.exists(path):
                    os.remove(path)
                with patch("pre_trade_guard._record_gate_denial", return_value=None):
                    baseline = self.outcome(argv)
                errors = self.heartbeat()["gate_denial_recorder_errors"]
                with patch("pre_trade_guard._record_gate_denial", side_effect=RuntimeError("boom")):
                    raised = self.outcome(argv)
                with patch.object(pre_trade_guard, "GATE_DENIAL_MAX_BYTES", 10):
                    oversized = self.outcome(argv)
                self.assertEqual(self.heartbeat()["gate_denial_recorder_errors"], errors + 1)
                os.makedirs(path)                                  # open(..., "a") raises
                unwritable = self.outcome(argv)
                self.assertEqual(self.heartbeat()["gate_denial_recorder_errors"], errors + 2)
                os.rmdir(path)
                normal = self.outcome(argv)
                self.assertEqual(len(read_events(self.root)), 1)
                self.assertEqual(baseline[0], "deny")
                self.assertIn(DELTA_REASON, baseline[1])
                self.assertEqual(baseline[3], 0 if argv else 2)
                for res in (raised, oversized, unwritable, normal):
                    self.assertEqual(res, baseline)
                # the counter survives later invocations (carried from the previous heartbeat)
                self.agy(self.cmd("ls"))
                self.assertEqual(self.heartbeat()["gate_denial_recorder_errors"], errors + 2)
        self.assertEqual(pre_trade_guard._gate_denial_recorder_errors, 0)   # consumed by the heartbeat

    def test_prod_event_sized_from_the_cached_equity(self):
        self.write_provenance_dossier(extra=self.PRICES)
        state_path = os.path.join(self.root, "logs", "session_state.json")
        self.write_session_state("LONG_HEAVY")
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        state.update(target_env="prod", operating_balance={"total_wallet_balance_usdt": 2000.0})
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        self.assertEqual(self.run_guard(self.payload(), argv=["--agy"]).get("decision"), "deny")
        ev = read_events(self.root)[0]
        # profile default risk_pct_equity 0.005: 10 USDT risk over a 5-point stop at 100
        self.assertEqual((ev["risk_usdt"], ev["notional_usdt"], ev["notional_derived"], ev["equity_source"]),
                         (10.0, 200.0, True, "session_state"))

    def test_prod_event_sized_from_the_brief_with_identical_decision(self):
        self.write_provenance_dossier(extra=self.PRICES)
        self.write_session_state("LONG_HEAVY")            # no equity in the cached state
        with patch("pre_trade_guard._record_gate_denial", return_value=None):
            baseline = {argv: self.run_guard(self.payload(), argv=list(argv)) for argv in (("--agy",), ())}
        with open(os.path.join(self.root, "logs", "primed_brief.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_at_ts": int(time.time()) - 600, "target_env": "PROD",
                       "risk_profile": {"account_equity_usdt": 3000.0}}, f)
        for argv in (("--agy",), ()):
            with self.subTest(argv=argv):
                res = self.run_guard(self.payload(), argv=list(argv))
                for key in ("decision", "reason", "code", "__exit_code__", "__stderr__"):
                    self.assertEqual(res.get(key), baseline[argv].get(key), key)
        ev = read_events(self.root)
        self.assertEqual(len(ev), 1)                       # the second run is deduped
        self.assertEqual((ev[0]["risk_usdt"], ev[0]["notional_usdt"], ev[0]["equity_source"]),
                         (15.0, 300.0, "primed_brief"))

    def test_heartbeat_counter_ignores_a_garbled_previous_heartbeat(self):
        with open(os.path.join(self.root, "logs", "hook_heartbeat.json"), "w", encoding="utf-8") as f:
            f.write("{garbled")
        self.agy(self.cmd("ls"))
        self.assertEqual(self.heartbeat()["gate_denial_recorder_errors"], 0)
        with open(os.path.join(self.root, "logs", "hook_heartbeat.json"), "w", encoding="utf-8") as f:
            json.dump({"gate_denial_recorder_errors": True}, f)
        self.agy(self.cmd("ls"))
        self.assertEqual(self.heartbeat()["gate_denial_recorder_errors"], 0)


class TestDoctorShowsCounter(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "logs"))

    def write_hb(self, **extra):
        with open(os.path.join(self.root, "logs", "hook_heartbeat.json"), "w", encoding="utf-8") as f:
            json.dump(dict({"hook": "pre_trade_guard", "mode": "agy", "last_seen_ts": int(time.time()),
                            "tool": "run_command", "decision": "ask"}, **extra), f)

    def test_info_line_only_when_errors(self):
        self.write_hb(gate_denial_recorder_errors=3)
        self.assertEqual(trading_doctor.read_hook_heartbeat(self.root)["gate_denial_recorder_errors"], 3)
        rep = trading_doctor.check_pretool_hook(self.root, run_selftest=False)
        lines = [m for m in rep["info"] if "Gate-denial recorder errors" in m]
        self.assertEqual(len(lines), 1)
        self.assertIn("3 delta-gate denial event(s)", lines[0])
        self.assertFalse(any("Gate-denial" in m for m in rep["warnings"] + rep["critical"]))
        for extra in ({"gate_denial_recorder_errors": 0}, {}):
            with self.subTest(heartbeat=extra):
                self.write_hb(**extra)
                rep = trading_doctor.check_pretool_hook(self.root, run_selftest=False)
                self.assertFalse(any("Gate-denial" in m for m in rep["info"]))


# =============================================================================
# Events without a sha (TESTNET): fallback identity in the hook and the tracker
# =============================================================================
class TestNoShaIdentity(HookBase):

    def test_hook_and_tracker_keys_agree(self):
        self.assertEqual(pre_trade_guard.GATE_DENIAL_DEDUPE_WINDOW_SECONDS, shadow_common.DEDUPE_WINDOW_SECONDS)
        for args in ((SHA, "prod", "FETUSDT", "LONG", 1), (None, "testnet", "FETUSDT", "LONG", 7200),
                     ("", "testnet", "FETUSDT", "SHORT", 7199.9), (None, "testnet", "X", "LONG", "soon")):
            with self.subTest(args=args):
                self.assertEqual(pre_trade_guard._gate_denial_key(*args), shadow_common.gate_event_key(*args))
        self.assertIsNone(shadow_common.gate_event_key(None, "testnet", "X", "LONG", None))

    def test_hook_dedupes_testnet_events_without_sha(self):
        self.state(env="testnet")
        cand = t261.TestHookRecordsDeltaDenial.cand(sha=None)
        for _ in range(3):
            self.assertEqual(self.hook(env="testnet", cand=dict(cand))[0], "deny")
        events = read_events(self.ws)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["env"], events[0]["dossier_sha256"]), ("testnet", None))
        state = self.state(env="testnet")
        # the next time window or another direction is a new event
        later = (self.now // 3600 + 1) * 3600
        pre_trade_guard._record_gate_denial(self.ws, "testnet", later, "FETUSDT", "LONG", dict(cand), state,
                                            "LONG_HEAVY", 0)
        self.assertEqual(len(read_events(self.ws)), 2)
        pre_trade_guard._record_gate_denial(self.ws, "testnet", later, "FETUSDT", "SHORT", dict(cand), state,
                                            "SHORT_HEAVY", 0)
        self.assertEqual(len(read_events(self.ws)), 3)
        self.assertEqual(pre_trade_guard._gate_denial_recorder_errors, 0)


class TrackerBase(t251.TrackerBase):

    def event(self, **extra):
        return t261.TestTrackerIntake.event(self, **extra)

    def write_events(self, events, raw_lines=()):
        t261.TestTrackerIntake.write_events(self, events, raw_lines)


class TestTrackerNoSha(TrackerBase):

    def test_testnet_event_without_sha_registers_once_also_after_resolution(self):
        ts = (int(time.time()) // 3600) * 3600 - 7200 + 10
        ev = self.event(ts=ts, sha=None, env="testnet")
        self.write_events([ev, dict(ev, ts=ts + 60)])     # the same denial, re-judged a minute later
        self.assertEqual(st.register_from_gate_denials(), 1)
        self.assertEqual(st.register_from_gate_denials(), 0)
        row = st.load_jsonl(st.SHADOW_TRADES_FILE)[0]
        self.assertEqual((row["dossier_sha256"], row["gate_event_env"]), (None, "testnet"))
        # resolved and gone from shadow_trades.jsonl: still once (before #275 it registered again)
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, [dict(row, status="RESOLVED")])
        t251.write_jsonl(st.SHADOW_TRADES_FILE, [])
        self.assertEqual(st.register_from_gate_denials(), 0)
        self.assertEqual(st.load_jsonl(st.SHADOW_TRADES_FILE), [])
        # the next window, another env or another direction is a new denial
        self.write_events([ev, self.event(ts=ts + 3600, sha=None, env="testnet")])
        self.assertEqual(st.register_from_gate_denials(), 1)
        self.write_events([self.event(ts=ts, sha=None, env="prod", symbol="WLDUSDT")])
        self.assertEqual(st.register_from_gate_denials(), 1)

    def test_sha_rows_keep_their_identity(self):
        self.write_events([self.event(ts=int(time.time()) - 5000)])
        self.assertEqual(st.register_from_gate_denials(), 1)
        self.assertEqual(st.register_from_gate_denials(), 0)
        self.assertEqual(st._gate_row_key(st.load_jsonl(st.SHADOW_TRADES_FILE)[0]),
                         ("sha", SHA, "FETUSDT", "LONG"))
        self.assertIsNone(st._gate_row_key({"symbol": "X", "direction": "LONG", "gate_source": "dossier"}))


# =============================================================================
# Tracker: notional / risk, one audit read, garbled count, --audit / --loop pick-up
# =============================================================================
class StopLoop(Exception):
    pass


class TestTrackerIntake(TrackerBase):

    def test_row_copies_notional_and_risk(self):
        self.write_events([self.event(notional_usdt=250.0, risk_usdt=12.5, notional_derived=True,
                                      equity_source="primed_brief"),
                           self.event(symbol="WLDUSDT", sha="c" * 64)])
        self.assertEqual(st.register_from_gate_denials(), 2)
        rows = self.rows()
        self.assertEqual((rows["FETUSDT"]["notional_usdt"], rows["FETUSDT"]["target_dollar_risk"],
                          rows["FETUSDT"]["notional_derived"], rows["FETUSDT"]["equity_source"]),
                         (250.0, 12.5, True, "primed_brief"))
        self.assertEqual(rows["WLDUSDT"]["target_dollar_risk"], 1.5)          # legacy event: default risk
        for key in ("notional_usdt", "notional_derived", "equity_source"):
            self.assertNotIn(key, rows["WLDUSDT"])

    def test_hook_event_round_trips_into_a_sized_row(self):
        """The hook's writer -> the tracker: the row carries the hook's notional and risk."""
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        os.makedirs(os.path.join(ws, "logs"))
        now = int(time.time())
        state = {"last_updated_ts": now - 10, "target_env": "prod",
                 "operating_balance": {"total_wallet_balance_usdt": 1000.0},
                 "portfolio_exposure": {"net_notional_delta_usdt": 140.0},
                 "active_positions": [{"symbol": "SUIUSDT", "direction": "LONG", "notional_usdt": 40.0,
                                       "entry_order_id": 9001, "entry_time_ts": now - 3600}]}
        cand = t261.TestHookRecordsDeltaDenial.cand()
        pre_trade_guard._record_gate_denial(ws, "prod", now, "FETUSDT", "LONG", cand, state, "LONG_HEAVY", 10,
                                            user_prof={"risk_pct_equity": 0.005})
        with patch.object(st, "GATE_DENIALS_FILE", os.path.join(ws, "logs", "gate_denials.jsonl")):
            self.assertEqual(st.register_from_gate_denials(), 1)
        row = self.rows()["FETUSDT"]
        self.assertEqual((row["notional_usdt"], row["target_dollar_risk"], row["notional_derived"],
                          row["equity_source"]), (100.0, 5.0, True, "session_state"))

    def test_one_trades_audit_read_per_call(self):
        now = int(time.time())
        self.write_events([self.event(ts=now - 600, symbol=f"S{i}USDT", sha=f"{i:064d}") for i in range(3)])
        t251.write_jsonl(st.TRADES_AUDIT_FILE, [{"symbol": "SUIUSDT", "direction": "LONG", "total_qty": 1,
                                                 "timestamp": now - 3600, "score": 72}])
        with patch.object(st, "load_jsonl", wraps=st.load_jsonl) as load:
            self.assertEqual(st.register_from_gate_denials(), 3)
        self.assertEqual(sum(1 for c in load.call_args_list if c.args[0] == st.TRADES_AUDIT_FILE), 1)
        for row in self.rows().values():
            sui = [b for b in row["blockers"] if b["symbol"] == "SUIUSDT"]
            self.assertEqual(sui[0]["score"], 72.0)
        # nothing to register: the audit file is not read at all
        with patch.object(st, "load_jsonl", wraps=st.load_jsonl) as load:
            self.assertEqual(st.register_from_gate_denials(), 0)
        self.assertFalse(any(c.args[0] == st.TRADES_AUDIT_FILE for c in load.call_args_list))

    def test_garbled_lines_counted(self):
        self.write_events([self.event()], raw_lines=["{broken", "[1, 2]", "", "null"])
        with open(st.GATE_DENIALS_FILE, "ab") as f:
            f.write(b"\xff\xfe garbage bytes\n")
        stats = {"deduped_window": 0}
        self.assertEqual(st.register_from_gate_denials(stats=stats), 1)
        self.assertEqual(stats, {"deduped_window": 0, "gate_denials_garbled": 2})
        clean = {"deduped_window": 0}
        st.register_from_gate_denials(stats=clean)
        self.assertEqual(clean, {"deduped_window": 0, "gate_denials_garbled": 2})
        self.write_events([self.event()])
        clean = {}
        st.register_from_gate_denials(stats=clean)
        self.assertEqual(clean, {})
        with patch.object(st, "load_jsonl", side_effect=OSError("disk gone")):
            err = {}
            self.assertEqual(st.register_from_gate_denials(stats=err), 0)
        self.assertIn("disk gone", err["gate_denials_error"])

    def run_main(self, argv, **patches):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["shadow_tracker.py"] + argv), \
                patch.object(st, "audit_shadow_trades", return_value={}) as audit, \
                patch.object(st, "print_shadow_dashboard"), \
                patch.object(st.time, "sleep", side_effect=StopLoop), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                st.main()
            except StopLoop:
                pass
        return out.getvalue(), err.getvalue(), audit

    def test_audit_picks_up_events(self):
        self.write_events([self.event()], raw_lines=["{broken"])
        out, _err, audit = self.run_main(["--audit"])
        audit.assert_called_once()
        self.assertEqual(set(self.rows()), {"FETUSDT"})
        self.assertIn("Registered 1 hook gate denial(s)", out)
        self.assertIn("garbled gate_denials lines skipped 1", out)

    def test_audit_json_keeps_stdout_json(self):
        self.write_events([self.event()])
        out, err, _audit = self.run_main(["--audit", "--json"])
        self.assertIsInstance(json.loads(out), dict)
        self.assertIn("Registered 1 hook gate denial(s)", err)
        self.assertEqual(set(self.rows()), {"FETUSDT"})

    def test_loop_picks_up_events(self):
        self.write_events([self.event()], raw_lines=["{broken"])
        out, _err, audit = self.run_main(["--loop", "--interval", "1"])
        audit.assert_called_once()
        self.assertEqual(set(self.rows()), {"FETUSDT"})
        self.assertIn("Registered 1 candidate(s)", out)
        self.assertIn("garbled gate_denials lines skipped 1", out)
        self.assertNotIn("hook gate denial(s)", out)    # registered once per cycle, by register_from_evaluation

    def test_register_from_eval_with_audit_reads_events_once(self):
        self.write_events([self.event()])
        with patch.object(st, "register_from_gate_denials", wraps=st.register_from_gate_denials) as reg:
            self.run_main(["--register-from-eval", "--audit"])
        self.assertEqual(reg.call_count, 1)
        self.assertEqual(set(self.rows()), {"FETUSDT"})


# =============================================================================
# Analytics: notional_derived_default and no_book alignment
# =============================================================================
class TestReplay(unittest.TestCase):

    ZRO = {"symbol": "ZROUSDT", "direction": "LONG", "kind": "resting", "score": 80.0, "entry_id": "z1",
           "notional": 100.0, "since_ts": 9400}
    SHORT = {"symbol": "OPUSDT", "direction": "SHORT", "kind": "position", "score": None, "entry_id": "p1",
             "notional": 20.0, "since_ts": 5000}

    def index(self):
        return sa.build_blocker_index([], [], [])

    def hook_row(self, rid, **extra):
        extra.setdefault("book", [self.ZRO, self.SHORT])
        return t251.resolved_row(rid, "h" + rid, pnl=2.7, score=95, blockers=[self.ZRO], gate=GATE,
                                 gate_source="hook_denial", **extra)

    def test_hook_notional_used_and_legacy_rows_counted(self):
        sized = self.hook_row("f", notional_usdt=30.0, notional_derived=True, target_dollar_risk=5.0,
                              simulated_pnl_usdt=9.0, equity_source="primed_brief")
        legacy = self.hook_row("g", registered_at_ts=20000, resolved_at_ts=21000)
        self.assertEqual(sa.candidate_notional(sized), (30.0, False))
        self.assertEqual(sa.shadow_r(sized), 1.8)
        res = sa.replay_policies([sized, legacy], self.index(), resting_age_min=30)
        self.assertEqual((res["n_events"], res["notional_derived"], res["notional_derived_default"]), (2, 1, 1))
        # resting_after_n_min: ZRO (10 min old) does not count, F at its own 30 USDT notional passes (+1.8R)
        placed = res["policies"]["resting_after_n_min"]["placements"]
        self.assertEqual([(p["id"], p["shadow_r"], p["notional_derived"]) for p in placed][0], ("f", 1.8, False))
        # a #251 DELTA_GATE row with a derived notional is not a default-risk hook row
        dossier_row = t251.resolved_row("a", "d1", pnl=2.7, score=85, blockers=[self.ZRO], book=[self.ZRO])
        res = sa.replay_policies([dossier_row], self.index())
        self.assertEqual((res["notional_derived"], res["notional_derived_default"]), (1, 0))
        text = sa.format_delta_gate_report(sa.regret_report([sized, legacy], [], [], [], resamples=20),
                                           sa.replay_policies([sized, legacy], self.index()))
        self.assertIn("notional_derived_default 1", text)
        self.assertIn("snapshot error with an empty book", text)

    def test_snapshot_error_with_empty_book_is_no_book(self):
        errored = t251.resolved_row("e", "d1", pnl=2.7, blockers=[], book=[], blockers_error="JSONDecodeError: x")
        partial = t251.resolved_row("p", "d2", pnl=2.7, blockers=[self.ZRO], book=[self.ZRO, self.SHORT],
                                    blockers_error="JSONDecodeError: y", registered_at_ts=20000)
        empty = t251.resolved_row("m", "d3", pnl=2.7, blockers=[], book=[], registered_at_ts=30000)
        truncated = self.hook_row("t", book=[], book_truncated=True)
        res = sa.replay_policies([errored, partial, empty, truncated], self.index())
        self.assertEqual(res["skipped"]["no_book"], 2)                 # errored + truncated, as before for truncated
        self.assertEqual((res["n_events"], res["n_rows"]), (2, 2))     # rows with a real (or empty, no error) book
        alone = sa.replay_policies([partial, empty], self.index())
        self.assertEqual({k: v["total_r"] for k, v in res["policies"].items()},
                         {k: v["total_r"] for k, v in alone["policies"].items()})


if __name__ == "__main__":
    unittest.main()
