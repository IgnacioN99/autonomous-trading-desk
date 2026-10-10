#!/usr/bin/env python3
"""
entry_policy_sim.py - Offline counterfactual ENTRY-policy simulator (issue #269), sibling of exit_policy_sim.py.

Read-only and unsigned: it never places, changes or cancels orders and sends no signed request. It changes no live
entry rule: every result is a counterfactual, in-sample replay for the owner to read (decision rule below).

Inputs
  - Real trades: the closed rows of logs/trade_outcomes.jsonl (--outcomes PATH to read another file) for --env,
    selected like exit_policy_sim (exit_policy_sim.select_rows; --exact-entry-only skips approximate entry times).
  - Shadow candidates: the resolved rows of logs/shadow_resolved.jsonl (--shadow PATH), read from this script's own
    logs/ directory. Rows whose outcome / classification is EXPIRED or EXPIRED_UNTRIGGERED count as unfilled signals
    (0R) under every policy. Shadow rows carry no env: --env only picks the klines host.
  - Public klines (1m and 15m, exit_policy_sim.fetch_range: paged, 429 backoff) and one public exchangeInfo per run.

Anchors (no look-ahead: ATR_15m and order flow use only 15m bars CLOSED before the decision time)
  - Real trades: the trigger is NOT stored, so the audit entry_price is its proxy (not entry_vwap) and the touch
    minute is entry_ts. The radar level is approximated as trigger / 1.0005 (LONG) or trigger / 0.9995 (SHORT)
    (broad_market_radar sets trigger = level x 1.0005 / 0.9995). The order placement time is not stored either: the
    alternative triggers are armed at the touch minute (a lower trigger already crossed then fills at that bar).
  - Shadow rows: trigger_price, armed at the first full 1m bar after registered_at_ts; the touch is the first 1m bar
    whose range reaches the trigger within ENTRY_WAIT_MINUTES (the stored 5m activated_at_ts and simulated_pnl_usdt
    are not used).

Policies (1m bars; R = |fill - SL|; SL / TP1 / TP2 stay at the same ABSOLUTE prices, so R:R shifts per fill)
  - current: STOP_MARKET at the trigger, fills at max(trigger, bar open) LONG / min SHORT, taker (modelled baseline).
  - closed_candle_confirm_5m / _15m: enter at the next 1m open after the first 5m / 15m close beyond the trigger
    (closing at or after the touch), taker; missed when the initial SL is touched first or after --confirm-minutes.
  - atr_buffer_0_1 / _0_25 / _0_5: STOP_MARKET at level +/- k x ATR_15m (pure formula, may sit below the current
    trigger), taker, waiting up to ENTRY_WAIT_MINUTES from the arming bar.
  - pullback_limit_0_25 / _0_5: after the touch bar, a maker limit at trigger -/+ p x R (R at the trigger), filled
    only on trade-through, cancelled after --pullback-minutes; maker entry fee.
  - orderflow_veto: skip the signal when the kline-derived OIB = (2 x taker_buy_volume - volume) / volume (kline
    index 9 and 5) of the last 15m bar closed before the touch opposes the direction with |OIB| >= 0.15; otherwise
    the current fill. The same quantity as the radar's OIB, but recomputed from klines: the radar's own OIB, CVD and
    ATR are not persisted. A row without index 9 is unavailable_no_taker_volume (excluded from this policy, never
    guessed).
  - Fill bar worst case: if the fill bar also reaches the initial SL, an immediate SL loss is booked (taker); TPs are
    never credited in the fill bar; a gap through a stop trigger fills at the open. A fill beyond the SL or at / past
    TP1 is a missed fill (the executor would reject those levels).
Exit: the live exit policy (exit_policy_sim.build_policies()["current"]) through exit_policy_sim.replay from the
first full 1m bar after the fill to fill + --horizon-hours. The replay's taker entry fee is corrected for maker
entries after the fact: r += (taker - maker) x fill / R.
Immediate stop-out: the initial SL is touched while the favorable excursion of the bars between the fill bar and the
stop bar stayed < 0.3R (a fill-bar stop counts).

Metrics (real and shadow blocks separate; the real trades decide): n eligible, n filled, fill rate, win %, net
expectancy R per filled trade and per signal (unfilled = 0R), profit factor, max drawdown R, immediate stop-outs and
their change versus current on the same rows, mean R:R shift (R:R to TP2 at the fill minus at the trigger), and the
paired per-signal delta versus current with a seeded cluster bootstrap 95% CI (clusters = dossier_sha256, else the
row). insufficient_sample below MIN_SAMPLE (50) signals. The real block also reports current_real (the actual fill
replayed like exit_policy_sim, with the row's sized risk) and the model-versus-real difference; the shadow block
reports expectancy by trigger distance (trigger_price vs current_price_at_eval). Decision rule (#269): change the
radar or executor entry only if a policy beats current on net expectancy with n >= 50, real trades deciding; the
owner decides through a reviewed PR.

Missing data (not persisted, hence the proxies above): the real trigger price, the order placement time and price at
placement, the radar's OIB / CVD / ATR; trigger distance for real trades. Follow-up (not done here, a HARNESS-file
change): persisting trigger_price, placed_at_ts and the price at placement in the executor audit record.

Output: logs/entry_policy_sim.json (--out PATH: a file inside logs/, else exit 2 before any read; rewritten
atomically) and, with --json, the same object on stdout. Exit code 0 when at least one signal was replayed, 1 when
none, 2 on bad arguments.

Usage:
  python3 scripts/entry_policy_sim.py [--env prod|testnet] [--outcomes PATH] [--shadow PATH] [--policies a,b]
      [--horizon-hours 48] [--taker-fee 0.0005] [--maker-fee 0.0002] [--trail-cadence 1m|15m]
      [--confirm-minutes 60] [--pullback-minutes 30] [--exact-entry-only] [--json] [--out PATH]
"""

