#!/usr/bin/env python3
"""
dossier_provenance.py - Binds evaluation dossiers to the real evaluator subagent output.

The isolated_market_evaluator subagent writes its Master Dossier (ending in a <dossier_json>
block) into its own transcript. Two runtimes are supported:

  * Google Antigravity (agy): the subagent is its own conversation,
        ~/.gemini/<product>/brain/<conversationId>/.system_generated/logs/transcript.jsonl
    record_evaluation.py --from-subagent <conversationId>
    agy truncates long fields in transcript.jsonl (row key "truncated_fields"; content/thinking keep
    head + "\\n<truncated N bytes>\\n" + tail, tool_call args keep a JSON-encoded prefix +
    "\\n<truncated N bytes>"). read_agy_steps() takes those scanned rows from the sibling
    transcript_full.jsonl (paired by step_index, unique in both files) only after cross-checking every
    untruncated field, the head/tail/prefix and N (UTF-8 bytes removed) against transcript.jsonl, and fails
    closed otherwise (also on a marker without truncated_fields and on malformed rows: always ProvenanceError).
    Untruncated rows are read from transcript.jsonl exactly as before.
  * Claude Code: the subagent transcript lives next to its parent session,
        ~/.claude/projects/<project-slug>/<parentSessionId>/subagents/agent-<agentId>.jsonl
    with agent-<agentId>.meta.json ({"agentType": "isolated_market_evaluator", ...}).
    record_evaluation.py --from-claude-subagent <agentId>  (or --from-subagent, auto-detected)
    The meta agentType MUST be the evaluator: a general-purpose agent cannot sign a dossier.

The recorder stores a provenance stamp (source, transcript path, step, sha256 of the block; agy also
full_transcript_used / resolved_steps).
Every consumer (pre_trade_guard.py hook, execute_futures_trade.py) re-verifies the stamp against
the transcript before allowing an order, so a dossier typed by the main agent is rejected.

Single source of truth for dossier validation: validate_dossier_for_trade().
check_precondition_checklist() compares the evaluator's visible checklist with its block; only the recorder
calls it (record time), using the extraction's final_text, which is never stored nor hashed.
"""

import datetime
import glob
import hashlib
import json
import math
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
TRANSCRIPT_FULL_NAME = "transcript_full.jsonl"
TRANSCRIPT_FULL_REL = os.path.join(".system_generated", "logs", TRANSCRIPT_FULL_NAME)
TRUNCATED_MARKER_RE = re.compile(r"\n<truncated (\d+) bytes>")
TRUNCATED_ARG_RE = re.compile(r"\A([\s\S]*)\n<truncated (\d+) bytes>\Z")
AGY_ROW_KEYS = ("step_index", "source", "type", "status", "created_at")

DOSSIER_RE = re.compile(r"<dossier_json>\s*([\s\S]*?)\s*</dossier_json>")
CONVERSATION_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
SENDER_RE = re.compile(r"sender=([0-9a-fA-F-]{8,64})")

# Provenance sources
AGY_SOURCE = "agy_subagent_transcript"
CLAUDE_SOURCE = "claude_subagent_transcript"
SUBAGENT_SOURCES = (AGY_SOURCE, CLAUDE_SOURCE)

# Claude Code subagent ids look like "a" + 16 hex chars (no dashes). agy ids are UUIDs, whose
# first dash comes after 8 hex chars, so the two formats never collide.
CLAUDE_AGENT_ID_RE = re.compile(r"^a[0-9a-f]{12,63}$")
CLAUDE_TRANSCRIPT_RE = re.compile(r"^agent-(a[0-9a-f]{12,63})\.jsonl$")
CLAUDE_PROJECTS_ENV = "CLAUDE_PROJECTS_DIRS"


class ProvenanceError(Exception):
    pass


def default_dossier_path(base_dir: str) -> str:
    return os.path.join(base_dir, "logs", "evaluations", "latest_dossier.json")


def is_claude_agent_id(value: str) -> bool:
    value = (value or "").strip()
    if value.startswith("agent-"):
        value = value[len("agent-"):]
    return bool(CLAUDE_AGENT_ID_RE.match(value))


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


