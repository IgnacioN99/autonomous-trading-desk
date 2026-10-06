#!/usr/bin/env python3
"""
test_issue_119_live_gate_inputs.py - Offline tests for issues #119 and #117 (follow-ups of #101 / PR #116).

#119
1. Gate 1 (PROD) classifies the filled positionRisk notional PLUS the opening orders resting on the exchange
   (utils/portfolio_exposure.resting_opening_legs; MCP algos without a quantity take it from their registry record,
   every live order counted once) and projects the new order: on a non-empty book an order that would tip the book
   heavy in its own direction is rejected; an empty book passes.
2. Gate 2 (PROD, standard) sizes the loss cap on min(wallet balance, balance + unrealized PnL of the live
   snapshot's positionRisk rows): losses lower it, gains never raise it, a missing uPnL on an open position rejects.
3. The hook's session_state.json checks are a cache-based pre-check: a forged fresh DELTA_BALANCED file passes the
   hook's session block while the executor, anchored to the live exchange, rejects the LONG.
#117
4. compute_exposure: a missing positionAmt, or a missing markPrice on an open position, raises (never dropped or
   zeroed); sync_session_state writes the fail-closed error state instead of crashing.
5. TESTNET unchanged.

No network (urlopen is blocked) and every file goes to a temp workspace, never the real logs/.
"""

import io
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
for p in (SCRIPTS_DIR, HOOKS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import sync_session_state as sss
import pre_trade_guard
from utils import portfolio_exposure as pe
import test_issue_101_exchange_anchored_gates as t101   # module import only: its tests are not collected twice
from test_pending_entries import make_record, write_registry

LiveExchange = t101.LiveExchange
pos = t101.pos
resting_limit = t101.resting_limit
PROFILE = t101.PROFILE


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def mcp_algo(algo_id, symbol, side="BUY", trigger=100.0):
    """A resting conditional entry as the MCP bridge lists it (send_signed_request L355-369): no quantity."""
    return {"algoId": algo_id, "symbol": symbol, "side": side, "triggerPrice": float(trigger),
            "orderType": "STOP_MARKET", "closePosition": False}


def keys_algo(algo_id, symbol, side="BUY", trigger=100.0, qty="12"):
    return {"algoId": algo_id, "symbol": symbol, "side": side, "orderType": "STOP_MARKET",
            "triggerPrice": str(trigger), "quantity": qty, "closePosition": False, "reduceOnly": False}


def info(symbol, side, price, quantity=None, kind="LIMIT", oid=1, executed=None):
    return {"symbol": symbol, "source": "order" if kind == "LIMIT" else "algo", "kind": kind, "id": oid,
            "type": kind, "side": side, "price": price, "quantity": quantity, "executed_qty": executed}


def prod_record(**kw):
    kw.setdefault("env", "prod")
    return make_record(**kw)


class GateWorkspace(t101.Workspace):
    """check_mechanical_gates in PROD with a chosen size / SL (the t101 gates() helper fixes qty 1, SL 98)."""

    def gate(self, fake, direction="LONG", qty=1.0, sl=None, env="prod", equity=10000.0, **kw):
        if sl is None:
            sl = 98.0 if direction == "LONG" else 102.0
        tp1 = 105.0 if direction == "LONG" else 95.0
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("quant_risk_engine.get_account_equity", return_value=equity), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)):
            return eft.check_mechanical_gates(direction, 100.0, sl, tp1, qty, 3, target_env=env, **kw)


