#!/usr/bin/env python3
"""
trading_scorecard.py - Quantitative Scorecard and Strategy Meta-Improver (issue #200).

Offline report: it never calls Binance and never rebuilds outcomes. Run scripts/trade_outcomes.py first.

Source: logs/trade_outcomes.jsonl (or --outcomes PATH), one row per trade reconstructed from Binance fills. Only
rows with status "closed" and env == --env are scored; rows without "env" (written before issue #200), other envs,
open / fills_unavailable rows and rows without realized R are excluded and counted in "excluded".
1. Metrics over the resolved trades: R basis = realized_r_net, else realized_r_gross (counted in
   r_basis.gross_fallback); win rate (R > 0 over resolved n), profit factor and expectancy in R, average win / loss,
   mean gross / net R; then USDT: gross = sum of the legs' realized_pnl, net = sum of realized_r_net x initial_risk x
   filled_qty (null when any input is null).
2. Tier breakdown (S, A+, A, YOLO, unknown): YOLO when is_yolo; else the dossier_tier persisted in the row (issue
   #202); else the tier of a matching approved candidate of logs/evaluations/latest_dossier.json (same symbol +
   direction, entry fill within [timestamp_ts, valid_until_ts + 90 min]; fallback for older trades); else unknown.
   Never inferred from leverage.
3. Score calibration (issue #202, PROD only, the gate's env): closed PROD rows with net R, bucketed by their dossier
   score (a heuristic, not a probability), merged by trade key into logs/score_calibration.json across runs (the CLI
   writes it on every run with PROD rows read from logs/trade_outcomes.jsonl, never from a custom --outcomes file;
   this script is its sole writer). Per bucket n, win rate, net expectancy, sd and lcb95 of net R,
   mean MFE, mean radar score, insufficient (n < MIN_SAMPLE) and calibrated (n >= profile
   tier_s_calibration_min_trades and lcb95 = mean - t95(n-1) x sd / sqrt(n) > tier_s_calibration_min_lcb_r, default
   +0.1R, issue #207); YOLO rows are excluded; rows without a dossier score count as unscored; rows of an older
   score_schema_version (or none) are kept in the store but excluded from the buckets (excluded_schema).
4. Loss-cause clusters from logs/trade_insights.jsonl (env-agnostic).
5. Meta-improver: below MIN_SAMPLE resolved trades only an insufficient-sample line; above it R-based data notes only
   (never instructions: any change needs the user's explicit decision). The profit-factor note needs net R on every
   resolved row (no gross fallback).
6. Shadow desk counterfactual metrics (best effort; an error is reported, never raised).
7. Fees (issue #268, new top-level keys over the resolved trades): "fees" (total / mean fees_r, rows with and
   without it: older rows and non-USDT commissions such as a BNB discount have none), "fees_by_stop_bucket" (stop
   distance <1.5, 1.5-2, 2-3, 3-4, >=4 % of the entry; stop_distance_pct, else initial_risk / entry_price),
   "gross_vs_net" (both expectancies over the same rows: those with gross and net R), "direction_split" (LONG vs
   SHORT: n, win rate, expectancy, mean gross / net R, total R) and "fee_threshold_backtest" (report only, no
   recommendation: per max_fee_r candidate 0.03-0.10, n / net expectancy / total net R of the trades the executor's
   fee-in-R gate would keep vs reject, utils.gate_limits.expected_fee_r of the stop distance; n < 10 flagged
   insufficient_n). The calibration store is unaffected (it keeps its own fields only).
"source" reports the file's age and the env / since stamped in its rows; the human output warns when it is older
than 24 h or its env differs from --env.

Output: logs/trading_scorecard.json (or --out PATH: a file inside logs/ other than the calibration store, else exit 2
before anything is written; written atomically, never through a hard link) and logs/score_calibration.json; stdout =
human report or, with --json, the same JSON. win_rate_pct = trades with R > 0 over every resolved trade: scratches
(R = 0, and near-zero R) count in the denominator as non-wins. Exit 0 for any readable run, 1 when the report cannot be written, 2 on bad arguments.

Usage:
  python3 scripts/trading_scorecard.py [--env prod|testnet] [--json] [--out PATH] [--outcomes PATH]
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.atomic_writer import atomic_write_json, path_inside_dir, same_file
from utils.env_resolver import resolve_env
from utils.lessons import read_active_lessons  # shared active-lesson reader (issue #187)
from utils.position_timing import norm_env
from utils import score_calibration as scal
from utils import gate_limits as gl

MIN_SAMPLE = 20
STALE_SECONDS = 24 * 3600
RESTING_FILL_SLACK_S = 5400  # resting entries may fill up to 90 min after the dossier expires
TIERS = ("S", "A+", "A", "YOLO", "unknown")
# Non-authoritative data notes (PR #203 review): never instructions to an agent
SCALING_MSG = ("Data note: profit factor ≥ 1.8 with positive net expectancy over {n} trades. "
               "Any margin change needs the user's explicit decision.")
CLUSTER_MSG = ("Data note: {count} loss lessons tagged BTC_DUMP_CORRELATION (BTC downside correlation). "
               "Any gate change needs the user's explicit decision.")


def _workspace_dir():
    """Repository root (scripts/..); tests patch it. No executor import (PR #203 review)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _logs_dir():
    return os.path.join(_workspace_dir(), "logs")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_jsonl(path):
    """JSON-object lines of path ([] when missing); malformed lines are skipped."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def load_insights_records() -> list:
    return read_active_lessons(os.path.join(_logs_dir(), "trade_insights.jsonl"))


def _norm_tier(raw):
    text = str(raw or "").upper().replace("TIER", " ")
    tokens = text.split()
    if "YOLO" in tokens:
        return "YOLO"
    if "A+" in tokens:
        return "A+"
    if "S" in tokens:
        return "S"
    if "A" in tokens:
        return "A"
    return "unknown"


def load_dossier_candidates():
    """(candidates, warning): approved candidates of latest_dossier.json with the dossier's validity window. The
    newest scan overall on purpose (issue #270: per-session dossier files are not read here)."""
    path = os.path.join(_logs_dir(), "evaluations", "latest_dossier.json")
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as f:
            dossier = json.load(f)
        start, until = _num(dossier.get("timestamp_ts")), _num(dossier.get("valid_until_ts"))
        out = []
        for c in dossier.get("approved_candidates") or []:
            out.append({"symbol": str(c.get("symbol") or "").upper(), "direction": str(c.get("direction") or "").upper(),
                        "tier": "YOLO" if c.get("is_yolo") is True else _norm_tier(c.get("tier")),
                        "start": start, "until": until})
        return out, None
    except Exception as e:
        return [], f"dossier unreadable ({type(e).__name__}: {e}): all tiers unknown"


def trade_tier(row, candidates):
    if row.get("is_yolo") is True:
        return "YOLO"
    persisted = _norm_tier(row.get("dossier_tier")) if row.get("dossier_tier") else "unknown"
    if persisted != "unknown":
        return persisted  # issue #202: the audit record's own dossier tier beats the latest_dossier join
    entry_s = _num(row.get("entry_ts"))
    if entry_s is None:
        return "unknown"
    entry_s /= 1000.0
    symbol, direction = str(row.get("symbol") or "").upper(), str(row.get("direction") or "").upper()
    for c in candidates:
        if c["symbol"] != symbol or c["direction"] != direction or c["start"] is None or c["until"] is None:
            continue
        if c["start"] <= entry_s <= c["until"] + RESTING_FILL_SLACK_S:
            return c["tier"]
    return "unknown"


def _r_value(row):
    """(R, used_gross_fallback): realized_r_net, else realized_r_gross; (None, False) when both are null."""
    net = _num(row.get("realized_r_net"))
    if net is not None:
        return net, False
    gross = _num(row.get("realized_r_gross"))
    return (gross, True) if gross is not None else (None, False)


def _r_stats(values):
    n = len(values)
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    pos, neg = sum(wins), sum(losses)
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else None,
        "profit_factor_r": round(pos / abs(neg), 4) if losses else None,
        "expectancy_r": round(sum(values) / n, 4) if n else None,
        "avg_win_r": round(pos / len(wins), 4) if wins else None,
        "avg_loss_r": round(neg / len(losses), 4) if losses else None,
    }


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def _usdt(rows):
    gross = net = 0.0
    for row in rows:
        legs = row.get("legs")
        pnls = [_num(l.get("realized_pnl")) if isinstance(l, dict) else None for l in legs] if isinstance(legs, list) else [None]
        if gross is not None:
            gross = None if None in pnls or not pnls else gross + sum(pnls)
        parts = [_num(row.get(k)) for k in ("realized_r_net", "initial_risk", "filled_qty")]
        if net is not None:
            net = None if None in parts else net + parts[0] * parts[1] * parts[2]
    return (round(gross, 2) if gross is not None else None), (round(net, 2) if net is not None else None)


# Issue #268: fees in R. Stop-distance buckets in percent of the entry, (label, low inclusive, high exclusive).
STOP_BUCKETS = (("<1.5", None, 1.5), ("1.5-2", 1.5, 2.0), ("2-3", 2.0, 3.0), ("3-4", 3.0, 4.0), (">=4", 4.0, None))
FEE_BACKTEST_THRESHOLDS = (0.03, 0.04, 0.05, 0.06, 0.08, 0.10)
FEE_BACKTEST_MIN_N = 10
FEES_NOTE = ("Data note: fees_r and realized_r_net are null when any commission is not paid in USDT (e.g. a BNB fee "
             "discount); BNB commissions are not converted at the fill price.")
FEE_BACKTEST_NOTE = ("Back-test of the executor fee-in-R gate (profile max_fee_r) over these closed trades: a trade is "
                     "rejected when its expected fee (taker entry + taker SL) exceeds the threshold. Report only: it "
                     "recommends no threshold.")


def _stop_distance_pct(row):
    """The row's stop_distance_pct, else initial_risk / entry_price x 100 (rows written before issue #268); None."""
    pct = _num(row.get("stop_distance_pct"))
    if pct is not None and pct > 0:
        return pct
    risk, entry = _num(row.get("initial_risk")), _num(row.get("entry_price"))
    return risk / entry * 100 if risk is not None and entry is not None and risk > 0 and entry > 0 else None


def _total(values):
    values = [v for v in values if v is not None]
    return round(sum(values), 4) if values else None


def _fees_block(rows):
    """Total / mean fee R over the rows with fees_r; rows without it (older rows, non-USDT commission) counted."""
    known = [f for f in (_num(r.get("fees_r")) for r in rows) if f is not None]
    return {"total_fees_r": _total(known), "mean_fees_r": _mean(known), "n_with_fees_r": len(known),
            "fees_r_unavailable": len(rows) - len(known), "note": FEES_NOTE}


def _fees_by_stop_bucket(rows):
    """Fee R per stop-distance bucket (STOP_BUCKETS); rows without a stop distance counted apart."""
    groups = {label: [] for label, _lo, _hi in STOP_BUCKETS}
    unavailable = 0
    for row in rows:
        pct = _stop_distance_pct(row)
        if pct is None:
            unavailable += 1
            continue
        label = next(label for label, lo, hi in STOP_BUCKETS
                     if (lo is None or pct >= lo) and (hi is None or pct < hi))
        groups[label].append(_num(row.get("fees_r")))
    buckets = [{"bucket": label, "n": len(fees), "n_with_fees_r": sum(f is not None for f in fees),
                "total_fees_r": _total(fees), "mean_fees_r": _mean(fees)} for label, fees in groups.items()]
    return {"unit": "percent of entry", "buckets": buckets, "stop_distance_unavailable": unavailable}


def _gross_vs_net(rows):
    """Gross and net expectancy over the SAME rows: those with both realized_r_gross and realized_r_net."""
    pairs = [(g, n) for g, n in ((_num(r.get("realized_r_gross")), _num(r.get("realized_r_net"))) for r in rows)
             if g is not None and n is not None]
    return {"n": len(pairs), "rows_without_both": len(rows) - len(pairs),
            "expectancy_r_gross": _mean(g for g, _n in pairs), "expectancy_r_net": _mean(n for _g, n in pairs),
            "total_r_gross": _total(g for g, _n in pairs), "total_r_net": _total(n for _g, n in pairs)}


def _direction_split(rows, r_values):
    """LONG vs SHORT: n, win rate and expectancy on the R basis (_r_stats), mean gross / net R, total R."""
    out = {}
    for d in ("LONG", "SHORT"):
        sel = [(row, r) for row, r in zip(rows, r_values) if str(row.get("direction") or "").upper() == d]
        stats = _r_stats([r for _row, r in sel])
        out[d] = {"n": stats["n"], "win_rate_pct": stats["win_rate_pct"], "expectancy_r": stats["expectancy_r"],
                  "expectancy_r_gross": _mean(_num(row.get("realized_r_gross")) for row, _r in sel),
                  "expectancy_r_net": _mean(_num(row.get("realized_r_net")) for row, _r in sel),
                  "total_r": _total(r for _row, r in sel)}
    return out


def _fee_threshold_backtest(rows):
    """Issue #268 (report only): for each FEE_BACKTEST_THRESHOLDS value, n / net expectancy / total net R of the rows
    the fee-in-R gate would keep (expected_fee_r <= threshold, as the gate) vs reject. Rows without a stop distance
    or without net R are counted, not used; a side with n < FEE_BACKTEST_MIN_N is flagged insufficient_n."""
    usable, no_stop, no_net = [], 0, 0
    for row in rows:
        pct, net = _stop_distance_pct(row), _num(row.get("realized_r_net"))
        if pct is None:
            no_stop += 1
        elif net is None:
            no_net += 1
        else:
            usable.append((gl.expected_fee_r(pct), net))

    def side(values):
        return {"n": len(values), "expectancy_r_net": _mean(values), "total_r_net": _total(values),
                "insufficient_n": len(values) < FEE_BACKTEST_MIN_N}

    return {"note": FEE_BACKTEST_NOTE, "fee_model": "taker entry + taker SL", "n": len(usable),
            "rows_without_stop_distance": no_stop, "rows_without_net_r": no_net,
            "thresholds": [{"max_fee_r": t, "kept": side([n for f, n in usable if f <= t]),
                            "rejected": side([n for f, n in usable if f > t])} for t in FEE_BACKTEST_THRESHOLDS]}


def _source_info(path, rows, env, now):
    exists = os.path.exists(path)
    info = {"path": path, "exists": exists, "mtime_utc": None, "age_seconds": None,
            "env_in_rows": sorted({norm_env(r.get("env")) for r in rows if norm_env(r.get("env"))}),
            "since_in_rows": sorted({str(r.get("since")) for r in rows if r.get("since")})}
    warnings = []
    if exists:
        mtime = os.path.getmtime(path)
        info["mtime_utc"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(mtime))
        info["age_seconds"] = int(now - mtime)
        if info["age_seconds"] > STALE_SECONDS:
            warnings.append(f"outcomes file is {info['age_seconds'] // 3600} h old: re-run trade_outcomes.py")
    if rows and info["env_in_rows"] != [env]:
        found = ", ".join(info["env_in_rows"]) or "none"
        warnings.append(f"outcomes rows env ({found}) differs from --env {env}: re-run trade_outcomes.py --env {env}")
    return info, warnings


def _recommendations(n, expectancy, profit_factor, cause_clusters, gross_fallback=0):
    if n < MIN_SAMPLE:
        return [f"Insufficient sample (n={n} < {MIN_SAMPLE} resolved trades): no parameter recommendation."]
    recs = []
    if expectancy is not None and expectancy < 0:
        recs.append(f"Negative expectancy ({expectancy:+.4f}R/trade over {n}): review entries before scaling.")
    elif (expectancy is not None and expectancy > 0 and profit_factor is not None and profit_factor >= 1.8
          and not gross_fallback):  # only over net R: a gross-fallback row would overstate the edge
        recs.append(SCALING_MSG.format(n=n))
    if cause_clusters.get("BTC_DUMP_CORRELATION", 0) >= 2:
        recs.append(CLUSTER_MSG.format(count=cause_clusters["BTC_DUMP_CORRELATION"]))
    return recs


def _min_trades():
    try:
        import user_profile as up
        return scal.calibration_policy(up.load_user_profile(base_dir=_workspace_dir()))[1]
    except Exception:
        return scal.DEFAULT_MIN_TRADES


def _min_lcb_r():
    """Profile tier_s_calibration_min_lcb_r (issue #207), the default when the profile cannot be read."""
    try:
        import user_profile as up
        return scal.calibration_policy(up.load_user_profile(base_dir=_workspace_dir()))[2]
    except Exception:
        return scal.DEFAULT_MIN_LCB_R


def calibration_store(rows, now, min_trades, min_lcb_r=scal.DEFAULT_MIN_LCB_R):
    """(store, has_prod_rows): the existing logs/score_calibration.json merged with this run's closed PROD rows.
    Without PROD rows the existing store is returned unchanged (None when missing or unreadable)."""
    existing = scal.load_calibration(_workspace_dir())
    prod_rows = [r for r in rows if norm_env(r.get("env")) == "prod"]
    if not prod_rows:
        return existing, False
    return scal.merge_store(existing, prod_rows, now, min_trades, min_lcb_r), True


def _calibration_block(store, min_trades, written, min_lcb_r=scal.DEFAULT_MIN_LCB_R):
    cal = store if isinstance(store, dict) and isinstance(store.get("buckets"), dict) \
        else scal.build_calibration([], scal.STORE_ENV, min_trades, min_lcb_r)
    keys = ("n", "wins", "win_rate", "expectancy_r_net", "sd_r_net", "lcb95_r_net", "mean_mfe_r", "mean_radar_score",
            "insufficient",
            "calibrated")
    return {
        "basis": "dossier_score", "note": "heuristic score, not a probability", "env": scal.STORE_ENV,
        "store": scal.STORE_REL_PATH, "store_written": written, "generated_at_ts": cal.get("generated_at_ts"),
        "min_trades": min_trades, "min_lcb_r": min_lcb_r, "min_sample": MIN_SAMPLE,
        "score_schema_version": scal.SCORE_SCHEMA_VERSION,
        "buckets": [dict({"bucket": label}, **{k: (cal["buckets"].get(label) or {}).get(k) for k in keys})
                    for label in scal.BUCKET_LABELS],
        "unscored": cal.get("unscored", 0), "out_of_range": cal.get("out_of_range", 0),
        "excluded_schema": cal.get("excluded_schema", 0),
    }


def _shadow():
    try:
        if not os.path.exists(os.path.join(_logs_dir(), "shadow_trades.jsonl")):
            return {}
        import shadow_tracker
        metrics = shadow_tracker.calculate_efficacy_metrics()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    regret = _shadow_delta_regret()
    if regret is not None:
        metrics["delta_regret"] = regret
    return metrics


def _shadow_delta_regret():
    """Conservative delta-gate regret summary (issue #251, shadow_analytics.regret_report on logs/), None without
    resolved DELTA_GATE / DUPLICATE_RESTING rows; never raises (an error is returned as {"error"})."""
    try:
        import shadow_analytics as sa
        logs = _logs_dir()
        resolved = sa.load_jsonl(os.path.join(logs, "shadow_resolved.jsonl"))
        if not any(sa.row_gate(r)[0] in sa.REGRET_GATES for r in resolved if isinstance(r, dict)):
            return None
        rep = sa.regret_report(resolved, sa.load_jsonl(os.path.join(logs, "guardian_actions.jsonl")),
                               sa.load_jsonl(os.path.join(logs, "trade_outcomes.jsonl")),
                               sa.load_jsonl(os.path.join(logs, "trades_audit.jsonl")))
        out = {k: rep["conservative"][k] for k in ("mean_regret_r", "ci95_low", "ci95_high", "n", "n_clusters",
                                                     "insufficient_sample")}
        out["blocker_unresolved"] = rep["counts"]["blocker_unresolved"]
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def generate_scorecard(env, outcomes_path=None, now=None, write_calibration=False) -> dict:
    """write_calibration (the CLI): persist the merged logs/score_calibration.json when the run has PROD rows and
    the outcomes file is the workspace's logs/trade_outcomes.jsonl (never a custom --outcomes path)."""
    now = time.time() if now is None else now
    outcomes_path = outcomes_path or os.path.join(_logs_dir(), "trade_outcomes.jsonl")
    rows = _read_jsonl(outcomes_path)
    source, warnings = _source_info(outcomes_path, rows, env, now)

    excluded = {"open": 0, "fills_unavailable": 0, "other_status": 0, "other_env": 0, "unknown_env": 0, "null_r": 0}
    resolved, r_values, gross_fallback = [], [], 0
    for row in rows:
        row_env = norm_env(row.get("env"))
        if row_env is None:
            excluded["unknown_env"] += 1
            continue
        if row_env != env:
            excluded["other_env"] += 1
            continue
        status = row.get("status")
        if status != "closed":
            excluded[status if status in ("open", "fills_unavailable") else "other_status"] += 1
            continue
        r, fallback = _r_value(row)
        if r is None:
            excluded["null_r"] += 1
            continue
        gross_fallback += fallback
        resolved.append(row)
        r_values.append(r)

    candidates, dossier_warning = load_dossier_candidates()
    if dossier_warning:
        warnings.append(dossier_warning)
    by_tier = {t: [] for t in TIERS}
    for row, r in zip(resolved, r_values):
        by_tier[trade_tier(row, candidates)].append(r)

    stats = _r_stats(r_values)
    gross_usdt, net_usdt = _usdt(resolved)
    insights = load_insights_records()
    cause_clusters = {}
    for i in insights:
        cause = i.get("root_cause", "GENERAL")
        cause_clusters[cause] = cause_clusters.get(cause, 0) + 1

    min_trades, min_lcb_r = _min_trades(), _min_lcb_r()
    store, has_prod = calibration_store(rows, now, min_trades, min_lcb_r)
    written = False
    # Only the guard-protected logs/trade_outcomes.jsonl may feed the store (a PROD gate input): any other
    # --outcomes file is reported but never persisted (issue #202 audit).
    canonical = os.path.realpath(os.path.join(_logs_dir(), "trade_outcomes.jsonl"))
    if write_calibration and has_prod and os.path.realpath(outcomes_path) == canonical:
        written = bool(atomic_write_json(scal.store_path(_workspace_dir()), store))

    n = len(resolved)
    performance = dict(stats)
    performance.update(
        mean_realized_r_gross=_mean(_num(r.get("realized_r_gross")) for r in resolved),
        mean_realized_r_net=_mean(_num(r.get("realized_r_net")) for r in resolved),
        net_pnl_usdt_gross=gross_usdt, net_pnl_usdt_net=net_usdt)
    return {
        "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
        "env": env,
        "sample_size": n,
        "source": source,
        "excluded": excluded,
        "r_basis": {"primary": "realized_r_net", "gross_fallback": gross_fallback},
        "performance": performance,
        "tiers_breakdown": {t: {k: v for k, v in _r_stats(vals).items() if k in ("n", "win_rate_pct", "expectancy_r")}
                            for t, vals in by_tier.items()},
        "fees": _fees_block(resolved),
        "fees_by_stop_bucket": _fees_by_stop_bucket(resolved),
        "gross_vs_net": _gross_vs_net(resolved),
        "direction_split": _direction_split(resolved, r_values),
        "fee_threshold_backtest": _fee_threshold_backtest(resolved),
        "score_calibration": _calibration_block(store, min_trades, written, min_lcb_r),
        "insights": {"env_agnostic": True, "loss_cause_clusters": cause_clusters},
        "recommendations": _recommendations(n, stats["expectancy_r"], stats["profit_factor_r"], cause_clusters,
                                            gross_fallback),
        "warnings": warnings,
        "shadow": _shadow(),
    }


def _fmt(value, spec, none="n/a"):
    return none if value is None else format(value, spec)


def _fee_report_lines(sc: dict) -> list:
    """Issue #268 report sections: fees in R, gross vs net, LONG / SHORT, the fee-in-R gate back-test."""
    fees, buckets, gvn = sc.get("fees") or {}, sc.get("fees_by_stop_bucket") or {}, sc.get("gross_vs_net") or {}
    lines = ["💸 FEES IN R (measured from fills):",
             f"  • Total: {_fmt(fees.get('total_fees_r'), '.4f')}R | Mean per trade: "
             f"{_fmt(fees.get('mean_fees_r'), '.4f')}R | rows with fees_r: {fees.get('n_with_fees_r', 0)} | "
             f"without: {fees.get('fees_r_unavailable', 0)}",
             f"  • Same {gvn.get('n', 0)} trades: expectancy gross {_fmt(gvn.get('expectancy_r_gross'), '+.4f')}R | "
             f"net {_fmt(gvn.get('expectancy_r_net'), '+.4f')}R (rows without both: {gvn.get('rows_without_both', 0)})"]
    for b in buckets.get("buckets") or []:
        lines.append(f"  • Stop {b['bucket']}%: n={b['n']} | mean fee {_fmt(b['mean_fees_r'], '.4f')}R | total "
                     f"{_fmt(b['total_fees_r'], '.4f')}R")
    lines.append(f"  stop distance unavailable: {buckets.get('stop_distance_unavailable', 0)} | {fees.get('note', FEES_NOTE)}")
    lines.append("-" * 70)
    lines.append("↔️  BY DIRECTION:")
    for d, s in (sc.get("direction_split") or {}).items():
        lines.append(f"  - {d}: {s['n']} trades | Win Rate: {_fmt(s['win_rate_pct'], '.1f')}% | Expectancy: "
                     f"{_fmt(s['expectancy_r'], '+.4f')}R (gross {_fmt(s['expectancy_r_gross'], '+.4f')}R / net "
                     f"{_fmt(s['expectancy_r_net'], '+.4f')}R) | Total: {_fmt(s['total_r'], '+.4f')}R")
    lines.append("-" * 70)
    bt = sc.get("fee_threshold_backtest") or {}
    lines.append(f"🧪 FEE-IN-R GATE BACK-TEST (n={bt.get('n', 0)}; without stop distance "
                 f"{bt.get('rows_without_stop_distance', 0)}, without net R {bt.get('rows_without_net_r', 0)}):")
    lines.append(f"  {bt.get('note', FEE_BACKTEST_NOTE)}")
    for t in bt.get("thresholds") or []:
        k, r = t["kept"], t["rejected"]
        lines.append(f"  - max_fee_r {t['max_fee_r']:.2f}: kept n={k['n']} exp {_fmt(k['expectancy_r_net'], '+.4f')}R "
                     f"total {_fmt(k['total_r_net'], '+.4f')}R{' (insufficient_n)' if k['insufficient_n'] else ''} | "
                     f"rejected n={r['n']} exp {_fmt(r['expectancy_r_net'], '+.4f')}R total "
                     f"{_fmt(r['total_r_net'], '+.4f')}R{' (insufficient_n)' if r['insufficient_n'] else ''}")
    lines.append("-" * 70)
    return lines


def format_scorecard_report(sc: dict) -> str:
    p = sc["performance"]
    src = sc["source"]
    lines = []
    lines.append("=" * 70)
    lines.append("🏆 QUANTITATIVE TRADING SCORECARD & META-IMPROVER")
    lines.append(f"Date: {sc['timestamp_utc']} | Env: {sc['env'].upper()} | Sample: {sc['sample_size']} resolved trades")
    lines.append(f"Source: {src['path']} ({'age ' + str(src['age_seconds']) + ' s' if src['exists'] else 'missing'}"
                 f"; since {', '.join(src['since_in_rows']) or 'n/a'})")
    for w in sc.get("warnings", []):
        lines.append(f"⚠️  WARNING: {w}")
    ex = sc["excluded"]
    lines.append("Excluded: " + ", ".join(f"{k}={v}" for k, v in ex.items())
                 + f" | R basis: net (gross fallback {sc['r_basis']['gross_fallback']})")
    lines.append("=" * 70)
    lines.append(f"• Win Rate: {_fmt(p['win_rate_pct'], '.1f')}% ({p['wins']}W / {p['losses']}L)")
    lines.append(f"• Expectancy: {_fmt(p['expectancy_r'], '+.4f')}R | Profit Factor: {_fmt(p['profit_factor_r'], '.2f')}")
    lines.append(f"• Average Win: {_fmt(p['avg_win_r'], '+.4f')}R | Average Loss: {_fmt(p['avg_loss_r'], '+.4f')}R")
    lines.append(f"• Mean R gross: {_fmt(p['mean_realized_r_gross'], '+.4f')} | Mean R net: {_fmt(p['mean_realized_r_net'], '+.4f')}")
    lines.append(f"• Net PnL: gross {_fmt(p['net_pnl_usdt_gross'], '+.2f')} USDT | net {_fmt(p['net_pnl_usdt_net'], '+.2f')} USDT")
    lines.append("-" * 70)
    lines.append("📊 PERFORMANCE BY TIER:")
    for tier, stats in sc.get("tiers_breakdown", {}).items():
        lines.append(f"  - {tier}: {stats['n']} trades | Win Rate: {_fmt(stats['win_rate_pct'], '.1f')}% | "
                     f"Expectancy: {_fmt(stats['expectancy_r'], '+.4f')}R")
    lines.append("-" * 70)
    lines.extend(_fee_report_lines(sc))
    cal = sc.get("score_calibration")
    if cal:
        lines.append("🎯 CALIBRATION BY SCORE BUCKET (dossier score; heuristic, not a probability; PROD):")
        for b in cal["buckets"]:
            flag = "calibrated" if b["calibrated"] else ("insufficient" if b["insufficient"] else "not calibrated")
            win = _fmt(b["win_rate"] * 100 if b["win_rate"] is not None else None, ".1f")
            lines.append(f"  - {b['bucket']}: n={b['n']} | Win Rate: {win}% | Exp net: "
                         f"{_fmt(b['expectancy_r_net'], '+.4f')}R | lcb95: {_fmt(b['lcb95_r_net'], '+.4f')}R | "
                         f"MFE: {_fmt(b['mean_mfe_r'], '.2f')}R | radar score: {_fmt(b['mean_radar_score'], '.1f')} | "
                         f"calibrated: {'yes' if b['calibrated'] else 'no'} ({flag})")
        lines.append(f"  unscored: {cal['unscored']} | out of range: {cal['out_of_range']} | older score schema "
                     f"(not v{cal.get('score_schema_version')}, excluded): {cal.get('excluded_schema', 0)} | "
                     f"autonomous Tier S needs a Tier S bucket (80-89 / 90-95) with n >= {cal['min_trades']} and a "
                     f"one-sided 95% t lower bound of mean net R > {cal.get('min_lcb_r', scal.DEFAULT_MIN_LCB_R):g}R")
        lines.append("-" * 70)
    lines.append("🔬 LOSS ROOT CAUSE CLUSTERS (all envs):")
    for cause, cnt in sc["insights"]["loss_cause_clusters"].items():
        lines.append(f"  - {cause}: {cnt} occurrence(s)")
    lines.append("-" * 70)
    lines.append("💡 META-IMPROVER RECOMMENDATIONS:")
    for rec in sc.get("recommendations", []):
        lines.append(f"  👉 {rec}")

    sd = sc.get("shadow")
    if sd and "error" in sd:
        lines.append("-" * 70)
        lines.append(f"👻 SHADOW DESK unavailable: {sd['error']}")
    elif sd:
        lines.append("-" * 70)
        lines.append("👻 SHADOW DESK — COUNTERFACTUAL FILTER EFFICACY:")
        lines.append(f"  • Monitored Setups: {sd.get('active_shadow_trades', 0)} active | {sd.get('total_resolved', 0)} resolved")
        lines.append(f"  • Filter Efficacy Ratio (FER): {sd.get('filter_efficacy_ratio_pct', 0.0)}% (TN: {sd.get('true_negatives', 0)} | FN: {sd.get('false_negatives', 0)})")
        lines.append(f"  • Capital Saved: +${sd.get('capital_saved_usdt', 0.0)} USDT | Missed Alpha: -${sd.get('missed_alpha_usdt', 0.0)} USDT")
        net_fe = sd.get('net_filter_edge_usdt', 0.0)
        lines.append(f"  • Net Filter Edge: {net_fe:+.2f} USDT")
        dr = sd.get("delta_regret")
        if dr and "error" in dr:
            lines.append(f"  • Delta-gate regret unavailable: {dr['error']}")
        elif dr:
            ci = "-" if dr["ci95_low"] is None else f"[{dr['ci95_low']:+.3f}, {dr['ci95_high']:+.3f}]"
            mean = "-" if dr["mean_regret_r"] is None else f"{dr['mean_regret_r']:+.3f}R"
            lines.append(f"  • Delta-gate regret (conservative, gross shadow R - net blocker R): {mean} | 95% CI {ci} | "
                         f"n={dr['n']} | n_clusters={dr['n_clusters']} | unresolved blockers "
                         f"{dr['blocker_unresolved']}{' | insufficient_sample' if dr['insufficient_sample'] else ''} "
                         f"(scripts/shadow_analytics.py)")

    lines.append("=" * 70)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline scorecard from logs/trade_outcomes.jsonl")
    parser.add_argument("--env", choices=["prod", "testnet"], default=None, help="Defaults to utils.env_resolver.resolve_env()")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print the scorecard as JSON")
    parser.add_argument("--out", default=None, help="Scorecard JSON (default logs/trading_scorecard.json)")
    parser.add_argument("--outcomes", default=None, help="Outcomes JSONL (default logs/trade_outcomes.jsonl)")
    args = parser.parse_args(argv)
    try:
        env = resolve_env(args.env)
    except ValueError as e:
        print(f"Invalid environment: {e}", file=sys.stderr)
        return 2
    out = args.out or os.path.join(_logs_dir(), "trading_scorecard.json")
    # Issue #191: the report stays inside logs/ and never replaces the calibration store this run writes itself
    if not path_inside_dir(out, _logs_dir()) or same_file(out, scal.store_path(_workspace_dir())):
        print(f"trading_scorecard: --out must be a file inside {_logs_dir()} other than "
              f"{scal.STORE_REL_PATH} (got {out!r})", file=sys.stderr)
        return 2
    sc = generate_scorecard(env, args.outcomes, write_calibration=True)
    try:
        atomic_write_json(out, sc)  # temp file + os.replace: never writes through a hard link
    except Exception as e:
        print(f"trading_scorecard: {out} NOT written ({type(e).__name__}: {e})", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(sc, indent=2, ensure_ascii=False))
    else:
        print(format_scorecard_report(sc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
