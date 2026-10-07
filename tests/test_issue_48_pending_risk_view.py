#!/usr/bin/env python3
"""
test_issue_48_pending_risk_view.py - Offline tests for the bundle #48 / #126 / #127 / #94 / #40.

#48  pending resting entries in every risk view: the hook's max-open-positions pre-check counts the same-env
     registry symbols (missing = 0, malformed = PROD deny / TESTNET ignore) and its delta pre-check uses the sync's
     delta_bias_incl_resting; sync_session_state adds resting_entries / resting_margin_usdt / delta_bias_incl_resting
     (only records still resting on the exchange, UNKNOWN when unreadable); the brief exposes pending_entries.
#126 defaulted standard margin sized on min(wallet, wallet + uPnL); triggered algo rows are not resting; half-tick
     registry match; a shrunk total_qty makes a record untrusted.
#127 sync exit code 1 on INVALID, brief state_sync FAILED, no non-atomic fallback, registry captured before the
     snapshot's exchange reads.
#94  Gate 0A: unreadable audit log rejects in PROD, bounded tail read.
#40  registry lock, --once keeps a live loop's state, cycle health by stage, abort escalation.

No network (urlopen is blocked) and every file goes to a temp workspace, never the real logs/.
"""

import io
import os
import sys
import json
import time
import shutil
import tempfile
import threading
import unittest
import contextlib
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
LOOPS_DIR = os.path.join(SCRIPTS_DIR, "loops")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, HOOKS_DIR, LOOPS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import sync_session_state as sss
import prime_evaluator_brief as peb
import pre_trade_guard
import position_guardian_loop as pgl
from utils import portfolio_exposure as pe
from utils import file_lock
import test_issue_101_exchange_anchored_gates as t101   # module imports only: their tests are not collected twice
import test_pending_entries as tpe
from test_pending_entries import make_record, write_registry, read_registry, write_guardian_state, INTERNAL_ERROR
from test_exit_management import FakeExchange, offline, long_position, stop

LiveExchange = t101.LiveExchange
pos = t101.pos
resting_limit = t101.resting_limit
PROFILE = t101.PROFILE
EX_FILTERS = t101.EX_FILTERS


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def keys_algo(algo_id, symbol, side="BUY", trigger=101.0, qty="12", **extra):
    return dict({"algoId": algo_id, "symbol": symbol, "side": side, "orderType": "STOP_MARKET",
                 "triggerPrice": str(trigger), "quantity": qty, "closePosition": False, "reduceOnly": False}, **extra)


def prod_record(**kw):
    kw.setdefault("env", "prod")
    return make_record(**kw)


