#!/usr/bin/env python3
"""
dossier_provenance.py - Binds evaluation dossiers to the real evaluator subagent output.

The isolated_market_evaluator subagent runs as its own Antigravity conversation and writes
its Master Dossier (ending in a <dossier_json> block) into that conversation's transcript:
    ~/.gemini/<product>/brain/<conversationId>/.system_generated/logs/transcript.jsonl

record_evaluation.py --from-subagent <conversationId> extracts that block and stores a
provenance stamp (transcript path, step index, sha256 of the block). Every consumer
(pre_trade_guard.py hook, execute_futures_trade.py) re-verifies the stamp against the
transcript before allowing an order, so a dossier typed by the main agent is rejected.

Single source of truth for dossier validation: validate_dossier_for_trade().
"""

import datetime
import glob
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

EVALUATOR_NAME = "isolated_market_evaluator"
TTL_SECONDS = 1200  # 20 minutes, counted from the moment the evaluator emitted the dossier
SCHEMA_VERSION = 2
CLOCK_DRIFT_TOLERANCE_S = 5
VALID_STATUSES = ("APPROVED", "REJECTED", "NEUTRAL")
VALID_DIRECTIONS = ("LONG", "SHORT")

# Antigravity products keep conversation data under ~/.gemini/<product>/brain
AGY_PRODUCT_DIRS = ("antigravity", "antigravity-cli", "antigravity-ide")
TRANSCRIPT_REL = os.path.join(".system_generated", "logs", "transcript.jsonl")

DOSSIER_RE = re.compile(r"<dossier_json>\s*([\s\S]*?)\s*</dossier_json>")
CONVERSATION_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
SENDER_RE = re.compile(r"sender=([0-9a-fA-F-]{8,64})")


class ProvenanceError(Exception):
    pass


def default_dossier_path(base_dir: str) -> str:
    return os.path.join(base_dir, "logs", "evaluations", "latest_dossier.json")


def brain_roots() -> list:
    """Directories that may contain Antigravity conversation folders.
    AGY_BRAIN_DIRS (os.pathsep-separated) overrides the defaults."""
    override = os.environ.get("AGY_BRAIN_DIRS")
    if override:
        return [p for p in override.split(os.pathsep) if p]
    home = os.path.expanduser("~")
    return [os.path.join(home, ".gemini", product, "brain") for product in AGY_PRODUCT_DIRS]


def find_subagent_transcript(conversation_id: str) -> str:
    """Resolves a subagent conversation id (full or unique prefix) to its transcript.jsonl."""
    conversation_id = (conversation_id or "").strip()
    if not CONVERSATION_ID_RE.match(conversation_id):
        raise ProvenanceError(f"Invalid subagent conversation id '{conversation_id}'.")

    matches = []
    for root in brain_roots():
        for conv_dir in glob.glob(os.path.join(root, conversation_id + "*")):
            path = os.path.join(conv_dir, TRANSCRIPT_REL)
            if os.path.isfile(path):
                matches.append(path)

    if not matches:
        raise ProvenanceError(
            f"No transcript found for subagent conversation '{conversation_id}' in: {', '.join(brain_roots())}"
        )
    if len(matches) > 1:
        raise ProvenanceError(f"Conversation id prefix '{conversation_id}' is ambiguous ({len(matches)} matches).")
    return matches[0]


def _read_steps(path: str) -> list:
    steps = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return steps


def _decode_arg(value: Any) -> str:
    # Tool call args are stored JSON-encoded (e.g. "\"text\"")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith('"'):
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, str):
                    return decoded
            except json.JSONDecodeError:
                pass
        return value
    return json.dumps(value)


