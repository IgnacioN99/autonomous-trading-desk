#!/usr/bin/env python3
"""
test_exit_policy_sim.py - scripts/exit_policy_sim.py (issue #182 item 3): offline exit-policy replay on closed trade
outcomes, and the klines_15m / filters injection into dynamic_exit_manager.calculate_structural_stop.

- dem: injected klines_15m / filters give the same result as the patched reads (LONG, SHORT) and skip both reads.
- Replay: SL only, TP1 then True Net BE (fixed_targets), TP2 hit, trail exit via the profit lock, same-bar SL + TP
  (worst case = SL), YOLO not trailed before TP1, horizon cap marks to the last close.
- Policies: close_at_0_5r, tp2_2_5r, split_50_50. Metrics on a hand-built set; skipped counts per reason.
- CLI: --json shape, exit codes 0 / 1 / 2, output file, no signed request, unsigned exchangeInfo read.
Hermetic: send_signed_request and urlopen blocked, trade_excursion.fetch_klines_range faked, exchangeInfo stubbed,
temp workspace.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(BASE_DIR, "scripts"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import dynamic_exit_manager as dem
import exit_policy_sim as eps
from test_issue_95_trailing_activation import make_klines, flat_pre, market
from test_issue_106_exit_manager_hardening import TICK_FILTERS

MIN = 60_000
M15 = 15 * MIN
NOW_MS = int(time.time() * 1000)
T0 = (NOW_MS - 3 * 86400 * 1000) // M15 * M15  # 15m-aligned candle containing the entry (three days ago)
ENTRY_MS = T0 + 5 * MIN + 30_000  # first full 1m bar after entry opens at T0 + 6 min
TAKER, MAKER = eps.DEFAULT_TAKER_FEE, eps.DEFAULT_MAKER_FEE
FLAT = (100.3, 99.8, 100.0)

EXCHANGE_INFO = {"symbols": [{"symbol": s, "filters": [
    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
    {"filterType": "MIN_NOTIONAL", "notional": "5"}]} for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")]}


class Market:
    """1m bars (pre-entry minutes of the T0 candle flat, then `path` from T0 + 6 min) and 15m bars (`warmup` zig-zag
    bars with range 1.0 before T0, then the 1m bars aggregated), served like fetch_klines_range."""

    def __init__(self, path, warmup=40, center=100.0):
        self.k1m = []
        for i in range(6):
            o = T0 + i * MIN
            self.k1m.append([o, "100.0", "100.2", "99.8", "100.0", "1", o + MIN - 1])
        for i, (h, l, c) in enumerate(path):
            o = T0 + (6 + i) * MIN
            self.k1m.append([o, str(c), str(h), str(l), str(c), "1", o + MIN - 1])
        self.k15m = []
        for i, (h, l, c) in enumerate(flat_pre(warmup, center)):
            o = T0 - (warmup - i) * M15
            self.k15m.append([o, str(c), str(h), str(l), str(c), "1", o + M15 - 1])
        groups = {}
        for k in self.k1m:
            groups.setdefault(k[0] // M15 * M15, []).append(k)
        for o in sorted(groups):
            g = groups[o]
            self.k15m.append([o, g[0][1], str(max(float(k[2]) for k in g)), str(min(float(k[3]) for k in g)),
                              g[-1][4], "1", o + M15 - 1])

    def __call__(self, symbol, interval, start_ms, limit, target_env, timeout=None):
        src = self.k1m if interval == "1m" else self.k15m
        return [list(k) for k in src if k[0] >= start_ms][:limit]


class MultiMarket:
    """Per-symbol Market; a symbol mapped to an exception raises it."""

    def __init__(self, by_symbol):
        self.by_symbol = by_symbol
        self.calls = []

    def __call__(self, symbol, interval, start_ms, limit, target_env, timeout=None):
        self.calls.append((symbol, interval, start_ms, limit, target_env, timeout))
        src = self.by_symbol[symbol]
        if isinstance(src, Exception):
            raise src
        return src(symbol, interval, start_ms, limit, target_env, timeout)


def outcome(**over):
    row = {"symbol": "BTCUSDT", "direction": "LONG", "status": "closed", "env": "prod", "since": "2026-10-01",
           "entry_ts": ENTRY_MS, "entry_price": 100.0, "sl_price": 99.0, "initial_risk": 1.0,
           "tp1_price": 101.0, "tp2_price": 105.0, "is_yolo": False, "realized_r_net": -1.1, "legs": []}
    row.update(over)
    return row


def fees_r(entry, exits, risk=1.0):
    """exits: [(frac, price, rate)]; entry taker on the whole size."""
    return (TAKER * entry + sum(f * rate * p for f, p, rate in exits)) / risk


class SimBase(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.logs = os.path.join(self.ws, "logs")
        os.makedirs(self.logs)
        self.signed = patch("execute_futures_trade.send_signed_request",
                            side_effect=AssertionError("signed request in offline simulator"))
        for p in (patch("execute_futures_trade._workspace_dir", return_value=self.ws),
                  patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test")),
                  patch("exit_policy_sim.fetch_exchange_info", return_value=EXCHANGE_INFO)):
            p.start()
            self.addCleanup(p.stop)
        self.signed_mock = self.signed.start()
        self.addCleanup(self.signed.stop)

    def tearDown(self):
        self.assertEqual(self.signed_mock.call_count, 0)

    def sim(self, rows, market, policies=("current",), horizon_hours=48.0, trail_cadence="1m"):
        fake = market if isinstance(market, MultiMarket) else MultiMarket({"BTCUSDT": market})
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=fake):
            return eps.simulate(rows, "prod", list(policies), horizon_hours, TAKER, MAKER, now_ms=NOW_MS,
                                trail_cadence=trail_cadence)

    def one(self, row, path, policy, **kw):
        res = self.sim([row], Market(path), policies=(policy,), **kw)
        self.assertEqual(res["n_trades"], 1, res)
        return res["policies"][policy]["trades"][0], res


# Profit-lock path (entry 100, R = 1): the T0 candle stays flat, the next 15m candle reaches 102.5 (MFE 2.5R, above
# 2x ATR) and closes 102.2, so the 15m-close trail call locks +1R (101.0); the first bar after it drops to 100.5.
LOCK_PATH = [FLAT] * 9 + [(101.0, 100.0, 100.9), (102.5, 100.9, 102.2)] + [(102.4, 102.0, 102.2)] * 13


class TestDemInjection(unittest.TestCase):

    def _both(self, direction, post, forming, planned, current, tp1):
        klines, entry_ts = make_klines(flat_pre(), post, forming, time.time())
        kw = dict(current_sl_price=current, target_env="prod", planned_sl=planned, entry_ts=entry_ts,
                  tp1_filled=tp1, mark_price=float(post[-1][2]), reference_source="trade_audit",
                  exit_management={"profit_lock_enabled": True, "profit_lock_steps": [{"mfe_r": 1.0, "lock_r": 0.0}],
                                   "extend_last_step": True, "lock_on_tp1": True})
        with patch("execute_futures_trade.get_symbol_filters", return_value=dict(TICK_FILTERS)), market(klines):
            read = dem.calculate_structural_stop("BTCUSDT", direction, 100.0, **kw)
        with patch("execute_futures_trade.get_symbol_filters", side_effect=AssertionError("filters read")), \
             patch("dynamic_exit_manager.get_klines_data", side_effect=AssertionError("klines read")):
            injected = dem.calculate_structural_stop("BTCUSDT", direction, 100.0, klines_15m=klines,
                                                     filters=dict(TICK_FILTERS), **kw)
        self.assertEqual(read["reason"], "activated", read)
        self.assertEqual(injected, read)

    def test_long_injected_equals_read(self):
        self._both("LONG", [(101.5, 100.5, 101.2), (102.6, 101.2, 102.3)], (102.4, 102.0, 102.2), 99.0, 99.0, True)

    def test_short_injected_equals_read(self):
        self._both("SHORT", [(99.5, 98.5, 98.8), (98.8, 97.4, 97.7)], (98.0, 97.6, 97.8), 101.0, 101.0, False)


class TestReplay(SimBase):

    def test_sl_only_is_minus_one_r_minus_fees(self):
        t, _ = self.one(outcome(), [FLAT] * 3 + [(100.0, 98.9, 99.0)], "current")
        self.assertEqual(t["exit_kind"], "sl")
        self.assertAlmostEqual(t["r"], -1.0 - fees_r(100.0, [(1.0, 99.0, TAKER)]), places=6)

    def test_short_sl_only(self):
        row = outcome(direction="SHORT", sl_price=101.0, tp1_price=99.0, tp2_price=95.0)
        t, _ = self.one(row, [FLAT] * 3 + [(101.1, 100.0, 101.0)], "current")
        self.assertEqual(t["exit_kind"], "sl")
        self.assertAlmostEqual(t["r"], -1.0 - fees_r(100.0, [(1.0, 101.0, TAKER)]), places=6)

    def test_fixed_targets_tp1_then_true_net_be(self):
        t, res = self.one(outcome(), [(101.2, 100.5, 101.0), (100.5, 100.1, 100.2)], "fixed_targets")
        self.assertEqual(t["exit_kind"], "be")
        expected = 0.3 * 1.0 + 0.7 * 0.2 - fees_r(100.0, [(0.3, 101.0, MAKER), (0.7, 100.2, TAKER)])
        self.assertAlmostEqual(t["r"], expected, places=6)

    def test_tp2_hit_with_trail(self):
        t, _ = self.one(outcome(), [(101.2, 100.8, 101.1), (105.5, 101.0, 105.2)], "current")
        self.assertEqual(t["exit_kind"], "tp2")
        expected = 0.3 * 1.0 + 0.7 * 5.0 - fees_r(100.0, [(0.3, 101.0, MAKER), (0.7, 105.0, MAKER)])
        self.assertAlmostEqual(t["r"], expected, places=6)

    def test_trail_exit_via_profit_lock(self):
        path = LOCK_PATH + [(102.2, 100.5, 100.6)]
        row = outcome(tp1_price=104.0, tp2_price=110.0)
        res = self.sim([row], Market(path), policies=("current", "current_no_lock"))
        lock = res["policies"]["current"]["trades"][0]
        self.assertEqual(lock["exit_kind"], "trail_stop")
        self.assertAlmostEqual(lock["r"], 1.0 - fees_r(100.0, [(1.0, 101.0, TAKER)]), places=6)
        no_lock = res["policies"]["current_no_lock"]["trades"][0]
        self.assertEqual(no_lock["exit_kind"], "trail_stop")
        self.assertLess(no_lock["r"], lock["r"])

    def test_tp1_minute_trail_uses_intrabar_lock(self):
        # TP1 fills in bar 1 (high 102.3): the same-minute trail call sees the intrabar extreme (MFE 2.3R) and locks
        # +1R (101.0); bar 2 (low 100.9) stops the rest there. Without the lock the stop sits at True Net BE 100.2.
        path = [(102.3, 100.5, 101.6), (101.5, 100.9, 101.0)]
        res = self.sim([outcome()], Market(path), policies=("current", "current_no_lock"))
        t = res["policies"]["current"]["trades"][0]
        self.assertEqual(t["exit_kind"], "tp1+trail")
        expected = 0.3 * 1.0 + 0.7 * 1.0 - fees_r(100.0, [(0.3, 101.0, MAKER), (0.7, 101.0, TAKER)])
        self.assertAlmostEqual(t["r"], expected, places=6)
        self.assertEqual(res["policies"]["current_no_lock"]["trades"][0]["exit_kind"], "capped")
        self.assertTrue(t["lock_binding"])
        self.assertFalse(res["policies"]["current_no_lock"]["trades"][0]["lock_binding"])
        closed = Market(path).k15m[:40]  # the 15m bars closed at entry (T0 candle still forming)
        atr = dem.calculate_atr([float(k[2]) for k in closed], [float(k[3]) for k in closed],
                                [float(k[4]) for k in closed], period=14)
        self.assertAlmostEqual(t["atr_r"], atr / 1.0, places=5)
        self.assertGreater(t["atr_r"], 0)

    def test_trail_cadence_1m_tightens_within_the_candle(self):
        # TP1 fills in bar 1 (close 101.2): the 0.5x ATR price floor caps the stop near 100.7. Bar 2 closes 101.9, so
        # a 1m-cadence trail raises the floor (and the stop) inside the same 15m candle and bar 3 (low 101.2) stops
        # it out; the 15m cadence only trails at the TP1 minute here and keeps the stop until the data ends.
        path = [(103.3, 100.5, 101.2), (102.0, 101.5, 101.9), (101.9, 101.2, 101.3)]
        fast = self.sim([outcome()], Market(path), trail_cadence="1m")["policies"]["current"]["trades"][0]
        slow = self.sim([outcome()], Market(path), trail_cadence="15m")["policies"]["current"]["trades"][0]
        self.assertEqual(fast["exit_kind"], "tp1+trail")
        self.assertEqual(slow["exit_kind"], "capped")
        self.assertGreater(fast["r"], 0.3 * 1.0 + 0.7 * 0.7)  # rest exited above the 15m-cadence stop (~100.7)

    def test_short_tp1_then_tp2(self):
        row = outcome(direction="SHORT", sl_price=101.0, tp1_price=99.0, tp2_price=95.0)
        t, _ = self.one(row, [(99.2, 98.8, 98.9), (99.0, 94.5, 94.8)], "current")
        self.assertEqual(t["exit_kind"], "tp2")
        expected = 0.3 * 1.0 + 0.7 * 5.0 - fees_r(100.0, [(0.3, 99.0, MAKER), (0.7, 95.0, MAKER)])
        self.assertAlmostEqual(t["r"], expected, places=6)

    def test_same_bar_sl_and_tp_is_sl(self):
        t, _ = self.one(outcome(), [(101.5, 98.5, 100.0)], "current")
        self.assertEqual(t["exit_kind"], "sl")
        self.assertAlmostEqual(t["r"], -1.0 - fees_r(100.0, [(1.0, 99.0, TAKER)]), places=6)

    def test_yolo_not_trailed_before_tp1(self):
        path = LOCK_PATH + [(102.2, 100.5, 100.6), (100.6, 98.9, 99.0)]
        plain, _ = self.one(outcome(tp1_price=104.0, tp2_price=110.0), path, "current")
        self.assertEqual(plain["exit_kind"], "trail_stop")
        yolo, _ = self.one(outcome(tp1_price=104.0, tp2_price=110.0, is_yolo=True), path, "current")
        self.assertEqual(yolo["exit_kind"], "sl")
        self.assertAlmostEqual(yolo["r"], -1.0 - fees_r(100.0, [(1.0, 99.0, TAKER)]), places=6)

    def test_horizon_cap_marks_to_last_close(self):
        path = [FLAT] * 28 + [(100.3, 99.8, 100.1)] + [(100.3, 99.8, 100.4)] * 30
        t, res = self.one(outcome(), path, "current", horizon_hours=0.5)
        # horizon = entry (T0 + 5.5 min) + 30 min: 1m bars closed by then open up to T0 + 34 min, i.e. 29 path bars
        # (last close 100.1); the later 100.4 bars are outside the horizon
        self.assertEqual(t["exit_kind"], "capped")
        self.assertTrue(t["capped"])
        self.assertEqual(res["policies"]["current"]["capped"], 1)
        self.assertAlmostEqual(t["r"], 0.1 - fees_r(100.0, [(1.0, 100.1, TAKER)]), places=6)


class TestPolicies(SimBase):

    def test_close_at_half_r(self):
        t, _ = self.one(outcome(), [FLAT, (100.6, 99.8, 100.5)], "close_at_0_5r")
        self.assertEqual(t["exit_kind"], "target")
        self.assertAlmostEqual(t["r"], 0.5 - fees_r(100.0, [(1.0, 100.5, MAKER)]), places=6)

    def test_tp2_at_2_5r(self):
        path = [(101.2, 100.8, 101.1), (103.0, 101.0, 102.8)]
        res = self.sim([outcome()], Market(path), policies=("tp2_2_5r", "current"))
        t = res["policies"]["tp2_2_5r"]["trades"][0]
        self.assertEqual(t["exit_kind"], "tp2")
        expected = 0.3 * 1.0 + 0.7 * 2.5 - fees_r(100.0, [(0.3, 101.0, MAKER), (0.7, 102.5, MAKER)])
        self.assertAlmostEqual(t["r"], expected, places=6)
        self.assertEqual(res["policies"]["current"]["trades"][0]["exit_kind"], "capped")  # row TP2 105 not reached

    def test_split_50_50(self):
        path = [(101.2, 100.8, 101.1), (105.5, 101.0, 105.2)]
        t, _ = self.one(outcome(), path, "split_50_50")
        expected = 0.5 * 1.0 + 0.5 * 5.0 - fees_r(100.0, [(0.5, 101.0, MAKER), (0.5, 105.0, MAKER)])
        self.assertAlmostEqual(t["r"], expected, places=6)
        row, p = outcome(), eps.TradePath(outcome(), Market(path).k1m[6:], Market(path).k15m, 48 * 3600_000, NOW_MS)
        detail = eps.replay(row, p, eps.build_policies()["split_50_50"], dict(TICK_FILTERS), "prod", TAKER, MAKER)
        self.assertEqual([(e["frac"], e["kind"]) for e in detail["exits"]], [(0.5, "tp1"), (0.5, "tp2")])


class TestMetrics(SimBase):

    def test_hand_built_metrics(self):
        results = [
            {"r": -1.0, "mfe_r": 0.0, "entry_ts": 2, "exit_kind": "sl", "capped": False},
            {"r": 2.0, "mfe_r": 3.0, "entry_ts": 1, "exit_kind": "tp2", "capped": False},
            {"r": 0.5, "mfe_r": 1.0, "entry_ts": 3, "exit_kind": "capped", "capped": True},
        ]
        m = eps.policy_metrics(results)
        self.assertEqual(m["n"], 3)
        self.assertAlmostEqual(m["win_rate"], 2 / 3, places=5)
        self.assertAlmostEqual(m["expectancy_r"], 0.5)
        self.assertAlmostEqual(m["total_r"], 1.5)
        self.assertAlmostEqual(m["profit_factor_r"], 2.5)
        self.assertAlmostEqual(m["avg_win_r"], 1.25)
        self.assertAlmostEqual(m["avg_loss_r"], -1.0)
        self.assertAlmostEqual(m["max_drawdown_r"], 1.0)  # ordered by entry_ts: +2, -1, +0.5
        self.assertAlmostEqual(m["capture_ratio"], 2.5 / 4.0)
        self.assertEqual((m["exit_kinds"]["sl"], m["exit_kinds"]["tp2"], m["exit_kinds"]["capped"]), (1, 1, 1))
        self.assertEqual(m["capped"], 1)
        self.assertIsNone(eps.policy_metrics(results[1:])["profit_factor_r"])  # no losses

    def test_skipped_counts_per_reason(self):
        rows = [
            outcome(),
            outcome(env="testnet"),
            outcome(status="open"),
            outcome(initial_risk=None),
            outcome(tp2_price=None),
            outcome(symbol="ETHUSDT"),   # klines raise
            outcome(symbol="SOLUSDT"),   # 5 warmup bars
            outcome(symbol="DOGEUSDT"),  # not in exchangeInfo
        ]
        fake = MultiMarket({"BTCUSDT": Market([FLAT, (100.0, 98.9, 99.0)]), "ETHUSDT": OSError("timeout"),
                            "SOLUSDT": Market([FLAT, (100.0, 98.9, 99.0)], warmup=5)})
        res = self.sim(rows, fake, policies=eps.POLICY_NAMES)
        self.assertEqual(res["n_trades"], 1)
        self.assertEqual(res["skipped"], {"other_env": 1, "not_closed": 1, "no_risk": 1, "no_levels": 1,
                                          "klines_error": 1, "warmup_short": 1, "filters_error": 1})
        self.assertEqual({m["n"] for m in res["policies"].values()}, {1})
        self.assertTrue(all(c[5] == eps.KLINES_TIMEOUT_SECONDS and c[3] == eps.KLINES_LIMIT for c in fake.calls))

    def test_approximate_entry_flagged_and_excluded_from_exact_fidelity(self):
        rows = [outcome(realized_r_net=-1.0),
                outcome(symbol="ETHUSDT", entry_commission_included=False, realized_r_net=0.5),
                outcome(symbol="SOLUSDT", entry_commission_included=True, realized_r_net=-1.2)]
        sl_path = [FLAT, (100.0, 98.9, 99.0)]
        fake = MultiMarket({s: Market(sl_path) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")})
        res = self.sim(rows, fake)
        sim_r = -1.0 - fees_r(100.0, [(1.0, 99.0, TAKER)])
        flags = {t["symbol"]: t["entry_ts_approx"] for t in res["policies"]["current"]["trades"]}
        self.assertEqual(flags, {"BTCUSDT": False, "ETHUSDT": True, "SOLUSDT": False})
        fid = res["fidelity"]
        self.assertEqual({f["symbol"]: f["entry_ts_approx"] for f in fid["trades"]}, flags)
        self.assertEqual((fid["compared"], fid["compared_exact_entry"]), (3, 2))
        all_diffs = [abs(sim_r + 1.0), abs(sim_r - 0.5), abs(sim_r + 1.2)]
        self.assertAlmostEqual(fid["mean_abs_diff_r"], sum(all_diffs) / 3, places=5)
        self.assertAlmostEqual(fid["mean_abs_diff_r_exact_entry"], (all_diffs[0] + all_diffs[2]) / 2, places=5)
        self.assertTrue(any("1 trade(s) with approximate entry_ts" in w for w in res["warnings"]))
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=fake):
            exact = eps.simulate(rows, "prod", ["current"], 48.0, TAKER, MAKER, now_ms=NOW_MS, exact_entry_only=True)
        self.assertEqual((exact["n_trades"], exact["skipped"]), (2, {"entry_ts_approx": 1}))
        self.assertEqual(exact["fidelity"]["compared_exact_entry"], 2)

    def test_exchange_info_failure_skips_rows(self):
        with patch("exit_policy_sim.fetch_exchange_info", side_effect=OSError("down")):
            res = self.sim([outcome()], Market([FLAT]))
        self.assertEqual((res["n_trades"], res["skipped"]), (0, {"filters_error": 1}))
        self.assertFalse(res["ok"])


class TestCli(SimBase):

    def write_outcomes(self, rows):
        with open(os.path.join(self.logs, "trade_outcomes.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def run_cli(self, argv, market=None):
        out = io.StringIO()
        fake = MultiMarket({"BTCUSDT": market or Market([FLAT, (100.0, 98.9, 99.0)])})
        with patch("utils.trade_excursion.fetch_klines_range", side_effect=fake), contextlib.redirect_stdout(out):
            code = eps.main(argv)
        return code, out.getvalue()

    def test_json_shape_and_output_file(self):
        self.write_outcomes([outcome(), outcome(env="testnet")])
        code, out = self.run_cli(["--env", "prod", "--json"])
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(set(res), {"ok", "env", "since_rows", "horizon_hours", "fees", "n_trades", "skipped",
                                    "policies", "fidelity", "ranking", "warnings", "trail_cadence",
                                    "exact_entry_only", "elapsed_seconds"})
        self.assertEqual((res["trail_cadence"], res["exact_entry_only"]), ("1m", False))
        self.assertGreaterEqual(res["elapsed_seconds"], 0)
        self.assertTrue(res["ok"])
        self.assertEqual((res["env"], res["n_trades"], res["since_rows"]), ("prod", 1, ["2026-10-01"]))
        self.assertEqual(set(res["policies"]), set(eps.POLICY_NAMES))
        self.assertEqual(res["fees"]["taker"], TAKER)
        self.assertTrue(all(r["insufficient_sample"] for r in res["ranking"]))
        self.assertIn(eps.IN_SAMPLE_NOTE, res["warnings"])
        fid = res["fidelity"]["trades"][0]
        self.assertAlmostEqual(fid["diff"], fid["sim_r"] - (-1.1), places=6)
        with open(os.path.join(self.logs, "exit_policy_sim.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), res)

    def test_human_mode_and_policy_subset(self):
        self.write_outcomes([outcome()])
        code, out = self.run_cli(["--env", "prod", "--policies", "current,close_at_0_5r"])
        self.assertEqual(code, 0)
        self.assertIn("close_at_0_5r", out)
        self.assertNotIn("split_50_50", out)

    def test_exact_entry_only_flag(self):
        self.write_outcomes([outcome(entry_commission_included=False)])
        code, out = self.run_cli(["--env", "prod", "--json", "--exact-entry-only", "--trail-cadence", "15m"])
        self.assertEqual(code, 1)
        res = json.loads(out)
        self.assertEqual((res["skipped"], res["trail_cadence"], res["exact_entry_only"]),
                         ({"entry_ts_approx": 1}, "15m", True))

    def test_exit_1_without_trades(self):
        code, out = self.run_cli(["--env", "prod", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.logs, "exit_policy_sim.json")))

    def test_exit_2_on_bad_args(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_cli(["--env", "prod", "--policies", "nope"])[0], 2)
            self.assertEqual(self.run_cli(["--env", "prod", "--horizon-hours", "0"])[0], 2)
            with self.assertRaises(SystemExit) as cm:
                self.run_cli(["--horizon-hours", "abc"])
        self.assertEqual(cm.exception.code, 2)


class TestExchangeInfo(unittest.TestCase):

    def test_unsigned_public_get(self):
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append((req.full_url, dict(req.header_items()), timeout))
            return io.BytesIO(json.dumps(EXCHANGE_INFO).encode())

        with patch("utils.trade_excursion.klines_base_url", return_value="https://fapi.binance.com"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("execute_futures_trade.send_signed_request", side_effect=AssertionError("signed")):
            filters = eps.parse_filters(eps.fetch_exchange_info("prod"))
        self.assertEqual(len(seen), 1)
        url, headers, _ = seen[0]
        self.assertEqual(url, "https://fapi.binance.com/fapi/v1/exchangeInfo")
        self.assertFalse(any(h.lower() == "x-mbx-apikey" for h in headers))
        self.assertEqual(filters["BTCUSDT"], {"stepSize": 0.001, "minQty": 0.001, "tickSize": 0.1,
                                              "precision_qty": 3, "precision_price": 1, "minNotional": 5.0})


if __name__ == "__main__":
    unittest.main()
