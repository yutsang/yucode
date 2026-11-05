# Re-running the Python single-loop eval

Use this procedure before comparing WP12 with the Python implementation. The
committed `eval.python-final.json` used orchestration mode `auto`, including
the coordinator on 11 of 12 prompts. Preserve that report; a single-loop run
gets its own output.

The eval makes live provider requests and sends workspace content to the
configured provider. Run it in an environment with working provider access.
The offline golden checks do not need that access.

## Prepare the comparison

Use a Python interpreter with the legacy package's dependencies installed.
Unpack `python-final` into `_legacy_python/` if needed, and run the maintained
scripts under `tools/golden/`, not the stale copy in the backup.

Create an independent clone of the eval workspace with `git clone
--no-hardlinks --no-checkout <source> <snapshot>`, then detach it at the full
SHA recorded in the old report. The historical SHA is
`99918bef7f02032539201cd0b08ccd0e32eedf78`. Verify `rev-parse HEAD` and a clean
`status --porcelain`. Do not reset the owner's workspace.

Compare suite IDs, prompts and `expected` dictionaries with the old report,
and record the current suite SHA-256. The suite verified on 2026-10-26 has 12
prompts and hash
`64a1a0a5ef42c1673ef8d2f3e191f3b3ff94580ecb1f4e26860a8161a455dde3`.

## Run with a fixed config and isolated state

The legacy CLI has no orchestration-mode flag. Its eval module imports
`load_app_config` directly, so the following wrapper supplies one frozen
config with `single` mode for both the report header and each prompt. It keeps
credentials in memory. Set the three paths before running this from the
repository root; keep the output outside the repository until reviewed.

```python
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

repo = Path.cwd()
source_workspace = Path("/path/to/original/eval-workspace")
snapshot = Path("/path/to/clean/historical-snapshot")
output = Path("/path/outside/repository/eval.python-single.json")
assert not output.exists(), "Choose a new output file"

sys.path.insert(0, str(repo / "_legacy_python"))
from coding_agent.config import settings

# Load the source workspace's resolved config before isolating runtime state.
# Explicitly record that instruction paths, hooks, MCP and plugin settings in
# this config are still in force; review them before running a live eval.
config = settings.load_app_config(workspace=source_workspace)
config = replace(config, runtime=replace(config.runtime, orchestration_mode="single"))
assert config.runtime.orchestration_mode == "single"

isolated_home = Path(tempfile.mkdtemp(prefix="yucode-single-state-"))
os.environ["YUCODE_HOME"] = str(isolated_home)
settings._HOME_YUCODE = isolated_home

from coding_agent.interface import eval as legacy_eval
legacy_eval.load_app_config = lambda *args, **kwargs: config
raise SystemExit(legacy_eval.run([
    "--suite", str(repo / "tests/eval/prompts.yaml"),
    "--workspace", str(snapshot),
    "--out", str(output),
    "--label", "python-single",
]))
```

This wrapper uses the legacy module's internal state-directory constant;
verify it against the frozen tag if replacing that implementation. Check that
the snapshot has no legacy `.yucode` directory before running: `state_dir`
can otherwise select it as a migration fallback. A fresh state directory
avoids resuming earlier runs; the snapshot path itself still changes prompt
context and the workspace hash, which must be recorded as a comparison limit.

Retain the exit code and raw report even when mechanical verdicts fail. Check
that the report says `single`, every `used_coordinator` is false, all suite IDs
occur exactly once, the workspace SHA matches, and no prompt has a transport
error before calling it a completed baseline.

## Record what the comparison can establish

Record the full workspace SHA, suite hash, Python tag, capture-script commit,
model name, non-secret resolved config and deliberate overrides. Keep keys,
extra authentication headers and private hostnames out of repository files.
The historical report pins only part of its config and does not identify the
provider's deployed model revision, so a new run cannot isolate mode as the
only possible cause of a difference.

Tool budgets, file reads and artifact mentions are mechanical checks. The
judge does not establish answer correctness. Review the final answers and the
suite's prose expectations, and document that review separately. Inspect raw
outputs for private identifiers and paths before committing a scrubbed copy.
