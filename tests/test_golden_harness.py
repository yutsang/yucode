"""Keep the harness baselines honest on the Python side.

Both capture scripts are byte-stable and carry a --check that recaptures into
a temp directory and diffs. This is the pytest wrapper, so a change that
alters a baseline fails the suite instead of landing silently.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "tools" / "golden"


@pytest.mark.parametrize("script", ["capture_harness_baselines.py", "capture_pure_functions.py"])
def test_baseline_is_reproducible(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"{script} drifted:\n{result.stdout}{result.stderr}"
