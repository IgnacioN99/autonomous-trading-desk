#!/usr/bin/env python3
"""
climax_watcher_loop.py - Institutional Volume Climax Watcher & Agent Wakeup Trigger.
Monitors Binance Futures in real-time for institutional volume climax setups (vol_ratio >= 1.4x),
absorption wicks (>= 50%), and delta-compatible direction.

When a valid Tier S setup is detected:
1. Emits structured notification telemetry.
2. TESTNET only: records a self-signed dossier (evaluator_agent="climax_watcher_loop") for sandbox runs.
   PROD: the watcher NEVER signs dossiers. Only the clean-room 'isolated_market_evaluator' subagent can
   approve a trade (recorded with `record_evaluation.py --from-subagent <conversationId>`).
3. With --auto-deploy: TESTNET deploys as before. PROD deploys only if a valid evaluator dossier already
   approves the symbol/direction (same gate as the execution engine); otherwise it raises an ALERT
   (logs/climax_alerts.jsonl) instead of deploying.
4. Exits with return code 10 to signal the agent runtime.

Usage:
  python3 scripts/loops/climax_watcher_loop.py [--once] [--interval 180] [--auto-deploy] [--env prod]
"""

import os
import sys
import time
import json
import argparse
import datetime

# Ensure scripts directory on sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from screening_pipeline import execute_screening_pipeline
from utils.env_resolver import resolve_env, find_workspace_root
import execute_futures_trade as eft
import user_profile as up

SELF_EVALUATOR_NAME = "climax_watcher_loop"


def emit_alert(event: str, payload: dict) -> None:
    """Appends an actionable alert for the agent/human (logs/climax_alerts.jsonl) and echoes it to stderr."""
    record = {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "timestamp_ts": int(time.time()),
        "event": event,
        **payload,
    }
    print(f"   🔔 ALERT [{event}]: {json.dumps(payload, ensure_ascii=False)}", file=sys.stderr)
    try:
        from utils.atomic_writer import atomic_append_jsonl
        atomic_append_jsonl(os.path.join(find_workspace_root(), "logs", "climax_alerts.jsonl"), record)
    except Exception as e:
        print(f"   ⚠️ Could not persist alert: {e}", file=sys.stderr)


def build_deploy_command(candidate, leverage: int, target_env: str, confirmed: bool) -> list:
    """Executor CLI for a candidate. The executor re-validates the dossier and every hard gate itself."""
    cmd = [
        sys.executable,
        os.path.join(BASE_DIR, "execute_futures_trade.py"),
        "--symbol", candidate.symbol,
        "--direction", candidate.direction,
        "--margin", str(candidate.required_margin),
        "--leverage", str(leverage),
        "--env", target_env,
    ]
    for flag, value in (("--sl-price", candidate.sl_price), ("--tp1-price", candidate.tp1_price), ("--tp2-price", candidate.tp2_price)):
        try:
            if value and float(value) > 0:
                cmd += [flag, str(value)]
        except (TypeError, ValueError):
            pass
    if confirmed:
        cmd.append("--confirmed")
    return cmd


def auto_deploy_candidate(candidate, leverage: int, target_env: str) -> dict:
    """
    PROD: never self-signs; pre-checks the evaluator dossier with the executor's own gate and alerts instead of
    deploying when it is missing/invalid. No human is present, so --confirmed is never passed in PROD.
    TESTNET: legacy behaviour (deploys with --confirmed against the self-signed sandbox dossier).
    """
    import subprocess
    is_prod = resolve_env(target_env) == "prod"
    if is_prod:
        ok, reason, _cand = eft.enforce_evaluation_dossier(
            candidate.symbol, candidate.direction, target_env=target_env, bypass_eval_gate=False, confirmed=False
        )
        if not ok:
            emit_alert("AUTO_DEPLOY_BLOCKED_NO_EVALUATOR_DOSSIER", {
                "symbol": candidate.symbol,
                "direction": candidate.direction,
                "env": target_env,
                "reason": reason,
                "action": "Invoke the isolated_market_evaluator subagent and record its verdict with "
                          "`record_evaluation.py --from-subagent <conversationId>` before deploying.",
            })
            return {"deployed": False, "blocked": True, "reason": reason}

    cmd = build_deploy_command(candidate, leverage, target_env, confirmed=not is_prod)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout)
    try:
        result = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    if proc.returncode != 0 or not result.get("success"):
        emit_alert("AUTO_DEPLOY_REJECTED", {
            "symbol": candidate.symbol,
            "direction": candidate.direction,
            "env": target_env,
            "exit_code": proc.returncode,
            "reason": result.get("error") or (proc.stderr or "").strip()[-500:],
        })
        return {"deployed": False, "blocked": False, "result": result}
    return {"deployed": True, "result": result}

