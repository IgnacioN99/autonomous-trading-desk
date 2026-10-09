#!/usr/bin/env python3
"""
test_issue_201_165_crossed_trigger.py - Issues #201 and #165.

- #165 crossed-trigger R:R gate: when the trigger is already crossed and a STOP_MARKET / MARKET order would enter at
  the current price, PROD rejects an R:R to TP2 below 3:1 from that price before any write (TESTNET warns). LIMIT
  orders, uncrossed triggers and plain MARKET orders are not concerned.
- #201 risk-cap clamp: in PROD, an explicit standard margin on a crossed trigger is clamped so the loss at SL fits
  98% of the Gate 2 cap (margin recomputed); a clamped size below minQty / minNotional rejects locally. Defaulted
  margins, YOLO and TESTNET are unchanged.
- #165 minNotional: Decimal comparison; a sure minNotional failure is rejected before any marginType / leverage write.
- #165 friction regression: executor Gate 3 rejects a TP1 under 0.35% at execute_complete_trade level (the radar has
  no friction filter, issue #141).
- #165 radars: broad trigger beyond the wick extreme, TP1 capped at TP2, absorption_scored false without micro data.
- #165 evaluator K2 wording (source and generated copy).

Fake exchange (send_signed_request), urllib blocked, temp workspace: no network, no orders, no writes to logs/.
"""

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execute_futures_trade as eft  # noqa: E402
import broad_market_radar as bmr  # noqa: E402
import intraday_radar as ir  # noqa: E402
import test_pending_entries as tpe  # noqa: E402  (fixtures only)
import test_analytics_cli as tac  # noqa: E402  (fixtures only)
from test_issue_20_wick_consistency import mixed_wick_klines, upper_wick_short_klines  # noqa: E402
from utils.gate_limits import MIN_RR_TP2_CROSSED  # noqa: E402

ALGO_ENDPOINT = "/fapi/v1/" + "algoOrder"
TIA_FILTERS = {"stepSize": 1.0, "minQty": 1.0, "tickSize": 0.0000001, "precision_qty": 0, "precision_price": 7,
               "minNotional": 5.0}


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


