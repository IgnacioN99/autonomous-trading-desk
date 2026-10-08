"""
tests/test_harness_fast_path.py
Validates Issue #174 Fast-Path invariants:
1. Fast-Path Market Scan Invariant is present in AGENTS.md, trading.md, and SKILL.md.
2. AGENTS.md does not instruct agents to call view_file on research/ notebooks or SKILL.md during scans.
3. Clean-room evaluator flow and pre-trade hook contracts remain intact.
4. AGENTS.md byte cap (< 22,000 bytes) is respected.
"""

import os
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
