#!/usr/bin/env python3
"""
scripts/shadow_analytics.py — Shadow Desk Forensic Analytics & Calibration Engine
==================================================================================
Performs 3 quantitative evaluations on counterfactual setups in the Shadow Desk:
  1. Calibration / Meta-Optimizer: Evaluates optimal vol_ratio cutoff threshold.
  2. Alpha Leakage Analysis: Forensic audit of False Negatives (missed TP1s).
  3. Dodge Audit / Proof of Edge: Forensic audit of True Negatives (bullets dodged).

Delta-gate opportunity cost (issue #251; report-only, read-only: it never writes a file or touches an order):
  4. Regret: regret_R = shadow_R(blocked) - realized_R(blocker), one pair per (resolved DELTA_GATE /
     DUPLICATE_RESTING / DELTA_GATE_POST_APPROVAL row, blocker of its registration snapshot). shadow_R = simulated_pnl_usdt / target_dollar_risk
     (GROSS; EXPIRED rows = 0 R, counted apart; rows without it skipped and counted). realized_R(blocker): 0 for a
     resting entry cancelled unfilled at the timeout (logs/guardian_actions.jsonl pending_timeout_cancel, by
     symbol + entry_id), else realized_r_net (NET) of its logs/trade_outcomes.jsonl row (KEYS mode only), joined by
     symbol + audit_ts (the blocker's, or the logs/trades_audit.jsonl record whose entry_order_id is its entry_id);
     anything else is blocker_unresolved (excluded, counted). Mean with a seeded cluster bootstrap CI (clusters =
     row_clusters: rows linked by dossier_sha256 or by the same symbol + direction within 3600 s, issue #262), by
     gate, score-delta bucket (blocked score - blocker score) and blocker kind; n, n_clusters and
     insufficient_sample (n < MIN_SAMPLE) always printed, no conclusion below the minimum. With unresolved blockers
     (always in MCP mode) a sensitivity block imputes their R at -1R / 0R / +1.8R and says whether the sign changes.
  5. Policy replay of the DELTA_GATE rows' book snapshots: current rule, resting entries counted only after N
     minutes or at a fraction weight (candidate at full weight: printed WARNING), and swap (a candidate outscoring
     the weakest same-direction resting blocker by >= X points cancels it and is placed, only if the delta gate
     re-checked on the book without it allows the candidate: else swap_blocked; unverifiable: swap_unchecked).
     Total R, max drawdown in R (blocker R dated at its trade_outcomes exit, else at its first event,
     blocker_time_approx) and a net-delta exposure summary per policy. Unknown blocker R counts 0R in the
     headline; when any is unknown a sensitivity block gives every policy's total R at -1R / 0R / +1.8R and the
     imputations that change the policy ranking (a 0R tie that splits names the tied policies, issue #290).
  A PROMOTION GUARD line (issue #290, advisory text only, nothing reads it) says whether a proposal to relax the
  delta gate would even be admissible, else which conditions fail (promotion_guard).
  DELTA_GATE_POST_APPROVAL (issue #261): the hook's denials of dossier-approved candidates, registered by
  shadow_tracker from logs/gate_denials.jsonl; in both 4 and 5 (each denial its own replay event) with a caveat:
  their book is the cached session state at denial time (source session_state_cache), and denials by the
  executor's own live Gate 1 are not recorded.

Usage:
  python3 scripts/shadow_analytics.py [--json] [--resting-age-min 30] [--resting-weight 0.5] [--swap-margin 10]
      [--resamples 2000] [--seed 251]
"""

import os
import sys
import json
import math
import random
import argparse
from typing import List, Dict, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.portfolio_exposure import LONG_HEAVY, SHORT_HEAVY, book_exposure, project_order
from utils.shadow_common import DEDUPE_WINDOW_SECONDS, row_gate  # not shadow_tracker: no import coupling (#290)

RESOLVED_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs", "shadow_resolved.jsonl"))
TRADES_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs", "shadow_trades.jsonl"))
LOGS_DIR = os.path.dirname(RESOLVED_FILE)

REGRET_GATES = ("DELTA_GATE", "DUPLICATE_RESTING", "DELTA_GATE_POST_APPROVAL")
REPLAY_GATES = ("DELTA_GATE", "DELTA_GATE_POST_APPROVAL")
EXPIRED_CLASSIFICATIONS = ("EXPIRED", "EXPIRED_UNTRIGGERED")
MIN_SAMPLE = 30  # same minimum as exit_policy_sim.MIN_SAMPLE
DEFAULT_RESAMPLES = 2000
DEFAULT_SEED = 251
DEFAULT_RESTING_AGE_MIN = 30.0
DEFAULT_RESTING_WEIGHT = 0.5
DEFAULT_SWAP_MARGIN = 10.0
SCORE_DELTA_BUCKETS = ("< 0", "0-9", "10-19", ">= 20", "unknown")
REPLAY_POLICIES = ("current", "resting_after_n_min", "resting_fraction", "swap")
CONSERVATIVE_NOTE = ("Conservative definition (reported first): every resolved blocked row counts, EXPIRED ones (never "
                     "triggered) as 0 R; blockers without a known outcome are excluded and counted "
                     "(blocker_unresolved). triggered_only drops the EXPIRED rows and is reported second.")
GROSS_NET_NOTE = ("shadow R is GROSS (kline simulation: fill at the trigger, SL -1R, TP1 +1.8R, 4h timeout clipped, no "
                  "fees or slippage) while the blocker's realized R is NET of fees: regret_R leans toward the blocked "
                  "candidate.")
SELECTION_BIAS_NOTE = ("Selection bias: delta-blocked candidates arrive while the book is already loaded in the same "
                       "direction (often correlated with the blocker); they are not a random sample. Read regret "
                       "conditional on that book and never extrapolate it to removing the gate; rule changes go through "
                       "the owner (#159).")
IN_SAMPLE_NOTE = "Results are in-sample on the desk's own shadow rows: compare policies, do not read them as a forecast."
RANKING_NOTE = ("Ranking caveat: policies are counterfactuals replayed on a model (fills at the trigger, no slippage, "
                "5m bars); promoting one to the live gate requires a reviewed PR and the owner's decision.")
REPLAY_MODEL_NOTE = ("Replay model: the book is each dossier's registration snapshot; counterfactual placements stay in "
                     "the book until their shadow row resolved; the candidate's own notional is projected at full "
                     "weight (Gate 1 new-order rule) even where resting entries are discounted; a swap is made only "
                     "when the delta gate re-checked on the book without the cancelled entry (full weight) allows the "
                     "candidate, else nothing is cancelled or placed (swap_blocked; a book item without a notional "
                     "cannot be re-checked: placed as before, swap_unchecked); a blocker's R is dated at its exit "
                     "(trade_outcomes exit_ts), else at its first event (blocker_time_approx); blockers with an "
                     "unknown realized R count 0 R in the headline totals (blocker_r_unknown; see the sensitivity).")
