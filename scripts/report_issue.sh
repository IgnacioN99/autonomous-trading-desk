#!/usr/bin/env bash
# ==============================================================================
# report_issue.sh - Native Bash GitHub Issue Reporter for Autonomous Agents
# ==============================================================================
# Designed to be invoked directly from the agent shell (run_command)
# Transport: the authenticated GitHub CLI (gh); curl + GITHUB_TOKEN is a fallback for environments without gh.
#
# If Python or the virtual environment crashes, this script continues functioning
# to report the incident directly to GitHub or safely enqueue in local backlog.
# Telemetry comes from scripts/utils/issue_telemetry.py when python3 + the helper are available,
# otherwise from a bash-only fallback (git + BINANCE_API_ENV; ledger "unavailable").
#
# Issue #270: the body carries the same "Fingerprint ID" as report_agent_issue.py; before creating, an OPEN issue
# with that fingerprint is looked up (report_agent_issue.py --find-open-issue, bounded) and, if found, reported
# instead of a duplicate. Offline or on any lookup failure the report is created or queued as before.
# Issue #284: a hit is also counted in the local fingerprint store; an open issue LESS severe than this report
# (its severity:* label) never swallows it. --sync does no lookup.
#
# Every issue carries the mandatory labels severity:<level> and priority:<Px>
# (priority defaults from severity: CRITICAL->P0, HIGH->P1, MEDIUM->P2, LOW->P3).
#
# Usage:
#   ./scripts/report_issue.sh --title "sync_session_state: ledger sync failed" --error "exit 1: HTTP 502" \
#       --severity HIGH --category tool_error \
#       --repro "python3 scripts/sync_session_state.py (exit 1)" \
#       --root-cause "positionRisk returned an error dict" --affected-files "scripts/sync_session_state.py:75-120" \
#       --context "Agent was refreshing the ledger before a scan" --output-file logs/last_run.txt \
#       --acceptance-criteria "Sync retries transient 5xx; regression test in tests/"
#   ./scripts/report_issue.sh --title "Binance API Failure" --error "Error 429 Too Many Requests" --severity MEDIUM --priority P1
#   ./scripts/report_issue.sh --sync
# ==============================================================================

set -e

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS_DIR="${ISSUE_REPORTER_LOGS_DIR:-${BASE_DIR}/logs}"
BACKLOG_FILE="${LOGS_DIR}/issues_backlog.jsonl"
TELEMETRY_HELPER="${BASE_DIR}/scripts/utils/issue_telemetry.py"

derive_repo() {
    if [ -n "${GITHUB_REPO:-}" ]; then
        echo "$GITHUB_REPO"
        return
    fi
    if command -v git >/dev/null 2>&1; then
        local remote_url
        remote_url=$(git -C "$BASE_DIR" config --get remote.origin.url 2>/dev/null || true)
        if [ -n "$remote_url" ]; then
            if echo "$remote_url" | grep -qi "github.com"; then
                local parsed
                parsed=$(echo "$remote_url" | sed -E -e 's#(https?://[^/]+/|git@[^:]+:)##' -e 's#\.git$##')
                if [ -n "$parsed" ]; then
                    echo "$parsed"
                    return
                fi
            fi
        fi
    fi
    echo ""
}

# 1. Load variables from .env if present and not exported. Only the reporter's own keys are loaded (GitHub
#    credentials / repository, ISSUE_REPORTER_* and BINANCE_API_ENV for telemetry): exporting arbitrary keys would
#    hand command channels (GIT_CONFIG_* -> core.fsmonitor, BASH_ENV, PAGER ...) to the git / gh / python3
#    commands this script runs. GH_* (incl. GH_TOKEN) is no longer loaded from .env (GH_PAGER / GH_EDITOR /
#    GH_BROWSER run commands): use GITHUB_TOKEN or `gh auth login`.
if [ -f "${BASE_DIR}/.env" ]; then
    while IFS='=' read -r key val || [ -n "$key" ]; do
        [[ "$key" =~ ^#.*$ ]] && continue
        [ -z "$key" ] && continue
        key="$(echo "$key" | tr -d '[:space:]')"
        case "$key" in
            GITHUB_*|ISSUE_REPORTER_*|BINANCE_API_ENV) ;;
            *) continue ;;
        esac
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        val="$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//')"
        if [ -n "$key" ] && [ -z "${!key}" ]; then
            export "$key"="$val"
        fi
    done < "${BASE_DIR}/.env"
fi

REPO="${GITHUB_REPO:-$(derive_repo)}"
TOKEN="${GITHUB_TOKEN:-}"

# Primary transport: the GitHub CLI with an authenticated session (gh auth login).
# GITHUB_TOKEN + curl is only a fallback for environments without gh (e.g. CI).
gh_ready() {
    command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1
}

# ------------------------------------------------------------------------------
# Label metadata (kept in sync with scripts/utils/issue_telemetry.py LABEL_SPECS)
# ------------------------------------------------------------------------------
label_color() {
    case "$1" in
        severity:critical|priority:P0) echo "b60205" ;;
        severity:high|priority:P1)     echo "d93f0b" ;;
        severity:medium|priority:P2)   echo "fbca04" ;;
        severity:low)                  echo "0e8a16" ;;
        priority:P3)                   echo "c5def5" ;;
        *)                             echo "ededed" ;;
    esac
}

label_description() {
    case "$1" in
        severity:critical) echo "A position is unprotected or its stop cannot be verified" ;;
        severity:high)     echo "Execution or a risk gate is blocked/incorrect" ;;
        severity:medium)   echo "Scan, analysis or tooling degraded" ;;
        severity:low)      echo "Cosmetic, docs or optional hardening" ;;
        priority:P0)       echo "Drop everything: fix now" ;;
        priority:P1)       echo "Next up: fix in the current cycle" ;;
        priority:P2)       echo "Planned: schedule soon" ;;
        priority:P3)       echo "Backlog: when convenient" ;;
        *)                 echo "" ;;
    esac
}

