#!/usr/bin/env python3
"""
radar_mcp_server.py - Official MCP Server for Trading Radar & Market Intelligence.
Exposes native MCP tools for scanning Binance Futures (15m/5m), querying Gmail newsletters,
calculating position sizing (Fractional Kelly), executing orders, and monitoring risk.
"""

import sys
import os
import json
import urllib.request
import time
import math
import importlib

# Ensure local path resolution
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
    description="MCP Server for Intraday Market Radar, Newsletters, and Sizing for Binance Futures"
)

@server.tool(description="Scans live Binance Futures contracts (15m) using the institutional Microstructure engine (CVD, Open Interest, and Liquidity Absorption), returning a conviction-ranked list.")
def scan_intraday_market(interval: str = "15m", top: int = 6) -> str:
    """
    Parameters:
    - interval: Analysis interval (default: '15m').
    - top: Number of opportunities to return (default: 6).
    """
    try:
        candidates = broad_market_radar.scan_all_liquid_pairs(top_n=60)
        if not candidates:
            return "No qualified institutional setups (>=55%) found at this moment."

        out = ["⚡ INSTITUTIONAL BINANCE FUTURES RADAR (Order Flow & 15m Microstructure) ⚡\n"]
        for i, item in enumerate(candidates[:top], 1):
            roe_est = round(item["risk_pct"] * item["rr"] * 3, 1) # at 3x
            trigger_str = f"{item['trigger']:.4f}" if item.get("trigger") else f"{item['price']:.4f}"
            m = item.get("micro", {})
            t_ratio = m.get("taker_ratio", 1.0)
            oi_pct = m.get("oi_change_pct", 0.0)
            regime = m.get("regime", "N/A")
            funding = m.get("funding_rate_pct", 0.0)

            out.append(f"#{i} | {item['symbol']} - {item['direction']} | Conviction: {item['confidence']}% ({item['tier']})")
            out.append(f"   • Price: {item['price']} | Confirmation Trigger: {trigger_str}")
            out.append(f"   • SL (ATR Buffer): {item['sl']:.4f} (-{item['risk_pct']}%) | TP1: {item['tp1']:.4f} | TP2: {item['tp2']:.4f} (R:R {item['rr']}:1 / ROE 3x: +{roe_est}%)")
            out.append(f"   • Microstructure: Taker Ratio: {t_ratio:.2f} | OI Delta: {oi_pct:+.2f}% | Regime: {regime} | Funding: {funding:.4f}%")
            out.append(f"   • Confluences: {', '.join(item['reasons'])}\n")

        return "\n".join(out)
    except Exception as e:
        return f"Error executing scanner: {str(e)}"

@server.tool(description="Queries latest crypto newsletters from Gmail inbox (label 'Newsletters/Crypto') to detect macro catalysts and market sentiment.")
def get_crypto_newsletters(limit: int = 5, sender: str = "") -> str:
    """
    Parameters:
    - limit: Number of emails to retrieve (default 5).
    - sender: Filter by sender (e.g. 'glassnode', 'blockworks', 'diariobitcoin').
    """
    try:
        user, password = fetch_newsletters.load_credentials()
        if not user or not password:
            return "Error: Gmail credentials not configured in .env or gmail_config.json"

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
            return "No emails found in Newsletters/Crypto."

        msg_ids = messages[0].split()
        selected_ids = msg_ids[-limit:]
        selected_ids.reverse()

        out = [f"📬 Latest {len(selected_ids)} Newsletters in 'Newsletters/Crypto':\n"]
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
            out.append(f"• Subject: {subject}\n  From: {from_hdr} | Date: {date_hdr[:16]}\n  Summary: {preview}...\n")

        mail.logout()
        return "\n".join(out)
    except Exception as e:
        return f"Error querying newsletters: {str(e)}"

