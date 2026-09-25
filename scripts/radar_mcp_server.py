#!/usr/bin/env python3
"""
radar_mcp_server.py - Servidor MCP Oficial de Trading Radar & Inteligencia de Mercado.
Expone herramientas nativas MCP para escanear Binance Futuros (15m/5m), consultar newsletters de Gmail
y calcular dimensionamiento de posición (Fractional Kelly).
"""

import sys
import os
import json
import urllib.request
import time
import math
import importlib

# Asegurar path local
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday_radar
import fetch_newsletters
import execute_futures_trade
import broad_market_radar
import microstructure_engine
import dynamic_exit_manager as dem
import quant_risk_engine as qre
import report_agent_issue

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="crypto-radar",
    description="Servidor MCP de Radar de Mercado Intradiario, Newsletters y Sizing para Binance Futuros"
)

@server.tool(description="Escanea los contratos de Binance Futuros en vivo (15m) con el motor institucional de Microestructura (CVD, Open Interest y Absorción de Liquidez), devolviendo un ranking clasificado por nivel de convicción.")
def scan_intraday_market(interval: str = "15m", top: int = 6) -> str:
    """
    Parámetros:
    - interval: Intervalo de análisis (default: '15m').
    - top: Cantidad de oportunidades a devolver (default: 6).
    """
    try:
        candidates = broad_market_radar.scan_all_liquid_pairs(top_n=60)
        if not candidates:
            return f"No se encontraron oportunidades institucionales calificadas (>=55%) en este momento."

        out = [f"⚡ RADAR INSTITUCIONAL BINANCE FUTUROS (Order Flow & Microestructura 15m) ⚡\n"]
        for i, item in enumerate(candidates[:top], 1):
            roe_est = round(item["risk_pct"] * item["rr"] * 3, 1) # a 3x
            trigger_str = f"{item['trigger']:.4f}" if item.get("trigger") else f"{item['price']:.4f}"
            m = item.get("micro", {})
            t_ratio = m.get("taker_ratio", 1.0)
            oi_pct = m.get("oi_change_pct", 0.0)
            regime = m.get("regime", "N/A")
            funding = m.get("funding_rate_pct", 0.0)

            out.append(f"#{i} | {item['symbol']} - {item['direction']} | Convicción: {item['confidence']}% ({item['tier']})")
            out.append(f"   • Precio: {item['price']} | Gatillo Confirmación: {trigger_str}")
            out.append(f"   • SL (ATR Buffer): {item['sl']:.4f} (-{item['risk_pct']}%) | TP1: {item['tp1']:.4f} | TP2: {item['tp2']:.4f} (R:R {item['rr']}:1 / ROE 3x: +{roe_est}%)")
            out.append(f"   • Microestructura: Taker Ratio: {t_ratio:.2f} | OI Delta: {oi_pct:+.2f}% | Régimen: {regime} | Funding: {funding:.4f}%")
            out.append(f"   • Confluencias: {', '.join(item['reasons'])}\n")

        return "\n".join(out)
    except Exception as e:
        return f"Error ejecutando escáner: {str(e)}"

@server.tool(description="Consulta las newsletters cripto más recientes de la bandeja de Gmail (etiqueta 'Newsletters/Crypto') para detectar catalizadores macro y sentimiento de mercado.")
def get_crypto_newsletters(limit: int = 5, sender: str = "") -> str:
    """
    Parámetros:
    - limit: Cantidad de correos a traer (default 5).
    - sender: Filtrar por remitente (ej. 'glassnode', 'blockworks', 'diariobitcoin').
    """
    try:
        user, password = fetch_newsletters.load_credentials()
        if not user or not password:
            return "Error: Credenciales de Gmail no configuradas en .env o gmail_config.json"

        import imaplib, email
        from email.header import decode_header

        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(user, password)
        status, count = mail.select('"Newsletters/Crypto"', readonly=True)
        if status != "OK":
            mail.select("INBOX", readonly=True)

        search_cmd = f'FROM "{sender}"' if sender else "ALL"
        status, messages = mail.search(None, search_cmd)
        if status != "OK" or not messages[0]:
            mail.logout()
            return "No se encontraron correos en Newsletters/Crypto."

        msg_ids = messages[0].split()
        selected_ids = msg_ids[-limit:]
        selected_ids.reverse()

        out = [f"📬 Últimas {len(selected_ids)} Newsletters en 'Newsletters/Crypto':\n"]
        for mid in selected_ids:
            res, data = mail.fetch(mid, "(RFC822)")
            if res != "OK":
                continue
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)
            subject = fetch_newsletters.decode_mime_str(msg.get("Subject", ""))
            from_hdr = fetch_newsletters.decode_mime_str(msg.get("From", ""))
            date_hdr = fetch_newsletters.decode_mime_str(msg.get("Date", ""))
            body = fetch_newsletters.extract_body(msg)
            preview = " ".join(body.split())[:350]
            out.append(f"• Asunto: {subject}\n  De: {from_hdr} | Fecha: {date_hdr[:16]}\n  Resumen: {preview}...\n")

        mail.logout()
        return "\n".join(out)
    except Exception as e:
        return f"Error consultando newsletters: {str(e)}"

