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
8. PASS-THROUGH:
   Tool calls unrelated to trading return "ask" so the runtime's normal permission policy applies.
   "allow" is reserved for calls that passed every trading gate or are purely risk-reducing.
9. HEARTBEAT:
   Every invocation refreshes logs/hook_heartbeat.json (best effort, never alters the decision).

Target latency: < 15ms (plus dossier provenance re-verification on trade openings).
"""

import os
import sys
import json
import time
import re
import shlex
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
REDIRECT_TOKENS = {">", ">>", ">|", "&>", "&>>", ">&"}
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
SESSION_STATE_RE = re.compile(r"session_state\.json", re.IGNORECASE)
HARNESS_PATH_CMD_RE = re.compile(
    r"scripts[\\/]+hooks[\\/]|\.agents[\\/]+hooks\.json|dossier_provenance\.py|record_evaluation\.py|"
    r"\.agents[\\/]+agents[\\/]|\.claude[\\/]+agents[\\/]|\.claude[\\/]+settings|config[\\/]+user_profile\.json",
    re.IGNORECASE,
)
INLINE_WRITE_MARKERS_RE = re.compile(r"\.write\s*\(|dump\s*\(|open\s*\([^)]*['\"][wax]\+?b?['\"]|os\.(?:remove|unlink|replace|rename)|shutil\.", re.IGNORECASE)

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
def _tokenize(command_line: str) -> List[str]:
    try:
        lexer = shlex.shlex(command_line, posix=True, punctuation_chars="();<>|&\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
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


def _redirect_targets(tokens: List[str]) -> List[str]:
    targets = []
    for i, tok in enumerate(tokens):
        if tok in REDIRECT_TOKENS and i + 1 < len(tokens):
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

    for tokens in subcommands:
        text = " ".join(tokens)
        prog = _program(tokens)

        # 4. Ground truth ledger must only be written by sync_session_state.py
        if SESSION_STATE_RE.search(text) and _subcommand_writes_path(tokens, text, SESSION_STATE_RE, inline):
            result["deny"] = (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Ground Truth Protection): logs/session_state.json may only be "
                "written by `python3 scripts/sync_session_state.py`."
            )
            return result

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
# File writes (evaluation trail & harness protection)
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
    if rel_l == "logs/session_state.json":
        return "deny", (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Ground Truth Protection): logs/session_state.json may only be "
            "written by `python3 scripts/sync_session_state.py`."
        )
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
