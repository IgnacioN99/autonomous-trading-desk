#!/usr/bin/env python3
"""
yolo_scan_health.py - Persistent health of the Barbell YOLO scan (issue #66).

screening_pipeline.py (and prime_evaluator_brief.py when the whole pipeline fails) record the final YOLO slot
status of every run that requested the YOLO scan in logs/yolo_scan_health.json:
  - UNAVAILABLE increments `consecutive_unavailable` and stores `last_unavailable_reason` (fixed desk text only,
    never exception messages) and `last_unavailable_ts`;
  - any other status (ACTIVE / INACTIVE / DISABLED) resets the counter and stores `last_ok_ts`;
  - `last_status` and `updated_ts` are always written.
trading_doctor.py warns when `consecutive_unavailable >= YOLO_UNAVAILABLE_WARN_AFTER`.

Fail-open: recording never raises (one stderr line on failure); it must never break the pipeline or the brief.
Stdlib + utils.atomic_writer only.
"""

import os
import sys
import time
from typing import Optional

from utils.atomic_writer import atomic_write_json, read_json_safe  # callers put scripts/ on sys.path

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
HEALTH_FILE = os.path.join(LOGS_DIR, "yolo_scan_health.json")  # module-level so tests can redirect it

YOLO_UNAVAILABLE_WARN_AFTER = 3
_REASON_MAX_CHARS = 200


def read_health() -> dict:
    """Current health record ({} when the file is missing or corrupt)."""
    try:
        data = read_json_safe(HEALTH_FILE, default=None)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def record_scan(status: str, reason: Optional[str] = None, now: Optional[float] = None) -> None:
    """Records the final YOLO slot status of one run. `reason` must be fixed desk text (UNAVAILABLE only)."""
    try:
        ts = float(time.time() if now is None else now)
        status = str(status or "").upper()
        health = read_health()
        try:
            count = int(health.get("consecutive_unavailable", 0))
        except (TypeError, ValueError):
            count = 0
        if status == "UNAVAILABLE":
            health["consecutive_unavailable"] = max(0, count) + 1
            health["last_unavailable_reason"] = str(reason or "unspecified")[:_REASON_MAX_CHARS]
            health["last_unavailable_ts"] = ts
        else:
            health["consecutive_unavailable"] = 0
            health["last_ok_ts"] = ts
        health["last_status"] = status
        health["updated_ts"] = ts
        atomic_write_json(HEALTH_FILE, health)
    except Exception as e:  # fail-open: observability must never break the scan path
        try:
            sys.stderr.write(f"yolo_scan_health: could not record the YOLO scan status ({type(e).__name__})\n")
        except Exception:
            pass
