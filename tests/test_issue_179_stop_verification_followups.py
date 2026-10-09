#!/usr/bin/env python3
"""
test_issue_179_stop_verification_followups.py - Offline tests for issue #179 (follow-ups to #156/#157).

1+2. The MARKET-entry pre-placement stop snapshot is read BEFORE the entry order (one symbol-scoped read, one short
     retry); a persistent failure keeps today's fail-closed behaviour (id-less placement response -> auto-destruct).
3.   The id-less fallback of verify_placed_stop accepts only a NEW closePosition stop at the SL (with an id).
4.   _stop_trigger_tolerance (tick x1.01, else 0.05%) is the one tolerance of the four stop checks.
8.   A stop kept after -4130 shows its listed trigger (kept_trigger_price) in the pending_protect_sl action and the
     audit record; sl_price stays the planned SL.

No network: every exchange call is faked (send_signed_request), report_agent_issue.report_issue is mocked, files go to
temp dirs; no .env credential is read.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, call

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
from test_exit_management import FakeExchange, offline, long_position, stop, ALGO_ENDPOINT
from test_pending_entries import ExecutorHarness, make_record, write_registry, read_jsonl, ORDER_ENDPOINT
from test_issue_36_prearm_resting_stop import run_protect, types, PROD_PROFILE, MINUS_4130
from test_issue_156_157_stop_lifecycle import (market_execute, lost_sl_response, ReporterMocked, READ_ERROR,
                                               ALGO_READ)

LOST = {"code": -1007, "msg": "Timeout waiting for response from backend server."}


def entry_index(calls):
    """Index of the entry order POST (the first non-reduce-only order on /fapi/v1/order)."""
    return next(i for i, c in enumerate(calls)
                if c[0] == "POST" and c[1] == ORDER_ENDPOINT and c[2].get("reduceOnly") != "true")


def symbol_algo_reads(calls, symbol="SOLUSDT", before=None):
    """Indexes of the symbol-scoped openAlgoOrders GETs (the all-symbol PROD live snapshot has no symbol)."""
    end = len(calls) if before is None else before
    return [i for i, c in enumerate(calls[:end])
            if c[0] == "GET" and c[1] == ALGO_READ and c[2].get("symbol") == symbol]


def fail_first_symbol_reads(send, fx, n):
    """The first n symbol-scoped openAlgoOrders reads fail (recorded in fx.calls); the rest go to send."""
    state = {"n": n}

    def wrapped(method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        if method == "GET" and endpoint == ALGO_READ and params.get("symbol") and state["n"] > 0:
            state["n"] -= 1
            fx.calls.append((method, endpoint, params))
            return dict(READ_ERROR)
        return send(method, endpoint, params, target_env)
    return wrapped


def created_row_as(send, fx, algo_id=9100, **fields):
    """lost_sl_response variant: the stop created by the lost placement is listed with `fields` overridden."""
    def wrapped(method, endpoint, params=None, target_env=None, retry_count=0):
        res = send(method, endpoint, params, target_env)
        for a in fx.algos:
            if a.get("algoId") == algo_id:
                a.update(fields)
        return res
    return wrapped


def lost_without_stop(fx):
    """Every closePosition SL POST loses its response (-1007, no id) and creates nothing."""
    def send(method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        if method == "POST" and endpoint == ALGO_ENDPOINT and params.get("closePosition") == "true":
            fx.calls.append((method, endpoint, params))
            return dict(LOST)
        return fx(method, endpoint, params, target_env)
    return send


# ---------------------------------------------------------------------------------------------
# Items 1 + 2: snapshot before the entry, one short retry
# ---------------------------------------------------------------------------------------------
class TestSnapshotBeforeEntry(ReporterMocked):

    def test_snapshot_read_precedes_the_entry_post_exactly_once(self):
        fx = FakeExchange([])
        res, mock_abort = market_execute(fx)
        self.assertTrue(res["success"], res.get("error"))
        entry = entry_index(fx.calls)
        reads = symbol_algo_reads(fx.calls, before=entry)
        self.assertEqual(len(reads), 1, "one symbol-scoped snapshot read before the entry")
        self.assertEqual([c for c in fx.calls[:entry] if c[0] in ("POST", "DELETE")], [],
                         "no write before the snapshot and entry (margin/leverage are answered by market_execute)")
        sl_post = next(i for i, c in enumerate(fx.calls) if c[0] == "POST" and c[1] == ALGO_ENDPOINT)
        self.assertEqual(symbol_algo_reads(fx.calls, before=sl_post), reads,
                         "no snapshot read left between the fill and the SL placement")

    def test_snapshot_uses_the_short_retry(self):
        self.assertEqual(eft.PRE_ENTRY_STOP_SNAPSHOT_RETRY_DELAYS, (0.3,))
        with patch("execute_futures_trade.get_open_stop_orders_with_retry",
                   wraps=eft.get_open_stop_orders_with_retry) as spy:
            res, _ = market_execute(FakeExchange([]))
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(spy.call_args_list[0],
                         call("SOLUSDT", "SELL", target_env="testnet", retry_delays=(0.3,)))

    def test_transient_failure_recovered_by_one_retry(self):
        fx = FakeExchange([])
        with patch("execute_futures_trade.get_open_stop_orders_with_retry",
                   wraps=eft.get_open_stop_orders_with_retry) as spy:
            res, mock_abort = market_execute(fail_first_symbol_reads(lost_sl_response(fx), fx, 1))
        self.assertEqual(spy.call_args_list[0].kwargs["retry_delays"], (0.3,), "the short delay, not 0.8+1.0+1.2")
        self.assertTrue(res["success"], res.get("error"))
        mock_abort.assert_not_called()
        self.assertEqual(res["sl_algo_order"]["algoId"], 9100, "lost response verified against the known snapshot")
        self.assertEqual(len(symbol_algo_reads(fx.calls, before=entry_index(fx.calls))), 2, "read + one retry")

    def test_persistent_failure_keeps_auto_destruct_on_id_less_response(self):
        fx = FakeExchange([])
        res, mock_abort = market_execute(lost_sl_response(fx, fail_reads_before_post=True))
        self.assertTrue(res["emergency_abort"])
        mock_abort.assert_called_once()
        self.assertEqual(len(symbol_algo_reads(fx.calls, before=entry_index(fx.calls))), 2,
                         "one retry only, never the long STOP_VERIFY_RETRY_DELAYS")

    def test_persistent_failure_with_an_id_response_still_verifies(self):
        fx = FakeExchange([])
        res, mock_abort = market_execute(fail_first_symbol_reads(fx, fx, 2))
        self.assertTrue(res["success"], res.get("error"))
        mock_abort.assert_not_called()


class TestSnapshotBeforeEntryProd(ExecutorHarness):

    def test_prod_market_entry_reads_the_snapshot_before_the_entry(self):
        with patch("report_agent_issue.report_issue"):
            res = self.execute(env="prod", order_type="MARKET")
        self.assertTrue(res["success"], res.get("error"))
        entry = entry_index(self.calls)
        self.assertEqual(len(symbol_algo_reads(self.calls, before=entry)), 1)


# ---------------------------------------------------------------------------------------------
# Item 3: closePosition filter, id-less rows
# ---------------------------------------------------------------------------------------------
class TestIdLessFallbackFilter(ReporterMocked):

    def test_reduce_only_new_stop_at_sl_not_accepted(self):
        fx = FakeExchange([])
        send = created_row_as(lost_sl_response(fx), fx, closePosition=False, reduceOnly=True, quantity="0.3")
        res, mock_abort = market_execute(send)
        self.assertTrue(res["emergency_abort"])
        mock_abort.assert_called_once()

    def test_close_position_new_stop_at_sl_accepted(self):
        fx = FakeExchange([])
        res, mock_abort = market_execute(lost_sl_response(fx))
        self.assertTrue(res["success"], res.get("error"))
        mock_abort.assert_not_called()
        self.assertEqual(res["sl_algo_order"]["algoId"], 9100)

    def test_stop_in_the_snapshot_not_accepted(self):
        fx = FakeExchange([], algos=[stop(555, 97.0, symbol="SOLUSDT")])
        res, mock_abort = market_execute(lost_without_stop(fx))
        self.assertTrue(res["emergency_abort"])
        mock_abort.assert_called_once()

    def test_new_id_less_stop_not_mistaken_for_new(self):
        fx = FakeExchange([])
        res, mock_abort = market_execute(created_row_as(lost_sl_response(fx), fx, algoId=None))
        self.assertTrue(res["emergency_abort"], "an id-less row can never be proven new")
        mock_abort.assert_called_once()

    def test_id_less_row_in_the_snapshot_does_not_hide_the_new_stop(self):
        idless = dict(stop(None, 97.0, symbol="SOLUSDT"))
        fx = FakeExchange([], algos=[idless])
        res, mock_abort = market_execute(lost_sl_response(fx))
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["sl_algo_order"]["algoId"], 9100)


# ---------------------------------------------------------------------------------------------
# Item 4: one tolerance helper
# ---------------------------------------------------------------------------------------------
class TestStopTriggerTolerance(ReporterMocked):

    def test_helper_values(self):
        self.assertAlmostEqual(eft._stop_trigger_tolerance(0.1, 100000.0), 0.101)
        self.assertAlmostEqual(eft._stop_trigger_tolerance("0.01", 97.0), 0.0101)
        for unknown in (None, 0, "", "x"):
            self.assertAlmostEqual(eft._stop_trigger_tolerance(unknown, 100.0), 0.05)
        self.assertAlmostEqual(eft._stop_trigger_tolerance(None, -100.0), 0.05)

    def test_wait_for_stop_confirmation_one_tick_on_a_high_price(self):
        # the old max(tick x1.01, 0.05%) form accepted any trigger within 50 of 100000
        far = FakeExchange([], algos=[stop(5, 100000.5)])
        near = FakeExchange([], algos=[stop(5, 100000.1)])
        with offline(far):
            self.assertEqual(eft.wait_for_stop_confirmation("BTCUSDT", "SELL", 100000.0, tick_size=0.1,
                                                            target_env="testnet"), (False, None))
        with offline(near):
            ok, info = eft.wait_for_stop_confirmation("BTCUSDT", "SELL", 100000.0, tick_size=0.1, target_env="testnet")
        self.assertTrue(ok)
        self.assertEqual(info["algoId"], 5)
        with offline(FakeExchange([], algos=[stop(5, 100040.0)])):
            self.assertTrue(eft.wait_for_stop_confirmation("BTCUSDT", "SELL", 100000.0, target_env="testnet")[0],
                            "no tick: 0.05% as before")

    def test_wait_for_stop_confirmation_uses_the_helper(self):
        with offline(FakeExchange([], algos=[stop(5, 101.0)])), \
             patch("execute_futures_trade._stop_trigger_tolerance", return_value=2.0) as helper:
            self.assertTrue(eft.wait_for_stop_confirmation("BTCUSDT", "SELL", 100.0, tick_size=0.1,
                                                           target_env="testnet")[0])
        helper.assert_called_with(0.1, 100.0)

    def test_ensure_entry_stop_minus_4130_fallback_uses_the_helper(self):
        def ensure(tolerance=None):
            fake = FakeExchange([], reject_new_stops=True, reject_response=MINUS_4130)
            state = {"fail": 0}

            def send(method, endpoint, params=None, target_env=None, retry_count=0):
                if method == "GET" and endpoint == ALGO_READ and state["fail"] > 0:
                    state["fail"] -= 1
                    return dict(READ_ERROR)
                res = fake(method, endpoint, params, target_env)
                if method == "POST" and endpoint == ALGO_ENDPOINT:
                    fake.algos.append(stop(9, 94.0))   # looser than the SL 95 by 1.0
                    state["fail"] = 1 + len(eft.STOP_VERIFY_RETRY_DELAYS)   # every confirmation read fails
                return res
            with offline(send), patch("execute_futures_trade._stop_trigger_tolerance",
                                      side_effect=(lambda t, p: tolerance) if tolerance else
                                      eft._stop_trigger_tolerance):
                return eft._ensure_entry_stop("BTCUSDT", "SELL", 95.0, tick_size=0.1, target_env="testnet")
        self.assertFalse(ensure()["verified"])
        self.assertEqual(ensure(tolerance=2.0)["source"], "kept")

    def test_verify_placed_stop_uses_the_helper(self):
        res, _ = market_execute(lost_sl_response(FakeExchange([]), trigger=96.5))
        self.assertTrue(res["emergency_abort"], "0.5 off the SL with tick 0.01")
        with patch("execute_futures_trade._stop_trigger_tolerance", return_value=1.0):
            res, mock_abort = market_execute(lost_sl_response(FakeExchange([]), trigger=96.5))
        self.assertTrue(res["success"], res.get("error"))
        mock_abort.assert_not_called()

    def test_protect_pending_entry_uses_the_helper(self):
        # filled position; an existing closePosition stop at 94.0, looser than the planned 95.0 by 1.0
        def run(tolerance=None):
            fake = FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")], algos=[stop(9, 94.0)])
            ctx = (patch("execute_futures_trade._stop_trigger_tolerance", return_value=tolerance) if tolerance
                   else patch("execute_futures_trade._stop_trigger_tolerance", wraps=eft._stop_trigger_tolerance))
            with ctx as helper:
                res, _, _ = run_protect(fake, make_record())
            return res, fake, helper
        res, fake, helper = run()
        self.assertEqual(res["actions"][0]["detail"]["mode"], "replace", "looser than plan beyond one tick")
        helper.assert_any_call(0.1, 95.0)
        res, fake, _ = run(tolerance=2.0)
        self.assertEqual(types(res), ["pending_tp_placed"], "within the patched tolerance: kept, no stop write")
        self.assertEqual([c for c in fake.calls if c[0] == "POST" and c[1] == ALGO_ENDPOINT], [])


# ---------------------------------------------------------------------------------------------
# Item 8: kept trigger visible
# ---------------------------------------------------------------------------------------------
def kept_after_minus_4130(fake, listed):
    """The planned SL placement gets -4130 and `listed` (an existing closePosition stop) appears on the listing."""
    def send(method, endpoint, params=None, target_env=None, retry_count=0):
        res = fake(method, endpoint, params, target_env)
        if method == "POST" and endpoint == ALGO_ENDPOINT:
            fake.algos.append(dict(listed))
        return res
    return send


class TestKeptTriggerVisible(ReporterMocked):

    def filled(self, **kw):
        return FakeExchange([long_position(amt="12", entry="101.0", mark="101.5")], **kw)

    def test_kept_trigger_in_action_and_audit_sl_price_unchanged(self):
        fake = self.filled(reject_new_stops=True, reject_response=MINUS_4130)
        res, ws, _ = run_protect(kept_after_minus_4130(fake, stop(9, 95.1)), make_record())
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(types(res), ["pending_protect_sl", "pending_tp_placed"])
        detail = res["actions"][0]["detail"]
        self.assertEqual((detail["stop_source"], detail["kept_trigger_price"], detail["sl_price"]), ("kept", 95.1, 95.0))
        audit = read_jsonl(ws, "trades_audit.jsonl")
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["kept_trigger_price"], audit[0]["sl_price"]), (95.1, 95.0))

    def test_placed_stop_has_no_kept_trigger(self):
        res, ws, _ = run_protect(self.filled(), make_record())
        self.assertTrue(res["ok"], res["errors"])
        detail = res["actions"][0]["detail"]
        self.assertEqual(detail["stop_source"], "placed")
        self.assertNotIn("kept_trigger_price", detail)
        audit = read_jsonl(ws, "trades_audit.jsonl")
        self.assertNotIn("kept_trigger_price", audit[0])
        self.assertEqual(audit[0]["sl_price"], 95.0)

    def test_audit_written_by_a_later_run_records_the_stop_in_force(self):
        # TPs fail in the run that keeps the stop: the audit is written by the next run, which finds the kept stop
        # (mode None) and, as before #179, audits the stop in force as sl_price.
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record())
        fake = self.filled(reject_new_stops=True, reject_response=MINUS_4130)
        state = {"fail_tps": True}
        inner = kept_after_minus_4130(fake, stop(9, 95.1))

        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            params = dict(params or {})
            if (state["fail_tps"] and method == "POST" and endpoint == ORDER_ENDPOINT
                    and params.get("reduceOnly") == "true"):
                fake.calls.append((method, endpoint, params))
                return {"code": -1001, "msg": "Internal error"}
            return inner(method, endpoint, params, target_env)

        with offline(send, workspace=ws, profile=PROD_PROFILE), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
            first = eft.protect_pending_entries(target_env="testnet")
            self.assertEqual(first["actions"][0]["detail"]["stop_source"], "kept")
            self.assertEqual(read_jsonl(ws, "trades_audit.jsonl"), [], "TPs failed: no audit yet")
            state["fail_tps"] = False
            second = eft.protect_pending_entries(target_env="testnet")
        self.assertTrue(second["ok"], second["errors"])
        self.assertNotIn("pending_protect_sl", types(second), "the kept stop covers the position: no new placement")
        audit = read_jsonl(ws, "trades_audit.jsonl")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["sl_price"], 95.1, "the stop in force (pre-existing mode-None behaviour)")


if __name__ == "__main__":
    unittest.main()
