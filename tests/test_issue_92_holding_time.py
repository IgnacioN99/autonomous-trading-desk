#!/usr/bin/env python3
"""
test_issue_92_holding_time.py - Offline tests for issue #92 (no network, no orders).

trading_drift_watchdog reported positions opened after the last ledger sync as 0.0h held (entry_time_ts defaulted to
"now"), so dead alpha never fired (fail-open). Covered here:
1. utils/position_timing.entry_time_from_fills: simple open, add-ons, partial reduce, flip, window too short, hedge.
2. resolve_entry_time: userTrades -> trades_audit (direction/env match, event records skipped) -> UNKNOWN; error
   dict (MCP gateway) / exception fall through; never now, never updateTime.
3. Watchdog: a position missing from the ledger gets its real holding time; no fill + no audit row -> UNKNOWN
   (elapsed_hours None + warning), never 0.0h and never dead alpha.
4. sync_session_state: no updateTime / now fallback (entry_time_ts null, "UNKNOWN", entry_time_source).
5. Doctor: missing/stale ledger is synced in-process BEFORE the watchdog; a failed sync skips the temporal audit with
   a warning; a watchdog exception is a named warning (exit code unchanged).
6. Guardian: UNKNOWN holding never closes, < 4h never closes even on a 15m stall, >= 4h + stagnant + stall closes
   (with --close-dead-alpha); doctor watchdog and guardian agree on the verdict for the same inputs.
"""