import argparse
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute_futures_trade as eft
import exit_policy_sim as eps
import dynamic_exit_manager as dem
from utils.atomic_writer import path_inside_dir
from utils.env_resolver import resolve_env
from utils import trade_excursion

BAR_1M_MS = eps.BAR_1M_MS
BAR_15M_MS = eps.BAR_15M_MS
DEFAULT_CONFIRM_MINUTES = 60
DEFAULT_PULLBACK_MINUTES = 30
ENTRY_WAIT_MINUTES = 90  # resting stop entry lifetime (desk order timeout 60-90 min; shadow_tracker waits 90 min)
LEVEL_OFFSET = 0.0005  # broad_market_radar: trigger = level x (1 + 0.0005) LONG, x (1 - 0.0005) SHORT
OIB_VETO = 0.15
IMMEDIATE_MFE_R = 0.3
MIN_SAMPLE = 50
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 269
EXPIRED_OUTCOMES = ("EXPIRED", "EXPIRED_UNTRIGGERED")
DISTANCE_BUCKETS = (("<0.25%", None, 0.25), ("0.25-1%", 0.25, 1.0), (">=1%", 1.0, None))

COUNTERFACTUAL_NOTE = ("Counterfactual and in-sample: every policy is replayed on the desk's own signals with modelled "
                       "fills; compare policies, do not read the numbers as a forecast. No live entry rule changes.")
DECISION_NOTE = (f"Decision rule (#269): change the radar or executor entry only if a policy beats current on net "
                 f"expectancy with at least {MIN_SAMPLE} signals; real and shadow are reported separately and the real "
                 f"trades decide. The owner decides, through a reviewed PR.")
ANCHOR_NOTE = ("Real trades: the trigger is not stored, so the audit entry_price is its proxy, the touch is entry_ts, "
               "the radar level is trigger / 1.0005 (LONG) or / 0.9995 (SHORT), and alternative triggers are armed at "
               "the touch minute (the placement time is not stored).")
OIB_NOTE = ("orderflow_veto uses the kline-derived OIB (2 x taker-buy volume - volume) / volume of the last 15m bar "
            "closed before the touch: the radar's quantity, recomputed; the radar's own OIB / CVD are not persisted.")
RR_NOTE = ("SL and TPs stay at their absolute prices and R = |fill - SL|: a later or worse fill shrinks R:R "
           "(mean_rr_shift); a fill beyond the SL or at / past TP1 is a missed fill.")
SELECTION_BIAS_NOTE = ("Shadow rows are candidates the desk did not take (gated, rejected or not executed): not a "
                       "random sample of signals; read them as context, the real trades decide.")
SHADOW_EXPIRED_NOTE = "Shadow rows resolved EXPIRED / EXPIRED_UNTRIGGERED count as unfilled (0R) under every policy."
REAL_TRIGGER_DISTANCE_NOTE = ("Trigger distance is not stored for real trades (no trigger price or price at "
                              "placement in the audit record): see the shadow block.")

SKIP_REASONS = eps.SKIP_REASONS


def _policies():
    """Fixed entry-policy registry: name -> settings."""
    out = {"current": {"kind": "stop"}}
    for minutes in (5, 15):
        out[f"closed_candle_confirm_{minutes}m"] = {"kind": "confirm", "minutes": minutes}
    for k, label in ((0.1, "0_1"), (0.25, "0_25"), (0.5, "0_5")):
        out[f"atr_buffer_{label}"] = {"kind": "atr", "k": k}
    for p, label in ((0.25, "0_25"), (0.5, "0_5")):
        out[f"pullback_limit_{label}"] = {"kind": "pullback", "depth": p}
    out["orderflow_veto"] = {"kind": "veto", "threshold": OIB_VETO}
    return out


POLICIES = _policies()
POLICY_NAMES = tuple(POLICIES)


def _num(value, default=None):
    return eps._num(value, default)


def _logs_dir():
    return os.path.join(eft._workspace_dir(), "logs")


# ---------------------------------------------------------------------------------------------------------- inputs

class Signal:
    """One entry signal: a real trade or a shadow candidate."""

    def __init__(self, source, row, symbol, direction, trigger, sl, tp1, tp2, anchor_ms, arm_ms, touch_known,
                 cluster, expired=False):
        self.source, self.row = source, row
        self.symbol, self.direction = symbol, direction
        self.is_long = direction == "LONG"
        self.sign = 1.0 if self.is_long else -1.0
        self.trigger, self.sl, self.tp1, self.tp2 = trigger, sl, tp1, tp2
        self.anchor_ms, self.arm_ms = anchor_ms, arm_ms
        self.touch_known = touch_known  # real trades: the arming bar is the touch bar
        self.cluster = cluster
        self.expired = expired
        self.level = trigger / (1 + LEVEL_OFFSET) if self.is_long else trigger / (1 - LEVEL_OFFSET)
        self.risk0 = abs(trigger - sl)
        self.is_yolo = bool(row.get("is_yolo"))
        self.id = f"{source}:{symbol}:{anchor_ms}"  # made unique per input row by _number


def _number(signals):
    """Unique ids (two rows may share symbol and time)."""
    for i, s in enumerate(signals):
        s.id = f"{s.source}:{i}:{s.symbol}:{s.anchor_ms}"
    return signals