# Prints the payload's labels, one per line (python3 if available, grep otherwise).
payload_labels() {
    local payload="$1" parsed
    if parsed=$(printf '%s' "$payload" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("labels") or []))' 2>/dev/null); then
        [ -n "$parsed" ] && printf '%s\n' "$parsed"
        return 0
    fi
    printf '%s' "$payload" | tr '\n' ' ' | grep -o '"labels"[[:space:]]*:[[:space:]]*\[[^]]*\]' | head -n1 \
        | grep -o '"[^"]*"' | sed -n '2,$p' | tr -d '"' || true
}

# Payload without labels and with the title prefixed by "[SEV/Px] " (python3 if available, sed otherwise).
payload_without_labels() {
    local payload="$1" prefix="$2" out
    if out=$(printf '%s' "$payload" | PREFIX="$prefix" python3 -c 'import json,os,sys; d=json.load(sys.stdin); d.pop("labels",None); d["title"]=os.environ["PREFIX"]+(d.get("title") or ""); print(json.dumps(d))' 2>/dev/null); then
        printf '%s' "$out"
        return 0
    fi
    printf '%s\n' "$payload" | sed -e ':a' -e '$!N' -e '$!ba' \
        -e 's/,[[:space:]]*"labels"[[:space:]]*:[[:space:]]*\[[^]]*\]//' \
        -e "s|\"title\"[[:space:]]*:[[:space:]]*\"|&${prefix}|"
}

# Legacy backlog entries (queued before priority labels existed) carry severity:* but no priority:*.
# Inserts the default priority label derived from the severity label (bash + sed only).
payload_with_default_priority() {
    local payload="$1" sev prio
    if printf '%s' "$payload" | grep -q '"priority:P[0-3]"'; then
        printf '%s' "$payload"
        return 0
    fi
    sev=$(printf '%s' "$payload" | grep -o '"severity:[a-z]*"' | head -n1 | tr -d '"' || true)
    case "$sev" in
        severity:critical) prio="P0" ;;
        severity:high)     prio="P1" ;;
        severity:medium)   prio="P2" ;;
        severity:low)      prio="P3" ;;
        *) printf '%s' "$payload"; return 0 ;;
    esac
    printf '%s' "$payload" | sed "s/\"${sev}\"/\"${sev}\", \"priority:${prio}\"/"
}

# Warns loudly (stdout) with the exact fix-up command when severity/priority labels are missing.
warn_missing_labels() {
    local num="$1" labels_csv="$2"
    echo "⚠️ WARNING: issue #${num} is missing its ${labels_csv} label(s). Apply them now:"
    echo "   gh issue edit ${num} --repo ${REPO} --add-label ${labels_csv}"
}

# Verifies that the created issue carries the required labels; adds them with gh issue edit if not.
ensure_required_labels() {
    local num="$1" required_csv="$2" have missing="" label
    local -a required=()
    [ -z "$required_csv" ] && return 0
    IFS=',' read -r -a required <<< "$required_csv"
    have=$(gh api "repos/${REPO}/issues/${num}" --jq '.labels[].name' </dev/null 2>/dev/null || true)
    for label in "${required[@]}"; do
        printf '%s\n' "$have" | grep -qxF "$label" || missing="${missing:+${missing},}${label}"
    done
    [ -z "$missing" ] && return 0
    if gh issue edit "$num" --repo "$REPO" --add-label "$missing" </dev/null >/dev/null 2>&1; then
        echo "ℹ️ Labels ${missing} were dropped on create and have been re-applied to issue #${num}."
        return 0
    fi
    warn_missing_labels "$num" "$missing"
    return 0
}

# POSTs a JSON payload with gh. Sets GH_POST_URL (issue URL) and GH_POST_ERR (gh stderr); returns gh's status.
GH_POST_URL=""
GH_POST_ERR=""
gh_post_issue() {
    local err_file rc=0
    err_file=$(mktemp 2>/dev/null || echo "${LOGS_DIR}/.gh_post_err.$$")
    GH_POST_URL=$(printf '%s' "$1" | gh api -X POST "repos/${REPO}/issues" --input - --jq '.html_url' 2>"$err_file") || rc=$?
    GH_POST_ERR=$(cat "$err_file" 2>/dev/null || true)
    rm -f "$err_file"
    return $rc
}

# True when the last gh POST failed because GitHub rejected the payload (HTTP 422, e.g. unknown labels).
gh_post_rejected_422() {
    printf '%s' "$GH_POST_ERR" | grep -q "HTTP 422"
}

