#!/usr/bin/env python3
"""
pre_trade_guard.py - PreToolUse Hook for the agentic runtime.
Deterministic pre-execution safety harness and programmatic risk verification.

Supported runtimes (output format is selected automatically):
  * Google Antigravity (agy), forced with `--agy`: stdin {toolCall:{name,args}, conversationId, ...};
    stdout {"decision": "allow"|"deny"|"ask"|"force_ask", "reason"}; ALWAYS exit 0
    (agy treats any non-zero exit code as a hook failure and drops the reason).
  * Claude Code (payload with tool_name/tool_input): deny -> exit 2 with the reason on stderr;
    otherwise exit 0 (JSON hookSpecificOutput.permissionDecision for "allow"/"ask").
  * Legacy (toolCall payload without --agy): {"decision", "code", "reason"}; deny exits 2.

Hardened against fail-open behaviors and spoofing vulnerabilities:
1. DEFAULT-DENY ON UNRECOGNIZED/EMPTY INPUT:
   Empty payload, invalid JSON, or malformed payload shape immediately return 'deny'.
2. SINGLE CHOKE POINT ENFORCEMENT:
   Opening trades are permitted ONLY via 'scripts/execute_futures_trade.py'.
   Binance MCP tools are checked against an ALLOWLIST of read-only tools (also when wrapped in the
   gateway meta-tool 'tool_execute'). Risk-reducing calls (reduceOnly=true, closePosition=true, cancel*)
   are allowed; every other Binance write tool is DENIED.
   The retired 'crypto_radar' MCP server (and its legacy tool names on any server alias) is DENIED
   outright, pointing to the CLI replacements (market-radar skill, executor flags, position guardian loop).
3. INLINE-CODE / RAW API BYPASS PREVENTION:
   run_command calls that use trading primitives outside the sanctioned scripts (python -c, heredocs,
   piped interpreters, curl/wget writes to Binance, unsanctioned scripts importing the engine) are denied.
   Batch deploy scripts and auto-deploy loops are treated as trade openings.
4. STRUCTURED RISK-REDUCING ACTION PARSING:
   Requires exact structured flags (--close-position, --auto-heal, --audit-orphans, --protect-pending,
   --move-breakeven with exactly one --symbol, reduceOnly=true), evaluated per shell sub-command. Never matches generic substrings
   like 'close'. `execute_futures_trade.py --positions` is read-only (ask). The position guardian loop never
   opens positions: bounded runs (--once / --dry-run) are allowed, long-running ones require confirmation.
5. FAIL-CLOSED SESSION STATE & STALENESS CHECK:
   logs/session_state.json must exist, be valid (is_valid=True) and NOT stale (<= 300s).
6. EVALUATION DOSSIER PROVENANCE (scripts/utils/dossier_provenance.py):
   PROD requires a schema v2 dossier whose provenance hash is re-verified against the
   isolated_market_evaluator subagent transcript (agy brain or Claude Code subagents/ with
   meta agentType), a matching direction and (when known) a parent conversation equal to the
   current one (agy conversationId / Claude Code session_id). TESTNET is relaxed.
7. EVALUATION TRAIL PROTECTION:
   Writes into logs/evaluations/, Antigravity brain transcripts or Claude Code subagent transcripts
   are denied, and so are agent-set transcript-root overrides (AGY_BRAIN_DIRS / CLAUDE_PROJECTS_DIRS);
   harness files (incl. .claude/agents/) require explicit confirmation (force_ask).
8. GROUND TRUTH PROTECTION (GROUND_TRUTH_FILES):
   Runtime state that gates PROD orders has exactly one sanctioned writer, which writes it from Python:
   logs/session_state.json <- scripts/sync_session_state.py; logs/guardian_state.json (guardian liveness
   attestation for resting entries) <- scripts/loops/position_guardian_loop.py; logs/pending_entries.json
   (resting-entry registry / post-fill protection) <- scripts/execute_futures_trade.py (registration and
   --protect-pending). File tools targeting them are denied: relative, absolute and Windows paths, NTFS aliases
   (trailing dot/space, ::$DATA streams) and targets whose os.path.realpath / samefile is a protected file
   (symlinked directory, hard link). In shell commands, a sub-command naming a protected file (literally or via
   a logs/ glob/brace word) is denied unless its program is read-only (GROUND_TRUTH_READ_PROGRAMS, jq without
   --in-place, python3 -m json.tool without an output file, git read sub-commands such as diff/log/commit,
   gh pr|issue, find judged below, python -c / node -e / python|node heredocs judged by write markers) and has no
   write/exec option (git --output / --open-files-in-pager / --ext-diff / --upload-pack / --receive-pack /
   --exec and their unique-prefix abbreviations (--op=, --upl=), short clusters with O (-nOrm), git -c /
   --config-env / --exec-path, rg --pre / --hostname-bin, less -o / -O / --log-file / +cmd) and no leading VAR=
   assignment (LESSOPEN, GIT_*). Any redirect to a protected file is denied (including ')>', ';>', '<>').
   Nested command strings are judged like top-level ones: sh/bash -c, eval, cmd /c, powershell -Command,
   git -c values, values of command-running options (git -O<cmd> / --upload-pack / --receive-pack / --exec,
   rg --pre, less +!cmd), whether or not a protected file is named, and find -exec commands ({} = a root the
   filters can match or logs/ under it). A command-running option that runs on files (rg --pre, git grep -O) or
   on a local repository (git fetch --upload-pack, push --receive-pack / --exec) is denied when an operand (or the
   default '.') is logs/ or one of its ancestors (rg --pre rm . logs, git fetch --upl=CMD .). $(pwd), `pwd`,
   $PWD and $(git rev-parse --show-toplevel) count as '.', and $(cmd)/path as ./path (cmd judged separately).
   Also denied: inline interpreters with write calls naming them, or with destructive calls next to a 'logs'
   literal / logs/ glob (rmtree, rmSync, unlink, rename...) or recursive deletes/moves next to an ancestor
   literal ('.', '..', the repo). Both are checked on the whole command line, so a `git commit -m "$(cat <<EOF
   ...)"` or `gh ... --body "$(...)"` text naming a protected file next to a write marker such as `.write(` is
   denied: use -F <file> / --body-file. Heredoc bodies are only exempt from the line-by-line check when fed to
   python/node. Also: destructive find whose filters can match them or that is unfiltered/negated over a root that
   is or contains logs/ (., .., /, ~, the repo), recursive rm / rd / del / Remove-Item / mv / move of logs/ or an
   ancestor (globs and braces expanded: log*, {logs,build}), copies into logs/ (incl. -t/--target-directory),
   symlinks/hard links (ln, mklink) aliasing logs/, git clean -x/-X and git stash --all.
   Reads by allowlisted programs keep the normal permission policy (ask). Not covered: variable indirection,
   xargs, archives, bare globs without a logs/ component (cd logs && rm *.json), cp -r src/. . / rsync src/ .
   into the repo root, rsync --files-from, powershell -EncodedCommand, and a missing pending_entries.json still
   reads as empty in the executor (tracked as a follow-up).
9. PASS-THROUGH:
   Tool calls unrelated to trading return "ask" so the runtime's normal permission policy applies.
   "allow" is reserved for calls that passed every trading gate or are purely risk-reducing.
10. HEARTBEAT:
   Every invocation refreshes logs/hook_heartbeat.json (best effort, never alters the decision).

Target latency: < 15ms (plus dossier provenance re-verification on trade openings).
"""

import os
import sys
import json
import time
import re
import shlex
import fnmatch
import posixpath
import datetime
from typing import Dict, Any, Tuple, Optional, List

# Ensure scripts directory is on sys.path for utils
base_script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_script_dir not in sys.path:
    sys.path.insert(0, base_script_dir)

try:
    from utils.env_resolver import resolve_env, is_prod_environment, find_workspace_root
except ImportError:
    try:
        from scripts.utils.env_resolver import resolve_env, is_prod_environment, find_workspace_root
    except ImportError:
        def find_workspace_root() -> str:
            p = os.path.abspath(__file__)
            while p and p != os.path.dirname(p):
                p = os.path.dirname(p)
                if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "logs")):
                    return p
            return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        def resolve_env(explicit_env=None, base_dir=None) -> str:
            val = (explicit_env or os.environ.get("BINANCE_API_ENV") or "prod").strip().lower()
            return "prod" if val in ["prod", "production", "mainnet"] else "testnet"

        def is_prod_environment(explicit_env=None, base_dir=None) -> bool:
            return resolve_env(explicit_env, base_dir=base_dir) == "prod"

try:
    from utils import dossier_provenance as dp
except ImportError:  # pragma: no cover - fail closed below when the module is missing
    dp = None

try:
    from utils.atomic_writer import atomic_write_json
except ImportError:  # pragma: no cover
    atomic_write_json = None


HOOK_NAME = "pre_trade_guard"
HEARTBEAT_ENV_OVERRIDE = "PRE_TRADE_GUARD_HEARTBEAT_FILE"

CHOKE_POINT = "'scripts/execute_futures_trade.py'"
EVALUATOR_HINT = (
    "Invoke the clean-room evaluator via invoke_subagent with TypeName 'isolated_market_evaluator', "
    "then record its verdict with `python3 scripts/record_evaluation.py --from-subagent <conversationId>` "
    "(Claude Code: Agent tool with subagent_type 'isolated_market_evaluator', then "
    "`python3 scripts/record_evaluation.py --from-claude-subagent <agentId>`)."
)

# -----------------------------------------------------------------------------
# Tool name classification
# -----------------------------------------------------------------------------
AGY_MCP_CALL_TOOLS = {"call_mcp_tool", "mcp_tool"}
FILE_WRITE_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content"}
CLAUDE_FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Retired servers stay listed so eager names (mcp_crypto_radar_<tool>) still split correctly.
KNOWN_MCP_SERVERS = ("crypto_radar", "binance")

# The crypto_radar MCP server is retired: every call is denied (fail closed if a stale client config still
# starts it). Its legacy tool names are denied on any server alias and mapped to their CLI replacements.
RETIRED_MCP_SERVERS = {"crypto_radar"}
LEGACY_RADAR_TOOL_REPLACEMENTS = {
    "scan_intraday_market": "python3 scripts/broad_market_radar.py --json",
    "scan_yolo_moonshot": "python3 scripts/broad_yolo_scanner.py --json",
    "scan_delta_neutral_pairs": "python3 scripts/quant_risk_engine.py pairs --json",
    "calculate_volatility_parity": "python3 scripts/quant_risk_engine.py parity --json",
    "calculate_position_sizing": "python3 scripts/quant_risk_engine.py parity --json",
    "get_empirical_kelly_audit": "python3 scripts/quant_risk_engine.py kelly --json",
    "get_crypto_newsletters": "python3 scripts/fetch_newsletters.py --format json",
    "deploy_futures_trade": "python3 scripts/execute_futures_trade.py --symbol <SYMBOL> --direction <LONG|SHORT> ... "
                            "(after the clean-room evaluation)",
    "place_order": "python3 scripts/execute_futures_trade.py --symbol <SYMBOL> --direction <LONG|SHORT> ... "
                   "(after the clean-room evaluation)",
    "get_open_positions": "python3 scripts/execute_futures_trade.py --positions --json",
    "move_to_breakeven": "python3 scripts/execute_futures_trade.py --move-breakeven --symbol <SYMBOL>",
    "move_sl_to_breakeven": "python3 scripts/execute_futures_trade.py --move-breakeven --symbol <SYMBOL>",
    "close_position_market": "python3 scripts/execute_futures_trade.py --close-position --symbol <SYMBOL>",
    "close_position": "python3 scripts/execute_futures_trade.py --close-position --symbol <SYMBOL>",
    "audit_orphan_positions": "python3 scripts/execute_futures_trade.py --audit-orphans (or --auto-heal)",
    "update_trailing_stop_structural": "python3 scripts/loops/position_guardian_loop.py --once",
    "audit_and_trail_all_positions": "python3 scripts/loops/position_guardian_loop.py --once",
    "check_dead_alpha": "python3 scripts/loops/position_guardian_loop.py --once --dry-run",
    "report_agent_execution_issue": "./scripts/report_issue.sh --title ... --error ...",
}

# Binance MCP product namespaces (dotted names such as futures_usds.newOrder)
BINANCE_NAMESPACE_RE = re.compile(
    r"^(?:futures_usds|futures_coin|spot|margin|wallet|convert|sub_account|analysis|algo|alpha|"
    r"copy_trading|simple_earn|staking|portfolio_margin\w*|derivatives_\w+|c2c|fiat|mining|pay|rebate|"
    r"vip_loan|crypto_loan|dual_investment|gift_card)\.[A-Za-z0-9_]+$"
)
# Gateway naming pattern `{verb}_{product}_{operation}` (e.g. create_spot_newOrder)
BINANCE_VERB_RE = re.compile(
    r"^(get|create|delete|update|put|post|cancel)_((?:futures_usds|futures_coin|spot|margin|wallet|convert|"
    r"sub_account|[a-z]+)(?:_[a-z]+)*)_([A-Za-z0-9]+)$"
)
BINANCE_META_READ_TOOLS = {"tool_search"}
BINANCE_META_EXECUTE_TOOLS = {"tool_execute"}

# Read-only Binance operations (lower-case operation names, namespace-agnostic). Derived from
# .agents/skills/binance/references/*.md and the tool names observed in agy transcripts.
BINANCE_READ_ONLY_OPS = frozenset(op.lower() for op in [
    # futures_usds - account
    "accountInformation", "accountInformationV2", "accountInformationV3",
    "futuresAccountBalance", "futuresAccountBalanceV2", "futuresAccountBalanceV3",
    "futuresAccountConfiguration", "futuresTradingQuantitativeRulesIndicators", "getBnbBurnStatus",
    "getCurrentMultiAssetsMode", "getCurrentPositionMode", "getDownloadIdForFuturesOrderHistory",
    "getDownloadIdForFuturesTradeHistory", "getDownloadIdForFuturesTransactionHistory",
    "getFuturesOrderHistoryDownloadLinkById", "getFuturesTradeDownloadLinkById",
    "getFuturesTransactionHistoryDownloadLinkById", "getIncomeHistory", "notionalAndLeverageBrackets",
    "queryUserRateLimit", "symbolConfiguration", "userCommissionRate",
    "classicPortfolioMarginAccountInformation",
    # futures - market data
    "adlRisk", "assetIndex", "multiAssetsModeAssetIndex", "basis", "checkServerTime",
    "compositeIndexSymbolInformation", "compressedAggregateTradesList", "continuousContractKlineCandlestickData",
    "exchangeInformation", "getFundingRateHistory", "getFundingRateInfo", "indexPriceKlineCandlestickData",
    "klineCandlestickData", "longShortRatio", "markPrice", "markPriceKlineCandlestickData", "oldTradesLookup",
    "openInterest", "openInterestStatistics", "orderBook", "premiumIndexKlineData",
    "quarterlyContractSettlementPrice", "queryIndexPriceConstituents", "queryInsuranceFundBalanceSnapshot",
    "recentTradesList", "rpiOrderBook", "symbolOrderBookTicker", "symbolPriceTicker", "symbolPriceTickerV2",
    "takerBuySellVolume", "testConnectivity", "ticker24hrPriceChangeStatistics",
    "topTraderLongShortRatioAccounts", "topTraderLongShortRatioPositions", "tradingSchedule",
    # futures - trade (queries only)
    "accountTradeList", "allOrders", "currentAllAlgoOpenOrders", "currentAllOpenOrders",
    "futuresTradfiPerpsContract", "getOrderModifyHistory", "getPositionMarginChangeHistory",
    "positionAdlQuantileEstimation", "positionInformation", "positionInformationV2", "positionInformationV3",
    "queryAlgoOrder", "queryAllAlgoOrders", "queryCurrentOpenOrder", "queryOrder", "testOrder",
    "usersForceOrders",
    # spot
    "depth", "exchangeInfo", "getAccount", "getOpenOrders", "getOrder", "getAllOrders", "klines", "myTrades",
    "ticker24hr", "tickerPrice", "uiKlines", "avgPrice", "bookTicker", "ticker", "tickerTradingDay",
    "trades", "historicalTrades", "aggTrades", "ping", "time",
    # margin
    "crossMarginCollateralRatio", "getAllIsolatedMarginSymbol", "getAllMarginAssets",
    "queryCrossMarginAccountDetails", "queryMarginAccountsAllOrders", "queryMarginAccountsOpenOrders",
    "queryMarginAccountsOrder", "queryMarginAccountsTradeList", "queryMaxBorrow",
    # wallet / sub-account / convert / analysis
    "accountStatus", "allCoinsInformation", "dailyAccountSnapshot", "depositAddress", "depositHistory",
    "queryUserUniversalTransferHistory", "queryUserWalletBalance", "withdrawHistory", "getApiKeyPermission",
    "systemStatus", "getMainAccountAsset", "getConvertTradeHistory", "listAllConvertPairs", "orderStatus",
    "queryLimitOpenOrders", "queryOrderQuantityPrecisionPerAsset", "getTokenAiReport",
])
BINANCE_REDUCE_ONLY_ORDER_OPS = {"neworder", "newalgoorder"}
BINANCE_BATCH_ORDER_OPS = {"placemultipleorders"}

