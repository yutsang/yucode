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
        "web_search", "web_fetch",
        "memory_list", "memory_read", "memory_search",
        # Office/PDF inspection so research can answer "what's in this spreadsheet?"
        "inspect_excel_sheets", "read_excel_sheet", "list_excel_sheets", "excel_to_json",
        "read_excel_preview", "read_word_text", "read_word_paragraphs",
        "read_pptx", "inspect_pptx_shapes", "estimate_pptx_text_capacity",
        "read_pdf_text", "image_read",
    ],
    WorkerRole.WORK: [
        "read_file", "read_files", "write_file", "edit_file", "list_directory",
        "grep_search", "glob_search", "bash", "edit_notebook_cell",
        "memory_list", "memory_read",
        # Office/PPTX read+write -- a "simple" plan runs everything (including
        # the databook cross-check and the actual PPTX export) through this
        # one role with no separate research phase, so WORK needs the full
        # office toolset, not just the write-capable half. Missing from this
        # list entirely (rather than just from a curated subset) meant a
        # coordinator-routed FDD/PPTX task could produce commentary from
        # research context but had no way to ever call fill_pptx_table or
        # even inspect_pptx_shapes -- found from a real run where the output
        # deck's financial table was never filled.
        "inspect_excel_sheets", "read_excel_sheet", "list_excel_sheets", "excel_to_json",
        "write_excel_cell", "read_excel_preview",
        "read_word_text", "read_word_paragraphs", "write_word", "append_word",
        "read_pptx", "write_pptx", "write_pptx_from_template",
        "inspect_pptx_shapes", "fill_pptx_shape_text", "fill_pptx_table",
        "estimate_pptx_text_capacity", "read_pdf_text", "image_read",
    ],
    WorkerRole.VALIDATE: [
        "read_file", "read_files", "list_directory", "grep_search", "glob_search",
        "bash",
        # Read-only office/PPTX access so validation can check the ACTUAL
        # produced artifact (e.g. "is the table shape really filled, is the
        # commentary non-trivial") instead of only judging the worker's
        # self-reported text.
        "inspect_excel_sheets", "read_excel_sheet", "excel_to_json", "read_excel_preview",
        "read_pptx", "inspect_pptx_shapes",
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


_INCOMPLETE_BANNER_PREFIX = "[INCOMPLETE:"


def _strip_incomplete_banner(output: str) -> str:
    """Drop a leading `[INCOMPLETE: ...]` paragraph, if present."""
    if not output.startswith(_INCOMPLETE_BANNER_PREFIX):
        return output
    head, sep, rest = output.partition("\n\n")
    if sep and head.rstrip().endswith("]"):
        return rest
    return output


def ended_mid_tool_call(summary: Any) -> bool:
    """True when a turn ended while the model still wanted to call tools.

    `run_turn` returns from *inside* its loop only when the model answers with
    no tool calls; every cut-off exit (step exhaustion, tool-call budget,
    dedup force-stop) instead falls out of the bottom of the loop with the last
    assistant message still carrying tool calls.

    `iterations >= max_steps` on its own cannot tell those apart: a run that
    answers on its very last allowed step reports exactly `max_steps` too, and
    was mislabelled INCOMPLETE. Conservative on unknown input — a summary with
    no recorded assistant messages reads as "finished", so this can only ever
    remove a false INCOMPLETE, never add one.
    """
    messages = getattr(summary, "assistant_messages", None)
    if not messages:
        return False
    return bool(getattr(messages[-1], "tool_calls", None))


@dataclass
class WorkerResult:
    role: WorkerRole
    task: str
    output: str
    usage: Usage = field(default_factory=Usage)
    iterations: int = 0
    # True when the worker ran out of steps instead of finishing. Without it,
    # a half-done result reads exactly like a finished one and the coordinator
    # (and then the user) treats partial work as fact. Also stated in `output`
    # so the model consuming it sees the same thing.
    hit_step_limit: bool = False


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


# Prompts at or under this length, once is_complex_prompt has already ruled
# out complexity/investigation signals, are treated as trivial (greetings,
# one-line lookups) rather than the ambiguous middle ground.
_TRIVIAL_WORD_COUNT = 8


def classify_reasoning_effort(prompt: str, intelligence_tier: str = "strong") -> str:
    """Map a user prompt to a reasoning_effort level for reasoning-model gateways.

    Reuses is_complex_prompt's coding-agent-tuned signals (refactor/implement/
    investigate keywords, multi-file references, "why"/"how" question forms)
    rather than a generic length heuristic, since those are what actually
    distinguish a hard coding task from an easy one. Deliberately asymmetric:
    under-reasoning on a hard task produces a worse answer, while
    over-reasoning on an easy one only costs time — so "high" fires on any
    complexity signal, and "low" only fires when no such signal is present
    AND the prompt is short enough to be a trivial lookup/greeting. Everything
    else defaults to "medium".
    """
    if is_complex_prompt(prompt, intelligence_tier=intelligence_tier):
        return "high"
    if len(prompt.split()) <= _TRIVIAL_WORD_COUNT:
        return "low"
    return "medium"


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
            # Strip each worker's INCOMPLETE banner before joining, for the same
            # reason _compose_final does: the banner is byte-identical across
            # workers of the same role, so N truncated workers contribute N
            # identical paragraphs at a regular spacing -- exactly the
            # periodicity dedup_repetitive_response hunts for, which silently
            # discarded the first workers' real findings. It also kept the
            # synthesis model from following its own "do not list 'worker 1
            # said...'" rule. Re-added once, aggregated, after both.
            joined = "\n\n".join(
                _strip_incomplete_banner(r.output) for r in non_empty
            ).strip() or research_context
            final_text: str | None = None
            # When ≥2 workers produced output, run a synthesis pass instead of
            # returning a stitched + deduped blob. The synthesis call consolidates
            # overlapping findings into one coherent answer keyed off the original
            # prompt — particularly helpful for cross-cutting questions where each
            # worker addressed a different facet.
            if len(non_empty) >= 2:
                synth = self._synthesise_investigation(prompt, joined, event_callback)
                if synth is not None:
                    final_text = synth.text
                    summary.usage.add(synth.usage)
            if final_text is None:
                # Fallback: cross-worker dedup on the joined text (also the single-worker path).
                final_text = dedup_repetitive_response(joined)
            truncated = [r for r in research_results if r.hit_step_limit]
            if truncated:
                # Without this the synthesis branch returned truncated research
                # with no INCOMPLETE marker whatsoever — a cut-off investigation
                # that read to the user like a finished answer.
                final_text = (
                    self._truncation_banner(len(truncated), len(research_results))
                    + "\n\n" + final_text
                )
            summary.final_text = final_text
            summary.iterations = max(summary.iterations, 1)
            if event_callback:
                event_callback({"type": "completed", "text": summary.final_text})
            return summary

        max_retries = self.config.runtime.max_coordinator_retries
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
            # Share the coordinator's provider instead of letting every worker
            # build its own. _scoped_config only rewrites `tools`, passing
            # `provider` through unchanged, so one instance is correct for all
            # roles -- and it is what makes external instrumentation of the
            # coordinator path see worker calls at all. Without it an A/B run
            # measured 2 calls / 1,333 tokens for work that really cost 26
            # calls / 196,738.
            provider=self.provider,
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
        max_worker_steps = self.config.runtime.max_worker_steps
        summary = worker_runtime.run_turn(
            worker_prompt,
            event_callback=event_callback,
            max_steps_override=max_worker_steps,
        )
        # `ended_mid_tool_call` is the whole signal. Pairing it with
        # `iterations >= max_worker_steps` excluded the cases this flag exists
        # to catch: `run_turn` sets `budget_exhausted = True` inside an
        # iteration and only breaks at the top of the NEXT one, and its
        # stuck-dedup force-exit leaves the loop early too — both report an
        # `iterations` well under the step limit, so both were silently
        # reported as finished work.
        hit_step_limit = ended_mid_tool_call(summary)
        output = dedup_repetitive_response(summary.final_text)
        if hit_step_limit:
            output = (
                f"[INCOMPLETE: this {role.value} worker was cut off mid-tool-call "
                f"after {summary.iterations} step(s) — its {max_worker_steps}-step "
                "limit, the tool-call budget, or a stuck-loop exit. Everything "
                "below is partial work — verify it before building on it.]\n\n"
                + output
            )
        return WorkerResult(
            role=role,
            task=prompt[:500],
            output=output,
            usage=summary.usage,
            iterations=summary.iterations,
            hit_step_limit=hit_step_limit,
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

    def _truncation_banner(self, truncated: int, total: int) -> str:
        """One aggregated INCOMPLETE banner for a joined, already-deduped blob.

        Shared by `_compose_final` and the investigate-only early return so
        both report truncation the same way.
        """
        return (
            f"[INCOMPLETE: {truncated} of {total} worker(s) were cut off "
            f"mid-tool-call — the {self.config.runtime.max_worker_steps}-step "
            "limit, the tool-call budget, or a stuck-loop exit — rather than "
            "finishing. Some of what follows is partial work — verify it "
            "before building on it.]"
        )

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
        # Strip each worker's INCOMPLETE banner before joining. The banner is
        # byte-identical across workers of the same role, so N truncated
        # workers contribute N identical paragraphs at a regular spacing --
        # exactly the periodicity dedup_repetitive_response hunts for. With
        # three truncated workers it detected a "repeat" and kept only the last
        # one, silently discarding the first two workers' real output. Re-added
        # once, below, after the dedup pass.
        parts: list[str] = []
        for r in work_results:
            parts.append(_strip_incomplete_banner(r.output))
        text = "\n\n".join(parts)
        # Cross-worker dedup — Qwen3 planner often splits a single question
        # into multiple work_tasks (e.g. "find declaration" + "get signature"),
        # each worker produces an overlapping answer, and joining them with
        # \n\n looks like multi-pass repetition. Collapse it here.
        text = dedup_repetitive_response(text)

        truncated = [r for r in work_results if r.hit_step_limit]
        if truncated:
            text = (
                self._truncation_banner(len(truncated), len(work_results))
                + "\n\n" + text
            )

        if max_retries_reached:
            note = (
                f"\n\n[Note: Reached maximum retry depth "
                f"({self.config.runtime.max_coordinator_retries})."
            )
            if validation.feedback:
                note += f" Validation feedback: {validation.feedback}"
            text += note + "]"
        return text