# Creates an issue from a JSON payload ({title, body, labels}) via gh and sets GH_ISSUE_URL.
# Label fallback (only on HTTP 422): create missing labels (--force) and retry -> create without labels with a
# "[SEV/Px] " title prefix and add the labels afterwards. After any create the labels are verified.
# Any other failure (5xx, timeout, 403...) returns 1 at once so the caller queues the report (no duplicates).
# Never silently drops severity/priority.
GH_ISSUE_URL=""
gh_create_issue() {
    local payload="$1" url label required_csv="" sev="" prio="" prefix
    local -a labels=()
    GH_ISSUE_URL=""
    while IFS= read -r label; do
        [ -n "$label" ] && labels+=("$label")
    done < <(payload_labels "$payload")
    for label in "${labels[@]}"; do
        case "$label" in
            severity:*) sev="${label#severity:}"; required_csv="${required_csv:+${required_csv},}${label}" ;;
            priority:*) prio="${label#priority:}"; required_csv="${required_csv:+${required_csv},}${label}" ;;
        esac
    done

    # 1. Labelled create, then verify the labels actually stuck
    if gh_post_issue "$payload"; then
        GH_ISSUE_URL="$GH_POST_URL"
        ensure_required_labels "${GH_ISSUE_URL##*/}" "$required_csv"
        return 0
    fi
    if ! gh_post_rejected_422; then
        echo "⚠️ gh api failed to create the issue: $(printf '%s' "$GH_POST_ERR" | head -n1)"
        return 1
    fi

    # 2. HTTP 422: labels probably missing in the repo. Create them (idempotent) and retry
    if [ "${#labels[@]}" -gt 0 ]; then
        for label in "${labels[@]}"; do
            gh label create "$label" --repo "$REPO" --color "$(label_color "$label")" \
                --description "$(label_description "$label")" --force </dev/null >/dev/null 2>&1 || true
        done
        if gh_post_issue "$payload"; then
            GH_ISSUE_URL="$GH_POST_URL"
            ensure_required_labels "${GH_ISSUE_URL##*/}" "$required_csv"
            return 0
        fi
        if ! gh_post_rejected_422; then
            echo "⚠️ gh api failed to create the issue: $(printf '%s' "$GH_POST_ERR" | head -n1)"
            return 1
        fi
    fi

    # 3. Last resort: no labels, severity/priority kept visible in the title, labels added afterwards
    prefix=""
    if [ -n "$sev" ] || [ -n "$prio" ]; then
        prefix="[$(printf '%s' "$sev" | tr '[:lower:]' '[:upper:]')${sev:+${prio:+/}}${prio}] "
    fi
    if gh_post_issue "$(payload_without_labels "$payload" "$prefix")"; then
        url="$GH_POST_URL"
        GH_ISSUE_URL="$url"
        echo "⚠️ The repository rejected the labels; issue created without labels (title prefixed '${prefix% }')."
        if [ -n "$required_csv" ]; then
            if gh issue edit "${url##*/}" --repo "$REPO" --add-label "$required_csv" </dev/null >/dev/null 2>&1; then
                echo "ℹ️ Labels ${required_csv} added to issue #${url##*/}."
            else
                warn_missing_labels "${url##*/}" "$required_csv"
            fi
        fi
        return 0
    fi
    return 1
}

# Default parameters
TITLE=""
ERROR_DETAIL=""
SEVERITY="HIGH"
PRIORITY=""
CATEGORY="agent_failure"
AGENT_NAME="autonomous_agent"
REMEDIATION=""
REPRO=""
ROOT_CAUSE=""
AFFECTED_FILES=""
CONTEXT=""
CONTEXT_FILE=""
OUTPUT_FILE=""
IMPACT=""
ACCEPTANCE=""
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
        -p|--priority)
            PRIORITY="$2"
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
        --repro)
            REPRO="$2"
            shift 2
            ;;
        --root-cause)
            ROOT_CAUSE="$2"
            shift 2
            ;;
        --affected-files)
            AFFECTED_FILES="$2"
            shift 2
            ;;
        --context)
            CONTEXT="$2"
            shift 2
            ;;
        --context-file)
            CONTEXT_FILE="$2"
            shift 2
            ;;
        --output-file)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        --impact)
            IMPACT="$2"
            shift 2
            ;;
        --acceptance-criteria)
            ACCEPTANCE="$2"
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
            echo "  -t, --title <text>              Issue title describing failure (required)"
            echo "  -e, --error <text>              Error details or returned exception message (required)"
            echo "  -s, --severity <level>          CRITICAL | HIGH | MEDIUM | LOW, case-insensitive (default: HIGH)"
            echo "  -p, --priority <Px>             P0 | P1 | P2 | P3 (default from severity:"
            echo "                                  CRITICAL->P0, HIGH->P1, MEDIUM->P2, LOW->P3)"
            echo "  -c, --category <type>           agent_failure | risk_gate | tool_error | infra (default: agent_failure;"
            echo "                                  case-insensitive, spaces/dashes become '_', other characters outside [a-z0-9_] exit 2)"
            echo "  -a, --agent <name>              Reporting agent name (default: autonomous_agent)"
            echo "  -r, --remediation <text>        Suggested fix or remediation step"
            echo "  --repro <text>                  Exact reproduction command and its exit code (for the trade executor:"
            echo "                                  script name + exit code only; full command/output via --output-file)"
            echo "  --root-cause <text>             Suspected or confirmed root cause"
            echo "  --affected-files <list>         Code pointers 'path:lines, path:lines' (comma or newline separated)"
            echo "  --context <text>                What the agent was doing and what it observed"
            echo "  --context-file <path>           File appended to --context (first 8000 chars);"
            echo "                                  credential files (.env, *.env, MCP configs, *.pem, *.key) exit 2"
            echo "  --output-file <path>            Raw command/agent output; last 200 lines (max 12000 chars) attached;"
            echo "                                  credential files (.env, *.env, MCP configs, *.pem, *.key) exit 2"
            echo "  --impact <text>                 Operational impact on the desk (default derived from category)"
            echo "  --acceptance-criteria <text>    Acceptance criteria, one per line or ';'-separated"
            echo "  --repo <owner/repo>             Target GitHub repository (derived dynamically if omitted)"
            echo "  --sync                          Dispatches pending offline backlog issues"
            echo ""
            echo "Labels: agent-failure, severity:<level>, priority:<Px>, cat:<category>. If the repository rejects"
            echo "labels they are created (gh label create --force) and the create is retried; as a last resort the"
            echo "issue is created unlabelled with a '[SEV/Px] ' title prefix and the labels are added afterwards."
            echo "Free text is sanitized (tokens, secret/token/password/api-key values, signature/listenKey, USD/USDT amounts, monetary keys); t_stat=-3.42 style quant values are kept."
            echo "Only HTTP 422 triggers the label fallback; other gh/curl failures queue the report in the backlog."
            echo "Backlog: \${ISSUE_REPORTER_LOGS_DIR:-logs}/issues_backlog.jsonl"
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
    if [ -z "$REPO" ]; then
        echo "❌ Error: GITHUB_REPO is not configured and cannot be derived from git remote."
        exit 1
    fi
    if ! gh_ready && [ -z "$TOKEN" ]; then
        echo "❌ Error: no authenticated GitHub CLI (run 'gh auth login') and no GITHUB_TOKEN fallback."
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
    count_skipped=0

    while IFS= read -r line || [ -n "$line" ]; do
        [ -z "$line" ] && continue

        item_title=$(printf '%s' "$line" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("title") or "")' 2>/dev/null \
            || printf '%s' "$line" | grep -o '"title"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n1 \
                | sed -E 's/^"title"[[:space:]]*:[[:space:]]*"//; s/"$//' \
            || true)
        if [ -z "$item_title" ]; then
            # Malformed entry (no title): GitHub would reject it; drop it instead of retrying forever
            ((count_skipped++)) || true
            continue
        fi

        # Legacy entries without a priority:* label get the default priority from their severity
        item_payload=$(payload_with_default_priority "$line")

        published=false
        if gh_ready </dev/null; then
            gh_create_issue "$item_payload" </dev/null && published=true
        elif [ -n "$TOKEN" ]; then
            http_code=$(curl -s -o /dev/null -w "%{http_code}" \
                -X POST "https://api.github.com/repos/${REPO}/issues" \
                -H "Authorization: Bearer ${TOKEN}" \
                -H "Accept: application/vnd.github+json" \
                -H "User-Agent: Autonomous-Trading-Desk-Bash" \
                -d "$item_payload" </dev/null) || true
            [ -z "$http_code" ] && http_code="000"
            [ "$http_code" = "201" ] && published=true
        fi

        if [ "$published" = true ]; then
            echo "   ✅ Successfully published: ${item_title}"
            ((count_success++)) || true
        else
            echo "   ❌ Error publishing: ${item_title}"
            echo "$line" >> "$TEMP_BACKLOG"
            ((count_failed++)) || true
        fi
        sleep 1
    done < "$BACKLOG_FILE"

    mv "$TEMP_BACKLOG" "$BACKLOG_FILE"
    echo "🏁 Backlog sync complete: $count_success published, $count_failed remaining, $count_skipped malformed dropped."
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

