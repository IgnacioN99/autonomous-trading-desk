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
                                       # breakeven_would_trigger_immediately | new_stop_unverified | invalid_env
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

  --close-position: {"success": bool, "closed": {...exchange response...}, "error"?: str}
  --audit-orphans / --auto-heal: {"total_active": int, "orphans_count": int, "all_protected": bool,
      "positions": [{"symbol", "direction", "amount", "entry_price", "mark_price", "leverage", "unpnl",
                     "is_protected", "active_sl_orders", "sl_triggers", "auto_heal_attempted"?,
                     "auto_heal_verified"?, "healed_sl_price"?}], "error"?: str}
  --protect-pending (exit 0 iff ok): {"ok": bool, "env": str, "dry_run": bool,
      "actions": [{"type": "pending_protect_sl" | "pending_tp_placed" | "pending_abort" | "pending_timeout_cancel" |
                           "pending_dropped" | "pending_sl_crossed_close", "key", "symbol", "success": bool,
                "dry_run": bool, "detail": {...}}],
      "errors": [{"key", "symbol", "stage", "error"}]}
  trade deployment: {"success": bool, "symbol", "direction", "leverage", "entry_price", "total_qty",
      "sl_price", "tp1_price", "tp2_price", ..., "error"?: str, "hard_gate_rejection"?: bool}
      Resting entries (untriggered STOP_MARKET via the algo order API, resting LIMIT) return
      "conditional_entry" / "pending_limit_entry": true and "pending_entry_key"; they are recorded in
      logs/pending_entries.json, require a live position guardian in PROD and count against max_open_positions.
      In PROD every new entry is rejected while an opening order rests on the exchange without a registry record
      (find_unregistered_resting_entries; the position guardian reports them as unknown_resting_entry).
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
from decimal import Decimal, ROUND_DOWN

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
    req = urllib.request.Request(
        "https://agent.binance.com/mcp/agentic",
        headers=headers,
        data=json.dumps(payload).encode("utf-8")
    )
    try:
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
            'triggerPrice': str(trig_p),
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
            mcp_args['quantity'] = str(qty)
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
        if isinstance(res, dict) and 'orderId' in res and 'algoId' not in res:
            res['algoId'] = res['orderId']
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

def verify_algo_stop_loss(symbol, exit_side, sl_price=None, target_env=None):
    """
    Verifies that the Algo Stop Loss order actually exists and is active on the exchange.
    Ensures that if sl_price is specified, True is ONLY returned if the price matches within 3% tolerance.
    Only real protective stops count (is_protective_stop): a resting conditional ENTRY on the same side
    (neither closePosition nor reduceOnly) never verifies as a Stop Loss.
    """
    target_env = resolve_env(target_env)
    try:
        algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}, target_env=target_env)
        if isinstance(algos, list):
            for ao in algos:
                if is_protective_stop(ao, exit_side, symbol):
                    if sl_price is not None:
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


