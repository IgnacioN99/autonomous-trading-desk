#!/usr/bin/env python3
"""
recheck_bounds.py - Deterministic bounds of a re-checked confirmation (issue #267). Stdlib only, pure functions.

After a dossier expired while the user was confirming a candidate, `prime_evaluator_brief.py --recheck` re-evaluates
that one candidate. The user's earlier "yes" covers the new plan only when every check holds:
- the new dossier is APPROVED and approves the same symbol and direction (never a YOLO entry);
- the tier is the same or higher (S > A+ > A; a downgrade fails);
- trigger (entry), stop loss, TP1 and TP2 each drifted at most `max_drift_r` R of the ORIGINAL plan
  (|new - old| / |old entry - old stop loss|);
- the new stop distance |entry - stop loss| is within [1 - max_drift_r, 1 + max_drift_r] R of the original one
  (issue #279: entry and SL moving in opposite directions cannot widen or tighten the stop beyond the bound);
- the new plan's R:R to TP2 is >= 3:1 (levels on the correct side of the entry);
- the new TP1 is at least the executor's friction floor (gate_limits.MIN_TP1_DISTANCE) from the entry, on the
  profit side (issue #279: an earlier signal; the executor still enforces it);
- at most `max_age_s` seconds passed since the original dossier was evaluated (`evaluated_ts`, the verifiable
  proxy for the user's "yes", which is not persisted).
Anything missing, malformed or ambiguous fails (the user is asked again). The verdict is advisory: printed and
logged by record_evaluation.py; the hook and the executor gates are unchanged.
"""

import math
from typing import Any, Dict, List, Optional

from utils.gate_limits import MIN_TP1_DISTANCE

TIER_RANK = {"S": 3, "A+": 2, "A": 1}
MIN_RR_TP2 = 3.0
DRIFT_FIELDS = (("entry", "trigger"), ("stop_loss", "stop loss"), ("tp1", "TP1"), ("tp2", "TP2"))
_EPS = 1e-9


def normalize_tier(raw: Any) -> Optional[str]:
    """"S" / "A+" / "A" from a dossier tier ("S", "Tier S", "Tier A+ (High Score)", case-insensitive), else None."""
    text = str(raw or "").strip().upper()
    if text.startswith("TIER "):
        text = text[5:].strip()
    code = text.split(" ")[0] if text else ""
    return code if code in TIER_RANK else None


def _num(value: Any) -> Optional[float]:
    """A finite positive float, else None (booleans are not numbers)."""
    if isinstance(value, bool):
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) and val > 0 else None


def _rr_tp2(direction: str, entry: float, sl: float, tp2: float) -> Optional[float]:
    """R:R to TP2 from the entry; None when the stop loss or TP2 is on the wrong side of the entry."""
    risk = entry - sl if direction == "LONG" else sl - entry
    reward = tp2 - entry if direction == "LONG" else entry - tp2
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def _find(record: dict, symbol: str, direction: str) -> Optional[dict]:
    for c in record.get("approved_candidates") or []:
        if (isinstance(c, dict) and str(c.get("symbol") or "").upper() == symbol
                and str(c.get("direction") or "").upper() == direction):
            return c
    return None


