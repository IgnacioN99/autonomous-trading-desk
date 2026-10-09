#!/usr/bin/env python3
"""
calibration_fallback.py - The confirmation messages the executor and the PreToolUse guard emit when
utils/score_calibration.py cannot be imported (issue #207). Constants only, stdlib-free, so both gates share one text
even when the calibration module is the missing one.
"""

# An unconfirmed Tier S candidate asks the user (the bucket cannot be checked)
TIER_S_FALLBACK_MESSAGE = ("Tier S score bucket not calibrated (calibration module unavailable): ask the user and "
                           "rerun with --confirmed.")

# The squeeze backstop (RULE 9) still asks for a flagged, unbound or unreadable radar snapshot
SQUEEZE_FALLBACK_MESSAGE = ("squeeze_risk SHORT: user confirmation required (calibration module unavailable; radar "
                            "snapshot flagged, unbound or unreadable): ask the user and rerun with --confirmed.")
