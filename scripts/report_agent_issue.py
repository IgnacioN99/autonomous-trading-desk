#!/usr/bin/env python3
"""
report_agent_issue.py - Autonomous GitHub Issue Generator for Agentic Setup Failures.

Enables subagents, hooks, and trading desk loops to programmatically report unhandled exceptions,
infrastructure anomalies, or tool errors directly to GitHub or safely enqueue in local backlog.

Resilience Features:
1. Direct GitHub Dispatch: Uses GitHub REST API v3 with GITHUB_TOKEN (Personal Access Token).
2. Resilient Offline Backlog: If GITHUB_TOKEN is not configured, GITHUB_REPO is unresolvable,
   or network fails, atomically enqueues the issue in logs/issues_backlog.jsonl for later sync.
3. Safe Repository Resolution: Derives repo dynamically from GITHUB_REPO env var or git remote origin.
   NEVER defaults to a hardcoded remote.
4. Telemetry Sanitization: Masks API tokens, secret keys, the values of any secret/token/password/private-key/api-key
   key, Binance signature= / listenKey values, exact balances, and full state payloads. Attachments
   (--context-file / --output-file) naming a credential file (.env, *.env, MCP configs, *.pem, *.key) are refused.
5. Intelligent Deduplication / Anti-Spam: Computes a SHA-256 fingerprint; if the same failure
   occurred within the past 24 hours, updates telemetry rather than spamming new issues. The fingerprint file's
   read-modify-write is serialised with utils/file_lock.py (best effort, never blocks). Issue #270 (sessions in
   parallel): before creating an issue, find_open_issue looks the fingerprint up in the OPEN GitHub issues (one
   `gh issue list`); a hit is counted locally instead of creating a duplicate, and any lookup failure creates or
   queues as before. Issue #284: a hit whose `severity:*` label is LESS severe than the new report does not swallow
   it (the new issue is created), and neither does a local 24 h record of a less severe report (records keep their
   severity; one written before #284 deduplicates as before). An open issue without a `severity:*` label still
   swallows even a CRITICAL report (both reporters always label their issues). Extra latency besides
   the GitHub POST itself: the lookup plus up to 3 fingerprint-lock waits per call (the 24 h check, the hit or the
   new record, and the backlog record when that write fails). In-process callers (executor, guardian, night
   cutoff: report_issue() defaults) are bounded to INPROCESS_LOOKUP_TIMEOUT_S = 3 s + 3 x INPROCESS_LOCK_WAIT_S
   = 1 s (worst case 6 s); the CLI keeps DEDUPE_LOOKUP_TIMEOUT_S = 5 s + 3 x file_lock.LOCK_WAIT_S = 2 s.
   `--find-open-issue` exposes the same fingerprint, lookup and severity rule to report_issue.sh and records a hit
   locally. Backlog sync (--sync-backlog, report_issue.sh --sync) does no lookup: it only skips fingerprints this
   machine already published. The logs directory follows ISSUE_REPORTER_LOGS_DIR when set (as report_issue.sh).

6. Structured Report: six-section body (scripts/utils/issue_telemetry.py, same headings as report_issue.sh)
   with runtime/ledger telemetry and mandatory severity:* + priority:* labels that are never silently dropped.

Usage:
  python3 scripts/report_agent_issue.py --title "Cointegration Calculation Failure" --error "ZeroDivisionError: ..." \\
      --category "quant_logic" --severity "HIGH" [--priority P1] [--repro "<cmd> (exit 1)"] [--root-cause "..."] \\
      [--affected-files "scripts/x.py:10-20"] [--context "..."] [--context-file F] [--output-file F] \\
      [--impact "..."] [--acceptance-criteria "a; b"]
  python3 scripts/report_agent_issue.py --sync-backlog

Priority defaults from severity: CRITICAL->P0, HIGH->P1, MEDIUM->P2, LOW->P3.
"""

import os
import sys
import json
import time
import re
import shutil
import shlex
import hashlib
import datetime
import subprocess
import tempfile
import http.client
import urllib.request
import urllib.error
import argparse
from typing import Dict, Any, Optional, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from utils import issue_telemetry  # noqa: E402
from utils import file_lock  # noqa: E402
LOGS_DIR = os.environ.get("ISSUE_REPORTER_LOGS_DIR") or os.path.join(BASE_DIR, "logs")
BACKLOG_FILE = os.path.join(LOGS_DIR, "issues_backlog.jsonl")
FINGERPRINTS_FILE = os.path.join(LOGS_DIR, "issues_fingerprints.json")
DEFAULT_REPO = None

PUBLISHED_GITHUB = "PUBLISHED_GITHUB"
PUBLISHED_UNKNOWN_URL = "PUBLISHED_UNKNOWN_URL"
PUBLISHED_STATUSES = (PUBLISHED_GITHUB, PUBLISHED_UNKNOWN_URL)

