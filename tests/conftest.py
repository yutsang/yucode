"""Keep the test suite out of the user's real home directory.

`state_dir()` resolves to `~/.yucode/projects/<workspace_key>/`, and nothing
redirected it during tests, so every run that touched a workspace left audit
logs, spilled tool output, session files and metrics snapshots in the user's
home. Measured on this machine: 240 workspace directories accumulated over
months, then 1,249 after a single day's work once metrics started being
written per turn. That is not only mess -- it makes the audit trail useless,
because real runs and test runs land in the same tree with no way to tell them
apart.

`workspace_key()` is a one-way hash of the path with no reverse index, so the
directories cannot even be attributed after the fact.

Two test modules were already monkeypatching `_HOME_YUCODE` by hand; this makes
it universal rather than something each new test has to remember.
"""
from __future__ import annotations

import pytest

from coding_agent.config import settings as _settings
from coding_agent.security import env_scrub as _env_scrub


@pytest.fixture(autouse=True)
def _isolate_yucode_home(tmp_path_factory, monkeypatch):
    """Point the home-based state directory at a per-test temporary path.

    Autouse and unconditional: an opt-in fixture only protects the tests that
    remember to ask for it, and the ones that forget are exactly the ones that
    pollute. Tests that need to assert on the real layout can still read
    `settings._HOME_YUCODE` -- it is a valid path, just a disposable one.
    """
    home = tmp_path_factory.mktemp("yucode_home")
    # Both mechanisms are needed. The attribute covers in-process code; the
    # environment variable covers the tests that spawn a subprocess, which
    # re-imports settings and recomputes the constant from Path.home(). Fixing
    # only the attribute left those tests still writing to the real home --
    # eight directories per run, which is how this was found.
    monkeypatch.setattr(_settings, "_HOME_YUCODE", home / ".yucode")
    monkeypatch.setenv("YUCODE_HOME", str(home / ".yucode"))
    return home


@pytest.fixture(autouse=True)
def _restore_env_scrub_policy():
    """Undo the process-wide scrub policy any test leaves behind.

    `AgentRuntime.__init__` installs `env_scrub._default_policy` from its
    config, so any test that builds a runtime with non-default `shell_env_*`
    settings silently changes how every later test scrubs child environments.
    Only one test was restoring it by hand, which makes the rest
    order-dependent -- the failure mode being a suite that passes alone and
    fails in a different order, or vice versa.

    Paired with the home isolation above rather than folded into it: they
    guard the same class of problem (global state escaping a test) but have
    nothing else to do with each other.
    """
    previous = _env_scrub.default_policy()
    try:
        yield
    finally:
        _env_scrub.set_default_policy(previous)
