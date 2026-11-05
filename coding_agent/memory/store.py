"""Persistent cross-session memory.

Memories are individual Markdown files with YAML-style frontmatter,
indexed by a single ``MEMORY.md`` per scope. Two scopes:

  - user:      ~/.yucode/memory/         (shared across all workspaces)
  - workspace: <workspace>/.yucode/memory/ (project-specific)

The store is intentionally simple file IO so the user can inspect or
edit memories with any text editor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

MemoryType = Literal["user", "feedback", "project", "reference"]
MemoryScope = Literal["user", "workspace"]

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SLUG_INVALID = re.compile(r"[^a-z0-9-]+")
_VALID_TYPES: frozenset[str] = frozenset({"user", "feedback", "project", "reference"})

INDEX_FILENAME = "MEMORY.md"
STALE_DAYS_THRESHOLD = 30  # entries older than this get a [stale: Xd] marker


@dataclass(frozen=True)
class MemoryEntry:
    name: str
    description: str
    type: MemoryType
    body: str
    path: Path
    scope: MemoryScope
    saved_at: str = ""  # ISO date (YYYY-MM-DD); empty for legacy entries

    def age_days(self, today: date | None = None) -> int | None:
        if not self.saved_at:
            return None
        try:
            saved = datetime.fromisoformat(self.saved_at).date()
        except ValueError:
            return None
        ref = today or date.today()
        return max(0, (ref - saved).days)


def slugify(name: str) -> str:
    """Lowercase kebab-case slug safe for use as a filename."""
    s = name.lower().strip().replace("_", "-")
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_INVALID.sub("", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "memory"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for raw in match.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        is_nested = line.startswith((" ", "\t"))
        key, value = line.split(":", 1)
        key = key.strip().lstrip("-").strip()
        value = value.strip().strip("'\"")
        if is_nested and not value:
            continue
        meta[key] = value
    return meta, text[match.end():]


def _build_frontmatter(name: str, description: str, mem_type: MemoryType, saved_at: str) -> str:
    safe_desc = description.replace("\n", " ").strip()
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {safe_desc}\n"
        f"saved_at: {saved_at}\n"
        "metadata:\n"
        f"  type: {mem_type}\n"
        "---\n"
    )


class MemoryStore:
    """File-backed persistent memory store with user + workspace scopes."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.user_root = Path.home() / ".yucode" / "memory"
        self.workspace_root = self.workspace / ".yucode" / "memory"

    def root_for(self, scope: MemoryScope) -> Path:
        return self.user_root if scope == "user" else self.workspace_root

    def index_path(self, scope: MemoryScope) -> Path:
        return self.root_for(scope) / INDEX_FILENAME

    def load_indexes_text(self, *, today: date | None = None) -> str:
        """Build the prompt-injected memory index for both scopes.

        Rebuilds the index from live entries (rather than reading the
        static MEMORY.md) so staleness markers reflect actual age, not
        whatever was true when MEMORY.md was last rewritten."""
        ref = today or date.today()
        chunks: list[str] = []
        for scope in ("user", "workspace"):
            scope_t: MemoryScope = scope  # type: ignore[assignment]
            entries = sorted(self.list(scope_t), key=lambda x: x.name)
            if not entries:
                continue
            lines = [f"## Memory index ({scope}) — {self.root_for(scope_t)}"]
            for e in entries:
                hook = e.description.strip().splitlines()[0] if e.description else ""
                age = e.age_days(ref)
                stale = f" [stale: {age}d]" if age is not None and age >= STALE_DAYS_THRESHOLD else ""
                line = f"- `{e.name}` ({e.type}){stale}"
                if hook:
                    line += f" — {hook}"
                lines.append(line)
            chunks.append("\n".join(lines))
        return "\n\n".join(chunks)

    def list(self, scope: MemoryScope | None = None) -> list[MemoryEntry]:
        scopes: tuple[MemoryScope, ...] = ("user", "workspace") if scope is None else (scope,)
        entries: list[MemoryEntry] = []
        for s in scopes:
            root = self.root_for(s)
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.md")):
                if path.name == INDEX_FILENAME:
                    continue
                try:
                    entries.append(self._load_entry(path, s))
                except OSError:
                    continue
        return entries

    def read(self, name: str, scope: MemoryScope | None = None) -> MemoryEntry | None:
        slug = slugify(name)
        scopes: tuple[MemoryScope, ...] = ("workspace", "user") if scope is None else (scope,)
        for s in scopes:
            path = self.root_for(s) / f"{slug}.md"
            if path.exists():
                return self._load_entry(path, s)
        return None

    def save(
        self,
        name: str,
        description: str,
        mem_type: MemoryType,
        body: str,
        scope: MemoryScope = "user",
    ) -> MemoryEntry:
        if mem_type not in _VALID_TYPES:
            raise ValueError(f"Invalid memory type `{mem_type}`; expected one of {sorted(_VALID_TYPES)}")
        slug = slugify(name)
        root = self.root_for(scope)
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{slug}.md"
        saved_at = date.today().isoformat()
        text = _build_frontmatter(slug, description, mem_type, saved_at) + "\n" + body.rstrip() + "\n"
        path.write_text(text, encoding="utf-8")
        self._rebuild_index(scope)
        return MemoryEntry(
            name=slug,
            description=description.strip(),
            type=mem_type,
            body=body.strip(),
            path=path,
            scope=scope,
            saved_at=saved_at,
        )

    def delete(self, name: str, scope: MemoryScope | None = None) -> bool:
        slug = slugify(name)
        scopes: tuple[MemoryScope, ...] = ("workspace", "user") if scope is None else (scope,)
        removed = False
        for s in scopes:
            path = self.root_for(s) / f"{slug}.md"
            if path.exists():
                path.unlink()
                self._rebuild_index(s)
                removed = True
        return removed

    def search(self, query: str) -> list[MemoryEntry]:
        q = query.lower().strip()
        if not q:
            return []
        hits: list[MemoryEntry] = []
        for entry in self.list():
            blob = f"{entry.name} {entry.description} {entry.body}".lower()
            if q in blob:
                hits.append(entry)
        return hits

    # ---- internal -----------------------------------------------------------

    def _load_entry(self, path: Path, scope: MemoryScope) -> MemoryEntry:
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        raw_type = meta.get("type", "project")
        mem_type: MemoryType = raw_type if raw_type in _VALID_TYPES else "project"  # type: ignore[assignment]
        return MemoryEntry(
            name=meta.get("name", path.stem),
            description=meta.get("description", ""),
            type=mem_type,
            body=body.strip(),
            path=path,
            scope=scope,
            saved_at=meta.get("saved_at", ""),
        )

    def _rebuild_index(self, scope: MemoryScope) -> None:
        """Rewrite the on-disk MEMORY.md (human-readable; no dynamic staleness markers)."""
        root = self.root_for(scope)
        if not root.is_dir():
            return
        entries = sorted(self.list(scope), key=lambda x: x.name)
        lines = ["# Memory index", ""]
        if not entries:
            lines.append("_(empty)_")
        else:
            for e in entries:
                hook = (e.description or "").strip().splitlines()[0] if e.description else ""
                date_part = f" [{e.saved_at}]" if e.saved_at else ""
                line = f"- [{e.name}]({e.name}.md){date_part}"
                if hook:
                    line += f" — {hook}"
                lines.append(line)
        (root / INDEX_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
