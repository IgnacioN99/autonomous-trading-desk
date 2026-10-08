#!/usr/bin/env python3
"""
test_issue_106_exit_manager_hardening.py - Offline tests for the dynamic exit manager hardening
(issues #106, #107 and #108, follow-ups of #95).

#106: before +2.0x ATR_15m MFE or a TP1 fill an activated (r_multiple) trail is capped one tick short of entry,
      never in the True Net BE fee dead zone; never-loosen still holds; with be_allowed the old behaviour stays.
#107: target_env on the market-entry audit record, env alias normalisation, stale same-direction records rejected
      against the userTrades open time, reference_unverified without userTrades, other-env records never hide a
      matching one, audit_unreadable / audit_corrupt_lines warnings in the trailing result and guardian state,
      yolo_source in yolo_before_tp1 results.
#108: klines limit 99 (weight 1); the CLI --symbol path returns structured JSON on exceptions.
No network, no orders: the exchange is FakeExchange, klines are synthetic and logs live in temp directories.
"""

import io
import os
import sys
import json
import time
import tempfile
import unittest
import contextlib
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "loops"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import dynamic_exit_manager as dem
import position_guardian_loop as pgl
import user_profile as up
from test_exit_management import FakeExchange, offline, long_position, stop, write_audit, ALGO_ENDPOINT
from test_issue_95_trailing_activation import make_klines, flat_pre, closed_atr, market, short_position

TICK_FILTERS = {"tickSize": 0.1, "precision_price": 1}

# LONG run after entry 100.0: highest high 101.2 (MFE 1.2), swing low 100.6 at the middle bar (two bars each side),
# so the structural level (100.6 - 0.3x ATR) lands ABOVE entry while MFE stays under 2x ATR.
LONG_POST = [(101.1, 100.8, 101.0), (101.2, 100.9, 101.0), (101.0, 100.6, 100.8), (101.2, 100.8, 101.1),
             (101.2, 100.9, 101.0)]
LONG_FORMING = (101.1, 100.9, 101.0)
# SHORT mirror around 100.0: lowest low 98.8 (MFE 1.2), swing high 99.4 below entry.
SHORT_POST = [(200.0 - l, 200.0 - h, 200.0 - c) for (h, l, c) in LONG_POST]
SHORT_FORMING = (99.1, 98.9, 99.0)


class UserTradesExchange(FakeExchange):
    """FakeExchange that also answers /fapi/v1/userTrades with the given fills (list) or error payload."""

    def __init__(self, positions, user_trades, **kw):
        super().__init__(positions, **kw)
        self.user_trades = user_trades

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == "/fapi/v1/userTrades":
            self.calls.append((method, endpoint, dict(params or {})))
            return [dict(f) for f in self.user_trades] if isinstance(self.user_trades, list) else self.user_trades
        return super().__call__(method, endpoint, params, target_env, retry_count)


def buy_fill(ts_s, qty="10", price="100.0"):
    return {"id": 1, "orderId": 1, "symbol": "BTCUSDT", "side": "BUY", "positionSide": "BOTH", "qty": qty,
            "price": price, "time": int(ts_s * 1000)}


def long_record(ws, **over):
    rec = dict(symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=99.0, total_qty=10, tp1_qty=3,
               is_yolo=False, target_env="testnet", timestamp=int(time.time()) - 3600)
    rec.update(over)
    write_audit(ws, **rec)


