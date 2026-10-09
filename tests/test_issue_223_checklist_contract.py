#!/usr/bin/env python3
"""
Issue #223: evaluator checklist contract (follow-up of #27 / PR #222).

1. check_precondition_checklist reads the K4 result token of every approved candidate: '[x]' AND APPROVED or
   DOWNGRADED (REJECTED, PASS or no result is a problem).
2. agent.md: K4 DOWNGRADED is '[x]' (checkbox semantics + EXAMPLE 14) and the checklist must be in the same final
   message as the <dossier_json> block (tool protocol, output contract and the generated Claude copy).
3. Trade time: rebuild_verified_record (reached by the guard and the executor through validate_dossier_for_trade,
   PROD) re-runs the checker on the re-extracted transcript, so a placed record pointing at a real transcript with
   no or an inconsistent checklist is refused. REJECTED/NEUTRAL and TESTNET (no require_provenance) are unaffected.

Offline: urllib is blocked, transcripts and workspaces live in temp dirs.
Run: python3 -m unittest tests.test_issue_223_checklist_contract -v
"""

import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BASE_DIR, os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "hooks"),
           os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_guard_bypasses as tgb  # noqa: E402  (fixtures only; imported first, see test_issue_79)
import execute_futures_trade as eft  # noqa: E402
import record_evaluation as rec  # noqa: E402
from utils import dossier_provenance as dp  # noqa: E402
from dossier_checklist_fixture import checklist_for  # noqa: E402
from test_dossier_provenance import (APPROVED_SHORT, ClaudeTranscriptFixture as _ClaudeFixture,  # noqa: E402
                                     dossier_text)
from scripts.dev import sync_claude_assets as gen  # noqa: E402

AGENT_MD = os.path.join(BASE_DIR, ".agents", "agents", "isolated_market_evaluator", "agent.md")
CLAUDE_MD = os.path.join(BASE_DIR, ".claude", "agents", "isolated_market_evaluator.md")
K4_LINE = "- [x] FILUSDT SHORT K4 Verdict: K1-K3 PASS -> APPROVED (Tier S)"
SAME_MESSAGE = ("The `## Precondition Checklist` and the `<dossier_json>` block must be in the same final message "
                "(one `send_message` call); a checklist sent in an earlier message is not read and an APPROVED "
                "dossier is then refused in PROD.")