import io
import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import contextlib
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
LOOPS_DIR = os.path.join(SCRIPTS_DIR, "loops")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (SCRIPTS_DIR, LOOPS_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execute_futures_trade as eft
import sync_session_state as sss
import trading_doctor
import trading_drift_watchdog as tdw
import position_guardian_loop as pgl
from utils import position_timing as pt
from test_exit_management import FakeExchange, offline, stop

HOUR = 3600
STALLED = {"status": "DEAD_ALPHA_STALLED", "range_pct": 0.2, "recommendation": "CLOSE_OR_PROTECT", "message": "stalled"}
HEALTHY = {"status": "HEALTHY_MOMENTUM", "range_pct": 1.2, "recommendation": "HOLD", "message": "ok"}
DOCTOR_PROFILE = {"profile_completed": True, "risk_pct_equity": 0.005, "max_margin_ratio": 0.30,
                  "yolo_slot_enabled": False, "leverage_standard": 3, "leverage_yolo": 7, "leverage_ceiling": 10,
                  "operating_mode": "BALANCED_DELTA_NEUTRAL"}


def fill(side, qty, ts_s, fid=None, position_side="BOTH"):
    return {"symbol": "BTCUSDT", "side": side, "qty": str(qty), "time": int(ts_s * 1000),
            "id": fid if fid is not None else int(ts_s), "positionSide": position_side}


def row(symbol="BTCUSDT", amt="1", entry="100.0", mark="100.5", upnl="0.5", margin="33.5", leverage="3",
        update_time=None):
    r = {"symbol": symbol, "positionAmt": str(amt), "entryPrice": str(entry), "markPrice": str(mark),
         "unRealizedProfit": str(upnl), "isolatedMargin": str(margin), "leverage": str(leverage),
         "liquidationPrice": "50.0", "marginType": "isolated"}
    if update_time is not None:
        r["updateTime"] = update_time
    return r


class FillsExchange(FakeExchange):
    """FakeExchange plus GET /fapi/v1/userTrades: per-symbol fills (or an error payload / exception)."""

    def __init__(self, positions, fills=None, trades_error=None, **kw):
        super().__init__(positions, **kw)
        self.fills = {k: [dict(f) for f in v] for k, v in (fills or {}).items()}
        self.trades_error = trades_error

    def __call__(self, method, endpoint, params=None, target_env=None, retry_count=0):
        if endpoint == "/fapi/v1/userTrades":
            self.calls.append((method, endpoint, dict(params or {})))
            if isinstance(self.trades_error, Exception):
                raise self.trades_error
            if self.trades_error is not None:
                return dict(self.trades_error)
            sym = (params or {}).get("symbol")
            return [dict(f) for f in self.fills.get(sym, [])] if sym else []
        if endpoint == "/fapi/v1/ticker/price":
            self.calls.append((method, endpoint, dict(params or {})))
            return {"price": "100.0"}
        return super().__call__(method, endpoint, params, target_env, retry_count)


def write_audit(ws, *records):
    logs = os.path.join(ws, "logs")
    os.makedirs(logs, exist_ok=True)
    with open(os.path.join(logs, "trades_audit.jsonl"), "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return os.path.join(logs, "trades_audit.jsonl")


def audit_rec(ts, symbol="BTCUSDT", direction="LONG", env="testnet", entry_price=100.0, total_qty=1.0, **extra):
    """Audit entry record; defaults match row() (entry 100.0, qty 1) so it describes the live position."""
    rec = {"timestamp": ts, "symbol": symbol, "direction": direction, "total_qty": total_qty,
           "entry_price": entry_price, "target_env": env}
    rec.update(extra)
    return rec


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


# =============================================================================
# 1. userTrades reconstruction
# =============================================================================
class TestEntryTimeFromFills(unittest.TestCase):
    T = 1_700_000_000

    def test_simple_open(self):
        self.assertEqual(pt.entry_time_from_fills([fill("BUY", 1, self.T)], "1"), self.T)
        self.assertEqual(pt.entry_time_from_fills([fill("SELL", "0.003", self.T), fill("SELL", "0.002", self.T + 60)],
                                                  "-0.005"), self.T)

    def test_add_on_fills_after_an_older_round_trip(self):
        fills = [fill("BUY", 1, self.T - 900), fill("SELL", 1, self.T - 600),   # closed earlier trade
                 fill("BUY", 1, self.T), fill("BUY", 2, self.T + 300)]
        self.assertEqual(pt.entry_time_from_fills(fills, "3"), self.T)

    def test_partial_reduce(self):
        fills = [fill("BUY", 2, self.T), fill("SELL", 1, self.T + 300)]
        self.assertEqual(pt.entry_time_from_fills(fills, "1"), self.T)

    def test_flip_uses_the_flipping_fill(self):
        fills = [fill("BUY", 10, self.T), fill("SELL", 15, self.T + 600)]
        self.assertEqual(pt.entry_time_from_fills(fills, "-5"), self.T + 600)

    def test_unsorted_input(self):
        fills = [fill("BUY", 2, self.T + 300), fill("BUY", 1, self.T)]
        self.assertEqual(pt.entry_time_from_fills(fills, "3"), self.T)

    def test_window_too_short_or_hedge_mode_is_unreconciled(self):
        self.assertIsNone(pt.entry_time_from_fills([fill("BUY", 1, self.T)], "3"))
        self.assertIsNone(pt.entry_time_from_fills([fill("BUY", 1, self.T, position_side="LONG")], "1"))
        self.assertIsNone(pt.entry_time_from_fills([], "1"))
        self.assertIsNone(pt.entry_time_from_fills([{"side": "BUY"}], "1"))


# =============================================================================
# 2. resolve_entry_time fallbacks
# =============================================================================
class TestResolveEntryTime(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.now = int(time.time())

    def test_user_trades_first(self):
        fetch = MagicMock(return_value=[fill("BUY", 1, self.now - 5 * HOUR)])
        path = write_audit(self.ws, audit_rec(self.now - 1 * HOUR))
        self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "1", "testnet", fetch=fetch, audit_path=path),
                         (self.now - 5 * HOUR, "userTrades"))
        args, kwargs = fetch.call_args
        self.assertEqual(args[:2], ("GET", "/fapi/v1/userTrades"))
        self.assertEqual(args[2], {"symbol": "BTCUSDT", "limit": 1000})

    def test_error_dict_exception_and_short_window_fall_back_to_matching_audit(self):
        path = write_audit(self.ws,
                           audit_rec(self.now - 6 * HOUR),                                   # the match
                           audit_rec(self.now - 2 * HOUR, direction="SHORT"),               # wrong direction
                           audit_rec(self.now - 1 * HOUR, env="prod"),                       # wrong env
                           {"timestamp": self.now, "symbol": "BTCUSDT", "event": "failsafe_abort"},  # event
                           {"timestamp": self.now, "symbol": "BTCUSDT", "direction": "LONG"})        # no total_qty
        for fetch in (MagicMock(return_value={"error": "endpoint not mapped by the MCP gateway"}),
                      MagicMock(return_value={"code": -1021, "msg": "timestamp"}),
                      MagicMock(side_effect=OSError("down")),
                      MagicMock(return_value=[fill("BUY", "0.5", self.now - HOUR)])):     # cannot reconcile 1.0
            self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "1", "testnet", entry_price="100.2",
                                                   fetch=fetch, audit_path=path),
                             (self.now - 6 * HOUR, "trades_audit"))

    def test_env_aliases_compare_equal(self):
        path = write_audit(self.ws, audit_rec(self.now - 6 * HOUR, env="mainnet"))
        self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "1", "prod", entry_price=100.0, audit_path=path),
                         (self.now - 6 * HOUR, "trades_audit"))

    def test_snapshot_race_falls_through(self):
        """positionRisk read before a fill that userTrades already shows (or vice versa): the walk cannot reconcile
        the snapshot size, so it falls through (here to UNKNOWN), never guessing an entry time."""
        fills = [fill("BUY", 1, self.now - 5 * HOUR), fill("SELL", 1, self.now - 3 * HOUR),
                 fill("BUY", "0.4", self.now - 60)]     # new position opened after the positionRisk snapshot
        fetch = MagicMock(return_value=fills)
        self.assertIsNone(pt.entry_time_from_fills(fills, "1"))
        self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "1", "testnet", entry_price=100.0, fetch=fetch,
                                               audit_path=os.path.join(self.ws, "missing.jsonl")),
                         (None, "UNKNOWN"))

    def test_stale_same_direction_record_is_rejected(self):
        """A manual/MCP position opened today at 120 must not inherit last week's 100.0 audit record."""
        path = write_audit(self.ws, audit_rec(self.now - 7 * 24 * HOUR, entry_price=100.0, total_qty=1.0))
        no_fills = MagicMock(return_value={"error": "not mapped"})
        self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "1", "testnet", entry_price=120.0, fetch=no_fills,
                                               audit_path=path), (None, "UNKNOWN"))
        # bigger than the recorded size -> not that trade either
        self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "2", "testnet", entry_price=100.0, fetch=no_fills,
                                               audit_path=path), (None, "UNKNOWN"))
        # without the live entry price the audit fallback is never used
        self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "1", "testnet", fetch=no_fills, audit_path=path),
                         (None, "UNKNOWN"))
        # matching record (entry within 0.5%, TP1 partial reduce left 0.7 of 1.0) -> trades_audit
        self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "0.7", "testnet", entry_price=100.4, fetch=no_fills,
                                               audit_path=path), (self.now - 7 * 24 * HOUR, "trades_audit"))

    def test_audit_matching_helper(self):
        rec = audit_rec(1, entry_price=100.0, total_qty=1.0)
        self.assertTrue(pt.audit_record_matches_position(rec, 100.49, "-1"))
        self.assertFalse(pt.audit_record_matches_position(rec, 100.6, "1"))
        self.assertFalse(pt.audit_record_matches_position(rec, 100.0, "1.01"))
        self.assertFalse(pt.audit_record_matches_position({"total_qty": 1}, 100.0, "1"))
        self.assertTrue(pt.is_autonomous_close_allowed("userTrades"))
        self.assertFalse(pt.is_autonomous_close_allowed("trades_audit"))
        self.assertFalse(pt.is_autonomous_close_allowed("UNKNOWN"))

    def test_unknown_never_now(self):
        path = write_audit(self.ws, audit_rec(self.now - HOUR, direction="SHORT"))
        res = pt.resolve_entry_time("BTCUSDT", "LONG", "1", "testnet", entry_price=100.0,
                                    fetch=MagicMock(return_value={"error": "x"}), audit_path=path)
        self.assertEqual(res, (None, "UNKNOWN"))
        self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "1", "testnet"), (None, "UNKNOWN"))

    def test_preparsed_audit_records(self):
        recs = [audit_rec(self.now - 6 * HOUR), audit_rec(self.now - HOUR, direction="SHORT"),
                {"timestamp": self.now, "symbol": "BTCUSDT", "event": "abort"}]
        self.assertEqual(pt.resolve_entry_time("BTCUSDT", "LONG", "1", "testnet", entry_price=100.0,
                                               audit_records=recs),
                         (self.now - 6 * HOUR, "trades_audit"))


