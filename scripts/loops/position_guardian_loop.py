#!/usr/bin/env python3
"""
position_guardian_loop.py - Deterministic background guardian for open Binance Futures positions.

Replaces the retired crypto_radar MCP tools update_trailing_stop_structural,
audit_and_trail_all_positions, check_dead_alpha and audit_orphan_positions.

Per cycle:
  0. Pending entries (execute_futures_trade.protect_pending_entries, same as --protect-pending): a filled
     resting entry from logs/pending_entries.json gets its planned SL (verified, else reduce-only close) and,
     once the entry order is gone, its TPs sized from the actual position; expired unfilled entries are
     cancelled. Runs first, so a fresh fill gets the planned stop instead of the orphan emergency stop.
  1. Sync open positions (GET /fapi/v2/positionRisk).
  2. Orphan audit: a position without a verified protective stop is auto-healed with a verified
     emergency stop (execute_futures_trade.heal_orphan_position); if the stop cannot be verified,
     the position is closed with a reduce-only market order (fail-safe auto-destruct policy).
  3. Structural trailing (dynamic_exit_manager.update_position_to_structural_stop): place-then-cancel,
     never loosens. YOLO positions are skipped until TP1 has filled (right-tail preservation). Activation
     gate (Issue #95): the planned SL is kept (reason "trail_not_activated") until +1.0R of planned risk or
     +2.0x ATR_15m of favourable excursion since entry on closed 15m bars, or TP1 fill; the Chandelier stop is
     then anchored to the extreme since entry. Take-profit orders are never re-based.
  4. Dead-alpha check: reported only; positions are closed (reduce-only) only with --close-dead-alpha.
  5. Unknown resting entries (execute_futures_trade.find_unregistered_resting_entries, all symbols): an opening
     order resting on the exchange without a logs/pending_entries.json record (e.g. deleted registry) would get
     no SL on fill; each one is reported (unknown_resting_entry action + pending_unknown_entry error, so
     cycle_ok is false) and never cancelled. A query failure is a pending_unknown_entry error too.
  6. State is written atomically to logs/guardian_state.json and every action is appended to
     logs/guardian_actions.jsonl.

Safety:
  - It NEVER opens or increases a position. The only writes it can send are protective stop
    placements/cancellations, reduce-only market closes, reduce-only TP limits for filled pending
    entries and cancellations of resting entries.
  - --dry-run computes every decision but sends no write request at all.
  - An exception on one position never stops the others; a network failure is logged and the
    next cycle retries.

Usage:
  python3 scripts/loops/position_guardian_loop.py --once [--env prod|testnet] [--dry-run] [--json]
  python3 scripts/loops/position_guardian_loop.py [--interval 300] [--env prod|testnet] [--close-dead-alpha]
  (PROD resting STOP_MARKET / LIMIT entries require a running loop with --interval <= 120, e.g. --interval 60)

Exit code (--once): 0 when the cycle completed without errors and every position ends protected, else 1.

State file (logs/guardian_state.json):
  {
    "schema_version": 1,
    "timestamp": int, "timestamp_utc": str, "env": "prod" | "testnet", "dry_run": bool,
    "mode": "loop" | "once",           # "loop" when running with --interval (no --once)
    "interval_seconds": int | null,    # loop interval; PROD resting entries need a loop with <= 120s
                                       # (execute_futures_trade.check_guardian_alive)
    "cycle_ok": bool,                  # no errors and every position protected at the end of the cycle
    "positions": [{
      "symbol": str, "side": "LONG" | "SHORT", "size": float, "entry_price": float, "mark_price": float,
      "leverage": int, "unrealized_pnl": float, "liquidation_price": float,
      "protected": bool, "stop_price": float | null,
      "is_yolo": bool, "yolo_source": str | null, "tp1_filled": bool | null,
      "trailing": {"success", "updated", "reason", "previous_sl", "new_sl"?, "planned_sl"?,
                   "activation_reason"?: "tp1_filled" | "r_multiple" | "atr_expansion" | null,
                   "reference_source"?: "trade_audit" | "current_stop", "message"} | null,
      "dead_alpha": {"status", "range_pct", "recommendation", "message"} | null,
      "error": str | null
    }],
    "actions": [ACTION, ...],
    "errors": [{"symbol": str | null, "stage": str, "error": str}]
  }

Action record (also one JSON line in logs/guardian_actions.jsonl):
  {"timestamp": int, "env": str, "symbol": str, "dry_run": bool, "success": bool,
   "type": "orphan_heal" | "orphan_close" | "trail_stop" | "dead_alpha_close" | "pending_protect_sl" |
           "pending_tp_placed" | "pending_abort" | "pending_timeout_cancel" | "pending_dropped" |
           "pending_sl_crossed_close" | "unknown_resting_entry" (report only, success false), "detail": {...}}

Scheduling (generic examples; run from the repository root):
  cron, every 5 minutes, one cycle per run:
    */5 * * * * cd <repo> && python3 scripts/loops/position_guardian_loop.py --once >> logs/guardian_cron.log 2>&1
  systemd: a oneshot service with
    WorkingDirectory=<repo>
    ExecStart=/usr/bin/env python3 scripts/loops/position_guardian_loop.py --once
  triggered by a timer with OnUnitActiveSec=5min (or run the service long-lived without --once).
"""

