#!/usr/bin/env python3
"""
execute_futures_trade.py - Execution Engine and Risk Management Harness for Binance Futures.
Supports Testnet and Prod, strict filter calculations, symmetric orders, and verified Algo Stop Loss.

CLI usage (every mode accepts --env {prod,testnet}, resolved via utils.env_resolver.resolve_env,
and --json; modes are mutually exclusive):

  # Read-only position listing (exit 0 ok, 1 API error)
  python3 scripts/execute_futures_trade.py --positions [--env prod] [--json]

  # Move the Stop Loss to True Net Break-Even (exit 0 success, 1 failure, 2 refused by rule)
  python3 scripts/execute_futures_trade.py --move-breakeven --symbol BTCUSDT [--force] [--is-yolo] [--env prod] [--json]

  # Risk-reducing maintenance (always emit JSON)
  python3 scripts/execute_futures_trade.py --close-position --symbol BTCUSDT [--env prod]
  python3 scripts/execute_futures_trade.py --audit-orphans [--env prod]
  python3 scripts/execute_futures_trade.py --auto-heal [--env prod]
  python3 scripts/execute_futures_trade.py --protect-pending [--env prod]   # also run by the position guardian

  # New position (requires an APPROVED clean-room dossier; always emits JSON)
  python3 scripts/execute_futures_trade.py --symbol BTCUSDT --direction LONG --sl-price ... --tp1-price ... --tp2-price ...

JSON schemas (stable; extra keys may be added, existing keys are never renamed):

  --positions:
    {
      "success": bool,                 # false on positionRisk failure or when any order query failed
      "env": "prod" | "testnet",
      "timestamp": int,                # unix seconds
      "count": int,
      "all_protected": bool,           # every position has a verified protective stop
      "error": str | null,
      "positions": [{
        "symbol": str, "side": "LONG" | "SHORT", "size": float, "entry_price": float,
        "mark_price": float, "leverage": int, "margin_type": str, "isolated_margin": float,
        "unrealized_pnl": float, "roe_pct": float, "liquidation_price": float,
        "protected": bool,             # a protective stop is present on /fapi/v1/openAlgoOrders
        "stop_orders": [{"algo_id", "type", "side", "trigger_price": float, "quantity": float | null,
                         "close_position": bool, "reduce_only": bool}],
        "take_profit_orders": [{"order_id", "source": "algo" | "order", "type", "side",
                                "price": float, "quantity": float | null, "reduce_only": bool}],
        "orders_error": str | null
      }]
    }

  --move-breakeven:
    {
      "success": bool,
      "refused": bool,                 # true when a rule declined the move (exit code 2)
      "reason": str,                   # moved | already_at_breakeven | dry_run | no_position |
                                       # position_query_failed | orders_query_failed | filters_unavailable |
                                       # mark_price_unavailable | yolo_tp1_not_filled | insufficient_expansion |
                                       # breakeven_would_trigger_immediately | new_stop_unverified | invalid_env |
                                       # hedge_mode_unsupported
      "message": str, "error": str (only when success is false),
      "symbol": str, "env": str, "direction": "LONG" | "SHORT" | null,
      "entry_price": float | null, "mark_price": float | null, "breakeven_price": float | null,
      "old_stop": {"algo_id", "type", "side", "trigger_price"} | null,   # tightest stop before the move
      "old_stops": [ ...same shape... ],
      "new_stop": {"algo_id", "type", "side", "trigger_price"} | null,   # verified on openAlgoOrders
      "cancelled_old_stop_ids": [...], "warnings": [str],
      "rules": {"force": bool, "is_yolo": bool, "yolo_source": str | null, "tp1_filled": bool | null,
                "tp1_source": str | null, "atr_15m": float | null, "expansion_atr_multiple": float | null},
      "dry_run": bool
    }

  --close-position (exit 0 iff success; stops are cancelled only once positionRisk shows the position flat):
      success: {"success": true, "closed": {...last close response...}, "attempts": int, "cleanup_errors": [str]}
      failure: {"success": false, "error": str, "attempts"?: int, "position_amt"?: float,
                "stop_protected"?: bool | null, "stop_source"?: "kept" | "healed" | "none" | "unknown",
                "stop_note"?: str, "redundant_stops"?: [{...}], "closed"?: {...}, "heal"?: {...}}
                (null/"unknown": every stop read failed)
                `stop_protected` is True / False / None; consumers MUST treat anything but True as unprotected
                (never test `is False`). redundant_stops: other stops seen next to a heal placed after every
                pre-heal read failed (reported, never cancelled).
                (no position / unreadable positionRisk ("position state unknown") return only "error";
                hedge mode returns "error" and "hedge_mode": true)
  --audit-orphans / --auto-heal (exit 1 on "error", unhealed orphans or unknown_count > 0):
      {"total_active": int, "orphans_count": int, "unknown_count": int, "all_protected": bool,
      "positions": [{"symbol", "direction", "amount", "entry_price", "mark_price", "leverage", "unpnl",
                     "is_protected", "protection": "protected" | "orphan" | "unknown", "active_sl_orders",
                     "sl_triggers", "orders_error"?, "note"?, "auto_heal_attempted"?, "auto_heal_verified"?,
                     "healed_sl_price"?}], "error"?: str, "hedge_mode"?: true}
      ("unknown": openAlgoOrders unreadable after retries; never healed, not counted in orphans_count)
  --protect-pending (exit 0 iff ok): {"ok": bool, "env": str, "dry_run": bool,
      "actions": [{"type": "pending_protect_sl" | "pending_tp_placed" | "pending_abort" | "pending_timeout_cancel" |
                           "pending_dropped" | "pending_sl_crossed_close" | "pending_record_mismatch", "key", "symbol",
                "success": bool,
                "dry_run": bool, "detail": {...}}],
      "errors": [{"key", "symbol", "stage", "error"}],
      "warnings"?: [{"key", "symbol", "stage": "loss_cap_check" | "qty_check" | "loss_cap_drift" | "registry_lock" |
                     "deferral_report", "warning"}]}   # check deferred / drift tolerated / not errors
      (issue #118 loss cap; issue #126 total_qty: a record whose total_qty is below margin_usdt x leverage / price
      x 0.98 minus one stepSize is untrusted, pending_record_mismatch; issue #156: 3 consecutive deferred runs of a
      record (check_deferrals) file a HIGH issue; pending_tp_placed detail has fill_quality_flags)
  trade deployment: {"success": bool, "symbol", "direction", "leverage", "entry_price", "total_qty",
      "sl_price", "tp1_price", "tp2_price", ..., "error"?: str, "hard_gate_rejection"?: bool}
      Resting entries (untriggered STOP_MARKET via the algo order API, resting LIMIT) return
      "conditional_entry" / "pending_limit_entry": true and "pending_entry_key"; they are recorded in
      logs/pending_entries.json, require a live position guardian in PROD and count against max_open_positions.
      Issue #36: on KEYS their planned SL is pre-armed as a closePosition stop when not crossed ("prearm_status":
      "placed" | "rejected:<code-or-text>" | "skipped:mcp" | "skipped:crossed", "prearm_algo_id"); the guardian
      verifies it at fill and is the fallback; it is cancelled (by algo id) when the entry ends without a position.
      Issue #157: a rejected (except -2021) or unverified pre-arm adds "prearm_anomaly": {"status", "message"} and
      files a MEDIUM issue (the entry is kept). A MARKET entry's SL is verified by its own algo id only (an id-less
      placement response: by a stop at the SL within one tick that was not listed before the placement); a -4130 on
      that placement with no such stop (a leftover closePosition stop) auto-destructs and files a HIGH issue.
      In PROD every new entry is rejected while an opening order rests on the exchange without a registry record
      (find_unregistered_resting_entries; the position guardian reports them as unknown_resting_entry).
      PROD gates are anchored to the exchange (issue #101): logs/session_state.json and logs/pending_entries.json
      are caches. One live snapshot per order attempt (fetch_live_gate_snapshot: all-symbol positionRisk,
      openAlgoOrders, openOrders) feeds Gate 0A (max open positions), Gate 1 (delta-neutral, via
      utils/portfolio_exposure.compute_exposure) and the unregistered-entry check; each takes the stricter of the
      file and the live view, and a failed live read, a missing/corrupt session_state.json or a missing registry
      while opening orders rest rejects the order. PROD equity is read live from /fapi/v2/balance.
      Issue #119: Gate 1 classifies filled positions plus resting opening orders and also rejects an order that
      would itself tip a non-empty book heavy in its direction; Gate 2 sizes the loss cap on
      min(wallet balance, balance + unrealized PnL of the snapshot's positionRisk rows).
      Issue #126: a defaulted standard margin is sized on that same equity; algo rows with a final algoStatus
      are not resting; the registry match of a quantity-less algo without an id allows half a tick (tick_size
      stored in the record). Issue #127: the snapshot captures logs/pending_entries.json before its exchange reads
      (Gate 0A and Gate 1 use it). Issue #94: Gate 0A reads only the last 4 MiB of logs/trades_audit.jsonl and, in
      PROD, rejects when the file exists but cannot be read. Issue #40: registry writes hold
      logs/pending_entries.json.lock (PendingRegistryLockError when not acquired: nothing is written); resting entries
      also need the guardian's last cycle free of positions_sync / pending_* errors; an abort or close that does not
      end flat or protected files a CRITICAL/P0 issue (_report_abort_failure).
      Issue #207: Daily Loss Gate (check_daily_loss_gate, opening orders only, before any write): PROD reads today's
      UTC fills live and refuses a new entry at -daily_stop_r x risk of the start-of-day equity, after
      max_consecutive_sl full SLs, or (YOLO, flag or dossier) after yolo_max_daily_losses YOLO full losses; any read
      problem, MCP mode included, refuses (fail closed). TESTNET skips it. The entry audit record (and a resting
      entry's registry record) carries the "daily_loss_gate" state it passed.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
import math
import subprocess
import hmac
import hashlib
import urllib.parse
import urllib.request
import json
import logging
import threading
from decimal import Decimal, InvalidOperation, ROUND_DOWN

logger = logging.getLogger("execute_futures_trade")

try:
    from utils.env_resolver import resolve_env
except ImportError:
    try:
        from scripts.utils.env_resolver import resolve_env
    except ImportError:
        def resolve_env(env=None):
            return str(env).lower() if env else os.environ.get('BINANCE_API_ENV', 'testnet').lower()

try:
    from utils.env_resolver import find_workspace_root
except ImportError:
    def find_workspace_root():
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Single dossier gate shared with the PreToolUse hook (scripts/utils/dossier_provenance.py).
# If it cannot be imported, new positions fail closed in PROD (see enforce_evaluation_dossier).
try:
    from utils.dossier_provenance import validate_dossier_for_trade
except Exception:  # pragma: no cover - exercised only on broken installs
    validate_dossier_for_trade = None

# GATE 2 (YOLO loss cap) and GATE 3 (friction floor) limits, shared with the YOLO scanner and the screening
# pipeline (scripts/utils/gate_limits.py, issue #64).
from utils.gate_limits import (MIN_TP1_DISTANCE, PENDING_DRIFT_CAP_TOLERANCE, YOLO_MAX_LOSS_MARGIN_FRACTION,
                               YOLO_MIN_LOSS_CAP_USDT)
# Portfolio delta classification shared with sync_session_state.py; PROD gates apply it to the live exchange view.
from utils.portfolio_exposure import (compute_exposure, book_exposure, project_order, resting_opening_legs,
                                      unrealized_pnl_total, LONG_HEAVY, SHORT_HEAVY)

# Liquidation gate parameters
DEFAULT_MAINT_MARGIN_RATIO = 0.01      # Conservative fallback when /fapi/v1/leverageBracket is unavailable
LIQUIDATION_SAFETY_FRACTION = 0.80     # SL distance must be <= 80% of the entry->liquidation distance

def load_env(target_env=None):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = {}

    # 1. Base .env file if present
    env_path = os.path.join(base_dir, '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip().strip('"').strip("'")

    # 2. Environment-specific configuration file (config/environments/{env}.env or ENV_FILE)
    env_file = os.environ.get('ENV_FILE')
    effective_env = (target_env or os.environ.get('BINANCE_API_ENV') or config.get('BINANCE_API_ENV', 'testnet')).lower()
    norm_env = resolve_env(effective_env)

    if not env_file:
        cand = os.path.join(base_dir, 'config', 'environments', f'{norm_env}.env')
        if os.path.exists(cand):
            env_file = cand

    if env_file and os.path.exists(env_file):
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip().strip('"').strip("'")

    # 3. Environment variables from os.environ take highest precedence
    for k, v in os.environ.items():
        if k.startswith(('BINANCE_', 'LIVE_TRADING_', 'GMAIL_', 'NOTION_', 'GITHUB_')) or k in ['ENV', 'TARGET_ENV', 'BINANCE_API_ENV']:
            config[k] = v

    return config

def get_mcp_oauth_token(cfg: dict = None):
    """Retrieves official Binance Agentic OAuth token if authenticated via MCP."""
    if cfg:
        token = cfg.get('BINANCE_MCP_OAUTH_TOKEN') or cfg.get('BINANCE_OAUTH_TOKEN')
        if token and token.strip():
            return token.strip()

    env_token = os.environ.get('BINANCE_MCP_OAUTH_TOKEN') or os.environ.get('BINANCE_OAUTH_TOKEN')
    if env_token and env_token.strip():
        return env_token.strip()

    # Check BINANCE_MCP_OAUTH_PATH as explicit override path before fallback paths or defaults
    custom_path = (cfg.get('BINANCE_MCP_OAUTH_PATH') if cfg else None) or os.environ.get('BINANCE_MCP_OAUTH_PATH')
    if custom_path and os.path.exists(custom_path):
        try:
            with open(custom_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content.startswith('{'):
                    d = json.loads(content)
                    tok = (
                        d.get('https://agent.binance.com/mcp/agentic', {}).get('token', {}).get('access_token')
                        or d.get('token', {}).get('access_token')
                        or d.get('access_token')
                    )
                    if tok:
                        return tok
                elif content:
                    return content
        except Exception as e:
            logger.warning(f"Failed to read token from BINANCE_MCP_OAUTH_PATH ({custom_path}): {e}")

    # Fallback to loading prod.env if cfg not passed
    try:
        loaded_cfg = load_env(target_env='prod')
        token = loaded_cfg.get('BINANCE_MCP_OAUTH_TOKEN') or loaded_cfg.get('BINANCE_OAUTH_TOKEN')
        if token and token.strip():
            return token.strip()
        custom_from_loaded = loaded_cfg.get('BINANCE_MCP_OAUTH_PATH')
        if custom_from_loaded and os.path.exists(custom_from_loaded):
            with open(custom_from_loaded, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content.startswith('{'):
                    d = json.loads(content)
                    tok = (
                        d.get('https://agent.binance.com/mcp/agentic', {}).get('token', {}).get('access_token')
                        or d.get('token', {}).get('access_token')
                        or d.get('access_token')
                    )
                    if tok:
                        return tok
                elif content:
                    return content
    except Exception:
        pass

    fallback_paths = [
        os.path.expanduser('~/.gemini/antigravity/mcp_oauth_tokens.json'),
        os.path.expanduser('~/.config/antigravity/mcp_oauth_tokens.json')
    ]

    for p in fallback_paths:
        if p and os.path.exists(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                    tok = (
                        d.get('https://agent.binance.com/mcp/agentic', {}).get('token', {}).get('access_token')
                        or d.get('token', {}).get('access_token')
                        or d.get('access_token')
                    )
                    if tok:
                        return tok
            except Exception:
                continue
    return None

def _plain_decimal(value):
    """Plain decimal text of a number, sign kept ("0.00001", never "1e-05"): floats/ints via repr, str/Decimal as
    given."""
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return format(Decimal(repr(float(value))), 'f')
    return format(Decimal(str(value)), 'f')


def _mcp_json_dumps(obj):
    """json.dumps for the MCP gateway body that writes finite floats and Decimals as plain decimals (no exponent);
    same separators as json.dumps and json.loads round-trips to the same values. NaN / Infinity raise ValueError
    (never sent: the gateway body must be valid JSON)."""
    if isinstance(obj, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))}: {_mcp_json_dumps(v)}" for k, v in obj.items()) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ", ".join(_mcp_json_dumps(v) for v in obj) + "]"
    if isinstance(obj, bool) or obj is None:
        return json.dumps(obj)
    if isinstance(obj, float) and math.isfinite(obj):
        return _plain_decimal(obj)
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError(f"Out of range Decimal value is not JSON compliant: {obj}")
        return _plain_decimal(obj)
    return json.dumps(obj, allow_nan=False)


def call_binance_mcp(tool_name: str, args: dict = None, session_id: str = None):
    """Executes official Binance MCP tools via the agent.binance.com JSON-RPC gateway."""
    token = get_mcp_oauth_token()
    if not token:
        err_dict = {
            "error": "Binance MCP OAuth token not found in ~/.gemini/antigravity/mcp_oauth_tokens.json or BINANCE_MCP_OAUTH_TOKEN",
            "isError": True
        }
        logger.error(err_dict["error"])
        return err_dict

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }

    mcp_session_id = session_id or os.environ.get("MCP_SESSION_ID") or os.environ.get("BINANCE_MCP_SESSION_ID")
    if mcp_session_id:
        headers["Mcp-Session-Id"] = str(mcp_session_id).strip()

    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": args or {}
        }
    }
    try:
        # Inside the try: a non-finite number raises ValueError here and nothing is sent (issue #173).
        req = urllib.request.Request(
            "https://agent.binance.com/mcp/agentic",
            headers=headers,
            data=_mcp_json_dumps(payload).encode("utf-8")
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "error" in data:
                err_msg = str(data["error"])
                logger.error(f"Binance MCP Gateway JSON-RPC error: {err_msg}")
                return {"error": err_msg, "isError": True, "raw": data["error"]}

            result_obj = data.get("result", {})
            if isinstance(result_obj, dict) and result_obj.get("isError"):
                content_text = ""
                content_list = result_obj.get("content", [])
                if isinstance(content_list, list) and content_list:
                    content_text = content_list[0].get("text", "")
                err_msg = content_text or "MCP tool execution failed"
                logger.error(f"Binance MCP tool returned error: {err_msg}")
                return {"error": err_msg, "isError": True, "raw": result_obj}

            content = result_obj.get("content", [{}])[0].get("text", "") if isinstance(result_obj, dict) else ""
            if isinstance(content, str) and (content.startswith("[") or content.startswith("{")):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and parsed.get("code") and parsed.get("code") < 0:
                        logger.error(f"Binance API returned error inside MCP: {parsed}")
                        return {"error": parsed.get("msg", str(parsed)), "isError": True, "code": parsed.get("code"), "raw": parsed}
                    return parsed
                except Exception:
                    pass
            return content
    except urllib.error.HTTPError as he:
        try:
            err_body = he.read().decode("utf-8")
        except Exception:
            err_body = str(he)
        err_msg = f"MCP Gateway HTTP {he.code}: {err_body}"
        logger.error(err_msg)
        return {"error": err_msg, "isError": True, "http_code": he.code}
    except Exception as e:
        err_msg = f"MCP Gateway Error: {str(e)}"
        logger.error(err_msg)
        return {"error": err_msg, "isError": True}

def send_mcp_gateway_request(method, endpoint, params=None):
    """Routes Binance Futures requests directly to the Binance Agentic MCP gateway."""
    if params is None:
        params = {}
    
    # 1. Balance
    if endpoint in ['/fapi/v2/balance', '/fapi/v3/balance']:
        return call_binance_mcp('futures_usds.futuresAccountBalanceV3')

    # 2. Position Risk
    if endpoint in ['/fapi/v2/positionRisk', '/fapi/v1/positionRisk']:
        pos_data = call_binance_mcp('futures_usds.positionInformationV2')
        if isinstance(pos_data, list) and params.get('symbol'):
            return [p for p in pos_data if p.get('symbol') == params['symbol']]
        return pos_data

    # 3. Open Orders
    if endpoint == '/fapi/v1/openOrders':
        return call_binance_mcp('futures_usds.currentAllOpenOrders', {'symbol': params.get('symbol')} if params.get('symbol') else {})

    # 4. Open Algo Orders (Stop Loss / Take Profit triggers)
    if endpoint == '/fapi/v1/openAlgoOrders':
        algos = call_binance_mcp('futures_usds.currentAllAlgoOpenOrders', {'symbol': params.get('symbol')} if params.get('symbol') else {})
        if isinstance(algos, list):
            res_list = []
            for a in algos:
                o_type = a.get('orderType') or a.get('type')
                if o_type in ['STOP_MARKET', 'STOP', 'TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'TRAILING_STOP_MARKET'] or 'stopPrice' in a or 'triggerPrice' in a:
                    res_list.append({
                        'algoId': a.get('algoId') or a.get('orderId'),
                        'symbol': a.get('symbol'),
                        'side': a.get('side'),
                        'triggerPrice': float(a.get('triggerPrice') or a.get('stopPrice') or 0),
                        'orderType': o_type,
                        'closePosition': _truthy(a.get('closePosition', False)) or _truthy(a.get('reduceOnly', False))
                    })
            return res_list
        return algos

    # 5. Leverage
    if endpoint == '/fapi/v1/leverage' and method.upper() == 'POST':
        return call_binance_mcp('futures_usds.changeInitialLeverage', {'symbol': params['symbol'], 'leverage': int(params['leverage'])})

    # 6. Margin Type
    if endpoint == '/fapi/v1/marginType' and method.upper() == 'POST':
        return call_binance_mcp('futures_usds.changeMarginType', {'symbol': params['symbol'], 'marginType': params['marginType']})

    # 7. Cancel Single Order
    if endpoint == '/fapi/v1/order' and method.upper() == 'DELETE':
        return call_binance_mcp('futures_usds.cancelOrder', {'symbol': params['symbol'], 'orderId': int(params['orderId'])})

    # 8. Cancel Algo Order
    if endpoint == '/fapi/v1/algoOrder' and method.upper() == 'DELETE':
        order_id = params.get('algoId') or params.get('orderId')
        return call_binance_mcp('futures_usds.cancelAlgoOrder', {'algoId': int(order_id)})

    # 9. Cancel All Open Orders
    if endpoint == '/fapi/v1/allOpenOrders' and method.upper() == 'DELETE':
        open_orders = call_binance_mcp('futures_usds.currentAllOpenOrders', {'symbol': params.get('symbol')} if params.get('symbol') else {})
        canceled = []
        if isinstance(open_orders, list):
            for o in open_orders:
                c_res = call_binance_mcp('futures_usds.cancelOrder', {'symbol': o['symbol'], 'orderId': int(o['orderId'])})
                canceled.append(c_res)
        return canceled

    # 10. Place New Order
    if endpoint == '/fapi/v1/order' and method.upper() == 'POST':
        if params.get('type') in ['STOP_MARKET', 'TAKE_PROFIT_MARKET', 'STOP', 'TAKE_PROFIT']:
            return send_mcp_gateway_request('POST', '/fapi/v1/algoOrder', params)
        mcp_args = {
            'symbol': params['symbol'],
            'side': params['side'],
            'type': params['type']
        }
        if 'quantity' in params:
            mcp_args['quantity'] = float(params['quantity'])
        if 'price' in params:
            mcp_args['price'] = float(params['price'])
        if 'stopPrice' in params:
            mcp_args['stopPrice'] = float(params['stopPrice'])
        if 'timeInForce' in params and params['type'] in ['LIMIT', 'STOP', 'TAKE_PROFIT']:
            mcp_args['timeInForce'] = params['timeInForce']
        if 'reduceOnly' in params:
            mcp_args['reduceOnly'] = str(params['reduceOnly']).lower()
        if 'closePosition' in params:
            mcp_args['closePosition'] = str(params['closePosition']).lower()
        return call_binance_mcp('futures_usds.newOrder', mcp_args)

    # 11. Place Algo Stop Loss / Conditional Order
    if endpoint == '/fapi/v1/algoOrder' and method.upper() == 'POST':
        trig_p = float(params.get('triggerPrice') or params.get('stopPrice', 0))
        close_pos = str(params.get('closePosition', 'true')).lower()
        order_type = params.get('type', 'STOP_MARKET')
        mcp_args = {
            'symbol': params['symbol'],
            'side': params['side'],
            'type': order_type,
            'algoType': 'CONDITIONAL',
            'triggerPrice': _plain_decimal(trig_p),
        }
        if 'workingType' in params:
            mcp_args['workingType'] = params['workingType']
        # MCP Gateway requires 'quantity' even with closePosition=true.
        # Always include quantity: from params, or look up from exchange.
        qty = params.get('quantity')
        if not qty and close_pos == 'true':
            try:
                pos_data = call_binance_mcp('futures_usds.positionInformationV2')
                if isinstance(pos_data, list):
                    for p in pos_data:
                        if p.get('symbol') == params['symbol']:
                            pos_amt = abs(float(p.get('positionAmt', 0)))
                            if pos_amt > 0:
                                qty = pos_amt
                                break
            except Exception:
                pass
        if qty:
            mcp_args['quantity'] = format_order_qty(qty)
        if close_pos == 'true':
            if qty:
                # closePosition cannot be combined with quantity: a quantity-based protective stop MUST be
                # reduce-only, otherwise it could open a reverse position once the original one is gone.
                mcp_args['reduceOnly'] = 'true'
            else:
                mcp_args['closePosition'] = 'true'
        elif 'reduceOnly' in params and str(params['reduceOnly']).lower() == 'true':
            mcp_args['reduceOnly'] = 'true'

        # Use hidden tool futures_usds.newAlgoOrder via tool_execute
        res = call_binance_mcp('tool_execute', {
            'toolName': 'futures_usds.newAlgoOrder',
            'arguments': mcp_args
        })
        if isinstance(res, dict) and 'algoId' not in res:
            # The verification of the stop is by algo id (issue #157): map the gateway's id to a top-level algoId,
            # from orderId or from an id nested one level (e.g. {"data": {"orderId": N}}, {"result": {"algoId": N}}).
            if 'orderId' in res:
                res['algoId'] = res['orderId']
            else:
                for inner in (res.get('data'), res.get('result')):
                    if isinstance(inner, dict) and (inner.get('algoId') is not None or inner.get('orderId') is not None):
                        res['algoId'] = inner['algoId'] if inner.get('algoId') is not None else inner['orderId']
                        break
        return res

    # 12. Notional & leverage brackets (read-only; used by the liquidation gate).
    # Any error here makes the caller fall back to the conservative DEFAULT_MAINT_MARGIN_RATIO.
    if endpoint == '/fapi/v1/leverageBracket' and method.upper() == 'GET':
        return call_binance_mcp('futures_usds.notionalAndLeverageBrackets', {'symbol': params['symbol']} if params.get('symbol') else {})

    # Public fallbacks: time, ticker, exchangeInfo
    public_url = f"https://fapi.binance.com{endpoint}"
    if params:
        qs = urllib.parse.urlencode(params)
        public_url = f"{public_url}?{qs}"
    try:
        req = urllib.request.Request(public_url, headers={'User-Agent': 'BinanceAgentic/1.0'}, method=method.upper())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {"error": f"Public fallback error: {str(e)}"}

def get_client_config(target_env=None):
    norm_env = resolve_env(target_env)
    cfg = load_env(target_env=norm_env)
    
    if norm_env == 'prod':
        auth_mode = str(cfg.get('BINANCE_AUTH_MODE', '')).strip().lower()
        mcp_token = get_mcp_oauth_token(cfg)
        # Explicit MCP mode uses the Agentic Gateway; explicit KEYS mode always uses HMAC keys.
        # Without an explicit mode, fall back to MCP only when an OAuth token exists and no keys are configured.
        has_keys = bool(cfg.get('BINANCE_PROD_API_KEY') or cfg.get('BINANCE_API_KEY'))
        if auth_mode == 'mcp' or (auth_mode != 'keys' and mcp_token and not has_keys):
            return "MCP_OAUTH_ACTIVE", mcp_token or "mcp_token", "https://fapi.binance.com"

        api_key = cfg.get('BINANCE_PROD_API_KEY') or cfg.get('BINANCE_API_KEY', '')
        secret_key = cfg.get('BINANCE_PROD_SECRET_KEY') or cfg.get('BINANCE_SECRET_KEY', '')
        base_url = cfg.get('BINANCE_FUTURES_BASE_URL') or 'https://fapi.binance.com'
    else:
        api_key = cfg.get('BINANCE_TESTNET_API_KEY') or cfg.get('BINANCE_API_KEY', '')
        secret_key = cfg.get('BINANCE_TESTNET_SECRET_KEY') or cfg.get('BINANCE_SECRET_KEY', '')
        base_url = cfg.get('BINANCE_FUTURES_BASE_URL') or 'https://testnet.binancefuture.com'
    
    if not api_key or 'PEGA_AQUI' in api_key:
        return None, None, None
        
    return api_key, secret_key, base_url

_SERVER_OFFSET = {}

def get_server_time_offset(base_url, force_refresh=False):
    now = time.time()
    cached = _SERVER_OFFSET.get(base_url)
    if force_refresh or not cached or (now - cached.get('time', 0)) > 30:
        try:
            t0 = time.time()
            req = urllib.request.Request(f"{base_url}/fapi/v1/time", headers={'User-Agent': 'BinanceAgentic/1.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                t1 = time.time()
                server_time = data.get('serverTime', 0)
                mid_local = int(((t0 + t1) / 2.0) * 1000)
                offset = server_time - mid_local
                _SERVER_OFFSET[base_url] = {'offset': offset, 'time': now}
        except Exception:
            _SERVER_OFFSET[base_url] = {'offset': 0, 'time': now}
    return _SERVER_OFFSET.get(base_url, {}).get('offset', 0)

def send_signed_request(method, endpoint, params=None, target_env=None, retry_count=0):
    target_env = resolve_env(target_env)
    api_key, secret_key, base_url = get_client_config(target_env)
    if not api_key:
        return {"error": "Binance credentials not configured in .env"}

    if api_key == "MCP_OAUTH_ACTIVE":
        return send_mcp_gateway_request(method, endpoint, params=params)

    if params is None:
        params = {}

    offset = get_server_time_offset(base_url)
    # 1500ms safety buffer to ensure timestamp never races ahead of server clock
    params['timestamp'] = int(time.time() * 1000) + offset - 1500
    params['recvWindow'] = 60000

    query_str = urllib.parse.urlencode(params)
    signature = hmac.new(secret_key.encode('utf-8'), query_str.encode('utf-8'), hashlib.sha256).hexdigest()
    full_url = f"{base_url}{endpoint}?{query_str}&signature={signature}"

    req = urllib.request.Request(full_url, headers={
        'X-MBX-APIKEY': api_key,
        'User-Agent': 'BinanceAgentic/1.0'
    }, method=method)

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
            return data
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        parsed = None
        try:
            parsed = json.loads(err_body)
        except Exception:
            pass

        # If Binance returns -1021 in HTTP 400, force re-synchronization and retry
        if isinstance(parsed, dict) and parsed.get('code') == -1021 and retry_count < 2:
            get_server_time_offset(base_url, force_refresh=True)
            return send_signed_request(method, endpoint, params=params, target_env=target_env, retry_count=retry_count + 1)

        return parsed if parsed is not None else {"error": f"HTTP {e.code}: {err_body}"}
    except Exception as e:
        return {"error": str(e)}

def get_symbol_filters(symbol, target_env=None):
    target_env = resolve_env(target_env)
    res = send_signed_request('GET', '/fapi/v1/exchangeInfo', target_env=target_env)
    for s in res.get('symbols', []):
        if s['symbol'] == symbol:
            lot = [f for f in s['filters'] if f['filterType'] == 'LOT_SIZE'][0]
            price = [f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER'][0]
            notional = [f for f in s['filters'] if f['filterType'] == 'MIN_NOTIONAL']
            min_notional = float(notional[0]['notional']) if notional else 5.0
            return {
                'stepSize': float(lot['stepSize']),
                'minQty': float(lot['minQty']),
                'tickSize': float(price['tickSize']),
                'precision_qty': abs(Decimal(str(lot['stepSize'])).as_tuple().exponent),
                'precision_price': abs(Decimal(str(price['tickSize'])).as_tuple().exponent),
                'minNotional': min_notional
            }
    return None

def round_step(val, step, prec):
    d_val = Decimal(str(val))
    d_step = Decimal(str(step))
    rounded = (d_val // d_step) * d_step
    return float(f"{rounded:.{prec}f}")

def format_order_qty(value):
    """Order quantity as a plain decimal string ("0.00001", never "1e-05" or "-0"). Accepts the raw positionAmt
    string or a number; the sign is dropped (the order side carries the direction). No exchangeInfo call."""
    try:
        d = Decimal(str(value)).copy_abs().normalize()
    except (InvalidOperation, TypeError, ValueError):
        d = Decimal(repr(abs(_to_float(value)))).normalize()
    return format(d, "f")

def round_price(val, step, prec):
    d_val = Decimal(str(val))
    d_step = Decimal(str(step))
    rounded = (d_val / d_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_step
    return float(f"{rounded:.{prec}f}")

def margin_type_isolated_confirmed(margin_res):
    """
    Interprets the response of POST /fapi/v1/marginType (ISOLATED), via HMAC REST or the MCP gateway.
    Returns (ok, reason). Only an explicit success or Binance -4046 ("No need to change margin type",
    i.e. already ISOLATED) is accepted; anything else (errors, -4047/-4048 open orders/positions,
    network failures, empty/unknown payloads) fails closed.
    """
    text = str(margin_res)
    lowered = text.lower()
    if '-4046' in text or 'no need to change margin type' in lowered:
        return True, "Margin type already ISOLATED (-4046)."
    if isinstance(margin_res, dict):
        code = margin_res.get('code')
        try:
            code_int = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_int = None
        if margin_res.get('isError') or 'error' in margin_res or (code_int is not None and code_int < 0):
            err = margin_res.get('error') or margin_res.get('msg') or margin_res.get('message') or text
            return False, f"Failed to set ISOLATED margin ({err})."
        if code_int == 200 or str(margin_res.get('msg', '')).lower() == 'success':
            return True, "Margin type set to ISOLATED."
        if margin_res:
            return True, "Margin type set to ISOLATED."
        return False, "Empty response when setting ISOLATED margin."
    if isinstance(margin_res, str) and 'success' in lowered and 'error' not in lowered:
        return True, "Margin type set to ISOLATED."
    return False, f"Unexpected response when setting ISOLATED margin ({text})."


def setup_margin_and_leverage(symbol, leverage, target_env=None):
    """
    1. Forces ISOLATED margin (fail-closed: on any failure other than -4046 the leverage is not touched
       and (None, margin_res, None) is returned so the caller aborts the trade).
    2. Sets leverage; on the generic sub-account cap (-4421) auto-clamps to 5x.
    Returns (lev_res, margin_res, confirmed_leverage).
    """
    target_env = resolve_env(target_env)
    # 1. Set Margin Type to ISOLATED (Binance returns error -4046 if already isolated, which is normal)
    margin_res = send_signed_request('POST', '/fapi/v1/marginType', {'symbol': symbol, 'marginType': 'ISOLATED'}, target_env=target_env)
    margin_ok, _ = margin_type_isolated_confirmed(margin_res)
    if not margin_ok:
        return None, margin_res, None

    # 2. Set Leverage
    lev_res = send_signed_request('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage}, target_env=target_env)

    confirmed_leverage = leverage
    err_str = str(lev_res)
    # Subaccount leverage cap (-4421): Binance restricts subaccounts to 5x max leverage
    if isinstance(lev_res, dict) and (lev_res.get('isError') or 'error' in lev_res or lev_res.get('code') in [-4421, -32603]):
        if '-4421' in err_str or 'Subaccounts are restricted from using leverage greater than 5x' in err_str:
            if leverage > 5:
                print(f"⚠️ Subaccount leverage ceiling detected (-4421). Clamping leverage from {leverage}x to 5x max for {symbol}.", file=sys.stderr)
                lev_res = send_signed_request('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': 5}, target_env=target_env)
                confirmed_leverage = 5

    if isinstance(lev_res, dict) and 'leverage' in lev_res:
        try:
            confirmed_leverage = int(lev_res['leverage'])
        except Exception:
            pass

    return lev_res, margin_res, confirmed_leverage


def place_algo_stop_loss(symbol, exit_side, sl_price, target_env=None, quantity=None):
    """
    Places a protective STOP_MARKET algo order.
    - quantity=None: closePosition=true (initial stop on a fresh position).
    - quantity set: quantity-based reduceOnly=true stop. Used by place-then-cancel replacements, because
      Binance rejects a second closePosition stop on the same side while the old one is still active.
    """
    target_env = resolve_env(target_env)
    try:
        filters = get_symbol_filters(symbol, target_env=target_env)
        if filters and 'tickSize' in filters and 'precision_price' in filters:
            sl_price = round_price(sl_price, filters['tickSize'], filters['precision_price'])
    except Exception:
        pass
    params = {
        'algoType': 'CONDITIONAL',
        'symbol': symbol,
        'side': exit_side,
        'type': 'STOP_MARKET',
        'triggerPrice': sl_price,
    }
    if quantity:
        params['quantity'] = quantity
        params['reduceOnly'] = 'true'
    else:
        params['closePosition'] = 'true'
    res = send_signed_request('POST', '/fapi/v1/algoOrder', params, target_env=target_env)
    if isinstance(res, dict) and ('algoId' in res or 'orderId' in res):
        return res
    profile = 'testnet' if target_env == 'testnet' else 'prod'
    size_args = ['--quantity', str(quantity), '--reduce-only', 'true'] if quantity else ['--close-position', 'true']
    cmd = [
        'binance-cli', 'futures-usds', 'new-algo-order',
        '--algo-type', 'CONDITIONAL',
        '--symbol', symbol,
        '--side', exit_side,
        '--type', 'STOP_MARKET',
        '--trigger-price', str(sl_price),
        *size_args,
        '--recv-window', '60000',
        '--profile', profile
    ]
    try:
        res_cli = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        parsed = json.loads(res_cli.stdout)
        if isinstance(parsed, dict) and ('algoId' in parsed or 'orderId' in parsed):
            return parsed
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return res

def verify_algo_stop_loss(symbol, exit_side, sl_price=None, target_env=None, algo_id=None):
    """
    Verifies that the Algo Stop Loss order actually exists and is active on the exchange.
    Ensures that if sl_price is specified, True is ONLY returned if the price matches within 3% tolerance.
    Only real protective stops count (is_protective_stop): a resting conditional ENTRY on the same side
    (neither closePosition nor reduceOnly) never verifies as a Stop Loss.
    Issue #157: with algo_id, only the protective stop with that exact algo id counts (no price fallback), so a
    leftover stop (e.g. an old pre-arm at the same price) never confirms another entry's stop.
    """
    target_env = resolve_env(target_env)
    try:
        algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}, target_env=target_env)
        if isinstance(algos, list):
            for ao in algos:
                if is_protective_stop(ao, exit_side, symbol):
                    if algo_id is not None:
                        if str(_order_id(ao)) == str(algo_id):
                            return True, ao
                    elif sl_price is not None:
                        trig = float(ao.get('triggerPrice') or ao.get('stopPrice') or 0)
                        if trig > 0 and abs(trig - float(sl_price)) / trig < 0.03:
                            return True, ao
                    else:
                        return True, ao
        return False, None
    except Exception as e:
        return False, str(e)


# -----------------------------------------------------------------------------
# Protective stop helpers (shared by break-even, structural trailing, orphan healing and the guardian)
# -----------------------------------------------------------------------------
STOP_ORDER_TYPES = ('STOP_MARKET', 'STOP')
TAKE_PROFIT_ORDER_TYPES = ('TAKE_PROFIT_MARKET', 'TAKE_PROFIT')
TRUE_NET_BE_FEE_BUFFER = 0.002          # True Net Break-Even: entry +/- 0.2% roundtrip taker fee buffer
BE_MIN_EXPANSION_ATR = 2.0              # Anti-truncation: standard positions move to BE only after >= 2x ATR_15m
STOP_VERIFY_RETRY_DELAYS = (0.8, 1.0, 1.2)  # Progressive verification (~3s) to absorb Mainnet indexing latency
ORPHAN_HEAL_SL_DISTANCE = 0.025         # Emergency stop distance for orphan positions (2.5%)
CLOSE_RETRY_DELAYS = (0.3, 0.6, 1.0)    # close_position_market: backoff after each of the 3 close attempts


def _workspace_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _truthy(value):
    return value is True or str(value).strip().lower() in ('true', '1', 'yes')


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_api_error(res):
    if not isinstance(res, (dict, list)):
        return True
    if isinstance(res, dict):
        if res.get('isError') or 'error' in res:
            return True
        try:
            return 'code' in res and int(res.get('code')) < 0
        except (TypeError, ValueError):
            return False
    return False


def _order_type(order):
    return str(order.get('orderType') or order.get('type') or '').upper()


def _trigger_price(order):
    return _to_float(order.get('triggerPrice') or order.get('stopPrice') or 0)


def _order_id(order):
    oid = order.get('algoId')
    if oid is None:
        oid = order.get('orderId')
    return oid


def is_protective_stop(order, exit_side, symbol=None):
    """A STOP/STOP_MARKET on the exit side with a positive trigger. Conditional ENTRY stops on the same side
    (neither closePosition nor reduceOnly) are not protective and are never touched."""
    if not isinstance(order, dict) or _order_type(order) not in STOP_ORDER_TYPES:
        return False
    if str(order.get('side', '')).upper() != str(exit_side).upper():
        return False
    if symbol and order.get('symbol') and str(order.get('symbol')).upper() != str(symbol).upper():
        return False
    if _trigger_price(order) <= 0:
        return False
    if ('closePosition' in order or 'reduceOnly' in order) and not (
            _truthy(order.get('closePosition')) or _truthy(order.get('reduceOnly'))):
        return False
    return True


def get_open_stop_orders(symbol, exit_side, target_env=None):
    """Read-only. Returns (protective_stops, error_or_None) from /fapi/v1/openAlgoOrders."""
    try:
        algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}, target_env=target_env)
    except Exception as e:
        return [], f"openAlgoOrders query failed: {e}"
    if not isinstance(algos, list):
        return [], f"openAlgoOrders query failed: {algos}"
    return [ao for ao in algos if is_protective_stop(ao, exit_side, symbol)], None


def get_open_stop_orders_with_retry(symbol, exit_side, target_env=None, retry_delays=STOP_VERIFY_RETRY_DELAYS):
    """get_open_stop_orders retried with retry_delays: the first successful (stops, None), else the last
    (stops, error)."""
    stops, err = [], None
    for delay in (0.0,) + tuple(retry_delays):
        if delay:
            time.sleep(delay)
        stops, err = get_open_stop_orders(symbol, exit_side, target_env=target_env)
        if err is None:
            return stops, None
    return stops, err


def _is_explicit_rejection(placement):
    """True when an order placement was explicitly rejected by Binance: no order id and a negative code other than
    the "unknown error / execution status unknown / server overloaded" codes, after which the order may exist. Transport and MCP errors
    without a code are not explicit rejections."""
    if not isinstance(placement, dict) or _order_id(placement) is not None:
        return False
    try:
        code = int(placement.get('code'))
    except (TypeError, ValueError):
        return False
    return code < 0 and code not in (-1000, -1001, -1006, -1007, -1008)


HEDGE_MODE_UNSUPPORTED = ("hedge mode (dualSidePosition=true) is not supported by the desk: reduce-only closes and "
                          "stops are sent without positionSide and Binance rejects them (-4061). Manage the position "
                          "manually on Binance or switch the account to one-way mode.")


def _is_hedge_mode_row(row):
    """positionRisk row of a hedge-mode account (positionSide LONG/SHORT). A missing field counts as BOTH."""
    return isinstance(row, dict) and str(row.get('positionSide') or 'BOTH').upper() != 'BOTH'


def tightest_stop(stops, is_long):
    """The stop closest to price (highest trigger for LONG, lowest for SHORT)."""
    if not stops:
        return None
    return max(stops, key=_trigger_price) if is_long else min(stops, key=_trigger_price)


def stop_summary(order):
    if not isinstance(order, dict):
        return None
    return {
        'algo_id': _order_id(order),
        'type': _order_type(order) or None,
        'side': order.get('side'),
        'trigger_price': _trigger_price(order),
    }


def is_tighter_stop(new_price, old_price, is_long):
    """True only if new_price strictly reduces risk vs old_price (never loosens)."""
    if not old_price or old_price <= 0:
        return True
    return new_price > old_price if is_long else new_price < old_price


def wait_for_stop_confirmation(symbol, exit_side, price, exclude_ids=(), algo_id=None, tick_size=None,
                               target_env=None, retry_delays=STOP_VERIFY_RETRY_DELAYS):
    """
    Progressive verification on /fapi/v1/openAlgoOrders that a protective stop at `price` exists and is not one
    of `exclude_ids` (the stops being replaced). Matches by algo id when known, otherwise by trigger price within
    one tick (or 0.05%). A tight tolerance avoids mistaking the OLD stop for the new one.
    Returns (verified, order_or_None).
    """
    excluded = {str(x) for x in exclude_ids if x is not None}
    price = float(price)
    tol = max(_to_float(tick_size) * 1.01, abs(price) * 0.0005)
    for delay in (0.0,) + tuple(retry_delays):
        if delay:
            time.sleep(delay)
        stops, err = get_open_stop_orders(symbol, exit_side, target_env=target_env)
        if err:
            continue
        for ao in stops:
            oid = _order_id(ao)
            if oid is not None and str(oid) in excluded:
                continue
            if algo_id is not None and oid is not None and str(oid) == str(algo_id):
                return True, ao
            if abs(_trigger_price(ao) - price) <= tol:
                return True, ao
    return False, None


def replace_protective_stop(symbol, exit_side, new_price, quantity, old_stops, target_env=None, tick_size=None):
    """
    PLACE-THEN-CANCEL stop replacement. The position is never left without a stop:
      1. place the new reduce-only stop,
      2. verify it on /fapi/v1/openAlgoOrders (progressive retries),
      3. only then cancel the old stop(s).
    If the new stop cannot be verified, NOTHING is cancelled and success is False.
    Returns {success, reason, new_stop, placement, cancelled_old_stop_ids, cancel_errors}.
    """
    old_ids = [_order_id(ao) for ao in (old_stops or []) if isinstance(ao, dict) and _order_id(ao) is not None]
    try:
        placement = place_algo_stop_loss(symbol, exit_side, new_price, target_env=target_env, quantity=quantity)
    except Exception as e:
        placement = {"error": f"placement exception: {e}"}
    placed_id = _order_id(placement) if isinstance(placement, dict) else None

    verified, info = wait_for_stop_confirmation(
        symbol, exit_side, new_price, exclude_ids=old_ids, algo_id=placed_id,
        tick_size=tick_size, target_env=target_env,
    )
    if not verified:
        return {
            "success": False,
            "reason": "new_stop_unverified",
            "new_stop": None,
            "placement": placement,
            "cancelled_old_stop_ids": [],
            "cancel_errors": [],
        }

    new_id = _order_id(info)
    cancelled, cancel_errors = [], []
    for oid in old_ids:
        if new_id is not None and str(oid) == str(new_id):
            continue
        try:
            res = send_signed_request('DELETE', '/fapi/v1/algoOrder', {'symbol': symbol, 'algoId': oid}, target_env=target_env)
        except Exception as e:
            res = {"error": str(e)}
        if isinstance(res, dict) and _is_api_error(res):
            cancel_errors.append({"algo_id": oid, "error": res.get('error') or res.get('msg') or str(res)})
        else:
            cancelled.append(oid)
    return {
        "success": True,
        "reason": "replaced",
        "new_stop": stop_summary(info),
        "placement": placement,
        "cancelled_old_stop_ids": cancelled,
        "cancel_errors": cancel_errors,
    }


def latest_trade_audit_record(symbol, base_dir=None):
    """Most recent entry record for `symbol` in logs/trades_audit.jsonl (failsafe/abort events are skipped)."""
    path = os.path.join(base_dir or _workspace_dir(), 'logs', 'trades_audit.jsonl')
    if not os.path.exists(path):
        return None
    symbol = str(symbol).upper()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get('event') or str(rec.get('symbol', '')).upper() != symbol:
            continue
        if 'total_qty' in rec:
            return rec
    return None


def detect_yolo_position(symbol, leverage=None, explicit=None, base_dir=None, *, record=None, audit_fallback=True):
    """
    Returns (is_yolo, source). A position is YOLO if any of:
      - explicit flag (can only mark a position as YOLO, never un-mark it),
      - the evaluation dossier candidate for the symbol has is_yolo,
      - the latest trades_audit entry for the symbol has is_yolo (`record`, when given, is used instead of the
        latest entry: the caller's already-matched trade reference),
      - leverage >= profile leverage_yolo, or leverage > profile leverage_standard (the executor refuses
        leverage above leverage_standard for non-YOLO trades).
    audit_fallback=False with record None skips the latest-entry lookup (no stale record of another trade).
    """
    if _truthy(explicit):
        return True, 'explicit_flag'
    base = base_dir or _workspace_dir()
    try:
        from utils.dossier_provenance import default_dossier_path, load_dossier, find_candidate
        cand = find_candidate(load_dossier(default_dossier_path(base)), symbol)
        if isinstance(cand, dict) and _truthy(cand.get('is_yolo')):
            return True, 'dossier_candidate'
    except Exception:
        pass
    if record is not None:
        rec = record
    else:
        rec = latest_trade_audit_record(symbol, base) if audit_fallback else None
    if rec and _truthy(rec.get('is_yolo')):
        return True, 'trade_audit'
    if leverage:
        try:
            import user_profile as up
            prof = up.load_user_profile()
        except Exception:
            prof = {}
        try:
            lev = int(float(leverage))
            lev_yolo = int(prof.get('leverage_yolo', 15))
            lev_std = int(prof.get('leverage_standard', 3))
            if lev >= lev_yolo or lev > lev_std:
                return True, 'leverage'
        except (TypeError, ValueError):
            pass
    return False, None


def detect_tp1_filled(symbol, current_qty, base_dir=None, *, record=None):
    """
    Returns (filled, source). TP1 is considered filled when the live position size has been reduced by at least
    half of the TP1 quantity recorded at entry in logs/trades_audit.jsonl. Returns (None, ...) when unknown.
    `record`, when given, is used instead of the latest entry for the symbol (the caller's matched reference).
    """
    rec = record if record is not None else latest_trade_audit_record(symbol, base_dir)
    if not rec:
        return None, 'no_audit_record'
    total = _to_float(rec.get('total_qty'))
    if total <= 0:
        return None, 'no_audit_record'
    tp1_qty = _to_float(rec.get('tp1_qty')) or total * 0.30
    current_qty = abs(_to_float(current_qty))
    if current_qty > total * 1.0001:
        return None, 'audit_record_mismatch'
    if current_qty <= total - 0.5 * tp1_qty:
        return True, 'position_reduced'
    return False, 'position_not_reduced'


def get_atr_15m(symbol):
    """ATR(14) on 15m candles (structural reference for anti-truncation). Returns None on failure."""
    try:
        import dynamic_exit_manager as dem
        k15m = dem.get_klines_data(symbol, interval="15m", limit=35)
        highs = [float(k[2]) for k in k15m]
        lows = [float(k[3]) for k in k15m]
        closes = [float(k[4]) for k in k15m]
        atr = dem.calculate_atr(highs, lows, closes, period=14)
        return atr if atr and atr > 0 else None
    except Exception:
        return None


def heal_orphan_position(position, target_env=None, close_on_failure=False, *, planned_sl=None, report_failure=True):
    """
    Places a verified emergency stop on a position that has none. The stop sits ORPHAN_HEAL_SL_DISTANCE away
    from the worse of entry/mark so it can never trigger on placement. planned_sl (the trade's planned SL, passed
    only by close_position_market and heal_unknown_stop) replaces that anchor when it is tighter and not crossed (below mark for LONG,
    above for SHORT); it never loosens the stop. A rejected or unverified planned-SL stop is retried once at the
    anchor (sl_source "anchor_after_planned_rejected"). An explicit Binance rejection (_is_explicit_rejection) skips
    the verification wait. When the anchor is not verified either and the planned placement was not explicitly
    rejected, the planned stop is verified once more (late indexing): success then has sl_source
    "planned_sl_late_indexed" and keeps planned_attempt / anchor_attempt; an anchor placement accepted with an id is
    listed in redundant_stops (reduce-only/closePosition, never cancelled here). If it cannot be verified and
    close_on_failure=True, the position is closed with a reduce-only market order (fail-safe auto-destruct). A hedge-mode
    row (positionSide != BOTH) returns reason "hedge_mode_unsupported" without any order. Never opens or increases
    exposure. A heal-and-close failure files a CRITICAL/P0 report (_report_abort_failure) unless report_failure=False
    (issue #160: the guardian passes it when this cycle's failed crossed close already reported the symbol); the heal
    and the close run either way.
    """
    target_env = resolve_env(target_env)
    sym = position['symbol']
    amt = _to_float(position.get('positionAmt'))
    is_long = amt > 0
    exit_side = 'SELL' if is_long else 'BUY'
    entry_p = _to_float(position.get('entryPrice'))
    mark_p = _to_float(position.get('markPrice')) or entry_p
    out = {"symbol": sym, "success": False, "verified": False, "healed_sl_price": None, "closed": False, "close_result": None}
    if _is_hedge_mode_row(position):
        # Binance rejects every stop and reduce-only close sent without positionSide: no doomed orders, no close.
        out["reason"] = "hedge_mode_unsupported"
        out["error"] = HEDGE_MODE_UNSUPPORTED
        return out

    filters = get_symbol_filters(sym, target_env=target_env)
    if not filters:
        out["reason"] = "filters_unavailable"
    else:
        anchor = min(entry_p, mark_p) if is_long else max(entry_p, mark_p)
        raw_sl = anchor * (1 - ORPHAN_HEAL_SL_DISTANCE) if is_long else anchor * (1 + ORPHAN_HEAL_SL_DISTANCE)
        anchor_sl = round_price(raw_sl, filters['tickSize'], filters['precision_price'])
        attempts = [(anchor_sl, None)]
        planned = _to_float(planned_sl)
        if planned > 0 and is_tighter_stop(planned, raw_sl, is_long):
            planned = round_price(planned, filters['tickSize'], filters['precision_price'])
            if planned > 0 and ((planned < mark_p) if is_long else (planned > mark_p)) \
                    and is_tighter_stop(planned, anchor_sl, is_long):
                # Planned SL first; if it is rejected or unverified (e.g. -2021 against a stale mark), retry once at
                # the anchor. Never looser than the anchor.
                attempts = [(planned, "planned_sl"), (anchor_sl, "anchor_after_planned_rejected")]
        planned_try = None   # (price, algo id, explicitly rejected) of the planned-SL attempt
        for heal_sl, sl_source in attempts:
            if sl_source == "anchor_after_planned_rejected":
                out["planned_attempt"] = {"sl_price": out["healed_sl_price"], "placement": out.get("placement")}
            if sl_source:
                out["sl_source"] = sl_source
            out["healed_sl_price"] = heal_sl
            try:
                out["placement"] = place_algo_stop_loss(sym, exit_side, heal_sl, target_env=target_env)
            except Exception as e:
                out["placement"] = {"error": str(e)}
            placed_id = _order_id(out["placement"]) if isinstance(out["placement"], dict) else None
            rejected = _is_explicit_rejection(out["placement"])
            if sl_source == "planned_sl":
                planned_try = (heal_sl, placed_id, rejected)
            if rejected:
                verified, info = False, None   # explicit rejection: nothing was placed, no indexing wait
            else:
                verified, info = wait_for_stop_confirmation(sym, exit_side, heal_sl, algo_id=placed_id,
                                                            tick_size=filters.get('tickSize'), target_env=target_env)
            out["verified"] = verified
            out["new_stop"] = stop_summary(info) if verified else None
            if verified:
                out["success"] = True
                out["reason"] = "healed"
                return out
        if planned_try is not None and not planned_try[2]:
            # The planned stop may have been accepted but indexed late (the anchor then fails, e.g. -4130).
            planned_price, planned_id, _ = planned_try
            verified, info = wait_for_stop_confirmation(sym, exit_side, planned_price, algo_id=planned_id,
                                                        tick_size=filters.get('tickSize'), target_env=target_env)
            if verified:
                out["anchor_attempt"] = {"sl_price": out["healed_sl_price"], "placement": out.get("placement")}
                anchor_id = _order_id(out.get("placement")) if isinstance(out.get("placement"), dict) else None
                if anchor_id is not None:
                    # The anchor was accepted too (unverified, may exist): listed, never cancelled here.
                    out["redundant_stops"] = [{"algo_id": anchor_id, "trigger_price": out["healed_sl_price"],
                                               "sl_source": "anchor_after_planned_rejected"}]
                out.update(verified=True, success=True, reason="healed", healed_sl_price=planned_price,
                           sl_source="planned_sl_late_indexed", new_stop=stop_summary(info),
                           placement=out["planned_attempt"]["placement"])
                return out
        out["reason"] = "heal_stop_unverified"

    if close_on_failure:
        close_res = emergency_abort_market_close(sym, exit_side, abs(amt), target_env=target_env)
        log_emergency_abort(sym, 'LONG' if is_long else 'SHORT', abs(amt), out.get("healed_sl_price"),
                            out.get("placement") or out.get("reason"), close_res, target_env)
        out["closed"] = bool(close_res.get("confirmed"))
        out["close_result"] = close_res
        out["success"] = out["closed"]
        out["reason"] = "closed_after_failed_heal" if out["closed"] else "heal_and_close_failed"
        if not out["closed"] and report_failure:
            _report_abort_failure(sym, target_env, "heal_orphan_position",
                                  f"heal stop unverified and reduce-only close not confirmed ({close_res})")
    return out


def log_emergency_abort(symbol, direction, qty, sl_p, sl_order, abort_exit, target_env=None):
    target_env = resolve_env(target_env)
    log_dir = os.path.join(_workspace_dir(), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    record = {
        'timestamp': int(time.time()),
        'symbol': symbol,
        'direction': str(direction).upper(),
        'event': 'CRITICAL_FAILSAFE_ABORT',
        'quantity': qty,
        'target_sl_price': sl_p,
        'sl_order_error': sl_order,
        'abort_exit_order': abort_exit,
        'target_env': target_env
    }
    with open(os.path.join(log_dir, 'emergency_aborts.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(record) + "\n")
    with open(os.path.join(log_dir, 'trades_audit.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(record) + "\n")

def emergency_abort_market_close(symbol, exit_side, total_qty, target_env=None):
    """
    Executes atomic failsafe auto-destruct to immediately eliminate unhedged exposure.
    Cancels all open orders and algo orders, then executes a reduce-only MARKET order.
    Verifies response and retries up to 3 times with progressive backoff if not confirmed.
    """
    target_env = resolve_env(target_env)

    # 1. Cancel all open standard orders
    try:
        send_signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol}, target_env=target_env)
    except Exception as e:
        print(f"⚠️ Warning canceling open orders during abort: {e}", file=sys.stderr)

    # 2. Cancel all open algo orders
    try:
        open_algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}, target_env=target_env)
        if isinstance(open_algos, list):
            for ao in open_algos:
                aid = ao.get('algoId') or ao.get('orderId')
                if aid:
                    send_signed_request('DELETE', '/fapi/v1/algoOrder', {'symbol': symbol, 'algoId': aid}, target_env=target_env)
    except Exception as e:
        print(f"⚠️ Warning canceling algo orders during abort: {e}", file=sys.stderr)

    abort_params = {
        'symbol': symbol,
        'side': exit_side,
        'type': 'MARKET',
        'quantity': format_order_qty(total_qty),
        'reduceOnly': 'true'
    }

    last_res = None
    confirmed = False
    max_retries = 3
    backoffs = [0.3, 0.6, 1.0]

    for attempt in range(max_retries):
        try:
            abort_res = send_signed_request('POST', '/fapi/v1/order', abort_params, target_env=target_env)
            last_res = abort_res
            if isinstance(abort_res, dict):
                status = str(abort_res.get('status', '')).upper()
                has_order_id = 'orderId' in abort_res
                has_error = 'code' in abort_res or 'error' in abort_res
                if has_order_id and not has_error and (status in ['FILLED', 'NEW', 'PARTIALLY_FILLED'] or not status):
                    confirmed = True
                    break
        except Exception as e:
            last_res = {"error": str(e)}

        if attempt < max_retries - 1:
            time.sleep(backoffs[attempt])

    if not confirmed:
        err_msg = f"🚨 CRITICAL ALARM: Emergency auto-destruct liquidation FAILED for {symbol} ({exit_side} {total_qty}) after {max_retries} attempts! Response: {last_res}"
        print(err_msg, file=sys.stderr)
        try:
            log_dir = os.path.join(_workspace_dir(), 'logs')
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, 'emergency_aborts.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps({
                    'timestamp': int(time.time()),
                    'symbol': symbol,
                    'event': 'CRITICAL_AUTO_DESTRUCT_FAILED',
                    'response': last_res,
                    'target_env': target_env
                }) + "\n")
        except Exception:
            pass

    return {
        "success": confirmed,
        "confirmed": confirmed,
        "symbol": symbol,
        "side": exit_side,
        "quantity": total_qty,
        "order": last_res,
        "retries": attempt + 1
    }

def get_maint_margin_bracket(symbol, notional, target_env=None):
    """
    Read-only lookup of the maintenance margin ratio (and maintenance amount 'cum') that applies to
    `notional` for `symbol` via GET /fapi/v1/leverageBracket.
    Returns (maint_margin_ratio, maint_amount, source). Falls back to DEFAULT_MAINT_MARGIN_RATIO on any failure.
    """
    try:
        res = send_signed_request('GET', '/fapi/v1/leverageBracket', {'symbol': symbol}, target_env=target_env)
    except Exception:
        res = None
    entries = res if isinstance(res, list) else ([res] if isinstance(res, dict) else [])
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('brackets'), list):
            continue
        if entry.get('symbol') and str(entry.get('symbol')).upper() != str(symbol).upper():
            continue
        brackets = []
        for b in entry['brackets']:
            try:
                brackets.append((float(b.get('notionalFloor', 0)), float(b.get('notionalCap', 0)),
                                 float(b['maintMarginRatio']), float(b.get('cum', 0) or 0)))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        if not brackets:
            continue
        brackets.sort()
        chosen = brackets[-1]
        for floor, cap, mmr, cum in brackets:
            if floor <= notional < cap:
                chosen = (floor, cap, mmr, cum)
                break
        mmr, cum = chosen[2], chosen[3]
        if 0 < mmr < 1:
            return mmr, max(cum, 0.0), 'leverageBracket'
    return DEFAULT_MAINT_MARGIN_RATIO, 0.0, 'fallback'


def estimate_isolated_liquidation_price(direction, entry_price, leverage, maint_margin_ratio=DEFAULT_MAINT_MARGIN_RATIO, maint_amount=0.0, qty=None):
    """
    Binance USD-M isolated liquidation price for a single one-way position:
        LP = (WB + cum - side*Q*EP) / (Q*MMR - side*Q),  WB = Q*EP/L (isolated margin), side = +1 LONG / -1 SHORT
    Per unit of quantity (cum only applies when qty is known):
        LONG : LP = EP*(1 - 1/L) / (1 - MMR)   (minus cum/Q/(1-MMR))
        SHORT: LP = EP*(1 + 1/L) / (1 + MMR)   (plus  cum/Q/(1+MMR))
    """
    entry_price = float(entry_price)
    leverage = float(leverage)
    mmr = float(maint_margin_ratio)
    if entry_price <= 0 or leverage < 1 or not (0 <= mmr < 1):
        raise ValueError(f"Invalid liquidation inputs (entry={entry_price}, leverage={leverage}, mmr={mmr}).")
    side = 1.0 if str(direction).upper() == 'LONG' else -1.0
    cum_per_unit = (float(maint_amount) / float(qty)) if (qty and float(qty) > 0 and maint_amount) else 0.0
    liq = (entry_price / leverage + cum_per_unit - side * entry_price) / (mmr - side)
    return max(liq, 0.0)


def check_liquidation_gate(direction, entry_price, sl_price, leverage, maint_margin_ratio=None, maint_amount=0.0, qty=None, mmr_source=None, safety_fraction=LIQUIDATION_SAFETY_FRACTION):
    """
    Hard gate: the Stop Loss must sit strictly between entry and the estimated isolated liquidation price,
    and its distance from entry must be <= safety_fraction (80%) of the entry->liquidation distance.
    Returns (ok, message_or_None, details).
    """
    is_long = str(direction).upper() == 'LONG'
    if maint_margin_ratio is None:
        maint_margin_ratio, mmr_source = DEFAULT_MAINT_MARGIN_RATIO, 'fallback'
    mmr_source = mmr_source or 'provided'
    try:
        entry = float(entry_price)
        sl = float(sl_price)
        liq = estimate_isolated_liquidation_price(direction, entry, leverage, maint_margin_ratio, maint_amount, qty)
    except (TypeError, ValueError) as e:
        return False, f"MECHANICAL HARD GATE REJECTION (Liquidation Gate): FAIL-CLOSED — cannot estimate liquidation price ({e}).", {}

    liq_dist = (entry - liq) if is_long else (liq - entry)
    sl_dist = (entry - sl) if is_long else (sl - entry)
    max_sl_dist = safety_fraction * liq_dist
    sl_limit = entry - max_sl_dist if is_long else entry + max_sl_dist
    details = {
        'direction': 'LONG' if is_long else 'SHORT',
        'entry_price': entry,
        'sl_price': sl,
        'leverage': leverage,
        'maint_margin_ratio': maint_margin_ratio,
        'maint_margin_source': mmr_source,
        'liquidation_price': liq,
        'liq_distance_pct': (liq_dist / entry * 100.0) if entry else 0.0,
        'sl_distance_pct': (sl_dist / entry * 100.0) if entry else 0.0,
        'max_sl_distance_pct': (max_sl_dist / entry * 100.0) if entry else 0.0,
        'sl_limit_price': sl_limit,
    }
    summary = (
        f"{details['direction']} {leverage}x entry {entry:.6g} -> est. isolated liquidation {liq:.6g} "
        f"({details['liq_distance_pct']:.2f}% away, MMR {maint_margin_ratio*100:.2f}% [{mmr_source}])"
    )
    if sl_dist <= 0:
        return False, (
            f"MECHANICAL HARD GATE REJECTION (Liquidation Gate): Stop Loss {sl:.6g} is on the wrong side of entry "
            f"for a {details['direction']} ({summary}). The SL must sit strictly between entry and liquidation."
        ), details
    if sl_dist > max_sl_dist:
        bound = ">=" if is_long else "<="
        beyond = (sl <= liq) if is_long else (sl >= liq)
        where = "at/beyond the liquidation price" if beyond else f"inside the last {100 - safety_fraction*100:.0f}% buffer before liquidation"
        return False, (
            f"MECHANICAL HARD GATE REJECTION (Liquidation Gate): {summary}. Stop Loss {sl:.6g} is "
            f"{details['sl_distance_pct']:.2f}% from entry, {where}; it must be <= {safety_fraction*100:.0f}% of the "
            f"entry->liquidation distance ({details['max_sl_distance_pct']:.2f}%, i.e. SL {bound} {sl_limit:.6g}). "
            f"Lower the leverage or tighten the stop."
        ), details
    return True, None, details


def profile_risk_fraction(prof):
    """Profile risk_pct_equity as a fraction (default 0.005 = 0.5%): 0.005 -> 0.005; 0.5 -> 0.005; 1.0 -> 0.01.
    Shared by Gate 2 and the Daily Loss Gate (issue #207)."""
    try:
        raw_risk = float((prof or {}).get("risk_pct_equity", 0.005))
    except Exception:
        raw_risk = 0.005
    return raw_risk if raw_risk <= 0.05 else (raw_risk / 100.0)


def monetary_loss_cap(account_equity, prof, *, is_testnet, is_yolo, ref, total_qty, leverage, unrealized=0.0):
    """Gate 2 loss cap (pure; shared by check_mechanical_gates and the protect-pending record check, issue #118).
    TESTNET: max(equity x risk x 1.25, 50); PROD YOLO: max(YOLO_MIN_LOSS_CAP_USDT, margin x
    YOLO_MAX_LOSS_MARGIN_FRACTION) with margin = ref x total_qty / leverage; PROD standard: min(wallet balance,
    balance + unrealized PnL) x risk x 1.25 (issue #119). Returns (max_allowed_loss, risk_fraction, equity_used,
    equity_note)."""
    risk_fraction = profile_risk_fraction(prof)

    equity_note = ""
    # Dynamic risk ceiling = account_equity * (risk_pct_equity / 100) * 1.25 buffer
    if is_testnet:
        max_allowed_loss = max(account_equity * risk_fraction * 1.25, 50.0)
    elif is_yolo:
        # Barbell YOLO Moonshot: strict software loss cap (35% of margin, min $3.75 USDT; utils/gate_limits.py)
        margin_est = (ref * total_qty / max(leverage, 1))
        max_allowed_loss = max(YOLO_MIN_LOSS_CAP_USDT, margin_est * YOLO_MAX_LOSS_MARGIN_FRACTION)
    else:
        # Issue #119: the cap is sized on min(wallet balance, balance + unrealized PnL), so open losses lower it and
        # open gains never raise it. /fapi/v2/balance has no marginBalance and its crossUnPnl excludes isolated
        # positions (the desk mandates isolated margin), so the caller passes the positionRisk uPnL.
        wallet_balance = account_equity
        account_equity = min(wallet_balance, wallet_balance + unrealized)
        equity_note = f" = min(wallet balance ${wallet_balance:.2f}, balance + unrealized PnL ${unrealized:+.2f})"
        max_allowed_loss = account_equity * risk_fraction * 1.25
    return max_allowed_loss, risk_fraction, account_equity, equity_note


def check_mechanical_gates(direction, cur_price, sl_price, tp1_price, total_qty, leverage, bypass_delta_gate=False, target_env=None, bypass_all_gates=False, is_yolo=False,
                           maint_margin_ratio=None, maint_amount=0.0, mmr_source=None, liq_entry_price=None, entry_price=None,
                           live_snapshot=None, exchange_ticks=None):
    """
    Mechanical Software Gates (Deterministic Precondition Validation).
    Verifies mathematical invariants and physically prevents execution if risk rules are violated.
    In TESTNET, free bypass of gates is permitted for testing, experiments, and stress tests.
    In PROD, gates are strict and inviolable.
    `entry_price` is the effective entry of the order actually sent (limit/trigger price for conditional
    entries); the risk, YOLO cap and friction gates are measured from it (falls back to cur_price).
    PROD (issue #101): Gate 0A and Gate 1 are anchored to the exchange via `live_snapshot` (the order attempt's
    fetch_live_gate_snapshot; fetched here when None, a read failure rejects) and take the stricter of
    logs/session_state.json (a cache) and the live view. TESTNET makes no extra exchange call.
    PROD (issue #119): Gate 1's live book = filled positionRisk notional + resting opening orders
    (utils/portfolio_exposure.resting_opening_legs, quantity-less MCP algos sized from logs/pending_entries.json);
    it rejects when that book is heavy in the order's direction and, on a non-empty book, when adding the order
    (total_qty x effective entry) would make it so. Gate 2 (standard orders) caps the loss at
    min(wallet balance, balance + unrealized PnL of the snapshot's open positions) x risk_pct_equity x 1.25; a
    missing unRealizedProfit on an open position rejects. Issue #126: in PROD a defaulted standard margin
    (execute_complete_trade, no explicit --margin) is sized on the same min(wallet, wallet + uPnL), so open losses
    size the order down instead of making Gate 2 reject it; an explicit margin is the user's and is not re-sized.
    Gate 0A and Gate 1 read the registry captured in the snapshot (issue #127). TESTNET skips Gate 1 and keeps the
    10000 fallback. `exchange_ticks` ({symbol: tickSize}, issue #160) caps the registry tick_size used to match
    quantity-less resting orders to their records (record_price_tolerances).
    """
    ref = entry_price if entry_price else cur_price
    target_env = resolve_env(target_env)
    is_testnet = str(target_env).lower() == 'testnet'
    is_long = str(direction).upper() == 'LONG'

    # Gate bypass protection: in PROD, gate bypasses are strictly forbidden
    if not is_testnet and (bypass_all_gates or bypass_delta_gate):
        return False, "MECHANICAL HARD GATE REJECTION: Gate bypass flags are strictly forbidden in PROD."

    if bypass_all_gates:
        return True, None

    try:
        import user_profile as up
    except Exception as e:
        return False, f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — user_profile module unavailable ({e}); cannot resolve the desk leverage ceiling."
    try:
        prof = up.load_user_profile()
    except Exception:
        prof = {}

    # --- GATE 0A: Max Open Positions Gate (open positions + pending resting entries, Issue #38) ---
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
    state_file = os.path.join(log_dir, 'session_state.json')
    if not is_testnet:
        live_snapshot, live_err = _live_snapshot_or_error(live_snapshot, target_env, "MECHANICAL HARD GATE REJECTION")
        if live_err:
            return False, live_err
    slots_ok, slots_err = check_max_open_positions(prof, target_env, live=live_snapshot if not is_testnet else None)
    if not slots_ok:
        return False, slots_err

    # --- GATE 0B: Leverage Ceiling Gate (Absolute Ceiling) ---
    # Single source of truth: user_profile.get_leverage_ceiling() (profile `leverage_ceiling`, default 15x)
    leverage_ceiling = up.get_leverage_ceiling(prof)
    if leverage > leverage_ceiling:
        return False, f"MECHANICAL HARD GATE REJECTION: Leverage {leverage}x exceeds absolute desk ceiling of {leverage_ceiling}x."

    if leverage < 1:
        return False, f"MECHANICAL HARD GATE REJECTION: Invalid leverage {leverage}x. Must be >= 1x."

    # --- GATE 0C: YOLO Slot Enabled Gate ---
    if is_yolo:
        if not prof.get("yolo_slot_enabled", False):
            return False, "MECHANICAL HARD GATE REJECTION: YOLO moonshot slot is disabled in user profile."
        try:
            yolo_cap = int(prof.get("leverage_yolo", leverage_ceiling))
        except (TypeError, ValueError):
            yolo_cap = leverage_ceiling
        yolo_cap = min(max(yolo_cap, 1), leverage_ceiling)
        if leverage > yolo_cap:
            return False, f"MECHANICAL HARD GATE REJECTION: Leverage {leverage}x exceeds the YOLO leverage limit ({yolo_cap}x, profile leverage_yolo)."

    # --- GATE 0D: Liquidation Gate (computed with the leverage that will actually apply) ---
    liq_ok, liq_err, _ = check_liquidation_gate(
        direction,
        liq_entry_price or entry_price or cur_price,
        sl_price,
        leverage,
        maint_margin_ratio=maint_margin_ratio,
        maint_amount=maint_amount,
        qty=total_qty,
        mmr_source=mmr_source,
    )
    if not liq_ok:
        return False, liq_err

    # Standard leverage limit: if not marked as YOLO, cap leverage dynamically at user profile leverage_standard
    if not is_yolo:
        std_cap = int(prof.get("leverage_standard", 3))
        if leverage > std_cap:
            return False, f"MECHANICAL HARD GATE REJECTION: Leverage {leverage}x exceeds standard limit ({std_cap}x). Set --is-yolo for leverage > {std_cap}x."

    # --- GATE 1: Delta-Neutral Gate (Finding 6: Fail-Closed & Staleness Check) ---
    if not bypass_delta_gate and not is_testnet:
        if not os.path.exists(state_file):
            return False, "MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — session_state.json does not exist. Cannot verify portfolio delta in PROD. Order blocked."
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                state_data = json.load(f)
        except Exception as e:
            return False, f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — session_state.json is corrupt ({e}). Order blocked."

        if not isinstance(state_data, dict):
            return False, "MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — session_state.json is malformed. Order blocked."

        # Check validity
        if state_data.get("is_valid") is not True or "error" in state_data:
            err_msg = state_data.get("error", "session_state marked invalid")
            return False, f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — session_state.json is INVALID ({err_msg}). Order blocked."

        # Check staleness (5 minutes = 300s limit)
        now_ts = int(time.time())
        last_updated_ts = state_data.get("last_updated_ts", 0)
        try:
            last_updated_ts = int(last_updated_ts)
        except Exception:
            last_updated_ts = 0

        age_seconds = now_ts - last_updated_ts if last_updated_ts > 0 else (now_ts - int(os.path.getmtime(state_file)))
        if last_updated_ts <= 0 or age_seconds > 300:
            return False, f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — session_state.json is STALE ({age_seconds}s > 300s limit in PROD). Re-sync session state before trading."

        file_exposure = state_data.get('portfolio_exposure', {})
        if not isinstance(file_exposure, dict):
            return False, "MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — session_state.json portfolio_exposure is malformed. Order blocked."
        file_bias = file_exposure.get('delta_bias') or state_data.get('portfolio_delta_bias', 'NEUTRAL')
        # Issue #101: the file is a cache; the live classification (same compute_exposure as the sync) is applied too
        # and the stricter of the two wins. Issue #119: the live book is the filled positionRisk notional plus the
        # opening orders resting on the exchange (resting_opening_legs; MCP algos without a quantity take it from
        # their logs/pending_entries.json record).
        live_exp = live_snapshot["exposure"]
        # Issue #127: the registry captured before the snapshot's exchange reads (a fill or cancel in between is a
        # record without a live order: ignored).
        entries, reg_err, _missing = _snapshot_registry(live_snapshot)
        if reg_err:
            return False, (f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — {reg_err}; cannot measure the resting "
                           "opening orders for the delta-neutral gate. Order blocked.")
        records = [r for r in entries.values() if isinstance(r, dict) and r.get('target_env') == target_env]
        resting = [dict(resting_entry_info(source, kind, o), executed_qty=o.get('executedQty'))
                   for source, kind, o in live_resting_opening_orders(live_snapshot)]
        try:
            legs = resting_opening_legs(resting, records,
                                        price_tol_by_symbol=record_price_tolerances(records, exchange_ticks))
        except ValueError as e:
            return False, (f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — {e}. The delta-neutral gate cannot "
                           "measure the portfolio. Order blocked.")
        rest_long = sum(l['notional'] for l in legs if l['side'] == 'LONG')
        rest_short = sum(l['notional'] for l in legs if l['side'] == 'SHORT')
        pre = book_exposure(live_exp['long_notional'] + rest_long, live_exp['short_notional'] + rest_short)
        blocked = LONG_HEAVY if is_long else SHORT_HEAVY
        live_name = "live exchange positionRisk + resting opening orders" if legs else "live exchange positionRisk"
        sources = [name for name, bias in ((live_name, pre['delta_bias']), ("session_state.json", file_bias))
                   if bias == blocked]
        book_note = (f"live delta_ratio {pre['delta_ratio']:+.2f}, long {pre['long_notional']:.2f} / short "
                     f"{pre['short_notional']:.2f} USDT incl. resting long {rest_long:.2f} / short {rest_short:.2f}")
        if sources:
            source_note = f" Source: {' and '.join(sources)} ({book_note}; session_state.json bias {file_bias})."
            if is_long:
                return False, ("MECHANICAL HARD GATE REJECTION: Portfolio is in LONG_HEAVY state (+Delta imbalanced). Opening additional Longs is strictly prohibited. Short hedge or neutral portfolio required."
                               + source_note)
            return False, ("MECHANICAL HARD GATE REJECTION: Portfolio is in SHORT_HEAVY state (-Delta imbalanced). Opening additional Shorts is strictly prohibited. Long hedge or neutral portfolio required."
                           + source_note)
        # Issue #119 new-order rule: on a non-empty book, the order itself may not tip the book heavy in its own
        # direction (post = pre + total_qty x effective entry on its side). An empty book always passes.
        if pre['long_notional'] + pre['short_notional'] > 0:
            order_notional = abs(total_qty * ref)
            post = project_order(pre, is_long, order_notional)
            if post['delta_bias'] == blocked:
                side = "LONG" if is_long else "SHORT"
                return False, (f"MECHANICAL HARD GATE REJECTION: This {side} order ({order_notional:.2f} USDT notional) "
                               f"would push the portfolio into {blocked} state (post-order delta_ratio "
                               f"{post['delta_ratio']:+.2f} beyond ±0.35; pre-order {book_note}). Reduce the size or "
                               "hedge first.")

    # --- GATE 2: Dynamic Equity Risk Gate (Finding 13) ---
    potential_dollar_loss = abs(ref - sl_price) * total_qty
    try:
        import quant_risk_engine as qre
        account_equity = qre.get_account_equity(target_env)
    except Exception as e:
        if is_testnet:
            account_equity = 10000.0  # Safe sandbox fallback
        else:
            return False, f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — Cannot verify account equity for PROD ({e}). Order blocked."

    unrealized = 0.0
    if not is_testnet and not is_yolo:
        # Issue #119: the uPnL comes from the live snapshot's positionRisk rows (KEYS /fapi/v2/positionRisk, MCP
        # positionInformationV2: unRealizedProfit); no extra request. A missing or unparseable unRealizedProfit on an
        # open position rejects (fail closed).
        try:
            unrealized = unrealized_pnl_total(live_snapshot["exposure"])
        except (ValueError, KeyError, TypeError) as e:
            return False, (f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — cannot read the unrealized PnL of the "
                           f"open positions for the monetary risk cap ({e}). Order blocked.")
    max_allowed_loss, risk_fraction, account_equity, equity_note = monetary_loss_cap(
        account_equity, prof, is_testnet=is_testnet, is_yolo=is_yolo, ref=ref, total_qty=total_qty,
        leverage=leverage, unrealized=unrealized)

    if potential_dollar_loss > max_allowed_loss:
        return False, f"MECHANICAL HARD GATE REJECTION: Monetary risk exceeds allowed cap (${potential_dollar_loss:.2f} > ${max_allowed_loss:.2f} USDT, entry ref {ref}, equity: ${account_equity:.2f}{equity_note}, risk fraction: {risk_fraction*100:.2f}% + buffer). Adjust margin or position size."

    # --- GATE 3: Financial Friction and Fee Gate ---
    if tp1_price and not is_testnet:
        # Signed distance: a TP1 on the wrong side of the effective entry is negative and rejected
        profit_pct_tp1 = ((tp1_price - ref) / ref) if is_long else ((ref - tp1_price) / ref)
        if profit_pct_tp1 < MIN_TP1_DISTANCE:
            return False, f"MECHANICAL HARD GATE REJECTION: Distance to TP1 ({profit_pct_tp1*100:.2f}%) below {MIN_TP1_DISTANCE*100:.2f}% friction floor or on the wrong side of entry (entry ref {ref}). Taker commissions erode statistical edge."

    return True, None

def _dossier_candidate_is_yolo(cand):
    """Same notion as pre_trade_guard._candidate_is_yolo: is_yolo truthy, or 'yolo' in the tier/strategy label."""
    if not isinstance(cand, dict):
        return False
    return (_truthy(cand.get('is_yolo')) or 'yolo' in str(cand.get('tier', '')).lower()
            or 'yolo' in str(cand.get('strategy', '')).lower())

def enforce_evaluation_dossier(symbol, direction, target_env=None, bypass_eval_gate=False, confirmed=False, base_dir=None,
                               is_yolo=False):
    """
    Clean-room evaluation gate for NEW positions (never used by close/breakeven/trailing/audit/heal/cancel paths).
    Delegates to utils.dossier_provenance.validate_dossier_for_trade (same gate as the PreToolUse hook).
    - PROD: fail closed. --bypass-eval-gate is refused. If the evaluator flagged the candidate as requiring
      user confirmation, `confirmed=True` is required. YOLO entries (dossier candidate flagged YOLO, or the order
      itself sent as YOLO) always require `confirmed=True`, even if `requires_user_confirmation` is false/missing.
    - TESTNET: same validation (provenance/direction only when present); explicit bypass_eval_gate is allowed.
    Returns (ok, reason, candidate).
    """
    try:
        env = resolve_env(target_env)
    except Exception as e:
        return False, f"MECHANICAL HARD GATE REJECTION (Evaluation Gate): FAIL-CLOSED — environment resolution failed ({e}).", None
    is_prod = env == 'prod'
    label = env.upper()

    if bypass_eval_gate:
        if is_prod:
            return False, (
                "MECHANICAL HARD GATE REJECTION (Evaluation Gate): --bypass-eval-gate is refused in PROD. "
                "Orders require an APPROVED dossier from 'isolated_market_evaluator' recorded with "
                "`record_evaluation.py --from-subagent <conversationId>`."
            ), None
        return True, "Evaluation gate explicitly bypassed (TESTNET only).", None

    if validate_dossier_for_trade is None:
        return False, "MECHANICAL HARD GATE REJECTION (Evaluation Gate): FAIL-CLOSED — dossier validator (utils/dossier_provenance.py) unavailable.", None

    try:
        ok, reason, cand = validate_dossier_for_trade(
            symbol, direction, env, base_dir=base_dir or find_workspace_root()
        )
    except Exception as e:
        return False, f"MECHANICAL HARD GATE REJECTION (Evaluation Gate, {label}): FAIL-CLOSED — dossier validation error ({e}).", None

    if not ok:
        hint = "" if is_prod else " (TESTNET: pass --bypass-eval-gate to skip explicitly)"
        return False, f"MECHANICAL HARD GATE REJECTION (Evaluation Gate, {label}): {reason}{hint}", None

    if is_prod and isinstance(cand, dict):
        # Same truthiness as the PreToolUse hook (True / 'true' / '1' / 'yes'), issue #63
        if _truthy(cand.get('requires_user_confirmation')) and not confirmed:
            return False, (
                f"MECHANICAL HARD GATE REJECTION (Evaluation Gate, {label}): the evaluator approved {str(symbol).upper()} "
                "pending explicit user confirmation. Re-run with confirmed=True / --confirmed after the user confirms."
            ), cand
        # Barbell YOLO entries are never fast-tracked, whatever the dossier says (issue #52).
        if (_dossier_candidate_is_yolo(cand) or _truthy(is_yolo)) and not confirmed:
            return False, (
                f"MECHANICAL HARD GATE REJECTION (Evaluation Gate, {label}): {str(symbol).upper()} is a YOLO entry; "
                "YOLO entries are never fast-tracked and always require explicit user confirmation. "
                "Re-run with confirmed=True / --confirmed after the user confirms."
            ), cand
        # Issue #202: an uncalibrated Tier S score bucket asks the user like Tier A+/A (never a rejection with
        # --confirmed). Same helper and message as the PreToolUse hook; read/parse problems mean uncalibrated.
        if not confirmed:
            calib_msg = _tier_s_calibration_message(cand, env, base_dir or find_workspace_root())
            if calib_msg:
                return False, (f"MECHANICAL HARD GATE REJECTION (Evaluation Gate, {label}): {str(symbol).upper()}: "
                               f"{calib_msg}"), cand

    return True, reason, cand


def _tier_s_calibration_message(cand, env, base_dir):
    """utils.score_calibration.tier_s_confirmation_required with the profile; fails closed (asks the user)."""
    try:
        from utils import score_calibration as scal
    except Exception:
        if _tier_label_of(cand) == "S":
            return TIER_S_FALLBACK_MESSAGE  # issue #207: one text shared with the guard
        return squeeze_fallback_message(cand, base_dir)  # issue #207: the squeeze backstop still asks
    try:
        import user_profile as up
        prof = up.load_user_profile(base_dir=base_dir)
    except Exception:
        prof = {}  # missing keys fall back to the defaults (gate on)
    try:
        return scal.tier_s_confirmation_required(cand, env, prof, base_dir)
    except Exception as e:  # fail closed: ask the user
        return scal.confirmation_reason((cand or {}).get('score'), f"calibration check failed ({type(e).__name__})")


def _tier_label_of(cand):
    tokens = str((cand or {}).get('tier') or '').upper().replace('TIER', ' ').split()
    return tokens[0] if tokens else None


# Issue #207: the fallback texts live in utils/calibration_fallback.py, shared with pre_trade_guard (score_calibration
# may be the missing module)
_GENERIC_FALLBACK_ASK = ("squeeze_risk SHORT / Tier S: user confirmation required (calibration modules unavailable): "
                         "ask the user and rerun with --confirmed.")
try:  # a broken texts module may only make the desk ask more, never stop the close / heal paths from loading
    from utils.calibration_fallback import TIER_S_FALLBACK_MESSAGE, SQUEEZE_FALLBACK_MESSAGE  # noqa: E402
except Exception:  # pragma: no cover - still ask the user, with a generic text
    TIER_S_FALLBACK_MESSAGE = SQUEEZE_FALLBACK_MESSAGE = _GENERIC_FALLBACK_ASK
# An emptied text would turn the ask off (callers test `if calib_msg:`): never empty
TIER_S_FALLBACK_MESSAGE = TIER_S_FALLBACK_MESSAGE or _GENERIC_FALLBACK_ASK
SQUEEZE_FALLBACK_MESSAGE = SQUEEZE_FALLBACK_MESSAGE or _GENERIC_FALLBACK_ASK


def squeeze_fallback_message(cand, base_dir):
    """Squeeze backstop when utils.score_calibration cannot be imported (issue #207): SQUEEZE_FALLBACK_MESSAGE when
    the stored dossier record's radar snapshot for the candidate has squeeze_risk true, or (a SHORT) is missing or not
    bound to the dossier sha256, or the record cannot be read (fail closed: ask); else None."""
    cand = cand if isinstance(cand, dict) else {}
    try:
        with open(os.path.join(base_dir, 'logs', 'evaluations', 'latest_dossier.json'), 'r', encoding='utf-8') as f:
            record = json.load(f)
        key = f"{str(cand.get('symbol') or '').upper()}|{str(cand.get('direction') or '').upper()}"
        entry = (record.get('radar_snapshots') or {}).get(key)
        row = entry.get('radar_snapshot') if isinstance(entry, dict) else None
        bound = bool(cand.get('dossier_sha256')) and (record.get('provenance') or {}).get('sha256') == \
            cand.get('dossier_sha256')
    except Exception:
        return SQUEEZE_FALLBACK_MESSAGE
    if isinstance(row, dict) and row.get('squeeze_risk') is True:
        return SQUEEZE_FALLBACK_MESSAGE
    if str(cand.get('direction') or '').upper() == 'SHORT' and not (isinstance(row, dict) and bound):
        return SQUEEZE_FALLBACK_MESSAGE
    return None


def check_daily_loss_gate(target_env, is_yolo_order, prof, equity_now, *, open_positions=None, now=None):
    """
    Daily Loss Gate (issue #207) for OPENING orders only: called from execute_complete_trade, never from the close /
    break-even / heal / protect / audit / positions paths. Returns (ok, reason, state).
    - TESTNET: skipped (info line), ok.
    - PROD: today's (UTC; sync_session_state.get_start_of_day_utc(now)) fills from GET /fapi/v1/userTrades
      (sync_session_state.fetch_day_fills) and the audit records give the per-trade list
      (sync_session_state.day_trade_view / trade_outcomes.closed_trades_today), judged by
      utils.daily_loss_gate.evaluate with the profile limits (user_profile.get_daily_loss_limits), the profile risk
      and equity_now (the live wallet balance). open_positions: (SYMBOL, DIRECTION) pairs open now (the live
      snapshot; read from positionRisk when None). Fail closed: MCP mode (the gateway does not serve userTrades), an
      unreadable or truncated fill read, an unreadable audit, trades not countable per trade (counted_by != "trades")
      or any exception refuses the opening. state: the evaluate() dict plus "is_yolo_order" (audit field).
    """
    is_yolo_order = bool(is_yolo_order)
    try:
        env = resolve_env(target_env)
    except Exception as e:
        env = None
        env_error = e
    if env == 'testnet':
        print("ℹ️ Daily Loss Gate skipped (TESTNET).", file=sys.stderr)  # stderr: stdout carries the JSON result
        return True, "Daily Loss Gate skipped (TESTNET).", {"skipped": "testnet", "is_yolo_order": is_yolo_order}

    def refuse(reason):
        return False, reason, {"blocked": True, "scope": "all", "reason": reason, "is_yolo_order": is_yolo_order}

    def unreadable(detail):
        return refuse(f"DAILY LOSS GATE: today's fills unreadable ({str(detail)[:200]}) — opening refused (fail closed)")

    if env is None:
        return unreadable(f"environment resolution failed: {env_error}")
    try:
        if uses_mcp_gateway(env):
            return unreadable("MCP mode: the Binance agentic gateway does not serve /fapi/v1/userTrades")
        import sync_session_state as sss
        import user_profile as up
        from utils import daily_loss_gate as dlg
        start_ms = sss.get_start_of_day_utc(now)
        fills, truncated = sss.fetch_day_fills(start_ms, env)
        if not isinstance(fills, list):
            return unreadable(fills)
        if truncated:
            return unreadable(f"truncated after {sss.DAY_FILLS_MAX_PAGES} pages of {sss.DAY_FILLS_LIMIT}, or a later "
                              "page failed")
        audit_path = os.path.join(_workspace_dir(), 'logs', 'trades_audit.jsonl')
        try:
            records = read_audit_tail(audit_path) if os.path.exists(audit_path) else []
        except OSError as e:
            return unreadable(f"trades audit unreadable: {e}")
        if open_positions is None:
            pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', target_env=env)
            if not isinstance(pos_res, list):
                return unreadable(f"/fapi/v2/positionRisk query failed: {pos_res}")
            open_positions = [(p["symbol"], p["side"]) for p in compute_exposure(pos_res)["active_positions"]]
        view = sss.day_trade_view(records, fills, start_ms, env, open_positions)
        if view.get("counted_by") != "trades":
            return refuse(f"DAILY LOSS GATE: today's trades cannot be counted per trade ({view.get('day_error')}): "
                          "the consecutive-SL streak is unverifiable — opening refused (fail closed)")
        net, other_asset = dlg.day_net_realized(fills)
        state = dlg.evaluate(net, view.get("trades") or [], risk_pct=profile_risk_fraction(prof),
                             equity_now=equity_now, is_yolo_order=is_yolo_order, **up.get_daily_loss_limits(prof))
        state["is_yolo_order"] = is_yolo_order
        dlg.note_unaudited_closing_symbols(state, view.get("unaudited_closing_symbols") or [])  # informational
        if other_asset:
            state["non_usdt_commission"] = True  # left out of the USDT sum; those trades have gross R only
        if state.get("blocked"):
            return False, state.get("reason"), state
        return True, None, state
    except Exception as e:
        return unreadable(f"{type(e).__name__}: {e}")


# Issue #202: score metadata on the entry audit record (audit only; never a gate input). Never named `provenance`
# (stamp_trade_record owns that key).
SCORE_AUDIT_KEYS = ('score', 'score_tier', 'score_components', 'score_source', 'score_missing_reason',
                    'dossier_tier', 'dossier_score', 'dossier_sha256', 'score_schema_version')


def score_audit_fields(meta):
    """The SCORE_AUDIT_KEYS of `meta`, None for each missing one (e.g. a pending record registered before #202)."""
    meta = meta if isinstance(meta, dict) else {}
    return {k: meta.get(k) for k in SCORE_AUDIT_KEYS}


def read_radar_snapshot(symbol, direction, dossier_sha256=None):
    """(row, None) or (None, reason): the radar row record_evaluation.py joined into latest_dossier.json
    (`radar_snapshots["SYMBOL|DIRECTION"]`). Fails open: any problem is a reason, never an exception."""
    try:
        path = os.path.join(_workspace_dir(), 'logs', 'evaluations', 'latest_dossier.json')
        if not os.path.exists(path):
            return None, 'missing'
        with open(path, 'r', encoding='utf-8') as f:
            record = json.load(f)
        prov = record.get('provenance') if isinstance(record.get('provenance'), dict) else {}
        if dossier_sha256 and prov.get('sha256') != dossier_sha256:
            return None, 'dossier_changed'
        snaps = record.get('radar_snapshots')
        if not isinstance(snaps, dict):
            return None, 'missing'
        entry = snaps.get(f"{str(symbol).upper()}|{str(direction).upper()}")
        if not isinstance(entry, dict):
            return None, 'no_match'
        row = entry.get('radar_snapshot')
        if not isinstance(row, dict):
            return None, str(entry.get('radar_snapshot_reason') or 'no_match')
        return row, None
    except Exception:
        return None, 'unreadable'


def build_score_meta(cand, symbol, direction):
    """SCORE_AUDIT_KEYS for the entry audit record from the dossier candidate (None when the gate returned none)
    and its radar snapshot. Fails open to nulls plus score_missing_reason."""
    meta = dict.fromkeys(SCORE_AUDIT_KEYS)
    if not isinstance(cand, dict):
        return meta
    try:
        meta.update(dossier_tier=cand.get('tier'), dossier_score=cand.get('score'),
                    dossier_sha256=cand.get('dossier_sha256'))
        row, reason = read_radar_snapshot(symbol, direction, meta['dossier_sha256'])
        if row is None:
            meta['score_missing_reason'] = reason
        else:
            score = row.get('confidence')
            meta.update(score=int(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
                        score_tier=row.get('tier'), score_components=row.get('score_components'),
                        score_source='radar_snapshot',
                        score_schema_version=(row.get('score_schema_version')
                                              if isinstance(row.get('score_schema_version'), int)
                                              and not isinstance(row.get('score_schema_version'), bool) else None))
    except Exception as e:
        meta['score_missing_reason'] = f"error ({type(e).__name__})"
    return meta


# -----------------------------------------------------------------------------
# Take-profit sizing / placement and audit ledger (shared by fresh entries and filled resting entries)
# -----------------------------------------------------------------------------
def split_take_profit_quantities(total_qty, filters, ref_price):
    """
    Asymmetric 30% TP1 / 70% TP2 split (positive right-tail skewness, no premature truncation). TP1 is bumped
    to the exchange minNotional at ref_price when possible; falls back to 50/50 when 30/70 cannot respect minQty.
    Returns (tp1_qty, tp2_qty).
    """
    min_notional = filters.get('minNotional', 5.0)
    tp1_qty = round_step(total_qty * 0.30, filters['stepSize'], filters['precision_qty'])
    if tp1_qty < filters['minQty']:
        tp1_qty = filters['minQty']
    if tp1_qty * ref_price < min_notional:
        needed_qty = round_step(math.ceil(min_notional / ref_price / filters['stepSize']) * filters['stepSize'], filters['stepSize'], filters['precision_qty'])
        if needed_qty < total_qty:
            tp1_qty = needed_qty

    tp2_qty = round_step(total_qty - tp1_qty, filters['stepSize'], filters['precision_qty'])
    if tp2_qty < filters['minQty']:
        # Fallback to 50/50 if position size is too small to split 30/70 while respecting minQty
        tp1_qty = round_step(total_qty / 2, filters['stepSize'], filters['precision_qty'])
        tp2_qty = round_step(total_qty - tp1_qty, filters['stepSize'], filters['precision_qty'])
    return tp1_qty, tp2_qty


def place_take_profit_orders(symbol, exit_side, tp1_price, tp2_price, tp1_qty, tp2_qty, target_env=None):
    """TP1 / TP2 as reduce-only GTC LIMIT orders on the exit side. Returns (tp1_order, tp2_order)."""
    placed = []
    for price, qty in ((tp1_price, tp1_qty), (tp2_price, tp2_qty)):
        order = None
        if qty > 0:
            tp_params = {
                'symbol': symbol,
                'side': exit_side,
                'type': 'LIMIT',
                'price': price,
                'quantity': qty,
                'timeInForce': 'GTC',
                'reduceOnly': 'true'
            }
            order = send_signed_request('POST', '/fapi/v1/order', tp_params, target_env=target_env)
        placed.append(order)
    return placed[0], placed[1]


def find_resting_take_profits(symbol, exit_side, wanted, tick_size=None, exclude_ids=(), target_env=None):
    """Issue #160 (read-only): reduce-only exit-side LIMIT orders already resting at the TP prices, so a record whose
    TP ids were not persisted adopts them instead of placing duplicates. wanted: {name: price}; an order matches when
    its price is within one tick (exact to 1e-9 relative without a tick); each order is adopted at most once and
    exclude_ids are skipped. Returns ({name: orderId}, error_or_None); a failed GET /fapi/v1/openOrders?symbol= is
    ({}, error)."""
    try:
        res = send_signed_request('GET', '/fapi/v1/openOrders', {'symbol': symbol}, target_env=target_env)
    except Exception as e:
        res = {"error": str(e)}
    if not isinstance(res, list):
        return {}, f"/fapi/v1/openOrders query for {symbol} failed: {res}"
    used = {str(i) for i in exclude_ids or ()}
    found = {}
    for name, price in (wanted or {}).items():
        price = _to_float(price)
        if price <= 0:
            continue
        tol = _to_float(tick_size) * 1.01 if _to_float(tick_size) > 0 else abs(price) * 1e-9
        for o in res:
            if not isinstance(o, dict) or str(_order_id(o)) in used or _order_id(o) is None:
                continue
            if (str(o.get('symbol') or symbol).upper() == str(symbol).upper()
                    and str(o.get('type') or '').upper() == 'LIMIT'
                    and str(o.get('side') or '').upper() == str(exit_side).upper()
                    and _truthy(o.get('reduceOnly')) and abs(_to_float(o.get('price')) - price) <= tol):
                found[name] = _order_id(o)
                used.add(str(_order_id(o)))
                break
    return found, None


def append_trade_audit_record(record, margin_usdt):
    """Appends an entry record to logs/trades_audit.jsonl with canonical provenance (atomic append)."""
    log_dir = os.path.join(_workspace_dir(), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    audit_file = os.path.join(log_dir, 'trades_audit.jsonl')
    try:
        from provenance_stamp import stamp_trade_record
        from utils.atomic_writer import atomic_append_jsonl
        record = stamp_trade_record(
            record,
            evaluator="isolated_market_evaluator",
            strategy="microstructure_wick_reversion",
            sizing_model=f"volatility_parity_margin_{margin_usdt}"
        )
        atomic_append_jsonl(audit_file, record)
    except Exception:
        with open(audit_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + "\n")
    return record


# -----------------------------------------------------------------------------
# Resting entries (Issue #33): untriggered conditional STOP_MARKET and resting LIMIT entries.
# Binance cannot attach a Stop Loss to a conditional/resting order, so each one is recorded in
# logs/pending_entries.json and protect_pending_entries() (--protect-pending, run by the position
# guardian at the start of every cycle) places the planned SL/TPs once it fills.
# Issue #36: on KEYS (HMAC) the planned SL is also pre-armed at placement as a closePosition STOP_MARKET when it is
# not crossed (prearm_resting_entry_stop); the guardian then verifies it at fill and stays the fallback.
# Schema v2 record fields: prearm_status ("placed" | "rejected:<code-or-text>" | "skipped:mcp" |
# "skipped:crossed"), prearm_algo_id / prearm_price (an algo id was returned), sl_close_position: true and
# sl_qty: null (verified pre-arm, covers any size). v1 records have none of them: not pre-armed.
# Issue #156 (optional, no version bump): gate2_loss_cap_usdt (PROD Gate 2 cap at placement; bounds the drift
# tolerance of the filled-record loss-cap re-check) and check_deferrals (consecutive runs with a deferred check).
# -----------------------------------------------------------------------------
PENDING_ENTRY_TIMEOUT_SECONDS = 5400    # desk order timeout (60-90 min) for unfilled resting entries
GUARDIAN_MAX_INTERVAL_FOR_RESTING = 120 # resting entries require a guardian LOOP (--interval <= 120s) for the env
PENDING_MISSING_GRACE_SECONDS = 60      # "entry gone, no position" must persist this long before a record is dropped
PENDING_ENTRIES_SCHEMA_VERSION = 2
FILL_MIN_RR_TP2 = 3.0                   # issue #39: audit flag "rr_below_3" for a fill whose real R:R to TP2 is lower
PENDING_DEFERRAL_REPORT_AFTER = 3       # issue #156: consecutive deferred record checks before a HIGH issue is filed


def pending_entries_path(base_dir=None):
    return os.path.join(base_dir or _workspace_dir(), 'logs', 'pending_entries.json')


def pending_entry_key(target_env, symbol, entry_id):
    return f"{target_env}:{str(symbol).upper()}:{entry_id}"


def load_pending_entries_status(base_dir=None):
    """Returns (entries, error, missing). missing is True only when logs/pending_entries.json does not exist (then
    entries == {} and error is None), so callers can tell a deleted registry from an empty one (issue #101)."""
    path = pending_entries_path(base_dir)
    if not os.path.exists(path):
        return {}, None, True
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {}, f"pending entries registry unreadable ({e})", False
    if not isinstance(data, dict) or not isinstance(data.get('entries'), dict):
        return {}, "pending entries registry malformed", False
    return data['entries'], None, False


def load_pending_entries(base_dir=None):
    """Returns (entries, error). A missing registry reads as empty here (protect_pending_entries and the guardian
    must never block on it); an unreadable or malformed one is an error (callers fail closed: no new resting entry is
    accepted and no record is ever dropped). Use load_pending_entries_status to tell missing from empty. The
    registry is never trusted alone in PROD: the order gates (check_max_open_positions, find_unregistered_resting_
    entries) cross-check it against the opening orders resting on the exchange, and a missing registry with any such
    order rests rejects every new entry."""
    entries, err, _missing = load_pending_entries_status(base_dir)
    return entries, err


PENDING_REGISTRY_LOCK_WAIT_S = 5.0
PENDING_SAVE_RETRY_DELAYS_S = (0.5, 1.0)   # issue #160: 3 attempts of the TP ids / audit_done saves
_registry_lock_state = threading.local()


class PendingRegistryLockError(RuntimeError):
    """The registry lock (logs/pending_entries.json.lock) was not acquired, or update_pending_entries was re-entered
    while this thread holds it (issue #40). The registry is never written unlocked."""


def update_pending_entries(mutate, base_dir=None):
    """Read-modify-write of logs/pending_entries.json: re-reads the registry, applies mutate(entries) and writes
    it atomically. Raises on an unreadable registry or a failed write.
    Issue #40: the whole read-modify-write holds an exclusive lock on logs/pending_entries.json.lock
    (utils.file_lock.locked, wait PENDING_REGISTRY_LOCK_WAIT_S), so concurrent writers (registration, the guardian's
    --protect-pending) never lose a record. Not acquired -> PendingRegistryLockError and nothing is written
    (registration then cancels the entry; protect keeps the record for the next run). A nested call from inside the
    lock (e.g. from `mutate`) would wait on its own flock: it raises PendingRegistryLockError at once instead."""
    if getattr(_registry_lock_state, 'held', False):
        raise PendingRegistryLockError("update_pending_entries re-entered while the registry lock is held "
                                       "(nested registry update)")
    from utils.file_lock import locked
    path = pending_entries_path(base_dir)
    with locked(path, wait_s=PENDING_REGISTRY_LOCK_WAIT_S) as held:
        if not held:
            raise PendingRegistryLockError(f"registry lock {os.path.basename(path)}.lock not acquired within "
                                           f"{PENDING_REGISTRY_LOCK_WAIT_S:.0f}s; registry not written")
        _registry_lock_state.held = True
        try:
            entries, err = load_pending_entries(base_dir)
            if err:
                raise IOError(err)
            mutate(entries)
            from utils.atomic_writer import atomic_write_json
            atomic_write_json(path, {"schema_version": PENDING_ENTRIES_SCHEMA_VERSION, "entries": entries})
        finally:
            _registry_lock_state.held = False
    return entries


def register_resting_entry(kind, entry_id, symbol, direction, entry_side, exit_side, target_env, price, total_qty,
                           sl_price, tp1_price, tp2_price, leverage, is_yolo, margin_usdt, prearm=None,
                           tick_size=None, step_size=None, gate2_loss_cap_usdt=None, *, score_meta=None,
                           daily_loss_gate=None):
    """Records a resting entry in logs/pending_entries.json (schema v2; `prearm`: the prearm_resting_entry_stop
    fields; tick_size / step_size: the symbol filters used for rounding, stored when given for the half-tick
    registry match of Gate 1 and the total_qty check, issue #126; gate2_loss_cap_usdt: the Gate 2 loss cap at
    placement, stored when positive, bounds the equity-drift tolerance of the protect-pending re-check, issue #156;
    score_meta: the SCORE_AUDIT_KEYS copied into the fill's audit record, issue #202; daily_loss_gate: the Daily
    Loss Gate state at placement, copied into the fill's audit record, issue #207).
    Returns (key, record); raises on failure."""
    now = int(time.time())
    key = pending_entry_key(target_env, symbol, entry_id)
    record = {
        'kind': kind,
        'entry_id': str(entry_id),
        'symbol': str(symbol).upper(),
        'direction': str(direction).upper(),
        'entry_side': entry_side,
        'exit_side': exit_side,
        'target_env': target_env,
        'trigger_or_limit_price': price,
        'total_qty': total_qty,
        'sl_price': sl_price,
        'tp1_price': tp1_price,
        'tp2_price': tp2_price,
        'leverage': leverage,
        'is_yolo': bool(is_yolo),
        'margin_usdt': margin_usdt,
        'placed_at_ts': now,
        'expires_at_ts': now + PENDING_ENTRY_TIMEOUT_SECONDS,
    }
    for name, value in (('tick_size', tick_size), ('step_size', step_size),
                        ('gate2_loss_cap_usdt', gate2_loss_cap_usdt)):
        if _to_float(value) > 0:
            record[name] = _to_float(value)
    record.update(prearm or {})
    if score_meta is not None:
        record['score_meta'] = score_audit_fields(score_meta)
    if daily_loss_gate is not None:
        record['daily_loss_gate'] = daily_loss_gate
    update_pending_entries(lambda entries: entries.__setitem__(key, record))
    return key, record


def _rejection_text(res):
    """Short reason of a rejected order response: the Binance code when present, else the message (<= 120 chars)."""
    if isinstance(res, dict):
        code = res.get('code')
        if code is not None and str(code).lstrip('-').isdigit() and int(code) < 0:
            return str(code)
        text = res.get('msg') or res.get('error') or res.get('message') or res
    else:
        text = res
    return str(text)[:120] or "unknown"


def prearm_resting_entry_stop(symbol, exit_side, sl_price, ref_price, target_env=None, tick_size=None):
    """
    Issue #36: pre-arms the planned Stop Loss of a resting entry right after the entry is placed, as a closePosition
    STOP_MARKET on the exit side (place_algo_stop_loss with quantity=None). A closePosition order can never open a
    position. Binance does not document whether such a stop is accepted with no position or what it does if it
    triggers before the fill; either way the guardian verifies it at fill (_ensure_entry_stop) and places the planned
    stop when it is gone. Pre-armed only when:
      - the orders do not route through the MCP gateway (it rejects a bare closePosition with no position);
      - sl_price is on the protective side of ref_price (the current last price; the stop has no workingType, i.e.
        CONTRACT_PRICE): below it for a SELL stop (LONG), above it for a BUY stop (SHORT).
    Never blocks the entry. Returns the v2 registry fields: {"prearm_status": "placed" | "rejected:<code-or-text>" |
    "skipped:mcp" | "skipped:crossed"}, plus prearm_algo_id / prearm_price (the listed trigger once verified, else
    the requested sl_price) when an algo id was returned (cancelled
    with the entry) and sl_close_position: True / sl_qty: None once verified on /fapi/v1/openAlgoOrders
    (wait_for_stop_confirmation: by algo id, falling back to a trigger match within one tick; only the MARKET-entry
    stop is verified by id alone, issue #157). Never
    raises: an unexpected error is "rejected:<error>" (the entry must still be registered).
    """
    try:
        return _prearm_resting_entry_stop(symbol, exit_side, sl_price, ref_price, target_env, tick_size)
    except Exception as e:
        return {'prearm_status': f"rejected:{type(e).__name__}: {e}"[:130]}


def _prearm_resting_entry_stop(symbol, exit_side, sl_price, ref_price, target_env, tick_size):
    if uses_mcp_gateway(target_env):
        return {'prearm_status': 'skipped:mcp'}
    sl_price, ref_price = _to_float(sl_price), _to_float(ref_price)
    is_long_exit = str(exit_side).upper() == 'SELL'
    if sl_price <= 0 or ref_price <= 0 or ((sl_price >= ref_price) if is_long_exit else (sl_price <= ref_price)):
        return {'prearm_status': 'skipped:crossed'}
    try:
        placement = place_algo_stop_loss(symbol, exit_side, sl_price, target_env=target_env)
    except Exception as e:
        placement = {"error": f"placement exception: {e}"}
    placed_id = _order_id(placement) if isinstance(placement, dict) and not _is_api_error(placement) else None
    if placed_id is None:
        return {'prearm_status': f"rejected:{_rejection_text(placement)}"}
    fields = {'prearm_algo_id': placed_id, 'prearm_price': sl_price}
    verified, info = wait_for_stop_confirmation(symbol, exit_side, sl_price, algo_id=placed_id, tick_size=tick_size,
                                                target_env=target_env)
    if not verified:
        return dict(fields, prearm_status='rejected:unverified')
    # Issue #157: the trigger as listed (tick-rounded by place_algo_stop_loss), else the requested price.
    fields['prearm_price'] = _trigger_price(info) if isinstance(info, dict) and _trigger_price(info) > 0 else sl_price
    return dict(fields, prearm_status='placed', sl_close_position=True, sl_qty=None)


def _prearm_note(prearm, sl_price):
    status = prearm.get('prearm_status')
    if status == 'placed':
        return f"Stop Loss pre-armed at {sl_price} (closePosition, algo {prearm.get('prearm_algo_id')}). "
    if status == 'rejected:unverified':
        return (f"Stop Loss pre-arm may exist (unverified, algo id {prearm.get('prearm_algo_id')}); the guardian "
                "verifies or places it at fill. ")
    return f"Stop Loss not pre-armed ({status}). "


def _prearm_anomaly(prearm):
    """Issue #157: a KEYS pre-arm that was not crossed but was rejected ("rejected:<code-or-text>", except -2021:
    the exchange saying the SL would trigger at once) or not verified ("rejected:unverified"). skipped:* never is.
    Returns {"status", "message"} or None."""
    status = str((prearm or {}).get('prearm_status') or '')
    if not status.startswith('rejected:') or status == 'rejected:-2021':
        return None
    return {"status": status, "message": _prearm_note(prearm, None).strip()}


def _report_prearm_anomaly(symbol, target_env, kind, entry_id, anomaly):
    """MEDIUM issue (issue #157) for a pre-arm anomaly. Never raises (the entry is kept and registered). error_detail
    is stable per symbol + status for the reporter's 24h fingerprint dedup."""
    try:
        import report_agent_issue
        report_agent_issue.report_issue(
            title=f"prearm_resting_entry_stop: pre-armed stop of {symbol} {anomaly['status']}",
            error_detail=f"{symbol} pre-arm {anomaly['status']}",
            category="risk_gate", severity="MEDIUM",
            agent_name="execute_futures_trade.prearm_resting_entry_stop",
            affected_files="scripts/execute_futures_trade.py:_prearm_resting_entry_stop",
            context=(f"env={target_env}; symbol={symbol}; {kind} entry {entry_id}; {anomaly['message']} The entry "
                     "was kept; the position guardian protects it at fill."),
            remediation="Check the algo orders of the symbol on Binance and the pre-arm rejection code.")
    except Exception as e:
        logger.error(f"pre-arm anomaly report for {symbol} could not be filed: {e}")


def cancel_prearmed_stop(symbol, exit_side, algo_id, target_env=None):
    """
    Cancels the pre-armed stop of a resting entry that ended without a position (issue #36), by its algo id only:
    never a sweep of the symbol's stops. It is cancelled only when /fapi/v1/openAlgoOrders lists it for symbol as a
    protective stop on exit_side (a forged id cannot cancel another symbol's stop); not listed = already gone (ok).
    A leftover stop must not stay: a later MARKET entry's closePosition SL on the symbol would get -4130 and, being
    verified by its own algo id only (issue #157), auto-destruct. Returns (ok, detail); a failed read or
    cancel returns ok False (callers keep the record and retry on the next cycle; never blocking).
    """
    if algo_id is None:
        return True, None
    stops, err = get_open_stop_orders(symbol, exit_side, target_env=target_env)
    if err:
        return False, err
    if not any(str(_order_id(s)) == str(algo_id) for s in stops):
        return True, "not_listed"
    try:
        res = send_signed_request('DELETE', '/fapi/v1/algoOrder', {'symbol': symbol, 'algoId': algo_id}, target_env=target_env)
    except Exception as e:
        res = {"error": str(e)}
    if _is_api_error(res):
        return False, res
    return True, res


def _ensure_entry_stop(symbol, exit_side, sl_price, prearm_algo_id=None, quantity=None, tick_size=None,
                       target_env=None):
    """
    One stop rule for a pending or just-filled entry (issue #36), shared by the inline PARTIALLY_FILLED path and
    _protect_pending_entry at fill:
      1. a pre-armed stop (prearm_algo_id) verified on /fapi/v1/openAlgoOrders (by algo id, then by price within one
         tick; one read) is kept: no placement;
      2. otherwise (no pre-arm, or it was consumed, e.g. triggered before the fill) the planned stop is placed as
         before (quantity=None: closePosition) and verified with progressive retries (wait_for_stop_confirmation: by
         the placement's algo id, falling back to a trigger match within one tick; unlike the id-only MARKET path);
      3. a -4130 on that placement means a closePosition stop already exists: re-verified by listing (pre-arm id,
         else the same one-tick price match), "kept"; when that fails, one more error-aware listing
         (get_open_stop_orders_with_retry, issue #157) keeps a listed closePosition stop whose trigger is at sl_price
         within one tick or tighter (on MCP the listing folds reduceOnly into closePosition, so the flag cannot be
         told apart there); a looser or reduceOnly-only stop, an empty or a failed listing is unverified.
    Returns {"verified", "source": "prearm" | "placed" | "kept" | None, "placement", "info"}.
    """
    if prearm_algo_id is not None:
        verified, info = wait_for_stop_confirmation(symbol, exit_side, sl_price, algo_id=prearm_algo_id,
                                                    tick_size=tick_size, target_env=target_env, retry_delays=())
        if verified:
            return {"verified": True, "source": "prearm", "placement": None, "info": info}
    try:
        placement = place_algo_stop_loss(symbol, exit_side, sl_price, target_env=target_env, quantity=quantity)
    except Exception as e:
        placement = {"error": f"placement exception: {e}"}
    if _is_existing_close_position_stop_rejection(placement):
        verified, info = wait_for_stop_confirmation(symbol, exit_side, sl_price, algo_id=prearm_algo_id,
                                                    tick_size=tick_size, target_env=target_env)
        if not verified:
            # Issue #157: wait_for_stop_confirmation cannot tell "every read failed" from "no stop": one error-aware
            # listing more. Kept only: a closePosition stop (the one -4130 refers to; a reduceOnly-only stop may not
            # cover the position) whose trigger is not looser than sl_price beyond one tick. Anything else, an empty
            # listing or another failed read stays unverified (the caller auto-destructs).
            stops, err = get_open_stop_orders_with_retry(symbol, exit_side, target_env=target_env)
            if err is None:
                is_long = str(exit_side).upper() == 'SELL'
                sl = float(sl_price)
                tol = _to_float(tick_size) * 1.01 if _to_float(tick_size) > 0 else abs(sl) * 0.0005
                kept =[s for s in stops if _truthy(s.get('closePosition'))
                        and (abs(_trigger_price(s) - sl) <= tol or is_tighter_stop(_trigger_price(s), sl, is_long))]
                if kept:
                    verified, info = True, tightest_stop(kept, is_long)
        return {"verified": verified, "source": "kept" if verified else None, "placement": placement, "info": info}
    placed_id = _order_id(placement) if isinstance(placement, dict) else None
    verified, info = wait_for_stop_confirmation(symbol, exit_side, sl_price, algo_id=placed_id, tick_size=tick_size,
                                                target_env=target_env)
    return {"verified": verified, "source": "placed" if verified else None, "placement": placement, "info": info}


def _stop_covers_position(order, qty, rec=None):
    """KEYS listing (issue #39/#118): a protective stop covers a position of `qty` when it is closePosition, when
    it is the verified pre-arm of `rec` (sl_close_position), or when its quantity is >= qty. An unreadable quantity
    does not cover (the caller resizes, the safe direction). Never used on MCP (its listing folds reduceOnly into
    closePosition and drops the quantity)."""
    if not isinstance(order, dict):
        return False
    if _truthy(order.get('closePosition')):
        return True
    if rec and _truthy(rec.get('sl_close_position')) and rec.get('prearm_algo_id') is not None \
            and str(_order_id(order)) == str(rec.get('prearm_algo_id')):
        return True
    stop_qty = _to_float(order.get('quantity') or order.get('origQty'))
    return stop_qty > 0 and stop_qty >= abs(_to_float(qty)) * (1 - 1e-9)


def fill_quality_fields(is_long, entry_px, stop_px, tp1_px, tp2_px):
    """Issue #39 (log only, no gate): fill quality of a filled resting entry for its audit record, from the real
    entry price and the stop in force. realized_rr_tp2 = reward to TP2 / risk to the stop (None when the stop is at
    or beyond the entry, e.g. break-even); tp1_distance_pct = signed TP1 distance from the entry in percent.
    fill_quality_flags: "rr_below_3" (realized_rr_tp2 < FILL_MIN_RR_TP2) and/or "tp1_below_friction" (TP1 closer
    than MIN_TP1_DISTANCE, or on the wrong side)."""
    entry_px, stop_px = _to_float(entry_px), _to_float(stop_px)
    tp1_px, tp2_px = _to_float(tp1_px), _to_float(tp2_px)
    sign = 1.0 if is_long else -1.0
    risk = (entry_px - stop_px) * sign
    rr = round((tp2_px - entry_px) * sign / risk, 4) if entry_px > 0 and stop_px > 0 and tp2_px > 0 and risk > 0 else None
    tp1_frac = (tp1_px - entry_px) * sign / entry_px if entry_px > 0 and tp1_px > 0 else None
    flags = []
    if rr is not None and rr < FILL_MIN_RR_TP2:
        flags.append("rr_below_3")
    if tp1_frac is not None and tp1_frac < MIN_TP1_DISTANCE:
        flags.append("tp1_below_friction")
    return {"realized_rr_tp2": rr,
            "tp1_distance_pct": round(tp1_frac * 100, 4) if tp1_frac is not None else None,
            "fill_quality_flags": flags}


def _record_loss_vs_cap(rec, ref_price, equity, prof, unrealized=0.0, leverage=None, position_qty=None):
    """Issue #118/#156 (pure): the loss at the record's SL and its Gate 2 cap. qty = max(total_qty, position_qty)
    (a filled record whose total_qty was edited below |positionAmt| is sized on the live position); leverage = the
    live position's, else the record's. Returns {"loss", "cap", "qty", "message"}, or {"problem": str} for a YOLO
    record without a positive leverage (its margin cap cannot be sized: the record is untrusted)."""
    ref = _to_float(ref_price)
    sl = _to_float(rec.get('sl_price'))
    qty = max(_to_float(rec.get('total_qty')), abs(_to_float(position_qty)))
    is_yolo = _truthy(rec.get('is_yolo'))
    lev = _to_float(leverage) or _to_float(rec.get('leverage'))
    if is_yolo and lev <= 0:
        return {"problem": f"yolo record without leverage (leverage {rec.get('leverage')!r}); the YOLO margin cap "
                           "cannot be sized"}
    cap, risk_fraction, equity_used, _note = monetary_loss_cap(
        _to_float(equity), prof, is_testnet=False, is_yolo=is_yolo, ref=ref, total_qty=qty, leverage=lev,
        unrealized=_to_float(unrealized))
    loss = abs(ref - sl) * qty
    basis = (f"YOLO margin cap at {lev:g}x" if is_yolo else
             f"equity ${equity_used:.2f} x {risk_fraction * 100:.2f}% x 1.25")
    return {"loss": loss, "cap": cap, "qty": qty,
            "message": (f"loss at sl_price {sl} ({loss:.2f} USDT for {qty} from {ref}) exceeds the Gate 2 loss cap "
                        f"{cap:.2f} USDT ({basis})")}


def record_loss_cap_problem(rec, ref_price, equity, prof, unrealized=0.0, leverage=None, position_qty=None):
    """Issue #118 (PROD, pure): the loss at the record's SL, abs(ref_price - sl_price) x qty, must not exceed the
    Gate 2 cap (monetary_loss_cap): standard min(wallet, wallet + uPnL) x risk_pct_equity x 1.25; YOLO the margin
    cap, with `leverage` (the live position's when filled, else the record's). Issue #156: qty = max(total_qty,
    position_qty) (|positionAmt| when filled); a YOLO record without a positive leverage is a problem (never 1x).
    Returns a problem string or None."""
    out = _record_loss_vs_cap(rec, ref_price, equity, prof, unrealized, leverage, position_qty)
    if "problem" in out:
        return out["problem"]
    return out["message"] if out["loss"] > out["cap"] else None


def cancel_resting_entry(symbol, kind, entry_id, target_env=None):
    """Cancels a resting entry (algo order for STOP_MARKET, regular order for LIMIT). Returns (ok, response)."""
    try:
        oid = int(entry_id)
    except (TypeError, ValueError):
        oid = entry_id
    try:
        if kind == 'STOP_MARKET':
            res = send_signed_request('DELETE', '/fapi/v1/algoOrder', {'symbol': symbol, 'algoId': oid}, target_env=target_env)
        else:
            res = send_signed_request('DELETE', '/fapi/v1/order', {'symbol': symbol, 'orderId': oid}, target_env=target_env)
    except Exception as e:
        res = {"error": str(e)}
    return not _is_api_error(res), res


def check_guardian_alive(target_env, now=None):
    """(ok, reason): logs/guardian_state.json was written by a running guardian LOOP (mode "loop", not a single
    --once run) for target_env, not in --dry-run, with interval_seconds <= GUARDIAN_MAX_INTERVAL_FOR_RESTING and a
    last cycle no older than max_age = 2 * interval_seconds + 30s (guardian_loop_state_fresh). Issue #40: the last
    cycle must also be healthy for resting entries: its error_stages may not hold "positions_sync" or a "pending_*"
    stage other than the report-only "pending_unknown_entry" (trailing / dead-alpha errors do not count; a state
    without error_stages, written before issue #40, is healthy). A loop killed after its last write still reads as
    alive for up to max_age (inherent to a liveness file; declined in issue #40). Read-only."""
    now = int(now if now is not None else time.time())
    path = os.path.join(_workspace_dir(), 'logs', 'guardian_state.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except FileNotFoundError:
        return False, "logs/guardian_state.json not found"
    except (OSError, ValueError) as e:
        return False, f"logs/guardian_state.json unreadable ({e})"
    if not isinstance(state, dict):
        return False, "logs/guardian_state.json malformed"
    if state.get('env') != target_env:
        return False, f"guardian state is for env {state.get('env')!r}, not {target_env!r}"
    if state.get('dry_run') is not False:
        return False, "guardian is running in --dry-run mode"
    if state.get('mode') != 'loop':
        return False, "guardian state was not written by a running loop (a single --once run does not count)"
    try:
        interval = int(state.get('interval_seconds'))
    except (TypeError, ValueError):
        return False, "guardian state has no valid interval_seconds"
    if interval <= 0 or interval > GUARDIAN_MAX_INTERVAL_FOR_RESTING:
        return False, f"guardian loop interval {interval}s exceeds {GUARDIAN_MAX_INTERVAL_FOR_RESTING}s"
    try:
        ts = int(state.get('timestamp'))
    except (TypeError, ValueError):
        return False, "guardian state has no valid timestamp"
    age = now - ts
    max_age = 2 * interval + 30
    if not guardian_loop_state_fresh(state, now):
        return False, f"guardian state is stale ({age}s old, limit {max_age}s for a {interval}s loop)"
    stages = state.get('error_stages')
    if stages is not None:
        if not isinstance(stages, list):
            return False, "guardian state error_stages is malformed"
        blocking = sorted({str(s) for s in stages if s == 'positions_sync'
                           or (str(s).startswith('pending_') and s != 'pending_unknown_entry')})
        if blocking:
            return False, (f"the guardian's last cycle failed in {', '.join(blocking)} (it cannot protect resting "
                           "entries reliably)")
    return True, f"guardian alive ({age}s old, {interval}s loop)"


def guardian_loop_state_fresh(state, now=None):
    """True when a guardian_state dict was written by a loop (mode "loop") with a positive interval_seconds and a
    timestamp no older than 2 * interval_seconds + 30s (and at most 60s in the future): the age rule of
    check_guardian_alive, also used by the guardian's --once to keep a live loop's state (issue #40). Pure."""
    now = int(now if now is not None else time.time())
    if not isinstance(state, dict) or state.get('mode') != 'loop':
        return False
    try:
        interval, ts = int(state.get('interval_seconds')), int(state.get('timestamp'))
    except (TypeError, ValueError):
        return False
    age = now - ts
    return interval > 0 and ts > 0 and -60 <= age <= 2 * interval + 30


def check_pending_entry_conflict(symbol, target_env):
    """
    PROD gate for EVERY new entry (MARKET, breached-trigger fallthrough, LIMIT, STOP_MARKET): the symbol must have
    no pending resting entry for target_env, and the registry must be readable (fail closed). A second entry on the
    symbol would make the fill detection of the pending one ambiguous. Returns (ok, message_or_None). Read-only.
    """
    symbol = str(symbol).upper()
    entries, err = load_pending_entries()
    if err:
        return False, f"ENTRY REJECTED: FAIL-CLOSED — {err}; cannot verify pending resting entries for {symbol}."
    for key, rec in entries.items():
        if isinstance(rec, dict) and rec.get('target_env') == target_env and str(rec.get('symbol', '')).upper() == symbol:
            return False, (f"ENTRY REJECTED: {symbol} has a pending resting entry ({key}) in logs/pending_entries.json; "
                           "wait until it fills (protected by --protect-pending / the guardian) or expires.")
    return True, None


def fetch_live_gate_snapshot(target_env):
    """
    PROD gate inputs read from the exchange, fetched ONCE per order attempt (issue #101): three all-symbol GETs,
    /fapi/v1/openAlgoOrders, /fapi/v1/openOrders, then /fapi/v2/positionRisk (issue #160: listings first, as in
    _protect_pending_entry, so an entry filling between the reads is counted twice, the safe side, instead of
    vanishing from both). Gate 0A (max open positions), Gate 1
    (delta-neutral, incl. resting opening orders), Gate 2 (unrealized PnL of the open positions, issue #119) and the
    unregistered-resting-entry check (1d) all read this snapshot, so editing or deleting
    logs/session_state.json / logs/pending_entries.json cannot make a gate pass that the live state would fail.
    Returns (snapshot, error): snapshot = {"env", "fetched_at", "positions": [rows], "exposure":
    utils.portfolio_exposure.compute_exposure(rows), "open_algo_orders": [...], "open_orders": [...],
    "registry": load_pending_entries_status()}; error is set (callers reject the order) when any query fails or
    returns a non-list. Issue #127: the registry is read BEFORE the exchange queries and Gate 0A / Gate 1 use this
    capture, so an entry that fills or is cancelled between the reads is a record without a live order (ignored),
    never a live order without its record (a spurious rejection). Check 1d keeps its own read after the queries
    (#46). A registry read error is kept in the capture (the gates fail closed on it as before). Read-only.
    """
    snap = {"env": target_env, "fetched_at": int(time.time()), "registry": load_pending_entries_status()}
    for name, endpoint in (('open_algo_orders', '/fapi/v1/openAlgoOrders'), ('open_orders', '/fapi/v1/openOrders'),
                           ('positions', '/fapi/v2/positionRisk')):
        try:
            res = send_signed_request('GET', endpoint, target_env=target_env)
        except Exception as e:
            res = {"error": str(e)}
        if not isinstance(res, list):
            return None, f"{endpoint} query failed: {res}"
        snap[name] = [r for r in res if isinstance(r, dict)] if name != 'positions' else res
    try:
        snap["exposure"] = compute_exposure(snap["positions"])   # a malformed row is a read failure (fail closed)
    except ValueError as e:
        return None, f"/fapi/v2/positionRisk returned malformed data: {e}"
    return snap, None


def record_price_tolerances(records, exchange_ticks=None):
    """Issue #126: {symbol: half a tick} from the tick_size stored in registry records at registration (exchangeInfo
    is not cached, so no extra read here). Records without a positive tick_size add nothing (exact match).
    Issue #160: the registry is editable, so a record's tick_size is capped at the exchange tick of its symbol when
    exchange_ticks ({symbol: tickSize}, filters already fetched by the caller) knows it."""
    known = {str(s).upper(): _to_float(t) for s, t in (exchange_ticks or {}).items()}
    out = {}
    for r in records or []:
        tick = _to_float((r or {}).get('tick_size')) if isinstance(r, dict) else 0.0
        if tick > 0 and known.get(str(r.get('symbol') or '').upper(), 0.0) > 0:
            tick = min(tick, known[str(r.get('symbol') or '').upper()])
        if tick > 0:
            sym = str(r.get('symbol') or '').upper()
            out[sym] = min(out.get(sym, tick / 2.0), tick / 2.0)
    return out


def _snapshot_registry(live, base_dir=None):
    """(entries, error, missing) of the registry: the capture of a fetch_live_gate_snapshot (issue #127) when `live`
    carries one and no other base_dir is asked for, else a fresh load_pending_entries_status(base_dir)."""
    captured = (live or {}).get('registry') if isinstance(live, dict) else None
    if base_dir is None and isinstance(captured, tuple) and len(captured) == 3:
        return captured
    return load_pending_entries_status(base_dir=base_dir)


def _live_snapshot_or_error(live, target_env, prefix):
    """Returns (snapshot, None) or (None, rejection message): reuses `live` when given, else fetches it."""
    if live is not None:
        return live, None
    live, err = fetch_live_gate_snapshot(target_env)
    if err:
        return None, (f"{prefix}: FAIL-CLOSED — cannot read the live exchange state for the PROD gates ({err}). "
                      "Order blocked.")
    return live, None


ALGO_FINAL_STATUSES = frozenset({'TRIGGERED', 'FINISHED', 'CANCELED', 'EXPIRED', 'REJECTED'})


def _is_opening_order(source, o):
    """An opening order of a listing: a dict that is neither closePosition nor reduceOnly (Stop Losses and TPs never
    count). Issue #126: an algo row with a FINAL algoStatus (ALGO_FINAL_STATUSES: TRIGGERED, FINISHED, CANCELED,
    EXPIRED, REJECTED; its child order / position is listed elsewhere) is not resting. NEW, TRIGGERING, unknown or
    missing statuses (the MCP listing drops it) stay resting (fail closed)."""
    if not isinstance(o, dict) or _truthy(o.get('closePosition')) or _truthy(o.get('reduceOnly')):
        return False
    return not (source == 'algo' and str(o.get('algoStatus') or '').strip().upper() in ALGO_FINAL_STATUSES)


def live_resting_opening_orders(live):
    """Opening orders resting in a live snapshot: [(source, kind, order)] for every algo / regular order that is
    neither closePosition nor reduceOnly (Stop Losses and TPs never count), nor an algo row with a final algoStatus
    (ALGO_FINAL_STATUSES, _is_opening_order). kind is the cancel_resting_entry kind."""
    out = []
    for source, key, kind in (('algo', 'open_algo_orders', 'STOP_MARKET'), ('order', 'open_orders', 'LIMIT')):
        out.extend((source, kind, o) for o in (live or {}).get(key) or [] if _is_opening_order(source, o))
    return out


def find_unregistered_resting_entries(target_env, live=None):
    """
    Read-only cross-check of the exchange against logs/pending_entries.json (Issue #46): a missing registry reads
    as empty, so an opening order resting on the exchange without a record would never get its SL on fill.
    Two all-symbol GETs (both endpoints, KEYS and MCP gateway, accept an omitted symbol):
      - /fapi/v1/openAlgoOrders: an algo order that is neither closePosition nor reduceOnly is an ENTRY (desk
        conditional STOP_MARKET entries); it is unknown unless a STOP_MARKET record of target_env has its algoId;
      - /fapi/v1/openOrders: a regular order that is neither reduceOnly nor closePosition is an ENTRY (desk
        resting LIMIT entries); it is unknown unless a LIMIT record of target_env has its orderId.
    Stop Losses and TPs (closePosition / reduceOnly) never count. Through the MCP gateway the algo listing keeps
    only conditional types with a trigger and folds reduceOnly into closePosition (same classification).
    Returns (unknown, error): unknown = [{"symbol", "source": "algo" | "order", "kind": "STOP_MARKET" | "LIMIT"
    (the cancel_resting_entry kind), "id", "type", "side", "price", "quantity"}]; error is set (and callers fail
    closed) when the registry is unreadable or either query fails. The registry is read AFTER the queries, so an
    entry the executor places and registers meanwhile is not reported. `live` (fetch_live_gate_snapshot, PROD order
    attempt) reuses its already fetched order listings instead of querying again.
    """
    if live is not None:
        listed = live_resting_opening_orders(live)
    else:
        listed = []
        for source, endpoint, kind in (('algo', '/fapi/v1/openAlgoOrders', 'STOP_MARKET'),
                                       ('order', '/fapi/v1/openOrders', 'LIMIT')):
            try:
                res = send_signed_request('GET', endpoint, target_env=target_env)
            except Exception as e:
                res = {"error": str(e)}
            if not isinstance(res, list):
                return [], f"{endpoint} query failed: {res}"
            listed.extend((source, kind, o) for o in res if _is_opening_order(source, o))
    entries, err = load_pending_entries()
    if err:
        return [], err
    known = {pending_entry_key(target_env, rec.get('symbol'), rec.get('entry_id')):
             ('STOP_MARKET' if str(rec.get('kind', '')).upper() == 'STOP_MARKET' else 'LIMIT')
             for rec in entries.values() if isinstance(rec, dict) and rec.get('target_env') == target_env}
    unknown = []
    for source, kind, o in listed:
        oid = _order_id(o)
        sym = str(o.get('symbol') or '').upper()
        if known.get(pending_entry_key(target_env, sym, oid)) == kind:
            continue
        unknown.append(resting_entry_info(source, kind, o))
    return unknown, None


def resting_entry_info(source, kind, o):
    return {"symbol": str(o.get('symbol') or '').upper(), "source": source, "kind": kind, "id": _order_id(o),
            "type": _order_type(o), "side": o.get('side'), "price": _trigger_price(o) or _to_float(o.get('price')),
            "quantity": o.get('quantity') or o.get('origQty')}


def describe_unregistered_entries(unknown):
    return ", ".join(f"{u['symbol']} {u['kind']} {u['source']} {u['id']} ({u['type']} {u['side']} @ {u['price']})"
                     for u in unknown)


def check_unregistered_resting_entries(target_env, live=None):
    """
    PROD gate for EVERY new entry, before any write (Issue #46): no opening order may rest on the exchange
    without a record in logs/pending_entries.json for target_env (deleted / lost registry, manual order). Fails
    closed on a query error. `live`: the order attempt's fetch_live_gate_snapshot (no second query).
    Returns (ok, message_or_None). Read-only (find_unregistered_resting_entries).
    """
    unknown, err = find_unregistered_resting_entries(target_env, live=live)
    if err:
        return False, (f"ENTRY REJECTED: FAIL-CLOSED — cannot cross-check resting entries on the exchange against "
                       f"logs/pending_entries.json ({err}).")
    if unknown:
        return False, unregistered_entries_message(unknown, missing=load_pending_entries_status()[2])
    return True, None


def unregistered_entries_message(unknown, missing=False):
    note = " (the registry file is MISSING)" if missing else ""
    return (f"ENTRY REJECTED: FAIL-CLOSED — {len(unknown)} resting entry order(s) on the exchange are not in "
            f"logs/pending_entries.json{note} and would get no Stop Loss on fill: {describe_unregistered_entries(unknown)}. "
            "Cancel them (or restore their registry records) before any new entry.")


AUDIT_TAIL_BYTES = 4 * 1024 * 1024   # issue #94: Gate 0A reads at most the last 4 MiB of logs/trades_audit.jsonl


def read_audit_tail(path, max_bytes=None, stats=None):
    """JSON-object records of the last `max_bytes` (AUDIT_TAIL_BYTES) of a JSONL file, in file order (issue #94).
    When the read starts after offset 0 its first, partial line is dropped; malformed lines are skipped. Raises
    OSError when the file cannot be read. `stats` (optional dict) receives "malformed_lines": the number of
    skipped non-blank lines that are not a JSON object (the dropped partial first line is not counted)."""
    max_bytes = AUDIT_TAIL_BYTES if max_bytes is None else max_bytes
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        # One byte earlier: when it is the newline ending the previous line, the first chunk is empty, so dropping
        # it never loses a complete line.
        f.seek(start - 1 if start > 0 else 0)
        data = f.read()
    lines = data.split(b'\n')
    if start > 0:
        lines = lines[1:]
    records = []
    malformed = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            r = json.loads(raw.decode('utf-8'))
        except (ValueError, TypeError, UnicodeDecodeError):
            malformed += 1
            continue
        if isinstance(r, dict):
            records.append(r)
        else:
            malformed += 1
    if stats is not None:
        stats["malformed_lines"] = malformed
    return records


def check_max_open_positions(prof, target_env, base_dir=None, live=None):
    """
    Gate 0A (max_open_positions), evaluated before any write. Committed slots = open positions (logs/session_state.json)
    + symbols with a pending resting entry for target_env in logs/pending_entries.json that have no open position yet
    (a partially filled LIMIT has both a position and a record: counted once)
    + symbols with a recent fill for target_env in logs/trades_audit.jsonl since last session_state sync (Issue #47).
    A new entry is rejected when committed slots >= profile max_open_positions.
    PROD (issue #101) is anchored to the exchange and takes the stricter of the files and the live view
    (`live` = the order attempt's fetch_live_gate_snapshot, fetched here when None; a read failure rejects):
      open = max(file count, |file symbols U live positionRisk symbols|); pending = registry symbols U symbols with a
      live resting opening order (not reduceOnly / closePosition), minus open symbols; recent fills skip open symbols.
      A missing or corrupt session_state.json rejects; a missing registry while opening orders rest on the exchange
      rejects (cancel them or restore their records); an unreadable registry rejects. The registry is the one
      captured in the snapshot before its exchange reads (issue #127, _snapshot_registry). An existing but
      unreadable trades_audit.jsonl rejects (issue #94); only its last AUDIT_TAIL_BYTES are read.
    TESTNET is file-only and unchanged: a missing or corrupt session_state counts zero, an unreadable registry counts
    zero pending entries, an unreadable audit log counts zero recent fills. Returns (ok, message_or_None). Read-only.
    """
    target_env = resolve_env(target_env)
    is_testnet = str(target_env).lower() == 'testnet'
    max_open_positions = int((prof or {}).get("max_open_positions", 3))
    prefix = f"MECHANICAL HARD GATE REJECTION: Max open positions limit ({max_open_positions})"

    base = base_dir or _workspace_dir()
    state_file = os.path.join(base, 'logs', 'session_state.json')
    open_count = 0
    open_symbols = set()
    last_sync_ts = 0
    if not is_testnet and not os.path.exists(state_file):
        return False, (f"{prefix}: FAIL-CLOSED — logs/session_state.json does not exist; cannot count open positions "
                       "in PROD. Re-sync session state (scripts/sync_session_state.py). Order blocked.")
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                state_data_pos = json.load(f)
            open_count = state_data_pos.get('portfolio_exposure', {}).get('total_active_positions')
            if open_count is None:
                open_count = len(state_data_pos.get('active_positions', []))
            open_count = int(open_count)
            try:
                last_sync_ts = int(state_data_pos.get('last_updated_ts', 0))
            except (TypeError, ValueError):
                last_sync_ts = 0
        except Exception as e:
            if not is_testnet:
                return False, (f"{prefix}: FAIL-CLOSED — logs/session_state.json is corrupt ({type(e).__name__}: {e}); "
                               "cannot count open positions in PROD. Order blocked.")
            open_count = 0
            last_sync_ts = 0
        else:
            try:
                open_symbols = {str(p.get('symbol')).upper() for p in state_data_pos.get('active_positions') or []
                                if isinstance(p, dict) and p.get('symbol')}
            except Exception:
                open_symbols = set()

    live_symbols = set()
    live_resting = []
    if not is_testnet:
        live, live_err = _live_snapshot_or_error(live, target_env, prefix)
        if live_err:
            return False, live_err
        live_symbols = set(live["exposure"]["symbols"])
        live_resting = live_resting_opening_orders(live)
        open_symbols |= live_symbols
        open_count = max(open_count, len(open_symbols))

    entries, err, missing = _snapshot_registry(live if not is_testnet else None, base_dir=base_dir)
    if err:
        if not is_testnet:
            return False, (f"{prefix}: FAIL-CLOSED — {err}; cannot count pending resting entries "
                           "(logs/pending_entries.json). Order blocked.")
        entries = {}
    if missing and live_resting:
        return False, unregistered_entries_message([resting_entry_info(*r) for r in live_resting], missing=True)
    pending_symbols = set()
    for rec in entries.values():
        if isinstance(rec, dict) and rec.get('target_env') == target_env:
            sym = str(rec.get('symbol', '')).upper()
            if sym and sym not in open_symbols:
                pending_symbols.add(sym)
    for _source, _kind, o in live_resting:
        sym = str(o.get('symbol') or '').upper()
        if sym and sym not in open_symbols:
            pending_symbols.add(sym)
    pending_count = len(pending_symbols)

    audit_file = os.path.join(base, 'logs', 'trades_audit.jsonl')
    # Recent fills are the records since the last session_state sync (Gate 1 requires it <= 300s old in PROD; 300s
    # without a sync timestamp): a handful of records, so the AUDIT_TAIL_BYTES (4 MiB) tail read covers the window.
    cutoff_ts = last_sync_ts if last_sync_ts > 0 else (time.time() - 300)
    recent_fill_symbols = set()
    if os.path.exists(audit_file):
        try:
            audit_records = read_audit_tail(audit_file)
        except OSError as e:
            # Issue #94: an existing but unreadable audit log hides recent fills: PROD rejects, TESTNET counts none.
            if not is_testnet:
                return False, (f"{prefix}: FAIL-CLOSED — logs/trades_audit.jsonl is unreadable ({type(e).__name__}: "
                               f"{e}); cannot count recent fills. Order blocked.")
            audit_records = []

        latest_entry_ts = {}
        latest_entry_seq = {}
        latest_abort_ts = {}
        latest_abort_seq = {}

        for seq, rec in enumerate(audit_records):
            rec_env = rec.get('target_env')
            if rec_env != target_env and str(rec_env).lower() != str(target_env).lower():
                continue
            try:
                rec_ts = float(rec.get('timestamp', 0))
            except (TypeError, ValueError):
                rec_ts = 0.0
            if rec_ts < cutoff_ts:
                continue
            sym = str(rec.get('symbol', '')).strip().upper()
            if not sym:
                continue

            event = rec.get('event')
            if event == 'CRITICAL_FAILSAFE_ABORT':
                if (rec_ts > latest_abort_ts.get(sym, -1.0) or
                    (rec_ts == latest_abort_ts.get(sym, -1.0) and seq > latest_abort_seq.get(sym, -1))):
                    latest_abort_ts[sym] = rec_ts
                    latest_abort_seq[sym] = seq
            elif (not event) or ('total_qty' in rec):
                if (rec_ts > latest_entry_ts.get(sym, -1.0) or
                    (rec_ts == latest_entry_ts.get(sym, -1.0) and seq > latest_entry_seq.get(sym, -1))):
                    latest_entry_ts[sym] = rec_ts
                    latest_entry_seq[sym] = seq

        for sym, e_ts in latest_entry_ts.items():
            if sym in open_symbols or sym in pending_symbols:
                continue
            a_ts = latest_abort_ts.get(sym)
            if a_ts is not None:
                e_seq = latest_entry_seq.get(sym, 0)
                a_seq = latest_abort_seq.get(sym, 0)
                if a_ts > e_ts or (a_ts == e_ts and a_seq > e_seq):
                    continue
            recent_fill_symbols.add(sym)

    recent_count = len(recent_fill_symbols)
    committed_count = open_count + pending_count + recent_count

    if committed_count >= max_open_positions:
        live_note = ""
        if not is_testnet:
            resting_syms = sorted({str(o.get('symbol') or '').upper() for _s, _k, o in live_resting})
            live_note = (f" Live exchange view: {len(live_symbols)} open position symbol(s) {sorted(live_symbols)}, "
                         f"{len(resting_syms)} symbol(s) with resting opening orders {resting_syms}.")
        if recent_count > 0:
            return False, (f"{prefix} reached (open {open_count} + pending {pending_count} + "
                           f"recent fills {recent_count} >= max {max_open_positions}).{live_note}")
        return False, (f"{prefix} reached (open {open_count} + pending {pending_count} >= max {max_open_positions})."
                       f"{live_note}")
    return True, None


def check_resting_entry_gates(symbol, target_env):
    """
    PROD gates for entries that rest on the book (untriggered STOP_MARKET, LIMIT), evaluated before any write.
    Their TPs (and their SL when it cannot be pre-armed: MCP, crossed or rejected, issue #36) are only placed on fill
    by protect_pending_entries, which also verifies a pre-armed SL, so:
      1. a guardian loop must be alive for this env (fail closed, see check_guardian_alive);
      2. the symbol must have no open position (keeps fill detection unambiguous).
    (No pending entry on the symbol is enforced for every entry by check_pending_entry_conflict.)
    Returns (ok, message_or_None). Read-only.
    """
    symbol = str(symbol).upper()
    alive, why = check_guardian_alive(target_env)
    if not alive:
        return False, (
            f"CONDITIONAL ENTRY REJECTED: FAIL-CLOSED — the position guardian is not alive ({why}). A resting entry "
            "gets its TPs (and its Stop Loss unless pre-armed) on fill and its stop verified by the guardian; start "
            "the guardian first: "
            f"`python3 scripts/loops/position_guardian_loop.py --interval 60 --env {target_env}`, or install it as a "
            f"background service: `python3 scripts/install_guardian_service.py --install --env {target_env}`."
        )
    try:
        pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol}, target_env=target_env)
    except Exception as e:
        pos_res = {"error": str(e)}
    if not isinstance(pos_res, list):
        return False, f"CONDITIONAL ENTRY REJECTED: FAIL-CLOSED — cannot verify open positions for {symbol} ({pos_res})."
    for p in pos_res:
        if isinstance(p, dict) and str(p.get('symbol', symbol)).upper() == symbol and _to_float(p.get('positionAmt')) != 0:
            return False, (f"CONDITIONAL ENTRY REJECTED: {symbol} already has an open position ({p.get('positionAmt')}); "
                           "a resting entry cannot share the symbol with it.")
    return True, None


def uses_mcp_gateway(target_env=None):
    """True when orders route through the Binance Agentic MCP gateway (get_client_config auth-mode detection).
    On a detection error returns True: the quantity-based reduce-only stop it implies is valid in both modes."""
    try:
        return get_client_config(target_env)[0] == "MCP_OAUTH_ACTIVE"
    except Exception:
        return True


def _is_immediate_trigger(res):
    """Binance -2021 'Order would immediately trigger' (the stop price is already crossed)."""
    if not isinstance(res, dict):
        return False
    text = f"{res.get('code', '')} {res.get('msg', '')} {res.get('error', '')}".lower()
    return '-2021' in text or 'immediately trigger' in text


def _market_order_accepted(res):
    if not isinstance(res, dict) or 'orderId' not in res or _is_api_error(res):
        return False
    status = str(res.get('status', '')).upper()
    return status in ('', 'FILLED', 'NEW', 'PARTIALLY_FILLED')


def _wait_until_flat(symbol, is_long, target_env=None, retry_delays=STOP_VERIFY_RETRY_DELAYS):
    """Progressive check that no position in the given direction remains on symbol."""
    for delay in (0.0,) + tuple(retry_delays):
        if delay:
            time.sleep(delay)
        try:
            pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol}, target_env=target_env)
        except Exception:
            continue
        if not isinstance(pos_res, list):
            continue
        if not any(isinstance(p, dict) and str(p.get('symbol', symbol)).upper() == symbol and
                   ((_to_float(p.get('positionAmt')) > 0) if is_long else (_to_float(p.get('positionAmt')) < 0))
                   for p in pos_res):
            return True
    return False


def _cancel_symbol_orders(symbol, target_env=None):
    """Cancels every open order and algo order of a FLAT symbol (leftover stops, TPs). Returns a list of errors."""
    errors = []
    try:
        res = send_signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol}, target_env=target_env)
        if isinstance(res, dict) and _is_api_error(res):
            errors.append(f"allOpenOrders: {res}")
    except Exception as e:
        errors.append(f"allOpenOrders: {e}")
    try:
        algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}, target_env=target_env)
    except Exception as e:
        algos = {"error": str(e)}
    if not isinstance(algos, list):
        return errors + [f"openAlgoOrders: {algos}"]
    for ao in algos:
        aid = _order_id(ao) if isinstance(ao, dict) else None
        if aid is None:
            continue
        try:
            res = send_signed_request('DELETE', '/fapi/v1/algoOrder', {'symbol': symbol, 'algoId': aid}, target_env=target_env)
        except Exception as e:
            res = {"error": str(e)}
        if isinstance(res, dict) and _is_api_error(res):
            errors.append(f"algo {aid}: {res}")
    return errors


