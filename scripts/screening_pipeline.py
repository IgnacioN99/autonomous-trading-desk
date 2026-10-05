#!/usr/bin/env python3
"""
screening_pipeline.py - High-Performance Deterministic Market Intelligence Pipeline.
Implements the 'Lean Evaluator' Pattern:
1. Native Python concurrency (ThreadPoolExecutor) for broad market screening (80+ pairs),
   institutional microstructure (CVD/OI Z-score), cointegrated ADF Stat-Arb, and newsletters.
2. Strict data modeling via Pydantic V2 schemas (zero information loss across agent boundaries).
3. Ultra-low latency structured output (~3.5 seconds) ready for consumption by the Clean-Room Evaluator Agent.
"""

import os
import sys
import json
import time
import threading
from typing import List, Literal, Optional, Tuple
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from pydantic import BaseModel, Field

# Ensure import paths
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broad_market_radar as bmr
import microstructure_engine as me
import quant_risk_engine as qre
import funding_arbitrage as fa
import sync_session_state as sss
import fetch_newsletters as fn
import broad_yolo_scanner as bys
from utils.env_resolver import resolve_env

# Barbell YOLO slot (issue #52): the memecoin scanner runs concurrently under a hard time budget so it can
# never block or break the standard scan (prime_evaluator_brief.py gives the whole pipeline 60 s).
YOLO_SCAN_TIMEOUT_S = 25
YOLO_SCAN_INTERVAL = "15m"
YOLO_SCAN_TOP = 3
YOLO_MAX_CANDIDATES = 2  # token budget of the primed brief
YOLO_FILTER_TEXT = f"climax volume >= {bys.MIN_VOL_RATIO}x or buyer absorption wick >= {bys.MIN_WICK_PCT:.0f}%"
YOLO_INACTIVE_STATUS = f"INACTIVE: Preserving capital. No memecoin exceeds {YOLO_FILTER_TEXT}."
YOLO_DISABLED_STATUS = "DISABLED: yolo_slot_enabled is false in the user profile."
# Executor gates mirrored here (not imported) so the evaluator never sees a YOLO entry that the executor would
# reject when entered at the trigger: friction floor (execute_futures_trade.py GATE 3, TP1 >= 0.35% from the entry)
# and the Barbell YOLO loss cap (GATE 2, loss at SL <= 35% of the isolated margin: SL distance x leverage <= 0.35).
YOLO_MIN_TP1_DISTANCE = 0.0035
YOLO_MAX_LOSS_MARGIN_FRACTION = 0.35
YOLO_SIG_DIGITS = 6  # forwarded floats are rounded to 6 significant digits (token budget)
_last_yolo_future = None  # Future of the latest pipeline run's YOLO scan (CLI exit handling)

# ==========================================
# 1. TYPED PYDANTIC SCHEMAS (DATA CONTRACTS)
# ==========================================

class MacroContext(BaseModel):
    btc_price: float
    btc_regime: str
    btc_regime_desc: str
    btc_absorption: str
    btc_taker_ratio: float
    btc_cvd_30v: float
    btc_oi_z_score: float
    btc_tape_bias: str
    btc_tape_imbalance: float
    allows_alt_shorts: bool
    macro_warning: Optional[str] = None

class CandidateSetup(BaseModel):
    symbol: str
    direction: Literal["LONG", "SHORT"]
    tier: str
    confidence: int
    current_price: float
    trigger_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    rr_ratio: float
    risk_pct: float
    rsi_15m: float
    vol_ratio: float
    lower_wick_pct: float
    upper_wick_pct: float
    cvd_delta: float
    oi_z_score: float
    oib_ratio: Optional[float] = 0.0
    vwap_deviation_pct: Optional[float] = 0.0
    cascade_risk: Optional[str] = "BASELINE"
    regime: str
    absorption: str
    whale_bias: str
    required_margin: float
    step_qty: float
    actual_notional: float
    target_dollar_risk: float
    reasons: List[str]
    sizing_entry_price: Optional[float] = None  # entry the sizing was computed from (trigger, else current price)

