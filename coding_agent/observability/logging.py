"""Structured logging setup for the agent runtime.

Supports two formats:
  - ``text``: human-readable log lines (default)
  - ``json``: machine-parseable JSON lines for ingestion pipelines
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.settings import LogFormat, LogLevel


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        return json.dumps(entry, ensure_ascii=False)


_TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def configure_logging(
    level: LogLevel = "INFO",
    fmt: LogFormat = "text",
    log_file: str = "",
) -> None:
    """Configure the ``yucode`` logger hierarchy."""
    root = logging.getLogger("yucode")
    root.setLevel(_LEVEL_MAP.get(level.upper(), logging.INFO))

    root.handlers.clear()

    if fmt == "json":
        formatter: logging.Formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(_TEXT_FORMAT)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
