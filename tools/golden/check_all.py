#!/usr/bin/env python3
"""Re-run every capture and diff it against what is committed.

The pytest wrappers that used to do this went with the Python tree, and
pytest is not part of the TypeScript toolchain, so this is the one command
that keeps the goldens honest:

    python tools/golden/check_all.py

Exit 0 when everything matches, 1 when any check fails. Needs the Python package, which
lives in the untracked `_legacy_python/` backup unpacked from the
`python-final` tag; the scripts find it there on their own.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

CHECKS = [
    ("harness baselines", ["capture_harness_baselines.py", "--check"]),
    ("pure functions", ["capture_pure_functions.py", "--check"]),
    ("office goldens", ["capture_office_goldens.py", "check"]),
]


def main() -> int:
    if not (REPO_ROOT / "_legacy_python" / "coding_agent").is_dir():
        print(
            "_legacy_python/coding_agent is missing. Unpack it from the tag:\n"
            "  mkdir -p _legacy_python && git archive python-final | tar -x -C _legacy_python",
            file=sys.stderr,
        )
        return 1
    failed = []
    for label, argv in CHECKS:
        result = subprocess.run(
            [sys.executable, str(HERE / argv[0]), *argv[1:]],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        status = "ok" if result.returncode == 0 else "FAIL"
        print(f"[{status}] {label}")
        if result.returncode != 0:
            failed.append(label)
            print(result.stdout.rstrip())
            print(result.stderr.rstrip(), file=sys.stderr)
    if failed:
        print(f"\n{len(failed)} of {len(CHECKS)} failed: {', '.join(failed)}")
        return 1
    print(f"\nall {len(CHECKS)} match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
