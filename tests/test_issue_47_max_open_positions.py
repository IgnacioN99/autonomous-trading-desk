#!/usr/bin/env python3
"""
test_issue_47_max_open_positions.py - Offline tests for Issue #47:
Gate 0A max_open_positions slot reconciliation with logs/trades_audit.jsonl.

Covers:
a) Pending fill gap: pending entry filled and dropped, audit log written,
   session_state not yet synced -> new entry blocked.
b) MARKET fill gap: MARKET order filled, audit log written,
   session_state not yet synced -> new entry blocked.
c) Sync handoff deduplication: once session_state is updated with the position
   and a newer timestamp, count is not duplicated.
d) Env isolation: testnet audit records do not affect prod, and vice-versa.
e) Failsafe abort: aborted records (CRITICAL_FAILSAFE_ABORT) do not consume slots.
f) Fallback 300s window when session_state timestamp <= 0.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft

GATE_PREFIX = "MECHANICAL HARD GATE REJECTION: Max open positions limit"


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def flat_exchange(method, endpoint, params=None, target_env=None, retry_count=0):
    """Live PROD gate snapshot (issue #101): no position, no resting order; any other call is a test bug."""
    if method == "GET" and not params and endpoint in ("/fapi/v2/positionRisk", "/fapi/v1/openAlgoOrders",
                                                       "/fapi/v1/openOrders"):
        return []
    raise AssertionError(f"unexpected exchange call {method} {endpoint} {params}")


def write_session_state(ws, symbols, last_updated_ts=None):
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    if last_updated_ts is None:
        last_updated_ts = int(time.time())
    state = {
        "is_valid": True,
        "last_updated_ts": int(last_updated_ts),
        "portfolio_exposure": {
            "delta_bias": "NEUTRAL",
            "total_active_positions": len(symbols)
        },
        "active_positions": [{"symbol": s} for s in symbols],
    }
    with open(os.path.join(ws, "logs", "session_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f)


def write_pending_registry(ws, *records):
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    entries = {}
    for r in records:
        key = f"{r.get('target_env')}:{str(r.get('symbol')).upper()}:{r.get('entry_id', '1')}"
        entries[key] = r
    with open(os.path.join(ws, "logs", "pending_entries.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "entries": entries}, f)


def append_audit_records(ws, *records):
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    path = os.path.join(ws, "logs", "trades_audit.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class TestIssue47MaxOpenPositions(unittest.TestCase):

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws, True)

    def _check(self, max_open=2, env="prod"):
        prof = {"max_open_positions": max_open}
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
             patch("execute_futures_trade.send_signed_request", side_effect=flat_exchange):
            return eft.check_max_open_positions(prof, env, base_dir=self.ws)

    def _check_gates(self, max_open=2, env="prod"):
        prof = {
            "max_open_positions": max_open,
            "yolo_slot_enabled": True,
            "leverage_standard": 3,
            "leverage_yolo": 15,
            "risk_pct_equity": 0.005
        }
        with patch("execute_futures_trade._workspace_dir", return_value=self.ws), \
             patch("execute_futures_trade.send_signed_request", side_effect=flat_exchange), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("user_profile.load_user_profile", return_value=prof):
            return eft.check_mechanical_gates(
                direction="LONG",
                cur_price=100.0,
                sl_price=98.0,
                tp1_price=105.0,
                total_qty=1.0,
                leverage=3,
                target_env=env,
                is_yolo=False,
                bypass_delta_gate=False
            )

    # -------------------------------------------------------------------------
    # a) Pending fill gap
    # -------------------------------------------------------------------------
    def test_pending_fill_gap_blocks_new_entry(self):
        """
        A resting LIMIT/STOP order fills: protect_pending_entries drops the record
        from logs/pending_entries.json and appends to logs/trades_audit.jsonl.
        Before sync_session_state updates session_state.json, check_max_open_positions
        must count the recent fill to avoid exceeding max_open_positions.
        """
        # 1 position in session_state synced at t=1000
        write_session_state(self.ws, ["BTCUSDT"], last_updated_ts=1000)
        # Pending entries registry is now empty (record was dropped on fill)
        write_pending_registry(self.ws)
        # Audit log has the fill record for ETHUSDT at t=1010
        append_audit_records(self.ws, {
            "timestamp": 1010,
            "symbol": "ETHUSDT",
            "direction": "LONG",
            "total_qty": 1.5,
            "target_env": "prod"
        })

        # Max open positions = 2: open (1) + pending (0) + recent fills (1) = 2 -> BLOCKED
        ok, reason = self._check(max_open=2, env="prod")
        self.assertFalse(ok)
        self.assertIn(GATE_PREFIX + " (2) reached", reason)
        self.assertIn("(open 1 + pending 0 + recent fills 1 >= max 2)", reason)

        # Same via check_mechanical_gates
        ok_gates, reason_gates = self._check_gates(max_open=2, env="prod")
        self.assertFalse(ok_gates)
        self.assertIn("(open 1 + pending 0 + recent fills 1 >= max 2)", reason_gates)

        # With max_open = 3, committed is 2 < 3 -> ALLOWED
        ok3, reason3 = self._check(max_open=3, env="prod")
        self.assertTrue(ok3)
        self.assertIsNone(reason3)

    # -------------------------------------------------------------------------
    # b) MARKET fill gap
    # -------------------------------------------------------------------------
    def test_market_fill_gap_blocks_new_entry(self):
        """
        A MARKET order fills: it never resided in pending_entries.json.
        execute_complete_trade writes to logs/trades_audit.jsonl.
        Until session_state.json syncs, the slot must be counted via trades_audit.jsonl.
        """
        write_session_state(self.ws, ["BTCUSDT"], last_updated_ts=1000)
        write_pending_registry(self.ws)
        append_audit_records(self.ws, {
            "timestamp": 1025,
            "symbol": "SOLUSDT",
            "direction": "SHORT",
            "total_qty": 10.0,
            "target_env": "prod"
        })

        ok, reason = self._check(max_open=2, env="prod")
        self.assertFalse(ok)
        self.assertIn(GATE_PREFIX + " (2) reached", reason)
        self.assertIn("(open 1 + pending 0 + recent fills 1 >= max 2)", reason)

    # -------------------------------------------------------------------------
    # c) Sync handoff deduplication
    # -------------------------------------------------------------------------
    def test_sync_handoff_deduplication(self):
        """
        Once sync_session_state runs and updates session_state.json:
        1. The position is now in active_positions (open_count includes it).
        2. last_updated_ts is bumped past the fill timestamp.
        3. Even if timestamps match, symbol in open_symbols prevents double counting.
        Committed slots should not double count.
        """
        # Pre-sync: 1 open position BTCUSDT, fill SOLUSDT at 1010
        write_session_state(self.ws, ["BTCUSDT"], last_updated_ts=1000)
        append_audit_records(self.ws, {
            "timestamp": 1010,
            "symbol": "SOLUSDT",
            "direction": "LONG",
            "total_qty": 5.0,
            "target_env": "prod"
        })

        # Pre-sync with max=2: 1 open + 1 recent = 2 -> blocked
        ok, reason = self._check(max_open=2, env="prod")
        self.assertFalse(ok)
        self.assertIn("(open 1 + pending 0 + recent fills 1 >= max 2)", reason)

        # Post-sync: session_state now includes SOLUSDT and last_updated_ts is 1020 > 1010
        write_session_state(self.ws, ["BTCUSDT", "SOLUSDT"], last_updated_ts=1020)

        # With max=3, committed should be open 2 + pending 0 + recent 0 = 2 -> ALLOWED
        ok, reason = self._check(max_open=3, env="prod")
        self.assertTrue(ok, reason)
        self.assertIsNone(reason)

        # Boundary condition: last_updated_ts exactly equals fill timestamp (1010)
        write_session_state(self.ws, ["BTCUSDT", "SOLUSDT"], last_updated_ts=1010)
        ok_exact, reason_exact = self._check(max_open=3, env="prod")
        self.assertTrue(ok_exact, reason_exact)
        self.assertIsNone(reason_exact)

    # -------------------------------------------------------------------------
    # d) Env isolation
    # -------------------------------------------------------------------------
    def test_env_isolation_testnet_and_prod_independent(self):
        """
        Audit records for testnet must not consume prod slots, and prod records
        must not consume testnet slots.
        """
        write_session_state(self.ws, ["BTCUSDT"], last_updated_ts=1000)

        # Audit log contains a TESTNET fill at t=1015
        append_audit_records(self.ws, {
            "timestamp": 1015,
            "symbol": "ETHUSDT",
            "direction": "LONG",
            "total_qty": 2.0,
            "target_env": "testnet"
        })

        # Checking PROD with max_open=2: ETHUSDT is testnet, so prod slot is NOT consumed
        ok_prod, reason_prod = self._check(max_open=2, env="prod")
        self.assertTrue(ok_prod, reason_prod)
        self.assertIsNone(reason_prod)

        # Checking TESTNET with max_open=2: ETHUSDT is counted against testnet -> BLOCKED
        ok_testnet, reason_testnet = self._check(max_open=2, env="testnet")
        self.assertFalse(ok_testnet)
        self.assertIn("(open 1 + pending 0 + recent fills 1 >= max 2)", reason_testnet)

        # Now append a PROD fill for BNBUSDT
        append_audit_records(self.ws, {
            "timestamp": 1020,
            "symbol": "BNBUSDT",
            "direction": "SHORT",
            "total_qty": 3.0,
            "target_env": "prod"
        })

        # Now PROD sees BNBUSDT (prod) but NOT ETHUSDT (testnet):
        # open 1 (BTCUSDT) + recent 1 (BNBUSDT) = 2 -> BLOCKED
        ok_prod2, reason_prod2 = self._check(max_open=2, env="prod")
        self.assertFalse(ok_prod2)
        self.assertIn("(open 1 + pending 0 + recent fills 1 >= max 2)", reason_prod2)

    # -------------------------------------------------------------------------
    # e) Failsafe abort
    # -------------------------------------------------------------------------
    def test_failsafe_abort_releases_slot(self):
        """
        When emergency_abort_market_close triggers auto-destruct on SL failure,
        CRITICAL_FAILSAFE_ABORT is written to trades_audit.jsonl.
        The aborted position is closed and must NOT consume a slot.
        """
        write_session_state(self.ws, ["BTCUSDT"], last_updated_ts=1000)

        # Entry at t=1010, followed by emergency abort at t=1015
        append_audit_records(self.ws,
            {
                "timestamp": 1010,
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "total_qty": 1.0,
                "target_env": "prod"
            },
            {
                "timestamp": 1015,
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "event": "CRITICAL_FAILSAFE_ABORT",
                "quantity": 1.0,
                "target_env": "prod"
            }
        )

        # ETHUSDT was aborted, so slot is free: open 1 + recent 0 = 1 < 2 -> ALLOWED
        ok, reason = self._check(max_open=2, env="prod")
        self.assertTrue(ok, reason)
        self.assertIsNone(reason)

    def test_failsafe_abort_same_timestamp_sequential_order(self):
        """
        If entry and abort happen in the same second (same timestamp),
        abort logged subsequent to entry must release the slot.
        """
        write_session_state(self.ws, ["BTCUSDT"], last_updated_ts=1000)
        append_audit_records(self.ws,
            {
                "timestamp": 1010,
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "total_qty": 1.0,
                "target_env": "prod"
            },
            {
                "timestamp": 1010,
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "event": "CRITICAL_FAILSAFE_ABORT",
                "quantity": 1.0,
                "target_env": "prod"
            }
        )
        ok, reason = self._check(max_open=2, env="prod")
        self.assertTrue(ok, reason)
        self.assertIsNone(reason)

    def test_new_entry_after_prior_abort_is_counted(self):
        """
        If an abort occurred at t=1005, but a subsequent NEW entry occurred at t=1010,
        the new entry must consume a slot.
        """
        write_session_state(self.ws, ["BTCUSDT"], last_updated_ts=1000)
        append_audit_records(self.ws,
            {
                "timestamp": 1005,
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "event": "CRITICAL_FAILSAFE_ABORT",
                "quantity": 1.0,
                "target_env": "prod"
            },
            {
                "timestamp": 1010,
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "total_qty": 1.0,
                "target_env": "prod"
            }
        )
        ok, reason = self._check(max_open=2, env="prod")
        self.assertFalse(ok)
        self.assertIn("(open 1 + pending 0 + recent fills 1 >= max 2)", reason)

    # -------------------------------------------------------------------------
    # f) Fallback 300s window when session_state has no timestamp
    # -------------------------------------------------------------------------
    def test_fallback_300s_window_when_sync_ts_zero(self):
        """
        When session_state last_updated_ts <= 0, the fallback window is time.time() - 300.
        Fills within 300s are counted; fills older than 300s are ignored.
        """
        now = 10000.0
        # session_state has last_updated_ts = 0
        write_session_state(self.ws, ["BTCUSDT"], last_updated_ts=0)

        # Record 1: 100s ago (within 300s) -> counted
        # Record 2: 400s ago (older than 300s) -> ignored
        append_audit_records(self.ws,
            {
                "timestamp": now - 400,
                "symbol": "OLDUSDT",
                "direction": "LONG",
                "total_qty": 1.0,
                "target_env": "prod"
            },
            {
                "timestamp": now - 100,
                "symbol": "NEWUSDT",
                "direction": "LONG",
                "total_qty": 1.0,
                "target_env": "prod"
            }
        )

        with patch("time.time", return_value=now):
            ok, reason = self._check(max_open=2, env="prod")
            self.assertFalse(ok)
            # Only NEWUSDT counted, OLDUSDT ignored: open 1 + recent 1 = 2 >= 2
            self.assertIn("(open 1 + pending 0 + recent fills 1 >= max 2)", reason)

    # -------------------------------------------------------------------------
    # g) No double counting with pending_entries
    # -------------------------------------------------------------------------
    def test_symbol_in_both_pending_and_audit_not_double_counted(self):
        """
        If a symbol is still in pending_entries.json and also has a fill record,
        it must only consume 1 slot (in pending_symbols, not duplicated in recent_fill_symbols).
        """
        write_session_state(self.ws, ["BTCUSDT"], last_updated_ts=1000)
        write_pending_registry(self.ws, {
            "symbol": "ETHUSDT",
            "entry_id": "1",
            "target_env": "prod",
            "kind": "LIMIT"
        })
        append_audit_records(self.ws, {
            "timestamp": 1010,
            "symbol": "ETHUSDT",
            "direction": "LONG",
            "total_qty": 1.0,
            "target_env": "prod"
        })

        # Committed should be open 1 + pending 1 + recent 0 = 2 (not 3!)
        ok, reason = self._check(max_open=2, env="prod")
        self.assertFalse(ok)
        self.assertIn("(open 1 + pending 1 >= max 2)", reason)

        # With max=3, 2 < 3 -> ALLOWED
        ok3, reason3 = self._check(max_open=3, env="prod")
        self.assertTrue(ok3, reason3)


if __name__ == "__main__":
    unittest.main()