K4_SEMANTICS = "K4 is `[x]` when its result is APPROVED or DOWNGRADED and `[ ]` when REJECTED."


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    tgb.setUpModule()
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# =============================================================================
# 1. K4 result token
# =============================================================================
class TestK4ResultToken(unittest.TestCase):

    def problems(self, k4_line, payload=APPROVED_SHORT):
        base = checklist_for(payload)
        self.assertIn(K4_LINE, base)
        return dp.check_precondition_checklist(dossier_text(payload, checklist=base.replace(K4_LINE, k4_line)),
                                               payload)

    def test_checked_rejected_is_a_problem(self):
        problems = self.problems("- [x] FILUSDT SHORT K4 Verdict: K2 FAIL -> REJECTED (dry volume)")
        self.assertEqual(problems, ["approved FILUSDT SHORT: K4 result 'REJECTED' is not APPROVED or DOWNGRADED"])

    def test_checked_downgraded_is_consistent(self):
        self.assertEqual(self.problems("- [x] FILUSDT SHORT K4 Verdict: K1-K3 PASS, lesson -> DOWNGRADED (Tier A)"), [])
        self.assertEqual(self.problems("- [X] FILUSDT SHORT K4 Verdict: ok -> **downgraded** (Tier A)."), [])

    def test_checked_pass_or_no_result_is_a_problem(self):
        problems = self.problems("- [x] FILUSDT SHORT K4 gate -> PASS")
        self.assertEqual(problems, ["approved FILUSDT SHORT: K4 result 'PASS' is not APPROVED or DOWNGRADED"])
        for line in ("- [x] FILUSDT SHORT K4 Verdict: APPROVED", "- [x] FILUSDT SHORT K4 Verdict ->"):
            with self.subTest(line=line):
                self.assertEqual(self.problems(line),
                                 ["approved FILUSDT SHORT: K4 result 'MISSING' is not APPROVED or DOWNGRADED"])

    def test_unchecked_downgraded_is_a_problem(self):
        problems = self.problems("- [ ] FILUSDT SHORT K4 Verdict: lesson -> DOWNGRADED (Tier A)")
        self.assertEqual(problems, ["approved FILUSDT SHORT: K4 is not checked [x]"])

    def test_last_arrow_decides_and_every_k4_line_counts(self):
        self.assertEqual(self.problems("- [x] FILUSDT SHORT K4 Verdict: K1 -> PASS, K2 -> PASS -> APPROVED (Tier S)"), [])
        self.assertEqual(len(self.problems("- [x] FILUSDT SHORT K4 Verdict: APPROVED -> REJECTED (catalyst)")), 1)
        dup = K4_LINE + "\n- [x] FILUSDT SHORT K4 Verdict: re-check -> REJECTED (catalyst)"
        self.assertEqual(self.problems(dup),
                         ["approved FILUSDT SHORT: K4 result 'REJECTED' is not APPROVED or DOWNGRADED"])

    def test_arrows_inside_the_tier_note_or_evidence_do_not_hide_the_verdict(self):
        """Round 2: arrows in the evidence or the tier note do not hide the APPROVED / DOWNGRADED verdict."""
        ok = ("- [x] FILUSDT SHORT K4 Verdict: K1-K3 PASS, lesson -> DOWNGRADED (Tier A+ -> A)",
              "- [x] FILUSDT SHORT K4 Verdict: K1-K3 PASS -> APPROVED (Tier S)",
              "- [x] FILUSDT SHORT K4 Verdict: C3.1 already checked -> N/A, K1-K3 PASS -> APPROVED (Tier S)",
              "- [x] FILUSDT SHORT K4 Verdict: K2 S -> A+ path -> Downgraded: (Tier S -> A+)")
        for line in ok:
            with self.subTest(line=line):
                self.assertEqual(self.problems(line), [])
        for line, token in (("- [x] FILUSDT SHORT K4 Verdict: K1 BLOCKED -> REJECTED (gate)", "REJECTED"),
                            ("- [x] FILUSDT SHORT K4 Verdict: K2 -> FAIL -> REJECTED (Tier S -> A)", "REJECTED"),
                            ("- [x] FILUSDT SHORT K4 Verdict: K1 -> N/A -> PASS", "PASS")):
            with self.subTest(line=line):
                self.assertEqual(self.problems(line), [f"approved FILUSDT SHORT: K4 result '{token}' is not "
                                                       f"APPROVED or DOWNGRADED"])

    def test_rejected_after_any_arrow_is_refused(self):
        """Round 3: a later APPROVED/DOWNGRADED never masks a REJECTED that follows an earlier '->'."""
        for line in ("- [x] FILUSDT SHORT K4 Verdict: -> REJECTED (K2 -> APPROVED path not taken)",
                     "- [x] FILUSDT SHORT K4 Verdict: K1 -> rejected, -> DOWNGRADED (Tier A)",
                     "- [x] FILUSDT SHORT K4 Verdict: K3 -> **REJECTED**. -> APPROVED (Tier S)"):
            with self.subTest(line=line):
                self.assertEqual(self.problems(line), ["approved FILUSDT SHORT: K4 result 'REJECTED' is not "
                                                       "APPROVED or DOWNGRADED"])
        for line in ("- [x] FILUSDT SHORT K4 Verdict: K1-K3 PASS, lesson -> DOWNGRADED (Tier A+ -> A)",
                     "- [x] FILUSDT SHORT K4 Verdict: K1 evidence -> N/A, K1-K3 PASS -> APPROVED (Tier S)",
                     "- [x] FILUSDT SHORT K4 Verdict: no REJECTED gate, K1-K3 PASS -> APPROVED (Tier S)"):
            with self.subTest(line=line):
                self.assertEqual(self.problems(line), [])

    def test_rejected_k4_of_a_candidate_not_in_the_block_is_ignored(self):
        extra = K4_LINE + "\n- [ ] TRXUSDT LONG K4 Verdict: K2 FAKE_TIER_S -> REJECTED (dry volume)"
        self.assertEqual(self.problems(extra), [])