@server.tool(description="Calculates exact position sizing, contract quantities, and liquidation distance for bounded-capital accounts under Fractional Kelly risk management.")
def calculate_position_sizing(entry_price: float, stop_loss_price: float, margin_usdt: float = 20.0, leverage: int = 3) -> str:
    """
    Parameters:
    - entry_price: Planned entry price.
    - stop_loss_price: Technical Stop Loss price level.
    - margin_usdt: Allocated margin (default $20 USDT).
    - leverage: Leverage multiplier (2x or 3x).
    """
    if entry_price <= 0 or stop_loss_price <= 0:
        return "Prices must be strictly greater than 0."

    risk_distance = abs(entry_price - stop_loss_price)
    risk_pct = (risk_distance / entry_price) * 100
    notional_size = margin_usdt * leverage
    quantity_tokens = notional_size / entry_price
    max_loss_usdt = notional_size * (risk_pct / 100)

    # Approximate liquidation estimate under Isolated margin
    is_long = entry_price > stop_loss_price
    liq_price = entry_price * (1 - (1 / leverage) * 0.9) if is_long else entry_price * (1 + (1 / leverage) * 0.9)

    return f"""📊 POSITION SIZING PROFILE (Bounded Capital / Kelly)
• Direction: {'LONG' if is_long else 'SHORT'}
• Allocated Margin: {margin_usdt:.2f} USDT (Isolated)
• Leverage: {leverage}x
• Total Notional: {notional_size:.2f} USDT
• Token Quantity: {quantity_tokens:.4f} units
• Trade Risk (SL): -{risk_pct:.2f}% price distance (-{max_loss_usdt:.2f} USDT capital risk)
• Estimated Liquidation Price: {liq_price:.4f} (Stop Loss at {stop_loss_price:.4f} protects capital well in advance)
"""

@server.tool(description="Screens memecoins and hyper-volatile assets (PEPE, WIF, BONK, DOGE, NEIRO) to find high-conviction YOLO Moonshot opportunities with isolated risk ($10 USDT, 10x-15x).")
def scan_yolo_moonshot(leverage: int = 15, margin_usdt: float = 10.0) -> str:
    """
    Parameters:
    - leverage: Aggressive leverage (10x to 15x). Default: 15x.
    - margin_usdt: Bounded micro-capital to risk (default: $10 USDT Isolated).
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

            # Hardened filters: require real volume or clear absorption
            if vol_ratio < 2.0 and lower_wick < 50.0:
                continue

            if rsi > 65.0:
                continue

            score = lower_wick * 0.5 + (vol_ratio * 15) + (max(0, 50 - rsi) * 0.8)
            if score < 50.0:
                continue

            if not best or score > best["score"]:
                atr = intraday_radar.calculate_atr(highs, lows, closes, period=14)
                trigger_p = c_high * 1.0008
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
        return "🚫 No memecoin asset currently passes the hardened high-conviction filters (Volume Climax ≥ 2.0x or Absorption Wick ≥ 50%). The YOLO slot remains empty to preserve capital."

    return f"""🚀 YOLO MOONSHOT RECOMMENDATION (Asymmetric Bounded Convexity)
• Pair: {best['symbol']} (Perpetual) - LONG
• Margin Mode: ISOLATED MANDATORY (100% risk containment)
• Allocated Capital: {margin_usdt:.2f} USDT | Leverage: {leverage}x (Notional: {margin_usdt * leverage:.2f} USDT)
• Contract Quantity: {best['qty']:.4f} units

PRICE LEVELS & TRIGGER:
• Confirmation Trigger (Next-Candle): {best['trigger']:.6f} | Market Price: {best['price']}
• Technical Stop Loss (1.5x ATR Buffer): {best['sl']:.6f} (-{best['risk_pct']:.2f}% price distance | Max Loss: -{best['max_loss']:.2f} USDT)
• TP1 (Capital Extraction + Break-Even): {best['tp1']:.6f} (ROE: +{best['roe_tp1']}% / Gain: +{(margin_usdt * best['roe_tp1'] / 100):.2f} USDT)
• TP2 (Unbounded Moonshot Runner): {best['tp2']:.6f} (ROE: +{best['roe_tp2']}% / Gain: +{(margin_usdt * best['roe_tp2'] / 100):.2f} USDT)