# -----------------------------------------------------------------------------
# run_command classification
# -----------------------------------------------------------------------------
SHELL_SEPARATORS = {"&&", "||", ";", "|", "&", "\n", ";;", "|&", "(", ")"}
REDIRECT_TOKENS = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
SHELL_PUNCTUATION = "();<>|&\n"
# bash operators, longest first: shlex returns punctuation runs (')>', ';>', '<>') as one token
SHELL_OPERATORS = ("&>>", "<<<", "&&", "||", ";;", "|&", ">>", ">|", ">&", "&>", "<>", "<<", "<&",
                   "(", ")", ";", "|", "&", "\n", ">", "<")
INSPECTION_PROGRAMS = {
    "git", "gh", "grep", "rg", "cat", "ls", "find", "diff", "pytest", "cp", "rm", "mkdir", "chmod",
    "echo", "printf", "head", "tail", "less", "wc", "stat", "file", "jq", "sort", "uniq", "awk",
    "sed", "more", "nl", "cut", "tr", "od", "xxd", "strings",
}
BENIGN_PROGRAMS = {"cd", "pushd", "popd", "pwd", "true", "date", "sleep"}
COMMAND_WRAPPERS = {"env", "nohup", "time", "exec", "nice", "stdbuf", "sudo", "command", "builtin"}
SHELL_INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
WRITE_PROGRAMS = {"cp", "mv", "rm", "tee", "truncate", "ln", "chmod", "chown", "install", "dd", "rsync",
                  "unlink", "shred", "touch"}

TRADE_ENGINE_RE = re.compile(r"\bexecute_futures_trade(?:\.py)?\b")
# Executor modes that never open a position (dispatched by the engine before the trade path)
EXECUTOR_MOVE_BREAKEVEN_FLAGS = {"--move-breakeven", "--move_breakeven"}
EXECUTOR_READ_ONLY_FLAGS = {"--positions"}
# Background position guardian (trailing stops, dead alpha, orphan audit): risk-reducing only
GUARDIAN_LOOP_RE = re.compile(r"\bposition_guardian_loop(?:\.py)?\b")
GUARDIAN_BOUNDED_FLAGS = {"--once", "--dry-run", "--dry_run", "--help", "-h"}
DEPLOY_BATCH_RE = re.compile(r"\bdeploy_[A-Za-z0-9_]+\.py\b")
AUTO_DEPLOY_LOOP_RE = re.compile(r"\bclimax_watcher_loop(?:\.py)?\b")
RECORD_EVALUATION_RE = re.compile(r"\brecord_evaluation(?:\.py)?\b")
USER_PROFILE_SET_RE = re.compile(r"\buser_profile(?:\.py)?\b.*\s--(?:set|setup)\b", re.IGNORECASE)

# Scripts whose CLI implements the risk-reducing flags / --help with argparse
RISK_FLAG_SCRIPTS_RE = re.compile(
    r"\b(?:execute_futures_trade|trading_doctor|night_cutoff_loop|record_evaluation|climax_watcher_loop|user_profile)(?:\.py)?\b"
)
RISK_REDUCING_FLAGS = {"--close-position", "--close_position", "--auto-heal", "--auto_heal",
                       "--audit-orphans", "--audit_orphans", "--heal", "--protect-pending", "--protect_pending"}
RISK_REDUCING_SCRIPTS_RE = re.compile(
    r"\b(?:night_cutoff_loop|audit_orphan_positions|close_position_market|close_position)\.py\b"
)

# Trading primitives that must never appear in inline code (python -c, heredocs, piped interpreters)
INLINE_TRADING_PRIMITIVES_RE = re.compile(
    r"execute_futures_trade|send_signed_request|send_mcp_gateway_request|call_binance_mcp|place_algo_stop_loss|"
    r"setup_margin_and_leverage|/fapi/v1/(?:order|batchOrders|algoOrder|leverage|marginType|positionSide|positionMargin)\b|"
    r"agent\.binance\.com",
    re.IGNORECASE,
)
# Primitives that mark a script file as order-capable
SCRIPT_TRADING_PRIMITIVES_RE = re.compile(
    r"\bimport\s+execute_futures_trade\b|\bfrom\s+execute_futures_trade\s+import\b|send_signed_request|"
    r"send_mcp_gateway_request|call_binance_mcp|place_algo_stop_loss|"
    r"/fapi/v1/(?:order|batchOrders|algoOrder|leverage|marginType)\b|futures_usds\.newOrder|agent\.binance\.com"
)
# Order-placing endpoints / helpers (content written by file tools)
WRITE_ENDPOINT_PRIMITIVES_RE = re.compile(
    r"/fapi/v1/(?:order|batchOrders|algoOrder|leverage|marginType)\b|place_algo_stop_loss\s*\(|"
    r"send_mcp_gateway_request\s*\(|futures_usds\.newOrder"
)
INLINE_PYTHON_RE = re.compile(r"\bpython[0-9.]*(?:\.exe)?['\"]?(?:\s+-[A-Za-z]+)*\s+-[A-Za-z]*c\b", re.IGNORECASE)
STDIN_PYTHON_RE = re.compile(r"\bpython[0-9.]*(?:\.exe)?['\"]?(?:\s+-[A-Za-z]+)*\s+-(?:\s|$)", re.IGNORECASE)
PIPE_TO_INTERPRETER_RE = re.compile(
    r"\|\s*(?:sudo\s+)?(?:python[0-9.]*|sh|bash|zsh|dash|node|perl|ruby)\b(?!\s+[^\s|;&-][^\s|;&]*\.(?:py|sh|js|pl|rb)\b)",
    re.IGNORECASE,
)
# `eval` as a shell word only (not inside flags such as --bypass-eval-gate)
OTHER_INLINE_RE = re.compile(r"\b(?:node|perl|ruby)\s+-e\b|\b(?:sh|bash|zsh)\s+-c\b|(?<![\w-])eval(?![\w-])", re.IGNORECASE)
BASE64_EXEC_RE = re.compile(r"base64\s+(?:-d|--decode|-D)\b.*\|\s*(?:python[0-9.]*|sh|bash|zsh|node|perl)\b", re.IGNORECASE)

BINANCE_HOST_RE = re.compile(
    r"(?:[a-z0-9-]*fapi[a-z0-9.-]*|[a-z0-9-]*dapi[a-z0-9.-]*|api[a-z0-9-]*|agent)\.binance\.com|binancefuture\.com",
    re.IGNORECASE,
)
HTTP_WRITE_RE = re.compile(
    r"(?:-X|--request)\s*['\"]?(?:POST|PUT|DELETE|PATCH)\b|(?:^|\s)(?:-d|--data(?:-raw|-binary|-urlencode)?|-F|--form|--json)\b|"
    r"--post-data|--post-file|--method[=\s]+['\"]?(?:POST|PUT|DELETE|PATCH)\b|\b(?:POST|PUT|DELETE|PATCH)\s+https?://",
    re.IGNORECASE,
)
HTTP_CLIENT_RE = re.compile(r"\b(?:curl|wget|http|https|xh|httpie)\b", re.IGNORECASE)

# Evaluation trail (dossiers + Antigravity brain / Claude Code subagent transcripts used for provenance)
EVALUATION_TRAIL_CMD_RE = re.compile(
    r"latest_dos|logs[\\/]+eval|\bevaluations[\\/]|\.gemini[\\/]+[^\\/\s'\"]+[\\/]+brain\b|"
    r"antigravity[^\\/\s'\"]*[\\/]+brain\b|\bsubagents[\\/]+agent-a[0-9a-f]|"
    r"\.claude[\\/]+projects[\\/]+[^\\/\s'\"]+[\\/]+[^\\/\s'\"]+[\\/]+subagents\b",
    re.IGNORECASE,
)
# Test-only overrides of the transcript roots must never reach the recorder or the executor from the agent:
# they would let a forged transcript outside the runtime's own directory sign a dossier.
TRANSCRIPT_ROOT_OVERRIDE_RE = re.compile(
    r"\b(?:AGY_BRAIN_DIRS|CLAUDE_PROJECTS_DIRS)\b(?:['\"]\]?)?\s*=|\b(?:AGY_BRAIN_DIRS|CLAUDE_PROJECTS_DIRS)['\"]\s*[,:]"
)
# Ground-truth runtime state: each file gates PROD orders and has exactly one sanctioned writer, a desk script
# that writes it from Python (atomic_write_json), never through a shell command or a file tool.
GROUND_TRUTH_FILES = {
    "logs/session_state.json": "`python3 scripts/sync_session_state.py`",
    "logs/guardian_state.json": "`python3 scripts/loops/position_guardian_loop.py`",
    "logs/pending_entries.json": "`python3 scripts/execute_futures_trade.py` (resting-entry registration and --protect-pending)",
}
GROUND_TRUTH_BASENAMES = {path.rsplit("/", 1)[-1].lower(): path for path in GROUND_TRUTH_FILES}
GROUND_TRUTH_RE = re.compile("|".join(re.escape(n) for n in GROUND_TRUTH_BASENAMES), re.IGNORECASE)
GROUND_TRUTH_TARGET_RE = re.compile(
    r"(?:^|/)logs/(" + "|".join(re.escape(n) for n in GROUND_TRUTH_BASENAMES) + r")$", re.IGNORECASE
)
SHELL_GLOB_RE = re.compile(r"[*?\[{]")
# A shell sub-command that names a protected file (literally or through a logs/ glob / brace word) is denied unless
# its program is one of these read-only tools (plus the special cases in _ground_truth_read_only).
GROUND_TRUTH_READ_PROGRAMS = {"cat", "head", "tail", "less", "more", "grep", "egrep", "rg", "jq", "wc", "stat",
                              "ls", "file", "diff", "cmp", "md5sum", "sha256sum"}
# Programs that destroy the logs/ directory itself (rm -rf logs, shred -u logs/*)
LOGS_DIR_DESTRUCTIVE_PROGRAMS = {"rm", "shred", "unlink", "truncate"}
# Programs that can overwrite files inside logs/ with sources whose names are not visible (cp -r src/. logs)
LOGS_DIR_COPY_PROGRAMS = {"cp", "rsync", "install"}
# Programs accepting -t DIR / --target-directory=DIR (GNU coreutils)
TARGET_DIR_PROGRAMS = {"cp", "mv", "install", "ln"}
# git sub-commands that never modify working-tree files (anything else naming a protected file is denied)
GIT_READ_SUBCOMMANDS = {"status", "diff", "log", "show", "grep", "blame", "commit", "add", "ls-files", "ls-tree",
                        "check-ignore", "rev-parse", "branch", "shortlog", "describe", "cat-file", "fetch", "push"}
GIT_GLOBAL_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
# Options that turn an allowlisted read into a write or a command run (git log --output=F, git grep -O<cmd>,
# git -c core.fsmonitor=<cmd>, rg --pre CMD, less -o F); leading VAR= assignments (LESSOPEN, GIT_*) too.
GIT_RUN_GLOBAL_OPTIONS = ("--config-env", "--exec-path")
# Sub-command long options that write a file or run a command -> shortest abbreviation counted. git's parse-options
# accepts any unique prefix (--op= for --open-files-in-pager, --upl=, --rece=, --exe=), so every prefix counts
# (config-env from 4 letters: --co/--con abbreviate --contains/--color...). Longer spellings count too (--output-*).
GIT_RUN_LONG_OPTIONS = {"output": 1, "open-files-in-pager": 1, "ext-diff": 1, "upload-pack": 1, "receive-pack": 1,
                        "exec": 1, "exec-path": 1, "config-env": 4}
# ...of which these run a shell command (their value, or the pager on matched files); the last three need a value
GIT_EXEC_LONG_OPTIONS = ("open-files-in-pager", "upload-pack", "receive-pack", "exec")
GIT_EXEC_VALUE_OPTIONS = ("upload-pack", "receive-pack", "exec")
# Short clusters with O (git grep -O<cmd>, -nOrm; diff -O<orderfile>) or, for diff/log/show, o void the exemption
GIT_LOWER_O_SUBCOMMANDS = {"diff", "log", "show"}
RG_EXEC_OPTIONS = ("--pre", "--hostname-bin")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Windows / PowerShell programs that delete or move directories (cmd //c rd /s /q logs, Remove-Item -Recurse logs)
WINDOWS_DELETE_PROGRAMS = {"rd", "rmdir", "del", "erase", "remove-item", "ri"}
WINDOWS_MOVE_PROGRAMS = {"move", "ren", "rename", "move-item", "mi", "rename-item", "rni"}
WINDOWS_SWITCH_RE = re.compile(r"^/[A-Za-z?](?::\S*)?$")
SHELL_VALUE_OPTIONS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
NESTED_DEPTH_LIMIT = 6
# Command substitutions that expand to the working directory / repo root, or that prefix a path ($(x)/logs)
CWD_SUBSTITUTION_RE = re.compile(
    r"\$\(\s*(?:pwd(?:\s+-[LP])?|git\s+rev-parse\s+--show-toplevel)\s*\)|"
    r"`\s*(?:pwd(?:\s+-[LP])?|git\s+rev-parse\s+--show-toplevel)\s*`|\$\{PWD\}|\$PWD(?![A-Za-z0-9_])"
)
HOME_VAR_RE = re.compile(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])")
PATH_SUBSTITUTION_RE = re.compile(r"\$\(([^()\n]*)\)(?=/)|`([^`\n]*)`(?=/)")
FIND_DELETE_ACTIONS = {"-delete"}
FIND_OUTPUT_ACTIONS = {"-fprint", "-fprint0", "-fprintf", "-fls"}
FIND_EXEC_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir"}
FIND_NAME_FILTERS = {"-name", "-iname"}
FIND_PATH_FILTERS = {"-path", "-ipath", "-wholename", "-iwholename"}
HEREDOC_RE = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
# Heredoc bodies are dropped from the line-by-line check only when they feed python/node (judged by markers)
CODE_INTERPRETER_PROGRAM_RE = re.compile(r"^(?:python[0-9.]*|node|nodejs)$", re.IGNORECASE)
PIPE_TO_CODE_INTERPRETER_RE = re.compile(
    r"^[^|;&\n]*\|\s*(?:python[0-9.]*|node)\b(?!\s+[^\s|;&-][^\s|;&]*\.(?:py|js)\b)", re.IGNORECASE)