class StatArbPair(BaseModel):
    pair: str
    symbol_a: str
    symbol_b: str
    price_a: float
    price_b: float
    correlation: float
    hedge_ratio_beta: float
    hedge_ratio_beta_dynamic_10d: Optional[float] = None
    beta_drift_pct: Optional[float] = None
    pci_r2_mr: Optional[float] = None
    target_unwind_z: Optional[float] = None
    notional_a: float = 20.0
    notional_b: float = 20.0
    margin_a: float = 6.67
    margin_b: float = 6.67
    sample_bars: int = 1000
    adf_pvalue: float
    coint_pvalue: float
    mackinnon_crit_5pct: float = -3.34
    half_life_hours: float
    is_cointegrated: bool
    z_score: float
    action: str
    recommendation: Optional[str] = None
    is_actionable: bool

class YoloCandidate(BaseModel):
    """Compact LONG-only subset of broad_yolo_scanner.build_levels() forwarded to the evaluator."""
    symbol: str
    direction: Literal["LONG"]
    score: float
    price: float
    trigger: float
    sl: float
    risk_pct: float
    tp1: float
    tp2: float
    roe_tp1_pct: float
    roe_tp2_pct: float
    leverage: int
    margin_usdt: float
    max_loss_usdt: float
    rsi: float
    vol_ratio: float
    lower_wick: float

class YoloSlot(BaseModel):
    status: Literal["ACTIVE", "INACTIVE", "DISABLED", "UNAVAILABLE"]
    interval: Optional[str] = None
    candidates: List[YoloCandidate] = Field(default_factory=list)

class MarketScreeningPayload(BaseModel):
    timestamp_utc: str
    pipeline_latency_ms: int
    macro: MacroContext
    portfolio_context: Optional[dict] = None
    top_candidates: List[CandidateSetup]
    actionable_stat_arb: List[StatArbPair]
    top_funding_arbitrage: Optional[List[dict]] = None
    yolo_slot_status: str
    yolo_slot: Optional[YoloSlot] = None
    news_catalysts_summary: List[str]
    untrusted_external_content: bool = True

# ==========================================
# 2. DETERMINISTIC EXECUTION PIPELINE
# ==========================================

def fetch_macro_btc() -> MacroContext:
    """Queries Bitcoin microstructure and tape to validate macro regime."""
    try:
        btc_micro = me.get_symbol_microstructure("BTCUSDT") or {}
        btc_tape = me.get_live_aggtrades_tape("BTCUSDT") or {}
        # Public mainnet ticker: market data is environment-independent (testnet prices are not representative).
        ticker = me.fetch_json(f"{me.BASE_FAPI}/fapi/v1/ticker/price?symbol=BTCUSDT")
        cur_price = float(ticker.get("price", 0)) if isinstance(ticker, dict) else 0.0

        regime = btc_micro.get("regime", "UNKNOWN")
        allows_shorts = regime != "SHORT_SQUEEZE"
        warning = None
        if not allows_shorts:
            warning = "⚠️ MACRO ALERT: Bitcoin in aggressive Short Squeeze. Altcoin Short orders prohibited."

        return MacroContext(
            btc_price=cur_price,
            btc_regime=regime,
            btc_regime_desc=btc_micro.get("regime_desc", "N/A"),
            btc_absorption=btc_micro.get("absorption", "NONE"),
            btc_taker_ratio=btc_micro.get("taker_ratio", 1.0),
            btc_cvd_30v=btc_micro.get("cvd_window_net", 0.0),
            btc_oi_z_score=btc_micro.get("oi_z_score", 0.0),
            btc_tape_bias=btc_tape.get("live_bias", "BALANCED"),
            btc_tape_imbalance=btc_tape.get("imbalance_pct", 0.0),
            allows_alt_shorts=allows_shorts,
            macro_warning=warning
        )
    except Exception as e:
        return MacroContext(
            btc_price=0.0,
            btc_regime="UNKNOWN",
            btc_regime_desc=f"Error querying BTC: {str(e)}",
            btc_absorption="NONE",
            btc_taker_ratio=1.0,
            btc_cvd_30v=0.0,
            btc_oi_z_score=0.0,
            btc_tape_bias="UNKNOWN",
            btc_tape_imbalance=0.0,
            allows_alt_shorts=True
        )