@server.tool(description="Calcula el dimensionamiento exacto de posición, contratos y distancia de liquidación para una cuenta micro con gestión de riesgo Fractional Kelly.")
def calculate_position_sizing(entry_price: float, stop_loss_price: float, margin_usdt: float = 20.0, leverage: int = 3) -> str:
    """
    Parámetros:
    - entry_price: Precio de entrada planeado.
    - stop_loss_price: Nivel de Stop Loss técnico.
    - margin_usdt: Margen real a asignar (default $20 USDT).
    - leverage: Apalancamiento (2x o 3x).
    """
    if entry_price <= 0 or stop_loss_price <= 0:
        return "Precios deben ser mayores a 0."

    risk_distance = abs(entry_price - stop_loss_price)
    risk_pct = (risk_distance / entry_price) * 100
    notional_size = margin_usdt * leverage
    quantity_tokens = notional_size / entry_price
    max_loss_usdt = notional_size * (risk_pct / 100)

    # Estimación de liquidación aproximada en Isolated
    is_long = entry_price > stop_loss_price
    liq_price = entry_price * (1 - (1 / leverage) * 0.9) if is_long else entry_price * (1 + (1 / leverage) * 0.9)

    return f"""📊 FICHA DE DIMENSIONAMIENTO (Capital Acotado / Kelly)
• Dirección: {'LONG' if is_long else 'SHORT'}
• Margen Real Asignado: {margin_usdt:.2f} USDT (Isolated)
• Apalancamiento: {leverage}x
• Tamaño Nocional Total: {notional_size:.2f} USDT
• Cantidad de Tokens a Cargar: {quantity_tokens:.4f} unidades
• Riesgo de la Operación (SL): -{risk_pct:.2f}% del precio (-{max_loss_usdt:.2f} USDT de tu capital)
• Precio Estimado de Liquidación: {liq_price:.4f} (El SL en {stop_loss_price:.4f} protege tu cuenta mucho antes)
"""

