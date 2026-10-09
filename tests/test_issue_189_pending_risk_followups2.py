#!/usr/bin/env python3
"""
test_issue_189_pending_risk_followups2.py - Offline tests for the #189 follow-ups of #160 / #188.

#189.1  TP adoption also requires the planned remaining quantity (origQty - executedQty within max(stepSize, 1%));
        a price match with another quantity is not adopted (warning, a new TP is placed); adopted_qty is logged.
#189.2  TP adoption price match: half a tick; without the exchange tick the record's tick_size, else 0.05% of price.
#189.3  record_price_tolerances(strict_ticks=True): a symbol with no known exchange tick matches exactly; Gate 1 uses it.
#189.4  sync: listing_read_error for a raised or non-list order listing read; doctor WARN (exit code unchanged).
#189.5  brief: pending_entries_status / state_sync always present (OK or the bad value); prompt: missing = fail closed.
#189.6  post-trade sync hook: the "attempted" flag is sync_attempted (synced is gone).
#189.7  evaluator prompt: no FLAT / BALANCED portfolio vocabulary outside DELTA_BALANCED.
#189.8  declined: offline() already patches execute_futures_trade.time.sleep (pinned here).

No network (urlopen is blocked), the Binance client is faked, and every file goes to a temp workspace.
"""

import io
import os
import re
import sys
import json
import shutil
import tempfile
import unittest
import contextlib
import importlib.util
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS_DIR, "hooks")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SCRIPTS_DIR, HOOKS_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import execute_futures_trade as eft
import sync_session_state as sss
import trading_doctor
import prime_evaluator_brief as peb
import test_issue_101_exchange_anchored_gates as t101   # module imports only: their tests are not collected twice
import test_issue_92_holding_time as t92
from test_pending_entries import make_record, write_registry, read_registry, posts
from test_exit_management import FakeExchange, offline, long_position, stop

LiveExchange = t101.LiveExchange
ORDER_ENDPOINT = "/fapi/v1/order"

_spec = importlib.util.spec_from_file_location("post_trade_sync_issue_189", os.path.join(HOOKS_DIR, "post_trade_sync.py"))
pts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pts)


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def tempdir(test):
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d, True)
    return d


def tp_order(order_id, price, qty="3", executed="0", side="SELL"):
    return {"orderId": order_id, "symbol": "BTCUSDT", "side": side, "type": "LIMIT", "price": str(price),
            "origQty": qty, "executedQty": executed, "reduceOnly": True, "status": "NEW"}


def find(orders, wanted, tick=0.1, **kw):
    with patch("execute_futures_trade.send_signed_request", return_value=orders):
        found, err = eft.find_resting_take_profits("BTCUSDT", "SELL", wanted, tick, **kw)
    assert err is None, err
    return found


