#!/usr/bin/env python3
"""
delta_fit.py - Advisory post-trade delta preview of approved candidates (issue #298, item 4). Stdlib +
portfolio_exposure; no network, no signed request, no writes.

The recorder (record_evaluation._print_summary) prints, per approved candidate, whether it would pass the executor's
Gate 1 (delta-neutral) against the cached book, and the cumulative ratio of the approved set:
  - book: positions' long / short notionals of logs/session_state.json plus the resting entries of
    logs/pending_entries.json (notional = total_qty x trigger_or_limit_price) that the state's `resting_entries` also
    lists (a dead record is not counted). The state must be valid, of the dossier's environment and at most
    STATE_MAX_AGE_S old (the executor's stale limit); otherwise, with an unreadable registry, when the state lists
    a resting entry the registry has no record for, when a record's symbol has an open position (a partial fill's
    remainder is not in the cache, Gate 1 counts it), or when an unlisted record is in the state's resting_mismatches
    or was placed at or after the sync (or has no placed_at_ts), UNKNOWN;
  - candidate notional (estimate): min(risk_per_trade_usdt / |entry - stop_loss| x entry,
    account_equity_usdt x max_margin_ratio x leverage), YOLO: yolo_margin_usdt x leverage, from the risk_profile of
    logs/primed_brief.json, only when the brief's generated_at_ts equals the dossier's brief_generated_at_ts;
  - rule: the same as Gate 1 (execute_futures_trade.py): blocked if the book (positions + resting) or the state's
    positions-only delta_bias label is already heavy on the candidate's side, or if the order tips a non-empty book
    heavy its way (portfolio_exposure.DELTA_HEAVY_RATIO); an empty book passes.
Fail closed to UNKNOWN, never to `fits`. Every number is an estimate (no step rounding / minNotional): the executor's
Gate 1 re-reads the exchange and decides.
"""

import datetime
import json
import os
import re
from collections import Counter

try:
    from utils.portfolio_exposure import book_exposure, project_order, LONG_HEAVY, SHORT_HEAVY
except ImportError:  # pragma: no cover - imported as scripts.utils.delta_fit
    from scripts.utils.portfolio_exposure import book_exposure, project_order, LONG_HEAVY, SHORT_HEAVY

