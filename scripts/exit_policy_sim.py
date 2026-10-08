#!/usr/bin/env python3
"""
exit_policy_sim.py - Offline exit-policy simulator on the desk's real trade paths (issue #182).

Read-only and unsigned: it never places, changes or cancels orders and sends no signed request. Inputs are the closed
rows of logs/trade_outcomes.jsonl (scripts/trade_outcomes.py) for --env, public klines
(utils/trade_excursion.fetch_klines_range, 1m and 15m, paged 1000 per request (weight 5), 0.2 s between pages, HTTP
429 retried up to 3 tries honouring Retry-After (418 / an over-cap Retry-After never), then "klines_error"; 6 s timeout) and one public
GET /fapi/v1/exchangeInfo per run (tick size for the trail engine). A row needs initial_risk, entry_ts, entry_price,
sl_price, tp1_price and tp2_price; others are counted under "skipped" by reason ("no_entry_fill" rows, and
"malformed" lines of the outcomes file, included).

Replay (per trade, per policy, 1m resolution, from the first full 1m bar after entry to entry + --horizon-hours; the
entry price is the row's entry_vwap (where the position really started; initial_risk is the sized |audit entry - SL|), else entry_price, for every policy, fee and level;
the trail engine therefore gets the VWAP entry, while the live guardian hands it the audit entry_price, so on a slipped
entry the replayed break-even / profit-lock levels differ slightly from live):
  - Worst case first: a 1m bar touching the current stop (LONG low <= stop, SHORT high >= stop) exits the remaining
    size at the stop (taker), even when a TP level is inside the same bar. Stop exits fill AT the stop price, with no
    gap slippage past it (slightly optimistic).
  - Otherwise a TP leg fills only when price trades THROUGH it (LONG high > tp, SHORT low < tp), at the TP price
    (maker). Legs are fractions of the position (30/70 by default); lot rounding is ignored.
  - Trailing uses the live engine, dynamic_exit_manager.calculate_structural_stop, fed with injected klines_15m (the
    last 98 closed 15m bars plus a forming stand-in row built from the forming candle's 1m bars) and filters: on every
    closed 1m bar (--trail-cadence 1m, default, like the 60 s guardian) or only on 15m closes (--trail-cadence 15m,
    faster), and always in the minute TP1 fills, with mark = that 1m close and, after a TP1 fill, the intrabar extreme
    of the live window (dem._intrabar_start_ms: the forming 15m candle, plus the fill candle when it is the previous
    one). Policy "current" uses the profile defaults (trail_activation "r_only", issue #205); "legacy_r_or_atr" the
    pre-#205 activation (+1R or +2x ATR_15m). The TP1 fill counts
    as verified (KEYS mode); MCP mode never verifies TP1 live, so fidelity there is lower. A new stop is applied only
    when should_update and it stays on the protective side of the bar close; YOLO rows are not trailed before TP1.
  - At the horizon (or the end of the available data) the remaining size is marked at the last close ("capped").
  - Fees: entry taker, TP legs maker, stop and capped exits taker (--taker-fee / --maker-fee, Binance USD-M base
    tier by default), expressed in R: fee_rate x price / R per leg.
Results are in-sample on a small sample (insufficient_sample below MIN_SAMPLE trades): use them to compare policies,
not as a forecast. The "fidelity" block compares the "current" policy's simulated R with each row's realized_r_net,
over all rows and over rows with an exact entry time only. A row with entry_match "legacy" (or, without
entry_match, entry_commission_included false) has an approximate entry_ts (trade_outcomes' audit timestamp - 120 s
fallback): flagged entry_ts_approx, counted in the warnings, skipped with --exact-entry-only (reason
"entry_ts_approx"). The warnings also carry the ranking caveat (policies are counterfactuals; promoting one to live
settings requires a reviewed PR) and the auth_mode note (the replay assumes verified TP1, KEYS mode).
Capture ratio = sum R / sum MFE_R, with MFE over the whole horizon (including after the exit): not comparable to
trade_outcomes' capture_ratio (MFE up to the real exit). Per trade: atr_r (ATR_15m at entry / R) and lock_binding
(the profit lock set the stop at least once).

Output: logs/exit_policy_sim.json (--out PATH: a file inside logs/, else exit 2 before any read; rewritten atomically)
and, with --json, the same object on stdout.
Exit code 0 when at least one trade was simulated, 1 when none, 2 on bad arguments.

Usage:
  python3 scripts/exit_policy_sim.py [--env prod|testnet] [--outcomes PATH] [--policies a,b] [--horizon-hours 48]
      [--taker-fee 0.0005] [--maker-fee 0.0002] [--trail-cadence 1m|15m] [--exact-entry-only] [--json] [--out PATH]
"""

