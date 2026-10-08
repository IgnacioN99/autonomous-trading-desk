#!/usr/bin/env python3
"""
score_calibration.py - Calibration of the heuristic radar score against resolved PROD outcomes (issue #202).

The radar `confidence` (copied into the dossier as `score`) is a heuristic point score, not a probability. This
module buckets resolved trades by their dossier score and tells whether a bucket has earned autonomous Tier S
execution: n >= min_trades resolved non-YOLO PROD trades whose one-sided 95% lower confidence bound of the mean net R
(mean - 1.645 x sd / sqrt(n), sd with n-1) is above 0. Only the Tier S buckets 80-89 / 90-95 can clear a Tier S.

Pure and network-free (stdlib only). Shared by scripts/trading_scorecard.py (the sole writer of the store),
scripts/execute_futures_trade.py and scripts/hooks/pre_trade_guard.py (readers, through the same helpers and
the same message).

Store: logs/score_calibration.json = {schema_version, generated_at_ts, env: "PROD", min_trades, trades: {key:
minimal outcome row}, buckets: {label: stats}, unscored, out_of_range}. `trades` is keyed symbol|direction|entry_ts
and merged across scorecard runs, so n grows beyond one trade_outcomes.py --since window without double counting.

Gate (PROD only): an approved, unconfirmed, non-YOLO Tier S candidate whose bucket is not calibrated needs the
user's explicit confirmation (--confirmed), exactly like Tier A+/A. The bucket also counts only when the stored
dossier record's radar snapshot confidence equals the dossier score (YOLO trades never enter the buckets). Any read
or parse problem means not calibrated
(fail closed); it is never a rejection of the trade and never applies to risk-reducing commands.
"""

import json
import math
import os
import time
from typing import Any, Dict, Iterable, Optional, Tuple

BUCKETS = [(55, 64), (65, 74), (75, 79), (80, 89), (90, 95)]
MIN_SAMPLE = 20                       # display: below it a bucket is marked insufficient
DEFAULT_MIN_TRADES = 30               # profile tier_s_calibration_min_trades default
MAX_AGE_S = 7 * 86400                 # an older store counts as not calibrated
FUTURE_TOLERANCE_S = 300
STORE_ENV = "PROD"
SCHEMA_VERSION = 1
STORE_REL_PATH = os.path.join("logs", "score_calibration.json")


def _label(lo: int, hi: int) -> str:
    return f"{lo}-{hi}"


BUCKET_LABELS = [_label(lo, hi) for lo, hi in BUCKETS]
TIER_S_BUCKETS = ("80-89", "90-95")  # the only buckets that can clear an autonomous Tier S


def _norm_env(env: Any) -> str:
    e = str(env or "").strip().lower()
    return "prod" if e in ("prod", "production", "mainnet") else e


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _int_score(value: Any) -> Optional[int]:
    val = _num(value)
    if val is None or val < 0 or val > 100:
        return None
    return int(round(val))


def bucket_for(score: Any) -> Optional[str]:
    """Bucket label ("55-64", ...) of a score; None for a missing score or one outside every bucket."""
    s = _int_score(score)
    if s is None:
        return None
    return next((_label(lo, hi) for lo, hi in BUCKETS if lo <= s <= hi), None)


def bucket_score(row: dict) -> Optional[int]:
    """The score calibration and the gate key on: the provenance-bound dossier score (never the radar score)."""
    return _int_score((row or {}).get("dossier_score"))


def _mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


LCB_Z = 1.645  # one-sided 95%


def lower_confidence_bound(values) -> Tuple[Optional[float], Optional[float]]:
    """(sd, lcb95): sample standard deviation (n-1) of the net R values and the one-sided 95% lower confidence
    bound of their mean, mean - 1.645 * sd / sqrt(n). (None, None) when n < 2 (undefined sd: never calibrated)."""
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n < 2:
        return None, None
    mean = sum(vals) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
    return round(sd, 4), round(mean - LCB_Z * sd / math.sqrt(n), 4)


