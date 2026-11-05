"""Keep the office goldens honest on the Python side.

Without this, `tests/golden/office/**` is a set of committed files that nothing
checks — and a stale oracle is worse than none, because the TypeScript port
would be measured against behaviour the Python no longer has.

The capture script's own `check` subcommand does the work: it rebuilds
everything into a temp directory and diffs. This is the pytest wrapper.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = REPO_ROOT / "tests" / "golden" / "office"
SCRIPT = REPO_ROOT / "tools" / "golden" / "capture_office_goldens.py"

pytest.importorskip("openpyxl")
pytest.importorskip("pptx")
pytest.importorskip("docx")
pytest.importorskip("pdfplumber")
pytest.importorskip("lxml")


def test_goldens_exist() -> None:
    assert (GOLDEN / "cases.json").is_file()
    assert (GOLDEN / "manifest.json").is_file()
    assert (GOLDEN / "fixtures").is_dir()


def test_every_case_has_an_outcome_and_a_reason() -> None:
    """A case with no note is a case nobody can maintain."""
    data = json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))
    assert data["case_count"] == len(data["cases"])
    for case in data["cases"]:
        assert case["outcome"] in ("ok", "error", "skipped"), case["id"]
        if case["outcome"] == "ok":
            assert case["output"] is not None, case["id"]
        elif case["outcome"] == "error":
            assert case["error"]["type"], case["id"]


def test_no_absolute_paths_leaked() -> None:
    """Every machine-specific path must have been tokenised.

    A leaked temp path would make the golden unusable on any other machine and
    would churn on every run here.
    """
    text = (GOLDEN / "cases.json").read_text(encoding="utf-8")
    for marker in ("/private/var/folders", "/Users/", "/home/", "C:\\\\Users"):
        assert marker not in text, f"{marker} leaked into cases.json"


def test_capture_is_reproducible() -> None:
    """Rebuild everything and diff. This is the check that actually matters."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "office goldens drifted:\n" + result.stdout + result.stderr
    )
