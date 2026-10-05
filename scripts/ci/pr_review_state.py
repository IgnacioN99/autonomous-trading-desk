#!/usr/bin/env python3
"""
scripts/ci/pr_review_state.py
Pending-review marker shared by the PR review hooks (agy .agents/hooks.json and Claude Code
.claude/settings.json) and the /pr-review skill. conversation_id holds the agy conversationId or
the Claude Code session_id of the session that created/pushed the PR.

Lifecycle (logs/pr_review_state.json, gitignored):
  pending      post_pr_review_hook.py (PostToolUse) saw a successful `gh pr create` or a feature-branch push.
  in_progress  the /pr-review skill started (`start`); the reviewer subagents are running.
  done         the report was posted (`gh pr comment ... --body-file .../pr_review/report.md`, detected by the
               PostToolUse hook) or the agent closed it (`done --reason no_pr|declined|...`).
  abandoned    the Stop hook gave up after MAX_STOP_ATTEMPTS continuations (never loops forever).

pr_review_stop_hook.py (Stop) keeps the session going while the review is pending.

CLI (run from the repo root):
  python3 scripts/ci/pr_review_state.py status
  python3 scripts/ci/pr_review_state.py start
  python3 scripts/ci/pr_review_state.py done --reason posted|no_pr|declined|manual

PR_REVIEW_STATE_FILE overrides the state file location (tests, sandboxes).
"""

import os
import sys
import json
import time
import argparse

STATE_ENV = "PR_REVIEW_STATE_FILE"
MAX_STOP_ATTEMPTS = 3
IN_PROGRESS_GRACE_S = 1800  # reviewers running: let the parent idle until they report back
ACTIVE_STATUSES = ("pending", "in_progress")


def workspace_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def state_path() -> str:
    override = os.environ.get(STATE_ENV)
    if override:
        return override
    return os.path.join(workspace_root(), "logs", "pr_review_state.json")


def events_path(path: str | None = None) -> str:
    return os.path.join(os.path.dirname(path or state_path()), "pr_hook_events.jsonl")


def load_state(path: str | None = None) -> dict:
    path = path or state_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str | None = None) -> None:
    path = path or state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def log_event(event: dict, path: str | None = None) -> None:
    try:
        event = dict(event, timestamp=time.time(), time_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        with open(events_path(path), "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def is_active(state: dict) -> bool:
    return state.get("status") in ACTIVE_STATUSES


def mark_pending(trigger_command: str, branch: str, head_sha: str, conversation_id: str,
                 path: str | None = None, now: float | None = None) -> dict:
    state = {
        "status": "pending",
        "source": "hook",
        "branch": branch,
        "head_sha": head_sha,
        "conversation_id": conversation_id or "",
        "trigger_command": trigger_command[:300],
        "created_ts": now or time.time(),
        "stop_attempts": 0,
    }
    save_state(state, path)
    return state


def mark_started(path: str | None = None, now: float | None = None) -> dict | None:
    state = load_state(path)
    if not is_active(state):
        return None
    state.update(status="in_progress", started_ts=now or time.time())
    save_state(state, path)
    return state


def mark_done(reason: str, path: str | None = None, now: float | None = None) -> dict | None:
    state = load_state(path)
    if not is_active(state):
        return None
    state.update(status="done", done_reason=reason, done_ts=now or time.time())
    save_state(state, path)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Pending PR review marker (agy /pr-review auto-review).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Print the current marker.")
    sub.add_parser("start", help="Mark the pending review as in progress (reviewers invoked).")
    done = sub.add_parser("done", help="Close the pending review.")
    done.add_argument("--reason", default="manual", help="posted | no_pr | declined | manual")
    args = parser.parse_args()

    if args.cmd == "status":
        print(json.dumps(load_state() or {"status": "none"}, indent=2))
        return 0
    if args.cmd == "start":
        state = mark_started()
        print("Auto-review marked in progress." if state else "No pending auto-review (manual run): nothing to mark.")
        return 0
    state = mark_done(args.reason)
    if state:
        log_event({"event": "review_closed", "reason": args.reason, "branch": state.get("branch", "")})
        print(f"Auto-review closed ({args.reason}).")
    else:
        print("No pending auto-review: nothing to close.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
