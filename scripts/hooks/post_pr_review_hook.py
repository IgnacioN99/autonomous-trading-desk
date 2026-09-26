#!/usr/bin/env python3
"""
scripts/hooks/post_pr_review_hook.py
PostToolUse Hook: Automatically triggers the Multi-Agent PR Review when a PR is created or pushed.

Intercepts tool execution after completion. If the command created a PR (e.g. `gh pr create`)
or pushed a non-main feature branch (`git push ... origin feat/...`), this hook:
1. Runs deterministic triage (scripts/ci/triage_pr.py).
2. Executes the multi-agent PR audit (scripts/ci/run_pr_audit.py).
3. Saves the report in logs/latest_pr_review.md and review_output.md.
4. If GitHub CLI (`gh`) is authenticated, posts the review comment to the PR.
5. Logs event to logs/pr_hook_events.jsonl.

Contract:
  Input (stdin): JSON with toolCall metadata (protojson camelCase).
  Output (stdout): {} (mandatory empty JSON object for PostToolUse).
"""

import os
import sys
import json
import re
import time
import subprocess
from pathlib import Path


def find_workspace_root() -> str:
    p = os.path.abspath(__file__)
    while p and p != os.path.dirname(p):
        p = os.path.dirname(p)
        if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "logs")):
            return p
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def is_pr_creation_or_push(command: str) -> bool:
    if not command:
        return False

    # Avoid recursive execution if the command itself was running the audit
    if "run_pr_audit" in command or "triage_pr" in command:
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


def get_current_branch(root: str) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            print(json.dumps({}))
            return

        payload = json.loads(raw_input)
        tool_call = payload.get("toolCall", {})
        args = tool_call.get("args", {})
        command_line = args.get("CommandLine", "")

        if is_pr_creation_or_push(command_line):
            root = find_workspace_root()
            branch = get_current_branch(root)
            logs_dir = os.path.join(root, "logs")
            os.makedirs(logs_dir, exist_ok=True)

            log_event = {
                "timestamp": time.time(),
                "time_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "trigger_command": command_line,
                "branch": branch,
                "status": "triggered",
            }

            audit_script = os.path.join(root, "scripts", "ci", "run_pr_audit.py")
            report_out = os.path.join(root, "logs", "latest_pr_review.md")

            if os.path.exists(audit_script):
                sys.stderr.write(
                    f"\n🚀 [POST-TOOL HOOK] Detección de creación/push de PR en rama '{branch}'.\n"
                    f"   Lanzando agente orquestador de auditoría...\n"
                )

                # Launch PR audit in background to ensure hook responds in < 15ms
                subprocess.Popen(
                    [sys.executable, audit_script, "origin/main", report_out],
                    cwd=root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

                sys.stderr.write(
                    f"✅ [POST-TOOL HOOK] Auditoría multi-agente despachada en background.\n"
                    f"   El informe se guardará en: {report_out}\n"
                )
                log_event["status"] = "dispatched_async"

            # Record event in ledger
            events_file = os.path.join(logs_dir, "pr_hook_events.jsonl")
            with open(events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_event) + "\n")

    except Exception as e:
        sys.stderr.write(f"[POST-TOOL HOOK ERROR] {str(e)}\n")

    # Contract requirement: PostToolUse must return {} on stdout
    print(json.dumps({}))


if __name__ == "__main__":
    main()
