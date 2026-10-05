#!/usr/bin/env python3
"""
scripts/hooks/pr_review_stop_hook.py
Stop Hook (agy "pr-review-trigger" / Claude Code .claude/settings.json): runs the multi-agent PR
review in the SAME session.

Runtime contracts (selected with --claude, or auto-detected from a Claude Code payload):
  * agy: stdout {"decision": "continue", "reason": "..."} blocks the stop and re-enters the agent loop
    with `reason` injected as a system message; {"decision": "stop"} lets the agent stop.
  * Claude Code: stdout {"decision": "block", "reason": "..."} keeps Claude working with `reason` as
    context; no output (exit 0) lets it stop. Input: {session_id, hook_event_name: "Stop",
    stop_hook_active, ...}.

When post_pr_review_hook.py armed a pending review (logs/pr_review_state.json), this hook asks the
agent to run the pr-review skill (agy: .agents/skills/pr-review/SKILL.md with invoke_subagent;
Claude Code: .claude/skills/pr-review/SKILL.md with the Agent tool), which posts the consolidated report.

Loop safety (never blocks forever):
  * no marker, or marker done/abandoned            -> stop allowed;
  * a different conversation/session (e.g. a reviewer subagent finishing) -> stop allowed;
  * terminationReason "error" or background tasks still running (fullyIdle false) -> stop allowed
    (subagents wake the parent when they finish; there is no need to poll);
  * Claude Code stop_hook_active (this stop already follows a blocked stop) -> stop allowed;
  * review in progress (skill started) younger than IN_PROGRESS_GRACE_S -> stop allowed;
  * at most MAX_STOP_ATTEMPTS continuations per marker, then it is marked "abandoned".
Always exits 0 (agy: always prints valid JSON).
"""

import os
import sys
import json
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from scripts.ci import pr_review_state as state_mod
except Exception as _import_error:  # pragma: no cover - the hook must still answer and exit 0
    state_mod = None
    sys.stderr.write(f"[PR REVIEW STOP HOOK ERROR] cannot import pr_review_state: {_import_error}\n")

ALLOW_STOP = {"decision": "stop"}


def is_claude_payload(payload: dict) -> bool:
    return "conversationId" not in payload and ("hook_event_name" in payload or "session_id" in payload)


def build_reason(state: dict, attempt: int, max_attempts: int, runtime: str = "agy") -> str:
    head = (
        f"[pr-review auto-review {attempt}/{max_attempts}] A PR was created or pushed in this session "
        f"(branch '{state.get('branch', '')}', trigger: `{state.get('trigger_command', '')[:120]}`) and its "
        "multi-agent review has not been posted yet. "
    )
    if runtime == "claude":
        how = (
            "Run the pr-review skill now (.claude/skills/pr-review/SKILL.md) in AUTO mode: triage, launch every "
            "required reviewer with the Agent tool in ONE message (subagent_type <id>_reviewer), assemble with "
            "`python3 scripts/ci/assemble_review.py --from-claude-subagent <id>=<agentId> ...`, verify, then post "
            "with `gh pr comment <n> --body-file logs/pr_review/report.md` without asking. If the reviewers are "
            "already running in the background, end your turn and wait for their completion (do not relaunch them). "
        )
    else:
        how = (
            "Run the /pr-review skill now (.agents/skills/pr-review/SKILL.md) in AUTO mode: triage, one "
            "invoke_subagent call with every required reviewer, assemble, verify, then post with `gh pr comment "
            "<n> --body-file logs/pr_review/report.md` without asking. If the reviewers are already running, end "
            "your turn and wait for their messages (do not re-invoke them). "
        )
    tail = (
        "If there is no open PR for this branch run `python3 scripts/ci/pr_review_state.py done --reason no_pr`; "
        "if the user declined the review run `python3 scripts/ci/pr_review_state.py done --reason declined`. "
        "Never place orders during the review."
    )
    return head + how + tail


def decide(payload: dict, path: str | None = None, now: float | None = None, runtime: str | None = None) -> dict:
    """Pure decision for one Stop payload (updates the marker when it continues or gives up).
    Returns the agy-style decision ({"decision": "stop"} or {"decision": "continue", "reason"});
    format_output() converts it for Claude Code."""
    if state_mod is None:
        return dict(ALLOW_STOP)
    runtime = runtime or ("claude" if is_claude_payload(payload) else "agy")
    now = now or time.time()
    state = state_mod.load_state(path)
    if not state_mod.is_active(state):
        return dict(ALLOW_STOP)

    marker_conv = state.get("conversation_id") or ""
    payload_conv = str(payload.get("conversationId") or payload.get("session_id") or "")
    if marker_conv and payload_conv and marker_conv != payload_conv:
        return dict(ALLOW_STOP)  # another conversation, e.g. a reviewer subagent finishing its turn

    if payload.get("terminationReason") == "error" or payload.get("error"):
        return dict(ALLOW_STOP)
    if payload.get("fullyIdle") is False:
        return dict(ALLOW_STOP)  # background tasks / subagents still running; they wake the parent
    if payload.get("hook_event_name") == "SubagentStop" or payload.get("agent_id"):
        return dict(ALLOW_STOP)  # Claude Code subagent finishing (only the main agent runs the review)
    if payload.get("stop_hook_active") is True:
        return dict(ALLOW_STOP)  # Claude Code: this stop already follows a blocked stop; never chain blocks

    if state.get("status") == "in_progress":
        started = float(state.get("started_ts") or 0)
        if now - started < state_mod.IN_PROGRESS_GRACE_S:
            return dict(ALLOW_STOP)

    attempts = int(state.get("stop_attempts") or 0)
    if attempts >= state_mod.MAX_STOP_ATTEMPTS:
        state.update(status="abandoned", abandoned_ts=now)
        state_mod.save_state(state, path)
        state_mod.log_event({"event": "review_abandoned", "branch": state.get("branch", ""),
                             "stop_attempts": attempts}, path)
        sys.stderr.write(
            "[PR REVIEW STOP HOOK] Auto-review abandoned after "
            f"{attempts} attempts; run /pr-review manually.\n"
        )
        return dict(ALLOW_STOP)

    attempts += 1
    state.update(stop_attempts=attempts, last_stop_ts=now)
    state_mod.save_state(state, path)
    state_mod.log_event({"event": "review_continue", "branch": state.get("branch", ""),
                         "stop_attempts": attempts}, path)
    return {"decision": "continue",
            "reason": build_reason(state, attempts, state_mod.MAX_STOP_ATTEMPTS, runtime)}


def format_output(result: dict, runtime: str) -> str:
    """Serializes a decide() result in the runtime's Stop contract ('' = print nothing)."""
    if runtime == "claude":
        if result.get("decision") == "continue":
            return json.dumps({"decision": "block", "reason": result.get("reason", "")})
        return ""
    return json.dumps(result)


def main(argv: list | None = None):
    argv = sys.argv[1:] if argv is None else argv
    runtime = "claude" if "--claude" in argv else None
    result = dict(ALLOW_STOP)
    try:
        raw_input = sys.stdin.read()
        payload = json.loads(raw_input) if raw_input.strip() else {}
        if isinstance(payload, dict):
            runtime = runtime or ("claude" if is_claude_payload(payload) else "agy")
            result = decide(payload, runtime=runtime)
    except Exception as e:
        sys.stderr.write(f"[PR REVIEW STOP HOOK ERROR] {e}\n")
        result = dict(ALLOW_STOP)
    out = format_output(result, runtime or "agy")
    if out:
        print(out)


if __name__ == "__main__":
    main()
