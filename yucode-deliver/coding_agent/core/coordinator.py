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
from .response_dedup import dedup_repetitive_response
from .session import Message, Usage

_log = logging.getLogger("yucode.coordinator")

EventCallback = Callable[[dict[str, Any]], None]


class WorkerRole(str, Enum):
    RESEARCH = "research"
    WORK = "work"
    VALIDATE = "validate"


ROLE_TOOLS: dict[WorkerRole, list[str]] = {
    WorkerRole.RESEARCH: [
        "read_file", "read_files", "file_outline",
        "list_directory", "grep_search", "glob_search",
        "web_search", "web_fetch", "tool_search",
        "memory_list", "memory_read", "memory_search",
        # Office/PDF inspection so research can answer "what's in this spreadsheet?"
        "inspect_excel_sheets", "read_excel_sheet", "read_excel_preview",
        "read_word_text", "read_pptx", "read_pdf_text",
    ],
    WorkerRole.WORK: [
        "read_file", "read_files", "write_file", "edit_file", "list_directory",
        "grep_search", "glob_search", "bash", "notebook_edit",
        "memory_list", "memory_read",
    ],
    WorkerRole.VALIDATE: [
        "read_file", "read_files", "list_directory", "grep_search", "glob_search",
        "bash",
    ],
}

_COMPLEXITY_KEYWORDS = [
    # English — implementation
    "refactor", "implement", "build", "create", "migrate", "redesign",
    "optimize", "rewrite", "add feature", "multi-step", "across files",
    "entire codebase", "all files", "multiple files",
    # English — investigation (added: weak models won't multi-mode without these)
    "investigate", "debug", "trace", "analyze", "analyse", "compare",
    # Chinese — implementation
    "重構", "實作", "實現", "建立", "建構", "加入", "新增", "修復", "優化", "改寫",
    # Chinese — investigation
    "調查", "追蹤", "分析", "比較", "解釋", "找出", "搜尋", "查詢", "為什麼", "為何",
]

# Conjunctions that suggest multi-part requests
_MULTI_PART_RE = __import__("re").compile(r"\band\s+also\b|\bthen\s+also\b|\b(?:also|additionally)\s+\w", __import__("re").IGNORECASE)

# Investigation question forms — "why does X happen", "where is Y defined", etc.
# Triggers coordinator even for short prompts because investigation tasks
# almost always need multi-step grep→read→synthesise.
_INVESTIGATION_RE = __import__("re").compile(
    # English question forms
    r"\bwhy\b"                                         # "why X happens", "check why"
    r"|\bwhere\s+(is|are|does)\b"                       # "where is foo defined"
    r"|\bhow\s+(does|is|are|many|much|do)\b"            # "how many", "how does"
    r"|\b(list|count|show|find)\s+(every|each|all)\b"   # "list every env var"
    r"|\bgroup\s+(them|by)\b"                           # "group them by", "group by"
    # Chinese question / investigation cues
    r"|為什麼|為何|哪裡|哪個|怎麼|怎樣|多少",
    __import__("re").IGNORECASE,
)


