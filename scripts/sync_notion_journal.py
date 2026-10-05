#!/usr/bin/env python3
"""
sync_notion_journal.py - Syncs the Notion Trading Journal with the Binance ledger.

Audits open positions in the Notion database ("Trading Journal - Futures") and
reconciles them against Binance's accounting ground truth (fapi/v1/userTrades and positionRisk).

If a position is marked "Open" or "Active" in Notion but is closed on Binance, the row
is updated to "TP Hit" or "SL Hit", recording the exit price, net realized PnL and
close date.

Usage:
  python3 scripts/sync_notion_journal.py [--dry-run] [--api-key <KEY>] [--database-id <ID>]
"""

import os
import sys
import json
import time
import datetime
import urllib.request
import urllib.error
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
import execute_futures_trade as eft
from utils.env_resolver import resolve_env, is_prod_environment

NOTION_VERSION = "2022-06-28"

def load_env_credentials():
    api_key = os.environ.get("NOTION_API_KEY")
    db_id = os.environ.get("NOTION_DATABASE_ID")

    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k == "NOTION_API_KEY" and not api_key:
                        api_key = v
                    elif k == "NOTION_DATABASE_ID" and not db_id:
                        db_id = v
    if not db_id:
        user_context_path = os.path.join(BASE_DIR, "config", "user_context.json")
        if os.path.exists(user_context_path):
            try:
                with open(user_context_path, "r", encoding="utf-8") as f:
                    u_ctx = json.load(f)
                    notion_cfg = u_ctx.get("notion", {})
                    db_id = notion_cfg.get("database_id") or notion_cfg.get("collection_id")
            except Exception:
                pass
    return api_key, db_id

def notion_api_request(endpoint: str, method: str = "GET", data: dict = None, api_key: str = None):
    url = f"https://api.notion.com/v1/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json"
    }
    req_body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=req_body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        try:
            return {"error": json.loads(err_msg), "status_code": e.code}
        except Exception:
            return {"error": err_msg, "status_code": e.code}
    except Exception as e:
        return {"error": str(e), "status_code": 0}