def real_signals(rows):
    out = []
    for r in rows:
        entry_ms = int(_num(r["entry_ts"]))
        out.append(Signal("real", r, str(r["symbol"]).upper(), str(r["direction"]).upper(), float(r["entry_price"]),
                          float(r["sl_price"]), float(r["tp1_price"]), float(r["tp2_price"]), entry_ms,
                          entry_ms // BAR_1M_MS * BAR_1M_MS, True,
                          r.get("dossier_sha256") or f"real:{r.get('symbol')}:{entry_ms}"))
    return _number(out)


def select_shadow(rows):
    """(signals sorted by registration, skipped {reason: count}) of resolved shadow rows."""
    skipped, out = {}, []

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for r in rows:
        direction = str(r.get("direction") or "").upper()
        levels = [_num(r.get(k)) for k in ("trigger_price", "sl_price", "tp1_price", "tp2_price")]
        reg_s = _num(r.get("registered_at_ts"))
        if str(r.get("status") or "").upper() != "RESOLVED":
            skip("not_closed")
        elif direction not in ("LONG", "SHORT") or reg_s is None or reg_s <= 0 or any(
                v is None or v <= 0 for v in levels):
            skip("no_levels")
        else:
            trigger, sl, tp1, tp2 = levels
            ok = (sl < trigger < tp1) if direction == "LONG" else (tp1 < trigger < sl)
            if not ok:
                skip("no_levels")
                continue
            reg_ms = int(reg_s * 1000)
            expired = (str(r.get("outcome") or "").upper() in EXPIRED_OUTCOMES
                       or str(r.get("classification") or "").upper() in EXPIRED_OUTCOMES)
            symbol = str(r.get("symbol") or "").upper()
            out.append(Signal("shadow", r, symbol, direction, trigger, sl, tp1, tp2, reg_ms,
                              trade_excursion.first_post_entry_bar_ms(reg_s), False,
                              r.get("dossier_sha256") or r.get("id") or f"shadow:{symbol}:{reg_ms}", expired=expired))
    out.sort(key=lambda s: (s.anchor_ms, s.symbol))
    return _number(out), skipped


# ----------------------------------------------------------------------------------------------------- order flow

def closed_15m_before(k15m, t_ms, count=eps.WARMUP_15M_BARS):
    """The last `count` 15m bars closed at t_ms (open + 15m <= t_ms)."""
    closed = [k for k in k15m if int(k[0]) + BAR_15M_MS <= t_ms]
    return closed[-count:]


def atr_15m(k15m, t_ms):
    """ATR_15m (dem.calculate_atr, period 14) of the 15m bars closed at t_ms; None without bars."""
    closed = closed_15m_before(k15m, t_ms)
    if not closed:
        return None
    return dem.calculate_atr([float(k[2]) for k in closed], [float(k[3]) for k in closed],
                             [float(k[4]) for k in closed], period=14)


def kline_oib(k):
    """(2 x taker-buy base volume - volume) / volume of a raw kline (index 9 and 5); None when index 9 is absent or
    the volume is not positive."""
    if k is None or len(k) <= 9 or k[9] is None:
        return None
    vol, buy = _num(k[5]), _num(k[9])
    if vol is None or buy is None or vol <= 0:
        return None
    return (2 * buy - vol) / vol


# ------------------------------------------------------------------------------------------------------- fill rules

def _touches_sl(sig, bar):
    return float(bar[3]) <= sig.sl if sig.is_long else float(bar[2]) >= sig.sl


def _reaches(sig, bar, price):
    return float(bar[2]) >= price if sig.is_long else float(bar[3]) <= price


def _stop_fill(sig, bar, price):
    o = float(bar[1])
    return max(price, o) if sig.is_long else min(price, o)


def find_touch(sig, bars):
    """Index of the touch bar of the current trigger: real trades, the entry minute bar; shadow rows, the first bar
    reaching the trigger within ENTRY_WAIT_MINUTES of the arming bar. None when not found."""
    if sig.touch_known:
        for i, k in enumerate(bars):
            if int(k[0]) == sig.arm_ms:
                return i
        return None
    limit = sig.arm_ms + ENTRY_WAIT_MINUTES * BAR_1M_MS
    for i, k in enumerate(bars):
        if int(k[0]) >= limit:
            return None
        if _reaches(sig, k, sig.trigger):
            return i
    return None