class TestDeadZoneCap(unittest.TestCase):
    """#106: an r_multiple activation never puts the stop at or beyond entry before +2x ATR / TP1."""

    def _calc(self, direction, post, forming, current_sl, planned_sl, mark, tp1_filled=False):
        klines, entry_ts = make_klines(flat_pre(), post, forming, time.time())
        with patch("execute_futures_trade.get_symbol_filters", return_value=dict(TICK_FILTERS)), market(klines) as gk:
            calc = dem.calculate_structural_stop("BTCUSDT", direction, 100.0, current_sl_price=current_sl,
                                                 target_env="testnet", planned_sl=planned_sl, entry_ts=entry_ts,
                                                 tp1_filled=tp1_filled, mark_price=mark)
        return calc, klines, gk

    def test_long_r_multiple_capped_below_entry(self):
        calc, klines, _ = self._calc("LONG", LONG_POST, LONG_FORMING, 99.0, 99.0, 101.0)
        atr = closed_atr(klines)
        # Preconditions: +1R reached, MFE under 2x ATR (no BE yet), structural level above entry.
        self.assertEqual(calc["activation_reason"], "r_multiple")
        self.assertLess(calc["mfe"], 2.0 * atr)
        self.assertGreater(100.6 - 0.3 * atr, 100.0)
        self.assertGreater(101.0 - 0.5 * atr, 100.0)
        self.assertEqual(calc["new_structural_sl"], 99.9)  # entry - 1 tick
        self.assertFalse(calc["is_profit_locked"])
        self.assertTrue(calc["should_update"])

    def test_short_r_multiple_capped_above_entry(self):
        calc, klines, _ = self._calc("SHORT", SHORT_POST, SHORT_FORMING, 101.0, 101.0, 99.0)
        atr = closed_atr(klines)
        self.assertEqual(calc["activation_reason"], "r_multiple")
        self.assertLess(calc["mfe"], 2.0 * atr)
        self.assertLess(99.4 + 0.3 * atr, 100.0)
        self.assertLess(99.0 + 0.5 * atr, 100.0)
        self.assertEqual(calc["new_structural_sl"], 100.1)  # entry + 1 tick
        self.assertFalse(calc["is_profit_locked"])
        self.assertTrue(calc["should_update"])

    def test_off_grid_entry_cap_stays_strictly_beyond(self):
        klines, entry_ts = make_klines(flat_pre(), LONG_POST, LONG_FORMING, time.time())
        with patch("execute_futures_trade.get_symbol_filters", return_value=dict(TICK_FILTERS)), market(klines):
            long_calc = dem.calculate_structural_stop("BTCUSDT", "LONG", 100.05, current_sl_price=99.05,
                                                      target_env="testnet", planned_sl=99.05, entry_ts=entry_ts,
                                                      tp1_filled=False, mark_price=101.0)
        self.assertEqual(long_calc["activation_reason"], "r_multiple")
        self.assertLess(long_calc["new_structural_sl"], 100.05)
        klines, entry_ts = make_klines(flat_pre(), SHORT_POST, SHORT_FORMING, time.time())
        with patch("execute_futures_trade.get_symbol_filters", return_value=dict(TICK_FILTERS)), market(klines):
            short_calc = dem.calculate_structural_stop("BTCUSDT", "SHORT", 99.95, current_sl_price=100.95,
                                                       target_env="testnet", planned_sl=100.95, entry_ts=entry_ts,
                                                       tp1_filled=False, mark_price=99.0)
        self.assertEqual(short_calc["activation_reason"], "r_multiple")
        self.assertGreater(short_calc["new_structural_sl"], 99.95)

    def test_long_capped_through_update(self):
        klines, entry_ts = make_klines(flat_pre(), LONG_POST, LONG_FORMING, time.time())
        fake = FakeExchange([long_position(mark="101.0")], algos=[stop(501, 99.0)])
        with offline(fake) as ws, market(klines):
            long_record(ws, timestamp=entry_ts)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertTrue(res["updated"], res)
        self.assertEqual(res["activation_reason"], "r_multiple")
        self.assertEqual(res["new_sl"], 99.9)
        self.assertLess(fake.write_index("POST", ALGO_ENDPOINT)[0], fake.write_index("DELETE", ALGO_ENDPOINT)[0])

    def test_short_capped_through_update(self):
        klines, entry_ts = make_klines(flat_pre(), SHORT_POST, SHORT_FORMING, time.time())
        fake = FakeExchange([short_position(mark="99.0")], algos=[stop(601, 101.0, side="BUY")])
        with offline(fake) as ws, market(klines):
            long_record(ws, direction="SHORT", sl_price=101.0, timestamp=entry_ts)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertTrue(res["updated"], res)
        self.assertEqual(res["new_sl"], 100.1)

    def test_current_stop_past_entry_is_kept(self):
        # After --move-breakeven the live stop is 100.2: the cap (99.9) must never loosen it.
        calc, _, _ = self._calc("LONG", LONG_POST, LONG_FORMING, 100.2, 99.0, 101.0)
        self.assertEqual(calc["activation_reason"], "r_multiple")
        self.assertEqual(calc["new_structural_sl"], 100.2)
        self.assertFalse(calc["should_update"])

        klines, entry_ts = make_klines(flat_pre(), LONG_POST, LONG_FORMING, time.time())
        fake = FakeExchange([long_position(mark="101.0")], algos=[stop(501, 100.2)])
        with offline(fake) as ws, market(klines):
            long_record(ws, timestamp=entry_ts)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["reference_source"], "trade_audit")
        self.assertEqual(res["activation_reason"], "r_multiple")
        self.assertEqual(res["reason"], "not_tighter")
        self.assertFalse(res["updated"])
        self.assertEqual(fake.writes(), [])

    def test_short_current_stop_past_entry_is_kept(self):
        calc, _, _ = self._calc("SHORT", SHORT_POST, SHORT_FORMING, 99.8, 101.0, 99.0)
        self.assertEqual(calc["activation_reason"], "r_multiple")
        self.assertEqual(calc["new_structural_sl"], 99.8)
        self.assertFalse(calc["should_update"])

    def test_be_allowed_keeps_old_behaviour(self):
        calc, klines, _ = self._calc("LONG", LONG_POST, LONG_FORMING, 99.0, 99.0, 101.0, tp1_filled=True)
        atr = closed_atr(klines)
        self.assertEqual(calc["activation_reason"], "tp1_filled")
        expected = min(max(100.6 - 0.3 * atr, 101.2 - 1.8 * atr, 100.0 * 1.002), 101.0 - 0.5 * atr)
        self.assertEqual(calc["new_structural_sl"], eft.round_price(expected, 0.1, 1))
        self.assertGreaterEqual(calc["new_structural_sl"], 100.2)

        calc, klines, _ = self._calc("SHORT", SHORT_POST, SHORT_FORMING, 101.0, 101.0, 99.0, tp1_filled=True)
        atr = closed_atr(klines)
        expected = max(min(99.4 + 0.3 * atr, 98.8 + 1.8 * atr, 100.0 * 0.998), 99.0 + 0.5 * atr)
        # Issue #197: SHORT stops are rounded up (away from price) to the tick.
        self.assertEqual(calc["new_structural_sl"], dem._round_stop(expected, "SHORT", 0.1, 1))
        self.assertGreaterEqual(calc["new_structural_sl"], expected)
        self.assertLessEqual(calc["new_structural_sl"], 99.8)