HARNESS_PATH_CMD_RE = re.compile(
    r"scripts[\\/]+hooks[\\/]|\.agents[\\/]+hooks\.json|dossier_provenance\.py|record_evaluation\.py|"
    r"\.agents[\\/]+agents[\\/]|\.claude[\\/]+agents[\\/]|\.claude[\\/]+settings|config[\\/]+user_profile\.json",
    re.IGNORECASE,
)
INLINE_WRITE_MARKERS_RE = re.compile(
    r"\.write\s*\(|write_text\s*\(|write_bytes\s*\(|dump\s*\(|open\s*\([^)]*['\"][rwabxt+]*[wax+][rwabxt+]*['\"]|"
    r"os\.(?:remove|unlink|replace|rename|truncate|system|popen|open|symlink|link)\b|\.unlink\s*\(|\.rename\s*\(|"
    r"\.replace\s*\(|\.touch\s*\(|\.(?:symlink|hardlink|link)_to\s*\(|\bO_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND)\b|"
    r"FileIO\s*\(|shutil\.|subprocess\.|\.system\s*\(|\.popen\s*\(|\bexec\s*\(|__import__|"
    r"\bos\.(?:exec|spawn|posix_spawn)\w*|\bpty\.|"
    r"writeFileSync|writeFile\s*\(|unlinkSync|rmSync|openSync|"
    r"(?:\bfs|require\s*\(\s*['\"](?:node:)?fs(?:/promises)?['\"]\s*\))\.(?:rm|rmdir|write\w*|open\w*)\s*\(|"
    r"\b(?:rm|rmdir|rmdirSync|writeSync)\s*\(|"
    r"\b(?:appendFile|copyFile|rename|symlink|truncate|cp)(?:Sync)?\s*\(|createWriteStream",
    re.IGNORECASE,
)
# Inline code acting on the logs/ directory itself (shutil.rmtree('logs'), fs.rmSync('logs', {recursive: true})):
# the destructive markers apply to a 'logs' string literal (or a logs/ glob reaching a protected file); the strong
# markers also apply to literals naming an ancestor of logs/ ('.', '..', '/', the repo root) or a glob ('*', 'log*').
INLINE_STRING_LITERAL_RE = re.compile(r"(['\"])([^'\"\s]+)\1")
INLINE_LOGS_DIR_MARKERS_RE = re.compile(
    r"rmtree|removedirs|\brename\w*\s*\(|\.replace\s*\(|\bremove\s*\(|unlink|\brm(?:dir)?(?:Sync)?\s*\(|rmSync|"
    r"symlink|\.(?:hardlink|symlink)_to\s*\(|\blink(?:Sync)?\s*\(|shutil\.(?:move|copytree)|\bcp(?:Sync)?\s*\(|copyFile",
    re.IGNORECASE,
)
INLINE_ANCESTOR_MARKERS_RE = re.compile(
    r"rmtree|removedirs|rmSync|\brm\s*\([^)]*recursive|shutil\.move|\brename(?:s|Sync)?\s*\(", re.IGNORECASE
)

# File-tool targets
HARNESS_FILES = {
    ".agents/hooks.json", "scripts/utils/dossier_provenance.py", "scripts/record_evaluation.py",
    ".claude/settings.json", ".claude/settings.local.json", "config/user_profile.json",
}
HARNESS_DIRS = ("scripts/hooks/", ".agents/agents/", ".claude/agents/")
BRAIN_PATH_RE = re.compile(r"(?:^|/)\.gemini/[^/]+/brain(?:/|$)", re.IGNORECASE)
# Claude Code subagent transcripts: <projects>/<slug>/<sessionId>/subagents/agent-<id>.jsonl (+ .meta.json)
CLAUDE_SUBAGENT_PATH_RE = re.compile(r"(?:^|/)subagents/agent-a[0-9a-f]+\.(?:jsonl|meta\.json)$|"
                                     r"(?:^|/)\.claude/projects/[^/]+/[^/]+/subagents(?:/|$)", re.IGNORECASE)


# =============================================================================
# Generic decoding helpers
# =============================================================================
def _decode_value(value: Any, max_depth: int = 3) -> Any:
    """agy may JSON-encode tool-call argument values (e.g. ServerName='"binance"'). Unwraps them."""
    for _ in range(max_depth):
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped or stripped[0] not in "\"{[":
            return value
        try:
            value = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _decode_str(value: Any) -> str:
    value = _decode_value(value)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _decode_dict(value: Any) -> dict:
    value = _decode_value(value)
    return value if isinstance(value, dict) else {}


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _is_true(value: Any) -> bool:
    value = _decode_value(value)
    return value is True or str(value).strip().lower() == "true"


def parse_mcp_arguments(args_dict: dict) -> dict:
    """Extracts dictionary of arguments from tool call args regardless of serialization."""
    if not args_dict:
        return {}
    if "Arguments" in args_dict:
        return _decode_dict(args_dict["Arguments"])
    return args_dict


# =============================================================================
# Tool call normalization (agy / Claude Code)
# =============================================================================
def _split_eager_mcp_name(name: str, args: dict) -> Tuple[str, str]:
    """Splits agy eager MCP names `mcp_<server>_<tool>` (server names may contain underscores)."""
    rest = name[len("mcp_"):]
    explicit_server = _decode_str(_first(args, "ServerName", "serverName", "server_name", "server"))
    if explicit_server and rest.startswith(explicit_server + "_"):
        return explicit_server, rest[len(explicit_server) + 1:]
    for server in sorted(KNOWN_MCP_SERVERS, key=len, reverse=True):
        if rest.startswith(server + "_"):
            return server, rest[len(server) + 1:]
    if "_" in rest:
        server, tool = rest.split("_", 1)
        return server, tool
    return explicit_server, rest


def normalize_tool_call(payload: dict) -> Dict[str, Any]:
    """
    Maps agy and Claude Code payloads onto a common structure:
    {kind: run_command|mcp|file_write|other, tool, command, cwd, server, mcp_tool, mcp_args, raw_args,
     target_file, content}
    """
    call: Dict[str, Any] = {
        "kind": "other", "tool": "", "command": "", "cwd": "", "server": "", "mcp_tool": "",
        "mcp_args": {}, "raw_args": {}, "target_file": "", "content": "",
    }

    if isinstance(payload.get("toolCall"), dict):
        tool_call = payload["toolCall"]
        name = tool_call.get("name", "")
        args = tool_call.get("args", {})
        if not isinstance(args, dict):
            args = _decode_dict(args)
        call["tool"] = name
        call["raw_args"] = args

        if name == "run_command":
            call["kind"] = "run_command"
            call["command"] = _decode_str(_first(args, "CommandLine", "commandLine", "command"))
            call["cwd"] = _decode_str(_first(args, "Cwd", "cwd"))
        elif name in AGY_MCP_CALL_TOOLS:
            call["kind"] = "mcp"
            call["server"] = _decode_str(_first(args, "ServerName", "serverName", "server_name", "server"))
            call["mcp_tool"] = _decode_str(_first(args, "ToolName", "toolName", "tool_name", "tool"))
            call["mcp_args"] = _decode_dict(_first(args, "Arguments", "arguments", "Args", "input"))
        elif name.startswith("mcp_"):
            call["kind"] = "mcp"
            server, tool = _split_eager_mcp_name(name, args)
            call["server"], call["mcp_tool"] = server, tool
            nested = _first(args, "Arguments", "arguments")
            call["mcp_args"] = _decode_dict(nested) if nested is not None else {
                k: _decode_value(v) for k, v in args.items()
                if k not in ("ServerName", "serverName", "toolAction", "toolSummary")
            }
        elif BINANCE_NAMESPACE_RE.match(name) or BINANCE_VERB_RE.match(name):
            # Eagerly loaded Binance tool called by its bare name
            call["kind"] = "mcp"
            call["server"] = "binance"
            call["mcp_tool"] = name
            nested = _first(args, "Arguments", "arguments")
            call["mcp_args"] = _decode_dict(nested) if nested is not None else {k: _decode_value(v) for k, v in args.items()}
        elif name in FILE_WRITE_TOOLS:
            call["kind"] = "file_write"
            call["target_file"] = _decode_str(_first(args, "TargetFile", "targetFile", "AbsolutePath", "file_path", "path"))
            call["content"] = json.dumps(args, ensure_ascii=False)
        return call

    # Claude Code PreToolUse contract
    name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    call["tool"] = name
    call["raw_args"] = tool_input
    if name == "Bash":
        call["kind"] = "run_command"
        call["command"] = _decode_str(tool_input.get("command"))
        call["cwd"] = _decode_str(payload.get("cwd"))
    elif isinstance(name, str) and name.startswith("mcp__"):
        parts = name.split("__")
        call["kind"] = "mcp"
        call["server"] = parts[1] if len(parts) > 1 else ""
        call["mcp_tool"] = "__".join(parts[2:]) if len(parts) > 2 else ""
        call["mcp_args"] = {k: _decode_value(v) for k, v in tool_input.items()}
    elif name in CLAUDE_FILE_WRITE_TOOLS:
        call["kind"] = "file_write"
        call["target_file"] = _decode_str(_first(tool_input, "file_path", "notebook_path", "path"))
        call["content"] = json.dumps(tool_input, ensure_ascii=False)
    return call


# =============================================================================
# Argument extraction helpers
# =============================================================================
def is_risk_reducing_action(cmd_or_name: str, args_dict: dict = None) -> bool:
    """
    Verifies whether an action reduces or eliminates risk (NEVER blocked).
    Structured arguments ONLY:
    - MCP: reduceOnly=true, closePosition=true, or cancel / delete operations
    - CLI: exact --close-position / --auto-heal / --audit-orphans / --protect-pending / --heal / --help tokens on scripts whose CLI
      implements them, `execute_futures_trade.py --move-breakeven` with exactly one --symbol, bounded position
      guardian runs (--once / --dry-run), or dedicated risk-reduction scripts. Batch deploy scripts are never
      risk-reducing.
    Never relies on generic substring 'close' across the command line.
    """
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        if _is_true(_first(mcp_args, "reduceOnly", "reduce_only", "reduce-only")):
            return True
        if _is_true(_first(mcp_args, "closePosition", "close_position", "close-position")):
            return True
        mcp_tool = _decode_str(args_dict.get("ToolName", "")).lower()
        op = mcp_tool.rsplit(".", 1)[-1]
        if "cancel" in op or op.startswith("delete"):
            return True

    if cmd_or_name:
        return _subcommand_is_risk_reducing(_tokenize_subcommand(cmd_or_name), cmd_or_name)
    return False


def extract_target_symbol(cmd: str, args_dict: dict = None) -> str:
    """Extracts target symbol from tool arguments or shell command line."""
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        if "symbol" in mcp_args and mcp_args["symbol"]:
            return str(_decode_value(mcp_args["symbol"])).upper().strip()
        if "symbol" in args_dict and args_dict["symbol"]:
            return str(_decode_value(args_dict["symbol"])).upper().strip()

    if cmd:
        symbols = {m.group(2).upper().strip() for m in re.finditer(r"--symbol(?:\s+|=)(['\"]?)([A-Za-z0-9_]+)\1", cmd)}
        if len(symbols) == 1:
            return symbols.pop()
        if len(symbols) > 1:
            return ""
        m2 = re.search(r"['\"]([A-Z0-9]+USDT)['\"]", cmd)
        if m2:
            return m2.group(1).upper().strip()
    return ""


def extract_env_argument(cmd: str, args_dict: dict = None) -> Optional[str]:
    """
    Extracts explicit environment parameter from tool args or command line.
    Shell assignments (BINANCE_API_ENV=prod, export BINANCE_API_ENV=prod) are honored as well.
    If several values are present and any of them is PROD, PROD wins (fail-safe).
    """
    found: List[str] = []
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        for key in ["target_env", "env", "TARGET_ENV", "BINANCE_API_ENV"]:
            if key in args_dict and args_dict[key] and not isinstance(args_dict[key], (dict, list)):
                found.append(_decode_str(args_dict[key]).strip())
            if key in mcp_args and mcp_args[key] and not isinstance(mcp_args[key], (dict, list)):
                found.append(_decode_str(mcp_args[key]).strip())

    if cmd:
        for m in re.finditer(r"--env(?:\s+|=)(['\"]?)([A-Za-z0-9_-]+)\1", cmd, re.IGNORECASE):
            found.append(m.group(2).strip())
        for m in re.finditer(r"\b(?:BINANCE_API_ENV|TARGET_ENV)=(['\"]?)([A-Za-z0-9_-]+)\1", cmd):
            found.append(m.group(2).strip())

    if not found:
        return None
    for val in found:
        if val.lower() in ("prod", "production", "mainnet"):
            return val
    return found[0]