import os
import sys
import time
import json
import datetime
import argparse
import traceback

# Ensure local path resolution (scripts/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import execute_futures_trade as eft
import dynamic_exit_manager as dem
from utils.env_resolver import resolve_env
from utils.atomic_writer import atomic_write_json, atomic_append_jsonl

SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 300
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LOG_DIR = os.path.join(BASE_DIR, "logs")
STATE_FILE_NAME = "guardian_state.json"
ACTIONS_FILE_NAME = "guardian_actions.jsonl"


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _position_view(p):
    amt = _f(p.get("positionAmt"))
    try:
        lev = int(_f(p.get("leverage")))
    except (TypeError, ValueError):
        lev = 0
    return {
        "symbol": p.get("symbol"),
        "side": "LONG" if amt > 0 else "SHORT",
        "size": abs(amt),
        "entry_price": _f(p.get("entryPrice")),
        "mark_price": _f(p.get("markPrice")),
        "leverage": lev,
        "unrealized_pnl": _f(p.get("unRealizedProfit")),
        "liquidation_price": _f(p.get("liquidationPrice")),
        "protected": False,
        "stop_price": None,
        "is_yolo": False,
        "yolo_source": None,
        "tp1_filled": None,
        "trailing": None,
        "dead_alpha": None,
        "error": None,
    }


