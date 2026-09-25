#!/usr/bin/env python3
"""
report_agent_issue.py - Autonomous GitHub Issue Generator for Agentic Setup Failures.

Enables subagents, hooks, and trading desk loops to programmatically report unhandled exceptions,
infrastructure anomalies, or tool errors directly to the repository:
https://github.com/IgnacioN99/autonomous-trading-desk/issues.

Resilience Features:
1. Direct GitHub Dispatch: Uses GitHub REST API v3 with GITHUB_TOKEN (Personal Access Token).
2. Resilient Offline Backlog: If GITHUB_TOKEN is not configured or network fails,
   atomically enqueues the issue in logs/issues_backlog.jsonl for later sync via --sync-backlog.
3. Intelligent Deduplication / Anti-Spam: Computes a SHA-256 fingerprint; if the same failure
   occurred within the past 24 hours, updates telemetry rather than spamming new issues.
4. Automatic Forensic Telemetry: Captures UTC timestamp, environment (Testnet/Prod), reporting agent,
   Python version, and portfolio exposure state.

Usage:
  python3 scripts/report_agent_issue.py --title "Cointegration Calculation Failure" --error "ZeroDivisionError: ..." --category "quant_logic" --severity "HIGH"
  python3 scripts/report_agent_issue.py --sync-backlog
"""

import os
import sys
import json
import time
import hashlib
import datetime
import urllib.request
import urllib.error
import argparse
from typing import Dict, Any, Optional, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
BACKLOG_FILE = os.path.join(LOGS_DIR, "issues_backlog.jsonl")
FINGERPRINTS_FILE = os.path.join(LOGS_DIR, "issues_fingerprints.json")
DEFAULT_REPO = "IgnacioN99/autonomous-trading-desk"

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
                    if key and key not in os.environ:
                        os.environ[key] = val

load_env_file()

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
    os.makedirs(LOGS_DIR, exist_ok=True)
    temp_file = FINGERPRINTS_FILE + f".tmp.{os.getpid()}"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp_file, FINGERPRINTS_FILE)

def append_to_backlog(issue_payload: Dict[str, Any]):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(BACKLOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(issue_payload, ensure_ascii=False) + "\n")

def get_system_context() -> Dict[str, Any]:
    """Collects lightweight system and portfolio telemetry fail-safely."""
    state_file = os.path.join(LOGS_DIR, "session_state.json")
    portfolio_summary = "FLAT / No state"
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
                portfolio_summary = f"Delta: {st.get('delta_bias', 'N/A')}, Positions: {len(st.get('active_positions', []))}, Floating PnL: ${st.get('floating_pnl_usdt', 0.0):.2f}"
        except Exception:
            pass

    return {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "target_env": os.getenv("BINANCE_API_ENV", "TESTNET").upper(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "portfolio_summary": portfolio_summary
    }

def format_issue_markdown(
    title: str,
    error_detail: str,
    category: str,
    severity: str,
    agent_name: str,
    stack_trace: str = "",
    remediation: str = "",
    fingerprint: str = ""
) -> str:
    """Builds an institutional Markdown body for GitHub issues."""
    ctx = get_system_context()
    
    severity_emojis = {
        "CRITICAL": "🔴 CRITICAL",
        "HIGH": "🟠 HIGH",
        "MEDIUM": "🟡 MEDIUM",
        "LOW": "🔵 LOW"
    }
    sev_badge = severity_emojis.get(severity.upper(), f"⚪ {severity}")

    body = [
        f"## 🚨 Autonomous Agent Setup Failure Report",
        f"",
        f"| Dimension | Value |",
        f"| :--- | :--- |",
        f"| **Severity** | **{sev_badge}** |",
        f"| **Category** | `{category}` |",
        f"| **Reporting Agent / Module** | `{agent_name}` |",
        f"| **Execution Environment** | `{ctx['target_env']}` |",
        f"| **Timestamp UTC** | `{ctx['timestamp_utc']}` |",
        f"| **Portfolio State** | `{ctx['portfolio_summary']}` |",
        f"| **Fingerprint ID** | `{fingerprint}` |",
        f"",
        f"---",
        f"",
        f"### 📋 Failure / Anomaly Description",
        f"{error_detail.strip()}",
        f""
    ]

    if stack_trace.strip():
        body.extend([
            f"### 🔍 Traceback / Technical Error Details",
            f"```text",
            f"{stack_trace.strip()}",
            f"```",
            f""
        ])

    if remediation.strip():
        body.extend([
            f"### 💡 Suggested Remediation",
            f"{remediation.strip()}",
            f""
        ])

    body.extend([
        f"---",
        f"*Reported automatically by the `autonomous-trading-desk` observability harness.*"
    ])

    return "\n".join(body)

def dispatch_github_issue(
    title: str,
    body: str,
    labels: List[str],
    repo: str = DEFAULT_REPO,
    token: Optional[str] = None
) -> Dict[str, Any]:
    """Dispatches HTTP POST request to GitHub REST API."""
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

    payload = {
        "title": title,
        "body": body,
        "labels": labels
    }

    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=12) as response:
        res_data = json.loads(response.read().decode("utf-8"))
        return {
            "success": True,
            "issue_number": res_data.get("number"),
            "html_url": res_data.get("html_url"),
            "state": res_data.get("state")
        }

