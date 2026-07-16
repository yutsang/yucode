"""Tests for the v0.5.0 improvements:

- read_files (batch) + file_outline tools
- edit_file no-op rejection
- planner JSON retry helper + tolerant parsing
- tier-aware weak-model prompt section
- post-condition hook framework + bash-claim-mismatch check
- _record_tool_observation correctness
- concrete pytest validator detection
- investigation synthesis output shape
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.core.coordinator import _try_parse_plan_json
from coding_agent.core.runtime import (
    _BASH_SUCCESS_PHRASES,
    _check_final_answer_grounding,
    _GroundingViolation,
    _ToolObservations,
)
from coding_agent.tools import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".yucode").mkdir()
    return tmp_path


@pytest.fixture
def strong_config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(
            name="test", api_key="k", model="gpt-test", intelligence_tier="strong",
        ),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )


@pytest.fixture
def weak_config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(
            name="test", api_key="k", model="qwen3-32b", intelligence_tier="weak",
        ),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )


# ---------------------------------------------------------------------------
# read_files
# ---------------------------------------------------------------------------

class TestReadFiles:
    def test_batch_returns_each_file_content(self, workspace: Path, strong_config: AppConfig):
        (workspace / "a.txt").write_text("alpha", encoding="utf-8")
        (workspace / "b.txt").write_text("beta\nbeta-line-2", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        result = json.loads(registry.execute("read_files", {"paths": ["a.txt", "b.txt"]}))
        assert len(result) == 2
        assert "alpha" in result[0]["content"]
        assert "beta-line-2" in result[1]["content"]
        assert result[1]["total_lines"] == 2

    def test_missing_file_reported_per_entry(self, workspace: Path, strong_config: AppConfig):
        (workspace / "a.txt").write_text("ok", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        result = json.loads(registry.execute(
            "read_files", {"paths": ["a.txt", "nope.txt"]},
        ))
        assert "content" in result[0]
        assert "error" in result[1]
        assert "not found" in result[1]["error"].lower()

    def test_rejects_too_many_paths(self, workspace: Path, strong_config: AppConfig):
        registry = ToolRegistry(workspace, strong_config)
        too_many = [f"f{i}.txt" for i in range(20)]
        with pytest.raises(ValueError, match="too many paths"):
            registry.execute("read_files", {"paths": too_many})

    def test_rejects_empty_paths(self, workspace: Path, strong_config: AppConfig):
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError, match="non-empty list"):
            registry.execute("read_files", {"paths": []})


# ---------------------------------------------------------------------------
# file_outline
# ---------------------------------------------------------------------------

class TestFileOutline:
    def test_extracts_classes_functions_imports(self, workspace: Path, strong_config: AppConfig):
        (workspace / "mod.py").write_text(
            "import os\nfrom pathlib import Path\n\n"
            "def top_fn():\n    pass\n\n"
            "class Foo(Base):\n"
            "    def method_a(self):\n        pass\n"
            "    async def method_b(self):\n        pass\n",
            encoding="utf-8",
        )
        registry = ToolRegistry(workspace, strong_config)
        result = json.loads(registry.execute("file_outline", {"path": "mod.py"}))
        names_imports = [i.get("module") or i.get("name") for i in result["imports"]]
        assert "os" in names_imports
        assert any("Path" in str(i.get("module") or i.get("name")) for i in result["imports"])
        assert [c["name"] for c in result["classes"]] == ["Foo"]
        assert result["classes"][0]["bases"] == ["Base"]
        method_names = {m["name"] for m in result["classes"][0]["methods"]}
        assert method_names == {"method_a", "method_b"}
        assert any(m["async"] for m in result["classes"][0]["methods"] if m["name"] == "method_b")
        assert [f["name"] for f in result["functions"]] == ["top_fn"]

    def test_rejects_non_python(self, workspace: Path, strong_config: AppConfig):
        (workspace / "doc.md").write_text("# heading\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError, match="only supports Python"):
            registry.execute("file_outline", {"path": "doc.md"})

    def test_missing_file_suggests_similar(self, workspace: Path, strong_config: AppConfig):
        # _find_similar_files matches workspace files whose name contains the
        # query stem, so query stem "config" → workspace "configuration.py".
        (workspace / "configuration.py").write_text("x = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(FileNotFoundError, match="Did you mean"):
            registry.execute("file_outline", {"path": "config.py"})

    def test_handles_syntax_error_gracefully(self, workspace: Path, strong_config: AppConfig):
        (workspace / "broken.py").write_text("def broken(\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        result = json.loads(registry.execute("file_outline", {"path": "broken.py"}))
        assert "error" in result
        assert "parse" in result["error"].lower()


# ---------------------------------------------------------------------------
# edit_file no-op rejection
# ---------------------------------------------------------------------------

class TestEditFileNoOp:
    def test_rejects_identical_old_and_new(self, workspace: Path, strong_config: AppConfig):
        (workspace / "f.txt").write_text("hello", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError, match="identical"):
            registry.execute("edit_file", {
                "path": "f.txt", "old_string": "hello", "new_string": "hello",
            })
        assert (workspace / "f.txt").read_text() == "hello"


# ---------------------------------------------------------------------------
# Planner JSON tolerant parser
# ---------------------------------------------------------------------------

class TestPlannerJsonParser:
    def test_plain_json(self):
        assert _try_parse_plan_json('{"a": 1}') == {"a": 1}

    def test_strips_markdown_fence(self):
        text = '```json\n{"is_simple": true, "work_tasks": ["x"]}\n```'
        result = _try_parse_plan_json(text)
        assert result == {"is_simple": True, "work_tasks": ["x"]}

    def test_strips_bare_triple_fence(self):
        text = '```\n{"a": 1}\n```'
        assert _try_parse_plan_json(text) == {"a": 1}

    def test_extracts_object_after_prose(self):
        text = "Sure! Here is the plan:\n{\"is_simple\": false, \"work_tasks\": []}"
        result = _try_parse_plan_json(text)
        assert result == {"is_simple": False, "work_tasks": []}

    def test_returns_none_on_bare_text(self):
        assert _try_parse_plan_json("nope this is just prose") is None

    def test_returns_none_on_empty(self):
        assert _try_parse_plan_json("") is None
        assert _try_parse_plan_json("   ") is None

    def test_returns_none_on_array_top_level(self):
        # We only accept top-level JSON objects.
        assert _try_parse_plan_json("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# Tier-aware prompt
# ---------------------------------------------------------------------------

class TestTierAwarePrompt:
    def _render(self, config: AppConfig, workspace: Path) -> str:
        from coding_agent.memory.prompting import (
            PromptAssembler,
            discover_project_context,
        )
        ctx = discover_project_context(workspace, current_date="2026-05-14", include_git_context=False)
        return PromptAssembler(config, ctx).render()

    def test_weak_tier_includes_grounding_rules(self, weak_config: AppConfig, workspace: Path):
        rendered = self._render(weak_config, workspace)
        assert "Grounding rules" in rendered
        assert "read_files" in rendered
        assert "file_outline" in rendered

    def test_strong_tier_omits_grounding_rules(self, strong_config: AppConfig, workspace: Path):
        rendered = self._render(strong_config, workspace)
        assert "Grounding rules" not in rendered


# ---------------------------------------------------------------------------
# Grounding hook framework
# ---------------------------------------------------------------------------

class TestGroundingChecks:
    def test_grep_matched_but_no_read_fires_only_for_weak_investigation(self):
        obs = _ToolObservations(grep_had_matches=True)
        violation = _check_final_answer_grounding(
            obs, "the answer is X", is_weak_investigation=True,
        )
        assert isinstance(violation, _GroundingViolation)
        assert violation.reason == "grep_matched_but_no_read"

    def test_grep_matched_but_no_read_skipped_for_strong_tier(self):
        obs = _ToolObservations(grep_had_matches=True)
        assert _check_final_answer_grounding(
            obs, "any text", is_weak_investigation=False,
        ) is None

    def test_grep_matched_but_no_read_skipped_when_read_happened(self):
        obs = _ToolObservations(grep_had_matches=True, read_paths={"foo.py"})
        assert _check_final_answer_grounding(
            obs, "any text", is_weak_investigation=True,
        ) is None

    def test_bash_claim_mismatch_fires(self):
        obs = _ToolObservations(last_bash_returncode=1)
        violation = _check_final_answer_grounding(
            obs, "All tests passed successfully.", is_weak_investigation=False,
        )
        assert isinstance(violation, _GroundingViolation)
        assert violation.reason == "claimed_success_but_bash_failed"

    def test_bash_claim_mismatch_skipped_when_returncode_zero(self):
        obs = _ToolObservations(last_bash_returncode=0)
        assert _check_final_answer_grounding(
            obs, "tests passed", is_weak_investigation=False,
        ) is None

    def test_bash_claim_mismatch_skipped_when_no_success_phrase(self):
        obs = _ToolObservations(last_bash_returncode=1)
        assert _check_final_answer_grounding(
            obs, "I encountered an error and couldn't finish.", is_weak_investigation=False,
        ) is None

    def test_chinese_success_phrase_triggers_mismatch(self):
        obs = _ToolObservations(last_bash_returncode=2)
        violation = _check_final_answer_grounding(
            obs, "測試通過", is_weak_investigation=False,
        )
        assert violation is not None
        assert violation.reason == "claimed_success_but_bash_failed"


# ---------------------------------------------------------------------------
# Concrete validator detection
# ---------------------------------------------------------------------------

class TestConcreteValidator:
    def test_returns_none_when_no_tests_dir(self, workspace: Path, strong_config: AppConfig):
        from coding_agent.core.coordinator import AdminCoordinator
        coord = AdminCoordinator(workspace, strong_config)
        assert coord._run_concrete_validators() is None

    def test_returns_pass_when_tests_pass(self, workspace: Path, strong_config: AppConfig):
        from coding_agent.core.coordinator import AdminCoordinator
        tests_dir = workspace / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_trivial.py").write_text(
            "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8",
        )
        coord = AdminCoordinator(workspace, strong_config)
        result = coord._run_concrete_validators()
        assert result is not None
        passed, _output = result
        # pytest may not be installed everywhere; we accept either pass or skip
        # via FileNotFoundError handled by the validator. If pytest IS available
        # and test passes, this must be True.
        assert passed is True

    def test_returns_fail_when_test_fails(self, workspace: Path, strong_config: AppConfig):
        from coding_agent.core.coordinator import AdminCoordinator
        tests_dir = workspace / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_broken.py").write_text(
            "def test_broken():\n    assert False, 'expected failure'\n", encoding="utf-8",
        )
        coord = AdminCoordinator(workspace, strong_config)
        result = coord._run_concrete_validators()
        assert result is not None
        passed, output = result
        assert passed is False
        assert "expected failure" in output or "assert False" in output


# ---------------------------------------------------------------------------
# Tool observation recording
# ---------------------------------------------------------------------------

class TestToolObservationRecording:
    def _make_runtime(self, workspace: Path, config: AppConfig):
        from coding_agent.core.runtime import AgentRuntime
        return AgentRuntime(workspace, config)

    def test_records_grep_match(self, workspace: Path, strong_config: AppConfig):
        runtime = self._make_runtime(workspace, strong_config)
        obs = _ToolObservations()
        runtime._record_tool_observation(
            obs, "grep_search", '{"pattern": "x"}', "foo.py:1:x = 1",
        )
        assert obs.grep_had_matches is True

    def test_ignores_error_envelope_for_grep(self, workspace: Path, strong_config: AppConfig):
        runtime = self._make_runtime(workspace, strong_config)
        obs = _ToolObservations()
        runtime._record_tool_observation(
            obs, "grep_search", "{}", '{\n  "error": "rg failed"\n}',
        )
        assert obs.grep_had_matches is False

    def test_records_read_path(self, workspace: Path, strong_config: AppConfig):
        runtime = self._make_runtime(workspace, strong_config)
        obs = _ToolObservations()
        runtime._record_tool_observation(
            obs, "read_file", '{"path": "foo.py"}', "content",
        )
        assert "foo.py" in obs.read_paths

    def test_records_read_files_paths(self, workspace: Path, strong_config: AppConfig):
        runtime = self._make_runtime(workspace, strong_config)
        obs = _ToolObservations()
        runtime._record_tool_observation(
            obs, "read_files", '{"paths": ["a.py", "b.py"]}', "[]",
        )
        assert "a.py" in obs.read_paths and "b.py" in obs.read_paths

    def test_records_bash_returncode(self, workspace: Path, strong_config: AppConfig):
        runtime = self._make_runtime(workspace, strong_config)
        obs = _ToolObservations()
        runtime._record_tool_observation(
            obs, "bash", '{"command": "x"}',
            '{"returncode": 2, "stdout": "", "stderr": "boom"}',
        )
        assert obs.last_bash_returncode == 2


# ---------------------------------------------------------------------------
# Sanity — constants
# ---------------------------------------------------------------------------

def test_bash_success_phrases_nonempty():
    assert len(_BASH_SUCCESS_PHRASES) > 0
    assert "tests passed" in _BASH_SUCCESS_PHRASES


# ---------------------------------------------------------------------------
# Per-turn reasoning_effort (Workbench/GPT-5.5): opt-in via extra_body,
# classified once per turn from the user prompt, not touched for providers
# that never configured reasoning_effort in the first place.
# ---------------------------------------------------------------------------

class TestPerTurnReasoningEffort:
    def _make_runtime(self, workspace: Path, extra_body: dict):
        from coding_agent.core.runtime import AgentRuntime
        config = AppConfig(
            provider=ProviderConfig(
                name="test", api_key="k", model="gpt-test", intelligence_tier="strong",
                extra_body=extra_body,
            ),
            runtime=RuntimeOptions(permission_mode="danger-full-access"),
        )
        return AgentRuntime(workspace, config)

    def _capture_body_overrides(self, runtime, monkeypatch) -> dict:
        captured: dict = {}

        def fake_complete(self, messages, tools, stream_callback=None, *, cancel_event=None, body_overrides=None):
            captured["body_overrides"] = body_overrides
            from coding_agent.core.session import AssistantResponse, Usage
            return AssistantResponse(text="done", tool_calls=[], usage=Usage(input_tokens=1, output_tokens=1))

        monkeypatch.setattr(type(runtime.provider), "complete", fake_complete)
        return captured

    def test_no_override_when_provider_has_not_opted_in(self, workspace: Path, monkeypatch):
        runtime = self._make_runtime(workspace, extra_body={})
        captured = self._capture_body_overrides(runtime, monkeypatch)
        runtime.run_turn("refactor the auth module to use JWT")
        assert captured["body_overrides"] is None

    def test_low_effort_for_trivial_prompt_when_opted_in(self, workspace: Path, monkeypatch):
        runtime = self._make_runtime(workspace, extra_body={"reasoning_effort": "medium"})
        captured = self._capture_body_overrides(runtime, monkeypatch)
        runtime.run_turn("hi")
        assert captured["body_overrides"] == {"reasoning_effort": "low"}

    def test_high_effort_for_complex_prompt_when_opted_in(self, workspace: Path, monkeypatch):
        runtime = self._make_runtime(workspace, extra_body={"reasoning_effort": "medium"})
        captured = self._capture_body_overrides(runtime, monkeypatch)
        runtime.run_turn("refactor the auth module to use JWT instead of session cookies")
        assert captured["body_overrides"] == {"reasoning_effort": "high"}
