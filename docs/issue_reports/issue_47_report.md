# Issue #47 Fix Report: Gate 0A Max Open Positions Slot Reconciliation (Fill Gap Protection)

**Date**: 2026-10-06  
**Branch**: `fix/issue-47-max-open-positions-slot-gap`  
**Target Repository**: `autonomous-trading-desk`  
**Target Worktree**: `/mnt/c/Users/Nacho/Documents/trading-wt-issue-47`  

---

## 1. Problem Overview

Prior to this fix, Gate 0A (`check_max_open_positions`) in `scripts/execute_futures_trade.py` calculated committed exposure slots solely based on:
1. Active open positions reported in `logs/session_state.json` (`open_count`).
2. Pending resting entries in `logs/pending_entries.json` (`pending_symbols`).

This left two critical concurrency gaps where a position was committed on the exchange, but not accounted for in Gate 0A:
1. **Resting Entry Fill Gap**: When an untriggered `STOP_MARKET` or resting `LIMIT` entry fills on the exchange, `protect_pending_entries()` drops the record from `logs/pending_entries.json` and appends an entry fill record to `logs/trades_audit.jsonl`. However, `logs/session_state.json` is updated asynchronously (via `sync_session_state.py` or position guardian cycles). During the window between fill/drop and the next state sync, the symbol disappeared from `pending_symbols` while not yet appearing in `open_symbols`.
2. **MARKET Order Fill Gap**: An immediate `MARKET` order bypasses `logs/pending_entries.json` entirely and appends directly to `logs/trades_audit.jsonl` upon execution. Until the next session state synchronization cycle, the new position was invisible to Gate 0A.

In both scenarios, concurrent or rapid subsequent trade executions could breach the user profile's `max_open_positions` mechanical limit.

---

## 2. Implemented Architecture & Design Decisions

### Scope & Boundaries
- Only modified `scripts/execute_futures_trade.py` and test suite `tests/test_issue_47_max_open_positions.py`.
- Preserved existing hooks (no modifications to `scripts/hooks/pre_trade_guard.py` to prevent merge collisions with PR #88).

### Reconciliation Logic in `check_max_open_positions(prof, target_env, base_dir=None)`:
1. **State Ledger Extraction**:
   - Reads `logs/session_state.json` to extract `open_count`, `open_symbols`, and `last_sync_ts = int(state_data_pos.get('last_updated_ts', 0))`.
2. **Pending Entries Registry**:
   - Reads `logs/pending_entries.json` via `load_pending_entries(base_dir=base)`.
   - Filters symbols matching `target_env` that are not already in `open_symbols` to form `pending_symbols`.
3. **Audit Ledger Reconciliation (`logs/trades_audit.jsonl`)**:
   - Resolves audit file path respecting `base_dir or _workspace_dir()`.
   - Computes cutoff timestamp: `cutoff_ts = last_sync_ts if last_sync_ts > 0 else (time.time() - 300)`.
   - Filters audit records strictly matching `target_env` (`rec.get('target_env') == target_env` / case-insensitive).
   - Only processes records with `rec_ts >= cutoff_ts`.
   - Identifies valid entry fills (`not event or 'total_qty' in rec`) and failsafe aborts (`event == 'CRITICAL_FAILSAFE_ABORT'`).
   - Tracks the latest entry and abort per symbol, including sequence indices to handle same-second timestamps cleanly.
   - For any entry fill whose symbol is not in `open_symbols` and not in `pending_symbols`:
     - If a posterior abort exists (`abort_ts > entry_ts` or same timestamp and later sequence in log), the position was auto-destructed and closed; slot is not consumed.
     - Otherwise, the symbol is added to `recent_fill_symbols`.
4. **Committed Slots Calculation & Gate Decision**:
   - `committed_count = open_count + len(pending_symbols) + len(recent_fill_symbols)`.
   - If `committed_count >= max_open_positions`: rejects execution.
   - Formats rejection message indicating open, pending, and recent fills when `len(recent_fill_symbols) > 0` (e.g. `(open 1 + pending 0 + recent fills 1 >= max 2)`), while falling back to `(open X + pending Y >= max Z)` when recent fills are 0 to preserve strict backwards compatibility with existing test suites.

---

## 3. Test Coverage

A new dedicated test suite was implemented in `tests/test_issue_47_max_open_positions.py` using `unittest`:
- **`test_pending_fill_gap_blocks_new_entry`**: Resting entry filled and dropped, audit log appended, session state not yet synced -> Gate 0A blocks further entry.
- **`test_market_fill_gap_blocks_new_entry`**: Immediate MARKET fill logged in audit, session state not yet synced -> Gate 0A blocks further entry.
- **`test_sync_handoff_deduplication`**: Once `sync_session_state` updates `session_state.json` with active position and bumped timestamp, Gate 0A counts position once without duplicate slot usage.
- **`test_env_isolation_testnet_and_prod_independent`**: Verifies strict isolation between environments; testnet audit records do not affect PROD slots and vice-versa.
- **`test_failsafe_abort_releases_slot`**: Emergency abort (`CRITICAL_FAILSAFE_ABORT`) frees the slot for new trades.
- **`test_failsafe_abort_same_timestamp_sequential_order`**: Verifies proper ordering handling when entry and abort occur within the same second.
- **`test_new_entry_after_prior_abort_is_counted`**: Verifies that a new entry following an older abort correctly consumes a slot.
- **`test_fallback_300s_window_when_sync_ts_zero`**: Fallback 300s window applies when `session_state.json` timestamp is missing or zero.
- **`test_symbol_in_both_pending_and_audit_not_double_counted`**: Ensures overlapping entries between pending registry and audit log consume only one slot.

### Verification Results:
- `python3 -m unittest tests/test_issue_47_max_open_positions.py`: **9/9 tests passed (1.43s)**.
- `python3 -m unittest tests/test_max_positions_pending.py`: **15/15 tests passed (4.78s)**.
- `python3 -m unittest tests/test_pending_entries.py`: **80/80 tests passed (2.54s)**.

---

## 4. Modified Files
- `scripts/execute_futures_trade.py`:
  - Added optional `base_dir=None` to `pending_entries_path`, `load_pending_entries`, `update_pending_entries`, and `check_max_open_positions`.
  - Enhanced `check_max_open_positions` with `trades_audit.jsonl` reconciliation and slot accounting.
- `tests/test_issue_47_max_open_positions.py`:
  - Complete offline unit test suite for Issue #47.
- `docs/issue_reports/issue_47_report.md`:
  - Detailed fix and architecture documentation.
