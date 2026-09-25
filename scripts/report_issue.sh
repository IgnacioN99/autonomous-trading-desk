#!/usr/bin/env bash
# ==============================================================================
# report_issue.sh - Native Bash GitHub Issue Reporter for Autonomous Agents
# ==============================================================================
# Designed to be invoked directly from the agent shell (run_command)
# with zero Python dependencies (requires only bash and curl).
#
# If Python or the virtual environment crashes, this script continues functioning
# to report the incident directly to GitHub.
#
# Usage:
#   ./scripts/report_issue.sh --title "Binance API Failure" --error "Error 429 Too Many Requests" --severity "HIGH"
#   ./scripts/report_issue.sh --sync
# ==============================================================================

set -e

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS_DIR="${BASE_DIR}/logs"
BACKLOG_FILE="${LOGS_DIR}/issues_backlog.jsonl"
DEFAULT_REPO="IgnacioN99/autonomous-trading-desk"

# 1. Load variables from .env if present and not exported
if [ -f "${BASE_DIR}/.env" ]; then
    while IFS='=' read -r key val || [ -n "$key" ]; do
        [[ "$key" =~ ^#.*$ ]] && continue
        [ -z "$key" ] && continue
        key="$(echo "$key" | tr -d '[:space:]')"
        val="$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//')"
        if [ -n "$key" ] && [ -z "${!key}" ]; then
            export "$key"="$val"
        fi
    done < "${BASE_DIR}/.env"
fi

REPO="${GITHUB_REPO:-$DEFAULT_REPO}"
TOKEN="${GITHUB_TOKEN:-}"

# Default parameters
TITLE=""
ERROR_DETAIL=""
SEVERITY="HIGH"
CATEGORY="agent_failure"
AGENT_NAME="autonomous_agent"
REMEDIATION=""
SYNC_MODE=false

# Argument parser
while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--title)
            TITLE="$2"
            shift 2
            ;;
        -e|--error|-b|--body)
            ERROR_DETAIL="$2"
            shift 2
            ;;
        -s|--severity)
            SEVERITY="$2"
            shift 2
            ;;
        -c|--category)
            CATEGORY="$2"
            shift 2
            ;;
        -a|--agent)
            AGENT_NAME="$2"
            shift 2
            ;;
        -r|--remediation)
            REMEDIATION="$2"
            shift 2
            ;;
        --repo)
            REPO="$2"
            shift 2
            ;;
        --sync)
            SYNC_MODE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  -t, --title <text>        Issue title describing failure"
            echo "  -e, --error <text>        Error details or returned exception message"
            echo "  -s, --severity <level>    CRITICAL | HIGH | MEDIUM | LOW (default: HIGH)"
            echo "  -c, --category <type>     agent_failure | risk_gate | tool_error | infra (default: agent_failure)"
            echo "  -a, --agent <name>        Reporting agent name (default: autonomous_agent)"
            echo "  -r, --remediation <text>  Suggested fix or remediation step"
            echo "  --sync                    Dispatches pending offline backlog issues"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

mkdir -p "$LOGS_DIR"

# ------------------------------------------------------------------------------
# Function: Offline Backlog Sync
# ------------------------------------------------------------------------------
sync_backlog() {
    if [ -z "$TOKEN" ]; then
        echo "❌ Error: GITHUB_TOKEN is not defined in environment or .env."
        exit 1
    fi
    if [ ! -f "$BACKLOG_FILE" ] || [ ! -s "$BACKLOG_FILE" ]; then
        echo "✅ Offline backlog is empty. No pending issues."
        exit 0
    fi

    echo "🔄 Syncing pending backlog issues to https://github.com/${REPO}/issues..."
    TEMP_BACKLOG="${BACKLOG_FILE}.tmp.$$"
    touch "$TEMP_BACKLOG"

    count_success=0
    count_failed=0

    while IFS= read -r line || [ -n "$line" ]; do
        [ -z "$line" ] && continue
        
        item_title=$(echo "$line" | sed -n 's/.*"title": *\([^,]*\),.*/\1/p' | sed 's/^"//;s/"$//')
        
        http_code=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST "https://api.github.com/repos/${REPO}/issues" \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Accept: application/vnd.github+json" \
            -H "User-Agent: Autonomous-Trading-Desk-Bash" \
            -d "$line")

        if [ "$http_code" = "201" ]; then
            echo "   ✅ Successfully published: ${item_title}"
            ((count_success++))
        else
            echo "   ❌ HTTP $http_code error publishing: ${item_title}"
            echo "$line" >> "$TEMP_BACKLOG"
            ((count_failed++))
        fi
        sleep 1
    done < "$BACKLOG_FILE"

    mv "$TEMP_BACKLOG" "$BACKLOG_FILE"
    echo "🏁 Backlog sync complete: $count_success published, $count_failed remaining."
    exit 0
}