class TestMarketEntryTargetEnv(unittest.TestCase):
    """#107.1: the market-entry audit record carries target_env (read back through the executor's reader)."""

    @patch("quant_risk_engine.get_account_equity", return_value=10000.0)
    @patch("execute_futures_trade.setup_margin_and_leverage")
    @patch("execute_futures_trade.get_symbol_filters")
    @patch("execute_futures_trade.send_signed_request")
    @patch("execute_futures_trade.place_algo_stop_loss", return_value={"algoId": 4242})
    @patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 4242}))
    @patch("execute_futures_trade.place_take_profit_orders", return_value=({"orderId": 1}, {"orderId": 2}))
    def test_market_entry_record_has_target_env(self, _tp, _verify, _sl, mock_send, mock_filters, _setup, _eq):
        mock_filters.return_value = {"stepSize": 0.001, "minQty": 0.001, "tickSize": 0.1,
                                     "precision_qty": 3, "precision_price": 1, "minNotional": 5.0}

        def fake_send(method, endpoint, params=None, target_env=None):
            if endpoint in ("/fapi/v2/balance", "/fapi/v3/balance"):
                return [{"asset": "USDT", "balance": "10000.0"}]
            if endpoint == "/fapi/v1/ticker/price":
                return {"price": "100.0"}
            if endpoint == "/fapi/v1/order" and method == "POST":
                return {"orderId": 50001, "avgPrice": "100.0", "status": "FILLED"}
            return {}
        mock_send.side_effect = fake_send

        with tempfile.TemporaryDirectory() as ws, patch("execute_futures_trade._workspace_dir", return_value=ws), \
             patch("user_profile.load_user_profile", return_value=dict(up.DEFAULT_PROFILE)), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network blocked in tests")), \
             contextlib.redirect_stdout(io.StringIO()):
            res = eft.execute_complete_trade(symbol="BTCUSDT", direction="LONG", leverage=3, margin_usdt=100.0,
                                             sl_price=98.0, tp1_price=103.0, tp2_price=106.0, target_env="testnet",
                                             bypass_eval_gate=True)
            rec = eft.latest_trade_audit_record("BTCUSDT")
        self.assertTrue(res.get("success"), res)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["target_env"], "testnet")
        self.assertEqual(rec["direction"], "LONG")


