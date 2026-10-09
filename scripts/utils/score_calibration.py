#!/usr/bin/env python3
"""
score_calibration.py - Calibration of the heuristic radar score against resolved PROD outcomes (issue #202).

The radar `confidence` (copied into the dossier as `score`) is a heuristic point score, not a probability. This
module buckets resolved trades by their dossier score and tells whether a bucket has earned autonomous Tier S
execution: n >= min_trades resolved non-YOLO PROD trades whose one-sided 95% lower confidence bound of the mean net R
(mean - t95(n-1) x sd / sqrt(n), sd with n-1, t95 = Student's t one-sided 95% critical value, issue #207) is above the
profile margin `tier_s_calibration_min_lcb_r` (default +0.1R). Only the Tier S buckets 80-89 / 90-95 can clear a
Tier S. Buckets use only rows of the current SCORE_SCHEMA_VERSION (`score_schema_version`; a row without it is v1):
older rows stay in the store and are counted in `excluded_schema`. A store whose own `score_schema_version` is not
the current one (e.g. a pre-#207 z-based store) is never calibrated (`store_schema_outdated`).

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
# Version of the radar score itself (issue #207): v2 = after the #202 cap at 74 for rows without volume or wick and
# the #206 squeeze cap. The radar stamps it on its rows; rows without it are v1. Calibration counts the current one only.
SCORE_SCHEMA_VERSION = 2
DEFAULT_MIN_LCB_R = 0.1               # profile tier_s_calibration_min_lcb_r default: lcb95 must be above this
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


LCB_Z = 1.645  # one-sided 95% normal quantile: the t critical value beyond the table (df > 120)
# One-sided 95% Student's t critical values by degrees of freedom (issue #207; stdlib only, the hook imports this
# module). Between two listed df the value is interpolated linearly; above 120 it is LCB_Z.
T95_TABLE = {1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015, 6: 1.943, 7: 1.895, 8: 1.860, 9: 1.833, 10: 1.812,
             11: 1.796, 12: 1.782, 13: 1.771, 14: 1.761, 15: 1.753, 16: 1.746, 17: 1.740, 18: 1.734, 19: 1.729,
             20: 1.725, 21: 1.721, 22: 1.717, 23: 1.714, 24: 1.711, 25: 1.708, 26: 1.706, 27: 1.703, 28: 1.701,
             29: 1.699, 30: 1.697, 40: 1.684, 60: 1.671, 120: 1.658}


def t95_critical(df: int) -> float:
    """One-sided 95% t critical value for df degrees of freedom (df >= 1): T95_TABLE, linear interpolation between
    its entries, LCB_Z above 120."""
    if df > 120:
        return LCB_Z
    if df in T95_TABLE:
        return T95_TABLE[df]
    keys = sorted(T95_TABLE)
    lo = max(k for k in keys if k < df)
    hi = min(k for k in keys if k > df)
    return T95_TABLE[lo] + (T95_TABLE[hi] - T95_TABLE[lo]) * (df - lo) / (hi - lo)


def lower_confidence_bound(values) -> Tuple[Optional[float], Optional[float]]:
    """(sd, lcb95): sample standard deviation (n-1) of the net R values and the one-sided 95% lower confidence
    bound of their mean, mean - t95(n-1) * sd / sqrt(n). (None, None) when n < 2 (undefined sd: never calibrated)."""
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n < 2:
        return None, None
    mean = sum(vals) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
    return round(sd, 4), round(mean - t95_critical(n - 1) * sd / math.sqrt(n), 4)


def row_schema_version(row: dict) -> int:
    """The row's score_schema_version (1 when missing or not an int)."""
    v = (row or {}).get("score_schema_version")
    return v if isinstance(v, int) and not isinstance(v, bool) else 1


def build_calibration(rows: Iterable[dict], env: str = STORE_ENV, min_trades: int = DEFAULT_MIN_TRADES,
                      min_lcb_r: float = DEFAULT_MIN_LCB_R) -> dict:
    """Bucket table over the rows of `env` (the gate's env, PROD) that are closed with a non-null net R and carry the
    current SCORE_SCHEMA_VERSION (others count in excluded_schema). calibrated: n >= min_trades and lcb95 > min_lcb_r."""
    want = _norm_env(env)
    per = {label: [] for label in BUCKET_LABELS}
    unscored = out_of_range = excluded_schema = 0
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
        if row_schema_version(row) != SCORE_SCHEMA_VERSION:
            excluded_schema += 1  # scored by an older radar formula (issue #207)
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
            "calibrated": n >= min_trades and lcb is not None and lcb > min_lcb_r,
        }
    return {"env": str(env).upper(), "min_trades": min_trades, "min_lcb_r": min_lcb_r,
            "score_schema_version": SCORE_SCHEMA_VERSION, "buckets": buckets, "unscored": unscored,
            "out_of_range": out_of_range, "excluded_schema": excluded_schema}


