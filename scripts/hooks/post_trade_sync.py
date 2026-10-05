#!/usr/bin/env python3
"""
post_trade_sync.py - PostToolUse Hook for deterministic portfolio synchronization.
Reactive local state update following each exchange execution.

If an executed command mutated trading positions (open, close, or break-even ratchet),
this hook runs `sync_session_state.py` to guarantee that
Ground Truth (session_state.json) remains permanently fresh.
If an opening order was placed, it immediately runs an orphan audit with auto-heal.

Recognized tool calls (Antigravity and Claude Code payloads):
  * run_command / Bash / PowerShell executing the sanctioned trading scripts (inspection commands such as
    grep/cat/Get-Content that merely mention them are ignored):
      - scripts/execute_futures_trade.py: trade openings, --close-position, --move-breakeven,
        --audit-orphans, --auto-heal, --protect-pending (read-only --positions and --help are ignored);
      - scripts/loops/position_guardian_loop.py (except --dry-run), night_cutoff_loop.py,
        dynamic_exit_manager.py and batch deploy scripts.
  * call_mcp_tool, mcp_tool, mcp_<server>_<tool>, mcp__<server>__<tool>:
      - Binance write tools (anything outside the read-only allowlist), including calls wrapped
        in the gateway meta-tool 'tool_execute', placeMultipleOrders and newAlgoOrder.
    The retired crypto_radar MCP server is denied by pre_trade_guard and never triggers a sync.

Contract:
  Input (stdin): JSON with step metadata.
  Output (stdout): {} — always, with exit code 0. Total runtime is bounded well below the
  30s hook timeout (sync <= SYNC_TIMEOUT_S, orphan audit <= AUDIT_TIMEOUT_S).
"""

import os
import re
import sys
import json
import threading
import subprocess

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

SYNC_TIMEOUT_S = 10
AUDIT_TIMEOUT_S = 12

BINANCE_OPENING_OPS = {"neworder", "newalgoorder", "placemultipleorders", "marginaccountneworder",
                       "modifyorder", "modifymultipleorders"}

EXECUTOR_RE = re.compile(r"\bexecute_futures_trade(?:\.py)?\b")
DEPLOY_BATCH_RE = re.compile(r"\bdeploy_[A-Za-z0-9_]+\.py\b")
GUARDIAN_RE = re.compile(r"\bposition_guardian_loop(?:\.py)?\b")
POSITION_SCRIPTS_RE = re.compile(r"\b(?:night_cutoff_loop|dynamic_exit_manager)(?:\.py)?\b")

HELP_FLAGS = {"--help", "-h"}
EXECUTOR_NON_OPENING_FLAGS = {
    "--close-position", "--close_position", "--audit-orphans", "--audit_orphans", "--auto-heal", "--auto_heal",
    "--move-breakeven", "--move_breakeven", "--protect-pending", "--protect_pending",
}
EXECUTOR_READ_ONLY_FLAGS = {"--positions"}
GUARDIAN_NO_WRITE_FLAGS = {"--dry-run", "--dry_run"}
INSPECTION_PROGRAMS = {
    "git", "gh", "grep", "rg", "cat", "ls", "find", "diff", "echo", "printf", "head", "tail", "less", "wc",
    "stat", "file", "jq", "sort", "uniq", "awk", "sed", "cp", "mv", "rm", "mkdir", "chmod", "pytest",
}
# PowerShell cmdlets that merely mention the scripts (Claude Code PowerShell tool)
PS_INSPECTION_PROGRAMS = {
    "get-content", "gc", "type", "select-string", "sls", "get-childitem", "gci", "dir", "get-item", "gi",
    "test-path", "get-filehash", "write-output", "write-host", "copy-item", "move-item", "remove-item",
}
# Same mapping as pre_trade_guard.PS_UNICODE_TRANSLATION (fallback normaliser)
PS_UNICODE_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "―": "-",
})

try:
    import pre_trade_guard as _guard
except Exception:  # pragma: no cover - fall back to a conservative local classifier
    _guard = None


def find_workspace_root() -> str:
    p = os.path.abspath(__file__)
    while p and p != os.path.dirname(p):
        p = os.path.dirname(p)
        if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "logs")):
            return p
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _decode_value(value):
    for _ in range(3):
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped or stripped[0] not in "\"{[":
            return value
        try:
            value = json.loads(stripped)
        except ValueError:
            return value
    return value


def _decode_dict(value) -> dict:
    value = _decode_value(value)
    return value if isinstance(value, dict) else {}


def _is_true(value) -> bool:
    value = _decode_value(value)
    return value is True or str(value).strip().lower() == "true"


def is_opening_mcp_order(mcp_args: dict) -> bool:
    if _is_true(mcp_args.get("reduceOnly", mcp_args.get("reduce_only"))):
        return False
    if _is_true(mcp_args.get("closePosition", mcp_args.get("close_position"))):
        return False
    batch = _decode_value(mcp_args.get("batchOrders", mcp_args.get("batch_orders")))
    if isinstance(batch, list) and batch:
        return any(isinstance(o, dict) and is_opening_mcp_order(o) for o in batch)
    return True


