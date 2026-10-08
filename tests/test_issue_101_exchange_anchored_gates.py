#!/usr/bin/env python3
"""
test_issue_101_exchange_anchored_gates.py - Offline tests for issue #101: the PROD gates are anchored to the
exchange, so deleting or editing logs/session_state.json / logs/pending_entries.json cannot make a PROD gate pass
that the live exchange state would fail.

1. utils/portfolio_exposure.compute_exposure (shared by sync_session_state.py and the executor); the sync output is
   unchanged.
2. Live snapshot (all-symbol positionRisk + openAlgoOrders + openOrders), fetched ONCE per order attempt; any read
   failure rejects in PROD.
3. Gate 0A in PROD: stricter of the files and the live view; missing / corrupt session_state rejects; a missing
   registry with live resting opening orders rejects.
4. Gate 1 in PROD: a forged fresh NEUTRAL file cannot hide a live LONG_HEAVY / SHORT_HEAVY portfolio.
5. Equity: PROD ignores session_state.operating_balance and reads /fapi/v2/balance.
6. protect_pending_entries cross-checks the resting entry against its record (pending_record_mismatch).
7. TESTNET behaviour unchanged (no new exchange call).

No network (urlopen is blocked) and every file goes to a temp workspace, never the real logs/.
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
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import quant_risk_engine as qre
import sync_session_state as sss
from utils import portfolio_exposure as pe
from test_exit_management import FakeExchange, offline, long_position, stop, ALGO_ENDPOINT
from test_pending_entries import make_record, write_registry, read_registry, entry_algo, write_guardian_state

SNAPSHOT_ENDPOINTS = ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders", "/fapi/v2/positionRisk")   # #160 order
PROFILE = {"max_open_positions": 3, "yolo_slot_enabled": True, "leverage_standard": 3, "leverage_yolo": 15,
           "leverage_ceiling": 15, "risk_pct_equity": 0.005, "max_margin_ratio": 0.30}
EX_FILTERS = {"stepSize": 0.001, "minQty": 0.001, "tickSize": 0.01, "precision_qty": 3, "precision_price": 2,
              "minNotional": 5.0}


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def pos(symbol, amt, mark=100.0, entry=None):
    return {"symbol": symbol, "positionAmt": str(amt), "markPrice": str(mark), "entryPrice": str(entry or mark),
            "leverage": "3", "unRealizedProfit": "0", "updateTime": 0}


def resting_limit(order_id, symbol, side="BUY", price=98.0, qty="1.5"):
    return {"orderId": order_id, "symbol": symbol, "side": side, "type": "LIMIT", "price": str(price),
            "origQty": qty, "reduceOnly": False, "closePosition": False, "status": "NEW"}


class LiveExchange:
    """Fake exchange for the PROD order path; records every call."""

    def __init__(self, positions=(), algos=(), orders=(), errors=None, balance=None):
        self.positions = [dict(p) for p in positions]
        self.algos = [dict(a) for a in algos]
        self.orders = [dict(o) for o in orders]
        self.errors = dict(errors or {})
        self.balance = balance
        self.calls = []

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        self.calls.append((method, endpoint, params))
        sym = params.get("symbol")
        if method == "GET" and endpoint in self.errors:
            err = self.errors[endpoint]
            if isinstance(err, Exception):
                raise err
            return err
        if endpoint == "/fapi/v2/positionRisk":
            return [dict(p) for p in self.positions if not sym or p["symbol"] == sym]
        if endpoint == "/fapi/v1/openAlgoOrders":
            return [dict(a) for a in self.algos if not sym or a["symbol"] == sym]
        if endpoint == "/fapi/v1/openOrders":
            return [dict(o) for o in self.orders if not sym or o["symbol"] == sym]
        if endpoint == "/fapi/v2/balance":
            return [{"asset": "USDT", "balance": str(self.balance)}] if self.balance is not None else {"error": "down"}
        if endpoint == "/fapi/v1/marginType":
            return {"code": 200, "msg": "success"}
        if endpoint == "/fapi/v1/leverage":
            return {"symbol": params["symbol"], "leverage": params["leverage"]}
        if endpoint == "/fapi/v1/leverageBracket":
            return {"error": "unavailable"}
        if endpoint == "/fapi/v1/ticker/price":
            return {"price": "100.0"}
        if endpoint == "/fapi/v1/order" and method == "POST":
            return {"orderId": 7, "avgPrice": "100.0", "status": "FILLED"}
        return {}

    def all_symbol_gets(self):
        return [c[1] for c in self.calls if c[0] == "GET" and not c[2] and c[1] in SNAPSHOT_ENDPOINTS]

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "DELETE", "PUT")]


class Workspace(unittest.TestCase):
    """Temp workspace for logs/ (executor _workspace_dir and module __file__ for Gate 1 / equity)."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws, True)
        os.makedirs(os.path.join(self.ws, "logs"), exist_ok=True)
        for p in (patch("execute_futures_trade._workspace_dir", return_value=self.ws),
                  patch.object(eft, "__file__", os.path.join(self.ws, "scripts", "execute_futures_trade.py")),
                  patch.object(qre, "__file__", os.path.join(self.ws, "scripts", "quant_risk_engine.py"))):
            p.start()
            self.addCleanup(p.stop)

    def write_state(self, symbols=(), bias="DELTA_BALANCED", count=None, **extra):
        state = {"is_valid": True, "last_updated_ts": int(time.time()), "target_env": "prod",
                 "active_positions": [{"symbol": s} for s in symbols],
                 "portfolio_exposure": {"total_active_positions": len(symbols) if count is None else count,
                                        "delta_bias": bias}}
        state.update(extra)
        with open(self.state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)

    def state_path(self):
        return os.path.join(self.ws, "logs", "session_state.json")

    def gates(self, fake, direction="LONG", env="prod", profile=None, **kw):
        sl, tp1 = (98.0, 105.0) if direction == "LONG" else (102.0, 95.0)
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("user_profile.load_user_profile", return_value=dict(profile or PROFILE)):
            return eft.check_mechanical_gates(direction, 100.0, sl, tp1, 1.0, 3, target_env=env, **kw)

    def slots(self, fake, env="prod", profile=None):
        with patch("execute_futures_trade.send_signed_request", side_effect=fake):
            return eft.check_max_open_positions(dict(profile or PROFILE), env)

    def execute(self, fake, env="prod", **kwargs):
        args = dict(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0, sl_price=97.0,
                    tp1_price=110.0, tp2_price=120.0, target_env=env, order_type="MARKET")
        if env == "testnet":
            args["bypass_eval_gate"] = True
        args.update(kwargs)
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.enforce_evaluation_dossier", return_value=(True, "ok", None)), \
             patch("execute_futures_trade.get_symbol_filters", return_value=dict(EX_FILTERS)), \
             patch("execute_futures_trade.place_algo_stop_loss", return_value={"algoId": 9}), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 9})), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch("provenance_stamp.stamp_trade_record", side_effect=lambda rec, **kw: rec):
            return eft.execute_complete_trade(**args)