# =============================================================================
# #189.1 / #189.2 TP adoption: quantity and tolerance
# =============================================================================
class TestFindRestingTakeProfits(unittest.TestCase):

    def test_quantity_match_is_adopted_and_reported(self):
        got = {}
        found = find([tp_order(1, 110.0, qty="3"), tp_order(2, 120.0, qty="7")], {"tp1": 110.0, "tp2": 120.0},
                     quantities={"tp1": 3.0, "tp2": 7.0}, step_size=0.001, adopted_qty=got)
        self.assertEqual(found, {"tp1": 1, "tp2": 2})
        self.assertEqual(got, {"tp1": 3.0, "tp2": 7.0})

    def test_quantity_mismatch_is_not_adopted_and_warns(self):
        got = {}
        with self.assertLogs("execute_futures_trade", level="WARNING") as logs:
            found = find([tp_order(1, 110.0, qty="5")], {"tp1": 110.0}, quantities={"tp1": 3.0}, step_size=0.001,
                         adopted_qty=got)
        self.assertEqual(found, {})
        self.assertEqual(got, {})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("not adopted", logs.output[0])
        self.assertIn("1 (5)", logs.output[0])

    def test_a_later_order_with_the_right_quantity_is_adopted_without_warning(self):
        with self.assertNoLogs("execute_futures_trade", level="WARNING"):
            found = find([tp_order(1, 110.0, qty="5"), tp_order(2, 110.0, qty="3")], {"tp1": 110.0},
                         quantities={"tp1": 3.0}, step_size=0.001)
        self.assertEqual(found, {"tp1": 2})

    def test_partially_executed_order_uses_its_remaining_quantity(self):
        got = {}
        self.assertEqual(find([tp_order(1, 110.0, qty="4", executed="1")], {"tp1": 110.0},
                              quantities={"tp1": 3.0}, step_size=0.001, adopted_qty=got), {"tp1": 1})
        self.assertEqual(got, {"tp1": 3.0})
        with self.assertLogs("execute_futures_trade", level="WARNING"):
            self.assertEqual(find([tp_order(1, 110.0, qty="3", executed="1")], {"tp1": 110.0},
                                  quantities={"tp1": 3.0}, step_size=0.001), {})

    def test_within_one_percent_or_one_step(self):
        q = {"tp1": 300.0}
        self.assertEqual(find([tp_order(1, 110.0, qty="297")], {"tp1": 110.0}, quantities=q, step_size=0.001),
                         {"tp1": 1}, "exactly 0.99x")
        with self.assertLogs("execute_futures_trade", level="WARNING"):
            self.assertEqual(find([tp_order(1, 110.0, qty="296.9")], {"tp1": 110.0}, quantities=q, step_size=0.001),
                             {})
        q = {"tp1": 0.03}   # small quantity: one step (0.001) is wider than 1%
        self.assertEqual(find([tp_order(1, 110.0, qty="0.029")], {"tp1": 110.0}, quantities=q, step_size=0.001),
                         {"tp1": 1}, "exactly one step away")
        with self.assertLogs("execute_futures_trade", level="WARNING"):
            self.assertEqual(find([tp_order(1, 110.0, qty="0.028")], {"tp1": 110.0}, quantities=q, step_size=0.001),
                             {})

    def test_without_quantities_the_price_match_is_unchanged(self):
        self.assertEqual(find([tp_order(1, 110.0, qty="5")], {"tp1": 110.0}), {"tp1": 1})

    def test_half_tick_with_the_exchange_tick(self):
        self.assertEqual(find([tp_order(1, 110.04)], {"tp1": 110.0}), {"tp1": 1})
        self.assertEqual(find([tp_order(1, 110.06)], {"tp1": 110.0}), {}, "beyond half a tick (0.05)")
        self.assertEqual(find([tp_order(1, 110.06)], {"tp1": 110.0}, record_tick=10.0), {},
                         "the exchange tick wins over the record's")

    def test_unknown_exchange_tick_uses_the_record_tick(self):
        self.assertEqual(find([tp_order(1, 110.04)], {"tp1": 110.0}, tick=None, record_tick=0.1), {"tp1": 1})
        self.assertEqual(find([tp_order(1, 110.06)], {"tp1": 110.0}, tick=None, record_tick=0.1), {})
        self.assertEqual(find([tp_order(1, 110.04)], {"tp1": 110.0}, tick=None, record_tick=0.01), {})

    def test_no_tick_at_all_uses_five_bps_of_price(self):
        self.assertEqual(find([tp_order(1, 110.05)], {"tp1": 110.0}, tick=None), {"tp1": 1}, "0.05 <= 0.055")
        self.assertEqual(find([tp_order(1, 110.06)], {"tp1": 110.0}, tick=None), {}, "0.06 > 0.055")