def parse_trade_direction(cmd: str, args_dict: dict = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Strictly parses trade direction ('LONG' or 'SHORT').
    Returns (direction, error_reason).
    If both or neither match, returns (None, error_reason) for fail-closed rejection.
    """
    found_dirs = set()

    # 1. Check structured MCP arguments
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        dir_val = _decode_str(mcp_args.get("direction", "")).upper().strip()
        side_val = _decode_str(mcp_args.get("side", "")).upper().strip()

        if dir_val in ["LONG", "BUY"]:
            found_dirs.add("LONG")
        elif dir_val in ["SHORT", "SELL"]:
            found_dirs.add("SHORT")
        elif dir_val:
            return None, f"Invalid direction value '{dir_val}' in tool arguments."

        if side_val in ["BUY", "LONG"]:
            found_dirs.add("LONG")
        elif side_val in ["SELL", "SHORT"]:
            found_dirs.add("SHORT")
        elif side_val:
            return None, f"Invalid side value '{side_val}' in tool arguments."

        if len(found_dirs) == 1:
            return next(iter(found_dirs)), None
        elif len(found_dirs) > 1:
            return None, "Conflicting trade directions detected in tool arguments (both LONG and SHORT)."

    # 2. Check structured command line flags: --dir, --direction, --side
    if cmd:
        flags_found = set()
        for m in re.finditer(r"--(?:dir(?:ection)?|side)(?:\s+|=)(['\"]?)(LONG|SHORT|BUY|SELL)\1\b", cmd, re.IGNORECASE):
            val = m.group(2).upper()
            if val in ["LONG", "BUY"]:
                flags_found.add("LONG")
            elif val in ["SHORT", "SELL"]:
                flags_found.add("SHORT")

        if not flags_found:
            # Check for quoted standalone words 'LONG' or 'SHORT' (e.g. positional script args)
            quoted_long = bool(re.search(r"['\"](LONG|BUY)['\"]", cmd, re.IGNORECASE))
            quoted_short = bool(re.search(r"['\"](SHORT|SELL)['\"]", cmd, re.IGNORECASE))
            if quoted_long:
                flags_found.add("LONG")
            if quoted_short:
                flags_found.add("SHORT")

        if len(flags_found) == 1:
            return next(iter(flags_found)), None
        elif len(flags_found) > 1:
            return None, "Conflicting trade directions detected in command line (both LONG and SHORT matched)."

    return None, "Unable to determine trade direction strictly (neither LONG nor SHORT found)."


# =============================================================================
# Dossier gate (single source of truth: scripts/utils/dossier_provenance.py)
# =============================================================================
def check_dossier(symbol: str, direction: Optional[str], env: str, base_dir: str,
                  conversation_id: Optional[str] = None, now_ts: Optional[int] = None) -> Tuple[bool, str, Optional[dict]]:
    """Validates the evaluator dossier for a trade and, in PROD, binds it to the current conversation."""
    if dp is None:
        return False, "Dossier provenance module (scripts/utils/dossier_provenance.py) is unavailable.", None
    ok, reason, cand = dp.validate_dossier_for_trade(
        symbol, direction, env, base_dir=base_dir, now_ts=now_ts if now_ts is not None else int(time.time())
    )
    if not ok:
        return False, reason, None

    if env != "prod":
        return True, reason, cand

    # PROD: the provenance hash only binds the raw <dossier_json> block. Re-derive the verdict from the
    # evaluator transcript itself so a tampered approved list / parent id in the JSON file cannot widen it.
    try:
        record = dp.load_dossier(dp.default_dossier_path(base_dir))
        rebuilt = dp.build_record_from_extraction(dp.extract_recorded_transcript(record))
    except Exception as e:
        return False, f"Failed to re-derive the dossier from the evaluator transcript ({e}).", None
    rebuilt_cand = dp.find_candidate(rebuilt, symbol) if rebuilt.get("status") == "APPROVED" else None
    if rebuilt_cand is None or (direction and rebuilt_cand.get("direction") != str(direction).upper()):
        return False, (
            f"Evaluator transcript does not approve {symbol} {direction or ''}".rstrip()
            + " (the recorded dossier differs from what the evaluator emitted)."
        ), None
    cand = rebuilt_cand

    if conversation_id:
        parent = rebuilt.get("parent_conversation_id")
        if parent and str(parent) != str(conversation_id):
            return False, (
                f"Evaluation dossier was produced for conversation '{parent}', not for the current "
                f"conversation '{conversation_id}'. Re-run the evaluator from this conversation."
            ), None
    return True, reason, cand


def _candidate_is_yolo(cand: Optional[dict]) -> bool:
    if not isinstance(cand, dict):
        return False
    tier = str(cand.get("tier", "")).lower()
    strategy = str(cand.get("strategy", "")).lower()
    return _is_true(cand.get("is_yolo")) or "yolo" in tier or "yolo" in strategy


def leverage_limits(user_prof: dict) -> Tuple[int, int, int]:
    """
    Returns (standard, ceiling, yolo_cap), aligned with execute_futures_trade.check_mechanical_gates:
    the absolute ceiling comes from user_profile.get_leverage_ceiling() (default 15x, max 125x) and the
    YOLO cap from profile `leverage_yolo`, clamped to [1, ceiling].
    """
    user_prof = user_prof or {}
    try:
        import user_profile as up
        ceiling = int(up.get_leverage_ceiling(user_prof))
    except Exception:
        ceiling = 15
    try:
        std_lev = int(user_prof.get("leverage_standard", 3))
    except (TypeError, ValueError):
        std_lev = 3
    try:
        yolo_cap = int(user_prof.get("leverage_yolo", ceiling))
    except (TypeError, ValueError):
        yolo_cap = ceiling
    yolo_cap = min(max(yolo_cap, 1), ceiling)
    return min(std_lev, ceiling), ceiling, yolo_cap


def check_leverage_bounds(requested_leverage: int, user_prof: dict, is_yolo: bool) -> Optional[str]:
    """Absolute ceiling / YOLO cap check. Returns a denial reason or None."""
    std_lev, ceiling, yolo_cap = leverage_limits(user_prof)
    if requested_leverage < 1:
        return f"Invalid leverage {requested_leverage}x. Must be >= 1x."
    if requested_leverage > ceiling:
        return f"Leverage {requested_leverage}x exceeds absolute desk ceiling of {ceiling}x (profile leverage_ceiling)."
    if (is_yolo or requested_leverage > std_lev) and requested_leverage > yolo_cap:
        return f"Leverage {requested_leverage}x exceeds the YOLO leverage limit ({yolo_cap}x, profile leverage_yolo)."
    return None


def check_leverage_gate(symbol: str, requested_leverage: int, base_dir: str, user_prof: dict = None,
                        target_env: Optional[str] = None, conversation_id: Optional[str] = None) -> Tuple[bool, str]:
    """
    Validates leverage changes against the profile limits (leverage_standard, leverage_yolo and the absolute
    ceiling from user_profile.get_leverage_ceiling()) and the approved YOLO status in the dossier.
    """
    if user_prof is None:
        try:
            import user_profile as up
            user_prof = up.load_user_profile(base_dir=base_dir)
        except Exception:
            user_prof = {}

    prefix = "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): "
    bounds_err = check_leverage_bounds(requested_leverage, user_prof, is_yolo=False)
    if bounds_err:
        return False, prefix + bounds_err

    std_lev, _ceiling, _yolo_cap = leverage_limits(user_prof)
    if requested_leverage <= std_lev:
        return True, f"Standard leverage (<= {std_lev}x) authorized."

    env = resolve_env(target_env, base_dir=base_dir)
    dossier_file = os.path.join(base_dir, "logs", "evaluations", "latest_dossier.json")
    if not os.path.exists(dossier_file):
        return False, prefix + f"Requested leverage ({requested_leverage}x > {std_lev}x) exceeds standard ceiling and no evaluation dossier exists."

    # A leverage change has no direction: bind it to the direction approved for the symbol.
    cand_direction = None
    try:
        record = dp.load_dossier(dossier_file) if dp is not None else {}
        cand_direction = (dp.find_candidate(record, symbol) or {}).get("direction") if dp is not None else None
    except Exception as e:
        return False, prefix + f"Failed to read evaluation dossier ({e})."

    ok, reason, cand = check_dossier(symbol, cand_direction, env, base_dir, conversation_id)
    if not ok:
        return False, prefix + reason

    is_yolo_authorized = _candidate_is_yolo(cand)
    if cand and not is_yolo_authorized:
        try:
            is_yolo_authorized = int(cand.get("leverage", 3)) >= requested_leverage
        except (TypeError, ValueError):
            is_yolo_authorized = False
    try:
        record = dp.load_dossier(dossier_file)
        if symbol.upper() in [str(s).upper() for s in record.get("yolo_approved_symbols", []) or []]:
            is_yolo_authorized = True
    except Exception:
        pass

    if not is_yolo_authorized:
        return False, prefix + (
            f"Requested leverage ({requested_leverage}x > {std_lev}x) for '{symbol}' is not authorized as a YOLO moonshot in the evaluation dossier."
        )

    if not user_prof.get("yolo_slot_enabled", False):
        return False, prefix + f"Requested leverage ({requested_leverage}x > {std_lev}x) requires YOLO status, but YOLO moonshot slot is disabled in user profile."

    return True, f"YOLO moonshot leverage ({requested_leverage}x) authorized for '{symbol}' in evaluation dossier."


# =============================================================================
# Shell command analysis
# =============================================================================
def _split_operators(tok: str) -> List[str]:
    """Splits a shlex punctuation run into bash operators: ')>' -> ')', '>'; ';>' -> ';', '>'; '&&\\n' -> '&&', '\\n'."""
    if not tok or any(c not in SHELL_PUNCTUATION for c in tok):
        return [tok]
    out: List[str] = []
    i = 0
    while i < len(tok):
        op = next((o for o in SHELL_OPERATORS if tok.startswith(o, i)), tok[i])
        out.append(op)
        i += len(op)
    return out


def _tokenize(command_line: str) -> List[str]:
    try:
        lexer = shlex.shlex(command_line, posix=True, punctuation_chars=SHELL_PUNCTUATION)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return [part for tok in lexer for part in _split_operators(tok)]
    except Exception:
        return re.split(r"\s+|(?=[;&|<>\n])|(?<=[;&|<>\n])", command_line)


def split_subcommands(command_line: str) -> List[List[str]]:
    """Quote-aware split of compound shell commands (&&, ||, ;, |, &, newlines)."""
    subcommands: List[List[str]] = []
    current: List[str] = []
    for tok in _tokenize(command_line):
        if not tok:
            continue
        if tok in SHELL_SEPARATORS:
            if current:
                subcommands.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        subcommands.append(current)
    return subcommands


def _tokenize_subcommand(cmd: str) -> List[str]:
    subs = split_subcommands(cmd)
    return [t for s in subs for t in s]


def _program_index(tokens: List[str]) -> int:
    """Index of the executed program, skipping VAR=value assignments and wrappers (env, nohup, timeout...)."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = os.path.basename(tok).lower()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
        elif base in COMMAND_WRAPPERS or base == "timeout" or base == "xargs":
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or re.match(r"^\d+[smhd]?$", tokens[i])):
                i += 1
        elif base == "uv" and i + 1 < len(tokens) and tokens[i + 1] == "run":
            i += 2
        else:
            return i
    return len(tokens)


def _program(tokens: List[str]) -> str:
    idx = _program_index(tokens)
    return os.path.basename(tokens[idx]).lower() if idx < len(tokens) else ""


def _flags(tokens: List[str]) -> set:
    return {tok.split("=", 1)[0].lower() for tok in tokens if tok.startswith("-")}


def _symbol_count(text: str) -> int:
    return len({m.group(2).upper() for m in re.finditer(r"--symbol(?:\s+|=)(['\"]?)([A-Za-z0-9_]+)\1", text)})


def _subcommand_is_risk_reducing(tokens: List[str], text: str) -> bool:
    if not tokens:
        return False
    if DEPLOY_BATCH_RE.search(text):
        return False
    if RISK_REDUCING_SCRIPTS_RE.search(text):
        return True
    flags = _flags(tokens)
    if GUARDIAN_LOOP_RE.search(text) and not TRADE_ENGINE_RE.search(text):
        # The guardian never opens positions; only bounded runs are auto-allowed.
        return bool(flags & GUARDIAN_BOUNDED_FLAGS)
    if not RISK_FLAG_SCRIPTS_RE.search(text):
        return False
    if flags & RISK_REDUCING_FLAGS or flags & {"--help", "-h"}:
        return True
    if TRADE_ENGINE_RE.search(text) and flags & EXECUTOR_MOVE_BREAKEVEN_FLAGS and _symbol_count(text) == 1:
        return True
    return False


def _is_redirect(tok: str) -> bool:
    """Output redirect operator: >, >>, >|, &>, >&, <> and any other punctuation-only run containing '>'."""
    return tok in REDIRECT_TOKENS or (">" in (tok or "") and all(c in SHELL_PUNCTUATION for c in tok))


def _redirect_targets(tokens: List[str]) -> List[str]:
    targets = []
    for i, tok in enumerate(tokens):
        if _is_redirect(tok) and i + 1 < len(tokens):
            targets.append(tokens[i + 1])
    return targets


def _is_inline_code(command_line: str) -> bool:
    return bool(
        INLINE_PYTHON_RE.search(command_line)
        or STDIN_PYTHON_RE.search(command_line)
        or "<<" in command_line
        or PIPE_TO_INTERPRETER_RE.search(command_line)
        or OTHER_INLINE_RE.search(command_line)
    )


def _subcommand_writes_path(tokens: List[str], text: str, path_re: re.Pattern, inline: bool) -> bool:
    """True when a sub-command can modify a path matched by path_re."""
    if any(path_re.search(t) for t in _redirect_targets(tokens)):
        return True
    prog = _program(tokens)
    if prog in WRITE_PROGRAMS and path_re.search(text):
        return True
    if prog in ("sed", "perl") and any(t.startswith("-i") or t == "--in-place" for t in tokens) and path_re.search(text):
        return True
    if prog == "git" and re.search(r"\bgit\s+(?:checkout|restore|rm|mv|apply|reset)\b", text) and path_re.search(text):
        return True
    if inline and path_re.search(text) and INLINE_WRITE_MARKERS_RE.search(text):
        return True
    return False


# -----------------------------------------------------------------------------
# Ground-truth runtime state (session_state / guardian_state / pending_entries)
# -----------------------------------------------------------------------------
def ground_truth_denial(paths: List[str]) -> str:
    """Denial reason naming each protected file and its sole sanctioned writer (GROUND_TRUTH_FILES order)."""
    keys = [p for p in GROUND_TRUTH_FILES if p in set(paths)]
    return "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Ground Truth Protection): " + "; ".join(
        f"{p} may only be written by {GROUND_TRUTH_FILES[p]}" for p in keys
    ) + "."


def _ground_truth_named(text: str) -> List[str]:
    return [GROUND_TRUTH_BASENAMES[m.group(0).lower()] for m in GROUND_TRUTH_RE.finditer(text or "")]


def _shell_path(word: str) -> str:
    """Forward slashes, no Windows drive prefix, '.', '..' and trailing slashes collapsed."""
    p = re.sub(r"^[A-Za-z]:(?=/|$)", "", (word or "").replace("\\", "/"))
    return posixpath.normpath(p) if p else ""


def _expand_braces(word: str, limit: int = 64) -> List[str]:
    """Minimal bash brace expansion ({a,b}) so logs/{guardian_state,x}.json is seen as two words."""
    m = re.search(r"\{([^{}]*,[^{}]*)\}", word)
    if not m:
        return [word]
    out: List[str] = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(word[:m.start()] + alt + word[m.end():], limit))
        if len(out) >= limit:
            break
    return out[:limit]


def _glob_ground_truth(word: str) -> List[str]:
    """Protected files a glob / brace word inside a logs/ directory can expand to (logs/*.json, logs/*state*)."""
    if not SHELL_GLOB_RE.search(word or ""):
        return []
    hits: List[str] = []
    for expanded in _expand_braces(word):
        head, _, name = _shell_path(expanded).rpartition("/")
        if not head or not fnmatch.fnmatchcase("logs", head.rpartition("/")[2].lower()):
            continue
        hits.extend(key for base, key in GROUND_TRUTH_BASENAMES.items() if fnmatch.fnmatchcase(base, name.lower()))
    return hits


def _is_logs_dir(word: str, cwd: str = "", base_dir: str = "") -> bool:
    """True when a word (braces expanded) names a `logs` directory: last component `logs` (logs, ./logs/,
    /abs/repo/logs) or a glob that can expand to it (log*, lo[g]s, *). A glob with a directory part (build/*) only
    counts when it can expand to the workspace logs/ (resolved against cwd / base_dir)."""
    for expanded in _expand_braces(word or ""):
        sp = _shell_path(expanded)
        head, _, last = sp.rpartition("/")
        last = last.lower()
        if last == "logs":
            return True
        if not (SHELL_GLOB_RE.search(last) and fnmatch.fnmatchcase("logs", last)):
            continue
        if not head or not base_dir or head.startswith("~"):
            return True
        logs = _canon_path(base_dir).rstrip("/") + "/logs"
        if fnmatch.fnmatchcase(logs, _canon_path(sp, cwd or base_dir)):
            return True
    return False


def _word_value(word: str) -> str:
    """Value of option/assignment words (of=..., --output=...), otherwise the word itself."""
    m = re.match(r"^-*[A-Za-z_][A-Za-z0-9_-]*=(.*)$", word)
    return m.group(1) if m else word


def _canon_path(path: str, cwd: str = "") -> str:
    """Lower-case absolute POSIX path without the Windows drive / WSL /mnt/<d> / Git Bash /<d> prefix."""
    p = (path or "").replace("\\", "/")
    m = re.match(r"^(?:/mnt/[A-Za-z]|/[A-Za-z]|[A-Za-z]:)(?=/|$)", p)
    if m:
        p = p[m.end():] or "/"
    if not p.startswith("/"):
        p = (_canon_path(cwd) if cwd else "") + "/" + p
    return "/" + posixpath.normpath(p).lstrip("/").lower()