def protect_pending_entries(target_env=None, dry_run=False, keys=None):
    """
    Strictly risk-reducing follow-up of logs/pending_entries.json (never opens or increases a position).
    For every record of target_env (only `keys` when given):
      - filled (position in the entry direction): ensure a verified protective stop, NEVER loosening one:
          * no stop at all -> _ensure_entry_stop: a still-verified pre-armed stop (issue #36) is kept, else the planned
            SL is placed (closePosition; -4130 re-verified as kept); if it cannot be verified, cancel the entry and
            close the position reduce-only (auto-destruct);
          * every existing stop looser than the plan (e.g. the 2.5% orphan heal) -> replace with the planned SL,
            place-then-cancel;
          * an existing stop at or tighter than the plan (trailing, break-even, the pre-arm) -> kept. KEYS: resized
            place-then-cancel at that tighter price for the full current size only when it does not cover the
            position (not closePosition and its listed quantity < |positionAmt|; issues #39 / #118). MCP (no
            readable coverage): resized when a partial LIMIT fill grew beyond sl_qty, or once when no sl_qty yet.
          A replace/resize that cannot be verified keeps the existing stop(s) (no auto-destruct) and is retried.
          If the planned SL is already crossed (mark beyond it, or -2021 on placement), a resting entry remainder is
          cancelled first, the position re-read and its live size closed reduce-only at MARKET without cancelling
          existing stops; leftovers are cancelled only once flat. A failed close with no stop at all gets an
          orphan-heal stop (issue #39).
        Once the entry order is gone, TP1/TP2 are placed reduce-only from the ACTUAL position size (idempotent:
        placed TP ids are saved first and only a missing TP is retried; issue #160: that save and the audit_done save
        retry a registry lock error, and a missing TP id first adopts a reduce-only exit-side LIMIT already resting
        at its price, find_resting_take_profits; a failed read places as before with a "tp_reconcile" warning),
        an audit record is appended (with
        realized_rr_tp2, tp1_distance_pct and fill_quality_flags, issue #39, log only) and the record dropped.
        A partially filled LIMIT keeps its record (remainder cancelled at expiry, TPs on a later run).
      - not filled and still open: cancelled once expires_at_ts is reached, else kept.
      - not filled and no longer open: marked missing_since_ts and dropped only if still so on a run at least
        PENDING_MISSING_GRACE_SECONDS later (positionRisk can lag behind a trigger).
      - every path that ends the entry without a position (timeout, drop, untrusted cancel) also cancels the
        record's pre-armed stop by prearm_algo_id only (cancel_prearmed_stop, after a positionRisk re-read); a failed
        cancel keeps the record for the next run.
      - record not trusted (issue #101, pending_record_mismatch + an error): while the entry rests, its side, quantity
        (within stepSize) and trigger/limit price (within tickSize) must match the record, and the record must be
        consistent (qty > 0, SL on the loss side, TP1/TP2 on the profit side of its entry price); otherwise the
        entry is cancelled and the record dropped (a partial position is protected as an orphan). Filled with an
        invalid SL: the record's SL is never used; with no protective stop, heal_orphan_position(close_on_failure)
        and drop; a kept stop must cover the position (KEYS: closePosition or quantity >= |positionAmt|; MCP: always
        replaced) or it is replaced place-then-cancel at its price (issue #118). Filled with invalid TPs only: the
        SL is handled, no TP is placed, record dropped.
        PROD (issue #118): the record's SL is also invalid when abs(trigger_or_limit_price - sl_price) x total_qty
        exceeds the Gate 2 loss cap (record_loss_cap_problem; YOLO leverage from the live position when filled); a
        failed equity read only adds a "warnings" entry and defers the check. TESTNET skips it.
        Issue #156: a filled record is sized on max(total_qty, |positionAmt|); a YOLO record without leverage is
        untrusted; a profile not read from config/user_profile.json defers the check of a standard record. Equity
        drift: a FILLED standard record whose loss exceeds the live cap but not min(gate2_loss_cap_usdt stored at
        placement, live cap x PENDING_DRIFT_CAP_TOLERANCE 1.2) stays trusted (TPs placed) with a "loss_cap_drift"
        warning; above that bound, a resting entry, a YOLO record or a record without the stored cap: breach as above. Consecutive runs with a
        deferred loss-cap or qty check are counted in check_deferrals (reset by a run without deferral); the
        PENDING_DEFERRAL_REPORT_AFTER-th files one HIGH issue (report_agent_issue). Deferrals stay warnings.
    Any query error keeps the record (fail closed). dry_run reports the decisions without any write. A missing
    registry reads as empty (never blocks this risk-reducing path).
    Returns {"ok", "env", "dry_run", "actions": [{"type", "key", "symbol", "success", "dry_run", "detail"}],
             "errors": [{"key", "symbol", "stage", "error"}]}
    with action types pending_protect_sl | pending_tp_placed | pending_abort | pending_timeout_cancel | pending_dropped |
    pending_sl_crossed_close | pending_record_mismatch.
    """
    dry_run = _truthy(dry_run)
    out = {"ok": False, "env": None, "dry_run": dry_run, "actions": [], "errors": []}
    try:
        target_env = resolve_env(target_env)
    except ValueError as e:
        out["errors"].append({"key": None, "symbol": None, "stage": "env", "error": str(e)})
        return out
    out["env"] = target_env
    entries, err = load_pending_entries()
    if err:
        out["errors"].append({"key": None, "symbol": None, "stage": "registry", "error": err})
        return out
    now = int(time.time())
    run_ctx = {}   # per-run cache of the PROD equity read (issue #118 loss cap check)
    for key in sorted(entries):
        rec = entries[key]
        if keys is not None and key not in keys:
            continue
        if not isinstance(rec, dict) or rec.get('target_env') != target_env:
            continue
        try:
            _protect_pending_entry(key, rec, target_env, dry_run, now, out, run_ctx)
        except Exception as e:
            out["errors"].append({"key": key, "symbol": rec.get('symbol'), "stage": "exception",
                                  "error": f"{type(e).__name__}: {e}"})
    out["ok"] = not out["errors"]
    return out


