"""Coordinator retry-loop convergence: validation-failure retries must be
bounded by max_coordinator_retries, not max_iterations. Before this fix, the
outer research->work->validate retry loop reused max_iterations (a
single-agent-turn budget, default 32) as the number of full work-phase
re-runs -- each retry reruns an entire worker (itself up to max_worker_steps
turns), so a task that kept failing validation could redo the whole work
phase up to 32 times. Found from a real run against the fdd-commentary skill
that spiralled into dozens of retries before exhausting max_iterations.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.core.coordinator import (
    ROLE_TOOLS,
    AdminCoordinator,
    TaskPlan,
    ValidationResult,
    WorkerResult,
    WorkerRole,
)


class TestRoleToolsIncludeOfficeTools:
    """Regression: WorkerRole.WORK/VALIDATE had NO office/PPTX tools at all --
    a coordinator-routed FDD/PPTX task (any multi-project or multi-step
    request is complex enough to route through the coordinator under
    orchestration_mode: auto) could produce markdown commentary from research
    context, but the WORK-phase worker had no way to ever call
    fill_pptx_table or even inspect_pptx_shapes. Found from a real run whose
    output deck had its financial table never filled."""

    def test_work_role_can_write_pptx_and_read_databooks(self) -> None:
        work_tools = ROLE_TOOLS[WorkerRole.WORK]
        for tool in (
            "inspect_pptx_shapes", "fill_pptx_shape_text", "fill_pptx_table",
            "estimate_pptx_text_capacity", "write_pptx_from_template",
            "read_excel_sheet", "excel_to_json", "inspect_excel_sheets",
        ):
            assert tool in work_tools, f"{tool} missing from WorkerRole.WORK"

    def test_validate_role_can_inspect_but_not_write_pptx(self) -> None:
        validate_tools = ROLE_TOOLS[WorkerRole.VALIDATE]
        for tool in ("inspect_pptx_shapes", "read_pptx", "read_excel_sheet"):
            assert tool in validate_tools, f"{tool} missing from WorkerRole.VALIDATE"
        for tool in ("fill_pptx_shape_text", "fill_pptx_table", "write_pptx_from_template"):
            assert tool not in validate_tools, f"{tool} should not be writable from VALIDATE"

    def test_research_role_can_inspect_pptx_shapes(self) -> None:
        # Already had read_pptx/read_excel_sheet; inspect_pptx_shapes and
        # estimate_pptx_text_capacity were missing even from research.
        research_tools = ROLE_TOOLS[WorkerRole.RESEARCH]
        assert "inspect_pptx_shapes" in research_tools
        assert "estimate_pptx_text_capacity" in research_tools


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".yucode").mkdir()
    return tmp_path


def _make_coordinator(
    workspace: Path, *, max_coordinator_retries: int, max_iterations: int,
) -> AdminCoordinator:
    config = AppConfig(
        provider=ProviderConfig(name="test", api_key="k", model="gpt-test", intelligence_tier="strong"),
        runtime=RuntimeOptions(
            permission_mode="danger-full-access",
            max_iterations=max_iterations,
            max_coordinator_retries=max_coordinator_retries,
        ),
    )
    return AdminCoordinator(workspace, config)


def _always_failing_plan() -> TaskPlan:
    return TaskPlan(
        is_simple=False,
        research_tasks=[],
        work_tasks=["do the thing"],
        validation_criteria=["must be correct"],
    )


class TestCoordinatorRetryBound:
    def test_retry_count_bounded_by_max_coordinator_retries_not_max_iterations(
        self, workspace: Path, monkeypatch,
    ) -> None:
        # max_iterations is huge (999) -- if the old bug regresses (retry loop
        # reusing max_iterations again), this test will hang/timeout instead
        # of finishing in 2 work-phase calls.
        coord = _make_coordinator(workspace, max_coordinator_retries=2, max_iterations=999)
        monkeypatch.setattr(coord, "_plan_task", lambda prompt, cb: _always_failing_plan())

        work_phase_calls = 0

        def fake_run_phase(role, tasks, context="", event_callback=None):
            nonlocal work_phase_calls
            assert role == WorkerRole.WORK
            work_phase_calls += 1
            return [WorkerResult(role=role, task=tasks[0], output="attempted output")]

        monkeypatch.setattr(coord, "_run_phase", fake_run_phase)
        monkeypatch.setattr(
            coord, "_validate",
            lambda criteria, work_results, event_callback=None: coord._ValidateOutcome(
                result=ValidationResult(passed=False, feedback="still wrong"),
            ),
        )

        summary = coord.orchestrate("do a multi-step task")

        assert work_phase_calls == 2
        assert summary.total_retries == 2
        assert "maximum retry depth (2)" in summary.final_text

    def test_stops_early_once_validation_passes(self, workspace: Path, monkeypatch) -> None:
        coord = _make_coordinator(workspace, max_coordinator_retries=5, max_iterations=999)
        monkeypatch.setattr(coord, "_plan_task", lambda prompt, cb: _always_failing_plan())

        work_phase_calls = 0

        def fake_run_phase(role, tasks, context="", event_callback=None):
            nonlocal work_phase_calls
            work_phase_calls += 1
            return [WorkerResult(role=role, task=tasks[0], output="good output")]

        monkeypatch.setattr(coord, "_run_phase", fake_run_phase)
        monkeypatch.setattr(
            coord, "_validate",
            lambda criteria, work_results, event_callback=None: coord._ValidateOutcome(
                result=ValidationResult(passed=True),
            ),
        )

        summary = coord.orchestrate("do a multi-step task")

        assert work_phase_calls == 1
        assert summary.total_retries == 1
        assert "good output" in summary.final_text


class _RecordingProvider:
    """Stands in for OpenAICompatibleProvider and counts every call made
    through it, whoever makes it."""

    def __init__(self, text: str = "worker output") -> None:
        self.calls: list[list[dict]] = []
        self._text = text

    def complete(self, messages, tools, stream_callback=None, **kwargs):  # noqa: ANN001
        from coding_agent.core.session import AssistantResponse
        self.calls.append(list(messages))
        return AssistantResponse(text=self._text)


class TestWorkerProviderInjection:
    """A9: workers built their own OpenAICompatibleProvider, so anything
    measuring the coordinator from the outside saw only the planner/validator
    calls and none of the workers'. A real A/B run this way reported 2 calls /
    1,333 tokens for work that actually cost 26 calls / 196,738."""

    def test_worker_runtime_uses_the_injected_provider(self, workspace: Path) -> None:
        provider = _RecordingProvider()
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="gpt-test"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", max_worker_steps=3),
        )
        coord = AdminCoordinator(workspace, config, provider=provider)  # type: ignore[arg-type]

        result = coord._run_worker(WorkerRole.WORK, "do the thing")

        # The worker's model call landed on the injected instance, not on a
        # private one -- this is the whole point of A9.
        assert len(provider.calls) == 1
        assert result.output == "worker output"

    def test_every_phase_of_a_full_orchestration_is_visible_on_one_provider(
        self, workspace: Path, monkeypatch,
    ) -> None:
        provider = _RecordingProvider()
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="gpt-test"),
            runtime=RuntimeOptions(
                permission_mode="danger-full-access", max_worker_steps=3,
                max_coordinator_retries=1,
            ),
        )
        coord = AdminCoordinator(workspace, config, provider=provider)  # type: ignore[arg-type]
        monkeypatch.setattr(
            coord, "_plan_task",
            lambda prompt, cb: TaskPlan(
                is_simple=False, research_tasks=["look it up"],
                work_tasks=["do it"], validation_criteria=[],
            ),
        )

        coord.orchestrate("investigate and implement the thing")

        # One research worker + one work worker, both through the injected
        # provider. Before A9 this was 0.
        assert len(provider.calls) == 2


def _cutoff_summary(iterations: int, text: str = "half-written analysis"):
    """A turn `run_turn` fell out of the bottom of: the last assistant message
    is still asking for a tool call."""
    from coding_agent.core.runtime import TurnSummary
    from coding_agent.core.session import Message, ToolCall

    return TurnSummary(
        final_text=text, iterations=iterations,
        assistant_messages=[Message(
            role="assistant", content=text,
            tool_calls=[ToolCall(id="c1", name="read_file", arguments="{}")],
        )],
    )


def _finished_summary(iterations: int, text: str = "half-written analysis"):
    """A turn `run_turn` returned from inside its loop: the model answered with
    no tool calls."""
    from coding_agent.core.runtime import TurnSummary
    from coding_agent.core.session import Message

    return TurnSummary(
        final_text=text, iterations=iterations,
        assistant_messages=[Message(role="assistant", content=text)],
    )


class TestWorkerStopReason:
    """A1: a worker cut off at max_worker_steps used to return a bare string,
    indistinguishable from a finished one."""

    def _coord_with_worker_summary(self, workspace: Path, monkeypatch, summary):
        from coding_agent.core.runtime import AgentRuntime

        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="gpt-test"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", max_worker_steps=4),
        )
        coord = AdminCoordinator(workspace, config, provider=_RecordingProvider())  # type: ignore[arg-type]

        def fake_run_turn(self, prompt, event_callback=None, max_steps_override=None):  # noqa: ANN001
            return summary

        monkeypatch.setattr(AgentRuntime, "run_turn", fake_run_turn)
        return coord

    def test_step_limited_worker_is_marked_incomplete(self, workspace: Path, monkeypatch) -> None:
        coord = self._coord_with_worker_summary(workspace, monkeypatch, _cutoff_summary(4))
        result = coord._run_worker(WorkerRole.WORK, "big task")

        assert result.hit_step_limit is True
        assert result.output.startswith("[INCOMPLETE:")
        assert "4-step limit" in result.output
        assert "half-written analysis" in result.output

    def test_worker_cut_off_by_the_tool_call_budget_is_marked_incomplete(
        self, workspace: Path, monkeypatch,
    ) -> None:
        """`iterations >= max_worker_steps` EXCLUDED the case it exists to catch.
        `run_turn` sets `budget_exhausted = True` inside an iteration and only
        breaks at the top of the NEXT one, so a worker whose tool-call budget
        died on step 2 of an allowed 4 reports `iterations == 2`. `2 >= 4` is
        False, so no banner, `hit_step_limit` False, and `_compose_final`
        counted zero truncated workers -- the user got budget-truncated
        half-work presented as a finished answer."""
        coord = self._coord_with_worker_summary(workspace, monkeypatch, _cutoff_summary(2))
        result = coord._run_worker(WorkerRole.WORK, "task killed by the tool budget")

        assert result.hit_step_limit is True
        assert result.output.startswith("[INCOMPLETE:")
        assert "half-written analysis" in result.output

    def test_completed_worker_output_is_untouched(self, workspace: Path, monkeypatch) -> None:
        coord = self._coord_with_worker_summary(workspace, monkeypatch, _finished_summary(2))
        result = coord._run_worker(WorkerRole.WORK, "small task")

        assert result.hit_step_limit is False
        assert result.output == "half-written analysis"

    def test_worker_that_answered_on_its_last_step_is_not_incomplete(
        self, workspace: Path, monkeypatch,
    ) -> None:
        """`iterations >= max_worker_steps` alone is not a cut-off signal.
        `run_turn` sets `summary.iterations = iteration` at the TOP of each
        pass and returns from inside the loop when the model answers, so a run
        that finishes on its very last allowed step reports exactly
        max_worker_steps -- and every one of them was labelled INCOMPLETE."""
        coord = self._coord_with_worker_summary(
            workspace, monkeypatch, _finished_summary(4, "the complete answer"))
        result = coord._run_worker(WorkerRole.WORK, "task that just fit")

        assert result.hit_step_limit is False
        assert result.output == "the complete answer"

    def test_marker_reaches_the_composed_final_text(self, workspace: Path, monkeypatch) -> None:
        coord = self._coord_with_worker_summary(workspace, monkeypatch, _cutoff_summary(4))
        monkeypatch.setattr(
            coord, "_plan_task",
            lambda prompt, cb: TaskPlan(is_simple=True, work_tasks=["do it"]),
        )
        summary = coord.orchestrate("implement the thing")

        assert "[INCOMPLETE:" in summary.final_text


class TestComposeFinalKeepsEveryWorkersOutput:
    """The INCOMPLETE banner is byte-identical across workers of the same role.
    Prepending it to each worker's output put N identical paragraphs at a
    regular spacing into `_compose_final`'s join -- the exact periodicity
    `dedup_repetitive_response` looks for. With three truncated workers it
    "collapsed the repetition" and kept only the last worker, throwing away the
    first two workers' real findings."""

    def _coord(self, workspace: Path) -> AdminCoordinator:
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="gpt-test"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", max_worker_steps=20),
        )
        return AdminCoordinator(workspace, config, provider=_RecordingProvider())  # type: ignore[arg-type]

    def _banner(self) -> str:
        return (
            "[INCOMPLETE: this work worker stopped at its 20-step limit rather "
            "than finishing. Everything below is partial work — verify it "
            "before building on it.]"
        )

    def _bodies(self) -> dict[str, list[str]]:
        # Every paragraph is distinct from every other, so the ONLY periodic
        # structure in the joined text is the repeated banner.
        return {
            "ALPHA": ["ALPHA-1-" + "a" * 200, "ALPHA-2-" + "b" * 200, "ALPHA-3-" + "c" * 200],
            "BRAVO": ["BRAVO-1-" + "d" * 200, "BRAVO-2-" + "e" * 200, "BRAVO-3-" + "f" * 200],
            "CHARLIE": ["CHARLIE-1-" + "g" * 200, "CHARLIE-2-" + "h" * 200,
                        "CHARLIE-3-" + "i" * 200],
        }

    def test_three_truncated_workers_all_survive(self, workspace: Path) -> None:
        banner = self._banner()
        bodies = self._bodies()
        results = [
            WorkerResult(role=WorkerRole.WORK, task=f"t{name}", hit_step_limit=True,
                         output=banner + "\n\n" + "\n\n".join(paras))
            for name, paras in bodies.items()
        ]
        final = self._coord(workspace)._compose_final(results, ValidationResult(passed=True))

        for name in bodies:
            assert f"{name}-1-" in final, f"{name}'s output was dropped by dedup"
        # One aggregated banner, not one per worker.
        assert final.count("[INCOMPLETE:") == 1
        assert "3 of 3 worker(s)" in final

    def test_the_banner_is_what_makes_dedup_misfire(self, workspace: Path) -> None:
        """Control: the same three bodies WITHOUT the banner are left alone by
        dedup_repetitive_response, so the banner is the cause, not the bodies."""
        from coding_agent.core.response_dedup import dedup_repetitive_response

        joined = "\n\n".join(p for paras in self._bodies().values() for p in paras)
        assert dedup_repetitive_response(joined) == joined

    def test_no_banner_when_every_worker_finished(self, workspace: Path) -> None:
        results = [
            WorkerResult(role=WorkerRole.WORK, task="t", output="clean output"),
        ]
        final = self._coord(workspace)._compose_final(results, ValidationResult(passed=True))
        assert final == "clean output"


