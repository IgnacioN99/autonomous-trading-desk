#!/usr/bin/env python3
"""
scripts/ci/assemble_review.py
Deterministic assembler for the in-session multi-agent PR review (/pr-review skill).

Each reviewer subagent (.agents/agents/<reviewer>_reviewer/agent.md, generated for Claude Code into
.claude/agents/<reviewer>_reviewer.md) replies to the parent with one "### Verdict: <reviewer>" section.
This script reads that section straight from each subagent transcript, so the parent agent never retypes
or edits a verdict, and computes the consolidated verdict mechanically:
  * agy:         ~/.gemini/<product>/brain/<conversationId>/.system_generated/logs/transcript.jsonl
                 (rows agy truncated there are read from the verified sibling transcript_full.jsonl,
                 same reader as the dossier recorder: dossier_provenance.read_agy_steps)
  * Claude Code: ~/.claude/projects/<slug>/<sessionId>/subagents/agent-<agentId>.jsonl, whose meta.json
                 agentType must be "<reviewer>_reviewer" (a general-purpose agent cannot sign a verdict).

Usage (repo root):
  python3 scripts/ci/assemble_review.py --pr 13 \
      --from-subagent trading_risk=<conversationId> --from-subagent agentic_harness=<conversationId> ...
  python3 scripts/ci/assemble_review.py --pr 13 \
      --from-claude-subagent trading_risk=<agentId> --from-claude-subagent agentic_harness=<agentId> ...
  # --from-subagent also accepts Claude Code agentIds (auto-detected).
  # Runtimes without transcripts: --section <reviewer>=<file.md>

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
from scripts.ci.triage_pr import REVIEWERS  # noqa: E402
from scripts.utils import dossier_provenance as transcripts  # noqa: E402

OVERALL_APPROVED = "[APPROVED FOR MERGE]"
OVERALL_BLOCKED = "[BLOCKED BY REQUIRED CHANGES]"
OVERALL_INCOMPLETE = "[INCOMPLETE REVIEW]"


def extract_section_from_transcript(path: str, reviewer: str) -> str | None:
    """Last '### Verdict: <reviewer>' section the subagent model emitted (send_message or response).
    Raises ProvenanceError when a truncated row cannot be resolved from transcript_full.jsonl."""
    found = None
    steps, _ = transcripts.read_agy_steps(path)
    for step in steps:
        for text in transcripts._model_texts(step):
            section = gate.extract_section(text, reviewer)
            if section:
                found = section
    return found


def reviewer_agent_type(reviewer: str) -> str:
    """Subagent type that must have produced a reviewer's verdict (e.g. trading_risk -> trading_risk_reviewer)."""
    conf = REVIEWERS.get(reviewer) or {}
    return conf.get("agent") or f"{reviewer}_reviewer"


def extract_section_from_claude_transcript(path: str, reviewer: str) -> str | None:
    """Last '### Verdict: <reviewer>' section written by a Claude Code '<reviewer>_reviewer' subagent.
    Raises ProvenanceError when the transcript does not belong to that subagent type."""
    _, rows = transcripts.read_claude_subagent(path, reviewer_agent_type(reviewer))
    found = None
    for _, row in rows:
        for text in transcripts.claude_assistant_texts(row):
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


def collect_sections(required: list[str], subagents: dict, section_files: dict,
                     claude_subagents: dict | None = None) -> tuple[dict, dict, list]:
    """Returns (sections, provenance, errors) for the required reviewers.
    subagents: reviewer -> agy conversationId (Claude Code agentIds are auto-detected);
    claude_subagents: reviewer -> Claude Code agentId; section_files: reviewer -> Markdown file."""
    sections, provenance, errors = {}, {}, []
    claude_subagents = dict(claude_subagents or {})
    subagents = dict(subagents or {})
    for rev, value in list(subagents.items()):
        if transcripts.is_claude_agent_id(value) and rev not in claude_subagents:
            claude_subagents[rev] = subagents.pop(rev)
    for rev in required:
        if rev in claude_subagents:
            agent_id = claude_subagents[rev]
            try:
                path = transcripts.find_claude_subagent_transcript(agent_id)
                section = extract_section_from_claude_transcript(path, rev)
            except transcripts.ProvenanceError as e:
                errors.append(f"{rev}: {e}")
                continue
            if section:
                sections[rev] = section
                provenance[rev] = f"claude subagent {agent_id}"
            else:
                errors.append(f"{rev}: no '### Verdict: {rev}' section in Claude Code subagent {agent_id} transcript")
        elif rev in subagents:
            try:
                path = transcripts.find_subagent_transcript(subagents[rev])
                section = extract_section_from_transcript(path, rev)
            except transcripts.ProvenanceError as e:
                errors.append(f"{rev}: {e}")
                continue
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
        "- **Method:** isolated reviewer subagents (agy invoke_subagent / Claude Code Agent tool); "
        "sections copied verbatim from their transcripts.",
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
    parser.add_argument("--from-claude-subagent", action="append", default=[], metavar="REVIEWER=AGENT_ID")
    parser.add_argument("--section", action="append", default=[], metavar="REVIEWER=FILE")
    args = parser.parse_args()

    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    required = manifest.get("required_reviewers", [])
    sections, provenance, errors = collect_sections(
        required, _pairs(args.from_subagent, "--from-subagent"), _pairs(args.section, "--section"),
        _pairs(args.from_claude_subagent, "--from-claude-subagent"))

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
