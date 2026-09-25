#!/usr/bin/env python3
"""
remember_trade_lesson.py - Immutable Memory Ledger for Trading Lessons.
Append-only operational lessons ledger with zero external dependencies.

STRICTLY APPEND-ONLY:
Operational learning records are written to logs/trade_insights.jsonl.
To invalidate or update a lesson, NEVER edit or delete past history;
append a tombstone ({"id": "...", "superseded": true}).

Usage:
  python3 scripts/remember_trade_lesson.py add --symbol HBARUSDT --dir LONG --outcome STOPPED_OUT --loss 1.68 --cause BTC_DUMP --insight "..." --tags macro,altcoins
  python3 scripts/remember_trade_lesson.py list [--tag macro]
  python3 scripts/remember_trade_lesson.py prune --id <id>
"""

import os
import sys
import json
import time
import uuid
import datetime
import argparse

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
INSIGHTS_FILE = os.path.join(LOGS_DIR, "trade_insights.jsonl")

def get_insights_path():
    os.makedirs(LOGS_DIR, exist_ok=True)
    return INSIGHTS_FILE

def append_record(record: dict):
    file_path = get_insights_path()
    try:
        from utils.atomic_writer import atomic_append_jsonl
        atomic_append_jsonl(file_path, record)
    except Exception:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

def read_all_insights(active_only=True):
    file_path = get_insights_path()
    if not os.path.exists(file_path):
        return []
    
    records = []
    superseded_ids = set()
    
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if item.get("superseded") is True:
                    superseded_ids.add(item.get("id"))
                records.append(item)
            except Exception:
                continue
                
    if active_only:
        active = []
        for r in records:
            r_id = r.get("id")
            if r_id and r_id not in superseded_ids and not r.get("superseded"):
                active.append(r)
        return active
    return records

def add_insight(symbol: str, direction: str, outcome: str, loss_usdt: float, root_cause: str, insight_text: str, tags: list):
    insight_id = f"ins-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    record = {
        "id": insight_id,
        "timestamp_utc": now_utc,
        "symbol": symbol.upper() if symbol else "MACRO",
        "direction": direction.upper() if direction else "NEUTRAL",
        "outcome": outcome.upper() if outcome else "NOTE",
        "loss_usdt": round(float(loss_usdt), 2) if loss_usdt is not None else 0.0,
        "root_cause": root_cause.upper() if root_cause else "UNSPECIFIED",
        "insight": insight_text.strip(),
        "tags": [t.strip().lower() for t in tags if t.strip()],
        "superseded": False
    }
    append_record(record)
    print(f"✅ Lesson recorded successfully [{insight_id}]: {record['insight']}")
    return record

def prune_insight(target_id: str):
    file_path = get_insights_path()
    if not os.path.exists(file_path):
        print("❌ Insights file does not exist.")
        return False
        
    tombstone = {
        "id": target_id,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "superseded": True,
        "note": "Tombstone appended via remember_trade_lesson.py"
    }
    append_record(tombstone)
    print(f"🪦 Tombstone appended for [{target_id}]. Lesson has been invalidated without rewriting history.")
    return True

def list_insights(tag_filter=None):
    insights = read_all_insights(active_only=True)
    if tag_filter:
        insights = [i for i in insights if tag_filter.lower() in [t.lower() for t in i.get("tags", [])]]
        
    if not insights:
        print("ℹ️  No recorded lessons" + (f" with tag '{tag_filter}'" if tag_filter else "") + ".")
        return
        
    print(f"\n🧠 COMMITTED MEMORY ({len(insights)} active lessons):")
    print("-" * 75)
    for i in insights:
        tags_str = f" [{', '.join(i.get('tags', []))}]" if i.get("tags") else ""
        loss_str = f" (-${i.get('loss_usdt')} USDT)" if i.get("loss_usdt") else ""
        print(f"• [{i.get('id')}] ({i.get('symbol')} {i.get('direction')}{loss_str}) Cause: {i.get('root_cause')}")
        print(f"  👉 \"{i.get('insight')}\"{tags_str}")
    print("-" * 75 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Committed Memory Ledger - Trade Insights")
    subparsers = parser.add_subparsers(dest="command")
    
    # add
    p_add = subparsers.add_parser("add")
    p_add.add_argument("--symbol", default="MACRO")
    p_add.add_argument("--dir", default="NEUTRAL", choices=["LONG", "SHORT", "NEUTRAL"])
    p_add.add_argument("--outcome", default="STOPPED_OUT")
    p_add.add_argument("--loss", type=float, default=0.0)
    p_add.add_argument("--cause", default="UNSPECIFIED")
    p_add.add_argument("--insight", required=True)
    p_add.add_argument("--tags", default="general")
    
    # list
    p_list = subparsers.add_parser("list")
    p_list.add_argument("--tag", default=None)
    
    # prune
    p_prune = subparsers.add_parser("prune")
    p_prune.add_argument("--id", required=True)
    
    args = parser.parse_args()
    
    if args.command == "add":
        tags_list = [t.strip() for t in args.tags.split(",")]
        add_insight(args.symbol, args.dir, args.outcome, args.loss, args.cause, args.insight, tags_list)
    elif args.command == "list":
        list_insights(args.tag)
    elif args.command == "prune":
        prune_insight(args.id)
    else:
        list_insights()