class TestTradeReferenceMatching(unittest.TestCase):
    """#107.1 / #107.2: env aliases, stale same-direction records, unverified references, other-env records."""

    def _resolve(self, fake, records, target_env="testnet", pos=None):
        pos = pos or dict(long_position(), updateTime=str(int(time.time() * 1000) - 60_000))
        with offline(fake) as ws:
            for r in records:
                write_audit(ws, **r)
            return dem._resolve_trade_reference_full("BTCUSDT", pos, "LONG", 95.0, target_env)

    @staticmethod
    def rec(**over):
        r = dict(symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=97.0, total_qty=10, tp1_qty=3,
                 target_env="testnet", timestamp=int(time.time()) - 600)
        r.update(over)
        return r

    def test_env_alias_normalised(self):
        sl, _, src, rec, _ = self._resolve(FakeExchange([]), [self.rec(target_env="mainnet")], target_env="prod")
        self.assertEqual((sl, src), (97.0, "trade_audit"))
        self.assertEqual(rec["target_env"], "mainnet")
        _, _, src, _, _ = self._resolve(FakeExchange([]), [self.rec(target_env="mainnet")], target_env="testnet")
        self.assertEqual(src, "current_stop")

    def test_old_same_direction_record_rejected_by_user_trades(self):
        now = time.time()
        # Old trade: same direction, entry within 0.5%, larger qty (passes audit_record_matches_position).
        old = self.rec(total_qty=20, tp1_qty=6, timestamp=int(now) - 86400)
        fake = UserTradesExchange([], [buy_fill(now - 600)])
        sl, ts, src, rec, warnings = self._resolve(fake, [old])
        self.assertEqual(src, "current_stop")
        self.assertEqual(sl, 95.0)
        self.assertIsNone(rec)
        self.assertNotIn("reference_unverified", warnings)
        self.assertTrue([c for c in fake.calls if c[1] == "/fapi/v1/userTrades"])

    def test_old_record_tp1_state_not_trusted_through_update(self):
        # The stale record (qty 20) would make detect_tp1_filled say True for a fresh 10-qty position.
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = UserTradesExchange([long_position(mark="100.5")], [buy_fill(now - 600)], algos=[stop(501, 98.0)])
        with offline(fake) as ws, market(klines):
            write_audit(ws, **self.rec(total_qty=20, tp1_qty=6, timestamp=int(now) - 86400))
            self.assertTrue(eft.detect_tp1_filled("BTCUSDT", 10)[0])
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["reference_source"], "current_stop")
        self.assertEqual(res["reason"], "trail_not_activated")
        self.assertIsNone(res["activation_reason"])
        self.assertEqual(fake.writes(), [])

    def test_record_after_open_time_accepted_without_warning(self):
        now = time.time()
        fake = UserTradesExchange([], [buy_fill(now - 600)])
        sl, ts, src, rec, warnings = self._resolve(fake, [self.rec(timestamp=int(now) - 590)])
        self.assertEqual((sl, src), (97.0, "trade_audit"))
        self.assertEqual(ts, float(int(now) - 590))
        self.assertEqual(warnings, [])

    def test_user_trades_unavailable_keeps_record_with_warning(self):
        for payload in ({"code": -1, "msg": "not mapped"}, {}):
            with self.subTest(payload=payload):
                fake = UserTradesExchange([], payload)
                sl, _, src, rec, warnings = self._resolve(fake, [self.rec(total_qty=20)])
                self.assertEqual((sl, src), (97.0, "trade_audit"))
                self.assertIn("reference_unverified", warnings)

    def test_reference_unverified_surfaces_in_update_result(self):
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = FakeExchange([long_position(mark="100.5")], algos=[stop(501, 98.0)])
        with offline(fake) as ws, market(klines):
            write_audit(ws, **self.rec(sl_price=98.0))
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        self.assertEqual(res["reference_source"], "trade_audit")
        self.assertIn("reference_unverified", res["warnings"])

    def test_newer_other_env_record_does_not_hide_matching_one(self):
        records = [self.rec(sl_price=97.0, target_env="testnet"),
                   self.rec(sl_price=96.0, target_env="prod", timestamp=int(time.time()) - 60)]
        sl, _, src, rec, _ = self._resolve(FakeExchange([]), records)
        self.assertEqual((sl, src), (97.0, "trade_audit"))
        self.assertEqual(rec["target_env"], "testnet")

    def test_newer_other_direction_record_does_not_hide_matching_one(self):
        records = [self.rec(sl_price=97.0), self.rec(direction="SHORT", sl_price=103.0)]
        sl, _, src, _, _ = self._resolve(FakeExchange([]), records)
        self.assertEqual((sl, src), (97.0, "trade_audit"))

    def test_public_wrapper_keeps_three_tuple(self):
        fake = FakeExchange([])
        pos = dict(long_position(), updateTime=str(int(time.time() * 1000) - 60_000))
        with offline(fake) as ws:
            write_audit(ws, **self.rec())
            out = dem.resolve_trade_reference("BTCUSDT", pos, "LONG", 95.0, "testnet")
        self.assertEqual(len(out), 3)
        self.assertEqual(out[2], "trade_audit")