@server.tool(description="Escanea memecoins y activos de alta volatilidad (PEPE, WIF, BONK, DOGE, NEIRO) para encontrar la oportunidad YOLO Moonshot del momento con apalancamiento alto (10x-20x) y micro-capital ($5-$10 USDT Isolated).")
def scan_yolo_moonshot(leverage: int = 15, margin_usdt: float = 10.0) -> str:
    """
    Parámetros:
    - leverage: Apalancamiento agresivo (ej. 10x a 20x). Default: 15x.
    - margin_usdt: Capital micro-mínimo a arriesgar (default: $10 USDT en Isolated).
    """
    memes = ["1000PEPEUSDT", "DOGEUSDT", "WIFUSDT", "1000BONKUSDT", "1000SHIBUSDT", "FLOKIUSDT", "POPCATUSDT", "NEIROUSDT"]
    best = None

    for sym in memes:
        try:
            klines = intraday_radar.get_klines(sym, interval="15m", limit=35)
            if len(klines) < 20:
                continue
            closes = [float(x[4]) for x in klines]
            highs = [float(x[2]) for x in klines]
            lows = [float(x[3]) for x in klines]
            opens = [float(x[1]) for x in klines]
            vols = [float(x[5]) for x in klines]

            cur_p = closes[-1]
            c_open, c_high, c_low, c_vol = opens[-1], highs[-1], lows[-1], vols[-1]
            total_range = c_high - c_low if (c_high - c_low) > 0 else 1e-8
            lower_wick = (min(c_open, cur_p) - c_low) / total_range * 100
            upper_wick = (c_high - max(c_open, cur_p)) / total_range * 100

            avg_v = sum(vols[-11:-1]) / 10 if len(vols) >= 11 else c_vol
            vol_ratio = c_vol / avg_v if avg_v > 0 else 1.0
            rsi = intraday_radar.calculate_rsi(closes)

            # Filtros endurecidos: Exigir volumen real o absorción evidente
            # 1. Debe haber volumen relativo significativo (al menos 2.0x) O una mecha compradora brutal (>50%)
            if vol_ratio < 2.0 and lower_wick < 50.0:
                continue

            # 2. No perseguir activos en sobrecompra extrema
            if rsi > 65.0:
                continue

            # Score cuantitativo para Long Moonshot
            score = lower_wick * 0.5 + (vol_ratio * 15) + (max(0, 50 - rsi) * 0.8)
            if score < 50.0:
                continue

            if not best or score > best["score"]:
                atr = intraday_radar.calculate_atr(highs, lows, closes, period=14)
                # Gatillo de confirmación de vela siguiente
                trigger_p = c_high * 1.0008
                # SL con colchón de 1.5x ATR para amortiguar mechas
                sl = c_low - (1.5 * atr)
                risk_pct = ((cur_p - sl) / cur_p) * 100
                if risk_pct < 2.2:
                    sl = cur_p * 0.975
                    risk_pct = 2.5
                elif risk_pct > 5.5:
                    sl = cur_p * 0.945
                    risk_pct = 5.5

                tp1 = cur_p * (1 + risk_pct * 2.0 / 100) # 2R
                tp2 = cur_p * (1 + risk_pct * 4.5 / 100) # 4.5R Moonshot
                notional = margin_usdt * leverage
                qty = notional / cur_p
                max_loss = notional * (risk_pct / 100)

                best = {
                    "symbol": sym,
                    "score": score,
                    "price": cur_p,
                    "trigger": trigger_p,
                    "sl": sl,
                    "risk_pct": risk_pct,
                    "tp1": tp1,
                    "tp2": tp2,
                    "roe_tp1": round(risk_pct * 2.0 * leverage, 1),
                    "roe_tp2": round(risk_pct * 4.5 * leverage, 1),
                    "max_loss": max_loss,
                    "qty": qty,
                    "lower_wick": lower_wick,
                    "rsi": rsi,
                    "vol_ratio": vol_ratio
                }
        except Exception:
            continue

    if not best:
        return "🚫 Ningún activo memecoin cumple actualmente los filtros endurecidos de alta convicción (Volumen Clímax ≥ 2.0x o Mecha de Absorción ≥ 50%). Se mantiene el Slot YOLO vacío para proteger el capital y evitar forzar operaciones sin liquidez fresca."

    return f"""🚀 RECOMENDACIÓN YOLO MOONSHOT (Riesgo Asimétrico Filtrado)
• Par: {best['symbol']} (Perpetuo) - LONG
• Modo de Margen: ISOLATED OBLIGATORIO (Para aislar 100% el riesgo)
• Capital Asignado: {margin_usdt:.2f} USDT | Apalancamiento: {leverage}x (Nocional: {margin_usdt * leverage:.2f} USDT)
• Cantidad de Contratos: {best['qty']:.4f} unidades

NIVELES DE PRECIO & GATILLO:
• Gatillo Confirmación (Next-Candle): {best['trigger']:.6f} | Precio Mercado: {best['price']}
• Stop Loss Técnico (Buffer 1.5x ATR): {best['sl']:.6f} (-{best['risk_pct']:.2f}% en precio | Pérdida máx: -{best['max_loss']:.2f} USDT)
• TP1 (Retiro Capital + Break-Even): {best['tp1']:.6f} (ROE: +{best['roe_tp1']}% / Ganas +{(margin_usdt * best['roe_tp1'] / 100):.2f} USDT)
• TP2 (Moonshot Corriendo Libre): {best['tp2']:.6f} (ROE: +{best['roe_tp2']}% / Ganas +{(margin_usdt * best['roe_tp2'] / 100):.2f} USDT)

CONFLUENCIA MEME (FILTROS ENDURECIDOS):
• Mecha de rechazo en soporte: {best['lower_wick']:.0f}% de la vela
• RSI 15m: {best['rsi']:.1f} | Volumen: {best['vol_ratio']:.1f}x la media (Exigido ≥ 2.0x o mecha ≥ 50%)
• ⚠️ REGLA DE PROTECCIÓN: No mover SL a Break-Even prematuramente; solo mover a BE una vez alcanzado el TP1 (+75% ROE) para absorber el ruido microestructural."""