def _reaches_logs_dir(word: str, cwd: str, base_dir: str) -> bool:
    """True when a word (braces/globs expanded) is the logs dir or one of its ancestors (., .., /, ~, the repo...)."""
    logs = _canon_path(base_dir).rstrip("/") + "/logs" if base_dir else "/logs"
    ancestors = [logs]
    while ancestors[-1] != "/":
        ancestors.append(posixpath.dirname(ancestors[-1]))
    for expanded in _expand_braces(word or ""):
        if not expanded:
            continue
        if _is_logs_dir(expanded, cwd, base_dir):
            return True
        sp = _shell_path(expanded)
        if sp in (".", "..", "/", "~") or sp.endswith("/.."):
            return True
        if sp.startswith("~/"):
            # Home is unknown to the hook: ~/Documents reaches the repo when that segment is one of its ancestors
            pattern = "*/" + sp[2:].lower()
        else:
            pattern = _canon_path(sp, cwd or base_dir)
        if any(fnmatch.fnmatchcase(a, pattern) for a in ancestors):
            return True
    return False


def _plain_args(args: List[str]) -> List[str]:
    """Arguments without redirect operators, their targets and the fd number glued before them (2>&1)."""
    out: List[str] = []
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if _is_redirect(a) or a in ("<", "<<", "<<<", "<&"):
            skip = True
            continue
        if a.isdigit() and i + 1 < len(args) and (_is_redirect(args[i + 1]) or args[i + 1] in ("<", "<&")):
            continue
        out.append(a)
    return out


def _split_operands(prog: str, args: List[str]) -> Tuple[List[str], Optional[str]]:
    """(operands, target directory) for coreutils-style args; -t DIR, -tDIR, -rtDIR, --target-directory[=]DIR."""
    operands: List[str] = []
    target: Optional[str] = None
    skip = end_of_options = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if end_of_options or not a.startswith("-") or a == "-":
            operands.append(a)
            continue
        if a == "--":
            end_of_options = True
            continue
        if prog not in TARGET_DIR_PROGRAMS:
            continue
        m = re.match(r"^--(t[\w-]*)(?:=(.*))?$", a, re.DOTALL)
        if m and "target-directory".startswith(m.group(1)):
            if m.group(2) is not None:
                target = m.group(2)
            elif i + 1 < len(args):
                target, skip = args[i + 1], True
            continue
        if a.startswith("--"):
            continue
        letters = a[1:]
        for j, ch in enumerate(letters):
            if ch == "t":
                if letters[j + 1:]:
                    target = letters[j + 1:]
                elif i + 1 < len(args):
                    target, skip = args[i + 1], True
                break
            if ch in "Smog":  # options taking a value (-S suffix, install -m/-o/-g)
                skip = not letters[j + 1:]
                break
    return operands, target


def _is_recursive(args: List[str]) -> bool:
    return any(re.match(r"^-[A-Za-z]*[rRa]", a) or a in ("--recursive", "--archive") for a in args)


def _exec_writes(args: List[str]) -> bool:
    """True when a find -exec/-ok command line (program + args) can modify files."""
    if not args:
        return False
    prog = os.path.basename(args[0]).lower()
    if prog in GROUND_TRUTH_READ_PROGRAMS:
        return False
    if prog in ("sed", "perl"):
        return any(re.match(r"^-[A-Za-z]*i", t) or t.startswith("--in-place") for t in args[1:])
    return True


def _find_roots(args: List[str]) -> List[str]:
    i = 0
    while i < len(args) and (args[i] in ("-H", "-L", "-P", "-D") or args[i].startswith("-O")):
        i += 2 if args[i] == "-D" else 1
    roots = []
    while i < len(args) and not args[i].startswith("-") and args[i] not in ("(", ")", "!", ","):
        roots.append(args[i])
        i += 1
    return roots or ["."]


def _find_ground_truth(args: List[str], cwd: str = "", base_dir: str = "", depth: int = 0) -> List[str]:
    """Protected files a `find` can write (-fprint ...) or delete (-delete, -exec rm, -exec sh ...)."""
    hits: List[str] = []
    for i, a in enumerate(args):
        if a in FIND_OUTPUT_ACTIONS and i + 1 < len(args):
            hits += _ground_truth_named(args[i + 1]) + _glob_ground_truth(args[i + 1])
    destructive = any(a in FIND_DELETE_ACTIONS for a in args)
    roots = _find_roots(args)
    unfiltered = any(a in ("-not", "!", "-o", "-or", ",") for a in args)
    filters = [(a.lower(), args[i + 1]) for i, a in enumerate(args)
               if a.lower() in FIND_NAME_FILTERS | FIND_PATH_FILTERS and i + 1 < len(args)]

    def can_match(name: str, path: str) -> bool:
        """Whether the (ANDed) -name/-path filters can match an entry with this name and path."""
        if unfiltered or not filters:
            return True
        return all(fnmatch.fnmatchcase((name if flt in FIND_NAME_FILTERS else path).lower(), pat.lower())
                   for flt, pat in filters)

    # {} expands to matched entries: a root the filters can match (-maxdepth 0 -name .) or the workspace logs/ dir
    # under a root that reaches it (-name 'lo*'); every -exec command is also judged with its literal operands
    # (find . -name x -exec rm -rf logs \;), before the name-filter shortcut below.
    matches = [r for r in roots if can_match(posixpath.basename(_shell_path(r)) or r, r)]
    matches += [posixpath.join(r, "logs") for r in roots
                if _reaches_logs_dir(r, cwd, base_dir) and not _is_logs_dir(r, cwd, base_dir)
                and can_match("logs", posixpath.join(r, "logs"))]
    exec_words: List[str] = []
    for i, a in enumerate(args):
        if a in FIND_EXEC_ACTIONS:
            end = next((j for j in range(i + 1, len(args)) if args[j] in (";", "+")), len(args))
            command = args[i + 1:end]
            if _exec_writes(command):
                destructive = True
                exec_words += args[i + 2:end]
            for value in ["__find_match__"] + matches:
                sub = [t.replace("{}", value) for t in command]
                hits += _ground_truth_writes(sub, " ".join(sub), cwd, base_dir, depth + 1)
    if not destructive:
        return hits
    hits += _ground_truth_named(" ".join(args))
    if filters and not unfiltered:
        # Name/path filters are ANDed: only files they can match are reachable
        for flt, pattern in filters:
            pat = pattern.lower()
            for base, key in GROUND_TRUTH_BASENAMES.items():
                candidates = [base] if flt in FIND_NAME_FILTERS else (
                    [f"logs/{base}", f"./logs/{base}", f"/x/logs/{base}"]
                    + [posixpath.join(r, "logs", base).lower() for r in roots])
                if any(fnmatch.fnmatchcase(c, pat) for c in candidates):
                    hits.append(key)
        return hits
    # Negated / alternated filters or only -type, -mmin, -regex ...: every file under the roots is reachable,
    # and so is logs/ when the -exec command writes into it (find /tmp/f -type f -exec cp {} logs/ \;)
    if (any(_reaches_logs_dir(r, cwd, base_dir) for r in roots)
            or any(_is_logs_dir(w, cwd, base_dir) for w in exec_words)):
        hits.extend(GROUND_TRUTH_FILES)
    return hits


def _ground_truth_read_only(prog: str, args: List[str], assigned: bool = False) -> bool:
    """True when a sub-command that names a protected file can only read it (read-only allowlist). Options that make
    an allowlisted program write a file or run a command (git --output / -O / -c, rg --pre, less -o) and leading
    VAR= assignments (LESSOPEN, GIT_EXTERNAL_DIFF, NODE_OPTIONS...) void the exemption."""
    if assigned:
        return False
    if prog in GROUND_TRUTH_READ_PROGRAMS:
        if prog == "jq":
            return not any(a == "-i" or a.startswith("--in-place") for a in args)
        if prog == "rg":
            return not any(a in RG_EXEC_OPTIONS or a.startswith(tuple(o + "=" for o in RG_EXEC_OPTIONS))
                           for a in args)
        if prog == "less":
            return not any(a.startswith(("+", "--log-file", "--LOG-FILE")) or re.match(r"^-[^-]*[oO]", a)
                           for a in args)
        return True
    if prog == "find":
        return True  # judged by _find_ground_truth (destructive actions, -fprint outputs, -exec commands)
    if prog == "git":
        # commit messages, greps and diffs may name them
        return _git_subcommand(args)[0] in GIT_READ_SUBCOMMANDS and not _git_runs_commands(args)
    if prog == "gh":
        return bool(args) and args[0] in ("pr", "issue")
    if prog.startswith("python"):
        if "-m" in args and "json.tool" in args:
            rest = args[args.index("json.tool") + 1:]
            positional = [a for i, a in enumerate(rest) if not a.startswith("-") and (i == 0 or rest[i - 1] != "--indent")]
            return len(positional) <= 1  # a second positional is the output file
        # python -c / python - (heredoc): inline code is judged by INLINE_WRITE_MARKERS_RE on the whole command
        for a in args:
            if a == "-" or re.match(r"^-[A-Za-z]*c$", a):
                return True
            if not a.startswith("-"):
                return False  # a script (python3 tool.py logs/...) is not read-only
        return False
    if prog in ("node", "nodejs"):
        return any(a in ("-e", "--eval", "-p", "--print") for a in args)
    return False


def _git_subcommand(args: List[str]) -> Tuple[str, List[str]]:
    """(sub-command, its args) after git's global options (git -C dir -c k=v clean -fdx -> 'clean', ['-fdx'])."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in GIT_GLOBAL_VALUE_OPTIONS else 1
    return (args[i].lower(), args[i + 1:]) if i < len(args) else ("", [])


def _git_long_options(arg: str) -> List[str]:
    """GIT_RUN_LONG_OPTIONS an argument can select: the exact name, a longer spelling (--output-directory) or a
    unique-prefix abbreviation parse-options accepts (--op='cmd', --upl=cmd, --rece=, --exe=)."""
    m = re.match(r"^--([A-Za-z][\w-]*)(?:=|$)", arg)
    if not m:
        return []
    name = m.group(1).lower()
    return [o for o, shortest in GIT_RUN_LONG_OPTIONS.items()
            if name.startswith(o) or (len(name) >= shortest and o.startswith(name))]


def _git_short_cluster_runs(sub: str, arg: str) -> bool:
    """A short-option cluster with O (grep -O<cmd>, -nOrm: n, then O takes 'rm') or, for diff/log/show, o."""
    if not re.match(r"^-[^-]", arg):
        return False
    return "O" in arg[1:] or (sub in GIT_LOWER_O_SUBCOMMANDS and "o" in arg[1:])


def _git_runs_commands(args: List[str]) -> bool:
    """git -c / --config-env / --exec-path (core.fsmonitor, core.pager, diff.external...) or a sub-command option
    that writes a file or runs a command (log/show/diff --output=F, grep -O<cmd> / -nOrm / --op=<cmd>, --ext-diff,
    fetch --upload-pack / --upl=, push --receive-pack / --exec), abbreviations included."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-c" or args[i].startswith(GIT_RUN_GLOBAL_OPTIONS):
            return True
        i += 2 if args[i] in GIT_GLOBAL_VALUE_OPTIONS else 1
    sub = args[i].lower() if i < len(args) else ""
    return any(_git_long_options(a) or _git_short_cluster_runs(sub, a) for a in args[i + 1:])


def _git_exec_options(args: List[str]) -> Tuple[List[str], List[str]]:
    """(shell-command values, operands the commands act on) of git's command-running sub-command options:
    grep -O<cmd> / -nOrm / --open-files-in-pager[=cmd] (abbreviations: --op=) run the pager on the matched files,
    whose operands default to '.'; fetch/ls-remote --upload-pack, push --receive-pack / --exec (--upl=, --rece=,
    --exe=) run on a local repository operand (git fetch --upl='rm -rf logs;:' .). Operands are [] when no such
    option is present; patterns (-e .) are kept as operands, which errs toward denying."""
    sub, rest = _git_subcommand(args)
    runs = False
    values: List[str] = []
    operands: List[str] = []
    skip = False
    for j, a in enumerate(rest):
        if skip:
            skip = False
            continue
        if a == "--":
            operands += rest[j + 1:]
            break
        names = [n for n in _git_long_options(a) if n in GIT_EXEC_LONG_OPTIONS]
        if names:
            runs = True
            if "=" in a:
                values.append(a.split("=", 1)[1])
            elif any(n in GIT_EXEC_VALUE_OPTIONS for n in names) and j + 1 < len(rest):
                values.append(rest[j + 1])
                skip = True
        elif re.match(r"^-[^-]", a) and "O" in a:
            runs = runs or sub == "grep"  # diff/log/show -O<orderfile> only reads a file
            if a[a.index("O") + 1:]:
                values.append(a[a.index("O") + 1:])
        elif not a.startswith("-"):
            operands.append(a)
    if not runs:
        return values, []
    if sub == "grep" and len(operands) <= 1:
        operands.append(".")  # only a pattern: git grep searches the working directory
    return values, operands


def _rg_exec_options(args: List[str]) -> Tuple[List[str], List[str]]:
    """(command values, operands) of rg --pre CMD (run on every searched file, operands default to '.') and
    --hostname-bin CMD; operands are [] without --pre. The pattern is kept as an operand (errs toward denying)."""
    pre = False
    values: List[str] = []
    operands: List[str] = []
    skip = False
    for j, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--":
            operands += args[j + 1:]
            break
        if a in RG_EXEC_OPTIONS:
            pre = pre or a == "--pre"
            if j + 1 < len(args):
                values.append(args[j + 1])
                skip = True
        elif a.startswith(tuple(o + "=" for o in RG_EXEC_OPTIONS)):
            pre = pre or a.startswith("--pre=")
            values.append(a.split("=", 1)[1])
        elif not a.startswith("-"):
            operands.append(a)
    if not pre:
        return values, []
    return values, operands + (["."] if len(operands) <= 1 else [])


def _command_option_operands(prog: str, args: List[str]) -> List[str]:
    """Operands of an allowlisted program whose command-running option acts on them (rg --pre, git grep -O,
    git fetch --upload-pack ...); [] when no such option is present."""
    if prog == "rg":
        return _rg_exec_options(args)[1]
    if prog == "git":
        return _git_exec_options(args)[1]
    return []


def _git_wipes_logs(args: List[str]) -> bool:
    """git clean -x/-X and git stash --all delete the ignored runtime state under logs/."""
    sub, rest = _git_subcommand(args)
    if sub == "clean":
        return any(re.match(r"^-[A-Za-z]*[xX]", a) for a in rest)
    if sub == "stash":
        return any(a == "--all" or re.match(r"^-[A-Za-z]*a", a) for a in rest)
    return False