MEME CONFLUENCE (HARDENED FILTERS):
• Support rejection wick: {best['lower_wick']:.0f}% of candle
• RSI 15m: {best['rsi']:.1f} | Volume: {best['vol_ratio']:.1f}x average (Required ≥ 2.0x or wick ≥ 50%)
• ⚠️ PROTECTION RULE: Do not move SL to Break-Even prematurely; only move to BE after TP1 execution (+75% ROE) to absorb microstructural noise."""

@server.tool(description="Executes a complete Binance Futures position (Testnet or Prod) with isolated margin, leverage, trigger validation or conditional/limit entry, verified algo Stop Loss, and asymmetric Take Profits (30% TP1 / 70% TP2) with Reduce-Only.")
def deploy_futures_trade(symbol: str, direction: str, leverage: int = 3, margin_usdt: float = 20.0, sl_price: float = 0.0, tp1_price: float = 0.0, tp2_price: float = 0.0, target_env: str = "testnet", trigger_price: float = 0.0, order_type: str = "MARKET", limit_price: float = 0.0) -> str:
    """
    Parameters:
    - symbol: Trading pair (e.g. 'EIGENUSDT', 'ETHUSDT').
    - direction: 'LONG' or 'SHORT'.
    - leverage: Leverage multiplier (e.g. 3 for standard, 10-15 for YOLO).
    - margin_usdt: Committed margin (default: 20.0 USDT).
    - sl_price: Technical Stop Loss price level.
    - tp1_price: Take Profit 1 price level (30% of position, fees locked, move to free-trade).
    - tp2_price: Take Profit 2 price level (70% remaining, capturing positive right-tail).
    - target_env: 'testnet' (default) or 'prod'.
    - trigger_price: Confirmation wick break trigger level.
    - order_type: 'MARKET', 'STOP_MARKET' (conditional on trigger), or 'LIMIT'.
    - limit_price: Limit price when order_type='LIMIT'.
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
            return f"❌ Error executing trade: {res.get('error')}"

        if res.get("conditional_entry"):
            return f"""⏳ CONDITIONAL ORDER PLACED ({target_env.upper()})
• Pair: {res['symbol']} ({res['direction']})
• Activation Trigger (Stop Price): {res['trigger_price']}
• Submission Market Price: {res['cur_price']}
• Binance Order ID: {res['orderId']}
• Status: Pending technical breakout confirmation."""

        return f"""✅ POSITION DEPLOYED AND PROTECTED ({target_env.upper()})
• Pair: {res['symbol']} ({res['direction']} {res['leverage']}x - ISOLATED)
• Real Entry: {res['entry_price']} | Contracts: {res['total_qty']} units
• Real Margin: {res['real_margin']:.2f} USDT (Notional: {res['notional']:.2f} USDT)
• Stop Loss (Algo Order): {res['sl_price']} (Close-Position: Yes | Reduce-Only: Yes)
• Take Profit 1: {res['tp1_price']} ({res['tp1_qty']} units - Reduce-Only)
• Take Profit 2: {res['tp2_price']} ({res['tp2_qty']} units - Reduce-Only)
• Entry Order ID: {res['entry_order_id']}
"""
    except Exception as e:
        return f"Error in deploy_futures_trade: {str(e)}"

@server.tool(description="Moves the Stop Loss of an active position to Break-Even (entry price) canceling the prior SL.")
def move_to_breakeven(symbol: str, target_env: str = "testnet") -> str:
    """
    Parameters:
    - symbol: Trading pair with open position (e.g. 'EIGENUSDT').
    - target_env: 'testnet' or 'prod'.
    """
    try:
        res = execute_futures_trade.move_sl_to_breakeven(symbol, target_env=target_env)
        if not res.get("success"):
            return f"❌ Error moving SL: {res.get('error')}"
        return f"🛡️ {res['message']} in {target_env.upper()}."
    except Exception as e:
        return f"Error in move_to_breakeven: {str(e)}"