def _parse_line(line: str, path: str) -> Any:
    """json.loads of one transcript line; a line nested too deeply to parse is hostile, not skippable."""
    try:
        return json.loads(line)
    except RecursionError:
        raise ProvenanceError(f"Transcript line nested too deeply to parse ({path}).")


def _read_steps(path: str) -> list:
    steps = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                steps.append(_parse_line(line, path))
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
            except (json.JSONDecodeError, RecursionError):
                pass
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError, RecursionError) as e:
        raise ProvenanceError(f"Tool call argument cannot be decoded: {type(e).__name__}")


def _model_texts(step: Any) -> list:
    """Text the model itself produced in a step: its response and the args of send_message calls.
    Tool results (e.g. view_file of the prompt with its few-shot dossiers) are never PLANNER_RESPONSE.
    Raises ProvenanceError on a malformed row (non-dict step, non-list tool_calls, non-dict call or args)."""
    if not isinstance(step, dict):
        raise ProvenanceError(f"Transcript row is not a JSON object ({type(step).__name__}).")
    if step.get("source") != "MODEL" or step.get("type") != "PLANNER_RESPONSE":
        return []
    texts = []
    if isinstance(step.get("content"), str):
        texts.append(step["content"])
    calls = step.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ProvenanceError(f"Malformed tool_calls at step {step.get('step_index')} (not a list).")
    for call in calls:
        if not isinstance(call, dict):
            raise ProvenanceError(f"Malformed tool call at step {step.get('step_index')} (not an object).")
        if call.get("name") == "send_message":
            args = call.get("args") or {}
            if not isinstance(args, dict):
                raise ProvenanceError(f"Malformed send_message args at step {step.get('step_index')} (not an object).")
            for value in args.values():
                texts.append(_decode_arg(value))
    return texts


# =============================================================================
# agy transcript truncation: resolve scanned rows from transcript_full.jsonl
# =============================================================================
def full_transcript_path(path: str) -> str:
    """Sibling transcript_full.jsonl of an agy transcript.jsonl (same logs/ dir, rows paired by step_index)."""
    return os.path.join(os.path.dirname(path), TRANSCRIPT_FULL_NAME)


def _read_rows_by_line(path: str) -> list:
    """Parsed JSON value per physical line (None for blank or unparsable lines).
    Raises ProvenanceError for a line nested too deeply to parse."""
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            try:
                rows.append(_parse_line(line, path) if line else None)
            except json.JSONDecodeError:
                rows.append(None)
    return rows


def _step_key(row: dict) -> Optional[int]:
    """step_index usable as a pairing key (an int, never a bool), else None."""
    idx = row.get("step_index")
    return idx if isinstance(idx, int) and not isinstance(idx, bool) else None


def _rows_by_step_index(rows: list, label: str) -> dict:
    """step_index -> row for the JSON-object rows of one file; a duplicate step_index fails closed."""
    by_index = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        idx = _step_key(row)
        if idx is None:
            continue
        if idx in by_index:
            raise ProvenanceError(f"{TRANSCRIPT_FULL_NAME} mismatch at step {idx}: duplicate step_index in {label}")
        by_index[idx] = row
    return by_index


def _needs_full_row(row: Any) -> bool:
    """True for a truncated row whose model text is scanned (PLANNER_RESPONSE content or send_message args)."""
    if not isinstance(row, dict) or not row.get("truncated_fields"):
        return False
    if row.get("source") != "MODEL" or row.get("type") != "PLANNER_RESPONSE":
        return False
    fields = row["truncated_fields"]
    if not isinstance(fields, list):
        return True  # malformed: resolve, so the cross-check fails closed
    if "content" in fields:
        return True
    if "tool_calls" not in fields:
        return False
    calls = row.get("tool_calls") or []
    if not isinstance(calls, list):
        return True  # malformed: resolve, so the cross-check fails closed
    return any(isinstance(c, dict) and c.get("name") == "send_message" for c in calls)


def _same_json_value(short_val: Any, full_val: Any) -> bool:
    """True when an untruncated transcript.jsonl arg (JSON-encoded by agy's own encoder, whose separators and
    escaping may differ from Python's, e.g. '{"a":1}' or '\\u003c') decodes to the transcript_full.jsonl value.
    Compared through a canonical encoding so 1, 1.0 and true stay distinct."""
    if not isinstance(short_val, str):
        return False
    try:
        decoded = json.loads(short_val)
        canon = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False)  # noqa: E731
        return canon(decoded) == canon(full_val)
    except (ValueError, TypeError, RecursionError):
        return False


