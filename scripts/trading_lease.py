#!/usr/bin/env python3
"""
trading_lease.py - Status and takeover of the PROD trading lease (issue #280).

One agent session at a time may open PROD positions (scripts/utils/trading_lease.py). This CLI never writes the lease:
  --status (default; --json): read-only, prints the holder (session, runtime, heartbeat age, stale or not).
  --take: asks for a takeover. The PreToolUse guard (scripts/hooks/pre_trade_guard.py) always asks the user before it
          runs (force_ask); once it ran, the PostToolUse hook (scripts/hooks/post_trade_sync.py) hands the lease to the
          calling session. Run outside an agent session, nothing changes.
The lease only applies to PROD: with --env testnet, --take refuses (exit 2). Risk-reducing commands never need it.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import trading_lease as tl  # noqa: E402
from utils.env_resolver import resolve_env, find_workspace_root  # noqa: E402


def main(argv=None, base_dir=None) -> int:
    parser = argparse.ArgumentParser(description="PROD trading lease: status (read-only) and takeover request",
                                     allow_abbrev=False)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="Print the lease holder (default, read-only)")
    group.add_argument("--take", action="store_true",
                       help="Request a takeover for this agent session (the user approves it; the hook records it)")
    parser.add_argument("--json", action="store_true", help="Print the status as JSON")
    parser.add_argument("--env", default=None, help="prod|testnet (defaults to the project resolver)")
    args = parser.parse_args(argv)
    base_dir = base_dir or find_workspace_root()
    try:
        env = resolve_env(args.env, base_dir=base_dir)
    except ValueError as e:
        print(f"Invalid environment: {e}", file=sys.stderr)
        return 2

    if args.take:
        if env != "prod":
            print("The trading lease applies to PROD only: nothing to take over in TESTNET.", file=sys.stderr)
            return 2
        print("Takeover requested. This script writes nothing: after the user approved this command, the PostToolUse "
              "hook (post_trade_sync.py) hands the trading lease to the calling agent session.")
        return 0

    now = time.time()
    record, error = tl.status(base_dir)
    if args.json:
        out = {"env": env, "applies": env == "prod", "error": error, "holder": None}
        if record is not None:
            out["holder"] = {"session_id": record.get("session_id"), "runtime": record.get("runtime"),
                             "acquired_at": record.get("acquired_at"), "heartbeat_at": record.get("heartbeat_at"),
                             "heartbeat_age_s": int(now - float(record["heartbeat_at"])),
                             "stale": tl.is_stale(record, now), "stale_after_s": tl.LEASE_STALE_SECONDS}
        print(json.dumps(out, indent=2))
    else:
        print(tl.status_line(base_dir, now))
        if env != "prod":
            print("TESTNET: the trading lease is not applied.")
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