def build_calibration(rows: Iterable[dict], env: str = STORE_ENV, min_trades: int = DEFAULT_MIN_TRADES) -> dict:
    """Bucket table over the rows of `env` (the gate's env, PROD) that are closed with a non-null net R."""
    want = _norm_env(env)
    per = {label: [] for label in BUCKET_LABELS}
    unscored = out_of_range = 0
    for row in rows or []:
        if not isinstance(row, dict) or _norm_env(row.get("env")) != want or row.get("status") != "closed":
            continue
        if row.get("is_yolo") is True:
            continue  # YOLO trades never calibrate the Tier S gate (PR #204 review)
        r_net = _num(row.get("realized_r_net"))
        if r_net is None:
            continue
        score = bucket_score(row)
        if score is None:
            unscored += 1
            continue
        label = bucket_for(score)
        if label is None:
            out_of_range += 1
            continue
        per[label].append((r_net, _num(row.get("mfe_r")), _num(row.get("score"))))
    buckets = {}
    for label, items in per.items():
        n = len(items)
        wins = sum(1 for r, _, _ in items if r > 0)
        expectancy = _mean(r for r, _, _ in items)
        sd, lcb = lower_confidence_bound([r for r, _, _ in items])
        buckets[label] = {
            "n": n, "wins": wins,
            "win_rate": round(wins / n, 4) if n else None,
            "expectancy_r_net": expectancy,
            "sd_r_net": sd,
            "lcb95_r_net": lcb,
            "mean_mfe_r": _mean(m for _, m, _ in items),
            "mean_radar_score": _mean(s for _, _, s in items),
            "insufficient": n < MIN_SAMPLE,
            "calibrated": n >= min_trades and lcb is not None and lcb > 0,
        }
    return {"env": str(env).upper(), "min_trades": min_trades, "buckets": buckets, "unscored": unscored,
            "out_of_range": out_of_range}


def trade_key(row: dict) -> str:
    return f"{str(row.get('symbol') or '').upper()}|{str(row.get('direction') or '').upper()}|{row.get('entry_ts')}"


_STORE_FIELDS = ("symbol", "direction", "entry_ts", "env", "status", "is_yolo", "dossier_score", "score",
                 "realized_r_net", "mfe_r")


def merge_store(existing: Optional[dict], rows: Iterable[dict], now: Optional[float] = None,
                min_trades: int = DEFAULT_MIN_TRADES) -> dict:
    """New store: the existing `trades` map updated with this run's closed PROD rows (same key = same trade, the
    newer row wins), then the buckets recomputed from the whole map."""
    trades = {}
    if isinstance(existing, dict) and isinstance(existing.get("trades"), dict):
        trades = {k: v for k, v in existing["trades"].items() if isinstance(v, dict)}
    for row in rows or []:
        if (isinstance(row, dict) and _norm_env(row.get("env")) == "prod" and row.get("status") == "closed"
                and row.get("is_yolo") is not True):
            trades[trade_key(row)] = dict({k: row.get(k) for k in _STORE_FIELDS}, env="prod")
    store = build_calibration(trades.values(), STORE_ENV, min_trades)
    store.update(schema_version=SCHEMA_VERSION, generated_at_ts=int(time.time() if now is None else now),
                 trades=trades)
    return store


def store_path(base_dir: str) -> str:
    return os.path.join(base_dir, STORE_REL_PATH)


def load_calibration_with_reason(base_dir: str) -> Tuple[Optional[dict], Optional[str]]:
    """(store, None), or (None, "calibration store missing" | "calibration store unreadable")."""
    path = store_path(base_dir)
    if not os.path.exists(path):
        return None, "calibration store missing"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, "calibration store unreadable"
    if not isinstance(data, dict):
        return None, "calibration store unreadable"
    return data, None


def load_calibration(base_dir: str) -> Optional[dict]:
    return load_calibration_with_reason(base_dir)[0]


def bucket_is_calibrated(cal: Optional[dict], score: Any, env: str = STORE_ENV, now: Optional[float] = None,
                         max_age_s: int = MAX_AGE_S, min_trades: int = DEFAULT_MIN_TRADES) -> Tuple[bool, str]:
    """(True, reason) only when the store is a fresh store of `env` and the score's bucket has n >= min_trades
    and lcb95_r_net > 0; (False, reason) otherwise (missing data always means not calibrated)."""
    if cal is None:
        return False, "calibration store missing or unreadable"
    gen = _num(cal.get("generated_at_ts")) if isinstance(cal, dict) else None
    if gen is None or not isinstance(cal.get("buckets"), dict):
        return False, "calibration store malformed"
    if str(cal.get("env") or "").upper() != str(env or "").upper():
        return False, f"calibration store env {cal.get('env')!r} != {str(env).upper()}"
    now = time.time() if now is None else now
    age = now - gen
    if age > max_age_s or age < -FUTURE_TOLERANCE_S:
        return False, f"calibration store stale (generated {int(age // 3600)} h ago, max {max_age_s // 86400} d)"
    s = _int_score(score)
    if s is None:
        return False, "no dossier score"
    label = bucket_for(s)
    if label is None:
        return False, f"score {s} outside the calibration buckets"
    b = cal["buckets"].get(label)
    if b is None:
        b = {"n": 0, "expectancy_r_net": None}
    if not isinstance(b, dict):
        return False, "calibration store malformed"
    n = _num(b.get("n"))
    if n is None:
        return False, "calibration store malformed"
    n = int(n)
    if n < min_trades:
        return False, f"n={n} < {min_trades}"
    lcb = _num(b.get("lcb95_r_net"))
    if lcb is None or lcb <= 0:
        return False, (f"net R lower 95% bound {'n/a' if lcb is None else format(lcb, '+.4f') + 'R'} <= 0 over "
                       f"n={n}")
    return True, f"bucket {label} calibrated (n={n}, net R lower 95% bound {lcb:+.4f}R)"