def _profile_standard_sizing():
    """(leverage_standard capped at the desk ceiling, max_margin_ratio) from config/user_profile.json."""
    try:
        import user_profile as up
        prof = up.load_user_profile()
        lev = max(1, min(int(prof.get("leverage_standard", 3)), up.get_leverage_ceiling(prof)))
        return lev, float(prof.get("max_margin_ratio", 0.30))
    except Exception:
        return 3, 0.30

def enrich_and_size_candidate(c: dict, target_env: Optional[str] = None) -> Optional[CandidateSetup]:
    """Calculates volatility parity sizing and encapsulates into Pydantic model."""
    try:
        sym = c["symbol"]
        entry = float(c["price"])
        sl = float(c["sl"])
        direction = c["direction"]
        lev, max_margin_ratio = _profile_standard_sizing()
        # Issue #22: size from the conditional entry (breakout trigger). It is always farther from the SL
        # than the current price, so it is also conservative for MARKET deployments.
        # Single expression for both sizing and trigger_price (a 0/None trigger falls back to the price).
        sizing_entry = float(c.get("trigger") or entry)

        target_env = resolve_env(target_env)

        # Calculate dynamic equity sizing (user profile risk_pct_equity x equity, margin capped at max_margin_ratio)
        sizing = qre.calculate_dynamic_equity_sizing(sym, sizing_entry, sl, risk_pct_equity=None, leverage=lev,
                                                     target_env=target_env, max_margin_ratio=max_margin_ratio)
        if not sizing or "error" in sizing or sizing.get("step_qty", 0.0) <= 0.0:
            print(f"Invalid or non-quantizable sizing for {sym}: {sizing.get('error') if sizing else 'Empty sizing'}", file=sys.stderr)
            return None

        req_margin = sizing["required_margin"]
        step_qty = sizing["step_qty"]
        actual_notional = sizing["actual_notional"]
        risk_dollar = sizing["actual_dollar_risk"]

        micro = c.get("micro") or {}
        tape = me.get_live_aggtrades_tape(sym) or {}

        return CandidateSetup(
            symbol=sym,
            direction=direction,
            tier=c.get("tier", "Tier A"),
            confidence=int(c.get("confidence", 60)),
            current_price=entry,
            trigger_price=sizing_entry,
            sl_price=sl,
            tp1_price=float(c.get("tp1", entry * 1.02)),
            tp2_price=float(c.get("tp2", entry * 1.04)),
            rr_ratio=float(c.get("rr", 3.0)),
            risk_pct=round(abs(sizing_entry - sl) / sizing_entry * 100, 2),
            sizing_entry_price=sizing_entry,
            rsi_15m=float(c.get("rsi_15m", 50)),
            vol_ratio=float(c.get("vol_ratio", 1.0)),
            lower_wick_pct=float(c.get("lower_wick", 0)),
            upper_wick_pct=float(c.get("upper_wick", 0)),
            cvd_delta=float(micro.get("cvd_window_net", 0.0)),
            oi_z_score=float(micro.get("oi_z_score", 0.0)),
            oib_ratio=float(micro.get("oib_ratio", 0.0)),
            vwap_deviation_pct=float(micro.get("vwap_deviation_pct", 0.0)),
            cascade_risk=str(micro.get("cascade_risk", "BASELINE")),
            regime=micro.get("regime", "CONSOLIDATION"),
            absorption=micro.get("absorption", "NONE"),
            whale_bias=tape.get("live_bias", "BALANCED"),
            required_margin=req_margin,
            step_qty=step_qty,
            actual_notional=actual_notional,
            target_dollar_risk=risk_dollar,
            reasons=c.get("reasons", [])
        )
    except Exception:
        return None

# Prompt-injection defense is shared with the newsletter reader (single source of truth).
PROMPT_INJECTION_PATTERNS = fn.PROMPT_INJECTION_PATTERNS
sanitize_untrusted_text = fn.sanitize_untrusted_text

