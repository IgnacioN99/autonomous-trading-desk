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


def gate_event_key(sha, env, symbol, direction, ts):
    """Identity of a logs/gate_denials.jsonl event (issue #275; mirrored by pre_trade_guard._gate_denial_key):
    ("sha", dossier_sha256, symbol, direction), else (an event without a sha: the hook's check_dossier adds none
    outside PROD) ("window", env, symbol, direction, ts // DEDUPE_WINDOW_SECONDS); None when ts is not a number."""
    if isinstance(sha, str) and sha:
        return ("sha", sha, symbol, direction)
    try:
        return ("window", env, symbol, direction, int(float(ts)) // DEDUPE_WINDOW_SECONDS)
    except (TypeError, ValueError, OverflowError):
        return None


def row_gate(row: dict) -> tuple:
    """(gate, gate_source) of a shadow row; a row written before issue #251 maps its vol_ratio category
    (DRY_VOLUME_FAKE_TIER_S -> DRY_VOLUME, anything else -> OTHER) with source "legacy_category"."""
    gate = row.get("gate")
    if isinstance(gate, str) and gate:
        return gate, str(row.get("gate_source") or "unknown")
    return ("DRY_VOLUME" if row.get("rejection_category") == "DRY_VOLUME_FAKE_TIER_S" else GATE_FALLBACK,
            "legacy_category")