class CrossedHarness(tpe.ExecutorHarness):
    """ExecutorHarness with the REAL check_mechanical_gates (wrapped), a settable ticker price, filters, equity and
    profile, and an optional -4421 leverage clamp. PROD Gate 1 reads the temp workspace's session_state.json."""

    def setUp(self):
        super().setUp()
        self.ticker = 100.0
        self.filters = dict(tpe.EX_FILTERS)
        self.equity = 1000.0                 # PROD standard cap: 1000 x 0.5% x 1.25 = 6.25 USDT
        self.profile = dict(tpe.EX_PROFILE)
        self.subaccount_cap = None           # leverage above it answers -4421 (auto-clamp to 5x)

    def fake(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == "/fapi/v1/ticker/price":
            self.calls.append((method, endpoint, dict(params or {})))
            return {"price": repr(self.ticker)}
        if endpoint == "/fapi/v1/leverage" and self.subaccount_cap and params["leverage"] > self.subaccount_cap:
            self.calls.append((method, endpoint, dict(params or {})))
            return {"code": -4421, "msg": "Subaccounts are restricted from using leverage greater than 5x."}
        return super().fake(method, endpoint, params, target_env, retry_count)

    def run_trade(self, env="prod", **kwargs):
        """Returns (result, gates mock, daily-loss-gate mock, audit mock, stderr text)."""
        args = dict(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                    sl_price=97.0, tp1_price=110.0, tp2_price=120.0, target_env=env)
        if env == "testnet":
            args["bypass_eval_gate"] = True
        args.update(kwargs)
        tpe.write_session_state(self.ws, self.positions)
        gates = MagicMock(wraps=eft.check_mechanical_gates)
        audit = MagicMock(wraps=eft.append_trade_audit_record)
        err = io.StringIO()
        with patch("execute_futures_trade.send_signed_request", side_effect=self.fake), \
             patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
             patch.object(eft, "__file__", os.path.join(self.ws, "scripts", "execute_futures_trade.py")), \
             patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.enforce_evaluation_dossier", return_value=(True, "ok", None)), \
             patch("execute_futures_trade.check_mechanical_gates", gates), \
             tpe.allow_daily_loss_gate() as daily, \
             patch("execute_futures_trade.append_trade_audit_record", audit), \
             patch("execute_futures_trade.get_symbol_filters", return_value=dict(self.filters)), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("execute_futures_trade.subprocess.run", side_effect=FileNotFoundError("binance-cli")), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 9})), \
             patch("quant_risk_engine.get_account_equity", return_value=self.equity), \
             patch("user_profile.load_user_profile", return_value=dict(self.profile)), \
             patch("report_agent_issue.report_issue"), \
             contextlib.redirect_stderr(err):
            res = eft.execute_complete_trade(**args)
        return res, gates, daily, audit, err.getvalue()

    def entry_orders(self):
        return [c[2] for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"
                and c[2].get("reduceOnly") != "true"]

    def setup_calls(self):
        return [c for c in self.calls if c[1] in ("/fapi/v1/marginType", "/fapi/v1/leverage")]


# =============================================================================
# #165 crossed-trigger R:R gate
# =============================================================================
class TestCrossedTriggerRRGate(CrossedHarness):

    def assertRejectedBeforeAnyWrite(self, res, gates, daily, rr_text):
        self.assertFalse(res["success"])
        self.assertTrue(res.get("hard_gate_rejection"))
        self.assertIn("MECHANICAL HARD GATE REJECTION", res["error"])
        self.assertIn("already crossed", res["error"])
        self.assertIn(f"R:R to TP2 from there is {rr_text}", res["error"])
        self.assertIn(f"below the {MIN_RR_TP2_CROSSED}:1 minimum", res["error"])
        self.assertIn("fail-closed", res["error"])
        self.assertEqual(self.writes(), [], "no marginType, leverage or order write")
        gates.assert_not_called()
        daily.assert_not_called()   # checked before the Daily Loss Gate and the live reads
        live_reads = [c for c in self.calls if c[1] in ("/fapi/v2/positionRisk", "/fapi/v1/openAlgoOrders",
                                                        "/fapi/v1/openOrders")]
        self.assertEqual(live_reads, [], "checked before the live gate snapshot")

    def test_constant(self):
        self.assertEqual(MIN_RR_TP2_CROSSED, 3.0)

    def test_prod_long_owner_example_rejected(self):
        # Owner example: trigger 100 / SL 98 / TP2 108 with the price at 101.5 -> 6.5 / 3.5 = 1.857:1
        self.ticker = 101.5
        for order_type in ("STOP_MARKET", "MARKET"):
            with self.subTest(order_type=order_type):
                self.calls = []
                res, gates, daily, _, _ = self.run_trade(order_type=order_type, trigger_price=100.0, sl_price=98.0,
                                                         tp1_price=104.0, tp2_price=108.0)
                self.assertRejectedBeforeAnyWrite(res, gates, daily, "1.857:1")
                self.assertIn("Trigger 100.0", res["error"])
                self.assertIn("current price 101.5", res["error"])
                self.assertIn("SL 98.0, TP2 108.0", res["error"])

    def test_dispatch_rule_helper(self):
        self.assertTrue(eft._enters_at_limit("LIMIT", 99.0))
        self.assertTrue(eft._enters_at_limit("limit", "99"))
        for order_type, limit_price in (("LIMIT", None), ("LIMIT", 0), ("MARKET", 99.0), ("STOP_MARKET", 99.0)):
            self.assertFalse(eft._enters_at_limit(order_type, limit_price), (order_type, limit_price))

    def test_prod_limit_without_limit_price_is_gated(self):
        # Round 2 (audit): a LIMIT without --limit-price is dispatched as MARKET at the current price, so a crossed
        # trigger gets the same R:R gate as STOP_MARKET / MARKET
        self.ticker = 101.5
        res, gates, daily, _, _ = self.run_trade(order_type="LIMIT", trigger_price=100.0, sl_price=98.0,
                                                 tp1_price=104.0, tp2_price=108.0)
        self.assertRejectedBeforeAnyWrite(res, gates, daily, "1.857:1")

    def test_prod_limit_without_limit_price_passing_rr_enters_at_market(self):
        res, _, _, _, _ = self.run_trade(order_type="LIMIT", trigger_price=99.5, sl_price=98.0, tp1_price=104.0,
                                         tp2_price=106.0)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(self.entry_orders()[0]["type"], "MARKET")
        self.assertFalse(res.get("pending_limit_entry"))

    def test_limit_without_price_and_new_market_status_is_not_a_resting_limit(self):
        # The MARKET sent for a price-less LIMIT may answer status NEW (ACK). Before the shared dispatch rule this hit
        # the resting-LIMIT branch, which reads the limit price that was never set (NameError, position unprotected).
        base_fake = self.fake

        def ack_fake(method, endpoint, params=None, target_env=None, retry_count=0):
            res = base_fake(method, endpoint, params, target_env, retry_count)
            if endpoint == "/fapi/v1/order" and method == "POST" and (params or {}).get("type") == "MARKET":
                res = dict(res, status="NEW")
            return res
        self.fake = ack_fake
        res, _, _, _, _ = self.run_trade(env="testnet", order_type="LIMIT")
        self.assertTrue(res["success"], res.get("error"))
        self.assertFalse(res.get("pending_limit_entry"))
        self.assertEqual(self.entry_orders()[0]["type"], "MARKET")

    def test_prod_yolo_crossed_rr_below_3_rejected(self):
        # The R:R gate has no YOLO exemption (only the #201 clamp does)
        self.ticker = 101.5
        res, gates, daily, _, _ = self.run_trade(is_yolo=True, order_type="STOP_MARKET", trigger_price=100.0,
                                                 sl_price=98.0, tp1_price=104.0, tp2_price=108.0)
        self.assertRejectedBeforeAnyWrite(res, gates, daily, "1.857:1")

    def test_prod_short_mirror_rejected(self):
        self.ticker = 98.5
        res, gates, daily, _, _ = self.run_trade(direction="SHORT", order_type="STOP_MARKET", trigger_price=100.0,
                                                 sl_price=102.0, tp1_price=96.0, tp2_price=92.0)
        self.assertRejectedBeforeAnyWrite(res, gates, daily, "1.857:1")

    def test_tiausdt_issue_numbers_rejected(self):
        # Issue #201 command: SHORT trigger 0.5099449, SL 0.53446393, TP2 0.41186877, price 0.5037 -> ~2.985:1
        self.ticker = 0.5037
        self.filters = dict(TIA_FILTERS)
        res, gates, daily, _, _ = self.run_trade(symbol="TIAUSDT", direction="SHORT", margin_usdt=12.41,
                                                 order_type="STOP_MARKET", trigger_price=0.5099449,
                                                 sl_price=0.53446393, tp1_price=0.46581064, tp2_price=0.41186877)
        self.assertRejectedBeforeAnyWrite(res, gates, daily, "2.985:1")

    def test_exact_boundary_passes_and_just_below_rejects(self):
        # price 100, SL 98, TP2 106: exactly 6 / 2 = 3.0
        res, gates, _, _, _ = self.run_trade(order_type="STOP_MARKET", trigger_price=99.5, sl_price=98.0,
                                             tp1_price=104.0, tp2_price=106.0)
        self.assertTrue(res["success"], res.get("error"))
        gates.assert_called_once()
        self.assertEqual(self.entry_orders()[0]["type"], "MARKET")
        self.calls = []
        res, gates, daily, _, _ = self.run_trade(order_type="STOP_MARKET", trigger_price=99.5, sl_price=98.0,
                                                 tp1_price=104.0, tp2_price=105.99)
        self.assertRejectedBeforeAnyWrite(res, gates, daily, "2.995:1")

    def test_uncrossed_trigger_not_gated(self):
        # Trigger 101 not reached at price 100: the order rests at the trigger (R:R from 100 would be 1:1)
        tpe.write_guardian_state(self.ws)
        res, gates, _, _, _ = self.run_trade(order_type="STOP_MARKET", trigger_price=101.0, sl_price=99.0,
                                             tp1_price=102.0, tp2_price=103.0)
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res.get("conditional_entry"))
        gates.assert_called_once()

    def test_limit_with_breached_trigger_not_gated(self):
        # LIMIT at 99 enters at its limit: from 99, R:R = 2 / 1; from the price 100 it would be 1 / 2
        tpe.write_guardian_state(self.ws)
        res, gates, _, _, _ = self.run_trade(order_type="LIMIT", limit_price=99.0, trigger_price=99.5,
                                             sl_price=98.0, tp1_price=100.0, tp2_price=101.0)
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res.get("pending_limit_entry"))
        self.assertNotIn("already crossed", str(res))

    def test_plain_market_without_trigger_not_gated(self):
        res, gates, _, _, err = self.run_trade(order_type="MARKET", sl_price=98.0, tp1_price=100.5, tp2_price=101.0)
        self.assertTrue(res["success"], res.get("error"))
        self.assertNotIn("crossed-trigger", err)

    def test_testnet_only_warns(self):
        self.ticker = 101.5
        res, _, _, _, err = self.run_trade(env="testnet", order_type="STOP_MARKET", trigger_price=100.0,
                                           sl_price=98.0, tp1_price=104.0, tp2_price=108.0)
        self.assertTrue(res["success"], res.get("error"))
        self.assertIn("TESTNET (crossed-trigger R:R gate relaxed)", err)
        self.assertIn("1.857:1", err)
        self.assertEqual(self.entry_orders()[0]["type"], "MARKET")