def detect_yolo_position(symbol, leverage=None, explicit=None, base_dir=None):
    """
    Returns (is_yolo, source). A position is YOLO if any of:
      - explicit flag (can only mark a position as YOLO, never un-mark it),
      - the evaluation dossier candidate for the symbol has is_yolo,
      - the latest trades_audit entry for the symbol has is_yolo,
      - leverage >= profile leverage_yolo, or leverage > profile leverage_standard (the executor refuses
        leverage above leverage_standard for non-YOLO trades).
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
    rec = latest_trade_audit_record(symbol, base)
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


def detect_tp1_filled(symbol, current_qty, base_dir=None):
    """
    Returns (filled, source). TP1 is considered filled when the live position size has been reduced by at least
    half of the TP1 quantity recorded at entry in logs/trades_audit.jsonl. Returns (None, ...) when unknown.
    """
    rec = latest_trade_audit_record(symbol, base_dir)
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


def heal_orphan_position(position, target_env=None, close_on_failure=False):
    """
    Places a verified emergency stop on a position that has none. The stop sits ORPHAN_HEAL_SL_DISTANCE away
    from the worse of entry/mark so it can never trigger on placement. If it cannot be verified and
    close_on_failure=True, the position is closed with a reduce-only market order (fail-safe auto-destruct).
    Never opens or increases exposure.
    """
    target_env = resolve_env(target_env)
    sym = position['symbol']
    amt = _to_float(position.get('positionAmt'))
    is_long = amt > 0
    exit_side = 'SELL' if is_long else 'BUY'
    entry_p = _to_float(position.get('entryPrice'))
    mark_p = _to_float(position.get('markPrice')) or entry_p
    out = {"symbol": sym, "success": False, "verified": False, "healed_sl_price": None, "closed": False, "close_result": None}

    filters = get_symbol_filters(sym, target_env=target_env)
    if not filters:
        out["reason"] = "filters_unavailable"
    else:
        anchor = min(entry_p, mark_p) if is_long else max(entry_p, mark_p)
        raw_sl = anchor * (1 - ORPHAN_HEAL_SL_DISTANCE) if is_long else anchor * (1 + ORPHAN_HEAL_SL_DISTANCE)
        heal_sl = round_price(raw_sl, filters['tickSize'], filters['precision_price'])
        out["healed_sl_price"] = heal_sl
        try:
            out["placement"] = place_algo_stop_loss(sym, exit_side, heal_sl, target_env=target_env)
        except Exception as e:
            out["placement"] = {"error": str(e)}
        placed_id = _order_id(out["placement"]) if isinstance(out["placement"], dict) else None
        verified, info = wait_for_stop_confirmation(sym, exit_side, heal_sl, algo_id=placed_id,
                                                    tick_size=filters.get('tickSize'), target_env=target_env)
        out["verified"] = verified
        out["new_stop"] = stop_summary(info) if verified else None
        if verified:
            out["success"] = True
            out["reason"] = "healed"
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
        'quantity': total_qty,
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


def check_mechanical_gates(direction, cur_price, sl_price, tp1_price, total_qty, leverage, bypass_delta_gate=False, target_env=None, bypass_all_gates=False, is_yolo=False,
                           maint_margin_ratio=None, maint_amount=0.0, mmr_source=None, liq_entry_price=None, entry_price=None):
    """
    Mechanical Software Gates (Deterministic Precondition Validation).
    Verifies mathematical invariants and physically prevents execution if risk rules are violated.
    In TESTNET, free bypass of gates is permitted for testing, experiments, and stress tests.
    In PROD, gates are strict and inviolable.
    `entry_price` is the effective entry of the order actually sent (limit/trigger price for conditional
    entries); the risk, YOLO cap and friction gates are measured from it (falls back to cur_price).
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
    slots_ok, slots_err = check_max_open_positions(prof, target_env)
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

        delta_bias = state_data.get('portfolio_exposure', {}).get('delta_bias') or state_data.get('portfolio_delta_bias', 'NEUTRAL')
        if delta_bias == 'LONG_HEAVY' and is_long:
            return False, "MECHANICAL HARD GATE REJECTION: Portfolio is in LONG_HEAVY state (+Delta imbalanced). Opening additional Longs is strictly prohibited. Short hedge or neutral portfolio required."
        elif delta_bias == 'SHORT_HEAVY' and not is_long:
            return False, "MECHANICAL HARD GATE REJECTION: Portfolio is in SHORT_HEAVY state (-Delta imbalanced). Opening additional Shorts is strictly prohibited. Long hedge or neutral portfolio required."

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

    # Load risk percentage from user profile (default 0.005 = 0.5%)
    try:
        raw_risk = float(prof.get("risk_pct_equity", 0.005))
    except Exception:
        raw_risk = 0.005

    # Normalize risk fraction: e.g. 0.005 -> 0.005; 0.5 -> 0.005; 1.0 -> 0.01
    risk_fraction = raw_risk if raw_risk <= 0.05 else (raw_risk / 100.0)

    # Dynamic risk ceiling = account_equity * (risk_pct_equity / 100) * 1.25 buffer
    if is_testnet:
        max_allowed_loss = max(account_equity * risk_fraction * 1.25, 50.0)
    elif is_yolo:
        # Barbell YOLO Moonshot: strict software loss cap (35% of margin, min $3.75 USDT)
        margin_est = (ref * total_qty / max(leverage, 1))
        max_allowed_loss = max(3.75, margin_est * 0.35)
    else:
        max_allowed_loss = account_equity * risk_fraction * 1.25

    if potential_dollar_loss > max_allowed_loss:
        return False, f"MECHANICAL HARD GATE REJECTION: Monetary risk exceeds allowed cap (${potential_dollar_loss:.2f} > ${max_allowed_loss:.2f} USDT, entry ref {ref}, equity: ${account_equity:.2f}, risk fraction: {risk_fraction*100:.2f}% + buffer). Adjust margin or position size."

    # --- GATE 3: Financial Friction and Fee Gate ---
    if tp1_price and not is_testnet:
        # Signed distance: a TP1 on the wrong side of the effective entry is negative and rejected
        profit_pct_tp1 = ((tp1_price - ref) / ref) if is_long else ((ref - tp1_price) / ref)
        if profit_pct_tp1 < 0.0035:
            return False, f"MECHANICAL HARD GATE REJECTION: Distance to TP1 ({profit_pct_tp1*100:.2f}%) below 0.35% friction floor or on the wrong side of entry (entry ref {ref}). Taker commissions erode statistical edge."

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
        needs_confirmation = cand.get('requires_user_confirmation')
        if (needs_confirmation is True or str(needs_confirmation).lower() == 'true') and not confirmed:
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

    return True, reason, cand


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
# -----------------------------------------------------------------------------
PENDING_ENTRY_TIMEOUT_SECONDS = 5400    # desk order timeout (60-90 min) for unfilled resting entries
GUARDIAN_MAX_INTERVAL_FOR_RESTING = 120 # resting entries require a guardian LOOP (--interval <= 120s) for the env
PENDING_MISSING_GRACE_SECONDS = 60      # "entry gone, no position" must persist this long before a record is dropped
PENDING_ENTRIES_SCHEMA_VERSION = 1


