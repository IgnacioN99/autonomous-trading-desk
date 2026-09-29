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
"""

import os
import sys
import json
import time
import datetime
from typing import Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
PROFILE_FILE = os.path.join(CONFIG_DIR, "user_profile.json")

DEFAULT_PROFILE = {
    "profile_completed": False,
    "risk_pct_equity": 0.005,           # 0.5% default risk per trade (e.g. $50 on $10k, $5 on $1k)
    "max_margin_ratio": 0.30,          # Maximum 30% of equity per position
    "max_open_positions": 3,           # Maximum concurrent active positions
    "operating_mode": "BALANCED_DELTA_NEUTRAL", # BALANCED_DELTA_NEUTRAL | CONSERVATIVE | AGGRESSIVE
    "yolo_slot_enabled": False,        # Barbell memecoin moonshot slot (10x-15x, $10 margin)
    "overnight_mode": "ZERO_OVERNIGHT_RISK", # ZERO_OVERNIGHT_RISK | SWING_STRUCTURAL_STOP
    "leverage_standard": 3,
    "leverage_yolo": 15,
    "experience_level": "INTERMEDIATE",# BEGINNER | INTERMEDIATE | ADVANCED_QUANT
    "created_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    "updated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
}

def load_user_profile() -> Dict[str, Any]:
    """Loads the user profile from config/user_profile.json or defaults."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(PROFILE_FILE):
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                profile = dict(DEFAULT_PROFILE)
                profile.update(data)
                return profile
        except Exception:
            pass
    # Fallback to example template if present
    example_file = PROFILE_FILE + ".example"
    if os.path.exists(example_file):
        try:
            with open(example_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                profile = dict(DEFAULT_PROFILE)
                profile.update(data)
                return profile
        except Exception:
            pass
    return dict(DEFAULT_PROFILE)

def save_user_profile(profile_data: Dict[str, Any]) -> bool:
    """Persists updated profile to config/user_profile.json."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    profile = load_user_profile()
    profile.update(profile_data)
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

def get_risk_pct_equity() -> float:
    """Returns the user's configured risk per trade as a fraction (default 0.005 = 0.5%)."""
    prof = load_user_profile()
    return float(prof.get("risk_pct_equity", 0.005))

def interactive_terminal_onboarding():
    """Interactive CLI Onboarding questionnaire when run directly in terminal."""
    print("=" * 65)
    print("👋 TRADING SYSTEM ONBOARDING & RISK PROFILER")
    print("=" * 65)
    print("Antes de operar, calibremos tus parámetros de riesgo y estilo:\n")

    current = load_user_profile()

    # 1. Riesgo por Trade
    print("1. ¿Qué porcentaje de tu capital deseas arriesgar por trade en Stop Loss?")
    print("   [1] 0.5% (Recomendado - Conservador / Estándar Prop Firm)")
    print("   [2] 1.0% (Equilibrado / Crecimiento Moderado)")
    print("   [3] 2.0% (Dinámico / Alto Crecimiento)")
    print(f"   (Actual: {current.get('risk_pct_equity', 0.005)*100:.1f}%)")
    choice = input("Selecciona [1/2/3] o escribe un porcentaje (ej. 0.5): ").strip()
    risk_map = {"1": 0.005, "2": 0.01, "3": 0.02}
    if choice in risk_map:
        current["risk_pct_equity"] = risk_map[choice]
    else:
        try:
            val = float(choice.replace("%", ""))
            current["risk_pct_equity"] = val / 100.0 if val > 0.05 else val
        except Exception:
            current["risk_pct_equity"] = 0.005

    # 2. Gestión Nocturna
    print("\n2. ¿Cómo prefieres gestionar las posiciones abiertas durante la noche (22:00 UTC)?")
    print("   [1] Cero Riesgo Nocturno (Ratchetear a Break-Even obligatorio o cerrar)")
    print("   [2] Swing Estructural (Permitir posiciones abiertas con Stop Loss estructural)")
    c2 = input("Selecciona [1/2]: ").strip()
    current["overnight_mode"] = "ZERO_OVERNIGHT_RISK" if c2 != "2" else "SWING_STRUCTURAL_STOP"

    # 3. Slot YOLO Memecoins
    print("\n3. ¿Deseas activar el slot Barbell YOLO (10x-15x en memecoins con clímax, máx $10 margin)?")
    print("   [1] No (Solo operaciones cuantitativas estándar Tier S)")
    print("   [2] Sí (Habilitar slot asimétrico acotado)")
    c3 = input("Selecciona [1/2]: ").strip()
    current["yolo_slot_enabled"] = (c3 == "2")

    # 4. Apalancamiento Estándar
    print("\n4. ¿Qué apalancamiento deseas utilizar para operaciones estándar?")
    print("   [1] 2x (Conservador / Swing: buffer ~45% a liquidación, menor ruido intradía)")
    print("   [2] 3x (Recomendado / Intradía óptimo: equilibrio entre margen y riesgo, buffer ~30%)")
    print("   [3] 5x (Agresivo / Intradía activo: menor margen requerido, mayor sensibilidad a mechas)")
    print(f"   (Actual: {current.get('leverage_standard', 3)}x)")
    c4 = input("Selecciona [1/2/3]: ").strip()
    lev_map = {"1": 2, "2": 3, "3": 5}
    if c4 in lev_map:
        current["leverage_standard"] = lev_map[c4]
    else:
        try:
            val = int(c4.replace("x", ""))
            if 1 <= val <= 20:
                current["leverage_standard"] = val
        except Exception:
            pass

    save_user_profile(current)
    print("\n" + "=" * 65)
    print(f"✅ PERFIL GUARDADO EXITOSAMENTE en config/user_profile.json")
    print(f"• Riesgo por trade: {current['risk_pct_equity']*100:.2f}% de tu balance total")
    print(f"• Modo nocturno: {current['overnight_mode']}")
    print(f"• Slot YOLO: {'ACTIVADO' if current['yolo_slot_enabled'] else 'DESACTIVADO'}")
    print(f"• Apalancamiento estándar: {current['leverage_standard']}x")
    print("=" * 65)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="User Profile & Onboarding Engine")
    parser.add_argument("--setup", action="store_true", help="Run interactive terminal onboarding")
    parser.add_argument("--set-risk", type=float, help="Set risk percent equity (e.g. 0.005 or 0.5)")
    parser.add_argument("--show", action="store_true", help="Show current profile")
    args = parser.parse_args()

    if args.setup:
        interactive_terminal_onboarding()
    elif args.set_risk is not None:
        val = args.set_risk / 100.0 if args.set_risk > 0.05 else args.set_risk
        save_user_profile({"risk_pct_equity": val})
        print(f"✅ Risk updated to {val*100:.2f}% of equity.")
    elif args.show or len(sys.argv) == 1:
        prof = load_user_profile()
        print(json.dumps(prof, indent=2))
