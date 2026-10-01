#!/usr/bin/env python3
"""
env_resolver.py - Centralized Environment Resolution and Security Enforcer.
Provides deterministic, fail-closed resolution of target trading environments ('prod' vs 'testnet').
"""

import os
import sys
from typing import Optional, Dict

ALIASES_PROD = frozenset({"prod", "production", "mainnet"})
ALIASES_TESTNET = frozenset({"testnet"})

def find_workspace_root() -> str:
    """Finds the root directory of the workspace by traversing upwards."""
    current = os.path.abspath(__file__)
    # scripts/utils/env_resolver.py -> parent is scripts/utils -> parent is scripts -> parent is workspace root
    base = os.path.dirname(os.path.dirname(os.path.dirname(current)))
    if os.path.exists(os.path.join(base, "AGENTS.md")) or os.path.exists(os.path.join(base, "config")):
        return base
    p = current
    while p and p != os.path.dirname(p):
        p = os.path.dirname(p)
        if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "config")):
            return p
    return base

def parse_env_file(filepath: str) -> Dict[str, str]:
    """Safely parses a simple .env file without third-party dependencies."""
    config: Dict[str, str] = {}
    if not os.path.exists(filepath):
        return config
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k:
                        config[k] = v
    except Exception:
        pass
    return config

def resolve_env(explicit_env: Optional[str] = None, base_dir: Optional[str] = None) -> str:
    """
    Resolves and normalizes the target environment ('prod' or 'testnet').

    Resolution precedence:
    1. explicit_env argument (if provided and non-empty) -> normalized ('prod' or 'testnet').
       If invalid, raises ValueError (Fail-Closed).
    2. os.environ['BINANCE_API_ENV'] -> normalized. If invalid, raises ValueError.
    3. Project root .env file ('BINANCE_API_ENV') -> normalized. If invalid, raises ValueError.
    4. config/environments/prod.env or testnet.env if applicable.
    5. Safe default: 'testnet'.
    """
    if explicit_env is not None:
        cleaned = str(explicit_env).strip().lower()
        if not cleaned:
            raise ValueError(
                "Explicit environment argument cannot be empty. "
                f"Must be one of: {sorted(ALIASES_PROD | ALIASES_TESTNET)}"
            )
        if cleaned in ALIASES_PROD:
            return "prod"
        elif cleaned in ALIASES_TESTNET:
            return "testnet"
        else:
            raise ValueError(
                f"Invalid explicit environment '{explicit_env}'. "
                f"Must be one of: {sorted(ALIASES_PROD | ALIASES_TESTNET)}"
            )

    # Check os.environ
    env_var = os.environ.get("BINANCE_API_ENV")
    if env_var is not None and env_var.strip():
        cleaned = env_var.strip().lower()
        if cleaned in ALIASES_PROD:
            return "prod"
        elif cleaned in ALIASES_TESTNET:
            return "testnet"
        else:
            raise ValueError(
                f"Invalid BINANCE_API_ENV in os.environ: '{env_var}'. "
                f"Must be one of: {sorted(ALIASES_PROD | ALIASES_TESTNET)}"
            )

    if base_dir is None:
        base_dir = find_workspace_root()

    # Check root .env
    root_env_path = os.path.join(base_dir, ".env")
    root_env = parse_env_file(root_env_path)
    if "BINANCE_API_ENV" in root_env and root_env["BINANCE_API_ENV"].strip():
        cleaned = root_env["BINANCE_API_ENV"].strip().lower()
        if cleaned in ALIASES_PROD:
            return "prod"
        elif cleaned in ALIASES_TESTNET:
            return "testnet"
        else:
            raise ValueError(
                f"Invalid BINANCE_API_ENV in .env: '{root_env['BINANCE_API_ENV']}'. "
                f"Must be one of: {sorted(ALIASES_PROD | ALIASES_TESTNET)}"
            )

    # Check config/environments/prod.env and testnet.env
    prod_env_path = os.path.join(base_dir, "config", "environments", "prod.env")
    testnet_env_path = os.path.join(base_dir, "config", "environments", "testnet.env")

    prod_exists = os.path.exists(prod_env_path)
    testnet_exists = os.path.exists(testnet_env_path)

    if prod_exists and not testnet_exists:
        prod_cfg = parse_env_file(prod_env_path)
        p_val = prod_cfg.get("BINANCE_API_ENV", "").strip().lower()
        if p_val in ALIASES_PROD:
            return "prod"
    elif testnet_exists and not prod_exists:
        testnet_cfg = parse_env_file(testnet_env_path)
        t_val = testnet_cfg.get("BINANCE_API_ENV", "").strip().lower()
        if t_val in ALIASES_TESTNET:
            return "testnet"
    elif prod_exists and testnet_exists:
        prod_cfg = parse_env_file(prod_env_path)
        testnet_cfg = parse_env_file(testnet_env_path)
        p_val = prod_cfg.get("BINANCE_API_ENV", "").strip().lower()
        t_val = testnet_cfg.get("BINANCE_API_ENV", "").strip().lower()
        if p_val in ALIASES_PROD and t_val not in ALIASES_TESTNET:
            return "prod"
        elif t_val in ALIASES_TESTNET and p_val not in ALIASES_PROD:
            return "testnet"

    # Default to safe mode 'testnet'
    return "testnet"

def is_prod_environment(explicit_env: Optional[str] = None, base_dir: Optional[str] = None) -> bool:
    """Returns True if the resolved environment is 'prod'."""
    return resolve_env(explicit_env, base_dir=base_dir) == "prod"

def get_env_config(target_env: Optional[str] = None, base_dir: Optional[str] = None) -> Dict[str, str]:
    """
    Loads environment configuration safely following the resolution precedence:
    1. Base .env file (if present)
    2. Environment-specific file (config/environments/{norm_env}.env or ENV_FILE)
    3. os.environ variables take highest precedence for relevant keys.
    """
    if base_dir is None:
        base_dir = find_workspace_root()
    norm_env = resolve_env(target_env, base_dir=base_dir)
    config: Dict[str, str] = {}

    # 1. Base .env
    env_path = os.path.join(base_dir, ".env")
    config.update(parse_env_file(env_path))

    # 2. Environment-specific file
    env_file = os.environ.get("ENV_FILE")
    if not env_file:
        cand = os.path.join(base_dir, "config", "environments", f"{norm_env}.env")
        if os.path.exists(cand):
            env_file = cand

    if env_file and os.path.exists(env_file):
        config.update(parse_env_file(env_file))

    # 3. Environment variables from os.environ take highest precedence
    for k, v in os.environ.items():
        if k.startswith(("BINANCE_", "LIVE_TRADING_", "GMAIL_", "NOTION_", "GITHUB_")) or k in ["ENV", "TARGET_ENV", "BINANCE_API_ENV"]:
            config[k] = v

    config["BINANCE_API_ENV"] = norm_env.upper()
    return config

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    resolved = resolve_env(arg)
    print(f"Resolved environment: {resolved} (is_prod={is_prod_environment(arg)})")
