#!/usr/bin/env python3
"""
Issue #207: mechanical Daily Loss Gate for opening orders, plus the calibration / doctor / PR #214 follow-ups.

1. utils/daily_loss_gate.evaluate: daily USDT limit on the start-of-day equity, full-SL streak (a partial loss is
   skipped, a scratch or a winner ends it, net R falls back to gross), YOLO-only limit, fail closed on bad inputs.
2. UTC day boundary: fetch_day_fills starts at 00:00 UTC of the injected `now`; yesterday's 23:59 loss is ignored.
3. trade_outcomes.closed_trades_today: the per-trade list behind summarize_closed_today.
4. Executor: PROD refusal before any write, fail closed on unreadable / truncated fills, MCP or an exception, TESTNET
   skip, risk-reducing paths never call the gate, effective YOLO from the dossier, the audit / registry field.
5. Profile validators and the example file; 6. sync state (live and error); 7. brief; 8. prompt; 9. doctor.
10. Calibration: Student-t bound, minimum margin, score_schema_version chain; 11. squeeze fallback without the
    calibration module; SHORT funding penalty per 8h.

Hermetic: temp workspaces, every exchange call faked, urlopen blocked.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "hooks"),
           os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_guard_bypasses as tgb  # noqa: E402  (fixtures only; imported first, see test_issue_79)
import pre_trade_guard  # noqa: E402
import execute_futures_trade as eft  # noqa: E402
import sync_session_state as sss  # noqa: E402
import trade_outcomes as to  # noqa: E402
import user_profile as up  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402
import trading_doctor  # noqa: E402
import broad_market_radar as bmr  # noqa: E402
import test_pending_entries as tpe  # noqa: E402  (fixtures only)
import test_analytics_cli as tac  # noqa: E402  (fixtures only)
from utils import daily_loss_gate as dlg  # noqa: E402
from utils import score_calibration as scal  # noqa: E402

DAY = 20735 * 86400 * 1000  # a UTC midnight (ms)
H = 3600 * 1000
AGENT_MD = os.path.join(BASE_DIR, ".agents", "agents", "isolated_market_evaluator", "agent.md")


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    tgb.setUpModule()
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def fill(fid, order_id, side, price, qty, t_ms, pnl=0.0, comm=0.0, asset="USDT", symbol="BTCUSDT"):
    return {"id": fid, "orderId": order_id, "symbol": symbol, "side": side, "price": str(price), "qty": str(qty),
            "realizedPnl": str(pnl), "commission": str(comm), "commissionAsset": asset, "positionSide": "BOTH",
            "time": int(t_ms)}


def closed_trade(i, r, yolo=False, symbol=None, day=DAY):
    """(audit record, fills) of a LONG entered at 100 with SL 90 (risk 10, qty 1) and closed at 100 + 10 R."""
    symbol = symbol or f"T{i}USDT"
    t_in = day + i * H + 60_000
    rec = {"symbol": symbol, "direction": "LONG", "entry_price": 100.0, "sl_price": 90.0, "total_qty": 1.0,
           "target_env": "prod", "timestamp": t_in // 1000 + 1, "entry_order_id": 1000 + i, "is_yolo": yolo}
    fills = [fill(10 * i + 1, 1000 + i, "BUY", 100.0, 1.0, t_in, symbol=symbol),
             fill(10 * i + 2, 2000 + i, "SELL", 100.0 + 10.0 * r, 1.0, t_in + H // 2, pnl=10.0 * r, symbol=symbol)]
    return rec, fills


def trades(*rs, yolo=()):
    return [{"exit_ms": DAY + i * H, "realized_r_net": r, "realized_r_gross": r, "is_yolo": i in yolo}
            for i, r in enumerate(rs)]


def evaluate(net=0.0, trade_list=(), equity=1000.0, yolo_order=False, risk=0.02, **over):
    limits = dict({"daily_stop_r": 3.0, "max_consecutive_sl": 2, "yolo_max_daily_losses": 1}, **over)
    return dlg.evaluate(net, list(trade_list), risk_pct=risk, equity_now=equity, is_yolo_order=yolo_order, **limits)


# =============================================================================
# 1. Pure gate
# =============================================================================
class TestEvaluate(unittest.TestCase):

    def test_daily_limit_at_just_above_and_below(self):
        # start equity 90 (equity now 84.6 after -5.40 realized), limit 3.0R x 2% x 90 = 5.40
        at = evaluate(-5.40, equity=84.6)
        self.assertEqual((at["blocked"], at["scope"]), (True, "all"))
        self.assertAlmostEqual(at["day_loss_limit_usdt"], 5.4, places=4)
        self.assertIn("day_net_realized_usdt=-5.40 <= limit_usdt=-5.40", at["reason"])
        self.assertIn("daily_stop_r=3 x risk_pct=2% x start_equity=90.00", at["reason"])
        self.assertIn("no new entries until 00:00 UTC", at["reason"])
        self.assertTrue(evaluate(-5.41, equity=84.59)["blocked"])
        below = evaluate(-5.39, equity=84.61)
        self.assertEqual((below["blocked"], below["scope"], below["reason"]), (False, None, None))

    def test_start_equity_is_equity_before_todays_result(self):
        self.assertAlmostEqual(evaluate(10.0, equity=110.0)["day_loss_limit_usdt"], 6.0, places=6)   # start 100
        self.assertAlmostEqual(evaluate(-10.0, equity=90.0)["day_loss_limit_usdt"], 6.0, places=6)   # start 100
        self.assertEqual(evaluate(-10.0, equity=90.0)["day_net_realized_usdt"], -10.0)

    def test_streak_cases(self):
        cases = {(-1.0, -0.9): 2,            # two full SL
                 (-1.0, 0.02, -0.9): 1,      # the scratch ends the walk
                 (-1.0, -0.4, -0.9): 2,      # a partial loss is skipped, not a reset
                 (-1.0, 0.5, -0.9): 1,       # a winner ends it
                 (-0.9, -1.0, 0.6): 0,       # newest is a winner
                 (-0.79, -0.8): 1}           # -0.8 counts, -0.79 is partial
        for rs, streak in cases.items():
            with self.subTest(rs=rs):
                self.assertEqual(dlg.consecutive_full_sl(trades(*rs)), streak)
                state = evaluate(-1.0, trades(*rs))
                self.assertEqual((state["consecutive_full_sl"], state["blocked"]), (streak, streak >= 2))
        state = evaluate(-1.0, trades(-1.0, -0.9))
        self.assertIn("consecutive_full_sl=2 >= max_consecutive_sl=2", state["reason"])
        self.assertEqual(state["scope"], "all")
        # order of the walk is by exit time, not list order
        shuffled = list(reversed(trades(-1.0, 0.5, -0.9)))
        self.assertEqual(dlg.consecutive_full_sl(shuffled), 1)

    def test_net_none_falls_back_to_gross(self):
        t = [{"exit_ms": DAY + 1, "realized_r_net": None, "realized_r_gross": -1.0},
             {"exit_ms": DAY + 2, "realized_r_net": None, "realized_r_gross": -0.95}]
        self.assertEqual(dlg.consecutive_full_sl(t), 2)
        self.assertEqual(dlg.consecutive_full_sl([{"exit_ms": 1, "realized_r_net": 0.3, "realized_r_gross": -1.0}]),
                         0)
        self.assertEqual(dlg.consecutive_full_sl([{"exit_ms": 1}]), 0)  # no R: skipped

    def test_yolo_limit_only_blocks_yolo_orders(self):
        t = trades(0.5, -1.0, yolo={1})
        std = evaluate(-5.0, t, yolo_order=False)
        yolo = evaluate(-5.0, t, yolo_order=True)
        self.assertEqual((std["blocked"], std["scope"]), (False, "yolo"))
        self.assertEqual((yolo["blocked"], yolo["scope"], yolo["yolo_full_losses"]), (True, "yolo", 1))
        self.assertIn("yolo_full_losses=1 >= yolo_max_daily_losses=1", yolo["reason"])
        self.assertFalse(evaluate(-5.0, trades(0.5, -0.5, yolo={1}), yolo_order=True)["blocked"])  # partial loss

    def test_yolo_losses_count_toward_streak_and_daily_usdt(self):
        t = trades(-1.0, -1.0, yolo={0, 1})
        state = evaluate(-20.0, t, yolo_order=False, yolo_max_daily_losses=5)
        self.assertEqual((state["blocked"], state["scope"], state["consecutive_full_sl"]), (True, "all", 2))
        daily = evaluate(-61.0, trades(-1.0, yolo={0}), equity=939.0, yolo_order=False, yolo_max_daily_losses=5)
        self.assertEqual((daily["blocked"], daily["scope"]), (True, "all"))  # 61 > 3 x 2% x 1000

    def test_unscored_closed_trades_are_counted_and_named(self):
        """Round 2: a closed trade with neither net nor gross R is skipped by the streak but reported."""
        none = {"exit_ms": DAY + 9 * H, "realized_r_net": None, "realized_r_gross": None}
        state = evaluate(-1.0, trades(-1.0) + [none])
        self.assertEqual((state["blocked"], state["consecutive_full_sl"], state["unscored_closed"]), (False, 1, 1))
        self.assertIn("unscored_closed=1", state["reason"])
        self.assertIn("not counted in the consecutive-SL streak", state["reason"])
        blocked = evaluate(-1.0, trades(-1.0, -1.0) + [none, dict(none)])
        self.assertTrue(blocked["reason"].startswith("DAILY LOSS GATE: consecutive_full_sl=2"))
        self.assertIn("unscored_closed=2", blocked["reason"])
        clean = evaluate(-1.0, trades(-1.0))
        self.assertEqual((clean["unscored_closed"], clean["reason"]), (0, None))

    def test_invalid_inputs_block(self):
        for over in ({"net": None}, {"equity": "x"}, {"risk": 0}, {"daily_stop_r": 0}, {"max_consecutive_sl": 0}):
            with self.subTest(over=over):
                kw = dict(over)
                state = evaluate(kw.pop("net", -1.0), **kw)
                self.assertTrue(state["blocked"])
                self.assertIn("fail closed", state["reason"])

    def test_day_net_realized_counts_only_usdt_commissions(self):
        fills = [fill(1, 1, "SELL", 1, 1, DAY, pnl=-5.0, comm=0.2),
                 fill(2, 2, "SELL", 1, 1, DAY, pnl=1.0, comm=0.001, asset="BNB")]
        self.assertEqual(dlg.day_net_realized(fills), (-4.2, True))
        self.assertEqual(dlg.day_net_realized([fill(1, 1, "SELL", 1, 1, DAY, pnl=2.0, comm=0.5)]), (1.5, False))


# =============================================================================
# 2. UTC day boundary
# =============================================================================
class DayFills:
    """GET /fapi/v1/userTrades without a symbol: the fills at or after startTime (any other request fails)."""

    def __init__(self, fills, positions=(), error=None):
        self.fills, self.positions, self.error, self.params = fills, list(positions), error, []

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        if method != "GET":
            raise AssertionError(f"write request {method} {endpoint}")
        if endpoint == "/fapi/v1/userTrades" and "symbol" not in params:
            self.params.append(params)
            if self.error is not None:
                return dict(self.error)
            return [dict(f) for f in sorted(self.fills, key=lambda f: (f["time"], f["id"]))
                    if f["time"] >= params["startTime"]][:params["limit"]]
        if endpoint == "/fapi/v2/positionRisk":
            return list(self.positions)
        raise AssertionError(f"unexpected request {endpoint}")


class TestUtcDayBoundary(unittest.TestCase):

    def test_start_of_day_from_injected_now(self):
        now = DAY / 1000 + 5 * 3600
        self.assertEqual(sss.get_start_of_day_utc(now), DAY)
        self.assertEqual(sss.get_start_of_day_utc(DAY / 1000 - 60), DAY - 86400 * 1000)
        self.assertEqual(dlg.day_start_ms(DAY / 1000), DAY)

    def test_yesterdays_late_loss_is_ignored(self):
        rec_y, fills_y = closed_trade(0, -1.0, symbol="YUSDT", day=DAY - 86400 * 1000)
        for f in fills_y:  # yesterday 23:59
            f["time"] = DAY - 60_000 + (1 if f["side"] == "SELL" else 0)
        rec_t, fills_t = closed_trade(1, -1.0, symbol="TUSDT")
        fake = DayFills(fills_y + fills_t)
        ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(ws, "logs"))
        with open(os.path.join(ws, "logs", "trades_audit.jsonl"), "w", encoding="utf-8") as f:
            for r in (rec_y, rec_t):
                f.write(json.dumps(r) + "\n")
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("execute_futures_trade._workspace_dir", return_value=ws), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
            ok, reason, state = eft.check_daily_loss_gate("prod", False, {"risk_pct_equity": 0.02}, 1000.0,
                                                          open_positions=[], now=DAY / 1000 + 3 * 3600)
        self.assertEqual(fake.params[0]["startTime"], DAY)
        self.assertTrue(ok, reason)
        self.assertEqual((state["consecutive_full_sl"], state["day_net_realized_usdt"]), (1, -10.0))
        # the same fills judged on yesterday's day would count both losses
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("execute_futures_trade._workspace_dir", return_value=ws), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
            ok, reason, state = eft.check_daily_loss_gate("prod", False, {"risk_pct_equity": 0.02}, 1000.0,
                                                          open_positions=[], now=DAY / 1000 - 30)
        self.assertEqual(fake.params[-1]["startTime"], DAY - 86400 * 1000)
        self.assertFalse(ok)
        self.assertIn("consecutive_full_sl=2", reason)


# =============================================================================
# 3. Per-trade list
# =============================================================================
class TestClosedTradesToday(unittest.TestCase):

    def test_list_matches_summary_and_skips_unaudited_symbols(self):
        recs, fills = [], []
        for i, r in enumerate((0.5, -1.0, -0.4)):
            rec, fs = closed_trade(i, r, yolo=(i == 1))
            recs.append(rec)
            fills += fs
        fills.append(fill(99, 99, "SELL", 10, 1, DAY + 9 * H, pnl=-7.0, symbol="NOAUDITUSDT"))
        out = to.closed_trades_today(recs, fills, DAY, "prod", open_positions=set())
        self.assertEqual([t["symbol"] for t in out], ["T0USDT", "T1USDT", "T2USDT"])
        self.assertEqual([t["realized_r_net"] for t in out], [0.5, -1.0, -0.4])
        self.assertEqual([t["is_yolo"] for t in out], [False, True, False])
        self.assertTrue(all(t["exit_ms"] >= DAY for t in out))
        self.assertAlmostEqual(out[1]["net_pnl_usdt"], -10.0)
        summary = to.summarize_closed_today(recs, fills, DAY, "prod", open_positions=set())
        self.assertEqual(summary, to.summarize_trade_list(out, fills))
        self.assertEqual((summary["trades_closed"], summary["wins"], summary["losses"]), (3, 1, 2))

    def test_outcome_rows_mark_dossier_yolo_and_stay_out_of_calibration(self):
        """Round 2: outcome rows use the gate's YOLO notion (CLI flag OR dossier YOLO), so merge_store skips them."""
        for over in ({"is_yolo": True}, {"dossier_tier": "Tier A (YOLO)"}, {"daily_loss_gate": {"is_yolo_order": True}}):
            with self.subTest(over=over):
                rec, fs = closed_trade(0, 1.0)
                rec.update(over, dossier_score=85, score_schema_version=scal.SCORE_SCHEMA_VERSION)
                row = to.resolve_trade(rec, fs, {}, [], None, "prod", klines=False)
                self.assertIs(row["is_yolo"], True)
                row["env"] = "prod"
                self.assertEqual(scal.merge_store(None, [row], now=1)["trades"], {})
        rec, fs = closed_trade(0, 1.0)
        row = to.resolve_trade(rec, fs, {}, [], None, "prod", klines=False)
        self.assertIs(row["is_yolo"], False)
        with patch.object(to, "fetch_fills", return_value=(None, "down")), \
             patch.object(to, "load_entries", return_value=[dict(rec, dossier_tier="YOLO")]), \
             patch.object(to, "load_trail_stops", return_value={}), patch.object(to, "load_filters", return_value={}):
            rows, _, unavailable = to.build_outcomes("prod", 0, klines=False)
        self.assertEqual((unavailable, rows[0]["status"], rows[0]["is_yolo"]), (1, "fills_unavailable", True))

    def test_dossier_yolo_counts_as_yolo(self):
        rec, fs = closed_trade(0, -1.0)
        rec["daily_loss_gate"] = {"is_yolo_order": True}
        self.assertTrue(to.closed_trades_today([rec], fs, DAY, "prod")[0]["is_yolo"])
        rec2, fs2 = closed_trade(1, -1.0)
        rec2["dossier_tier"] = "YOLO"
        self.assertTrue(to.closed_trades_today([rec2], fs2, DAY, "prod")[0]["is_yolo"])