def plan_fill(sig, policy, bars, touch, k15m, confirm_minutes, pullback_minutes):
    """{"status": "filled", "idx", "price", "maker"} | {"status": "missed" | "vetoed" | "unavailable", "reason"}."""
    kind = policy["kind"]
    if kind == "atr":
        atr = atr_15m(k15m, sig.anchor_ms)
        if not atr:
            return {"status": "unavailable", "reason": "no_atr"}
        alt = sig.level + sig.sign * policy["k"] * atr
        start = next((i for i, k in enumerate(bars) if int(k[0]) >= sig.arm_ms), None)
        if start is None:
            return {"status": "missed", "reason": "no_touch"}
        limit = sig.arm_ms + ENTRY_WAIT_MINUTES * BAR_1M_MS
        for i in range(start, len(bars)):
            if int(bars[i][0]) >= limit:
                break
            if _reaches(sig, bars[i], alt):
                return {"status": "filled", "idx": i, "price": _stop_fill(sig, bars[i], alt), "maker": False,
                        "trigger": alt}
        return {"status": "missed", "reason": "no_touch"}
    if touch is None:
        return {"status": "missed", "reason": "no_touch"}
    touch_open = int(bars[touch][0])
    if kind == "stop":
        return {"status": "filled", "idx": touch, "price": _stop_fill(sig, bars[touch], sig.trigger), "maker": False}
    if kind == "veto":
        last = closed_15m_before(k15m, sig.anchor_ms if sig.touch_known else touch_open, count=1)
        oib = kline_oib(last[0]) if last else None
        if oib is None:
            return {"status": "unavailable", "reason": "unavailable_no_taker_volume"}
        if (oib <= -policy["threshold"]) if sig.is_long else (oib >= policy["threshold"]):
            return {"status": "vetoed", "reason": "orderflow_opposes", "oib": round(oib, 6)}
        return {"status": "filled", "idx": touch, "price": _stop_fill(sig, bars[touch], sig.trigger), "maker": False,
                "oib": round(oib, 6)}
    if kind == "confirm":
        span = policy["minutes"] * BAR_1M_MS
        limit = touch_open + confirm_minutes * BAR_1M_MS
        for i in range(touch, len(bars)):
            k = bars[i]
            if int(k[0]) >= limit:
                return {"status": "missed", "reason": "timeout"}
            if _touches_sl(sig, k):
                return {"status": "missed", "reason": "sl_before_fill"}
            closes_bucket = (int(k[0]) + BAR_1M_MS) % span == 0
            close = float(k[4])
            if closes_bucket and ((close > sig.trigger) if sig.is_long else (close < sig.trigger)):
                if i + 1 >= len(bars):
                    return {"status": "unavailable", "reason": "no_bars_after_fill"}
                return {"status": "filled", "idx": i + 1, "price": float(bars[i + 1][1]), "maker": False}
        return {"status": "missed", "reason": "timeout"}
    if kind == "pullback":
        limit_price = sig.trigger - sig.sign * policy["depth"] * sig.risk0
        end = touch_open + BAR_1M_MS + pullback_minutes * BAR_1M_MS
        for i in range(touch + 1, len(bars)):
            k = bars[i]
            if int(k[0]) >= end:
                break
            if (float(k[3]) < limit_price) if sig.is_long else (float(k[2]) > limit_price):
                return {"status": "filled", "idx": i, "price": limit_price, "maker": True}
        return {"status": "missed", "reason": "cancelled"}
    raise ValueError(f"unknown policy kind {kind!r}")


# ----------------------------------------------------------------------------------------------------------- book

def rr_tp2(sig, price):
    risk = abs(price - sig.sl)
    return abs(sig.tp2 - price) / risk if risk > 0 else None


def immediate_stop(sig, bars_after, fill_price, risk):
    """True when the initial SL is touched while the favorable excursion of the earlier bars (after the fill bar)
    stayed < IMMEDIATE_MFE_R."""
    best = 0.0
    for k in bars_after:
        if _touches_sl(sig, k):
            return best < IMMEDIATE_MFE_R
        ext = float(k[2]) if sig.is_long else float(k[3])
        best = max(best, sig.sign * (ext - fill_price) / risk)
    return False


def book(sig, fill, bars, k15m, filters, env, taker_fee, maker_fee, trail_cadence, horizon_ms, now_ms):
    """Outcome of a planned fill: dict(status "filled", r, gross_r, fees_r, exit_kind, capped, immediate_stop, ...)
    or a missed / unavailable dict."""
    price = float(fill["price"])
    if (price <= sig.sl) if sig.is_long else (price >= sig.sl):
        return {"status": "missed", "reason": "fill_beyond_sl"}
    if (price >= sig.tp1) if sig.is_long else (price <= sig.tp1):
        return {"status": "missed", "reason": "fill_beyond_tp1"}
    risk = abs(price - sig.sl)
    fill_bar = bars[fill["idx"]]
    fill_open = int(fill_bar[0])
    fill_ms = max(fill_open, sig.anchor_ms) if sig.touch_known and fill_open == sig.arm_ms else fill_open
    entry_rate = maker_fee if fill["maker"] else taker_fee
    base = {"status": "filled", "fill_price": price, "fill_ms": fill_ms, "risk": risk,
            "liquidity": "maker" if fill["maker"] else "taker",
            "rr_shift": round(rr_tp2(sig, price) - rr_tp2(sig, sig.trigger), 6)}
    if _touches_sl(sig, fill_bar):  # fill bar worst case: immediate SL loss, no TP credited
        fees = (entry_rate * price + taker_fee * sig.sl) / risk
        return dict(base, r=round(-1.0 - fees, 6), gross_r=-1.0, fees_r=round(fees, 6), exit_kind="sl", capped=False,
                    immediate_stop=True)
    first = trade_excursion.first_post_entry_bar_ms(fill_ms / 1000.0)
    after = [k for k in bars if int(k[0]) >= first]
    row = {"symbol": sig.symbol, "direction": sig.direction, "entry_ts": fill_ms, "entry_price": price,
           "entry_vwap": price, "initial_risk": risk, "sl_price": sig.sl, "tp1_price": sig.tp1, "tp2_price": sig.tp2,
           "is_yolo": sig.is_yolo}
    path = eps.TradePath(row, after, k15m, horizon_ms, now_ms)
    if not path.bars:
        return {"status": "unavailable", "reason": "no_bars_after_fill"}
    res = eps.replay(row, path, eps.build_policies()["current"], filters, env, taker_fee, maker_fee,
                     trail_cadence=trail_cadence)
    r, fees = res["r"], res["fees_r"]
    if fill["maker"]:  # replay books a taker entry fee
        corr = (taker_fee - maker_fee) * price / risk
        r, fees = r + corr, fees - corr
    return dict(base, r=round(r, 6), gross_r=res["gross_r"], fees_r=round(fees, 6), exit_kind=res["exit_kind"],
                capped=res["capped"],
                immediate_stop=res["exit_kind"] == "sl" and immediate_stop(sig, path.bars, price, risk))