SEVERITY_UPPER=$(printf '%s' "$SEVERITY" | tr '[:lower:]' '[:upper:]')
case "$SEVERITY_UPPER" in
    CRITICAL|HIGH|MEDIUM|LOW) SEVERITY="$SEVERITY_UPPER" ;;
    *)
        echo "❌ Error: invalid --severity '${SEVERITY}' (expected CRITICAL|HIGH|MEDIUM|LOW)."
        exit 2
        ;;
esac

if [ -z "$PRIORITY" ]; then
    case "$SEVERITY" in
        CRITICAL) PRIORITY="P0" ;;
        HIGH)     PRIORITY="P1" ;;
        MEDIUM)   PRIORITY="P2" ;;
        LOW)      PRIORITY="P3" ;;
    esac
else
    PRIORITY_UPPER=$(printf '%s' "$PRIORITY" | tr '[:lower:]' '[:upper:]')
    case "$PRIORITY_UPPER" in
        P0|P1|P2|P3) PRIORITY="$PRIORITY_UPPER" ;;
        *)
            echo "❌ Error: invalid --priority '${PRIORITY}' (expected P0|P1|P2|P3)."
            exit 2
            ;;
    esac
fi

CATEGORY_LOWER=$(printf '%s' "$CATEGORY" | tr '[:upper:]' '[:lower:]' | sed -E 's/^[[:space:]]+|[[:space:]]+$//g; s/[[:space:]-]+/_/g')
if ! [[ "$CATEGORY_LOWER" =~ ^[a-z0-9_-]+$ ]]; then
    echo "❌ Error: invalid --category '${CATEGORY}' (lowercase letters, digits and '_' only; spaces and '-' become '_', e.g. tool_error|risk_gate|infra|agent_failure)."
    exit 2
fi
CATEGORY="$CATEGORY_LOWER"

CREDENTIAL_PATH_RE='^(\.env(\..*)?|.*\.env|\.?mcp\.json|.*mcp_config.*|.*credentials.*|.*\.pem|.*\.key)$'
# True when the path as given or its resolved target names a credential-bearing file (#58)
is_credential_path() {
    local candidate resolved base
    resolved=$(readlink -f -- "$1" 2>/dev/null || true)
    for candidate in "$1" "$resolved"; do
        [ -n "$candidate" ] || continue
        candidate="${candidate//\\//}"
        base=$(printf '%s' "${candidate##*/}" | tr '[:upper:]' '[:lower:]')
        [[ "$base" =~ $CREDENTIAL_PATH_RE ]] && return 0
    done
    return 1
}
for attach_pair in "--context-file:${CONTEXT_FILE}" "--output-file:${OUTPUT_FILE}"; do
    attach_flag="${attach_pair%%:*}"
    attach_path="${attach_pair#*:}"
    if [ -n "$attach_path" ] && is_credential_path "$attach_path"; then
        echo "❌ Error: refusing to attach '${attach_path}' (${attach_flag}): credential-bearing file (.env, *.env, MCP configs (.mcp.json, mcp_config*), *credentials*, *.pem, *.key). Copy only the relevant non-secret lines into logs/issue_output_<unix_ts>.log and attach that file instead."
        exit 2
    fi