class TestAssessDeadAlpha(unittest.TestCase):

    def test_verdicts(self):
        a = pt.assess_dead_alpha
        self.assertEqual(a(elapsed_hours=None, entry_price=100, mark_price=100.1, roe_pct=0.3)["verdict"], "UNKNOWN")
        self.assertFalse(a(elapsed_hours=None, entry_price=100, mark_price=100.1, roe_pct=0.3)["is_dead_alpha"])
        self.assertEqual(a(elapsed_hours=5, entry_price=100, mark_price=100.5, roe_pct=1.5)["verdict"], "DEAD_ALPHA")
        self.assertEqual(a(elapsed_hours=4.0, entry_price=100, mark_price=100.5, roe_pct=1.5)["verdict"], "DEAD_ALPHA")
        self.assertEqual(a(elapsed_hours=3.9, entry_price=100, mark_price=100.5, roe_pct=1.5)["verdict"], "HEALTHY")
        self.assertEqual(a(elapsed_hours=5, entry_price=100, mark_price=102, roe_pct=6)["verdict"], "HEALTHY")
        self.assertEqual(a(elapsed_hours=5, entry_price=100, mark_price=100.5, roe_pct=20)["verdict"], "HEALTHY")
        self.assertEqual(a(elapsed_hours=5, entry_price=0, mark_price=100.5, roe_pct=1)["verdict"], "UNKNOWN")

    def test_roe_matches_watchdog_and_falls_back_to_notional(self):
        self.assertAlmostEqual(pt.position_roe_pct(row(upnl="10", margin="100")), 10.0)
        self.assertAlmostEqual(pt.position_roe_pct(row(amt="3", mark="100", upnl="10", margin="0", leverage="3")), 10.0)
        self.assertEqual(pt.position_roe_pct({"unRealizedProfit": "1"}), 0.0)