# =============================================================================
# 1. compute_exposure + sync_session_state output unchanged
# =============================================================================
class TestComputeExposure(unittest.TestCase):

    def test_classification_and_thresholds(self):
        exp = pe.compute_exposure([pos("BTCUSDT", "0.5", 60000), pos("ETHUSDT", "-2", 3000), pos("XRPUSDT", "0", 1)])
        self.assertEqual(exp["total_active_positions"], 2)
        self.assertEqual(exp["symbols"], ["BTCUSDT", "ETHUSDT"])
        self.assertAlmostEqual(exp["long_notional"], 30000.0)
        self.assertAlmostEqual(exp["short_notional"], 6000.0)
        self.assertAlmostEqual(exp["net_notional"], 24000.0)
        self.assertAlmostEqual(exp["delta_ratio"], 24000.0 / 36000.0)
        self.assertEqual(exp["delta_bias"], pe.LONG_HEAVY)
        self.assertEqual([p["side"] for p in exp["active_positions"]], ["LONG", "SHORT"])

    def test_boundaries(self):
        self.assertEqual(pe.DELTA_HEAVY_RATIO, 0.35)
        self.assertEqual(pe.classify_delta(0.35), pe.DELTA_BALANCED)
        self.assertEqual(pe.classify_delta(0.3501), pe.LONG_HEAVY)
        self.assertEqual(pe.classify_delta(-0.35), pe.DELTA_BALANCED)
        self.assertEqual(pe.classify_delta(-0.3501), pe.SHORT_HEAVY)

    def test_empty_input_is_flat(self):
        for rows in (None, [], [pos("XRPUSDT", "0")]):
            exp = pe.compute_exposure(rows)
            self.assertEqual(exp["total_active_positions"], 0)
            self.assertEqual(exp["delta_ratio"], 0.0)
            self.assertEqual(exp["delta_bias"], pe.DELTA_BALANCED)

    def test_malformed_rows_raise_never_dropped(self):
        for row in (None, "BTCUSDT", {"positionAmt": "1"}, {"symbol": "X", "positionAmt": "abc"},
                    {"symbol": "X", "positionAmt": "1", "markPrice": "n/a"}):
            with self.assertRaises(ValueError, msg=repr(row)):
                pe.compute_exposure([pos("BTCUSDT", "1"), row])

    def test_malformed_position_row_fails_the_live_snapshot(self):
        fake = LiveExchange(positions=[pos("BTCUSDT", "1"), {"symbol": "ETHUSDT", "positionAmt": "garbage"}])
        with patch("execute_futures_trade.send_signed_request", side_effect=fake):
            snap, err = eft.fetch_live_gate_snapshot("prod")
        self.assertIsNone(snap)
        self.assertIn("malformed", err)