# =============================================================================
# Pure functions (utils/portfolio_exposure.py)
# =============================================================================
class TestRestingOpeningLegs(unittest.TestCase):

    def test_sides_and_notional(self):
        legs = pe.resting_opening_legs([info("ETHUSDT", "SELL", 98.0, "1.5"), info("BTCUSDT", "BUY", 50.0, "2")])
        self.assertEqual([(l["symbol"], l["side"], l["notional"], l["source"]) for l in legs],
                         [("ETHUSDT", "SHORT", 147.0, "live"), ("BTCUSDT", "LONG", 100.0, "live")])

    def test_partially_filled_limit_counts_only_the_remainder(self):
        legs = pe.resting_opening_legs([info("ETHUSDT", "BUY", 100.0, "2", executed="0.5")])
        self.assertAlmostEqual(legs[0]["qty"], 1.5)
        self.assertEqual(pe.resting_opening_legs([info("ETHUSDT", "BUY", 100.0, "2", executed="2")]), [])

    def test_mcp_algo_takes_quantity_from_its_registry_record_once(self):
        rec = prod_record(kind="STOP_MARKET", entry_id="7001", symbol="BTCUSDT", total_qty=3.0)
        other = prod_record(kind="LIMIT", entry_id="55", symbol="XRPUSDT", total_qty=9.0)   # no live order: ignored
        legs = pe.resting_opening_legs([info("BTCUSDT", "BUY", 100.0, None, kind="STOP_MARKET", oid=7001)],
                                       [rec, other])
        self.assertEqual(len(legs), 1)
        self.assertEqual((legs[0]["qty"], legs[0]["notional"], legs[0]["source"]), (3.0, 300.0, "registry"))

    def test_live_quantity_wins_over_registry_duplicate(self):
        rec = prod_record(kind="STOP_MARKET", entry_id="7001", symbol="BTCUSDT", total_qty=99.0)
        legs = pe.resting_opening_legs([info("BTCUSDT", "BUY", 101.0, "12", kind="STOP_MARKET", oid=7001)], [rec])
        self.assertEqual(len(legs), 1)
        self.assertEqual((legs[0]["qty"], legs[0]["source"]), (12.0, "live"))

    def test_registry_match_requires_same_id_and_kind(self):
        rec = prod_record(kind="LIMIT", entry_id="7001", symbol="BTCUSDT")
        with self.assertRaises(ValueError):
            pe.resting_opening_legs([info("BTCUSDT", "BUY", 100.0, None, kind="STOP_MARKET", oid=7001)], [rec])
        with self.assertRaises(ValueError):
            pe.resting_opening_legs([info("BTCUSDT", "BUY", 100.0, None, kind="STOP_MARKET", oid=7002)],
                                    [prod_record(kind="STOP_MARKET", entry_id="7001", symbol="BTCUSDT")])

    def test_fallback_match_without_id_by_symbol_side_price(self):
        rec = prod_record(kind="STOP_MARKET", entry_id="7001", symbol="BTCUSDT", total_qty=2.0)  # price 101, BUY
        legs = pe.resting_opening_legs([info("BTCUSDT", "BUY", 101.0, None, kind="STOP_MARKET", oid=None)], [rec])
        self.assertEqual(legs[0]["qty"], 2.0)
        for side, price in (("SELL", 101.0), ("BUY", 102.0)):
            with self.assertRaises(ValueError):
                pe.resting_opening_legs([info("BTCUSDT", side, price, None, kind="STOP_MARKET", oid=None)], [rec])

    def test_fail_closed_inputs(self):
        cases = ([info("BTCUSDT", "BUY", 100.0, None)],            # no quantity, no record
                 [info("BTCUSDT", "HOLD", 100.0, "1")],            # unknown side
                 [info("BTCUSDT", "BUY", 100.0, "abc")],           # unparseable quantity
                 [info("BTCUSDT", "BUY", 0.0, "1")],               # no price
                 [info("BTCUSDT", "BUY", 100.0, "1", executed="x")])
        for resting in cases:
            with self.assertRaises(ValueError, msg=repr(resting)):
                pe.resting_opening_legs(resting, [])
        bad_rec = prod_record(kind="LIMIT", entry_id="1", symbol="BTCUSDT", total_qty=0)
        with self.assertRaises(ValueError):
            pe.resting_opening_legs([info("BTCUSDT", "BUY", 100.0, None, oid=1)], [bad_rec])

    def test_book_exposure(self):
        self.assertEqual(pe.book_exposure(0.0, 0.0)["delta_bias"], pe.DELTA_BALANCED)
        self.assertEqual(pe.book_exposure(220.0, 100.0)["delta_bias"], pe.LONG_HEAVY)
        self.assertEqual(pe.book_exposure(100.0, 220.0)["delta_bias"], pe.SHORT_HEAVY)
        self.assertEqual(pe.book_exposure(200.0, 100.0)["delta_bias"], pe.DELTA_BALANCED)

    def test_project_order(self):
        book = pe.book_exposure(100.0, 100.0)
        self.assertEqual(pe.project_order(book, True, 120.0)["delta_bias"], pe.LONG_HEAVY)
        self.assertEqual(pe.project_order(book, False, 120.0)["delta_bias"], pe.SHORT_HEAVY)
        self.assertEqual(pe.project_order(book, True, 100.0)["delta_bias"], pe.DELTA_BALANCED)
        self.assertEqual(pe.project_order(pe.book_exposure(100.0, 200.0), True, 120.0)["delta_bias"],
                         pe.DELTA_BALANCED)


