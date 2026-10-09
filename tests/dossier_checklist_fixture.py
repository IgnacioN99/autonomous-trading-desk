#!/usr/bin/env python3
"""
dossier_checklist_fixture.py - Shared test fixture (not a test module): the evaluator's '## Precondition Checklist'
consistent with a <dossier_json> payload, as dossier_provenance.check_precondition_checklist reads it (issues #27,
#223). Fixtures that write evaluator transcripts put it in the same message as the block.
"""


def checklist_for(payload: dict) -> str:
    """Precondition Checklist consistent with the payload (issue #27): K1-K3/C3.1 [x] and K4 '[x] ... -> APPROVED'
    per approved candidate, and a C4.2 line carrying the raw status."""
    lines = ["## Precondition Checklist", "- [x] C0.2 Brief age: 1 min -> PASS"]
    for c in payload.get("approved_candidates") or []:
        prefix = (f"{str(c.get('symbol') or '').strip().upper()} {str(c.get('direction') or '').strip().upper()}"
                  + (" (YOLO)" if c.get("is_yolo") else ""))
        lines += [f"- [x] {prefix} {check} gate: brief value -> PASS" for check in ("K1", "K2", "K3", "C3.1")]
        lines.append(f"- [x] {prefix} K4 Verdict: K1-K3 PASS -> APPROVED (Tier S)")
    lines.append(f"- [{'x' if payload.get('status') != 'REJECTED' else ' '}] C4.2 Overall status: verdict -> "
                 f"{payload.get('status')}")
    return "\n".join(lines) + "\n\n## 1. Basket\n"