class TestProtectAdoptsByQuantity(unittest.TestCase):
    """_protect_pending_entry passes the planned TP quantities, the stepSize and the record tick."""

    def setUp(self):
        self.ws = tempdir(self)

    def protect(self, open_orders):
        write_registry(self.ws, make_record(sl_qty=10.0, tp1_qty=3.0, tp2_qty=7.0, tick_size=0.1))
        fake = FakeExchange([long_position(amt="10", entry="101.0")], algos=[stop(701, 95.0)], open_orders=open_orders)
        spy = MagicMock(wraps=eft.find_resting_take_profits)
        with offline(fake, workspace=self.ws), \
             patch("execute_futures_trade.find_resting_take_profits", spy), \
             patch("provenance_stamp.stamp_trade_record", side_effect=lambda rec, **kw: rec):
            res = eft.protect_pending_entries(target_env="testnet")
        return res, fake, spy

    def test_adopted_quantity_is_logged_and_no_tp_is_placed(self):
        res, fake, spy = self.protect([tp_order(501, 110.0, qty="3"), tp_order(502, 120.0, qty="7")])
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(posts(fake, ORDER_ENDPOINT), [])
        tp = [a for a in res["actions"] if a["type"] == "pending_tp_placed"][0]
        self.assertEqual(tp["detail"]["adopted_existing"], ["tp1_order_id", "tp2_order_id"])
        self.assertEqual(tp["detail"]["adopted_qty"], {"tp1_order_id": 3.0, "tp2_order_id": 7.0})
        kw = spy.call_args.kwargs
        self.assertEqual(kw["quantities"], {"tp1_order_id": 3.0, "tp2_order_id": 7.0})
        self.assertEqual(kw["step_size"], 0.001)
        self.assertEqual(kw["record_tick"], 0.1)
        self.assertEqual(read_registry(self.ws), {})

    def test_wrong_quantity_places_a_new_tp(self):
        with self.assertLogs("execute_futures_trade", level="WARNING") as logs:
            res, fake, _ = self.protect([tp_order(501, 110.0, qty="3"), tp_order(502, 120.0, qty="5")])
        self.assertTrue(res["ok"], res["errors"])
        self.assertTrue(any("not adopted" in m for m in logs.output))
        self.assertEqual([(t["price"], t["quantity"]) for t in posts(fake, ORDER_ENDPOINT)], [(120.0, 7.0)])
        tp = [a for a in res["actions"] if a["type"] == "pending_tp_placed"][0]
        self.assertTrue(tp["success"])
        self.assertEqual(tp["detail"]["adopted_existing"], ["tp1_order_id"])
        self.assertEqual(tp["detail"]["adopted_qty"], {"tp1_order_id": 3.0})
        self.assertNotEqual(tp["detail"]["tp2_order_id"], 502)


# =============================================================================
# #189.3 strict registry ticks
# =============================================================================
def prod_record(**kw):
    kw.setdefault("env", "prod")
    return make_record(**kw)


class TestStrictTicks(t101.Workspace):

    def test_strict_drops_unknown_tick_symbols(self):
        rec = prod_record(tick_size=10.0)
        self.assertEqual(eft.record_price_tolerances([rec], {"ETHUSDT": 0.01}), {"BTCUSDT": 5.0}, "default unchanged")
        self.assertEqual(eft.record_price_tolerances([rec], {"ETHUSDT": 0.01}, strict_ticks=True), {})
        self.assertEqual(eft.record_price_tolerances([rec], {"BTCUSDT": None}, strict_ticks=True), {})
        self.assertEqual(eft.record_price_tolerances([rec], {"BTCUSDT": 0.01}, strict_ticks=True), {"BTCUSDT": 0.005})
        self.assertEqual(eft.record_price_tolerances([rec], None, strict_ticks=True), {"BTCUSDT": 5.0},
                         "no exchange ticks given: nothing to be strict against")

    def gate(self, **kw):
        self.write_state([])
        write_registry(self.ws, prod_record(total_qty=2.0, tick_size=1.0))   # half = 0.5 matches 101.3
        fake = LiveExchange(algos=[{"symbol": "BTCUSDT", "side": "BUY", "triggerPrice": 101.3,
                                    "orderType": "STOP_MARKET", "closePosition": False}])
        with patch("execute_futures_trade.check_max_open_positions", return_value=(True, None)):
            return self.gates(fake, "SHORT", **kw)

    def test_gate1_is_strict_for_other_symbols(self):
        ok, msg = self.gate()
        self.assertTrue(ok, msg)
        ok, msg = self.gate(exchange_ticks={"SOLUSDT": 0.01})   # the ordered symbol's tick only
        self.assertFalse(ok)
        self.assertIn("delta-neutral gate cannot measure the portfolio", msg)

    def test_gate1_passes_strict_ticks(self):
        spy = MagicMock(wraps=eft.record_price_tolerances)
        with patch("execute_futures_trade.record_price_tolerances", spy):
            self.gate(exchange_ticks={"BTCUSDT": 1.0})
        self.assertIs(spy.call_args.kwargs["strict_ticks"], True)