@server.tool(description="Ejecuta una posición de futuros completa en Binance (Testnet o Prod) con margen aislado, apalancamiento, validación de gatillo o entrada condicional/límite, Stop Loss algo verificado y Take Profits asimétricos (30% TP1 / 70% TP2) con Reduce-Only.")
def deploy_futures_trade(symbol: str, direction: str, leverage: int = 3, margin_usdt: float = 20.0, sl_price: float = 0.0, tp1_price: float = 0.0, tp2_price: float = 0.0, target_env: str = "testnet", trigger_price: float = 0.0, order_type: str = "MARKET", limit_price: float = 0.0) -> str:
    """
    Parámetros:
    - symbol: Par a operar (ej. 'EIGENUSDT', 'ETHUSDT').
    - direction: 'LONG' o 'SHORT'.
    - leverage: Apalancamiento (ej. 3 para normales, 10-15 para YOLO).
    - margin_usdt: Margen real a comprometer (default: 20.0 USDT).
    - sl_price: Precio de Stop Loss técnico.
    - tp1_price: Precio de Take Profit 1 (30% de la posición, para asegurar comisiones y pasar a Free-Trade).
    - tp2_price: Precio de Take Profit 2 (70% de la posición restante, capturando la cola derecha).
    - target_env: 'testnet' (default) o 'prod' (red real con guardarraíl de seguridad).
    - trigger_price: Nivel gatillo de confirmación de rotura de mecha.
    - order_type: 'MARKET', 'STOP_MARKET' (condicional al gatillo), o 'LIMIT'.
    - limit_price: Precio límite en caso de order_type='LIMIT'.
    """
    try:
        importlib.reload(execute_futures_trade)
        res = execute_futures_trade.execute_complete_trade(
            symbol=symbol,
            direction=direction,
            leverage=leverage,
            margin_usdt=margin_usdt,
            sl_price=sl_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            target_env=target_env,
            trigger_price=trigger_price if trigger_price > 0 else None,
            order_type=order_type,
            limit_price=limit_price if limit_price > 0 else None
        )
        if not res.get("success"):
            return f"❌ Error ejecutando posición: {res.get('error')}"

        if res.get("conditional_entry"):
            return f"""⏳ ORDEN CONDICIONAL COLOCADA ({target_env.upper()})
• Par: {res['symbol']} ({res['direction']})
• Gatillo Activador (Stop Price): {res['trigger_price']}
• Precio Actual al Enviar: {res['cur_price']}
• ID Orden Binance: {res['orderId']}
• Estatus: Pendiente de confirmación de rotura técnica."""

        return f"""✅ POSICIÓN EJECUTADA Y BLINDADA ({target_env.upper()})
• Par: {res['symbol']} ({res['direction']} {res['leverage']}x - ISOLATED)
• Entrada Real: {res['entry_price']} | Contratos: {res['total_qty']} unidades
• Margen Real: {res['real_margin']:.2f} USDT (Nocional: {res['notional']:.2f} USDT)
• Stop Loss (Algo Order): {res['sl_price']} (Close-Position: Sí | Reduce-Only: Sí)
• Take Profit 1: {res['tp1_price']} ({res['tp1_qty']} unidades - Reduce-Only)
• Take Profit 2: {res['tp2_price']} ({res['tp2_qty']} unidades - Reduce-Only)
• ID Orden Entrada: {res['entry_order_id']}
"""
    except Exception as e:
        return f"Error en deploy_futures_trade: {str(e)}"

@server.tool(description="Mueve el Stop Loss de una posición activa a Break-Even (precio de entrada exacto) cancelando el SL anterior.")
def move_to_breakeven(symbol: str, target_env: str = "testnet") -> str:
    """
    Parámetros:
    - symbol: Par con posición abierta (ej. 'EIGENUSDT').
    - target_env: 'testnet' o 'prod'.
    """
    try:
        res = execute_futures_trade.move_sl_to_breakeven(symbol, target_env=target_env)
        if not res.get("success"):
            return f"❌ Error moviendo SL: {res.get('error')}"
        return f"🛡️ {res['message']} en {target_env.upper()}."
    except Exception as e:
        return f"Error en move_to_breakeven: {str(e)}"

