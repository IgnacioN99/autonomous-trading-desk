#!/usr/bin/env python3
"""
trading_doctor.py - Pre-Flight Diagnostic & Health Sensor for Trading Desk.
Comprehensive verification of connectivity, clock synchronization, and orphan position auditing.

Verifies:
1. API Connectivity and Network Latency (< 800ms)
2. Clock Drift (< 1000ms against Binance server)
3. API Credentials and Permissions (Testnet / Mainnet)
4. Available Capital and USDT Balance
5. Forensic Orphan Position Audit (Fail CLOSED if position lacks Stop Loss on ledger)
6. State Ledger Freshness (session_state.json), plus WARNs for closed_today_summary.counted_by != "trades" and the
   cached Daily Loss Gate state (issue #207)
6b. Score calibration store (logs/score_calibration.json) stale or missing with PROD closed trades (WARN only, #207)
7. Barbell YOLO scan health (logs/yolo_scan_health.json; WARN only, when yolo_slot_enabled)
8. Position guardian loop liveness (check_guardian_alive; WARN only, with the install_guardian_service.py hint)
9. Python dependencies of the scanners (numpy, pydantic, statsmodels; WARN only)
10. [FEES] (issue #268, informational only, read-only): KEYS mode reads GET /fapi/v1/commissionRate (BTCUSDT maker /
   taker rate) and GET /fapi/v1/feeBurn (BNB fee discount ON / OFF); MCP mode prints "unavailable". A failed read is
   printed as unavailable; it never adds a warning or a critical failure, so it never changes the exit code.
11. [PREARM] / [FILL-STOP] (issue #273, informational only, read-only, last 24 h of logs/guardian_actions.jsonl): the
   resting-entry pre-arms rejected with -4509 (and how many were LIMIT entries) and the max / median fill-to-stop
   seconds of stops placed at fill by the guardian ([FILL-STOP] only when there is at least one).
User profile: not onboarded = critical; PROD also fails when config/user_profile.json is missing or unreadable
(a fallback example/default profile, issue #180).

Usage:
  python3 scripts/trading_doctor.py [--env testnet|mainnet] [--heal]
  Exit 0 if system is healthy and ready to trade.
  Exit 1 if a critical failure occurs (Fail CLOSED).
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import hmac
import hashlib

import re
import shlex
import statistics
import shutil
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
import sync_session_state as sss
from utils.env_resolver import resolve_env, is_prod_environment, find_workspace_root
from utils import position_timing as pt

# The temporal (dead-alpha) audit only runs on a ledger synced within this window (issue #92).
LEDGER_MAX_AGE_FOR_TEMPORAL_AUDIT_S = 300

HOOK_SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl")
HOOK_HEARTBEAT_MAX_AGE_S = 24 * 3600
HOOK_SELFTEST_ENV = "TRADING_HOOK_SELFTEST"
HOOK_HEARTBEAT_OVERRIDE_ENV = "PRE_TRADE_GUARD_HEARTBEAT_FILE"  # honoured by scripts/hooks/pre_trade_guard.py

# Synthetic direct Binance MCP order: the PreToolUse guard must ALWAYS deny it (choke-point enforcement).
SYNTHETIC_NEW_ORDER_PAYLOAD = {
    "conversationId": "trading-doctor-selftest",
    "stepIdx": 0,
    "modelName": "trading-doctor",
    "toolCall": {
        "name": "call_mcp_tool",
        "args": {
            "ServerName": "binance",
            "ToolName": "futures_usds.newOrder",
            "Arguments": {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"},
        },
    },
}


def _matcher_matches(matcher, tool_name: str) -> bool:
    """agy matcher semantics: '' or '*' match all tools, otherwise a regex over the tool name."""
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(str(matcher), tool_name) is not None
    except re.error:
        return False


def _hook_script_paths(command: str, hooks_dir: str) -> list:
    """Script files referenced by a hook command, resolved like agy does (cwd = directory containing hooks.json)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    paths = []
    for tok in tokens[1:]:
        if tok.startswith("-"):
            continue
        if tok.endswith(HOOK_SCRIPT_SUFFIXES) or "/" in tok:
            expanded = os.path.expanduser(tok)
            paths.append(expanded if os.path.isabs(expanded) else os.path.normpath(os.path.join(hooks_dir, expanded)))
    return paths


CLAUDE_SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")
# Tools the Claude Code PreToolUse matcher must route to pre_trade_guard.py (issues #49, #73)
CLAUDE_GUARDED_TOOLS = ("Bash", "PowerShell", "NotebookEdit", "Write", "Edit", "MultiEdit", "mcp__binance__x")
CLAUDE_GUARD_COMMAND_RE = re.compile(r"(?:^|[\\/\s\"'])pre_trade_guard\.py(?=$|[\s\"'])")


def check_claude_hook_matcher(base_dir: str) -> tuple:
    """Claude Code runtime check (issue #73): every tool in CLAUDE_GUARDED_TOOLS must be matched (_matcher_matches)
    by a PreToolUse entry of .claude/settings.json or .claude/settings.local.json (union of both) whose command runs
    pre_trade_guard.py (basename match: "$CLAUDE_PROJECT_DIR"/... and wsl.exe ... python3 /abs/... both count).
    Returns (critical, info) lists: no settings file -> skipped (agy-only install, info); an existing file that
    cannot be parsed or whose hooks / hooks.PreToolUse has the wrong type, or an uncovered tool -> critical. A file
    without hooks (permissions only) adds no coverage."""
    critical, info, covered, found = [], [], set(), []
    for rel in CLAUDE_SETTINGS_FILES:
        path = os.path.join(base_dir, *rel.split("/"))
        if not os.path.exists(path):
            continue
        found.append(rel)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # A file without hooks (Claude Code's own permissions-only settings.local.json) adds no coverage
            if not isinstance(cfg, dict) or not isinstance(cfg.get("hooks", {}), dict):
                raise ValueError("top-level value or 'hooks' is not an object")
            groups = cfg.get("hooks", {}).get("PreToolUse", [])
            if not isinstance(groups, list):
                raise ValueError("hooks.PreToolUse is not a list")
        except Exception as e:
            critical.append(f"{rel} cannot be parsed ({e}): the Claude Code PreToolUse guard cannot be verified.")
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            if not any(isinstance(h, dict) and CLAUDE_GUARD_COMMAND_RE.search(str(h.get("command") or ""))
                       for h in group["hooks"]):
                continue
            covered.update(t for t in CLAUDE_GUARDED_TOOLS if _matcher_matches(group.get("matcher"), t))
    if not found:
        info.append("Claude Code matcher check skipped: no .claude/settings.json or .claude/settings.local.json "
                    "(agy-only install).")
        return critical, info
    missing = [t for t in CLAUDE_GUARDED_TOOLS if t not in covered]
    if missing:
        critical.append(f"Claude Code PreToolUse matcher in {' / '.join(found)} does not route {', '.join(missing)} "
                        "to pre_trade_guard.py.")
    elif not critical:
        info.append(f"Claude Code PreToolUse matcher ({' / '.join(found)}) routes "
                    f"{', '.join(CLAUDE_GUARDED_TOOLS)} to pre_trade_guard.py.")
    return critical, info


def _last_json_object(text: str):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def read_hook_heartbeat(base_dir: str, now_ts: float = None) -> dict:
    """Reads logs/hook_heartbeat.json written by the PreToolUse guard on every live invocation."""
    path = os.path.join(base_dir, "logs", "hook_heartbeat.json")
    if not os.path.exists(path):
        return {"present": False, "path": path}
    try:
        with open(path, "r", encoding="utf-8") as f:
            hb = json.load(f)
        last = float(hb.get("last_seen_ts", 0))
    except Exception as e:
        return {"present": True, "path": path, "error": f"unreadable heartbeat ({e})"}
    now_ts = time.time() if now_ts is None else now_ts
    return {
        "present": True,
        "path": path,
        "age_s": max(0, int(now_ts - last)) if last > 0 else None,
        "hook": hb.get("hook"),
        "mode": hb.get("mode"),
        "tool": hb.get("tool"),
        "decision": hb.get("decision"),
        "last_seen_utc": hb.get("last_seen_utc"),
        "gate_denial_recorder_errors": hb.get("gate_denial_recorder_errors"),
    }