def _nested_commands(prog: str, args: List[str]) -> List[str]:
    """Command strings a sub-command runs through another shell: sh/bash -c '...', eval ..., cmd /c ...,
    powershell -Command ..., git -c values (core.fsmonitor='rm -rf logs', alias.x='!cmd') and the values of
    command-running options (git grep -O<cmd> / --op=, fetch --upload-pack / --upl=, push --receive-pack / --exec,
    rg --pre / --hostname-bin, less +!cmd / +|<mark>cmd)."""
    if prog == "git":
        values = []
        for i, a in enumerate(args[:-1]):
            if a == "-c" and "=" in args[i + 1]:
                values.append(args[i + 1].split("=", 1)[1].lstrip("!"))
            elif not a.startswith("-") and (i == 0 or args[i - 1] not in GIT_GLOBAL_VALUE_OPTIONS):
                break  # the sub-command: its own -c options (git grep -c) are not config values
        return values + _git_exec_options(args)[0]
    if prog == "rg":
        return _rg_exec_options(args)[0]
    if prog == "less":
        values = []
        for a in args:
            m = re.search(r"!(.*)|\|.(.*)", a[1:], re.DOTALL) if a.startswith("+") else None
            if m:
                values.append(m.group(1) if m.group(1) is not None else m.group(2))
        return values
    if prog in SHELL_INTERPRETERS:
        has_c = skip = False
        for a in args:
            if skip:
                skip = False
            elif a in SHELL_VALUE_OPTIONS:
                skip = True
            elif a == "--":
                continue
            elif a.startswith(("-", "+")) and len(a) > 1:
                has_c = has_c or (not a.startswith("--") and "c" in a[1:])
            else:
                return [a] if has_c else []  # without -c the first operand is a script file
        return []
    if prog == "eval":
        return [" ".join(args)] if args else []
    if prog == "cmd":
        return next(([" ".join(args[i + 1:])] for i, a in enumerate(args) if re.match(r"^/+[cCkK]$", a)), [])
    if prog in ("powershell", "pwsh"):
        return next(([" ".join(args[i + 1:])] for i, a in enumerate(args)
                     if re.match(r"^-(?:c|com|comm|comma|comman|command)$", a, re.IGNORECASE)), [])
    return []


def _ground_truth_writes(tokens: List[str], text: str, cwd: str = "", base_dir: str = "", depth: int = 0) -> List[str]:
    """Protected ground-truth files a single shell sub-command can create, modify, move, delete or alias."""
    if depth > NESTED_DEPTH_LIMIT:
        return list(GROUND_TRUTH_FILES)  # pathological nesting: fail closed
    hits: List[str] = []
    for target in _redirect_targets(tokens):
        hits += _ground_truth_named(target) + _glob_ground_truth(target)
    idx = _program_index(tokens)
    prog = re.sub(r"\.exe$", "", _program(tokens))
    args = _plain_args(tokens[idx + 1:] if idx < len(tokens) else [])
    assigned = any(ASSIGNMENT_RE.match(t) for t in tokens[:idx])
    mentioned = _ground_truth_named(text)
    for a in tokens:
        mentioned += _glob_ground_truth(_word_value(a))
    if mentioned and not _ground_truth_read_only(prog, args, assigned):
        hits += mentioned
    # Nested shells (bash -c 'rm -rf logs', eval, cmd //c rd /s /q logs): the inner command line is judged too
    for nested in _nested_commands(prog, args):
        for sub in _ground_truth_subcommands(nested):
            hits += _ground_truth_writes(sub, " ".join(sub), cwd, base_dir, depth + 1)
    # A command-running option over logs/ or an ancestor (rg --pre rm . logs, git fetch --upl=CMD .): the command
    # runs on the protected files (or the local repository) whatever its value names
    if any(_reaches_logs_dir(o, cwd, base_dir) for o in _command_option_operands(prog, args)):
        hits.extend(GROUND_TRUTH_FILES)
    operands, target_dir = _split_operands(prog, args)
    if prog in WINDOWS_DELETE_PROGRAMS | WINDOWS_MOVE_PROGRAMS:
        operands = [o for o in operands if not WINDOWS_SWITCH_RE.match(o)]  # rd /s /q: switches, not paths
    globbed = any(SHELL_GLOB_RE.search(a) for a in operands)
    recursive = _is_recursive(args) or (prog in WINDOWS_DELETE_PROGRAMS and any(a.lower() == "/s" for a in args))
    if prog in {"rm"} | WINDOWS_DELETE_PROGRAMS and recursive:
        if any(_reaches_logs_dir(a, cwd, base_dir) for a in operands):
            hits.extend(GROUND_TRUTH_FILES)
    elif (prog in LOGS_DIR_DESTRUCTIVE_PROGRAMS | WINDOWS_DELETE_PROGRAMS
          and any(_is_logs_dir(a, cwd, base_dir) for a in operands)):
        hits.extend(GROUND_TRUTH_FILES)
    if prog in {"mv"} | WINDOWS_MOVE_PROGRAMS and operands:
        sources, dest = (operands, target_dir) if target_dir is not None else (operands[:-1], operands[-1])
        # Moving the logs directory (or an ancestor) away, or a glob of unseen names into it;
        # `mv report.txt logs/` stays allowed.
        if (any(_reaches_logs_dir(s, cwd, base_dir) for s in sources)
                or (dest and globbed and _is_logs_dir(dest, cwd, base_dir))):
            hits.extend(GROUND_TRUTH_FILES)
    if prog in LOGS_DIR_COPY_PROGRAMS and operands and (_is_recursive(args) or globbed):
        sources, dest = (operands, target_dir) if target_dir is not None else (operands[:-1], operands[-1])
        # Into logs/ (cp -r src/. logs, cp -t logs src/*), or a source dir named logs (any glob that can expand to
        # one: /tmp/f/*) into logs/ or one of its ancestors (cp -r /tmp/f/* .)
        if ((dest and _is_logs_dir(dest, cwd, base_dir))
                or any(_shell_path(s).rpartition("/")[2].lower() == "logs" for s in sources)
                or (dest and _reaches_logs_dir(dest, cwd, base_dir) and any(_is_logs_dir(s) for s in sources))):
            hits.extend(GROUND_TRUTH_FILES)
    if prog == "ln" and any(_is_logs_dir(a, cwd, base_dir) for a in operands + ([target_dir] if target_dir else [])):
        # A symlink/hard link to the logs dir is an alias that file tools would not recognise (ln -s logs st)
        hits.extend(GROUND_TRUTH_FILES)
    if re.search(r"\bmklink\b", text, re.IGNORECASE) and any(_is_logs_dir(w, cwd, base_dir) for w in text.split()):
        hits.extend(GROUND_TRUTH_FILES)
    if prog == "find":
        hits += _find_ground_truth(args, cwd, base_dir, depth)
    if prog == "git" and _git_wipes_logs(args):
        hits.extend(GROUND_TRUTH_FILES)
    return [p for p in GROUND_TRUTH_FILES if p in set(hits)]


def _heredoc_feeds_interpreter(line: str, m: "re.Match") -> bool:
    """True when the heredoc operator m belongs to a python/node sub-command (python3 - <<EOF, node <<EOF) or to
    `cat <<EOF | python3`; any other program (perl, bash, cat...) keeps its body in the line-by-line check."""
    head = split_subcommands(line[:m.start()])
    prog = re.sub(r"\.exe$", "", _program(head[-1])) if head else ""
    if CODE_INTERPRETER_PROGRAM_RE.match(prog):
        return True
    return prog == "cat" and bool(PIPE_TO_CODE_INTERPRETER_RE.match(line[m.end():]))


def _strip_interpreter_heredocs(command_line: str) -> str:
    """Drops heredoc bodies fed to python/node: their code is judged by INLINE_WRITE_MARKERS_RE on the whole
    command line, not line by line as shell sub-commands."""
    lines = command_line.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC_RE.finditer(line):
            interpreter = _heredoc_feeds_interpreter(line, m)
            delim, strip_tabs = m.group(3), m.group(1) == "-"
            while i < len(lines) and (lines[i].lstrip("\t") if strip_tabs else lines[i]) != delim:
                if not interpreter:
                    out.append(lines[i])
                i += 1
            if i < len(lines):
                if not interpreter:
                    out.append(lines[i])
                i += 1
    return "\n".join(out)


def _inline_logs_dir_writes(command_line: str, cwd: str = "", base_dir: str = "") -> List[str]:
    """Protected files inline code can destroy through the logs/ directory: a 'logs' string literal (or a logs/ glob
    reaching a protected file) next to a destructive call (shutil.rmtree('logs'), fs.rmSync('logs'), os.remove over
    glob('logs/*')), or an ancestor / glob literal ('.', '..', the repo root, '*') next to a recursive delete or move."""
    broad = bool(INLINE_LOGS_DIR_MARKERS_RE.search(command_line) or INLINE_WRITE_MARKERS_RE.search(command_line))
    strong = bool(INLINE_ANCESTOR_MARKERS_RE.search(command_line))
    if not broad:
        return []
    hits: List[str] = []
    for m in INLINE_STRING_LITERAL_RE.finditer(command_line):
        literal = m.group(2)
        if not SHELL_GLOB_RE.search(literal) and _is_logs_dir(literal, cwd, base_dir):
            hits.extend(GROUND_TRUTH_FILES)
        hits += _glob_ground_truth(literal)
        if strong and _reaches_logs_dir(literal, cwd, base_dir):
            hits.extend(GROUND_TRUTH_FILES)
    return [p for p in GROUND_TRUTH_FILES if p in set(hits)]


def _lift_path_substitutions(command_line: str) -> str:
    """Rewrites working-directory substitutions ($(pwd), `pwd`, $PWD, $(git rev-parse --show-toplevel)) to '.' and
    $HOME to '~', and replaces a command substitution used as a path prefix ($(cmd)/logs) by '.', appending its
    command as a separate line: unquoted, the tokenizer would split `$(pwd)/logs` into '$', '(', 'pwd', ')', '/logs'
    and the path would never reach the rm / ln operand checks."""
    line = HOME_VAR_RE.sub("~", CWD_SUBSTITUTION_RE.sub(".", command_line))
    lifted: List[str] = []

    def lift(m: "re.Match") -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        if inner.strip():
            lifted.append(inner)
        return "."

    line = PATH_SUBSTITUTION_RE.sub(lift, line)
    return line + "".join("\n" + c for c in lifted)


def _ground_truth_subcommands(command_line: str) -> List[List[str]]:
    """Sub-commands for the ground-truth check: path substitutions lifted, interpreter heredoc bodies dropped and
    `find` predicates split off by escaped parentheses / `\\;` re-attached to their find (find . \\( -type f \\) -delete)."""
    merged: List[List[str]] = []
    for tokens in split_subcommands(_strip_interpreter_heredocs(_lift_path_substitutions(command_line))):
        if merged and _program(merged[-1]) == "find" and (tokens[0].startswith("-") or tokens[0] in ("!", ",")):
            merged[-1] = merged[-1] + tokens
        else:
            merged.append(list(tokens))
    return merged


def _resolve_script_path(token: str, cwd: str, base_dir: str) -> Optional[str]:
    if not token.endswith(".py"):
        return None
    path = os.path.expanduser(token)
    if not os.path.isabs(path):
        path = os.path.join(cwd or base_dir, path)
    return os.path.normpath(path)


def _unsanctioned_trading_script(tokens: List[str], cwd: str, base_dir: str) -> Optional[str]:
    """Returns the path of an executed .py file outside scripts/ and tests/ that contains trading primitives."""
    idx = _program_index(tokens)
    if idx >= len(tokens):
        return None
    prog = os.path.basename(tokens[idx]).lower()
    candidates = []
    if prog.endswith(".py"):
        candidates.append(tokens[idx])
    elif prog.startswith("python"):
        for tok in tokens[idx + 1:]:
            if tok.startswith("-"):
                continue
            candidates.append(tok)
            break
    base_norm = os.path.normcase(os.path.normpath(base_dir))
    for tok in candidates:
        path = _resolve_script_path(tok, cwd, base_dir)
        if not path or not os.path.isfile(path):
            continue
        rel = os.path.relpath(os.path.normcase(path), base_norm).replace("\\", "/")
        if not rel.startswith("..") and (rel.startswith("scripts/") or rel.startswith("tests/")):
            continue
        try:
            if os.path.getsize(path) > 2 * 1024 * 1024:
                return path
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if SCRIPT_TRADING_PRIMITIVES_RE.search(f.read()):
                    return path
        except OSError:
            continue
    return None


def analyze_run_command(command_line: str, cwd: str, base_dir: str) -> Dict[str, Any]:
    """
    Classifies a shell command. Returns
    {deny: reason|None, force_ask: reason|None, trading: [subcommand text], risk_reducing: bool,
     neutral_only: bool, record_eval: {...}|None}
    """
    result: Dict[str, Any] = {
        "deny": None, "force_ask": None, "trading": [], "batch": [], "risk_reducing": False,
        "all_safe": True, "record_eval": None,
    }
    if not command_line.strip():
        return result

    inline = _is_inline_code(command_line)
    subcommands = split_subcommands(command_line)

    # 1. Evaluation trail is immutable for the agent (record_evaluation.py --from-subagent writes it itself)
    if EVALUATION_TRAIL_CMD_RE.search(command_line):
        result["deny"] = (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Evaluation Trail Protection): Commands must not read or write "
            "logs/evaluations/, latest_dossier.json, Antigravity brain transcripts or Claude Code subagent "
            "transcripts. Use view_file (Claude Code: Read) to inspect the dossier. " + EVALUATOR_HINT
        )
        return result
    if TRANSCRIPT_ROOT_OVERRIDE_RE.search(command_line):
        result["deny"] = (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Evaluation Trail Protection): AGY_BRAIN_DIRS / CLAUDE_PROJECTS_DIRS "
            "are test-only overrides of the subagent transcript roots and cannot be set by the agent in a "
            "command. " + EVALUATOR_HINT
        )
        return result

    # 2. Inline code / piped interpreters using trading primitives
    if BASE64_EXEC_RE.search(command_line):
        result["deny"] = "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Obfuscated Execution): Decoded payloads piped into an interpreter are forbidden."
        return result
    if inline and INLINE_TRADING_PRIMITIVES_RE.search(command_line):
        result["deny"] = (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Inline code (python -c, heredoc, piped interpreter) "
            "using trading primitives (execute_futures_trade, send_signed_request, /fapi/v1 write endpoints, MCP gateway) "
            f"is strictly forbidden. Orders must be routed exclusively through {CHOKE_POINT}; "
            "risk reduction must use the sanctioned CLI flags (--close-position, --move-breakeven, --auto-heal, "
            "--audit-orphans, --protect-pending) or scripts/loops/position_guardian_loop.py."
        )
        return result

    # 3. Raw HTTP writes to Binance
    if HTTP_CLIENT_RE.search(command_line) and BINANCE_HOST_RE.search(command_line) and HTTP_WRITE_RE.search(command_line):
        result["deny"] = (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Raw HTTP write requests to Binance "
            f"(fapi / MCP gateway) are strictly forbidden. Route orders through {CHOKE_POINT}."
        )
        return result

    # 4a. Ground-truth state written from inline code. Checked on the whole command line because heredoc bodies
    #     are split into many sub-commands (newlines, parentheses), separating the path from the write call.
    if inline and INLINE_WRITE_MARKERS_RE.search(command_line):
        named = _ground_truth_named(command_line)
        if named:
            result["deny"] = ground_truth_denial(named)
            return result
    #     ... and inline code destroying / moving / aliasing the logs/ directory itself (shutil.rmtree('logs'))
    if inline:
        protected = _inline_logs_dir_writes(command_line, cwd, base_dir)
        if protected:
            result["deny"] = ground_truth_denial(protected)
            return result

    # 4b. Ground-truth state (GROUND_TRUTH_FILES) may only be written by its sanctioned desk script
    for tokens in _ground_truth_subcommands(command_line):
        protected = _ground_truth_writes(tokens, " ".join(tokens), cwd, base_dir)
        if protected:
            result["deny"] = ground_truth_denial(protected)
            return result

    for tokens in subcommands:
        text = " ".join(tokens)
        prog = _program(tokens)

        # 5. Harness files require explicit confirmation
        if HARNESS_PATH_CMD_RE.search(text) and _subcommand_writes_path(tokens, text, HARNESS_PATH_CMD_RE, inline):
            result["force_ask"] = "Command modifies trading harness files (hooks, dossier provenance, profile). Explicit confirmation required."
        if USER_PROFILE_SET_RE.search(text):
            result["force_ask"] = "Command changes the trading user profile (risk/autonomy/YOLO settings). Explicit confirmation required."

        # 6. Executing scripts outside scripts/ that embed trading primitives
        script = _unsanctioned_trading_script(tokens, cwd, base_dir)
        if script:
            result["deny"] = (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Script "
                f"'{os.path.basename(script)}' outside scripts/ uses trading primitives. "
                f"Orders must be routed exclusively through {CHOKE_POINT}."
            )
            return result

        # 7. Evaluation recorder
        if RECORD_EVALUATION_RE.search(text) and prog not in INSPECTION_PROGRAMS:
            from_subagent = any(t in ("--from-subagent", "--from-claude-subagent")
                                or t.startswith(("--from-subagent=", "--from-claude-subagent=")) for t in tokens)
            result["record_eval"] = {"from_subagent": from_subagent, "text": text}
            result["all_safe"] = False
            continue

        if prog in INSPECTION_PROGRAMS and not inline:
            result["all_safe"] = False if prog not in BENIGN_PROGRAMS else result["all_safe"]
            continue

        # 8. Trade openings (engine, batch deploy scripts, auto-deploy loops)
        is_batch = bool(DEPLOY_BATCH_RE.search(text)) or (
            bool(AUTO_DEPLOY_LOOP_RE.search(text)) and any(t.split("=", 1)[0] == "--auto-deploy" for t in tokens)
        )
        if is_batch:
            result["batch"].append(text)
            continue
        if TRADE_ENGINE_RE.search(text):
            flags = _flags(tokens)
            if _subcommand_is_risk_reducing(tokens, text):
                result["risk_reducing"] = True
            elif flags & EXECUTOR_MOVE_BREAKEVEN_FLAGS:
                result["deny"] = (
                    "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Structured Risk Parsing): `execute_futures_trade.py "
                    "--move-breakeven` requires exactly one --symbol (e.g. --move-breakeven --symbol BTCUSDT)."
                )
                return result
            elif flags & EXECUTOR_READ_ONLY_FLAGS:
                result["all_safe"] = False  # read-only position listing: normal permission policy (ask)
            else:
                result["trading"].append(text)
            continue

        if _subcommand_is_risk_reducing(tokens, text):
            result["risk_reducing"] = True
            continue

        if prog not in BENIGN_PROGRAMS or _redirect_targets(tokens):
            result["all_safe"] = False

    return result