def pending_entries_path():
    return os.path.join(_workspace_dir(), 'logs', 'pending_entries.json')


def pending_entry_key(target_env, symbol, entry_id):
    return f"{target_env}:{str(symbol).upper()}:{entry_id}"


def load_pending_entries():
    """Returns (entries, error). A missing registry is empty; an unreadable or malformed one is an error
    (callers fail closed: no new resting entry is accepted and no record is ever dropped). An empty registry is
    only trusted together with the exchange: find_unregistered_resting_entries (PROD executor gate and guardian
    cycle) reports opening orders resting on the exchange without a record."""
    path = pending_entries_path()
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {}, f"pending entries registry unreadable ({e})"
    if not isinstance(data, dict) or not isinstance(data.get('entries'), dict):
        return {}, "pending entries registry malformed"
    return data['entries'], None


def update_pending_entries(mutate):
    """Read-modify-write of logs/pending_entries.json: re-reads the registry, applies mutate(entries) and writes
    it atomically. Raises on an unreadable registry or a failed write."""
    entries, err = load_pending_entries()
    if err:
        raise IOError(err)
    mutate(entries)
    from utils.atomic_writer import atomic_write_json
    atomic_write_json(pending_entries_path(), {"schema_version": PENDING_ENTRIES_SCHEMA_VERSION, "entries": entries})
    return entries


def register_resting_entry(kind, entry_id, symbol, direction, entry_side, exit_side, target_env, price, total_qty,
                           sl_price, tp1_price, tp2_price, leverage, is_yolo, margin_usdt):
    """Records a resting entry in logs/pending_entries.json. Returns (key, record); raises on failure."""
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
    update_pending_entries(lambda entries: entries.__setitem__(key, record))
    return key, record


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
    last cycle no older than 2 * interval_seconds + 30s. Read-only."""
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
    if ts <= 0 or age > max_age or age < -60:
        return False, f"guardian state is stale ({age}s old, limit {max_age}s for a {interval}s loop)"
    return True, f"guardian alive ({age}s old, {interval}s loop)"


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


def find_unregistered_resting_entries(target_env):
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
    entry the executor places and registers meanwhile is not reported.
    """
    listed = []
    for source, endpoint, kind in (('algo', '/fapi/v1/openAlgoOrders', 'STOP_MARKET'),
                                   ('order', '/fapi/v1/openOrders', 'LIMIT')):
        try:
            res = send_signed_request('GET', endpoint, target_env=target_env)
        except Exception as e:
            res = {"error": str(e)}
        if not isinstance(res, list):
            return [], f"{endpoint} query failed: {res}"
        listed.extend((source, kind, o) for o in res if isinstance(o, dict)
                      and not _truthy(o.get('closePosition')) and not _truthy(o.get('reduceOnly')))
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
        unknown.append({"symbol": sym, "source": source, "kind": kind, "id": oid, "type": _order_type(o),
                        "side": o.get('side'), "price": _trigger_price(o) or _to_float(o.get('price')),
                        "quantity": o.get('quantity') or o.get('origQty')})
    return unknown, None


def describe_unregistered_entries(unknown):
    return ", ".join(f"{u['symbol']} {u['kind']} {u['source']} {u['id']} ({u['type']} {u['side']} @ {u['price']})"
                     for u in unknown)


def check_unregistered_resting_entries(target_env):
    """
    PROD gate for EVERY new entry, before any write (Issue #46): no opening order may rest on the exchange
    without a record in logs/pending_entries.json for target_env (deleted / lost registry, manual order). Fails
    closed on a query error. Returns (ok, message_or_None). Read-only (find_unregistered_resting_entries).
    """
    unknown, err = find_unregistered_resting_entries(target_env)
    if err:
        return False, (f"ENTRY REJECTED: FAIL-CLOSED — cannot cross-check resting entries on the exchange against "
                       f"logs/pending_entries.json ({err}).")
    if unknown:
        return False, (f"ENTRY REJECTED: FAIL-CLOSED — {len(unknown)} resting entry order(s) on the exchange are not in "
                       f"logs/pending_entries.json and would get no Stop Loss on fill: {describe_unregistered_entries(unknown)}. "
                       "Cancel them (or restore their registry records) before any new entry.")
    return True, None


