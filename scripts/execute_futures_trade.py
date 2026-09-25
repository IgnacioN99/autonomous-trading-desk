#!/usr/bin/env python3
"""
execute_futures_trade.py - Execution Engine and Risk Management Harness for Binance Futures.
Supports Testnet and Prod, strict filter calculations, symmetric orders, and verified Algo Stop Loss.
"""

import os
import sys
import time
import math
import subprocess
import hmac
import hashlib
import urllib.parse
import urllib.request
import json
from decimal import Decimal, ROUND_DOWN

def load_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    config = {}
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip().strip('"').strip("'")
    return config

def get_client_config(target_env='testnet'):
    cfg = load_env()
    api_key = cfg.get('BINANCE_API_KEY', '')
    secret_key = cfg.get('BINANCE_SECRET_KEY', '')
    env = target_env.lower()
    
    if not api_key or 'PEGA_AQUI' in api_key:
        return None, None, None
        
    base_url = 'https://testnet.binancefuture.com' if env == 'testnet' else 'https://fapi.binance.com'
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

def send_signed_request(method, endpoint, params=None, target_env='testnet', retry_count=0):
    api_key, secret_key, base_url = get_client_config(target_env)
    if not api_key:
        return {"error": "Binance credentials not configured in .env"}

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

def get_symbol_filters(symbol, target_env='testnet'):
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

def setup_margin_and_leverage(symbol, leverage, target_env='testnet'):
    lev_res = send_signed_request('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage}, target_env=target_env)
    margin_res = send_signed_request('POST', '/fapi/v1/marginType', {'symbol': symbol, 'marginType': 'ISOLATED'}, target_env=target_env)
    return lev_res, margin_res

def place_algo_stop_loss(symbol, exit_side, sl_price, target_env='testnet'):
    params = {
        'algoType': 'CONDITIONAL',
        'symbol': symbol,
        'side': exit_side,
        'type': 'STOP_MARKET',
        'triggerPrice': sl_price,
        'closePosition': 'true'
    }
    res = send_signed_request('POST', '/fapi/v1/algoOrder', params, target_env=target_env)
    if isinstance(res, dict) and 'algoId' in res:
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
    res_cli = subprocess.run(cmd, capture_output=True, text=True)
    try:
        parsed = json.loads(res_cli.stdout)
        if 'algoId' in parsed:
            return parsed
    except Exception:
        pass
    return res

def verify_algo_stop_loss(symbol, exit_side, sl_price=None, target_env='testnet'):
    """
    Verifies that the Algo Stop Loss order actually exists and is active on the exchange.
    """
    try:
        algos = send_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}, target_env=target_env)
        if isinstance(algos, list):
            for ao in algos:
                if ao.get('orderType') in ['STOP_MARKET', 'STOP'] and ao.get('side') == exit_side:
                    if sl_price is not None:
                        trig = float(ao.get('triggerPrice', 0))
                        if trig > 0 and abs(trig - float(sl_price)) / trig < 0.03:
                            return True, ao
                    return True, ao
        return False, None
    except Exception as e:
        return False, str(e)

