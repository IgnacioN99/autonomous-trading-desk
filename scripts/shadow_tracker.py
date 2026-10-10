#!/usr/bin/env python3
"""
shadow_tracker.py - Counterfactual Shadow Trading & Filter Efficacy Auditor.
Deterministic tracking of unexecuted/rejected setups to eliminate selection bias.

Monitors discarded or disqualified candidates against live market klines:
1. Simulates conditional trigger execution (Pending -> Active).
2. Audits whether price reaches Stop Loss (True Negative / Capital Saved) or Take Profit (False Negative / Missed Alpha).
3. Computes Filter Efficacy Ratio (FER): TN / (TN + FN).
4. Persists results to logs/shadow_trades.jsonl and logs/shadow_resolved.jsonl without risking capital or consuming LLM tokens.

Rejection reason (issue #251): the dossier may carry `rejected_candidates: [{symbol, direction, score, gate, detail}]`
(gate in GATE_ENUM). A candidate with a typed entry gets gate_source "dossier"; without one the vol_ratio heuristic
stays as a flagged fallback (gate_source "heuristic_vol_ratio"). Malformed entries are dropped or mapped to OTHER and
flagged (normalize_rejected_candidates), never refused. Rows also carry score, dossier_sha256 (one registration per
dossier_sha256 + symbol + direction, resolved rows included, and, issue #262, one per symbol + direction + gate within
DEDUPE_WINDOW_SECONDS across dossiers, counted as deduped_window) and, for DELTA_GATE / DUPLICATE_RESTING, a best-effort
snapshot of the blocking positions / resting entries ("blockers") and of the whole book ("book") read from
logs/pending_entries.json, logs/session_state.json and logs/trades_audit.jsonl. Report-only: nothing here gates an
order. Regret and the policy replay are computed in scripts/shadow_analytics.py.

Hook denials (issue #261): when pre_trade_guard.py denies a dossier-approved candidate on the Delta-Neutral gate it
appends one event to logs/gate_denials.jsonl (its own ground-truth log: dossier prices / score / sha and the cached
book, source session_state_cache). register_from_gate_denials (run first by --register-from-eval, i.e. at every
record_evaluation.py) turns recent events into DELTA_GATE_POST_APPROVAL rows (gate_source "hook_denial", blockers
and book via book_from_sources, registered_at_ts = the denial time), once per dossier_sha256 + symbol + direction.
Denials by the executor's own Gate 1 (live book) are not recorded.

Usage:
  python3 scripts/shadow_tracker.py --register-from-eval
  python3 scripts/shadow_tracker.py --audit
  python3 scripts/shadow_tracker.py --loop --interval 300
"""

import os
import sys
import json
import time
import datetime
import argparse
import collections
import urllib.request
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.atomic_writer import atomic_write_json, atomic_append_jsonl
# Shared with shadow_analytics.py (issue #290), re-exported here: st.GATE_ENUM, st.row_gate, ... keep working
from utils.shadow_common import (  # noqa: F401
    GATE_ENUM, GATE_FALLBACK, POST_APPROVAL_GATE, BLOCKER_GATES, DEDUPE_WINDOW_SECONDS, row_gate)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
SHADOW_TRADES_FILE = os.path.join(LOGS_DIR, "shadow_trades.jsonl")
SHADOW_RESOLVED_FILE = os.path.join(LOGS_DIR, "shadow_resolved.jsonl")
DOSSIER_FILE = os.path.join(LOGS_DIR, "evaluations", "latest_dossier.json")  # newest scan on purpose (#270)
BRIEF_FILE = os.path.join(LOGS_DIR, "primed_brief.json")
PENDING_ENTRIES_FILE = os.path.join(LOGS_DIR, "pending_entries.json")
SESSION_STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
TRADES_AUDIT_FILE = os.path.join(LOGS_DIR, "trades_audit.jsonl")
GATE_DENIALS_FILE = os.path.join(LOGS_DIR, "gate_denials.jsonl")

# GATE_ENUM, GATE_FALLBACK, POST_APPROVAL_GATE, BLOCKER_GATES, DEDUPE_WINDOW_SECONDS: utils/shadow_common.py
GATE_DETAIL_MAX_CHARS = 300
GATE_DENIAL_MAX_AGE_SECONDS = 86400  # older events would expire at once in the kline audit (24 h)
GATE_DENIAL_TAIL_LINES = 500

# Intraday Desk Constraints & Statistical Hygiene
MAX_TRIGGER_WAIT_SECONDS = 5400    # 90 min max to breach trigger (matches limit cancellation rule)
MAX_INTRADAY_HOLD_SECONDS = 14400  # 4 hours max intraday holding duration (matches Dead Alpha watchdog)
ROLLING_WINDOW_SIZE = 30           # Sample size for rolling FER calculation

def load_jsonl(filepath: str) -> List[dict]:
    if not os.path.exists(filepath):
        return []
    items = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
    return items

def rewrite_jsonl(filepath: str, items: List[dict]):
    temp_path = filepath + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(temp_path, filepath)

def fetch_klines(symbol: str, start_time_ms: int, interval: str = "5m", limit: int = 500) -> List[list]:
    """Fetches public klines from Binance USDⓈ-M Futures."""
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&startTime={start_time_ms}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ShadowTracker/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        # Fallback to testnet if symbol only exists in testnet
        try:
            url_testnet = f"https://testnet.binancefuture.com/fapi/v1/klines?symbol={symbol}&interval={interval}&startTime={start_time_ms}&limit={limit}"
            req_t = urllib.request.Request(url_testnet, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req_t, timeout=8) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return []