def _middle_cut_matches(short: Any, full: Any) -> bool:
    """agy content/thinking truncation: head + '\\n<truncated N bytes>\\n' + tail, N = UTF-8 bytes removed."""
    if not isinstance(short, str) or not isinstance(full, str):
        return False
    full_bytes = len(full.encode("utf-8"))
    for m in TRUNCATED_MARKER_RE.finditer(short):
        head, tail = short[:m.start()], short[m.end():]
        if not tail.startswith("\n"):
            continue
        tail = tail[1:]
        removed = full_bytes - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
        if removed == int(m.group(1)) and full.startswith(head) and full.endswith(tail):
            return True
    return False


def _resolve_tool_calls(short_calls: Any, full_calls: Any, truncated: bool, bad) -> list:
    """Cross-checks tool_calls and returns them in transcript.jsonl form (JSON-encoded arg values).
    Truncated args keep a prefix of json.dumps(full_value, ensure_ascii=False) + '\\n<truncated N bytes>',
    N = UTF-8 bytes removed from that encoded value. A prefix cannot be parsed, so that comparison stays
    sensitive to agy's encoder (separators, escaping). Every other arg must decode to the full value: compared
    with json.loads on the short side (so encoder differences do not matter), else as before."""
    if not isinstance(short_calls, list) or not isinstance(full_calls, list) or len(short_calls) != len(full_calls):
        raise bad("tool_calls count differs")
    out = []
    for i, (s, f) in enumerate(zip(short_calls, full_calls)):
        if not isinstance(s, dict) or not isinstance(f, dict) or s.get("name") != f.get("name"):
            raise bad(f"tool_calls[{i}] name differs")
        if set(s) != set(f) or any(s[k] != f[k] for k in s if k != "args"):
            raise bad(f"tool_calls[{i}] fields differ")
        s_args, f_args = s.get("args"), f.get("args")
        if s_args is None and f_args is None:
            out.append(dict(s))
            continue
        if not isinstance(s_args, dict) or not isinstance(f_args, dict) or set(s_args) != set(f_args):
            raise bad(f"tool_calls[{i}] args differ")
        args = {}
        for name, s_val in s_args.items():
            encoded = json.dumps(f_args[name], ensure_ascii=False)
            m = TRUNCATED_ARG_RE.match(s_val) if truncated and isinstance(s_val, str) else None
            if m:
                prefix = m.group(1)
                removed = len(encoded.encode("utf-8")) - len(prefix.encode("utf-8"))
                if removed != int(m.group(2)) or not encoded.startswith(prefix):
                    raise bad(f"truncated tool_calls[{i}].args.{name} does not match the full value")
                args[name] = encoded
            elif not _same_json_value(s_val, f_args[name]) and _decode_arg(s_val) != _decode_arg(encoded):
                raise bad(f"tool_calls[{i}].args.{name} differs")
            else:
                args[name] = s_val
        call = dict(s)
        call["args"] = args
        out.append(call)
    return out


def _resolve_truncated_row(short: dict, full: Any) -> dict:
    """Returns the short row with its truncated fields taken from the full row, after verifying that the
    full row is the same step (identity keys, every untruncated field) and extends every truncated value."""
    step = short.get("step_index")

    def bad(reason: str) -> ProvenanceError:
        return ProvenanceError(f"{TRANSCRIPT_FULL_NAME} mismatch at step {step}: {reason}")

    if not isinstance(full, dict):
        raise bad("row missing or unreadable")
    if "truncated_fields" in full:
        raise bad("full row is truncated too")
    fields = short.get("truncated_fields")
    if not isinstance(fields, list) or not all(isinstance(x, str) for x in fields):
        raise bad("malformed truncated_fields")
    for key in AGY_ROW_KEYS:
        if short.get(key) != full.get(key):
            raise bad(f"'{key}' differs")
    for key in fields:
        if key not in full:
            raise bad(f"truncated field '{key}' missing from the full row")

    resolved = {}
    for key in (set(short) - {"truncated_fields"}) | set(full):
        if key not in short or key not in full:
            raise bad(f"field '{key}' differs")
        if key == "tool_calls":
            resolved[key] = _resolve_tool_calls(short[key], full[key], key in fields, bad)
        elif key in fields:
            if key not in ("content", "thinking"):
                raise bad(f"unsupported truncated field '{key}'")
            if not _middle_cut_matches(short[key], full[key]):
                raise bad(f"truncated '{key}' does not match the full value")
            resolved[key] = full[key]
        elif short[key] != full[key]:
            raise bad(f"field '{key}' differs")
        else:
            resolved[key] = short[key]
    return resolved


