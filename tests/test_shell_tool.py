"""bash tool must decode subprocess output as UTF-8, not the OS locale
default. On a Western-locale Windows box, Python's subprocess text=True
(without an explicit encoding) decodes as cp1252, which crashes with a
UnicodeDecodeError on any non-ASCII output (e.g. Chinese account names/
remarks in an FDD databook) instead of returning it. Found via a real
crash loop while running the fdd-commentary skill on a Chinese-language
databook: 'Exception in thread ... cp1252.py ... UnicodeDecodeError'.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_agent.config import AppConfig, McpServerConfig, ProviderConfig, RuntimeOptions
from coding_agent.core.runtime import AgentRuntime
from coding_agent.hooks import HookConfig, HookRunner
from coding_agent.plugins import PluginTool
from coding_agent.plugins.mcp import StdioMcpClient
from coding_agent.security import env_scrub
from coding_agent.tools.shell import _child_env


@pytest.fixture()
def runtime(tmp_path: Path) -> AgentRuntime:
    config = AppConfig(
        provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )
    return AgentRuntime(tmp_path, config)


class TestBashSubprocessEncoding:
    """White-box: assert the exact subprocess.run kwargs, so this fails
    against the old code regardless of which locale the test happens to run
    under (this Mac's default locale is already UTF-8, so a purely
    behavioural test wouldn't reproduce the Windows cp1252 crash)."""

    def test_no_sandbox_path_passes_utf8_encoding(self, runtime: AgentRuntime) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "ok"
            mock_run.return_value.stderr = ""
            runtime._execute_tool("bash", json.dumps({"command": "echo hi"}))
            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs.get("encoding") == "utf-8"
            assert kwargs.get("errors") == "replace"
            assert kwargs.get("text") is True


def test_bash_handles_utf8_output_end_to_end(runtime: AgentRuntime) -> None:
    """Functional check: a command that prints non-ASCII text must come back
    intact (or at least not crash), not raise UnicodeDecodeError."""
    python = sys.executable
    output = runtime._execute_tool(
        "bash",
        json.dumps({"command": f'{python} -c "print(\'示範城區\')"'}),
    )
    payload = json.loads(output)
    assert payload["returncode"] == 0
    assert "示範城區" in payload["stdout"]


def _names(env: dict[str, str]) -> set[str]:
    """Env var names, lowercased — Windows uppercases what os.environ stores."""
    return {name.lower() for name in env}


class TestChildEnvSecretFiltering:
    """The bash tool used to hand **os.environ to every child, so a single
    injected `env`/`curl` (from a malicious page or file read into context)
    walked off with the provider API key. Names are filtered now; the field
    risk is over-filtering, hence the proxy/TLS and allowlist cases."""

    def test_credential_looking_vars_are_withheld(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YUCODE_API_KEY", "sk-must-not-leak")
        monkeypatch.setenv("BRAVE_API_KEY", "brave-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        monkeypatch.setenv("DB_PASSWORD", "hunter2")
        monkeypatch.setenv("SVC_CREDENTIAL", "cred")
        env = _child_env(RuntimeOptions())
        assert _names(env).isdisjoint({
            "yucode_api_key", "brave_api_key", "github_token",
            "aws_secret_access_key", "db_password", "svc_credential",
        })
        assert "sk-must-not-leak" not in env.values()

    def test_filtering_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("openai_api_key", "lowercase-secret")
        monkeypatch.setenv("MixedCase_Token", "mixed-secret")
        env = _child_env(RuntimeOptions())
        assert _names(env).isdisjoint({"openai_api_key", "mixedcase_token"})

    def test_proxy_and_tls_vars_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Locked-down corporate boxes route pip/git/curl through a proxy with
        a re-signing CA; losing these breaks tooling with nobody on site."""
        for name in (
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE",
            "SSL_CERT_FILE", "CURL_CA_BUNDLE", "PIP_INDEX_URL",
            "PIP_TRUSTED_HOST", "GIT_SSL_CAINFO", "GIT_EXEC_PATH",
        ):
            monkeypatch.setenv(name, "value")
        env = _child_env(RuntimeOptions())
        assert _names(env) >= {
            "http_proxy", "https_proxy", "no_proxy", "requests_ca_bundle",
            "ssl_cert_file", "curl_ca_bundle", "pip_index_url",
            "pip_trusted_host", "git_ssl_cainfo", "git_exec_path",
        }

    def test_preserve_list_beats_an_overbroad_user_pattern(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
        env = _child_env(RuntimeOptions(shell_env_exclude_patterns=["*PROXY*"]))
        assert "https_proxy" in _names(env)

    def test_extra_exclude_patterns_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INTERNAL_HOSTNAME", "box-1")
        env = _child_env(RuntimeOptions(shell_env_exclude_patterns=["INTERNAL_*"]))
        assert "internal_hostname" not in _names(env)

    def test_allowlist_readmits_a_false_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The on-site escape hatch: a variable the defaults wrongly caught."""
        monkeypatch.setenv("JAVA_KEYSTORE_PATH", "C:/certs/store.jks")
        assert "java_keystore_path" not in _names(_child_env(RuntimeOptions()))
        env = _child_env(RuntimeOptions(shell_env_allowlist=["java_keystore_path"]))
        assert "java_keystore_path" in _names(env)

    def test_filtering_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YUCODE_API_KEY", "sk-opt-out")
        env = _child_env(RuntimeOptions(shell_env_filter_secrets=False))
        assert "yucode_api_key" in _names(env)

    def test_utf8_forcing_still_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YUCODE_API_KEY", "sk-must-not-leak")
        env = _child_env(RuntimeOptions())
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"

    def test_debug_log_records_names_never_values(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("YUCODE_API_KEY", "sk-must-not-leak")
        # Logger moved to security.env_scrub when the filter was shared with
        # the hook / plugin / MCP child-process paths.
        with caplog.at_level(logging.DEBUG, logger="yucode.security.env"):
            _child_env(RuntimeOptions())
        assert "YUCODE_API_KEY" in caplog.text
        assert "sk-must-not-leak" not in caplog.text


class TestDefaultPatternGaps:
    """Names an adversarial review found missing from SECRET_ENV_PATTERNS.
    Each case pairs the withheld variable with the innocent neighbour a naive
    pattern would have taken down with it -- that friendly fire is the whole
    reason these are anchored rather than blanket globs."""

    def test_mysql_pwd_withheld_but_pwd_and_oldpwd_survive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`*PWD*` would eat PWD/OLDPWD -- the working directory, not a
        secret, and removing it breaks anything resolving a relative path."""
        monkeypatch.setenv("MYSQL_PWD", "hunter2")
        monkeypatch.setenv("PWD", "/home/dev/project")
        monkeypatch.setenv("OLDPWD", "/home/dev")
        names = _names(_child_env(RuntimeOptions()))
        assert "mysql_pwd" not in names
        assert {"pwd", "oldpwd"} <= names

    def test_auth_vars_withheld_but_git_author_identity_survives(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`*AUTH*` also matches the AUTHOR family; dropping those silently
        rewrites commit authorship to whatever git falls back to."""
        monkeypatch.setenv("AUTHORIZATION", "Basic abc123")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
        monkeypatch.setenv("DOCKER_AUTH_CONFIG", "{}")
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Dev")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "dev@example.com")
        monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-08-25T00:00:00")
        names = _names(_child_env(RuntimeOptions()))
        assert names.isdisjoint({"authorization", "ssh_auth_sock", "docker_auth_config"})
        assert {"git_author_name", "git_author_email", "git_author_date"} <= names

    def test_git_author_preserve_entries_cannot_readmit_a_secret(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The preserve list beats *every* pattern, so those entries are exact
        names -- a `*AUTHOR*` glob there would have re-admitted this."""
        monkeypatch.setenv("GIT_AUTHOR_API_KEY", "sk-must-not-leak")
        assert "git_author_api_key" not in _names(_child_env(RuntimeOptions()))

    def test_database_url_withheld_but_other_database_vars_survive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A connection string carries the password inline
        (postgres://user:pw@host/db); the sibling settings carry nothing."""
        monkeypatch.setenv("DATABASE_URL", "postgres://u:pw@host/db")
        monkeypatch.setenv("STAGING_DATABASE_URL", "postgres://u:pw@host/db2")
        monkeypatch.setenv("DATABASE_HOST", "db.internal")
        monkeypatch.setenv("DATABASE_NAME", "app")
        monkeypatch.setenv("DATABASE_PORT", "5432")
        names = _names(_child_env(RuntimeOptions()))
        assert names.isdisjoint({"database_url", "staging_database_url"})
        assert {"database_host", "database_name", "database_port"} <= names


@pytest.fixture()
def scrub_policy() -> Iterator[Callable[[RuntimeOptions], None]]:
    """Install the process-wide scrub policy for one test, then restore it."""
    original = env_scrub.default_policy()

    def install(options: RuntimeOptions) -> None:
        env_scrub.set_default_policy(env_scrub.policy_from_runtime(options))

    try:
        yield install
    finally:
        env_scrub.set_default_policy(original)


class TestPolicyReachesEverySpawnPoint:
    """The bash tool passes runtime.shell_env_allowlist down; the hook, plugin
    and MCP spawn points called scrub_secrets() with no settings at all. On
    site that meant a user hitting a false positive, adding the variable to
    runtime.shell_env_allowlist exactly as documented, and finding bash fixed
    while hooks and MCP servers still could not see it -- with no error
    anywhere saying why.

    White-box (assert the env handed to the spawn call) for the same reason
    TestBashSubprocessEncoding is: it pins the wiring on every platform."""

    ALLOWED = "ARTIFACTORY_API_KEY"

    def _options(self) -> RuntimeOptions:
        return RuntimeOptions(shell_env_allowlist=[self.ALLOWED])

    def test_hook_child_env_honours_the_allowlist(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scrub_policy: Callable[[RuntimeOptions], None],
    ) -> None:
        monkeypatch.setenv(self.ALLOWED, "artifactory-token")
        monkeypatch.setenv("YUCODE_API_KEY", "sk-must-not-leak")
        scrub_policy(self._options())
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = b""
            mock_run.return_value.stderr = b""
            HookRunner(HookConfig(pre_tool_use=["true"])).run_pre_tool_use("Bash", "{}")
        _, kwargs = mock_run.call_args
        child_env = kwargs["env"]
        assert child_env[self.ALLOWED] == "artifactory-token"
        assert "YUCODE_API_KEY" not in child_env

    def test_hook_child_actually_receives_the_documented_hook_vars(
        self, scrub_policy: Callable[[RuntimeOptions], None],
    ) -> None:
        """The module docstring and README promise HOOK_EVENT/HOOK_TOOL_NAME
        for every hook invocation; nothing asserted they arrived."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = b""
            mock_run.return_value.stderr = b""
            HookRunner(HookConfig(pre_tool_use=["true"])).run_pre_tool_use("Bash", '{"a":1}')
        _, kwargs = mock_run.call_args
        child_env = kwargs["env"]
        assert child_env["HOOK_EVENT"] == "PreToolUse"
        assert child_env["HOOK_TOOL_NAME"] == "Bash"
        assert child_env["HOOK_TOOL_INPUT"] == '{"a":1}'
        assert child_env["HOOK_TOOL_IS_ERROR"] == "0"

    def test_plugin_tool_child_env_honours_the_allowlist(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        scrub_policy: Callable[[RuntimeOptions], None],
    ) -> None:
        monkeypatch.setenv(self.ALLOWED, "artifactory-token")
        monkeypatch.setenv("YUCODE_API_KEY", "sk-must-not-leak")
        scrub_policy(self._options())
        tool_path = tmp_path / "tools" / "fmt"
        tool_path.parent.mkdir(parents=True)
        tool_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool = PluginTool(
            name="fmt", description="", input_schema={}, permission="read-only",
            plugin_name="p", plugin_dir=tmp_path,
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = b"{}"
            mock_run.return_value.stderr = b""
            tool.execute({})
        _, kwargs = mock_run.call_args
        child_env = kwargs["env"]
        assert child_env[self.ALLOWED] == "artifactory-token"
        assert "YUCODE_API_KEY" not in child_env

    def test_mcp_server_child_env_honours_the_allowlist(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scrub_policy: Callable[[RuntimeOptions], None],
    ) -> None:
        monkeypatch.setenv(self.ALLOWED, "artifactory-token")
        monkeypatch.setenv("YUCODE_API_KEY", "sk-must-not-leak")
        scrub_policy(self._options())
        client = StdioMcpClient(McpServerConfig(name="s", command="srv", args=[]))
        with patch("subprocess.Popen") as mock_popen, patch.object(
            StdioMcpClient, "_call", return_value={},
        ):
            client._ensure_initialized()
        _, kwargs = mock_popen.call_args
        child_env = kwargs["env"]
        assert child_env[self.ALLOWED] == "artifactory-token"
        assert "YUCODE_API_KEY" not in child_env

    def test_disabling_the_filter_reaches_every_spawn_point(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scrub_policy: Callable[[RuntimeOptions], None],
    ) -> None:
        """The opt-out has to be as reachable as the allowlist, or the same
        surprise shows up in the other direction."""
        monkeypatch.setenv("YUCODE_API_KEY", "sk-opt-out")
        scrub_policy(RuntimeOptions(shell_env_filter_secrets=False))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = b""
            mock_run.return_value.stderr = b""
            HookRunner(HookConfig(pre_tool_use=["true"])).run_pre_tool_use("Bash", "{}")
        _, kwargs = mock_run.call_args
        assert kwargs["env"]["YUCODE_API_KEY"] == "sk-opt-out"


@pytest.mark.skipif(sys.platform == "win32", reason="uses the POSIX `sh -lc` hook path")
def test_hook_child_process_cannot_read_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end counterpart of the bash injection test: the hook path built
    a scrubbed environment and then spawned without it, so a hook command --
    including one contributed by an installed plugin -- saw the raw
    os.environ. The comment claimed the opposite; nothing checked."""
    monkeypatch.setenv("YUCODE_API_KEY", "sk-must-not-leak")
    command = f"{sys.executable} -c \"import os;print(os.environ.get('YUCODE_API_KEY','ABSENT'))\""
    result = HookRunner(HookConfig(pre_tool_use=[command])).run_pre_tool_use("Bash", "{}")
    joined = "\n".join(result.messages)
    assert "sk-must-not-leak" not in joined
    assert "ABSENT" in joined
    assert os.environ["YUCODE_API_KEY"] == "sk-must-not-leak"


def test_agent_runtime_installs_the_scrub_policy(tmp_path: Path) -> None:
    """The policy is only useful if something installs it.

    Was left xfail while `core/runtime.py` was off-limits: the mechanism can
    be complete and still do nothing, which is the same shape as the dead
    ported modules this codebase has been shedding all week. The one line in
    AgentRuntime.__init__ is what makes a user's shell_env_allowlist reach
    hooks, plugin tools and MCP servers rather than bash alone.
    """
    original = env_scrub.default_policy()
    try:
        AgentRuntime(
            tmp_path,
            AppConfig(
                provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
                runtime=RuntimeOptions(shell_env_allowlist=["ARTIFACTORY_API_KEY"]),
            ),
        )
        assert env_scrub.default_policy().allowlist == ("ARTIFACTORY_API_KEY",)
    finally:
        env_scrub.set_default_policy(original)


def test_bash_child_cannot_read_the_api_key(
    runtime: AgentRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end version of the injection payload: ask a real child process
    to print the key."""
    monkeypatch.setenv("YUCODE_API_KEY", "sk-must-not-leak")
    python = sys.executable
    output = runtime._execute_tool(
        "bash",
        json.dumps({"command": f'{python} -c "import os;print(os.environ.get(\'YUCODE_API_KEY\', \'ABSENT\'))"'}),
    )
    payload = json.loads(output)
    assert payload["returncode"] == 0
    assert "sk-must-not-leak" not in payload["stdout"]
    assert "ABSENT" in payload["stdout"]
    # Sanity: the parent process really does have it set.
    assert os.environ["YUCODE_API_KEY"] == "sk-must-not-leak"
