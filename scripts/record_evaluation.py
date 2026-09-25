#!/usr/bin/env python3
"""
record_evaluation.py - Registro Atómico del Dossier de Evaluación del Subagente.
Persistencia atómica de tokens de autorización de evaluación previa.

Guarda el dictamen emitido por el subagente 'isolated_market_evaluator' en logs/evaluations/latest_dossier.json.
Este archivo actúa como Token de Autorización criptográfico/mecánico que el hook pre_trade_guard.py
exige antes de permitir cualquier ejecución de órdenes en Binance.

Uso:
  python3 scripts/record_evaluation.py --symbols TIAUSDT,SAGAUSDT --directions LONG,SHORT --evaluator isolated_market_evaluator --summary "Cesta delta neutral aprobada"
  python3 scripts/record_evaluation.py --json-file ruta/dossier.json
"""

import os
import sys
import json
import time
import datetime
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.atomic_writer import atomic_write_json, atomic_append_jsonl

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(BASE_DIR, "logs", "evaluations")
DOSSIER_FILE = os.path.join(EVAL_DIR, "latest_dossier.json")
EVAL_HISTORY_FILE = os.path.join(EVAL_DIR, "evaluations_history.jsonl")

TTL_SECONDS = 1200 # 20 minutos de validez máxima antes de expirar

def record_evaluation_dossier(
    approved_candidates: list,
    evaluator_agent: str = "isolated_market_evaluator",
    conversation_id: str = None,
    summary: str = "",
    status: str = "APPROVED",
    raw_payload: dict = None
) -> dict:
    os.makedirs(EVAL_DIR, exist_ok=True)
    now_ts = int(time.time())
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    dossier = {
        "timestamp_utc": now_utc,
        "timestamp_ts": now_ts,
        "valid_until_ts": now_ts + TTL_SECONDS,
        "evaluator_agent": evaluator_agent,
        "conversation_id": conversation_id or os.environ.get("CONVERSATION_ID", "clean_room_context"),
        "status": status.upper(),
        "approved_symbols": [c.get("symbol", "").upper() for c in approved_candidates if isinstance(c, dict)],
        "approved_candidates": approved_candidates,
        "summary": summary.strip(),
        "raw_payload": raw_payload or {}
    }

    # Escritura atómica del latest_dossier.json
    atomic_write_json(DOSSIER_FILE, dossier)

    # Historial persistente append-only
    history_record = {
        "timestamp_utc": now_utc,
        "evaluator_agent": evaluator_agent,
        "status": status.upper(),
        "approved_symbols": dossier["approved_symbols"],
        "summary": summary.strip()
    }
    atomic_append_jsonl(EVAL_HISTORY_FILE, history_record)

    print(f"✅ DOSSIER DE EVALUACIÓN REGISTRADO: {len(dossier['approved_symbols'])} activo(s) aprobados.")
    print(f"   Símbolos: {', '.join(dossier['approved_symbols'])} | Válido hasta: {datetime.datetime.fromtimestamp(dossier['valid_until_ts'], datetime.timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"   Ubicación: {DOSSIER_FILE}")
    return dossier

def main():
    parser = argparse.ArgumentParser(description="Registrador de Dossier de Evaluación de Subagente")
    parser.add_argument("--symbols", type=str, help="Lista de símbolos aprobados separados por comas (ej. TIAUSDT,SAGAUSDT)")
    parser.add_argument("--directions", type=str, help="Direcciones correspondientes (ej. LONG,SHORT)")
    parser.add_argument("--evaluator", type=str, default="isolated_market_evaluator", help="Nombre del subagente evaluador")
    parser.add_argument("--summary", type=str, default="Evaluación cuantitativa aprobada", help="Resumen o tesis")
    parser.add_argument("--status", type=str, default="APPROVED", choices=["APPROVED", "REJECTED", "NEUTRAL"], help="Veredicto")
    parser.add_argument("--json-file", type=str, help="Cargar dossier completo desde un archivo JSON")
    args = parser.parse_args()

    if args.json_file and os.path.exists(args.json_file):
        with open(args.json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        candidates = data.get("approved_candidates", [])
        if not candidates and "top_candidates" in data:
            candidates = data["top_candidates"]
        record_evaluation_dossier(
            approved_candidates=candidates,
            evaluator_agent=data.get("evaluator_agent", args.evaluator),
            summary=data.get("summary", args.summary),
            status=data.get("status", args.status),
            raw_payload=data
        )
    elif args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        dirs = [d.strip().upper() for d in (args.directions or "").split(",") if d.strip()]
        candidates = []
        for i, sym in enumerate(syms):
            direction = dirs[i] if i < len(dirs) else "LONG"
            candidates.append({"symbol": sym, "direction": direction})
        record_evaluation_dossier(
            approved_candidates=candidates,
            evaluator_agent=args.evaluator,
            summary=args.summary,
            status=args.status
        )
    else:
        # Si no hay argumentos, intentar leer JSON de stdin
        if not sys.stdin.isatty():
            try:
                import re
                raw_input = sys.stdin.read()
                match = re.search(r"<dossier_json>([\s\S]*?)</dossier_json>", raw_input)
                json_str = match.group(1).strip() if match else raw_input.strip()
                data = json.loads(json_str)
                candidates = data.get("approved_candidates", data.get("top_candidates", []))
                record_evaluation_dossier(
                    approved_candidates=candidates,
                    evaluator_agent=data.get("evaluator_agent", args.evaluator),
                    summary=data.get("summary", args.summary),
                    status=data.get("status", args.status),
                    raw_payload=data
                )
                return
            except Exception as e:
                print(f"Error parseando JSON de stdin: {e}", file=sys.stderr)
        parser.print_help()

if __name__ == "__main__":
    main()