class TestSyncSessionStateOutputUnchanged(unittest.TestCase):

    def run_sync(self, positions):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = LiveExchange(positions=positions)
        with patch.object(sss, "LOGS_DIR", tmp), \
             patch.object(sss, "STATE_FILE", os.path.join(tmp, "session_state.json")), \
             patch.object(sss, "AUDIT_LOG", os.path.join(tmp, "trades_audit.jsonl")), \
             patch.dict(sys.modules, {"shadow_tracker": None}), \
             patch("execute_futures_trade.send_signed_request", side_effect=fake):
            return sss.sync_session_state(target_env="prod")

    def test_long_heavy_golden_values(self):
        state = self.run_sync([pos("BTCUSDT", "0.5", 60000, 59000), pos("ETHUSDT", "-2", 3000, 3100), pos("XRPUSDT", "0")])
        exp = state["portfolio_exposure"]
        self.assertEqual(exp["total_active_positions"], 2)
        self.assertEqual(exp["long_notional_usdt"], 30000.0)
        self.assertEqual(exp["short_notional_usdt"], 6000.0)
        self.assertEqual(exp["net_notional_delta_usdt"], 24000.0)
        self.assertEqual(exp["delta_bias"], "LONG_HEAVY")
        self.assertIn("BULLISH IMBALANCE", exp["delta_advice"])
        self.assertEqual([(p["symbol"], p["direction"], p["qty"], p["notional_usdt"], p["margin_usdt"])
                          for p in state["active_positions"]],
                         [("BTCUSDT", "LONG", 0.5, 30000.0, 10000.0), ("ETHUSDT", "SHORT", -2.0, 6000.0, 2000.0)])

    def test_short_heavy_and_balanced(self):
        self.assertEqual(self.run_sync([pos("ETHUSDT", "-2", 3000)])["portfolio_exposure"]["delta_bias"], "SHORT_HEAVY")
        state = self.run_sync([pos("BTCUSDT", "1", 100), pos("ETHUSDT", "-1", 100)])
        self.assertEqual(state["portfolio_exposure"]["delta_bias"], "DELTA_BALANCED")
        self.assertIn("DELTA-NEUTRAL EQUILIBRIUM", state["portfolio_exposure"]["delta_advice"])
        flat = self.run_sync([])
        self.assertEqual(flat["portfolio_exposure"]["delta_bias"], "DELTA_BALANCED")
        self.assertEqual(flat["portfolio_exposure"]["total_active_positions"], 0)


