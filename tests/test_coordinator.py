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