class TestUnrealizedPnl(unittest.TestCase):

    def test_sum_over_open_positions(self):
        rows = [dict(pos("BTCUSDT", "1"), unRealizedProfit="-150.5"), dict(pos("ETHUSDT", "-1"), unRealizedProfit="20"),
                {"symbol": "XRPUSDT", "positionAmt": "0"}]          # flat row without uPnL / markPrice: fine
        self.assertAlmostEqual(pe.unrealized_pnl_total(pe.compute_exposure(rows)), -130.5)

    def test_missing_or_unparseable_on_open_position_raises(self):
        for value in (None, "n/a"):
            row = pos("BTCUSDT", "1")
            if value is None:
                del row["unRealizedProfit"]
            else:
                row["unRealizedProfit"] = value
            with self.assertRaises(ValueError):
                pe.unrealized_pnl_total(pe.compute_exposure([row]))


# =============================================================================
# #117: compute_exposure strictness + sync_session_state fail-closed state
# =============================================================================
class TestComputeExposureStrict(unittest.TestCase):

    def test_missing_position_amt_raises(self):
        with self.assertRaises(ValueError):
            pe.compute_exposure([{"symbol": "BTCUSDT", "markPrice": "100"}])

    def test_missing_mark_price_on_open_position_raises(self):
        with self.assertRaises(ValueError):
            pe.compute_exposure([{"symbol": "BTCUSDT", "positionAmt": "1"}])

    def test_zero_rows_may_lack_mark_price_and_entry_price_defaults(self):
        exp = pe.compute_exposure([{"symbol": "XRPUSDT", "positionAmt": "0"},
                                   {"symbol": "BTCUSDT", "positionAmt": "1", "markPrice": "100"}])
        self.assertEqual(exp["total_active_positions"], 1)
        self.assertEqual(exp["active_positions"][0]["entry_price"], 0.0)


class TestSyncMalformedRowFailsClosed(unittest.TestCase):

    def run_sync(self, fake):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "session_state.json")
        with patch.object(sss, "LOGS_DIR", tmp), patch.object(sss, "STATE_FILE", path), \
             patch.object(sss, "AUDIT_LOG", os.path.join(tmp, "trades_audit.jsonl")), \
             patch.dict(sys.modules, {"shadow_tracker": None}), \
             patch("execute_futures_trade.send_signed_request", side_effect=fake):
            state = sss.sync_session_state(target_env="prod")
        with open(path, "r", encoding="utf-8") as f:
            return state, json.load(f)

    def test_malformed_row_writes_the_error_state(self):
        bad = LiveExchange(positions=[pos("BTCUSDT", "1"), {"symbol": "ETHUSDT", "positionAmt": "2"}])
        state, on_disk = self.run_sync(bad)
        self.assertEqual(state, on_disk)
        self.assertIs(state["is_valid"], False)
        self.assertIn("Malformed positionRisk data from ledger", state["error"])
        self.assertIn("markPrice", state["error"])
        self.assertEqual(state["portfolio_exposure"]["delta_bias"], "UNKNOWN")
        self.assertEqual(state["portfolio_exposure"]["total_active_positions"], 0)
        self.assertEqual(state["active_positions"], [])
        # same shape as the positionRisk failure path
        failed, _ = self.run_sync(LiveExchange(errors={"/fapi/v2/positionRisk": {"code": -1001, "msg": "x"}}))
        self.assertIn("Failed to fetch positionRisk from ledger", failed["error"])
        self.assertEqual(sorted(state), sorted(failed))
        self.assertEqual(sorted(state["portfolio_exposure"]), sorted(failed["portfolio_exposure"]))
        self.assertEqual(state["closed_today_summary"], failed["closed_today_summary"])


