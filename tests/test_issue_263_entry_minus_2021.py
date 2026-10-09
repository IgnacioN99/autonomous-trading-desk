#!/usr/bin/env python3
"""
test_issue_263_entry_minus_2021.py - Issue #263.

A STOP_MARKET entry whose trigger is reached between the price read and the POST /fapi/v1/algoOrder gets Binance
-2021 ("Order would immediately trigger"); no entry order exists. The executor re-reads the price ONCE: past the
trigger, the whole order runs once more (dossier, every PROD gate, the crossed R:R gate and the risk clamp, fresh
snapshots) and enters at MARKET with the verified SL and TPs, tagged converted_from / trigger_crossed_retry /
retry_price in the result and the audit record. Otherwise (price back below, re-read failure, a second -2021) a typed
trigger_crossed failure and nothing placed; a gate rejection on the retry stays that gate's rejection. Never a third
attempt. Any other entry rejection keeps the plain error.

Fake exchange (send_signed_request), urllib blocked, temp workspace, time.sleep patched: no network, no orders, no
writes to logs/, no .env read (load_env, the dossier gate and equity are patched).
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
import test_pending_entries as tpe  # noqa: E402  (fixtures only)
from test_issue_201_165_crossed_trigger import CrossedHarness  # noqa: E402  (fixture only)

ALGO_ENDPOINT = "/fapi/v1/" + "algoOrder"
TICKER = "/fapi/v1/ticker/price"
MINUS_2021 = {"code": -2021, "msg": "Order would immediately trigger."}
DOSSIER_OK = (True, "ok", None)

# LONG trigger 100.5 rejected with -2021, re-read 101: R:R to TP2 from 101 = (110 - 101) / (101 - 98) = 3.0
LONG = dict(order_type="STOP_MARKET", trigger_price=100.5, sl_price=98.0, tp1_price=104.0, tp2_price=110.0)
SHORT = dict(direction="SHORT", order_type="STOP_MARKET", trigger_price=99.5, sl_price=102.0, tp1_price=96.0,
             tp2_price=90.0)


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


class Minus2021Harness(CrossedHarness):
    """CrossedHarness with a sequenced ticker (`self.ticker_seq`, the last value sticky; an Exception item is raised),
    the first `self.entry_2021` STOP_MARKET entry POSTs answered `self.entry_reject` (-2021 by default), an
    `on_reject` hook run at each rejection, and settable dossier / daily-loss answers per pass."""

    def setUp(self):
        super().setUp()
        tpe.write_guardian_state(self.ws)    # PROD resting STOP_MARKET entries need a live guardian
        self.ticker_seq = [100.0, 101.0]
        self.entry_2021 = 1
        self.entry_reject = MINUS_2021
        self.on_reject = None
        self.dossier = [DOSSIER_OK]
        self.daily = [tpe.DAILY_LOSS_GATE_ALLOW]
        self.sl_verify = (True, {"algoId": 9})

    def fake(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == TICKER:
            self.calls.append((method, endpoint, dict(params or {})))
            px = self.ticker_seq.pop(0) if len(self.ticker_seq) > 1 else self.ticker_seq[0]
            if isinstance(px, Exception):
                raise px
            return {"price": repr(px)} if px is not None else {"code": -1001, "msg": "Internal error"}
        if method == "POST" and endpoint == ALGO_ENDPOINT and (params or {}).get("closePosition") == "false":
            if len(self.entry_algo_posts()) < self.entry_2021:
                self.calls.append((method, endpoint, dict(params)))
                if self.on_reject:
                    self.on_reject()
                return dict(self.entry_reject)
        return super().fake(method, endpoint, params, target_env, retry_count)

    def run_trade(self, env="prod", **kwargs):
        """As CrossedHarness.run_trade, with per-pass dossier / daily-loss answers (the last one sticky) and
        time.sleep patched. Returns (result, gates mock, audit mock, stderr text)."""
        args = dict(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0, target_env=env)
        if env == "testnet":
            args["bypass_eval_gate"] = True
        args.update(kwargs)
        tpe.write_session_state(self.ws, self.positions)
        gates = MagicMock(wraps=eft.check_mechanical_gates)
        audit = MagicMock(wraps=eft.append_trade_audit_record)

        def sticky(answers):
            return lambda *a, **k: answers.pop(0) if len(answers) > 1 else answers[0]

        self.dossier_mock = MagicMock(side_effect=sticky(self.dossier))
        self.daily_mock = MagicMock(side_effect=sticky(self.daily))
        err = io.StringIO()
        with patch("execute_futures_trade.send_signed_request", side_effect=self.fake), \
             patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
             patch.object(eft, "__file__", os.path.join(self.ws, "scripts", "execute_futures_trade.py")), \
             patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.enforce_evaluation_dossier", self.dossier_mock), \
             patch("execute_futures_trade.check_mechanical_gates", gates), \
             patch("execute_futures_trade.check_daily_loss_gate", self.daily_mock), \
             patch("execute_futures_trade.append_trade_audit_record", audit), \
             patch("execute_futures_trade.get_symbol_filters", return_value=dict(self.filters)), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("execute_futures_trade.subprocess.run", side_effect=FileNotFoundError("binance-cli")), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=self.sl_verify), \
             patch("execute_futures_trade.time.sleep"), \
             patch("quant_risk_engine.get_account_equity", return_value=self.equity), \
             patch("user_profile.load_user_profile", return_value=dict(self.profile)), \
             patch("report_agent_issue.report_issue"), \
             contextlib.redirect_stderr(err):
            res = eft.execute_complete_trade(**args)
        return res, gates, audit, err.getvalue()

    def entry_algo_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == ALGO_ENDPOINT
                and c[2].get("closePosition") == "false"]

    def tickers(self):
        return [c for c in self.calls if c[1] == TICKER]

    def protective_posts(self):
        return [c for c in self.calls if c[0] == "POST" and (c[2].get("closePosition") == "true"
                                                              or c[2].get("reduceOnly") == "true")]

    def assertNothingPlaced(self, res):
        self.assertFalse(res["success"])
        self.assertEqual(self.entry_orders(), [], "no /fapi/v1/order entry")
        self.assertEqual(self.protective_posts(), [], "no SL / TP / pre-arm")
        self.assertLessEqual(len(self.entry_algo_posts()), 2)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "logs", "pending_entries.json"))
                         and tpe.read_registry(self.ws), "no resting entry registered")


class TestMinus2021Conversion(Minus2021Harness):

    def test_prod_long_converted_to_one_market_entry(self):
        res, gates, audit, err = self.run_trade(**LONG)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(len(self.entry_algo_posts()), 1, "the STOP_MARKET entry is POSTed once")
        entries = self.entry_orders()
        self.assertEqual([e["type"] for e in entries], ["MARKET"], "exactly one MARKET entry")
        self.assertEqual(entries[0]["side"], "BUY")
        sl_posts = [c for c in self.protective_posts() if c[1] == ALGO_ENDPOINT]
        tp_posts = [c for c in self.protective_posts() if c[1] == "/fapi/v1/order"]
        self.assertEqual(len(sl_posts), 1)
        self.assertEqual(float(sl_posts[0][2]["triggerPrice"]), 98.0)
        self.assertEqual(sorted(float(c[2]["price"]) for c in tp_posts), [104.0, 110.0])
        self.assertEqual((res["converted_from"], res["trigger_crossed_retry"], res["retry_price"]),
                         ("STOP_MARKET", True, 101.0))
        record = audit.call_args.args[0]
        self.assertEqual((record["converted_from"], record["trigger_crossed_retry"], record["retry_price"]),
                         ("STOP_MARKET", True, 101.0))
        self.assertEqual(record["entry_reference"], "last")       # the crossed path of issue #236 ran
        self.assertEqual(len(self.tickers()), 3, "first read, the one re-read, the retry pass's own read")
        self.assertEqual(self.dossier_mock.call_count, 2, "the dossier is re-validated on the retry")
        self.assertEqual(self.daily_mock.call_count, 2)
        self.assertEqual(gates.call_count, 2)
        self.assertIn("-2021 on the STOP_MARKET entry of SOLUSDT", err)
        self.assertNotIn("_crossed_retry_price", res)

    def test_prod_short_mirror(self):
        self.ticker_seq = [100.0, 99.0]
        res, _, audit, _ = self.run_trade(**SHORT)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(len(self.entry_algo_posts()), 1)
        self.assertEqual([(e["type"], e["side"]) for e in self.entry_orders()], [("MARKET", "SELL")])
        self.assertEqual((res["converted_from"], res["retry_price"]), ("STOP_MARKET", 99.0))
        self.assertEqual(audit.call_args.args[0]["converted_from"], "STOP_MARKET")

    def test_converted_entry_keeps_the_sl_fail_safe(self):
        # The converted MARKET entry runs the normal SL verification: unverified -> auto-destruct, CRITICAL text first
        self.sl_verify = (False, None)
        res, _, audit, _ = self.run_trade(**LONG)
        self.assertFalse(res["success"])
        self.assertTrue(res["emergency_abort"])
        self.assertTrue(res["error"].startswith("CRITICAL FAIL-SAFE TRIGGERED"), res["error"])
        self.assertIn("(after a -2021 STOP_MARKET -> MARKET conversion (trigger 100.5 crossed, re-read price 101.0))",
                      res["error"])
        self.assertTrue(res["trigger_crossed_retry"])
        self.assertEqual([e["type"] for e in self.entry_orders()], ["MARKET"], "one entry, then the reduce-only close")
        closes = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"
                  and c[2].get("type") == "MARKET" and c[2].get("reduceOnly") == "true"]
        self.assertTrue(closes, "emergency MARKET close sent")
        audit.assert_not_called()

    def test_testnet_follows_the_same_path(self):
        res, _, audit, _ = self.run_trade(env="testnet", **LONG)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(len(self.entry_algo_posts()), 1)
        self.assertEqual([e["type"] for e in self.entry_orders()], ["MARKET"])
        self.assertEqual(res["converted_from"], "STOP_MARKET")
        self.assertTrue(audit.call_args.args[0]["trigger_crossed_retry"])

    def test_testnet_short_rr_below_3_only_warns(self):
        # TESTNET keeps relaxing the crossed R:R gate on the retry, as on the first read
        self.ticker_seq = [100.0, 98.5]
        res, _, _, err = self.run_trade(env="testnet", **dict(SHORT, tp2_price=96.0))
        self.assertTrue(res["success"], res.get("error"))
        self.assertIn("TESTNET (crossed-trigger R:R gate relaxed)", err)
        self.assertEqual(res["converted_from"], "STOP_MARKET")


class TestMinus2021RetryRejections(Minus2021Harness):

    def assertRetryRejection(self, res):
        self.assertNothingPlaced(res)
        self.assertTrue(res.get("hard_gate_rejection"), res)
        self.assertTrue(res["trigger_crossed_retry"])
        self.assertEqual(res["retry_price"], 101.0)
        self.assertIn("After a -2021 STOP_MARKET -> MARKET conversion (trigger 100.5 crossed, re-read price 101.0)",
                      res["error"])
        self.assertNotIn("converted_from", res)
        self.assertEqual(len(self.entry_algo_posts()), 1)

    def test_prod_rr_below_3_after_minus_2021_rejected(self):
        res, _, audit, _ = self.run_trade(**dict(LONG, tp2_price=105.0))
        self.assertRetryRejection(res)
        self.assertIn("MECHANICAL HARD GATE REJECTION", res["error"])
        self.assertIn("already crossed", res["error"])
        self.assertIn("1.333:1", res["error"])
        audit.assert_not_called()

    def test_prod_clamp_reject_after_minus_2021(self):
        # Equity 50: cap 0.3125. First pass at the trigger 100.5: qty 0.055, loss 0.3025 fits Gate 2. Retry at 102
        # (R:R (125 - 102) / 7 = 3.29): clamp 0.3125 x 0.98 / 7 -> qty 0.043 x 102 = 4.39 USDT < minNotional 5
        # (local reject, no flag)
        self.equity = 50.0
        self.ticker_seq = [100.0, 102.0]
        res, gates, _, _ = self.run_trade(**dict(LONG, margin_usdt=1.85, sl_price=95.0, tp1_price=104.0,
                                                 tp2_price=125.0))
        self.assertNothingPlaced(res)
        self.assertEqual(len(self.entry_algo_posts()), 1)
        self.assertIn("Gate 2 risk clamp (qty 0.054 -> 0.043", res["error"])
        self.assertIn("minNotional", res["error"])
        self.assertIn("After a -2021 STOP_MARKET -> MARKET conversion", res["error"])
        self.assertTrue(res["trigger_crossed_retry"])
        self.assertEqual(gates.call_count, 1, "only the first pass reached the mechanical gates")

    def test_prod_dossier_expired_on_retry(self):
        self.dossier = [DOSSIER_OK, (False, "FAIL-CLOSED: dossier expired", None)]
        res, gates, _, _ = self.run_trade(**LONG)
        self.assertRetryRejection(res)
        self.assertTrue(res["evaluation_gate_rejection"])
        self.assertIn("dossier expired", res["error"])
        self.assertEqual(gates.call_count, 1)

    def test_prod_daily_loss_gate_on_retry(self):
        blocked = (False, "DAILY LOSS GATE: daily stop reached", {"blocked": True})
        self.daily = [tpe.DAILY_LOSS_GATE_ALLOW, blocked]
        res, _, _, _ = self.run_trade(**LONG)
        self.assertRetryRejection(res)
        self.assertTrue(res["daily_loss_gate_rejection"])
        self.assertEqual(res["daily_loss_gate"], {"blocked": True})
        self.assertIn("DAILY LOSS GATE", res["error"])

    def test_prod_max_open_positions_on_retry(self):
        # A position opened elsewhere between the passes fills the only slot: the retry's fresh snapshot sees it
        self.profile["max_open_positions"] = 1
        self.on_reject = lambda: self.positions.append(
            {"symbol": "BTCUSDT", "positionAmt": "-0.5", "markPrice": "100", "entryPrice": "100",
             "unRealizedProfit": "0"})
        res, _, _, _ = self.run_trade(**LONG)
        self.assertRetryRejection(res)
        self.assertIn("Max open positions limit (1)", res["error"])

    def test_prod_gate1_delta_on_retry(self):
        # A large LONG opened between the passes makes the book LONG_HEAVY: Gate 1 rejects the converted LONG
        self.on_reject = lambda: self.positions.append(
            {"symbol": "BTCUSDT", "positionAmt": "10", "markPrice": "100", "entryPrice": "100",
             "unRealizedProfit": "0"})
        res, gates, _, _ = self.run_trade(**LONG)
        self.assertRetryRejection(res)
        self.assertIn("LONG_HEAVY", res["error"])
        self.assertEqual(gates.call_count, 2)


class TestMinus2021TypedFailure(Minus2021Harness):

    def assertTypedFailure(self, res, cur_price):
        self.assertNothingPlaced(res)
        self.assertTrue(res["trigger_crossed"])
        self.assertEqual(res["trigger_price"], 100.5)
        self.assertEqual(res["cur_price"], cur_price)
        self.assertIn("Entry trigger crossed (-2021)", res["error"])
        self.assertIn("No order was placed", res["error"])
        self.assertNotIn("converted_from", res)
        self.assertNotIn("_crossed_retry_price", res)

    def test_reread_still_below_trigger(self):
        self.ticker_seq = [100.0, 100.4]
        res, gates, _, _ = self.run_trade(**LONG)
        self.assertTypedFailure(res, 100.4)
        self.assertIn("the re-read price 100.4 is not past the trigger", res["error"])
        self.assertEqual(len(self.entry_algo_posts()), 1, "no retry, no loop")
        self.assertEqual(len(self.tickers()), 2, "exactly one re-read")
        self.assertEqual(self.dossier_mock.call_count, 1, "no second pass")
        self.assertNotIn("trigger_crossed_retry", res)

    def test_reread_fails(self):
        for answer in (None, RuntimeError("timeout")):
            with self.subTest(answer=answer):
                self.calls = []
                self.ticker_seq = [100.0, answer, 101.0]
                res, _, _, _ = self.run_trade(**LONG)
                self.assertTypedFailure(res, None)
                self.assertIn("the price re-read failed", res["error"])
                self.assertEqual(len(self.entry_algo_posts()), 1)

    def test_short_reread_still_above_trigger(self):
        self.ticker_seq = [100.0, 99.6]
        res, _, _, _ = self.run_trade(**SHORT)
        self.assertNothingPlaced(res)
        self.assertTrue(res["trigger_crossed"])
        self.assertEqual((res["trigger_price"], res["cur_price"]), (99.5, 99.6))

    def test_minus_2021_twice_no_third_attempt(self):
        # The retry pass reads 100.2 (back below the trigger), POSTs the STOP_MARKET again and gets -2021 again
        self.ticker_seq = [100.0, 101.0, 100.2]
        self.entry_2021 = 5
        res, _, _, _ = self.run_trade(**LONG)
        self.assertNothingPlaced(res)
        self.assertEqual(len(self.entry_algo_posts()), 2, "the first POST and the one retry, never a third")
        self.assertEqual(len(self.tickers()), 3, "no re-read on the second -2021")
        self.assertTrue(res["trigger_crossed"])
        self.assertTrue(res["trigger_crossed_retry"])
        self.assertEqual(res["retry_price"], 101.0)
        self.assertEqual(res["cur_price"], 100.2)
        self.assertIn("a second -2021 on the one allowed retry", res["error"])

    def test_retry_back_below_trigger_rests_once(self):
        # The retry pass sees the price back below the trigger: the original STOP_MARKET rests (one order), not
        # tagged as a conversion
        self.ticker_seq = [100.0, 101.0, 100.2]
        res, _, _, _ = self.run_trade(**LONG)
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(res["conditional_entry"])
        self.assertEqual(len(self.entry_algo_posts()), 2)
        self.assertEqual(self.entry_orders(), [])
        self.assertNotIn("converted_from", res)
        self.assertTrue(res["trigger_crossed_retry"])

    def test_other_entry_rejection_keeps_plain_error(self):
        self.entry_reject = {"code": -4164, "msg": "Order's notional must be no smaller than 5."}
        res, _, _, _ = self.run_trade(**LONG)
        self.assertNothingPlaced(res)
        self.assertEqual(res, {"success": False, "error": f"Failed to place conditional order: {self.entry_reject}"})
        self.assertEqual(len(self.tickers()), 1, "no re-read")
        self.assertEqual(self.dossier_mock.call_count, 1)

    def test_helper_matches_mcp_style_error(self):
        self.entry_reject = {"isError": True, "error": "Order would immediately trigger."}
        self.ticker_seq = [100.0, 100.1]
        res, _, _, _ = self.run_trade(**LONG)
        self.assertTypedFailure(res, 100.1)


if __name__ == "__main__":
    unittest.main()
