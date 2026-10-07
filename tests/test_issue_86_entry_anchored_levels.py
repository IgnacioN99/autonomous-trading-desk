#!/usr/bin/env python3
"""
test_issue_86_entry_anchored_levels.py - Issues #86, #41, #42 and #84: levels anchored at the effective entry.

- Radar (#86/#41): risk_pct, TP1, TP2, rr and the friction filter are measured from the breakout trigger the order
  enters at, not from the current price; `price` and `trigger_distance_pct` are informational.
- Brief (#41): enrich_and_size_candidate recomputes rr_ratio from the sizing entry and never invents TPs.
- Ceiling (#84): rows whose trigger-anchored risk_pct exceeds 5.0% are flagged and dropped from the qualified list.
- Executor (#42): SL/TP1 are tick-rounded before the gates; a rounded level on the wrong side of the entry aborts
  before any write; the TP1-leg minNotional is checked at the TP1 price; the conditional entry minNotional uses
  min(trigger, current price).

No network (urllib is blocked for the module), no orders, no writes to the real logs/.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broad_market_radar as bmr  # noqa: E402
import execute_futures_trade as eft  # noqa: E402
import screening_pipeline as sp  # noqa: E402
import test_analytics_cli as tac  # noqa: E402  (fixtures only)

FILTERS = {"stepSize": 0.001, "minQty": 0.001, "tickSize": 0.01, "precision_qty": 3, "precision_price": 2,
           "minNotional": 5.0}
PROFILE = {"yolo_slot_enabled": True, "leverage_standard": 3, "leverage_yolo": 15, "max_open_positions": 100,
           "risk_pct_equity": 0.005, "max_margin_ratio": 0.30}


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def radar_short_klines(n=55, wick_high=1.025):
    """Mirror of tac.radar_long_klines: steady rally, a climax-volume candle with a large upper absorption wick
    (high = `wick_high` x its open), then the still-open candle (Tier S SHORT). The default keeps the stop under the
    5% ceiling from the trigger; a higher `wick_high` pushes it above."""
    ks, price = [], 100.0
    for i in range(n - 2):
        o, c = price, price * 1.005
        ks.append([i, str(o), str(c * 1.001), str(o * 0.999), str(c), "100"])
        price = c
    o = price
    ks.append([n - 2, str(o), str(o * wick_high), str(o * 0.996), str(o * 0.998), "400"])
    o = o * 0.998
    ks.append([n - 1, str(o), str(o * 1.001), str(o * 0.999), str(o * 0.9995), "400"])
    return ks


def _analyze(klines):
    routes = [("/fapi/v1/klines", lambda url: klines)]
    with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(routes)):
        return bmr.analyze_single_symbol("AAAUSDT", interval="15m")


def _ema20(klines):
    return bmr.calculate_ema([float(k[4]) for k in klines], period=20)[-1]


class TestRadarLevelsFromTrigger(unittest.TestCase):

    def _assert_trigger_anchored(self, cand, klines, sign):
        trigger, price, sl = cand["trigger"], cand["price"], cand["sl"]
        self.assertNotAlmostEqual(trigger, price)
        self.assertEqual(price, float(klines[-1][4]))
        self.assertEqual(cand["trigger_distance_pct"], round(abs(trigger - price) / price * 100, 2))
        risk_pct = abs(trigger - sl) / trigger * 100
        self.assertEqual(cand["risk_pct"], round(risk_pct, 2))
        pick = max if sign > 0 else min
        self.assertAlmostEqual(cand["tp1"], pick(_ema20(klines), trigger * (1 + sign * risk_pct * 1.8 / 100)))
        self.assertAlmostEqual(cand["tp2"], trigger * (1 + sign * risk_pct * 4.0 / 100))
        self.assertEqual(cand["rr"], round(abs(cand["tp2"] - trigger) / abs(trigger - sl), 2))
        # The old price-anchored TP2 differs: the levels really moved to the trigger
        price_risk = abs(price - sl) / price * 100
        self.assertNotAlmostEqual(cand["tp2"], price * (1 + sign * price_risk * 4.0 / 100), places=4)
        self.assertGreaterEqual(abs(cand["tp1"] - trigger) / trigger * 100, 0.50)   # friction from the trigger
        self.assertNotIn("risk_pct_over_ceiling", cand)
        self.assertNotIn("disqualify_reason", cand)

    def test_long_levels_from_trigger(self):
        klines = tac.radar_long_klines()
        cand = _analyze(klines)
        self.assertEqual(cand["direction"], "LONG")
        self.assertGreater(cand["trigger"], cand["price"])
        self._assert_trigger_anchored(cand, klines, +1)

    def test_short_levels_from_trigger(self):
        klines = radar_short_klines()
        cand = _analyze(klines)
        self.assertEqual(cand["direction"], "SHORT")
        self.assertLess(cand["trigger"], cand["price"])
        self._assert_trigger_anchored(cand, klines, -1)


class TestRiskPctCeiling(unittest.TestCase):
    """#84: risk_pct above MAX_RISK_PCT is flagged by the radar row and never reaches the qualified output."""

    def test_constants(self):
        self.assertEqual(bmr.MIN_RISK_PCT, 1.4)
        self.assertEqual(bmr.MAX_RISK_PCT, 5.0)

    def _assert_flagged(self, cand):
        self.assertGreater(cand["risk_pct"], 5.0)
        self.assertTrue(cand["risk_pct_over_ceiling"])
        self.assertEqual(cand["disqualify_reason"], f"risk_pct {cand['risk_pct']:.2f}% > 5.0% intraday ceiling")

    def test_long_and_short_above_ceiling_flagged(self):
        self._assert_flagged(_analyze(tac.radar_long_klines(wick_low=0.94)))
        self._assert_flagged(_analyze(radar_short_klines(wick_high=1.06)))

    def test_boundary_at_ceiling_not_flagged(self):
        for klines in (tac.radar_long_klines(), radar_short_klines()):
            base = _analyze(klines)
            exact = abs(base["trigger"] - base["sl"]) / base["trigger"] * 100
            with patch.object(bmr, "MAX_RISK_PCT", exact):
                at = _analyze(klines)
            self.assertNotIn("risk_pct_over_ceiling", at)
            self.assertEqual(at, base)   # below/at the ceiling the row is unchanged
            with patch.object(bmr, "MAX_RISK_PCT", exact - 0.01):
                self.assertTrue(_analyze(klines)["risk_pct_over_ceiling"])

    def _scan(self, by_symbol):
        info = {"symbols": [{"symbol": s, "underlyingType": "COIN", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                             "status": "TRADING"} for s in by_symbol]}
        tickers = [{"symbol": s, "quoteVolume": str(1000000 * (i + 1))} for i, s in enumerate(by_symbol)]

        def klines(url):
            return next(k for s, k in by_symbol.items() if f"symbol={s}&" in url)

        routes = [("exchangeInfo", info), ("ticker/24hr", tickers), ("/fapi/v1/klines", klines)]
        with patch("urllib.request.urlopen", side_effect=tac.fake_urlopen(routes)), \
             patch("microstructure_engine.get_symbol_microstructure", side_effect=tac.micro_snapshot):
            return bmr.scan_all_liquid_pairs(top_n=10, interval="15m")

    def test_scan_drops_rows_above_ceiling(self):
        res = self._scan({"LOKUSDT": tac.radar_long_klines(), "LBADUSDT": tac.radar_long_klines(wick_low=0.94),
                          "SOKUSDT": radar_short_klines(), "SBADUSDT": radar_short_klines(wick_high=1.06)})
        self.assertEqual(sorted(c["symbol"] for c in res), ["LOKUSDT", "SOKUSDT"])
        by_sym = {c["symbol"]: c for c in res}
        self.assertEqual(by_sym["LOKUSDT"]["direction"], "LONG")
        self.assertEqual(by_sym["SOKUSDT"]["direction"], "SHORT")
        for c in res:
            self.assertLessEqual(c["risk_pct"], 5.0)
            self.assertNotIn("risk_pct_over_ceiling", c)
            self.assertEqual(c["tier_code"], "S")