def _model_texts(step: dict) -> list:
    """Text the model itself produced in a step: its response and the args of send_message calls.
    Tool results (e.g. view_file of the prompt with its few-shot dossiers) are never PLANNER_RESPONSE."""
    if step.get("source") != "MODEL" or step.get("type") != "PLANNER_RESPONSE":
        return []
    texts = []
    if isinstance(step.get("content"), str):
        texts.append(step["content"])
    for call in step.get("tool_calls") or []:
        if call.get("name") == "send_message":
            for value in (call.get("args") or {}).values():
                texts.append(_decode_arg(value))
    return texts


def _parse_created_at(value: Any) -> int:
    if not value:
        return 0
    try:
        dt = datetime.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    except ValueError:
        return 0


def _parse_block(raw: str) -> Optional[dict]:
    """Parses a <dossier_json> body. Tool-call args may still carry one level of string escaping."""
    for candidate in (raw, None):
        if candidate is None:
            try:
                candidate = json.loads('"' + raw + '"')
            except json.JSONDecodeError:
                return None
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_dossier_from_transcript(path: str) -> Dict[str, Any]:
    """Returns the last <dossier_json> block the subagent model emitted, with its provenance."""
    steps = _read_steps(path)
    if not steps:
        raise ProvenanceError(f"Transcript is empty or unreadable: {path}")

    parent_id = None
    for step in steps[:3]:
        if step.get("source") == "SYSTEM" and isinstance(step.get("content"), str):
            m = SENDER_RE.search(step["content"])
            if m:
                parent_id = m.group(1)
                break

    found = None
    saw_block = False
    for step in steps:
        for text in _model_texts(step):
            for m in DOSSIER_RE.finditer(text):
                saw_block = True
                raw = m.group(1).strip()
                parsed = _parse_block(raw)
                if parsed is not None:
                    found = (step, raw, parsed)
                    break

    if not found:
        if saw_block:
            raise ProvenanceError(f"<dossier_json> block emitted by the subagent is not a valid JSON object ({path}).")
        raise ProvenanceError(f"No <dossier_json> block emitted by the subagent in {path}")

    step, raw, dossier = found

    return {
        "raw": raw,
        "sha256": sha256_text(raw),
        "dossier": dossier,
        "step_index": step.get("step_index"),
        "created_at_ts": _parse_created_at(step.get("created_at")),
        "conversation_id": os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path)))),
        "parent_conversation_id": parent_id,
        "transcript_path": os.path.abspath(path),
    }


def normalize_status(status: Any) -> Tuple[str, bool]:
    """Maps the evaluator verdict to APPROVED/REJECTED/NEUTRAL.
    Returns (status, pending_confirmation). Unknown values fail closed to REJECTED."""
    s = str(status or "").strip().upper()
    if s in VALID_STATUSES:
        return s, False
    if s.startswith("APPROVED"):  # e.g. APPROVED_PENDING_CONFIRMATION
        return "APPROVED", "PENDING" in s or "CONFIRM" in s
    return "REJECTED", False


def normalize_candidates(dossier: dict, pending_confirmation: bool = False) -> list:
    out = []
    for cand in dossier.get("approved_candidates") or []:
        if not isinstance(cand, dict):
            continue
        symbol = str(cand.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        c = dict(cand)
        c["symbol"] = symbol
        direction = str(cand.get("direction", "")).strip().upper()
        c["direction"] = direction if direction in VALID_DIRECTIONS else None
        if pending_confirmation and "requires_user_confirmation" not in c:
            c["requires_user_confirmation"] = True
        out.append(c)
    return out


def build_record_from_extraction(extracted: Dict[str, Any], recorded_at_ts: Optional[int] = None) -> Dict[str, Any]:
    dossier = extracted["dossier"]
    status, pending = normalize_status(dossier.get("status"))
    candidates = normalize_candidates(dossier, pending) if status == "APPROVED" else []
    evaluated_at = extracted.get("created_at_ts") or 0
    recorded_at = int(recorded_at_ts or time.time())
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp_utc": datetime.datetime.fromtimestamp(evaluated_at, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "timestamp_ts": evaluated_at,
        "valid_until_ts": evaluated_at + TTL_SECONDS,
        "recorded_at_ts": recorded_at,
        "evaluator_agent": EVALUATOR_NAME,
        "conversation_id": extracted["conversation_id"],
        "parent_conversation_id": extracted.get("parent_conversation_id"),
        "status": status,
        "approved_symbols": [c["symbol"] for c in candidates],
        "approved_candidates": candidates,
        "summary": str(dossier.get("summary", "")).strip(),
        "provenance": {
            "source": "agy_subagent_transcript",
            "transcript_path": extracted["transcript_path"],
            "step_index": extracted.get("step_index"),
            "sha256": extracted["sha256"],
        },
        "raw_payload": dossier,
    }


