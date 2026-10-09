#!/usr/bin/env python3
"""
test_issue_236_crossed_trigger_followups.py - Issue #236 (follow-ups of #201 / #165) and the #232 observability fix.

- Named haircut: RISK_CLAMP_HAIRCUT (utils/gate_limits.py) sizes the crossed-trigger risk clamp and is printed in the
  [RISK CLAMP] line; behaviour unchanged.
- Misleading clamp log: `clamped` compares step-rounded quantities, so an excess of less than one step is not a clamp
  (no [RISK CLAMP] line, the explicit margin is kept).
- Reject flag: the clamp-too-small rejects (minQty / minNotional) have the same shape as the existing minQty /
  minNotional rejects: no hard_gate_rejection.
- Slippage reference (PROD crossed trigger): the R:R gate and the clamp are measured from the worse of the last
  price and the book side a MARKET order hits (one GET /fapi/v1/ticker/bookTicker); an unreadable book falls back to
  the last price; the audit record carries entry_reference / entry_reference_price / entry_slippage_pct. TESTNET and
  non-crossed orders make no book read.
- K2 prompt: the two absorption paths named once; EXAMPLE 5 carries an abs:unscored row (absorption 70%, vol_ratio
  1.2x) that FAILs K2.
- #232: a rejected pre-arm keeps prearm_status "rejected:<code>" and adds prearm_reject_msg (the Binance msg) to the
  result, the registry record and the reported issue context; error_detail (dedup) is unchanged.

Fake exchange (send_signed_request), urllib blocked, temp workspace, report_issue mocked: no network, no orders, no
writes to logs/, no credentials read.
"""

import json
import os
import re
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execute_futures_trade as eft  # noqa: E402
import test_pending_entries as tpe  # noqa: E402  (fixtures only)
from test_issue_201_165_crossed_trigger import CrossedHarness  # noqa: E402  (fixture only)
from utils import gate_limits  # noqa: E402
from utils.dossier_provenance import check_precondition_checklist  # noqa: E402

BOOK = "/fapi/v1/ticker/bookTicker"
ALGO_ENDPOINT = "/fapi/v1/" + "algoOrder"


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


class BookHarness(CrossedHarness):
    """CrossedHarness with a settable book ticker (`self.book`: a dict, any other answer, or an exception to raise)
    and an optional MARKET entry fill price (`self.avg_price`)."""

    def setUp(self):
        super().setUp()
        self.book = {}
        self.avg_price = None

    def fake(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == BOOK:
            self.calls.append((method, endpoint, dict(params or {})))
            if isinstance(self.book, Exception):
                raise self.book
            return dict(self.book) if isinstance(self.book, dict) else self.book
        res = super().fake(method, endpoint, params, target_env, retry_count)
        if (self.avg_price is not None and method == "POST" and endpoint == "/fapi/v1/order"
                and (params or {}).get("reduceOnly") != "true"):
            res = dict(res, avgPrice=repr(self.avg_price))
        return res

    def book_calls(self):
        return [c for c in self.calls if c[1] == BOOK]

    def audit_record(self, audit):
        return audit.call_args.args[0]


# =============================================================================
# Named haircut
# =============================================================================
class TestNamedHaircut(BookHarness):

    LONG = dict(order_type="STOP_MARKET", trigger_price=100.0, margin_usdt=40.0, sl_price=95.0, tp1_price=105.0,
                tp2_price=120.0)

    def test_constant_lives_in_gate_limits(self):
        self.assertEqual(gate_limits.RISK_CLAMP_HAIRCUT, 0.98)
        self.assertIs(eft.RISK_CLAMP_HAIRCUT, gate_limits.RISK_CLAMP_HAIRCUT)

    def test_no_literal_haircut_left_at_the_clamp(self):
        with open(os.path.join(BASE_DIR, "scripts", "execute_futures_trade.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("clamp_cap * 0.98", src)
        self.assertNotIn("USDT x 0.98", src)

    def test_clamp_unchanged_and_log_names_the_haircut(self):
        self.ticker = 101.0
        res, _, _, _, err = self.run_trade(**self.LONG)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), 1.02)   # as before #236
        self.assertIn("[RISK CLAMP] qty 1.188 -> 1.02", err)
        self.assertIn(f"x {gate_limits.RISK_CLAMP_HAIRCUT}", err)


# =============================================================================
# Misleading clamp log (rounded comparison)
# =============================================================================
class TestSubStepExcessIsNotAClamp(BookHarness):

    def test_sub_step_excess_prints_no_clamp_line(self):
        # Cap qty round_step(6.25 x 0.98 / 6) = 1.020; margin 34.37 x 3 / 101 = 1.020891 -> 1.020 after rounding:
        # the raw qty exceeds the cap by less than one step, so the order is unchanged.
        self.ticker = 101.0
        raw = 34.37 * 3 / 101.0
        cap_qty = eft.round_step(6.25 * gate_limits.RISK_CLAMP_HAIRCUT / 6.0, 0.001, 3)
        self.assertGreater(raw, cap_qty)
        self.assertEqual(eft.round_step(raw, 0.001, 3), cap_qty)
        res, gates, _, audit, err = self.run_trade(order_type="STOP_MARKET", trigger_price=100.0, margin_usdt=34.37,
                                                   sl_price=95.0, tp1_price=105.0, tp2_price=120.0)
        self.assertTrue(res["success"], res.get("error"))
        self.assertNotIn("[RISK CLAMP]", err)
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), 1.02)
        self.assertEqual(gates.call_args.args[4], 1.02)
        self.assertEqual(audit.call_args.args[1], 34.37, "not clamped: the explicit margin is kept")

    def test_real_clamp_still_prints(self):
        self.ticker = 101.0
        _, _, _, _, err = self.run_trade(**TestNamedHaircut.LONG)
        self.assertIn("[RISK CLAMP] qty 1.188 -> 1.02", err)