class TestEnrichFromSizingEntry(unittest.TestCase):

    def _enrich(self, candidate):
        with patch("quant_risk_engine.get_account_equity", return_value=386.0), \
             patch("execute_futures_trade.get_symbol_filters",
                   return_value={"stepSize": 1.0, "minQty": 1.0, "tickSize": 0.00000001, "precision_qty": 0,
                                 "precision_price": 8, "minNotional": 5.0}), \
             patch("user_profile.load_user_profile",
                   return_value={"risk_pct_equity": 0.005, "leverage_standard": 3, "max_margin_ratio": 0.30}), \
             patch("microstructure_engine.get_live_aggtrades_tape", return_value={"live_bias": "BALANCED"}), \
             patch("builtins.print"):
            return sp.enrich_and_size_candidate(candidate, target_env="prod")

    def test_rr_ratio_recomputed_from_trigger_long_and_short(self):
        cases = [("LONG", 0.00250900, 0.00252326, 0.00240840, 0.0027, 0.0029),
                 ("SHORT", 0.00250000, 0.00248000, 0.00260000, 0.0023, 0.0021)]
        for direction, price, trigger, sl, tp1, tp2 in cases:
            res = self._enrich({"symbol": "BEAMXUSDT", "direction": direction, "price": price, "trigger": trigger,
                                "sl": sl, "tp1": tp1, "tp2": tp2, "rr": 4.0})
            self.assertIsNotNone(res, direction)
            self.assertEqual(res.sizing_entry_price, trigger)
            self.assertEqual((res.tp1_price, res.tp2_price), (tp1, tp2))
            expected = round(abs(tp2 - trigger) / abs(trigger - sl), 2)
            self.assertEqual(res.rr_ratio, expected, direction)
            self.assertNotEqual(res.rr_ratio, 4.0)   # the radar's rr is not copied

    def test_missing_take_profits_skipped(self):
        base = {"symbol": "BEAMXUSDT", "direction": "LONG", "price": 0.002509, "trigger": 0.00252326,
                "sl": 0.0024084, "tp1": 0.0027, "tp2": 0.0029, "rr": 3.0}
        self.assertIsNotNone(self._enrich(dict(base)))
        for missing in ("tp1", "tp2"):
            row = dict(base)
            del row[missing]
            self.assertIsNone(self._enrich(row), missing)
            row[missing] = None
            self.assertIsNone(self._enrich(row), missing)