# =============================================================================
# 2-4. PROD gates anchored to the live exchange
# =============================================================================
class TestGate1LiveDelta(Workspace):

    def test_forged_neutral_file_cannot_hide_live_long_heavy(self):
        self.write_state(["BTCUSDT"], bias="NEUTRAL")
        fake = LiveExchange(positions=[pos("BTCUSDT", "1", 100)])
        ok, msg = self.gates(fake, "LONG")
        self.assertFalse(ok)
        self.assertIn("LONG_HEAVY state", msg)
        self.assertIn("Source: live exchange positionRisk", msg)
        self.assertNotIn("and session_state.json (", msg)
        ok, msg = self.gates(fake, "SHORT")
        self.assertTrue(ok, msg)

    def test_forged_neutral_file_cannot_hide_live_short_heavy(self):
        self.write_state(["ETHUSDT"], bias="DELTA_BALANCED")
        fake = LiveExchange(positions=[pos("ETHUSDT", "-3", 100)])
        ok, msg = self.gates(fake, "SHORT")
        self.assertFalse(ok)
        self.assertIn("SHORT_HEAVY state", msg)
        self.assertIn("live exchange positionRisk", msg)
        self.assertTrue(self.gates(fake, "LONG")[0])

    def test_file_long_heavy_still_rejects_when_live_is_balanced(self):
        self.write_state([], bias="LONG_HEAVY")
        ok, msg = self.gates(LiveExchange(), "LONG")
        self.assertFalse(ok)
        self.assertIn("Source: session_state.json", msg)

    def test_both_sources_named(self):
        self.write_state(["BTCUSDT"], bias="LONG_HEAVY")
        ok, msg = self.gates(LiveExchange(positions=[pos("BTCUSDT", "1")]), "LONG")
        self.assertFalse(ok)
        self.assertIn("live exchange positionRisk and session_state.json", msg)

    def test_existing_file_requirements_kept(self):
        fake = LiveExchange()
        self.write_state([], last_updated_ts=int(time.time()) - 400)
        ok, msg = self.gates(fake)
        self.assertFalse(ok)
        self.assertIn("STALE", msg)
        self.write_state([], is_valid=False)
        ok, msg = self.gates(fake)
        self.assertFalse(ok)
        self.assertIn("INVALID", msg)

    def test_non_dict_portfolio_exposure_rejects_cleanly(self):
        with open(self.state_path(), "w", encoding="utf-8") as f:
            json.dump({"is_valid": True, "last_updated_ts": int(time.time()), "portfolio_exposure": ["LONG_HEAVY"]}, f)
        ok, msg = self.gates(LiveExchange())          # Gate 0A sees it first: corrupt cache
        self.assertFalse(ok)
        self.assertIn("FAIL-CLOSED", msg)
        with patch("execute_futures_trade.check_max_open_positions", return_value=(True, None)):
            ok, msg = self.gates(LiveExchange())      # Gate 1 itself
        self.assertFalse(ok)
        self.assertIn("portfolio_exposure is malformed", msg)

    def test_position_risk_failure_rejects(self):
        self.write_state([])
        for err in ({"code": -1001, "msg": "Internal error"}, OSError("unreachable")):
            fake = LiveExchange(errors={"/fapi/v2/positionRisk": err})
            ok, msg = self.gates(fake)
            self.assertFalse(ok)
            self.assertIn("FAIL-CLOSED — cannot read the live exchange state for the PROD gates", msg)
            self.assertIn("/fapi/v2/positionRisk query failed", msg)

    def test_snapshot_passed_in_is_not_refetched(self):
        self.write_state([])
        fake = LiveExchange()
        with patch("execute_futures_trade.send_signed_request", side_effect=fake):
            snap, err = eft.fetch_live_gate_snapshot("prod")
        self.assertIsNone(err)
        self.assertEqual(fake.all_symbol_gets(), list(SNAPSHOT_ENDPOINTS))
        fake2 = LiveExchange(errors={e: {"error": "must not be called"} for e in SNAPSHOT_ENDPOINTS})
        ok, msg = self.gates(fake2, live_snapshot=snap)
        self.assertTrue(ok, msg)
        self.assertEqual(fake2.calls, [])