class GuardianCycle:
    def __init__(self, target_env, dry_run=False, close_dead_alpha=False, log_dir=None, mode="once", interval_seconds=None):
        self.env = target_env
        self.dry_run = bool(dry_run)
        self.close_dead_alpha = bool(close_dead_alpha)
        self.log_dir = log_dir or DEFAULT_LOG_DIR
        now = int(time.time())
        self.state = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": now,
            "timestamp_utc": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "env": target_env,
            "dry_run": self.dry_run,
            "mode": mode,
            "interval_seconds": interval_seconds,
            "cycle_ok": False,
            "positions": [],
            "actions": [],
            "errors": [],
        }

    # -- bookkeeping -------------------------------------------------------
    def error(self, symbol, stage, err):
        self.state["errors"].append({"symbol": symbol, "stage": stage, "error": str(err)})

    def action(self, symbol, action_type, success, detail):
        rec = {
            "timestamp": int(time.time()),
            "env": self.env,
            "symbol": symbol,
            "type": action_type,
            "dry_run": self.dry_run,
            "success": bool(success),
            "detail": detail,
        }
        self.state["actions"].append(rec)
        return rec

    # -- per-position steps ------------------------------------------------
    def _guard_position(self, p, view):
        sym = view["symbol"]
        is_long = view["side"] == "LONG"
        exit_side = "SELL" if is_long else "BUY"

        # 1. Orphan audit
        stops, err = eft.get_open_stop_orders(sym, exit_side, target_env=self.env)
        if err:
            # Unknown protection state: never act blindly, retry next cycle.
            view["error"] = err
            self.error(sym, "orders_query", err)
            return
        if stops:
            view["protected"] = True
            view["stop_price"] = eft._trigger_price(eft.tightest_stop(stops, is_long))
        elif self._heal_orphan(p, view) != "healed":
            return  # still unprotected, dry run, or closed: nothing left to trail

        # 2. Structural trailing (YOLO positions only after TP1)
        yolo, yolo_src = eft.detect_yolo_position(sym, leverage=p.get("leverage"))
        tp1_filled, _ = eft.detect_tp1_filled(sym, view["size"])
        view.update(is_yolo=yolo, yolo_source=yolo_src, tp1_filled=tp1_filled)
        if yolo and tp1_filled is not True:
            view["trailing"] = {"success": True, "updated": False, "reason": "yolo_before_tp1",
                                "message": "YOLO position: trailing deferred until TP1 fills (right-tail preservation)."}
        else:
            self._trail(p, view)

        # 3. Dead alpha (report only unless --close-dead-alpha)
        self._dead_alpha(view)

    def _heal_orphan(self, p, view):
        """Returns 'healed', 'closed', 'dry_run' or 'failed'."""
        sym = view["symbol"]
        if self.dry_run:
            self.action(sym, "orphan_heal", False, {"planned": True, "reason": "dry_run",
                                                    "message": "Unprotected position; would place a verified emergency stop."})
            self.error(sym, "orphan", "Position has no verified stop (dry run: not healed).")
            return "dry_run"
        heal = eft.heal_orphan_position(p, target_env=self.env, close_on_failure=True)
        detail = {k: heal.get(k) for k in ("reason", "verified", "healed_sl_price", "new_stop", "closed")}
        if heal.get("closed"):
            self.action(sym, "orphan_close", True, dict(detail, close_result=heal.get("close_result")))
            view["size"] = 0.0
            view["protected"] = True  # no exposure left
            view["error"] = "Position closed after failed orphan heal."
            return "closed"
        self.action(sym, "orphan_heal", bool(heal.get("verified")), detail)
        if heal.get("verified"):
            view["protected"] = True
            view["stop_price"] = heal.get("healed_sl_price")
            return "healed"
        view["error"] = f"Orphan heal failed ({heal.get('reason')})."
        self.error(sym, "orphan", view["error"])
        return "failed"

    def _trail(self, p, view):
        sym = view["symbol"]
        try:
            res = dem.update_position_to_structural_stop(sym, target_env=self.env, dry_run=self.dry_run, position=p)
        except Exception as e:
            self.error(sym, "trailing", e)
            view["trailing"] = {"success": False, "updated": False, "reason": "exception", "message": str(e)}
            return
        keys = ("success", "updated", "reason", "previous_sl", "new_sl", "planned_sl", "activation_reason",
                "reference_source", "message", "error", "warnings")
        view["trailing"] = {k: res.get(k) for k in keys if k in res}
        if res.get("updated"):
            view["stop_price"] = res.get("new_sl")
            self.action(sym, "trail_stop", True, view["trailing"])
        elif res.get("reason") == "dry_run":
            self.action(sym, "trail_stop", False, dict(view["trailing"], planned=True))
        elif not res.get("success"):
            if res.get("reason") == "new_stop_unverified":
                self.action(sym, "trail_stop", False, view["trailing"])
                if not res.get("previous_sl"):
                    view["protected"] = False
            self.error(sym, "trailing", res.get("error") or res.get("message"))

    def _dead_alpha(self, view):
        sym = view["symbol"]
        try:
            da = dem.check_dead_alpha_timeout(sym, target_env=self.env)
        except Exception as e:
            self.error(sym, "dead_alpha", e)
            return
        view["dead_alpha"] = {k: da.get(k) for k in ("status", "range_pct", "recommendation", "message")}
        if da.get("status") != "DEAD_ALPHA_STALLED" or not self.close_dead_alpha:
            return
        if self.dry_run:
            self.action(sym, "dead_alpha_close", False, {"planned": True, "reason": "dry_run", "dead_alpha": view["dead_alpha"]})
            return
        res = eft.close_position_market(sym, target_env=self.env)
        self.action(sym, "dead_alpha_close", bool(res.get("success")), {"dead_alpha": view["dead_alpha"], "result": res})

    def _protect_pending(self):
        """Post-fill protection of resting entries (planned SL/TPs) BEFORE the orphan audit, so a freshly filled
        entry gets its planned stop instead of the emergency orphan stop. No-op without logs/pending_entries.json."""
        try:
            res = eft.protect_pending_entries(target_env=self.env, dry_run=self.dry_run)
        except Exception as e:
            self.error(None, "pending_entries", f"{type(e).__name__}: {e}")
            return
        for a in res.get("actions", []):
            self.action(a.get("symbol"), a.get("type"), a.get("success"),
                        dict(a.get("detail") or {}, pending_entry_key=a.get("key")))
        for e in res.get("errors", []):
            self.error(e.get("symbol"), f"pending_{e.get('stage')}", e.get("error"))
        if not res.get("ok") and not res.get("errors"):
            self.error(None, "pending_entries", "protect_pending_entries reported failure")

    def _check_unknown_entries(self):
        """Report-only (Issue #46): opening orders resting on the exchange without a logs/pending_entries.json record
        (deleted registry, manual order) would get no SL on fill. Each one is an unknown_resting_entry action plus a
        pending_unknown_entry error (cycle_ok false); they are never cancelled here (could be operator orders)."""
        try:
            unknown, err = eft.find_unregistered_resting_entries(self.env)
        except Exception as e:
            unknown, err = [], f"{type(e).__name__}: {e}"
        if err:
            self.error(None, "pending_unknown_entry", f"Cannot cross-check resting entries against the registry: {err}")
            return
        for u in unknown:
            msg = (f"{u['kind']} entry {u['id']} rests on the exchange without a logs/pending_entries.json record: "
                   "no Stop Loss on fill. Cancel it or restore its record.")
            self.action(u["symbol"], "unknown_resting_entry", False, dict(u, message=msg))
            self.error(u["symbol"], "pending_unknown_entry", msg)

    # -- cycle -------------------------------------------------------------
    def run(self):
        self._protect_pending()
        try:
            pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=self.env)
        except Exception as e:
            pos_res = {"error": str(e)}
        if not isinstance(pos_res, list):
            self.error(None, "positions_sync", f"Position query failed: {pos_res}")
            self._check_unknown_entries()
            return self.finish()

        for p in pos_res:
            try:
                if _f(p.get("positionAmt")) == 0:
                    continue
                view = _position_view(p)
            except Exception as e:
                self.error(None, "position_parse", e)
                continue
            self.state["positions"].append(view)
            try:
                self._guard_position(p, view)
            except Exception as e:
                view["error"] = f"{type(e).__name__}: {e}"
                self.error(view["symbol"], "exception", f"{view['error']}\n{traceback.format_exc(limit=3)}")
        self._check_unknown_entries()
        return self.finish()

    def finish(self):
        self.state["cycle_ok"] = not self.state["errors"] and all(v["protected"] for v in self.state["positions"])
        self.persist()
        return self.state

    def persist(self):
        try:
            atomic_write_json(os.path.join(self.log_dir, STATE_FILE_NAME), self.state)
        except Exception as e:
            print(f"guardian: failed to write state: {e}", file=sys.stderr)
        actions_path = os.path.join(self.log_dir, ACTIONS_FILE_NAME)
        for rec in self.state["actions"]:
            try:
                atomic_append_jsonl(actions_path, rec)
            except Exception as e:
                print(f"guardian: failed to append action: {e}", file=sys.stderr)


