#!/usr/bin/env python3
"""
post_trade_sync.py - Wrapper/Bridge for scripts/hooks/post_trade_sync.py.
Enables execution from scripts/ directory as well as hooks/ directory.
"""

import os
import sys
import importlib.util

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_PATH = os.path.join(BASE_DIR, "scripts", "hooks", "post_trade_sync.py")

spec = importlib.util.spec_from_file_location("hooks_post_trade_sync", HOOK_PATH)
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)

def find_workspace_root() -> str:
    return _mod.find_workspace_root()

def is_opening_mcp_order(mcp_args: dict) -> bool:
    return _mod.is_opening_mcp_order(mcp_args)

def handle_post_trade_sync(payload: dict) -> dict:
    # Sync monkeypatched functions if any
    curr_fwr = globals().get("find_workspace_root")
    orig_fwr = _mod.find_workspace_root
    if curr_fwr and curr_fwr != orig_fwr:
        _mod.find_workspace_root = curr_fwr
    try:
        return _mod.handle_post_trade_sync(payload)
    finally:
        _mod.find_workspace_root = orig_fwr

def main():
    return _mod.main()

if __name__ == "__main__":
    main()
