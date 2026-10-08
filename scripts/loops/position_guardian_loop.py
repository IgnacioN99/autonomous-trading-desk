#!/usr/bin/env python3
"""
position_guardian_loop.py - Deterministic background guardian for open Binance Futures positions.

Replaces the retired crypto_radar MCP tools update_trailing_stop_structural,
audit_and_trail_all_positions, check_dead_alpha and audit_orphan_positions.

Per cycle:
  0. Pending entries (execute_futures_trade.protect_pending_entries, same as --protect-pending): a filled
     resting entry from logs/pending_entries.json gets its planned SL (its pre-armed stop when still verified,
     issue #36; else placed and verified, else reduce-only close) and, once the entry order is gone, its TPs sized
     from the actual position; expired unfilled entries are cancelled with their pre-armed stop. Runs first, so a
     fresh fill gets the planned stop instead of the orphan emergency stop.
  1. Sync open positions (GET /fapi/v2/positionRisk).
  2. Orphan audit: a position without a verified protective stop is auto-healed with a verified
     emergency stop (execute_futures_trade.heal_orphan_position); if the stop cannot be verified,
     the position is closed with a reduce-only market order (fail-safe auto-destruct policy; no second P0 when
     step 0's failed crossed close already reported the symbol this cycle, issue #160). An unreadable stop
     listing is UNKNOWN (no action) until it persists STOP_UNKNOWN_ESCALATE_AFTER cycles (issue #173): then
     eft.heal_unknown_stop (no close) and a CRITICAL/P0 report (see stop_unknown_cycles).
  3. Structural trailing (dynamic_exit_manager.update_position_to_structural_stop): place-then-cancel,
     never loosens, stops re-read right before a write. YOLO positions are skipped until TP1 has filled
     (right-tail preservation). is_yolo / yolo_source / tp1_filled come from dem's result (its matched trade
     reference; issue #163: TP1 is unknown when the reference is unverified). Activation
     gate (Issue #95): the planned SL is kept (reason "trail_not_activated") until +1.0R of planned risk or
     +2.0x ATR_15m of favourable excursion since entry on closed 15m bars, or TP1 fill; the Chandelier stop is
     then anchored to the extreme since entry. Take-profit orders are never re-based. userTrades is read at most
     once per symbol per cycle (shared with step 4). "reference_unverified" is reported once per position (not
     repeated while the previous state already flags it). An unreadable / corrupt logs/trades_audit.jsonl
     (audit_health) files one issue per state change through report_agent_issue (never in --dry-run, never
     stops the loop).
  4. Dead-alpha check: reported only; positions are closed (reduce-only) only with --close-dead-alpha.
     DEAD_ALPHA_STALLED requires BOTH the 15m range stall (last 6 closed 15m bars < 0.40%) AND the shared
     holding-time verdict also used by the doctor's watchdog (utils/position_timing.py: held >= 4h, mark within
     1.2% of entry, |ROE| < 15%; entry time from Binance fills, then trades_audit). Issue #92: a stall alone no
     longer qualifies (status STALLED_WITHIN_HORIZON), an unknown holding time is never closed
     (status UNKNOWN_HOLDING_TIME), and a holding time from trades_audit (not Binance fills) is report-only
     (close_blocked). The holding time is only looked up when the 15m stall fires.
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
  python3 scripts/loops/position_guardian_loop.py [--interval 60] [--env prod|testnet] [--close-dead-alpha]
      [--log-file logs/guardian.log]
  (PROD resting STOP_MARKET / LIMIT entries require a running loop with --interval <= 120; the default interval is
  execute_futures_trade.GUARDIAN_MAX_INTERVAL_FOR_RESTING // 2 = 60s and a larger one prints a warning at start)
  --log-file tees everything printed to a rotating file (5 MiB x 3 backups, UTF-8; relative to the repo root); if it
  cannot be opened, one stderr line is printed and the guardian runs with stdout/stderr only.
  Loop mode (not --once, not --dry-run) holds a non-blocking per-env lock on logs/guardian_loop.<env>.lock with the
  holder's {"env", "interval_seconds", "pid", "started_ts"} written into it: a second loop of the same env prints
  "another guardian loop for <env> is already running (interval <n>s, pid <pid>); exiting" and exits 0 (so a
  supervisor does not restart it). A loop started before issue #167 holds the legacy logs/guardian_loop.lock: a new
  loop then prints "a pre-upgrade guardian loop holds logs/guardian_loop.lock; restart the guardian task; exiting".
  When the lock cannot be taken for another reason the loop runs unlocked and records it as lock_warning (the
  doctor shows it).

Exit code (--once): 0 when the cycle completed without errors and every position ends protected, else 1.

State file (logs/guardian_state.json):
  {
    "schema_version": 1,
    "timestamp": int, "timestamp_utc": str, "env": "prod" | "testnet", "dry_run": bool,
    "mode": "loop" | "once",           # "loop" when running with --interval (no --once)
    "interval_seconds": int | null,    # loop interval; PROD resting entries need a loop with <= 120s
                                       # (execute_futures_trade.check_guardian_alive)
    "lock_warning": str | null,        # set when this process runs without the single-instance lock
    "audit_health": "ok" | "unreadable" | "corrupt" | null,  # logs/trades_audit.jsonl as seen by trailing; carried
                                       # forward from the previous state when no trailing evaluation ran
    "cycle_ok": bool,                 # no errors and every position protected at the end of the cycle
    "error_stages": [str],             # distinct stages of "errors" (issue #40): positions_sync or a pending_* stage
                                       # other than pending_unknown_entry makes PROD reject new resting entries
    "positions": [{
      "symbol": str, "side": "LONG" | "SHORT", "size": float, "entry_price": float, "mark_price": float,
      "leverage": int, "unrealized_pnl": float, "liquidation_price": float,
      "protected": bool, "stop_price": float | null,
      "is_yolo": bool, "yolo_source": str | null, "tp1_filled": bool | null,  # from dem's result
      "reference_unverified": bool,    # this cycle's dem reference had no userTrades open time
      "stop_unknown_cycles": int,      # consecutive cycles whose stop read failed (0 after a successful read; carried
                                       # from the previous state by symbol + side, so --once runs beside a live loop
                                       # do not advance it). At each multiple of STOP_UNKNOWN_ESCALATE_AFTER (3):
                                       # stop_unknown_heal action (eft.heal_unknown_stop, never a close) and a
                                       # CRITICAL/P0 report at the first crossing or when the heal "failed"
      "trailing": {"success", "updated", "reason", "previous_sl", "new_sl"?, "planned_sl"?,
                   "activation_reason"?: "tp1_filled" | "r_multiple" | "atr_expansion" | null,
                   "reference_source"?: "trade_audit" | "current_stop", "message",
                   "warnings"?: [str]} | null,  # "reference_unverified" (no userTrades open time),
                                                  # "audit_unreadable", "audit_corrupt_lines:<n>", cancel errors
      "dead_alpha": {"status": "DEAD_ALPHA_STALLED" | "HEALTHY_MOMENTUM" | "STALLED_WITHIN_HORIZON" |
                                "UNKNOWN_HOLDING_TIME" | "UNKNOWN", "range_pct", "recommendation", "message",
                     # only when the 15m stall fired:
                     "elapsed_hours"?: float | null, "entry_time_source"?: "userTrades" | "trades_audit" | "UNKNOWN",
                     "holding_verdict"?: "DEAD_ALPHA" | "HEALTHY" | "UNKNOWN", "close_blocked"?: str} | null,
      "error": str | null
    }],
    "actions": [ACTION, ...],
    "errors": [{"symbol": str | null, "stage": str, "error": str}],
    "pending_warnings": [{"key", "symbol", "stage", "warning"}]  # protect_pending_entries "warnings" (issue #156):
                                       # loss_cap_check / qty_check deferrals, loss_cap_drift, registry_lock,
                                       # deferral_report; printed, never errors (no effect on cycle_ok or liveness)
  }

Action record (also one JSON line in logs/guardian_actions.jsonl):
  {"timestamp": int, "env": str, "symbol": str, "dry_run": bool, "success": bool,
   "type": "orphan_heal" | "orphan_close" | "trail_stop" | "dead_alpha_close" | "pending_protect_sl" |
           "pending_tp_placed" | "pending_abort" | "pending_timeout_cancel" | "pending_dropped" |
           "pending_sl_crossed_close" | "pending_record_mismatch" | "unknown_resting_entry" (report only, success
           false) | "stop_unknown_heal" (success = heal result healed/kept), "detail": {...}}

A --once run does not overwrite the state of a live loop (mode "loop", fresh by check_guardian_alive's age rule):
it prints its result and appends its actions only (issue #40). Likewise a non-PROD cycle never overwrites a live
PROD loop's state (issue #167: the PROD liveness attestation wins); a PROD loop always writes.

Request weight per cycle (Binance-documented values at the time of writing, may change; USD-M limit 2400/min per
IP, shared with the executor and the scanners): GET /fapi/v2/positionRisk (all symbols, 5);
protect_pending_entries (calls only for pending records); all-symbol GET /fapi/v1/openAlgoOrders and
/fapi/v1/openOrders (find_unregistered_resting_entries, 40 each); per position: symbol openAlgoOrders twice (orphan
audit + dem, 1 each), exchangeInfo (calculate_structural_stop, 1), 15m klines limit=99 and limit=10 (1 each),
userTrades at most once (5); per actual stop write: one more openAlgoOrders re-read, exchangeInfo, the POST, the
verification reads and the DELETE. About 85 + ~10 per position per cycle: at the 60s default even 10 positions use
well under 10% of the per-minute limit.

Scheduling: on Windows (WSL) install it as a Task Scheduler task that starts the loop at logon and restarts it on
failure: python3 scripts/install_guardian_service.py --install --env prod (--status, --uninstall, --dry-run).
Only a loop with --interval <= 120 counts as a live guardian; --once runs never do.
"""