class TestInvestigateOnlyKeepsEveryWorkersOutput:
    """The investigate-only early return in `orchestrate` never got the fix
    `_compose_final` did: it joined the research workers' outputs WITH their
    INCOMPLETE banners still attached and handed the blob to
    `dedup_repetitive_response`. Three truncated research workers put three
    byte-identical paragraphs at a regular spacing into that join -- the exact
    periodicity the dedup pass hunts for -- so workers 1 and 2 lost their real
    findings. The synthesis branch was worse still: it fed the banners to a
    model told "do not list 'worker 1 said...'", then returned `synth.text`
    with no INCOMPLETE marker at all, so truncated research read as finished."""

    def _coord(self, workspace: Path, provider=None) -> AdminCoordinator:
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="gpt-test"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", max_worker_steps=20),
        )
        return AdminCoordinator(  # type: ignore[arg-type]
            workspace, config, provider=provider or _RecordingProvider(),
        )

    def _banner(self) -> str:
        return (
            "[INCOMPLETE: this research worker stopped at its 20-step limit rather "
            "than finishing. Everything below is partial work — verify it "
            "before building on it.]"
        )

    def _bodies(self) -> dict[str, list[str]]:
        # Every paragraph is distinct from every other, so the ONLY periodic
        # structure in the joined text is the repeated banner.
        return {
            "ALPHA": ["ALPHA-1-" + "a" * 200, "ALPHA-2-" + "b" * 200, "ALPHA-3-" + "c" * 200],
            "BRAVO": ["BRAVO-1-" + "d" * 200, "BRAVO-2-" + "e" * 200, "BRAVO-3-" + "f" * 200],
            "CHARLIE": ["CHARLIE-1-" + "g" * 200, "CHARLIE-2-" + "h" * 200,
                        "CHARLIE-3-" + "i" * 200],
        }

    def _truncated_results(self) -> list[WorkerResult]:
        banner = self._banner()
        return [
            WorkerResult(role=WorkerRole.RESEARCH, task=f"t{name}", hit_step_limit=True,
                         output=banner + "\n\n" + "\n\n".join(paras))
            for name, paras in self._bodies().items()
        ]

    def _orchestrate(self, coord: AdminCoordinator, monkeypatch, results) -> str:
        monkeypatch.setattr(
            coord, "_plan_task",
            lambda prompt, cb: TaskPlan(
                is_simple=False, investigate_only=True,
                research_tasks=[r.task for r in results],
            ),
        )
        monkeypatch.setattr(
            coord, "_run_phase",
            lambda role, tasks, context="", event_callback=None: results,
        )
        return coord.orchestrate("why does the thing happen").final_text

    def test_three_truncated_research_workers_all_survive_the_dedup_path(
        self, workspace: Path, monkeypatch,
    ) -> None:
        coord = self._coord(workspace)
        # Force the fallback branch (as a provider error would), so this test
        # covers the joined+deduped path rather than synthesis.
        monkeypatch.setattr(
            coord, "_synthesise_investigation",
            lambda prompt, joined, cb=None: None,
        )
        final = self._orchestrate(coord, monkeypatch, self._truncated_results())

        for name in self._bodies():
            assert f"{name}-1-" in final, f"{name}'s findings were dropped by dedup"
        assert final.count("[INCOMPLETE:") == 1
        assert "3 of 3 worker(s)" in final

    def test_synthesised_investigation_still_carries_the_incomplete_marker(
        self, workspace: Path, monkeypatch,
    ) -> None:
        coord = self._coord(workspace, provider=_RecordingProvider("consolidated answer"))
        final = self._orchestrate(coord, monkeypatch, self._truncated_results())

        assert final.startswith("[INCOMPLETE:")
        assert "3 of 3 worker(s)" in final
        assert "consolidated answer" in final

    def test_synthesis_prompt_does_not_carry_the_per_worker_banners(
        self, workspace: Path, monkeypatch,
    ) -> None:
        provider = _RecordingProvider("consolidated answer")
        coord = self._coord(workspace, provider=provider)
        self._orchestrate(coord, monkeypatch, self._truncated_results())

        synth_prompt = provider.calls[-1][-1]["content"]
        assert "[INCOMPLETE:" not in synth_prompt

    def test_finished_research_workers_get_no_banner(
        self, workspace: Path, monkeypatch,
    ) -> None:
        coord = self._coord(workspace, provider=_RecordingProvider("consolidated answer"))
        results = [
            WorkerResult(role=WorkerRole.RESEARCH, task="a", output="finding A"),
            WorkerResult(role=WorkerRole.RESEARCH, task="b", output="finding B"),
        ]
        final = self._orchestrate(coord, monkeypatch, results)

        assert final == "consolidated answer"


class TestRoleToolsNamesAreReal:
    """Every name in ROLE_TOOLS must be a tool the registry actually has.

    A typo here fails silently: `_scoped_config` filters the child runtime's
    tool set by name, so a name that matches nothing simply removes that
    capability from the role with no error anywhere. This caught
    `notebook_edit`, which had been listed for WORK since the role table was
    written while the tool has always been registered as `edit_notebook_cell`
    -- so WORK workers never had notebook editing at all.
    """

    def test_every_role_tool_is_registered(self, tmp_path) -> None:
        from coding_agent.config import load_app_config
        from coding_agent.core.coordinator import ROLE_TOOLS
        from coding_agent.tools import ToolRegistry

        config = load_app_config(None)
        registry = ToolRegistry(tmp_path, config, None)
        known = set(registry.all_tool_names())

        unknown: dict[str, list[str]] = {}
        for role, names in ROLE_TOOLS.items():
            missing = sorted(n for n in names if n not in known)
            if missing:
                unknown[str(role)] = missing
        assert not unknown, f"ROLE_TOOLS names with no registered tool: {unknown}"
