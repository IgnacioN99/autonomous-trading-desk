#!/usr/bin/env python3
"""
Issue #206 round 4 (PR #214 review): mechanical backstop for evaluator RULE 9.

An unconfirmed, non-YOLO candidate whose radar snapshot (sidecar row joined by record_evaluation.py, bound to the
validated dossier sha256) has `squeeze_risk: true` asks the user in PROD, in the executor and in the PreToolUse
guard, with the same message, whatever its tier label and whatever `require_calibrated_tier_s` says. --confirmed
proceeds; TESTNET is relaxed; a missing snapshot does not trigger it; risk-reducing commands never consult it.

Hermetic: temp workspace (tgb.GuardHarness), urlopen blocked, no writes to the real logs/.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "hooks"), os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_guard_bypasses as tgb  # noqa: E402  (fixtures only; imported first, see test_issue_79)
import pre_trade_guard  # noqa: E402
import execute_futures_trade as eft  # noqa: E402
from utils import score_calibration as scal  # noqa: E402

SCRIPT = "python3 scripts/execute_futures_trade.py"
SQUEEZE_GATE = "Squeeze Risk SHORT"
CALIB_GATE = "Uncalibrated Tier S Score"
MESSAGE = "squeeze_risk SHORT: user confirmation required"


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    tgb.setUpModule()
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


class TestSqueezeBackstop(tgb.GuardHarness):

    SYMBOL = "ETHUSDT"

    def profile(self, **extra):
        with open(os.path.join(self.root, "config", "user_profile.json"), "w", encoding="utf-8") as f:
            json.dump(dict({"profile_completed": True, "yolo_slot_enabled": True, "leverage_standard": 3,
                            "max_open_positions": 5, "autonomous_execution_tier_s": True}, **extra), f)

    def dossier(self, squeeze=True, snapshot=True, **cand):
        """A fast-tracked SHORT dossier (requires_user_confirmation false) whose radar snapshot carries the flag."""
        extra = dict({"tier": "A+", "score": 64, "requires_user_confirmation": False}, **cand)
        record = self.write_provenance_dossier(symbol=self.SYMBOL, direction="SHORT", extra=extra)
        key = f"{self.SYMBOL}|SHORT"
        if snapshot:
            record["radar_snapshots"][key]["radar_snapshot"]["squeeze_risk"] = squeeze
        else:
            record.pop("radar_snapshots", None)
        with open(self.dossier_path, "w", encoding="utf-8") as f:
            json.dump(record, f)

    def deploy(self, flags="", env="prod"):
        command = f"{SCRIPT} --symbol {self.SYMBOL} --direction SHORT --leverage 3 --env {env} {flags}".strip()
        return self.agy(self.cmd(command, conversationId=tgb.PARENT_CONV_ID))

    def execute(self, env="prod", confirmed=False):
        return eft.enforce_evaluation_dossier(self.SYMBOL, "SHORT", env, confirmed=confirmed, base_dir=self.root)

    def assert_asks_in_both(self):
        res = self.deploy()
        self.assertDenied(res, SQUEEZE_GATE)
        self.assertIn(MESSAGE, res["reason"])
        self.assertIn("rerun with --confirmed", res["reason"])
        ok, ex_reason, cand = self.execute()
        self.assertFalse(ok)
        self.assertIsNotNone(cand)
        self.assertIn(MESSAGE, ex_reason)
        self.assertIn(ex_reason.split(f"{self.SYMBOL}: ", 1)[1], res["reason"])  # one helper, one message
        self.assertEqual(self.deploy("--confirmed").get("decision"), "allow")
        self.assertTrue(self.execute(confirmed=True)[0])

    def test_squeezed_short_asks_even_with_calibration_off(self):
        self.profile(require_calibrated_tier_s=False)
        self.dossier()
        self.assert_asks_in_both()

    def test_relabelled_tier_s_asks_even_with_a_calibrated_bucket(self):
        # an evaluator that relabels the capped SHORT as Tier S (score 85, calibrated store) still has to ask
        self.dossier(tier="S", score=85)
        self.assert_asks_in_both()
        self.profile(require_calibrated_tier_s=False)
        self.assert_asks_in_both()

    def test_clean_snapshot_is_autonomous(self):
        self.profile(require_calibrated_tier_s=False)
        self.dossier(squeeze=False)
        self.assertEqual(self.deploy().get("decision"), "allow")
        self.assertTrue(self.execute()[0])
        self.dossier(squeeze="true")  # only an exact true triggers
        self.assertEqual(self.deploy().get("decision"), "allow")

    def test_missing_snapshot_does_not_trigger(self):
        self.profile(require_calibrated_tier_s=False)
        self.dossier(snapshot=False)
        self.assertEqual(self.deploy().get("decision"), "allow")
        self.assertTrue(self.execute()[0])
        cand = {"symbol": self.SYMBOL, "direction": "SHORT", "dossier_sha256": "x"}
        self.assertIsNone(scal.squeeze_confirmation_required(cand, "prod", os.path.join(self.root, "nowhere")))

    def test_snapshot_bound_to_the_validated_dossier(self):
        self.profile(require_calibrated_tier_s=False)
        self.dossier()
        with open(self.dossier_path, encoding="utf-8") as f:
            sha = json.load(f)["provenance"]["sha256"]
        cand = {"symbol": self.SYMBOL, "direction": "SHORT", "dossier_sha256": sha}
        self.assertIn(MESSAGE, scal.squeeze_confirmation_required(cand, "prod", self.root))
        self.assertIsNone(scal.squeeze_confirmation_required(dict(cand, dossier_sha256="f" * 64), "prod", self.root))
        self.assertIsNone(scal.squeeze_confirmation_required(cand, "testnet", self.root))

    def test_testnet_relaxed(self):
        self.profile(require_calibrated_tier_s=False)
        self.dossier()
        res = self.deploy(env="testnet")
        self.assertNotIn(SQUEEZE_GATE, res.get("reason", ""))
        self.assertNotIn(MESSAGE, res.get("reason", ""))
        ok, reason, _ = self.execute(env="testnet")
        self.assertNotIn(MESSAGE, reason)

    def test_confirmation_and_yolo_paths_keep_their_messages(self):
        self.dossier(requires_user_confirmation=True)
        res = self.deploy()
        self.assertDenied(res, "User Confirmation Required")
        self.assertNotIn(MESSAGE, res["reason"])

    def test_risk_reducing_commands_never_consult_it(self):
        self.dossier()
        with patch.object(pre_trade_guard.scal, "squeeze_confirmation_required",
                          side_effect=AssertionError("squeeze backstop consulted")):
            for flags in (f"--close-position --symbol {self.SYMBOL}", f"--move-breakeven --symbol {self.SYMBOL}",
                          "--auto-heal", "--audit-orphans", "--protect-pending"):
                with self.subTest(flags=flags):
                    self.assertEqual(self.agy(self.cmd(f"{SCRIPT} {flags} --env prod")).get("decision"), "allow")

    def test_uncalibrated_tier_s_keeps_its_header(self):
        os.remove(os.path.join(self.root, "logs", "score_calibration.json"))
        self.dossier(tier="S", score=85, squeeze=False)
        res = self.deploy()
        self.assertDenied(res, CALIB_GATE)
        self.assertNotIn(MESSAGE, res["reason"])


if __name__ == "__main__":
    unittest.main()