# =============================================================================
# 3. Watchdog
# =============================================================================
class TestWatchdogHoldingTime(unittest.TestCase):

    def run_watchdog(self, fake, audit=()):
        with offline(fake) as ws:
            if audit:
                write_audit(ws, *audit)
            return quiet(tdw.audit_dead_alpha, target_env="testnet", max_hours=4.0)

    def test_position_opened_after_last_sync_gets_real_holding_time(self):
        now = int(time.time())
        # No ledger involvement at all: the live position resolves its own entry time from Binance fills.
        fake = FillsExchange([row("BTCUSDT"), row("ETHUSDT")],
                             fills={"BTCUSDT": [fill("BUY", 1, now - 5 * HOUR)],
                                    "ETHUSDT": [fill("BUY", 1, now - 1 * HOUR)]})
        rep = self.run_watchdog(fake)
        by = {p["symbol"]: p for p in rep["positions"]}
        self.assertAlmostEqual(by["BTCUSDT"]["elapsed_hours"], 5.0, delta=0.01)
        self.assertEqual(by["BTCUSDT"]["entry_time_source"], "userTrades")
        self.assertTrue(by["BTCUSDT"]["is_dead_alpha"])
        self.assertEqual(by["BTCUSDT"]["action_taken"], "RECOMMEND_EXIT")
        self.assertAlmostEqual(by["ETHUSDT"]["elapsed_hours"], 1.0, delta=0.01)
        self.assertFalse(by["ETHUSDT"]["is_dead_alpha"])
        self.assertEqual(rep["dead_alpha_count"], 1)
        self.assertEqual(rep["unknown_holding_count"], 0)
        self.assertEqual(fake.writes(), [])

    def test_missing_audit_row_and_user_trades_error_is_unknown_not_zero(self):
        fake = FillsExchange([row("BTCUSDT", update_time=int(time.time() * 1000))],
                             trades_error={"error": "not mapped"})
        rep = self.run_watchdog(fake)
        item = rep["positions"][0]
        self.assertIsNone(item["elapsed_hours"])
        self.assertNotEqual(item["elapsed_hours"], 0.0)
        self.assertEqual(item["entry_time_source"], "UNKNOWN")
        self.assertEqual(item["holding_verdict"], "UNKNOWN")
        self.assertFalse(item["is_dead_alpha"])
        self.assertIn("UNKNOWN", item["warning"])
        self.assertEqual(rep["unknown_holding_count"], 1)
        self.assertEqual(rep["unknown_holding_symbols"], ["BTCUSDT"])

    def test_audit_fallback_when_user_trades_unavailable(self):
        now = int(time.time())
        fake = FillsExchange([row("BTCUSDT")], trades_error={"error": "not mapped"})
        rep = self.run_watchdog(fake, audit=[audit_rec(now - 6 * HOUR)])
        item = rep["positions"][0]
        self.assertEqual(item["entry_time_source"], "trades_audit")
        self.assertAlmostEqual(item["elapsed_hours"], 6.0, delta=0.01)
        self.assertTrue(item["is_dead_alpha"])

    def test_auto_exit_never_closes_unknown(self):
        fake = FillsExchange([row("BTCUSDT")], trades_error={"error": "not mapped"})
        with offline(fake), patch("execute_futures_trade.close_position_market") as mock_close:
            quiet(tdw.audit_dead_alpha, target_env="testnet", auto_exit=True)
        mock_close.assert_not_called()

    def test_auto_exit_never_closes_trades_audit_source(self):
        fake = FillsExchange([row("BTCUSDT")], trades_error={"error": "not mapped"})
        with offline(fake) as ws, patch("execute_futures_trade.close_position_market") as mock_close:
            write_audit(ws, audit_rec(int(time.time()) - 6 * HOUR))
            rep = quiet(tdw.audit_dead_alpha, target_env="testnet", auto_exit=True)
        mock_close.assert_not_called()
        item = rep["positions"][0]
        self.assertTrue(item["is_dead_alpha"])
        self.assertEqual(item["action_taken"], "RECOMMEND_EXIT")
        self.assertIn("report only", item["auto_exit_skipped"])

    def test_auto_exit_closes_user_trades_source(self):
        fake = FillsExchange([row("BTCUSDT")], fills={"BTCUSDT": [fill("BUY", 1, int(time.time()) - 6 * HOUR)]})
        with offline(fake), patch("execute_futures_trade.close_position_market", return_value={"success": True}) as mc:
            rep = quiet(tdw.audit_dead_alpha, target_env="testnet", auto_exit=True)
        mc.assert_called_once_with("BTCUSDT", target_env="testnet")
        self.assertEqual(rep["positions"][0]["action_taken"], "AUTO_EXIT_CLOSED")

    def test_stale_audit_record_with_fresh_manual_position_is_unknown(self):
        now = int(time.time())
        fake = FillsExchange([row("BTCUSDT", entry="120.0", mark="120.5")], trades_error={"error": "not mapped"})
        rep = self.run_watchdog(fake, audit=[audit_rec(now - 30 * HOUR, entry_price=100.0)])
        item = rep["positions"][0]
        self.assertEqual((item["entry_time_source"], item["elapsed_hours"], item["is_dead_alpha"]),
                         ("UNKNOWN", None, False))