# =============================================================================
# 2. Prompt contract (agent.md and the generated Claude copy)
# =============================================================================
class TestPromptContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _read(AGENT_MD)

    def test_k4_checkbox_semantics(self):
        semantics = next(l for l in self.text.splitlines() if l.startswith("- Checkbox semantics"))
        self.assertIn(K4_SEMANTICS, semantics)
        self.assertIn("YOLO candidates REJECTED, standard ones stay approvable", semantics)

    def test_same_message_rule_in_protocol_and_output_contract(self):
        protocol = self.text.split("<tool_use_protocol>")[1].split("</tool_use_protocol>")[0]
        step5 = next(l for l in protocol.splitlines() if l.startswith("5. `send_message` is used ONCE"))
        self.assertIn(SAME_MESSAGE, step5)
        contract = self.text.split("<output_contract>")[1].split("</output_contract>")[0]
        delivery = next(l for l in contract.splitlines() if l.startswith("8. DELIVERY:"))
        self.assertIn(SAME_MESSAGE, delivery)

    def test_generated_claude_copy_carries_both_rules(self):
        claude = _read(CLAUDE_MD)
        self.assertIn(K4_SEMANTICS, claude)
        self.assertEqual(claude.count(SAME_MESSAGE), 2)
        same_block = "in the same final text block of your final response"
        self.assertIn(same_block, gen.EVALUATOR_DELIVERY)
        runtime = claude.split("<claude_code_runtime>")[1].split("</claude_code_runtime>")[0]
        self.assertIn(same_block, runtime)
        self.assertIn("refused in PROD", runtime.split(same_block)[1])

    def test_downgraded_few_shot(self):
        self.assertIn("<!-- EXAMPLE 14: POSITIVE - TIER A+ DOWNGRADED TO A BY A COMMITTED LESSON", self.text)
        body = self.text.split('<example id="eval_pos_05_lesson_downgrade_tier_a">')[1].split("</example>")[0]
        final = re.search(r"<final_response>([\s\S]*?)</final_response>", body).group(1)
        k4 = [l.strip() for l in final.splitlines() if "NEARUSDT LONG K4" in l]
        self.assertEqual(len(k4), 1)
        self.assertTrue(k4[0].startswith("- [x] NEARUSDT LONG K4 Verdict:"), k4)
        self.assertTrue(k4[0].endswith("-> DOWNGRADED (Tier A)"), k4)
        self.assertIn("RULE 11", k4[0])
        dossier = json.loads(dp.DOSSIER_RE.search(final).group(1))
        cand = dossier["approved_candidates"][0]
        self.assertEqual((dossier["status"], cand["tier"], cand["score"]), ("APPROVED", "A", 72))
        self.assertIs(cand["requires_user_confirmation"], True)
        self.assertIn("NEARUSDT Tier A, confidence 72 = score 72", final)
        self.assertEqual(dp.check_precondition_checklist(final, dossier), [])
        # The same approval with K4 left '[ ]' (the ambiguity this issue removes) is refused
        unchecked = final.replace("- [x] NEARUSDT LONG K4", "- [ ] NEARUSDT LONG K4")
        self.assertEqual(dp.check_precondition_checklist(unchecked, dossier),
                         ["approved NEARUSDT LONG: K4 is not checked [x]"])