def fetch_news_summary() -> List[str]:
    """Reads and filters news catalysts or recent newsletter mentions deterministically."""
    catalysts = []
    newsletter_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_newsletters.py")
    if os.path.exists(newsletter_script):
        try:
            import subprocess
            res = subprocess.run([sys.executable, newsletter_script, "--limit", "3"], capture_output=True, text=True, timeout=8)
            if res.returncode == 0 and res.stdout:
                try:
                    data = json.loads(res.stdout)
                    emails = data.get("emails", [])
                    for em in emails:
                        sender = em.get("from", "").split("<")[0].strip()
                        subj = em.get("subject", "").strip()
                        date_str = em.get("date", "").split(" +")[0].strip()
                        if subj:
                            clean_subj = sanitize_untrusted_text(subj)
                            clean_sender = sanitize_untrusted_text(sender)
                            catalysts.append(f"<untrusted_newsletter_data>[{clean_sender} | {date_str}] {clean_subj}</untrusted_newsletter_data>")
                except Exception:
                    for line in res.stdout.strip().split("\n"):
                        if line.strip() and not line.startswith("===") and "{" not in line:
                            clean_line = sanitize_untrusted_text(line.strip())
                            catalysts.append(f"<untrusted_newsletter_data>{clean_line}</untrusted_newsletter_data>")
        except Exception:
            pass
    if not catalysts:
        catalysts.append("<untrusted_newsletter_data>Stable macro. No high-impact Federal Reserve or CPI events scheduled in the immediate intraday window.</untrusted_newsletter_data>")
    return catalysts[:5]

def _yolo_slot_enabled() -> bool:
    """Profile gate: the YOLO scan only runs when the user enabled the Barbell slot."""
    import user_profile as up
    return bool(up.load_user_profile().get("yolo_slot_enabled", False))

def _start_yolo_scan(target_env: str) -> Future:
    """Runs broad_yolo_scanner.scan_yolo in a daemon thread. Unlike a `with ThreadPoolExecutor` block (which
    joins its workers on exit), a hung scan cannot hold the pipeline past its budget. The scanner's own
    non-daemon kline workers are cut off at CLI exit by `_run_cli` (hard exit while the scan is running)."""
    fut: Future = Future()

    def _run():
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(bys.scan_yolo(target_env, interval=YOLO_SCAN_INTERVAL, top=YOLO_SCAN_TOP))
        except BaseException as e:  # surfaced to the pipeline as UNAVAILABLE
            fut.set_exception(e)

    threading.Thread(target=_run, name="yolo-scan", daemon=True).start()
    return fut

def _yolo_unavailable(reason: str) -> Tuple[str, YoloSlot]:
    reason = " ".join(str(reason).split())[:160]
    return f"UNAVAILABLE: {reason}. YOLO slot kept empty.", YoloSlot(status="UNAVAILABLE", interval=YOLO_SCAN_INTERVAL)

def _sig(x: float, digits: int = YOLO_SIG_DIGITS) -> float:
    """Plain float rounded to `digits` significant digits."""
    return float(f"{float(x):.{digits}g}")

def _to_yolo_candidate(raw: dict) -> Optional[YoloCandidate]:
    """Plain-typed LONG candidate that passes the Barbell filters and the executor's YOLO gates when entered at its
    trigger (the entry the evaluator uses), else None. risk_pct, ROE and max loss are recomputed from the trigger
    (the scanner computes them from the current price)."""
    try:
        if raw.get("direction") != "LONG":
            return None
        symbol, price, trigger, sl = str(raw["symbol"]), float(raw["price"]), float(raw["trigger"]), float(raw["sl"])
        tp1, tp2 = float(raw["tp1"]), float(raw["tp2"])
        leverage, margin = int(raw["leverage"]), float(raw["margin_usdt"])
        score, rsi = float(raw["score"]), float(raw["rsi"])
        vol_ratio, lower_wick = float(raw["vol_ratio"]), float(raw["lower_wick"])
    except Exception:
        return None
    # Defense in depth: never forward a memecoin that fails both hardened filters (AGENTS.md Barbell rule).
    if not (vol_ratio >= bys.MIN_VOL_RATIO or lower_wick >= bys.MIN_WICK_PCT):
        return None
    # LONG levels must be coherent around the current price and the trigger entry (also drops NaN levels).
    if not (0.0 < sl < price and sl < trigger < tp1 <= tp2 and leverage >= 1 and margin > 0.0):
        return None
    risk_frac = (trigger - sl) / trigger
    if (tp1 - trigger) / trigger < YOLO_MIN_TP1_DISTANCE:
        return None
    if risk_frac * leverage > YOLO_MAX_LOSS_MARGIN_FRACTION:
        return None
    return YoloCandidate(
        symbol=symbol,
        direction="LONG",
        score=_sig(score),
        price=_sig(price),
        trigger=_sig(trigger),
        sl=_sig(sl),
        risk_pct=round(risk_frac * 100, 2),
        tp1=_sig(tp1),
        tp2=_sig(tp2),
        roe_tp1_pct=round((tp1 - trigger) / trigger * 100 * leverage, 1),
        roe_tp2_pct=round((tp2 - trigger) / trigger * 100 * leverage, 1),
        leverage=leverage,
        margin_usdt=round(margin, 2),
        max_loss_usdt=round(margin * leverage * risk_frac, 2),
        rsi=_sig(rsi),
        vol_ratio=_sig(vol_ratio),
        lower_wick=_sig(lower_wick),
    )

