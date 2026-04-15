from __future__ import annotations

import hashlib
import json
import logging
import os
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
    CompactionError,
    McpError,
    ProviderError,
    tool_error_response,
)
from .providers import OpenAICompatibleProvider
from .session import Message, Session, Usage, UsageTracker

_log = logging.getLogger("yucode.runtime")


EventCallback = Callable[[dict[str, Any]], None]

_AUTO_COMPACT_ENV = "YUCODE_AUTO_COMPACT_INPUT_TOKENS"
_AUTO_COMPACT_ENV_LEGACY = "CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS"
_DEFAULT_AUTO_COMPACT_THRESHOLD = 100_000
# Hard cap on any single tool result to prevent context blowout (32 KB).
# Large outputs (bash stdout, file reads) should use offset/limit instead.
_MAX_TOOL_RESULT_BYTES = 32 * 1024


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


@dataclass
class TurnSummary:
    final_text: str
    iterations: int
    assistant_messages: list[Message] = field(default_factory=list)
    tool_messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    auto_compaction_performed: bool = False


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
        use_coordinator = (
            mode == "multi"
            or (mode == "auto" and is_complex_prompt(prompt))
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
        prior_message_count = len(self.session.messages)
        system_prompt = PromptAssembler(
            self.config,
            project_context,
            resumed_messages=prior_message_count,
            estimated_tokens=self.estimated_tokens(),
        ).render()
        self.session.add_message(Message(role="user", content=prompt))

        max_steps = max_steps_override or self.config.runtime.max_iterations
        max_tool_calls = self.config.runtime.max_tool_calls
        dedup_threshold = self.config.runtime.dedup_tool_threshold
        tool_call_count = 0
        dedup_counts: dict[tuple[str, str], int] = {}
        dedup_blocks_this_turn = 0   # how many hard dedup stops have fired
        budget_exhausted = False

        summary = TurnSummary(final_text="", iterations=0)
        for iteration in range(1, max_steps + 1):
            summary.iterations = iteration
            if event_callback:
                event_callback({"type": "iteration_started", "iteration": iteration})

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
                    self.session.provider_messages(system_prompt),
                    self.tools.definitions_for_provider(),
                    stream_callback=event_callback,
                )
            except Exception as exc:
                _log.error("Provider call failed: %s", exc)
                if self.config.runtime.error_strategy == "strict":
                    raise ProviderError(str(exc)) from exc
                summary.final_text = f"[Provider error: {exc}. The task could not be completed.]"
                if event_callback:
                    event_callback({"type": "error", "error": str(exc), "category": "provider_error"})
                return summary

            self.session.usage.add(response.usage)
            self.usage_tracker.record(response.usage)

            assistant_message = Message(
                role="assistant",
                content=response.text,
                tool_calls=response.tool_calls,
            )
            self.session.add_message(assistant_message)
            summary.assistant_messages.append(assistant_message)
            summary.usage.add(response.usage)

            if not response.tool_calls:
                summary.final_text = response.text
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

                call_key = (tool_call.name, _content_stable_hash(tool_call.arguments))
                dedup_counts[call_key] = dedup_counts.get(call_key, 0) + 1

                if dedup_counts[call_key] >= dedup_threshold:
                    dedup_blocks_this_turn += 1
                    hint = (
                        f" You have been hard-blocked {dedup_blocks_this_turn} time(s) "
                        "this turn. Stop looping and answer with what you have."
                        if dedup_blocks_this_turn > 1 else ""
                    )
                    tool_result_content = json.dumps({
                        "error": (
                            f"HARD STOP: `{tool_call.name}` called {dedup_threshold}+ times "
                            "with identical arguments — this is a hard limit you cannot bypass. "
                            "You MUST do one of: (a) use different arguments, "
                            "(b) switch to a different tool, or "
                            f"(c) stop and answer the user with what you have.{hint}"
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
                    tool_result_content = self._execute_tool(
                        tool_call.name, tool_call.arguments,
                    )
                    tool_call_count += 1

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
        cumulative = self.usage_tracker.total_input_tokens
        if cumulative < self._auto_compact_threshold:
            return
        try:
            force_config = CompactionConfig(
                preserve_recent_messages=self._compaction_config.preserve_recent_messages,
                max_estimated_tokens=0,
            )
            result = compact_session(self.session.messages, force_config)
            if result.removed_message_count > 0:
                self.session.messages = result.compacted_messages
                summary.auto_compaction_performed = True
                _log.info(
                    "Auto-compacted %d messages (cumulative input tokens %d >= threshold %d)",
                    result.removed_message_count, cumulative, self._auto_compact_threshold,
                )
                if event_callback:
                    event_callback({
                        "type": "auto_compaction",
                        "removed": result.removed_message_count,
                        "threshold": self._auto_compact_threshold,
                        "cumulative_input_tokens": cumulative,
                    })
        except Exception as exc:
            _log.warning("Auto-compaction failed: %s", exc)

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def _make_llm_compactor(self):  # type: ignore[return]
        """Return a callable that uses the provider to summarise dropped messages."""
        provider = self.provider

        def _compactor(messages: list[dict]) -> str:
            parts = []
            for m in messages[:30]:
                role = m.get("role", "?")
                content = str(m.get("content") or "")
                # Summarise long tool results inline rather than dumping them
                if role == "tool" and len(content) > 300:
                    content = content[:300] + "…"
                parts.append(f"[{role}]: {content[:400]}")
                for tc in m.get("tool_calls", []):
                    parts.append(f"  tool_call {tc.get('name', '?')}({tc.get('arguments', '')[:120]})")
            transcript = "\n".join(parts)
            prompt = (
                "Summarise the following conversation fragment for a coding agent. "
                "Keep: user goals, files modified/read, decisions made, tool results that matter. "
                "Drop: verbose reasoning and redundant tool calls. "
                "Be concise but complete — the agent will continue work from this summary.\n\n"
                + transcript
            )
            try:
                response = provider.complete(
                    [{"role": "user", "content": prompt}],
                    tools=[],
                )
                return response.text
            except Exception as exc:  # noqa: BLE001
                _log.warning("LLM compactor failed, falling back to heuristic: %s", exc)
                return ""

        return _compactor

    def compact(self, config: CompactionConfig | None = None) -> CompactionResult:
        msg_count = len(self.session.messages)
        est_tokens = self.estimated_tokens()

        self.hook_runner.run_pre_compact(msg_count, est_tokens)

        self._archive_before_compact()

        result = compact_session(self.session.messages, config)
        if result.removed_message_count > 0:
            self.session.messages = result.compacted_messages
            self.hook_runner.run_post_compact(result.removed_message_count, len(result.summary))
            _log.info(
                "Compacted %d messages (est. %d tokens -> %d tokens)",
                result.removed_message_count, est_tokens, self.estimated_tokens(),
            )
        return result

    def _archive_before_compact(self) -> None:
        try:
            from ..config.settings import state_dir
            archives_dir = state_dir(self.workspace_root) / "archives"
            archives_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            sid = self.session.session_id if hasattr(self.session, "session_id") else "unknown"
            archive_path = archives_dir / f"{sid}_{ts}.json"
            data = {
                "session_id": sid,
                "timestamp": ts,
                "message_count": len(self.session.messages),
                "messages": [
                    {"role": m.role, "content": (m.content or "")[:500]}
                    for m in self.session.messages
                ],
            }
            archive_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            _log.warning("Failed to archive session before compaction: %s", exc)

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
            truncated = output.encode("utf-8", errors="replace")[:_MAX_TOOL_RESULT_BYTES].decode("utf-8", errors="replace")
            # Trim to last newline so we don't cut mid-line
            last_nl = truncated.rfind("\n")
            if last_nl > _MAX_TOOL_RESULT_BYTES // 2:
                truncated = truncated[:last_nl]
            output = (
                truncated
                + f"\n\n[Output capped at {_MAX_TOOL_RESULT_BYTES // 1024}KB. "
                "Use offset/limit, a narrower glob/grep pattern, or read specific sections.]"
            )

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
