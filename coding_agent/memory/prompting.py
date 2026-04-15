from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig
from .skills import skill_summaries_for_prompt

SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"
MAX_INSTRUCTION_FILE_CHARS = 10_000
MAX_TOTAL_INSTRUCTION_CHARS = 30_000


@dataclass(frozen=True)
class ContextFile:
    path: Path
    content: str


@dataclass(frozen=True)
class ProjectContext:
    cwd: Path
    current_date: str
    git_status: str | None
    git_diff: str | None
    instruction_files: list[ContextFile]
    skills_summary: str = ""


def discover_project_context(
    cwd: Path,
    current_date: str,
    include_git_context: bool,
    explicit_instruction_files: list[str] | None = None,
) -> ProjectContext:
    instruction_files = discover_instruction_files(cwd, explicit_instruction_files or [])
    git_status = _run_git(cwd, ["status", "--short", "--branch"]) if include_git_context else None
    git_diff = _collect_git_diff(cwd) if include_git_context else None
    skills_summary = skill_summaries_for_prompt(cwd)
    return ProjectContext(
        cwd=cwd,
        current_date=current_date,
        git_status=git_status,
        git_diff=git_diff,
        instruction_files=instruction_files,
        skills_summary=skills_summary,
    )


class PromptAssembler:
    def __init__(
        self,
        config: AppConfig,
        project_context: ProjectContext,
        *,
        resumed_messages: int = 0,
        estimated_tokens: int = 0,
    ) -> None:
        self.config = config
        self.project_context = project_context
        self.resumed_messages = resumed_messages
        self.estimated_tokens = estimated_tokens

    def build_sections(self) -> list[str]:
        sections = [
            _intro_section(),
            _system_section(),
            _doing_tasks_section(self.config.runtime.dedup_tool_threshold),
            _executing_actions_section(),
            SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
            self._environment_section(),
            self._project_context_section(),
        ]
        if self.project_context.instruction_files:
            sections.append(self._instruction_files_section())
        if self.project_context.skills_summary:
            sections.append(self.project_context.skills_summary)
        if self.config.runtime.config_dump_in_prompt:
            sections.append(self._config_section())
        return [section for section in sections if section.strip()]

    def render(self) -> str:
        return "\n\n".join(self.build_sections())

    def _environment_section(self) -> str:
        lines = [
            "# Environment context",
            f"- Working directory: {self.project_context.cwd}",
            f"- Date: {self.project_context.current_date}",
            f"- Platform: {platform.system()} {platform.release()}",
        ]
        if self.resumed_messages > 0:
            lines.append(
                f"- Session: RESUMED — {self.resumed_messages} prior message(s) in context. "
                "Do not re-introduce yourself. Continue from where you left off."
            )
        if self.estimated_tokens > 0:
            threshold = self.config.runtime.compact_token_threshold
            pct = min(100, int(self.estimated_tokens * 100 / threshold)) if threshold else 0
            lines.append(
                f"- Context usage: ~{self.estimated_tokens:,} estimated tokens "
                f"({pct}% of compaction threshold). "
                "Use concise tool calls; prefer targeted reads over broad ones."
            )
        return "\n".join(lines)

    def _project_context_section(self) -> str:
        lines = ["# Project context"]
        if self.project_context.git_status:
            lines.extend(["## Git status", self.project_context.git_status])
        if self.project_context.git_diff:
            lines.extend(["## Git diff", self.project_context.git_diff])
        return "\n".join(lines)

    def _instruction_files_section(self) -> str:
        parts = ["# Instruction files"]
        remaining = MAX_TOTAL_INSTRUCTION_CHARS
        for item in self.project_context.instruction_files:
            content = item.content[: min(MAX_INSTRUCTION_FILE_CHARS, remaining)]
            if not content:
                break
            parts.extend([f"## {item.path}", content])
            remaining -= len(content)
            if remaining <= 0:
                break
        return "\n".join(parts)

    def _config_section(self) -> str:
        return "# Runtime config\n" + json.dumps(
            self.config.as_prompt_safe_dict(),
            indent=2,
            sort_keys=True,
        )