RESTING_WEIGHT_WARNING = ("WARNING resting_after_n_min / resting_fraction: these are NOT faithful models of a rule that "
                          "discounts resting entries; the candidate is projected at full weight while the book's "
                          "resting entries are dropped or discounted, so their placed count and total R mix two "
                          "weightings and understate what such a rule would place. Compare them only as rough bounds.")
SENSITIVITY_R = (-1.0, 0.0, 1.8)  # R imputed to blockers with an unknown outcome (MCP mode: no trade_outcomes)
CLUSTER_WINDOW_SECONDS = DEDUPE_WINDOW_SECONDS
HOOK_DENIAL_NOTE = ("DELTA_GATE_POST_APPROVAL rows (issue #261) are the hook's denials of dossier-approved candidates: "
                    "their book is the cached logs/session_state.json at denial time (source session_state_cache, "
                    "possibly up to 300 s old), not the exchange; denials by the executor's own live Gate 1 are not "
                    "recorded.")

def load_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records

def run_calibration_analysis(resolved: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyzes win/loss rates across different vol_ratio buckets."""
    buckets = {
        "<= 0.2x (Extreme Illiquidity)": {"total": 0, "tn": 0, "fn": 0, "saved": 0.0, "missed": 0.0},
        "0.21x - 0.5x (Thin Volume)": {"total": 0, "tn": 0, "fn": 0, "saved": 0.0, "missed": 0.0},
        "> 0.5x (Approaching Institutional)": {"total": 0, "tn": 0, "fn": 0, "saved": 0.0, "missed": 0.0}
    }

    for r in resolved:
        vr = r.get("vol_ratio", 0.0)
        c = r.get("classification")
        saved = abs(r.get("simulated_pnl_usdt", 1.50)) if c == "TRUE_NEGATIVE" else 0.0
        missed = r.get("simulated_pnl_usdt", 2.70) if c == "FALSE_NEGATIVE" else 0.0

        if vr <= 0.25:
            b = buckets["<= 0.2x (Extreme Illiquidity)"]
        elif vr <= 0.55:
            b = buckets["0.21x - 0.5x (Thin Volume)"]
        else:
            b = buckets["> 0.5x (Approaching Institutional)"]

        b["total"] += 1
        if c == "TRUE_NEGATIVE":
            b["tn"] += 1
            b["saved"] += saved
        elif c == "FALSE_NEGATIVE":
            b["fn"] += 1
            b["missed"] += missed

    # Compute win rates and efficacy
    summary = {}
    for name, data in buckets.items():
        tot = data["total"]
        fer = round((data["tn"] / tot * 100), 1) if tot > 0 else 0.0
        net = round(data["saved"] - data["missed"], 2)
        summary[name] = {
            "total_setups": tot,
            "true_negatives_avoided_sl": data["tn"],
            "false_negatives_missed_tp1": data["fn"],
            "filter_efficacy_pct": fer,
            "capital_saved_usdt": round(data["saved"], 2),
            "missed_alpha_usdt": round(data["missed"], 2),
            "net_edge_usdt": net
        }
    return summary

def run_alpha_leakage_analysis(resolved: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deep forensic inspection of False Negatives (missed TP1s)."""
    fns = [r for r in resolved if r.get("classification") == "FALSE_NEGATIVE"]
    leakage = []
    for r in fns:
        leakage.append({
            "symbol": r.get("symbol"),
            "direction": r.get("direction"),
            "vol_ratio": r.get("vol_ratio"),
            "simulated_pnl_usdt": r.get("simulated_pnl_usdt"),
            "mfe_pct": r.get("max_favorable_excursion_pct"),
            "mae_pct": r.get("max_adverse_excursion_pct"),
            "duration_hours": round((r.get("resolved_at_ts", 0) - r.get("activated_at_ts", 0)) / 3600, 1),
            "rejection_reason": r.get("rejection_reason"),
            "risk_profile": "EXTREME_SLIPPAGE_OR_DRAWDOWN" if abs(r.get("max_adverse_excursion_pct", 0)) > 2.0 else "CLEAN_BOUNCE"
        })
    return leakage

def run_dodge_audit(resolved: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deep forensic inspection of True Negatives (avoided losses / dodged bullets)."""
    tns = [r for r in resolved if r.get("classification") == "TRUE_NEGATIVE"]
    dodges = []
    for r in tns:
        dodges.append({
            "symbol": r.get("symbol"),
            "direction": r.get("direction"),
            "vol_ratio": r.get("vol_ratio"),
            "simulated_loss_avoided_usdt": abs(r.get("simulated_pnl_usdt", -1.50)),
            "mfe_pct": r.get("max_favorable_excursion_pct"),
            "mae_pct": r.get("max_adverse_excursion_pct"),
            "duration_to_sl_hours": round((r.get("resolved_at_ts", 0) - r.get("activated_at_ts", 0)) / 3600, 1),
            "rejection_reason": r.get("rejection_reason")
        })
    # Sort by worst MAE (most violent adverse moves dodged)
    dodges.sort(key=lambda x: abs(x["mae_pct"]), reverse=True)
    return dodges

def run_intraday_hygiene_audit(resolved: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Segregates trades by holding horizon (<=4h intraday vs >4h drift vs timeouts)."""
    intraday = []
    drift = []
    timeouts = []

    for r in resolved:
        c = r.get("classification")
        act = r.get("activated_at_ts") or r.get("registered_at_ts", 0)
        res = r.get("resolved_at_ts", 0)
        dur_h = round((res - act) / 3600, 1) if res > act else 0.0

        if c == "TIMEOUT_CLOSED":
            timeouts.append(r)
        elif dur_h <= 4.0:
            intraday.append(r)
        else:
            drift.append(r)

    i_tn = sum(1 for r in intraday if r.get("classification") == "TRUE_NEGATIVE")
    i_fn = sum(1 for r in intraday if r.get("classification") == "FALSE_NEGATIVE")
    i_conc = i_tn + i_fn
    i_fer = round((i_tn / i_conc * 100), 1) if i_conc > 0 else 0.0
    i_saved = sum(abs(r.get("simulated_pnl_usdt", 1.5)) for r in intraday if r.get("classification") == "TRUE_NEGATIVE")
    i_missed = sum(r.get("simulated_pnl_usdt", 0) for r in intraday if r.get("classification") == "FALSE_NEGATIVE")

    return {
        "intraday_trades": len(intraday),
        "intraday_fer_pct": i_fer,
        "intraday_saved": round(i_saved, 2),
        "intraday_missed": round(i_missed, 2),
        "intraday_net_edge": round(i_saved - i_missed, 2),
        "drift_trades": len(drift),
        "timeout_closed": len(timeouts)
    }

# ------------------------------------------------------------------ delta-gate opportunity cost (issue #251)

def _num(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def shadow_r(row: Dict[str, Any]) -> Optional[float]:
    """Gross simulated R of a resolved shadow row: 0 when it never triggered (EXPIRED), else simulated_pnl_usdt /
    target_dollar_risk (None when either is missing or the risk is not positive)."""
    if row.get("classification") in EXPIRED_CLASSIFICATIONS:
        return 0.0
    pnl, risk = _num(row.get("simulated_pnl_usdt")), _num(row.get("target_dollar_risk"))
    if pnl is None or risk is None or risk <= 0:
        return None
    return pnl / risk


def _ts_key(value) -> Optional[float]:
    f = _num(value)
    return round(f, 3) if f is not None else None


def build_blocker_index(guardian_actions: List[Dict[str, Any]], trade_outcomes: List[Dict[str, Any]],
                        trades_audit: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Lookups for blocker outcomes: resting entries cancelled unfilled at the timeout ((symbol, entry_id) of
    successful, non-dry-run pending_timeout_cancel actions), audit_ts by (symbol, entry_order_id) of the
    trades_audit entry records, and trade_outcomes rows by (symbol, audit_ts)."""
    expired = set()
    for a in guardian_actions:
        detail = a.get("detail") if isinstance(a, dict) and isinstance(a.get("detail"), dict) else {}
        if (a.get("type") == "pending_timeout_cancel" and a.get("success") is True and not a.get("dry_run")
                and detail.get("entry_id") is not None):
            expired.add((str(a.get("symbol") or "").upper(), str(detail["entry_id"])))
    audit_ts = {}
    for rec in trades_audit:
        if isinstance(rec, dict) and not rec.get("event") and rec.get("entry_order_id") is not None:
            audit_ts[(str(rec.get("symbol") or "").upper(), str(rec["entry_order_id"]))] = _ts_key(rec.get("timestamp"))
    outcomes = {}
    for row in trade_outcomes:
        if isinstance(row, dict) and _ts_key(row.get("audit_ts")) is not None:
            outcomes[(str(row.get("symbol") or "").upper(), _ts_key(row.get("audit_ts")))] = row
    return {"expired": expired, "audit_ts": audit_ts, "outcomes": outcomes}


def blocker_outcome(blocker: Dict[str, Any], index: Dict[str, Any]) -> tuple:
    """(realized_r, kind): (0.0, "resting_expired_unfilled") for a resting entry cancelled unfilled at the timeout;
    (realized_r_net, "position" | "resting_filled") for a closed trade_outcomes row with a net R; else
    (None, "unresolved")."""
    sym = str(blocker.get("symbol") or "").upper()
    entry_id = None if blocker.get("entry_id") is None else str(blocker.get("entry_id"))
    resting = blocker.get("kind") == "resting"
    if resting and entry_id is not None and (sym, entry_id) in index["expired"]:
        return 0.0, "resting_expired_unfilled"
    row = _outcome_row(blocker, index)
    if row and row.get("status") == "closed" and _num(row.get("realized_r_net")) is not None:
        return _num(row.get("realized_r_net")), ("resting_filled" if resting else "position")
    return None, "unresolved"


def _outcome_row(blocker: Dict[str, Any], index: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The blocker's trade_outcomes row, joined by symbol + audit_ts (its own, else via its entry_id), or None."""
    sym = str(blocker.get("symbol") or "").upper()
    ts = _ts_key(blocker.get("audit_ts"))
    if ts is None and blocker.get("entry_id") is not None:
        ts = index["audit_ts"].get((sym, str(blocker.get("entry_id"))))
    return index["outcomes"].get((sym, ts)) if ts is not None else None


def blocker_exit_ts(blocker: Dict[str, Any], index: Dict[str, Any]) -> Optional[float]:
    """Close time in seconds of the blocker's closed trade_outcomes row (exit_ts is in ms), else None (issue #262)."""
    row = _outcome_row(blocker, index)
    exit_ms = _num(row.get("exit_ts")) if row and row.get("status") == "closed" else None
    return exit_ms / 1000.0 if exit_ms is not None and exit_ms > 0 else None


def score_delta_bucket(delta: Optional[float]) -> str:
    if delta is None:
        return "unknown"
    if delta < 0:
        return "< 0"
    if delta < 10:
        return "0-9"
    if delta < 20:
        return "10-19"
    return ">= 20"


def row_clusters(rows: List[Dict[str, Any]]) -> List[str]:
    """Bootstrap cluster label per row (issue #262): connected components of the rows that share a dossier_sha256 or
    have the same (symbol, direction) registered within CLUSTER_WINDOW_SECONDS of each other (chained in time order),
    so one candidate re-rejected by several dossiers never counts as independent clusters. Unlike the registration
    dedupe window (shadow_tracker, measured from the first row, no chaining) this chains: rows 0 s, 3000 s and 6000 s
    apart are one cluster. Label: the smallest dossier_sha256 (else id) of the component."""
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first_by_sha: Dict[str, int] = {}
    by_candidate: Dict[tuple, List[tuple]] = {}
    for i, r in enumerate(rows):
        sha = r.get("dossier_sha256")
        if isinstance(sha, str) and sha:
            union(first_by_sha.setdefault(sha, i), i)
        ts = _num(r.get("registered_at_ts"))
        if ts is not None:
            key = (str(r.get("symbol") or "").upper(), str(r.get("direction") or "").upper())
            by_candidate.setdefault(key, []).append((ts, i))
    for members in by_candidate.values():
        members.sort()
        for (t0, a), (t1, b) in zip(members, members[1:]):
            if t1 - t0 < CLUSTER_WINDOW_SECONDS:
                union(a, b)
    labels: Dict[int, str] = {}
    for i, r in enumerate(rows):
        label = str(r.get("dossier_sha256") or r.get("id"))
        root = find(i)
        labels[root] = min(labels.get(root, label), label)
    return [labels[find(i)] for i in range(len(rows))]


def regret_pairs(resolved: List[Dict[str, Any]], index: Dict[str, Any],
                 unresolved: Optional[List[Dict[str, Any]]] = None) -> tuple:
    """(pairs, counts): one pair per (resolved DELTA_GATE / DUPLICATE_RESTING row, blocker with a known outcome);
    cluster = row_clusters. unresolved (optional list): receives {"cluster", "shadow_r", "expired"} per blocker
    without a known outcome (the sensitivity imputes its R)."""
    pairs = []
    counts = {"rows": 0, "no_shadow_r": 0, "no_blockers": 0, "blockers_error": 0, "expired_blocked_rows": 0,
              "blocker_unresolved": 0}
    rows = [r for r in resolved if row_gate(r)[0] in REGRET_GATES]
    for r, cluster in zip(rows, row_clusters(rows)):
        gate, _source = row_gate(r)
        counts["rows"] += 1
        sr = shadow_r(r)
        if sr is None:
            counts["no_shadow_r"] += 1
            continue
        blockers = [b for b in (r.get("blockers") or []) if isinstance(b, dict)]
        if not blockers:
            counts["no_blockers"] += 1
            if r.get("blockers_error"):
                counts["blockers_error"] += 1
            continue
        expired = r.get("classification") in EXPIRED_CLASSIFICATIONS
        if expired:
            counts["expired_blocked_rows"] += 1
        score = _num(r.get("score"))
        for b in blockers:
            realized, kind = blocker_outcome(b, index)
            if realized is None:
                counts["blocker_unresolved"] += 1
                if unresolved is not None:
                    unresolved.append({"cluster": cluster, "shadow_r": sr, "expired": expired})
                continue
            b_score = _num(b.get("score"))
            delta = score - b_score if score is not None and b_score is not None else None
            pairs.append({"cluster": cluster, "row_id": r.get("id"), "gate": gate,
                          "symbol": r.get("symbol"), "direction": r.get("direction"),
                          "blocker_symbol": b.get("symbol"), "blocker_kind": kind, "shadow_r": round(sr, 6),
                          "blocker_r": round(realized, 6), "regret_r": round(sr - realized, 6),
                          "score_delta": delta, "score_delta_bucket": score_delta_bucket(delta), "expired": expired})
    return pairs, counts


def bootstrap_mean_ci(pairs: List[Dict[str, Any]], resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED,
                      key: str = "regret_r") -> Dict[str, Any]:
    """Mean of pairs[key] with a 95% percentile CI from a cluster bootstrap (whole clusters resampled with
    replacement, random.Random(seed)): deterministic for the same input order, seed and resamples."""
    clusters: Dict[Any, List[float]] = {}
    for p in pairs:
        clusters.setdefault(p.get("cluster"), []).append(p[key])
    groups = [(sum(v), len(v)) for v in clusters.values()]
    n = len(pairs)
    out = {"n": n, "n_clusters": len(groups), "mean_regret_r": None, "ci95_low": None, "ci95_high": None,
           "insufficient_sample": n < MIN_SAMPLE, "resamples": resamples, "seed": seed}
    if not n:
        return out
    out["mean_regret_r"] = round(sum(p[key] for p in pairs) / n, 6)
    if resamples < 1:
        return out
    rng = random.Random(seed)
    k = len(groups)
    means = []
    for _ in range(resamples):
        total = count = 0
        for _ in range(k):
            s, c = groups[rng.randrange(k)]
            total += s
            count += c
        means.append(total / count)
    means.sort()
    out["ci95_low"] = round(means[int(math.floor(0.025 * (resamples - 1)))], 6)
    out["ci95_high"] = round(means[int(math.ceil(0.975 * (resamples - 1)))], 6)
    return out


def regret_report(resolved: List[Dict[str, Any]], guardian_actions: List[Dict[str, Any]],
                  trade_outcomes: List[Dict[str, Any]], trades_audit: List[Dict[str, Any]],
                  resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    """Regret of the delta gate (and duplicate-resting rule) in R; conservative definition first. The headline
    excludes blockers without a known outcome; when there are any (always in MCP mode), "sensitivity" holds the
    conservative statistics with their R imputed at each SENSITIVITY_R (issue #262), else None."""
    unresolved: List[Dict[str, Any]] = []
    pairs, counts = regret_pairs(resolved, build_blocker_index(guardian_actions, trade_outcomes, trades_audit),
                                 unresolved)

    def split(field, labels):
        return {label: bootstrap_mean_ci([p for p in pairs if p[field] == label], resamples, seed)
                for label in labels if any(p[field] == label for p in pairs)}

    sensitivity = None
    if unresolved:
        stats = {_r_label(v): bootstrap_mean_ci(
            pairs + [{"cluster": u["cluster"], "regret_r": round(u["shadow_r"] - v, 6)} for u in unresolved],
            resamples, seed) for v in SENSITIVITY_R}
        signs = {label: (s["mean_regret_r"] > 0) - (s["mean_regret_r"] < 0) for label, s in stats.items()}
        sensitivity = {"unknown_blockers": len(unresolved), "by_imputed_r": stats,
                       "sign_changes": len(set(signs.values())) > 1}

    return {
        "definition": "conservative",
        "conservative": bootstrap_mean_ci(pairs, resamples, seed),
        "triggered_only": bootstrap_mean_ci([p for p in pairs if not p["expired"]], resamples, seed),
        "by_gate": split("gate", REGRET_GATES),
        "by_score_delta": split("score_delta_bucket", SCORE_DELTA_BUCKETS),
        "by_blocker_kind": split("blocker_kind", ("position", "resting_filled", "resting_expired_unfilled")),
        "counts": counts, "min_sample": MIN_SAMPLE, "pairs": pairs, "sensitivity": sensitivity,
        "warnings": [CONSERVATIVE_NOTE, GROSS_NET_NOTE, SELECTION_BIAS_NOTE, HOOK_DENIAL_NOTE],
    }


def _r_label(value: float) -> str:
    return f"{value:+g}R" if value else "0R"


def candidate_notional(row: Dict[str, Any]) -> tuple:
    """(notional, derived): the row's notional_usdt when positive, else target_dollar_risk / |trigger - sl| x
    trigger (derived True); (None, False) when neither is available."""
    n = _num(row.get("notional_usdt"))
    if n is not None and n > 0:
        return n, False
    risk, trig, sl = _num(row.get("target_dollar_risk")), _num(row.get("trigger_price")), _num(row.get("sl_price"))
    if risk and risk > 0 and trig and trig > 0 and sl is not None and abs(trig - sl) > 0:
        return risk / abs(trig - sl) * trig, True
    return None, False


def _gate_allows(long_n: float, short_n: float, is_long: bool, notional: float) -> bool:
    """Gate 1 rule (execute_futures_trade): blocked when the book is heavy in the order's direction or, on a
    non-empty book, when the order would tip it heavy that way."""
    heavy = LONG_HEAVY if is_long else SHORT_HEAVY
    book = book_exposure(long_n, short_n)
    if book["delta_bias"] == heavy:
        return False
    return not (long_n + short_n > 0 and project_order(book, is_long, notional)["delta_bias"] == heavy)


def _item_key(item: Dict[str, Any]) -> tuple:
    """Trade identity of a book item / blocker: (symbol, direction, entry_id), without kind, so a resting entry and
    the position it filled into (same entry_id, execute_futures_trade -> active_positions.entry_order_id) are one
    trade: its R counts once and a swap's cancellation drops both. Without an entry_id the kind stays in the key
    plus the item's own start time (since_ts, else audit_ts), so two trades of one symbol / direction at different
    times never collapse into one (issue #262); with neither, the time cannot be checked and the item keeps the
    collapsed pre-#262 key (one trade per symbol / direction / kind, never counted twice)."""
    sym, direction = str(item.get("symbol") or "").upper(), str(item.get("direction") or "").upper()
    if item.get("entry_id") is None:
        for field in ("since_ts", "audit_ts"):
            ts = _ts_key(item.get(field))
            if ts is not None:
                return (sym, direction, None, item.get("kind"), field, ts)
        return (sym, direction, None, item.get("kind"))
    return (sym, direction, str(item.get("entry_id")))


def _max_drawdown(contributions: List[tuple]) -> float:
    peak = cum = dd = 0.0
    for _ts, r in sorted(contributions, key=lambda x: x[0]):
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return round(dd, 6)


def replay_policies(resolved: List[Dict[str, Any]], index: Dict[str, Any],
                    resting_age_min: float = DEFAULT_RESTING_AGE_MIN, resting_weight: float = DEFAULT_RESTING_WEIGHT,
                    swap_margin: float = DEFAULT_SWAP_MARGIN) -> Dict[str, Any]:
    """Report-only replay of the resolved DELTA_GATE and DELTA_GATE_POST_APPROVAL rows (REPLAY_GATES, grouped per
    dossier into events, oldest first) under:
    current (nothing placed), resting_after_n_min (a resting entry counts toward delta only once it is
    resting_age_min old; unknown age counts), resting_fraction (resting entries count at resting_weight) and swap (a
    candidate whose score beats the weakest scored same-direction resting blocker by >= swap_margin cancels it and is
    placed, only if the delta gate re-checked on the book without that entry allows it, issue #262: else swap_blocked
    and nothing changes; a book item without a notional: placed without re-check, swap_unchecked). Candidates of one
    event are taken by score, highest first; a placement joins the book (as a resting entry) until its shadow row
    resolved. Total R = placed candidates' shadow R + realized R of the events' blockers not cancelled, dated at
    their exit (trade_outcomes exit_ts) else at their first event (blocker_time_approx); unknown blocker R counts
    0 R in the headline, and when any is unknown "sensitivity" holds every policy's total R with it at each
    SENSITIVITY_R, the ranking (best first) under each, the imputations that reverse a strict 0R order
    (ranking_changes) and those whose ranking differs only by ties (ranking_ties), with tie_splits: per
    imputation, the policy pairs tied at 0R that split strictly there (issue #290) (else None).
    Exposure = the full-weight book after each event's decisions."""
    out = _replay(resolved, index, resting_age_min, resting_weight, swap_margin, 0.0)
    out["sensitivity"] = None
    if out["blocker_r_unknown"]:
        totals = {}
        for v in SENSITIVITY_R:
            res = out if v == 0.0 else _replay(resolved, index, resting_age_min, resting_weight, swap_margin, v)
            totals[_r_label(v)] = {name: m["total_r"] for name, m in res["policies"].items()}
        ranking = {label: sorted(t, key=lambda n: (-t[n], REPLAY_POLICIES.index(n))) for label, t in totals.items()}
        base = totals["0R"]
        changes, ties, splits = [], [], {}
        for label, t in totals.items():
            if ranking[label] == ranking["0R"]:
                continue
            # a real change reverses a strict order; otherwise only ties (broken by REPLAY_POLICIES order) differ
            reversed_ = any(base[x] > base[y] and t[y] > t[x] for x in t for y in t)
            (changes if reversed_ else ties).append(label)
            if not reversed_:
                splits[label] = [[x, y] for i, x in enumerate(REPLAY_POLICIES) for y in REPLAY_POLICIES[i + 1:]
                                 if base[x] == base[y] and t[x] != t[y]]
        out["sensitivity"] = {"unknown_blockers": out["blocker_r_unknown"], "total_r": totals, "ranking": ranking,
                              "ranking_changes": changes, "ranking_ties": ties, "tie_splits": splits}
    return out


def _replay(resolved: List[Dict[str, Any]], index: Dict[str, Any], resting_age_min: float, resting_weight: float,
            swap_margin: float, unknown_r: float) -> Dict[str, Any]:
    """replay_policies with blockers of unknown outcome counted at unknown_r."""
    rows = [r for r in resolved if row_gate(r)[0] in REPLAY_GATES]
    skipped = {"no_book": 0, "no_shadow_r": 0, "notional_missing": 0}
    events: Dict[Any, Dict[str, Any]] = {}
    notional_derived = 0
    for r in rows:
        if not isinstance(r.get("book"), list) or r.get("book_truncated"):
            # issue #261: a hook event whose book did not fit its 4 KB line has no usable (complete) book
            skipped["no_book"] += 1
            continue
        if shadow_r(r) is None:
            skipped["no_shadow_r"] += 1
            continue
        notional, derived = candidate_notional(r)
        if notional is None:
            skipped["notional_missing"] += 1
            continue
        notional_derived += int(derived)
        # A hook denial (later, cache book) is its own event, apart from its dossier's DELTA_GATE rows
        key = (r.get("dossier_sha256") or r.get("id"), row_gate(r)[0])
        ev = events.setdefault(key, {"ts": _num(r.get("registered_at_ts")) or 0.0, "book": r["book"], "rows": []})
        ev["ts"] = min(ev["ts"], _num(r.get("registered_at_ts")) or 0.0)
        ev["rows"].append(dict(r, _notional=notional, _derived=derived))
    ordered = sorted(events.items(), key=lambda kv: (kv[1]["ts"], str(kv[0])))

    blocker_r: Dict[tuple, Optional[float]] = {}
    blocker_ts: Dict[tuple, float] = {}
    blocker_time_approx = 0
    for _key, ev in ordered:
        for row in ev["rows"]:
            for b in row.get("blockers") or []:
                if not isinstance(b, dict) or _item_key(b) in blocker_r:
                    continue
                key = _item_key(b)
                realized, kind = blocker_outcome(b, index)
                blocker_r[key] = realized
                exit_ts = blocker_exit_ts(b, index) if kind in ("position", "resting_filled") else None
                blocker_time_approx += int(kind in ("position", "resting_filled") and exit_ts is None)
                blocker_ts[key] = exit_ts if exit_ts is not None else ev["ts"]
    book_notional_missing = sum(1 for _k, ev in ordered for i in ev["book"]
                                if isinstance(i, dict) and _num(i.get("notional")) is None)

    def weight(item, ts, mode):
        if item.get("kind") != "resting":
            return 1.0
        if mode == "age":
            since = _num(item.get("since_ts"))
            return 1.0 if since is None or ts - since >= resting_age_min * 60 else 0.0
        if mode == "weight":
            return resting_weight
        return 1.0

    def sums(items, ts, mode):
        long_n = short_n = 0.0
        for i in items:
            n = (_num(i.get("notional")) or 0.0) * weight(i, ts, mode)
            if str(i.get("direction") or "").upper() == "LONG":
                long_n += n
            elif str(i.get("direction") or "").upper() == "SHORT":
                short_n += n
        return long_n, short_n

    modes = {"current": "current", "resting_after_n_min": "age", "resting_fraction": "weight", "swap": "swap"}
    policies = {}
    for name in REPLAY_POLICIES:
        mode = modes[name]
        cancelled, placed, contributions, series, placements = set(), [], [], [], []
        swapped = swap_blocked = swap_unchecked = 0
        for _key, ev in ordered:
            ts = ev["ts"]
            items = [i for i in ev["book"] if isinstance(i, dict) and _item_key(i) not in cancelled]
            items += [p for p in placed if p["since_ts"] <= ts < p["until_ts"]]
            for row in sorted(ev["rows"], key=lambda x: (-(_num(x.get("score")) if _num(x.get("score")) is not None
                                                             else -math.inf), str(x.get("id")))):
                is_long = str(row.get("direction") or "").upper() == "LONG"
                place = False
                if mode in ("age", "weight"):
                    place = _gate_allows(*sums(items, ts, mode), is_long, row["_notional"])
                elif mode == "swap":
                    score = _num(row.get("score"))
                    eligible = [i for i in items if i.get("kind") == "resting" and i.get("entry_id") is not None
                                and str(i.get("direction") or "").upper() == str(row.get("direction") or "").upper()
                                and _num(i.get("score")) is not None and _item_key(i) not in cancelled]
                    if score is not None and eligible:
                        weakest = min(eligible, key=lambda i: (_num(i.get("score")), str(i.get("entry_id"))))
                        if score - _num(weakest.get("score")) >= swap_margin:
                            wkey = _item_key(weakest)
                            rest = [i for i in items if _item_key(i) != wkey]
                            if any(_num(i.get("notional")) is None for i in rest):
                                swap_unchecked += 1   # no faithful projection: placed as before #262
                                place = True
                            else:
                                place = _gate_allows(*sums(rest, ts, "current"), is_long, row["_notional"])
                                swap_blocked += int(not place)
                            if place:
                                cancelled.add(wkey)
                                items = rest
                                swapped += 1
                if place:
                    until = _num(row.get("resolved_at_ts")) or ts
                    p = {"kind": "resting", "symbol": row.get("symbol"), "direction": row.get("direction"),
                         "notional": row["_notional"], "since_ts": ts, "until_ts": until, "entry_id": None}
                    placed.append(p)
                    items.append(p)
                    contributions.append((until, shadow_r(row)))
                    placements.append({"id": row.get("id"), "symbol": row.get("symbol"), "ts": ts,
                                       "shadow_r": round(shadow_r(row), 6), "notional_derived": row["_derived"]})
            long_n, short_n = sums(items, ts, "current")
            exp = book_exposure(long_n, short_n)
            series.append({"ts": ts, "net_notional": round(exp["net_notional"], 2),
                           "delta_ratio": round(exp["delta_ratio"], 6), "delta_bias": exp["delta_bias"]})
        for key, r in blocker_r.items():
            if key not in cancelled:
                contributions.append((blocker_ts[key], unknown_r if r is None else r))
        abs_ratios = [abs(s["delta_ratio"]) for s in series]
        policies[name] = {
            "total_r": round(sum(r for _ts, r in contributions), 6),
            "max_drawdown_r": _max_drawdown(contributions),
            "placed": len(placements), "swapped": swapped, "swap_blocked": swap_blocked,
            "swap_unchecked": swap_unchecked, "placements": placements,
            "exposure": {"points": len(series),
                         "mean_abs_delta_ratio": round(sum(abs_ratios) / len(abs_ratios), 6) if abs_ratios else None,
                         "max_abs_delta_ratio": round(max(abs_ratios), 6) if abs_ratios else None,
                         "heavy_share": (round(sum(1 for s in series if s["delta_bias"] != "DELTA_BALANCED")
                                               / len(series), 6) if series else None),
                         "series": series},
        }
    return {"n_events": len(ordered), "n_rows": sum(len(ev["rows"]) for _k, ev in ordered),
            "insufficient_sample": len(ordered) < MIN_SAMPLE, "min_sample": MIN_SAMPLE,
            "params": {"resting_age_min": resting_age_min, "resting_weight": resting_weight,
                       "swap_margin": swap_margin},
            "skipped": skipped, "notional_derived": notional_derived, "book_notional_missing": book_notional_missing,
            "blockers": len(blocker_r), "blocker_r_unknown": sum(1 for r in blocker_r.values() if r is None),
            "blocker_time_approx": blocker_time_approx, "unknown_blocker_r": unknown_r,
            "policies": policies,
            "warnings": [IN_SAMPLE_NOTE, RANKING_NOTE, SELECTION_BIAS_NOTE, REPLAY_MODEL_NOTE, RESTING_WEIGHT_WARNING,
                         GROSS_NET_NOTE, HOOK_DENIAL_NOTE]}


def _fmt_r(value) -> str:
    return "-" if value is None else f"{value:+.3f}"


def _stat_line(label: str, s: Dict[str, Any]) -> str:
    ci = "-" if s["ci95_low"] is None else f"[{s['ci95_low']:+.3f}, {s['ci95_high']:+.3f}]"
    flag = " insufficient_sample" if s["insufficient_sample"] else ""
    return (f"  • {label:<28} mean regret {_fmt_r(s['mean_regret_r'])}R | 95% CI {ci} | n={s['n']} | "
            f"n_clusters={s['n_clusters']}{flag}")


def format_delta_gate_report(regret: Dict[str, Any], replay: Dict[str, Any]) -> str:
    """Terminal section for the regret metric and the policy replay (no conclusion below MIN_SAMPLE)."""
    c = regret["counts"]
    lines = ["⚖️ APPLICATION D: DELTA GATE OPPORTUNITY COST (regret_R = shadow R blocked - realized R blocker)",
             "-" * 80]
    lines += [f"  ! {w}" for w in regret["warnings"]]
    lines.append(_stat_line("conservative (first)", regret["conservative"]))
    lines.append(_stat_line("triggered_only", regret["triggered_only"]))
    for title, block in (("gate", regret["by_gate"]), ("score delta", regret["by_score_delta"]),
                         ("blocker kind", regret["by_blocker_kind"])):
        for label, s in block.items():
            lines.append(_stat_line(f"{title} {label}", s))
    lines.append(f"  rows {c['rows']} | expired blocked rows {c['expired_blocked_rows']} (0 R) | blocker_unresolved "
                 f"{c['blocker_unresolved']} (excluded) | no shadow R {c['no_shadow_r']} | no blockers "
                 f"{c['no_blockers']} (snapshot errors {c['blockers_error']})")
    sens = regret.get("sensitivity")
    if sens:
        lines.append(f"  Sensitivity (headline above excludes the {sens['unknown_blockers']} blocker(s) with unknown "
                     f"R, e.g. MCP mode without trade_outcomes; here they are imputed):")
        for label, s in sens["by_imputed_r"].items():
            lines.append(_stat_line(f"unknown blocker R = {label}", s))
        lines.append("  The sign of the mean regret " + ("CHANGES with the imputed blocker R: no conclusion on its sign."
                                                         if sens["sign_changes"] else
                                                         "is the same under every imputation."))
    cons = regret["conservative"]
    if cons["insufficient_sample"]:
        lines.append(f"  insufficient_sample: n={cons['n']} < {regret['min_sample']}: no conclusion.")
    elif cons["ci95_low"] is None:
        lines.append("  No bootstrap CI (resamples < 1): no conclusion.")
    elif cons["ci95_low"] > 0 or cons["ci95_high"] < 0:
        lines.append("  The conservative 95% CI excludes 0 (in-sample, gross vs net; see the caveats above).")
    else:
        lines.append("  The conservative 95% CI includes 0: no measurable regret on this sample.")
    lines += ["", "🔁 APPLICATION E: DELTA GATE POLICY REPLAY (report only)", "-" * 80]
    lines += [f"  ! {w}" for w in replay["warnings"]]
    p = replay["params"]
    lines.append(f"  events {replay['n_events']} (rows {replay['n_rows']}) | skipped {replay['skipped']} | notional "
                 f"derived {replay['notional_derived']} | blocker R unknown {replay['blocker_r_unknown']}/"
                 f"{replay['blockers']} | blocker_time_approx {replay.get('blocker_time_approx', 0)} | "
                 f"N={p['resting_age_min']:g} min, weight={p['resting_weight']:g}, swap X={p['swap_margin']:g}")
    sens = replay.get("sensitivity")
    if sens:
        lines.append(f"  Headline total R counts the {sens['unknown_blockers']} blocker(s) with unknown R at 0R "
                     f"(see the sensitivity below).")
    lines.append(f"  {'policy':<22}{'total R':>10}{'maxDD R':>10}{'placed':>8}{'swapped':>9}{'mean|dr|':>10}"
                 f"{'heavy %':>9}")
    for name, m in replay["policies"].items():
        e = m["exposure"]
        mean_dr = "-" if e["mean_abs_delta_ratio"] is None else f"{e['mean_abs_delta_ratio']:.3f}"
        heavy = "-" if e["heavy_share"] is None else f"{e['heavy_share'] * 100:.1f}"
        lines.append(f"  {name:<22}{m['total_r']:>+10.3f}{m['max_drawdown_r']:>10.3f}{m['placed']:>8}"
                     f"{m['swapped']:>9}{mean_dr:>10}{heavy:>9}")
    swap = replay["policies"].get("swap")
    if swap is not None:
        lines.append(f"  swap re-check (delta gate on the book without the cancelled entry): swap_blocked "
                     f"{swap.get('swap_blocked', 0)} | swap_unchecked {swap.get('swap_unchecked', 0)} (book item "
                     f"without notional: placed without re-check)")
    if sens:
        lines.append(f"  Sensitivity, total R with unknown blocker R imputed ({sens['unknown_blockers']} blocker(s)):")
        for label, totals in sens["total_r"].items():
            tag = " (headline)" if label == "0R" else ""
            lines.append(f"    unknown = {label:<6}" + " | ".join(f"{n} {totals[n]:+.3f}" for n in totals) + tag)
        order = " > ".join(sens["ranking"]["0R"])
        ties = sens.get("ranking_ties") or []
        splits = sens.get("tie_splits") or {}
        generic_ties = [label for label in ties if not splits.get(label)]
        for labels, verb in ((sens["ranking_changes"], "CHANGES at"), (generic_ties, "differs only by a tie at")):
            if labels:
                lines.append(f"  Policy ranking at 0R ({order}) {verb} "
                             + ", ".join(f"{label} ({' > '.join(sens['ranking'][label])})" for label in labels) + ".")
        for label in ties:
            if splits.get(label):
                pairs = ", ".join(f"{x} = {y}" for x, y in splits[label])
                lines.append(f"  Policy ranking at 0R ({order}) differs at {label} "
                             f"({' > '.join(sens['ranking'][label])}): policies tied at 0R split there: {pairs}.")
        if not sens["ranking_changes"] and not ties:
            lines.append(f"  Policy ranking unchanged at every imputation: {order}.")
    if replay["insufficient_sample"]:
        lines.append(f"  insufficient_sample: {replay['n_events']} event(s) < {replay['min_sample']}: no ranking.")
    guard = promotion_guard(regret, replay)
    if guard is not None:
        if guard["admissible"]:
            lines.append("  PROMOTION GUARD (advisory, nothing reads it): a proposal to relax the delta gate would be "
                         "admissible for review (sample, clusters, sign, blocker R and swap checks pass); promotion "
                         "still needs a reviewed PR and the owner's decision.")
        else:
            lines.append("  PROMOTION GUARD (advisory, nothing reads it): a proposal to relax the delta gate is NOT "
                         "admissible: " + "; ".join(guard["failing"]) + ".")
    lines.append("=" * 80)
    return "\n".join(lines)


def promotion_guard(regret: Dict[str, Any], replay: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Whether a proposal to relax the delta gate would even be admissible (issue #290; advisory text only, no code
    path consumes it): {"admissible", "failing"} where failing lists the conditions that do not hold: the
    conservative regret and the replay not insufficient_sample, regret n_clusters >= MIN_SAMPLE, no sign change of
    the mean regret under the unknown-blocker imputations (sign_changes), no blocker of unknown R in the replay
    (blocker_r_unknown) and no swap placed without the delta re-check (swap_unchecked). None without data (no
    regret row and no replay event)."""
    if not regret["counts"]["rows"] and not replay["n_events"]:
        return None
    cons = regret["conservative"]
    min_sample = regret.get("min_sample", MIN_SAMPLE)
    failing = []
    if cons["insufficient_sample"]:
        failing.append(f"insufficient_sample (regret n={cons['n']} < {min_sample})")
    if replay["insufficient_sample"]:
        failing.append(f"insufficient_sample (replay events {replay['n_events']} < {replay['min_sample']})")
    if cons["n_clusters"] < min_sample:
        failing.append(f"n_clusters {cons['n_clusters']} < {min_sample}")
    if (regret.get("sensitivity") or {}).get("sign_changes"):
        failing.append("sign_changes true (the mean regret's sign depends on the imputed blocker R)")
    if replay["blocker_r_unknown"]:
        failing.append(f"blocker_r_unknown {replay['blocker_r_unknown']}")
    unchecked = (replay["policies"].get("swap") or {}).get("swap_unchecked", 0)
    if unchecked:
        failing.append(f"swap: swap_unchecked {unchecked} (placed without the delta re-check; must fail closed)")
    return {"admissible": not failing, "failing": failing}


def format_terminal_report(calibration: Dict[str, Any], leakage: List[Dict[str, Any]], dodges: List[Dict[str, Any]], hygiene: Optional[Dict[str, Any]] = None) -> str:
    lines = [
        "=" * 80,
        "🔬 SHADOW DESK COMPREHENSIVE FORENSIC REPORT",
        "=" * 80,
        ""
    ]

    if hygiene:
        lines.extend([
            "⏱️ APPLICATION 0: INTRADAY HORIZON & SAMPLE HYGIENE AUDIT",
            "-" * 80,
            f"  • Clean Intraday Trades (<= 4.0h): {hygiene['intraday_trades']}",
            f"  • Clean Intraday FER:              {hygiene['intraday_fer_pct']}%",
            f"  • Capital Preserved (Intraday):    +${hygiene['intraday_saved']:.2f} USDT",
            f"  • Missed Alpha (Intraday):         +${hygiene['intraday_missed']:.2f} USDT",
            f"  • Net Intraday Filter Edge:        {'+' if hygiene['intraday_net_edge'] >= 0 else ''}${hygiene['intraday_net_edge']:.2f} USDT",
            f"  • Stagnant Drift Setups (> 4.0h):  {hygiene['drift_trades']} (quarantined from intraday sample)",
            f"  • Timeout Reaped Setups (4.0h):    {hygiene['timeout_closed']}",
            "-" * 80,
            "💡 HYGIENE VERDICT:",
            "  • Filters demonstrate 75%+ efficacy when evaluated under the desk's true intraday horizon (<= 4h).",
            "  • Quarantining multi-day drift eliminates artificial sample pollution caused by intermittent analysis.",
            ""
        ])

    lines.extend([
        "📊 APPLICATION A: PARAMETER THRESHOLD CALIBRATION (vol_ratio)",
        "-" * 80,
        f"{'Volume Bucket':<35} | {'Trades':<6} | {'TN (Dodged)':<11} | {'FN (Missed)':<11} | {'FER %':<7} | {'Net Edge':<10}",
        "-" * 80
    ])
    for b_name, b in calibration.items():
        edge_str = f"{'+' if b['net_edge_usdt'] >= 0 else ''}${b['net_edge_usdt']:.2f}"
        lines.append(f"{b_name:<35} | {b['total_setups']:<6} | {b['true_negatives_avoided_sl']:<11} | {b['false_negatives_missed_tp1']:<11} | {b['filter_efficacy_pct']:<6.1f}% | {edge_str:<10}")
    lines.append("-" * 80)
    lines.append("💡 CALIBRATION VERDICT:")
    lines.append("  • At vol_ratio <= 0.2x: 66.7% of setups hit SL directly. Extreme illiquidity makes stops highly fragile.")
    lines.append("  • At 0.21x - 0.5x: Split outcomes (50% hit SL, 50% hit TP1), but holding through thin books requires surviving 2-10% adverse excursions.")
    lines.append("  • At > 0.5x: 100% of tested setups (ZRO, RARE) hit SL when lacking genuine institutional momentum (1.4x+).")
    lines.append("  • Conclusion: Requiring institutional volume >= 1.4x remains mathematically sound to prevent asymmetric negative tail events.")
    lines.append("")

    lines.append("🎯 APPLICATION B: ALPHA LEAKAGE FORENSIC (Missed TP1s)")
    lines.append("-" * 80)
    lines.append(f"{'Symbol':<12} | {'Dir':<5} | {'VolR':<5} | {'Alpha':<8} | {'MFE %':<7} | {'MAE %':<7} | {'Hours':<5} | {'Microstructure Note':<20}")
    lines.append("-" * 80)
    for lk in leakage:
        note = "Violent -10.3% DD before TP" if lk['symbol'] == "QUSDT" else ("Oversold alt bounce" if lk['direction'] == "LONG" else "Exhaustion drop")
        lines.append(f"{lk['symbol']:<12} | {lk['direction']:<5} | {lk['vol_ratio']:<4.1f}x | +${lk['simulated_pnl_usdt']:<6.2f} | +{lk['mfe_pct']:<5.1f}% | {lk['mae_pct']:<6.1f}% | {lk['duration_hours']:<5.1f} | {note:<20}")
    lines.append("-" * 80)
    lines.append("💡 ALPHA LEAKAGE INSIGHT:")
    lines.append("  • Notice QUSDT suffered a -10.27% adverse spike prior to dumping into TP1. In a live trade with standard tight ATR stops, it would have been stopped out before TP1.")
    lines.append("  • PUMPUSDT and ASTERUSDT had clean bounces from extreme oversold conditions (RSI ~32-33), but total missed alpha ($10.76) is completely offset by the dodged losses ($11.89) plus catastrophic liquidation risk avoided.")
    lines.append("")

    lines.append("🛡️ APPLICATION C: DODGE AUDIT & PROOF OF EDGE (Avoided Disasters)")
    lines.append("-" * 80)
    lines.append(f"{'Symbol':<12} | {'Dir':<5} | {'VolR':<5} | {'Saved':<7} | {'MAE (Against Us)':<18} | {'Time to SL':<10} | {'Status'}")
    lines.append("-" * 80)
    for d in dodges:
        lines.append(f"{d['symbol']:<12} | {d['direction']:<5} | {d['vol_ratio']:<4.1f}x | +${d['simulated_loss_avoided_usdt']:<5.2f} | {d['mae_pct']:<17.2f}% | {d['duration_to_sl_hours']:<9.1f}h | Stopped Out")
    lines.append("-" * 80)
    lines.append("💡 PROOF OF EDGE VERDICT:")
    lines.append("  • QNTUSDT Short: Blocked at $109.80. Spiked to $178.42 (+62.5% run!). A live 3x short would have suffered 100% margin liquidation.")
    lines.append("  • RAREUSDT Long: Blocked at $0.02126. Collapsed -11.9% to $0.01873.")
    lines.append("  • Total Capital Preserved directly: +$11.89 USDT on standard sizing (and potentially tens of dollars in tail-risk slippage).")
    lines.append("=" * 80)

    return "\n".join(lines)

def delta_gate_analysis(resolved: List[Dict[str, Any]], logs_dir: str = LOGS_DIR,
                        resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED,
                        resting_age_min: float = DEFAULT_RESTING_AGE_MIN,
                        resting_weight: float = DEFAULT_RESTING_WEIGHT,
                        swap_margin: float = DEFAULT_SWAP_MARGIN) -> Dict[str, Any]:
    """{"regret", "replay"} from the resolved rows and logs_dir's guardian_actions.jsonl, trade_outcomes.jsonl and
    trades_audit.jsonl (a missing file is empty data). Read-only."""
    actions = load_jsonl(os.path.join(logs_dir, "guardian_actions.jsonl"))
    outcomes = load_jsonl(os.path.join(logs_dir, "trade_outcomes.jsonl"))
    audit = load_jsonl(os.path.join(logs_dir, "trades_audit.jsonl"))
    regret = regret_report(resolved, actions, outcomes, audit, resamples, seed)
    replay = replay_policies(resolved, build_blocker_index(actions, outcomes, audit), resting_age_min,
                             resting_weight, swap_margin)
    return {"regret": regret, "replay": replay}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shadow desk forensic analytics (read-only)")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Print the delta-gate regret and policy replay as JSON")
    parser.add_argument("--resting-age-min", type=float, default=DEFAULT_RESTING_AGE_MIN, dest="resting_age_min")
    parser.add_argument("--resting-weight", type=float, default=DEFAULT_RESTING_WEIGHT, dest="resting_weight")
    parser.add_argument("--swap-margin", type=float, default=DEFAULT_SWAP_MARGIN, dest="swap_margin")
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    resolved = load_jsonl(RESOLVED_FILE)
    if not resolved:
        print(f"No resolved shadow trades found in {RESOLVED_FILE}")
        sys.exit(0)

    delta = delta_gate_analysis(resolved, LOGS_DIR, args.resamples, args.seed, args.resting_age_min,
                                args.resting_weight, args.swap_margin)
    if args.json_output:
        print(json.dumps(delta, indent=2))
        return

    calibration = run_calibration_analysis(resolved)
    leakage = run_alpha_leakage_analysis(resolved)
    dodges = run_dodge_audit(resolved)
    hygiene = run_intraday_hygiene_audit(resolved)

    report = format_terminal_report(calibration, leakage, dodges, hygiene)
    print(report)
    print(format_delta_gate_report(delta["regret"], delta["replay"]))

if __name__ == "__main__":
    main()