import os
import sys
import time
import json
import datetime
import argparse
import errno
import logging
import logging.handlers
import traceback

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None

# Ensure local path resolution (scripts/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import execute_futures_trade as eft
import dynamic_exit_manager as dem
from utils.env_resolver import resolve_env
from utils.atomic_writer import atomic_write_json, atomic_append_jsonl
from utils import position_timing as pt

SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING // 2
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LOG_DIR = os.path.join(BASE_DIR, "logs")
STATE_FILE_NAME = "guardian_state.json"
ACTIONS_FILE_NAME = "guardian_actions.jsonl"
LOCK_FILE_TEMPLATE = "guardian_loop.{env}.lock"
LEGACY_LOCK_FILE_NAME = "guardian_loop.lock"  # shared lock of loops started before issue #167
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3
STOP_UNKNOWN_ESCALATE_AFTER = 3  # consecutive UNKNOWN stop reads before heal_unknown_stop + P0 report (issue #173)


def lock_file_name(env):
    """Per-env single-instance lock file name (issue #167): a TESTNET loop never blocks the PROD one."""
    return LOCK_FILE_TEMPLATE.format(env=env)


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
        "reference_unverified": False,
        "stop_unknown_cycles": 0,
        "trailing": None,
        "dead_alpha": None,
        "error": None,
    }