import argparse
import bisect
import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
import dynamic_exit_manager as dem
import user_profile
from decimal import Decimal
from utils.atomic_writer import path_inside_dir
from utils.env_resolver import resolve_env
from utils import trade_excursion

BAR_1M_MS = trade_excursion.BAR_MS
BAR_15M_MS = 15 * BAR_1M_MS
WARMUP_15M_BARS = 98  # closed 15m bars handed to the trail engine (+1 forming row = the live limit=99 read)
KLINES_LIMIT = trade_excursion.KLINES_PAGE_LIMIT  # 1000, weight 5 per page
KLINES_PAGE_SLEEP_SECONDS = trade_excursion.KLINES_PAGE_SLEEP_SECONDS
KLINES_MAX_TRIES = trade_excursion.KLINES_MAX_TRIES  # per page on HTTP 429, honouring Retry-After (418 is never retried)
KLINES_TIMEOUT_SECONDS = 6
EXCHANGE_INFO_TIMEOUT_SECONDS = 6
MIN_WARMUP_BARS = 15  # calculate_structural_stop needs at least 15 closed 15m bars
DEFAULT_HORIZON_HOURS = 48.0
DEFAULT_TAKER_FEE = 0.0005
DEFAULT_MAKER_FEE = 0.0002
MIN_SAMPLE = 30
IN_SAMPLE_NOTE = "Results are in-sample on the desk's own trades: compare policies, do not read them as a forecast."
RANKING_NOTE = ("Ranking caveat: policies are counterfactuals; promoting one to live settings requires a reviewed PR "
                "(tp2_2_5r is below 3:1 R:R, close_at_0_5r truncates the right tail).")
AUTH_MODE_NOTE = ("auth_mode: the replay counts TP1 fills as verified (KEYS mode); in MCP mode the live guardian "
                  "never verifies TP1, so post-TP1 trailing there differs from the simulation.")
EXIT_KINDS = ("sl", "trail_stop", "be", "tp1+trail", "tp2", "capped", "target")
SKIP_REASONS = ("other_env", "no_entry_fill", "not_closed", "no_risk", "no_levels", "entry_ts_approx",
                "klines_error", "warmup_short", "filters_error", "malformed")
TRAIL_CADENCES = ("1m", "15m")


def entry_ts_approx(row):
    """True when the row's entry_ts is trade_outcomes' legacy fallback (audit timestamp - 120 s, no entry fill
    matched): entry_match "legacy", or, for rows written before entry_match existed, entry_commission_included
    false."""
    if row.get("entry_match") is not None:
        return row.get("entry_match") == "legacy"
    return row.get("entry_commission_included", True) is False


def entry_basis(row):
    """Replay entry price: the row's entry_vwap (VWAP of the matched entry fills; initial_risk stays the sized risk) when
    present and > 0, else entry_price."""
    vwap = _num(row.get("entry_vwap"))
    return vwap if vwap and vwap > 0 else float(row["entry_price"])


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _default_lock():
    em = user_profile.get_exit_management(profile={})  # defaults only, never the user's profile file
    em.pop("warnings", None)
    return em


