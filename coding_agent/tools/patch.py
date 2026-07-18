"""apply_patch — Codex-style multi-file patch tool.

Grammar (one call can mix any number of these across different files):

    *** Begin Patch
    *** Add File: <path>
    +line1
    +line2
    *** Update File: <path>
    *** Move to: <new path>          (optional; only under Update File)
    @@ <optional context, e.g. a function name>
     <context line>                  (space prefix, unchanged)
    -<old line>
    +<new line>
    *** End of File                  (optional; anchors the hunk at EOF)
    *** Delete File: <path>
    *** End Patch

Matching is exact (contiguous-line), not fuzzy — a hunk that doesn't match
verbatim fails with the searched-for text quoted, plus the same
nearest-match / Unicode-confusable recovery hints edit_file already uses.

Execution is atomic across the whole patch: every operation is validated
(files exist/don't exist as expected, every hunk finds its match) before
anything is written. A failure on operation 3 of 5 writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import RiskLevel, ToolDefinition, ToolSpec
from .filesystem import MAX_WRITE_SIZE, _build_confusable_hint, _build_nearest_match_hint

if TYPE_CHECKING:
    from . import ToolRegistry

_BEGIN_MARKER = "*** Begin Patch"
_END_MARKER = "*** End Patch"
_ADD_PREFIX = "*** Add File: "
_DELETE_PREFIX = "*** Delete File: "
_UPDATE_PREFIX = "*** Update File: "
_MOVE_PREFIX = "*** Move to: "
_EOF_MARKER = "*** End of File"
_HUNK_HEADER_PREFIX = "@@"

_DESCRIPTION = """Apply a multi-file patch in one call: create, delete, move/rename, and edit any number of files atomically (if any operation fails, nothing is written).

Format:
*** Begin Patch
*** Add File: <path>
+line1
+line2
*** Update File: <path>
*** Move to: <new path>          (optional — renames while editing)
@@ <optional context, e.g. a function name>
 <unchanged context line, space-prefixed>
-<line to remove>
+<line to add>
*** End of File                  (optional — anchors the hunk at end of file)
*** Delete File: <path>
*** End Patch

Hunks must match the file's CURRENT content exactly (context + removed lines, contiguous). Prefer this over edit_file when a change spans multiple files or needs to create/delete/rename in the same operation; use edit_file for a single in-place string replacement."""


@dataclass(frozen=True)
class _Hunk:
    context_header: str
    lines: list[tuple[str, str]]  # (marker, text) with marker in (" ", "-", "+")
    end_of_file: bool = False


@dataclass(frozen=True)
class _AddFileOp:
    path: str
    content: str


@dataclass(frozen=True)
class _DeleteFileOp:
    path: str


@dataclass(frozen=True)
class _UpdateFileOp:
    path: str
    hunks: list[_Hunk] = field(default_factory=list)
    move_to: str | None = None


_PatchOp = _AddFileOp | _DeleteFileOp | _UpdateFileOp


