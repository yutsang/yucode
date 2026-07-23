from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig, load_app_config
from ..hooks import HookConfig, HookRunner, merge_hook_feedback
from ..memory.compact import (
    CompactionConfig,
    CompactionResult,
    compact_session,
    estimate_session_tokens,
    is_degenerate_summary,
    should_compact,
)
from ..memory.prompting import PromptAssembler, discover_project_context
from ..observability.metrics import AuditLogger, MetricsCollector
from ..plugins import PluginManager
from ..plugins.mcp import McpManager
from ..security.permissions import (
    PermissionEnforcer,
    PermissionPolicy,
    PermissionPrompter,
    PermissionRules,
    parse_permission_rule,
)
from ..security.safety import scan_and_redact_secrets
from ..tools import ToolRegistry
from .errors import (
    AgentError,
    CompactionError,
    McpError,
    ProviderError,
    tool_error_response,
)
from .providers import OpenAICompatibleProvider
from .response_dedup import dedup_repetitive_response
from .session import Message, Session, Usage, UsageTracker

_log = logging.getLogger("yucode.runtime")


EventCallback = Callable[[dict[str, Any]], None]

_AUTO_COMPACT_ENV = "YUCODE_AUTO_COMPACT_INPUT_TOKENS"
_AUTO_COMPACT_ENV_LEGACY = "CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS"
_DEFAULT_AUTO_COMPACT_THRESHOLD = 100_000
# Hard cap on any single tool result to prevent context blowout (32 KB).
# Large outputs (bash stdout, file reads) should use offset/limit instead.
_MAX_TOOL_RESULT_BYTES = 32 * 1024
# After this many dedup blocks in one turn, the agent is stuck: force-stop.
_MAX_STUCK_DEDUP_BLOCKS = 3
# After this many consecutive read-only tool calls with no write/exec, nudge the agent.
_MAX_CONSECUTIVE_READONLY = 8


def _parse_auto_compact_threshold() -> int:
    raw = (
        os.environ.get(_AUTO_COMPACT_ENV, "").strip()
        or os.environ.get(_AUTO_COMPACT_ENV_LEGACY, "").strip()
    )
    if not raw:
        return _DEFAULT_AUTO_COMPACT_THRESHOLD
    try:
        val = int(raw)
        return val if val > 0 else _DEFAULT_AUTO_COMPACT_THRESHOLD
    except ValueError:
        return _DEFAULT_AUTO_COMPACT_THRESHOLD


def _content_stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _shrink_json_strings(node: Any, max_leaf_chars: int) -> bool:
    """Recursively shorten over-long string leaves in *node* (dict/list), in place.

    Returns True if anything was shortened. Handles nested shapes (a list of
    dicts, like read_files' batch result; a dict with a list field, like
    list_directory's entries) so the JSON-safety fix in _truncate_tool_output
    isn't limited to a flat {"stdout": ..., "stderr": ...}-style dict."""
    shrunk = False
    if isinstance(node, dict):
        pairs: Any = node.items()
    elif isinstance(node, list):
        pairs = enumerate(node)
    else:
        return False
    for key, value in pairs:
        if isinstance(value, str) and len(value) > max_leaf_chars:
            node[key] = value[:max_leaf_chars] + f"\n\n[truncated; {len(value):,} chars total]"
            shrunk = True
        elif isinstance(value, (dict, list)) and _shrink_json_strings(value, max_leaf_chars):
            shrunk = True
    return shrunk


