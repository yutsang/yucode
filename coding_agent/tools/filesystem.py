from __future__ import annotations

import contextlib
import json
import re
import subprocess
from pathlib import Path
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
            ToolSpec(
                "write_file",
                "Write (or overwrite) a text file in the workspace. "
                "Use a workspace-relative path (e.g. 'wiki/note.md'). "
                "The 'content' parameter must be the full file text — never null or empty unless you intend a blank file.",
                {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
                "workspace-write", RiskLevel.MEDIUM,
            ),
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
        ToolDefinition(
            ToolSpec(
                "list_directory",
                "List files and subdirectories in a workspace directory. "
                "Returns each entry's name, type (file/dir), and size for files.",
                {"type": "object", "properties": {"path": {"type": "string", "description": "Directory path relative to workspace root. Defaults to workspace root."}}, "required": []},
                "read-only", RiskLevel.LOW,
            ),
            lambda args: _list_directory(registry, args),
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


def _find_similar_files(workspace: Path, path_arg: str) -> list[str]:
    """Return up to 5 workspace-relative paths with similar names."""
    name = Path(path_arg).name
    if not name:
        return []
    stem = Path(name).stem
    suffix = Path(name).suffix
    seen: set[str] = set()
    results: list[str] = []

    def _collect(pattern: str) -> None:
        for p in workspace.rglob(pattern):
            rel = str(p.relative_to(workspace))
            if rel not in seen:
                seen.add(rel)
                results.append(rel)

    _collect(f"**/{name}")
    if stem:
        _collect(f"**/*{stem}*{suffix}" if suffix else f"**/*{stem}*")
    return results[:5]


def _read_file(registry: ToolRegistry, args: dict[str, Any]) -> str:
    path = registry._resolve_path(str(args["path"]))
    if not path.exists():
        similar = _find_similar_files(registry.workspace_root, str(args["path"]))
        msg = f"File not found: `{path}`."
        if similar:
            msg += f" Did you mean: {', '.join(similar[:3])}?"
        raise FileNotFoundError(msg)
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
    raw_content = args.get("content")
    if raw_content is None:
        raise ValueError(
            "write_file: 'content' must be a string, not null. "
            "Provide the full file text as a string."
        )
    content = str(raw_content)
    if len(content.encode("utf-8")) > MAX_WRITE_SIZE:
        raise ValueError(
            f"Content size exceeds limit ({MAX_WRITE_SIZE:,} bytes). "
            "Split into smaller writes or use bash."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    actual_size = path.stat().st_size
    expected_size = len(content.encode("utf-8"))
    if actual_size == 0 and expected_size > 0:
        raise OSError(
            f"Write appeared to succeed but `{path}` is empty "
            f"({expected_size:,} bytes expected). Check disk space or permissions."
        )
    try:
        rel = str(path.relative_to(registry.workspace_root))
    except ValueError:
        rel = str(path)
    num_lines = content.count("\n") + 1
    return f"Wrote {rel} ({num_lines} lines, {actual_size:,} bytes)"


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
    try:
        rel = str(path.relative_to(registry.workspace_root))
    except ValueError:
        rel = str(path)
    lines_changed = new.count("\n") + 1
    return (
        f"Edited {rel} ({count} replacement{'s' if count > 1 else ''}, "
        f"{lines_changed} line{'s' if lines_changed > 1 else ''} in new text)"
    )


def _glob_fuzzy_fallback(workspace: Path, pattern: str) -> list[dict[str, Any]]:
    """Return alternative patterns that DO have matches when the original returns nothing."""
    from pathlib import PurePosixPath
    pure = PurePosixPath(pattern.replace("\\", "/"))
    name = pure.name
    stem = pure.stem
    suffix = pure.suffix

    results: list[dict[str, Any]] = []
    tried: set[str] = {pattern}

    def _probe(alt: str) -> None:
        if alt in tried or len(results) >= 5:
            return
        tried.add(alt)
        hits = sorted(str(p.relative_to(workspace)) for p in workspace.rglob(alt))[:5]
        if hits:
            results.append({"pattern": alt, "matches": hits})

    if stem:
        _probe(f"**/*{stem}*{suffix}" if suffix else f"**/*{stem}*")
        _probe(f"**/{stem}*{suffix}" if suffix else f"**/{stem}*")
    if name and ("/" in pattern or "\\" in pattern):
        _probe(f"**/{name}")
    if name:
        _probe(f"**/{name.lower()}")
        _probe(f"**/{name.upper()}")
    if suffix and stem:
        _probe(f"**/{stem}*")
    return results


def _glob_search(registry: ToolRegistry, args: dict[str, Any]) -> str:
    target = registry._resolve_path(str(args.get("target_directory", registry.workspace_root)))
    pattern = str(args["pattern"])
    matches = sorted(str(p.relative_to(registry.workspace_root)) for p in target.rglob(pattern))
    if matches:
        return json.dumps(matches[:200], indent=2)

    # No exact matches — try fuzzy alternatives so the model can recover without
    # requiring the user to manually rephrase ("if not ok please find similar").
    suggestions = _glob_fuzzy_fallback(registry.workspace_root, pattern)
    result: dict[str, Any] = {"matches": []}
    if suggestions:
        result["hint"] = (
            f"No files matched '{pattern}'. "
            f"Similar patterns with results are listed in 'similar'."
        )
        result["similar"] = suggestions
    else:
        result["hint"] = f"No files matched '{pattern}' anywhere in the workspace."
    return json.dumps(result, indent=2)


def _list_directory(registry: ToolRegistry, args: dict[str, Any]) -> str:
    raw = args.get("path")
    path = registry._resolve_path(str(raw)) if raw else registry.workspace_root
    if not path.exists():
        similar = _find_similar_files(registry.workspace_root, str(raw or ""))
        msg = f"Directory not found: `{path}`."
        if similar:
            msg += f" Similar paths: {', '.join(similar[:3])}."
        raise FileNotFoundError(msg)
    if not path.is_dir():
        raise ValueError(f"`{path}` is a file, not a directory. Use read_file to read it.")
    entries = []
    for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if entry.name.startswith("."):
            continue
        row: dict[str, Any] = {"name": entry.name, "type": "file" if entry.is_file() else "dir"}
        if entry.is_file():
            with contextlib.suppress(OSError):
                row["size"] = entry.stat().st_size
        entries.append(row)
    try:
        rel = str(path.relative_to(registry.workspace_root))
    except ValueError:
        rel = str(path)
    return json.dumps({"path": rel or ".", "entries": entries, "count": len(entries)}, indent=2)


def _grep_rel(workspace: Path, files: list[str]) -> list[str]:
    result: list[str] = []
    for f in files:
        with contextlib.suppress(ValueError):
            result.append(str(Path(f).relative_to(workspace)))
            continue
        result.append(f)
    return result


def _grep_search(registry: ToolRegistry, args: dict[str, Any]) -> str:
    pattern = str(args["pattern"])
    search_path = str(args.get("path", registry.workspace_root))
    # -i: case-insensitive by default — avoids missing "Queueing" when searching "queueing"
    command = ["rg", "-i", pattern, search_path]
    if args.get("glob"):
        command.extend(["--glob", str(args["glob"])])
    result = subprocess.run(command, cwd=registry.workspace_root, capture_output=True, text=True, check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or "rg failed")
    output = result.stdout.strip()

    if not output:
        tokens = [t for t in re.split(r"\W+", pattern) if len(t) >= 3]
        partial: list[dict[str, Any]] = []

        if len(tokens) >= 2:
            # Multi-word: try each token separately so one misspelled word
            # doesn't kill the whole query.
            for token in tokens:
                cmd = ["rg", "-i", "-l", token, search_path]
                if args.get("glob"):
                    cmd.extend(["--glob", str(args["glob"])])
                r = subprocess.run(cmd, cwd=registry.workspace_root, capture_output=True, text=True, check=False)
                files = [f for f in r.stdout.strip().splitlines() if f][:10]
                if files:
                    partial.append({"term": token, "files": _grep_rel(registry.workspace_root, files)})

        elif len(tokens) == 1:
            # Single word: the case-insensitive search above already failed, so
            # try progressively shorter prefixes to catch trailing-char typos
            # (e.g. "websrach" → "websrac…" → "websra…" until a match is found).
            token = tokens[0]
            for trim in range(1, min(4, len(token) - 2)):
                prefix = token[:-trim]
                if len(prefix) < 4:
                    break
                cmd = ["rg", "-i", "-l", prefix, search_path]
                if args.get("glob"):
                    cmd.extend(["--glob", str(args["glob"])])
                r = subprocess.run(cmd, cwd=registry.workspace_root, capture_output=True, text=True, check=False)
                files = [f for f in r.stdout.strip().splitlines() if f][:10]
                if files:
                    partial.append({"term": prefix + "…", "files": _grep_rel(registry.workspace_root, files)})
                    break  # stop at first successful prefix

        if partial:
            return json.dumps(
                {
                    "hint": (
                        f"No exact match for '{pattern}'. "
                        "Showing files that match individual terms "
                        "(original query may contain a typo or misspelling)."
                    ),
                    "partial_matches": partial,
                },
                indent=2,
            )
        return output  # empty — let the agent know nothing was found

    if len(output) > _GREP_MAX_OUTPUT:
        truncated = output[:_GREP_MAX_OUTPUT]
        last_nl = truncated.rfind("\n")
        if last_nl > 0:
            truncated = truncated[:last_nl]
        line_count = output.count("\n")
        shown = truncated.count("\n")
        return f"{truncated}\n\n[Output truncated: showing {shown}/{line_count} lines. Narrow your search with --glob or a more specific pattern.]"
    return output
