#!/usr/bin/env python3
"""
portfolio_exposure.py - Pure portfolio delta classification from Binance /fapi/v2/positionRisk rows.

Single implementation shared by scripts/sync_session_state.py (the logs/session_state.json cache) and the PROD
gates of scripts/execute_futures_trade.py (Gate 0A max open positions, Gate 1 delta-neutral), which classify the
live exchange view with it (issue #101). No I/O.
"""

# |delta_ratio| above this marks the portfolio LONG_HEAVY / SHORT_HEAVY; delta_ratio = (long - short) / (long + short)
DELTA_HEAVY_RATIO = 0.35
LONG_HEAVY = "LONG_HEAVY"
SHORT_HEAVY = "SHORT_HEAVY"
DELTA_BALANCED = "DELTA_BALANCED"


def _num(row, field, default=None):
    """float(row[field]); a missing field gives `default` (None: required). Raises ValueError when unparseable."""
    value = row.get(field)
    if value is None and default is not None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"positionRisk row {row.get('symbol')!r}: unparseable {field} {value!r}") from None


def classify_delta(delta_ratio):
    """LONG_HEAVY if delta_ratio > DELTA_HEAVY_RATIO, SHORT_HEAVY if < -DELTA_HEAVY_RATIO, else DELTA_BALANCED."""
    if delta_ratio > DELTA_HEAVY_RATIO:
        return LONG_HEAVY
    if delta_ratio < -DELTA_HEAVY_RATIO:
        return SHORT_HEAVY
    return DELTA_BALANCED


def compute_exposure(position_risk_rows):
    """
    Classifies positionRisk rows (numbers may be strings, as Binance returns them). Rows with positionAmt == 0 are
    ignored. Fail closed: a row that is not a dict, has no symbol, or has an unparseable positionAmt / markPrice
    raises ValueError (never silently dropped, so a malformed ledger read cannot hide a position).
    Notional = |positionAmt * markPrice| per side.
    Returns {"active_positions": [{"symbol", "side": "LONG" | "SHORT", "qty", "entry_price", "mark_price",
    "notional", "row"}], "long_notional", "short_notional", "net_notional", "delta_ratio", "delta_bias",
    "total_active_positions", "symbols"} (floats unrounded; symbols = sorted distinct symbols with a position).
    """
    active = []
    long_notional = 0.0
    short_notional = 0.0
    for row in position_risk_rows or []:
        if not isinstance(row, dict) or not row.get("symbol"):
            raise ValueError(f"malformed positionRisk row: {row!r}")
        amt = _num(row, "positionAmt", 0.0)
        if amt == 0:
            continue
        mark = _num(row, "markPrice", 0.0)
        notional = abs(amt * mark)
        side = "LONG" if amt > 0 else "SHORT"
        if side == "LONG":
            long_notional += notional
        else:
            short_notional += notional
        active.append({"symbol": str(row["symbol"]).upper(), "side": side, "qty": amt,
                       "entry_price": _num(row, "entryPrice", 0.0), "mark_price": mark, "notional": notional,
                       "row": row})
    total = long_notional + short_notional
    net = long_notional - short_notional
    delta_ratio = (net / total) if total > 0 else 0.0
    return {
        "active_positions": active,
        "long_notional": long_notional,
        "short_notional": short_notional,
        "net_notional": net,
        "delta_ratio": delta_ratio,
        "delta_bias": classify_delta(delta_ratio),
        "total_active_positions": len(active),
        "symbols": sorted({p["symbol"] for p in active}),
    }