class TestGate0ALive(Workspace):

    def test_deleted_session_state_rejected_in_prod(self):
        ok, msg = self.slots(LiveExchange())
        self.assertFalse(ok)
        self.assertIn("Max open positions limit (3): FAIL-CLOSED — logs/session_state.json does not exist", msg)
        ok, msg = self.gates(LiveExchange())
        self.assertFalse(ok)
        self.assertIn("session_state.json does not exist", msg)

    def test_corrupt_session_state_rejected_in_prod(self):
        with open(self.state_path(), "w") as f:
            f.write("{broken")
        ok, msg = self.slots(LiveExchange())
        self.assertFalse(ok)
        self.assertIn("session_state.json is corrupt", msg)

    def test_file_zero_positions_but_live_at_max_rejected(self):
        self.write_state([], count=0)
        fake = LiveExchange(positions=[pos("AUSDT", "1"), pos("BUSDT", "-1"), pos("CUSDT", "1"), pos("DUSDT", "-1")])
        ok, msg = self.slots(fake)
        self.assertFalse(ok)
        self.assertIn("reached (open 4 + pending 0 >= max 3)", msg)
        self.assertIn("Live exchange view: 4 open position symbol(s)", msg)
        ok, msg = self.gates(fake)
        self.assertFalse(ok)
        self.assertIn("Max open positions limit (3) reached", msg)

    def test_union_of_file_and_live_symbols(self):
        self.write_state(["AUSDT"])
        fake = LiveExchange(positions=[pos("BUSDT", "1"), pos("CUSDT", "-1")])
        ok, msg = self.slots(fake)
        self.assertFalse(ok)
        self.assertIn("(open 3 + pending 0 >= max 3)", msg)

    def test_file_count_kept_when_stricter(self):
        self.write_state(["AUSDT", "BUSDT", "CUSDT"])
        ok, msg = self.slots(LiveExchange())
        self.assertFalse(ok)
        self.assertIn("(open 3 + pending 0", msg)

    def test_registry_deleted_with_live_resting_entry_rejected(self):
        self.write_state([])
        fake = LiveExchange(algos=[entry_algo(algo_id=7001, symbol="BTCUSDT")])
        ok, msg = self.slots(fake)
        self.assertFalse(ok)
        self.assertIn("logs/pending_entries.json (the registry file is MISSING)", msg)
        self.assertIn("BTCUSDT STOP_MARKET algo 7001", msg)
        self.assertIn("Cancel them (or restore their registry records)", msg)
        self.assertIsNone(read_registry(self.ws), "the gate never writes the registry")

    def test_registry_deleted_without_resting_entries_passes(self):
        self.write_state([])
        fake = LiveExchange(algos=[stop(501, 95.0)], orders=[dict(resting_limit(8, "ETHUSDT", side="SELL"), reduceOnly=True)])
        ok, msg = self.slots(fake)
        self.assertTrue(ok, msg)

    def test_empty_registry_but_live_resting_entries_push_over_limit(self):
        self.write_state(["AUSDT"])
        write_registry(self.ws)  # present but empty
        fake = LiveExchange(positions=[pos("AUSDT", "1")],
                            algos=[entry_algo(algo_id=1, symbol="BTCUSDT")], orders=[resting_limit(2, "ETHUSDT")])
        ok, msg = self.slots(fake)
        self.assertFalse(ok)
        self.assertIn("(open 1 + pending 2 >= max 3)", msg)
        self.assertIn("2 symbol(s) with resting opening orders ['BTCUSDT', 'ETHUSDT']", msg)

    def test_resting_entry_on_open_symbol_counted_once(self):
        # partially filled LIMIT: position and resting remainder on the same symbol take one slot
        self.write_state(["AUSDT"])
        write_registry(self.ws, make_record(kind="LIMIT", symbol="AUSDT", entry_id="2", env="prod"))
        fake = LiveExchange(positions=[pos("AUSDT", "1")], orders=[resting_limit(2, "AUSDT")])
        ok, msg = self.slots(fake)
        self.assertTrue(ok, msg)

    def test_live_read_failure_rejected(self):
        self.write_state([])
        for endpoint in SNAPSHOT_ENDPOINTS:
            ok, msg = self.slots(LiveExchange(errors={endpoint: {"code": -1001, "msg": "Internal error"}}))
            self.assertFalse(ok, endpoint)
            self.assertIn(f"{endpoint} query failed", msg)
            self.assertIn("FAIL-CLOSED", msg)


class TestExecutorLiveSnapshot(Workspace):
    """execute_complete_trade in PROD: one snapshot per attempt, shared by Gate 0A (x2), Gate 1 and check 1d."""

    def test_snapshot_fetched_once_per_attempt(self):
        self.write_state([])
        write_registry(self.ws)
        fake = LiveExchange()
        res = self.execute(fake)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(fake.all_symbol_gets(), list(SNAPSHOT_ENDPOINTS))

    def test_forged_file_rejected_before_any_write(self):
        self.write_state([], bias="NEUTRAL")
        write_registry(self.ws)
        fake = LiveExchange(positions=[pos("BTCUSDT", "2", 100)])
        res = self.execute(fake, direction="LONG")
        self.assertFalse(res["success"])
        self.assertTrue(res["hard_gate_rejection"])
        self.assertIn("LONG_HEAVY", res["error"])
        self.assertFalse([c for c in fake.writes() if c[1] in ("/fapi/v1/order", ALGO_ENDPOINT)])
        self.assertEqual(fake.all_symbol_gets(), list(SNAPSHOT_ENDPOINTS))

    def test_position_risk_failure_rejected_before_any_write(self):
        self.write_state([])
        fake = LiveExchange(errors={"/fapi/v2/positionRisk": {"error": "timeout"}})
        res = self.execute(fake)
        self.assertFalse(res["success"])
        self.assertTrue(res["hard_gate_rejection"])
        self.assertIn("cannot read the live exchange state for the PROD gates", res["error"])
        self.assertEqual(fake.writes(), [])

    def test_deleted_registry_and_live_resting_entry_rejected(self):
        self.write_state([])
        write_guardian_state(self.ws)
        fake = LiveExchange(orders=[resting_limit(77, "ETHUSDT")])
        res = self.execute(fake)
        self.assertFalse(res["success"])
        self.assertIn("(the registry file is MISSING)", res["error"])
        self.assertIn("ETHUSDT LIMIT order 77", res["error"])
        self.assertEqual(fake.writes(), [])

    def test_deleted_session_state_rejected(self):
        write_registry(self.ws)
        fake = LiveExchange()
        res = self.execute(fake)
        self.assertFalse(res["success"])
        self.assertIn("session_state.json does not exist", res["error"])
        self.assertEqual(fake.writes(), [])

    def test_testnet_unchanged(self):
        # no session_state, no registry, live LONG_HEAVY: TESTNET makes no snapshot read and does not care
        fake = LiveExchange(positions=[pos("BTCUSDT", "2", 100)], algos=[entry_algo(algo_id=1, symbol="XRPUSDT")])
        res = self.execute(fake, env="testnet")
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(fake.all_symbol_gets(), [])
        with patch("execute_futures_trade.send_signed_request", side_effect=AssertionError("no exchange call")):
            self.assertEqual(eft.check_max_open_positions(dict(PROFILE), "testnet"), (True, None))
        ok, msg = self.gates(LiveExchange(errors={e: {"error": "x"} for e in SNAPSHOT_ENDPOINTS}), env="testnet")
        self.assertTrue(ok, msg)