# =============================================================================
# Binance MCP evaluation (read-only allowlist)
# =============================================================================
def split_binance_tool(name: str) -> Tuple[str, str, str]:
    """Returns (namespace, operation, verb) for dotted (futures_usds.newOrder) or verb-style names."""
    name = (name or "").strip()
    m = BINANCE_VERB_RE.match(name)
    if m and "." not in name:
        return m.group(2), m.group(3), m.group(1).lower()
    if "." in name:
        ns, op = name.rsplit(".", 1)
        return ns, op, ""
    return "", name, ""


def is_binance_call(server: str, tool: str) -> bool:
    server_l = (server or "").lower()
    if "binance" in server_l:
        return True
    return bool(BINANCE_NAMESPACE_RE.match(tool or "") or BINANCE_VERB_RE.match(tool or ""))


def _order_is_reduce_only(order_args: dict) -> bool:
    return _is_true(_first(order_args, "reduceOnly", "reduce_only", "reduce-only")) or \
        _is_true(_first(order_args, "closePosition", "close_position", "close-position"))


def evaluate_binance_tool(tool: str, mcp_args: dict, depth: int = 0) -> Tuple[str, str, Dict[str, Any]]:
    """
    Returns (verdict, reason, info) where verdict is one of:
      read_only | risk_reducing | leverage | deny
    """
    name = (tool or "").strip()
    info: Dict[str, Any] = {"tool": name, "args": mcp_args}
    deny_reason = (
        f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Direct call to Binance MCP write tool '{name}' "
        f"is strictly forbidden. Opening trades must be routed exclusively through the approved choke point: {CHOKE_POINT} "
        "to enforce the evaluator dossier, mechanical gates and atomic Stop Loss placement."
    )

    base = name.rsplit(".", 1)[-1] if name.startswith("binance.") else name
    if base in BINANCE_META_EXECUTE_TOOLS:
        if depth >= 3:
            return "deny", "🚨 FAIL-CLOSED: Nested tool_execute wrappers are not allowed.", info
        inner = _decode_str(_first(mcp_args, "toolName", "tool_name", "ToolName", "name"))
        inner_args = _decode_dict(_first(mcp_args, "arguments", "Arguments", "args") or {})
        if not inner:
            return "deny", "🚨 FAIL-CLOSED: Binance gateway tool_execute called without a target toolName.", info
        return evaluate_binance_tool(inner, inner_args, depth + 1)
    if base in BINANCE_META_READ_TOOLS:
        return "read_only", f"Binance gateway discovery tool '{name}' (read-only).", info

    ns, op, verb = split_binance_tool(name)
    op_l = op.lower()
    info.update({"namespace": ns, "operation": op})

    if verb == "get" or op_l in BINANCE_READ_ONLY_OPS:
        return "read_only", f"Binance read-only tool '{name}'.", info
    if "cancel" in op_l or op_l.startswith("delete"):
        return "risk_reducing", f"Risk-reducing action / exit authorized (Binance '{name}').", info
    if op_l == "changeinitialleverage":
        return "leverage", "", info

    is_futures = ns.startswith("futures")
    if is_futures and op_l in BINANCE_REDUCE_ONLY_ORDER_OPS and _order_is_reduce_only(mcp_args):
        return "risk_reducing", f"Risk-reducing action / exit authorized (reduce-only Binance '{name}').", info
    if is_futures and op_l in BINANCE_BATCH_ORDER_OPS:
        batch = _decode_value(_first(mcp_args, "batchOrders", "batch_orders", "batch-orders"))
        if isinstance(batch, list) and batch and all(isinstance(o, dict) and _order_is_reduce_only(o) for o in batch):
            return "risk_reducing", f"Risk-reducing action / exit authorized (reduce-only batch '{name}').", info
    return "deny", deny_reason, info


# =============================================================================
# Retired crypto_radar MCP server
# =============================================================================
def is_retired_mcp_server(server: str) -> bool:
    """True for the retired crypto_radar server, including plugin/alias prefixes (e.g. plugin_x_crypto_radar)."""
    norm = (server or "").strip().lower().replace("-", "_")
    return any(norm == s or norm.endswith("_" + s) for s in RETIRED_MCP_SERVERS)


def retired_radar_reason(tool: str) -> str:
    replacement = LEGACY_RADAR_TOOL_REPLACEMENTS.get(tool or "")
    hint = f"Use `{replacement}` instead. " if replacement else ""
    return (
        f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Retired MCP Server): The 'crypto_radar' MCP server has been retired "
        f"and tool '{tool or '?'}' is no longer available. {hint}"
        "Read-only analytics are CLI scripts with --json output (see .agents/skills/market-radar/SKILL.md); "
        f"orders and position management go exclusively through {CHOKE_POINT} "
        "(--positions, --move-breakeven, --close-position, --audit-orphans, --auto-heal, --protect-pending); trailing stops, dead-alpha "
        "and orphan audits run in scripts/loops/position_guardian_loop.py. Remove the stale 'crypto_radar' entry "
        "from your MCP client configuration."
    )


# =============================================================================
# File writes (evaluation trail, ground-truth state & harness protection)
# Ground-truth files are matched by suffix (logs/<name>) on the workspace-relative path, the normalised absolute
# path and the raw target (backslashes converted, Windows drive stripped), so `logs/x`, `./logs/x`, POSIX
# absolute paths and `C:\...\logs\x` (Claude Code on Windows feeding the WSL hook) are all denied. NTFS aliases
# (trailing dots/spaces, `::$DATA` streams) are stripped first, and the target is also resolved with
# os.path.realpath / os.path.samefile so symlinked directories and hard links to the files are denied too.
# =============================================================================
def _normalize_target(path: str, base_dir: str) -> Tuple[str, str]:
    """Returns (absolute normalized path with forward slashes, workspace-relative path or '')."""
    p = (path or "").strip()
    if p.lower().startswith("file://"):
        p = re.sub(r"^file:/*", "/", p, flags=re.IGNORECASE)
        if re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(base_dir, p)
    abs_norm = os.path.normpath(p).replace("\\", "/")
    base_norm = os.path.normpath(base_dir).replace("\\", "/").rstrip("/")
    rel = ""
    if abs_norm.lower() == base_norm.lower():
        rel = ""
    elif abs_norm.lower().startswith(base_norm.lower() + "/"):
        rel = abs_norm[len(base_norm) + 1:]
    return abs_norm, rel


def _strip_windows_aliases(path: str) -> str:
    """NTFS aliases of the same file: trailing dots/spaces of each component and alternate data streams
    (guardian_state.json. / guardian_state.json::$DATA / name:stream). The drive letter is kept."""
    p = re.sub(r"^/(?=[A-Za-z]:)", "", (path or "").replace("\\", "/"))
    m = re.match(r"^[A-Za-z]:", p)
    drive, rest = (p[:2], p[2:]) if m else ("", p)
    parts = []
    for comp in rest.split("/"):
        comp = comp.split(":", 1)[0]
        parts.append(comp if comp in (".", "..") else comp.rstrip(". "))
    return drive + "/".join(parts)


def _ground_truth_file_target(target: str, abs_norm: str, rel: str) -> Optional[str]:
    """GROUND_TRUTH_FILES key when a file-tool target ends with logs/<protected name>, else None."""
    raw = re.sub(r"^file:/*", "/", (target or "").strip(), flags=re.IGNORECASE)
    for candidate in (rel, abs_norm, raw):
        path = _shell_path(_strip_windows_aliases(candidate or ""))
        m = GROUND_TRUTH_TARGET_RE.search(path) if path else None
        if m:
            return GROUND_TRUTH_BASENAMES[m.group(1).lower()]
    return None


def _host_path(target: str, base_dir: str) -> str:
    """File-tool target as a path on the hook's own filesystem (C:\\x -> /mnt/c/x and /c/x -> /mnt/c/x under WSL)."""
    p = re.sub(r"^file:/*", "/", (target or "").strip(), flags=re.IGNORECASE)
    p = re.sub(r"^/(?=[A-Za-z]:)", "", p)
    if os.name != "nt":
        m = re.match(r"^([A-Za-z]):[\\/]", p)
        if m:
            p = f"/mnt/{m.group(1).lower()}/" + p[3:].replace("\\", "/")
        m = re.match(r"^/([A-Za-z])(?=/)", p)
        if m and not os.path.isdir(p[:2]) and os.path.isdir(f"/mnt/{m.group(1).lower()}"):
            p = f"/mnt/{m.group(1).lower()}" + p[2:]
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(base_dir, p)


def _ground_truth_alias_target(target: str, base_dir: str) -> Optional[str]:
    """GROUND_TRUTH_FILES key when a target reaches a protected file through a symlinked directory or file
    (ln -s logs st; Write st/guardian_state.json) or a hard link (os.path.realpath / os.path.samefile)."""
    try:
        host = _host_path(target, base_dir)
        real = os.path.realpath(host)
    except (OSError, ValueError):
        return None
    for key in GROUND_TRUTH_FILES:
        protected = os.path.join(base_dir, *key.split("/"))
        try:
            if os.path.normcase(real) == os.path.normcase(os.path.realpath(protected)):
                return key
            if os.path.exists(host) and os.path.exists(protected) and os.path.samefile(host, protected):
                return key
        except (OSError, ValueError):
            continue
    m = GROUND_TRUTH_TARGET_RE.search(_shell_path(_strip_windows_aliases(real)))
    return GROUND_TRUTH_BASENAMES[m.group(1).lower()] if m else None


def evaluate_file_write(target: str, content: str, base_dir: str) -> Tuple[str, str]:
    if not target:
        return "force_ask", "File write without a resolvable target path."
    abs_norm, rel = _normalize_target(target, base_dir)
    rel_l = rel.lower()
    if (rel_l == "logs/evaluations" or rel_l.startswith("logs/evaluations/") or BRAIN_PATH_RE.search(abs_norm)
            or CLAUDE_SUBAGENT_PATH_RE.search(abs_norm)):
        return "deny", (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Evaluation Trail Protection): Writing to logs/evaluations/, "
            "Antigravity brain transcripts or Claude Code subagent transcripts is forbidden. " + EVALUATOR_HINT
        )
    protected = _ground_truth_file_target(target, abs_norm, rel) or _ground_truth_alias_target(target, base_dir)
    if protected:
        return "deny", ground_truth_denial([protected])
    if rel_l in HARNESS_FILES or any(rel_l.startswith(d) for d in HARNESS_DIRS):
        return "force_ask", f"'{rel}' is a trading harness file (hooks / dossier provenance / evaluator). Explicit confirmation required."
    if content and WRITE_ENDPOINT_PRIMITIVES_RE.search(content):
        return "force_ask", f"'{rel or abs_norm}' contains order-placing primitives. Explicit confirmation required."
    if content and not (rel_l.startswith("scripts/") or rel_l.startswith("tests/")) and SCRIPT_TRADING_PRIMITIVES_RE.search(content):
        return "force_ask", f"'{rel or abs_norm}' (outside scripts/) contains trading primitives. Explicit confirmation required."
    return "ask", ""


