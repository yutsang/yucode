"""Atomic text-file writes for state that must survive an abrupt kill.

A plain ``path.write_text(...)`` truncates the file first and then writes; a
crash, terminal kill, or a stalled write to a network-mapped HOME (common on
corporate Windows roaming profiles) in between leaves a truncated/corrupt
file, and the next load fails with JSONDecodeError — losing the session,
trust store, or todo list outright. Writing to a sibling temp file and
``os.replace``-ing it in is atomic on both POSIX and Windows (same
directory => same filesystem, so the rename can't cross devices).
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding=encoding)
    os.replace(tmp_path, path)
