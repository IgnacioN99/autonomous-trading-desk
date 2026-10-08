#!/usr/bin/env python3
"""
test_trade_excursion.py - scripts/utils/trade_excursion.py (issue #182): R / percent excursions and the incremental
MFE / MAE record built from closed 1m bars after the fill minute plus the mark price. Hermetic: no network.
"""

import os
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from utils import trade_excursion as tx

MIN = 60_000
ENTRY_TS = 1_700_000_030.0  # 30 s into its minute
FILL_BAR = int(ENTRY_TS * 1000) // MIN * MIN


def bar(open_ms, high, low, close=None):
    close = high if close is None else close
    return [open_ms, str(close), str(high), str(low), str(close), "10", open_ms + MIN - 1]


def now_after(bars):
    """Seconds just after the given number of 1m bars past the fill bar have closed (the next one is forming)."""
    return (FILL_BAR + (bars + 1) * MIN + 5_000) / 1000.0


class TestExcursionR(unittest.TestCase):

    def test_long(self):
        self.assertEqual(tx.excursion_r("LONG", 100.0, 2.0, 105.0, 97.0), (2.5, -1.5))

    def test_short_mirrored(self):
        self.assertEqual(tx.excursion_r("SHORT", 100.0, 2.0, 103.0, 95.0), (2.5, -1.5))

    def test_clamped_signs(self):
        # Range entirely on the favourable side: MAE 0, never positive; entirely adverse: MFE 0.
        self.assertEqual(tx.excursion_r("LONG", 100.0, 2.0, 104.0, 101.0), (2.0, 0.0))
        self.assertEqual(tx.excursion_r("SHORT", 100.0, 2.0, 104.0, 101.0), (0.0, -2.0))

    def test_no_risk(self):
        for risk in (None, 0, -1.0):
            self.assertEqual(tx.excursion_r("LONG", 100.0, risk, 105.0, 97.0), (None, None))