# =============================================================================
# #201 risk-cap clamp
# =============================================================================
class TestCrossedTriggerRiskClamp(CrossedHarness):

    # LONG trigger 100 crossed at 101, SL 95, TP2 120: R:R 19 / 6 = 3.17 (passes the #165 gate). Margin 40 sized at
    # the trigger: 40 x 3 / 100 = 1.2 -> loss 6.0 USDT at 100, but at 101 qty 1.188 -> loss 7.13 > 6.25 cap.
    LONG = dict(order_type="STOP_MARKET", trigger_price=100.0, margin_usdt=40.0, sl_price=95.0, tp1_price=105.0,
                tp2_price=120.0)

    def setUp(self):
        super().setUp()
        self.ticker = 101.0

    def _cap_qty(self, cap, entry, sl):
        return eft.round_step(cap * 0.98 / abs(entry - sl), self.filters["stepSize"], self.filters["precision_qty"])

    def test_issue_201_without_clamp_gate2_would_reject(self):
        unclamped = eft.round_step(40.0 * 3 / 101.0, 0.001, 3)
        self.assertEqual(unclamped, 1.188)
        self.assertGreater(unclamped * (101.0 - 95.0), 6.25)

    def test_prod_long_clamped_to_cap_and_gate2_passes(self):
        res, gates, _, audit, err = self.run_trade(**self.LONG)
        self.assertTrue(res["success"], res.get("error"))
        expected = self._cap_qty(6.25, 101.0, 95.0)
        self.assertEqual(expected, 1.02)
        self.assertEqual(self.entry_orders()[0]["type"], "MARKET")
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), expected)
        self.assertEqual(gates.call_args.args[4], expected)            # Gate 2 saw the clamped qty
        self.assertLessEqual(expected * (101.0 - 95.0), 6.25 * 0.98)
        self.assertEqual(res["total_qty"], expected)
        # Issue #126 invariant: the recorded margin is qty x price / leverage
        margin = audit.call_args.args[1]
        self.assertAlmostEqual(margin, expected * 101.0 / 3, places=6)
        self.assertIn("[RISK CLAMP] qty 1.188 -> 1.02 to fit the Gate 2 cap", err)

    def test_prod_limit_without_limit_price_clamped(self):
        # Round 2 (audit): dispatched as MARKET at the current price, so the clamp applies as for STOP_MARKET
        res, gates, _, _, err = self.run_trade(**dict(self.LONG, order_type="LIMIT"))
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(self.entry_orders()[0]["type"], "MARKET")
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), self._cap_qty(6.25, 101.0, 95.0))
        self.assertIn("[RISK CLAMP]", err)

    def test_prod_short_clamped(self):
        self.ticker = 99.0
        res, gates, _, _, err = self.run_trade(direction="SHORT", order_type="STOP_MARKET", trigger_price=100.0,
                                               margin_usdt=40.0, sl_price=105.0, tp1_price=95.0, tp2_price=80.0)
        self.assertTrue(res["success"], res.get("error"))
        expected = self._cap_qty(6.25, 99.0, 105.0)
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), expected)
        self.assertLess(expected, eft.round_step(40.0 * 3 / 99.0, 0.001, 3))
        self.assertIn("[RISK CLAMP]", err)

    def test_cap_uses_gate2_equity_with_open_losses(self):
        # An open SHORT with -200 uPnL: the cap is min(1000, 800) x 0.5% x 1.25 = 5.0 (as in Gate 2). Its 150 USDT
        # notional keeps the book balanced after the ~100 USDT LONG (Gate 1).
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "-1.5", "markPrice": "100", "entryPrice": "100",
                           "unRealizedProfit": "-200"}]
        res, gates, _, _, _ = self.run_trade(**self.LONG)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), self._cap_qty(5.0, 101.0, 95.0))

    def test_clamp_below_min_notional_rejects_locally(self):
        # Equity 100: cap 0.625; SL 50 -> 0.6125 / 51 = 0.012 x 101 = 1.21 USDT < 5 minNotional (never bumped)
        self.equity = 100.0
        res, gates, daily, _, _ = self.run_trade(order_type="STOP_MARKET", trigger_price=100.0, margin_usdt=20.0,
                                                 sl_price=50.0, tp1_price=110.0, tp2_price=300.0)
        self.assertFalse(res["success"])
        self.assertIn("minNotional", res["error"])
        self.assertIn("Gate 2 risk clamp (qty 0.594 -> 0.012", res["error"])
        self.assertIn("fail-closed", res["error"])
        self.assertEqual(self.writes(), [], "rejected before the marginType / leverage setup")
        gates.assert_not_called()

    def test_clamp_below_min_qty_rejects_locally(self):
        self.equity = 100.0
        self.filters.update(minQty=0.05, minNotional=1.0)
        res, gates, _, _, _ = self.run_trade(order_type="STOP_MARKET", trigger_price=100.0, margin_usdt=20.0,
                                             sl_price=50.0, tp1_price=110.0, tp2_price=300.0)
        self.assertFalse(res["success"])
        self.assertIn("lower than minimum allowed 0.05", res["error"])
        self.assertIn("Gate 2 risk clamp", res["error"])
        self.assertEqual(self.writes(), [])

    def test_unreadable_upnl_rejects(self):
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0.001", "markPrice": "100", "entryPrice": "100"}]
        res, gates, _, _, _ = self.run_trade(**self.LONG)
        self.assertFalse(res["success"])
        self.assertTrue(res.get("hard_gate_rejection"))
        self.assertIn("unrealized PnL", res["error"])
        self.assertEqual(self.writes(), [])

    def test_defaulted_margin_not_clamped(self):
        res, gates, _, _, err = self.run_trade(**dict(self.LONG, margin_usdt=None))
        self.assertNotIn("[RISK CLAMP]", err)
        # Issue #126 sizing: min(100, max(5, 1000 x 0.30 x 0.5)) = 100 -> 300 / 101 = 2.970 (Gate 2 then rejects)
        self.assertEqual(gates.call_args.args[4], eft.round_step(300.0 / 101.0, 0.001, 3))
        self.assertFalse(res["success"])
        self.assertIn("Monetary risk exceeds allowed cap", res["error"])

    def test_yolo_not_clamped(self):
        res, gates, _, _, err = self.run_trade(is_yolo=True, **self.LONG)
        self.assertNotIn("[RISK CLAMP]", err)
        self.assertEqual(gates.call_args.args[4], 1.188)

    def test_testnet_not_clamped(self):
        res, gates, _, _, err = self.run_trade(env="testnet", **self.LONG)
        self.assertTrue(res["success"], res.get("error"))
        self.assertNotIn("[RISK CLAMP]", err)
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), 1.188)

    def test_uncrossed_trigger_not_clamped(self):
        # Sized and gated at the 102 trigger (price 101): 40 x 3 / 102 = 1.176, loss 8.2 -> Gate 2 rejects unchanged
        tpe.write_guardian_state(self.ws)
        res, gates, _, _, err = self.run_trade(**dict(self.LONG, trigger_price=102.0, tp2_price=130.0))
        self.assertNotIn("[RISK CLAMP]", err)
        self.assertEqual(gates.call_args.args[4], eft.round_step(120.0 / 102.0, 0.001, 3))
        self.assertIn("Monetary risk exceeds allowed cap", res["error"])