# =============================================================================
# 3. Trade-time re-check (validate_dossier_for_trade / rebuild_verified_record)
# =============================================================================
class TestTradeTimeRecheck(_ClaudeFixture):

    def place_agy(self, conv_id, payload, checklist=True):
        """Evaluator transcript + record built from it without the recorder (a placed latest_dossier.json whose
        provenance sha matches the transcript)."""
        ts = self.now - 30
        steps = [self.system_step(ts - 5)] + self.view_file_steps(ts - 3) + \
            [self.send_message_step(ts, dossier_text(payload, checklist=checklist))]
        return self.place(dp.extract_dossier_from_transcript(self.write_transcript(conv_id, steps)))

    def place(self, extracted):
        record = dp.build_record_from_extraction(extracted)
        with open(dp.default_dossier_path(self.workspace), "w", encoding="utf-8") as f:
            json.dump(record, f)
        return record

    def assertRefused(self, fragment, symbol="FILUSDT", direction="SHORT"):
        ok, reason, cand = self.validate(symbol, direction)
        self.assertFalse(ok, reason)
        self.assertIsNone(cand)
        self.assertIn("precondition checklist inconsistent", reason)
        self.assertIn(fragment, reason)

    def test_placed_record_without_checklist_refused_in_prod(self):
        record = self.place_agy("dededede-2230-0000-0000-000000000001", APPROVED_SHORT, checklist=False)
        self.assertRefused("missing ## Precondition Checklist")
        ok, reason, _ = dp.rebuild_verified_record(record)
        self.assertFalse(ok)
        self.assertIn("missing ## Precondition Checklist", reason)

    def test_placed_record_with_inconsistent_checklist_refused_in_prod(self):
        for i, (bad, fragment) in enumerate((
                (K4_LINE.replace("-> APPROVED (Tier S)", "-> REJECTED (catalyst)"), "K4 result 'REJECTED'"),
                (K4_LINE.replace("- [x]", "- [ ]"), "K4 is not checked [x]"),
                (K4_LINE + "\n- [ ] FILUSDT SHORT K2 Institutional volume -> FAKE_TIER_S", "K2 is not checked"))):
            with self.subTest(fragment=fragment):
                self.place_agy(f"dededede-2230-0000-0000-00000000001{i}", APPROVED_SHORT,
                               checklist=checklist_for(APPROVED_SHORT).replace(K4_LINE, bad))
                self.assertRefused(fragment)

    def test_consistent_checklist_passes(self):
        self.place_agy("dededede-2230-0000-0000-000000000021", APPROVED_SHORT)
        ok, reason, cand = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)
        self.assertEqual(cand["symbol"], "FILUSDT")
        downgraded = checklist_for(APPROVED_SHORT).replace(K4_LINE, K4_LINE.replace("APPROVED (Tier S)",
                                                                                    "DOWNGRADED (Tier A)"))
        self.place_agy("dededede-2230-0000-0000-000000000022", APPROVED_SHORT, checklist=downgraded)
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)

    def test_recorded_dossier_still_passes(self):
        self.record_prod("dededede-2230-0000-0000-000000000031", APPROVED_SHORT)
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)

    def test_rejected_and_neutral_without_checklist_unaffected(self):
        for i, status in enumerate(("REJECTED", "NEUTRAL")):
            with self.subTest(status=status):
                payload = {"status": status, "target_env": "PROD", "approved_candidates": [], "summary": "none"}
                record = self.place_agy(f"dededede-2230-0000-0000-00000000004{i}", payload, checklist=False)
                ok, reason, rebuilt = dp.rebuild_verified_record(record)
                self.assertTrue(ok, reason)
                self.assertEqual(rebuilt["status"], status)
                ok, reason, _ = self.validate("FILUSDT", "SHORT")
                self.assertFalse(ok)
                self.assertIn(f"status is '{status}'", reason)  # the existing status gate, not the checklist

    def test_testnet_without_require_provenance_unaffected(self):
        payload = dict(APPROVED_SHORT, target_env="TESTNET")
        self.place_agy("dededede-2230-0000-0000-000000000051", payload, checklist=False)
        ok, reason, _ = self.validate("FILUSDT", "SHORT", env="testnet")
        self.assertTrue(ok, reason)
        # Forcing require_provenance (the PROD default) applies the re-check in any environment
        ok, reason, _ = dp.validate_dossier_for_trade("FILUSDT", "SHORT", env="testnet", base_dir=self.workspace,
                                                      require_provenance=True)
        self.assertFalse(ok)
        self.assertIn("precondition checklist inconsistent", reason)

    def test_claude_checklist_in_an_earlier_text_block_refused(self):
        """Same-message rule: the checker reads only the text block that holds the <dossier_json> block."""
        agent = "a2230000000000001"
        ts = self.now - 30
        rows = [self.user_row(ts - 5, "Evaluate logs/primed_brief.json for PROD.")] + self.read_rows(ts - 3) + [
            self.text_row(ts - 1, "# QUANTITATIVE EVALUATION MASTER DOSSIER\n" + checklist_for(APPROVED_SHORT)),
            self.text_row(ts, dossier_text(APPROVED_SHORT, header="", checklist=False))]
        self.place(dp.extract_dossier_from_claude_transcript(self.write_claude(agent, rows)))
        self.assertRefused("missing ## Precondition Checklist")
        # Round 2: two text blocks of the SAME assistant row are still two messages for the checker
        agent = "a2230000000000003"
        row = self.text_row(ts, "")
        row["message"]["content"] = [
            {"type": "text", "text": "# QUANTITATIVE EVALUATION MASTER DOSSIER\n" + checklist_for(APPROVED_SHORT)},
            {"type": "text", "text": dossier_text(APPROVED_SHORT, header="", checklist=False)}]
        rows = [self.user_row(ts - 5, "Evaluate logs/primed_brief.json for PROD.")] + self.read_rows(ts - 3) + [row]
        extracted = dp.extract_dossier_from_claude_transcript(self.write_claude(agent, rows))
        self.assertNotIn("## Precondition Checklist", extracted["final_text"])
        self.place(extracted)
        self.assertRefused("missing ## Precondition Checklist")
        # Same content in one text block passes
        agent = "a2230000000000002"
        self.place(dp.extract_dossier_from_claude_transcript(self.standard_claude(agent, APPROVED_SHORT)))
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)