def read_agy_steps(path: str) -> Tuple[list, list]:
    """Rows of an agy transcript.jsonl, with every truncated row whose model text is scanned replaced by its
    verified counterpart from transcript_full.jsonl, paired by step_index (unique in both files; rows of the full
    file that no truncated row needs are ignored, so line layouts may differ). Other rows are returned exactly as
    in transcript.jsonl. Returns (steps, resolved_rows). Raises ProvenanceError (never another exception type) if
    a row is not a JSON object or is malformed, if a needed row cannot be resolved, or if a scanned row that is
    not resolved carries the '<truncated N bytes>' marker (agy truncated it without listing truncated_fields)."""
    try:
        return _read_agy_steps(path)
    except (TypeError, AttributeError, ValueError, RecursionError) as e:
        raise ProvenanceError(f"Malformed agy transcript ({path}): {type(e).__name__}: {e}")


def _read_agy_steps(path: str) -> Tuple[list, list]:
    rows = [row for row in _read_rows_by_line(path) if row is not None]
    for row in rows:
        if not isinstance(row, dict):
            raise ProvenanceError(f"Transcript row is not a JSON object ({type(row).__name__}) in {path}")
    needed = [row for row in rows if _needs_full_row(row)]
    needed_ids = {id(row) for row in needed}
    for row in rows:
        if id(row) in needed_ids:
            continue
        if any(TRUNCATED_MARKER_RE.search(text) for text in _model_texts(row)):
            raise ProvenanceError(
                f"transcript.jsonl step {row.get('step_index')} carries a '<truncated N bytes>' marker in the "
                f"subagent output but no truncated_fields: cannot read it safely ({path})."
            )
    full_by_index = {}
    if needed:
        full_path = full_transcript_path(path)
        try:
            full_rows = _read_rows_by_line(full_path)
        except OSError as e:
            raise ProvenanceError(
                f"transcript.jsonl truncates the subagent output at step {needed[0].get('step_index')} and "
                f"{TRANSCRIPT_FULL_NAME} is missing or unreadable ({full_path}): {e}"
            )
        _rows_by_step_index(rows, "transcript.jsonl")
        full_by_index = _rows_by_step_index(full_rows, TRANSCRIPT_FULL_NAME)
    steps, resolved = [], []
    for row in rows:
        if id(row) in needed_ids:
            idx = _step_key(row)
            if idx is None:
                raise ProvenanceError(
                    f"{TRANSCRIPT_FULL_NAME} mismatch at step {row.get('step_index')}: missing or invalid step_index")
            row = _resolve_truncated_row(row, full_by_index.get(idx))
            resolved.append(row)
        steps.append(row)
    return steps, resolved


def _parse_created_at(value: Any) -> int:
    """ISO-8601 UTC timestamp ('2026-10-05T00:11:07Z' or with fractional seconds) -> epoch seconds."""
    if not value:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.datetime.strptime(str(value), fmt)
            return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            continue
    return 0


def _parse_block_ex(raw: str) -> Tuple[Optional[dict], bool]:
    """Parses a <dossier_json> body. Tool-call args may still carry one level of string escaping.
    Returns (dossier or None, True when that single-escaped decode was needed)."""
    for candidate in (raw, None):
        escaped = candidate is None
        if escaped:
            try:
                candidate = json.loads('"' + raw + '"')
            except (json.JSONDecodeError, RecursionError):
                return None, escaped
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, RecursionError):
            continue
        return (parsed if isinstance(parsed, dict) else None), escaped
    return None, False