def _truncate_tool_output(output: str, max_bytes: int) -> str:
    """Cap *output* at roughly *max_bytes*, keeping JSON structurally valid.

    Naive byte-slicing a JSON-serialized string can land inside a string
    value or before a closing brace, producing invalid JSON that silently
    breaks any downstream json.loads() of the tool result (e.g. the bash
    returncode parser in _record_tool_observation, or a tool's own disk-
    pointer footer for large output). When *output* parses as JSON, its
    largest string leaves (anywhere in the structure) are shortened instead,
    so the result stays valid JSON. Falls back to the previous plain-text
    head truncation for non-JSON tool output, or JSON with no leaf worth
    shrinking (e.g. bloat from many small fields rather than a few large
    ones — rare in practice)."""
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, (dict, list)):
        max_leaf_chars = max(max_bytes // 4, 500)
        if _shrink_json_strings(payload, max_leaf_chars):
            return json.dumps(payload, indent=2, ensure_ascii=False)

    encoded = output.encode("utf-8", errors="replace")
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    last_nl = truncated.rfind("\n")
    if last_nl > max_bytes // 2:
        truncated = truncated[:last_nl]
    return (
        truncated
        + f"\n\n[Output capped at {max_bytes // 1024}KB. "
        "Use offset/limit, a narrower glob/grep pattern, or read specific sections.]"
    )


@dataclass
class TurnSummary:
    final_text: str
    iterations: int
    assistant_messages: list[Message] = field(default_factory=list)
    tool_messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    auto_compaction_performed: bool = False


@dataclass
class _ToolObservations:
    """Per-turn accumulated facts about tool calls — used by grounding checks
    to decide whether the model's final answer is consistent with reality.

    Replaces the earlier ad-hoc grep_had_matches / read_was_called locals so
    additional checks can hang off the same state without growing more flags."""
    grep_had_matches: bool = False
    read_paths: set[str] = field(default_factory=set)
    write_paths: set[str] = field(default_factory=set)
    edit_paths: set[str] = field(default_factory=set)
    last_bash_returncode: int | None = None
    web_searched: bool = False
    web_fetched: bool = False


@dataclass
class _GroundingViolation:
    reason: str   # short machine-readable tag emitted in events
    message: str  # supervisor message injected back into the conversation


_BASH_SUCCESS_PHRASES = (
    "test pass", "tests pass", "all tests pass", "tests passed", "tests are passing",
    "build succeed", "build success", "build passed", "compiled successfully",
    "no errors", "ran successfully", "executed successfully",
    "測試通過", "測試成功", "成功通過", "全部通過", "編譯成功",
)

# Time-sensitive query markers — when a prompt contains any of these, the
# model's final answer must be backed by a fresh web_search/web_fetch
# rather than training memory. Covers English + Chinese (and a few Japanese
# overlaps). Word-boundary anchored where applicable to avoid false positives.
import re as _re_module  # noqa: E402 (top-level import already done; alias for clarity)

# Strong markers — almost always time-sensitive
_TIME_SENSITIVE_RE = _re_module.compile(
    r"\b(?:latest|newest|most\s+recent|nowadays|tonight|right\s+now|upcoming|"
    r"stock\s+price|share\s+price|exchange\s+rate|live\s+score|"
    r"this\s+(?:week|month|year|quarter|season))\b"
    # Chinese / Japanese strong markers
    r"|最新|今天|今日|今年|本年|本月|"
    r"近期|最近|實時|实时|即時|"
    r"股價|股价|匯率|汇率",
    _re_module.IGNORECASE,
)

# Weak markers — only count when followed by a noun that makes them temporal
# (avoids false positives like "current directory", "current file", "current
# state of the code"). The trailing \b is critical so "director" doesn't
# match inside "directory" and "state" doesn't match inside "statement".
_WEAK_TEMPORAL_RE = _re_module.compile(
    r"\b(?:current|currently|today|as\s+of)\s+"
    r"(?:version|release|stable|price|ceo|president|"
    r"government|champion|winner|owner|head|leader|news)\b"
    r"|\b(?:current|latest)\s+(?:prime\s+minister|chairman|governor)\b"
    r"|現在.*(?:是|有什麼|有哪些|多少|誰|價|價格|匯率)"
    r"|目前.*(?:是|有什麼|有哪些|多少|誰|價|價格|匯率)"
    r"|當前.*(?:是|有什麼|有哪些|多少|誰|價|價格|匯率)"
    r"|当前.*(?:是|有什么|有哪些|多少|谁|价|价格|汇率)"
    r"|誰是|谁是",
    _re_module.IGNORECASE,
)


def _is_time_sensitive_prompt(prompt: str) -> bool:
    """Return True if *prompt* asks about something likely to have changed since training."""
    if not prompt:
        return False
    if _TIME_SENSITIVE_RE.search(prompt):
        return True
    return bool(_WEAK_TEMPORAL_RE.search(prompt))


def _check_final_answer_grounding(
    obs: _ToolObservations,
    response_text: str,
    *,
    is_weak_investigation: bool,
    is_time_sensitive: bool = False,
) -> _GroundingViolation | None:
    """Return the first grounding violation in *response_text* given *obs*, or None.

    Checks (in order):
    1. Weak-tier investigation: grep returned matches but no read_file was ever called.
    2. Bash claimed success in final text but last bash returncode was non-zero.
    3. Time-sensitive prompt but the model never called web_search/web_fetch.
    """
    if is_weak_investigation and obs.grep_had_matches and not obs.read_paths:
        return _GroundingViolation(
            reason="grep_matched_but_no_read",
            message=(
                "[SYSTEM SUPERVISOR] You called grep_search and got matches, "
                "but never called read_file. Your previous answer is likely "
                "hallucinated — grep returns ONE LINE of context per match, "
                "which is NOT enough to determine values, defaults, or full "
                "behavior. Before giving any final answer you MUST call "
                "read_file on at least one of the cited files. Do that NOW."
            ),
        )
    if obs.last_bash_returncode is not None and obs.last_bash_returncode != 0:
        lowered = response_text.lower()
        if any(phrase in lowered for phrase in _BASH_SUCCESS_PHRASES):
            return _GroundingViolation(
                reason="claimed_success_but_bash_failed",
                message=(
                    f"[SYSTEM SUPERVISOR] Your final answer claims success "
                    f"(\"passed\" / \"successful\" / \"通過\"), but the most recent "
                    f"bash call returned exit code {obs.last_bash_returncode}. "
                    "Either the command actually failed and you should investigate "
                    "(read the stderr, fix the issue) or you should rewrite your "
                    "answer to acknowledge the failure. Do not claim success on a "
                    "non-zero exit code."
                ),
            )
    if is_time_sensitive and not obs.web_searched and not obs.web_fetched:
        return _GroundingViolation(
            reason="time_sensitive_no_web_search",
            message=(
                "[SYSTEM SUPERVISOR] The user asked about something time-sensitive "
                "(latest / current / today / 最新 / 現在 / 今年 ...), but you answered "
                "without calling web_search or web_fetch. Your training data is "
                "older than today and likely stale on this topic — answering from "
                "memory risks naming dissolved companies, retired products, or "
                "out-of-date facts. Call web_search NOW with a focused query, then "
                "web_fetch the most promising 1-3 URLs, and rewrite your answer "
                "citing those sources."
            ),
        )
    return None


def _prepare_transcript_for_summary(messages: list[dict]) -> str:
    """Flatten the full conversation being compacted into transcript text
    for the LLM summarizer.

    Tool results are dropped (raw tool output doesn't help a summary — the
    summary should capture what the agent learned and did, not re-paste
    stdout/file contents) and each assistant tool call is flattened into a
    short `[Called tools: ...]` annotation instead of full arguments. User
    and assistant text is kept verbatim and untruncated, unlike the previous
    30-message/400-char preview, so the summarizer sees the actual
    conversation rather than a low-detail sample of it."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        if role == "tool":
            continue
        content = str(m.get("content") or "")
        tool_calls = m.get("tool_calls") or []
        if tool_calls:
            names = [tc.get("name", "?") for tc in tool_calls]
            annotation = f"[Called tools: {', '.join(names)}]"
            content = f"{content}\n{annotation}" if content else annotation
        lines.append(f"[{role}]\n{content}")
    return "\n\n".join(lines)


# Structured summary prompt (adapted from grok-build's compaction prompt):
# numbered sections outperform a single "be concise but complete" instruction
# because they force the model to actually enumerate files/errors/pending
# work rather than write one vague paragraph.
_STRUCTURED_SUMMARY_PROMPT = """Your task is to create a detailed summary of the conversation above between a user and a coding agent, paying close attention to the user's explicit requests and all actions taken so far. This summary should be thorough enough that another coding agent could continue the work with no other context than this summary and the original user request.

Your final summary must contain the following sections, in order:

1. Primary Request and Intent: capture all of the user's explicit requests in detail, including any evolution or change of direction over the conversation.
2. Key Technical Concepts: technologies, frameworks, patterns, and architectural decisions discussed.
3. Files and Code Artifacts: every file read, created, or modified, with a summary of why it matters and any code snippets essential for continuation.
4. Errors and Fixes: every error encountered (tool failures, test failures, wrong assumptions) and how it was diagnosed and fixed. Pay special attention to explicit user corrections ("do this differently", "no, ...", confirmations of an approach).
5. Problem Solving: problems solved so far and any ongoing troubleshooting.
6. Pending Work: what remains to be done, in priority order.
7. All User Messages: list every user message verbatim (not tool results) in order — these carry the intent and feedback that must not be lost.

Output the summary directly using the section headings above as plain text. Do not wrap the output in <summary> tags or any other markup — the caller adds that wrapper itself. Do not call any tools."""


# Instruction-file names checked by the AGENTS.md dynamic-discovery reminder
# (subset of memory/prompting.py::discover_instruction_files's list — path-only
# check here, no content reading).
_INSTRUCTION_FILENAMES = ("CLAUDE.md", "YUCODE.md", "AGENTS.md", "CLAW.md")

# Skill-directory relative paths checked by the skill dynamic-discovery
# reminder — matches memory/skills.py::_discover_skill_roots's workspace-
# relative roots (the home-directory roots there aren't relevant to an
# upward walk from a workspace-relative accessed path).
_SKILL_DIR_NAMES = (".yucode/skills", ".claw/skills", ".codex/skills")

# Tool -> its single path-valued argument, for the AGENTS.md discovery check.
_PATH_ARG_TOOLS: dict[str, str] = {
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "list_directory": "path",
    "file_outline": "path",
}


def _extract_accessed_path(tool_name: str, raw_arguments: str) -> str | None:
    key = _PATH_ARG_TOOLS.get(tool_name)
    if key is None:
        return None
    try:
        args = json.loads(raw_arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    value = args.get(key)
    return str(value) if value else None


class AgentRuntime:
    def __init__(
        self,
        workspace_root: Path,
        config: AppConfig,
        *,
        provider: OpenAICompatibleProvider | None = None,
        tool_registry: ToolRegistry | None = None,
        mcp_manager: McpManager | None = None,
        hook_runner: HookRunner | None = None,
        permission_prompter: PermissionPrompter | None = None,
        session: Session | None = None,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.config = config

        perm_rules = PermissionRules(
            allow=[parse_permission_rule(r) for r in config.permission_rules.allow],
            deny=[parse_permission_rule(r) for r in config.permission_rules.deny],
            ask=[parse_permission_rule(r) for r in config.permission_rules.ask],
        )
        self.permission_policy = PermissionPolicy(config.runtime.permission_mode, rules=perm_rules)
        self.permission_prompter = permission_prompter
        self.permission_enforcer = PermissionEnforcer(self.permission_policy, self.workspace_root)
        self.session = session or Session(model=config.provider.model)
        self.mcp_manager = mcp_manager if mcp_manager is not None else (McpManager(config.mcp) if config.mcp else None)

        self.plugin_manager = plugin_manager or PluginManager(self.workspace_root)
        self.plugin_registry = self.plugin_manager.build_registry(enabled_only=True)
        plugin_tools = self.plugin_registry.all_tools()
        plugin_hooks = self.plugin_registry.all_hook_commands()

        self.tools = tool_registry or ToolRegistry(
            self.workspace_root, config, self.mcp_manager,
            plugin_tools=plugin_tools or None,
        )

        for name, perm in self.tools.permission_specs():
            self.permission_policy.with_tool_requirement(name, perm)

        self.provider = provider or OpenAICompatibleProvider(config.provider)

        hook_config = HookConfig(
            pre_tool_use=list(config.hooks.pre_tool_use) + plugin_hooks.pre_tool_use,
            post_tool_use=list(config.hooks.post_tool_use) + plugin_hooks.post_tool_use,
            post_tool_use_failure=list(config.hooks.post_tool_use_failure) + getattr(plugin_hooks, "post_tool_use_failure", []),
            pre_compact=list(config.hooks.pre_compact),
            post_compact=list(config.hooks.post_compact),
        )
        self.hook_runner = hook_runner or HookRunner(hook_config)
        self.usage_tracker = UsageTracker.from_session(self.session)
        self._compaction_config = CompactionConfig(
            preserve_recent_messages=config.runtime.compact_preserve_recent,
            max_estimated_tokens=config.runtime.compact_token_threshold,
            strategy=config.runtime.compact_strategy,
            llm_compactor=self._make_llm_compactor() if config.runtime.compact_strategy == "llm" else None,
        )
        self._auto_compact_threshold = _parse_auto_compact_threshold()
        audit_logger = AuditLogger(self.workspace_root, enabled=config.audit.enabled)
        self.metrics = MetricsCollector(audit_logger=audit_logger)

        self._plugins_initialized = False
        self._cancel_event = threading.Event()

        # Session-scoped reminder bookkeeping (persists across run_turn()
        # calls within one interactive session — see _collect_post_tool_reminders).
        self._turns_since_todo_write = 0
        self._turns_since_todo_nudge = 1000  # eligible to nudge from the start
        self._agents_md_checked: set[Path] = set()
        self._skill_dirs_checked: set[Path] = set()
        self._announced_skill_files: set[Path] = set()
        # Files written/edited across the whole session (not just the current
        # turn) — snapshotted into the compaction state reminder so "what did
        # I touch" survives being summarized away.
        self._session_edited_paths: set[str] = set()
        # input_tokens from the most recent provider response — see
        # _maybe_auto_compact, which gates on this instead of a lifetime sum.
        self._last_response_input_tokens = 0

    def _ensure_plugins_initialized(self) -> None:
        if self._plugins_initialized:
            return
        self._plugins_initialized = True
        try:
            self.plugin_registry.init_plugins()
        except Exception as exc:
            _log.warning("Plugin init failed: %s", exc)

    def _shutdown_plugins(self) -> None:
        if self._plugins_initialized:
            try:
                self.plugin_registry.shutdown_plugins()
            except Exception as exc:
                _log.warning("Plugin shutdown failed: %s", exc)

    def cancel(self) -> None:
        """Signal any in-progress provider stream to stop at its next poll interval."""
        self._cancel_event.set()

    @classmethod
    def from_workspace(
        cls,
        workspace_root: str | Path,
        config_path: str | None = None,
        *,
        session: Session | None = None,
    ) -> AgentRuntime:
        from ..config import is_dangerous_mode

        runtime = cls(Path(workspace_root), load_app_config(config_path), session=session)
        if is_dangerous_mode():
            _log.warning("YUCODE_DANGEROUS_MODE is enabled -- all safety checks are bypassed")
            runtime.metrics.record_security_event(
                "dangerous_mode_enabled", "", "Safety checks bypassed via YUCODE_DANGEROUS_MODE"
            )
        return runtime

    # ------------------------------------------------------------------
    # orchestrate (top-level entry)
    # ------------------------------------------------------------------

    @property
    def orchestration_mode(self) -> str:
        return self.config.runtime.orchestration_mode

    @property
    def compact_preserve_recent(self) -> int:
        return self.config.runtime.compact_preserve_recent

    @property
    def auto_resume_latest(self) -> bool:
        return self.config.runtime.auto_resume_latest

    def orchestrate(
        self,
        prompt: str,
        event_callback: EventCallback | None = None,
    ) -> TurnSummary:
        from .coordinator import AdminCoordinator, is_complex_prompt

        self._ensure_plugins_initialized()

        mode = self.config.runtime.orchestration_mode
        tier = self.config.provider.resolved_tier()
        use_coordinator = (
            mode == "multi"
            or (mode == "auto" and is_complex_prompt(prompt, intelligence_tier=tier))
        )

        if not use_coordinator:
            return self.run_turn(prompt, event_callback)

        coordinator = AdminCoordinator(
            self.workspace_root,
            self.config,
            provider=self.provider,
            mcp_manager=self.mcp_manager,
        )
        if event_callback:
            event_callback({
                "type": "provider_info",
                "provider": self.config.provider.name,
                "model": self.config.provider.model,
            })
        result = coordinator.orchestrate(prompt, event_callback)
        self.session.usage.add(result.usage)
        self.usage_tracker.record(result.usage)
        self.metrics.record_session(result.iterations, result.usage)

        summary = TurnSummary(
            final_text=result.final_text,
            iterations=result.iterations,
            usage=result.usage,
        )
        return summary

    # ------------------------------------------------------------------
    # run_turn (single-agent loop)
    # ------------------------------------------------------------------

    def run_turn(
        self,
        prompt: str,
        event_callback: EventCallback | None = None,
        max_steps_override: int | None = None,
    ) -> TurnSummary:
        self._ensure_plugins_initialized()

        if event_callback:
            event_callback({
                "type": "provider_info",
                "provider": self.config.provider.name,
                "model": self.config.provider.model,
            })

        date_text = datetime.now().strftime("%Y-%m-%d")
        project_context = discover_project_context(
            self.workspace_root,
            current_date=date_text,
            include_git_context=self.config.runtime.include_git_context,
            explicit_instruction_files=self.config.instruction_files,
        )
        # Seed the AGENTS.md-discovery "already told the model about this
        # file" set with what's already in the system prompt, so the
        # discovery reminder only ever surfaces files NOT already in context.
        self._agents_md_checked.update(f.path for f in project_context.instruction_files)
        # Same idea for skills already listed in the system prompt's skill summary.
        from ..memory.skills import list_skills as _list_skills_for_seeding
        self._announced_skill_files.update(s.path for s in _list_skills_for_seeding(self.workspace_root))
        prior_message_count = len(self.session.messages)
        system_prompt = PromptAssembler(
            self.config,
            project_context,
            resumed_messages=prior_message_count,
            estimated_tokens=self.estimated_tokens(),
        ).render()
        # Grounding nudge: weak models often answer investigation prompts from
        # memory without reading any source. Front-load an explicit directive
        # so the first response has to call grep_search or read_file.
        effective_prompt = prompt
        if self.config.provider.resolved_tier() == "weak":
            from .coordinator import looks_like_investigation
            if looks_like_investigation(prompt):
                effective_prompt = (
                    f"{prompt}\n\n"
                    "[System: investigation task. Before answering you MUST call "
                    "grep_search or read_file at least once. Answering from general "
                    "knowledge without inspecting the actual workspace source is a "
                    "failure mode — cite specific file paths and line numbers.]"
                )
        self.session.add_message(Message(role="user", content=effective_prompt))

        self._cancel_event.clear()

        max_steps = max_steps_override or self.config.runtime.max_iterations
        max_tool_calls = self.config.runtime.max_tool_calls
        dedup_threshold = self.config.runtime.dedup_tool_threshold
        # Weak models loop more readily — block the 2nd identical call instead
        # of the 3rd. (Observed: Qwen3 looped 8× on the same read_file path.)
        if self.config.provider.resolved_tier() == "weak":
            dedup_threshold = max(2, dedup_threshold - 1)
        tool_call_count = 0
        dedup_counts: dict[tuple[str, str], int] = {}
        dedup_blocks_this_turn = 0   # how many hard dedup stops have fired
        _last_dedup_tool = ""        # most recently blocked tool name
        budget_exhausted = False
        consecutive_readonly_calls = 0  # read-only calls with no write/exec in between
        _tool_cache: dict[tuple[str, str], str] = {}  # within-turn cache for read-only tools
        # Grounding observations + supervisor — track tool-call facts and run
        # consistency checks against the model's final answer. Originally added
        # for the v6 C3 failure (Qwen3 grep'd `compact_token_threshold`, never
        # read, hallucinated "10,000"); now also covers bash-success mismatches.
        from .coordinator import looks_like_investigation as _looks_inv
        is_weak_investigation = (
            self.config.provider.resolved_tier() == "weak"
            and _looks_inv(prompt)
        )
        is_time_sensitive = _is_time_sensitive_prompt(prompt)
        observations = _ToolObservations()
        forced_retry_done = False
        todo_gate_fires_this_turn = 0

        # Per-turn reasoning_effort: only touched if the provider config already
        # opts in via extra_body.reasoning_effort (e.g. Workbench/GPT-5.5) —
        # providers that don't understand the param are left untouched. Computed
        # once per turn (not per iteration) since the whole turn is one task.
        reasoning_effort_overrides: dict[str, Any] | None = None
        if self.config.provider.extra_body.get("reasoning_effort"):
            from .coordinator import classify_reasoning_effort
            effort = classify_reasoning_effort(prompt, intelligence_tier=self.config.provider.resolved_tier())
            reasoning_effort_overrides = {"reasoning_effort": effort}

        summary = TurnSummary(final_text="", iterations=0)
        for iteration in range(1, max_steps + 1):
            summary.iterations = iteration
            if event_callback:
                event_callback({"type": "iteration_started", "iteration": iteration})
            # Reset (not increment) inline below if this iteration calls
            # todo_write, so a call this same iteration still nets to 0.
            self._turns_since_todo_write += 1
            self._turns_since_todo_nudge += 1

            if budget_exhausted:
                break

            if should_compact(self.session.messages, self._compaction_config):
                try:
                    compaction = self.compact()
                    if event_callback and compaction.removed_message_count > 0:
                        event_callback({
                            "type": "compaction",
                            "removed": compaction.removed_message_count,
                            "summary_length": len(compaction.summary),
                        })
                except Exception as exc:
                    _log.warning("Compaction failed: %s", exc)
                    if self.config.runtime.error_strategy == "strict":
                        raise CompactionError(str(exc)) from exc

            try:
                response = self.provider.complete(
                    self.session.provider_messages(system_prompt, pruning=self.config.runtime),
                    self.tools.definitions_for_provider(),
                    stream_callback=event_callback,
                    cancel_event=self._cancel_event,
                    body_overrides=reasoning_effort_overrides,
                )
            except Exception as exc:
                _log.error("Provider call failed: %s", exc)
                if self.config.runtime.error_strategy == "strict":
                    if isinstance(exc, AgentError):
                        raise
                    raise ProviderError(str(exc)) from exc
                summary.final_text = f"[Provider error: {exc}. The task could not be completed.]"
                if event_callback:
                    event_callback({"type": "error", "error": str(exc), "category": "provider_error"})
                return summary

            self.session.usage.add(response.usage)
            self.usage_tracker.record(response.usage)
            self._last_response_input_tokens = response.usage.input_tokens

            # Surface gateway-returned chain-of-thought (reasoning_content)
            # for observability — it is NOT stored in the session or sent
            # back to the provider, so emitting it here is the only place
            # it becomes visible (e.g. to diagnose_agent.py's trace).
            # getattr: duck-typed providers (test fakes, mocks) may return
            # response objects without the reasoning field.
            reasoning_text = getattr(response, "reasoning", "")
            if reasoning_text and event_callback:
                event_callback({
                    "type": "reasoning",
                    "iteration": iteration,
                    "chars": len(reasoning_text),
                    "content": reasoning_text,
                })

            assistant_message = Message(
                role="assistant",
                content=response.text,
                tool_calls=response.tool_calls,
            )
            self.session.add_message(assistant_message)
            summary.assistant_messages.append(assistant_message)
            summary.usage.add(response.usage)

            if not response.tool_calls:
                # Grounding supervisor: run consistency checks against the
                # accumulated tool observations. Fires at most once per turn.
                if not forced_retry_done:
                    violation = _check_final_answer_grounding(
                        observations, response.text,
                        is_weak_investigation=is_weak_investigation,
                        is_time_sensitive=is_time_sensitive,
                    )
                    if violation is not None:
                        forced_retry_done = True
                        self.session.add_message(Message(
                            role="user",
                            content=violation.message,
                        ))
                        if event_callback:
                            event_callback({
                                "type": "grounding_retry",
                                "reason": violation.reason,
                            })
                        continue
                # Todo turn-end gate (opt-in): don't let the model stop with
                # pending/in_progress todos still open. Separate fire budget
                # from the grounding supervisor above.
                if (
                    self.config.runtime.todo_gate_enabled
                    and todo_gate_fires_this_turn < self.config.runtime.todo_gate_max_fires_per_prompt
                ):
                    open_todos = self._read_open_todos()
                    if open_todos:
                        todo_gate_fires_this_turn += 1
                        listing = "\n".join(
                            f"- [{t.get('status', 'pending')}] {t.get('content', '?')}"
                            for t in open_todos[:10]
                        )
                        self.session.add_message(Message(
                            role="user",
                            content=(
                                "[SYSTEM SUPERVISOR] You still have pending/in_progress todos:\n"
                                f"{listing}\n"
                                "Continue working on them, or call todo_write to mark them "
                                "completed/cancelled if they're no longer relevant, before "
                                "giving your final answer."
                            ),
                        ))
                        if event_callback:
                            event_callback({
                                "type": "todo_gate_retry",
                                "fires_this_turn": todo_gate_fires_this_turn,
                                "open_todos": len(open_todos),
                            })
                        continue
                summary.final_text = dedup_repetitive_response(response.text)
                if not response.text and response.usage.total_tokens() == 0 and iteration == 1:
                    if event_callback:
                        event_callback({
                            "type": "error",
                            "error": (
                                "The provider returned an empty response with 0 tokens. "
                                "This usually means your provider configuration is wrong."
                            ),
                            "category": "empty_response",
                        })
                    else:
                        _log.warning(
                            "Provider returned empty text with zero token usage on the "
                            "first iteration. This usually indicates a provider "
                            "configuration problem (wrong base_url, chat_path, "
                            "append_chat_path, verify_tls, model, or API key). "
                            "Run `yucode doctor --workspace .` to diagnose."
                        )
                if event_callback:
                    event_callback({"type": "completed", "text": response.text})
                self.metrics.record_session(summary.iterations, summary.usage)
                self._maybe_auto_compact(summary, event_callback)
                return summary

            for tool_call in response.tool_calls:
                if event_callback:
                    event_callback({
                        "type": "tool_call",
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                        "tool_call_id": tool_call.id,
                    })

                # Canonicalize args before hashing so two semantically identical
                # calls with different JSON formatting (whitespace, key order)
                # collapse into the same dedup key. Qwen3 frequently emits
                # `{"a":1,"b":2}` and `{"a": 1, "b": 2}` for the same intent.
                try:
                    canonical_args = json.dumps(
                        json.loads(tool_call.arguments or "{}"),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                except (json.JSONDecodeError, TypeError):
                    canonical_args = tool_call.arguments or ""
                call_key = (tool_call.name, _content_stable_hash(canonical_args))
                dedup_counts[call_key] = dedup_counts.get(call_key, 0) + 1

                if dedup_counts[call_key] >= dedup_threshold:
                    dedup_blocks_this_turn += 1
                    _last_dedup_tool = tool_call.name
                    if dedup_blocks_this_turn >= _MAX_STUCK_DEDUP_BLOCKS:
                        escalation = (
                            f" FINAL WARNING ({dedup_blocks_this_turn}/{_MAX_STUCK_DEDUP_BLOCKS}): "
                            "The system will force-stop after this response. "
                            "You MUST provide your final answer NOW — no more tool calls."
                        )
                    elif dedup_blocks_this_turn > 1:
                        escalation = (
                            f" You have been hard-blocked {dedup_blocks_this_turn} time(s) "
                            "this turn. Stop looping and answer with what you have."
                        )
                    else:
                        escalation = ""
                    tool_result_content = json.dumps({
                        "error": (
                            f"HARD STOP: `{tool_call.name}` called {dedup_threshold}+ times "
                            "with identical arguments — this is a hard limit you cannot bypass. "
                            "You MUST do one of: (a) use different arguments, "
                            "(b) switch to a different tool, or "
                            f"(c) stop and answer the user with what you have.{escalation}"
                        ),
                        "error_code": "dedup_limit",
                    }, indent=2)
                    if event_callback:
                        event_callback({
                            "type": "dedup_limit",
                            "tool": tool_call.name,
                            "blocks_this_turn": dedup_blocks_this_turn,
                        })
                elif tool_call_count >= max_tool_calls:
                    tool_result_content = json.dumps({
                        "error": (
                            f"Tool call budget exhausted ({max_tool_calls} calls). "
                            "Summarize your findings and respond to the user."
                        ),
                    }, indent=2)
                    budget_exhausted = True
                else:
                    cached = _tool_cache.get(call_key)
                    if cached is not None:
                        tool_result_content = cached
                    else:
                        tool_result_content = self._execute_tool(
                            tool_call.name, tool_call.arguments,
                        )
                        tool_call_count += 1
                        # Cache read-only results to skip redundant re-reads within this turn
                        try:
                            if self.tools.permission_for(tool_call.name) == "read-only":
                                _tool_cache[call_key] = tool_result_content
                        except KeyError:
                            pass
                    # Update grounding observations for the supervisor.
                    self._record_tool_observation(
                        observations, tool_call.name, tool_call.arguments, tool_result_content,
                    )
                    self._session_edited_paths.update(observations.write_paths)
                    self._session_edited_paths.update(observations.edit_paths)
                    if tool_call.name == "todo_write":
                        self._turns_since_todo_write = 0
                    # Track read-only streak (cache hits still count as reads)
                    try:
                        perm = self.tools.permission_for(tool_call.name)
                    except KeyError:
                        perm = "unknown"
                    if perm == "read-only":
                        consecutive_readonly_calls += 1
                    else:
                        consecutive_readonly_calls = 0
                    # Nudge the model once when it crosses the read-only threshold
                    if consecutive_readonly_calls == _MAX_CONSECUTIVE_READONLY:
                        tool_result_content = (
                            tool_result_content
                            + f"\n\n[Advisory: {consecutive_readonly_calls} consecutive "
                            "read-only calls without writing or executing anything. "
                            "If you have gathered enough information, act on it now — "
                            "write files, run commands, or answer the user. "
                            "Avoid reading more unless strictly necessary.]"
                        )

                for reminder in self._collect_post_tool_reminders(tool_call.name, tool_call.arguments):
                    tool_result_content = f"{tool_result_content}\n\n{reminder}"

                tool_message = Message(
                    role="tool",
                    content=tool_result_content,
                    tool_call_id=tool_call.id,
                )
                self.session.add_message(tool_message)
                summary.tool_messages.append(tool_message)
                if event_callback:
                    event_callback({
                        "type": "tool_result",
                        "name": tool_call.name,
                        "tool_call_id": tool_call.id,
                        "content": tool_result_content,
                    })

        # -------------------------------------------------------
        # Forced exit: the model is clearly stuck in a dedup loop
        # -------------------------------------------------------
        if dedup_blocks_this_turn >= _MAX_STUCK_DEDUP_BLOCKS:
            last_text = ""
            for msg in reversed(summary.assistant_messages):
                if msg.content and msg.content.strip():
                    last_text = msg.content
                    break
            summary.final_text = (
                (last_text + "\n\n" if last_text else "")
                + f"[Stopped: `{_last_dedup_tool}` was repeatedly blocked "
                f"({dedup_blocks_this_turn}×) in one turn. The agent appears stuck. "
                "Rephrase your request or use /clear to start fresh.]"
            )
            if event_callback:
                event_callback({
                    "type": "stuck_exit",
                    "tool": _last_dedup_tool,
                    "blocks": dedup_blocks_this_turn,
                })
            self.metrics.record_session(summary.iterations, summary.usage)
            self._maybe_auto_compact(summary, event_callback)
            return summary

        last_text = ""
        for msg in reversed(summary.assistant_messages):
            if msg.content and msg.content.strip():
                last_text = msg.content
                break
        reason = "tool call budget" if budget_exhausted else "iterations"
        limit = max_tool_calls if budget_exhausted else max_steps
        summary.final_text = (
            last_text
            + f"\n\n[Note: Reached the maximum number of {reason} "
            f"({limit}). "
            "The task may be incomplete. Use /resume to continue.]"
        )
        if event_callback:
            event_callback({"type": "completed", "text": summary.final_text})
        self.metrics.record_session(summary.iterations, summary.usage)
        self._maybe_auto_compact(summary, event_callback)
        return summary

    # ------------------------------------------------------------------
    # Post-turn auto-compaction (Rust parity: cumulative input tokens)
    # ------------------------------------------------------------------

    def _maybe_auto_compact(
        self,
        summary: TurnSummary,
        event_callback: EventCallback | None = None,
    ) -> None:
        # Gated on the MOST RECENT response's input tokens, not a lifetime
        # cumulative sum. The cumulative sum only ever grows across a long
        # interactive session — even after a compaction shrinks the actual
        # context back down — so once it crossed the threshold once, this
        # used to force-compact on every subsequent turn forever, regardless
        # of the real current context size.
        trigger_tokens = self._last_response_input_tokens
        if trigger_tokens < self._auto_compact_threshold:
            return
        try:
            archive_path = self._archive_before_compact()
            force_config = dataclasses.replace(
                self._compaction_config,
                max_estimated_tokens=0,
                transcript_path=str(archive_path) if archive_path is not None else None,
                state_reminder=self._build_compaction_state_reminder(),
            )
            result = compact_session(self.session.messages, force_config)
            if result.removed_message_count > 0:
                self.session.messages = result.compacted_messages
                summary.auto_compaction_performed = True
                _log.info(
                    "Auto-compacted %d messages (last response: %d input tokens >= threshold %d)",
                    result.removed_message_count, trigger_tokens, self._auto_compact_threshold,
                )
                if event_callback:
                    event_callback({
                        "type": "auto_compaction",
                        "removed": result.removed_message_count,
                        "threshold": self._auto_compact_threshold,
                        "trigger_input_tokens": trigger_tokens,
                    })
        except Exception as exc:
            _log.warning("Auto-compaction failed: %s", exc)

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def _make_llm_compactor(self):  # type: ignore[return]
        """Return a callable that uses the provider to summarise dropped messages.

        Retries once (2 attempts total) on either a provider error or a
        degenerate (near-empty) response before giving up; `_llm_summarize`
        (memory/compact.py) falls back to the deterministic heuristic
        summarizer when this returns an empty string."""
        provider = self.provider

        def _compactor(messages: list[dict]) -> str:
            transcript = _prepare_transcript_for_summary(messages)
            prompt = f"{transcript}\n\n{_STRUCTURED_SUMMARY_PROMPT}"
            text = ""
            for attempt in range(1, 3):
                try:
                    response = provider.complete(
                        [{"role": "user", "content": prompt}],
                        tools=[],
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning("LLM compactor failed (attempt %d/2): %s", attempt, exc)
                    continue
                text = response.text or ""
                if not is_degenerate_summary(text):
                    return text
                _log.info(
                    "LLM compactor produced a degenerate summary (%d chars, attempt %d/2); retrying",
                    len(text), attempt,
                )
            _log.warning("LLM compactor still failing after retry (%d chars); falling back to heuristic", len(text))
            return ""

        return _compactor

    def compact(self, config: CompactionConfig | None = None) -> CompactionResult:
        cfg = config or self._compaction_config
        msg_count = len(self.session.messages)
        est_tokens = self.estimated_tokens()

        self.hook_runner.run_pre_compact(msg_count, est_tokens)

        archive_path = self._archive_before_compact()
        cfg = dataclasses.replace(
            cfg,
            transcript_path=str(archive_path) if archive_path is not None else cfg.transcript_path,
            state_reminder=self._build_compaction_state_reminder(),
        )

        result = compact_session(self.session.messages, cfg)
        if result.removed_message_count > 0:
            self.session.messages = result.compacted_messages
            self.hook_runner.run_post_compact(result.removed_message_count, len(result.summary))
            _log.info(
                "Compacted %d messages (est. %d tokens -> %d tokens)",
                result.removed_message_count, est_tokens, self.estimated_tokens(),
            )
        return result

    def _archive_before_compact(self) -> Path | None:
        """Write the full pre-compaction conversation to disk and return its path.

        Stores complete message content (not a truncated preview) so the
        model can read it back via `compact.py`'s transcript-pointer feature
        when the compaction summary drops detail it later needs."""
        try:
            import uuid

            from ..config.settings import state_dir
            archives_dir = state_dir(self.workspace_root) / "archives"
            archives_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            sid = self.session.session_id if hasattr(self.session, "session_id") else "unknown"
            archive_path = archives_dir / f"{sid}_{ts}_{uuid.uuid4().hex[:8]}.json"
            data = {
                "session_id": sid,
                "timestamp": ts,
                "message_count": len(self.session.messages),
                "messages": [m.to_dict() for m in self.session.messages],
            }
            archive_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            return archive_path
        except Exception as exc:
            _log.warning("Failed to archive session before compaction: %s", exc)
            return None

    def _build_compaction_state_reminder(self) -> str | None:
        """Snapshot session-level state — edited files, open todos, running
        background tasks — into a `<system-reminder>` block appended to the
        compaction continuation (see CompactionConfig.state_reminder).

        Compaction replaces most of the conversation with an LLM- or
        heuristic-generated summary; concrete facts like "which files did I
        touch" are easy for a summary to blur or drop, and a still-running
        background task the model forgets about is a real cost (never
        collected, never reported). Snapshotting them structurally here
        means they survive regardless of summary quality. Returns None when
        there's nothing to report, so callers can leave state_reminder unset."""
        sections: list[str] = []
        if self._session_edited_paths:
            paths = sorted(self._session_edited_paths)[:30]
            sections.append("Files edited this session:\n" + "\n".join(f"- {p}" for p in paths))
        try:
            open_todos = self._read_open_todos()
        except Exception:  # noqa: BLE001 — never let a reminder block compaction itself
            open_todos = []
        if open_todos:
            listing = "\n".join(
                f"- [{t.get('status', 'pending')}] {t.get('content', '?')}" for t in open_todos[:15]
            )
            sections.append("Open todos:\n" + listing)
        running = [t for t in self.tools.background_tasks.values() if t.popen.poll() is None]
        if running:
            listing = "\n".join(f'- "{t.task_id}": {t.command}' for t in running)
            sections.append("Background tasks still running:\n" + listing)
        if not sections:
            return None
        return "<system-reminder>\n" + "\n\n".join(sections) + "\n</system-reminder>"

    def estimated_tokens(self) -> int:
        return estimate_session_tokens(self.session.messages)

    def save_session(self, session_id: str | None = None) -> Path:
        import uuid
        sid = session_id or str(uuid.uuid4())[:8]
        return self.session.save_to_workspace(self.workspace_root, sid)

    def checkpoint(self, label: str = "") -> Path:
        from ..config.settings import state_dir
        checkpoints_dir = state_dir(self.workspace_root) / "checkpoints"
        checkpoints_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cp_path = checkpoints_dir / f"checkpoint_{ts}.json"
        data = {
            "label": label,
            "timestamp": ts,
            "estimated_tokens": self.estimated_tokens(),
            "message_count": len(self.session.messages),
            "metrics": self.metrics.to_dict(),
            "session": {
                "model": self.session.model,
                "messages": [
                    {
                        "role": m.role,
                        "content": m.content,
                        "tool_calls": [
                            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                            for tc in m.tool_calls
                        ],
                        "tool_call_id": m.tool_call_id,
                    }
                    for m in self.session.messages
                ],
            },
        }
        cp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        _log.info("Checkpoint saved to %s", cp_path)
        return cp_path

    # ------------------------------------------------------------------
    # Post-tool-call reminders — cross-cutting, session-scoped checks run
    # after every tool call and appended to that call's tool result.
    #
    # Each check tracks its own "already reported" state (a `reported` flag,
    # a seen-paths set, a turn counter) so a given fact — a completed
    # background task, a todo nudge, a newly-found instruction file — is
    # surfaced at most once rather than repeating on every subsequent call.
    # This is the generalised form of the older single-purpose
    # consecutive-readonly advisory (still inline in run_turn(), since it
    # depends on per-turn loop-local counters rather than session state).
    # ------------------------------------------------------------------

    def _collect_post_tool_reminders(self, tool_name: str, raw_arguments: str) -> list[str]:
        reminders: list[str] = []
        bg = self._check_completed_background_tasks()
        if bg:
            reminders.append(bg)
        sub = self._check_completed_subagent_tasks()
        if sub:
            reminders.append(sub)
        nudge = self._check_todo_nudge()
        if nudge:
            reminders.append(nudge)
        agents_md = self._check_agents_md_discovery(tool_name, raw_arguments)
        if agents_md:
            reminders.append(agents_md)
        skill = self._check_skill_discovery(tool_name, raw_arguments)
        if skill:
            reminders.append(skill)
        return reminders

    def _check_completed_background_tasks(self) -> str:
        """Poll tracked background bash tasks for completions since the last
        check. Returns a `<system-reminder>` block for any newly-finished
        task (each reported exactly once, tracked via BackgroundTask.reported
        on the registry), or "" if nothing new to report.

        Called after every tool call in run_turn() so the model learns about
        a completion on its next turn instead of never checking back in —
        previously `run_in_background=true` output went to DEVNULL and was
        unrecoverable."""
        import time as _time
        lines: list[str] = []
        for task in self.tools.background_tasks.values():
            if task.reported:
                continue
            returncode = task.popen.poll()
            if returncode is None:
                continue
            task.reported = True
            elapsed = _time.monotonic() - task.started_at
            try:
                content = task.output_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            preview = content[-2000:] if len(content) > 2000 else content
            preview_note = f" (last 2,000 of {len(content):,} chars)" if len(content) > 2000 else ""
            lines.append(
                f'Background task "{task.task_id}" completed (exit code: {returncode}, '
                f"ran {elapsed:.1f}s). Command: {task.command}\n"
                f"Output{preview_note}:\n{preview}\n"
                f"Full output at: {task.output_path}"
            )
        if not lines:
            return ""
        return "<system-reminder>\n" + "\n\n---\n\n".join(lines) + "\n</system-reminder>"

    def _check_completed_subagent_tasks(self) -> str:
        """Poll tracked background subagents (spawned via the `agent` tool
        with run_in_background=true, or auto-backgrounded on timeout) for
        completions since the last check. Mirrors
        _check_completed_background_tasks for bash commands, but a
        subagent's "output" is a full run_turn() result rather than a
        stdout stream -- see tools/agent_tool.py."""
        import time as _time
        lines: list[str] = []
        for task in self.tools.subagent_tasks.values():
            if task.reported or task.thread.is_alive():
                continue
            task.reported = True
            elapsed = _time.monotonic() - task.started_at
            prompt_preview = task.prompt[:200] + ("..." if len(task.prompt) > 200 else "")
            if task.error_holder:
                lines.append(
                    f'Sub-agent "{task.task_id}" failed after {elapsed:.1f}s '
                    f"(prompt: {prompt_preview!r}): {task.error_holder[0]}"
                )
            elif task.result_holder:
                result_text = task.result_holder[0]
                preview = result_text[-4000:] if len(result_text) > 4000 else result_text
                preview_note = f" (last 4,000 of {len(result_text):,} chars)" if len(result_text) > 4000 else ""
                lines.append(
                    f'Sub-agent "{task.task_id}" completed (ran {elapsed:.1f}s, '
                    f"prompt: {prompt_preview!r}).\nResult{preview_note}:\n{preview}"
                )
            else:
                lines.append(
                    f'Sub-agent "{task.task_id}" finished with no output after {elapsed:.1f}s '
                    f"(prompt: {prompt_preview!r})."
                )
        if not lines:
            return ""
        return "<system-reminder>\n" + "\n\n---\n\n".join(lines) + "\n</system-reminder>"

    def _check_todo_nudge(self) -> str:
        """Remind the model to use todo_write if it hasn't in a while.

        Fires at most once per `todo_nudge_min_gap_turns` iterations, and
        only once `todo_nudge_turns_since_write` iterations have passed
        without a todo_write call — both counters are session-scoped (see
        __init__), not reset per run_turn() call, matching how a real
        interactive session's todo list is expected to span multiple turns."""
        rt = self.config.runtime
        if not rt.todo_nudge_enabled:
            return ""
        if self._turns_since_todo_write < rt.todo_nudge_turns_since_write:
            return ""
        if self._turns_since_todo_nudge < rt.todo_nudge_min_gap_turns:
            return ""
        self._turns_since_todo_nudge = 0
        return (
            "<system-reminder>\n"
            f"You haven't called todo_write in {self._turns_since_todo_write} turns. "
            "If this task has multiple steps, track them with todo_write so progress "
            "isn't lost. Skip this if the task is simple enough not to need a todo list.\n"
            "</system-reminder>"
        )

    def _read_open_todos(self) -> list[dict[str, Any]]:
        """Return todo entries whose status is pending/in_progress, or [] if
        there's no todo list yet or it can't be read. Used by the turn-end
        gate (run_turn) to decide whether to force another iteration."""
        from ..config.settings import state_dir
        todos_path = state_dir(self.workspace_root) / "todos.json"
        if not todos_path.exists():
            return []
        try:
            data = json.loads(todos_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, list):
            return []
        return [
            t for t in data
            if isinstance(t, dict) and str(t.get("status", "pending")).lower() in ("pending", "in_progress")
        ]

    def _discover_new_instruction_files(self, accessed_path: Path) -> list[Path]:
        """Walk up from *accessed_path* (a file or directory) to
        workspace_root, checking each level for instruction files not
        already known this session. Newly-found files are marked seen (so
        they're only ever returned once) and returned.

        Bounded at workspace_root — a monorepo subpackage's own AGENTS.md is
        exactly the case the initial cwd-rooted discovery in
        memory/prompting.py misses (it only walks UP from cwd, so a
        DESCENDANT directory's instruction file is never found until now)."""
        try:
            resolved = accessed_path.resolve()
        except OSError:
            return []
        current = resolved.parent if resolved.is_file() else resolved
        ws_root = self.workspace_root.resolve()
        found: list[Path] = []
        for _ in range(10):  # matches grok-build's MAX_WALK_DEPTH
            for name in _INSTRUCTION_FILENAMES:
                candidate = current / name
                if candidate in self._agents_md_checked:
                    continue
                self._agents_md_checked.add(candidate)  # don't re-stat a miss either
                if candidate.is_file():
                    found.append(candidate)
            if current == ws_root or ws_root not in current.parents:
                break
            current = current.parent
        return found

    def _check_agents_md_discovery(self, tool_name: str, raw_arguments: str) -> str:
        if not self.config.runtime.agents_md_discovery_enabled:
            return ""
        accessed = _extract_accessed_path(tool_name, raw_arguments)
        if accessed is None:
            return ""
        try:
            target = self.tools._resolve_path(accessed)
        except (OSError, ValueError):
            return ""
        found = self._discover_new_instruction_files(target)
        if not found:
            return ""
        rel_paths: list[str] = []
        for p in found:
            try:
                rel_paths.append(str(p.relative_to(self.workspace_root)))
            except ValueError:
                rel_paths.append(str(p))
        listing = "\n".join(f"- {p}" for p in rel_paths)
        return (
            "<system-reminder>\n"
            "Found project instruction file(s) not yet in context, near a path "
            f"you just accessed:\n{listing}\n"
            "Read it with read_file if it might contain rules relevant to this "
            "area of the codebase.\n"
            "</system-reminder>"
        )

    def _discover_new_skills_near_path(self, accessed_path: Path) -> list:  # -> list[SkillInfo]
        """Walk up from *accessed_path* to workspace_root, checking each
        level for skill directories (.yucode/skills, .claw/skills,
        .codex/skills) not already known this session. Mirrors
        _discover_new_instruction_files (same walk-and-memoize shape) but
        for skill directories, returning SkillInfo entries instead of bare
        paths so the reminder can include each skill's description."""
        from ..memory.skills import SkillInfo, _parse_frontmatter
        try:
            resolved = accessed_path.resolve()
        except OSError:
            return []
        current = resolved.parent if resolved.is_file() else resolved
        ws_root = self.workspace_root.resolve()
        found: list[SkillInfo] = []
        for _ in range(10):  # matches grok-build's MAX_WALK_DEPTH
            for dirname in _SKILL_DIR_NAMES:
                skill_root = current / dirname
                if skill_root in self._skill_dirs_checked:
                    continue
                self._skill_dirs_checked.add(skill_root)
                if not skill_root.is_dir():
                    continue
                for child in sorted(skill_root.iterdir()):
                    skill_file = child / "SKILL.md"
                    if not skill_file.is_file() or skill_file in self._announced_skill_files:
                        continue
                    self._announced_skill_files.add(skill_file)
                    meta = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
                    found.append(SkillInfo(
                        name=meta.get("name", child.name),
                        description=str(meta.get("description", "")),
                        path=skill_file,
                    ))
            if current == ws_root or ws_root not in current.parents:
                break
            current = current.parent
        return found

    def _check_skill_discovery(self, tool_name: str, raw_arguments: str) -> str:
        if not self.config.runtime.skill_discovery_enabled:
            return ""
        accessed = _extract_accessed_path(tool_name, raw_arguments)
        if accessed is None:
            return ""
        try:
            target = self.tools._resolve_path(accessed)
        except (OSError, ValueError):
            return ""
        found = self._discover_new_skills_near_path(target)
        if not found:
            return ""
        listing = "\n".join(
            f"- {s.name}: {s.description}" if s.description else f"- {s.name}"
            for s in found
        )
        return (
            "<system-reminder>\n"
            "Found skill(s) not yet in context, near a path you just accessed:\n"
            f"{listing}\n"
            "Call load_skill with the skill name if it's relevant to the "
            "current task.\n"
            "</system-reminder>"
        )

    # ------------------------------------------------------------------
    # Grounding observation recording
    # ------------------------------------------------------------------

    def _record_tool_observation(
        self,
        observations: _ToolObservations,
        tool_name: str,
        raw_arguments: str,
        tool_result_content: str,
    ) -> None:
        """Update grounding observations from one tool call's result.

        Read-only inspection: never raises. Unknown tools are ignored."""
        stripped = (tool_result_content or "").strip()
        is_error_envelope = stripped.startswith('{\n  "error"') or stripped.startswith('{"error"')
        if tool_name == "grep_search":
            if stripped and not is_error_envelope:
                observations.grep_had_matches = True
        elif tool_name == "read_file":
            try:
                args = json.loads(raw_arguments or "{}")
                if "path" in args:
                    observations.read_paths.add(str(args["path"]))
            except (json.JSONDecodeError, TypeError):
                observations.read_paths.add("?")  # at least record that read_file was called
        elif tool_name == "read_files":
            try:
                args = json.loads(raw_arguments or "{}")
                for p in args.get("paths", []) or []:
                    observations.read_paths.add(str(p))
            except (json.JSONDecodeError, TypeError):
                observations.read_paths.add("?")
        elif tool_name == "write_file":
            try:
                args = json.loads(raw_arguments or "{}")
                if "path" in args and not is_error_envelope:
                    observations.write_paths.add(str(args["path"]))
            except (json.JSONDecodeError, TypeError):
                pass
        elif tool_name == "edit_file":
            try:
                args = json.loads(raw_arguments or "{}")
                if "path" in args and not is_error_envelope:
                    observations.edit_paths.add(str(args["path"]))
            except (json.JSONDecodeError, TypeError):
                pass
        elif tool_name == "bash" and not is_error_envelope:
            # Bash returns JSON like {"returncode": N, "stdout": "...", "stderr": "..."}.
            with contextlib.suppress(json.JSONDecodeError, KeyError, TypeError, ValueError):
                payload = json.loads(stripped)
                if isinstance(payload, dict) and "returncode" in payload:
                    observations.last_bash_returncode = int(payload["returncode"])
        elif tool_name == "web_search" and not is_error_envelope:
            observations.web_searched = True
        elif tool_name == "web_fetch" and not is_error_envelope:
            observations.web_fetched = True

    # ------------------------------------------------------------------
    # Tool execution with enforcer + failure hooks
    # ------------------------------------------------------------------

    def _execute_tool(self, tool_name: str, raw_arguments: str) -> str:
        import time as _time
        started = _time.monotonic()

        if not self.tools.has_tool(tool_name):
            self.metrics.record_tool_call(tool_name, _time.monotonic() - started, is_error=True)
            return tool_error_response(
                f"Unknown tool `{tool_name}`.",
                error_code="unknown_tool",
                recoverable=False,
                suggestion="Check available tools and try a different one.",
            )

        enforcer_result = self.permission_enforcer.check(tool_name, raw_arguments)
        if not enforcer_result.allowed:
            self.metrics.record_security_event("enforcer_denied", tool_name, enforcer_result.reason)
            self.metrics.record_tool_call(tool_name, _time.monotonic() - started, is_error=True)
            return tool_error_response(
                enforcer_result.reason,
                error_code="permission_denied",
                recoverable=False,
                suggestion="Request elevated permissions or use a read-only alternative.",
            )

        permission = self.tools.permission_for(tool_name)
        decision = self.permission_policy.authorize(
            permission,
            tool_name,
            tool_input=raw_arguments,
            prompter=self.permission_prompter,
        )
        if not decision.allowed:
            self.metrics.record_security_event("permission_denied", tool_name, decision.reason)
            self.metrics.record_tool_call(tool_name, _time.monotonic() - started, is_error=True)
            return tool_error_response(
                decision.reason,
                error_code="permission_denied",
                recoverable=False,
                suggestion="Request elevated permissions or use a read-only alternative.",
            )

        pre_hook = self.hook_runner.run_pre_tool_use(tool_name, raw_arguments)
        if pre_hook.is_denied:
            deny_msg = f"PreToolUse hook denied tool `{tool_name}`"
            output = merge_hook_feedback(pre_hook.messages, deny_msg, True)
            self.metrics.record_security_event("hook_denied", tool_name, deny_msg)
            self.metrics.record_tool_call(tool_name, _time.monotonic() - started, is_error=True)
            return tool_error_response(output, error_code="hook_denied", recoverable=False)

        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            self.metrics.record_tool_call(tool_name, _time.monotonic() - started, is_error=True)
            return tool_error_response(
                f"Invalid tool arguments for {tool_name}: {exc}",
                error_code="invalid_arguments",
                recoverable=True,
                suggestion="Fix the JSON syntax and retry.",
            )

        try:
            output = self.tools.execute(tool_name, arguments)
            is_error = False
        except McpError as exc:
            _log.warning("MCP error in tool %s: %s", tool_name, exc)
            output = str(exc)
            is_error = True
        except Exception as exc:  # noqa: BLE001
            output = str(exc)
            is_error = True

        output = merge_hook_feedback(pre_hook.messages, output, False)

        if is_error:
            failure_hook = self.hook_runner.run_post_tool_use_failure(
                tool_name, raw_arguments, output,
            )
            if failure_hook.is_denied:
                is_error = True
            output = merge_hook_feedback(failure_hook.messages, output, failure_hook.is_denied)
        else:
            post_hook = self.hook_runner.run_post_tool_use(
                tool_name, raw_arguments, output, is_error,
            )
            if post_hook.is_denied:
                is_error = True
            output = merge_hook_feedback(post_hook.messages, output, post_hook.is_denied)

        self.metrics.record_tool_call(tool_name, _time.monotonic() - started, is_error=is_error)
        self.metrics.record_tool_audit(tool_name, raw_arguments, output, is_error=is_error)

        scan = scan_and_redact_secrets(output)
        if scan.redaction_count > 0:
            _log.warning(
                "Redacted %d secret(s) from tool %s output: %s",
                scan.redaction_count, tool_name, scan.matched_types,
            )
            self.metrics.record_security_event(
                "secret_redacted", tool_name,
                f"Redacted {scan.redaction_count} secret(s): {', '.join(scan.matched_types)}",
            )
            output = scan.redacted_text

        # Cap output size to prevent a single tool call from filling the context window.
        if len(output.encode("utf-8", errors="replace")) > _MAX_TOOL_RESULT_BYTES:
            output = _truncate_tool_output(output, _MAX_TOOL_RESULT_BYTES)

        if is_error:
            return tool_error_response(output, error_code="tool_error", recoverable=True)
        return output


def run_prompt(
    workspace_root: str | Path,
    prompt: str,
    config_path: str | None = None,
    event_callback: EventCallback | None = None,
) -> TurnSummary:
    runtime = AgentRuntime.from_workspace(workspace_root, config_path=config_path)
    return runtime.run_turn(prompt, event_callback=event_callback)