def register_shadow_trade(
    symbol: str,
    direction: str,
    trigger_price: float,
    sl_price: float,
    tp1_price: float,
    tp2_price: float,
    current_price: float,
    vol_ratio: float = 1.0,
    rejection_reason: str = "Manual rejection",
    rejection_category: str = "GENERIC_FILTER",
    target_dollar_risk: float = 1.50,
    gate: Optional[str] = None,
    gate_detail: Optional[str] = None,
    gate_source: Optional[str] = None,
    score: Optional[float] = None,
    dossier_sha256: Optional[str] = None,
    extra: Optional[dict] = None,
    registered_at_ts: Optional[int] = None,
    stats: Optional[dict] = None,
    dedupe_ref_ts: Optional[float] = None
) -> Optional[dict]:
    """Registers a rejected setup into shadow_trades.jsonl if not already active.
    With dossier_sha256: at most one row per (dossier_sha256, symbol, direction), also after it resolved (issue #251),
    and none when a row (pending or resolved, any dossier) with the same (symbol, direction, gate) was registered
    within DEDUPE_WINDOW_SECONDS of this one (issue #262: a candidate re-rejected by every new dossier counts once;
    counted in stats["deduped_window"] when stats is given; the window is measured from dedupe_ref_ts, the
    rejection's own time such as the dossier's, so a rerun on the same dossier after the window stays deduped, else
    from the registration time); without it: no second active/pending row for the symbol within 60 minutes. extra:
    additional row keys (blockers). registered_at_ts: the rejection's own time when it is registered later (issue
    #261: the kline audit starts there), else now."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    existing = load_jsonl(SHADOW_TRADES_FILE)

    now_ts = int(registered_at_ts) if registered_at_ts else int(time.time())
    if dossier_sha256:
        sym, dir_ = symbol.upper().strip(), direction.upper().strip()
        key = (dossier_sha256, sym, dir_)
        rows = existing + load_jsonl(SHADOW_RESOLVED_FILE)
        for t in rows:
            if (t.get("dossier_sha256"), t.get("symbol"), str(t.get("direction") or "").upper()) == key:
                return None
        row_gate_value = row_gate({"gate": gate, "rejection_category": rejection_category})[0]
        ref_ts = _to_float(dedupe_ref_ts)
        ref_ts = ref_ts if ref_ts is not None and ref_ts > 0 else now_ts
        for t in rows:
            if not isinstance(t, dict) or t.get("symbol") != sym or str(t.get("direction") or "").upper() != dir_:
                continue
            ts = _to_float(t.get("registered_at_ts"))
            if row_gate(t)[0] == row_gate_value and ts is not None and abs(ref_ts - ts) < DEDUPE_WINDOW_SECONDS:
                if stats is not None:
                    stats["deduped_window"] = stats.get("deduped_window", 0) + 1
                return None
    else:
        # Avoid duplicate active/pending trade for same symbol registered in last 60 minutes
        for t in existing:
            if t.get("symbol") == symbol and t.get("status") in ["PENDING_TRIGGER", "ACTIVE"]:
                if now_ts - t.get("registered_at_ts", 0) < 3600:
                    return None

    now_utc = datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    trade_id = f"shadow_{symbol}_{now_ts}"

    trade = {
        "id": trade_id,
        "symbol": symbol.upper().strip(),
        "direction": direction.upper().strip(),
        "registered_at_utc": now_utc,
        "registered_at_ts": now_ts,
        "current_price_at_eval": float(current_price),
        "trigger_price": float(trigger_price),
        "sl_price": float(sl_price),
        "tp1_price": float(tp1_price),
        "tp2_price": float(tp2_price),
        "vol_ratio": float(vol_ratio),
        "rejection_reason": rejection_reason,
        "rejection_category": rejection_category,
        "gate": gate,
        "gate_detail": gate_detail,
        "gate_source": gate_source,
        "score": score,
        "dossier_sha256": dossier_sha256,
        "target_dollar_risk": float(target_dollar_risk),
        "status": "PENDING_TRIGGER",
        "activated_at_utc": None,
        "activated_at_ts": None,
        "resolved_at_utc": None,
        "resolved_at_ts": None,
        "outcome": None,
        "classification": None,
        "simulated_pnl_usdt": 0.0,
        "max_favorable_excursion_pct": 0.0,
        "max_adverse_excursion_pct": 0.0,
        "highest_price": float(current_price),
        "lowest_price": float(current_price),
        "last_checked_price": float(current_price),
        "last_checked_ts": now_ts
    }
    for k, v in (extra or {}).items():
        trade.setdefault(k, v)

    atomic_append_jsonl(SHADOW_TRADES_FILE, trade)
    return trade


def _to_float(value) -> Optional[float]:
    """float(value) for a finite number (never a bool), else None."""
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def normalize_rejected_candidates(raw) -> tuple:
    """(entries, flags) of the dossier's optional rejected_candidates (issue #251). Never raises and never refuses:
    a non-dict entry or one without a symbol is dropped (flagged), an unknown / missing gate becomes GATE_FALLBACK
    (flagged, original kept in gate_raw), a non-numeric score becomes None, a direction other than LONG / SHORT
    becomes None (the entry then matches by symbol only). Each entry: {symbol, direction, score, gate, detail, flags}."""
    flags: List[str] = []
    try:
        if raw is None:
            return [], flags
        if not isinstance(raw, list):
            return [], [f"rejected_candidates_not_a_list:{type(raw).__name__}"]
        out = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                flags.append(f"entry_{i}_not_a_dict")
                continue
            sym = item.get("symbol")
            if not isinstance(sym, str) or not sym.strip():
                flags.append(f"entry_{i}_without_symbol")
                continue
            entry_flags = []
            direction = str(item.get("direction") or "").upper().strip()
            if direction not in ("LONG", "SHORT"):
                entry_flags.append("invalid_direction")
                direction = None
            gate_raw = item.get("gate")
            gate = str(gate_raw).upper().strip() if isinstance(gate_raw, str) else None
            entry = {"symbol": sym.upper().strip(), "direction": direction}
            if gate not in GATE_ENUM:
                entry_flags.append("missing_gate" if gate_raw is None else "unknown_gate")
                entry["gate_raw"] = None if gate_raw is None else str(gate_raw)[:60]
                gate = GATE_FALLBACK
            score = _to_float(item.get("score"))
            if score is None and item.get("score") is not None:
                entry_flags.append("invalid_score")
            detail = item.get("detail")
            entry.update(score=score, gate=gate,
                         detail=detail.strip()[:GATE_DETAIL_MAX_CHARS] if isinstance(detail, str) else "",
                         flags=entry_flags)
            out.append(entry)
        return out, flags
    except Exception as e:  # never refuse a dossier over its optional field
        return [], flags + [f"normalize_error:{type(e).__name__}"]


def _env_name(value) -> str:
    v = str(value or "").strip().lower()
    return "prod" if v == "mainnet" else v


def _latest_audit_by_symbol(path: str, until_ts: Optional[float] = None) -> Dict[str, dict]:
    """Latest non-event entry record (with total_qty) per symbol of logs/trades_audit.jsonl (missing file: {});
    with until_ts, records timestamped later are ignored (a later trade is not the blocker of an older event)."""
    latest: Dict[str, dict] = {}
    for rec in load_jsonl(path):
        if isinstance(rec, dict) and not rec.get("event") and "total_qty" in rec and rec.get("symbol"):
            ts = _to_float(rec.get("timestamp"))
            if until_ts is not None and ts is not None and ts > until_ts:
                continue
            latest[str(rec["symbol"]).upper()] = rec
    return latest


def book_from_sources(state: dict, entries: dict, audit: Dict[str, dict], target_env: Optional[str] = None) -> list:
    """Book items (pure, no file reads; issue #261 builds the hook's denial book with it): active_positions of the session state and the
    resting entries of the registry's "entries" dict (of target_env when given; a symbol with an open position counts
    as the position, as in sync_session_state), each as {symbol, direction, kind: "position" | "resting", score,
    entry_id, notional, since_ts, audit_ts}. Resting score: score_meta.score (else dossier_score); position score /
    audit_ts: audit[symbol] (the latest trades_audit.jsonl entry record) in the same direction, else None."""
    items = []
    open_symbols = set()
    for p in state.get("active_positions") or []:
        if not isinstance(p, dict) or not p.get("symbol"):
            continue
        sym = str(p["symbol"]).upper()
        direction = str(p.get("direction") or "").upper()
        open_symbols.add(sym)
        rec = audit.get(sym)
        if rec is not None and str(rec.get("direction") or "").upper() != direction:
            rec = None
        items.append({"symbol": sym, "direction": direction, "kind": "position",
                      "score": _to_float(rec.get("score")) if rec else None,
                      "entry_id": None if p.get("entry_order_id") is None else str(p.get("entry_order_id")),
                      "notional": _to_float(p.get("notional_usdt")),
                      "since_ts": _to_float(p.get("entry_time_ts")),
                      "audit_ts": _to_float(rec.get("timestamp")) if rec else None})
    for rec in entries.values():
        if not isinstance(rec, dict) or not rec.get("symbol"):
            continue
        if target_env and rec.get("target_env") and _env_name(rec.get("target_env")) != _env_name(target_env):
            continue
        sym = str(rec["symbol"]).upper()
        if sym in open_symbols:
            continue
        meta = rec.get("score_meta") if isinstance(rec.get("score_meta"), dict) else {}
        score = _to_float(meta.get("score"))
        price, qty = _to_float(rec.get("trigger_or_limit_price")), _to_float(rec.get("total_qty"))
        items.append({"symbol": sym, "direction": str(rec.get("direction") or "").upper(), "kind": "resting",
                      "score": score if score is not None else _to_float(meta.get("dossier_score")),
                      "entry_id": None if rec.get("entry_id") is None else str(rec.get("entry_id")),
                      "notional": abs(price * qty) if price is not None and qty is not None else None,
                      "since_ts": _to_float(rec.get("placed_at_ts")), "audit_ts": None})
    return items


def snapshot_book(target_env: Optional[str] = None) -> dict:
    """Best-effort snapshot of the book at registration time (issue #251), read-only: book_from_sources over
    logs/session_state.json, logs/pending_entries.json and logs/trades_audit.jsonl. A missing file is an empty source.
    Any read error: {"items": [], "error": "..."}; never raises."""
    try:
        state = {}
        if os.path.exists(SESSION_STATE_FILE):
            with open(SESSION_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError("session_state malformed")
        state_env = _env_name(state.get("target_env"))
        if target_env and state and state_env and state_env != _env_name(target_env):
            raise ValueError(f"session_state env {state_env} != dossier env {_env_name(target_env)}")
        entries = {}
        if os.path.exists(PENDING_ENTRIES_FILE):
            with open(PENDING_ENTRIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
                raise ValueError("pending entries registry malformed")
            entries = data["entries"]
        items = book_from_sources(state, entries, _latest_audit_by_symbol(TRADES_AUDIT_FILE), target_env)
        return {"items": items, "error": None, "session_state_ts": _to_float(state.get("last_updated_ts"))}
    except Exception as e:
        return {"items": [], "error": f"{type(e).__name__}: {e}"[:200], "session_state_ts": None}


def blockers_for(gate: str, symbol: str, direction: str, book: dict) -> List[dict]:
    """The snapshot items that block a candidate: DELTA_GATE / DELTA_GATE_POST_APPROVAL -> same direction;
    DUPLICATE_RESTING -> same symbol."""
    items = book.get("items") or []
    if gate in ("DELTA_GATE", POST_APPROVAL_GATE):
        return [dict(i) for i in items if i.get("direction") == direction]
    if gate == "DUPLICATE_RESTING":
        return [dict(i) for i in items if i.get("symbol") == symbol]
    return []


def _trimmed_book(items: list) -> list:
    return [{k: i.get(k) for k in ("symbol", "direction", "kind", "score", "notional", "since_ts", "entry_id")}
            for i in items]


def _register_gate_denial(ev, now_ts: int, seen: set, stats: Optional[dict] = None) -> Optional[dict]:
    """One logs/gate_denials.jsonl event -> a DELTA_GATE_POST_APPROVAL shadow row (None when skipped)."""
    if not isinstance(ev, dict) or ev.get("gate") != POST_APPROVAL_GATE:
        return None
    ts = _to_float(ev.get("ts"))
    if ts is None or ts > now_ts + 300 or now_ts - ts > GATE_DENIAL_MAX_AGE_SECONDS:
        return None
    sym = str(ev.get("symbol") or "").upper().strip()
    direction = str(ev.get("direction") or "").upper().strip()
    prices = [_to_float(ev.get(k)) for k in ("entry", "stop_loss", "tp1", "tp2")]
    if not sym or direction not in ("LONG", "SHORT") or any(p is None or p <= 0 for p in prices):
        return None
    sha = ev.get("dossier_sha256") if isinstance(ev.get("dossier_sha256"), str) and ev.get("dossier_sha256") else None
    if sha and (sha, sym, direction) in seen:
        return None
    env = ev.get("env") if isinstance(ev.get("env"), str) else None
    raw = [i for i in ev.get("book") or [] if isinstance(i, dict)] if isinstance(ev.get("book"), list) else []
    state = {"active_positions": [i for i in raw if i.get("kind") == "position"]}
    entries = {str(n): i for n, i in enumerate(i for i in raw if i.get("kind") == "resting")}
    book = {"items": book_from_sources(state, entries, _latest_audit_by_symbol(TRADES_AUDIT_FILE, until_ts=ts), env)}
    net = _to_float(ev.get("net_notional_delta_usdt"))
    detail = (f"hook denied {sym} {direction} after approval: delta_bias {ev.get('delta_bias')} (cached net delta "
              f"{'?' if net is None else f'{net:+.2f}'} USDT, session_state age {ev.get('age_seconds')}s)")
    extra = {"blockers": blockers_for(POST_APPROVAL_GATE, sym, direction, book),
             "book": _trimmed_book(book["items"]),
             "book_session_state_ts": _to_float(ev.get("session_state_ts")),
             "book_source": str(ev.get("source") or "session_state_cache"),
             "gate_event_env": env, "delta_bias_at_denial": ev.get("delta_bias"),
             "net_notional_delta_usdt_at_denial": net, "session_state_age_seconds": _to_float(ev.get("age_seconds")),
             "tier": ev.get("tier"), "is_yolo": ev.get("is_yolo")}
    if ev.get("book_truncated"):
        extra["book_truncated"] = True
    if ev.get("book_error"):
        extra["blockers_error"] = str(ev["book_error"])[:200]
    entry, sl, tp1, tp2 = prices
    row = register_shadow_trade(
        symbol=sym, direction=direction, trigger_price=entry, sl_price=sl, tp1_price=tp1, tp2_price=tp2,
        current_price=entry, rejection_reason=f"[{POST_APPROVAL_GATE}] {detail}"[:GATE_DETAIL_MAX_CHARS],
        rejection_category=POST_APPROVAL_GATE, gate=POST_APPROVAL_GATE, gate_detail=detail[:GATE_DETAIL_MAX_CHARS],
        gate_source="hook_denial", score=_to_float(ev.get("score")), dossier_sha256=sha, extra=extra,
        registered_at_ts=int(ts), stats=stats)
    if row and sha:
        seen.add((sha, sym, direction))
    return row


def register_from_gate_denials(now_ts: Optional[int] = None, stats: Optional[dict] = None) -> int:
    """Registers the hook's delta-gate denials of approved candidates (logs/gate_denials.jsonl, issue #261) as
    DELTA_GATE_POST_APPROVAL shadow rows: gate_source "hook_denial", blockers and book from the event's cached book
    (book_from_sources; position scores from trades_audit records not newer than the event), registered_at_ts = the
    denial time and trigger = current price = the dossier entry. Reads only the last GATE_DENIAL_TAIL_LINES lines and
    events of the last GATE_DENIAL_MAX_AGE_SECONDS; skips events without valid dossier prices. Idempotent through
    register_shadow_trade's dedupe (stats: see there). A missing or garbled file registers nothing; never raises."""
    try:
        if not os.path.exists(GATE_DENIALS_FILE):
            return 0
        with open(GATE_DENIALS_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = list(collections.deque(f, maxlen=GATE_DENIAL_TAIL_LINES))
        now = int(time.time()) if now_ts is None else int(now_ts)
        seen = {(t.get("dossier_sha256"), t.get("symbol"), str(t.get("direction") or "").upper())
                for t in load_jsonl(SHADOW_TRADES_FILE) + load_jsonl(SHADOW_RESOLVED_FILE)
                if isinstance(t, dict) and t.get("dossier_sha256")}
    except Exception:
        return 0
    count = 0
    for line in lines:
        try:
            if _register_gate_denial(json.loads(line), now, seen, stats):
                count += 1
        except Exception:
            continue
    return count

def register_from_evaluation(stats: Optional[dict] = None) -> int:
    """Reads latest evaluation brief and dossier, auto-registering rejected setups, and first the hook's recent
    delta-gate denials of approved candidates (register_from_gate_denials, issue #261; also without a brief).
    stats: optional dict that receives the "deduped_window" count (issue #262, register_shadow_trade)."""
    registered_count = register_from_gate_denials(stats=stats)

    # 1. Read candidates from primed brief
    brief_data = {}
    if os.path.exists(BRIEF_FILE):
        try:
            with open(BRIEF_FILE, "r", encoding="utf-8") as f:
                brief_data = json.load(f)
        except Exception:
            pass

    opps = brief_data.get("filtered_opportunities", [])
    if not opps:
        return registered_count

    # 2. Check latest dossier to see which were rejected or if all were rejected
    dossier_data = {}
    if os.path.exists(DOSSIER_FILE):
        try:
            with open(DOSSIER_FILE, "r", encoding="utf-8") as f:
                dossier_data = json.load(f)
        except Exception:
            pass

    approved_symbols = set(dossier_data.get("approved_symbols", []))
    dossier_status = dossier_data.get("status", "").upper()

    # Issue #251: typed rejection reasons (optional; malformed entries are flagged, never refused)
    raw_payload = dossier_data.get("raw_payload") if isinstance(dossier_data.get("raw_payload"), dict) else {}
    provenance = dossier_data.get("provenance") if isinstance(dossier_data.get("provenance"), dict) else {}
    dossier_sha256 = provenance.get("sha256") if isinstance(provenance.get("sha256"), str) else None
    typed, typed_flags = normalize_rejected_candidates(raw_payload.get("rejected_candidates"))
    typed_by_key = {}
    for c in typed:
        typed_by_key.setdefault((c["symbol"], c["direction"]), c)
    target_env = raw_payload.get("target_env") or dossier_data.get("target_env")
    # Issue #262: the dedupe window is measured from the dossier's own time, so a rerun on a deduped dossier stays
    # deduped after the window (the row itself is still registered now)
    dossier_ts = _to_float(dossier_data.get("timestamp_ts")) or _to_float(dossier_data.get("recorded_at_ts"))
    book = None

    for o in opps:
        sym = o.get("symbol", "").upper()
        # If dossier rejected all or sym not approved, register for shadow tracking
        if dossier_status == "REJECTED" or sym not in approved_symbols:
            # Determine rejection category
            vol = float(o.get("vol_ratio", 1.0))
            if vol < 1.0:
                cat = "DRY_VOLUME_FAKE_TIER_S"
                reason = f"Fake Tier S: vol_ratio {vol:.1f}x < 1.0x"
            else:
                cat = "DELTA_GATE_OR_MACRO"
                reason = "Rejected by delta gate or macro regime"

            direction = str(o.get("direction", "LONG")).upper().strip()
            tc = typed_by_key.get((sym, direction)) or typed_by_key.get((sym, None))
            extra = {}
            if tc is not None:
                gate, gate_source, gate_detail = tc["gate"], "dossier", tc["detail"]
                reason = f"[{gate}] {gate_detail}".strip()
                score = tc["score"] if tc["score"] is not None else _to_float(o.get("confidence"))
                flags = tc["flags"] + typed_flags
            else:
                # Fallback for dossiers without a typed entry for this candidate (flagged by gate_source)
                gate = "DRY_VOLUME" if vol < 1.0 else GATE_FALLBACK
                gate_source, gate_detail = "heuristic_vol_ratio", reason
                score = _to_float(o.get("confidence"))
                flags = typed_flags
            if flags:
                extra["gate_flags"] = flags
            if _to_float(o.get("notional_usdt")):
                extra["notional_usdt"] = _to_float(o.get("notional_usdt"))
            if gate in BLOCKER_GATES:
                if book is None:
                    book = snapshot_book(target_env)
                extra["blockers"] = blockers_for(gate, sym, direction, book)
                extra["book"] = _trimmed_book(book.get("items") or [])
                extra["book_session_state_ts"] = book.get("session_state_ts")
                if not target_env:
                    extra["book_env_unfiltered"] = True  # no dossier env: resting entries of every env included
                if book.get("error"):
                    extra["blockers_error"] = book["error"]

            res = register_shadow_trade(
                symbol=sym,
                direction=direction,
                trigger_price=float(o.get("trigger_price", o.get("current_price", 0))),
                sl_price=float(o.get("sl_price", 0)),
                tp1_price=float(o.get("tp1_price", 0)),
                tp2_price=float(o.get("tp2_price", 0)),
                current_price=float(o.get("current_price", 0)),
                vol_ratio=vol,
                rejection_reason=reason,
                rejection_category=cat,
                target_dollar_risk=float(o.get("target_dollar_risk", 1.50)),
                gate=gate,
                gate_detail=gate_detail,
                gate_source=gate_source,
                score=score,
                dossier_sha256=dossier_sha256,
                extra=extra,
                stats=stats,
                dedupe_ref_ts=dossier_ts
            )
            if res:
                registered_count += 1

    return registered_count

def audit_shadow_trades() -> dict:
    """
    Audits all active and pending shadow trades against live klines.
    Moves resolved trades to shadow_resolved.jsonl.
    """
    trades = load_jsonl(SHADOW_TRADES_FILE)
    if not trades:
        return {"active": 0, "resolved_new": 0, "total_resolved": len(load_jsonl(SHADOW_RESOLVED_FILE))}

    now_ts = int(time.time())
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    updated_trades = []
    newly_resolved = []

    for t in trades:
        if t.get("status") == "RESOLVED":
            continue

        sym = t["symbol"]
        direction = t["direction"].upper()
        trigger_p = t["trigger_price"]
        sl_p = t["sl_price"]
        tp1_p = t["tp1_price"]
        tp2_p = t["tp2_price"]
        risk_dollar = t.get("target_dollar_risk", 1.50)

        # Start from registration timestamp
        start_ms = (t.get("registered_at_ts", now_ts) - 300) * 1000
        klines = fetch_klines(sym, start_ms, interval="5m", limit=300)
        if not klines:
            updated_trades.append(t)
            continue

        highest_p = t.get("highest_price", trigger_p)
        lowest_p = t.get("lowest_price", trigger_p)
        status = t.get("status", "PENDING_TRIGGER")
        outcome = None
        classification = None
        simulated_pnl = 0.0

        for k in klines:
            k_open_time = int(k[0]) // 1000
            k_high = float(k[2])
            k_low = float(k[3])
            k_close = float(k[4])

            # 1. State: PENDING_TRIGGER
            if status == "PENDING_TRIGGER":
                reg_ts = t.get("registered_at_ts", now_ts)
                if (k_open_time - reg_ts) > MAX_TRIGGER_WAIT_SECONDS:
                    status = "RESOLVED"
                    outcome = "EXPIRED_UNTRIGGERED"
                    classification = "EXPIRED"
                    simulated_pnl = 0.0
                    break
                elif direction == "LONG" and k_high >= trigger_p:
                    status = "ACTIVE"
                    t["activated_at_ts"] = k_open_time
                    t["activated_at_utc"] = datetime.datetime.fromtimestamp(k_open_time, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    highest_p = trigger_p
                    lowest_p = trigger_p
                elif direction == "SHORT" and k_low <= trigger_p:
                    status = "ACTIVE"
                    t["activated_at_ts"] = k_open_time
                    t["activated_at_utc"] = datetime.datetime.fromtimestamp(k_open_time, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    highest_p = trigger_p
                    lowest_p = trigger_p

            # 2. State: ACTIVE
            if status == "ACTIVE":
                highest_p = max(highest_p, k_high)
                lowest_p = min(lowest_p, k_low)

                # Check SL hit
                if direction == "LONG" and k_low <= sl_p:
                    status = "RESOLVED"
                    outcome = "STOP_LOSS_HIT"
                    classification = "TRUE_NEGATIVE"
                    simulated_pnl = -risk_dollar
                    break
                elif direction == "SHORT" and k_high >= sl_p:
                    status = "RESOLVED"
                    outcome = "STOP_LOSS_HIT"
                    classification = "TRUE_NEGATIVE"
                    simulated_pnl = -risk_dollar
                    break

                # Check TP1 hit
                if direction == "LONG" and k_high >= tp1_p:
                    status = "RESOLVED"
                    outcome = "TP1_HIT"
                    classification = "FALSE_NEGATIVE"
                    simulated_pnl = risk_dollar * 1.8
                    break
                elif direction == "SHORT" and k_low <= tp1_p:
                    status = "RESOLVED"
                    outcome = "TP1_HIT"
                    classification = "FALSE_NEGATIVE"
                    simulated_pnl = risk_dollar * 1.8
                    break

                # Check Intraday Dead Alpha Timeout (Max 4.0 Hours Holding Horizon)
                act_ts = t.get("activated_at_ts", k_open_time)
                if (k_open_time - act_ts) >= MAX_INTRADAY_HOLD_SECONDS:
                    status = "RESOLVED"
                    ret_pct = ((k_close - trigger_p) / trigger_p) if direction == "LONG" else ((trigger_p - k_close) / trigger_p)
                    d_sl = abs(trigger_p - sl_p) / trigger_p if trigger_p > 0 else 0.02
                    est_notional = (risk_dollar / d_sl) if d_sl > 0 else (risk_dollar * 20.0)
                    sim_pnl = round(est_notional * ret_pct, 2)
                    sim_pnl = max(-risk_dollar, min(risk_dollar * 1.8, sim_pnl))

                    outcome = "INTRADAY_TIMEOUT_PROFIT" if sim_pnl >= 0 else "INTRADAY_TIMEOUT_LOSS"
                    classification = "TIMEOUT_CLOSED"
                    simulated_pnl = sim_pnl
                    break

        # Fallback Check Expiration (>24 hours)
        if status in ["PENDING_TRIGGER", "ACTIVE"] and (now_ts - t.get("registered_at_ts", now_ts)) > 86400:
            status = "RESOLVED"
            outcome = "EXPIRED"
            classification = "EXPIRED"
            simulated_pnl = 0.0

        # Calculate MFE & MAE
        if trigger_p > 0:
            if direction == "LONG":
                mfe = ((highest_p - trigger_p) / trigger_p) * 100
                mae = ((lowest_p - trigger_p) / trigger_p) * 100
            else:
                mfe = ((trigger_p - lowest_p) / trigger_p) * 100
                mae = ((trigger_p - highest_p) / trigger_p) * 100
        else:
            mfe, mae = 0.0, 0.0

        t["highest_price"] = highest_p
        t["lowest_price"] = lowest_p
        t["max_favorable_excursion_pct"] = round(mfe, 2)
        t["max_adverse_excursion_pct"] = round(mae, 2)
        t["last_checked_price"] = float(klines[-1][4])
        t["last_checked_ts"] = now_ts
        t["status"] = status

        if status == "RESOLVED":
            t["resolved_at_utc"] = now_utc
            t["resolved_at_ts"] = now_ts
            t["outcome"] = outcome
            t["classification"] = classification
            t["simulated_pnl_usdt"] = round(simulated_pnl, 2)
            newly_resolved.append(t)
            atomic_append_jsonl(SHADOW_RESOLVED_FILE, t)
        else:
            updated_trades.append(t)

    # Rewrite shadow_trades.jsonl with remaining unresolved trades
    rewrite_jsonl(SHADOW_TRADES_FILE, updated_trades)

    return {
        "active_remaining": len(updated_trades),
        "newly_resolved": len(newly_resolved),
        "total_resolved": len(load_jsonl(SHADOW_RESOLVED_FILE))
    }

def calculate_efficacy_metrics(rolling_window: int = ROLLING_WINDOW_SIZE) -> dict:
    """Calculates Filter Efficacy Ratio (FER) all-time, clean intraday (<=4h), and rolling window."""
    resolved = load_jsonl(SHADOW_RESOLVED_FILE)
    active = load_jsonl(SHADOW_TRADES_FILE)

    # 1. All-time global metrics
    tn_count = sum(1 for r in resolved if r.get("classification") == "TRUE_NEGATIVE")
    fn_count = sum(1 for r in resolved if r.get("classification") == "FALSE_NEGATIVE")
    expired_count = sum(1 for r in resolved if r.get("classification") in ["EXPIRED", "EXPIRED_UNTRIGGERED"])
    timeout_count = sum(1 for r in resolved if r.get("classification") == "TIMEOUT_CLOSED")

    total_conclusive = tn_count + fn_count
    fer = (tn_count / total_conclusive * 100) if total_conclusive > 0 else 0.0

    capital_saved_usdt = sum(abs(r.get("simulated_pnl_usdt", 1.5)) for r in resolved if r.get("classification") == "TRUE_NEGATIVE")
    missed_alpha_usdt = sum(r.get("simulated_pnl_usdt", 0) for r in resolved if r.get("classification") == "FALSE_NEGATIVE")
    net_filter_edge = capital_saved_usdt - missed_alpha_usdt

    # 2. Clean intraday metrics (<= 4.0h horizon)
    intraday_records = [
        r for r in resolved
        if r.get("classification") in ["TRUE_NEGATIVE", "FALSE_NEGATIVE"]
        and ((r.get("resolved_at_ts", 0) - (r.get("activated_at_ts") or r.get("registered_at_ts", 0))) <= MAX_INTRADAY_HOLD_SECONDS + 300)
    ]
    i_tn = sum(1 for r in intraday_records if r.get("classification") == "TRUE_NEGATIVE")
    i_fn = sum(1 for r in intraday_records if r.get("classification") == "FALSE_NEGATIVE")
    i_conc = i_tn + i_fn
    intraday_fer = (i_tn / i_conc * 100) if i_conc > 0 else 0.0
    i_saved = sum(abs(r.get("simulated_pnl_usdt", 1.5)) for r in intraday_records if r.get("classification") == "TRUE_NEGATIVE")
    i_missed = sum(r.get("simulated_pnl_usdt", 0) for r in intraday_records if r.get("classification") == "FALSE_NEGATIVE")
    intraday_net_edge = i_saved - i_missed

    # 3. Rolling window metrics (last N resolved records)
    recent_slice = resolved[-rolling_window:] if len(resolved) > rolling_window else resolved
    r_tn = sum(1 for r in recent_slice if r.get("classification") == "TRUE_NEGATIVE")
    r_fn = sum(1 for r in recent_slice if r.get("classification") == "FALSE_NEGATIVE")
    r_conc = r_tn + r_fn
    rolling_fer = (r_tn / r_conc * 100) if r_conc > 0 else 0.0
    r_saved = sum(abs(r.get("simulated_pnl_usdt", 1.5)) for r in recent_slice if r.get("classification") == "TRUE_NEGATIVE")
    r_missed = sum(r.get("simulated_pnl_usdt", 0) for r in recent_slice if r.get("classification") == "FALSE_NEGATIVE")
    rolling_net_edge = r_saved - r_missed

    # 4. Resolved rows by rejection gate (issue #251; rows without "gate" predate it: legacy vol_ratio category)
    gate_counts: Dict[str, int] = {}
    gate_source_counts: Dict[str, int] = {}
    for r in resolved:
        gate, source = row_gate(r)
        gate_counts[gate] = gate_counts.get(gate, 0) + 1
        gate_source_counts[source] = gate_source_counts.get(source, 0) + 1

    return {
        "active_shadow_trades": len(active),
        "total_resolved": len(resolved),
        "true_negatives": tn_count,
        "false_negatives": fn_count,
        "expired": expired_count,
        "timeouts": timeout_count,
        "filter_efficacy_ratio_pct": round(fer, 1),
        "capital_saved_usdt": round(capital_saved_usdt, 2),
        "missed_alpha_usdt": round(missed_alpha_usdt, 2),
        "net_filter_edge_usdt": round(net_filter_edge, 2),
        "intraday_conclusive_count": i_conc,
        "intraday_fer_pct": round(intraday_fer, 1),
        "intraday_capital_saved_usdt": round(i_saved, 2),
        "intraday_missed_alpha_usdt": round(i_missed, 2),
        "intraday_net_edge_usdt": round(intraday_net_edge, 2),
        "rolling_window_size": len(recent_slice),
        "rolling_fer_pct": round(rolling_fer, 1),
        "rolling_capital_saved_usdt": round(r_saved, 2),
        "rolling_missed_alpha_usdt": round(r_missed, 2),
        "rolling_net_edge_usdt": round(rolling_net_edge, 2),
        "gate_counts": gate_counts,
        "gate_source_counts": gate_source_counts,
        "active_trades": active,
        "recent_resolved": resolved[-5:]
    }

def print_shadow_dashboard():
    m = calculate_efficacy_metrics()
    print("=" * 80)
    print("👻 SHADOW DESK — COUNTERFACTUAL FILTER EFFICACY AUDITOR")
    print(f"Timestamp: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 80)
    print(f"📊 SUMMARY METRICS (ALL-TIME):")
    print(f"  • Active Shadow Trades:  {m['active_shadow_trades']}")
    print(f"  • Total Resolved Trades:  {m['total_resolved']} (TN: {m['true_negatives']} | FN: {m['false_negatives']} | Timeouts: {m['timeouts']} | Expired: {m['expired']})")
    
    fer_color = "🟢" if m["filter_efficacy_ratio_pct"] >= 70 else ("🟡" if m["filter_efficacy_ratio_pct"] >= 50 else "🔴")
    print(f"  • Filter Efficacy Ratio:  {fer_color} {m['filter_efficacy_ratio_pct']}% (Target: > 70%)")
    print(f"  • Capital Saved (SL Avoided): +${m['capital_saved_usdt']} USDT")
    print(f"  • Missed Alpha (TP Missed):   -${m['missed_alpha_usdt']} USDT")
    net_str = f"+${m['net_filter_edge_usdt']}" if m['net_filter_edge_usdt'] >= 0 else f"-${abs(m['net_filter_edge_usdt'])}"
    print(f"  • Net Filter Advantage:   {net_str} USDT")
    print("-" * 80)
    print(f"⚡ INTRADAY CLEAN HORIZON (<= 4.0 Hours Holding):")
    i_color = "🟢" if m["intraday_fer_pct"] >= 70 else ("🟡" if m["intraday_fer_pct"] >= 50 else "🔴")
    print(f"  • Intraday Conclusive:    {m['intraday_conclusive_count']} setups")
    print(f"  • Intraday Clean FER:     {i_color} {m['intraday_fer_pct']}%")
    i_net_str = f"+${m['intraday_net_edge_usdt']}" if m['intraday_net_edge_usdt'] >= 0 else f"-${abs(m['intraday_net_edge_usdt'])}"
    print(f"  • Intraday Clean Edge:    {i_net_str} USDT (Saved: +${m['intraday_capital_saved_usdt']} | Missed: -${m['intraday_missed_alpha_usdt']})")
    print("-" * 80)
    print(f"🔄 ROLLING WINDOW (Last {m['rolling_window_size']} Setups):")
    r_color = "🟢" if m["rolling_fer_pct"] >= 70 else ("🟡" if m["rolling_fer_pct"] >= 50 else "🔴")
    print(f"  • Rolling FER:            {r_color} {m['rolling_fer_pct']}%")
    r_net_str = f"+${m['rolling_net_edge_usdt']}" if m['rolling_net_edge_usdt'] >= 0 else f"-${abs(m['rolling_net_edge_usdt'])}"
    print(f"  • Rolling Net Edge:       {r_net_str} USDT")
    print("-" * 80)

    if m["active_trades"]:
        print("🔍 CURRENTLY MONITORING (ACTIVE & PENDING):")
        print(f"{'Symbol':<14} | {'Dir':<5} | {'Status':<15} | {'Trigger':<10} | {'SL':<10} | {'TP1':<10} | {'Mark':<10} | {'MFE %':<7} | {'MAE %':<7}")
        print("-" * 95)
        for t in m["active_trades"]:
            print(f"{t['symbol']:<14} | {t['direction']:<5} | {t['status']:<15} | {t['trigger_price']:<10.4f} | {t['sl_price']:<10.4f} | {t['tp1_price']:<10.4f} | {t['last_checked_price']:<10.4f} | {t['max_favorable_excursion_pct']:<+7.2f} | {t['max_adverse_excursion_pct']:<+7.2f}")
    else:
        print("ℹ️ No active shadow trades currently pending.")

    if m["recent_resolved"]:
        print("-" * 80)
        print("🏁 RECENT RESOLUTIONS:")
        for r in m["recent_resolved"]:
            tag = "✅ TRUE NEGATIVE (Saved Loss)" if r["classification"] == "TRUE_NEGATIVE" else ("⚠️ FALSE NEGATIVE (Missed Profit)" if r["classification"] == "FALSE_NEGATIVE" else f"ℹ️ {r.get('classification')}")
            print(f"  • {r['symbol']} ({r['direction']}): {tag} | Outcome: {r['outcome']} | PnL: ${r['simulated_pnl_usdt']} USDT | Reason: {r['rejection_reason']}")
    print("=" * 80)

def _register_and_print() -> int:
    """register_from_evaluation with its deduped_window counter printed (--register-from-eval and --loop)."""
    stats = {"deduped_window": 0}
    count = register_from_evaluation(stats)
    print(f"📥 Registered {count} candidate(s) into shadow ledger (deduped_window {stats['deduped_window']}: "
          f"same symbol/direction/gate within {DEDUPE_WINDOW_SECONDS} s).")
    return count


def main():
    parser = argparse.ArgumentParser(description="Counterfactual Shadow Tracker")
    parser.add_argument("--register-from-eval", action="store_true", help="Auto-register rejected candidates from latest brief/dossier")
    parser.add_argument("--audit", action="store_true", help="Audit all active shadow trades against live klines")
    parser.add_argument("--json", action="store_true", help="Output metrics in JSON")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=300, help="Loop interval in seconds (default: 300)")
    args = parser.parse_args()

    if args.register_from_eval:
        _register_and_print()

    if args.audit or not (args.register_from_eval or args.loop or args.json):
        res = audit_shadow_trades()
        if not args.json:
            print_shadow_dashboard()
        else:
            print(json.dumps(calculate_efficacy_metrics(), indent=2))
        return

    if args.json and not args.loop:
        print(json.dumps(calculate_efficacy_metrics(), indent=2))
        return

    if args.loop:
        print(f"🚀 Starting Shadow Tracker Loop (interval: {args.interval}s)...")
        while True:
            try:
                # 1. Check for newly rejected evaluations (deduped_window printed too, issue #290)
                _register_and_print()
                # 2. Audit active trades
                audit_shadow_trades()
                # 3. Print report
                print_shadow_dashboard()
            except Exception as e:
                print(f"⚠️ Error in shadow loop: {e}")
            time.sleep(args.interval)

if __name__ == "__main__":
    main()