def _block_text(text: str, escaped: bool) -> str:
    """Message text as the block was parsed: single-escaped text is decoded the same way (raw text if that fails),
    so check_precondition_checklist reads real lines."""
    if not escaped:
        return text
    try:
        decoded = json.loads('"' + text + '"')
    except (json.JSONDecodeError, RecursionError):
        return text
    return decoded if isinstance(decoded, str) else text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_dossier_from_transcript(path: str) -> Dict[str, Any]:
    """Returns the last <dossier_json> block the subagent model emitted, with its provenance.
    Truncated scanned rows are resolved from transcript_full.jsonl (read_agy_steps)."""
    steps, resolved = read_agy_steps(path)
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
                parsed, escaped = _parse_block_ex(raw)
                if parsed is not None:
                    found = (step, raw, parsed, _block_text(text, escaped))
                    break

    if not found:
        if saw_block:
            raise ProvenanceError(f"<dossier_json> block emitted by the subagent is not a valid JSON object ({path}).")
        raise ProvenanceError(f"No <dossier_json> block emitted by the subagent in {path}")

    step, raw, dossier, final_text = found

    return {
        "source": AGY_SOURCE,
        "raw": raw,
        "sha256": sha256_text(raw),
        "dossier": dossier,
        # Message carrying the block (resolved text for truncated rows), for check_precondition_checklist at
        # record time. Never stored in the record nor hashed.
        "final_text": final_text,
        "step_index": step.get("step_index"),
        "created_at_ts": _parse_created_at(step.get("created_at")),
        "conversation_id": os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path)))),
        "parent_conversation_id": parent_id,
        "transcript_path": os.path.abspath(path),
        "full_transcript_used": any(row is step for row in resolved),
        "resolved_steps": [row.get("step_index") for row in resolved],
    }


# =============================================================================
# Claude Code subagent transcripts
# =============================================================================
def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def claude_project_roots() -> list:
    """Directories that may contain Claude Code project folders (~/.claude/projects).
    CLAUDE_PROJECTS_DIRS (os.pathsep-separated) overrides the defaults. Under WSL the Windows
    profiles (/mnt/<drive>/Users/<user>/.claude/projects) are searched too, because Claude Code
    on Windows writes its transcripts there while the hooks and scripts run inside WSL."""
    override = os.environ.get(CLAUDE_PROJECTS_ENV)
    if override:
        return [p for p in override.split(os.pathsep) if p]
    roots = []
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        roots.append(os.path.join(os.path.expanduser(config_dir), "projects"))
    roots.append(os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    if _is_wsl():
        roots.extend(sorted(glob.glob("/mnt/*/Users/*/.claude/projects")))
    out = []
    for root in roots:
        if root not in out:
            out.append(root)
    return out


def _normalize_claude_agent_id(agent_id: str) -> str:
    agent_id = (agent_id or "").strip()
    if agent_id.startswith("agent-"):
        agent_id = agent_id[len("agent-"):]
    if not CLAUDE_AGENT_ID_RE.match(agent_id):
        raise ProvenanceError(f"Invalid Claude Code subagent id '{agent_id}' (expected 'a' followed by hex digits).")
    return agent_id


def find_claude_subagent_transcript(agent_id: str) -> str:
    """Resolves a Claude Code subagent id to <projects>/<slug>/<sessionId>/subagents/agent-<id>.jsonl."""
    agent_id = _normalize_claude_agent_id(agent_id)
    matches = []
    for root in claude_project_roots():
        pattern = os.path.join(glob.escape(root), "*", "*", "subagents", f"agent-{agent_id}.jsonl")
        for path in glob.glob(pattern):
            if os.path.isfile(path) and os.path.abspath(path) not in matches:
                matches.append(os.path.abspath(path))
    if not matches:
        raise ProvenanceError(
            f"No transcript found for Claude Code subagent '{agent_id}' in: {', '.join(claude_project_roots())}"
        )
    if len(matches) > 1:
        raise ProvenanceError(f"Claude Code subagent id '{agent_id}' is ambiguous ({len(matches)} transcripts).")
    return matches[0]


def claude_meta_path(transcript_path: str) -> str:
    base = transcript_path[:-len(".jsonl")] if transcript_path.endswith(".jsonl") else transcript_path
    return base + ".meta.json"


def read_claude_subagent(path: str, expected_agent_type: str) -> Tuple[dict, list]:
    """Validates a Claude Code subagent transcript and returns (info, assistant_rows).

    Checks: canonical location (.../<sessionId>/subagents/agent-<agentId>.jsonl), meta.json agentType
    equal to expected_agent_type, and every assistant row being a sidechain row of this agent and of
    the parent session named by the directory. info = {agent_id, agent_type, session_id, meta_path}."""
    path = os.path.abspath(path or "")
    m = CLAUDE_TRANSCRIPT_RE.match(os.path.basename(path))
    subagents_dir = os.path.dirname(path)
    if not m or os.path.basename(subagents_dir) != "subagents":
        raise ProvenanceError(f"Not a Claude Code subagent transcript path: {path}")
    agent_id = m.group(1)
    session_dir_id = os.path.basename(os.path.dirname(subagents_dir))
    if not os.path.isfile(path):
        raise ProvenanceError(f"Subagent transcript not found: {path}")

    meta_path = claude_meta_path(path)
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError, RecursionError) as e:
        raise ProvenanceError(f"Claude Code subagent metadata unreadable ({meta_path}): {type(e).__name__}: {e}")
    agent_type = meta.get("agentType") if isinstance(meta, dict) else None
    if agent_type != expected_agent_type:
        raise ProvenanceError(
            f"Claude Code subagent '{agent_id}' is of type '{agent_type}', expected '{expected_agent_type}'. "
            f"Launch it with the Agent tool and subagent_type '{expected_agent_type}'."
        )

    rows = _read_steps(path)
    if not rows:
        raise ProvenanceError(f"Transcript is empty or unreadable: {path}")
    assistant_rows = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        if row.get("isSidechain") is not True or row.get("agentId") != agent_id:
            raise ProvenanceError(f"Transcript row {idx} does not belong to subagent '{agent_id}' ({path}).")
        if row.get("sessionId") != session_dir_id:
            raise ProvenanceError(f"Transcript row {idx} belongs to another parent session ({path}).")
        assistant_rows.append((idx, row))
    info = {"agent_id": agent_id, "agent_type": agent_type, "session_id": session_dir_id, "meta_path": meta_path}
    return info, assistant_rows