# =============================================================================
# 4. Executor
# =============================================================================
class GateWorkspace(unittest.TestCase):

    PROFILE = {"risk_pct_equity": 0.02}

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.ws, "logs"))

    def audit(self, *recs):
        with open(os.path.join(self.ws, "logs", "trades_audit.jsonl"), "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def gate(self, fake, env="prod", yolo=False, mcp=False, equity=1000.0, profile=None, positions=()):
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=mcp), \
             patch.object(sss, "get_start_of_day_utc", return_value=DAY):
            return eft.check_daily_loss_gate(env, yolo, dict(profile or self.PROFILE), equity,
                                             open_positions=list(positions))


class TestCheckDailyLossGate(GateWorkspace):

    def test_two_full_stops_refuse_and_a_clean_day_passes(self):
        (r0, f0), (r1, f1) = closed_trade(0, -1.0), closed_trade(1, -1.0)
        self.audit(r0, r1)
        ok, reason, state = self.gate(DayFills(f0 + f1))
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("DAILY LOSS GATE: consecutive_full_sl=2"), reason)
        self.assertEqual((state["blocked"], state["is_yolo_order"]), (True, False))
        (r2, f2), = [closed_trade(2, 1.0)]
        self.audit(r0, r1, r2)
        ok, reason, state = self.gate(DayFills(f0 + f1 + f2))
        self.assertTrue(ok, reason)
        self.assertEqual((state["consecutive_full_sl"], state["blocked"]), (0, False))

    def test_daily_usdt_limit_refuses(self):
        rec, fs = closed_trade(0, -7.0)    # -70 USDT on a 1000 start: limit 3 x 2% x 1070 = 64.2
        self.audit(rec)
        ok, reason, state = self.gate(DayFills(fs), equity=1000.0)
        self.assertFalse(ok)
        self.assertIn("day_net_realized_usdt=-70.00", reason)

    def test_yolo_order_refused_after_one_yolo_full_loss(self):
        rec, fs = closed_trade(0, -1.0, yolo=True)
        self.audit(rec)
        self.assertTrue(self.gate(DayFills(fs), yolo=False)[0])
        ok, reason, _ = self.gate(DayFills(fs), yolo=True)
        self.assertFalse(ok)
        self.assertIn("YOLO", reason)

    def test_fail_closed_reads(self):
        cases = {
            "error reply": (DayFills([], error={"code": -1102, "msg": "symbol required"}), "symbol required"),
            "exception": (MagicMock(side_effect=ConnectionError("reset")), "ConnectionError"),
        }
        for name, (fake, fragment) in cases.items():
            with self.subTest(name=name):
                ok, reason, state = self.gate(fake)
                self.assertFalse(ok)
                self.assertIn("today's fills unreadable", reason)
                self.assertIn(fragment, reason)
                self.assertIn("fail closed", reason)
                self.assertEqual((state["blocked"], state["scope"]), (True, "all"))
        with patch.object(sss, "fetch_day_fills", return_value=([], True)):
            ok, reason, _ = self.gate(DayFills([]))
        self.assertFalse(ok)
        self.assertIn("truncated", reason)
        with patch.object(sss, "fetch_day_fills", side_effect=RuntimeError("boom")):
            ok, reason, _ = self.gate(DayFills([]))
        self.assertFalse(ok)
        self.assertIn("RuntimeError: boom", reason)
        ok, reason, _ = self.gate(DayFills([]), mcp=True)
        self.assertFalse(ok)
        self.assertIn("MCP", reason)

    def test_trades_not_countable_per_trade_refuse(self):
        # closing fills of a flat symbol with no audit trade: the streak cannot be verified
        ok, reason, _ = self.gate(DayFills([fill(1, 1, "SELL", 10, 1, DAY + H, pnl=-3.0)]))
        self.assertFalse(ok)
        self.assertIn("cannot be counted per trade", reason)
        # a TP1 partial of a position still open is not a closed trade: allowed
        rec, fs = closed_trade(0, 1.0, symbol="OPENUSDT")
        fs[1]["qty"] = "0.3"
        self.audit(rec)
        ok, reason, _ = self.gate(DayFills(fs), positions=[("OPENUSDT", "LONG")])
        self.assertTrue(ok, reason)

    def test_unaudited_closing_symbols_are_noted_not_blocking(self):
        """Round 4: closing fills of a symbol with no audit record (counts kept per trade) are named, never block."""
        rec, fs = closed_trade(0, 1.0)
        self.audit(rec)
        manual = [fill(900 + i, 900 + i, "SELL", 10, 1, DAY + 5 * H, pnl=-1.0, symbol=f"M{i:02d}USDT")
                  for i in range(12)]
        ok, reason, state = self.gate(DayFills(fs + manual))
        self.assertTrue(ok, reason)
        self.assertEqual(state["unaudited_closing_symbols"], [f"M{i:02d}USDT" for i in range(10)])  # capped at 10
        self.assertIn("unaudited_closing_symbols=M00USDT", state["reason"])
        self.assertEqual(state["day_net_realized_usdt"], 10.0 - 12.0)  # still in the USDT figure
        ok, _, clean = self.gate(DayFills(fs))
        self.assertEqual((ok, clean["unaudited_closing_symbols"], clean["reason"]), (True, [], None))
        level, msg = trading_doctor.daily_loss_gate_line({"target_env": "prod", "daily_loss_gate": state}, "prod")
        self.assertEqual(level, "warn")
        self.assertIn("M00USDT", msg)
        self.assertIn("NOT in the consecutive-SL streak", msg)
        self.assertEqual(trading_doctor.daily_loss_gate_line({"target_env": "prod", "daily_loss_gate": clean},
                                                             "prod")[0], "ok")

    def test_testnet_skipped_without_requests(self):
        fake = MagicMock(side_effect=AssertionError("no request in TESTNET"))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            ok, reason, state = self.gate(fake, env="testnet")
        self.assertTrue(ok)
        self.assertEqual(state["skipped"], "testnet")
        self.assertIn("Daily Loss Gate skipped (TESTNET)", err.getvalue())
        self.assertEqual(out.getvalue(), "")  # stdout stays pure JSON for the CLI

    def test_unreadable_audit_refuses(self):
        os.makedirs(os.path.join(self.ws, "logs", "trades_audit.jsonl"))  # a directory: open() fails
        ok, reason, _ = self.gate(DayFills([]))
        self.assertFalse(ok)
        self.assertIn("audit unreadable", reason)


