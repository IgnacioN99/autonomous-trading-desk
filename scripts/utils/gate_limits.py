#!/usr/bin/env python3
"""
gate_limits.py - Single source of truth for the executor's mechanical gate limits (issue #64).

`execute_futures_trade.check_mechanical_gates` enforces these values; the YOLO scanner
(`broad_yolo_scanner.yolo_gate_failures`) and the screening pipeline import the same constants so a scan never
forwards a level the executor would reject. Stdlib only, no side effects.
"""

# GATE 2 (PROD, YOLO branch): Barbell loss cap. The loss at SL may not exceed this fraction of the isolated
# margin, i.e. SL distance from the entry x leverage <= 0.35.
YOLO_MAX_LOSS_MARGIN_FRACTION = 0.35

# GATE 2 (PROD, YOLO branch): floor of the loss cap in USDT (cap = max(this, margin x fraction)).
YOLO_MIN_LOSS_CAP_USDT = 3.75

# GATE 3 (PROD): financial friction floor. TP1 must be at least this fraction (0.35%) from the effective entry,
# on the profit side, or taker fees eat the edge.
MIN_TP1_DISTANCE = 0.0035