def holding_verdict(p, view, target_env, now_ts=None, *, fetch=None):
    """Shared holding-time dead-alpha verdict for one positionRisk row (utils/position_timing, issue #92): entry time
    from Binance fills, then trades_audit, else UNKNOWN; same criteria and ROE as trading_drift_watchdog. fetch:
    send_signed_request-like callable for the userTrades read (the cycle's per-symbol cache)."""
    entry_ts, source = pt.resolve_entry_time(view["symbol"], view["side"], p.get("positionAmt"), target_env,
                                             entry_price=view["entry_price"], fetch=fetch or eft.send_signed_request,
                                             audit_path=os.path.join(eft._workspace_dir(), "logs", "trades_audit.jsonl"))
    elapsed = pt.holding_hours(entry_ts, now_ts)
    res = pt.assess_dead_alpha(elapsed_hours=elapsed, entry_price=view["entry_price"], mark_price=view["mark_price"],
                               roe_pct=pt.position_roe_pct(p))
    res["entry_time_source"] = source
    return res


def _crossed_close_reported(action):
    """Issue #160: True when a protect-pending action is a failed crossed close that filed a P0 (eft crossed_close:
    not flat, no stop kept and no verified orphan-heal stop). A dry run has no "flat" key and never reports."""
    if not isinstance(action, dict) or action.get("type") != "pending_sl_crossed_close" or action.get("success"):
        return False
    detail = action.get("detail") or {}
    return (detail.get("flat") is False and not detail.get("kept_stops")
            and not (detail.get("heal") or {}).get("success"))


