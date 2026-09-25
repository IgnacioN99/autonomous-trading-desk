#!/usr/bin/env python3
"""
provenance_stamp.py - Firma y Trazabilidad Operativa de Acciones de Trading.
Auditoría y metadatos de atribución para operaciones y decisiones agénticas.

Registra de forma determinista el origen de cada orden y evaluación:
Cada orden, salida a mercado, lección o ajuste de stop lleva una firma estructurada que identifica:
1. Qué agente o evaluador emitió la señal (y su versión).
2. Qué loop operacional ejecutó la orden.
3. Qué estrategia técnica cuantitativa generó la hipótesis.
4. Qué modelo de dimensionamiento de riesgo se utilizó.

Gramática Canónica:
  🤖 <agent>@<version> · via <loop>@<version> · strategy:<name>@<version> · sizing:<model>
"""

import os
import sys
import re
import time
from typing import Dict, Any, Optional

DEFAULT_VERSIONS = {
    "evaluator": "isolated_market_evaluator@2.1.0",
    "loop": "fast_track_execution@1.0.0",
    "strategy": "microstructure_wick_reversion@3.0.0",
    "sizing": "volatility_parity@1.5.0"
}

def generate_provenance_stamp(
    evaluator: str = "isolated_market_evaluator",
    evaluator_version: str = "2.1.0",
    loop: str = "fast_track_execution",
    loop_version: str = "1.0.0",
    strategy: str = "microstructure_wick_reversion",
    strategy_version: str = "3.0.0",
    sizing_model: str = "volatility_parity_1.50",
    author: str = "agent"
) -> Dict[str, Any]:
    """
    Genera el bloque estructurado de procedencia y el string canónico.
    """
    evaluator_full = f"{evaluator}@{evaluator_version}"
    loop_full = f"{loop}@{loop_version}"
    strategy_full = f"{strategy}@{strategy_version}"
    
    canonical_stamp = f"🤖 {evaluator_full} · via {loop_full} · strategy:{strategy_full} · sizing:{sizing_model}"
    
    return {
        "canonical_stamp": canonical_stamp,
        "author": author,
        "evaluator": evaluator,
        "evaluator_version": evaluator_version,
        "loop": loop,
        "loop_version": loop_version,
        "strategy": strategy,
        "strategy_version": strategy_version,
        "sizing_model": sizing_model,
        "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    }

def parse_provenance_stamp(stamp_str: str) -> Optional[Dict[str, str]]:
    """
    Parsea un string de procedencia canónico en sus componentes estructurados.
    """
    pattern = r"🤖\s+([^@]+)@([^\s]+)\s+·\s+via\s+([^@]+)@([^\s]+)\s+·\s+strategy:([^@]+)@([^\s]+)\s+·\s+sizing:([^\s]+)"
    match = re.search(pattern, stamp_str)
    if not match:
        return None
        
    return {
        "evaluator": match.group(1),
        "evaluator_version": match.group(2),
        "loop": match.group(3),
        "loop_version": match.group(4),
        "strategy": match.group(5),
        "strategy_version": match.group(6),
        "sizing_model": match.group(7)
    }

def stamp_trade_record(trade_record: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """
    Adjunta metadatos de procedencia a un registro de trade antes de guardarlo en ledger.
    """
    prov = generate_provenance_stamp(**kwargs)
    trade_record["provenance"] = prov
    trade_record["provenance_stamp"] = prov["canonical_stamp"]
    return trade_record

if __name__ == "__main__":
    test_stamp = generate_provenance_stamp()
    print("Canónico:", test_stamp["canonical_stamp"])
    parsed = parse_provenance_stamp(test_stamp["canonical_stamp"])
    print("Parseado:", parsed)
