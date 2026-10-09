"""Issue #65: isolated_market_evaluator YOLO prompt polish.

Positive YOLO Barbell few-shot, ROE at the emitted leverage, YOLO in the Pending User Confirmation
verdict, leverage_standard in the C0.4 lines of the examples, and a single Barbell bullet in RULE 6.
"""
import json
import os
import re
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_MD = os.path.join(BASE_DIR, ".agents", "agents", "isolated_market_evaluator", "agent.md")
POS_03 = '<example id="eval_pos_03_yolo_barbell_approved">'


def _text():
    with open(AGENT_MD, encoding="utf-8") as f:
        return f.read()


def _rule6(text):
    return text.split("- RULE 6")[1].split("- RULE 7")[0]


def _contract_item(text, number):
    contract = text.split("<output_contract>")[1].split("</output_contract>")[0]
    return next(line for line in contract.splitlines() if line.strip().startswith(f"{number}. "))


class TestYoloPositiveFewShot(unittest.TestCase):

    def _shot(self):
        text = _text()
        self.assertEqual(text.count(POS_03), 1)
        return text.split(POS_03)[1].split("</example>")[0]

    def test_pos_03_dossier_matches_user_input(self):
        shot = self._shot()
        user_input = re.search(r"<user_input>([\s\S]*?)</user_input>", shot).group(1)
        trigger = float(re.search(r"\btrigger ([\d.]+)", user_input).group(1))
        cand_lev = int(re.search(r"\bleverage (\d+)", user_input).group(1))
        yolo_lev = int(re.search(r"\bleverage_yolo (\d+)", user_input).group(1))
        self.assertNotEqual(cand_lev, yolo_lev, "the shot must show the lower-leverage rule")
        dossier = json.loads(re.search(r"<dossier_json>([\s\S]*?)</dossier_json>", shot).group(1))
        self.assertEqual(dossier["status"], "APPROVED")
        self.assertEqual(dossier["brief_generated_at_ts"], 1790000000)
        self.assertEqual(len(dossier["approved_candidates"]), 1)
        cand = dossier["approved_candidates"][0]
        self.assertEqual([c["symbol"] for c in dossier["approved_candidates"]], dossier["approved_symbols"])
        self.assertIs(cand["is_yolo"], True)
        self.assertIs(cand["requires_user_confirmation"], True)
        self.assertEqual(cand["tier"], "A")
        self.assertEqual(cand["entry"], trigger)
        self.assertEqual(cand["leverage"], min(cand_lev, yolo_lev))
        # Issue #76.1: the candidate's own sl/tp1/tp2, stop on the losing side, TP2 >= 3.0R
        for key, field in (("sl", "stop_loss"), ("tp1", "tp1"), ("tp2", "tp2")):
            level = float(re.search(rf"\b{key} ([\d.]+)", user_input).group(1))
            self.assertEqual(cand[field], level, field)
        sign = 1 if cand["direction"] == "LONG" else -1
        risk = sign * (cand["entry"] - cand["stop_loss"])
        self.assertGreater(risk, 0, "the stop must be on the losing side of the entry")
        self.assertGreater(sign * (cand["tp1"] - cand["entry"]), 0)
        self.assertGreaterEqual(sign * (cand["tp2"] - cand["entry"]) / risk, 3.0)

    def test_pos_03_k2_evidence_and_execution_verdict(self):
        shot = self._shot()
        k2 = next(line for line in shot.splitlines() if "(YOLO) K2 Institutional volume (Barbell path)" in line)
        self.assertIn("1.0x floor", k2)
        self.assertIn("lower_wick 41% < 50%", k2)
        verdict = shot.split("## 6. Execution Verdict")[1].split("<dossier_json>")[0]
        self.assertIn("PENDING USER CONFIRMATION", verdict)

    def test_pos_03_checklist_barbell_k2_and_section_5(self):
        shot = self._shot()
        k2 = [line.strip() for line in shot.splitlines() if "(YOLO) K2 Institutional volume (Barbell path)" in line]
        self.assertEqual(len(k2), 1)
        self.assertTrue(k2[0].startswith("- [x]"), k2[0])
        self.assertIn("PASS (Barbell path)", k2[0])
        c41 = next(line for line in shot.splitlines() if "C4.1" in line)
        self.assertIn("requires_user_confirmation true", c41)
        self.assertIn("## 5. Barbell YOLO Moonshot Slot Status", shot)
        section5 = shot.split("## 5. Barbell YOLO Moonshot Slot Status")[1].split("<dossier_json>")[0]
        self.assertIn("Isolated margin", section5)
        self.assertIn("ROE at 5x", section5)
        self.assertIn("confirm", section5)

    def test_pos_03_is_example_3_and_comments_stay_sequential(self):
        text = _text()
        self.assertRegex(text, r"<!-- EXAMPLE 3: POSITIVE - YOLO [^\n]*-->\s*" + re.escape(POS_03))
        numbers = [int(n) for n in re.findall(r"<!-- EXAMPLE (\d+):", text)]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertEqual(len(numbers), text.count("<example id="))


class TestYoloRoeAndConfirmationWording(unittest.TestCase):

    def test_rule_6_roe_uses_emitted_leverage(self):
        rule6 = _rule6(_text())
        self.assertNotIn("ROE as price % x `leverage_yolo`", rule6)
        self.assertNotIn("leverage = `leverage_yolo`", rule6)
        self.assertIn("derive ROE as price % x the emitted `leverage`", rule6)
        self.assertIn("SL % x margin x the emitted `leverage`", rule6)

    def test_output_contract_item_5_roe_uses_emitted_leverage(self):
        item5 = _contract_item(_text(), 5)
        self.assertNotIn("ROE at `leverage_yolo`", item5)
        self.assertIn("ROE at the emitted `leverage`", item5)

    def test_output_contract_item_6_mentions_yolo(self):
        item6 = _contract_item(_text(), 6)
        self.assertIn("Pending User Confirmation", item6)
        self.assertIn("YOLO", item6)


class TestC04AndRule6Dedup(unittest.TestCase):

    def test_example_c04_lines_list_leverage_standard_with_leverage_yolo(self):
        examples = _text().split("<few_shot_examples>")[1].split("</few_shot_examples>")[0]
        c04 = [line for line in examples.splitlines() if "C0.4" in line and "leverage_yolo" in line]
        self.assertGreaterEqual(len(c04), 3)
        for line in c04:
            self.assertIn("leverage_standard", line)

    def test_rule_6_has_a_single_barbell_threshold_bullet(self):
        rule6 = _rule6(_text())
        bullets = [line for line in rule6.splitlines() if line.strip().startswith("* ")]
        barbell = [b for b in bullets if "2.0" in b and re.search(r"50\\?%", b)]
        self.assertEqual(len(barbell), 1, barbell)
        self.assertEqual(rule6.count("climax volume $\\ge 2.0\\times$"), 1)


if __name__ == "__main__":
    unittest.main()
