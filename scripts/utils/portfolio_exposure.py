#!/usr/bin/env python3
"""
portfolio_exposure.py - Pure portfolio delta classification from Binance /fapi/v2/positionRisk rows.

Single implementation shared by scripts/sync_session_state.py (the logs/session_state.json cache) and the PROD
gates of scripts/execute_futures_trade.py (Gate 0A max open positions, Gate 1 delta-neutral, Gate 2 loss cap),
which classify the live exchange view with it (issues #101, #119). Gate 1 also counts the resting opening orders
(resting_opening_legs) and projects the new order (book_exposure). No I/O.
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


def book_exposure(long_notional, short_notional):
    """{"long_notional", "short_notional", "net_notional", "delta_ratio", "delta_bias"} of a book of gross long /
    short notionals (an empty book is DELTA_BALANCED with delta_ratio 0)."""
    total = long_notional + short_notional
    net = long_notional - short_notional
    delta_ratio = (net / total) if total > 0 else 0.0
    return {"long_notional": long_notional, "short_notional": short_notional, "net_notional": net,
            "delta_ratio": delta_ratio, "delta_bias": classify_delta(delta_ratio)}


def project_order(book, is_long, order_notional):
    """book_exposure of `book` (a book_exposure / compute_exposure result) after adding a new order's notional on
    its side (Gate 1 new-order rule, issue #119)."""
    add = abs(order_notional)
    return book_exposure(book["long_notional"] + (add if is_long else 0.0),
                         book["short_notional"] + (0.0 if is_long else add))


def compute_exposure(position_risk_rows):
    """
    Classifies positionRisk rows (numbers may be strings, as Binance returns them). Rows with positionAmt == 0 are
    ignored (they may lack markPrice). Fail closed: a row that is not a dict, has no symbol, has a missing or
    unparseable positionAmt, or a non-zero positionAmt with a missing or unparseable markPrice raises ValueError
    (never silently dropped or zeroed, so a malformed ledger read cannot hide a position). entryPrice defaults to 0.
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
        amt = _num(row, "positionAmt")
        if amt == 0:
            continue
        mark = _num(row, "markPrice")
        notional = abs(amt * mark)
        side = "LONG" if amt > 0 else "SHORT"
        if side == "LONG":
            long_notional += notional
        else:
            short_notional += notional
        active.append({"symbol": str(row["symbol"]).upper(), "side": side, "qty": amt,
                       "entry_price": _num(row, "entryPrice", 0.0), "mark_price": mark, "notional": notional,
                       "row": row})
    out = {"active_positions": active}
    out.update(book_exposure(long_notional, short_notional))
    out["total_active_positions"] = len(active)
    out["symbols"] = sorted({p["symbol"] for p in active})
    return out


def unrealized_pnl_total(exposure):
    """Sum of unRealizedProfit over the open positions of a compute_exposure result (Gate 2 loss cap, issue #119).
    A missing or unparseable unRealizedProfit on an open position raises ValueError (fail closed)."""
    return sum(_num(p["row"], "unRealizedProfit") for p in exposure["active_positions"])


def _positive(value):
    """float(value) when it parses and is > 0, else None."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _registry_kind(kind):
    return "STOP_MARKET" if str(kind or "").upper() == "STOP_MARKET" else "LIMIT"


def resting_opening_legs(resting, registry_records=()):
    """
    Long / short legs of the opening orders resting on the exchange (Gate 1, issue #119).
    `resting`: the live opening orders (neither reduceOnly nor closePosition) as
    execute_futures_trade.resting_entry_info dicts {"symbol", "kind", "id", "side", "price", "quantity",
    "executed_qty"}; `registry_records`: the logs/pending_entries.json records of the target env.
    BUY -> LONG, SELL -> SHORT; notional = price (trigger or limit) x remaining quantity, remaining quantity =
    quantity - executed_qty (a partially filled LIMIT's filled part is already in positionRisk).
    Dedupe, every live order counts exactly once:
      - a live order with a positive quantity counts with it (live wins over the registry);
      - a live order without one (the MCP algo listing carries no quantity) takes total_qty (and
        trigger_or_limit_price when its own price is not positive) from its registry record, matched by
        symbol + entry id + kind (the find_unregistered_resting_entries key; the MCP listing carries algoId), and
        only when the live order has no id by symbol + entry_side + price;
      - a registry record without a live order is ignored (filled: already in positionRisk; gone: no exposure).
    Fail closed (ValueError): a live order with no quantity and no registry record, an unparseable quantity, an
    unknown side, or no positive price / registry total_qty.
    Returns [{"symbol", "side": "LONG" | "SHORT", "qty", "price", "notional", "source": "live" | "registry"}].
    """
    records = [r for r in (registry_records or []) if isinstance(r, dict)]
    by_id = {(str(r.get("symbol", "")).upper(), str(r.get("entry_id")), _registry_kind(r.get("kind"))): r
             for r in records if r.get("entry_id") is not None}
    legs = []
    for o in resting or []:
        sym = str(o.get("symbol") or "").upper()
        label = f"resting opening order {sym} {o.get('kind')} {o.get('id')}"
        side = str(o.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"{label}: unknown side {o.get('side')!r}")
        price = _positive(o.get("price"))
        raw_qty = o.get("quantity")
        live_qty = None
        if raw_qty not in (None, ""):
            try:
                live_qty = float(raw_qty)
            except (TypeError, ValueError):
                raise ValueError(f"{label}: unparseable quantity {raw_qty!r}") from None
        if live_qty is not None and live_qty > 0:
            executed = o.get("executed_qty")
            try:
                executed = float(executed) if executed not in (None, "") else 0.0
            except (TypeError, ValueError):
                raise ValueError(f"{label}: unparseable executedQty {executed!r}") from None
            qty = live_qty - max(executed, 0.0)
            source = "live"
        else:
            if o.get("id") is not None:
                rec = by_id.get((sym, str(o.get("id")), _registry_kind(o.get("kind"))))
            else:
                rec = next((r for r in records if str(r.get("symbol", "")).upper() == sym
                            and str(r.get("entry_side") or "").upper() == side and price is not None
                            and _positive(r.get("trigger_or_limit_price")) is not None
                            and abs(float(r["trigger_or_limit_price"]) - price) <= 1e-9 * max(1.0, price)), None)
            if rec is None:
                raise ValueError(f"{label}: the exchange reports no quantity and logs/pending_entries.json has no "
                                 "record for it, so its exposure cannot be measured")
            qty = _positive(rec.get("total_qty"))
            if qty is None:
                raise ValueError(f"{label}: registry record has no positive total_qty ({rec.get('total_qty')!r})")
            if price is None:
                price = _positive(rec.get("trigger_or_limit_price"))
            source = "registry"
        if price is None:
            raise ValueError(f"{label}: no positive trigger/limit price ({o.get('price')!r})")
        if qty <= 0:
            continue   # fully executed: already in positionRisk
        legs.append({"symbol": sym, "side": "LONG" if side == "BUY" else "SHORT", "qty": qty, "price": price,
                     "notional": abs(qty * price), "source": source})
    return legs