def _verdict_fingerprint(record: dict) -> tuple:
    cands = sorted(
        (str(c.get("symbol", "")).upper(), str(c.get("direction") or "").upper(), bool(c.get("is_yolo")))
        for c in record.get("approved_candidates") or [] if isinstance(c, dict)
    )
    return (
        str(record.get("status", "")).upper(),
        tuple(sorted(str(s).upper() for s in record.get("approved_symbols") or [])),
        tuple(cands),
        record.get("timestamp_ts"),
        record.get("conversation_id"),
    )


def rebuild_verified_record(record: dict) -> Tuple[bool, str, Optional[dict]]:
    """Re-reads the subagent transcript, checks the stored hash, and rebuilds the record from what the
    evaluator actually emitted. Any hand edit to verdict fields (status, symbols, directions, timestamps)
    makes the stored record differ from the rebuilt one and fails verification."""
    prov = record.get("provenance")
    if not isinstance(prov, dict) or prov.get("source") != "agy_subagent_transcript":
        return False, "Dossier has no subagent provenance (it was not recorded with --from-subagent).", None

    path = prov.get("transcript_path")
    if not path or not os.path.isfile(path):
        return False, f"Subagent transcript not found: {path}", None
    if os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path)))) != record.get("conversation_id"):
        return False, "Dossier conversation_id does not match its transcript path.", None

    try:
        extracted = extract_dossier_from_transcript(path)
    except ProvenanceError as e:
        return False, str(e), None

    parent = extracted.get("parent_conversation_id")
    if not parent or parent == extracted["conversation_id"]:
        return False, "Transcript is not a subagent conversation (no parent sender).", None
    if extracted["sha256"] != prov.get("sha256") or extracted["step_index"] != prov.get("step_index"):
        return False, "Dossier hash does not match the latest <dossier_json> emitted by the evaluator subagent.", None

    rebuilt = build_record_from_extraction(extracted, record.get("recorded_at_ts"))
    if _verdict_fingerprint(rebuilt) != _verdict_fingerprint(record):
        return False, "Dossier verdict fields differ from what the evaluator subagent emitted (edited record).", None
    return True, "Provenance verified against evaluator transcript.", rebuilt


def verify_provenance(record: dict) -> Tuple[bool, str]:
    ok, reason, _ = rebuild_verified_record(record)
    return ok, reason