def build_yolo_slot(scan: dict) -> Tuple[str, YoloSlot]:
    """Maps a broad_yolo_scanner.scan_yolo() payload to (yolo_slot_status sentence, structured YoloSlot)."""
    interval = str(scan.get("interval") or YOLO_SCAN_INTERVAL)
    slot_status = scan.get("slot_status")
    if slot_status == "CANDIDATE_SLOT_DISABLED":
        return YOLO_DISABLED_STATUS, YoloSlot(status="DISABLED", interval=interval)
    if slot_status == "EMPTY":
        return YOLO_INACTIVE_STATUS, YoloSlot(status="INACTIVE", interval=interval)
    if slot_status != "CANDIDATE":
        return _yolo_unavailable(f"unexpected scanner slot_status {slot_status!r}")

    candidates: List[YoloCandidate] = []
    for raw in scan.get("longs") or []:
        cand = _to_yolo_candidate(raw) if isinstance(raw, dict) else None
        if cand is not None:
            candidates.append(cand)
        if len(candidates) >= YOLO_MAX_CANDIDATES:
            break
    if not candidates:
        return YOLO_INACTIVE_STATUS, YoloSlot(status="INACTIVE", interval=interval)
    return (f"ACTIVE: {len(candidates)} memecoin(s) pass {YOLO_FILTER_TEXT}.",
            YoloSlot(status="ACTIVE", interval=interval, candidates=candidates))