# =============================================================================
# #119 Gate 1: resting opening orders + new-order projection
# =============================================================================
class TestGate1RestingAndNewOrder(GateWorkspace):

    def setUp(self):
        super().setUp()
        self.write_state([])
        write_registry(self.ws)   # present and empty unless a test writes records

    def test_flat_book_large_order_passes(self):
        ok, msg = self.gate(LiveExchange(), "LONG", qty=10.0)
        self.assertTrue(ok, msg)
        ok, msg = self.gate(LiveExchange(), "SHORT", qty=10.0)
        self.assertTrue(ok, msg)

    def test_balanced_book_order_that_tips_it_heavy_rejects(self):
        book = LiveExchange(positions=[pos("BTCUSDT", "1", 100), pos("ETHUSDT", "-1", 100)])
        ok, msg = self.gate(book, "LONG", qty=1.0)          # post 200/100: +0.33, balanced
        self.assertTrue(ok, msg)
        ok, msg = self.gate(book, "LONG", qty=1.2)          # post 220/100: +0.375
        self.assertFalse(ok)
        self.assertIn("This LONG order (120.00 USDT notional) would push the portfolio into LONG_HEAVY", msg)
        # symmetric: the same size as a SHORT tips it SHORT_HEAVY (100/220: -0.375); a smaller one passes
        ok, msg = self.gate(book, "SHORT", qty=1.2)
        self.assertFalse(ok)
        self.assertIn("This SHORT order (120.00 USDT notional) would push the portfolio into SHORT_HEAVY", msg)
        self.assertTrue(self.gate(book, "SHORT", qty=1.0)[0])

    def test_short_that_flips_a_long_heavy_book_rejects_rebalancing_short_passes(self):
        book = LiveExchange(positions=[pos("BTCUSDT", "2", 100)])        # 200 long: LONG_HEAVY
        ok, msg = self.gate(book, "SHORT", qty=6.0)         # post 200/600: -0.50
        self.assertFalse(ok)
        self.assertIn("This SHORT order (600.00 USDT notional) would push the portfolio into SHORT_HEAVY", msg)
        ok, msg = self.gate(book, "SHORT", qty=2.0)         # post 200/200: 0.00
        self.assertTrue(ok, msg)

    def test_book_of_only_resting_orders_applies_the_new_order_rule(self):
        write_registry(self.ws, prod_record(kind="LIMIT", entry_id="8", symbol="ETHUSDT", direction="LONG"),
                       prod_record(kind="LIMIT", entry_id="9", symbol="XRPUSDT", direction="SHORT"))
        fake = LiveExchange(orders=[resting_limit(8, "ETHUSDT", side="BUY", price=100.0, qty="1"),
                                    resting_limit(9, "XRPUSDT", side="SELL", price=100.0, qty="1")])
        ok, msg = self.gate(fake, "LONG", qty=1.2)          # no positions; post 220/100: +0.375
        self.assertFalse(ok)
        self.assertIn("would push the portfolio into LONG_HEAVY", msg)
        self.assertIn("resting long 100.00 / short 100.00", msg)
        ok, msg = self.gate(fake, "LONG", qty=1.0)          # post 200/100: +0.33
        self.assertTrue(ok, msg)

    def test_order_against_the_lean_passes(self):
        book = LiveExchange(positions=[pos("BTCUSDT", "1", 100), pos("ETHUSDT", "-2", 100)])   # -0.33
        ok, msg = self.gate(book, "LONG", qty=1.2)          # post 220/200: +0.05
        self.assertTrue(ok, msg)

    def test_resting_sell_limit_counts_as_short(self):
        write_registry(self.ws, prod_record(kind="LIMIT", entry_id="8", symbol="ETHUSDT", direction="SHORT"))
        fake = LiveExchange(orders=[resting_limit(8, "ETHUSDT", side="SELL", price=100.0, qty="3")])
        ok, msg = self.gate(fake, "SHORT", qty=0.1)
        self.assertFalse(ok)
        self.assertIn("SHORT_HEAVY state", msg)
        self.assertIn("Source: live exchange positionRisk + resting opening orders", msg)
        self.assertIn("resting long 0.00 / short 300.00", msg)
        self.assertTrue(self.gate(fake, "LONG", qty=0.1)[0])

    def test_registry_only_mcp_algo_is_counted(self):
        # short 400 filled + MCP BUY algo 2 @ 100 (registry total_qty 2): pre -0.33, a small SHORT passes; without
        # the algo the book would be -1.0 (SHORT_HEAVY) and the SHORT would be rejected
        write_registry(self.ws, prod_record(kind="STOP_MARKET", entry_id="7001", symbol="BTCUSDT", total_qty=2.0))
        fake = LiveExchange(positions=[pos("ETHUSDT", "-4", 100)], algos=[mcp_algo(7001, "BTCUSDT", "BUY", 100.0)])
        ok, msg = self.gate(fake, "SHORT", qty=0.1)
        self.assertTrue(ok, msg)
        fake_no_algo = LiveExchange(positions=[pos("ETHUSDT", "-4", 100)])
        self.assertFalse(self.gate(fake_no_algo, "SHORT", qty=0.1)[0])

    def test_live_and_registry_duplicate_counted_once(self):
        # short 1200 filled + KEYS algo BUY 12 @ 101 (also in the registry): counted once -> balanced, the LONG
        # passes; counted twice (2424 long) it would be LONG_HEAVY
        write_registry(self.ws, prod_record(kind="STOP_MARKET", entry_id="7001", symbol="BTCUSDT", total_qty=12.0))
        fake = LiveExchange(positions=[pos("ETHUSDT", "-12", 100)], algos=[keys_algo(7001, "BTCUSDT", "BUY", 101.0)])
        ok, msg = self.gate(fake, "LONG", qty=0.1)
        self.assertTrue(ok, msg)

    def test_quantityless_order_without_record_rejects_in_gate1(self):
        fake = LiveExchange(algos=[mcp_algo(9001, "BTCUSDT", "BUY", 100.0)])
        ok, msg = self.gate(fake, "LONG", qty=0.1)
        self.assertFalse(ok)
        self.assertIn("FAIL-CLOSED — resting opening order BTCUSDT STOP_MARKET 9001", msg)
        self.assertIn("delta-neutral gate cannot measure the portfolio", msg)

    def test_quantityless_order_without_record_rejected_by_1d_in_the_executor(self):
        # pairing: execute_complete_trade rejects it earlier, at the unregistered-entry check (1d)
        fake = LiveExchange(algos=[mcp_algo(9001, "BTCUSDT", "BUY", 100.0)])
        res = self.execute(fake)
        self.assertFalse(res["success"])
        self.assertTrue(res["hard_gate_rejection"])
        self.assertIn("are not in logs/pending_entries.json", res["error"])
        self.assertIn("BTCUSDT STOP_MARKET algo 9001", res["error"])
        self.assertEqual(fake.writes(), [])

    def test_unreadable_registry_rejects(self):
        with open(os.path.join(self.ws, "logs", "pending_entries.json"), "w") as f:
            f.write("{broken")
        with patch("execute_futures_trade.check_max_open_positions", return_value=(True, None)):
            ok, msg = self.gate(LiveExchange(), "LONG")
        self.assertFalse(ok)
        self.assertIn("cannot measure the resting opening orders", msg)

    def test_executor_rejects_order_that_tips_the_book(self):
        # execute(): SOLUSDT LONG, margin 10 x 3 = 30 USDT notional at 100 on a 50/50 book -> 80/50 = +0.23 passes;
        # margin 20 -> 110/50 = +0.375 rejects before any order (rejected attempt first: a fill would take a slot)
        fake = LiveExchange(positions=[pos("BTCUSDT", "0.5", 100), pos("ETHUSDT", "-0.5", 100)])
        res = self.execute(fake, margin_usdt=20.0)
        self.assertFalse(res["success"])
        self.assertTrue(res["hard_gate_rejection"])
        self.assertIn("would push the portfolio into LONG_HEAVY", res["error"])
        self.assertFalse([c for c in fake.writes() if c[1] == "/fapi/v1/order"])
        fake = LiveExchange(positions=[pos("BTCUSDT", "0.5", 100), pos("ETHUSDT", "-0.5", 100)])
        res = self.execute(fake)
        self.assertTrue(res["success"], res.get("error"))

    def test_testnet_skips_gate1(self):
        book = LiveExchange(positions=[pos("BTCUSDT", "1", 100), pos("ETHUSDT", "-1", 100)],
                            algos=[mcp_algo(9001, "BTCUSDT", "BUY", 100.0)])
        ok, msg = self.gate(book, "LONG", qty=5.0, env="testnet")
        self.assertTrue(ok, msg)


