#!/usr/bin/env python3
"""
atomic_writer.py - Atomic Writing and Concurrency Protection for Ledgers.
Transactional collision-free persistence via POSIX atomic file replacement.

Guarantees that no critical JSON or JSONL file (session_state.json, trades_audit.jsonl)
gets corrupted when multiple subagents, hooks, or loops attempt concurrent reads or writes.
Utilizes 'write-to-temp-then-atomic-replace' at OS kernel level (POSIX os.replace).
"""

import os
import sys
import json
import tempfile
import time
from typing import Any, Dict, Optional

def atomic_write_json(filepath: str, data: Any, indent: int = 2, separators: Optional[tuple] = None) -> bool:
    """
    Writes data to a JSON file in a 100% atomic manner.
    Creates a temporary file in the same directory and executes os.replace.
    separators: json.dump separators (None = the json default for the indent).
    """
    filepath = os.path.abspath(filepath)
    dirname = os.path.dirname(filepath)
    os.makedirs(dirname, exist_ok=True)
    
    # Create temporary file in the SAME directory to guarantee identical filesystem/mount
    prefix = f".{os.path.basename(filepath)}.tmp_"
    try:
        with tempfile.NamedTemporaryFile("w", dir=dirname, prefix=prefix, delete=False, encoding="utf-8") as tf:
            json.dump(data, tf, indent=indent, separators=separators, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
            temp_name = tf.name
            
        # Atomic replacement at OS kernel level
        os.replace(temp_name, filepath)
        return True
    except Exception as e:
        if 'temp_name' in locals() and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except Exception:
                pass
        raise IOError(f"Atomic write failure for {filepath}: {e}")


def path_inside_dir(path: str, directory: str) -> bool:
    """True when path is a file path strictly inside directory, lexically (os.path.abspath) and after resolving
    symlinks (os.path.realpath), so a symlinked directory cannot lead out (issue #191: offline CLI --out / --output
    restricted to logs/). Write such files with atomic_write_json (os.replace never writes through a hard link)."""
    root, target = os.path.abspath(directory), os.path.abspath(path)
    real_root, real_target = os.path.realpath(root), os.path.realpath(target)
    try:
        lexical = os.path.commonpath([root, target]) == root and target != root
        resolved = os.path.commonpath([real_root, real_target]) == real_root and real_target != real_root
    except ValueError:
        return False
    return lexical and resolved


def same_file(a: str, b: str) -> bool:
    """True when a and b name the same file: equal realpaths, or os.path.samefile (a hard link) when both exist."""
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    try:
        return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False

def atomic_append_jsonl(filepath: str, record: Dict[str, Any]) -> bool:
    """
    Appends a record to a JSON Lines file safely and consistently.
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
    Reads a JSON file with retries to mitigate transient read/write race conditions.
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
