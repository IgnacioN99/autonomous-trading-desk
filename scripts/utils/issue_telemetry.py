#!/usr/bin/env python3
"""
issue_telemetry.py - Shared telemetry collector and issue-body renderer for the GitHub issue reporters.

Used by scripts/report_agent_issue.py (imported) and scripts/report_issue.sh (CLI), so both reporters
emit the same six-section engineering report with mandatory severity + priority labels.

Privacy contract: telemetry never contains monetary amounts (no notional, margin, balance or PnL values;
PnL is reported as a sign only) and never the hostname. Every field falls back to "unavailable"; nothing
in this module raises to the caller.

CLI (always exits 0):
  python3 scripts/utils/issue_telemetry.py --json [--logs-dir X] [--base-dir Y]
  python3 scripts/utils/issue_telemetry.py --markdown [--logs-dir X] [--base-dir Y]
  python3 scripts/utils/issue_telemetry.py --kv [--logs-dir X] [--base-dir Y]   (key<TAB>value lines)
"""

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

UNAVAILABLE = "unavailable"
NO_STATE = "no session_state.json"

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
PRIORITIES = ("P0", "P1", "P2", "P3")
DEFAULT_PRIORITY = {"CRITICAL": "P0", "HIGH": "P1", "MEDIUM": "P2", "LOW": "P3"}
CATEGORY_RE = re.compile(r"^[a-z0-9_-]+$")

SEVERITY_BADGES = {"CRITICAL": "🔴 CRITICAL", "HIGH": "🟠 HIGH", "MEDIUM": "🟡 MEDIUM", "LOW": "🔵 LOW"}
PRIORITY_BADGES = {"P0": "🔴 P0", "P1": "🟠 P1", "P2": "🟡 P2", "P3": "🔵 P3"}

# name -> (color, description), used by `gh label create --force` / REST label ensure.
LABEL_SPECS = {
    "severity:critical": ("b60205", "A position is unprotected or its stop cannot be verified"),
    "severity:high": ("d93f0b", "Execution or a risk gate is blocked/incorrect"),
    "severity:medium": ("fbca04", "Scan, analysis or tooling degraded"),
    "severity:low": ("0e8a16", "Cosmetic, docs or optional hardening"),
    "priority:P0": ("b60205", "Drop everything: fix now"),
    "priority:P1": ("d93f0b", "Next up: fix in the current cycle"),
    "priority:P2": ("fbca04", "Planned: schedule soon"),
    "priority:P3": ("c5def5", "Backlog: when convenient"),
    "agent-failure": ("ededed", ""),
}
DEFAULT_LABEL_SPEC = ("ededed", "")  # cat:* and anything else

SECTION_HEADINGS = (
    "### 1. Executive Summary & Severity Matrix",
    "### 2. Runtime & Ledger Telemetry Snapshot",
    "### 3. Reproduction & Exact Telemetry",
    "### 4. Code Pointers & Root Cause Analysis",
    "### 5. Operational Impact on Trading Desk",
    "### 6. Remediation Plan & Acceptance Criteria",
)

CATEGORY_IMPACT = {
    "risk_gate": "A risk gate blocked or mis-evaluated an order (fail-closed)",
    "tool_error": "A desk script/tool failed; the affected step was stopped fail-closed",
    "infra": "Infrastructure/API degradation",
    "agent_failure": "Agent workflow failure; no orders were placed from this step",
}
GENERIC_IMPACT = "Impact not specified by the reporting agent; triage required"
DEFAULT_ACCEPTANCE = "Regression test added in tests/ covering this failure"

OUTPUT_TAIL_LINES = 200
OUTPUT_TAIL_CHARS = 12000
CONTEXT_FILE_CHARS = 8000

