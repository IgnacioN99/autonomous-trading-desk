#!/usr/bin/env python3
"""
execute_futures_trade.py - Execution Engine and Risk Management Harness for Binance Futures.
Supports Testnet and Prod, strict filter calculations, symmetric orders, and verified Algo Stop Loss.
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


def place_algo_stop_loss(symbol, exit_side, sl_price, target_env=None):
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
        'closePosition': 'true'
    }
    res = send_signed_request('POST', '/fapi/v1/algoOrder', params, target_env=target_env)
    if isinstance(res, dict) and ('algoId' in res or 'orderId' in res):
        return res
    profile = 'testnet' if target_env == 'testnet' else 'prod'
    cmd = [
        'binance-cli', 'futures-usds', 'new-algo-order',
        '--algo-type', 'CONDITIONAL',
        '--symbol', symbol,
        '--side', exit_side,
        '--type', 'STOP_MARKET',
        '--trigger-price', str(sl_price),
        '--close-position', 'true',
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

def log_emergency_abort(symbol, direction, qty, sl_p, sl_order, abort_exit, target_env=None):
    target_env = resolve_env(target_env)
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
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
            log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
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

def move_sl_to_breakeven(symbol, target_env=None):
    target_env = resolve_env(target_env)
    pos_res = send_signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol}, target_env=target_env)
    active = [p for p in pos_res if float(p.get('positionAmt', 0)) != 0] if isinstance(pos_res, list) else []
    if not active:
        return {"success": False, "error": f"No active open position for {symbol}"}

    pos = active[0]
    entry_p = float(pos['entryPrice'])
    amt = float(pos['positionAmt'])
    exit_side = 'SELL' if amt > 0 else 'BUY'

    # Query prior active SL algo orders
    open_algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}, target_env=target_env)
    old_sl_orders = []
    if isinstance(open_algos, list):
        for ao in open_algos:
            if ao.get('orderType') in ['STOP_MARKET', 'STOP'] and ao.get('algoId'):
                old_sl_orders.append(ao)

    # Round BE price with fee buffer (True Net Break-Even: covers taker fees 0.10% + slippage 0.02%)
    fee_buffer = 0.0012
    is_long = amt > 0
    raw_be = entry_p * (1.0 + fee_buffer) if is_long else entry_p * (1.0 - fee_buffer)
    filters = get_symbol_filters(symbol, target_env=target_env)
    be_price = round_price(raw_be, filters['tickSize'], filters['precision_price']) if filters else raw_be

    # Cancel previous SL
    for ao in old_sl_orders:
        send_signed_request('DELETE', '/fapi/v1/algoOrder', {'symbol': symbol, 'algoId': ao['algoId']}, target_env=target_env)

    # Place new SL at Breakeven
    new_sl = place_algo_stop_loss(symbol, exit_side, be_price, target_env=target_env)
    verified, _ = verify_algo_stop_loss(symbol, exit_side, be_price, target_env=target_env)

    # SAFETY ROLLBACK: If placing new SL fails, immediately restore previous SL
    if not verified and old_sl_orders:
        prev_trig = float(old_sl_orders[0].get('triggerPrice', 0))
        if prev_trig > 0:
            rollback_sl = place_algo_stop_loss(symbol, exit_side, prev_trig, target_env=target_env)
            return {
                "success": False,
                "error": f"Failed to move to Break-Even ({new_sl}). Rollback executed: Stop Loss restored at {prev_trig}."
            }

    return {
        "success": True,
        "symbol": symbol,
        "message": f"Stop Loss moved to Break-Even ({be_price}) for position {pos['positionAmt']} {symbol}",
        "entry_price": entry_p,
        "new_sl_price": be_price,
        "algo_order": new_sl
    }

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

        # Query active algo orders on exchange
        algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': sym}, target_env=target_env)
        active_sls = []
        if isinstance(algos, list):
            for ao in algos:
                if ao.get('orderType') in ['STOP_MARKET', 'STOP'] and ao.get('side') == exit_side:
                    active_sls.append(ao)

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
            "active_sl_orders": [ao.get('algoId') for ao in active_sls],
            "sl_triggers": [float(ao.get('triggerPrice', 0)) for ao in active_sls]
        }

        if not is_protected:
            orphans.append(info)
            if auto_heal:
                filters = get_symbol_filters(sym, target_env=target_env)
                if filters:
                    # 2.5% emergency buffer
                    dist = 0.025
                    raw_sl = entry_p * (1 - dist) if pos_dir == 'LONG' else entry_p * (1 + dist)
                    heal_sl = round_price(raw_sl, filters['tickSize'], filters['precision_price'])
                    heal_res = place_algo_stop_loss(sym, exit_side, heal_sl, target_env=target_env)
                    verified, _ = verify_algo_stop_loss(sym, exit_side, heal_sl, target_env=target_env)
                    info["auto_heal_attempted"] = True
                    info["auto_heal_verified"] = verified
                    info["healed_sl_price"] = heal_sl
                    if verified:
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
    parser.add_argument("--env", type=str, choices=["prod", "testnet"], default=None, help="Target environment ('prod' or 'testnet')")
    parser.add_argument("--bypass-eval-gate", "--bypass_eval_gate", action="store_true", dest="bypass_eval_gate", help="Bypass clean-room evaluation gate (TESTNET only; refused in PROD)")
    parser.add_argument("--bypass-delta-gate", "--bypass_delta_gate", action="store_true", dest="bypass_delta_gate", help="Bypass delta-neutral gate (Testnet only)")
    parser.add_argument("--is-yolo", "--is_yolo", action="store_true", dest="is_yolo", help="Mark trade as YOLO moonshot (authorizes leverage > 5x)")
    parser.add_argument("--confirmed", "--user-confirmed", action="store_true", dest="confirmed", help="Explicit human confirmation for live order in PROD")
    parser.add_argument("--close-position", "--close_position", action="store_true", dest="close_position", help="Close open position at market with reduceOnly")
    parser.add_argument("--audit-orphans", "--audit_orphans", action="store_true", dest="audit_orphans", help="Audit all open positions for missing Stop Loss")
    parser.add_argument("--auto-heal", "--auto_heal", action="store_true", dest="auto_heal", help="Audit and automatically heal orphan positions lacking Stop Loss")

    args = parser.parse_args()

    target_env = resolve_env(args.env)

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
