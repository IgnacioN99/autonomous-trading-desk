#!/usr/bin/env python3
"""
test_issue_312_shadow_resolution.py - shadow rows are resolved on a schedule (issue #312).

Covers the bounded audit (time budget with a fake clock, row cap, oldest-first, no duplicates or losses), the
non-blocking audit lock, rows appended during an audit, the logs/shadow_state.json heartbeat (also when nothing
resolved and when the pass fails), late audits on historical klines (same windows as an on-time audit), the ACTIVE
re-audit from its activation bar, the kline-failure counter and its no_klines expiry, the fallback_24h flag, the
guardian's bounded step (real logs dir only, never in --dry-run, never raises, after the unknown-entries check), the
doctor's staleness WARN, the analytics freshness line / JSON key and the R totals.
Hermetic: every shadow_tracker path redirected to a temp dir (TrackerBase), klines mocked, urlopen blocked, the
Binance client faked where a cycle or the doctor runs; no .env read.
"""

import io
import os
import sys
import json
import time
import tempfile
import unittest
import contextlib
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "loops"), TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import shadow_tracker as st
import shadow_analytics as sa
import sync_session_state as sss
import trading_doctor
import position_guardian_loop as pgl
import test_issue_251_delta_gate_regret as t251
from test_report_issue import summary_state

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

BAR = 300


def bar(open_s, high, low, close, open_=None):
    return [int(open_s) * 1000, str(open_ if open_ is not None else close), str(high), str(low), str(close), "100"]


def grid(ts):
    return int(ts) // BAR * BAR


class FakeTime:
    """Stands in for shadow_tracker's time module: real wall clock, controllable monotonic clock."""

    def __init__(self):
        self.clock = 1000.0

    def time(self):
        return time.time()

    def monotonic(self):
        return self.clock