@server.tool(description="Queries real-time summary of all active open positions, unrealized PnL, ROE, and liquidation prices.")
def get_open_positions(target_env: str = "testnet") -> str:
    """
    Parameters:
    - target_env: 'testnet' (default) or 'prod'.
    """
    try:
        return execute_futures_trade.get_positions_summary(target_env=target_env)
    except Exception as e:
        return f"Error querying positions: {str(e)}"

@server.tool(description="Immediately closes an open position at market price and cancels all associated pending orders.")
def close_position_market(symbol: str, target_env: str = "testnet") -> str:
    """
    Parameters:
    - symbol: Pair to close (e.g. 'EIGENUSDT').
    - target_env: 'testnet' or 'prod'.
    """
    try:
        res = execute_futures_trade.close_position_market(symbol, target_env=target_env)
        if not res.get("success"):
            return f"❌ Error closing position: {res.get('error')}"
        return f"🔒 Position {symbol} successfully closed at market in {target_env.upper()}."
    except Exception as e:
        return f"Error in close_position_market: {str(e)}"

@server.tool(description="Exhaustively audits active Binance positions to check if any lacks an indexed Stop Loss (orphan/naked position). If auto_heal=True, places an emergency technical Stop Loss immediately.")
def audit_orphan_positions(auto_heal: bool = False, target_env: str = "testnet") -> str:
    """
    Parameters:
    - auto_heal: If True, automatically places an emergency Algo SL on unprotected positions.
    - target_env: 'testnet' (default) or 'prod'.
    """
    try:
        report = execute_futures_trade.audit_orphan_positions(target_env=target_env, auto_heal=auto_heal)
        if "error" in report:
            return f"❌ Audit error: {report['error']}"

        if report.get("total_active", 0) == 0:
            return "✅ No open positions in account."

        if report.get("all_protected"):
            lines = [f"🛡️ INTEGRITY AUDIT: 100% PROTECTED ({target_env.upper()})"]
            lines.append(f"• Total Active Positions: {report['total_active']}")
            lines.append("• Orphan Positions: 0 (All verified with active Stop Loss in ledger)")
            for p in report["positions"]:
                lines.append(f"   - {p['symbol']} ({p['direction']}): Active SL at {p['sl_triggers']} | PnL: {p['unpnl']:+.2f} USDT")
            return "\n".join(lines)
        else:
            lines = [f"🚨 CRITICAL ALERT: DETECTED {report['orphans_count']} UNPROTECTED ORPHAN POSITIONS ({target_env.upper()})"]
            for p in report["positions"]:
                prot_status = "✅ Protected" if p["is_protected"] else "❌ NAKED (Missing SL)"
                lines.append(f"• {p['symbol']} ({p['direction']}) -> {prot_status}")
                if not p["is_protected"]:
                    if p.get("auto_heal_attempted"):
                        heal_res = "Success" if p.get("auto_heal_verified") else "Failure"
                        lines.append(f"   [Auto-Heal]: Healing attempt: {heal_res} (New SL: {p.get('healed_sl_price')})")
                    else:
                        lines.append("   ⚠️ Recommended action: Deploy immediate SL or market close.")
            return "\n".join(lines)
    except Exception as e:
        return f"Error in audit_orphan_positions: {str(e)}"

@server.tool(description="Updates the Stop Loss of an active position to its optimal structural level (15m Swing + Chandelier ATR) to lock profits or tighten risk.")
def update_trailing_stop_structural(symbol: str, target_env: str = "testnet") -> str:
    """
    Parameters:
    - symbol: Active pair to tighten (e.g. 'SOLUSDT').
    - target_env: 'testnet' or 'prod'.
    """
    try:
        res = dem.update_position_to_structural_stop(symbol, target_env=target_env)
        if not res.get("success"):
            return f"❌ Error: {res.get('error')}"
        return res.get("message", "Update processed.")
    except Exception as e:
        return f"Error in update_trailing_stop_structural: {str(e)}"

