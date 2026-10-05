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
  trade deployment: {"success": bool, "symbol", "direction", "leverage", "entry_price", "total_qty",
      "sl_price", "tp1_price", "tp2_price", ..., "error"?: str, "hard_gate_rejection"?: bool}
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
                        'closePosition': bool(a.get('closePosition', False) or a.get('reduceOnly', False))
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
    """
    target_env = resolve_env(target_env)
    try:
        algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}, target_env=target_env)
        if isinstance(algos, list):
            for ao in algos:
                o_type = ao.get('orderType') or ao.get('type')
                if o_type in ['STOP_MARKET', 'STOP'] and ao.get('side') == exit_side:
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
                           maint_margin_ratio=None, maint_amount=0.0, mmr_source=None, liq_entry_price=None):
    """
    Mechanical Software Gates (Deterministic Precondition Validation).
    Verifies mathematical invariants and physically prevents execution if risk rules are violated.
    In TESTNET, free bypass of gates is permitted for testing, experiments, and stress tests.
    In PROD, gates are strict and inviolable.
    """
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

    # --- GATE 0A: Max Open Positions Gate ---
    max_open_positions = int(prof.get("max_open_positions", 3))
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
    state_file = os.path.join(log_dir, 'session_state.json')
    total_active_positions = 0
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                state_data_pos = json.load(f)
                total_active_positions = state_data_pos.get('portfolio_exposure', {}).get('total_active_positions')
                if total_active_positions is None:
                    total_active_positions = len(state_data_pos.get('active_positions', []))
                total_active_positions = int(total_active_positions)
        except Exception:
            total_active_positions = 0

    if total_active_positions >= max_open_positions:
        return False, f"MECHANICAL HARD GATE REJECTION: Max open positions limit ({max_open_positions}) reached."

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
        liq_entry_price if liq_entry_price else cur_price,
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
    potential_dollar_loss = abs(cur_price - sl_price) * total_qty
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
        margin_est = (cur_price * total_qty / max(leverage, 1))
        max_allowed_loss = max(3.75, margin_est * 0.35)
    else:
        max_allowed_loss = account_equity * risk_fraction * 1.25

    if potential_dollar_loss > max_allowed_loss:
        return False, f"MECHANICAL HARD GATE REJECTION: Monetary risk exceeds allowed cap (${potential_dollar_loss:.2f} > ${max_allowed_loss:.2f} USDT, equity: ${account_equity:.2f}, risk fraction: {risk_fraction*100:.2f}% + buffer). Adjust margin or position size."

    # --- GATE 3: Financial Friction and Fee Gate ---
    if tp1_price and not is_testnet:
        profit_pct_tp1 = abs(tp1_price - cur_price) / cur_price
        if profit_pct_tp1 < 0.0035:
            return False, f"MECHANICAL HARD GATE REJECTION: Distance to TP1 ({profit_pct_tp1*100:.2f}%) below 0.35% friction floor. Taker commissions erode statistical edge."

    return True, None

def enforce_evaluation_dossier(symbol, direction, target_env=None, bypass_eval_gate=False, confirmed=False, base_dir=None):
    """
    Clean-room evaluation gate for NEW positions (never used by close/breakeven/trailing/audit/heal/cancel paths).
    Delegates to utils.dossier_provenance.validate_dossier_for_trade (same gate as the PreToolUse hook).
    - PROD: fail closed. --bypass-eval-gate is refused. If the evaluator flagged the candidate as requiring
      user confirmation, `confirmed=True` is required.
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

    return True, reason, cand


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
        symbol, direction, target_env=target_env, bypass_eval_gate=bypass_eval_gate, confirmed=confirmed
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

    # Fallback / default SL and TP calculations if not provided or 0
    if sl_price is None or float(sl_price) <= 0:
        sl_price = cur_price * (1.0 - 0.02) if is_long else cur_price * (1.0 + 0.02)
    if tp1_price is None or float(tp1_price) <= 0:
        tp1_price = cur_price * (1.0 + 0.03) if is_long else cur_price * (1.0 - 0.03)
    if tp2_price is None or float(tp2_price) <= 0:
        tp2_price = cur_price * (1.0 + 0.06) if is_long else cur_price * (1.0 - 0.06)

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

    # 3. Calculate exact token quantity using verified effective leverage
    notional_target = margin_usdt * effective_leverage
    raw_qty = notional_target / cur_price
    total_qty = round_step(raw_qty, filters['stepSize'], filters['precision_qty'])
    min_notional = filters.get('minNotional', 5.0)
    if total_qty * cur_price < min_notional:
        bumped_qty = round_step(total_qty + filters['stepSize'], filters['stepSize'], filters['precision_qty'])
        if bumped_qty * cur_price >= min_notional:
            total_qty = bumped_qty
    if total_qty < filters['minQty']:
        return {"success": False, "error": f"Quantity {total_qty} lower than minimum allowed {filters['minQty']}"}

    # 4. MECHANICAL HARD GATES VERIFICATION (incl. liquidation gate with the confirmed effective leverage)
    liq_entry_price = cur_price
    if str(order_type).upper() == 'LIMIT' and limit_price:
        liq_entry_price = float(limit_price)
    elif str(order_type).upper() == 'STOP_MARKET' and trigger_price:
        liq_entry_price = float(trigger_price)
    mmr, maint_amount, mmr_source = get_maint_margin_bracket(symbol, total_qty * liq_entry_price, target_env=target_env)
    gate_ok, gate_err = check_mechanical_gates(
        direction, cur_price, sl_price, tp1_price, total_qty, effective_leverage,
        bypass_delta_gate=bypass_delta_gate, target_env=target_env, is_yolo=is_yolo,
        maint_margin_ratio=mmr, maint_amount=maint_amount, mmr_source=mmr_source,
        liq_entry_price=liq_entry_price
    )
    if not gate_ok:
        return {"success": False, "hard_gate_rejection": True, "error": gate_err}

    # 5. Split TPs asymmetrically (30% TP1 / 70% TP2) to preserve positive right-tail skewness
    # and prevent premature profit truncation.
    tp1_qty = round_step(total_qty * 0.30, filters['stepSize'], filters['precision_qty'])
    if tp1_qty < filters['minQty']:
        tp1_qty = filters['minQty']
    if tp1_qty * cur_price < min_notional:
        needed_qty = round_step(math.ceil(min_notional / cur_price / filters['stepSize']) * filters['stepSize'], filters['stepSize'], filters['precision_qty'])
        if needed_qty < total_qty:
            tp1_qty = needed_qty

    tp2_qty = round_step(total_qty - tp1_qty, filters['stepSize'], filters['precision_qty'])
    if tp2_qty < filters['minQty']:
        # Fallback to 50/50 if position size is too small to split 30/70 while respecting minQty
        tp1_qty = round_step(total_qty / 2, filters['stepSize'], filters['precision_qty'])
        tp2_qty = round_step(total_qty - tp1_qty, filters['stepSize'], filters['precision_qty'])

    # 6. Round SL and TP prices
    sl_p = round_price(sl_price, filters['tickSize'], filters['precision_price'])
    tp1_p = round_price(tp1_price, filters['tickSize'], filters['precision_price'])
    tp2_p = round_price(tp2_price, filters['tickSize'], filters['precision_price'])

    # 7. Technical Trigger Validation (Confirmation breakout)
    if trigger_price is not None and trigger_price > 0:
        trigger_p = round_price(trigger_price, filters['tickSize'], filters['precision_price'])
        trigger_breached = (cur_price >= trigger_p) if is_long else (cur_price <= trigger_p)

        if not trigger_breached:
            if order_type.upper() == 'STOP_MARKET':
                entry_params = {
                    'symbol': symbol,
                    'side': entry_side,
                    'type': 'STOP_MARKET',
                    'stopPrice': trigger_p,
                    'quantity': total_qty,
                    'closePosition': 'false'
                }
                cond_order = send_signed_request('POST', '/fapi/v1/order', entry_params, target_env=target_env)
                order_id = cond_order.get('algoId') or cond_order.get('orderId') if isinstance(cond_order, dict) else None
                if order_id:
                    return {
                        "success": True,
                        "conditional_entry": True,
                        "orderId": order_id,
                        "symbol": symbol,
                        "direction": str(direction).upper(),
                        "trigger_price": trigger_p,
                        "cur_price": cur_price,
                        "message": f"Conditional STOP_MARKET order placed at {trigger_p}. Will trigger upon institutional wick breakout. SL/TP deferred to fill."
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

    # Protection against premature reduceOnly orders on resting LIMIT orders (Finding 9)
    if order_type.upper() == 'LIMIT' and entry_order.get('status') == 'NEW':
        return {
            "success": True,
            "pending_limit_entry": True,
            "orderId": entry_order.get('orderId'),
            "symbol": symbol,
            "direction": str(direction).upper(),
            "limit_price": lim_p,
            "quantity": total_qty,
            "status": "NEW",
            "message": f"LIMIT order placed at {lim_p} (order ID: {entry_order.get('orderId')}). TP reduce-only orders deferred until fill to prevent -2022 rejection."
        }

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

        # 10. Execute TP1 (LIMIT, 30% position, Reduce-Only)
        tp1_order = None
        if tp1_qty > 0:
            tp1_params = {
                'symbol': symbol,
                'side': exit_side,
                'type': 'LIMIT',
                'price': tp1_p,
                'quantity': tp1_qty,
                'timeInForce': 'GTC',
                'reduceOnly': 'true'
            }
            tp1_order = send_signed_request('POST', '/fapi/v1/order', tp1_params, target_env=target_env)

        # 11. Execute TP2 (LIMIT, 70% position remaining, Reduce-Only)
        tp2_order = None
        if tp2_qty > 0:
            tp2_params = {
                'symbol': symbol,
                'side': exit_side,
                'type': 'LIMIT',
                'price': tp2_p,
                'quantity': tp2_qty,
                'timeInForce': 'GTC',
                'reduceOnly': 'true'
            }
            tp2_order = send_signed_request('POST', '/fapi/v1/order', tp2_params, target_env=target_env)

        # Log to local audit ledger with canonical provenance and atomic writing
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        audit_file = os.path.join(log_dir, 'trades_audit.jsonl')
        
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
    parser.add_argument("--positions", action="store_true", help="Read-only list of open positions with attached SL/TP orders")
    parser.add_argument("--move-breakeven", "--move_breakeven", action="store_true", dest="move_breakeven", help="Move the Stop Loss of --symbol to True Net Break-Even (place-then-cancel)")
    parser.add_argument("--force", action="store_true", help="With --move-breakeven: override the YOLO-before-TP1 and anti-truncation rules")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit machine-readable JSON (schemas in the module docstring)")

    args = parser.parse_args()

    new_modes = [m for m in ("positions", "move_breakeven") if getattr(args, m)]
    other_modes = [m for m in ("close_position", "audit_orphans", "auto_heal") if getattr(args, m)]
    if new_modes and (len(new_modes) > 1 or other_modes or args.direction):
        print(json.dumps({"success": False, "error": "--positions and --move-breakeven are exclusive modes; they cannot be combined with other modes or --direction."}, indent=2))
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