def build_policies():
    """Fixed policy registry: name -> settings. tp2_r None = the row's TP2; lock None = no profit lock."""
    lock = _default_lock()
    base = {"tp1_frac": 0.3, "tp2_frac": 0.7, "tp2_r": None, "trail": True, "lock": lock, "be_on_tp1": False,
            "target_r": None}
    gap = dict(lock, profit_lock_enabled=True, extend_last_step=True,
               profit_lock_steps=[{"mfe_r": 1.0, "lock_r": 0.0}, {"mfe_r": 1.75, "lock_r": 1.0},
                                  {"mfe_r": 2.75, "lock_r": 2.0}])
    return {
        "current": dict(base),
        "legacy_r_or_atr": dict(base, lock=dict(lock, trail_activation="r_or_atr")),  # pre-#205 activation
        "r_and_atr": dict(base, lock=dict(lock, trail_activation="r_and_atr")),  # +1R and +2x ATR_15m
        "current_no_lock": dict(base, lock={"profit_lock_enabled": False}),
        "tp2_2_5r": dict(base, tp2_r=2.5),
        "lock_gap_0_75": dict(base, lock=gap),
        "split_50_50": dict(base, tp1_frac=0.5, tp2_frac=0.5),
        "fixed_targets": dict(base, trail=False, lock=None, be_on_tp1=True),
        "close_at_0_5r": dict(base, trail=False, lock=None, target_r=0.5),
    }


POLICY_NAMES = tuple(build_policies())


# ---------------------------------------------------------------------------------------------------------- inputs

def _logs_dir():
    return os.path.join(eft._workspace_dir(), "logs")


def _read_jsonl(path):
    """(JSON-object lines of path, malformed line count); ([], 0) when missing. A non-empty line that is not a JSON
    object is malformed."""
    if not os.path.exists(path):
        return [], 0
    out, malformed = [], 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if isinstance(rec, dict):
                out.append(rec)
            else:
                malformed += 1
    return out, malformed


def select_rows(rows, env, exact_entry_only=False):
    """(usable rows sorted by entry_ts, skipped {reason: count}). exact_entry_only skips rows whose entry_ts is the
    approximate fallback (entry_ts_approx)."""
    skipped = {}
    out = []

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for r in rows:
        if str(r.get("env") or "").strip().lower() != env:
            skip("other_env")
        elif r.get("status") == "no_entry_fill":
            skip("no_entry_fill")
        elif r.get("status") != "closed":
            skip("not_closed")
        elif not _num(r.get("initial_risk")) or _num(r.get("initial_risk")) <= 0:
            skip("no_risk")
        elif (str(r.get("direction") or "").upper() not in ("LONG", "SHORT")
              or any(not _num(r.get(k)) or _num(r.get(k)) <= 0
                     for k in ("entry_ts", "entry_price", "sl_price", "tp1_price", "tp2_price"))):
            skip("no_levels")
        elif exact_entry_only and entry_ts_approx(r):
            skip("entry_ts_approx")
        else:
            out.append(r)
    out.sort(key=lambda r: (_num(r.get("entry_ts"), 0), str(r.get("symbol"))))
    return out, skipped


def fetch_range(symbol, interval, start_ms, end_ms, env):
    """Raw klines of [start_ms, end_ms) in pages of KLINES_LIMIT (public endpoint), KLINES_PAGE_SLEEP_SECONDS apart,
    through the shared trade_excursion.fetch_klines_pages (429 backoff, no retry on 418). Raises on a failed read."""
    step = BAR_15M_MS if interval == "15m" else BAR_1M_MS
    return trade_excursion.fetch_klines_pages(symbol, interval, start_ms, end_ms, env, step, limit=KLINES_LIMIT,
                                              timeout=KLINES_TIMEOUT_SECONDS, page_sleep=KLINES_PAGE_SLEEP_SECONDS,
                                              max_tries=KLINES_MAX_TRIES)


def fetch_exchange_info(env):
    """Public, unsigned GET /fapi/v1/exchangeInfo of env's host (one request per run)."""
    url = f"{trade_excursion.klines_base_url(env)}/fapi/v1/exchangeInfo"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=EXCHANGE_INFO_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode())


def parse_filters(info):
    """{SYMBOL: filters dict shaped like execute_futures_trade.get_symbol_filters} of an exchangeInfo payload."""
    out = {}
    for s in (info or {}).get("symbols", []) if isinstance(info, dict) else []:
        try:
            lot = [f for f in s["filters"] if f["filterType"] == "LOT_SIZE"][0]
            price = [f for f in s["filters"] if f["filterType"] == "PRICE_FILTER"][0]
            notional = [f for f in s["filters"] if f["filterType"] == "MIN_NOTIONAL"]
            out[str(s["symbol"]).upper()] = {
                "stepSize": float(lot["stepSize"]),
                "minQty": float(lot["minQty"]),
                "tickSize": float(price["tickSize"]),
                "precision_qty": abs(Decimal(str(lot["stepSize"])).as_tuple().exponent),
                "precision_price": abs(Decimal(str(price["tickSize"])).as_tuple().exponent),
                "minNotional": float(notional[0]["notional"]) if notional else 5.0,
            }
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------------------------------------- replay