def pending_record_problems(rec, is_long):
    """Internal consistency of a logs/pending_entries.json record (issue #101). Returns {"sl": [...], "tp": [...],
    "entry": [...]} (empty lists when consistent): total_qty and trigger_or_limit_price > 0; sl_price > 0 and on the
    loss side of the entry price (below it for a LONG); tp1_price / tp2_price > 0 and on the profit side."""
    ref = _to_float(rec.get('trigger_or_limit_price'))
    out = {"sl": [], "tp": [], "entry": []}
    if _to_float(rec.get('total_qty')) <= 0:
        out["entry"].append(f"total_qty {rec.get('total_qty')!r} is not positive")
    if ref <= 0:
        out["entry"].append(f"trigger_or_limit_price {rec.get('trigger_or_limit_price')!r} is not positive")
    sl = _to_float(rec.get('sl_price'))
    if sl <= 0:
        out["sl"].append(f"sl_price {rec.get('sl_price')!r} is not positive")
    elif ref > 0 and ((sl >= ref) if is_long else (sl <= ref)):
        out["sl"].append(f"sl_price {sl} is not on the loss side of entry {ref} for a {'LONG' if is_long else 'SHORT'}")
    for name in ('tp1_price', 'tp2_price'):
        tp = _to_float(rec.get(name))
        if tp <= 0:
            out["tp"].append(f"{name} {rec.get(name)!r} is not positive")
        elif ref > 0 and ((tp <= ref) if is_long else (tp >= ref)):
            out["tp"].append(f"{name} {tp} is not on the profit side of entry {ref}")
    return out


