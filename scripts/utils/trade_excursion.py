#!/usr/bin/env python3
"""
trade_excursion.py - Maximum favourable / adverse excursion (MFE / MAE) of a live or closed trade (issue #182).

Pure helpers shared by the position guardian (live tracking, scripts/loops/position_guardian_loop.py) and
scripts/trade_outcomes.py (offline reconstruction). The only I/O is fetch_klines_range (public klines, one request),
kept as a single function so tests patch trade_excursion.fetch_klines_range.

Units: entry_ts / now_ts in seconds; kline open/close times, mfe_ts, mae_ts and last_bar_open_ms in milliseconds.
R multiples use the initial risk |entry - planned SL|: mfe_r >= 0, mae_r <= 0, None without a risk. Percent values
are signed like scripts/shadow_tracker.py (mfe_pct >= 0, mae_pct <= 0, percent of entry).
Price source (PRICE_SOURCE): 1m klines are LAST-price bars and the folded mark price is the MARK price, so on a thin
book a mark spike can set the peak / trough.
"""

import json
import urllib.parse
import urllib.request

BAR_MS = 60_000
GUARDIAN_KLINES_LIMIT = 99  # weight 1 per request (Binance: limit in [1, 100) -> weight 1)
KLINES_TIMEOUT_SECONDS = 2  # guardian excursion read: short, so a slow host never stalls the cycle
DEFAULT_HOSTS = {"prod": "https://fapi.binance.com", "testnet": "https://testnet.binancefuture.com"}
# Issue #192: price source of the guardian's MFE / MAE: LAST-price 1m klines folded with the MARK price.
PRICE_SOURCE = "last_1m+mark"

_HOST_CACHE = None  # {env: (base_url, warning)} only while armed by reset_klines_host_cache() (guardian cycle)


def reset_klines_host_cache(enabled=True):
    """Issue #192: arm (empty) the per-env host cache, so klines_base_url resolves each env once until the next reset;
    enabled=False disarms it (every call resolves again, the behaviour for callers that never arm it)."""
    global _HOST_CACHE
    _HOST_CACHE = {} if enabled else None


def resolve_klines_host(target_env):
    """(base_url, warning | None) for target_env: the executor's base-url resolution (BINANCE_FUTURES_BASE_URL or the
    env's default host) when it resolves one, else the env's default host, never the other env's host. A config error
    falls back to the default host with warning "klines_host_fallback: <Type>: <msg>". Cached while armed."""
    from utils.env_resolver import resolve_env
    env = resolve_env(target_env)
    if _HOST_CACHE is not None and env in _HOST_CACHE:
        return _HOST_CACHE[env]
    warning = None
    try:
        import execute_futures_trade as eft
        base = eft.get_client_config(env)[2]
    except Exception as e:
        base = None
        warning = f"klines_host_fallback: {type(e).__name__}: {e}"[:200]
    out = (str(base or DEFAULT_HOSTS[env]).rstrip("/"), warning)
    if _HOST_CACHE is not None:
        _HOST_CACHE[env] = out
    return out


def klines_base_url(target_env):
    """Futures REST host for target_env (resolve_klines_host without the warning)."""
    return resolve_klines_host(target_env)[0]


def klines_host_warnings():
    """Distinct klines_host_fallback warnings of the hosts resolved since the last reset ([] when not armed)."""
    return sorted({w for _, w in (_HOST_CACHE or {}).values() if w})


def fetch_klines_range(symbol, interval, start_ms, limit, target_env, timeout=None):
    """Raw klines of symbol from start_ms (inclusive, ascending, at most `limit`) from the public endpoint of
    target_env's host. Raises on a network error or a non-list response. timeout: seconds, default
    KLINES_TIMEOUT_SECONDS (the guardian path; the offline trade_outcomes CLI passes a longer one)."""
    timeout = KLINES_TIMEOUT_SECONDS if timeout is None else timeout
    qs = urllib.parse.urlencode({"symbol": str(symbol).upper(), "interval": interval, "startTime": int(start_ms),
                                 "limit": int(limit)})
    url = f"{klines_base_url(target_env)}/fapi/v1/klines?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if not isinstance(data, list):
        raise ValueError(f"unexpected klines response: {str(data)[:160]}")
    return data


def floor_minute_ms(ts_s):
    """Open time (ms) of the 1m bar containing ts_s (seconds)."""
    return int(float(ts_s) * 1000) // BAR_MS * BAR_MS


def first_post_entry_bar_ms(entry_ts):
    """Open time (ms) of the first full 1m bar after entry (the fill minute is excluded, like dem's 15m rule)."""
    return floor_minute_ms(entry_ts) + BAR_MS


