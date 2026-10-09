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
    """(candidates, warning): approved candidates of latest_dossier.json with the dossier's validity window."""
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
        return shadow_tracker.calculate_efficacy_metrics()
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
        "score_calibration": _calibration_block(store, min_trades, written, min_lcb_r),
        "insights": {"env_agnostic": True, "loss_cause_clusters": cause_clusters},
        "recommendations": _recommendations(n, stats["expectancy_r"], stats["profit_factor_r"], cause_clusters,
                                            gross_fallback),
        "warnings": warnings,
        "shadow": _shadow(),
    }


def _fmt(value, spec, none="n/a"):
    return none if value is None else format(value, spec)


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