# =============================================================================
# 4. Guard and executor reach the re-check through validate_dossier_for_trade
# =============================================================================
class TestGuardAndExecutorRecheck(tgb.GuardHarness):

    def strip_checklist(self, record):
        """Rewrites the evaluator message without its checklist; the block (and so the stored sha) is unchanged."""
        path = record["provenance"]["transcript_path"]
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        content = rows[1]["content"]
        rows[1]["content"] = "Master Dossier\n" + content[content.index("<dossier_json>"):]
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        self.assertEqual(dp.extract_dossier_from_transcript(path)["sha256"], record["provenance"]["sha256"])

    def deploy(self):
        return self.agy(self.cmd("python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG "
                                 "--leverage 3 --env prod", conversationId=tgb.PARENT_CONV_ID))

    def executor(self):
        return eft.enforce_evaluation_dossier("BTCUSDT", "LONG", "prod", confirmed=True, base_dir=self.root)

    def test_guard_refuses_placed_record_without_checklist(self):
        self.strip_checklist(self.write_provenance_dossier())
        self.assertDenied(self.deploy(), "precondition checklist inconsistent")

    def test_executor_refuses_placed_record_without_checklist(self):
        self.strip_checklist(self.write_provenance_dossier())
        ok, reason, _ = self.executor()
        self.assertFalse(ok)
        self.assertIn("Evaluation Gate, PROD", reason)
        self.assertIn("precondition checklist inconsistent", reason)

    def test_consistent_checklist_allowed_by_guard_and_executor(self):
        self.write_provenance_dossier()
        self.assertEqual(self.deploy().get("decision"), "allow")
        ok, reason, _ = self.executor()
        self.assertTrue(ok, reason)

    def test_recorder_still_refuses_k4_rejected_in_prod(self):
        """Record time (issue #27) applies the same K4 token rule: an approved candidate with K4 REJECTED."""
        record = self.write_provenance_dossier()
        path = record["provenance"]["transcript_path"]
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        rows[1]["content"] = rows[1]["content"].replace("-> APPROVED (Tier S)", "-> REJECTED (catalyst)")
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        with patch.object(rec, "_register_shadow"), redirect_stdout(io.StringIO()):
            with self.assertRaises(rec.RecordRefused) as ctx:
                rec.record_from_subagent(tgb.EVALUATOR_CONV_ID, target_env="prod", base_dir=self.root)
        self.assertIn("K4 result 'REJECTED'", str(ctx.exception))
        self.assertDenied(self.deploy(), "K4 result 'REJECTED'")


if __name__ == "__main__":
    unittest.main()