RECORD_QTY_SLACK = 0.02   # issue #126: total_qty may sit this fraction (plus one stepSize) below margin x lev / price


def record_qty_problem(rec, step_size=None):
    """Issue #126 (pure): a record's total_qty must not be shrunk below what its own sizing fields give:
    total_qty >= margin_usdt x leverage / trigger_or_limit_price x (1 - RECORD_QTY_SLACK) - one step_size (the
    registration rounds down by less than a step). Checked only when margin_usdt, leverage, trigger_or_limit_price
    and total_qty are all positive (v1 / partial records skip it). Returns a problem string or None."""
    margin, lev = _to_float(rec.get('margin_usdt')), _to_float(rec.get('leverage'))
    price, qty = _to_float(rec.get('trigger_or_limit_price')), _to_float(rec.get('total_qty'))
    if margin <= 0 or lev <= 0 or price <= 0 or qty <= 0:
        return None
    expected = margin * lev / price
    floor = expected * (1 - RECORD_QTY_SLACK) - max(_to_float(step_size), 0.0)
    if qty < floor:
        return (f"total_qty {qty} is below the record's sizing (margin_usdt {margin} x leverage {lev:g} / price "
                f"{price} = {expected:.8g}; floor {floor:.8g})")
    return None


def pending_entry_order_mismatches(rec, kind, order, is_long, filters=None):
    """Cross-check of the resting entry order on the exchange against its record (issue #101): side == the
    direction's entry side (and the record's entry_side); quantity (algo `quantity` / LIMIT `origQty`) == total_qty
    within half a stepSize; trigger (algo triggerPrice) / LIMIT price == trigger_or_limit_price within half a tickSize.
    A field the listing omits is not compared. Without filters, values must match exactly (1e-9 relative); callers
    re-check with filters before acting. Returns a list of mismatch descriptions."""
    out = []
    expected_side = 'BUY' if is_long else 'SELL'
    side = str(order.get('side') or '').upper()   # omitted by the listing: not compared
    if (side and side != expected_side) or str(rec.get('entry_side') or expected_side).upper() != expected_side:
        out.append(f"side {side or None} (record entry_side {rec.get('entry_side')}) != {expected_side} for "
                   f"{'LONG' if is_long else 'SHORT'}")
    step = _to_float((filters or {}).get('stepSize'))
    tick = _to_float((filters or {}).get('tickSize'))

    def differs(a, b, unit):
        tol = unit * 0.5 if unit > 0 else 0.0
        return abs(a - b) > max(tol, abs(b) * 1e-9)

    raw_qty = (order.get('quantity') if kind == 'STOP_MARKET' else order.get('origQty'))
    if raw_qty in (None, ''):
        raw_qty = order.get('origQty') if kind == 'STOP_MARKET' else order.get('quantity')
    if raw_qty not in (None, ''):
        qty, rec_qty = _to_float(raw_qty), _to_float(rec.get('total_qty'))
        if differs(qty, rec_qty, step):
            out.append(f"quantity {qty} != record total_qty {rec_qty}")
    price = _trigger_price(order) if kind == 'STOP_MARKET' else _to_float(order.get('price'))
    if price > 0:
        rec_price = _to_float(rec.get('trigger_or_limit_price'))
        if differs(price, rec_price, tick):
            out.append(f"{'triggerPrice' if kind == 'STOP_MARKET' else 'price'} {price} != record "
                       f"trigger_or_limit_price {rec_price}")
    return out


