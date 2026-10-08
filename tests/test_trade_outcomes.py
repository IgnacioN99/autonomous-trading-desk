#!/usr/bin/env python3
"""
test_trade_outcomes.py - scripts/trade_outcomes.py (issue #182): per-trade exit legs, weighted realized R (gross and
net), MFE / giveback from 1m klines, the fills_unavailable path and exit codes, 7-day userTrades windows with
pagination, read-only requests (only GET /fapi/v1/userTrades) and the atomic output rewrite.
Hermetic: send_signed_request and the klines fetch are fakes, urlopen is blocked, temp workspace.
"""

import io
import os
import sys
import json
import time
import random
import tempfile
import unittest
import contextlib
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import trade_outcomes as to

USER_TRADES = "/fapi/v1/userTrades"
DAY_MS = 24 * 3600 * 1000
MIN = 60_000
NOW_S = int(time.time())
T0 = (NOW_S - 2 * 86400) * 1000  # entry fills two days ago (ms)


def fill(fid, order_id, side, price, qty, t_ms, pnl=0.0, comm=0.0, asset="USDT", symbol="BTCUSDT"):
    return {"id": fid, "orderId": order_id, "symbol": symbol, "side": side, "price": str(price), "qty": str(qty),
            "quoteQty": str(price * qty), "realizedPnl": str(pnl), "commission": str(comm), "commissionAsset": asset,
            "positionSide": "BOTH", "maker": False, "time": int(t_ms)}


class FakeFills:
    """send_signed_request fake: GET /fapi/v1/userTrades from per-symbol fill lists (or an error payload); any other
    request fails the test."""

    def __init__(self, by_symbol, order="asc"):
        self.by_symbol = by_symbol
        self.order = order  # which `limit` rows a full window returns: "asc" oldest, "desc" newest, "shuffle" any
        self.calls = []

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        params = dict(params or {})
        self.calls.append((method, endpoint, params))
        if method != "GET" or endpoint != USER_TRADES:
            raise AssertionError(f"unexpected request {method} {endpoint}")
        src = self.by_symbol.get(params["symbol"], [])
        if not isinstance(src, list):
            return src
        rows = [dict(f) for f in src if params["startTime"] <= f["time"] <= params["endTime"]]
        rows.sort(key=lambda f: (f["time"], f["id"]), reverse=self.order == "desc")
        if self.order == "shuffle":
            random.Random(len(self.calls)).shuffle(rows)
        return rows[:params["limit"]]


class KlinesFake:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def __call__(self, symbol, interval, start_ms, limit, target_env, timeout=None):
        self.calls.append((symbol, interval, start_ms, limit, target_env))
        self.timeouts = getattr(self, "timeouts", []) + [timeout]
        out = []
        o = int(start_ms)
        while len(out) < limit and o < NOW_S * 1000 - MIN:
            h, l = self.path(symbol, o)
            out.append([o, str(l), str(h), str(l), str(h), "1", o + MIN - 1])
            o += MIN
        return out