# =============================================================================
# #189.4 listing_read_error and the doctor WARN
# =============================================================================
class TestListingReadError(unittest.TestCase):

    def setUp(self):
        self.tmp = tempdir(self)

    def sync(self, fake):
        with patch.object(sss, "LOGS_DIR", self.tmp), \
             patch.object(sss, "STATE_FILE", os.path.join(self.tmp, "session_state.json")), \
             patch.object(sss, "AUDIT_LOG", os.path.join(self.tmp, "trades_audit.jsonl")), \
             patch.dict(sys.modules, {"shadow_tracker": None}), \
             patch("execute_futures_trade.send_signed_request", side_effect=fake):
            state = sss.sync_session_state(target_env="prod")
        with open(os.path.join(self.tmp, "session_state.json"), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f).get("listing_read_error"), state.get("listing_read_error"))
        return state

    def test_raised_read(self):
        state = self.sync(LiveExchange(errors={"/fapi/v1/openAlgoOrders": ConnectionError("down")}))
        self.assertIs(state["is_valid"], True)
        self.assertIn("/fapi/v1/openAlgoOrders read failed", state["listing_read_error"])
        self.assertIn("ConnectionError: down", state["listing_read_error"])
        self.assertNotIn("/fapi/v1/openOrders", state["listing_read_error"])
        self.assertIn("Order listing read failed", sss.format_markdown_summary(state))

    def test_non_list_reply(self):
        state = self.sync(LiveExchange(errors={"/fapi/v1/openOrders": {"code": -1001, "msg": "x"}}))
        self.assertIs(state["is_valid"], True)
        self.assertIn("/fapi/v1/openOrders read failed", state["listing_read_error"])
        self.assertIn("-1001", state["listing_read_error"])
        self.assertEqual(state["portfolio_exposure"]["delta_bias_incl_resting"], "UNKNOWN")

    def test_clean_read_is_none(self):
        state = self.sync(LiveExchange())
        self.assertIn("listing_read_error", state)
        self.assertIsNone(state["listing_read_error"])
        self.assertNotIn("Order listing read failed", sss.format_markdown_summary(state))

    def test_error_state_carries_it(self):
        state = self.sync(LiveExchange(errors={e: ConnectionError("down") for e in
                                               ("/fapi/v1/openOrders", "/fapi/v2/positionRisk")}))
        self.assertIs(state["is_valid"], False)
        self.assertIn("/fapi/v1/openOrders read failed", state["listing_read_error"])
        with patch.object(sss, "STATE_FILE", os.path.join(self.tmp, "error_state.json")):
            self.assertIsNone(sss.write_error_state("x", 0, "now", "prod", 0.0)["listing_read_error"])

    def test_doctor_warns_and_still_exits_zero(self):
        h = t92.TestDoctorTemporalAudit("setUp")
        h.setUp()
        self.addCleanup(h.doCleanups)
        h.write_state(age_s=10, listing_read_error="/fapi/v1/openAlgoOrders read failed: ConnectionError: down")
        code, out, _, _, _ = h.run_doctor()
        self.assertIn("[STATE LEDGER] Ledger sync: order listing read failed", out)
        self.assertIn("ConnectionError: down", out)
        self.assertIn("OPERATIONAL WITH WARNINGS", out)
        self.assertEqual(code, 0, out)
        h.write_state(age_s=10, listing_read_error=None)
        self.assertNotIn("order listing read failed", h.run_doctor()[1])

    def test_doctor_ignores_other_env_ledger(self):
        state = {"target_env": "prod", "listing_read_error": "x"}
        self.assertIsNone(trading_doctor.ledger_listing_warning(state, "testnet"))
        self.assertIsNotNone(trading_doctor.ledger_listing_warning(state, "prod"))


