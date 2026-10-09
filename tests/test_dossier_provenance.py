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


def checklist_for(payload: dict) -> str:
    """Precondition Checklist consistent with the payload (issue #27): K1-K4/C3.1 [x] per approved candidate
    and a C4.2 line carrying the raw status."""
    lines = ["## Precondition Checklist", "- [x] C0.2 Brief age: 1 min -> PASS"]
    for c in payload.get("approved_candidates") or []:
        prefix = f"{str(c['symbol']).upper()} {str(c['direction']).upper()}" + (" (YOLO)" if c.get("is_yolo") else "")
        lines += [f"- [x] {prefix} {check} gate: brief value -> PASS" for check in ("K1", "K2", "K3", "C3.1")]
        lines.append(f"- [x] {prefix} K4 Verdict: K1-K3 PASS -> APPROVED (Tier S)")
    lines.append(f"- [{'x' if payload.get('status') != 'REJECTED' else ' '}] C4.2 Overall status: verdict -> "
                 f"{payload.get('status')}")
    return "\n".join(lines) + "\n\n## 1. Basket\n"


def dossier_text(payload: dict, header: str = "# QUANTITATIVE EVALUATION MASTER DOSSIER\n", checklist=True) -> str:
    """Evaluator message: header, Precondition Checklist (True: consistent with the payload; False: none;
    a string: that text) and the <dossier_json> block."""
    if checklist is True:
        checklist = checklist_for(payload)
    return f"{header}{checklist or ''}\n<dossier_json>\n{json.dumps(payload, indent=2)}\n</dossier_json>\n"


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


def agy_full_row(step: dict) -> dict:
    """transcript.jsonl row -> its transcript_full.jsonl twin (tool-call args as plain values)."""
    row = dict(step)
    if isinstance(row.get("tool_calls"), list):
        row["tool_calls"] = [dict(c, args={k: json.loads(v) for k, v in (c.get("args") or {}).items()})
                             for c in row["tool_calls"]]
    return row


def agy_middle_cut(text: str, head: int = 60, tail: int = 20) -> str:
    """agy content/thinking truncation: head + '\\n<truncated N bytes>\\n' + tail (N = UTF-8 bytes removed)."""
    h, t = text[:head], text[len(text) - tail:]
    removed = len(text.encode("utf-8")) - len(h.encode("utf-8")) - len(t.encode("utf-8"))
    return f"{h}\n<truncated {removed} bytes>\n{t}"


def agy_prefix_cut(value, keep: int = 60) -> str:
    """agy tool-call arg truncation: prefix of the JSON-encoded value + '\\n<truncated N bytes>'."""
    encoded = json.dumps(value, ensure_ascii=False)
    prefix = encoded[:keep]
    return f"{prefix}\n<truncated {len(encoded.encode('utf-8')) - len(prefix.encode('utf-8'))} bytes>"


def agy_truncate_row(step: dict, fields: list, keep: int = 60) -> dict:
    """Truncates a transcript.jsonl row exactly like agy does (only values longer than `keep`)."""
    row = dict(step)
    for field in fields:
        if field in ("content", "thinking"):
            row[field] = agy_middle_cut(row[field], head=keep)
        elif field == "tool_calls":
            row["tool_calls"] = [dict(c, args={
                k: agy_prefix_cut(json.loads(v), keep) if len(v) > keep else v
                for k, v in (c.get("args") or {}).items()}) for c in row["tool_calls"]]
    row["truncated_fields"] = list(fields)
    return row