def evaluate_recheck_bounds(old: dict, new_record: dict, now_ts: int, max_drift_r: float,
                            max_age_s: int) -> Dict[str, Any]:
    """Compares the old confirmed plan (`recheck_of` snapshot: symbol, direction, tier, entry, stop_loss, tp1, tp2,
    evaluated_ts) with the newly recorded dossier. Returns {"within_bounds": bool, "checks": [{"check", "value",
    "limit", "ok"}], "reasons": [str]}; within_bounds is True only when every check passed."""
    checks: List[dict] = []

    def check(name: str, value: Any, limit: Any, ok: bool, reason: str = "") -> bool:
        checks.append({"check": name, "value": value, "limit": limit, "ok": bool(ok),
                       **({"reason": reason} if not ok and reason else {})})
        return ok

    old = old if isinstance(old, dict) else {}
    new_record = new_record if isinstance(new_record, dict) else {}
    symbol = str(old.get("symbol") or "").upper()
    direction = str(old.get("direction") or "").upper()
    status = str(new_record.get("status") or "").upper()
    check("status", status or None, "APPROVED", status == "APPROVED",
          f"the re-check dossier is {status or 'missing'}: no trade")
    new = _find(new_record, symbol, direction) if status == "APPROVED" and symbol and direction else None
    approved = sorted(f"{c.get('symbol')} {c.get('direction')}" for c in new_record.get("approved_candidates") or []
                      if isinstance(c, dict))
    check("symbol_direction", ", ".join(approved) or None, f"{symbol} {direction}",
          new is not None and direction in ("LONG", "SHORT"),
          f"the re-check does not approve {symbol} {direction}")
    if new is not None:
        check("not_yolo", bool(new.get("is_yolo")), False, not new.get("is_yolo"),
              "a YOLO entry always asks the user")
        old_tier, new_tier = normalize_tier(old.get("tier")), normalize_tier(new.get("tier"))
        check("tier", new_tier or new.get("tier"), f">= {old_tier or old.get('tier')}",
              old_tier is not None and new_tier is not None and TIER_RANK[new_tier] >= TIER_RANK[old_tier],
              f"tier {new.get('tier')} is below the confirmed {old.get('tier')} (or unknown)")
        old_entry, old_sl = _num(old.get("entry")), _num(old.get("stop_loss"))
        old_r = abs(old_entry - old_sl) if old_entry is not None and old_sl is not None else 0.0
        for key, label in DRIFT_FIELDS:
            a, b = _num(old.get(key)), _num(new.get(key))
            if old_r <= 0 or a is None or b is None:
                check(f"drift_{key}", None, max_drift_r, False,
                      f"{label} drift not measurable (missing level or zero original R)")
                continue
            drift = abs(b - a) / old_r
            check(f"drift_{key}", round(drift, 4), max_drift_r, drift <= max_drift_r + _EPS,
                  f"{label} moved {drift:.2f}R (> {max_drift_r}R of the original plan)")
        entry, sl, tp2 = _num(new.get("entry")), _num(new.get("stop_loss")), _num(new.get("tp2"))
        stop_r = abs(entry - sl) / old_r if old_r > 0 and entry is not None and sl is not None else None
        stop_limit = f"{max(0.0, 1 - max_drift_r):g}R-{1 + max_drift_r:g}R"
        check("stop_distance", f"new {stop_r:.2f}R vs old 1.00R" if stop_r is not None else None, stop_limit,
              stop_r is not None and abs(stop_r - 1) <= max_drift_r + _EPS,
              f"stop distance is {stop_r:.2f}R of the original plan (outside {stop_limit})" if stop_r is not None
              else "stop distance not measurable (missing level or zero original R)")
        rr = _rr_tp2(direction, entry, sl, tp2) if None not in (entry, sl, tp2) else None
        check("rr_tp2", round(rr, 3) if rr is not None else None, MIN_RR_TP2,
              rr is not None and rr >= MIN_RR_TP2 - _EPS,
              "new R:R to TP2 below 3:1 (or levels on the wrong side of the entry)")
        tp1 = _num(new.get("tp1"))
        # Same signed distance as the executor's friction gate: a TP1 on the wrong side is negative
        tp1_frac = (((tp1 - entry) if direction == "LONG" else (entry - tp1)) / entry
                    if tp1 is not None and entry is not None else None)
        check("tp1_friction", f"{tp1_frac * 100:.2f}%" if tp1_frac is not None else None,
              f">= {MIN_TP1_DISTANCE * 100:.2f}%", tp1_frac is not None and tp1_frac >= MIN_TP1_DISTANCE,
              "new TP1 is closer to the entry than the executor's friction floor (or missing / on the wrong side)")
    try:
        age = int(now_ts) - int(old.get("evaluated_ts"))
    except (TypeError, ValueError):
        age = None
    check("age_s", age, max_age_s, age is not None and 0 <= age <= max_age_s,
          f"the original evaluation is {age if age is not None else 'of unknown'} s old (> {max_age_s} s)")
    return {"within_bounds": all(c["ok"] for c in checks), "checks": checks,
            "reasons": [c["reason"] for c in checks if not c["ok"] and c.get("reason")]}


def format_recheck_verdict(verdict: dict) -> List[str]:
    """Printable lines: the verdict and every check with its value and limit."""
    ok = bool((verdict or {}).get("within_bounds"))
    lines = ["WITHIN BOUNDS: the user's earlier yes covers this plan (execute with --confirmed before the deadline)"
             if ok else "OUT OF BOUNDS: do not execute; show the new plan next to the old one and ask the user again"]
    for c in (verdict or {}).get("checks") or []:
        lines.append(f"[{'PASS' if c.get('ok') else 'FAIL'}] {c.get('check')}: {c.get('value')} "
                     f"(limit {c.get('limit')})" + (f" - {c['reason']}" if c.get("reason") else ""))
    return lines