class TradePath:
    """Price data of one trade: closed 1m bars after entry up to the horizon and 15m bars from the warmup on."""

    def __init__(self, row, k1m, k15m, horizon_ms, now_ms):
        self.entry_ms = int(_num(row["entry_ts"]))
        end_ms = min(self.entry_ms + horizon_ms, now_ms)
        self.bars = [k for k in k1m if int(k[0]) + BAR_1M_MS <= end_ms]
        self.k15m = sorted((k for k in k15m if int(k[0]) + BAR_15M_MS <= now_ms), key=lambda k: int(k[0]))
        self.opens15 = [int(k[0]) for k in self.k15m]
        self.opens1 = [int(k[0]) for k in self.bars]
        self.warmup = sum(1 for o in self.opens15 if o < self.entry_ms)

    def closed_15m(self, t_end_ms):
        """The last WARMUP_15M_BARS 15m bars closed at t_end_ms."""
        idx = bisect.bisect_right(self.opens15, t_end_ms - BAR_15M_MS)
        return self.k15m[max(0, idx - WARMUP_15M_BARS):idx]

    def window_15m(self, t_end_ms):
        """closed_15m(t_end_ms) plus a forming stand-in row (dropped by dem) built from the closed 1m bars after
        entry of the forming 15m candle (the last closed 15m close when there are none)."""
        closed = self.closed_15m(t_end_ms)
        forming_open = t_end_ms // BAR_15M_MS * BAR_15M_MS
        lo = bisect.bisect_left(self.opens1, forming_open)
        hi = bisect.bisect_right(self.opens1, t_end_ms - BAR_1M_MS)
        mins = self.bars[lo:hi]
        if mins:
            forming = [forming_open, mins[0][1], str(max(float(k[2]) for k in mins)),
                       str(min(float(k[3]) for k in mins)), mins[-1][4], "0", forming_open + BAR_15M_MS - 1]
        else:
            c = closed[-1][4] if closed else "0"
            forming = [forming_open, c, c, c, c, "0", forming_open + BAR_15M_MS - 1]
        return closed + [forming]

    def atr_r(self, risk):
        """ATR_15m (dem.calculate_atr, period 14) of the 15m bars closed at entry, in R."""
        closed = self.closed_15m(self.entry_ms)
        if not closed or not risk:
            return None
        atr = dem.calculate_atr([float(k[2]) for k in closed], [float(k[3]) for k in closed],
                                [float(k[4]) for k in closed], period=14)
        return round(atr / risk, 6)

    def mfe_r(self, is_long, entry, risk):
        if not self.bars:
            return 0.0
        best = max(float(k[2]) for k in self.bars) if is_long else min(float(k[3]) for k in self.bars)
        return max(0.0, ((best - entry) if is_long else (entry - best)) / risk)


