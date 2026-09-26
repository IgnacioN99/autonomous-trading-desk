#!/usr/bin/env python3
"""
shadow_tracker.py - Counterfactual Shadow Trading & Filter Efficacy Auditor.
Deterministic tracking of unexecuted/rejected setups to eliminate selection bias.

Monitors discarded or disqualified candidates against live market klines:
1. Simulates conditional trigger execution (Pending -> Active).
2. Audits whether price reaches Stop Loss (True Negative / Capital Saved) or Take Profit (False Negative / Missed Alpha).
3. Computes Filter Efficacy Ratio (FER): TN / (TN + FN).
4. Persists results to logs/shadow_trades.jsonl and logs/shadow_resolved.jsonl without risking capital or consuming LLM tokens.

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
    target_dollar_risk: float = 1.50
) -> Optional[dict]:
    """Registers a rejected setup into shadow_trades.jsonl if not already active."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    existing = load_jsonl(SHADOW_TRADES_FILE)
    
    # Avoid duplicate active/pending trade for same symbol registered in last 60 minutes
    now_ts = int(time.time())
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

    atomic_append_jsonl(SHADOW_TRADES_FILE, trade)
    return trade

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

            res = register_shadow_trade(
                symbol=sym,
                direction=o.get("direction", "LONG"),
                trigger_price=float(o.get("trigger_price", o.get("current_price", 0))),
                sl_price=float(o.get("sl_price", 0)),
                tp1_price=float(o.get("tp1_price", 0)),
                tp2_price=float(o.get("tp2_price", 0)),
                current_price=float(o.get("current_price", 0)),
                vol_ratio=vol,
                rejection_reason=reason,
                rejection_category=cat,
                target_dollar_risk=float(o.get("target_dollar_risk", 1.50))
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
                if direction == "LONG" and k_high >= trigger_p:
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

                if direction == "LONG":
                    # Check SL hit
                    if k_low <= sl_p:
                        status = "RESOLVED"
                        outcome = "STOP_LOSS_HIT"
                        classification = "TRUE_NEGATIVE"
                        simulated_pnl = -risk_dollar # Filter saved us this loss!
                        break
                    # Check TP1 hit
                    elif k_high >= tp1_p:
                        status = "RESOLVED"
                        outcome = "TP1_HIT"
                        classification = "FALSE_NEGATIVE"
                        # Standard TP1 is ~1.8R
                        simulated_pnl = risk_dollar * 1.8 # Missed profit
                        break
                elif direction == "SHORT":
                    if k_high >= sl_p:
                        status = "RESOLVED"
                        outcome = "STOP_LOSS_HIT"
                        classification = "TRUE_NEGATIVE"
                        simulated_pnl = -risk_dollar
                        break
                    elif k_low <= tp1_p:
                        status = "RESOLVED"
                        outcome = "TP1_HIT"
                        classification = "FALSE_NEGATIVE"
                        simulated_pnl = risk_dollar * 1.8
                        break

        # Check Expiration (>24 hours)
        if status in ["PENDING_TRIGGER", "ACTIVE"] and (now_ts - t["registered_at_ts"]) > 86400:
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

def calculate_efficacy_metrics() -> dict:
    """Calculates Filter Efficacy Ratio and financial impact."""
    resolved = load_jsonl(SHADOW_RESOLVED_FILE)
    active = load_jsonl(SHADOW_TRADES_FILE)

    tn_count = sum(1 for r in resolved if r.get("classification") == "TRUE_NEGATIVE")
    fn_count = sum(1 for r in resolved if r.get("classification") == "FALSE_NEGATIVE")
    expired_count = sum(1 for r in resolved if r.get("classification") == "EXPIRED")

    total_conclusive = tn_count + fn_count
    fer = (tn_count / total_conclusive * 100) if total_conclusive > 0 else 0.0

    capital_saved_usdt = sum(abs(r.get("simulated_pnl_usdt", 1.5)) for r in resolved if r.get("classification") == "TRUE_NEGATIVE")
    missed_alpha_usdt = sum(r.get("simulated_pnl_usdt", 0) for r in resolved if r.get("classification") == "FALSE_NEGATIVE")
    net_filter_edge = capital_saved_usdt - missed_alpha_usdt

    return {
        "active_shadow_trades": len(active),
        "total_resolved": len(resolved),
        "true_negatives": tn_count,
        "false_negatives": fn_count,
        "expired": expired_count,
        "filter_efficacy_ratio_pct": round(fer, 1),
        "capital_saved_usdt": round(capital_saved_usdt, 2),
        "missed_alpha_usdt": round(missed_alpha_usdt, 2),
        "net_filter_edge_usdt": round(net_filter_edge, 2),
        "active_trades": active,
        "recent_resolved": resolved[-5:]
    }

def print_shadow_dashboard():
    m = calculate_efficacy_metrics()
    print("=" * 80)
    print("👻 SHADOW DESK — COUNTERFACTUAL FILTER EFFICACY AUDITOR")
    print(f"Timestamp: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 80)
    print(f"📊 SUMMARY METRICS:")
    print(f"  • Active Shadow Trades:  {m['active_shadow_trades']}")
    print(f"  • Total Resolved Trades:  {m['total_resolved']} (TN: {m['true_negatives']} | FN: {m['false_negatives']} | Expired: {m['expired']})")
    
    fer_color = "🟢" if m["filter_efficacy_ratio_pct"] >= 70 else ("🟡" if m["filter_efficacy_ratio_pct"] >= 50 else "🔴")
    print(f"  • Filter Efficacy Ratio:  {fer_color} {m['filter_efficacy_ratio_pct']}% (Target: > 70%)")
    print(f"  • Capital Saved (SL Evitado): +${m['capital_saved_usdt']} USDT")
    print(f"  • Missed Alpha (TP Perdido):  -${m['missed_alpha_usdt']} USDT")
    net_str = f"+${m['net_filter_edge_usdt']}" if m['net_filter_edge_usdt'] >= 0 else f"-${abs(m['net_filter_edge_usdt'])}"
    print(f"  • Net Filter Advantage:   {net_str} USDT")
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
            tag = "✅ TRUE NEGATIVE (Saved Loss)" if r["classification"] == "TRUE_NEGATIVE" else "⚠️ FALSE NEGATIVE (Missed Profit)"
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
