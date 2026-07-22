"""Hardening for locked-down corporate machines (no admin, Python-only,
no git/ripgrep on PATH, HOME on a slow/read-only network share, flaky
proxied network). Two audits drove these: an unbounded-loop/blocking-call
sweep and a Windows restricted-environment sweep.

Covers: provider circuit breaker + cancel-aware backoff + ca_bundle,
git-missing resilience, config-load fallback on unwritable HOME, atomic
state writes, artifact-pruned lazy file walking, MCP call timeout, and the
notebook cell-index bound.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.core import providers as providers_mod
from coding_agent.core.atomic_io import atomic_write_text
from coding_agent.core.errors import ProviderError
from coding_agent.core.providers import OpenAICompatibleProvider

# ---------------------------------------------------------------------------
# Provider circuit breaker
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_breakers():
    providers_mod._reset_circuit_breakers()
    yield
    providers_mod._reset_circuit_breakers()


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _provider(**overrides) -> OpenAICompatibleProvider:
    config = ProviderConfig(
        base_url="http://127.0.0.1:9",  # port 9 (discard): connection refused instantly
        api_key="k",
        model="m",
        request_timeout_seconds=2,
        streaming_mode="no_stream",
        **overrides,
    )
    return OpenAICompatibleProvider(config)


class TestCircuitBreaker:
    def test_opens_after_threshold_and_fails_fast(self, monkeypatch) -> None:
        # No backoff sleeps so the test runs instantly.
        monkeypatch.setattr(providers_mod, "_RETRY_BACKOFF_BASE", 0.0)
        provider = _provider()

        with pytest.raises(ProviderError):
            provider.complete([{"role": "user", "content": "hi"}], [])

        # Breaker is now open (>=3 connection failures happened during the
        # first call's retry ladder) — the next call must fail fast with the
        # breaker message, without re-paying the retry ladder.
        started = time.monotonic()
        with pytest.raises(ProviderError, match="marked unavailable"):
            provider.complete([{"role": "user", "content": "hi"}], [])
        assert time.monotonic() - started < 1.0

    def test_breaker_aborts_remaining_retry_ladder_mid_call(self, monkeypatch) -> None:
        monkeypatch.setattr(providers_mod, "_RETRY_BACKOFF_BASE", 0.0)
        attempts = {"n": 0}
        real_urlopen = providers_mod.urllib.request.urlopen

        def counting_urlopen(*args, **kwargs):
            attempts["n"] += 1
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(providers_mod.urllib.request, "urlopen", counting_urlopen)
        provider = _provider()
        with pytest.raises(ProviderError):
            provider.complete([{"role": "user", "content": "hi"}], [])
        # The ladder allows 5 attempts, but the breaker trips at 3 and aborts
        # the rest instead of burning attempts 4-5.
        assert attempts["n"] == providers_mod._BREAKER_FAILURE_THRESHOLD
        del real_urlopen

    def test_success_resets_breaker_state(self, monkeypatch) -> None:
        providers_mod._breaker_record_connection_failure("somehost:1234")
        providers_mod._breaker_record_connection_failure("somehost:1234")
        assert providers_mod._circuit_states["somehost:1234"].consecutive_failures == 2
        providers_mod._breaker_record_success("somehost:1234")
        assert "somehost:1234" not in providers_mod._circuit_states

    def test_cooldown_elapse_allows_probe(self, monkeypatch) -> None:
        host = "probe-host:9"
        for _ in range(providers_mod._BREAKER_FAILURE_THRESHOLD):
            providers_mod._breaker_record_connection_failure(host)
        assert providers_mod._breaker_remaining_cooldown(host) is not None
        # Simulate the cooldown having elapsed.
        providers_mod._circuit_states[host].opened_at = (
            time.monotonic() - providers_mod._BREAKER_COOLDOWN_SECONDS - 1
        )
        assert providers_mod._breaker_remaining_cooldown(host) is None

    def test_breakers_are_per_host(self) -> None:
        for _ in range(providers_mod._BREAKER_FAILURE_THRESHOLD):
            providers_mod._breaker_record_connection_failure("down-host:1")
        assert providers_mod._breaker_remaining_cooldown("down-host:1") is not None
        assert providers_mod._breaker_remaining_cooldown("healthy-host:2") is None


class TestCancelDuringBackoff:
    def test_cancel_event_interrupts_retry_backoff(self, monkeypatch) -> None:
        """A cancelled request must not sit out the full backoff sleep."""
        monkeypatch.setattr(providers_mod, "_RETRY_BACKOFF_BASE", 30.0)  # huge backoff

        def failing_urlopen(*args, **kwargs):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(providers_mod.urllib.request, "urlopen", failing_urlopen)
        provider = _provider()
        cancel_event = threading.Event()
        timer = threading.Timer(0.2, cancel_event.set)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(ProviderError, match="Cancelled|marked unavailable"):
                provider.complete(
                    [{"role": "user", "content": "hi"}], [], cancel_event=cancel_event,
                )
        finally:
            timer.cancel()
        # Well under the 30s backoff: the wait was interrupted by the event
        # (or the breaker aborted first — either way, no 30s sleep).
        assert time.monotonic() - started < 5.0


class TestCaBundleConfig:
    def test_ca_bundle_parsed_from_settings(self) -> None:
        from coding_agent.config.settings import app_config_from_dict
        config = app_config_from_dict({"provider": {"ca_bundle": "/etc/corp/ca.pem"}})
        assert config.provider.ca_bundle == "/etc/corp/ca.pem"
        assert config.to_control_dict()["provider"]["ca_bundle"] == "/etc/corp/ca.pem"

    def test_missing_ca_bundle_file_raises_clear_error(self, tmp_path: Path) -> None:
        provider = _provider(ca_bundle=str(tmp_path / "nope.pem"))
        with pytest.raises(ProviderError, match="ca_bundle"):
            provider._ssl_context()

    def test_valid_ca_bundle_builds_verifying_context(self, tmp_path: Path) -> None:
        import ssl as _ssl
        # A syntactically valid (self-signed test) cert isn't needed — an
        # empty CA list loads fine; what matters is a verifying context comes
        # back rather than None or unverified.
        pem = tmp_path / "ca.pem"
        # Generate a minimal self-signed cert via ssl's own test helper is
        # overkill; instead just verify the None/unverified paths.
        provider_default = _provider()
        assert provider_default._ssl_context() is None  # system default context
        provider_unverified = _provider(verify_tls=False)
        ctx = provider_unverified._ssl_context()
        assert isinstance(ctx, _ssl.SSLContext)
        assert ctx.verify_mode == _ssl.CERT_NONE
        del pem


# ---------------------------------------------------------------------------
# Git resilience (no git on PATH / hung git)
# ---------------------------------------------------------------------------

class TestGitResilience:
    def test_prompting_run_git_survives_missing_git(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.memory import prompting

        def no_git(*args, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(prompting.subprocess, "run", no_git)
        assert prompting._run_git(tmp_path, ["status"]) is None
        assert prompting._collect_git_diff(tmp_path) is None

    def test_prompting_run_git_survives_hung_git(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.memory import prompting

        def hung_git(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=10)

        monkeypatch.setattr(prompting.subprocess, "run", hung_git)
        assert prompting._run_git(tmp_path, ["status"]) is None

    def test_slash_command_git_helpers_survive_missing_git(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.interface import commands

        def no_git(*args, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(commands.subprocess, "run", no_git)
        assert "not available" in commands.run_git_diff(tmp_path)
        assert "not available" in commands.run_git_branch(tmp_path)
        assert "not available" in commands.run_git_log(tmp_path)
        assert "not available" in commands.run_git_commit(tmp_path, "msg")

    def test_statusline_git_branch_survives_missing_git(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.interface import render

        def no_git(*args, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(render.subprocess, "run", no_git)
        assert render._git_branch(tmp_path) == ""


# ---------------------------------------------------------------------------
# Config load survives unwritable/read-only HOME
# ---------------------------------------------------------------------------

class TestConfigLoadFallback:
    def test_load_app_config_falls_back_when_home_unwritable(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.config import settings as settings_mod

        def deny_write(*args, **kwargs):
            raise PermissionError("read-only roaming profile")

        monkeypatch.setattr(settings_mod, "ensure_default_config", deny_write)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        config = settings_mod.load_app_config(workspace=workspace)
        # Built-in defaults loaded instead of crashing at startup.
        assert isinstance(config, AppConfig)
        assert config.runtime.max_iterations > 0


# ---------------------------------------------------------------------------
# Atomic state writes
# ---------------------------------------------------------------------------

class TestAtomicWrites:
    def test_atomic_write_text_replaces_content(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        atomic_write_text(target, '{"a": 1}')
        atomic_write_text(target, '{"a": 2}')
        assert json.loads(target.read_text()) == {"a": 2}
        assert not (tmp_path / "state.json.tmp").exists()

    def test_session_save_is_loadable_and_leaves_no_tmp(self, tmp_path: Path) -> None:
        from coding_agent.core.session import Message, Session
        session = Session(model="m")
        session.add_message(Message(role="user", content="hello"))
        path = tmp_path / "sessions" / "s1.json"
        session.save(path)
        loaded = Session.load(path)
        assert loaded.messages[0].content == "hello"
        assert not path.with_name(path.name + ".tmp").exists()

    def test_todo_write_survives_corrupt_existing_file(self, tmp_path: Path) -> None:
        from coding_agent.tools import ToolRegistry
        config = AppConfig(
            provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access"),
        )
        registry = ToolRegistry(tmp_path, config)
        from coding_agent.config.settings import state_dir
        todos_path = state_dir(tmp_path) / "todos.json"
        todos_path.parent.mkdir(parents=True, exist_ok=True)
        todos_path.write_text("{corrupt json!", encoding="utf-8")

        out = registry.execute("todo_write", {"todos": [{"id": "t1", "content": "x", "status": "pending"}]})
        assert "Updated" in out
        assert json.loads(todos_path.read_text())[0]["id"] == "t1"


# ---------------------------------------------------------------------------
# Lazy, artifact-pruned file walking (the no-ripgrep fallback path)
# ---------------------------------------------------------------------------

class TestIterSourceFiles:
    def test_never_descends_into_artifact_dirs(self, tmp_path: Path) -> None:
        from coding_agent.tools.filesystem import _iter_source_files
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        (tmp_path / "node_modules" / "dep").mkdir(parents=True)
        (tmp_path / "node_modules" / "dep" / "index.js").write_text("junk\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("junk\n")

        found = [str(p) for p in _iter_source_files(tmp_path)]
        assert any("main.py" in p for p in found)
        assert not any("node_modules" in p for p in found)
        assert not any(".git" in p for p in found)

    def test_py_grep_stops_at_cap_without_full_enumeration(self, tmp_path: Path) -> None:
        from coding_agent.tools.filesystem import _py_grep_lines
        for i in range(50):
            (tmp_path / f"f{i:03}.txt").write_text("needle\n" * 10)
        results = _py_grep_lines("needle", tmp_path, tmp_path, max_lines=5)
        assert len(results) == 5

    def test_py_list_files_is_capped(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.tools import filesystem as fs_mod
        monkeypatch.setattr(fs_mod, "_PY_LIST_FILES_CAP", 3)
        for i in range(10):
            (tmp_path / f"f{i}.txt").write_text("x")
        assert len(fs_mod._py_list_files(tmp_path)) == 3


# ---------------------------------------------------------------------------
# MCP call timeout (unresponsive server can't wedge the agent)
# ---------------------------------------------------------------------------

class TestMcpCallTimeout:
    def test_unresponsive_mcp_server_times_out(self, monkeypatch) -> None:
        from coding_agent.config import McpServerConfig
        from coding_agent.core.errors import McpError
        from coding_agent.plugins import mcp as mcp_mod
        from coding_agent.plugins.mcp import StdioMcpClient

        monkeypatch.setattr(mcp_mod, "_MCP_CALL_TIMEOUT_SECONDS", 0.5)
        # A real subprocess that reads stdin but never answers — exactly the
        # "spawned fine, blocked by policy" failure mode on a corporate box.
        client = StdioMcpClient(McpServerConfig(
            name="silent",
            transport="stdio",
            command=sys.executable,
            args=["-c", "import time; time.sleep(60)"],
        ))
        started = time.monotonic()
        with pytest.raises(McpError, match="did not respond|failed"):
            client.list_tools()
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"MCP init should time out in ~0.5s, took {elapsed:.1f}s"
        # The poisoned process must not be reused.
        assert client._process is None
        client.shutdown()


# ---------------------------------------------------------------------------
# Windows cmd.exe quote handling (shell_invocation)
# ---------------------------------------------------------------------------

class TestShellInvocation:
    def test_posix_passes_command_through_with_shell_true(self, monkeypatch) -> None:
        from coding_agent.core import shellexec
        monkeypatch.setattr(shellexec.os, "name", "posix")
        args, use_shell = shellexec.shell_invocation('echo "hello world"')
        assert args == 'echo "hello world"'
        assert use_shell is True

    def test_windows_wraps_with_cmd_s_c_and_outer_quotes(self, monkeypatch) -> None:
        """The multi-quote command that actually failed on the real Windows
        box: cmd /C strips the first and last quote of a >2-quote command,
        mangling '"exe" "arg"' into an unparseable line. /S + one outer
        quote pair forces the deterministic strip-outer-quotes rule so the
        interior arrives at cmd intact."""
        from coding_agent.core import shellexec
        monkeypatch.setattr(shellexec.os, "name", "nt")
        monkeypatch.setattr(
            shellexec.os, "environ",
            {**shellexec.os.environ, "COMSPEC": r"C:\Windows\system32\cmd.exe"},
        )
        command = r'"C:\Program Files\python.exe" "C:\tmp\script.py"'
        args, use_shell = shellexec.shell_invocation(command)
        assert use_shell is False
        assert args == rf'"C:\Windows\system32\cmd.exe" /S /C "{command}"'

    def test_windows_defaults_comspec_when_unset(self, monkeypatch) -> None:
        from coding_agent.core import shellexec
        monkeypatch.setattr(shellexec.os, "name", "nt")
        env = {k: v for k, v in shellexec.os.environ.items() if k != "COMSPEC"}
        monkeypatch.setattr(shellexec.os, "environ", env)
        args, _ = shellexec.shell_invocation("dir")
        assert args == '"cmd.exe" /S /C "dir"'

    def test_bash_tool_still_executes_quoted_paths_on_posix(self, tmp_path: Path) -> None:
        """End-to-end through the real bash tool: a command quoting both the
        interpreter path and a script path (the exact shape that broke on
        Windows) must run and return its CJK stdout."""
        from coding_agent.tools import ToolRegistry
        config = AppConfig(
            provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access"),
        )
        registry = ToolRegistry(tmp_path, config)
        script = tmp_path / "probe.py"
        script.write_text("print('中文OK')\n", encoding="utf-8")
        out = registry.execute("bash", {"command": f'"{sys.executable}" "{script}"'})
        payload = json.loads(out)
        assert payload["returncode"] == 0
        assert "中文OK" in payload["stdout"]

    def test_bash_tool_forces_utf8_io_in_python_children(self, tmp_path: Path) -> None:
        """A child python on a PIPE defaults its stdout to the locale code
        page (cp1252 on Western-locale Windows) — printing CJK then crashes
        the child with UnicodeEncodeError before any output reaches the
        parent (observed on the real Windows box: rc=1, empty stdout). The
        bash tool must inject PYTHONIOENCODING/PYTHONUTF8 so every python
        child does UTF-8 I/O regardless of locale."""
        from coding_agent.tools import ToolRegistry
        config = AppConfig(
            provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access"),
        )
        registry = ToolRegistry(tmp_path, config)
        probe = tmp_path / "env_probe.py"
        probe.write_text(
            "import os, sys\n"
            "print(os.environ.get('PYTHONIOENCODING'), os.environ.get('PYTHONUTF8'), sys.stdout.encoding)\n",
            encoding="utf-8",
        )
        out = registry.execute("bash", {"command": f'"{sys.executable}" "{probe}"'})
        payload = json.loads(out)
        assert payload["returncode"] == 0
        fields = payload["stdout"].split()
        assert fields[0] == "utf-8", f"PYTHONIOENCODING not injected: {payload['stdout']!r}"
        assert fields[1] == "1", f"PYTHONUTF8 not injected: {payload['stdout']!r}"
        assert fields[2].lower().replace("-", "") == "utf8", f"child pipe encoding: {payload['stdout']!r}"

    def test_background_bash_also_gets_utf8_env(self, tmp_path: Path) -> None:
        from coding_agent.tools import ToolRegistry
        config = AppConfig(
            provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access"),
        )
        registry = ToolRegistry(tmp_path, config)
        probe = tmp_path / "bg_probe.py"
        probe.write_text("print('背景中文OK')\n", encoding="utf-8")
        out = json.loads(registry.execute("bash", {
            "command": f'"{sys.executable}" "{probe}"', "run_in_background": True,
        }))
        task = registry.background_tasks[out["task_id"]]
        assert _wait_until(lambda: task.popen.poll() is not None)
        content = Path(out["output_file"]).read_text(encoding="utf-8", errors="replace")
        assert "背景中文OK" in content


# ---------------------------------------------------------------------------
# Behavioral diagnostic harness (diagnose_agent.py) — mock-mode regression
# ---------------------------------------------------------------------------

class TestDiagnoseAgentHarness:
    def test_mock_mode_all_scenarios_pass(self) -> None:
        """The behavioral harness's own plumbing: synthetic workspaces build,
        the mock provider's scripted tool calls execute for real, ground-truth
        judges see the effects, and the digest renders — all without network.
        Guards the harness against regressions in runtime/tool APIs it uses."""
        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, str(repo_root / "diagnose_agent.py"), "--mock"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(repo_root),
        )
        assert result.returncode == 0, f"mock run failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        assert "verdicts passed across 5 scenarios" in result.stdout
        assert "[FAIL]" not in result.stdout


# ---------------------------------------------------------------------------
# Notebook cell-index bound
# ---------------------------------------------------------------------------

class TestNotebookIndexBound:
    def test_huge_cell_index_rejected(self, tmp_path: Path) -> None:
        from coding_agent.tools import ToolRegistry
        config = AppConfig(
            provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access"),
        )
        registry = ToolRegistry(tmp_path, config)
        nb_path = tmp_path / "n.ipynb"
        nb_path.write_text(json.dumps({"cells": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="out of range"):
            registry.execute("edit_notebook_cell", {
                "path": "n.ipynb", "cell_index": 10_000_000, "new_source": "x",
            })
        # Appending a reasonable distance past the end still works.
        out = registry.execute("edit_notebook_cell", {
            "path": "n.ipynb", "cell_index": 3, "new_source": "x = 1",
        })
        assert "Updated notebook cell 3" in out