class TestAuditReadabilityWarnings(unittest.TestCase):
    """#107.3: an unreadable or corrupt trades_audit is a visible warning, never a silent fallback or a block."""

    def _unreadable(self, ws):
        os.makedirs(os.path.join(ws, "logs", "trades_audit.jsonl"))  # a directory: open() raises OSError

    def _corrupt(self, ws):
        long_record(ws, sl_price=98.0)
        with open(os.path.join(ws, "logs", "trades_audit.jsonl"), "a", encoding="utf-8") as f:
            f.write("{not json\n[1, 2]\n")

    def _update(self, prepare):
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = FakeExchange([long_position(mark="100.5")], algos=[stop(501, 98.0)])
        with offline(fake) as ws, market(klines):
            prepare(ws)
            res = dem.update_position_to_structural_stop("BTCUSDT", target_env="testnet")
        return res, fake

    def test_unreadable_audit_warning(self):
        res, fake = self._update(self._unreadable)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["reference_source"], "current_stop")
        self.assertIn("audit_unreadable", res["warnings"])
        self.assertEqual(fake.writes(), [])

    def test_corrupt_lines_warning(self):
        res, _ = self._update(self._corrupt)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["reference_source"], "trade_audit")  # valid records are still used
        self.assertIn("audit_corrupt_lines:2", res["warnings"])

    def test_clean_audit_has_no_audit_warning(self):
        res, _ = self._update(lambda ws: long_record(ws, sl_price=98.0))
        self.assertFalse([w for w in res["warnings"] if w.startswith("audit_")])

    def test_read_audit_tail_counts_malformed_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"n": 1}\n\nnot json\n"str"\n{"n": 2}\n')
            stats = {}
            recs = eft.read_audit_tail(path, stats=stats)
            self.assertEqual([r["n"] for r in recs], [1, 2])
            self.assertEqual(stats["malformed_lines"], 2)
            stats = {}
            eft.read_audit_tail(path, max_bytes=len('"str"\n{"n": 2}\n') + 1, stats=stats)
            self.assertEqual(stats["malformed_lines"], 1)  # the dropped partial first line is not counted

    def _guardian(self, prepare):
        log_dir = tempfile.mkdtemp()
        now = time.time()
        klines, _ = make_klines(flat_pre(), [], (100.8, 99.9, 100.5), now)
        fake = FakeExchange([long_position(mark="100.5")], algos=[stop(501, 98.0)])
        healthy = {"status": "HEALTHY_MOMENTUM", "range_pct": 1.2, "recommendation": "HOLD", "message": "ok"}
        with offline(fake) as ws, market(klines), patch.object(pgl, "DEFAULT_LOG_DIR", log_dir), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=healthy), \
             patch("report_agent_issue.report_issue"), \
             contextlib.redirect_stdout(io.StringIO()):
            prepare(ws)
            code = pgl.main(["--once", "--env", "testnet"])
        with open(os.path.join(log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            return code, json.load(f)

    def test_guardian_state_carries_unreadable_warning(self):
        code, state = self._guardian(self._unreadable)
        trailing = state["positions"][0]["trailing"]
        self.assertIn("audit_unreadable", trailing["warnings"])
        self.assertTrue(trailing["success"])
        self.assertEqual(code, 0)
        self.assertNotIn("trailing", state.get("error_stages", []))

    def test_guardian_state_carries_corrupt_warning(self):
        _, state = self._guardian(self._corrupt)
        self.assertIn("audit_corrupt_lines:2", state["positions"][0]["trailing"]["warnings"])


class TestYoloSource(unittest.TestCase):
    """#107.4: yolo_before_tp1 results say why the position counts as YOLO."""

    def test_leverage_source(self):
        fake = FakeExchange([long_position("PEPEUSDT", leverage="15")], algos=[stop(501, 95.0, symbol="PEPEUSDT")])
        with offline(fake):
            res = dem.update_position_to_structural_stop("PEPEUSDT", target_env="testnet")
        self.assertEqual(res["reason"], "yolo_before_tp1")
        self.assertEqual(res["yolo_source"], "leverage")
        self.assertEqual(fake.writes(), [])

    def test_matched_record_source(self):
        fake = FakeExchange([long_position("PEPEUSDT", leverage="3")], algos=[stop(501, 95.0, symbol="PEPEUSDT")])
        with offline(fake) as ws:
            long_record(ws, symbol="PEPEUSDT", sl_price=95.0, is_yolo=True)
            res = dem.update_position_to_structural_stop("PEPEUSDT", target_env="testnet")
        self.assertEqual(res["reason"], "yolo_before_tp1")
        self.assertEqual(res["yolo_source"], "trade_audit")


class TestIssue108(unittest.TestCase):

    def test_klines_limit_is_weight_one(self):
        klines, entry_ts = make_klines(flat_pre(), LONG_POST, LONG_FORMING, time.time())
        with patch("execute_futures_trade.get_symbol_filters", return_value=dict(TICK_FILTERS)), market(klines) as gk:
            dem.calculate_structural_stop("BTCUSDT", "LONG", 100.0, current_sl_price=99.0, target_env="testnet",
                                          planned_sl=99.0, entry_ts=entry_ts, tp1_filled=False, mark_price=101.0)
        self.assertEqual(gk.call_args.kwargs["limit"], 99)
        self.assertLessEqual(gk.call_args.kwargs["limit"], 99)
        self.assertEqual(gk.call_args.kwargs["interval"], "15m")

    def test_cli_symbol_exception_is_structured_json(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        out = io.StringIO()
        with offline(fake), patch("dynamic_exit_manager.get_klines_data", side_effect=OSError("klines down")), \
             contextlib.redirect_stdout(out):
            code = dem.main(["--symbol", "btcusdt", "--json", "--env", "testnet"])
        self.assertEqual(code, 1)
        data = json.loads(out.getvalue())
        self.assertEqual(data["total_active"], 1)
        res = data["results"][0]
        self.assertEqual(res, {"symbol": "BTCUSDT", "success": False, "updated": False, "reason": "exception",
                               "error": "klines down", "message": "Error: klines down"})
        self.assertEqual(fake.writes(), [])


if __name__ == "__main__":
    unittest.main()