# =============================================================================
# Trade gates (opening orders through the sanctioned choke point)
# =============================================================================
def evaluate_trade_opening(cmd: str, args: dict, mcp_args: dict, base_dir: str,
                           conversation_id: Optional[str], env_hint_cmd: str = "") -> Tuple[str, str]:
    now_ts = int(time.time())
    target_sym = extract_target_symbol(cmd, args)

    explicit_env = extract_env_argument((env_hint_cmd or cmd), args)
    try:
        env = resolve_env(explicit_env, base_dir=base_dir)
    except ValueError as ve:
        return "deny", f"🚨 FAIL-CLOSED (Environment Resolution): {str(ve)}"
    is_prod = env == "prod"

    try:
        import user_profile as up
        user_prof = up.load_user_profile(base_dir=base_dir)
    except Exception:
        user_prof = {}

    # PRODUCTION INVARIANT: NO GATE BYPASSES IN PROD
    has_bypass_eval = ("--bypass-eval-gate" in cmd or "--bypass_eval_gate" in cmd
                       or _is_true(args.get("bypass_eval_gate")) or _is_true(mcp_args.get("bypass_eval_gate")))
    has_bypass_delta = ("--bypass-delta-gate" in cmd or "--bypass_delta_gate" in cmd
                        or _is_true(args.get("bypass_delta_gate")) or _is_true(mcp_args.get("bypass_delta_gate")))
    if is_prod and (has_bypass_eval or has_bypass_delta):
        return "deny", (
            "🚨 PROD INVARIANT VIOLATION: Gate bypasses (--bypass-eval-gate, --bypass-delta-gate) "
            "are strictly FORBIDDEN in PROD environment."
        )

    # USER PROFILE GATES: AUTONOMOUS TIER S & YOLO SLOT
    if is_prod and not user_prof.get("autonomous_execution_tier_s", False):
        is_confirmed = bool(re.search(r"--(?:confirmed|user[-_]confirmed)\b", cmd, re.IGNORECASE))
        for key in ("confirmed", "user_confirmed"):
            if _is_true(args.get(key)) or _is_true(mcp_args.get(key)):
                is_confirmed = True
        if not is_confirmed:
            return "deny", (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Autonomous Execution Disabled):\n"
                "Autonomous Tier S execution is disabled in user profile (autonomous_execution_tier_s=False).\n"
                "Human confirmation is required in PROD before opening new positions.\n"
                "👉 Ask the user to confirm and add the '--confirmed' flag; only the user may change "
                "autonomous execution in config/user_profile.json."
            )

    std_lev, _ceiling, _yolo_cap = leverage_limits(user_prof)
    is_yolo_trade = "--is-yolo" in cmd or "--is_yolo" in cmd or _is_true(args.get("is_yolo")) or _is_true(mcp_args.get("is_yolo"))
    trade_lev = 3
    m_lev = re.search(r"--leverage(?:\s+|=)(\d+)", cmd)
    if m_lev:
        trade_lev = int(m_lev.group(1))
    elif "leverage" in mcp_args:
        try:
            trade_lev = int(float(_decode_value(mcp_args["leverage"])))
        except (TypeError, ValueError):
            return "deny", f"🚨 FAIL-CLOSED: Invalid leverage value ({mcp_args.get('leverage')})."
    if trade_lev > std_lev:
        is_yolo_trade = True
    if is_yolo_trade and not user_prof.get("yolo_slot_enabled", False):
        return "deny", "🚨 BLOCKED BY PRE-TOOL-USE HOOK (YOLO Slot Disabled): YOLO moonshot slot is disabled in user profile."
    bounds_err = check_leverage_bounds(trade_lev, user_prof, is_yolo=is_yolo_trade)
    if bounds_err:
        return "deny", "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): " + bounds_err

    if not target_sym:
        return "deny", "🚨 FAIL-CLOSED: Unable to determine the order symbol deterministically. Exactly one --symbol is required."

    trade_dir, dir_err = parse_trade_direction(cmd, mcp_args or args)
    if not trade_dir and (is_prod or not has_bypass_delta):
        return "deny", f"🚨 FAIL-CLOSED (Direction Gate): {dir_err} Order blocked."

    # GATE 1: MANDATORY CLEAN-ROOM EVALUATOR (provenance-verified dossier)
    if not has_bypass_eval:
        ok, dossier_reason, cand = check_dossier(target_sym, trade_dir, env, base_dir, conversation_id, now_ts=now_ts)
        if not ok:
            return "deny", (
                "🚨 ACTION BLOCKED BY PRE-TOOL-USE HOOK (Clean-Room Evaluator Required):\n"
                f"{dossier_reason}\n"
                "Executing orders directly in primary chat without a fresh, provenance-verified 'APPROVED' "
                "dossier (< 20 min) is STRICTLY PROHIBITED.\n👉 " + EVALUATOR_HINT
            )
        if _candidate_is_yolo(cand) and not user_prof.get("yolo_slot_enabled", False):
            return "deny", "🚨 BLOCKED BY PRE-TOOL-USE HOOK (YOLO Slot Disabled): Candidate requires YOLO moonshot slot, which is disabled in user profile."

    # GATE 2: DELTA-NEUTRAL & SESSION STATE AUDIT
    if not has_bypass_delta:
        state_file = os.path.join(base_dir, "logs", "session_state.json")
        if not os.path.exists(state_file):
            return "deny", "🚨 FAIL-CLOSED: session_state.json does not exist. Portfolio delta cannot be audited before executing the order."
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            return "deny", f"🚨 FAIL-CLOSED: Critical error reading session_state.json ({str(e)}). Order blocked."
        if not isinstance(state, dict):
            return "deny", "🚨 FAIL-CLOSED: session_state.json is not a valid JSON object. Order blocked."

        if state.get("is_valid") is False or "error" in state:
            return "deny", f"🚨 FAIL-CLOSED: session_state.json is flagged INVALID ({state.get('error', 'Binance synchronization error')}). Order blocked."
        if is_prod and state.get("is_valid") is not True:
            return "deny", "🚨 FAIL-CLOSED: session_state.json lacks the 'is_valid': true required to trade in PROD. Order blocked."

        try:
            last_updated_ts = int(state.get("last_updated_ts", 0))
        except (TypeError, ValueError):
            last_updated_ts = 0
        age_seconds = now_ts - last_updated_ts if last_updated_ts > 0 else (now_ts - int(os.path.getmtime(state_file)))
        if is_prod and (last_updated_ts <= 0 or age_seconds > 300):
            return "deny", f"🚨 FAIL-CLOSED: session_state.json is STALE ({age_seconds}s > 300s limit in PROD). Run 'python3 scripts/sync_session_state.py' before trading."
        if age_seconds > 300:
            return "deny", f"🚨 FAIL-CLOSED: session_state.json is STALE ({age_seconds}s > 300s). Re-synchronize the session state."

        max_open_positions = int(user_prof.get("max_open_positions", 3))
        portfolio = state.get("portfolio_exposure", {}) or {}
        total_active = portfolio.get("total_active_positions")
        if total_active is None:
            total_active = len(state.get("active_positions", []))
        try:
            total_active = int(total_active)
        except (TypeError, ValueError):
            total_active = 0
        if total_active >= max_open_positions:
            return "deny", (
                f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Max Open Positions Gate): "
                f"Active positions ({total_active}) reached or exceeded maximum limit ({max_open_positions}) configured in user profile."
            )

        delta_bias = portfolio.get("delta_bias", "NEUTRAL")
        if delta_bias == "LONG_HEAVY" and trade_dir == "LONG":
            return "deny", (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Delta-Neutral Hard Gate): "
                f"Portfolio is bullishly unbalanced (Delta: +${portfolio.get('net_notional_delta_usdt', 0):.2f} USDT / LONG_HEAVY). "
                "Opening additional Longs without Short hedging is strictly prohibited."
            )
        if delta_bias == "SHORT_HEAVY" and trade_dir == "SHORT":
            return "deny", (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Delta-Neutral Hard Gate): "
                f"Portfolio is bearishly unbalanced (Delta: -${abs(portfolio.get('net_notional_delta_usdt', 0)):.2f} USDT / SHORT_HEAVY). "
                "Opening additional Shorts without Long hedging is strictly prohibited."
            )

    return "allow", "Mechanical hard gates and subagent validation PASSED successfully."


# =============================================================================
# Decision engine
# =============================================================================
def evaluate_payload(payload: dict) -> Tuple[str, str, str]:
    """Returns (decision, reason, tool_label). decision in allow|deny|ask|force_ask."""
    call = normalize_tool_call(payload)
    tool_label = call["tool"] or "?"
    base_dir = find_workspace_root()
    conversation_id = payload.get("conversationId") if isinstance(payload.get("conversationId"), str) else None
    if conversation_id is None and "toolCall" not in payload and isinstance(payload.get("session_id"), str):
        # Claude Code: the evaluator subagent transcript records the parent session as its sessionId
        conversation_id = payload["session_id"] or None

    # ---------------------------------------------------------------- file writes
    if call["kind"] == "file_write":
        decision, reason = evaluate_file_write(call["target_file"], call["content"], base_dir)
        return decision, reason, tool_label

    # ---------------------------------------------------------------- MCP calls
    if call["kind"] == "mcp":
        server, mcp_tool, mcp_args = call["server"], call["mcp_tool"], call["mcp_args"]
        tool_label = f"{server}:{mcp_tool}" if server else mcp_tool
        server_norm = (server or "").lower().replace("-", "_")
        wrapped_binance = mcp_tool in BINANCE_META_EXECUTE_TOOLS and is_binance_call(
            "", _decode_str(_first(mcp_args, "toolName", "tool_name", "ToolName", "name"))
        )

        # Retired crypto_radar MCP server (any tool) and its legacy tool names on any server alias.
        if is_retired_mcp_server(server_norm) or mcp_tool in LEGACY_RADAR_TOOL_REPLACEMENTS:
            return "deny", retired_radar_reason(mcp_tool), tool_label

        if is_binance_call(server, mcp_tool) or wrapped_binance:
            verdict, reason, info = evaluate_binance_tool(mcp_tool, mcp_args)
            if verdict == "read_only":
                return "ask", reason, tool_label
            if verdict == "risk_reducing":
                return "allow", reason, tool_label
            if verdict == "deny":
                return "deny", reason, tool_label
            # leverage gate
            lev_args = info.get("args") or {}
            raw_lev = _decode_value(lev_args.get("leverage"))
            try:
                requested_lev = int(float(raw_lev))
            except (TypeError, ValueError):
                return "deny", f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): Invalid leverage value ({raw_lev}).", tool_label
            symbol = extract_target_symbol("", lev_args)
            explicit_env = extract_env_argument("", lev_args)
            try:
                resolve_env(explicit_env, base_dir=base_dir)
            except ValueError as ve:
                return "deny", f"🚨 FAIL-CLOSED (Environment Resolution): {str(ve)}", tool_label
            allowed, reason = check_leverage_gate(symbol, requested_lev, base_dir, target_env=explicit_env,
                                                  conversation_id=conversation_id)
            return ("allow" if allowed else "deny"), reason, tool_label

        return "ask", "", tool_label

    # ---------------------------------------------------------------- shell commands
    if call["kind"] == "run_command":
        command_line = call["command"]
        analysis = analyze_run_command(command_line, call["cwd"], base_dir)
        if analysis["deny"]:
            return "deny", analysis["deny"], tool_label

        record_eval = analysis["record_eval"]
        if record_eval and not record_eval["from_subagent"]:
            try:
                env = resolve_env(extract_env_argument(record_eval["text"]) or extract_env_argument(command_line), base_dir=base_dir)
            except ValueError as ve:
                return "deny", f"🚨 FAIL-CLOSED (Environment Resolution): {str(ve)}", tool_label
            if env == "prod":
                return "deny", (
                    "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Evaluation Trail Protection): Manual dossier recording is disabled "
                    "in PROD. " + EVALUATOR_HINT
                ), tool_label

        if analysis["batch"]:
            try:
                env = resolve_env(extract_env_argument(command_line), base_dir=base_dir)
            except ValueError as ve:
                return "deny", f"🚨 FAIL-CLOSED (Environment Resolution): {str(ve)}", tool_label
            if env == "prod" or analysis["trading"] or len(analysis["batch"]) > 1:
                return "deny", (
                    "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Batch deploy scripts and auto-deploy loops "
                    "open positions that the per-trade evaluator dossier cannot verify. Route each order through "
                    "'scripts/execute_futures_trade.py' after a clean-room evaluation."
                ), tool_label
            analysis["trading"] = analysis["batch"]

        if len(analysis["trading"]) > 1:
            return "deny", (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Only one trade opening per command is allowed "
                "so that each order is gated individually."
            ), tool_label

        if analysis["trading"]:
            sub = analysis["trading"][0]
            decision, reason = evaluate_trade_opening(sub, {"CommandLine": sub}, {}, base_dir, conversation_id,
                                                      env_hint_cmd=command_line)
            if decision == "allow" and not analysis["all_safe"]:
                decision = "force_ask" if analysis["force_ask"] else "ask"
                reason = reason + " Compound command contains other sub-commands; user confirmation required."
            return decision, reason, tool_label

        if analysis["force_ask"]:
            return "force_ask", analysis["force_ask"], tool_label
        if analysis["risk_reducing"] and analysis["all_safe"]:
            return "allow", "Risk-reducing action / exit authorized.", tool_label
        return "ask", "", tool_label

    # ---------------------------------------------------------------- anything else
    return "ask", "", tool_label


# =============================================================================
# Output contracts
# =============================================================================
def _write_heartbeat(mode: str, tool: str, decision: str) -> None:
    """Best-effort liveness signal for trading_doctor; never changes the decision."""
    try:
        path = os.environ.get(HEARTBEAT_ENV_OVERRIDE) or os.path.join(find_workspace_root(), "logs", "hook_heartbeat.json")
        now = time.time()
        record = {
            "hook": HOOK_NAME,
            "mode": mode,
            "last_seen_ts": int(now),
            "last_seen_utc": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "tool": tool,
            "decision": decision,
        }
        if atomic_write_json is not None:
            atomic_write_json(path, record)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f)
            os.replace(tmp, path)
    except Exception:
        pass


def emit_decision(decision: str, reason: str = "", code: int = None, mode: str = "legacy") -> int:
    """Emits the decision in the runtime's contract and returns the process exit code."""
    if mode == "agy":
        res = {"decision": decision}
        if reason:
            res["reason"] = reason
        print(json.dumps(res))
        return 0

    if mode == "claude":
        if decision == "deny":
            sys.stderr.write((reason or "Blocked by pre_trade_guard.") + "\n")
            return 2
        if decision in ("allow", "force_ask"):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow" if decision == "allow" else "ask",
                    "permissionDecisionReason": reason or "",
                }
            }))
        return 0

    res = {"decision": decision}
    if code is not None:
        res["code"] = code
    elif decision == "deny":
        res["code"] = 2
    if reason:
        res["reason"] = reason
    print(json.dumps(res))
    return res.get("code", 0) if decision == "deny" else 0


def _detect_mode(argv: List[str], payload: Any) -> str:
    if "--agy" in argv:
        return "agy"
    if isinstance(payload, dict) and "toolCall" not in payload and ("tool_name" in payload or "tool_input" in payload):
        return "claude"
    return "legacy"


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = "agy" if "--agy" in argv else "legacy"
    tool_label = "?"
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            decision, reason = "deny", "🚨 FAIL-CLOSED: Empty payload received by pre-trade guard."
        else:
            try:
                payload = json.loads(raw_input)
            except Exception as e:
                payload = None
                decision, reason = "deny", f"🚨 FAIL-CLOSED: Invalid JSON payload ({str(e)})."
            if payload is not None:
                mode = _detect_mode(argv, payload)
                if mode == "claude":
                    tool_label = str(payload.get("tool_name") or "?")
                    if not isinstance(payload.get("tool_name"), str) or not payload.get("tool_name"):
                        decision, reason = "deny", "🚨 FAIL-CLOSED: Missing or invalid tool_name in payload."
                    else:
                        decision, reason, tool_label = evaluate_payload(payload)
                elif not isinstance(payload, dict) or not isinstance(payload.get("toolCall"), dict):
                    decision, reason = "deny", "🚨 FAIL-CLOSED: Unknown or malformed payload shape (missing toolCall object)."
                elif not payload["toolCall"].get("name") or not isinstance(payload["toolCall"].get("name"), str):
                    decision, reason = "deny", "🚨 FAIL-CLOSED: Missing or invalid tool name in toolCall."
                else:
                    decision, reason, tool_label = evaluate_payload(payload)
    except Exception as e:
        sys.stderr.write(f"[PRE-TRADE-GUARD INTERNAL ERROR] {str(e)}\n")
        decision, reason = "deny", f"🚨 FAIL-CLOSED: Pre-trade guard internal error ({str(e)}). Cannot verify safety — order blocked."

    _write_heartbeat(mode, tool_label, decision)
    try:
        return emit_decision(decision, reason, mode=mode)
    except Exception:
        return 0 if mode == "agy" else 2


if __name__ == "__main__":
    exit_code = main()
    if "--agy" in sys.argv[1:]:
        # Antigravity contract: decision is conveyed via JSON stdout; always exit 0.
        sys.exit(0)
    sys.exit(exit_code if exit_code is not None else 0)
