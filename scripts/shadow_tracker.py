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
dossier_sha256 + symbol + direction, resolved rows included) and, for DELTA_GATE / DUPLICATE_RESTING, a best-effort
snapshot of the blocking positions / resting entries ("blockers") and of the whole book ("book") read from
logs/pending_entries.json, logs/session_state.json and logs/trades_audit.jsonl. Report-only: nothing here gates an
order. Regret and the policy replay are computed in scripts/shadow_analytics.py.

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
import urllib.request
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.atomic_writer import atomic_write_json, atomic_append_jsonl

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
SHADOW_TRADES_FILE = os.path.join(LOGS_DIR, "shadow_trades.jsonl")
SHADOW_RESOLVED_FILE = os.path.join(LOGS_DIR, "shadow_resolved.jsonl")
DOSSIER_FILE = os.path.join(LOGS_DIR, "evaluations", "latest_dossier.json")
BRIEF_FILE = os.path.join(LOGS_DIR, "primed_brief.json")
PENDING_ENTRIES_FILE = os.path.join(LOGS_DIR, "pending_entries.json")
SESSION_STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
TRADES_AUDIT_FILE = os.path.join(LOGS_DIR, "trades_audit.jsonl")

# Typed rejection reasons of the dossier's rejected_candidates (issue #251). K5 squeeze risk only caps a tier.
GATE_ENUM = ("DELTA_GATE", "MACRO_SHORT", "DUPLICATE_RESTING", "DRY_VOLUME", "FRICTION", "CATALYST_DOWNGRADE",
             "UNREADABLE_BOOK", "DAILY_LOSS_GATE", "OTHER")
GATE_FALLBACK = "OTHER"
BLOCKER_GATES = ("DELTA_GATE", "DUPLICATE_RESTING")
GATE_DETAIL_MAX_CHARS = 300

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
    extra: Optional[dict] = None
) -> Optional[dict]:
    """Registers a rejected setup into shadow_trades.jsonl if not already active.
    With dossier_sha256: at most one row per (dossier_sha256, symbol, direction), also after it resolved (issue #251);
    without it: no second active/pending row for the symbol within 60 minutes. extra: additional row keys (blockers)."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    existing = load_jsonl(SHADOW_TRADES_FILE)

    now_ts = int(time.time())
    if dossier_sha256:
        key = (dossier_sha256, symbol.upper().strip(), direction.upper().strip())
        for t in existing + load_jsonl(SHADOW_RESOLVED_FILE):
            if (t.get("dossier_sha256"), t.get("symbol"), str(t.get("direction") or "").upper()) == key:
                return None
    else:
        # Avoid duplicate active/pending trade for same symbol registered in last 60 minutes
        for t in existing:
            if t.get("symbol") == symbol and t.get("status") in ["PENDING_TRIGGER", "ACTIVE"]:
                if now_ts - t.get("registered_at_ts", 0) < 3600:
                    return None

    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
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


def _latest_audit_by_symbol(path: str) -> Dict[str, dict]:
    """Latest non-event entry record (with total_qty) per symbol of logs/trades_audit.jsonl (missing file: {})."""
    latest: Dict[str, dict] = {}
    for rec in load_jsonl(path):
        if isinstance(rec, dict) and not rec.get("event") and "total_qty" in rec and rec.get("symbol"):
            latest[str(rec["symbol"]).upper()] = rec
    return latest


def snapshot_book(target_env: Optional[str] = None) -> dict:
    """Best-effort snapshot of the book at registration time (issue #251), read-only:
    resting entries of logs/pending_entries.json (of target_env when given; a symbol with an open position counts as
    the position, as in sync_session_state) and active_positions of logs/session_state.json, each as
    {symbol, direction, kind: "position" | "resting", score, entry_id, notional, since_ts, audit_ts}. Resting score:
    score_meta.score (else dossier_score); position score / audit_ts: the latest trades_audit.jsonl entry record of
    the symbol in the same direction, else None. A missing file is an empty source. Any read error: {"items": [],
    "error": "..."}; never raises."""
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
        audit = _latest_audit_by_symbol(TRADES_AUDIT_FILE)

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
        return {"items": items, "error": None, "session_state_ts": _to_float(state.get("last_updated_ts"))}
    except Exception as e:
        return {"items": [], "error": f"{type(e).__name__}: {e}"[:200], "session_state_ts": None}


def blockers_for(gate: str, symbol: str, direction: str, book: dict) -> List[dict]:
    """The snapshot items that block a candidate: DELTA_GATE -> same direction; DUPLICATE_RESTING -> same symbol."""
    items = book.get("items") or []
    if gate == "DELTA_GATE":
        return [dict(i) for i in items if i.get("direction") == direction]
    if gate == "DUPLICATE_RESTING":
        return [dict(i) for i in items if i.get("symbol") == symbol]
    return []

def register_from_evaluation() -> int:
    """Reads latest evaluation brief and dossier, auto-registering rejected setups."""
    registered_count = 0
    
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
        return 0

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
                extra["book"] = [{k: i.get(k) for k in ("symbol", "direction", "kind", "score", "notional",
                                                        "since_ts", "entry_id")} for i in book.get("items") or []]
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
                extra=extra
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

def row_gate(row: dict) -> tuple:
    """(gate, gate_source) of a shadow row; a row written before issue #251 maps its vol_ratio category
    (DRY_VOLUME_FAKE_TIER_S -> DRY_VOLUME, anything else -> OTHER) with source "legacy_category"."""
    gate = row.get("gate")
    if isinstance(gate, str) and gate:
        return gate, str(row.get("gate_source") or "unknown")
    return ("DRY_VOLUME" if row.get("rejection_category") == "DRY_VOLUME_FAKE_TIER_S" else GATE_FALLBACK,
            "legacy_category")


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

def main():
    parser = argparse.ArgumentParser(description="Counterfactual Shadow Tracker")
    parser.add_argument("--register-from-eval", action="store_true", help="Auto-register rejected candidates from latest brief/dossier")
    parser.add_argument("--audit", action="store_true", help="Audit all active shadow trades against live klines")
    parser.add_argument("--json", action="store_true", help="Output metrics in JSON")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=300, help="Loop interval in seconds (default: 300)")
    args = parser.parse_args()

    if args.register_from_eval:
        count = register_from_evaluation()
        print(f"📥 Registered {count} candidate(s) into shadow ledger.")

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
                # 1. Check for newly rejected evaluations
                register_from_evaluation()
                # 2. Audit active trades
                audit_shadow_trades()
                # 3. Print report
                print_shadow_dashboard()
            except Exception as e:
                print(f"⚠️ Error in shadow loop: {e}")
            time.sleep(args.interval)

if __name__ == "__main__":
    main()