# =============================================================================
# 5. Equity
# =============================================================================
class TestEquityFromLedgerInProd(Workspace):

    def test_forged_operating_balance_ignored_in_prod(self):
        self.write_state([], operating_balance={"total_wallet_balance_usdt": 1e9})
        fake = LiveExchange(balance=500.0)
        with patch("execute_futures_trade.send_signed_request", side_effect=fake):
            self.assertEqual(qre.get_account_equity("prod"), 500.0)
        self.assertIn(("GET", "/fapi/v2/balance", {}), fake.calls)

    def test_prod_live_failure_raises(self):
        self.write_state([], operating_balance={"total_wallet_balance_usdt": 1e9})
        with patch("execute_futures_trade.send_signed_request", side_effect=LiveExchange()), \
             patch("builtins.print"):
            with self.assertRaises(RuntimeError):
                qre.get_account_equity("prod")

    def test_testnet_cache_unchanged(self):
        self.write_state([], target_env="testnet", operating_balance={"total_wallet_balance_usdt": 1234.5})
        with patch("execute_futures_trade.send_signed_request", side_effect=AssertionError("no exchange call")):
            self.assertEqual(qre.get_account_equity("testnet"), 1234.5)


# =============================================================================
# 6. protect_pending_entries: record vs exchange cross-check
# =============================================================================
class RaceExchange(FakeExchange):
    """The resting entry partially fills (4 units) right before its cancel lands: positionRisk shows it afterwards."""

    def __init__(self, *a, fail_reread=False, **kw):
        super().__init__(*a, **kw)
        self.fail_reread = fail_reread
        self.cancelled = False

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if method == "DELETE" and endpoint == ALGO_ENDPOINT and str((params or {}).get("algoId")) == "7001":
            self.cancelled = True
            self.positions = [long_position(amt="4", entry="101.0", mark="101.0")]
        if self.cancelled and self.fail_reread and endpoint == "/fapi/v2/positionRisk":
            self.calls.append((method, endpoint, dict(params or {})))
            return {"code": -1001, "msg": "Internal error"}
        return super().__call__(method, endpoint, params, target_env, retry_count)