def _fallback_powershell_text(command: str) -> str:
    """PowerShell text for classify_command when pre_trade_guard cannot be imported, with the same escape rules as
    its scanner: Unicode quotes/dashes -> ASCII, backtick + newline -> space, escaped quotes dropped, `x -> x,
    backslashes -> '/'."""
    command = command.translate(PS_UNICODE_TRANSLATION)
    command = re.sub(r"`(\r?\n|.)", lambda m: " " if m.group(1) in ("\n", "\r\n") else
                     ("" if m.group(1) in ("'", '"') else m.group(1)), command, flags=re.DOTALL)
    return command.replace("\\", "/")


def _normalize(payload: dict) -> dict:
    """Returns {kind, command, shell, server, tool, args} for agy or Claude Code payloads (PowerShell commands are
    normalised: backtick escapes stripped, backslashes -> /)."""
    if _guard is not None:
        try:
            call = _guard.normalize_tool_call(payload)
            command = call["command"]
            if call.get("shell") == "powershell":
                command = _guard.normalize_powershell_command(command)  # scripts\x.py -> scripts/x.py
            return {"kind": call["kind"], "command": command, "shell": call.get("shell", "bash"),
                    "server": call["server"], "tool": call["mcp_tool"], "args": call["mcp_args"] or {}}
        except Exception:
            pass

    out = {"kind": "other", "command": "", "shell": "bash", "server": "", "tool": "", "args": {}}
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else None
    if tool_call is not None:
        name = tool_call.get("name", "")
        args = tool_call.get("args", {}) if isinstance(tool_call.get("args"), dict) else {}
        if name == "run_command":
            out.update(kind="run_command", command=str(_decode_value(args.get("CommandLine", "")) or ""))
        elif name in ("call_mcp_tool", "mcp_tool"):
            out.update(kind="mcp", server=str(_decode_value(args.get("ServerName", "")) or ""),
                       tool=str(_decode_value(args.get("ToolName", "")) or ""),
                       args=_decode_dict(args.get("Arguments", {})))
        elif name.startswith("mcp_"):
            rest = name[4:]
            server, _, tool = rest.partition("_")
            out.update(kind="mcp", server=server, tool=tool, args=_decode_dict(args.get("Arguments", args)))
        return out

    name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) if isinstance(payload.get("tool_input"), dict) else {}
    if name in ("Bash", "PowerShell"):
        command = str(tool_input.get("command", ""))
        if name == "PowerShell":
            command = _fallback_powershell_text(command)
        out.update(kind="run_command", command=command, shell="powershell" if name == "PowerShell" else "bash")
    elif isinstance(name, str) and name.startswith("mcp__"):
        parts = name.split("__")
        out.update(kind="mcp", server=parts[1] if len(parts) > 1 else "",
                   tool="__".join(parts[2:]), args=tool_input)
    return out


def classify_binance_call(tool: str, mcp_args: dict):
    """Returns (order_placed, is_opening) for a Binance MCP tool call."""
    if _guard is not None:
        verdict, _reason, info = _guard.evaluate_binance_tool(tool, mcp_args)
        if verdict == "read_only":
            return False, False
        inner_tool = info.get("tool", tool)
        inner_args = info.get("args") or {}
        op = (info.get("operation") or str(inner_tool).rsplit(".", 1)[-1]).lower()
        if verdict == "risk_reducing" or verdict == "leverage":
            return True, False
        return True, op in BINANCE_OPENING_OPS and is_opening_mcp_order(inner_args)

    # Conservative fallback when the guard module cannot be imported
    name = tool
    args = mcp_args
    if name == "tool_execute":
        name = str(_decode_value(args.get("toolName", "")) or "")
        args = _decode_dict(args.get("arguments", {}))
    op = name.rsplit(".", 1)[-1].lower()
    if op in BINANCE_OPENING_OPS:
        return True, is_opening_mcp_order(args)
    if "cancel" in op or op.startswith("delete") or op in ("changeinitialleverage", "changemargintype"):
        return True, False
    return False, False


def _split_subcommands(command_line: str):
    if _guard is not None:
        try:
            return _guard.split_subcommands(command_line)
        except Exception:
            pass
    parts = re.split(r"&&|\|\||[;|&\n]", command_line)
    return [p.split() for p in parts if p.strip()]


def _program(tokens) -> str:
    if _guard is not None:
        try:
            return _guard._program(tokens)
        except Exception:
            pass
    for tok in tokens:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            return os.path.basename(tok).lower()
    return ""


