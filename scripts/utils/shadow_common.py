#!/usr/bin/env python3
"""
shadow_common.py - Definitions shared by the shadow desk scripts (issue #290).

scripts/shadow_tracker.py (registration) re-exports them and scripts/shadow_analytics.py (read-only regret and policy
replay) imports them from here, so the report does not import the tracker. Stdlib only, no side effects, no file
reads. Report-only: nothing here gates an order.
"""

# Typed rejection reasons of the dossier's rejected_candidates (issue #251). K5 squeeze risk only caps a tier.
# DELTA_GATE_POST_APPROVAL (issue #261) is assigned by the hook's denial log, never by the evaluator.
GATE_ENUM = ("DELTA_GATE", "MACRO_SHORT", "DUPLICATE_RESTING", "DRY_VOLUME", "FRICTION", "CATALYST_DOWNGRADE",
             "UNREADABLE_BOOK", "DAILY_LOSS_GATE", "OTHER", "DELTA_GATE_POST_APPROVAL")
GATE_FALLBACK = "OTHER"
POST_APPROVAL_GATE = "DELTA_GATE_POST_APPROVAL"
BLOCKER_GATES = ("DELTA_GATE", "DUPLICATE_RESTING", POST_APPROVAL_GATE)
DEDUPE_WINDOW_SECONDS = 3600  # one row per (symbol, direction, gate) within this window, across dossiers (#262)


def row_gate(row: dict) -> tuple:
    """(gate, gate_source) of a shadow row; a row written before issue #251 maps its vol_ratio category
    (DRY_VOLUME_FAKE_TIER_S -> DRY_VOLUME, anything else -> OTHER) with source "legacy_category"."""
    gate = row.get("gate")
    if isinstance(gate, str) and gate:
        return gate, str(row.get("gate_source") or "unknown")
    return ("DRY_VOLUME" if row.get("rejection_category") == "DRY_VOLUME_FAKE_TIER_S" else GATE_FALLBACK,
            "legacy_category")