def replay(row, path, policy, filters, env, taker_fee, maker_fee, trail_cadence="1m"):
    """Simulated outcome of one trade under one policy: {r, gross_r, fees_r, exit_kind, capped, lock_binding, exits}.
    trail_cadence "1m": the trail engine runs on every closed 1m bar (the live guardian runs every 60 s); "15m": only
    on 15m closes. Both also run it in the minute TP1 fills."""
    is_long = str(row["direction"]).upper() == "LONG"
    sign = 1.0 if is_long else -1.0
    entry = entry_basis(row)
    risk = float(row["initial_risk"])
    planned_sl = float(row["sl_price"])
    entry_ts_s = path.entry_ms / 1000.0
    is_yolo = bool(row.get("is_yolo"))

    if policy["target_r"] is not None:
        legs = [{"name": "target", "frac": 1.0, "price": entry + sign * policy["target_r"] * risk}]
    else:
        tp2 = float(row["tp2_price"]) if policy["tp2_r"] is None else entry + sign * policy["tp2_r"] * risk
        legs = [{"name": "tp1", "frac": policy["tp1_frac"], "price": float(row["tp1_price"])},
                {"name": "tp2", "frac": policy["tp2_frac"], "price": tp2}]
    tp1_price = float(row["tp1_price"])
    lock = policy["lock"] if policy["lock"] is not None else {"profit_lock_enabled": False}

    stop, stop_kind = planned_sl, "initial"
    remaining = 1.0
    tp1_filled = False
    exits = []  # (frac, price, fee_rate, kind)
    last_close = entry
    lock_binding = False

    def stop_exit_kind():
        if stop_kind == "initial":
            return "sl"
        if stop_kind == "be":
            return "be"
        return "tp1+trail" if tp1_filled else "trail_stop"

    def trail(bar_close, t_end_ms):
        nonlocal stop, stop_kind, lock_binding
        if is_yolo and not tp1_filled:
            return  # YOLO: never trailed before TP1 (dynamic_exit_manager, update_position_to_structural_stop)
        intrabar = None
        if tp1_filled:
            start = dem._intrabar_start_ms(entry_ts_s, t_end_ms)  # the live window (forming + previous fill candle)
            closed_1m = [k for k in path.bars if start <= int(k[0]) and int(k[0]) + BAR_1M_MS <= t_end_ms]
            if closed_1m:
                intrabar = (max(float(k[2]) for k in closed_1m) if is_long else min(float(k[3]) for k in closed_1m))
        calc = dem.calculate_structural_stop(
            row["symbol"], "LONG" if is_long else "SHORT", entry, current_sl_price=stop, target_env=env,
            planned_sl=planned_sl, entry_ts=entry_ts_s, tp1_filled=tp1_filled, mark_price=bar_close,
            reference_source="trade_audit", intrabar_extreme=intrabar, tp1_price=tp1_price if tp1_filled else None,
            exit_management=lock, klines_15m=path.window_15m(t_end_ms), filters=filters)
        if not calc or not calc.get("should_update"):
            return
        new_sl = float(calc["new_structural_sl"])
        if (is_long and new_sl >= bar_close) or (not is_long and new_sl <= bar_close):
            return  # would trigger immediately (live skip)
        if (new_sl > stop) if is_long else (new_sl < stop):
            stop, stop_kind = new_sl, "trail"
            lock_binding = lock_binding or bool((calc.get("profit_lock") or {}).get("binding"))

    for k in path.bars:
        open_ms = int(k[0])
        high, low, close = float(k[2]), float(k[3]), float(k[4])
        t_end = open_ms + BAR_1M_MS
        last_close = close
        if (low <= stop) if is_long else (high >= stop):
            exits.append((remaining, stop, taker_fee, stop_exit_kind()))
            remaining = 0.0
            break
        tp1_now = False
        for leg in legs:
            if leg.get("filled"):
                continue
            if (high > leg["price"]) if is_long else (low < leg["price"]):
                leg["filled"] = True
                frac = min(leg["frac"], remaining)
                remaining -= frac
                exits.append((frac, leg["price"], maker_fee, leg["name"]))
                if leg["name"] == "tp1":
                    tp1_filled = tp1_now = True
        if remaining <= 1e-12:
            remaining = 0.0
            break
        if tp1_now and policy["be_on_tp1"]:
            be = entry * ((1 + eft.TRUE_NET_BE_FEE_BUFFER) if is_long else (1 - eft.TRUE_NET_BE_FEE_BUFFER))
            if (be > stop) if is_long else (be < stop):
                stop, stop_kind = be, "be"
        if policy["trail"] and (tp1_now or trail_cadence == "1m" or t_end % BAR_15M_MS == 0):
            trail(close, t_end)

    capped = remaining > 0
    if capped:
        exits.append((remaining, last_close, taker_fee, "capped"))
    gross = sum(f * sign * (p - entry) for f, p, _, _ in exits) / risk
    fees = (taker_fee * entry + sum(f * rate * p for f, p, rate, _ in exits)) / risk
    last_kind = exits[-1][3]
    if last_kind == "tp1":  # cannot end on TP1 alone (TP2 > 0), kept for completeness
        last_kind = "tp1+trail"
    return {"r": round(gross - fees, 6), "gross_r": round(gross, 6), "fees_r": round(fees, 6),
            "exit_kind": last_kind, "capped": capped, "lock_binding": lock_binding,
            "exits": [{"frac": round(f, 6), "price": p, "kind": kind} for f, p, _, kind in exits]}