# Long send_message text with non-ASCII in the truncated region (agy counts UTF-8 bytes)
LONG_HEADER = "# QUANTITATIVE EVALUATION MASTER DOSSIER\n" + "Análisis: régimen σ = +0.66 — ok.\n" * 20


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
    def write_transcript(self, conv_id: str, steps: list, truncate: dict = None, full: bool = None) -> str:
        """Writes transcript.jsonl. truncate = {line: [fields]} writes those rows truncated like agy.
        full (default: True when truncating) also writes the untruncated transcript_full.jsonl."""
        d = os.path.join(self.brain, conv_id, ".system_generated", "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "transcript.jsonl")
        truncate = truncate or {}
        for i, step in enumerate(steps):
            step.setdefault("step_index", i)
        with open(path, "w", encoding="utf-8") as f:
            for i, step in enumerate(steps):
                row = agy_truncate_row(step, truncate[i]) if i in truncate else step
                f.write(json.dumps(row) + "\n")
        if full if full is not None else bool(truncate):
            self.dump_rows(dp.full_transcript_path(path), [agy_full_row(s) for s in steps])
        return path

    @staticmethod
    def load_rows(path: str) -> list:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    @staticmethod
    def dump_rows(path: str, rows: list) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def edit_row(self, path: str, line: int, edit) -> None:
        rows = self.load_rows(path)
        edit(rows[line])
        self.dump_rows(path, rows)

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
        # final_text is decoded like the block, so the checklist check reads real lines (issue #27)
        self.assertEqual(ex["final_text"], dossier_text(APPROVED_SHORT))
        with patch.object(rec, "_register_shadow"), redirect_stdout(io.StringIO()):
            record = rec.record_from_subagent(conv, target_env="prod", base_dir=self.workspace)
        self.assertEqual(record["status"], "APPROVED")
        # An inconsistent single-escaped checklist is still refused in PROD, and nothing is written
        os.remove(dp.default_dossier_path(self.workspace))
        bad = checklist_for(APPROVED_SHORT).replace("- [x] FILUSDT SHORT K1", "- [ ] FILUSDT SHORT K1")
        step["tool_calls"][0]["args"]["Message"] = json.dumps(dossier_text(APPROVED_SHORT, checklist=bad))[1:-1]
        self.write_transcript("aaaaaaaa-0000-0000-0000-00000000000b", [self.system_step(self.now), step])
        with self.assertRaises(rec.RecordRefused) as cm:
            rec.record_from_subagent("aaaaaaaa-0000-0000-0000-00000000000b", target_env="prod",
                                     base_dir=self.workspace, shadow=False, verbose=False)
        self.assertIn("K1 is not checked", str(cm.exception))
        self.assertFalse(os.path.exists(dp.default_dossier_path(self.workspace)))

    def test_claude_text_with_one_level_of_escaping_decodes_final_text(self):
        with tempfile.TemporaryDirectory() as projects, patch.dict(os.environ, {dp.CLAUDE_PROJECTS_ENV: projects}):
            d = os.path.join(projects, "-repo", "5e55105e-0000-4000-8000-000000000009", "subagents")
            os.makedirs(d)
            path = os.path.join(d, "agent-a2800000000000001.jsonl")
            row = {"type": "assistant", "timestamp": iso(self.now - 30), "uuid": "u1", "isSidechain": True,
                   "agentId": "a2800000000000001", "sessionId": "5e55105e-0000-4000-8000-000000000009",
                   "message": {"role": "assistant", "content": [
                       {"type": "text", "text": json.dumps(dossier_text(APPROVED_SHORT))[1:-1]}]}}
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            with open(dp.claude_meta_path(path), "w", encoding="utf-8") as f:
                json.dump({"agentType": dp.EVALUATOR_NAME}, f)
            ex = dp.extract_dossier_from_claude_transcript(path)
        self.assertEqual(ex["final_text"], dossier_text(APPROVED_SHORT))
        self.assertEqual(dp.check_precondition_checklist(ex["final_text"], ex["dossier"]), [])

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


class TestTruncatedAgyTranscripts(TranscriptFixture):
    """agy truncates long fields in transcript.jsonl; the full row lives in transcript_full.jsonl (issue #31)."""

    def truncated_send_message(self, conv: str, payload: dict = APPROVED_SHORT, full: bool = None, ts: int = None) -> str:
        ts = self.now - 30 if ts is None else ts
        step = self.send_message_step(ts, dossier_text(payload, header=LONG_HEADER))
        step["thinking"] = "Checking the brief freshness and the delta gate. " * 10
        steps = [self.system_step(ts - 5)] + self.view_file_steps(ts - 3) + [step]
        return self.write_transcript(conv, steps, truncate={3: ["thinking", "tool_calls"]}, full=full)

    def record(self, conv: str) -> dict:
        with patch.object(rec, "_register_shadow"), redirect_stdout(io.StringIO()):
            return rec.record_from_subagent(conv, target_env="prod", base_dir=self.workspace)

    def assertMismatch(self, path: str, fragment: str = "transcript_full.jsonl mismatch"):
        with self.assertRaises(dp.ProvenanceError) as cm:
            dp.extract_dossier_from_transcript(path)
        self.assertIn(fragment, str(cm.exception))

    def test_short_row_really_is_truncated(self):
        path = self.truncated_send_message("ffffffff-0000-0000-0000-000000000001")
        row = self.load_rows(path)[3]
        self.assertEqual(row["truncated_fields"], ["thinking", "tool_calls"])
        self.assertNotIn("</dossier_json>", row["tool_calls"][0]["args"]["Message"])
        self.assertRegex(row["tool_calls"][0]["args"]["Message"], r"\n<truncated \d+ bytes>\Z")

    def test_truncated_send_message_resolved_from_full_transcript(self):
        conv = "ffffffff-0000-0000-0000-000000000002"
        path = self.truncated_send_message(conv)
        self.assertEqual(dp.find_subagent_transcript(conv), path)  # stored path stays transcript.jsonl
        ex = dp.extract_dossier_from_transcript(path)
        expected_raw = dp.DOSSIER_RE.search(dossier_text(APPROVED_SHORT, header=LONG_HEADER)).group(1).strip()
        self.assertEqual(ex["raw"], expected_raw)
        self.assertEqual(ex["sha256"], dp.sha256_text(expected_raw))
        self.assertEqual(ex["step_index"], 3)
        self.assertEqual(ex["parent_conversation_id"], PARENT_ID)
        self.assertEqual(ex["conversation_id"], conv)
        self.assertTrue(ex["full_transcript_used"])
        self.assertEqual(ex["resolved_steps"], [3])
        self.assertEqual(ex["transcript_path"], os.path.abspath(path))

        # Same hash as an untruncated transcript carrying the same message
        plain = self.write_transcript("ffffffff-0000-0000-0000-000000000003", [
            self.system_step(self.now), self.send_message_step(self.now, dossier_text(APPROVED_SHORT, header=LONG_HEADER))])
        self.assertEqual(dp.extract_dossier_from_transcript(plain)["sha256"], ex["sha256"])

        record = self.record(conv)
        self.assertTrue(record["provenance"]["full_transcript_used"])
        self.assertEqual(record["provenance"]["resolved_steps"], [3])
        self.assertEqual(record["provenance"]["transcript_path"], os.path.abspath(path))
        ok, reason, cand = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)
        self.assertEqual(cand["direction"], "SHORT")

    def test_missing_full_transcript_fails_closed(self):
        conv = "ffffffff-0000-0000-0000-000000000004"
        path = self.truncated_send_message(conv, full=False)
        with self.assertRaises(dp.ProvenanceError) as cm:
            dp.extract_dossier_from_transcript(path)
        self.assertIn("transcript_full.jsonl", str(cm.exception))
        with self.assertRaises(dp.ProvenanceError):
            self.record(conv)
        self.assertFalse(os.path.exists(dp.default_dossier_path(self.workspace)))

    def test_full_transcript_deleted_after_recording_rejected(self):
        conv = "ffffffff-0000-0000-0000-000000000005"
        path = self.truncated_send_message(conv)
        self.record(conv)
        os.remove(dp.full_transcript_path(path))
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("transcript_full.jsonl", reason)

    def test_full_row_prefix_mismatch_rejected(self):
        path = self.truncated_send_message("ffffffff-0000-0000-0000-000000000006")
        full = dp.full_transcript_path(path)

        def forge(row):
            msg = row["tool_calls"][0]["args"]["Message"]
            row["tool_calls"][0]["args"]["Message"] = msg.replace("QUANTITATIVE", "QUALITATIVEX", 1)  # same length
        self.edit_row(full, 3, forge)
        self.assertMismatch(path, "does not match the full value")

    def test_full_row_byte_count_mismatch_rejected(self):
        path = self.truncated_send_message("ffffffff-0000-0000-0000-000000000007")
        self.edit_row(dp.full_transcript_path(path), 3,
                      lambda row: row["tool_calls"][0]["args"].update(
                          Message=row["tool_calls"][0]["args"]["Message"] + "extra"))
        self.assertMismatch(path, "does not match the full value")

    def test_full_row_identity_and_untruncated_fields_must_match(self):
        edits = {
            "created_at": lambda row: row.update(created_at=iso(self.now - 999)),
            "step_index": lambda row: row.update(step_index=7),
            "tool name": lambda row: row["tool_calls"][0].update(name="notify_user"),
            "recipient": lambda row: row["tool_calls"][0]["args"].update(Recipient="99999999-0000-0000-0000-000000000000"),
            "tool count": lambda row: row["tool_calls"].append({"name": "view_file", "args": {"AbsolutePath": "x"}}),
            "extra field": lambda row: row.update(content="injected"),
            "thinking": lambda row: row.update(thinking="Different reasoning entirely. " * 20),
            "full truncated": lambda row: row.update(truncated_fields=["tool_calls"]),
        }
        for i, (label, edit) in enumerate(edits.items()):
            with self.subTest(label):
                path = self.truncated_send_message(f"ffffffff-1000-0000-0000-00000000000{i}")
                self.edit_row(dp.full_transcript_path(path), 3, edit)
                self.assertMismatch(path)

    def test_full_transcript_edited_after_recording_rejected_by_hash(self):
        conv = "ffffffff-0000-0000-0000-000000000008"
        path = self.truncated_send_message(conv)
        self.record(conv)
        full = dp.full_transcript_path(path)

        def tamper(row):  # same byte length, inside the truncated region: every cross-check still passes
            msg = row["tool_calls"][0]["args"]["Message"]
            self.assertIn("1.0663", msg)
            row["tool_calls"][0]["args"]["Message"] = msg.replace("1.0663", "1.0669")
        self.edit_row(full, 3, tamper)
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("hash", reason.lower())

    def test_short_transcript_edited_after_recording_rejected(self):
        conv = "ffffffff-0000-0000-0000-000000000009"
        path = self.truncated_send_message(conv)
        self.record(conv)

        def tamper(row):
            msg = row["tool_calls"][0]["args"]["Message"]
            row["tool_calls"][0]["args"]["Message"] = msg.replace("QUANTITATIVE", "QUALITATIVEX", 1)
        self.edit_row(path, 3, tamper)
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("transcript_full.jsonl mismatch", reason)

        # Dropping the truncation marker instead leaves no block to extract
        conv2 = "ffffffff-0000-0000-0000-00000000000a"
        path2 = self.truncated_send_message(conv2)
        self.record(conv2)
        self.edit_row(path2, 3, lambda row: row.pop("truncated_fields"))
        ok, _, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)

    def test_resolution_flag_tampered_in_record_rejected(self):
        conv = "ffffffff-0000-0000-0000-00000000000b"
        self.truncated_send_message(conv)
        self.record(conv)
        dossier = dp.default_dossier_path(self.workspace)
        record = dp.load_dossier(dossier)
        record["provenance"]["full_transcript_used"] = False
        with open(dossier, "w", encoding="utf-8") as f:
            json.dump(record, f)
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok)
        self.assertIn("transcript_full.jsonl", reason)

    def test_truncated_planner_content_middle_cut_resolved(self):
        conv = "ffffffff-0000-0000-0000-00000000000c"
        text = dossier_text(APPROVED_SHORT, header=LONG_HEADER)
        steps = [self.system_step(self.now), self.content_step(self.now, text)]
        path = self.write_transcript(conv, steps, truncate={1: ["content"]})
        short = self.load_rows(path)[1]["content"]
        self.assertNotIn("<dossier_json>", short)
        self.assertIn("\n<truncated ", short)
        ex = dp.extract_dossier_from_transcript(path)
        self.assertEqual(ex["dossier"]["approved_symbols"], ["FILUSDT"])
        self.assertTrue(ex["full_transcript_used"])
        # A full content whose tail differs from the kept tail is rejected
        self.edit_row(dp.full_transcript_path(path), 1, lambda row: row.update(content=row["content"][:-1] + "X"))
        self.assertMismatch(path, "truncated 'content' does not match")

    def test_untruncated_rows_ignore_full_transcript(self):
        conv = "ffffffff-0000-0000-0000-00000000000d"
        path = self.standard_transcript(conv, APPROVED_SHORT)
        before = dp.extract_dossier_from_transcript(path)
        rejected = {"status": "REJECTED", "approved_candidates": []}
        steps = [self.system_step(self.now)] + self.view_file_steps(self.now) + \
            [self.send_message_step(self.now, dossier_text(rejected))]
        for i, s in enumerate(steps):
            s["step_index"] = i
        self.dump_rows(dp.full_transcript_path(path), [agy_full_row(s) for s in steps])
        after = dp.extract_dossier_from_transcript(path)
        self.assertEqual(after["dossier"]["status"], "APPROVED")
        self.assertEqual(after["sha256"], before["sha256"])
        self.assertFalse(after["full_transcript_used"])
        self.assertEqual(after["resolved_steps"], [])

    def test_unscanned_truncated_rows_do_not_need_full_transcript(self):
        conv = "ffffffff-0000-0000-0000-00000000000e"
        ts = self.now - 30
        view_call, view_result = self.view_file_steps(ts - 3)
        view_call["thinking"] = "Reading the brief first. " * 10
        view_result["content"] = FEW_SHOT_PROMPT * 3
        steps = [self.system_step(ts - 5), view_call, view_result, self.send_message_step(ts, dossier_text(APPROVED_SHORT))]
        path = self.write_transcript(conv, steps, truncate={1: ["thinking"], 2: ["content"]}, full=False)
        ex = dp.extract_dossier_from_transcript(path)
        self.assertEqual(ex["dossier"]["status"], "APPROVED")
        self.assertFalse(ex["full_transcript_used"])
        self.assertEqual(ex["resolved_steps"], [])


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


