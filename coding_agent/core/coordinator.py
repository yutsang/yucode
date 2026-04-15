"""Multi-worker coordinator for the agent runtime.

Implements an admin/coordinator pattern that decomposes tasks into
research -> work -> validate phases with retry logic.  Each phase
runs as a scoped sub-runtime with role-appropriate tools.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..config import AppConfig, ToolOptions
from ..plugins.mcp import McpManager
from .providers import OpenAICompatibleProvider
from .session import Message, Usage

_log = logging.getLogger("yucode.coordinator")

EventCallback = Callable[[dict[str, Any]], None]


class WorkerRole(str, Enum):
    RESEARCH = "research"
    WORK = "work"
    VALIDATE = "validate"


ROLE_TOOLS: dict[WorkerRole, list[str]] = {
    WorkerRole.RESEARCH: [
        "read_file", "list_directory", "grep_search", "glob_search",
        "web_search", "web_fetch", "tool_search",
    ],
    WorkerRole.WORK: [
        "read_file", "write_file", "edit_file", "list_directory",
        "grep_search", "glob_search", "bash", "notebook_edit",
    ],
    WorkerRole.VALIDATE: [
        "read_file", "list_directory", "grep_search", "glob_search",
        "bash",
    ],
}

_COMPLEXITY_KEYWORDS = [
    "refactor", "implement", "build", "create", "migrate", "redesign",
    "optimize", "rewrite", "add feature", "multi-step", "across files",
    "entire codebase", "all files", "multiple files",
]

# Conjunctions that suggest multi-part requests
_MULTI_PART_RE = __import__("re").compile(r"\band\s+also\b|\bthen\s+also\b|\b(?:also|additionally)\s+\w", __import__("re").IGNORECASE)

PLAN_PROMPT_TEMPLATE = """\
You are a task planner for a coding agent. Analyze the user request and decompose it
into structured phases.

User request:
{prompt}

Respond with ONLY a valid JSON object (no markdown fences) with these keys:
- "is_simple": boolean — true if the task can be done in a single step without research
- "research_tasks": list of strings — questions to answer before doing work (empty if is_simple)
- "work_tasks": list of strings — concrete implementation steps
- "validation_criteria": list of strings — how to verify the work is correct

Keep each list short (1-5 items). Be specific and actionable.
"""

VALIDATE_PROMPT_TEMPLATE = """\
You are a code reviewer and validator. Check whether the following work results
satisfy the validation criteria.

Validation criteria:
{criteria}

Work results:
{work_results}

