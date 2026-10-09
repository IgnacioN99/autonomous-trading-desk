#!/usr/bin/env python3
"""
Issue #146 (bundled #142, #76, #29): evaluator prompt and market-radar skill text.

1. #146: a brief whose `market_data_status` starts with UNAVAILABLE (Binance rate-limit ban) is a data outage, not a
   quiet market: NEUTRAL, nothing approved, summary `MARKET_DATA_UNAVAILABLE:` + the retry time; STALE_BRIEF,
   ENV_MISMATCH and DAILY_LOSS_GATE take precedence. Negative few-shot EXAMPLE 15. The skill forbids scan retries
   during the ban.
2. #142: every evaluated K3 line names its reference (`trigger_price` / YOLO `trigger` to `tp1`) and its stated
   distance matches those levels; the standard approved shots follow the radar levels (TP1 >= 1.8R, TP2 = 4.0R).
3. #29: every `Tier X` token in the prompt is a schema tier (S, A+, A); RULE 3 has no Tier B.
4. #76.5: contract item 7 sources `leverage` from leverage_standard (standard) or the candidate (YOLO).

Text-only checks: no network, nothing written.
Run: python3 -m unittest tests.test_issue_146_market_data_outage_prompt -v
"""

import json
import os
import re
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils import dossier_provenance as dp  # noqa: E402
from utils import rate_limit_guard as rlg  # noqa: E402

AGENT_MD = os.path.join(BASE_DIR, ".agents", "agents", "isolated_market_evaluator", "agent.md")
CLAUDE_MD = os.path.join(BASE_DIR, ".claude", "agents", "isolated_market_evaluator.md")
SKILL_MD = os.path.join(BASE_DIR, ".agents", "skills", "market-radar", "SKILL.md")
CLAUDE_SKILL_MD = os.path.join(BASE_DIR, ".claude", "skills", "market-radar", "SKILL.md")
OUTAGE_ID = '<example id="eval_neg_09_market_data_unavailable">'
OUTAGE_C42 = "- [x] C4.2 Overall status: MARKET_DATA_UNAVAILABLE -> NEUTRAL"
RETRY_RE = re.compile(r"retry after (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ|unknown)")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _example(text, example_id):
    return text.split(f'<example id="{example_id}">')[1].split("</example>")[0]


def _final(shot):
    return re.search(r"<final_response>([\s\S]*?)</final_response>", shot).group(1)


def _dossier(final):
    return json.loads(dp.DOSSIER_RE.search(final).group(1))