def execute_screening_pipeline(top_pairs_count: int = 80, target_env: Optional[str] = None,
                               include_yolo: bool = True) -> MarketScreeningPayload:
    """
    Executes the full screening pipeline concurrently in Python without any intermediary LLM.
    Returns a validated, structured MarketScreeningPayload object.
    include_yolo=False skips the Barbell YOLO memecoin scan (for callers that only use top_candidates).
    """
    global _last_yolo_future
    _last_yolo_future = None
    t0 = time.time()

    target_env = resolve_env(target_env)

    # Barbell YOLO slot: gated by the profile and started first so it overlaps the standard scan.
    f_yolo: Optional[Future] = None
    yolo_deadline = 0.0
    yolo_result: Optional[Tuple[str, YoloSlot]] = None
    if not include_yolo:
        yolo_result = ("UNAVAILABLE: YOLO scan not requested by this caller. YOLO slot kept empty.",
                       YoloSlot(status="UNAVAILABLE", interval=YOLO_SCAN_INTERVAL))
    else:
        try:
            if _yolo_slot_enabled():
                yolo_deadline = time.time() + YOLO_SCAN_TIMEOUT_S
                f_yolo = _start_yolo_scan(target_env)
                _last_yolo_future = f_yolo
            else:
                yolo_result = (YOLO_DISABLED_STATUS, YoloSlot(status="DISABLED", interval=YOLO_SCAN_INTERVAL))
        except Exception as e:
            yolo_result = _yolo_unavailable(f"user profile unavailable ({type(e).__name__})")

    with ThreadPoolExecutor(max_workers=5) as executor:
        f_macro = executor.submit(fetch_macro_btc)
        f_radar = executor.submit(bmr.scan_all_liquid_pairs, top_pairs_count)
        f_statarb = executor.submit(qre.scan_coingrated_market_pairs)
        f_funding = executor.submit(fa.scan_top_funding_opportunities, 20_000_000, 3)
        f_news = executor.submit(fetch_news_summary)

        macro_data = f_macro.result()
        raw_candidates = f_radar.result()
        raw_statarb = f_statarb.result()
        raw_funding = f_funding.result()
        news_data = f_news.result()

    # Filter and type candidate setups (top 6 balanced)
    parsed_candidates: List[CandidateSetup] = []
    with ThreadPoolExecutor(max_workers=6) as c_exec:
        futures = [c_exec.submit(enrich_and_size_candidate, c, target_env) for c in raw_candidates[:10]]
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                parsed_candidates.append(res)

    # Sync live portfolio state and apply Delta-Neutral guardrail
    portfolio_ctx = None
    try:
        s_state = sss.sync_session_state(target_env=target_env)
        p_exp = s_state.get("portfolio_exposure", {})
        portfolio_ctx = {
            "total_active_positions": p_exp.get("total_active_positions", 0),
            "delta_bias": p_exp.get("delta_bias", "DELTA_BALANCED"),
            "delta_advice": p_exp.get("delta_advice", ""),
            "long_notional_usdt": p_exp.get("long_notional_usdt", 0.0),
            "short_notional_usdt": p_exp.get("short_notional_usdt", 0.0),
            "active_symbols": [p["symbol"] for p in s_state.get("active_positions", [])]
        }
        
        # DELTA-NEUTRAL GUARDRAIL:
        # If live portfolio is already skewed, prioritize the opposing hedging direction
        delta_bias = portfolio_ctx["delta_bias"]
        if delta_bias == "LONG_HEAVY":
            parsed_candidates.sort(key=lambda x: (x.direction == "SHORT", x.tier.startswith("Tier S"), x.confidence), reverse=True)
        elif delta_bias == "SHORT_HEAVY":
            parsed_candidates.sort(key=lambda x: (x.direction == "LONG", x.tier.startswith("Tier S"), x.confidence), reverse=True)
        else:
            parsed_candidates.sort(key=lambda x: (x.tier.startswith("Tier S"), x.confidence), reverse=True)
    except Exception:
        parsed_candidates.sort(key=lambda x: (x.tier.startswith("Tier S"), x.confidence), reverse=True)

    top_candidates = parsed_candidates[:6]

    # Actionable or cointegrated Stat-Arb pairs
    stat_arb_list: List[StatArbPair] = []
    for p in raw_statarb:
        try:
            stat_arb_list.append(StatArbPair(
                pair=p["pair"],
                symbol_a=p["symbol_a"],
                symbol_b=p["symbol_b"],
                price_a=float(p["price_a"]),
                price_b=float(p["price_b"]),
                correlation=float(p["correlation"]),
                hedge_ratio_beta=float(p["hedge_ratio_beta"]),
                hedge_ratio_beta_dynamic_10d=float(p.get("hedge_ratio_beta_dynamic_10d", p["hedge_ratio_beta"])),
                beta_drift_pct=float(p.get("beta_drift_pct", 0.0)),
                pci_r2_mr=float(p.get("pci_r2_mr", 0.0)),
                target_unwind_z=float(p.get("target_unwind_z", 0.5 if float(p["z_score"]) > 0 else -0.5)),
                notional_a=float(p.get("notional_a", 20.0)),
                notional_b=float(p.get("notional_b", 20.0)),
                margin_a=float(p.get("margin_a", 6.67)),
                margin_b=float(p.get("margin_b", 6.67)),
                sample_bars=int(p.get("sample_bars", 1000)),
                adf_pvalue=float(p.get("adf_pvalue", 1.0)),
                coint_pvalue=float(p.get("coint_pvalue", 1.0)),
                mackinnon_crit_5pct=float(p.get("mackinnon_crit_5pct", -3.34)),
                half_life_hours=float(p.get("half_life_hours", 999.0)),
                is_cointegrated=bool(p.get("is_cointegrated", False)),
                z_score=float(p["z_score"]),
                action=p["action"],
                recommendation=p.get("recommendation"),
                is_actionable=bool(p.get("is_actionable", False))
            ))
        except Exception:
            pass

    # Barbell YOLO Slot Status (bounded wait; any failure or timeout leaves the slot empty)
    if f_yolo is not None:
        try:
            yolo_result = build_yolo_slot(f_yolo.result(timeout=max(0.0, yolo_deadline - time.time())))
        except FutureTimeoutError:
            yolo_result = _yolo_unavailable(f"YOLO scan exceeded its {YOLO_SCAN_TIMEOUT_S}s budget")
        except Exception as e:
            yolo_result = _yolo_unavailable(f"YOLO scan failed ({type(e).__name__}: {e})")
    yolo_status, yolo_slot = yolo_result

    t1 = time.time()
    latency_ms = int((t1 - t0) * 1000)

    return MarketScreeningPayload(
        timestamp_utc=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        pipeline_latency_ms=latency_ms,
        macro=macro_data,
        portfolio_context=portfolio_ctx,
        top_candidates=top_candidates,
        actionable_stat_arb=stat_arb_list,
        top_funding_arbitrage=raw_funding,
        yolo_slot_status=yolo_status,
        yolo_slot=yolo_slot,
        news_catalysts_summary=news_data,
        untrusted_external_content=True
    )