def check_yolo_scan_health(profile: dict) -> tuple:
    """WARN-only Barbell YOLO scan health (issue #66), from logs/yolo_scan_health.json written by
    screening_pipeline.py / prime_evaluator_brief.py. Returns (level, message) with level "skip" (slot disabled),
    "info" (no scan recorded yet), "ok" or "warn". Never critical."""
    if not bool((profile or {}).get("yolo_slot_enabled", False)):
        return "skip", "YOLO slot disabled in the user profile."
    from utils import yolo_scan_health as ysh
    health = ysh.read_health()
    if not health:
        return "info", "No YOLO scan recorded yet."
    try:
        count = int(health.get("consecutive_unavailable", 0))
    except (TypeError, ValueError):
        count = 0
    if count >= ysh.YOLO_UNAVAILABLE_WARN_AFTER:
        reason = str(health.get("last_unavailable_reason") or "unspecified")[:200]
        return "warn", (f"YOLO scan UNAVAILABLE in {count} consecutive runs (last reason: {reason}). "
                        "If it persists, open a MEDIUM issue: ./scripts/report_issue.sh --category tool_error "
                        "--severity MEDIUM --title \"YOLO scan UNAVAILABLE\" --repro \"<command> (exit <code>)\" "
                        "--output-file <file with the raw output>.")
    return "ok", f"YOLO scan healthy (last status {health.get('last_status', 'UNKNOWN')}, {count} consecutive UNAVAILABLE)."


def _prod_pending_summary(target_env: str):
    """Text describing the target env's resting entries in logs/pending_entries.json, an "unreadable" text when the
    registry cannot be loaded, or None when it holds none."""
    try:
        entries, err = eft.load_pending_entries()
    except Exception as e:
        entries, err = {}, f"{type(e).__name__}: {e}"
    if err:
        return f"pending entries registry unreadable: {err}"
    count = sum(1 for rec in (entries or {}).values()
                if isinstance(rec, dict) and rec.get("target_env") == target_env)
    return f"{count} pending PROD resting entr{'y' if count == 1 else 'ies'}" if count else None


def guardian_check_failed(target_env: str, exc: Exception) -> tuple:
    """(level, message) when check_guardian_service raised (issue #172). PROD with a resting entry in
    logs/pending_entries.json, or an unreadable registry -> "critical" (fail closed: the guardian may be down while a
    fill needs its planned SL); otherwise "warn"."""
    msg = f"Position guardian liveness unreadable ({type(exc).__name__})."
    if is_prod_environment(target_env):
        pending = _prod_pending_summary(target_env)
        if pending:
            return "critical", (f"{msg[:-1]} with {pending} in logs/pending_entries.json: a filled entry can stay "
                                "without its planned SL/TPs. Run python3 scripts/execute_futures_trade.py "
                                "--protect-pending now and check the guardian loop.")
    return "warn", msg


