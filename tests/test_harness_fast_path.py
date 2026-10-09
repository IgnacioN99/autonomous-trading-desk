"""
tests/test_harness_fast_path.py
Validates Issue #174 Fast-Path invariants:
1. Fast-Path Market Scan Invariant is present in AGENTS.md, trading.md, and SKILL.md.
2. AGENTS.md does not instruct agents to call view_file on research/ notebooks or SKILL.md during scans.
3. Clean-room evaluator flow and pre-trade hook contracts remain intact.
4. AGENTS.md byte cap (< 22,000 bytes) is respected.
5. The generated Claude copy of the planner skill keeps the fast-path wording (#177), every generated
   skill tells to load sibling skills with the Skill tool (#81), and the planner pins the resting-entry
   and close guidance (#44, #112).
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scripts.dev import sync_claude_assets as gen  # noqa: E402


class TestHarnessFastPath(unittest.TestCase):

    def test_agents_md_fast_path_and_byte_cap(self):
        agents_path = os.path.join(BASE_DIR, "AGENTS.md")
        self.assertTrue(os.path.exists(agents_path))

        with open(agents_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Byte cap invariant (< 22,000 bytes)
        self.assertLess(len(content.encode("utf-8")), 22000)

        # Fast-Path directive present
        self.assertIn("Fast-Path Market Scan Invariant", content)
        self.assertIn("MUST NOT call `view_file` on `SKILL.md`, `research/`", content)
        self.assertIn("scripts/prime_evaluator_brief.py", content)

        # Verbose reading list of research files removed from Phase 1
        self.assertNotIn('1. `"Bitcoin Volatility & Market Microstructure"`', content)
        self.assertNotIn("research/01_kelly_criterion_crypto_risk.md", content)

    def test_trading_rules_fast_path(self):
        trading_path = os.path.join(BASE_DIR, ".agents", "rules", "trading.md")
        self.assertTrue(os.path.exists(trading_path))

        with open(trading_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Fast-Path Invariant", content)
        self.assertIn("NUNCA ejecutes `view_file` sobre `research/`", content)

    def test_skill_md_fast_path(self):
        skill_path = os.path.join(BASE_DIR, ".agents", "skills", "trade-execution-planner", "SKILL.md")
        self.assertTrue(os.path.exists(skill_path))

        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Deterministic Fast-Path", content)
        self.assertIn("Do NOT call `view_file` on `research/`", content)

        claude_path = os.path.join(BASE_DIR, ".claude", "skills", "trade-execution-planner", "SKILL.md")
        self.assertTrue(os.path.exists(claude_path))
        with open(claude_path, "r", encoding="utf-8") as f:
            claude_content = f.read()
        self.assertIn("Deterministic Fast-Path", claude_content)
        self.assertIn("Do NOT call `view_file` on `research/`", claude_content)

    def test_generated_skills_say_to_load_siblings_with_skill_tool(self):
        skills_dir = os.path.join(BASE_DIR, ".claude", "skills")
        self.assertIn("trade-execution-planner", gen.SKILLS)
        for name in gen.SKILLS:
            with open(os.path.join(skills_dir, name, "SKILL.md"), "r", encoding="utf-8") as f:
                text = f.read()
            self.assertIn("Load sibling skills", text, name)
            self.assertIn("Skill tool", text, name)

    def test_planner_resting_entry_and_close_guidance(self):
        skill_path = os.path.join(BASE_DIR, ".agents", "skills", "trade-execution-planner", "SKILL.md")
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()
        step7 = content.split("7. Field-by-field checklist", 1)[1].split("8. **Manage open positions**", 1)[0]
        self.assertIn("Entry type: a resting entry (`STOP_MARKET` / `LIMIT`)", step7)
        step8 = content.split("8. **Manage open positions**", 1)[1]
        self.assertIn("--interval 60 --env <env>", step8)
        self.assertIn("WRONG", step8)
        self.assertIn("RIGHT", step8)

    def test_clean_room_evaluator_flow_intact(self):
        agents_path = os.path.join(BASE_DIR, "AGENTS.md")
        with open(agents_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Invariants of Layer 4 and pre-trade guard remain intact
        self.assertIn("Layer 4: Clean-Room Quantitative Evaluator", content)
        self.assertIn("isolated_market_evaluator", content)
        self.assertIn("record_evaluation.py --from-subagent", content)
        self.assertIn("latest_dossier.json", content)


if __name__ == "__main__":
    unittest.main()
