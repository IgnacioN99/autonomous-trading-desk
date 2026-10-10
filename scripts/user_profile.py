#!/usr/bin/env python3
"""
user_profile.py - User Profile, Onboarding & Dynamic Risk Configuration Engine.
Manages persistent user preferences, risk appetite, and trading personality.

Prior to deploying any live or sandbox trade, the system verifies that the user
profile has been established. If absent, an onboarding protocol is initiated to
calibrate the parameters to the user's risk tolerance.

Default Parameters:
- risk_pct_equity: 0.005 (0.5% default risk per trade on Stop Loss)
- max_margin_ratio: 0.30 (Max 30% of total balance allocated to a single position)
- max_open_positions: 3
- overnight_mode: "ZERO_OVERNIGHT_RISK" (Ratchet to BE or close at 22:00 UTC)
- yolo_slot_enabled: False
- leverage_standard: 3
- leverage_yolo: 15 (Barbell YOLO slot; sub-accounts are auto-clamped to 5x by the executor on -4421)
- leverage_ceiling: 15 (absolute desk ceiling enforced by the execution engine and the pre-trade guard;
  raise it here, never above MAX_LEVERAGE_CEILING)
- max_fee_r: null (fee-in-R executor gate, issue #268; OFF by default, the owner sets the threshold in R)
"""

import os
import sys
import json
import time
import datetime
from typing import Dict, Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
PROFILE_FILE = os.path.join(CONFIG_DIR, "user_profile.json")

# Single source of truth for the absolute desk leverage ceiling.
# execute_futures_trade.py (hard gate) and hooks/pre_trade_guard.py (pre-check) both read it through
# get_leverage_ceiling(); users may raise it per profile via `leverage_ceiling` up to MAX_LEVERAGE_CEILING.
DEFAULT_LEVERAGE_CEILING = 15
MAX_LEVERAGE_CEILING = 125

DEFAULT_PROFILE = {
    "profile_completed": False,
    "risk_pct_equity": 0.005,           # 0.5% default risk per trade (e.g. $50 on $10k, $5 on $1k)
    "max_margin_ratio": 0.30,          # Maximum 30% of equity per position
    "max_open_positions": 3,           # Maximum concurrent active positions
    "operating_mode": "BALANCED_DELTA_NEUTRAL", # BALANCED_DELTA_NEUTRAL | CONSERVATIVE | AGGRESSIVE
    "autonomous_execution_tier_s": False, # Cold start: autonomous execution disabled by default; requires explicit opt-in
    # Issue #202: autonomous Tier S only in a calibrated score bucket (n >= min trades resolved PROD trades with
    # a one-sided 95% Student-t lower bound of mean net R above tier_s_calibration_min_lcb_r, issue #207;
    # logs/score_calibration.json); otherwise ask the user. Validated by
    # utils.score_calibration.calibration_policy (bool; int >= 1; number >= 0; anything else = the default).
    "require_calibrated_tier_s": True,
    "tier_s_calibration_min_trades": 30,
    "tier_s_calibration_min_lcb_r": 0.1,
    # Issue #207: Daily Loss Gate (executor, opening orders only; PROD strict). No new entry once today's (UTC) net
    # realized PnL <= -daily_stop_r x risk_pct_equity x start-of-day equity, or after max_consecutive_sl full stops
    # (<= -0.8R) in a row; YOLO entries also stop after yolo_max_daily_losses YOLO full losses. Validated by
    # get_daily_loss_limits (an invalid value = the default; no value turns the gate off).
    "daily_stop_r": 3.0,
    "max_consecutive_sl": 2,
    "yolo_max_daily_losses": 1,
    # Issue #267: bounds under which a re-checked candidate (prime_evaluator_brief.py --recheck after the dossier
    # expired) is covered by the user's earlier "yes" (utils/recheck_bounds.py). Validated by get_recheck_bounds.
    "recheck_max_drift_r": 0.25,
    "recheck_max_age_seconds": 1800,
    # Issue #268: fee-in-R gate (executor, PROD openings only): reject when the expected taker entry + taker SL fee
    # (utils/gate_limits.expected_fee_r) exceeds this many R. null = OFF (the default: the owner chooses a value).
    # Validated by get_max_fee_r: a present but invalid value rejects PROD openings (never silently OFF).
    "max_fee_r": None,
    "yolo_slot_enabled": False,      # Barbell memecoin moonshot slot (10x-15x, $10 margin or 0.5% equity)
    "yolo_equity_pct": 0.005,          # 0.5% default margin for YOLO moonshots (e.g. $50 on $10k)
    "overnight_mode": "ZERO_OVERNIGHT_RISK", # ZERO_OVERNIGHT_RISK | CLOSE_ALL_AT_MARKET | SWING_STRUCTURAL_STOP
    "leverage_standard": 3,
    "leverage_yolo": 15,
    "leverage_ceiling": DEFAULT_LEVERAGE_CEILING,
    "experience_level": "INTERMEDIATE",# BEGINNER | INTERMEDIATE | ADVANCED_QUANT
    "created_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    "updated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
}