# =============================================================================
# #119 Gate 2: loss cap on min(wallet balance, balance + unrealized PnL)
# =============================================================================
class TestGate2UnrealizedPnl(GateWorkspace):
    """Book 10000 long / 10000 short (Gate 1 stays balanced); cap at 10000 equity = 10000 x 0.5% x 1.25 = 62.50."""

    def setUp(self):
        super().setUp()
        self.write_state([])
        write_registry(self.ws)

    def book(self, btc_upnl="0", eth_upnl="0"):
        return LiveExchange(positions=[dict(pos("BTCUSDT", "100", 100), unRealizedProfit=btc_upnl),
                                       dict(pos("ETHUSDT", "-100", 100), unRealizedProfit=eth_upnl)])

    def test_unrealized_loss_lowers_the_cap(self):
        ok, msg = self.gate(self.book(), "LONG", qty=31.0)            # loss 62.00 <= 62.50
        self.assertTrue(ok, msg)
        ok, msg = self.gate(self.book(btc_upnl="-150", eth_upnl="-50"), "LONG", qty=31.0)   # cap 9800 -> 61.25
        self.assertFalse(ok)
        self.assertIn("Monetary risk exceeds allowed cap ($62.00 > $61.25 USDT", msg)
        self.assertIn("equity: $9800.00 = min(wallet balance $10000.00, balance + unrealized PnL $-200.00)", msg)

    def test_unrealized_gain_does_not_raise_the_cap(self):
        ok, msg = self.gate(self.book(btc_upnl="200"), "LONG", qty=31.3)   # loss 62.60 > 62.50 even with +200
        self.assertFalse(ok)
        self.assertIn("($62.60 > $62.50 USDT", msg)
        self.assertIn("equity: $10000.00", msg)
        self.assertTrue(self.gate(self.book(btc_upnl="200"), "LONG", qty=31.0)[0])

    def test_missing_unrealized_pnl_on_open_position_rejects(self):
        fake = self.book()
        del fake.positions[0]["unRealizedProfit"]
        ok, msg = self.gate(fake, "LONG", qty=1.0)
        self.assertFalse(ok)
        self.assertIn("FAIL-CLOSED — cannot read the unrealized PnL of the open positions", msg)
        # a flat row without uPnL is fine
        flat = LiveExchange(positions=[{"symbol": "XRPUSDT", "positionAmt": "0"}])
        self.assertTrue(self.gate(flat, "LONG", qty=1.0)[0])

    def test_testnet_keeps_its_fallback(self):
        fake = self.book(btc_upnl="-5000")
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("quant_risk_engine.get_account_equity", side_effect=RuntimeError("down")), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)):
            ok, msg = eft.check_mechanical_gates("LONG", 100.0, 98.0, 105.0, 31.0, 3, target_env="testnet")
        self.assertTrue(ok, msg)