# --------------------------------------------------------------------------------------------------------- metrics

def _mean(values):
    return round(sum(values) / len(values), 6) if values else None


def policy_metrics(results):
    """results: [{"r", "mfe_r", "entry_ts", "exit_kind", "capped"}] of one policy."""
    rs = [x["r"] for x in results]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    peak = cum = dd = 0.0
    for x in sorted(results, key=lambda x: x["entry_ts"]):
        cum += x["r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    with_mfe = [x for x in results if x["mfe_r"] > 0]
    mfe_sum = sum(x["mfe_r"] for x in with_mfe)
    kinds = {k: 0 for k in EXIT_KINDS}
    for x in results:
        kinds[x["exit_kind"]] = kinds.get(x["exit_kind"], 0) + 1
    return {
        "n": len(rs),
        "win_rate": round(len(wins) / len(rs), 6) if rs else None,
        "expectancy_r": _mean(rs),
        "total_r": round(sum(rs), 6),
        "profit_factor_r": round(sum(wins) / abs(sum(losses)), 6) if losses else None,
        "avg_win_r": _mean(wins),
        "avg_loss_r": _mean(losses),
        "max_drawdown_r": round(dd, 6),
        "capture_ratio": round(sum(x["r"] for x in with_mfe) / mfe_sum, 6) if mfe_sum > 0 else None,
        "exit_kinds": kinds,
        "capped": sum(1 for x in results if x["capped"]),
    }


def implied_fee_rate(rows):
    """Effective fee rate of the rows' recorded exit legs (USDT commission / notional), for information only."""
    comm = notional = 0.0
    for r in rows:
        for leg in r.get("legs") or []:
            if str(leg.get("commission_asset") or "").upper() != "USDT":
                continue
            qty, price = _num(leg.get("qty"), 0.0), _num(leg.get("price"), 0.0)
            comm += _num(leg.get("commission"), 0.0)
            notional += qty * price
    return round(comm / notional, 8) if notional > 0 else None


# ------------------------------------------------------------------------------------------------------------ run

def simulate(rows_all, env, policy_names, horizon_hours, taker_fee, maker_fee, now_ms=None, *, trail_cadence="1m",
             exact_entry_only=False, malformed=0):
    """malformed: malformed lines of the outcomes file (counted in skipped["malformed"] when > 0)."""
    started = time.monotonic()
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    horizon_ms = int(horizon_hours * 3600 * 1000)
    registry = build_policies()
    rows, skipped = select_rows(rows_all, env, exact_entry_only=exact_entry_only)
    if malformed:
        skipped["malformed"] = int(malformed)
    warnings = [IN_SAMPLE_NOTE, RANKING_NOTE, AUTH_MODE_NOTE]

    filters_by_symbol = None
    if rows:
        try:
            filters_by_symbol = parse_filters(fetch_exchange_info(env))
        except Exception as e:
            warnings.append(f"exchangeInfo unavailable: {type(e).__name__}: {e}"[:200])
            filters_by_symbol = {}

    per_policy = {name: [] for name in policy_names}
    fidelity_rows = []
    simulated = []
    for row in rows:
        symbol = str(row["symbol"]).upper()
        filters = filters_by_symbol.get(symbol)
        if not filters:
            skipped["filters_error"] = skipped.get("filters_error", 0) + 1
            continue
        entry_ms = int(_num(row["entry_ts"]))
        end_ms = entry_ms + horizon_ms
        try:
            k1m = fetch_range(symbol, "1m", trade_excursion.first_post_entry_bar_ms(entry_ms / 1000.0), end_ms, env)
            k15m = fetch_range(symbol, "15m", entry_ms - WARMUP_15M_BARS * BAR_15M_MS, end_ms, env)
        except Exception as e:
            skipped["klines_error"] = skipped.get("klines_error", 0) + 1
            warnings.append(f"{symbol} {entry_ms}: klines unavailable ({type(e).__name__})")
            continue
        path = TradePath(row, k1m, k15m, horizon_ms, now_ms)
        if not path.bars:
            skipped["klines_error"] = skipped.get("klines_error", 0) + 1
            continue
        if path.warmup < MIN_WARMUP_BARS:
            skipped["warmup_short"] = skipped.get("warmup_short", 0) + 1
            continue
        is_long = str(row["direction"]).upper() == "LONG"
        mfe_r = path.mfe_r(is_long, entry_basis(row), float(row["initial_risk"]))
        atr_r = path.atr_r(float(row["initial_risk"]))
        approx = entry_ts_approx(row)
        simulated.append(row)
        for name in policy_names:
            res = replay(row, path, registry[name], filters, env, taker_fee, maker_fee, trail_cadence=trail_cadence)
            per_policy[name].append(dict(res, mfe_r=mfe_r, entry_ts=entry_ms, symbol=symbol, atr_r=atr_r,
                                         entry_ts_approx=approx))
            if name == "current":
                real = _num(row.get("realized_r_net"))
                fidelity_rows.append({"symbol": symbol, "direction": row["direction"], "entry_ts": entry_ms,
                                      "entry_ts_approx": approx, "sim_r": res["r"], "realized_r_net": real,
                                      "diff": round(res["r"] - real, 6) if real is not None else None})

    n_approx = sum(1 for r in simulated if entry_ts_approx(r))
    if n_approx:
        warnings.append(f"{n_approx} trade(s) with approximate entry_ts (audit timestamp - 120 s, no entry fill "
                        f"matched): their replay starts at a guessed time (--exact-entry-only skips them)")
    policies = {name: policy_metrics(per_policy[name]) for name in policy_names}
    trade_keys = ("symbol", "entry_ts", "entry_ts_approx", "r", "exit_kind", "capped", "mfe_r", "atr_r",
                  "lock_binding")
    for name in policy_names:
        policies[name]["trades"] = [{k: x[k] for k in trade_keys} for x in per_policy[name]]
    diffs = [abs(f["diff"]) for f in fidelity_rows if f["diff"] is not None]
    exact = [abs(f["diff"]) for f in fidelity_rows if f["diff"] is not None and not f["entry_ts_approx"]]
    fidelity = ({"policy": "current", "trades": fidelity_rows, "mean_abs_diff_r": _mean(diffs),
                 "compared": len(diffs), "mean_abs_diff_r_exact_entry": _mean(exact), "compared_exact_entry": len(exact)}
                if "current" in policy_names else None)
    ranked = sorted((n for n in policy_names if policies[n]["n"]),
                    key=lambda n: policies[n]["expectancy_r"], reverse=True)
    ranking = [{"policy": n, "expectancy_r": policies[n]["expectancy_r"], "n": policies[n]["n"],
                "insufficient_sample": policies[n]["n"] < MIN_SAMPLE} for n in ranked]
    since_rows = sorted({str(r.get("since")) for r in simulated if r.get("since")})
    return {"ok": bool(simulated), "env": env, "since_rows": since_rows, "horizon_hours": horizon_hours,
            "fees": {"taker": taker_fee, "maker": maker_fee, "entry": "taker",
                     "implied_exit_fee_rate": implied_fee_rate(simulated)},
            "n_trades": len(simulated), "skipped": skipped, "policies": policies, "fidelity": fidelity,
            "ranking": ranking, "warnings": warnings, "trail_cadence": trail_cadence,
            "exact_entry_only": exact_entry_only, "elapsed_seconds": round(time.monotonic() - started, 3)}


def _write_json_atomic(path, obj):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{os.path.basename(path)}.tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _fmt(value, pct=False):
    if value is None:
        return "-"
    return f"{value * 100:.1f}" if pct else f"{value:+.3f}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline exit-policy simulator on closed trades (read-only)")
    parser.add_argument("--env", choices=["prod", "testnet"], default=None, help="Defaults to resolve_env()")
    parser.add_argument("--outcomes", default=None, help="Input JSONL (default logs/trade_outcomes.jsonl)")
    parser.add_argument("--policies", default=None, help=f"Comma-separated subset of: {', '.join(POLICY_NAMES)}")
    parser.add_argument("--horizon-hours", type=float, default=DEFAULT_HORIZON_HOURS, dest="horizon_hours")
    parser.add_argument("--taker-fee", type=float, default=DEFAULT_TAKER_FEE, dest="taker_fee")
    parser.add_argument("--maker-fee", type=float, default=DEFAULT_MAKER_FEE, dest="maker_fee")
    parser.add_argument("--trail-cadence", choices=TRAIL_CADENCES, default="1m", dest="trail_cadence",
                        help="Run the trail engine on every closed 1m bar (default, like the 60 s guardian) or on 15m closes")
    parser.add_argument("--exact-entry-only", action="store_true", dest="exact_entry_only",
                        help="Skip rows whose entry_ts is approximate (no entry fill matched)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print the result as JSON")
    parser.add_argument("--out", default=None, help="Output JSON (default logs/exit_policy_sim.json)")
    args = parser.parse_args(argv)

    if args.policies:
        names = [n.strip() for n in args.policies.split(",") if n.strip()]
        unknown = [n for n in names if n not in POLICY_NAMES]
        if unknown or not names:
            parser.print_usage(sys.stderr)
            print(f"exit_policy_sim: unknown policies {unknown} (valid: {', '.join(POLICY_NAMES)})", file=sys.stderr)
            return 2
        names = list(dict.fromkeys(names))
    else:
        names = list(POLICY_NAMES)
    if not args.horizon_hours > 0 or args.taker_fee < 0 or args.maker_fee < 0:
        print("exit_policy_sim: --horizon-hours must be > 0 and fees >= 0", file=sys.stderr)
        return 2
    try:
        env = resolve_env(args.env)
    except ValueError as e:
        print(f"exit_policy_sim: invalid environment: {e}", file=sys.stderr)
        return 2
    out_path = args.out or os.path.join(_logs_dir(), "exit_policy_sim.json")
    if not path_inside_dir(out_path, _logs_dir()):  # issue #191: before any read or request
        print(f"exit_policy_sim: --out must be a file inside {_logs_dir()} (got {out_path!r})", file=sys.stderr)
        return 2

    rows, malformed = _read_jsonl(args.outcomes or os.path.join(_logs_dir(), "trade_outcomes.jsonl"))
    result = simulate(rows, env, names, args.horizon_hours, args.taker_fee, args.maker_fee,
                      trail_cadence=args.trail_cadence, exact_entry_only=args.exact_entry_only, malformed=malformed)
    _write_json_atomic(out_path, result)

    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        print(f"exit_policy_sim {env}: {result['n_trades']} trade(s), horizon {args.horizon_hours:g}h, "
              f"skipped {result['skipped'] or 'none'} -> {out_path}")
        print(f"  {'policy':<16}{'n':>4}{'win %':>8}{'exp R':>9}{'total R':>9}{'PF':>8}{'maxDD R':>9}{'capture':>9}")
        for name in names:
            m = result["policies"][name]
            pf = "-" if m["profit_factor_r"] is None else f"{m['profit_factor_r']:.2f}"
            cap = "-" if m["capture_ratio"] is None else f"{m['capture_ratio']:.2f}"
            print(f"  {name:<16}{m['n']:>4}{_fmt(m['win_rate'], pct=True):>8}{_fmt(m['expectancy_r']):>9}"
                  f"{_fmt(m['total_r']):>9}{pf:>8}{m['max_drawdown_r']:>9.3f}{cap:>9}")
        fid = result["fidelity"]
        if fid and fid["mean_abs_diff_r"] is not None:
            exact = fid["mean_abs_diff_r_exact_entry"]
            print(f"  fidelity (current vs realized_r_net): mean |diff| {fid['mean_abs_diff_r']:.3f}R "
                  f"over {fid['compared']} trade(s); exact entry only "
                  f"{'-' if exact is None else f'{exact:.3f}R'} over {fid['compared_exact_entry']}")
        for w in result["warnings"]:
            print(f"  note: {w}")
    return 0 if result["n_trades"] else 1


if __name__ == "__main__":
    sys.exit(main())