def scan_for_climax_setups(target_env: str = None, min_vol: float = 1.4, min_wick: float = 50.0):
    """
    Executes a single market scan targeting institutional climax setups.
    Returns list of qualified candidates respecting delta gates.
    """
    target_env = resolve_env(target_env)
    payload = execute_screening_pipeline(target_env=target_env)
    
    portfolio_ctx = payload.portfolio_context or {}
    if isinstance(portfolio_ctx, dict):
        portfolio_delta = portfolio_ctx.get("delta_bias", "DELTA_BALANCED")
    else:
        portfolio_delta = getattr(portfolio_ctx, "delta_bias", "DELTA_BALANCED")
    qualified = []
    
    for c in payload.top_candidates:
        # Check directional compatibility with portfolio delta gate
        if portfolio_delta == "LONG_HEAVY" and c.direction == "LONG":
            continue
        if portfolio_delta == "SHORT_HEAVY" and c.direction == "SHORT":
            continue
            
        # Check institutional volume climax
        if c.vol_ratio < min_vol:
            continue
            
        # Check absorption wick
        wick = c.upper_wick_pct if c.direction == "SHORT" else c.lower_wick_pct
        if wick < min_wick:
            continue
            
        qualified.append(c)
        
    return qualified, payload, portfolio_delta

def run_watcher(interval_seconds: int = 180, once: bool = False, auto_deploy: bool = False, target_env: str = "prod"):
    target_env = resolve_env(target_env)
    prof = up.load_user_profile()
    risk_pct = prof.get("risk_pct_equity", 0.02)
    leverage = prof.get("leverage_standard", 3)
    
    print("=" * 70)
    print("🔭 CLIMAX VOLUME RADAR & AGENT WAKEUP TRIGGER")
    print(f"Target Env: {target_env.upper()} | Risk: {risk_pct*100:.1f}% | Min Climax Vol: >= 1.4x")
    print(f"Polling Interval: {interval_seconds}s | Auto-Deploy: {auto_deploy}")
    print("=" * 70)
    
    iteration = 0
    while True:
        iteration += 1
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n[{now_str}] (Scan #{iteration}) Scanning futures market...")
        
        try:
            candidates, payload, portfolio_delta = scan_for_climax_setups(target_env=target_env)
            
            if not candidates:
                print(f"   ⏳ No compatible institutional climax (Delta: {portfolio_delta}). Waiting for next candle...")
            else:
                print(f"   🚨 INSTITUTIONAL CLIMAX DETECTED! {len(candidates)} qualified candidate(s):")
                approved_dicts = []
                for c in candidates:
                    wick = c.upper_wick_pct if c.direction == "SHORT" else c.lower_wick_pct
                    print(f"   🔥 {c.symbol} ({c.direction}) | Vol: {c.vol_ratio:.1f}x | Wick: {wick:.1f}% | Entry: {c.current_price} | SL: {c.sl_price:.4f} | Margin: ${c.required_margin:.1f} USDT")
                    approved_dicts.append({
                        "symbol": c.symbol,
                        "direction": c.direction,
                        "tier": "Tier S",
                        "leverage": leverage,
                        "thesis": f"Institutional volume climax {c.vol_ratio:.1f}x with {wick:.1f}% absorption wick. Portfolio delta: {portfolio_delta}."
                    })
                
                if target_env == "prod":
                    # PROD: the watcher must never self-sign. Surface the setups for the clean-room evaluator.
                    emit_alert("CLIMAX_SETUP_REQUIRES_EVALUATION", {
                        "env": target_env,
                        "candidates": approved_dicts,
                        "action": "Invoke isolated_market_evaluator; only its recorded dossier can approve a PROD trade.",
                    })
                else:
                    # TESTNET sandbox: legacy self-signed dossier (provenance is not required outside PROD)
                    from record_evaluation import record_evaluation_dossier
                    record_evaluation_dossier(
                        approved_candidates=approved_dicts,
                        evaluator_agent=SELF_EVALUATOR_NAME,
                        summary=f"Climax volume >= 1.4x triggered on {len(candidates)} symbol(s)",
                        status="APPROVED"
                    )

                if auto_deploy:
                    top_c = candidates[0]
                    print(f"\n⚡ Auto-deploying order for {top_c.symbol} ({top_c.direction})...")
                    auto_deploy_candidate(top_c, leverage, target_env)

                # Exit code 10 signals Antigravity / external orchestrator that an actionable event occurred
                sys.exit(10)
                
        except Exception as e:
            print(f"   ⚠️ Scan cycle error: {e}", file=sys.stderr)
            
        if once:
            break
            
        time.sleep(interval_seconds)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Institutional Climax Watcher Loop")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    parser.add_argument("--interval", type=int, default=180, help="Polling interval in seconds (default: 180s = 3m)")
    parser.add_argument("--auto-deploy", action="store_true", help="Automatically deploy the first qualified Tier S candidate")
    parser.add_argument("--env", default="prod", choices=["prod", "testnet"], help="Target environment")
    args = parser.parse_args()
    
    run_watcher(interval_seconds=args.interval, once=args.once, auto_deploy=args.auto_deploy, target_env=args.env)
