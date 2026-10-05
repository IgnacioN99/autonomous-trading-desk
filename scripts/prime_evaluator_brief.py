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
import subprocess
from typing import Dict, Any, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

BASE_DIR = os.path.dirname(SCRIPTS_DIR)
LOGS_DIR = os.path.join(BASE_DIR, "logs")
STATE_FILE = os.path.join(LOGS_DIR, "session_state.json")
INSIGHTS_FILE = os.path.join(LOGS_DIR, "trade_insights.jsonl")
BRIEF_FILE = os.path.join(LOGS_DIR, "primed_brief.json")

BRIEF_MAX_AGE_SECONDS = 600       # The evaluator rejects older briefs
DEFAULT_RISK_PCT_EQUITY = 0.005   # Fallback when the profile has no risk_pct_equity


def ensure_fresh_state(max_age_sec: int = 600, target_env: str = "prod") -> dict:
    """Verifies whether session_state.json is fresh; if not, syncs in ~600ms."""
    needs_sync = True
    if os.path.exists(STATE_FILE):
        age = time.time() - os.path.getmtime(STATE_FILE)
        if age < max_age_sec:
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    curr = json.load(f)
                    if curr.get("target_env", "").lower() == target_env.lower():
                        needs_sync = False
            except Exception:
                pass

    if needs_sync:
        sync_script = os.path.join(BASE_DIR, "scripts", "sync_session_state.py")
        subprocess.run([sys.executable, sync_script, "--env", target_env], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


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


def get_latest_screening_payload(target_env: str = "prod") -> dict:
    """Fetches the latest market screening payload or invokes screening_pipeline."""
    pipeline_script = os.path.join(BASE_DIR, "scripts", "screening_pipeline.py")
    try:
        res = subprocess.run([sys.executable, pipeline_script, "--json", "--env", target_env], capture_output=True, text=True, timeout=60)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
    except Exception:
        pass
    return {}


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
    }


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
    state = ensure_fresh_state(target_env=target_env)
    screening = get_latest_screening_payload(target_env=target_env)
    insights = load_recent_insights(limit=3)
    risk_profile = build_risk_profile(target_env)

    portfolio = state.get("portfolio_exposure", {})
    active_pos = state.get("active_positions", [])
    closed_today = state.get("closed_today_summary", {})
    generated_at_ts = int(time.time())

    # Condensed context pack (token-budget optimized)
    brief = {
        "generated_at_ts": generated_at_ts,
        "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(generated_at_ts)),
        "max_age_seconds": BRIEF_MAX_AGE_SECONDS,
        "target_env": str(target_env).upper(),
        "state_env": str(state.get("target_env", "unknown")).upper(),
        "risk_profile": risk_profile,
        "ground_truth_portfolio": {
            "delta_bias": portfolio.get("delta_bias", "NEUTRAL"),
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
            "allows_alt_shorts": True
        }),
        "filtered_opportunities": screening.get("top_candidates", []),
        "stat_arb_pairs": screening.get("actionable_stat_arb", []),
        "funding_arbitrage_desk": screening.get("top_funding_arbitrage", []),
        "yolo_slot": screening.get("yolo_slot_status", "INACTIVE: Preserving capital."),
        "committed_memory_lessons": [
            {
                "tag": i.get("tags", []),
                "lesson": i.get("insight")
            } for i in insights
        ]
    }

    # Atomic write: the evaluator subagent reads logs/primed_brief.json with view_file
    _write_json(BRIEF_FILE, brief)
    if out_path and os.path.abspath(out_path) != os.path.abspath(BRIEF_FILE):
        _write_json(out_path, brief)

    return brief


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
    lines.append("")
    lines.append("### ⚖️ Portfolio Ground Truth (Binance Ledger)")
    lines.append(f"- **Active Positions ({p['active_positions_count']}):** " + (", ".join([f"{x['symbol']} ({x['dir']} PnL: ${x['pnl']})" for x in p['positions_summary']]) if p['positions_summary'] else "None"))
    lines.append(f"- **Net Delta:** `${p['net_delta_usdt']:+.2f}` (L: ${p['long_notional_usdt']} | S: ${p['short_notional_usdt']})")
    lines.append(f"- **Realized PnL Today:** `${p['realized_pnl_today']:+.2f}` USDT | **Floating PnL:** `${p['floating_pnl_usdt']:+.2f}` USDT")
    lines.append(f"- **Tactical Rule:** {p['tactical_rule']}")
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
        lines.append("| Symbol | Dir | Tier | Conf | Price | Trigger | SL | TP1 / TP2 | R:R | Risk $ | Confluences |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |")
        for o in opps:
            trig = o.get('trigger_price')
            lines.append(f"| **{o.get('symbol')}** | {o.get('direction')} | {(o.get('tier') or '').split(' ')[0]} | {o.get('confidence')}% | {o.get('current_price')} | {trig if trig is not None else '-'} | {o.get('sl_price')} | {o.get('tp1_price')} / {o.get('tp2_price')} | {o.get('rr_ratio')}R | ${o.get('target_dollar_risk', default_risk)} | {'; '.join(o.get('reasons', [])[:2])} |")
    else:
        lines.append("*(No intraday setups passing institutional microstructure filter)*")
    lines.append("")

    lines.append(f"**YOLO Slot:** {brief.get('yolo_slot')}")
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