# =============================================================================
# #165 minNotional edges
# =============================================================================
class TestMinNotionalEdges(CrossedHarness):

    def test_notional_below_is_decimal(self):
        self.assertLess(0.57 * 100.0, 57.0)                     # float says below...
        self.assertFalse(eft._notional_below(0.57, 100.0, 57.0))  # ...but 0.57 x 100 is exactly 57
        self.assertTrue(eft._notional_below(0.569, 100.0, 57.0))

    def test_exact_min_notional_is_not_bumped(self):
        self.filters["minNotional"] = 57.0
        self.assertLess(0.57 * 100.0, 57.0)
        res, _, _, _, _ = self.run_trade(env="testnet", order_type="MARKET", margin_usdt=19.0)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), 0.57)   # float compare bumped it to 0.571

    def test_sure_failure_rejected_before_setup(self):
        # 1.0 x 3 / 100 -> 0.03; one bump -> 0.031 x 100 = 3.1 USDT < 5 with the requested leverage
        for env in ("testnet", "prod"):
            with self.subTest(env=env):
                self.calls = []
                res, gates, _, _, _ = self.run_trade(env=env, margin_usdt=1.0)
                self.assertFalse(res["success"])
                self.assertIn("minNotional", res["error"])
                self.assertEqual(self.setup_calls(), [], "no /fapi/v1/marginType or /fapi/v1/leverage call")
                self.assertEqual(self.writes(), [])
                gates.assert_not_called()

    def test_bump_rescue_still_reaches_setup(self):
        res, _, _, _, _ = self.run_trade(env="testnet", margin_usdt=1.65)
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(self.setup_calls())
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), 0.05)

    def test_post_setup_check_kept_when_leverage_is_clamped(self):
        # 0.8 x 10 / 100 = 0.08 (8 USDT) passes before the setup; the sub-account clamps to 5x: 0.04 -> bump 0.041
        # = 4.1 USDT < 5 -> rejected after the setup, before any order
        self.subaccount_cap = 5
        self.profile.update(leverage_standard=10)
        res, gates, _, _, _ = self.run_trade(env="testnet", leverage=10, margin_usdt=0.8)
        self.assertFalse(res["success"])
        self.assertIn("minNotional", res["error"])
        self.assertTrue(self.setup_calls())
        self.assertEqual([c for c in self.writes() if c[1] in ("/fapi/v1/order", ALGO_ENDPOINT)], [])
        gates.assert_not_called()