# =============================================================================
# 4. sync_session_state entry time
# =============================================================================
class TestSyncEntryTime(unittest.TestCase):

    def run_sync(self, fake, audit=()):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        audit_path = os.path.join(tmp, "trades_audit.jsonl")
        if audit:
            with open(audit_path, "w", encoding="utf-8") as f:
                for r in audit:
                    f.write(json.dumps(r) + "\n")
        with patch.object(sss, "LOGS_DIR", tmp), \
             patch.object(sss, "STATE_FILE", os.path.join(tmp, "session_state.json")), \
             patch.object(sss, "AUDIT_LOG", audit_path), \
             patch.dict(sys.modules, {"shadow_tracker": None}), \
             patch("execute_futures_trade.send_signed_request", side_effect=fake):
            return sss.sync_session_state(target_env="testnet")

    def test_env_alias_is_normalised_in_the_ledger(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with patch.object(sss, "LOGS_DIR", tmp), \
             patch.object(sss, "STATE_FILE", os.path.join(tmp, "session_state.json")), \
             patch.object(sss, "AUDIT_LOG", os.path.join(tmp, "trades_audit.jsonl")), \
             patch.dict(sys.modules, {"shadow_tracker": None}), \
             patch("execute_futures_trade.send_signed_request", side_effect=FillsExchange([])):
            self.assertEqual(sss.sync_session_state(target_env="mainnet")["target_env"], "prod")
            self.assertEqual(sss.sync_session_state(target_env="TESTNET")["target_env"], "testnet")

    def test_load_audit_metadata_normalises_env_aliases(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "trades_audit.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in (audit_rec(1, env="mainnet", sl_price=95.0), audit_rec(2, symbol="ETHUSDT", env="testnet"),
                      audit_rec(3, symbol="XRPUSDT", env="PRODUCTION")):
                f.write(json.dumps(r) + "\n")
        with patch.object(sss, "AUDIT_LOG", path):
            self.assertEqual(sorted(sss.load_audit_metadata("prod")), ["BTCUSDT", "XRPUSDT"])
            self.assertEqual(sss.load_audit_metadata("prod")["BTCUSDT"]["sl_price"], 95.0)
            self.assertEqual(sorted(sss.load_audit_metadata("mainnet")), ["BTCUSDT", "XRPUSDT"])
            self.assertEqual(sorted(sss.load_audit_metadata("testnet")), ["ETHUSDT"])

    def test_no_update_time_or_now_fallback(self):
        recent_ms = int(time.time() * 1000) - 60_000
        state = self.run_sync(FillsExchange([row("BTCUSDT", update_time=recent_ms)], trades_error={"error": "x"}))
        pos = state["active_positions"][0]
        self.assertIsNone(pos["entry_time_ts"])
        self.assertEqual(pos["entry_time_utc"], "UNKNOWN")
        self.assertEqual(pos["entry_time_source"], "UNKNOWN")

    def test_fills_then_audit(self):
        now = int(time.time())
        fake = FillsExchange([row("BTCUSDT"), row("ETHUSDT", amt="-1")],
                             fills={"BTCUSDT": [fill("BUY", 1, now - 3 * HOUR)]})
        state = self.run_sync(fake, audit=[audit_rec(now - 7 * HOUR, symbol="ETHUSDT", direction="SHORT")])
        by = {p["symbol"]: p for p in state["active_positions"]}
        self.assertEqual((by["BTCUSDT"]["entry_time_ts"], by["BTCUSDT"]["entry_time_source"]),
                         (now - 3 * HOUR, "userTrades"))
        self.assertEqual((by["ETHUSDT"]["entry_time_ts"], by["ETHUSDT"]["entry_time_source"]),
                         (now - 7 * HOUR, "trades_audit"))
        per_symbol = [c[2]["symbol"] for c in fake.calls if c[1] == "/fapi/v1/userTrades" and "symbol" in c[2]]
        self.assertEqual(sorted(per_symbol), ["BTCUSDT", "ETHUSDT"])  # one fills query per position per sync


# =============================================================================
# 5. Doctor: fresh ledger before the watchdog
# =============================================================================
class TestDoctorTemporalAudit(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_file = os.path.join(self.tmp, "session_state.json")

    def write_state(self, age_s=0, **extra):
        state = {"is_valid": True, "target_env": "testnet", "last_updated_ts": int(time.time()) - age_s}
        state.update(extra)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)
        t = time.time() - age_s
        os.utime(self.state_file, (t, t))

    def run_doctor(self, sync=None, watchdog=None):
        """Doctor with one protected position (so it reaches the temporal audit); sync and watchdog are mocks."""
        def send(method, endpoint, params=None, target_env=None, retry_count=0):
            if endpoint == "/fapi/v2/balance":
                return [{"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}]
            if endpoint == "/fapi/v2/positionRisk":
                return [row("BTCUSDT")]
            if endpoint == "/fapi/v1/openAlgoOrders":
                return [stop(501, 95.0)]
            return []
        order = MagicMock()
        sync = sync or MagicMock(return_value={"is_valid": True})
        watchdog = watchdog or MagicMock(return_value={"dead_alpha_count": 0, "unknown_holding_symbols": []})
        order.attach_mock(sync, "sync")
        order.attach_mock(watchdog, "watchdog")
        resp = MagicMock()
        resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        out = io.StringIO()
        with patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.get_client_config", return_value=("key12345678", "sec12345678", "http://x")), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("execute_futures_trade.send_signed_request", side_effect=send), \
             patch("user_profile.load_user_profile", return_value=dict(DOCTOR_PROFILE)), \
             patch("trading_doctor.check_pretool_hook", return_value={"ok": True, "critical": [], "warnings": [], "info": []}), \
             patch("shadow_tracker.calculate_efficacy_metrics", side_effect=RuntimeError("skip")), \
             patch.object(sss, "STATE_FILE", self.state_file), \
             patch("sync_session_state.sync_session_state", sync), \
             patch("trading_drift_watchdog.audit_dead_alpha", watchdog), \
             contextlib.redirect_stdout(out):
            mock_urlopen.return_value.__enter__.return_value = resp
            code = trading_doctor.run_doctor(target_env="testnet")
        return code, out.getvalue(), order, sync, watchdog

    def test_missing_ledger_is_synced_before_the_watchdog(self):
        code, out, order, sync, watchdog = self.run_doctor()
        self.assertEqual([c[0] for c in order.mock_calls], ["sync", "watchdog"])
        sync.assert_called_once_with("testnet")
        self.assertEqual(code, 0, out)

    def test_stale_ledger_is_synced_before_the_watchdog(self):
        self.write_state(age_s=301)
        _, _, order, sync, _ = self.run_doctor()
        self.assertEqual([c[0] for c in order.mock_calls], ["sync", "watchdog"])

    def test_invalid_ledger_is_synced(self):
        self.write_state(is_valid=False)
        self.assertTrue(self.run_doctor()[3].called)

    def test_fresh_other_env_ledger_is_not_overwritten(self):
        self.write_state(age_s=10, target_env="prod")
        code, out, _, sync, watchdog = self.run_doctor()
        sync.assert_not_called()
        watchdog.assert_called_once()            # still runs: it reads live positions, not the ledger
        self.assertIn("belongs to PROD", out)
        self.assertIn("not overwritten", out)
        self.assertEqual(code, 0, out)
        self.write_state(age_s=10, target_env="mainnet")   # alias of prod: still the other env
        self.assertFalse(self.run_doctor()[3].called)

    def test_stale_or_invalid_other_env_ledger_is_not_overwritten(self):
        for kw in ({"age_s": 3600}, {"age_s": 10, "is_valid": False}):
            self.write_state(target_env="prod", **kw)
            code, out, _, sync, watchdog = self.run_doctor()
            sync.assert_not_called()
            watchdog.assert_called_once()
            self.assertIn("belongs to PROD", out)
            self.assertEqual(code, 0, out)

    def test_same_env_alias_counts_as_fresh(self):
        self.write_state(age_s=10, target_env="TESTNET")
        self.assertFalse(self.run_doctor()[3].called)

    def test_fresh_ledger_is_not_resynced(self):
        self.write_state(age_s=10)
        _, out, _, sync, watchdog = self.run_doctor()
        sync.assert_not_called()
        watchdog.assert_called_once()
        self.assertIn("GREEN AND HEALTHY", out)

    def test_failed_sync_skips_temporal_audit_with_warning(self):
        for sync in (MagicMock(return_value={"is_valid": False, "error": "positionRisk down"}),
                     MagicMock(side_effect=RuntimeError("boom"))):
            code, out, _, _, watchdog = self.run_doctor(sync=sync)
            watchdog.assert_not_called()
            self.assertIn("Temporal audit skipped: ledger could not be synced", out)
            self.assertNotIn("0.0h", out)
            self.assertEqual(code, 0, out)
            self.assertIn("OPERATIONAL WITH WARNINGS", out)

    def test_watchdog_exception_is_a_named_warning(self):
        self.write_state(age_s=10)
        code, out, _, _, _ = self.run_doctor(watchdog=MagicMock(side_effect=KeyError("entryPrice")))
        self.assertIn("Temporal audit failed: KeyError", out)
        self.assertEqual(code, 0, out)
        self.assertIn("OPERATIONAL WITH WARNINGS", out)

    def test_unknown_holding_is_a_warning(self):
        self.write_state(age_s=10)
        _, out, _, _, _ = self.run_doctor(watchdog=MagicMock(return_value={
            "dead_alpha_count": 0, "unknown_holding_symbols": ["BTCUSDT"]}))
        self.assertIn("Holding time UNKNOWN for ['BTCUSDT']", out)
        self.assertIn("OPERATIONAL WITH WARNINGS", out)


# =============================================================================
# 6. Guardian uses the shared verdict
# =============================================================================
class TestGuardianSharedVerdict(unittest.TestCase):

    def run_guardian(self, fake, stall=STALLED, close=True, audit=()):
        log_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, log_dir, True)
        with offline(fake) as ws, \
             patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
             patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(stall)), \
             patch("execute_futures_trade.close_position_market", return_value={"success": True}) as mock_close:
            if audit:
                write_audit(ws, *audit)
            state = pgl.run_cycle("testnet", close_dead_alpha=close, log_dir=log_dir)
        return state, mock_close

    def fake(self, age_hours=None, mark="100.5", upnl="0.5", **kw):
        fills = {"BTCUSDT": [fill("BUY", 1, int(time.time()) - age_hours * HOUR)]} if age_hours is not None else {}
        return FillsExchange([row("BTCUSDT", mark=mark, upnl=upnl)], fills=fills, algos=[stop(501, 95.0)], **kw)

    def test_unknown_holding_never_closes(self):
        state, mock_close = self.run_guardian(self.fake(None, trades_error={"error": "not mapped"}))
        mock_close.assert_not_called()
        da = state["positions"][0]["dead_alpha"]
        self.assertEqual(da["status"], "UNKNOWN_HOLDING_TIME")
        self.assertEqual(da["holding_verdict"], "UNKNOWN")
        self.assertIsNone(da["elapsed_hours"])

    def test_stall_under_four_hours_never_closes(self):
        state, mock_close = self.run_guardian(self.fake(2))
        mock_close.assert_not_called()
        da = state["positions"][0]["dead_alpha"]
        self.assertEqual(da["status"], "STALLED_WITHIN_HORIZON")
        self.assertEqual(da["entry_time_source"], "userTrades")

    def test_overdue_stagnant_and_stalled_closes_with_flag(self):
        state, mock_close = self.run_guardian(self.fake(5))
        mock_close.assert_called_once_with("BTCUSDT", target_env="testnet")
        da = state["positions"][0]["dead_alpha"]
        self.assertEqual((da["status"], da["holding_verdict"]), ("DEAD_ALPHA_STALLED", "DEAD_ALPHA"))
        state, mock_close = self.run_guardian(self.fake(5), close=False)
        mock_close.assert_not_called()

    def test_no_stall_means_no_holding_lookup(self):
        fake = self.fake(5)
        state, mock_close = self.run_guardian(fake, stall=HEALTHY)
        mock_close.assert_not_called()
        da = state["positions"][0]["dead_alpha"]
        self.assertEqual(da["status"], "HEALTHY_MOMENTUM")
        self.assertNotIn("holding_verdict", da)
        self.assertFalse([c for c in fake.calls if c[1] == "/fapi/v1/userTrades"])

    def test_trades_audit_source_is_report_only(self):
        for dry_run in (False, True):
            fake = self.fake(None, trades_error={"error": "x"})
            log_dir = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, log_dir, True)
            with offline(fake) as ws, \
                 patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
                 patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(STALLED)), \
                 patch("execute_futures_trade.close_position_market", return_value={"success": True}) as mock_close:
                write_audit(ws, audit_rec(int(time.time()) - 6 * HOUR))
                state = pgl.run_cycle("testnet", close_dead_alpha=True, dry_run=dry_run, log_dir=log_dir)
            mock_close.assert_not_called()
            da = state["positions"][0]["dead_alpha"]
            self.assertEqual((da["status"], da["holding_verdict"], da["entry_time_source"]),
                             ("DEAD_ALPHA_STALLED", "DEAD_ALPHA", "trades_audit"))
            self.assertEqual(da["recommendation"], "REVIEW_MANUALLY")
            self.assertIn("report only", da["close_blocked"])
            self.assertFalse([a for a in state["actions"] if a["type"] == "dead_alpha_close"])

    def test_old_record_passing_price_and_size_check_never_auto_closes(self):
        """Reopened at the same price and size: a 3-day-old audit record passes the match, so the verdict is
        DEAD_ALPHA from trades_audit, but neither the guardian nor the watchdog closes on it (report-only)."""
        old = int(time.time()) - 72 * HOUR
        fake = self.fake(None, trades_error={"error": "not mapped"})
        state, mock_close = self.run_guardian(fake, audit=[audit_rec(old, entry_price=100.0, total_qty=1.0)])
        mock_close.assert_not_called()
        da = state["positions"][0]["dead_alpha"]
        self.assertEqual((da["holding_verdict"], da["entry_time_source"]), ("DEAD_ALPHA", "trades_audit"))
        self.assertIn("report only", da["close_blocked"])
        with offline(self.fake(None, trades_error={"error": "not mapped"})) as ws, \
             patch("execute_futures_trade.close_position_market") as mock_wd_close:
            write_audit(ws, audit_rec(old, entry_price=100.0, total_qty=1.0))
            rep = quiet(tdw.audit_dead_alpha, target_env="testnet", auto_exit=True)
        mock_wd_close.assert_not_called()
        self.assertEqual(rep["positions"][0]["action_taken"], "RECOMMEND_EXIT")

    def test_stale_audit_record_with_fresh_manual_position_never_closes(self):
        fake = FillsExchange([row("BTCUSDT", entry="120.0", mark="120.5")], trades_error={"error": "x"},
                             algos=[stop(501, 95.0)])
        state, mock_close = self.run_guardian(fake, audit=[audit_rec(int(time.time()) - 30 * HOUR, entry_price=100.0)])
        mock_close.assert_not_called()
        self.assertEqual(state["positions"][0]["dead_alpha"]["status"], "UNKNOWN_HOLDING_TIME")


