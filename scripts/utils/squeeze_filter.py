#!/usr/bin/env python3
"""
squeeze_filter.py - Squeeze-risk flags and the macro rule for altcoin shorts (issue #206).

Shared by the radar (`broad_market_radar.enrich_candidate_microstructure`: SHORT cap at Tier A, LONG crowding flag)
and the screening pipeline (hard macro gate for altcoin SHORTs). Pure functions, stdlib only, no I/O.

- A SHORT carries squeeze risk when the latest OI change is a spike (`oi_z_score >= 2.0`) or shorts already pay
  funding (`<= -0.01` % per 8h, normalized from the symbol's funding interval). Missing or non-numeric micro data
  counts as squeeze risk (fail closed). The radar caps such a SHORT at Tier A (score 64); it is never rejected for
  this alone.
- A LONG is flagged as crowded when `oi_z_score >= 2.0` and funding `>= +0.05` % per 8h (flag only).
- A non-BTC SHORT is allowed only when `allows_alt_shorts` is True and BTC rejects resistance or the alt's climax
  volume is `>= 2.5x` (the radar's exact `alt_short_climax_ok` flag, not the rounded `vol_ratio`). "BTC rejects
  resistance" is a documented proxy (the desk has no other BTC resistance signal): bearish absorption on BTC (upper
  wick of at least 40% with taker buying absorbed by the ask wall) or a falling-on-OI regime (`SHORT_BUILDUP` /
  `LONG_UNWINDING`). Missing or malformed BTC data counts as no rejection; unavailable BTC data (`btc_data_ok`
  False or regime UNKNOWN) rejects every alt SHORT.
"""

import math
import numbers

OI_Z_SQUEEZE = 2.0
FUNDING_SHORT_CROWDED_PCT = -0.01  # % per 8h; a SHORT at or below this pays the longs (crowded short)
FUNDING_LONG_CROWDED_PCT = 0.05  # % per 8h; a LONG at or above this, with an OI spike, is crowded
SQUEEZE_SCORE_CAP = 64  # top of Tier A (A 55-64)
ALT_SHORT_CLIMAX_VOL = 2.5
SQUEEZE_REASON_PREFIX = "⚠️ Squeeze risk:"  # radar reason line; the brief table shows the SQZ marker instead
BTC_SYMBOL = "BTCUSDT"
BTC_FALLING_ON_OI_REGIMES = ("SHORT_BUILDUP", "LONG_UNWINDING")


def _finite(value):
    """The value as a float when it is a finite real number (bool excluded), else None."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):  # numpy scalars register as Real
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _field(obj, key):
    """`key` from a dict or an attribute of a MacroContext-like object; None when absent."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def funding_8h_pct(micro):
    """Funding in % per 8h: `funding_rate_8h_pct` (normalized by the radar from the symbol's funding interval) when
    the key is present, else `funding_rate_pct` (Binance's default interval is 8h). None when not a finite number."""
    if not isinstance(micro, dict):
        return None
    if "funding_rate_8h_pct" in micro:
        return _finite(micro.get("funding_rate_8h_pct"))
    return _finite(micro.get("funding_rate_pct"))


DEFAULT_FUNDING_INTERVAL_H = 8  # Binance default; /fapi/v1/fundingInfo lists only symbols with adjusted parameters


def parse_funding_intervals(payload):
    """{symbol: fundingIntervalHours} from a /fapi/v1/fundingInfo payload; malformed entries are skipped."""
    out = {}
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        sym, hours = item.get("symbol"), item.get("fundingIntervalHours")
        if isinstance(sym, str) and isinstance(hours, int) and not isinstance(hours, bool) and hours > 0:
            out[sym] = hours
    return out


def funding_interval_h(symbol, intervals):
    """The symbol's funding interval in hours from a parse_funding_intervals map, else the 8h default."""
    hours = intervals.get(symbol) if isinstance(intervals, dict) else None
    return hours if isinstance(hours, int) and not isinstance(hours, bool) and hours > 0 else DEFAULT_FUNDING_INTERVAL_H


def normalize_funding_8h(rate_pct, interval_h):
    """Per-interval funding (%) as its 8h equivalent: rate x 8 / interval_h. None when the rate is not finite."""
    rate = _finite(rate_pct)
    if rate is None:
        return None
    return rate * DEFAULT_FUNDING_INTERVAL_H / interval_h


def short_squeeze_reasons(micro):
    """Squeeze reasons for a SHORT: [] when clean, ["micro_unavailable"] when oi_z / funding are missing or not
    finite numbers (fail closed). Funding is compared per 8h (funding_8h_pct)."""
    if not isinstance(micro, dict) or not micro:
        return ["micro_unavailable"]
    oi_z = _finite(micro.get("oi_z_score"))
    funding = funding_8h_pct(micro)
    if oi_z is None or funding is None:
        return ["micro_unavailable"]
    reasons = []
    if oi_z >= OI_Z_SQUEEZE:
        reasons.append(f"oi_z>={OI_Z_SQUEEZE} (oi_z={oi_z:.2f})")
    if funding <= FUNDING_SHORT_CROWDED_PCT:
        reasons.append(f"funding<={FUNDING_SHORT_CROWDED_PCT}% (funding={funding:.4f}%)")
    return reasons


def long_crowding_reasons(micro):
    """Crowding reasons for a LONG (OI spike AND high positive funding). Missing data -> [] (flag only)."""
    if not isinstance(micro, dict):
        return []
    oi_z = _finite(micro.get("oi_z_score"))
    funding = funding_8h_pct(micro)
    if oi_z is None or funding is None or oi_z < OI_Z_SQUEEZE or funding < FUNDING_LONG_CROWDED_PCT:
        return []
    return [f"oi_z>={OI_Z_SQUEEZE} (oi_z={oi_z:.2f})", f"funding>={FUNDING_LONG_CROWDED_PCT}% (funding={funding:.4f}%)"]


def btc_rejects_resistance(macro):
    """True when BTC shows bearish absorption or falls on OI (proxy, see the module docstring); else False."""
    if macro is None:
        return False
    return (_field(macro, "btc_absorption") == "BEARISH_ABSORPTION"
            or _field(macro, "btc_regime") in BTC_FALLING_ON_OI_REGIMES)


def alt_short_macro_reason(symbol, climax_ok, macro, vol_ratio=None):
    """None when the SHORT passes the macro rule for altcoin shorts, else the rejection reason. BTCUSDT is exempt.
    `climax_ok` is the radar's exact (unrounded) `vol_ratio >= 2.5` flag; anything but True counts as False.
    `vol_ratio` (the rounded display value) only goes into the reason text. Reasons: `btc_data_unavailable` (no
    macro, `btc_data_ok` False or regime UNKNOWN), `btc_short_squeeze` (regime SHORT_SQUEEZE), `alt_shorts_not_allowed`
    (`allows_alt_shorts` not True for any other cause), else the climax reason."""
    if symbol == BTC_SYMBOL:
        return None
    if macro is None or _field(macro, "btc_data_ok") is False or _field(macro, "btc_regime") == "UNKNOWN":
        return "btc_data_unavailable"
    if _field(macro, "allows_alt_shorts") is not True:
        return "btc_short_squeeze" if _field(macro, "btc_regime") == "SHORT_SQUEEZE" else "alt_shorts_not_allowed"
    if btc_rejects_resistance(macro) or climax_ok is True:
        return None
    vol = _finite(vol_ratio)
    vol_txt = f"{vol:.2f}" if vol is not None else "MISSING"
    return f"no_btc_rejection_and_climax<{ALT_SHORT_CLIMAX_VOL}x (vol_ratio={vol_txt})"