def log_emergency_abort(symbol, direction, qty, sl_p, sl_order, abort_exit, target_env='testnet'):
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    record = {
        'timestamp': int(time.time()),
        'symbol': symbol,
        'direction': direction.upper(),
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

def check_mechanical_gates(direction, cur_price, sl_price, tp1_price, total_qty, leverage, bypass_delta_gate=False, target_env='testnet', bypass_all_gates=False):
    """
    Mechanical Software Gates (Deterministic Precondition Validation).
    Verifies mathematical invariants and physically prevents execution if risk rules are violated.
    In TESTNET, free bypass of gates is permitted for testing, experiments, and stress tests.
    In PROD, gates are strict and inviolable.
    """
    if bypass_all_gates:
        return True, None

    is_testnet = str(target_env).lower() == 'testnet'
    is_long = direction.upper() == 'LONG'
    
    # 1. Delta-Neutral Gate (Permits bypass in testnet for open sandbox testing)
    if not bypass_delta_gate and not is_testnet:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
        state_file = os.path.join(log_dir, 'session_state.json')
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    state_data = json.load(f)
                    delta_bias = state_data.get('portfolio_exposure', {}).get('delta_bias') or state_data.get('portfolio_delta_bias', 'NEUTRAL')
                    if delta_bias == 'LONG_HEAVY' and is_long:
                        return False, "MECHANICAL HARD GATE REJECTION: Portfolio is in LONG_HEAVY state (+Delta imbalanced). Opening additional Longs is strictly prohibited. Short hedge or neutral portfolio required."
                    elif delta_bias == 'SHORT_HEAVY' and not is_long:
                        return False, "MECHANICAL HARD GATE REJECTION: Portfolio is in SHORT_HEAVY state (-Delta imbalanced). Opening additional Shorts is strictly prohibited. Long hedge or neutral portfolio required."
            except Exception as e:
                pass

    # 2. Maximum Monetary Risk Gate (Capped Risk - Relaxed up to $50 in testnet for sandbox testing)
    potential_dollar_loss = abs(cur_price - sl_price) * total_qty
    max_allowed_loss = 50.0 if is_testnet else (4.0 if leverage >= 10 else 2.50)
    if potential_dollar_loss > max_allowed_loss:
        return False, f"MECHANICAL HARD GATE REJECTION: Monetary risk exceeds allowed cap (${potential_dollar_loss:.2f} > ${max_allowed_loss:.2f} USDT). Adjust margin or position size."

    # 3. Financial Friction and Fee Gate (Relaxed in testnet for testing)
    if tp1_price and not is_testnet:
        profit_pct_tp1 = abs(tp1_price - cur_price) / cur_price
        if profit_pct_tp1 < 0.0035:
            return False, f"MECHANICAL HARD GATE REJECTION: Distance to TP1 ({profit_pct_tp1*100:.2f}%) below 0.35% friction floor. Taker commissions erode statistical edge."

    return True, None

def execute_complete_trade(symbol, direction, leverage, margin_usdt, sl_price, tp1_price, tp2_price, target_env='testnet', trigger_price=None, order_type='MARKET', limit_price=None, bypass_delta_gate=False):
    # Guardrail for PROD
    MAX_PROD_MARGIN = 25.0
    if target_env.lower() == 'prod' and margin_usdt > MAX_PROD_MARGIN:
        return {"success": False, "error": f"GUARDRAIL: Margin of {margin_usdt} USDT exceeds maximum allowed cap of {MAX_PROD_MARGIN} USDT on REAL network."}

    filters = get_symbol_filters(symbol, target_env=target_env)
    if not filters:
        return {"success": False, "error": f"Filters not found for {symbol}"}

    # 1. Fetch current price first to validate filters and gates
    ticker_res = send_signed_request('GET', '/fapi/v1/ticker/price', {'symbol': symbol}, target_env=target_env)
    cur_price = float(ticker_res.get('price', 0))
    if cur_price <= 0:
        return {"success": False, "error": "Could not fetch current market price"}

    is_long = direction.upper() == 'LONG'
    entry_side = 'BUY' if is_long else 'SELL'
    exit_side = 'SELL' if is_long else 'BUY'

    # 2. Calculate exact token quantity
    notional_target = margin_usdt * leverage
    raw_qty = notional_target / cur_price
    total_qty = round_step(raw_qty, filters['stepSize'], filters['precision_qty'])
    min_notional = filters.get('minNotional', 5.0)
    if total_qty * cur_price < min_notional:
        bumped_qty = round_step(total_qty + filters['stepSize'], filters['stepSize'], filters['precision_qty'])
        if bumped_qty * cur_price >= min_notional:
            total_qty = bumped_qty
    if total_qty < filters['minQty']:
        return {"success": False, "error": f"Quantity {total_qty} lower than minimum allowed {filters['minQty']}"}

    # 3. MECHANICAL HARD GATES VERIFICATION
    gate_ok, gate_err = check_mechanical_gates(direction, cur_price, sl_price, tp1_price, total_qty, leverage, bypass_delta_gate=bypass_delta_gate, target_env=target_env)
    if not gate_ok:
        return {"success": False, "hard_gate_rejection": True, "error": gate_err}

    # 4. Configure Isolated margin and leverage
    setup_margin_and_leverage(symbol, leverage, target_env=target_env)

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
                    'quantity': total_qty
                }
                cond_order = send_signed_request('POST', '/fapi/v1/order', entry_params, target_env=target_env)
                if 'orderId' in cond_order:
                    return {
                        "success": True,
                        "conditional_entry": True,
                        "orderId": cond_order['orderId'],
                        "symbol": symbol,
                        "direction": direction.upper(),
                        "trigger_price": trigger_p,
                        "cur_price": cur_price,
                        "message": f"Conditional STOP_MARKET order placed at {trigger_p}. Will trigger upon institutional wick breakout."
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
    if 'orderId' not in entry_order:
        return {"success": False, "error": f"Entry order failed: {entry_order}"}

    actual_entry_price = float(entry_order.get('avgPrice', cur_price))
    if actual_entry_price == 0:
        actual_entry_price = cur_price

    # 9. Execute Hard Stop Loss (Algo Order, closePosition=true, reduceOnly=true)
    sl_order = place_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)
    sl_verified, sl_info = verify_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)

    # Progressive retries (up to 3 attempts in ~2.8s) to absorb Mainnet indexing latency
    if not sl_verified:
        for retry_delay in [0.8, 1.0, 1.2]:
            time.sleep(retry_delay)
            if isinstance(sl_order, dict) and "error" in sl_order:
                sl_order = place_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)
            sl_verified, sl_info = verify_algo_stop_loss(symbol, exit_side, sl_p, target_env=target_env)
            if sl_verified:
                break

    # ATOMIC AUTO-DESTRUCT / FAIL-SAFE PROTOCOL:
    # If Stop Loss is NOT verified after 3 attempts (~2.8s), ABORT IMMEDIATELY
    if not sl_verified:
        send_signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol}, target_env=target_env)
        abort_exit = send_signed_request('POST', '/fapi/v1/order', {
            'symbol': symbol,
            'side': exit_side,
            'type': 'MARKET',
            'quantity': total_qty,
            'reduceOnly': 'true'
        }, target_env=target_env)
        log_emergency_abort(symbol, direction, total_qty, sl_p, sl_order, abort_exit, target_env)
        return {
            "success": False,
            "emergency_abort": True,
            "symbol": symbol,
            "error": f"CRITICAL FAIL-SAFE TRIGGERED: Stop Loss could not be confirmed after 3 attempts ({sl_order}). Position closed at MARKET immediately (order {abort_exit.get('orderId')}) to eliminate unhedged exposure.",
            "abort_exit": abort_exit
        }

    # 10. Execute TP1 (LIMIT, 30% position, Reduce-Only)
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
        'direction': direction.upper(),
        'leverage': leverage,
        'entry_price': actual_entry_price,
        'total_qty': total_qty,
        'sl_price': sl_p,
        'sl_verified': True,
        'sl_algo_id': sl_info.get('algoId') if sl_info else sl_order.get('algoId'),
        'tp1_price': tp1_p,
        'tp2_price': tp2_p,
        'entry_order_id': entry_order.get('orderId'),
        'sl_order': sl_order,
        'tp1_order_id': tp1_order.get('orderId'),
        'tp2_order_id': tp2_order.get('orderId'),
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
        "direction": direction.upper(),
        "leverage": leverage,
        "entry_price": actual_entry_price,
        "total_qty": total_qty,
        "entry_order_id": entry_order.get('orderId'),
        "sl_price": sl_p,
        "sl_algo_order": sl_info or sl_order,
        "tp1_price": tp1_p,
        "tp1_qty": tp1_qty,
        "tp1_order_id": tp1_order.get('orderId'),
        "tp2_price": tp2_p,
        "tp2_qty": tp2_qty,
        "tp2_order_id": tp2_order.get('orderId'),
        "notional": total_qty * actual_entry_price,
        "real_margin": (total_qty * actual_entry_price) / leverage
    }

def move_sl_to_breakeven(symbol, target_env='testnet'):
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

def get_positions_summary(target_env='testnet'):
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

def audit_orphan_positions(target_env='testnet', auto_heal=False):
    """
    Exhaustively audits all active positions in the account.
    Detects 'orphan' / 'naked' positions (without verified Algo Stop Loss on Binance).
    If auto_heal=True, places an emergency Algo SL calculated via volatility/liquidation buffer.
    """
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

def close_position_market(symbol, target_env='testnet'):
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
            if ao.get('algoId'):
                send_signed_request('DELETE', '/fapi/v1/algoOrder', {'symbol': symbol, 'algoId': ao['algoId']}, target_env=target_env)

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