class TestDoctorAndGuardianAgree(unittest.TestCase):
    """The doctor's watchdog and the guardian (given a 15m stall) reach the same dead-alpha verdict."""

    SCENARIOS = [  # (age_hours | None, mark, uPnL)
        (5, "100.5", "0.5"),      # overdue + stagnant          -> dead alpha
        (3.5, "100.5", "0.5"),    # within horizon              -> not
        (5, "103.0", "3.0"),      # overdue but moved 3%        -> not
        (5, "100.5", "6.0"),      # overdue, ROE 17.9%          -> not
        (None, "100.5", "0.5"),   # unknown holding             -> not
    ]

    def test_agreement(self):
        for age, mark, upnl in self.SCENARIOS:
            with self.subTest(age=age, mark=mark, upnl=upnl):
                now = int(time.time())
                fills = {"BTCUSDT": [fill("BUY", 1, now - int(age * HOUR))]} if age is not None else {}
                fake = FillsExchange([row("BTCUSDT", mark=mark, upnl=upnl)], fills=fills, algos=[stop(501, 95.0)],
                                     trades_error=None if age is not None else {"error": "x"})
                with offline(fake):
                    wd = quiet(tdw.audit_dead_alpha, target_env="testnet", max_hours=4.0)["positions"][0]
                log_dir = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, log_dir, True)
                with offline(fake), \
                     patch("dynamic_exit_manager.calculate_structural_stop", return_value=None), \
                     patch("dynamic_exit_manager.check_dead_alpha_timeout", return_value=dict(STALLED)), \
                     patch("execute_futures_trade.close_position_market", return_value={"success": True}):
                    gv = pgl.run_cycle("testnet", log_dir=log_dir)["positions"][0]["dead_alpha"]
                self.assertEqual(wd["is_dead_alpha"], gv["status"] == "DEAD_ALPHA_STALLED")
                self.assertEqual(wd["holding_verdict"], gv["holding_verdict"])
                self.assertEqual(wd["entry_time_source"], gv["entry_time_source"])
                if wd["elapsed_hours"] is None:
                    self.assertIsNone(gv["elapsed_hours"])
                else:
                    self.assertAlmostEqual(wd["elapsed_hours"], gv["elapsed_hours"], delta=0.01)
        self.assertEqual(pt.DEFAULT_MAX_HOURS, 4.0)


if __name__ == "__main__":
    unittest.main()
