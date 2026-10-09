#!/usr/bin/env python3
"""
prime_evaluator_brief.py - Deterministic Context Packer for Quantitative Evaluator.
Context compression and structuring for high-performance inference.

Synthesizes essential market information and account state for evaluation without prompt bloat.
Assembles the exact evaluation brief from Ground Truth (session_state.json),
the typed screening payload, committed lessons from trade_insights.jsonl and the sizing
parameters of the user profile (config/user_profile.json).

The brief is always written to logs/primed_brief.json. The isolated_market_evaluator subagent
reads that file with view_file and rejects it when it is older than 10 minutes (generated_at_ts).

Target size: < 1,800 tokens (vs 35,000 tokens of accumulated chat history).
Zero information loss, zero hallucination in clean-room instances.

Usage:
  python3 scripts/prime_evaluator_brief.py [--env prod|testnet] [--json] [--out [PATH]]
    --json        print the brief as JSON instead of Markdown
    --out [PATH]  also write the JSON brief to PATH (default: logs/primed_brief.json)
"""

import argparse
import os
import sys
import json
import time
import uuid
import subprocess
from typing import Dict, Any, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
from utils.yolo_scan_health import RUN_ID_ENV, YOLO_DISABLED_STATUS  # stdlib-only module (no pipeline import)
from utils.squeeze_filter import SQUEEZE_REASON_PREFIX  # stdlib-only (issue #206)

BASE_DIR = os.path.dirname(SCRIPTS_DIR)
LOGS_DIR = os.path.join(BASE_DIR, "logs")
STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
INSIGHTS_FILE = os.path.join(LOGS_DIR, "trade_insights.jsonl")
BRIEF_FILE = os.path.join(LOGS_DIR, "primed_brief.json")

BRIEF_MAX_AGE_SECONDS = 600       # The evaluator rejects older briefs
DEFAULT_RISK_PCT_EQUITY = 0.005   # Fallback when the profile has no risk_pct_equity
# Issue #66: a crashed pipeline with the Barbell slot enabled is UNAVAILABLE (not INACTIVE "Preserving capital").
YOLO_PIPELINE_FAILED_REASON = "screening pipeline failed"
YOLO_PIPELINE_FAILED_SUMMARY = f"UNAVAILABLE: {YOLO_PIPELINE_FAILED_REASON}. YOLO slot kept empty."
# Issue #91.6: a usable payload whose run_id is not this brief's run id is not trusted (stale or foreign output).
YOLO_RUN_ID_MISMATCH_REASON = "screening payload run id mismatch"
YOLO_RUN_ID_MISMATCH_SUMMARY = f"UNAVAILABLE: {YOLO_RUN_ID_MISMATCH_REASON}. YOLO slot kept empty."
SYNC_FAILED_KEY = "_state_sync_failed"   # set by ensure_fresh_state, popped by assemble_primed_brief (issue #127)