def _exit_without_waiting_for_yolo(code: int) -> None:
    """CLI only. If the YOLO scan outlived its budget, the scanner's inner ThreadPoolExecutor workers would be joined
    at interpreter exit and keep this process alive after the payload was emitted (prime_evaluator_brief.py waits
    for the subprocess with a 60 s timeout and would then drop the whole payload). Flush and hard-exit instead."""
    fut = _last_yolo_future
    if fut is None or fut.done():
        return
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)

def main(argv: Optional[list] = None) -> int:
    import argparse
    import contextlib
    parser = argparse.ArgumentParser(description="Deterministic Market Intelligence Pipeline")
    parser.add_argument("--json", action="store_true", help="Print payload in strict JSON format")
    parser.add_argument("--env", default=None, help="Target execution environment (prod/testnet)")
    args = parser.parse_args(argv)

    try:
        env = resolve_env(args.env)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    real_stdout = sys.stdout
    try:
        # Keep stdout pure JSON with --json: library diagnostics go to stderr.
        with contextlib.redirect_stdout(sys.stderr if args.json else real_stdout):
            payload = execute_screening_pipeline(target_env=env)
    except Exception as e:
        if args.json:
            real_stdout.write(json.dumps({"status": "error", "command": "screening", "env": env,
                                          "error": f"{type(e).__name__}: {e}"}, indent=2) + "\n")
        else:
            print(f"Screening pipeline failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.json:
        real_stdout.write(payload.model_dump_json(indent=2) + "\n")
    else:
        print(f"⚡ PIPELINE COMPLETED IN {payload.pipeline_latency_ms} ms ({payload.timestamp_utc})")
        print(f"• Macro BTC: {payload.macro.btc_regime} | Price: ${payload.macro.btc_price:,.1f} | Allows Shorts: {payload.macro.allows_alt_shorts}")
        print(f"• Top Qualified Setups: {len(payload.top_candidates)}")
        for c in payload.top_candidates:
            print(f"  [{c.tier}] {c.symbol} ({c.direction}): Conf {c.confidence}% | Entry {c.current_price} | SL {c.sl_price} | Margin ${c.required_margin:.1f} USDT")
        print(f"• Stat-Arb Pairs ({len(payload.actionable_stat_arb)} analyzed):")
        actionable = [p for p in payload.actionable_stat_arb if p.is_actionable]
        if actionable:
            for a in actionable:
                print(f"  🔥 {a.pair}: Z={a.z_score:+.2f}σ, ADF p={a.adf_pvalue:.3f}, Half-Life={a.half_life_hours:.1f}h -> {a.recommendation}")
        else:
            print("  ⚖️ No extreme cointegrated divergences (|Z| >= 2.0σ with ADF p < 0.05).")
        print(f"• YOLO Slot: {payload.yolo_slot_status}")
        for y in (payload.yolo_slot.candidates if payload.yolo_slot else []):
            print(f"  🚀 {y.symbol} (LONG {y.leverage}x): Trigger {y.trigger:.6g} | SL {y.sl:.6g} (-{y.risk_pct}%) | "
                  f"TP1 {y.tp1:.6g} / TP2 {y.tp2:.6g} | Vol {y.vol_ratio}x | Wick {y.lower_wick}% | RSI {y.rsi}")
    return 0

def _run_cli(argv: Optional[list] = None) -> None:
    code = main(argv)
    _exit_without_waiting_for_yolo(code)
    sys.exit(code)

if __name__ == "__main__":
    _run_cli()