# =============================================================================
# #165 friction floor regression (executor Gate 3, no radar filter)
# =============================================================================
class TestFrictionFloorAtExecution(CrossedHarness):

    def test_tp1_under_035_pct_rejected_by_gate3(self):
        res, gates, _, _, _ = self.run_trade(order_type="MARKET", sl_price=98.0, tp1_price=100.3, tp2_price=110.0)
        self.assertFalse(res["success"])
        self.assertTrue(res.get("hard_gate_rejection"))
        self.assertIn("below 0.35% friction floor", res["error"])
        gates.assert_called_once()
        self.assertEqual(self.entry_orders(), [])
        self.assertEqual([c for c in self.writes() if c[1] == ALGO_ENDPOINT], [])

    def test_tp1_at_040_pct_passes(self):
        res, _, _, _, _ = self.run_trade(order_type="MARKET", sl_price=98.0, tp1_price=100.4, tp2_price=110.0)
        self.assertTrue(res["success"], res.get("error"))


# =============================================================================
# #165 radars
# =============================================================================
def _broad(klines):
    with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen([("/fapi/v1/klines", lambda url: klines)])):
        return bmr.analyze_single_symbol("AAAUSDT", interval="15m")


def _intraday(klines):
    with patch.object(ir, "get_klines", return_value=klines):
        return ir.analyze_symbol("AAAUSDT")


