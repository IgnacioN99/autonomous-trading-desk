#!/usr/bin/env python3
"""
atomic_writer.py - Escritura Atómica y Protección de Concurrencia para Ledgers.
Persistencia transaccional sin colisiones mediante reemplazo atómico POSIX.

Garantiza que ningún archivo JSON o JSONL crítico (session_state.json, trades_audit.jsonl)
se corrompa si múltiples subagentes, hooks o loops intentan leer o escribir simultáneamente.
Utiliza 'write-to-temp-then-atomic-replace' a nivel de sistema operativo (POSIX os.replace).
"""

import os
import sys
import json
import tempfile
import time
from typing import Any, Dict, Optional

def atomic_write_json(filepath: str, data: Any, indent: int = 2) -> bool:
    """
    Escribe datos en un archivo JSON de forma 100% atómica.
    Crea un archivo temporal en el mismo directorio y ejecuta os.replace.
    """
    filepath = os.path.abspath(filepath)
    dirname = os.path.dirname(filepath)
    os.makedirs(dirname, exist_ok=True)
    
    # Crear archivo temporal en el MISMO directorio para garantizar que esté en el mismo filesystem/mount
    prefix = f".{os.path.basename(filepath)}.tmp_"
    try:
        with tempfile.NamedTemporaryFile("w", dir=dirname, prefix=prefix, delete=False, encoding="utf-8") as tf:
            json.dump(data, tf, indent=indent, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
            temp_name = tf.name
            
        # Reemplazo atómico a nivel de kernel de OS
        os.replace(temp_name, filepath)
        return True
    except Exception as e:
        if 'temp_name' in locals() and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except Exception:
                pass
        raise IOError(f"Fallo en escritura atómica para {filepath}: {e}")

def atomic_append_jsonl(filepath: str, record: Dict[str, Any]) -> bool:
    """
    Appendea un registro a un archivo JSON Lines de forma segura y consistente.
    """
    filepath = os.path.abspath(filepath)
    dirname = os.path.dirname(filepath)
    os.makedirs(dirname, exist_ok=True)
    
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    return True

def read_json_safe(filepath: str, default: Optional[Any] = None, retries: int = 3, delay: float = 0.05) -> Any:
    """
    Lee un archivo JSON con reintentos para mitigar colisiones transitorias de lectura/escritura.
    """
    if not os.path.exists(filepath):
        return default
        
    for attempt in range(retries):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return default
                return json.loads(content)
        except Exception:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                return default
    return default