def get_binance_trade_history(target_env: str = None) -> dict:
    """Collects the final state and PnL of every symbol traded on Binance."""
    target_env = resolve_env(target_env)
    # 1. Check for any live position on the ledger
    active_positions = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=target_env)
    live_map = {}
    if isinstance(active_positions, list):
        for p in active_positions:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                live_map[p["symbol"]] = {
                    "is_open": True,
                    "amt": amt,
                    "entry_price": float(p.get("entryPrice", 0)),
                    "unrealized_pnl": float(p.get("unRealizedProfit", 0))
                }

    # 2. Fetch recent trade history
    trades = eft.send_signed_request("GET", "/fapi/v1/userTrades", {"limit": 100}, target_env=target_env)
    history_by_symbol = {}
    if isinstance(trades, list):
        for t in trades:
            sym = t.get("symbol")
            pnl = float(t.get("realizedPnl", 0))
            time_utc = datetime.datetime.fromtimestamp(t.get("time", 0)/1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            price = float(t.get("price", 0))
            side = t.get("side")

            if sym not in history_by_symbol:
                history_by_symbol[sym] = {
                    "is_open": live_map.get(sym, {}).get("is_open", False),
                    "total_realized_pnl": 0.0,
                    "exit_trades": [],
                    "last_exit_price": price,
                    "last_exit_time": time_utc
                }
            if pnl != 0:
                history_by_symbol[sym]["total_realized_pnl"] += pnl
                history_by_symbol[sym]["exit_trades"].append({
                    "time": time_utc,
                    "side": side,
                    "price": price,
                    "pnl": pnl
                })
                history_by_symbol[sym]["last_exit_price"] = price
                history_by_symbol[sym]["last_exit_time"] = time_utc

    return history_by_symbol, live_map

def reconcile_notion(api_key: str, db_id: str, dry_run: bool = False, target_env: str = None):
    target_env = resolve_env(target_env)
    print("=" * 65)
    print("🔄 NOTION JOURNAL vs BINANCE FUTURES LEDGER RECONCILIATION")
    print(f"Target Env: {target_env.upper()} | Notion DB: {db_id[:8]}...")
    print("=" * 65)

    if not api_key or not db_id:
        print("❌ Error: NOTION_API_KEY or NOTION_DATABASE_ID not configured.")
        return False

    # 1. Fetch Binance ground truth
    binance_history, live_map = get_binance_trade_history(target_env)
    print(f"• Binance Ledger: {len(live_map)} active positions | {len(binance_history)} symbols with history.")

    # 2. Query the Notion database metadata
    db_meta = notion_api_request(f"databases/{db_id}", method="GET", api_key=api_key)
    if "error" in db_meta:
        print(f"❌ Error querying database metadata: {db_meta.get('error')}")
        return False

    props_meta = db_meta.get("properties", {})
    status_prop_name = None
    # "estado" / "ganancia" / "abierta" / "activa" are Spanish aliases kept so existing
    # Spanish-named Notion columns and status options keep matching.
    for name, p in props_meta.items():
        if p.get("type") in ["status", "select"] and any(w in name.lower() for w in ["status", "estado"]):
            status_prop_name = name
            break

    pnl_prop_name = None
    for name, p in props_meta.items():
        if p.get("type") == "number" and any(w in name.lower() for w in ["pnl", "profit", "ganancia"]):
            pnl_prop_name = name
            break

    print(f"• Notion properties detected: Status='{status_prop_name}', PnL='{pnl_prop_name}'")

    # 3. Query database pages
    query_res = notion_api_request(f"databases/{db_id}/query", method="POST", data={"page_size": 100}, api_key=api_key)
    pages = query_res.get("results", [])
    print(f"• Rows found in Notion: {len(pages)}")

    updated_count = 0
    for page in pages:
        props = page.get("properties", {})
        page_id = page.get("id")

        # Extract symbol
        symbol = None
        for k, v in props.items():
            if v.get("type") == "title" and v.get("title"):
                symbol = v["title"][0].get("plain_text", "").strip().upper()
                break
            elif "symbol" in k.lower() and v.get("rich_text"):
                symbol = v["rich_text"][0].get("plain_text", "").strip().upper()
                break

        if not symbol:
            continue

        # Extract current status
        current_status = ""
        if status_prop_name and status_prop_name in props:
            p_val = props[status_prop_name]
            if p_val.get("type") == "select" and p_val.get("select"):
                current_status = p_val["select"].get("name", "")
            elif p_val.get("type") == "status" and p_val.get("status"):
                current_status = p_val["status"].get("name", "")

        is_considered_open = current_status.lower() in ["open", "abierta", "active", "activa", "pending", "in progress"]
        
        # Check against Binance
        binance_data = binance_history.get(symbol)
        is_live_on_binance = live_map.get(symbol, {}).get("is_open", False)

        if is_considered_open and not is_live_on_binance:
            # Open in Notion but 100% CLOSED on Binance
            pnl = binance_data["total_realized_pnl"] if binance_data else 0.0
            new_status = "TP Hit" if pnl > 0 else ("SL Hit" if pnl < 0 else "Closed")

            print(f"⚠️ DISCREPANCY DETECTED on {symbol}:")
            print(f"   Notion says: '{current_status}' | Binance Ledger: CLOSED (PnL: {pnl:+.4f} USDT)")

            if not dry_run:
                update_payload = {"properties": {}}
                if status_prop_name:
                    p_type = props_meta[status_prop_name]["type"]
                    update_payload["properties"][status_prop_name] = {p_type: {"name": new_status}}
                if pnl_prop_name:
                    update_payload["properties"][pnl_prop_name] = {"number": round(pnl, 4)}
                # "Entorno" (environment) is the existing Notion column name; keep it.
                env_label = "REAL" if is_prod_environment(target_env) else "TESTNET"
                if "Entorno" in props_meta:
                    update_payload["properties"]["Entorno"] = {"select": {"name": env_label}}

                upd_res = notion_api_request(f"pages/{page_id}", method="PATCH", data=update_payload, api_key=api_key)
                if "error" not in upd_res:
                    print(f"   ✅ Updated in Notion -> Status: '{new_status}', PnL: {pnl:+.4f} USDT, Entorno: '{env_label}'")
                    updated_count += 1
                else:
                    print(f"   ❌ Failed to update page {page_id}: {upd_res.get('error')}")
            else:
                print(f"   [DRY-RUN] Would update to: '{new_status}', PnL: {pnl:+.4f} USDT")
                updated_count += 1

    print("=" * 65)
    print(f"🎯 RECONCILIATION COMPLETE: {updated_count} positions reconciled.")
    print("=" * 65)
    return True

def main():
    default_env = resolve_env()
    parser = argparse.ArgumentParser(description="Sync Notion Journal with Binance Ledger")
    parser.add_argument("--dry-run", action="store_true", help="Simulate reconciliation without modifying Notion")
    parser.add_argument("--api-key", type=str, help="Notion API Key (starts with secret_ or ntn_)")
    parser.add_argument("--database-id", type=str, help="Notion Database ID (32 chars UUID)")
    parser.add_argument("--env", type=str, default=default_env, help="Binance environment (testnet/prod)")
    args = parser.parse_args()

    api_key = args.api_key
    db_id = args.database_id

    if not api_key or not db_id:
        env_key, env_db = load_env_credentials()
        api_key = api_key or env_key
        db_id = db_id or env_db

    if not api_key or not db_id:
        print("⚠️ Notion credentials not configured.")
        print("Please configure NOTION_API_KEY and NOTION_DATABASE_ID in .env or pass via --api-key and --database-id.")
        sys.exit(1)

    reconcile_notion(api_key, db_id, dry_run=args.dry_run, target_env=args.env)

if __name__ == "__main__":
    main()