class OutcomesBase(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.logs = os.path.join(self.ws, "logs")
        os.makedirs(self.logs)
        for p in (patch("execute_futures_trade._workspace_dir", return_value=self.ws),
                  patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test"))):
            p.start()
            self.addCleanup(p.stop)

    def audit(self, **rec):
        base = dict(symbol="BTCUSDT", direction="LONG", entry_price=100.0, sl_price=95.0, total_qty=10.0,
                    target_env="prod", timestamp=T0 // 1000 + 10, is_yolo=False)
        base.update(rec)
        with open(os.path.join(self.logs, "trades_audit.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(base) + "\n")

    def guardian_action(self, **rec):
        with open(os.path.join(self.logs, "guardian_actions.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def run_cli(self, fake, argv, klines=None):
        out = io.StringIO()
        klines = klines or KlinesFake(lambda s, o: (100.0, 100.0))
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("utils.trade_excursion.fetch_klines_range", side_effect=klines), \
             contextlib.redirect_stdout(out):
            code = to.main(["--env", "prod"] + argv)
        return code, out.getvalue()

    def rows(self, path=None):
        with open(path or os.path.join(self.logs, "trade_outcomes.jsonl"), "r", encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]


class TestLegsAndR(OutcomesBase):

    def test_ondo_like_short_tp1_then_manual_gives_2_5r(self):
        # SHORT entry 1.0, SL 1.05 (risk 0.05): TP1 30% at 1.8R (0.91), the rest closed manually at 2.8R (0.86).
        self.audit(symbol="ONDOUSDT", direction="SHORT", entry_price=1.0, sl_price=1.05, total_qty=1000.0,
                   entry_order_id=11, tp1_order_id=12, tp2_order_id=13, tp1_price=0.91, tp2_price=0.8)
        fills = [fill(1, 11, "SELL", 1.0, 1000, T0, comm=0.3, symbol="ONDOUSDT"),
                 fill(2, 12, "BUY", 0.91, 300, T0 + 3600_000, pnl=27.0, comm=0.1, symbol="ONDOUSDT"),
                 fill(3, 99, "BUY", 0.86, 700, T0 + 7200_000, pnl=98.0, comm=0.2, symbol="ONDOUSDT")]
        code, out = self.run_cli(FakeFills({"ONDOUSDT": fills}), ["--json", "--no-klines"])
        self.assertEqual(code, 0)
        t = self.rows()[0]
        self.assertEqual(t["status"], "closed")
        self.assertEqual([l["reason"] for l in t["legs"]], ["TP1", "MANUAL_OR_OTHER"])
        self.assertAlmostEqual(t["realized_r_gross"], 2.5, places=4)
        self.assertAlmostEqual(t["realized_r_net"], (27.0 + 98.0 - 0.6) / 50.0, places=4)
        self.assertTrue(t["entry_commission_included"])
        self.assertEqual((t["exit_reason"], t["tp1_filled"]), ("MANUAL_OR_OTHER", True))
        self.assertEqual((t["entry_ts"], t["exit_ts"]), (T0, T0 + 7200_000))
        self.assertNotIn("mfe_r", t)
        summary = json.loads(out)
        self.assertEqual((summary["ok"], summary["trades"], summary["closed"]), (True, 1, 1))
        self.assertEqual(summary["summary"]["by_exit_reason"], {"MANUAL_OR_OTHER": 1})

    def test_reason_labels_and_summary(self):
        # BTC: TP1 + TP2. ETH: SL. SOL: TP1 then break-even. XRP: trailed stop. ADA: manual. DOT: still open.
        self.audit(symbol="BTCUSDT", entry_order_id=1, tp1_order_id=2, tp2_order_id=3, tp1_price=109.0, tp2_price=120.0)
        self.audit(symbol="ETHUSDT", entry_order_id=10)
        self.audit(symbol="SOLUSDT", entry_order_id=20, tp1_order_id=21, tp2_order_id=22)
        self.audit(symbol="XRPUSDT", entry_order_id=30)
        self.audit(symbol="ADAUSDT", entry_order_id=40)
        self.audit(symbol="DOTUSDT", entry_order_id=50, tp1_order_id=51)
        h = 3600_000
        self.guardian_action(timestamp=(T0 + h) // 1000, env="prod", symbol="XRPUSDT", type="trail_stop",
                             dry_run=False, success=True, detail={"new_sl": 103.0})
        self.guardian_action(timestamp=(T0 + h) // 1000, env="prod", symbol="ADAUSDT", type="trail_stop",
                             dry_run=True, success=False, detail={"new_sl": 104.0, "planned": True})
        fills = {
            "BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 2, "SELL", 109, 3, T0 + h), fill(3, 3, "SELL", 120, 7, T0 + 2 * h)],
            "ETHUSDT": [fill(1, 10, "BUY", 100, 10, T0), fill(2, 77, "SELL", 94.8, 10, T0 + h)],
            "SOLUSDT": [fill(1, 20, "BUY", 100, 10, T0), fill(2, 21, "SELL", 109, 3, T0 + h),
                        fill(3, 78, "SELL", 100.15, 7, T0 + 2 * h)],
            "XRPUSDT": [fill(1, 30, "BUY", 100, 10, T0), fill(2, 79, "SELL", 102.9, 10, T0 + 2 * h)],
            "ADAUSDT": [fill(1, 40, "BUY", 100, 10, T0), fill(2, 80, "SELL", 104.0, 10, T0 + 2 * h)],
            "DOTUSDT": [fill(1, 50, "BUY", 100, 10, T0), fill(2, 51, "SELL", 109, 3, T0 + h)],
        }
        code, out = self.run_cli(FakeFills(fills), ["--json", "--no-klines"])
        self.assertEqual(code, 0)
        by = {t["symbol"]: t for t in self.rows()}
        self.assertEqual([l["reason"] for l in by["BTCUSDT"]["legs"]], ["TP1", "TP2"])
        self.assertAlmostEqual(by["BTCUSDT"]["realized_r_gross"], (3 * 9 + 7 * 20) / 50.0, places=4)
        self.assertEqual(by["ETHUSDT"]["exit_reason"], "SL")
        self.assertAlmostEqual(by["ETHUSDT"]["realized_r_gross"], -1.04, places=4)
        self.assertEqual([l["reason"] for l in by["SOLUSDT"]["legs"]], ["TP1", "BREAKEVEN"])
        self.assertEqual(by["XRPUSDT"]["exit_reason"], "TRAILED_STOP")
        self.assertEqual(by["ADAUSDT"]["exit_reason"], "MANUAL_OR_OTHER")  # dry-run trail actions are ignored
        self.assertEqual(by["DOTUSDT"]["status"], "open")
        self.assertIsNone(by["DOTUSDT"]["exit_reason"])
        s = json.loads(out)
        self.assertEqual((s["trades"], s["closed"]), (6, 5))
        self.assertEqual(s["summary"]["by_exit_reason"],
                         {"TP2": 1, "SL": 1, "BREAKEVEN": 1, "TRAILED_STOP": 1, "MANUAL_OR_OTHER": 1})
        self.assertEqual((s["summary"]["tp1_then_breakeven"], s["summary"]["tp2_hits"], s["summary"]["full_sl"]),
                         (1, 1, 1))

    def test_fill_split_between_consecutive_trades_and_no_entry_order_id(self):
        # Two LONG entries; the second starts before the first is fully closed: the first stays open.
        self.audit(timestamp=T0 // 1000 + 60, total_qty=10.0)
        self.audit(timestamp=(T0 + 3 * 3600_000) // 1000 + 60, total_qty=10.0)
        fills = {"BTCUSDT": [fill(1, 5, "BUY", 100, 10, T0), fill(2, 6, "SELL", 101, 4, T0 + 3600_000),
                             fill(3, 7, "BUY", 100, 10, T0 + 3 * 3600_000),
                             fill(4, 8, "SELL", 102, 10, T0 + 4 * 3600_000)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        first, second = self.rows()
        self.assertEqual((first["status"], len(first["legs"])), ("open", 1))
        self.assertEqual(second["status"], "closed")
        self.assertFalse(second["entry_commission_included"])

    def test_partly_filled_entry_closes_on_filled_qty(self):
        # Planned 10, only 6 filled (LIMIT partly filled then cancelled), 6 closed: closed, R on 6.
        self.audit(total_qty=10.0, entry_order_id=1)
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 6, T0, comm=0.0),
                             fill(2, 2, "SELL", 110, 6, T0 + 3600_000, pnl=60.0, comm=0.0)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        t = self.rows()[0]
        self.assertEqual((t["status"], t["filled_qty"], t["total_qty"]), ("closed", 6.0, 10.0))
        self.assertAlmostEqual(t["realized_r_gross"], 2.0, places=4)
        self.assertAlmostEqual(t["realized_r_net"], 60.0 / (5.0 * 6), places=4)
        self.assertEqual((t["exit_ts"], t["exit_reason"]), (T0 + 3600_000, "MANUAL_OR_OTHER"))

    def test_opposite_entry_fill_is_not_an_exit_leg(self):
        # One-way mode: a partly filled SHORT (planned 10, filled 4, 2 closed: still open), then a LONG entry whose
        # BUY fill carries its entry_order_id: that BUY must not become a leg of the SHORT.
        self.audit(direction="SHORT", sl_price=105.0, total_qty=10.0, entry_order_id=1)
        self.audit(direction="LONG", total_qty=5.0, entry_order_id=7, timestamp=(T0 + 2 * 3600_000) // 1000 + 10)
        fills = {"BTCUSDT": [fill(1, 1, "SELL", 100, 4, T0), fill(2, 2, "BUY", 99, 2, T0 + 3600_000),
                             fill(3, 7, "BUY", 100, 5, T0 + 2 * 3600_000),
                             fill(4, 8, "SELL", 104, 5, T0 + 3 * 3600_000)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        by = {t["direction"]: t for t in self.rows()}
        short = by["SHORT"]
        self.assertEqual(short["status"], "open")
        self.assertEqual([l["order_id"] for l in short["legs"]], [2])
        self.assertEqual(short["filled_qty"], 4.0)
        self.assertEqual(by["LONG"]["status"], "closed")

    def test_non_usdt_commission_gives_null_net(self):
        self.audit(entry_order_id=1)
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0, comm=0.001, asset="BNB"),
                             fill(2, 2, "SELL", 110, 10, T0 + 3600_000, pnl=100.0, comm=0.1)]}
        self.run_cli(FakeFills(fills), ["--no-klines"])
        t = self.rows()[0]
        self.assertAlmostEqual(t["realized_r_gross"], 2.0, places=4)
        self.assertIsNone(t["realized_r_net"])


class TestKlineExcursion(OutcomesBase):

    def test_mfe_and_giveback_from_klines(self):
        self.audit(entry_order_id=1, tp1_order_id=2)
        h = 3600_000
        peak_bar = (T0 // MIN) * MIN + 30 * MIN
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 2, "SELL", 109, 3, T0 + h),
                             fill(3, 9, "SELL", 100.1, 7, T0 + 2 * h)]}
        klines = KlinesFake(lambda s, o: (115.0, 99.0) if o == peak_bar else (101.0, 98.0))
        code, out = self.run_cli(FakeFills(fills), ["--json"], klines=klines)
        self.assertEqual(code, 0)
        t = self.rows()[0]
        self.assertEqual(t["mfe_r"], 3.0)
        self.assertEqual(t["mae_r"], -0.4)
        self.assertEqual(t["mfe_ts"], peak_bar)
        self.assertAlmostEqual(t["giveback_r"], 3.0 - t["realized_r_gross"], places=4)
        self.assertEqual(klines.calls[0][1:4], ("1m", (T0 // MIN) * MIN + MIN, 1500))
        self.assertEqual(set(klines.timeouts), {to.KLINES_TIMEOUT_SECONDS})  # offline CLI: the longer timeout
        s = json.loads(out)["summary"]
        self.assertAlmostEqual(s["capture_ratio"], round(t["realized_r_gross"] / 3.0, 4), places=4)
        self.assertAlmostEqual(s["mean_giveback_r"], t["giveback_r"], places=4)

    def test_klines_failure_is_recorded_per_trade(self):
        self.audit(entry_order_id=1)
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 2, "SELL", 110, 10, T0 + 3600_000)]}

        def boom(*a, **k):
            raise OSError("klines down")

        code, _ = self.run_cli(FakeFills(fills), [], klines=boom)
        self.assertEqual(code, 0)
        t = self.rows()[0]
        self.assertIsNone(t["mfe_r"])
        self.assertIn("klines down", t["klines_error"])


class TestFillsUnavailableAndRequests(OutcomesBase):

    def test_mcp_error_payload_marks_fills_unavailable_and_exit_1(self):
        self.audit(symbol="BTCUSDT")
        fake = FakeFills({"BTCUSDT": {"error": "Public fallback error: HTTP Error 401"}})
        code, out = self.run_cli(fake, ["--json", "--no-klines"])
        self.assertEqual(code, 1)
        t = self.rows()[0]
        self.assertEqual(t["status"], "fills_unavailable")
        self.assertIn("401", t["error"])
        self.assertFalse(json.loads(out)["ok"])

    def test_one_readable_symbol_gives_exit_0(self):
        self.audit(symbol="BTCUSDT", entry_order_id=1)
        self.audit(symbol="ETHUSDT")
        fake = FakeFills({"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 2, "SELL", 110, 10, T0 + 3600_000)],
                          "ETHUSDT": {"code": -2015, "msg": "Invalid API-key"}})
        code, _ = self.run_cli(fake, ["--no-klines"])
        self.assertEqual(code, 0)
        self.assertEqual({t["symbol"]: t["status"] for t in self.rows()},
                         {"BTCUSDT": "closed", "ETHUSDT": "fills_unavailable"})

    def test_no_trades_in_range_is_ok(self):
        code, out = self.run_cli(FakeFills({}), ["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["trades"], 0)
        self.assertEqual(self.rows(), [])

    def test_only_get_user_trades_and_seven_day_windows_with_pagination(self):
        entry_s = NOW_S - 10 * 86400
        self.audit(symbol="BTCUSDT", timestamp=entry_s, total_qty=2000.0, entry_order_id=1)
        self.audit(symbol="ETHUSDT", timestamp=entry_s, target_env="testnet")  # other env: ignored
        t_entry = entry_s * 1000 - 30_000
        # 1500 closing fills in the first window: the window must be paginated.
        fills = [fill(1, 1, "BUY", 100, 2000, t_entry)]
        fills += [fill(10 + i, 500 + i, "SELL", 101, 1, t_entry + 1000 * (i + 1)) for i in range(1500)]
        fills.append(fill(9999, 7777, "SELL", 102, 500, (NOW_S - 86400) * 1000))
        fake = FakeFills({"BTCUSDT": fills})
        since = time.strftime("%Y-%m-%d", time.gmtime(entry_s - 86400))
        code, _ = self.run_cli(fake, ["--since", since, "--no-klines"])
        self.assertEqual(code, 0)
        self.assertTrue(all(c[0] == "GET" and c[1] == USER_TRADES for c in fake.calls))
        self.assertEqual({c[2]["symbol"] for c in fake.calls}, {"BTCUSDT"})
        for _, _, p in fake.calls:
            self.assertLessEqual(p["endTime"] - p["startTime"], 7 * DAY_MS)
            self.assertEqual(p["limit"], 1000)
        starts = sorted({p["startTime"] for _, _, p in fake.calls})
        self.assertEqual(starts[0], entry_s * 1000 - 5 * MIN)
        self.assertGreaterEqual(len(fake.calls), 3)  # two windows, the first one paginated
        t = self.rows()[0]
        self.assertEqual(t["status"], "closed")
        self.assertAlmostEqual(sum(l["qty"] for l in t["legs"]), 2000.0)
        self.assertEqual(len(t["legs"]), 1501)

    def test_full_windows_collected_whatever_the_reply_order(self):
        # > 1000 fills in one window, several of them sharing a millisecond; a full reply may hold the newest rows
        # or any subset: every fill id must still be collected.
        t_entry = T0 - 30_000
        fills = [fill(1, 1, "BUY", 100, 2500, t_entry)]
        fills += [fill(10 + i, 500 + i, "SELL", 101, 1, t_entry + 1000 + 10 * (i // 3)) for i in range(2500)]
        for order in ("desc", "shuffle"):
            with self.subTest(order=order):
                with open(os.path.join(self.logs, "trades_audit.jsonl"), "w", encoding="utf-8"):
                    pass
                self.audit(total_qty=2500.0, entry_order_id=1)
                fake = FakeFills({"BTCUSDT": fills}, order=order)
                with patch("execute_futures_trade.send_signed_request", side_effect=fake):
                    got, err = to.fetch_fills("BTCUSDT", t_entry - 5 * MIN, NOW_S * 1000, "prod")
                self.assertIsNone(err)
                self.assertEqual({f["id"] for f in got}, {f["id"] for f in fills})
                self.assertEqual([f["time"] for f in got], sorted(f["time"] for f in got))
                code, _ = self.run_cli(FakeFills({"BTCUSDT": fills}, order=order), ["--no-klines"])
                self.assertEqual(code, 0)
                t = self.rows()[0]
                self.assertEqual(t["status"], "closed")
                self.assertEqual(len(t["legs"]), 2500)

    def test_output_rewritten_atomically(self):
        out_path = os.path.join(self.ws, "out", "trade_outcomes.jsonl")
        self.audit(symbol="BTCUSDT", entry_order_id=1)
        self.audit(symbol="ETHUSDT", entry_order_id=1)
        fills = {s: [fill(1, 1, "BUY", 100, 10, T0, symbol=s), fill(2, 2, "SELL", 110, 10, T0 + 3600_000, symbol=s)]
                 for s in ("BTCUSDT", "ETHUSDT")}
        self.run_cli(FakeFills(fills), ["--no-klines", "--output", out_path])
        self.assertEqual(len(self.rows(out_path)), 2)
        self.run_cli(FakeFills(fills), ["--no-klines", "--output", out_path, "--symbol", "ethusdt"])
        rows = self.rows(out_path)
        self.assertEqual([r["symbol"] for r in rows], ["ETHUSDT"])
        self.assertEqual(os.listdir(os.path.dirname(out_path)), ["trade_outcomes.jsonl"])

    def test_human_output_mentions_counts(self):
        self.audit(symbol="BTCUSDT", entry_order_id=1)
        fills = {"BTCUSDT": [fill(1, 1, "BUY", 100, 10, T0), fill(2, 2, "SELL", 110, 10, T0 + 3600_000)]}
        code, out = self.run_cli(FakeFills(fills), ["--no-klines"])
        self.assertEqual(code, 0)
        self.assertIn("1 trade(s), 1 closed", out)


if __name__ == "__main__":
    unittest.main()