# Labelled quant statistics whose signed values survive the bare signed-decimal rule (#61)
QUANT_STAT_KEYS = r't[_-]?stat|tstat|z|z[_-]?score|beta|half[_-]?life|hurst|r2|p[_-]?value|pvalue|corr'

# Load local .env manually if present to avoid python-dotenv external dependency
def load_env_file():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key.startswith("GITHUB_") and key not in os.environ:
                        os.environ[key] = val

load_env_file()

def derive_github_repo(base_dir: str = BASE_DIR) -> Optional[str]:
    """
    Derives GitHub repository from GITHUB_REPO environment variable or git remote origin.
    Returns None if not configured or unresolvable.
    """
    env_repo = os.getenv("GITHUB_REPO")
    if env_repo and env_repo.strip():
        return env_repo.strip()

    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=5
        )
        if res.returncode == 0 and res.stdout.strip():
            url = res.stdout.strip()
            # Match formats:
            # git@github.com:owner/repo(.git)
            # https://github.com/owner/repo(.git)
            # ssh://git@github.com/owner/repo(.git)
            m = re.search(r'(?:github\.com[:/])([\w\-]+/[\w\-]+?)(?:\.git)?$', url)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

def sanitize_telemetry(text: str) -> str:
    """
    Masks or redacts sensitive telemetry:
    - GitHub tokens (ghp_, github_pat_)
    - Notion tokens (secret_, ntn_)
    - Bearer tokens
    - API keys, secret keys, passwords (Binance, Gmail, etc.)
    - Values of any *secret* / *token* / *password* / *passwd* / *private_key* / *api_key* key (key=value,
      key: value, JSON "key": "value"); the key name is kept, so prose such as "tokens: 1800" is over-redacted
    - Binance signed-request signatures and user-data stream listen keys (signature=, listenKey=)
    - Exact monetary balances and session state payloads, and bare signed decimals, except the values of
      labelled quant statistics (t_stat: -3.42, "z": -2.15, beta:-0.87; see QUANT_STAT_KEYS)
    """
    if not text:
        return ""

    # Redact GitHub tokens
    text = re.sub(r'ghp_[A-Za-z0-9_]{20,}', '[REDACTED_GH_TOKEN]', text)
    text = re.sub(r'github_pat_[A-Za-z0-9_]{20,}', '[REDACTED_GH_PAT]', text)

    # Redact Notion tokens
    text = re.sub(r'(?:secret_|ntn_)[A-Za-z0-9_]{20,}', '[REDACTED_NOTION_TOKEN]', text)

    # Redact Authorization: Bearer tokens
    text = re.sub(r'(Bearer\s+)[A-Za-z0-9\-._~+/]+=*', r'\1[REDACTED_TOKEN]', text, flags=re.IGNORECASE)

    # Redact common key/secret pairs in JSON or key=value formats
    text = re.sub(
        r'(?i)(api[_-]?key|secret[_-]?key|password|auth[_-]?token|app[_-]?password)\s*[:=]\s*["\']?([A-Za-z0-9/+=._-]{8,})["\']?',
        r'\1=[REDACTED]',
        text
    )
    # Generic secret/token/password/private-key/api-key values (key=value, key: value, JSON "key": "value"); key name kept
    text = re.sub(
        r'(?i)((?:secret|token|passw(?:or)?d|private[_-]?key|api[_-]?key)[A-Za-z0-9_-]*"?[ \t]*[:=][ \t]*"?)[A-Za-z0-9/+=._~-]+',
        r'\1[REDACTED]', text)
    # Binance signed-request signatures and user-data stream listen keys
    text = re.sub(r'(?i)((?:signature|listen[_-]?key)"?[ \t]*[:=][ \t]*"?)[A-Za-z0-9]+', r'\1[REDACTED]', text)

    # Redact exact balances in JSON formats
    text = re.sub(
        r'(?i)"(totalWalletBalance|availableBalance|walletBalance|unrealizedProfit|isolatedMargin|balance|equity)"\s*:\s*["\']?[0-9.-]+["\']?',
        r'"\1": "[REDACTED]"',
        text
    )

    # Redact numeric values of monetary keys in JSON / key=value text (e.g. "notional_usdt": 4321.87, margin=-3.2)
    text = re.sub(
        r'(?i)((?<![A-Za-z0-9_])"?[A-Za-z0-9_]*(?:usdt|usd|pnl|notional|margin|balance|equity|profit|wallet)'
        r'[A-Za-z0-9_]*"?\s*[:=]\s*)'
        r'["\']?[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?["\']?',
        r'\1"[REDACTED]"',
        text
    )

    # Redact exact monetary balances in text (e.g. $10245.50 USDT, $+3087.31, $-16.75)
    text = re.sub(r'\$\s*[-+]?\d+(?:\.\d+)?\s*(USDT|USD)?', r'$[REDACTED]', text)
    # Amounts followed by USDT/USD, also after a closing backtick/bold marker (`3.20` USDT, **3.20** USDT);
    # \b keeps symbols such as API3USDT / C98USDT intact
    text = re.sub(r'(?i)\b\d+(?:\.\d+)?[`*]*\s*(USDT|USD)\b', r'[REDACTED_AMT] USDT', text)
    # Labelled quant statistics (t_stat: -3.42, "z": -2.15, beta:-0.87) keep their sign: protect it, redact, restore
    text = re.sub(r'~Q(?:NEG|POS)~', '', text)
    text = re.sub(r'(?im)(^|[^A-Za-z0-9_])("?(?:' + QUANT_STAT_KEYS + r')"?[ \t]*[:=][ \t]*)-([0-9])', r'\1\2~QNEG~\3', text)
    text = re.sub(r'(?im)(^|[^A-Za-z0-9_])("?(?:' + QUANT_STAT_KEYS + r')"?[ \t]*[:=][ \t]*)\+([0-9])', r'\1\2~QPOS~\3', text)
    # Bare signed decimals (PnL table cells like "| -16.75 |"), not percentages
    text = re.sub(r'(?<![^\s|`(:])[+-]\d+\.\d+(?![\d%])', '[REDACTED_AMT]', text)
    text = text.replace('~QNEG~', '-').replace('~QPOS~', '+')

    return text