# (key, row label) in the order rendered by render_telemetry_markdown
TELEMETRY_FIELDS = (
    ("target_env", "Target environment"),
    ("commit", "Git commit"),
    ("branch", "Git branch"),
    ("dirty", "Working tree dirty"),
    ("python_version", "Python"),
    ("platform", "Platform"),
    ("ledger", "Ledger snapshot"),
    ("is_valid", "Ledger sync valid (is_valid)"),
    ("state_age_s", "Ledger age (s)"),
    ("delta_bias", "Portfolio delta bias"),
    ("positions_count", "Open positions"),
    ("positions", "Positions (symbol direction)"),
    ("sl_verified", "Stop Loss verified (algo)"),
    ("floating_pnl_sign", "Floating PnL sign"),
    ("session_pnl_sign", "Session realized PnL sign"),
    ("closed_trades", "Closed trades today"),
)
LEDGER_KEYS = ("is_valid", "state_age_s", "delta_bias", "positions_count", "positions", "sl_verified",
               "floating_pnl_sign", "session_pnl_sign", "closed_trades")


# ------------------------------------------------------------------------------
# Severity / priority / labels
# ------------------------------------------------------------------------------
def normalize_severity(severity: Any) -> str:
    """Case-insensitive severity normalization; raises ValueError on anything outside SEVERITIES."""
    value = str(severity if severity is not None else "").strip().upper()
    if value not in SEVERITIES:
        raise ValueError(f"invalid severity {severity!r}: expected one of {'|'.join(SEVERITIES)}")
    return value


def normalize_priority(priority: Any, severity: Any = "HIGH") -> str:
    """Case-insensitive priority normalization; None/"" -> default from severity. Raises ValueError."""
    if priority is None or not str(priority).strip():
        return DEFAULT_PRIORITY[normalize_severity(severity)]
    value = str(priority).strip().upper()
    if value not in PRIORITIES:
        raise ValueError(f"invalid priority {priority!r}: expected one of {'|'.join(PRIORITIES)}")
    return value


def normalize_category(category: Any, required: bool = False) -> str:
    """Lowercases a category, turns runs of spaces/'-' into '_' and validates it. Empty is allowed (no cat:* label) unless required."""
    value = re.sub(r'[\s-]+', '_', str(category if category is not None else "").strip().lower())
    if not value and not required:
        return ""
    if not CATEGORY_RE.match(value):
        raise ValueError(f"invalid category {category!r}: lowercase letters, digits and '_' only (spaces and '-' become '_')")
    return value


def with_default_priority(labels: List[str]) -> List[str]:
    """Legacy label lists with severity:* but no priority:* get the default priority right after the severity."""
    labels = list(labels or [])
    if any(str(l).startswith("priority:") for l in labels):
        return labels
    for i, label in enumerate(labels):
        if str(label).startswith("severity:"):
            try:
                prio = DEFAULT_PRIORITY[normalize_severity(label.split(":", 1)[1])]
            except ValueError:
                return labels
            return labels[:i + 1] + [f"priority:{prio}"] + labels[i + 1:]
    return labels


def severity_label(severity: str) -> str:
    return f"severity:{normalize_severity(severity).lower()}"


def priority_label(priority: str, severity: str = "HIGH") -> str:
    return f"priority:{normalize_priority(priority, severity)}"


def build_labels(severity: str, priority: Optional[str], category: str = "") -> List[str]:
    labels = ["agent-failure", severity_label(severity), priority_label(priority, severity)]
    if category:
        labels.append(f"cat:{str(category).lower()}")
    return labels


def label_spec(name: str):
    """Returns (color, description) for a label name."""
    return LABEL_SPECS.get(name, DEFAULT_LABEL_SPEC)


def required_labels(labels: List[str]) -> List[str]:
    """The labels that must never be silently dropped: severity:* and priority:*."""
    return [l for l in (labels or []) if str(l).startswith(("severity:", "priority:"))]


def title_prefix_from_labels(labels: List[str]) -> str:
    """'[HIGH/P1] ' from the severity/priority labels (used only when an issue is created without labels)."""
    parts = []
    sev = next((l.split(":", 1)[1] for l in labels or [] if str(l).startswith("severity:")), "")
    prio = next((l.split(":", 1)[1] for l in labels or [] if str(l).startswith("priority:")), "")
    if sev:
        parts.append(sev.upper())
    if prio:
        parts.append(prio.upper())
    return f"[{'/'.join(parts)}] " if parts else ""


