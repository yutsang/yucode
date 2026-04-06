from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry


def filesystem_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            ToolSpec("read_file", "Read a text file from the workspace.",
                     {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]},
                     "read-only", RiskLevel.LOW),
            lambda args: _read_file(registry, args),
        ),
        ToolDefinition(
            ToolSpec("write_file", "Write a text file in the workspace.",
                     {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
                     "workspace-write", RiskLevel.MEDIUM),
            lambda args: _write_file(registry, args),
        ),
        ToolDefinition(
            ToolSpec("edit_file", "Replace text in a workspace file.",
                     {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "old_string", "new_string"]},
                     "workspace-write", RiskLevel.MEDIUM),
            lambda args: _edit_file(registry, args),
        ),
        ToolDefinition(
            ToolSpec("glob_search", "Search files by glob within the workspace.",
                     {"type": "object", "properties": {"pattern": {"type": "string"}, "target_directory": {"type": "string"}}, "required": ["pattern"]},
                     "read-only", RiskLevel.LOW),
            lambda args: _glob_search(registry, args),
        ),
        ToolDefinition(
            ToolSpec("grep_search", "Search file contents with ripgrep.",
                     {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}}, "required": ["pattern"]},
                     "read-only", RiskLevel.LOW),
            lambda args: _grep_search(registry, args),
        ),
    ]


def _read_file(registry: ToolRegistry, args: dict[str, Any]) -> str:
    path = registry._resolve_path(str(args["path"]))
    lines = path.read_text(encoding="utf-8").splitlines()
    offset = int(args.get("offset", 1))
    limit = int(args.get("limit", len(lines)))
    start = max(offset - 1, 0)
    selected = lines[start : start + limit]
    return "\n".join(f"{index}|{line}" for index, line in enumerate(selected, start=start + 1))


def _write_file(registry: ToolRegistry, args: dict[str, Any]) -> str:
    path = registry._resolve_path(str(args["path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(args["content"]), encoding="utf-8")
    return f"Wrote {path}"


def _edit_file(registry: ToolRegistry, args: dict[str, Any]) -> str:
    path = registry._resolve_path(str(args["path"]))
    old = str(args["old_string"])
    new = str(args["new_string"])
    replace_all = bool(args.get("replace_all", False))
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise ValueError(f"`{old}` not found in {path}")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    return f"Edited {path}"


def _glob_search(registry: ToolRegistry, args: dict[str, Any]) -> str:
    target = registry._resolve_path(str(args.get("target_directory", registry.workspace_root)))
    pattern = str(args["pattern"])
    matches = sorted(str(p.relative_to(registry.workspace_root)) for p in target.rglob(pattern))
    return json.dumps(matches[:200], indent=2)


def _grep_search(registry: ToolRegistry, args: dict[str, Any]) -> str:
    command = ["rg", str(args["pattern"]), str(args.get("path", registry.workspace_root))]
    if args.get("glob"):
        command.extend(["--glob", str(args["glob"])])
    result = subprocess.run(command, cwd=registry.workspace_root, capture_output=True, text=True, check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or "rg failed")
    return result.stdout.strip()