def check_max_open_positions(prof, target_env):
    """
    Gate 0A (max_open_positions), evaluated before any write. Committed slots = open positions (logs/session_state.json)
    + symbols with a pending resting entry for target_env in logs/pending_entries.json that have no open position yet
    (a partially filled LIMIT has both a position and a record: counted once). A new entry is rejected when committed
    slots >= profile max_open_positions. A missing registry counts zero pending entries (in PROD the executor then
    rejects any entry resting on the exchange without a record, check_unregistered_resting_entries); an unreadable
    one fails closed in PROD (TESTNET counts zero). Returns (ok, message_or_None). Read-only, file-only.
    """
    target_env = resolve_env(target_env)
    is_testnet = str(target_env).lower() == 'testnet'
    max_open_positions = int((prof or {}).get("max_open_positions", 3))
    prefix = f"MECHANICAL HARD GATE REJECTION: Max open positions limit ({max_open_positions})"

    state_file = os.path.join(_workspace_dir(), 'logs', 'session_state.json')
    open_count = 0
    open_symbols = set()
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                state_data_pos = json.load(f)
            open_count = state_data_pos.get('portfolio_exposure', {}).get('total_active_positions')
            if open_count is None:
                open_count = len(state_data_pos.get('active_positions', []))
            open_count = int(open_count)
        except Exception:
            open_count = 0
        else:
            try:
                open_symbols = {str(p.get('symbol')).upper() for p in state_data_pos.get('active_positions') or []
                                if isinstance(p, dict) and p.get('symbol')}
            except Exception:
                open_symbols = set()

    entries, err = load_pending_entries()
    if err:
        if not is_testnet:
            return False, (f"{prefix}: FAIL-CLOSED — {err}; cannot count pending resting entries "
                           "(logs/pending_entries.json). Order blocked.")
        entries = {}
    pending_symbols = set()
    for rec in entries.values():
        if isinstance(rec, dict) and rec.get('target_env') == target_env:
            sym = str(rec.get('symbol', '')).upper()
            if sym and sym not in open_symbols:
                pending_symbols.add(sym)
    pending_count = len(pending_symbols)

    if open_count + pending_count >= max_open_positions:
        return False, (f"{prefix} reached (open {open_count} + pending {pending_count} >= max {max_open_positions}).")
    return True, None