def _pending_loss_cap_problem(rec, position, target_env, run_ctx):
    """Issue #118 (PROD): record_loss_cap_problem with live inputs. The reference is the record's
    trigger_or_limit_price (the check is about the record, not the fill's slippage). Standard records read the wallet
    balance (quant_risk_engine.get_account_equity) and the all-symbol positionRisk uPnL once per protect-pending run
    (run_ctx); YOLO records need no read (margin cap; leverage from the live position when filled, else the record).
    Issue #156: for a standard record, a profile that did not come from config/user_profile.json (`_profile_source`
    "example" / "default") defers the check like a failed read (its risk_pct_equity is not the user's). A FILLED
    record (position given) is sized on max(total_qty, |positionAmt|); when a standard record's loss exceeds the
    live cap only by equity drift since placement, i.e. loss <= min(gate2_loss_cap_usdt stored at registration,
    live cap x PENDING_DRIFT_CAP_TOLERANCE), it stays trusted with a "loss_cap_drift" warning. A resting entry, a
    YOLO record, or a record without the stored cap is governed by the live cap alone.
    Returns (problem_or_None, warning_or_None, warning_stage): stage "loss_cap_check" for a deferral (a failed read
    or a non-user profile; never blocks), "loss_cap_drift" for a tolerated drift."""
    try:
        import user_profile as up
        prof = up.load_user_profile()
    except Exception:
        prof = {}
    equity, unrealized = 0.0, 0.0
    is_yolo = _truthy(rec.get('is_yolo'))
    if not is_yolo:
        # The YOLO margin cap reads neither the profile nor equity: only standard records defer on them.
        source = prof.get('_profile_source') if isinstance(prof, dict) else None
        if source is not None and source != 'user':
            return None, (f"SL distance vs loss cap check deferred to the next run: the user profile was not read "
                          f"from config/user_profile.json (source: {source})"), 'loss_cap_check'
        if 'equity' not in run_ctx:
            try:
                import quant_risk_engine as qre
                wallet = float(qre.get_account_equity(target_env))
                rows = send_signed_request('GET', '/fapi/v2/positionRisk', target_env=target_env)
                if not isinstance(rows, list):
                    raise ValueError(f"/fapi/v2/positionRisk query failed: {rows}")
                run_ctx['equity'] = (wallet, unrealized_pnl_total(compute_exposure(rows)))
            except Exception as e:
                run_ctx['equity'] = f"{type(e).__name__}: {e}"
        if isinstance(run_ctx['equity'], str):
            return None, (f"SL distance vs loss cap check deferred to the next run: equity read failed "
                          f"({run_ctx['equity']})"), 'loss_cap_check'
        equity, unrealized = run_ctx['equity']
    leverage = position.get('leverage') if position is not None else None
    position_qty = abs(_to_float(position.get('positionAmt'))) if position is not None else None
    out = _record_loss_vs_cap(rec, rec.get('trigger_or_limit_price'), equity, prof, unrealized, leverage,
                              position_qty)
    if "problem" in out:
        return out["problem"], None, None
    loss, cap = out["loss"], out["cap"]
    if loss <= cap:
        return None, None, None
    # Standard records only: a YOLO cap does not depend on equity, so a lower live YOLO cap means a higher live
    # leverage, never equity drift.
    stored = _to_float(rec.get('gate2_loss_cap_usdt'))
    if (position is not None and not is_yolo and stored > 0
            and loss <= min(stored, cap * PENDING_DRIFT_CAP_TOLERANCE)):
        return None, (f"loss at sl_price {loss:.2f} USDT exceeds the live Gate 2 cap {cap:.2f} USDT but not the cap "
                      f"stored at placement {stored:.2f} USDT (bounded by live x {PENDING_DRIFT_CAP_TOLERANCE:g}): "
                      "equity drift since placement; record kept trusted"), 'loss_cap_drift'
    return out["message"], None, None


