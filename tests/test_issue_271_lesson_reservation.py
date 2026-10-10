#!/usr/bin/env python3
"""
Issue #271 (reopened): after PR #276 the evaluator still received one lesson (the forced correction) and 6-7
dropped_lessons in every PROD brief; the TRXUSDT LONG of 01:24 UTC was approved Tier S without the lesson that had
downgraded it 4 minutes earlier.

1. The 01:24 reproduction: 6 setups with the radar's long reasons and noise fields, the 8 active lessons of the
   ledger (realistic Spanish text and sizes, a suppressed and a tombstoned one): all 8 reach the brief, whole and with
   their id, ecc33c ranked ahead of the non-matching global lessons, no dropped_lessons, the brief under
   BRIEF_BUDGET_BYTES (< 3,000 tokens at bytes / 4).
2. The floor wins over the cap: 8 setups still get LESSON_FLOOR_BYTES of lessons (stderr note when the brief ends
   over BRIEF_BUDGET_BYTES); a much bigger ledger lists the overflow in dropped_lessons (ids match the entries).
3. Row noise trim: dropped fields gone, every consumer-read field kept (shadow_tracker, markdown, prompt).
4. Direction-aware ranking of global / pinned lessons.

Hermetic: temp brief and ledger files (never the real ledger), the screening payload, state and equity faked,
urlopen blocked, no Binance client.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BASE_DIR, "scripts"), os.path.join(BASE_DIR, "scripts", "hooks"),
           os.path.join(BASE_DIR, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_issue_187_lesson_selection as t187  # noqa: E402  (fixtures only)
import test_issue_271_brief_lesson_budget as t271  # noqa: E402  (fixtures only)
import prime_evaluator_brief as peb  # noqa: E402


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def setUpModule():
    global _net_patch
    _net_patch = patch("urllib.request.urlopen", side_effect=_no_network)
    _net_patch.start()


def tearDownModule():
    _net_patch.stop()


# =============================================================================
# The 01:24 UTC fixture
# =============================================================================
CORRECTED_ID = "ins-1791473720-95c1c8"
CORRECTION_ID = "ins-1791477666-4fd4d6"
TOMBSTONED_ID = "ins-1791250000-c5ea21"
SHORTS_ID = "ins-1791486469-ecc33c"
MANAGEMENT_ID = "ins-1791486469-14123c"
DECISIVE = "no encadenar setups marginales; parar el dia tras 2 SL seguidos o -3R"

LESSON_TEXTS = {
    "ins-1789900000-d9c32c": (
        "BTC macro: no abrir LONGs de altcoins en las 2 horas previas a FOMC o CPI; la volatilidad del evento barre "
        "los stops ajustados y el régimen de BTC siempre manda."),
    "ins-1790183839-93fae4": (
        "UNI short: no abrir SHORTs justo después de un anuncio de listado en CME o de un catalizador institucional; "
        "el squeeze posterior liquidó el short completo en menos de 40 minutos."),
    "ins-1790500000-5c4020": (
        "Cesta de alts: varios LONGs de alts a la vez son una sola apuesta a la beta de BTC, no diversificación. "
        "Máximo dos LONGs correlacionados abiertos a la vez; el tercero se descarta aunque el radar lo marque Tier A+ "
        "con buen R:R y volumen clímax."),
    "ins-1790800000-5f1380": (
        "Shadow desk: los candidatos rechazados se registran como shadow trades para medir el coste de oportunidad. "
        "Tras 30 registros cerrados, los rechazos por volumen seco (vol_ratio < 1.0x) acertaron en 8 de cada 10 casos "
        "y los rechazos por delta gate en 7 de cada 10. No relajar el filtro de volumen por un par de aciertos sueltos "
        "del shadow desk."),
    "ins-1791000000-7a5119": (
        "Capital: el riesgo por operación es el 2% del equity y el margen de una posición nunca supera el 30% del "
        "equity. Tras una racha de tres pérdidas seguidas no se sube el tamaño para recuperar: se mantiene el riesgo "
        "fijo y se revisa el journal antes de la siguiente entrada. Si el equity cae un 10% desde el máximo de la "
        "semana, el riesgo por operación baja a la mitad hasta recuperarlo. Nunca se añade margen a una posición "
        "perdedora ni se mueve el stop loss para evitar que salte: el stop es la tesis invalidada, no una opinión."),
    CORRECTION_ID: (
        "Corrige ins-1791473720: la regla del 80% (cerrar el 80% de un SHORT en TP1) recortaba la cola derecha: en "
        "la muestra de octubre los SHORTs que llegaron a TP1 siguieron hasta TP2 en 6 de 9 casos y el cierre parcial "
        "dejó sobre la mesa más de 2R por operación. El problema no era el TP1 sino mover el stop a break-even "
        "demasiado pronto con ruido de 5m, lo que convirtió ganadores en cierres a cero. Regla: en SHORTs, TP1 cierra "
        "solo el 30% de la posición; el stop pasa a True Net Break-Even (+0.2% de comisiones) únicamente tras TP1 o "
        "tras +2 ATR de 15m a favor, nunca antes; el 70% restante se gestiona con el trailing estructural de swings "
        "de 15m hasta TP2. No volver a la regla del 80% sin una muestra de 30 operaciones en el scorecard, ni por "
        "una sola semana buena de resultados."),
    SHORTS_ID: (
        "Mercado SHORTs: LITUSDT y BTWUSDT SHORT se evaluaron en régimen SHORT_SQUEEZE con oi_z por encima de 2.0 y "
        "funding negativo (-0.012%/8h): ambos saltaron el stop en menos de una hora porque el interés abierto seguía "
        "subiendo, los cortos pagaban funding cada 4 horas y BTC rompía su resistencia de 4h. Un SHORT de alt con oi_z >= 2.0 o funding <= -0.01%/8h es "
        "combustible para un squeeze, no una señal de agotamiento. Regla: con squeeze_risk activo, un SHORT de alt "
        "es como mucho Tier A con confirmación; sin rechazo de BTC en resistencia o volumen clímax >= 2.5x, se "
        "rechaza sin excepciones por puntuación."),
    MANAGEMENT_ID: (
        "Gestión: revisión de la sesión del 9 de octubre. (1) Los stops movidos a break-even antes de +1R por ruido "
        "de 5m cerraron a cero tres operaciones que luego llegaron a TP2; el trailing solo se activa tras +1R en "
        "velas de 15m cerradas o tras TP1. (2) El trailing con swings de 15m y Chandelier de 1.8x ATR protegió bien "
        "las dos operaciones ganadoras; no apretarlo a mano ni moverlo por intuición tras una vela roja de 5m. (3) "
        "Las salidas por dead alpha en rangos comprimidos con volumen seco fueron prematuras: si la estructura sigue "
        "intacta es acumulación, no un fallo de la tesis, y el stop estructural ya limita la pérdida. (4) El día se "
        "perdió por sobreoperar: tras dos SL seguidos se abrieron cuatro setups Tier A marginales más, todos "
        "perdedores, y la pérdida pasó de -2R a -5R en menos de tres horas, con el último trade abierto por pura "
        "impaciencia. Lección: " + DECISIVE + "."),
}
# insight sizes (bytes) of the real ledger's active lessons (locator estimates)
LESSON_SIZES = {"d9c32c": 160, "93fae4": 175, "5c4020": 245, "5f1380": 345, "7a5119": 540, "4fd4d6": 790,
                "ecc33c": 620, "14123c": 950}
ACTIVE_IDS = tuple(LESSON_TEXTS)


def ledger_0124(extra=()):
    """The ledger of 2026-10-10 in file order: 8 active lessons, the lesson 4fd4d6 corrects (suppressed) and a
    tombstoned one. `extra`: more records appended (most recent)."""
    t = LESSON_TEXTS
    return [
        t187.lesson("ins-1789900000-d9c32c", "MACRO", "LONG", t["ins-1789900000-d9c32c"],
                    ["macro", "btc_regime", "longs", "fomc"]),
        t187.lesson("ins-1790183839-93fae4", "UNI", "SHORT", t["ins-1790183839-93fae4"],
                    ["uni", "short_squeeze", "cme_listing", "catalyst"]),
        t187.lesson("ins-1790500000-5c4020", "ALTS_BASKET", "LONG", t["ins-1790500000-5c4020"],
                    ["alts", "basket", "correlation", "btc_beta"]),
        t187.lesson("ins-1790800000-5f1380", "SHADOW_DESK", "NEUTRAL", t["ins-1790800000-5f1380"],
                    ["shadow_desk", "calibration", "process"]),
        t187.lesson("ins-1791000000-7a5119", "ACCOUNT_CAPITAL", "NEUTRAL", t["ins-1791000000-7a5119"],
                    ["account_capital", "sizing", "risk_pct_equity", "drawdown"]),
        t187.lesson(TOMBSTONED_ID, "MACRO", "NEUTRAL", "Lección retirada: ya no aplica.", ["retired"]),
        t187.tombstone(TOMBSTONED_ID),
        t187.lesson(CORRECTED_ID, "MARKET_MANAGEMENT", "SHORT",
                    "Regla del 80%: en SHORTs cerrar el 80% de la posición en TP1 para asegurar el día.",
                    ["rule_80pct", "management", "take_profit"]),
        t187.lesson(CORRECTION_ID, "MARKET_MANAGEMENT", "SHORT", t[CORRECTION_ID],
                    ["corrects_" + CORRECTED_ID, "management", "take_profit", "shorts", "correction"]),
        t187.lesson(SHORTS_ID, "MARKET_SHORTS", "SHORT", t[SHORTS_ID],
                    ["shorts", "squeeze", "oi_z", "funding"]),
        t187.lesson(MANAGEMENT_ID, "MARKET_MANAGEMENT", "NEUTRAL", t[MANAGEMENT_ID],
                    ["management", "trailing", "overtrading", "daily_stop"]),
    ] + list(extra)


TIER_LABELS = {"S": "Tier S (🔥 Top Score, order flow confirmed)", "A+": "Tier A+ (High Confirmed Score)",
               "A": "Tier A (Strong Confluence / Hedge)"}


def radar_row(sym, direction, price, conf, tier_code, scored=True):
    """A top_candidates row as screening_pipeline.CandidateSetup dumps it, with the radar's reasons (an ORDER FLOW
    line of 150-170 bytes when absorption was scored)."""
    up = 1 if direction == "LONG" else -1
    sl, tp1, tp2 = price * (1 - 0.021 * up), price * (1 + 0.0378 * up), price * (1 + 0.084 * up)
    if direction == "LONG":
        flow = ("🔬 ORDER FLOW: Active Bullish Absorption on the 2026-10-10 01:15 UTC candle (Taker Ratio 0.78, "
                "CVD delta -18,342 absorbed by bid wall, lower wick 52%)")
        reasons = ["RSI 15m oversold (27.4)", "Support rejection (52% lower wick)"]
    else:
        flow = ("🔬 ORDER FLOW: Active Bearish Absorption on the 2026-10-10 01:15 UTC candle (Taker Ratio 1.31, "
                "CVD delta +22,907 absorbed by ask wall, upper wick 48%)")
        reasons = ["RSI 15m overbought (74.1)", "Resistance rejection (48% upper wick)"]
    reasons.append(flow if scored else "🔬 ORDER FLOW: absorption not scored (wick/taker candle mismatch)")
    return {"symbol": sym, "direction": direction, "tier": TIER_LABELS[tier_code], "confidence": conf,
            "current_price": price, "trigger_price": round(price * (1 - 0.0015 * up), 6), "sl_price": sl,
            "tp1_price": tp1, "tp2_price": tp2, "rr_ratio": 4.0, "risk_pct": 2.1, "rsi_15m": 27.4 if up > 0 else 74.1,
            "vol_ratio": 1.62, "lower_wick_pct": 52.0, "upper_wick_pct": 11.0, "cvd_delta": -18342.6,
            "oi_z_score": -1.38, "oib_ratio": 0.17, "vwap_deviation_pct": -2.31, "cascade_risk": "BASELINE",
            "regime": "LONG_UNWINDING", "absorption": "BULLISH_ABSORPTION", "whale_bias": "BALANCED",
            "required_margin": 9.87, "step_qty": 118.0, "actual_notional": 29.61, "target_dollar_risk": 5.34,
            "reasons": reasons, "sizing_entry_price": round(price * (1 - 0.0015 * up), 6), "tier_code": tier_code,
            "absorption_scored": scored, "score_components": {"rsi": 20, "wick": 15, "sweep": 10, "flow": 20},
            "tier_s_eligible": tier_code == "S", "funding_rate_pct": 0.0031, "squeeze_risk": False,
            "squeeze_reasons": [], "long_crowding_risk": False,
            "macro_short_check": "btc_rejection" if direction == "SHORT" else None, "funding_rate_8h_pct": None,
            "funding_interval_h": None, "alt_short_climax_ok": False, "score_schema_version": 2,
            "funding_interval_unknown": False, "expected_fee_r": 0.048}


# The first four are the 01:24 brief's setups; with them the pre-#271-reopen code writes 6,958 bytes (the real brief:
# 6,854) with 1 lesson shown and 7 dropped
SETUPS_0124 = (("BRUSDT", "SHORT", 0.08412, 68, "A+", False), ("TRXUSDT", "LONG", 0.33871, 84, "S", False),
               ("GRAMUSDT", "LONG", 0.002871, 66, "A+", False), ("QUSDT", "SHORT", 0.01734, 61, "A", False),
               ("ZECUSDT", "LONG", 241.37, 63, "A"), ("ENAUSDT", "SHORT", 0.5713, 58, "A", False),
               ("AVAXUSDT", "LONG", 27.418, 57, "A"), ("WLDUSDT", "SHORT", 1.2874, 56, "A", False),
               ("SEIUSDT", "LONG", 0.18342, 59, "A"), ("TIAUSDT", "SHORT", 3.7712, 57, "A", False),
               ("INJUSDT", "LONG", 13.284, 58, "A"), ("OPUSDT", "SHORT", 0.6418, 56, "A", False))


def screening_0124(setups=6):
    """The 01:24 UTC screening: `setups` radar rows, 10 non-actionable stat-arb pairs, 3 funding rows, BTC macro."""
    return {"top_candidates": [radar_row(*s) for s in SETUPS_0124[:setups]],
            "actionable_stat_arb": t271.session_pairs(),
            "top_funding_arbitrage": [t271.funding_row("MOODENGUSDT", 0.0612), t271.funding_row("PNUTUSDT", 0.0488),
                                      t271.funding_row("WIFUSDT", -0.0391)],
            "macro": {"btc_price": 62187.4, "btc_regime": "NEUTRAL_CONSOLIDATION",
                      "btc_regime_desc": "Range Consolidation / Auction Equilibrium (OI(latest) Z_OI=+0.41σ)",
                      "btc_absorption": "NONE", "btc_taker_ratio": 0.97, "btc_cvd_30v": -1834.2,
                      "btc_oi_z_score": 0.41, "btc_tape_bias": "BALANCED", "btc_tape_imbalance": -3.2,
                      "allows_alt_shorts": True, "macro_warning": None, "btc_data_ok": True}}


STATE_0124 = {
    "target_env": "prod", "last_updated_ts": 0,
    "active_positions": [{"symbol": "TAOUSDT", "direction": "LONG", "entry_price": 318.42, "mark_price": 321.07,
                          "unrealized_pnl_usdt": 0.42, "roe_pct": 2.49, "sl_price": 311.9, "sl_algo_verified": True,
                          "origin": "agy:3f2a91c0 dossier 9b1e44d2"}],
    "portfolio_exposure": {"delta_bias": "LONG_HEAVY", "delta_bias_incl_resting": "LONG_HEAVY",
                           "long_notional_usdt": 50.97, "short_notional_usdt": 0.0, "net_notional_delta_usdt": 50.97,
                           "total_floating_pnl_usdt": 0.42,
                           "delta_advice": "LONG heavy: only SHORT entries or delta-reducing hedges.",
                           "resting_entries": [{"symbol": "SUIUSDT", "dir": "SHORT", "kind": "STOP_MARKET",
                                                "origin": "agy:3f2a91c0 dossier 9b1e44d2"}]},
    "closed_today_summary": {"closed_trades_count": 2, "net_realized_pnl_usdt": -3.71, "counted_by": "trades",
                             "fills_closed": 0, "truncated": False},
    "daily_loss_gate": {"blocked": False, "scope": None, "reason": None},
}


def block_bytes_table(brief):
    """Bytes of each top-level block of the brief file (exact: indent 0 serializes a part as it is in the whole)."""
    return {k: peb._brief_bytes(v) for k, v in brief.items()}


class Case(t271.BriefCase):

    def run_0124(self, setups=6, extra=()):
        ledger = ledger_0124(extra)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), patch.object(peb, "load_recent_insights",
                                                            wraps=peb.load_recent_insights) as fn:
            brief = self.assemble(screening_0124(setups), self.ledger_file(ledger), dict(STATE_0124))
        with open(self.brief_file, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(json.loads(text), json.loads(json.dumps(brief, ensure_ascii=False)))
        self.assertLessEqual(max(len(line) for line in text.splitlines()), peb.BRIEF_MAX_LINE_CHARS)
        self.assertEqual(len(brief["filtered_opportunities"]), setups)  # setups are never trimmed
        budgets = [c.kwargs["budget_bytes"] for c in fn.call_args_list]
        return brief, ledger, err.getvalue(), budgets, os.path.getsize(self.brief_file)


# =============================================================================
# 1. The 01:24 reproduction
# =============================================================================
class TestBrief0124(Case):

    def test_fixture_reproduces_the_0124_brief_with_the_old_settings(self):
        """Calibration: the PR #276 settings (7,199 / 1,500 bytes, the lesson budget without a floor, no row trim)
        give the real 01:24 brief: about 6,854 bytes, the correction shown and the other 7 lessons dropped."""
        old_drop = {k: v for k, v in peb._DROP_WHEN_UNSET.items() if k not in ("cascade_risk", "whale_bias")}
        with patch.object(peb, "BRIEF_BUDGET_BYTES", 7199), patch.object(peb, "LESSON_FLOOR_BYTES", 1500), \
             patch.object(peb, "_lesson_budget", lambda b: min(peb.LESSON_BUDGET_BYTES, peb._lesson_room(b))), \
             patch.object(peb, "_BRIEF_DROP_KEYS", ()), patch.object(peb, "_TIER_CODES", ()), \
             patch.object(peb, "BRIEF_REASONS_MAX", 99), patch.object(peb, "BRIEF_REASON_MAX", 9999), \
             patch.object(peb, "_DROP_WHEN_UNSET", old_drop):
            brief, _ledger, _err, _budgets, size = self.run_0124(4)
        self.assertLessEqual(abs(size - 6854), 6854 * 0.02, size)
        self.assertEqual([e["id"] for e in brief["committed_memory_lessons"]], [CORRECTION_ID])
        self.assertEqual(len(brief["dropped_lessons"]), 7)
        self.assertIn("tier", brief["filtered_opportunities"][0])  # the old rows

    def test_fixture_matches_the_ledger_sizes(self):
        for lid, text in LESSON_TEXTS.items():
            size = len(text.encode("utf-8"))
            target = LESSON_SIZES[lid[-6:]]
            self.assertTrue(0.95 * target <= size <= 1.05 * target, f"{lid}: {size} bytes vs ~{target}")
        self.assertTrue(LESSON_TEXTS[MANAGEMENT_ID].endswith(DECISIVE + "."))  # the decisive sentence is last

    def test_six_setups_carry_all_eight_lessons_whole_with_their_id(self):
        brief, ledger, err, budgets, size = self.run_0124(6)
        entries = brief["committed_memory_lessons"]
        ids = [e["id"] for e in entries]
        self.assertEqual(sorted(ids), sorted(ACTIVE_IDS))
        self.assertEqual(ids[0], CORRECTION_ID)  # the correction first
        self.assertNotIn(CORRECTED_ID, ids)
        self.assertNotIn(TOMBSTONED_ID, ids)
        for e in entries:
            self.assertEqual(e["lesson"], LESSON_TEXTS[e["id"]])  # never cut
        self.assertTrue(entries[ids.index(MANAGEMENT_ID)]["lesson"].endswith(DECISIVE + "."))
        self.assertNotIn("dropped_lessons", brief)
        self.assertNotIn("lesson_budget_exceeded", brief)
        self.assertNotIn("WARNING", err)
        self.assertLessEqual(size, peb.BRIEF_BUDGET_BYTES, block_bytes_table(brief))
        self.assertLess(size / 4, 3000)
        self.assertGreaterEqual(budgets[0], peb.LESSON_FLOOR_BYTES)

    def test_shorts_lesson_ranks_ahead_of_non_matching_globals(self):
        brief, _ledger, _err, _budgets, _size = self.run_0124(6)
        ids = [e["id"] for e in brief["committed_memory_lessons"]]
        # LONG and SHORT candidates: no global lesson is off-direction, so recency (14123c is the most recent)
        self.assertEqual(ids[:3], [CORRECTION_ID, MANAGEMENT_ID, SHORTS_ID])
        long_globals = ["ins-1790500000-5c4020", "ins-1789900000-d9c32c"]  # LONG, recent first
        neutral = [MANAGEMENT_ID, "ins-1791000000-7a5119", "ins-1790800000-5f1380"]  # NEUTRAL, recent first

        def ids_for(direction):
            screening = screening_0124(6)
            screening["top_candidates"] = [c for c in screening["top_candidates"] if c["direction"] == direction]
            with contextlib.redirect_stderr(io.StringIO()):
                brief = self.assemble(screening, self.ledger_file(ledger_0124()), dict(STATE_0124))
            return [e["id"] for e in brief["committed_memory_lessons"]]

        # only SHORT candidates: ecc33c ahead of the LONG globals, which come last in their rank
        ids = ids_for("SHORT")
        self.assertEqual(ids[:7], [CORRECTION_ID, MANAGEMENT_ID, SHORTS_ID] + neutral[1:] + long_globals)
        # only LONG candidates: the SHORT global lesson is still eligible, after every matching one
        ids = ids_for("LONG")
        self.assertEqual(ids[:7], [CORRECTION_ID] + neutral + long_globals + [SHORTS_ID])

    def test_markdown_brief_renders_lessons_and_rows(self):
        brief, _ledger, _err, _budgets, _size = self.run_0124(6)
        md = peb.format_markdown_brief(brief)
        self.assertIn(DECISIVE, md)
        self.assertIn("### 🎯 Filtered Technical Setups (6)", md)
        trx = next(line for line in md.splitlines() if line.startswith("| **TRXUSDT**"))
        self.assertIn("| LONG | S | score 84 | 0.33871 |", trx)
        self.assertIn("| $5.34 |", trx)
        self.assertIn("RSI 15m oversold (27.4); Support rejection (52% lower wick)", trx)


# =============================================================================
# 2. Floor over the cap, overflow listed
# =============================================================================
class TestFloorAndOverflow(Case):

    def test_eight_setups_keep_at_least_the_floor(self):
        brief, _ledger, err, budgets, size = self.run_0124(8)
        self.assertTrue(all(b >= peb.LESSON_FLOOR_BYTES for b in budgets), budgets)
        shown = [e["id"] for e in brief["committed_memory_lessons"]]
        self.assertGreaterEqual(len(shown), 4)
        self.assertEqual(shown[:3], [CORRECTION_ID, MANAGEMENT_ID, SHORTS_ID])  # 14123c: the TRXUSDT lesson
        self.assertEqual(set(brief.get("dropped_lessons", [])) | {e["id"] for e in brief["committed_memory_lessons"]},
                         set(ACTIVE_IDS))
        self.assertLessEqual(size, peb.BRIEF_BUDGET_BYTES)
        self.assertNotIn("NOTE: brief", err)

    def test_twelve_setups_the_floor_wins(self):
        brief, _ledger, err, budgets, size = self.run_0124(12)
        rest = peb._brief_bytes(dict(brief, committed_memory_lessons=[], dropped_lessons=[]))
        self.assertLess(peb.BRIEF_BUDGET_BYTES - rest + 2, peb.LESSON_FLOOR_BYTES)  # the cap alone would squeeze
        self.assertEqual(budgets[-1], peb.LESSON_FLOOR_BYTES)  # the floor wins
        shown = peb._brief_bytes(brief["committed_memory_lessons"])
        self.assertLessEqual(shown, peb.LESSON_FLOOR_BYTES)
        self.assertEqual(brief["committed_memory_lessons"][0]["id"], CORRECTION_ID)
        self.assertGreaterEqual(len(brief["committed_memory_lessons"]), 3)
        self.assertIn(SHORTS_ID, [e["id"] for e in brief["committed_memory_lessons"]])
        self.assertIn(MANAGEMENT_ID, [e["id"] for e in brief["committed_memory_lessons"]])  # the TRXUSDT lesson
        self.assertGreater(size, peb.BRIEF_BUDGET_BYTES)
        notes = [line for line in err.splitlines() if line.startswith("NOTE: brief")]
        self.assertEqual(len(notes), 1, err)
        self.assertIn(f"over BRIEF_BUDGET_BYTES {peb.BRIEF_BUDGET_BYTES}", notes[0])
        # the lessons left out are listed, ids matching the ledger
        self.assertEqual(set(brief["dropped_lessons"]) | {e["id"] for e in brief["committed_memory_lessons"]},
                         set(ACTIVE_IDS))
        self.assertNotIn("lesson_budget_exceeded", brief)  # the floor is not a forced lesson over the cap

    def test_a_forced_lesson_over_the_cap_is_not_blamed_on_the_floor(self):
        """A pinned lesson larger than the brief budget: lesson_budget_exceeded, and no floor NOTE (the rest of the
        brief leaves more than the floor)."""
        pinned = t187.lesson("ins-1700000001-pin000", "XRP", text="Lección fijada. " * 800, tags=["pinned"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            brief = self.assemble(screening_0124(4), self.ledger_file([pinned]), dict(STATE_0124))
        self.assertGreater(os.path.getsize(self.brief_file), peb.BRIEF_BUDGET_BYTES)
        self.assertIs(brief["lesson_budget_exceeded"], True)
        self.assertNotIn("NOTE: brief", err.getvalue())

    def test_floor_wins_over_the_cap(self):
        """A brief whose rest leaves no room: the lesson budget is LESSON_FLOOR_BYTES, not 0."""
        screening = screening_0124(6)
        screening["macro_rejected_shorts"] = [{"symbol": f"S{i:03d}USDT" + "X" * 40} for i in range(120)]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), patch.object(peb, "load_recent_insights",
                                                            wraps=peb.load_recent_insights) as fn:
            brief = self.assemble(screening, self.ledger_file(ledger_0124()), dict(STATE_0124))
        rest = peb._brief_bytes(dict(brief, committed_memory_lessons=[]))
        self.assertGreater(rest, peb.BRIEF_BUDGET_BYTES)  # the old formula: a negative budget
        self.assertEqual(fn.call_args_list[0].kwargs["budget_bytes"], peb.LESSON_FLOOR_BYTES)
        self.assertGreaterEqual(len(brief["committed_memory_lessons"]), 3)
        self.assertEqual(len([line for line in err.getvalue().splitlines() if line.startswith("NOTE: brief")]), 1)

    def test_a_much_bigger_ledger_lists_the_overflow(self):
        extra = [t187.lesson(f"ins-17920000{i:02d}-big{i:03d}", "XRP", "LONG",
                             f"Lección {i}: " + "texto de relleno con acentos áéí " * 12, ["xrp"]) for i in range(20)]
        brief, ledger, err, _budgets, size = self.run_0124(6, extra)
        shown = [e["id"] for e in brief["committed_memory_lessons"]]
        dropped = brief["dropped_lessons"]
        active = set(ACTIVE_IDS) | {r["id"] for r in extra}
        self.assertEqual(set(shown) | set(dropped), active)  # never dropped silently
        self.assertFalse(set(shown) & set(dropped))
        self.assertTrue({r["id"] for r in extra} & set(dropped))
        # the ranked lessons come before the recency fill (the dropped_lessons key's room costs the oldest ones)
        self.assertFalse({r["id"] for r in extra} & set(shown))
        self.assertGreaterEqual(len(set(ACTIVE_IDS) & set(shown)), 6)
        self.assertEqual(shown[:3], [CORRECTION_ID, MANAGEMENT_ID, SHORTS_ID])
        warnings = [line for line in err.splitlines() if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)
        self.assertIn(", ".join(dropped), warnings[0])
        self.assertLessEqual(size, peb.BRIEF_BUDGET_BYTES)
        self.assertLessEqual(peb._brief_bytes(brief["committed_memory_lessons"]), peb.LESSON_BUDGET_BYTES)


# =============================================================================
# 3. Row noise trim
# =============================================================================
# Read by scripts/shadow_tracker.register_from_evaluation, format_markdown_brief or a prompt rule
CONSUMER_KEYS = ("symbol", "direction", "tier_code", "confidence", "current_price", "trigger_price", "sl_price",
                 "tp1_price", "tp2_price", "rr_ratio", "risk_pct", "vol_ratio", "target_dollar_risk",
                 "sizing_entry_price", "oi_z_score", "fee_r", "macro_short_check", "funding_rate_pct",
                 "absorption_scored", "reasons")


class TestRowTrim(unittest.TestCase):

    def test_noise_fields_dropped_and_consumer_fields_kept(self):
        raw = radar_row("BRUSDT", "SHORT", 0.08412, 68, "A+")
        row = peb._brief_opportunity(raw)
        for k in ("tier", "required_margin", "step_qty", "actual_notional", "cascade_risk", "whale_bias"):
            self.assertNotIn(k, row)
        for k in CONSUMER_KEYS:
            self.assertIn(k, row)
        for k in ("current_price", "trigger_price", "sl_price", "tp1_price", "tp2_price", "sizing_entry_price",
                  "target_dollar_risk", "vol_ratio", "confidence", "oi_z_score"):
            self.assertEqual(row[k], raw[k], k)  # never rounded or altered
        self.assertEqual(row["reasons"], raw["reasons"][:2])
        self.assertEqual(peb._tier_label(row), "A+")
        self.assertLess(peb._brief_bytes(row) + 300, peb._brief_bytes(raw))

    def test_kept_when_not_the_neutral_default(self):
        raw = dict(radar_row("BRUSDT", "SHORT", 0.08412, 68, "A+"), cascade_risk="NUCLEATION_PEAK",
                   whale_bias="BEARISH_PRESSURE")
        row = peb._brief_opportunity(raw)
        self.assertEqual((row["cascade_risk"], row["whale_bias"]), ("NUCLEATION_PEAK", "BEARISH_PRESSURE"))
        for code in (None, "DISQUALIFIED", "B"):  # no valid tier_code: the label stays (the markdown reads it)
            with self.subTest(code=code):
                row = peb._brief_opportunity(dict(raw, tier_code=code))
                self.assertEqual(row["tier"], raw["tier"])
                self.assertEqual(peb._tier_label(row), "A+")

    def test_reasons_capped_at_two_and_120_characters(self):
        long_reason = "🔬 ORDER FLOW: " + "x" * 200
        row = peb._brief_opportunity({"symbol": "A", "reasons": [long_reason, "short", "third"]})
        self.assertEqual(len(row["reasons"]), 2)
        self.assertEqual(len(row["reasons"][0]), peb.BRIEF_REASON_MAX)
        self.assertTrue(row["reasons"][0].endswith("…"))
        self.assertTrue(long_reason.startswith(row["reasons"][0][:-1]))
        self.assertEqual(row["reasons"][1], "short")
        self.assertEqual(peb._brief_opportunity({"symbol": "A", "reasons": []})["reasons"], [])
        self.assertEqual(peb._brief_opportunity({"symbol": "A"}), {"symbol": "A"})

    def test_squeeze_flags_survive(self):
        raw = dict(radar_row("QUSDT", "SHORT", 0.01734, 61, "A"), squeeze_risk=True,
                   squeeze_reasons=["oi_z>=2.0 (oi_z=2.40)"], funding_rate_8h_pct=-0.012, funding_interval_h=4)
        row = peb._brief_opportunity(raw)
        self.assertEqual((row["squeeze_risk"], row["squeeze_reasons"]), (True, ["oi_z>=2.0 (oi_z=2.40)"]))
        self.assertEqual((row["funding_rate_8h_pct"], row["funding_interval_h"]), (-0.012, 4))


# =============================================================================
# 4. Direction-aware ranking
# =============================================================================
class TestDirectionRanking(t187.InsightsFile):

    RECS = [t187.lesson("ins-1-long", "MACRO", "LONG"), t187.lesson("ins-2-short", "MARKET_SHORTS", "SHORT"),
            t187.lesson("ins-3-neutral", "ACCOUNT_CAPITAL", "NEUTRAL"),
            t187.lesson("ins-4-pin-short", "XRP", "SHORT", tags=["pinned"]), t187.lesson("ins-5-uni", "UNI", "SHORT")]

    def test_matching_direction_first_within_the_global_rank(self):
        """A LONG / SHORT global or pinned lesson whose direction no candidate has comes after the others; NEUTRAL
        lessons count as matching."""
        self.assertEqual(self.ids(self.RECS, candidates=[("UNIUSDT", "SHORT")]),
                         ["ins-4-pin-short", "ins-3-neutral", "ins-2-short", "ins-1-long", "ins-5-uni"])
        self.assertEqual(self.ids(self.RECS, candidates=[("BTCUSDT", "LONG")]),
                         ["ins-3-neutral", "ins-1-long", "ins-4-pin-short", "ins-2-short", "ins-5-uni"])
        both = self.ids(self.RECS, candidates=[("BTCUSDT", "LONG"), ("UNIUSDT", "SHORT")])
        self.assertEqual(both, ["ins-4-pin-short", "ins-3-neutral", "ins-2-short", "ins-1-long", "ins-5-uni"])
        # no candidate direction (no candidates, or only stat-arb legs): recency, as before
        for cands in (None, [("SOL", None)]):
            self.assertEqual(self.ids(self.RECS, candidates=cands),
                             ["ins-4-pin-short", "ins-3-neutral", "ins-2-short", "ins-1-long", "ins-5-uni"])

    def test_undirected_lessons_are_never_demoted(self):
        """NEUTRAL, empty or missing direction: never behind an off-direction lesson, however old."""
        recs = [t187.lesson("ins-1-neutral", "MARKET_MANAGEMENT", "NEUTRAL"),
                t187.lesson("ins-2-blank", "MACRO", ""), dict(t187.lesson("ins-3-none", "SHADOW_DESK"), direction=None),
                t187.lesson("ins-4-long", "ALTS_BASKET", "LONG"), t187.lesson("ins-5-short", "MARKET_SHORTS", "SHORT")]
        for cand, off in (("SHORT", "ins-4-long"), ("LONG", "ins-5-short")):
            with self.subTest(candidate=cand):
                ids = self.ids(recs, candidates=[("UNIUSDT", cand)])
                self.assertEqual(ids[-1], off)  # the only off-direction lesson, though more recent than three
                self.assertEqual(ids[:-1], [r["id"] for r in reversed(recs) if r["id"] != off])  # recency


if __name__ == "__main__":
    unittest.main()
