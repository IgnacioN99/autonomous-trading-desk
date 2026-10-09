#!/usr/bin/env python3
"""
lessons.py - Shared reader of the committed lessons ledger (logs/trade_insights.jsonl, issue #187). Stdlib only.

The ledger is append-only: a lesson is invalidated by a later tombstone ({"id": <target>, "superseded": true}).
read_records(path) returns the JSON-object lines in file order (malformed or non-object lines are skipped);
active_lessons(records) drops the tombstones and every record whose id a tombstone names, keeping file order.
"""

import json
import os
import re
from typing import List

# Shortest lesson-id reference a correction may use: ins-<10-digit epoch seconds> (remember_trade_lesson --corrects
# and the brief's correction pairing)
LESSON_ID_MIN_PREFIX_RE = re.compile(r"ins-\d{10}", re.IGNORECASE)


def read_records(path: str) -> List[dict]:
    """JSON-object lines of `path`, in file order ([] when missing); malformed lines are skipped. Raises OSError
    when the file exists but cannot be read."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def active_lessons(records: List[dict]) -> List[dict]:
    """Non-tombstone records (falsy `superseded`) whose id no tombstone (truthy `superseded`) names, in file order."""
    superseded = {r.get("id") for r in records if r.get("superseded")}
    return [r for r in records if not r.get("superseded") and r.get("id") not in superseded]


def read_active_lessons(path: str) -> List[dict]:
    """active_lessons(read_records(path))."""
    return active_lessons(read_records(path))