def _protect_pending_entry(key, rec, target_env, dry_run, now, out, run_ctx=None):
    sym = str(rec['symbol']).upper()
    kind = 'STOP_MARKET' if str(rec.get('kind', '')).upper() == 'STOP_MARKET' else 'LIMIT'
    entry_id = str(rec['entry_id'])
    is_long = str(rec.get('direction', '')).upper() == 'LONG'
    direction = 'LONG' if is_long else 'SHORT'
    exit_side = 'SELL' if is_long else 'BUY'
    sl_p = _to_float(rec.get('sl_price'))   # validated below (pending_record_problems) before it is ever used
    expires = _to_float(rec.get('expires_at_ts'))

    def act(action_type, success, **detail):
        if dry_run:
            success = False
            detail.setdefault('planned', True)
        out["actions"].append({"type": action_type, "key": key, "symbol": sym, "success": bool(success),
                               "dry_run": dry_run, "detail": detail})

    def fail(stage, error):
        out["errors"].append({"key": key, "symbol": sym, "stage": stage, "error": str(error)})

    def drop():
        if not dry_run:
            update_pending_entries(lambda entries: entries.pop(key, None))

    def save(**fields):
        """Updates the registry record (a None value removes the field). No-op in dry run."""
        def mutate(entries):
            if isinstance(entries.get(key), dict):
                for k, v in fields.items():
                    if v is None:
                        entries[key].pop(k, None)
                    else:
                        entries[key][k] = v
        if not dry_run:
            update_pending_entries(mutate)

    def save_retrying(**fields):
        """save() retried on PendingRegistryLockError after each PENDING_SAVE_RETRY_DELAYS_S delay (issue #160: the
        TP ids and audit_done saves); the last failure is raised as before."""
        for delay in PENDING_SAVE_RETRY_DELAYS_S + (None,):
            try:
                return save(**fields)
            except PendingRegistryLockError as e:
                if delay is None:
                    raise
                logger.warning(f"{sym} {key}: registry update {sorted(fields)} retried in {delay}s ({e})")
                time.sleep(delay)

    def save_before_stop(**fields):
        """save() for the bookkeeping writes that run BEFORE the stop is placed/verified: a registry lock failure
        (issue #40) is logged as a "registry_lock" warning and the protection continues in this cycle (the write is
        retried on the next run). Stop placement is never skipped because of a registry write."""
        try:
            save(**fields)
        except PendingRegistryLockError as e:
            logger.warning(f"{sym} {key}: registry update {sorted(fields)} deferred ({e}); protection continues")
            out.setdefault("warnings", []).append({"key": key, "symbol": sym, "stage": "registry_lock",
                                                   "warning": f"registry update {sorted(fields)} deferred: {e}"})

    # Open orders first, then positions: a trigger between the two reads shows up as a position.
    orders_ep = '/fapi/v1/openAlgoOrders' if kind == 'STOP_MARKET' else '/fapi/v1/openOrders'
    open_res = send_signed_request('GET', orders_ep, {'symbol': sym}, target_env=target_env)
    if not isinstance(open_res, list):
        return fail("orders_query", f"{orders_ep} query failed: {open_res}")
    entry_order = next((o for o in open_res if isinstance(o, dict) and str(_order_id(o)) == entry_id), None)
    entry_open = entry_order is not None
    pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', {'symbol': sym}, target_env=target_env)
    if not isinstance(pos_res, list):
        return fail("position_query", f"Position query failed: {pos_res}")
    position = None
    for p in pos_res:
        if not isinstance(p, dict) or str(p.get('symbol', sym)).upper() != sym:
            continue
        amt = _to_float(p.get('positionAmt'))
        if (amt > 0) if is_long else (amt < 0):
            position = p
            break

    # The entry (or a position) is visible again: clear a previous "missing" mark.
    if rec.get('missing_since_ts') is not None and (entry_open or position is not None):
        save_before_stop(missing_since_ts=None)

    # --- Record vs exchange cross-check (issue #101): never act on a forged / corrupted record ---------------
    problems = pending_record_problems(rec, is_long)
    deferred = []   # issue #156: checks deferred in this run (counted in check_deferrals)
    if entry_open or position is not None:
        # Issue #126: a total_qty shrunk below the record's own sizing makes the record untrusted. The stepSize comes
        # from the record (stored at registration); exchangeInfo is read only when a record without it is flagged
        # without the step allowance. Unavailable filters defer the check (warning; never blocks the protection).
        qty_problem = record_qty_problem(rec, rec.get('step_size'))
        if qty_problem and _to_float(rec.get('step_size')) <= 0:
            try:
                qty_filters = get_symbol_filters(sym, target_env=target_env)
            except Exception:
                qty_filters = None
            if qty_filters:
                qty_problem = record_qty_problem(rec, qty_filters.get('stepSize'))
            else:
                out.setdefault("warnings", []).append({
                    "key": key, "symbol": sym, "stage": "qty_check",
                    "warning": "total_qty check deferred to the next run: symbol filters unavailable"})
                deferred.append("qty_check")
                qty_problem = None
        if qty_problem:
            problems["entry"].append(qty_problem)
    if (str(target_env).lower() != 'testnet' and (entry_open or position is not None)
            and not problems["sl"] and not problems["entry"]):
        # Issue #118 (PROD): an SL pushed further away (still on the loss side) must not exceed the Gate 2 loss cap.
        # A breach makes the record untrusted: the entry is cancelled while it rests; a filled position without a
        # stop gets the orphan heal, whose 2.5% anchor may be looser than the record SL (accepted: the record is not
        # trusted at that point).
        cap_problem, cap_warning, cap_stage = _pending_loss_cap_problem(rec, position, target_env,
                                                                        run_ctx if run_ctx is not None else {})
        if cap_problem:
            problems["sl"].append(cap_problem)
        if cap_warning:
            logger.warning(f"{sym} {key}: {cap_warning}")
            out.setdefault("warnings", []).append({"key": key, "symbol": sym, "stage": cap_stage,
                                                   "warning": cap_warning})
            if cap_stage == 'loss_cap_check':
                deferred.append("loss_cap_check")
    if entry_open or position is not None:
        # Issue #156: consecutive runs with a deferred loss-cap / qty check are counted on the record
        # (check_deferrals); reaching PENDING_DEFERRAL_REPORT_AFTER files one HIGH issue. A run where none was
        # deferred resets it. Never blocks (registry lock failures are warnings).
        if deferred:
            n_deferrals = int(_to_float(rec.get('check_deferrals'))) + 1
            save_before_stop(check_deferrals=n_deferrals)
            if n_deferrals == PENDING_DEFERRAL_REPORT_AFTER and not dry_run:
                report_err = _report_check_deferrals(sym, key, target_env, n_deferrals, deferred)
                if report_err:
                    out.setdefault("warnings", []).append({"key": key, "symbol": sym, "stage": "deferral_report",
                                                           "warning": report_err})
        elif rec.get('check_deferrals') is not None:
            save_before_stop(check_deferrals=None)

    def cancel_prearm_if_flat():
        """Issue #36: cancels the record's pre-armed stop (by algo id only) once the entry ended without a position.
        positionRisk is re-read first: a position (e.g. a fill racing the entry cancel) keeps the stop. Returns
        (ok, detail); not ok keeps the record so the next run retries (never blocking)."""
        pa_id = rec.get('prearm_algo_id')
        if pa_id is None:
            return True, None
        if dry_run:
            return True, f"planned cancel of pre-armed stop {pa_id}"
        row, err = _read_open_position(sym, target_env=target_env, is_long=is_long)
        if err:
            return False, f"position re-read failed ({err}); pre-armed stop {pa_id} kept"
        if row is not None:
            return False, f"a position appeared ({row.get('positionAmt')}); pre-armed stop {pa_id} kept"
        ok, res = cancel_prearmed_stop(sym, exit_side, pa_id, target_env=target_env)
        if not ok:
            logger.warning(f"{sym} {key}: cancel of pre-armed stop {pa_id} failed ({res}); retried next run")
        return ok, res

    def untrusted_record(reason, mismatches):
        """Cancels the resting entry (when open), protects any position WITHOUT the record (orphan heal only when no
        protective stop exists), drops the record. Never places the record's SL/TPs."""
        detail = dict(reason=reason, mismatches=mismatches, kind=kind, entry_id=entry_id, entry_open=entry_open,
                      position_amt=position.get('positionAmt') if position is not None else None)
        if dry_run:
            act("pending_record_mismatch", False, **detail)
            return fail("record_mismatch", f"{sym} record {key} not trusted ({'; '.join(mismatches)}); dry run.")
        success = True
        cur_pos = position
        if entry_open:
            ok, res = cancel_resting_entry(sym, kind, entry_id, target_env=target_env)
            detail.update(entry_cancelled=ok, cancel_result=res)
            if not ok:
                act("pending_record_mismatch", False, **detail)
                return fail("record_mismatch_cancel", f"{sym} record {key} not trusted ({'; '.join(mismatches)}) and "
                                                      f"the resting entry {entry_id} could not be cancelled ({res}); "
                                                      "record kept.")
            # The entry may have (partially) filled between the position read and the cancel: re-read before dropping.
            try:
                again = send_signed_request('GET', '/fapi/v2/positionRisk', {'symbol': sym}, target_env=target_env)
            except Exception as e:
                again = {"error": str(e)}
            if not isinstance(again, list):
                act("pending_record_mismatch", False, position_reread_error=str(again), **detail)
                return fail("record_mismatch_position_reread",
                            f"{sym} record {key} not trusted; entry {entry_id} cancelled but the position re-read failed "
                            f"({again}); record kept for the next run.")
            cur_pos = next((p for p in again if isinstance(p, dict) and str(p.get('symbol', sym)).upper() == sym
                            and ((_to_float(p.get('positionAmt')) > 0) if is_long else (_to_float(p.get('positionAmt')) < 0))),
                           None)
            detail.update(position_amt_after_cancel=cur_pos.get('positionAmt') if cur_pos is not None else None)
        if cur_pos is not None:
            stops, serr = get_open_stop_orders(sym, exit_side, target_env=target_env)
            if serr:
                act("pending_record_mismatch", False, stops_error=serr, **detail)
                return fail("record_mismatch_stops", f"{sym} record {key} not trusted; stops query failed ({serr}); "
                                                     "record kept for the next run.")
            if stops:
                detail.update(kept_stops=[stop_summary(s) for s in stops])
                # Issue #118: the kept stop must cover the whole position. KEYS reads the coverage from the listing
                # (closePosition, or quantity >= |positionAmt|; the untrusted record is not used); MCP cannot read
                # it, so a covering stop is always placed. Place-then-cancel at the tightest stop's price; if the
                # replacement is unverified the existing stops stay (the record is dropped anyway: it is untrusted).
                tight = tightest_stop(stops, is_long)
                pos_qty = abs(_to_float(cur_pos.get('positionAmt')))
                if uses_mcp_gateway(target_env) or not _stop_covers_position(tight, pos_qty):
                    cov_filters = get_symbol_filters(sym, target_env=target_env)
                    rep = replace_protective_stop(sym, exit_side, _trigger_price(tight),
                                                  format_order_qty(cur_pos.get('positionAmt')), stops,
                                                  target_env=target_env,
                                                  tick_size=(cov_filters or {}).get('tickSize'))
                    detail.update(coverage_replace=rep)
                    for ce in rep.get('cancel_errors', []):
                        fail("record_mismatch_cancel_old", ce)
                    if not rep.get('success'):
                        fail("record_mismatch_coverage", f"{sym} record {key} not trusted; the kept stop may not cover "
                                                         f"{pos_qty} and its covering replacement was not verified; "
                                                         "existing stop(s) kept.")
            else:
                heal = heal_orphan_position(cur_pos, target_env=target_env, close_on_failure=True)
                detail.update(heal=heal)
                success = bool(heal.get('success') or heal.get('closed'))
        elif rec.get('prearm_algo_id') is not None:
            # Issue #36: entry cancelled, no position: cancel the pre-armed stop (by algo id); retried next run.
            pa_ok, pa_res = cancel_prearmed_stop(sym, exit_side, rec.get('prearm_algo_id'), target_env=target_env)
            detail.update(prearm_cancelled=pa_ok, prearm_cancel_result=pa_res)
            if not pa_ok:
                success = False
                fail("record_mismatch_prearm_cancel", f"{sym} record {key}: entry cancelled but the pre-armed stop "
                                                      f"{rec.get('prearm_algo_id')} could not be cancelled ({pa_res}); "
                                                      "record kept for the next run.")
        act("pending_record_mismatch", success, **detail)
        fail("record_mismatch", f"{sym} pending entry record {key} does not match the exchange / is inconsistent "
                                f"({'; '.join(mismatches)}); "
                                + ("resting entry cancelled, " if entry_open else "")
                                + ("position protected without the record, " if cur_pos is not None else "")
                                + "record dropped.")
        if success:
            return drop()
        return None

    if entry_open:
        mismatches = pending_entry_order_mismatches(rec, kind, entry_order, is_long)
        if mismatches:
            # Re-check within the symbol's stepSize / tickSize before acting; never cancel on missing metadata.
            ex_filters = get_symbol_filters(sym, target_env=target_env)
            if not ex_filters:
                return fail("filters", f"Symbol filters unavailable for {sym}; cannot cross-check the resting entry "
                                       "against its record; record kept.")
            mismatches = pending_entry_order_mismatches(rec, kind, entry_order, is_long, ex_filters)
        mismatches = mismatches + problems["entry"] + problems["sl"] + problems["tp"]
        if mismatches:
            return untrusted_record("resting_entry_mismatch", mismatches)
    elif position is not None and (problems["sl"] or problems["entry"]):
        return untrusted_record("filled_with_invalid_sl", problems["sl"] + problems["entry"])

    # --- Not filled ---------------------------------------------------------
    if position is None:
        if not entry_open:
            # positionRisk can lag behind a trigger: drop only if still missing on a run >= the grace period later.
            since = rec.get('missing_since_ts')
            if since is None:
                save(missing_since_ts=now)
                return None
            if now - _to_float(since) < PENDING_MISSING_GRACE_SECONDS:
                return None
            pa_ok, pa_res = cancel_prearm_if_flat()
            prearm_detail = ({"prearm_cancelled": pa_ok, "prearm_cancel_result": pa_res}
                             if rec.get('prearm_algo_id') is not None else {})
            act("pending_dropped", pa_ok, reason="entry_not_open_no_position", kind=kind, entry_id=entry_id,
                missing_since_ts=since,
                message="Entry no longer open and no position: cancelled or expired outside the desk.", **prearm_detail)
            if not pa_ok:
                return fail("prearm_cancel", f"Entry {entry_id} gone without a position but its pre-armed stop "
                                             f"{rec.get('prearm_algo_id')} was not cancelled ({pa_res}); record kept.")
            return drop()
        if now < expires:
            return None
        if dry_run:
            return act("pending_timeout_cancel", False, kind=kind, entry_id=entry_id, expires_at_ts=expires)
        ok, res = cancel_resting_entry(sym, kind, entry_id, target_env=target_env)
        prearm_detail = {}
        if ok and rec.get('prearm_algo_id') is not None:
            pa_ok, pa_res = cancel_prearm_if_flat()
            prearm_detail = {"prearm_cancelled": pa_ok, "prearm_cancel_result": pa_res}
        act("pending_timeout_cancel", ok, kind=kind, entry_id=entry_id, expires_at_ts=expires, result=res,
            **prearm_detail)
        if not ok:
            return fail("timeout_cancel", f"Cancel of expired entry {entry_id} failed: {res}")
        if prearm_detail and not prearm_detail["prearm_cancelled"]:
            # The entry is cancelled: the next run sees it gone and retries the pre-arm cancel (pending_dropped path).
            return fail("prearm_cancel", f"Expired entry {entry_id} cancelled but its pre-armed stop "
                                         f"{rec.get('prearm_algo_id')} was not ({prearm_detail['prearm_cancel_result']}); "
                                         "record kept.")
        return drop()

    # --- Filled (fully or partially): planned Stop Loss first ----------------
    qty_str = str(position.get('positionAmt')).strip().lstrip('-')
    qty = abs(_to_float(position.get('positionAmt')))
    entry_px = _to_float(position.get('entryPrice')) or _to_float(rec.get('trigger_or_limit_price'))
    filters = get_symbol_filters(sym, target_env=target_env)
    tick = filters.get('tickSize') if filters else None
    tol = max(_to_float(tick) * 1.01, abs(sl_p) * 0.0005)

    stops, err = get_open_stop_orders(sym, exit_side, target_env=target_env)
    if err:
        return fail("stops_query", err)
    # Never loosen: the reference is the TIGHTEST existing protective stop (trailing / break-even may have moved it).
    tightest = tightest_stop(stops, is_long)
    ex_p = _trigger_price(tightest) if tightest else None
    covered = _to_float(rec.get('sl_qty'))
    looser = bool(stops) and abs(ex_p - sl_p) > tol and is_tighter_stop(sl_p, ex_p, is_long)
    mcp = uses_mcp_gateway(target_env) if stops and not looser else None
    if not stops:
        mode, target_p, old_stops = 'place', sl_p, []
    elif looser:
        mode, target_p, old_stops = 'replace', sl_p, stops   # every stop looser than plan (e.g. 2.5% orphan heal)
    elif not mcp:
        # KEYS (issues #39 / #118): the listing is reliable, so the kept (tighter) stop is resized place-then-cancel at
        # ITS price only when it does not cover the position: a closePosition stop (e.g. the issue #36 pre-arm)
        # already covers any size and is never downgraded to a fixed-quantity one.
        covers = _stop_covers_position(tightest, qty, rec)
        mode, target_p, old_stops = (None, ex_p, []) if covers else ('resize', ex_p, stops)
    elif not covered or qty > covered * 1.000001:
        # MCP (its listing folds reduceOnly into closePosition and drops the quantity, and detection errors land here):
        # kept (tighter) stop, resized place-then-cancel at ITS price for the full current size when the partial fill
        # grew, or once when its coverage is unknown (a stop not placed here, e.g. an MCP orphan heal sized
        # reduce-only for a partial fill); sl_qty is then the baseline.
        mode, target_p, old_stops = 'resize', ex_p, stops
    else:
        mode, target_p, old_stops = None, ex_p, []           # existing stop at or tighter than plan: keep it
    sl_stop = stop_summary(tightest) if tightest else None
    mark_p = _to_float(position.get('markPrice'))
    if (mode is None and rec.get('prearm_algo_id') is not None and tightest is not None
            and str(_order_id(tightest)) == str(rec.get('prearm_algo_id'))
            and str(rec.get('sl_algo_id')) != str(rec.get('prearm_algo_id'))):
        save_before_stop(sl_algo_id=rec.get('prearm_algo_id'))   # issue #36: the verified pre-arm is the stop in force

    def crossed_close(reason, placement=None):
        """The planned SL is already crossed. Issue #39: the resting entry remainder is cancelled FIRST (it cannot keep
        filling), then the position is re-read and its live size closed reduce-only at MARKET WITHOUT cancelling the
        existing stops; only once flat are leftover stops/TPs cancelled. A failed close that leaves a position with
        no stop at all (place mode) gets an orphan-heal stop (heal_orphan_position, never looser than its anchor)."""
        detail = dict(reason=reason, planned_sl_price=sl_p, mark_price=mark_p or None, quantity=qty,
                      kept_stops=[stop_summary(s) for s in stops], placement=placement)
        if dry_run:
            return act("pending_sl_crossed_close", False, **detail)
        entry_cancel_ok, entry_cancel_res = (cancel_resting_entry(sym, kind, entry_id, target_env=target_env)
                                             if entry_open else (True, None))
        live_row, live_err = _read_open_position(sym, target_env=target_env, is_long=is_long)
        if live_row is None and live_err is None:
            close = {"skipped": "position already flat on re-read"}
            flat = _wait_until_flat(sym, is_long, target_env)
        else:
            close_qty = format_order_qty(live_row.get('positionAmt') if live_row is not None else qty)
            detail.update(quantity=close_qty)
            try:
                close = send_signed_request('POST', '/fapi/v1/order', {'symbol': sym, 'side': exit_side, 'type': 'MARKET',
                                                                      'quantity': close_qty, 'reduceOnly': 'true'},
                                            target_env=target_env)
            except Exception as e:
                close = {"error": str(e)}
            flat = _market_order_accepted(close) and _wait_until_flat(sym, is_long, target_env)
        if not flat:
            heal = None
            if not stops:
                heal_row, heal_err = _read_open_position(sym, target_env=target_env, is_long=is_long)
                if heal_row is not None:
                    heal = heal_orphan_position(heal_row, target_env=target_env, planned_sl=sl_p)
                elif heal_err:
                    heal = {"success": False, "reason": f"position re-read failed ({heal_err})"}
            act("pending_sl_crossed_close", False, close=close, flat=False,
                entry_cancelled=entry_cancel_ok if entry_open else None, heal=heal, **detail)
            if not stops and not (heal or {}).get('success'):
                _report_abort_failure(sym, target_env, "pending_sl_crossed_close",
                                      f"planned SL {sl_p} crossed, close not flat ({close}), no stop kept or healed")
            return fail("sl_crossed_close", f"Planned SL {sl_p} crossed for {sym} but the reduce-only close was not "
                                            f"confirmed flat ({close}); "
                                            + (f"orphan-heal stop verified={bool((heal or {}).get('success'))}; "
                                               if not stops else "existing stop(s) kept; ")
                                            + "record kept.")
        cleanup_errors = _cancel_symbol_orders(sym, target_env)
        act("pending_sl_crossed_close", True, close=close, flat=True, entry_cancelled=entry_cancel_ok if entry_open else None,
            cleanup_errors=cleanup_errors, **detail)
        for ce in cleanup_errors:
            fail("sl_crossed_cleanup", ce)
        if not entry_cancel_ok:
            return fail("sl_crossed_entry_cancel", f"Position closed but the resting entry {entry_id} could not be "
                                                   f"cancelled ({entry_cancel_res}); record kept.")
        return drop()

    if mode in ('place', 'replace') and mark_p > 0 and ((mark_p <= sl_p) if is_long else (mark_p >= sl_p)):
        return crossed_close("mark_beyond_planned_sl")

    if mode and dry_run:
        act("pending_protect_sl", False, mode=mode, sl_price=target_p, planned_sl_price=sl_p, quantity=qty,
            old_stops=[stop_summary(s) for s in stops])
    elif mode:
        cancelled_old = []
        stop_source = None
        if mode == 'place':
            # Issue #36: a pre-armed stop that is still verified is kept; otherwise (consumed) the planned SL is placed.
            ensured = _ensure_entry_stop(sym, exit_side, target_p, prearm_algo_id=rec.get('prearm_algo_id'),
                                         tick_size=tick, target_env=target_env)
            placement, verified, stop_source = ensured["placement"], ensured["verified"], ensured["source"]
            new_stop = stop_summary(ensured["info"]) if verified else None
        else:
            rep = replace_protective_stop(sym, exit_side, target_p, qty_str, old_stops, target_env=target_env, tick_size=tick)
            placement, verified, new_stop = rep.get('placement'), bool(rep.get('success')), rep.get('new_stop')
            cancelled_old = rep.get('cancelled_old_stop_ids', [])
            for ce in rep.get('cancel_errors', []):
                fail("protect_sl_cancel_old", ce)
        act("pending_protect_sl", verified, mode=mode, sl_price=target_p, planned_sl_price=sl_p, quantity=qty,
            verified=verified, new_stop=new_stop, cancelled_old_stop_ids=cancelled_old, placement=placement,
            coverage_unknown=(mode == 'resize' and bool(mcp) and not covered), stop_source=stop_source)
        if not verified and mode in ('place', 'replace') and _is_immediate_trigger(placement):
            return crossed_close("sl_rejected_would_immediately_trigger", placement)
        if not verified and mode != 'place':
            # A verified stop already protects the position and nothing was cancelled: keep it. Never auto-destruct
            # here (the abort cancels every stop first; a failed close would leave the position with none).
            return fail("protect_sl", f"{mode} of the stop for {sym} at {target_p} unverified; existing stop(s) kept, "
                                      "record kept for the next run.")
        if not verified:
            # No stop existed and the planned SL cannot be verified: fail-safe auto-destruct.
            entry_cancel_ok, entry_cancel_res = True, None
            if entry_open:
                entry_cancel_ok, entry_cancel_res = cancel_resting_entry(sym, kind, entry_id, target_env=target_env)
            abort = emergency_abort_market_close(sym, exit_side, qty, target_env=target_env)
            log_emergency_abort(sym, direction, qty, sl_p, placement, abort, target_env)
            act("pending_abort", abort.get("confirmed"), reason="planned_sl_unverified", quantity=qty, abort_exit=abort,
                entry_cancelled=entry_cancel_ok if entry_open else None)
            if not abort.get("confirmed"):
                _report_abort_failure(sym, target_env, "pending_abort",
                                      f"planned SL unverified and auto-destruct not confirmed ({abort.get('order')})")
                return fail("abort", f"Planned SL unverified and auto-destruct NOT confirmed for {sym} ({abort.get('order')}).")
            if not entry_cancel_ok:
                return fail("abort_entry_cancel", f"Position closed but the resting entry {entry_id} could not be "
                                                  f"cancelled ({entry_cancel_res}); record kept.")
            return drop()
        sl_stop = new_stop
        save(sl_qty=qty, sl_algo_id=(new_stop or {}).get('algo_id'))

    if entry_open:
        # Partial LIMIT fill: keep the record; at expiry cancel the remainder (TPs once the entry is gone).
        if now >= expires:
            if dry_run:
                act("pending_timeout_cancel", False, kind=kind, entry_id=entry_id, expires_at_ts=expires, partial_fill=True)
            else:
                ok, res = cancel_resting_entry(sym, kind, entry_id, target_env=target_env)
                act("pending_timeout_cancel", ok, kind=kind, entry_id=entry_id, expires_at_ts=expires,
                    partial_fill=True, result=res)
                if not ok:
                    fail("timeout_cancel", f"Cancel of partially filled entry {entry_id} failed: {res}")
        return None
    if mode and dry_run:
        return None  # TPs are planned once the SL is actually verified

    # --- Entry fully done: take profits from the ACTUAL position size (idempotent) ----------
    if problems["tp"] and not rec.get('tp_placed'):
        # Issue #101: a TP on the wrong side of the entry would fill at once; the verified SL stays, no TP is placed.
        act("pending_record_mismatch", True, reason="invalid_take_profits", mismatches=problems["tp"], kind=kind,
            entry_id=entry_id, sl_stop=sl_stop)
        fail("record_mismatch", f"{sym} record {key} has invalid take profits ({'; '.join(problems['tp'])}); the "
                                "Stop Loss is in place, no TP placed, record dropped.")
        return drop()
    tp1_p, tp2_p = _to_float(rec.get('tp1_price')), _to_float(rec.get('tp2_price'))
    ids = {'tp1_order_id': rec.get('tp1_order_id'), 'tp2_order_id': rec.get('tp2_order_id')}
    if rec.get('tp1_qty') is not None and rec.get('tp2_qty') is not None:
        tp1_qty, tp2_qty = _to_float(rec['tp1_qty']), _to_float(rec['tp2_qty'])   # split fixed on the first attempt
    elif not filters:
        return fail("filters", f"Symbol filters unavailable for {sym}; TPs deferred to the next run.")
    else:
        # The TP1 leg is a LIMIT at the record's TP1 price: check its minNotional there (issue #42).
        tp1_qty, tp2_qty = split_take_profit_quantities(qty, filters, tp1_p if tp1_p > 0 else entry_px)
    # Issue #156: the fill quality flags (issue #39) are also shown in the pending_tp_placed action (guardian output).
    fill_flags = fill_quality_fields(is_long, entry_px, target_p, tp1_p, tp2_p)["fill_quality_flags"]
    if not rec.get('tp_placed'):
        need1 = tp1_qty > 0 and ids['tp1_order_id'] is None
        need2 = tp2_qty > 0 and ids['tp2_order_id'] is None
        if dry_run:
            return act("pending_tp_placed", False, tp1_price=tp1_p, tp1_qty=tp1_qty, tp2_price=tp2_p, tp2_qty=tp2_qty,
                       quantity=qty, place_tp1=need1, place_tp2=need2, fill_quality_flags=fill_flags)
        retried = ids['tp1_order_id'] is not None or ids['tp2_order_id'] is not None
        adopted = []
        if need1 or need2:
            # Issue #160: TP ids lost to a failed save must not mean duplicate TPs: adopt the reduce-only exit-side
            # LIMIT orders already resting at the TP prices. A failed read places as before (never blocks protection).
            wanted = {name: price for name, price, need in (('tp1_order_id', tp1_p, need1),
                                                            ('tp2_order_id', tp2_p, need2)) if need}
            found, read_err = find_resting_take_profits(sym, exit_side, wanted, tick,
                                                        exclude_ids=[v for v in ids.values() if v is not None],
                                                        target_env=target_env)
            if read_err:
                out.setdefault("warnings", []).append({"key": key, "symbol": sym, "stage": "tp_reconcile",
                                                       "warning": f"{read_err}; TPs placed without the duplicate "
                                                                  "check"})
            for name, order_id in found.items():
                ids[name] = order_id
                adopted.append(name)
            need1 = need1 and ids['tp1_order_id'] is None
            need2 = need2 and ids['tp2_order_id'] is None
        o1, o2 = place_take_profit_orders(sym, exit_side, tp1_p, tp2_p, tp1_qty if need1 else 0, tp2_qty if need2 else 0,
                                          target_env=target_env)
        for name, order in (('tp1_order_id', o1), ('tp2_order_id', o2)):
            if isinstance(order, dict) and 'orderId' in order and not _is_api_error(order):
                ids[name] = order['orderId']
        tp_ok = (tp1_qty <= 0 or ids['tp1_order_id'] is not None) and (tp2_qty <= 0 or ids['tp2_order_id'] is not None)
        # Persist what was placed BEFORE anything else, so a later run never duplicates a TP (lock retried, #160).
        save_retrying(tp1_qty=tp1_qty, tp2_qty=tp2_qty, tp_placed=tp_ok, **ids)
        act("pending_tp_placed", tp_ok, tp1_price=tp1_p, tp1_qty=tp1_qty, tp2_price=tp2_p, tp2_qty=tp2_qty, quantity=qty,
            retried=retried, record_dropped=tp_ok, fill_quality_flags=fill_flags, **ids,
            **({"adopted_existing": adopted} if adopted else {}))
        if not tp_ok:
            return fail("take_profit", f"TP placement failed for {sym} (tp1={o1}, tp2={o2}); SL is in place, only the "
                                       "missing TP is retried next run.")
    if dry_run:
        return None
    if not rec.get('audit_done'):
        record = {
            'timestamp': int(time.time()),
            'symbol': sym,
            'direction': direction,
            'leverage': rec.get('leverage'),
            'entry_price': entry_px,
            'total_qty': qty,
            'sl_price': target_p,
            'sl_verified': True,
            'sl_algo_id': (sl_stop or {}).get('algo_id'),
            'tp1_price': tp1_p,
            'tp2_price': tp2_p,
            'tp1_qty': tp1_qty,
            'tp2_qty': tp2_qty,
            'is_yolo': bool(rec.get('is_yolo')),
            'entry_order_id': rec.get('entry_id'),
            'sl_order': sl_stop,
            'tp1_order_id': ids['tp1_order_id'],
            'tp2_order_id': ids['tp2_order_id'],
            'target_env': target_env,
            'pending_entry_key': key,
        }
        record.update(score_audit_fields(rec.get('score_meta')))
        if isinstance(rec.get('daily_loss_gate'), dict):
            record['daily_loss_gate'] = rec['daily_loss_gate']  # issue #207: state at placement
        record.update(fill_quality_fields(is_long, entry_px, target_p, tp1_p, tp2_p))
        try:
            append_trade_audit_record(record, rec.get('margin_usdt'))
        except Exception as e:
            return fail("audit", f"Audit append failed ({e}); record kept, only the audit/drop is retried.")
        save_retrying(audit_done=True)   # a lost flag would re-append the audit record next run (issue #160)
    return drop()