class TestBroadTriggerUsesWickExtreme(unittest.TestCase):

    def test_long_trigger_from_wick_high_when_above_forming(self):
        klines = mixed_wick_klines()
        klines[-1][2] = repr(float(klines[-1][1]) * 1.002)          # forming high below the wick high
        wick_high = float(klines[-2][2])
        self.assertLess(float(klines[-1][2]), wick_high)
        cand = _broad(klines)
        self.assertEqual(cand["direction"], "LONG")
        self.assertAlmostEqual(cand["trigger"], wick_high * 1.0005)

    def test_long_trigger_from_forming_high_when_above_wick(self):
        klines = mixed_wick_klines()
        self.assertGreater(float(klines[-1][2]), float(klines[-2][2]))
        cand = _broad(klines)
        self.assertEqual(cand["direction"], "LONG")
        self.assertAlmostEqual(cand["trigger"], float(klines[-1][2]) * 1.0005)

    def test_short_trigger_from_wick_low_when_below_forming(self):
        klines = upper_wick_short_klines()
        wick_low = float(klines[-2][3])
        self.assertLess(wick_low, float(klines[-1][3]))
        cand = _broad(klines)
        self.assertEqual(cand["direction"], "SHORT")
        self.assertAlmostEqual(cand["trigger"], wick_low * 0.9995)
        self.assertGreater(cand["trigger_distance_pct"], 0)