def claude_assistant_texts(row: dict) -> list:
    """Text blocks the subagent model itself wrote in one assistant row (never tool results).
    Raises ProvenanceError when the message content is neither a string nor a list."""
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    if message.get("role", "assistant") != "assistant":
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if content is not None and not isinstance(content, list):
        raise ProvenanceError(f"Malformed assistant message content ({type(content).__name__}).")
    texts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


def extract_dossier_from_claude_transcript(path: str, expected_agent_type: str = EVALUATOR_NAME) -> Dict[str, Any]:
    """Returns the last <dossier_json> block the Claude Code evaluator subagent wrote, with its provenance."""
    info, assistant_rows = read_claude_subagent(path, expected_agent_type)

    found = None
    saw_block = False
    for idx, row in assistant_rows:
        for text in claude_assistant_texts(row):
            for m in DOSSIER_RE.finditer(text):
                saw_block = True
                raw = m.group(1).strip()
                parsed, escaped = _parse_block_ex(raw)
                if parsed is not None:
                    found = (idx, row, raw, parsed, _block_text(text, escaped))
                    break

    if not found:
        if saw_block:
            raise ProvenanceError(f"<dossier_json> block emitted by the subagent is not a valid JSON object ({path}).")
        raise ProvenanceError(f"No <dossier_json> block emitted by the subagent in {path}")

    idx, row, raw, dossier, final_text = found
    return {
        "source": CLAUDE_SOURCE,
        "raw": raw,
        "sha256": sha256_text(raw),
        "dossier": dossier,
        "final_text": final_text,  # see extract_dossier_from_transcript; never stored nor hashed
        "step_index": idx,
        "step_uuid": row.get("uuid"),
        "created_at_ts": _parse_created_at(row.get("timestamp")),
        "conversation_id": info["agent_id"],
        "parent_conversation_id": info["session_id"],
        "agent_type": info["agent_type"],
        "transcript_path": os.path.abspath(path),
    }