class TestOutageRule(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _read(AGENT_MD)

    def test_input_brief_rule(self):
        protocol = self.text.split("<input_brief_protocol>")[1].split("</input_brief_protocol>")[0]
        rule = next(l for l in protocol.splitlines() if l.startswith("5. MARKET DATA OUTAGE:"))
        for fragment in ("`brief.market_data_status` is a string starting with `UNAVAILABLE`",
                         "blind, not quiet", '`status: "NEUTRAL"`', "approve no candidate, even if the brief lists one",
                         "`MARKET_DATA_UNAVAILABLE:`", "wait until the retry time, copied verbatim from the status",
                         "(`unknown` when the status says unknown)",
                         "A null or missing `market_data_status` means no outage."):
            self.assertIn(fragment, rule)
        # Precedence: the REJECTED stops win over the outage
        # Only the scope-all daily loss gate wins; C1.3 YOLO (scope yolo) still gets the outage verdict
        self.assertIn("Once C0.2 and C0.3 pass and C1.3 is not ACTIVE (scope all)", rule)
        self.assertIn("(STALE_BRIEF, ENV_MISMATCH and DAILY_LOSS_GATE take precedence)", rule)
        # The prompt cannot enforce a rescan policy on the parent: it only says to wait
        self.assertNotIn("rescan", rule.lower())

    def test_checklist_stop_and_status_lines(self):
        stop = next(l for l in self.text.splitlines() if l.startswith("Omit no check group"))
        self.assertIn(f"stop after C1 too and close with `{OUTAGE_C42}`", stop)
        # The pinned sentences of the other stops are unchanged
        self.assertIn("likewise after C1 when C1.3 is ACTIVE (`DAILY_LOSS_GATE:`)", stop)
        self.assertIn("<STALE_BRIEF|ENV_MISMATCH|DAILY_LOSS_GATE>", stop)
        c42 = next(l for l in self.text.splitlines() if l.strip().startswith("- C4.2 Overall status"))
        self.assertIn("or C1.3 ACTIVE) / NEUTRAL (nothing to evaluate, or a market data outage: "
                      "`MARKET_DATA_UNAVAILABLE:`)", c42)

    def test_output_contract(self):
        contract = self.text.split("<output_contract>")[1].split("</output_contract>")[0]
        neutral = next(l for l in contract.splitlines() if l.strip().startswith("* NEUTRAL:"))
        self.assertIn("market data outage (`market_data_status` starts with `UNAVAILABLE`)", neutral)
        summary = next(l for l in contract.splitlines() if l.strip().startswith("- `summary`:"))
        self.assertIn("`ENV_MISMATCH:`, `DAILY_LOSS_GATE:` or `BRIEF_FILE_UNAVAILABLE:` when applicable).", summary)
        self.assertIn("A market data outage uses `MARKET_DATA_UNAVAILABLE:` plus the retry time", summary)

    def test_generated_copy_carries_the_rule_and_the_shot(self):
        claude = _read(CLAUDE_MD)
        self.assertIn("5. MARKET DATA OUTAGE:", claude)
        self.assertIn(OUTAGE_ID, claude)
        self.assertIn(OUTAGE_C42, claude)


class TestOutageFewShot(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _read(AGENT_MD)
        cls.shot = _example(cls.text, "eval_neg_09_market_data_unavailable")
        cls.final = _final(cls.shot)
        cls.dossier = _dossier(cls.final)

    def test_is_example_15_and_the_last_shot(self):
        self.assertEqual(self.text.count(OUTAGE_ID), 1)
        self.assertRegex(self.text, r"<!-- EXAMPLE 15: NEGATIVE - MARKET DATA OUTAGE [^\n]*-->\s*" + re.escape(OUTAGE_ID))
        examples = self.text.split("<few_shot_examples>")[1].split("</few_shot_examples>")[0]
        self.assertEqual(re.findall(r'<example id="([^"]+)">', examples)[-1], "eval_neg_09_market_data_unavailable")

    def test_scenario_uses_the_guard_status_format(self):
        scenario = self.shot.split("<scenario>")[1].split("</scenario>")[0]
        status = re.search(r'`market_data_status: "([^"]+)"`', scenario).group(1)
        self.assertTrue(status.startswith(rlg.UNAVAILABLE_PREFIX), status)
        self.assertRegex(status, r"^UNAVAILABLE: Binance rate limit \(HTTP (429|418)\), retry after "
                                 r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_dossier_is_neutral_empty_and_carries_the_retry_time(self):
        d = self.dossier
        self.assertEqual(d["status"], "NEUTRAL")
        self.assertEqual(d["approved_symbols"], [])
        self.assertEqual(d["approved_candidates"], [])
        self.assertNotIn('"score": ', self.shot)
        self.assertTrue(d["summary"].startswith("MARKET_DATA_UNAVAILABLE:"), d["summary"])
        retry = RETRY_RE.search(self.shot.split("<scenario>")[1].split("</scenario>")[0]).group(1)
        self.assertIn(retry, d["summary"])
        self.assertIn(f"wait until {retry}", d["summary"])
        self.assertNotIn("rescan", d["summary"].lower())

    def test_minimal_checklist_passes_the_recorder_check(self):
        lines = [l.strip() for l in self.final.splitlines() if l.strip().startswith("- [")]
        ids = [re.match(r"- \[[ x]\] (\S+)", l).group(1) for l in lines]
        self.assertEqual(ids, ["C0.1", "C0.2", "C0.3", "C0.4", "C1.1", "C1.2", "C1.3", "C4.2"])
        self.assertIn("- [x] C1.3 Daily loss gate: blocked false -> NOT ACTIVE", lines)
        self.assertTrue(lines[-1].startswith("- [x] C4.2 Overall status: MARKET_DATA_UNAVAILABLE"), lines[-1])
        self.assertTrue(lines[-1].endswith("-> NEUTRAL"), lines[-1])
        self.assertEqual(dp.check_precondition_checklist(self.final, self.dossier), [])

    def test_contrasts_with_the_empty_radar_shot(self):
        quiet = _dossier(_final(_example(self.text, "eval_neu_01_no_candidates")))
        self.assertEqual(quiet["status"], "NEUTRAL")
        self.assertFalse(quiet["summary"].startswith("MARKET_DATA_UNAVAILABLE:"))
        self.assertNotEqual(quiet["summary"], self.dossier["summary"])


class TestMarketRadarSkill(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.skill = _read(SKILL_MD)
        cls.validation = cls.skill.split("## Validation")[1]

    def test_validation_forbids_retries_during_a_ban(self):
        self.assertIn("Never retry a scan while `market_data_status` starts with `UNAVAILABLE`", self.validation)
        self.assertIn("Wait for the `retry after` time", self.validation)
        # The one-retry allowance excludes the ban case
        self.assertIn("retry once only for a transient network error without an `UNAVAILABLE`", self.validation)
        self.assertNotIn("retry once for transient network errors", self.skill)

    def test_pinned_substrings_survive_the_trim(self):
        for text in ("If `gate_ok` is false, NEVER propose, recommend or forward that row.",
                     "SL distance × leverage ≤ 0.35", "3.75 USDT", "desk floors, not executor gates",
                     "(ask − bid) / mid × 100", "ambiguous_symbols", "wide_spread", "market_data_status",
                     "underlyingSubType", "tickSize", "rounding_margin"):
            self.assertIn(text, self.skill)
        # The maintainer-only rounding explanation is gone
        self.assertNotIn("about +50% on a ~0.1% stop", self.skill)
        for word in ("invoke_subagent", "send_message", "TypeName", "conversationId"):
            self.assertNotIn(word, self.skill)

    def test_generated_skill_copy(self):
        self.assertIn("Never retry a scan while `market_data_status` starts with `UNAVAILABLE`", _read(CLAUDE_SKILL_MD))


class TestK3ReferenceAndLevels(unittest.TestCase):
    """Issue #142."""

    @classmethod
    def setUpClass(cls):
        cls.text = _read(AGENT_MD)
        cls.examples = cls.text.split("<few_shot_examples>")[1].split("</few_shot_examples>")[0]

    def test_every_evaluated_k3_line_names_its_reference(self):
        lines = [l.strip() for l in self.examples.splitlines() if " K3 Friction:" in l and not l.rstrip().endswith("-> N/A")]
        self.assertGreaterEqual(len(lines), 6)
        for line in lines:
            m = re.search(r"TP1 distance ([\d.]+)% \((trigger_price|trigger) ([\d.]+) to tp1 ([\d.]+)", line)
            self.assertIsNotNone(m, line)
            stated, entry, tp1 = float(m.group(1)), float(m.group(3)), float(m.group(4))
            self.assertLess(abs(abs(tp1 - entry) / entry * 100 - stated), 0.05, line)
            self.assertGreaterEqual(stated, 0.50, line)

    def test_k3_line_matches_each_approved_dossier(self):
        for final in re.findall(r"<final_response>([\s\S]*?)</final_response>", self.examples):
            dossier = _dossier(final)
            for cand in dossier["approved_candidates"]:
                k3 = next(l for l in final.splitlines() if f"{cand['symbol']} {cand['direction']}" in l and " K3 " in l)
                m = re.search(r"\((?:trigger_price|trigger) ([\d.]+) to tp1 ([\d.]+)", k3)
                self.assertEqual((float(m.group(1)), float(m.group(2))), (cand["entry"], cand["tp1"]), k3)

    def test_standard_approved_shots_follow_the_radar_levels(self):
        seen = set()
        for final in re.findall(r"<final_response>([\s\S]*?)</final_response>", self.examples):
            for cand in _dossier(final)["approved_candidates"]:
                if cand["is_yolo"]:
                    continue
                sign = 1 if cand["direction"] == "LONG" else -1
                risk = sign * (cand["entry"] - cand["stop_loss"])
                self.assertGreater(risk, 0, cand["symbol"])
                self.assertGreaterEqual(sign * (cand["tp1"] - cand["entry"]) / risk, 1.79, cand["symbol"])
                self.assertAlmostEqual(sign * (cand["tp2"] - cand["entry"]) / risk, 4.0, delta=0.01, msg=cand["symbol"])
                seen.add(cand["symbol"])
        self.assertTrue({"FILUSDT", "SOLUSDT", "RLCUSDT", "NEARUSDT"} <= seen, seen)

    def test_sol_example_figures_are_consistent(self):
        shot = _example(self.text, "eval_pos_02_tier_a_plus_confirmation")
        cand = _dossier(_final(shot))["approved_candidates"][0]
        risk = cand["entry"] - cand["stop_loss"]
        self.assertGreaterEqual((cand["tp1"] - cand["entry"]) / risk, 1.8)
        self.assertAlmostEqual((cand["tp2"] - cand["entry"]) / risk, 4.0, places=6)
        for stale in ("R:R 3.2", "3.2:1", "1.1%", "143.70", "149.20"):
            self.assertNotIn(stale, shot)
        self.assertIn("R:R 4.0 >= 3:1 (Tier A+ path)", shot)
        self.assertIn("| 4.0:1 |", shot)

    def test_fil_distance_matches_its_levels(self):
        shot = _example(self.text, "eval_pos_01_tier_s_approved")
        self.assertNotIn("2.1%", shot)
        self.assertIn("TP1 -2.98%", shot)


class TestTierTokensAndLeverageSource(unittest.TestCase):
    """Issues #29 and #76.5."""

    @classmethod
    def setUpClass(cls):
        cls.text = _read(AGENT_MD)

    def test_every_tier_token_is_a_schema_tier(self):
        tokens = set(re.findall(r"\bTier ([A-Za-z]+\+?)", self.text))
        self.assertTrue(tokens, "no Tier tokens found")
        self.assertLessEqual(tokens, {"S", "A+", "A"}, tokens)
        self.assertNotIn("Tier B", self.text)

    def test_rule_3_fake_tier_s_outcomes(self):
        rule3 = self.text.split("- RULE 3")[1].split("- RULE 4")[0]
        self.assertIn("dry volume (`vol_ratio < 1.0x`) is REJECTED as FAKE_TIER_S", rule3)
        self.assertIn("downgrade it to Tier A+/A only if the A-tier K2 path holds (absorption >= 55% with R:R >= 3:1)",
                      rule3)
        self.assertIn("otherwise reject it", rule3)

    def test_contract_leverage_source(self):
        contract = self.text.split("<output_contract>")[1].split("</output_contract>")[0]
        self.assertNotIn("(integer from the risk profile)", contract)
        self.assertIn("`leverage` (integer: `leverage_standard` from the risk profile; YOLO: the candidate's, "
                      "capped as below)", contract)


if __name__ == "__main__":
    unittest.main()