# =============================================================================
# Reject flag: one shape for every minQty / minNotional reject
# =============================================================================
class TestClampRejectShape(BookHarness):

    SMALL = dict(order_type="STOP_MARKET", trigger_price=100.0, margin_usdt=20.0, sl_price=50.0, tp1_price=110.0,
                 tp2_price=300.0)

    def assertLocalReject(self, res, text):
        self.assertFalse(res["success"])
        self.assertIn(text, res["error"])
        self.assertNotIn("hard_gate_rejection", res)
        self.assertEqual(set(res), {"success", "error"})
        self.assertEqual(self.writes(), [])

    def test_clamp_below_min_notional(self):
        self.ticker = 101.0
        self.equity = 100.0
        res, _, _, _, _ = self.run_trade(**self.SMALL)
        self.assertLocalReject(res, "minNotional")
        self.assertIn("Gate 2 risk clamp", res["error"])

    def test_clamp_below_min_qty(self):
        self.ticker = 101.0
        self.equity = 100.0
        self.filters.update(minQty=0.05, minNotional=1.0)
        res, _, _, _, _ = self.run_trade(**self.SMALL)
        self.assertLocalReject(res, "lower than minimum allowed 0.05")
        self.assertIn("Gate 2 risk clamp", res["error"])

    def test_unclamped_min_notional_same_shape(self):
        res, _, _, _, _ = self.run_trade(margin_usdt=1.0)
        self.assertLocalReject(res, "minNotional")
        self.assertNotIn("Gate 2 risk clamp", res["error"])

    def test_unclamped_min_qty_same_shape(self):
        self.filters.update(minQty=1.0)
        res, _, _, _, _ = self.run_trade(margin_usdt=10.0)
        self.assertLocalReject(res, "lower than minimum allowed 1.0")
        self.assertNotIn("Gate 2 risk clamp", res["error"])