class TestUpdateExcursion(unittest.TestCase):

    def test_uses_closed_post_entry_bars_only(self):
        klines = [
            bar(FILL_BAR - MIN, 150.0, 50.0),   # before entry
            bar(FILL_BAR, 140.0, 60.0),         # the fill minute: excluded
            bar(FILL_BAR + MIN, 103.0, 99.0),   # first full post-entry minute
            bar(FILL_BAR + 2 * MIN, 102.0, 98.0),
            bar(FILL_BAR + 3 * MIN, 130.0, 70.0),  # still forming at now: excluded
        ]
        rec = tx.update_excursion(None, side="LONG", entry_price=100.0, risk=2.0, entry_ts=ENTRY_TS, klines=klines,
                                  mark_price=100.5, now_ts=now_after(2))
        self.assertEqual(rec["peak_price"], 103.0)
        self.assertEqual(rec["trough_price"], 98.0)
        self.assertEqual((rec["mfe_r"], rec["mae_r"]), (1.5, -1.0))
        self.assertEqual((rec["mfe_pct"], rec["mae_pct"]), (3.0, -2.0))
        self.assertEqual(rec["mfe_ts"], FILL_BAR + MIN)
        self.assertEqual(rec["mae_ts"], FILL_BAR + 2 * MIN)
        self.assertEqual(rec["last_bar_open_ms"], FILL_BAR + 2 * MIN)
        self.assertFalse(rec["partial"])

    def test_short_side(self):
        klines = [bar(FILL_BAR + MIN, 101.0, 96.0)]
        rec = tx.update_excursion(None, side="SHORT", entry_price=100.0, risk=2.0, entry_ts=ENTRY_TS, klines=klines,
                                  mark_price=99.0, now_ts=now_after(1))
        self.assertEqual((rec["peak_price"], rec["trough_price"]), (96.0, 101.0))
        self.assertEqual((rec["mfe_r"], rec["mae_r"]), (2.0, -0.5))
        self.assertEqual((rec["mfe_pct"], rec["mae_pct"]), (4.0, -1.0))

    def test_folds_mark_price(self):
        now = now_after(1)
        rec = tx.update_excursion(None, side="LONG", entry_price=100.0, risk=2.0, entry_ts=ENTRY_TS,
                                  klines=[bar(FILL_BAR + MIN, 101.0, 99.5)], mark_price=104.0, now_ts=now)
        self.assertEqual(rec["peak_price"], 104.0)
        self.assertEqual(rec["mfe_r"], 2.0)
        self.assertEqual(rec["mfe_ts"], int(now * 1000))

    def test_never_decreases_mfe_or_raises_mae_and_skips_seen_bars(self):
        first = tx.update_excursion(None, side="LONG", entry_price=100.0, risk=2.0, entry_ts=ENTRY_TS,
                                    klines=[bar(FILL_BAR + MIN, 104.0, 97.0)], mark_price=101.0, now_ts=now_after(1))
        self.assertEqual((first["mfe_r"], first["mae_r"]), (2.0, -1.5))
        # A re-delivered (already seen) bar with a higher high is ignored; the new bar is milder.
        klines = [bar(FILL_BAR + MIN, 110.0, 90.0), bar(FILL_BAR + 2 * MIN, 101.0, 99.0)]
        second = tx.update_excursion(first, side="LONG", entry_price=100.0, risk=2.0, entry_ts=ENTRY_TS,
                                     klines=klines, mark_price=100.0, now_ts=now_after(2))
        self.assertEqual((second["mfe_r"], second["mae_r"]), (2.0, -1.5))
        self.assertEqual(second["peak_price"], 104.0)
        self.assertEqual(second["last_bar_open_ms"], FILL_BAR + 2 * MIN)
        # Even a prev with a larger stored MFE (e.g. a smaller earlier risk) is never lowered.
        third = tx.update_excursion(dict(second, mfe_r=3.0, mae_r=-2.0), side="LONG", entry_price=100.0, risk=2.0,
                                    entry_ts=ENTRY_TS, klines=[], mark_price=100.0, now_ts=now_after(2))
        self.assertEqual((third["mfe_r"], third["mae_r"]), (3.0, -2.0))

    def test_no_risk_keeps_prices_and_percent(self):
        rec = tx.update_excursion(None, side="LONG", entry_price=100.0, risk=None, entry_ts=ENTRY_TS,
                                  klines=[bar(FILL_BAR + MIN, 102.0, 99.0)], mark_price=100.0, now_ts=now_after(1))
        self.assertEqual((rec["mfe_r"], rec["mae_r"]), (None, None))
        self.assertEqual((rec["peak_price"], rec["mfe_pct"]), (102.0, 2.0))

    def test_partial_when_gap_exceeds_limit_and_sticky(self):
        gap_bars = tx.GUARDIAN_KLINES_LIMIT + 50
        klines = [bar(FILL_BAR + (i + 1) * MIN, 100.5, 99.5) for i in range(tx.GUARDIAN_KLINES_LIMIT)]
        rec = tx.update_excursion(None, side="LONG", entry_price=100.0, risk=1.0, entry_ts=ENTRY_TS, klines=klines,
                                  mark_price=100.0, now_ts=now_after(gap_bars))
        self.assertTrue(rec["partial"])
        self.assertEqual(rec["last_bar_open_ms"], FILL_BAR + tx.GUARDIAN_KLINES_LIMIT * MIN)
        # Next cycle catches up the rest from the next bar; partial stays set.
        self.assertEqual(tx.next_bar_start_ms(rec, ENTRY_TS), FILL_BAR + (tx.GUARDIAN_KLINES_LIMIT + 1) * MIN)
        rest = [bar(FILL_BAR + (i + 1) * MIN, 100.5, 99.5) for i in range(tx.GUARDIAN_KLINES_LIMIT, gap_bars)]
        rec2 = tx.update_excursion(rec, side="LONG", entry_price=100.0, risk=1.0, entry_ts=ENTRY_TS, klines=rest,
                                   mark_price=100.0, now_ts=now_after(gap_bars))
        self.assertTrue(rec2["partial"])
        self.assertEqual(rec2["last_bar_open_ms"], FILL_BAR + gap_bars * MIN)

    def test_gap_within_limit_is_not_partial(self):
        n = tx.GUARDIAN_KLINES_LIMIT
        klines = [bar(FILL_BAR + (i + 1) * MIN, 100.5, 99.5) for i in range(n)]
        rec = tx.update_excursion(None, side="LONG", entry_price=100.0, risk=1.0, entry_ts=ENTRY_TS, klines=klines,
                                  mark_price=100.0, now_ts=now_after(n))
        self.assertFalse(rec["partial"])

    def test_no_entry_ts_uses_mark_only(self):
        rec = tx.update_excursion(None, side="LONG", entry_price=100.0, risk=2.0, entry_ts=None,
                                  klines=[bar(FILL_BAR + MIN, 120.0, 80.0)], mark_price=101.0, now_ts=now_after(1))
        self.assertEqual((rec["peak_price"], rec["trough_price"]), (101.0, 100.0))
        self.assertIsNone(rec["last_bar_open_ms"])


class TestFetchHost(unittest.TestCase):

    def test_env_hosts_never_mixed(self):
        with patch("execute_futures_trade.get_client_config", return_value=(None, None, None)):
            self.assertEqual(tx.klines_base_url("prod"), "https://fapi.binance.com")
            self.assertEqual(tx.klines_base_url("testnet"), "https://testnet.binancefuture.com")

    def test_fetch_builds_start_time_request_and_rejects_non_list(self):
        seen = []

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            return Resp(b"[[1, \"1\", \"2\", \"0.5\", \"1\", \"1\", 60000]]" if len(seen) == 1 else b"{\"code\": -1}")

        with patch("execute_futures_trade.get_client_config", return_value=(None, None, None)), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows = tx.fetch_klines_range("btcusdt", "1m", 123000, 99, "prod")
            with self.assertRaises(ValueError):
                tx.fetch_klines_range("BTCUSDT", "1m", 123000, 99, "prod")
        self.assertEqual(len(rows), 1)
        self.assertTrue(seen[0].startswith("https://fapi.binance.com/fapi/v1/klines?"))
        for part in ("symbol=BTCUSDT", "interval=1m", "startTime=123000", "limit=99"):
            self.assertIn(part, seen[0])


if __name__ == "__main__":
    unittest.main()
