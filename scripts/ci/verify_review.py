#!/usr/bin/env python3
"""
scripts/ci/verify_review.py
Deterministic Mechanical Gate for Antigravity PR Reviewer.

Enforces zero-hallucination and complete reviewer coverage:
1. Checks that every required reviewer from logs/pr_manifest.json has executed
   and signed their verdict in the review report.
2. Checks the status of each verdict. If any reviewer requested changes
   ([CAMBIOS REQUERIDOS]), exits with code 1 to block CI merge.
3. Exits with code 0 only if all required reviewers are present and approved.
"""

import sys
import json
import re
from pathlib import Path


def verify_review(manifest_path: str, report_path: str) -> tuple[bool, str]:
    if not Path(manifest_path).exists():
        return False, f"Manifest file '{manifest_path}' not found."

    if not Path(report_path).exists():
        return False, f"Review report file '{report_path}' not found."

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    with open(report_path, "r", encoding="utf-8") as f:
        report = f.read()

    required_reviewers = manifest.get("required_reviewers", [])
    if not required_reviewers:
        return True, "No reviewers required for this PR."

    missing_reviewers = []
    rejected_reviewers = []
    approved_reviewers = []

    for rev in required_reviewers:
        # Regex to locate the reviewer's section header
        header_pat = rf"###\s*Veredicto:\s*{re.escape(rev)}"
        match = re.search(header_pat, report, re.IGNORECASE)
        if not match:
            missing_reviewers.append(rev)
            continue

        # Extract content of this reviewer section up to next section or end
        section_start = match.start()
        next_section = re.search(r"###\s*Veredicto:", report[section_start + 10:])
        section_end = (section_start + 10 + next_section.start()) if next_section else len(report)
        section_text = report[section_start:section_end]

        # Check for status
        if re.search(r"\[APROBADO\]", section_text, re.IGNORECASE):
            approved_reviewers.append(rev)
        elif re.search(r"\[CAMBIOS\s*REQUERIDOS\]", section_text, re.IGNORECASE):
            rejected_reviewers.append(rev)
        else:
            # If no clear status tag is found, treat as missing/incomplete
            missing_reviewers.append(f"{rev} (Estado no concluyente)")

    print("=== Mechanical Review Verification Gate ===")
    print(f"Total Required Reviewers: {len(required_reviewers)}")
    print(f"Approved ({len(approved_reviewers)}): {approved_reviewers}")
    print(f"Changes Requested ({len(rejected_reviewers)}): {rejected_reviewers}")
    print(f"Missing / Unsigned ({len(missing_reviewers)}): {missing_reviewers}")

    if missing_reviewers:
        err_msg = (
            f"FATAL OMISSION DETECTED: The following required reviewers were not found or "
            f"did not complete their evaluation: {missing_reviewers}. "
            "Failing closed: PR cannot be merged."
        )
        return False, err_msg

    if rejected_reviewers:
        err_msg = (
            f"CHANGES REQUESTED: One or more specialized reviewers flagged critical violations: "
            f"{rejected_reviewers}. PR merge blocked."
        )
        return False, err_msg

    return True, "All required specialized reviewers completed audit and APPROVED the PR."


def main():
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else "logs/pr_manifest.json"
    report_path = sys.argv[2] if len(sys.argv) > 2 else "review_output.md"

    success, message = verify_review(manifest_path, report_path)
    print(f"Result: {message}")

    if not success:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