# =============================================================================
# Slippage reference: book side vs last price
# =============================================================================
class TestBookReference(BookHarness):

    # Exactly 3.0 from the last price 100: LONG SL 98 / TP2 106, SHORT SL 102 / TP2 94
    LONG = dict(order_type="STOP_MARKET", trigger_price=99.5, sl_price=98.0, tp1_price=104.0, tp2_price=106.0)
    SHORT = dict(direction="SHORT", order_type="STOP_MARKET", trigger_price=100.5, sl_price=102.0, tp1_price=96.0,
                 tp2_price=94.0)

    def assertBookReject(self, res, gates, daily, rr_text):
        self.assertFalse(res["success"])
        self.assertTrue(res.get("hard_gate_rejection"))
        self.assertIn("MECHANICAL HARD GATE REJECTION", res["error"])
        self.assertIn(f"R:R to TP2 from there is {rr_text}", res["error"])
        self.assertIn("current price 100.0", res["error"])
        self.assertEqual(self.writes(), [], "rejected before any write")
        gates.assert_not_called()
        daily.assert_not_called()
        self.assertEqual(len(self.book_calls()), 1, "one book read, no retry")
        self.assertEqual(self.book_calls()[0][2], {"symbol": "SOLUSDT"})

    def test_last_price_passes_at_the_boundary(self):
        for kwargs in (self.LONG, self.SHORT):
            with self.subTest(direction=kwargs.get("direction", "LONG")):
                self.calls = []
                res, _, _, audit, _ = self.run_trade(**kwargs)
                self.assertTrue(res["success"], res.get("error"))
                rec = self.audit_record(audit)
                self.assertEqual((rec["entry_reference"], rec["entry_reference_price"]), ("last", 100.0))
                self.assertNotIn("book_side_price", rec)

    def test_long_worse_ask_rejects_where_last_passes(self):
        self.book = {"symbol": "SOLUSDT", "bidPrice": "100.00", "askPrice": "100.01"}
        res, gates, daily, _, _ = self.run_trade(**self.LONG)
        self.assertBookReject(res, gates, daily, "2.980:1")     # 5.99 / 2.01
        self.assertIn("measured from the best ask 100.01", res["error"])

    def test_short_worse_bid_rejects_where_last_passes(self):
        self.book = {"symbol": "SOLUSDT", "bidPrice": "99.99", "askPrice": "100.00"}
        res, gates, daily, _, _ = self.run_trade(**self.SHORT)
        self.assertBookReject(res, gates, daily, "2.980:1")
        self.assertIn("measured from the best bid 99.99", res["error"])

    def test_better_book_never_loosens_the_gate(self):
        # LONG ask below last / SHORT bid above last: the reference stays the last price (the worse entry)
        cases = ((self.LONG, {"bidPrice": "99.98", "askPrice": "99.99"}, 99.99),
                 (self.SHORT, {"bidPrice": "100.01", "askPrice": "100.02"}, 100.01))
        for kwargs, book, side in cases:
            with self.subTest(direction=kwargs.get("direction", "LONG")):
                self.calls = []
                self.book = book
                res, _, _, audit, _ = self.run_trade(**kwargs)
                self.assertTrue(res["success"], res.get("error"))
                rec = self.audit_record(audit)
                self.assertEqual((rec["entry_reference"], rec["entry_reference_price"]), ("book", 100.0))
                self.assertEqual(rec["book_side_price"], side)
        # and below 3.0 from last stays rejected whatever the book says
        self.calls = []
        self.book = {"bidPrice": "99.0", "askPrice": "99.01"}
        res, gates, daily, _, _ = self.run_trade(**dict(self.LONG, tp2_price=105.99))
        self.assertBookReject(res, gates, daily, "2.995:1")

    def test_book_beyond_tp2_rejects_as_wrong_side(self):
        self.book = {"bidPrice": "106.4", "askPrice": "106.5"}
        res, gates, daily, _, _ = self.run_trade(**self.LONG)
        self.assertBookReject(res, gates, daily, "undefined (zero or wrong-side distance)")

    def test_unavailable_book_falls_back_to_last(self):
        unavailable = ({}, {"code": -1003, "msg": "Too many requests"}, {"error": "timeout"}, [],
                       "not json", None, RuntimeError("socket closed"),
                       {"bidPrice": "0", "askPrice": "100.01"}, {"bidPrice": "100.0", "askPrice": "nan"},
                       {"bidPrice": "100.02", "askPrice": "100.01"}, {"bidPrice": "100.0"},
                       {"bidPrice": "100.0", "askPrice": "inf"})
        for book in unavailable:
            with self.subTest(book=repr(book)):
                self.calls = []
                self.book = book
                res, _, _, audit, _ = self.run_trade(**self.LONG)
                self.assertTrue(res["success"], res.get("error"))
                self.assertEqual(len(self.book_calls()), 1, "one read, no retry")
                rec = self.audit_record(audit)
                self.assertEqual((rec["entry_reference"], rec["entry_reference_price"]), ("last", 100.0))
                self.assertNotIn("book_side_price", rec)

    def test_audit_records_reference_and_slippage(self):
        self.book = {"bidPrice": "99.99", "askPrice": "100.0"}
        self.avg_price = 100.05
        res, _, _, audit, _ = self.run_trade(**dict(self.LONG, tp2_price=107.0))
        self.assertTrue(res["success"], res.get("error"))
        rec = self.audit_record(audit)
        self.assertEqual(rec["entry_reference"], "book")
        self.assertEqual(rec["entry_reference_price"], 100.0)
        self.assertEqual(rec["book_side_price"], 100.0)
        self.assertAlmostEqual(rec["entry_slippage_pct"], 0.05, places=6)   # filled 0.05% worse than the reference

    def test_short_slippage_sign(self):
        self.book = {"bidPrice": "100.0", "askPrice": "100.01"}
        self.avg_price = 99.9
        res, _, _, audit, _ = self.run_trade(**dict(self.SHORT, tp2_price=93.0))
        self.assertTrue(res["success"], res.get("error"))
        self.assertAlmostEqual(self.audit_record(audit)["entry_slippage_pct"], 0.1, places=6)  # sold 0.1% lower

    def test_clamp_sized_from_the_worse_book_price(self):
        # LONG crossed at 101 (book ask 101.5), SL 95: cap qty round_step(6.25 x 0.98 / 6.5) = 0.942 (1.02 from last)
        self.ticker = 101.0
        self.book = {"bidPrice": "101.4", "askPrice": "101.5"}
        res, gates, _, audit, err = self.run_trade(order_type="STOP_MARKET", trigger_price=100.0, margin_usdt=40.0,
                                                   sl_price=95.0, tp1_price=105.0, tp2_price=130.0)
        self.assertTrue(res["success"], res.get("error"))
        expected = eft.round_step(6.25 * gate_limits.RISK_CLAMP_HAIRCUT / 6.5, 0.001, 3)
        self.assertEqual(expected, 0.942)
        self.assertEqual(float(self.entry_orders()[0]["quantity"]), expected)
        self.assertEqual(gates.call_args.args[4], expected)
        self.assertIn("[RISK CLAMP] qty 1.188 -> 0.942", err)
        self.assertIn("measured from 101.5 (book)", err)
        self.assertAlmostEqual(audit.call_args.args[1], expected * 101.0 / 3, places=6)  # issue #126 invariant

    def test_testnet_makes_no_book_read_and_is_unchanged(self):
        self.book = {"bidPrice": "100.00", "askPrice": "100.01"}
        res, _, _, audit, err = self.run_trade(env="testnet", **self.LONG)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(self.book_calls(), [])
        self.assertNotIn("crossed-trigger R:R gate relaxed", err)   # exactly 3.0 from last, as before
        self.assertNotIn("entry_reference", self.audit_record(audit))

    def test_non_crossed_prod_orders_make_no_book_read(self):
        self.book = {"bidPrice": "100.00", "askPrice": "100.01"}
        res, _, _, audit, _ = self.run_trade(order_type="MARKET", sl_price=98.0, tp1_price=104.0, tp2_price=106.0)
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(self.book_calls(), [])
        self.assertNotIn("entry_reference", self.audit_record(audit))
        tpe.write_guardian_state(self.ws)
        self.calls = []
        res, _, _, _, _ = self.run_trade(order_type="STOP_MARKET", trigger_price=101.0, sl_price=99.0,
                                         tp1_price=102.0, tp2_price=103.0)
        self.assertTrue(res.get("conditional_entry"), res)
        self.assertEqual(self.book_calls(), [])


