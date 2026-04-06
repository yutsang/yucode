"""Input parsing for slash commands and @ file references.

This module sits between user input and the agent runtime.  It
classifies input lines as slash commands or normal chat, and resolves
``@path`` references into file contents that get prepended to the
prompt sent to the model.

The parser never modifies the runtime directly -- it returns a
ParsedInput object that callers dispatch.
"""

from __future__ import annotations

import enum
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class InputKind(enum.Enum):
    CHAT = "chat"
    SLASH = "slash"


@dataclass(frozen=True)
class FileReference:
    raw: str
    resolved: Path
    content: str


@dataclass(frozen=True)
class ParsedInput:
    kind: InputKind
    raw: str
    command: str = ""
    arguments: str = ""
    file_references: list[FileReference] = field(default_factory=list)
    prompt_without_refs: str = ""

    @property
    def effective_prompt(self) -> str:
        if not self.file_references:
            return self.prompt_without_refs or self.raw
        sections = []
        for ref in self.file_references:
            sections.append(f"<file path=\"{ref.raw}\">\n{ref.content}\n</file>")
        sections.append(self.prompt_without_refs or self.raw)
        return "\n\n".join(sections)


_AT_REF_RE = re.compile(r"@([\w./_-]+(?:\.[\w]+)?)")
_MAX_REF_FILE_CHARS = 8_000
_MAX_TOTAL_REF_CHARS = 24_000

SLASH_COMMANDS = frozenset({
    "help", "status", "config", "tools", "mcp", "skills",
    "clear", "exit", "quit", "compact", "diff", "memory",
    "resume", "permissions", "branch", "commit", "plugins",
    "agents", "save", "metrics", "cost", "pr", "log",
    "worktree", "stash", "export", "model", "version",
})


def parse_input(text: str, workspace: Path) -> ParsedInput:
    text = text.strip()
    if not text:
        return ParsedInput(kind=InputKind.CHAT, raw=text)

    if text.startswith("/"):
        return _parse_slash(text)

    return _parse_chat(text, workspace)


def _parse_slash(text: str) -> ParsedInput:
    parts = text[1:].split(None, 1)
    command = parts[0].lower() if parts else ""
    arguments = parts[1] if len(parts) > 1 else ""
    return ParsedInput(
        kind=InputKind.SLASH,
        raw=text,
        command=command,
        arguments=arguments,
    )


def _parse_chat(text: str, workspace: Path) -> ParsedInput:
    refs: list[FileReference] = []
    seen: set[str] = set()
    total_chars = 0
    clean_text = text

    for match in _AT_REF_RE.finditer(text):
        raw_path = match.group(1)
        if raw_path in seen:
            continue
        seen.add(raw_path)
        resolved = _resolve_ref(raw_path, workspace)
        if resolved is None:
            continue
        content = _read_ref(resolved)
        if not content:
            continue
        truncated = content[:min(_MAX_REF_FILE_CHARS, _MAX_TOTAL_REF_CHARS - total_chars)]
        if not truncated:
            break
        total_chars += len(truncated)
        refs.append(FileReference(raw=raw_path, resolved=resolved, content=truncated))
        clean_text = clean_text.replace(f"@{raw_path}", f"`{raw_path}`")

    return ParsedInput(
        kind=InputKind.CHAT,
        raw=text,
        file_references=refs,
        prompt_without_refs=clean_text.strip(),
    )


def _resolve_ref(raw_path: str, workspace: Path) -> Path | None:
    candidate = (workspace / raw_path).resolve()
    if workspace.resolve() not in (candidate, *candidate.parents):
        return None
    if candidate.exists():
        return candidate
    return None


def _read_ref(path: Path) -> str:
    if path.is_dir():
        entries = sorted(p.name for p in path.iterdir() if not p.name.startswith("."))
        return "Directory listing:\n" + "\n".join(f"  {e}" for e in entries[:50])
    try:
        return path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Slash command helpers (used by cli.py interactive mode)
# ---------------------------------------------------------------------------