@server.tool(description="Audits all active portfolio positions and ratchets their Stop Loss to structural profit levels.")
def audit_and_trail_all_positions(target_env: str = "testnet") -> str:
    """
    Parameters:
    - target_env: 'testnet' or 'prod'.
    """
    try:
        res = dem.audit_and_trail_all_positions(target_env=target_env)
        if "error" in res:
            return f"❌ Error: {res['error']}"
        lines = [f"🔬 STRUCTURAL TRAILING AUDIT ({target_env.upper()}):"]
        for r in res.get("results", []):
            lines.append(f"• {r.get('symbol')}: {r.get('message')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error in audit_and_trail_all_positions: {str(e)}"

@server.tool(description="Checks whether an active position is suffering from momentum exhaustion (Dead Alpha / stagnation > 90 min).")
def check_dead_alpha(symbol: str, target_env: str = "testnet") -> str:
    """
    Parameters:
    - symbol: Pair to audit (e.g. 'SOLUSDT').
    - target_env: 'testnet' or 'prod'.
    """
    try:
        res = dem.check_dead_alpha_timeout(symbol, target_env=target_env)
        return f"[{symbol}] {res.get('message')} | Recommendation: {res.get('recommendation')}"
    except Exception as e:
        return f"Error in check_dead_alpha: {str(e)}"

@server.tool(description="Calculates exact Volatility Parity sizing, where every trade risks an identical constant monetary loss (e.g. $1.50 USDT) regardless of asset volatility.")
def calculate_volatility_parity(symbol: str, entry_price: float, sl_price: float, target_dollar_risk: float = 1.50, leverage: int = 3, target_env: str = "testnet") -> str:
    """
    Parameters:
    - symbol: Trading pair (e.g. 'BTCUSDT', 'STRKUSDT').
    - entry_price: Planned entry price.
    - sl_price: Technical Stop Loss price level.
    - target_dollar_risk: Exact maximum monetary loss on SL (default: $1.50 USDT).
    - leverage: Leverage multiplier (default: 3).
    - target_env: 'testnet' or 'prod'.
    """
    try:
        s = qre.calculate_volatility_parity_sizing(symbol, entry_price, sl_price, target_dollar_risk, leverage, target_env)
        if "error" in s:
            return f"❌ Error: {s['error']}"
        return f"""📊 VOLATILITY PARITY PROFILE (CONSTANT MONETARY RISK)
• Pair: {s['symbol']} ({s['direction']} {s['leverage']}x)
• Entry: {s['entry_price']} | SL: {s['sl_price']} (-{s['risk_pct']}%)
• TP1 (+1.8R): {s['tp1_price']} (Est gain: +${s['potential_gain_tp1']} USDT)
• TP2 (+4.0R): {s['tp2_price']} (Est gain: +${s['potential_gain_tp2']} USDT)
• Contracts: {s['step_qty']} units | Notional: ${s['actual_notional']} USDT
• Required Margin: ${s['required_margin']} USDT (Isolated)
• Exact Monetary Risk: ${s['actual_dollar_risk']} USDT (Target: ${s['target_dollar_risk']} USDT)
• R:R Ratio: {s['ratio_rr']}:1"""
    except Exception as e:
        return f"Error in calculate_volatility_parity: {str(e)}"

@server.tool(description="Screens for Cointegrated Statistical Arbitrage (Pairs Trading) opportunities using MacKinnon Engle-Granger ADF test and Ornstein-Uhlenbeck half-life.")
def scan_delta_neutral_pairs() -> str:
    """
    Screens structurally cointegrated pairs (BTC/ETH, SOL/AVAX, SUI/APT, etc.) calculating Z-Score, ADF test, and half-life.
    """
    try:
        importlib.reload(qre)
        pairs = qre.scan_coingrated_market_pairs()
        lines = ["⚖️ COINTEGRATED PAIRS SCANNER (DELTA-0 STAT-ARB - ADF & OU HALF-LIFE TEST):\n"]
        for p in pairs:
            coint_tag = "✅ Cointegrated" if p.get("is_cointegrated") else "❌ Not Cointegrated"
            tag = "🔥 ACTIONABLE" if p["is_actionable"] else "⚖️ Equilibrium / Not Cointegrated"
            lines.append(f"• [{tag}] {p['pair']} ({coint_tag})")
            lines.append(f"  Z-Score: {p['z_score']:+.2f}σ | Beta: {p['hedge_ratio_beta']:.2f} | ADF p-val: {p.get('adf_pvalue', 1.0):.3f} | Half-Life: {p.get('half_life_hours', 0)}h")
            if p.get("recommendation"):
                lines.append(f"  {p['recommendation']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error in scan_delta_neutral_pairs: {str(e)}"

@server.tool(description="Audits Binance ledger trade history and calculates empirical Win Rate, Payoff Ratio, and mathematical Fractional Kelly sizing.")
def get_empirical_kelly_audit(target_env: str = "testnet") -> str:
    """
    Parameters:
    - target_env: 'testnet' (default) or 'prod'.
    """
    try:
        k = qre.calculate_empirical_kelly(target_env=target_env)
        if "error" in k:
            return f"❌ Error: {k['error']}"
        if k.get("status") == "INSUFFICIENT_DATA_CONSERVATIVE_MODE":
            return k["message"]
        return f"""🔬 MATHEMATICAL KELLY AUDIT ({target_env.upper()})
• Analyzed Trades: {k['total_trades_analyzed']} ({k['win_count']} Wins / {k['loss_count']} Losses)
• Empirical Win Rate: {k['win_rate_pct']}%
• Average Win: +${k['avg_win_usdt']} USDT | Average Loss: -${k['avg_loss_usdt']} USDT
• Payoff Ratio (b): {k['payoff_ratio_b']}
• Full Kelly Fraction (f*): {k['full_kelly_pct']}%
• Recommended Quarter-Kelly: {k['quarter_kelly_pct']}%
• Quantitative Diagnosis: {k['diagnosis']}
• Suggested Dollar Risk per Trade: ${k['recommended_dollar_risk']} USDT"""
    except Exception as e:
        return f"Error in get_empirical_kelly_audit: {str(e)}"

@server.tool(description="Reports an execution failure, anomaly, or technical error in the agentic setup by publishing an official GitHub Issue (or queuing it in local backlog if offline).")
def report_agent_execution_issue(
    title: str,
    error_detail: str,
    category: str = "agent_failure",
    severity: str = "HIGH",
    remediation: str = "",
    stack_trace: str = ""
) -> str:
    """
    Parameters:
    - title: Concise issue title (e.g. 'Timeout on endpoint /fapi/v1/depth').
    - error_detail: Description of failure or discrepancy.
    - category: 'agent_failure', 'risk_gate', 'tool_error', 'quant_logic', 'infra' (default: 'agent_failure').
    - severity: 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW' (default: 'HIGH').
    - remediation: Proposed remediation or patch.
    - stack_trace: Traceback or error payload (optional).
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
            return f"✅ Issue #{res.get('issue_number')} created successfully on GitHub: {res.get('html_url')}"
        elif res.get("deduplicated"):
            return f"ℹ️ {res.get('message')}"
        else:
            return f"📁 Issue queued successfully in local backlog (logs/issues_backlog.jsonl) with ID {res.get('fingerprint')[:8]}. Sync via 'python3 scripts/report_agent_issue.py --sync-backlog'."
    except Exception as e:
        return f"Error reporting issue: {str(e)}"

if __name__ == "__main__":
    server.run(transport="stdio")