def trade_key(row: dict) -> str:
    return f"{str(row.get('symbol') or '').upper()}|{str(row.get('direction') or '').upper()}|{row.get('entry_ts')}"


_STORE_FIELDS = ("symbol", "direction", "entry_ts", "audit_ts", "env", "status", "is_yolo", "dossier_score",
                 "score", "realized_r_net", "mfe_r", "score_schema_version")
LEGACY_ENTRY_SLACK_MS = 120 * 1000  # trade_outcomes.ENTRY_FILL_SLACK_MS: pre-#210 entry_ts = audit_ts*1000 - this


def _same_trade(stored: dict, row: dict) -> bool:
    """stored and row describe the same audit record (PR #212): same symbol + direction and the same audit_ts, or a
    pre-#210 stored row (no audit_ts) whose legacy entry_ts was audit_ts*1000 - LEGACY_ENTRY_SLACK_MS."""
    if (str(stored.get("symbol") or "").upper(), str(stored.get("direction") or "").upper()) != \
            (str(row.get("symbol") or "").upper(), str(row.get("direction") or "").upper()):
        return False
    audit = _num(row.get("audit_ts"))
    if audit is None:
        return False
    stored_audit = _num(stored.get("audit_ts"))
    if stored_audit is not None:
        return stored_audit == audit
    return _num(stored.get("entry_ts")) == int(audit * 1000) - LEGACY_ENTRY_SLACK_MS


def merge_store(existing: Optional[dict], rows: Iterable[dict], now: Optional[float] = None,
                min_trades: int = DEFAULT_MIN_TRADES, min_lcb_r: float = DEFAULT_MIN_LCB_R) -> dict:
    """New store: the existing `trades` map updated with this run's closed PROD rows (same key = same trade, the
    newer row wins), then the buckets recomputed from the whole map. PR #212: a closed row REPLACES any earlier key of
    the same audit record (re-keyed entry_ts after better entry matching, or a pre-#210 legacy key), so one audit
    record is counted at most once. Nothing is ever deleted otherwise: a `truncated` (degraded fills), open,
    no_entry_fill or fills_unavailable row leaves the stored trades untouched, because dropping a stored loss could
    lift a bucket over its calibration threshold (that would loosen the Tier S confirmation gate)."""
    trades = {}
    if isinstance(existing, dict) and isinstance(existing.get("trades"), dict):
        trades = {k: v for k, v in existing["trades"].items() if isinstance(v, dict)}
    for row in rows or []:
        if (not isinstance(row, dict) or _norm_env(row.get("env")) != "prod" or row.get("is_yolo") is True
                or row.get("status") != "closed" or row.get("truncated") is True):
            continue
        key = trade_key(row)
        for old in [k for k, v in trades.items() if k != key and _same_trade(v, row)]:
            del trades[old]  # same audit record under its previous key: replaced, not added
        trades[key] = dict({k: row.get(k) for k in _STORE_FIELDS}, env="prod")
    store = build_calibration(trades.values(), STORE_ENV, min_trades, min_lcb_r)
    store.update(schema_version=SCHEMA_VERSION, generated_at_ts=int(time.time() if now is None else now),
                 trades=trades)
    return store


def store_path(base_dir: str) -> str:
    return os.path.join(base_dir, STORE_REL_PATH)


STORE_SCHEMA_OUTDATED = "store_schema_outdated"


def store_schema_current(cal: Any) -> bool:
    """True when the store was built for the current SCORE_SCHEMA_VERSION (issue #207: a pre-#207 store has no
    score_schema_version and z-based bounds, so it can never calibrate)."""
    return isinstance(cal, dict) and row_schema_version(cal) == SCORE_SCHEMA_VERSION


def load_calibration_with_reason(base_dir: str, require_current: bool = False) -> Tuple[Optional[dict], Optional[str]]:
    """(store, None), or (None, "calibration store missing" | "calibration store unreadable"). require_current (the
    gate): a store of another score_schema_version is (None, STORE_SCHEMA_OUTDATED). The scorecard (the store's
    writer) reads it without the check so stored trades are kept when it rebuilds the buckets."""
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
    if require_current and isinstance(data.get("buckets"), dict) and not store_schema_current(data):
        return None, STORE_SCHEMA_OUTDATED  # a malformed store keeps its own reason (bucket_is_calibrated)
    return data, None