def run_git_diff(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "diff"],
        capture_output=True, text=True, cwd=str(workspace),
    )
    return result.stdout.strip() or "(no changes)"


def run_git_branch(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "-v"],
        capture_output=True, text=True, cwd=str(workspace),
    )
    return result.stdout.strip()


def run_git_commit(workspace: Path, message: str) -> str:
    if not message:
        return "Usage: /commit <message>"
    result = subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, text=True, cwd=str(workspace),
    )
    result = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True, text=True, cwd=str(workspace),
    )
    return (result.stdout + result.stderr).strip()


def list_instruction_files(workspace: Path) -> list[Path]:
    candidates = [
        workspace / "YUCODE.md",
        workspace / "CLAW.md",
        workspace / ".yucode" / "instructions.md",
        workspace / ".claw" / "instructions.md",
    ]
    return [p for p in candidates if p.is_file()]


def run_git_log(workspace: Path, count: int = 10) -> str:
    result = subprocess.run(
        ["git", "log", f"--oneline", f"-{count}", "--decorate"],
        capture_output=True, text=True, cwd=str(workspace),
    )
    return result.stdout.strip() or "(no commits)"


def run_git_stash(workspace: Path, action: str = "list") -> str:
    cmd = ["git", "stash"]
    if action == "list":
        cmd.append("list")
    elif action == "pop":
        cmd.append("pop")
    elif action == "push":
        cmd.extend(["push", "-m", "yucode auto-stash"])
    else:
        cmd.append(action)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(workspace))
    return (result.stdout + result.stderr).strip() or "(no stash entries)"


def run_git_pr(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "log", "--oneline", "@{u}..HEAD"],
        capture_output=True, text=True, cwd=str(workspace),
    )
    ahead = result.stdout.strip()
    if result.returncode != 0 or not ahead:
        return "No unpushed commits. Commit and push first."

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=str(workspace),
    ).stdout.strip()

    lines = [f"Branch: {branch}", f"Unpushed commits:\n{ahead}", ""]

    has_gh = subprocess.run(
        ["which", "gh"], capture_output=True, text=True,
    ).returncode == 0

    if has_gh:
        lines.append("To create a PR:  gh pr create --fill")
        lines.append("To push first:   git push -u origin HEAD")
    else:
        lines.append("Push with:  git push -u origin HEAD")
        lines.append("Then create a PR on your Git hosting platform.")

    return "\n".join(lines)


def run_git_worktree(workspace: Path, action: str = "list") -> str:
    if action == "list" or not action:
        result = subprocess.run(
            ["git", "worktree", "list"],
            capture_output=True, text=True, cwd=str(workspace),
        )
        return result.stdout.strip() or "(no worktrees)"

    parts = action.split(None, 1)
    sub = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if sub == "add" and arg:
        name = arg.split("/")[-1].split("\\")[-1]
        worktree_path = workspace.parent / f"{workspace.name}-{name}"
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", name],
            capture_output=True, text=True, cwd=str(workspace),
        )
        return (result.stdout + result.stderr).strip()

    if sub == "remove" and arg:
        result = subprocess.run(
            ["git", "worktree", "remove", arg],
            capture_output=True, text=True, cwd=str(workspace),
        )
        return (result.stdout + result.stderr).strip()

    return f"Usage: /worktree [list|add <branch>|remove <path>]"


def export_session(workspace: Path, messages: list, fmt: str = "md") -> str:
    export_dir = workspace / ".yucode" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    import time
    ts = time.strftime("%Y%m%d-%H%M%S")

    if fmt == "json":
        import json
        path = export_dir / f"session-{ts}.json"
        data = [{"role": m.role, "content": m.content[:2000]} for m in messages]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        path = export_dir / f"session-{ts}.md"
        lines = [f"# Session export {ts}\n"]
        for m in messages:
            role = m.role.upper()
            content = m.content[:3000] if m.content else "(empty)"
            lines.append(f"## {role}\n\n{content}\n")
        path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
