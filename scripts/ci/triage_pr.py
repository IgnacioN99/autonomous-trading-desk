#!/usr/bin/env python3
"""
scripts/ci/triage_pr.py
Deterministic PR Triage & Dispatcher for Antigravity PR Reviewer.

Extracts modified files in the current branch against target (origin/main),
and deterministically maps them to required specialized reviewer agents.
Enforces FAIL-CLOSED policy: if unknown files or core rule files (AGENTS.md)
are modified, all 4 specialized reviewers are triggered.
"""

import os
import sys
import json
import re
import subprocess
from pathlib import Path

# Specialized Reviewer Domains
REVIEWERS = {
    "trading_risk": {
        "name": "Trading Risk & Quantitative Math Specialist",
        "doc": ".agents/reviewers/trading_risk_reviewer.md",
        "patterns": [
            r"^scripts/execute_futures_trade\.py$",
            r"^scripts/loops/.*\.py$",
            r"^scripts/trading_doctor\.py$",
            r"^scripts/.*risk.*\.py$",
            r"^scripts/.*volatility.*\.py$",
            r"^scripts/.*kelly.*\.py$",
        ],
    },
    "binance_microstructure": {
        "name": "Binance Microstructure & Crypto Execution Specialist",
        "doc": ".agents/reviewers/binance_microstructure_reviewer.md",
        "patterns": [
            r"^scripts/execute_futures_trade\.py$",
            r"^scripts/sync_session_state\.py$",
            r"^scripts/loops/.*\.py$",
            r"^scripts/.*binance.*\.py$",
            r"^scripts/.*order.*\.py$",
            r"^scripts/.*doctor.*\.py$",
        ],
    },
    "agentic_harness": {
        "name": "Agentic Harness & Fail-Closed Architecture Specialist",
        "doc": ".agents/reviewers/agentic_harness_reviewer.md",
        "patterns": [
            r"^\.agents/.*",
            r"^hooks/.*",
            r"^scripts/trading_doctor\.py$",
            r"^scripts/sync_session_state\.py$",
            r"^scripts/prime_evaluator_brief\.py$",
            r"^scripts/report_issue\.sh$",
            r"^scripts/.*hook.*",
            r"^scripts/.*guard.*",
        ],
    },
    "prompt_engineering": {
        "name": "Prompt Engineering & LLM Alignment Specialist",
        "doc": ".agents/reviewers/prompt_engineering_reviewer.md",
        "patterns": [
            r"^docs/.*prompt.*\.md$",
            r"^docs/.*",
            r"^scripts/.*evaluator.*\.py$",
            r"^scripts/prime_evaluator_brief\.py$",
            r"^prompts/.*",
        ],
    },
}

# Files that automatically trigger ALL reviewers (Fail-Closed Core Files)
CORE_OMNIBUS_FILES = [
    r"^AGENTS\.md$",
    r"^\.github/workflows/.*",
    r"^requirements\.txt$",
    r"^pyproject\.toml$",
]


def get_git_diff_files(base_ref: str = "origin/main") -> list[str]:
    """Retrieve list of modified and added files compared to base branch or working tree."""
    if base_ref == "--working-tree":
        cmd_status = ["git", "status", "--porcelain"]
        status_res = subprocess.run(cmd_status, capture_output=True, text=True)
        files = []
        for line in status_res.stdout.splitlines():
            line = line.strip()
            if line:
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    files.append(parts[1].replace("\\", "/"))
        return files

    # First check if base_ref exists in git
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", base_ref],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        base_ref = "main" if os.system("git rev-parse --verify main >/dev/null 2>&1") == 0 else "HEAD~1"

    cmd = ["git", "diff", "--name-only", f"{base_ref}...HEAD"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    files = [f.strip().replace("\\", "/") for f in res.stdout.splitlines() if f.strip()]

    # If branch diff is empty, check uncommitted working tree changes (useful for local runs)
    if not files:
        cmd_status = ["git", "status", "--porcelain"]
        status_res = subprocess.run(cmd_status, capture_output=True, text=True)
        for line in status_res.stdout.splitlines():
            line = line.strip()
            if line:
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    files.append(parts[1].replace("\\", "/"))

    return files


def triage(files: list[str]) -> dict:
    """Classifies files deterministically into required reviewer domains."""
    if not files:
        return {
            "changed_files": [],
            "required_reviewers": [],
            "reasons": {"info": "No files changed"},
            "fail_closed_triggered": False,
        }

    required_reviewers = set()
    reasons = {}
    fail_closed = False

    # Check omnibus core files first
    for f in files:
        for omni_pat in CORE_OMNIBUS_FILES:
            if re.match(omni_pat, f, re.IGNORECASE):
                fail_closed = True
                reasons[f] = f"Matched omnibus core rule '{omni_pat}'. Triggering all 4 reviewers."
                for rev_key in REVIEWERS:
                    required_reviewers.add(rev_key)
                break
        if fail_closed:
            break

    # If not triggered omnibus, match pattern by pattern
    unclassified_files = []
    if not fail_closed:
        for f in files:
            matched_any = False
            for rev_key, conf in REVIEWERS.items():
                for pat in conf["patterns"]:
                    if re.match(pat, f):
                        required_reviewers.add(rev_key)
                        reasons.setdefault(rev_key, []).append(f)
                        matched_any = True
                        break
            if not matched_any:
                unclassified_files.append(f)

        # Fail-closed safety: If an unclassified file is touched, activate ALL reviewers
        if unclassified_files:
            fail_closed = True
            for rev_key in REVIEWERS:
                required_reviewers.add(rev_key)
            reasons["unclassified_safety"] = (
                f"Unclassified files detected: {unclassified_files}. "
                "Fail-closed policy activated: triggering all 4 reviewers."
            )

    sorted_reviewers = sorted(list(required_reviewers))

    manifest = {
        "changed_files": files,
        "required_reviewers": sorted_reviewers,
        "reviewer_details": {
            k: {
                "name": REVIEWERS[k]["name"],
                "doc": REVIEWERS[k]["doc"],
            }
            for k in sorted_reviewers
        },
        "reasons": reasons,
        "fail_closed_triggered": fail_closed,
    }

    return manifest


def main():
    base_ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    files = get_git_diff_files(base_ref)
    manifest = triage(files)

    os.makedirs("logs", exist_ok=True)
    out_path = Path("logs/pr_manifest.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"=== PR Triage Completed ({len(files)} files changed) ===")
    print(f"Fail-Closed Triggered: {manifest['fail_closed_triggered']}")
    print(f"Required Reviewers ({len(manifest['required_reviewers'])}):")
    for rev in manifest["required_reviewers"]:
        print(f"  - [{rev}] {REVIEWERS[rev]['name']}")
    print(f"Manifest written to: {out_path}")


if __name__ == "__main__":
    main()