def execute_complete_trade(
    symbol,
    direction,
    leverage=3,
    margin_usdt=100.0,
    sl_price=None,
    tp1_price=None,
    tp2_price=None,
    target_env=None,
    trigger_price=None,
    order_type='MARKET',
    limit_price=None,
    bypass_delta_gate=False,
    is_yolo=False,
    bypass_eval_gate=False,
    confirmed=False
):
    target_env = resolve_env(target_env)
    is_yolo = (is_yolo is True) or (str(is_yolo).lower() in ['true', '1', 'yes'])
    confirmed = (confirmed is True) or (str(confirmed).lower() in ['true', '1', 'yes'])
    is_prod = str(target_env).lower() != 'testnet'
    if is_prod:
        cfg = load_env(target_env=target_env)
        if str(cfg.get('LIVE_TRADING_ARMED', '')).strip().lower() != 'true':
            return {
                "success": False,
                "hard_gate_rejection": True,
                "error": "FAIL-CLOSED: Environment is PROD but LIVE_TRADING_ARMED is not 'true'. Live trading execution is disarmed."
            }

    # 0. Clean-room evaluation dossier gate (before ANY write: margin type, leverage or orders)
    bypass_eval_gate = (bypass_eval_gate is True) or (str(bypass_eval_gate).lower() in ['true', '1', 'yes'])
    eval_ok, eval_reason, _eval_cand = enforce_evaluation_dossier(
        symbol, direction, target_env=target_env, bypass_eval_gate=bypass_eval_gate, confirmed=confirmed,
        is_yolo=is_yolo
    )
    if not eval_ok:
        return {"success": False, "hard_gate_rejection": True, "evaluation_gate_rejection": True, "error": eval_reason}
    score_meta = build_score_meta(_eval_cand, symbol, direction)  # audit only (issue #202)

    try:
        import quant_risk_engine as qre
        account_equity = qre.get_account_equity(target_env)
    except Exception as e:
        if is_prod:
            return {"success": False, "hard_gate_rejection": True,
                    "error": f"FAIL-CLOSED: Cannot verify account equity for PROD ({e}). Order blocked."}
        account_equity = 10000.0  # Safe testnet sandbox fallback

    profile_unreadable = False
    try:
        import user_profile as up
        prof = up.load_user_profile()
        max_margin_ratio = float(prof.get("max_margin_ratio", 0.30))
    except Exception as e:
        prof = {}
        max_margin_ratio = 0.30
        # Issue #207 round 4: the defaults apply (never a block), but the fallback is visible
        profile_unreadable = True
        print(f"⚠️ user profile unreadable ({type(e).__name__}: {e}); default limits applied (Daily Loss Gate, "
              "sizing).", file=sys.stderr)

    # Dynamic margin scaling: if margin_usdt is None or default 100.0, scale dynamically
    margin_defaulted = margin_usdt is None or margin_usdt == 100.0
    if margin_defaulted:
        if is_yolo:
            try:
                import user_profile as up
                margin_usdt = up.get_yolo_margin(target_env)
            except Exception:
                margin_usdt = 10.0
        else:
            margin_usdt = round(min(100.0, max(5.0, account_equity * max_margin_ratio * 0.5)), 2)

    max_prod_margin = account_equity * max_margin_ratio
    if is_prod and margin_usdt > max_prod_margin:
        return {"success": False, "hard_gate_rejection": True,
                "error": f"GUARDRAIL: Margin of {margin_usdt} USDT exceeds max cap of {max_prod_margin:.2f} USDT ({max_margin_ratio*100:.0f}% equity) on REAL network."}

    filters = get_symbol_filters(symbol, target_env=target_env)
    if not filters:
        return {"success": False, "error": f"Filters not found for {symbol}"}

    # 1. Fetch current price first to validate filters and gates
    ticker_res = send_signed_request('GET', '/fapi/v1/ticker/price', {'symbol': symbol}, target_env=target_env)
    cur_price = float(ticker_res.get('price', 0))
    if cur_price <= 0:
        return {"success": False, "error": "Could not fetch current market price"}

    is_long = str(direction).upper() == 'LONG'
    entry_side = 'BUY' if is_long else 'SELL'
    exit_side = 'SELL' if is_long else 'BUY'

    # Effective entry (Issue #22): worst-case fill reference of the order that will actually be sent,
    # using the same rounded prices submitted below. Sizing and PROD gates are measured from it.
    #   LIMIT -> rounded limit (a buy fills at <= limit, a sell at >= limit);
    #   STOP_MARKET with trigger not breached -> rounded trigger;
    #   breached trigger (falls through to MARKET) or plain MARKET -> current price.
    trigger_p = None
    trigger_breached = True
    if trigger_price is not None and trigger_price > 0:
        trigger_p = round_price(trigger_price, filters['tickSize'], filters['precision_price'])
        trigger_breached = (cur_price >= trigger_p) if is_long else (cur_price <= trigger_p)
    effective_entry = cur_price
    if str(order_type).upper() == 'LIMIT' and limit_price:
        effective_entry = float(round_price(limit_price, filters['tickSize'], filters['precision_price']))
    elif str(order_type).upper() == 'STOP_MARKET' and trigger_p is not None and not trigger_breached:
        effective_entry = float(trigger_p)

    # Fallback / default SL and TP calculations if not provided or 0 (anchored at the effective entry)
    if sl_price is None or float(sl_price) <= 0:
        sl_price = effective_entry * (1.0 - 0.02) if is_long else effective_entry * (1.0 + 0.02)
    if tp1_price is None or float(tp1_price) <= 0:
        tp1_price = effective_entry * (1.0 + 0.03) if is_long else effective_entry * (1.0 - 0.03)
    if tp2_price is None or float(tp2_price) <= 0:
        tp2_price = effective_entry * (1.0 + 0.06) if is_long else effective_entry * (1.0 - 0.06)

    # Round SL and TP prices to the tick BEFORE the gates (issue #42), so the gates check exactly what is submitted.
    sl_p = round_price(sl_price, filters['tickSize'], filters['precision_price'])
    tp1_p = round_price(tp1_price, filters['tickSize'], filters['precision_price'])
    tp2_p = round_price(tp2_price, filters['tickSize'], filters['precision_price'])
    # Fail closed if rounding collapsed a level onto the entry or onto the wrong side of it (checked before any write).
    if sl_p <= 0 or (sl_p >= effective_entry if is_long else sl_p <= effective_entry):
        return {"success": False, "error": (f"Rounded Stop Loss {sl_p} (from {sl_price}) is not on the loss side of the "
                                            f"effective entry {effective_entry} for a {'LONG' if is_long else 'SHORT'}. "
                                            "Execution aborted (fail-closed).")}
    if tp1_p <= 0 or (tp1_p <= effective_entry if is_long else tp1_p >= effective_entry):
        return {"success": False, "error": (f"Rounded TP1 {tp1_p} (from {tp1_price}) is not on the profit side of the "
                                            f"effective entry {effective_entry} for a {'LONG' if is_long else 'SHORT'}. "
                                            "Execution aborted (fail-closed).")}
    # A wrong-side TP2 would rest as a reduce-only LIMIT that fills at once (issue #141).
    if tp2_p <= 0 or (tp2_p <= effective_entry if is_long else tp2_p >= effective_entry):
        return {"success": False, "error": (f"Rounded TP2 {tp2_p} (from {tp2_price}) is not on the profit side of the "
                                            f"effective entry {effective_entry} for a {'LONG' if is_long else 'SHORT'}. "
                                            "Execution aborted (fail-closed).")}

    # 1b. Pending resting entries (Issue #33, PROD): no new entry of any type on a symbol with a pending resting
    # entry (or an unreadable registry). An untriggered STOP_MARKET or a LIMIT entry rests on the book and gets its TPs
    # (and its SL unless pre-armed, issue #36) on fill (--protect-pending / position guardian loop). Checked before
    # any write.
    if is_prod:
        pend_ok, pend_err = check_pending_entry_conflict(symbol, target_env)
        if not pend_ok:
            return {"success": False, "hard_gate_rejection": True, "error": pend_err}
    # 1b'. Live exchange snapshot (issue #101, PROD): positionRisk + openAlgoOrders + openOrders (all symbols), fetched
    # ONCE for this order attempt and shared by Gate 0A (1c and step 4), Gate 1 and 1d. A read failure rejects.
    live_snapshot = None
    if is_prod:
        live_snapshot, live_err = fetch_live_gate_snapshot(target_env)
        if live_err:
            return {"success": False, "hard_gate_rejection": True,
                    "error": (f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — cannot read the live exchange state for "
                              f"the PROD gates ({live_err}). Order blocked.")}
        if margin_defaulted and not is_yolo:
            # Issue #126: a defaulted standard margin is sized on the Gate 2 equity, min(wallet balance, balance +
            # unrealized PnL of the snapshot's positions), then the per-position cap is re-applied on it (the qty is
            # rounded from it below), so sizing and Gate 2 agree. A missing uPnL rejects, as in Gate 2.
            try:
                unrealized = unrealized_pnl_total(live_snapshot["exposure"])
            except (ValueError, KeyError, TypeError) as e:
                return {"success": False, "hard_gate_rejection": True,
                        "error": (f"MECHANICAL HARD GATE REJECTION: FAIL-CLOSED — cannot read the unrealized PnL of "
                                  f"the open positions to size the order ({e}). Order blocked.")}
            sizing_equity = min(account_equity, account_equity + unrealized)
            margin_usdt = round(min(100.0, max(5.0, sizing_equity * max_margin_ratio * 0.5)), 2)
            sizing_cap = sizing_equity * max_margin_ratio
            if margin_usdt > sizing_cap:
                return {"success": False, "hard_gate_rejection": True,
                        "error": (f"GUARDRAIL: Margin of {margin_usdt} USDT exceeds max cap of {sizing_cap:.2f} USDT "
                                  f"({max_margin_ratio*100:.0f}% of equity {sizing_equity:.2f} = min(wallet balance, "
                                  "balance + unrealized PnL)) on REAL network.")}
    # 1b''. Daily Loss Gate (issue #207): opening orders only, before any write. PROD reads today's fills live and
    # fails closed; it reuses the snapshot's open positions (TP1 partials of open trades are not closed trades).
    # Effective YOLO = the --is-yolo flag or a YOLO dossier candidate.
    daily_ok, daily_reason, daily_state = check_daily_loss_gate(
        target_env, is_yolo or _dossier_candidate_is_yolo(_eval_cand), prof, account_equity,
        open_positions=([(p["symbol"], p["side"]) for p in live_snapshot["exposure"]["active_positions"]]
                        if live_snapshot else None))
    if profile_unreadable and isinstance(daily_state, dict):
        daily_state = dict(daily_state, profile_unreadable=True)  # round 4: carried into the audit record
    if not daily_ok:
        return {"success": False, "hard_gate_rejection": True, "daily_loss_gate_rejection": True,
                "error": daily_reason, "daily_loss_gate": daily_state}
    # 1c. Max open positions (Gate 0A, Issue #38): open positions + pending resting entries, checked before any write
    # (check_mechanical_gates re-checks it after sizing).
    slots_ok, slots_err = check_max_open_positions(prof, target_env, live=live_snapshot)
    if not slots_ok:
        return {"success": False, "hard_gate_rejection": True, "error": slots_err}
    # 1d. Unregistered resting entries (Issue #46, PROD): a missing registry reads as empty, so every opening order
    # resting on the exchange must have its logs/pending_entries.json record (live snapshot listings, fail closed).
    if is_prod:
        unreg_ok, unreg_err = check_unregistered_resting_entries(target_env, live=live_snapshot)
        if not unreg_ok:
            return {"success": False, "hard_gate_rejection": True, "error": unreg_err}
    resting_kind = None
    if str(order_type).upper() == 'STOP_MARKET' and trigger_p is not None and not trigger_breached:
        resting_kind = 'STOP_MARKET'
    elif str(order_type).upper() == 'LIMIT' and limit_price and (trigger_p is None or trigger_breached):
        resting_kind = 'LIMIT'
    if resting_kind and is_prod:
        rest_ok, rest_err = check_resting_entry_gates(symbol, target_env)
        if not rest_ok:
            return {"success": False, "hard_gate_rejection": True, "error": rest_err}

    # 2. Configure Isolated margin and leverage first (Fail-Closed & Auto-Clamp for Subaccounts)
    setup_res = setup_margin_and_leverage(symbol, leverage, target_env=target_env)
    confirmed_leverage = leverage
    if isinstance(setup_res, tuple):
        if len(setup_res) >= 3:
            lev_res, margin_res, confirmed_leverage = setup_res[:3]
        elif len(setup_res) == 2:
            lev_res, margin_res = setup_res
            if isinstance(lev_res, dict) and 'leverage' in lev_res:
                try:
                    confirmed_leverage = int(lev_res['leverage'])
                except Exception:
                    pass
        # Fail-closed check: ISOLATED margin must be confirmed (only -4046 "no need to change" is tolerated)
        margin_ok, margin_reason = margin_type_isolated_confirmed(margin_res)
        if not margin_ok:
            return {"success": False, "error": f"{margin_reason} Execution aborted (fail-closed): isolated margin is mandatory."}
        # Fail-closed check: if setting leverage failed completely on Binance
        if lev_res is None:
            return {"success": False, "error": "Leverage was not configured on Binance. Execution aborted (fail-closed)."}
        if isinstance(lev_res, dict) and (lev_res.get('isError') or ('code' in lev_res and lev_res.get('code') < 0)):
            err_msg = lev_res.get('error') or lev_res.get('message') or str(lev_res)
            return {"success": False, "error": f"Failed to configure leverage on Binance ({err_msg}). Execution aborted (fail-closed)."}

    effective_leverage = int(confirmed_leverage) if confirmed_leverage else leverage

    # 3. Calculate exact token quantity using verified effective leverage, sized at the effective entry
    notional_target = margin_usdt * effective_leverage
    raw_qty = notional_target / effective_entry
    total_qty = round_step(raw_qty, filters['stepSize'], filters['precision_qty'])
    min_notional = filters.get('minNotional', 5.0)
    # Binance does not document whether an untriggered conditional entry's notional is checked at the trigger or at
    # the mark/last price (issue #42): use the lower of the two, which satisfies both readings.
    notional_ref = min(effective_entry, cur_price) if resting_kind == 'STOP_MARKET' else effective_entry
    if total_qty * notional_ref < min_notional:
        bumped_qty = round_step(total_qty + filters['stepSize'], filters['stepSize'], filters['precision_qty'])
        if bumped_qty * notional_ref >= min_notional:
            total_qty = bumped_qty
    if total_qty < filters['minQty']:
        return {"success": False, "error": f"Quantity {total_qty} lower than minimum allowed {filters['minQty']}"}
    # Still below minNotional after the one-step bump: reject locally instead of a Binance -4164 (issue #141).
    if total_qty * notional_ref < min_notional:
        return {"success": False, "error": (f"Entry notional {total_qty * notional_ref:.4f} USDT ({total_qty} x "
                                            f"{notional_ref}) is below the exchange minNotional {min_notional} USDT "
                                            "after a one-step size bump. Increase the margin. Execution aborted "
                                            "(fail-closed).")}

    # 4. MECHANICAL HARD GATES VERIFICATION (incl. liquidation gate with the confirmed effective leverage)
    liq_entry_price = effective_entry
    mmr, maint_amount, mmr_source = get_maint_margin_bracket(symbol, total_qty * liq_entry_price, target_env=target_env)
    gate_ok, gate_err = check_mechanical_gates(
        direction, cur_price, sl_p, tp1_p, total_qty, effective_leverage,
        bypass_delta_gate=bypass_delta_gate, target_env=target_env, is_yolo=is_yolo,
        maint_margin_ratio=mmr, maint_amount=maint_amount, mmr_source=mmr_source,
        liq_entry_price=liq_entry_price, entry_price=effective_entry, live_snapshot=live_snapshot,
        exchange_ticks={symbol: filters.get('tickSize')}
    )
    if not gate_ok:
        return {"success": False, "hard_gate_rejection": True, "error": gate_err}

    # 5. Split TPs asymmetrically (30% TP1 / 70% TP2) to preserve positive right-tail skewness
    # and prevent premature profit truncation. The TP1 leg is a LIMIT at tp1_p, so its minNotional is checked there.
    tp1_qty, tp2_qty = split_take_profit_quantities(total_qty, filters, tp1_p)

    def placement_loss_cap():
        """Issue #156 (PROD): the Gate 2 loss cap at placement (pure monetary_loss_cap on the values Gate 2 used:
        equity, profile, the snapshot's uPnL, effective entry, qty, leverage), stored in the record. None when it
        cannot be computed (the record then has no stored cap: no drift tolerance)."""
        if not is_prod:
            return None
        try:
            unrealized = unrealized_pnl_total(live_snapshot["exposure"]) if live_snapshot else 0.0
            cap = monetary_loss_cap(float(account_equity), prof, is_testnet=False, is_yolo=is_yolo,
                                    ref=effective_entry, total_qty=total_qty, leverage=effective_leverage,
                                    unrealized=unrealized)[0]
            return round(cap, 8) if cap > 0 else None
        except Exception:
            return None

    def register_or_cancel(kind, entry_id, price, prearm=None):
        """Records the resting entry for post-fill protection; if that fails the entry and its pre-armed stop are
        cancelled (fail closed)."""
        try:
            key, rec = register_resting_entry(
                kind, entry_id, symbol, direction, entry_side, exit_side, target_env, price, total_qty,
                sl_p, tp1_p, tp2_p, effective_leverage, is_yolo, margin_usdt, prearm=prearm,
                tick_size=filters.get('tickSize'), step_size=filters.get('stepSize'),
                gate2_loss_cap_usdt=placement_loss_cap(), score_meta=score_meta, daily_loss_gate=daily_state)
            return key, rec, None
        except Exception as e:
            cancelled, cancel_res = cancel_resting_entry(symbol, kind, entry_id, target_env=target_env)
            state = "the entry was cancelled" if cancelled else "CANCEL ALSO FAILED: cancel it manually now"
            failure = {
                "success": False,
                "pending_registry_failure": True,
                "orderId": entry_id,
                "entry_cancelled": cancelled,
                "cancel_result": cancel_res,
                "error": (f"FAIL-CLOSED: {kind} entry {entry_id} for {symbol} was placed but could not be registered "
                          f"for post-fill protection in logs/pending_entries.json ({e}); {state}."),
            }
            prearm_id = (prearm or {}).get('prearm_algo_id')
            if prearm_id is not None:
                # Only once the entry is cancelled and no position exists: otherwise the pre-arm protects a fill.
                pos_row, pos_err = _read_open_position(symbol, target_env=target_env, is_long=is_long)
                if cancelled and pos_row is None and pos_err is None:
                    pa_ok, pa_res = cancel_prearmed_stop(symbol, exit_side, prearm_id, target_env=target_env)
                else:
                    pa_ok, pa_res = False, ("kept: the entry is not cancelled or a position may exist "
                                            f"(position read: {pos_err or (pos_row or {}).get('positionAmt')})")
                failure.update(prearm_cancelled=pa_ok, prearm_cancel_result=pa_res)
                if not pa_ok:
                    failure["error"] += f" The pre-armed stop {prearm_id} was not cancelled ({pa_res})."
            return None, None, failure

    # 7. Technical Trigger Validation (Confirmation breakout)
    # (trigger_p / trigger_breached were computed with the effective entry, before sizing and gates)
    if trigger_p is not None:
        if not trigger_breached:
            if order_type.upper() == 'STOP_MARKET':
                # Conditional orders must use the Algo Order API (POST /fapi/v1/order rejects them with -4120).
                # closePosition='false' + quantity: an opening order, never reduce-only.
                entry_params = {
                    'algoType': 'CONDITIONAL',
                    'symbol': symbol,
                    'side': entry_side,
                    'type': 'STOP_MARKET',
                    'triggerPrice': trigger_p,
                    'quantity': total_qty,
                    'closePosition': 'false',
                    'workingType': 'CONTRACT_PRICE'
                }
                cond_order = send_signed_request('POST', '/fapi/v1/algoOrder', entry_params, target_env=target_env)
                order_id = _order_id(cond_order) if isinstance(cond_order, dict) and not _is_api_error(cond_order) else None
                if order_id:
                    # Issue #36: pre-arm the planned SL after the entry (the entry stays the first algo order sent).
                    prearm = prearm_resting_entry_stop(symbol, exit_side, sl_p, cur_price, target_env=target_env,
                                                       tick_size=filters.get('tickSize'))
                    key, rec, failure = register_or_cancel('STOP_MARKET', order_id, trigger_p, prearm)
                    if failure:
                        return failure
                    result = {
                        "success": True,
                        "conditional_entry": True,
                        "orderId": order_id,
                        "symbol": symbol,
                        "direction": str(direction).upper(),
                        "trigger_price": trigger_p,
                        "cur_price": cur_price,
                        "quantity": total_qty,
                        "pending_entry_key": key,
                        "expires_at_ts": rec['expires_at_ts'],
                        "prearm_status": prearm['prearm_status'],
                        "prearm_algo_id": prearm.get('prearm_algo_id'),
                        "message": (f"Conditional STOP_MARKET entry placed at {trigger_p} (algo order {order_id}). "
                                    + _prearm_note(prearm, sl_p) +
                                    "Its TPs (and the SL when not pre-armed) are placed on fill by "
                                    "`execute_futures_trade.py --protect-pending` / the position guardian loop, which also "
                                    "verifies the stop; unfilled after "
                                    f"{PENDING_ENTRY_TIMEOUT_SECONDS // 60} min it is cancelled.")
                    }
                    # Issue #157: a rejected / unverified pre-arm is reported (the entry is kept).
                    anomaly = _prearm_anomaly(prearm)
                    if anomaly:
                        result["prearm_anomaly"] = anomaly
                        _report_prearm_anomaly(symbol, target_env, 'STOP_MARKET', order_id, anomaly)
                    return result
                else:
                    return {"success": False, "error": f"Failed to place conditional order: {cond_order}"}
            else:
                return {
                    "success": False,
                    "error": f"TRIGGER NOT REACHED: Current price {cur_price} has not broken trigger level {trigger_p} ({direction}). Await confirmation or specify order_type='STOP_MARKET'."
                }

    # 8. Execute Entry Order (MARKET or LIMIT)
    if order_type.upper() == 'LIMIT' and limit_price:
        lim_p = round_price(limit_price, filters['tickSize'], filters['precision_price'])
        entry_params = {
            'symbol': symbol,
            'side': entry_side,
            'type': 'LIMIT',
            'timeInForce': 'GTC',
            'price': lim_p,
            'quantity': total_qty
        }
    else:
        entry_params = {
            'symbol': symbol,
            'side': entry_side,
            'type': 'MARKET',
            'quantity': total_qty
        }

    entry_order = send_signed_request('POST', '/fapi/v1/order', entry_params, target_env=target_env)
    if not isinstance(entry_order, dict) or 'orderId' not in entry_order:
        return {"success": False, "error": f"Entry order failed: {entry_order}"}

    # Protection against premature reduceOnly orders on resting LIMIT orders (Finding 9). A PARTIALLY_FILLED LIMIT
    # still rests too: it is registered and its partial position gets the planned SL right away (TPs once filled).
    entry_status = str(entry_order.get('status', '')).upper()
    if order_type.upper() == 'LIMIT' and entry_status in ('NEW', 'PARTIALLY_FILLED'):
        # Issue #36: a resting (NEW) LIMIT gets its SL pre-armed after the entry; a PARTIALLY_FILLED one is protected
        # below from executedQty (_ensure_entry_stop).
        prearm = (prearm_resting_entry_stop(symbol, exit_side, sl_p, cur_price, target_env=target_env,
                                            tick_size=filters.get('tickSize'))
                  if entry_status == 'NEW' else None)
        key, rec, failure = register_or_cancel('LIMIT', entry_order.get('orderId'), lim_p, prearm)
        if failure:
            return failure
        result = {
            "success": True,
            "pending_limit_entry": True,
            "orderId": entry_order.get('orderId'),
            "symbol": symbol,
            "direction": str(direction).upper(),
            "limit_price": lim_p,
            "quantity": total_qty,
            "status": entry_status,
            "pending_entry_key": key,
            "expires_at_ts": rec['expires_at_ts'],
            "message": (f"LIMIT order placed at {lim_p} (order ID: {entry_order.get('orderId')}). "
                        + (_prearm_note(prearm, sl_p) if prearm else "") +
                        "TP orders (and the SL when not pre-armed) deferred until fill "
                        "(prevents -2022) and placed on fill by `execute_futures_trade.py --protect-pending` / the position "
                        f"guardian loop, which also verifies the stop; unfilled after {PENDING_ENTRY_TIMEOUT_SECONDS // 60} "
                        "min it is cancelled.")
        }
        if prearm:
            result.update(prearm_status=prearm['prearm_status'], prearm_algo_id=prearm.get('prearm_algo_id'))
            anomaly = _prearm_anomaly(prearm)   # issue #157: reported, the entry is kept
            if anomaly:
                result["prearm_anomaly"] = anomaly
                _report_prearm_anomaly(symbol, target_env, 'LIMIT', entry_order.get('orderId'), anomaly)
        if entry_status == 'PARTIALLY_FILLED':
            entry_id = entry_order.get('orderId')
            head = f"LIMIT order PARTIALLY_FILLED at {lim_p} (order ID: {entry_id}). "
            exec_qty = _to_float(entry_order.get('executedQty'))
            if exec_qty > 0:
                # Protect the filled part NOW from the entry response (no dependency on positionRisk visibility):
                # closePosition with HMAC keys; quantity-based reduce-only via the MCP gateway (which needs a quantity).
                ensured = _ensure_entry_stop(symbol, exit_side, sl_p,
                                             quantity=exec_qty if uses_mcp_gateway(target_env) else None,
                                             tick_size=filters.get('tickSize'), target_env=target_env)
                sl_order, sl_ok, sl_info = ensured["placement"], ensured["verified"], ensured["info"]
                result["partial_fill_protection"] = {"executed_qty": exec_qty, "sl_order": sl_order, "verified": sl_ok,
                                                     "stop_source": ensured["source"],
                                                     "new_stop": stop_summary(sl_info) if sl_ok else None}
                result["partial_fill_protected"] = sl_ok
                if not sl_ok:
                    # Fail-safe auto-destruct: cancel the resting remainder, then close the LIVE size (issue #39: units
                    # filled between the entry response and the cancel are included); executedQty if the re-read fails.
                    entry_cancel_ok, entry_cancel_res = cancel_resting_entry(symbol, 'LIMIT', entry_id, target_env=target_env)
                    live_row, live_err = _read_open_position(symbol, target_env=target_env, is_long=is_long)
                    close_qty = (format_order_qty(live_row.get('positionAmt'))
                                 if live_row is not None and live_err is None else exec_qty)
                    abort_exit = emergency_abort_market_close(symbol, exit_side, close_qty, target_env=target_env)
                    log_emergency_abort(symbol, direction, close_qty, sl_p, sl_order, abort_exit, target_env)
                    if not abort_exit.get("confirmed"):
                        _report_abort_failure(symbol, target_env, "partial_fill_abort",
                                              f"partial-fill SL unverified and MARKET close not confirmed ({abort_exit})")
                    if abort_exit.get("confirmed") and entry_cancel_ok:
                        try:
                            update_pending_entries(lambda entries: entries.pop(key, None))
                        except Exception:
                            pass  # an unfilled leftover record is dropped by --protect-pending after the grace period
                    result.update(success=False, emergency_abort=True, abort_exit=abort_exit, entry_cancelled=entry_cancel_ok,
                                  error=("CRITICAL FAIL-SAFE TRIGGERED: the Stop Loss of the partially filled LIMIT could "
                                         f"not be confirmed ({sl_order}); remainder cancel ok={entry_cancel_ok}, MARKET close "
                                         f"confirmed={bool(abort_exit.get('confirmed'))}."))
                    return result
                try:
                    update_pending_entries(lambda entries: entries[key].update(
                        sl_qty=exec_qty, sl_algo_id=stop_summary(sl_info).get('algo_id')) if key in entries else None)
                except Exception as e:
                    result["warnings"] = [f"sl_qty not saved ({e}); --protect-pending re-baselines the stop size."]
                result["message"] = (head + f"The planned SL protects the filled {exec_qty}; TPs are placed once the entry "
                                     "is fully filled (--protect-pending / guardian loop).")
                return result
            # Fallback (no executedQty in the response): protect from positionRisk, with progressive retries.
            prot = None
            for delay in (0.0,) + tuple(STOP_VERIFY_RETRY_DELAYS):
                if delay:
                    time.sleep(delay)
                prot = protect_pending_entries(target_env=target_env, keys=[key])
                if prot.get("errors") or prot.get("actions"):
                    break
            actions = prot.get("actions", []) if isinstance(prot, dict) else []
            result["partial_fill_protection"] = prot
            result["partial_fill_protected"] = any(a.get("type") == "pending_protect_sl" and a.get("success") for a in actions)
            tps_placed = any(a.get("type") == "pending_tp_placed" and a.get("success") for a in actions)
            if any(a.get("type") in ("pending_abort", "pending_sl_crossed_close") for a in actions):
                result.update(success=False, emergency_abort=True,
                              error=("CRITICAL FAIL-SAFE TRIGGERED: the Stop Loss of the partially filled LIMIT could not be "
                                     "kept; the position was closed at MARKET (see partial_fill_protection)."))
            else:
                result["message"] = (head
                                     + ("The planned SL protects the position; " if result["partial_fill_protected"] else
                                        "WARNING: the partial position is not protected yet (executedQty missing and no "
                                        "position visible; the guardian loop retries); ")
                                     + ("the entry already filled completely and the TPs were placed."
                                        if tps_placed else "TPs are placed once the entry is fully filled."))
        return result

    actual_entry_price = float(entry_order.get('avgPrice', cur_price))
    if actual_entry_price == 0:
        actual_entry_price = cur_price

    # ATOMIC POST-ENTRY HARDENING: Position is now live on the books.
    # Enclose in try/except to guarantee emergency auto-destruct on ANY failure.
    try:
        # 9. Execute Hard Stop Loss (Algo Order, closePosition=true, reduceOnly=true)
        # Ids of the protective stops that exist BEFORE this placement (one read, no retry delay before the stop is
        # placed); None = unknown (read failed).
        pre_stops, pre_err = get_open_stop_orders(symbol, exit_side, target_env=target_env)
        pre_ids = None if pre_err else {str(_order_id(s)) for s in pre_stops}
        sl_order = place_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)

        def verify_placed_stop():
            # Issue #157: verified by the algo id of THIS placement only (never by price), so a leftover stop on the
            # symbol (e.g. an old pre-arm at the same price) cannot confirm it. A placement without an id (lost
            # response, error, -4130) is verified only by a stop listed now that was NOT listed before the placement
            # (pre_ids) at sl_p within one tick: this trade's own stop whose response was lost. A pre-existing stop
            # never counts; an unknown pre-placement listing or no such stop is unverified and auto-destructs
            # (accepted, fail closed).
            placed_id = _order_id(sl_order) if isinstance(sl_order, dict) and not _is_api_error(sl_order) else None
            if placed_id is not None:
                return verify_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env, algo_id=placed_id)
            if pre_ids is None:
                return False, None
            now_stops, now_err = get_open_stop_orders(symbol, exit_side, target_env=target_env)
            if now_err:
                return False, None
            tol = _to_float(filters.get('tickSize')) * 1.01 or abs(float(sl_p)) * 0.0005
            for s in now_stops:
                if str(_order_id(s)) not in pre_ids and abs(_trigger_price(s) - float(sl_p)) <= tol:
                    return True, s
            return False, None

        sl_verified, sl_info = verify_placed_stop()

        # Progressive retries (up to 3 attempts in ~2.8s) to absorb Mainnet indexing latency
        if not sl_verified:
            for retry_delay in [0.8, 1.0, 1.2]:
                time.sleep(retry_delay)
                if isinstance(sl_order, dict) and ("error" in sl_order or "code" in sl_order):
                    sl_order = place_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)
                sl_verified, sl_info = verify_placed_stop()
                if sl_verified:
                    break

        # ATOMIC AUTO-DESTRUCT / FAIL-SAFE PROTOCOL:
        # If Stop Loss is NOT verified after retries, ABORT IMMEDIATELY
        if not sl_verified:
            abort_exit = emergency_abort_market_close(symbol, exit_side, total_qty, target_env=target_env)
            log_emergency_abort(symbol, direction, total_qty, sl_p, sl_order, abort_exit, target_env)
            leftover = _is_existing_close_position_stop_rejection(sl_order)
            if leftover:
                _report_leftover_stop_abort(symbol, target_env, sl_order, abort_exit)
            return {
                "success": False,
                "emergency_abort": True,
                "symbol": symbol,
                "error": (f"CRITICAL FAIL-SAFE TRIGGERED: Stop Loss could not be confirmed after 3 attempts ({sl_order}). "
                          + ("Binance -4130: a closePosition stop already exists on the symbol, probably a leftover "
                             "stop (e.g. an old pre-armed stop) that cannot be verified as this entry's stop. "
                             if leftover else "")
                          + "Position closed at MARKET immediately to eliminate unhedged exposure."),
                "abort_exit": abort_exit
            }

        # 10-11. TP1 (LIMIT, 30% position) and TP2 (LIMIT, 70% remaining), both Reduce-Only
        tp1_order, tp2_order = place_take_profit_orders(symbol, exit_side, tp1_p, tp2_p, tp1_qty, tp2_qty, target_env=target_env)

        # Log to local audit ledger with canonical provenance and atomic writing
        record = {
            'timestamp': int(time.time()),
            'symbol': symbol,
            'direction': str(direction).upper(),
            'leverage': effective_leverage,
            'entry_price': actual_entry_price,
            'total_qty': total_qty,
            'sl_price': sl_p,
            'sl_verified': True,
            'sl_algo_id': sl_info.get('algoId') if sl_info else (sl_order.get('algoId') if isinstance(sl_order, dict) else None),
            'tp1_price': tp1_p,
            'tp2_price': tp2_p,
            'tp1_qty': tp1_qty,
            'tp2_qty': tp2_qty,
            'is_yolo': is_yolo,
            'entry_order_id': entry_order.get('orderId'),
            'sl_order': sl_order,
            'tp1_order_id': tp1_order.get('orderId') if isinstance(tp1_order, dict) else None,
            'tp2_order_id': tp2_order.get('orderId') if isinstance(tp2_order, dict) else None,
            'target_env': target_env
        }
        record.update(score_audit_fields(score_meta))
        record['daily_loss_gate'] = daily_state  # issue #207: the gate state this entry passed
        if profile_unreadable:
            record['profile_unreadable'] = True  # round 4: sized and gated on the default profile
        append_trade_audit_record(record, margin_usdt)

        return {
            "success": True,
            "symbol": symbol,
            "direction": str(direction).upper(),
            "leverage": effective_leverage,
            "entry_price": actual_entry_price,
            "total_qty": total_qty,
            "entry_order_id": entry_order.get('orderId'),
            "sl_price": sl_p,
            "sl_algo_order": sl_info or sl_order,
            "tp1_price": tp1_p,
            "tp1_qty": tp1_qty,
            "tp1_order_id": tp1_order.get('orderId') if isinstance(tp1_order, dict) else None,
            "tp2_price": tp2_p,
            "tp2_qty": tp2_qty,
            "tp2_order_id": tp2_order.get('orderId') if isinstance(tp2_order, dict) else None,
            "notional": total_qty * actual_entry_price,
            "real_margin": (total_qty * actual_entry_price) / effective_leverage
        }
    except Exception as exc:
        abort_exit = emergency_abort_market_close(symbol, exit_side, total_qty, target_env=target_env)
        log_emergency_abort(symbol, direction, total_qty, sl_p, str(exc), abort_exit, target_env)
        return {
            "success": False,
            "emergency_abort": True,
            "symbol": symbol,
            "error": f"CRITICAL POST-ENTRY EXCEPTION ({exc}). Emergency auto-destruct executed at MARKET.",
            "abort_exit": abort_exit
        }

def move_sl_to_breakeven(symbol, target_env=None, force=False, is_yolo=None, dry_run=False, base_dir=None):
    """
    Moves the protective stop to True Net Break-Even (entry +/- 0.2% fee buffer) using PLACE-THEN-CANCEL:
    the new reduce-only stop is placed and verified on /fapi/v1/openAlgoOrders BEFORE the old stop(s) are
    cancelled. If the new stop cannot be verified, the old stop is kept and nothing is cancelled.

    Rules (bypassed only with force=True):
      - YOLO positions (explicit flag, dossier candidate is_yolo, trade audit is_yolo, or YOLO leverage):
        only after TP1 has filled (right-tail preservation).
      - Standard positions: only after TP1 has filled or price expanded >= 2x ATR_15m (anti-truncation).
    Never loosens an existing stop and never places a stop that would trigger immediately (even with force).
    Issue #172: the stops are re-read right before the write; a fresher stop at or beyond break-even gives
    "already_at_breakeven" (no write), and a failed re-read falls back to the first read (never blocks).
    Returns the --move-breakeven JSON schema documented in the module docstring.
    """
    force = _truthy(force)
    dry_run = _truthy(dry_run)
    symbol = str(symbol).upper()
    result = {
        "success": False, "refused": False, "reason": None, "message": "",
        "symbol": symbol, "env": None, "direction": None,
        "entry_price": None, "mark_price": None, "breakeven_price": None,
        "old_stop": None, "old_stops": [], "new_stop": None,
        "cancelled_old_stop_ids": [], "warnings": [],
        "rules": {"force": force, "is_yolo": False, "yolo_source": None, "tp1_filled": None,
                  "tp1_source": None, "atr_15m": None, "expansion_atr_multiple": None},
        "dry_run": dry_run,
    }

    def finish(success, reason, message, refused=False):
        result.update(success=success, reason=reason, message=message, refused=refused)
        if not success:
            result["error"] = message
        return result

    try:
        target_env = resolve_env(target_env)
    except ValueError as e:
        return finish(False, "invalid_env", str(e))
    result["env"] = target_env

    pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol}, target_env=target_env)
    if not isinstance(pos_res, list):
        return finish(False, "position_query_failed", f"Position query failed for {symbol}: {pos_res}")
    active = [p for p in pos_res if _to_float(p.get('positionAmt')) != 0 and str(p.get('symbol', symbol)).upper() == symbol]
    if not active:
        return finish(False, "no_position", f"No active open position for {symbol}")

    pos = active[0]
    if _is_hedge_mode_row(pos):
        return finish(False, "hedge_mode_unsupported", HEDGE_MODE_UNSUPPORTED)
    amt = _to_float(pos.get('positionAmt'))
    is_long = amt > 0
    exit_side = 'SELL' if is_long else 'BUY'
    qty = str(pos.get('positionAmt')).strip().lstrip('-')
    entry_p = _to_float(pos.get('entryPrice'))
    mark_p = _to_float(pos.get('markPrice'))
    result.update(direction='LONG' if is_long else 'SHORT', entry_price=entry_p, mark_price=mark_p or None)

    old_stops, err = get_open_stop_orders(symbol, exit_side, target_env=target_env)
    if err:
        return finish(False, "orders_query_failed", f"Cannot read current stops for {symbol} ({err}); nothing changed.")
    old = tightest_stop(old_stops, is_long)
    result["old_stops"] = [stop_summary(ao) for ao in old_stops]
    result["old_stop"] = stop_summary(old)
    old_trigger = _trigger_price(old) if old else 0.0

    filters = get_symbol_filters(symbol, target_env=target_env)
    if not filters:
        return finish(False, "filters_unavailable", f"Symbol filters unavailable for {symbol}; nothing changed.")
    raw_be = entry_p * (1.0 + TRUE_NET_BE_FEE_BUFFER) if is_long else entry_p * (1.0 - TRUE_NET_BE_FEE_BUFFER)
    be_price = round_price(raw_be, filters['tickSize'], filters['precision_price'])
    result["breakeven_price"] = be_price

    # Never loosen: an existing stop at or beyond break-even is kept as is.
    if old and not is_tighter_stop(be_price, old_trigger, is_long):
        return finish(True, "already_at_breakeven",
                      f"Existing stop {old_trigger} for {symbol} is already at or beyond True Net Break-Even ({be_price}). Unchanged.")

    # Rule checks (YOLO right-tail preservation / anti-truncation)
    if not force:
        yolo, yolo_src = detect_yolo_position(symbol, leverage=pos.get('leverage'), explicit=is_yolo, base_dir=base_dir)
        tp1_filled, tp1_src = detect_tp1_filled(symbol, abs(amt), base_dir=base_dir)
        result["rules"].update(is_yolo=yolo, yolo_source=yolo_src, tp1_filled=tp1_filled, tp1_source=tp1_src)
        if yolo and tp1_filled is not True:
            return finish(False, "yolo_tp1_not_filled",
                          f"REFUSED: {symbol} is a YOLO position ({yolo_src}); its stop moves to break-even only after TP1 "
                          f"has filled (TP1 status: {tp1_src}). Use force=True / --force to override.", refused=True)
        if not yolo and tp1_filled is not True:
            atr = get_atr_15m(symbol)
            result["rules"]["atr_15m"] = atr
            favorable = (mark_p - entry_p) if is_long else (entry_p - mark_p)
            multiple = (favorable / atr) if atr else None
            result["rules"]["expansion_atr_multiple"] = round(multiple, 3) if multiple is not None else None
            if multiple is None or multiple < BE_MIN_EXPANSION_ATR:
                shown = f"{multiple:.2f}x" if multiple is not None else "unknown (ATR unavailable)"
                return finish(False, "insufficient_expansion",
                              f"REFUSED: {symbol} has not filled TP1 ({tp1_src}) and favorable expansion is {shown} ATR_15m "
                              f"(< {BE_MIN_EXPANSION_ATR:.1f}x required, anti-truncation). Use force=True / --force to override.",
                              refused=True)

    # Never place a stop that would trigger immediately (applies even with force).
    if mark_p <= 0:
        return finish(False, "mark_price_unavailable", f"Mark price unavailable for {symbol}; nothing changed.")
    if (is_long and mark_p <= be_price) or (not is_long and mark_p >= be_price):
        return finish(False, "breakeven_would_trigger_immediately",
                      f"REFUSED: mark price {mark_p} has not cleared True Net Break-Even {be_price} for {symbol}; a stop there "
                      f"would trigger immediately.", refused=True)

    if dry_run:
        result["new_stop"] = {"algo_id": None, "type": "STOP_MARKET", "side": exit_side, "trigger_price": be_price}
        return finish(True, "dry_run", f"DRY RUN: would move {symbol} stop from {old_trigger or 'none'} to {be_price}.")

    # Issue #172: re-read the stops right before writing, so a stop tightened meanwhile (guardian trail, another run) is
    # neither loosened nor duplicated. A failed re-read never blocks this risk-reducing move: the first read is used.
    fresh, fresh_err = get_open_stop_orders(symbol, exit_side, target_env=target_env)
    if not fresh_err:
        fresh_old = tightest_stop(fresh, is_long)
        fresh_trigger = _trigger_price(fresh_old) if fresh_old else 0.0
        result["old_stops"] = [stop_summary(ao) for ao in fresh]
        result["old_stop"] = stop_summary(fresh_old)
        if fresh_old and not is_tighter_stop(be_price, fresh_trigger, is_long):
            return finish(True, "already_at_breakeven",
                          f"Existing stop {fresh_trigger} for {symbol} is already at or beyond True Net Break-Even "
                          f"({be_price}) on a fresh read. Unchanged.")
        old_stops, old_trigger = fresh, fresh_trigger
    rep = replace_protective_stop(symbol, exit_side, be_price, qty, old_stops, target_env=target_env,
                                  tick_size=filters.get('tickSize'))
    if not rep["success"]:
        return finish(False, "new_stop_unverified",
                      f"New break-even stop for {symbol} at {be_price} could not be verified on openAlgoOrders "
                      f"({rep.get('placement')}). Old stop {old_trigger or 'none'} kept; nothing cancelled.")
    result["new_stop"] = rep["new_stop"]
    result["cancelled_old_stop_ids"] = rep["cancelled_old_stop_ids"]
    for ce in rep["cancel_errors"]:
        result["warnings"].append(f"Old stop {ce['algo_id']} could not be cancelled ({ce['error']}); it remains active "
                                  f"below the new stop (still reduce-only protection).")
    return finish(True, "moved",
                  f"Stop Loss moved to True Net Break-Even ({be_price}) for position {pos.get('positionAmt')} {symbol} "
                  f"(place-then-cancel; previous stop {old_trigger or 'none'}).")


def _f(value, default=0.0):
    return _to_float(value, default)