class GuardianCycle:
    def __init__(self, target_env, dry_run=False, close_dead_alpha=False, log_dir=None, mode="once", interval_seconds=None,
                 lock_warning=None):
        self.env = target_env
        self.dry_run = bool(dry_run)
        self.close_dead_alpha = bool(close_dead_alpha)
        self.log_dir = log_dir or DEFAULT_LOG_DIR
        self._user_trades = {}  # (SYMBOL, limit) -> userTrades response, shared by trailing and dead alpha
        self._audit_kinds = set()  # dem's raw audit_* warnings of this cycle (issue #163 escalation)
        self._audit_resolved = False  # at least one trailing evaluation resolved the trade reference
        self._crossed_close_reported = set()  # symbols whose failed crossed close filed a P0 this cycle (issue #160)
        self._previous = {}  # previous guardian_state.json of this env (read at the start of run())
        now = int(time.time())
        self.state = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": now,
            "timestamp_utc": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "env": target_env,
            "dry_run": self.dry_run,
            "mode": mode,
            "interval_seconds": interval_seconds,
            "lock_warning": lock_warning,
            "audit_health": None,
            "cycle_ok": False,
            "positions": [],
            "actions": [],
            "errors": [],
            "pending_warnings": [],
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

    def _fetch(self, method, endpoint, params=None, target_env=None, **kw):
        """send_signed_request with a per-cycle cache for GET /fapi/v1/userTrades keyed by (SYMBOL, limit): trailing
        and dead alpha share one read per symbol (issue #163). Lists and error payloads are cached alike; an
        exception propagates uncached. Every other request passes straight through."""
        if method == "GET" and endpoint == "/fapi/v1/userTrades":
            p = params or {}
            key = (str(p.get("symbol")).upper(), p.get("limit"))
            if key not in self._user_trades:
                self._user_trades[key] = eft.send_signed_request(method, endpoint, params, target_env=target_env, **kw)
            return self._user_trades[key]
        return eft.send_signed_request(method, endpoint, params, target_env=target_env, **kw)

    def _load_previous_state(self):
        """Previous logs/guardian_state.json when it is a dict for this env, else {}. Read-only, never raises."""
        try:
            with open(os.path.join(self.log_dir, STATE_FILE_NAME), "r", encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            return {}
        return prev if isinstance(prev, dict) and prev.get("env") == self.env else {}

    def _was_reference_unverified(self, view):
        for v in self._previous.get("positions") or []:
            if (isinstance(v, dict) and v.get("symbol") == view["symbol"] and v.get("side") == view["side"]
                    and v.get("reference_unverified") is True):
                return True
        return False

    def _previous_stop_unknown_cycles(self, view):
        """stop_unknown_cycles of the same position (symbol + side) in the previous state, else 0."""
        for v in self._previous.get("positions") or []:
            if isinstance(v, dict) and v.get("symbol") == view["symbol"] and v.get("side") == view["side"]:
                try:
                    return max(int(v.get("stop_unknown_cycles") or 0), 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    def _escalate_unknown_stop(self, p, view):
        """Issue #173: a stop read that stays UNKNOWN for a multiple of STOP_UNKNOWN_ESCALATE_AFTER consecutive cycles
        gets eft.heal_unknown_stop (never a close; -4130 = a closePosition stop exists = kept) and a CRITICAL/P0 issue
        at the first crossing or whenever the heal result is "failed". --dry-run: planned action only, no report."""
        sym, count = view["symbol"], view["stop_unknown_cycles"]
        if count < STOP_UNKNOWN_ESCALATE_AFTER or count % STOP_UNKNOWN_ESCALATE_AFTER:
            return
        if self.dry_run:
            self.action(sym, "stop_unknown_heal", False, {"planned": True, "reason": "dry_run",
                                                         "stop_unknown_cycles": count})
            return
        res = eft.heal_unknown_stop(sym, p, target_env=self.env)
        result = res.get("result")
        heal = res.get("heal") or {}
        self.action(sym, "stop_unknown_heal", result in ("healed", "kept"),
                    {"stop_unknown_cycles": count, "result": result, "detail": res.get("detail"),
                     "healed_sl_price": heal.get("healed_sl_price"), "new_stop": heal.get("new_stop"),
                     "redundant_stops": res.get("redundant_stops"), "note": res.get("note")})
        if result == "healed":
            view["protected"] = True
            view["stop_price"] = heal.get("healed_sl_price")
        elif result == "kept":
            view["protected"] = True
        if count == STOP_UNKNOWN_ESCALATE_AFTER or result == "failed":
            _report_unknown_stop(self.env, sym, count, result, res.get("detail"))

    # -- per-position steps ------------------------------------------------
    def _guard_position(self, p, view):
        sym = view["symbol"]
        is_long = view["side"] == "LONG"
        exit_side = "SELL" if is_long else "BUY"

        # 1. Orphan audit
        stops, err = eft.get_open_stop_orders(sym, exit_side, target_env=self.env)
        if err:
            # Unknown protection state: never act blindly, retry next cycle; a persistent UNKNOWN escalates.
            view["error"] = err
            self.error(sym, "orders_query", err)
            view["stop_unknown_cycles"] = self._previous_stop_unknown_cycles(view) + 1
            self._escalate_unknown_stop(p, view)
            return
        if stops:
            view["protected"] = True
            view["stop_price"] = eft._trigger_price(eft.tightest_stop(stops, is_long))
        elif self._heal_orphan(p, view) != "healed":
            return  # still unprotected, dry run, or closed: nothing left to trail

        # 2. Structural trailing (dem defers YOLO positions until TP1 and reports is_yolo / tp1_filled)
        self._trail(p, view)

        # 3. Dead alpha (report only unless --close-dead-alpha)
        self._dead_alpha(p, view)

    def _heal_orphan(self, p, view):
        """Returns 'healed', 'closed', 'dry_run' or 'failed'."""
        sym = view["symbol"]
        if self.dry_run:
            self.action(sym, "orphan_heal", False, {"planned": True, "reason": "dry_run",
                                                    "message": "Unprotected position; would place a verified emergency stop."})
            self.error(sym, "orphan", "Position has no verified stop (dry run: not healed).")
            return "dry_run"
        # Issue #160: the heal and its close always run; only a second P0 for the same failure is skipped.
        heal = eft.heal_orphan_position(p, target_env=self.env, close_on_failure=True,
                                        report_failure=str(sym).upper() not in self._crossed_close_reported)
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
            res = dem.update_position_to_structural_stop(sym, target_env=self.env, dry_run=self.dry_run, position=p,
                                                         fetch=self._fetch)
        except Exception as e:
            self.error(sym, "trailing", e)
            view["trailing"] = {"success": False, "updated": False, "reason": "exception", "message": str(e)}
            return
        # YOLO / TP1 come from dem's matched trade reference (issue #163); an early return keeps the view defaults.
        for k in ("is_yolo", "yolo_source", "tp1_filled"):
            if k in res:
                view[k] = res[k]
        if "is_yolo" in res:
            self._audit_resolved = True
        raw_warnings = list(res.get("warnings") or [])
        self._audit_kinds.update(w for w in raw_warnings if isinstance(w, str) and w.startswith("audit_"))
        view["reference_unverified"] = "reference_unverified" in raw_warnings
        keys = ("success", "updated", "reason", "previous_sl", "new_sl", "planned_sl", "activation_reason",
                "reference_source", "message", "error", "warnings")
        view["trailing"] = {k: res.get(k) for k in keys if k in res}
        if "warnings" in view["trailing"]:
            warnings = list(raw_warnings)
            if view["reference_unverified"] and self._was_reference_unverified(view):
                # Reported once per position: already flagged in the previous cycle's state.
                warnings = [w for w in warnings if w != "reference_unverified"]
            view["trailing"]["warnings"] = warnings
        if res.get("updated"):
            view["stop_price"] = res.get("new_sl")
            self.action(sym, "trail_stop", True, view["trailing"])
        elif res.get("reason") == "dry_run":
            self.action(sym, "trail_stop", False, dict(view["trailing"], planned=True))
        elif not res.get("success"):
            if res.get("reason") == "new_stop_unverified":
                self.action(sym, "trail_stop", False, view["trailing"])
                if not res.get("current_sl"):  # the stops re-read right before the write (issue #167)
                    view["protected"] = False
            self.error(sym, "trailing", res.get("error") or res.get("message"))

    def _dead_alpha(self, p, view):
        """Dead alpha = the shared holding-time verdict (utils/position_timing.assess_dead_alpha: held >= 4h AND
        within 1.2% of entry AND |ROE| < 15%, same as the doctor's watchdog) AND the 15m range stall
        (dynamic_exit_manager.check_dead_alpha_timeout). Issue #92: a stall alone no longer qualifies, so a position
        is never flagged (or closed with --close-dead-alpha) before max_hours, and an UNKNOWN holding time is
        report-only (status UNKNOWN_HOLDING_TIME), never closed. The holding time (a userTrades call) is only
        looked up when the 15m stall fires. --close-dead-alpha closes only per pt.dead_alpha_close_decision (the
        watchdog's rule): a trades_audit-sourced DEAD_ALPHA_STALLED is report-only (close_blocked, REVIEW_MANUALLY)."""
        sym = view["symbol"]
        try:
            da = dem.check_dead_alpha_timeout(sym, target_env=self.env)
        except Exception as e:
            self.error(sym, "dead_alpha", e)
            return
        view["dead_alpha"] = {k: da.get(k) for k in ("status", "range_pct", "recommendation", "message")}
        if da.get("status") != "DEAD_ALPHA_STALLED":
            return  # no 15m stall: no holding-time lookup (saves a userTrades call per position per cycle)
        try:
            verdict = holding_verdict(p, view, self.env, fetch=self._fetch)
        except Exception as e:
            verdict = {"verdict": pt.VERDICT_UNKNOWN, "elapsed_hours": None, "entry_time_source": pt.SOURCE_UNKNOWN}
            self.error(sym, "dead_alpha", f"holding time unresolved: {type(e).__name__}: {e}")
        view["dead_alpha"].update(elapsed_hours=verdict.get("elapsed_hours"),
                                  entry_time_source=verdict.get("entry_time_source"),
                                  holding_verdict=verdict.get("verdict"))
        if verdict.get("verdict") != pt.VERDICT_DEAD_ALPHA:
            if verdict.get("verdict") == pt.VERDICT_UNKNOWN:
                view["dead_alpha"].update(status="UNKNOWN_HOLDING_TIME", recommendation="HOLD",
                                          message=f"{sym}: 15m range stalled but holding time is UNKNOWN; not dead alpha.")
            else:
                view["dead_alpha"].update(status="STALLED_WITHIN_HORIZON", recommendation="HOLD",
                                          message=f"{sym}: 15m range stalled but the position is not dead alpha yet "
                                                  f"(held {verdict.get('elapsed_hours')}h, needs >= "
                                                  f"{pt.DEFAULT_MAX_HOURS}h and stagnant).")
        if view["dead_alpha"].get("status") != "DEAD_ALPHA_STALLED" or not self.close_dead_alpha:
            return
        allowed, reason = pt.dead_alpha_close_decision(verdict.get("verdict"), verdict.get("entry_time_source"),
                                                       da.get("status"))
        if not allowed:
            # Shared close rule (issue #173): e.g. holding time from trades_audit is report only, never a close.
            view["dead_alpha"].update(recommendation="REVIEW_MANUALLY", close_blocked=reason)
            return
        if self.dry_run:
            self.action(sym, "dead_alpha_close", False, {"planned": True, "reason": "dry_run", "dead_alpha": view["dead_alpha"]})
            return
        res = eft.close_position_market(sym, target_env=self.env)
        self.action(sym, "dead_alpha_close", bool(res.get("success")), {"dead_alpha": view["dead_alpha"], "result": res})
        if not res.get("success"):
            self.error(sym, "dead_alpha_close", res.get("error") or "close not confirmed")

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
            if _crossed_close_reported(a):
                self._crossed_close_reported.add(str(a.get("symbol") or "").upper())
        for e in res.get("errors", []):
            self.error(e.get("symbol"), f"pending_{e.get('stage')}", e.get("error"))
        for w in res.get("warnings") or []:
            # Issue #156: deferrals, loss-cap drift and registry-lock notes are reported, never errors (no cycle_ok
            # / check_guardian_alive effect).
            self.state["pending_warnings"].append({"key": w.get("key"), "symbol": w.get("symbol"),
                                                   "stage": w.get("stage"), "warning": str(w.get("warning"))})
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
        self._previous = self._load_previous_state()
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
        self.state["error_stages"] = sorted({str(e.get("stage")) for e in self.state["errors"]})
        previous_health = self._previous.get("audit_health")
        health = self._audit_health(previous_health)
        self.state["audit_health"] = health
        self.persist()
        if health in ("unreadable", "corrupt") and health != previous_health and not self.dry_run:
            _report_audit_health(self.env, health, sorted(self._audit_kinds))
        return self.state

    def _audit_health(self, previous_health):
        """Issue #163: "unreadable" / "corrupt" from dem's raw audit warnings of this cycle, "ok" when a trailing
        evaluation resolved the reference without them, else the previous state's value (no evaluation ran)."""
        if "audit_unreadable" in self._audit_kinds:
            return "unreadable"
        if any(k.startswith("audit_corrupt_lines") for k in self._audit_kinds):
            return "corrupt"
        if self._audit_resolved:
            return "ok"
        return previous_health if previous_health in ("ok", "unreadable", "corrupt") else None

    def _keeps_loop_state(self, path):
        """Env of a live guardian loop whose state this cycle must not overwrite, else None. Issue #40: a --once run
        never overwrites a live loop's state (any env). Issue #167: a non-PROD cycle never overwrites a live PROD
        loop's state (PROD liveness attestation); a PROD loop always writes. Live = fresh by
        check_guardian_alive's age rule (guardian_loop_state_fresh)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError):
            return None
        if not eft.guardian_loop_state_fresh(existing):
            return None
        if self.state.get("mode") == "once" or (existing.get("env") == "prod" and self.env != "prod"):
            return str(existing.get("env") or "unknown-env")
        return None

    def persist(self):
        state_path = os.path.join(self.log_dir, STATE_FILE_NAME)
        owner = self._keeps_loop_state(state_path)
        if owner is not None:
            print(f"guardian: a live {owner} guardian loop owns logs/guardian_state.json; this cycle is not recorded "
                  "there (its actions are still appended to logs/guardian_actions.jsonl)", file=sys.stderr)
        else:
            try:
                atomic_write_json(state_path, self.state)
            except Exception as e:
                print(f"guardian: failed to write state: {e}", file=sys.stderr)
        actions_path = os.path.join(self.log_dir, ACTIONS_FILE_NAME)
        for rec in self.state["actions"]:
            try:
                atomic_append_jsonl(actions_path, rec)
            except Exception as e:
                print(f"guardian: failed to append action: {e}", file=sys.stderr)


def _report_audit_health(env, health, detail):
    """Issue #163: file an issue when logs/trades_audit.jsonl turns unreadable or corrupt (called once per state
    change). Never raises: reporting must never change cycle_ok or stop the loop. error_detail is stable so the
    reporter's 24h fingerprint dedups as a second layer."""
    try:
        import report_agent_issue
        severity, priority = ("HIGH", "P1") if health == "unreadable" else ("MEDIUM", "P2")
        report_agent_issue.report_issue(
            title=f"position_guardian_loop: logs/trades_audit.jsonl is {health}",
            error_detail=f"trades_audit.jsonl {health}",
            category="risk_gate", severity=severity, priority=priority,
            agent_name="position_guardian_loop",
            affected_files="logs/trades_audit.jsonl, scripts/dynamic_exit_manager.py:_resolve_trade_reference_full",
            context=(f"env={env}; warnings={', '.join(detail)}; trailing reference falls back to "
                     "the current stop; executor Gate 0A reads the same file"),
            remediation="Inspect logs/trades_audit.jsonl (malformed or unreadable lines); do not hand-edit it during trading.")
    except Exception as e:
        print(f"guardian: trades_audit health report could not be filed ({type(e).__name__}: {e})", file=sys.stderr)


def _report_unknown_stop(env, symbol, cycles, result, detail):
    """Issue #173: CRITICAL/P0 issue for a stop read UNKNOWN for `cycles` consecutive guardian cycles, with the
    heal_unknown_stop result. Never raises. error_detail carries the count and result, so a repeat at a later multiple
    after a failed heal is a new fingerprint while the reporter's 24h dedup absorbs exact repeats."""
    try:
        import report_agent_issue
        report_agent_issue.report_issue(
            title=f"position_guardian_loop: stop of {symbol} UNKNOWN for {cycles} consecutive cycles",
            error_detail=f"{symbol} stop UNKNOWN {cycles} cycles; heal {result}",
            category="risk_gate", severity="CRITICAL", priority="P0",
            agent_name="position_guardian_loop",
            affected_files="scripts/loops/position_guardian_loop.py:_escalate_unknown_stop",
            context=(f"env={env}; symbol={symbol}; stop_unknown_cycles={cycles}; heal result={result}; "
                     f"detail: {detail}"),
            remediation=("Check the position's stop on Binance now (openAlgoOrders unreadable); protect it or close "
                         "it with --close-position."))
    except Exception as e:
        print(f"guardian: unknown-stop report for {symbol} could not be filed ({type(e).__name__}: {e})",
              file=sys.stderr)


def run_cycle(target_env=None, dry_run=False, close_dead_alpha=False, log_dir=None, mode="once", interval_seconds=None,
              lock_warning=None):
    target_env = resolve_env(target_env)
    return GuardianCycle(target_env, dry_run=dry_run, close_dead_alpha=close_dead_alpha, log_dir=log_dir,
                         mode=mode, interval_seconds=interval_seconds, lock_warning=lock_warning).run()


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
    for w in state.get("pending_warnings") or []:
        lines.append(f"  ~ warning {w.get('stage')} {w.get('symbol') or ''}: "
                     f"{(str(w.get('warning')).splitlines() or [''])[0]}")
    if state.get("lock_warning"):
        lines.append(f"  ! lock: {state['lock_warning']}")
    return "\n".join(lines)


class _TeeStream:
    """Writes to the wrapped stream and sends every complete line to a file logger (--log-file). Logging is
    best-effort: a failure (or a re-entrant write while logging) never reaches the wrapped stream's caller."""

    def __init__(self, stream, logger):
        self._stream = stream
        self._logger = logger
        self._buf = ""
        self._logging = False

    def _log(self, line):
        if self._logging:
            return
        self._logging = True
        try:
            self._logger.info(line)
        except Exception:
            pass
        finally:
            self._logging = False

    def write(self, text):
        self._stream.write(text)
        if self._logging:  # e.g. a logging error report written to this stream
            return len(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._log(line)
        return len(text)

    def flush(self):
        if self._buf and not self._logging:
            line, self._buf = self._buf, ""
            self._log(line)
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _GuardianLogHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler whose errors (rollover rename blocked, disk full) go as one line to the raw stderr saved
    before the tee was installed, never back into the tee."""

    def __init__(self, *args, error_stream=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._error_stream = error_stream
        self._error_reported = False

    def handleError(self, record):
        if self._error_reported:
            return
        self._error_reported = True
        try:
            err = sys.exc_info()[1]
            stream = self._error_stream or sys.__stderr__
            stream.write(f"guardian: --log-file write failed ({type(err).__name__}: {err}); the loop keeps running, "
                         "further log errors are not reported\n")
            stream.flush()
        except Exception:
            pass


def _open_log_file(path, error_stream=None):
    """File-only logger with a rotating handler (LOG_FILE_MAX_BYTES x LOG_FILE_BACKUPS, UTF-8); a relative path
    resolves against the repo root. Handler errors go to error_stream (the raw stderr)."""
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handler = _GuardianLogHandler(path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8",
                                  error_stream=error_stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger = logging.Logger("position_guardian_loop.log_file")  # not registered globally: no handler build-up
    logger.addHandler(handler)
    return logger, handler


_LOCK_HELD_ERRNOS = {errno.EWOULDBLOCK, errno.EAGAIN}
_MSVCRT_LOCK_HELD_ERRNOS = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)}


def _try_lock(fh):
    """Non-blocking exclusive lock on an open file (flock on POSIX, msvcrt.locking on Windows); raises OSError."""
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt is not None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)


def _lock_held_errnos():
    return _LOCK_HELD_ERRNOS if fcntl is not None else _MSVCRT_LOCK_HELD_ERRNOS


def _close_quietly(fh):
    try:
        fh.close()
    except OSError:
        pass


def _legacy_lock_held(log_dir):
    """True only when the pre-issue-#167 shared <log_dir>/guardian_loop.lock exists and another process holds it
    (a loop started before the upgrade). Any other error counts as not held: it never blocks the guardian."""
    path = os.path.join(log_dir, LEGACY_LOCK_FILE_NAME)
    if not os.path.exists(path):
        return False
    try:
        fh = open(path, "a+")
    except OSError:
        return False
    try:
        _try_lock(fh)
    except OSError as e:
        _close_quietly(fh)
        return e.errno in _lock_held_errnos()
    _release_loop_lock(fh)
    return False


def _acquire_loop_lock(log_dir, env, interval_seconds=None):
    """Non-blocking exclusive lock on <log_dir>/guardian_loop.<env>.lock (issue #167: per env, so a TESTNET loop never
    blocks the PROD one). Returns (held_by_other, fh, info); fh stays open while the loop runs.
    - Held by another process (EWOULDBLOCK/EAGAIN, or EACCES/EDEADLOCK from msvcrt): info is the holder JSON written
      into the lock file ({"env", "interval_seconds", "pid", "started_ts"}) or None when unreadable. A held legacy
      guardian_loop.lock (a loop started before the upgrade) also counts: info {"legacy": True}.
    - Any other error (open failure, ENOLCK, EOPNOTSUPP): one stderr line, the loop runs unlocked and info is that
      warning text (recorded as lock_warning in the state): the lock never stops the guardian.
    - Acquired: the holder JSON is written into the file (best effort) and info is None."""
    name = lock_file_name(env)
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = open(os.path.join(log_dir, name), "a+")
    except OSError as e:
        warning = f"cannot open {name} ({e}); running without the single-instance lock"
        print(f"guardian: {warning}", file=sys.stderr)
        return False, None, warning
    try:
        _try_lock(fh)
    except OSError as e:
        if e.errno in _lock_held_errnos():
            try:
                fh.seek(0)
                info = json.loads(fh.read())
            except Exception:
                info = None
            _close_quietly(fh)
            return True, None, info if isinstance(info, dict) else None
        _close_quietly(fh)
        warning = f"cannot lock {name} ({e}); running without the single-instance lock"
        print(f"guardian: {warning}", file=sys.stderr)
        return False, None, warning
    if _legacy_lock_held(log_dir):
        _release_loop_lock(fh)
        return True, None, {"legacy": True}
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"env": env, "interval_seconds": interval_seconds, "pid": os.getpid(),
                             "started_ts": int(time.time())}))
        fh.flush()
    except OSError:
        pass
    return False, fh, None


def _release_loop_lock(fh):
    if fh is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Position guardian: orphan heal, structural trailing, dead-alpha report")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Seconds between cycles in loop mode")
    parser.add_argument("--env", choices=["prod", "testnet"], default=None, help="Target environment; defaults to utils.env_resolver.resolve_env()")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Compute decisions but never send write requests")
    parser.add_argument("--close-dead-alpha", action="store_true", dest="close_dead_alpha", help="Close (reduce-only) positions flagged as dead alpha")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print the cycle state as JSON")
    parser.add_argument("--log-file", dest="log_file", default=None,
                        help="Also write everything printed to this rotating log file (relative to the repo root)")
    args = parser.parse_args(argv)

    try:
        target_env = resolve_env(args.env)
    except ValueError as e:
        print(json.dumps({"success": False, "error": f"Invalid environment: {e}"}))
        return 1

    lock_fh = None
    lock_warning = None
    if not args.once and not args.dry_run:
        held_by_other, lock_fh, info = _acquire_loop_lock(DEFAULT_LOG_DIR, target_env, max(int(args.interval), 10))
        if held_by_other:
            if isinstance(info, dict) and info.get("legacy"):
                message = ("a pre-upgrade guardian loop holds logs/guardian_loop.lock; restart the guardian task; "
                           "exiting")
            elif isinstance(info, dict) and "interval_seconds" in info and "pid" in info:
                message = (f"another guardian loop for {target_env} is already running "
                           f"(interval {info.get('interval_seconds')}s, pid {info.get('pid')}); exiting")
            else:
                message = (f"another guardian loop for {target_env} is already running (holder details unavailable); "
                           "exiting")
            print(json.dumps({"success": True, "already_running": True, "env": target_env, "holder": info,
                              "message": message})
                  if args.json_output else message, flush=True)
            return 0
        if isinstance(info, str):
            lock_warning = info

    saved_streams = (sys.stdout, sys.stderr)
    handler = None
    try:
        if args.log_file:
            try:
                logger, handler = _open_log_file(args.log_file, error_stream=saved_streams[1])
            except Exception as e:
                handler = None
                try:
                    saved_streams[1].write(f"guardian: cannot open --log-file {args.log_file} ({type(e).__name__}: "
                                           f"{e}); running without the log file\n")
                    saved_streams[1].flush()
                except Exception:
                    pass
            if handler is not None:
                sys.stdout = _TeeStream(sys.stdout, logger)
                sys.stderr = _TeeStream(sys.stderr, logger)
        return _run_main(args, target_env, lock_warning=lock_warning)
    finally:
        if handler is not None:
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except Exception:
                    pass
            sys.stdout, sys.stderr = saved_streams
            handler.close()
        _release_loop_lock(lock_fh)


def _run_main(args, target_env, lock_warning=None):
    interval = max(int(args.interval), 10)
    mode = "once" if args.once else "loop"
    if not args.once and interval > eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING:
        print(f"guardian: WARNING --interval {interval}s exceeds {eft.GUARDIAN_MAX_INTERVAL_FOR_RESTING}s: PROD resting "
              f"STOP_MARKET/LIMIT entries will be rejected while this loop runs (use --interval "
              f"{DEFAULT_INTERVAL_SECONDS})", file=sys.stderr, flush=True)

    def one_cycle():
        try:
            state = run_cycle(target_env, dry_run=args.dry_run, close_dead_alpha=args.close_dead_alpha,
                              mode=mode, interval_seconds=None if args.once else interval,
                              lock_warning=lock_warning)
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