done

SEV_LABEL="severity:$(printf '%s' "$SEVERITY" | tr '[:upper:]' '[:lower:]')"
PRIO_LABEL="priority:${PRIORITY}"
CAT_LABEL="cat:${CATEGORY}"

# ------------------------------------------------------------------------------
# Telemetry Sanitization
# ------------------------------------------------------------------------------
sanitize_telemetry() {
    local text="$1"
    # Redact GitHub Tokens
    text=$(printf '%s\n' "$text" | sed -E 's/ghp_[A-Za-z0-9_]{20,}/[REDACTED_GH_TOKEN]/g')
    text=$(printf '%s\n' "$text" | sed -E 's/github_pat_[A-Za-z0-9_]{20,}/[REDACTED_GH_PAT]/g')
    # Redact Notion Tokens
    text=$(printf '%s\n' "$text" | sed -E 's/(secret_|ntn_)[A-Za-z0-9_]{10,}/[REDACTED_NOTION_TOKEN]/g')
    # Redact Bearer Tokens
    text=$(printf '%s\n' "$text" | sed -E 's|(Bearer[[:space:]]+)[A-Za-z0-9._~+/-]+=*|\1[REDACTED_TOKEN]|gI')
    # Redact API Keys / Passwords
    text=$(printf '%s\n' "$text" | sed -E 's/(api[_-]?key|secret[_-]?key|password|app[_-]?password)[[:space:]]*[:=][[:space:]]*["\x27]?[A-Za-z0-9/+=._-]{8,}["\x27]?/\1=[REDACTED]/gI')
    # Generic secret/token/password/private-key/api-key values (key=value, key: value, JSON "key": "value"); key name kept
    text=$(printf '%s\n' "$text" | sed -E 's/((secret|token|passw(or)?d|private[_-]?key|api[_-]?key)[A-Za-z0-9_-]*"?[[:blank:]]*[:=][[:blank:]]*"?)[A-Za-z0-9/+=._~-]+/\1[REDACTED]/gI')
    # Binance signed-request signatures and user-data stream listen keys
    text=$(printf '%s\n' "$text" | sed -E 's/((signature|listen[_-]?key)"?[[:blank:]]*[:=][[:blank:]]*"?)[A-Za-z0-9]+/\1[REDACTED]/gI')
    # Redact numeric values of monetary keys in JSON / key=value text (e.g. "notional_usdt": 4321.87, margin=-3.2)
    text=$(printf '%s\n' "$text" | sed -E 's/("?[A-Za-z0-9_]*(usdt|usd|pnl|notional|margin|balance|equity|profit|wallet)[A-Za-z0-9_]*"?[[:space:]]*[:=][[:space:]]*)["\x27]?[-+]?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?["\x27]?/\1"[REDACTED]"/gI')
    # Redact balances and dollar amounts (signed too: $+3087.31, $-16.75)
    text=$(printf '%s\n' "$text" | sed -E 's/\$[[:space:]]*[-+]?[0-9]+(\.[0-9]+)?/[REDACTED_USD]/g')
    # Amounts followed by USDT/USD, also after a closing backtick/bold marker (`3.20` USDT, **3.20** USDT);
    # the non-word guard keeps symbols such as API3USDT / C98USDT intact
    text=$(printf '%s\n' "$text" | sed -E 's/(^|[^A-Za-z0-9_])[0-9]+(\.[0-9]+)?[`*]*[[:space:]]*(USDT|USD)/\1[REDACTED_AMT] USDT/gI')
    # Labelled quant statistics (t_stat: -3.42, "z": -2.15, beta:-0.87) keep their sign: protect it, redact, restore
    text=$(printf '%s\n' "$text" | sed -E -e 's/~Q(NEG|POS)~//g' \
        -e 's/(^|[^A-Za-z0-9_])("?(t[_-]?stat|tstat|z|z[_-]?score|beta|half[_-]?life|hurst|r2|p[_-]?value|pvalue|corr)"?[[:blank:]]*[:=][[:blank:]]*)-([0-9])/\1\2~QNEG~\4/gI' \
        -e 's/(^|[^A-Za-z0-9_])("?(t[_-]?stat|tstat|z|z[_-]?score|beta|half[_-]?life|hurst|r2|p[_-]?value|pvalue|corr)"?[[:blank:]]*[:=][[:blank:]]*)\+([0-9])/\1\2~QPOS~\4/gI')
    # Bare signed decimals (PnL table cells like "| -16.75 |"), not percentages
    text=$(printf '%s\n' "$text" | sed -E -e ':a' -e 's/(^|[[:space:]|`(:])[+-][0-9]+\.[0-9]+([^0-9%]|$)/\1[REDACTED_AMT]\2/' -e 'ta')
    text=$(printf '%s\n' "$text" | sed -E -e 's/~QNEG~/-/g' -e 's/~QPOS~/+/g')
    printf '%s\n' "$text"
}

CLEAN_TITLE=$(sanitize_telemetry "$TITLE")
CLEAN_ERROR=$(sanitize_telemetry "$ERROR_DETAIL")
CLEAN_REMEDIATION=$(sanitize_telemetry "$REMEDIATION")
CLEAN_REPRO=$(sanitize_telemetry "$REPRO")
CLEAN_ROOT_CAUSE=$(sanitize_telemetry "$ROOT_CAUSE")
CLEAN_AFFECTED=$(sanitize_telemetry "$AFFECTED_FILES")
CLEAN_IMPACT=$(sanitize_telemetry "$IMPACT")
CLEAN_ACCEPTANCE=$(sanitize_telemetry "$ACCEPTANCE")