def get_yolo_margin(target_env="testnet") -> float:
    """Calculates YOLO margin based on yolo_margin_fixed or yolo_equity_pct (default $10 - $15 USDT)."""
    prof = load_user_profile()
    if "yolo_margin_fixed" in prof and prof["yolo_margin_fixed"] is not None:
        try:
            return round(float(prof["yolo_margin_fixed"]), 2)
        except Exception:
            pass
    yolo_pct = float(prof.get("yolo_equity_pct", 0.12))
    try:
        from quant_risk_engine import get_account_equity, RateLimitedError
    except Exception:
        get_account_equity, RateLimitedError = None, None
    try:
        if get_account_equity is None:
            raise RuntimeError("quant_risk_engine unavailable")
        equity = get_account_equity(target_env=target_env)
    except Exception as e:
        # A Binance rate limit on the equity read stops the caller (issue #91.2): never size from a default.
        if RateLimitedError is not None and isinstance(e, RateLimitedError):
            raise
        equity = 10000.0 if str(target_env).lower() == "testnet" else 100.0
    return round(min(max(equity * yolo_pct, 10.0), 15.0), 2)

PROFILE_SOURCE_KEY = "_profile_source"

def load_user_profile(base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Loads the user profile from config/user_profile.json or defaults. The returned dict carries
    PROFILE_SOURCE_KEY ("_profile_source"): "user" (config/user_profile.json), "example" (its .example template) or
    "default" (DEFAULT_PROFILE), so callers can tell a fallback (issue #156). It is never persisted
    (save_user_profile strips it)."""
    config_dir = os.path.join(base_dir, "config") if base_dir else CONFIG_DIR
    profile_file = os.path.join(config_dir, "user_profile.json")
    os.makedirs(config_dir, exist_ok=True)
    if os.path.exists(profile_file):
        try:
            with open(profile_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                profile = dict(DEFAULT_PROFILE)
                profile.update(data)
                profile[PROFILE_SOURCE_KEY] = "user"
                return profile
        except Exception:
            pass
    # Fallback to example template if present
    example_file = profile_file + ".example"
    if os.path.exists(example_file):
        try:
            with open(example_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                profile = dict(DEFAULT_PROFILE)
                profile.update(data)
                profile[PROFILE_SOURCE_KEY] = "example"
                return profile
        except Exception:
            pass
    return dict(DEFAULT_PROFILE, **{PROFILE_SOURCE_KEY: "default"})

def save_user_profile(profile_data: Dict[str, Any]) -> bool:
    """Persists updated profile to config/user_profile.json (without the load-time PROFILE_SOURCE_KEY marker)."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    profile = load_user_profile()
    profile.update(profile_data)
    profile.pop(PROFILE_SOURCE_KEY, None)
    profile["updated_at_utc"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    profile["profile_completed"] = True
    try:
        temp_file = PROFILE_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
        os.replace(temp_file, PROFILE_FILE)
        return True
    except Exception as e:
        print(f"Error saving profile: {e}", file=sys.stderr)
        return False

def get_leverage_ceiling(profile: Optional[Dict[str, Any]] = None) -> int:
    """Absolute desk leverage ceiling: profile `leverage_ceiling` (default 15), clamped to [1, MAX_LEVERAGE_CEILING].
    Invalid values fall back to DEFAULT_LEVERAGE_CEILING."""
    if profile is None:
        profile = load_user_profile()
    raw = (profile or {}).get("leverage_ceiling", DEFAULT_LEVERAGE_CEILING)
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_LEVERAGE_CEILING
    if value < 1:
        return DEFAULT_LEVERAGE_CEILING
    return min(value, MAX_LEVERAGE_CEILING)

# Issue #183: R-based profit-lock steps applied by dynamic_exit_manager once True Net BE is allowed (verified TP1
# fill or closed-15m MFE >= 2x ATR_15m). lock_r 0 = True Net BE; lock_r > 0 = entry +/- lock_r x initial risk.
# Issue #205: trail_activation = pre-TP1 trail activation rule with an R reference: "r_only" (MFE >= 1R), "r_and_atr"
# (>= 1R and >= 2x ATR_15m) or "r_or_atr" (legacy: either one; on wide stops 2x ATR fired well before +1R).
TRAIL_ACTIVATION_MODES = ("r_only", "r_and_atr", "r_or_atr")
PROFIT_LOCK_MIN_GAP_R = 0.5  # issue #197: every step needs mfe_r - lock_r >= 0.5
DEFAULT_EXIT_MANAGEMENT = {
    "profit_lock_enabled": True,
    "profit_lock_steps": [{"mfe_r": 1.0, "lock_r": 0.0}, {"mfe_r": 2.0, "lock_r": 1.0}, {"mfe_r": 3.0, "lock_r": 2.0}],
    "extend_last_step": True,   # beyond the last step, each further full 1.0R of MFE raises the lock by 1.0R
    "lock_on_tp1": True,        # on a verified TP1 fill the lock also uses intrabar MFE (closed 1m bars, mark, TP1)
    "trail_activation": "r_only",
}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
        and value not in (float("inf"), float("-inf"))


def _profit_lock_steps_error(steps) -> Optional[str]:
    """Why `steps` is not a valid profit_lock_steps list, else None."""
    if not isinstance(steps, list) or not steps:
        return "must be a non-empty list"
    prev = None
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not _is_number(step.get("mfe_r")) or not _is_number(step.get("lock_r")):
            return f"step {i} needs numeric mfe_r and lock_r"
        mfe_r, lock_r = float(step["mfe_r"]), float(step["lock_r"])
        if mfe_r <= 0:
            return f"step {i} mfe_r must be > 0"
        if not 0 <= lock_r < mfe_r:
            return f"step {i} needs 0 <= lock_r < mfe_r"
        if mfe_r - lock_r < PROFIT_LOCK_MIN_GAP_R - 1e-9:
            return f"step {i} needs mfe_r - lock_r >= {PROFIT_LOCK_MIN_GAP_R:g}"
        if prev is not None and mfe_r <= prev:
            return f"step {i} mfe_r must be strictly ascending"
        prev = mfe_r
    return None


def get_exit_management(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Profile `exit_management` (issue #183) merged key by key over DEFAULT_EXIT_MANAGEMENT. An invalid value falls
    back to the default for that key only and adds a warning. Returns profit_lock_enabled, profit_lock_steps (list of
    {"mfe_r": float, "lock_r": float}, each with mfe_r - lock_r >= PROFIT_LOCK_MIN_GAP_R), extend_last_step,
    lock_on_tp1, trail_activation (one of TRAIL_ACTIVATION_MODES, issue #205) and warnings (list of str). Never
    raises."""
    warnings = []
    out = {k: v for k, v in DEFAULT_EXIT_MANAGEMENT.items() if k != "profit_lock_steps"}
    out["profit_lock_steps"] = [dict(s) for s in DEFAULT_EXIT_MANAGEMENT["profit_lock_steps"]]
    try:
        if profile is None:
            profile = load_user_profile()
        raw = (profile or {}).get("exit_management") if isinstance(profile, dict) else None
    except Exception as e:
        raw = None
        warnings.append(f"exit_management unreadable ({type(e).__name__}); defaults used")
    if raw is not None and not isinstance(raw, dict):
        warnings.append("exit_management invalid: must be an object; defaults used")
        raw = None
    for key in ("profit_lock_enabled", "extend_last_step", "lock_on_tp1"):
        if raw and key in raw:
            if isinstance(raw[key], bool):
                out[key] = raw[key]
            else:
                warnings.append(f"exit_management.{key} invalid: must be true or false; defaults used")
    if raw and "trail_activation" in raw:
        if isinstance(raw["trail_activation"], str) and raw["trail_activation"] in TRAIL_ACTIVATION_MODES:
            out["trail_activation"] = raw["trail_activation"]
        else:
            warnings.append("exit_management.trail_activation invalid: must be one of "
                            f"{', '.join(TRAIL_ACTIVATION_MODES)}; defaults used")
    if raw and "profit_lock_steps" in raw:
        err = _profit_lock_steps_error(raw["profit_lock_steps"])
        if err:
            warnings.append(f"exit_management.profit_lock_steps invalid: {err}; defaults used")
        else:
            out["profit_lock_steps"] = [{"mfe_r": float(s["mfe_r"]), "lock_r": float(s["lock_r"])}
                                        for s in raw["profit_lock_steps"]]
    out["warnings"] = warnings
    return out


DAILY_STOP_R_MAX = 20.0


def get_daily_loss_limits(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Daily Loss Gate limits (issue #207) from the profile: daily_stop_r (number, 0 < x <= DAILY_STOP_R_MAX),
    max_consecutive_sl and yolo_max_daily_losses (int >= 1). A missing or invalid value falls back to its
    DEFAULT_PROFILE value (conservative); none of them can disable the gate. Never raises."""
    prof = profile if isinstance(profile, dict) else {}
    out = {}
    raw = prof.get("daily_stop_r", DEFAULT_PROFILE["daily_stop_r"])
    out["daily_stop_r"] = (float(raw) if _is_number(raw) and 0 < raw <= DAILY_STOP_R_MAX
                           else DEFAULT_PROFILE["daily_stop_r"])
    for key in ("max_consecutive_sl", "yolo_max_daily_losses"):
        raw = prof.get(key, DEFAULT_PROFILE[key])
        out[key] = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1 else DEFAULT_PROFILE[key]
    return out


RECHECK_MAX_DRIFT_R_MAX = 0.5  # issue #279: above it the earlier "yes" no longer covers the new plan
RECHECK_MAX_AGE_RANGE_S = (300, 7200)


def get_recheck_bounds(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Re-check bounds (issue #267) from the profile: recheck_max_drift_r (number, 0 < x <= RECHECK_MAX_DRIFT_R_MAX)
    and recheck_max_age_seconds (number within RECHECK_MAX_AGE_RANGE_S, as int). A missing or invalid value falls
    back to its DEFAULT_PROFILE value. Never raises."""
    prof = profile if isinstance(profile, dict) else {}
    raw = prof.get("recheck_max_drift_r", DEFAULT_PROFILE["recheck_max_drift_r"])
    drift = (float(raw) if _is_number(raw) and 0 < raw <= RECHECK_MAX_DRIFT_R_MAX
             else DEFAULT_PROFILE["recheck_max_drift_r"])
    raw = prof.get("recheck_max_age_seconds", DEFAULT_PROFILE["recheck_max_age_seconds"])
    low, high = RECHECK_MAX_AGE_RANGE_S
    age = int(raw) if _is_number(raw) and low <= raw <= high else DEFAULT_PROFILE["recheck_max_age_seconds"]
    return {"recheck_max_drift_r": drift, "recheck_max_age_seconds": age}


def get_max_fee_r(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fee-in-R gate threshold (issue #268) from the profile: {"max_fee_r": float | None, "error": str | None}.
    Absent or null = OFF ({"max_fee_r": None, "error": None}); a positive finite number = the threshold; any other
    present value (bool, string, zero, negative, NaN, inf) = {"max_fee_r": None, "error": <why>}, so an invalid value
    never silently disables an enabled protection (the executor rejects PROD openings on it). Never raises."""
    prof = profile if isinstance(profile, dict) else {}
    raw = prof.get("max_fee_r")
    if raw is None:
        return {"max_fee_r": None, "error": None}
    if _is_number(raw) and raw > 0:
        return {"max_fee_r": float(raw), "error": None}
    return {"max_fee_r": None, "error": f"max_fee_r must be null or a positive finite number; got {raw!r}"[:120]}


def validate_leverage_setting(name: str, value: int, ceiling: int) -> Optional[str]:
    """Returns an error message if `value` is not a valid leverage for `name` (1..ceiling), else None."""
    if not isinstance(value, int) or value < 1 or value > ceiling:
        return f"{name} must be an integer between 1 and the desk leverage ceiling ({ceiling}x); got {value}."
    return None

def get_risk_pct_equity() -> float:
    """Returns the user's configured risk per trade as a fraction (default 0.005 = 0.5%)."""
    prof = load_user_profile()
    return float(prof.get("risk_pct_equity", 0.005))

def interactive_terminal_onboarding():
    """Interactive CLI Onboarding questionnaire when run directly in terminal."""
    print("=" * 65)
    print("👋 TRADING SYSTEM ONBOARDING & RISK PROFILER")
    print("=" * 65)
    print("Before trading, let's calibrate your risk and style parameters:\n")

    current = load_user_profile()

    # 1. Risk per Trade
    print("1. What percentage of your capital do you want to risk per trade at Stop Loss?")
    print("   [1] 0.5% (Recommended - Conservative / Prop Firm Standard)")
    print("   [2] 1.0% (Balanced / Moderate Growth)")
    print("   [3] 2.0% (Dynamic / High Growth)")
    print(f"   (Current: {current.get('risk_pct_equity', 0.005)*100:.1f}%)")
    choice = input("Select [1/2/3] or type a percentage (e.g. 0.5): ").strip()
    risk_map = {"1": 0.005, "2": 0.01, "3": 0.02}
    if choice in risk_map:
        current["risk_pct_equity"] = risk_map[choice]
    else:
        try:
            val = float(choice.replace("%", ""))
            current["risk_pct_equity"] = val / 100.0 if val > 0.05 else val
        except Exception:
            current["risk_pct_equity"] = 0.005

    # 2. Overnight Handling
    print("\n2. How do you want to handle open positions overnight (22:00 UTC)?")
    print("   [1] Zero Overnight Risk (ZERO_OVERNIGHT_RISK - Mandatory ratchet to Break-Even or close)")
    print("   [2] Close All at Market (CLOSE_ALL_AT_MARKET - Close 100% of positions at 22:00 UTC)")
    print("   [3] Structural Swing (SWING_STRUCTURAL_STOP - Allow positions with a structural Stop Loss)")
    c2 = input("Select [1/2/3]: ").strip()
    if c2 == "2":
        current["overnight_mode"] = "CLOSE_ALL_AT_MARKET"
    elif c2 == "3":
        current["overnight_mode"] = "SWING_STRUCTURAL_STOP"
    else:
        current["overnight_mode"] = "ZERO_OVERNIGHT_RISK"

    # 3. YOLO Memecoin Slot
    print("\n3. Do you want to enable the Barbell YOLO slot (10x-15x on memecoins with climax volume, max $10 margin)?")
    print("   [1] No (Standard Tier S quantitative trades only)")
    print("   [2] Yes (Enable the capped asymmetric slot)")
    c3 = input("Select [1/2]: ").strip()
    current["yolo_slot_enabled"] = (c3 == "2")

    # 4. Standard Leverage
    print("\n4. Which leverage do you want to use for standard trades?")
    print("   [1] 2x (Conservative / Swing: ~45% buffer to liquidation, less intraday noise)")
    print("   [2] 3x (Recommended / Optimal intraday: balance between margin and risk, ~30% buffer)")
    print("   [3] 5x (Aggressive / Active intraday: less margin required, more sensitive to wicks)")
    print(f"   (Current: {current.get('leverage_standard', 3)}x)")
    c4 = input("Select [1/2/3]: ").strip()
    lev_map = {"1": 2, "2": 3, "3": 5}
    if c4 in lev_map:
        current["leverage_standard"] = lev_map[c4]
    else:
        try:
            val = int(c4.replace("x", ""))
            if validate_leverage_setting("leverage_standard", val, get_leverage_ceiling(current)) is None:
                current["leverage_standard"] = val
        except Exception:
            pass

    # 5. Tier S Autonomous Execution
    print("\n5. Do you want to enable immediate autonomous execution for Tier S opportunities?")
    print("   [1] No (Recommended / Safe: Requires human confirmation in chat before deploying)")
    print("   [2] Yes (Fast-Track: Immediate autonomous execution and protection of approved Tier S setups)")
    c5 = input("Select [1/2]: ").strip()
    current["autonomous_execution_tier_s"] = (c5 == "2")

    save_user_profile(current)
    print("\n" + "=" * 65)
    print(f"✅ PROFILE SAVED SUCCESSFULLY to config/user_profile.json")
    print(f"• Risk per trade: {current['risk_pct_equity']*100:.2f}% of your total balance")
    print(f"• Overnight mode: {current['overnight_mode']}")
    print(f"• YOLO slot: {'ENABLED' if current['yolo_slot_enabled'] else 'DISABLED'}")
    print(f"• Standard leverage: {current['leverage_standard']}x")
    print(f"• Tier S autonomous execution: {'ENABLED (Fast-Track)' if current.get('autonomous_execution_tier_s') else 'DISABLED (Requires human confirmation)'}")
    print("=" * 65)
    _offer_guardian_service()


def _offer_guardian_service():
    """Issue #55: in WSL, offer to install the position guardian as a Windows Task Scheduler task. No profile key;
    a failed install never affects the saved profile."""
    try:
        import install_guardian_service as isg
        if not isg.is_wsl():
            return
    except Exception:
        return
    try:
        answer = input("\n6. Install the position guardian as a Windows background task "
                       "(recommended for PROD resting entries)? [y/N]: ").strip().lower()
    except EOFError:  # closed stdin: no answer
        answer = ""
    if answer not in ("y", "yes"):
        print("Skipped. Install it later with: python3 scripts/install_guardian_service.py --install")
        return
    try:
        rc = isg.install()
    except Exception as e:
        rc = f"{type(e).__name__}: {e}"
    if rc != 0:
        print(f"Guardian install did not complete ({rc}). Retry with: python3 scripts/install_guardian_service.py --install")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="User Profile & Onboarding Engine")
    parser.add_argument("--setup", action="store_true", help="Run interactive terminal onboarding")
    parser.add_argument("--set-risk", type=float, help="Set risk percent equity (e.g. 0.005 or 0.5)")
    parser.add_argument("--set-autonomous-tier-s", choices=["true", "false", "True", "False"], help="Set autonomous execution Tier S")
    parser.add_argument("--set-overnight-mode", choices=["ZERO_OVERNIGHT_RISK", "CLOSE_ALL_AT_MARKET", "SWING_STRUCTURAL_STOP"], help="Set overnight mode")
    parser.add_argument("--set-max-positions", type=int, help="Set max open positions limit")
    parser.add_argument("--set-yolo", choices=["true", "false", "True", "False"], help="Enable/disable YOLO moonshot slot")
    parser.add_argument("--set-leverage-standard", type=int, help="Set standard leverage limit (1..leverage ceiling)")
    parser.add_argument("--set-leverage-yolo", type=int, help="Set YOLO moonshot leverage (1..leverage ceiling)")
    parser.add_argument("--set-leverage-ceiling", type=int,
                        help=f"Set absolute desk leverage ceiling (1..{MAX_LEVERAGE_CEILING}, default {DEFAULT_LEVERAGE_CEILING})")
    parser.add_argument("--show", action="store_true", help="Show current profile")
    args = parser.parse_args()

    updates = {}
    if not args.setup:
        current_prof = load_user_profile()
        ceiling = get_leverage_ceiling(current_prof)
        errors = []
        if args.set_leverage_ceiling is not None:
            if not 1 <= args.set_leverage_ceiling <= MAX_LEVERAGE_CEILING:
                errors.append(f"leverage_ceiling must be between 1 and {MAX_LEVERAGE_CEILING}; got {args.set_leverage_ceiling}.")
            else:
                ceiling = args.set_leverage_ceiling
                updates["leverage_ceiling"] = ceiling
        for name, value in (("leverage_standard", args.set_leverage_standard), ("leverage_yolo", args.set_leverage_yolo)):
            if value is None:
                continue
            err = validate_leverage_setting(name, value, ceiling)
            if err:
                errors.append(err)
            else:
                updates[name] = value
        if errors:
            for err in errors:
                print(f"❌ {err}", file=sys.stderr)
            sys.exit(2)
        if "leverage_ceiling" in updates:
            for name in ("leverage_standard", "leverage_yolo"):
                existing = updates.get(name, current_prof.get(name))
                try:
                    if int(existing) > ceiling:
                        print(f"⚠️ {name}={existing}x exceeds the new ceiling ({ceiling}x); the execution engine will reject it until lowered.", file=sys.stderr)
                except (TypeError, ValueError):
                    pass

    if args.setup:
        interactive_terminal_onboarding()
    else:
        if args.set_risk is not None:
            val = args.set_risk / 100.0 if args.set_risk > 0.05 else args.set_risk
            updates["risk_pct_equity"] = val
        if args.set_autonomous_tier_s is not None:
            updates["autonomous_execution_tier_s"] = args.set_autonomous_tier_s.lower() == "true"
        if args.set_overnight_mode is not None:
            updates["overnight_mode"] = args.set_overnight_mode
        if args.set_max_positions is not None:
            updates["max_open_positions"] = int(args.set_max_positions)
        if args.set_yolo is not None:
            updates["yolo_slot_enabled"] = args.set_yolo.lower() == "true"

        if updates:
            save_user_profile(updates)
            print(f"✅ Profile updated: {updates}")

        if args.show or (len(sys.argv) == 1 and not updates):
            prof = load_user_profile()
            prof.pop(PROFILE_SOURCE_KEY, None)
            print(json.dumps(prof, indent=2))