def replay_actual(sig, bars, k15m, filters, env, taker_fee, maker_fee, trail_cadence, horizon_ms, now_ms):
    """current_real: the real trade replayed from its actual fill like exit_policy_sim (entry_vwap, the row's sized
    initial_risk, from the first full 1m bar after entry_ts)."""
    row = sig.row
    entry = eps.entry_basis(row)
    risk = float(row["initial_risk"])
    first = trade_excursion.first_post_entry_bar_ms(sig.anchor_ms / 1000.0)
    path = eps.TradePath(row, [k for k in bars if int(k[0]) >= first], k15m, horizon_ms, now_ms)
    if not path.bars:
        return None
    res = eps.replay(row, path, eps.build_policies()["current"], filters, env, taker_fee, maker_fee,
                     trail_cadence=trail_cadence)
    return {"status": "filled", "fill_price": entry, "fill_ms": sig.anchor_ms, "risk": risk, "r": res["r"],
            "gross_r": res["gross_r"], "fees_r": res["fees_r"], "exit_kind": res["exit_kind"],
            "capped": res["capped"],
            "immediate_stop": res["exit_kind"] == "sl" and immediate_stop(sig, path.bars, entry, risk),
            "realized_r_net": _num(row.get("realized_r_net"))}


# --------------------------------------------------------------------------------------------------------- metrics

def bootstrap_mean_ci(items, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED):
    """items: [(cluster, value)]. Mean with a 95% percentile CI from a cluster bootstrap (whole clusters resampled
    with replacement, random.Random(seed)): deterministic for the same input order, seed and resamples."""
    clusters = {}
    for c, v in items:
        clusters.setdefault(c, []).append(v)
    groups = [(sum(v), len(v)) for v in clusters.values()]
    n = len(items)
    out = {"n": n, "n_clusters": len(groups), "mean_delta_r": None, "ci95_low": None, "ci95_high": None,
           "insufficient_sample": n < MIN_SAMPLE, "resamples": resamples, "seed": seed}
    if not n:
        return out
    out["mean_delta_r"] = round(sum(v for _c, v in items) / n, 6)
    rng = random.Random(seed)
    k = len(groups)
    means = []
    for _ in range(resamples):
        total = count = 0
        for _ in range(k):
            s, c = groups[rng.randrange(k)]
            total += s
            count += c
        means.append(total / count)
    means.sort()
    out["ci95_low"] = round(means[int(math.floor(0.025 * (resamples - 1)))], 6)
    out["ci95_high"] = round(means[int(math.ceil(0.975 * (resamples - 1)))], 6)
    return out


def _r_signal(res):
    return res["r"] if res["status"] == "filled" else 0.0


def policy_block(name, results, baseline):
    """results / baseline: {signal id: result} of this policy and of current over the same signals. A signal is
    eligible when both this policy and current could be evaluated on it (unavailable rows are counted apart)."""
    eligible = {sid: x for sid, x in results.items()
                if x["status"] != "unavailable" and baseline[sid]["status"] != "unavailable"}
    filled = [x for x in eligible.values() if x["status"] == "filled"]
    m = eps.policy_metrics([dict(x, mfe_r=0.0, entry_ts=x["fill_ms"]) for x in filled])
    m.pop("capture_ratio", None)
    n = len(eligible)
    missed, unavailable = {}, {}
    for x in results.values():
        if x["status"] == "missed":
            missed[x["reason"]] = missed.get(x["reason"], 0) + 1
        elif x["status"] == "unavailable":
            unavailable[x["reason"]] = unavailable.get(x["reason"], 0) + 1
    imm = sum(1 for x in filled if x["immediate_stop"])
    imm_cur = sum(1 for sid in eligible if baseline[sid]["status"] == "filled" and baseline[sid]["immediate_stop"])
    deltas = [(eligible[sid]["cluster"], _r_signal(eligible[sid]) - _r_signal(baseline[sid])) for sid in eligible]
    shifts = [x["rr_shift"] for x in filled if x.get("rr_shift") is not None]
    total = sum(x["r"] for x in filled)
    m.update(
        n_eligible=n, n_filled=len(filled), fill_rate=round(len(filled) / n, 6) if n else None,
        expectancy_r_filled=m.pop("expectancy_r"), expectancy_r_per_signal=round(total / n, 6) if n else None,
        missed=missed, vetoed=sum(1 for x in results.values() if x["status"] == "vetoed"), unavailable=unavailable,
        immediate_stop_outs=imm, immediate_stop_outs_delta_vs_current=imm - imm_cur,
        mean_rr_shift=round(sum(shifts) / len(shifts), 6) if shifts else None,
        mean_fees_r=round(sum(x["fees_r"] for x in filled) / len(filled), 6) if filled else None,
        insufficient_sample=n < MIN_SAMPLE)
    m["paired_delta_vs_current"] = bootstrap_mean_ci(deltas) if name != "current" else None
    return m


def _trade_view(sig, x):
    keys = ("status", "reason", "trigger", "fill_price", "r", "exit_kind", "immediate_stop", "rr_shift", "liquidity",
            "oib")
    return dict({"symbol": sig.symbol, "direction": sig.direction, "anchor_ms": sig.anchor_ms},
                **{k: x[k] for k in keys if k in x})