FULL_CONTEXT="$CONTEXT"
if [ -n "$CONTEXT_FILE" ]; then
    if [ -r "$CONTEXT_FILE" ]; then
        CONTEXT_FILE_TEXT=$(head -c 8000 "$CONTEXT_FILE" 2>/dev/null || true)
    else
        CONTEXT_FILE_TEXT="(could not read context file ${CONTEXT_FILE})"
    fi
    if [ -n "$FULL_CONTEXT" ]; then
        FULL_CONTEXT=$(printf '%s\n\n%s' "$FULL_CONTEXT" "$CONTEXT_FILE_TEXT")
    else
        FULL_CONTEXT="$CONTEXT_FILE_TEXT"
    fi
fi
CLEAN_CONTEXT=$(sanitize_telemetry "$FULL_CONTEXT")

CLEAN_OUTPUT=""
if [ -n "$OUTPUT_FILE" ]; then
    if [ -r "$OUTPUT_FILE" ]; then
        OUTPUT_TAIL=$(tail -n 200 "$OUTPUT_FILE" 2>/dev/null | tail -c 12000 2>/dev/null | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' || true)
    else
        OUTPUT_TAIL="(could not read output file ${OUTPUT_FILE})"
    fi
    CLEAN_OUTPUT=$(sanitize_telemetry "$OUTPUT_TAIL")
fi

TIMESTAMP_UTC=$(date -u +"%Y-%m-%d %H:%M:%S UTC")

# ------------------------------------------------------------------------------
# Runtime & Ledger Telemetry (never fails the report)
# ------------------------------------------------------------------------------
T_COMMIT="unavailable"
T_BRANCH="unavailable"
T_DIRTY="unavailable"
T_ENV=""
TELEMETRY_MD=""
if command -v python3 >/dev/null 2>&1 && [ -f "$TELEMETRY_HELPER" ]; then
    TELEMETRY_KV=$(python3 "$TELEMETRY_HELPER" --kv --base-dir "$BASE_DIR" --logs-dir "$LOGS_DIR" 2>/dev/null || true)
    while IFS=$'\t' read -r t_key t_val; do
        case "$t_key" in
            commit)     T_COMMIT="$t_val" ;;
            branch)     T_BRANCH="$t_val" ;;
            dirty)      T_DIRTY="$t_val" ;;
            target_env) T_ENV="$t_val" ;;
        esac
    done <<< "$TELEMETRY_KV"
    TELEMETRY_MD=$(python3 "$TELEMETRY_HELPER" --markdown --base-dir "$BASE_DIR" --logs-dir "$LOGS_DIR" 2>/dev/null || true)