# =============================================================================
# Issue #27: Precondition Checklist vs dossier at record time
# =============================================================================
PEPE_APPROVED = {
    "status": "APPROVED", "target_env": "PROD", "summary": "pepe",
    "approved_candidates": [{"symbol": "1000PEPEUSDT", "direction": "LONG", "is_yolo": True, "tier": "A",
                             "requires_user_confirmation": True}],
}


def c0_stop_text(payload: dict) -> str:
    return ("# QUANTITATIVE EVALUATION MASTER DOSSIER\n## Precondition Checklist\n"
            "- [x] C0.1 Brief source: view_file logs/primed_brief.json -> file\n"
            "- [ ] C0.2 Brief age: generated_at_ts 14 min ago -> FAIL\n"
            "- [ ] C4.2 Overall status: STALE_BRIEF -> REJECTED\n\n"
            f"<dossier_json>\n{json.dumps(payload)}\n</dossier_json>\n")


class TestPreconditionChecklistChecker(unittest.TestCase):
    """dp.check_precondition_checklist on synthetic evaluator messages."""

    def check(self, payload, checklist=True):
        return dp.check_precondition_checklist(dossier_text(payload, checklist=checklist), payload)

    def test_consistent_checklist_passes(self):
        self.assertEqual(self.check(APPROVED_SHORT), [])
        self.assertEqual(self.check(PEPE_APPROVED), [])
        self.assertEqual(self.check({"status": "NEUTRAL", "approved_candidates": []}), [])

    def test_missing_checklist(self):
        self.assertEqual(self.check(APPROVED_SHORT, checklist=False), ["missing ## Precondition Checklist"])
        text = dossier_text(APPROVED_SHORT, checklist=checklist_for(APPROVED_SHORT).replace("## Precondition", "### Precondition"))
        self.assertIn("missing ## Precondition Checklist", dp.check_precondition_checklist(text, APPROVED_SHORT))

    def test_c42_mismatch_missing_and_duplicate(self):
        base = checklist_for(APPROVED_SHORT)
        mismatch = base.replace("verdict -> APPROVED", "verdict -> REJECTED")
        problems = dp.check_precondition_checklist(dossier_text(APPROVED_SHORT, checklist=mismatch), APPROVED_SHORT)
        self.assertEqual(len(problems), 1)
        self.assertIn("does not match the dossier status 'APPROVED'", problems[0])
        missing = "\n".join(l for l in base.splitlines() if "C4.2" not in l)
        problems = dp.check_precondition_checklist(dossier_text(APPROVED_SHORT, checklist=missing), APPROVED_SHORT)
        self.assertEqual(problems, ["expected exactly one 'C4.2 Overall status' line in the checklist, found 0"])
        c42 = next(l for l in base.splitlines() if "C4.2" in l)
        dup = base.replace(c42, c42 + "\n" + c42)
        problems = dp.check_precondition_checklist(dossier_text(APPROVED_SHORT, checklist=dup), APPROVED_SHORT)
        self.assertIn("found 2", problems[0])
        # A C4.2 line after the next heading is outside the checklist region
        moved = missing.replace("## 1. Basket", "## 1. Basket\n" + c42)
        self.assertIn("found 0", dp.check_precondition_checklist(
            dossier_text(APPROVED_SHORT, checklist=moved), APPROVED_SHORT)[0])

    def test_unchecked_or_missing_gate_of_an_approved_candidate(self):
        base = checklist_for(APPROVED_SHORT)
        for check in ("K1", "K2", "K3", "K4", "C3.1"):
            with self.subTest(check):
                line = next(l for l in base.splitlines() if f"FILUSDT SHORT {check} " in l)
                unchecked = base.replace(line, line.replace("- [x]", "- [ ]"))
                problems = dp.check_precondition_checklist(dossier_text(APPROVED_SHORT, checklist=unchecked),
                                                           APPROVED_SHORT)
                self.assertEqual(problems, [f"approved FILUSDT SHORT: {check} is not checked [x]"])
                dropped = base.replace(line + "\n", "")
                problems = dp.check_precondition_checklist(dossier_text(APPROVED_SHORT, checklist=dropped),
                                                           APPROVED_SHORT)
                self.assertEqual(problems, [f"approved FILUSDT SHORT: no {check} line in the checklist"])

    def test_same_symbol_other_direction_does_not_count(self):
        other = checklist_for(APPROVED_SHORT).replace("FILUSDT SHORT", "FILUSDT LONG")
        problems = dp.check_precondition_checklist(dossier_text(APPROVED_SHORT, checklist=other), APPROVED_SHORT)
        self.assertEqual(len(problems), 5)
        self.assertTrue(all("approved FILUSDT SHORT: no" in p for p in problems), problems)

    def test_symbol_match_is_token_exact(self):
        pepe = dict(PEPE_APPROVED, approved_candidates=[{"symbol": "PEPEUSDT", "direction": "LONG"}])
        text = dossier_text(pepe, checklist=checklist_for(PEPE_APPROVED))  # lines name 1000PEPEUSDT
        problems = dp.check_precondition_checklist(text, pepe)
        self.assertEqual(len(problems), 5)
        self.assertTrue(all("approved PEPEUSDT LONG: no" in p for p in problems), problems)

    def test_rejected_candidate_lines_may_be_unchecked(self):
        rejected_lines = "".join(f"- [ ] WLFIUSDT LONG {c} gate -> BLOCKED\n" for c in ("K1", "K2", "K3", "C3.1", "K4"))
        text = checklist_for(APPROVED_SHORT).replace("## Precondition Checklist\n",
                                                     "## Precondition Checklist\n" + rejected_lines)
        self.assertEqual(dp.check_precondition_checklist(dossier_text(APPROVED_SHORT, checklist=text), APPROVED_SHORT), [])

    def test_c0_stop_rejected_dossier_is_consistent(self):
        payload = {"status": "REJECTED", "approved_candidates": [], "summary": "STALE_BRIEF: 14 min"}
        self.assertEqual(dp.check_precondition_checklist(c0_stop_text(payload), payload), [])

    def test_formatting_tolerance(self):
        text = dossier_text(APPROVED_SHORT).replace("\n", "\r\n").replace("- [x]", "    - [X]").replace(
            "-> PASS", "-> PASS   ")
        self.assertEqual(dp.check_precondition_checklist(text, APPROVED_SHORT), [])
        # Lowercase dossier symbol/direction and the legacy APPROVED_PENDING_CONFIRMATION status are normalized
        legacy = dict(APPROVED_SHORT, status="APPROVED_PENDING_CONFIRMATION")
        self.assertEqual(dp.check_precondition_checklist(dossier_text(legacy), legacy), [])
        # Unrelated check ids (another session may add C1.3 / RULE 10 lines) are ignored
        extra = checklist_for(APPROVED_SHORT).replace("- [x] C0.2", "- [ ] C1.3 New gate -> FAIL\n- [x] C0.2")
        self.assertEqual(dp.check_precondition_checklist(dossier_text(APPROVED_SHORT, checklist=extra), APPROVED_SHORT), [])