def _bucket(pct):
    return next(label for label, lo, hi in DISTANCE_BUCKETS if (lo is None or pct >= lo) and (hi is None or pct < hi))


def trigger_distance_block(signals, per_policy, names):
    """Shadow only: signed distance trigger vs current_price_at_eval in the trade direction (percent; a crossed
    trigger is negative and falls in the first bucket)."""
    groups = {label: [] for label, _lo, _hi in DISTANCE_BUCKETS}
    unavailable = 0
    for s in signals:
        price = _num(s.row.get("current_price_at_eval"))
        if price is None or price <= 0:
            unavailable += 1
            continue
        groups[_bucket(s.sign * (s.trigger - price) / price * 100)].append(s.id)
    out = []
    for label, ids in groups.items():
        entry = {"bucket": label, "n": len(ids), "policies": {}}
        for name in names:
            res = [per_policy[name][sid] for sid in ids if per_policy[name][sid]["status"] != "unavailable"]
            filled = [x["r"] for x in res if x["status"] == "filled"]
            entry["policies"][name] = {
                "n_eligible": len(res), "n_filled": len(filled),
                "expectancy_r_per_signal": round(sum(filled) / len(res), 6) if res else None,
                "expectancy_r_filled": round(sum(filled) / len(filled), 6) if filled else None}
        out.append(entry)
    return {"unit": "percent of current_price_at_eval, in the trade direction", "buckets": out,
            "unavailable": unavailable}


# ------------------------------------------------------------------------------------------------------------ run

def _replay_signals(signals, names, env, filters_by_symbol, taker_fee, maker_fee, trail_cadence, horizon_ms, now_ms,
                    confirm_minutes, pullback_minutes, skipped, warnings):
    """({policy: {signal id: result}}, simulated signals, current_real {id: result})."""
    per_policy = {n: {} for n in names}
    simulated, actual = [], {}
    wait_ms = (ENTRY_WAIT_MINUTES + max(confirm_minutes, pullback_minutes) + 2) * BAR_1M_MS

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for sig in signals:
        if sig.expired:
            simulated.append(sig)
            for n in names:
                per_policy[n][sig.id] = {"status": "missed", "reason": "expired", "cluster": sig.cluster}
            continue
        filters = (filters_by_symbol or {}).get(sig.symbol)
        if not filters:
            skip("filters_error")
            continue
        end_ms = sig.arm_ms + wait_ms + horizon_ms
        try:
            bars = eps.fetch_range(sig.symbol, "1m", sig.arm_ms, end_ms, env)
            k15m = eps.fetch_range(sig.symbol, "15m", sig.arm_ms - (eps.WARMUP_15M_BARS + 1) * BAR_15M_MS, end_ms,
                                   env)
        except Exception as e:
            skip("klines_error")
            warnings.append(f"{sig.symbol} {sig.anchor_ms}: klines unavailable ({type(e).__name__})")
            continue
        bars = sorted((k for k in bars if int(k[0]) + BAR_1M_MS <= now_ms), key=lambda k: int(k[0]))
        k15m = sorted((k for k in k15m if int(k[0]) + BAR_15M_MS <= now_ms), key=lambda k: int(k[0]))
        if not bars:
            skip("klines_error")
            continue
        if len(closed_15m_before(k15m, sig.anchor_ms)) < eps.MIN_WARMUP_BARS:
            skip("warmup_short")
            continue
        touch = find_touch(sig, bars)
        if sig.touch_known and touch is None:
            skip("klines_error")
            continue
        simulated.append(sig)
        for n in names:
            fill = plan_fill(sig, POLICIES[n], bars, touch, k15m, confirm_minutes, pullback_minutes)
            res = (book(sig, fill, bars, k15m, filters, env, taker_fee, maker_fee, trail_cadence, horizon_ms, now_ms)
                   if fill["status"] == "filled" else dict(fill))
            for key in ("oib", "trigger"):
                if key in fill:
                    res.setdefault(key, fill[key])
            res["cluster"] = sig.cluster
            per_policy[n][sig.id] = res
        if sig.source == "real":
            act = replay_actual(sig, bars, k15m, filters, env, taker_fee, maker_fee, trail_cadence, horizon_ms, now_ms)
            if act is not None:
                actual[sig.id] = dict(act, cluster=sig.cluster)
    return per_policy, simulated, actual


def _block(signals, per_policy, names, skipped):
    by_id = {s.id: s for s in signals}
    policies = {}
    for n in names:
        res = {sid: x for sid, x in per_policy[n].items() if sid in by_id}
        policies[n] = policy_block(n, res, {sid: per_policy["current"][sid] for sid in res})
        policies[n]["trades"] = [_trade_view(by_id[sid], x) for sid, x in res.items()]
    return {"n_signals": len(signals), "insufficient_sample": len(signals) < MIN_SAMPLE, "skipped": skipped,
            "policies": policies}