# ------------------------------------------------------------------------------
# Telemetry
# ------------------------------------------------------------------------------
def _git(base_dir: str, *args: str) -> Optional[str]:
    try:
        res = subprocess.run(["git", *args], cwd=base_dir, capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return res.stdout
    except Exception:
        pass
    return None


def _resolve_target_env(base_dir: str) -> str:
    try:
        try:
            from utils.env_resolver import resolve_env
        except Exception:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from env_resolver import resolve_env
        return resolve_env(base_dir=base_dir).upper()
    except Exception:
        return os.getenv("BINANCE_API_ENV", "TESTNET").upper()


def _sign(value: Any) -> str:
    try:
        v = float(value)
    except Exception:
        return UNAVAILABLE
    return "POSITIVE" if v > 0 else ("NEGATIVE" if v < 0 else "FLAT")


def _ledger_telemetry(logs_dir: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {k: UNAVAILABLE for k in LEDGER_KEYS}
    state_file = os.path.join(logs_dir, "session_state.json")
    if not os.path.exists(state_file):
        out["ledger"] = NO_STATE
        return out
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("session_state.json is not an object")
    except Exception:
        out["ledger"] = "unreadable session_state.json"
        return out

    out["ledger"] = "session_state.json"
    if "is_valid" in st:
        out["is_valid"] = bool(st.get("is_valid"))
    try:
        out["state_age_s"] = max(0, int(time.time() - float(st["last_updated_ts"])))
    except Exception:
        pass

    exposure = st.get("portfolio_exposure") if isinstance(st.get("portfolio_exposure"), dict) else {}
    if exposure.get("delta_bias"):
        out["delta_bias"] = str(exposure["delta_bias"])
    if "total_floating_pnl_usdt" in exposure:
        out["floating_pnl_sign"] = _sign(exposure.get("total_floating_pnl_usdt"))

    closed = st.get("closed_today_summary") if isinstance(st.get("closed_today_summary"), dict) else {}
    if "net_realized_pnl_usdt" in closed:
        out["session_pnl_sign"] = _sign(closed.get("net_realized_pnl_usdt"))
    if "closed_trades_count" in closed:
        try:
            out["closed_trades"] = int(closed["closed_trades_count"])
        except Exception:
            pass

    positions = st.get("active_positions")
    if isinstance(positions, list):
        valid = [p for p in positions if isinstance(p, dict)]
        out["positions_count"] = len(valid)
        entries = sorted(f"{p.get('symbol', '?')} {p.get('direction') or 'UNKNOWN'}" for p in valid)
        out["positions"] = ", ".join(entries) if entries else "none"
        verified = sum(1 for p in valid if p.get("sl_algo_verified") is True)
        out["sl_verified"] = f"{verified}/{len(valid)}"
    return out


def collect_telemetry(base_dir: str, logs_dir: Optional[str] = None) -> Dict[str, Any]:
    """Runtime + git + ledger telemetry. Never raises; every field falls back to 'unavailable'."""
    t: Dict[str, Any] = {key: UNAVAILABLE for key, _ in TELEMETRY_FIELDS}
    try:
        base_dir = os.path.abspath(base_dir or ".")
        logs_dir = logs_dir or os.path.join(base_dir, "logs")

        commit = _git(base_dir, "rev-parse", "--short", "HEAD")
        if commit and commit.strip():
            t["commit"] = commit.strip()
        branch = _git(base_dir, "rev-parse", "--abbrev-ref", "HEAD")
        if branch and branch.strip():
            t["branch"] = branch.strip()
        porcelain = _git(base_dir, "status", "--porcelain")
        if porcelain is not None:
            t["dirty"] = bool(porcelain.strip())

        t["target_env"] = _resolve_target_env(base_dir)
        t["python_version"] = platform.python_version()
        try:
            t["platform"] = platform.platform()
        except Exception:
            pass
        t.update(_ledger_telemetry(logs_dir))
    except Exception:
        pass
    return t


def _cell(value: Any) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    return "`" + str(value).replace("|", "\\|").replace("`", "'").replace("\n", " ") + "`"


def render_telemetry_markdown(t: Dict[str, Any]) -> str:
    rows = ["| Signal | Value |", "| :--- | :--- |"]
    for key, label in TELEMETRY_FIELDS:
        rows.append(f"| **{label}** | {_cell((t or {}).get(key, UNAVAILABLE))} |")
    return "\n".join(rows)


def git_summary(t: Dict[str, Any]) -> str:
    dirty = t.get("dirty", UNAVAILABLE)
    if isinstance(dirty, bool):
        dirty = "true" if dirty else "false"
    return f"`{t.get('commit', UNAVAILABLE)}` on `{t.get('branch', UNAVAILABLE)}` (dirty: {dirty})"


# ------------------------------------------------------------------------------
# Body rendering helpers
# ------------------------------------------------------------------------------
def read_tail(path: str, lines: int = OUTPUT_TAIL_LINES, max_chars: int = OUTPUT_TAIL_CHARS) -> str:
    """Last `lines` lines of a file, capped at `max_chars` characters (keeps the end)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = "".join(f.readlines()[-lines:])
    except Exception as e:
        return f"(could not read output file {path}: {type(e).__name__})"
    return text[-max_chars:] if len(text) > max_chars else text


def read_capped(path: str, max_chars: int = CONTEXT_FILE_CHARS) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except Exception as e:
        return f"(could not read context file {path}: {type(e).__name__})"


CREDENTIAL_PATH_RE = re.compile(r'^(\.env(\..*)?|.*\.env|\.?mcp\.json|.*mcp_config.*|.*credentials.*|.*\.pem|.*\.key)$')
CREDENTIAL_PATH_HINT = ".env, *.env, MCP configs (.mcp.json, mcp_config*), *credentials*, *.pem, *.key"


def is_credential_path(path: Any) -> bool:
    """True when the path as given or its resolved target names a credential-bearing file (#58)."""
    if path is None or not str(path).strip():
        return False
    raw = str(path)
    try:
        resolved = os.path.realpath(raw)
    except (OSError, ValueError):
        resolved = ""
    for candidate in (raw, resolved):
        if candidate and CREDENTIAL_PATH_RE.match(candidate.replace("\\", "/").rsplit("/", 1)[-1].lower()):
            return True
    return False


def split_items(text: str, separators: str = ",\n") -> List[str]:
    items = [text or ""]
    for sep in separators:
        items = [part for chunk in items for part in chunk.split(sep)]
    return [i.strip() for i in items if i.strip()]


def acceptance_items(text: str) -> List[str]:
    out = []
    for item in split_items(text, ";\n"):
        item = item.lstrip("-* ").strip()
        if item[:3].lower() in ("[ ]", "[x]"):
            item = item[3:].strip()
        if item:
            out.append(item)
    return out or [DEFAULT_ACCEPTANCE]


def default_impact(category: str) -> str:
    return CATEGORY_IMPACT.get(str(category or "").lower(), GENERIC_IMPACT)


def render_issue_body(
    error_detail: str,
    severity: str,
    priority: Optional[str] = None,
    category: str = "agent_failure",
    agent_name: str = "autonomous_agent",
    telemetry: Optional[Dict[str, Any]] = None,
    repro: str = "",
    output_text: str = "",
    stack_trace: str = "",
    affected_files: str = "",
    root_cause: str = "",
    context: str = "",
    impact: str = "",
    remediation: str = "",
    acceptance_criteria: str = "",
    fingerprint: str = "",
    timestamp_utc: Optional[str] = None,
    sanitize: Optional[Callable[[str], str]] = None,
    footer: str = "*Reported automatically by the `autonomous-trading-desk` observability harness.*",
) -> str:
    """Six-section Markdown body (same headings as report_issue.sh). Free text goes through `sanitize`."""
    clean = sanitize or (lambda s: s)

    def s(text: str) -> str:
        return clean((text or "").strip())

    sev = normalize_severity(severity)
    prio = normalize_priority(priority, sev)
    # Telemetry, category and agent name reach the body too: sanitize them like any other free text
    t = {k: (clean(v) if isinstance(v, str) else v) for k, v in (telemetry or {}).items()}
    category = clean(str(category or ""))
    agent_name = clean(str(agent_name or ""))
    ts = timestamp_utc or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    body = [
        "## 🚨 Autonomous Agent Failure Report",
        "",
        SECTION_HEADINGS[0],
        "",
        "| Dimension | Value |",
        "| :--- | :--- |",
        f"| **Severity** | **{SEVERITY_BADGES[sev]}** |",
        f"| **Priority** | **{PRIORITY_BADGES[prio]}** |",
        f"| **Category** | `{category}` |",
        f"| **Reporting Agent** | `{agent_name}` |",
        f"| **Environment** | `{t.get('target_env', UNAVAILABLE)}` |",
        f"| **Git** | {git_summary(t)} |",
        f"| **Timestamp UTC** | `{ts}` |",
    ]
    if fingerprint:
        body.append(f"| **Fingerprint ID** | `{fingerprint}` |")
    body += ["", "**Description:**", s(error_detail), ""]

    body += [SECTION_HEADINGS[1], "", render_telemetry_markdown(t), ""]

    body += [SECTION_HEADINGS[2], ""]
    clean_repro = s(repro)
    if clean_repro:
        body += ["**Reproduction command:**", "```bash", clean_repro, "```", ""]
    else:
        body += ["**Reproduction command:** not provided", ""]
    clean_trace = s(stack_trace)
    if clean_trace:
        body += ["**Traceback:**", "```text", clean_trace, "```", ""]
    clean_output = clean(output_text or "").rstrip()
    if clean_output:
        body += ["<details><summary>Raw output (tail)</summary>", "", "```text", clean_output, "```", "",
                 "</details>", ""]

    body += [SECTION_HEADINGS[3], ""]
    files = split_items(s(affected_files))
    if files:
        body += ["**Affected files:**"] + [f"- `{f}`" for f in files] + [""]
    else:
        body += ["**Affected files:** not provided", ""]
    body += [f"**Root cause:** {s(root_cause) or 'not provided'}", ""]
    clean_context = s(context)
    if clean_context:
        body += ["**Context:**", clean_context, ""]

    body += [SECTION_HEADINGS[4], "", s(impact) or default_impact(category), ""]

    body += [SECTION_HEADINGS[5], "", f"**Remediation:** {s(remediation) or 'not provided'}", "",
             "**Acceptance criteria:**"]
    body += [f"- [ ] {item}" for item in acceptance_items(s(acceptance_criteria))]
    body += ["", "---", footer]
    return "\n".join(body)


# ------------------------------------------------------------------------------
# CLI (used by scripts/report_issue.sh)
# ------------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    default_base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser = argparse.ArgumentParser(description="Issue reporter telemetry (no monetary amounts)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="Print the telemetry dict as JSON (default)")
    mode.add_argument("--markdown", action="store_true", help="Print the telemetry Markdown table")
    mode.add_argument("--kv", action="store_true", help="Print key<TAB>value lines (for bash)")
    parser.add_argument("--logs-dir", default=None, help="Directory holding session_state.json")
    parser.add_argument("--base-dir", default=default_base, help="Repository root (git + env resolution)")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    try:
        t = collect_telemetry(args.base_dir, args.logs_dir)
        if args.markdown:
            print(render_telemetry_markdown(t))
        elif args.kv:
            for key, _ in TELEMETRY_FIELDS:
                value = t.get(key, UNAVAILABLE)
                if isinstance(value, bool):
                    value = "true" if value else "false"
                print(f"{key}\t{str(value).replace(chr(9), ' ').replace(chr(10), ' ')}")
        else:
            print(json.dumps(t, ensure_ascii=False))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
