#!/usr/bin/env python3
"""
test_dossier_provenance.py - Unit tests for the evaluator-subagent dossier pipeline.

Covers scripts/utils/dossier_provenance.py (transcript extraction, provenance verification,
status normalisation, trade validation), scripts/record_evaluation.py (--from-subagent,
PROD refusal of manual paths) and the risk profile emitted by scripts/prime_evaluator_brief.py.

Fake Antigravity transcripts are written to a temp dir exposed through AGY_BRAIN_DIRS.
No network access; nothing touches the real logs/ directory.

Run: python3 -m unittest tests.test_dossier_provenance -v
"""

import datetime
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from utils import dossier_provenance as dp  # noqa: E402
import record_evaluation as rec  # noqa: E402
import prime_evaluator_brief as peb  # noqa: E402

PARENT_ID = "11111111-2222-3333-4444-555555555555"


def iso(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dossier_text(payload: dict, header: str = "# QUANTITATIVE EVALUATION MASTER DOSSIER\n") -> str:
    return f"{header}\n<dossier_json>\n{json.dumps(payload, indent=2)}\n</dossier_json>\n"


APPROVED_SHORT = {
    "status": "APPROVED",
    "evaluator_agent": "isolated_market_evaluator",
    "target_env": "PROD",
    "approved_symbols": ["FILUSDT"],
    "approved_candidates": [{
        "symbol": "filusdt", "direction": "short", "tier": "S", "entry": 1.0489, "stop_loss": 1.0663,
        "tp1": 1.0176, "tp2": 0.9794, "leverage": 3, "is_yolo": False, "requires_user_confirmation": False,
    }],
    "summary": "FILUSDT short approved",
}

FEW_SHOT_PROMPT = (
    "<few_shot_examples>\n" + dossier_text({
        "status": "APPROVED",
        "approved_candidates": [{"symbol": "XRPUSDT", "direction": "LONG"}],
        "summary": "few-shot example, must never be extracted",
    }) + "</few_shot_examples>"
)


class TranscriptFixture(unittest.TestCase):
    """Creates a fake brain dir (AGY_BRAIN_DIRS) and a fake workspace for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.brain = os.path.join(self.root, "brain")
        self.workspace = os.path.join(self.root, "workspace")
        os.makedirs(self.brain)
        os.makedirs(os.path.join(self.workspace, "logs", "evaluations"))
        self._env = patch.dict(os.environ, {"AGY_BRAIN_DIRS": self.brain})
        self._env.start()
        self.now = int(time.time())

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    # -- transcript builders -------------------------------------------------
    def write_transcript(self, conv_id: str, steps: list) -> str:
        d = os.path.join(self.brain, conv_id, ".system_generated", "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for i, step in enumerate(steps):
                step.setdefault("step_index", i)
                f.write(json.dumps(step) + "\n")
        return path

    def system_step(self, ts: int) -> dict:
        return {"source": "SYSTEM", "type": "SYSTEM_MESSAGE", "created_at": iso(ts),
                "content": f"<SYSTEM_MESSAGE>Subagent task from sender={PARENT_ID}. Evaluate the brief.</SYSTEM_MESSAGE>"}

    def view_file_steps(self, ts: int) -> list:
        call = {"source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": iso(ts), "content": "",
                "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": json.dumps("logs/primed_brief.json")}}]}
        result = {"source": "MODEL", "type": "GENERIC", "created_at": iso(ts), "content": FEW_SHOT_PROMPT}
        return [call, result]

    def send_message_step(self, ts: int, text: str) -> dict:
        return {"source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": iso(ts), "content": "",
                "tool_calls": [{"name": "send_message", "args": {
                    "Message": json.dumps(text), "Recipient": json.dumps(PARENT_ID)}}]}

    def content_step(self, ts: int, text: str) -> dict:
        return {"source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": iso(ts), "content": text}

    def standard_transcript(self, conv_id: str, payload: dict, ts: int = None) -> str:
        ts = self.now - 30 if ts is None else ts
        steps = [self.system_step(ts - 5)] + self.view_file_steps(ts - 3) + [self.send_message_step(ts, dossier_text(payload))]
        return self.write_transcript(conv_id, steps)

    def record_prod(self, conv_id: str, payload: dict, ts: int = None) -> dict:
        self.standard_transcript(conv_id, payload, ts)
        with patch.object(rec, "_register_shadow"), redirect_stdout(io.StringIO()):
            return rec.record_from_subagent(conv_id, target_env="prod", base_dir=self.workspace)

    def validate(self, symbol, direction, env="prod", now_ts=None):
        return dp.validate_dossier_for_trade(symbol, direction, env=env, base_dir=self.workspace, now_ts=now_ts)


class TestTranscriptExtraction(TranscriptFixture):

    def test_send_message_escaped_block(self):
        conv = "aaaaaaaa-0000-0000-0000-000000000001"
        path = self.standard_transcript(conv, APPROVED_SHORT)
        self.assertEqual(dp.find_subagent_transcript(conv), path)
        ex = dp.extract_dossier_from_transcript(path)
        self.assertEqual(ex["dossier"]["status"], "APPROVED")
        self.assertEqual(ex["dossier"]["approved_candidates"][0]["symbol"], "filusdt")
        self.assertEqual(ex["conversation_id"], conv)
        self.assertEqual(ex["parent_conversation_id"], PARENT_ID)
        self.assertEqual(ex["step_index"], 3)
        self.assertEqual(ex["sha256"], dp.sha256_text(ex["raw"]))
        self.assertGreater(ex["created_at_ts"], 0)

    def test_send_message_with_one_level_of_escaping(self):
        conv = "aaaaaaaa-0000-0000-0000-000000000002"
        escaped = json.dumps(dossier_text(APPROVED_SHORT))[1:-1]  # raw arg, no surrounding quotes
        step = {"source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": iso(self.now), "content": "",
                "tool_calls": [{"name": "send_message", "args": {"Message": escaped}}]}
        path = self.write_transcript(conv, [self.system_step(self.now), step])
        ex = dp.extract_dossier_from_transcript(path)
        self.assertEqual(ex["dossier"]["status"], "APPROVED")

    def test_content_block(self):
        conv = "aaaaaaaa-0000-0000-0000-000000000003"
        path = self.write_transcript(conv, [self.system_step(self.now), self.content_step(self.now, dossier_text(APPROVED_SHORT))])
        ex = dp.extract_dossier_from_transcript(path)
        self.assertEqual(ex["dossier"]["approved_symbols"], ["FILUSDT"])

    def test_view_file_few_shots_are_ignored(self):
        conv = "aaaaaaaa-0000-0000-0000-000000000004"
        steps = [self.system_step(self.now)] + self.view_file_steps(self.now) + [self.content_step(self.now, "No block here.")]
        path = self.write_transcript(conv, steps)
        with self.assertRaises(dp.ProvenanceError):
            dp.extract_dossier_from_transcript(path)

    def test_tool_result_after_model_block_does_not_override(self):
        conv = "aaaaaaaa-0000-0000-0000-000000000005"
        rejected = {"status": "REJECTED", "approved_candidates": [], "summary": "nothing"}
        steps = [self.system_step(self.now), self.send_message_step(self.now, dossier_text(rejected))] + self.view_file_steps(self.now)
        path = self.write_transcript(conv, steps)
        ex = dp.extract_dossier_from_transcript(path)
        self.assertEqual(ex["dossier"]["status"], "REJECTED")

    def test_last_model_block_wins(self):
        conv = "aaaaaaaa-0000-0000-0000-000000000006"
        first = {"status": "REJECTED", "approved_candidates": []}
        steps = [self.system_step(self.now), self.content_step(self.now - 10, dossier_text(first)),
                 self.send_message_step(self.now, dossier_text(APPROVED_SHORT))]
        ex = dp.extract_dossier_from_transcript(self.write_transcript(conv, steps))
        self.assertEqual(ex["dossier"]["status"], "APPROVED")
        self.assertEqual(ex["step_index"], 2)

    def test_invalid_json_block(self):
        conv = "aaaaaaaa-0000-0000-0000-000000000007"
        path = self.write_transcript(conv, [self.content_step(self.now, "<dossier_json>{not json</dossier_json>")])
        with self.assertRaises(dp.ProvenanceError):
            dp.extract_dossier_from_transcript(path)

    def test_prefix_resolution_and_ambiguity(self):
        self.standard_transcript("bbbbbbbb-1111-0000-0000-000000000001", APPROVED_SHORT)
        self.standard_transcript("bbbbbbbb-2222-0000-0000-000000000001", APPROVED_SHORT)
        self.assertTrue(dp.find_subagent_transcript("bbbbbbbb-1111").endswith("transcript.jsonl"))
        with self.assertRaises(dp.ProvenanceError):
            dp.find_subagent_transcript("bbbbbbbb")
        with self.assertRaises(dp.ProvenanceError):
            dp.find_subagent_transcript("../../etc")
        with self.assertRaises(dp.ProvenanceError):
            dp.find_subagent_transcript("cccccccc-0000")


class TestStatusNormalisation(unittest.TestCase):

    def test_pending_confirmation_maps_to_approved(self):
        self.assertEqual(dp.normalize_status("APPROVED_PENDING_CONFIRMATION"), ("APPROVED", True))
        self.assertEqual(dp.normalize_status("approved"), ("APPROVED", False))
        self.assertEqual(dp.normalize_status("NEUTRAL"), ("NEUTRAL", False))
        self.assertEqual(dp.normalize_status("MAYBE"), ("REJECTED", False))
        self.assertEqual(dp.normalize_status(None), ("REJECTED", False))

    def test_pending_confirmation_sets_flag_on_candidates(self):
        extracted = {
            "dossier": {"status": "APPROVED_PENDING_CONFIRMATION", "approved_candidates": [
                {"symbol": "solusdt", "direction": "long"},
                {"symbol": "ethusdt", "direction": "SHORT", "requires_user_confirmation": False},
                {"symbol": "", "direction": "LONG"},
                {"symbol": "bnbusdt", "direction": "sideways"},
            ]},
            "created_at_ts": 1_700_000_000, "conversation_id": "x", "transcript_path": "/t", "sha256": "h",
        }
        record = dp.build_record_from_extraction(extracted, recorded_at_ts=1_700_000_010)
        self.assertEqual(record["status"], "APPROVED")
        self.assertEqual(record["schema_version"], dp.SCHEMA_VERSION)
        self.assertEqual(record["approved_symbols"], ["SOLUSDT", "ETHUSDT", "BNBUSDT"])
        cands = {c["symbol"]: c for c in record["approved_candidates"]}
        self.assertTrue(cands["SOLUSDT"]["requires_user_confirmation"])
        self.assertEqual(cands["SOLUSDT"]["direction"], "LONG")
        self.assertFalse(cands["ETHUSDT"]["requires_user_confirmation"])
        self.assertIsNone(cands["BNBUSDT"]["direction"])
        self.assertEqual(record["valid_until_ts"], 1_700_000_000 + dp.TTL_SECONDS)

    def test_rejected_dossier_has_no_candidates(self):
        extracted = {"dossier": {"status": "REJECTED", "approved_candidates": [{"symbol": "X", "direction": "LONG"}]},
                     "created_at_ts": 1, "conversation_id": "x", "transcript_path": "/t", "sha256": "h"}
        record = dp.build_record_from_extraction(extracted)
        self.assertEqual(record["approved_candidates"], [])
        self.assertEqual(record["approved_symbols"], [])


class TestTradeValidation(TranscriptFixture):

    def test_valid_prod_dossier_passes(self):
        self.record_prod("dddddddd-0000-0000-0000-000000000001", APPROVED_SHORT)
        ok, reason, cand = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)
        self.assertEqual(cand["direction"], "SHORT")

    def test_direction_mismatch_rejected(self):
        self.record_prod("dddddddd-0000-0000-0000-000000000002", APPROVED_SHORT)
        ok, reason, _ = self.validate("FILUSDT", "LONG")
        self.assertFalse(ok)
        self.assertIn("SHORT", reason)
        ok, _, _ = self.validate("FILUSDT", None)
        self.assertFalse(ok, "PROD requires an explicit direction")

    def test_unapproved_symbol_rejected(self):
        self.record_prod("dddddddd-0000-0000-0000-000000000003", APPROVED_SHORT)
        ok, reason, _ = self.validate("BTCUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("NOT approved", reason)

    def test_expired_dossier_rejected(self):
        self.record_prod("dddddddd-0000-0000-0000-000000000004", APPROVED_SHORT)
        record = dp.load_dossier(dp.default_dossier_path(self.workspace))
        ok, reason, _ = self.validate("FILUSDT", "SHORT", now_ts=record["timestamp_ts"] + dp.TTL_SECONDS + 1)
        self.assertFalse(ok)
        self.assertIn("expired", reason)

    def test_tampered_transcript_hash_rejected(self):
        conv = "dddddddd-0000-0000-0000-000000000005"
        self.record_prod(conv, APPROVED_SHORT)
        tampered = json.loads(json.dumps(APPROVED_SHORT))
        tampered["approved_candidates"][0]["stop_loss"] = 9.99
        self.standard_transcript(conv, tampered)  # rewrite the transcript after recording
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("hash", reason.lower())

    def test_tampered_record_sha_rejected(self):
        self.record_prod("dddddddd-0000-0000-0000-000000000006", APPROVED_SHORT)
        path = dp.default_dossier_path(self.workspace)
        record = dp.load_dossier(path)
        record["provenance"]["sha256"] = "0" * 64
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        ok, _, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)

    def test_missing_transcript_rejected(self):
        conv = "dddddddd-0000-0000-0000-000000000007"
        transcript = self.standard_transcript(conv, APPROVED_SHORT)
        with patch.object(rec, "_register_shadow"), redirect_stdout(io.StringIO()):
            rec.record_from_subagent(conv, target_env="prod", base_dir=self.workspace)
        os.remove(transcript)
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("not found", reason)

    def test_edited_record_contents_rejected(self):
        """A REJECTED verdict hand-edited into APPROVED in latest_dossier.json must not validate."""
        rejected = {"status": "REJECTED", "target_env": "PROD", "approved_candidates": [], "summary": "no"}
        self.record_prod("dddddddd-0000-0000-0000-000000000008", rejected)
        path = dp.default_dossier_path(self.workspace)
        record = dp.load_dossier(path)
        record["status"] = "APPROVED"
        record["approved_symbols"] = ["BTCUSDT"]
        record["approved_candidates"] = [{"symbol": "BTCUSDT", "direction": "LONG"}]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        ok, _, _ = self.validate("BTCUSDT", "LONG")
        self.assertFalse(ok)

    def test_legacy_dossier_rejected_in_prod_accepted_in_testnet(self):
        with patch.object(rec, "_register_shadow"), redirect_stdout(io.StringIO()):
            record = rec.record_evaluation_dossier(
                [{"symbol": "ethusdt", "direction": "LONG"}], summary="manual", target_env="testnet",
                base_dir=self.workspace)
        self.assertEqual(record["schema_version"], rec.LEGACY_SCHEMA_VERSION)
        self.assertEqual(record["provenance"]["source"], "manual_testnet")
        ok, reason, _ = self.validate("ETHUSDT", "LONG", env="prod")
        self.assertFalse(ok)
        self.assertIn("Legacy", reason)
        ok, reason, _ = self.validate("ETHUSDT", "LONG", env="testnet")
        self.assertTrue(ok, reason)


class TestRecordEvaluationCli(TranscriptFixture):

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(rec, "BASE_DIR", self.workspace), patch.object(rec, "_register_shadow"), \
                redirect_stdout(out), redirect_stderr(err):
            code = rec.main(argv)
        return code, out.getvalue(), err.getvalue()

    def dossier_path(self):
        return dp.default_dossier_path(self.workspace)

    def test_from_subagent_writes_valid_record(self):
        conv = "eeeeeeee-0000-0000-0000-000000000001"
        self.standard_transcript(conv, APPROVED_SHORT)
        code, out, err = self.run_main(["--from-subagent", conv[:13], "--env", "prod"])
        self.assertEqual(code, 0, err)
        self.assertIn("FILUSDT SHORT", out)
        self.assertIn("fast-track", out)
        self.assertIn("Valid until", out)
        record = dp.load_dossier(self.dossier_path())
        self.assertEqual(record["schema_version"], dp.SCHEMA_VERSION)
        self.assertEqual(record["target_env"], "prod")
        self.assertEqual(record["conversation_id"], conv)
        self.assertEqual(record["parent_conversation_id"], PARENT_ID)
        self.assertEqual(record["provenance"]["source"], "agy_subagent_transcript")
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)
        history = os.path.join(self.workspace, "logs", "evaluations", "evaluations_history.jsonl")
        with open(history, encoding="utf-8") as f:
            entries = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(entries[-1]["sha256"], record["provenance"]["sha256"])

    def test_from_subagent_reports_confirmation_flag(self):
        conv = "eeeeeeee-0000-0000-0000-000000000002"
        payload = {"status": "APPROVED_PENDING_CONFIRMATION", "target_env": "PROD",
                   "approved_candidates": [{"symbol": "SOLUSDT", "direction": "LONG", "tier": "A+"}]}
        self.standard_transcript(conv, payload)
        code, out, err = self.run_main(["--from-subagent", conv, "--env", "prod"])
        self.assertEqual(code, 0, err)
        self.assertIn("REQUIRES USER CONFIRMATION", out)
        record = dp.load_dossier(self.dossier_path())
        self.assertEqual(record["status"], "APPROVED")
        self.assertTrue(record["approved_candidates"][0]["requires_user_confirmation"])

    def test_from_subagent_refuses_expired_dossier(self):
        conv = "eeeeeeee-0000-0000-0000-000000000003"
        self.standard_transcript(conv, APPROVED_SHORT, ts=self.now - dp.TTL_SECONDS - 60)
        code, _, err = self.run_main(["--from-subagent", conv, "--env", "prod"])
        self.assertEqual(code, 2)
        self.assertIn("expired", err)
        self.assertFalse(os.path.exists(self.dossier_path()))

    def test_from_subagent_refuses_env_mismatch(self):
        conv = "eeeeeeee-0000-0000-0000-000000000004"
        payload = dict(APPROVED_SHORT, target_env="TESTNET")
        self.standard_transcript(conv, payload)
        code, _, err = self.run_main(["--from-subagent", conv, "--env", "prod"])
        self.assertEqual(code, 2)
        self.assertIn("TESTNET", err)
        self.assertFalse(os.path.exists(self.dossier_path()))

    def test_from_subagent_without_block_fails(self):
        conv = "eeeeeeee-0000-0000-0000-000000000005"
        self.write_transcript(conv, [self.system_step(self.now)] + self.view_file_steps(self.now))
        code, _, err = self.run_main(["--from-subagent", conv, "--env", "prod"])
        self.assertEqual(code, 1)
        self.assertIn("PROVENANCE", err)
        self.assertFalse(os.path.exists(self.dossier_path()))

    def test_from_subagent_refuses_non_subagent_transcript(self):
        """A main-agent transcript (no parent sender) with a self-written block must not be recordable."""
        conv = "eeeeeeee-0000-0000-0000-000000000006"
        user_step = {"source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": iso(self.now), "content": "trade FIL"}
        self.write_transcript(conv, [user_step, self.content_step(self.now, dossier_text(APPROVED_SHORT))])
        code, _, err = self.run_main(["--from-subagent", conv, "--env", "prod"])
        self.assertEqual(code, 2)
        self.assertIn("not a subagent", err)
        self.assertFalse(os.path.exists(self.dossier_path()))

    def test_validation_rejects_record_pointing_to_main_agent_transcript(self):
        """A record whose provenance points at a block the main agent typed in its own transcript must not validate."""
        conv = "eeeeeeee-0000-0000-0000-000000000007"
        user_step = {"source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": iso(self.now), "content": "trade FIL"}
        path = self.write_transcript(conv, [user_step, self.content_step(self.now, dossier_text(APPROVED_SHORT))])
        record = dp.build_record_from_extraction(dp.extract_dossier_from_transcript(path))
        os.makedirs(os.path.dirname(self.dossier_path()), exist_ok=True)
        with open(self.dossier_path(), "w", encoding="utf-8") as f:
            json.dump(record, f)
        ok, _, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)

    def test_prod_refuses_manual_symbols(self):
        code, _, err = self.run_main(["--env", "prod", "--symbols", "BTCUSDT", "--directions", "LONG"])
        self.assertEqual(code, 2)
        self.assertIn("--from-subagent", err)
        self.assertFalse(os.path.exists(self.dossier_path()))

    def test_prod_refuses_json_file(self):
        jf = os.path.join(self.root, "d.json")
        with open(jf, "w", encoding="utf-8") as f:
            json.dump(APPROVED_SHORT, f)
        code, _, _ = self.run_main(["--env", "prod", "--json-file", jf])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.dossier_path()))

    def test_prod_refuses_manual_function_call(self):
        with self.assertRaises(rec.RecordRefused):
            rec.record_evaluation_dossier([{"symbol": "BTCUSDT", "direction": "LONG"}], target_env="prod",
                                          base_dir=self.workspace, shadow=False)
        self.assertFalse(os.path.exists(self.dossier_path()))

    def test_testnet_manual_symbols_still_work(self):
        code, out, err = self.run_main(["--env", "testnet", "--symbols", "btcusdt,ethusdt", "--directions", "LONG,SHORT"])
        self.assertEqual(code, 0, err)
        record = dp.load_dossier(self.dossier_path())
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["provenance"]["source"], "manual_testnet")
        self.assertEqual(record["approved_symbols"], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(record["approved_candidates"][1]["direction"], "SHORT")

    def test_invalid_env_refused(self):
        code, _, _ = self.run_main(["--env", "staging", "--from-subagent", "eeeeeeee"])
        self.assertEqual(code, 2)


# =============================================================================
# Claude Code subagent transcripts
# =============================================================================
CLAUDE_SESSION = "5e55105e-0000-4000-8000-000000000001"
CLAUDE_OTHER_SESSION = "0f0f0f0f-1111-2222-3333-444444444444"


def iso_ms(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123Z")


class ClaudeTranscriptFixture(TranscriptFixture):
    """Fake ~/.claude/projects tree exposed through CLAUDE_PROJECTS_DIRS."""

    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.root, "claude_projects")
        os.makedirs(self.projects)
        self._claude_env = patch.dict(os.environ, {dp.CLAUDE_PROJECTS_ENV: self.projects})
        self._claude_env.start()

    def tearDown(self):
        self._claude_env.stop()
        super().tearDown()

    def write_claude(self, agent_id: str, rows: list, agent_type: str = dp.EVALUATOR_NAME,
                     session: str = CLAUDE_SESSION, slug: str = "-repo-trading", meta: bool = True) -> str:
        d = os.path.join(self.projects, slug, session, "subagents")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"agent-{agent_id}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for i, row in enumerate(rows):
                row = dict(row)
                row.setdefault("uuid", f"uuid-{agent_id}-{i}")
                row.setdefault("isSidechain", True)
                row.setdefault("agentId", agent_id)
                row.setdefault("sessionId", session)
                f.write(json.dumps(row) + "\n")
        if meta:
            with open(path[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as f:
                json.dump({"agentType": agent_type, "description": "Evaluate brief", "toolUseId": "toolu_x",
                           "spawnDepth": 1}, f)
        return path

    @staticmethod
    def user_row(ts: int, text: str) -> dict:
        return {"type": "user", "timestamp": iso_ms(ts), "message": {"role": "user", "content": text}}

    @staticmethod
    def read_rows(ts: int) -> list:
        """Assistant Read call + tool result holding the agent prompt with its few-shot dossiers."""
        return [
            {"type": "assistant", "timestamp": iso_ms(ts), "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "logs/primed_brief.json"}}]}},
            {"type": "user", "timestamp": iso_ms(ts), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": FEW_SHOT_PROMPT}]}},
        ]

    @staticmethod
    def text_row(ts: int, text: str) -> dict:
        return {"type": "assistant", "timestamp": iso_ms(ts),
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}

    def standard_claude(self, agent_id: str, payload: dict, ts: int = None, **kw) -> str:
        ts = self.now - 30 if ts is None else ts
        rows = [self.user_row(ts - 5, "Evaluate logs/primed_brief.json for PROD.")] + self.read_rows(ts - 3) + \
            [self.text_row(ts, dossier_text(payload))]
        return self.write_claude(agent_id, rows, **kw)

    def record_claude(self, agent_id: str, payload: dict, ts: int = None) -> dict:
        self.standard_claude(agent_id, payload, ts)
        with patch.object(rec, "_register_shadow"), redirect_stdout(io.StringIO()):
            return rec.record_from_claude_subagent(agent_id, target_env="prod", base_dir=self.workspace)


class TestClaudeTranscriptExtraction(ClaudeTranscriptFixture):

    def test_id_detection(self):
        self.assertTrue(dp.is_claude_agent_id("a09a60f5e7bdf2633"))
        self.assertTrue(dp.is_claude_agent_id("agent-a09a60f5e7bdf2633"))
        self.assertFalse(dp.is_claude_agent_id("aaaaaaaa-0000-0000-0000-000000000001"))
        self.assertFalse(dp.is_claude_agent_id("aaaaaaaa"))
        self.assertFalse(dp.is_claude_agent_id("../../etc/passwd"))

    def test_valid_transcript_extracted(self):
        agent = "a0000000000000001"
        path = self.standard_claude(agent, APPROVED_SHORT)
        self.assertEqual(dp.find_claude_subagent_transcript(agent), os.path.abspath(path))
        ex = dp.extract_dossier_from_claude_transcript(path)
        self.assertEqual(ex["source"], dp.CLAUDE_SOURCE)
        self.assertEqual(ex["dossier"]["status"], "APPROVED")
        self.assertEqual(ex["conversation_id"], agent)
        self.assertEqual(ex["parent_conversation_id"], CLAUDE_SESSION)
        self.assertEqual(ex["agent_type"], dp.EVALUATOR_NAME)
        self.assertEqual(ex["step_index"], 3)
        self.assertEqual(ex["step_uuid"], f"uuid-{agent}-3")
        self.assertEqual(ex["created_at_ts"], self.now - 30)
        self.assertEqual(ex["sha256"], dp.sha256_text(ex["raw"]))

    def test_few_shots_in_tool_results_and_prompt_are_ignored(self):
        agent = "a0000000000000002"
        rows = [self.user_row(self.now, dossier_text(APPROVED_SHORT))] + self.read_rows(self.now) + \
            [self.text_row(self.now, "No block.")]
        path = self.write_claude(agent, rows)
        with self.assertRaises(dp.ProvenanceError):
            dp.extract_dossier_from_claude_transcript(path)

    def test_last_assistant_block_wins(self):
        agent = "a0000000000000003"
        rejected = {"status": "REJECTED", "approved_candidates": []}
        rows = [self.text_row(self.now - 10, dossier_text(rejected)), self.text_row(self.now, dossier_text(APPROVED_SHORT))]
        ex = dp.extract_dossier_from_claude_transcript(self.write_claude(agent, rows))
        self.assertEqual(ex["dossier"]["status"], "APPROVED")
        self.assertEqual(ex["step_index"], 1)

    def test_wrong_agent_type_refused(self):
        agent = "a0000000000000004"
        path = self.standard_claude(agent, APPROVED_SHORT, agent_type="general-purpose")
        with self.assertRaises(dp.ProvenanceError) as cm:
            dp.extract_dossier_from_claude_transcript(path)
        self.assertIn("general-purpose", str(cm.exception))

    def test_missing_meta_refused(self):
        agent = "a0000000000000005"
        path = self.standard_claude(agent, APPROVED_SHORT, meta=False)
        with self.assertRaises(dp.ProvenanceError):
            dp.extract_dossier_from_claude_transcript(path)

    def test_rows_from_other_agent_or_session_refused(self):
        agent = "a0000000000000006"
        row = self.text_row(self.now, dossier_text(APPROVED_SHORT))
        row["sessionId"] = CLAUDE_OTHER_SESSION
        with self.assertRaises(dp.ProvenanceError):
            dp.extract_dossier_from_claude_transcript(self.write_claude(agent, [row]))
        agent2 = "a0000000000000007"
        row = self.text_row(self.now, dossier_text(APPROVED_SHORT))
        row["isSidechain"] = False
        with self.assertRaises(dp.ProvenanceError):
            dp.extract_dossier_from_claude_transcript(self.write_claude(agent2, [row]))

    def test_non_canonical_path_refused(self):
        path = os.path.join(self.root, "agent-a0000000000000008.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.text_row(self.now, dossier_text(APPROVED_SHORT))) + "\n")
        with self.assertRaises(dp.ProvenanceError):
            dp.extract_dossier_from_claude_transcript(path)

    def test_lookup_errors(self):
        with self.assertRaises(dp.ProvenanceError):
            dp.find_claude_subagent_transcript("a0000000000000099")
        with self.assertRaises(dp.ProvenanceError):
            dp.find_claude_subagent_transcript("../etc")
        self.standard_claude("a00000000000000aa", APPROVED_SHORT, slug="-repo-a")
        self.standard_claude("a00000000000000aa", APPROVED_SHORT, slug="-repo-b")
        with self.assertRaises(dp.ProvenanceError) as cm:
            dp.find_claude_subagent_transcript("a00000000000000aa")
        self.assertIn("ambiguous", str(cm.exception))


class TestClaudeTradeValidation(ClaudeTranscriptFixture):

    def test_valid_claude_dossier_passes(self):
        record = self.record_claude("a1000000000000001", APPROVED_SHORT)
        self.assertEqual(record["provenance"]["source"], dp.CLAUDE_SOURCE)
        self.assertEqual(record["provenance"]["agent_type"], dp.EVALUATOR_NAME)
        self.assertEqual(record["parent_conversation_id"], CLAUDE_SESSION)
        ok, reason, cand = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)
        self.assertEqual(cand["direction"], "SHORT")

    def test_direction_and_symbol_enforced(self):
        self.record_claude("a1000000000000002", APPROVED_SHORT)
        ok, reason, _ = self.validate("FILUSDT", "LONG")
        self.assertFalse(ok)
        self.assertIn("SHORT", reason)
        ok, _, _ = self.validate("BTCUSDT", "SHORT")
        self.assertFalse(ok)

    def test_expired_dossier_rejected(self):
        record = self.record_claude("a1000000000000003", APPROVED_SHORT)
        ok, reason, _ = self.validate("FILUSDT", "SHORT", now_ts=record["timestamp_ts"] + dp.TTL_SECONDS + 1)
        self.assertFalse(ok)
        self.assertIn("expired", reason)

    def test_tampered_transcript_rejected(self):
        agent = "a1000000000000004"
        self.record_claude(agent, APPROVED_SHORT)
        tampered = json.loads(json.dumps(APPROVED_SHORT))
        tampered["approved_candidates"][0]["stop_loss"] = 9.99
        self.standard_claude(agent, tampered)
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("hash", reason.lower())

    def test_meta_changed_after_recording_rejected(self):
        agent = "a1000000000000005"
        path = self.standard_claude(agent, APPROVED_SHORT)
        with patch.object(rec, "_register_shadow"), redirect_stdout(io.StringIO()):
            rec.record_from_claude_subagent(agent, target_env="prod", base_dir=self.workspace)
        with open(dp.claude_meta_path(path), "w", encoding="utf-8") as f:
            json.dump({"agentType": "general-purpose"}, f)
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("general-purpose", reason)

    def test_edited_record_rejected(self):
        rejected = {"status": "REJECTED", "target_env": "PROD", "approved_candidates": [], "summary": "no"}
        self.record_claude("a1000000000000006", rejected)
        path = dp.default_dossier_path(self.workspace)
        record = dp.load_dossier(path)
        record.update(status="APPROVED", approved_symbols=["BTCUSDT"],
                      approved_candidates=[{"symbol": "BTCUSDT", "direction": "LONG"}])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        ok, _, _ = self.validate("BTCUSDT", "LONG")
        self.assertFalse(ok)

    def test_record_pointing_to_other_agent_rejected(self):
        self.record_claude("a1000000000000007", APPROVED_SHORT)
        path = dp.default_dossier_path(self.workspace)
        record = dp.load_dossier(path)
        record["conversation_id"] = "a1000000000000008"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("conversation_id", reason)

    def test_record_refuses_wrong_agent_type_and_expired(self):
        self.standard_claude("a1000000000000009", APPROVED_SHORT, agent_type="general-purpose")
        with self.assertRaises(dp.ProvenanceError):
            rec.record_from_claude_subagent("a1000000000000009", target_env="prod", base_dir=self.workspace,
                                            shadow=False, verbose=False)
        self.standard_claude("a100000000000000a", APPROVED_SHORT, ts=self.now - dp.TTL_SECONDS - 60)
        with self.assertRaises(rec.RecordRefused):
            rec.record_from_claude_subagent("a100000000000000a", target_env="prod", base_dir=self.workspace,
                                            shadow=False, verbose=False)
        self.assertFalse(os.path.exists(dp.default_dossier_path(self.workspace)))


class TestClaudeRecordEvaluationCli(ClaudeTranscriptFixture):

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(rec, "BASE_DIR", self.workspace), patch.object(rec, "_register_shadow"), \
                redirect_stdout(out), redirect_stderr(err):
            code = rec.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_from_claude_subagent(self):
        self.standard_claude("a2000000000000001", APPROVED_SHORT)
        code, out, err = self.run_main(["--from-claude-subagent", "a2000000000000001", "--env", "prod"])
        self.assertEqual(code, 0, err)
        self.assertIn("Claude Code subagent transcript", out)
        record = dp.load_dossier(dp.default_dossier_path(self.workspace))
        self.assertEqual(record["conversation_id"], "a2000000000000001")
        self.assertEqual(record["provenance"]["source"], dp.CLAUDE_SOURCE)

    def test_from_subagent_auto_detects_claude_id(self):
        self.standard_claude("a2000000000000002", APPROVED_SHORT)
        code, _, err = self.run_main(["--from-subagent", "a2000000000000002", "--env", "prod"])
        self.assertEqual(code, 0, err)
        self.assertEqual(dp.load_dossier(dp.default_dossier_path(self.workspace))["provenance"]["source"],
                         dp.CLAUDE_SOURCE)

    def test_wrong_agent_type_fails_closed(self):
        self.standard_claude("a2000000000000003", APPROVED_SHORT, agent_type="general-purpose")
        code, _, err = self.run_main(["--from-claude-subagent", "a2000000000000003", "--env", "prod"])
        self.assertEqual(code, 1)
        self.assertIn("PROVENANCE", err)
        self.assertFalse(os.path.exists(dp.default_dossier_path(self.workspace)))

    def test_both_flags_refused(self):
        code, _, _ = self.run_main(["--from-claude-subagent", "a2000000000000004", "--from-subagent", "x", "--env", "prod"])
        self.assertEqual(code, 2)


class TestClaudeProjectRoots(unittest.TestCase):

    def test_override_and_defaults(self):
        with patch.dict(os.environ, {dp.CLAUDE_PROJECTS_ENV: os.pathsep.join(["/x", "/y"])}):
            self.assertEqual(dp.claude_project_roots(), ["/x", "/y"])
        env = {k: v for k, v in os.environ.items() if k not in (dp.CLAUDE_PROJECTS_ENV, "CLAUDE_CONFIG_DIR")}
        with patch.dict(os.environ, env, clear=True):
            roots = dp.claude_project_roots()
        self.assertEqual(roots[0], os.path.join(os.path.expanduser("~"), ".claude", "projects"))


class TestPrimeEvaluatorBriefRiskProfile(unittest.TestCase):

    def test_risk_profile_from_profile_values(self):
        profile = {"risk_pct_equity": 0.01, "leverage_standard": 4, "leverage_yolo": 20,
                   "yolo_slot_enabled": True, "yolo_equity_pct": 0.02, "max_margin_ratio": 0.3}
        rp = peb.build_risk_profile("testnet", profile=profile, equity=2000.0)
        self.assertEqual(rp["risk_pct_equity"], 0.01)
        self.assertEqual(rp["risk_per_trade_usdt"], 20.0)
        self.assertEqual(rp["leverage_standard"], 4)
        self.assertEqual(rp["leverage_yolo"], 15)  # clamped to the default desk ceiling
        self.assertEqual(rp["leverage_ceiling"], 15)
        self.assertIsNone(rp["yolo_margin_fixed"])
        self.assertEqual(rp["yolo_margin_usdt"], 40.0)

    def test_profile_leverage_ceiling_raises_yolo_cap(self):
        profile = {"leverage_standard": 4, "leverage_yolo": 20, "leverage_ceiling": 25}
        rp = peb.build_risk_profile("testnet", profile=profile, equity=1000.0)
        self.assertEqual(rp["leverage_yolo"], 20)
        self.assertEqual(rp["leverage_ceiling"], 25)

    def test_percent_style_risk_and_fixed_yolo_margin(self):
        rp = peb.build_risk_profile("testnet", profile={"risk_pct_equity": 0.5, "yolo_margin_fixed": 12}, equity=1000.0)
        self.assertAlmostEqual(rp["risk_pct_equity"], 0.005)
        self.assertEqual(rp["risk_per_trade_usdt"], 5.0)
        self.assertEqual(rp["yolo_margin_fixed"], 12.0)
        self.assertEqual(rp["yolo_margin_usdt"], 12.0)

    def test_unknown_equity_gives_no_dollar_amount(self):
        with patch.object(peb, "_get_equity", return_value=None):
            rp = peb.build_risk_profile("prod", profile={})
        self.assertIsNone(rp["risk_per_trade_usdt"])
        self.assertEqual(rp["risk_pct_equity"], peb.DEFAULT_RISK_PCT_EQUITY)

    def test_assemble_writes_fresh_brief_and_out_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief_file = os.path.join(tmp, "logs", "primed_brief.json")
            out_file = os.path.join(tmp, "copy", "brief.json")
            fake_rp = peb.build_risk_profile("prod", profile={"risk_pct_equity": 0.005}, equity=1000.0)
            with patch.object(peb, "BRIEF_FILE", brief_file), \
                    patch.object(peb, "ensure_fresh_state", return_value={"target_env": "prod"}), \
                    patch.object(peb, "get_latest_screening_payload", return_value={}), \
                    patch.object(peb, "load_recent_insights", return_value=[]), \
                    patch.object(peb, "build_risk_profile", return_value=fake_rp):
                brief = peb.assemble_primed_brief(target_env="prod", out_path=out_file)
                md = peb.format_markdown_brief(brief)
            with open(brief_file, encoding="utf-8") as f:
                on_disk = json.load(f)
            with open(out_file, encoding="utf-8") as f:
                copy = json.load(f)
        self.assertEqual(on_disk, copy)
        self.assertLessEqual(abs(on_disk["generated_at_ts"] - int(time.time())), 5)
        self.assertEqual(on_disk["target_env"], "PROD")
        self.assertEqual(on_disk["risk_profile"]["risk_per_trade_usdt"], 5.0)
        self.assertIn("Risk/trade:** $5.0", md)


if __name__ == "__main__":
    unittest.main()