def _model_vs_real(signals, per_policy, actual):
    rows, r_diffs, fill_diffs, realized = [], [], [], []
    for s in signals:
        model, act = per_policy["current"].get(s.id), actual.get(s.id)
        if not act or not model or model["status"] != "filled":
            continue
        fd = s.sign * (model["fill_price"] - act["fill_price"]) / float(s.row["initial_risk"])
        rd = model["r"] - act["r"]
        fill_diffs.append(fd)
        r_diffs.append(rd)
        if act["realized_r_net"] is not None:
            realized.append(act["realized_r_net"])
        rows.append({"symbol": s.symbol, "anchor_ms": s.anchor_ms, "model_fill": model["fill_price"],
                     "real_fill": act["fill_price"], "fill_diff_r": round(fd, 6), "model_r": model["r"],
                     "real_r": act["r"], "r_diff": round(rd, 6), "realized_r_net": act["realized_r_net"]})

    def mean(v):
        return round(sum(v) / len(v), 6) if v else None

    return {"compared": len(rows), "mean_fill_diff_r": mean(fill_diffs), "mean_r_diff": mean(r_diffs),
            "mean_realized_r_net": mean(realized), "trades": rows,
            "note": "model = current (STOP_MARKET at the entry_price proxy, R = |fill - SL|); real = the actual fill "
                    "(entry_vwap, sized initial_risk) replayed with the same exit; fill_diff_r > 0 = model fill worse"}