# =============================================================================
# K2 prompt
# =============================================================================
class TestK2Prompt(unittest.TestCase):

    PATHS = ("by one of three paths (state which): the volume path (`vol_ratio >= 1.4x`) or the two absorption paths, "
             "OIB (absorption >= 60% with |OIB| >= 0.15) and A-tier (Tier A+/A only: absorption >= 55% with "
             "R:R >= 3:1).")
    SOURCES = ((".agents", "agents", "isolated_market_evaluator", "agent.md"),
               (".claude", "agents", "isolated_market_evaluator.md"))

    def read(self, rel):
        with open(os.path.join(BASE_DIR, *rel), encoding="utf-8") as f:
            return f.read()

    def test_k2_line_names_the_absorption_paths_once(self):
        for rel in self.SOURCES:
            with self.subTest(path=os.path.join(*rel)):
                text = self.read(rel)
                k2 = next(line for line in text.splitlines() if line.strip().startswith("- K2 Institutional volume"))
                self.assertIn(self.PATHS, k2)
                self.assertEqual(k2.count("two absorption paths"), 1)
                self.assertNotIn("Tier A+/A setups may pass via", k2)
                self.assertLess(k2.index(self.PATHS), k2.index("`abs:unscored`"))

    def test_example_5_abs_unscored_row_fails_k2(self):
        for rel in self.SOURCES:
            with self.subTest(path=os.path.join(*rel)):
                text = self.read(rel)
                examples = text.split("<few_shot_examples>")[1].split("</few_shot_examples>")[0]
                shot = examples.split('<example id="eval_neg_02_fake_tier_s_downgrade">')[1].split("</example>")[0]
                self.assertIn("`abs:unscored` with absorption 70% and vol_ratio 1.2x", shot)
                rows = {m.group(1): m.group(0) for m in
                        re.finditer(r"- \[[ x]\] SEIUSDT LONG (K1|K2|K3|C3\.1|K4) .*", shot)}
                self.assertEqual(set(rows), {"K1", "K2", "K3", "C3.1", "K4"})
                k2 = rows["K2"]
                self.assertTrue(k2.startswith("- [ ]"), k2)
                for part in ("abs:unscored", "absorption 70%", "vol_ratio 1.2x", "not FAKE_TIER_S"):
                    self.assertIn(part, k2)
                self.assertTrue(k2.rstrip().endswith("-> FAIL"), k2)
                self.assertTrue(rows["K3"].rstrip().endswith("-> N/A"))
                self.assertTrue(rows["K4"].startswith("- [ ]"))
                self.assertIn("-> REJECTED", rows["K4"])
                final = shot.split("<final_response>")[1].split("</final_response>")[0]
                dossier = json.loads(final.split("<dossier_json>")[1].split("</dossier_json>")[0])
                self.assertEqual(dossier["status"], "REJECTED")
                self.assertEqual(dossier["approved_symbols"], [])
                self.assertEqual(check_precondition_checklist(final, dossier), [])

    def test_no_new_example_added(self):
        examples = self.read(self.SOURCES[0]).split("<few_shot_examples>")[1].split("</few_shot_examples>")[0]
        ids = re.findall(r'<example id="([^"]+)">', examples)
        self.assertEqual(len(ids), 15)
        self.assertEqual(ids[-1], "eval_neg_09_market_data_unavailable")


