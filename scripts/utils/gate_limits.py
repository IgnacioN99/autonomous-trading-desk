#!/usr/bin/env python3
"""
gate_limits.py - Single source of truth for the executor's mechanical gate limits (issue #64).

`execute_futures_trade.check_mechanical_gates` enforces these values; the YOLO scanner
(`broad_yolo_scanner.yolo_gate_failures`) and the screening pipeline import the same constants so a scan never
forwards a level the executor would reject. Stdlib only, no side effects. It also holds the desk's fee model
(TAKER_FEE_RATE, MAKER_FEE_RATE, expected_fee_r; issue #268).
"""

# GATE 2 (PROD, YOLO branch): Barbell loss cap. The loss at SL may not exceed this fraction of the isolated
# margin, i.e. SL distance from the entry x leverage <= 0.35.
YOLO_MAX_LOSS_MARGIN_FRACTION = 0.35

# GATE 2 (PROD, YOLO branch): floor of the loss cap in USDT (cap = max(this, margin x fraction)).
YOLO_MIN_LOSS_CAP_USDT = 3.75

# Protect-pending loss-cap re-check (PROD, issue #156): a FILLED standard (non-YOLO) resting entry whose loss at SL
# exceeds the live Gate 2 cap only because equity dropped after placement stays trusted (warning) while the loss is
# <= min(the cap stored at registration, live cap x this tolerance). Bounds a forged stored cap; above it the record
# is untrusted.
PENDING_DRIFT_CAP_TOLERANCE = 1.2

# GATE 3 (PROD): financial friction floor. TP1 must be at least this fraction (0.35%) from the effective entry,
# on the profit side, or taker fees eat the edge.
MIN_TP1_DISTANCE = 0.0035

# Fee model (issue #268): Binance USDT-M futures base commission rates (same values as exit_policy_sim's defaults).
TAKER_FEE_RATE = 0.0005
MAKER_FEE_RATE = 0.0002


def expected_fee_r(risk_pct, entry_rate=TAKER_FEE_RATE, sl_rate=TAKER_FEE_RATE):
    """Plan-time estimate of the round-trip fee in R (issue #268): (entry_rate + sl_rate) x 100 / risk_pct, where
    risk_pct is the stop distance in percent of the entry. The default is the worst case, a taker entry plus a taker
    stop loss (TP limits fill as maker and cost less). None for a missing, non-numeric, non-finite or non-positive
    risk_pct. An estimate, not a measurement (trade_outcomes.py measures fees_r from the fills)."""
    if isinstance(risk_pct, bool):
        return None
    try:
        risk_pct = float(risk_pct)
    except (TypeError, ValueError):
        return None
    if risk_pct != risk_pct or risk_pct in (float("inf"), float("-inf")) or risk_pct <= 0:
        return None
    return (entry_rate + sl_rate) * 100 / risk_pct


# Crossed-trigger R:R gate (PROD, issue #165): when the trigger is already crossed at execution time, the order
# enters at the current price; R:R to TP2 from that price (|TP2 - price| / |price - SL|) must be at least this.
MIN_RR_TP2_CROSSED = 3.0

# Crossed-trigger risk clamp (PROD, issue #201): an explicit standard margin is clamped so the loss at SL fits this
# fraction of the Gate 2 loss cap; the 2% haircut absorbs Gate 2's own equity re-read (issue #236).
RISK_CLAMP_HAIRCUT = 0.98