def simulate(outcome_rows, shadow_rows, env, policy_names, horizon_hours, taker_fee, maker_fee, now_ms=None, *,
             trail_cadence="1m", exact_entry_only=False, confirm_minutes=DEFAULT_CONFIRM_MINUTES,
             pullback_minutes=DEFAULT_PULLBACK_MINUTES, malformed=0, shadow_malformed=0):
    started = time.monotonic()
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    horizon_ms = int(horizon_hours * 3600 * 1000)
    names = ["current"] + [n for n in policy_names if n != "current"]
    rows, real_skipped = eps.select_rows(outcome_rows, env, exact_entry_only=exact_entry_only)
    if malformed:
        real_skipped["malformed"] = int(malformed)
    real = real_signals(rows)
    shadow, shadow_skipped = select_shadow(shadow_rows)
    if shadow_malformed:
        shadow_skipped["malformed"] = int(shadow_malformed)
    warnings = []

    filters_by_symbol = {}
    if real or any(not s.expired for s in shadow):
        try:
            filters_by_symbol = eps.parse_filters(eps.fetch_exchange_info(env))
        except Exception as e:
            warnings.append(f"exchangeInfo unavailable: {type(e).__name__}: {e}"[:200])

    common = (names, env, filters_by_symbol, taker_fee, maker_fee, trail_cadence, horizon_ms, now_ms,
              confirm_minutes, pullback_minutes)
    real_pp, real_sim, actual = _replay_signals(real, *common, real_skipped, warnings)
    shadow_pp, shadow_sim, _ = _replay_signals(shadow, *common, shadow_skipped, warnings)

    real_block = _block(real_sim, real_pp, names, real_skipped)
    act_rows = list(actual.values())
    cur_real = eps.policy_metrics([dict(x, mfe_r=0.0, entry_ts=x["fill_ms"]) for x in act_rows])
    cur_real.pop("capture_ratio", None)
    cur_real["immediate_stop_outs"] = sum(1 for x in act_rows if x["immediate_stop"])
    real_block["current_real"] = cur_real
    real_block["model_vs_real"] = _model_vs_real(real_sim, real_pp, actual)
    real_block["trigger_distance"] = {"available": False, "note": REAL_TRIGGER_DISTANCE_NOTE}
    n_approx = sum(1 for s in real_sim if eps.entry_ts_approx(s.row))
    real_block["entry_ts_approx"] = n_approx

    shadow_block = _block(shadow_sim, shadow_pp, names, shadow_skipped)
    shadow_block["expired_unfilled"] = sum(1 for s in shadow_sim if s.expired)
    shadow_block["trigger_distance"] = trigger_distance_block(shadow_sim, shadow_pp, names)
    shadow_block["selection_bias_note"] = SELECTION_BIAS_NOTE

    if n_approx:
        warnings.append(f"{n_approx} real trade(s) with approximate entry_ts (audit timestamp - 120 s): their touch "
                        f"minute is a guess (--exact-entry-only skips them)")
    replayed = sum(1 for s in real_sim + shadow_sim if not s.expired)
    return {"ok": bool(replayed), "env": env, "counterfactual": True, "in_sample": True,
            "horizon_hours": horizon_hours, "trail_cadence": trail_cadence, "exact_entry_only": exact_entry_only,
            "fees": {"taker": taker_fee, "maker": maker_fee, "entry": "taker (pullback_limit: maker)"},
            "params": {"confirm_minutes": confirm_minutes, "pullback_minutes": pullback_minutes,
                       "entry_wait_minutes": ENTRY_WAIT_MINUTES, "oib_veto": OIB_VETO,
                       "immediate_mfe_r": IMMEDIATE_MFE_R, "min_sample": MIN_SAMPLE,
                       "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED}},
            "policies": names, "n_replayed": replayed, "real": real_block, "shadow": shadow_block,
            "notes": [COUNTERFACTUAL_NOTE, DECISION_NOTE, ANCHOR_NOTE, OIB_NOTE, RR_NOTE, SHADOW_EXPIRED_NOTE],
            "warnings": warnings, "elapsed_seconds": round(time.monotonic() - started, 3)}


def _print_block(title, block, names):
    print(f"  {title}: {block['n_signals']} signal(s){' (insufficient_sample)' if block['insufficient_sample'] else ''}"
          f", skipped {block['skipped'] or 'none'}")
    print(f"    {'policy':<27}{'elig':>5}{'fill':>5}{'fill %':>8}{'win %':>7}{'exp/fill':>9}{'exp/sig':>9}"
          f"{'PF':>7}{'maxDD':>7}{'imm':>5}{'d imm':>6}{'d R':>8}")
    for name in names:
        m = block["policies"][name]
        pf = "-" if m["profit_factor_r"] is None else f"{m['profit_factor_r']:.2f}"
        delta = (m["paired_delta_vs_current"] or {}).get("mean_delta_r")
        print(f"    {name:<27}{m['n_eligible']:>5}{m['n_filled']:>5}{eps._fmt(m['fill_rate'], pct=True):>8}"
              f"{eps._fmt(m['win_rate'], pct=True):>7}{eps._fmt(m['expectancy_r_filled']):>9}"
              f"{eps._fmt(m['expectancy_r_per_signal']):>9}{pf:>7}{m['max_drawdown_r']:>7.2f}"
              f"{m['immediate_stop_outs']:>5}{m['immediate_stop_outs_delta_vs_current']:>+6d}{eps._fmt(delta):>8}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline counterfactual entry-policy simulator (read-only)")
    parser.add_argument("--env", choices=["prod", "testnet"], default=None, help="Defaults to resolve_env()")
    parser.add_argument("--outcomes", default=None, help="Real trades JSONL (default logs/trade_outcomes.jsonl)")
    parser.add_argument("--shadow", default=None, help="Resolved shadow rows JSONL (default logs/shadow_resolved.jsonl)")
    parser.add_argument("--policies", default=None, help=f"Comma-separated subset of: {', '.join(POLICY_NAMES)} "
                                                         f"(current is always included)")
    parser.add_argument("--horizon-hours", type=float, default=eps.DEFAULT_HORIZON_HOURS, dest="horizon_hours")
    parser.add_argument("--taker-fee", type=float, default=eps.DEFAULT_TAKER_FEE, dest="taker_fee")
    parser.add_argument("--maker-fee", type=float, default=eps.DEFAULT_MAKER_FEE, dest="maker_fee")
    parser.add_argument("--trail-cadence", choices=eps.TRAIL_CADENCES, default="1m", dest="trail_cadence")
    parser.add_argument("--confirm-minutes", type=int, default=DEFAULT_CONFIRM_MINUTES, dest="confirm_minutes",
                        help="closed_candle_confirm: give up this many minutes after the touch")
    parser.add_argument("--pullback-minutes", type=int, default=DEFAULT_PULLBACK_MINUTES, dest="pullback_minutes",
                        help="pullback_limit: cancel the limit this many minutes after the touch bar")
    parser.add_argument("--exact-entry-only", action="store_true", dest="exact_entry_only",
                        help="Skip real rows whose entry_ts is approximate (no entry fill matched)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print the result as JSON")
    parser.add_argument("--out", default=None, help="Output JSON (default logs/entry_policy_sim.json)")
    args = parser.parse_args(argv)

    if args.policies:
        names = [n.strip() for n in args.policies.split(",") if n.strip()]
        unknown = [n for n in names if n not in POLICY_NAMES]
        if unknown or not names:
            parser.print_usage(sys.stderr)
            print(f"entry_policy_sim: unknown policies {unknown} (valid: {', '.join(POLICY_NAMES)})", file=sys.stderr)
            return 2
        names = list(dict.fromkeys(names))
    else:
        names = list(POLICY_NAMES)
    if (not args.horizon_hours > 0 or args.taker_fee < 0 or args.maker_fee < 0 or args.confirm_minutes <= 0
            or args.pullback_minutes <= 0):
        print("entry_policy_sim: --horizon-hours, --confirm-minutes and --pullback-minutes must be > 0 and fees >= 0",
              file=sys.stderr)
        return 2
    try:
        env = resolve_env(args.env)
    except ValueError as e:
        print(f"entry_policy_sim: invalid environment: {e}", file=sys.stderr)
        return 2
    out_path = args.out or os.path.join(_logs_dir(), "entry_policy_sim.json")
    if not path_inside_dir(out_path, _logs_dir()):  # before any read or request
        print(f"entry_policy_sim: --out must be a file inside {_logs_dir()} (got {out_path!r})", file=sys.stderr)
        return 2

    rows, malformed = eps._read_jsonl(args.outcomes or os.path.join(_logs_dir(), "trade_outcomes.jsonl"))
    shadow, shadow_malformed = eps._read_jsonl(args.shadow or os.path.join(_logs_dir(), "shadow_resolved.jsonl"))
    result = simulate(rows, shadow, env, names, args.horizon_hours, args.taker_fee, args.maker_fee,
                      trail_cadence=args.trail_cadence, exact_entry_only=args.exact_entry_only,
                      confirm_minutes=args.confirm_minutes, pullback_minutes=args.pullback_minutes,
                      malformed=malformed, shadow_malformed=shadow_malformed)
    eps._write_json_atomic(out_path, result)

    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        print(f"entry_policy_sim {env} (counterfactual, in-sample): horizon {args.horizon_hours:g}h -> {out_path}")
        _print_block("REAL trades (decide)", result["real"], result["policies"])
        mvr = result["real"]["model_vs_real"]
        if mvr["compared"]:
            print(f"    current_real: exp {eps._fmt(result['real']['current_real']['expectancy_r'])}R over "
                  f"{result['real']['current_real']['n']}; model vs real fill {eps._fmt(mvr['mean_fill_diff_r'])}R, "
                  f"R diff {eps._fmt(mvr['mean_r_diff'])}R over {mvr['compared']}")
        _print_block("SHADOW candidates (context)", result["shadow"], result["policies"])
        for w in result["notes"] + [REAL_TRIGGER_DISTANCE_NOTE, SELECTION_BIAS_NOTE] + result["warnings"]:
            print(f"  note: {w}")
    return 0 if result["n_replayed"] else 1


if __name__ == "__main__":
    sys.exit(main())
