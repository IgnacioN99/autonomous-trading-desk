#!/usr/bin/env python3
"""
daily_loss_gate.py - Daily Loss Gate decision (issue #207). Pure, stdlib only, no I/O.

Shared by scripts/execute_futures_trade.py (check_daily_loss_gate: the authoritative PROD gate for opening orders,
read live from today's fills) and scripts/sync_session_state.py (the ledger cache the brief and the doctor show).
Risk-reducing actions (close, break-even, heal, protect) never consult it.

Day = the UTC calendar day (day_start_ms). Inputs: the day's net realized PnL in USDT (day_net_realized: sum of the
fills' realizedPnl minus their USDT commissions; a non-USDT commission is left out of the sum and reported) and the
per-trade list of trades closed today (trade_outcomes.closed_trades_today: exit_ms, realized_r_net or, when None,
realized_r_gross, is_yolo). Three limits, from the profile (user_profile.get_daily_loss_limits):
  - Daily loss (every order): start_equity = equity_now - day_net_realized_usdt (equity before today's realized
    result), limit_usdt = daily_stop_r x risk_pct x start_equity; blocked when day_net_realized_usdt <= -limit_usdt.
  - Full-SL streak (every order): walking today's closed trades from the newest exit back, a trade at R <= -0.8 adds
    1, a scratch (|R| < 0.05) or a winner (R >= 0.05) ends the walk, a partial loss (-0.8 < R <= -0.05) or a trade
    without R is skipped (the latter counted as "unscored_closed" and named in the reason); blocked when the streak
    >= max_consecutive_sl.
  - YOLO (YOLO orders only): today's YOLO trades at R <= -0.8; blocked when >= yolo_max_daily_losses. YOLO trades
    also count in the two limits above.
evaluate() returns {"blocked" (this order refused), "scope" ("all" | "yolo" | None: the widest active restriction,
whatever the order), "reason", "day_net_realized_usdt", "day_loss_limit_usdt", "consecutive_full_sl",
"yolo_full_losses", "unscored_closed"}. Invalid numeric inputs block (fail closed).
"""

import datetime
import math
from typing import Any, Iterable, Optional, Tuple

FULL_SL_R = -0.8   # a trade closed at or below this R is a full stop-loss
SCRATCH_R = 0.05   # |R| below this is a scratch (trade_outcomes.SCRATCH_R)
RESET_NOTE = "no new entries until 00:00 UTC"


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def day_start_ms(now: Optional[float] = None) -> int:
    """Start (ms) of the UTC calendar day of `now` (epoch seconds; default the current time)."""
    dt = (datetime.datetime.now(datetime.timezone.utc) if now is None
          else datetime.datetime.fromtimestamp(float(now), datetime.timezone.utc))
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def day_net_realized(fills: Iterable[dict]) -> Tuple[float, bool]:
    """(sum of realizedPnl - USDT commissions, True when some commission was in another asset and left out)."""
    net, other_asset = 0.0, False
    for f in fills or []:
        if not isinstance(f, dict):
            continue
        net += _num(f.get("realizedPnl")) or 0.0
        commission = _num(f.get("commission")) or 0.0
        if str(f.get("commissionAsset") or "USDT").upper() == "USDT":
            net -= commission
        elif commission:
            other_asset = True
    return round(net, 8), other_asset


def trade_r(trade: dict) -> Optional[float]:
    """realized_r_net, else realized_r_gross (None without either)."""
    r = _num((trade or {}).get("realized_r_net"))
    return r if r is not None else _num((trade or {}).get("realized_r_gross"))


def consecutive_full_sl(trades: Iterable[dict]) -> int:
    """Full-SL streak of today's closed trades (module docstring)."""
    streak = 0
    for t in sorted((t for t in trades or [] if isinstance(t, dict)),
                    key=lambda t: _num(t.get("exit_ms")) or 0, reverse=True):
        r = trade_r(t)
        if r is None:
            continue
        if r <= FULL_SL_R:
            streak += 1
        elif abs(r) < SCRATCH_R or r >= SCRATCH_R:
            break
    return streak


def yolo_full_losses(trades: Iterable[dict]) -> int:
    """Today's YOLO trades closed at R <= FULL_SL_R."""
    return sum(1 for t in trades or [] if isinstance(t, dict) and t.get("is_yolo") is True
               and (trade_r(t) is not None and trade_r(t) <= FULL_SL_R))


def unscored_closed(trades: Iterable[dict]) -> int:
    """Today's closed trades with neither net nor gross R (left out of the streak and the YOLO count)."""
    return sum(1 for t in trades or [] if isinstance(t, dict) and trade_r(t) is None)


