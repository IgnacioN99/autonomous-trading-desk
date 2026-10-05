#!/usr/bin/env python3
"""
scripts/hooks/post_pr_review_hook.py
PostToolUse Hook (agy "pr-review-trigger" / Claude Code .claude/settings.json): arms the in-session
multi-agent PR review.

This hook only keeps the pending-review marker (scripts/ci/pr_review_state.py,
logs/pr_review_state.json) up to date:
  * successful `gh pr create` or `git push ... origin <feature-branch>` -> marker "pending"
    (the Stop hook pr_review_stop_hook.py then makes the agent run the /pr-review skill, which
    launches the reviewer subagents natively in the same session: agy invoke_subagent, Claude Code
    Agent tool);
  * successful `gh pr comment ... --body-file .../pr_review/report.md` -> marker "done".
Failed commands change nothing. Events are logged to logs/pr_hook_events.jsonl.

Contract:
  Input (stdin): agy {toolCall:{name:"run_command", args:{CommandLine}}, conversationId, error?} or
                 Claude Code {tool_name:"Bash", tool_input:{command}, tool_response, session_id}
                 (Claude Code only fires PostToolUse on success; failed calls go to PostToolUseFailure).
  Output (stdout): {} (empty JSON object, valid for both runtimes), always with exit code 0.
"""

import os
import re
import sys
import json
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from scripts.ci import pr_review_state as state_mod
except Exception as _import_error:  # pragma: no cover - the hook must still answer {} and exit 0
    state_mod = None
    sys.stderr.write(f"[PR REVIEW HOOK ERROR] cannot import pr_review_state: {_import_error}\n")

# Commands issued by the review flow itself never re-arm the review
REVIEW_FLOW_MARKERS = ("run_pr_audit", "triage_pr", "assemble_review", "verify_review", "pr_review_state")


def is_pr_creation_or_push(command: str) -> bool:
    if not command:
        return False

    # Avoid recursive execution if the command itself was running the review flow
    if any(marker in command for marker in REVIEW_FLOW_MARKERS):
        return False

    # Case 1: Direct GitHub CLI PR creation
    if re.search(r"\bgh\s+pr\s+create\b", command):
        return True

    # Case 2: Git push of a feature/fix branch to origin
    # Matches: git push origin feat/..., git push -u origin feat/..., etc.
    # Excludes pushes to main/master
    push_match = re.search(
        r"\bgit\s+push\b.*?\borigin\s+(?:--set-upstream\s+|-u\s+)?([a-zA-Z0-9_\-\/]+)",
        command,
    )
    if push_match:
        branch = push_match.group(1).strip()
        if branch not in ("main", "master", "HEAD"):
            return True

    return False


def is_review_post(command: str) -> bool:
    """`gh pr comment <n> --body-file <...>/pr_review/report.md` (the /pr-review publication step)."""
    if not command or not re.search(r"\bgh\s+pr\s+comment\b", command):
        return False
    return bool(re.search(r"--body-file[=\s]+['\"]?\S*pr_review/report\.md", command))


def _git(args: list[str]) -> str:
    try:
        res = subprocess.run(["git"] + args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=5)
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def is_claude_payload(payload: dict) -> bool:
    return "toolCall" not in payload and ("tool_name" in payload or "hook_event_name" in payload)


def extract_command(payload: dict) -> str:
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    command_line = args.get("CommandLine", "")
    if not command_line and isinstance(payload.get("tool_input"), dict):
        # Claude Code PostToolUse payload: only the Bash tool runs shell commands
        if payload.get("tool_name", "Bash") != "Bash":
            return ""
        command_line = payload["tool_input"].get("command", "")
    return command_line if isinstance(command_line, str) else ""


def conversation_of(payload: dict) -> str:
    """agy conversationId or Claude Code session_id (the Stop hook compares against the same field)."""
    return str(payload.get("conversationId") or payload.get("session_id") or "")


def tool_failed(payload: dict) -> bool:
    if payload.get("error"):
        return True
    if payload.get("hook_event_name") == "PostToolUseFailure":
        return True
    response = payload.get("tool_response")
    if isinstance(response, dict):
        if response.get("is_error") or response.get("interrupted"):
            return True
        for key in ("exit_code", "exitCode", "returncode"):
            code = response.get(key)
            if isinstance(code, int) and code != 0:
                return True
    return False


def handle_post_tool_use(payload: dict, path: str | None = None) -> str:
    """Updates the marker for one PostToolUse payload. Returns the action taken (for tests/logs)."""
    command_line = extract_command(payload)
    if not command_line or tool_failed(payload):
        return "ignored"

    if is_review_post(command_line):
        if state_mod.mark_done("posted", path=path):
            state_mod.log_event({"event": "review_posted", "trigger_command": command_line[:300]}, path)
            sys.stderr.write("[PR REVIEW HOOK] Review comment posted: pending auto-review closed.\n")
            return "done"
        return "ignored"

    if is_pr_creation_or_push(command_line):
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
        state = state_mod.mark_pending(
            trigger_command=command_line,
            branch=branch,
            head_sha=_git(["rev-parse", "HEAD"]),
            conversation_id=conversation_of(payload),
            path=path,
        )
        state_mod.log_event({"event": "review_pending", "trigger_command": command_line[:300],
                             "branch": branch, "conversation_id": state["conversation_id"]}, path)
        sys.stderr.write(
            f"[PR REVIEW HOOK] PR creation/push detected on branch '{branch}': multi-agent review pending.\n"
            "   It runs in this session via the /pr-review skill (reviewer subagents) when the turn ends.\n"
        )
        return "pending"

    return "ignored"


def main():
    try:
        raw_input = sys.stdin.read()
        payload = json.loads(raw_input) if raw_input.strip() else {}
        if isinstance(payload, dict) and state_mod is not None:
            handle_post_tool_use(payload)
    except Exception as e:
        sys.stderr.write(f"[PR REVIEW HOOK ERROR] {e}\n")

    # Contract requirement: PostToolUse must return {} on stdout
    print(json.dumps({}))


if __name__ == "__main__":
    main()
