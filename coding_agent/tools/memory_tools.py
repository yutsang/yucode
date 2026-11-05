"""Persistent memory tools (memory_save / list / read / delete / search)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..memory.store import MemoryStore
from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry


_VALID_TYPES = {"user", "feedback", "project", "reference"}
_VALID_SCOPES = {"user", "workspace"}


def memory_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            ToolSpec(
                "memory_save",
                "Persist a memory across sessions. Use for: user preferences, "
                "feedback corrections you should not repeat, project facts not derivable "
                "from code, references to external systems (Linear/Slack/Grafana). "
                "Do NOT save: code patterns derivable from the repo, ephemeral task state, "
                "anything already in CLAUDE.md/YUCODE.md/AGENTS.md.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short kebab-case identifier (e.g. 'user-prefers-go-style-tests')."},
                        "description": {"type": "string", "description": "One-line summary used to decide relevance in future sessions."},
                        "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"], "description": "user = role/preferences; feedback = corrections to keep; project = ongoing work facts; reference = pointers to external systems."},
                        "content": {"type": "string", "description": "The memory body. For feedback/project, structure as: rule/fact line, then a **Why:** line, then a **How to apply:** line."},
                        "scope": {"type": "string", "enum": ["user", "workspace"], "description": "user = ~/.yucode/memory (cross-project, default); workspace = ./.yucode/memory (project-specific)."},
                    },
                    "required": ["name", "description", "type", "content"],
                },
                "workspace-write",
                RiskLevel.LOW,
            ),
            lambda args: _memory_save(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "memory_list",
                "List saved memories with name, description, type, scope. "
                "Check this before saving a new memory to avoid duplicates.",
                {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string", "enum": ["user", "workspace"], "description": "Optional scope filter; omit to list both."},
                    },
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _memory_list(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "memory_read",
                "Read the full body of a saved memory by name. "
                "The MEMORY.md index in the prompt only shows descriptions; "
                "call this to see the actual content.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "scope": {"type": "string", "enum": ["user", "workspace"], "description": "Optional; searches workspace first, then user."},
                    },
                    "required": ["name"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _memory_read(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "memory_delete",
                "Delete a memory by name when it has turned out to be wrong, outdated, "
                "or no longer relevant.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "scope": {"type": "string", "enum": ["user", "workspace"]},
                    },
                    "required": ["name"],
                },
                "workspace-write",
                RiskLevel.MEDIUM,
            ),
            lambda args: _memory_delete(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "memory_search",
                "Full-text search across memory bodies for a keyword. "
                "Returns matching entries with name, description, scope.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _memory_search(registry, args),
        ),
    ]


def _store(registry: ToolRegistry) -> MemoryStore:
    return MemoryStore(registry.workspace_root)


def _pretty_path(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _memory_save(registry: ToolRegistry, args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    mem_type = str(args.get("type", "")).strip()
    content = str(args.get("content", "")).strip()
    scope = str(args.get("scope", "user")).strip().lower() or "user"
    if not name or not description or not content:
        return json.dumps({"error": "name, description, and content are all required"})
    if mem_type not in _VALID_TYPES:
        return json.dumps({"error": f"type must be one of {sorted(_VALID_TYPES)}"})
    if scope not in _VALID_SCOPES:
        return json.dumps({"error": f"scope must be one of {sorted(_VALID_SCOPES)}"})
    entry = _store(registry).save(name, description, mem_type, content, scope)  # type: ignore[arg-type]
    return json.dumps(
        {"saved": entry.name, "scope": entry.scope, "path": _pretty_path(entry.path)},
        indent=2,
        ensure_ascii=False,
    )


def _memory_list(registry: ToolRegistry, args: dict[str, Any]) -> str:
    scope = args.get("scope")
    entries = _store(registry).list(scope if scope in _VALID_SCOPES else None)  # type: ignore[arg-type]
    return json.dumps(
        [{"name": e.name, "description": e.description, "type": e.type, "scope": e.scope} for e in entries],
        indent=2,
        ensure_ascii=False,
    )


def _memory_read(registry: ToolRegistry, args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    scope = args.get("scope")
    if not name:
        return json.dumps({"error": "name is required"})
    entry = _store(registry).read(name, scope if scope in _VALID_SCOPES else None)  # type: ignore[arg-type]
    if not entry:
        return json.dumps({"error": f"Memory `{name}` not found"})
    return json.dumps(
        {
            "name": entry.name,
            "description": entry.description,
            "type": entry.type,
            "scope": entry.scope,
            "body": entry.body,
        },
        indent=2,
        ensure_ascii=False,
    )


def _memory_delete(registry: ToolRegistry, args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    scope = args.get("scope")
    if not name:
        return json.dumps({"error": "name is required"})
    removed = _store(registry).delete(name, scope if scope in _VALID_SCOPES else None)  # type: ignore[arg-type]
    if not removed:
        return json.dumps({"error": f"Memory `{name}` not found"})
    return json.dumps({"deleted": name}, indent=2)


def _memory_search(registry: ToolRegistry, args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "query is required"})
    hits = _store(registry).search(query)
    return json.dumps(
        [{"name": e.name, "description": e.description, "scope": e.scope} for e in hits],
        indent=2,
        ensure_ascii=False,
    )