class TestTp1CappedAtTp2(unittest.TestCase):
    """A far EMA 20 (in the trade direction) used to put TP1 beyond TP2; it is capped at TP2 in both radars."""

    def _check(self, res, direction):
        self.assertEqual(res["direction"], direction)
        self.assertEqual(res["tp1"], res["tp2"])
        if direction == "LONG":
            self.assertLessEqual(res["tp1"], res["tp2"])
        else:
            self.assertGreaterEqual(res["tp1"], res["tp2"])

    FAR = ((mixed_wick_klines, "LONG", 3.0), (upper_wick_short_klines, "SHORT", 0.3))

    def test_intraday_long_and_short(self):
        for make, direction, far in self.FAR:
            klines = make()
            with self.subTest(direction=direction):
                price = float(klines[-1][4])
                with patch.object(ir, "calculate_ema", return_value=[price * far]):
                    res = _intraday(klines)
                self._check(res, direction)

    def test_broad_long_and_short(self):
        for make, direction, far in self.FAR:
            klines = make()
            with self.subTest(direction=direction):
                price = float(klines[-1][4])
                with patch.object(bmr, "calculate_ema", return_value=[price * far]):
                    cand = _broad(klines)
                self._check(cand, direction)

    def test_near_ema_unchanged(self):
        # Without a far EMA, TP1 stays the 1.8R level (below TP2): the cap does not move it
        res = _intraday(mixed_wick_klines())
        self.assertLess(res["tp1"], res["tp2"])


class TestNoMicroAbsorptionUnscored(unittest.TestCase):

    REASON = "🔬 ORDER FLOW: absorption not scored (no microstructure data)"

    def test_no_micro_sets_flag_and_reason(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                cand = {"symbol": "AAAUSDT", "direction": direction, "confidence": 60, "reasons": [],
                        "interval": "15m", "tier_s_eligible": True}
                with patch("microstructure_engine.get_symbol_microstructure", return_value=None):
                    out = bmr.enrich_candidate_microstructure(cand)
                self.assertIs(out["absorption_scored"], False)
                self.assertIn(self.REASON, out["reasons"])

    def test_default_is_false(self):
        import screening_pipeline as sp
        self.assertIs(sp.CandidateSetup.model_fields["absorption_scored"].default, False)


# =============================================================================
# #165 evaluator K2 wording
# =============================================================================
class TestK2UnscoredWording(unittest.TestCase):

    TAIL = ("`abs:unscored` (`absorption_scored: false`) -> the absorption paths (>= 60% with |OIB| >= 0.15, >= 55% "
            "with R:R >= 3:1) never pass K2; only `vol_ratio >= 1.4x` can.")

    def test_source_and_generated_copy(self):
        for rel in ((".agents", "agents", "isolated_market_evaluator", "agent.md"),
                    (".claude", "agents", "isolated_market_evaluator.md")):
            with self.subTest(path=os.path.join(*rel)):
                with open(os.path.join(BASE_DIR, *rel), encoding="utf-8") as f:
                    text = f.read()
                k2 = next(line for line in text.splitlines() if line.strip().startswith("- K2 Institutional volume"))
                self.assertTrue(k2.rstrip().endswith(self.TAIL), k2)
                self.assertNotIn("absorption gives no confluence", text)


if __name__ == "__main__":
    unittest.main()