def load_dossier(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Evaluation dossier is not a JSON object.")
    return data


def find_candidate(record: dict, symbol: str) -> Optional[dict]:
    symbol = (symbol or "").upper()
    for cand in record.get("approved_candidates") or []:
        if isinstance(cand, dict) and str(cand.get("symbol", "")).upper() == symbol:
            return cand
    return None


def validate_dossier_for_trade(
    symbol: str,
    direction: Optional[str] = None,
    env: str = "prod",
    base_dir: Optional[str] = None,
    dossier_path: Optional[str] = None,
    now_ts: Optional[int] = None,
    require_provenance: Optional[bool] = None,
) -> Tuple[bool, str, Optional[dict]]:
    """
    Single gate used by the PreToolUse hook and the execution engine.
    PROD (require_provenance=True): schema v2, signed by isolated_market_evaluator, provenance hash
    verified against the subagent transcript, fresh (< TTL since the evaluator emitted it), APPROVED,
    symbol approved, and direction equal to the approved candidate direction.
    TESTNET: provenance and direction are only enforced when present.
    Returns (ok, reason, candidate).
    """
    env = (env or "prod").lower()
    if require_provenance is None:
        require_provenance = env == "prod"
    now_ts = int(now_ts if now_ts is not None else time.time())
    if dossier_path is None:
        if not base_dir:
            return False, "No dossier path or workspace root provided.", None
        dossier_path = default_dossier_path(base_dir)

    if not os.path.exists(dossier_path):
        return False, (
            "No evaluation dossier at logs/evaluations/latest_dossier.json. Invoke the "
            f"'{EVALUATOR_NAME}' subagent and record its verdict with "
            "`record_evaluation.py --from-subagent <conversationId>`."
        ), None
    try:
        record = load_dossier(dossier_path)
    except Exception as e:
        return False, f"Failed to read evaluation dossier ({e}).", None

    try:
        ts = int(record.get("timestamp_ts"))
    except (TypeError, ValueError):
        return False, "Evaluation dossier timestamp_ts is missing or invalid.", None
    if ts <= 0:
        return False, "Evaluation dossier timestamp_ts must be positive.", None
    if ts > now_ts + CLOCK_DRIFT_TOLERANCE_S:
        return False, f"Evaluation dossier timestamp_ts ({ts}) is in the future (now: {now_ts}).", None
    try:
        valid_until = int(record.get("valid_until_ts", 0))
    except (TypeError, ValueError):
        valid_until = 0
    expiry = min(valid_until, ts + TTL_SECONDS)
    if now_ts > expiry:
        return False, f"Evaluation dossier has expired (expiry: {expiry}, now: {now_ts}). Re-run the evaluator.", None

    status = str(record.get("status", "")).upper()
    if status != "APPROVED":
        return False, f"Evaluation dossier status is '{status}', expected 'APPROVED'.", None

    if require_provenance:
        try:
            schema = int(record.get("schema_version", 1))
        except (TypeError, ValueError):
            schema = 1
        if schema < SCHEMA_VERSION:
            return False, "Legacy dossier format is not accepted in PROD. Record it with --from-subagent.", None
        if record.get("evaluator_agent") != EVALUATOR_NAME:
            return False, f"Dossier was not signed by '{EVALUATOR_NAME}' (got '{record.get('evaluator_agent')}').", None
        ok, reason, rebuilt = rebuild_verified_record(record)
        if not ok:
            return False, reason, None
        # From here on, trust only what the evaluator emitted
        record = rebuilt

    dossier_env = str((record.get("raw_payload") or {}).get("target_env") or record.get("target_env") or "").strip().lower()
    if dossier_env in ("production", "mainnet"):
        dossier_env = "prod"
    if dossier_env and dossier_env != env:
        return False, f"Dossier was evaluated for {dossier_env.upper()}, but the order targets {env.upper()}.", None

    symbol = (symbol or "").upper()
    if not symbol:
        return False, "Unable to determine the order symbol deterministically.", None
    approved = [str(s).upper() for s in record.get("approved_symbols") or []]
    cand = find_candidate(record, symbol)
    if symbol not in approved or cand is None and require_provenance:
        return False, (
            f"Asset '{symbol}' was NOT approved in the evaluator dossier. "
            f"Approved: {', '.join(approved) if approved else 'NONE'}."
        ), None

    want = (direction or "").upper() or None
    have = (cand or {}).get("direction")
    have = str(have).upper() if have else None
    if require_provenance and (want not in VALID_DIRECTIONS or have not in VALID_DIRECTIONS):
        return False, f"Trade direction for {symbol} could not be matched against the dossier (order: {want}, dossier: {have}).", None
    if want and have and want != have:
        return False, f"Dossier approved {symbol} {have}, but the order is {want}.", None

    return True, "Dossier valid and approved.", cand