def classify_command(command_line: str, shell: str = "bash"):
    """Returns (order_placed, is_opening) for a shell command, evaluated per sub-command."""
    order_placed = False
    is_opening = False
    inspection = INSPECTION_PROGRAMS | (PS_INSPECTION_PROGRAMS if shell == "powershell" else set())
    for tokens in _split_subcommands(command_line or ""):
        if not tokens or _program(tokens) in inspection:
            continue
        text = " ".join(tokens)
        flags = {t.split("=", 1)[0].lower() for t in tokens if t.startswith("-")}
        if DEPLOY_BATCH_RE.search(text):
            order_placed = is_opening = True
        elif EXECUTOR_RE.search(text):
            if flags & EXECUTOR_NON_OPENING_FLAGS:
                order_placed = True
            elif flags & (EXECUTOR_READ_ONLY_FLAGS | HELP_FLAGS):
                continue
            else:
                order_placed = is_opening = True
        elif GUARDIAN_RE.search(text):
            if not flags & (GUARDIAN_NO_WRITE_FLAGS | HELP_FLAGS):
                order_placed = True
        elif POSITION_SCRIPTS_RE.search(text):
            if not flags & HELP_FLAGS:
                order_placed = True
    return order_placed, is_opening


def handle_post_trade_sync(payload: dict) -> dict:
    """
    Inspects toolCall payload and executes necessary sync / audit operations.
    Returns status dict for observability and testing.
    """
    call = _normalize(payload if isinstance(payload, dict) else {})
    command_line = call["command"] or ""
    mcp_args = call["args"] if isinstance(call["args"], dict) else {}

    order_placed = False
    is_opening = False

    if call["kind"] == "mcp":
        server = (call["server"] or "").lower().replace("-", "_")
        tool = call["tool"] or ""
        is_binance = "binance" in server or (_guard is not None and _guard.is_binance_call("", tool))
        if tool == "tool_execute" and _guard is not None:
            inner = str(_decode_value(mcp_args.get("toolName", "")) or "")
            is_binance = is_binance or _guard.is_binance_call("", inner)

        if is_binance:
            order_placed, is_opening = classify_binance_call(tool, mcp_args)

    elif call["kind"] == "run_command":
        order_placed, is_opening = classify_command(command_line, call.get("shell", "bash"))

    result = {
        "order_placed": order_placed,
        "is_opening": is_opening,
        "synced": False,
        "audit_healed": False
    }

    if not order_placed:
        return result

    base_dir = find_workspace_root()
    scripts_dir = os.path.join(base_dir, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    target_env = None
    if isinstance(mcp_args, dict):
        target_env = mcp_args.get("target_env") or mcp_args.get("env")
    if not target_env and command_line:
        m = re.search(r"--env(?:\s+|=)(['\"]?)([A-Za-z0-9_-]+)\1", command_line, re.IGNORECASE)
        if m:
            target_env = m.group(2)
    if not target_env:
        try:
            from utils.env_resolver import resolve_env
            target_env = resolve_env(base_dir=base_dir)
        except Exception:
            target_env = os.environ.get("BINANCE_API_ENV", "prod").lower()
    target_env = str(target_env).lower()

    # 1. Trigger session state sync (bounded)
    sync_script = os.path.join(base_dir, "scripts", "sync_session_state.py")
    if os.path.exists(sync_script):
        try:
            subprocess.run([sys.executable, sync_script, "--env", target_env], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=SYNC_TIMEOUT_S)
            result["synced"] = True
        except Exception as e:
            sys.stderr.write(f"[POST-TRADE-SYNC ERROR] Sync failed: {e}\n")

    # 2. Run orphan audit with auto-heal if opening order (bounded, daemon thread)
    if is_opening:
        outcome = {}

        def _audit():
            try:
                import execute_futures_trade as eft
                eft.audit_orphan_positions(target_env=target_env, auto_heal=True)
                outcome["ok"] = True
            except Exception as e:  # pragma: no cover - logged only
                outcome["error"] = str(e)

        worker = threading.Thread(target=_audit, name="post-trade-orphan-audit", daemon=True)
        worker.start()
        worker.join(AUDIT_TIMEOUT_S)
        if outcome.get("ok"):
            result["audit_healed"] = True
        elif worker.is_alive():
            sys.stderr.write(f"[POST-TRADE-SYNC AUDIT ERROR] Orphan audit exceeded {AUDIT_TIMEOUT_S}s budget.\n")
        else:
            sys.stderr.write(f"[POST-TRADE-SYNC AUDIT ERROR] Orphan audit failed: {outcome.get('error')}\n")

    return result


def main():
    try:
        raw_input = sys.stdin.read()
        if raw_input.strip():
            payload = json.loads(raw_input)
            if isinstance(payload, dict):
                handle_post_trade_sync(payload)
    except Exception as e:
        sys.stderr.write(f"[POST-TRADE-SYNC ERROR] {e}\n")

    # PostToolUse contract expects an empty JSON object on stdout
    print(json.dumps({}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    main()
    # Exit immediately even if a bounded audit thread is still running.
    os._exit(0)