def discover_instruction_files(cwd: Path, explicit_paths: list[str]) -> list[ContextFile]:
    files: list[ContextFile] = []
    seen: set[Path] = set()
    candidates: list[Path] = []
    for explicit in explicit_paths:
        candidates.append(Path(explicit).expanduser())
    directories = list(cwd.parents)[::-1] + [cwd]
    for directory in directories:
        candidates.extend(
            [
                directory / "YUCODE.md",
                directory / "YUCODE.local.md",
                directory / ".yucode" / "YUCODE.md",
                directory / ".yucode" / "instructions.md",
                directory / "CLAW.md",
                directory / "CLAW.local.md",
                directory / ".claw" / "CLAW.md",
                directory / ".claw" / "instructions.md",
                directory / "CLAUDE.md",
                directory / "CLAUDE.local.md",
            ]
        )
    for candidate in candidates:
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved in seen or not candidate.exists() or not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8").strip()
        if text:
            files.append(ContextFile(path=resolved, content=text))
            seen.add(resolved)
    return files


def _run_git(cwd: Path, args: list[str]) -> str | None:
    result = subprocess.run(
        ["git", "--no-optional-locks", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _collect_git_diff(cwd: Path) -> str | None:
    staged = _run_git(cwd, ["diff", "--cached"])
    unstaged = _run_git(cwd, ["diff"])
    sections = []
    if staged:
        sections.append("Staged changes:\n" + staged)
    if unstaged:
        sections.append("Unstaged changes:\n" + unstaged)
    return "\n\n".join(sections) if sections else None


def _intro_section() -> str:
    return "\n".join(
        [
            "# Intro",
            "You are a local coding agent running from a Python backend.",
            "Be accurate, prefer direct answers, and avoid pretending that work succeeded when it did not.",
        ]
    )


def _system_section() -> str:
    return "\n".join(
        [
            "# System",
            "- Prefer reading before changing code.",
            "- Treat tool use as explicit actions with side effects.",
            "- Respect repo instructions and config.",
            "- Keep answers concise and useful for coding work.",
        ]
    )


def _doing_tasks_section(dedup_threshold: int = 3) -> str:
    # The runtime blocks the Nth identical call; so N-1 calls are permitted.
    max_allowed = max(1, dedup_threshold - 1)
    dedup_line = (
        "- NEVER call the same tool with the same arguments more than once."
        if max_allowed == 1 else
        f"- NEVER call the same tool with the same arguments more than {max_allowed} times."
    )
    return "\n".join(
        [
            "# Doing tasks",
            "- Understand the request before editing.",
            "- Preserve unrelated user changes.",
            "- Call out uncertainty instead of guessing.",
            "- Use MCP tools when they are the best source of truth.",
            "",
            "# Tool usage limits (CRITICAL)",
            dedup_line,
            "- You have a finite tool call budget per turn. Use calls wisely.",
            "- Prefer fewer, high-quality tool calls over many repetitive ones.",
            "- If a tool returns no useful data, do NOT retry with the same arguments.",
            "- If you cannot find the answer after 3-4 tool calls, report what you found and STOP.",
            "",
            "# Workspace search (ALWAYS before web search)",
            "When looking for a file, note, or topic in the project:",
            "1. Use grep_search with content keywords — do NOT guess exact filenames.",
            "2. If a catalog or index exists (wiki/index.md, index.md, README), read it first",
            "   to find canonical page names and avoid creating duplicates.",
            "3. Use glob_search with patterns like '**/*keyword*' to find files by name fragment.",
            "4. Only fall back to web_search when the topic genuinely does not exist in workspace.",
            "- When writing new files, follow any schema in instruction files (CLAUDE.md, YUCODE.md).",
            "  Check for required frontmatter fields, section order, and link conventions.",
            "",
            "# Web research workflow",
            "When searching the web for information:",
            "1. Search with a specific query (web_search).",
            "2. Review the titles and URLs returned — pick the 1-3 most promising.",
            "3. Fetch those URLs (web_fetch) to extract the actual data.",
            "4. If needed, do ONE more refined search, then fetch again.",
            "5. STOP after 2-3 search+fetch cycles. Summarize what you found.",
            "6. Do NOT loop endlessly through searches. If data is unavailable, say so.",
            "- Prefer fewer, targeted searches over many broad ones.",
            "- When fetching, pass a 'prompt' parameter describing what to extract.",
            "- NEVER call web_search more than 5 times total in one turn.",
        ]
    )


def _executing_actions_section() -> str:
    return "\n".join(
        [
            "# Executing actions with care",
            "- Prefer reversible steps.",
            "- Avoid destructive actions unless explicitly asked.",
            "- Keep the blast radius small and explain limitations clearly.",
        ]
    )
