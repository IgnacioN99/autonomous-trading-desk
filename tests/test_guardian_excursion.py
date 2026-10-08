#!/usr/bin/env python3
"""
test_guardian_excursion.py - Position guardian excursion tracking and position_closed records (issue #182).

- Per-position MFE / MAE in R from closed 1m bars after the fill minute plus the mark price, R from the matched
  trades_audit record; carried over across cycles (never decreasing), reset on a new entry price.
- Exactly one position_closed action per disappearance (with the excursion record, last_stop_r and timestamps);
  none after a positions_sync failure (excursions preserved) and none from a --once run beside a live loop.
- A klines failure only sets view["excursion_error"] (cycle_ok and errors unchanged); no extra userTrades call.
- trail_stop actions carry the new algo stop (new_stop).
Hermetic: FakeExchange, temp workspaces and log dirs, klines fetch patched, urlopen blocked, reporter mocked.
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

import position_guardian_loop as pgl
from utils import trade_excursion as tx
from test_exit_management import FakeExchange, offline, long_position, stop, structural
from test_issue_106_exit_manager_hardening import long_record, UserTradesExchange, buy_fill
from test_position_guardian import HEALTHY, read_actions

MIN = 60_000
USER_TRADES = "/fapi/v1/userTrades"


class KlineSource:
    """Fake fetch_klines_range: closed 1m bars from start_ms (at most `limit`), price from `path(open_ms)`."""

    def __init__(self, path, error=None):
        self.path = path
        self.error = error
        self.calls = []

    def __call__(self, symbol, interval, start_ms, limit, target_env):
        self.calls.append((symbol, interval, start_ms, limit, target_env))
        if self.error:
            raise self.error
        now_ms = int(time.time() * 1000)
        out = []
        o = int(start_ms)
        while len(out) < limit and o + MIN <= now_ms:
            high, low = self.path(o)
            out.append([o, str(low), str(high), str(low), str(high), "1", o + MIN - 1])
            o += MIN
        return out


class GuardianExcursionBase(unittest.TestCase):

    def setUp(self):
        for target in (patch("report_agent_issue.report_issue"),
                       patch("urllib.request.urlopen", side_effect=AssertionError("network access in offline test"))):
            target.start()
            self.addCleanup(target.stop)
        self.ws = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.ws, "logs")
        os.makedirs(self.log_dir, exist_ok=True)

    def cycle(self, fake, klines=None, calc=None, env="testnet", **kw):
        klines = klines or KlineSource(lambda o: (100.5, 99.5))
        with offline(fake, workspace=self.ws), \
             patch("utils.trade_excursion.fetch_klines_range", side_effect=klines), \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=calc), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(HEALTHY)), \
             contextlib.redirect_stderr(io.StringIO()):
            return pgl.run_cycle(env, log_dir=self.log_dir, **kw)

    def write_state(self, **over):
        state = {"schema_version": 1, "timestamp": int(time.time()) - 60, "env": "testnet", "dry_run": False,
                 "mode": "once", "interval_seconds": None, "cycle_ok": True, "positions": [], "actions": [],
                 "errors": [], "excursions": {}}
        state.update(over)
        with open(os.path.join(self.log_dir, pgl.STATE_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump(state, f)
        return state

    def read_state(self):
        with open(os.path.join(self.log_dir, pgl.STATE_FILE_NAME), "r", encoding="utf-8") as f:
            return json.load(f)

    def closed_actions(self, state):
        return [a for a in state["actions"] if a["type"] == "position_closed"]


class TestExcursionTracking(GuardianExcursionBase):

    def test_r_from_audit_record_and_closed_post_entry_bars(self):
        entry_ts = int(time.time()) - 3600
        long_record(self.ws, sl_price=95.0, timestamp=entry_ts)
        fill_bar = entry_ts * 1000 // MIN * MIN
        peak_bar = fill_bar + 10 * MIN
        klines = KlineSource(lambda o: (108.0, 99.0) if o == peak_bar else (101.0, 98.0))
        fake = FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)])
        state = self.cycle(fake, klines)

        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual(klines.calls, [("BTCUSDT", "1m", fill_bar + MIN, 99, "testnet")])
        self.assertEqual((rec["initial_sl"], rec["initial_risk"], rec["reference_source"]), (95.0, 5.0, "trade_audit"))
        self.assertEqual(rec["entry_ts"], entry_ts)
        self.assertEqual((rec["peak_price"], rec["trough_price"]), (108.0, 98.0))
        self.assertEqual((rec["mfe_r"], rec["mae_r"]), (1.6, -0.4))
        self.assertEqual(rec["mfe_ts"], peak_bar)
        self.assertFalse(rec["partial"])
        self.assertEqual(rec["last_stop_price"], 95.0)
        view = state["positions"][0]
        self.assertEqual((view["mfe_r"], view["mae_r"], view["peak_price"]), (1.6, -0.4, 108.0))
        self.assertTrue(state["cycle_ok"], state["errors"])
        self.assertEqual(state, self.read_state())

    def test_carry_over_never_decreases_and_fetches_from_next_bar(self):
        entry_ts = int(time.time()) - 3600
        long_record(self.ws, sl_price=95.0, timestamp=entry_ts)
        last_bar = (int(time.time()) - 600) * 1000 // MIN * MIN
        prev = {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0, "entry_ts": entry_ts, "initial_sl": 95.0,
                "initial_risk": 5.0, "reference_source": "trade_audit", "is_yolo": False, "tp1_filled": None,
                "tp1_seen_ts": None, "last_stop_price": 95.0, "first_seen_ts": entry_ts + 30,
                "last_seen_ts": entry_ts + 30, "peak_price": 115.0, "trough_price": 97.0, "mfe_r": 3.0,
                "mae_r": -0.6, "mfe_pct": 15.0, "mae_pct": -3.0, "mfe_ts": last_bar - MIN, "mae_ts": last_bar - 5 * MIN,
                "last_bar_open_ms": last_bar, "partial": False}
        self.write_state(excursions={"BTCUSDT|LONG": prev})
        klines = KlineSource(lambda o: (102.0, 99.0))
        fake = FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)])
        state = self.cycle(fake, klines)
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual(klines.calls[0][2], last_bar + MIN)
        self.assertEqual((rec["mfe_r"], rec["mae_r"], rec["peak_price"]), (3.0, -0.6, 115.0))
        self.assertEqual(rec["first_seen_ts"], entry_ts + 30)
        self.assertGreater(rec["last_bar_open_ms"], last_bar)
        self.assertEqual(self.closed_actions(state), [])

    def test_two_cycles_keep_the_record(self):
        fake = FakeExchange([long_position(mark="103.0")], algos=[stop(501, 95.0)])
        first = self.cycle(fake)
        rec1 = first["excursions"]["BTCUSDT|LONG"]
        # No trades_audit record: prices / percent tracked from first sight, R unknown, partial.
        self.assertEqual((rec1["initial_risk"], rec1["reference_source"], rec1["mfe_r"]), (None, None, None))
        self.assertTrue(rec1["partial"])
        self.assertEqual((rec1["peak_price"], rec1["mfe_pct"]), (103.0, 3.0))
        fake.positions[0]["markPrice"] = "101.0"
        second = self.cycle(fake)
        rec2 = second["excursions"]["BTCUSDT|LONG"]
        self.assertEqual(rec2["peak_price"], 103.0)
        self.assertEqual(rec2["first_seen_ts"], rec1["first_seen_ts"])
        self.assertEqual(self.closed_actions(second), [])

    def test_reset_on_new_entry_price(self):
        self.write_state(excursions={"BTCUSDT|LONG": {
            "symbol": "BTCUSDT", "side": "LONG", "entry_price": 90.0, "entry_ts": 1, "first_seen_ts": 1,
            "peak_price": 120.0, "trough_price": 85.0, "mfe_pct": 33.3, "mae_pct": -5.5, "last_bar_open_ms": 60_000,
            "reference_source": None, "partial": True}})
        fake = FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)])
        state = self.cycle(fake)
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual(rec["entry_price"], 100.0)
        self.assertEqual(rec["peak_price"], 101.0)
        self.assertEqual(rec["first_seen_ts"], state["timestamp"])
        self.assertNotEqual(rec["last_bar_open_ms"], 60_000)

    def test_tp1_seen_ts_set_once(self):
        prev_ts = int(time.time()) - 120
        self.write_state(excursions={"BTCUSDT|LONG": {
            "symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0, "entry_ts": prev_ts, "first_seen_ts": prev_ts,
            "tp1_filled": True, "tp1_seen_ts": prev_ts, "reference_source": None, "partial": True}})
        state = self.cycle(FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)]))
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual((rec["tp1_filled"], rec["tp1_seen_ts"]), (True, prev_ts))

    def test_kline_failure_only_sets_excursion_error(self):
        fake = FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)])
        long_record(self.ws, sl_price=95.0, timestamp=int(time.time()) - 3600)
        state = self.cycle(fake, KlineSource(None, error=OSError("klines down")))
        view = state["positions"][0]
        self.assertEqual(view["excursion_error"], "OSError: klines down")
        self.assertTrue(state["cycle_ok"])
        self.assertEqual(state["errors"], [])
        self.assertEqual(state["error_stages"], [])

    def test_kline_failure_keeps_the_previous_record(self):
        prev = {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0, "entry_ts": 1, "first_seen_ts": 1,
                "peak_price": 109.0, "mfe_r": 1.8, "reference_source": "trade_audit", "partial": False}
        self.write_state(excursions={"BTCUSDT|LONG": prev})
        long_record(self.ws, sl_price=95.0, timestamp=int(time.time()) - 3600)
        state = self.cycle(FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)]),
                           KlineSource(None, error=OSError("klines down")))
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual((rec["peak_price"], rec["mfe_r"]), (109.0, 1.8))
        self.assertEqual(rec["last_seen_ts"], state["timestamp"])

    def test_reference_never_adds_a_user_trades_call(self):
        # Dry-run orphan: the trail does not run, so the excursion reference must not read userTrades itself.
        long_record(self.ws, sl_price=95.0, timestamp=int(time.time()) - 3600)
        fake = FakeExchange([long_position(mark="101.0")], algos=[])
        state = self.cycle(fake, dry_run=True)
        self.assertEqual([c for c in fake.calls if c[1] == USER_TRADES], [])
        rec = state["excursions"]["BTCUSDT|LONG"]
        # The reference is skipped (its staleness check would need userTrades): no R, tracked from first sight.
        self.assertEqual((rec["initial_risk"], rec["reference_source"], rec["mfe_r"]), (None, None, None))
        self.assertTrue(rec["partial"])

    def test_skipped_reference_keeps_the_earlier_one(self):
        entry_ts = int(time.time()) - 3600
        long_record(self.ws, sl_price=95.0, timestamp=entry_ts)
        prev = {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0, "entry_ts": entry_ts, "initial_sl": 95.0,
                "initial_risk": 5.0, "reference_source": "trade_audit", "first_seen_ts": entry_ts + 30,
                "peak_price": 104.0, "trough_price": 99.0, "mfe_r": 0.8, "mae_r": -0.2, "partial": False}
        self.write_state(excursions={"BTCUSDT|LONG": prev})
        fake = FakeExchange([long_position(mark="101.0")], algos=[])
        state = self.cycle(fake, dry_run=True)
        self.assertEqual([c for c in fake.calls if c[1] == USER_TRADES], [])
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual((rec["initial_risk"], rec["reference_source"], rec["mfe_r"]), (5.0, "trade_audit", 0.8))
        self.assertFalse(rec["partial"])

    def test_reference_rejected_as_stale_drops_the_carried_r(self):
        # The trail's userTrades open time is newer than the record: dem rejects it (current_stop), so R is dropped.
        entry_ts = int(time.time()) - 3600
        long_record(self.ws, sl_price=95.0, timestamp=entry_ts)
        prev = {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0, "entry_ts": entry_ts, "initial_sl": 95.0,
                "initial_risk": 5.0, "reference_source": "trade_audit", "first_seen_ts": entry_ts + 30,
                "peak_price": 104.0, "trough_price": 99.0, "mfe_r": 0.8, "mae_r": -0.2, "partial": False}
        self.write_state(excursions={"BTCUSDT|LONG": prev})
        fake = UserTradesExchange([long_position(mark="101.0")], [buy_fill(time.time() - 600)], algos=[stop(501, 95.0)])
        state = self.cycle(fake)
        rec = state["excursions"]["BTCUSDT|LONG"]
        self.assertEqual((rec["initial_risk"], rec["reference_source"], rec["mfe_r"]), (None, None, None))
        self.assertEqual(rec["peak_price"], 104.0)  # prices kept

    def test_one_user_trades_call_when_trail_ran(self):
        long_record(self.ws, sl_price=95.0, timestamp=int(time.time()) - 3600)
        fake = FakeExchange([long_position(mark="101.0")], algos=[stop(501, 95.0)])
        self.cycle(fake)
        self.assertEqual(len([c for c in fake.calls if c[1] == USER_TRADES]), 1)


class _PositionsDown(FakeExchange):
    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == "/fapi/v2/positionRisk":
            self.calls.append((method, endpoint, dict(params or {})))
            return {"code": -1001, "msg": "Internal error"}
        return super().__call__(method, endpoint, params, target_env, retry_count)


class TestPositionClosed(GuardianExcursionBase):

    def _open_then_gone(self):
        entry_ts = int(time.time()) - 3600
        long_record(self.ws, sl_price=95.0, timestamp=entry_ts)
        fake = FakeExchange([long_position(mark="104.0")], algos=[stop(501, 102.0)])
        first = self.cycle(fake)
        self.assertIn("BTCUSDT|LONG", first["excursions"])
        return first, FakeExchange([], algos=[])

    def test_exactly_one_record_per_disappearance(self):
        first, flat = self._open_then_gone()
        second = self.cycle(flat)
        closed = self.closed_actions(second)
        self.assertEqual(len(closed), 1)
        a = closed[0]
        self.assertEqual((a["symbol"], a["success"], a["dry_run"]), ("BTCUSDT", True, False))
        d = a["detail"]
        prev = first["excursions"]["BTCUSDT|LONG"]
        for k, v in prev.items():
            self.assertEqual(d[k], v, k)
        self.assertEqual(d["last_stop_r"], 0.4)  # (102 - 100) / 5
        self.assertEqual(d["disappeared_after_ts"], first["timestamp"])
        self.assertEqual(d["detected_ts"], second["timestamp"])
        self.assertEqual(second["excursions"], {})
        self.assertTrue(second["cycle_ok"])
        self.assertEqual(len([r for r in read_actions(self.log_dir) if r["type"] == "position_closed"]), 1)
        third = self.cycle(FakeExchange([], algos=[]))
        self.assertEqual(self.closed_actions(third), [])

    def test_dry_run_emits_with_dry_run_flag(self):
        self._open_then_gone()
        second = self.cycle(FakeExchange([], algos=[]), dry_run=True)
        closed = self.closed_actions(second)
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0]["dry_run"])

    def test_short_last_stop_r_sign(self):
        self.write_state(excursions={"ETHUSDT|SHORT": {
            "symbol": "ETHUSDT", "side": "SHORT", "entry_price": 100.0, "initial_risk": 4.0, "last_stop_price": 98.0}})
        state = self.cycle(FakeExchange([], algos=[]))
        self.assertEqual(self.closed_actions(state)[0]["detail"]["last_stop_r"], 0.5)

    def test_no_risk_gives_null_last_stop_r(self):
        self.write_state(excursions={"ETHUSDT|LONG": {
            "symbol": "ETHUSDT", "side": "LONG", "entry_price": 100.0, "initial_risk": None, "last_stop_price": 98.0}})
        state = self.cycle(FakeExchange([], algos=[]))
        self.assertIsNone(self.closed_actions(state)[0]["detail"]["last_stop_r"])

    def test_none_after_positions_sync_failure_and_excursions_preserved(self):
        first, _ = self._open_then_gone()
        second = self.cycle(_PositionsDown([], algos=[]))
        self.assertIn("positions_sync", second["error_stages"])
        self.assertEqual(self.closed_actions(second), [])
        self.assertEqual(second["excursions"], first["excursions"])
        self.assertEqual(self.read_state()["excursions"], first["excursions"])
        # The next healthy cycle still reports the disappearance once.
        third = self.cycle(FakeExchange([], algos=[]))
        self.assertEqual(len(self.closed_actions(third)), 1)

    def test_none_from_once_beside_a_live_loop(self):
        rec = {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0, "initial_risk": 5.0, "last_stop_price": 95.0}
        self.write_state(mode="loop", interval_seconds=60, timestamp=int(time.time()) - 5,
                         excursions={"BTCUSDT|LONG": rec})
        state = self.cycle(FakeExchange([], algos=[]), mode="once")
        self.assertEqual(self.closed_actions(state), [])
        self.assertEqual(self.read_state()["excursions"], {"BTCUSDT|LONG": rec})  # loop state untouched
        self.assertEqual([r for r in read_actions(self.log_dir) if r["type"] == "position_closed"], [])

    def test_none_from_testnet_cycle_beside_a_live_prod_loop(self):
        # The previous state belongs to PROD: a TESTNET cycle neither carries it over nor reports closes from it.
        rec = {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0}
        self.write_state(env="prod", mode="loop", interval_seconds=60, timestamp=int(time.time()) - 5,
                         excursions={"BTCUSDT|LONG": rec})
        state = self.cycle(FakeExchange([], algos=[]), env="testnet", mode="loop", interval_seconds=60)
        self.assertEqual(self.closed_actions(state), [])
        self.assertEqual(self.read_state()["env"], "prod")


class TestTrailStopCarriesNewStop(GuardianExcursionBase):

    def test_trail_stop_action_has_new_stop(self):
        fake = FakeExchange([long_position()], algos=[stop(501, 95.0)])
        state = self.cycle(fake, calc=structural(102.0))
        trail = [a for a in state["actions"] if a["type"] == "trail_stop"]
        self.assertEqual(len(trail), 1)
        self.assertTrue(trail[0]["success"])
        new_stop = trail[0]["detail"]["new_stop"]
        self.assertIsInstance(new_stop, dict)
        self.assertEqual({str(new_stop.get("algo_id"))}, {str(a["algoId"]) for a in fake.algos})  # old one cancelled
        self.assertEqual(trail[0]["detail"]["new_sl"], 102.0)


if __name__ == "__main__":
    unittest.main()