if [ "$SYNC_MODE" = true ]; then
    sync_backlog
fi

# ------------------------------------------------------------------------------
# Input Validation
# ------------------------------------------------------------------------------
if [ -z "$TITLE" ] || [ -z "$ERROR_DETAIL" ]; then
    echo "❌ Error: Missing required arguments (--title and --error)."
    echo "Example: $0 --title 'Binance API Failure' --error 'HTTP 502 Bad Gateway'"
    exit 1
fi

TIMESTAMP_UTC=$(date -u +"%Y-%m-%d %H:%M:%S UTC")
TARGET_ENV="${BINANCE_API_ENV:-TESTNET}"

case "${SEVERITY^^}" in
    CRITICAL) SEV_BADGE="🔴 CRITICAL" ;;
    HIGH)     SEV_BADGE="🟠 HIGH" ;;
    MEDIUM)   SEV_BADGE="🟡 MEDIUM" ;;
    LOW)      SEV_BADGE="🔵 LOW" ;;
    *)        SEV_BADGE="⚪ $SEVERITY" ;;
esac

# ------------------------------------------------------------------------------
# Markdown Body Construction
# ------------------------------------------------------------------------------
MD_BODY=$(cat <<EOF
## 🚨 Autonomous Agent Setup Failure Report

| Dimension | Value |
| :--- | :--- |
| **Severity** | **${SEV_BADGE}** |
| **Category** | \`${CATEGORY}\` |
| **Reporting Agent** | \`${AGENT_NAME}\` |
| **Environment** | \`${TARGET_ENV}\` |
| **Timestamp UTC** | \`${TIMESTAMP_UTC}\` |

---

### 📋 Failure / Anomaly Description
${ERROR_DETAIL}
EOF
)

if [ -n "$REMEDIATION" ]; then
    MD_BODY="${MD_BODY}

### 💡 Suggested Remediation
${REMEDIATION}"
fi

MD_BODY="${MD_BODY}

---
*Reported natively via bash shell by the \`autonomous-trading-desk\` observability harness.*"

# ------------------------------------------------------------------------------
# JSON Payload Serialization
# ------------------------------------------------------------------------------
json_escape() {
    python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || \
    echo -n "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a' -e 'N' -e '$!ba' -e 's/\n/\\n/g'
}

JSON_TITLE=$(echo -n "$TITLE" | json_escape 2>/dev/null || echo "\"$TITLE\"")
JSON_BODY=$(echo -n "$MD_BODY" | json_escape 2>/dev/null || echo "\"$MD_BODY\"")

PAYLOAD=$(cat <<EOF
{
  "title": ${JSON_TITLE},
  "body": ${JSON_BODY},
  "labels": ["agent-failure", "severity:${SEVERITY,,}", "cat:${CATEGORY,,}"]
}
EOF
)

# ------------------------------------------------------------------------------
# Dispatch to GitHub API or Enqueue in Local Backlog
# ------------------------------------------------------------------------------
if [ -n "$TOKEN" ]; then
    HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST "https://api.github.com/repos/${REPO}/issues" \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Accept: application/vnd.github+json" \
        -H "User-Agent: Autonomous-Trading-Desk-Bash" \
        -d "$PAYLOAD")

    HTTP_STATUS=$(echo "$HTTP_RESPONSE" | tail -n1)
    RESPONSE_BODY=$(echo "$HTTP_RESPONSE" | sed '$d')

    if [ "$HTTP_STATUS" = "201" ]; then
        ISSUE_URL=$(echo "$RESPONSE_BODY" | grep -o '"html_url": *"[^"]*"' | head -n1 | cut -d'"' -f4)
        ISSUE_NUM=$(echo "$RESPONSE_BODY" | grep -o '"number": *[0-9]*' | head -n1 | cut -d':' -f2 | tr -d ' ')
        echo "✅ GITHUB ISSUE CREATED SUCCESSFULLY: #${ISSUE_NUM}"
        echo "   URL: ${ISSUE_URL}"
        exit 0
    else
        echo "⚠️ Failed to connect to GitHub API (HTTP ${HTTP_STATUS}). Enqueueing in local backlog..."
    fi
else
    echo "ℹ️ GITHUB_TOKEN not detected in .env. Enqueueing issue in local backlog (${BACKLOG_FILE})..."
fi

# Fallback: persist in local backlog
echo "$PAYLOAD" | tr '\n' ' ' >> "$BACKLOG_FILE"
echo "" >> "$BACKLOG_FILE"
echo "📁 Issue saved in local backlog (${BACKLOG_FILE})."
echo "   To publish once GITHUB_TOKEN is configured: ./scripts/report_issue.sh --sync"