def check_guardian_service(target_env: str) -> tuple:
    """Position guardian liveness (issues #55, #167) from execute_futures_trade.check_guardian_alive (reads
    logs/guardian_state.json; no subprocess). Returns (level, message), level "ok", "warn" or "critical".
    "critical" only in PROD when the loop is not alive AND logs/pending_entries.json holds a PROD resting entry (or
    cannot be read): a filled entry could stay without its planned SL/TPs. Not alive otherwise -> "warn". Alive but
    running without the single-instance lock (state lock_warning) -> "warn"."""
    alive, why = eft.check_guardian_alive(target_env)
    if alive:
        try:
            with open(os.path.join(eft._workspace_dir(), "logs", "guardian_state.json"), "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = None
        lock_warning = state.get("lock_warning") if isinstance(state, dict) and state.get("env") == target_env else None
        if isinstance(lock_warning, str) and lock_warning.strip():
            return "warn", (f"Position guardian loop alive but running WITHOUT the single-instance lock ({lock_warning}): "
                            "a second loop or a --once run may repeat heal/trailing requests.")
        return "ok", f"Position guardian loop alive: {why}."
    if is_prod_environment(target_env):
        impact = "PROD resting STOP_MARKET/LIMIT entries are rejected without it"
    else:
        impact = "resting STOP_MARKET/LIMIT entries get their TPs (and unarmed SL) on fill only while it runs"
    from utils import dossier_provenance as dp
    if dp._is_wsl():
        hint = f"Install it as a background service: python3 scripts/install_guardian_service.py --install --env {target_env}"
    else:
        hint = f"Start the loop: python3 scripts/loops/position_guardian_loop.py --interval 60 --env {target_env}"
    if is_prod_environment(target_env):
        pending = _prod_pending_summary(target_env)
        if pending:
            return "critical", (f"Position guardian loop not alive ({why}) with {pending} in logs/pending_entries.json: "
                                "a filled entry can stay without its planned SL/TPs (STOP_MARKET entries, and "
                                "MCP: no pre-armed SL). Run "
                                "python3 scripts/execute_futures_trade.py --protect-pending now and start the loop. "
                                f"{hint}")
    return "warn", (f"Position guardian loop not alive ({why}). {impact}; MARKET entries do not need the guardian. "
                    f"{hint}")


def check_user_profile_source(profile, target_env: str) -> tuple:
    """Issue #180: where the loaded user profile came from (`_profile_source` stamped by
    user_profile.load_user_profile). Returns (level, message), level "ok" or "critical". "critical" only in PROD for
    a present source other than "user" (config/user_profile.json missing or unreadable: the executor rejects every
    PROD opening). TESTNET and a profile without the marker: "ok"."""
    source = eft.fallback_profile_source(profile)
    if source is not None and is_prod_environment(target_env):
        return "critical", (f"config/user_profile.json is missing or unreadable: the {source} profile was loaded "
                            "instead and PROD entries are rejected. Run 'python3 scripts/user_profile.py --setup'.")
    return "ok", f"User profile source: {source or 'user'}."


DEPENDENCY_MODULES = ("numpy", "pydantic", "statsmodels")


def check_dependencies(modules=DEPENDENCY_MODULES) -> tuple:
    """WARN-only check that the scanners' third-party modules are installed (issue #135: the radars import
    microstructure_engine, which needs numpy at import time). Returns ("ok" | "warn", message). Never critical."""
    import importlib.util
    missing = []
    for name in modules:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    if missing:
        return "warn", (f"Missing Python modules: {', '.join(missing)}. Scanners that import them will fail; "
                        "install requirements.txt.")
    return "ok", f"Python dependencies present ({', '.join(modules)})."


def check_pretool_hook(base_dir: str = None, run_selftest: bool = True, timeout_cap_s: int = 30) -> dict:
    """
    Real activation check of the PreToolUse safety guard for both runtimes (replaces the old string match):
      0. Claude Code: check_claude_hook_matcher (.claude/settings.json / settings.local.json route Bash, PowerShell,
         NotebookEdit, Write, Edit, MultiEdit and mcp__* to pre_trade_guard.py; skipped without either file).
         Runs before, and independently of, the agy checks below.
      1. agy: parses .agents/hooks.json and collects enabled PreToolUse handlers whose matcher covers call_mcp_tool.
      2. Resolves every hook command's script path relative to .agents/ (agy runs hooks with cwd = hooks.json dir)
         and verifies it exists; verifies the interpreter is on PATH.
      3. POSIX: executes each such PreToolUse command via `sh -c` (cwd .agents) with a synthetic Binance
         futures_usds.newOrder payload and requires exit 0 + decision "deny". Windows: skipped with a warning.
      4. Reports logs/hook_heartbeat.json age if present (read BEFORE the self-test).
    Returns {"ok": bool, "critical": [...], "warnings": [...], "info": [...], "heartbeat": {...}}.
    """
    base_dir = base_dir or find_workspace_root()
    hooks_dir = os.path.join(base_dir, ".agents")
    hooks_path = os.path.join(hooks_dir, "hooks.json")
    report = {"ok": False, "critical": [], "warnings": [], "info": [], "heartbeat": read_hook_heartbeat(base_dir)}

    hb = report["heartbeat"]
    if not hb.get("present"):
        report["warnings"].append("No logs/hook_heartbeat.json yet: the guard has not fired in a live agent session.")
    elif hb.get("error"):
        report["warnings"].append(f"Hook heartbeat {hb['error']}.")
    elif hb.get("age_s") is None or hb["age_s"] > HOOK_HEARTBEAT_MAX_AGE_S:
        report["warnings"].append(f"Hook heartbeat is stale (age: {hb.get('age_s')}s, last: {hb.get('last_seen_utc')}).")
    else:
        report["info"].append(
            f"Hook heartbeat {hb['age_s']}s ago (hook={hb.get('hook')}, mode={hb.get('mode')}, tool={hb.get('tool')}, decision={hb.get('decision')})."
        )
    recorder_errors = hb.get("gate_denial_recorder_errors")
    if hb.get("present") and isinstance(recorder_errors, int) and not isinstance(recorder_errors, bool) \
            and recorder_errors > 0:
        # issue #275: informational only (lost logs/gate_denials.jsonl events of the shadow desk, never a gate)
        report["info"].append(f"Gate-denial recorder errors: {recorder_errors} delta-gate denial event(s) not "
                              "recorded in logs/gate_denials.jsonl (hook heartbeat counter; shadow desk only).")

    # Claude Code runtime, independent of the agy checks below (and of their early returns)
    claude_critical, claude_info = check_claude_hook_matcher(base_dir)
    report["critical"].extend(claude_critical)
    report["info"].extend(claude_info)

    if not os.path.exists(hooks_path):
        report["critical"].append(".agents/hooks.json not found: no PreToolUse safety guard is configured.")
        return report
    try:
        with open(hooks_path, "r", encoding="utf-8") as f:
            hooks_cfg = json.load(f)
        if not isinstance(hooks_cfg, dict):
            raise ValueError("top-level value is not an object")
    except Exception as e:
        report["critical"].append(f".agents/hooks.json is not valid JSON ({e}).")
        return report

    guard_handlers = []
    for hook_name, spec in hooks_cfg.items():
        if not isinstance(spec, dict) or spec.get("enabled", True) is False:
            continue
        for event, groups in spec.items():
            if event not in ("PreToolUse", "PostToolUse", "PreInvocation", "PostInvocation", "Stop") or not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                handlers = group.get("hooks") if isinstance(group.get("hooks"), list) else [group]
                for handler in handlers:
                    if not isinstance(handler, dict) or not handler.get("command"):
                        continue
                    cmd = str(handler["command"])
                    is_guard = event == "PreToolUse" and _matcher_matches(group.get("matcher"), "call_mcp_tool")
                    bucket = report["critical"] if is_guard else report["warnings"]
                    try:
                        interp = shlex.split(cmd)[0]
                    except (ValueError, IndexError):
                        interp = cmd.split()[0] if cmd.split() else ""
                    if os.path.isabs(interp):
                        report["warnings"].append(f"[{hook_name}/{event}] uses a machine-specific interpreter path '{interp}'.")
                        if not os.path.exists(interp):
                            bucket.append(f"[{hook_name}/{event}] interpreter not found: {interp}")
                    elif os.name != "nt" and interp and not shutil.which(interp) and not interp.startswith("."):
                        bucket.append(f"[{hook_name}/{event}] interpreter '{interp}' not found on PATH.")
                    scripts = _hook_script_paths(cmd, hooks_dir)
                    for sp in scripts:
                        if not os.path.exists(sp):
                            bucket.append(f"[{hook_name}/{event}] script not found (resolved from .agents/): {sp}")
                    if is_guard:
                        try:
                            timeout_s = int(handler.get("timeout", 30))
                        except (TypeError, ValueError):
                            timeout_s = 30
                        guard_handlers.append((hook_name, cmd, max(1, min(timeout_s, timeout_cap_s))))

    if not guard_handlers:
        report["critical"].append("No enabled PreToolUse hook in .agents/hooks.json matches 'call_mcp_tool'.")
        return report

    if not run_selftest:
        report["ok"] = not report["critical"]
        return report

    if os.name == "nt":
        report["warnings"].append("PreToolUse self-test skipped on Windows (agy runs hooks via `cmd /c`); run the doctor under WSL/Linux/macOS to execute it.")
        report["ok"] = not report["critical"]
        return report

    env = dict(os.environ)
    env[HOOK_SELFTEST_ENV] = "1"
    payload = dict(SYNTHETIC_NEW_ORDER_PAYLOAD)
    payload["workspacePaths"] = [base_dir]
    denied_by_any = False
    for hook_name, cmd, timeout_s in guard_handlers:
        try:
            # The self-test must not refresh the live heartbeat: redirect it to a throwaway file.
            with tempfile.TemporaryDirectory() as hb_dir:
                env[HOOK_HEARTBEAT_OVERRIDE_ENV] = os.path.join(hb_dir, "hook_heartbeat.json")
                proc = subprocess.run(
                    ["sh", "-c", cmd], cwd=hooks_dir, input=json.dumps(payload),
                    capture_output=True, text=True, timeout=timeout_s, env=env,
                )
        except subprocess.TimeoutExpired:
            report["critical"].append(f"[{hook_name}] PreToolUse self-test timed out after {timeout_s}s.")
            continue
        except Exception as e:
            report["critical"].append(f"[{hook_name}] PreToolUse self-test could not run ({e}).")
            continue
        decision_obj = _last_json_object(proc.stdout)
        decision = (decision_obj or {}).get("decision")
        if proc.returncode != 0:
            report["critical"].append(
                f"[{hook_name}] PreToolUse guard exited {proc.returncode} (expected 0; agy needs exit 0 + JSON decision). "
                f"stderr: {(proc.stderr or '').strip()[-300:]}"
            )
        elif decision != "deny":
            report["critical"].append(
                f"[{hook_name}] PreToolUse guard did not deny a direct futures_usds.newOrder (decision={decision!r})."
            )
        else:
            denied_by_any = True
            report["info"].append(f"[{hook_name}] PreToolUse self-test: direct futures_usds.newOrder denied (exit 0).")

    report["ok"] = denied_by_any and not report["critical"]
    return report

def _read_session_state():
    try:
        with open(sss.STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else None
    except (OSError, ValueError):
        return None


def ensure_fresh_ledger(target_env: str, max_age_s: int = LEDGER_MAX_AGE_FOR_TEMPORAL_AUDIT_S) -> tuple:
    """Issue #92: the temporal audit runs on a ledger synced within max_age_s. The ledger
    (sync_session_state.STATE_FILE) is synced in-process when it is missing, or stale/invalid for the SAME env.
    A ledger of the other environment (fresh or stale) is never overwritten (another session may own it): no sync,
    the watchdog still runs (it resolves holding time from the live exchange, not the ledger) and a warning is
    returned. Returns (ok, detail, warning | None); ok False means the temporal audit must be skipped (never 0.0h)."""
    state = _read_session_state()
    if state is not None and os.path.exists(sss.STATE_FILE):
        age = time.time() - os.path.getmtime(sss.STATE_FILE)
        ledger_env = pt.norm_env(state.get("target_env"))
        fresh_valid = state.get("is_valid", True) is not False and age <= max_age_s
        if ledger_env and ledger_env != pt.norm_env(target_env):
            return True, "other-env ledger kept", (
                f"session_state.json belongs to {str(ledger_env).upper()} ({int(age)}s old); not overwritten by the "
                f"{str(target_env).upper()} doctor (temporal audit runs on live positions without a ledger sync).")
        if fresh_valid:
            return True, f"ledger fresh ({int(age)}s)", None
    try:
        synced = sss.sync_session_state(target_env)
    except Exception as e:
        return False, f"ledger sync raised {type(e).__name__}: {e}", None
    if not isinstance(synced, dict) or not synced.get("is_valid", False) or synced.get("state_write_error"):
        # Issue #160: a sync whose atomic write failed (state_write_error) left no fresh ledger on disk.
        err = (synced.get("state_write_error") or synced.get("error")) if isinstance(synced, dict) else synced
        return False, f"ledger sync returned an invalid state ({err})", None
    return True, "ledger synced in-process", None


def ledger_audit_warning(state, target_env: str):
    """Issue #173: WARN text when the same-env ledger reports logs/trades_audit.jsonl unreadable (audit_read_error) or
    with corrupt lines (audit_corrupt_lines > 0), else None. Suggests report_issue.sh; never files an issue itself."""
    if not isinstance(state, dict) or pt.norm_env(state.get("target_env")) != pt.norm_env(target_env):
        return None
    read_error = state.get("audit_read_error")
    try:
        corrupt = int(state.get("audit_corrupt_lines") or 0)
    except (TypeError, ValueError):
        corrupt = 0
    if read_error:
        problem, severity = f"is unreadable ({str(read_error)[:200]})", "HIGH"
    elif corrupt > 0:
        problem, severity = f"has {corrupt} corrupt line(s) (skipped)", "MEDIUM"
    else:
        return None
    return (f"Ledger sync: logs/trades_audit.jsonl {problem}; planned stops and holding times may be missing. "
            f"If it persists, open a {severity} issue: ./scripts/report_issue.sh --category risk_gate --severity "
            f"{severity} --title \"trades audit ledger unreadable or corrupt\" --output-file <file with the raw output> "
            "(the command never names the ledger file, a guard-protected path: put file names and raw output in the "
            "--output-file / --context-file).")


def ledger_resting_mismatch_warning(state, target_env: str):
    """Issue #160: WARN text when the same-env ledger lists pending-registry records with no live order match and no
    open position (portfolio_exposure.resting_mismatches), else None. Read-only: the cleanup is --protect-pending or
    the guardian (a record is dropped once its entry has been gone for 60 s)."""
    if not isinstance(state, dict) or pt.norm_env(state.get("target_env")) != pt.norm_env(target_env):
        return None
    mismatches = (state.get("portfolio_exposure") or {}).get("resting_mismatches") or []
    if not isinstance(mismatches, list) or not mismatches:
        return None
    names = ", ".join(f"{m.get('symbol')}#{m.get('entry_id')}" for m in mismatches if isinstance(m, dict))
    return (f"Pending registry record(s) with no live entry order and no open position: {names}. They are not "
            "counted in delta_bias_incl_resting; clear stale records with python3 scripts/execute_futures_trade.py "
            "--protect-pending or the position guardian.")


def ledger_listing_warning(state, target_env: str):
    """Issue #189: WARN text when the same-env ledger reports a failed order listing read (listing_read_error), else
    None. The state stays valid: resting exposure is UNKNOWN and the SL / TP listings of that sync are empty."""
    if not isinstance(state, dict) or pt.norm_env(state.get("target_env")) != pt.norm_env(target_env):
        return None
    read_error = state.get("listing_read_error")
    if not read_error:
        return None
    return (f"Ledger sync: order listing read failed ({str(read_error)[:300]}); delta_bias_incl_resting is UNKNOWN "
            "and positions may show no verified Stop Loss in the ledger. Re-run python3 scripts/sync_session_state.py.")


def ledger_counted_by_warning(state, target_env: str):
    """Issue #207 (#212 request): WARN text when the same-env ledger's closed_today_summary.counted_by is present and
    not "trades" (per-fill counts or unreadable fills: the day's closed-trade counts are not per trade), else None."""
    if not isinstance(state, dict) or pt.norm_env(state.get("target_env")) != pt.norm_env(target_env):
        return None
    closed = state.get("closed_today_summary") or {}
    counted_by = closed.get("counted_by") if isinstance(closed, dict) else None
    if counted_by is None or counted_by == "trades":
        return None
    detail = closed.get("trade_summary_error") or closed.get("fills_error") or "no detail"
    return (f"Ledger sync: today's closed trades are counted_by={counted_by} (not per trade: {str(detail)[:160]}); "
            "win/loss counts may be wrong and the Daily Loss Gate refuses PROD openings until they are countable.")


MCP_GATE_CAUSE = "MCP mode: the gateway has no userTrades, so the gate refuses PROD openings"


def daily_loss_gate_line(state, target_env: str, mcp: bool = False):
    """Issue #207: ("ok" | "warn", text) for the Daily Loss Gate state cached by the ledger sync (the executor
    re-reads the exchange). A missing state or another env's ledger is a WARN (sync required); TESTNET is "info"
    (the executor skips the gate there). mcp: the doctor runs in MCP auth mode; an "unavailable:" gate then names
    MCP_GATE_CAUSE (issue #187)."""
    if pt.norm_env(target_env) == "testnet":
        return "info", "Daily Loss Gate skipped (TESTNET)."
    if not isinstance(state, dict) or pt.norm_env(state.get("target_env")) != pt.norm_env(target_env):
        return "warn", "Daily Loss Gate state unknown (no same-env ledger): run sync_session_state.py."
    gate = state.get("daily_loss_gate")
    text = sss.format_daily_loss_gate(gate)
    if not isinstance(gate, dict) or gate.get("blocked") is not False:
        if mcp and isinstance(gate, dict) and str(gate.get("reason") or "").startswith("unavailable:"):
            text += f" ({MCP_GATE_CAUSE})"
        return "warn", f"Daily Loss Gate: {text}"
    unaudited = gate.get("unaudited_closing_symbols") or []
    if unaudited:  # round 4: informational (never blocks), but the streak cannot see those trades
        return "warn", (f"Daily Loss Gate: {text}; closing fills today without an audit record on "
                        f"{', '.join(str(s) for s in unaudited)}: counted in the USDT figure, NOT in the "
                        "consecutive-SL streak.")
    return "ok", f"Daily Loss Gate: {text}"


def calibration_store_warning(base_dir: str, target_env: str, now: float = None):
    """Issue #207 (#204 note): WARN text when logs/score_calibration.json is older than score_calibration.MAX_AGE_S,
    or missing while PROD closed trades exist (logs/trade_outcomes.jsonl closed PROD rows, or closed trades in the
    PROD ledger), else None. PROD only; never critical."""
    if pt.norm_env(target_env) != "prod":
        return None
    try:
        from utils import score_calibration as scal
    except Exception as e:
        return f"Score calibration module unavailable ({type(e).__name__}): autonomous Tier S will always ask."
    hint = "autonomous Tier S will always ask; rerun python3 scripts/trading_scorecard.py"
    path = scal.store_path(base_dir)
    now = time.time() if now is None else now
    if os.path.exists(path):
        cal = scal.load_calibration(base_dir) or {}
        try:
            age = now - float(cal.get("generated_at_ts"))
        except (TypeError, ValueError):
            return f"logs/score_calibration.json unreadable or malformed: {hint}."
        if age > scal.MAX_AGE_S:
            return (f"logs/score_calibration.json is stale ({int(age // 86400)} d old, max "
                    f"{scal.MAX_AGE_S // 86400} d): {hint}.")
        return None
    closed = False
    try:
        with open(os.path.join(base_dir, "logs", "trade_outcomes.jsonl"), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and pt.norm_env(row.get("env")) == "prod" and row.get("status") == "closed":
                    closed = True
                    break
    except OSError:
        pass
    state = _read_session_state() or {}
    if pt.norm_env(state.get("target_env")) == "prod":
        try:
            closed = closed or int((state.get("closed_today_summary") or {}).get("closed_trades_count") or 0) > 0
        except (TypeError, ValueError):
            pass
    return f"logs/score_calibration.json is missing while PROD closed trades exist: {hint}." if closed else None


FEE_RATE_SYMBOL = "BTCUSDT"  # the commission rate is read for one symbol (the account's tier applies to all)


def _fee_reply_text(res) -> str:
    return str(res)[:120]


def _commission_pct(res, key):
    """res[key] (a commission rate fraction, e.g. "0.000500") in percent, None when missing or not a number."""
    raw = res.get(key) if isinstance(res, dict) else None
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw) * 100
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def fee_status_line(target_env: str, mcp: bool = False) -> str:
    """Issue #268: informational [FEES] text, read-only. KEYS mode: the commission rate on FEE_RATE_SYMBOL (signed
    GET /fapi/v1/commissionRate: makerCommissionRate / takerCommissionRate) and the futures BNB fee discount (signed
    GET /fapi/v1/feeBurn: feeBurn true = ON). MCP mode: unavailable (no gateway mapping). A failed or non-dict reply
    or a missing field reads as unavailable. Never raises and never affects the exit code; enabling the BNB discount
    is the owner's decision."""
    if mcp:
        return "Fee tier and BNB fee discount unavailable in MCP mode."
    parts = []
    try:
        rate = eft.send_signed_request("GET", "/fapi/v1/commissionRate", {"symbol": FEE_RATE_SYMBOL},
                                       target_env=target_env)
    except Exception as e:
        rate = f"{type(e).__name__}"
    maker, taker = _commission_pct(rate, "makerCommissionRate"), _commission_pct(rate, "takerCommissionRate")
    if maker is None and taker is None:
        parts.append(f"commission rate unavailable ({_fee_reply_text(rate)})")
    else:
        maker_txt, taker_txt = (("n/a" if v is None else f"{v:.4f}%") for v in (maker, taker))
        parts.append(f"{FEE_RATE_SYMBOL} commission maker {maker_txt} / taker {taker_txt}")
    try:
        burn = eft.send_signed_request("GET", "/fapi/v1/feeBurn", target_env=target_env)
    except Exception as e:
        burn = f"{type(e).__name__}"
    flag = burn.get("feeBurn") if isinstance(burn, dict) else None
    if isinstance(flag, bool):
        parts.append(f"BNB fee discount {'ON' if flag else 'OFF'}")
    else:
        parts.append(f"BNB fee discount status unavailable ({_fee_reply_text(burn)})")
    return "; ".join(parts) + " (informational; the BNB discount is the owner's choice)."


def pr_hook_detect_error_line(logs_dir: str, now: float = None) -> str:
    """Issue #257: informational [PR-HOOK] text, the number of `detect_error` events (commands the PR review hook
    could not scan, so it did not arm the review) in pr_hook_events.jsonl over the last 24 h. Unreadable lines skip."""
    cutoff, count = (now or time.time()) - 24 * 3600, 0
    try:
        with open(os.path.join(logs_dir, "pr_hook_events.jsonl"), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                    count += event.get("event") == "detect_error" and float(event.get("timestamp")) >= cutoff
                except (ValueError, TypeError, AttributeError):
                    continue
    except FileNotFoundError:
        pass
    return f"{count} PR review hook detect_error event(s) in the last 24h (those commands did not arm the review)."


# Issue #312: shadow-audit staleness (WARN only, never critical)
SHADOW_WINDOW_GRACE_S = 5400 + 14400 + 3600  # 90 min trigger + 4 h hold + 1 h grace since registration
SHADOW_RESOLVED_STALE_S = 12 * 3600          # shadow_resolved.jsonl age while rows are past their window
SHADOW_AUDIT_STALE_S = 6 * 3600              # last audit (shadow_state.json) age while rows are past their window


def _age_text(seconds) -> str:
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}h {seconds % 3600 // 60}m ago" if seconds >= 3600 else f"{seconds // 60}m ago"


def shadow_desk_status(now: float = None) -> tuple:
    """(line, warnings) of the [SHADOW DESK] check (issue #312): counts, FER, USDT and R totals, the last audit age and
    the 3 rows with the largest target_dollar_risk. One WARN when a pending / active row is older than
    SHADOW_WINDOW_GRACE_S since registration, also naming a shadow_resolved.jsonl older than SHADOW_RESOLVED_STALE_S
    and a missing or older than SHADOW_AUDIT_STALE_S heartbeat. No rows: no WARN."""
    import shadow_tracker as st
    now = time.time() if now is None else now
    m = st.calculate_efficacy_metrics()
    last = st._to_float(st.read_audit_state().get("last_audit_ts"))
    audit_age = now - last if last is not None else None
    line = (f"{m.get('active_shadow_trades', 0)} unexecuted candidate(s) under counterfactual monitoring | Resolved: "
            f"{m.get('total_resolved', 0)} (FER: {m.get('filter_efficacy_ratio_pct', 0.0)}% | Saved: "
            f"+${m.get('capital_saved_usdt', 0.0)} USDT, mixed row sizes, see R | Net "
            f"{m.get('net_filter_edge_r', 0.0):+}R, {m.get('r_rows_skipped', 0)} row(s) without risk skipped) | "
            f"Last audit: {_age_text(audit_age)}")
    largest = m.get("largest_risk_rows") or []
    if largest:
        line += " | Largest row risk: " + ", ".join(f"{r.get('symbol')} ${r.get('target_dollar_risk')}" for r in largest)
    ages = [now - ts for ts in (st._to_float(t.get("registered_at_ts")) for t in m.get("active_trades") or []
                                if isinstance(t, dict)) if ts is not None]
    overdue = [a for a in ages if a > SHADOW_WINDOW_GRACE_S]
    if not overdue:
        return line, []
    reasons = [f"{len(overdue)} shadow row(s) past their window without resolution (oldest {max(overdue) / 3600:.1f}h "
               f"> {SHADOW_WINDOW_GRACE_S / 3600:.1f}h since registration)"]
    try:
        resolved_age = now - os.path.getmtime(st.SHADOW_RESOLVED_FILE)
    except OSError:
        resolved_age = None
    if resolved_age is None or resolved_age > SHADOW_RESOLVED_STALE_S:
        reasons.append("shadow_resolved.jsonl " + ("missing" if resolved_age is None
                                                   else f"last written {resolved_age / 3600:.1f}h ago"))
    if audit_age is None or audit_age > SHADOW_AUDIT_STALE_S:
        reasons.append("no shadow audit heartbeat (shadow_state.json)" if audit_age is None
                       else f"last shadow audit {audit_age / 3600:.1f}h ago")
    return line, ["Shadow desk audit stale: " + "; ".join(reasons) + ". The guardian loop audits a bounded batch each "
                  "cycle; run python3 scripts/shadow_tracker.py --audit for the whole backlog."]


PREARM_STATS_WINDOW_S = 24 * 3600


def _finite(value):
    """float(value) when it is a finite number (not a bool), else None."""
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def prearm_fill_stop_lines(base_dir: str, target_env: str, now: float = None) -> list:
    """Issue #273: informational [PREARM] / [FILL-STOP] lines from logs/guardian_actions.jsonl, read-only, for
    target_env and the last PREARM_STATS_WINDOW_S. [PREARM]: "prearm_rejected" events with code -4509 (written by
    the executor) and how many of them were LIMIT entries. [FILL-STOP]: max / median fill_to_stop_s of successful,
    non-dry-run pending_protect_sl actions (only when n > 0). Unreadable files and malformed lines are ignored. Returns
    [(tag, message)]; never raises on file contents and never affects the exit code."""
    now = time.time() if now is None else now
    env = pt.norm_env(target_env)
    envs = {}
    rejected = limit = 0
    latencies, from_exchange = [], 0
    try:
        f = open(os.path.join(base_dir, "logs", "guardian_actions.jsonl"), "r", encoding="utf-8", errors="replace")
    except OSError:
        f = None
    if f is not None:
        with f:
            for line in f:
                if "prearm_rejected" not in line and "pending_protect_sl" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                raw_env = str(rec.get("env") or "")
                if raw_env not in envs:
                    envs[raw_env] = pt.norm_env(raw_env)
                if envs[raw_env] != env:
                    continue
                if rec.get("event") == "prearm_rejected":
                    ts = _finite(rec.get("ts"))
                    if ts is None or now - ts > PREARM_STATS_WINDOW_S or _finite(rec.get("code")) != -4509:
                        continue
                    rejected += 1
                    limit += str(rec.get("entry_type") or "").upper() == "LIMIT"
                elif rec.get("type") == "pending_protect_sl" and rec.get("success") is True and not rec.get("dry_run"):
                    ts = _finite(rec.get("timestamp"))
                    detail = rec.get("detail") if isinstance(rec.get("detail"), dict) else {}
                    latency = _finite(detail.get("fill_to_stop_s"))
                    if ts is None or now - ts > PREARM_STATS_WINDOW_S or latency is None or latency < 0:
                        continue
                    latencies.append(latency)
                    from_exchange += detail.get("fill_ts_source") == "position_update"
    lines = [("PREARM", f"{rejected} pre-arm(s) rejected with -4509 in the last 24 h ({limit} on LIMIT entries); "
                        "the guardian places those stops at fill (informational).")]
    if latencies:
        lines.append(("FILL-STOP", f"Fill-to-stop in the last 24 h: max {max(latencies):.1f} s, median "
                                   f"{statistics.median(latencies):.1f} s (n={len(latencies)}, {from_exchange} from the "
                                   "exchange fill time); accepted window about 60-120 s, bounded by the guardian "
                                   "interval (informational)."))
    return lines


def run_doctor(target_env: str = None, auto_heal: bool = False) -> int:
    target_env = resolve_env(target_env)
    start_time = time.time()
    print("=" * 65)
    print("🩺 TRADING DOCTOR — PRE-FLIGHT SYSTEM DIAGNOSTIC")
    print(f"Target Environment: {target_env.upper()}")
    print("=" * 65)

    critical_failures = []
    warnings = []
    ok_items = []

    # 1. API Configuration & Credentials
    cfg = eft.load_env(target_env=target_env)
    api_key, secret_key, base_url = eft.get_client_config(target_env=target_env)
    if not api_key or not secret_key:
        critical_failures.append(f"API credentials not found or invalid in environment config for {target_env.upper()}")
        print(f"❌ [API KEYS] Missing credentials or placeholder in environment config for {target_env.upper()}")
        return 1
    else:
        masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "***"
        ok_items.append(f"Credentials detected for {target_env.upper()} ({masked_key})")
        print(f"✅ [API KEYS] Credentials OK ({target_env.upper()})")

    # Safety Flag verification for PROD
    if is_prod_environment(target_env):
        is_armed = str(cfg.get("LIVE_TRADING_ARMED", "")).strip().lower() == "true"
        if not is_armed:
            critical_failures.append("LIVE_TRADING_ARMED=true is required for PROD/MAINNET live trading.")
            print("❌ [SAFETY FLAG] LIVE_TRADING_ARMED is not 'true'. Live execution disarmed.")
        else:
            ok_items.append("LIVE_TRADING_ARMED=true verified for PROD.")
            print("✅ [SAFETY FLAG] LIVE_TRADING_ARMED=true verified (PROD ARMED)")
    else:
        print("ℹ️  [SAFETY FLAG] Sandbox mode (Testnet). LIVE_TRADING_ARMED flag not required.")

    # 2. Network Latency & Clock Drift
    try:
        t0 = time.time()
        req = urllib.request.Request(f"{base_url}/fapi/v1/time", headers={"User-Agent": "TradingDoctor/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            t1 = time.time()
            data = json.loads(resp.read().decode())
            server_time = data.get("serverTime", 0)
            latency_ms = int((t1 - t0) * 1000)
            mid_local_ms = int(((t0 + t1) / 2.0) * 1000)
            drift_ms = abs(server_time - mid_local_ms)

            if latency_ms > 1200:
                warnings.append(f"Elevated network latency: {latency_ms}ms")
                print(f"⚠️  [NETWORK] High latency: {latency_ms}ms")
            else:
                ok_items.append(f"API latency: {latency_ms}ms")
                print(f"✅ [NETWORK] API latency: {latency_ms}ms")

            max_drift_ms = 2500 if (target_env == "testnet" or api_key == "MCP_OAUTH_ACTIVE") else 1000
            if drift_ms > max_drift_ms:
                critical_failures.append(f"Excessive clock drift: {drift_ms}ms (limit: {max_drift_ms}ms)")
                print(f"❌ [CLOCK DRIFT] Dangerous clock drift: {drift_ms}ms (limit: {max_drift_ms}ms)")
            elif drift_ms > 400:
                warnings.append(f"Moderate clock drift: {drift_ms}ms (compensated dynamically by execution harness)")
                print(f"⚠️  [CLOCK DRIFT] Moderate clock drift: {drift_ms}ms (compensated dynamically)")
            else:
                ok_items.append(f"Optimal clock drift: {drift_ms}ms")
                print(f"✅ [CLOCK DRIFT] Clock synchronization OK ({drift_ms}ms)")
    except Exception as e:
        critical_failures.append(f"Failed to connect to time endpoint: {str(e)}")
        print(f"❌ [NETWORK] Unable to connect to {base_url}: {e}")
        return 1

    # 3. Balance & Free Margin Query
    try:
        balance_res = eft.send_signed_request("GET", "/fapi/v2/balance", target_env=target_env)
        if isinstance(balance_res, list):
            usdt_bal = next((b for b in balance_res if b.get("asset") == "USDT"), None)
            if usdt_bal:
                total_bal = float(usdt_bal.get("balance", 0.0))
                free_bal = float(usdt_bal.get("availableBalance", 0.0))
                if free_bal < 10.0:
                    warnings.append(f"Low available USDT balance: ${free_bal:.2f} USDT")
                    print(f"⚠️  [BALANCE] Low available USDT balance: ${free_bal:.2f} (Total: ${total_bal:.2f})")
                else:
                    ok_items.append(f"USDT Balance: ${free_bal:.2f} available of ${total_bal:.2f}")
                    print(f"✅ [BALANCE] Available balance: ${free_bal:.2f} USDT (Total: ${total_bal:.2f})")
            else:
                warnings.append("USDT asset not found in futures balance")
                print("⚠️  [BALANCE] USDT asset not found")
        else:
            critical_failures.append(f"Unexpected response fetching balance: {balance_res}")
            print(f"❌ [BALANCE] Error fetching balance: {balance_res}")
    except Exception as e:
        critical_failures.append(f"Failed to fetch balance: {str(e)}")
        print(f"❌ [BALANCE] Authentication or connection error: {e}")

    # 3b. User Profile Calibration Check (FAIL CLOSED)
    try:
        import user_profile as up
        profile = up.load_user_profile()
        risk_pct = profile.get("risk_pct_equity", 0.005) * 100
        is_completed = profile.get("profile_completed", False)
        if not is_completed:
            critical_failures.append("User profile has not completed onboarding. Run 'python3 scripts/user_profile.py --setup' before trading.")
            print("🚨 [USER PROFILE] User profile has not completed onboarding. Run 'python3 scripts/user_profile.py --setup' before trading.")
        else:
            ok_items.append(f"User Profile calibrated (Risk: {risk_pct:.2f}% equity, Mode: {profile.get('operating_mode')})")
            print(f"✅ [USER PROFILE] Calibrated: {risk_pct:.2f}% risk per trade ({profile.get('operating_mode')})")
        source_level, source_msg = check_user_profile_source(profile, target_env)
        if source_level == "critical":
            critical_failures.append(f"User profile: {source_msg}")
            print(f"🚨 [USER PROFILE] {source_msg}")
    except Exception as e:
        critical_failures.append(f"User profile error: {e}")
        print(f"🚨 [USER PROFILE] Could not load profile: {e}")

    # 3c. PreToolUse Safety Hook Activation Check (FAIL CLOSED): parse, resolve and EXECUTE the guard
    base_dir = find_workspace_root()
    try:
        hook_report = check_pretool_hook(base_dir)
    except Exception as e:
        hook_report = {"ok": False, "critical": [f"PreToolUse hook check crashed ({e})."], "warnings": [], "info": []}
    for msg in hook_report.get("info", []):
        print(f"ℹ️  [SAFETY HOOKS] {msg}")
    for msg in hook_report.get("warnings", []):
        warnings.append(f"Safety hooks: {msg}")
        print(f"⚠️  [SAFETY HOOKS] {msg}")
    if hook_report.get("ok"):
        ok_items.append("PreToolUse safety guard verified (agy .agents/hooks.json; Claude Code .claude/settings*.json "
                        "when present)")
        print("✅ [SAFETY HOOKS] PreToolUse execution guard configured and verified.")
    else:
        for msg in hook_report.get("critical", []) or ["PreToolUse guard could not be verified."]:
            critical_failures.append(f"Safety hooks: {msg} Live order execution is strictly prohibited.")
            print(f"🚨 [SAFETY HOOKS] {msg} Live order execution is strictly prohibited.")

    # 4. Forensic Orphan Position Audit (FAIL CLOSED)
    try:
        pos_res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
        active_positions = [p for p in pos_res if float(p.get("positionAmt", 0)) != 0] if isinstance(pos_res, list) else []

        # Issue #151: unreadable reads are UNKNOWN (critical), never "clean portfolio" or "every position orphan".
        algos_res = eft.send_signed_request("GET", "/fapi/v1/openAlgoOrders", target_env=target_env) \
            if isinstance(pos_res, list) else None
        active_algos = algos_res if isinstance(algos_res, list) else []
        algo_symbols = {a.get("symbol") for a in active_algos if a.get("orderType") in ["STOP_MARKET", "STOP"]}

        orphan_positions = []
        for p in active_positions:
            sym = p.get("symbol")
            if sym not in algo_symbols:
                orphan_positions.append(p)

        if not isinstance(pos_res, list):
            msg = f"Orphan audit: positionRisk unreadable ({pos_res}); open positions UNKNOWN."
            critical_failures.append(msg)
            print(f"❌ [ORPHAN AUDIT] {msg}")
        elif active_positions and not isinstance(algos_res, list):
            msg = (f"Orphan audit: openAlgoOrders unreadable ({algos_res}); stop protection UNKNOWN for "
                   f"{[p.get('symbol') for p in active_positions]}; no heal attempted.")
            critical_failures.append(msg)
            print(f"❌ [ORPHAN AUDIT] {msg}")
        elif orphan_positions:
            err_msg = f"Detected {len(orphan_positions)} ORPHAN position(s) lacking Stop Loss on Binance: {[p['symbol'] for p in orphan_positions]}"
            if auto_heal:
                print(f"🚨 [ORPHAN AUDIT] {err_msg} — TRIGGERING AUTO-HEAL...")
                for op in orphan_positions:
                    sym = op["symbol"]
                    # Verified emergency stop (place + confirm on openAlgoOrders); never opens exposure
                    heal_res = eft.heal_orphan_position(op, target_env=target_env, close_on_failure=False)
                    if heal_res.get("success"):
                        print(f"   🛡️ Auto-Heal successful for {sym}: verified Algo SL at {heal_res.get('healed_sl_price')}")
                        ok_items.append(f"Auto-Heal applied to {sym}")
                    else:
                        critical_failures.append(f"Auto-Heal failure on {sym}: {heal_res.get('reason')}")
                        print(f"   ❌ Failed to apply Auto-Heal on {sym}: {heal_res.get('reason')}")
            else:
                critical_failures.append(err_msg)
                print(f"❌ [ORPHAN AUDIT] FAIL CLOSED: {err_msg}")
                print("   👉 Run `python3 scripts/trading_doctor.py --heal` or place Stop Loss immediately.")
        else:
            if active_positions:
                ok_items.append(f"{len(active_positions)} active position(s), all with verified Stop Loss on Binance")
                print(f"✅ [ORPHAN AUDIT] {len(active_positions)} active position(s) — All protected with Stop Loss.")

                # 4b. Dead Alpha & Temporal Drift Sensor (issue #92: fresh ledger first, never fail-open)
                ledger_ok, ledger_detail, ledger_warning = ensure_fresh_ledger(target_env)
                if ledger_warning:
                    warnings.append(ledger_warning)
                    print(f"⚠️  [STATE LEDGER] {ledger_warning}")
                if not ledger_ok:
                    msg = f"Temporal audit skipped: ledger could not be synced ({ledger_detail})."
                    warnings.append(msg)
                    print(f"⚠️  [DEAD ALPHA] {msg}")
                else:
                    audit_warning = ledger_audit_warning(_read_session_state(), target_env)
                    if audit_warning:
                        warnings.append(audit_warning)
                        print(f"⚠️  [STATE LEDGER] {audit_warning}")
                    try:
                        import trading_drift_watchdog as tdw
                        drift_report = tdw.audit_dead_alpha(target_env=target_env, max_hours=4.0, auto_exit=False)
                        dead_count = drift_report.get("dead_alpha_count", 0)
                        unknown = drift_report.get("unknown_holding_symbols") or []
                        read_error = drift_report.get("read_error")
                        if read_error:
                            msg = (f"Temporal audit failed: {read_error} (watchdog read error: its view may diverge "
                                   "from the exchange; verify with python3 scripts/execute_futures_trade.py "
                                   "--positions --json).")
                            warnings.append(msg)
                            print(f"⚠️  [DEAD ALPHA] {msg}")
                        if dead_count > 0:
                            warnings.append(f"Detected {dead_count} position(s) with Dead Alpha (>4h stagnant).")
                            print(f"⚠️  [DEAD ALPHA] {dead_count} stagnant position(s) exceed intraday holding threshold.")
                        if unknown:
                            warnings.append(f"Holding time UNKNOWN for {unknown}: no Binance fill or trades_audit "
                                            "record; dead alpha cannot be assessed for them. Review manually: "
                                            "python3 scripts/execute_futures_trade.py --positions --json.")
                            print(f"⚠️  [DEAD ALPHA] Holding time UNKNOWN for {unknown}. Review manually: "
                                  "python3 scripts/execute_futures_trade.py --positions --json.")
                        if not dead_count and not unknown and not read_error:
                            ok_items.append("Active position holding health OK (Zero Dead Alpha).")
                    except Exception as e:
                        msg = f"Temporal audit failed: {type(e).__name__}: {e}"
                        warnings.append(msg)
                        print(f"⚠️  [DEAD ALPHA] {msg}")
            else:
                ok_items.append("Zero open positions. Zero unhedged exposure.")
                print("✅ [ORPHAN AUDIT] Clean portfolio. Zero open positions.")
    except Exception as e:
        critical_failures.append(f"Failed orphan order audit: {str(e)}")
        print(f"❌ [ORPHAN AUDIT] Error querying positions and orders: {e}")

    # 5. session_state.json Freshness Audit
    # Same file the temporal audit syncs in-process (sync_session_state.STATE_FILE, issue #92).
    state_file = sss.STATE_FILE
    if os.path.exists(state_file):
        mtime = os.path.getmtime(state_file)
        age_sec = time.time() - mtime
        if (_read_session_state() or {}).get("is_valid", True) is False:
            warnings.append("session_state.json is INVALID (last ledger sync failed). Run `sync_session_state.py`.")
            print("⚠️  [STATE LEDGER] session_state.json is INVALID (last ledger sync failed). Sync required.")
        elif age_sec > 1800:
            warnings.append(f"session_state.json is stale ({int(age_sec/60)} minutes old). Run `sync_session_state.py`.")
            print(f"⚠️  [STATE LEDGER] session_state.json is {int(age_sec/60)} min old. Sync recommended.")
        else:
            ok_items.append(f"session_state.json is fresh ({int(age_sec)}s)")
            print(f"✅ [STATE LEDGER] session_state.json synced {int(age_sec)}s ago")
        mismatch_warning = ledger_resting_mismatch_warning(_read_session_state(), target_env)
        if mismatch_warning:
            warnings.append(mismatch_warning)
            print(f"⚠️  [STATE LEDGER] {mismatch_warning}")
        # Issue #207: per-trade counting quality and the Daily Loss Gate state (WARN only, never critical)
        ledger_state = _read_session_state()  # read once for the checks below
        listing_warning = ledger_listing_warning(ledger_state, target_env)   # issue #189
        if listing_warning:
            warnings.append(listing_warning)
            print(f"⚠️  [STATE LEDGER] {listing_warning}")
        counted_warning = ledger_counted_by_warning(ledger_state, target_env)
        if counted_warning:
            warnings.append(counted_warning)
            print(f"⚠️  [STATE LEDGER] {counted_warning}")
        gate_level, gate_msg = daily_loss_gate_line(ledger_state, target_env, mcp=api_key == "MCP_OAUTH_ACTIVE")
        if gate_level == "warn":
            warnings.append(gate_msg)
            print(f"⚠️  [DAILY LOSS GATE] {gate_msg}")
        elif gate_level == "info":
            print(f"ℹ️  [DAILY LOSS GATE] {gate_msg}")
        else:
            ok_items.append(gate_msg)
            print(f"✅ [DAILY LOSS GATE] {gate_msg}")
    else:
        warnings.append("session_state.json does not exist yet. Run `sync_session_state.py`.")
        print("⚠️  [STATE LEDGER] session_state.json does not exist. Run `sync_session_state.py`.")

    # 5b. Barbell YOLO scan health (WARN only, never critical; issue #66)
    try:
        import user_profile as up
        level, msg = check_yolo_scan_health(up.load_user_profile())
    except Exception as e:
        level, msg = "info", f"YOLO scan health unreadable ({type(e).__name__})."
    if level == "warn":
        warnings.append(msg)
        print(f"⚠️  [YOLO_SCAN] {msg}")
    elif level == "ok":
        ok_items.append(msg)
        print(f"✅ [YOLO_SCAN] {msg}")
    elif level == "info":
        print(f"ℹ️  [YOLO_SCAN] {msg}")

    # 5c. Position guardian loop liveness (issue #55; critical only in PROD with pending resting entries, issue #167)
    try:
        level, msg = check_guardian_service(target_env)
    except Exception as e:
        level, msg = guardian_check_failed(target_env, e)
    if level == "critical":
        critical_failures.append(msg)
        print(f"❌ [GUARDIAN] {msg}")
    elif level == "warn":
        warnings.append(msg)
        print(f"⚠️  [GUARDIAN] {msg}")
    else:
        ok_items.append(msg)
        print(f"✅ [GUARDIAN] {msg}")

    # 5c''. Trading lease holder (issue #280; informational only, never a warning or a failure)
    try:
        from utils import trading_lease as tl
        lease_msg = tl.status_line(os.path.dirname(sss.LOGS_DIR))
    except Exception as e:
        lease_msg = f"Trading lease status unavailable ({type(e).__name__})."
    print(f"ℹ️  [LEASE] {lease_msg}")

    # 5c'''. PR review hook scan failures (issue #257; informational only, never a warning or a failure)
    try:
        pr_hook_msg = pr_hook_detect_error_line(sss.LOGS_DIR)
    except Exception as e:
        pr_hook_msg = f"PR review hook events unreadable ({type(e).__name__})."
    print(f"ℹ️  [PR-HOOK] {pr_hook_msg}")

    # 5c'. Score calibration store freshness (WARN only, never critical; issue #207)
    try:
        calib_msg = calibration_store_warning(os.path.dirname(sss.LOGS_DIR), target_env)
    except Exception as e:
        calib_msg = f"Score calibration store check failed ({type(e).__name__})."
    if calib_msg:
        warnings.append(calib_msg)
        print(f"⚠️  [CALIBRATION] {calib_msg}")

    # 5d. Python dependencies of the scanners (WARN only, never critical; issue #135)
    level, msg = check_dependencies()
    if level == "warn":
        warnings.append(msg)
        print(f"⚠️  [DEPENDENCIES] {msg}")
    else:
        ok_items.append(msg)
        print(f"✅ [DEPENDENCIES] {msg}")

    # 5e. Fee tier and BNB fee discount (issue #268; informational only: never a warning, never critical)
    try:
        fee_msg = fee_status_line(target_env, mcp=api_key == "MCP_OAUTH_ACTIVE")
    except Exception as e:
        fee_msg = f"Fee status check failed ({type(e).__name__})."
    print(f"ℹ️  [FEES] {fee_msg}")

    # 5f. Pre-arm -4509 rejections and fill-to-stop latency (issue #273; informational only)
    try:
        prearm_lines = prearm_fill_stop_lines(os.path.dirname(sss.LOGS_DIR), target_env)
    except Exception as e:
        prearm_lines = [("PREARM", f"Pre-arm / fill-to-stop stats unavailable ({type(e).__name__}).")]
    for tag, msg in prearm_lines:
        print(f"ℹ️  [{tag}] {msg}")

    # 6. Shadow Desk Counterfactual Audit (issue #312: audit staleness WARN; fail-open, never critical)
    try:
        shadow_msg, shadow_warnings = shadow_desk_status()
    except Exception as e:
        shadow_msg, shadow_warnings = f"Shadow desk status unavailable ({type(e).__name__}).", []
    print(f"👻 [SHADOW DESK] {shadow_msg}")
    for w in shadow_warnings:
        warnings.append(w)
        print(f"⚠️  [SHADOW DESK] {w}")

    elapsed = round(time.time() - start_time, 2)
    print("=" * 65)
    print(f"DIAGNOSTIC COMPLETED IN {elapsed}s")

    if critical_failures:
        print(f"🔴 STATUS: SYSTEM DISABLED ({len(critical_failures)} critical failure(s)). FAIL CLOSED.")
        for f in critical_failures:
            print(f"   ✖ {f}")
        print("=" * 65)
        return 1
    elif warnings:
        print(f"🟡 STATUS: OPERATIONAL WITH WARNINGS ({len(warnings)} warning(s)).")
        for w in warnings:
            print(f"   ▲ {w}")
        print("=" * 65)
        return 0
    else:
        print("🟢 STATUS: 100% GREEN AND HEALTHY. READY TO TRADE.")
        print("=" * 65)
        return 0

if __name__ == "__main__":
    import argparse
    default_env = resolve_env()
    parser = argparse.ArgumentParser(description="Trading Doctor - Pre-flight Health Check")
    parser.add_argument("--env", default=default_env, help="Target execution environment (prod/testnet)")
    parser.add_argument("--heal", action="store_true", help="Auto-heal orphan positions by placing emergency SL")
    args = parser.parse_args()

    sys.exit(run_doctor(target_env=args.env, auto_heal=args.heal))
