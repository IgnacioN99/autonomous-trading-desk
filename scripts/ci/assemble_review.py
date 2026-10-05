#!/usr/bin/env python3
"""
scripts/ci/assemble_review.py
Deterministic assembler for the in-session multi-agent PR review (/pr-review skill).

Each reviewer subagent (.agents/agents/<reviewer>_reviewer/agent.md) replies to the parent with a
send_message holding one "### Verdict: <reviewer>" section. This script reads that section straight
from each subagent transcript (~/.gemini/<product>/brain/<conversationId>/...), so the parent agent
never retypes or edits a verdict, and computes the consolidated verdict mechanically.

Usage (repo root):
  python3 scripts/ci/assemble_review.py --pr 13 \
      --from-subagent trading_risk=<conversationId> --from-subagent agentic_harness=<conversationId> ...
  # Runtimes without agy transcripts: --section <reviewer>=<file.md>

Writes logs/pr_review/report.md (--out). Exit codes follow verify_review.py:
  0 all approved | 1 changes required | 2 incomplete (missing or inconclusive reviewers)
"""

import os
import sys
import json
import argparse
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts.ci import verify_review as gate  # noqa: E402
from scripts.utils import dossier_provenance as transcripts  # noqa: E402

OVERALL_APPROVED = "[APPROVED FOR MERGE]"
OVERALL_BLOCKED = "[BLOCKED BY REQUIRED CHANGES]"
OVERALL_INCOMPLETE = "[INCOMPLETE REVIEW]"


def extract_section_from_transcript(path: str, reviewer: str) -> str | None:
    """Last '### Verdict: <reviewer>' section the subagent model emitted (send_message or response)."""
    found = None
    for step in transcripts._read_steps(path):
        for text in transcripts._model_texts(step):
            section = gate.extract_section(text, reviewer)
            if section:
                found = section
    return found


def _pairs(values: list[str], flag: str) -> dict:
    out = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit(f"{flag} expects <reviewer>=<value>, got '{value}'")
        key, val = value.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def collect_sections(required: list[str], subagents: dict, section_files: dict) -> tuple[dict, dict, list]:
    """Returns (sections, provenance, errors) for the required reviewers."""
    sections, provenance, errors = {}, {}, []
    for rev in required:
        if rev in subagents:
            try:
                path = transcripts.find_subagent_transcript(subagents[rev])
            except transcripts.ProvenanceError as e:
                errors.append(f"{rev}: {e}")
                continue
            section = extract_section_from_transcript(path, rev)
            if section:
                sections[rev] = section
                provenance[rev] = f"subagent {subagents[rev]}"
            else:
                errors.append(f"{rev}: no '### Verdict: {rev}' section in subagent {subagents[rev]} transcript")
        elif rev in section_files:
            text = Path(section_files[rev]).read_text(encoding="utf-8")
            section = gate.extract_section(text, rev)
            if section:
                sections[rev] = section
                provenance[rev] = f"file {Path(section_files[rev]).as_posix()}"
            else:
                errors.append(f"{rev}: no '### Verdict: {rev}' section in {section_files[rev]}")
        else:
            errors.append(f"{rev}: no subagent conversation or section file provided")
    return sections, provenance, errors


def build_report(manifest: dict, sections: dict, provenance: dict, pr: str = "") -> tuple[str, dict]:
    required = manifest.get("required_reviewers", [])
    body = "\n\n".join(sections[rev] for rev in required if rev in sections)
    result = gate.evaluate_review(manifest, body)

    if result["missing"]:
        overall = OVERALL_INCOMPLETE
        action = (f"Do not merge. Re-run the missing reviewers ({', '.join(result['missing_ids'])}) "
                  "with /pr-review before deciding.")
    elif result["rejected"]:
        overall = OVERALL_BLOCKED
        action = (f"Do not merge. Address the critical violations raised by {', '.join(result['rejected'])}, "
                  "push the fixes and re-run /pr-review.")
    else:
        overall = OVERALL_APPROVED
        action = "All required reviewers approved. A human still reviews and merges the PR."

    def status_of(rev: str) -> str:
        if rev in result["approved"]:
            return gate.APPROVED
        if rev in result["rejected"]:
            return gate.CHANGES_REQUIRED
        return "MISSING" if rev not in sections else "INCONCLUSIVE"

    lines = [
        "# Pull Request Audit Report",
        "",
        f"- **PR:** {('#' + str(pr)) if pr else 'n/a'} | **Base:** `{manifest.get('base_ref', '')}` | "
        f"**Head:** `{str(manifest.get('head_sha', ''))[:12]}`",
        f"- **Changed Files:** {len(manifest.get('changed_files', []))} | "
        f"**Fail-Closed Triage:** {manifest.get('fail_closed_triggered', False)}",
        f"- **Required Reviewers:** {', '.join(required) or 'none'}",
        "- **Method:** isolated agy reviewer subagents (invoke_subagent); sections copied verbatim from their transcripts.",
        "",
        body if body else "_No reviewer sections were collected._",
        "",
        "### Final Consolidated Verdict",
        f"- **Overall Status:** {overall}",
        "- **Reviewers:**",
    ]
    for rev in required:
        lines.append(f"  - `{rev}`: {status_of(rev)} ({provenance.get(rev, 'not received')})")
    lines.append(f"- **Action for the Human Operator:** {action}")
    return "\n".join(lines) + "\n", result


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble the multi-agent PR review report.")
    parser.add_argument("--manifest", default="logs/pr_manifest.json")
    parser.add_argument("--out", default="logs/pr_review/report.md")
    parser.add_argument("--pr", default="", help="PR number (shown in the report).")
    parser.add_argument("--from-subagent", action="append", default=[], metavar="REVIEWER=CONVERSATION_ID")
    parser.add_argument("--section", action="append", default=[], metavar="REVIEWER=FILE")
    args = parser.parse_args()

    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    required = manifest.get("required_reviewers", [])
    sections, provenance, errors = collect_sections(
        required, _pairs(args.from_subagent, "--from-subagent"), _pairs(args.section, "--section"))

    report, result = build_report(manifest, sections, provenance, args.pr)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    for err in errors:
        print(f"WARNING: {err}")
    print(f"Approved: {result['approved']} | Changes required: {result['rejected']} | Missing: {result['missing']}")
    print(f"Report written to: {out.as_posix()}")
    return result["exit_code"] if required else gate.EXIT_APPROVED


if __name__ == "__main__":
    sys.exit(main())