fi
if [ -z "$TELEMETRY_MD" ]; then
    # Bash-only fallback: python3 or the helper is missing/broken
    T_COMMIT="unavailable"
    T_BRANCH="unavailable"
    T_DIRTY="unavailable"
    if command -v git >/dev/null 2>&1; then
        T_COMMIT=$(git -C "$BASE_DIR" rev-parse --short HEAD 2>/dev/null || echo "unavailable")
        T_BRANCH=$(git -C "$BASE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unavailable")
        if T_PORCELAIN=$(git -C "$BASE_DIR" status --porcelain 2>/dev/null); then
            if [ -n "$T_PORCELAIN" ]; then T_DIRTY="true"; else T_DIRTY="false"; fi
        fi
    fi
    T_ENV=$(printf '%s' "${BINANCE_API_ENV:-TESTNET}" | tr '[:lower:]' '[:upper:]')
    T_PLATFORM=$(uname -sr 2>/dev/null || echo "unavailable")
    TELEMETRY_MD=$(cat <<EOF
| Signal | Value |
| :--- | :--- |
| **Target environment** | \`${T_ENV}\` |
| **Git commit** | \`${T_COMMIT}\` |
| **Git branch** | \`${T_BRANCH}\` |
| **Working tree dirty** | \`${T_DIRTY}\` |
| **Platform** | \`${T_PLATFORM}\` |
| **Ledger snapshot** | \`unavailable (python3 or scripts/utils/issue_telemetry.py missing)\` |
EOF
)
fi
[ -z "$T_ENV" ] && T_ENV=$(printf '%s' "${BINANCE_API_ENV:-TESTNET}" | tr '[:lower:]' '[:upper:]')

# Every value that reaches the body goes through the sanitizer (telemetry included)
TELEMETRY_MD=$(sanitize_telemetry "$TELEMETRY_MD")
T_ENV=$(sanitize_telemetry "$T_ENV")
T_COMMIT=$(sanitize_telemetry "$T_COMMIT")
T_BRANCH=$(sanitize_telemetry "$T_BRANCH")
T_DIRTY=$(sanitize_telemetry "$T_DIRTY")
CLEAN_CATEGORY=$(sanitize_telemetry "$CATEGORY")
CLEAN_AGENT=$(sanitize_telemetry "$AGENT_NAME")

case "$SEVERITY" in
    CRITICAL) SEV_BADGE="🔴 CRITICAL" ;;
    HIGH)     SEV_BADGE="🟠 HIGH" ;;
    MEDIUM)   SEV_BADGE="🟡 MEDIUM" ;;
    LOW)      SEV_BADGE="🔵 LOW" ;;
esac
case "$PRIORITY" in
    P0) PRIO_BADGE="🔴 P0" ;;
    P1) PRIO_BADGE="🟠 P1" ;;
    P2) PRIO_BADGE="🟡 P2" ;;
    P3) PRIO_BADGE="🔵 P3" ;;
esac

# Section 5 default: one-liner derived from the category
if [ -z "$CLEAN_IMPACT" ]; then
    case "$(printf '%s' "$CATEGORY" | tr '[:upper:]' '[:lower:]')" in
        risk_gate)     CLEAN_IMPACT="A risk gate blocked or mis-evaluated an order (fail-closed)" ;;
        tool_error)    CLEAN_IMPACT="A desk script/tool failed; the affected step was stopped fail-closed" ;;
        infra)         CLEAN_IMPACT="Infrastructure/API degradation" ;;
        agent_failure) CLEAN_IMPACT="Agent workflow failure; no orders were placed from this step" ;;
        *)             CLEAN_IMPACT="Impact not specified by the reporting agent; triage required" ;;
    esac
fi

# Section 3: reproduction + raw output tail
if [ -n "$CLEAN_REPRO" ]; then
    REPRO_MD=$(printf '**Reproduction command:**\n```bash\n%s\n```' "$CLEAN_REPRO")
else
    REPRO_MD="**Reproduction command:** not provided"
fi
OUTPUT_MD=""
if [ -n "$CLEAN_OUTPUT" ]; then
    OUTPUT_MD=$(printf '\n<details><summary>Raw output (tail)</summary>\n\n```text\n%s\n```\n\n</details>' "$CLEAN_OUTPUT")
fi

# Section 4: code pointers, root cause, context
AFFECTED_MD=$(printf '%s\n' "$CLEAN_AFFECTED" | tr ',' '\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    | grep -v '^$' | sed 's/.*/- `&`/' || true)
if [ -n "$AFFECTED_MD" ]; then
    AFFECTED_MD=$(printf '**Affected files:**\n%s' "$AFFECTED_MD")
else
    AFFECTED_MD="**Affected files:** not provided"
fi
CONTEXT_MD=""
if [ -n "$CLEAN_CONTEXT" ]; then
    CONTEXT_MD=$(printf '\n**Context:**\n%s' "$CLEAN_CONTEXT")
fi

# Section 6: acceptance criteria as checkboxes
ACCEPTANCE_MD=$(printf '%s\n' "$CLEAN_ACCEPTANCE" | tr ';' '\n' \
    | sed -e 's/^[[:space:]]*//' -e 's/^[-*][[:space:]]*//' -e 's/^\[[ xX]\][[:space:]]*//' -e 's/[[:space:]]*$//' \
    | grep -v '^$' | sed 's/^/- [ ] /' || true)
[ -z "$ACCEPTANCE_MD" ] && ACCEPTANCE_MD="- [ ] Regression test added in tests/ covering this failure"

# ------------------------------------------------------------------------------
# Fingerprint and open-issue lookup (issue #270: reporters of parallel sessions)
# ------------------------------------------------------------------------------
# The Python reporter computes the fingerprint (same as its own reports) and, when a repo and gh are available,
# looks it up in the OPEN issues (one gh call, 5 s bound; the whole helper is capped at 10 s with `timeout`).
# Any failure (no python3 / helper / gh, offline, timeout) leaves both empty: the report is created or queued as
# before.
DEDUPE_HELPER="${BASE_DIR}/scripts/report_agent_issue.py"
FINGERPRINT=""
OPEN_ISSUE_URL=""
if command -v python3 >/dev/null 2>&1 && [ -f "$DEDUPE_HELPER" ]; then
    LOOKUP_REPO=""
    if [ -n "$REPO" ] && command -v gh >/dev/null 2>&1; then
        LOOKUP_REPO="$REPO"
    fi
    if type -P timeout >/dev/null 2>&1; then
        LOOKUP_OUT=$(timeout 10 python3 "$DEDUPE_HELPER" --find-open-issue --title "$TITLE" --error "$ERROR_DETAIL" \
            --severity "$SEVERITY" --repo "$LOOKUP_REPO" </dev/null 2>/dev/null || true)
    else
        LOOKUP_OUT=$(python3 "$DEDUPE_HELPER" --find-open-issue --title "$TITLE" --error "$ERROR_DETAIL" \
            --severity "$SEVERITY" --repo "$LOOKUP_REPO" </dev/null 2>/dev/null || true)
    fi
    while IFS=$'\t' read -r l_key l_val; do
        if [ "$l_key" = "fingerprint" ] && [[ "$l_val" =~ ^[0-9a-f]{16}$ ]]; then
            FINGERPRINT="$l_val"
        elif [ "$l_key" = "open_issue" ] && [[ "$l_val" =~ ^https://[^[:space:]]+/issues/[0-9]+$ ]]; then
            OPEN_ISSUE_URL="$l_val"
        fi
    done <<< "$LOOKUP_OUT"
fi
FINGERPRINT_ROW=""
if [ -n "$FINGERPRINT" ]; then
    FINGERPRINT_ROW=$'\n'"| **Fingerprint ID** | \`${FINGERPRINT}\` |"
fi

# ------------------------------------------------------------------------------
# Markdown Body Construction (same six headings as scripts/utils/issue_telemetry.py)
# ------------------------------------------------------------------------------
MD_BODY=$(cat <<EOF
## 🚨 Autonomous Agent Failure Report

### 1. Executive Summary & Severity Matrix

| Dimension | Value |
| :--- | :--- |
| **Severity** | **${SEV_BADGE}** |
| **Priority** | **${PRIO_BADGE}** |
| **Category** | \`${CLEAN_CATEGORY}\` |
| **Reporting Agent** | \`${CLEAN_AGENT}\` |
| **Environment** | \`${T_ENV}\` |
| **Git** | \`${T_COMMIT}\` on \`${T_BRANCH}\` (dirty: ${T_DIRTY}) |
| **Timestamp UTC** | \`${TIMESTAMP_UTC}\` |${FINGERPRINT_ROW}

**Description:**
${CLEAN_ERROR}

### 2. Runtime & Ledger Telemetry Snapshot

${TELEMETRY_MD}

### 3. Reproduction & Exact Telemetry

${REPRO_MD}
${OUTPUT_MD}

### 4. Code Pointers & Root Cause Analysis

${AFFECTED_MD}

**Root cause:** ${CLEAN_ROOT_CAUSE:-not provided}
${CONTEXT_MD}

### 5. Operational Impact on Trading Desk

${CLEAN_IMPACT}

### 6. Remediation Plan & Acceptance Criteria

**Remediation:** ${CLEAN_REMEDIATION:-not provided}

**Acceptance criteria:**
${ACCEPTANCE_MD}

---
*Reported natively via bash shell by the \`autonomous-trading-desk\` observability harness.*
EOF
)

# ------------------------------------------------------------------------------
# JSON Payload Serialization
# ------------------------------------------------------------------------------
# Reads stdin and prints it as a JSON string literal (python3 if available, sed otherwise).
json_escape() {
    local s
    s=$(cat)
    if printf '%s' "$s" | python3 -c 'import json, sys; sys.stdout.write(json.dumps(sys.stdin.buffer.read().decode("utf-8", "replace")))' 2>/dev/null; then
        return 0
    fi
    printf '"%s"' "$(printf '%s\n' "$s" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' -e 's/\r/\\r/g' \
        | tr -d '\000-\010\013\014\016-\037' | awk 'NR > 1 { printf "\\n" } { printf "%s", $0 }')"
}

JSON_TITLE=$(printf '%s' "$CLEAN_TITLE" | json_escape)
JSON_BODY=$(printf '%s' "$MD_BODY" | json_escape)
JSON_SEV_LABEL=$(printf '%s' "$SEV_LABEL" | json_escape)
JSON_CAT_LABEL=$(printf '%s' "$CAT_LABEL" | json_escape)

PAYLOAD=$(cat <<EOF
{
  "title": ${JSON_TITLE},
  "body": ${JSON_BODY},
  "labels": ["agent-failure", ${JSON_SEV_LABEL}, "${PRIO_LABEL}", ${JSON_CAT_LABEL}]
}
EOF
)

# ------------------------------------------------------------------------------
# Dispatch to GitHub API or Enqueue in Local Backlog
# ------------------------------------------------------------------------------
if [ -n "$OPEN_ISSUE_URL" ]; then
    echo "ℹ️ Deduplicated: open issue #${OPEN_ISSUE_URL##*/} already reports this failure (fingerprint ${FINGERPRINT}); no new issue created."
    echo "   URL: ${OPEN_ISSUE_URL}"
    exit 0
fi
if [ -n "$REPO" ] && gh_ready; then
    if gh_create_issue "$PAYLOAD"; then
        ISSUE_URL="$GH_ISSUE_URL"
        echo "✅ GITHUB ISSUE CREATED SUCCESSFULLY: #${ISSUE_URL##*/}"
        echo "   URL: ${ISSUE_URL}"
        exit 0
    fi
    echo "⚠️ gh failed to create the issue. Enqueueing in local backlog..."
elif [ -n "$TOKEN" ] && [ -n "$REPO" ]; then
    HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST "https://api.github.com/repos/${REPO}/issues" \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Accept: application/vnd.github+json" \
        -H "User-Agent: Autonomous-Trading-Desk-Bash" \
        -d "$PAYLOAD") || true

    HTTP_STATUS=$(echo "$HTTP_RESPONSE" | tail -n1)
    RESPONSE_BODY=$(echo "$HTTP_RESPONSE" | sed '$d')
    [[ "$HTTP_STATUS" =~ ^[0-9]{3}$ ]] || HTTP_STATUS="000"

    if [ "$HTTP_STATUS" = "201" ]; then
        ISSUE_URL=$(echo "$RESPONSE_BODY" | grep -o '"html_url": *"[^"]*"' | head -n1 | cut -d'"' -f4)
        ISSUE_NUM=$(echo "$RESPONSE_BODY" | grep -o '"number": *[0-9]*' | head -n1 | cut -d':' -f2 | tr -d ' ')
        echo "✅ GITHUB ISSUE CREATED SUCCESSFULLY: #${ISSUE_NUM}"
        echo "   URL: ${ISSUE_URL}"
        MISSING_LABELS=""
        for label in "$SEV_LABEL" "$PRIO_LABEL"; do
            printf '%s' "$RESPONSE_BODY" | grep -qE "\"name\": *\"${label}\"" \
                || MISSING_LABELS="${MISSING_LABELS:+${MISSING_LABELS},}${label}"
        done
        if [ -n "$MISSING_LABELS" ]; then
            warn_missing_labels "$ISSUE_NUM" "$MISSING_LABELS"
        fi
        exit 0
    else
        echo "⚠️ Failed to connect to GitHub API (HTTP ${HTTP_STATUS}). Enqueueing in local backlog..."
    fi
elif [ -z "$REPO" ]; then
    echo "ℹ️ GITHUB_REPO not configured or detectable from git remote. Enqueueing issue in local backlog (${BACKLOG_FILE})..."
else
    echo "ℹ️ GitHub CLI not authenticated (gh auth login) and no GITHUB_TOKEN. Enqueueing issue in local backlog (${BACKLOG_FILE})..."
fi

# Fallback: persist in local backlog
echo "$PAYLOAD" | tr '\n' ' ' >> "$BACKLOG_FILE"
echo "" >> "$BACKLOG_FILE"
echo "📁 Issue saved in local backlog (${BACKLOG_FILE})."
echo "   To publish once gh is authenticated: ./scripts/report_issue.sh --sync"