class DayHarness(tpe.ExecutorHarness):
    """ExecutorHarness with the real Daily Loss Gate: the fake also serves today's userTrades."""

    def setUp(self):
        super().setUp()
        self.day_fills = []

    def fake(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == "/fapi/v1/userTrades" and "symbol" not in (params or {}):
            self.calls.append((method, endpoint, dict(params or {})))
            return [dict(f) for f in self.day_fills if f["time"] >= params["startTime"]]
        return super().fake(method, endpoint, params, target_env, retry_count)

    def execute_real_gate(self, env="prod", **kwargs):
        args = dict(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                    sl_price=97.0, tp1_price=110.0, tp2_price=120.0, target_env=env)
        if env == "testnet":
            args["bypass_eval_gate"] = True
        args.update(kwargs)
        tpe.write_session_state(self.ws, self.positions)
        with patch("execute_futures_trade.send_signed_request", side_effect=self.fake), \
             patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
             patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.enforce_evaluation_dossier",
                   return_value=getattr(self, "eval_result", (True, "ok", None))), \
             patch("execute_futures_trade.check_mechanical_gates", return_value=(True, None)), \
             patch("execute_futures_trade.get_symbol_filters", return_value=dict(tpe.EX_FILTERS)), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 9})), \
             patch("quant_risk_engine.get_account_equity", return_value=1000.0), \
             patch("user_profile.load_user_profile", return_value=dict(tpe.EX_PROFILE, risk_pct_equity=0.02)), \
             patch.object(sss, "get_start_of_day_utc", return_value=DAY), \
             patch("report_agent_issue.report_issue"):
            return eft.execute_complete_trade(**args)