def check_resting_entry_gates(symbol, target_env):
    """
    PROD gates for entries that rest on the book (untriggered STOP_MARKET, LIMIT), evaluated before any write.
    Their SL/TPs are only placed on fill by protect_pending_entries, so:
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
            "only gets its Stop Loss on fill; start the guardian first: "
            f"`python3 scripts/loops/position_guardian_loop.py --interval 60 --env {target_env}`."
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
          * no stop at all -> place the planned SL (closePosition); if it cannot be verified, cancel the entry and
            close the position reduce-only (auto-destruct);
          * every existing stop looser than the plan (e.g. the 2.5% orphan heal) -> replace with the planned SL,
            place-then-cancel;
          * an existing stop at or tighter than the plan (trailing, break-even) -> kept; when a partial LIMIT fill
            grew beyond the quantity covered (or once when the coverage is unknown, i.e. no sl_qty yet), it is
            resized place-then-cancel at that tighter price for the full current size.
          A replace/resize that cannot be verified keeps the existing stop(s) (no auto-destruct) and is retried.
          If the planned SL is already crossed (mark beyond it, or -2021 on placement), the position is closed
          reduce-only at MARKET without cancelling existing stops first; leftovers are cancelled only once flat.
        Once the entry order is gone, TP1/TP2 are placed reduce-only from the ACTUAL position size (idempotent:
        placed TP ids are saved first and only a missing TP is retried), an audit record is appended and the record
        dropped. A partially filled LIMIT keeps its record (remainder cancelled at expiry, TPs on a later run).
      - not filled and still open: cancelled once expires_at_ts is reached, else kept.
      - not filled and no longer open: marked missing_since_ts and dropped only if still so on a run at least
        PENDING_MISSING_GRACE_SECONDS later (positionRisk can lag behind a trigger).
    Any query error keeps the record (fail closed). dry_run reports the decisions without any write.
    Returns {"ok", "env", "dry_run", "actions": [{"type", "key", "symbol", "success", "dry_run", "detail"}],
             "errors": [{"key", "symbol", "stage", "error"}]}
    with action types pending_protect_sl | pending_tp_placed | pending_abort | pending_timeout_cancel | pending_dropped |
    pending_sl_crossed_close.
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
    for key in sorted(entries):
        rec = entries[key]
        if keys is not None and key not in keys:
            continue
        if not isinstance(rec, dict) or rec.get('target_env') != target_env:
            continue
        try:
            _protect_pending_entry(key, rec, target_env, dry_run, now, out)
        except Exception as e:
            out["errors"].append({"key": key, "symbol": rec.get('symbol'), "stage": "exception",
                                  "error": f"{type(e).__name__}: {e}"})
    out["ok"] = not out["errors"]
    return out


def _protect_pending_entry(key, rec, target_env, dry_run, now, out):
    sym = str(rec['symbol']).upper()
    kind = 'STOP_MARKET' if str(rec.get('kind', '')).upper() == 'STOP_MARKET' else 'LIMIT'
    entry_id = str(rec['entry_id'])
    is_long = str(rec.get('direction', '')).upper() == 'LONG'
    direction = 'LONG' if is_long else 'SHORT'
    exit_side = 'SELL' if is_long else 'BUY'
    sl_p = float(rec['sl_price'])
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

    # Open orders first, then positions: a trigger between the two reads shows up as a position.
    orders_ep = '/fapi/v1/openAlgoOrders' if kind == 'STOP_MARKET' else '/fapi/v1/openOrders'
    open_res = send_signed_request('GET', orders_ep, {'symbol': sym}, target_env=target_env)
    if not isinstance(open_res, list):
        return fail("orders_query", f"{orders_ep} query failed: {open_res}")
    entry_open = any(isinstance(o, dict) and str(_order_id(o)) == entry_id for o in open_res)
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
        save(missing_since_ts=None)

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
            act("pending_dropped", True, reason="entry_not_open_no_position", kind=kind, entry_id=entry_id,
                missing_since_ts=since,
                message="Entry no longer open and no position: cancelled or expired outside the desk.")
            return drop()
        if now < expires:
            return None
        if dry_run:
            return act("pending_timeout_cancel", False, kind=kind, entry_id=entry_id, expires_at_ts=expires)
        ok, res = cancel_resting_entry(sym, kind, entry_id, target_env=target_env)
        act("pending_timeout_cancel", ok, kind=kind, entry_id=entry_id, expires_at_ts=expires, result=res)
        if not ok:
            return fail("timeout_cancel", f"Cancel of expired entry {entry_id} failed: {res}")
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
    if not stops:
        mode, target_p, old_stops = 'place', sl_p, []
    elif abs(ex_p - sl_p) > tol and is_tighter_stop(sl_p, ex_p, is_long):
        mode, target_p, old_stops = 'replace', sl_p, stops   # every stop looser than plan (e.g. 2.5% orphan heal)
    elif not covered or qty > covered * 1.000001:
        # Kept (tighter) stop, resized place-then-cancel at ITS price for the full current size when the partial fill
        # grew, or once when its coverage is unknown (a stop not placed here, e.g. an MCP orphan heal sized
        # reduce-only for a partial fill); sl_qty is then the baseline.
        mode, target_p, old_stops = 'resize', ex_p, stops
    else:
        mode, target_p, old_stops = None, ex_p, []           # existing stop at or tighter than plan: keep it
    sl_stop = stop_summary(tightest) if tightest else None
    mark_p = _to_float(position.get('markPrice'))

    def crossed_close(reason, placement=None):
        """The planned SL is already crossed: close reduce-only at MARKET for the actual size WITHOUT cancelling the
        existing stops first; only once flat are leftover stops/TPs and the entry remainder cancelled."""
        detail = dict(reason=reason, planned_sl_price=sl_p, mark_price=mark_p or None, quantity=qty,
                      kept_stops=[stop_summary(s) for s in stops], placement=placement)
        if dry_run:
            return act("pending_sl_crossed_close", False, **detail)
        try:
            close = send_signed_request('POST', '/fapi/v1/order', {'symbol': sym, 'side': exit_side, 'type': 'MARKET',
                                                                  'quantity': qty, 'reduceOnly': 'true'}, target_env=target_env)
        except Exception as e:
            close = {"error": str(e)}
        flat = _market_order_accepted(close) and _wait_until_flat(sym, is_long, target_env)
        if not flat:
            act("pending_sl_crossed_close", False, close=close, flat=False, **detail)
            return fail("sl_crossed_close", f"Planned SL {sl_p} crossed for {sym} but the reduce-only close was not "
                                            f"confirmed flat ({close}); existing stop(s) and record kept.")
        entry_cancel_ok, entry_cancel_res = (cancel_resting_entry(sym, kind, entry_id, target_env=target_env)
                                             if entry_open else (True, None))
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
        if mode == 'place':
            try:
                placement = place_algo_stop_loss(sym, exit_side, target_p, target_env=target_env)
            except Exception as e:
                placement = {"error": f"placement exception: {e}"}
            placed_id = _order_id(placement) if isinstance(placement, dict) else None
            verified, info = wait_for_stop_confirmation(sym, exit_side, target_p, algo_id=placed_id, tick_size=tick,
                                                        target_env=target_env)
            new_stop = stop_summary(info) if verified else None
        else:
            rep = replace_protective_stop(sym, exit_side, target_p, qty_str, old_stops, target_env=target_env, tick_size=tick)
            placement, verified, new_stop = rep.get('placement'), bool(rep.get('success')), rep.get('new_stop')
            cancelled_old = rep.get('cancelled_old_stop_ids', [])
            for ce in rep.get('cancel_errors', []):
                fail("protect_sl_cancel_old", ce)
        act("pending_protect_sl", verified, mode=mode, sl_price=target_p, planned_sl_price=sl_p, quantity=qty,
            verified=verified, new_stop=new_stop, cancelled_old_stop_ids=cancelled_old, placement=placement,
            coverage_unknown=(mode == 'resize' and not covered))
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
    tp1_p, tp2_p = _to_float(rec.get('tp1_price')), _to_float(rec.get('tp2_price'))
    ids = {'tp1_order_id': rec.get('tp1_order_id'), 'tp2_order_id': rec.get('tp2_order_id')}
    if rec.get('tp1_qty') is not None and rec.get('tp2_qty') is not None:
        tp1_qty, tp2_qty = _to_float(rec['tp1_qty']), _to_float(rec['tp2_qty'])   # split fixed on the first attempt
    elif not filters:
        return fail("filters", f"Symbol filters unavailable for {sym}; TPs deferred to the next run.")
    else:
        tp1_qty, tp2_qty = split_take_profit_quantities(qty, filters, entry_px)
    if not rec.get('tp_placed'):
        need1 = tp1_qty > 0 and ids['tp1_order_id'] is None
        need2 = tp2_qty > 0 and ids['tp2_order_id'] is None
        if dry_run:
            return act("pending_tp_placed", False, tp1_price=tp1_p, tp1_qty=tp1_qty, tp2_price=tp2_p, tp2_qty=tp2_qty,
                       quantity=qty, place_tp1=need1, place_tp2=need2)
        retried = ids['tp1_order_id'] is not None or ids['tp2_order_id'] is not None
        o1, o2 = place_take_profit_orders(sym, exit_side, tp1_p, tp2_p, tp1_qty if need1 else 0, tp2_qty if need2 else 0,
                                          target_env=target_env)
        for name, order in (('tp1_order_id', o1), ('tp2_order_id', o2)):
            if isinstance(order, dict) and 'orderId' in order and not _is_api_error(order):
                ids[name] = order['orderId']
        tp_ok = (tp1_qty <= 0 or ids['tp1_order_id'] is not None) and (tp2_qty <= 0 or ids['tp2_order_id'] is not None)
        # Persist what was placed BEFORE anything else, so a later run never duplicates a TP.
        save(tp1_qty=tp1_qty, tp2_qty=tp2_qty, tp_placed=tp_ok, **ids)
        act("pending_tp_placed", tp_ok, tp1_price=tp1_p, tp1_qty=tp1_qty, tp2_price=tp2_p, tp2_qty=tp2_qty, quantity=qty,
            retried=retried, record_dropped=tp_ok, **ids)
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
        try:
            append_trade_audit_record(record, rec.get('margin_usdt'))
        except Exception as e:
            return fail("audit", f"Audit append failed ({e}); record kept, only the audit/drop is retried.")
        save(audit_done=True)
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

    try:
        import quant_risk_engine as qre
        account_equity = qre.get_account_equity(target_env)
    except Exception as e:
        if is_prod:
            return {"success": False, "hard_gate_rejection": True,
                    "error": f"FAIL-CLOSED: Cannot verify account equity for PROD ({e}). Order blocked."}
        account_equity = 10000.0  # Safe testnet sandbox fallback

    try:
        import user_profile as up
        prof = up.load_user_profile()
        max_margin_ratio = float(prof.get("max_margin_ratio", 0.30))
    except Exception:
        prof = {}
        max_margin_ratio = 0.30

    # Dynamic margin scaling: if margin_usdt is None or default 100.0, scale dynamically
    if margin_usdt is None or margin_usdt == 100.0:
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

    # 1b. Pending resting entries (Issue #33, PROD): no new entry of any type on a symbol with a pending resting
    # entry (or an unreadable registry). An untriggered STOP_MARKET or a LIMIT entry rests on the book and only gets
    # its SL/TPs on fill (--protect-pending / position guardian loop). Checked before any write.
    if is_prod:
        pend_ok, pend_err = check_pending_entry_conflict(symbol, target_env)
        if not pend_ok:
            return {"success": False, "hard_gate_rejection": True, "error": pend_err}
    # 1c. Max open positions (Gate 0A, Issue #38): open positions + pending resting entries, checked before any write
    # (check_mechanical_gates re-checks it after sizing).
    slots_ok, slots_err = check_max_open_positions(prof, target_env)
    if not slots_ok:
        return {"success": False, "hard_gate_rejection": True, "error": slots_err}
    # 1d. Unregistered resting entries (Issue #46, PROD): a missing registry reads as empty, so every opening order
    # resting on the exchange must have its logs/pending_entries.json record (two all-symbol GETs, fail closed).
    if is_prod:
        unreg_ok, unreg_err = check_unregistered_resting_entries(target_env)
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
    if total_qty * effective_entry < min_notional:
        bumped_qty = round_step(total_qty + filters['stepSize'], filters['stepSize'], filters['precision_qty'])
        if bumped_qty * effective_entry >= min_notional:
            total_qty = bumped_qty
    if total_qty < filters['minQty']:
        return {"success": False, "error": f"Quantity {total_qty} lower than minimum allowed {filters['minQty']}"}

    # 4. MECHANICAL HARD GATES VERIFICATION (incl. liquidation gate with the confirmed effective leverage)
    liq_entry_price = effective_entry
    mmr, maint_amount, mmr_source = get_maint_margin_bracket(symbol, total_qty * liq_entry_price, target_env=target_env)
    gate_ok, gate_err = check_mechanical_gates(
        direction, cur_price, sl_price, tp1_price, total_qty, effective_leverage,
        bypass_delta_gate=bypass_delta_gate, target_env=target_env, is_yolo=is_yolo,
        maint_margin_ratio=mmr, maint_amount=maint_amount, mmr_source=mmr_source,
        liq_entry_price=liq_entry_price, entry_price=effective_entry
    )
    if not gate_ok:
        return {"success": False, "hard_gate_rejection": True, "error": gate_err}

    # 5. Split TPs asymmetrically (30% TP1 / 70% TP2) to preserve positive right-tail skewness
    # and prevent premature profit truncation.
    tp1_qty, tp2_qty = split_take_profit_quantities(total_qty, filters, cur_price)

    # 6. Round SL and TP prices
    sl_p = round_price(sl_price, filters['tickSize'], filters['precision_price'])
    tp1_p = round_price(tp1_price, filters['tickSize'], filters['precision_price'])
    tp2_p = round_price(tp2_price, filters['tickSize'], filters['precision_price'])

    def register_or_cancel(kind, entry_id, price):
        """Records the resting entry for post-fill protection; if that fails the entry is cancelled (fail closed)."""
        try:
            key, rec = register_resting_entry(
                kind, entry_id, symbol, direction, entry_side, exit_side, target_env, price, total_qty,
                sl_p, tp1_p, tp2_p, effective_leverage, is_yolo, margin_usdt)
            return key, rec, None
        except Exception as e:
            cancelled, cancel_res = cancel_resting_entry(symbol, kind, entry_id, target_env=target_env)
            state = "the entry was cancelled" if cancelled else "CANCEL ALSO FAILED: cancel it manually now"
            return None, None, {
                "success": False,
                "pending_registry_failure": True,
                "orderId": entry_id,
                "entry_cancelled": cancelled,
                "cancel_result": cancel_res,
                "error": (f"FAIL-CLOSED: {kind} entry {entry_id} for {symbol} was placed but could not be registered "
                          f"for post-fill protection in logs/pending_entries.json ({e}); {state}."),
            }

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
                    key, rec, failure = register_or_cancel('STOP_MARKET', order_id, trigger_p)
                    if failure:
                        return failure
                    return {
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
                        "message": (f"Conditional STOP_MARKET entry placed at {trigger_p} (algo order {order_id}). "
                                    "Its SL/TPs are placed on fill by `execute_futures_trade.py --protect-pending` / the "
                                    f"position guardian loop; unfilled after {PENDING_ENTRY_TIMEOUT_SECONDS // 60} min it is cancelled.")
                    }
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
        key, rec, failure = register_or_cancel('LIMIT', entry_order.get('orderId'), lim_p)
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
            "message": (f"LIMIT order placed at {lim_p} (order ID: {entry_order.get('orderId')}). SL/TP orders deferred until fill "
                        "(prevents -2022) and placed on fill by `execute_futures_trade.py --protect-pending` / the position "
                        f"guardian loop; unfilled after {PENDING_ENTRY_TIMEOUT_SECONDS // 60} min it is cancelled.")
        }
        if entry_status == 'PARTIALLY_FILLED':
            entry_id = entry_order.get('orderId')
            head = f"LIMIT order PARTIALLY_FILLED at {lim_p} (order ID: {entry_id}). "
            exec_qty = _to_float(entry_order.get('executedQty'))
            if exec_qty > 0:
                # Protect the filled part NOW from the entry response (no dependency on positionRisk visibility):
                # closePosition with HMAC keys; quantity-based reduce-only via the MCP gateway (which needs a quantity).
                try:
                    sl_order = place_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env,
                                                    quantity=exec_qty if uses_mcp_gateway(target_env) else None)
                except Exception as e:
                    sl_order = {"error": f"placement exception: {e}"}
                placed_id = _order_id(sl_order) if isinstance(sl_order, dict) else None
                sl_ok, sl_info = wait_for_stop_confirmation(symbol, exit_side, sl_p, algo_id=placed_id,
                                                            tick_size=filters.get('tickSize'), target_env=target_env)
                result["partial_fill_protection"] = {"executed_qty": exec_qty, "sl_order": sl_order, "verified": sl_ok,
                                                     "new_stop": stop_summary(sl_info) if sl_ok else None}
                result["partial_fill_protected"] = sl_ok
                if not sl_ok:
                    # Fail-safe auto-destruct: cancel the resting remainder, then close the filled part.
                    entry_cancel_ok, entry_cancel_res = cancel_resting_entry(symbol, 'LIMIT', entry_id, target_env=target_env)
                    abort_exit = emergency_abort_market_close(symbol, exit_side, exec_qty, target_env=target_env)
                    log_emergency_abort(symbol, direction, exec_qty, sl_p, sl_order, abort_exit, target_env)
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
        sl_order = place_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)
        sl_verified, sl_info = verify_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)

        # Progressive retries (up to 3 attempts in ~2.8s) to absorb Mainnet indexing latency
        if not sl_verified:
            for retry_delay in [0.8, 1.0, 1.2]:
                time.sleep(retry_delay)
                if isinstance(sl_order, dict) and ("error" in sl_order or "code" in sl_order):
                    sl_order = place_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)
                sl_verified, sl_info = verify_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)
                if sl_verified:
                    break

        # ATOMIC AUTO-DESTRUCT / FAIL-SAFE PROTOCOL:
        # If Stop Loss is NOT verified after retries, ABORT IMMEDIATELY
        if not sl_verified:
            abort_exit = emergency_abort_market_close(symbol, exit_side, total_qty, target_env=target_env)
            log_emergency_abort(symbol, direction, total_qty, sl_p, sl_order, abort_exit, target_env)
            return {
                "success": False,
                "emergency_abort": True,
                "symbol": symbol,
                "error": f"CRITICAL FAIL-SAFE TRIGGERED: Stop Loss could not be confirmed after 3 attempts ({sl_order}). Position closed at MARKET immediately to eliminate unhedged exposure.",
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
    """
    target_env = resolve_env(target_env)
    pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', target_env=target_env)
    if not isinstance(pos_res, list):
        return {"error": f"Error querying positions: {pos_res}"}

    active = [p for p in pos_res if float(p.get('positionAmt', 0)) != 0]
    if not active:
        return {
            "total_active": 0,
            "orphans_count": 0,
            "all_protected": True,
            "message": "No open positions in account."
        }

    positions_report = []
    orphans = []

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

        # Query active protective stop algo orders on exchange
        active_sls, orders_err = get_open_stop_orders(sym, exit_side, target_env=target_env)

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
            "active_sl_orders": [_order_id(ao) for ao in active_sls],
            "sl_triggers": [_trigger_price(ao) for ao in active_sls]
        }
        if orders_err:
            info["orders_error"] = orders_err

        if not is_protected:
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
        "all_protected": len(orphans) == 0,
        "positions": positions_report
    }