class Market:
    """klines per symbol from a price path relative to the row's registration (honours start_ms and limit, no bar
    after now). trigger_at / tp_at / sl_at: offsets in seconds (multiples of 300) from the registration."""

    def __init__(self):
        self.paths = {}
        self.calls = []

    def add(self, symbol, reg, trigger_at=None, tp_at=None, sl_at=None):
        self.paths[symbol] = (reg, trigger_at, tp_at, sl_at)

    def price(self, symbol, open_s):
        reg, trigger_at, tp_at, sl_at = self.paths[symbol]
        off = open_s - reg
        if trigger_at is not None and off >= trigger_at:
            high, low, close = 10.25, 10.15, 10.2
        else:
            high, low, close = 10.05, 9.95, 10.0
        if tp_at is not None and off == tp_at:
            high = 11.0
        if sl_at is not None and off == sl_at:
            low = 9.4
        return bar(open_s, high, low, close)

    def __call__(self, symbol, start_ms, interval="5m", limit=500, **kwargs):
        self.calls.append((symbol, start_ms, kwargs))
        reg = self.paths[symbol][0]
        start = int(start_ms) // 1000
        first = reg - BAR + ((max(start, reg - BAR) - (reg - BAR) + BAR - 1) // BAR) * BAR
        now = time.time()
        out = []
        t = first
        while len(out) < limit and t + BAR <= now:
            out.append(self.price(symbol, t))
            t += BAR
        return out


def register(symbol, reg, direction="LONG", trigger=10.2, sl=9.5, tp1=10.9, **kw):
    return st.register_shadow_trade(symbol=symbol, direction=direction, trigger_price=trigger, sl_price=sl,
                                    tp1_price=tp1, tp2_price=12.0, current_price=10.0, registered_at_ts=int(reg), **kw)


class Base(t251.TrackerBase):

    def resolved(self):
        return {r["symbol"]: r for r in st.load_jsonl(st.SHADOW_RESOLVED_FILE)}

    def state(self):
        with open(st.shadow_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)


# =============================================================================
# 1. Bounded pass: budget, row cap, oldest-first, no duplicates or losses
# =============================================================================
class TestBoundedPass(Base):

    def test_budget_stops_the_pass_and_keeps_unprocessed_rows(self):
        now = grid(time.time())
        regs = {"AAAUSDT": now - 4 * 3600, "BBBUSDT": now - 3 * 3600, "CCCUSDT": now - 2 * 3600,
                "DDDUSDT": now - 3600}
        for sym in ("DDDUSDT", "CCCUSDT", "BBBUSDT", "AAAUSDT"):  # file order: newest first
            register(sym, regs[sym])
        before = st.load_jsonl(st.SHADOW_TRADES_FILE)
        clock = FakeTime()
        calls = []

        def fetch(symbol, start_ms, interval="5m", limit=500, **kw):
            calls.append((symbol, kw.get("deadline")))
            clock.clock += 2.0
            return [bar(regs[symbol], 10.05, 9.95, 10.0)]  # nothing concludes

        with patch.object(st, "time", clock), patch.object(st, "fetch_klines", side_effect=fetch):
            res = st.audit_shadow_trades(budget_s=5)
        self.assertEqual([c[0] for c in calls], ["AAAUSDT", "BBBUSDT", "CCCUSDT"])  # oldest-first
        self.assertTrue(all(c[1] == 1005.0 for c in calls))  # the deadline reaches every klines read
        self.assertEqual((res["processed"], res["partial"], res["newly_resolved"]), (3, True, 0))
        after = st.load_jsonl(st.SHADOW_TRADES_FILE)
        self.assertEqual([r["symbol"] for r in after], [r["symbol"] for r in before])  # nothing lost, same order
        self.assertEqual(after[0], before[0])  # DDDUSDT not reached: unchanged
        self.assertTrue(self.state()["last_audit_partial"])
        self.assertEqual(self.state()["last_audit_remaining"], 4)

    def test_zero_budget_processes_nothing(self):
        register("AAAUSDT", grid(time.time()) - 3600)
        with patch.object(st, "fetch_klines") as fetch:
            res = st.audit_shadow_trades(budget_s=0)
        fetch.assert_not_called()
        self.assertEqual((res["processed"], res["partial"], res["active_remaining"]), (0, True, 1))

    def test_spent_deadline_during_a_read_is_not_a_kline_failure(self):
        register("AAAUSDT", grid(time.time()) - 3600)
        clock = FakeTime()

        def fetch(*a, **kw):
            clock.clock += 10.0
            return []

        with patch.object(st, "time", clock), patch.object(st, "fetch_klines", side_effect=fetch):
            res = st.audit_shadow_trades(budget_s=5)
        self.assertTrue(res["partial"])
        row = st.load_jsonl(st.SHADOW_TRADES_FILE)[0]
        self.assertNotIn("kline_failures", row)

    def test_row_cap_resolves_in_batches_without_duplicates(self):
        now = grid(time.time())
        market = Market()
        for i, sym in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT")):
            reg = now - (10 - i) * 3600
            register(sym, reg)
            market.add(sym, reg, trigger_at=1800, tp_at=3600)
        with patch.object(st, "fetch_klines", side_effect=market):
            first = st.audit_shadow_trades(max_rows=2)
            self.assertEqual((first["newly_resolved"], first["partial"]), (2, True))
            self.assertEqual(set(self.resolved()), {"AAAUSDT", "BBBUSDT"})
            self.assertEqual([r["symbol"] for r in st.load_jsonl(st.SHADOW_TRADES_FILE)], ["CCCUSDT"])
            second = st.audit_shadow_trades(max_rows=2)
        self.assertEqual((second["newly_resolved"], second["partial"], second["active_remaining"]), (1, False, 0))
        ids = [r["id"] for r in st.load_jsonl(st.SHADOW_RESOLVED_FILE)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 3)
        self.assertEqual(st.load_jsonl(st.SHADOW_TRADES_FILE), [])

    def test_a_failing_row_is_kept_unchanged_and_the_pass_goes_on(self):
        now = grid(time.time())
        market = Market()
        register("AAAUSDT", now - 5 * 3600)
        register("BBBUSDT", now - 4 * 3600)
        market.add("BBBUSDT", now - 4 * 3600, trigger_at=1800, tp_at=3600)
        rows = st.load_jsonl(st.SHADOW_TRADES_FILE)
        rows[0]["trigger_price"] = "not a number"
        t251.write_jsonl(st.SHADOW_TRADES_FILE, rows)

        def fetch(symbol, *a, **kw):
            return [bar(now - 5 * 3600, 10.5, 9.0, 10.0)] if symbol == "AAAUSDT" else market(symbol, *a, **kw)

        with patch.object(st, "fetch_klines", side_effect=fetch):
            res = st.audit_shadow_trades()
        self.assertEqual(res["newly_resolved"], 1)
        self.assertEqual(st.load_jsonl(st.SHADOW_TRADES_FILE), [rows[0]])


# =============================================================================
# 2. Lock, mid-audit appends, heartbeat
# =============================================================================
class TestLockAppendHeartbeat(Base):

    @unittest.skipIf(fcntl is None, "flock is POSIX")
    def test_held_lock_skips_without_blocking(self):
        register("AAAUSDT", grid(time.time()) - 3600)
        fh = open(os.path.join(self.dir, st.SHADOW_AUDIT_LOCK_FILE_NAME), "a+")
        self.addCleanup(fh.close)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(st, "fetch_klines") as fetch:
            started = time.monotonic()
            res = st.audit_shadow_trades()
            self.assertLess(time.monotonic() - started, 1.0)
            fetch.assert_not_called()
            self.assertEqual(res["skipped"], "lock_held")
            self.assertFalse(os.path.exists(st.shadow_state_path()))  # a skip is not an audit
            out = io.StringIO()
            with patch.object(sys, "argv", ["shadow_tracker.py", "--audit"]), contextlib.redirect_stdout(out):
                st.main()  # returns: exit 0
        self.assertIn(st.LOCK_HELD_LINE, out.getvalue())
        self.assertNotIn("SHADOW DESK", out.getvalue())
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        with patch.object(st, "fetch_klines", return_value=[]):
            self.assertIsNone(st.audit_shadow_trades()["skipped"])

    def test_row_registered_during_the_audit_is_kept(self):
        now = grid(time.time())
        market = Market()
        register("AAAUSDT", now - 5 * 3600)
        market.add("AAAUSDT", now - 5 * 3600, trigger_at=1800, tp_at=3600)

        def fetch(*a, **kw):
            register("NEWUSDT", now - 60)  # record_evaluation.py appends while the audit runs
            return market(*a, **kw)

        with patch.object(st, "fetch_klines", side_effect=fetch):
            res = st.audit_shadow_trades()
        self.assertEqual(res["newly_resolved"], 1)
        self.assertEqual([r["symbol"] for r in st.load_jsonl(st.SHADOW_TRADES_FILE)], ["NEWUSDT"])
        self.assertEqual(res["active_remaining"], 1)

    def test_same_symbol_both_directions_same_second_are_distinct(self):
        reg = grid(time.time()) - 600
        a = register("AAAUSDT", reg, dossier_sha256="a" * 64)
        b = register("AAAUSDT", reg, direction="SHORT", trigger=9.8, sl=10.5, tp1=9.1, dossier_sha256="a" * 64)
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(st._row_identity(a), st._row_identity(b))

    def test_heartbeat_written_when_nothing_resolves(self):
        register("AAAUSDT", grid(time.time()) - 3600)
        with patch.object(st, "fetch_klines", return_value=[bar(grid(time.time()) - 3600, 10.05, 9.95, 10.0)]):
            st.audit_shadow_trades()
        state = self.state()
        self.assertTrue(set(state).issuperset({"last_audit_ts", "last_audit_duration_s", "last_audit_resolved",
                                               "last_audit_remaining", "last_audit_partial"}))
        self.assertEqual((state["last_audit_resolved"], state["last_audit_remaining"], state["last_audit_partial"]),
                         (0, 1, False))
        self.assertLessEqual(abs(state["last_audit_ts"] - time.time()), 5)

    def test_heartbeat_written_with_an_empty_ledger(self):
        res = st.audit_shadow_trades()
        self.assertEqual((res["newly_resolved"], res["active_remaining"]), (0, 0))
        self.assertIsNone(self.state()["last_audit_error"])
        self.assertFalse(os.path.exists(st.SHADOW_TRADES_FILE))

    def test_failing_pass_is_fail_open_and_recorded(self):
        register("AAAUSDT", grid(time.time()) - 3600)
        with patch.object(st, "_audit_pass", side_effect=OSError("disk full")):
            res = st.audit_shadow_trades()
        self.assertIn("disk full", res["error"])
        self.assertIn("disk full", self.state()["last_audit_error"])
        with patch.object(st, "atomic_write_json", side_effect=OSError("ro")), \
                patch.object(st, "fetch_klines", return_value=[]):
            st.audit_shadow_trades()  # heartbeat write fails: still no exception

    def test_min_interval_skips_a_recent_audit(self):
        register("AAAUSDT", grid(time.time()) - 3600)
        t251.write_json(st.shadow_state_path(), {"last_audit_ts": int(time.time()) - 30})
        with patch.object(st, "fetch_klines") as fetch:
            self.assertEqual(st.audit_shadow_trades(min_interval_s=120)["skipped"], "min_interval")
        fetch.assert_not_called()
        t251.write_json(st.shadow_state_path(), {"last_audit_ts": int(time.time()) - 300})
        with patch.object(st, "fetch_klines", return_value=[]) as fetch:
            self.assertIsNone(st.audit_shadow_trades(min_interval_s=120)["skipped"])
        fetch.assert_called_once()

    def test_fetch_klines_never_outlives_the_deadline(self):
        opener = MagicMock(side_effect=AssertionError("network"))
        with patch("urllib.request.urlopen", opener):
            self.assertEqual(st.fetch_klines("AAAUSDT", 0, deadline=time.monotonic() - 1), [])
        opener.assert_not_called()


# =============================================================================
# 3. Late resolution on historical klines
# =============================================================================
class TestLateResolution(Base):

    def audit(self, market):
        with patch.object(st, "fetch_klines", side_effect=market):
            return st.audit_shadow_trades()

    def assert_same_windows(self, late, on_time):
        for key in ("outcome", "classification", "simulated_pnl_usdt", "resolution_basis"):
            self.assertEqual(late[key], on_time[key], key)
        for row in (late, on_time):
            self.assertEqual(row["audit_lag_s"], row["resolved_at_ts"] - row["resolved_bar_ts"])
        self.assertEqual(late["resolved_bar_ts"] - late["registered_at_ts"],
                         on_time["resolved_bar_ts"] - on_time["registered_at_ts"])

    def test_three_day_old_tp1_matches_an_on_time_audit(self):
        now = grid(time.time())
        market = Market()
        for sym, reg in (("OLDUSDT", now - 3 * 86400), ("NOWUSDT", now - 3 * 3600)):
            register(sym, reg)
            market.add(sym, reg, trigger_at=1800, tp_at=7200)
        self.audit(market)
        rows = self.resolved()
        self.assert_same_windows(rows["OLDUSDT"], rows["NOWUSDT"])
        old = rows["OLDUSDT"]
        self.assertEqual((old["outcome"], old["resolution_basis"]), ("TP1_HIT", "klines"))
        self.assertEqual(old["activated_at_ts"] - old["registered_at_ts"], 1800)
        self.assertEqual(old["resolved_bar_ts"] - old["registered_at_ts"], 7200)
        self.assertGreater(old["audit_lag_s"], 2 * 86400)
        self.assertEqual(market.calls[0][1], (old["registered_at_ts"] - 300) * 1000)

    def test_three_day_old_timeout_and_untriggered_keep_their_windows(self):
        now = grid(time.time())
        market = Market()
        for sym, reg, trig in (("TOOLD", now - 3 * 86400, 1800), ("TONOW", now - 5 * 3600, 1800),
                               ("UNOLD", now - 3 * 86400, None), ("UNNOW", now - 2 * 3600, None)):
            register(sym, reg)
            market.add(sym, reg, trigger_at=trig)
        self.audit(market)
        rows = self.resolved()
        self.assert_same_windows(rows["TOOLD"], rows["TONOW"])
        self.assertEqual(rows["TOOLD"]["classification"], "TIMEOUT_CLOSED")
        self.assertEqual(rows["TOOLD"]["resolved_bar_ts"] - rows["TOOLD"]["activated_at_ts"], 14400)
        self.assert_same_windows(rows["UNOLD"], rows["UNNOW"])
        self.assertEqual(rows["UNOLD"]["outcome"], "EXPIRED_UNTRIGGERED")
        self.assertEqual(rows["UNOLD"]["resolution_basis"], "klines")

    def test_unconcluded_replay_after_24h_is_flagged_fallback(self):
        reg = grid(time.time()) - 2 * 86400
        register("TRUNCUSDT", reg)
        with patch.object(st, "fetch_klines", return_value=[bar(reg, 10.05, 9.95, 10.0)]):
            st.audit_shadow_trades()
        row = self.resolved()["TRUNCUSDT"]
        self.assertEqual((row["classification"], row["resolution_basis"]), ("EXPIRED", "fallback_24h"))
        self.assertIsNone(row["resolved_bar_ts"])
        self.assertIsNone(row["audit_lag_s"])

    def test_active_reaudit_ignores_pre_activation_bars(self):
        reg = grid(time.time()) - 3600
        register("ACTUSDT", reg)
        rows = st.load_jsonl(st.SHADOW_TRADES_FILE)
        rows[0].update(status="ACTIVE", activated_at_ts=reg + 1200, highest_price=10.6, lowest_price=10.1)
        t251.write_jsonl(st.SHADOW_TRADES_FILE, rows)
        calls = []

        def fetch(symbol, start_ms, interval="5m", limit=500, **kw):  # ignores start_ms on purpose
            calls.append(start_ms)
            return [bar(reg, 10.05, 9.0, 10.0),            # pre-activation: would hit the SL at 9.5
                    bar(reg + 1200, 10.3, 10.15, 10.2),     # activation bar
                    bar(reg + 1500, 10.4, 10.2, 10.3)]

        with patch.object(st, "fetch_klines", side_effect=fetch):
            res = st.audit_shadow_trades()
        self.assertEqual(calls, [(reg + 1200) * 1000])
        self.assertEqual(res["newly_resolved"], 0)
        row = st.load_jsonl(st.SHADOW_TRADES_FILE)[0]
        self.assertEqual(row["status"], "ACTIVE")
        self.assertEqual((row["highest_price"], row["lowest_price"]), (10.6, 10.1))

        def fetch_tp(symbol, start_ms, interval="5m", limit=500, **kw):
            return fetch(symbol, start_ms) + [bar(reg + 1800, 11.0, 10.3, 10.9)]

        with patch.object(st, "fetch_klines", side_effect=fetch_tp):
            st.audit_shadow_trades()
        row = self.resolved()["ACTUSDT"]
        self.assertEqual((row["outcome"], row["resolved_bar_ts"]), ("TP1_HIT", reg + 1800))

    def test_empty_or_raising_klines_count_failures_fail_open(self):
        reg = grid(time.time()) - 3600
        register("EMPTYUSDT", reg)
        register("RAISEUSDT", reg + 300)

        def fetch(symbol, *a, **kw):
            if symbol == "RAISEUSDT":
                raise RuntimeError("HTTP 418")
            return []

        with patch.object(st, "fetch_klines", side_effect=fetch):
            res = st.audit_shadow_trades()
            self.assertEqual(res["newly_resolved"], 0)
            rows = self.rows()
            self.assertEqual((rows["EMPTYUSDT"]["kline_failures"], rows["RAISEUSDT"]["kline_failures"]), (1, 1))
            st.audit_shadow_trades()  # within the hour: not counted again
        rows = self.rows()
        self.assertEqual(rows["EMPTYUSDT"]["kline_failures"], 1)

    def test_kline_failure_cap_expires_only_old_rows(self):
        now = int(time.time())
        register("DELISTUSDT", now - 4 * 86400)
        register("YOUNGUSDT", now - 2 * 86400)
        rows = st.load_jsonl(st.SHADOW_TRADES_FILE)
        for r in rows:
            r.update(kline_failures=4, last_kline_failure_ts=now - 7200)
        t251.write_jsonl(st.SHADOW_TRADES_FILE, rows)
        with patch.object(st, "fetch_klines", return_value=[]):
            st.audit_shadow_trades()
        row = self.resolved()["DELISTUSDT"]
        self.assertEqual((row["classification"], row["resolution_basis"], row["simulated_pnl_usdt"]),
                         ("EXPIRED", "no_klines", 0.0))
        left = self.rows()
        self.assertEqual(list(left), ["YOUNGUSDT"])
        self.assertEqual(left["YOUNGUSDT"]["kline_failures"], 5)

    def test_failed_rows_go_behind_the_others(self):
        now = int(time.time())
        register("FAILEDUSDT", now - 9000)
        register("FRESHUSDT", now - 3600)
        rows = st.load_jsonl(st.SHADOW_TRADES_FILE)
        rows[0].update(kline_failures=1, last_kline_failure_ts=now - 7200)
        t251.write_jsonl(st.SHADOW_TRADES_FILE, rows)
        with patch.object(st, "fetch_klines", return_value=[]) as fetch:
            st.audit_shadow_trades(max_rows=1)
        self.assertEqual(fetch.call_args[0][0], "FRESHUSDT")


# =============================================================================
# 4. Guardian step
# =============================================================================
class TestGuardianStep(Base):

    def cycle(self, log_dir=None, dry_run=False):
        return pgl.GuardianCycle("prod", dry_run=dry_run, log_dir=log_dir or self.dir)

    def test_runs_bounded_for_the_shadow_logs_dir(self):
        with patch.object(st, "audit_shadow_trades") as audit:
            self.cycle()._shadow_audit()
        audit.assert_called_once_with(budget_s=pgl.SHADOW_AUDIT_BUDGET_SECONDS, max_rows=pgl.SHADOW_AUDIT_MAX_ROWS,
                                      min_interval_s=pgl.SHADOW_AUDIT_MIN_INTERVAL_S)
        self.assertLessEqual(pgl.SHADOW_AUDIT_BUDGET_SECONDS, 5)
        self.assertEqual(pgl.SHADOW_AUDIT_MIN_INTERVAL_S, 120)

    def test_skipped_for_a_foreign_log_dir_and_dry_run(self):
        with patch.object(st, "audit_shadow_trades") as audit:
            self.cycle(log_dir=tempfile.mkdtemp())._shadow_audit()
            self.cycle(dry_run=True)._shadow_audit()
        audit.assert_not_called()

    def test_never_raises(self):
        err = io.StringIO()
        with patch.object(st, "audit_shadow_trades", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            self.cycle()._shadow_audit()
        self.assertIn("shadow audit skipped", err.getvalue())
        with patch.dict(sys.modules, {"shadow_tracker": None}), contextlib.redirect_stderr(io.StringIO()):
            self.cycle()._shadow_audit()

    def test_real_audit_respects_min_interval_and_budget(self):
        now = grid(time.time())
        for i in range(4):
            register(f"R{i}USDT", now - (8 - i) * 3600)
        clock = FakeTime()

        def fetch(symbol, *a, **kw):
            clock.clock += 3.0
            return []

        with patch.object(st, "time", clock), patch.object(st, "fetch_klines", side_effect=fetch) as f:
            self.cycle()._shadow_audit()
            self.assertEqual(f.call_count, 2)  # 0 s and 3 s < 5 s budget, then the budget is spent
            self.cycle()._shadow_audit()       # heartbeat younger than SHADOW_AUDIT_MIN_INTERVAL_S
            self.assertEqual(f.call_count, 2)
        self.assertTrue(self.state()["last_audit_partial"])

    def test_cycle_runs_it_after_the_unknown_entries_check(self):
        cycle = self.cycle()
        order = []
        with patch.object(cycle, "_load_previous_state", return_value={}), \
                patch.object(cycle, "_protect_pending"), \
                patch.object(cycle, "_check_unknown_entries", side_effect=lambda: order.append("unknown")), \
                patch.object(cycle, "_shadow_audit", side_effect=lambda: order.append("shadow")), \
                patch.object(cycle, "finish", side_effect=lambda: order.append("finish")), \
                patch("execute_futures_trade.send_signed_request", return_value=[]):
            cycle._run()
        self.assertEqual(order, ["unknown", "shadow", "finish"])


# =============================================================================
# 5. Doctor staleness WARN
# =============================================================================
class TestDoctor(Base):

    def test_no_rows_no_warning(self):
        line, warnings = trading_doctor.shadow_desk_status()
        self.assertEqual(warnings, [])
        self.assertIn("Last audit: never", line)
        self.assertIn("see R", line)

    def test_rows_inside_their_window_do_not_warn(self):
        register("AAAUSDT", time.time() - 2 * 3600)
        self.assertEqual(trading_doctor.shadow_desk_status()[1], [])

    def test_overdue_row_without_audit_or_resolved_file_warns(self):
        register("AAAUSDT", time.time() - 8 * 3600)
        line, warnings = trading_doctor.shadow_desk_status()
        self.assertEqual(len(warnings), 1)
        self.assertIn("1 shadow row(s) past their window", warnings[0])
        self.assertIn("shadow_resolved.jsonl missing", warnings[0])
        self.assertIn("no shadow audit heartbeat", warnings[0])
        self.assertIn("shadow_tracker.py --audit", warnings[0])

    def test_overdue_row_with_fresh_audit_names_only_the_row(self):
        register("AAAUSDT", time.time() - 8 * 3600)
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, [])
        t251.write_json(st.shadow_state_path(), {"last_audit_ts": int(time.time()) - 600})
        line, warnings = trading_doctor.shadow_desk_status()
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("shadow_resolved.jsonl", warnings[0])
        self.assertNotIn("heartbeat", warnings[0])
        self.assertIn("Last audit: 10m ago", line)

    def test_stale_resolved_file_and_audit_are_named(self):
        register("AAAUSDT", time.time() - 8 * 3600)
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, [])
        old = time.time() - 13 * 3600
        os.utime(st.SHADOW_RESOLVED_FILE, (old, old))
        t251.write_json(st.shadow_state_path(), {"last_audit_ts": int(time.time()) - 7 * 3600})
        warnings = trading_doctor.shadow_desk_status()[1]
        self.assertIn("shadow_resolved.jsonl last written 13.0h ago", warnings[0])
        self.assertIn("last shadow audit 7.0h ago", warnings[0])

    def _run_doctor(self, **patches):
        ws = tempfile.mkdtemp()
        resp = MagicMock()
        resp.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        profile = {"profile_completed": True, "risk_pct_equity": 0.005, "yolo_slot_enabled": False}
        with patch("execute_futures_trade.load_env", return_value={"LIVE_TRADING_ARMED": "true"}), \
             patch("execute_futures_trade.get_client_config", return_value=("key12345678", "sec12345678", "http://x")), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("execute_futures_trade.send_signed_request",
                   side_effect=lambda method, endpoint, params=None, target_env=None, **kw: [
                       {"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}]
                   if endpoint == "/fapi/v2/balance" else []), \
             patch("user_profile.load_user_profile", return_value=profile), \
             patch("trading_doctor.check_pretool_hook", return_value={"ok": True, "critical": [], "warnings": [], "info": []}), \
             patch("utils.dossier_provenance._is_wsl", return_value=True), \
             patch.object(sss, "STATE_FILE", os.path.join(ws, "session_state.json")), \
             patch("sync_session_state.sync_session_state", MagicMock(return_value={"is_valid": True})), \
             patch("trading_doctor.check_guardian_service", side_effect=RuntimeError("boom")), \
             contextlib.ExitStack() as stack:
            for target, value in patches.items():
                stack.enter_context(patch(target, value))
            mock_urlopen.return_value.__enter__.return_value = resp
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = trading_doctor.run_doctor(target_env="testnet")
        return code, out.getvalue()

    def test_doctor_lists_the_warning_and_stays_non_critical(self):
        code, out = self._run_doctor(**{"trading_doctor.shadow_desk_status":
                                        MagicMock(return_value=("line", ["Shadow desk audit stale: x"]))})
        self.assertEqual(code, 0, out)
        self.assertIn("⚠️  [SHADOW DESK] Shadow desk audit stale: x", out)
        self.assertIn("▲ Shadow desk audit stale: x", out)
        code, out = self._run_doctor(**{"trading_doctor.shadow_desk_status": MagicMock(side_effect=OSError("io"))})
        self.assertEqual(code, 0, out)
        self.assertIn("[SHADOW DESK] Shadow desk status unavailable (OSError).", out)


# =============================================================================
# 6. Analytics freshness and R totals
# =============================================================================
class TestAnalyticsAndR(Base):

    def run_analytics(self, argv):
        logs = tempfile.mkdtemp()
        path = os.path.join(logs, "shadow_resolved.jsonl")
        t251.write_jsonl(path, [dict(id="r1", symbol="AAAUSDT", direction="LONG", classification="TRUE_NEGATIVE",
                                     simulated_pnl_usdt=-1.5, target_dollar_risk=1.5, vol_ratio=0.5,
                                     max_favorable_excursion_pct=0.1, max_adverse_excursion_pct=-1.0,
                                     activated_at_ts=1000, resolved_at_ts=99999, resolved_bar_ts=4600,
                                     rejection_reason="x")])
        out = io.StringIO()
        with patch.object(sa, "RESOLVED_FILE", path), patch.object(sa, "LOGS_DIR", logs), \
                contextlib.redirect_stdout(out):
            sa.main(argv)
        return logs, out.getvalue()

    def test_freshness_line_never_and_json_null(self):
        _logs, text = self.run_analytics(["--resamples", "5"])
        self.assertIn("Last shadow audit: never", text)
        self.assertIn("Largest target_dollar_risk rows", text)
        _logs, raw = self.run_analytics(["--json", "--resamples", "5"])
        self.assertIsNone(json.loads(raw)["last_audit_ts"])

    def test_freshness_line_with_heartbeat(self):
        ts = int(time.time()) - 2 * 3600 - 60
        self.assertTrue(sa.freshness_line(ts, now=ts + 7260).endswith(" (2h 1m ago)"))
        logs = tempfile.mkdtemp()
        t251.write_json(os.path.join(logs, "shadow_state.json"), {"last_audit_ts": ts})
        self.assertEqual(sa.last_audit_ts(logs), ts)
        self.assertIn("UTC (2h 1m ago)", sa.freshness_line(sa.last_audit_ts(logs), now=ts + 7260))

    def test_hygiene_uses_the_resolving_bar(self):
        row = {"classification": "TRUE_NEGATIVE", "simulated_pnl_usdt": -1.5, "activated_at_ts": 1000,
               "resolved_at_ts": 1000 + 3 * 86400}
        self.assertEqual(sa.run_intraday_hygiene_audit([row])["drift_trades"], 1)  # old row: as before
        self.assertEqual(sa.run_intraday_hygiene_audit([dict(row, resolved_bar_ts=4600)])["intraday_trades"], 1)

    def test_r_totals_and_largest_rows(self):
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, [
            {"id": "a", "symbol": "A", "classification": "TRUE_NEGATIVE", "simulated_pnl_usdt": -3.0,
             "target_dollar_risk": 3.0},
            {"id": "b", "symbol": "B", "classification": "TRUE_NEGATIVE", "simulated_pnl_usdt": -1.5,
             "target_dollar_risk": 1.5},
            {"id": "c", "symbol": "C", "classification": "FALSE_NEGATIVE", "simulated_pnl_usdt": 27.0,
             "target_dollar_risk": 15.0},
            {"id": "d", "symbol": "D", "classification": "FALSE_NEGATIVE", "simulated_pnl_usdt": 2.7},
        ])
        rows = [dict(r, direction="LONG", outcome="X", rejection_reason="y")
                for r in st.load_jsonl(st.SHADOW_RESOLVED_FILE)]  # fields the dashboard prints
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, rows)
        m = st.calculate_efficacy_metrics()
        self.assertEqual((m["missed_alpha_usdt"], m["net_filter_edge_usdt"]), (29.7, -25.2))
        self.assertEqual((m["capital_saved_r"], m["missed_alpha_r"], m["net_filter_edge_r"], m["r_rows_skipped"]),
                         (2.0, 1.8, 0.2, 1))
        self.assertEqual([r["id"] for r in m["largest_risk_rows"]], ["c", "a", "b"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            st.print_shadow_dashboard()
        self.assertIn("mixed row sizes, see R", out.getvalue())
        self.assertIn("Net +0.2R", out.getvalue())

    def test_intraday_filter_uses_the_resolving_bar(self):
        now = int(time.time())
        row = {"id": "x", "symbol": "X", "classification": "TRUE_NEGATIVE", "simulated_pnl_usdt": -1.5,
               "target_dollar_risk": 1.5, "activated_at_ts": now - 30 * 3600, "resolved_at_ts": now}
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, [row])
        self.assertEqual(st.calculate_efficacy_metrics()["intraday_conclusive_count"], 0)  # old row: as before
        t251.write_jsonl(st.SHADOW_RESOLVED_FILE, [dict(row, resolved_bar_ts=now - 29 * 3600)])
        self.assertEqual(st.calculate_efficacy_metrics()["intraday_conclusive_count"], 1)

    def test_sync_summary_prints_r(self):
        state = summary_state()
        state["shadow_desk_summary"].update(capital_saved_r=2.0, missed_alpha_r=1.8, net_filter_edge_r=0.2,
                                            r_rows_skipped=1)
        text = sss.format_markdown_summary(state)
        self.assertIn("In R (USDT above mixes row sizes):** Saved +2.0R | Missed -1.8R | Net +0.2R", text)
        self.assertNotIn("In R", sss.format_markdown_summary(summary_state()))


if __name__ == "__main__":
    unittest.main()