def compute_fingerprint(title: str, error_detail: str) -> str:
    """Generates a unique SHA-256 fingerprint for the error signature to prevent duplicates."""
    norm_text = f"{title.strip().lower()}|{error_detail.strip()[:200].lower()}"
    return hashlib.sha256(norm_text.encode("utf-8")).hexdigest()[:16]

def load_fingerprints() -> Dict[str, Any]:
    if os.path.exists(FINGERPRINTS_FILE):
        try:
            with open(FINGERPRINTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_fingerprints(data: Dict[str, Any]):
    """Atomic write through a unique temp file (concurrent writers never share it)."""
    target_dir = os.path.dirname(os.path.abspath(FINGERPRINTS_FILE))
    os.makedirs(target_dir, exist_ok=True)
    fd, temp_file = tempfile.mkstemp(prefix=".issues_fingerprints.", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, FINGERPRINTS_FILE)
    except BaseException:
        try:
            os.remove(temp_file)
        except OSError:
            pass
        raise

def set_fingerprint(fingerprint: str, entry: Dict[str, Any], wait_s: Optional[float] = None):
    """Locked read-modify-write of one fingerprint record (issue #270: reporters of parallel sessions). wait_s: the
    lock wait (None: file_lock.LOCK_WAIT_S)."""
    with file_lock.locked(FINGERPRINTS_FILE, wait_s):
        cache = load_fingerprints()
        cache[fingerprint] = entry
        save_fingerprints(cache)

DEDUPE_LOOKUP_TIMEOUT_S = 5.0  # CLI (and report_issue.sh) open-issue lookup bound
# Issue #284: report_issue() defaults for in-process callers (executor / guardian close and abort-failure paths)
INPROCESS_LOOKUP_TIMEOUT_S = 3.0
INPROCESS_LOCK_WAIT_S = 1.0
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
SEVERITY_LABEL_RE = re.compile(r"^severity:(critical|high|medium|low)$", re.IGNORECASE)

def _issue_severity(labels: Any) -> Optional[str]:
    """The most severe `severity:*` label of a gh issue (label objects or names), None when there is none."""
    found = []
    for label in labels if isinstance(labels, list) else []:
        name = label.get("name") if isinstance(label, dict) else label
        m = SEVERITY_LABEL_RE.match(str(name or "").strip())
        if m:
            found.append(m.group(1).upper())
    return min(found, key=issue_telemetry.SEVERITIES.index) if found else None

def severity_outranks(new: Any, existing: Any) -> bool:
    """Issue #284: True when the new report's severity is higher (CRITICAL > HIGH > MEDIUM > LOW) than the open
    issue's. An unknown severity on either side is False: the hit deduplicates as before."""
    order = issue_telemetry.SEVERITIES
    new, existing = str(new or "").upper(), str(existing or "").upper()
    return new in order and existing in order and order.index(new) < order.index(existing)

def find_open_issue(fingerprint: str, repo: Optional[str],
                    timeout: float = DEDUPE_LOOKUP_TIMEOUT_S) -> Optional[Dict[str, Any]]:
    """Issue #270: {"number", "url", "severity"} of an OPEN issue of `repo` whose body carries `fingerprint` (the
    "Fingerprint ID" row of both reporters; severity: its `severity:*` label, issue #284, else None; with several
    matches the most severe one, unlabelled ones last), from one
    `gh issue list` call bounded by `timeout` seconds. None when there is none and on ANY failure (no gh,
    unauthenticated, offline, timeout, unexpected output): the caller creates or queues."""
    if not repo or not FINGERPRINT_RE.match(str(fingerprint or "")):
        return None
    try:
        if not shutil.which("gh"):
            return None
        res = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open", "--search", f"{fingerprint} in:body",
             "--json", "number,url,body,labels", "--limit", "10"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
        if res.returncode != 0:
            return None
        matches = [{"number": item.get("number"), "url": item.get("url"),
                    "severity": _issue_severity(item.get("labels"))}
                   for item in json.loads(res.stdout or "[]")
                   if isinstance(item, dict) and fingerprint in str(item.get("body") or "") and item.get("url")]
        if matches:
            # Issue #284: the most severe match (independent of gh's ordering); unlabelled ones rank last
            return min(matches, key=lambda m: issue_telemetry.SEVERITIES.index(m["severity"])
                       if m["severity"] else len(issue_telemetry.SEVERITIES))
    except Exception:
        return None
    return None

def _more_severe(a: Any, b: Any) -> Optional[str]:
    """The more severe of two severities (an unknown one is ignored); None when both are unknown."""
    return b if severity_outranks(b, a) or (a not in issue_telemetry.SEVERITIES) else a

def record_open_issue_hit(fingerprint: str, title: str, found: Dict[str, Any], now_ts: int,
                          wait_s: Optional[float] = None, severity: Optional[str] = None) -> int:
    """Counts a hit on an open issue in the local fingerprint store (locked); returns the occurrences. The record's
    severity (issue #284) is the more severe of the issue's label and the report's."""
    with file_lock.locked(FINGERPRINTS_FILE, wait_s):
        fp_cache = load_fingerprints()
        prev = fp_cache.get(fingerprint) if isinstance(fp_cache.get(fingerprint), dict) else {}
        occurrences = prev.get("count", 1) + 1
        fp_cache[fingerprint] = {
            "title": title,
            "issue_number": found.get("number"),
            "html_url": found.get("url"),
            "last_seen_ts": now_ts,
            "count": occurrences,
            "status": PUBLISHED_GITHUB,
            "severity": _more_severe(found.get("severity"), str(severity or "").upper() or None)
        }
        save_fingerprints(fp_cache)
    return occurrences

def append_to_backlog(issue_payload: Dict[str, Any]):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(BACKLOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(issue_payload, ensure_ascii=False) + "\n")

def get_system_context() -> Dict[str, Any]:
    """Collects runtime, git and ledger telemetry fail-safely without exposing any monetary amount."""
    ctx = issue_telemetry.collect_telemetry(BASE_DIR, LOGS_DIR)
    ctx["timestamp_utc"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ctx["portfolio_summary"] = (
        f"Delta: {ctx.get('delta_bias')}, Positions: {ctx.get('positions_count')}, "
        f"Floating Bias: {ctx.get('floating_pnl_sign')}"
    )
    return ctx

def format_issue_markdown(
    title: str,
    error_detail: str,
    category: str,
    severity: str,
    agent_name: str,
    stack_trace: str = "",
    remediation: str = "",
    fingerprint: str = "",
    priority: Optional[str] = None,
    repro: str = "",
    root_cause: str = "",
    affected_files: str = "",
    context: str = "",
    context_file: str = "",
    output_file: str = "",
    impact: str = "",
    acceptance_criteria: str = ""
) -> str:
    """Builds the six-section Markdown body (shared with report_issue.sh) with sanitized telemetry."""
    ctx = get_system_context()

    full_context = (context or "").strip()
    if context_file:
        file_text = issue_telemetry.read_capped(context_file)
        full_context = f"{full_context}\n\n{file_text}".strip() if full_context else file_text
    output_text = issue_telemetry.read_tail(output_file) if output_file else ""

    return issue_telemetry.render_issue_body(
        error_detail=error_detail,
        severity=severity,
        priority=priority,
        category=category,
        agent_name=agent_name,
        telemetry=ctx,
        repro=repro,
        output_text=output_text,
        stack_trace=stack_trace,
        affected_files=affected_files,
        root_cause=root_cause,
        context=full_context,
        impact=impact,
        remediation=remediation,
        acceptance_criteria=acceptance_criteria,
        fingerprint=fingerprint,
        timestamp_utc=ctx["timestamp_utc"],
        sanitize=sanitize_telemetry
    )

def _post_issue(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=12) as response:
        status = response.status
        try:
            return json.loads(response.read().decode("utf-8"))
        except (ValueError, OSError, http.client.HTTPException):
            # A 201 means the issue exists even if its body is unreadable: never let the caller queue a duplicate (#62)
            if status == 201:
                return {"number": None, "html_url": None, "labels": None, "_url_unknown": True}
            raise

def _ensure_labels(repo: str, issue_number: Any, missing: List[str]) -> bool:
    """Adds missing severity/priority labels via gh when available; warns loudly with the exact fix-up command."""
    if not missing:
        return True
    label_csv = ",".join(missing)
    fix_cmd = f"gh issue edit {issue_number} --repo {repo} --add-label {label_csv}"
    if issue_number and shutil.which("gh"):
        try:
            res = subprocess.run(
                ["gh", "issue", "edit", str(issue_number), "--repo", repo, "--add-label", label_csv],
                capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL
            )
            if res.returncode == 0:
                print(f"ℹ️ Labels {label_csv} added to issue #{issue_number} via gh.")
                return True
        except Exception:
            pass
    print(f"⚠️ WARNING: issue #{issue_number} was created WITHOUT its {label_csv} label(s). Apply them now:")
    print(f"   {fix_cmd}")
    return False

def dispatch_github_issue(
    title: str,
    body: str,
    labels: List[str],
    repo: str,
    token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Dispatches HTTP POST request to GitHub REST API.
    If the labelled create is rejected with HTTP 422 (unknown labels), retries once without labels
    and with a '[SEV/Px] ' title prefix; any other HTTP error is raised so the caller queues the report.
    Missing severity/priority labels are re-applied with gh when possible, otherwise a warning with the
    exact `gh issue edit` command is printed. A 201 whose body cannot be read returns url_unknown=True
    with a `gh issue list --search` hint instead of raising.
    """
    if not repo:
        raise ValueError("Target repository is not specified.")

    token = token or os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN is not configured in environment or .env")

    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Autonomous-Trading-Desk-Agent"
    }

    clean_title = sanitize_telemetry(title)
    payload = {
        "title": clean_title,
        "body": body,
        "labels": labels
    }

    try:
        res_data = _post_issue(url, headers, payload)
    except urllib.error.HTTPError as e:
        if not labels or e.code != 422:
            raise
        print(f"⚠️ GitHub rejected the labelled issue (HTTP {e.code}); retrying without labels...")
        res_data = _post_issue(url, headers, {
            "title": issue_telemetry.title_prefix_from_labels(labels) + clean_title,
            "body": body
        })

    if res_data.get("_url_unknown"):
        print("⚠️ WARNING: GitHub answered HTTP 201 (issue created) but its response could not be read; "
              "the report was NOT queued to avoid a duplicate. Find it and check its severity/priority labels with:")
        print(f"   gh issue list --repo {repo} --state all --search {shlex.quote(clean_title + ' in:title')}")
        return {
            "success": True,
            "issue_number": None,
            "html_url": None,
            "state": None,
            "labels_applied": False,
            "url_unknown": True
        }

    required = issue_telemetry.required_labels(labels)
    applied_names = {l.get("name") for l in (res_data.get("labels") or []) if isinstance(l, dict)}
    missing = [l for l in required if l not in applied_names]
    labels_applied = _ensure_labels(repo, res_data.get("number"), missing)

    return {
        "success": True,
        "issue_number": res_data.get("number"),
        "html_url": res_data.get("html_url"),
        "state": res_data.get("state"),
        "labels_applied": labels_applied
    }

def report_issue(
    title: str,
    error_detail: str,
    category: str = "agent_failure",
    severity: str = "HIGH",
    agent_name: str = "autonomous_agent",
    stack_trace: str = "",
    remediation: str = "",
    repo: Optional[str] = None,
    force_sync: bool = False,
    priority: Optional[str] = None,
    repro: str = "",
    root_cause: str = "",
    affected_files: str = "",
    context: str = "",
    context_file: str = "",
    output_file: str = "",
    impact: str = "",
    acceptance_criteria: str = "",
    lookup_timeout_s: float = INPROCESS_LOOKUP_TIMEOUT_S,
    lock_wait_s: Optional[float] = INPROCESS_LOCK_WAIT_S
) -> Dict[str, Any]:
    """
    Main exportable function for subagents and desk scripts to report failures.
    Applies deduplication, markdown formatting, telemetry sanitization,
    and fail-safe dispatch (online or local backlog).
    Severity/priority are case-insensitive; priority defaults from severity. Invalid values raise ValueError.
    lookup_timeout_s / lock_wait_s (issue #284): the open-issue lookup bound and each of the (up to 3)
    fingerprint-lock waits; the defaults bound in-process callers (3 s + 3 x 1 s), main() passes the CLI values
    (5 s, 2 s).
    """
    severity = issue_telemetry.normalize_severity(severity)
    priority = issue_telemetry.normalize_priority(priority, severity)
    category = issue_telemetry.normalize_category(category)
    for flag, path in (("--context-file", context_file), ("--output-file", output_file)):
        if path and issue_telemetry.is_credential_path(path):
            raise ValueError(_credential_refusal(path, flag))
    clean_title = sanitize_telemetry(title)
    target_repo = repo or derive_github_repo()

    fingerprint = compute_fingerprint(clean_title, error_detail)
    now_ts = int(time.time())

    # 24-hour deduplication control (locked read-modify-write, issue #270)
    occurrences = None
    if not force_sync:
        with file_lock.locked(FINGERPRINTS_FILE, lock_wait_s):
            fp_cache = load_fingerprints()
            entry = fp_cache.get(fingerprint)
            # Less than 24h, and (issue #284) never a report more severe than the recorded one (a record without
            # a severity, written before #284, deduplicates as before)
            if (isinstance(entry, dict) and now_ts - entry.get("last_seen_ts", 0) < 86400
                    and not severity_outranks(severity, entry.get("severity"))):
                occurrences = entry.get("count", 1) + 1
                entry["count"] = occurrences
                entry["last_seen_ts"] = now_ts
                save_fingerprints(fp_cache)
    if occurrences is None and not force_sync and target_repo and os.getenv("GITHUB_TOKEN"):
        # Issue #270: another session (or reporter) may already have an OPEN issue for this fingerprint
        found = find_open_issue(fingerprint, target_repo, timeout=lookup_timeout_s)
        if found and severity_outranks(severity, found.get("severity")):
            # Issue #284: a less severe open issue never swallows this report
            print(f"ℹ️ Open issue #{found.get('number')} carries fingerprint {fingerprint} at severity "
                  f"{found.get('severity')}, lower than {severity}: creating a new issue.")
        elif found:
            occurrences = record_open_issue_hit(fingerprint, clean_title, found, now_ts, wait_s=lock_wait_s,
                                                severity=severity)
            print(f"ℹ️ Open issue #{found.get('number')} ({found.get('url')}) already reports fingerprint {fingerprint}.")
    if occurrences is not None:
        msg = f"Deduplication active: Error '{clean_title}' was already reported previously (Occurrences: {occurrences}). Omitting duplicate issue."
        print(f"ℹ️ {msg}")
        return {
            "success": True,
            "deduplicated": True,
            "fingerprint": fingerprint,
            "occurrences": occurrences,
            "message": msg
        }

    # Format GitHub labels (severity + priority are mandatory)
    labels = issue_telemetry.build_labels(severity, priority, category)

    markdown_body = format_issue_markdown(
        title=clean_title,
        error_detail=error_detail,
        category=category,
        severity=severity,
        agent_name=agent_name,
        stack_trace=stack_trace,
        remediation=remediation,
        fingerprint=fingerprint,
        priority=priority,
        repro=repro,
        root_cause=root_cause,
        affected_files=affected_files,
        context=context,
        context_file=context_file,
        output_file=output_file,
        impact=impact,
        acceptance_criteria=acceptance_criteria
    )

    issue_record = {
        "fingerprint": fingerprint,
        "title": clean_title,
        "body": markdown_body,
        "labels": labels,
        "repo": target_repo or "local_backlog",
        "category": category,
        "severity": severity,
        "priority": priority,
        "agent_name": agent_name,
        "created_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": "QUEUED_OFFLINE"
    }

    # Safe Queue Check: if no repo is resolvable, queue in backlog without attempting dispatch
    if not target_repo:
        print("ℹ️ GITHUB_REPO not configured or detectable from git remote. Enqueueing issue in local backlog (logs/issues_backlog.jsonl)...")
        append_to_backlog(issue_record)
        set_fingerprint(fingerprint, {
            "title": clean_title,
            "last_seen_ts": now_ts,
            "count": 1,
            "status": "QUEUED_OFFLINE",
            "severity": severity
        }, wait_s=lock_wait_s)
        return issue_record

    token = os.getenv("GITHUB_TOKEN")
    if token:
        try:
            gh_res = dispatch_github_issue(title=clean_title, body=markdown_body, labels=labels, repo=target_repo, token=token)
            status = PUBLISHED_UNKNOWN_URL if gh_res.get("url_unknown") else PUBLISHED_GITHUB
            issue_record["status"] = status
            issue_record["issue_number"] = gh_res.get("issue_number")
            issue_record["html_url"] = gh_res.get("html_url")
            issue_record["labels_applied"] = gh_res.get("labels_applied")

            # Record fingerprint (the status lets --sync-backlog skip already-published reports)
            set_fingerprint(fingerprint, {
                "title": clean_title,
                "issue_number": gh_res.get("issue_number"),
                "html_url": gh_res.get("html_url"),
                "last_seen_ts": now_ts,
                "count": 1,
                "status": status,
                "severity": severity
            }, wait_s=lock_wait_s)

            if gh_res.get("url_unknown"):
                print("✅ GITHUB ISSUE CREATED (URL unknown: see the warning above)")
            else:
                print(f"✅ GITHUB ISSUE CREATED SUCCESSFULLY: #{gh_res.get('issue_number')}")
                print(f"   URL: {gh_res.get('html_url')}")
            return issue_record
        except Exception as e:
            print(f"⚠️ Failed to connect to GitHub API ({e}). Saving issue to local backlog...", file=sys.stderr)
            issue_record["dispatch_error"] = str(e)
    else:
        print(f"ℹ️ GITHUB_TOKEN not detected. Enqueueing issue #{fingerprint[:8]} in local backlog (logs/issues_backlog.jsonl)...")

    # If no token or dispatch failed, enqueue in backlog
    append_to_backlog(issue_record)
    set_fingerprint(fingerprint, {
        "title": clean_title,
        "last_seen_ts": now_ts,
        "count": 1,
        "status": "QUEUED_OFFLINE",
        "severity": severity
    }, wait_s=lock_wait_s)
    return issue_record

def _fingerprint_published(entry: Any) -> bool:
    """True for a fingerprint record of a published issue (legacy records: no status but an issue number)."""
    if not isinstance(entry, dict):
        return False
    if entry.get("status") in PUBLISHED_STATUSES:
        return True
    return "status" not in entry and bool(entry.get("issue_number"))

def sync_backlog(repo: Optional[str] = None):
    """Retries dispatch of all pending offline backlog issues."""
    target_repo = repo or derive_github_repo()
    if not target_repo:
        print("❌ Error: GITHUB_REPO is not configured and cannot be derived from git remote. Cannot sync.")
        return

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("❌ Error: GITHUB_TOKEN is not defined in environment or .env. Cannot sync.")
        return

    if not os.path.exists(BACKLOG_FILE):
        print("✅ Backlog is empty. No issues pending synchronization.")
        return

    lines = []
    with open(BACKLOG_FILE, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        print("✅ No entries found in backlog.")
        return

    print(f"🔄 Syncing {len(lines)} pending issue(s) to https://github.com/{target_repo}/issues...")
    fp_cache = load_fingerprints()
    fp_dirty = set()
    remaining = []
    success_count = 0
    skipped_count = 0

    for line in lines:
        item = {}
        try:
            item = json.loads(line)
            # Entries without a fingerprint (legacy, bash-written) are always dispatched
            fp = item.get("fingerprint")
            if fp and _fingerprint_published(fp_cache.get(fp)):
                print(f"   ⏭️ Skipped '{item.get('title')}': fingerprint {fp} is already published; dropped from the backlog.")
                skipped_count += 1
                continue
            res = dispatch_github_issue(
                title=item["title"],
                body=item["body"],
                # Legacy entries (queued before priority labels existed) get the default priority from severity
                labels=issue_telemetry.with_default_priority(item.get("labels", [])),
                repo=target_repo,
                token=token
            )
            if res.get("url_unknown"):
                print(f"   ✅ Issue published (URL unknown): {item['title']}")
            else:
                print(f"   ✅ Issue #{res.get('issue_number')} published: {item['title']} -> {res.get('html_url')}")
            success_count += 1
            if fp:
                # Mark it published so a later duplicate entry (same sync or a later one) is skipped
                entry = fp_cache.get(fp) if isinstance(fp_cache.get(fp), dict) else {}
                entry.update({
                    "title": item["title"],
                    "issue_number": res.get("issue_number"),
                    "html_url": res.get("html_url"),
                    "last_seen_ts": int(time.time()),
                    "status": PUBLISHED_UNKNOWN_URL if res.get("url_unknown") else PUBLISHED_GITHUB,
                    "count": entry.get("count", 1),
                })
                fp_cache[fp] = entry
                fp_dirty.add(fp)
            time.sleep(1)  # Rate limit cushion
        except Exception as e:
            print(f"   ❌ Failed to dispatch '{item.get('title')}': {e}")
            remaining.append(line)

    if fp_dirty:
        # Merge into the current file under the lock: other reporters may have written meanwhile (issue #270)
        with file_lock.locked(FINGERPRINTS_FILE):
            current = load_fingerprints()
            current.update({fp: fp_cache[fp] for fp in fp_dirty})
            save_fingerprints(current)

    # Rewrite backlog only with remaining failures
    with open(BACKLOG_FILE, "w", encoding="utf-8") as f:
        for r in remaining:
            f.write(r + "\n")

    print(f"🏁 Sync completed: {success_count} published, {skipped_count} skipped (already published), "
          f"{len(remaining)} remaining in backlog.")

def _severity_arg(value: str) -> str:
    try:
        return issue_telemetry.normalize_severity(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))

def _category_arg(value: str) -> str:
    try:
        return issue_telemetry.normalize_category(value, required=True)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))

def _credential_refusal(path: str, flag: str) -> str:
    return (f"refusing to attach '{path}' ({flag}): credential-bearing file ({issue_telemetry.CREDENTIAL_PATH_HINT}). "
            "Copy only the relevant non-secret lines into logs/issue_output_<unix_ts>.log and attach that file instead.")

def _attachment_arg(flag: str):
    def check(value: str) -> str:
        if issue_telemetry.is_credential_path(value):
            raise argparse.ArgumentTypeError(_credential_refusal(value, flag))
        return value
    return check

def _priority_arg(value: str) -> str:
    if not str(value).strip():
        return ""
    try:
        return issue_telemetry.normalize_priority(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))

def main():
    parser = argparse.ArgumentParser(description="Autonomous GitHub Issue Reporter for Agent Failures")
    parser.add_argument("--title", type=str, help="Descriptive title of the failure or anomaly")
    parser.add_argument("--error", type=str, help="Detailed description of the failure or anomaly")
    parser.add_argument("--category", type=_category_arg, default="agent_failure", choices=["agent_failure", "risk_gate", "tool_error", "quant_logic", "infra", "enhancement"], help="Issue category (spaces/dashes become '_')")
    parser.add_argument("--severity", type=_severity_arg, default="HIGH", help="CRITICAL | HIGH | MEDIUM | LOW (case-insensitive, default: HIGH)")
    parser.add_argument("--priority", type=_priority_arg, default=None, help="P0 | P1 | P2 | P3 (default from severity: CRITICAL->P0, HIGH->P1, MEDIUM->P2, LOW->P3)")
    parser.add_argument("--agent", type=str, default="cli_operator", help="Reporting subagent or module name")
    parser.add_argument("--stack-trace", type=str, default="", help="Traceback or technical error payload")
    parser.add_argument("--remediation", type=str, default="", help="Suggested fix or proposed remediation")
    parser.add_argument("--repro", type=str, default="", help="Exact reproduction command and its exit code")
    parser.add_argument("--root-cause", type=str, default="", help="Suspected or confirmed root cause")
    parser.add_argument("--affected-files", type=str, default="", help="Comma-separated code pointers (path:lines, ...)")
    parser.add_argument("--context", type=str, default="", help="Narrative context: what the agent was doing and what it observed")
    parser.add_argument("--context-file", type=_attachment_arg("--context-file"), default="", help="File whose content is appended to --context (first 8000 chars; credential files such as .env/*.env/MCP configs are refused: exit 2)")
    parser.add_argument("--output-file", type=_attachment_arg("--output-file"), default="", help="Raw command/agent output; the last 200 lines (max 12000 chars) are attached (credential files such as .env/*.env/MCP configs are refused: exit 2)")
    parser.add_argument("--impact", type=str, default="", help="Operational impact on the trading desk (default derived from category)")
    parser.add_argument("--acceptance-criteria", type=str, default="", help="Acceptance criteria, one per line or ';'-separated")
    parser.add_argument("--repo", type=str, default=None, help="Target repository (e.g. owner/repo). If omitted, derived from GITHUB_REPO or git remote origin.")
    parser.add_argument("--sync-backlog", action="store_true", help="Sync queued issues in logs/issues_backlog.jsonl to GitHub")
    parser.add_argument("--force", action="store_true", help="Bypass 24h deduplication and force issue creation")
    parser.add_argument("--find-open-issue", action="store_true",
                        help="Issue #270 (used by report_issue.sh): print 'fingerprint<TAB><fp>' for --title/--error "
                             "and, only with --repo, 'open_issue<TAB><url>' when an OPEN issue carries it and is not "
                             "less severe than --severity (issue #284; the hit is then counted in the local "
                             "fingerprint store). Bounded gh lookup; any failure prints nothing more. Never creates "
                             "or queues an issue.")

    args = parser.parse_args()

    if args.sync_backlog:
        sync_backlog(repo=args.repo)
        return

    if args.find_open_issue:
        if not args.title or not args.error:
            parser.print_help()
            sys.exit(1)
        clean_title = sanitize_telemetry(args.title)
        fingerprint = compute_fingerprint(clean_title, args.error)
        print(f"fingerprint\t{fingerprint}")
        found = find_open_issue(fingerprint, args.repo) if args.repo else None
        if found and not severity_outranks(args.severity, found.get("severity")):
            try:  # issue #284: a shell dedupe hit is counted locally too (best effort: the dedupe stands)
                record_open_issue_hit(fingerprint, clean_title, found, int(time.time()), severity=args.severity)
            except Exception:
                pass
            print(f"open_issue\t{found['url']}")
        return

    if not args.title or not args.error:
        parser.print_help()
        sys.exit(1)

    res = report_issue(
        title=args.title,
        error_detail=args.error,
        category=args.category,
        severity=args.severity,
        agent_name=args.agent,
        stack_trace=args.stack_trace,
        remediation=args.remediation,
        repo=args.repo,
        force_sync=args.force,
        priority=args.priority or None,
        repro=args.repro,
        root_cause=args.root_cause,
        affected_files=args.affected_files,
        context=args.context,
        context_file=args.context_file,
        output_file=args.output_file,
        impact=args.impact,
        acceptance_criteria=args.acceptance_criteria,
        lookup_timeout_s=DEDUPE_LOOKUP_TIMEOUT_S,  # issue #284: the CLI keeps 5 s / 2 s
        lock_wait_s=file_lock.LOCK_WAIT_S
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