class TestExecutorIntegration(DayHarness):

    def test_prod_block_refused_before_any_write_or_leverage_call(self):
        (r0, f0), (r1, f1) = closed_trade(0, -1.0), closed_trade(1, -1.0)
        os.makedirs(os.path.join(self.ws, "logs"), exist_ok=True)
        with open(os.path.join(self.ws, "logs", "trades_audit.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(r0) + "\n" + json.dumps(r1) + "\n")
        self.day_fills = f0 + f1
        res = self.execute_real_gate()
        self.assertFalse(res["success"])
        self.assertTrue(res["daily_loss_gate_rejection"])
        self.assertIn("DAILY LOSS GATE: consecutive_full_sl=2", res["error"])
        self.assertEqual(self.writes(), [])
        self.assertFalse(any(c[1] in ("/fapi/v1/leverage", "/fapi/v1/marginType") for c in self.calls))

    def test_prod_clean_day_trades_and_audit_carries_the_state(self):
        res = self.execute_real_gate()
        self.assertTrue(res["success"], res.get("error"))
        audit = tpe.read_jsonl(self.ws, "trades_audit.jsonl")[-1]
        self.assertEqual(audit["daily_loss_gate"]["blocked"], False)
        self.assertIs(audit["daily_loss_gate"]["is_yolo_order"], False)
        self.assertIn("consecutive_full_sl", audit["daily_loss_gate"])

    def test_unreadable_profile_is_visible_not_blocking(self):
        """Round 4: a failed profile load keeps the defaults (no block) but flags state, audit record and stderr."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
             patch("user_profile.load_user_profile", side_effect=ValueError("bad json")):
            res = self._execute_without_profile_patch()
        self.assertTrue(res["success"], res.get("error"))
        audit = tpe.read_jsonl(self.ws, "trades_audit.jsonl")[-1]
        self.assertIs(audit["profile_unreadable"], True)
        self.assertIs(audit["daily_loss_gate"]["profile_unreadable"], True)
        self.assertIs(audit["daily_loss_gate"]["blocked"], False)
        self.assertIn("user profile unreadable (ValueError: bad json)", err.getvalue())
        ok = self.execute_real_gate()
        audit = tpe.read_jsonl(self.ws, "trades_audit.jsonl")[-1]
        self.assertTrue(ok["success"])
        self.assertNotIn("profile_unreadable", audit)
        self.assertNotIn("profile_unreadable", audit["daily_loss_gate"])

    def _execute_without_profile_patch(self):
        """execute_real_gate, but the caller controls user_profile.load_user_profile."""
        with patch("execute_futures_trade.send_signed_request", side_effect=self.fake), \
             patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
             patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.enforce_evaluation_dossier", return_value=(True, "ok", None)), \
             patch("execute_futures_trade.check_mechanical_gates", return_value=(True, None)), \
             patch("execute_futures_trade.get_symbol_filters", return_value=dict(tpe.EX_FILTERS)), \
             patch("execute_futures_trade.uses_mcp_gateway", return_value=False), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 9})), \
             patch("quant_risk_engine.get_account_equity", return_value=1000.0), \
             patch.object(sss, "get_start_of_day_utc", return_value=DAY), \
             patch("report_agent_issue.report_issue"):
            tpe.write_session_state(self.ws, self.positions)
            return eft.execute_complete_trade(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                                              sl_price=97.0, tp1_price=110.0, tp2_price=120.0, target_env="prod")

    def test_testnet_never_reads_the_day(self):
        res = self.execute_real_gate(env="testnet")
        self.assertTrue(res["success"], res.get("error"))
        self.assertFalse(any(c[1] == "/fapi/v1/userTrades" for c in self.calls))

    def test_effective_yolo_from_the_dossier_candidate(self):
        gate = MagicMock(return_value=tpe.DAILY_LOSS_GATE_ALLOW)
        self.eval_result = (True, "ok", {"symbol": "SOLUSDT", "direction": "LONG", "tier": "A", "is_yolo": True})
        with patch("execute_futures_trade.check_daily_loss_gate", gate):
            self.execute_real_gate()
        self.assertIs(gate.call_args[0][1], True)
        gate.reset_mock()
        self.eval_result = (True, "ok", {"symbol": "SOLUSDT", "direction": "LONG", "tier": "A"})
        with patch("execute_futures_trade.check_daily_loss_gate", gate):
            self.execute_real_gate()
        self.assertIs(gate.call_args[0][1], False)
        gate.reset_mock()
        with patch("execute_futures_trade.check_daily_loss_gate", gate):
            self.execute_real_gate(is_yolo=True, leverage=15)
        self.assertIs(gate.call_args[0][1], True)

    def test_resting_entry_registry_carries_the_state_to_the_fill_audit(self):
        state = dict(tpe.DAILY_LOSS_GATE_ALLOW[2], consecutive_full_sl=1)
        tpe.write_guardian_state(self.ws)
        with patch("execute_futures_trade.check_daily_loss_gate", return_value=(True, None, state)):
            res = self.execute_real_gate(order_type="STOP_MARKET", trigger_price=102.347)
        self.assertTrue(res["success"], res.get("error"))
        rec = tpe.read_registry(self.ws)[res["pending_entry_key"]]
        self.assertEqual(rec["daily_loss_gate"], state)
        # Round 2: the fill's audit record (protect-pending) carries the registry's state
        from test_exit_management import FakeExchange, offline, long_position
        ws = tempfile.mkdtemp()
        tpe.write_registry(ws, tpe.make_record(daily_loss_gate=rec["daily_loss_gate"]))
        fake = FakeExchange([long_position(amt="10", entry="101.0", mark="101.5")])
        with offline(fake, workspace=ws), patch("execute_futures_trade.uses_mcp_gateway", return_value=False):
            out = eft.protect_pending_entries(target_env="testnet")
        self.assertTrue(out["ok"], out["errors"])
        audit = tpe.read_jsonl(ws, "trades_audit.jsonl")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["daily_loss_gate"], state)


class TestRiskReducingPathsNeverCallTheGate(unittest.TestCase):

    def test_cli_branches(self):
        ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(ws, "logs"))
        trap = MagicMock(side_effect=AssertionError("Daily Loss Gate consulted by a risk-reducing path"))
        handlers = {"--close-position --symbol BTCUSDT": "close_position_market",
                    "--move-breakeven --symbol BTCUSDT": "move_sl_to_breakeven",
                    "--auto-heal": "audit_and_auto_heal_orphans",
                    "--audit-orphans": "audit_orphan_positions",
                    "--protect-pending": "protect_pending_entries",
                    "--positions --json": "get_positions_report"}
        for flags, handler in handlers.items():
            with self.subTest(flags=flags):
                fn = MagicMock(return_value={"success": True, "ok": True, "message": "", "warnings": []})
                argv = ["execute_futures_trade.py"] + flags.split() + ["--env", "prod"]
                with patch.object(sys, "argv", argv), patch.object(eft, handler, fn), \
                     patch.object(eft, "check_daily_loss_gate", trap), \
                     patch.object(eft, "execute_complete_trade", trap), \
                     patch("execute_futures_trade._workspace_dir", return_value=ws), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        eft.main()
                fn.assert_called_once()
                trap.assert_not_called()

    def test_only_execute_complete_trade_calls_it(self):
        import inspect
        source = inspect.getsource(eft)
        body = inspect.getsource(eft.execute_complete_trade)
        self.assertEqual(source.count("check_daily_loss_gate("), 2)  # the definition and one call
        self.assertIn("check_daily_loss_gate(", body)
        for name in ("close_position_market", "move_sl_to_breakeven", "audit_orphan_positions",
                     "protect_pending_entries", "heal_orphan_position", "get_positions_report"):
            self.assertNotIn("check_daily_loss_gate", inspect.getsource(getattr(eft, name)), name)


# =============================================================================
# 5. Profile
# =============================================================================
class TestProfileLimits(unittest.TestCase):

    def test_defaults_and_example(self):
        self.assertEqual(up.get_daily_loss_limits({}),
                         {"daily_stop_r": 3.0, "max_consecutive_sl": 2, "yolo_max_daily_losses": 1})
        self.assertEqual((up.DEFAULT_PROFILE["daily_stop_r"], up.DEFAULT_PROFILE["max_consecutive_sl"],
                          up.DEFAULT_PROFILE["yolo_max_daily_losses"],
                          up.DEFAULT_PROFILE["tier_s_calibration_min_lcb_r"]), (3.0, 2, 1, 0.1))
        with open(os.path.join(BASE_DIR, "config", "user_profile.json.example"), encoding="utf-8") as f:
            example = json.load(f)
        for key in ("daily_stop_r", "max_consecutive_sl", "yolo_max_daily_losses", "tier_s_calibration_min_lcb_r"):
            self.assertEqual(example[key], up.DEFAULT_PROFILE[key], key)

    def test_invalid_values_fall_back_and_cannot_disable(self):
        for bad in (0, -1, 25, "3", None, True, float("nan"), float("inf")):
            self.assertEqual(up.get_daily_loss_limits({"daily_stop_r": bad})["daily_stop_r"], 3.0, bad)
        for key, default in (("max_consecutive_sl", 2), ("yolo_max_daily_losses", 1)):
            for bad in (0, -2, 1.5, "2", None, True):
                self.assertEqual(up.get_daily_loss_limits({key: bad})[key], default, (key, bad))
        self.assertEqual(up.get_daily_loss_limits({"daily_stop_r": 20, "max_consecutive_sl": 5,
                                                   "yolo_max_daily_losses": 3}),
                         {"daily_stop_r": 20.0, "max_consecutive_sl": 5, "yolo_max_daily_losses": 3})
        self.assertEqual(up.get_daily_loss_limits({"daily_stop_r": 0.5})["daily_stop_r"], 0.5)

    def test_min_lcb_validator(self):
        self.assertEqual(scal.calibration_policy({"tier_s_calibration_min_lcb_r": 0.25})[2], 0.25)
        self.assertEqual(scal.calibration_policy({"tier_s_calibration_min_lcb_r": 0})[2], 0.0)
        for bad in (-0.1, "0.2", None, True, float("nan")):
            self.assertEqual(scal.calibration_policy({"tier_s_calibration_min_lcb_r": bad})[2], 0.1, bad)


# =============================================================================
# 6. Ledger sync
# =============================================================================
class SyncExchange(DayFills):

    def __init__(self, fills, balance="1000", **kw):
        super().__init__(fills, **kw)
        self.balance = balance

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == "/fapi/v1/ticker/price":
            return {"price": "100.0"}
        if endpoint in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
            return []
        if endpoint == "/fapi/v2/balance":
            return [{"asset": "USDT", "balance": self.balance}] if self.balance else {"code": -1001}
        return super().__call__(method, endpoint, params, target_env, retry_count)


class TestSyncState(unittest.TestCase):

    def run_sync(self, fake, audit=(), env="prod", profile=None):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "session_state.json")
        with open(os.path.join(tmp, "trades_audit.jsonl"), "w", encoding="utf-8") as f:
            for r in audit:
                f.write(json.dumps(r) + "\n")
        with patch.object(sss, "LOGS_DIR", tmp), patch.object(sss, "STATE_FILE", path), \
             patch.object(sss, "AUDIT_LOG", os.path.join(tmp, "trades_audit.jsonl")), \
             patch.object(sss, "get_start_of_day_utc", return_value=DAY), \
             patch.dict(sys.modules, {"shadow_tracker": None}), \
             patch("user_profile.load_user_profile", return_value=dict(profile or {"risk_pct_equity": 0.02})), \
             patch("execute_futures_trade.send_signed_request", side_effect=fake):
            return sss.sync_session_state(target_env=env)

    def test_live_state_carries_the_gate(self):
        (r0, f0), (r1, f1) = closed_trade(0, -1.0), closed_trade(1, -1.0)
        state = self.run_sync(SyncExchange(f0 + f1), audit=[r0, r1])
        gate = state["daily_loss_gate"]
        self.assertEqual((gate["blocked"], gate["scope"], gate["consecutive_full_sl"]), (True, "all", 2))
        self.assertEqual(gate["day_net_realized_usdt"], -20.0)
        self.assertIn("ACTIVE (all)", sss.format_markdown_summary(state))
        clean = self.run_sync(SyncExchange([]), audit=[])["daily_loss_gate"]
        self.assertEqual((clean["blocked"], clean["scope"]), (False, None))

    def test_error_and_unreadable_states_fail_closed(self):
        with patch.object(sss, "STATE_FILE", os.path.join(tempfile.mkdtemp(), "s.json")):
            err = sss.write_error_state("positionRisk failed", 0, "now", "prod", 0.0)
        live = self.run_sync(SyncExchange([]), audit=[])
        self.assertIn("daily_loss_gate", err)
        self.assertIn("daily_loss_gate", live)
        self.assertEqual((err["daily_loss_gate"]["blocked"], err["daily_loss_gate"]["scope"]), (True, "all"))
        self.assertTrue(err["daily_loss_gate"]["reason"].startswith("unavailable:"))
        for fake, fragment in ((SyncExchange([], error={"code": -1102, "msg": "symbol required"}), "fills unreadable"),
                               (SyncExchange([], balance=None), "balance unreadable"),
                               (SyncExchange([fill(1, 1, "SELL", 10, 1, DAY + H, pnl=-3.0)]), "per trade")):
            with self.subTest(fragment=fragment):
                gate = self.run_sync(fake, audit=[])["daily_loss_gate"]
                self.assertEqual((gate["blocked"], gate["scope"]), (True, "all"))
                self.assertIn(fragment, gate["reason"])

    def test_sync_notes_unaudited_closing_symbols(self):
        rec, fs = closed_trade(0, 1.0)
        manual = [fill(901, 901, "SELL", 10, 1, DAY + 5 * H, pnl=-1.0, symbol="MANUSDT")]
        gate = self.run_sync(SyncExchange(fs + manual), audit=[rec])["daily_loss_gate"]
        self.assertEqual((gate["blocked"], gate["unaudited_closing_symbols"]), (False, ["MANUSDT"]))
        self.assertIn("unaudited_closing_symbols=MANUSDT", gate["reason"])

    def test_testnet_not_blocked(self):
        gate = self.run_sync(SyncExchange([], error={"code": -1}), env="testnet")["daily_loss_gate"]
        self.assertEqual((gate["blocked"], gate["scope"]), (False, None))


# =============================================================================
# 7. Brief
# =============================================================================
class TestBrief(unittest.TestCase):

    def assemble(self, state, screening=None):
        tmp = tempfile.mkdtemp()
        with patch.object(peb, "BRIEF_FILE", os.path.join(tmp, "primed_brief.json")), \
             patch.object(peb, "ensure_fresh_state", return_value=dict(state)), \
             patch.object(peb, "get_latest_screening_payload", return_value=dict(screening or {})), \
             patch.object(peb, "load_recent_insights", return_value=[]), \
             patch.object(peb, "_get_equity", return_value=1000.0), \
             patch("user_profile.load_user_profile", return_value={"risk_pct_equity": 0.02, "daily_stop_r": 2.5}):
            return peb.assemble_primed_brief(target_env="prod")

    def test_limits_gate_and_quality_fields(self):
        gate = {"blocked": True, "scope": "all", "reason": "DAILY LOSS GATE: x", "day_net_realized_usdt": -9.0,
                "day_loss_limit_usdt": 5.4, "consecutive_full_sl": 2, "yolo_full_losses": 0}
        brief = self.assemble({"target_env": "prod", "daily_loss_gate": gate,
                               "closed_today_summary": {"counted_by": "trades", "fills_closed": 0, "truncated": False}})
        rp = brief["risk_profile"]
        self.assertEqual((rp["daily_stop_r"], rp["max_consecutive_sl"], rp["yolo_max_daily_losses"]), (2.5, 2, 1))
        self.assertEqual(brief["daily_loss_gate"], {"blocked": True, "scope": "all", "reason": "DAILY LOSS GATE: x"})
        self.assertNotIn("closed_today_data", brief["ground_truth_portfolio"])  # all defaults: omitted
        self.assertIn("Daily Loss Gate:** `ACTIVE (all)`", peb.format_markdown_brief(brief))
        bad = self.assemble({"target_env": "prod", "daily_loss_gate": dict(gate, blocked=False, scope=None),
                             "closed_today_summary": {"counted_by": "fills", "fills_closed": 3, "truncated": True,
                                                      "trade_summary_error": "boom"}})
        self.assertEqual(bad["ground_truth_portfolio"]["closed_today_data"],
                         {"counted_by": "fills", "fills_closed": 3, "truncated": True, "trade_summary_error": "boom"})
        self.assertEqual(bad["daily_loss_gate"], {"blocked": False, "scope": None, "reason": None})
        # an inactive gate's informational reason (unscored / unaudited notes) never reaches the evaluator
        note = dict(gate, blocked=False, scope=None, unaudited_closing_symbols=["MANUSDT"], unscored_closed=1,
                    reason="DAILY LOSS GATE: inactive; unaudited_closing_symbols=MANUSDT")
        quiet = self.assemble({"target_env": "prod", "daily_loss_gate": note})
        self.assertEqual(quiet["daily_loss_gate"], {"blocked": False, "scope": None, "reason": None})
        self.assertNotIn("MANUSDT", json.dumps(quiet))

    def test_missing_or_foreign_state_reads_blocked(self):
        for state in ({"target_env": "prod"}, {"target_env": "testnet", "daily_loss_gate": {"blocked": False}}, {}):
            with self.subTest(state=state):
                self.assertEqual(self.assemble(state)["daily_loss_gate"]["blocked"], True)

    def test_funding_info_warning_forwarded(self):
        brief = self.assemble({"target_env": "prod"}, {"funding_info_warning": "fundingInfo unavailable (X)"})
        self.assertEqual(brief["funding_info_warning"], "fundingInfo unavailable (X)")
        self.assertNotIn("funding_info_warning", self.assemble({"target_env": "prod"}))

    def test_brief_budget_with_every_flag_set(self):
        """Token budget (< 1,800 tokens, estimated as bytes / 4 as in #23) with 10 macro-rejected SHORTs, every
        brief-level flag, the daily-loss-gate state and two flagged candidates."""
        cand = {"symbol": "AAAUSDT", "direction": "SHORT", "tier": "Tier A (Strong Confluence / Hedge)",
                "tier_code": "A", "confidence": 64, "current_price": 1.2345, "trigger_price": 1.2301,
                "sl_price": 1.2551, "tp1_price": 1.1849, "tp2_price": 1.1297, "rr_ratio": 4.0, "risk_pct": 2.03,
                "vol_ratio": 2.6, "rsi_15m": 81.2, "oi_z_score": 2.4, "funding_rate_8h_pct": -0.012,
                "funding_interval_h": 4, "funding_rate_pct": -0.006, "squeeze_risk": True,
                "squeeze_reasons": ["oi_z>=2.0 (oi_z=2.40)", "funding_interval_unknown"],
                "funding_interval_unknown": True, "macro_short_check": "climax>=2.5x", "absorption_scored": False,
                "score_components": {"rsi": 30}, "score_schema_version": 2,
                "reasons": ["⚠️ Squeeze risk: oi_z → capped at Tier A", "RSI 81 overbought"]}
        screening = {"top_candidates": [dict(cand), dict(cand, symbol="BBBUSDT")],
                     "macro_rejected_shorts": [{"symbol": f"S{i}USDT", "direction": "SHORT",
                                                "reason": "alt_shorts_not_allowed"} for i in range(10)],
                     "funding_info_warning": "fundingInfo unavailable (URLError): funding read as 8h for every symbol",
                     "macro": {"btc_price": 60000.0, "allows_alt_shorts": False}}
        state = {"target_env": "prod",
                 "daily_loss_gate": {"blocked": True, "scope": "yolo", "reason": "DAILY LOSS GATE (YOLO): "
                                     "yolo_full_losses=1 >= yolo_max_daily_losses=1 today — no new YOLO entries "
                                     "until 00:00 UTC"},
                 "closed_today_summary": {"counted_by": "fills", "fills_closed": 12, "truncated": True,
                                          "trade_summary_error": "closing fills without an audit trade (not "
                                                                 "counted): XUSDT", "fills_error": "x" * 200},
                 "portfolio_exposure": {"delta_bias_incl_resting": "UNKNOWN"}}
        with patch.object(peb, "SYNC_FAILED_KEY", "_sync_failed_test"):
            brief = self.assemble(dict(state, _sync_failed_test=True), screening)
        self.assertEqual(len(brief["macro_rejected_shorts"]), 10)
        self.assertEqual(brief["pending_entries_status"], "UNREADABLE")
        self.assertEqual(brief["state_sync"], "FAILED")
        self.assertNotIn("score_schema_version", brief["filtered_opportunities"][0])  # sidecar only
        size = len(json.dumps(brief, ensure_ascii=False).encode("utf-8"))
        self.assertLess(size / 4, 1800, f"{size} bytes")


# =============================================================================
# 8. Evaluator prompt
# =============================================================================
class TestPrompt(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(AGENT_MD, encoding="utf-8") as f:
            cls.text = f.read()

    def test_rule_10_and_checklist_line(self):
        rules = self.text.split("<operational_rules>")[1].split("</operational_rules>")[0]
        rule10 = rules.split("- RULE 10")[1]
        self.assertLess(rules.index("- RULE 9"), rules.index("- RULE 10"))
        for fragment in ("daily_loss_gate.blocked", "scope: all", "REJECTED", "no approved candidates",
                         "summary starting `DAILY_LOSS_GATE:`",
                         "scope: yolo", "YOLO slot rejected", "standard candidates evaluated normally"):
            self.assertIn(fragment, rule10)
        items = self.text.split("<checklist_items>")[1].split("</checklist_items>")[0]
        c1 = items.split("C1 PORTFOLIO DELTA GATE:")[1].split("C2 MACRO")[0]
        self.assertIn("- C1.3 Daily loss gate (`daily_loss_gate`, RULE 10)", c1)
        self.assertIn("or C1.3 ACTIVE) / NEUTRAL", items)
        stop = next(l for l in self.text.splitlines() if l.startswith("Omit no check group"))
        self.assertIn("likewise after C1 when C1.3 is ACTIVE (`DAILY_LOSS_GATE:`)", stop)
        self.assertIn("<STALE_BRIEF|ENV_MISMATCH|DAILY_LOSS_GATE>", stop)
        # Round 4: evidence copied from the brief; every example but the daily-loss-gate one shows it NOT ACTIVE
        self.assertEqual(self.text.count("- [x] C1.3 Daily loss gate: blocked false -> NOT ACTIVE"),
                         self.text.count("<example id=") - 1)
        self.assertNotIn("C1.3 Daily loss gate: none", self.text)
        coherence = "C1.3 is NOT ACTIVE (scope `yolo`: non-YOLO only)"
        constraint9 = next(l for l in self.text.splitlines() if l.startswith("9. CHECKLIST CONSTRAINT"))
        agreement = next(l for l in self.text.splitlines() if l.startswith("- Every later section"))
        self.assertIn(coherence, constraint9)
        self.assertIn(coherence, agreement)

    def test_daily_loss_gate_few_shot(self):
        """Round 4: one compact negative few-shot; it passes the recorder's checklist check (PR #222)."""
        import re
        from utils import dossier_provenance as dp
        shot = re.search(r'<example id="eval_neg_08_daily_loss_gate_active">([\s\S]*?)</example>', self.text)
        self.assertIsNotNone(shot)
        body = shot.group(1)
        self.assertIn("<!-- EXAMPLE 13: NEGATIVE - DAILY LOSS GATE ACTIVE", self.text)
        self.assertIn("- [ ] C1.3 Daily loss gate: blocked true, scope all -> ACTIVE (scope all: REJECTED)", body)
        final = re.search(r"<final_response>([\s\S]*?)</final_response>", body).group(1)
        dossier = json.loads(re.search(r"<dossier_json>([\s\S]*?)</dossier_json>", final).group(1))
        self.assertEqual((dossier["status"], dossier["approved_candidates"]), ("REJECTED", []))
        self.assertTrue(dossier["summary"].startswith("DAILY_LOSS_GATE:"))
        self.assertEqual(dp.check_precondition_checklist(final, dossier), [])

    def test_rule_9_and_rule_1_notes(self):
        rule9 = self.text.split("- RULE 9")[1].split("- RULE 10")[0]
        self.assertIn("`SQZ` in the Markdown brief = `squeeze_risk: true`", rule9)
        self.assertIn("`LONG-CROWD` / `long_crowding_risk: true` is informational, never a gate", rule9)
        rule1 = self.text.split("- RULE 1 ")[1].split("- RULE 2")[0]
        self.assertIn("2.5x", rule1)


# =============================================================================
# 9. Doctor
# =============================================================================
class TestDoctor(unittest.TestCase):

    def test_counted_by_warning(self):
        warn = trading_doctor.ledger_counted_by_warning(
            {"target_env": "prod", "closed_today_summary": {"counted_by": "fills", "trade_summary_error": "boom"}},
            "prod")
        self.assertIn("counted_by=fills", warn)
        self.assertIn("boom", warn)
        for state in ({"target_env": "prod", "closed_today_summary": {"counted_by": "trades"}},
                      {"target_env": "prod", "closed_today_summary": {}},
                      {"target_env": "testnet", "closed_today_summary": {"counted_by": "fills"}}, None):
            self.assertIsNone(trading_doctor.ledger_counted_by_warning(state, "prod"), state)

    def test_gate_line(self):
        level, msg = trading_doctor.daily_loss_gate_line(
            {"target_env": "prod", "daily_loss_gate": {"blocked": True, "scope": "all", "reason": "R1"}}, "prod")
        self.assertEqual(level, "warn")
        self.assertIn("ACTIVE (all): R1", msg)
        level, msg = trading_doctor.daily_loss_gate_line(
            {"target_env": "prod", "daily_loss_gate": {"blocked": False, "scope": None, "reason": None,
                                                        "day_net_realized_usdt": -1.0, "day_loss_limit_usdt": 5.4,
                                                        "consecutive_full_sl": 1, "yolo_full_losses": 0}}, "prod")
        self.assertEqual(level, "ok")
        self.assertIn("inactive", msg)
        self.assertIn("consecutive_full_sl=1", msg)
        self.assertEqual(trading_doctor.daily_loss_gate_line({"target_env": "prod"}, "prod")[0], "warn")
        self.assertEqual(trading_doctor.daily_loss_gate_line(None, "prod")[0], "warn")
        self.assertEqual(trading_doctor.daily_loss_gate_line(None, "testnet")[0], "info")

    def test_calibration_store_warning(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "logs"))
        now = time.time()
        with patch.object(trading_doctor, "_read_session_state", return_value=None):
            self.assertIsNone(trading_doctor.calibration_store_warning(root, "prod", now))  # nothing closed yet
            with open(os.path.join(root, "logs", "trade_outcomes.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"env": "prod", "status": "closed"}) + "\n")
            warn = trading_doctor.calibration_store_warning(root, "prod", now)
            self.assertIn("missing while PROD closed trades exist", warn)
            self.assertIn("python3 scripts/trading_scorecard.py", warn)
            self.assertIsNone(trading_doctor.calibration_store_warning(root, "testnet", now))
            for age, stale in ((8 * 86400, True), (86400, False)):
                with open(scal.store_path(root), "w", encoding="utf-8") as f:
                    json.dump({"generated_at_ts": now - age, "env": "PROD", "buckets": {}}, f)
                warn = trading_doctor.calibration_store_warning(root, "prod", now)
                self.assertEqual(warn is not None and "stale" in warn, stale, warn)
        os.remove(os.path.join(root, "logs", "trade_outcomes.jsonl"))
        os.remove(scal.store_path(root))
        with patch.object(trading_doctor, "_read_session_state",
                          return_value={"target_env": "prod", "closed_today_summary": {"closed_trades_count": 2}}):
            self.assertIn("missing", trading_doctor.calibration_store_warning(root, "prod", now))


# =============================================================================
# 10. Calibration follow-ups
# =============================================================================
def crow(r, key, version=scal.SCORE_SCHEMA_VERSION, score=85):
    row = {"symbol": f"S{key}USDT", "direction": "LONG", "entry_ts": 1000 + key, "env": "prod", "status": "closed",
           "dossier_score": score, "score": score, "realized_r_net": r, "mfe_r": 1.0}
    if version is not None:
        row["score_schema_version"] = version
    return row


class TestCalibration(unittest.TestCase):

    def test_t_critical_values(self):
        self.assertEqual(scal.t95_critical(29), 1.699)
        self.assertEqual(scal.t95_critical(1), 6.314)
        self.assertEqual(scal.t95_critical(121), 1.645)
        self.assertEqual(scal.t95_critical(10_000), 1.645)
        self.assertAlmostEqual(scal.t95_critical(50), (1.684 + 1.671) / 2, places=6)  # interpolated
        sd, lcb = scal.lower_confidence_bound([0.0, 2.0])  # n 2: mean 1, sd sqrt(2), t(1) 6.314
        self.assertAlmostEqual(lcb, 1.0 - 6.314 * 2 ** 0.5 / 2 ** 0.5, places=3)  # -5.314

    def store_with_lcb(self, lcb, n=30):
        stats = {"n": n, "lcb95_r_net": lcb, "expectancy_r_net": 0.5, "calibrated": True}
        return {"generated_at_ts": time.time(), "env": "PROD", "score_schema_version": scal.SCORE_SCHEMA_VERSION,
                "buckets": {"80-89": stats}}

    def test_outdated_store_never_calibrates(self):
        """Round 2: a pre-#207 store (no score_schema_version, z-based bounds) or another version is uncalibrated."""
        for version in (None, 1, 3, "2"):
            with self.subTest(version=version):
                store = self.store_with_lcb(0.9)
                if version is None:
                    store.pop("score_schema_version")
                else:
                    store["score_schema_version"] = version
                self.assertEqual(scal.bucket_is_calibrated(store, 85, "PROD"), (False, scal.STORE_SCHEMA_OUTDATED))
                root = tempfile.mkdtemp()
                os.makedirs(os.path.join(root, "logs"))
                with open(scal.store_path(root), "w", encoding="utf-8") as f:
                    json.dump(store, f)
                self.assertEqual(scal.load_calibration_with_reason(root, require_current=True),
                                 (None, scal.STORE_SCHEMA_OUTDATED))
                self.assertIsNotNone(scal.load_calibration(root))  # the scorecard still reads (and keeps) its trades
                cand = {"symbol": "BTCUSDT", "direction": "LONG", "tier": "S", "score": 85}
                with patch.object(scal, "radar_snapshot_matches", return_value=(True, "ok")):
                    msg = scal.tier_s_confirmation_required(cand, "prod", {}, root)
                self.assertIn(scal.STORE_SCHEMA_OUTDATED, msg)
        self.assertTrue(scal.bucket_is_calibrated(self.store_with_lcb(0.9), 85, "PROD")[0])
        self.assertEqual(scal.merge_store(None, [], now=1)["score_schema_version"], scal.SCORE_SCHEMA_VERSION)

    def test_min_margin_boundary(self):
        self.assertFalse(scal.bucket_is_calibrated(self.store_with_lcb(0.1), 85, "PROD")[0])
        self.assertIn("<= 0.1R", scal.bucket_is_calibrated(self.store_with_lcb(0.1), 85, "PROD")[1])
        self.assertTrue(scal.bucket_is_calibrated(self.store_with_lcb(0.1001), 85, "PROD")[0])
        rows = [crow(0.1, i) for i in range(30)]  # sd 0: lcb = mean = 0.1 exactly
        self.assertFalse(scal.build_calibration(rows, "PROD", 30)["buckets"]["80-89"]["calibrated"])
        rows = [crow(0.1001, i) for i in range(30)]
        self.assertTrue(scal.build_calibration(rows, "PROD", 30)["buckets"]["80-89"]["calibrated"])
        self.assertFalse(scal.build_calibration(rows, "PROD", 30, min_lcb_r=0.2)["buckets"]["80-89"]["calibrated"])

    def test_profile_margin_rechecked_at_gate_time(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "logs"))
        with open(scal.store_path(root), "w", encoding="utf-8") as f:
            json.dump(self.store_with_lcb(0.2), f)  # stored "calibrated": True under the old margin
        cand = {"symbol": "BTCUSDT", "direction": "LONG", "tier": "S", "score": 85}
        with patch.object(scal, "radar_snapshot_matches", return_value=(True, "ok")):
            self.assertIsNone(scal.tier_s_confirmation_required(cand, "prod", {}, root))
            msg = scal.tier_s_confirmation_required(cand, "prod", {"tier_s_calibration_min_lcb_r": 0.3}, root)
        self.assertIn("<= 0.3R", msg)

    def test_schema_filter_excludes_and_counts_legacy_rows(self):
        rows = [crow(0.5, i) for i in range(30)] + [crow(-3.0, 100 + i, version=None) for i in range(5)] \
            + [crow(-3.0, 200, version=1)]
        cal = scal.build_calibration(rows, "PROD", 30)
        self.assertEqual((cal["buckets"]["80-89"]["n"], cal["excluded_schema"]), (30, 6))
        self.assertEqual(cal["score_schema_version"], scal.SCORE_SCHEMA_VERSION)
        store = scal.merge_store(None, rows, now=1)
        self.assertEqual(len(store["trades"]), 36)  # legacy rows stay in the store
        self.assertEqual((store["buckets"]["80-89"]["n"], store["excluded_schema"]), (30, 6))
        stored = store["trades"][scal.trade_key(rows[0])]
        self.assertEqual(stored["score_schema_version"], scal.SCORE_SCHEMA_VERSION)

    def test_schema_version_chain(self):
        self.assertEqual(scal.SCORE_SCHEMA_VERSION, 2)
        import screening_pipeline as sp
        self.assertIn("score_schema_version", sp.CandidateSetup.model_fields
                      if hasattr(sp.CandidateSetup, "model_fields") else sp.CandidateSetup.__fields__)
        # sidecar row keeps it (brief does not)
        self.assertIn("score_schema_version", peb._SIDECAR_ONLY_KEYS)
        self.assertNotIn("score_schema_version", peb._brief_opportunity({"symbol": "X", "score_schema_version": 2}))
        tmp = tempfile.mkdtemp()
        with patch.object(peb, "BRIEF_FILE", os.path.join(tmp, "primed_brief.json")):
            peb._write_scores_sidecar({"top_candidates": [{"symbol": "X", "direction": "LONG", "confidence": 85,
                                                           "score_schema_version": 2}]}, {}, 1, "prod")
            with open(peb.scores_sidecar_path(), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["rows"][0]["score_schema_version"], 2)
        # audit keys -> outcomes -> store
        self.assertIn("score_schema_version", eft.SCORE_AUDIT_KEYS)
        self.assertIn("score_schema_version", to.SCORE_FIELDS)
        self.assertEqual(to.score_fields({"score_schema_version": 2})["score_schema_version"], 2)
        self.assertIn("score_schema_version", scal._STORE_FIELDS)

    def test_radar_stamps_rows(self):
        from test_issue_206_squeeze_filter import _flat_klines
        with patch.object(bmr, "fetch_klines", return_value=_flat_klines(2.0)), \
             patch.object(bmr, "calculate_rsi", return_value=30), \
             patch.object(bmr, "calculate_ema", return_value=[99.0]), \
             patch.object(bmr.me, "candle_wick_pcts", return_value=(55, 0.0)):
            row = bmr.analyze_single_symbol("AAAUSDT")
        self.assertIsNotNone(row)
        self.assertEqual(row["score_schema_version"], scal.SCORE_SCHEMA_VERSION)


# =============================================================================
# 11. Squeeze fallback without the calibration module; SHORT funding penalty per 8h
# =============================================================================
class TestSqueezeFallback(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "logs", "evaluations"))

    def record(self, snaps, sha="abc"):
        with open(os.path.join(self.root, "logs", "evaluations", "latest_dossier.json"), "w", encoding="utf-8") as f:
            json.dump({"provenance": {"sha256": sha}, "radar_snapshots": snaps}, f)

    def both(self, cand):
        import utils
        # the executor's lazy `from utils import score_calibration` raises ImportError (module unavailable)
        with patch.dict(sys.modules, {"utils.score_calibration": None}), patch.dict(utils.__dict__):
            utils.__dict__.pop("score_calibration", None)
            ex = eft._tier_s_calibration_message(cand, "prod", self.root)
        with patch.object(pre_trade_guard, "scal", None):
            guard = pre_trade_guard._tier_s_calibration_message(cand, "prod", {}, self.root)
        self.assertEqual(ex, guard)
        return ex

    def test_same_message_in_both_gates(self):
        self.assertEqual(eft.SQUEEZE_FALLBACK_MESSAGE, pre_trade_guard.SQUEEZE_FALLBACK_MESSAGE)
        short = {"symbol": "ETHUSDT", "direction": "SHORT", "tier": "A+", "dossier_sha256": "abc"}
        long_ = dict(short, direction="LONG")
        self.record({"ETHUSDT|SHORT": {"radar_snapshot": {"squeeze_risk": True}},
                     "ETHUSDT|LONG": {"radar_snapshot": {"squeeze_risk": False}}})
        self.assertEqual(self.both(short), eft.SQUEEZE_FALLBACK_MESSAGE)
        self.assertIsNone(self.both(long_))
        self.record({"ETHUSDT|SHORT": {"radar_snapshot": {"squeeze_risk": False}}})
        self.assertIsNone(self.both(short))
        self.assertIsNone(self.both(long_))  # a LONG without a snapshot does not ask
        self.assertEqual(self.both(dict(short, dossier_sha256="other")), eft.SQUEEZE_FALLBACK_MESSAGE)  # unbound
        self.record({})
        self.assertEqual(self.both(short), eft.SQUEEZE_FALLBACK_MESSAGE)  # SHORT without a snapshot
        with open(os.path.join(self.root, "logs", "evaluations", "latest_dossier.json"), "w") as f:
            f.write("{broken")
        self.assertEqual(self.both(long_), eft.SQUEEZE_FALLBACK_MESSAGE)  # any read error asks
        with patch.object(pre_trade_guard, "scal", None):  # Tier S keeps its own (pre-#207) fallback text
            self.assertIn("Tier S score bucket not calibrated",
                          pre_trade_guard._tier_s_calibration_message(dict(long_, tier="S"), "prod", {}, self.root))

    def test_tier_s_fallback_text_identical(self):
        """Round 2: the Tier S module-unavailable text is one shared constant in both gates."""
        from utils import calibration_fallback as cf
        self.assertIs(eft.TIER_S_FALLBACK_MESSAGE, cf.TIER_S_FALLBACK_MESSAGE)
        self.assertIs(pre_trade_guard.TIER_S_FALLBACK_MESSAGE, cf.TIER_S_FALLBACK_MESSAGE)
        self.assertIs(pre_trade_guard.SQUEEZE_FALLBACK_MESSAGE, cf.SQUEEZE_FALLBACK_MESSAGE)
        self.record({})
        tier_s = {"symbol": "ETHUSDT", "direction": "LONG", "tier": "Tier S", "dossier_sha256": "abc"}
        self.assertEqual(self.both(tier_s), cf.TIER_S_FALLBACK_MESSAGE)

    def test_guard_header_for_the_fallback(self):
        self.assertTrue(pre_trade_guard.SQUEEZE_FALLBACK_MESSAGE.startswith("squeeze_risk SHORT"))


class TestShortFundingPenaltyPer8h(unittest.TestCase):

    def enrich(self, interval_h):
        snap = dict(tac.micro_snapshot("AAAUSDT"), funding_rate_pct=-0.008)
        cand = {"symbol": "AAAUSDT", "direction": "SHORT", "confidence": 70, "reasons": [], "interval": "15m",
                "wick_candle_open_time": 1, "tier_s_eligible": True, "score_components": {"rsi": 70}}
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            return bmr.enrich_candidate_microstructure(cand, {"AAAUSDT": interval_h})

    def test_four_hour_symbol_penalized_eight_hour_not(self):
        four = self.enrich(4)   # -0.016 %/8h < -0.015
        eight = self.enrich(8)  # -0.008 %/8h
        self.assertEqual(four["score_components"].get("funding"), -15)
        self.assertTrue(any("CROWDED: Negative funding (-0.0160%/8h)" in r for r in four["reasons"]))
        self.assertNotIn("funding", eight["score_components"])

    def enrich_long(self, interval_h, rate=0.02, normalize=True):
        snap = dict(tac.micro_snapshot("AAAUSDT"), funding_rate_pct=rate)
        cand = {"symbol": "AAAUSDT", "direction": "LONG", "confidence": 70, "reasons": [], "interval": "15m",
                "wick_candle_open_time": 1, "tier_s_eligible": True, "score_components": {"rsi": 70}}
        no_norm = patch.object(bmr.sqf, "normalize_funding_8h", return_value=None)
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap), \
             (no_norm if not normalize else contextlib.nullcontext()):
            return bmr.enrich_candidate_microstructure(cand, {"AAAUSDT": interval_h})

    def test_long_penalty_per_8h_too(self):
        """Round 2: +0.02% raw on a 4h symbol = +0.04%/8h > 0.035 -> penalty; on an 8h symbol it is not."""
        four, eight = self.enrich_long(4), self.enrich_long(8)
        self.assertEqual(four["score_components"].get("funding"), -15)
        self.assertTrue(any("Excessive positive funding (0.0400%/8h)" in r for r in four["reasons"]))
        self.assertNotIn("funding", eight["score_components"])
        raw = self.enrich_long(8, rate=0.04, normalize=False)  # normalization unavailable: the raw rate decides
        self.assertEqual(raw["score_components"].get("funding"), -15)

    def test_raw_rate_is_the_fallback(self):
        snap = dict(tac.micro_snapshot("AAAUSDT"), funding_rate_pct=-0.02)
        cand = {"symbol": "AAAUSDT", "direction": "SHORT", "confidence": 70, "reasons": [], "interval": "15m",
                "wick_candle_open_time": 1, "tier_s_eligible": True, "score_components": {"rsi": 70}}
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap), \
             patch.object(bmr.sqf, "normalize_funding_8h", return_value=None):
            row = bmr.enrich_candidate_microstructure(cand, None)
        self.assertEqual(row["score_components"].get("funding"), -15)

    def test_funding_interval_unknown_only_for_negative_short_rates(self):
        snap = dict(tac.micro_snapshot("AAAUSDT"), funding_rate_pct=0.004)
        cand = {"symbol": "AAAUSDT", "direction": "SHORT", "confidence": 70, "reasons": [], "interval": "15m",
                "wick_candle_open_time": 1, "tier_s_eligible": True, "score_components": {"rsi": 70}}
        with patch("microstructure_engine.get_symbol_microstructure", return_value=snap):
            row = bmr.enrich_candidate_microstructure(dict(cand), {}, funding_interval_unknown=True)
        self.assertIs(row["funding_interval_unknown"], True)
        self.assertFalse(row["squeeze_risk"])
        long_cand = dict(cand, direction="LONG")
        with patch("microstructure_engine.get_symbol_microstructure",
                   return_value=dict(snap, funding_rate_pct=-0.004)):
            row = bmr.enrich_candidate_microstructure(long_cand, {}, funding_interval_unknown=True)
        self.assertNotIn("funding_interval_unknown", row)


if __name__ == "__main__":
    unittest.main()