class TestExecutorRoundsBeforeGates(unittest.TestCase):

    def _execute(self, **kwargs):
        calls = []
        placed_stops = []   # echoed on GET openAlgoOrders so the issue #36 pre-arm verifies

        def place_stop(symbol, exit_side, sl_price, target_env=None, quantity=None):
            placed_stops.append({"algoId": 9, "symbol": symbol, "side": exit_side, "orderType": "STOP_MARKET",
                                 "triggerPrice": str(sl_price), "closePosition": quantity is None,
                                 "reduceOnly": quantity is not None})
            return {"algoId": 9}

        def fake(method, endpoint, params=None, target_env=None):
            calls.append((method, endpoint, dict(params or {})))
            if endpoint == "/fapi/v1/openAlgoOrders":
                return [dict(s) for s in placed_stops]
            if endpoint == "/fapi/v1/marginType":
                return {"code": 200, "msg": "success"}
            if endpoint == "/fapi/v1/leverage":
                return {"symbol": params["symbol"], "leverage": params["leverage"]}
            if endpoint == "/fapi/v1/leverageBracket":
                return {"error": "unavailable"}
            if endpoint == "/fapi/v1/ticker/price":
                return {"price": "100.0"}
            if endpoint == "/fapi/v1/order" and method == "POST":
                status = "NEW" if params.get("type") == "LIMIT" else "FILLED"
                return {"orderId": 7, "avgPrice": "100.0", "status": status}
            if endpoint == "/fapi/v1/" + "algoOrder" and method == "POST":
                return {"algoId": 8}
            return {}
        args = dict(symbol="SOLUSDT", direction="LONG", leverage=3, margin_usdt=10.0,
                    sl_price=97.0, tp1_price=110.0, tp2_price=120.0, target_env="testnet", bypass_eval_gate=True)
        args.update(kwargs)
        gates = MagicMock(wraps=eft.check_mechanical_gates)
        ws = tempfile.mkdtemp()
        with patch("execute_futures_trade.send_signed_request", side_effect=fake), \
             patch("execute_futures_trade._workspace_dir", return_value=ws), \
             patch("execute_futures_trade.check_mechanical_gates", gates), \
             patch("execute_futures_trade.get_symbol_filters", return_value=FILTERS), \
             patch("execute_futures_trade.place_algo_stop_loss", side_effect=place_stop), \
             patch("execute_futures_trade.verify_algo_stop_loss", return_value=(True, {"algoId": 9})), \
             patch("quant_risk_engine.get_account_equity", return_value=10000.0), \
             patch("user_profile.load_user_profile", return_value=dict(PROFILE)), \
             patch("builtins.print"), \
             patch("utils.atomic_writer.atomic_append_jsonl"), \
             patch("provenance_stamp.stamp_trade_record", side_effect=lambda rec, **kw: rec):
            res = eft.execute_complete_trade(**args)
        return res, calls, gates

    @staticmethod
    def _writes(calls):
        return [c for c in calls if c[0] in ("POST", "DELETE", "PUT")]

    def test_gates_receive_rounded_sl_and_tp1(self):
        res, calls, gates = self._execute(sl_price=97.009, tp1_price=110.009, tp2_price=120.009)
        self.assertTrue(res["success"], res.get("error"))
        gates.assert_called_once()
        self.assertEqual(gates.call_args.args[2], 97.0)
        self.assertEqual(gates.call_args.args[3], 110.0)
        tps = [c[2] for c in calls if c[1] == "/fapi/v1/order" and c[2].get("reduceOnly")]
        self.assertEqual(sorted(float(p["price"]) for p in tps), [110.0, 120.0])

    def _assert_aborted_before_any_write(self, res, calls, gates, needle):
        self.assertFalse(res["success"])
        self.assertIn(needle, res["error"])
        self.assertIn("fail-closed", res["error"])
        self.assertEqual(self._writes(calls), [])   # no leverage/margin setup, no order
        gates.assert_not_called()

    def test_long_sl_rounding_onto_trigger_aborts(self):
        # SL 102.345 is below the raw trigger 102.347, but both round down to 102.34 (the submitted entry)
        res, calls, gates = self._execute(order_type="STOP_MARKET", trigger_price=102.347, sl_price=102.345)
        self._assert_aborted_before_any_write(res, calls, gates, "Rounded Stop Loss 102.34")

    def test_short_sl_rounding_onto_entry_aborts(self):
        # MARKET SHORT at 100.0: SL 100.005 rounds down to 100.0 = the entry
        res, calls, gates = self._execute(direction="SHORT", sl_price=100.005, tp1_price=90.0, tp2_price=80.0)
        self._assert_aborted_before_any_write(res, calls, gates, "Rounded Stop Loss 100.0")

    def test_tp1_rounding_onto_entry_aborts(self):
        res, calls, gates = self._execute(tp1_price=100.004)
        self._assert_aborted_before_any_write(res, calls, gates, "Rounded TP1 100.0")

    def test_tp1_leg_min_notional_at_tp1_price(self):
        # 3.3334 x 3 / 100 -> 0.1 qty; TP1 30% = 0.03 is below 5 USDT and is bumped to 5 USDT at the TP1 price 110
        res, calls, _ = self._execute(margin_usdt=3.3334)
        self.assertTrue(res["success"], res.get("error"))
        tps = {float(c[2]["price"]): float(c[2]["quantity"]) for c in calls
               if c[1] == "/fapi/v1/order" and c[2].get("reduceOnly")}
        self.assertEqual(tps[110.0], 0.046)   # ceil(5 / 110 / 0.001) steps; at the current price 100 it was 0.05
        self.assertEqual(tps[120.0], 0.054)

    def test_conditional_entry_min_notional_uses_lower_price(self):
        # LONG STOP_MARKET: 1.675 x 3 / 102.34 -> 0.049; 0.049 x 102.34 >= 5 but 0.049 x 100 (current) < 5 -> bumped
        res, calls, _ = self._execute(order_type="STOP_MARKET", trigger_price=102.347, margin_usdt=1.675)
        self.assertTrue(res["success"], res.get("error"))
        entry = [c[2] for c in calls if c[1] == "/fapi/v1/" + "algoOrder" and c[2].get("type") == "STOP_MARKET"]
        self.assertEqual(entry[0]["quantity"], 0.05)