def _try_parse_plan_json(text: str) -> dict[str, Any] | None:
    """Tolerantly parse a planner response — strips ```json fences and finds the
    outermost {...} block. Returns None when no valid JSON object is found."""
    stripped = text.strip()
    if not stripped:
        return None
    # Strip optional ```json … ``` fences.
    if stripped.startswith("```"):
        first_nl = stripped.find("\n")
        if first_nl != -1:
            stripped = stripped[first_nl + 1:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError:
        # Last resort: find the outermost {...} block by bracket matching.
        start = stripped.find("{")
        if start == -1:
            return None
        depth = 0
        end = -1
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return None
        try:
            result = json.loads(stripped[start:end + 1])
        except json.JSONDecodeError:
            return None
    return result if isinstance(result, dict) else None


def looks_like_investigation(prompt: str) -> bool:
    """True if *prompt* matches investigation patterns — used by runtime + coordinator."""
    lower = prompt.lower()
    if _INVESTIGATION_RE.search(prompt):
        return True
    investigation_kw = (
        "investigate", "debug", "trace", "analyze", "analyse", "explain", "compare",
        "調查", "追蹤", "分析", "比較", "解釋", "找出", "為什麼", "為何",
    )
    return any(kw in lower for kw in investigation_kw)

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
    # True when this plan is pure investigation — research output IS the answer,
    # work/validate phases are skipped. Set by _plan_task's weak-model override.
    investigate_only: bool = False


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


def is_complex_prompt(prompt: str, intelligence_tier: str = "strong") -> bool:
    """Heuristic: does the prompt look like it needs multi-phase orchestration?

    *intelligence_tier* controls how aggressive the heuristic is. For "weak"
    models (Qwen3-32B, Llama-70B, smaller local models) we trigger coordinator
    more readily — they benefit more from explicit decomposition.
    """
    import re
    lower = prompt.lower()

    # These signals are strong enough to override any word-count gate.
    # Explicit complexity keywords (now includes Chinese + investigation verbs)
    if any(kw in lower for kw in _COMPLEXITY_KEYWORDS):
        return True
    # Investigation question forms — bypass word-count gate (often short prompts)
    if _INVESTIGATION_RE.search(prompt):
        return True
    # Multiple @ file references or source-file extensions → touches many files
    if len(re.findall(r"@[\w./\\-]+|\w+\.(?:py|ts|js|go|rs|java|cpp|c|rb|sh|yaml|yml|json|toml)\b", prompt)) >= 2:
        return True

    # Word-count gate: by default 6 words minimum to avoid false positives like
    # "update and fix". Weak models benefit from earlier decomposition, so we
    # drop the gate to 3 words for them.
    min_words = 3 if intelligence_tier == "weak" else 6
    if len(lower.split()) < min_words:
        return False

    # "and also / then also / additionally X" → clearly multi-part request
    if _MULTI_PART_RE.search(prompt):
        return True
    # Three or more " and " conjunctions suggest a compound task
    if lower.count(" and ") >= 3:
        return True

    # Weak-model only: any prompt with an explicit verb-like investigation cue
    # ("find", "show me", "list", "explain") gets coordinator. Strong models
    # handle these fine single-agent, so we keep the gate for them.
    if intelligence_tier == "weak":
        weak_cues = ("find ", "show me", "list all", "explain", "tell me", "告訴我", "列出", "顯示")
        if any(cue in lower for cue in weak_cues):
            return True
    return False


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
        research_results: list[WorkerResult] = []
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

        # Investigation-only plan: the research output IS the answer.
        # Skip work + validate, return immediately.
        if plan.investigate_only and research_results:
            non_empty = [r for r in research_results if r.output and r.output.strip()]
            joined = "\n\n".join(r.output for r in non_empty).strip() or research_context
            # When ≥2 workers produced output, run a synthesis pass instead of
            # returning a stitched + deduped blob. The synthesis call consolidates
            # overlapping findings into one coherent answer keyed off the original
            # prompt — particularly helpful for cross-cutting questions where each
            # worker addressed a different facet.
            if len(non_empty) >= 2:
                synth = self._synthesise_investigation(prompt, joined, event_callback)
                if synth is not None:
                    summary.final_text = synth.text
                    summary.usage.add(synth.usage)
                    summary.iterations = max(summary.iterations, 1)
                    if event_callback:
                        event_callback({"type": "completed", "text": summary.final_text})
                    return summary
            # Fallback: cross-worker dedup on the joined text (also the single-worker path).
            summary.final_text = dedup_repetitive_response(joined)
            summary.iterations = max(summary.iterations, 1)
            if event_callback:
                event_callback({"type": "completed", "text": summary.final_text})
            return summary

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
        data = _try_parse_plan_json(response.text)
        if data is None:
            # Retry once with a stricter "JSON ONLY" reminder — weak models often
            # produce a markdown-wrapped or prose-prefixed response on the first
            # attempt but recover when the format is restated.
            _log.info("Planner returned non-JSON on first attempt; retrying with stricter format reminder.")
            retry_messages = messages + [
                {"role": "assistant", "content": response.text},
                {"role": "user", "content": (
                    "Your previous response was not valid JSON. Respond now with ONLY a "
                    "single JSON object — no prose, no markdown fences, no commentary. "
                    "Required keys: is_simple, research_tasks, work_tasks, validation_criteria."
                )},
            ]
            retry_response = self.provider.complete(retry_messages, tools=[], stream_callback=event_callback)
            data = _try_parse_plan_json(retry_response.text)
            if data is None:
                _log.warning(
                    "Task planning returned non-JSON on both attempts; falling back to simple mode. "
                    "First response (first 200 chars): %s",
                    response.text[:200],
                )
                return TaskPlan(is_simple=True, work_tasks=[prompt])

        plan = TaskPlan(
            is_simple=bool(data.get("is_simple", False)),
            research_tasks=data.get("research_tasks", []),
            work_tasks=data.get("work_tasks", [prompt]),
            validation_criteria=data.get("validation_criteria", []),
        )
        # Weak-model override: Qwen3-class models often return is_simple=True
        # for investigation prompts ("explain X", "why does Y") and the
        # downstream worker then answers from memory with zero tool calls.
        # Force them through a research-only flow so the answer is grounded.
        if (
            self.config.provider.resolved_tier() == "weak"
            and plan.is_simple
            and looks_like_investigation(prompt)
        ):
            plan.is_simple = False
            plan.investigate_only = True
            plan.work_tasks = []          # skip work phase
            plan.validation_criteria = []  # skip validate phase
            if not plan.research_tasks:
                plan.research_tasks = [
                    f"Investigate and answer using workspace evidence: {prompt}"
                ]
        return plan

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
                    "total_tasks": len(tasks),
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
        worker_prompt = prompt
        if role == WorkerRole.RESEARCH:
            # Research workers MUST escalate from grep to read. Observed
            # failure: Qwen3 grep'd `compact_token_threshold`, saw one line,
            # then hallucinated the default value (10,000 — actual is 60,000).
            worker_prompt = (
                "[Research role rules — MUST follow]\n"
                "1. After any grep_search that returns hits, you MUST call "
                "   read_file on at least one of the cited files before "
                "   drawing a conclusion. Grep returns one line of context — "
                "   that is NOT enough to determine a symbol's value or "
                "   definition.\n"
                "2. If grep hits are in .yaml / .md / .json / .txt / .rst, "
                "   treat them as documentation references, not definitions. "
                "   Find the real .py / .ts / source-language definition.\n"
                "3. Cite specific file paths and line numbers in your answer.\n"
                "4. Do NOT repeat your answer in multiple rephrasings — "
                "   produce ONE answer.\n\n"
                + prompt
            )
        summary = worker_runtime.run_turn(
            worker_prompt,
            event_callback=event_callback,
            max_steps_override=self.config.runtime.max_worker_steps,
        )
        return WorkerResult(
            role=role,
            task=prompt[:500],
            output=dedup_repetitive_response(summary.final_text),
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
    # Investigation synthesis
    # ------------------------------------------------------------------

    @dataclass
    class _SynthOutcome:
        text: str
        usage: Usage = field(default_factory=Usage)

    def _synthesise_investigation(
        self,
        original_prompt: str,
        joined_findings: str,
        event_callback: EventCallback | None = None,
    ) -> AdminCoordinator._SynthOutcome | None:
        """Consolidate ≥2 research worker outputs into one coherent answer.

        Returns None on provider error so the caller can fall back to the
        joined+deduped blob.
        """
        synth_prompt = (
            "You are synthesising the findings of multiple research workers into a single,"
            " coherent answer to the user's original question.\n\n"
            f"User's original question:\n{original_prompt}\n\n"
            "Research findings (multiple workers, may overlap):\n"
            f"{joined_findings}\n\n"
            "Rules:\n"
            "- Produce ONE consolidated answer. Do not list 'worker 1 said... worker 2 said...'.\n"
            "- Preserve every concrete fact: file paths, line numbers, values, signatures.\n"
            "- If workers disagree, prefer the one that cited specific filename:line evidence.\n"
            "- Do not invent details that no worker mentioned.\n"
            "- Output prose (or a short bulleted list if the question asked for one). No JSON."
        )
        messages = [
            {"role": "system", "content": "You consolidate research findings into one answer."},
            {"role": "user", "content": synth_prompt},
        ]
        try:
            response = self.provider.complete(messages, tools=[], stream_callback=event_callback)
        except Exception as exc:  # noqa: BLE001 — fall back to non-synthesised path
            _log.warning("Investigation synthesis failed; using joined+deduped output: %s", exc)
            return None
        text = dedup_repetitive_response(response.text.strip())
        if not text:
            return None
        return self._SynthOutcome(text=text, usage=response.usage)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @dataclass
    class _ValidateOutcome:
        result: ValidationResult
        usage: Usage = field(default_factory=Usage)

    def _run_concrete_validators(self) -> tuple[bool, str] | None:
        """Run language-level ground-truth validators on the current workspace.

        Returns ``(passed, output)`` or ``None`` when no applicable validator is
        present OR the validator itself failed to start (e.g. pytest not
        installed). Currently runs pytest if ``tests/`` contains test files;
        future additions (cargo check, npm test, …) hang off the same return
        shape.

        Critical: when pytest is missing (`No module named pytest`) we return
        ``None`` (inconclusive), NOT (False, ...). Returning False would
        trigger the coordinator retry loop on every turn until max-retries —
        a real failure mode that wasted ~50 minutes on a Windows box that
        didn't have pytest installed."""
        import subprocess
        tests_dir = self.workspace_root / "tests"
        has_pytest_dir = tests_dir.is_dir() and any(tests_dir.rglob("test_*.py"))
        if not has_pytest_dir:
            return None
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", "-x", "-q", "--no-header", "tests/"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "pytest exceeded 120s timeout"
        except FileNotFoundError:
            return None  # python not on PATH — skip rather than fail
        combined = (result.stdout + ("\n" + result.stderr if result.stderr else "")).strip()
        # Distinguish "tests failed" from "pytest couldn't even start". The
        # latter (missing module, missing python, missing pytest plugin) is
        # NOT a verdict on the agent's work — skip rather than mark failed.
        startup_failure_markers = (
            "No module named pytest",
            "no module named pytest",
            "ModuleNotFoundError: No module named",
            "command not found",
            "pytest.ini exception",
        )
        if result.returncode != 0 and any(m in combined for m in startup_failure_markers):
            return None
        passed = result.returncode == 0
        # Keep only the tail so a flood of test output doesn't fill the validator prompt.
        if len(combined) > 4000:
            combined = "…(truncated)…\n" + combined[-4000:]
        return passed, combined

    def _validate(
        self,
        criteria: list[str],
        work_results: list[WorkerResult],
        event_callback: EventCallback | None = None,
    ) -> _ValidateOutcome:
        # Concrete validator first — if pytest exists and fails, that's
        # ground truth; skip the LLM-as-judge step which would otherwise
        # be free to declare "passed: true" on broken code.
        concrete = self._run_concrete_validators()
        if event_callback and concrete is not None:
            event_callback({
                "type": "concrete_validator",
                "validator": "pytest",
                "passed": concrete[0],
            })
        if concrete is not None and not concrete[0]:
            return self._ValidateOutcome(
                result=ValidationResult(
                    passed=False,
                    feedback=(
                        "Concrete validator (pytest) failed. Tail of output:\n"
                        + concrete[1]
                    ),
                ),
            )
        criteria_text = "\n".join(f"- {c}" for c in criteria)
        work_text = "\n\n---\n\n".join(
            f"### Task: {r.task[:200]}\n{r.output}" for r in work_results
        )
        if concrete is not None and concrete[0]:
            # Pass the pytest evidence to the LLM judge so it can rely on it.
            work_text += (
                "\n\n---\n\n### Concrete validator evidence\n"
                f"pytest exited 0. Tail of output:\n{concrete[1][-1500:]}"
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
        # Cross-worker dedup — Qwen3 planner often splits a single question
        # into multiple work_tasks (e.g. "find declaration" + "get signature"),
        # each worker produces an overlapping answer, and joining them with
        # \n\n looks like multi-pass repetition. Collapse it here.
        text = dedup_repetitive_response(text)

        if max_retries_reached:
            note = (
                f"\n\n[Note: Reached maximum retry depth "
                f"({self.config.runtime.max_iterations})."
            )
            if validation.feedback:
                note += f" Validation feedback: {validation.feedback}"
            text += note + "]"
        return text
