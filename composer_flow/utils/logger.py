"""Central logging setup.

Writes rotating log files to %LOCALAPPDATA%/ComposerFlow/logs and mirrors to
stderr during development. Every CLI command, DAG transition, status, duration,
error and retry is logged through this module by the services layer.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys

from composer_flow.config import log_dir

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("composerflow")
    root.setLevel(level)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir() / "composerflow.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    if sys.stderr is not None:  # absent in a windowed (no-console) .exe
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(stream)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"composerflow.{name}")