class TestSplitTakeProfitQuantities(unittest.TestCase):

    def test_tp1_bump_uses_passed_reference_price(self):
        tp1_qty, tp2_qty = eft.split_take_profit_quantities(0.1, FILTERS, 110.0)
        self.assertEqual(tp1_qty, 0.046)
        self.assertGreaterEqual(tp1_qty * 110.0, 5.0)
        self.assertEqual(tp2_qty, 0.054)
        low_ref = eft.split_take_profit_quantities(0.1, FILTERS, 100.0)[0]
        self.assertGreaterEqual(low_ref, 0.05)

    def test_no_bump_when_tp1_leg_already_above_min_notional(self):
        self.assertEqual(eft.split_take_profit_quantities(0.2, FILTERS, 110.0), (0.06, 0.14))

    def test_pending_fill_tp1_leg_min_notional_at_record_tp1(self):
        # Filled resting entry of 0.1 at 101: the TP1 leg is a LIMIT at the record's TP1 (110), so 5 USDT there is
        # 0.046, not the 0.05 needed at the 101 fill price.
        from test_exit_management import FakeExchange, offline, long_position
        from test_pending_entries import make_record, write_registry, posts, ORDER_ENDPOINT
        fake = FakeExchange([long_position(amt="0.1", entry="101.0", mark="101.5")])
        ws = tempfile.mkdtemp()
        write_registry(ws, make_record(total_qty=0.1, tp1=110.0, tp2=120.0))
        with offline(fake, workspace=ws):
            res = eft.protect_pending_entries(target_env="testnet", dry_run=False)
        self.assertTrue(res["ok"], res["errors"])
        tps = posts(fake, ORDER_ENDPOINT)
        self.assertEqual([(t["price"], t["quantity"]) for t in tps], [(110.0, 0.046), (120.0, 0.054)])


if __name__ == "__main__":
    unittest.main()