def write_raw_registry(ws, text):
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    with open(os.path.join(ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
        f.write(text)


def tempdir(test):
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d, True)
    return d


# =============================================================================
# #48.1 hook: pending entries in the max-open-positions pre-check; delta incl. resting
# =============================================================================
class TestHookCountsPendingEntries(unittest.TestCase):
    """evaluate_trade_opening with the dossier check patched: only the session-state block is exercised."""

    PROFILE = {"autonomous_execution_tier_s": True, "max_open_positions": 3, "leverage_standard": 3,
               "leverage_yolo": 15, "leverage_ceiling": 15, "yolo_slot_enabled": False}

    def setUp(self):
        self.ws = tempdir(self)
        os.makedirs(os.path.join(self.ws, "logs"))

    def state(self, symbols=("BTCUSDT", "ETHUSDT"), bias="DELTA_BALANCED", env="prod", **portfolio):
        exposure = dict({"total_active_positions": len(symbols), "delta_bias": bias}, **portfolio)
        with open(os.path.join(self.ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
            json.dump({"is_valid": True, "last_updated_ts": int(time.time()), "target_env": env,
                       "active_positions": [{"symbol": s} for s in symbols], "portfolio_exposure": exposure}, f)

    def hook(self, direction="LONG", env="prod"):
        cmd = (f"python3 scripts/execute_futures_trade.py --symbol SOLUSDT --direction {direction} --leverage 3 "
               f"--env {env}")
        cand = {"symbol": "SOLUSDT", "direction": direction, "requires_user_confirmation": False}
        with patch("user_profile.load_user_profile", return_value=dict(self.PROFILE)), \
             patch("pre_trade_guard.check_dossier", return_value=(True, "ok", cand)):
            return pre_trade_guard.evaluate_trade_opening(cmd, {"CommandLine": cmd}, {}, self.ws, None)

    def test_pending_symbols_take_slots(self):
        self.state()
        write_registry(self.ws, prod_record(symbol="XRPUSDT"))
        decision, reason = self.hook()
        self.assertEqual(decision, "deny")
        self.assertIn("Max Open Positions Gate", reason)
        self.assertIn("Active positions (2) + pending resting entries (1)", reason)

    def test_record_on_an_open_symbol_is_counted_once(self):
        self.state()
        write_registry(self.ws, prod_record(symbol="BTCUSDT"))   # a partially filled LIMIT: position + record
        self.assertEqual(self.hook()[0], "allow")

    def test_missing_registry_counts_zero(self):
        self.state()
        self.assertEqual(self.hook()[0], "allow")

    def test_other_env_records_are_ignored(self):
        self.state()
        write_registry(self.ws, make_record(symbol="XRPUSDT", env="testnet"))
        self.assertEqual(self.hook()[0], "allow")

    def test_malformed_registry_denies_in_prod(self):
        self.state()
        for text in ("{broken", "[]", json.dumps({"entries": []})):
            with self.subTest(text=text):
                write_raw_registry(self.ws, text)
                decision, reason = self.hook()
                self.assertEqual(decision, "deny")
                self.assertIn("FAIL-CLOSED", reason)
                self.assertIn("pending entries registry", reason)

    def test_malformed_registry_is_ignored_in_testnet(self):
        self.state(env="testnet")
        write_raw_registry(self.ws, "{broken")
        self.assertEqual(self.hook(env="testnet")[0], "allow")

    def test_delta_precheck_uses_delta_incl_resting(self):
        self.state(symbols=(), bias="DELTA_BALANCED", delta_bias_incl_resting="LONG_HEAVY")
        decision, reason = self.hook("LONG")
        self.assertEqual(decision, "deny")
        self.assertIn("Delta-Neutral Hard Gate", reason)
        self.assertEqual(self.hook("SHORT")[0], "allow")
        # resting shorts balancing a heavy filled book: same book as executor Gate 1 (filled + resting)
        self.state(symbols=(), bias="LONG_HEAVY", delta_bias_incl_resting="DELTA_BALANCED")
        self.assertEqual(self.hook("LONG")[0], "allow")

    def test_unknown_delta_incl_resting_falls_back_to_delta_bias(self):
        self.state(symbols=(), bias="LONG_HEAVY", delta_bias_incl_resting="UNKNOWN")
        self.assertEqual(self.hook("LONG")[0], "deny")
        self.state(symbols=(), bias="DELTA_BALANCED", delta_bias_incl_resting="UNKNOWN")
        self.assertEqual(self.hook("LONG")[0], "allow")
        self.state(symbols=(), bias="SHORT_HEAVY")   # older state without the field
        self.assertEqual(self.hook("SHORT")[0], "deny")


# =============================================================================
# #48.3 / #127 sync_session_state: resting fields, exit code, atomic-only write
# =============================================================================
class TestSyncRestingEntries(unittest.TestCase):

    def setUp(self):
        self.tmp = tempdir(self)
        self.state_path = os.path.join(self.tmp, "session_state.json")

    def patches(self, fake):
        return [patch.object(sss, "LOGS_DIR", self.tmp), patch.object(sss, "STATE_FILE", self.state_path),
                patch.object(sss, "AUDIT_LOG", os.path.join(self.tmp, "trades_audit.jsonl")),
                patch.dict(sys.modules, {"shadow_tracker": None}),
                patch("execute_futures_trade.send_signed_request", side_effect=fake)]

    def sync(self, fake):
        with contextlib.ExitStack() as stack:
            for p in self.patches(fake):
                stack.enter_context(p)
            return sss.sync_session_state(target_env="prod")

    def registry(self, *records):
        entries = {eft.pending_entry_key(r["target_env"], r["symbol"], r["entry_id"]): r for r in records}
        with open(os.path.join(self.tmp, "pending_entries.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2, "entries": entries}, f)

    def test_only_records_still_resting_count(self):
        self.registry(prod_record(entry_id="7001", symbol="BTCUSDT"),                       # resting algo: counts
                      prod_record(kind="LIMIT", entry_id="77", symbol="ETHUSDT", direction="SHORT",
                                  trigger_or_limit_price=98.0, total_qty=1.5),               # resting LIMIT: counts
                      prod_record(entry_id="7002", symbol="ADAUSDT"),                       # entry gone: ignored
                      prod_record(entry_id="7003", symbol="SOLUSDT"),                       # filled, open: ignored
                      prod_record(entry_id="7004", symbol="DOTUSDT"),                       # triggered row: ignored
                      make_record(entry_id="7005", symbol="LINKUSDT", env="testnet"))       # other env: ignored
        fake = LiveExchange(positions=[pos("SOLUSDT", "1", 100)],
                            algos=[keys_algo(7001, "BTCUSDT"), keys_algo(7003, "SOLUSDT"),
                                   keys_algo(7004, "DOTUSDT", algoStatus="TRIGGERED"), keys_algo(7005, "LINKUSDT")],
                            orders=[resting_limit(77, "ETHUSDT", "SELL", 98.0, "1.5")])
        state = self.sync(fake)
        exp = state["portfolio_exposure"]
        self.assertIs(state["is_valid"], True)
        self.assertEqual(exp["resting_entries"], [{"symbol": "BTCUSDT", "dir": "LONG", "kind": "STOP_MARKET"},
                                                  {"symbol": "ETHUSDT", "dir": "SHORT", "kind": "LIMIT"}])
        self.assertEqual(exp["resting_margin_usdt"], round(12 * 101.0 / 3 + 1.5 * 98.0 / 3, 2))
        # filled 100 long + resting 1212 long / 147 short -> heavy long; the filled-only delta_bias is unchanged
        self.assertEqual(exp["delta_bias_incl_resting"], "LONG_HEAVY")
        self.assertEqual(exp["delta_bias"], "LONG_HEAVY")
        with open(self.state_path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["portfolio_exposure"]["resting_entries"], exp["resting_entries"])

    def test_resting_entries_can_tip_a_balanced_filled_book(self):
        self.registry(prod_record(entry_id="7001", symbol="BTCUSDT"))
        state = self.sync(LiveExchange(positions=[pos("ETHUSDT", "2", 100), pos("XRPUSDT", "-2", 100)],
                                       algos=[keys_algo(7001, "BTCUSDT")]))
        self.assertEqual(state["portfolio_exposure"]["delta_bias"], "DELTA_BALANCED")
        self.assertEqual(state["portfolio_exposure"]["delta_bias_incl_resting"], "LONG_HEAVY")

    def test_no_registry_is_empty_and_balanced(self):
        state = self.sync(LiveExchange())
        exp = state["portfolio_exposure"]
        self.assertEqual((exp["resting_entries"], exp["resting_margin_usdt"], exp["delta_bias_incl_resting"]),
                         ([], 0.0, "DELTA_BALANCED"))

    def test_unreadable_registry_or_listing_is_unknown_but_sync_valid(self):
        with open(os.path.join(self.tmp, "pending_entries.json"), "w") as f:
            f.write("{broken")
        state = self.sync(LiveExchange(algos=[keys_algo(7001, "BTCUSDT")]))
        self.assertIs(state["is_valid"], True)
        self.assertEqual(state["portfolio_exposure"]["delta_bias_incl_resting"], "UNKNOWN")
        self.registry(prod_record(entry_id="7001", symbol="BTCUSDT"))
        state = self.sync(LiveExchange(errors={"/fapi/v1/openOrders": {"code": -1001, "msg": "x"}}))
        self.assertIs(state["is_valid"], True)
        self.assertEqual(state["portfolio_exposure"]["delta_bias_incl_resting"], "UNKNOWN")
        self.assertEqual(state["portfolio_exposure"]["resting_entries"], [])

    def test_error_state_carries_the_fields(self):
        state = self.sync(LiveExchange(errors={"/fapi/v2/positionRisk": {"code": -1001, "msg": "x"}}))
        exp = state["portfolio_exposure"]
        self.assertIs(state["is_valid"], False)
        self.assertEqual((exp["delta_bias_incl_resting"], exp["resting_entries"], exp["resting_margin_usdt"]),
                         ("UNKNOWN", [], 0.0))

    def run_main(self, fake):
        with contextlib.ExitStack() as stack:
            for p in self.patches(fake):
                stack.enter_context(p)
            stack.enter_context(patch("execute_futures_trade.load_env", return_value={}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            return sss.main(["--env", "prod"])

    def test_cli_exit_code_is_1_on_invalid_state(self):
        self.assertEqual(self.run_main(LiveExchange()), 0)
        self.assertEqual(self.run_main(LiveExchange(errors={"/fapi/v2/positionRisk": {"code": -1, "msg": "x"}})), 1)
        self.assertEqual(self.run_main(LiveExchange(positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}])), 1)

    def test_failed_atomic_write_keeps_the_previous_file_and_exits_1(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"previous": True}, f)
        with patch("utils.atomic_writer.atomic_write_json", side_effect=IOError("disk full")), \
             contextlib.redirect_stderr(io.StringIO()):
            state = self.sync(LiveExchange())
            self.assertIn("disk full", state["state_write_error"])
            self.assertEqual(self.run_main(LiveExchange()), 1)
            err = self.sync(LiveExchange(errors={"/fapi/v2/positionRisk": {"code": -1, "msg": "x"}}))
            self.assertIn("state_write_error", err)
        with open(self.state_path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"previous": True}, "no plain open(..., 'w') fallback")


class TestNightCutoffSyncStatus(unittest.TestCase):
    """#127.2: the night cutoff keeps os.system and reports a failed sync with its exit code (not the wait status)."""

    def run_cutoff(self, rc):
        import night_cutoff_loop as ncl
        import test_issue_144_close_path_followups as t144
        out = io.StringIO()
        with patch("execute_futures_trade.send_signed_request", side_effect=t144.night_send([])), \
             patch("user_profile.load_user_profile", return_value={"overnight_mode": "CLOSE_ALL_AT_MARKET"}), \
             patch("os.system", return_value=rc), contextlib.redirect_stdout(out):
            ncl.run_night_cutoff(target_env="testnet")
        return out.getvalue()

    def test_failed_sync_prints_the_exit_code(self):
        text = self.run_cutoff(256 if os.name != "nt" else 1)   # POSIX wait status of exit(1)
        self.assertIn("sync FAILED (exit code 1)", text)
        self.assertNotIn("session_state.json updated", text)

    def test_successful_or_mocked_sync_reports_success(self):
        for rc in (0, MagicMock()):
            with self.subTest(rc=rc):
                text = self.run_cutoff(rc)
                self.assertIn("session_state.json updated with nightly cutoff state", text)
                self.assertNotIn("sync FAILED", text)


# =============================================================================
# #48.2 / #127 evaluator brief
# =============================================================================
class TestBriefPendingEntries(unittest.TestCase):

    def setUp(self):
        self.tmp = tempdir(self)

    def brief(self, state):
        with patch.object(peb, "BRIEF_FILE", os.path.join(self.tmp, "primed_brief.json")), \
             patch.object(peb, "ensure_fresh_state", return_value=state), \
             patch.object(peb, "get_latest_screening_payload", return_value={}), \
             patch.object(peb, "load_recent_insights", return_value=[]), \
             patch.object(peb, "build_risk_profile", return_value={}):
            return peb.assemble_primed_brief(target_env="prod")

    @staticmethod
    def state(**exposure):
        return {"target_env": "prod", "is_valid": True, "active_positions": [],
                "portfolio_exposure": dict({"delta_bias": "DELTA_BALANCED"}, **exposure)}

    def test_pending_entries_from_the_session_state(self):
        brief = self.brief(self.state(delta_bias_incl_resting="LONG_HEAVY",
                                      resting_entries=[{"symbol": "BTCUSDT", "dir": "LONG", "kind": "STOP_MARKET"}]))
        self.assertEqual(brief["pending_entries"], [{"symbol": "BTCUSDT", "dir": "LONG", "kind": "STOP_MARKET"}])
        self.assertEqual(brief["ground_truth_portfolio"]["delta_bias_incl_resting"], "LONG_HEAVY")
        self.assertNotIn("pending_entries_status", brief)
        self.assertNotIn("state_sync", brief)
        md = peb.format_markdown_brief(brief)
        self.assertIn("Pending Entries (1):** BTCUSDT (LONG STOP_MARKET)", md)

    def test_unknown_delta_incl_resting_is_unreadable(self):
        for state in (self.state(delta_bias_incl_resting="UNKNOWN", resting_entries=[]), self.state()):
            with self.subTest(state=state):
                brief = self.brief(state)
                self.assertEqual(brief["pending_entries_status"], "UNREADABLE")
                self.assertEqual(brief["pending_entries"], [])
                self.assertIn("`UNREADABLE`", peb.format_markdown_brief(brief))

    def test_failed_sync_marks_the_brief(self):
        state = dict(self.state(delta_bias_incl_resting="DELTA_BALANCED"), **{peb.SYNC_FAILED_KEY: True})
        brief = self.brief(state)
        self.assertEqual(brief["state_sync"], "FAILED")
        self.assertNotIn(peb.SYNC_FAILED_KEY, json.dumps(brief))

    def run_ensure(self, state, rc=1):
        path = os.path.join(self.tmp, "session_state.json")
        if state is not None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f)
        run = MagicMock(return_value=MagicMock(returncode=rc))
        with patch.object(peb, "STATE_FILE", path), patch.object(peb.subprocess, "run", run):
            return peb.ensure_fresh_state(target_env="prod"), run

    def test_ensure_fresh_state_reads_the_sync_return_code(self):
        state, run = self.run_ensure(None, rc=1)
        self.assertTrue(run.called)
        self.assertIs(state.get(peb.SYNC_FAILED_KEY), True)
        state, run = self.run_ensure(None, rc=0)
        self.assertNotIn(peb.SYNC_FAILED_KEY, state)

    def test_ensure_fresh_state_judges_the_file_by_its_own_timestamp(self):
        # a stale-but-valid file left by a failed write (fresh mtime here) must not look fresh
        _, run = self.run_ensure({"target_env": "prod", "is_valid": True, "last_updated_ts": int(time.time()) - 900})
        self.assertTrue(run.called)
        state, run = self.run_ensure({"target_env": "prod", "is_valid": True, "last_updated_ts": int(time.time())})
        self.assertFalse(run.called)
        self.assertNotIn(peb.SYNC_FAILED_KEY, state)


# =============================================================================
# #126.1 sizing on the Gate 2 equity
# =============================================================================
class TestDefaultMarginSizedOnGate2Equity(t101.Workspace):
    """PROD, wallet 400: default margin = min(100, max(5, equity x 0.30 x 0.5)) at 3x, entry 100."""

    def setUp(self):
        super().setUp()
        self.write_state(["BTCUSDT"])
        write_registry(self.ws)

    def run_exec(self, upnl="-100", **kw):
        row = dict(pos("BTCUSDT", "1", 100), unRealizedProfit=upnl)
        if upnl is None:
            del row["unRealizedProfit"]
        fake = LiveExchange(positions=[row])
        args = dict(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=None, sl_price=97.0,
                    tp1_price=110.0, tp2_price=120.0, target_env="prod", order_type="MARKET")
        args.update(kw)
        gates = MagicMock(return_value=(False, "stop at the gates"))
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.enforce_evaluation_dossier", return_value=(True, "ok", None)), \
             patch("execute_futures_trade.get_symbol_filters", return_value=dict(EX_FILTERS)), \
             patch("execute_futures_trade.check_mechanical_gates", gates), \
             patch("quant_risk_engine.get_account_equity", return_value=400.0), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)):
            res = eft.execute_complete_trade(**args)
        return res, gates, fake

    def test_defaulted_margin_uses_min_wallet_and_wallet_plus_upnl(self):
        _, gates, _ = self.run_exec(upnl="-100")          # equity 300 -> margin 45 -> 135 / 100 = 1.35
        self.assertEqual(gates.call_args[0][4], 1.35)
        _, gates, _ = self.run_exec(upnl="250")           # a gain never raises it: equity 400 -> margin 60 -> 1.8
        self.assertEqual(gates.call_args[0][4], 1.8)

    def test_explicit_margin_is_untouched(self):
        _, gates, _ = self.run_exec(upnl="-100", margin_usdt=50.0)   # 50 x 3 / 100
        self.assertEqual(gates.call_args[0][4], 1.5)

    def test_missing_upnl_fails_closed_before_any_write(self):
        res, gates, fake = self.run_exec(upnl=None)
        self.assertFalse(res["success"])
        self.assertTrue(res["hard_gate_rejection"])
        self.assertIn("cannot read the unrealized PnL of the open positions to size the order", res["error"])
        gates.assert_not_called()
        self.assertEqual(fake.writes(), [])

    def test_yolo_margin_is_not_resized(self):
        with patch("user_profile.get_yolo_margin", return_value=10.0):
            _, gates, _ = self.run_exec(upnl=None, is_yolo=True, leverage=3)   # no uPnL needed for YOLO sizing
        self.assertEqual(gates.call_args[0][4], 0.3)


# =============================================================================
# #126.3 triggered algo rows; #126.4 half-tick match; #126.5 shrunk total_qty
# =============================================================================
class TestTriggeredAlgoRows(t101.Workspace):

    def test_rows_with_a_non_new_algo_status_are_not_resting(self):
        live = {"open_algo_orders": [keys_algo(1, "BTCUSDT"), keys_algo(2, "ETHUSDT", algoStatus="TRIGGERED"),
                                     keys_algo(3, "XRPUSDT", algoStatus="NEW")],
                "open_orders": [resting_limit(4, "SOLUSDT")]}
        self.assertEqual([eft._order_id(o) for _s, _k, o in eft.live_resting_opening_orders(live)], [1, 3, 4])

    def test_non_live_path_skips_triggered_rows(self):
        with patch("execute_futures_trade.send_signed_request",
                   side_effect=LiveExchange(algos=[keys_algo(2, "ETHUSDT", algoStatus="TRIGGERED")])):
            self.assertEqual(eft.find_unregistered_resting_entries("prod"), ([], None))
        with patch("execute_futures_trade.send_signed_request",
                   side_effect=LiveExchange(algos=[keys_algo(2, "ETHUSDT", algoStatus="NEW")])):
            unknown, err = eft.find_unregistered_resting_entries("prod")
        self.assertIsNone(err)
        self.assertEqual([u["id"] for u in unknown], [2])

    def test_gate0a_does_not_count_a_triggered_row(self):
        self.write_state([])
        fake = LiveExchange(algos=[keys_algo(2, "ETHUSDT", algoStatus="TRIGGERED")])   # no registry file
        self.assertEqual(self.slots(fake), (True, None))
        fake = LiveExchange(algos=[keys_algo(2, "ETHUSDT")])
        ok, msg = self.slots(fake)
        self.assertFalse(ok)
        self.assertIn("the registry file is MISSING", msg)


def quantityless_algo(symbol="BTCUSDT", side="BUY", trigger=101.004):
    """An MCP-style resting algo with neither a quantity nor an id: matched to its record by symbol/side/price."""
    return {"symbol": symbol, "side": side, "triggerPrice": float(trigger), "orderType": "STOP_MARKET",
            "closePosition": False}


class TestHalfTickRegistryMatch(t101.Workspace):

    def info(self, price=101.004):
        return {"symbol": "BTCUSDT", "source": "algo", "kind": "STOP_MARKET", "id": None, "type": "STOP_MARKET",
                "side": "BUY", "price": price, "quantity": None, "executed_qty": None}

    def test_pure_match_within_half_a_tick(self):
        rec = prod_record(total_qty=2.0, tick_size=0.01)
        tol = eft.record_price_tolerances([rec])
        self.assertEqual(tol, {"BTCUSDT": 0.005})
        legs = pe.resting_opening_legs([self.info()], [rec], price_tol_by_symbol=tol)
        self.assertEqual([(l["qty"], l["source"]) for l in legs], [(2.0, "registry")])
        with self.assertRaises(ValueError):
            pe.resting_opening_legs([self.info(price=101.006)], [rec], price_tol_by_symbol=tol)   # beyond half a tick
        with self.assertRaises(ValueError):
            pe.resting_opening_legs([self.info()], [rec])                                       # exact without it

    def test_record_without_tick_size_stays_exact(self):
        rec = prod_record(total_qty=2.0)
        self.assertEqual(eft.record_price_tolerances([rec]), {})
        with self.assertRaises(ValueError):
            pe.resting_opening_legs([self.info()], [rec], price_tol_by_symbol=eft.record_price_tolerances([rec]))

    def gate(self, rec):
        self.write_state([])
        write_registry(self.ws, rec)
        fake = LiveExchange(algos=[quantityless_algo()])
        with patch("execute_futures_trade.check_max_open_positions", return_value=(True, None)):
            return self.gates(fake, "SHORT")

    def test_gate1_matches_a_tick_rounded_record(self):
        ok, msg = self.gate(prod_record(total_qty=2.0, tick_size=0.01))
        self.assertTrue(ok, msg)
        ok, msg = self.gate(prod_record(total_qty=2.0))
        self.assertFalse(ok)
        self.assertIn("delta-neutral gate cannot measure the portfolio", msg)

    def test_registration_stores_tick_and_step_size(self):
        eft.register_resting_entry("STOP_MARKET", 7001, "BTCUSDT", "LONG", "BUY", "SELL", "prod", 101.0, 12.0,
                                   95.0, 110.0, 120.0, 3, False, 404.0, tick_size=0.01, step_size=0.001)
        eft.register_resting_entry("LIMIT", 77, "ETHUSDT", "SHORT", "SELL", "BUY", "prod", 98.0, 1.5,
                                   101.0, 90.0, 80.0, 3, False, 49.0)
        entries = read_registry(self.ws)
        self.assertEqual((entries["prod:BTCUSDT:7001"]["tick_size"], entries["prod:BTCUSDT:7001"]["step_size"]),
                         (0.01, 0.001))
        self.assertNotIn("tick_size", entries["prod:ETHUSDT:77"])
        self.assertNotIn("step_size", entries["prod:ETHUSDT:77"])


class TestExecutorRegistersFilters(tpe.ExecutorHarness):

    def test_stop_market_record_carries_the_symbol_filters(self):
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        rec = read_registry(self.ws)["testnet:SOLUSDT:8"]
        self.assertEqual((rec["tick_size"], rec["step_size"]), (0.01, 0.001))


class TestShrunkTotalQty(unittest.TestCase):

    def test_pure_check(self):
        rec = make_record(total_qty=12.0, margin_usdt=404.0)        # 404 x 3 / 101 = 12
        self.assertIsNone(eft.record_qty_problem(rec))
        self.assertIsNone(eft.record_qty_problem(dict(rec, total_qty=11.77)))           # within 2%
        self.assertIn("is below the record's sizing", eft.record_qty_problem(dict(rec, total_qty=11.7)))
        self.assertIsNone(eft.record_qty_problem(dict(rec, total_qty=11.7), step_size=0.1))   # one step allowance
        self.assertIn("total_qty 6.0", eft.record_qty_problem(dict(rec, total_qty=6.0), step_size=0.1))
        # v1 / partial records skip the check
        for field in ("margin_usdt", "leverage", "trigger_or_limit_price"):
            with self.subTest(field=field):
                partial = dict(rec, total_qty=1.0)
                partial.pop(field)
                self.assertIsNone(eft.record_qty_problem(partial))
        self.assertIsNone(eft.record_qty_problem(dict(rec, total_qty=1.0, margin_usdt=0)))

    def protect(self, fake, rec, filters=True):
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        write_registry(ws, rec)
        with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=True):
            if not filters:
                with patch("execute_futures_trade.get_symbol_filters", return_value=None):
                    return eft.protect_pending_entries(target_env="testnet"), ws
            return eft.protect_pending_entries(target_env="testnet"), ws

    def test_shrunk_record_is_untrusted_and_its_entry_cancelled(self):
        fake = FakeExchange([], algos=[keys_algo(7001, "BTCUSDT", qty="6")])   # the MCP-visible shrunk entry
        res, ws = self.protect(fake, make_record(total_qty=6.0, margin_usdt=404.0))
        self.assertEqual([a["type"] for a in res["actions"]], ["pending_record_mismatch"])
        self.assertIn("is below the record's sizing", " ".join(res["actions"][0]["detail"]["mismatches"]))
        self.assertTrue(res["actions"][0]["detail"]["entry_cancelled"])
        self.assertEqual(fake.algos, [])
        self.assertEqual(read_registry(ws), {})

    def test_yolo_record_is_checked_too(self):
        fake = FakeExchange([], algos=[keys_algo(7001, "BTCUSDT", qty="6")])
        res, _ = self.protect(fake, make_record(total_qty=6.0, margin_usdt=404.0, is_yolo=True))
        self.assertEqual([a["type"] for a in res["actions"]], ["pending_record_mismatch"])

    def test_consistent_and_v1_records_are_kept(self):
        for rec in (make_record(), make_record(total_qty=6.0, margin_usdt=None)):
            with self.subTest(margin=rec["margin_usdt"]):
                fake = FakeExchange([], algos=[keys_algo(7001, "BTCUSDT", qty=str(rec["total_qty"]))])
                res, ws = self.protect(fake, rec)
                self.assertTrue(res["ok"], res["errors"])
                self.assertEqual(res["actions"], [])
                self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_unavailable_filters_defer_the_check(self):
        fake = FakeExchange([], algos=[keys_algo(7001, "BTCUSDT", qty="6")])
        res, ws = self.protect(fake, make_record(total_qty=6.0, margin_usdt=404.0), filters=False)
        self.assertEqual(res["actions"], [])
        self.assertEqual([w["stage"] for w in res["warnings"]], ["qty_check"])
        self.assertIn("testnet:BTCUSDT:7001", read_registry(ws))

    def test_filled_shrunk_record_still_protects_the_position(self):
        fake = FakeExchange([long_position(amt="6", entry="101.0", mark="101.5")])
        res, ws = self.protect(fake, make_record(total_qty=6.0, margin_usdt=404.0))
        self.assertEqual(res["actions"][0]["type"], "pending_record_mismatch")
        self.assertTrue(res["actions"][0]["detail"]["heal"]["success"], "orphan heal without the record")
        self.assertTrue(fake.algos, "a verified protective stop is in place")


# =============================================================================
# #127.4 registry captured before the snapshot's exchange reads
# =============================================================================
class TestSnapshotCapturesRegistry(t101.Workspace):

    def snapshot(self, fake):
        with patch("execute_futures_trade.send_signed_request", side_effect=fake):
            snap, err = eft.fetch_live_gate_snapshot("prod")
        self.assertIsNone(err)
        return snap

    def test_registry_read_before_the_exchange_queries(self):
        fake = LiveExchange()
        seen = []
        real = eft.load_pending_entries_status

        def spy(*a, **kw):
            seen.append(len(fake.calls))
            return real(*a, **kw)
        with patch("execute_futures_trade.load_pending_entries_status", side_effect=spy):
            snap = self.snapshot(fake)
        self.assertEqual(seen, [0])
        self.assertEqual(snap["registry"], ({}, None, True))

    def test_gate0a_uses_the_captured_registry(self):
        self.write_state([])
        write_registry(self.ws, prod_record(symbol="XRPUSDT"))
        fake = LiveExchange()
        snap = self.snapshot(fake)
        write_registry(self.ws)                 # the record is dropped after the capture
        prof = dict(PROFILE, max_open_positions=1)
        ok, msg = eft.check_max_open_positions(prof, "prod", live=snap)
        self.assertFalse(ok)
        self.assertIn("open 0 + pending 1", msg)
        snap.pop("registry")                    # a hand-built snapshot without capture reads the file
        self.assertEqual(eft.check_max_open_positions(prof, "prod", live=snap), (True, None))

    def test_gate1_uses_the_captured_registry(self):
        self.write_state([])
        write_registry(self.ws, prod_record(entry_id="9001", symbol="BTCUSDT", total_qty=2.0))
        mcp = {"algoId": 9001, "symbol": "BTCUSDT", "side": "BUY", "triggerPrice": 101.0, "orderType": "STOP_MARKET",
               "closePosition": False}
        fake = LiveExchange(algos=[mcp])
        snap = self.snapshot(fake)
        os.remove(os.path.join(self.ws, "logs", "pending_entries.json"))   # e.g. registry rewritten meanwhile
        ok, msg = self.gates(fake, "SHORT", live_snapshot=snap)
        self.assertTrue(ok, msg)
        snap.pop("registry")
        ok, msg = self.gates(fake, "SHORT", live_snapshot=snap)
        self.assertFalse(ok)


# =============================================================================
# #94 Gate 0A audit log
# =============================================================================
class TestAuditLogRead(t101.Workspace):

    def audit_path(self):
        return os.path.join(self.ws, "logs", "trades_audit.jsonl")

    def test_tail_read_drops_the_partial_first_line(self):
        lines = [json.dumps({"n": i, "pad": "x" * 20}) for i in range(3)]
        with open(self.audit_path(), "w", encoding="utf-8") as f:
            f.write("\n".join(lines[:1]) + "\n{malformed\n" + "\n".join(lines[1:]) + "\n")
        self.assertEqual([r["n"] for r in eft.read_audit_tail(self.audit_path())], [0, 1, 2])
        tail = len(lines[2]) + 1
        self.assertEqual([r["n"] for r in eft.read_audit_tail(self.audit_path(), max_bytes=tail + 5)], [2])
        self.assertEqual([r["n"] for r in eft.read_audit_tail(self.audit_path(), max_bytes=tail)], [2],
                         "a read starting exactly at a line start keeps that line")
        both = tail + len(lines[1]) + 1
        self.assertEqual([r["n"] for r in eft.read_audit_tail(self.audit_path(), max_bytes=both)], [1, 2])
        self.assertEqual(eft.AUDIT_TAIL_BYTES, 4 * 1024 * 1024)

    def test_recent_fill_found_in_the_tail_of_a_large_log(self):
        now = int(time.time())
        self.write_state([], last_updated_ts=now - 10)
        with open(self.audit_path(), "w", encoding="utf-8") as f:
            for i in range(50):
                f.write(json.dumps({"symbol": "OLDUSDT", "timestamp": now - 100000, "target_env": "prod",
                                    "total_qty": 1}) + "\n")
            f.write(json.dumps({"symbol": "NEWUSDT", "timestamp": now, "target_env": "prod", "total_qty": 1}) + "\n")
        with patch.object(eft, "AUDIT_TAIL_BYTES", 200):
            ok, msg = self.slots(LiveExchange(), profile=dict(PROFILE, max_open_positions=1))
        self.assertFalse(ok)
        self.assertIn("recent fills 1", msg)

    def test_unreadable_audit_log_rejects_in_prod_only(self):
        self.write_state([])
        os.makedirs(self.audit_path())          # exists, but open() raises IsADirectoryError (an OSError)
        ok, msg = self.slots(LiveExchange())
        self.assertFalse(ok)
        self.assertIn("FAIL-CLOSED — logs/trades_audit.jsonl is unreadable", msg)
        self.assertEqual(eft.check_max_open_positions(dict(PROFILE), "testnet"), (True, None))


# =============================================================================
# #40.1 registry lock
# =============================================================================
class TestRegistryLock(unittest.TestCase):

    def setUp(self):
        self.ws = tempdir(self)
        write_registry(self.ws, make_record())
        with open(eft.pending_entries_path(self.ws), "r", encoding="utf-8") as f:
            self.before = f.read()

    def registry_text(self):
        with open(eft.pending_entries_path(self.ws), "r", encoding="utf-8") as f:
            return f.read()

    def test_lock_not_acquired_raises_and_writes_nothing(self):
        @contextlib.contextmanager
        def not_held(path, wait_s=None):
            yield False
        mutate = MagicMock()
        with patch.object(file_lock, "locked", not_held):
            with self.assertRaises(eft.PendingRegistryLockError):
                eft.update_pending_entries(mutate, base_dir=self.ws)
        mutate.assert_not_called()
        self.assertEqual(self.registry_text(), self.before)

    def test_lock_held_by_another_writer_times_out(self):
        errors = []

        def writer():
            try:
                eft.update_pending_entries(lambda e: e.clear(), base_dir=self.ws)
            except Exception as e:
                errors.append(e)
        with patch.object(eft, "PENDING_REGISTRY_LOCK_WAIT_S", 0.2), \
             file_lock.locked(eft.pending_entries_path(self.ws)) as held, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(held)
            t = threading.Thread(target=writer)
            t.start()
            t.join(5)
        self.assertEqual([type(e) for e in errors], [eft.PendingRegistryLockError])
        self.assertEqual(self.registry_text(), self.before)

    def test_nested_update_raises_at_once(self):
        start = time.monotonic()
        with self.assertRaises(eft.PendingRegistryLockError) as ctx:
            eft.update_pending_entries(lambda e: eft.update_pending_entries(lambda e2: None, base_dir=self.ws),
                                       base_dir=self.ws)
        self.assertLess(time.monotonic() - start, 1.0, "no wait on its own flock")
        self.assertIn("re-entered", str(ctx.exception))
        self.assertEqual(self.registry_text(), self.before)
        eft.update_pending_entries(lambda e: e.__setitem__("k", {"x": 1}), base_dir=self.ws)   # flag released
        self.assertIn("k", read_registry(self.ws))

    def test_concurrent_updates_are_serialised(self):
        inside = threading.Event()

        def slow_a(entries):
            inside.set()
            time.sleep(0.3)               # B reads now without the lock and A's write would drop B's record
            entries["a"] = {"x": 1}

        def run_a():
            eft.update_pending_entries(slow_a, base_dir=self.ws)

        def run_b():
            inside.wait(5)
            eft.update_pending_entries(lambda e: e.__setitem__("b", {"x": 2}), base_dir=self.ws)
        threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertTrue({"a", "b", "testnet:BTCUSDT:7001"} <= set(read_registry(self.ws)))

    def test_lock_error_routes_registration_to_cancel(self):
        harness = tpe.ExecutorHarness("setUp")
        harness.setUp()
        self.addCleanup(shutil.rmtree, harness.ws, True)
        with patch("execute_futures_trade.update_pending_entries",
                   side_effect=eft.PendingRegistryLockError("not acquired")):
            res = harness.execute(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertFalse(res["success"])
        self.assertTrue(res["pending_registry_failure"])
        self.assertTrue(res["entry_cancelled"])


class TestLockFailureBeforeStopNeverSkipsProtection(unittest.TestCase):
    """Round 2: the bookkeeping saves before the stop (missing_since_ts clear, pre-arm sl_algo_id) log a lock failure
    and the same cycle still places / verifies the stop; the record is kept and the write retried next run."""

    def protect(self, fake, rec):
        ws = tempdir(self)
        write_registry(ws, rec)
        real = eft.update_pending_entries
        calls = []

        def first_call_locked(mutate, base_dir=None):
            calls.append(1)
            if len(calls) == 1:
                raise eft.PendingRegistryLockError("registry lock not acquired")
            return real(mutate, base_dir)
        with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("execute_futures_trade.update_pending_entries", side_effect=first_call_locked), \
             self.assertLogs("execute_futures_trade", level="WARNING"):
            res = eft.protect_pending_entries(target_env="testnet")
        return res, ws

    @staticmethod
    def partial_limit():
        return {"orderId": 8001, "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": "101.0",
                "origQty": "12", "reduceOnly": False}

    def test_missing_since_clear_lock_error_still_places_the_stop(self):
        fake = FakeExchange([long_position(amt="4", entry="101.0", mark="101.5")], open_orders=[self.partial_limit()])
        res, ws = self.protect(fake, make_record(kind="LIMIT", entry_id="8001", missing_since_ts=int(time.time()) - 10))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual([w["stage"] for w in res["warnings"]], ["registry_lock"])
        protect = [a for a in res["actions"] if a["type"] == "pending_protect_sl"]
        self.assertTrue(protect and protect[0]["success"], res["actions"])
        self.assertEqual([float(a["triggerPrice"]) for a in fake.algos], [95.0], "planned stop placed and verified")
        rec = read_registry(ws)["testnet:BTCUSDT:8001"]
        self.assertIn("missing_since_ts", rec, "the deferred clear is retried on the next run")
        self.assertEqual(rec["sl_qty"], 4.0)

    def test_prearm_sl_algo_id_lock_error_keeps_the_verified_stop(self):
        fake = FakeExchange([long_position(amt="4", entry="101.0", mark="101.5")], algos=[stop(555, 95.0)],
                            open_orders=[self.partial_limit()])
        res, ws = self.protect(fake, make_record(kind="LIMIT", entry_id="8001", prearm_algo_id=555,
                                                 prearm_status="placed", sl_close_position=True, sl_qty=None))
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual([w["stage"] for w in res["warnings"]], ["registry_lock"])
        self.assertEqual([a["algoId"] for a in fake.algos], [555], "the verified pre-armed stop stays in force")
        self.assertEqual(fake.writes(), [])
        rec = read_registry(ws)["testnet:BTCUSDT:8001"]
        self.assertNotIn("sl_algo_id", rec, "retried on the next run")


# =============================================================================
# #40.2 --once keeps a live loop's state; #40.3 cycle health by stage
# =============================================================================
class TestGuardianStateOwnership(unittest.TestCase):

    def setUp(self):
        self.ws = tempdir(self)
        self.log_dir = os.path.join(self.ws, "logs")

    def state_file(self):
        return os.path.join(self.log_dir, pgl.STATE_FILE_NAME)

    def read_state(self):
        with open(self.state_file(), "r", encoding="utf-8") as f:
            return json.load(f)

    def run_once(self, fake=None, env="testnet"):
        with offline(fake or FakeExchange([]), workspace=self.ws), patch.object(pgl, "DEFAULT_LOG_DIR", self.log_dir), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(tpe.HEALTHY)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = pgl.main(["--once", "--env", env])
        return code, err.getvalue()

    def test_once_does_not_overwrite_a_fresh_loop_state(self):
        write_guardian_state(self.ws, env="prod", age=5)
        before = self.read_state()
        code, err = self.run_once()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state(), before)
        self.assertIn("a live guardian loop owns", err)
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws):
            self.assertTrue(eft.check_guardian_alive("prod")[0])

    def test_once_overwrites_a_stale_or_once_state(self):
        for kw in ({"age": 1000}, {"mode": "once", "interval_seconds": None, "age": 5}):
            with self.subTest(kw=kw):
                write_guardian_state(self.ws, env="prod", **kw)
                self.run_once()
                state = self.read_state()
                self.assertEqual((state["mode"], state["env"]), ("once", "testnet"))

    def test_once_still_runs_and_appends_its_actions(self):
        write_guardian_state(self.ws, env="prod", age=5)
        fake = FakeExchange([long_position()])                      # orphan: healed by the --once cycle
        code, _ = self.run_once(fake)
        self.assertEqual(code, 0)
        self.assertTrue(fake.algos, "the protective cycle ran")
        with open(os.path.join(self.log_dir, pgl.ACTIONS_FILE_NAME), "r", encoding="utf-8") as f:
            self.assertIn("orphan_heal", f.read())
        self.assertEqual(self.read_state()["mode"], "loop")

    def test_cycle_writes_error_stages(self):
        fake = FakeExchange([])

        def broken(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v1/openAlgoOrders":
                return {"error": "timeout"}
            return fake(method, endpoint, params, target_env)
        write_registry(self.ws, make_record())
        self.run_once(broken)
        self.assertEqual(self.read_state()["error_stages"], ["pending_orders_query", "pending_unknown_entry"])
        os.remove(eft.pending_entries_path(self.ws))
        self.run_once()
        self.assertEqual(self.read_state()["error_stages"], [])


class TestGuardianCycleHealth(unittest.TestCase):

    def setUp(self):
        self.ws = tempdir(self)

    def alive(self, stages="absent"):
        write_guardian_state(self.ws, env="prod")
        if stages != "absent":
            path = os.path.join(self.ws, "logs", "guardian_state.json")
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            state["error_stages"] = stages
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f)
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws):
            return eft.check_guardian_alive("prod")

    def test_stage_rule(self):
        for stages in ("absent", [], ["trailing"], ["dead_alpha", "dead_alpha_close", "exception"],
                       ["pending_unknown_entry"]):
            with self.subTest(stages=stages):
                self.assertTrue(self.alive(stages)[0])
        for stages in (["positions_sync"], ["pending_orders_query"], ["pending_entries"],
                       ["trailing", "pending_abort"], "garbage"):
            with self.subTest(stages=stages):
                ok, why = self.alive(stages)
                self.assertFalse(ok)
        self.assertIn("positions_sync", self.alive(["positions_sync", "pending_unknown_entry"])[1])

    def test_unhealthy_guardian_rejects_resting_entries(self):
        self.alive(["positions_sync"])
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws):
            ok, msg = eft.check_resting_entry_gates("BTCUSDT", "prod")
        self.assertFalse(ok)
        self.assertIn("the position guardian is not alive", msg)