def run_cycle(target_env=None, dry_run=False, close_dead_alpha=False, log_dir=None, mode="once", interval_seconds=None):
    target_env = resolve_env(target_env)
    return GuardianCycle(target_env, dry_run=dry_run, close_dead_alpha=close_dead_alpha, log_dir=log_dir,
                         mode=mode, interval_seconds=interval_seconds).run()


def format_state(state):
    lines = [f"[{state['timestamp_utc']}] GUARDIAN {state['env'].upper()}{' (DRY RUN)' if state['dry_run'] else ''}: "
             f"{len(state['positions'])} position(s), {len(state['actions'])} action(s), {len(state['errors'])} error(s)"
             f" -> {'OK' if state['cycle_ok'] else 'ATTENTION'}"]
    for v in state["positions"]:
        trail = (v.get("trailing") or {}).get("message") or ""
        da = (v.get("dead_alpha") or {}).get("status") or ""
        lines.append(f"  - {v['symbol']} {v['side']} size {v['size']} | protected: {'yes' if v['protected'] else 'NO'}"
                     f" | SL {v['stop_price']} | {da} {('| ' + trail) if trail else ''}".rstrip())
    for a in state["actions"]:
        lines.append(f"  * action {a['type']} {a['symbol']} success={a['success']}{' (dry run)' if a['dry_run'] else ''}")
    for e in state["errors"]:
        lines.append(f"  ! {e['stage']} {e['symbol'] or ''}: {str(e['error']).splitlines()[0]}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Position guardian: orphan heal, structural trailing, dead-alpha report")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Seconds between cycles in loop mode")
    parser.add_argument("--env", choices=["prod", "testnet"], default=None, help="Target environment; defaults to utils.env_resolver.resolve_env()")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Compute decisions but never send write requests")
    parser.add_argument("--close-dead-alpha", action="store_true", dest="close_dead_alpha", help="Close (reduce-only) positions flagged as dead alpha")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print the cycle state as JSON")
    args = parser.parse_args(argv)

    try:
        target_env = resolve_env(args.env)
    except ValueError as e:
        print(json.dumps({"success": False, "error": f"Invalid environment: {e}"}))
        return 1

    interval = max(int(args.interval), 10)
    mode = "once" if args.once else "loop"

    def one_cycle():
        try:
            state = run_cycle(target_env, dry_run=args.dry_run, close_dead_alpha=args.close_dead_alpha,
                              mode=mode, interval_seconds=None if args.once else interval)
        except Exception as e:  # never let a cycle crash the loop
            print(f"guardian: cycle failed: {e}", file=sys.stderr)
            return False
        print(json.dumps(state, indent=2) if args.json_output else format_state(state), flush=True)
        return state["cycle_ok"]

    if args.once:
        return 0 if one_cycle() else 1

    while True:
        one_cycle()
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