class TestPreconditionChecklistRecording(ClaudeTranscriptFixture):
    """record_evaluation.py refuses (PROD) / warns (TESTNET) on an inconsistent checklist (issue #27)."""

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(rec, "BASE_DIR", self.workspace), patch.object(rec, "_register_shadow"), \
                redirect_stdout(out), redirect_stderr(err):
            code = rec.main(argv)
        return code, out.getvalue(), err.getvalue()

    def agy_transcript(self, conv: str, text: str) -> str:
        ts = self.now - 30
        steps = [self.system_step(ts - 5)] + self.view_file_steps(ts - 3) + [self.send_message_step(ts, text)]
        return self.write_transcript(conv, steps)

    def claude_transcript(self, agent: str, text: str) -> str:
        ts = self.now - 30
        rows = [self.user_row(ts - 5, "Evaluate logs/primed_brief.json.")] + self.read_rows(ts - 3) + [self.text_row(ts, text)]
        return self.write_claude(agent, rows)

    def dossier_path(self):
        return dp.default_dossier_path(self.workspace)

    def history(self):
        path = os.path.join(self.workspace, "logs", "evaluations", "evaluations_history.jsonl")
        with open(path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def assertRefused(self, flag, ident, fragment):
        code, _, err = self.run_main([flag, ident, "--env", "prod"])
        self.assertEqual(code, 2, err)
        self.assertIn("Precondition Checklist", err)
        self.assertIn(fragment, err)
        self.assertFalse(os.path.exists(self.dossier_path()))

    def test_prod_approved_without_checklist_refused(self):
        self.agy_transcript("abababab-0000-0000-0000-000000000001", dossier_text(APPROVED_SHORT, checklist=False))
        self.assertRefused("--from-subagent", "abababab-0000-0000-0000-000000000001", "missing ## Precondition Checklist")
        self.claude_transcript("a2700000000000001", dossier_text(APPROVED_SHORT, checklist=False))
        self.assertRefused("--from-claude-subagent", "a2700000000000001", "missing ## Precondition Checklist")

    def test_prod_approved_with_inconsistent_checklist_refused(self):
        base = checklist_for(APPROVED_SHORT)
        cases = {
            "C4.2 mismatch": (base.replace("verdict -> APPROVED", "verdict -> NEUTRAL"), "does not match"),
            "C4.2 missing": ("\n".join(l for l in base.splitlines() if "C4.2" not in l), "found 0"),
            "K2 unchecked": (base.replace("- [x] FILUSDT SHORT K2", "- [ ] FILUSDT SHORT K2"), "K2 is not checked"),
            "other direction": (base.replace("FILUSDT SHORT", "FILUSDT LONG"), "approved FILUSDT SHORT: no K1"),
        }
        for i, (label, (checklist, fragment)) in enumerate(cases.items()):
            with self.subTest(label):
                conv = f"abababab-1000-0000-0000-00000000000{i}"
                self.agy_transcript(conv, dossier_text(APPROVED_SHORT, checklist=checklist))
                self.assertRefused("--from-subagent", conv, fragment)
                agent = f"a27100000000000{i:02d}"
                self.claude_transcript(agent, dossier_text(APPROVED_SHORT, checklist=checklist))
                self.assertRefused("--from-claude-subagent", agent, fragment)

    def test_testnet_approved_without_checklist_warns_and_records(self):
        payload = dict(APPROVED_SHORT, target_env="TESTNET")
        self.agy_transcript("abababab-0000-0000-0000-000000000002", dossier_text(payload, checklist=False))
        code, _, err = self.run_main(["--from-subagent", "abababab-0000-0000-0000-000000000002", "--env", "testnet"])
        self.assertEqual(code, 0, err)
        self.assertIn("CHECKLIST WARNING (TESTNET", err)
        self.assertIn("missing ## Precondition Checklist", err)
        record = dp.load_dossier(self.dossier_path())
        self.assertEqual((record["status"], record["target_env"]), ("APPROVED", "testnet"))
        os.remove(self.dossier_path())
        self.claude_transcript("a2700000000000002", dossier_text(payload, checklist=False))
        code, _, err = self.run_main(["--from-claude-subagent", "a2700000000000002", "--env", "testnet"])
        self.assertEqual(code, 0, err)
        self.assertIn("CHECKLIST WARNING (TESTNET", err)
        self.assertEqual(dp.load_dossier(self.dossier_path())["provenance"]["source"], dp.CLAUDE_SOURCE)

    def test_prod_rejected_with_inconsistent_checklist_recorded_with_warning(self):
        """A refused REJECTED record would leave an older APPROVED latest_dossier.json live."""
        self.record_prod("abababab-0000-0000-0000-000000000003", APPROVED_SHORT)
        rejected = {"status": "REJECTED", "target_env": "PROD", "approved_candidates": [], "summary": "no"}
        bad = checklist_for(rejected).replace("verdict -> REJECTED", "verdict -> APPROVED")
        self.agy_transcript("abababab-0000-0000-0000-000000000004", dossier_text(rejected, checklist=bad))
        code, _, err = self.run_main(["--from-subagent", "abababab-0000-0000-0000-000000000004", "--env", "prod"])
        self.assertEqual(code, 0, err)
        self.assertIn("CHECKLIST WARNING (PROD, status REJECTED)", err)
        self.assertEqual(dp.load_dossier(self.dossier_path())["status"], "REJECTED")
        ok, _, _ = self.validate("FILUSDT", "SHORT")
        self.assertFalse(ok, "the older APPROVED dossier must no longer authorize anything")
        # Missing checklist on a NEUTRAL dossier: same treatment
        neutral = {"status": "NEUTRAL", "target_env": "PROD", "approved_candidates": []}
        self.claude_transcript("a2700000000000003", dossier_text(neutral, checklist=False))
        code, _, err = self.run_main(["--from-claude-subagent", "a2700000000000003", "--env", "prod"])
        self.assertEqual(code, 0, err)
        self.assertIn("CHECKLIST WARNING (PROD, status NEUTRAL)", err)

    def test_prod_c0_stop_rejected_dossier_recorded_without_warning(self):
        payload = {"status": "REJECTED", "target_env": "PROD", "approved_candidates": [], "summary": "STALE_BRIEF: x"}
        self.agy_transcript("abababab-0000-0000-0000-000000000005", c0_stop_text(payload))
        code, _, err = self.run_main(["--from-subagent", "abababab-0000-0000-0000-000000000005", "--env", "prod"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("CHECKLIST WARNING", err)
        self.assertEqual(dp.load_dossier(self.dossier_path())["status"], "REJECTED")

    def test_prod_yolo_candidate_and_rejected_lines_recorded(self):
        text = checklist_for(PEPE_APPROVED).replace("## Precondition Checklist\n", "## Precondition Checklist\n" + "".join(
            f"- [ ] PEPEUSDT LONG {c} gate -> FAIL\n" for c in ("K1", "K2", "K3", "C3.1", "K4")))
        self.agy_transcript("abababab-0000-0000-0000-000000000006", dossier_text(PEPE_APPROVED, checklist=text))
        code, _, err = self.run_main(["--from-subagent", "abababab-0000-0000-0000-000000000006", "--env", "prod"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("CHECKLIST WARNING", err)

    def test_stored_record_keeps_snapshots_and_hash_without_final_text(self):
        path = self.agy_transcript("abababab-0000-0000-0000-000000000007", dossier_text(APPROVED_SHORT))
        extracted = dp.extract_dossier_from_transcript(path)
        self.assertIn("## Precondition Checklist", extracted["final_text"])
        self.assertNotIn("final_text", dp.build_record_from_extraction(extracted))
        code, _, err = self.run_main(["--from-subagent", "abababab-0000-0000-0000-000000000007", "--env", "prod"])
        self.assertEqual(code, 0, err)
        with open(self.dossier_path(), encoding="utf-8") as f:
            stored_text = f.read()
        record = json.loads(stored_text)
        self.assertIn("FILUSDT|SHORT", record["radar_snapshots"])
        self.assertEqual(record["provenance"]["sha256"], dp.sha256_text(extracted["raw"]))
        self.assertNotIn("final_text", stored_text)
        self.assertNotIn("Precondition Checklist", stored_text)
        self.assertEqual(self.history()[-1]["sha256"], record["provenance"]["sha256"])
        ok, reason, cand = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)
        self.assertEqual(cand["dossier_sha256"], record["provenance"]["sha256"])
        claude = self.claude_transcript("a2700000000000004", dossier_text(APPROVED_SHORT))
        self.assertIn("## Precondition Checklist", dp.extract_dossier_from_claude_transcript(claude)["final_text"])

    def test_truncated_agy_checklist_is_read_from_the_full_transcript(self):
        conv = "abababab-0000-0000-0000-000000000008"
        ts = self.now - 30
        steps = [self.system_step(ts - 5), self.send_message_step(ts, dossier_text(APPROVED_SHORT, header=LONG_HEADER))]
        path = self.write_transcript(conv, steps, truncate={1: ["tool_calls"]})
        self.assertNotIn("Precondition Checklist", self.load_rows(path)[1]["tool_calls"][0]["args"]["Message"])
        code, _, err = self.run_main(["--from-subagent", conv, "--env", "prod"])
        self.assertEqual(code, 0, err)
        self.assertTrue(dp.load_dossier(self.dossier_path())["provenance"]["full_transcript_used"])
        # The same cut over an inconsistent checklist is refused: the check reads the resolved text
        os.remove(self.dossier_path())
        conv2 = "abababab-0000-0000-0000-000000000009"
        bad = checklist_for(APPROVED_SHORT).replace("- [x] FILUSDT SHORT K3", "- [ ] FILUSDT SHORT K3")
        steps = [self.system_step(ts - 5),
                 self.send_message_step(ts, dossier_text(APPROVED_SHORT, header=LONG_HEADER, checklist=bad))]
        self.write_transcript(conv2, steps, truncate={1: ["tool_calls"]})
        self.assertRefused("--from-subagent", conv2, "K3 is not checked")


# =============================================================================
# Issue #43: read_agy_steps hardening
# =============================================================================
class TestAgyReaderHardening(TranscriptFixture):

    def truncated(self, conv: str) -> str:
        ts = self.now - 30
        steps = [self.system_step(ts - 5)] + self.view_file_steps(ts - 3) + \
            [self.send_message_step(ts, dossier_text(APPROVED_SHORT, header=LONG_HEADER))]
        return self.write_transcript(conv, steps, truncate={3: ["tool_calls"]})

    def write_lines(self, path: str, lines: list, newline: str = "\n") -> None:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("".join(line + newline for line in lines))

    def raw_lines(self, path: str) -> list:
        with open(path, encoding="utf-8") as f:
            return [l.rstrip("\r\n") for l in f if l.strip()]

    def assertProvenanceError(self, path: str, fragment: str):
        with self.assertRaises(dp.ProvenanceError) as cm:
            dp.extract_dossier_from_transcript(path)
        self.assertIn(fragment, str(cm.exception))

    def test_marker_without_truncated_fields_fails_closed(self):
        # send_message arg cut like agy, but the row does not list truncated_fields
        path = self.truncated("cdcdcdcd-0000-0000-0000-000000000001")
        self.edit_row(path, 3, lambda row: row.pop("truncated_fields"))
        self.assertProvenanceError(path, "no truncated_fields")
        # Planner content middle-cut without truncated_fields (an earlier complete dossier must not win)
        conv = "cdcdcdcd-0000-0000-0000-000000000002"
        steps = [self.system_step(self.now), self.content_step(self.now - 5, dossier_text(APPROVED_SHORT)),
                 self.content_step(self.now, agy_middle_cut(dossier_text({"status": "REJECTED"}, header=LONG_HEADER)))]
        self.assertProvenanceError(self.write_transcript(conv, steps), "no truncated_fields")

    def test_model_row_with_truncated_tool_calls_and_no_send_message(self):
        conv = "cdcdcdcd-0000-0000-0000-000000000003"
        ts = self.now - 30
        view_call, view_result = self.view_file_steps(ts - 3)
        view_call["tool_calls"][0]["args"]["AbsolutePath"] = json.dumps("logs/" + "deep/" * 40 + "primed_brief.json")
        steps = [self.system_step(ts - 5), view_call, view_result, self.send_message_step(ts, dossier_text(APPROVED_SHORT))]
        path = self.write_transcript(conv, steps, truncate={1: ["tool_calls"]}, full=False)
        self.assertIn("\n<truncated ", self.load_rows(path)[1]["tool_calls"][0]["args"]["AbsolutePath"])
        ex = dp.extract_dossier_from_transcript(path)
        self.assertEqual(ex["dossier"]["status"], "APPROVED")
        self.assertFalse(ex["full_transcript_used"])

    def test_extra_or_unparsable_line_in_one_file_still_resolves(self):
        layouts = {
            "extra row in full": lambda short, full: (short, [json.dumps({"step_index": 99, "source": "SYSTEM"})] + full),
            "unparsable line in full": lambda short, full: (short, ["{not json"] + full),
            "unparsable line in short": lambda short, full: (short[:2] + ["{not json"] + short[2:], full),
            "full rows reordered": lambda short, full: (short, list(reversed(full))),
        }
        for i, (label, change) in enumerate(layouts.items()):
            with self.subTest(label):
                path = self.truncated(f"cdcdcdcd-1000-0000-0000-00000000000{i}")
                full_path = dp.full_transcript_path(path)
                short, full = change(self.raw_lines(path), self.raw_lines(full_path))
                self.write_lines(path, short)
                self.write_lines(full_path, full)
                ex = dp.extract_dossier_from_transcript(path)
                self.assertEqual((ex["step_index"], ex["full_transcript_used"]), (3, True))

    def test_crlf_in_only_one_file(self):
        for i, which in enumerate(("short", "full")):
            with self.subTest(which):
                path = self.truncated(f"cdcdcdcd-2000-0000-0000-00000000000{i}")
                target = path if which == "short" else dp.full_transcript_path(path)
                self.write_lines(target, self.raw_lines(target), newline="\r\n")
                ex = dp.extract_dossier_from_transcript(path)
                self.assertTrue(ex["full_transcript_used"])

    def test_duplicate_or_missing_step_index(self):
        path = self.truncated("cdcdcdcd-3000-0000-0000-000000000001")
        full_path = dp.full_transcript_path(path)
        full = self.raw_lines(full_path)
        self.write_lines(full_path, full + [full[3]])
        self.assertProvenanceError(path, "duplicate step_index in transcript_full.jsonl")

        path = self.truncated("cdcdcdcd-3000-0000-0000-000000000002")
        self.edit_row(path, 1, lambda row: row.update(step_index=3))
        self.assertProvenanceError(path, "duplicate step_index in transcript.jsonl")

        path = self.truncated("cdcdcdcd-3000-0000-0000-000000000003")
        self.edit_row(path, 3, lambda row: row.pop("step_index"))
        self.assertProvenanceError(path, "transcript_full.jsonl mismatch at step None: missing or invalid step_index")

        path = self.truncated("cdcdcdcd-3000-0000-0000-000000000004")
        full_path = dp.full_transcript_path(path)
        self.write_lines(full_path, self.raw_lines(full_path)[:3])
        self.assertProvenanceError(path, "transcript_full.jsonl mismatch at step 3: row missing or unreadable")

    def test_encoder_differences_in_untruncated_args(self):
        """agy's compact encoder ('{"a":1}', '\\u003c') vs Python's json.dumps in the full row."""
        conv = "cdcdcdcd-4000-0000-0000-000000000001"
        ts = self.now - 30
        step = self.send_message_step(ts, dossier_text(APPROVED_SHORT, header=LONG_HEADER))
        compact = json.dumps({"a": 1, "t": "<b>"}, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")
        step["tool_calls"].append({"name": "notify", "args": {"Options": compact}})
        path = self.write_transcript(conv, [self.system_step(ts - 5), step], truncate={1: ["tool_calls"]})
        self.assertEqual(self.load_rows(path)[1]["tool_calls"][1]["args"]["Options"], '{"a":1,"t":"\\u003cb\\u003e"}')
        self.assertEqual(self.load_rows(dp.full_transcript_path(path))[1]["tool_calls"][1]["args"]["Options"],
                         {"a": 1, "t": "<b>"})
        ex = dp.extract_dossier_from_transcript(path)
        self.assertTrue(ex["full_transcript_used"])
        # A different value (or the same digits with another JSON type) is still a mismatch
        for other in ({"a": 2, "t": "<b>"}, {"a": True, "t": "<b>"}, {"a": 1.0, "t": "<b>"}):
            with self.subTest(other=other):
                self.edit_row(dp.full_transcript_path(path), 1,
                              lambda row: row["tool_calls"][1]["args"].update(Options=other))
                self.assertProvenanceError(path, "tool_calls[1].args.Options differs")

    def test_malformed_rows_raise_provenance_error(self):
        model = {"source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": iso(self.now), "content": ""}
        rows = {
            "non-list tool_calls": dict(model, tool_calls="send_message"),
            "non-dict call": dict(model, tool_calls=["send_message"]),
            "non-dict args": dict(model, tool_calls=[{"name": "send_message", "args": ["x"]}]),
        }
        for i, (label, row) in enumerate(rows.items()):
            with self.subTest(label):
                with self.assertRaises(dp.ProvenanceError):
                    dp._model_texts(row)  # assemble_review.py calls it directly
                path = self.write_transcript(f"cdcdcdcd-5000-0000-0000-00000000000{i}", [self.system_step(self.now), row])
                self.assertProvenanceError(path, "Malformed")
        with self.assertRaises(dp.ProvenanceError):
            dp._model_texts(["not", "a", "row"])
        # Truncated row whose tool_calls is not a list: resolution fails closed
        bad = dict(model, tool_calls={"name": "send_message"}, truncated_fields=["tool_calls"])
        path = self.write_transcript("cdcdcdcd-5000-0000-0000-00000000000a", [self.system_step(self.now), bad], full=True)
        self.edit_row(dp.full_transcript_path(path), 1, lambda row: row.pop("truncated_fields"))
        with self.assertRaises(dp.ProvenanceError) as cm:
            dp.read_agy_steps(path)
        self.assertIn("tool_calls count differs", str(cm.exception))

    def test_non_object_and_deeply_nested_lines(self):
        path = self.standard_transcript("cdcdcdcd-6000-0000-0000-000000000001", APPROVED_SHORT)
        lines = self.raw_lines(path)
        self.write_lines(path, lines[:1] + ["[1, 2]"] + lines[1:])
        self.assertProvenanceError(path, "not a JSON object")
        deep = "[" * 100000 + "]" * 100000
        path = self.standard_transcript("cdcdcdcd-6000-0000-0000-000000000002", APPROVED_SHORT)
        self.write_lines(path, self.raw_lines(path) + [deep])
        self.assertProvenanceError(path, "nested too deeply")
        # Same line in transcript_full.jsonl, needed for a truncated row
        path = self.truncated("cdcdcdcd-6000-0000-0000-000000000003")
        full_path = dp.full_transcript_path(path)
        self.write_lines(full_path, self.raw_lines(full_path) + [deep])
        self.assertProvenanceError(path, "nested too deeply")

    def test_legacy_record_without_full_transcript_used_still_verifies(self):
        self.record_prod("cdcdcdcd-7000-0000-0000-000000000001", APPROVED_SHORT)
        path = dp.default_dossier_path(self.workspace)
        record = dp.load_dossier(path)
        record["provenance"].pop("full_transcript_used")
        record["provenance"].pop("resolved_steps")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        ok, reason, rebuilt = dp.rebuild_verified_record(record)
        self.assertTrue(ok, reason)
        self.assertFalse(rebuilt["provenance"]["full_transcript_used"])
        ok, reason, _ = self.validate("FILUSDT", "SHORT")
        self.assertTrue(ok, reason)


class TestClaudeReaderHardening(ClaudeTranscriptFixture):

    def test_malformed_claude_rows_raise_provenance_error(self):
        agent = "a4300000000000001"
        row = self.text_row(self.now, "x")
        row["message"]["content"] = 42
        with self.assertRaises(dp.ProvenanceError):
            dp.extract_dossier_from_claude_transcript(self.write_claude(agent, [row]))
        agent = "a4300000000000002"
        path = self.standard_claude(agent, APPROVED_SHORT)
        with open(path, "a", encoding="utf-8") as f:
            f.write("[" * 100000 + "]" * 100000 + "\n")
        with self.assertRaises(dp.ProvenanceError) as cm:
            dp.extract_dossier_from_claude_transcript(path)
        self.assertIn("nested too deeply", str(cm.exception))


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
