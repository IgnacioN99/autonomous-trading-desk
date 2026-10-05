#!/usr/bin/env python3
"""
funding_arbitrage.py - Delta-Neutral Funding Rate Arbitrage Engine and Simulation.
Calculates exact return, payment intervals, collateral, and basis spread for Binance.
"""

import argparse
import os
import sys
import urllib.request
import json
import time

def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'BinanceAgentic/1.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def scan_top_funding_opportunities(min_volume_usdt=20_000_000, top_n=6):
    """Screens top contracts for Cash-and-Carry (Positive and Negative)."""
    tickers = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    vol_map = {t['symbol']: float(t['quoteVolume']) for t in tickers}
    
    premiums = fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex")
    
    opportunities = []
    now_ms = int(time.time() * 1000)
    
    ROUNDTRIP_FRICTION_PCT = 0.16 # ~0.16% spot + perp taker fees + median spread
    HURDLE_APR_PCT = 25.0 # Minimum annualized hurdle rate to justify capital lockup

    for p in premiums:
        sym = p['symbol']
        vol = vol_map.get(sym, 0)
        if vol >= min_volume_usdt and sym.endswith('USDT'):
            fr = float(p.get('lastFundingRate', 0)) * 100 # % per 8h
            apr = fr * 3 * 365
            next_funding_ms = int(p.get('nextFundingTime', 0))
            mins_remaining = max(0, int((next_funding_ms - now_ms) / 60000))
            
            mark_p = float(p.get('markPrice', 0))
            index_p = float(p.get('indexPrice', 0))
            basis_pct = ((mark_p - index_p) / index_p) * 100 if index_p > 0 else 0.0

            # Binance Clamped Mechanism:
            # iota = 0.01%, gamma = 0.05%
            basis_frac = (mark_p - index_p) / mark_p if mark_p > 0 else 0.0
            clamp_val = min(max(0.0001 - basis_frac, -0.0005), 0.0005)
            theoretical_fr_pct = (basis_frac + clamp_val) * 100

            # Arbitrage direction:
            # If Funding > 0: SHORTS receive -> Long Spot + Short Perp (Traditional Cash-and-Carry)
            # If Funding < 0: LONGS receive  -> Short Margin Spot + Long Perp (Reverse Cash-and-Carry)
            if fr > 0:
                side = "CASH_AND_CARRY (Long Spot + Short Perp)"
                yield_type = "Yield on Short Perp"
            else:
                side = "REVERSE C&C (Short Margin Spot + Long Perp)"
                yield_type = "Yield on Long Perp"
                
            # Estimated net yield after 72h (9 8h payments) amortizing fees
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
                "recommended_otc_hours": "48h to 96h (6-12 payments to amortize friction)",
                "is_actionable": is_actionable
            })
            
    # Sort by absolute APR magnitude
    opportunities.sort(key=lambda x: abs(x['apr']), reverse=True)
    return opportunities[:top_n]

def simulate_funding_trade(symbol, total_capital_usdt=100.0):
    """Simulates a 1:1 Delta-Neutral position for a specific pair."""
    opps = scan_top_funding_opportunities(min_volume_usdt=10_000_000, top_n=50)
    target = next((o for o in opps if o['symbol'] == symbol), None)
    if not target:
        return {"error": f"Symbol {symbol} not found in scanner."}
        
    half_capital = total_capital_usdt / 2.0
    fr = target['funding_rate_8h']
    daily_yield = target['daily_yield']
    apr = target['apr']
    
    payout_8h = half_capital * (abs(fr) / 100.0)
    payout_daily = half_capital * (abs(daily_yield) / 100.0)
    payout_monthly = payout_daily * 30
    
    return {
        "symbol": symbol,
        "total_capital_usdt": total_capital_usdt,
        "spot_allocation_usdt": half_capital,
        "perp_allocation_usdt": half_capital,
        "delta_risk": "0.0 (Pure Delta Neutral)",
        "funding_rate_8h": f"{fr:+.4f}%",
        "annualized_apr": f"{apr:+.1f}%",
        "payout_8h": f"+{payout_8h:.3f} USDT",
        "payout_daily": f"+{payout_daily:.3f} USDT",
        "payout_monthly_est": f"+{payout_monthly:.2f} USDT",
        "strategy": target['strategy_type']
    }

def main(argv=None):
    """CLI: python3 scripts/funding_arbitrage.py [--json] [--top N] [--min-volume USDT]
                                                [--simulate SYMBOL --capital USDT] [--env prod|testnet]
    Read-only public market data. Exit codes: 0 ok, 1 data/API error, 2 bad usage."""
    parser = argparse.ArgumentParser(description="Funding rate / cash-and-carry scanner (read-only)")
    parser.add_argument("--json", action="store_true", help="Print a single JSON document on stdout")
    parser.add_argument("--top", type=int, default=6, help="Number of opportunities (default 6)")
    parser.add_argument("--min-volume", type=float, default=20_000_000, help="Min 24h quote volume in USDT (default 20M)")
    parser.add_argument("--simulate", default=None, metavar="SYMBOL", help="Also simulate a delta-neutral position for SYMBOL")
    parser.add_argument("--capital", type=float, default=None, help="Total capital (USDT) for --simulate (required with --simulate)")
    parser.add_argument("--env", default=None, help="prod|testnet (resolved via env_resolver; market data is always public mainnet)")
    args = parser.parse_args(argv)

    if args.top < 1 or args.min_volume < 0 or (args.simulate and (args.capital is None or args.capital <= 0)):
        parser.print_usage(sys.stderr)
        sys.stderr.write("error: --top >= 1, --min-volume >= 0, and --simulate requires --capital > 0\n")
        return 2
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from utils.env_resolver import resolve_env
        env = resolve_env(args.env)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    try:
        top = scan_top_funding_opportunities(min_volume_usdt=args.min_volume, top_n=args.top)
        sim = simulate_funding_trade(args.simulate.upper(), args.capital) if args.simulate else None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if args.json:
            sys.stdout.write(json.dumps({"status": "error", "command": "funding", "env": env, "error": err}, indent=2) + "\n")
        else:
            sys.stderr.write(f"Funding scan failed: {err}\n")
        return 1

    if args.json:
        payload = {"status": "ok", "command": "funding", "env": env, "count": len(top), "opportunities": top}
        if sim is not None:
            payload["simulation"] = sim
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0

    print("=== TOP FUNDING RATE ARBITRAGE OPPORTUNITIES (CASH-AND-CARRY) ===")
    for o in top:
        print(f"• {o['symbol']}: {o['funding_rate_8h']:+.4f}%/8h | APR: {o['apr']:+.1f}% | Vol: ${o['volume_24h_m']}M | Next in {o['mins_to_payout']//60}h {o['mins_to_payout']%60}m | {o['strategy_type']}")
    if not top:
        print("No contract clears the volume filter right now.")
    if sim is not None:
        print(f"\n--- SIMULATION ({args.capital:.2f} USDT) ---")
        print(json.dumps(sim, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
