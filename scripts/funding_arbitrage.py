#!/usr/bin/env python3
"""
funding_arbitrage.py - Motor de Análisis y Simulación de Arbitraje Delta-Neutral de Funding Rate
Calcula el retorno exacto, intervalos de cobro, colateral y diferencial de base para Binance.
"""

import urllib.request
import json
import time

def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'BinanceAgentic/1.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def scan_top_funding_opportunities(min_volume_usdt=20_000_000, top_n=6):
    """Escanea los mejores contratos para Cash-and-Carry (Positivo y Negativo)."""
    tickers = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    vol_map = {t['symbol']: float(t['quoteVolume']) for t in tickers}
    
    premiums = fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex")
    
    opportunities = []
    now_ms = int(time.time() * 1000)
    
    ROUNDTRIP_FRICTION_PCT = 0.16 # ~0.16% comisiones spot + perp taker + spread medio
    HURDLE_APR_PCT = 25.0 # Tasa de corte anualizada mínima para justificar inmovilización de margen

    for p in premiums:
        sym = p['symbol']
        vol = vol_map.get(sym, 0)
        if vol >= min_volume_usdt and sym.endswith('USDT'):
            fr = float(p.get('lastFundingRate', 0)) * 100 # % en 8h
            apr = fr * 3 * 365
            next_funding_ms = int(p.get('nextFundingTime', 0))
            mins_remaining = max(0, int((next_funding_ms - now_ms) / 60000))
            
            mark_p = float(p.get('markPrice', 0))
            index_p = float(p.get('indexPrice', 0))
            basis_pct = ((mark_p - index_p) / index_p) * 100 if index_p > 0 else 0.0

            # Mecánica Clamped Binance (NotebookLM Pillar 2):
            # iota = 0.01%, gamma = 0.05%
            # Deviation rho = basis + clamp(iota - basis, -gamma, gamma)
            basis_frac = (mark_p - index_p) / mark_p if mark_p > 0 else 0.0
            clamp_val = min(max(0.0001 - basis_frac, -0.0005), 0.0005)
            theoretical_fr_pct = (basis_frac + clamp_val) * 100

            # Dirección del arbitraje:
            # Si Funding > 0: Cobran los SHORTS -> Long Spot + Short Perp (Cash-and-Carry Tradicional)
            # Si Funding < 0: Cobran los LONGS  -> Short Spot (Margin) + Long Perp (Reverse Cash-and-Carry)
            if fr > 0:
                side = "CASH_AND_CARRY (Long Spot + Short Perp)"
                yield_type = "Cobras en Short Perp"
            else:
                side = "REVERSE C&C (Short Margin Spot + Long Perp)"
                yield_type = "Cobras en Long Perp"
                
            # Retorno neto estimado tras 72h (9 cobros de 8h) amortizando comisiones
            gross_yield_72h = abs(fr) * 9
            net_yield_72h = max(0.0, gross_yield_72h - ROUNDTRIP_FRICTION_PCT)
            is_actionable = bool(abs(apr) >= HURDLE_APR_PCT and net_yield_72h > 0.15)

            opportunities.append({
                "symbol": sym,
                "funding_rate_8h": round(fr, 4),
                "theoretical_funding_8h": round(theoretical_fr_pct, 4),
                "apr": round(apr, 1),
                "daily_yield": round(fr * 3, 3),
                "volume_24h_m": round(vol / 1e6, 1),
                "mins_to_payout": mins_remaining,
                "strategy_type": side,
                "yield_type": yield_type,
                "mark_price": mark_p,
                "basis_spread_pct": round(basis_pct, 3),
                "hurdle_apr_pct": HURDLE_APR_PCT,
                "net_yield_72h_pct": round(net_yield_72h, 3),
                "recommended_otc_hours": "48h a 96h (6-12 cobros para amortizar fricción)",
                "is_actionable": is_actionable
            })
            
    # Ordenar por magnitud absoluta del APR
    opportunities.sort(key=lambda x: abs(x['apr']), reverse=True)
    return opportunities[:top_n]

def simulate_funding_trade(symbol, total_capital_usdt=100.0):
    """Simula una posición 1:1 Delta Neutral para un par específico."""
    opps = scan_top_funding_opportunities(min_volume_usdt=10_000_000, top_n=50)
    target = next((o for o in opps if o['symbol'] == symbol), None)
    if not target:
        return {"error": f"No se encontró el símbolo {symbol} en el escáner."}
        
    half_capital = total_capital_usdt / 2.0
    fr = target['funding_rate_8h']
    daily_yield = target['daily_yield']
    apr = target['apr']
    
    # Ganancia por cada pago de 8 horas sobre la pata que cobra (la mitad del capital total)
    payout_8h = half_capital * (abs(fr) / 100.0)
    payout_daily = half_capital * (abs(daily_yield) / 100.0)
    payout_monthly = payout_daily * 30
    
    return {
        "symbol": symbol,
        "total_capital_usdt": total_capital_usdt,
        "spot_allocation_usdt": half_capital,
        "perp_allocation_usdt": half_capital,
        "delta_risk": "0.0 (Delta Neutral Puro)",
        "funding_rate_8h": f"{fr:+.4f}%",
        "apr_anual": f"{apr:+.1f}%",
        "cobro_cada_8h": f"+{payout_8h:.3f} USDT",
        "cobro_diario": f"+{payout_daily:.3f} USDT",
        "cobro_mensual_est": f"+{payout_monthly:.2f} USDT",
        "strategy": target['strategy_type']
    }

if __name__ == "__main__":
    top = scan_top_funding_opportunities()
    print("=== TOP OPORTUNIDADES ARBITRAJE FUNDING RATE (CASH-AND-CARRY) ===")
    for o in top:
        print(f"• {o['symbol']}: {o['funding_rate_8h']:+.4f}%/8h | APR: {o['apr']:+.1f}% | Vol: ${o['volume_24h_m']}M | Faltan {o['mins_to_payout']//60}h {o['mins_to_payout']%60}m | {o['strategy_type']}")
    
    print("\n--- SIMULACIÓN DE EJEMPLO ($100 USDT) ---")
    sim = simulate_funding_trade(top[0]['symbol'], 100.0)
    print(json.dumps(sim, indent=2))