def next_bar_start_ms(prev, entry_ts):
    """Open time (ms) of the next 1m bar to fetch: max(prev last_bar_open_ms + 1m, first post-entry bar); None
    without entry_ts."""
    if entry_ts is None:
        return None
    start = first_post_entry_bar_ms(entry_ts)
    last_bar = (prev or {}).get("last_bar_open_ms")
    if last_bar is not None:
        start = max(start, int(last_bar) + BAR_MS)
    return start


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def excursion_r(side, entry, risk, high, low):
    """(mfe_r, mae_r) of the price range [low, high] for a LONG / SHORT entered at entry with initial risk `risk`
    (price units). mfe_r >= 0, mae_r <= 0; (None, None) when risk is None / <= 0 or an input is missing."""
    entry, risk, high, low = _num(entry), _num(risk), _num(high), _num(low)
    if risk is None or risk <= 0 or entry is None or high is None or low is None:
        return None, None
    if str(side).upper() == "SHORT":
        mfe, mae = (entry - low) / risk, (entry - high) / risk
    else:
        mfe, mae = (high - entry) / risk, (low - entry) / risk
    return round(max(0.0, mfe), 4), round(min(0.0, mae), 4)


def excursion_pct(side, entry, high, low):
    """(mfe_pct, mae_pct) in percent of entry, signed like shadow_tracker (mae_pct <= 0); (None, None) without entry."""
    entry, high, low = _num(entry), _num(high), _num(low)
    if not entry or entry <= 0 or high is None or low is None:
        return None, None
    if str(side).upper() == "SHORT":
        mfe, mae = (entry - low) / entry * 100, (entry - high) / entry * 100
    else:
        mfe, mae = (high - entry) / entry * 100, (low - entry) / entry * 100
    return round(max(0.0, mfe), 4), round(min(0.0, mae), 4)


def update_excursion(prev, *, side, entry_price, risk, entry_ts, klines, mark_price, now_ts,
                     limit=GUARDIAN_KLINES_LIMIT):
    """New excursion record from prev (None / {} for a new trade) plus this cycle's 1m klines and mark price.

    Bars used: CLOSED (close_time < now) with open_time >= first_post_entry_bar_ms(entry_ts) and open_time >
    prev["last_bar_open_ms"]; no bars when entry_ts is None. The mark price is folded in too (its ts = now).
    peak_price is the best favourable price (LONG: highest high, SHORT: lowest low), trough_price the worst adverse,
    both seeded with entry_price. MFE never decreases and MAE never rises versus prev. partial (sticky) becomes True
    when more than `limit` closed bars separate the next expected bar from now (this cycle's fetch cannot cover them;
    later cycles catch up)."""
    prev = dict(prev or {})
    is_short = str(side).upper() == "SHORT"
    entry = float(entry_price)
    now_ms = int(float(now_ts) * 1000)

    peak = _num(prev.get("peak_price"))
    trough = _num(prev.get("trough_price"))
    peak = entry if peak is None else peak
    trough = entry if trough is None else trough
    mfe_ts, mae_ts = prev.get("mfe_ts"), prev.get("mae_ts")
    last_bar = prev.get("last_bar_open_ms")
    partial = bool(prev.get("partial"))

    def better(a, b):  # a more favourable than b
        return a < b if is_short else a > b

    def worse(a, b):  # a more adverse than b
        return a > b if is_short else a < b

    if entry_ts is not None:
        min_open = first_post_entry_bar_ms(entry_ts)
        last_closed_open = now_ms // BAR_MS * BAR_MS - BAR_MS
        if (last_closed_open - next_bar_start_ms(prev, entry_ts)) // BAR_MS + 1 > int(limit):
            partial = True  # this cycle's fetch cannot reach the latest closed bar
        new_last = last_bar
        for k in klines or []:
            try:
                open_ms, close_ms = int(k[0]), int(k[6])
                high, low = float(k[2]), float(k[3])
            except (TypeError, ValueError, IndexError):
                continue
            if close_ms >= now_ms or open_ms < min_open or (last_bar is not None and open_ms <= int(last_bar)):
                continue
            fav, adv = (low, high) if is_short else (high, low)
            if better(fav, peak):
                peak, mfe_ts = fav, open_ms
            if worse(adv, trough):
                trough, mae_ts = adv, open_ms
            new_last = open_ms if new_last is None else max(int(new_last), open_ms)
        last_bar = new_last

    mark = _num(mark_price)
    if mark is not None and mark > 0:
        if better(mark, peak):
            peak, mfe_ts = mark, now_ms
        if worse(mark, trough):
            trough, mae_ts = mark, now_ms

    high, low = (trough, peak) if is_short else (peak, trough)
    mfe_r, mae_r = excursion_r(side, entry, risk, high, low)
    prev_mfe, prev_mae = _num(prev.get("mfe_r")), _num(prev.get("mae_r"))
    if mfe_r is not None and prev_mfe is not None:
        mfe_r = max(mfe_r, prev_mfe)
    if mae_r is not None and prev_mae is not None:
        mae_r = min(mae_r, prev_mae)
    mfe_pct, mae_pct = excursion_pct(side, entry, high, low)

    out = dict(prev)
    out.update(peak_price=peak, trough_price=trough, mfe_ts=mfe_ts, mae_ts=mae_ts, last_bar_open_ms=last_bar,
               mfe_r=mfe_r, mae_r=mae_r, mfe_pct=mfe_pct, mae_pct=mae_pct, partial=partial)
    return out