# =============================================================================
# #40.4 abort escalation
# =============================================================================
class TestAbortEscalation(unittest.TestCase):

    def setUp(self):
        self.report = MagicMock(return_value={})
        p = patch("report_agent_issue.report_issue", self.report)
        p.start()
        self.addCleanup(p.stop)

    def protect(self, fake, rec, abort=None):
        ws = tempdir(self)
        write_registry(ws, rec)
        with offline(fake, workspace=ws), contextlib.ExitStack() as stack:
            if abort is not None:
                stack.enter_context(patch("execute_futures_trade.emergency_abort_market_close", return_value=abort))
            return eft.protect_pending_entries(target_env="testnet")

    def assertReported(self, site, symbol="BTCUSDT"):
        self.report.assert_called_once()
        kw = self.report.call_args.kwargs
        self.assertEqual((kw["severity"], kw["priority"], kw["category"]), ("CRITICAL", "P0", "risk_gate"))
        self.assertEqual(kw["error_detail"], f"{symbol} abort not flat or protected at {site}")
        self.assertIn(site, kw["title"])
        return kw

    def test_unconfirmed_pending_abort_is_escalated_with_a_stable_fingerprint(self):
        fail = {"confirmed": False, "order": {"code": -1001, "msg": "Internal error"}}
        fake = FakeExchange([long_position(amt="10", entry="101.0")], reject_new_stops=True,
                            reject_response=INTERNAL_ERROR)
        res = self.protect(fake, make_record(), abort=fail)
        self.assertEqual(res["errors"][-1]["stage"], "abort")
        first = self.assertReported("pending_abort")
        self.report.reset_mock()
        self.protect(FakeExchange([long_position(amt="10", entry="101.0")], reject_new_stops=True,
                                  reject_response=INTERNAL_ERROR),
                     make_record(), abort=dict(fail, order={"code": -1000, "msg": "other text"}))
        second = self.assertReported("pending_abort")
        self.assertEqual((first["title"], first["error_detail"]), (second["title"], second["error_detail"]))

    def test_confirmed_abort_is_not_escalated(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0")], reject_new_stops=True,
                            reject_response=INTERNAL_ERROR)
        res = self.protect(fake, make_record(), abort={"confirmed": True, "order": {"orderId": 1}})
        self.assertIn("pending_abort", [a["type"] for a in res["actions"]])
        self.report.assert_not_called()

    def crossed_send(self, fake):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == "/fapi/v1/order" and (params or {}).get("type") == "MARKET":
                fake.calls.append((method, endpoint, dict(params)))
                return {"code": -1001, "msg": "Internal error"}
            return fake(method, endpoint, params, target_env)
        return send

    def test_crossed_close_not_flat_with_a_kept_stop_is_not_escalated(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="99.0")], algos=[stop(601, 98.4)])
        res = self.protect(self.crossed_send(fake), make_record(sl=99.4))
        self.assertEqual(res["errors"][0]["stage"], "sl_crossed_close")
        self.report.assert_not_called()

    def test_crossed_close_not_flat_without_any_stop_is_escalated(self):
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="94.0")], reject_new_stops=True,
                            reject_response=INTERNAL_ERROR)
        res = self.protect(self.crossed_send(fake), make_record(sl=95.0))
        self.assertEqual(res["errors"][0]["stage"], "sl_crossed_close")
        self.assertReported("pending_sl_crossed_close")

    def test_heal_close_failure_is_escalated(self):
        fake = FakeExchange([long_position()], reject_new_stops=True, reject_response=INTERNAL_ERROR)
        with offline(fake), patch("execute_futures_trade.emergency_abort_market_close",
                                  return_value={"confirmed": False}):
            out = eft.heal_orphan_position(long_position(), target_env="testnet", close_on_failure=True)
        self.assertEqual(out["reason"], "heal_and_close_failed")
        self.assertReported("heal_orphan_position")
        self.report.reset_mock()
        with offline(fake), patch("execute_futures_trade.emergency_abort_market_close",
                                  return_value={"confirmed": True}):
            eft.heal_orphan_position(long_position(), target_env="testnet", close_on_failure=True)
        self.report.assert_not_called()

    def test_partial_fill_abort_not_confirmed_is_escalated(self):
        fx = FakeExchange([], reject_new_stops=True)
        with patch("execute_futures_trade.emergency_abort_market_close", return_value={"confirmed": False}):
            res, _ws = tpe.TestPartiallyFilledLimitAtPlacement._execute(self, fx)
        self.assertTrue(res["emergency_abort"])
        self.assertReported("partial_fill_abort", symbol="SOLUSDT")

    def test_reporting_failure_never_breaks_the_path(self):
        self.report.side_effect = RuntimeError("gh down")
        fake = FakeExchange([long_position(amt="10", entry="101.0")], reject_new_stops=True,
                            reject_response=INTERNAL_ERROR)
        with self.assertLogs("execute_futures_trade", level="ERROR"):
            res = self.protect(fake, make_record(), abort={"confirmed": False, "order": {}})
        self.assertEqual(res["errors"][-1]["stage"], "abort")


if __name__ == "__main__":
    unittest.main()