def get_positions_report(target_env=None):
    """
    Read-only listing of open positions with their attached SL/TP orders (--positions JSON schema).
    Never sends a write request.
    """
    target_env = resolve_env(target_env)
    report = {"success": False, "env": target_env, "timestamp": int(time.time()), "count": 0,
              "all_protected": True, "error": None, "positions": []}
    try:
        pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', target_env=target_env)
    except Exception as e:
        pos_res = {"error": str(e)}
    if not isinstance(pos_res, list):
        report["error"] = f"Error querying positions: {pos_res}"
        report["all_protected"] = False
        return report

    order_errors = []
    for p in pos_res:
        amt = _f(p.get('positionAmt'))
        if amt == 0:
            continue
        sym = p.get('symbol')
        is_long = amt > 0
        exit_side = 'SELL' if is_long else 'BUY'
        margin = _f(p.get('isolatedMargin'))
        unpnl = _f(p.get('unRealizedProfit'))
        try:
            lev = int(_f(p.get('leverage')))
        except (TypeError, ValueError):
            lev = 0
        entry = {
            "symbol": sym,
            "side": 'LONG' if is_long else 'SHORT',
            "size": abs(amt),
            "entry_price": _f(p.get('entryPrice')),
            "mark_price": _f(p.get('markPrice')),
            "leverage": lev,
            "margin_type": str(p.get('marginType', '')).upper(),
            "isolated_margin": margin,
            "unrealized_pnl": unpnl,
            "roe_pct": round(unpnl / margin * 100, 2) if margin > 0 else 0.0,
            "liquidation_price": _f(p.get('liquidationPrice')),
            "protected": False,
            "stop_orders": [],
            "take_profit_orders": [],
            "orders_error": None,
        }
        errs = []
        try:
            algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': sym}, target_env=target_env)
        except Exception as e:
            algos = {"error": str(e)}
        if isinstance(algos, list):
            for ao in algos:
                if not isinstance(ao, dict) or (ao.get('symbol') and ao.get('symbol') != sym):
                    continue
                if is_protective_stop(ao, exit_side, sym):
                    entry["stop_orders"].append({
                        "algo_id": _order_id(ao), "type": _order_type(ao), "side": ao.get('side'),
                        "trigger_price": _trigger_price(ao),
                        "quantity": _f(ao.get('quantity'), None) if ao.get('quantity') is not None else None,
                        "close_position": _truthy(ao.get('closePosition')),
                        "reduce_only": _truthy(ao.get('reduceOnly')),
                    })
                elif _order_type(ao) in TAKE_PROFIT_ORDER_TYPES and str(ao.get('side', '')).upper() == exit_side:
                    entry["take_profit_orders"].append({
                        "order_id": _order_id(ao), "source": "algo", "type": _order_type(ao), "side": ao.get('side'),
                        "price": _trigger_price(ao),
                        "quantity": _f(ao.get('quantity'), None) if ao.get('quantity') is not None else None,
                        "reduce_only": _truthy(ao.get('reduceOnly')) or _truthy(ao.get('closePosition')),
                    })
        else:
            errs.append(f"openAlgoOrders: {algos}")
        try:
            orders = send_signed_request('GET', '/fapi/v1/openOrders', {'symbol': sym}, target_env=target_env)
        except Exception as e:
            orders = {"error": str(e)}
        if isinstance(orders, list):
            for o in orders:
                if not isinstance(o, dict) or (o.get('symbol') and o.get('symbol') != sym):
                    continue
                if str(o.get('side', '')).upper() == exit_side and _truthy(o.get('reduceOnly')) and \
                        _order_type(o) in ('LIMIT',) + TAKE_PROFIT_ORDER_TYPES:
                    entry["take_profit_orders"].append({
                        "order_id": o.get('orderId'), "source": "order", "type": _order_type(o), "side": o.get('side'),
                        "price": _f(o.get('price')) or _f(o.get('stopPrice')),
                        "quantity": _f(o.get('origQty'), None) if o.get('origQty') is not None else None,
                        "reduce_only": True,
                    })
        else:
            errs.append(f"openOrders: {orders}")
        entry["protected"] = bool(entry["stop_orders"])
        if errs:
            entry["orders_error"] = "; ".join(errs)
            order_errors.append(f"{sym}: {entry['orders_error']}")
        report["positions"].append(entry)

    report["count"] = len(report["positions"])
    report["all_protected"] = all(p["protected"] for p in report["positions"])
    if order_errors:
        report["error"] = "Order queries failed: " + " | ".join(order_errors)
    else:
        report["success"] = True
    return report


def format_positions_report(report):
    if report.get("error") and not report.get("positions"):
        return f"Error querying positions: {report['error']}"
    if not report.get("positions"):
        return f"No active positions currently open ({str(report.get('env', '')).upper()})."
    lines = [f"ACTIVE POSITIONS ({str(report.get('env', '')).upper()}):"]
    for p in report["positions"]:
        stops = ", ".join(str(s["trigger_price"]) for s in p["stop_orders"]) or "NONE"
        tps = ", ".join(str(t["price"]) for t in p["take_profit_orders"]) or "none"
        lines.append(f"- {p['symbol']} {p['side']} {p['leverage']}x {p['margin_type']} | size {p['size']} | "
                     f"entry {p['entry_price']} | mark {p['mark_price']} | uPnL {p['unrealized_pnl']:+.2f} USDT "
                     f"({p['roe_pct']:+.2f}% ROE) | liq {p['liquidation_price']}")
        lines.append(f"  protected: {'yes' if p['protected'] else 'NO'} | SL: {stops} | TP: {tps}"
                     + (f" | orders error: {p['orders_error']}" if p.get('orders_error') else ""))
    if report.get("error"):
        lines.append(f"Error: {report['error']}")
    return "\n".join(lines)

def get_positions_summary(target_env=None):
    target_env = resolve_env(target_env)
    pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', target_env=target_env)
    if not isinstance(pos_res, list):
        return f"Error querying positions: {pos_res}"

    active = [p for p in pos_res if float(p.get('positionAmt', 0)) != 0]
    if not active:
        return "No active positions currently open."

    lines = [f"📊 ACTIVE POSITIONS ({target_env.upper()}):\n"]
    for p in active:
        amt = float(p['positionAmt'])
        dir_str = 'LONG' if amt > 0 else 'SHORT'
        entry = float(p['entryPrice'])
        mark = float(p['markPrice'])
        unpnl = float(p['unRealizedProfit'])
        margin = float(p.get('isolatedMargin', 0))
        roe = (unpnl / margin * 100) if margin > 0 else 0.0
        lines.append(f"• {p['symbol']} ({dir_str} {p['leverage']}x - {p['marginType'].upper()})")
        lines.append(f"  Size: {abs(amt)} | Entry: {entry:.4f} | Mark Price: {mark:.4f}")
        lines.append(f"  Unrealized PnL: {unpnl:+.2f} USDT (ROE: {roe:+.2f}%)")
        lines.append(f"  Liquidation Price: {float(p.get('liquidationPrice', 0)):.4f}\n")
    return "\n".join(lines)

def audit_orphan_positions(target_env=None, auto_heal=False):
    """
    Exhaustively audits all active positions in the account.
    Detects 'orphan' / 'naked' positions (without verified Algo Stop Loss on Binance).
    If auto_heal=True, places an emergency Algo SL calculated via volatility/liquidation buffer.
    Stops are read with retries (get_open_stop_orders_with_retry; after one symbol's read fails persistently, later
    symbols get a single read so the hook's audit stays bounded). protection per position: "protected" | "orphan" |
    "unknown" (every read failed: never healed here, not counted as an orphan, counted in unknown_count; the guardian
    heals one that stays UNKNOWN for STOP_UNKNOWN_ESCALATE_AFTER cycles via heal_unknown_stop). A hedge-mode
    account returns {"error", "hedge_mode": True}.
    """
    target_env = resolve_env(target_env)
    pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', target_env=target_env)
    if not isinstance(pos_res, list):
        return {"error": f"Error querying positions: {pos_res}"}
    if any(_is_hedge_mode_row(p) for p in pos_res):
        return {"error": HEDGE_MODE_UNSUPPORTED, "hedge_mode": True}

    active = [p for p in pos_res if float(p.get('positionAmt', 0)) != 0]
    if not active:
        return {
            "total_active": 0,
            "orphans_count": 0,
            "unknown_count": 0,
            "all_protected": True,
            "message": "No open positions in account."
        }

    positions_report = []
    orphans = []
    unknown = []
    read_delays = STOP_VERIFY_RETRY_DELAYS

    for p in active:
        sym = p['symbol']
        amt = float(p['positionAmt'])
        pos_dir = 'LONG' if amt > 0 else 'SHORT'
        exit_side = 'SELL' if amt > 0 else 'BUY'
        entry_p = float(p['entryPrice'])
        mark_p = float(p['markPrice'])
        lev = int(p['leverage'])
        margin = float(p.get('isolatedMargin', 0))
        unpnl = float(p.get('unRealizedProfit', 0))

        # Query active protective stop algo orders on exchange (retried; one read per symbol once a read failed)
        active_sls, orders_err = get_open_stop_orders_with_retry(sym, exit_side, target_env=target_env,
                                                                 retry_delays=read_delays)
        if orders_err:
            read_delays = ()

        is_protected = len(active_sls) > 0
        info = {
            "symbol": sym,
            "direction": pos_dir,
            "amount": abs(amt),
            "entry_price": entry_p,
            "mark_price": mark_p,
            "leverage": lev,
            "unpnl": unpnl,
            "is_protected": is_protected,
            "protection": "unknown" if orders_err else ("protected" if is_protected else "orphan"),
            "active_sl_orders": [_order_id(ao) for ao in active_sls],
            "sl_triggers": [_trigger_price(ao) for ao in active_sls]
        }
        if orders_err:
            info["orders_error"] = orders_err
            info["note"] = "openAlgoOrders unreadable: protection UNKNOWN; not healed (next audit/guardian cycle retries)"
            unknown.append(info)
        elif not is_protected:
            orphans.append(info)
            if auto_heal:
                # 2.5% emergency stop, verified with progressive retries (no market close from this path)
                heal = heal_orphan_position(p, target_env=target_env, close_on_failure=False)
                info["auto_heal_attempted"] = True
                info["auto_heal_verified"] = bool(heal.get("verified"))
                info["healed_sl_price"] = heal.get("healed_sl_price")
                if heal.get("verified"):
                    info["is_protected"] = True

        positions_report.append(info)

    return {
        "total_active": len(active),
        "orphans_count": len(orphans),
        "unknown_count": len(unknown),
        "all_protected": len(orphans) == 0 and len(unknown) == 0,
        "positions": positions_report
    }

def audit_and_auto_heal_orphans(target_env=None):
    """
    Audits all active positions and automatically heals any orphan positions lacking Stop Loss.
    """
    target_env = resolve_env(target_env)
    return audit_orphan_positions(target_env=target_env, auto_heal=True)

def _read_open_position(symbol, target_env=None, is_long=None):
    """Read-only positionRisk lookup. Returns (row_or_None, error_or_None): the first non-zero row of symbol (in the
    given direction when is_long is not None); (None, None) means flat, an error means the state is unknown.
    A hedge-mode row of the symbol returns (None, HEDGE_MODE_UNSUPPORTED)."""
    try:
        pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol}, target_env=target_env)
    except Exception as e:
        return None, str(e)
    if not isinstance(pos_res, list):
        return None, str(pos_res)
    if any(_is_hedge_mode_row(p) and str(p.get('symbol', symbol)).upper() == symbol.upper() for p in pos_res):
        return None, HEDGE_MODE_UNSUPPORTED
    for p in pos_res:
        if not isinstance(p, dict) or str(p.get('symbol', symbol)).upper() != symbol.upper():
            continue
        amt = _to_float(p.get('positionAmt'))
        if amt != 0 and (is_long is None or (amt > 0) == is_long):
            return p, None
    return None, None


def _qty_tolerance(symbol, target_env=None):
    """Quantity match tolerance: max(1e-9, stepSize / 2) from get_symbol_filters; 1e-9 when the filters read fails."""
    try:
        return max(1e-9, float(get_symbol_filters(symbol, target_env=target_env)['stepSize']) / 2)
    except Exception:
        return 1e-9


def _planned_sl_for_position(symbol, row, target_env=None):
    """sl_price of the latest trades_audit entry record that matches the live position (direction, env, entry within
    tolerance, total_qty >= |positionAmt|), else None. Never raises.
    With a matching record, a ratcheted stop (break-even / trailed) from logs/session_state.json replaces it when the
    state is valid, of the same env and not older than the record, the entry matches (symbol, direction, entry within
    tolerance, qty >= |positionAmt| within _qty_tolerance, sl_algo_verified) and its sl_price is tighter than the planned SL and not crossed
    vs markPrice. The state's sl_price is any open algo trigger of the symbol, hence those guards. Limitation: the
    state reflects the last ledger sync only."""
    try:
        from utils import position_timing as pt
        amt = _to_float(row.get('positionAmt'))
        is_long = amt > 0
        direction = 'LONG' if is_long else 'SHORT'
        audit_path = os.path.join(_workspace_dir(), 'logs', 'trades_audit.jsonl')
        rec = pt.latest_audit_entry_record(audit_path, symbol, direction, target_env)
        if not (rec and pt.audit_record_matches_position(rec, row.get('entryPrice'), row.get('positionAmt'))):
            return None
        planned = rec.get('sl_price')
    except Exception:
        return None
    try:
        with open(os.path.join(_workspace_dir(), 'logs', 'session_state.json'), 'r', encoding='utf-8') as f:
            state = json.load(f)
        if not isinstance(state, dict) or state.get('is_valid') is False or not state.get('target_env') \
                or resolve_env(state.get('target_env')) != resolve_env(target_env) \
                or float(state.get('last_updated_ts')) < float(rec.get('timestamp')):
            return planned
        live_entry = _to_float(row.get('entryPrice'))
        mark = _to_float(row.get('markPrice'))
        planned_f = _to_float(planned)
        qty_tol = None
        for entry in state.get('active_positions') or []:
            if not isinstance(entry, dict) or str(entry.get('symbol', '')).upper() != str(symbol).upper() \
                    or str(entry.get('direction', '')).upper() != direction or entry.get('sl_algo_verified') is not True:
                continue
            if qty_tol is None:
                qty_tol = _qty_tolerance(symbol, target_env)
            entry_p = _to_float(entry.get('entry_price'))
            if live_entry <= 0 or entry_p <= 0 \
                    or abs(entry_p - live_entry) / live_entry * 100 > pt.AUDIT_ENTRY_PRICE_TOLERANCE_PCT \
                    or abs(_to_float(entry.get('qty'))) < abs(amt) - qty_tol:
                continue
            cand = float(entry.get('sl_price'))
            if cand > 0 and mark > 0 and (planned_f <= 0 or is_tighter_stop(cand, planned_f, is_long)) \
                    and ((cand < mark) if is_long else (cand > mark)):
                return cand
            return planned
    except Exception:
        pass
    return planned


def _is_existing_close_position_stop_rejection(placement):
    """True when a closePosition stop placement was rejected with Binance -4130 (a closePosition stop in that
    direction already exists)."""
    if not isinstance(placement, dict):
        return False
    if str(placement.get('code')) == '-4130':
        return True
    return '-4130' in str(placement.get('error') or placement.get('msg') or '')


def _report_close_failure(symbol, target_env, error, stop_source):
    """CRITICAL/P0 issue for a close that was not confirmed flat. Never raises (the close path must not break).
    error_detail (hashed into the dedup fingerprint) is stable per symbol + stop source; the full error (attempts,
    raw responses, read errors) goes into context."""
    try:
        import report_agent_issue
        report_agent_issue.report_issue(
            title=f"close_position_market: reduce-only close of {symbol} not confirmed flat",
            error_detail=f"{symbol} close not confirmed flat; stop {stop_source}",
            category="risk_gate", severity="CRITICAL", priority="P0",
            agent_name="execute_futures_trade.close_position_market",
            affected_files="scripts/execute_futures_trade.py:close_position_market",
            context=(f"env={target_env}; symbol={symbol}; stop_source={stop_source}; no order was cancelled; "
                     f"error: {error}"),
            remediation="Check the position on Binance; keep it protected and retry --close-position.")
    except Exception as e:
        logger.error(f"close failure report for {symbol} could not be filed: {e}")


def _report_leftover_stop_abort(symbol, target_env, sl_order, abort_exit):
    """HIGH issue (issue #157): a MARKET entry auto-destructed because its Stop Loss placement got -4130 (a
    closePosition stop already exists, probably a leftover pre-arm) and stop verification is by algo id only. Never
    raises (the abort path must not break)."""
    try:
        import report_agent_issue
        report_agent_issue.report_issue(
            title=f"execute_complete_trade: MARKET entry on {symbol} auto-destructed, probable leftover stop (-4130)",
            error_detail=f"{symbol} MARKET SL rejected -4130, leftover stop",
            category="risk_gate", severity="HIGH",
            agent_name="execute_futures_trade.execute_complete_trade",
            affected_files="scripts/execute_futures_trade.py:execute_complete_trade",
            context=(f"env={target_env}; symbol={symbol}; SL placement: {sl_order}; abort confirmed="
                     f"{bool((abort_exit or {}).get('confirmed'))}"),
            remediation=("List the symbol's algo orders on Binance and cancel the leftover closePosition stop "
                         "(e.g. a pre-arm of an ended resting entry) once no position is open."))
    except Exception as e:
        logger.error(f"leftover stop report for {symbol} could not be filed: {e}")


def _report_check_deferrals(symbol, key, target_env, count, stages):
    """HIGH issue (issue #156) when the protect-pending loss-cap / qty checks of a record were deferred `count`
    consecutive runs. Never raises; returns None when filed, else the error text (the caller adds a warning).
    error_detail is stable per symbol so the reporter's 24h fingerprint dedups repeats."""
    try:
        import report_agent_issue
        report_agent_issue.report_issue(
            title=f"protect-pending: record checks of {symbol} deferred {count} consecutive runs",
            error_detail=f"{symbol} pending record loss-cap/qty check deferred repeatedly",
            category="risk_gate", severity="HIGH",
            agent_name="execute_futures_trade.protect_pending_entries",
            affected_files="scripts/execute_futures_trade.py:_protect_pending_entry",
            context=(f"env={target_env}; symbol={symbol}; key={key}; deferrals={count}; "
                     f"deferred checks this run: {', '.join(stages)}"),
            remediation=("Check the equity / positionRisk reads, exchangeInfo filters and config/user_profile.json; "
                         "the record's SL is not re-checked against the Gate 2 cap until they succeed."))
        return None
    except Exception as e:
        logger.error(f"deferral report for {symbol} could not be filed: {e}")
        return f"check_deferrals={count}: issue report failed ({type(e).__name__}: {e})"


def _report_abort_failure(symbol, target_env, site, error):
    """CRITICAL/P0 issue (issue #40) for a fail-safe abort or close that did not end flat or protected: the
    pending_abort and crossed-close paths of --protect-pending, the inline partial-fill abort and the
    heal_orphan_position close. Never raises (reporting must not break the risk-reducing path). error_detail (hashed
    into the 24h dedup fingerprint with the title) is stable per symbol + site; the full error goes into context.
    Issue #160: the fingerprint differs per site, so a guardian cycle whose failed crossed close already reported a
    symbol heals it with heal_orphan_position(report_failure=False): one P0 per failure, the heal/close still run."""
    try:
        import report_agent_issue
        report_agent_issue.report_issue(
            title=f"{site}: fail-safe abort of {symbol} did not end flat or protected",
            error_detail=f"{symbol} abort not flat or protected at {site}",
            category="risk_gate", severity="CRITICAL", priority="P0",
            agent_name=f"execute_futures_trade.{site}",
            affected_files=f"scripts/execute_futures_trade.py:{site}",
            context=f"env={target_env}; symbol={symbol}; site={site}; error: {error}",
            remediation="Check the position on Binance now; protect it or close it with --close-position.")
    except Exception as e:
        logger.error(f"abort failure report for {symbol} could not be filed: {e}")


def heal_unknown_stop(symbol, position, target_env=None):
    """Heal of a position whose stop read failed (issue #173: close_position_market, the guardian's persistent UNKNOWN
    escalation, the SWING night cutoff). heal_orphan_position with close_on_failure=False and the planned SL of
    _planned_sl_for_position; never closes and never raises. Returns {"result", "detail", "heal", "redundant_stops",
    "note"}:
      result "healed" -> an emergency stop was placed and verified; one more read lists any other protective stops in
                         redundant_stops (never cancelled: the cancelled one could be the only real stop; all are
                         reduce-only/closePosition and the next flat close cancels the leftovers) with a note;
             "kept"   -> the placement got -4130: a closePosition stop already exists (note "stop inferred from -4130");
             "failed" -> anything else (heal unverified or raised).
    heal is the raw heal_orphan_position result ({"verified": False, "reason": "heal failed: ..."} on an exception)."""
    try:
        heal = heal_orphan_position(position, target_env=target_env, close_on_failure=False,
                                    planned_sl=_planned_sl_for_position(symbol, position, target_env))
    except Exception as e:
        heal = {"verified": False, "reason": f"heal failed: {e}"}
    out = {"result": "failed", "detail": f"heal not verified ({heal.get('reason')})", "heal": heal,
           "redundant_stops": [], "note": None}
    if heal.get("verified"):
        out.update(result="healed", detail=f"verified emergency stop at {heal.get('healed_sl_price')}")
        try:
            exit_side = 'SELL' if _to_float(position.get('positionAmt')) > 0 else 'BUY'
            after, after_err = get_open_stop_orders(symbol, exit_side, target_env=target_env)
            healed = heal.get("new_stop") or {}
            others = [s for s in after if (str(_order_id(s)) != str(healed.get("algo_id"))
                                           if healed.get("algo_id") is not None
                                           else _trigger_price(s) != healed.get("trigger_price"))] \
                if after_err is None else []
        except Exception:
            others = []
        if others:
            out["redundant_stops"] = [stop_summary(s) for s in others]
            out["note"] = ("heal stop placed next to existing stop(s) (pre-heal reads failed); all are "
                           "reduce-only/closePosition and the next flat close cancels the leftovers")
    elif _is_existing_close_position_stop_rejection(heal.get("placement")):
        out.update(result="kept", detail="placement rejected -4130: a closePosition stop already exists",
                   note="stop inferred from -4130")
    return out


def close_position_market(symbol, target_env=None):
    """
    Risk-reducing reduce-only MARKET close of the open position of symbol (same in PROD/TESTNET, KEYS/MCP).
    The protective stop is never cancelled before the position is confirmed flat on positionRisk:
      1. read positionRisk (an unreadable state sends nothing),
      2. send the reduce-only MARKET close, up to 3 attempts (CLOSE_RETRY_DELAYS backoff), re-reading the size
         before each retry (partial fills retry the residual; already flat = done),
      3. flat: cancel leftover orders/stops (_cancel_symbol_orders); cleanup errors are returned, not fatal,
      4. not flat: cancel nothing, read the stops on openAlgoOrders (retried with STOP_VERIFY_RETRY_DELAYS), heal
         one when none is seen (also when every read failed: a naked position must not stay naked; the heal stop
         uses the planned SL of the matching trades_audit record, or a newer ratcheted SL from session_state.json, when
         tighter and not crossed) and report CRITICAL/P0.
    The quantity is sent as a plain decimal string (format_order_qty, never scientific notation).
    Not-flat result: {success: False, error, attempts, position_amt, closed, heal?, stop_source, stop_protected,
    stop_note?, redundant_stops?}:
      stop_source "kept"    -> a protective stop was read (or inferred from a -4130 heal rejection after every read
                               failed), stop_protected True;
                  "healed"  -> an emergency stop was placed and verified, stop_protected True;
                  "none"    -> a successful read showed no stop and the heal failed, stop_protected False;
                  "unknown" -> every read failed and the heal failed for another reason, stop_protected None.
    stop_protected is True / False / None; consumers MUST treat anything but True as unprotected (never test
    `is False`). When every pre-heal read failed the heal is heal_unknown_stop; when it verified, one more read lists any other protective stops
    in redundant_stops with a stop_note: they are never cancelled here (the cancelled one could be the only real
    stop); all are reduce-only/closePosition and the next flat close cancels the leftovers.
    Early returns (only "error"; no report): no position, unreadable positionRisk, hedge mode ("hedge_mode": True).
    """
    target_env = resolve_env(target_env)
    symbol = str(symbol).upper()  # every read/write (and _wait_until_flat's match) uses the exchange's symbol case
    row, err = _read_open_position(symbol, target_env)
    if err == HEDGE_MODE_UNSUPPORTED:
        return {"success": False, "error": HEDGE_MODE_UNSUPPORTED + f" ({symbol})", "hedge_mode": True}
    if err is not None:
        return {"success": False, "error": f"position state unknown: {err}"}
    if row is None:
        return {"success": False, "error": f"No open position in {symbol}"}

    is_long = _to_float(row.get('positionAmt')) > 0
    exit_side = 'SELL' if is_long else 'BUY'
    qty = format_order_qty(row.get('positionAmt'))
    res, attempts, flat, state_unknown = None, 0, False, None

    for attempts, delay in enumerate(CLOSE_RETRY_DELAYS, start=1):
        try:
            res = send_signed_request('POST', '/fapi/v1/order', {'symbol': symbol, 'side': exit_side, 'type': 'MARKET',
                                                                'quantity': qty, 'reduceOnly': 'true'},
                                      target_env=target_env)
        except Exception as e:
            res = {"error": str(e)}
        if _market_order_accepted(res) and _wait_until_flat(symbol, is_long, target_env):
            flat = True
            break
        time.sleep(delay)
        fresh, state_unknown = _read_open_position(symbol, target_env, is_long=is_long)
        if state_unknown is None and fresh is None:
            flat = True
            break
        if fresh is not None:
            # Partial fill: the next attempt closes the residual (an unreadable state keeps the last known size).
            row, qty = fresh, format_order_qty(fresh.get('positionAmt'))

    if flat:
        cleanup_errors = _cancel_symbol_orders(symbol, target_env)
        for ce in cleanup_errors:
            print(f"WARNING: {symbol} closed but a leftover order could not be cancelled: {ce}", file=sys.stderr)
        return {"success": True, "closed": res, "attempts": attempts, "cleanup_errors": cleanup_errors}

    # Not confirmed flat: nothing is cancelled; make sure the (residual) position keeps a verified stop.
    stops, stop_err = get_open_stop_orders_with_retry(symbol, exit_side, target_env=target_env)
    heal, note, redundant = None, None, None
    if stops:
        stop_source = "kept"
    elif stop_err is None:
        try:
            heal = heal_orphan_position(row, target_env=target_env, close_on_failure=False,
                                        planned_sl=_planned_sl_for_position(symbol, row, target_env))
        except Exception as e:
            heal = {"verified": False, "reason": f"heal failed: {e}"}
        stop_source = "healed" if heal.get("verified") else "none"
    else:
        # Every read failed: the shared unknown-stop heal (-4130 = kept, redundant stops listed, never a close).
        unknown = heal_unknown_stop(symbol, row, target_env=target_env)
        heal, redundant = unknown["heal"], unknown["redundant_stops"] or None
        stop_source = {"healed": "healed", "kept": "kept"}.get(unknown["result"], "unknown")
        note = unknown.get("note")
    error = (f"Reduce-only MARKET close of {symbol} not confirmed flat after {attempts} attempt(s) "
             f"(last response: {res}); nothing cancelled; stop {stop_source}")
    if note:
        error += f" ({note})"
    if state_unknown is not None:
        error += f"; position state unknown on the last read: {state_unknown}"
    if stop_err and not stops:
        error += f"; stop check: {stop_err}"
    logger.error(error)
    _report_close_failure(symbol, target_env, error, stop_source)
    stop_protected = {"kept": True, "healed": True, "none": False}.get(stop_source)
    out = {"success": False, "error": error, "attempts": attempts, "position_amt": _to_float(row.get('positionAmt')),
           "stop_protected": stop_protected, "stop_source": stop_source, "closed": res}
    if note:
        out["stop_note"] = note
    if redundant:
        out["redundant_stops"] = redundant
    if heal is not None:
        out["heal"] = heal
    return out

def deploy_futures_trade(
    symbol,
    direction,
    leverage=3,
    margin_usdt=100.0,
    sl_price=None,
    tp1_price=None,
    tp2_price=None,
    target_env=None,
    trigger_price=None,
    order_type='MARKET',
    limit_price=None,
    bypass_delta_gate=False,
    is_yolo=False,
    bypass_eval_gate=False,
    confirmed=False
):
    """
    Deploy futures trade with risk gates, isolated margin, verified Stop Loss, and asymmetric Take Profits.
    """
    return execute_complete_trade(
        symbol=symbol,
        direction=direction,
        leverage=leverage,
        margin_usdt=margin_usdt,
        sl_price=sl_price,
        tp1_price=tp1_price,
        tp2_price=tp2_price,
        target_env=target_env,
        trigger_price=trigger_price,
        order_type=order_type,
        limit_price=limit_price,
        bypass_delta_gate=bypass_delta_gate,
        is_yolo=is_yolo,
        bypass_eval_gate=bypass_eval_gate,
        confirmed=confirmed
    )

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Execution Engine and Risk Management Harness for Binance Futures",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--symbol", type=str, default=None, help="Trading pair symbol (e.g. BTCUSDT, ETHUSDT)")
    parser.add_argument("--direction", type=str, choices=["LONG", "SHORT", "long", "short"], default=None, help="Position direction")
    parser.add_argument("--leverage", type=int, default=3, help="Leverage multiplier (max: profile leverage_ceiling, default 15x)")
    parser.add_argument("--margin", type=float, default=100.0, help="Committed margin in USDT")
    parser.add_argument("--trigger-price", "--trigger_price", type=float, default=None, dest="trigger_price", help="Breakout trigger price for conditional entry")
    parser.add_argument("--sl-price", "--sl_price", type=float, default=None, dest="sl_price", help="Stop Loss price")
    parser.add_argument("--tp1-price", "--tp1_price", type=float, default=None, dest="tp1_price", help="Take Profit 1 price (30%% position)")
    parser.add_argument("--tp2-price", "--tp2_price", type=float, default=None, dest="tp2_price", help="Take Profit 2 price (70%% position)")
    parser.add_argument("--order-type", "--order_type", type=str, choices=["MARKET", "LIMIT", "STOP_MARKET", "market", "limit", "stop_market"], default="MARKET", dest="order_type", help="Order type")
    parser.add_argument("--limit-price", "--limit_price", type=float, default=None, dest="limit_price", help="Limit price when order_type=LIMIT")
    parser.add_argument("--env", type=str, choices=["prod", "testnet"], default=None, help="Target environment ('prod' or 'testnet'); defaults to utils.env_resolver.resolve_env()")
    parser.add_argument("--bypass-eval-gate", "--bypass_eval_gate", action="store_true", dest="bypass_eval_gate", help="Bypass clean-room evaluation gate (TESTNET only; refused in PROD)")
    parser.add_argument("--bypass-delta-gate", "--bypass_delta_gate", action="store_true", dest="bypass_delta_gate", help="Bypass delta-neutral gate (Testnet only)")
    parser.add_argument("--is-yolo", "--is_yolo", action="store_true", dest="is_yolo", help="Mark trade as YOLO moonshot (authorizes leverage > 5x)")
    parser.add_argument("--confirmed", "--user-confirmed", action="store_true", dest="confirmed", help="Explicit human confirmation for live order in PROD")
    parser.add_argument("--close-position", "--close_position", action="store_true", dest="close_position", help="Close open position at market with reduceOnly")
    parser.add_argument("--audit-orphans", "--audit_orphans", action="store_true", dest="audit_orphans", help="Audit all open positions for missing Stop Loss")
    parser.add_argument("--auto-heal", "--auto_heal", action="store_true", dest="auto_heal", help="Audit and automatically heal orphan positions lacking Stop Loss")
    parser.add_argument("--protect-pending", "--protect_pending", action="store_true", dest="protect_pending", help="Place the planned SL/TPs of filled resting entries (logs/pending_entries.json); cancel expired ones")
    parser.add_argument("--positions", action="store_true", help="Read-only list of open positions with attached SL/TP orders")
    parser.add_argument("--move-breakeven", "--move_breakeven", action="store_true", dest="move_breakeven", help="Move the Stop Loss of --symbol to True Net Break-Even (place-then-cancel)")
    parser.add_argument("--force", action="store_true", help="With --move-breakeven: override the YOLO-before-TP1 and anti-truncation rules")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit machine-readable JSON (schemas in the module docstring)")

    args = parser.parse_args()

    new_modes = [m for m in ("positions", "move_breakeven") if getattr(args, m)]
    other_modes = [m for m in ("close_position", "audit_orphans", "auto_heal", "protect_pending") if getattr(args, m)]
    if new_modes and (len(new_modes) > 1 or other_modes or args.direction):
        print(json.dumps({"success": False, "error": "--positions and --move-breakeven are exclusive modes; they cannot be combined with other modes or --direction."}, indent=2))
        sys.exit(1)
        return
    if args.protect_pending and (len(other_modes) > 1 or args.direction):
        print(json.dumps({"success": False, "error": "--protect-pending is an exclusive mode; it cannot be combined with other modes or --direction."}, indent=2))
        sys.exit(1)
        return

    try:
        target_env = resolve_env(args.env)
    except ValueError as e:
        print(json.dumps({"success": False, "error": f"Invalid environment: {e}"}, indent=2))
        sys.exit(1)
        return

    # 0a. Read-only positions listing
    if args.positions:
        res = get_positions_report(target_env=target_env)
        print(json.dumps(res, indent=2) if args.json_output else format_positions_report(res))
        sys.exit(0 if res.get("success") else 1)
        return

    # 0b. Move Stop Loss to True Net Break-Even (risk-reducing, place-then-cancel)
    if args.move_breakeven:
        if not args.symbol:
            print(json.dumps({"success": False, "refused": False, "reason": "missing_symbol", "error": "--symbol is required for --move-breakeven"}, indent=2))
            sys.exit(1)
            return
        res = move_sl_to_breakeven(args.symbol.upper(), target_env=target_env, force=args.force,
                                   is_yolo=True if args.is_yolo else None)
        if args.json_output:
            print(json.dumps(res, indent=2))
        else:
            print(res.get("message", ""))
            for w in res.get("warnings", []):
                print(f"WARNING: {w}")
        sys.exit(0 if res.get("success") else (2 if res.get("refused") else 1))
        return

    # 0c. Post-fill protection of resting entries (risk-reducing: never opens or increases a position)
    if args.protect_pending:
        res = protect_pending_entries(target_env=target_env)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("ok") else 1)
        return

    # 1. Close Position
    if args.close_position:
        if not args.symbol:
            print(json.dumps({"success": False, "error": "--symbol is required for --close-position"}, indent=2))
            sys.exit(1)
            return
        res = close_position_market(args.symbol.upper(), target_env=target_env)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("success") else 1)
        return

    # 2. Auto-Heal Orphans
    if args.auto_heal:
        res = audit_and_auto_heal_orphans(target_env=target_env)
        print(json.dumps(res, indent=2))
        if "error" in res or res.get("unknown_count", 0) > 0 or \
                (res.get("orphans_count", 0) > 0 and not res.get("all_protected", False)):
            sys.exit(1)
        else:
            sys.exit(0)
        return

    # 3. Audit Orphans
    if args.audit_orphans:
        res = audit_orphan_positions(target_env=target_env, auto_heal=False)
        print(json.dumps(res, indent=2))
        if "error" in res or res.get("unknown_count", 0) > 0:
            sys.exit(1)
        else:
            sys.exit(0)
        return

    # 4. Standard Trade Deployment
    if not args.symbol:
        print(json.dumps({"success": False, "error": "--symbol is required for trade deployment"}, indent=2))
        sys.exit(1)
        return

    if not args.direction:
        print(json.dumps({"success": False, "error": "--direction (LONG or SHORT) is required for trade deployment"}, indent=2))
        sys.exit(1)
        return

    direction = args.direction.upper()
    symbol = args.symbol.upper()
    order_type = args.order_type.upper()

    trade_kwargs = {
        "symbol": symbol,
        "direction": direction,
        "leverage": args.leverage,
        "margin_usdt": args.margin,
        "sl_price": args.sl_price,
        "tp1_price": args.tp1_price,
        "tp2_price": args.tp2_price,
        "target_env": target_env,
        "trigger_price": args.trigger_price,
        "order_type": order_type,
        "limit_price": args.limit_price,
        "bypass_delta_gate": args.bypass_delta_gate,
        "is_yolo": args.is_yolo,
        "bypass_eval_gate": args.bypass_eval_gate,
    }
    if getattr(args, "confirmed", False):
        trade_kwargs["confirmed"] = True

    res = execute_complete_trade(**trade_kwargs)

    print(json.dumps(res, indent=2))
    if res.get("success"):
        sys.exit(0)
    else:
        sys.exit(1)
    return

if __name__ == '__main__':
    main()