def evaluate(fills_net_pnl_usdt: Any, trades: Iterable[dict], *, risk_pct: Any, equity_now: Any, daily_stop_r: Any,
             max_consecutive_sl: Any, yolo_max_daily_losses: Any, is_yolo_order: bool) -> dict:
    """Daily Loss Gate state for one order (module docstring). risk_pct is a fraction (0.02 = 2%). The state also
    carries "unscored_closed" (closed trades without R, skipped by the streak); when > 0 the reason names it (an
    otherwise inactive gate then has an informational reason)."""
    trades = [t for t in trades or [] if isinstance(t, dict)]
    state = _evaluate(fills_net_pnl_usdt, trades, risk_pct=risk_pct, equity_now=equity_now, daily_stop_r=daily_stop_r,
                      max_consecutive_sl=max_consecutive_sl, yolo_max_daily_losses=yolo_max_daily_losses,
                      is_yolo_order=is_yolo_order)
    state["unscored_closed"] = unscored_closed(trades)
    if state["unscored_closed"]:
        note = (f"unscored_closed={state['unscored_closed']} (closed today without net or gross R, not counted in the "
                "consecutive-SL streak)")
        state["reason"] = f"{state['reason']}; {note}" if state["reason"] else f"DAILY LOSS GATE: inactive; {note}"
    return state


UNAUDITED_SYMBOLS_CAP = 10


def note_unaudited_closing_symbols(state: dict, symbols: Iterable[str]) -> dict:
    """Informational only (issue #207 round 4; never blocks): closing fills today of symbols with no audit record
    (e.g. manual trades) count in the USDT figure but not in the streak. state["unaudited_closing_symbols"] = the
    sorted symbols (at most UNAUDITED_SYMBOLS_CAP); when non-empty the reason names them."""
    syms = sorted({str(s).upper() for s in symbols or [] if str(s or "").strip()})[:UNAUDITED_SYMBOLS_CAP]
    state["unaudited_closing_symbols"] = syms
    if syms:
        note = (f"unaudited_closing_symbols={','.join(syms)} (closing fills without an audit record: in the USDT "
                "figure, not in the consecutive-SL streak)")
        state["reason"] = f"{state['reason']}; {note}" if state.get("reason") else f"DAILY LOSS GATE: inactive; {note}"
    return state


def _evaluate(fills_net_pnl_usdt, trades, *, risk_pct, equity_now, daily_stop_r, max_consecutive_sl,
              yolo_max_daily_losses, is_yolo_order) -> dict:
    net, risk, equity, stop_r = (_num(fills_net_pnl_usdt), _num(risk_pct), _num(equity_now), _num(daily_stop_r))
    max_sl, max_yolo = _num(max_consecutive_sl), _num(yolo_max_daily_losses)
    streak, yolo_losses = consecutive_full_sl(trades), yolo_full_losses(trades)
    state = {"blocked": True, "scope": "all", "reason": None, "day_net_realized_usdt": net,
             "day_loss_limit_usdt": None, "consecutive_full_sl": streak, "yolo_full_losses": yolo_losses}
    if None in (net, risk, equity, stop_r, max_sl, max_yolo) or risk <= 0 or stop_r <= 0 or max_sl < 1 \
            or max_yolo < 1:
        state["reason"] = (f"DAILY LOSS GATE: inputs invalid (day_net_realized_usdt={fills_net_pnl_usdt}, "
                           f"risk_pct={risk_pct}, equity_now={equity_now}) — opening refused (fail closed)")
        return state
    start_equity = equity - net
    limit = stop_r * risk * start_equity
    state["day_net_realized_usdt"] = round(net, 4)
    state["day_loss_limit_usdt"] = round(limit, 4)
    if start_equity <= 0 or net <= -limit:
        state["reason"] = (f"DAILY LOSS GATE: day_net_realized_usdt={net:+.2f} <= limit_usdt={-limit:+.2f} "
                           f"(daily_stop_r={stop_r:g} x risk_pct={risk * 100:g}% x start_equity={start_equity:.2f})"
                           f" — {RESET_NOTE}")
        return state
    if streak >= max_sl:
        state["reason"] = (f"DAILY LOSS GATE: consecutive_full_sl={streak} >= max_consecutive_sl={int(max_sl)} "
                           f"(trades closed today at R <= {FULL_SL_R:g}) — {RESET_NOTE}")
        return state
    if yolo_losses >= max_yolo:
        state.update(blocked=bool(is_yolo_order), scope="yolo",
                     reason=(f"DAILY LOSS GATE (YOLO): yolo_full_losses={yolo_losses} >= "
                             f"yolo_max_daily_losses={int(max_yolo)} today — no new YOLO entries until 00:00 UTC"))
        return state
    state.update(blocked=False, scope=None)
    return state