def _parse_patch(text: str) -> list[_PatchOp]:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0].strip() != _BEGIN_MARKER:
        raise ValueError(f"Patch must start with '{_BEGIN_MARKER}'")
    if lines[-1].strip() != _END_MARKER:
        raise ValueError(f"Patch must end with '{_END_MARKER}'")
    body = lines[1:-1]

    ops: list[_PatchOp] = []
    i = 0
    while i < len(body):
        line = body[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith(_ADD_PREFIX):
            path = line[len(_ADD_PREFIX):].strip()
            if not path:
                raise ValueError("Add File: missing a path")
            i += 1
            content_lines: list[str] = []
            while i < len(body) and not body[i].startswith("*** "):
                raw = body[i]
                if not raw.startswith("+"):
                    raise ValueError(f"Add File '{path}': expected a '+'-prefixed content line, got: {raw!r}")
                content_lines.append(raw[1:])
                i += 1
            content = "\n".join(content_lines)
            if content_lines:
                content += "\n"
            ops.append(_AddFileOp(path=path, content=content))
        elif line.startswith(_DELETE_PREFIX):
            path = line[len(_DELETE_PREFIX):].strip()
            if not path:
                raise ValueError("Delete File: missing a path")
            ops.append(_DeleteFileOp(path=path))
            i += 1
        elif line.startswith(_UPDATE_PREFIX):
            path = line[len(_UPDATE_PREFIX):].strip()
            if not path:
                raise ValueError("Update File: missing a path")
            i += 1
            move_to: str | None = None
            if i < len(body) and body[i].startswith(_MOVE_PREFIX):
                move_to = body[i][len(_MOVE_PREFIX):].strip()
                i += 1
            hunks, i = _parse_update_hunks(body, i, path)
            ops.append(_UpdateFileOp(path=path, hunks=hunks, move_to=move_to))
        else:
            raise ValueError(f"Unexpected line in patch body: {line!r}")
    if not ops:
        raise ValueError("Patch contains no operations")
    return ops


def _parse_update_hunks(body: list[str], i: int, path: str) -> tuple[list[_Hunk], int]:
    hunks: list[_Hunk] = []
    while i < len(body) and (not body[i].startswith("*** ") or body[i].strip() == _EOF_MARKER):
        context_parts: list[str] = []
        while i < len(body) and body[i].startswith(_HUNK_HEADER_PREFIX):
            header = body[i][len(_HUNK_HEADER_PREFIX):].strip()
            if header:
                context_parts.append(header)
            i += 1
        hunk_lines: list[tuple[str, str]] = []
        end_of_file = False
        while i < len(body):
            row = body[i]
            if row.strip() == _EOF_MARKER:
                end_of_file = True
                i += 1
                break
            if row.startswith("*** ") or row.startswith(_HUNK_HEADER_PREFIX):
                break
            if not row or row[0] not in (" ", "-", "+"):
                raise ValueError(f"Update File '{path}': expected a ' '/'-'/'+' prefixed diff line, got: {row!r}")
            hunk_lines.append((row[0], row[1:]))
            i += 1
        if not hunk_lines:
            raise ValueError(f"Update File '{path}': hunk has no diff lines")
        hunks.append(_Hunk(context_header=" ".join(context_parts), lines=hunk_lines, end_of_file=end_of_file))
    if not hunks:
        raise ValueError(f"Update File '{path}': no hunks found")
    return hunks, i


def _apply_update(original_text: str, hunks: list[_Hunk], path: str) -> str:
    """Apply *hunks* to *original_text* in order, top-to-bottom.

    Raises ValueError with the searched-for text quoted (plus nearest-match /
    confusable diagnostics, reusing edit_file's recovery hints) on the first
    hunk that doesn't find an exact contiguous match."""
    # splitlines() (not split("\n")) so a trailing newline doesn't add a
    # phantom empty last line that throws off *** End of File anchoring;
    # trailing_newline is tracked separately and restored at the end.
    trailing_newline = original_text.endswith("\n")
    lines = original_text.splitlines()
    cursor = 0
    for hunk_idx, hunk in enumerate(hunks, start=1):
        old_block = [text for marker, text in hunk.lines if marker in (" ", "-")]
        new_block = [text for marker, text in hunk.lines if marker in (" ", "+")]

        if not old_block:
            if not hunk.end_of_file:
                raise ValueError(
                    f"{path}: hunk {hunk_idx} has no context/removed lines to anchor an "
                    "insertion point (and is not marked *** End of File)"
                )
            lines = lines + new_block
            cursor = len(lines)
            continue

        start = _find_block(lines, old_block, cursor=cursor, at_eof=hunk.end_of_file)
        if start < 0:
            old_text = "\n".join(old_block)
            hint = (
                _build_confusable_hint(original_text, old_text)
                or _build_nearest_match_hint(original_text, old_text)
            )
            raise ValueError(
                f"{path}: hunk {hunk_idx} context/old lines not found "
                f"(searched from line {cursor + 1}):\n{old_text}{hint}"
            )
        lines = lines[:start] + new_block + lines[start + len(old_block):]
        cursor = start + len(new_block)
    result = "\n".join(lines)
    if trailing_newline and result:
        result += "\n"
    return result


def _find_block(lines: list[str], block: list[str], *, cursor: int, at_eof: bool) -> int:
    n = len(block)
    if at_eof:
        start = len(lines) - n
        return start if start >= cursor and lines[start:start + n] == block else -1
    for i in range(cursor, len(lines) - n + 1):
        if lines[i:i + n] == block:
            return i
    return -1


def patch_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            ToolSpec(
                "apply_patch",
                _DESCRIPTION,
                {"type": "object", "properties": {
                    "patch": {"type": "string", "description": "The full patch text, from *** Begin Patch to *** End Patch."},
                }, "required": ["patch"]},
                "workspace-write", RiskLevel.MEDIUM,
            ),
            lambda args: _apply_patch(registry, args),
        ),
    ]


