#!/usr/bin/env python3
"""
yolo_scan_health.py - Persistent health of the Barbell YOLO scan (issue #66).

screening_pipeline.py (and prime_evaluator_brief.py when the whole pipeline fails) record the final YOLO slot
status of every run that requested the YOLO scan in logs/yolo_scan_health.json:
  - UNAVAILABLE increments `consecutive_unavailable` and stores `last_unavailable_reason` (fixed desk text only,
    never exception messages) and `last_unavailable_ts`;
  - any other status (ACTIVE / INACTIVE / DISABLED) resets the counter and stores `last_ok_ts`;
  - `last_status` and `updated_ts` are always written; `last_run_id` is the run id of the recording run
    (issue #91.6: prime_evaluator_brief.py passes DESK_SCAN_RUN_ID to the pipeline; removed when absent).
trading_doctor.py warns when `consecutive_unavailable >= YOLO_UNAVAILABLE_WARN_AFTER`.
The read-modify-write runs under utils/file_lock.py (bounded wait), so concurrent runs do not lose an increment.

Fail-open: recording never raises (one stderr line on failure); it must never break the pipeline or the brief.
Stdlib + utils.atomic_writer / utils.file_lock only.
"""

import os
import sys
import time
from typing import Optional

from utils.atomic_writer import atomic_write_json, read_json_safe  # callers put scripts/ on sys.path
from utils import file_lock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
HEALTH_FILE = os.path.join(LOGS_DIR, "yolo_scan_health.json")  # module-level so tests can redirect it

YOLO_UNAVAILABLE_WARN_AFTER = 3
_REASON_MAX_CHARS = 200
RUN_ID_ENV = "DESK_SCAN_RUN_ID"  # run id passed from prime_evaluator_brief.py to the pipeline subprocess
# Shared YOLO slot text (screening_pipeline.py and prime_evaluator_brief.py; peb does not import the pipeline).
YOLO_DISABLED_STATUS = "DISABLED: yolo_slot_enabled is false in the user profile."


def read_health() -> dict:
    """Current health record ({} when the file is missing or corrupt)."""
    try:
        data = read_json_safe(HEALTH_FILE, default=None)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def record_scan(status: str, reason: Optional[str] = None, now: Optional[float] = None,
                run_id: Optional[str] = None) -> None:
    """Records the final YOLO slot status of one run. `reason` must be fixed desk text (UNAVAILABLE only)."""
    try:
        ts = float(time.time() if now is None else now)
        status = str(status or "").upper()
        with file_lock.locked(HEALTH_FILE):
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
            if run_id:
                health["last_run_id"] = str(run_id)[:64]
            else:
                health.pop("last_run_id", None)
            atomic_write_json(HEALTH_FILE, health)
    except Exception as e:  # fail-open: observability must never break the scan path
        try:
            sys.stderr.write(f"yolo_scan_health: could not record the YOLO scan status ({type(e).__name__})\n")
        except Exception:
            pass
