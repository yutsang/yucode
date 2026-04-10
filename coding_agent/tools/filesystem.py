from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry

# Parity with Rust file_ops.rs: hard limits to prevent OOM / context blowout
MAX_READ_SIZE = 10 * 1024 * 1024   # 10 MB
MAX_WRITE_SIZE = 10 * 1024 * 1024  # 10 MB
_BINARY_PROBE_SIZE = 8192
_GREP_MAX_OUTPUT = 64 * 1024       # 64 KB max grep output


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


def _is_binary_file(path: Any) -> bool:
    """Detect binary files by scanning for NUL bytes (parity with Rust file_ops.rs)."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(_BINARY_PROBE_SIZE)
        return b"\x00" in chunk
    except OSError:
        return False


def _read_file(registry: ToolRegistry, args: dict[str, Any]) -> str:
    path = registry._resolve_path(str(args["path"]))
    size = path.stat().st_size
    if size > MAX_READ_SIZE:
        raise ValueError(
            f"File `{path}` is {size:,} bytes (limit: {MAX_READ_SIZE:,}). "
            "Use offset/limit to read a portion, or use grep_search."
        )
    if _is_binary_file(path):
        raise ValueError(
            f"File `{path}` appears to be binary. "
            "Use bash with `xxd`, `file`, or `hexdump` to inspect binary files."
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    total_lines = len(lines)
    offset = int(args.get("offset", 1))
    limit = int(args.get("limit", total_lines))
    start = max(offset - 1, 0)
    selected = lines[start : start + limit]
    numbered = "\n".join(f"{index}|{line}" for index, line in enumerate(selected, start=start + 1))
    return f"[{len(selected)} lines shown, {total_lines} total]\n{numbered}"


def _write_file(registry: ToolRegistry, args: dict[str, Any]) -> str:
    path = registry._resolve_path(str(args["path"]))
    content = str(args["content"])
    if len(content.encode("utf-8")) > MAX_WRITE_SIZE:
        raise ValueError(
            f"Content size exceeds limit ({MAX_WRITE_SIZE:,} bytes). "
            "Split into smaller writes or use bash."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    num_lines = content.count("\n") + 1
    return f"Wrote {path} ({num_lines} lines)"


def _edit_file(registry: ToolRegistry, args: dict[str, Any]) -> str:
    path = registry._resolve_path(str(args["path"]))
    old = str(args["old_string"])
    new = str(args["new_string"])
    replace_all = bool(args.get("replace_all", False))
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise ValueError(f"`{old}` not found in {path}")
    count = text.count(old) if replace_all else 1
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    # Return a snippet showing the change context
    lines_changed = new.count("\n") + 1
    return (
        f"Edited {path} ({count} replacement{'s' if count > 1 else ''}, "
        f"{lines_changed} line{'s' if lines_changed > 1 else ''} in new text)"
    )


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
    output = result.stdout.strip()
    if len(output) > _GREP_MAX_OUTPUT:
        truncated = output[:_GREP_MAX_OUTPUT]
        last_nl = truncated.rfind("\n")
        if last_nl > 0:
            truncated = truncated[:last_nl]
        line_count = output.count("\n")
        shown = truncated.count("\n")
        return f"{truncated}\n\n[Output truncated: showing {shown}/{line_count} lines. Narrow your search with --glob or a more specific pattern.]"
    return output