def _apply_patch(registry: ToolRegistry, args: dict[str, Any]) -> str:
    patch_text = str(args["patch"])
    try:
        ops = _parse_patch(patch_text)
    except ValueError as exc:
        raise ValueError(f"apply_patch: {exc}") from exc

    # Phase 1: resolve paths and validate every operation without writing
    # anything, collecting ALL problems (not just the first) so a retry
    # doesn't need N round-trips to surface N mistakes.
    errors: list[str] = []
    writes: list[tuple[Any, str]] = []       # (resolved_path, new_content)
    deletes: list[Any] = []                   # resolved_path
    moves: list[tuple[Any, Any]] = []         # (old_resolved_path, new_resolved_path)
    summary_lines: list[str] = []

    for op in ops:
        if isinstance(op, _AddFileOp):
            try:
                resolved = registry._resolve_path(op.path)
            except ValueError as exc:
                errors.append(f"Add File '{op.path}': {exc}")
                continue
            if resolved.exists():
                errors.append(f"Add File '{op.path}': already exists (use Update File to edit it)")
                continue
            if len(op.content.encode("utf-8")) > MAX_WRITE_SIZE:
                errors.append(f"Add File '{op.path}': content exceeds limit ({MAX_WRITE_SIZE:,} bytes)")
                continue
            writes.append((resolved, op.content))
            summary_lines.append(f"A {op.path}")
        elif isinstance(op, _DeleteFileOp):
            try:
                resolved = registry._resolve_path(op.path)
            except ValueError as exc:
                errors.append(f"Delete File '{op.path}': {exc}")
                continue
            if not resolved.is_file():
                errors.append(f"Delete File '{op.path}': file not found")
                continue
            deletes.append(resolved)
            summary_lines.append(f"D {op.path}")
        elif isinstance(op, _UpdateFileOp):
            try:
                resolved = registry._resolve_path(op.path)
            except ValueError as exc:
                errors.append(f"Update File '{op.path}': {exc}")
                continue
            if not resolved.is_file():
                errors.append(f"Update File '{op.path}': file not found")
                continue
            try:
                original = resolved.read_text(encoding="utf-8")
                new_content = _apply_update(original, op.hunks, op.path)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if len(new_content.encode("utf-8")) > MAX_WRITE_SIZE:
                errors.append(f"Update File '{op.path}': result exceeds limit ({MAX_WRITE_SIZE:,} bytes)")
                continue
            if op.move_to:
                try:
                    target = registry._resolve_path(op.move_to)
                except ValueError as exc:
                    errors.append(f"Update File '{op.path}' Move to '{op.move_to}': {exc}")
                    continue
                if target.exists():
                    errors.append(f"Update File '{op.path}' Move to '{op.move_to}': target already exists")
                    continue
                writes.append((target, new_content))
                moves.append((resolved, target))
                summary_lines.append(f"M {op.path} -> {op.move_to}")
            else:
                writes.append((resolved, new_content))
                summary_lines.append(f"M {op.path}")

    if errors:
        raise ValueError(
            f"apply_patch: {len(errors)} operation(s) failed validation; nothing was written.\n"
            + "\n".join(f"- {e}" for e in errors)
        )

    # Phase 2: commit. Writes (including move targets) happen before the
    # matching move's source is unlinked, so a mid-commit failure leaves a
    # duplicate rather than losing data.
    for path, content in writes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for old_path, _new_path in moves:
        old_path.unlink()
    for path in deletes:
        path.unlink()

    return "Success. Updated the following files:\n" + "\n".join(summary_lines)