# =============================================================================
# #232 pre-arm rejection message
# =============================================================================
class TestPrearmRejectMessage(tpe.ExecutorHarness):

    MSG = "Order type not supported for this symbol."

    def send_with_prearm(self, response):
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if method == "POST" and endpoint == ALGO_ENDPOINT and (params or {}).get("closePosition") == "true":
                self.calls.append((method, endpoint, dict(params)))
                if isinstance(response, Exception):
                    raise response
                return dict(response)
            return self.fake(method, endpoint, params, target_env)
        return send

    def test_stop_market_never_sends_the_prearm(self):
        # Owner decision (issue #232): a STOP_MARKET entry is not pre-armed, so it can no longer hit -4509.
        res = self.execute(order_type="STOP_MARKET", trigger_price=102.347,
                           send=self.send_with_prearm({"code": -4509, "msg": self.MSG}))
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["prearm_status"], "skipped:no_position")
        self.assertEqual([c for c in self.calls if c[0] == "POST" and c[1] == ALGO_ENDPOINT
                          and c[2].get("closePosition") == "true"], [])
        self.assertNotIn("prearm_reject_msg", res)
        self.assertNotIn("prearm_anomaly", res)
        self.report.assert_not_called()

    def test_minus_4509_with_message_resting_limit_logged_not_reported(self):
        with self.assertLogs("execute_futures_trade", level="WARNING") as logs:
            res = self.execute(order_type="LIMIT", limit_price=98.767,
                               send=self.send_with_prearm({"code": -4509, "msg": self.MSG}))
        self.assertTrue(res["pending_limit_entry"], res)
        self.assertEqual(res["prearm_status"], "rejected:-4509")
        self.assertEqual(res["prearm_reject_msg"], self.MSG)
        self.assertIn("Stop Loss not pre-armed (rejected:-4509). ", res["message"])
        self.assertIn(self.MSG, res["message"])
        self.assertNotIn("prearm_anomaly", res)
        self.report.assert_not_called()
        rec = tpe.read_registry(self.ws)["testnet:SOLUSDT:7"]
        self.assertEqual((rec["prearm_status"], rec["prearm_reject_msg"]), ("rejected:-4509", self.MSG))
        line = next(m for m in logs.output if "rejected:-4509" in m)
        self.assertIn("SOLUSDT", line)
        self.assertIn(self.MSG, line)

    def test_other_rejection_code_still_reported(self):
        msg = "Reach max stop order limit."
        res = self.execute(order_type="LIMIT", limit_price=98.767,
                           send=self.send_with_prearm({"code": -4045, "msg": msg}))
        self.assertEqual(res["prearm_status"], "rejected:-4045")
        self.assertEqual(res["prearm_reject_msg"], msg)
        self.assertEqual(res["prearm_anomaly"]["status"], "rejected:-4045")
        self.assertIn(msg, res["prearm_anomaly"]["message"])
        self.report.assert_called_once()
        kw = self.report.call_args.kwargs
        self.assertIn(msg, kw["context"])
        self.assertIn("the position guardian protects it at fill", kw["context"])
        self.assertEqual(kw["error_detail"], "SOLUSDT pre-arm rejected:-4045", "dedup fingerprint unchanged")
        self.assertEqual(kw["title"], "prearm_resting_entry_stop: pre-armed stop of SOLUSDT rejected:-4045")

    def test_no_message_no_field(self):
        with self.assertLogs("execute_futures_trade", level="WARNING") as logs:
            res = self.execute(order_type="LIMIT", limit_price=98.767, send=self.send_with_prearm({"code": -4509}))
        self.assertEqual(res["prearm_status"], "rejected:-4509")
        self.assertNotIn("prearm_reject_msg", res)
        self.assertNotIn("prearm_reject_msg", tpe.read_registry(self.ws)["testnet:SOLUSDT:7"])
        self.assertIn("Stop Loss not pre-armed (rejected:-4509). ", res["message"])
        self.assertNotIn("Binance message", res["message"])
        self.assertTrue(any("rejected:-4509" in m and "no Binance message" in m for m in logs.output), logs.output)
        self.report.assert_not_called()

    def test_minus_2021_still_not_an_anomaly(self):
        res = self.execute(order_type="LIMIT", limit_price=98.767,
                           send=self.send_with_prearm({"code": -2021, "msg": "Order would immediately trigger."}))
        self.assertEqual(res["prearm_status"], "rejected:-2021")
        self.assertEqual(res["prearm_reject_msg"], "Order would immediately trigger.")
        self.assertNotIn("prearm_anomaly", res)
        self.report.assert_not_called()

    def test_non_api_rejection_has_no_message_field(self):
        for response in ({"error": "timeout"}, RuntimeError("socket closed")):
            with self.subTest(response=repr(response)):
                out = None
                with patch("execute_futures_trade.send_signed_request", side_effect=self.send_with_prearm(response)), \
                     patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
                    out = eft.prearm_resting_entry_stop("SOLUSDT", "SELL", 97.0, 100.0, target_env="testnet")
                self.assertTrue(out["prearm_status"].startswith("rejected:"), out)
                self.assertNotIn("prearm_reject_msg", out)

    def test_message_sanitized(self):
        long_msg = "Bad   request\n\twith " + "x" * 300
        out = eft._api_reject_msg({"code": -4509, "msg": long_msg})
        self.assertTrue(out.startswith("Bad request with xxx"))
        self.assertEqual(len(out), 200)
        self.assertNotIn("\n", out)
        for res in ({"code": 200, "msg": "success"}, {"msg": "no code"}, {"code": -4509, "msg": "   "},
                    {"code": "abc", "msg": "x"}, "text", None, [{"code": -1}]):
            with self.subTest(res=repr(res)):
                self.assertIsNone(eft._api_reject_msg(res))
        self.assertEqual(eft._api_reject_msg({"code": "-4509", "msg": "m"}), "m")

    def test_note_unchanged_without_message(self):
        self.assertEqual(eft._prearm_note({"prearm_status": "rejected:-2021"}, 95.0),
                         "Stop Loss not pre-armed (rejected:-2021). ")
        self.assertEqual(eft._prearm_note({"prearm_status": "rejected:-4509", "prearm_reject_msg": "m"}, 95.0),
                         'Stop Loss not pre-armed (rejected:-4509). Binance message: "m". ')


if __name__ == "__main__":
    unittest.main()