@server.tool(description="Consulta en tiempo real el resumen de todas las posiciones abiertas activas, PnL no realizado, ROE y precio de liquidación.")
def get_open_positions(target_env: str = "testnet") -> str:
    """
    Parámetros:
    - target_env: 'testnet' (default) o 'prod'.
    """
    try:
        return execute_futures_trade.get_positions_summary(target_env=target_env)
    except Exception as e:
        return f"Error consultando posiciones: {str(e)}"

@server.tool(description="Cierra inmediatamente a mercado una posición abierta y cancela todas sus órdenes pendientes asociadas.")
def close_position_market(symbol: str, target_env: str = "testnet") -> str:
    """
    Parámetros:
    - symbol: Par a cerrar (ej. 'EIGENUSDT').
    - target_env: 'testnet' o 'prod'.
    """
    try:
        res = execute_futures_trade.close_position_market(symbol, target_env=target_env)
        if not res.get("success"):
            return f"❌ Error cerrando posición: {res.get('error')}"
        return f"🔒 Posición {symbol} cerrada exitosamente a mercado en {target_env.upper()}."
    except Exception as e:
        return f"Error en close_position_market: {str(e)}"

@server.tool(description="Audita de forma exhaustiva si alguna posición activa en Binance carece de Stop Loss confirmado (posición huérfana/desnuda). Con auto_heal=True coloca un Stop Loss técnico de emergencia de inmediato.")
def audit_orphan_positions(auto_heal: bool = False, target_env: str = "testnet") -> str:
    """
    Parámetros:
    - auto_heal: Si es True, coloca automáticamente un Algo SL de emergencia a las posiciones desnudas.
    - target_env: 'testnet' (default) o 'prod'.
    """
    try:
        report = execute_futures_trade.audit_orphan_positions(target_env=target_env, auto_heal=auto_heal)
        if "error" in report:
            return f"❌ Error en auditoría: {report['error']}"

        if report.get("total_active", 0) == 0:
            return "✅ No hay posiciones abiertas en la cuenta."

        if report.get("all_protected"):
            lines = [f"🛡️ AUDITORÍA DE INTEGRIDAD: 100% PROTEGIDAS ({target_env.upper()})"]
            lines.append(f"• Total de Posiciones Activas: {report['total_active']}")
            lines.append("• Posiciones Huérfanas (Desnudas): 0 (Todas tienen Stop Loss verificado en ledger)")
            for p in report["positions"]:
                lines.append(f"   - {p['symbol']} ({p['direction']}): SL Activo en {p['sl_triggers']} | PnL: {p['unpnl']:+.2f} USDT")
            return "\n".join(lines)
        else:
            lines = [f"🚨 ALERTA CRÍTICA: SE DETECTARON {report['orphans_count']} POSICIONES HUÉRFANAS/DESNUDAS ({target_env.upper()})"]
            for p in report["positions"]:
                prot_status = "✅ Protegida" if p["is_protected"] else "❌ DESNUDA (Sin SL)"
                lines.append(f"• {p['symbol']} ({p['direction']}) -> {prot_status}")
                if not p["is_protected"]:
                    if p.get("auto_heal_attempted"):
                        heal_res = "Éxito" if p.get("auto_heal_verified") else "Fallo"
                        lines.append(f"   [Auto-Heal]: Intento de curación: {heal_res} (Nuevo SL: {p.get('healed_sl_price')})")
                    else:
                        lines.append("   ⚠️ Acción recomendada: Colocar SL inmediato o cerrar a mercado.")
            return "\n".join(lines)
    except Exception as e:
        return f"Error en audit_orphan_positions: {str(e)}"

@server.tool(description="Actualiza el Stop Loss de una posición activa a su nivel estructural óptimo (Swing 5m + Chandelier ATR) para asegurar ganancias o ceñir el riesgo.")
def update_trailing_stop_structural(symbol: str, target_env: str = "testnet") -> str:
    """
    Parámetros:
    - symbol: Par activo a ceñir (ej. 'SOLUSDT').
    - target_env: 'testnet' o 'prod'.
    """
    try:
        res = dem.update_position_to_structural_stop(symbol, target_env=target_env)
        if not res.get("success"):
            return f"❌ Error: {res.get('error')}"
        return res.get("message", "Actualización procesada.")
    except Exception as e:
        return f"Error en update_trailing_stop_structural: {str(e)}"