class TestPendingRecordCrossCheck(unittest.TestCase):

    def run_protect(self, fake, *records):
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        write_registry(ws, *records)
        with offline(fake, workspace=ws):
            res = eft.protect_pending_entries(target_env="testnet")
        return res, ws

    def mismatch(self, res):
        return [a for a in res["actions"] if a["type"] == "pending_record_mismatch"]

    def assertCancelledAndDropped(self, fake, res, ws, fragment):
        acts = self.mismatch(res)
        self.assertEqual(len(acts), 1, res)
        self.assertTrue(acts[0]["success"])
        self.assertTrue(acts[0]["detail"]["entry_cancelled"])
        self.assertTrue(any(fragment in m for m in acts[0]["detail"]["mismatches"]), acts[0]["detail"]["mismatches"])
        self.assertFalse(res["ok"])
        self.assertTrue(any(e["stage"] == "record_mismatch" for e in res["errors"]))
        self.assertEqual([c for c in fake.calls if c[0] == "DELETE" and c[1] == ALGO_ENDPOINT][0][2]["algoId"], 7001)
        self.assertEqual(fake.algos, [])
        self.assertEqual(read_registry(ws), {})
        self.assertFalse([c for c in fake.calls if c[0] == "POST"], "nothing is placed from an untrusted record")

    def test_side_mismatch_cancels(self):
        fake = FakeExchange([], algos=[entry_algo(side="SELL")])
        res, ws = self.run_protect(fake, make_record())
        self.assertCancelledAndDropped(fake, res, ws, "side SELL")

    def test_quantity_mismatch_cancels(self):
        fake = FakeExchange([], algos=[dict(entry_algo(), quantity="13")])
        res, ws = self.run_protect(fake, make_record())
        self.assertCancelledAndDropped(fake, res, ws, "quantity 13.0 != record total_qty 12.0")

    def test_price_mismatch_cancels(self):
        fake = FakeExchange([], algos=[entry_algo(trigger=103.0)])
        res, ws = self.run_protect(fake, make_record())
        self.assertCancelledAndDropped(fake, res, ws, "triggerPrice 103.0 != record trigger_or_limit_price 101.0")

    def test_sl_on_wrong_side_while_resting_cancels(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws = self.run_protect(fake, make_record(sl=105.0))
        self.assertCancelledAndDropped(fake, res, ws, "not on the loss side")

    def test_tp_on_wrong_side_while_resting_cancels(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws = self.run_protect(fake, make_record(tp1=99.0))
        self.assertCancelledAndDropped(fake, res, ws, "tp1_price 99.0 is not on the profit side")

    def test_within_step_and_tick_is_consistent(self):
        # FILTERS (test_exit_management): stepSize 0.001, tickSize 0.1 -> sub-half-unit noise is no mismatch
        fake = FakeExchange([], algos=[dict(entry_algo(trigger=101.04), quantity="12.0004")])
        res, ws = self.run_protect(fake, make_record())
        self.assertEqual(self.mismatch(res), [])
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(len(read_registry(ws)), 1)
        self.assertEqual(fake.writes(), [])

    def test_consistent_record_untouched(self):
        fake = FakeExchange([], algos=[entry_algo()])
        res, ws = self.run_protect(fake, make_record())
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["actions"], [])
        self.assertEqual(fake.writes(), [])

    def test_filters_unavailable_keeps_record_and_entry(self):
        fake = FakeExchange([], algos=[dict(entry_algo(), quantity="13")])
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        write_registry(ws, make_record())
        with offline(fake, workspace=ws), patch("execute_futures_trade.get_symbol_filters", return_value=None):
            res = eft.protect_pending_entries(target_env="testnet")
        self.assertFalse(res["ok"])
        self.assertEqual(res["errors"][0]["stage"], "filters")
        self.assertEqual(fake.writes(), [])
        self.assertEqual(len(read_registry(ws)), 1)

    def test_dry_run_reports_without_writes(self):
        fake = FakeExchange([], algos=[entry_algo(side="SELL")])
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        write_registry(ws, make_record())
        with offline(fake, workspace=ws):
            res = eft.protect_pending_entries(target_env="testnet", dry_run=True)
        self.assertEqual(len(self.mismatch(res)), 1)
        self.assertFalse(self.mismatch(res)[0]["success"])
        self.assertEqual(fake.writes(), [])
        self.assertEqual(len(read_registry(ws)), 1)

    def test_partial_limit_fill_with_mismatch_cancels_remainder_and_heals(self):
        limit = {"orderId": 8001, "symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "price": "101.0",
                 "origQty": "12", "reduceOnly": False}
        fake = FakeExchange([long_position(amt="4", entry="101.0", mark="101.0")], open_orders=[limit])
        res, ws = self.run_protect(fake, make_record(kind="LIMIT", entry_id="8001"))
        acts = self.mismatch(res)
        self.assertEqual(len(acts), 1)
        self.assertTrue(acts[0]["success"])
        self.assertTrue(acts[0]["detail"]["heal"]["success"])
        self.assertEqual(fake.open_orders, [])
        self.assertEqual(len([a for a in fake.algos if float(a["triggerPrice"]) > 0]), 1, "orphan heal stop only")
        # orphan heal distance from the entry (101 x (1 - ORPHAN_HEAL_SL_DISTANCE)), tick-rounded; not the record's 95
        self.assertAlmostEqual(float(fake.algos[0]["triggerPrice"]), 101.0 * (1 - eft.ORPHAN_HEAL_SL_DISTANCE), delta=0.1)
        self.assertEqual(read_registry(ws), {})

    def test_filled_with_invalid_sl_heals_and_places_no_tp(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.0")])
        res, ws = self.run_protect(fake, make_record(sl=105.0))
        acts = self.mismatch(res)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["detail"]["reason"], "filled_with_invalid_sl")
        self.assertTrue(acts[0]["success"])
        self.assertTrue(acts[0]["detail"]["heal"]["success"])
        sent = [c[2] for c in fake.calls if c[0] == "POST" and c[1] == ALGO_ENDPOINT]
        self.assertEqual(len(sent), 1)
        self.assertNotEqual(float(sent[0]["triggerPrice"]), 105.0, "the record's SL is never used")
        self.assertLess(float(sent[0]["triggerPrice"]), 101.0)
        self.assertFalse([c for c in fake.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"], "no TP placed")
        self.assertEqual(read_registry(ws), {})
        self.assertTrue(any(e["stage"] == "record_mismatch" for e in res["errors"]))

    def test_filled_with_non_positive_sl_and_existing_stop_keeps_it(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.0")], algos=[stop(501, 97.0)])
        res, ws = self.run_protect(fake, make_record(sl=0))
        acts = self.mismatch(res)
        self.assertEqual(len(acts), 1)
        self.assertTrue(acts[0]["success"])
        self.assertNotIn("heal", acts[0]["detail"])
        self.assertEqual(fake.writes(), [], "an existing protective stop is kept; nothing placed or cancelled")
        self.assertEqual(read_registry(ws), {})

    def test_filled_with_invalid_tp_places_sl_but_no_tp(self):
        fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.0")])
        res, ws = self.run_protect(fake, make_record(tp2=100.0))
        self.assertEqual([a["type"] for a in res["actions"]], ["pending_protect_sl", "pending_record_mismatch"])
        self.assertTrue(res["actions"][0]["success"])
        self.assertEqual(res["actions"][1]["detail"]["reason"], "invalid_take_profits")
        self.assertFalse([c for c in fake.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"], "no TP placed")
        self.assertEqual(read_registry(ws), {})

    def test_side_omitted_by_listing_is_not_compared(self):
        algo = entry_algo()
        del algo["side"]
        fake = FakeExchange([], algos=[algo])
        res, ws = self.run_protect(fake, make_record())
        self.assertEqual(self.mismatch(res), [])
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(fake.writes(), [])
        self.assertEqual(len(read_registry(ws)), 1)

    def test_fill_racing_the_cancel_is_protected_not_dropped(self):
        fake = RaceExchange([], algos=[entry_algo(side="SELL")])
        res, ws = self.run_protect(fake, make_record())
        acts = self.mismatch(res)
        self.assertEqual(len(acts), 1)
        self.assertTrue(acts[0]["detail"]["entry_cancelled"])
        self.assertIsNone(acts[0]["detail"]["position_amt"])
        self.assertEqual(acts[0]["detail"]["position_amt_after_cancel"], "4")
        self.assertTrue(acts[0]["detail"]["heal"]["success"])
        stops = [a for a in fake.algos if a.get("closePosition")]
        self.assertEqual(len(stops), 1, "the racing fill gets an orphan heal stop")
        self.assertNotEqual(float(stops[0]["triggerPrice"]), 95.0, "never the untrusted record SL")
        self.assertTrue(any("position protected without the record" in e["error"] for e in res["errors"]))
        self.assertEqual(read_registry(ws), {})

    def test_position_reread_failure_after_cancel_keeps_record(self):
        fake = RaceExchange([], algos=[entry_algo(side="SELL")], fail_reread=True)
        res, ws = self.run_protect(fake, make_record())
        self.assertFalse(self.mismatch(res)[0]["success"])
        self.assertTrue(any(e["stage"] == "record_mismatch_position_reread" for e in res["errors"]))
        self.assertEqual(len(read_registry(ws)), 1, "record kept for the next run")
        self.assertFalse([c for c in fake.calls if c[0] == "POST"])

    def test_missing_registry_never_blocks_protect_pending(self):
        fake = FakeExchange([long_position()])
        ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ws, True)
        with offline(fake, workspace=ws):
            res = eft.protect_pending_entries(target_env="testnet")
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(eft.load_pending_entries_status(ws), ({}, None, True))
        self.assertEqual(eft.load_pending_entries(ws), ({}, None))


if __name__ == "__main__":
    unittest.main()