def report_issue(
    title: str,
    error_detail: str,
    category: str = "agent_failure",
    severity: str = "HIGH",
    agent_name: str = "autonomous_agent",
    stack_trace: str = "",
    remediation: str = "",
    repo: str = DEFAULT_REPO,
    force_sync: bool = False
) -> Dict[str, Any]:
    """
    Main exportable function for subagents and desk scripts to report failures.
    Applies deduplication, markdown formatting, and fail-safe dispatch (online or local backlog).
    """
    fingerprint = compute_fingerprint(title, error_detail)
    fp_cache = load_fingerprints()
    now_ts = int(time.time())

    # 24-hour deduplication control
    if not force_sync and fingerprint in fp_cache:
        last_seen = fp_cache[fingerprint].get("last_seen_ts", 0)
        count = fp_cache[fingerprint].get("count", 1)
        if now_ts - last_seen < 86400: # Less than 24h
            fp_cache[fingerprint]["count"] = count + 1
            fp_cache[fingerprint]["last_seen_ts"] = now_ts
            save_fingerprints(fp_cache)
            msg = f"Deduplication active: Error '{title}' was already reported previously (Occurrences: {count + 1}). Omitting duplicate issue."
            print(f"ℹ️ {msg}")
            return {
                "success": True,
                "deduplicated": True,
                "fingerprint": fingerprint,
                "occurrences": count + 1,
                "message": msg
            }

    # Format GitHub labels
    labels = ["agent-failure", f"severity:{severity.lower()}"]
    if category:
        labels.append(f"cat:{category.lower()}")

    markdown_body = format_issue_markdown(
        title=title,
        error_detail=error_detail,
        category=category,
        severity=severity,
        agent_name=agent_name,
        stack_trace=stack_trace,
        remediation=remediation,
        fingerprint=fingerprint
    )

    issue_record = {
        "fingerprint": fingerprint,
        "title": title,
        "body": markdown_body,
        "labels": labels,
        "repo": repo,
        "category": category,
        "severity": severity,
        "agent_name": agent_name,
        "created_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": "QUEUED_OFFLINE"
    }

    token = os.getenv("GITHUB_TOKEN")
    if token:
        try:
            gh_res = dispatch_github_issue(title=title, body=markdown_body, labels=labels, repo=repo, token=token)
            issue_record["status"] = "PUBLISHED_GITHUB"
            issue_record["issue_number"] = gh_res.get("issue_number")
            issue_record["html_url"] = gh_res.get("html_url")
            
            # Record fingerprint
            fp_cache[fingerprint] = {
                "title": title,
                "issue_number": gh_res.get("issue_number"),
                "html_url": gh_res.get("html_url"),
                "last_seen_ts": now_ts,
                "count": 1
            }
            save_fingerprints(fp_cache)
            
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
    fp_cache[fingerprint] = {
        "title": title,
        "last_seen_ts": now_ts,
        "count": 1,
        "status": "QUEUED_OFFLINE"
    }
    save_fingerprints(fp_cache)
    return issue_record

def sync_backlog(repo: str = DEFAULT_REPO):
    """Retries dispatch of all pending offline backlog issues."""
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

    print(f"🔄 Syncing {len(lines)} pending issue(s) to https://github.com/{repo}/issues...")
    remaining = []
    success_count = 0

    for line in lines:
        try:
            item = json.loads(line)
            res = dispatch_github_issue(
                title=item["title"],
                body=item["body"],
                labels=item.get("labels", []),
                repo=repo,
                token=token
            )
            print(f"   ✅ Issue #{res.get('issue_number')} published: {item['title']} -> {res.get('html_url')}")
            success_count += 1
            time.sleep(1) # Rate limit cushion
        except Exception as e:
            print(f"   ❌ Failed to dispatch '{item.get('title')}': {e}")
            remaining.append(line)

    # Rewrite backlog only with remaining failures
    with open(BACKLOG_FILE, "w", encoding="utf-8") as f:
        for r in remaining:
            f.write(r + "\n")

    print(f"🏁 Sync completed: {success_count} published, {len(remaining)} remaining in backlog.")

def main():
    parser = argparse.ArgumentParser(description="Autonomous GitHub Issue Reporter for Agent Failures")
    parser.add_argument("--title", type=str, help="Descriptive title of the failure or anomaly")
    parser.add_argument("--error", type=str, help="Detailed description of the failure or anomaly")
    parser.add_argument("--category", type=str, default="agent_failure", choices=["agent_failure", "risk_gate", "tool_error", "quant_logic", "infra", "enhancement"], help="Issue category")
    parser.add_argument("--severity", type=str, default="HIGH", choices=["CRITICAL", "HIGH", "MEDIUM", "LOW"], help="Severity level")
    parser.add_argument("--agent", type=str, default="cli_operator", help="Reporting subagent or module name")
    parser.add_argument("--stack-trace", type=str, default="", help="Traceback or technical error payload")
    parser.add_argument("--remediation", type=str, default="", help="Suggested fix or proposed remediation")
    parser.add_argument("--repo", type=str, default=DEFAULT_REPO, help="Target repository (e.g. IgnacioN99/autonomous-trading-desk)")
    parser.add_argument("--sync-backlog", action="store_true", help="Sync queued issues in logs/issues_backlog.jsonl to GitHub")
    parser.add_argument("--force", action="store_true", help="Bypass 24h deduplication and force issue creation")

    args = parser.parse_args()

    if args.sync_backlog:
        sync_backlog(repo=args.repo)
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
        force_sync=args.force
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