def extract_recorded_transcript(record: dict) -> Dict[str, Any]:
    """Re-extracts the dossier from the transcript referenced by a recorded dossier's provenance."""
    prov = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    source = prov.get("source")
    path = prov.get("transcript_path")
    if source == CLAUDE_SOURCE:
        return extract_dossier_from_claude_transcript(path, EVALUATOR_NAME)
    if source == AGY_SOURCE:
        return extract_dossier_from_transcript(path)
    raise ProvenanceError("Dossier has no subagent provenance (it was not recorded from a subagent transcript).")


def normalize_status(status: Any) -> Tuple[str, bool]:
    """Maps the evaluator verdict to APPROVED/REJECTED/NEUTRAL.
    Returns (status, pending_confirmation). Unknown values fail closed to REJECTED."""
    s = str(status or "").strip().upper()
    if s in VALID_STATUSES:
        return s, False
    if s.startswith("APPROVED"):  # e.g. APPROVED_PENDING_CONFIRMATION
        return "APPROVED", "PENDING" in s or "CONFIRM" in s
    return "REJECTED", False


# =============================================================================
# Precondition Checklist vs dossier (issue #27, checked by record_evaluation.py at record time only)
# =============================================================================
CHECKLIST_HEADING_RE = re.compile(r"^\s*##\s+Precondition Checklist\s*$")
CHECKLIST_END_RE = re.compile(r"^\s*#{1,2}\s|<dossier_json>")
C42_LINE_RE = re.compile(r"C4\.2 Overall status:.*->\s*(\S+)")
CANDIDATE_CHECK_RE = re.compile(
    r"^\s*-\s*\[([ xX])\]\s+(\S+)\s+(\S+)(?:\s+\(YOLO\))?\s+(K1|K2|K3|K4|C3\.1)(?=\s|$)")
APPROVED_CANDIDATE_CHECKS = ("K1", "K2", "K3", "K4", "C3.1")


def check_precondition_checklist(text: str, dossier: dict) -> list:
    """Consistency of the evaluator's visible '## Precondition Checklist' with its <dossier_json> (the format
    of .agents/agents/isolated_market_evaluator/agent.md). Returns human-readable problems; [] = consistent.
    Region: from the heading to the next '# '/'## ' heading, the <dossier_json> block or the end of the text.
    Rules: exactly one 'C4.2 Overall status: ... -> <STATUS>' line whose status equals the dossier status (both
    normalized with normalize_status); every approved candidate has '[x]' K1, K2, K3, K4 and C3.1 lines prefixed
    exactly '<SYMBOL> <DIRECTION>' (optionally '(YOLO)'). Lines of other candidates and other ids are ignored."""
    lines = str(text or "").splitlines()
    start = next((i for i, line in enumerate(lines) if CHECKLIST_HEADING_RE.match(line)), None)
    if start is None:
        return ["missing ## Precondition Checklist"]
    region = []
    for line in lines[start + 1:]:
        if CHECKLIST_END_RE.search(line):
            break
        region.append(line)

    problems = []
    dossier = dossier if isinstance(dossier, dict) else {}
    status = normalize_status(dossier.get("status"))[0]
    c42 = [m for m in (C42_LINE_RE.search(line) for line in region) if m]
    if len(c42) != 1:
        problems.append(f"expected exactly one 'C4.2 Overall status' line in the checklist, found {len(c42)}")
    else:
        token = c42[0].group(1).strip(".,;:!?()[]*`'\"")
        c42_status = normalize_status(token)[0]
        if c42_status != status:
            problems.append(f"C4.2 Overall status '{token}' does not match the dossier status '{status}'")

    marks = {}
    for line in region:
        m = CANDIDATE_CHECK_RE.match(line)
        if m:
            key = (m.group(2).upper(), m.group(3).upper(), m.group(4))
            marks.setdefault(key, []).append(m.group(1).lower() == "x")
    candidates = dossier.get("approved_candidates")
    for cand in candidates if isinstance(candidates, list) else []:
        if not isinstance(cand, dict):
            continue
        symbol = str(cand.get("symbol") or "").strip().upper()
        direction = str(cand.get("direction") or "").strip().upper()
        if not symbol:
            continue
        for check in APPROVED_CANDIDATE_CHECKS:
            seen = marks.get((symbol, direction, check))
            if not seen:
                problems.append(f"approved {symbol} {direction}: no {check} line in the checklist")
            elif not all(seen):
                problems.append(f"approved {symbol} {direction}: {check} is not checked [x]")
    return problems


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
        # Heuristic radar score (issue #202); `conviction_pct` is the legacy alias. Never a reason to reject.
        c["score"] = normalize_score(cand.get("score") if cand.get("score") is not None else cand.get("conviction_pct"))
        out.append(c)
    return out