def ensure_fresh_state(max_age_sec: int = 600, target_env: str = "prod") -> dict:
    """Verifies whether session_state.json is fresh (its own last_updated_ts; the file mtime only when it has none);
    if not, syncs in ~600ms. Issue #127: a sync that exits non-zero (INVALID state written, or the write failed and
    the previous file was kept) adds the private key SYNC_FAILED_KEY to the returned state."""
    needs_sync = True
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                curr = json.load(f)
            try:
                ts = float(curr.get("last_updated_ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            age = time.time() - (ts if ts > 0 else os.path.getmtime(STATE_FILE))
            if age < max_age_sec and str(curr.get("target_env", "")).lower() == target_env.lower():
                needs_sync = False
        except Exception:
            pass

    sync_failed = False
    if needs_sync:
        sync_script = os.path.join(BASE_DIR, "scripts", "sync_session_state.py")
        try:
            proc = subprocess.run([sys.executable, sync_script, "--env", target_env], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            sync_failed = proc.returncode != 0
        except Exception:
            sync_failed = True

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    if sync_failed and isinstance(state, dict):
        state[SYNC_FAILED_KEY] = True
    return state


def load_recent_insights(limit: int = 3) -> List[dict]:
    """Loads the last K active lessons from trade_insights.jsonl."""
    if not os.path.exists(INSIGHTS_FILE):
        return []
    active = []
    superseded = set()
    try:
        with open(INSIGHTS_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
            for l in lines:
                try:
                    obj = json.loads(l)
                    if obj.get("superseded"):
                        superseded.add(obj.get("id"))
                    else:
                        active.append(obj)
                except Exception:
                    continue
        valid = [a for a in active if a.get("id") not in superseded]
        return valid[-limit:]
    except Exception:
        return []


def get_latest_screening_payload(target_env: str = "prod", run_id: Optional[str] = None) -> dict:
    """Fetches the latest market screening payload or invokes screening_pipeline. `run_id` is passed to the
    subprocess as DESK_SCAN_RUN_ID; the pipeline echoes it in the payload and its YOLO health record."""
    pipeline_script = os.path.join(BASE_DIR, "scripts", "screening_pipeline.py")
    env = dict(os.environ)
    if run_id:
        env[RUN_ID_ENV] = run_id
    try:
        res = subprocess.run([sys.executable, pipeline_script, "--json", "--env", target_env], capture_output=True,
                             text=True, timeout=60, env=env)
        if res.returncode == 0 and res.stdout.strip():
            payload = json.loads(res.stdout.strip())
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {}


def screening_payload_unusable(screening: Any) -> bool:
    """True when the pipeline subprocess failed ({}), or its payload carries no YOLO slot information."""
    return not isinstance(screening, dict) or not ("yolo_slot" in screening or "yolo_slot_status" in screening)


def _record_pipeline_failure(run_id: str, reason: str = YOLO_PIPELINE_FAILED_REASON) -> None:
    """Counts a failed pipeline as an UNAVAILABLE YOLO run (logs/yolo_scan_health.json). Skipped only when the
    health record carries this run's id with status UNAVAILABLE (the pipeline subprocess already counted this run
    as failed). A record of this run with another status (e.g. OK recorded, then the subprocess crashed while
    writing its output) or of another run does not suppress the count (issue #91.6). Fail-open."""
    try:
        from utils import yolo_scan_health
        health = yolo_scan_health.read_health()
        if run_id and health.get("last_run_id") == run_id and health.get("last_status") == "UNAVAILABLE":
            return
        yolo_scan_health.record_scan("UNAVAILABLE", reason, run_id=run_id)
    except Exception as e:
        print(f"YOLO scan health not recorded ({type(e).__name__})", file=sys.stderr)


def _normalize_risk_fraction(raw: Any) -> float:
    """Same normalization as the execution engine: 0.005 -> 0.5%; 0.5 -> 0.5%; 1.0 -> 1%."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RISK_PCT_EQUITY
    if val <= 0:
        return DEFAULT_RISK_PCT_EQUITY
    return val if val <= 0.05 else val / 100.0


def _get_equity(target_env: str) -> Optional[float]:
    """Account equity via the risk engine (session_state.json first, read-only ledger query otherwise)."""
    try:
        import quant_risk_engine as qre
        equity = float(qre.get_account_equity(target_env=target_env))
        return equity if equity > 0 else None
    except Exception:
        return None


def build_risk_profile(target_env: str, profile: Optional[dict] = None, equity: Optional[float] = None) -> dict:
    """Sizing parameters for the evaluator, derived from config/user_profile.json (never hard-coded)."""
    if profile is None:
        try:
            import user_profile as up
            profile = up.load_user_profile()
        except Exception:
            profile = {}
    if equity is None:
        equity = _get_equity(target_env)

    risk_fraction = _normalize_risk_fraction(profile.get("risk_pct_equity", DEFAULT_RISK_PCT_EQUITY))

    def _int(key: str, default: int) -> int:
        try:
            return int(profile.get(key, default))
        except (TypeError, ValueError):
            return default

    import user_profile as up
    ceiling = up.get_leverage_ceiling(profile)  # Same ceiling enforced by execute_futures_trade.py
    lev_std = min(_int("leverage_standard", 3), ceiling)
    lev_yolo = min(_int("leverage_yolo", lev_std), ceiling)

    yolo_fixed = profile.get("yolo_margin_fixed")
    try:
        yolo_fixed = round(float(yolo_fixed), 2) if yolo_fixed is not None else None
    except (TypeError, ValueError):
        yolo_fixed = None
    yolo_margin = yolo_fixed
    if yolo_margin is None and equity:
        try:
            yolo_margin = round(equity * float(profile.get("yolo_equity_pct", DEFAULT_RISK_PCT_EQUITY)), 2)
        except (TypeError, ValueError):
            yolo_margin = None

    return {
        "source": "config/user_profile.json",
        "profile_completed": bool(profile.get("profile_completed", False)),
        "account_equity_usdt": round(equity, 2) if equity else None,
        "risk_pct_equity": risk_fraction,
        "risk_per_trade_usdt": round(equity * risk_fraction, 2) if equity else None,
        "max_margin_ratio": profile.get("max_margin_ratio"),
        "max_open_positions": profile.get("max_open_positions"),
        "leverage_standard": lev_std,
        "leverage_yolo": lev_yolo,
        "leverage_ceiling": ceiling,
        "yolo_slot_enabled": bool(profile.get("yolo_slot_enabled", False)),
        "yolo_margin_fixed": yolo_fixed,
        "yolo_margin_usdt": yolo_margin,
        "autonomous_execution_tier_s": bool(profile.get("autonomous_execution_tier_s", False)),
        "overnight_mode": profile.get("overnight_mode"),
        **up.get_daily_loss_limits(profile),  # issue #207: daily_stop_r, max_consecutive_sl, yolo_max_daily_losses
    }


def build_yolo_slot_brief(screening: dict) -> dict:
    """Brief `yolo_slot` object: {status, summary, candidates} from the structured screening field (issue #52).
    Falls back to the legacy `yolo_slot_status` string (INACTIVE, no candidates) when the field is missing.
    Candidates are forwarded only when the slot is ACTIVE."""
    summary = str(screening.get("yolo_slot_status") or "INACTIVE: Preserving capital.")
    slot = screening.get("yolo_slot")
    if not isinstance(slot, dict) or not slot.get("status"):
        return {"status": "INACTIVE", "summary": summary, "candidates": []}
    status = str(slot["status"]).upper()
    candidates = slot.get("candidates") if status == "ACTIVE" else None
    return {"status": status, "summary": summary,
            "candidates": [c for c in (candidates or []) if isinstance(c, dict)]}


def _write_json(path: str, data: dict) -> None:
    try:
        from utils.atomic_writer import atomic_write_json
    except ImportError:
        atomic_write_json = None
    if atomic_write_json is not None:
        atomic_write_json(path, data)
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def assemble_primed_brief(target_env: str = "prod", out_path: Optional[str] = None) -> dict:
    run_id = uuid.uuid4().hex
    state = ensure_fresh_state(target_env=target_env)
    if not isinstance(state, dict):
        state = {}
    sync_failed = bool(state.pop(SYNC_FAILED_KEY, False))
    screening = get_latest_screening_payload(target_env=target_env, run_id=run_id)
    insights = load_recent_insights(limit=3)
    risk_profile = build_risk_profile(target_env)

    portfolio = state.get("portfolio_exposure", {})
    active_pos = state.get("active_positions", [])
    closed_today = state.get("closed_today_summary", {})
    generated_at_ts = int(time.time())

    yolo_slot = build_yolo_slot_brief(screening)
    unusable = screening_payload_unusable(screening)
    if risk_profile.get("yolo_slot_enabled"):
        if unusable:
            yolo_slot = {"status": "UNAVAILABLE", "summary": YOLO_PIPELINE_FAILED_SUMMARY, "candidates": []}
            _record_pipeline_failure(run_id)
        elif screening.get("run_id") != run_id:  # missing or foreign run id: fail closed (issue #91.6)
            yolo_slot = {"status": "UNAVAILABLE", "summary": YOLO_RUN_ID_MISMATCH_SUMMARY, "candidates": []}
            _record_pipeline_failure(run_id, YOLO_RUN_ID_MISMATCH_REASON)
    elif unusable:
        yolo_slot = {"status": "DISABLED", "summary": YOLO_DISABLED_STATUS, "candidates": []}
    market_data_status = screening.get("market_data_status") if isinstance(screening, dict) else None
    # Issue #48 / #127: resting entries come from the session state only (sync_session_state.resting_entry_exposure),
    # the same view as delta_bias_incl_resting. A missing field (an older state) reads as UNKNOWN (fail closed).
    resting_bias = portfolio.get("delta_bias_incl_resting", "UNKNOWN")
    pending_entries = [{"symbol": r.get("symbol"), "dir": r.get("dir"), "kind": r.get("kind")}
                       for r in (portfolio.get("resting_entries") or []) if isinstance(r, dict)]

    # Condensed context pack (token-budget optimized)
    brief = {
        "generated_at_ts": generated_at_ts,
        "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(generated_at_ts)),
        "max_age_seconds": BRIEF_MAX_AGE_SECONDS,
        "target_env": str(target_env).upper(),
        "state_env": str(state.get("target_env", "unknown")).upper(),
        "market_data_status": str(market_data_status) if market_data_status else None,
        "risk_profile": risk_profile,
        "pending_entries": pending_entries,
        "ground_truth_portfolio": {
            "delta_bias": portfolio.get("delta_bias", "NEUTRAL"),
            "delta_bias_incl_resting": resting_bias,
            "long_notional_usdt": portfolio.get("long_notional_usdt", 0.0),
            "short_notional_usdt": portfolio.get("short_notional_usdt", 0.0),
            "net_delta_usdt": portfolio.get("net_notional_delta_usdt", 0.0),
            "floating_pnl_usdt": portfolio.get("total_floating_pnl_usdt", 0.0),
            "closed_trades_today": closed_today.get("closed_trades_count", 0),
            "realized_pnl_today": closed_today.get("net_realized_pnl_usdt", 0.0),
            "tactical_rule": portfolio.get("delta_advice", "Standard balanced operation."),
            "active_positions_count": len(active_pos),
            "positions_summary": [
                {
                    "symbol": p["symbol"],
                    "dir": p["direction"],
                    "entry": p["entry_price"],
                    "mark": p["mark_price"],
                    "pnl": p["unrealized_pnl_usdt"],
                    "roe": p["roe_pct"],
                    "sl": p.get("sl_price"),
                    "sl_verified": p.get("sl_algo_verified", False)
                } for p in active_pos
            ]
        },
        "macro_btc": screening.get("macro", {
            "btc_price": state.get("macro_btc", {}).get("price_usdt", 0.0),
            "allows_alt_shorts": False  # no BTC context: no altcoin shorts (fail closed, issue #206)
        }),
        "filtered_opportunities": [_brief_opportunity(o) for o in screening.get("top_candidates", [])],
        "stat_arb_pairs": screening.get("actionable_stat_arb", []),
        "funding_arbitrage_desk": screening.get("top_funding_arbitrage", []),
        "yolo_slot": yolo_slot,
        "committed_memory_lessons": [
            {
                "tag": i.get("tags", []),
                "lesson": i.get("insight")
            } for i in insights
        ]
    }

    if resting_bias == "UNKNOWN":
        brief["pending_entries_status"] = "UNREADABLE"
    if sync_failed:
        brief["state_sync"] = "FAILED"
    # Issue #206: altcoin SHORTs the screener's macro gate dropped (symbols only), so a thin radar is not read as quiet
    rejected_shorts = [r.get("symbol") for r in (screening.get("macro_rejected_shorts") or [])
                       if isinstance(r, dict) and r.get("symbol")]
    if rejected_shorts:
        brief["macro_rejected_shorts"] = rejected_shorts
    # Issue #207 (PR #214 review): fundingInfo failed, every funding interval was read as 8h
    if isinstance(screening, dict) and screening.get("funding_info_warning"):
        brief["funding_info_warning"] = str(screening["funding_info_warning"])[:120]
    # Issue #207: the ledger's Daily Loss Gate (the executor re-reads the exchange and is authoritative)
    brief["daily_loss_gate"] = brief_daily_loss_gate(state, target_env)
    data_quality = closed_today_data_quality(closed_today)
    if data_quality:
        brief["ground_truth_portfolio"]["closed_today_data"] = data_quality

    # Atomic write: the evaluator subagent reads logs/primed_brief.json with view_file
    _write_json(BRIEF_FILE, brief)
    if out_path and os.path.abspath(out_path) != os.path.abspath(BRIEF_FILE):
        _write_json(out_path, brief)
    _write_scores_sidecar(screening, yolo_slot, generated_at_ts, target_env)

    return brief


def brief_daily_loss_gate(state: Any, target_env: str) -> dict:
    """Compact {blocked, scope, reason} of the ledger's daily_loss_gate (issue #207). A state of another environment
    or without the key reads as blocked for every entry (fail closed; the executor re-reads the exchange)."""
    gate = state.get("daily_loss_gate") if isinstance(state, dict) else None
    same_env = isinstance(state, dict) and str(state.get("target_env", "")).lower() == str(target_env).lower()
    if not same_env or not isinstance(gate, dict):
        return {"blocked": True, "scope": "all", "reason": "unavailable: ledger has no daily_loss_gate state"}
    blocked = gate.get("blocked") is not False
    scope = gate.get("scope") if gate.get("scope") in ("all", "yolo") else ("all" if blocked else None)
    return {"blocked": blocked, "scope": scope, "reason": gate.get("reason")}


# Issue #187 / #212: closed-today data-quality keys reach the brief only when they differ from these defaults.
CLOSED_TODAY_DEFAULTS = {"counted_by": "trades", "fills_closed": 0, "truncated": False,
                         "trade_summary_error": None, "fills_error": None}


def closed_today_data_quality(closed_today: Any) -> dict:
    """The CLOSED_TODAY_DEFAULTS keys of the ledger's closed_today_summary whose value is not the default."""
    closed_today = closed_today if isinstance(closed_today, dict) else {}
    return {k: closed_today.get(k) for k, default in CLOSED_TODAY_DEFAULTS.items()
            if closed_today.get(k, default) != default}


# Audit-only fields (issue #202): kept out of the evaluator's brief (token budget) and written to the sidecar.
# alt_short_climax_ok (issue #206) is a pipeline-only gate input: kept out of the brief, not written to the sidecar.
# score_schema_version (issue #207): sidecar only (calibration input).
_SIDECAR_ONLY_KEYS = ("score_components", "tier_s_eligible", "alt_short_climax_ok", "score_schema_version")


# Issue #206 flags reach the brief only when set (token budget): these values are dropped from a row.
_DROP_WHEN_UNSET = {"squeeze_risk": False, "squeeze_reasons": [], "long_crowding_risk": False,
                    "macro_short_check": None, "funding_rate_pct": None, "funding_interval_unknown": False}
# The 8h-normalized funding and the interval add nothing for an 8h symbol (normalized == raw)
_FUNDING_8H_KEYS = ("funding_rate_8h_pct", "funding_interval_h")


def _brief_opportunity(o: Any) -> Any:
    if not isinstance(o, dict):
        return o
    out = {k: v for k, v in o.items() if k not in _SIDECAR_ONLY_KEYS
           and not (k in _DROP_WHEN_UNSET and v == _DROP_WHEN_UNSET[k] and type(v) is type(_DROP_WHEN_UNSET[k]))}
    if out.get("funding_interval_h") in (None, 8):
        for k in _FUNDING_8H_KEYS:
            out.pop(k, None)
    return out


def scores_sidecar_path() -> str:
    """logs/primed_brief_scores.json, next to BRIEF_FILE (so tests that redirect the brief redirect it too)."""
    return os.path.join(os.path.dirname(os.path.abspath(BRIEF_FILE)), "primed_brief_scores.json")


def _write_scores_sidecar(screening: Any, yolo_slot: dict, generated_at_ts: int, target_env: str) -> None:
    """Radar score, tier and components of every candidate in the brief (issue #202). Read only by
    record_evaluation.py for the audit trail, never by the evaluator or a gate. Fail-open: the brief is unaffected."""
    try:
        cands = list(screening.get("top_candidates") or []) if isinstance(screening, dict) else []
        cands += [dict(c, direction=c.get("direction") or "LONG") for c in (yolo_slot.get("candidates") or [])]
        rows = [dict({"symbol": c.get("symbol"), "direction": c.get("direction"), "confidence": c.get("confidence"),
                      "tier": c.get("tier"), "tier_s_eligible": c.get("tier_s_eligible"),
                      "squeeze_risk": c.get("squeeze_risk") is True,
                      "score_components": c.get("score_components"), "reasons": c.get("reasons")},
                     # issue #207: the radar score formula version, when the row carries one (calibration filter)
                     **({"score_schema_version": c["score_schema_version"]}
                        if isinstance(c.get("score_schema_version"), int) else {}))
                for c in cands if isinstance(c, dict) and c.get("symbol")]
        _write_json(scores_sidecar_path(), {"generated_at_ts": generated_at_ts, "env": str(target_env).upper(),
                                            "rows": rows})
    except Exception as e:
        print(f"Radar score sidecar not written ({type(e).__name__})", file=sys.stderr)


_TIER_CODES = ("S", "A+", "A", "B+")


def _tier_label(o: dict) -> str:
    """Tier code for a brief row: a valid `tier_code` wins, else the code named by the `tier` label
    ("Tier A+ (...)" -> "A+"), else "?" (issue #135)."""
    if o.get("tier_code") in _TIER_CODES:
        return o["tier_code"]
    words = (o.get("tier") or "").split(" ")[:2]
    return next((c for c in _TIER_CODES if words == ["Tier", c]), "?")


def format_markdown_brief(brief: dict) -> str:
    p = brief["ground_truth_portfolio"]
    m = brief["macro_btc"]
    rp = brief.get("risk_profile") or {}
    risk_usdt = rp.get("risk_per_trade_usdt")
    risk_txt = f"${risk_usdt}" if risk_usdt is not None else "UNKNOWN (equity unavailable)"

    lines = []
    lines.append(f"# 📦 PRIMED EVALUATOR BRIEF ({brief['timestamp_utc']})")
    lines.append(f"**Env:** `{brief['target_env']}` | **BTC:** `${m.get('btc_price', 0):,.1f}` | **Delta:** `{p['delta_bias']}`")
    lines.append(
        f"**Risk/trade:** {risk_txt} ({float(rp.get('risk_pct_equity') or 0) * 100:.2f}% equity) | "
        f"**Leverage:** std {rp.get('leverage_standard')}x / YOLO {rp.get('leverage_yolo')}x (ceiling {rp.get('leverage_ceiling')}x) | "
        f"**YOLO slot:** {'ON' if rp.get('yolo_slot_enabled') else 'OFF'} (margin {rp.get('yolo_margin_usdt')})"
    )
    lines.append(f"**Brief file:** `logs/primed_brief.json` (generated_at_ts {brief.get('generated_at_ts')}, max age {brief.get('max_age_seconds', BRIEF_MAX_AGE_SECONDS) // 60} min)")
    if brief.get("market_data_status"):
        lines.append(f"**Market data:** {brief['market_data_status']}")
    lines.append("")
    lines.append("### ⚖️ Portfolio Ground Truth (Binance Ledger)")
    lines.append(f"- **Active Positions ({p['active_positions_count']}):** " + (", ".join([f"{x['symbol']} ({x['dir']} PnL: ${x['pnl']})" for x in p['positions_summary']]) if p['positions_summary'] else "None"))
    lines.append(f"- **Net Delta:** `${p['net_delta_usdt']:+.2f}` (L: ${p['long_notional_usdt']} | S: ${p['short_notional_usdt']})")
    lines.append(f"- **Realized PnL Today:** `${p['realized_pnl_today']:+.2f}` USDT | **Floating PnL:** `${p['floating_pnl_usdt']:+.2f}` USDT")
    lines.append(f"- **Tactical Rule:** {p['tactical_rule']}")
    pend = brief.get("pending_entries") or []
    lines.append(f"- **Pending Entries ({len(pend)}):** "
                 + (", ".join(f"{x.get('symbol')} ({x.get('dir')} {x.get('kind')})" for x in pend) or "None")
                 + f" | **Delta incl. resting:** `{p.get('delta_bias_incl_resting', 'UNKNOWN')}`"
                 + (" | **Pending entries status:** `UNREADABLE`" if brief.get("pending_entries_status") else "")
                 + (" | **State sync:** `FAILED`" if brief.get("state_sync") else ""))
    dlg = brief.get("daily_loss_gate") or {}
    if dlg.get("blocked") or dlg.get("scope"):  # issue #207
        lines.append(f"- **Daily Loss Gate:** `ACTIVE ({dlg.get('scope') or 'all'})` {dlg.get('reason') or ''}".rstrip())
    if p.get("closed_today_data"):
        lines.append(f"- **Closed-today data:** {json.dumps(p['closed_today_data'], ensure_ascii=False)}")
    if brief.get("funding_info_warning"):
        lines.append(f"- **Funding info:** {brief['funding_info_warning']}")
    lines.append("")

    if brief.get("committed_memory_lessons"):
        lines.append("### 🧠 Recent Committed Lessons")
        for les in brief["committed_memory_lessons"]:
            lines.append(f"- *[{', '.join(les['tag'])}]:* {les['lesson']}")
        lines.append("")

    opps = brief.get("filtered_opportunities", [])
    default_risk = risk_usdt if risk_usdt is not None else "?"
    lines.append(f"### 🎯 Filtered Technical Setups ({len(opps)})")
    if opps:
        lines.append("| Symbol | Dir | Tier | Score | Price | Trigger | SL | TP1 / TP2 | R:R | Risk $ | Confluences |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |")
        for o in opps:
            trig = o.get('trigger_price')
            # abs:unscored: the wick/taker candles did not match, so absorption gave no confluence (issue #135)
            # SQZ: SHORT squeeze risk, capped at Tier A; LONG-CROWD: crowded LONG, flag only (issue #206). With SQZ
            # shown, the radar's "Squeeze risk" reason line is left out so the two factors show real confluences.
            sqz = o.get('squeeze_risk') is True
            shown = [r for r in o.get('reasons', []) if not (sqz and str(r).startswith(SQUEEZE_REASON_PREFIX))]
            factors = ((["SQZ"] if sqz else [])
                       + (["LONG-CROWD"] if o.get('long_crowding_risk') is True else [])
                       + (["abs:unscored"] if o.get('absorption_scored') is False else []) + shown[:2])
            lines.append(f"| **{o.get('symbol')}** | {o.get('direction')} | {_tier_label(o)} | score {o.get('confidence')} | {o.get('current_price')} | {trig if trig is not None else '-'} | {o.get('sl_price')} | {o.get('tp1_price')} / {o.get('tp2_price')} | {o.get('rr_ratio')}R | ${o.get('target_dollar_risk', default_risk)} | {'; '.join(factors)} |")
    else:
        lines.append("*(No intraday setups passing institutional microstructure filter)*")
    if brief.get("macro_rejected_shorts"):
        rej = brief["macro_rejected_shorts"]
        lines.append(f"**Macro-rejected alt SHORTs ({len(rej)}):** {', '.join(str(s) for s in rej)}")
    lines.append("")

    yolo = brief.get("yolo_slot")
    if not isinstance(yolo, dict):  # briefs written before issue #52 carried a plain string
        yolo = {"summary": yolo, "candidates": []}
    lines.append(f"**YOLO Slot:** {yolo.get('summary')}")
    for y in yolo.get("candidates") or []:
        lines.append(f"- **{y.get('symbol')}** LONG {y.get('leverage')}x | Trigger {y.get('trigger')} | "
                     f"SL {y.get('sl')} (-{y.get('risk_pct')}%) | TP1 {y.get('tp1')} / TP2 {y.get('tp2')} "
                     f"({y.get('rr_tp2')}R) | Margin {y.get('margin_usdt')} | Vol {y.get('vol_ratio')}x | "
                     f"Wick {y.get('lower_wick')}% | RSI {y.get('rsi')}")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic context packer for the isolated_market_evaluator subagent")
    parser.add_argument("--env", default=None, help="Target environment (prod|testnet). Defaults to the project resolver.")
    parser.add_argument("--json", action="store_true", help="Print the brief as JSON instead of Markdown")
    parser.add_argument("--out", nargs="?", const=BRIEF_FILE, default=None, metavar="PATH",
                        help="Also write the JSON brief to PATH (logs/primed_brief.json is always written)")
    args = parser.parse_args(argv)

    try:
        from utils.env_resolver import resolve_env
        env = resolve_env(args.env, base_dir=BASE_DIR)
    except ValueError as e:
        print(f"Invalid environment: {e}", file=sys.stderr)
        return 2

    brief = assemble_primed_brief(target_env=env, out_path=args.out)
    if args.json:
        print(json.dumps(brief, indent=2, ensure_ascii=False))
    else:
        print(format_markdown_brief(brief))
    return 0


if __name__ == "__main__":
    sys.exit(main())