# =============================================================================
# #189.5 brief status keys and the prompt
# =============================================================================
AGENT_SOURCE = os.path.join(BASE_DIR, ".agents", "agents", "isolated_market_evaluator", "agent.md")
AGENT_GENERATED = os.path.join(BASE_DIR, ".claude", "agents", "isolated_market_evaluator.md")


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestBriefStatusKeys(unittest.TestCase):

    def setUp(self):
        self.tmp = tempdir(self)

    def brief(self, state):
        with patch.object(peb, "BRIEF_FILE", os.path.join(self.tmp, "primed_brief.json")), \
             patch.object(peb, "ensure_fresh_state", return_value=state), \
             patch.object(peb, "get_latest_screening_payload", return_value={}), \
             patch.object(peb, "load_recent_insights", return_value=[]), \
             patch.object(peb, "build_risk_profile", return_value={}):
            return peb.assemble_primed_brief(target_env="prod")

    @staticmethod
    def state(**extra):
        return dict({"target_env": "prod", "is_valid": True, "active_positions": [],
                     "portfolio_exposure": {"delta_bias": "DELTA_BALANCED",
                                            "delta_bias_incl_resting": "DELTA_BALANCED"}}, **extra)

    def test_ok_values_are_emitted_and_not_printed_as_problems(self):
        brief = self.brief(self.state())
        self.assertEqual((brief["pending_entries_status"], brief["state_sync"]), ("OK", "OK"))
        md = peb.format_markdown_brief(brief)
        self.assertNotIn("UNREADABLE", md)
        self.assertNotIn("FAILED", md)
        self.assertLessEqual(peb._brief_bytes(brief), peb.BRIEF_BUDGET_BYTES)

    def test_bad_values(self):
        brief = self.brief(self.state(**{peb.SYNC_FAILED_KEY: True,
                                         "portfolio_exposure": {"delta_bias": "DELTA_BALANCED"}}))
        self.assertEqual((brief["pending_entries_status"], brief["state_sync"]), ("UNREADABLE", "FAILED"))
        md = peb.format_markdown_brief(brief)
        self.assertIn("**Pending entries status:** `UNREADABLE`", md)
        self.assertIn("**State sync:** `FAILED`", md)

    def test_prompt_missing_key_fails_closed(self):
        for path in (AGENT_SOURCE, AGENT_GENERATED):
            with self.subTest(path=path):
                text = read(path)
                rule2 = text.split("- RULE 2")[1].split("- RULE 3")[0]
                self.assertNotIn("means OK", rule2)
                self.assertIn("Both keys are always present", rule2)
                self.assertIn("a MISSING key (a brief from an older run) counts as the bad value (fail closed: "
                              "UNREADABLE / FAILED)", rule2)


# =============================================================================
# #189.6 sync_attempted
# =============================================================================
class TestSyncAttempted(unittest.TestCase):

    PAYLOAD = {"toolCall": {"name": "call_mcp_tool", "args": {
        "ServerName": "binance", "ToolName": "futures_usds.newOrder",
        "Arguments": {"symbol": "BTCUSDT", "side": "SELL", "quantity": "0.01", "reduceOnly": True,
                      "env": "testnet"}}}}

    def test_key_renamed(self):
        root = tempdir(self)
        os.makedirs(os.path.join(root, "scripts"))
        with open(os.path.join(root, "scripts", "sync_session_state.py"), "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\n")
        with patch.object(pts, "find_workspace_root", return_value=root), \
             patch.object(pts.subprocess, "run", return_value=MagicMock(returncode=1)), \
             contextlib.redirect_stderr(io.StringIO()):
            res = pts.handle_post_trade_sync(json.loads(json.dumps(self.PAYLOAD)))
        self.assertIs(res["sync_attempted"], True)
        self.assertEqual(res["sync_rc"], 1)
        self.assertNotIn("synced", res)
        idle = pts.handle_post_trade_sync({"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}})
        self.assertIs(idle["sync_attempted"], False)
        self.assertNotIn("synced", idle)


# =============================================================================
# #189.7 prompt vocabulary
# =============================================================================
class TestPromptVocabulary(unittest.TestCase):

    def test_no_flat_or_bare_balanced_portfolio(self):
        for path in (AGENT_SOURCE, AGENT_GENERATED):
            text = read(path)
            scenarios = re.findall(r"<scenario>([\s\S]*?)</scenario>", text)
            self.assertEqual(len(scenarios), 15)
            for s in scenarios + re.findall(r"<user_input>([\s\S]*?)</user_input>", text):
                with self.subTest(path=path, text=s[:60]):
                    self.assertIsNone(re.search(r"\bFLAT\b", s))
                    self.assertIsNone(re.search(r"(?<!DELTA_)BALANCED", s))
            for phrase in ("Portfolio FLAT", "FLAT portfolio", "BALANCED portfolio", "Portfolio BALANCED"):
                self.assertNotIn(phrase, text.replace("DELTA_BALANCED", "DELTA_B"))


# =============================================================================
# #189.8 declined: the TP lock-retry tests never sleep for real
# =============================================================================
class TestOfflinePatchesSleep(unittest.TestCase):

    def test_offline_patches_the_executor_sleep(self):
        with offline(FakeExchange([])):
            self.assertIsInstance(eft.time.sleep, MagicMock)
            eft.time.sleep(eft.PENDING_SAVE_RETRY_DELAYS_S[-1])   # returns at once


if __name__ == "__main__":
    unittest.main()