def normalize_score(raw: Any) -> Optional[int]:
    """Integer score in 0-100, else None (non-numeric, boolean, non-finite or out of range)."""
    if isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val < 0 or val > 100:
        return None
    return int(round(val))


def build_record_from_extraction(extracted: Dict[str, Any], recorded_at_ts: Optional[int] = None) -> Dict[str, Any]:
    dossier = extracted["dossier"]
    status, pending = normalize_status(dossier.get("status"))
    candidates = normalize_candidates(dossier, pending) if status == "APPROVED" else []
    evaluated_at = extracted.get("created_at_ts") or 0
    recorded_at = int(recorded_at_ts or time.time())
    source = extracted.get("source") or AGY_SOURCE
    provenance = {
        "source": source,
        "transcript_path": extracted["transcript_path"],
        "step_index": extracted.get("step_index"),
        "sha256": extracted["sha256"],
    }
    if source == CLAUDE_SOURCE:
        provenance["step_uuid"] = extracted.get("step_uuid")
        provenance["agent_type"] = extracted.get("agent_type")
    else:
        # Whether the dossier step was resolved from transcript_full.jsonl, and every resolved step
        provenance["full_transcript_used"] = bool(extracted.get("full_transcript_used"))
        provenance["resolved_steps"] = list(extracted.get("resolved_steps") or [])
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
        "provenance": provenance,
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
    if not isinstance(prov, dict) or prov.get("source") not in SUBAGENT_SOURCES:
        return False, (
            "Dossier has no subagent provenance (it was not recorded with --from-subagent / --from-claude-subagent)."
        ), None
    source = prov["source"]

    path = prov.get("transcript_path")
    if not path or not os.path.isfile(path):
        return False, f"Subagent transcript not found: {path}", None
    if source == AGY_SOURCE:
        transcript_conv = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
    else:
        m = CLAUDE_TRANSCRIPT_RE.match(os.path.basename(path))
        transcript_conv = m.group(1) if m else None
    if transcript_conv != record.get("conversation_id"):
        return False, "Dossier conversation_id does not match its transcript path.", None

    try:
        extracted = extract_recorded_transcript(record)
    except ProvenanceError as e:
        return False, str(e), None

    parent = extracted.get("parent_conversation_id")
    if not parent or parent == extracted["conversation_id"]:
        return False, "Transcript is not a subagent conversation (no parent sender).", None
    if extracted["sha256"] != prov.get("sha256") or extracted["step_index"] != prov.get("step_index"):
        return False, "Dossier hash does not match the latest <dossier_json> emitted by the evaluator subagent.", None
    if source == CLAUDE_SOURCE and extracted.get("step_uuid") != prov.get("step_uuid"):
        return False, "Dossier hash does not match the latest <dossier_json> emitted by the evaluator subagent.", None
    # sha256 + step_index already pin the block; this also pins how it was read (transcript.jsonl vs the
    # verified transcript_full.jsonl row). resolved_steps is informational: later rows may legitimately add to it.
    if source == AGY_SOURCE and bool(extracted.get("full_transcript_used")) != bool(prov.get("full_transcript_used")):
        return False, "Dossier was recorded from a different transcript source (transcript_full.jsonl resolution changed).", None

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
            "`record_evaluation.py --from-subagent <conversationId>` (agy) or "
            "`record_evaluation.py --from-claude-subagent <agentId>` (Claude Code)."
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
            return False, ("Legacy dossier format is not accepted in PROD. Record it with --from-subagent "
                           "(agy) or --from-claude-subagent (Claude Code)."), None
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

    if cand is not None:
        # Audit metadata on a copy (issue #202): the stored/rebuilt record is never mutated
        prov = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        cand = dict(cand, dossier_sha256=prov.get("sha256"))
    return True, "Dossier valid and approved.", cand
