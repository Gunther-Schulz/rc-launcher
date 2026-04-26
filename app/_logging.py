"""Shared logging helper.

Single-line prints to stdout with millisecond timestamps and a per-module
prefix. Captured by uvicorn → visible in Coolify's app log panel.
We deliberately do NOT use Python's `logging` module — uvicorn already
configures one and our prints stay out of its way, with simpler control
over flush behavior.
"""
import sys
import time
from typing import Callable


def make_logger(prefix: str) -> Callable[[str], None]:
    def _log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        print(f"[{prefix} {ts}] {msg}", flush=True, file=sys.stdout)
    return _log