def calibration_policy(profile: Any) -> Tuple[bool, int]:
    """(require_calibrated_tier_s, tier_s_calibration_min_trades) from the profile. Only an explicit boolean False
    turns the gate off (missing or malformed = on); min_trades must be an int >= 1, else the default."""
    prof = profile if isinstance(profile, dict) else {}
    require = prof.get("require_calibrated_tier_s", True) is not False
    raw = prof.get("tier_s_calibration_min_trades", DEFAULT_MIN_TRADES)
    min_trades = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1 else DEFAULT_MIN_TRADES
    return require, min_trades


def candidate_is_tier_s(cand: Any) -> bool:
    """Tier label "S", "Tier S" or "Tier S (...)" (case-insensitive)."""
    if not isinstance(cand, dict):
        return False
    tokens = str(cand.get("tier") or "").upper().replace("TIER", " ").split()
    return bool(tokens) and tokens[0] == "S"


def confirmation_reason(score: Any, reason: str) -> str:
    """The single message both gates emit for an uncalibrated Tier S bucket."""
    label = bucket_for(score) or ("unscored" if _int_score(score) is None else f"score {_int_score(score)}")
    return (f"Tier S score bucket {label} not calibrated ({reason}): ask the user and rerun with --confirmed "
            "(the score is a heuristic, not a probability).")


def tier_s_confirmation_required(cand: Any, env: str, profile: Any, base_dir: str,
                                 now: Optional[float] = None) -> Optional[str]:
    """Message when an otherwise fast-tracked Tier S candidate needs the user's confirmation because its dossier
    score bucket is not calibrated; None when the check does not apply (not PROD, flag off, not Tier S) or the
    bucket is calibrated. Callers invoke it only for unconfirmed, non-YOLO candidates that would not already ask."""
    if _norm_env(env) != "prod" or not candidate_is_tier_s(cand):
        return None
    require, min_trades = calibration_policy(profile)
    if not require:
        return None
    score = cand.get("score")
    try:
        label = bucket_for(score)
        if label is not None and label not in TIER_S_BUCKETS:
            # a Tier S label never borrows a lower bucket's calibration (PR #204 re-review)
            ok, reason = False, f"tier_s_score_below_80 (score {_int_score(score)})"
        else:
            cal, load_reason = load_calibration_with_reason(base_dir)
            if cal is None:
                ok, reason = False, load_reason
            else:
                ok, reason = bucket_is_calibrated(cal, score, STORE_ENV, now=now, min_trades=min_trades)
        if ok:
            ok, reason = radar_snapshot_matches(cand, base_dir)
    except Exception as e:  # fail closed
        ok, reason = False, f"calibration check failed ({type(e).__name__})"
    return None if ok else confirmation_reason(score, reason)


DOSSIER_REL_PATH = os.path.join("logs", "evaluations", "latest_dossier.json")


def radar_snapshot_matches(cand: dict, base_dir: str) -> Tuple[bool, str]:
    """(True, reason) only when the stored latest dossier record carries radar_snapshots["SYMBOL|DIRECTION"] (joined
    by record_evaluation.py from the brief's radar rows) whose `confidence` equals the dossier `score` exactly, so the
    evaluator cannot pick a calibrated bucket by writing a different score. The record is bound to the dossier the
    gate validated: its provenance sha256 must equal the candidate's `dossier_sha256` (else `dossier_changed`, as
    in the executor's read_radar_snapshot). Any read problem is not a match."""
    key = f"{str(cand.get('symbol') or '').upper()}|{str(cand.get('direction') or '').upper()}"
    try:
        with open(os.path.join(base_dir, DOSSIER_REL_PATH), "r", encoding="utf-8") as f:
            record = json.load(f)
        snaps = record.get("radar_snapshots") if isinstance(record, dict) else None
        prov = record.get("provenance") if isinstance(record, dict) else None
        stored_sha = prov.get("sha256") if isinstance(prov, dict) else None
    except Exception:
        return False, "radar_snapshot_unreadable"
    if not cand.get("dossier_sha256") or stored_sha != cand.get("dossier_sha256"):
        return False, "dossier_changed"
    entry = snaps.get(key) if isinstance(snaps, dict) else None
    row = entry.get("radar_snapshot") if isinstance(entry, dict) else None
    if not isinstance(row, dict):
        return False, "radar_snapshot_missing"
    dossier_score, radar_score = _int_score(cand.get("score")), _num(row.get("confidence"))
    if dossier_score is None or radar_score is None or radar_score != int(radar_score) \
            or int(radar_score) != dossier_score:
        shown = "n/a" if radar_score is None else (int(radar_score) if radar_score == int(radar_score) else radar_score)
        return False, f"score_mismatch (dossier {cand.get('score')} vs radar {shown})"
    return True, "radar snapshot matches the dossier score"