# =============================================================================
# #119 hook: cache-based pre-check; the executor's live-anchored gates are authoritative
# =============================================================================
class TestHookIsACachePreCheck(t101.Workspace):

    def run_guard(self, payload):
        stdin, stdout = sys.stdin, sys.stdout
        try:
            sys.stdin = io.StringIO(json.dumps(payload))
            sys.stdout = io.StringIO()
            with patch("pre_trade_guard.find_workspace_root", return_value=self.ws):
                pre_trade_guard.main()
            return json.loads(sys.stdout.getvalue().strip())
        finally:
            sys.stdin, sys.stdout = stdin, stdout

    def test_forged_fresh_balanced_file_passes_hook_but_executor_rejects(self):
        now = int(time.time())
        os.makedirs(os.path.join(self.ws, "logs", "evaluations"), exist_ok=True)
        with open(os.path.join(self.ws, "logs", "evaluations", "latest_dossier.json"), "w", encoding="utf-8") as f:
            json.dump({"timestamp_ts": now, "valid_until_ts": now + 1200, "evaluator_agent": "isolated_market_evaluator",
                       "status": "APPROVED", "approved_symbols": ["BTCUSDT"],
                       "approved_candidates": [{"symbol": "BTCUSDT", "direction": "LONG", "leverage": 3}]}, f)
        self.write_state([], bias="DELTA_BALANCED")          # forged: fresh, valid, balanced, 0 positions
        res = self.run_guard({"toolCall": {"name": "run_command", "args": {
            "CommandLine": "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --leverage 3 "
                           "--env testnet"}}})
        self.assertEqual(res.get("decision"), "allow", res)  # the hook's session block reads only the cache
        self.write_state([], bias="LONG_HEAVY")               # ...and trusts it: a heavy cache is denied
        heavy = self.run_guard({"toolCall": {"name": "run_command", "args": {
            "CommandLine": "python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --leverage 3 "
                           "--env testnet"}}})
        self.assertEqual(heavy.get("decision"), "deny", heavy)
        self.assertIn("Delta-Neutral Hard Gate", heavy.get("reason", ""))
        self.write_state([], bias="DELTA_BALANCED")
        write_registry(self.ws)
        live = LiveExchange(positions=[pos("BTCUSDT", "2", 100)])       # the exchange is LONG_HEAVY
        ok, msg = self.gates(live, "LONG")
        self.assertFalse(ok)
        self.assertIn("LONG_HEAVY state", msg)
        self.assertIn("Source: live exchange positionRisk", msg)


if __name__ == "__main__":
    unittest.main()
