"""Parity harness -- deterministic scenarios matching claw-code-main mock harness.

These scenarios mirror the 10 scripted scenarios from
``claw-code-main/rust/MOCK_PARITY_HARNESS.md`` and verify that the Python
``coding_agent`` runtime produces behaviorally equivalent results.

Scenarios:
  1. streaming_text
  2. read_file_roundtrip
  3. grep_chunk_assembly
  4. write_file_allowed
  5. write_file_denied
  6. multi_tool_turn_roundtrip
  7. bash_stdout_roundtrip
  8. bash_permission_prompt_approved
  9. bash_permission_prompt_denied
  10. plugin_tool_roundtrip
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions, ToolOptions
from coding_agent.core.session import Message, Session, Usage
from coding_agent.hooks import HookConfig, HookRunner
from coding_agent.memory.compact import (
    CompactionConfig,
    compact_session,
    should_compact,
)
from coding_agent.plugins.mcp import McpConnectionStatus, McpManager
from coding_agent.security.permissions import (
    PermissionDecision,
    PermissionEnforcer,
    PermissionPolicy,
    PermissionRules,
    parse_permission_rule,
)
from coding_agent.tools import ToolRegistry, normalize_allowed_tools


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".yucode").mkdir()
    return tmp_path


@pytest.fixture
def default_config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(name="test", api_key="test-key", model="test-model"),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )


@pytest.fixture
def extended_config() -> AppConfig:
    """The claw-code-ported tool specs are opt-in (tools.extended_tools_enabled)."""
    return AppConfig(
        provider=ProviderConfig(name="test", api_key="test-key", model="test-model"),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
        tools=ToolOptions(extended_tools_enabled=True),
    )


# ---------------------------------------------------------------------------
# 1. streaming_text -- provider response without tool calls
# ---------------------------------------------------------------------------

class TestStreamingText:
    def test_session_records_assistant_message(self):
        session = Session(model="test")
        session.add_message(Message(role="user", content="hello"))
        session.add_message(Message(role="assistant", content="response text"))
        assert len(session.messages) == 2
        assert session.messages[-1].role == "assistant"
        assert session.messages[-1].content == "response text"


# ---------------------------------------------------------------------------
# 2. read_file_roundtrip
# ---------------------------------------------------------------------------

class TestReadFileRoundtrip:
    def test_read_file_in_workspace(self, workspace: Path, default_config: AppConfig):
        (workspace / "hello.txt").write_text("hello world", encoding="utf-8")
        registry = ToolRegistry(workspace, default_config)
        result = registry.execute("read_file", {"path": "hello.txt"})
        assert "hello world" in result

    def test_read_file_outside_workspace_rejected(self, workspace: Path, default_config: AppConfig):
        registry = ToolRegistry(workspace, default_config)
        with pytest.raises(ValueError, match="escapes the workspace"):
            registry.execute("read_file", {"path": "/etc/passwd"})


# ---------------------------------------------------------------------------
# 3. grep_chunk_assembly
# ---------------------------------------------------------------------------

class TestGrepChunkAssembly:
    def test_grep_tool_exists(self, workspace: Path, default_config: AppConfig):
        registry = ToolRegistry(workspace, default_config)
        assert registry.has_tool("grep_search")


# ---------------------------------------------------------------------------
# 4. write_file_allowed
# ---------------------------------------------------------------------------

class TestWriteFileAllowed:
    def test_write_file_in_workspace(self, workspace: Path, default_config: AppConfig):
        registry = ToolRegistry(workspace, default_config)
        registry.execute("write_file", {
            "path": "output.txt",
            "content": "written by test",
        })
        assert (workspace / "output.txt").read_text() == "written by test"


class TestEditFileUniqueness:
    def test_edit_file_replaces_unique_match(self, workspace: Path, default_config: AppConfig):
        (workspace / "f.txt").write_text("alpha beta gamma", encoding="utf-8")
        registry = ToolRegistry(workspace, default_config)
        result = registry.execute("edit_file", {
            "path": "f.txt", "old_string": "beta", "new_string": "BETA",
        })
        assert "1 replacement" in result
        assert (workspace / "f.txt").read_text() == "alpha BETA gamma"

    def test_edit_file_rejects_non_unique_without_replace_all(
        self, workspace: Path, default_config: AppConfig,
    ):
        (workspace / "f.txt").write_text("foo foo foo", encoding="utf-8")
        registry = ToolRegistry(workspace, default_config)
        with pytest.raises(ValueError, match="matches 3 places"):
            registry.execute("edit_file", {
                "path": "f.txt", "old_string": "foo", "new_string": "bar",
            })
        # File must be left untouched.
        assert (workspace / "f.txt").read_text() == "foo foo foo"

    def test_edit_file_replace_all_handles_multiple(
        self, workspace: Path, default_config: AppConfig,
    ):
        (workspace / "f.txt").write_text("foo foo foo", encoding="utf-8")
        registry = ToolRegistry(workspace, default_config)
        result = registry.execute("edit_file", {
            "path": "f.txt",
            "old_string": "foo",
            "new_string": "bar",
            "replace_all": True,
        })
        assert "3 replacements" in result
        assert (workspace / "f.txt").read_text() == "bar bar bar"


# ---------------------------------------------------------------------------
# 5. write_file_denied
# ---------------------------------------------------------------------------

class TestWriteFileDenied:
    def test_enforcer_denies_write_in_readonly(self, workspace: Path):
        policy = PermissionPolicy("read-only")
        enforcer = PermissionEnforcer(policy, workspace)
        result = enforcer.check_file_write("some_file.txt")
        assert not result.allowed
        assert "read-only" in result.reason

    def test_enforcer_denies_write_outside_workspace(self, workspace: Path):
        policy = PermissionPolicy("workspace-write")
        enforcer = PermissionEnforcer(policy, workspace)
        result = enforcer.check_file_write("/tmp/outside.txt")
        assert not result.allowed
        assert "outside workspace" in result.reason


# ---------------------------------------------------------------------------
# 6. multi_tool_turn_roundtrip
# ---------------------------------------------------------------------------

class TestMultiToolTurn:
    def test_session_tracks_multiple_tool_calls(self):
        from coding_agent.core.session import ToolCall
        session = Session(model="test")
        session.add_message(Message(role="user", content="do two things"))
        session.add_message(Message(
            role="assistant", content="",
            tool_calls=[
                ToolCall(id="tc1", name="read_file", arguments='{"file_path":"a.txt"}'),
                ToolCall(id="tc2", name="read_file", arguments='{"file_path":"b.txt"}'),
            ],
        ))
        session.add_message(Message(role="tool", content="result1", tool_call_id="tc1"))
        session.add_message(Message(role="tool", content="result2", tool_call_id="tc2"))
        assert len(session.messages) == 4
        assert session.messages[1].tool_calls[0].name == "read_file"
        assert session.messages[1].tool_calls[1].name == "read_file"


# ---------------------------------------------------------------------------
# 7. bash_stdout_roundtrip
# ---------------------------------------------------------------------------

class TestBashStdoutRoundtrip:
    def test_bash_tool_exists(self, workspace: Path, default_config: AppConfig):
        registry = ToolRegistry(workspace, default_config)
        assert registry.has_tool("bash")


# ---------------------------------------------------------------------------
# 8. bash_permission_prompt_approved
# ---------------------------------------------------------------------------

class TestBashPermissionPromptApproved:
    def test_prompt_mode_with_approving_prompter(self):
        class Approver:
            def decide(self, request):
                return PermissionDecision(True, "approved")

        policy = PermissionPolicy("prompt")
        decision = policy.authorize(
            "danger-full-access", "bash", '{"command":"ls"}',
            prompter=Approver(),
        )
        assert decision.allowed


# ---------------------------------------------------------------------------
# 9. bash_permission_prompt_denied
# ---------------------------------------------------------------------------

class TestBashPermissionPromptDenied:
    def test_prompt_mode_with_denying_prompter(self):
        class Denier:
            def decide(self, request):
                return PermissionDecision(False, "denied by test")

        policy = PermissionPolicy("prompt")
        decision = policy.authorize(
            "danger-full-access", "bash", '{"command":"rm -rf /"}',
            prompter=Denier(),
        )
        assert not decision.allowed
        assert "denied by test" in decision.reason

    def test_enforcer_defers_in_prompt_mode(self, workspace: Path):
        policy = PermissionPolicy("prompt")
        enforcer = PermissionEnforcer(policy, workspace)
        result = enforcer.check("bash", '{"command":"ls"}')
        assert result.allowed


# ---------------------------------------------------------------------------
# 10. plugin_tool_roundtrip
# ---------------------------------------------------------------------------

class TestPluginToolRoundtrip:
    def test_plugin_tool_collision_rejected(self, workspace: Path, default_config: AppConfig):
        """Built-in names cannot be overwritten by plugins."""
        fake_plugin_tool = type("FakePluginTool", (), {
            "name": "bash",
            "description": "fake bash",
            "input_schema": {"type": "object", "properties": {}},
            "permission": "danger-full-access",
            "execute": lambda self, args: "nope",
        })()
        registry = ToolRegistry(workspace, default_config, plugin_tools=[fake_plugin_tool])
        assert registry.has_tool("bash")
        result = registry.execute("bash", {"command": "echo hi"})
        assert "nope" not in result


# ---------------------------------------------------------------------------
# Cross-cutting: permission rules
# ---------------------------------------------------------------------------

class TestPermissionRules:
    def test_deny_rule_blocks(self):
        rules = PermissionRules(
            deny=[parse_permission_rule("bash")],
        )
        policy = PermissionPolicy("danger-full-access", rules=rules)
        decision = policy.authorize("danger-full-access", "bash", '{"command":"ls"}')
        assert not decision.allowed

    def test_allow_rule_grants(self):
        rules = PermissionRules(
            allow=[parse_permission_rule("bash")],
        )
        policy = PermissionPolicy("read-only", rules=rules)
        decision = policy.authorize("danger-full-access", "bash", '{"command":"ls"}')
        assert decision.allowed

    def test_default_permission_is_danger(self):
        policy = PermissionPolicy("read-only")
        assert policy.required_mode_for("unknown_tool") == "danger-full-access"


# ---------------------------------------------------------------------------
# Cross-cutting: tool normalize
# ---------------------------------------------------------------------------

class TestNormalizeAllowedTools:
    def test_alias_resolution(self):
        result = normalize_allowed_tools(["read", "write"], {"read_file", "write_file", "bash"})
        assert result is not None
        assert "read_file" in result
        assert "write_file" in result

    def test_empty_returns_none(self):
        assert normalize_allowed_tools([]) is None


# ---------------------------------------------------------------------------
# Cross-cutting: compaction
# ---------------------------------------------------------------------------

class TestCompaction:
    def test_should_compact_respects_threshold(self):
        msgs = [Message(role="user", content="x" * 50000)]
        config = CompactionConfig(preserve_recent_messages=0, max_estimated_tokens=100)
        assert should_compact(msgs, config)

    def test_compaction_preserves_recent(self):
        msgs = [
            Message(role="user", content="old " * 5000),
            Message(role="assistant", content="old reply " * 3000),
            Message(role="user", content="recent"),
            Message(role="assistant", content="recent reply"),
        ]
        config = CompactionConfig(preserve_recent_messages=2, max_estimated_tokens=100)
        result = compact_session(msgs, config)
        assert result.removed_message_count == 2
        assert len(result.compacted_messages) == 3


# ---------------------------------------------------------------------------
# Cross-cutting: hook failure path
# ---------------------------------------------------------------------------

class TestHookFailurePath:
    def test_hook_config_has_failure_field(self):
        config = HookConfig(post_tool_use_failure=["echo fail"])
        runner = HookRunner(config)
        assert runner.config.post_tool_use_failure == ["echo fail"]


# ---------------------------------------------------------------------------
# Cross-cutting: MCP lifecycle
# ---------------------------------------------------------------------------

class TestMcpLifecycle:
    def test_connection_status_enum(self):
        assert McpConnectionStatus.DISCONNECTED.value == "disconnected"
        assert McpConnectionStatus.CONNECTED.value == "connected"
        assert McpConnectionStatus.ERROR.value == "error"

    def test_empty_manager_produces_empty_report(self):
        manager = McpManager([])
        report = manager.discovery_report()
        assert report.tools == []
        assert report.failed_servers == []


# ---------------------------------------------------------------------------
# Cross-cutting: extended tool specs
# ---------------------------------------------------------------------------

class TestExtendedToolSpecs:
    def test_registry_has_task_tools(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        for name in ["TaskCreate", "TaskGet", "TaskList", "TaskStop", "TaskUpdate", "TaskOutput"]:
            assert registry.has_tool(name), f"Missing tool: {name}"

    def test_registry_has_worker_tools(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        for name in ["WorkerCreate", "WorkerGet", "WorkerTerminate"]:
            assert registry.has_tool(name), f"Missing tool: {name}"

    def test_registry_has_mcp_lifecycle_tools(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        for name in ["ListMcpResources", "ReadMcpResource", "McpAuth", "MCP"]:
            assert registry.has_tool(name), f"Missing tool: {name}"

    def test_registry_has_team_cron_tools(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        for name in ["TeamCreate", "TeamDelete", "CronCreate", "CronDelete", "CronList"]:
            assert registry.has_tool(name), f"Missing tool: {name}"


class TestExtendedToolsDisabledByDefault:
    """The ported specs are opt-in: by default the model must not be offered
    tools that answer "acknowledged" without doing the work (PowerShell is the
    dangerous one -- on Windows it is the first name a model reaches for)."""

    NO_OP_STUBS = [
        "SendUserMessage", "Config", "EnterPlanMode", "ExitPlanMode",
        "StructuredOutput", "REPL", "PowerShell", "ListMcpResources",
        "ReadMcpResource", "McpAuth", "MCP",
    ]

    def test_no_op_stubs_are_not_registered_by_default(
        self, workspace: Path, default_config: AppConfig,
    ):
        registry = ToolRegistry(workspace, default_config)
        registered = registry.all_tool_names()
        for name in self.NO_OP_STUBS:
            assert name not in registered, f"No-op stub still registered: {name}"

    def test_registry_backed_specs_also_gated(self, workspace: Path, default_config: AppConfig):
        registry = ToolRegistry(workspace, default_config)
        for name in ["TaskCreate", "WorkerCreate", "CronCreate", "TeamCreate"]:
            assert not registry.has_tool(name), f"Extended tool still registered: {name}"

    def test_no_advertised_tool_answers_acknowledged(
        self, workspace: Path, default_config: AppConfig,
    ):
        """No tool offered to the model may be a handler that reports success
        without doing anything."""
        registry = ToolRegistry(workspace, default_config)
        advertised = {d["function"]["name"] for d in registry.definitions_for_provider()}
        fake_success = [
            name for name in advertised
            if registry._tools[name].handler.__qualname__.startswith("_stub_handler")
        ]
        assert fake_success == []

    def test_flag_restores_them(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        for name in [*self.NO_OP_STUBS, "TaskCreate"]:
            assert registry.has_tool(name), f"Missing tool with flag on: {name}"


class TestToolNameIndexCollisions:
    def test_camel_case_alias_resolves_to_the_real_tool(
        self, workspace: Path, extended_config: AppConfig,
    ):
        """`structured_output` (real) and the ported `StructuredOutput` (no-op)
        normalize to the same index key; the built-in must win."""
        registry = ToolRegistry(workspace, extended_config)
        assert registry.has_tool("structured_output")
        assert registry._resolve_name("structuredOutput") == "structured_output"
        assert "acknowledged" not in registry.execute("structuredOutput", {})

    def test_exact_names_are_never_shadowed(self, workspace: Path, extended_config: AppConfig):
        """A collision may cost an alias, but never an exact-name lookup."""
        registry = ToolRegistry(workspace, extended_config)
        for name in registry.list_names():
            assert registry._resolve_name(name) == name

    def test_alias_resolution_still_works(self, workspace: Path, default_config: AppConfig):
        registry = ToolRegistry(workspace, default_config)
        assert registry._resolve_name("WebSearch") == "web_search"


# ---------------------------------------------------------------------------
# Registry-backed tool execution tests
# ---------------------------------------------------------------------------

class TestTaskRegistryIntegration:
    def test_task_create_returns_real_data(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        result = json.loads(registry.execute("TaskCreate", {"prompt": "test task", "description": "desc"}))
        assert "task_id" in result
        assert result["status"] == "created"

    def test_task_list_returns_array(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        result = json.loads(registry.execute("TaskList", {}))
        assert isinstance(result, list)

    def test_task_lifecycle(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        created = json.loads(registry.execute("TaskCreate", {"prompt": "lifecycle test"}))
        task_id = created["task_id"]
        got = json.loads(registry.execute("TaskGet", {"task_id": task_id}))
        assert got["task_id"] == task_id
        stopped = json.loads(registry.execute("TaskStop", {"task_id": task_id}))
        assert stopped["status"] == "stopped"


class TestWorkerRegistryIntegration:
    def test_worker_create_returns_real_data(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        result = json.loads(registry.execute("WorkerCreate", {"cwd": "/tmp"}))
        assert "worker_id" in result
        assert result["status"] == "spawning"


class TestTeamCronIntegration:
    def test_cron_create_and_list(self, workspace: Path, extended_config: AppConfig):
        registry = ToolRegistry(workspace, extended_config)
        created = json.loads(registry.execute("CronCreate", {"schedule": "*/5 * * * *", "prompt": "check status"}))
        assert "cron_id" in created
        items = json.loads(registry.execute("CronList", {}))
        assert isinstance(items, list)
        assert any(c["cron_id"] == created["cron_id"] for c in items)


class TestSummaryCompression:
    def test_compress_within_budget(self):
        from coding_agent.core.summary_compression import SummaryCompressionBudget, compress_summary
        text = "\n".join([f"Line {i}: some content here" for i in range(50)])
        result = compress_summary(text, SummaryCompressionBudget(max_chars=200, max_lines=5))
        assert result.compressed_lines <= 6
        assert result.omitted_lines > 0


class TestTaskPacket:
    def test_validate_packet(self):
        from coding_agent.core.task_packet import TaskPacket, validate_packet
        packet = TaskPacket(objective="build feature")
        validated = validate_packet(packet)
        assert validated.objective == "build feature"

    def test_validate_rejects_empty_objective(self):
        from coding_agent.core.task_packet import TaskPacket, TaskPacketValidationError, validate_packet
        with pytest.raises(TaskPacketValidationError):
            validate_packet(TaskPacket(objective=""))


class TestCostTracking:
    def test_usage_tracker_cost_summary(self):
        from coding_agent.core.session import Session, UsageTracker
        session = Session(model="test")
        tracker = UsageTracker.from_session(session)
        tracker.record(Usage(input_tokens=100, output_tokens=50))
        summary = tracker.cost_summary()
        assert summary["input_tokens"] == 100
        assert summary["output_tokens"] == 50
        assert summary["total_tokens"] == 150
        assert summary["turns"] == 1