def audit_and_auto_heal_orphans(target_env=None):
    """
    Audits all active positions and automatically heals any orphan positions lacking Stop Loss.
    """
    target_env = resolve_env(target_env)
    return audit_orphan_positions(target_env=target_env, auto_heal=True)

def close_position_market(symbol, target_env=None):
    target_env = resolve_env(target_env)
    # 1. Active position
    pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol}, target_env=target_env)
    active = [p for p in pos_res if float(p.get('positionAmt', 0)) != 0] if isinstance(pos_res, list) else []
    if not active:
        return {"success": False, "error": f"No open position in {symbol}"}

    amt = float(active[0]['positionAmt'])
    exit_side = 'SELL' if amt > 0 else 'BUY'
    qty = abs(amt)

    # 2. Cancel all standard open orders
    send_signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol}, target_env=target_env)

    # 3. Cancel all open algo orders via native REST
    open_algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}, target_env=target_env)
    if isinstance(open_algos, list):
        for ao in open_algos:
            aid = ao.get('algoId') or ao.get('orderId')
            if aid:
                send_signed_request('DELETE', '/fapi/v1/algoOrder', {'symbol': symbol, 'algoId': aid}, target_env=target_env)

    # 4. Market close with reduceOnly
    params = {
        'symbol': symbol,
        'side': exit_side,
        'type': 'MARKET',
        'quantity': qty,
        'reduceOnly': 'true'
    }
    res = send_signed_request('POST', '/fapi/v1/order', params, target_env=target_env)
    return {"success": True, "closed": res}

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
        if "error" in res or (res.get("orphans_count", 0) > 0 and not res.get("all_protected", False)):
            sys.exit(1)
        else:
            sys.exit(0)
        return

    # 3. Audit Orphans
    if args.audit_orphans:
        res = audit_orphan_positions(target_env=target_env, auto_heal=False)
        print(json.dumps(res, indent=2))
        if "error" in res:
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
