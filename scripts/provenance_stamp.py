#!/usr/bin/env python3
"""
provenance_stamp.py - Operational Provenance and Trade Attribution Stamp.
Auditing and attribution metadata for agentic trades and decisions.

Deterministically logs the origin of every order and evaluation:
Every order, market exit, lesson, or stop adjustment carries a structured signature identifying:
1. Which agent or evaluator emitted the signal (and its version).
2. Which operational loop executed the order.
3. Which quantitative strategy generated the thesis.
4. Which risk sizing model was applied.

Canonical Grammar:
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
    Generates structured provenance block and canonical string.
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
    Parses a canonical provenance string into structured components.
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
    Attaches provenance metadata to a trade record before saving to ledger.
    """
    prov = generate_provenance_stamp(**kwargs)
    trade_record["provenance"] = prov
    trade_record["provenance_stamp"] = prov["canonical_stamp"]
    return trade_record

if __name__ == "__main__":
    test_stamp = generate_provenance_stamp()
    print("Canonical:", test_stamp["canonical_stamp"])
    parsed = parse_provenance_stamp(test_stamp["canonical_stamp"])
    print("Parsed:", parsed)