@server.tool(description="Audita todas las posiciones activas de la cartera y ratchetea sus Stop Loss a niveles estructurales en beneficio para blindar ganancias.")
def audit_and_trail_all_positions(target_env: str = "testnet") -> str:
    """
    Parámetros:
    - target_env: 'testnet' o 'prod'.
    """
    try:
        res = dem.audit_and_trail_all_positions(target_env=target_env)
        if "error" in res:
            return f"❌ Error: {res['error']}"
        lines = [f"🔬 AUDITORÍA DE TRAILING ESTRUCTURAL ({target_env.upper()}):"]
        for r in res.get("results", []):
            lines.append(f"• {r.get('symbol')}: {r.get('message')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error en audit_and_trail_all_positions: {str(e)}"

@server.tool(description="Verifica si una posición activa sufre de agotamiento de impulso (Alpha Decay / estancamiento por más de 90 min).")
def check_dead_alpha(symbol: str, target_env: str = "testnet") -> str:
    """
    Parámetros:
    - symbol: Par a auditar (ej. 'SOLUSDT').
    - target_env: 'testnet' o 'prod'.
    """
    try:
        res = dem.check_dead_alpha_timeout(symbol, target_env=target_env)
        return f"[{symbol}] {res.get('message')} | Recomendación: {res.get('recommendation')}"
    except Exception as e:
        return f"Error en check_dead_alpha: {str(e)}"

@server.tool(description="Calcula el dimensionamiento exacto por Paridad de Volatilidad, donde cada trade arriesga idéntica pérdida monetaria fija (ej. $1.50 USDT) sin importar cuán volátil sea el activo.")
def calculate_volatility_parity(symbol: str, entry_price: float, sl_price: float, target_dollar_risk: float = 1.50, leverage: int = 3, target_env: str = "testnet") -> str:
    """
    Parámetros:
    - symbol: Par a operar (ej. 'BTCUSDT', 'STRKUSDT').
    - entry_price: Precio planeado de entrada.
    - sl_price: Nivel de Stop Loss técnico.
    - target_dollar_risk: Pérdida monetaria máxima exacta si toca SL (default: $1.50 USDT).
    - leverage: Apalancamiento a utilizar (default: 3).
    - target_env: 'testnet' o 'prod'.
    """
    try:
        s = qre.calculate_volatility_parity_sizing(symbol, entry_price, sl_price, target_dollar_risk, leverage, target_env)
        if "error" in s:
            return f"❌ Error: {s['error']}"
        return f"""📊 FICHA DE PARIDAD DE VOLATILIDAD (RIESGO MONETARIO CONSTANTE)
• Par: {s['symbol']} ({s['direction']} {s['leverage']}x)
• Entrada: {s['entry_price']} | SL: {s['sl_price']} (-{s['risk_pct']}%)
• TP1 (+1.5R): {s['tp1_price']} (Ganancia est: +${s['potential_gain_tp1']} USDT)
• TP2 (+3.0R): {s['tp2_price']} (Ganancia est: +${s['potential_gain_tp2']} USDT)
• Contratos: {s['step_qty']} unidades | Nocional: ${s['actual_notional']} USDT
• Margen Requerido: ${s['required_margin']} USDT (Isolated)
• Riesgo Monetario Exacto: ${s['actual_dollar_risk']} USDT (Objetivo: ${s['target_dollar_risk']} USDT)
• R:R Ratio: {s['ratio_rr']}:1"""
    except Exception as e:
        return f"Error en calculate_volatility_parity: {str(e)}"

@server.tool(description="Escanea oportunidades de Arbitraje Estadístico de Pares Cointegrados (Pairs Trading) aplicando test ADF de estacionariedad y vida media Ornstein-Uhlenbeck.")
def scan_delta_neutral_pairs() -> str:
    """
    Escanea la canasta oficial de pares cointegrados (BTC/ETH, SOL/AVAX, DOGE/BONK, etc.) calculando su Z-Score, test ADF y vida media.
    """
    try:
        importlib.reload(qre)
        pairs = qre.scan_coingrated_market_pairs()
        lines = ["⚖️ ESCÁNER DE PARES COINTEGRADOS (STAT-ARB DELTA-0 - TEST ADF & HALF-LIFE OU):\n"]
        for p in pairs:
            coint_tag = "✅ Cointegrado (ADF)" if p.get("is_cointegrated") else "❌ No Cointegrado"
            tag = "🔥 ACCIONABLE" if p["is_actionable"] else "⚖️ En Rango / No Cointegrado"
            lines.append(f"• [{tag}] {p['pair']} ({coint_tag})")
            lines.append(f"  Z-Score: {p['z_score']:+.2f}σ | Beta: {p['hedge_ratio_beta']:.2f} | ADF p-val: {p.get('adf_pvalue', 1.0):.3f} | Half-Life: {p.get('half_life_hours', 0)}h")
            if p.get("recommendation"):
                lines.append(f"  {p['recommendation']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error en scan_delta_neutral_pairs: {str(e)}"

@server.tool(description="Audita el historial de trades en Binance y calcula las métricas empíricas de Win Rate, Payoff Ratio y la fracción matemática Fractional Kelly.")
def get_empirical_kelly_audit(target_env: str = "testnet") -> str:
    """
    Parámetros:
    - target_env: 'testnet' (default) o 'prod'.
    """
    try:
        k = qre.calculate_empirical_kelly(target_env=target_env)
        if "error" in k:
            return f"❌ Error: {k['error']}"
        if k.get("status") == "INSUFFICIENT_DATA":
            return k["message"]
        return f"""🔬 AUDITORÍA MATEMÁTICA DE KELLY ({target_env.upper()})
• Trades Analizados: {k['total_trades_analyzed']} ({k['win_count']} Ganadas / {k['loss_count']} Perdidas)
• Win Rate Empírico: {k['win_rate_pct']}%
• Ganancia Promedio: +${k['avg_win_usdt']} USDT | Pérdida Promedio: -${k['avg_loss_usdt']} USDT
• Ratio Payoff (b): {k['payoff_ratio_b']}
• Full Kelly Fraction (f*): {k['full_kelly_pct']}%
• Quarter-Kelly Recomendado: {k['quarter_kelly_pct']}%
• Diagnóstico Cuantitativo: {k['diagnosis']}
• Riesgo Dollar Sugerido por Operación: ${k['recommended_dollar_risk']} USDT"""
    except Exception as e:
        return f"Error en get_empirical_kelly_audit: {str(e)}"

@server.tool(description="Reporta una falla de ejecución, anomalía o error técnico del setup agéntico creando un GitHub Issue oficial en el repositorio autónomo (o encolándolo en el backlog local si no hay token).")
def report_agent_execution_issue(
    title: str,
    error_detail: str,
    category: str = "agent_failure",
    severity: str = "HIGH",
    remediation: str = "",
    stack_trace: str = ""
) -> str:
    """
    Parámetros:
    - title: Título conciso del fallo (ej. 'Timeout en endpoint /fapi/v1/depth').
    - error_detail: Descripción del fallo o discrepancia encontrada.
    - category: 'agent_failure', 'risk_gate', 'tool_error', 'quant_logic', 'infra' (default: 'agent_failure').
    - severity: 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW' (default: 'HIGH').
    - remediation: Propuesta de solución o parche si el agente la deduce.
    - stack_trace: Traceback o payload de error (opcional).
    """
    try:
        res = report_agent_issue.report_issue(
            title=title,
            error_detail=error_detail,
            category=category,
            severity=severity,
            agent_name="crypto_radar_agent",
            stack_trace=stack_trace,
            remediation=remediation
        )
        if res.get("status") == "PUBLISHED_GITHUB":
            return f"✅ Issue #{res.get('issue_number')} creado con éxito en GitHub: {res.get('html_url')}"
        elif res.get("deduplicated"):
            return f"ℹ️ {res.get('message')}"
        else:
            return f"📁 Issue encolado exitosamente en el backlog local (logs/issues_backlog.jsonl) con ID {res.get('fingerprint')[:8]}. Sincronizar con 'python3 scripts/report_agent_issue.py --sync-backlog'."
    except Exception as e:
        return f"Error al reportar issue: {str(e)}"

if __name__ == "__main__":
    server.run(transport="stdio")