Respond with ONLY a valid JSON object (no markdown fences) with these keys:
- "passed": boolean — true if all criteria are met
- "feedback": string — specific feedback on what failed or needs improvement (empty if passed)
"""


@dataclass
class TaskPlan:
    is_simple: bool
    research_tasks: list[str] = field(default_factory=list)
    work_tasks: list[str] = field(default_factory=list)
    validation_criteria: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    passed: bool
    feedback: str = ""


@dataclass
class WorkerResult:
    role: WorkerRole
    task: str
    output: str
    usage: Usage = field(default_factory=Usage)
    iterations: int = 0


@dataclass
class CoordinatorSummary:
    final_text: str
    iterations: int
    total_retries: int = 0
    worker_results: list[WorkerResult] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    assistant_messages: list[Message] = field(default_factory=list)
    tool_messages: list[Message] = field(default_factory=list)


def is_complex_prompt(prompt: str) -> bool:
    """Heuristic: does the prompt look like it needs multi-phase orchestration?"""
    import re
    lower = prompt.lower()

    # These signals are strong enough to override any word-count gate.
    # Explicit complexity keywords
    if any(kw in lower for kw in _COMPLEXITY_KEYWORDS):
        return True
    # Multiple @ file references or source-file extensions → touches many files
    if len(re.findall(r"@[\w./\\-]+|\w+\.(?:py|ts|js|go|rs|java|cpp|c|rb|sh|yaml|yml|json|toml)\b", prompt)) >= 2:
        return True

    # Weaker signals require a minimum word count to avoid false positives on
    # very short prompts like "update and fix".
    if len(lower.split()) < 6:
        return False

    # "and also / then also / additionally X" → clearly multi-part request
    if _MULTI_PART_RE.search(prompt):
        return True
    # Three or more " and " conjunctions suggest a compound task
    return lower.count(" and ") >= 3


class AdminCoordinator:
    """Orchestrates research/work/validate phases with retry."""

    def __init__(
        self,
        workspace_root: Path,
        config: AppConfig,
        *,
        provider: OpenAICompatibleProvider | None = None,
        mcp_manager: McpManager | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.config = config
        self.provider = provider or OpenAICompatibleProvider(config.provider)
        self.mcp_manager = mcp_manager
        self._tool_result_cache: dict[str, str] = {}

    def orchestrate(
        self,
        prompt: str,
        event_callback: EventCallback | None = None,
    ) -> CoordinatorSummary:
        summary = CoordinatorSummary(final_text="", iterations=0)

        if event_callback:
            event_callback({"type": "phase_started", "phase": "plan"})

        plan = self._plan_task(prompt, event_callback)

        if plan.is_simple:
            if event_callback:
                event_callback({"type": "phase_started", "phase": "work_simple"})
            result = self._run_worker(
                WorkerRole.WORK, prompt, event_callback=event_callback,
            )
            summary.final_text = result.output
            summary.iterations = 1
            summary.usage.add(result.usage)
            summary.worker_results.append(result)
            if event_callback:
                event_callback({"type": "completed", "text": summary.final_text})
            return summary

        research_context = ""
        if plan.research_tasks:
            if event_callback:
                event_callback({"type": "phase_started", "phase": "research"})
            research_results = self._run_phase(
                WorkerRole.RESEARCH, plan.research_tasks, event_callback=event_callback,
            )
            summary.worker_results.extend(research_results)
            for r in research_results:
                summary.usage.add(r.usage)
            research_context = self._format_context(research_results)

        max_retries = self.config.runtime.max_iterations
        for attempt in range(1, max_retries + 1):
            summary.total_retries = attempt
            summary.iterations = attempt

            if event_callback:
                event_callback({
                    "type": "phase_started",
                    "phase": "work",
                    "attempt": attempt,
                })

            work_prompt_parts = []
            if research_context:
                work_prompt_parts.append(
                    f"## Research context\n{research_context}"
                )
            work_prompt_parts.append(
                f"## Original request\n{prompt}"
            )

            work_results = self._run_phase(
                WorkerRole.WORK,
                plan.work_tasks,
                context="\n\n".join(work_prompt_parts),
                event_callback=event_callback,
            )
            summary.worker_results.extend(work_results)
            for r in work_results:
                summary.usage.add(r.usage)

            # Skip validation if the plan produced no measurable criteria —
            # an empty criteria list causes the validator to hallucinate a pass.
            if not plan.validation_criteria:
                summary.final_text = self._compose_final(work_results, ValidationResult(passed=True))
                if event_callback:
                    event_callback({"type": "completed", "text": summary.final_text})
                return summary

            if event_callback:
                event_callback({"type": "phase_started", "phase": "validate"})

            validation = self._validate(
                plan.validation_criteria,
                work_results,
                event_callback=event_callback,
            )
            summary.usage.add(validation.usage)

            if validation.result.passed:
                if event_callback:
                    event_callback({
                        "type": "validation_result",
                        "passed": True,
                        "attempt": attempt,
                    })
                summary.final_text = self._compose_final(
                    work_results, validation.result,
                )
                if event_callback:
                    event_callback({"type": "completed", "text": summary.final_text})
                return summary

            if event_callback:
                event_callback({
                    "type": "validation_result",
                    "passed": False,
                    "feedback": validation.result.feedback,
                    "attempt": attempt,
                })

            if attempt < max_retries:
                if event_callback:
                    event_callback({"type": "retry_started", "attempt": attempt + 1})
                plan.work_tasks = self._incorporate_feedback(
                    plan.work_tasks, validation.result.feedback,
                )

        summary.final_text = self._compose_final(
            work_results, validation.result, max_retries_reached=True,
        )
        if event_callback:
            event_callback({"type": "completed", "text": summary.final_text})
        return summary

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def _plan_task(
        self,
        prompt: str,
        event_callback: EventCallback | None = None,
    ) -> TaskPlan:
        plan_prompt = PLAN_PROMPT_TEMPLATE.format(prompt=prompt)
        messages = [
            {"role": "system", "content": "You are a task decomposition assistant."},
            {"role": "user", "content": plan_prompt},
        ]
        response = self.provider.complete(messages, tools=[], stream_callback=event_callback)
        try:
            data = json.loads(response.text.strip())
        except json.JSONDecodeError:
            _log.warning(
                "Task planning returned non-JSON response; falling back to simple mode. "
                "Response (first 200 chars): %s",
                response.text[:200],
            )
            return TaskPlan(is_simple=True, work_tasks=[prompt])

        return TaskPlan(
            is_simple=bool(data.get("is_simple", False)),
            research_tasks=data.get("research_tasks", []),
            work_tasks=data.get("work_tasks", [prompt]),
            validation_criteria=data.get("validation_criteria", []),
        )

    # ------------------------------------------------------------------
    # Worker execution
    # ------------------------------------------------------------------

    def _run_phase(
        self,
        role: WorkerRole,
        tasks: list[str],
        context: str = "",
        event_callback: EventCallback | None = None,
    ) -> list[WorkerResult]:
        results: list[WorkerResult] = []
        for i, task in enumerate(tasks):
            if event_callback:
                event_callback({
                    "type": "worker_spawned",
                    "role": role.value,
                    "task_index": i,
                    "task": task[:200],
                })
            full_prompt = f"{context}\n\n{task}" if context else task
            result = self._run_worker(role, full_prompt, event_callback=event_callback)
            results.append(result)
        return results

    def _run_worker(
        self,
        role: WorkerRole,
        prompt: str,
        event_callback: EventCallback | None = None,
    ) -> WorkerResult:
        from .runtime import AgentRuntime

        worker_config = self._scoped_config(role)
        worker_runtime = AgentRuntime(
            self.workspace_root,
            worker_config,
            mcp_manager=self.mcp_manager,
        )
        summary = worker_runtime.run_turn(
            prompt,
            event_callback=event_callback,
            max_steps_override=self.config.runtime.max_worker_steps,
        )
        return WorkerResult(
            role=role,
            task=prompt[:500],
            output=summary.final_text,
            usage=summary.usage,
            iterations=summary.iterations,
        )

    def _scoped_config(self, role: WorkerRole) -> AppConfig:
        role_tools = list(ROLE_TOOLS.get(role, []))
        if self.mcp_manager:
            for spec in self.mcp_manager.tool_specs():
                mcp_name = spec["function"]["name"]
                if mcp_name not in role_tools:
                    role_tools.append(mcp_name)
        return AppConfig(
            provider=self.config.provider,
            runtime=self.config.runtime,
            tools=ToolOptions(
                allowed=role_tools,
                disabled=list(self.config.tools.disabled),
            ),
            mcp=self.config.mcp,
            vscode=self.config.vscode,
            instruction_files=self.config.instruction_files,
            hooks=self.config.hooks,
            plugins=self.config.plugins,
            sandbox=self.config.sandbox,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @dataclass
    class _ValidateOutcome:
        result: ValidationResult
        usage: Usage = field(default_factory=Usage)

    def _validate(
        self,
        criteria: list[str],
        work_results: list[WorkerResult],
        event_callback: EventCallback | None = None,
    ) -> _ValidateOutcome:
        criteria_text = "\n".join(f"- {c}" for c in criteria)
        work_text = "\n\n---\n\n".join(
            f"### Task: {r.task[:200]}\n{r.output}" for r in work_results
        )
        validate_prompt = VALIDATE_PROMPT_TEMPLATE.format(
            criteria=criteria_text,
            work_results=work_text,
        )
        messages = [
            {"role": "system", "content": "You are a code review validator."},
            {"role": "user", "content": validate_prompt},
        ]
        response = self.provider.complete(messages, tools=[], stream_callback=event_callback)
        try:
            data = json.loads(response.text.strip())
            result = ValidationResult(
                passed=bool(data.get("passed", False)),
                feedback=str(data.get("feedback", "")),
            )
        except json.JSONDecodeError:
            lower = response.text.lower()
            result = ValidationResult(
                passed="pass" in lower and "fail" not in lower,
                feedback=response.text,
            )
        return self._ValidateOutcome(result=result, usage=response.usage)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_context(self, results: list[WorkerResult]) -> str:
        parts: list[str] = []
        for r in results:
            parts.append(f"### {r.role.value}: {r.task[:200]}\n{r.output}")
        return "\n\n".join(parts)

    def _incorporate_feedback(
        self,
        work_tasks: list[str],
        feedback: str,
    ) -> list[str]:
        return [
            f"{task}\n\n[IMPORTANT - Previous attempt feedback]: {feedback}"
            for task in work_tasks
        ]

    def _compose_final(
        self,
        work_results: list[WorkerResult],
        validation: ValidationResult,
        max_retries_reached: bool = False,
    ) -> str:
        parts: list[str] = []
        for r in work_results:
            parts.append(r.output)
        text = "\n\n".join(parts)

        if max_retries_reached:
            note = (
                f"\n\n[Note: Reached maximum retry depth "
                f"({self.config.runtime.max_iterations})."
            )
            if validation.feedback:
                note += f" Validation feedback: {validation.feedback}"
            text += note + "]"
        return text
