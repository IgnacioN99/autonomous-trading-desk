#!/usr/bin/env python3
"""
scripts/ci/verify_review.py
Deterministic Mechanical Gate for the Antigravity PR review.

Enforces zero-hallucination and complete reviewer coverage:
1. Checks that every required reviewer from logs/pr_manifest.json has executed
   and signed its verdict in the review report ("### Verdict: <reviewer>").
2. Checks the status of each verdict. If any reviewer requested changes
   ([CHANGES REQUIRED]), the PR is blocked.
3. Passes only if all required reviewers are present and approved.

Exit codes (main):
  0  every required reviewer APPROVED
  1  review complete, but at least one reviewer requested changes
  2  review incomplete: missing/inconclusive reviewers or unreadable inputs
     (re-invoke only the reviewers listed as missing)
"""

import sys
import json
import re
from pathlib import Path

APPROVED = "APPROVED"
CHANGES_REQUIRED = "CHANGES REQUIRED"

EXIT_APPROVED = 0
EXIT_CHANGES_REQUIRED = 1
EXIT_INCOMPLETE = 2

_APPROVED_RE = re.compile(r"\[\s*APPROVED\s*\]", re.IGNORECASE)
_CHANGES_RE = re.compile(r"\[\s*CHANGES\s+REQUIRED\s*\]", re.IGNORECASE)
_STATUS_LINE_RE = re.compile(r"\*\*\s*Status\s*:?\s*\*\*\s*:?(.*)", re.IGNORECASE)
_NEXT_SECTION_RE = re.compile(r"^#{1,3}\s", re.MULTILINE)


def verdict_header_re(reviewer: str) -> re.Pattern:
    return re.compile(rf"^###\s*Verdict:\s*`?{re.escape(reviewer)}(?!\w)[^\n]*$", re.IGNORECASE | re.MULTILINE)


def extract_section(report: str, reviewer: str) -> str | None:
    """Returns the '### Verdict: <reviewer>' section, up to the next h1-h3 heading."""
    match = verdict_header_re(reviewer).search(report)
    if not match:
        return None
    nxt = _NEXT_SECTION_RE.search(report, match.end())
    return report[match.start():nxt.start() if nxt else len(report)].rstrip()


def _status_from_text(text: str) -> str | None:
    approved = bool(_APPROVED_RE.search(text))
    changes = bool(_CHANGES_RE.search(text))
    if approved == changes:  # neither, or an unfilled "[APPROVED] | [CHANGES REQUIRED]" template
        return None
    return APPROVED if approved else CHANGES_REQUIRED


def section_status(section: str) -> str | None:
    """Status from the '**Status:**' line; falls back to the whole section. None when inconclusive."""
    status_line = _STATUS_LINE_RE.search(section)
    if status_line:
        return _status_from_text(status_line.group(1))
    return _status_from_text(section)


def reviewer_status(report: str, reviewer: str) -> str | None:
    """APPROVED / CHANGES REQUIRED for one reviewer, '' when absent, None when inconclusive."""
    section = extract_section(report, reviewer)
    if section is not None:
        return section_status(section)

    # Markdown table row fallback: | `reviewer` | ... APPROVED ... |
    row = re.search(rf"^\|\s*`?{re.escape(reviewer)}`?\s*\|([^\n]+)", report, re.IGNORECASE | re.MULTILINE)
    if row:
        cells = row.group(1)
        if re.search(r"CHANGES\s+REQUIRED", cells, re.IGNORECASE):
            return CHANGES_REQUIRED
        if re.search(r"\bAPPROVED\b", cells, re.IGNORECASE):
            return APPROVED
        return None
    return ""


def evaluate_review(manifest: dict, report: str) -> dict:
    required = manifest.get("required_reviewers", [])
    approved, rejected, missing = [], [], []
    for rev in required:
        status = reviewer_status(report, rev)
        if status == APPROVED:
            approved.append(rev)
        elif status == CHANGES_REQUIRED:
            rejected.append(rev)
        elif status is None:
            missing.append(f"{rev} (inconclusive status)")
        else:
            missing.append(rev)

    if missing:
        exit_code = EXIT_INCOMPLETE
    elif rejected:
        exit_code = EXIT_CHANGES_REQUIRED
    else:
        exit_code = EXIT_APPROVED
    return {
        "required": required,
        "approved": approved,
        "rejected": rejected,
        "missing": missing,
        "missing_ids": [m.split(" ", 1)[0] for m in missing],
        "exit_code": exit_code,
    }


def check_review(manifest_path: str, report_path: str) -> tuple[int, str, dict]:
    if not Path(manifest_path).exists():
        return EXIT_INCOMPLETE, f"Manifest file '{manifest_path}' not found.", {}
    if not Path(report_path).exists():
        return EXIT_INCOMPLETE, f"Review report file '{report_path}' not found.", {}

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    with open(report_path, "r", encoding="utf-8") as f:
        report = f.read()

    if not manifest.get("required_reviewers", []):
        return EXIT_APPROVED, "No reviewers required for this PR.", evaluate_review(manifest, report)

    result = evaluate_review(manifest, report)
    print("=== Mechanical Review Verification Gate ===")
    print(f"Total Required Reviewers: {len(result['required'])}")
    print(f"Approved ({len(result['approved'])}): {result['approved']}")
    print(f"Changes Requested ({len(result['rejected'])}): {result['rejected']}")
    print(f"Missing / Unsigned ({len(result['missing'])}): {result['missing']}")

    if result["missing"]:
        return EXIT_INCOMPLETE, (
            f"FATAL OMISSION DETECTED: The following required reviewers were not found or "
            f"did not complete their evaluation: {result['missing']}. "
            "Failing closed: PR cannot be merged."
        ), result
    if result["rejected"]:
        return EXIT_CHANGES_REQUIRED, (
            f"CHANGES REQUESTED: One or more specialized reviewers flagged critical violations: "
            f"{result['rejected']}. PR merge blocked."
        ), result
    return EXIT_APPROVED, "All required specialized reviewers completed audit and APPROVED the PR.", result


def verify_review(manifest_path: str, report_path: str) -> tuple[bool, str]:
    exit_code, message, _ = check_review(manifest_path, report_path)
    return exit_code == EXIT_APPROVED, message


def main():
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else "logs/pr_manifest.json"
    report_path = sys.argv[2] if len(sys.argv) > 2 else "logs/pr_review/report.md"

    exit_code, message, _ = check_review(manifest_path, report_path)
    print(f"Result: {message}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