STATE_MAX_AGE_S = 300  # the executor's Gate 1 refuses an older session_state.json in PROD
FITS, BLOCKED, UNKNOWN = "fits", "blocked_by_delta", "UNKNOWN"
ESTIMATE_NOTE = "estimate (no step rounding / minNotional); the executor's Gate 1 re-reads the exchange and decides"
BOOK_UNAVAILABLE = "book unavailable/stale: run `sync_session_state.py` or trust the executor"
TESTNET_NA = "n/a (TESTNET, Gate 1 not enforced)"
_SAFE_SYMBOL = re.compile(r"^[A-Z0-9]{2,30}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _float(value):
    """float(value), or None when missing or unparseable (NaN / inf too)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def load_registry_records(logs_dir, target_env):
    """Same-env records of <logs_dir>/pending_entries.json: (records, error). The semantics of
    sync_session_state._load_registry_records (a missing file is no records; an unreadable or malformed one is an
    error), with the workspace's logs dir instead of the module's (that module imports the executor)."""
    path = os.path.join(logs_dir, "pending_entries.json")
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [], f"pending entries registry unreadable ({type(e).__name__})"
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return [], "pending entries registry malformed"
    return [r for r in data["entries"].values() if isinstance(r, dict) and r.get("target_env") == target_env], None


def book_from_state(state, records, registry_error, target_env, now_ts):
    """({"long", "short", "resting": [legs], "file_bias"}, None) or (None, reason). `state`: the parsed
    session_state.json (None when missing or unreadable); `records`: the env's registry records. Legs: {symbol,
    direction, notional, entry_id, kind, expires_at_ts, score}, only for (symbol, direction) the state's
    resting_entries lists (as many as it lists); a listed entry left without a record is UNKNOWN, and so is an
    unlisted record that may still rest (see the module docstring). file_bias: the state's positions-only delta_bias
    label."""
    if not isinstance(state, dict):
        return None, f"session_state.json missing or unreadable; {BOOK_UNAVAILABLE}"
    if state.get("is_valid") is not True or "error" in state:
        return None, f"session_state.json invalid; {BOOK_UNAVAILABLE}"
    state_env = str(state.get("target_env") or "").lower()
    if state_env != str(target_env or "").lower():
        return None, (f"session_state.json is for {state_env.upper() or 'an unknown environment'}, the dossier for "
                      f"{str(target_env).upper()}; {BOOK_UNAVAILABLE}")
    updated = _float(state.get("last_updated_ts"))
    if updated is None or updated <= 0 or now_ts - updated > STATE_MAX_AGE_S:
        age = "unknown age" if updated is None or updated <= 0 else f"{int(now_ts - updated)}s"
        return None, f"session_state.json stale ({age} > {STATE_MAX_AGE_S}s); {BOOK_UNAVAILABLE}"
    exp = state.get("portfolio_exposure")
    if not isinstance(exp, dict):
        return None, f"session_state.json malformed; {BOOK_UNAVAILABLE}"
    long_n, short_n = _float(exp.get("long_notional_usdt")), _float(exp.get("short_notional_usdt"))
    listed = exp.get("resting_entries")
    if long_n is None or short_n is None or not isinstance(listed, list):
        return None, f"session_state.json malformed; {BOOK_UNAVAILABLE}"
    if exp.get("delta_bias_incl_resting") == "UNKNOWN":
        return None, f"resting entries unknown in session_state.json; {BOOK_UNAVAILABLE}"
    if registry_error:
        return None, f"{registry_error}; {BOOK_UNAVAILABLE}"
    positions = state.get("active_positions")
    if not isinstance(positions, list) or not all(isinstance(p, dict) for p in positions):
        return None, f"session_state.json malformed (active_positions); {BOOK_UNAVAILABLE}"
    open_symbols = {str(p.get("symbol") or "").upper() for p in positions}
    for rec in records or []:
        # The sync skips a record whose symbol has a position, but Gate 1 counts a partial fill's unfilled remainder
        key = (str(rec.get("symbol") or "").upper(), str(rec.get("direction") or "").upper())
        if key[0] in open_symbols:
            return None, (f"possible partially filled resting entry {key[0]} {key[1]}: remainder not in the cache; "
                          f"{BOOK_UNAVAILABLE}")
    mismatches = exp.get("resting_mismatches", [])
    if not isinstance(mismatches, list):
        return None, f"session_state.json malformed (resting_mismatches); {BOOK_UNAVAILABLE}"
    mismatches = [m for m in mismatches if isinstance(m, dict)]
    allowed = Counter((str(e.get("symbol") or "").upper(), str(e.get("dir") or "").upper())
                      for e in listed if isinstance(e, dict))
    legs = []
    for rec in records or []:
        key = (str(rec.get("symbol") or "").upper(), str(rec.get("direction") or "").upper())
        if allowed[key] <= 0:
            # Not listed at the last sync. Dead only when placed before it and not among its resting_mismatches
            # (unmatched at the sync, e.g. an MCP algo not yet indexed); a newer one may rest and Gate 1 counts it.
            placed = _float(rec.get("placed_at_ts"))
            mismatched = any(str(m.get("entry_id")) == str(rec.get("entry_id")) if m.get("entry_id") is not None
                             else str(m.get("symbol") or "").upper() == key[0] for m in mismatches)
            if mismatched or placed is None or placed >= updated:
                return None, (f"resting entry {key[0]} {key[1]} not matched at the last sync (placed after it or "
                              f"not yet indexed); {BOOK_UNAVAILABLE}")
            continue  # older than the sync and not resting at it: filled, cancelled or a dead record
        allowed[key] -= 1
        qty, price = _float(rec.get("total_qty")), _float(rec.get("trigger_or_limit_price"))
        if qty is None or price is None:
            return None, f"registry record {key[0]} {key[1]} without a readable notional; {BOOK_UNAVAILABLE}"
        meta = rec.get("score_meta") if isinstance(rec.get("score_meta"), dict) else {}
        legs.append({"symbol": key[0], "direction": key[1], "notional": abs(qty * price),
                     "entry_id": rec.get("entry_id"), "kind": rec.get("kind"),
                     "expires_at_ts": rec.get("expires_at_ts"), "score": meta.get("score")})
    missing = sorted(k for k, n in allowed.items() if n > 0)
    if missing:  # its notional is unknown: never read as no exposure (a missing registry file included)
        return None, (f"session_state.json lists resting {missing[0][0]} {missing[0][1]} without a registry record "
                      f"(filled, cancelled or changed since the sync); {BOOK_UNAVAILABLE}")
    # Gate 1 also rejects on the state's own label (positions only), read as the executor reads it
    file_bias = exp.get("delta_bias") or state.get("portfolio_delta_bias", "NEUTRAL")
    return {"long": long_n, "short": short_n, "resting": legs, "file_bias": file_bias}, None


def estimate_notional(cand, brief, brief_generated_at_ts):
    """(notional, None) or (None, reason): min(risk_per_trade_usdt / |entry - stop_loss| x entry,
    account_equity_usdt x max_margin_ratio x leverage), or for a YOLO candidate (is_yolo true) yolo_margin_usdt x
    leverage (the executor sizes YOLO from its margin), from the brief the dossier was evaluated on only. Missing or
    non-positive inputs are UNKNOWN."""
    if not isinstance(brief, dict):
        return None, "logs/primed_brief.json missing or unreadable: no sizing inputs"
    gen, raw = _float(brief.get("generated_at_ts")), _float(brief_generated_at_ts)
    if gen is None or raw is None or int(gen) != int(raw):
        return None, "the brief was replaced or the dossier names none: no sizing inputs"
    rp = brief.get("risk_profile") if isinstance(brief.get("risk_profile"), dict) else {}
    cand = cand if isinstance(cand, dict) else {}
    lev = _float(cand.get("leverage"))
    if cand.get("is_yolo") is True or str(cand.get("is_yolo")).strip().lower() in ("true", "1", "yes"):
        margin = _float(rp.get("yolo_margin_usdt"))
        if margin is None or lev is None or margin <= 0 or lev <= 0:
            return None, "missing or non-positive yolo_margin_usdt or leverage"
        return margin * lev, None
    risk, equity, ratio = (_float(rp.get(k)) for k in ("risk_per_trade_usdt", "account_equity_usdt",
                                                        "max_margin_ratio"))
    entry, sl = _float(cand.get("entry")), _float(cand.get("stop_loss"))
    if None in (risk, equity, ratio, entry, sl, lev):
        return None, "missing risk_profile or candidate sizing fields"
    if min(risk, equity, ratio, lev) <= 0:
        return None, "non-positive risk_profile value or leverage"
    if entry <= 0 or entry == sl:
        return None, "entry equals stop_loss or is not positive"
    return min(risk / abs(entry - sl) * entry, equity * ratio * lev), None


def _blocked(long_n, short_n, is_long, notional, file_bias=None):
    """(blocked, post-trade book) by the Gate 1 rule (pre or the state's label `file_bias` heavy on that side, or a
    non-empty book tipped heavy)."""
    heavy = LONG_HEAVY if is_long else SHORT_HEAVY
    pre = book_exposure(long_n, short_n)
    post = project_order(pre, is_long, notional)
    if pre["delta_bias"] == heavy or file_bias == heavy:
        return True, post
    return (long_n + short_n > 0 and post["delta_bias"] == heavy), post


def _sides(legs):
    return (sum(l["notional"] for l in legs if l["direction"] == "LONG"),
            sum(l["notional"] for l in legs if l["direction"] == "SHORT"))


def evaluate(book, book_reason, candidates):
    """`candidates`: [{symbol, direction, notional_estimate, reason}] in dossier order. Returns {"book": {ratio, bias,
    resting} | None, "book_reason", "candidates": [{symbol, direction, notional, status, ratio, blocked_by_resting,
    blockers, blockers_together?, reason}], "cumulative": {status, ratio, bias, reason}}. Each candidate is assessed
    alone against the current book; the cumulative line adds the whole approved set."""
    out = {"book": None, "book_reason": book_reason, "candidates": [],
           "cumulative": {"status": UNKNOWN, "ratio": None, "bias": None, "reason": book_reason}}
    if book is not None:
        rest_long, rest_short = _sides(book["resting"])
        now = book_exposure(book["long"] + rest_long, book["short"] + rest_short)
        out["book"] = {"ratio": now["delta_ratio"], "bias": now["delta_bias"], "resting": len(book["resting"])}
    for c in candidates:
        row = {"symbol": c.get("symbol"), "direction": c.get("direction"), "notional": c.get("notional_estimate"),
               "status": UNKNOWN, "ratio": None, "blocked_by_resting": False, "blockers": [],
               "reason": c.get("reason")}
        out["candidates"].append(row)
        direction = str(c.get("direction") or "").upper()
        if book is None:
            row["reason"] = book_reason
            continue
        if direction not in ("LONG", "SHORT"):
            row["reason"] = "unknown direction"
            continue
        if row["notional"] is None:
            continue
        is_long, legs, label = direction == "LONG", book["resting"], book.get("file_bias")
        rest_long, rest_short = _sides(legs)
        blocked, post = _blocked(book["long"] + rest_long, book["short"] + rest_short, is_long, row["notional"], label)
        row.update(status=BLOCKED if blocked else FITS, ratio=post["delta_ratio"], reason=None)
        if blocked and legs and not _blocked(book["long"], book["short"], is_long, row["notional"], label)[0]:
            row["blocked_by_resting"] = True
            singles = []
            for i in range(len(legs)):
                rl, rs = _sides(legs[:i] + legs[i + 1:])
                if not _blocked(book["long"] + rl, book["short"] + rs, is_long, row["notional"], label)[0]:
                    singles.append(legs[i])
            row["blockers"] = singles or list(legs)  # no single one suffices: they block together
            row["blockers_together"] = not singles
    if book is not None:
        unknown = next((r for r in out["candidates"] if r["status"] == UNKNOWN), None)
        if unknown is not None:
            out["cumulative"]["reason"] = f"{unknown['symbol']} {unknown['direction']}: {unknown['reason']}"
        elif out["candidates"]:
            rest_long, rest_short = _sides(book["resting"])
            add_long = sum(r["notional"] for r in out["candidates"] if str(r["direction"]).upper() == "LONG")
            add_short = sum(r["notional"] for r in out["candidates"] if str(r["direction"]).upper() == "SHORT")
            total = book_exposure(book["long"] + rest_long + add_long, book["short"] + rest_short + add_short)
            out["cumulative"] = {"status": "ok", "ratio": total["delta_ratio"], "bias": total["delta_bias"],
                                 "reason": None}
    return out


def _utc(ts):
    try:
        return datetime.datetime.fromtimestamp(int(float(ts)), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def format_candidate(row, env="prod"):
    """The candidate's lines: the `Delta (est.): ...` line, then one `swap: ...` line per resting blocker (with
    `--env <env>` when env is not prod)."""
    if row["status"] == UNKNOWN:
        return [f"Delta (est.): UNKNOWN ({row['reason']})"]
    size = f"~{row['notional']:.0f} USDT"
    if row["status"] == FITS:
        return [f"Delta (est.): fits (ratio {row['ratio']:+.2f}, {size})"]
    if not row["blocked_by_resting"]:
        heavy = LONG_HEAVY if str(row["direction"]).upper() == "LONG" else SHORT_HEAVY
        return [f"Delta (est.): BLOCKED by delta (ratio {row['ratio']:+.2f}, {size}): {heavy} even without the "
                "resting entries"]
    named = ", ".join(f"{b['symbol']} {b['direction']} ~{b['notional']:.0f} USDT (entry {b['entry_id']}, expires "
                      f"{_utc(b['expires_at_ts'])}" + (f", score {b['score']}" if b.get("score") is not None else "")
                      + ")" for b in row["blockers"])
    lines = [f"Delta (est.): BLOCKED by delta (ratio {row['ratio']:+.2f}, {size}): resting {named}"]
    env_arg = "" if str(env or "prod").lower() == "prod" else f" --env {str(env).lower()}"
    for b in row["blockers"]:
        sym, eid = str(b["symbol"]), str(b["entry_id"])
        if _SAFE_SYMBOL.match(sym) and _SAFE_ID.match(eid) and (not env_arg or _SAFE_ID.match(str(env))):
            lines.append(f"swap: python3 scripts/execute_futures_trade.py --cancel-pending --symbol {sym} "
                         f"--entry-id {eid}{env_arg}" + (" (cancel together)" if row.get("blockers_together") else ""))
    return lines


def format_summary(result, n_candidates):
    """The cumulative line of the approved set (with the estimate note)."""
    cum, book = result["cumulative"], result["book"]
    if book is None or cum["status"] == UNKNOWN:
        return f"Delta (est.) of the approved set: UNKNOWN ({cum['reason']})"
    return (f"Delta (est.) of the approved set: book now {book['ratio']:+.2f} (positions + {book['resting']} "
            f"resting), after all {n_candidates} {cum['ratio']:+.2f} ({cum['bias']}); {ESTIMATE_NOTE}")