def load_calibration(base_dir: str) -> Optional[dict]:
    return load_calibration_with_reason(base_dir)[0]


def bucket_is_calibrated(cal: Optional[dict], score: Any, env: str = STORE_ENV, now: Optional[float] = None,
                         max_age_s: int = MAX_AGE_S, min_trades: int = DEFAULT_MIN_TRADES,
                         min_lcb_r: float = DEFAULT_MIN_LCB_R) -> Tuple[bool, str]:
    """(True, reason) only when the store is a fresh store of `env` and the score's bucket has n >= min_trades
    and lcb95_r_net > min_lcb_r (the current profile margin, re-checked here against the stored bound, so a profile
    change applies without rerunning the scorecard); (False, reason) otherwise (missing data always means not
    calibrated)."""
    if cal is None:
        return False, "calibration store missing or unreadable"
    gen = _num(cal.get("generated_at_ts")) if isinstance(cal, dict) else None
    if gen is None or not isinstance(cal.get("buckets"), dict):
        return False, "calibration store malformed"
    if str(cal.get("env") or "").upper() != str(env or "").upper():
        return False, f"calibration store env {cal.get('env')!r} != {str(env).upper()}"
    if not store_schema_current(cal):
        return False, STORE_SCHEMA_OUTDATED
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
    if lcb is None or lcb <= min_lcb_r:
        return False, (f"net R lower 95% bound {'n/a' if lcb is None else format(lcb, '+.4f') + 'R'} <= "
                       f"{min_lcb_r:g}R over n={n}")
    return True, f"bucket {label} calibrated (n={n}, net R lower 95% bound {lcb:+.4f}R > {min_lcb_r:g}R)"


def calibration_policy(profile: Any) -> Tuple[bool, int, float]:
    """(require_calibrated_tier_s, tier_s_calibration_min_trades, tier_s_calibration_min_lcb_r) from the profile.
    Only an explicit boolean False turns the gate off (missing or malformed = on); min_trades must be an int >= 1 and
    min_lcb_r a finite number >= 0, else the default (issue #207: +0.1R)."""
    prof = profile if isinstance(profile, dict) else {}
    require = prof.get("require_calibrated_tier_s", True) is not False
    raw = prof.get("tier_s_calibration_min_trades", DEFAULT_MIN_TRADES)
    min_trades = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1 else DEFAULT_MIN_TRADES
    raw = prof.get("tier_s_calibration_min_lcb_r", DEFAULT_MIN_LCB_R)
    margin = _num(raw) if isinstance(raw, (int, float)) else None
    min_lcb_r = float(margin) if margin is not None and margin >= 0 else DEFAULT_MIN_LCB_R
    return require, min_trades, min_lcb_r


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
    bucket is calibrated. Callers invoke it only for unconfirmed, non-YOLO candidates that would not already ask.
    Checked first, for any tier and whatever the profile: a radar-flagged squeeze SHORT (squeeze_confirmation_required,
    issue #206), then a SHORT without a usable radar snapshot (snapshot_confirmation_required, issue #207)."""
    if _norm_env(env) != "prod":
        return None
    squeeze_msg = (squeeze_confirmation_required(cand, env, base_dir)  # any tier, whatever the profile (#206)
                   or snapshot_confirmation_required(cand, env, base_dir))
    if squeeze_msg:
        return squeeze_msg
    if not candidate_is_tier_s(cand):
        return None
    require, min_trades, min_lcb_r = calibration_policy(profile)
    if not require:
        return None
    score = cand.get("score")
    try:
        label = bucket_for(score)
        if label is not None and label not in TIER_S_BUCKETS:
            # a Tier S label never borrows a lower bucket's calibration (PR #204 re-review)
            ok, reason = False, f"tier_s_score_below_80 (score {_int_score(score)})"
        else:
            cal, load_reason = load_calibration_with_reason(base_dir, require_current=True)
            if cal is None:
                ok, reason = False, load_reason
            else:
                ok, reason = bucket_is_calibrated(cal, score, STORE_ENV, now=now, min_trades=min_trades,
                                                  min_lcb_r=min_lcb_r)
        if ok:
            ok, reason = radar_snapshot_matches(cand, base_dir)
    except Exception as e:  # fail closed
        ok, reason = False, f"calibration check failed ({type(e).__name__})"
    return None if ok else confirmation_reason(score, reason)


DOSSIER_REL_PATH = os.path.join("logs", "evaluations", "latest_dossier.json")


SQUEEZE_CONFIRMATION_REASON = "squeeze_risk SHORT: user confirmation required"


def squeeze_confirmation_message() -> str:
    """The single message both gates emit for a radar-flagged squeeze SHORT (issue #206)."""
    return f"{SQUEEZE_CONFIRMATION_REASON}: ask the user and rerun with --confirmed (RULE 9: at most Tier A)."


def squeeze_confirmation_required(cand: Any, env: str, base_dir: str) -> Optional[str]:
    """Mechanical backstop for evaluator RULE 9 (issue #206): in PROD, an otherwise fast-tracked candidate whose
    radar snapshot (bound to the validated dossier) has `squeeze_risk: true` needs the user's confirmation, whatever
    its tier label and whatever `require_calibrated_tier_s` says. A missing or unreadable snapshot does not trigger
    this message: for a SHORT, snapshot_confirmation_required asks instead (issue #207)."""
    if _norm_env(env) != "prod" or not isinstance(cand, dict):
        return None
    row, _ = _radar_snapshot_row(cand, base_dir)
    return squeeze_confirmation_message() if isinstance(row, dict) and row.get("squeeze_risk") is True else None


SNAPSHOT_UNAVAILABLE_REASON = "radar snapshot unavailable for SHORT: user confirmation required"


def snapshot_confirmation_required(cand: Any, env: str, base_dir: str) -> Optional[str]:
    """Issue #207 (PR #214 review): in PROD, an otherwise fast-tracked SHORT whose radar snapshot is missing,
    unreadable, stale, from another environment or not bound to the validated dossier (no row to check squeeze_risk
    on) needs the user's confirmation, whatever its tier and profile. LONGs are unaffected."""
    if _norm_env(env) != "prod" or not isinstance(cand, dict) or str(cand.get("direction") or "").upper() != "SHORT":
        return None
    row, reason = _radar_snapshot_row(cand, base_dir)
    if isinstance(row, dict):
        return None
    return f"{SNAPSHOT_UNAVAILABLE_REASON} ({reason}): ask the user and rerun with --confirmed (RULE 9 unverifiable)."


def _radar_snapshot_row(cand: dict, base_dir: str) -> Tuple[Optional[dict], str]:
    """(row, "") for the stored latest dossier record's radar_snapshots["SYMBOL|DIRECTION"], bound to the validated
    dossier by its provenance sha256; (None, reason) on any read problem or mismatch."""
    key = f"{str(cand.get('symbol') or '').upper()}|{str(cand.get('direction') or '').upper()}"
    try:
        with open(os.path.join(base_dir, DOSSIER_REL_PATH), "r", encoding="utf-8") as f:
            record = json.load(f)
        snaps = record.get("radar_snapshots") if isinstance(record, dict) else None
        prov = record.get("provenance") if isinstance(record, dict) else None
        stored_sha = prov.get("sha256") if isinstance(prov, dict) else None
    except Exception:
        return None, "radar_snapshot_unreadable"
    if not cand.get("dossier_sha256") or stored_sha != cand.get("dossier_sha256"):
        return None, "dossier_changed"
    entry = snaps.get(key) if isinstance(snaps, dict) else None
    row = entry.get("radar_snapshot") if isinstance(entry, dict) else None
    if not isinstance(row, dict):
        return None, "radar_snapshot_missing"
    return row, ""


def radar_snapshot_matches(cand: dict, base_dir: str) -> Tuple[bool, str]:
    """(True, reason) only when the stored latest dossier record carries radar_snapshots["SYMBOL|DIRECTION"] (joined
    by record_evaluation.py from the brief's radar rows) whose `confidence` equals the dossier `score` exactly, so the
    evaluator cannot pick a calibrated bucket by writing a different score. The record is bound to the dossier the
    gate validated: its provenance sha256 must equal the candidate's `dossier_sha256` (else `dossier_changed`, as
    in the executor's read_radar_snapshot). Any read problem is not a match."""
    row, reason = _radar_snapshot_row(cand, base_dir)
    if row is None:
        return False, reason
    dossier_score, radar_score = _int_score(cand.get("score")), _num(row.get("confidence"))
    if dossier_score is None or radar_score is None or radar_score != int(radar_score) \
            or int(radar_score) != dossier_score:
        shown = "n/a" if radar_score is None else (int(radar_score) if radar_score == int(radar_score) else radar_score)
        return False, f"score_mismatch (dossier {cand.get('score')} vs radar {shown})"
    return True, "radar snapshot matches the dossier score"
